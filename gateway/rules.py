"""Request-side rule enforcement (N2) — pure, stdlib-only, surface-agnostic.

The gateway is the only hard enforcement point for retain FORMAT rules: the MCP spec
puts no schema-validation duty on clients, so anything the client "promises" is
advisory. What each bank requires is declared in `policies/bank-policies.json`
(gateway.bank_policies); this module APPLIES that declaration.

Unit of work is a single ITEM dict, never a request:
  * MCP `retain`/`sync_retain` take FLAT arguments (content/tags/timestamp/metadata/
    strategy/... — `additionalProperties:false`), so the MCP surface applies this once
    per tools/call element.
  * REST `POST .../memories` takes `{items:[MemoryItem], ...}` with tags/timestamp at
    the ITEM top level, so N3 applies the very same function once per `items[i]`.
Keeping the unit at "one item dict" is what lets both surfaces enforce identical rules
(P1) instead of drifting apart (fixtures/mcp/NOTES.md ①②).

Two shapes of outcome, both carried by ItemVerdict:
  * CORRECTION (timestamp autofill) — a new dict is returned with the value filled in.
    The caller forwards it upstream and annotates the success response (P5, wired in
    N5); `"unset"` is upstream's advertised no-timestamp sentinel and is treated as
    absent on purpose, otherwise it would be a silent bypass of the very incident this
    enforcement exists for (D1-①). A bank that genuinely wants timeless writes declares
    `timestamp_autofill:false`.
  * REJECTION (rule violations) — the caller must NOT forward, and must answer on the
    isError channel with the violation text (P2). Every message is written FOR A MODEL
    TO SELF-CORRECT: `RULE:<id>` + what is wrong + at least one valid example + the
    corrective basis (allowed prefixes, the pattern, the allowed strategies).

`now_iso` is injected by the caller (never read from the clock here) so the function
stays pure and tests are deterministic; gateway callers use `utc_now_iso()`.
"""
from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from . import retain_item_key
from .bank_policies import BankPolicy
from .policy import RETAIN_TOOLS  # single source of "what carries retained content"

__all__ = [
    "RETAIN_TOOLS", "UNSET_TIMESTAMP", "Violation", "ItemVerdict", "TagVerdict",
    "evaluate_tags", "evaluate_item", "evaluate_tool_call", "utc_now_iso", "instructions_for",
]

# upstream's advertised "store without a timestamp" sentinel (v0.8.3 실측:
# MemoryItem.timestamp description + 422 본문 양쪽이 광고. fixtures/rest/memories_post_fail.json).
UNSET_TIMESTAMP = "unset"

# Rule ids — the model-visible contract. Keep stable; they are quoted in docs/gates.
RULE_TAGS_REQUIRED = "RULE:tags-required"
RULE_TAGS_GRAMMAR = "RULE:tags-grammar"
RULE_TAGS_VOCABULARY = "RULE:tags-vocabulary"          # value outside a closed list (#39)
RULE_TAGS_MISSING_PREFIX = "RULE:tags-missing-prefix"  # a required prefix is absent (#39)
RULE_CONTENT_PREFIX = "RULE:content-prefix"
RULE_STRATEGY_UNKNOWN = "RULE:strategy-unknown"
RULE_RECALL_MAX_TOKENS_FLOOR = "RULE:recall-max-tokens-floor"
RULE_ARGUMENTS_NOT_OBJECT = "RULE:arguments-not-object"  # tools/call arguments is not a JSON object (#61)
RULE_METADATA_NOT_OBJECT = "RULE:metadata-not-object"    # retain-family metadata is not a JSON object (#70)
RULE_PARAMS_NOT_OBJECT = "RULE:params-not-object"        # tools/call params is not a JSON object (#70)
RULE_REQUEST_NOT_OBJECT = "RULE:request-not-object"      # JSON-RPC body/batch element is not an object (#70)
RULE_METHOD_NOT_STRING = "RULE:method-not-string"        # JSON-RPC method is not a string (#70)
RULE_TOOL_NAME_NOT_STRING = "RULE:tool-name-not-string"  # tools/call params.name is not a string (#70)
RULE_ITEM_KEY_REQUIRED = retain_item_key.RULE_ID       # S2: handoff/todo/completion with no item key (#364)

# Tag grammar: <declared prefix>:<lowercase slug>. The prefix set is per-bank.
_TAG_SLUG = r"[a-z0-9-]+"

# Illustrative values used to BUILD examples out of the bank's own declared prefixes,
# so the guidance a model reads is always valid for the bank it just hit.
_PREFIX_SAMPLES = {
    "project": "hindsight-crew",
    "kind": "decision",
    "topic": "mcp-gateway",
    "user": "demo",
}
# Candidate contents for content_prefix_pattern guidance; the first one that actually
# MATCHES the bank's pattern is quoted (never advertise an example that would be
# rejected — that would teach the model a wrong retry).
TIMESTAMP_HINT = (
    '(gateway note: no usable "timestamp" was supplied. Nothing was stored, so on a '
    "successful retry the gateway will record {now} unless you send an explicit ISO8601 "
    '"timestamp".)'
)
_CONTENT_EXAMPLES = (
    "[decision] 게이트웨이가 retain 규칙을 서버측에서 강제하도록 결정",
    "[note] 짧은 라벨 뒤 공백 하나, 그리고 본문",
    "note: 라벨 없는 본문",
)


@dataclass(frozen=True)
class Violation:
    """One broken rule. `message` is what the model receives verbatim."""

    rule: str
    message: str


@dataclass(frozen=True)
class ItemVerdict:
    """Result of applying a bank's rules to ONE item.

    `item` is the (possibly corrected) item to forward — a NEW dict whenever the source
    was a dict, so callers can never accidentally mutate the client's parsed body.
    `autofilled_timestamp` is the injected value, or None when nothing was corrected.
    """

    item: Any
    violations: Tuple[Violation, ...] = ()
    autofilled_timestamp: Optional[str] = None
    normalized_tags: Tuple[Tuple[str, str], ...] = ()   # (as sent, as forwarded)
    warnings: Tuple[str, ...] = ()                       # non-blocking tag hints (#40)

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def rule_ids(self) -> Tuple[str, ...]:
        return tuple(v.rule for v in self.violations)

    @property
    def message(self) -> str:
        """All violations as one model-facing block (one rule per line).

        When the same item ALSO lacked a usable timestamp, the corrective basis (what
        the gateway would record on a successful retry) is appended: a rejected call
        stores nothing, so the model must know the instant it is being offered.
        """
        lines = [v.message for v in self.violations]
        if lines and self.autofilled_timestamp:
            lines.append(TIMESTAMP_HINT.format(now=self.autofilled_timestamp))
        return "\n".join(lines)


@dataclass(frozen=True)
class TagVerdict:
    """Result of the tag-only evaluation (normalization + tag check) of ONE tags value.

    `tags` is what would be forwarded: a new normalized list when a rewrite happened,
    otherwise the caller's own object untouched (list, tuple or anything else - a
    violation then explains a non-array). Copy it before mutating. `normalized_tags` is
    the public rewrite channel, (as sent, as forwarded). `violations` are the tag rules
    only. `warnings` is the channel for non-blocking hints (#40: ambiguous project candidates,
    a new project name close to an existing one); a warning never blocks the retain.
    """

    tags: Any
    normalized_tags: Tuple[Tuple[str, str], ...] = ()
    violations: Tuple[Violation, ...] = ()
    warnings: Tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.violations


def utc_now_iso() -> str:
    """UTC ISO8601 seconds precision — same shape as the audit log's `ts`."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_unset_timestamp(value: Any) -> bool:
    """True when the value carries no usable instant (absent/null/blank/`"unset"`)."""
    if value is None:
        return True
    if isinstance(value, str):
        stripped = value.strip()
        return not stripped or stripped.lower() == UNSET_TIMESTAMP
    return False


# --- guidance builders (kept next to the checks that quote them) --------------

def _tag_examples(policy: BankPolicy) -> List[str]:
    if not policy.tag_prefixes:
        return ["project:hindsight-crew"]
    return [f"{p}:{_PREFIX_SAMPLES.get(p, 'example')}" for p in policy.tag_prefixes[:2]]


def _prefix_basis(policy: BankPolicy) -> str:
    if not policy.tag_prefixes:
        return ("Allowed tag prefixes: (none declared - any non-empty tag string is "
                "accepted on this bank).")
    return (f"Allowed tag prefixes: {', '.join(policy.tag_prefixes)} "
            f"(grammar: <prefix>:{_TAG_SLUG}).")


def _tag_example_text(policy: BankPolicy) -> str:
    return f'Valid example: "tags": {_tag_examples(policy)!r}'.replace("'", '"')


def _content_example(pattern: str) -> Optional[str]:
    for candidate in _CONTENT_EXAMPLES:
        if re.search(pattern, candidate):
            return candidate
    return None


# --- individual rules ---------------------------------------------------------

def _normalize_tag(raw: Any, policy: BankPolicy) -> Optional[str]:
    """Return the gateway-legal form of `raw`, or None when it cannot be derived.

    Mechanical only: case-fold, slugify the separator characters a human or a model
    naturally types, and file a prefix-less tag under the bank's generic prefix when it
    declares one. It NEVER invents meaning - a value with no ASCII slug left (pure
    Hangul, punctuation) returns None and stays a violation, because dropping it would
    store something the caller never sees (P5).
    """
    if not isinstance(raw, str):
        return None
    head, sep, tail = raw.strip().partition(":")
    if sep and head.strip().lower() not in policy.tag_prefixes:
        # "projekt:kit" is a TYPO, not a topic. Filing it under the generic bucket would
        # store a wrong prefix forever, while the rejection quotes the allowed list and the
        # caller lands on "project:kit" next time. Only a colon-less tag is a fallback case.
        return None
    known = bool(sep)
    prefix = head.strip().lower() if known else _fallback_prefix(policy)
    if prefix is None:
        return None
    slug = re.sub(r"-+", "-", re.sub(r"[^a-z0-9-]+", "-", (tail if known else raw).lower())).strip("-")
    return f"{prefix}:{slug}" if slug else None


def _fallback_prefix(policy: BankPolicy) -> Optional[str]:
    """Where a prefix-less tag goes: only 'topic', the generic bucket, and only if declared.

    Falling back to an arbitrary declared prefix ('project:skills' on a bank that declares
    project|kind) would INVENT meaning, which is the one thing normalization may not do.
    A bank without a generic bucket keeps the rejection, hint included.
    """
    return "topic" if "topic" in policy.tag_prefixes else None


def normalize_tags(item: Dict[str, Any], policy: BankPolicy) -> Tuple[List[str], Tuple[Tuple[str, str], ...]]:
    """Rewrite the fixable tags in place-free fashion; report every rewrite.

    Two rewrites compose here, in this order: the MECHANICAL fix (case, separators, a
    missing prefix) and then the bank's declared `tag_aliases` lookup ON THE RESULT.
    The order is forced by the map's own shape - its keys are declared in
    post-normalization form (`topic:aside`, never bare `aside`), so a lookup placed
    before normalization would miss every legacy prefix-less tag. The lookup is a
    SINGLE hop and needs no loop: the loader rejects any map whose value is also a key
    (bank_policies._as_tag_aliases), so there is nothing left to resolve.

    An alias hit is reported through the same (as sent, as forwarded) channel as a
    mechanical fix - the caller is told the value that was actually stored, never the
    intermediate form.

    Returns (tags to forward, ((sent, forwarded), ...)). Values that cannot be made legal
    are passed through UNCHANGED so `_check_tags` still rejects them - the alias step is
    not a hole in enforcement, because the grammar check still runs after it.
    """
    out, rewrites, _ = _normalize_tags(item, policy)
    return out, rewrites


def _normalize_tags(
    item: Dict[str, Any], policy: BankPolicy,
) -> Tuple[List[str], Tuple[Tuple[str, str], ...], Tuple[str, ...]]:
    """normalize_tags plus the warnings the project-candidate step raised (#40).

    Two passes. The first settles every fixable piece (mechanical fix, closed list, alias) so
    that the project decision can look at the WHOLE list - whether the memory already carries
    a project, and how many candidates there are - and the second emits in the caller's order.
    """
    tags = item.get("tags")
    if not policy.tag_prefixes or not isinstance(tags, (list, tuple)):
        return list(tags) if isinstance(tags, (list, tuple)) else tags, (), ()

    grammar = _grammar_re(policy)
    aliases = policy.tag_aliases            # {} on every bank that declares none
    # A bank with a vocabulary may collapse a rewrite-made duplicate too (split pieces).
    collapse = bool(aliases or policy.tag_vocabulary)
    steps: List[Tuple[Any, Any, bool, Optional[str]]] = []   # (tag, piece, split, settled value)
    topic_targets: Dict[str, str] = {}      # topic tag -> candidate tag its alias names (ADR 0001)
    for tag in tags:
        cut = None if isinstance(tag, str) and grammar.match(tag) else _split_attached(tag, policy)
        pieces = [tag] if cut is None else cut  # a legal tag has no separator to split at
        for piece in pieces:
            if isinstance(piece, str) and grammar.match(piece):
                fixed = piece
            else:
                fixed = _normalize_tag(piece, policy)
            if fixed is not None:
                settled = _resolve_closed(fixed, policy)
                if settled is None:
                    target = aliases.get(fixed, fixed)
                    if _is_candidate_alias(fixed, target, policy):
                        # A topic alias that names a candidate-prefix value is a CANDIDATE, not an
                        # unconditional rewrite: the whole list decides (ADR 0001).
                        topic_targets[fixed] = target
                        settled = fixed
                    else:
                        settled = target
                fixed = settled
            steps.append((tag, piece, cut is not None, fixed))

    resolution, warnings = _resolve_candidates(
        [f for _, _, _, f in steps if f is not None], policy, topic_targets)
    out: List[str] = []
    produced: Set[str] = set()              # values THIS function created
    rewrites: List[Tuple[str, str]] = []
    for tag, piece, split, fixed in steps:
        if fixed is None:
            out.append(piece)               # unfixable -> stays a violation
            continue
        if fixed in resolution:
            target, duplicate = resolution[fixed]
            if duplicate:
                # The memory already carries this project: the topic copy is dropped. Disclosed as
                # (sent, the project tag that stays), never silently (P5). The project tag itself
                # is emitted where the caller put it.
                if not (split and (tag, target) in rewrites):
                    rewrites.append((tag, target))
                continue
            fixed = target
        if fixed == piece and not split:
            # A value one of OUR rewrites already emitted is not stored twice just
            # because it also arrived legally - but ONLY on a bank that declares aliases
            # (or a vocabulary). A bank with neither must behave exactly as it did
            # before they existed, down to this duplicate, so the cross-arrival collapse
            # is gated on the declaration rather than applied to every bank that happens
            # to declare tag_prefixes.
            if not (collapse and piece in produced):
                out.append(piece)     # the caller's own duplicates are always left alone
            continue
        # A split reports EVERY piece as (whole string sent, piece stored): one input
        # becoming several values is disclosed as several pairs sharing the same "sent".
        if not (split and (tag, fixed) in rewrites):   # non-split keeps every pair, as before
            rewrites.append((tag, fixed))
        produced.add(fixed)
        if fixed not in out:      # a duplicate a REWRITE created is ours to collapse;
            out.append(fixed)     # duplicates the caller sent legally are left alone.
    return out, tuple(rewrites), tuple(warnings)


def _candidate_prefixes(policy: BankPolicy) -> Dict[str, Tuple[str, ...]]:
    """{candidate prefix: its vocabulary} in declaration order; {} when the bank declares none.

    A candidate prefix is OPEN (a value outside its list is still accepted, never rejected) but
    a topic word that names one of its values is treated as a possible misfile (ADR 0001). The
    generic bucket and closed prefixes are never candidates: the first is where the words come
    from, the second is resolved by `_resolve_closed`. A bank without a generic bucket has no
    topic words to resolve (`_resolve_candidates`), but its candidate prefix still gets the
    similar-name warning.
    """
    generic = _fallback_prefix(policy)
    return {p: policy.tag_vocabulary[p] for p in policy.tag_candidate_prefixes
            if p != generic and p not in policy.tag_closed_prefixes
            and policy.tag_vocabulary.get(p)}


def _is_candidate_alias(topic: str, target: str, policy: BankPolicy) -> bool:
    """True when the alias `topic` -> `target` lands on a value of a candidate prefix's list."""
    generic = _fallback_prefix(policy)
    if target == topic or not generic or not topic.startswith(f"{generic}:"):
        return False
    prefix, _, slug = target.partition(":")
    return slug in _candidate_prefixes(policy).get(prefix, ())


def _resolve_candidates(
    values: List[str], policy: BankPolicy, topic_targets: Optional[Dict[str, str]] = None,
) -> Tuple[Dict[str, Tuple[str, bool]], List[str]]:
    """Decide what each `topic:X` does when `<candidate>:X` is in a candidate vocabulary (ADR 0001).

    `values` are the settled tags of ONE retain. A topic is a candidate when its slug, with the
    alias applied to `<candidate>:<slug>` FIRST, lands in that prefix's list; a topic whose own
    alias names such a value (`topic_targets`, e.g. `topic:mentor` -> `project:hickory-bell`) is a
    candidate for that value. Then, per prefix:
      * the same candidate tag is already on the memory -> the topic copy is dropped;
      * the memory has no tag of that prefix and the candidates are exactly one -> they become it;
      * no such tag and two or more different candidates -> all stay topics, with a warning
        (which project the memory belongs to cannot be told mechanically);
      * another tag of that prefix is there -> cross reference: stays a topic, silently.
    Returns ({topic tag: (candidate tag, True when it is only a duplicate)}, warnings).
    """
    generic = _fallback_prefix(policy)
    if generic is None:
        return {}, []
    decisions: Dict[str, Tuple[str, bool]] = {}
    warnings: List[str] = []
    claimed: Set[str] = set()                       # topics an earlier prefix already decided
    for name, vocab in _candidate_prefixes(policy).items():
        have = {v for v in values if v.startswith(f"{name}:")}
        topics: Dict[str, str] = {}                 # topic tag -> candidate tag, first-seen order
        for value in values:
            if value in topics or value in claimed or not value.startswith(f"{generic}:"):
                continue
            named = (topic_targets or {}).get(value)
            if named is not None and not named.startswith(f"{name}:"):
                continue                            # its alias names another candidate prefix
            cand = named or policy.tag_aliases.get(f"{name}:{value.partition(':')[2]}",
                                                   f"{name}:{value.partition(':')[2]}")
            cand_prefix, _, cand_slug = cand.partition(":")
            if cand_prefix == name and cand_slug in vocab:
                topics[value] = cand
        claimed.update(topics)                      # kept as topic or moved: the first prefix decides
        for topic, cand in topics.items():
            if cand in have:
                decisions[topic] = (cand, True)
        rest = {t: c for t, c in topics.items() if t not in decisions}
        distinct = list(dict.fromkeys(rest.values()))
        if have or not rest:
            continue                                # cross reference (or nothing to do)
        if len(distinct) == 1:
            decisions.update({t: (c, False) for t, c in rest.items()})
        else:
            pairs = ", ".join(f"{t} -> {c}" for t, c in rest.items())
            warnings.append(
                f"[gateway 경고] {name} 태그가 없는데 {name} 후보 topic이 둘 이상이라 어느 {name}의 "
                f"기억인지 가릴 수 없어 모두 topic으로 저장했습니다: {pairs}. 이 기억이 속한 {name}를 "
                f"{name}:<이름> 태그로 직접 붙이세요."
            )
    return decisions, warnings


def _edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _similar_names(slug: str, known: Tuple[str, ...]) -> List[str]:
    """Known names `slug` could be a variant of: a hyphen-boundary prefix/suffix of one another,
    or a few characters apart (1 edit for a name up to 7 characters, 2 above; names under 4
    characters are too short to be told apart by spelling). Nearest first, at most three."""
    found: List[Tuple[int, str]] = []
    for name in known:
        if name == slug:
            continue
        shortest = min(len(name), len(slug))
        gap = abs(len(name) - len(slug))
        related = (slug.startswith(name + "-") or name.startswith(slug + "-")
                   or slug.endswith("-" + name) or name.endswith("-" + slug))
        if related:
            found.append((gap, name))       # an affix relation is `gap` insertions apart, exactly
        elif shortest >= 4 and gap <= 2:    # the edit distance is at least the length gap: the
            dist = _edit_distance(slug, name)   # table is only built for names of a similar length
            if dist <= (1 if shortest <= 7 else 2):
                found.append((dist, name))
    return [n for _, n in sorted(found)[:3]]


def _similar_name_warnings(tags: Any, policy: BankPolicy) -> List[str]:
    """One warning per new candidate-prefix tag (outside its vocabulary) that resembles a known
    name (#40). A warning, never a rejection: a new project name is always accepted."""
    if not isinstance(tags, (list, tuple)):
        return []
    warnings: List[str] = []
    for name, vocab in _candidate_prefixes(policy).items():
        for tag in dict.fromkeys(t for t in tags if isinstance(t, str)):
            prefix, _, slug = tag.partition(":")
            if prefix != name or slug in vocab:
                continue
            near = _similar_names(slug, vocab)
            if near:
                warnings.append(
                    f"[gateway 경고] {tag!r}는 이 뱅크에 없는 새 {name} 이름이고 기존 {name}와 비슷합니다: "
                    f"{', '.join(f'{name}:{n}' for n in near)}. 같은 작업의 기억이면 기존 이름을 쓰세요. "
                    f"저장은 그대로 진행했습니다."
                )
    return warnings


def _closed_lists(policy: BankPolicy) -> Dict[str, Tuple[str, ...]]:
    """{closed prefix: allowed slugs} in declaration order; {} when the bank closes nothing.

    The loader guarantees every closed prefix has a non-empty list; a policy built in code
    without one (missing or empty) simply has that prefix open, so it can never crash a retain.
    """
    return {p: policy.tag_vocabulary[p] for p in policy.tag_closed_prefixes
            if policy.tag_vocabulary.get(p)}


def _resolve_closed(tag: str, policy: BankPolicy) -> Optional[str]:
    """Move a bare word or misfiled topic into the closed list that names it (ADR 0001).

    `tag` is a normalized `<prefix>:<slug>`. Only the generic bucket (`topic:X`, where a bare
    word lands) is looked at: for each closed AND required prefix in declaration order the candidate
    `<closed>:X` gets its alias applied FIRST, and the candidate is taken when the result is
    in that prefix's list (`done` -> `kind:done` -> `kind:completion`). Returns None when no
    closed list claims the word, so it stays a topic. Which prefixes are closed/required is the
    bank's declaration; nothing here names one.
    """
    closed = _closed_lists(policy)
    prefix, _, slug = tag.partition(":")
    if not closed or prefix != _fallback_prefix(policy) or prefix in closed:
        return None
    for name in closed:
        if name not in policy.tag_required_prefixes:
            continue        # closed but not required (user): only a written `user:` prefix is checked
        cand = policy.tag_aliases.get(f"{name}:{slug}", f"{name}:{slug}")
        cand_prefix, _, cand_slug = cand.partition(":")
        if cand_slug in closed.get(cand_prefix, ()):
            return cand
    return None


def _split_attached(tag: Any, policy: BankPolicy) -> Optional[List[str]]:
    """Cut one tag string where a declared prefix and a colon follow a separator.

    Only a bank that DECLARED a tag vocabulary splits (personal-alice and team-eng declare
    the same prefixes but no vocabulary, so they keep storing one slug, as before). The
    cut goes just after a comma, semicolon or whitespace when `<declared prefix>:` comes
    next; every other space or punctuation mark stays inside the slug. A colon-less value
    is never cut: `project:a-kind-gotcha` is a legal slug whose boundaries are ambiguous.
    Returns None whenever there is nothing to cut. Each piece comes back with the separators
    it dragged along ("kind:gotcha, ") trimmed, so a piece is never a bare separator and never
    ends in a newline (which the grammar's `$` would accept and store verbatim); a piece that
    was only separators is dropped.
    """
    if not policy.tag_vocabulary or not isinstance(tag, str):
        return None
    allowed = "|".join(re.escape(p) for p in policy.tag_prefixes)
    pieces = re.split(r"(?<=[\s,;])(?=(?:" + allowed + r"):)", tag.strip(), flags=re.IGNORECASE)
    if len(pieces) < 2:
        return None
    return [t for t in (re.sub(r"^[\s,;]+|[\s,;]+$", "", p) for p in pieces) if t]


def _grammar_re(policy: BankPolicy) -> "re.Pattern[str]":
    allowed = "|".join(re.escape(p) for p in policy.tag_prefixes)
    return re.compile(r"^(?:" + allowed + r"):" + _TAG_SLUG + r"$")


def _check_tags(item: Dict[str, Any], policy: BankPolicy) -> List[Violation]:
    has_key = "tags" in item
    tags = item.get("tags")

    if has_key and tags is not None and not isinstance(tags, (list, tuple)):
        return [Violation(RULE_TAGS_GRAMMAR, (
            f'{RULE_TAGS_GRAMMAR} "tags" must be an array of strings, got '
            f"{type(tags).__name__}. {_tag_example_text(policy)}. {_prefix_basis(policy)}"
        ))]

    values = list(tags) if isinstance(tags, (list, tuple)) else []

    if policy.tags_required and not values:
        return [Violation(RULE_TAGS_REQUIRED, (
            f"{RULE_TAGS_REQUIRED} this bank requires at least one tag, but the call "
            f"carried none. Add tags and retry the same content. "
            f"{_tag_example_text(policy)}. {_prefix_basis(policy)}"
            + (f" This bank also requires a tag for each of: "
               f"{', '.join(policy.tag_required_prefixes)}. "
               f"{_required_guidance(policy, list(policy.tag_required_prefixes))}"
               if policy.tag_required_prefixes else "")
        ))]

    if not policy.tag_prefixes:
        return []  # no declared prefixes = no grammar to enforce

    grammar = _grammar_re(policy)
    bad = [t for t in values if not (isinstance(t, str) and grammar.match(t))]
    if bad:
        # The closed-list findings on the tags that DO parse ride along, so one rejection
        # carries every fix the caller owes (#39: fixable in a single retry).
        good = [t for t in values if isinstance(t, str) and grammar.match(t)]
        return [Violation(RULE_TAGS_GRAMMAR, (
            f"{RULE_TAGS_GRAMMAR} these tags do not match the required grammar "
            f"<prefix>:{_TAG_SLUG} (lowercase, digits and '-' after the prefix): "
            f"{bad!r}. Fix or drop them and retry. {_tag_example_text(policy)}. "
            f"{_prefix_basis(policy)}"
        ))] + _check_closed_lists(good, policy)
    return _check_closed_lists(values, policy)


def _closed_example(prefix: str, slugs: Tuple[str, ...]) -> str:
    """One legal `<prefix>:<slug>` for a closed prefix (the sample when it is in the list)."""
    sample = _PREFIX_SAMPLES.get(prefix)
    return f"{prefix}:{sample if sample in slugs else slugs[0]}"


def _allowed_values(prefix: str, slugs: Tuple[str, ...]) -> str:
    return f"Allowed {prefix} values: {', '.join(slugs)}."


def _check_closed_lists(values: List[Any], policy: BankPolicy) -> List[Violation]:
    """Closed-list and required-prefix checks over tags that already pass the grammar (#39).

    Both messages quote the allowed values and a legal example, so a caller can fix the
    call in one retry. Nothing here names a prefix: the bank declares which are closed
    and which are required.
    """
    closed = _closed_lists(policy)
    required = policy.tag_required_prefixes
    found = set()
    outside: List[Tuple[str, str]] = []                   # (tag, prefix) in caller order
    for tag in values:
        prefix, _, slug = tag.partition(":")
        found.add(prefix)
        if prefix in closed and slug not in closed[prefix]:
            outside.append((tag, prefix))
    out: List[Violation] = []
    if outside:
        parts = []
        for tag, prefix in outside:
            near = difflib.get_close_matches(tag.partition(":")[2], closed[prefix],
                                             n=3, cutoff=0.0)
            parts.append(f"{tag!r} (closest: {', '.join(f'{prefix}:{n}' for n in near)})"
                         if near else f"{tag!r} (no close match)")
        prefixes = sorted({p for _, p in outside}, key=list(closed).index)
        example = [_closed_example(p, closed[p]) for p in prefixes]
        generic = _fallback_prefix(policy)
        subject = (f" A subject word that is none of these belongs in {generic}:<slug>."
                   if generic and generic not in closed else "")
        out.append(Violation(RULE_TAGS_VOCABULARY, (
            f"{RULE_TAGS_VOCABULARY} these tags are outside the closed list for their prefix: "
            f"{'; '.join(parts)}. Replace each with an allowed value and retry. "
            f"{' '.join(_allowed_values(p, closed[p]) for p in prefixes)} "
            f'Valid example: "tags": {json.dumps(example)}.{subject}'
        )))
    missing = [p for p in required if p not in found]
    if missing:
        out.append(Violation(RULE_TAGS_MISSING_PREFIX, (
            f"{RULE_TAGS_MISSING_PREFIX} this bank requires at least one tag for each of these "
            f"prefixes: {', '.join(required)}. The call carried none for: "
            f"{', '.join(missing)}. Add them and retry the same content. "
            f"{_required_guidance(policy, missing)}"
        )))
    return out


def _required_guidance(policy: BankPolicy, prefixes: List[str]) -> str:
    """Allowed values (for closed prefixes) and one valid example covering `prefixes`."""
    closed = _closed_lists(policy)
    example = [_closed_example(p, closed[p]) if p in closed
               else f"{p}:{_PREFIX_SAMPLES.get(p, 'example')}" for p in prefixes]
    allowed = " ".join(_allowed_values(p, closed[p]) for p in prefixes if p in closed)
    return (f"{allowed} " if allowed else "") + f'Valid example: "tags": {json.dumps(example)}.'


def evaluate_tags(tags: Any, policy: BankPolicy) -> TagVerdict:
    """Tag-only rules for one `tags` value: normalization, then the tag check.

    Independent of content, strategy and timestamp, so a caller holding only a stored
    tag list (re-tagging, measurement) gets the same tag outcome `evaluate_item` gives a
    retain. An absent `tags` key and `tags: None` are the same input here - both
    normalize to nothing and both hit only the `tags_required` check.
    """
    item: Dict[str, Any] = {"tags": tags}
    fixed_tags, normalized_tags, warnings = _normalize_tags(item, policy)
    if normalized_tags:
        item["tags"] = fixed_tags
    return TagVerdict(tags=item["tags"], normalized_tags=normalized_tags,
                      violations=tuple(_check_tags(item, policy)),
                      warnings=warnings + tuple(_similar_name_warnings(item["tags"], policy)))


def _check_content(item: Dict[str, Any], policy: BankPolicy) -> List[Violation]:
    pattern = policy.content_prefix_pattern
    if not pattern:
        return []
    content = item.get("content")
    example = _content_example(pattern)
    guidance = (f'Valid example: "content": "{example}"' if example else
                "No built-in example satisfies this pattern - craft content that "
                "matches the regex above.")
    if not isinstance(content, str) or not content:
        return [Violation(RULE_CONTENT_PREFIX, (
            f'{RULE_CONTENT_PREFIX} this bank requires "content" to match {pattern} '
            f"but the call carried no content string. {guidance}."
        ))]
    if re.search(pattern, content):
        return []
    head = content[:40] + ("..." if len(content) > 40 else "")
    return [Violation(RULE_CONTENT_PREFIX, (
        f'{RULE_CONTENT_PREFIX} "content" must match this bank\'s required prefix '
        f"pattern {pattern}. Got: {head!r}. Prepend the label and retry. {guidance}."
    ))]


def _check_strategy(item: Dict[str, Any], policy: BankPolicy) -> List[Violation]:
    allowed = policy.allowed_strategies
    if allowed is None:            # null = unrestricted (distinct from [] = none)
        return []
    strategy = item.get("strategy")
    if strategy is None:
        return []                  # omitting it is always fine
    if isinstance(strategy, str) and strategy in allowed:
        return []
    if allowed:
        basis = (f"Allowed strategies: {', '.join(allowed)}. "
                 f'Valid example: "strategy": "{allowed[0]}"')
    else:
        basis = ('Allowed strategies: (none configured for this bank). '
                 'Valid example: omit the "strategy" argument entirely')
    return [Violation(RULE_STRATEGY_UNKNOWN, (
        f"{RULE_STRATEGY_UNKNOWN} strategy {strategy!r} is not accepted on this bank. "
        f"{basis}."
    ))]


# --- read-argument guard: recall token budget (2026-08-26) --------------------
# The only READ-side rule. Upstream `_filter_by_token_budget` accumulates results while
# the running tiktoken count of `text` stays within `max_tokens` and BREAKS at the first
# result that would exceed it, and never truncates. So a budget smaller than the top
# ranked memory returns `results: []`, indistinguishable from "nothing matched". The
# guard is a REQUEST-SIDE REJECTION, never a silent clamp: clamping would answer a
# question the caller did not ask, and a bypass flag would just move the trap one
# argument over. Floors are declared per bank (BankPolicy.recall_max_tokens_floor).

def _recall_floor_message(value, floor: int) -> str:
    # Never advertise an example the same rule would reject: the quoted budget is the
    # upstream default unless this bank's floor is higher.
    example = max(floor, 4096)
    return (
        f"{RULE_RECALL_MAX_TOKENS_FLOOR} max_tokens={value} is below this bank's floor "
        f"{floor}. Hindsight returns results only while the cumulative token count stays "
        f"within max_tokens and stops at the first memory that would exceed it, so a small "
        f"budget makes the top-ranked memory itself not fit and yields an empty result even "
        f"when matching memories exist (memories on this deployment run up to ~2,000 tokens). Omit "
        f"max_tokens (upstream default 4096) or send max_tokens >= {floor}, e.g. "
        f'{{"query": "...", "max_tokens": {example}}}. To lower relevance noise use '
        f"tags/min_scores, not a smaller budget. This connection's bank is fixed by the "
        f"gateway (bank_id is ignored); low-budget experiments need a connection to a bank "
        f"whose recall_max_tokens_floor is 0, or a direct upstream call."
    )


def check_recall_budget(arguments: Any, policy: BankPolicy) -> Tuple[Violation, ...]:
    """Violations for a recall whose declared budget is below this bank's floor.

    Shared by both surfaces (issue #458): MCP via evaluate_tool_call("recall", ...), REST
    POST memories/recall via gateway.app with the request body as `arguments`.

    Absent `max_tokens` passes (upstream's own 4096 default applies). A non-numeric value
    passes untouched: upstream owns type validation, and answering a type error with a
    budget lecture would teach the model the wrong correction. `bool` is not a budget.
    """
    floor = policy.recall_max_tokens_floor
    if floor <= 0 or not isinstance(arguments, dict) or "max_tokens" not in arguments:
        return ()
    value = arguments["max_tokens"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ()
    if value >= floor:
        return ()
    return (Violation(RULE_RECALL_MAX_TOKENS_FLOOR, _recall_floor_message(value, floor)),)


# --- arguments shape guard (issue #61) ---------------------------------------
#
# `params.arguments` is the one place every tool shares, and everything downstream
# (retain_gates, policy.sanitize_arguments' `dict(arguments or {})`)
# assumes a mapping: a non-dict used to surface as an HTTP 500 for recall and the tools
# without a rule. The retain family depended on the bank, because evaluate_item reads a
# non-dict as `{}`: a strict bank answered with a misleading `RULE:tags-required`, and a
# loose bank (BankPolicy() defaults) silently forwarded `{}` upstream. `null`/absent is NOT
# a violation: a tool call without parameters is the normal path. Falsy non-dicts (`[]`,
# `0`, `false`, `""`) are violations too - `or {}` only hid them by accident.

_JSON_TYPE_NAMES = ((bool, "boolean"), (int, "number"), (float, "number"),
                    (str, "string"), (list, "array"))


def _json_type_name(value: Any) -> str:
    for py_type, name in _JSON_TYPE_NAMES:      # bool first: it is an int subclass
        if isinstance(value, py_type):
            return name
    return type(value).__name__


def _article(type_name: str) -> str:
    return "an" if type_name in ("array", "object") else "a"


# Required argument names of every tool the gateway lets through, from the measured upstream
# tools/list (fixtures/mcp/tools_list.raw). An empty tuple = a tool whose arguments are all
# optional. A tool missing here is one the gateway does not know the schema of (issue #107).
_REQUIRED_ARGUMENTS: Dict[str, Tuple[str, ...]] = {
    "recall": ("query",), "reflect": ("query",),
    "get_memory": ("memory_id",), "update_memory": ("memory_id",),
    "invalidate_memory": ("memory_id",),
    "get_mental_model": ("mental_model_id",), "update_mental_model": ("mental_model_id",),
    "delete_mental_model": ("mental_model_id",), "refresh_mental_model": ("mental_model_id",),
    "clear_mental_model": ("mental_model_id",),
    "create_mental_model": ("name", "source_query"),
    "create_directive": ("name", "content"), "delete_directive": ("directive_id",),
    "get_document": ("document_id",), "delete_document": ("document_id",),
    "get_operation": ("operation_id",), "cancel_operation": ("operation_id",),
    "list_memories": (), "list_mental_models": (), "list_directives": (),
    "list_documents": (), "list_operations": (), "list_tags": (),
    "get_bank": (), "get_bank_stats": (), "update_bank": (), "delete_bank": (),
    "clear_memories": (),
    # upstream v1.1.0 knowledge base reads - only reachable while HC_KB_READ_TOOLS is on (issue #302)
    "get_knowledge_base_tree": (), "search_knowledge_base": ("query",), "get_knowledge_page": ("page_id",),
}


def _arguments_example(tool: Optional[str], policy: BankPolicy) -> Optional[str]:
    """The valid `arguments` object quoted to the model, or None when no honest example exists.

    The retain family gets one built from this bank's own declared rules (same examples its
    retain rule messages quote); every other tool gets its own required argument names.
    None = the bank's content pattern has no built-in example (a placeholder would be
    rejected again as RULE:content-prefix), or the gateway does not know the tool's schema.
    """
    if tool in RETAIN_TOOLS:
        content = (_content_example(policy.content_prefix_pattern)
                   if policy.content_prefix_pattern else _CONTENT_EXAMPLES[0])
        if not content:
            return None
        return json.dumps({"content": content, "tags": _tag_examples(policy)}, ensure_ascii=False)
    required = _REQUIRED_ARGUMENTS.get(tool) if tool is not None else None
    if required is None:
        return None
    return json.dumps({name: "..." for name in required}, ensure_ascii=False)


def _arguments_no_example_note(tool: Optional[str], policy: BankPolicy) -> str:
    """Why `_arguments_example` is None, phrased like `_check_content`'s "no built-in example"."""
    if tool in RETAIN_TOOLS:
        return (f"No built-in example satisfies this bank's content pattern "
                f"{policy.content_prefix_pattern} - craft \"content\" that matches that regex and "
                f"send \"tags\" like {json.dumps(_tag_examples(policy), ensure_ascii=False)}")
    named = f"tool {tool!r}" if tool is not None else "this call's tool"
    return (f"No example is available: the gateway does not know the argument names of "
            f"{named} - read its inputSchema from tools/list")


def _arguments_not_object_message(value: Any, tool: Optional[str], policy: BankPolicy) -> str:
    type_name = _json_type_name(value)
    example = _arguments_example(tool, policy)
    form = (f', e.g. "arguments": {example}' if example is not None
            else f". {_arguments_no_example_note(tool, policy)}")
    return (
        f'{RULE_ARGUMENTS_NOT_OBJECT} "arguments" of a tools/call must be a JSON object '
        f"(or omitted), but the call sent {_article(type_name)} {type_name}. Nothing was "
        f"forwarded. Send every field as a named key{form}; a tool without parameters "
        f'takes {{}} or no "arguments" at all.'
    )


def _metadata_not_object_message(value: Any) -> str:
    type_name = _json_type_name(value)
    return (
        f'{RULE_METADATA_NOT_OBJECT} "metadata" of a retained item must be a JSON object '
        f"(or omitted), but it is {_article(type_name)} {type_name}. "
        f'Nothing was forwarded. Send it as key/value pairs, e.g. "metadata": {{"source": "..."}}, '
        f'or leave "metadata" out.'
    )


def _check_arguments_shape(
    tool: Optional[str], arguments: Any, policy: BankPolicy,
) -> Tuple[Violation, ...]:
    if arguments is None:
        return ()
    if not isinstance(arguments, dict):
        return (Violation(RULE_ARGUMENTS_NOT_OBJECT,
                          _arguments_not_object_message(arguments, tool, policy)),)
    return ()


def _check_metadata(item: dict) -> Tuple[Violation, ...]:
    # #70/#73: team-bank attribution (MCP policy.sanitize_arguments, REST items loop) merges
    # into `metadata` as a mapping - a list 500ed there (or `["ab"]` silently became
    # {"a": "b"}). A shape rule judged per item, so it holds on every bank and both surfaces.
    metadata = item.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        return (Violation(RULE_METADATA_NOT_OBJECT, _metadata_not_object_message(metadata)),)
    return ()


def request_shape_violation(element: Any) -> Optional[Violation]:
    """JSON-RPC envelope shape, judged BEFORE anything reads the element (#70).

    The DECIDE loop reads `element["params"]["name"]` and decide() looks `method`/`tool` up
    in frozensets, so a non-object element (`[1]`, a bare `"x"` body), a non-object
    `tools/call` params, or an unhashable method/name used to 500 there. `null`/absent
    params is NOT a violation: decide() answers it as `missing-tool-name`, as before.
    Other methods' params are not judged here (nothing on their path reads them as a map).
    """
    if not isinstance(element, dict):
        type_name = _json_type_name(element) if element is not None else "null"
        return Violation(RULE_REQUEST_NOT_OBJECT, (
            f"{RULE_REQUEST_NOT_OBJECT} every JSON-RPC request (and every batch element) must "
            f"be a JSON object, but this one is {_article(type_name)} {type_name}. Nothing was "
            f'forwarded. Send e.g. {{"jsonrpc": "2.0", "id": 1, "method": "tools/call", '
            f'"params": {{"name": "recall", "arguments": {{"query": "..."}}}}}}.'))
    method = element.get("method")
    if method is not None and not isinstance(method, str):
        type_name = _json_type_name(method)
        return Violation(RULE_METHOD_NOT_STRING, (
            f'{RULE_METHOD_NOT_STRING} "method" must be a string, but the request sent '
            f"{_article(type_name)} {type_name}. Nothing was forwarded. Send e.g. "
            f'"method": "tools/call".'))
    params = element.get("params")
    if method == "tools/call" and isinstance(params, dict):
        name = params.get("name")
        if name is not None and not isinstance(name, str):
            type_name = _json_type_name(name)
            return Violation(RULE_TOOL_NAME_NOT_STRING, (
                f'{RULE_TOOL_NAME_NOT_STRING} "params.name" of a tools/call must be a string, '
                f"but the call sent {_article(type_name)} {type_name}. Nothing was forwarded. "
                f'Send e.g. "params": {{"name": "recall", "arguments": {{"query": "..."}}}}.'))
    if method == "tools/call" and params is not None and not isinstance(params, dict):
        type_name = _json_type_name(params)
        return Violation(RULE_PARAMS_NOT_OBJECT, (
            f'{RULE_PARAMS_NOT_OBJECT} "params" of a tools/call must be a JSON object, but '
            f"the call sent {_article(type_name)} {type_name}. Nothing was forwarded. Name the "
            f'tool and its arguments as keys, e.g. "params": {{"name": "recall", '
            f'"arguments": {{"query": "..."}}}}.'))
    return None


# --- public entry points ------------------------------------------------------

def evaluate_item(item: Any, policy: BankPolicy, now_iso: str) -> ItemVerdict:
    """Apply one bank's declared rules to a single item dict.

    Returns a verdict holding a NEW dict (timestamp corrected when the policy says so)
    plus every violation found. Never mutates `item`; never reads the clock.
    """
    source = item if isinstance(item, dict) else {}
    out = dict(source)

    autofilled = None
    if policy.timestamp_autofill and (
        "timestamp" not in out or is_unset_timestamp(out.get("timestamp"))
    ):
        out["timestamp"] = now_iso
        autofilled = now_iso

    # Normalization runs BEFORE the checks, so a mechanically fixable tag never becomes a
    # rejection the caller can only answer by retyping the same tag (2026-07-27). What it
    # cannot fix it leaves alone, and _check_tags still rejects that.
    tag_verdict = evaluate_tags(out.get("tags"), policy)
    normalized_tags = tag_verdict.normalized_tags
    if normalized_tags:
        out["tags"] = tag_verdict.tags

    violations: List[Violation] = []
    violations.extend(tag_verdict.violations)
    violations.extend(_check_content(out, policy))
    violations.extend(_check_strategy(out, policy))
    violations.extend(_check_metadata(out))
    key_violations, key_warnings = _item_key_rule(source, out, policy)
    violations.extend(key_violations)

    return ItemVerdict(item=out, violations=tuple(violations), autofilled_timestamp=autofilled,
                       normalized_tags=normalized_tags, warnings=tag_verdict.warnings + key_warnings)


def _item_key_rule(sent: Dict[str, Any], out: Dict[str, Any],
                   policy: BankPolicy) -> Tuple[Tuple[Violation, ...], Tuple[str, ...]]:
    """S2 (issue #364, flag HC_RETAIN_ITEM_KEY, default off): a handoff/todo/completion item
    (or a pending-label decision) with no item key. Judged on the tags as SENT (kind:todo) and
    as CORRECTED (aliases applied) - see gateway/retain_item_key.py. Off returns ((), ()) without
    judging anything, so evaluate_item is unchanged; warn adds one warning, reject one violation.
    """
    mode = retain_item_key.mode()
    if mode == retain_item_key.MODE_OFF:
        return (), ()
    item_tag = retain_item_key.item_tag_allowed(policy.tag_prefixes)
    stored = out.get("tags")
    if not item_tag and isinstance(stored, (list, tuple)):
        # An `item:` tag on a bank that does not declare the prefix is rejected by tags-grammar in
        # this same verdict; counting it as a key would hide S2 until the caller drops it.
        stored = [t for t in stored if not (isinstance(t, str) and t.strip().lower().startswith("item:"))]
    why = retain_item_key.missing_key(_mechanical_tags(sent.get("tags"), policy), stored,
                                      out.get("content"), policy.item_repo_aliases)
    if why is None:
        return (), ()
    if mode == retain_item_key.MODE_WARN:
        return (), (retain_item_key.warning_text(why, out.get("tags"), item_tag),)
    return (Violation(RULE_ITEM_KEY_REQUIRED,
                      retain_item_key.violation_message(
                          why, out.get("tags"), item_tag,
                          retain_item_key.example_kind(why, policy.tag_aliases))),), ()


def _mechanical_tags(tags: Any, policy: BankPolicy) -> List[Any]:
    """The sent tags after the MECHANICAL fix only (split, case, separators), before any alias.

    S2 judges `kind:todo` on the tags as sent because the alias turns it into kind:decision; a
    sent `kind: todo` or an attached `kind:todo,project:x` becomes kind:todo by the same fix
    `_normalize_tags` applies, so it is a todo too. The raw values are kept alongside.
    """
    if not isinstance(tags, (list, tuple)):
        return []
    grammar = _grammar_re(policy) if policy.tag_prefixes else None
    out: List[Any] = list(tags)
    for tag in tags:
        if grammar is None or (isinstance(tag, str) and grammar.match(tag)):
            continue
        for piece in _split_attached(tag, policy) or [tag]:
            fixed = _normalize_tag(piece, policy)
            if fixed is not None:
                out.append(fixed)
    return out


def instructions_for(policy: BankPolicy) -> Optional[str]:
    """Render this bank's rules as MCP `initialize` instructions, or None when it has none.

    DERIVED from the same policy the enforcement reads, never hand-written prose: a client
    that is told the rules before its first call does not need a rejection to learn them,
    and derivation is the only way the advertised rules cannot drift from the enforced
    ones (2026-07-27). `instructions_text` is appended as the operator's own words.
    """
    lines: List[str] = []
    if policy.tag_prefixes:
        lines.append(
            f"- tags: each tag must be \"<prefix>:<slug>\" where prefix is one of "
            f"{', '.join(policy.tag_prefixes)} and slug is {_TAG_SLUG} (lowercase). "
            f"{_tag_example_text(policy)}. A fixable tag (missing prefix, uppercase, spaces) "
            f"is normalized by the gateway and the correction is reported in the response; "
            f"a tag with no ASCII slug is rejected."
        )
    if policy.tag_aliases:
        # Two pairs, taken in declaration order: the map is the SSoT, and a handshake that
        # listed all of it would spend the whole instruction budget on a lookup table.
        pairs = ", ".join(f'"{old}" -> "{new}"'
                          for old, new in list(policy.tag_aliases.items())[:2])
        lines.append(
            f"- obsolete tags are rewritten to their canonical form server-side "
            f"(e.g. {pairs}); the rewrite is reported in the response, so the tag you "
            f"sent is not always the tag that was stored - send the canonical one when "
            f"you know it."
        )
    if policy.tags_required:
        lines.append("- tags are REQUIRED on this bank: a retain with no tag is rejected.")
    closed = _closed_lists(policy)
    if closed or policy.tag_required_prefixes:
        # Sizes, not lists: a 26-value list would spend the handshake budget (the 2048-byte cap),
        # and the rejection quotes the full allowed values anyway. Short lists are spelled out.
        parts = []
        for prefix, vals in closed.items():
            parts.append(f"{prefix} = {'|'.join(vals)}" if len(vals) <= 3
                         else f"{prefix} ({len(vals)} values)")
        need = ", ".join(policy.tag_required_prefixes)
        lines.append(
            (f"- at least one tag of each of these prefixes is REQUIRED: {need}. " if need else "")
            + (f"Closed lists: {', '.join(parts)}; a value outside its list is rejected and "
               f"the rejection quotes the allowed values." if parts else "")
        )
    if policy.content_prefix_pattern:
        example = _content_example(policy.content_prefix_pattern)
        lines.append(
            f'- content must match {policy.content_prefix_pattern} '
            f"(a short bracketed label, then a space, then the entry)."
            + (f' Valid example: "{example}"' if example else "")
        )
    if policy.allowed_strategies is not None and not policy.allowed_strategies:
        lines.append('- do not send "strategy": this bank accepts no strategy name.')
    if not lines:
        # A bank that declares no constraint has nothing a caller must satisfy. Timestamp
        # autofill alone is a courtesy correction (already annotated on every response),
        # not a rule - advertising it would put noise in every loose bank's handshake.
        return None
    if policy.timestamp_autofill:
        lines.append(
            "- timestamp may be omitted: the gateway records the current UTC instant and "
            "says so in the response."
        )
    if policy.recall_max_tokens_floor > 0:
        # Deliberately AFTER the `not lines` gate: a bank that declares no format rule at
        # all still gets no handshake (the rejection message teaches the rule on contact).
        lines.append(
            f"- recall: omit max_tokens or send >= {policy.recall_max_tokens_floor}; "
            f"smaller values are rejected because the top memory may not fit and the "
            f"result would be empty."
        )
    body = "This memory bank enforces the following retain rules server-side. " \
           "Follow them on the FIRST call - a violation is rejected, not queued.\n" + "\n".join(lines)
    if policy.instructions_text:
        body = body + "\n" + policy.instructions_text
    return body


def evaluate_tool_call(
    tool: Optional[str],
    arguments: Any,
    policy: BankPolicy,
    now_iso: str,
) -> ItemVerdict:
    """MCP adapter: retain-family tools/call arguments ARE a single item; others pass.

    Non-retain tools (recall, get_operation, ...) carry no retained content, so they are
    returned untouched — enforcement must never widen beyond the family that writes. The
    lone read-side guard is `recall`'s token budget, which rejects (never rewrites).

    Before any per-tool logic, a non-object `arguments` is rejected for EVERY tool
    (RULE:arguments-not-object, #61): the later stages all assume a mapping.
    """
    shape = _check_arguments_shape(tool, arguments, policy)
    if shape:
        return ItemVerdict(item=arguments, violations=shape)
    if tool not in RETAIN_TOOLS:
        # One exception: `recall` carries a READ argument that can silently empty the
        # answer (check_recall_budget). Nothing is corrected; the arguments are returned
        # exactly as sent, either forwarded untouched or rejected whole.
        if tool == "recall":
            return ItemVerdict(item=arguments,
                               violations=check_recall_budget(arguments, policy))
        return ItemVerdict(item=arguments)
    return evaluate_item(arguments, policy, now_iso)
