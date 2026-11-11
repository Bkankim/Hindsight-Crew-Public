"""#43 A 단계: 컨테이너 쪽 `scripts/tag_migrate.py` 의 manifest 실행 모드(--apply-manifest)와
읽기 전용 라벨 export(--export-labels), 그리고 `scripts/tag-migrate.sh` 의 manifest 분기.

고정하는 것:
  ① 매핑 유형 행렬 x manifest dry-run 요약 (계획기 출력 쪽 행렬은 tests/test_tag_retag_plan.py)
  ② live: manifest 밖·unresolved 문서에는 PATCH 가 가지 않는다 / 현재 태그가 old 와 다르면 건너뛰고 보고한다 /
     PATCH body 의 tags 는 manifest 의 new 와 정확히 같다 / 재시도·3연속 실패 중단·배치별 drain·checkpoint
  ③ 라벨 export: 라벨만 나오고 본문은 출력에 없다 / 본문 필드를 못 찾으면 오류로 중단한다
  ④ 사본 없음: manifest 경로의 함수들이 transform_tags·ALIAS_MAP 등을 부르지 않는다(AST) + 셸 분기는 정책 alias 를 주입하지 않는다
전부 가짜 transport 와 fixture 로만 돈다. docker·라이브 뱅크·.env.local 을 쓰지 않는다.
"""
import ast
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import urllib.parse
from pathlib import Path

import pytest

from gateway import rules
from gateway.bank_policies import BankPolicies

from test_tag_retag_plan import (
    CHANGE_ROWS, COMMITTED_MAP, DOCUMENTS, FIXTURES, LABELS, NOT_LISTED, POLICY_PATH, RESTORE_MAP, ROOT,
    SCRIPTS, UNRESOLVED_ROWS, load_script,
)

MARKER = "MARKER-BODY-7431"


@pytest.fixture(scope="module")
def migrate():
    module = load_script("tag_migrate.py", "tag_migrate_for_retag")
    assert module.ALIAS_MAP is None and module.DELETE_TAGS is None and module.REMOVE_TAGS is None
    return module


@pytest.fixture(scope="module")
def planner():
    return load_script("tag_retag_plan.py", "tag_retag_plan_for_manifest")


@pytest.fixture(scope="module")
def documents(planner):
    return planner.tag_hygiene.read_documents(str(DOCUMENTS))


@pytest.fixture(scope="module")
def manifest(planner, documents):
    mapping = planner.load_mapping(str(COMMITTED_MAP))
    mapping["merged_restore"] = planner.load_mapping(str(RESTORE_MAP))["merged_restore"]
    policy = BankPolicies.from_file(str(POLICY_PATH)).for_bank("personal-demo")
    return planner.build_manifest(documents, mapping, planner.read_labels(str(LABELS)), policy, rules)


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return str(path)


class StoreTransport:
    """가짜 뱅크: 문서 목록(tags 포함)·상세·PATCH·operations(비어 있음)에 답하고 모든 호출을 기록한다."""

    def __init__(self, tags_by_id, fail_patch=(), flaky_patch=(), bodies=None, list_extra=None, on_page=None, hide_first=()):
        self.tags = {k: list(v) for k, v in tags_by_id.items()}
        self.list_extra = list_extra or {}      # 목록 항목·PATCH 응답에 섞이는 필드(본문이 새는지 보려는 미끼)
        self.on_page = on_page                  # (이번 요청의 offset, 이 스토어) -> None, 응답을 만든 직후 호출
        self.list_offsets = []
        self.hide_first = set(hide_first)       # 첫 번째 목록 조회 동안만 목록에서 빠지는 id(페이지 사이 밀림을 흉내)
        self.bodies = bodies or {}
        self.fail_patch = set(fail_patch)       # 항상 422
        self.flaky_patch = set(flaky_patch)     # 첫 호출만 503
        self.calls = []

    @property
    def patches(self):
        return [(path, body) for method, path, body in self.calls if method == "PATCH"]

    def request(self, method, path, body=None, authenticated=True):
        self.calls.append((method, path, body))
        base = "/v1/default/banks/personal-demo"
        parsed = urllib.parse.urlparse(path)
        query = urllib.parse.parse_qs(parsed.query)
        if method == "GET" and parsed.path == base + "/documents":
            limit, offset = int(query["limit"][0]), int(query["offset"][0])
            rows = [dict(self.list_extra, id=k, created_at="2026-08-30T00:00:00Z", tags=list(v))
                    for k, v in self.tags.items() if not (k in self.hide_first and self.list_offsets.count(0) < 1)]
            response = self._ok({"items": rows[offset:offset + limit], "total": len(rows), "limit": limit, "offset": offset})
            self.list_offsets.append(offset)
            if self.on_page:
                self.on_page(offset, self)
            return response
        if parsed.path.startswith(base + "/documents/"):
            document_id = urllib.parse.unquote(parsed.path.rsplit("/", 1)[1])
            if method == "GET":
                if document_id not in self.tags:
                    return 404, {"detail": "not found"}, "not found"
                return self._ok(dict(self.bodies.get(document_id, {}), id=document_id, tags=list(self.tags[document_id])))
            if method == "PATCH":
                if document_id in self.fail_patch:
                    return 422, {"error": "injected"}, "injected"
                if document_id in self.flaky_patch and sum(
                        1 for m, p, _ in self.calls if m == "PATCH" and p == path) == 1:
                    return 503, {"error": "injected"}, "injected"
                self.tags[document_id] = list(body["tags"])
                return self._ok(dict(self.list_extra, success=True))
        if method == "GET" and parsed.path == base + "/operations":
            return self._ok({"operations": [], "total": 0, "limit": 100, "offset": 0})
        if method == "GET" and parsed.path == base + "/tags":
            return self._ok({"items": []})
        raise AssertionError("unexpected request %s %s" % (method, path))

    @staticmethod
    def _ok(payload):
        return 200, payload, json.dumps(payload)


def store_for(documents, **kwargs):
    return StoreTransport({d["id"]: d["tags"] for d in documents}, **kwargs)


def apply_live(migrate, transport, manifest, tmp_path, **kwargs):
    return migrate.apply_manifest_live(
        transport, manifest, str(tmp_path / "state.json"),
        drain_fn=kwargs.pop("drain_fn", lambda t: None), request_sleeper=lambda s: None, **kwargs)


# ============================ ① 매핑 유형 행렬 x manifest dry-run 요약 ============================

def dry_run(migrate, tmp_path, manifest, capsys):
    path = write_json(tmp_path / "manifest.json", manifest)
    assert migrate.main(["--dry-run", "--apply-manifest", path]) == 0
    return capsys.readouterr().out.splitlines()


@pytest.mark.parametrize("doc_id,expected_new,expected_types", CHANGE_ROWS, ids=[r[0] for r in CHANGE_ROWS])
def test_change_rows_in_dry_run_summary(migrate, manifest, documents, tmp_path, capsys, doc_id, expected_new, expected_types):
    lines = dry_run(migrate, tmp_path, manifest, capsys)
    old = next(d["tags"] for d in documents if d["id"] == doc_id)
    expected = "%s | %s | %s | %s" % (doc_id, json.dumps(old, ensure_ascii=False),
                                      json.dumps(expected_new, ensure_ascii=False), ",".join(expected_types))
    assert expected in lines
    assert not any(line.startswith("UNRESOLVED %s " % doc_id) for line in lines)


@pytest.mark.parametrize("doc_id,reason_prefix", UNRESOLVED_ROWS, ids=[r[0] for r in UNRESOLVED_ROWS])
def test_unresolved_rows_in_dry_run_summary(migrate, manifest, documents, tmp_path, capsys, doc_id, reason_prefix):
    lines = dry_run(migrate, tmp_path, manifest, capsys)
    (line,) = [l for l in lines if l.startswith("UNRESOLVED %s | " % doc_id)]
    old = next(d["tags"] for d in documents if d["id"] == doc_id)
    assert json.dumps(old, ensure_ascii=False) in line
    assert line.split(" | ", 2)[2].startswith(reason_prefix)
    assert not any(l.startswith(doc_id + " | ") for l in lines)      # 적용 예정 목록에는 없다


@pytest.mark.parametrize("doc_id", NOT_LISTED)
def test_untouched_documents_are_absent_from_dry_run_summary(migrate, manifest, tmp_path, capsys, doc_id):
    assert not any(doc_id in line for line in dry_run(migrate, tmp_path, manifest, capsys))


def test_dry_run_summary_counts_and_zero_mutations(migrate, manifest, tmp_path, capsys, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("dry-run must not build a transport")
    monkeypatch.setattr(migrate, "HttpTransport", forbidden)       # 네트워크·토큰 없이 돈다
    monkeypatch.delenv("HINDSIGHT_API_MCP_AUTH_TOKEN", raising=False)
    lines = dry_run(migrate, tmp_path, manifest, capsys)
    assert "SUMMARY documents=%d unresolved=%d" % (len(CHANGE_ROWS), len(UNRESOLVED_ROWS)) in lines
    for kind, count in {"label-to-kind": 5, "merged-restore": 2, "project-merge": 7, "user-to-topic": 2}.items():
        assert "SUMMARY type %s %d" % (kind, count) in lines
    assert "SUMMARY untargeted_changes=1" in lines
    assert "SUMMARY documents_sha256=%s" % manifest["documents_sha256"] in lines
    assert lines[-1] == "DRY-RUN complete; mutations=0"


# ============================ ① 사본 없음 / 설정 없이 동작 ============================

MANIFEST_PATH_FUNCTIONS = (
    "run_apply_manifest", "run_export_labels", "apply_manifest_live", "validate_apply_manifest",
    "manifest_digest", "apply_manifest_lines", "injected_or_file", "export_labels", "extract_label",
    "document_body", "reject_flags", "require_token",
)
FORBIDDEN_NAMES = {
    "transform_tags", "plan_document", "make_plan", "ALIAS_MAP", "REMOVE_TAGS", "DELETE_TAGS",
    "require_configuration", "run_live", "allowed_pipeline_outputs", "count_pipeline_violations",
    "slugify", "BACKFILL_TAGS", "BACKFILL_ID",
}


def test_manifest_path_never_references_the_rule_copy():
    tree = ast.parse((SCRIPTS / "tag_migrate.py").read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    reachable, pending = set(), list(MANIFEST_PATH_FUNCTIONS)
    while pending:
        name = pending.pop()
        if name in reachable:
            continue
        reachable.add(name)
        for node in ast.walk(functions[name]):
            if isinstance(node, ast.Name) and node.id in functions:
                pending.append(node.id)
    used = {node.id for name in reachable for node in ast.walk(functions[name]) if isinstance(node, ast.Name)}
    assert not (used & FORBIDDEN_NAMES), used & FORBIDDEN_NAMES
    assert {"list_paginated", "request_with_failure_cap", "drain", "save_json"} <= reachable     # 기존 계약을 재사용한다


def test_manifest_modes_need_no_injected_configuration(migrate, manifest, tmp_path, capsys):
    # 정책 prelude(ALIAS_MAP 등)가 없는 모듈에서 dry-run 이 끝까지 돈다 = require_configuration 을 요구하지 않는다
    assert migrate.ALIAS_MAP is None
    path = write_json(tmp_path / "m.json", manifest)
    assert migrate.main(["--dry-run", "--apply-manifest", path]) == 0
    with pytest.raises(migrate.MigrationError, match="configuration prelude missing"):
        migrate.main(["--dry-run"])          # 기존 경로는 그대로 설정을 요구한다


# ============================ ② live ============================

def test_live_patches_only_manifest_documents_with_exact_new_tags(migrate, manifest, documents, tmp_path):
    transport = store_for(documents)
    result = apply_live(migrate, transport, manifest, tmp_path)
    planned = {e["id"]: e for e in manifest["documents"]}
    assert sorted(result["patched"]) == sorted(planned)
    assert result["drift"] == result["already"] == result["missing"] == []
    # PATCH 는 manifest 문서에만, body 는 {"tags": new} 와 정확히 같다
    assert sorted(path.rsplit("/", 1)[1] for path, _ in transport.patches) == sorted(planned)
    for path, body in transport.patches:
        assert body == {"tags": planned[path.rsplit("/", 1)[1]]["new"]}
    # manifest 밖(clean·cross-reference·alias-only)과 unresolved 는 태그가 그대로다
    untouched = [d for d in documents if d["id"] not in planned]
    assert {d["id"] for d in untouched} >= set(NOT_LISTED) | {row[0] for row in UNRESOLVED_ROWS}
    for doc in untouched:
        assert transport.tags[doc["id"]] == doc["tags"]
    assert all(method in ("GET", "PATCH") for method, _, _ in transport.calls)


def test_live_skips_and_reports_documents_whose_current_tags_differ_from_old(migrate, manifest, documents, tmp_path, capsys):
    transport = store_for(documents)
    transport.tags["merge-birch"] = ["project:birch", "kind:completion", "topic:added-later"]
    result = apply_live(migrate, transport, manifest, tmp_path)
    assert result["drift"] == ["merge-birch"]
    assert "merge-birch" not in [p.rsplit("/", 1)[1] for p, _ in transport.patches]
    assert transport.tags["merge-birch"] == ["project:birch", "kind:completion", "topic:added-later"]
    out = capsys.readouterr().out
    assert "DRIFT merge-birch | current=" in out and "topic:added-later" in out
    assert "drift=1" in out and len(result["patched"]) == len(manifest["documents"]) - 1


def test_live_treats_old_tags_as_a_multiset_so_order_changes_are_not_drift(migrate, manifest, documents, tmp_path):
    transport = store_for(documents)
    transport.tags["merge-birch"] = ["kind:completion", "project:birch"]
    result = apply_live(migrate, transport, manifest, tmp_path)
    assert "merge-birch" in result["patched"] and result["drift"] == []


def test_live_skips_documents_that_already_have_the_new_tags(migrate, manifest, documents, tmp_path):
    transport = store_for(documents)
    new = next(e["new"] for e in manifest["documents"] if e["id"] == "label-done")
    transport.tags["label-done"] = list(new)
    result = apply_live(migrate, transport, manifest, tmp_path)
    assert result["already"] == ["label-done"]
    assert "label-done" not in [p.rsplit("/", 1)[1] for p, _ in transport.patches]


def test_live_reports_documents_missing_from_the_bank(migrate, manifest, documents, tmp_path):
    transport = store_for([d for d in documents if d["id"] != "merge-tupelo"])
    result = apply_live(migrate, transport, manifest, tmp_path)
    assert result["missing"] == ["merge-tupelo"]
    assert "merge-tupelo" not in [p.rsplit("/", 1)[1] for p, _ in transport.patches]


def test_live_resumes_from_its_checkpoint_and_never_repatches(migrate, manifest, documents, tmp_path):
    first = store_for(documents)
    apply_live(migrate, first, manifest, tmp_path)
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert sorted(state["processed"]) == sorted(e["id"] for e in manifest["documents"])
    second = StoreTransport(first.tags)                # 적용이 끝난 뱅크
    result = apply_live(migrate, second, manifest, tmp_path)
    assert result["patched"] == [] and second.patches == []


def test_live_refuses_a_checkpoint_from_another_manifest(migrate, manifest, documents, tmp_path):
    apply_live(migrate, store_for(documents), manifest, tmp_path)
    other = json.loads(json.dumps(manifest))
    other["documents"] = other["documents"][:-1]
    other["summary"]["documents"] -= 1
    other["summary"]["by_type"] = {}
    for entry in other["documents"]:
        for kind in entry["types"]:
            other["summary"]["by_type"][kind] = other["summary"]["by_type"].get(kind, 0) + 1
    other["summary"]["by_type"] = dict(sorted(other["summary"]["by_type"].items()))
    other["documents_sha256"] = migrate.documents_digest(other["documents"])
    transport = store_for(documents)
    with pytest.raises(migrate.MigrationError, match="different manifest"):
        apply_live(migrate, transport, other, tmp_path)
    assert transport.calls == []


def test_live_retries_a_transient_5xx_then_succeeds(migrate, manifest, documents, tmp_path):
    transport = store_for(documents, flaky_patch={"merge-birch"})
    result = apply_live(migrate, transport, manifest, tmp_path)
    assert "merge-birch" in result["patched"]
    assert len([p for p, _ in transport.patches if p.endswith("/merge-birch")]) == 2


def test_live_stops_after_three_consecutive_failed_documents(migrate, manifest, documents, tmp_path):
    ordered = [e["id"] for e in manifest["documents"]]
    transport = store_for(documents, fail_patch=set(ordered[:3]))
    with pytest.raises(migrate.MigrationError, match="3 consecutive failed documents"):
        apply_live(migrate, transport, manifest, tmp_path)
    assert [p.rsplit("/", 1)[1] for p, _ in transport.patches] == ordered[:3]     # 4xx 는 재시도하지 않는다


def test_live_reports_a_non_consecutive_failure_after_finishing_the_rest(migrate, manifest, documents, tmp_path):
    victim = manifest["documents"][1]["id"]
    transport = store_for(documents, fail_patch={victim})
    with pytest.raises(migrate.MigrationError, match="1 document mutation"):
        apply_live(migrate, transport, manifest, tmp_path)
    assert transport.tags[victim] == next(d["tags"] for d in documents if d["id"] == victim)
    assert len(transport.patches) == len(manifest["documents"])


def test_live_relists_and_drains_once_per_batch(migrate, manifest, documents, tmp_path, monkeypatch):
    monkeypatch.setattr(migrate, "BATCH_SIZE", 5)
    drained = []
    transport = store_for(documents)
    apply_live(migrate, transport, manifest, tmp_path, drain_fn=lambda t: drained.append(len(transport.patches)))
    batches = -(-len(manifest["documents"]) // 5)
    assert len(drained) == batches + 1                      # 배치마다 + 요약 전 무조건 1회
    lists = [c for c in transport.calls if c[0] == "GET" and "/documents?" in c[1]]
    assert len(lists) == batches
    assert drained[0] == 5 and drained[-1] == len(manifest["documents"])      # drain 은 그 배치의 PATCH 뒤에 온다


def test_main_live_end_to_end_with_the_store_transport(migrate, manifest, documents, tmp_path, monkeypatch, capsys):
    # 목록 항목과 PATCH 응답에 본문이 실려 와도 live 출력에는 문서 id와 상태 줄만 나온다
    transport = store_for(documents, list_extra={"original_text": "[완료] %s 지어낸 본문" % MARKER})
    seen = {}

    def factory(base_url, token, log=True):
        seen["log"] = log
        return transport
    monkeypatch.setattr(migrate, "HttpTransport", factory)
    monkeypatch.setenv("HINDSIGHT_API_MCP_AUTH_TOKEN", "[REDACTED]")
    path = write_json(tmp_path / "m.json", manifest)
    assert migrate.main(["--live", "--apply-manifest", path, "--state", str(tmp_path / "s.json")]) == 0
    captured = capsys.readouterr()
    assert seen["log"] is False
    assert MARKER not in captured.out + captured.err and "지어낸" not in captured.out + captured.err
    assert "APPLY complete=%d" % len(manifest["documents"]) in captured.out
    assert "unresolved_untouched=%d" % len(UNRESOLVED_ROWS) in captured.out
    assert "DRAIN pending=0 processing=0" in captured.out
    assert captured.out.count("PATCHED ") == len(manifest["documents"])
    assert len(transport.patches) == len(manifest["documents"])


def test_live_transport_logs_bodies_only_when_asked(migrate, monkeypatch, capsys):
    # log=False 와 기본값의 차이를 고정한다(위 테스트가 log=False 를 요구하는 이유)
    class Response:
        status = 200
        def read(self):
            return json.dumps({"items": [{"original_text": MARKER}]}).encode()
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False
    monkeypatch.setattr(migrate.urllib.request, "urlopen", lambda request, timeout=None: Response())
    migrate.HttpTransport("http://h", "[REDACTED]", log=False).request("PATCH", "/x", {"tags": []})
    assert MARKER not in capsys.readouterr().out
    migrate.HttpTransport("http://h", "[REDACTED]").request("PATCH", "/x", {"tags": []})
    assert MARKER in capsys.readouterr().out


def test_live_requires_a_token(migrate, manifest, tmp_path, monkeypatch):
    monkeypatch.delenv("HINDSIGHT_API_MCP_AUTH_TOKEN", raising=False)
    path = write_json(tmp_path / "m.json", manifest)
    with pytest.raises(migrate.MigrationError, match="TOKEN"):
        migrate.main(["--live", "--apply-manifest", path])


def test_live_reports_non_string_current_tags_as_drift_instead_of_crashing(migrate, manifest, documents, tmp_path, capsys):
    transport = store_for(documents)
    transport.tags["merge-birch"] = [None, "kind:completion"]
    result = apply_live(migrate, transport, manifest, tmp_path)           # TypeError 로 중단하지 않는다
    assert result["drift"] == ["merge-birch"]
    assert "merge-birch" not in [p.rsplit("/", 1)[1] for p, _ in transport.patches]
    assert len(result["patched"]) == len(manifest["documents"]) - 1
    assert "DRIFT merge-birch | current=[null," in capsys.readouterr().out


def grow_after_first_page(store):
    if "late-arrival" not in store.tags:
        store.tags["late-arrival"] = ["kind:fix"]            # 다른 세션의 정상 retain


def test_live_tolerates_the_document_total_growing_during_pagination(migrate, manifest, documents, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(migrate, "PAGE_SIZE", 5)
    transport = store_for(documents, on_page=lambda offset, store: grow_after_first_page(store) if offset == 5 else None)
    result = apply_live(migrate, transport, manifest, tmp_path)
    assert len(result["patched"]) == len(manifest["documents"]) and result["missing"] == []
    assert "LIST total changed during pagination" in capsys.readouterr().out
    assert transport.tags["late-arrival"] == ["kind:fix"]                 # manifest 밖 문서는 그대로
    # 기존 list_paginated 는 그대로 엄격하다(이 완화는 manifest 모드 전용)
    strict = store_for(documents, on_page=lambda offset, store: grow_after_first_page(store) if offset == 5 else None)
    with pytest.raises(migrate.MigrationError, match="total changed"):
        migrate.list_paginated(strict, "/v1/default/banks/personal-demo/documents")


def test_live_rereads_once_before_calling_a_document_missing(migrate, manifest, documents, tmp_path):
    transport = store_for(documents, hide_first={"merge-tupelo"})       # 첫 조회에서만 빠짐
    result = apply_live(migrate, transport, manifest, tmp_path)
    assert "merge-tupelo" in result["patched"] and result["missing"] == []
    assert transport.list_offsets.count(0) == 2
    gone = store_for([d for d in documents if d["id"] != "merge-tupelo"])
    assert apply_live(migrate, gone, manifest, tmp_path / "other")["missing"] == ["merge-tupelo"]


def test_live_drains_once_more_before_finishing_even_when_nothing_is_patched(migrate, manifest, documents, tmp_path):
    first = store_for(documents)
    apply_live(migrate, first, manifest, tmp_path)
    drained = []
    second = StoreTransport(first.tags)                                   # 이전 실행이 끝까지 적용한 뱅크
    result = apply_live(migrate, second, manifest, tmp_path, drain_fn=lambda t: drained.append(1))
    assert result["patched"] == [] and second.patches == []
    assert drained == [1]                                                 # 재개 실행도 consolidation 을 기다린다


def test_live_resume_after_interruption_drains_even_if_the_rest_is_already_applied(migrate, manifest, documents, tmp_path):
    ordered = [e["id"] for e in manifest["documents"]]
    crashed = store_for(documents, fail_patch=set(ordered[:3]))
    with pytest.raises(migrate.MigrationError):
        apply_live(migrate, crashed, manifest, tmp_path)
    resumed = StoreTransport({k: (next(e["new"] for e in manifest["documents"] if e["id"] == k)
                                   if k in ordered else v) for k, v in crashed.tags.items()})
    drained = []
    apply_live(migrate, resumed, manifest, tmp_path, drain_fn=lambda t: drained.append(1))
    assert resumed.patches == [] and len(drained) >= 1


@pytest.mark.parametrize("mode", ["--dry-run", "--live"])
def test_a_hand_edited_manifest_is_refused_by_dry_run_and_live(migrate, manifest, tmp_path, monkeypatch, capsys, mode):
    edited = tampered(manifest, lambda m: m["documents"][0].update(new=m["documents"][0]["new"] + ["topic:sneaky"]))
    path = write_json(tmp_path / "edited.json", edited)
    built = []
    monkeypatch.setattr(migrate, "HttpTransport", lambda *a, **k: built.append(1))
    monkeypatch.setenv("HINDSIGHT_API_MCP_AUTH_TOKEN", "[REDACTED]")
    with pytest.raises(migrate.MigrationError, match="documents_sha256"):
        migrate.main([mode, "--apply-manifest", path, "--state", str(tmp_path / "s.json")])
    assert built == [] and capsys.readouterr().out == ""                  # 요약도 네트워크도 없다


def test_container_and_planner_compute_the_same_documents_digest(migrate, planner, manifest):
    assert migrate.documents_digest(manifest["documents"]) == planner.documents_digest(manifest["documents"])
    assert migrate.documents_digest([]) == planner.documents_digest([])


# ---- manifest 검증 / 플래그 조합 ----

def tampered(manifest, mutate):
    value = json.loads(json.dumps(manifest))
    mutate(value)
    return value


BAD_MANIFESTS = {
    "v1-id-list": lambda m: m.update(version=1),
    "wrong-bank": lambda m: m.update(bank="other"),
    "duplicate-id": lambda m: m["documents"].append(dict(m["documents"][0])),
    "planned-and-unresolved": lambda m: m["unresolved"].append({"id": m["documents"][0]["id"], "old": [], "reason": "x"}),
    "unchanged-entry": lambda m: m["documents"][0].update(new=list(reversed(m["documents"][0]["old"]))),
    "empty-new": lambda m: m["documents"][0].update(new=[]),
    "summary-count": lambda m: m["summary"].update(documents=999),
    "summary-by-type": lambda m: m["summary"]["by_type"].update({"project-merge": 99}),
    "no-summary": lambda m: m.pop("summary"),
    "no-digest": lambda m: m.pop("documents_sha256"),
    "wrong-digest": lambda m: m.update(documents_sha256="0" * 64),
    "edited-new-tags": lambda m: m["documents"][0].update(new=m["documents"][0]["new"] + ["topic:sneaky"]),
    "edited-old-tags": lambda m: m["documents"][1].update(old=m["documents"][1]["old"] + ["topic:sneaky"]),
    "dropped-document": lambda m: m["documents"].pop(),
}


@pytest.mark.parametrize("name", sorted(BAD_MANIFESTS))
def test_invalid_manifests_abort_before_any_request(migrate, manifest, tmp_path, name):
    bad = tampered(manifest, BAD_MANIFESTS[name])
    transport = StoreTransport({})
    with pytest.raises(migrate.MigrationError):
        apply_live(migrate, transport, bad, tmp_path)
    assert transport.calls == []


@pytest.mark.parametrize("extra", [["--manifest", "x.json"], ["--only", "retag"], ["--manifest-out", "o.json"],
                                   ["--ids-file", "ids.json"]])
def test_apply_manifest_rejects_legacy_flags(migrate, manifest, tmp_path, extra):
    path = write_json(tmp_path / "m.json", manifest)
    with pytest.raises(migrate.MigrationError, match="cannot be combined"):
        migrate.main(["--dry-run", "--apply-manifest", path] + extra)


def test_apply_manifest_needs_a_dry_run_or_live_mode(migrate, tmp_path):
    with pytest.raises(SystemExit):
        migrate.parse_args(["--apply-manifest", "m.json"])


def test_injected_input_is_used_for_dash(migrate, manifest, capsys, monkeypatch):
    monkeypatch.setattr(migrate, "INJECTED_INPUT_JSON", json.dumps(manifest))
    assert migrate.main(["--dry-run", "--apply-manifest", "-"]) == 0
    assert "SUMMARY documents=%d" % len(manifest["documents"]) in capsys.readouterr().out
    monkeypatch.setattr(migrate, "INJECTED_INPUT_JSON", None)
    with pytest.raises(migrate.MigrationError, match="no injected input"):
        migrate.main(["--dry-run", "--apply-manifest", "-"])


# ============================ ③ 라벨 export ============================

def body_docs():
    ids = ["by-original-text", "by-content", "by-text", "no-label-body", "long-label", "empty-body"]
    bodies = {
        "by-original-text": {"original_text": "[완료] %s 지어낸 첫 문서" % MARKER, "content": "[무시] 뒤 후보"},
        "by-content": {"content": "[발견] %s 지어낸 둘째" % MARKER},
        "by-text": {"text": "  [결정] %s 앞 공백 있는 셋째" % MARKER},
        "no-label-body": {"original_text": "라벨 없이 시작하는 %s 본문 [나중] 것" % MARKER},
        "long-label": {"original_text": "[열세글자를넘는아주긴라벨입니다] %s" % MARKER},
        "empty-body": {"original_text": ""},
    }
    return ids, bodies


def test_export_labels_prints_only_labels_never_bodies(migrate, capsys):
    ids, bodies = body_docs()
    transport = StoreTransport({i: ["kind:fix"] for i in ids}, bodies=bodies)
    out = io.StringIO()
    assert migrate.export_labels(transport, ids, out) == len(ids)
    rows = [json.loads(line) for line in out.getvalue().splitlines()]
    assert {r["id"]: r["label"] for r in rows} == {
        "by-original-text": "완료", "by-content": "발견", "by-text": "결정",
        "no-label-body": None, "long-label": None, "empty-body": None,
    }
    assert all(set(r) == {"id", "label"} for r in rows)
    captured = capsys.readouterr()
    assert MARKER not in out.getvalue() and MARKER not in captured.out + captured.err
    assert "지어낸" not in out.getvalue() + captured.out + captured.err
    assert all(method == "GET" for method, _, _ in transport.calls)       # 읽기 전용


def test_export_labels_aborts_when_no_body_field_is_present(migrate, capsys):
    transport = StoreTransport({"a": ["kind:fix"], "b": ["kind:fix"]},
                               bodies={"a": {"original_text": "[완료] ok"}, "b": {"summary": "[완료] %s" % MARKER}})
    out = io.StringIO()
    with pytest.raises(migrate.MigrationError) as excinfo:
        migrate.export_labels(transport, ["a", "b"], out)
    message = str(excinfo.value)
    assert "original_text/content/text" in message and "summary" in message     # 키 이름만 싣는다
    assert MARKER not in message + out.getvalue() + capsys.readouterr().err
    assert [json.loads(line)["id"] for line in out.getvalue().splitlines()] == ["a"]      # 조용히 넘기지 않고 거기서 멈춘다


@pytest.mark.parametrize("payload", [None, [], "text", {"original_text": None, "content": 3}],
                         ids=["none", "list", "string", "non-string-fields"])
def test_export_labels_aborts_on_unusable_detail_payloads(migrate, payload):
    class Odd:
        def request(self, method, path, body=None, authenticated=True):
            return 200, payload, json.dumps(payload)
    with pytest.raises(migrate.MigrationError, match="none of the body fields"):
        migrate.export_labels(Odd(), ["a"], io.StringIO())


def test_export_labels_rejects_a_missing_document(migrate):
    with pytest.raises(migrate.MigrationError, match="rejected"):
        migrate.export_labels(StoreTransport({}), ["ghost"], io.StringIO())


def test_export_labels_rejects_bad_id_lists(migrate):
    for bad in ({"ids": ["a"]}, "a", [1], [""]):
        with pytest.raises(migrate.MigrationError, match="list of non-empty strings"):
            migrate.export_labels(StoreTransport({}), bad, io.StringIO())


def test_http_transport_log_false_never_prints_a_response_body(migrate, capsys, monkeypatch):
    class Response:
        status = 200
        def read(self):
            return json.dumps({"original_text": "[완료] %s" % MARKER}).encode()
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False
    monkeypatch.setattr(migrate.urllib.request, "urlopen", lambda request, timeout=None: Response())
    quiet = migrate.HttpTransport("http://h", "[REDACTED]", log=False)
    assert quiet.request("GET", "/x")[1]["original_text"].startswith("[완료]")
    captured = capsys.readouterr()
    assert MARKER not in captured.out + captured.err
    migrate.HttpTransport("http://h", "[REDACTED]").request("GET", "/x")      # 대조: 기본 로그는 본문을 찍는다
    assert MARKER in capsys.readouterr().out


def test_export_labels_cli_uses_a_quiet_transport_and_the_injected_ids(migrate, capsys, monkeypatch):
    ids, bodies = body_docs()
    transport = StoreTransport({i: ["kind:fix"] for i in ids}, bodies=bodies)
    seen = {}
    def factory(base_url, token, log=True):
        seen["log"] = log
        return transport
    monkeypatch.setattr(migrate, "HttpTransport", factory)
    monkeypatch.setenv("HINDSIGHT_API_MCP_AUTH_TOKEN", "[REDACTED]")
    monkeypatch.setattr(migrate, "INJECTED_INPUT_JSON", json.dumps(ids))
    assert migrate.main(["--export-labels", "--ids-file", "-"]) == 0
    captured = capsys.readouterr()
    assert seen["log"] is False
    assert len(captured.out.splitlines()) == len(ids) and MARKER not in captured.out + captured.err


def test_export_labels_flag_rules(migrate, tmp_path, monkeypatch):
    monkeypatch.setenv("HINDSIGHT_API_MCP_AUTH_TOKEN", "[REDACTED]")
    with pytest.raises(migrate.MigrationError, match="requires --ids-file"):
        migrate.main(["--export-labels"])
    ids = write_json(tmp_path / "ids.json", ["a"])
    with pytest.raises(migrate.MigrationError, match="cannot be combined"):
        migrate.main(["--export-labels", "--ids-file", ids, "--apply-manifest", "m.json"])
    with pytest.raises(migrate.MigrationError, match="only for --export-labels"):
        migrate.main(["--dry-run", "--ids-file", ids])


# ============================ ④ 셸 분기 ============================

SHELL = SCRIPTS / "tag-migrate.sh"


def test_shell_manifest_branch_injects_no_policy_alias_prelude():
    text = SHELL.read_text(encoding="utf-8")
    start = text.index("# --- 호스트 파일 플래그 (issue #43")
    end = text.index("# 본 실행.")
    branch = text[start:end]
    for forbidden in ("ALIAS_MAP", "DELETE_TAGS", "REMOVE_TAGS", "tag_aliases", "bank-policies", "delete_tags", "remove_tags"):
        assert forbidden not in branch, forbidden
    assert "flag_direction" in branch and "trap finish EXIT" in branch      # 표 기반 단일 분기 + 스테이징·회수
    legacy = text[end:]
    assert "ALIAS_MAP = " in legacy and "DELETE_TAGS = set(" in legacy       # 기존 경로는 그대로 남아 있다
    assert 'if [ -z "$pair_flag" ]; then' in legacy                          # alias 는 manifest 모드(pair_flag)에서만 건너뛴다


def run_shell_with_stub_docker(tmp_path, args, policy=False, cwd=None):
    """저장소를 흉내 낸 임시 디렉터리에서 tag-migrate.sh 를 돌리되 docker 를 가짜로 대체한다.

    PATH 에는 가짜 docker 와 python3 링크, 기본 유틸(/usr/bin:/bin)만 둔다. 가짜 docker 는 받은 인자를
    기록하고 같은 stdin 을 이 파이썬으로 실행할 뿐이라 운영 컨테이너·뱅크에는 닿지 않는다.
    """
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "bin").mkdir()
    shutil.copy(SHELL, root / "scripts" / "tag-migrate.sh")
    shutil.copy(SCRIPTS / "hc-host-env.sh", root / "scripts" / "hc-host-env.sh")
    shutil.copy(SCRIPTS / "tag_migrate.py", root / "scripts" / "tag_migrate.py")
    (root / ".env.local").write_text("", encoding="utf-8")
    if policy:
        (root / "policies").mkdir()
        shutil.copy(POLICY_PATH, root / "policies" / "bank-policies.json")
    (root / "bin" / "python3").symlink_to(sys.executable)
    docker = root / "bin" / "docker"
    calls = root / "docker-calls"
    calls.mkdir()
    docker.write_text(
        "#!/bin/sh\n"
        'n=$(ls "' + str(calls) + '" | grep -c "args$")\n'
        'n=$((n + 1))\n'
        'printf "%s\\n" "$@" > "' + str(calls) + '/$n.args"\n'
        "shift 5\n"
        'cat > "' + str(calls) + '/$n.stdin"\n'
        'exec "' + sys.executable + '" "$@" < "' + str(calls) + '/$n.stdin"\n', encoding="utf-8")
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    env = {"PATH": "%s:/usr/bin:/bin" % (root / "bin"), "HOME": str(tmp_path), "HC_PERSONAL_BANK": "personal-demo"}
    done = subprocess.run(["/bin/bash", str(root / "scripts" / "tag-migrate.sh")] + args,
                          cwd=str(cwd or root), env=env, capture_output=True, text=True, timeout=60)
    # 호스트 파일 플래그를 쓰면 래퍼는 스테이징·본 실행·회수·정리로 exec 을 여러 번 부른다: 본 실행(tag_migrate.py 를 받는 호출)의 인자를 돌려준다.
    main_args = []
    for args_file in sorted(calls.glob("*.args"), key=lambda path: int(path.stem)):
        if "def main(" in args_file.with_suffix(".stdin").read_text(encoding="utf-8"):
            main_args = args_file.read_text(encoding="utf-8").splitlines()
    return done, main_args


def assert_container_temp_path(value, host_path):
    """컨테이너에는 호스트 경로가 아니라 래퍼가 스테이징한 컨테이너 임시 경로(tag-migrate-io-*)가 간다(issue #143)."""
    assert "tag-migrate-io-" in value and str(host_path) not in value and value != "-"


@pytest.mark.skipif(shutil.which("docker", path="/usr/bin:/bin") is not None, reason="a real docker is on the restricted PATH")
def test_shell_manifest_dry_run_injects_the_file_and_runs_without_policy(manifest, tmp_path):
    path = write_json(tmp_path / "manifest.json", manifest)
    done, docker_args = run_shell_with_stub_docker(tmp_path, ["--dry-run", "--apply-manifest", path])
    assert done.returncode == 0, done.stderr
    assert docker_args[:5] == ["compose", "exec", "-T", "hindsight", "python"]
    assert docker_args[5:8] == ["-", "--dry-run", "--apply-manifest"]
    assert_container_temp_path(docker_args[8], path)                            # 호스트 경로는 컨테이너로 넘기지 않는다
    assert "SUMMARY documents=%d unresolved=%d" % (len(manifest["documents"]), len(manifest["unresolved"])) in done.stdout
    assert done.stdout.rstrip().endswith("DRY-RUN complete; mutations=0")


@pytest.mark.skipif(shutil.which("docker", path="/usr/bin:/bin") is not None, reason="a real docker is on the restricted PATH")
def test_shell_export_labels_injects_ids_and_stops_without_a_token(tmp_path):
    ids = write_json(tmp_path / "ids.json", ["a", "b"])
    done, docker_args = run_shell_with_stub_docker(tmp_path, ["--export-labels", "--ids-file", ids])
    assert docker_args[5:8] == ["-", "--export-labels", "--ids-file"]
    assert_container_temp_path(docker_args[8], ids)
    assert done.returncode == 1 and "HINDSIGHT_API_MCP_AUTH_TOKEN is required" in done.stderr      # 네트워크 전에 멈춘다


@pytest.mark.skipif(shutil.which("docker", path="/usr/bin:/bin") is not None, reason="a real docker is on the restricted PATH")
def test_shell_legacy_path_still_injects_the_policy_prelude(tmp_path):
    done, docker_args = run_shell_with_stub_docker(tmp_path, ["--self-test"], policy=True)
    assert done.returncode == 0, done.stderr
    assert docker_args[5:] == ["-", "--self-test"] and "SELF-TEST PASS" in done.stdout


@pytest.mark.parametrize("args,message", [
    (["--apply-manifest"], "requires a FILE"),
    (["--apply-manifest", "a.json", "--ids-file", "b.json"], "only one of"),
])
@pytest.mark.skipif(shutil.which("docker", path="/usr/bin:/bin") is not None, reason="a real docker is on the restricted PATH")
def test_shell_rejects_bad_manifest_arguments(tmp_path, args, message):
    done, docker_args = run_shell_with_stub_docker(tmp_path, args)
    assert done.returncode == 2 and message in done.stderr and docker_args == []


@pytest.mark.skipif(shutil.which("docker", path="/usr/bin:/bin") is not None, reason="a real docker is on the restricted PATH")
def test_shell_reads_a_relative_manifest_path_from_the_callers_directory(manifest, tmp_path):
    # 스크립트는 리포 루트로 cd 한다. 상대 경로는 호출한 디렉터리 기준이어야 한다(리포 루트에는 같은 이름이 없다).
    work = tmp_path / "caller"
    work.mkdir()
    write_json(work / "manifest.json", manifest)
    done, docker_args = run_shell_with_stub_docker(tmp_path, ["--dry-run", "--apply-manifest", "manifest.json"], cwd=work)
    assert done.returncode == 0, done.stderr
    assert docker_args[5:8] == ["-", "--dry-run", "--apply-manifest"]
    assert_container_temp_path(docker_args[8], "manifest.json")
    assert "SUMMARY documents=%d" % len(manifest["documents"]) in done.stdout
    assert not (tmp_path / "repo" / "manifest.json").exists()


@pytest.mark.skipif(shutil.which("docker", path="/usr/bin:/bin") is not None, reason="a real docker is on the restricted PATH")
def test_shell_reads_a_relative_ids_path_from_the_callers_directory(tmp_path):
    work = tmp_path / "caller"
    (work / "sub").mkdir(parents=True)
    write_json(work / "sub" / "ids.json", ["a", "b"])
    done, docker_args = run_shell_with_stub_docker(tmp_path, ["--export-labels", "--ids-file", "sub/ids.json"], cwd=work)
    assert docker_args[5:8] == ["-", "--export-labels", "--ids-file"]
    assert_container_temp_path(docker_args[8], "ids.json")
    # 파일을 찾았다면 토큰 검사까지 간다(못 찾았다면 FileNotFoundError 로 다른 메시지가 난다)
    assert done.returncode == 1 and "HINDSIGHT_API_MCP_AUTH_TOKEN is required" in done.stderr


@pytest.mark.skipif(shutil.which("docker", path="/usr/bin:/bin") is not None, reason="a real docker is on the restricted PATH")
def test_shell_absolute_paths_are_unchanged(manifest, tmp_path):
    path = write_json(tmp_path / "abs-manifest.json", manifest)
    done, _ = run_shell_with_stub_docker(tmp_path, ["--dry-run", "--apply-manifest", path])
    assert done.returncode == 0, done.stderr


# ============================ 이슈 #109 항목 3: 3연속 실패 중단 전에 drain 1회 ============================
# run_live 와 apply_manifest_live 모두: 이미 성공한 PATCH 가 있으면 그 consolidation 을 기다린 뒤에 MigrationError 로 끝난다.
# (이전에는 raise 가 drain 을 건너뛰었다. 성공한 PATCH 가 없어도 이전 실행의 consolidation 을 기다리는 약속은 같다.)

ABORT_PATHS = ["run_live", "apply_manifest_live"]


def run_aborting(migrate, path, documents, manifest, tmp_path, successes, drain_fn):
    """successes 개의 문서는 PATCH 성공, 뒤따르는 3개는 422 로 연속 실패 -> 중단."""
    ordered = [e["id"] for e in manifest["documents"]]
    assert len(ordered) >= successes + 3
    failing = set(ordered[successes:successes + 3])
    transport = store_for(documents, fail_patch=failing)
    if path == "apply_manifest_live":
        call = lambda: migrate.apply_manifest_live(
            transport, manifest, str(tmp_path / "state.json"), drain_fn=drain_fn, request_sleeper=lambda s: None)
    else:
        changes = [{"id": e["id"], "old": e["old"], "new": e["new"], "action": "RETAG"} for e in manifest["documents"]]
        call = lambda: migrate.run_live(
            transport, changes, {"RETAG"}, migrate.manifest_for(changes, {"RETAG"}), str(tmp_path / "state.json"),
            drain_fn=drain_fn, delete_tags=set(), request_sleeper=lambda s: None)
    return transport, call


@pytest.mark.parametrize("path", ABORT_PATHS)
@pytest.mark.parametrize("successes", [0, 1, 2])
def test_three_consecutive_failures_drain_once_then_raise(migrate, manifest, documents, tmp_path, path, successes):
    drained = []
    transport, call = run_aborting(migrate, path, documents, manifest, tmp_path, successes,
                                   drain_fn=lambda t: drained.append(len(transport.patches)))
    with pytest.raises(migrate.MigrationError, match="3 consecutive failed documents"):
        call()
    assert drained == [successes + 3]                         # 중단 직전 한 번, 시도한 PATCH 전부 뒤에
    assert len(transport.patches) == successes + 3            # 임계값 3 은 그대로: 4번째 문서는 시도하지 않는다


@pytest.mark.parametrize("path", ABORT_PATHS)
def test_abort_still_raises_the_failure_when_the_drain_itself_fails(migrate, manifest, documents, tmp_path, path):
    def broken_drain(_transport):
        raise migrate.MigrationError("terminal drain timeout after 1 seconds")
    _, call = run_aborting(migrate, path, documents, manifest, tmp_path, 1, drain_fn=broken_drain)
    with pytest.raises(migrate.MigrationError, match="3 consecutive failed documents"):
        call()


@pytest.mark.parametrize("path", ABORT_PATHS)
@pytest.mark.parametrize("raised", [
    pytest.param(lambda: __import__("http.client").client.IncompleteRead(b"x"), id="IncompleteRead"),
    pytest.param(lambda: AttributeError("'str' object has no attribute 'get'"), id="AttributeError"),
])
def test_abort_reason_survives_a_non_migration_error_from_the_drain(migrate, manifest, documents, tmp_path,
                                                                    capsys, path, raised):
    """final-review C6: drain 이 MigrationError 가 아닌 예외를 내도 3연속 실패 MigrationError 가 올라간다(오류는 출력만)."""
    def broken_drain(_transport):
        raise raised()
    _, call = run_aborting(migrate, path, documents, manifest, tmp_path, 1, drain_fn=broken_drain)
    with pytest.raises(migrate.MigrationError, match="3 consecutive failed documents"):
        call()
    assert "DRAIN FAILED before abort" in capsys.readouterr().out


# --- 이슈 #128: argparse 약어 거부 ------------------------------------------------------------------
# 원인 판정: parse_args 가 allow_abbrev 기본값(True)이라 `--apply`->`--apply-manifest`, `--ids`->`--ids-file` 로 받았다.
# scripts/tag-migrate.sh 의 case 는 전체 이름과 `=` 형태만 호스트 파일 주입 경로로 보내므로, 약어는 주입을 우회해
# 컨테이너가 호스트 경로를 직접 열게 된다.
ABBREV_MATRIX = [
    pytest.param(["--dry-run", "--apply", "/x.json"], id="apply-space"),
    pytest.param(["--dry-run", "--apply=/x.json"], id="apply-equals"),
    pytest.param(["--export-labels", "--ids", "/x"], id="ids-space"),
    pytest.param(["--export-labels", "--ids=/x"], id="ids-equals"),
]
FULL_NAME_MATRIX = [
    pytest.param(["--dry-run", "--apply-manifest", "/x.json"], "apply_manifest", "/x.json", id="apply-manifest-space"),
    pytest.param(["--dry-run", "--apply-manifest=/x.json"], "apply_manifest", "/x.json", id="apply-manifest-equals"),
    pytest.param(["--export-labels", "--ids-file", "/x"], "ids_file", "/x", id="ids-file-space"),
    pytest.param(["--export-labels", "--ids-file=/x"], "ids_file", "/x", id="ids-file-equals"),
]


@pytest.mark.parametrize("argv", ABBREV_MATRIX)
def test_parse_args_rejects_abbreviated_flags(migrate, argv, capsys):
    with pytest.raises(SystemExit) as exc:
        migrate.parse_args(argv)
    assert exc.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


@pytest.mark.parametrize("argv,attr,value", FULL_NAME_MATRIX)
def test_parse_args_still_accepts_full_flag_names(migrate, argv, attr, value):
    assert getattr(migrate.parse_args(argv), attr) == value


def _proper_prefixes(flag):
    return [flag[:n] for n in range(3, len(flag))]          # `--a` 부터 전체 이름 직전까지


@pytest.mark.parametrize("prefix", _proper_prefixes("--apply-manifest") + _proper_prefixes("--ids-file"))
@pytest.mark.parametrize("form", ["space", "equals"])
def test_every_proper_prefix_of_the_injected_flags_is_rejected(migrate, prefix, form, capsys):
    """셸 case 가 주입 분기로 보내는 두 플래그의 모든 접두 약어(`--a`..`--apply-manifes`, `--i`..`--ids-fil`)를 막는다."""
    mode = "--export-labels" if prefix.startswith("--i") else "--dry-run"
    argv = [mode, prefix + "=/x"] if form == "equals" else [mode, prefix, "/x"]
    with pytest.raises(SystemExit) as exc:
        migrate.parse_args(argv)
    assert exc.value.code == 2
