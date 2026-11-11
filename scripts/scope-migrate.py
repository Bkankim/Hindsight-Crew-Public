#!/usr/bin/env python3
"""scope-migrate.py - move a bank's documents to new observation scopes, then watch the rebuild drain (issue #298).

  scope-migrate.py migrate --bank <id> --scopes <value> [--tag T ...] [--tags-match M] [--page-size N]
                           [--dry-run | --apply] [--batch-size N] [--batch-sleep S] [--allow-prod]
  scope-migrate.py drain   --bank <id> [--interval S] [--timeout S] [--max-llm N] [--since ISO]
                           [--timeseries <jsonl>] [--allow-prod]

migrate lists every document of the bank (all pages, optionally filtered by tag) BEFORE it sends anything, and refuses
when the rows it collected do not match the listing's `total`: the target set must be known. Then, with --apply, it
sends one `PATCH .../documents/{id}` `{"observation_scopes": <value>}` per document, in batches of --batch-size with
--batch-sleep seconds between batches. --dry-run (the default) only counts the targets. <value> is either a JSON list of
lists of tags (e.g. '[["project:*"]]') or one of the scope strings (`combined` is the rollback to the default), and goes
to the server exactly as given. Upstream (observations.mdx "Changing the Scopes", v1.1.0): a moved memory loses its
observations until the next consolidation rebuilds them in the new scope; re-sending the current value queues nothing,
so a run cut short can simply be run again.

drain polls the bank until no operation is pending or processing and pending_consolidation is 0, after it has seen
consolidation work (a pending or processing operation, pending_consolidation above 0, or a consolidation llm-requests
row since --since; --allow-empty skips this). The --max-llm and --timeout caps are checked before that success, and
--timeout counts from --since when it is given. Fail-closed: every
poll checks that the five operation states (pending, processing, completed, failed, cancelled) of the bank stats add up
to the operations list `total`; a sum that still disagrees after 3 re-reads 2s apart within the poll (an operation
queued between the two reads), or a state outside the five, is exit 1. Each poll is one JSON line on
stdout (and appended to --timeseries): observations, pending_consolidation, the five states, and the LLM requests since
--since (default: drain start) from `GET .../llm-requests` (every row of the window is read and classified): all rows,
local embedding rows (provider `local`), reranks (provider `tei`), generative rows (any other provider), and the rows of
the consolidation stage (operations named consolidation*, e.g. consolidation and consolidation_dedup), all and generative
only. Every --scope-every polls, and on the last one, the row also carries `scopes`: the number of distinct tag sets over
all observations (read with `GET .../memories/list?type=observation`); the polls in between, and a poll whose
count fails (logged, the drain goes on), carry null.
--max-llm caps the generative consolidation-stage calls (#296 decision B: the local embedding rows, two per call, cost
nothing and do not count, nor do page refreshes); --count-all puts the cap on all rows instead.
Exit: 0 drained, 1 refusal or failure, 2 --timeout reached, 3 --max-llm reached (the watcher stops; the server goes on).

Target and API key: the #242 contract through scripts/bank-config.py (HC_TARGET=next marks the hc-next copy; standard
call `next_env=$(bash scripts/next-stack.sh env) || exit 1; eval "$next_env"`). One `target:` line goes to stderr. Any
command on a target that is not the copy needs --allow-prod, refused before the first request. The upstream API is
reached from INSIDE the hindsight container; the key and the request go in on stdin, never on a command line.
"""
import argparse
import datetime as dt
import importlib.util
import json
import math
import os
import sys
import time
import urllib.parse

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("bank_config", os.path.join(_HERE, "bank-config.py"))
bank_config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bank_config)
Fail = bank_config.Fail

SCOPE_STRINGS = ("per_tag", "combined", "all_combinations", "shared")
FIVE = ("pending", "processing", "completed", "failed", "cancelled")
NON_GENERATIVE = ("local", "tei")                  # llm-requests providers of embedding and rerank rows, not LLM calls
# stats and the operations list are two reads: an operation queued between them (a page refresh right after a
# consolidation) makes the sums disagree for a moment. A poll re-reads both up to RECHECKS times, RECHECK_SLEEP apart.
RECHECKS = 3
RECHECK_SLEEP = 2.0                                # SCOPE_MIGRATE_RECHECK_SLEEP overrides it (tests set 0), see main()


def parse_scopes(text):
    """--scopes -> the value sent as `observation_scopes`: a scope string, or a non-empty list of non-empty tag lists."""
    value = text if text in SCOPE_STRINGS else None
    if value is None:
        try:
            value = json.loads(text)
        except ValueError:
            raise Fail("--scopes %r is neither a scope string (%s) nor JSON" % (text, ", ".join(SCOPE_STRINGS)))
    if isinstance(value, str):
        if value not in SCOPE_STRINGS:
            raise Fail("--scopes %r is not one of %s" % (value, ", ".join(SCOPE_STRINGS)))
        return value
    if (not isinstance(value, list) or not value
            or not all(isinstance(s, list) and s and all(isinstance(t, str) and t for t in s) for s in value)):
        raise Fail('--scopes must be a list of non-empty lists of tags, e.g. [["project:*"]]; got %s' % text)
    return value


def bank_path(bank, rest=""):
    return "/v1/default/banks/%s%s" % (urllib.parse.quote(bank, safe=""), rest)


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- migrate


def list_documents(container, key, bank, tags, tags_match, page_size):
    ids, seen, offset, total = [], set(), 0, None
    while True:
        q = [("limit", page_size), ("offset", offset)] + [("tags", t) for t in tags]
        if tags:
            q.append(("tags_match", tags_match))
        page = bank_config.upstream(container, key, "GET", bank_path(bank, "/documents?" + urllib.parse.urlencode(q)))
        if page.get("total_is_estimate"):
            raise Fail("the listing total is an estimate; narrow the filter")
        if total is None:
            total = page.get("total")
        elif page.get("total") != total:
            raise Fail("the listing total moved from %s to %s while paging; nothing was sent" % (total, page.get("total")))
        items = page.get("items") or []
        for it in items:
            if it["id"] not in seen:
                seen.add(it["id"])
                ids.append(it["id"])
        offset += len(items)
        if not items or len(ids) >= (total or 0):
            break
    if len(ids) != total:
        raise Fail("listed %d documents but the listing total is %s; nothing was sent" % (len(ids), total))
    return ids


def cmd_migrate(args, container, key):
    scopes = parse_scopes(args.scopes)
    ids = list_documents(container, key, args.bank, args.tag, args.tags_match, args.page_size)
    batches = [ids[i:i + args.batch_size] for i in range(0, len(ids), args.batch_size)]
    print("targets: %d documents in %d batches of up to %d, scopes %s" % (
        len(ids), len(batches), args.batch_size, bank_config.canon(scopes)))
    if not args.apply:
        print("dry-run: nothing sent (--apply sends the PATCH)")
        return 0
    done = 0
    for n, batch in enumerate(batches, 1):
        if n > 1 and args.batch_sleep:
            time.sleep(args.batch_sleep)
        for doc in batch:
            try:
                bank_config.upstream(container, key, "PATCH", bank_path(args.bank, "/documents/" + urllib.parse.quote(
                    doc, safe="")), {"observation_scopes": scopes})
            except Fail as e:
                raise Fail("%s (moved %d/%d before it; running again re-sends the same value safely)" % (
                    e, done, len(ids)))
            done += 1
        print("batch %d/%d: %d patched, %d/%d done at %s" % (n, len(batches), len(batch), done, len(ids), now_iso()))
        sys.stdout.flush()
    return 0


# ---------------------------------------------------------------- drain


def list_paged(container, key, path, query, page_size, what):
    """Every item of a paged listing, deduplicated by id; fail-closed unless the ids add up to the listing total."""
    for _attempt in range(3):                   # rows written while paging shift the offsets; read the window again
        seen, offset, total = {}, 0, None
        while True:
            page = bank_config.upstream(container, key, "GET", path + "?" + urllib.parse.urlencode(
                query + [("limit", page_size), ("offset", offset)]))
            if not isinstance(page.get("total"), int):
                raise Fail("drain: %s answered without an integer total" % what)
            total = page["total"]
            items = page.get("items") or []
            for it in items:
                seen[it["id"]] = it
            offset += len(items)
            if not items or offset >= total:
                break
        if len(seen) == total:
            return list(seen.values())
    raise Fail("drain: %s listed %d distinct rows but reports a total of %s" % (what, len(seen), total))


def llm_counts(rows):
    """llm-requests rows -> the drain counts. Generative = a provider outside NON_GENERATIVE."""
    gen = [r for r in rows if r.get("provider") not in NON_GENERATIVE]
    stage = lambda r: str(r.get("operation") or "").startswith("consolidation")
    return {"llm_requests": len(rows), "llm_embedding": sum(1 for r in rows if r.get("provider") == "local"),
            "llm_rerank": sum(1 for r in rows if r.get("provider") == "tei"), "llm_generative": len(gen),
            "llm_consolidation": sum(1 for r in rows if stage(r)),
            "llm_consolidation_generative": sum(1 for r in gen if stage(r))}


def count_scopes(container, key, bank):
    obs = list_paged(container, key, bank_path(bank, "/memories/list"), [("type", "observation")], 1000,
                     "memories/list")
    return len({tuple(sorted(o.get("tags") or [])) for o in obs})


def read_states(container, key, bank):
    stats = bank_config.upstream(container, key, "GET", bank_path(bank, "/stats"))
    listing = bank_config.upstream(container, key, "GET", bank_path(bank, "/operations?limit=1"))
    by_status = stats.get("operations_by_status") or {}
    return stats, listing, sorted(set(by_status) - set(FIVE)), {k: by_status.get(k, 0) for k in FIVE}


def poll(container, key, bank, since, started):
    stats, listing, unknown, states = read_states(container, key, bank)
    for _recheck in range(RECHECKS):            # only a sum mismatch is re-read; an unknown state fails at once
        if unknown or sum(states.values()) == listing.get("total"):
            break
        time.sleep(RECHECK_SLEEP)
        stats, listing, unknown, states = read_states(container, key, bank)
    row = {"at": now_iso(), "elapsed_s": round(time.monotonic() - started, 1),
           "observations": stats.get("total_observations"), "pending_consolidation": stats.get("pending_consolidation"),
           "operations": states, "operations_total": listing.get("total")}
    row.update(llm_counts(list_paged(container, key, bank_path(bank, "/llm-requests"),
                                     [("start_date", since), ("end_date", row["at"])], 500, "llm-requests")))
    row["scopes"] = None
    if unknown or sum(states.values()) != row["operations_total"]:
        return row, "the five operation states add up to %d but the operations total is %s%s" % (
            sum(states.values()), row["operations_total"],
            "; states outside the five: %s" % ", ".join(unknown) if unknown else "")
    return row, None


def parse_since(text):
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        raise Fail("--since %r is not an ISO date-time" % text)


def cmd_drain(args, container, key):
    since, started = args.since or now_iso(), time.monotonic()
    # the --timeout clock starts at --since (the reconsolidation start) when given, else at the drain start
    deadline = (parse_since(args.since) if args.since else time.time()) + args.timeout
    sink = open(args.timeseries, "a", encoding="utf-8") if args.timeseries else None
    try:
        n, seen_work = 0, args.allow_empty
        while True:
            row, mismatch = poll(container, key, args.bank, since, started)
            ops = row["operations"]
            idle = ops["pending"] == 0 and ops["processing"] == 0 and row["pending_consolidation"] == 0
            # an idle bank only counts as drained once the rebuild was seen: right after the PATCH the first poll
            # can come before the consolidation is queued
            seen_work = seen_work or not idle or row["llm_consolidation"] > 0
            drained = idle and seen_work
            counted, what = ((row["llm_requests"], "llm-requests rows") if args.count_all
                             else (row["llm_consolidation_generative"], "generative consolidation calls"))
            capped = bool(args.max_llm) and counted >= args.max_llm
            timed_out = time.time() >= deadline       # decided before the scope count, which can be slow
            if not mismatch and (drained or capped or timed_out or n % args.scope_every == 0):
                try:
                    row["scopes"] = count_scopes(container, key, args.bank)
                except Fail as e:                     # an auxiliary count: the row keeps scopes=null
                    print("drain: scope count failed, scopes=null for this poll: %s" % e, file=sys.stderr)
                timed_out = timed_out or time.time() >= deadline
            n += 1
            line = json.dumps(row, sort_keys=True)
            print(line)
            sys.stdout.flush()
            if sink:
                sink.write(line + "\n")
                sink.flush()
            if mismatch:
                raise Fail("drain: %s (fail-closed)" % mismatch)
            # the caps are checked before success: a drain that finishes past a cap still overran it
            if capped:
                print("drain: LLM cap reached: %d %s since %s >= --max-llm %d (%d rows in all, %d generative); "
                      "the server keeps consolidating" % (counted, what, since, args.max_llm, row["llm_requests"],
                                                          row["llm_generative"]), file=sys.stderr)
                return 3
            if timed_out:
                print("drain: timeout after %ss with %d pending, %d processing, %s pending_consolidation" % (
                    row["elapsed_s"], ops["pending"], ops["processing"], row["pending_consolidation"]), file=sys.stderr)
                return 2
            if drained:
                print("drain: drained after %ss, %s LLM requests since %s (generative %s, generative consolidation "
                      "%s, local embedding %s), %s observations in %s scopes" % (
                          row["elapsed_s"], row["llm_requests"], since, row["llm_generative"],
                          row["llm_consolidation_generative"], row["llm_embedding"], row["observations"],
                          row["scopes"]), file=sys.stderr)
                return 0
            time.sleep(max(0.0, min(args.interval, deadline - time.time())))
    finally:
        if sink:
            sink.close()


def positive_int(text):
    n = int(text)
    if n < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return n


def nonnegative_int(text):
    n = int(text)
    if n < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return n


def nonnegative_float(text):
    x = float(text)
    if not x >= 0:                              # also refuses nan
        raise argparse.ArgumentTypeError("must be 0 or more")
    return x


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    mig = sub.add_parser("migrate", help="PATCH observation_scopes on every listed document")
    mig.add_argument("--scopes", required=True, help='JSON list of tag lists (e.g. [["project:*"]]) or a scope string')
    mig.add_argument("--tag", action="append", default=[], help="list only documents with this tag (repeatable)")
    mig.add_argument("--tags-match", default="any_strict", choices=("any", "all", "any_strict", "all_strict", "exact"))
    mig.add_argument("--page-size", type=positive_int, default=100, help="listing page size")
    mode = mig.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="count the targets only (the default)")
    mode.add_argument("--apply", action="store_true", help="send the PATCH")
    mig.add_argument("--batch-size", type=positive_int, default=50, help="documents per batch")
    mig.add_argument("--batch-sleep", type=nonnegative_float, default=0.0, help="seconds between batches")
    dr = sub.add_parser("drain", help="wait for pending and processing to reach 0, recording a timeseries")
    dr.add_argument("--interval", type=nonnegative_float, default=30.0, help="seconds between polls")
    dr.add_argument("--timeout", type=nonnegative_float, default=4 * 3600.0,
                    help="give up this many seconds after --since, or after the drain start without it (exit 2)")
    dr.add_argument("--allow-empty", action="store_true",
                    help="count an idle bank as drained even if no consolidation work was seen since the start")
    dr.add_argument("--max-llm", type=nonnegative_int, default=0,
                    help="stop watching at this many generative consolidation-stage calls (exit 3, 0 = off)")
    dr.add_argument("--count-all", action="store_true", help="--max-llm counts every llm-requests row instead")
    dr.add_argument("--scope-every", type=positive_int, default=5,
                    help="count observation scopes (distinct tag sets, a full observation listing) on every Nth poll "
                         "and on the last one; the polls in between carry scopes=null (default 5)")
    dr.add_argument("--since", help="ISO start of the LLM request window (default: now)")
    dr.add_argument("--timeseries", help="append every poll to this JSONL file")
    for p in (mig, dr):
        p.add_argument("--bank", required=True, help="bank id")
        p.add_argument("--allow-prod", action="store_true", help="let this run on a target that is not the copy")
    args = ap.parse_args(argv)
    try:
        if not bank_config.BANK_ID.match(args.bank):
            raise Fail("--bank %r is not a plain bank id" % args.bank)
        if args.command == "migrate":
            parse_scopes(args.scopes)               # a malformed value is refused before the target is touched
        else:
            global RECHECK_SLEEP
            text = os.environ.get("SCOPE_MIGRATE_RECHECK_SLEEP", "2")
            try:
                RECHECK_SLEEP = float(text)
            except ValueError:
                RECHECK_SLEEP = -1.0
            if not (math.isfinite(RECHECK_SLEEP) and RECHECK_SLEEP >= 0):
                raise Fail("SCOPE_MIGRATE_RECHECK_SLEEP=%r is not a finite number of seconds >= 0; nothing was sent"
                           % text)
        container, is_copy, key = bank_config.resolve_target()
        if not is_copy and not args.allow_prod:
            raise Fail("%s on a target that is not the hc-next copy needs --allow-prod; nothing was sent" % args.command)
        return (cmd_migrate if args.command == "migrate" else cmd_drain)(args, container, key)
    except Fail as e:
        print("scope-migrate: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
