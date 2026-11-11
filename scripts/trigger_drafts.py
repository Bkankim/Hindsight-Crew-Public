#!/usr/bin/env python3
"""trigger_drafts.py - 트리거 초안(audit/pending-triggers.jsonl)의 처리 기록 CLI (issue #276).

게이트웨이(gateway/recurrence.py)는 초안을 pending-triggers.jsonl 에 덧붙이기만 한다. 컨테이너가 같은
파일에 동시에 덧붙이므로 호스트는 그 파일을 다시 쓰거나 줄을 지우지 않는다. 등록(approved)·기각(rejected)은
별도 파일 trigger-decisions.jsonl 에 덧붙이고, 같은 draft_id 가 여러 번 나오면 마지막 줄이 이긴다.

    trigger_drafts.py list [--all]
    trigger_drafts.py resolve <draft_id>... --decision approved|rejected --reason "<텍스트>"

경로는 model_audit.py 와 같은 규칙이다: 디렉터리 = HC_GATEWAY_AUDIT_LOG(기본 <리포>/audit/gateway.log)의
디렉터리. HC_PENDING_TRIGGERS / HC_TRIGGER_DECISIONS 가 있으면 CLI는 그것을 우선한다(테스트용).
model_audit.py 는 이 모듈을 import 하지 않고 읽기 규칙을 복사해 둔다(test_host_env.py 가 model_audit.py 만 임시 트리에
복사해 돌리기 때문). tests/test_trigger_drafts.py::test_the_audit_count_agrees_with_the_cli_list 가 두 판독기를 고정한다.
표준 라이브러리만 쓴다.
"""
import argparse
import datetime as dt
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DECISIONS = ("approved", "rejected")
PREVIEW_CHARS = 60
DRAFT_PREFIX = "draft-"
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def default_paths():
    """(pending 파일, 처리 기록 파일). model_audit.py 의 PENDING_TRIGGERS/TRIGGER_DECISIONS 와 같은 규칙."""
    audit_log = os.environ.get("HC_GATEWAY_AUDIT_LOG", os.path.join(ROOT, "audit", "gateway.log"))
    audit_dir = os.path.dirname(audit_log)
    pending = os.environ.get("HC_PENDING_TRIGGERS") or os.path.join(audit_dir, "pending-triggers.jsonl")
    decisions = os.environ.get("HC_TRIGGER_DECISIONS") or os.path.join(audit_dir, "trigger-decisions.jsonl")
    return pending, decisions


def bare_id(draft_id):
    """`draft-aee0f09652c4` 와 `aee0f09652c4` 를 같은 맨 id 로 본다."""
    draft_id = draft_id.strip()
    return draft_id[len(DRAFT_PREFIX):] if draft_id.startswith(DRAFT_PREFIX) else draft_id


def _clean(text):
    """제어문자(ESC 등)는 운영자 터미널로 가지 않게, list 가 한 줄이어야 하므로 공백 하나로 바꾼다."""
    return _CONTROL.sub(" ", text)


def _lines(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return [line for line in fh if line.strip()]
    except OSError:
        return []


def read_pending(path):
    """pending 파일의 비지 않은 줄마다 dict 하나(파일 순서).

    draft_id·state 가 둘 다 있는 줄만 resolvable 이다(resolve 대상). draft_id 만 있고 state 가 없으면 id 는 보이되
    resolvable 이 아니다. 그 밖(키 없음, JSON 깨짐, 쓰는 중인 마지막 줄, 객체가 아닌 값, 너무 깊은 중첩)은
    draft_id 가 None 인 '기타' 줄이다 - 세기만 하고 처리할 수 없다."""
    drafts = []
    for line in _lines(path):
        entry = {"draft_id": None, "state": None, "created_at": None, "preview": "", "resolvable": False}
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):
            row = None
        if isinstance(row, dict):
            draft_id, state = row.get("draft_id"), row.get("state")
            if isinstance(draft_id, str) and draft_id.strip():
                evidence = row.get("evidence")
                preview = evidence.get("new_content_preview") if isinstance(evidence, dict) else None
                created = row.get("created_at")
                has_state = isinstance(state, str) and bool(state.strip())
                entry.update(draft_id=bare_id(draft_id), state=state if has_state else None, resolvable=has_state,
                             created_at=created if isinstance(created, str) and created else None,
                             preview=" ".join(_clean(preview).split()) if isinstance(preview, str) else "")
        drafts.append(entry)
    return drafts


def read_decisions(path):
    """{맨 draft_id: 마지막 decision}. 파일이 없거나 깨진·형식이 틀린 줄은 건너뛴다."""
    latest = {}
    for line in _lines(path):
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(row, dict):
            continue
        draft_id, decision = row.get("draft_id"), row.get("decision")
        if isinstance(draft_id, str) and draft_id.strip() and decision in DECISIONS:
            latest[bare_id(draft_id)] = decision
    return latest


def unresolved(drafts, decisions):
    return [d for d in drafts if not d["resolvable"] or d["draft_id"] not in decisions]


def _row(d, decision=None):
    fields = [_clean(d["draft_id"] or "-"), _clean(d["state"] or "-"), _clean(d["created_at"] or "-")]
    if decision is not None:
        fields.append(decision)
    fields.append(d["preview"][:PREVIEW_CHARS] or "-")
    return " ".join(fields)


def cmd_list(args, pending_path, decisions_path):
    drafts = read_pending(pending_path)
    decisions = read_decisions(decisions_path)
    if args.all:
        for d in drafts:
            print(_row(d, decisions.get(d["draft_id"], "-") if d["resolvable"] else "-"))
    else:
        for d in unresolved(drafts, decisions):
            print(_row(d))
    return 0


def cmd_resolve(args, pending_path, decisions_path):
    if not args.reason.strip():
        print("--reason 이 비어 있다 - 아무것도 기록하지 않았다", file=sys.stderr)
        return 2
    known = {d["draft_id"] for d in read_pending(pending_path) if d["resolvable"]}
    ids = []
    for raw in args.draft_ids:
        draft_id = bare_id(raw)
        if draft_id not in ids:
            ids.append(draft_id)
    missing = [i for i in ids if i not in known]
    if missing:
        print(f"없는 draft_id: {', '.join(missing)} - 아무것도 기록하지 않았다", file=sys.stderr)
        return 2
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = "".join(json.dumps({"ts": ts, "draft_id": i, "decision": args.decision, "reason": args.reason},
                                 ensure_ascii=False) + "\n" for i in ids)
    # 마지막 줄이 개행 없이 끊긴 파일이면 새 줄이 그 줄에 붙지 않게 개행부터 쓴다.
    try:
        with open(decisions_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            if fh.tell():
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) != b"\n":
                    payload = "\n" + payload
    except OSError:
        pass
    try:
        with open(decisions_path, "a", encoding="utf-8") as fh:
            fh.write(payload)
    except OSError as exc:
        print(f"처리 기록을 쓰지 못했다 ({decisions_path}): {exc}", file=sys.stderr)
        return 1
    for i in ids:
        print(f"{i} {args.decision}")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description="트리거 초안 처리 기록 (list / resolve)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list", help="미처리 초안을 한 줄씩 출력")
    p_list.add_argument("--all", action="store_true", help="처리된 초안도 decision 과 함께 출력")
    p_resolve = sub.add_parser("resolve", help="초안의 등록·기각을 처리 기록에 덧붙임")
    p_resolve.add_argument("draft_ids", nargs="+", metavar="draft_id")
    p_resolve.add_argument("--decision", required=True, choices=DECISIONS)
    p_resolve.add_argument("--reason", required=True)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    pending_path, decisions_path = default_paths()
    return {"list": cmd_list, "resolve": cmd_resolve}[args.cmd](args, pending_path, decisions_path)


if __name__ == "__main__":
    sys.exit(main())
