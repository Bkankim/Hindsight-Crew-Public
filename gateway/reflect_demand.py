"""Reflect demand telemetry (issue #11) - metadata-only events for the weekly model audit.

Every allowed reflect call gets ONE extra audit line (decision `reflect-demand`) that says
which tags were asked and whether a mental model served the answer. It never records the
query, the context, the answer text, or memory text - only tags, model ids, budget, the
isError flag and upstream's token usage counters. model_audit.py aggregates these lines
into the demand signal (tags reflect asks for that no live model covers).

`based_on` semantics (the field upstream only returns when the caller asked for it):
  * "hit"     - based_on present and >= 1 mental model in it
  * "miss"    - based_on present, 0 mental models (raw memories only)
  * "unknown" - based_on omitted / response unparseable. NOT a miss: without the block
                nobody can tell whether a model served the call, so it must be counted
                separately, never as 0 hits.

Everything here is fail-open and read-only: the response bytes are never touched, and a
telemetry failure degrades to an "unknown" event rather than an exception.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from . import reframe
from .audit import cut_field, field_keys, tag_list, tag_list_keys

DECISION = "reflect-demand"
TOOL = "reflect"
_MODEL_KEYS = ("mental-models", "mental_models")


def request_meta(arguments: Any) -> Dict[str, Any]:
    """tags / tags_match / budget of a reflect call (MCP `arguments` or REST body). No text.
    `tags` is only what the audit line keeps (first TAG_LOG_COUNT_MAX strings within the byte
    bounds of audit.tag_list - issues #103/#117); `tags_count` is the number of tag strings
    received, `tags_truncated` how many kept tags were cut. `tags_match`/`budget` are cut to
    audit.FIELD_LOG_MAX characters and `<field>_truncated` says so. The request that goes
    upstream is never touched."""
    args = arguments if isinstance(arguments, dict) else {}
    tags, tags_count, tags_truncated = tag_list(args.get("tags"))
    meta: Dict[str, Any] = {"tags": tags, "tags_count": tags_count, "tags_truncated": tags_truncated}
    for field in ("tags_match", "budget"):
        value = args.get(field)
        text, truncated = cut_field(value) if isinstance(value, str) else (None, False)
        meta[field] = text
        meta[f"{field}_truncated"] = truncated
    return meta


def outcome_from_reflect(payload: Any) -> Dict[str, Any]:
    """based_on state + served model ids + token usage from a parsed reflect response."""
    out: Dict[str, Any] = {"based_on": "unknown", "mental_models": [], "input_tokens": None}
    if not isinstance(payload, dict):
        return out
    usage = payload.get("usage")
    if isinstance(usage, dict) and isinstance(usage.get("input_tokens"), int):
        out["input_tokens"] = usage["input_tokens"]
    based_on = payload.get("based_on")
    if not isinstance(based_on, dict):
        return out
    models: List[str] = []
    for key in _MODEL_KEYS:
        entries = based_on.get(key)
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                    models.append(entry["id"])
                elif isinstance(entry, str):
                    models.append(entry)
    out["mental_models"] = sorted(set(models))
    out["based_on"] = "hit" if models else "miss"
    return out


def _reflect_payload_from_mcp_result(result: Any) -> Any:
    """Unwrap a CallToolResult: structuredContent.result first, then the JSON text block."""
    if not isinstance(result, dict):
        return None
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        inner = structured.get("result", structured)
        if isinstance(inner, str):
            try:
                inner = json.loads(inner)
            except ValueError:
                inner = None
        if isinstance(inner, dict):
            return inner
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            try:
                parsed = json.loads(block["text"])
            except ValueError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


def mcp_outcomes(calls: Dict[Any, Dict[str, Any]], media: Optional[str],
                 body: Optional[bytes]) -> Dict[Any, Dict[str, Any]]:
    """Per reflect element id: request meta + outcome, read from upstream's MCP response."""
    outcomes = {el_id: {**meta, **outcome_from_reflect(None), "ok": False}
                for el_id, meta in calls.items()}
    parsed = reframe.parse_response(media, body)
    if parsed.failure is not None:
        return outcomes
    for frame in parsed.frames:
        elements = frame.payload if isinstance(frame.payload, list) else [frame.payload]
        for el in elements:
            el_id = reframe.element_id(el)
            if el_id is None or el_id not in outcomes:
                continue
            result = el.get("result")
            record = outcomes[el_id]
            record["ok"] = isinstance(result, dict) and not result.get("isError") \
                and "error" not in el
            record.update(outcome_from_reflect(_reflect_payload_from_mcp_result(result)))
    return outcomes


def rest_outcome(meta: Dict[str, Any], status_code: int, body: Optional[bytes]) -> Dict[str, Any]:
    record = {**meta, **outcome_from_reflect(None), "ok": 200 <= status_code < 300}
    try:
        payload = json.loads((body or b"").decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return record
    record.update(outcome_from_reflect(payload))
    return record


def _marker_keys(event: Dict[str, Any]) -> Dict[str, Any]:
    """`tags_count`/`tags_omitted`/`tags_truncated` and `tags_match_truncated`/`budget_truncated`,
    each only for a value that lost something (same shape as retain-tags, audit.tag_list_keys)."""
    count, truncated = event.get("tags_count"), event.get("tags_truncated")
    keys = tag_list_keys("tags", event.get("tags") or [],
                         count if isinstance(count, int) else 0,
                         truncated if isinstance(truncated, int) else 0)
    for field in ("tags_match", "budget"):
        keys.update(field_keys(field, event.get(f"{field}_truncated") is True))
    return keys


def record(audit, *, token, identity, bank, method, tool, transport, event: Dict[str, Any]) -> None:
    """One audit line per reflect call. Never raises (a telemetry bug must not fail a call)."""
    try:
        audit.record(
            token=token, identity=identity, method=method, tool=tool,
            attempted_bank=bank, resolved_bank=bank, decision=DECISION,
            reason="based_on:" + str(event.get("based_on", "unknown")),
            extra={"reflect": {
                "transport": transport,
                "tags": list(event.get("tags") or []),
                **_marker_keys(event),
                "tags_match": event.get("tags_match"),
                "budget": event.get("budget"),
                "ok": bool(event.get("ok")),
                "based_on": event.get("based_on", "unknown"),
                "mental_models": list(event.get("mental_models") or []),
                "input_tokens": event.get("input_tokens"),
            }},
        )
    except Exception:  # noqa: BLE001 - telemetry is best-effort by contract
        return
