"""Declarative per-bank rule profiles (N1) — loader, validator, drift compare.

The gateway enforces retain/recall FORMAT rules per bank (N2/N3). What each bank
requires is DECLARED here, never hardcoded: `policies/bank-policies.json`, mounted
read-only at /app/policies and pointed at by env `HC_BANK_POLICIES`.

File shape:
{
  "defaults": {"tags_required": false, "tag_prefixes": [], ...},
  "banks": {"personal-demo": {"tags_required": true, ...}}
}

Merge is KEY-EXISTENCE based, never truthiness: a bank entry overrides only the
keys it spells out, and an explicit `null` is a real value. This matters because
`null` and `[]` mean different things for `allowed_strategies` (null = no
restriction, [] = no strategy name accepted) — the same distinction upstream makes
in GET /config, where `retain_strategies` is a PRESENT key with a NULL value
(v0.8.3 실측, fixtures/rest/bank_config.json).

Validation is hand-rolled stdlib (no jsonschema — production deps stay at zero) and
fail-fast: an unknown key is an error, not a silently ignored typo (P5).

Local overlay (issue #187): `policies/local/bank-policies.json` (gitignored, path derived
next to the tracked file, overridable with env `HC_BANK_POLICIES_LOCAL`) may hold
{"banks": {...}} only. Each bank there is ADDED, or REPLACES the tracked bank of the same
name wholesale (no per-key merge with the tracked bank; it still inherits the tracked
`defaults`). Absent local file = tracked file alone; present-but-broken = startup failure.

Update procedure (no rebuild): edit policies/bank-policies.json → restart the
gateway container only (`docker compose restart gateway`; the file is a ro bind
mount, so the image is unchanged). A malformed file fails the gateway at startup by
design — validate locally with `.venv/bin/pytest tests/test_bank_policies.py` first.
Full deployment/rollback runbook: N7.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field, fields, replace
from typing import Dict, FrozenSet, List, Optional, Tuple

_LOG = logging.getLogger("gateway.bank_policies")

# Same relative default as compose's HC_BANK_POLICIES (kept identical to avoid drift).
DEFAULT_POLICY_FILE = "policies/bank-policies.json"
# Per-install overlay directory, next to the tracked file (issue #187, gitignored).
LOCAL_DIR_NAME = "local"
# UTF-8 byte cap for initialize instructions (2회차 S3 uses it; declared here now).
INSTRUCTIONS_MAX_BYTES = 2048

_TAG_PREFIX_RE = re.compile(r"^[a-z][a-z0-9-]*$")
# Slug half of the tag grammar. Duplicated from rules._TAG_SLUG on purpose: rules imports
# this module, so importing rules back would be a cycle for a 12-character regex.
_TAG_SLUG = r"[a-z0-9-]+"
_BANK_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")  # mirrors app.py _BANK_RE
# "update_bank" = every write of the bank profile and configuration (#473): MCP update_bank, REST PATCH and PUT
# (provision_bank) on the bank root, and REST PATCH .../config are all denied by this one entry.
_BLOCKABLE_OPERATIONS = frozenset({"delete_bank", "clear_memories", "update_bank"})


class BankPolicyError(ValueError):
    """Malformed policy declaration. Always raised — never degraded to a default."""


@dataclass(frozen=True)
class BankPolicy:
    """One bank's declared rule profile. Loose defaults; strict banks opt in."""

    tags_required: bool = False
    tag_prefixes: Tuple[str, ...] = ()
    content_prefix_pattern: Optional[str] = None     # None = no prefix requirement
    allowed_strategies: Optional[Tuple[str, ...]] = None  # None = unrestricted, () = none
    blocked_operations: FrozenSet[str] = frozenset()
    timestamp_autofill: bool = True                  # universal (correction, not filtering)
    # Smallest `max_tokens` a recall may ask for; 0 = no floor. Upstream's
    # _filter_by_token_budget BREAKS at the first result that would exceed the budget
    # instead of truncating it, so a budget under one memory's length returns an EMPTY
    # result with no explanation. The code default is protective on purpose: a missing
    # policy file falls back to BankPolicy(), and that fallback must not re-open the trap.
    recall_max_tokens_floor: int = 4096
    instructions_text: Optional[str] = None          # S3 (2회차); validated here already
    # Obsolete tag -> canonical tag, in POST-normalization form (bare `aside` arrives as
    # `topic:aside`). A mutable default needs a factory; a bare {} raises at class creation.
    tag_aliases: Dict[str, str] = field(default_factory=dict)
    # Declared tag vocabulary: prefix -> allowed slugs (no prefix in the value). {} = none
    # declared, and a bank that declares none behaves exactly as before vocabularies
    # existed. #38 uses it only as the GATE for splitting run-together tags; later tickets
    # (#39 bare words, #40 project correction) resolve against the same lists.
    tag_vocabulary: Dict[str, Tuple[str, ...]] = field(default_factory=dict)
    # Which declared prefixes are CLOSED (#39): a tag under one is accepted only when its slug
    # is in tag_vocabulary[prefix] (after aliases). () = none closed, and such a bank behaves
    # as before. A bare word / misfiled topic is resolved only into a prefix that is closed AND
    # required (a closed but optional prefix such as user is checked, never a resolution target).
    tag_closed_prefixes: Tuple[str, ...] = ()
    # Prefixes every retained item must carry at least once. () = none required.
    tag_required_prefixes: Tuple[str, ...] = ()
    # Open prefixes whose vocabulary is a RESOLUTION CANDIDATE (#40, ADR 0001): a topic word that
    # names one of its values (after aliases) is moved there when the memory has none of that
    # prefix and the candidate is unique. The prefix stays open - a new value is never rejected.
    # () = none, and such a bank behaves as before.
    tag_candidate_prefixes: Tuple[str, ...] = ()
    # Repo alias table for work-item keys (#320, gateway/item_keys.py): {alias: canonical repo slug},
    # both sides [a-z0-9-]+. {} = none declared, and such a bank behaves exactly as before the key
    # existed. Independent of tag_prefixes: it is a lookup table for item_keys, not a tag rule.
    item_repo_aliases: Dict[str, str] = field(default_factory=dict)


_FIELD_NAMES = frozenset(f.name for f in fields(BankPolicy))
_TOP_LEVEL_KEYS = frozenset({"defaults", "banks"})
# A local overlay adds/replaces banks only: defaults stay tracked, so one install cannot
# silently loosen every unregistered bank.
_LOCAL_TOP_LEVEL_KEYS = frozenset({"banks"})


def local_overlay_path(path: str) -> str:
    """`<dir>/local/<name>` for a tracked policy/trigger file `<dir>/<name>`."""
    return os.path.join(os.path.dirname(path), LOCAL_DIR_NAME, os.path.basename(path))


# --- field validators (each raises BankPolicyError with the offending path) ----

def _as_bool(value, where: str) -> bool:
    if not isinstance(value, bool):
        raise BankPolicyError(f"{where}: expected bool, got {type(value).__name__} ({value!r})")
    return value


def _as_tag_prefixes(value, where: str) -> Tuple[str, ...]:
    if not isinstance(value, list):
        raise BankPolicyError(f"{where}: expected array of strings, got {type(value).__name__}")
    out: List[str] = []
    for i, item in enumerate(value):
        if not isinstance(item, str) or not _TAG_PREFIX_RE.match(item):
            raise BankPolicyError(
                f"{where}[{i}]: {item!r} is not a valid tag prefix (must match ^[a-z][a-z0-9-]*$)"
            )
        out.append(item)
    return tuple(out)


def _as_pattern(value, where: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise BankPolicyError(f"{where}: expected string or null, got {type(value).__name__}")
    try:
        re.compile(value)
    except re.error as exc:
        raise BankPolicyError(f"{where}: not a compilable regex ({exc})") from exc
    return value


def _as_strategies(value, where: str) -> Optional[Tuple[str, ...]]:
    if value is None:
        return None  # null = unrestricted (distinct from [] = no strategy accepted)
    if not isinstance(value, list):
        raise BankPolicyError(f"{where}: expected array of strings or null, got {type(value).__name__}")
    out: List[str] = []
    for i, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise BankPolicyError(f"{where}[{i}]: expected non-empty string, got {item!r}")
        out.append(item)
    return tuple(out)


def _as_blocked_operations(value, where: str) -> FrozenSet[str]:
    if not isinstance(value, list):
        raise BankPolicyError(f"{where}: expected array of strings, got {type(value).__name__}")
    out: List[str] = []
    for i, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise BankPolicyError(f"{where}[{i}]: expected non-empty string, got {item!r}")
        if item not in _BLOCKABLE_OPERATIONS:
            raise BankPolicyError(
                f"{where}[{i}]: unknown blocked operation {item!r}; expected one of "
                f"{sorted(_BLOCKABLE_OPERATIONS)}"
            )
        if item in out:
            raise BankPolicyError(f"{where}[{i}]: duplicate blocked operation {item!r}")
        out.append(item)
    return frozenset(out)


def _as_instructions(value, where: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise BankPolicyError(f"{where}: expected string or null, got {type(value).__name__}")
    size = len(value.encode("utf-8"))
    if size > INSTRUCTIONS_MAX_BYTES:
        raise BankPolicyError(
            f"{where}: {size} UTF-8 bytes exceeds the {INSTRUCTIONS_MAX_BYTES}-byte cap"
        )
    return value


def _as_recall_floor(value, where: str) -> int:
    """Non-negative int only. `bool` is NOT accepted even though it subclasses int:
    `true` in JSON is a declaration mistake, and silently reading it as a floor of 1
    would disable the guard while looking configured."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise BankPolicyError(
            f"{where}: expected a non-negative integer (0 = no floor), got "
            f"{type(value).__name__} ({value!r})"
        )
    if value < 0:
        raise BankPolicyError(
            f"{where}: expected a non-negative integer (0 = no floor), got {value}"
        )
    return value


def _as_tag_aliases(value, where: str) -> Dict[str, str]:
    """Shape + non-transitivity. Grammar needs the merged tag_prefixes → _check_alias_grammar.

    Non-transitivity is an invariant of the DECLARATION, enforced here at load: if a
    canonical value were itself an alias key, the request path would owe callers either a
    resolution loop or a silently half-applied rewrite. Rejecting it at startup is what
    lets the enforcement seam resolve exactly one hop.
    """
    if not isinstance(value, dict):
        raise BankPolicyError(
            f"{where}: expected an object mapping obsolete tag -> canonical tag, "
            f"got {type(value).__name__}"
        )
    out: Dict[str, str] = {}
    for key, canonical in value.items():
        if not isinstance(key, str):
            raise BankPolicyError(
                f"{where}: alias key {key!r} must be a string, got {type(key).__name__}"
            )
        if not isinstance(canonical, str):
            raise BankPolicyError(
                f"{where}: alias value for {key!r} must be a string, "
                f"got {type(canonical).__name__} ({canonical!r})"
            )
        out[key] = canonical
    transitive = sorted(set(out.values()) & set(out))
    if transitive:
        raise BankPolicyError(
            f"{where}: transitive alias target(s) {transitive} — an alias value may never "
            f"also be an alias key (rewrites resolve exactly one hop)"
        )
    return out


def _as_item_repo_aliases(value, where: str) -> Dict[str, str]:
    """Shape only: {alias: repo}, every key and value a non-empty slug [a-z0-9-]+ (#320).

    No transitivity rule (unlike tag_aliases): item_keys resolves one hop, and the example table
    maps a repo to itself (`{"hindsight-crew": "hindsight-crew"}`) so it can be listed as a known name.
    """
    if not isinstance(value, dict):
        raise BankPolicyError(
            f"{where}: expected an object mapping repo alias -> repo slug, got {type(value).__name__}"
        )
    slug_re = re.compile(_TAG_SLUG)       # fullmatch: `$` would also accept a trailing newline
    out: Dict[str, str] = {}
    for key, repo in value.items():
        if not isinstance(key, str) or not slug_re.fullmatch(key):
            raise BankPolicyError(
                f"{where}: alias {key!r} is not a valid slug (must match ^{_TAG_SLUG}$, lowercase)"
            )
        if not isinstance(repo, str) or not slug_re.fullmatch(repo):
            raise BankPolicyError(
                f"{where}: repo for alias {key!r} must be a slug matching ^{_TAG_SLUG}$, "
                f"got {type(repo).__name__} ({repo!r})"
            )
        out[key] = repo
    return out


def _as_tag_vocabulary(value, where: str) -> Dict[str, Tuple[str, ...]]:
    """Shape only: {prefix: [slug, ...]}. The checks that need the merged policy (declared
    prefixes, collision with alias keys) run in _check_vocabulary after the overlay."""
    if not isinstance(value, dict):
        raise BankPolicyError(
            f"{where}: expected an object mapping tag prefix -> array of slugs, "
            f"got {type(value).__name__}"
        )
    slug_re = re.compile(r"^" + _TAG_SLUG + r"$")
    out: Dict[str, Tuple[str, ...]] = {}
    for prefix, slugs in value.items():
        if not isinstance(prefix, str) or not _TAG_PREFIX_RE.match(prefix):
            raise BankPolicyError(
                f"{where}: vocabulary prefix {prefix!r} is not a valid tag prefix "
                f"(must match ^[a-z][a-z0-9-]*$)"
            )
        if not isinstance(slugs, list) or not slugs:
            raise BankPolicyError(
                f"{where}.{prefix}: expected a non-empty array of slugs, got "
                f"{type(slugs).__name__} ({slugs!r})"
            )
        seen: List[str] = []
        for i, slug in enumerate(slugs):
            if not isinstance(slug, str) or not slug_re.match(slug):
                raise BankPolicyError(
                    f"{where}.{prefix}[{i}]: {slug!r} is not a valid slug (write it without "
                    f"the prefix, matching ^{_TAG_SLUG}$)"
                )
            if slug in seen:
                raise BankPolicyError(f"{where}.{prefix}[{i}]: duplicate slug {slug!r}")
            seen.append(slug)
        out[prefix] = tuple(seen)
    return out


def _check_vocabulary(policy: BankPolicy, where: str) -> None:
    """Post-merge checks: only declared prefixes, and no vocabulary value is an alias key.

    An alias key is an OBSOLETE name that gets rewritten, so a vocabulary that also lists
    it would call the same tag both retired and canonical.
    """
    if not policy.tag_vocabulary:
        return
    if not policy.tag_prefixes:
        raise BankPolicyError(
            f"{where}.tag_vocabulary: declared without tag_prefixes - the prefixes it "
            f"lists are undefined"
        )
    for prefix, slugs in policy.tag_vocabulary.items():
        if prefix not in policy.tag_prefixes:
            raise BankPolicyError(
                f"{where}.tag_vocabulary: prefix {prefix!r} is not one of this bank's "
                f"tag_prefixes {list(policy.tag_prefixes)}"
            )
        clash = sorted(s for s in slugs if f"{prefix}:{s}" in policy.tag_aliases)
        if clash:
            raise BankPolicyError(
                f"{where}.tag_vocabulary.{prefix}: {clash} are also tag_aliases keys - an "
                f"alias key is an obsolete name and cannot be a vocabulary value"
            )


def _check_closed_prefixes(policy: BankPolicy, where: str) -> None:
    """Post-merge checks for closed/required prefixes (#39).

    A closed prefix needs a vocabulary to be closed over, and a bare word resolves against
    every closed AND required list at once, so two such lists sharing a slug would make that
    resolution order-dependent (closed lists that are not required never resolve a bare
    word, so they may share slugs). An alias that lands on a closed prefix must land INSIDE its list,
    otherwise the alias would rewrite a tag into one the same request then rejects.
    """
    for name, prefixes in (("tag_closed_prefixes", policy.tag_closed_prefixes),
                           ("tag_required_prefixes", policy.tag_required_prefixes),
                           ("tag_candidate_prefixes", policy.tag_candidate_prefixes)):
        dup = sorted({p for p in prefixes if prefixes.count(p) > 1})
        if dup:
            raise BankPolicyError(f"{where}.{name}: duplicate prefix(es) {dup}")
        if prefixes and not policy.tag_prefixes:
            raise BankPolicyError(
                f"{where}.{name}: declared without tag_prefixes - the prefixes it lists "
                f"are undefined"
            )
        outside = [p for p in prefixes if p not in policy.tag_prefixes]
        if outside:
            raise BankPolicyError(
                f"{where}.{name}: {outside} are not among this bank's tag_prefixes "
                f"{list(policy.tag_prefixes)}"
            )
    for prefix in policy.tag_closed_prefixes:
        if prefix not in policy.tag_vocabulary:
            raise BankPolicyError(
                f"{where}.tag_closed_prefixes: {prefix!r} is closed but tag_vocabulary "
                f"declares no list for it"
            )
    for prefix in policy.tag_candidate_prefixes:
        if prefix not in policy.tag_vocabulary:
            raise BankPolicyError(
                f"{where}.tag_candidate_prefixes: {prefix!r} is a candidate but tag_vocabulary "
                f"declares no list for it"
            )
        if prefix in policy.tag_closed_prefixes:
            raise BankPolicyError(
                f"{where}.tag_candidate_prefixes: {prefix!r} is also closed - a closed prefix is "
                f"resolved by its own list, a candidate prefix stays open"
            )
    # A bare word resolves (rules._resolve_closed) to `alias(<prefix>:word)` for the first closed
    # required prefix whose result lands in a closed list, so ownership is judged AFTER the alias
    # (#107): `a:[x]`, `b:[y]` and alias `b:x -> b:y` give x two different values depending on
    # declaration order. Two prefixes reaching the SAME value are not ambiguous.
    claims: Dict[str, Dict[str, str]] = {}     # bare word -> {claiming prefix: resolved value}
    for prefix in policy.tag_closed_prefixes:
        if prefix not in policy.tag_required_prefixes:
            continue
        words = list(policy.tag_vocabulary[prefix]) + [
            key.partition(":")[2] for key in policy.tag_aliases if key.partition(":")[0] == prefix]
        for word in words:
            cand = policy.tag_aliases.get(f"{prefix}:{word}", f"{prefix}:{word}")
            cand_prefix, _, cand_slug = cand.partition(":")
            if cand_prefix in policy.tag_closed_prefixes and cand_slug in policy.tag_vocabulary[cand_prefix]:
                claims.setdefault(word, {})[prefix] = cand
    for word, by_prefix in claims.items():
        if len(set(by_prefix.values())) > 1:
            detail = ", ".join(f"{p!r} -> {value!r}" for p, value in by_prefix.items())
            raise BankPolicyError(
                f"{where}.tag_vocabulary: bare word {word!r} is claimed by the closed lists of "
                f"both prefixes ({detail}), after tag_aliases are applied - a bare word could "
                f"not be resolved"
            )
    for key, canonical in policy.tag_aliases.items():
        prefix, _, slug = canonical.partition(":")
        if prefix in policy.tag_closed_prefixes and slug not in policy.tag_vocabulary[prefix]:
            raise BankPolicyError(
                f"{where}.tag_aliases: alias {key!r} -> {canonical!r} points outside the "
                f"closed {prefix!r} list"
            )


def _check_alias_grammar(policy: BankPolicy, where: str) -> None:
    """Every alias key AND value must be a legal tag for THIS bank (post-merge check).

    Runs on the merged policy, not on the raw overlay, so a bank that narrows
    tag_prefixes while inheriting the map from defaults still fails loudly.
    """
    if not policy.tag_aliases:
        return
    if not policy.tag_prefixes:
        raise BankPolicyError(
            f"{where}.tag_aliases: declared without tag_prefixes — the tag grammar these "
            f"aliases must satisfy is undefined"
        )
    allowed = "|".join(re.escape(p) for p in policy.tag_prefixes)
    grammar = re.compile(r"^(?:" + allowed + r"):" + _TAG_SLUG + r"$")
    for key, canonical in policy.tag_aliases.items():
        for role, tag in (("key", key), ("value", canonical)):
            if not grammar.match(tag):
                raise BankPolicyError(
                    f"{where}.tag_aliases: alias {role} {tag!r} violates this bank's tag "
                    f"grammar <prefix>:{_TAG_SLUG} with prefixes "
                    f"{list(policy.tag_prefixes)}"
                )


_VALIDATORS = {
    "tags_required": _as_bool,
    "tag_prefixes": _as_tag_prefixes,
    "content_prefix_pattern": _as_pattern,
    "allowed_strategies": _as_strategies,
    "blocked_operations": _as_blocked_operations,
    "timestamp_autofill": _as_bool,
    "recall_max_tokens_floor": _as_recall_floor,
    "instructions_text": _as_instructions,
    "tag_aliases": _as_tag_aliases,
    "tag_vocabulary": _as_tag_vocabulary,
    "tag_closed_prefixes": _as_tag_prefixes,
    "tag_required_prefixes": _as_tag_prefixes,
    "tag_candidate_prefixes": _as_tag_prefixes,
    "item_repo_aliases": _as_item_repo_aliases,
}


def _parse_policy(base: BankPolicy, raw, where: str) -> BankPolicy:
    """Overlay `raw` onto `base` by key existence (absent key = inherit)."""
    if not isinstance(raw, dict):
        raise BankPolicyError(f"{where}: expected an object, got {type(raw).__name__}")
    unknown = sorted(set(raw) - _FIELD_NAMES)
    if unknown:
        raise BankPolicyError(
            f"{where}: unknown key(s) {unknown} — known keys are {sorted(_FIELD_NAMES)}"
        )
    overlay = {
        key: _VALIDATORS[key](raw[key], f"{where}.{key}")
        for key in raw  # key existence, NOT truthiness
    }
    merged = replace(base, **overlay)
    _check_alias_grammar(merged, where)
    _check_vocabulary(merged, where)
    _check_closed_prefixes(merged, where)
    return merged


class BankPolicies:
    """Resolved policy set: file defaults + per-bank overlays, with fallback."""

    def __init__(self, defaults: BankPolicy, banks: Optional[Dict[str, BankPolicy]] = None):
        self._defaults = defaults
        self._banks: Dict[str, BankPolicy] = dict(banks or {})

    @property
    def defaults(self) -> BankPolicy:
        return self._defaults

    def bank_names(self) -> frozenset:
        return frozenset(self._banks)

    def for_bank(self, bank: Optional[str]) -> BankPolicy:
        """Registered profile, else the loose defaults (never KeyError)."""
        return self._banks.get(bank or "", self._defaults)

    @classmethod
    def defaults_only(cls) -> "BankPolicies":
        """Loose baseline with no strict banks — the no-policy-file posture."""
        return cls(BankPolicy())

    @classmethod
    def from_dict(cls, data, *, source: str = "<dict>") -> "BankPolicies":
        if not isinstance(data, dict):
            raise BankPolicyError(f"{source}: expected an object at the top level, "
                                  f"got {type(data).__name__}")
        unknown = sorted(set(data) - _TOP_LEVEL_KEYS)
        if unknown:
            raise BankPolicyError(
                f"{source}: unknown top-level key(s) {unknown} — known keys are "
                f"{sorted(_TOP_LEVEL_KEYS)}"
            )
        defaults = _parse_policy(BankPolicy(), data.get("defaults", {}), f"{source}: defaults")
        return cls(defaults, _parse_banks(defaults, data.get("banks", {}), source))

    def with_local(self, data, *, source: str = "<dict>") -> "BankPolicies":
        """This set plus a local overlay {"banks": {...}}: a bank there is added, or replaces
        the same-named bank wholesale. It is parsed against THIS set's defaults."""
        if not isinstance(data, dict):
            raise BankPolicyError(f"{source}: expected an object at the top level, "
                                  f"got {type(data).__name__}")
        unknown = sorted(set(data) - _LOCAL_TOP_LEVEL_KEYS)
        if unknown:
            raise BankPolicyError(
                f"{source}: unknown top-level key(s) {unknown} in a local overlay - known "
                f"keys are {sorted(_LOCAL_TOP_LEVEL_KEYS)} (defaults stay in the tracked file)"
            )
        banks = dict(self._banks)
        banks.update(_parse_banks(self._defaults, data.get("banks", {}), source))
        return type(self)(self._defaults, banks)

    @classmethod
    def from_file(cls, path: str) -> "BankPolicies":
        """Parse + validate. Present-but-broken is fail-fast (never defaults)."""
        return cls.from_dict(_read_json(path), source=path)

    @classmethod
    def from_files(cls, path: str, local_path: Optional[str] = None) -> "BankPolicies":
        """Tracked file (must exist) + its local overlay when present (issue #187)."""
        return cls.from_file(path)._overlaid(path, local_path)

    def _overlaid(self, path: str, local_path: Optional[str]) -> "BankPolicies":
        explicit = local_path or os.environ.get("HC_BANK_POLICIES_LOCAL")
        local_path = explicit or local_overlay_path(path)
        if not os.path.exists(local_path):
            if explicit:
                # A configured overlay path that is missing is probably a typo or a missing
                # mount: say so, since the owner's banks would silently fall back to defaults.
                _LOG.warning("bank policy local overlay not found at %s (set explicitly) - "
                             "running the tracked file alone", local_path)
            return self
        return self.with_local(_read_json(local_path), source=local_path)

    @classmethod
    def load(cls, path: Optional[str] = None,
             local_path: Optional[str] = None) -> "BankPolicies":
        """Env-aware load (mirrors ACL.load). Absent file = defaults-only + warning;
        present file = strict validation. Absence is legitimate (CI pytest outside
        the container), corruption is not. The local overlay (`local_path`, else env
        HC_BANK_POLICIES_LOCAL, else policies/local/<name>) follows the same asymmetry."""
        path = path or os.environ.get("HC_BANK_POLICIES", DEFAULT_POLICY_FILE)
        if not os.path.exists(path):
            # Judge the warning on the RESULT: a local overlay may still declare strict banks.
            policies = cls.defaults_only()._overlaid(path, local_path)
            if policies.bank_names():
                _LOG.warning("bank policy file not found at %s - running code defaults plus the "
                             "local overlay banks %s", path, sorted(policies.bank_names()))
            else:
                _LOG.warning(
                    "bank policy file not found at %s - running defaults-only (loose: no tag/"
                    "prefix/strategy requirements, timestamp autofill on)", path
                )
            return policies
        return cls.from_files(path, local_path)


def _read_json(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise BankPolicyError(f"{path}: invalid JSON ({exc})") from exc


def _parse_banks(defaults: BankPolicy, raw_banks, source: str) -> Dict[str, BankPolicy]:
    if not isinstance(raw_banks, dict):
        raise BankPolicyError(f"{source}: 'banks' must be an object, "
                              f"got {type(raw_banks).__name__}")
    banks: Dict[str, BankPolicy] = {}
    for name, spec in raw_banks.items():
        if not isinstance(name, str) or not _BANK_NAME_RE.match(name):
            raise BankPolicyError(
                f"{source}: invalid bank name {name!r} (must match ^[A-Za-z0-9_-]{{1,128}}$)"
            )
        banks[name] = _parse_policy(defaults, spec, f"{source}: banks.{name}")
    return banks


# --- upstream drift compare (pure; the verify gate that calls it is N6) --------

def _upstream_strategy_names(raw) -> Optional[frozenset]:
    """Names declared upstream, or None when the shape is unrecognized."""
    if isinstance(raw, dict):
        return frozenset(str(k) for k in raw)
    if isinstance(raw, (list, tuple)):
        names = set()
        for item in raw:
            if isinstance(item, str):
                names.add(item)
            elif isinstance(item, dict) and isinstance(item.get("name"), str):
                names.add(item["name"])
            else:
                return None
        return frozenset(names)
    return None


def strategy_drift(bank_policy: BankPolicy, upstream_config: dict) -> List[str]:
    """Compare declared allowed_strategies against upstream `retain_strategies`.

    Accepts either the full GET /config response ({bank_id, config, overrides}) or a
    bare config object. Returns human-readable drift lines ([] = in sync).

    KEY EXISTENCE, never truthiness: upstream returns `retain_strategies` as a
    present key with a null value when nothing is configured (N0 실측), so a
    `if not config.get("retain_strategies")` gate would report the healthy null case
    and the genuinely-missing-key case identically.
    """
    cfg = upstream_config
    if isinstance(cfg, dict) and isinstance(cfg.get("config"), dict):
        cfg = cfg["config"]
    if not isinstance(cfg, dict):
        return [f"upstream config: expected an object, got {type(upstream_config).__name__}"]

    if "retain_strategies" not in cfg:
        return ["retain_strategies: key absent from upstream config (contract drift — "
                "v0.8.3 returns the key with a null value when nothing is configured)"]

    raw = cfg["retain_strategies"]
    declared = bank_policy.allowed_strategies

    if raw is None:
        # upstream: no strategies configured. Consistent with () and with None.
        if declared:
            return [f"retain_strategies: upstream value is null (no strategies configured) "
                    f"but policy declares {sorted(declared)}"]
        return []

    upstream_names = _upstream_strategy_names(raw)
    if upstream_names is None:
        return [f"retain_strategies: unexpected type {type(raw).__name__} "
                f"(expected object, array or null)"]
    if declared is None:
        return []  # null = unrestricted → nothing declared to compare

    out: List[str] = []
    missing = sorted(set(declared) - upstream_names)
    extra = sorted(upstream_names - set(declared))
    if missing:
        out.append(f"retain_strategies: policy declares {missing} but upstream has "
                   f"{sorted(upstream_names)}")
    if extra:
        out.append(f"retain_strategies: upstream declares {extra} not listed in policy "
                   f"allowed_strategies {sorted(declared)}")
    return out
