"""정책·트리거 로컬 오버레이 (issue #187).

추적 파일(policies/<name>)에 gitignore 된 policies/local/<name> 을 로더가 병합한다.
  * 운영자 분리 동등성: 분리 전 두 파일(tests/fixtures/policies/sample/)을 scripts/policy-split.py 로
    나눈 뒤 병합 로드한 결과가 원본 로드 결과와 같다(정책 객체·트리거 행).
  * 로컬 행렬: 없음 / 뱅크 추가 / 뱅크 대체 / 깨진 로컬(기동 실패) / 트리거 id 중복(기동 실패).
  * 템플릿 policies/templates/personal-bank.json 을 __OWNER__=alice 로 렌더하면 받아들여지고
    user:alice 는 통과, user:demo 는 거부된다.
  * 핫 리로드: 추적·로컬 어느 쪽을 고쳐도 다음 요청(refreshed)에서 반영된다.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
from pathlib import Path

import pytest

from gateway import rules
from gateway.bank_policies import BankPolicies, BankPolicyError
from gateway.context_triggers import TriggerTable, TriggerTableError

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "tests" / "fixtures" / "policies" / "sample"
TEMPLATE = ROOT / "policies" / "templates" / "personal-bank.json"
POLICY, TRIGGERS = "bank-policies.json", "context-triggers.json"


def _split_module():
    spec = importlib.util.spec_from_file_location("policy_split_under_test", ROOT / "scripts" / "policy-split.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


split_tool = _split_module()


def _dump(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _row(**over):
    base = {"id": "t-one", "bank": "personal-alice", "tools": ["aside"],
            "all_of": [["zzzuniq"]], "recall": {"query": "오버레이 테스트용 고정 쿼리"}}
    base.update(over)
    return base


# --- 운영자 분리 동등성 ------------------------------------------------------------------------

def test_demo_split_then_merge_equals_the_original(tmp_path):
    src = tmp_path / "src"
    shutil.copytree(DEMO, src)
    out = tmp_path / "policies"
    assert split_tool.main(["--src", str(src), "--local-dir", str(out / "local"),
                            "--tracked-dir", str(out)]) == 0

    original = BankPolicies.from_file(str(src / POLICY))
    merged = BankPolicies.from_files(str(out / POLICY))           # local/ 는 경로에서 유도
    assert merged.bank_names() == original.bank_names()
    assert merged.defaults == original.defaults
    for name in original.bank_names():
        assert merged.for_bank(name) == original.for_bank(name), name
    assert split_tool.policies_equal(original, merged)

    original_rows = TriggerTable.from_file(str(src / TRIGGERS)).rows
    merged_rows = TriggerTable.from_files(str(out / TRIGGERS)).rows
    assert merged_rows == original_rows and [r.id for r in merged_rows] == ["demo-dash-login"]

    # 분리된 추적분은 리포에 커밋된 추적 파일과 바이트 단위로 같다(운영 pull 뒤 병합 == 원본의 전제)
    for name in (POLICY, TRIGGERS):
        assert (out / name).read_bytes() == (ROOT / "policies" / name).read_bytes(), name
    assert split_tool.main(["check", "--src", str(src), "--policies-dir", str(out)]) == 0
    shutil.rmtree(out / "local")
    assert split_tool.main(["check", "--src", str(src), "--policies-dir", str(out)]) == 1


def test_tracked_files_hold_only_demo_banks_and_local_is_gitignored():
    tracked = json.loads((ROOT / "policies" / POLICY).read_text(encoding="utf-8"))
    assert sorted(tracked["banks"]) == ["personal-alice", "team-eng"]
    assert json.loads((ROOT / "policies" / TRIGGERS).read_text(encoding="utf-8"))["triggers"] == []
    assert "policies/local/" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()


def test_split_refuses_to_overwrite_an_existing_local_file(tmp_path):
    local = tmp_path / "local"
    _dump(local / POLICY, {"banks": {}})
    assert split_tool.main(["--src", str(DEMO), "--local-dir", str(local)]) == 2
    assert json.loads((local / POLICY).read_text(encoding="utf-8")) == {"banks": {}}
    assert not (local / TRIGGERS).exists()                        # 하나라도 막히면 아무것도 쓰지 않는다


# --- 로컬 행렬 -----------------------------------------------------------------------------

TRACKED_POLICY = {"defaults": {"recall_max_tokens_floor": 4096},
                  "banks": {"personal-alice": {"tags_required": True, "tag_prefixes": ["topic"]}}}
TRACKED_TRIGGERS = {"version": 1, "triggers": [_row()]}


@pytest.mark.parametrize("case, local_policy, local_triggers, expect", [
    ("absent", None, None,
     {"banks": ["personal-alice"], "alice_required": True, "rows": ["t-one"]}),
    ("add-bank", {"banks": {"personal-carol": {"tags_required": True, "tag_prefixes": ["kind"]}}},
     {"version": 1, "triggers": [_row(id="t-carol", bank="personal-carol")]},
     {"banks": ["personal-alice", "personal-carol"], "alice_required": True, "rows": ["t-one", "t-carol"]}),
    ("replace-bank", {"banks": {"personal-alice": {"tags_required": False}}},
     {"version": 1, "triggers": []},
     {"banks": ["personal-alice"], "alice_required": False, "rows": ["t-one"]}),
    ("broken-policy-json", "{broken", None, BankPolicyError),
    ("broken-policy-schema", {"defaults": {"tags_required": True}}, None, BankPolicyError),
    ("broken-trigger-json", None, "{broken", TriggerTableError),
    ("duplicate-trigger-id", None, {"version": 1, "triggers": [_row(bank="personal-carol")]}, TriggerTableError),
])
def test_local_overlay_matrix(tmp_path, monkeypatch, case, local_policy, local_triggers, expect):
    monkeypatch.delenv("HC_BANK_POLICIES_LOCAL", raising=False)
    monkeypatch.delenv("HC_CONTEXT_TRIGGERS_LOCAL", raising=False)
    _dump(tmp_path / POLICY, TRACKED_POLICY)
    _dump(tmp_path / TRIGGERS, TRACKED_TRIGGERS)
    for name, data in ((POLICY, local_policy), (TRIGGERS, local_triggers)):
        if data is None:
            continue
        path = tmp_path / "local" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")

    def load():
        # server.py 와 같은 진입점(load). 깨진 쪽은 기동 실패 = 예외.
        return (BankPolicies.load(str(tmp_path / POLICY)), TriggerTable.load(str(tmp_path / TRIGGERS)))

    if isinstance(expect, type):
        with pytest.raises(expect):
            load()
        return
    policies, table = load()
    assert sorted(policies.bank_names()) == expect["banks"]
    assert policies.for_bank("personal-alice").tags_required is expect["alice_required"]
    assert policies.for_bank("personal-alice").recall_max_tokens_floor == 4096   # 추적 defaults 상속
    assert [r.id for r in table.rows] == expect["rows"]


def test_env_override_points_the_loaders_at_another_local_file(tmp_path, monkeypatch):
    _dump(tmp_path / POLICY, TRACKED_POLICY)
    _dump(tmp_path / TRIGGERS, TRACKED_TRIGGERS)
    _dump(tmp_path / "elsewhere" / POLICY, {"banks": {"personal-dave": {}}})
    _dump(tmp_path / "elsewhere" / TRIGGERS, {"version": 1, "triggers": [_row(id="t-dave")]})
    monkeypatch.setenv("HC_BANK_POLICIES_LOCAL", str(tmp_path / "elsewhere" / POLICY))
    monkeypatch.setenv("HC_CONTEXT_TRIGGERS_LOCAL", str(tmp_path / "elsewhere" / TRIGGERS))
    assert "personal-dave" in BankPolicies.load(str(tmp_path / POLICY)).bank_names()
    assert [r.id for r in TriggerTable.load(str(tmp_path / TRIGGERS)).rows] == ["t-one", "t-dave"]


# --- 템플릿 -------------------------------------------------------------------------------

def test_template_has_no_demo_values_left():
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "personal-demo" not in text and '"demo"' not in text and "user:demo" not in text
    assert "__OWNER__" in text


@pytest.mark.parametrize("tags, ok", [
    (["kind:decision", "user:alice"], True),
    (["kind:decision", "user:demo"], False),
    # item: 접두어 (issue #320) - 열린 접두어라 slug 문법만 본다
    (["kind:decision", "user:alice", "item:hindsight-crew-251"], True),
    (["kind:decision", "user:alice", "item:HC-251"], True),          # 대문자는 소문자로 고쳐진다(아래 별도 단언)
    (["kind:decision", "user:alice", "item:이슈"], False),            # ASCII slug 없음
    (["kind:decision", "user:alice", "item:"], False),               # 빈 slug
    (["kind:decision", "user:alice", "item:___"], False),            # 구두점뿐
])
def test_template_rendered_for_alice_loads_and_closes_user_vocabulary(tmp_path, tags, ok):
    shutil.copy(ROOT / "policies" / POLICY, tmp_path / POLICY)
    rendered = TEMPLATE.read_text(encoding="utf-8").replace("__OWNER__", "alice")
    (tmp_path / "local").mkdir()
    (tmp_path / "local" / POLICY).write_text(rendered, encoding="utf-8")
    policy = BankPolicies.load(str(tmp_path / POLICY)).for_bank("personal-alice")
    assert policy.tag_closed_prefixes == ("kind", "user") and policy.tag_vocabulary["user"] == ("alice",)
    assert "(personal-alice)" in policy.instructions_text
    verdict = rules.evaluate_tags(tags, policy)
    assert (not verdict.violations) is ok, verdict.violations


def _rendered_alice_policy(tmp_path):
    shutil.copy(ROOT / "policies" / POLICY, tmp_path / POLICY)
    (tmp_path / "local").mkdir(exist_ok=True)
    (tmp_path / "local" / POLICY).write_text(
        TEMPLATE.read_text(encoding="utf-8").replace("__OWNER__", "alice"), encoding="utf-8")
    return BankPolicies.load(str(tmp_path / POLICY)).for_bank("personal-alice")


def test_template_declares_item_prefix_open_and_repo_aliases(tmp_path):
    policy = _rendered_alice_policy(tmp_path)
    assert "item" in policy.tag_prefixes
    assert "item" not in policy.tag_closed_prefixes and "item" not in policy.tag_candidate_prefixes
    assert "item" not in policy.tag_vocabulary
    assert policy.item_repo_aliases == {"hc": "hindsight-crew", "hindsight-crew": "hindsight-crew"}
    template = json.loads(TEMPLATE.read_text(encoding="utf-8"))["banks"]["personal-__OWNER__"]
    assert template["item_repo_aliases"] == policy.item_repo_aliases


def test_template_item_tag_verdicts(tmp_path):
    policy = _rendered_alice_policy(tmp_path)
    ok = rules.evaluate_tags(["kind:completion", "user:alice", "item:hindsight-crew-251"], policy)
    assert not ok.violations and not ok.normalized_tags
    folded = rules.evaluate_tags(["kind:completion", "user:alice", "item:HC-251"], policy)
    assert not folded.violations and folded.normalized_tags == (("item:HC-251", "item:hc-251"),)
    for bad in ("item:이슈", "item:", "item:___"):
        assert rules.evaluate_tags(["kind:completion", "user:alice", bad], policy).violations, bad


def _alias_bank(aliases):
    return {"banks": {"personal-alice": {"tag_prefixes": ["project", "item"], "item_repo_aliases": aliases}}}


@pytest.mark.parametrize("case, aliases", [
    ("not-an-object-list", [["hc", "hindsight-crew"]]),
    ("not-an-object-string", "hc"),
    ("null", None),
    ("value-not-string", {"hc": 1}),
    ("value-null", {"hc": None}),
    ("value-uppercase", {"hc": "Hindsight-Crew"}),
    ("value-underscore", {"hc": "hindsight_crew"}),
    ("value-empty", {"hc": ""}),
    ("value-hangul", {"hc": "힌드사이트"}),
    ("key-trailing-newline", {"hc\n": "hindsight-crew"}),
    ("value-trailing-newline", {"hc": "hindsight-crew\n"}),
    ("key-uppercase", {"HC": "hindsight-crew"}),
    ("key-empty", {"": "hindsight-crew"}),
    ("key-with-space", {"h c": "hindsight-crew"}),
    ("key-with-slash", {"o/hc": "hindsight-crew"}),
])
def test_item_repo_aliases_malformed_is_rejected_at_load(tmp_path, case, aliases):
    _dump(tmp_path / POLICY, _alias_bank(aliases))
    # 경로(banks.<뱅크>.item_repo_aliases)가 메시지에 있어야 한다: 알 수 없는 키 거부와 구별된다
    with pytest.raises(BankPolicyError, match=r"banks\.personal-alice\.item_repo_aliases"):
        BankPolicies.load(str(tmp_path / POLICY))


def test_item_repo_aliases_load_inherit_and_replace_by_key_existence(tmp_path):
    _dump(tmp_path / POLICY, {
        "defaults": {"item_repo_aliases": {"hc": "hindsight-crew"}},
        "banks": {"personal-alice": {},
                  "personal-carol": {"item_repo_aliases": {"x": "other-repo"}},
                  "personal-dave": {"item_repo_aliases": {}}}})
    policies = BankPolicies.load(str(tmp_path / POLICY))
    assert policies.for_bank("personal-alice").item_repo_aliases == {"hc": "hindsight-crew"}      # 상속
    assert policies.for_bank("personal-carol").item_repo_aliases == {"x": "other-repo"}           # 교체
    assert policies.for_bank("personal-dave").item_repo_aliases == {}                            # 빈 표도 값
    assert policies.for_bank("unregistered").item_repo_aliases == {"hc": "hindsight-crew"}


def test_item_repo_aliases_work_without_item_in_tag_prefixes(tmp_path):
    """별칭 표는 태그 문법과 독립이다: item 접두어가 없는 뱅크도 로드된다(운영 오버레이 호환)."""
    _dump(tmp_path / POLICY, {"banks": {"personal-alice": {"tag_prefixes": ["kind"],
                                                           "item_repo_aliases": {"hc": "hindsight-crew"}}}})
    policy = BankPolicies.load(str(tmp_path / POLICY)).for_bank("personal-alice")
    assert policy.item_repo_aliases == {"hc": "hindsight-crew"}


# --- 핫 리로드 ----------------------------------------------------------------------------

def _bump(path, ns):
    os.utime(path, ns=(ns, ns))                                   # 같은 초 안의 저장도 다른 mtime


@pytest.mark.parametrize("edited", ["tracked", "local"])
def test_trigger_edit_in_either_file_is_seen_on_the_next_request(tmp_path, edited):
    _dump(tmp_path / TRIGGERS, TRACKED_TRIGGERS)
    _dump(tmp_path / "local" / TRIGGERS, {"version": 1, "triggers": [_row(id="t-local")]})
    table = TriggerTable.load(str(tmp_path / TRIGGERS))
    assert [r.id for r in table.rows] == ["t-one", "t-local"]
    assert table.refreshed() is table                             # 아무것도 안 바뀌면 같은 객체

    target = tmp_path / TRIGGERS if edited == "tracked" else tmp_path / "local" / TRIGGERS
    current = json.loads(target.read_text(encoding="utf-8"))
    current["triggers"].append(_row(id=f"t-new-{edited}"))
    _dump(target, current)
    _bump(target, 10)
    fresh = table.refreshed()
    assert f"t-new-{edited}" in [r.id for r in fresh.rows]

    target.write_text("{broken", encoding="utf-8")                # 깨진 편집은 직전 표를 유지한다
    _bump(target, 20)
    assert fresh.refreshed() is fresh and fresh.refreshed() is fresh


def test_local_trigger_file_created_or_removed_after_startup_is_picked_up(tmp_path):
    _dump(tmp_path / TRIGGERS, TRACKED_TRIGGERS)
    table = TriggerTable.load(str(tmp_path / TRIGGERS))
    assert [r.id for r in table.rows] == ["t-one"]
    local = tmp_path / "local" / TRIGGERS
    _dump(local, {"version": 1, "triggers": [_row(id="t-later")]})
    table = table.refreshed()
    assert [r.id for r in table.rows] == ["t-one", "t-later"]
    local.unlink()
    assert [r.id for r in table.refreshed().rows] == ["t-one"]


def test_duplicate_id_introduced_by_a_live_local_edit_keeps_the_last_good_table(tmp_path):
    _dump(tmp_path / TRIGGERS, TRACKED_TRIGGERS)
    table = TriggerTable.load(str(tmp_path / TRIGGERS))
    _dump(tmp_path / "local" / TRIGGERS, {"version": 1, "triggers": [_row()]})   # t-one 중복
    assert table.refreshed() is table and [r.id for r in table.rows] == ["t-one"]


# --- final-review 반영 (issue #187) --------------------------------------------------------

def test_app_keeps_a_loaded_empty_table_so_a_later_local_trigger_is_served(tmp_path):
    """추적 triggers [] + 기동 시 로컬 없음 -> 나중에 policies/local/ 에 추가 -> 다음 요청에서 반영.
    app.py 가 `trigger_table or empty()` 로 행 0개 표(len 0 = falsy)를 소스 없는 빈 표로 바꾸던 회귀."""
    import test_context_triggers as tct
    _dump(tmp_path / TRIGGERS, {"version": 1, "triggers": []})
    loaded = TriggerTable.load(str(tmp_path / TRIGGERS))
    assert len(loaded) == 0
    client, _ = tct.make(tct.Upstream(results=[tct.memory()]), tmp_path, loaded)
    assert tct.post(client, tct.TASK_TEXT).content == b""          # 아직 행 없음
    _dump(tmp_path / "local" / TRIGGERS, {"version": 1, "triggers": [tct.row()]})
    resp = tct.post(client, tct.TASK_TEXT)
    assert resp.status_code == 200 and resp.headers["x-hc-trigger"] == "demo-dash-login"


@pytest.mark.parametrize("loader", ["policies", "triggers"])
def test_explicit_local_path_that_is_missing_warns_and_keeps_running(tmp_path, monkeypatch, caplog, loader):
    _dump(tmp_path / POLICY, TRACKED_POLICY)
    _dump(tmp_path / TRIGGERS, TRACKED_TRIGGERS)
    env = "HC_BANK_POLICIES_LOCAL" if loader == "policies" else "HC_CONTEXT_TRIGGERS_LOCAL"
    monkeypatch.setenv(env, str(tmp_path / "typo" / "missing.json"))
    with caplog.at_level("WARNING"):
        if loader == "policies":
            assert BankPolicies.load(str(tmp_path / POLICY)).bank_names() == {"personal-alice"}
        else:
            assert [r.id for r in TriggerTable.load(str(tmp_path / TRIGGERS)).rows] == ["t-one"]
    assert "local overlay not found" in caplog.text and "missing.json" in caplog.text


def test_derived_local_path_that_is_missing_stays_quiet(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("HC_BANK_POLICIES_LOCAL", raising=False)
    monkeypatch.delenv("HC_CONTEXT_TRIGGERS_LOCAL", raising=False)
    _dump(tmp_path / POLICY, TRACKED_POLICY)
    _dump(tmp_path / TRIGGERS, TRACKED_TRIGGERS)
    with caplog.at_level("WARNING"):
        BankPolicies.load(str(tmp_path / POLICY))
        TriggerTable.load(str(tmp_path / TRIGGERS))
    assert "local overlay not found" not in caplog.text            # 로컬 없음은 정상 설치 상태


@pytest.mark.parametrize("local, expect_defaults_only", [(None, True), ({"banks": {"personal-carol": {}}}, False)])
def test_missing_tracked_policy_warning_reflects_the_overlay_result(tmp_path, monkeypatch, caplog, local,
                                                                   expect_defaults_only):
    monkeypatch.delenv("HC_BANK_POLICIES_LOCAL", raising=False)
    if local is not None:
        _dump(tmp_path / "local" / POLICY, local)
    with caplog.at_level("WARNING"):
        policies = BankPolicies.load(str(tmp_path / POLICY))      # 추적 파일 없음
    assert ("defaults-only" in caplog.text) is expect_defaults_only
    assert (policies.bank_names() == {"personal-carol"}) is (not expect_defaults_only)
    if not expect_defaults_only:
        assert "personal-carol" in caplog.text


def test_tracked_file_vanishing_between_stat_and_open_keeps_the_last_good_table(tmp_path, monkeypatch):
    import gateway.context_triggers as ct
    _dump(tmp_path / TRIGGERS, TRACKED_TRIGGERS)
    _dump(tmp_path / "local" / TRIGGERS, {"version": 1, "triggers": [_row(id="t-local")]})
    table = TriggerTable.load(str(tmp_path / TRIGGERS))
    _bump(tmp_path / TRIGGERS, 30)                                 # stat 은 바뀐 mtime 을 본다
    real = ct._read_table_file
    tracked = str(tmp_path / TRIGGERS)
    monkeypatch.setattr(ct, "_read_table_file", lambda p: (None, None) if p == tracked else real(p))
    assert table.refreshed() is table and [r.id for r in table.rows] == ["t-one", "t-local"]
    monkeypatch.setattr(ct, "_read_table_file", real)
    assert [r.id for r in table.refreshed().rows] == ["t-one", "t-local"]   # 다음 요청에서 재시도


def test_tag_hygiene_default_policy_reads_the_local_overlay(tmp_path, monkeypatch):
    """--policy 없이 돌면(예약 #44 리포트) 추적 + policies/local/ 병합으로 읽는다."""
    import io
    spec = importlib.util.spec_from_file_location("tag_hygiene_overlay_test", ROOT / "scripts" / "tag_hygiene.py")
    hygiene = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hygiene)
    monkeypatch.delenv("HC_BANK_POLICIES_LOCAL", raising=False)
    shutil.copy(ROOT / "policies" / POLICY, tmp_path / POLICY)
    demo = json.loads((DEMO / POLICY).read_text(encoding="utf-8"))["banks"]["personal-demo"]
    _dump(tmp_path / "local" / POLICY, {"banks": {"personal-demo": demo}})
    monkeypatch.setattr(hygiene, "default_policy_path", lambda: str(tmp_path / POLICY))
    docs = tmp_path / "docs.jsonl"
    docs.write_text(json.dumps({"id": "d1", "tags": ["kind:fix", "user:demofull"]}) + "\n", encoding="utf-8")
    args = hygiene.build_parser().parse_args(["report", str(docs), "--json", "--bank", "personal-demo"])
    out = io.StringIO()
    assert hygiene.run_report(args, out=out) == 0                  # personal-demo 는 로컬에만 있다
    (tmp_path / "local" / POLICY).unlink()
    with pytest.raises(hygiene.HygieneError, match="not declared"):
        hygiene.run_report(args, out=io.StringIO())
