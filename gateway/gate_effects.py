"""Commit prepared retain effects after success, outside the response latency.

An async retain's acceptance is not storage. Poll its operation to a successful terminal
state before committing candidates, dedup or correction telemetry. These best-effort tasks
are bounded to five minutes and not durable across gateway restarts; failures are audited.
"""
from __future__ import annotations

import asyncio
import time
from urllib.parse import quote

from . import honesty, item_invalidate, recurrence, reframe
from .retain_gates import _audit

OPERATION_WAIT_SECONDS = 300.0
OPERATION_POLL_SECONDS = 1.0
SUCCESS = frozenset({"completed", "complete", "done", "succeeded", "success"})
FAILURE = frozenset({"failed", "error", "cancelled", "canceled"})
# Terminal states that are not success: an unknown operation never becomes completed, so
# polling it for five minutes only holds a task (review P3, 2026-09-23).
GONE = frozenset({"not_found", "not-found", "unknown", "missing"})
# Global bound on concurrent effect tasks. Each may poll for OPERATION_WAIT_SECONDS; without
# a cap a burst of eligible retains piles up pollers. Over the cap, effects are skipped and
# audited (they are best-effort by contract), never queued.
MAX_ACTIVE_FINALIZE = 32
_active = 0


def storage_status(payload):
    """(confirmed, operation id(s) or None). Unknown/error shapes never commit effects."""
    if not isinstance(payload, dict) or payload.get("error") or payload.get("success") is False:
        return False, None
    status = str(payload.get("status", "")).lower()
    if status in FAILURE or payload.get("memory_ids") == [] or payload.get("items_count") == 0:
        return False, None
    # REST submits one operation per strategy group. The singular id is only the first
    # group for backward compatibility, so it cannot prove storage of the whole request.
    ops = payload.get("operation_ids")
    if ops is not None:
        if not isinstance(ops, list) or not ops or not all(isinstance(op, str) and op for op in ops):
            return False, None
        return False, tuple(dict.fromkeys(ops))
    op = payload.get("operation_id")
    if isinstance(op, str) and op:
        return False, op
    if payload.get("async") is True:
        return False, None          # async without an operation id cannot be verified
    return bool(payload.get("memory_ids") or status in SUCCESS or payload.get("success") is True), None


def mcp_successes(media, body, wanted_ids, status_code):
    if not wanted_ids or not 200 <= status_code < 300:
        return {}               # nothing prepared: never parse (this runs on every MCP call)
    parsed = reframe.parse_response(media, body)
    if parsed.failure:
        return {}
    out = {}
    for frame in parsed.frames:
        for el in frame.payload if isinstance(frame.payload, list) else [frame.payload]:
            el_id = reframe.element_id(el)
            if el_id is None or el_id not in wanted_ids or "error" in el:
                continue
            result = el.get("result")
            if not isinstance(result, dict) or result.get("isError") or honesty.looks_like_failure(result):
                continue
            confirmed, op = storage_status(recurrence.mcp_recall_payload(result))
            if confirmed or op:
                out[el_id] = op
    return out


def _completed_cleanly(data) -> bool:
    """A completed row proves storage only with well-formed metadata; any other shape is
    decided now (False) instead of raising inside the poll loop and polling to the deadline."""
    meta = data.get("result_metadata")
    if meta is None:
        meta = {}
    children = data.get("child_operations") or []
    if not isinstance(meta, dict) or not isinstance(children, list):
        return False
    if any(not isinstance(c, dict) or str(c.get("status", "")).lower() in FAILURE for c in children):
        return False
    return not (meta.get("extraction_errors_count", 0) or meta.get("unit_ids_count") == 0)


async def wait_for_storage(client, upstream_key, bank, operation_id, *, deadline=None):
    if deadline is None:
        deadline = time.monotonic() + OPERATION_WAIT_SECONDS
    headers = {"authorization": f"Bearer {upstream_key}"} if upstream_key else {}
    while time.monotonic() < deadline:
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            async with asyncio.timeout(remaining):
                resp = await client.get(
                    f"/v1/default/banks/{quote(bank, safe='')}/operations/{quote(operation_id, safe='')}",
                    headers=headers, timeout=min(5.0, remaining))
            if resp.status_code == 404:
                return False
            data = resp.json() if resp.status_code == 200 else {}
            if isinstance(data, dict):
                status = str(data.get("status", "")).lower()
                # error_message alone is not failure: upstream's _schedule_retry leaves the
                # last retry's error on the row and does not clear it when the retry completes.
                if status in FAILURE or status in GONE:
                    return False
                if status in SUCCESS:
                    return _completed_cleanly(data)
        except Exception:  # transient read failures do not change the caller's retain result
            pass
        remaining = deadline - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(min(OPERATION_POLL_SECONDS, remaining))
    return False


async def finalize(*, app, client, upstream_key, plans, identity, bank, token, audit, transport,
                   invalidations=()):
    """Response task with one shared operation-wait budget for all prepared outcomes.

    `invalidations` (issue #321, flag HC_ITEM_INVALIDATE) are (item_invalidate.Plan, operation)
    pairs; empty when the flag is off, and then nothing below differs from before."""
    global _active
    common = dict(audit=audit, token=token, identity=identity, bank=bank, tool="retain")
    if _active >= MAX_ACTIVE_FINALIZE:
        if plans:
            _audit(**common, decision=recurrence.DECISION, reason="skip:effects-backlog",
                   extra={"recurrence": {"transport": transport, "active": _active}})
        for inv, inv_op in invalidations:
            _audit(**common, decision=item_invalidate.DECISION, reason="skip:effects-backlog",
                   extra=item_invalidate.skip_extra(inv, inv_op, active=_active))
        return
    _active += 1
    try:
        operations, deadline = await _finalize(
            app=app, client=client, upstream_key=upstream_key, plans=plans,
            common=common, identity=identity, bank=bank, transport=transport)
        if invalidations:
            await _invalidate(client=client, upstream_key=upstream_key, invalidations=invalidations,
                              operations=operations, deadline=deadline, common=common,
                              identity=identity, bank=bank)
    finally:
        _active -= 1


async def _invalidate(*, client, upstream_key, invalidations, operations, deadline, common, identity, bank):
    """Issue #321: after the same storage confirmation the effects use, retire this completion's
    open raw memories. Fail-open: every failure is one audit line, never a changed retain."""
    for plan, op in invalidations:
        try:
            operation_ids = (op,) if isinstance(op, str) else (op or ())
            stored = True
            for operation_id in operation_ids:
                if operation_id not in operations:
                    operations[operation_id] = await wait_for_storage(
                        client, upstream_key, bank, operation_id, deadline=deadline)
                if not operations[operation_id]:
                    stored = False
                    break
            if not stored:
                _audit(**common, decision=item_invalidate.DECISION, reason="skip:retain-not-completed",
                       extra=item_invalidate.skip_extra(plan, op))
                continue
            await item_invalidate.execute(client=client, upstream_key=upstream_key, bank=bank, plan=plan,
                                          operation=op, audit=common["audit"], token=common["token"],
                                          identity=identity)
        except Exception as exc:
            _audit(**common, decision=item_invalidate.DECISION, reason=f"failed:{type(exc).__name__}",
                   extra=item_invalidate.skip_extra(plan, op))


async def _finalize(*, app, client, upstream_key, plans, common, identity, bank, transport):
    operations = {}
    deadline = time.monotonic() + OPERATION_WAIT_SECONDS
    for outcome, op in plans:
        try:
            operation_ids = (op,) if isinstance(op, str) else (op or ())
            stored = True
            for operation_id in operation_ids:
                if operation_id not in operations:
                    operations[operation_id] = await wait_for_storage(
                        client, upstream_key, bank, operation_id, deadline=deadline)
                if not operations[operation_id]:
                    stored = False
                    break
            if not stored:
                _audit(**common, decision=recurrence.DECISION, reason="skip:retain-not-completed",
                       extra={"recurrence": {"transport": transport}})
                continue
            for decision, reason, extra in outcome.events:
                _audit(**common, decision=decision, reason=reason, extra=extra)
            if outcome.correction:
                _audit(**common, decision="injection-correction", reason="related-served-memory",
                       extra={"injection_correction": outcome.correction})
            for draft in outcome.drafts:
                ev = draft["evidence"]
                key = f"{identity}|{bank}|{draft['signal']}|{ev['similar_memory_id']}|{draft['state']}"
                if not app.state.recurrence_dedup.first_time(key):
                    _audit(**common, decision=recurrence.DECISION, reason="skip:dedup",
                           extra={"recurrence": {"transport": transport, "signal": draft["signal"]}})
                    continue
                if not app.state.pending_triggers.append(draft):
                    app.state.recurrence_dedup.forget(key)
                    _audit(**common, decision=recurrence.DECISION, reason="skip:pending-write-failed",
                           extra={"recurrence": {"transport": transport}})
                    continue
                notify = "skipped:no-notifier"
                if app.state.notifier is not None:
                    try:
                        notify = await app.state.notifier(draft)
                    except Exception as exc:
                        notify = f"failed:{type(exc).__name__}"
                _audit(**common, decision=recurrence.DECISION, reason=f"draft:{draft['signal']}",
                       extra={"recurrence": {"transport": transport, "signal": draft["signal"],
                           "draft_id": draft["draft_id"], "state": draft["state"],
                           "similar_memory_id": ev["similar_memory_id"], "similar_score": ev["similar_score"],
                           "rescue_query_source": ev["rescue_query_source"],
                           "rescue_result_hit": ev["rescue_result_hit"], "pending_written": True,
                           "notify": notify}})
        except Exception as exc:
            _audit(**common, decision=recurrence.DECISION, reason=f"effects-failed:{type(exc).__name__}",
                   extra={"recurrence": {"transport": transport}})
    return operations, deadline
