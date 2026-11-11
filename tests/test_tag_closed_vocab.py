"""#39: kind·user 닫힌 목록 - 맨단어 해석(alias 경유)과 kind 필수.

어떤 prefix가 닫혀 있는지(`tag_closed_prefixes`)와 무엇이 필수인지(`tag_required_prefixes`)는
정책이 선언한다. rules 코드에는 "kind"·"user" 문자열이 규칙으로 박혀 있지 않다는 것도 여기서
kind·user가 아닌 축의 가짜 뱅크로 증명한다. 어휘·필수를 선언하지 않은 뱅크는 결과가 그대로다.

RULE id는 계약이다(호출자·문서가 인용한다): 목록 밖 값 = RULE:tags-vocabulary,
필수 prefix 부재 = RULE:tags-missing-prefix.
"""
import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import UPSTREAM_KEY, bank_config_reply, call, load_rest_fixture
from gateway import rules
from gateway.acl import ACL
from gateway.app import create_app
from gateway.audit import Audit
from gateway import recurrence
from gateway.bank_policies import BankPolicies, BankPolicy

REPO_ROOT = os.path.dirname(os.path.dirname(__file__))
REPO_POLICY_FILE = os.path.join(REPO_ROOT, "tests", "fixtures", "policies", "sample", "bank-policies.json")
NOW = "2026-10-01T00:00:00Z"

_POLICIES = BankPolicies.from_file(REPO_POLICY_FILE)
DEMO = _POLICIES.for_bank("personal-demo")
ALICE = _POLICIES.for_bank("personal-alice")
TEAM = _POLICIES.for_bank("team-eng")
GOOD = {"content": "[note] 닫힌 목록 테스트"}

VOCAB_RULE = "RULE:tags-vocabulary"
MISSING_RULE = "RULE:tags-missing-prefix"


def tags_of(policy, tags):
    v = rules.evaluate_item(dict(GOOD, tags=tags), policy, NOW)
    return v, v.item["tags"]


# ============================ ① 재현: 선언 전에는 통과했다 ============================

def test_the_policy_declares_kind_and_user_closed_and_kind_required():
    assert DEMO.tag_closed_prefixes == ("kind", "user")
    assert DEMO.tag_required_prefixes == ("kind",)
    assert DEMO.tag_vocabulary["user"] == ("demo",)


def test_without_the_declarations_the_old_behavior_returns():
    """RED 재현: 새 선언을 걷어내면 gotcha는 topic이 되고 kind 없는 태그도 통과한다."""
    from dataclasses import replace
    old = replace(DEMO, tag_closed_prefixes=(), tag_required_prefixes=())
    v, tags = tags_of(old, ["gotcha"])
    assert v.ok and tags == ["topic:gotcha"]
    v, tags = tags_of(old, ["project:x"])
    assert v.ok and tags == ["project:x"]


# ============================ ② 맨단어·topic 해석 (alias 경유) ============================

@pytest.mark.parametrize("sent, stored", [
    ("gotcha", "kind:gotcha"),
    ("done", "kind:completion"),            # done -> kind:done -> kind:completion
    ("topic:gotcha", "kind:gotcha"),        # 칸 오배정 교정
    ("Gotcha", "kind:gotcha"),              # 기계 정규화 후 해석
    ("topic:done", "kind:completion"),
    ("policy", "kind:decision"),            # 기존 topic:policy alias와 같은 결과
])
def test_bare_words_and_misfiled_topics_resolve_into_the_closed_list(sent, stored):
    v, tags = tags_of(DEMO, [sent])
    assert v.ok, v.message
    assert tags == [stored]
    assert v.normalized_tags == ((sent, stored),)       # 보낸 값 -> 저장된 값 공개


def test_a_resolved_duplicate_is_folded_in_either_order():
    v, tags = tags_of(DEMO, ["gotcha", "kind:gotcha"])
    assert v.ok and tags == ["kind:gotcha"]
    v, tags = tags_of(DEMO, ["kind:gotcha", "gotcha"])
    assert v.ok and tags == ["kind:gotcha"]
    v, tags = tags_of(DEMO, ["topic:gotcha", "gotcha"])
    assert v.ok and tags == ["kind:gotcha"]
    assert v.normalized_tags == (("topic:gotcha", "kind:gotcha"), ("gotcha", "kind:gotcha"))


def test_a_word_no_closed_list_claims_stays_a_topic():
    v, tags = tags_of(DEMO, ["kind:fix", "skills", "topic:deploy"])
    assert v.ok and tags == ["kind:fix", "topic:skills", "topic:deploy"]


def test_project_words_are_not_this_tickets_business():
    """project 후보 해석은 #40 소유(tag_candidate_prefixes 선언): 그 선언을 걷어낸 사본에서는 닫힌
    목록만 있는 뱅크가 맨단어를 kind에서 못 찾으면 지금처럼 topic이다."""
    from dataclasses import replace
    closed_only = replace(DEMO, tag_candidate_prefixes=())
    v, tags = tags_of(closed_only, ["kind:fix", "larch-yard", "topic:larch-yard"])
    assert v.ok and tags == ["kind:fix", "topic:larch-yard"]


def test_a_legal_prefixed_tag_of_another_axis_is_not_reinterpreted():
    v, tags = tags_of(DEMO, ["kind:fix", "project:gotcha-lab"])
    assert v.ok and tags == ["kind:fix", "project:gotcha-lab"]


def test_closed_but_not_required_user_is_not_a_resolution_target():
    """해석 대상은 필수 prefix(kind)뿐: 맨단어·topic 의 demo/demoalt 은 topic 으로 남는다.
    user 값은 `user:` prefix 를 직접 붙였을 때만 닫힌 목록 검사와 alias 를 받는다."""
    assert "user" in DEMO.tag_closed_prefixes and "user" not in DEMO.tag_required_prefixes
    for sent in ("demo", "topic:demo", "topic:demoalt"):
        v, tags = tags_of(DEMO, ["kind:fix", sent])
        assert v.ok and tags == ["kind:fix", "topic:" + sent.split(":")[-1]], sent
    v, tags = tags_of(DEMO, ["kind:fix", "user:demoalt"])
    assert v.ok and tags == ["kind:fix", "user:demo"]


# ============================ ③ kind 필수 ============================

@pytest.mark.parametrize("sent", [
    ["project:x"], ["topic:blog"], ["user:demo"], ["project:a", "topic:b", "user:demo"],
])
def test_no_kind_is_rejected_with_the_allowed_list_and_an_example(sent):
    v, _ = tags_of(DEMO, sent)
    assert not v.ok and v.rule_ids == (MISSING_RULE,)
    msg = v.message
    assert msg.startswith(MISSING_RULE)
    assert "kind" in msg and "gotcha" in msg and "completion" in msg      # 허용 목록 근거
    assert '"tags": ["kind:decision"]' in msg                             # 유효 예시


def test_a_kind_that_only_appears_after_resolution_satisfies_the_requirement():
    v, tags = tags_of(DEMO, ["project:x", "gotcha"])
    assert v.ok and tags == ["project:x", "kind:gotcha"]


def test_the_empty_tag_list_keeps_its_own_rule_on_a_tags_required_bank():
    v, _ = tags_of(DEMO, [])
    assert v.rule_ids == ("RULE:tags-required",)


def test_the_empty_tag_list_message_also_names_the_required_prefix_and_allowed_values():
    """두 번째 거부가 생기지 않게: 비어 있는 호출에 kind 필수와 허용 값, 유효 예시를 함께 싣는다."""
    v, _ = tags_of(DEMO, [])
    msg = v.message
    assert "requires a tag for each of: kind" in msg
    assert "Allowed kind values: artifact," in msg
    assert '"tags": ["kind:decision"]' in msg
    # 안내대로 고친 호출은 통과한다
    ok, _ = tags_of(DEMO, ["kind:decision"])
    assert ok.ok


@pytest.mark.parametrize("policy", [ALICE, TEAM], ids=["personal-alice", "team-eng"])
def test_the_empty_tag_list_message_of_banks_without_required_prefixes_is_unchanged(policy):
    v, _ = tags_of(policy, [])
    assert v.rule_ids == ("RULE:tags-required",)
    assert "requires a tag for each of" not in v.message


def test_a_grammar_violation_comes_first_and_carries_the_closed_list_findings():
    v, _ = tags_of(DEMO, ["kind:한글"])
    assert v.rule_ids == ("RULE:tags-grammar", MISSING_RULE)       # 한 번에 고칠 수 있게 함께 보고
    v, _ = tags_of(DEMO, ["프로젝트", "topic:foo"])
    assert v.rule_ids == ("RULE:tags-grammar", MISSING_RULE)
    v, _ = tags_of(DEMO, ["", "kind:foo"])
    assert v.rule_ids == ("RULE:tags-grammar", VOCAB_RULE)
    assert "'kind:foo'" in v.violations[1].message
    v, _ = tags_of(DEMO, ["kind:한글", "kind:fix"])
    assert v.rule_ids == ("RULE:tags-grammar",)                    # kind 가 있으면 문법만


# ============================ ④ 닫힌 목록 ============================

def test_a_kind_outside_the_list_is_rejected_with_the_closest_value():
    v, _ = tags_of(DEMO, ["kind:decison"])
    assert not v.ok and v.rule_ids == (VOCAB_RULE,)
    assert v.message.startswith(VOCAB_RULE)
    assert "'kind:decison' (closest: kind:decision," in v.message      # 상위 3개, 첫째가 가장 가깝다
    assert "Allowed kind values: artifact," in v.message
    assert '"tags": ["kind:decision"]' in v.message


def test_kind_foo_is_rejected_and_says_a_subject_word_belongs_in_topic():
    v, _ = tags_of(DEMO, ["kind:foo", "kind:fix"])
    assert v.rule_ids == (VOCAB_RULE,)
    assert "'kind:foo' (closest: kind:" in v.message      # cutoff 밖이어도 최근접 값을 싣는다
    assert "topic:<slug>" in v.message


def test_one_bad_kind_next_to_a_good_one_is_still_rejected():
    v, _ = tags_of(DEMO, ["kind:fix", "kind:foo"])
    assert not v.ok and v.rule_ids == (VOCAB_RULE,)
    assert "kind:fix'" not in v.message.split("Replace")[0]      # 멀쩡한 태그는 지적하지 않는다


def test_the_closest_hint_is_always_given_even_below_the_similarity_cutoff():
    v, _ = tags_of(DEMO, ["kind:zzzz"])
    assert "(no close match)" not in v.message
    assert "'kind:zzzz' (closest: kind:" in v.message


# ============================ ⑤ user ============================

@pytest.mark.parametrize("sent", ["user:demoalt", "user:demofull", "user:DEMOALT"])
def test_user_aliases_resolve_to_user_demo(sent):
    v, tags = tags_of(DEMO, ["kind:fix", sent])
    assert v.ok, v.message
    assert tags == ["kind:fix", "user:demo"]
    assert v.normalized_tags[-1][1] == "user:demo"


def test_user_kauri_gate_is_rejected_and_is_not_an_alias():
    assert "user:kauri-gate" not in DEMO.tag_aliases
    v, _ = tags_of(DEMO, ["kind:fix", "user:kauri-gate"])
    assert not v.ok and v.rule_ids == (VOCAB_RULE,)
    assert "'user:kauri-gate' (closest: user:demo)" in v.message
    assert "Allowed user values: demo." in v.message
    assert "topic:<slug>" in v.message


def test_an_unknown_user_is_rejected_with_the_only_value_as_a_hint():
    v, _ = tags_of(DEMO, ["kind:fix", "user:demoo"])
    assert v.rule_ids == (VOCAB_RULE,)
    assert "(closest: user:demo)" in v.message


# ============================ ⑥ 선언이 규칙이다: kind·user 하드코딩 없음 ============================

CUSTOM = BankPolicy(
    tags_required=True, tag_prefixes=("area", "topic"),
    tag_vocabulary={"area": ("web", "db")},
    tag_aliases={"area:frontend": "area:web"},
    tag_closed_prefixes=("area",), tag_required_prefixes=("area",),
)


def test_another_axis_can_be_closed_and_required_by_declaration():
    v, tags = tags_of(CUSTOM, ["frontend", "topic:x"])
    assert v.ok and tags == ["area:web", "topic:x"]
    v, tags = tags_of(CUSTOM, ["topic:db"])
    assert v.ok and tags == ["area:db"]
    v, _ = tags_of(CUSTOM, ["topic:x"])
    assert v.rule_ids == (MISSING_RULE,) and "area" in v.message and "web, db" in v.message
    v, _ = tags_of(CUSTOM, ["area:cloud"])
    assert v.rule_ids == (VOCAB_RULE,)


def test_kind_and_user_mean_nothing_to_a_bank_that_does_not_declare_them():
    same_words = BankPolicy(tags_required=True, tag_prefixes=("kind", "user", "topic"))
    v, tags = tags_of(same_words, ["gotcha", "user:anyone"])
    assert v.ok and tags == ["topic:gotcha", "user:anyone"]


# ============================ ⑦ 선언 없는 뱅크는 그대로 ============================

@pytest.mark.parametrize("policy", [ALICE, TEAM], ids=["personal-alice", "team-eng"])
@pytest.mark.parametrize("sent, stored", [
    (["gotcha"], ["topic:gotcha"]),
    (["done"], ["topic:done"]),
    (["topic:gotcha"], ["topic:gotcha"]),
    (["project:x"], ["project:x"]),
    (["kind:foo"], ["kind:foo"]),
    (["user:demoalt"], ["user:demoalt"]),
    (["user:kauri-gate"], ["user:kauri-gate"]),
    (["gotcha", "kind:gotcha"], ["topic:gotcha", "kind:gotcha"]),
])
def test_banks_that_declare_no_closed_lists_are_unchanged(policy, sent, stored):
    assert policy.tag_closed_prefixes == () and policy.tag_required_prefixes == ()
    v, tags = tags_of(policy, sent)
    assert v.ok and tags == stored


def test_the_new_fields_are_absent_from_every_other_declared_bank():
    for name in ("personal-alice", "team-eng", "project-demo-gm", "team-demo", "personal-bob"):
        pol = _POLICIES.for_bank(name)
        assert pol.tag_closed_prefixes == () and pol.tag_required_prefixes == ()


def test_evaluate_tags_and_evaluate_item_agree():
    for sent in (["gotcha"], ["project:x"], ["kind:foo"], ["user:demoalt", "done"]):
        tag_verdict = rules.evaluate_tags(list(sent), DEMO)
        item_verdict = rules.evaluate_item(dict(GOOD, tags=list(sent)), DEMO, NOW)
        assert item_verdict.normalized_tags == tag_verdict.normalized_tags
        assert [v.message for v in tag_verdict.violations] == [
            v.message for v in item_verdict.violations]


# ============================ ⑧ initialize instructions ============================

def test_personal_demo_instructions_announce_kind_required_and_stay_within_the_cap():
    text = rules.instructions_for(DEMO)
    assert "REQUIRED: kind" in text
    assert "Closed lists: kind (26 values), user = demo" in text
    assert len(text.encode("utf-8")) <= 2048      # INSTRUCTIONS_MAX_BYTES 를 렌더링 결과에도 지킨다


def test_instructions_of_banks_without_closed_lists_carry_no_such_line():
    for pol in (ALICE, TEAM):
        assert "Closed lists" not in (rules.instructions_for(pol) or "")


# ============================ ⑨ 표면: MCP 와 REST ============================

DEMO_TOKEN = "tok-test-demo"        # 테스트 전용 리터럴. 실제 토큰 아님.
ACL_DEMO = {"tokens": {
    DEMO_TOKEN: {"identity": "demo", "personal": "personal-demo", "teams": ["team-eng"]},
}}


@pytest.fixture
def surface(recorder, tmp_path):
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder.handler),
                               base_url="http://upstream", timeout=5.0)
    app = create_app(acl=ACL.from_dict(ACL_DEMO), audit=Audit(path=str(tmp_path / "audit.log")),
                     upstream_key=UPSTREAM_KEY, http_client=client,
                     bank_policies=BankPolicies.from_file(REPO_POLICY_FILE))
    return TestClient(app)


def mcp_retain(client, tags):
    return call(client, "personal-demo", DEMO_TOKEN, name="sync_retain", arguments=dict(GOOD, tags=tags))


def rest_retain(client, tags):
    it = dict(load_rest_fixture("memories_post_ok")["request"]["items"][0])
    it.pop("timestamp", None)
    it["tags"] = tags
    return client.post("/v1/default/banks/personal-demo/memories",
                       content=json.dumps({"items": [it], "async": False}),
                       headers={"content-type": "application/json",
                                "authorization": f"Bearer {DEMO_TOKEN}"})


# 스펙 "회귀 ... MCP와 REST 둘 다"를 케이스 x 표면 완전 행렬로 고정한다.
# 통과: (보낸 태그, 저장된 태그). 거부: (보낸 태그, RULE id, 메시지에 반드시 들어갈 조각).
PASS_CASES = {
    "gotcha": (["gotcha"], ["kind:gotcha"]),
    "done": (["done"], ["kind:completion"]),
    "topic-gotcha": (["topic:gotcha"], ["kind:gotcha"]),
    "user-demoalt": (["kind:fix", "user:demoalt"], ["kind:fix", "user:demo"]),
}
REJECT_CASES = {
    "no-kind": (["project:x"], MISSING_RULE, ["Allowed kind values:", '"tags": ["kind:decision"]']),
    "kind-foo": (["kind:foo"], VOCAB_RULE, ["'kind:foo' (closest: kind:", "Allowed kind values:"]),
    "user-kauri-gate": (["kind:fix", "user:kauri-gate"], VOCAB_RULE,
                         ["'user:kauri-gate' (closest: user:demo)", "Allowed user values: demo."]),
}
SURFACES = ("mcp", "rest")


def _send(surface, which, tags):
    return mcp_retain(surface, tags) if which == "mcp" else rest_retain(surface, tags)


def _forwarded_tags(recorder, which):
    body = recorder.last["body"]
    return body["params"]["arguments"]["tags"] if which == "mcp" else body["items"][0]["tags"]


@pytest.mark.parametrize("which", SURFACES)
@pytest.mark.parametrize("case", list(PASS_CASES))
def test_surface_matrix_corrections_reach_upstream_and_are_disclosed(surface, recorder, case, which):
    sent, stored = PASS_CASES[case]
    r = _send(surface, which, sent)
    assert r.status_code == 200
    assert recorder.calls, "교정된 요청은 upstream 으로 전달돼야 한다"
    assert _forwarded_tags(recorder, which) == stored
    if which == "rest":                                   # REST 는 응답에 보낸 값 -> 저장된 값을 공개한다
        rewrites = r.json()["gateway_normalized_tags"][0]["rewrites"]
        assert {(w["sent"], w["stored"]) for w in rewrites} <= {(s, t) for s in sent for t in stored}
        assert any(w["stored"] in stored for w in rewrites)
    else:                                                  # MCP 는 교정 노트가 응답 본문에 붙는다
        assert "->" in json.dumps(r.json(), ensure_ascii=False)


@pytest.mark.parametrize("which", SURFACES)
@pytest.mark.parametrize("case", list(REJECT_CASES))
def test_surface_matrix_rejections_carry_the_rule_id_and_never_reach_upstream(surface, recorder, case, which):
    sent, rule, fragments = REJECT_CASES[case]
    r = _send(surface, which, sent)
    assert recorder.calls == []
    if which == "mcp":
        assert r.status_code == 200                                   # MCP 는 422 가 아니다
        result = r.json()["result"]
        assert result["isError"] is True
        text = result["content"][0]["text"]
        assert text.startswith(rule)
    else:
        assert r.status_code == 422
        data = r.json()
        assert data["error"] == "rule-violation" and data["rule"] == rule
        assert "detail" not in data
        assert data["rule_detail"][0]["rules"] == [rule]
        text = data["rule_detail"][0]["message"]
        assert text.startswith(rule)
    for fragment in fragments:
        assert fragment in text, (which, case, fragment)


# ============================ ⑩ MCP 재발 게이트는 정규화 뒤 태그로 판정한다 ============================

class _GateUpstream:
    """게이트 recall(REST) 호출 여부만 센다. 나머지는 성공으로 답한다."""

    def __init__(self):
        self.paths = []

    def handler(self, request):
        if (config := bank_config_reply(request)) is not None:   # #473 b upstream config read
            return config
        self.paths.append(request.url.path)
        if request.url.path.endswith("/memories/recall"):
            return httpx.Response(200, json={"results": []},
                                  headers={"content-type": "application/json"})
        if request.url.path == "/mcp":
            body = json.loads(request.content or b"{}")
            result = {"content": [{"type": "text", "text": "{}"}], "isError": False}
            return httpx.Response(
                200, content=b"event: message\r\ndata: " + json.dumps(
                    {"jsonrpc": "2.0", "id": body.get("id"), "result": result}).encode() + b"\r\n\r\n",
                headers={"content-type": "text/event-stream", "mcp-session-id": "s1"})
        return httpx.Response(200, json={"success": True, "items_count": 1},
                              headers={"content-type": "application/json"})

    @property
    def gate_recalls(self):
        return [p for p in self.paths if p.endswith("/memories/recall")]


@pytest.fixture
def gated(tmp_path):
    up = _GateUpstream()
    client = httpx.AsyncClient(transport=httpx.MockTransport(up.handler),
                               base_url="http://upstream", timeout=5.0)
    app = create_app(acl=ACL.from_dict(ACL_DEMO), audit=Audit(path=str(tmp_path / "audit.log")),
                     upstream_key=UPSTREAM_KEY, http_client=client,
                     bank_policies=BankPolicies.from_file(REPO_POLICY_FILE),
                     recall_ledger=recurrence.RecallLedger(str(tmp_path / "recall-queries.log")),
                     pending_triggers=recurrence.PendingTriggers(str(tmp_path / "pending.jsonl")))
    return TestClient(app), up


@pytest.mark.parametrize("sent", [["project:x", "gotcha"], ["project:x", "topic:gotcha"],
                                  ["project:x", "kind:lesson"]])
@pytest.mark.parametrize("which", SURFACES)
def test_gotcha_gate_fires_on_what_is_stored_not_on_what_was_sent(gated, sent, which):
    """저장 태그가 kind:gotcha 가 되는 입력은 MCP 와 REST 에서 같은 게이트 판정을 받는다."""
    client, up = gated
    if which == "mcp":
        r = call(client, "personal-demo", DEMO_TOKEN, name="sync_retain",
                 arguments=dict(GOOD, tags=sent))
    else:
        it = dict(load_rest_fixture("memories_post_ok")["request"]["items"][0])
        it.pop("timestamp", None)
        it["tags"] = sent
        r = client.post("/v1/default/banks/personal-demo/memories",
                        content=json.dumps({"items": [it], "async": False}),
                        headers={"content-type": "application/json",
                                 "authorization": f"Bearer {DEMO_TOKEN}"})
    assert r.status_code == 200
    assert len(up.gate_recalls) == 1, up.paths


@pytest.mark.parametrize("which", SURFACES)
def test_a_non_gotcha_retain_pays_no_gate_recall_on_either_surface(gated, which):
    client, up = gated
    if which == "mcp":
        call(client, "personal-demo", DEMO_TOKEN, name="sync_retain",
             arguments=dict(GOOD, tags=["project:x", "kind:fix"]))
    else:
        it = dict(load_rest_fixture("memories_post_ok")["request"]["items"][0])
        it.pop("timestamp", None)
        it["tags"] = ["project:x", "kind:fix"]
        client.post("/v1/default/banks/personal-demo/memories",
                    content=json.dumps({"items": [it], "async": False}),
                    headers={"content-type": "application/json",
                             "authorization": f"Bearer {DEMO_TOKEN}"})
    assert up.gate_recalls == []
