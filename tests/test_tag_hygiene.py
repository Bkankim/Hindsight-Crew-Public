"""#42: 태그 위생 측정 명령 - 분석기(report)와 내보내기(export) 로직.

분석기는 저장된 태그를 `gateway.rules.evaluate_tags`에 다시 통과시켜 세는 것이 전부이므로,
규칙 자체는 여기서 다시 검증하지 않는다(tests/test_tag_*.py 몫). 여기서 고정하는 것은
  ① fixture 문서 -> 기대 카운트 (합쳐진 slug / 칸 오배정 / 교차 참조 제외 / kind 누락 / user 분열 /
     --since / 감사 출처)
  ② 분석기가 평가 함수를 실제로 위임 호출한다는 것(규칙 사본이 없다는 증거)
  ③ 내보내기 래퍼의 읽기 전용·페이지네이션 거부 계약 (가짜 transport, 운영 스택 없음)
"""
import ast
import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from gateway import rules

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "tag_hygiene"
DOCUMENTS = FIXTURES / "documents.jsonl"
AUDIT = FIXTURES / "audit.jsonl"
POLICY = FIXTURES / "policy.json"
SINCE = "2026-09-01T00:00:00Z"


def load_script(name, module_name):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "scripts" / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def hygiene():
    return load_script("tag_hygiene.py", "tag_hygiene_under_test")


@pytest.fixture(scope="module")
def migrate():
    return load_script("tag_migrate.py", "tag_migrate_for_parity")


@pytest.fixture(scope="module")
def policy(hygiene):
    _, policies = hygiene._load_gateway()
    return policies.from_file(str(POLICY)).for_bank("personal-demo")


def build(hygiene, policy, *, since=SINCE, audit=(), documents=DOCUMENTS):
    parsed = hygiene.parse_iso(since) if since else None
    return hygiene.build_report(hygiene.read_documents(str(documents)), policy, rules,
                                bank="personal-demo", since=parsed, audit_paths=tuple(audit))


def doc_ids(report, kind):
    return sorted({d["id"] for d in report["documents"] for p in d["problems"] if p["type"] == kind})


# ============================ ① fixture -> 기대 카운트 ============================

def test_merged_slug_counts_embedded_prefix_and_split_string(hygiene, policy):
    merged = build(hygiene, policy)["problem_types"]["merged-slug"]
    # 문서 2건: 콜론 없이 합쳐져 저장된 slug 1건 + 한 문자열에 붙어 저장된 태그 1건(평가 함수가 쪼갠다)
    assert merged == {"documents": 2, "tags": 2, "occurrences": 2}
    report = build(hygiene, policy)
    (embedded,) = [p for d in report["documents"] for p in d["problems"] if p["how"] == "embedded-prefix"]
    assert embedded["tag"] == "project:myall-shore-kind-gotcha-topic-blog"
    assert embedded["to"] == []          # 이미 합쳐진 slug 는 기계적으로 되돌리지 않는다(ADR 0001 결정)


def test_misfiled_slot_counts_project_less_topic_but_not_cross_references(hygiene, policy):
    report = build(hygiene, policy)
    assert report["problem_types"]["misfiled-slot"] == {"documents": 3, "tags": 2, "occurrences": 3}
    assert doc_ids(report, "misfiled-slot") == ["misfile-duplicate", "misfile-kind", "misfile-project"]
    by_tag = {(e["type"], e["tag"]): e for e in report["problem_tags"]}
    assert by_tag[("misfiled-slot", "topic:larch-yard")]["to"] == ["project:larch-yard"]
    assert by_tag[("misfiled-slot", "topic:larch-yard")]["documents"] == 2
    assert by_tag[("misfiled-slot", "topic:gotcha")]["to"] == ["kind:gotcha"]


def test_cross_reference_and_ambiguous_candidates_are_not_counted(hygiene, policy):
    report = build(hygiene, policy)
    # `project:rowan-tide` 기억의 `topic:larch-yard` 는 정상 교차 참조, 후보 둘 이상은 평가 함수가 옮기지 않는다
    assert "cross-reference" not in doc_ids(report, "misfiled-slot")
    assert "ambiguous-two-candidates" not in doc_ids(report, "misfiled-slot")
    flagged = {d["id"]: d for d in report["documents"]}
    assert "cross-reference" not in flagged                       # 바뀌지도 경고도 없다
    assert flagged["ambiguous-two-candidates"]["warnings"]        # 대신 경고는 남는다
    assert report["reference"]["documents_with_warnings"] >= 1


def test_duplicate_topic_copy_is_reported_as_misfile_with_how(hygiene, policy):
    report = build(hygiene, policy)
    (record,) = [d for d in report["documents"] if d["id"] == "misfile-duplicate"]
    assert record["old"] == ["project:larch-yard", "topic:larch-yard", "kind:fix"]
    assert record["new"] == ["project:larch-yard", "kind:fix"]
    assert record["problems"][0]["how"] == "duplicate"


def test_missing_kind_counts_only_the_missing_prefix_rule(hygiene, policy):
    report = build(hygiene, policy)
    assert report["missing_kind"] == {"documents": 1}
    (record,) = [d for d in report["documents"] if d["missing_kind"]]
    assert record["id"] == "kind-missing" and record["violations"] == [rules.RULE_TAGS_MISSING_PREFIX]
    # 태그가 아예 없는 문서는 다른 규칙(tags-required)이고 kind 누락 칸에 섞이지 않는다
    assert report["reference"]["untagged_documents"] == 1
    assert report["reference"]["violations_by_rule"][rules.RULE_TAGS_REQUIRED] == 1


def test_user_split_counts_alias_and_outside_list_values(hygiene, policy):
    report = build(hygiene, policy)
    assert report["problem_types"]["user-split"] == {"documents": 2, "tags": 2, "occurrences": 2}
    hows = {p["tag"]: p["how"] for d in report["documents"] for p in d["problems"] if p["type"] == "user-split"}
    assert hows == {"user:demoalt": "alias", "user:kauri-gate": "outside-list"}
    # 정상 user:demo 는 세지 않는다
    assert all(p["tag"] != "user:demo" for d in report["documents"] for p in d["problems"])


def test_user_alias_is_split_even_when_the_bank_keeps_user_open(hygiene, policy):
    from dataclasses import replace
    open_user = replace(policy, tag_closed_prefixes=("kind",))
    record = hygiene.evaluate_document({"id": "x", "tags": ["kind:fix", "user:demoalt", "user:demo"]},
                                       open_user, rules)
    assert [(p["tag"], p["how"], p["to"]) for p in record["problems"]] == [("user:demoalt", "alias", ["user:demo"])]


def evaluate(hygiene, policy, tags):
    return hygiene.evaluate_document({"id": "x", "tags": tags}, policy, rules)


def summary(record):
    return sorted((p["type"], p["tag"], p["how"]) for p in record["problems"])


def test_leading_space_is_plain_normalization_not_a_split(hygiene, policy):
    """final-review #42: 앞 공백만 붙은 태그는 평가 함수도 쪼개지 않는다 -> 칸 오배정 1, 합쳐진 slug 0."""
    record = evaluate(hygiene, policy, [" topic:gotcha", "project:hindsight-crew"])
    assert record["new"] == ["kind:gotcha", "project:hindsight-crew"]
    assert summary(record) == [("misfiled-slot", " topic:gotcha", "moved")]


@pytest.mark.parametrize("tags", [["kind:gotcha kind:gotcha"], ["project:x, topic:한글"]])
def test_split_strings_whose_pairs_collapse_are_still_merged(hygiene, policy, tags):
    record = evaluate(hygiene, policy, tags)
    assert [(p["type"], p["how"]) for p in record["problems"]] == [("merged-slug", "split")]


@pytest.mark.parametrize("tags", [["myall-shore-kind-gotcha"],
                                  ["Project:Larch-Yard-Kind-Gotcha"],
                                  ["project:larch-yard-kind:gotcha"]])
def test_merged_slug_is_judged_on_the_normalized_value(hygiene, policy, tags):
    record = evaluate(hygiene, policy, tags + ["kind:fix"])
    assert [p["type"] for p in record["problems"]] == ["merged-slug"]


@pytest.mark.parametrize("variant", ["user:DEMO", "user: demo"])
def test_user_case_and_space_variants_that_normalize_into_the_list_are_not_a_split(hygiene, policy, variant):
    assert evaluate(hygiene, policy, ["kind:fix", variant])["problems"] == []


def test_user_alias_after_normalization_is_reported_as_alias(hygiene, policy):
    record = evaluate(hygiene, policy, ["kind:fix", "User:DEMOALT"])
    assert summary(record) == [("user-split", "User:DEMOALT", "alias")]


def test_alias_rewrites_are_found_after_normalization_and_splitting(hygiene, policy):
    for tags, expected in (
        (["Kind:Done"], [["Kind:Done", "kind:completion"]]),
        (["done"], [["done", "kind:completion"]]),
        (["project:quince, kind:done"], [["project:quince, kind:done", "kind:completion"],
                                        ["project:quince, kind:done", "project:hickory-bell"]]),
        (["kind:fix", "project:hindsight-crew"], []),
    ):
        assert evaluate(hygiene, policy, tags)["alias_rewrites"] == expected


def test_duplicate_label_uses_the_normalized_original_tags(hygiene, policy):
    record = evaluate(hygiene, policy, ["Project:Larch-Yard", "topic:larch-yard", "kind:fix"])
    assert summary(record) == [("misfiled-slot", "topic:larch-yard", "duplicate")]


def test_two_topics_moved_to_the_same_project_are_moved_then_moved_merged(hygiene, policy):
    record = evaluate(hygiene, policy, ["topic:quince", "topic:hickory-bell", "kind:fix"])
    assert summary(record) == [("misfiled-slot", "topic:hickory-bell", "moved-merged"),
                               ("misfiled-slot", "topic:quince", "moved")]
    assert record["new"] == ["project:hickory-bell", "kind:fix"]


def test_since_with_an_offset_is_normalized_to_utc(hygiene, policy):
    stamp = hygiene.parse_iso("2026-10-02T09:00:00+09:00")
    assert stamp.strftime("%Y-%m-%dT%H:%M:%SZ") == "2026-10-02T00:00:00Z"
    report = hygiene.build_report([], policy, rules, bank="personal-demo", since=stamp)
    assert report["since"] == "2026-10-02T00:00:00Z"


def test_reference_values(hygiene, policy):
    ref = build(hygiene, policy)["reference"]
    assert ref["documents_scanned"] == 13 and ref["skipped_no_timestamp"] == 1
    assert ref["documents_without_project"] == 3                  # misfile-project, ambiguous, untagged
    assert ref["project_missing_ratio"] == round(3 / 13, 4)
    # alias 로 바뀌는 태그: project:quince, kind:done, user:demoalt
    assert ref["alias_rewrites"] == {"occurrences": 3, "distinct_tags": 3}
    assert ref["tag_occurrences"] == sum(
        len(d["tags"]) for d in map(json.loads, DOCUMENTS.read_text().splitlines())
        if d["created_at"] and d["created_at"] >= SINCE)


def test_since_filters_documents_and_reports_unknown_timestamps(hygiene, policy):
    everything = build(hygiene, policy, since=None)
    filtered = build(hygiene, policy, since=SINCE)
    assert everything["reference"]["documents_scanned"] == 15 and everything["since"] is None
    assert filtered["reference"]["documents_scanned"] == 13
    # 기준 이전 문서(`topic:gotcha` 칸 오배정)는 --since 로 빠진다
    assert "old-before-since" in doc_ids(everything, "misfiled-slot")
    assert "old-before-since" not in doc_ids(filtered, "misfiled-slot")
    assert everything["problem_types"]["misfiled-slot"]["documents"] == 4
    assert filtered["problem_types"]["misfiled-slot"]["documents"] == 3
    # 시각이 없는 문서는 조용히 넣거나 빼지 않고 센다
    assert filtered["reference"]["skipped_no_timestamp"] == 1
    assert everything["reference"]["skipped_no_timestamp"] == 0


def test_documents_detail_carries_old_to_new_for_retagging(hygiene, policy):
    report = build(hygiene, policy)
    detail = {d["id"]: d for d in report["documents"]}
    assert detail["alias-only"]["old"] == ["project:quince", "kind:done"]
    assert detail["alias-only"]["new"] == ["project:hickory-bell", "kind:completion"]
    assert detail["alias-only"]["changed"] is True and detail["alias-only"]["problems"] == []
    assert "clean" not in detail and "user-alias" in detail
    assert set(detail["user-alias"]) == {"id", "created_at", "old", "new", "changed", "problems",
                                         "missing_kind", "violations", "warnings"}


# ============================ 감사 로그 출처 ============================

def test_audit_sources_group_problem_tags_by_transport_and_client(hygiene, policy):
    audit = build(hygiene, policy, audit=[AUDIT])["audit"]
    rows = {(s["tag"], s["transport"], s["client"]): s["lines"] for s in audit["sources"]}
    assert rows == {
        ("project:myall-shore-kind-gotcha-topic-blog", "mcp", "quince"): 2,
        ("topic:gotcha", "rest", None): 1,      # 기준 이전 줄(8/25)과 다른 뱅크 줄은 들어오지 않는다
        ("user:demoalt", "mcp", "claude-code"): 1,
    }
    assert audit["retain_tag_lines"] == 5       # personal-demo + retain-tags + since 이후
    assert audit["unparsable_lines"] == 1       # 깨진 줄은 세고 넘어간다
    assert audit["truncated_retain_tag_lines"] == 0     # 잘림 없는 줄만 있으면 키는 있고 값은 0
    assert {"transport": "mcp", "client": "quince", "lines": 2} in audit["summary"]
    assert "user:kauri-gate" in audit["unattributed_tags"]
    assert "kind:gotcha" not in rows and all(s["tag"] != "kind:gotcha" for s in audit["sources"])


def test_audit_is_optional_and_since_also_filters_audit_lines(hygiene, policy):
    assert "audit" not in build(hygiene, policy)
    everything = build(hygiene, policy, since=None, audit=[AUDIT])["audit"]
    by_tag = {(s["tag"], s["transport"], s["client"]): s["lines"] for s in everything["sources"]}
    assert by_tag[("topic:gotcha", "mcp", "codex")] == 1        # 8/25 줄은 --since 없을 때만
    assert everything["retain_tag_lines"] == 6


def audit_line(stored, **retain_tags):
    return json.dumps({"ts": "2026-09-03T01:00:00Z", "resolved_bank": "personal-demo",
                       "decision": "retain-tags", "reason": "tags-unchanged", "tool": "retain",
                       "retain_tags": {"transport": "mcp", "client": "quince", "sent": stored,
                                       "stored": stored, **retain_tags}})


def test_audit_reports_lines_whose_stored_list_was_capped_by_the_gateway(hygiene, policy, tmp_path):
    """issue #99: stored 는 앞 64개만 실린다 - 65번째 뒤 문제 태그는 귀속에서 빠지므로 잘린 줄 수를 드러낸다."""
    stored = ["kind:fix"] * 63 + ["topic:gotcha"]                  # 앞 64개 안의 문제 태그는 그대로 귀속
    path = tmp_path / "audit.jsonl"
    path.write_text("\n".join([
        audit_line(stored, stored_count=65, stored_omitted=1),
        audit_line(stored),
    ]) + "\n", encoding="utf-8")

    audit = build(hygiene, policy, since=None, audit=[path])["audit"]

    assert audit["truncated_retain_tag_lines"] == 1
    assert audit["retain_tag_lines"] == 2
    assert [s["lines"] for s in audit["sources"] if s["tag"] == "topic:gotcha"] == [2]


# ============================ ② 평가 함수 위임 (규칙 사본 없음) ============================

def test_new_tags_equal_the_evaluator_output_for_every_fixture_document(hygiene, policy):
    for document in hygiene.read_documents(str(DOCUMENTS)):
        record = hygiene.evaluate_document(document, policy, rules)
        assert record["new"] == list(rules.evaluate_tags(document["tags"], policy).tags)


def test_classification_follows_the_evaluator_not_a_private_rule_copy(hygiene, policy, monkeypatch):
    """평가 함수 출력만 바꿔도 분류가 따라간다: 분석기가 태그 규칙을 따로 갖고 있지 않다."""
    def stub(tags, pol):
        return rules.TagVerdict(tags=["kind:gotcha"],
                                normalized_tags=(("topic:whatever", "kind:gotcha"),),
                                violations=(rules.Violation(rules.RULE_TAGS_MISSING_PREFIX, "x"),))
    monkeypatch.setattr(rules, "evaluate_tags", stub)
    record = hygiene.evaluate_document({"id": "x", "tags": ["topic:whatever"]}, policy, rules)
    assert [(p["type"], p["tag"], p["to"]) for p in record["problems"]] == [
        ("misfiled-slot", "topic:whatever", ["kind:gotcha"])]
    assert record["missing_kind"] is True


def test_real_policy_file_end_to_end(hygiene):
    """정책 파일의 personal-demo 로 같은 분류가 나온다(fixture 정책과 별개의 연결 확인)."""
    rules_mod, policies = hygiene._load_gateway()
    real = policies.from_file(str(ROOT / "tests" / "fixtures" / "policies" / "sample" / "bank-policies.json")).for_bank("personal-demo")
    documents = [
        {"id": "a", "tags": ["topic:gotcha", "project:hindsight-crew"]},
        {"id": "b", "tags": ["kind:fix", "user:demofull", "project:hindsight-crew"]},
        {"id": "c", "tags": ["project:myall-shore-kind-gotcha-topic-blog", "kind:fix"]},
        {"id": "d", "tags": ["project:hindsight-crew"]},
        {"id": "e", "tags": ["project:hindsight-crew", "kind:decision", "user:demo"]},
    ]
    report = hygiene.build_report(documents, real, rules_mod, bank="personal-demo")
    types = report["problem_types"]
    assert (types["misfiled-slot"]["documents"], types["user-split"]["documents"],
            types["merged-slug"]["documents"], report["missing_kind"]["documents"]) == (1, 1, 1, 1)


# ============================ CLI ============================

def run_cli(hygiene, capsys, *argv):
    code = hygiene.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_cli_table_output(hygiene, capsys):
    code, out, err = run_cli(hygiene, capsys, "report", str(DOCUMENTS), "--policy", str(POLICY),
                             "--since", SINCE, "--audit", str(AUDIT))
    assert code == 0 and err == ""
    for expected in ("합쳐진 slug", "칸 오배정", "user 분열", "[kind 누락] 문서 1건", "[참고값]",
                     "project 없는 문서: 3건 (23.1%)", "mcp", "quince", "claude-code"):
        assert expected in out
    assert "\u2014" not in out


def test_cli_json_output_is_machine_readable(hygiene, capsys):
    code, out, _ = run_cli(hygiene, capsys, "report", str(DOCUMENTS), "--policy", str(POLICY),
                           "--since", SINCE, "--json")
    assert code == 0
    data = json.loads(out)
    assert data["schema_version"] == 1 and data["bank"] == "personal-demo" and data["since"] == SINCE
    assert data["problem_types"]["user-split"]["documents"] == 2
    assert any(d["id"] == "misfile-kind" and d["new"] == ["project:hindsight-crew", "kind:gotcha"]
               for d in data["documents"])


@pytest.mark.parametrize("argv, message", [
    (["report", str(DOCUMENTS), "--policy", str(POLICY), "--bank", "no-such-bank"], "not declared"),
    (["report", str(DOCUMENTS), "--policy", str(POLICY), "--since", "yesterday"], "--since"),
    (["report", str(FIXTURES / "missing.jsonl"), "--policy", str(POLICY)], "missing.jsonl"),
])
def test_cli_errors_exit_2_with_a_message(hygiene, capsys, argv, message):
    code, out, err = run_cli(hygiene, capsys, *argv)
    assert code == 2 and out == "" and message in err


def test_broken_jsonl_line_aborts_with_its_line_number(hygiene, tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"id": "ok", "tags": []}\n{not json}\n', encoding="utf-8")
    with pytest.raises(hygiene.HygieneError, match=r"bad.jsonl:2"):
        hygiene.read_documents(str(bad))


def test_help_runs_under_plain_python3():
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "tag_hygiene.py"), "--help"],
                          capture_output=True, text=True)
    assert proc.returncode == 0 and "report" in proc.stdout and "export" in proc.stdout


# ============================ ③ 내보내기 ============================

class ScriptedTransport:
    """응답을 순서대로 돌려주는 가짜 transport. 호출은 전부 기록한다."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, path, body=None, authenticated=True):
        self.calls.append((method, path))
        status, payload = self.responses.pop(0)
        return status, payload, json.dumps(payload)


def page(items, total, offset=0, limit=100):
    return 200, {"items": items, "total": total, "limit": limit, "offset": offset}


def make_docs(count):
    return [{"id": "doc-%03d" % i, "created_at": "2026-09-0%dT00:00:00Z" % (1 + i % 9),
             "tags": ["kind:fix"], "content": "본문은 내보내지 않는다"} for i in range(count)]


def test_export_paginates_and_keeps_only_id_created_at_tags(hygiene):
    docs = make_docs(250)
    docs[1] = {"document_id": "alt-id", "created_at": None, "tags": None}
    transport = ScriptedTransport([page(docs[:100], 250, 0), page(docs[100:200], 250, 100),
                                   page(docs[200:], 250, 200)])
    rows = hygiene.export_documents(transport, "personal-demo")
    assert len(rows) == 250
    assert rows[0] == {"id": "doc-000", "created_at": "2026-09-01T00:00:00Z", "tags": ["kind:fix"]}
    assert rows[1] == {"id": "alt-id", "created_at": None, "tags": []}
    assert [c[1] for c in transport.calls] == [
        "/v1/default/banks/personal-demo/documents?limit=100&offset=%d" % o for o in (0, 100, 200)]


def test_export_stops_when_a_listed_document_has_no_created_at(hygiene):
    """없으면 report --since 가 전부 '시각 없음'으로 빼고 조용히 0건을 스캔한다."""
    with pytest.raises(hygiene.HygieneError, match="lacks created_at"):
        hygiene.export_documents(ScriptedTransport([page([{"id": "a", "tags": []}], 1)]), "personal-demo")


def test_export_only_issues_get_requests(hygiene):
    transport = ScriptedTransport([page(make_docs(1), 1)])
    hygiene.export_documents(transport, "personal-demo")
    assert {method for method, _ in transport.calls} == {"GET"}
    http = hygiene.HttpTransport("http://localhost:8888", "unused")
    for method in ("POST", "PATCH", "DELETE", "PUT"):
        with pytest.raises(hygiene.HygieneError, match="read-only"):
            http.request(method, "/v1/default/banks/personal-demo/documents")


@pytest.mark.parametrize("name, responses, message", [
    ("total changed", [page(make_docs(100), 150, 0), page(make_docs(50), 151, 100)],
     "API total changed during pagination: 150 -> 151"),
    ("overshoot", [page(make_docs(4), 3, 0)], "pagination returned more items than total"),
    ("early stop", [page(make_docs(2), 5, 0), page([], 5, 2)], "pagination stopped before reported total"),
    ("http error", [(500, {"detail": "boom"})], "list failed with HTTP 500"),
    ("not an object", [(200, ["nope"])], "page response is not an object"),
    ("no total", [(200, {"items": [], "limit": 100, "offset": 0})], "lacks integer total"),
    ("no items", [(200, {"total": 1, "limit": 100, "offset": 0})], "lacks list items"),
])
def test_export_rejects_inconsistent_pagination(hygiene, migrate, name, responses, message):
    with pytest.raises(hygiene.HygieneError, match=message):
        hygiene.export_documents(ScriptedTransport(list(responses)), "personal-demo")
    # tag_migrate.list_paginated 와 같은 계약: 같은 입력에 같은 거부
    with pytest.raises(migrate.MigrationError, match=message):
        migrate.list_paginated(ScriptedTransport(list(responses)), "/v1/default/banks/personal-demo/documents")


@pytest.mark.parametrize("bank", ["", "bad/bank", "../x", "a b", "x" * 129])
def test_export_rejects_bank_names_that_could_alter_the_path(hygiene, bank):
    transport = ScriptedTransport([])
    with pytest.raises(hygiene.HygieneError, match="invalid bank name"):
        hygiene.export_documents(transport, bank)
    assert transport.calls == []


def test_export_rejects_a_document_without_an_id(hygiene):
    with pytest.raises(hygiene.HygieneError, match="lacks id"):
        hygiene.export_documents(ScriptedTransport([page([{"tags": []}], 1)]), "personal-demo")


def test_run_export_writes_pure_jsonl_to_stdout_and_summary_to_stderr(hygiene):
    out, err = io.StringIO(), io.StringIO()
    args = hygiene.build_parser().parse_args(["export", "--bank", "personal-demo"])
    code = hygiene.run_export(args, out=out, err=err,
                              transport=ScriptedTransport([page(make_docs(3), 3)]))
    assert code == 0
    lines = out.getvalue().splitlines()
    assert len(lines) == 3 and all(set(json.loads(l)) == {"id", "created_at", "tags"} for l in lines)
    assert "exported 3 documents" in err.getvalue() and "exported" not in out.getvalue()


def test_run_export_requires_the_token_from_the_environment(hygiene, monkeypatch):
    monkeypatch.delenv("HINDSIGHT_API_MCP_AUTH_TOKEN", raising=False)
    args = hygiene.build_parser().parse_args(["export"])
    with pytest.raises(hygiene.HygieneError, match="HINDSIGHT_API_MCP_AUTH_TOKEN"):
        hygiene.run_export(args, out=io.StringIO(), err=io.StringIO())


def test_script_is_container_safe_no_module_level_gateway_import():
    """export 는 gateway 패키지가 없는 컨테이너 안에서 돈다: 모듈 수준 import 는 표준 라이브러리뿐이어야 한다."""
    tree = ast.parse((ROOT / "scripts" / "tag_hygiene.py").read_text(encoding="utf-8"))
    names = {(a.name if isinstance(n, ast.Import) else n.module).split(".")[0]
             for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    assert "gateway" not in names and names <= set(sys.stdlib_module_names)


def test_wrapper_pipes_the_script_into_the_container_and_is_valid_bash():
    wrapper = ROOT / "scripts" / "tag-hygiene-export.sh"
    text = wrapper.read_text(encoding="utf-8")
    assert "set -euo pipefail" in text
    assert 'docker compose exec -T hindsight python - export ${bank_args[@]+"${bank_args[@]}"} "$@" < scripts/tag_hygiene.py' in text
    assert os.access(wrapper, os.X_OK)
    assert subprocess.run(["bash", "-n", str(wrapper)]).returncode == 0     # 구문만 검사, 실행 안 함


# ============================ 이슈 #109 항목 5: 256자로 잘린 감사 태그의 귀속 ============================
# 게이트웨이는 감사 줄의 태그를 t[:256] 으로 싣는다(gateway/audit.py TAG_LOG_MAX). 그래서 256자를 넘는 문제 태그는
# 정확 일치로는 영영 귀속되지 않고, 접두 일치로 귀속하되 "길이 잘림 귀속"으로 표기한다.
# 256자 이하 태그는 기존 정확 일치 결과가 그대로여야 한다(표기 없음, 줄 수 동일).

TRUNCATED_NOTE = "길이 잘림 귀속"


def problem(tag, kind="topic-in-project-slot"):
    return {"type": kind, "tag": tag}


def attribute(hygiene, tmp_path, tags, logged):
    path = tmp_path / "audit.jsonl"
    path.write_text("\n".join(audit_line(stored) for stored in logged) + "\n", encoding="utf-8")
    return hygiene.attribute_sources([problem(t) for t in tags], [path], "personal-demo", None)


def test_script_cap_matches_the_gateway_audit_cap(hygiene):
    from gateway import audit
    assert hygiene.TAG_LOG_MAX == audit.TAG_LOG_MAX == 256


def test_tag_over_the_log_cap_is_attributed_by_prefix_and_marked(hygiene, tmp_path):
    long_tag = "topic:" + "x" * 294                      # 300자
    assert len(long_tag) == 300
    info = attribute(hygiene, tmp_path, [long_tag], [[long_tag[:256], "kind:fix"], [long_tag[:256]]])
    assert [(s["tag"], s["lines"], s.get("attribution")) for s in info["sources"]] == [(long_tag, 2, TRUNCATED_NOTE)]
    assert info["unattributed_tags"] == []


@pytest.mark.parametrize("length", [1, 100, 255, 256])
def test_tag_up_to_the_cap_keeps_the_exact_match_result_without_a_mark(hygiene, tmp_path, length):
    tag = ("topic:" + "y" * length)[:length]
    info = attribute(hygiene, tmp_path, [tag], [[tag, "kind:fix"]])
    assert [(s["tag"], s["lines"]) for s in info["sources"]] == [(tag, 1)]
    assert "attribution" not in info["sources"][0]
    assert info["unattributed_tags"] == []


def test_exact_256_char_tag_is_not_attributed_from_a_different_short_log_tag(hygiene, tmp_path):
    tag = "topic:" + "z" * 250                           # 256자
    assert len(tag) == 256
    info = attribute(hygiene, tmp_path, [tag], [[tag[:255]], [tag[:100]]])
    assert info["sources"] == [] and info["unattributed_tags"] == [tag]


def test_long_tag_is_not_attributed_from_a_log_tag_shorter_than_the_cap(hygiene, tmp_path):
    long_tag = "topic:" + "q" * 294
    info = attribute(hygiene, tmp_path, [long_tag], [[long_tag[:255]]])
    assert info["sources"] == [] and info["unattributed_tags"] == [long_tag]


def test_a_256_char_log_tag_shared_by_an_exact_tag_and_a_longer_tag_marks_both(hygiene, tmp_path):
    """로그의 256자 줄은 진짜 256자 태그일 수도, 더 긴 태그가 잘린 것일 수도 있다: 둘 다 표기해 모호함을 드러낸다."""
    exact = "topic:" + "w" * 250
    longer = exact + "tail"
    info = attribute(hygiene, tmp_path, [exact, longer], [[exact]])
    rows = {s["tag"]: (s["lines"], s.get("attribution")) for s in info["sources"]}
    assert rows == {exact: (1, TRUNCATED_NOTE), longer: (1, TRUNCATED_NOTE)}


def test_two_long_tags_with_the_same_prefix_are_both_marked_not_silently_split(hygiene, tmp_path):
    first = "topic:" + "v" * 250 + "-one"
    second = "topic:" + "v" * 250 + "-two"
    info = attribute(hygiene, tmp_path, [first, second], [[first[:256]]])
    assert {s["tag"]: s.get("attribution") for s in info["sources"]} == {first: TRUNCATED_NOTE, second: TRUNCATED_NOTE}


def test_text_report_shows_the_truncated_attribution_mark(hygiene, policy, tmp_path):
    long_tag = "topic:" + "x" * 294
    path = tmp_path / "audit.jsonl"
    path.write_text(audit_line([long_tag[:256]]) + "\n", encoding="utf-8")
    report = build(hygiene, policy, since=None, audit=[path], documents=DOCUMENTS)
    report["audit"] = hygiene.attribute_sources([problem(long_tag)], [path], "personal-demo", None)
    assert TRUNCATED_NOTE in hygiene.render_table(report)


# ===== 이슈 #117: 이스케이프 바이트 상한으로 256자보다 짧게 잘린 감사 태그의 귀속 =====
# 제어 문자가 많은 태그는 게이트웨이가 1024바이트로 잘라 256자 미만으로 남고 그 줄에 stored_truncated 가 붙는다.
# 감사 줄은 이 PR의 audit 헬퍼로 실제 생성한다.

CTRL_TAG = "x" + "\x01" * 200             # 201자, 이스케이프 후 1206바이트: 접두 없는 뱅크에 저장되는 문제 태그


def gateway_stored_line(stored):
    """게이트웨이가 retain-tags 줄에 싣는 `stored` 와 잘림 표기를 audit 헬퍼로 만든 감사 줄."""
    from gateway import audit
    shown, count, truncated = audit.tag_list(stored)
    meta = {"transport": "mcp", "client": "quince", "sent": shown, "stored": shown,
            **audit.tag_list_keys("stored", shown, count, truncated)}
    return json.dumps({"ts": "2026-09-03T01:00:00Z", "resolved_bank": "personal-demo",
                       "decision": "retain-tags", "reason": "tags-unchanged", "tool": "retain",
                       "retain_tags": meta}, ensure_ascii=False)


def attribute_gateway_lines(hygiene, tmp_path, tags, stored_lists):
    path = tmp_path / "audit.jsonl"
    path.write_text("\n".join(gateway_stored_line(s) for s in stored_lists) + "\n", encoding="utf-8")
    return hygiene.attribute_sources([problem(t) for t in tags], [path], "personal-demo", None)


def test_script_byte_cap_matches_the_gateway_audit_cap(hygiene):
    from gateway import audit
    assert hygiene.TAG_LOG_BYTES_MAX == audit.TAG_LOG_BYTES_MAX == 1024


def test_control_char_tag_cut_below_256_chars_is_attributed_marked_and_counted(hygiene, tmp_path):
    from gateway import audit
    logged = audit.tag_list([CTRL_TAG])[0][0]
    assert len(logged) < 201 and CTRL_TAG.startswith(logged) and logged != CTRL_TAG     # 실제로 짧게 잘렸다

    info = attribute_gateway_lines(hygiene, tmp_path, [CTRL_TAG], [[CTRL_TAG, "kind:fix"], [CTRL_TAG]])

    assert [(s["tag"], s["lines"], s.get("attribution")) for s in info["sources"]] == [(CTRL_TAG, 2, TRUNCATED_NOTE)]
    assert info["unattributed_tags"] == []
    assert info["truncated_retain_tag_lines"] == 2          # stored_truncated 줄도 잘린 줄로 센다


def test_short_uncut_log_tag_is_not_a_prefix_attribution_even_on_a_truncated_line(hygiene, tmp_path):
    """같은 줄에 잘린 태그가 있어도 짧은 정상 태그를 접두로 읽어 무관한 긴 문제 태그에 귀속하지 않는다."""
    long_problem = "topic:" + "k" * 300
    info = attribute_gateway_lines(hygiene, tmp_path, [long_problem, CTRL_TAG], [["topic:kkk", CTRL_TAG]])
    assert {s["tag"] for s in info["sources"]} == {CTRL_TAG}
    assert info["unattributed_tags"] == [long_problem]


def test_lines_without_stored_truncated_keep_the_256_char_rule_and_exact_match(hygiene, tmp_path):
    # 옛 줄(키 없음): 256자 미만 접두는 그대로 귀속하지 않고, 정확 일치는 표기 없이 귀속한다
    info = attribute(hygiene, tmp_path, [CTRL_TAG], [[CTRL_TAG[:150]], [CTRL_TAG]])
    assert [(s["tag"], s["lines"], s.get("attribution")) for s in info["sources"]] == [(CTRL_TAG, 1, None)]
    assert info["truncated_retain_tag_lines"] == 0
