#!/usr/bin/env python3
"""item-backfill.py - invalidate the open handoff/todo memories that a completion memory closed (issue #322).

  item-backfill.py --bank <id> --aliases <json> --out <jsonl> [--dry-run | --apply [--targets <jsonl>]]
                   [--expect-aliases-sha256 <hex>] [--page-size N] [--batch-size N] [--batch-sleep S] [--allow-prod]
  item-backfill.py --bank <id> --check <jsonl> [--allow-prod]

The B treatment of the architecture v2 run 2 on the hc-next copy: a `kind:completion` memory and the older open
memories (handoff, status, todo, pending decision) that share its item key, e.g. `<repo>#<n>`, go stale together, so the
open ones are invalidated with the upstream memory invalidation (v1.1.0 `api/memories.mdx`: it drops the observations
derived from that memory and re-consolidates the ones that shared a source).

The rule is NOT here: it is gateway/item_keys.py (#320, `extract_keys`, `is_target`, `find_targets`, RULE_VERSION),
the same module the gateway hook (#321) uses. This script only feeds it. Its inputs are the bank's world and experience
memories (`GET .../memories/list?type=<t>`, every page; a set of ids that does not add up to the listing `total`, or a
row whose type is not the one asked for, is a refusal before anything is sent; an id listed under both types counts
once) and the repo alias table --aliases (a JSON object {alias: repo}, or a policy object holding it under
`item_repo_aliases`; --expect-aliases-sha256 refuses a file with another sha256). Nothing else: no answer key, no fixed
set.

Every valid completion is matched against every memory. A completion with more targets than
MAX_TARGETS_PER_COMPLETION is over the cap and invalidates nothing (its pairs are still listed, `over_cap: true`).
--out gets one JSON line per (completion, target) pair: completion id, target id, shared keys, target kinds, over_cap.
--dry-run (the default) sends nothing. --apply sends one `PATCH .../memories/{id}`
`{"state": "invalidated", "reason": "<RULE_VERSION> backfill"}` per distinct target of a completion under the cap, in
batches of --batch-size with --batch-sleep seconds between batches, and prints its start time first (the start of the
partial reconsolidation, the `--since` of `scope-migrate.py drain`). --targets <the reviewed dry-run --out file> makes
--apply refuse, before any PATCH, when the (completion, target) pairs it computes again differ from that file (both
sha256 in the message); --apply without it warns on stderr and sends what it computes. --check reads a --out file
back and counts the `state` of every listed target with `GET .../memories/{id}` (the LINEAGE `invalidations` counts).

Target and API key: the #242 contract through scripts/bank-config.py, as scripts/scope-migrate.py (HC_TARGET=next marks
the hc-next copy; standard call `next_env=$(bash scripts/next-stack.sh env) || exit 1; eval "$next_env"`). One `target:`
line goes to stderr. A target that is not the copy needs --allow-prod, refused before the first request. The upstream
API is reached from INSIDE the hindsight container; the key and the request go in on stdin, never on a command line.
Exit: 0 done, 1 refusal or failure.
"""
import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import math
import os
import re
import sys
import time
import urllib.parse

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("bank_config", os.path.join(_HERE, "bank-config.py"))
bank_config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bank_config)
Fail = bank_config.Fail
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "gateway"))
import item_keys  # noqa: E402  (standard library only, #320)

FACT_TYPES = ("world", "experience")
SLUG = re.compile(r"^[a-z0-9-]+$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def bank_path(bank, rest=""):
    return "/v1/default/banks/%s%s" % (urllib.parse.quote(bank, safe=""), rest)


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_aliases(path):
    """--aliases -> ({alias: repo}, sha256 of the file). Same shape check as bank_policies._as_item_repo_aliases."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        value = json.loads(raw)
    except (OSError, ValueError) as e:
        raise Fail("--aliases %s is not a readable JSON file: %s" % (path, e))
    if isinstance(value, dict) and isinstance(value.get("item_repo_aliases"), dict):
        value = value["item_repo_aliases"]
    if not isinstance(value, dict) or not value:
        raise Fail("--aliases must be a non-empty JSON object {alias: repo}")
    for k, v in value.items():
        if not (isinstance(k, str) and SLUG.match(k) and isinstance(v, str) and SLUG.match(v)):
            raise Fail("--aliases: %r -> %r is not a slug pair [a-z0-9-]+" % (k, v))
    return value, hashlib.sha256(raw).hexdigest()


def list_memories(container, key, bank, fact_type, page_size):
    """Every memory of one fact type, all pages; fail-closed unless the distinct ids add up to the listing total."""
    seen, offset, total = {}, 0, None
    while True:
        q = [("type", fact_type), ("limit", page_size), ("offset", offset)]
        page = bank_config.upstream(container, key, "GET", bank_path(bank, "/memories/list?" + urllib.parse.urlencode(q)))
        if not isinstance(page.get("total"), int):
            raise Fail("memories/list type=%s answered without an integer total; nothing was sent" % fact_type)
        if total is None:
            total = page["total"]
        elif page["total"] != total:
            raise Fail("memories/list type=%s total moved from %s to %s while paging; nothing was sent" % (
                fact_type, total, page["total"]))
        items = page.get("items") or []
        for it in items:
            got = it.get("fact_type", it.get("type"))
            if got != fact_type:                    # a server that ignores the filter must not double or mix the set
                raise Fail("memories/list type=%s returned a row of type %r; nothing was sent" % (fact_type, got))
            seen[it["id"]] = it
        offset += len(items)
        if not items or offset >= total:
            break
    if len(seen) != total:
        raise Fail("memories/list type=%s listed %d distinct memories but the total is %s; nothing was sent" % (
            fact_type, len(seen), total))
    for it in seen.values():
        if "state" not in it:
            raise Fail("memories/list type=%s returned a memory without `state`; nothing was sent" % fact_type)
    return list(seen.values())


def text(mem):
    for k in ("text", "content"):               # the same fallback as item_keys._text
        if isinstance(mem.get(k), str):
            return mem[k]
    return ""


def kinds(mem):
    return sorted(t for t in (mem.get("tags") or []) if isinstance(t, str) and t.startswith("kind:"))


def plan(memories, aliases):
    """-> (summary counts, pair rows, distinct target ids to invalidate in first-seen order)."""
    completions = [m for m in memories if item_keys.is_completion(m) and m.get("state") == "valid"]
    rows, apply_ids, keyed, capped = [], [], 0, 0
    for comp in completions:
        comp_keys = item_keys.extract_keys(text(comp), comp.get("tags"), aliases)
        if comp_keys:
            keyed += 1
        result = item_keys.find_targets(comp, memories, aliases)
        capped += result.over_cap
        for cand in result.targets:
            shared = comp_keys & item_keys.extract_keys(text(cand), cand.get("tags"), aliases)
            rows.append({"completion": comp["id"], "target": cand["id"], "kind": kinds(cand),
                         "keys": ["%s#%d" % k for k in sorted(shared)], "over_cap": result.over_cap})
        for cand in result.invalidate:
            if cand["id"] not in apply_ids:
                apply_ids.append(cand["id"])
    summary = {"memories": len(memories), "completions": len(completions), "completions_with_keys": keyed,
               "pairs": len(rows), "targets": len(apply_ids), "over_cap_completions": capped}
    return summary, rows, apply_ids


def pair_digest(pairs):
    """sha256 of the sorted, distinct (completion id, target id) pairs, one `<completion> <target>` line each."""
    return hashlib.sha256("".join("%s %s\n" % p for p in sorted(set(pairs))).encode()).hexdigest()


def read_pairs(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return {(r["completion"], r["target"]) for r in (json.loads(ln) for ln in fh if ln.strip())}
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise Fail("--targets %s is not a readable --out JSONL file: %s" % (path, e))


def cmd_backfill(args, container, key):
    aliases, digest = load_aliases(args.aliases)
    reviewed = read_pairs(args.targets) if args.targets else None   # read before --out, which may be the same path
    memories = {}
    for fact_type in FACT_TYPES:
        for mem in list_memories(container, key, args.bank, fact_type, args.page_size):
            memories.setdefault(mem["id"], mem)
    summary, rows, ids = plan(list(memories.values()), aliases)
    summary.update({"rule_version": item_keys.RULE_VERSION, "aliases_sha256": digest,
                    "mode": "apply" if args.apply else "dry-run"})
    with open(args.out, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    os.chmod(args.out, 0o600)
    print("summary: " + json.dumps(summary, sort_keys=True))
    if not args.apply:
        print("dry-run: nothing sent (--apply sends the PATCH)")
        return 0
    computed = {(r["completion"], r["target"]) for r in rows}
    if reviewed is None:
        print("item-backfill: warning: --apply without --targets sends the list it computed now, not a reviewed "
              "dry-run list (pairs sha256 %s)" % pair_digest(computed), file=sys.stderr)
    elif computed != reviewed:
        raise Fail("the pairs computed now (sha256 %s, %d) differ from --targets %s (sha256 %s, %d); nothing was sent"
                   % (pair_digest(computed), len(computed), args.targets, pair_digest(reviewed), len(reviewed)))
    body = {"state": "invalidated", "reason": "%s backfill" % item_keys.RULE_VERSION}
    batches = [ids[i:i + args.batch_size] for i in range(0, len(ids), args.batch_size)]
    print("apply: start %s, %d targets in %d batches of up to %d" % (now_iso(), len(ids), len(batches),
                                                                     args.batch_size))
    sys.stdout.flush()
    done = 0
    for n, batch in enumerate(batches, 1):
        if n > 1 and args.batch_sleep:
            time.sleep(args.batch_sleep)
        for mid in batch:
            try:
                bank_config.upstream(container, key, "PATCH",
                                     bank_path(args.bank, "/memories/" + urllib.parse.quote(mid, safe="")), body)
            except Fail as e:
                raise Fail("%s (invalidated %d/%d before it)" % (e, done, len(ids)))
            done += 1
        print("batch %d/%d: %d invalidated, %d/%d done at %s" % (n, len(batches), len(batch), done, len(ids),
                                                                 now_iso()))
        sys.stdout.flush()
    print("apply: end %s" % now_iso())
    return 0


def cmd_check(args, container, key):
    try:
        with open(args.check, encoding="utf-8") as fh:
            rows = [json.loads(ln) for ln in fh if ln.strip()]
    except (OSError, ValueError) as e:
        raise Fail("--check %s is not a readable JSONL file: %s" % (args.check, e))
    ids = []
    for row in rows:
        if not row.get("over_cap") and row["target"] not in ids:
            ids.append(row["target"])
    states = {}
    for mid in ids:
        mem = bank_config.upstream(container, key, "GET", bank_path(args.bank, "/memories/" + urllib.parse.quote(
            mid, safe="")))
        state = mem.get("state") if isinstance(mem.get("state"), str) else "(missing)"
        states[state] = states.get(state, 0) + 1
    print("check: " + json.dumps({"at": now_iso(), "targets": len(ids), "states": states}, sort_keys=True))
    return 0


def positive_int(text):
    n = int(text)
    if n < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return n


def nonnegative_float(text):
    x = float(text)
    if not (math.isfinite(x) and x >= 0):       # also refuses nan and inf (time.sleep(inf) overflows mid-apply)
        raise argparse.ArgumentTypeError("must be 0 or more")
    return x


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--bank", required=True, help="bank id")
    ap.add_argument("--aliases", help="repo alias table, JSON {alias: repo} (a file outside the repo)")
    ap.add_argument("--out", help="write the (completion, target) pairs here as JSONL (mode 0600)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="list the targets only (the default)")
    mode.add_argument("--apply", action="store_true", help="send the invalidation PATCH")
    mode.add_argument("--check", metavar="JSONL", help="count the state of every target of a --out file")
    ap.add_argument("--targets", metavar="JSONL", help="--apply only: the reviewed dry-run --out file; refuse if the "
                    "pairs computed now differ")
    ap.add_argument("--expect-aliases-sha256", metavar="HEX", help="refuse an --aliases file with another sha256")
    ap.add_argument("--page-size", type=positive_int, default=500, help="memories/list page size")
    ap.add_argument("--batch-size", type=positive_int, default=20, help="memories per batch")
    ap.add_argument("--batch-sleep", type=nonnegative_float, default=0.0, help="seconds between batches")
    ap.add_argument("--allow-prod", action="store_true", help="let this run on a target that is not the copy")
    args = ap.parse_args(argv)
    try:
        if not bank_config.BANK_ID.match(args.bank):
            raise Fail("--bank %r is not a plain bank id" % args.bank)
        if not args.check:
            if not args.aliases or not args.out:
                raise Fail("--aliases and --out are required unless --check")
            _table, digest = load_aliases(args.aliases)   # a malformed table is refused before the target is touched
            if args.expect_aliases_sha256 is not None:
                want = args.expect_aliases_sha256.strip().lower()
                if not HEX64.match(want):
                    raise Fail("--expect-aliases-sha256 %r is not a 64-digit hex sha256" % args.expect_aliases_sha256)
                if digest != want:
                    raise Fail("--aliases %s has sha256 %s, not --expect-aliases-sha256 %s; nothing was sent" % (
                        args.aliases, digest, want))
        if args.targets and not args.apply:
            raise Fail("--targets goes with --apply")
        container, is_copy, key = bank_config.resolve_target()
        if not is_copy and not args.allow_prod:
            raise Fail("item-backfill on a target that is not the hc-next copy needs --allow-prod; nothing was sent")
        return (cmd_check if args.check else cmd_backfill)(args, container, key)
    except Fail as e:
        print("item-backfill: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
