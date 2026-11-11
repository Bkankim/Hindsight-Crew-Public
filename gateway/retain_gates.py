"""Retain-side gates (issue #12, 2차 + 3차) - ONE recall per eligible retain, two readers.

Eligible = the retain content/tags carry a structural signal: kind:gotcha (recurrence
candidate), a negative bracket label (contradiction candidate) or a correction label
(late-recall pattern). Everything else is untouched and pays nothing. For an eligible item
the gateway runs a single upstream recall with the new content as the query and hands the
same result list to both readers:

  * contradiction (3차): positive-polarity precedent above threshold -> warning text the
    caller appends to the SUCCESS response (MCP: extra content block; REST: gateway_warnings).
    The retain is still forwarded and stored - warn, never reject.
  * recurrence (2차): prepare candidates only. gate_effects commits them AFTER confirmed
    retain success and sends notifications after the response, outside the caller's latency.

Fail-open everywhere: a recall failure, a ledger error or a notifier failure degrades to
"no warning / no draft" plus an audit line, never to a failed retain. Audit lines carry
ids, scores and decisions - never the retain text or memory text.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

import httpx

from . import contradiction, recurrence, reframe
from .audit import Audit
from .context_triggers import clip_query, rerank_score, upstream_recall
from .policy import RETAIN_TOOLS

# Bound on gate recalls per request. Latency note (review 2026-09-23): a gate recall measured
# 4.5-6 s live against RECALL_TIMEOUT_SECONDS = 8, and items run sequentially, so a batch of 3
# eligible items can add ~24 s (worse if the rare "Query too long" retry fires). Kept as is:
# eligible retains are rare and single-item in practice; revisit (parallel or 1 item) if the
# audit shows multi-item batches or recall-failed timeouts. Since #197 a kind:gotcha item may also
# spend up to recurrence.SOURCE_LOOKUP_BUDGET_SECONDS (4 s) on source lookups of above-threshold
# observations, so the worst case is ~12 s per item, ~36 s for 3 items.
MAX_ITEMS_PER_REQUEST = 3

Notifier = Callable[[Dict[str, Any]], Awaitable[str]]


@dataclass
class GateOutcome:
    warning: Optional[str] = None
    drafts: List[Dict[str, Any]] = field(default_factory=list)
    correction: Optional[Dict[str, Any]] = None
    events: List[tuple] = field(default_factory=list)


def eligible(content: Any, tags: Any) -> bool:
    if not isinstance(content, str) or not content.strip():
        return False
    return (recurrence.is_gotcha(tags) or contradiction.is_negative(content)
            or recurrence.is_correction(content))


async def evaluate_item(*, app, client: httpx.AsyncClient, upstream_key: str, content: str,
                        tags: Any, identity: Optional[str], bank: str, token: Optional[str],
                        audit: Audit, transport: str, tool: Optional[str],
                        session_id: Optional[str] = None) -> GateOutcome:
    outcome = GateOutcome()
    if not eligible(content, tags):
        return outcome
    # The whole retain used to go out as the query (2000-char cut). Upstream 400s any query
    # above 500 cl100k tokens (~430 Korean chars), so every real-length retain came back
    # recall-failed and both gates were silently off in production (review P1, 2026-09-23).
    query = clip_query(content)
    diag: dict = {}
    results = await upstream_recall(client, upstream_key=upstream_key, bank=bank, query=query,
                                    diag=diag)
    if results is None:
        _audit(audit, token=token, identity=identity, bank=bank, tool=tool,
               decision=contradiction.DECISION, reason="recall-failed",
               extra={"gate": {"transport": transport, "recall_ok": False,
                               "recall_error": diag.get("error")}})
        return outcome

    # --- 3차: contradiction warning --------------------------------------------------
    if contradiction.is_negative(content):
        precedents = contradiction.find_precedents(content, results)
        if precedents:
            outcome.warning = contradiction.warning_text(precedents)
        outcome.events.append((contradiction.DECISION,
               ("precedent:" + ",".join(str(p.get("id", ""))[:8] for p in precedents)
                       if precedents else "no-precedent"),
               {"contradiction": {
                   "transport": transport, "polarity": "negative",
                   "precedent_ids": [p.get("id") for p in precedents],
                   "precedent_scores": [round(rerank_score(p), 4) for p in precedents],
                   "top_score": round(max((rerank_score(r) for r in results), default=0.0), 4),
                   "threshold": contradiction.min_rerank(),
               }}))

    # --- 2차: recurrence drafts -------------------------------------------------------
    ledger: recurrence.RecallLedger = app.state.recall_ledger
    candidates: List[tuple] = []          # (signal, similar_memory or None)
    if recurrence.is_gotcha(tags):
        similar = await _recurrent(recurrence.similar_gotchas(content, results), client=client,
                                   upstream_key=upstream_key, bank=bank, content=content,
                                   outcome=outcome, transport=transport)
        if similar is not None:
            candidates.append((recurrence.SIGNAL_GOTCHA, similar))
    if recurrence.is_correction(content):
        related = sorted((r for r in results if rerank_score(r) >= recurrence.min_rerank()),
                         key=rerank_score, reverse=True)
        if related:
            candidates.append((recurrence.SIGNAL_CORRECTION, related[0]))
        else:
            outcome.events.append((recurrence.DECISION, "skip:no-related-memory",
                                   {"recurrence": {"transport": transport}}))
        served = app.state.injection_ledger.related(
            identity=identity, bank=bank,
            memory_ids=[r["id"] for r in related if isinstance(r.get("id"), str)])
        if served["memory_ids"]:
            outcome.correction = {**served, "transport": transport}
    for signal, similar in candidates:
        similar_ids = [similar["id"]] if similar and isinstance(similar.get("id"), str) else []
        rescue = ledger.rescue_for(identity=identity, bank=bank, memory_ids=similar_ids,
                                   session_id=session_id)
        if signal == recurrence.SIGNAL_CORRECTION and rescue is None:
            # A correction with no recall in the window is just a correction - not the
            # "failed, recalled late, fixed" pattern this signal is about.
            outcome.events.append((recurrence.DECISION, "skip:no-verified-recall",
                                   {"recurrence": {"transport": transport, "signal": signal}}))
            continue
        draft = recurrence.build_draft(bank=bank, signal=signal, new_content=content,
                                       similar=similar, rescue=rescue, tool_hint=None)
        outcome.drafts.append(draft)
    return outcome


async def _recurrent(similar: Sequence[Dict[str, Any]], *, client: httpx.AsyncClient,
                     upstream_key: str, bank: str, content: str, outcome: GateOutcome,
                     transport: str) -> Optional[Dict[str, Any]]:
    """Best above-threshold gotcha that is not derived from this retain's own work (#197).

    Observations are looked up (one GET each, at most recurrence.MAX_SOURCE_LOOKUPS, all
    inside recurrence.SOURCE_LOOKUP_BUDGET_SECONDS); a raw fact is judged on itself with no GET.
    Anything that cannot be judged keeps the pre-#197 behaviour (draft) plus an audit key:
    a failed or malformed lookup, an incomplete source list, the cap, the deadline."""
    deadline = time.monotonic() + recurrence.SOURCE_LOOKUP_BUDGET_SECONDS
    for pos, mem in enumerate(similar):
        meta = {"transport": transport, "signal": recurrence.SIGNAL_GOTCHA,
                "similar_memory_id": mem.get("id"), "similar_score": round(rerank_score(mem), 4)}

        def keep(reason: str) -> Dict[str, Any]:
            outcome.events.append((recurrence.DECISION, reason, {"recurrence": meta}))
            return mem

        try:
            if not recurrence.needs_source_lookup(mem):
                # A raw fact is its own single source: the same rules on its own text and
                # mentioned_at, no GET (no mentioned_at in the recall row -> draft as before).
                detail: Optional[Dict[str, Any]] = {"source_memories": [mem]}
            else:
                if pos >= recurrence.MAX_SOURCE_LOOKUPS:
                    return keep(recurrence.LOOKUP_CAP_REACHED)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return keep(recurrence.LOOKUP_DEADLINE_REACHED)
                detail = (await recurrence.upstream_memory(
                              client, upstream_key=upstream_key, bank=bank, memory_id=mem["id"],
                              timeout=min(recurrence.SOURCE_LOOKUP_TIMEOUT_SECONDS, remaining))
                          if isinstance(mem.get("id"), str) else None)
                if detail is None:
                    return keep(recurrence.SOURCE_LOOKUP_FAILED)
            why = recurrence.derivation(content, detail)
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception:  # noqa: BLE001 - a malformed detail or client error must not drop the outcome
            return keep(recurrence.SOURCE_LOOKUP_FAILED)
        if why == recurrence.SOURCE_INCOMPLETE:
            return keep(why)
        if why is None:
            return mem
        sources = detail.get("source_memories") or []
        outcome.events.append((recurrence.DECISION, why,
                               {"recurrence": {**meta, "source_count": len(sources)}}))
    return None


async def evaluate_mcp(*, app, client, upstream_key, batch: Sequence[Any], decisions: Sequence[Any],
                       identity: Optional[str], bank: str, token: Optional[str],
                       audit: Audit, session_id: Optional[str] = None,
                       verdicts: Optional[Sequence[Any]] = None) -> Dict[Any, GateOutcome]:
    """Prepare per-element effects; no state is committed before the response is known.

    `verdicts` (one rules verdict per batch element, same order) makes the gate judge the tags
    that will actually be STORED, as the REST path does: a bare `gotcha` the rules resolved to
    `kind:gotcha` must be eligible on MCP too. Without it the raw argument tags are used.
    """
    outcomes: Dict[Any, GateOutcome] = {}
    budget = MAX_ITEMS_PER_REQUEST
    for pos, (el, d) in enumerate(zip(batch, decisions)):
        if budget <= 0:
            break
        if not (isinstance(el, dict) and el.get("method") == "tools/call"
                and getattr(d, "tool", None) in RETAIN_TOOLS):
            continue
        el_id = reframe.element_id(el)
        if el_id is None:
            continue              # notification or invalid id: no response to carry effects
        args = (el.get("params") or {}).get("arguments") or {}
        if not isinstance(args, dict):
            continue
        tags = args.get("tags")
        verdict_item = (getattr(verdicts[pos], "item", None)
                        if verdicts is not None and pos < len(verdicts) else None)
        if isinstance(verdict_item, dict) and "tags" in verdict_item:
            tags = verdict_item["tags"]
        if not eligible(args.get("content"), tags):
            continue
        budget -= 1
        try:
            outcome = await evaluate_item(
                app=app, client=client, upstream_key=upstream_key, content=args["content"],
                tags=tags, identity=identity, bank=bank, token=token, audit=audit,
                transport="mcp", tool=d.tool, session_id=session_id)
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception:  # noqa: BLE001 - a gate bug must never fail a retain
            continue
        outcomes[el_id] = outcome
    return outcomes


async def evaluate_rest(*, app, client, upstream_key, items: Sequence[Any], identity: Optional[str],
                        bank: str, token: Optional[str], audit: Audit,
                        tool: str, session_id: Optional[str] = None) -> Dict[int, GateOutcome]:
    """Prepare effects keyed by REST item index."""
    outcomes: Dict[int, GateOutcome] = {}
    budget = MAX_ITEMS_PER_REQUEST
    for idx, item in enumerate(items):
        if budget <= 0:
            break
        if not isinstance(item, dict) or not eligible(item.get("content"), item.get("tags")):
            continue
        budget -= 1
        try:
            outcome = await evaluate_item(
                app=app, client=client, upstream_key=upstream_key, content=item["content"],
                tags=item.get("tags"), identity=identity, bank=bank, token=token, audit=audit,
                transport="rest", tool=tool, session_id=session_id)
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception:  # noqa: BLE001
            continue
        outcomes[idx] = outcome
    return outcomes


def _audit(audit: Audit, *, token, identity, bank, tool, decision, reason, extra) -> None:
    try:
        audit.record(token=token, identity=identity, method="tools/call", tool=tool,
                     attempted_bank=bank, resolved_bank=bank, decision=decision, reason=reason,
                     extra=extra)
    except Exception:  # noqa: BLE001 - telemetry must not fail the call
        return
