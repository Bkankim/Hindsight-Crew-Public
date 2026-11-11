#!/usr/bin/env python3
"""정책·트리거 단일 파일을 추적분 + 로컬분으로 나눈다 (issue #187).

게이트웨이 로더는 추적 파일(policies/<name>)에 로컬 오버레이(policies/local/<name>,
gitignore)를 병합한다. 정책은 뱅크 키 단위로 로컬이 추가·대체하고, 트리거는 추적 행 뒤에
로컬 행을 이어 붙인다. 이 도구는 지금의 단일 파일을 그 두 조각으로 나눈다.

  split  (기본) --src DIR 의 bank-policies.json·context-triggers.json 을 읽어
         --local-dir 에 로컬분을 쓴다. --tracked-dir 를 주면 추적분도 쓴다.
         --keep 뱅크(기본 personal-alice, team-eng - verify gate 가 쓰는 데모 뱅크)와
         defaults 는 추적분에, 나머지 뱅크와 그 뱅크를 가리키는 트리거 행은 로컬분에 간다.
         이미 있는 로컬 파일은 --force 없이는 덮어쓰지 않는다.
  check  --src DIR(분리 전 원본) 의 단일 파일 로드 결과와 --policies-dir 의
         추적 + local/ 병합 로드 결과가 같은지 게이트웨이 로더로 비교한다. 다르면 exit 1.

split 은 표준 라이브러리만 쓴다(운영 반영 때 pull 전에 `git show origin/main:scripts/policy-split.py
| python3 -` 로 돌릴 수 있게). check 는 리포 루트의 gateway 패키지를 import 한다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

POLICY_FILE = "bank-policies.json"
TRIGGER_FILE = "context-triggers.json"
DEFAULT_KEEP = ("personal-alice", "team-eng")


class SplitError(Exception):
    pass


def _read(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def split(policies, triggers, keep):
    """(추적 정책, 로컬 정책, 추적 트리거, 로컬 트리거). 원본 순서를 보존한다."""
    banks = policies.get("banks", {})
    tracked_policies = {key: value for key, value in policies.items() if key != "banks"}
    tracked_policies["banks"] = {name: spec for name, spec in banks.items() if name in keep}
    local_policies = {"banks": {name: spec for name, spec in banks.items() if name not in keep}}
    rows = triggers.get("triggers", [])
    tracked_rows = [row for row in rows if row.get("bank") in keep]
    local_rows = [row for row in rows if row.get("bank") not in keep]
    # 병합은 추적 행 뒤에 로컬 행을 붙인다. 원래 순서가 그와 다르면(로컬 행이 추적 행보다 앞)
    # 첫 매치 우선 규칙 때문에 병합 결과가 원본과 달라진다 - 조용히 바꾸지 않고 멈춘다.
    if rows != tracked_rows + local_rows:
        raise SplitError("trigger rows interleave kept and local banks; merged order would "
                         "differ from the source (move kept-bank rows first, then re-run)")
    tracked_triggers = {key: value for key, value in triggers.items() if key != "triggers"}
    tracked_triggers["triggers"] = tracked_rows
    local_triggers = {"version": triggers.get("version", 1), "triggers": local_rows}
    return tracked_policies, local_policies, tracked_triggers, local_triggers


def _write(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def cmd_split(args):
    keep = set(args.keep or DEFAULT_KEEP)
    policies = _read(os.path.join(args.src, POLICY_FILE))
    triggers = _read(os.path.join(args.src, TRIGGER_FILE))
    missing = sorted(keep - set(policies.get("banks", {})))
    if missing:
        raise SplitError("--keep bank(s) %s are not in %s" % (missing, os.path.join(args.src, POLICY_FILE)))
    t_pol, l_pol, t_trg, l_trg = split(policies, triggers, keep)
    outputs = [(os.path.join(args.local_dir, POLICY_FILE), l_pol),
               (os.path.join(args.local_dir, TRIGGER_FILE), l_trg)]
    if args.tracked_dir:
        outputs += [(os.path.join(args.tracked_dir, POLICY_FILE), t_pol),
                    (os.path.join(args.tracked_dir, TRIGGER_FILE), t_trg)]
    for path, _ in outputs:                       # 하나라도 막히면 아무것도 쓰지 않는다
        if os.path.exists(path) and not args.force:
            raise SplitError("%s already exists (pass --force to overwrite)" % path)
    for path, data in outputs:
        _write(path, data)
    print("POLICY_SPLIT_OK local_banks=%s local_triggers=%s -> %s" % (
        ",".join(l_pol["banks"]) or "-", ",".join(r.get("id", "?") for r in l_trg["triggers"]) or "-",
        args.local_dir))
    return 0


def _load_gateway():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)
    from gateway.bank_policies import BankPolicies
    from gateway.context_triggers import TriggerTable
    return BankPolicies, TriggerTable


def policies_equal(a, b):
    names = a.bank_names() | b.bank_names()
    return a.defaults == b.defaults and a.bank_names() == b.bank_names() and all(
        a.for_bank(n) == b.for_bank(n) for n in names)


def cmd_check(args):
    BankPolicies, TriggerTable = _load_gateway()
    orig_pol = BankPolicies.from_file(os.path.join(args.src, POLICY_FILE))
    orig_trg = TriggerTable.from_file(os.path.join(args.src, TRIGGER_FILE))
    local_dir = os.path.join(args.policies_dir, "local")
    merged_pol = BankPolicies.from_files(os.path.join(args.policies_dir, POLICY_FILE),
                                         os.path.join(local_dir, POLICY_FILE))
    merged_trg = TriggerTable.from_files(os.path.join(args.policies_dir, TRIGGER_FILE),
                                         os.path.join(local_dir, TRIGGER_FILE))
    problems = []
    if not policies_equal(orig_pol, merged_pol):
        problems.append("bank policies differ (banks: original %s, merged %s)" % (
            sorted(orig_pol.bank_names()), sorted(merged_pol.bank_names())))
    if orig_trg.rows != merged_trg.rows:
        problems.append("trigger rows differ (original %s, merged %s)" % (
            [r.id for r in orig_trg.rows], [r.id for r in merged_trg.rows]))
    if problems:
        for line in problems:
            print("POLICY_SPLIT_CHECK_FAIL - " + line)
        return 1
    print("POLICY_SPLIT_CHECK_OK banks=%d triggers=%d" % (len(merged_pol.bank_names()),
                                                          len(merged_trg)))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", nargs="?", choices=("split", "check"), default="split")
    parser.add_argument("--src", default="policies", help="분리 전 단일 파일 디렉터리 (기본 policies)")
    parser.add_argument("--local-dir", default=os.path.join("policies", "local"),
                        help="split: 로컬분을 쓸 디렉터리 (기본 policies/local)")
    parser.add_argument("--tracked-dir", help="split: 추적분도 쓸 디렉터리 (생략하면 쓰지 않는다)")
    parser.add_argument("--keep", action="append",
                        help="split: 추적분에 남길 뱅크 (반복 가능, 기본 %s)" % ", ".join(DEFAULT_KEEP))
    parser.add_argument("--force", action="store_true", help="split: 기존 파일 덮어쓰기")
    parser.add_argument("--policies-dir", default="policies",
                        help="check: 추적 + local/ 을 읽을 디렉터리 (기본 policies)")
    args = parser.parse_args(argv)
    try:
        return cmd_check(args) if args.mode == "check" else cmd_split(args)
    except (SplitError, OSError, ValueError) as exc:
        print("POLICY_SPLIT_FAIL - %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
