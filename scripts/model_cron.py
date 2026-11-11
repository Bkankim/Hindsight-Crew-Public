#!/usr/bin/env python3
"""model_cron.py - put mental models on the server's refresh_cron schedule, or take them off (issue #428).

    model_cron.py --cron "30 18 * * *" (--all | --models a,b) [--apply] [--backup-dir D]
    model_cron.py --unset             (--all | --models a,b) [--apply] [--backup-dir D]

Default is a dry run: it reads the models, checks them and prints the plan with ZERO writes (no PATCH,
no backup file). `--apply` writes. The cron is UTC (the server evaluates it in UTC, see
docs/model-refresh-cron.md); "30 18 * * *" is 03:30 Asia/Seoul.

Apply, in this order:
 1 read every model page by page (scripts/model_watch.list_all_models; a missing page fails the run);
   an unknown target id fails the run.
 2 refuse before any write: a target whose trigger.mode is not exactly "full" (the operator's decision keeps
   mode as it is and the schedule only for full models), a target with refresh_after_consolidation
   true (mutually exclusive with refresh_cron upstream), a cron this repo's watch cannot evaluate.
 3 save each target's current trigger (memory + a backup JSON file written before the first PATCH).
 4 PATCH /mental-models/{id} {"trigger": {"refresh_cron": C, "mode": <saved mode>,
   "refresh_after_consolidation": false}} - upstream merges a trigger patch over the stored one, so
   the other fields stay. The first failure stops further writes.
 5 re-read every model and compare each written target with the saved trigger + the new refresh_cron.
 6 any failed write, any write whose answer was lost (outcome unknown), any mismatch or an unreadable
   re-read: restore EVERY attempted target (the failed/unknown one included) by PATCHing its whole saved
   trigger, re-read, compare each with its saved trigger and report the ones that did not come back.
Exit: 0 done (or dry run ok), 1 failed and fully restored, 2 failed and NOT fully restored (see
`unrestored`), 3 refused before any write. JSON result to stdout.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import hc_host_env  # noqa: E402  (같은 디렉터리, 표준 라이브러리만)
import model_watch  # noqa: E402  (같은 디렉터리: 목록 페이지 처리, cron 해석, HTTP 클라이언트를 공유한다)

EXIT_OK, EXIT_RESTORED, EXIT_UNRESTORED, EXIT_REFUSED = 0, 1, 2, 3
DEFAULT_BACKUP_DIR = os.path.join(os.path.expanduser("~"), "Library", "Logs", "hindsight-model-refresh", "cron-backups")


class Refused(Exception):
    pass


def _model_path(bank, mid):
    return "/v1/default/banks/%s/mental-models/%s" % (bank, mid)


def select_targets(models, wanted):
    by_id = {m["id"]: m for m in models}
    if wanted is None:
        return [by_id[k] for k in sorted(by_id)]
    unknown = [w for w in wanted if w not in by_id]
    if unknown:
        raise Refused("unknown model id(s): %s" % ",".join(unknown))
    return [by_id[w] for w in wanted]


def check_targets(targets, cron):
    """Refusal reasons, all of them at once (nothing is written when there is any)."""
    reasons = []
    if cron is not None:
        try:
            model_watch.parse_cron(cron)
        except model_watch.CronError as e:
            reasons.append("cron %r refused: %s" % (cron, e))
    for m in targets:
        trig = m.get("trigger")
        if not isinstance(trig, dict):
            reasons.append("%s: no trigger object (list detail=content)" % m.get("id"))
            continue
        if cron is None:
            continue   # unset only clears the schedule; mode and the rest stay whatever they are
        if trig.get("mode") != "full":
            reasons.append("%s: trigger.mode is %r, not 'full'" % (m["id"], trig.get("mode")))
        if trig.get("refresh_after_consolidation") is True:
            reasons.append("%s: refresh_after_consolidation is true (mutually exclusive with refresh_cron)" % m["id"])
    return reasons


def apply_patch(saved, cron):
    """The trigger patch for one model. For apply it restates mode and refresh_after_consolidation so the
    upstream merge (which drops an unstated refresh_after_consolidation when refresh_cron is set) leaves
    every field but refresh_cron as it was."""
    if cron is None:
        return {"trigger": {"refresh_cron": None}}
    return {"trigger": {"refresh_cron": cron, "mode": saved.get("mode"),
                        "refresh_after_consolidation": bool(saved.get("refresh_after_consolidation", False))}}


def restore_patch(saved):
    """The whole saved trigger, every field stated: the merge then leaves nothing of the failed write behind
    (the response and the request share upstream's MentalModelTrigger, so the saved object round-trips)."""
    return {"trigger": copy.deepcopy(saved)}


def expected_trigger(saved, cron):
    exp = copy.deepcopy(saved)
    exp["refresh_cron"] = cron
    return exp


def same_trigger(a, b):
    """Equal triggers, a null field and an absent one counted alike (the response may carry either)."""
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    return {k: v for k, v in a.items() if v is not None} == {k: v for k, v in b.items() if v is not None}


def _patch(client, bank, mid, body):
    """'ok' | 'failed:<detail>' | 'unknown:<detail>' (no answer: the write may or may not have landed)."""
    try:
        status, _payload = client.request("PATCH", _model_path(bank, mid), body)
    except model_watch.TransportError as e:
        return "unknown:%s" % e
    return "ok" if 200 <= status < 300 else "failed:http_%d" % status


def _reread(client, bank):
    try:
        return {m["id"]: m for m in model_watch.list_all_models(client, bank)}, None
    except model_watch.ObservationError as e:
        return None, str(e)


def write_backup(backup_dir, bank, cron, saved, now):
    os.makedirs(backup_dir, exist_ok=True)
    path = os.path.join(backup_dir, "triggers-%s.json" % now.strftime("%Y%m%dT%H%M%SZ"))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"saved_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "bank": bank, "target_cron": cron,
                   "triggers": saved}, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def run(client, bank, cron, wanted, apply, backup_dir, now=None):
    """The whole operation over `client` (request(method, path, body) -> (status, payload)). Returns
    (exit_code, result dict)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    result = {"mode": "apply" if apply else "dry-run", "action": "unset" if cron is None else "set",
              "cron": cron, "targets": [], "writes": 0, "outcome": None}
    try:
        models = model_watch.list_all_models(client, bank)
        targets = select_targets(models, wanted)
    except (model_watch.ObservationError, Refused) as e:
        result.update(outcome="refused", reasons=[str(e)])
        return EXIT_REFUSED, result
    result["targets"] = [m["id"] for m in targets]
    reasons = check_targets(targets, cron)
    if reasons:
        result.update(outcome="refused", reasons=reasons)
        return EXIT_REFUSED, result
    saved = {m["id"]: copy.deepcopy(m["trigger"]) for m in targets}
    result["saved_triggers"] = saved
    result["plan"] = {mid: apply_patch(saved[mid], cron) for mid in result["targets"]}
    if not apply:
        result["outcome"] = "dry-run"
        return EXIT_OK, result
    try:
        result["backup"] = write_backup(backup_dir, bank, cron, saved, now)
    except OSError as e:
        result.update(outcome="refused", reasons=["backup not written: %s" % e])
        return EXIT_REFUSED, result

    attempted, write_results, problem = [], {}, None
    for mid in result["targets"]:
        attempted.append(mid)
        result["writes"] += 1
        outcome = _patch(client, bank, mid, apply_patch(saved[mid], cron))
        write_results[mid] = outcome
        if outcome != "ok":
            problem = "write %s: %s" % (mid, outcome)
            break
    result["write_results"] = write_results

    if problem is None:
        current, err = _reread(client, bank)
        if current is None:
            problem = "verify re-read failed: %s" % err
        else:
            mismatched = [mid for mid in attempted
                          if mid not in current or not same_trigger(current[mid].get("trigger"), expected_trigger(saved[mid], cron))]
            if mismatched:
                result["mismatched"] = mismatched
                problem = "verify mismatch: %s" % ",".join(mismatched)
    if problem is None:
        result["outcome"] = "applied"
        return EXIT_OK, result

    # Restore every attempted target, the failed or unknown one included.
    result["problem"] = problem
    restore_results = {}
    for mid in attempted:
        result["writes"] += 1
        restore_results[mid] = _patch(client, bank, mid, restore_patch(saved[mid]))
    result["restore_results"] = restore_results
    current, err = _reread(client, bank)
    if current is None:
        result["restore_check"] = "re-read failed: %s" % err
        unrestored = list(attempted)
    else:
        unrestored = [mid for mid in attempted if mid not in current or not same_trigger(current[mid].get("trigger"), saved[mid])]
    result["unrestored"] = unrestored
    result["restore_failed_calls"] = [mid for mid, o in restore_results.items() if o != "ok"]
    if unrestored or result["restore_failed_calls"]:
        result["outcome"] = "failed-unrestored"
        return EXIT_UNRESTORED, result
    result["outcome"] = "failed-restored"
    return EXIT_RESTORED, result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    what = ap.add_mutually_exclusive_group(required=True)
    what.add_argument("--cron", help="UTC 5-field cron to set as trigger.refresh_cron")
    what.add_argument("--unset", action="store_true", help="clear trigger.refresh_cron")
    who = ap.add_mutually_exclusive_group(required=True)
    who.add_argument("--all", action="store_true", help="every mental model of the bank")
    who.add_argument("--models", help="comma-separated model ids")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run, zero writes)")
    ap.add_argument("--backup-dir", default=DEFAULT_BACKUP_DIR, help="where the saved triggers go before writing")
    args = ap.parse_args(argv)
    wanted = None if args.all else [w.strip() for w in args.models.split(",") if w.strip()]
    if wanted is not None and not wanted:
        ap.error("--models is empty")
    bank = hc_host_env.require("HC_PERSONAL_BANK", "HC_OWNER")["HC_PERSONAL_BANK"]
    client = model_watch.HttpClient(model_watch.read_token())
    code, result = run(client, bank, None if args.unset else args.cron, wanted, args.apply, args.backup_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("MODEL_CRON_%s targets=%d writes=%d" % (result["outcome"].upper().replace("-", "_"),
                                                   len(result["targets"]), result["writes"]), file=sys.stderr)
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except hc_host_env.ConfigMissing as e:
        print("MODEL_CRON_FAIL - %s" % e, file=sys.stderr)
        sys.exit(EXIT_REFUSED)
