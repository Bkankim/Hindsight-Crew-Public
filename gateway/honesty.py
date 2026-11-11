"""Response-side honesty for the retain family (N5) — pure, stdlib-only.

Incident D1-② : upstream answered with a **success-shaped frame carrying a failure body**
and the model read it as "stored". The gateway is the only place that sees both what was
asked and what came back, so it is where that lie gets corrected — on the same isError
channel a model already knows how to read (SEP-1303).

This module owns the per-element decision only; framing (SSE/JSON parse + re-serialize)
belongs to `gateway.reframe`, and the wiring/fail-open/counter belong to `app.py`.
`annotate_element()` is applied through `reframe.transform_results()`, i.e. it receives a
**deep copy** of one JSON-RPC response element and may mutate it in place.

Decision order (first match wins — deliberately narrow at every step):

  1. JSON-RPC ``error``          → untouched. A protocol error is already unambiguous and
                                   rewriting it would forge a CallToolResult that never was.
  2. no dict ``result``          → untouched (nothing we understand).
  3. ``result.isError is True``  → untouched. Already honest; never "double-report".
  4. success shape + failure signal → ``isError`` flipped to true, text preserved verbatim.
  5. otherwise (real success)    → annotations appended (async guidance, autofill notice).

The failure signal (step 4) is **conservative by construction**. A dict ``structuredContent`` decides on
its own and the text heuristic never runs for it; without one the text heuristic decides:

  * ``structuredContent`` is a dict whose ``status`` is the string ``error`` (strip + lowercase,
    exact match only - no 'failed', no substring). That is what upstream v1.0.0 itself returns
    for a retain it could not store while answering a success-shaped frame: ``sync_retain`` with
    an unparseable timestamp gives ``isError:false`` + ``{"status":"error","message":...}``
    (fixtures/mcp/sync_retain_error_frame.raw, mcp_tools.py ``return {"status": "error", ...}``).
    Every observed success carries ``status`` ``completed``/``accepted``
    (fixtures/mcp/NOTES.md ④), so they are exempt.
  * ``structuredContent`` absent (not a dict) AND a text block that STARTS with ``error``/``failed``
    (word boundary, case-insensitive) or contains the phrase ``operation failed``.

That is why "the deployment failed last week" — a perfectly ordinary thing to retain — is
not a failure: 'failed' appears mid-sentence, and 'Errors'/'failed_count' do not end on a
word boundary. Widening this (bare substring 'error'/'failed' anywhere) would turn honest
success into a fabricated error, which is a worse failure mode than the one being fixed:
the model would retry a write that already landed. New signals get added only with a
fixture that shows upstream actually emitting them.

Annotations are appended as SEPARATE content blocks, never spliced into an existing text.
The retain result's first text block is a JSON document (`{"status":"accepted",...}`);
appending a line inside it would break any client that parses it.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

__all__ = [
    "ASYNC_NOTE", "AUTOFILL_NOTE", "TERMINAL_STATUSES",
    "annotate_element", "looks_like_failure", "pending_operation_id",
]

# Model-facing annotation texts (P5: a correction the client cannot see is a silent one).
ASYNC_NOTE = "[gateway: async retain - operation_id=%s, get_operation으로 완료를 확인하세요]"
AUTOFILL_NOTE = "[gateway: timestamp 자동 기록 %s]"
# Tag rewrites are corrections too: the caller must see what was actually stored (P5).
NORMALIZE_NOTE = "[gateway: 태그 정규화 %s]"
# Contradiction gate (issue #12, 3차): the retain WAS stored; the appended block carries the
# opposite-polarity precedent (text built by gateway.contradiction.warning_text) so the model
# can decide whether it really hit a wall or skipped a path.
# Schema-level restatement: a bare "array of string" taught clients nothing, which is how
# every runtime paid a wasted round trip before the gateway started describing the rule.
TAGS_SCHEMA_NOTE = ('Each tag must be "<prefix>:<slug>" using a prefix this bank declares '
                    '(see the tool description); lowercase [a-z0-9-] after the prefix.')

# Conservative failure heuristic — see module docstring for why it is this narrow.
FAILURE_PREFIX_RE = re.compile(r"^\s*(?:error|failed)\b", re.IGNORECASE)
FAILURE_PHRASE_RE = re.compile(r"\boperation\s+failed\b", re.IGNORECASE)

# An operation in one of these states is settled; polling guidance would be noise.
TERMINAL_STATUSES = frozenset({
    "completed", "complete", "done", "succeeded", "success",
    "failed", "error", "cancelled", "canceled",
})


def _text_blocks(result: Dict[str, Any]) -> List[str]:
    content = result.get("content")
    if not isinstance(content, list):
        return []
    return [b["text"] for b in content
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)]


def _append_text(result: Dict[str, Any], text: str) -> None:
    content = result.get("content")
    if not isinstance(content, list):
        content = []
        result["content"] = content
    content.append({"type": "text", "text": text})


def looks_like_failure(result: Dict[str, Any]) -> bool:
    """True when a success-shaped CallToolResult is really reporting a failure."""
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        # structural signal present → the text heuristic never runs; only status == "error" fails
        status = structured.get("status")
        return isinstance(status, str) and status.strip().lower() == "error"
    return any(FAILURE_PREFIX_RE.match(t) or FAILURE_PHRASE_RE.search(t)
               for t in _text_blocks(result))


def pending_operation_id(result: Dict[str, Any]) -> Optional[str]:
    """The operation_id of an accepted-but-unfinished async retain, else None."""
    structured = result.get("structuredContent")
    if not isinstance(structured, dict):
        return None
    op_id = structured.get("operation_id")
    if not isinstance(op_id, str) or not op_id.strip():
        return None
    if structured.get("memory_ids"):
        return None         # ids already handed out → the write is done
    status = structured.get("status")
    if isinstance(status, str) and status.strip().lower() in TERMINAL_STATUSES:
        return None
    return op_id


def _same_id(a: Any, b: Any) -> bool:
    """JSON-RPC id equality without str/int coercion (2 and "2" are different ids)."""
    if isinstance(a, str) != isinstance(b, str):
        return False
    return a == b


def autofilled_timestamp(el_id: Any, autofills: Sequence[Dict[str, Any]]) -> Optional[str]:
    """The timestamp the request side wrote for this element id (app.state stash, N2)."""
    for entry in autofills or ():
        if isinstance(entry, dict) and _same_id(entry.get("id"), el_id):
            value = entry.get("timestamp")
            if isinstance(value, str) and value:
                return value
    return None


def normalized_tag_note(el_id: Any, normalizations: Sequence[Dict[str, Any]]) -> Optional[str]:
    """The '<sent> -> <forwarded>' summary for this element, or None when nothing changed."""
    for entry in normalizations or ():
        if _same_id(el_id, entry.get("id")) and entry.get("tags"):
            # A split reports one pair per piece, all sharing the same (comma-bearing) sent
            # string. Consecutive pairs with one sent and DIFFERENT stored values fold into
            # `sent -> a, b, c`; identical pairs stay separate, so every no-split note is
            # byte-identical to before.
            groups: list = []
            for before, after in entry["tags"]:
                if groups and groups[-1][0] == before and after not in groups[-1][1]:
                    groups[-1][1].append(after)
                else:
                    groups.append((before, [after]))
            return ", ".join(f"{before} -> {', '.join(afters)}" for before, afters in groups)
    return None


def warning_note(el_id: Any, warnings: Sequence[Dict[str, Any]]) -> Optional[str]:
    """The warnings stashed for this element id (contradiction gate, tag hints #40), or None.

    Several entries for one id are joined one per line into a single note; one entry comes
    back exactly as stashed.
    """
    texts = [entry["text"] for entry in warnings or ()
             if isinstance(entry, dict) and _same_id(entry.get("id"), el_id)
             and isinstance(entry.get("text"), str) and entry["text"].strip()]
    return "\n".join(texts) if texts else None


def describe_tools(element: Any, tools_ids: Sequence[Any], rules_text: Optional[str],
                   retain_tools: Sequence[str]) -> Any:
    """Append the bank's rules to the retain-family tool descriptions in a tools/list result.

    Measured 2026-07-27: only Claude Code surfaces the `initialize` instructions to its
    model; YEW and aside drop them. A tool description is the one channel every client
    passes through, so the rules ride here too. Non-retain tools are left byte-identical.

    Only a rule-bearing bank's tools/list is buffered at all (a rule-less bank's stays a
    byte passthrough).
    """
    if not rules_text or not isinstance(element, dict):
        return element
    if not any(_same_id(element.get("id"), tid) for tid in tools_ids):
        return element
    result = element.get("result")
    if "error" in element or not isinstance(result, dict):
        return element
    tools = result.get("tools")
    if not isinstance(tools, list):
        return element
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("name") not in retain_tools:
            continue
        existing = tool.get("description")
        tool["description"] = (f"{existing}\n\n{rules_text}"
                               if isinstance(existing, str) and existing else rules_text)
        schema = tool.get("inputSchema")
        props = schema.get("properties") if isinstance(schema, dict) else None
        tags = props.get("tags") if isinstance(props, dict) else None
        if isinstance(tags, dict):
            tags["description"] = TAGS_SCHEMA_NOTE
    return element


def annotate_element(
    element: Any,
    *,
    retain_ids: Sequence[Any] = (),
    autofills: Sequence[Dict[str, Any]] = (),
    normalizations: Sequence[Dict[str, Any]] = (),
    warnings: Sequence[Dict[str, Any]] = (),
    initialize_ids: Sequence[Any] = (),
    instructions: Optional[str] = None,
) -> Any:
    """Honestify / annotate ONE JSON-RPC response element. Returns the element.

    `retain_ids` scopes the rewrite to the ids that were retain-family tools/call in the
    SAME request: in a mixed batch the recall sibling must come back byte-untouched.
    """
    if not isinstance(element, dict):
        return element
    el_id = element.get("id")
    if instructions and any(_same_id(el_id, iid) for iid in initialize_ids):
        # Handshake carries no retained content, so nothing here is honestified - the
        # gateway only ADDS its rules, appending when upstream stated its own.
        result = element.get("result")
        if isinstance(result, dict) and "error" not in element:
            existing = result.get("instructions")
            result["instructions"] = (f"{existing}\n{instructions}"
                                      if isinstance(existing, str) and existing else instructions)
        return element
    if not any(_same_id(el_id, rid) for rid in retain_ids):
        return element
    if "error" in element:                      # (1) protocol error — not ours to rewrite
        return element
    result = element.get("result")
    if not isinstance(result, dict):            # (2) shape we do not understand
        return element
    if result.get("isError") is True:           # (3) already honest
        return element

    if looks_like_failure(result):              # (4) the lie this module exists for
        result["isError"] = True
        return element

    op_id = pending_operation_id(result)        # (5) real success → annotate
    if op_id:
        _append_text(result, ASYNC_NOTE % op_id)
    filled = autofilled_timestamp(el_id, autofills)
    if filled:
        _append_text(result, AUTOFILL_NOTE % filled)
    rewrites = normalized_tag_note(el_id, normalizations)
    if rewrites:
        _append_text(result, NORMALIZE_NOTE % rewrites)
    warning = warning_note(el_id, warnings)
    if warning:
        _append_text(result, warning)
    return element
