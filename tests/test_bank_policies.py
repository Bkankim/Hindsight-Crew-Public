"""N1: bank policy loader — declarative per-bank rule profiles.

Loader/validator/drift-compare ONLY. Rule enforcement (autofill, tag grammar,
content prefix) is N2 and is deliberately absent here.

Key invariant under test: every merge/compare is KEY-EXISTENCE based, never
truthiness. upstream `/config` returns `retain_strategies` as a present key with a
`null` value (N0 실측, fixtures/rest/bank_config.json), so `if not cfg.get(k)`
would conflate "key absent" (contract drift) with "null" (no strategies).
"""
import json
import os
import re

import pytest

from conftest import call, load_rest_fixture
from gateway.bank_policies import (
    BankPolicies,
    BankPolicy,
    BankPolicyError,
    strategy_drift,
)

REPO_ROOT = os.path.dirname(os.path.dirname(__file__))
REPO_POLICY_FILE = os.path.join(REPO_ROOT, "tests", "fixtures", "policies", "sample", "bank-policies.json")


def _write(tmp_path, data):
    p = tmp_path / "bank-policies.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return str(p)


# --- ① 정상 파일 로드 + for_bank 폴백 ---------------------------------------

def test_repo_policy_file_loads_with_strict_profiles():
    pols = BankPolicies.from_file(REPO_POLICY_FILE)
    for bank in ("personal-demo", "personal-alice"):
        pol = pols.for_bank(bank)
        assert pol.tags_required is True
        assert pol.tag_prefixes == ("project", "kind", "topic", "user")
        assert pol.content_prefix_pattern == r"^\[[^\]]{1,12}\] "
        assert pol.allowed_strategies == ()       # [] = 어떤 전략명도 불허 (null 아님)
        assert pol.timestamp_autofill is True


def test_repo_policy_file_declares_tags_only_profile_for_the_team_bank():
    """옵션 B 절충(2026-07-26 운영자 승인): team-eng은 태그만 강제, content 접두는 개인 전용."""
    pol = BankPolicies.from_file(REPO_POLICY_FILE).for_bank("team-eng")
    assert pol.tags_required is True
    assert pol.tag_prefixes == ("project", "kind", "topic", "user")
    assert pol.content_prefix_pattern is None     # 개인 뱅크와 갈리는 유일한 지점
    assert pol.allowed_strategies == ()           # [] = 어떤 전략명도 불허 (null 아님)
    assert pol.timestamp_autofill is True


def test_unregistered_bank_falls_back_to_defaults():
    # personal-bob은 정책 파일 미등록 뱅크 (team-eng은 2026-07-26 옵션 B로 등록됨).
    pols = BankPolicies.from_file(REPO_POLICY_FILE)
    pol = pols.for_bank("personal-bob")
    assert pol.tags_required is False
    assert pol.tag_prefixes == ()
    assert pol.content_prefix_pattern is None
    assert pol.allowed_strategies is None         # null = 제한 없음 ([] 와 구분)
    assert pol.timestamp_autofill is True         # 전 뱅크 보편 이득
    assert pol is pols.for_bank("whatever-else")


def test_bank_entry_inherits_unspecified_keys_from_defaults(tmp_path):
    path = _write(tmp_path, {
        "defaults": {"tags_required": False, "allowed_strategies": None,
                     "timestamp_autofill": True},
        "banks": {"b1": {"tags_required": True}},
    })
    pol = BankPolicies.from_file(path).for_bank("b1")
    assert pol.tags_required is True
    assert pol.allowed_strategies is None
    assert pol.timestamp_autofill is True


def test_explicit_null_overrides_defaults_by_key_existence(tmp_path):
    """defaults=[] 인데 뱅크가 명시적 null → 제한 없음. truthy 병합이면 못 잡는 케이스."""
    path = _write(tmp_path, {
        "defaults": {"allowed_strategies": []},
        "banks": {"b1": {"allowed_strategies": None}},
    })
    pols = BankPolicies.from_file(path)
    assert pols.defaults.allowed_strategies == ()
    assert pols.for_bank("b1").allowed_strategies is None


# --- ② 스키마 위반 fail-fast --------------------------------------------------

def test_unknown_key_is_rejected(tmp_path):
    path = _write(tmp_path, {"defaults": {"tags_requird": True}})
    with pytest.raises(BankPolicyError) as ei:
        BankPolicies.from_file(path)
    assert "tags_requird" in str(ei.value)


def test_unknown_top_level_key_is_rejected(tmp_path):
    path = _write(tmp_path, {"default": {}})
    with pytest.raises(BankPolicyError):
        BankPolicies.from_file(path)


def test_wrong_type_is_rejected(tmp_path):
    path = _write(tmp_path, {"defaults": {"tags_required": "yes"}})
    with pytest.raises(BankPolicyError) as ei:
        BankPolicies.from_file(path)
    assert "tags_required" in str(ei.value)


def test_bad_tag_prefix_syntax_is_rejected(tmp_path):
    path = _write(tmp_path, {"defaults": {"tag_prefixes": ["project", "Kind"]}})
    with pytest.raises(BankPolicyError) as ei:
        BankPolicies.from_file(path)
    assert "Kind" in str(ei.value)


def test_uncompilable_content_prefix_pattern_is_rejected(tmp_path):
    path = _write(tmp_path, {"defaults": {"content_prefix_pattern": "^[unclosed"}})
    with pytest.raises(BankPolicyError) as ei:
        BankPolicies.from_file(path)
    assert "content_prefix_pattern" in str(ei.value)


def test_bad_bank_name_is_rejected(tmp_path):
    path = _write(tmp_path, {"banks": {"../etc": {}}})
    with pytest.raises(BankPolicyError):
        BankPolicies.from_file(path)


def test_broken_json_fails_fast(tmp_path):
    p = tmp_path / "bank-policies.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(BankPolicyError):
        BankPolicies.load(str(p))


def test_blocked_operations_defaults_to_empty_frozenset():
    assert BankPolicies.defaults_only().defaults.blocked_operations == frozenset()


def test_blocked_operations_are_parsed_as_a_frozenset():
    policy = BankPolicies.from_dict({
        "banks": {"x": {"blocked_operations": ["delete_bank", "clear_memories"]}}
    }).for_bank("x")
    assert policy.blocked_operations == frozenset({"delete_bank", "clear_memories"})


def test_blocked_operations_rejects_null():
    with pytest.raises(BankPolicyError):
        BankPolicies.from_dict({"banks": {"x": {"blocked_operations": None}}})


def test_blocked_operations_rejects_unknown_operation():
    with pytest.raises(BankPolicyError):
        BankPolicies.from_dict({
            "banks": {"x": {"blocked_operations": ["delete_document"]}}
        })


def test_blocked_operations_rejects_duplicates():
    with pytest.raises(BankPolicyError):
        BankPolicies.from_dict({
            "banks": {"x": {"blocked_operations": ["delete_bank", "delete_bank"]}}
        })


# --- ③ instructions_text UTF-8 2048바이트 캡 ---------------------------------

def test_instructions_text_2048_bytes_ok_2049_rejected(tmp_path):
    ok = "x" * 2048
    BankPolicies.from_file(_write(tmp_path, {"defaults": {"instructions_text": ok}}))
    too_long = "x" * 2049
    with pytest.raises(BankPolicyError) as ei:
        BankPolicies.from_file(_write(tmp_path, {"defaults": {"instructions_text": too_long}}))
    assert "2048" in str(ei.value)


def test_instructions_text_cap_counts_utf8_bytes_not_chars(tmp_path):
    multibyte = "가" * 683                       # 683 chars, 2049 bytes
    assert len(multibyte) < 2048 and len(multibyte.encode("utf-8")) == 2049
    with pytest.raises(BankPolicyError):
        BankPolicies.from_file(_write(tmp_path, {"defaults": {"instructions_text": multibyte}}))


# --- ④ 파일 부재 = defaults-only 기동 ----------------------------------------

def test_missing_file_yields_defaults_only(tmp_path, caplog):
    missing = str(tmp_path / "nope.json")
    pols = BankPolicies.load(missing)
    assert pols.for_bank("personal-demo") == BankPolicies.defaults_only().defaults
    assert pols.for_bank("personal-demo").tags_required is False
    assert "nope.json" in caplog.text


def test_defaults_only_constructor_registers_no_banks():
    pols = BankPolicies.defaults_only()
    assert pols.bank_names() == frozenset()
    assert pols.defaults.timestamp_autofill is True


# --- ⑤ strategy_drift: null / 부재 / 불일치 ----------------------------------

def test_strategy_drift_upstream_null_matches_empty_declaration():
    """upstream 실측 형상: 키 존재 + 값 null = 설정된 전략 없음 → []와 정합."""
    cfg = load_rest_fixture("bank_config")["response"]
    assert "retain_strategies" in cfg["config"] and cfg["config"]["retain_strategies"] is None
    assert strategy_drift(BankPolicy(allowed_strategies=()), cfg) == []
    assert strategy_drift(BankPolicy(allowed_strategies=None), cfg) == []
    drift = strategy_drift(BankPolicy(allowed_strategies=("deep",)), cfg)
    assert drift and "null" in drift[0] and "deep" in drift[0]


def test_strategy_drift_key_absent_is_contract_drift():
    drift = strategy_drift(BankPolicy(allowed_strategies=()), {"config": {}})
    assert len(drift) == 1
    assert "absent" in drift[0] and "retain_strategies" in drift[0]


def test_strategy_drift_mismatch_reports_both_directions():
    cfg = {"config": {"retain_strategies": {"summary": {}, "deep": {}}}}
    drift = strategy_drift(BankPolicy(allowed_strategies=("summary", "fast")), cfg)
    joined = " | ".join(drift)
    assert "fast" in joined            # 선언했으나 upstream에 없음
    assert "deep" in joined            # upstream에 있으나 미선언
    assert strategy_drift(BankPolicy(allowed_strategies=("summary", "deep")), cfg) == []
    assert strategy_drift(BankPolicy(allowed_strategies=None), cfg) == []


def test_strategy_drift_accepts_bare_config_dict_and_list_shape():
    assert strategy_drift(BankPolicy(allowed_strategies=("a",)),
                          {"retain_strategies": ["a"]}) == []
    drift = strategy_drift(BankPolicy(allowed_strategies=("a",)),
                           {"retain_strategies": "a"})
    assert drift and "unexpected type" in drift[0]


# --- ⑥ create_app DI 스모크 ---------------------------------------------------

def test_create_app_without_policies_is_defaults_only(make_app):
    app = make_app()
    assert app.state.bank_policies.bank_names() == frozenset()
    assert app.state.bank_policies.for_bank("personal-alice").tags_required is False


def test_injected_policies_do_not_change_existing_flow(make_app, recorder):
    from fastapi.testclient import TestClient
    pols = BankPolicies.from_file(REPO_POLICY_FILE)
    client = TestClient(make_app(bank_policies=pols))
    r = call(client, "personal-alice", "tok-alice", name="recall", arguments={"query": "q"})
    assert r.status_code == 200                                   # N1은 강제하지 않는다
    assert recorder.last["headers"]["x-bank-id"] == "personal-alice"
    assert recorder.last["body"]["params"]["arguments"] == {"query": "q"}
    assert client.app.state.bank_policies.for_bank("personal-alice").tags_required is True


# --- instructions_text 소비 (S3) ------------------------------------------------
# 정책 파일에 적어 둔 운영자 문구가 실제로 클라이언트가 받는 instructions 에 들어가는지.
# 파생 문장만 나가고 이 필드가 무시되면, 선언은 있는데 아무도 읽지 않는 죽은 설정이 된다.

def test_personal_demo_declares_operator_instructions():
    import os
    from gateway.bank_policies import BankPolicies

    repo_root = os.path.dirname(os.path.dirname(__file__))
    policies = BankPolicies.from_file(os.path.join(repo_root, "tests", "fixtures", "policies", "sample", "bank-policies.json"))
    policy = policies.for_bank("personal-demo")

    assert policy.instructions_text, "personal-demo 는 운영자 안내 문구를 선언해야 한다"
    assert "[" in policy.instructions_text          # 콘텐츠 라벨 관례를 담는다


def test_operator_instructions_are_appended_after_the_derived_rules():
    import os
    from gateway import rules
    from gateway.bank_policies import BankPolicies

    repo_root = os.path.dirname(os.path.dirname(__file__))
    policies = BankPolicies.from_file(os.path.join(repo_root, "tests", "fixtures", "policies", "sample", "bank-policies.json"))
    policy = policies.for_bank("personal-demo")

    text = rules.instructions_for(policy)
    assert text is not None
    assert "enforces the following retain rules" in text        # 파생 문장이 먼저
    assert policy.instructions_text in text                     # 운영자 문구가 그 뒤에
    assert text.index("enforces the following") < text.index(policy.instructions_text)
    assert len(text.encode("utf-8")) <= 2048 * 2                # 핸드셰이크에 실릴 크기


# --- ⑦ tag_aliases: 폐기 태그 → 정규 태그 선언 (SSoT는 정책 파일) -----------------
# 별칭 맵은 "정규화 이후 형태"를 키로 쓴다: 게이트웨이가 접두 없는 태그를 topic:<slug>
# 으로 먼저 정규화하므로(rules._fallback_prefix), 레거시 bare `aside` 는 topic:aside 로
# 도착한다. 그래서 키에 bare 형태가 아니라 topic:aside 가 들어간다.

EXPECTED_PERSONAL_DEMO_TAG_ALIASES = {
    # kind 축 병합
    "kind:done": "kind:completion",
    "kind:task-done": "kind:completion",
    "kind:task-completion": "kind:completion",
    "kind:work-log": "kind:completion",
    "kind:work": "kind:completion",
    "kind:cleanup": "kind:completion",
    "kind:lesson": "kind:gotcha",
    "kind:bug-finding": "kind:gotcha",
    "kind:policy": "kind:decision",
    "kind:plan": "kind:decision",
    "kind:todo": "kind:decision",
    "kind:solution": "kind:fix",
    "kind:recovery": "kind:fix",
    "kind:investigation": "kind:research",
    "kind:config": "kind:setting",
    "kind:setup": "kind:setting",
    "kind:test": "kind:verification",
    "kind:audit": "kind:verification",
    "kind:finding": "kind:discovery",
    "kind:infra": "kind:ops",
    "kind:playbook": "kind:rule",
    # 축 교정: topic 으로 잘못 들어간 kind 값
    "topic:policy": "kind:decision",
    "topic:config": "kind:setting",
    "topic:setup": "kind:setting",
    "topic:verify": "kind:verification",
    "topic:verification": "kind:verification",
    "topic:runtime-verification": "kind:verification",
    "topic:handoff": "kind:handoff",
    "topic:infra": "kind:ops",
    "topic:playbook": "kind:rule",
    # 축 교정 + 병합: project 축
    "project:quince": "project:hickory-bell",
    "project:linden": "project:hickory-bell",
    "topic:linden": "project:hickory-bell",
    "topic:quince": "project:hickory-bell",
    "topic:quince-linden-update": "project:hickory-bell",
    "topic:quince-custom": "project:holly-spire",
    "project:hindsight": "project:hemlock-loom",
    "topic:hindsight": "project:hemlock-loom",
    "topic:hindsight-crew": "project:hemlock-loom",
    "project:hindsight-token": "project:hemlock-loom",
    "topic:kapok": "project:yew",
    "topic:jarrah": "project:sumac",
    "topic:tansy-reed": "project:aspen-crate",
    "topic:orca": "project:teak-comet",
    "topic:aside": "project:cedar-dial",
    "topic:hazel": "project:walnut-moss",
    "project:sorrel-ssot": "project:willow",
    "topic:sorrel": "project:willow",
    "topic:workspace": "topic:ssot",
    "project:mesquite-ridge": "project:maple-bridge",
    "topic:claude-code": "project:maple-bridge",
    "topic:claude-md": "project:maple-bridge",
    "topic:claude-code-mcp": "project:maple-bridge",
    # topic 축 내부 병합
    "topic:gateway": "topic:mcp-gateway",
    "topic:hindsight-gateway": "topic:mcp-gateway",
    "topic:agent-memory": "topic:memory-vault",
    "topic:personal-demo": "topic:memory-vault",
    "topic:routing-config": "topic:model-routing",
    "topic:agent-routing": "topic:model-routing",
    "topic:ultrawork-cadence": "topic:ultrawork",
    "topic:ultrawork-protocol": "topic:ultrawork",
    "topic:ulw-goal": "topic:ultrawork",
    "topic:auth-path": "topic:auth",
    "topic:cloudflare": "topic:cloudflare-security",
    "topic:privacy": "topic:privacy-exposure",
    "topic:grok-4-6": "topic:xai",
    "topic:model-effort": "topic:effort",
    "topic:claude-sdk-timeout": "topic:claude-sdk",
    # 쓰는 시점 분열 이름 alias (#40)
    "project:wattle-cove-2026": "project:rowan-tide",
    "project:banksia-loft": "project:elm-gazette",
    "project:myall-shore": "project:larch-yard",
    "project:birch": "project:fir-easel",
    "project:juniper": "project:poplar-kiln",
    "project:tupelo": "project:alder-thistle",
    "topic:deployment": "topic:deploy",
    "topic:humanizer": "topic:humanize",
    "topic:git-commit-lore": "topic:commit-lore",
    "topic:dashboard-design": "topic:dashboard",
    "topic:headless-claude": "topic:headless",
    # user 축 (#39): 사람은 demo 하나. user:kauri-gate 은 일부러 없다(쓰는 시점에 거부)
    "user:demoalt": "user:demo",
    "user:demofull": "user:demo",
}


def test_personal_demo_declares_the_canonical_tag_alias_map():
    """맵 자체가 계약이다 - 개수만 세면 오타 한 글자가 그대로 통과한다."""
    pol = BankPolicies.from_file(REPO_POLICY_FILE).for_bank("personal-demo")
    assert pol.tag_aliases == EXPECTED_PERSONAL_DEMO_TAG_ALIASES
    assert len(pol.tag_aliases) == 81


def test_personal_demo_alias_map_is_non_transitive_and_grammar_legal():
    """정규값은 다시 별칭 키가 되지 않는다 (요청 시점 1홉 보장의 근거)."""
    pol = BankPolicies.from_file(REPO_POLICY_FILE).for_bank("personal-demo")
    assert set(pol.tag_aliases.values()).isdisjoint(pol.tag_aliases)
    legal = re.compile(r"^(?:" + "|".join(pol.tag_prefixes) + r"):[a-z0-9-]+$")
    for key, value in pol.tag_aliases.items():
        assert legal.match(key), key
        assert legal.match(value), value


def test_bank_without_tag_aliases_yields_an_empty_map():
    """선언하지 않은 뱅크는 빈 맵 - 별칭 기능 도입 전과 동작이 같아야 한다."""
    pols = BankPolicies.from_file(REPO_POLICY_FILE)
    assert pols.for_bank("team-eng").tag_aliases == {}
    assert pols.for_bank("personal-alice").tag_aliases == {}
    assert pols.for_bank("personal-bob").tag_aliases == {}      # 미등록 뱅크 = defaults
    assert BankPolicies.defaults_only().defaults.tag_aliases == {}


def test_tag_aliases_default_is_not_shared_between_policies():
    """field(default_factory=dict) 확인 - 인스턴스 간 기본값 공유가 없어야 한다."""
    a, b = BankPolicy(), BankPolicy()
    assert a.tag_aliases == {} and a.tag_aliases is not b.tag_aliases


def test_valid_tag_aliases_load_for_any_bank():
    pol = BankPolicies.from_dict({
        "banks": {"b1": {"tag_prefixes": ["kind", "topic"],
                         "tag_aliases": {"kind:done": "kind:completion",
                                         "topic:gateway": "topic:mcp-gateway"}}}
    }).for_bank("b1")
    assert pol.tag_aliases == {"kind:done": "kind:completion",
                               "topic:gateway": "topic:mcp-gateway"}


# from_dict 경유: JSON 은 비문자열 키를 표현할 수 없어 파일로는 그 케이스를 못 만든다.
# 두 경로 모두 from_dict 검증기를 지나므로 검증 지점은 동일하다.
@pytest.mark.parametrize("aliases, needle", [
    (["kind:done"], "expected an object"),                            # 맵이 아님
    ({1: "kind:completion"}, "alias key"),                            # 키가 문자열이 아님
    ({"kind:done": ["kind:completion"]}, "alias value"),              # 값이 문자열이 아님
    ({"kind:Done": "kind:completion"}, "kind:Done"),                  # 키 문법 위반(대문자)
    ({"nope:done": "kind:completion"}, "nope:done"),                  # 키 접두 미선언
    ({"kind:done": "kind:completion!"}, "kind:completion!"),          # 값 문법 위반
    ({"kind:done": "kind:"}, "kind:"),                                # 값 슬러그 비어 있음
    ({"kind:done": "kind:completion",
      "kind:completion": "kind:fact"}, "transitive"),                 # 값이 다시 키
])
def test_malformed_tag_aliases_fail_at_load(aliases, needle):
    with pytest.raises(BankPolicyError) as ei:
        BankPolicies.from_dict({
            "banks": {"b1": {"tag_prefixes": ["kind", "topic"], "tag_aliases": aliases}}
        })
    assert needle in str(ei.value)


def test_tag_aliases_without_declared_prefixes_is_rejected():
    """접두 선언이 없으면 문법 자체가 성립하지 않는다 - 조용히 통과시키지 않는다."""
    with pytest.raises(BankPolicyError) as ei:
        BankPolicies.from_dict({"banks": {"b1": {"tag_aliases": {"kind:done": "kind:completion"}}}})
    assert "tag_prefixes" in str(ei.value)


def test_tag_aliases_grammar_is_rechecked_against_the_banks_own_prefixes():
    """defaults 에서 물려받은 맵도 뱅크의 좁아진 접두 기준으로 다시 검사한다."""
    with pytest.raises(BankPolicyError) as ei:
        BankPolicies.from_dict({
            "defaults": {"tag_prefixes": ["kind", "topic"],
                         "tag_aliases": {"topic:gateway": "topic:mcp-gateway"}},
            "banks": {"b1": {"tag_prefixes": ["kind"]}},
        })
    assert "banks.b1" in str(ei.value) and "topic:gateway" in str(ei.value)


def test_tag_slug_is_identical_between_bank_policies_and_rules():
    """_TAG_SLUG 는 임포트 순환 회피용 의도적 복제(bank_policies.py 주석 참조).
    두 사본이 갈라지면 별칭 맵 검증과 retain 태그 문법 검사가 서로 다른 태그를
    합법으로 판정하므로, 동일성을 여기서 핀으로 고정한다."""
    from gateway import bank_policies, rules
    assert bank_policies._TAG_SLUG == rules._TAG_SLUG


# --- ⑧ recall_max_tokens_floor: 읽기 예산 하한 선언 ------------------------------
# 정책 파일이 없으면 BankPolicy() 로 폴백하므로, 코드 기본값 자체가 보호적이어야 한다
# (0 이었다면 폴백이 곧 가드 해제였을 것).

def test_code_default_floor_is_protective_not_zero():
    assert BankPolicy().recall_max_tokens_floor == 4096
    assert BankPolicies.defaults_only().defaults.recall_max_tokens_floor == 4096


def test_repo_policy_file_declares_the_floor_and_the_probe_bank_opt_out():
    pols = BankPolicies.from_file(REPO_POLICY_FILE)
    assert pols.defaults.recall_max_tokens_floor == 4096
    assert pols.for_bank("personal-demo").recall_max_tokens_floor == 4096
    assert pols.for_bank("personal-alice").recall_max_tokens_floor == 0   # 실험용 뱅크
    assert pols.for_bank("team-eng").recall_max_tokens_floor == 4096      # 상속
    assert pols.for_bank("personal-bob").recall_max_tokens_floor == 4096  # 미등록 폴백


@pytest.mark.parametrize("value", [0, 4096, 1])
def test_floor_accepts_non_negative_ints(tmp_path, value):
    pols = BankPolicies.from_file(_write(tmp_path, {
        "banks": {"b1": {"recall_max_tokens_floor": value}}}))
    assert pols.for_bank("b1").recall_max_tokens_floor == value


@pytest.mark.parametrize("value", [-1, True, False, "4096", 4096.5, None, [4096]])
def test_floor_rejects_non_int_and_negative(tmp_path, value):
    with pytest.raises(BankPolicyError) as ei:
        BankPolicies.from_file(_write(tmp_path, {
            "banks": {"b1": {"recall_max_tokens_floor": value}}}))
    assert "banks.b1.recall_max_tokens_floor" in str(ei.value)


def test_floor_is_declared_in_defaults_and_inherited_by_key_existence(tmp_path):
    pols = BankPolicies.from_file(_write(tmp_path, {
        "defaults": {"recall_max_tokens_floor": 2048},
        "banks": {"inherits": {}, "opts-out": {"recall_max_tokens_floor": 0}}}))
    assert pols.for_bank("inherits").recall_max_tokens_floor == 2048
    assert pols.for_bank("opts-out").recall_max_tokens_floor == 0


# --- ⑧ tag_vocabulary: 태그 어휘 선언 (#38, 분리를 켜는 게이트) ----------------------

def test_personal_demo_declares_the_seed_tag_vocabulary():
    vocab = BankPolicies.from_file(REPO_POLICY_FILE).for_bank("personal-demo").tag_vocabulary
    assert set(vocab) == {"kind", "project", "user"}
    assert len(vocab["kind"]) == 26 and len(vocab["project"]) == 44
    assert "gotcha" in vocab["kind"] and "completion" in vocab["kind"]
    assert "larch-yard" in vocab["project"] and "hickory-bell" in vocab["project"]


def test_personal_demo_vocabulary_has_no_prefix_in_values_and_no_alias_key():
    pol = BankPolicies.from_file(REPO_POLICY_FILE).for_bank("personal-demo")
    for prefix, slugs in pol.tag_vocabulary.items():
        for slug in slugs:
            assert ":" not in slug
            assert f"{prefix}:{slug}" not in pol.tag_aliases


def test_banks_without_a_vocabulary_yield_an_empty_map():
    pols = BankPolicies.from_file(REPO_POLICY_FILE)
    for bank in ("personal-alice", "team-eng", "project-demo-gm", "team-demo", "personal-bob"):
        assert pols.for_bank(bank).tag_vocabulary == {}
    assert BankPolicies.defaults_only().defaults.tag_vocabulary == {}


def test_tag_vocabulary_default_is_not_shared_between_policies():
    a, b = BankPolicy(), BankPolicy()
    assert a.tag_vocabulary == {} and a.tag_vocabulary is not b.tag_vocabulary


def test_valid_tag_vocabulary_loads_as_tuples():
    pol = BankPolicies.from_dict({
        "banks": {"b1": {"tag_prefixes": ["kind", "project", "topic"],
                         "tag_vocabulary": {"kind": ["fix", "gotcha"], "project": ["aside"]}}}
    }).for_bank("b1")
    assert pol.tag_vocabulary == {"kind": ("fix", "gotcha"), "project": ("aside",)}


# from_dict 경유: 비문자열 키는 JSON 으로 못 만든다(위 alias 테스트와 같은 이유).
@pytest.mark.parametrize("vocab, needle", [
    (["fix"], "expected an object"),                               # 맵이 아님
    ({1: ["fix"]}, "vocabulary prefix"),                           # 접두가 문자열이 아님
    ({"Kind": ["fix"]}, "vocabulary prefix"),                      # 접두 문법 위반
    ({"kind": "fix"}, "non-empty array"),                          # 값이 배열이 아님
    ({"kind": []}, "non-empty array"),                             # 빈 어휘는 오타로 본다
    ({"kind": [1]}, "not a valid slug"),                           # 슬러그가 문자열이 아님
    ({"kind": ["Fix"]}, "not a valid slug"),                       # 대문자
    ({"kind": ["kind:fix"]}, "not a valid slug"),                  # prefix 를 붙여 씀
    ({"kind": ["fix now"]}, "not a valid slug"),                   # 공백
    ({"kind": [""]}, "not a valid slug"),                          # 빈 슬러그
    ({"kind": ["fix", "fix"]}, "duplicate slug"),                  # 중복
    ({"nope": ["fix"]}, "not one of this bank's tag_prefixes"),    # 선언되지 않은 접두
    ({"kind": ["done"]}, "alias"),                                 # alias 키와 겹침(kind:done)
])
def test_malformed_tag_vocabulary_fails_at_load(vocab, needle):
    with pytest.raises(BankPolicyError) as ei:
        BankPolicies.from_dict({
            "banks": {"b1": {"tag_prefixes": ["kind", "topic"],
                             "tag_aliases": {"kind:done": "kind:completion"},
                             "tag_vocabulary": vocab}}
        })
    assert needle in str(ei.value)
    assert "banks.b1.tag_vocabulary" in str(ei.value)


def test_tag_vocabulary_without_declared_prefixes_is_rejected():
    with pytest.raises(BankPolicyError) as ei:
        BankPolicies.from_dict({"banks": {"b1": {"tag_vocabulary": {"kind": ["fix"]}}}})
    assert "tag_prefixes" in str(ei.value)


def test_tag_vocabulary_is_rechecked_against_the_banks_own_prefixes():
    """defaults 가 어휘를 주고 뱅크가 접두를 좁히면 뱅크 단위로 다시 거부한다."""
    with pytest.raises(BankPolicyError) as ei:
        BankPolicies.from_dict({
            "defaults": {"tag_prefixes": ["kind", "project"],
                         "tag_vocabulary": {"project": ["aside"]}},
            "banks": {"narrow": {"tag_prefixes": ["kind"]}},
        })
    assert "narrow" in str(ei.value) and "project" in str(ei.value)


def test_tag_vocabulary_is_rechecked_against_inherited_aliases():
    with pytest.raises(BankPolicyError) as ei:
        BankPolicies.from_dict({
            "defaults": {"tag_prefixes": ["kind"], "tag_aliases": {"kind:done": "kind:fix"}},
            "banks": {"b1": {"tag_vocabulary": {"kind": ["done"]}}},
        })
    assert "alias" in str(ei.value)


# --- ⑨ tag_closed_prefixes / tag_required_prefixes: 닫힌 목록·필수 선언 (#39) ----------

_CLOSED_BASE = {"tag_prefixes": ["kind", "user", "topic"],
                "tag_vocabulary": {"kind": ["fix", "gotcha"], "user": ["demo"]}}


def _closed_bank(**overrides):
    return BankPolicies.from_dict({"banks": {"b1": dict(_CLOSED_BASE, **overrides)}})


def test_personal_demo_declares_kind_and_user_closed_and_kind_required():
    pol = BankPolicies.from_file(REPO_POLICY_FILE).for_bank("personal-demo")
    assert pol.tag_closed_prefixes == ("kind", "user")
    assert pol.tag_required_prefixes == ("kind",)
    assert pol.tag_vocabulary["user"] == ("demo",)


def test_personal_demo_user_kauri_gate_is_never_an_alias():
    pol = BankPolicies.from_file(REPO_POLICY_FILE).for_bank("personal-demo")
    assert not [k for k in pol.tag_aliases if k.startswith("user:") and k not in
                ("user:demoalt", "user:demofull")]
    assert not [v for v in pol.tag_aliases.values() if "kauri-gate" in v]


def test_valid_closed_and_required_declarations_load_as_tuples():
    pol = _closed_bank(tag_closed_prefixes=["kind", "user"],
                       tag_required_prefixes=["kind"]).for_bank("b1")
    assert pol.tag_closed_prefixes == ("kind", "user")
    assert pol.tag_required_prefixes == ("kind",)


def test_banks_that_declare_nothing_get_empty_closed_and_required():
    pol = BankPolicies.defaults_only().defaults
    assert pol.tag_closed_prefixes == () and pol.tag_required_prefixes == ()


@pytest.mark.parametrize("overrides, needle", [
    ({"tag_closed_prefixes": "kind"}, "expected array"),
    ({"tag_required_prefixes": ["Kind"]}, "not a valid tag prefix"),
    ({"tag_closed_prefixes": ["kind", "kind"]}, "duplicate"),
    ({"tag_closed_prefixes": ["project"]}, "not among"),                  # 선언한 prefix가 아님
    ({"tag_required_prefixes": ["project"]}, "not among"),
    ({"tag_closed_prefixes": ["topic"]}, "declares no list"),             # 닫을 목록이 없음
    ({"tag_vocabulary": {"kind": ["fix", "gotcha"], "user": ["fix"]},
      "tag_closed_prefixes": ["kind", "user"],
      "tag_required_prefixes": ["kind", "user"]}, "both"),                # 해석 대상 목록 간 slug 겹침
    ({"tag_aliases": {"kind:done": "kind:completion"},
      "tag_closed_prefixes": ["kind"]}, "outside the closed"),            # alias가 목록 밖으로
])
def test_malformed_closed_or_required_declaration_fails_at_load(overrides, needle):
    with pytest.raises(BankPolicyError) as ei:
        _closed_bank(**overrides)
    assert needle in str(ei.value)
    assert "b1" in str(ei.value)


def test_closed_lists_that_never_resolve_a_bare_word_may_share_slugs():
    """필수가 아닌 닫힌 목록(user)은 해석 대상이 아니므로 kind 와 slug 를 공유해도 기동한다."""
    pol = _closed_bank(tag_vocabulary={"kind": ["fix", "gotcha"], "user": ["fix"]},
                       tag_closed_prefixes=["kind", "user"],
                       tag_required_prefixes=["kind"]).for_bank("b1")
    assert pol.tag_vocabulary["user"] == ("fix",)


def test_closed_prefixes_without_declared_prefixes_are_rejected():
    with pytest.raises(BankPolicyError) as ei:
        BankPolicies.from_dict({"banks": {"b1": {"tag_required_prefixes": ["kind"]}}})
    assert "tag_prefixes" in str(ei.value)


def test_closed_prefixes_are_rechecked_against_the_banks_own_vocabulary():
    """defaults 가 닫힘을 주고 뱅크가 어휘를 바꾸면 뱅크 단위로 다시 거부한다."""
    with pytest.raises(BankPolicyError) as ei:
        BankPolicies.from_dict({
            "defaults": dict(_CLOSED_BASE, tag_closed_prefixes=["kind"]),
            "banks": {"b1": {"tag_vocabulary": {"user": ["demo"]}}},
        })
    assert "b1" in str(ei.value) and "kind" in str(ei.value)


# --- ⑩ 닫힌 필수 prefix 의 맨단어 소유: alias 적용 뒤에도 모호하면 로드에서 거부 (#107-7) --------
# 맨단어 x 는 rules._resolve_closed 가 선언 순서대로 `alias(<prefix>:x)`를 시도해 처음 닫힌 목록에 들어가는
# 후보로 해석한다. 로더는 alias 적용 전 slug 만 겹치는지 봤기 때문에 a:[x], b:[y], alias b:x -> b:y 는
# 통과했고, 그러면 선언 순서에 따라 x 가 a:x 가 되기도 b:y 가 되기도 했다.

_AMBIGUOUS_VOCAB = {"a": ["x"], "b": ["y"]}


def _owner_bank(order, aliases=None, vocab=None):
    vocab = vocab or _AMBIGUOUS_VOCAB
    return BankPolicies.from_dict({"banks": {"b1": {
        "tag_prefixes": ["a", "b", "c", "topic"],
        "tag_vocabulary": {p: vocab[p] for p in order},
        "tag_closed_prefixes": list(order),
        "tag_required_prefixes": list(order),
        **({"tag_aliases": aliases} if aliases else {}),
    }}})


@pytest.mark.parametrize("order", [("a", "b"), ("b", "a")], ids=lambda o: "-".join(o))
def test_alias_created_ambiguity_is_rejected_whatever_the_declaration_order(order):
    with pytest.raises(BankPolicyError) as ei:
        _owner_bank(order, aliases={"b:x": "b:y"})
    msg = str(ei.value)
    assert "b1" in msg and "'x'" in msg and "both" in msg
    assert "a:x" in msg and "b:y" in msg                  # 어느 후보로 갈라지는지 알려 준다


@pytest.mark.parametrize("order", [("a", "b"), ("b", "a")], ids=lambda o: "-".join(o))
def test_issue_repro_without_the_alias_the_same_declaration_loads(order):
    pol = _owner_bank(order).for_bank("b1")
    assert pol.tag_vocabulary == {p: tuple(_AMBIGUOUS_VOCAB[p]) for p in order}


def test_the_defect_the_load_check_prevents_is_real_in_the_resolver():
    """RED 재현: 로더를 거치지 않고 만든 같은 선언은 선언 순서에 따라 x 를 다르게 해석한다."""
    from dataclasses import replace
    from gateway import rules
    base = BankPolicy(tag_prefixes=("a", "b", "topic"),
                      tag_vocabulary={"a": ("x",), "b": ("y",)},
                      tag_aliases={"b:x": "b:y"},
                      tag_closed_prefixes=("a", "b"), tag_required_prefixes=("a", "b"))
    reverse = replace(base, tag_closed_prefixes=("b", "a"), tag_required_prefixes=("b", "a"))

    assert rules._resolve_closed("topic:x", base) == "a:x"
    assert rules._resolve_closed("topic:x", reverse) == "b:y"


def test_an_alias_only_one_prefix_can_reach_is_not_ambiguous():
    """a:z -> b:y: 맨단어 z 는 a 를 거쳐서만 b:y 로 간다(b:z 는 b 목록에 없다). 후보가 하나라 모호하지 않다."""
    pol = _owner_bank(("a", "b"), aliases={"a:z": "b:y"}).for_bank("b1")
    assert pol.tag_aliases == {"a:z": "b:y"}


def test_two_prefixes_resolving_a_word_to_the_same_value_are_not_ambiguous():
    """순서와 무관하게 같은 값으로 가면 모호하지 않다: a:z, b:z 둘 다 c:w 로 간다."""
    pol = BankPolicies.from_dict({"banks": {"b1": {
        "tag_prefixes": ["a", "b", "c", "topic"],
        "tag_vocabulary": {"a": ["p"], "b": ["q"], "c": ["w"]},
        "tag_closed_prefixes": ["a", "b", "c"],
        "tag_required_prefixes": ["a", "b", "c"],
        "tag_aliases": {"a:z": "c:w", "b:z": "c:w"},
    }}}).for_bank("b1")
    assert pol.tag_aliases == {"a:z": "c:w", "b:z": "c:w"}


def test_vocabulary_overlap_without_aliases_is_still_rejected():
    with pytest.raises(BankPolicyError) as ei:
        _owner_bank(("a", "b"), vocab={"a": ["x"], "b": ["x"]})
    assert "both" in str(ei.value) and "'x'" in str(ei.value)


def test_current_repo_policy_file_still_loads_unchanged():
    """정책 파일(정본)은 바꾸지 않는다: 새 검사를 켠 로더가 현재 파일을 그대로 받아들인다."""
    pols = BankPolicies.from_file(REPO_POLICY_FILE)
    assert {"personal-demo", "personal-alice", "team-eng", "project-demo-gm"} <= set(pols.bank_names())
    demo = pols.for_bank("personal-demo")
    assert demo.tag_required_prefixes == ("kind",) and demo.tag_closed_prefixes == ("kind", "user")


@pytest.mark.parametrize("bank", sorted(BankPolicies.from_file(REPO_POLICY_FILE).bank_names()))
def test_every_loaded_repo_bank_resolves_bare_words_independent_of_prefix_order(bank):
    """로더가 통과시킨 뱅크는 닫힌 필수 prefix 순서를 뒤집어도 모든 맨단어를 같은 값으로 해석한다."""
    from dataclasses import replace
    from itertools import permutations
    from gateway import rules
    pol = BankPolicies.from_file(REPO_POLICY_FILE).for_bank(bank)
    required = [p for p in pol.tag_closed_prefixes if p in pol.tag_required_prefixes]
    words = {s for p in pol.tag_closed_prefixes for s in pol.tag_vocabulary.get(p, ())}
    words |= {k.partition(":")[2] for k in pol.tag_aliases}
    seen = set()
    for order in permutations(pol.tag_closed_prefixes):
        flipped = replace(pol, tag_closed_prefixes=order)
        seen.add(tuple(rules._resolve_closed(f"topic:{w}", flipped) for w in sorted(words)))
    assert len(seen) <= 1, f"{bank}: order-dependent bare words (required={required})"
