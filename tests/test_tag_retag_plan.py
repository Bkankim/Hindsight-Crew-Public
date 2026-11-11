"""#43 A 단계: 재태깅 계획기(scripts/tag_retag_plan.py) 단위 테스트.

고정하는 것:
  ① 매핑 유형 행렬 {합쳐진 태그 복원, user->topic, project 병합 6건 각각, 라벨->kind, unresolved 3종 + 라벨 행 없음}
     x {계획기 출력, manifest dry-run 요약} (dry-run 쪽은 tests/test_tag_retag_manifest.py 의 같은 행렬)
  ② 계획기는 규칙을 다시 구현하지 않는다: 최종 new 는 평가 함수를 위반 없이 통과하고, 적용 뒤 재계획하면 변경 0건
  ③ 커밋된 매핑 파일의 kind·project 값이 정책의 선언 어휘 안에 있다
  ④ 결정성: 같은 입력 -> 같은 바이트(입력 순서와도 무관)
fixture 는 지어낸 짧은 태그뿐이다(기억 본문 없음). 운영 스택·라이브 뱅크는 쓰지 않는다.
"""
import importlib.util
import json
import random
import sys
from pathlib import Path

import pytest

from gateway import rules
from gateway.bank_policies import BankPolicies

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
FIXTURES = ROOT / "tests" / "fixtures" / "tag_retag"
DOCUMENTS = FIXTURES / "documents.jsonl"
LABELS = FIXTURES / "labels.jsonl"
RESTORE_MAP = FIXTURES / "merged-restore.json"
POLICY_PATH = ROOT / "tests" / "fixtures" / "policies" / "sample" / "bank-policies.json"
COMMITTED_MAP = FIXTURES / "retag-map.json"   # 운영자 재태깅 표(공개판에 없음)의 데모 사본(#423)


def load_script(name, module_name):
    sys.path.insert(0, str(SCRIPTS))        # 계획기가 tag_hygiene 를 같은 디렉터리에서 import 한다
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def planner():
    return load_script("tag_retag_plan.py", "tag_retag_plan_under_test")


@pytest.fixture(scope="module")
def policy():
    return BankPolicies.from_file(str(POLICY_PATH)).for_bank("personal-demo")


@pytest.fixture(scope="module")
def mapping(planner):
    """운영에 커밋된 매핑(user->topic, project 병합, 라벨 표) + 테스트 전용 합쳐진 태그 복원."""
    merged = planner.load_mapping(str(COMMITTED_MAP))
    merged["merged_restore"] = planner.load_mapping(str(RESTORE_MAP))["merged_restore"]
    return merged


@pytest.fixture(scope="module")
def documents(planner):
    return planner.tag_hygiene.read_documents(str(DOCUMENTS))


@pytest.fixture(scope="module")
def labels(planner):
    return planner.read_labels(str(LABELS))


@pytest.fixture(scope="module")
def manifest(planner, mapping, documents, labels, policy):
    return planner.build_manifest(documents, mapping, labels, policy, rules)


def by_id(entries):
    return {entry["id"]: entry for entry in entries}


# ---- ① 매핑 유형 행렬: (문서 id, 기대 new 또는 None(=unresolved), 기대 types 또는 None, unresolved 사유 접두) ----
CHANGE_ROWS = [
    # 합쳐진 태그 복원: 운영 3건의 실제 문자열은 커밋하지 않고 지어낸 문자열로 형식만 고정한다
    ("restore-merged", ["project:larch-yard", "kind:gotcha", "topic:blog", "user:demo"],
     ["merged-restore", "merged-slug"]),
    # user -> topic
    ("user-kauri-gate", ["project:rowan-tide", "kind:decision", "topic:kauri-gate"],
     ["user-split", "user-to-topic"]),
    # project 병합 6건 각각
    ("merge-wattle-cove-2026", ["project:rowan-tide", "kind:completion"], ["project-merge"]),
    ("merge-banksia-loft", ["project:elm-gazette", "kind:completion"], ["project-merge"]),
    ("merge-myall-shore", ["project:larch-yard", "kind:completion"], ["project-merge"]),
    ("merge-birch", ["project:fir-easel", "kind:completion"], ["project-merge"]),
    ("merge-tupelo", ["project:alder-thistle", "kind:completion"], ["project-merge"]),
    ("merge-juniper", ["project:poplar-kiln", "kind:completion"], ["project-merge"]),
    # 대소문자·공백 변형: 저장 태그의 'Project:Birch' 등도 같은 매핑을 탄다(평가 함수가 그 변형을 정규화해 고치므로
    # 정확 일치만 찾으면 매핑 유형 없이 조용히 대상에서 빠진다)
    ("case-project-birch", ["project:fir-easel", "kind:completion"], ["project-merge"]),
    ("case-user-kauri-gate", ["project:rowan-tide", "kind:decision", "topic:kauri-gate"],
     ["user-split", "user-to-topic"]),
    ("case-restore-merged", ["project:larch-yard", "kind:gotcha", "topic:blog", "user:demo"],
     ["merged-restore", "merged-slug"]),
    # 라벨 -> kind (라벨은 대괄호가 있든 없든, 영문이면 대소문자 무관하게 같다)
    ("label-done", ["project:hindsight-crew", "topic:blog", "kind:completion"], ["label-to-kind", "missing-kind"]),
    ("label-found", ["project:hindsight-crew", "topic:blog", "kind:discovery"], ["label-to-kind", "missing-kind"]),
    ("label-decided", ["project:hindsight-crew", "topic:blog", "kind:decision"], ["label-to-kind", "missing-kind"]),
    ("label-rule", ["project:hindsight-crew", "topic:blog", "kind:rule"], ["label-to-kind", "missing-kind"]),
    ("label-english", ["project:hindsight-crew", "topic:blog", "kind:completion"], ["label-to-kind", "missing-kind"]),
    # 매핑 없이 평가 함수가 고치는 칸 오배정은 그대로 실린다
    ("misfile-kind", ["project:hindsight-crew", "kind:gotcha"], ["misfiled-slot"]),
]
UNRESOLVED_ROWS = [
    ("unresolved-merged-nomap", "merged-slug without restore mapping"),
    ("unresolved-no-label", "kind-missing: no label"),
    ("unresolved-no-label-row", "kind-missing: no label"),
    ("unresolved-unknown-label", "kind-missing: label not in table: 미정"),
]
NOT_LISTED = ["clean", "cross-reference", "alias-only"]


@pytest.mark.parametrize("doc_id,expected_new,expected_types", CHANGE_ROWS, ids=[r[0] for r in CHANGE_ROWS])
def test_change_rows_in_planner_output(manifest, documents, doc_id, expected_new, expected_types):
    entry = by_id(manifest["documents"])[doc_id]
    old = next(d["tags"] for d in documents if d["id"] == doc_id)
    assert entry["old"] == old
    assert entry["new"] == expected_new
    assert entry["types"] == expected_types
    assert doc_id not in by_id(manifest["unresolved"])


@pytest.mark.parametrize("doc_id,reason_prefix", UNRESOLVED_ROWS, ids=[r[0] for r in UNRESOLVED_ROWS])
def test_unresolved_rows_in_planner_output(manifest, documents, doc_id, reason_prefix):
    entry = by_id(manifest["unresolved"])[doc_id]
    assert entry["old"] == next(d["tags"] for d in documents if d["id"] == doc_id)
    assert entry["reason"].startswith(reason_prefix)
    assert doc_id not in by_id(manifest["documents"])


@pytest.mark.parametrize("doc_id", NOT_LISTED)
def test_untouched_documents_are_not_in_the_manifest(manifest, doc_id):
    assert doc_id not in by_id(manifest["documents"]) and doc_id not in by_id(manifest["unresolved"])


def test_alias_only_change_is_counted_but_not_targeted(manifest):
    # project:quince / kind:done 는 평가 함수가 고치지만 문제 유형이 아니라서 재태깅 대상이 아니다(건수만 센다)
    assert manifest["summary"]["untargeted_changes"] == 1


def test_summary_counts_match_the_lists(manifest):
    summary = manifest["summary"]
    assert manifest["version"] == 2 and manifest["bank"] == "personal-demo"
    assert summary["scanned"] == 24
    assert summary["documents"] == len(manifest["documents"]) == len(CHANGE_ROWS)
    assert summary["unresolved"] == len(manifest["unresolved"]) == len(UNRESOLVED_ROWS)
    assert summary["by_type"] == {
        "label-to-kind": 5, "merged-restore": 2, "merged-slug": 2, "misfiled-slot": 1,
        "missing-kind": 5, "project-merge": 7, "user-split": 2, "user-to-topic": 2,
    }


def test_committed_map_project_merges_are_exactly_the_issue_six(planner):
    committed = planner.load_mapping(str(COMMITTED_MAP))
    assert committed["project_merge"] == {
        "project:wattle-cove-2026": "project:rowan-tide",
        "project:banksia-loft": "project:elm-gazette",
        "project:myall-shore": "project:larch-yard",
        "project:birch": "project:fir-easel",
        "project:tupelo": "project:alder-thistle",
        "project:juniper": "project:poplar-kiln",
    }
    assert committed["user_to_topic"] == {"user:kauri-gate": "topic:kauri-gate"}


# ---- ② 규칙 사본 없음 / 최종 new 검증 ----

def test_every_planned_new_passes_the_evaluator_without_violations(manifest, policy):
    for entry in manifest["documents"]:
        verdict = rules.evaluate_tags(entry["new"], policy)
        assert verdict.violations == (), entry["id"]
        assert list(verdict.tags) == entry["new"], entry["id"]      # 한 번 더 통과시켜도 그대로


def test_replanning_the_applied_result_yields_no_changes(planner, mapping, manifest, documents, labels, policy):
    applied = [dict(d, tags=next((e["new"] for e in manifest["documents"] if e["id"] == d["id"]), d["tags"]))
               for d in documents]
    again = planner.build_manifest(applied, mapping, labels, policy, rules)
    assert again["documents"] == []
    assert sorted(e["id"] for e in again["unresolved"]) == sorted(r[0] for r in UNRESOLVED_ROWS)


def test_planner_delegates_to_the_evaluator_not_a_rule_copy(planner, mapping, documents, labels, policy, monkeypatch):
    calls = {"evaluate_tags": 0}
    real = rules.evaluate_tags

    def spy(tags, pol):
        calls["evaluate_tags"] += 1
        return real(tags, pol)

    monkeypatch.setattr(rules, "evaluate_tags", spy)
    planner.build_manifest(documents, mapping, labels, policy, rules)
    assert calls["evaluate_tags"] >= len(documents)


def test_unmapped_merged_tag_is_unresolved_even_when_a_kind_is_present(planner, policy):
    doc = {"id": "x", "tags": ["project:zzz-kind-gotcha-topic-blog", "kind:fix"]}
    result = planner.plan_document(doc, {"merged_restore": {}, "user_to_topic": {}, "project_merge": {},
                                         "label_to_kind": {}}, None, policy, rules)
    assert result["status"] == "unresolved"
    assert result["entry"]["reason"].startswith("merged-slug without restore mapping")


def test_restore_output_is_still_subject_to_substitution_and_evaluation(planner, policy):
    mapping = {"merged_restore": {"project:zz-kind-fix": ["project:birch", "kind:fix"]},
               "user_to_topic": {}, "project_merge": {"project:birch": "project:fir-easel"},
               "label_to_kind": {}}
    result = planner.plan_document({"id": "x", "tags": ["project:zz-kind-fix"]}, mapping, None, policy, rules)
    assert result["entry"]["new"] == ["project:fir-easel", "kind:fix"]
    assert result["entry"]["types"] == ["merged-restore", "merged-slug", "project-merge"]


def test_restore_that_yields_a_violation_is_unresolved(planner, policy):
    mapping = {"merged_restore": {"project:zz-kind-fix": ["project:larch-yard", "kind:not-a-kind"]},
               "user_to_topic": {}, "project_merge": {}, "label_to_kind": {}}
    result = planner.plan_document({"id": "x", "tags": ["project:zz-kind-fix"]}, mapping, None, policy, rules)
    assert result["status"] == "unresolved" and "RULE:tags-vocabulary" in result["entry"]["reason"]


def test_untagged_document_takes_the_label_path(planner, mapping, policy):
    with_label = planner.plan_document({"id": "x", "tags": []}, mapping, {"x": "완료"}, policy, rules)
    assert with_label["entry"]["new"] == ["kind:completion"]
    without = planner.plan_document({"id": "x", "tags": []}, mapping, {}, policy, rules)
    assert without["status"] == "unresolved" and without["needs_label"] is True


# ---- ③ 매핑 파일 검증 ----

def test_committed_map_values_are_inside_the_declared_vocabulary(planner):
    committed = planner.load_mapping(str(COMMITTED_MAP))
    policy_json = json.loads(POLICY_PATH.read_text(encoding="utf-8"))["banks"]["personal-demo"]
    vocabulary = policy_json["tag_vocabulary"]
    assert committed["label_to_kind"], "label table must not be empty"
    for label, kind in committed["label_to_kind"].items():
        prefix, _, slug = kind.partition(":")
        assert prefix == "kind" and slug in vocabulary["kind"], (label, kind)
    for old, new in committed["project_merge"].items():
        assert new.partition(":")[2] in vocabulary["project"], (old, new)
        assert old.partition(":")[2] not in vocabulary["project"], (old, new)      # 합쳐진 옛 이름은 선언 어휘에 없다


def test_committed_label_table_has_the_english_kind_names(planner):
    table = planner.load_mapping(str(COMMITTED_MAP))["label_to_kind"]
    for kind in set(table.values()):
        assert table.get(kind.partition(":")[2]) == kind, kind          # kind:completion <- completion


@pytest.mark.parametrize("label", ["Completion", "[COMPLETION]", "  completion ", "[완료]", "완료"])
def test_label_lookup_ignores_brackets_case_and_spaces(planner, mapping, policy, label):
    result = planner.plan_document({"id": "x", "tags": ["project:hindsight-crew"]}, mapping, {"x": label}, policy, rules)
    assert result["entry"]["new"] == ["project:hindsight-crew", "kind:completion"]


@pytest.mark.parametrize("section,table", [
    ("project_merge", {"project:Birch": "project:a", "PROJECT:birch ": "project:b"}),
    ("label_to_kind", {"Completion": "kind:completion", "completion": "kind:completion"}),
], ids=["tag-keys", "label-keys"])
def test_mapping_keys_that_differ_only_by_case_are_rejected(planner, tmp_path, section, table):
    path = tmp_path / "map.json"
    path.write_text(json.dumps({"version": 1, section: table}), encoding="utf-8")
    with pytest.raises(planner.RetagError, match="differ only by case"):
        planner.load_mapping(str(path))


def test_manifest_carries_a_digest_of_its_documents(planner, manifest):
    digest = manifest["documents_sha256"]
    assert digest == planner.documents_digest(manifest["documents"]) and len(digest) == 64
    edited = json.loads(json.dumps(manifest["documents"]))
    edited[0]["new"] = edited[0]["new"] + ["topic:sneaky"]
    assert planner.documents_digest(edited) != digest
    # 키 순서나 공백 차이로는 바뀌지 않는다(정규형으로 잰다)
    assert planner.documents_digest(json.loads(json.dumps(manifest["documents"], indent=4))) == digest


@pytest.mark.parametrize("bad", [
    {"version": 2},
    {"version": 1, "surprise": {}},
    {"version": 1, "label_to_kind": {"완료": "topic:completion"}},
    {"version": 1, "merged_restore": {"project:a-b": "project:a"}},
    {"version": 1, "project_merge": {"": "project:a"}},
], ids=["version", "unknown-section", "label-not-kind", "restore-not-list", "empty-key"])
def test_bad_mapping_files_are_rejected(planner, tmp_path, bad):
    path = tmp_path / "map.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(planner.RetagError):
        planner.load_mapping(str(path))


@pytest.mark.parametrize("line", ["not json", '{"id": "a"}', '{"id": "a", "label": 3}'],
                         ids=["invalid-json", "no-label-key", "label-not-string"])
def test_bad_label_lines_are_rejected_with_line_number(planner, tmp_path, line):
    path = tmp_path / "labels.jsonl"
    path.write_text('{"id": "ok", "label": null}\n' + line + "\n", encoding="utf-8")
    with pytest.raises(planner.RetagError, match=r":2:"):
        planner.read_labels(str(path))


def test_duplicate_label_rows_are_rejected(planner, tmp_path):
    path = tmp_path / "labels.jsonl"
    path.write_text('{"id": "a", "label": "완료"}\n{"id": "a", "label": "발견"}\n', encoding="utf-8")
    with pytest.raises(planner.RetagError, match="duplicate"):
        planner.read_labels(str(path))


# ---- ④ 결정성 + CLI ----

def write_full_map(planner, tmp_path):
    full = json.loads(COMMITTED_MAP.read_text(encoding="utf-8"))
    full["merged_restore"] = json.loads(RESTORE_MAP.read_text(encoding="utf-8"))["merged_restore"]
    path = tmp_path / "full-map.json"
    path.write_text(json.dumps(full, ensure_ascii=False), encoding="utf-8")
    return path


def test_same_input_twice_gives_the_same_bytes_even_when_input_order_differs(planner, tmp_path, capsys):
    map_path = write_full_map(planner, tmp_path)
    lines = DOCUMENTS.read_text(encoding="utf-8").splitlines()
    shuffled = list(lines)
    random.Random(7).shuffle(shuffled)
    shuffled_path = tmp_path / "shuffled.jsonl"
    shuffled_path.write_text("\n".join(shuffled) + "\n", encoding="utf-8")
    outputs = []
    for index, source in enumerate((DOCUMENTS, DOCUMENTS, shuffled_path)):
        out = tmp_path / ("m%d.json" % index)
        code = planner.main(["plan", str(source), "--map", str(map_path), "--labels", str(LABELS), "--out", str(out), "--policy", str(POLICY_PATH)])
        assert code == 0
        outputs.append(out.read_bytes())
    assert outputs[0] == outputs[1] == outputs[2]
    assert json.loads(outputs[0])["summary"]["documents"] == len(CHANGE_ROWS)


def test_label_targets_lists_documents_that_still_lack_a_kind(planner, tmp_path):
    map_path = write_full_map(planner, tmp_path)
    out = tmp_path / "ids.json"
    assert planner.main(["label-targets", str(DOCUMENTS), "--map", str(map_path), "--out", str(out), "--policy", str(POLICY_PATH)]) == 0
    assert json.loads(out.read_text(encoding="utf-8")) == sorted([
        "label-decided", "label-done", "label-english", "label-found", "label-rule",
        "unresolved-no-label", "unresolved-no-label-row", "unresolved-unknown-label",
    ])


def test_plan_without_labels_leaves_label_documents_unresolved(planner, tmp_path):
    map_path = write_full_map(planner, tmp_path)
    out = tmp_path / "m.json"
    assert planner.main(["plan", str(DOCUMENTS), "--map", str(map_path), "--out", str(out), "--policy", str(POLICY_PATH)]) == 0
    result = json.loads(out.read_text(encoding="utf-8"))
    reasons = {e["id"]: e["reason"] for e in result["unresolved"]}
    assert reasons["label-done"] == "kind-missing: no label"
    assert "label-done" not in by_id(result["documents"])


def test_planner_cli_reports_a_broken_mapping_with_exit_2(planner, tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    assert planner.main(["plan", str(DOCUMENTS), "--map", str(bad), "--policy", str(POLICY_PATH)]) == 2
    assert "tag_retag_plan:" in capsys.readouterr().err


def test_duplicate_input_ids_are_rejected(planner, mapping, policy):
    docs = [{"id": "a", "tags": ["kind:fix"]}, {"id": "a", "tags": ["kind:fix"]}]
    with pytest.raises(planner.RetagError, match="duplicate"):
        planner.build_manifest(docs, mapping, None, policy, rules)


# ---- 이슈 #109 항목 1: 비문자열 태그 원소가 있어도 계획 생성이 끝까지 간다 ----

NON_STRING_TAGS = [
    [{"a": 1}],
    ["kind:fix", {"a": 1}],
    [["x"], "kind:fix"],
    [1, "kind:fix"],
]


@pytest.mark.parametrize("tags", NON_STRING_TAGS)
def test_non_string_tag_document_does_not_kill_the_whole_plan(planner, mapping, policy, tags):
    docs = [{"id": "weird", "tags": tags}, {"id": "ok", "tags": ["project:larch-yard", "kind:fix"]}]
    built = planner.build_manifest(docs, mapping, None, policy, rules)
    assert built["summary"]["scanned"] == 2
    entries = {e["id"]: e for e in built["documents"] + built["unresolved"]}
    if "weird" in entries:
        assert entries["weird"]["old"] == tags       # 원소를 지우거나 바꾸지 않는다


@pytest.mark.parametrize("tags", NON_STRING_TAGS)
def test_apply_mapping_keeps_non_string_elements_verbatim(planner, mapping, tags):
    result, _ = planner.apply_mapping(tags, mapping)
    for element in tags:
        if not isinstance(element, str):
            assert element in result


def test_dedupe_dedupes_strings_only_and_keeps_every_non_string_element(planner):
    got = planner._dedupe(["a", {"a": 1}, "a", {"a": 1}, ["x"], "b", "a"])
    assert got == ["a", {"a": 1}, {"a": 1}, ["x"], "b"]


def test_plan_document_with_non_string_tag_preserves_it_in_old(planner, mapping, policy):
    result = planner.plan_document({"id": "w", "tags": [{"a": 1}]}, mapping, None, policy, rules)
    assert result["status"] == "unresolved"
    assert result["entry"]["old"] == [{"a": 1}]


def test_plan_cli_completes_with_a_non_string_tag_document_in_the_export(planner, tmp_path, capsys):
    export = tmp_path / "docs.jsonl"
    rows = [{"id": "weird", "created_at": "2026-01-01T00:00:00Z", "tags": [{"a": 1}]},
            {"id": "fine", "created_at": "2026-01-01T00:00:00Z", "tags": ["project:larch-yard", "kind:fix"]}]
    export.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    out = tmp_path / "plan.json"
    assert planner.main(["plan", str(export), "--map", str(COMMITTED_MAP), "--out", str(out), "--policy", str(POLICY_PATH)]) == 0
    built = json.loads(out.read_text(encoding="utf-8"))
    assert built["summary"]["scanned"] == 2
    assert [e["old"] for e in built["unresolved"] if e["id"] == "weird"] == [[{"a": 1}]]


@pytest.fixture(scope="module")
def prefixless_policy():
    """tag_prefixes 가 비어 있고 tags_required 가 아닌 뱅크(team-demo 형): 문법 위반이 없어 문서가 unresolved 로 먼저 빠지지 않는다."""
    policy = BankPolicies.from_file(str(POLICY_PATH)).for_bank("team-demo")
    assert policy.tag_prefixes == () or list(policy.tag_prefixes) == []
    return policy


@pytest.mark.parametrize("tags", [["x", {"a": 1}], [{"a": 1}, {"b": 2}], [["n"], "x", {"a": 1}]])
def test_prefixless_bank_plans_to_the_end_with_mixed_type_tags(planner, mapping, prefixless_policy, tags):
    """이슈 #109 항목 1 후속(final-review C1): _same_tags 의 sorted() 가 str 과 비문자열 혼합에서 TypeError 를 내던 경로."""
    docs = [{"id": "mixed", "tags": tags}, {"id": "plain", "tags": ["x"]}]
    built = planner.build_manifest(docs, mapping, None, prefixless_policy, rules, bank="team-demo")
    assert built["summary"]["scanned"] == 2
    listed = {e["id"]: e for e in built["documents"] + built["unresolved"]}
    if "mixed" in listed:
        assert listed["mixed"]["old"] == tags            # 비문자열 원소는 그대로 보존


def test_same_tags_compares_mixed_types_without_error(planner):
    assert planner._same_tags(["a", {"x": 1}], [{"x": 1}, "a"]) is True
    assert planner._same_tags(["a", {"x": 1}], ["a", {"x": 2}]) is False
