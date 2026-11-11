"""Append-only audit log for the gateway.

Records token FINGERPRINT (never the raw token — public-repo secret hygiene),
attempted vs resolved bank, action, decision + reason, and timestamp. A single
process-wide lock serializes writes so concurrent requests cannot interleave lines.

`decision` vocabulary (the only consumer is this file logger — no parser to migrate):
  * "allow" / "deny"        — authorization outcome (gateway.policy.decide)
  * "enforce-autofill"      — request-side correction applied, request still forwarded
                              (reason: "timestamp-autofill:<value>"); N2/N3
  * "enforce-normalize"     - request-side tag correction applied, request still forwarded
                              (reason: "tag-normalize:<sent>-><stored>,..."); written only when
                              at least one tag was rewritten. One line per retain element
                              (MCP) / items[i] (REST). Tag strings only. The reason holds at most
                              the first 64 pairs (TAG_LOG_COUNT_MAX, issue #103) within
                              NORMALIZE_LOG_BYTES_MAX escaped bytes (issue #117), each side cut
                              to 256 chars / 1024 escaped bytes (cut_text). Only a line that
                              lost pairs or had a side cut gets `normalize_tags`:
                              "normalized_count" (all pairs) + "normalized_omitted" (pairs not
                              shown) when pairs were lost, "normalized_truncated" (cut sides)
                              when a side was cut - same shape as `retain_tags`.
  * "enforce-reject"        — format rule violated, nothing forwarded; answered on the MCP
                              isError channel or with a REST 422 rule-violation body
                              (reason: "rule-violation:RULE:<id>,..."); one line per
                              offending element (MCP) / items[i] (REST)
  * "rewrite-failure"       — ALARM: the response-side rewrite (N5) could not parse or
                              re-serialize upstream's answer, so the original bytes were
                              passed through uncorrected (fail-open). reason:
                              "response-rewrite:<why>". Pairs with the /healthz
                              `rewrite_failures` counter; a nonzero delta means honesty
                              enforcement was silently absent for that call.
  * "reflect-demand"        — telemetry (issue #11): one line per allowed reflect call with a
                              metadata-only `reflect` object (transport, tags, budget, isError,
                              served mental-model ids, token usage). Never query/answer text.
                              `reflect.tags` holds only the first 64 tag strings within
                              TAG_LOG_LIST_BYTES_MAX escaped bytes (issues #103/#117);
                              only a list that lost tags also gets `tags_count` and
                              `tags_omitted` inside `reflect` (the #99 convention), and
                              `tags_truncated` when a kept tag was cut. `tags_match` and
                              `budget` are cut to FIELD_LOG_MAX (64) chars; a cut one adds
                              `tags_match_truncated` / `budget_truncated`: true.
                              Consumer: scripts/model_audit.py (weekly demand signal).
  * "allow" of get_mental_model - issue #238: the line also carries `mental_model_id`, the
                              requested id cut by cut_field (64 chars / 256 escaped bytes; a cut
                              id adds `mental_model_id_truncated: true`). A missing or non-string
                              id leaves no key (the consumer counts that as unknown), as does
                              every other tool and every deny line. Consumer: scripts/model_audit.py.
  * "context-trigger"       — 길목 주입 (issue #12, 1차): one line per POST /context-triggers
                              call. reason "match:<id>" | "match-empty:<id>" |
                              "recall-failed:<id>" | "miss"; `context_trigger` carries tool,
                              text length, matched ids, served memory ids, token estimate, and
                              `recall_error` (null, or on recall-failed "timeout" | "transport" |
                              "http-<status>" | "bad-json" | "bad-shape", issue #268).
                              Never the task text or the injected memory text.
  * "contradiction-warn"    — retain 모순 게이트 (issue #12, 3차): a negative-label retain was
                              checked against positive precedents. reason "precedent:<ids>" |
                              "no-precedent" | "recall-failed" (`gate.recall_error` names the
                              cause, same codes, issue #268). "recall-failed" is written for
                              every gate-eligible retain item (gotcha / correction too), since
                              the gates share one recall; ids + scores only.
  * "recurrence"            — 재발 탐지 (issue #12, 2차): reason "draft:<signal>" (a pending
                              trigger row was written + notified) | "skip:dedup" |
                              "skip:no-verified-recall" | "skip:retain-not-completed".
                              ids, scores, notify status only.
  * "injection-correction"  - successful correction retain related to recently served ids
                              above the recurrence score threshold. Metadata only; an
                              identity-bank association signal, not a measured false positive.
  * "retain-tags"           - issue #37: one line per ALLOWED retain (MCP `retain`/`sync_retain`
                              call; REST `memories` POST `items[i]`), corrected or not, so the
                              arrival of a tag is traceable even when nothing was rewritten.
                              reason "tags-unchanged" | "tags-rewritten" (the rewrite pairs
                              stay on the "enforce-normalize" line). `retain_tags` carries
                              transport ("mcp" | "rest"), `sent` (tags as the caller sent
                              them, [] when none) and `stored` (tags of the rules verdict that
                              was FORWARDED to upstream - written before the send, so it does
                              not say whether upstream then stored the memory), and on MCP `client`: the initialize
                              `clientInfo.name` remembered for the request's Mcp-Session-Id,
                              "unknown" when the session never initialized through this
                              process (e.g. after a gateway restart). REST has no `client`.
                              `sent` and `stored` hold only the first 64 tag strings
                              (TAG_LOG_COUNT_MAX, issue #99) within TAG_LOG_LIST_BYTES_MAX escaped
                              bytes (issue #117), each cut to 256 chars / 1024 escaped bytes; a
                              list that lost tags also gets `<list>_count` (all tag strings
                              received) and `<list>_omitted` (count - shown), counted over
                              strings only, and one that had a tag cut gets `<list>_truncated`
                              (how many shown tags were cut). A list that lost nothing has none
                              of those keys. `client` is already cut to 64 printable chars when
                              the name is bound to the session (issue #37), so it needs no bound
                              here.
                              A rejected retain gets no such line, only "enforce-reject".
                              Tag strings only - never content or query text. Separate from
                              "allow" because that line is written before the rules run.
The recall ledger file (audit/recall-queries.log, gateway/recurrence.py) is not this log but
follows the same markers (issue #117): its `tags` use tag_list/tag_list_keys and its
`tags_match` cut_field + `tags_match_truncated`; only the FILE line is bounded.
An "allow" line for an element followed by "enforce-reject" is expected and correct:
authorization passed, the format did not.

Lone surrogates (issue #135): a string that arrives as JSON `"\\ud800"` (or raw CESU bytes) is a
str holding U+D800-U+DFFF, which strict utf-8 cannot write. Every audit line, recall ledger
line and pending-trigger line is written with each such character replaced by U+FFFD (3 bytes
either way, so the escaped-byte bounds above measure what is written). A line where that
happened gets a top-level `surrogates_replaced: <count>` marker (the #99 convention: only a
line that lost something carries the key); a line without surrogates is byte-identical to before.
The file never holds a `\\udXXX` escape - json.loads would turn it back into a surrogate in a
consumer (scripts/model_audit.py) and its strict utf-8 output would crash. The pending-trigger
row (gateway/recurrence.py) is replaced without a marker: it is a draft record, not a log line.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from typing import Optional

_LOCK = threading.Lock()

# Tag strings on a metadata-only audit line are bounded in count, length and BYTES (issues
# #99/#103/#117): an unbounded list would make one line as large as the request, and the log
# has no rotation. Bytes are measured the way the line is written (json.dumps, ensure_ascii=
# False): a control character costs 6 ("\u0001"), a quote or backslash 2, so 64 tags of 256
# control characters were ~96KB although the character caps held.
TAG_LOG_MAX = 256                 # chars per tag. scripts/tag_hygiene.py and model_audit.py cut
                                  # tags the same way (t[:256]) - keep the char cut itself unchanged
TAG_LOG_COUNT_MAX = 64
TAG_LOG_BYTES_MAX = 4 * TAG_LOG_MAX   # escaped bytes of ONE logged tag: printable text never
                                      # reaches it (<= 4 UTF-8 bytes per char), escape-heavy text does
# Escaped bytes of one whole tag list ("<tag>", per element) / one normalize reason. Sized so a
# list of TAG_LOG_COUNT_MAX plain-ASCII tags of TAG_LOG_MAX chars still fits unchanged; only
# escape-heavy or non-ASCII-heavy lists are shortened (and say so via `<list>_omitted`).
TAG_LOG_LIST_BYTES_MAX = TAG_LOG_COUNT_MAX * (TAG_LOG_MAX + 6)
NORMALIZE_LOG_BYTES_MAX = TAG_LOG_COUNT_MAX * (2 * TAG_LOG_MAX + 3)
# Free-text request arguments on an audit line (reflect `tags_match`/`budget`, recall ledger
# `tags_match`): upstream's valid values are a few characters, the cut matches the recall
# ledger's budget/max_tokens cap (#80).
FIELD_LOG_MAX = 64
FIELD_LOG_BYTES_MAX = 4 * FIELD_LOG_MAX


_SURROGATES = re.compile("[\ud800-\udfff]")
SURROGATES_MARKER = "surrogates_replaced"


def replace_surrogates(line: str) -> tuple:
    """(`line` with every lone surrogate U+D800-U+DFFF replaced by U+FFFD, how many were replaced).
    Done on the str, before encoding: utf-8 error handlers do not take a non-ASCII replacement."""
    return _SURROGATES.subn("\ufffd", line)


def dump_line(entry: dict, *, mark: bool = True, on_replace: Optional[dict] = None) -> str:
    """The file line (no newline) of `entry`: json.dumps(ensure_ascii=False, sort_keys=True), lone
    surrogates replaced, and `surrogates_replaced: <count>` on lines where that happened (`mark`).
    `on_replace` keys override `entry` on such a line only (a row that no longer holds what was
    received, e.g. the ledger's `reusable: false`)."""
    line, count = replace_surrogates(json.dumps(entry, ensure_ascii=False, sort_keys=True))
    if count and mark:
        line, _ = replace_surrogates(json.dumps({**entry, **(on_replace or {}), SURROGATES_MARKER: count},
                                                ensure_ascii=False, sort_keys=True))
    return line


def _escaped_len(text: str) -> int:
    """Bytes `text` takes inside a written audit line, without the surrounding quotes."""
    return len(json.dumps(text, ensure_ascii=False).encode("utf-8", "surrogatepass")) - 2


def cut_text(value: str, max_chars: int = TAG_LOG_MAX, max_bytes: int = TAG_LOG_BYTES_MAX) -> tuple:
    """(longest prefix of `value` within max_chars characters AND max_bytes escaped bytes,
    whether anything was cut). The prefix test is monotonic, so a binary search finds the cut."""
    text = value[:max_chars]
    if _escaped_len(text) > max_bytes:
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if _escaped_len(text[:mid]) <= max_bytes:
                lo = mid
            else:
                hi = mid - 1
        text = text[:lo]
    return text, len(text) < len(value)


def cut_field(value: str) -> tuple:
    """(`value` cut to FIELD_LOG_MAX chars / FIELD_LOG_BYTES_MAX escaped bytes, whether cut)."""
    return cut_text(value, FIELD_LOG_MAX, FIELD_LOG_BYTES_MAX)


def field_keys(name: str, truncated: bool) -> dict:
    """`<field>_truncated: true` - only on a line whose string field was actually cut."""
    return {f"{name}_truncated": True} if truncated else {}


def mental_model_audit(arguments) -> Optional[dict]:
    """Extra audit keys of an allowed get_mental_model call (issue #238): the requested
    `mental_model_id` bounded by cut_field, plus `mental_model_id_truncated` when it was cut.
    None when the argument is missing or not a string (nothing is invented for it)."""
    value = arguments.get("mental_model_id") if isinstance(arguments, dict) else None
    if not isinstance(value, str):
        return None
    text, was_cut = cut_field(value)
    return {"mental_model_id": text, **field_keys("mental_model_id", was_cut)}


def tag_list(value) -> tuple:
    """(tag strings to log, number of tag strings received, number of logged tags that were cut):
    only strings, each cut by cut_text, at most TAG_LOG_COUNT_MAX of them and at most
    TAG_LOG_LIST_BYTES_MAX escaped bytes together. A bank without declared prefixes lets any
    JSON through as a "tag", and this line must never become a channel for content (issue #37);
    an unbounded count or size would make one line as large as the request (issues #99/#117)."""
    if not isinstance(value, (list, tuple)):
        return [], 0, 0
    strings = [t for t in value if isinstance(t, str)]
    shown, used, truncated = [], 0, 0
    for tag in strings[:TAG_LOG_COUNT_MAX]:
        text, was_cut = cut_text(tag)
        cost = _escaped_len(text) + 6                    # two quotes + ", "
        if used + cost > TAG_LOG_LIST_BYTES_MAX:
            break
        shown.append(text)
        used += cost
        truncated += was_cut
    return shown, len(strings), truncated


def tag_list_keys(name: str, shown, count: int, truncated: int) -> dict:
    """Marker keys of a logged tag list, the `<list>_count`/`<list>_omitted` convention of #99:
    `_count` (all tag strings received) and `_omitted` (count - shown) only when the list lost
    tags, `_truncated` (how many shown tags were cut) only when one was. A list that lost
    nothing adds no key at all."""
    keys: dict = {}
    if count > len(shown):
        keys[f"{name}_count"] = count
        keys[f"{name}_omitted"] = count - len(shown)
    if truncated:
        keys[f"{name}_truncated"] = truncated
    return keys


def normalize_audit(pairs) -> tuple:
    """(reason, extra) of an "enforce-normalize" line for the (sent, stored) tag rewrites of
    one retain element. The reason lists at most TAG_LOG_COUNT_MAX pairs within
    NORMALIZE_LOG_BYTES_MAX escaped bytes, each side cut by cut_text; a list that lost pairs
    also records its size, like `<list>_count`/`<list>_omitted` do on the retain-tags line
    (issues #103/#117), and `normalized_truncated` counts the cut sides."""
    pairs = list(pairs)
    shown, used, truncated = [], 0, 0
    for before, after in pairs[:TAG_LOG_COUNT_MAX]:
        b, b_cut = cut_text(before)
        a, a_cut = cut_text(after)
        item = f"{b}->{a}"
        cost = _escaped_len(item) + 1                    # the joining comma
        if used + cost > NORMALIZE_LOG_BYTES_MAX:
            break
        shown.append(item)
        used += cost
        truncated += b_cut + a_cut
    info = tag_list_keys("normalized", shown, len(pairs), truncated)
    return "tag-normalize:" + ",".join(shown), ({"normalize_tags": info} if info else None)


def fingerprint(token: Optional[str]) -> str:
    if not token:
        return "anon"
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


class Audit:
    def __init__(self, path: Optional[str] = None):
        self.path = path or os.environ.get("HC_AUDIT_LOG", "audit/gateway.log")
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)

    def record(
        self,
        *,
        token: Optional[str],
        identity: Optional[str],
        method: str,
        tool: Optional[str],
        attempted_bank: Optional[str],
        resolved_bank: Optional[str],
        decision: str,           # "allow" | "deny" | "enforce-autofill" | "enforce-reject"
        reason: str,
        extra: Optional[dict] = None,   # namespaced metadata (e.g. {"reflect": {...}})
    ) -> dict:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "token_fp": fingerprint(token),
            "identity": identity,
            "method": method,
            "tool": tool,
            "attempted_bank": attempted_bank,
            "resolved_bank": resolved_bank,
            "decision": decision,
            "reason": reason,
        }
        if extra:
            for key, value in extra.items():
                entry.setdefault(key, value)
        line = dump_line(entry)
        with _LOCK:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        return entry
