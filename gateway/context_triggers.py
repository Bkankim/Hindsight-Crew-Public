"""길목 주입 (issue #12, 1차) - trigger table + matcher + injection builder.

The failure class this closes is "the memory existed but nobody pulled it": every
Hindsight tool is pull-only, so a session that never recalls walks into a trap the bank
already describes. `POST /context-triggers/<bank>` is the push side. A choke-point shim
(aside CLI wrapper, Claude Code PreToolUse hook) posts the task text; the gateway matches
it against a declared trigger table, runs ONE live recall for the first matching row and
returns an injection block the caller prepends to the task. No match -> empty body, so a
non-matching call adds zero tokens to anybody's context.

Design decisions pinned here (see the issue for the reasoning):
  * The table stores recall QUERIES, never answer text. Every hit is a live recall, so a
    correction retain updates what gets injected without touching the table (self-healing).
  * Matching is a combination condition: tool axis ("tools") AND every "all_of" keyword
    group (each group = any-of, case-insensitive substring), minus "none_of". Loose single
    keywords are rejected at load (min 3 chars) - precision is the anti-pollution control.
  * Hygiene the gateway enforces, not the caller: at most "max_memories" (<= 3) memories,
    "max_tokens" (<= 500 estimated) per injection, every memory framed as
    "[hindsight 참고, YYYY-MM-DD 관측, id:xxxxxxxx]" and the block closed by FOOTER, which
    demotes the injection from an instruction to a prior ("현장이 기록과 다르면 현장이 우선").
  * Secret scrub on the way out (token / JWT / otpauth / base32-seed shapes). Memories are
    not supposed to hold secrets in the first place; this is defense in depth, so a
    redaction is visible ("[redacted]") rather than silent.
  * Fail-open toward the caller: an upstream recall failure yields an EMPTY injection (the
    task proceeds without the hint) and an audit line saying so. The gateway never turns a
    hint lookup into a blocked task.

Table file: policies/context-triggers.json (ro bind mount next to bank-policies.json;
env HC_CONTEXT_TRIGGERS). Absent = empty table + warning (CI pytest outside the
container), present-but-broken = fail-fast at startup, same posture as bank_policies.
Local overlay (issue #187): policies/local/context-triggers.json (gitignored; env
HC_CONTEXT_TRIGGERS_LOCAL overrides the derived path), same file shape. Its rows are
appended AFTER the tracked rows (first match wins, so tracked rows keep precedence); an id
present in both files is a load error. Both files are watched by the hot reload.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

_LOG = logging.getLogger("gateway.context_triggers")

DEFAULT_TRIGGER_FILE = "policies/context-triggers.json"
DECISION = "context-trigger"           # audit vocabulary (audit.py)

# Hard ceilings the gateway enforces regardless of what a row declares (rows above them are
# rejected at load, never clamped - P5 no-silent-correction).
MAX_TOKENS_CEILING = 500
MAX_MEMORIES_CEILING = 3
MIN_KEYWORD_CHARS = 3                  # ASCII keywords; non-ASCII (한글 등) may be 2 chars
MIN_KEYWORD_CHARS_NON_ASCII = 2
MAX_TEXT_CHARS = 20000                 # matcher looks at this much of the task text
RECALL_MAX_TOKENS = 4096               # upstream recall budget (>= the bank floor)
RECALL_TIMEOUT_SECONDS = 8.0           # retain-side gates: they run inside the retain request
# The trigger endpoint's callers give up at 6 s (CC hook `curl --max-time 6`, aside shim
# HC_SHIM_TIMEOUT default 6). Waiting longer than they do audits a "match" no client received,
# so the trigger path times out first and the miss is recorded as recall-failed (issue #268).
# It is both the httpx timeout and the wall-clock deadline of the whole recall (app.py); the
# 0.5 s left covers in-process work around it (table reload, match, ledger, audit line).
TRIGGER_RECALL_TIMEOUT_SECONDS = 5.5
# Upstream rejects a recall QUERY above recall_max_query_tokens (default 500, cl100k) with a
# 400 "Query too long" (hindsight_api/api/http.py). Every gateway-originated query is kept
# under QUERY_TOKEN_TARGET by query_token_bound(), leaving headroom below the hard 500.
UPSTREAM_QUERY_MAX_TOKENS = 500
QUERY_TOKEN_TARGET = 450

FOOTER = ("(이 기록은 그 시점의 관측이다. 현장이 기록과 다르면 현장이 우선이다. "
          "기록대로 실패하면 즉시 기록 불신으로 전환하고, 작업 종료 시 정정 retain을 남겨라.)")
FRAME = "[hindsight 참고, {date} 관측, id:{short_id}]"
TRUNCATED_MARK = " …(잘림 - 전문은 recall id:{short_id})"


def _min_row_max_tokens() -> int:
    return estimate_tokens(FOOTER) + estimate_tokens(FRAME.format(date="2026-01-01", short_id="xxxxxxxx")) + 64

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
_BANK_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")      # mirrors app.py _BANK_RE
_TAG_RE = re.compile(r"^[a-z][a-z0-9-]*:[a-z0-9-]+$")
_TAGS_MATCH = frozenset({"any", "all", "any_strict", "all_strict"})
_ROW_KEYS = frozenset({"id", "bank", "tools", "all_of", "none_of", "recall",
                       "max_memories", "max_tokens", "note"})
_RECALL_KEYS = frozenset({"query", "tags", "tags_match", "budget", "max_tokens"})
# recall.budget / recall.max_tokens are the arguments of the recall that found a memory (a
# draft copies them from the recall ledger, #88). Upstream's budget enum, and the token floor
# that keeps the top-ranked memory from not fitting and returning an empty result.
RECALL_BUDGETS = frozenset({"low", "mid", "high"})
# Ceiling for recall.max_tokens: a triggered recall runs on the request path of a task and
# above 8x the default (4096) its latency and token cost leave what a trigger is for (final-review
# C3, 2026-10-02). The table rejects above it, a draft leaves the key out - never clamps.
RECALL_MAX_TOKENS_CEILING = 32768
_TOP_KEYS = frozenset({"version", "triggers"})

# Visible redaction of secret-shaped substrings. Deliberately shape-based (no dictionary):
# gateway member tokens, JWTs, TOTP provisioning URIs, GitHub PATs, OpenAI-style keys, Slack
# and Cloudflare tokens, the Hindsight upstream key (hs_ + hex), and base32 runs (TOTP seeds
# are base32).
# ASCII-only token boundaries. Python's \b is Unicode-aware and counts Hangul as a word
# char, so "JBSWY3DPEHPK3PXP로" or "ghp_…를" had NO boundary and leaked (review 2nd pass):
# Korean particles attach directly to the token.
_NB = r"(?<![A-Za-z0-9])"
_NA = r"(?![A-Za-z0-9])"
_SECRET_SHAPES = (
    re.compile(r"mt_[A-Za-z0-9]{10,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{5,}(?:\.[A-Za-z0-9_-]+)?"),
    re.compile(r"otpauth://\S+"),
    re.compile(_NB + r"gh[pousr]_[A-Za-z0-9]{20,}" + _NA),
    re.compile(_NB + r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(_NB + r"xox[abposre]-[A-Za-z0-9-]{10,}"),
    re.compile(_NB + r"cf[a-z]{1,3}_[A-Za-z0-9_-]{20,}"),
    # This project's own upstream key (scripts/seed-demo.sh: "hs_" + openssl rand -hex 24, issue
    # #458). Hex only and 16+ chars, so short code names (hs_get, hs_call) stay; no trailing
    # boundary, so a key glued to more letters still loses its hex run. Besides the ASCII
    # boundary it also starts right after a URL-encoded "=" (%3D/%3d) or a literal string escape
    # (backslash + letter, e.g. \n \t in serialized text), where the char before "hs_" is a
    # letter; a word that merely contains "hs_" (graphs_deadbeef...) still stays.
    re.compile(r"(?:" + _NB + r"|(?<=%3[Dd])|(?<=\\[A-Za-z]))hs_[0-9A-Fa-f]{16,}"),
)
# Base32 seeds come in any case and are often shown in 4-char groups ("jbsw y3dp ehpk 3pxp").
# The old uppercase-only run missed both (review P2-1, 2026-09-23). Case-insensitive runs would
# also hit long plain words ("internationalization"), so a lowercase/mixed-case candidate must
# carry a base32 digit 2-7 - a random 16-char seed lacks one with probability (26/32)^16 = 3.6%.
# An uppercase group with no digit ("HJKM NPQR STUV WXYZ", issue #270) is erased only when some
# 4-char group has no vowel (A E I O U Y): English 4-letter words almost always have one, so
# "THIS THAT WITH FROM" survives. A seed slips through only if every group has a vowel - about
# 0.6% for 16 chars (3.6% no digit x ~18% all four groups voweled), ~0.004% for 32. Accepted
# residual false positive: 4+ consecutive digitless uppercase 4-char groups with even one
# vowelless group are erased ("HTTP DATA FROM THIS", "HTTP JSON YAML TOML").
_BASE32_RUN = re.compile(_NB + r"[A-Za-z2-7]{16,}" + _NA)
_BASE32_GROUPS = re.compile(_NB + r"[A-Za-z2-7]{4}(?:[ -][A-Za-z2-7]{4}){3,}" + _NA)
_BASE32_DIGIT = re.compile(r"[2-7]")
_BASE32_VOWEL = re.compile(r"[AEIOUY]")


def _redact_base32(m: "re.Match[str]") -> str:
    s = m.group(0)
    compact = s.replace(" ", "").replace("-", "")
    if compact.isupper() and " " not in s and "-" not in s:
        return "[redacted]"               # original shape: prose has no 16+ char [A-Z2-7] words
    # A seed is single-case; camelCase identifiers ("passwordResetToken2", "Qwen3VLEmbedding")
    # are not (review 2nd pass false positives).
    if not (compact.isupper() or compact.islower()):
        return s
    if _BASE32_DIGIT.search(compact):
        return "[redacted]"
    if compact.isupper() and any(not _BASE32_VOWEL.search(compact[i:i + 4]) for i in range(0, len(compact), 4)):
        return "[redacted]"               # grouped uppercase, no digit: a vowelless group is not prose
    return s


class TriggerTableError(ValueError):
    """Malformed trigger declaration. Always raised - never degraded to a default."""


@dataclass(frozen=True)
class Trigger:
    id: str
    bank: str
    tools: Optional[Tuple[str, ...]]           # None = any tool
    all_of: Tuple[Tuple[str, ...], ...]         # every group must hit (group = any-of)
    none_of: Tuple[str, ...]
    query: str
    tags: Tuple[str, ...]
    tags_match: Optional[str]
    max_memories: int = 2
    max_tokens: int = MAX_TOKENS_CEILING
    note: Optional[str] = None
    # Upstream recall arguments (recall block). Not the row's `max_tokens`, which caps the
    # injection text. None = the row did not declare it: upstream_recall's default applies.
    recall_budget: Optional[str] = None
    recall_max_tokens: Optional[int] = None

    def matches(self, *, bank: str, tool: Optional[str], text: str) -> bool:
        if bank != self.bank:
            return False
        if self.tools is not None and (tool or "").strip().lower() not in self.tools:
            return False
        low = (text or "")[:MAX_TEXT_CHARS].lower()
        if any(kw in low for kw in self.none_of):
            return False
        return all(any(kw in low for kw in group) for group in self.all_of)


# --- validators ---------------------------------------------------------------------

def _expect(cond: bool, where: str, msg: str) -> None:
    if not cond:
        raise TriggerTableError(f"{where}: {msg}")


def _keyword_ok(kw: Any) -> bool:
    if not isinstance(kw, str):
        return False
    s = kw.strip()
    if any(ord(ch) > 127 for ch in s):
        return len(s) >= MIN_KEYWORD_CHARS_NON_ASCII
    return len(s) >= MIN_KEYWORD_CHARS


def _as_keyword_list(value: Any, where: str) -> Tuple[str, ...]:
    _expect(isinstance(value, list) and bool(value), where, "expected a non-empty array of strings")
    out: List[str] = []
    for i, kw in enumerate(value):
        _expect(_keyword_ok(kw), f"{where}[{i}]",
                f"keyword must be a string of at least {MIN_KEYWORD_CHARS} chars "
                f"({MIN_KEYWORD_CHARS_NON_ASCII} for non-ASCII) - loose keywords are exactly "
                f"the pollution this table must not cause, got {kw!r}")
        out.append(kw.strip().lower())
    return tuple(out)


def _as_int(value: Any, where: str, lo: int, hi: int) -> int:
    _expect(isinstance(value, int) and not isinstance(value, bool) and lo <= value <= hi,
            where, f"expected an integer in [{lo}, {hi}], got {value!r}")
    return value


def recall_budget_arg(value: Any) -> Optional[str]:
    """`value` when it is a recall budget the table accepts, else None."""
    return value if isinstance(value, str) and value in RECALL_BUDGETS else None


def recall_max_tokens_arg(value: Any) -> Optional[int]:
    """`value` when it is a recall max_tokens the table accepts, else None (bool is not a count)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if RECALL_MAX_TOKENS <= value <= RECALL_MAX_TOKENS_CEILING else None


def coerce_recall_budget(value: Any) -> Optional[str]:
    """A ledger budget in the form the table accepts: lower-cased, or None when it is not one.

    The ledger keeps the string as it was SENT; this is the only normalisation a draft applies
    (no strip, no aliases) so a value that is not unambiguously low|mid|high stays out."""
    return recall_budget_arg(value.lower()) if isinstance(value, str) else None


def coerce_recall_max_tokens(value: Any) -> Optional[int]:
    """A ledger max_tokens as the int the table accepts, or None.

    The ledger keeps the type that was SENT, and the MCP floor rule lets non-numeric values
    through, so "16384" and 16384.0 are real ledger values. Only an exact conversion counts:
    a plain ASCII-digit string or a finite whole float. Anything else (fractions, signs,
    padding, nan, bool) is left out, never rounded. The result still passes recall_max_tokens_arg."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = int(value) if re.fullmatch(r"[0-9]{1,16}", value) else None
    elif isinstance(value, float):
        value = int(value) if math.isfinite(value) and value.is_integer() else None
    return recall_max_tokens_arg(value)


def _parse_row(raw: Any, where: str) -> Trigger:
    _expect(isinstance(raw, dict), where, f"expected an object, got {type(raw).__name__}")
    unknown = sorted(set(raw) - _ROW_KEYS)
    _expect(not unknown, where, f"unknown key(s) {unknown} - known keys are {sorted(_ROW_KEYS)}")
    for key in ("id", "bank", "all_of", "recall"):
        _expect(key in raw, where, f"missing required key {key!r}")
    trig_id = raw["id"]
    _expect(isinstance(trig_id, str) and bool(_ID_RE.match(trig_id)), f"{where}.id",
            f"must match {_ID_RE.pattern}, got {trig_id!r}")
    bank = raw["bank"]
    _expect(isinstance(bank, str) and bool(_BANK_RE.match(bank)), f"{where}.bank",
            f"invalid bank name {bank!r}")
    tools: Optional[Tuple[str, ...]] = None
    if raw.get("tools") is not None:
        t = raw["tools"]
        _expect(isinstance(t, list) and bool(t) and all(isinstance(x, str) and x.strip() for x in t),
                f"{where}.tools", "expected null or a non-empty array of tool names")
        tools = tuple(x.strip().lower() for x in t)   # "Aside " == "aside" (review P3)
    groups = raw["all_of"]
    _expect(isinstance(groups, list) and bool(groups), f"{where}.all_of",
            "expected a non-empty array of keyword groups")
    all_of = tuple(_as_keyword_list(g, f"{where}.all_of[{i}]") for i, g in enumerate(groups))
    none_of: Tuple[str, ...] = ()
    if raw.get("none_of"):
        _expect(isinstance(raw["none_of"], list), f"{where}.none_of", "expected an array of strings")
        none_of = _as_keyword_list(raw["none_of"], f"{where}.none_of")
    rec = raw["recall"]
    _expect(isinstance(rec, dict), f"{where}.recall", "expected an object")
    unknown = sorted(set(rec) - _RECALL_KEYS)
    _expect(not unknown, f"{where}.recall", f"unknown key(s) {unknown}")
    query = rec.get("query")
    _expect(isinstance(query, str) and 8 <= len(query.strip()) <= 500, f"{where}.recall.query",
            "expected a query string of 8..500 chars (the table stores QUERIES, not answers)")
    _expect(query_token_bound(query.strip()) <= QUERY_TOKEN_TARGET, f"{where}.recall.query",
            f"query may exceed upstream's {UPSTREAM_QUERY_MAX_TOKENS}-token recall query limit "
            f"(estimate {query_token_bound(query.strip()):.0f} > {QUERY_TOKEN_TARGET}) - "
            f"upstream would answer 400 and the trigger would never inject")
    tags: Tuple[str, ...] = ()
    if rec.get("tags") is not None:
        _expect(isinstance(rec["tags"], list), f"{where}.recall.tags", "expected an array of tags")
        for i, tag in enumerate(rec["tags"]):
            _expect(isinstance(tag, str) and bool(_TAG_RE.match(tag)), f"{where}.recall.tags[{i}]",
                    f"tag must be <prefix>:<slug>, got {tag!r}")
        tags = tuple(rec["tags"])
    tags_match = rec.get("tags_match")
    if tags_match is not None:
        _expect(tags_match in _TAGS_MATCH, f"{where}.recall.tags_match",
                f"expected one of {sorted(_TAGS_MATCH)}, got {tags_match!r}")
    # null = "not declared", like tags / tags_match above (final-review C6)
    recall_budget: Optional[str] = rec.get("budget")
    if recall_budget is not None:
        _expect(recall_budget_arg(recall_budget) is not None, f"{where}.recall.budget",
                f"expected one of {sorted(RECALL_BUDGETS)}, got {recall_budget!r}")
    recall_max_tokens: Optional[int] = rec.get("max_tokens")
    if recall_max_tokens is not None:
        _expect(recall_max_tokens_arg(recall_max_tokens) is not None, f"{where}.recall.max_tokens",
                f"expected an integer in [{RECALL_MAX_TOKENS}, {RECALL_MAX_TOKENS_CEILING}] (a smaller "
                f"budget can leave the top memory unable to fit, which returns an empty result), "
                f"got {recall_max_tokens!r}")
    max_memories = _as_int(raw.get("max_memories", 2), f"{where}.max_memories", 1, MAX_MEMORIES_CEILING)
    # Floor = the footer plus room for one framed memory line. The old floor (50) was below
    # the footer's own estimate, so a legal row could never serve a memory (review P3).
    max_tokens = _as_int(raw.get("max_tokens", MAX_TOKENS_CEILING), f"{where}.max_tokens",
                         _min_row_max_tokens(), MAX_TOKENS_CEILING)
    note = raw.get("note")
    _expect(note is None or isinstance(note, str), f"{where}.note", "expected a string or null")
    return Trigger(id=trig_id, bank=bank, tools=tools, all_of=all_of, none_of=none_of,
                   query=query.strip(), tags=tags, tags_match=tags_match,
                   max_memories=max_memories, max_tokens=max_tokens, note=note,
                   recall_budget=recall_budget, recall_max_tokens=recall_max_tokens)


class TriggerTable:
    """Ordered trigger rows; first match wins on the request path."""

    def __init__(self, rows: Sequence[Trigger] = ()):
        self._rows: Tuple[Trigger, ...] = tuple(rows)
        self._source: Optional[str] = None      # tracked file this table came from (hot reload)
        self._local: Optional[str] = None       # local overlay file (issue #187); None = not watched
        self._mtimes: Tuple[Optional[int], ...] = ()   # per watched file at load; None = absent

    def _watched(self) -> Tuple[str, ...]:
        return (self._source,) if self._local is None else (self._source, self._local)

    def refreshed(self) -> "TriggerTable":
        """This table, or a fresh one when a source file (tracked or local) changed since load.

        Approving a trigger used to need a gateway restart, which drops every runtime's MCP
        session (review P3, 2026-09-23). ./policies is a directory bind mount, so a host edit
        is visible here; one stat per watched file per request decides. A broken edit keeps
        serving the last good table and logs why - startup stays fail-fast, a live edit must
        not take the endpoint down. The broken mtimes are remembered so they are not re-parsed
        per request. A tracked file that vanished keeps the table (an editor's delete-then-
        write gap); a local file that vanished is a change, since absence is legitimate."""
        if not self._source:
            return self
        now = tuple(_mtime_or_none(p) for p in self._watched())
        if now[0] is None and self._mtimes[0] is not None:
            return self
        if now == self._mtimes:
            return self
        try:
            return TriggerTable._read(self._source, self._local, tracked_required=now[0] is not None)
        except FileNotFoundError:
            # The tracked file was stat-ed but vanished before open (delete-then-write): keep
            # the last good table and retry next request, as the single-file reload did.
            return self
        except Exception as exc:  # noqa: BLE001 - a live edit must never take the endpoint down
            _LOG.warning("context trigger table %s changed but is invalid; keeping the last "
                         "good table: %s", " + ".join(self._watched()), exc)
            self._mtimes = now
            return self

    def __len__(self) -> int:
        return len(self._rows)

    @property
    def rows(self) -> Tuple[Trigger, ...]:
        return self._rows

    def match(self, *, bank: str, tool: Optional[str], text: str) -> List[Trigger]:
        return [r for r in self._rows if r.matches(bank=bank, tool=tool, text=text)]

    @classmethod
    def empty(cls) -> "TriggerTable":
        return cls(())

    @classmethod
    def from_dict(cls, data: Any, *, source: str = "<dict>") -> "TriggerTable":
        return cls(_check_unique_ids(_parse_rows(data, source), source))

    @classmethod
    def from_file(cls, path: str) -> "TriggerTable":
        """The tracked file alone (no local overlay), hot-reloaded on change. Must exist."""
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        return cls._read(path, None)

    @classmethod
    def from_files(cls, path: str, local_path: Optional[str] = None) -> "TriggerTable":
        """Tracked file + local overlay (issue #187). A missing file of either kind reads as
        no rows; `local_path` None derives policies/local/<name> (env override first)."""
        explicit = local_path or os.environ.get("HC_CONTEXT_TRIGGERS_LOCAL")
        local_path = explicit or local_overlay_path(path)
        if explicit and not os.path.exists(local_path):
            _LOG.warning("context trigger local overlay not found at %s (set explicitly) - "
                         "serving the tracked rows; a file created there later is picked up",
                         local_path)
        return cls._read(path, local_path)

    @classmethod
    def _read(cls, path: str, local_path: Optional[str],
              tracked_required: bool = False) -> "TriggerTable":
        rows: List[Trigger] = []
        mtimes: List[Optional[int]] = []
        for p in (path,) if local_path is None else (path, local_path):
            data, mtime = _read_table_file(p)
            if data is None and p == path and tracked_required:
                raise FileNotFoundError(path)
            mtimes.append(mtime)
            if data is not None:
                rows.extend(_parse_rows(data, p))
        where = path if local_path is None else f"{path} + {local_path}"
        table = cls(_check_unique_ids(rows, where))
        table._source, table._local, table._mtimes = path, local_path, tuple(mtimes)
        return table

    @classmethod
    def load(cls, path: Optional[str] = None, local_path: Optional[str] = None) -> "TriggerTable":
        path = path or os.environ.get("HC_CONTEXT_TRIGGERS", DEFAULT_TRIGGER_FILE)
        if not os.path.exists(path):
            _LOG.warning("context trigger table not found at %s - /context-triggers answers "
                         "empty for every text unless a local overlay declares rows", path)
        # A file created later (tracked or local) is picked up by refreshed().
        return cls.from_files(path, local_path)


def local_overlay_path(path: str) -> str:
    """`<dir>/local/<name>` for the tracked table `<dir>/<name>` (same rule as bank_policies)."""
    return os.path.join(os.path.dirname(path), "local", os.path.basename(path))


def _mtime_or_none(path: str) -> Optional[int]:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def _read_table_file(path: str) -> Tuple[Any, Optional[int]]:
    """(parsed JSON, mtime) or (None, None) when the file does not exist."""
    try:
        fh = open(path, "rb")
    except FileNotFoundError:
        return None, None
    with fh:
        # mtime BEFORE the read: a save landing mid-read then leaves the NEW mtime unseen,
        # so the next request reloads (stat-after-read pinned stale content - review 2nd pass).
        mtime = os.fstat(fh.fileno()).st_mtime_ns
        raw = fh.read()
    try:
        return json.loads(raw.decode("utf-8")), mtime
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        # ValueError covers JSONDecodeError and int-digit limits; a CP949 save or a
        # half-written multibyte char is a table error, not a crash.
        raise TriggerTableError(f"{path}: invalid JSON ({type(exc).__name__}: {exc})") from exc


def _parse_rows(data: Any, source: str) -> List[Trigger]:
    _expect(isinstance(data, dict), source,
            f"expected an object at the top level, got {type(data).__name__}")
    unknown = sorted(set(data) - _TOP_KEYS)
    _expect(not unknown, source, f"unknown top-level key(s) {unknown}")
    _expect(data.get("version") == 1, source,
            f"unsupported version {data.get('version')!r} (expected 1)")
    rows_raw = data.get("triggers", [])
    _expect(isinstance(rows_raw, list), source, "'triggers' must be an array")
    return [_parse_row(r, f"{source}: triggers[{i}]") for i, r in enumerate(rows_raw)]


def _check_unique_ids(rows: List[Trigger], source: str) -> List[Trigger]:
    ids = [r.id for r in rows]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    _expect(not dupes, source, f"duplicate trigger id(s) {dupes}")
    return rows


# --- injection builder ---------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    """Tokenizer-free estimate that upper-bounds cl100k on REAL memory text (not on adversarial
    input: random ASCII measured 1.05x, rare-syllable-only Hangul 2.2x) for injection budgeting: Hangul
    syllable 1.3, ASCII 0.7, other non-ASCII its UTF-8 byte count.

    The old weights (non-ASCII 1, ASCII 1/4) undercounted 659 of 673 real 1,500-char chunks,
    up to 1.64x - an injection "estimated 496" measured 617 real tokens (review P3,
    2026-09-23). These weights left 0 of 673 chunks over (max real/estimate 0.971)."""
    if not text:
        return 0
    total = 0.0
    for ch in text:
        o = ord(ch)
        if o < 128:
            total += 0.7
        elif 0xAC00 <= o <= 0xD7A3:
            total += 1.3
        else:
            total += len(ch.encode("utf-8", "surrogatepass"))     # lone surrogate = 3 bytes (issue #138)
    return math.ceil(total)


def query_token_bound(text: str, hangul: float = 2.0) -> float:
    """Upper-bound estimate of upstream's cl100k count for a recall query (no tokenizer in
    this image). ASCII = 1 per char and other non-Hangul chars = their UTF-8 byte count are
    hard bounds (a byte-level BPE token covers >= 1 byte). Hangul syllables count 2.0: the
    measured maximum over 3,649 real 300-char Korean chunks was 1.09 tokens/char (p99 1.02,
    2026-09-23), but a run of rare syllables reached 1.36 (뷁 alone ~2.8), and 1.5 let such a
    run through at 510 real tokens. Text made mostly of the rarest syllables can still exceed
    2.0 (measured 2.45); hangul=3.0 is a hard bound (a syllable is 3 UTF-8 bytes) and is what
    upstream_recall falls back to when upstream still answers "Query too long"."""
    total = 0.0
    for ch in text:
        o = ord(ch)
        if o < 128:
            total += 1
        elif 0xAC00 <= o <= 0xD7A3:
            total += hangul
        else:
            total += len(ch.encode("utf-8", "surrogatepass"))      # a lone surrogate counts 3 bytes (#135)
    return total


def clip_query(text: str, budget: float = QUERY_TOKEN_TARGET, hangul: float = 2.0) -> str:
    """Whitespace-normalized longest prefix within `budget` (query_token_bound), cut back to
    a word boundary when that keeps most of it. Retain content leads with its label and
    subject ("[라벨] 무엇을 - 결과"), so the prefix carries what similarity needs."""
    flat = " ".join((text or "").split())
    if query_token_bound(flat, hangul) <= budget:
        return flat
    lo, hi = 0, len(flat)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if query_token_bound(flat[:mid], hangul) <= budget:
            lo = mid
        else:
            hi = mid - 1
    cut = flat[:lo]
    space = cut.rfind(" ")
    if space >= int(lo * 0.7):
        cut = cut[:space]
    return cut.rstrip()


def scrub_secrets(text: str) -> str:
    out = text
    for pat in _SECRET_SHAPES:
        out = pat.sub("[redacted]", out)
    out = _BASE32_GROUPS.sub(_redact_base32, out)
    return _BASE32_RUN.sub(_redact_base32, out)


def _observed_date(memory: Dict[str, Any]) -> str:
    for key in ("occurred_start", "mentioned_at", "created_at", "timestamp"):
        value = memory.get(key)
        if isinstance(value, str) and len(value) >= 10:
            return value[:10]
    return "날짜 미상"


def _truncate_to_budget(text: str, budget: int) -> str:
    """Longest prefix whose estimate fits the budget (binary search on char count)."""
    if estimate_tokens(text) <= budget:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid]) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip()


_LABEL_PREFIX_RE = re.compile(r"^\s*\[[^\]]{1,24}\]\s*")
_DUP_PREFIX_CHARS = 60


def _dedup_key(body: str) -> str:
    """Upstream extracts several near-identical units from one retain (measured 2026-09-21:
    a world fact plus an observation paraphrase differing by one punctuation mark). Serving
    both spends the budget twice on one memory, so units whose first 60 chars (punctuation
    stripped) coincide are treated as duplicates - rank order keeps the first.

    The bracket label is part of the key: a "[정정] X ..." that opens like the older
    "[gotcha] X ..." it corrects is a different memory, and dropping it behind the stale one
    broke self-healing (review P3, 2026-09-23). Both units of one retain carry its label."""
    m = _LABEL_PREFIX_RE.match(body)
    label = re.sub(r"\s+", "", m.group(0).lower()) if m else ""
    stripped = body[m.end():] if m else body
    return label + re.sub(r"[\s\W_]+", "", stripped.lower())[:_DUP_PREFIX_CHARS]


def build_injection(memories: Sequence[Dict[str, Any]], *, max_memories: int,
                    max_tokens: int) -> Tuple[str, List[str], int]:
    """(injection text, memory ids used, estimated tokens). Empty text when nothing usable.

    Greedy in rank order: a memory is added whole when it fits; the FIRST memory is always
    represented (truncated to the budget if it alone exceeds it); later memories that would
    not fit whole are included truncated only when a meaningful remainder (>= 120 tokens) is
    left, otherwise skipped. The footer is reserved up front so the cap holds end to end.
    """
    cap = min(int(max_tokens), MAX_TOKENS_CEILING)
    limit = max(1, min(int(max_memories), MAX_MEMORIES_CEILING))
    budget = cap - estimate_tokens(FOOTER)
    parts: List[str] = []
    used: List[str] = []
    seen_keys: List[str] = []
    for memory in memories:
        if len(used) >= limit:
            break
        if not isinstance(memory, dict):
            continue
        mem_id = memory.get("id")
        text = memory.get("text")
        if not isinstance(mem_id, str) or not isinstance(text, str) or not text.strip():
            continue
        short_id = mem_id[:8]
        frame = FRAME.format(date=_observed_date(memory), short_id=short_id)
        body = scrub_secrets(" ".join(text.split()))
        key = _dedup_key(body)
        if key and key in seen_keys:
            continue                      # near-duplicate of an already served memory
        seen_keys.append(key)
        need = estimate_tokens(frame) + estimate_tokens(body) + 1
        if need <= budget:
            parts.append(frame + "\n" + body)
            used.append(mem_id)
            budget -= need
            continue
        mark = TRUNCATED_MARK.format(short_id=short_id)
        room = budget - estimate_tokens(frame) - estimate_tokens(mark) - 1
        if (not used or room >= 120) and room > 0:
            parts.append(frame + "\n" + _truncate_to_budget(body, room) + mark)
            used.append(mem_id)
        break
    if not parts:
        return "", [], 0
    injection = "\n".join(parts) + "\n" + FOOTER
    return injection, used, estimate_tokens(injection)


# --- upstream recall (shared by the endpoint and the retain-side gates) ---------------

async def upstream_recall(client: httpx.AsyncClient, *, upstream_key: str, bank: str,
                          query: str, tags: Sequence[str] = (), tags_match: Optional[str] = None,
                          max_tokens: int = RECALL_MAX_TOKENS, budget: Optional[str] = None,
                          timeout: float = RECALL_TIMEOUT_SECONDS,
                          diag: Optional[Dict[str, Any]] = None) -> Optional[List[Dict[str, Any]]]:
    """One live recall against upstream's REST surface. None = failure (caller fails open).

    On failure `diag["error"]` (when a dict is passed) names the cause for the audit line:
    "timeout" | "transport" | "http-<status>" | "bad-json" | "bad-shape". A code only - the
    upstream message can echo the query, and audit lines never carry text (issue #268)."""
    def fail(code: str) -> None:
        if diag is not None:
            diag["error"] = code
        return None

    body: Dict[str, Any] = {"query": query, "max_tokens": max_tokens}
    if budget is not None:                  # absent key = upstream's own default, as before #88
        body["budget"] = budget
    if tags:
        body["tags"] = list(tags)
        body["tags_match"] = tags_match or "any"
    headers = {"content-type": "application/json", "accept": "application/json"}
    if upstream_key:
        headers["authorization"] = f"Bearer {upstream_key}"
    try:
        resp = await client.post(f"/v1/default/banks/{bank}/memories/recall",
                                 content=json.dumps(body).encode("utf-8"),
                                 headers=headers, timeout=timeout)
        if resp.status_code == 400 and "query too long" in resp.text.lower():
            # Callers pre-clip with the 2.0 Hangul estimate; only rare-syllable text gets here.
            # One retry with the hard bound, which cannot exceed the limit.
            short = clip_query(query, UPSTREAM_QUERY_MAX_TOKENS, hangul=3.0)
            if short and short != query:
                body["query"] = short
                resp = await client.post(f"/v1/default/banks/{bank}/memories/recall",
                                         content=json.dumps(body).encode("utf-8"),
                                         headers=headers, timeout=timeout)
    except httpx.TimeoutException:
        return fail("timeout")
    except (httpx.HTTPError, OSError):
        return fail("transport")
    if resp.status_code != 200:
        return fail(f"http-{resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        return fail("bad-json")
    results = data.get("results") if isinstance(data, dict) else data
    if not isinstance(results, list):
        return fail("bad-shape")
    return [r for r in results if isinstance(r, dict)]


def rerank_score(memory: Dict[str, Any]) -> float:
    scores = memory.get("scores")
    if isinstance(scores, dict):
        for key in ("reranker", "final"):
            value = scores.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
    return 0.0
