"""Body-aware deny-by-default MCP gateway — the single isolation enforcement point.

Clients POST JSON-RPC to /mcp/<bank>/ with their member token (Bearer). The gateway:
  1. resolves the principal from the ACL (never trusts client bank hints),
  2. decides per JSON-RPC element (batch-aware) deny-by-default — any deny → 403 whole request,
  3. only on allow: applies the bank's declared FORMAT rules (N2/N3 — timestamp autofill,
     tag/content/strategy validation) per MCP tools/call element and per REST `items[i]`;
     a violation answers on the MCP isError channel (HTTP 200) / with a REST 422 and the
     whole batch stays unforwarded,
  4. then sanitizes the body (strip client bank, stamp team-retain attribution),
  5. forwards with header allowlist + injected upstream key (MCP: POST /mcp + authoritative
     X-Bank-Id header; REST: bank in the /v1/{tenant}/banks/{bank} path),
  6. streams the response back (SSE/streamable HTTP passthrough),
  7. audits every decision (token fingerprint, attempted vs resolved bank, decision+reason).

Three rejection bodies, deliberately distinct (P2) so a client can tell them apart:
  * authorization — HTTP 403 `{"error":"forbidden","reason":...}` (may not do this at all),
  * FORMAT on MCP — HTTP 200 + `result.isError:true` + `RULE:<id>` (the model's self-
    correction channel; a 403 there would be indistinguishable from an ACL failure),
  * FORMAT on REST — HTTP 422 `{"error":"rule-violation","rule":"RULE:<id>","rule_detail":[...]}`.
    `rule_detail` (not `detail`) because upstream's own Pydantic 422 owns the `detail` key.

create_app(...) takes an injectable httpx client so unit tests run with a mock upstream
(no Docker). Body mutation invariant: single canonical json.dumps re-serialization, so
Content-Length is recomputed by httpx from the new bytes.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
import re
import os
import hashlib
from collections import OrderedDict
from typing import Callable, List, Optional

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse as _StarletteJSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from .acl import ACL
from .audit import (Audit, mental_model_audit as _mental_model_audit, normalize_audit,
                    tag_list as _tag_list, tag_list_keys as _tag_list_keys)
from .bank_policies import BankPolicies
from .context_triggers import TriggerTable
from . import context_triggers
from . import honesty
from . import gate_effects
from . import item_invalidate
from . import item_ledger
from . import item_open_table
from . import item_status
from . import policy
from . import recall_label
from . import recall_prune
from . import recurrence
from . import reframe
from . import reflect_based_on_default
from . import reflect_demand
from . import retain_gates
from . import rules
from . import unescape
from . import webhooks

_BANK_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class JSONResponse(_StarletteJSONResponse):
    """Every JSON answer the gateway builds. Starlette's render encodes strict utf-8, so a lone
    surrogate the caller sent (`"\\ud800"`) and the gateway echoes (a deny reason, the REST
    `gateway_normalized_tags.sent`) was a 500 (issue #135). Same separators and allow_nan as
    Starlette; only a lone surrogate differs - it leaves as the `\\udXXX` escape the caller sent."""

    def render(self, content) -> bytes:
        return json.dumps(content, ensure_ascii=False, allow_nan=False, indent=None,
                          separators=(",", ":")).encode("utf-8", "backslashreplace")


def _is_error_result(el_id, text: str) -> dict:
    """MCP CallToolResult carrying a rule violation (SEP-1303 self-correction channel)."""
    return {
        "jsonrpc": "2.0",
        "id": el_id,
        "result": {"content": [{"type": "text", "text": text}], "isError": True},
    }


def _valid_jsonrpc_id(value) -> bool:
    """JSON-RPC 2.0 id: a string, a number (bool is not one), or null (issue #127). NaN and
    Infinity parse as floats but JSON cannot carry them back (JSONResponse refuses, a 500)."""
    if isinstance(value, float):
        return math.isfinite(value)
    return value is None or (isinstance(value, (str, int)) and not isinstance(value, bool))


def _shape_rejection(payload, batch, shape_violations) -> JSONResponse:
    """Whole-request rejection for a malformed JSON-RPC envelope (issue #70).

    Same fail-closed contract as a rule violation: nothing is forwarded, and addressable
    tools/call siblings get `RULE:batch-aborted`. A non-object element - or an offending
    object without a usable id (issue #107: it is not a notification, it is a malformed
    request) - has no id to carry an isError result, so it gets JSON-RPC's own
    `-32600 Invalid Request` (id null). Only a VALID id-less element is a notification that
    gets no response. A usable id is a string, a non-bool number or null (issue #127): an
    object, array or bool id is never reflected and counts as no id, so it is judged exactly
    like an absent one - an offender gets -32600, a violation-free sibling is a notification.
    (A lone request whose only fault is such an id never reaches this function and is
    forwarded as before.)
    """
    def addressable(el) -> bool:
        return isinstance(el, dict) and el.get("id") is not None and _valid_jsonrpc_id(el.get("id"))

    offender_desc = next(
        (f"id={el.get('id')}" if addressable(el) else f"index={idx}"
         for idx, (el, v) in enumerate(zip(batch, shape_violations)) if v is not None),
        "unknown",
    )
    responses = []
    for el, v in zip(batch, shape_violations):
        if not isinstance(el, dict) or (v is not None and not addressable(el)):
            responses.append({"jsonrpc": "2.0", "id": None,
                              "error": {"code": -32600, "message": "Invalid Request: " + v.message}})
            continue
        if not addressable(el):
            continue        # notification (no usable id): JSON-RPC forbids a response element
        responses.append(_is_error_result(
            el.get("id"),
            v.message if v is not None
            else "RULE:batch-aborted (caused by element %s)" % offender_desc,
        ))
    out = responses if isinstance(payload, list) or not responses else responses[0]
    return JSONResponse(out, status_code=200)


def _failure_body(payload) -> bool:
    """True when a 2xx upstream REST body reports a failure: a truthy `error`, `success:false`, or
    a `status` in gate_effects.FAILURE. Same bar as MCP honesty.annotate_element (a body that
    says it failed). Bodies that merely stored nothing (`memory_ids == []`, `items_count == 0`),
    which storage_status also refuses to confirm, are NOT judged here."""
    return isinstance(payload, dict) and bool(
        payload.get("error") or payload.get("success") is False
        or str(payload.get("status", "")).lower() in gate_effects.FAILURE)


def _bearer(request: Request) -> Optional[str]:
    h = request.headers.get("authorization", "")
    if h.lower().startswith("bearer "):
        return h[7:].strip() or None
    return None


def _session_scope(request: Request) -> Optional[str]:
    """Only a fingerprint is kept; REST without a session remains identity-bank scoped."""
    value = request.headers.get("mcp-session-id")
    return hashlib.sha256(value.encode()).hexdigest() if value else None


# --- MCP client name per session (issue #37) -------------------------------------------
# Every runtime shares one token, so the only place a client announces itself is the MCP
# initialize request (`params.clientInfo.name`). The session id is decided by upstream's
# RESPONSE header, so the name is bound after the answer and read back on later retains.
# Keyed by the same fingerprint _session_scope() uses (no raw session id is held) and
# capped LRU so a client that opens sessions forever cannot grow the process. A gateway
# restart forgets the map: those sessions log "unknown" until they initialize again.
CLIENT_UNKNOWN = "unknown"
_CLIENT_NAME_MAX = 64
_CLIENT_SESSIONS_MAX = 1024


def _clean_client_name(value, default: Optional[str] = CLIENT_UNKNOWN) -> Optional[str]:
    """The printable, length-capped name, or `default` when there is no usable one.
    default=None lets a caller tell "no usable name" from a client literally named "unknown"."""
    if not isinstance(value, str):
        return default
    cleaned = "".join(ch for ch in value if ch.isprintable()).strip()[:_CLIENT_NAME_MAX]
    return cleaned or default


def _initialize_client_name(batch):
    """(True, name) for the first initialize element - name is None when it carries no usable
    clientInfo.name (the caller must not let that overwrite a name already bound to the
    session) - or (False, None) when the batch has no initialize."""
    for el in batch:
        if isinstance(el, dict) and el.get("method") == "initialize":
            params = el.get("params")
            info = params.get("clientInfo") if isinstance(params, dict) else None
            return True, _clean_client_name(info.get("name") if isinstance(info, dict) else None, None)
    return False, None


class _ClientSessions:
    def __init__(self, limit: int = _CLIENT_SESSIONS_MAX):
        self._limit = limit
        self._names: "OrderedDict[str, str]" = OrderedDict()   # only sessions with a usable name

    def bind(self, session: Optional[str], name: Optional[str]) -> None:
        if not session:
            return
        if name is None:
            # A nameless initialize never takes an LRU slot (issue #107): it would evict a named
            # session for nothing, and lookup()/peek() answer the same for "unbound" and
            # "bound without a name". A known name survives a nameless re-initialize.
            if session in self._names:
                self._names.move_to_end(session)
            return
        self._names[session] = name
        self._names.move_to_end(session)
        while len(self._names) > self._limit:
            self._names.popitem(last=False)

    def lookup(self, session: Optional[str]) -> str:
        if not session or session not in self._names:
            return CLIENT_UNKNOWN
        self._names.move_to_end(session)     # a session still retaining is not the one to evict
        return self._names[session] or CLIENT_UNKNOWN

    def peek(self, session: Optional[str]) -> Optional[str]:
        """The bound name, or None (unbound, or bound without a usable name). Leaves the LRU
        order alone, so reading for the recall ledger never changes which session is evicted."""
        return self._names.get(session) if session else None

    def __len__(self) -> int:
        return len(self._names)


def _retain_tags_extra(*, transport: str, sent, stored, client: Optional[str] = None) -> dict:
    """Metadata for the per-retain "retain-tags" audit line: tag strings only, never content.
    A list that lost tags to the count or byte bound also records `<list>_count` and
    `<list>_omitted`, one that had a tag cut `<list>_truncated` (issues #99/#117); a list that
    lost nothing adds no key."""
    info = {"transport": transport}
    for name, value in (("sent", sent), ("stored", stored)):
        shown, count, truncated = _tag_list(value)
        info[name] = shown
        info.update(_tag_list_keys(name, shown, count, truncated))
    if client is not None:
        info["client"] = client
    return {"retain_tags": info}


def _query_reusable(args: dict) -> bool:
    # Trigger rows cannot express these additional restrictions; never silently broaden.
    return not any(args.get(k) for k in ("types", "tag_groups", "query_timestamp", "min_scores"))


def _recall_ids_from_mcp(media: Optional[str], body: Optional[bytes], wanted_ids: set) -> dict:
    """{element id: [memory ids]} read from a recall tools/call response (SSE or JSON).

    Read-only; any parse failure yields {} and is excluded from retrieval evidence.
    Unwrapping (incl. upstream's double-encoded structuredContent.result string) lives in
    recurrence.mcp_recall_payload so the fixture-driven test can pin the real shape.
    """
    parsed = reframe.parse_response(media, body)
    if parsed.failure is not None:
        return {}
    out: dict = {}
    for frame in parsed.frames:
        elements = frame.payload if isinstance(frame.payload, list) else [frame.payload]
        for el in elements:
            el_id = reframe.element_id(el)
            if el_id is None or el_id not in wanted_ids:
                continue
            payload = recurrence.mcp_recall_payload(el.get("result"))
            if payload is not None:
                out[el_id] = recurrence.recall_result_ids(payload)
    return out


# Upstream request timeout (issue #262). v1.0.0 reflect with a failing primary claude-code member fails
# over to the next member but finishes upstream at ~30.4s (copy measurement 2026-10-15); the old 30s cut
# turned that delivered answer into an HTTP 500. 60s matches Codex's default MCP tool timeout.
UPSTREAM_TIMEOUT_SECONDS = 60.0
# Issue #473 b: the per-request upstream bank-config read behind a REST write. Short on purpose: a
# slow or absent answer denies the write (fail-closed) instead of holding the caller for a minute.
BANK_CONFIG_LOOKUP_TIMEOUT_SECONDS = 5.0
# Marks that read as the gateway's own (not a forwarded client GET .../config), for upstream access
# logs and for mock upstreams that answer it apart from the calls they record.
BANK_CONFIG_LOOKUP_HEADER = ("x-hc-gateway-op", "bank-config-lookup")


def create_app(
    *,
    acl: ACL,
    audit: Optional[Audit] = None,
    upstream_base: str = "http://hindsight:8888",
    upstream_key: str = "",
    http_client: Optional[httpx.AsyncClient] = None,
    attribution_enabled: bool = True,
    bank_policies: Optional[BankPolicies] = None,
    trigger_table: Optional[TriggerTable] = None,
    recall_ledger: Optional[recurrence.RecallLedger] = None,
    pending_triggers: Optional[recurrence.PendingTriggers] = None,
    notifier: Optional[retain_gates.Notifier] = recurrence.notify_discord,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    app = FastAPI(title="hindsight-crew-gateway")
    audit = audit or Audit()
    # Per-bank rule profiles (N1). Injected already-validated (env resolution lives in
    # server.py) so validation is synchronous on the create_app path, never in lifespan —
    # a non-context TestClient would otherwise skip it. None = loose defaults for every
    # bank, i.e. behaviour identical to pre-N1.
    app.state.bank_policies = bank_policies or BankPolicies.defaults_only()
    client = http_client or httpx.AsyncClient(base_url=upstream_base, timeout=UPSTREAM_TIMEOUT_SECONDS)
    app.state.http_client = client
    # 길목 주입 (issue #12). Table injected pre-validated like bank_policies (env resolution in
    # server.py); None = empty table, i.e. /context-triggers answers empty for every text.
    app.state.trigger_table = trigger_table if trigger_table is not None else TriggerTable.empty()
    app.state.recall_ledger = recall_ledger or recurrence.RecallLedger()
    app.state.pending_triggers = pending_triggers or recurrence.PendingTriggers()
    app.state.recurrence_dedup = recurrence.Deduper()
    app.state.injection_ledger = recurrence.InjectionLedger()
    app.state.notifier = notifier
    # Process-cumulative count of response rewrites that had to fail open (N4/N5). Every
    # increment means a client got upstream's bytes UNCORRECTED — the honest failure mode,
    # but one that must be visible: verify's gate asserts a start→end delta of 0.
    app.state.rewrite_failures = 0
    # initialize clientInfo.name per MCP session (issue #37); read by the retain-tags audit line.
    app.state.mcp_clients = _ClientSessions()

    def _ledger_client(request: Request) -> Optional[str]:
        """Recall ledger `client` (issue #89, MCP rows only): the cleaned name #37 bound to this
        request's session, or None (no session, unseen session, nameless initialize - the
        retain-tags audit line spells those "unknown"). peek(): reading must not reorder the LRU."""
        return app.state.mcp_clients.peek(_session_scope(request))

    def _record_mcp_rewrite_failure(*, token, principal, bank, failure: str) -> None:
        """One MCP response rewrite failed open: raise the counter + one audit alarm line.
        Shared by the buffered path and the streamed reflect unescape (issue #26)."""
        app.state.rewrite_failures += 1
        audit.record(
            token=token, identity=(principal.identity if principal else None),
            method="tools/call", tool=None, attempted_bank=bank, resolved_bank=bank,
            decision="rewrite-failure", reason="response-rewrite:" + failure,
        )

    # Upstream reachability is reported as its own field, deliberately NOT folded into
    # "ok". This endpoint is the gateway's own liveness, and whatever watches it must not
    # restart the gateway because hindsight is down - that would be the wrong remedy. But
    # the window has to be visible: on 2026-07-26 the main container sat stopped for about
    # two hours while this endpoint kept answering 200, because it only ever described
    # itself. Recall was dead and nothing said so.
    #
    # Cached for a few seconds so health polling cannot become upstream load, with a short
    # probe timeout so a hung upstream cannot hang this endpoint. The detail string is a
    # coarse status or an exception class name only - never an upstream body, which would
    # risk leaking recall content into logs and monitors.
    _UPSTREAM_TTL_SECONDS = 5.0
    app.state.upstream_probe = (0.0, None)

    async def _upstream_state() -> dict:
        checked_at, cached = app.state.upstream_probe
        now = time.monotonic()
        if cached is not None and now - checked_at < _UPSTREAM_TTL_SECONDS:
            return cached
        try:
            resp = await client.get("/health", timeout=1.5)
            ok = resp.status_code == 200
            state = {"upstream_ok": ok, "upstream": "ok" if ok else f"status_{resp.status_code}"}
        except Exception as exc:  # noqa: BLE001 - any failure means unreachable; class name only
            state = {"upstream_ok": False, "upstream": type(exc).__name__}
        app.state.upstream_probe = (now, state)
        return state

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "rewrite_failures": app.state.rewrite_failures, **await _upstream_state()}

    # ---- 길목 주입 (issue #12, 1차): task text in, injection block out -----------------
    # Same auth model as every other surface (Bearer -> ACL -> bank in path). The body is the
    # caller's TASK TEXT, matched against the declared trigger table; on the first match the
    # gateway runs one live recall and returns the framed, capped, secret-scrubbed injection.
    # Miss -> empty 200 (text/plain) so a shim can prepend the body unconditionally.
    # ?format=json returns {matched, trigger_id, injection, memory_ids, tokens_est, recall_ok}.
    @app.post("/context-triggers/{bank}")
    @app.post("/context-triggers/{bank}/")
    async def context_trigger(bank: str, request: Request):
        token = _bearer(request)
        principal = acl.principal_for(token)
        identity = principal.identity if principal else None
        as_json = request.query_params.get("format") == "json"

        def deny(reason: str):
            audit.record(token=token, identity=identity, method="POST", tool="context-triggers",
                         attempted_bank=bank, resolved_bank=None, decision="deny", reason=reason)
            return JSONResponse({"error": "forbidden", "reason": reason}, status_code=403)

        if principal is None:
            return deny("unknown-or-missing-token")
        if not _BANK_RE.match(bank or ""):
            return deny("bad-bank-format")
        if bank not in principal.allowed_banks():
            return deny(f"bank-not-in-acl:{bank}")
        raw = await request.body()
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return JSONResponse({"error": "bad json"}, status_code=400)
        if not isinstance(body, dict) or not isinstance(body.get("text"), str):
            return JSONResponse({"error": "bad request", "reason": "text (string) required"},
                                status_code=400)
        text_in = body["text"]
        tool = body.get("tool") if isinstance(body.get("tool"), str) else None
        table: TriggerTable = app.state.trigger_table.refreshed()
        app.state.trigger_table = table
        matched = table.match(bank=bank, tool=tool, text=text_in)
        event = {"tool": tool, "text_chars": len(text_in), "matched_ids": [t.id for t in matched],
                 "served": None, "memory_ids": [], "tokens_est": 0, "recall_ok": None,
                 "recall_error": None}
        injection, reason = "", "miss"
        if matched:
            trig = matched[0]                       # first match wins (rows are ordered)
            diag: dict = {}
            deadline = context_triggers.TRIGGER_RECALL_TIMEOUT_SECONDS
            # httpx's timeout is per phase and the "query too long" retry gets a fresh one, so
            # the whole recall also runs under one wall-clock deadline (issue #268 review).
            try:
                results = await asyncio.wait_for(context_triggers.upstream_recall(
                    client, upstream_key=upstream_key, bank=bank, query=trig.query,
                    tags=trig.tags, tags_match=trig.tags_match, budget=trig.recall_budget,
                    max_tokens=(trig.recall_max_tokens if trig.recall_max_tokens is not None
                                else context_triggers.RECALL_MAX_TOKENS),
                    timeout=deadline, diag=diag), deadline)
            except asyncio.TimeoutError:
                results, diag["error"] = None, "timeout"
            event["recall_ok"] = results is not None
            if results is None:
                reason = f"recall-failed:{trig.id}"
                event["recall_error"] = diag.get("error")
            else:
                injection, used, tokens = context_triggers.build_injection(
                    results, max_memories=trig.max_memories, max_tokens=trig.max_tokens)
                event.update(served=trig.id, memory_ids=used, tokens_est=tokens)
                reason = f"match:{trig.id}" if injection else f"match-empty:{trig.id}"
                app.state.injection_ledger.record(identity=identity, bank=bank,
                                                  trigger_id=trig.id, memory_ids=used)
        # Never the text: ids, counts and the decision only.
        audit.record(token=token, identity=identity, method="POST", tool="context-triggers",
                     attempted_bank=bank, resolved_bank=bank, decision=context_triggers.DECISION,
                     reason=reason, extra={"context_trigger": event})
        if as_json:
            return JSONResponse({"matched": bool(event["served"]), "trigger_id": event["served"],
                                 "injection": injection, "memory_ids": event["memory_ids"],
                                 "tokens_est": event["tokens_est"], "recall_ok": event["recall_ok"]})
        # upstream 이 준 값에 lone surrogate 가 있어도 500 이 아니다(issue #138): 본문은 surrogate 만 `\udXXX`
        # 글자열로(JSON 응답의 injection 과 같은 표기), 헤더 값은 latin-1 밖 문자를 이스케이프한다.
        headers = {"x-hc-trigger": event["served"] or "",
                   "x-hc-memory-ids": ",".join(event["memory_ids"]).encode("latin-1", "backslashreplace")
                   .decode("latin-1"),
                   "x-hc-tokens-est": str(event["tokens_est"])}
        return Response(content=injection.encode("utf-8", "backslashreplace"), status_code=200,
                        media_type="text/plain; charset=utf-8", headers=headers)

    # upstream 웹훅 수신 (issue #303): 기능 플래그 HC_WEBHOOK_RECEIVER 기본 꺼짐, 꺼지면 이 라우트는 등록되지 않는다.
    # Bearer/ACL 표면이 아니라 HMAC 서명(webhooks.py)이 문이다.
    webhooks.register(app, audit=audit, clock=clock)

    # Root multi-bank surface is never exposed (enumeration). Explicit 403.
    @app.post("/mcp/")
    @app.post("/mcp")
    async def root_blocked(request: Request):
        token = _bearer(request)
        p = acl.principal_for(token)
        audit.record(token=token, identity=(p.identity if p else None), method="*",
                     tool=None, attempted_bank=None, resolved_bank=None,
                     decision="deny", reason="root-multibank-blocked")
        return JSONResponse({"error": "multi-bank endpoint disabled"}, status_code=403)

    @app.post("/mcp/{bank}/")
    @app.post("/mcp/{bank}")
    async def proxy(bank: str, request: Request):
        arrived = clock()        # recall ledger `arrived_ts` (issue #89): before any work on it
        # Pinned on arrival too (issue #107): a re-initialize landing while upstream is awaited
        # must not rename the call that was already in flight.
        arrived_client = _ledger_client(request)
        token = _bearer(request)
        principal = acl.principal_for(token)

        # Bank format guard (defense-in-depth vs traversal/encoding).
        if not _BANK_RE.match(bank or ""):
            audit.record(token=token, identity=(principal.identity if principal else None),
                         method="tools/call", tool=None, attempted_bank=bank,
                         resolved_bank=None, decision="deny", reason="bad-bank-format")
            return JSONResponse({"error": "forbidden"}, status_code=403)

        raw = await request.body()
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return JSONResponse({"error": "bad json"}, status_code=400)

        batch = payload if isinstance(payload, list) else [payload]

        # --- DECIDE first (fail-closed): any denied element rejects the whole request ---
        bank_policy = app.state.bank_policies.for_bank(bank)
        kb_read_on = policy.kb_read_enabled()     # issue #302: read once per request, same for every element
        decisions = []
        shape_violations = []   # issue #70: envelope shape, answered after every decide()
        for el in batch:
            shape = rules.request_shape_violation(el)
            el_obj = el if isinstance(el, dict) else {}
            method = el_obj.get("method", "")
            method = method if isinstance(method, str) else ""   # decide() hashes it
            params = el_obj.get("params")
            tool = (params.get("name")
                    if method == "tools/call" and isinstance(params, dict) else None)
            tool = tool if isinstance(tool, str) else None
            d = policy.decide(
                principal, bank, method, tool, bank_policy=bank_policy, kb_read_enabled=kb_read_on
            )
            # Authz still answers first (403 for token/root/ACL, which leave resolved_bank
            # unset). Past that, a malformed element is a FORMAT failure, not the
            # method-not-allowed / missing-tool-name deny decide() derives from its debris.
            if shape is not None and d.resolved_bank is not None:
                audit.record(token=token, identity=(principal.identity if principal else None),
                             method=method, tool=tool,
                             attempted_bank=bank, resolved_bank=bank, decision="enforce-reject",
                             reason="rule-violation:" + shape.rule)
                decisions.append(d)
                shape_violations.append(shape)
                continue
            shape_violations.append(None)
            # issue #238: which mental model an allowed get_mental_model read asked for (the
            # weekly demand signal); no other tool and no deny line gets the key.
            demand_extra = (_mental_model_audit(params.get("arguments"))
                            if d.allow and tool == "get_mental_model" and isinstance(params, dict)
                            else None)
            audit.record(token=token, identity=(principal.identity if principal else None),
                         method=method, tool=tool, attempted_bank=bank,
                         resolved_bank=(d.resolved_bank), decision=("allow" if d.allow else "deny"),
                         reason=d.reason, extra=demand_extra)
            decisions.append(d)
            if not d.allow:
                # A method outside the whitelist is a protocol-level "no such method", not an
                # authorization failure. HTTP 403 there makes clients that probe non-standard
                # methods abort the whole connection (measured 2026-09-05: ZCode calls
                # `server/discover` during version negotiation and reads 403 as fatal).
                # JSON-RPC -32601 is the in-protocol answer: nothing is forwarded upstream,
                # the deny is still audited, and authz denies keep their 403 channel (P2).
                if d.reason.startswith("method-not-allowed:") and len(batch) == 1:
                    el_id = (el or {}).get("id")
                    if el_id is None:
                        return Response(status_code=202, media_type="application/json")
                    return JSONResponse(
                        {"jsonrpc": "2.0", "id": el_id,
                         "error": {"code": -32601, "message": "Method not found"}},
                        status_code=200,
                    )
                return JSONResponse({"error": "forbidden", "reason": d.reason}, status_code=403)

        if any(shape_violations):
            return _shape_rejection(payload, batch, shape_violations)

        # --- RULES (N2), then MUTATE — only after all allowed ---
        # Order contract: decide() FIRST → rules (validate/autofill) → sanitize (mutate).
        # Rules run on the CLIENT's arguments (pre-sanitize) so the model is judged on what
        # it actually sent; sanitize then re-applies the bank pin/attribution on top of the
        # corrected item, keeping the security invariants last-word.
        now_iso = rules.utc_now_iso()
        verdicts = []
        for el, d in zip(batch, decisions):
            if (el or {}).get("method") == "tools/call":
                params = (el or {}).get("params") or {}
                verdicts.append(rules.evaluate_tool_call(
                    d.tool, params.get("arguments"), bank_policy, now_iso))
            else:
                verdicts.append(rules.ItemVerdict(item=None))
        request.state.rule_autofills = []   # always defined for the response side (N5)

        # A format violation anywhere kills the whole batch (never a partial write) and is
        # answered on the isError channel — NOT 403, which is reserved for authz (P2).
        if any(not v.ok for v in verdicts):
            # The blamed element is resolved BEFORE emitting, so siblings that precede the
            # offender still name it (a single pass would print "None" for them).
            offender_desc = next(
                (f"id={(el or {}).get('id')}" if (el or {}).get("id") is not None
                 else f"index={idx} (notification, no id)"
                 for idx, (el, v) in enumerate(zip(batch, verdicts)) if not v.ok),
                "unknown",
            )
            responses = []
            for el, d, v in zip(batch, decisions, verdicts):
                el_id = (el or {}).get("id")
                if not v.ok:
                    audit.record(
                        token=token, identity=(principal.identity if principal else None),
                        method=(el or {}).get("method", ""), tool=d.tool, attempted_bank=bank,
                        resolved_bank=bank, decision="enforce-reject",
                        reason="rule-violation:" + ",".join(v.rule_ids),
                    )
                if el_id is None:
                    continue        # notification: JSON-RPC forbids a response element
                responses.append(_is_error_result(
                    el_id,
                    v.message if not v.ok
                    else "RULE:batch-aborted (caused by element %s)" % offender_desc,
                ))
            # Shape preservation: array in → array out, single object in → single object
            # out. An all-notification request has no addressable response (spec), so the
            # honest answer is an empty batch rather than a fabricated id.
            if isinstance(payload, list) or not responses:
                out = responses
            else:
                out = responses[0]
            return JSONResponse(out, status_code=200)

        autofills = []
        normalizations = []   # tag rewrites, annotated on the response like autofill (N5)
        retain_ids = []     # ids whose RESPONSE the N5 pass may touch (retain family only)
        based_on_fill_ids = []    # reflect calls whose include_based_on the gateway filled (#55)
        # Retain-side gates (issue #12): one recall per eligible retain, BEFORE sanitize so the
        # gate judges the client's own content. Warnings ride the response; drafts are side
        # effects. Fail-open inside (a gate error never fails the retain).
        gate_outcomes = await retain_gates.evaluate_mcp(
            app=app, client=client, upstream_key=upstream_key, batch=batch, decisions=decisions,
            identity=(principal.identity if principal else None), bank=bank, token=token,
            audit=audit, session_id=_session_scope(request), verdicts=verdicts)
        warnings = [{"id": el_id, "text": result.warning} for el_id, result in gate_outcomes.items()
                    if result.warning]
        # Completion -> open-item invalidation (issue #321, flag HC_ITEM_INVALIDATE, default off):
        # only the pure preparation runs here; lookup and PATCH happen after confirmed storage.
        invalidate_plans = item_invalidate.prepare_mcp(
            batch=batch, decisions=decisions, verdicts=verdicts, bank=bank, principal=principal,
            bank_policy=bank_policy)
        # Recall ledger (issue #12, 2차): remember WHICH query each recall element asked so a
        # later draft can reuse the query that actually rescued the session.
        recall_calls = {
            reframe.element_id(el): ((el.get("params") or {}).get("arguments") or {})
            for el, d in zip(batch, decisions)
            if isinstance(el, dict) and el.get("method") == "tools/call"
            and d.tool == "recall" and reframe.element_id(el) is not None
            and isinstance((el.get("params") or {}).get("arguments"), dict)
        }
        # initialize is the ONE chance to tell a client the rules before it calls; the
        # response side fills in `instructions` derived from this bank's policy (2026-07-27).
        initialize_ids = [reframe.element_id(el) for el in batch
                          if isinstance(el, dict) and el.get("method") == "initialize"
                          and reframe.element_id(el) is not None]
        # tools/list is the only rules channel every client forwards to its model
        # (initialize instructions are dropped by some - measured 2026-07-27).
        tools_list_ids = [reframe.element_id(el) for el in batch
                          if isinstance(el, dict) and el.get("method") == "tools/list"
                          and reframe.element_id(el) is not None]
        # reflect demand telemetry (issue #11): remember WHICH tags each reflect element asked
        # for, so the response side can log tags + served model ids (metadata only, never text).
        reflect_calls = {
            reframe.element_id(el): reflect_demand.request_meta((el.get("params") or {}).get("arguments"))
            for el, d in zip(batch, decisions)
            if isinstance(el, dict) and el.get("method") == "tools/call"
            and d.tool == reflect_demand.TOOL and reframe.element_id(el) is not None
        }
        for el, d, v in zip(batch, decisions, verdicts):
            if (el or {}).get("method") == "tools/call":
                if d.tool in policy.RETAIN_TOOLS and reframe.element_id(el) is not None:
                    retain_ids.append(reframe.element_id(el))
                params = el.get("params") or {}
                sent_arguments = params.get("arguments")     # pre-rules, for the retain-tags line
                arguments = v.item if v.item is not None else params.get("arguments")
                # issue #55: same spot, same rule for reflect include_based_on. The filled ids are
                # the only responses whose based_on is stripped again on the way back, so it is only
                # filled when that strip can run: the call has an addressable id (a response we can
                # find again) and HC_UNESCAPE is on (the #26 lever must restore reflect's old
                # pass-through bytes completely, and a filled based_on must never leak past it).
                based_on_filled = False
                if reframe.element_id(el) is not None and unescape.enabled():
                    arguments, based_on_filled = reflect_based_on_default.fill_include_based_on(
                        d.tool, arguments)
                params["arguments"] = policy.sanitize_arguments(
                    arguments,
                    resolved_bank=bank,
                    principal=principal,
                    tool=d.tool,
                    attribution_enabled=attribution_enabled,
                )
                el["params"] = params
                if v.autofilled_timestamp:
                    audit.record(
                        token=token, identity=(principal.identity if principal else None),
                        method=el.get("method", ""), tool=d.tool, attempted_bank=bank,
                        resolved_bank=bank, decision="enforce-autofill",
                        reason="timestamp-autofill:" + v.autofilled_timestamp,
                    )
                    autofills.append({"id": el.get("id"), "tool": d.tool,
                                      "timestamp": v.autofilled_timestamp})
                if based_on_filled:
                    audit.record(
                        token=token, identity=(principal.identity if principal else None),
                        method=el.get("method", ""), tool=d.tool, attempted_bank=bank,
                        resolved_bank=bank, decision="enforce-autofill",
                        reason=reflect_based_on_default.AUDIT_REASON,
                    )
                    based_on_fill_ids.append(reframe.element_id(el))
                if v.normalized_tags:
                    norm_reason, norm_extra = normalize_audit(v.normalized_tags)
                    audit.record(
                        token=token, identity=(principal.identity if principal else None),
                        method=el.get("method", ""), tool=d.tool, attempted_bank=bank,
                        resolved_bank=bank, decision="enforce-normalize",
                        reason=norm_reason, extra=norm_extra,
                    )
                    normalizations.append({"id": el.get("id"), "tool": d.tool,
                                           "tags": v.normalized_tags})
                if v.warnings and d.tool in policy.RETAIN_TOOLS:
                    # Non-blocking tag hints (#40) ride the same success-response channel as the
                    # contradiction warning; the retain is forwarded and stored regardless.
                    warnings.extend({"id": el.get("id"), "text": text} for text in v.warnings)
                if d.tool in policy.RETAIN_TOOLS:
                    # Every allowed retain, corrected or not (issue #37): sent vs stored tags
                    # plus the initialize client name. `stored` is the rules verdict's item -
                    # sanitize never touches tags - so it is what is forwarded. No content.
                    sent_tags = sent_arguments.get("tags") if isinstance(sent_arguments, dict) else None
                    # The name below is the arrival value (issue #127); this call only keeps the LRU
                    # recency lookup() always gave a session that is still retaining (issue #37).
                    app.state.mcp_clients.lookup(_session_scope(request))
                    stored_tags = v.item.get("tags") if isinstance(v.item, dict) else None
                    audit.record(
                        token=token, identity=(principal.identity if principal else None),
                        method=el.get("method", ""), tool=d.tool, attempted_bank=bank,
                        resolved_bank=bank, decision="retain-tags",
                        reason="tags-rewritten" if v.normalized_tags else "tags-unchanged",
                        extra=_retain_tags_extra(
                            transport="mcp", sent=sent_tags, stored=stored_tags,
                            client=arrived_client or CLIENT_UNKNOWN),
                    )
        # Stashed for the response side (N5 annotates the success payload with
        # "[gateway: timestamp 자동 기록 <값>]" — P5 no-silent-correction). Storing only.
        request.state.rule_autofills = autofills

        out_payload = batch if isinstance(payload, list) else batch[0]
        body_bytes = json.dumps(out_payload).encode("utf-8")  # canonical re-serialization

        # Header allowlist (drops client X-Bank-Id etc.) + inject upstream key.
        # Verified contract (hindsight v0.8.3): MCP is mounted at /mcp and resolves the bank
        # from the X-Bank-Id HEADER. So the gateway pins the bank by SETTING X-Bank-Id to the
        # ACL-resolved bank (never trusting the client's X-Bank-Id, which the allowlist drops),
        # and forwards to upstream /mcp. Body arguments.bank is also stripped (defense-in-depth).
        fwd_headers = {
            k: v for k, v in request.headers.items()
            if k.lower() in policy.FORWARD_HEADER_ALLOWLIST
        }
        fwd_headers["content-type"] = "application/json"
        fwd_headers["x-bank-id"] = bank                 # authoritative bank pin
        if upstream_key:
            fwd_headers["authorization"] = f"Bearer {upstream_key}"

        up_req = client.build_request("POST", "/mcp", content=body_bytes, headers=fwd_headers)
        has_initialize, initialize_client = _initialize_client_name(batch)

        def _bind_client(sid: str, status: int) -> None:
            """Tie the initialize clientInfo.name to the session id upstream just chose."""
            if has_initialize and 200 <= status < 300:
                app.state.mcp_clients.bind(hashlib.sha256(sid.encode()).hexdigest(),
                                           initialize_client)

        up_resp = await client.send(up_req, stream=True)
        media = up_resp.headers.get("content-type", "application/json")
        needs_rules = bool(initialize_ids or tools_list_ids)
        bank_instructions = rules.instructions_for(bank_policy) if needs_rules else None
        # `id` survives pruning by contract, so the recall ledger below still reads ids off
        # these (possibly pruned) bytes.
        prune_ids = set(recall_calls) if recall_calls and recall_prune.enabled() else set()
        # issue #302: recall calls whose result rows get a kind label (HC_RECALL_KIND_LABEL, default off).
        label_ids = set(recall_calls) if recall_calls and recall_label.enabled() else set()
        # Korean \uXXXX escapes (issue #23): allowlisted read tools join the buffered path so
        # their escapes can be undone; recall/retain are buffered anyway, so they ride along.
        # reflect stays out of THIS set - buffering it would undo the streaming passthrough
        # below; it is unescaped per SSE event instead (issue #26, reflect_unescape_ids).
        unescape_ids = {
            reframe.element_id(el) for el, d in zip(batch, decisions)
            if isinstance(el, dict) and el.get("method") == "tools/call"
            and (d.tool in unescape.TOOLS or d.tool == "recall" or d.tool in policy.RETAIN_TOOLS)
            and reframe.element_id(el) is not None
        } if unescape.enabled() else set()
        reflect_unescape_ids = set(reflect_calls) if unescape.enabled() else set()
        # issue #55: reflect calls whose include_based_on WE filled - their based_on goes back out
        # of the response (the caller never asked for it). Filling already required HC_UNESCAPE on.
        based_on_strip_ids = set(based_on_fill_ids)

        def convert_event(content_type, event):
            """One SSE event (or whole non-SSE body) of a streamed reflect: strip the based_on we
            asked for, then the #26 unescape. Each step fails open on its own (its input bytes)."""
            out, strip_failure = reflect_based_on_default.strip_mcp_body(
                content_type, event, based_on_strip_ids)
            if not reflect_unescape_ids:
                return out, strip_failure
            try:
                out2, unescape_failure = unescape.unescape_mcp_body(
                    content_type, out, reflect_unescape_ids)
            except Exception as exc:  # noqa: BLE001 - keep the strip that already succeeded
                return out, strip_failure or "rewrite-exception:" + type(exc).__name__
            return out2, strip_failure or unescape_failure

        def record_reflect_demand(media_seen: str, body_seen: bytes) -> None:
            """Reflect demand telemetry (issue #11): read-only, fail-open, one line per call."""
            try:
                outcomes = reflect_demand.mcp_outcomes(reflect_calls, media_seen, body_seen)
            except Exception:  # noqa: BLE001 - never let telemetry fail the call
                outcomes = {el_id: {**meta, "ok": False} for el_id, meta in reflect_calls.items()}
            for event in outcomes.values():
                reflect_demand.record(
                    audit, token=token, identity=(principal.identity if principal else None),
                    bank=bank, method="tools/call", tool=reflect_demand.TOOL, transport="mcp",
                    event=event)

        # --- STREAMING PASSTHROUGH (2026-09-26) ---------------------------------------
        # When nothing below needs upstream's WHOLE body - no response rewrite (retain family,
        # initialize/tools-list instructions, recall pruning), no recall ledger, no retain-gate
        # effects - the bytes go out as they arrive. Claude Code aborts an MCP POST whose
        # response HEADERS miss a max(60s, MCP_TIMEOUT) first-byte budget, and a claude-code
        # reflect runs ~100s; upstream's 15s ': ping' SSE comments only help if they reach the
        # client. Reflect telemetry tees the bytes and records once the stream ends.
        # The issue #23 unescape is a response rewrite too, which is why reflect is not in it;
        # reflect's own unescape (issue #26) rewrites event by event and keeps streaming.
        if not (retain_ids or bank_instructions or prune_ids or recall_calls or gate_outcomes
                or unescape_ids):
            async def passthrough():
                seen = bytearray() if reflect_calls else None
                try:
                    async for chunk in up_resp.aiter_bytes():
                        if seen is not None:
                            seen.extend(chunk)
                        yield chunk
                finally:
                    await up_resp.aclose()
                    if seen is not None:
                        record_reflect_demand(media, bytes(seen))

            async def unescape_events():
                """Issue #26: same stream, reflect results unescaped per completed SSE event.
                Events without data (': ping') leave the moment they complete; each event
                fails open on its own (original bytes + counter + one audit line), and the
                telemetry tee still reads upstream's ORIGINAL bytes - which is why the based_on
                strip of issue #55 (filled calls only) cannot blind the hit/miss judgement."""
                seen = bytearray()
                rewriter = unescape.EventRewriter(
                    media, reflect_unescape_ids,
                    convert=convert_event if based_on_strip_ids else None)

                def emit(outs):
                    for out, failure in outs:
                        if failure:
                            _record_mcp_rewrite_failure(token=token, principal=principal,
                                                        bank=bank, failure=failure)
                        yield out

                try:
                    async for chunk in up_resp.aiter_bytes():
                        seen.extend(chunk)
                        for out in emit(rewriter.feed(chunk)):
                            yield out
                    for out in emit(rewriter.finish()):
                        yield out
                finally:
                    await up_resp.aclose()
                    record_reflect_demand(media, bytes(seen))

            body_iter = (unescape_events() if (reflect_unescape_ids or based_on_strip_ids)
                         else passthrough())
            streamed = StreamingResponse(body_iter, status_code=up_resp.status_code,
                                         media_type=media)
            sid = up_resp.headers.get("mcp-session-id")
            if sid:
                streamed.headers["mcp-session-id"] = sid
                _bind_client(sid, up_resp.status_code)
            return streamed

        # Buffered path: the rewrite/ledger/gate passes below read the complete (possibly
        # SSE-framed) response; SSE framing is preserved verbatim for the client to parse.
        try:
            await up_resp.aread()
        finally:
            await up_resp.aclose()
        body_out = up_resp.content
        upstream_media, upstream_body = media, body_out      # reflect telemetry reads these (issue #55)

        # --- recall status enrichment (issue #339, flag HC_ITEM_STATUS, default off) ------
        # Appends the same item key's completion-family memories to a successful MCP recall
        # result. Runs on upstream's whole body before the rewrite pass below, so appended rows
        # get the same prune/label/unescape. Fail-OPEN inside: error or timeout -> same bytes.
        if recall_calls and item_status.enabled() and 200 <= up_resp.status_code < 300:
            body_out = await item_status.enrich_mcp_body(
                media=media, body=body_out, recall_calls=recall_calls, client=client,
                upstream_key=upstream_key, bank=bank, bank_policy=bank_policy, audit=audit, token=token,
                identity=(principal.identity if principal else None))

        # --- S1 read-time item ledger (issue #363, flag HC_ITEM_LEDGER, default off) -------
        # Independent of HC_ITEM_STATUS; runs after it and never touches the rows X1 appended.
        # Annotates each result row that names an item key with that key's latest completion
        # evidence. Same fail-OPEN contract: error or timeout -> the bytes it was given.
        if recall_calls and item_ledger.enabled() and 200 <= up_resp.status_code < 300:
            body_out = await item_ledger.annotate_mcp_body(
                media=media, body=body_out, recall_calls=recall_calls, client=client,
                upstream_key=upstream_key, bank=bank, bank_policy=bank_policy, audit=audit, token=token,
                identity=(principal.identity if principal else None))

        # --- S3 open-key table (issue #377, flag HC_ITEM_OPEN_TABLE, default off) ---------
        # Independent of X1/S1. Reads the S4 side store (sqlite, read-only, no upstream call, no
        # LLM) and puts one table under the recall payload's top-level key gateway_item_open_table
        # on both JSON surfaces. Same fail-OPEN contract: any failure -> the bytes it was given.
        if recall_calls and item_open_table.enabled() and 200 <= up_resp.status_code < 300:
            body_out = await item_open_table.attach_mcp_body(
                media=media, body=body_out, recall_calls=recall_calls, bank=bank,
                bank_policy=bank_policy, audit=audit, token=token,
                identity=(principal.identity if principal else None))

        # --- RESPONSE SIDE (N5): honestify failures, annotate corrections ---------
        # Scoped to retain-family responses plus recall PRUNING (2026-09-22): tools/list and
        # every other tool body is still never parsed. The recall pass only deletes engine
        # internals from a payload upstream already produced — it cannot reorder, drop or
        # rewrite a result (recall_prune's invariants, pinned by tests/test_recall_prune.py),
        # so the "a rewriter bug corrupts an answer" risk that scoped this block stays bounded.
        # The issue #23 unescape (allowlisted read tools) is bounded the same way: it only
        # changes notation, and each call re-checks json.loads equality or keeps the original.
        # Everything here is fail-OPEN — a rewriting gateway must never be able to
        # withhold an answer upstream already produced. Any failure (parse, callback,
        # re-serialize) returns upstream's original bytes, raises the visible counter
        # and writes one audit alarm line.
        if (retain_ids or bank_instructions or prune_ids or label_ids or unescape_ids
                or reflect_unescape_ids or based_on_strip_ids):
            parsed = reframe.parse_response(media, body_out)
            rewritten = reframe.transform_results(
                parsed,
                lambda el: unescape.unescape_mcp_element(
                    recall_label.label_mcp_element(
                        recall_prune.prune_mcp_element(
                            honesty.describe_tools(
                                honesty.annotate_element(
                                    reflect_based_on_default.strip_mcp_element(el, based_on_strip_ids),
                                    retain_ids=retain_ids, autofills=autofills,
                                    normalizations=normalizations, warnings=warnings,
                                    initialize_ids=initialize_ids, instructions=bank_instructions),
                                tools_list_ids, bank_instructions, policy.RETAIN_TOOLS),
                            prune_ids, bank),
                        label_ids),
                    unescape_ids | reflect_unescape_ids),
            )
            new_media, new_body, serialize_failure = reframe.try_serialize(rewritten)
            failure = rewritten.failure or serialize_failure
            if failure:
                _record_mcp_rewrite_failure(token=token, principal=principal, bank=bank,
                                            failure=failure)
                # The composed pass is all-or-nothing, so one failing element puts the whole
                # original back - including the based_on WE asked for. Strip it once more on its
                # own (issue #55); if that fails too the original goes out with its own reason.
                if based_on_strip_ids:
                    stripped, strip_failure = reflect_based_on_default.strip_mcp_body(
                        media, body_out, based_on_strip_ids)
                    if strip_failure:
                        _record_mcp_rewrite_failure(token=token, principal=principal, bank=bank,
                                                    failure=strip_failure)
                    else:
                        body_out = stripped
            else:
                media, body_out = new_media, new_body

        # --- reflect demand telemetry (issue #11) for a reflect batched with buffered work
        # Reads upstream's ORIGINAL bytes, not the rewritten ones: a filled call's based_on was
        # stripped from body_out above, and the telemetry needs it to tell hit from miss (#55).
        if reflect_calls:
            record_reflect_demand(upstream_media, upstream_body)

        # --- recall ledger (issue #12): query + result ids, read-only on the response bytes
        if recall_calls:
            try:
                ids_by_el = (_recall_ids_from_mcp(media, body_out, set(recall_calls))
                             if 200 <= up_resp.status_code < 300 else {})
            except Exception:  # noqa: BLE001 - ledger is best-effort
                ids_by_el = {}
            for el_id, args in recall_calls.items():
                if el_id not in ids_by_el:
                    continue              # failed/unknown recall is not retrieval evidence
                app.state.recall_ledger.record(
                    identity=(principal.identity if principal else None), bank=bank,
                    transport="mcp", query=args.get("query"), tags=args.get("tags"),
                    result_ids=ids_by_el[el_id], session_id=_session_scope(request),
                    tags_match=args.get("tags_match") or "any", reusable=_query_reusable(args),
                    budget=args.get("budget"), max_tokens=args.get("max_tokens"),
                    client=arrived_client, arrived_ts=arrived, ts=clock())

        successes = gate_effects.mcp_successes(media, body_out, set(gate_outcomes) | set(invalidate_plans),
                                               up_resp.status_code)
        plans = [(gate_outcomes[el_id], op) for el_id, op in successes.items() if el_id in gate_outcomes]
        invalidations = [(invalidate_plans[el_id], op) for el_id, op in successes.items()
                         if el_id in invalidate_plans]
        background = BackgroundTask(gate_effects.finalize, app=app, client=client,
            upstream_key=upstream_key, plans=plans, identity=principal.identity, bank=bank,
            token=token, audit=audit, transport="mcp",
            invalidations=invalidations) if plans or invalidations else None
        resp = Response(content=body_out, status_code=up_resp.status_code, media_type=media,
                        background=background)
        sid = up_resp.headers.get("mcp-session-id")
        if sid:
            resp.headers["mcp-session-id"] = sid
            _bind_client(sid, up_resp.status_code)
        return resp

    # ---- REST surface proxy (/v1/{tenant}/banks/{bank}/...) ----
    # The Hindsight REST API carries the bank in the PATH. The gateway enforces the same
    # deny-by-default bank ACL, blocks the bank-collection (enumeration) endpoint, judges
    # METHOD + path as the MCP tool it maps to (policy.decide_rest, issue #458: unknown
    # operations denied), and injects team-retain attribution.
    @app.api_route("/v1/{tenant}/banks", methods=["GET", "POST"])
    async def rest_collection_blocked(tenant: str, request: Request):
        token = _bearer(request)
        p = acl.principal_for(token)
        audit.record(token=token, identity=(p.identity if p else None), method=request.method,
                     tool="banks-collection", attempted_bank=None, resolved_bank=None,
                     decision="deny", reason="bank-collection-blocked")
        return JSONResponse({"error": "bank collection endpoint disabled"}, status_code=403)

    def _upstream_bank_missing(resp, bank: str) -> bool:
        """upstream's _require_bank_exists answer: 404 {"detail": "Bank '<bank>' not found"} (exact)."""
        try:
            body = json.loads(resp.content)
        except (ValueError, RecursionError):
            return False
        return isinstance(body, dict) and body.get("detail") == f"Bank '{bank}' not found"

    async def _rest_write_restriction(tenant: str, bank: str, tool: str) -> Optional[str]:
        """None = upstream's bank config lets `tool` write; else the deny reason (policy prefixes)."""
        headers = {"accept": "application/json", BANK_CONFIG_LOOKUP_HEADER[0]: BANK_CONFIG_LOOKUP_HEADER[1]}
        if upstream_key:
            headers["authorization"] = f"Bearer {upstream_key}"
        try:
            resp = await client.get(f"/v1/{tenant}/banks/{bank}/config", headers=headers,
                                    timeout=BANK_CONFIG_LOOKUP_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001 - timeout, connection, anything: fail-closed
            return policy.BANK_CONFIG_LOOKUP_FAILED_PREFIX + type(exc).__name__
        if resp.status_code != 200:
            if resp.status_code == 404 and _upstream_bank_missing(resp, bank):
                # Still denied (fail-closed): a bank upstream has not created has no config to judge, and
                # upstream's own MCP filter would still apply the global/tenant tool allowlist to it. The
                # reason names the cause so an operator provisions the bank instead of chasing an outage.
                return policy.BANK_CONFIG_LOOKUP_FAILED_PREFIX + "bank-not-provisioned"
            return policy.BANK_CONFIG_LOOKUP_FAILED_PREFIX + f"http-{resp.status_code}"
        try:
            config_body = json.loads(resp.content)
        except (ValueError, RecursionError):
            return policy.BANK_CONFIG_LOOKUP_FAILED_PREFIX + "not-json"
        return policy.write_restriction_reason(config_body, tool)

    @app.api_route("/v1/{tenant}/banks/{bank}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    @app.api_route("/v1/{tenant}/banks/{bank}/{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def rest_proxy(tenant: str, bank: str, request: Request, rest: str = ""):
        arrived = clock()        # recall ledger `arrived_ts` (issue #89): before any work on it
        token = _bearer(request)
        principal = acl.principal_for(token)
        # Canonical path (issue #49): the ONE path every decision below, the upstream request
        # and the audit `tool` value use. Judging `memories` but forwarding `memories/` would
        # let a trailing slash dodge rules the day upstream stops 307-ing it.
        normalized_rest = rest.strip("/")
        rpath = f"/v1/{tenant}/banks/{bank}" + (f"/{normalized_rest}" if normalized_rest else "")

        def deny(reason: str, code: int = 403):
            audit.record(token=token, identity=(principal.identity if principal else None),
                         method=request.method, tool=rpath, attempted_bank=bank,
                         resolved_bank=None, decision="deny", reason=reason)
            return JSONResponse({"error": "forbidden", "reason": reason}, status_code=code)

        if principal is None:
            return deny("unknown-or-missing-token")
        # Non-canonical paths are rejected, never normalized (issue #49 final-review): httpx folds
        # `.`/`..` segments and drops `#...` when it joins rpath onto base_url, so the path judged
        # here would differ from the one upstream receives (ACL/rules judged on one bank or route,
        # another one written). Folding `..` ourselves would change the judged bank, so refuse.
        if (tenant in (".", "..")
                or any(seg in ("", ".", "..") for seg in (normalized_rest.split("/") if normalized_rest else ()))
                or any(ch in normalized_rest or ch in tenant for ch in "#?\\")):
            return deny("non-canonical-path", code=400)
        if not _BANK_RE.match(bank or ""):
            return deny("bad-bank-format")
        # Operation verdict (issue #458): METHOD + canonical path resolve to the MCP tool whose
        # verdict they get, judged by the same policy function and tool sets as /mcp/<bank>/
        # (owner-only destructive/corrective, blocked operations, KB read flag). A path or method
        # outside policy.REST_OPERATIONS is denied - admin POSTs and knowledge base writes (#305)
        # never reach upstream.
        bank_policy = app.state.bank_policies.for_bank(bank)
        d = policy.decide_rest(principal, bank, request.method, normalized_rest,
                               bank_policy=bank_policy, kb_read_enabled=policy.kb_read_enabled())
        if not d.allow:
            return deny(d.reason)
        # Upstream write restriction (issue #473 b): upstream applies the bank config
        # `mcp_enabled_tools` to MCP tool calls only, so a REST write is judged here by the same datum,
        # read from upstream on every request (no cache). The read is an authorization step: it runs
        # before the rules and nothing is forwarded when it denies or fails (fail-closed).
        if d.tool in policy.RETAIN_TOOLS:
            reason = await _rest_write_restriction(tenant, bank, d.tool)
            if reason:
                return deny(reason, code=(503 if reason.startswith(policy.BANK_CONFIG_LOOKUP_FAILED_PREFIX)
                                          else 403))

        body = await request.body()
        # Read-side guard (issue #458 c): the recall token-budget floor the MCP surface applies
        # (rules.evaluate_tool_call -> the same check) also applies here, same rule id, rejected
        # whole and never forwarded. Non-object bodies pass: upstream's 422 owns their shape.
        # It runs BEFORE the `allow` audit line (final-review C1): a rejected REST recall leaves only
        # its enforce-reject line, so scripts/duplicate_recall.py (which keeps REST recall `allow`
        # lines and drops REST rejects) never counts it as a recall that arrived.
        if request.method == "POST" and normalized_rest == "memories/recall" and body:
            try:
                recall_args = json.loads(body)
            except (ValueError, RecursionError):
                # JSONDecodeError/UnicodeDecodeError, an int past max_str_digits, or nesting past the
                # recursion limit: not a shape this guard judges, so it is forwarded as before (no 500).
                recall_args = None
            floor_violations = rules.check_recall_budget(recall_args, bank_policy)
            if floor_violations:
                rule_ids = [v.rule for v in floor_violations]
                audit.record(token=token, identity=principal.identity, method=request.method,
                             tool=rpath, attempted_bank=bank, resolved_bank=bank,
                             decision="enforce-reject", reason="rule-violation:" + ",".join(rule_ids))
                return JSONResponse(
                    {"error": "rule-violation", "rule": rule_ids[0],
                     "rule_detail": [{"rules": rule_ids, "message": floor_violations[0].message}]},
                    status_code=422,
                )
        audit.record(token=token, identity=principal.identity, method=request.method, tool=rpath,
                     attempted_bank=bank, resolved_bank=bank, decision="allow", reason="rest-allow")

        rest_normalizations = []    # tag rewrites disclosed on the success body (N2b)
        rest_warnings = []          # contradiction-gate warnings disclosed the same way (#12)
        rest_tag_warnings = []      # tag hints (#40): kept apart, they do not depend on storage status
        rest_outcomes = {}
        rest_invalidations = {}     # issue #321 (flag HC_ITEM_INVALIDATE): prepared completions, run after storage

        # --- RULES (N3) — same evaluate_item() the MCP surface uses, once per items[i] ---
        # Order contract, identical to /mcp/<bank>/: ACL (above) → rules → sanitize (below).
        # Rules judge the CLIENT's own items, never values the gateway itself wrote.
        # Shape (v0.8.3 실측, fixtures/mcp/NOTES.md ②): {items:[MemoryItem], async, ...} with
        # tags/timestamp at the ITEM top level — hence the per-element loop instead of MCP's
        # single flat-arguments call. A non-dict element is passed through unjudged: the
        # gateway only enforces rules on shapes it recognizes, upstream's 422 owns the rest.
        if request.method == "POST" and normalized_rest == "memories" and body:
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = None
            items = payload.get("items") if isinstance(payload, dict) else None
            if isinstance(items, list):
                bank_policy = app.state.bank_policies.for_bank(bank)
                now_iso = rules.utc_now_iso()
                verdicts = [
                    rules.evaluate_item(it, bank_policy, now_iso) if isinstance(it, dict)
                    else rules.ItemVerdict(item=it)
                    for it in items
                ]
                offenders = [(i, v) for i, v in enumerate(verdicts) if not v.ok]
                if offenders:
                    # All-or-nothing: one bad item kills the request. A partial write would
                    # leave the caller unable to tell WHICH items were stored.
                    for idx, v in offenders:
                        audit.record(token=token, identity=principal.identity,
                                     method=request.method, tool=rpath, attempted_bank=bank,
                                     resolved_bank=bank, decision="enforce-reject",
                                     reason="rule-violation:" + ",".join(v.rule_ids))
                    return JSONResponse(
                        {
                            "error": "rule-violation",
                            "rule": offenders[0][1].rule_ids[0],
                            "rule_detail": [
                                {"index": idx, "rules": list(v.rule_ids), "message": v.message}
                                for idx, v in offenders
                            ],
                        },
                        status_code=422,
                    )
                payload["items"] = [v.item for v in verdicts]
                body = json.dumps(payload).encode("utf-8")   # canonical re-serialization
                rest_outcomes = await retain_gates.evaluate_rest(
                    app=app, client=client, upstream_key=upstream_key, items=payload["items"],
                    identity=principal.identity, bank=bank, token=token, audit=audit, tool=rpath,
                    session_id=_session_scope(request))
                rest_invalidations = item_invalidate.prepare_rest(
                    items=payload["items"], bank=bank, principal=principal, bank_policy=bank_policy)
                rest_warnings = [{"index": idx, "text": outcome.warning}
                                 for idx, outcome in rest_outcomes.items() if outcome.warning]
                for idx, v in enumerate(verdicts):
                    rest_tag_warnings.extend({"index": idx, "text": text} for text in v.warnings)
                    if isinstance(items[idx], dict):
                        # One line per allowed items[i] (issue #37); `items` is still the
                        # client's own list (only payload["items"] was replaced), so
                        # items[idx] carries the tags as SENT.
                        audit.record(token=token, identity=principal.identity,
                                     method=request.method, tool=rpath, attempted_bank=bank,
                                     resolved_bank=bank, decision="retain-tags",
                                     reason="tags-rewritten" if v.normalized_tags else "tags-unchanged",
                                     extra=_retain_tags_extra(
                                         transport="rest", sent=items[idx].get("tags"),
                                         stored=v.item.get("tags")))
                    if v.autofilled_timestamp:
                        audit.record(token=token, identity=principal.identity,
                                     method=request.method, tool=rpath, attempted_bank=bank,
                                     resolved_bank=bank, decision="enforce-autofill",
                                     reason="timestamp-autofill:" + v.autofilled_timestamp)
                    if v.normalized_tags:
                        # Same disclosure contract as MCP: a correction the caller cannot
                        # see is a silent correction, whichever surface made it (P1/P5).
                        norm_reason, norm_extra = normalize_audit(v.normalized_tags)
                        audit.record(token=token, identity=principal.identity,
                                     method=request.method, tool=rpath, attempted_bank=bank,
                                     resolved_bank=bank, decision="enforce-normalize",
                                     reason=norm_reason, extra=norm_extra)
                        rest_normalizations.append({
                            "index": idx,
                            "rewrites": [{"sent": before, "stored": after}
                                         for before, after in v.normalized_tags],
                        })

        # team-retain attribution: stamp member identity into each item's metadata (overwrite)
        if (request.method == "POST" and normalized_rest == "memories" and attribution_enabled
                and principal.is_team_bank(bank) and body):
            try:
                payload = json.loads(body)
                items = payload.get("items")
                if isinstance(items, list):
                    for it in items:
                        if isinstance(it, dict):
                            md = dict(it.get("metadata") or {})
                            md[policy.ATTRIBUTION_KEY] = principal.identity
                            it["metadata"] = md
                    body = json.dumps(payload).encode("utf-8")
            except json.JSONDecodeError:
                pass

        fwd = {k: v for k, v in request.headers.items() if k.lower() in policy.FORWARD_HEADER_ALLOWLIST}
        if upstream_key:
            fwd["authorization"] = f"Bearer {upstream_key}"
        # multi_items(): dict() would keep only the LAST value of a repeated key
        # (?tags=a&tags=b → tags=b), silently widening upstream list filters.
        up = client.build_request(request.method, rpath, content=(body or None),
                                  headers=fwd, params=list(request.query_params.multi_items()))
        up_resp = await client.send(up)
        # recall ledger (issue #12, 2차) - REST shape of the same event as the MCP surface.
        if request.method == "POST" and normalized_rest == "memories/recall" and up_resp.status_code == 200:
            try:
                req_body = json.loads(body) if body else {}
                recall_body = json.loads(up_resp.content)
                res_ids = recurrence.recall_result_ids(recall_body)
                if not isinstance(recall_body, dict) or not isinstance(recall_body.get("results"), list):
                    req_body = {}
            except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
                req_body, res_ids = {}, []
            if isinstance(req_body, dict):
                app.state.recall_ledger.record(
                    identity=principal.identity, bank=bank, transport="rest",
                    query=req_body.get("query"), tags=req_body.get("tags"), result_ids=res_ids,
                    session_id=_session_scope(request), tags_match=req_body.get("tags_match") or "any",
                    reusable=_query_reusable(req_body),
                    budget=req_body.get("budget"), max_tokens=req_body.get("max_tokens"),
                    arrived_ts=arrived, ts=clock())      # client stays null: REST has no MCP client (#89)
        # reflect demand telemetry (issue #11) - same event as the MCP surface, REST shape.
        if request.method == "POST" and normalized_rest == reflect_demand.TOOL:
            try:
                req_meta = reflect_demand.request_meta(json.loads(body) if body else None)
            except (json.JSONDecodeError, UnicodeDecodeError):
                req_meta = reflect_demand.request_meta(None)
            reflect_demand.record(
                audit, token=token, identity=principal.identity, bank=bank,
                method=request.method, tool=rpath, transport="rest",
                event=reflect_demand.rest_outcome(req_meta, up_resp.status_code, up_resp.content))
        # Disclosure is fail-OPEN exactly like the MCP annotation: if upstream's body is not
        # a JSON object we pass its bytes through untouched rather than withhold an answer.
        background = None
        confirmed, operation_id = False, None
        if (rest_outcomes or rest_invalidations) and 200 <= up_resp.status_code < 300:
            try:
                confirmed, operation_id = gate_effects.storage_status(up_resp.json())
            except ValueError:
                pass
            if confirmed or operation_id:
                background = BackgroundTask(gate_effects.finalize, app=app, client=client,
                    upstream_key=upstream_key,
                    plans=[(outcome, operation_id) for outcome in rest_outcomes.values()],
                    identity=principal.identity, bank=bank, token=token, audit=audit, transport="rest",
                    invalidations=[(plan, operation_id) for plan in rest_invalidations.values()])
            else:
                rest_warnings = []
        if (rest_normalizations or rest_warnings or rest_tag_warnings) and up_resp.status_code < 400:
            try:
                disclosed = json.loads(up_resp.content)
            except (json.JSONDecodeError, UnicodeDecodeError):
                disclosed = None
            if isinstance(disclosed, dict):
                if _failure_body(disclosed):
                    # Tag hints say "stored anyway" (issue #107): untrue over a failure body,
                    # which MCP answers the same way (no annotation on an isError result).
                    rest_tag_warnings = []
                if rest_normalizations or rest_tag_warnings or rest_warnings:
                    if rest_normalizations:
                        disclosed["gateway_normalized_tags"] = rest_normalizations
                    if rest_tag_warnings or rest_warnings:
                        disclosed["gateway_warnings"] = rest_tag_warnings + rest_warnings
                    return JSONResponse(disclosed, status_code=up_resp.status_code,
                                        background=background)
            else:
                # Undisclosable rewrite: still fail-open, but never silent - same visible
                # counter + audit alarm the MCP rewrite path raises.
                app.state.rewrite_failures += 1
                audit.record(token=token, identity=principal.identity, method=request.method,
                             tool=rpath, attempted_bank=bank, resolved_bank=bank,
                             decision="rewrite-failure",
                             reason="rest-normalize-undisclosed:non-dict-upstream-body")
        # recall pruning (2026-09-22), REST shape of the MCP pass. The ledger above already
        # read its ids off the untouched bytes; pruning is the last thing before the wire and
        # returns the original bytes on any failure.
        out_content = up_resp.content
        if (request.method == "POST" and normalized_rest == "memories/recall"
                and 200 <= up_resp.status_code < 300 and recall_prune.enabled()):
            out_content = recall_prune.prune_rest_body(out_content, bank)
        # issue #302: kind label after the prune, still the last thing before the wire (fail-open to the bytes it got).
        if (request.method == "POST" and normalized_rest == "memories/recall"
                and 200 <= up_resp.status_code < 300 and recall_label.enabled()):
            out_content = recall_label.label_rest_body(out_content)
        return Response(content=out_content, status_code=up_resp.status_code,
                        media_type=up_resp.headers.get("content-type", "application/json"),
                        background=background)
    return app
