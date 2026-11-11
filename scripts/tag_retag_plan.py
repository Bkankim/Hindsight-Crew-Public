#!/usr/bin/env python3
"""재태깅 계획기 (issue #43 A 단계, 상위 #35). 호스트에서 실행한다.

저장된 문서 태그를 `tag_hygiene.evaluate_document`(= `gateway.rules.evaluate_tags` + 정책)에 다시
통과시켜 정규화 결과와 문제 유형을 얻고, 그 위에 재태깅 전용 매핑(운영자 표는 공개판에 없다, --map으로 표를 준다)만 덧씌워
문서별 `old -> new` manifest(version 2)를 만든다. 정규화 규칙은 여기서 다시 구현하지 않는다.

순서(문서 하나):
  1. 매핑을 정규화 전에 저장 태그에 적용한다: 합쳐진 태그 복원 -> user->topic, project 병합.
  2. 평가 함수를 통과시킨다. 그래도 kind가 없으면 라벨(본문 맨 앞 `[라벨]`)로 kind를 채운다.
  3. 최종 new가 다시 평가 함수를 위반 없이 통과하고, 같은 잣대(tag_hygiene)로 문제 유형·kind 누락이
     0이고, 한 번 더 통과시켜도 그대로일 때만 manifest에 싣는다. 아니면 `unresolved`(사유 포함).

명령:
  plan           문서 JSONL -> manifest JSON (결정적: 같은 입력이면 같은 바이트)
  label-targets  라벨이 필요한 문서 id 목록 JSON (scripts/tag-migrate.sh --export-labels --ids-file 입력)

사용 예:
  python3 scripts/tag_retag_plan.py label-targets docs.jsonl --map <재태깅 표> --out ids.json
  scripts/tag-migrate.sh --export-labels --ids-file ids.json > labels.jsonl
  python3 scripts/tag_retag_plan.py plan docs.jsonl --map <재태깅 표> --labels labels.jsonl --out manifest.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import tag_hygiene  # noqa: E402  (같은 디렉터리. gateway 는 tag_hygiene._load_gateway 가 지연 import)

MANIFEST_VERSION = 2
DEFAULT_BANK = tag_hygiene.DEFAULT_BANK
DEFAULT_MAP = os.path.join(_HERE, "retag-map.json")

TYPE_MERGED_RESTORE = "merged-restore"
TYPE_USER_TO_TOPIC = "user-to-topic"
TYPE_PROJECT_MERGE = "project-merge"
TYPE_LABEL_TO_KIND = "label-to-kind"
TYPE_MISSING_KIND = "missing-kind"

_MAP_SECTIONS = ("merged_restore", "user_to_topic", "project_merge", "label_to_kind")


class RetagError(RuntimeError):
    pass


# --------------------------------------------------------------------------- 입력

def load_mapping(path):
    """재태깅 매핑 파일. 모양이 틀리면 조용히 넘기지 않고 중단한다. `_`로 시작하는 키는 주석이다."""
    with open(path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise RetagError("%s: mapping must be an object with version 1" % path)
    unknown = sorted(k for k in raw if k != "version" and not k.startswith("_") and k not in _MAP_SECTIONS)
    if unknown:
        raise RetagError("%s: unknown section(s): %s" % (path, ", ".join(unknown)))
    mapping = {}
    for section in _MAP_SECTIONS:
        table = raw.get(section, {})
        if not isinstance(table, dict):
            raise RetagError("%s: %s must be an object" % (path, section))
        for key, value in table.items():
            if section == "merged_restore":
                ok = isinstance(value, list) and value and all(isinstance(t, str) and t for t in value)
            else:
                ok = isinstance(value, str) and bool(value)
            if not ok or not isinstance(key, str) or not key:
                raise RetagError("%s: bad entry in %s: %r" % (path, section, key))
        folded = Counter(key.strip().casefold() for key in table)
        clash = sorted(key for key in table if folded[key.strip().casefold()] > 1)
        if clash:
            # 키는 공백·대소문자를 떼고 비교한다(apply_mapping). 그 기준으로 겹치면 어느 값이 이길지 모호하다.
            raise RetagError("%s: %s has keys that differ only by case/space: %s" % (path, section, ", ".join(clash)))
        mapping[section] = table
    for key, value in mapping["label_to_kind"].items():
        if not value.startswith("kind:"):
            raise RetagError("%s: label_to_kind[%r] must be a kind: tag, got %r" % (path, key, value))
    return mapping


def read_labels(path):
    """라벨 JSONL `{"id","label"}` -> {id: 라벨 또는 None}. 깨진 줄·중복 id는 줄 번호와 함께 중단한다."""
    labels = {}
    with open(path, "r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RetagError("%s:%d: invalid JSON (%s)" % (path, number, exc))
            if not isinstance(row, dict) or not row.get("id") or "label" not in row:
                raise RetagError('%s:%d: each line must be {"id","label"}' % (path, number))
            label = row["label"]
            if label is not None and not isinstance(label, str):
                raise RetagError("%s:%d: label must be a string or null" % (path, number))
            if row["id"] in labels:
                raise RetagError("%s:%d: duplicate id %r" % (path, number, row["id"]))
            labels[row["id"]] = label
    return labels


def normalize_label(label):
    """`[완료]`·` 완료 `·`완료` 모두 `완료`. 없거나 비면 None. 표 조회는 casefold 로 한다(`[Completion]` = `completion`)."""
    if not isinstance(label, str):
        return None
    text = label.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1].strip()
    return text or None


# --------------------------------------------------------------------------- 계획

def _dedupe(tags):
    """문자열 태그만 중복을 걷는다(첫 등장 순서 유지). 비문자열 원소(dict·list 등)는 도구가 판단할 수 없는 데이터라
    해시도 못 하므로 지우거나 바꾸지 않고 그 자리 그대로 둔다."""
    seen = set()
    result = []
    for tag in tags:
        if isinstance(tag, str):
            if tag in seen:
                continue
            seen.add(tag)
        result.append(tag)
    return result


def _folded(table):
    """매핑 키를 앞뒤 공백·대소문자(casefold)를 뗀 형태로 본다: 평가 함수가 같은 변형('Project:Birch')을 정규화해
    고치기 때문에, 정확 일치만 찾으면 그 문서는 매핑 유형이 안 붙어 조용히 대상에서 빠진다."""
    return {key.strip().casefold(): value for key, value in table.items()}


def apply_mapping(tags, mapping):
    """정규화 전 매핑. (tags, 적용된 매핑 유형 집합)."""
    applied = set()
    restore = _folded(mapping["merged_restore"])
    user_to_topic = _folded(mapping["user_to_topic"])
    project_merge = _folded(mapping["project_merge"])
    restored = []
    for tag in tags:
        pieces = restore.get(tag.strip().casefold()) if isinstance(tag, str) else None
        if pieces is None:
            restored.append(tag)
        else:
            restored.extend(pieces)
            applied.add(TYPE_MERGED_RESTORE)
    substituted = []
    for tag in restored:
        key = tag.strip().casefold() if isinstance(tag, str) else None
        if key in user_to_topic:
            substituted.append(user_to_topic[key])
            applied.add(TYPE_USER_TO_TOPIC)
        elif key in project_merge:
            substituted.append(project_merge[key])
            applied.add(TYPE_PROJECT_MERGE)
        else:
            substituted.append(tag)
    return _dedupe(substituted), applied


def _same_tags(left, right):
    # 비문자열 원소가 섞여도 정렬이 TypeError 를 내지 않도록 JSON 표현을 키로 쓴다(태그 접두 규칙이 없는 뱅크 대응, issue #109).
    def key(tag):
        return json.dumps(tag, sort_keys=True, ensure_ascii=False)
    return sorted(left, key=key) == sorted(right, key=key)


def _unresolved(document_id, old, reason):
    return {"id": document_id, "old": old, "reason": reason}


def plan_document(document, mapping, labels, policy, rules):
    """문서 하나 -> {"status": "change"|"unresolved"|"skip", ..., "needs_label": bool}.

    `labels`는 {id: 라벨} 또는 None(라벨 입력 없음).
    """
    original = tag_hygiene.evaluate_document(document, policy, rules)
    document_id, old = original["id"], original["old"]
    types = {p["type"] for p in original["problems"]}

    mapped, applied = apply_mapping(old, mapping)
    types |= applied
    verdict = rules.evaluate_tags(mapped, policy)

    needs_label = any(v.rule in (rules.RULE_TAGS_MISSING_PREFIX, rules.RULE_TAGS_REQUIRED)
                      for v in verdict.violations)
    if needs_label:
        types.add(TYPE_MISSING_KIND)       # 매핑·정규화를 거친 뒤에도 kind 가 없는 문서만 센다
        label = normalize_label((labels or {}).get(document_id))
        if label is None:
            return {"status": "unresolved", "needs_label": True,
                    "entry": _unresolved(document_id, old, "kind-missing: no label")}
        kind = _folded(mapping["label_to_kind"]).get(label.casefold())
        if kind is None:
            return {"status": "unresolved", "needs_label": True,
                    "entry": _unresolved(document_id, old, "kind-missing: label not in table: %s" % label)}
        types.add(TYPE_LABEL_TO_KIND)
        verdict = rules.evaluate_tags(_dedupe(mapped + [kind]), policy)

    if verdict.violations:
        reason = "violations: " + ", ".join(sorted({v.rule for v in verdict.violations}))
        return {"status": "unresolved", "needs_label": needs_label,
                "entry": _unresolved(document_id, old, reason)}

    new = list(verdict.tags)
    # 같은 잣대(tag_hygiene)로 new를 다시 잰다: 남은 문제 유형·kind 누락·불안정(한 번 더 돌리면 바뀜)이면 싣지 않는다.
    check = tag_hygiene.evaluate_document({"id": document_id, "tags": new}, policy, rules)
    if check["problems"] or check["missing_kind"] or check["violations"] or check["changed"]:
        parts = ["%s(%s): %s" % (p["type"], p["how"], p["tag"]) for p in check["problems"]]
        if check["changed"]:
            parts.append("unstable: re-evaluation changes the result")
        if check["missing_kind"] or check["violations"]:
            parts.append("violations: " + ", ".join(sorted(check["violations"])))
        if any(p["type"] == tag_hygiene.TYPE_MERGED and p["how"] == "embedded-prefix" for p in check["problems"]):
            parts.insert(0, "merged-slug without restore mapping")
        return {"status": "unresolved", "needs_label": needs_label,
                "entry": _unresolved(document_id, old, "; ".join(parts))}

    if _same_tags(new, old):
        return {"status": "skip", "needs_label": needs_label}
    if not types:
        # 정규화만 바뀐 문서(alias 잔재·대소문자 등). 문제 유형 재태깅의 대상이 아니라서 싣지 않고 센다.
        return {"status": "untargeted", "needs_label": needs_label}
    return {"status": "change", "needs_label": needs_label,
            "entry": {"id": document_id, "old": old, "new": new, "types": sorted(types)}}


def build_manifest(documents, mapping, labels, policy, rules, bank=DEFAULT_BANK):
    changes, unresolved = [], []
    untargeted = 0
    seen = set()
    for document in documents:
        if document["id"] in seen:
            raise RetagError("duplicate document id in input: %r" % document["id"])
        seen.add(document["id"])
        result = plan_document(document, mapping, labels, policy, rules)
        if result["status"] == "change":
            changes.append(result["entry"])
        elif result["status"] == "unresolved":
            unresolved.append(result["entry"])
        elif result["status"] == "untargeted":
            untargeted += 1
    changes.sort(key=lambda e: e["id"])
    unresolved.sort(key=lambda e: e["id"])
    by_type = Counter(t for entry in changes for t in entry["types"])
    return {
        "version": MANIFEST_VERSION,
        "bank": bank,
        "documents": changes,
        "documents_sha256": documents_digest(changes),
        "unresolved": unresolved,
        "summary": {
            "scanned": len(documents),
            "documents": len(changes),
            "by_type": dict(sorted(by_type.items())),
            "unresolved": len(unresolved),
            "untargeted_changes": untargeted,
        },
    }


def label_targets(documents, mapping, policy, rules):
    """매핑을 적용한 뒤에도 kind가 없어 라벨이 필요한 문서 id(정렬)."""
    return sorted(document["id"] for document in documents
                  if plan_document(document, mapping, None, policy, rules)["needs_label"])


def documents_digest(documents):
    """manifest 의 documents 부분 sha256. 컨테이너 쪽 tag_migrate.documents_digest 와 같은 알고리즘이어야 한다
    (tests/test_tag_retag_manifest.py 가 두 구현이 같은 값을 내는지 고정한다)."""
    canonical = json.dumps(documents, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def dumps(value):
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


# --------------------------------------------------------------------------- cli

def _context(args):
    tag_hygiene.require_bank(args.bank)       # no default bank (issue #186): fail before reading anything else
    rules, policies_cls = tag_hygiene._load_gateway()
    # --policy 는 그 파일 하나, 기본은 추적 + policies/local/ 병합(issue #187)
    policies = (policies_cls.from_file(args.policy) if args.policy
                else policies_cls.from_files(tag_hygiene.default_policy_path()))
    if args.bank not in policies.bank_names():
        raise RetagError("bank %r is not declared in the policy file" % args.bank)
    return rules, policies.for_bank(args.bank), load_mapping(args.map), tag_hygiene.read_documents(args.documents)


def _emit(text, out_path):
    if out_path:
        with open(out_path, "w", encoding="utf-8") as handle:
            handle.write(text)
    else:
        sys.stdout.write(text)


def run_plan(args):
    rules, policy, mapping, documents = _context(args)
    labels = read_labels(args.labels) if args.labels else None
    manifest = build_manifest(documents, mapping, labels, policy, rules, bank=args.bank)
    _emit(dumps(manifest), args.out)
    summary = manifest["summary"]
    sys.stderr.write("planned %d documents, unresolved %d (scanned %d)\n" % (
        summary["documents"], summary["unresolved"], summary["scanned"]))
    return 0


def run_label_targets(args):
    rules, policy, mapping, documents = _context(args)
    ids = label_targets(documents, mapping, policy, rules)
    _emit(json.dumps(ids, ensure_ascii=False, indent=2) + "\n", args.out)
    sys.stderr.write("%d documents need a label\n" % len(ids))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description="재태깅 계획기: 문서 JSONL + 매핑 -> 문서별 old -> new manifest.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, handler, help_text in (
        ("plan", run_plan, "manifest(version 2) JSON을 낸다."),
        ("label-targets", run_label_targets, "라벨이 필요한 문서 id 목록 JSON을 낸다."),
    ):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("documents", help='문서 JSONL, 한 줄에 {"id","created_at","tags"} (tag-hygiene-export.sh). "-"는 stdin')
        sub.add_argument("--map", default=DEFAULT_MAP, help="재태깅 매핑 (기본 scripts/retag-map.json)")
        sub.add_argument("--bank", default=DEFAULT_BANK)
        sub.add_argument("--policy", help="정책 파일 하나 (기본 policies/bank-policies.json + policies/local/ 병합)")
        sub.add_argument("--out", help="출력 파일 (기본 stdout)")
        if name == "plan":
            sub.add_argument("--labels", help='라벨 JSONL {"id","label"} (scripts/tag-migrate.sh --export-labels)')
        sub.set_defaults(handler=handler)
    return parser


def main(argv=None):
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return args.handler(args)
    except (RetagError, tag_hygiene.HygieneError, OSError, ValueError) as exc:
        if isinstance(exc, tag_hygiene.ConfigMissingError):
            sys.stderr.write("TAG_RETAG_PLAN_FAIL - %s\n" % exc)
            return 2
        sys.stderr.write("tag_retag_plan: %s\n" % exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
