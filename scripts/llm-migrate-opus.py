#!/usr/bin/env python3
"""Move an env file's LLM block to the hindsight v1.0.0 target: claude-code / claude-opus-5-5 with a two-member
failover chain (account1, then account2) on every slot.

    llm-migrate-opus.py --env-file <path> [--apply]

Target keys (full names):
    HINDSIGHT_API_LLM_PROVIDER=claude-code          HINDSIGHT_API_LLM_MODEL=claude-opus-5-5
    HINDSIGHT_API_LLM_API_KEY=<today's LLM_1 key>   (account1)
    HINDSIGHT_API_LLM_STRATEGY='{"mode":"failover"}'
    HINDSIGHT_API_LLM_1_PROVIDER=claude-code        HINDSIGHT_API_LLM_1_MODEL=claude-opus-5-5
    HINDSIGHT_API_LLM_1_API_KEY=<today's LLM_2 key> (account2)
    HINDSIGHT_API_MENTAL_MODEL_REFRESH_LLM_STRATEGY='{"mode":"failover"}'
    HINDSIGHT_API_MENTAL_MODEL_REFRESH_LLM_1_PROVIDER=claude-code
    HINDSIGHT_API_MENTAL_MODEL_REFRESH_LLM_1_MODEL=claude-opus-5-5
    HINDSIGHT_API_MENTAL_MODEL_REFRESH_LLM_1_API_KEY=<same as HINDSIGHT_API_LLM_1_API_KEY> (account2)
Removed: every HINDSIGHT_API_LLM_2_*, HINDSIGHT_API_LLM_REASONING_EFFORT (claude-code ignores it), every slot key
HINDSIGHT_API_<SLOT>_LLM_* (slots then follow the global chain) except the four MENTAL_MODEL_REFRESH keys above,
every key with XAI in its name.

Why the four mental-model-refresh keys: hindsight v1.0.0 (memory_engine.py 2885-2956) builds a refresh-only LLM when
reflect's timeout (default 30.0) differs from the global llm_timeout (120.0), and that one inherits chain members from
the REFLECT_ slot only, never from the global chain - so without these keys the automatic refresh runs on the first
account alone, with no fallback.

Order of judgment: first "is the input already the target" (every target key matches, nothing to remove), only then
the validation of the old shape (LLM_1/LLM_2 providers must be claude-code, both tokens present and different).
Default is a dry run that prints key NAMES only (+ added, - removed, ~ changed); values are never printed. --apply
writes <env-file>.bak-<timestamp> (0600, byte-identical to the input) first, then replaces the file atomically
(temp file in the same directory + os.replace). The strategy is written single-quoted: a shell `source` and a
compose env_file then read the same JSON.

Refused as well: any key under HINDSIGHT_API_LLM_1_/_2_ other than PROVIDER, MODEL, API_KEY, and any member 3 or up (their
move would be a guess; the key names are printed, nothing is written).
--classify only reads: it prints which layout the file has (scripts/next-stack.sh uses it).

exit: 0 done / already migrated; 1 input refused or a file error; 2 usage (argparse).
Applying it to production is a human-gated step (#249); run it on a copy or a test file.
"""
import argparse
import json
import os
import re
import sys
import tempfile
import time

PROVIDER, MODEL = "claude-code", "claude-sonnet-5-5"   # 2026-11-01 opus-5-5 -> sonnet-5-5 (운영 전환과 함께)
STRATEGY = {"mode": "failover"}
STRATEGY_LINE = "'" + json.dumps(STRATEGY, separators=(",", ":")) + "'"
K = "HINDSIGHT_API_LLM_"
MM = "HINDSIGHT_API_MENTAL_MODEL_REFRESH_LLM_"
MM_KEYS = (MM + "STRATEGY", MM + "1_PROVIDER", MM + "1_MODEL", MM + "1_API_KEY")   # the slot keys that stay
LINE_RE = re.compile(r"^(?P<export>export[ \t]+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<raw>.*)$")
SLOT_RE = re.compile(r"^HINDSIGHT_API_([A-Z0-9]+_)+LLM_")
MEMBER_RE = re.compile(r"^HINDSIGHT_API_LLM_([0-9]+)_(.*)$")
MEMBER_FIELDS = ("PROVIDER", "MODEL", "API_KEY")   # the only keys of members 1 and 2 whose move is defined


class Refused(Exception):
    """The input cannot be migrated. The message names keys, never values."""


def unquote(raw):
    v = raw.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        q, v = v[0], v[1:-1]
        if q == '"':                       # a shell reads \" \\ \$ \` inside double quotes as the bare character
            v = re.sub(r'\\(["\\$`])', r"\1", v)
    return v


def strip_comment(raw):
    """The value token of the text after "=": a quoted value ends at its closing quote, an unquoted one at the first
    whitespace + '#' (what a shell source and compose env_file read); an inline comment never belongs to the value."""
    v = raw.strip()
    if v[:1] in ("'", '"'):
        q, i = v[0], 1
        while i < len(v):
            if v[i] == "\\" and q == '"':
                i += 2
                continue
            if v[i] == q:
                return v[:i + 1]
            i += 1
        return v
    m = re.search(r"\s#", v)
    return (v[:m.start()] if m else v).rstrip()


def parse(text):
    """-> (lines, positions): lines keep their line ending; positions maps KEY -> [line index...]."""
    lines = text.splitlines(keepends=True)
    pos = {}
    for i, line in enumerate(lines):
        m = LINE_RE.match(line.rstrip("\r\n"))
        if m:
            pos.setdefault(m.group("key"), []).append(i)
    return lines, pos


def value(lines, pos, key):
    """The value token of a key (the last assignment wins, as in a shell source; an inline comment is dropped), or None when absent."""
    if key not in pos:
        return None
    return strip_comment(LINE_RE.match(lines[pos[key][-1]].rstrip("\r\n")).group("raw"))


def is_removed(key):
    if key in MM_KEYS:
        return False
    return (key.startswith(K + "2_") or key == K + "REASONING_EFFORT" or bool(SLOT_RE.match(key))
            or "XAI" in key.upper())


def strategy_is_target(raw):
    try:
        return json.loads(unquote(raw)) == STRATEGY
    except (ValueError, TypeError):
        return False


def is_target(lines, pos, ignore_removed=False):
    """Every target key matches (quotes do not matter, the strategy is compared as JSON) and nothing is left to remove."""
    if not ignore_removed and any(is_removed(k) for k in pos):
        return False
    get = lambda k: value(lines, pos, k)
    if get(K + "PROVIDER") is None or unquote(get(K + "PROVIDER")) != PROVIDER:
        return False
    if get(K + "MODEL") is None or unquote(get(K + "MODEL")) != MODEL:
        return False
    if get(K + "STRATEGY") is None or not strategy_is_target(get(K + "STRATEGY")):
        return False
    if get(K + "1_PROVIDER") is None or unquote(get(K + "1_PROVIDER")) != PROVIDER:
        return False
    if get(K + "1_MODEL") is None or unquote(get(K + "1_MODEL")) != MODEL:
        return False
    if get(MM + "STRATEGY") is None or not strategy_is_target(get(MM + "STRATEGY")):
        return False
    if get(MM + "1_PROVIDER") is None or unquote(get(MM + "1_PROVIDER")) != PROVIDER:
        return False
    if get(MM + "1_MODEL") is None or unquote(get(MM + "1_MODEL")) != MODEL:
        return False
    account1, account2, mm_account2 = get(K + "API_KEY"), get(K + "1_API_KEY"), get(MM + "1_API_KEY")
    return bool(account1 and unquote(account1) and account2 and unquote(account2) and unquote(account1) != unquote(account2)
                and mm_account2 is not None and unquote(mm_account2) == unquote(account2))


def unmovable_member_keys(pos):
    """Key names under HINDSIGHT_API_LLM_<n>_ that the move does not define: anything but PROVIDER/MODEL/API_KEY of
    members 1 and 2, and every member 3 and up. The script does not guess where those belong."""
    out = []
    for k in pos:
        m = MEMBER_RE.match(k)
        if m and (int(m.group(1)) >= 3 or (int(m.group(1)) in (1, 2) and m.group(2) not in MEMBER_FIELDS)):
            out.append(k)
    return sorted(out)


def validate_old_shape(lines, pos, strict_extras=True):
    get = lambda k: value(lines, pos, k)
    if strict_extras:
        odd = unmovable_member_keys(pos)
        if odd:
            raise Refused("keys with no defined move (remove or migrate them by hand): %s" % " ".join(odd))
    for k in (K + "1_PROVIDER", K + "2_PROVIDER"):
        if get(k) is None or unquote(get(k)) != PROVIDER:
            raise Refused("%s is missing or not %s" % (k, PROVIDER))
    for k in (K + "1_API_KEY", K + "2_API_KEY"):
        if get(k) is None or not unquote(get(k)):
            raise Refused("%s is missing or empty" % k)
    if unquote(get(K + "1_API_KEY")) == unquote(get(K + "2_API_KEY")):
        raise Refused("%s and %s hold the same token (the chain would collapse)" % (K + "1_API_KEY", K + "2_API_KEY"))


def layout(lines, pos):
    """'target' or 'old' (the pre-migration LLM_1 = account1, LLM_2 = account2 shape; keys with no defined move are tolerated here),
    else Refused. Used by scripts/next-stack.sh to read the production file in either shape."""
    if is_target(lines, pos):
        return "target"
    validate_old_shape(lines, pos, strict_extras=False)
    return "old"


def plan(lines, pos):
    """-> (changes [(sign, key)], new_lines). Pure: nothing is written."""
    get = lambda k: value(lines, pos, k)
    targets = [  # (key, raw to write, compare as)
        (K + "PROVIDER", PROVIDER, PROVIDER),
        (K + "MODEL", MODEL, MODEL),
        (K + "API_KEY", get(K + "1_API_KEY"), unquote(get(K + "1_API_KEY"))),
        (K + "STRATEGY", STRATEGY_LINE, None),
        (K + "1_PROVIDER", PROVIDER, PROVIDER),
        (K + "1_MODEL", MODEL, MODEL),
        (K + "1_API_KEY", get(K + "2_API_KEY"), unquote(get(K + "2_API_KEY"))),
        (MM + "STRATEGY", STRATEGY_LINE, None),
        (MM + "1_PROVIDER", PROVIDER, PROVIDER),
        (MM + "1_MODEL", MODEL, MODEL),
        (MM + "1_API_KEY", get(K + "2_API_KEY"), unquote(get(K + "2_API_KEY"))),
    ]
    changes, replace, drop, append = [], {}, set(), []
    for key, raw, cmp in targets:
        old = get(key)
        if old is None:
            changes.append(("+", key)); append.append("%s=%s" % (key, raw)); continue
        # a strategy that is the right JSON but not single-quoted would lose its quotes in a shell `source`: rewrite it
        same = old.strip() == STRATEGY_LINE if cmp is None else unquote(old) == cmp
        if same:
            continue
        changes.append(("~", key))
        idx = pos[key]
        exp = LINE_RE.match(lines[idx[-1]].rstrip("\r\n")).group("export") or ""
        replace[idx[-1]] = "%s%s=%s\n" % (exp, key, raw)
        drop.update(idx[:-1])
    for key in sorted(k for k in pos if is_removed(k)):
        changes.append(("-", key)); drop.update(pos[key])
    out = []
    for i, line in enumerate(lines):
        if i in drop:
            continue
        out.append(replace.get(i, line))
    if append:
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        out.extend(a + "\n" for a in append)
    return changes, out


def apply(path, original, new_lines):
    """Backup first (0600, O_EXCL), then atomic replace. Returns the backup path."""
    backup = "%s.bak-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(original)
        fh.flush(); os.fsync(fh.fileno())
    real = os.path.realpath(path)
    mode = os.stat(real).st_mode & 0o777
    tfd, tmp = tempfile.mkstemp(dir=os.path.dirname(real), prefix=".llm-migrate-")
    try:
        with os.fdopen(tfd, "wb") as fh:
            fh.write("".join(new_lines).encode("utf-8"))
            fh.flush(); os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, real)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return backup


def main(argv=None):
    ap = argparse.ArgumentParser(description="Move an env file's LLM block to claude-code/claude-opus-5-5 (account1 -> account2).")
    ap.add_argument("--env-file", required=True, help="env file to migrate (no default)")
    ap.add_argument("--apply", action="store_true", help="write the result (default: dry run, key names only)")
    ap.add_argument("--classify", action="store_true",
                    help="read only: print 'layout: target' or 'layout: old', exit 1 (reason on stderr) for anything else")
    a = ap.parse_args(argv)
    try:
        with open(a.env_file, "rb") as fh:
            original = fh.read()
        lines, pos = parse(original.decode("utf-8"))
    except (OSError, UnicodeDecodeError) as e:
        print("llm-migrate-opus: cannot read %s: %s" % (a.env_file, type(e).__name__), file=sys.stderr)
        return 1
    if a.classify:
        if a.apply:
            ap.error("--classify and --apply exclude each other")
        try:
            print("layout: %s" % layout(lines, pos))
        except Refused as e:
            print("llm-migrate-opus: neither the pre-migration layout nor the target layout: %s" % e, file=sys.stderr)
            return 1
        return 0
    if is_target(lines, pos):
        print("already migrated: %s" % a.env_file)
        return 0
    try:
        validate_old_shape(lines, pos)
    except Refused as e:
        print("llm-migrate-opus: refused: %s" % e, file=sys.stderr)
        left = sorted(k for k in pos if is_removed(k))
        if left and is_target(lines, pos, ignore_removed=True):
            print("llm-migrate-opus: the target keys are already in place; only these keys are left over, remove them by hand: %s"
                  % " ".join(left), file=sys.stderr)
        return 1
    changes, new_lines = plan(lines, pos)
    for sign, key in changes:
        print("%s %s" % (sign, key))
    if not a.apply:
        print("dry-run: %d change(s); rerun with --apply to write" % len(changes))
        return 0
    try:
        backup = apply(a.env_file, original, new_lines)
    except OSError as e:
        print("llm-migrate-opus: write failed: %s" % type(e).__name__, file=sys.stderr)
        return 1
    print("applied: %d change(s); backup %s" % (len(changes), backup))
    return 0


if __name__ == "__main__":
    sys.exit(main())
