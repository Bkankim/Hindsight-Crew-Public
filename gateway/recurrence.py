"""재발 탐지 + 트리거 초안 (issue #12, 2차) - detect, draft, notify; never register.

The last step of learning from a trap - putting a row in the trigger table - used to be a
purely manual chore. This module shrinks it to a one-click approval:

  1. DETECT (automatic, LLM-free). Two recurrence signals, both read off a retain that is
     already passing through the gateway:
       (a) gotcha-recurrence  - a new kind:gotcha retain is highly similar (recall reranker
           >= HC_RECURRENCE_MIN_RERANK, default 0.92) to an EXISTING gotcha: the same trap
           recorded a second time.
       (b) correction-after-recall - a retain labelled as a correction ([정정...], [수정...],
           [철회...]) lands within HC_RECURRENCE_WINDOW_MIN (default 60) minutes of a recall by
           the same identity on the same bank: the "failed, then recalled late, then fixed"
           pattern.
  2. DRAFT (automatic). A trigger row whose match keywords come from the memory's tags,
     entities and host-like tokens, and whose recall QUERY is the recall that actually rescued
     session - a successful recall in the same available session scope whose results contained
     the similar memory. This proves retrieval, not causal rescue. A gotcha without such a
     query produces a needs-evidence candidate with NO registrable trigger.
  3. APPROVE (human). Drafts are appended to audit/pending-triggers.jsonl and announced on
     Discord. Registration stays manual: copy the "trigger" object into
     policies/local/context-triggers.json (picked up on the next request, no restart).

Full automation was rejected on purpose (same governance as "no automatic mental models"):
a trigger fires on every future matching prompt (blast radius), a false notification costs
one second while a false registration costs forever, and letting a memory promote itself to
a quasi-instruction is the path by which one polluted memory becomes every runtime's order.

Recall ledger: the audit log stays metadata-only, so recall QUERY text lives in its own file,
audit/recall-queries.log (JSONL: ts, identity, bank, transport, query, tags, result_ids, budget,
max_tokens). It exists so a draft can reuse a proven query and so two recalls of the same query
can be told apart by their arguments; budget/max_tokens are null when the request omitted them or
sent a value the ledger does not record (see _ledger_scalar). The gateway never guesses upstream's
defaults.
"""
from __future__ import annotations

import collections
import json
import math
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence
from urllib.parse import quote

import httpx

from .audit import cut_field, dump_line, field_keys, tag_list, tag_list_keys
from .context_triggers import (QUERY_TOKEN_TARGET, coerce_recall_budget, coerce_recall_max_tokens,
                               query_token_bound, rerank_score, scrub_secrets)
from . import contradiction

DECISION = "recurrence"                # audit vocabulary (audit.py)
DEFAULT_MIN_RERANK = 0.92
DEFAULT_WINDOW_MIN = 60
DEFAULT_PENDING_FILE = "audit/pending-triggers.jsonl"
DEFAULT_QUERY_LOG = "audit/recall-queries.log"
DEDUP_TTL_SECONDS = 24 * 3600
SIGNAL_GOTCHA = "gotcha-recurrence"
SIGNAL_CORRECTION = "correction-after-recall"

_LOCK = threading.Lock()
_CORRECTION_WORDS = ("정정", "수정", "철회", "번복", "correction", "corrected", "retract")
_HOST_RE = re.compile(r"\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)+\.[a-z]{2,}\b|\b[a-z0-9-]+\.(?:com|dev|io|net|org|kr|app)\b")
_WORD_RE = re.compile(r"[a-z][a-z0-9_-]{3,}")
_STOP = frozenset({
    "http", "https", "that", "this", "with", "from", "into", "then", "than", "when", "what",
    "true", "false", "null", "none", "file", "path", "user", "users", "demoalt", "claude", "code",
    "aside", "bash", "retain", "recall", "hindsight", "gateway", "memory", "session", "error",
    "failed", "cannot", "operation", "permitted", "secrets", "runtime", "home",
})
_GENERIC_SLUGS = frozenset({"security", "deploy", "dev", "blog", "ops", "gotcha", "completion",
                            "decision", "rule", "fix", "setting", "verification", "discovery",
                            "research", "handoff", "demo", "demoalt", "misc", "crew", "config",
                            "job", "search"})
# 범용 메일·검색·코드 호스트(issue #277): 어떤 대화에나 나와 any-of 트리거를 아무 때나 발화시킨다.
# 맨 라벨(gmail 등)은 _WORD_RE가 호스트에서 따로 뽑으므로 함께 거른다.
_GENERIC_HOSTS = frozenset({"gmail.com", "naver.com", "github.com", "google.com"})
_GENERIC_KEYWORDS = _GENERIC_SLUGS | _GENERIC_HOSTS | {h.split(".", 1)[0] for h in _GENERIC_HOSTS}
_GENERIC_HOST_SUFFIXES = tuple("." + h for h in _GENERIC_HOSTS)      # mail.google.com 같은 서브도메인
_KIND_GOTCHA = "kind:gotcha"

# aside 등록 가능성 단서 (issue #237). 길목 훅은 tool "aside"만 보내므로(#174) aside 밖 함정 초안은
# 등록해도 발화하지 않는다. 영문 단서는 소문자 비교 + 영숫자 경계(복수 s는 허용, exec이 execute/executable에,
# repl이 replace에 걸리지 않게), 한글 단서는 부분 문자열이다. 오분류는 likely 쪽으로 기운다:
# 느슨한 단서(login/cookie/browser)가 aside 밖 글에 걸리면 표시만 빠질 뿐 예전 초안과 같고,
# 진짜 aside 함정이 unlikely로 떨어지는 쪽이 더 비싸다. 어느 쪽이든 자동 기각은 없다.
ASIDE_CLUES_ASCII = ("aside", "mcp__aside", "exec", "repl", "browser", "login", "cookie", "2fa",
                     "chrome", "playwright", "captcha", "totp")
ASIDE_CLUES_KO = ("어사이드", "브라우저", "브라우징", "로그인", "쿠키", "크롬", "캡차", "스크린샷")
ASIDE_LIKELY = "likely"
ASIDE_UNLIKELY = "unlikely"
ASIDE_UNLIKELY_NOTE = "등록 불가 추정(비-aside 함정, #174)"
_ASIDE_ASCII_RE = re.compile(
    r"(?<![a-z0-9])(?:" + "|".join(re.escape(c) for c in ASIDE_CLUES_ASCII) + r")s?(?![a-z0-9])")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def min_rerank() -> float:
    return _env_float("HC_RECURRENCE_MIN_RERANK", DEFAULT_MIN_RERANK)


def window_seconds() -> float:
    return _env_float("HC_RECURRENCE_WINDOW_MIN", DEFAULT_WINDOW_MIN) * 60.0


def now_iso(ts: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else time.time()))


def now_iso_ms(ts: float) -> str:
    """now_iso with millisecond precision ("...:SS.mmmZ"); truncates like now_iso, never rounds up."""
    secs, msec = divmod(int(ts * 1000 + 1e-6), 1000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(secs)) + f".{msec:03d}Z"


# --- recall ledger --------------------------------------------------------------------

@dataclass(frozen=True)
class RecallEvent:
    ts: float
    identity: str
    bank: str
    transport: str
    query: str
    tags: tuple
    result_ids: tuple
    session_id: Optional[str] = None
    tags_match: str = "any"
    reusable: bool = True
    budget: Any = None
    max_tokens: Any = None
    client: Optional[str] = None
    arrived_ts: Optional[float] = None

    def as_json(self) -> Dict[str, Any]:
        """The ledger FILE line. Only this serialization is bounded (issue #117): the in-memory
        event keeps every tag and the exact tags_match, because a recurrence draft replays
        them (build_draft reads rescue.tags / rescue.tags_match). Tags follow audit.tag_list
        (count, per-tag and list byte bounds) and add `tags_count`/`tags_omitted`/`tags_truncated`
        only when something was lost; a string `tags_match` is cut to audit.FIELD_LOG_MAX and
        a value that is not a string or a recordable scalar is written null, both marked
        `tags_match_truncated: true`."""
        tags, tags_count, tags_truncated = tag_list(list(self.tags))
        match = self.tags_match
        if isinstance(match, str):
            match, match_cut = cut_field(match)
        else:
            kept = _ledger_scalar(match)
            match, match_cut = kept, kept is None and self.tags_match is not None
        tag_keys = tag_list_keys("tags", tags, tags_count, tags_truncated)
        lossy = bool(tag_keys) or match_cut
        return {"ts": now_iso(self.ts), "identity": self.identity, "bank": self.bank,
                "transport": self.transport, "query": self.query, "tags": tags,
                **tag_keys,
                "result_ids": list(self.result_ids), "session_id": self.session_id,
                "tags_match": match, **field_keys("tags_match", match_cut),
                # A row that lost tags or tags_match no longer replays the recall that ran, and the
                # file consumers (duplicate_recall, bench prune_effect) read tags as the full filter
                # without looking at the markers: not reusable there (the in-memory event keeps its value).
                "reusable": self.reusable and not lossy,
                "budget": self.budget, "max_tokens": self.max_tokens, "client": self.client,
                "arrived_ts": now_iso_ms(self.arrived_ts) if self.arrived_ts is not None else None}


LEDGER_ARG_MAX_CHARS = 64          # budget/max_tokens strings: upstream's valid values are far shorter
LEDGER_ARG_MAX_NUMBER = 2 ** 53    # largest int a JSON double carries exactly; beyond it the line only grows


def _ledger_scalar(value: Any) -> Any:
    """A request argument as received, or None when it is not worth recording.

    None means "the request omitted it OR the gateway does not record this value" (explicit
    null, list/dict, non-finite float, number beyond +-2**53) - the ledger does not tell those
    apart. Only recalls upstream answered successfully are ledgered, but upstream's MCP schema
    types `budget` as a bare string (no enum), so a string is not known to be valid: it goes
    through the same scrub_secrets as `query` and is cut to LEDGER_ARG_MAX_CHARS. Scalars keep
    the type that was SENT ("4096" stays a string even if upstream coerced it), which is what
    tells two requests apart.
    """
    if isinstance(value, str):
        return scrub_secrets(value)[:LEDGER_ARG_MAX_CHARS]
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value if abs(value) <= LEDGER_ARG_MAX_NUMBER else None
    return None


class RecallLedger:
    """Recent recall queries: bounded in-memory deque + append-only JSONL file."""

    def __init__(self, path: Optional[str] = None, maxlen: int = 500):
        self.path = path or os.environ.get("HC_RECALL_QUERY_LOG", DEFAULT_QUERY_LOG)
        self._events: Deque[RecallEvent] = collections.deque(maxlen=maxlen)

    def record(self, *, identity: Optional[str], bank: str, transport: str, query: Any,
               tags: Any, result_ids: Sequence[str] = (), ts: Optional[float] = None,
               session_id: Optional[str] = None, tags_match: str = "any",
               reusable: bool = True, budget: Any = None,
               max_tokens: Any = None, client: Optional[str] = None,
               arrived_ts: Optional[float] = None) -> Optional[RecallEvent]:
        if not isinstance(query, str) or not query.strip():
            return None
        # Tags reach the ledger file too, so they go through the same scrub as `query` (issue #458
        # final-review C3: a secret-shaped tag was written verbatim). A clean tag is unchanged.
        sent_tags = tuple(t for t in tags if isinstance(t, str)) if isinstance(tags, list) else ()
        clean_tags = tuple(scrub_secrets(t) for t in sent_tags)
        clean_query = scrub_secrets(query.strip())
        done = ts if ts is not None else time.time()
        ev = RecallEvent(ts=done, identity=identity or "anon",
                         bank=bank, transport=transport, query=clean_query[:1000],
                         tags=clean_tags, result_ids=tuple(str(i) for i in result_ids),
                         session_id=session_id, tags_match=tags_match,
                         budget=_ledger_scalar(budget), max_tokens=_ledger_scalar(max_tokens),
                         client=client,
                         # a wall clock that stepped back mid-request must not put arrival after completion
                         arrived_ts=None if arrived_ts is None else min(arrived_ts, done),
                         # a scrubbed tag (rework #458-1) makes the row unreplayable like a scrubbed query:
                         # a draft must never replay "[redacted]" as a recall filter
                         reusable=reusable and clean_tags == sent_tags
                         and clean_query == query.strip() and 8 <= len(clean_query) <= 500
                         and query_token_bound(clean_query) <= QUERY_TOKEN_TARGET)
        with _LOCK:
            self._events.append(ev)
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(dump_line(ev.as_json(), on_replace={"reusable": False}) + "\n")     # lone surrogate -> U+FFFD (#135); the row no longer replays the recall that ran
            except OSError:
                pass            # the deque still serves drafting; the file is best-effort
        return ev

    def recent(self, *, identity: Optional[str], bank: str, since: float) -> List[RecallEvent]:
        who = identity or "anon"
        with _LOCK:
            events = [e for e in self._events if e.identity == who and e.bank == bank and e.ts >= since]
        events.sort(key=lambda e: e.ts, reverse=True)
        return events

    def rescue_for(self, *, identity: Optional[str], bank: str, memory_ids: Sequence[str],
                   window: Optional[float] = None, session_id: Optional[str] = None) -> Optional[RecallEvent]:
        """Newest successful, reusable retrieval of the requested ids in the same scope."""
        since = time.time() - (window_seconds() if window is None else window)
        events = self.recent(identity=identity, bank=bank, since=since)
        wanted = set(memory_ids)
        for ev in events:
            if ev.reusable and ev.session_id == session_id and wanted.intersection(ev.result_ids):
                return ev
        return None


class InjectionLedger:
    """Bounded, metadata-only serving history. Correlation is a signal, not proof of error."""

    def __init__(self, maxlen: int = 500):
        self._events = collections.deque(maxlen=maxlen)

    def record(self, *, identity, bank, trigger_id, memory_ids):
        if memory_ids:
            with _LOCK:
                self._events.append((time.time(), identity, bank, trigger_id, tuple(memory_ids)))

    def related(self, *, identity, bank, memory_ids):
        wanted = set(memory_ids)
        since = time.time() - window_seconds()
        with _LOCK:
            hits = [(trig, wanted.intersection(ids)) for ts, who, b, trig, ids in self._events
                    if ts >= since and who == identity and b == bank and wanted.intersection(ids)]
        return {"trigger_ids": sorted({trig for trig, _ in hits}),
                "memory_ids": sorted({mid for _, ids in hits for mid in ids}),
                "scope": "identity-bank", "evidence_method": "recent-served-id+reranker"}


def recall_result_ids(payload: Any) -> List[str]:
    """Memory ids out of a parsed recall response ({results:[{id}]} or a bare list)."""
    results = payload.get("results") if isinstance(payload, dict) else payload
    if not isinstance(results, list):
        return []
    return [r["id"] for r in results if isinstance(r, dict) and isinstance(r.get("id"), str)]


def mcp_recall_payload(result: Any) -> Any:
    """Unwrap a recall CallToolResult to its {results:[...]} payload, or None.

    Upstream (v0.8.3, measured 2026-09-21) DOUBLE-encodes: structuredContent.result is a
    JSON *string*, and content[0].text is the same JSON string. Reading the string as the
    payload silently yields no ids (review D2: 97/97 live MCP ledger entries were empty), so
    a string is decoded first and the text block is the fallback.
    """
    if not isinstance(result, dict) or result.get("isError"):
        return None
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        inner = structured.get("result", structured)
        if isinstance(inner, str):
            try:
                inner = json.loads(inner)
            except ValueError:
                inner = None
        if isinstance(inner, (dict, list)):
            return inner
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            try:
                parsed = json.loads(block["text"])
            except ValueError:
                continue
            if isinstance(parsed, (dict, list)):
                return parsed
    return None


# --- signals ----------------------------------------------------------------------------

def is_gotcha(tags: Any) -> bool:
    return isinstance(tags, list) and _KIND_GOTCHA in tags


def is_correction(content: Any) -> bool:
    label = contradiction.label_of(content)
    return bool(label) and any(w in label for w in _CORRECTION_WORDS)


def similar_gotchas(content: str, results: Sequence[Dict[str, Any]], *,
                    threshold: Optional[float] = None) -> List[Dict[str, Any]]:
    thr = min_rerank() if threshold is None else threshold
    norm = " ".join(content.split())
    out = []
    for mem in results:
        text = mem.get("text")
        tags = mem.get("tags")
        if not isinstance(text, str) or " ".join(text.split()) == norm:
            continue
        if not (isinstance(tags, list) and _KIND_GOTCHA in tags):
            continue
        if rerank_score(mem) >= thr:
            out.append(mem)
    out.sort(key=rerank_score, reverse=True)
    return out


# --- derivation (issue #197) ---------------------------------------------------------------
# The gate runs BEFORE the retain is forwarded (app.py), so the new retain has no id yet and
# can never be a source of what the gate recall returns. What #150 measured (4 of 12 drafts,
# 2026-10-07) was an observation consolidated from a SIBLING retain of the same work minutes
# earlier; consolidation later folded the new retain into it too, which made it look
# self-derived after the fact. Two exclusions, judged on the upstream detail view of an
# above-threshold observation only (one GET per candidate, never per retain):
#   self-derived    - a source memory's body equals the new retain's body (normalize_body):
#                     a duplicate retain, not a recurrence.
#   sibling-derived - EVERY source was mentioned within HC_RECURRENCE_WINDOW_MIN before now:
#                     the same work recorded twice, not the trap stepped on again later.
# Upstream stores no session or work identifier on a memory (document_id is per retain,
# context/metadata are empty), so sibling-derived is time-only: a different session that hits
# the same trap inside the window is missed (judgement in _out/197/judgement.md).
SKIP_SELF_DERIVED = "skip:self-derived"
SKIP_SIBLING_DERIVED = "skip:sibling-derived"
SOURCE_LOOKUP_FAILED = "source-lookup-failed"
SOURCE_INCOMPLETE = "source-incomplete"           # draft kept: some sources were not returned
LOOKUP_CAP_REACHED = "lookup-cap-reached"         # draft kept: candidate past MAX_SOURCE_LOOKUPS
LOOKUP_DEADLINE_REACHED = "lookup-deadline-reached"   # draft kept: item's lookup budget spent
MAX_SOURCE_LOOKUPS = 3              # candidates past this keep today's behaviour (no lookup)
SOURCE_LOOKUP_TIMEOUT_SECONDS = 3.0
SOURCE_LOOKUP_BUDGET_SECONDS = 4.0  # all lookups of ONE retain item; the gate runs before forward


class MalformedDetail(ValueError):
    """The detail view's source_memories is not a list: judged as a failed lookup."""


def normalize_body(text: str) -> str:
    """Whitespace collapsed and case folded. The stored source body measured byte-equal to the
    retain body (2026-10-07); this only absorbs a client re-wrapping or re-casing the same text."""
    return " ".join(text.split()).lower()


def _epoch(value: Any) -> Optional[float]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def needs_source_lookup(memory: Dict[str, Any]) -> bool:
    """Only consolidated observations have source memories."""
    return memory.get("fact_type") == "observation"


def derivation(content: str, detail: Dict[str, Any], *, now: Optional[float] = None,
               window: Optional[float] = None) -> Optional[str]:
    """SKIP_SELF_DERIVED / SKIP_SIBLING_DERIVED when `detail` (upstream GET memory) is not a
    recurrence of `content`, SOURCE_INCOMPLETE when the window rule cannot be judged on the full
    source set, else None. Only the two skip values drop the candidate.

    Upstream (hindsight_api engine/memories/pg/curation.py get_memory_unit) returns every
    source with no LIMIT, but reads them from the live table only: an invalidated source is
    missing from source_memories while its id stays in source_memory_ids. "All sources in the
    window" on such a subset could hide an old source, so a short list keeps the draft.
    Raises MalformedDetail when source_memories is present but not a list."""
    raw = detail.get("source_memories")
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise MalformedDetail(type(raw).__name__)
    sources = [s for s in raw if isinstance(s, dict)]
    if not sources:
        return None
    norm = normalize_body(content)
    if any(isinstance(s.get("text"), str) and normalize_body(s["text"]) == norm for s in sources):
        return SKIP_SELF_DERIVED            # conclusive even on a partial list
    ids = detail.get("source_memory_ids")
    if len(sources) != len(raw) or (isinstance(ids, list) and len(ids) != len(sources)):
        return SOURCE_INCOMPLETE
    t = time.time() if now is None else now
    w = window_seconds() if window is None else window
    ages = [_epoch(s.get("mentioned_at")) for s in sources]
    # A source with no readable time, or dated after now (client-supplied timestamp), is not
    # proven to be a sibling: keep the draft.
    if all(a is not None and 0 <= t - a <= w for a in ages):
        return SKIP_SIBLING_DERIVED
    return None


async def upstream_memory(client: httpx.AsyncClient, *, upstream_key: str, bank: str,
                          memory_id: str,
                          timeout: float = SOURCE_LOOKUP_TIMEOUT_SECONDS) -> Optional[Dict[str, Any]]:
    """GET one memory's detail view (with source_memories). None = failure (caller keeps the draft)."""
    headers = {"accept": "application/json"}
    if upstream_key:
        headers["authorization"] = f"Bearer {upstream_key}"
    try:
        resp = await client.get(f"/v1/default/banks/{quote(bank, safe='')}/memories/{quote(memory_id, safe='')}",
                                headers=headers, timeout=timeout)
    except (httpx.HTTPError, OSError):
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# --- draft ------------------------------------------------------------------------------

def match_keywords(new_content: str, similar: Optional[Dict[str, Any]]) -> List[str]:
    """Any-of keyword group for the draft: hosts + words shared by both texts + tag slugs.

    One generic-word filter (_GENERIC_KEYWORDS) applies to every source (#277); empty if nothing survives."""
    # Scrub BEFORE case folding: Base32 recognition depends on case.
    a = scrub_secrets(new_content or "").lower()
    b = scrub_secrets(str((similar or {}).get("text", ""))).lower()
    hosts = set(_HOST_RE.findall(a)) | set(_HOST_RE.findall(b))
    shared = {w for w in _WORD_RE.findall(a) if w in set(_WORD_RE.findall(b))} - _STOP if b else set()
    slugs = set()
    for tag in (similar or {}).get("tags") or []:
        if isinstance(tag, str) and tag.split(":", 1)[0] in ("topic", "project"):
            for part in tag.split(":", 1)[1].split("-"):
                if len(part) >= 3 and scrub_secrets(part) == part:
                    slugs.add(part)
    entities = {scrub_secrets(str(e)).lower() for e in (similar or {}).get("entities") or [] if len(str(e)) >= 3}
    ordered = sorted(hosts) + sorted(shared & entities) + sorted(slugs) + sorted(shared - entities)
    seen: List[str] = []
    for kw in ordered:
        if kw not in seen and len(kw) >= 3 and kw != "redacted" and kw not in _GENERIC_KEYWORDS \
                and not kw.endswith(_GENERIC_HOST_SUFFIXES):
            seen.append(kw)
    return seen[:6]


def aside_scope(new_content: str, similar: Optional[Dict[str, Any]], tool_hint: Optional[str] = None) -> str:
    """'likely' | 'unlikely': can a trigger built from this draft fire under tool "aside"?

    A known tool_hint wins (it becomes trigger.tools): aside -> likely, any other tool -> unlikely.
    Without one, the gotcha text (new retain + similar memory) is searched for aside clues.
    A display hint only - the caller never rejects or drops a draft on it."""
    if tool_hint:
        return ASIDE_LIKELY if "aside" in str(tool_hint).lower() else ASIDE_UNLIKELY
    text = "%s\n%s" % (new_content or "", (similar or {}).get("text", "") or "")
    low = str(text).lower()
    if _ASIDE_ASCII_RE.search(low) or any(c in low for c in ASIDE_CLUES_KO):
        return ASIDE_LIKELY
    return ASIDE_UNLIKELY


def build_draft(*, bank: str, signal: str, new_content: str, similar: Optional[Dict[str, Any]],
                rescue: Optional[RecallEvent], tool_hint: Optional[str] = None) -> Dict[str, Any]:
    draft_id = uuid.uuid4().hex[:12]
    keywords = match_keywords(new_content, similar) or ["<match keyword needed>"]
    if rescue is not None and rescue.reusable:
        query, source = scrub_secrets(rescue.query), "recall-log"
        recall_tags = list(rescue.tags)
    else:
        query, source = "", "needs-evidence"
        recall_tags = []
    trigger: Dict[str, Any] = {
        "id": f"draft-{draft_id}",
        "bank": bank,
        "tools": [tool_hint] if tool_hint else None,
        "all_of": [keywords],
        "recall": {"query": query},
        "max_memories": 2,
        "max_tokens": 500,
        "note": f"자동 초안 ({signal}, {now_iso()}). 승인 전 keywords/tools를 사람이 좁힐 것.",
    }
    if recall_tags:
        trigger["recall"]["tags"] = recall_tags
        trigger["recall"]["tags_match"] = rescue.tags_match
    if source == "recall-log":
        # The recall that found the memory ran with these arguments; a draft that drops them
        # replays with upstream's defaults and can lose that memory (#88). The ledger keeps what
        # was SENT, so an exact conversion ("16384" -> 16384, "High" -> "high") is carried; a
        # value that does not convert, is over the table's ceiling, or is null is left out -
        # never rounded or clamped - so the draft stays pasteable into the trigger table.
        for key, valid in (("budget", coerce_recall_budget(rescue.budget)),
                           ("max_tokens", coerce_recall_max_tokens(rescue.max_tokens))):
            if valid is not None:
                trigger["recall"][key] = valid
    return {
        "draft_id": draft_id,
        "created_at": now_iso(),
        "bank": bank,
        "signal": signal,
        "state": "ready-for-review" if source == "recall-log" else "needs-evidence",
        "aside_scope": aside_scope(new_content, similar, tool_hint),
        "trigger": trigger if source == "recall-log" else None,
        "evidence": {
            "similar_memory_id": (similar or {}).get("id"),
            "similar_score": round(rerank_score(similar), 4) if similar else None,
            "rescue_query_source": source,
            "rescue_recall_ts": now_iso(rescue.ts) if rescue else None,
            "rescue_result_hit": bool(rescue and similar and similar.get("id") in rescue.result_ids),
            "recall_scope": "session" if rescue and rescue.session_id else "identity-bank",
            # Same visible redaction the injection path applies: the draft file and the
            # Discord message are read by people, and a retain may quote a secret by mistake.
            "new_content_preview": scrub_secrets(" ".join(new_content.split()))[:160],
        },
        "approve": ("policies/local/context-triggers.json의 triggers 배열에 trigger 객체를 추가한다. "
                    "재기동 불요 - 다음 요청에서 파일 변경을 감지해 다시 읽는다(깨진 파일이면 직전 테이블 유지)."),
    }


class PendingTriggers:
    def __init__(self, path: Optional[str] = None):
        self.path = path or os.environ.get("HC_PENDING_TRIGGERS", DEFAULT_PENDING_FILE)

    def append(self, draft: Dict[str, Any]) -> bool:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with _LOCK:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(dump_line(draft, mark=False) + "\n")     # lone surrogate -> U+FFFD (#135)
            return True
        except OSError:
            return False


class Deduper:
    """One draft per (signal, anchor) per TTL - a re-run of the same session must not spam."""

    def __init__(self, ttl: float = DEDUP_TTL_SECONDS):
        self.ttl = ttl
        self._seen: Dict[str, float] = {}

    def first_time(self, key: str, *, now: Optional[float] = None) -> bool:
        t = time.time() if now is None else now
        with _LOCK:
            expired = [k for k, ts in self._seen.items() if t - ts > self.ttl]
            for k in expired:
                del self._seen[k]
            if key in self._seen:
                return False
            self._seen[key] = t
            return True

    def forget(self, key: str) -> None:
        with _LOCK:
            self._seen.pop(key, None)


# --- notify -----------------------------------------------------------------------------

DISCORD_MESSAGE_MAX = 1900


def discord_message(draft: Dict[str, Any]) -> str:
    ev = draft.get("evidence", {})
    trig = draft.get("trigger") or {}
    lines = [
        f"[hindsight-crew] 재발 신호 → 트리거 초안 #{draft.get('draft_id')} ({draft.get('signal')}, bank {draft.get('bank')})",
        f"- 유사 기억: id {str(ev.get('similar_memory_id') or '-')[:8]} (rerank {ev.get('similar_score')})",
        f"- 상태: {draft.get('state', 'ready-for-review')}",
        *([f"- {ASIDE_UNLIKELY_NOTE}"] if draft.get("aside_scope") == ASIDE_UNLIKELY else []),
        f"- 초안 쿼리 [{ev.get('rescue_query_source')}]: {trig.get('recall', {}).get('query', '')[:180]}",
        f"- 매치 키워드: {trig.get('all_of')}",
        f"- 새 기억: {ev.get('new_content_preview', '')[:120]}",
        ("- 승인: keywords/tools 검토 후 trigger 등록. 자동 등록 없음." if trig else
         "- 검증된 recall 쿼리가 없어 등록 불가. 관련 기억을 실제 조회한 후 다시 검토."),
    ]
    return scrub_secrets("\n".join(lines))[:DISCORD_MESSAGE_MAX]


async def _send_discord(build: Callable[[], str], client: Optional[httpx.AsyncClient],
                        *, no_mentions: bool = False) -> str:
    """'ok' | 'skipped:<why>' | 'failed:<class>'. Never raises. `build()` makes the message; it runs only once the
    credentials are present and inside the try, as the draft message always did."""
    bot = os.environ.get("HC_NOTIFY_DISCORD_BOT_TOKEN")
    channel = os.environ.get("HC_NOTIFY_DISCORD_CHANNEL_ID")
    if not bot or not channel:
        return "skipped:missing_env"
    own = client is None
    client = client or httpx.AsyncClient(timeout=15.0)
    try:
        resp = await client.post(
            f"https://discord.com/api/v10/channels/{channel}/messages",
            content=json.dumps({"content": build(), **({"allowed_mentions": {"parse": []}} if no_mentions else {})}
                               ).encode("utf-8"),
            headers={"Authorization": f"Bot {bot}", "Content-Type": "application/json",
                     # Discord 403s the default python user agent.
                     "User-Agent": "DiscordBot (hindsight-crew context-triggers, 1.0)"},
        )
        return "ok" if 200 <= resp.status_code < 300 else f"failed:status_{resp.status_code}"
    except Exception as exc:  # noqa: BLE001 - notification is best-effort by contract
        return f"failed:{type(exc).__name__}"
    finally:
        if own:
            await client.aclose()


async def post_discord_message(content: str, *, client: Optional[httpx.AsyncClient] = None) -> str:
    """Free-form message to the HC_NOTIFY_DISCORD_* channel (issue #303 webhook alerts): scrub_secrets, cut
    to DISCORD_MESSAGE_MAX, then the same sender as notify_discord (env, UA, never raises). The text carries
    upstream-supplied strings (bank, document id), so it can never ping anyone: allowed_mentions parse [].
    notify_discord's request body stays as it was."""
    return await _send_discord(lambda: scrub_secrets(content)[:DISCORD_MESSAGE_MAX], client, no_mentions=True)


async def notify_discord(draft: Dict[str, Any], *, client: Optional[httpx.AsyncClient] = None) -> str:
    """'ok' | 'skipped:<why>' | 'failed:<class>'. Never raises."""
    return await _send_discord(lambda: discord_message(draft), client)
