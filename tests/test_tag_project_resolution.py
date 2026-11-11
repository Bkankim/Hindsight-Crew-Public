"""#40: project 조건부 교정(ADR 0001)과 비슷한 이름 경고 - 열린 어휘 project.

맨단어·`topic:X`에서 후보 `project:X`에 alias를 먼저 적용하고 그 결과가 project 어휘에 있으면,
같은 retain 한 건의 태그 목록을 보고 옮기거나(project 없음·후보 하나), 모두 topic에 두고 경고하거나
(후보 둘 이상), 교차 참조로 그대로 두거나(다른 project 있음), 중복만 뺀다(같은 project 있음).
project는 닫힌 목록이 아니다: 새 이름은 거부하지 않고, 비슷한 기존 이름이 있으면 경고만 단다.

"project를 후보로 삼는다"는 것도 정책이 선언한다(`tag_candidate_prefixes`). rules 코드에 "project"
문자열이 규칙으로 박혀 있지 않다는 것은 project가 아닌 축의 가짜 뱅크로 증명한다. 선언이 없는
뱅크는 결과가 그대로다(f3010c3 와 같은 불변식).
"""
import json
import os
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import UPSTREAM_KEY, call, load_rest_fixture
from gateway import rules
from gateway.acl import ACL
from gateway.app import create_app
from gateway.audit import Audit
from gateway.bank_policies import BankPolicies, BankPolicy, BankPolicyError

REPO_ROOT = os.path.dirname(os.path.dirname(__file__))
REPO_POLICY_FILE = os.path.join(REPO_ROOT, "tests", "fixtures", "policies", "sample", "bank-policies.json")
NOW = "2026-10-01T00:00:00Z"

_POLICIES = BankPolicies.from_file(REPO_POLICY_FILE)
DEMO = _POLICIES.for_bank("personal-demo")
ALICE = _POLICIES.for_bank("personal-alice")
TEAM = _POLICIES.for_bank("team-eng")
GOOD = {"content": "[note] project 해석 테스트"}


def tags_of(policy, tags):
    v = rules.evaluate_item(dict(GOOD, tags=tags), policy, NOW)
    return v, v.item["tags"]


# ============================ ① 재현: 선언 전에는 topic에 남았다 ============================

def test_the_policy_declares_project_as_a_candidate_prefix_and_keeps_it_open():
    assert DEMO.tag_candidate_prefixes == ("project",)
    assert "project" not in DEMO.tag_closed_prefixes


def test_without_the_declaration_the_old_behavior_returns():
    """RED 재현: 선언을 걷어내면 project 없는 기억의 larch-yard 는 topic 에 남고 경고도 없다."""
    old = replace(DEMO, tag_candidate_prefixes=())
    v, tags = tags_of(old, ["kind:fix", "larch-yard"])
    assert v.ok and tags == ["kind:fix", "topic:larch-yard"] and v.warnings == ()
    v, tags = tags_of(old, ["kind:fix", "project:hemlock-loom-v2"])
    assert v.ok and tags == ["kind:fix", "project:hemlock-loom-v2"] and v.warnings == ()


# ============================ ② 네 경우 (ADR 0001) ============================

@pytest.mark.parametrize("sent, pair", [
    (["kind:fix", "larch-yard"], ("larch-yard", "project:larch-yard")),            # 맨단어
    (["kind:fix", "topic:larch-yard"], ("topic:larch-yard", "project:larch-yard")),  # 칸 오배정
    (["topic:larch-yard", "kind:fix"], ("topic:larch-yard", "project:larch-yard")),  # 순서 무관
])
def test_no_project_and_exactly_one_candidate_moves_it_to_project(sent, pair):
    v, tags = tags_of(DEMO, sent)
    assert v.ok and v.warnings == ()
    assert tags == [pair[1] if t in (pair[0],) else t for t in sent]
    assert v.normalized_tags == (pair,)              # 보낸 값 -> 저장된 값 공개


def test_the_same_project_named_twice_collapses_into_one_project_tag():
    v, tags = tags_of(DEMO, ["kind:fix", "larch-yard", "topic:larch-yard"])
    assert v.ok and tags == ["kind:fix", "project:larch-yard"]
    assert v.normalized_tags == (("larch-yard", "project:larch-yard"),
                                 ("topic:larch-yard", "project:larch-yard"))


@pytest.mark.parametrize("sent", [
    ["kind:fix", "topic:larch-yard", "topic:tupelo"],
    ["kind:fix", "larch-yard", "tupelo"],
    ["kind:fix", "topic:larch-yard", "topic:rowan-tide"],
])
def test_two_or_more_candidates_stay_topics_with_one_warning(sent):
    v, tags = tags_of(DEMO, sent)
    assert v.ok                                       # 경고는 저장을 막지 않는다
    assert tags == ["kind:fix"] + [t if t.startswith("topic:") else f"topic:{t}" for t in sent[1:]]
    assert not any(t.startswith("project:") for t in tags)
    assert len(v.warnings) == 1
    w = v.warnings[0]
    assert w.startswith("[gateway 경고]")
    for t in tags[1:]:                                # 후보 topic 과 그 project 이름을 모두 알려 준다
        assert t in w


def test_the_two_candidate_warning_names_the_aliased_project_not_the_word():
    v, _ = tags_of(DEMO, ["kind:fix", "topic:larch-yard", "topic:tupelo"])
    assert "topic:tupelo -> project:alder-thistle" in v.warnings[0]


@pytest.mark.parametrize("sent", [
    ["project:rowan-tide", "topic:larch-yard", "kind:fix"],
    ["topic:larch-yard", "project:rowan-tide", "kind:fix"],
    ["project:rowan-tide", "topic:larch-yard", "topic:tupelo", "kind:fix"],   # 후보 둘이어도 교차 참조
    ["project:brand-new-thing", "topic:larch-yard", "kind:fix"],               # 어휘 밖 project 도 project
])
def test_another_project_present_keeps_the_topic_as_a_cross_reference_without_warning(sent):
    v, tags = tags_of(DEMO, sent)
    assert v.ok and tags == sent
    assert v.warnings == () and v.normalized_tags == ()


@pytest.mark.parametrize("sent, stored", [
    (["project:larch-yard", "topic:larch-yard", "kind:fix"], ["project:larch-yard", "kind:fix"]),
    (["topic:larch-yard", "project:larch-yard", "kind:fix"], ["project:larch-yard", "kind:fix"]),
    (["project:larch-yard", "larch-yard", "kind:fix"], ["project:larch-yard", "kind:fix"]),
])
def test_the_same_project_present_drops_the_topic_duplicate_and_says_so(sent, stored):
    v, tags = tags_of(DEMO, sent)
    assert v.ok and tags == stored and v.warnings == ()
    (before, after), = v.normalized_tags
    assert before in ("topic:larch-yard", "larch-yard") and after == "project:larch-yard"


def test_dropping_a_duplicate_does_not_turn_a_second_candidate_into_a_move():
    """같은 project 가 있으면 다른 후보 topic 은 교차 참조다(project 가 이미 있으므로)."""
    v, tags = tags_of(DEMO, ["project:larch-yard", "topic:larch-yard", "topic:tupelo", "kind:fix"])
    assert v.ok and tags == ["project:larch-yard", "topic:tupelo", "kind:fix"] and v.warnings == ()


# ============================ ③ alias 를 먼저 적용한다 ============================

@pytest.mark.parametrize("sent, stored", [
    ("quince", "project:hickory-bell"),                 # 기존 topic:quince alias
    ("topic:quince", "project:hickory-bell"),
    ("tupelo", "project:alder-thistle"),            # project:tupelo -> project:alder-thistle
    ("topic:tupelo", "project:alder-thistle"),
    ("birch", "project:fir-easel"),
    ("juniper", "project:poplar-kiln"),
    ("banksia-loft", "project:elm-gazette"),
    ("myall-shore", "project:larch-yard"),
    ("wattle-cove-2026", "project:rowan-tide"),
])
def test_a_candidate_is_aliased_before_the_vocabulary_is_consulted(sent, stored):
    """`birch` 는 어휘에 없다. alias 를 거친 `project:fir-easel` 가 어휘에 있어서 옮겨진다."""
    assert sent.rpartition(":")[2] not in DEMO.tag_vocabulary["project"]
    v, tags = tags_of(DEMO, ["kind:fix", sent])
    assert v.ok and tags == ["kind:fix", stored]
    assert v.normalized_tags[-1][1] == stored and v.warnings == ()


def test_a_topic_alias_to_a_project_is_a_candidate_not_an_unconditional_rewrite():
    """`topic:quince` -> `project:hickory-bell` (alias) is decided by the whole list like any candidate:
    another project present -> cross reference (stays topic, no warning); two candidates -> warn."""
    v, tags = tags_of(DEMO, ["kind:fix", "project:rowan-tide", "topic:quince"])
    assert v.ok and tags == ["kind:fix", "project:rowan-tide", "topic:quince"]
    assert v.warnings == () and v.normalized_tags == ()
    v, tags = tags_of(DEMO, ["kind:fix", "topic:quince", "topic:larch-yard"])
    assert v.ok and tags == ["kind:fix", "topic:quince", "topic:larch-yard"]
    assert len(v.warnings) == 1 and "topic:quince -> project:hickory-bell" in v.warnings[0]
    v, tags = tags_of(DEMO, ["kind:fix", "project:hickory-bell", "topic:quince"])      # 같은 project
    assert v.ok and tags == ["kind:fix", "project:hickory-bell"]
    assert v.normalized_tags == (("topic:quince", "project:hickory-bell"),)


@pytest.mark.parametrize("sent, stored", [
    ("project:wattle-cove-2026", "project:rowan-tide"),
    ("project:banksia-loft", "project:elm-gazette"),
    ("project:myall-shore", "project:larch-yard"),
    ("project:birch", "project:fir-easel"),
    ("project:juniper", "project:poplar-kiln"),
    ("project:tupelo", "project:alder-thistle"),
    ("topic:deployment", "topic:deploy"),
    ("topic:humanizer", "topic:humanize"),
    ("topic:git-commit-lore", "topic:commit-lore"),
    ("topic:dashboard-design", "topic:dashboard"),
    ("topic:headless-claude", "topic:headless"),
])
def test_the_write_time_aliases_rewrite_and_disclose(sent, stored):
    v, tags = tags_of(DEMO, ["kind:fix", sent])
    assert v.ok and tags == ["kind:fix", stored]
    assert v.normalized_tags == ((sent, stored),)
    assert v.warnings == ()                          # alias 가 어휘 안으로 넣었으니 경고할 것이 없다


def test_the_bare_spellings_of_the_topic_aliases_land_on_the_canonical_topic():
    v, tags = tags_of(DEMO, ["kind:fix", "deployment", "humanizer"])
    assert v.ok and tags == ["kind:fix", "topic:deploy", "topic:humanize"]


def test_the_closed_list_is_resolved_before_the_project_candidate():
    """해석 순서: kind·user(닫힌) -> project -> topic. 두 어휘에 같은 slug 가 있으면 kind 가 이긴다."""
    both = replace(DEMO, tag_vocabulary={**DEMO.tag_vocabulary,
                                      "project": DEMO.tag_vocabulary["project"] + ("gotcha",)})
    v, tags = tags_of(both, ["gotcha"])
    assert v.ok and tags == ["kind:gotcha"] and v.warnings == ()


def test_a_word_no_vocabulary_claims_is_still_a_topic():
    v, tags = tags_of(DEMO, ["kind:fix", "skills", "topic:deploy"])
    assert v.ok and tags == ["kind:fix", "topic:skills", "topic:deploy"] and v.warnings == ()


# ============================ ④ 비슷한 새 이름 경고 ============================

@pytest.mark.parametrize("new, near", [
    ("hemlock-loom-v2", "project:hemlock-loom"),        # 접두(하이픈 경계)
    ("my-willow", "project:willow"),                          # 접미(하이픈 경계)
    ("larch-yard-shop", "project:larch-yard"),
    ("elm-gazete", "project:elm-gazette"),                      # 편집 거리 1
    ("hemlock-lom", "project:hemlock-loom"),
])
def test_a_new_project_close_to_an_existing_one_is_accepted_with_a_warning(new, near):
    v, tags = tags_of(DEMO, ["kind:fix", f"project:{new}"])
    assert v.ok                                               # 절대 거부하지 않는다
    assert tags == ["kind:fix", f"project:{new}"] and v.normalized_tags == ()
    assert len(v.warnings) == 1
    assert v.warnings[0].startswith("[gateway 경고]")
    assert f"project:{new}" in v.warnings[0] and near in v.warnings[0]


def test_the_hyphen_boundary_is_required_for_an_affix_relation():
    v, _ = tags_of(DEMO, ["kind:fix", "project:willowbox"])   # 'willow' 의 접두지만 하이픈 경계가 아님
    assert v.ok and v.warnings == ()


@pytest.mark.parametrize("name", [
    "brand-new-thing", "payments", "abc",                      # 멀리 떨어진 이름·짧은 이름
    "larch-yard",                                                # 어휘 안의 이름은 경고 대상이 아니다
])
def test_a_new_but_unrelated_project_name_gets_no_warning(name):
    v, tags = tags_of(DEMO, ["kind:fix", f"project:{name}"])
    assert v.ok and tags == ["kind:fix", f"project:{name}"] and v.warnings == ()


def test_the_warning_lists_at_most_three_nearest_names():
    v, _ = tags_of(DEMO, ["kind:fix", "project:larch-yard-doc"])
    assert v.ok and len(v.warnings) == 1
    listed = v.warnings[0].split("비슷합니다: ")[1].split(". ")[0].split(", ")
    assert listed[0] == "project:larch-yard-docs" and 1 <= len(listed) <= 3


def test_a_similar_name_warning_does_not_depend_on_the_other_tags():
    v, _ = tags_of(DEMO, ["project:rowan-tide", "project:hemlock-loom-v2", "kind:fix"])
    assert v.ok and len(v.warnings) == 1 and "project:hemlock-loom-v2" in v.warnings[0]


def test_each_new_similar_name_warns_once_even_when_repeated():
    v, _ = tags_of(DEMO, ["kind:fix", "project:hemlock-loom-v2", "project:hemlock-loom-v2"])
    assert len(v.warnings) == 1


def test_an_absurdly_long_slug_is_checked_without_building_a_distance_table(monkeypatch):
    """편집 거리는 길이 차가 임계 이내인 이름에만 계산한다(이벤트 루프를 붙잡지 않는다)."""
    calls = []
    real = rules._edit_distance
    monkeypatch.setattr(rules, "_edit_distance", lambda a, b: calls.append((len(a), len(b))) or real(a, b))
    v, _ = tags_of(DEMO, ["kind:fix", "project:" + "a" * 200000])
    assert v.ok and v.warnings == ()
    assert calls == []
    v, _ = tags_of(DEMO, ["kind:fix", "project:" + "hemlock-loom-" + "a" * 50000])   # 접두 관계는 경고
    assert v.ok and len(v.warnings) == 1 and "project:hemlock-loom" in v.warnings[0]
    assert calls == []


def test_topic_tags_are_never_name_checked():
    v, _ = tags_of(DEMO, ["kind:fix", "topic:hemlock-loom-v2"])
    assert v.ok and v.warnings == ()


def test_evaluate_tags_and_evaluate_item_agree_on_warnings():
    for sent in (["kind:fix", "project:hemlock-loom-v2"], ["topic:larch-yard", "topic:tupelo"],
                 ["larch-yard"], ["project:x", "topic:larch-yard"]):
        tv = rules.evaluate_tags(list(sent), DEMO)
        iv = rules.evaluate_item(dict(GOOD, tags=list(sent)), DEMO, NOW)
        assert iv.warnings == tv.warnings and iv.normalized_tags == tv.normalized_tags


# ============================ ⑤ 어휘·선언 없는 뱅크는 그대로 ============================

@pytest.mark.parametrize("policy", [ALICE, TEAM], ids=["personal-alice", "team-eng"])
@pytest.mark.parametrize("sent, stored", [
    (["larch-yard"], ["topic:larch-yard"]),
    (["topic:larch-yard", "topic:tupelo"], ["topic:larch-yard", "topic:tupelo"]),
    (["project:wattle-cove-2026"], ["project:wattle-cove-2026"]),
    (["project:hemlock-loom-v2"], ["project:hemlock-loom-v2"]),
    (["project:larch-yard", "topic:larch-yard"], ["project:larch-yard", "topic:larch-yard"]),
    (["birch", "deployment"], ["topic:birch", "topic:deployment"]),
])
def test_banks_that_declare_no_candidate_prefixes_are_unchanged(policy, sent, stored):
    assert policy.tag_candidate_prefixes == ()
    v, tags = tags_of(policy, sent)
    assert v.ok and tags == stored and v.warnings == ()


def test_the_declaration_is_absent_from_every_other_declared_bank():
    for name in ("personal-alice", "team-eng", "project-demo-gm", "team-demo", "personal-bob"):
        assert _POLICIES.for_bank(name).tag_candidate_prefixes == ()


# ============================ ⑥ rules 에는 prefix 이름이 박혀 있지 않다 ============================

CUSTOM = BankPolicy(
    tags_required=True,
    tag_prefixes=("kind", "topic", "area"),
    tag_vocabulary={"kind": ("fix", "note"), "area": ("web", "web-legacy", "db")},
    tag_closed_prefixes=("kind",),
    tag_required_prefixes=("kind",),
    tag_candidate_prefixes=("area",),
    tag_aliases={"area:website": "area:web"},
)


def test_a_bank_with_another_candidate_axis_resolves_the_same_way():
    v, tags = tags_of(CUSTOM, ["kind:fix", "website"])            # alias 먼저, 그다음 어휘
    assert v.ok and tags == ["kind:fix", "area:web"] and v.warnings == ()
    v, tags = tags_of(CUSTOM, ["kind:fix", "web", "db"])           # 후보 둘 이상
    assert v.ok and tags == ["kind:fix", "topic:web", "topic:db"]
    assert len(v.warnings) == 1 and "area" in v.warnings[0]
    v, tags = tags_of(CUSTOM, ["kind:fix", "area:db", "topic:web"])    # 다른 area 가 있다
    assert v.ok and tags == ["kind:fix", "area:db", "topic:web"] and v.warnings == ()
    v, tags = tags_of(CUSTOM, ["kind:fix", "area:web", "topic:web"])   # 같은 area
    assert v.ok and tags == ["kind:fix", "area:web"]
    v, _ = tags_of(CUSTOM, ["kind:fix", "area:web-lgcy"])
    assert v.ok and "area:web-legacy" in v.warnings[0]


def test_with_two_candidate_prefixes_the_first_declared_one_decides_a_topic():
    """앞 prefix 가 topic 으로 남긴(경고·교차 참조) 값을 다음 prefix 가 다시 옮기지 않는다."""
    two = replace(CUSTOM, tag_prefixes=("kind", "topic", "area", "zone"),
                  tag_vocabulary={**CUSTOM.tag_vocabulary, "zone": ("web", "cache")},
                  tag_candidate_prefixes=("area", "zone"))
    v, tags = tags_of(two, ["kind:fix", "topic:web", "topic:db"])       # area 가 모호 -> 둘 다 topic + 경고
    assert v.ok and tags == ["kind:fix", "topic:web", "topic:db"] and len(v.warnings) == 1
    v, tags = tags_of(two, ["kind:fix", "area:db", "topic:web"])        # 교차 참조는 zone 이 옮기지 않는다
    assert v.ok and tags == ["kind:fix", "area:db", "topic:web"] and v.warnings == ()
    v, tags = tags_of(two, ["kind:fix", "topic:cache"])                 # area 가 주장하지 않으면 zone
    assert v.ok and tags == ["kind:fix", "zone:cache"]


def test_a_bank_without_a_topic_bucket_still_gets_the_similar_name_warning():
    """topic 칸이 없는 뱅크는 풀 맨단어가 없지만, 후보 prefix 의 비슷한 이름 경고는 그대로 난다."""
    bare = replace(CUSTOM, tag_prefixes=("kind", "area"), tag_aliases={})
    v, tags = tags_of(bare, ["kind:fix", "area:web-lgcy"])
    assert v.ok and tags == ["kind:fix", "area:web-lgcy"]
    assert "area:web-legacy" in v.warnings[0]
    v, _ = tags_of(bare, ["kind:fix", "area:db"])
    assert v.ok and v.warnings == ()


def test_the_word_project_means_nothing_to_a_bank_that_does_not_declare_it_a_candidate():
    same_words = replace(CUSTOM, tag_candidate_prefixes=(),
                         tag_vocabulary={"kind": ("fix", "note"), "project": ("larch-yard",)},
                         tag_prefixes=("kind", "topic", "project"))
    v, tags = tags_of(same_words, ["kind:fix", "larch-yard"])
    assert v.ok and tags == ["kind:fix", "topic:larch-yard"] and v.warnings == ()


# ============================ ⑦ 로더 검증 ============================

def _bank(**over):
    decl = {
        "tag_prefixes": ["project", "kind", "topic"],
        "tag_vocabulary": {"kind": ["fix"], "project": ["alpha"]},
        "tag_closed_prefixes": ["kind"],
        "tag_candidate_prefixes": ["project"],
    }
    decl.update(over)
    return {"banks": {"b": decl}}


def test_a_valid_candidate_declaration_loads():
    pol = BankPolicies.from_dict(_bank()).for_bank("b")
    assert pol.tag_candidate_prefixes == ("project",)


@pytest.mark.parametrize("over, fragment", [
    ({"tag_candidate_prefixes": ["area"]}, "not among this bank's tag_prefixes"),
    ({"tag_candidate_prefixes": ["project", "project"]}, "duplicate prefix"),
    ({"tag_candidate_prefixes": ["topic"]}, "declares no list for it"),
    ({"tag_candidate_prefixes": ["kind"]}, "also closed"),
    ({"tag_candidate_prefixes": "project"}, "expected array"),
    ({"tag_prefixes": [], "tag_vocabulary": {}, "tag_closed_prefixes": []}, "without tag_prefixes"),
])
def test_a_malformed_candidate_declaration_fails_at_load(over, fragment):
    with pytest.raises(BankPolicyError) as exc:
        BankPolicies.from_dict(_bank(**over))
    assert fragment in str(exc.value)


def test_the_default_is_no_candidates():
    assert BankPolicy().tag_candidate_prefixes == ()
    assert BankPolicies.from_dict({"banks": {"b": {"tag_prefixes": ["topic"]}}}) \
        .for_bank("b").tag_candidate_prefixes == ()


# ============================ ⑧ 표면: 경고가 MCP 와 REST 응답에 보인다 ============================

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


def _send(surface, which, tags):
    return mcp_retain(surface, tags) if which == "mcp" else rest_retain(surface, tags)


def _forwarded_tags(recorder, which):
    body = recorder.last["body"]
    return body["params"]["arguments"]["tags"] if which == "mcp" else body["items"][0]["tags"]


def _mcp_text(r):
    return "\n".join(c.get("text", "") for c in r.json()["result"]["content"])


SURFACES = ("mcp", "rest")


@pytest.mark.parametrize("which", SURFACES)
def test_similar_name_warning_reaches_the_caller_and_the_retain_is_stored(surface, recorder, which):
    sent = ["kind:fix", "project:hemlock-loom-v2"]
    r = _send(surface, which, sent)
    assert r.status_code == 200 and recorder.calls            # 저장을 막지 않는다
    assert _forwarded_tags(recorder, which) == sent           # 태그도 그대로 저장
    if which == "mcp":
        text = _mcp_text(r)
        assert r.json()["result"].get("isError") is not True
        assert "[gateway 경고]" in text and "project:hemlock-loom-v2" in text
        assert "비슷합니다: project:hemlock-loom." in text
    else:
        warnings = r.json()["gateway_warnings"]
        assert [w["index"] for w in warnings] == [0]
        assert "project:hemlock-loom-v2" in warnings[0]["text"]
        assert "project:hemlock-loom" in warnings[0]["text"]
        assert "gateway_normalized_tags" not in r.json()        # 교정은 없었다


@pytest.mark.parametrize("which", SURFACES)
def test_ambiguous_candidates_warning_reaches_the_caller(surface, recorder, which):
    sent = ["kind:fix", "topic:larch-yard", "topic:tupelo"]
    r = _send(surface, which, sent)
    assert r.status_code == 200 and recorder.calls
    assert _forwarded_tags(recorder, which) == sent
    if which == "mcp":
        text = _mcp_text(r)
        assert "[gateway 경고]" in text and "topic:tupelo -> project:alder-thistle" in text
    else:
        texts = [w["text"] for w in r.json()["gateway_warnings"]]
        assert len(texts) == 1 and "topic:tupelo -> project:alder-thistle" in texts[0]


@pytest.mark.parametrize("which", SURFACES)
def test_a_project_move_is_forwarded_and_disclosed_on_both_surfaces(surface, recorder, which):
    r = _send(surface, which, ["kind:fix", "larch-yard"])
    assert r.status_code == 200
    assert _forwarded_tags(recorder, which) == ["kind:fix", "project:larch-yard"]
    if which == "mcp":
        assert "larch-yard -> project:larch-yard" in _mcp_text(r)
        assert "[gateway 경고]" not in _mcp_text(r)
    else:
        body = r.json()
        assert body["gateway_normalized_tags"][0]["rewrites"] == [
            {"sent": "larch-yard", "stored": "project:larch-yard"}]
        assert "gateway_warnings" not in body


@pytest.mark.parametrize("which", SURFACES)
def test_a_cross_reference_carries_no_warning_on_either_surface(surface, recorder, which):
    sent = ["kind:fix", "project:rowan-tide", "topic:larch-yard"]
    r = _send(surface, which, sent)
    assert r.status_code == 200 and _forwarded_tags(recorder, which) == sent
    if which == "mcp":
        assert "[gateway 경고]" not in _mcp_text(r)
    else:
        assert "gateway_warnings" not in r.json() and "gateway_normalized_tags" not in r.json()


@pytest.mark.parametrize("which", SURFACES)
def test_an_alias_spelling_is_rewritten_on_both_surfaces(surface, recorder, which):
    r = _send(surface, which, ["kind:fix", "project:wattle-cove-2026"])
    assert r.status_code == 200
    assert _forwarded_tags(recorder, which) == ["kind:fix", "project:rowan-tide"]


def test_rest_warnings_are_indexed_per_item(surface, recorder):
    base = dict(load_rest_fixture("memories_post_ok")["request"]["items"][0])
    base.pop("timestamp", None)
    items = [dict(base, tags=["kind:fix", "project:rowan-tide"]),
             dict(base, tags=["kind:fix", "project:hemlock-loom-v2"])]
    r = surface.post("/v1/default/banks/personal-demo/memories",
                     content=json.dumps({"items": items, "async": False}),
                     headers={"content-type": "application/json",
                              "authorization": f"Bearer {DEMO_TOKEN}"})
    assert r.status_code == 200
    assert [w["index"] for w in r.json()["gateway_warnings"]] == [1]


@pytest.mark.parametrize("which", SURFACES)
def test_a_rejected_retain_carries_no_warning_and_is_not_forwarded(surface, recorder, which):
    r = _send(surface, which, ["project:hemlock-loom-v2"])       # kind 없음 -> 거부
    assert recorder.calls == []
    text = (r.json()["result"]["content"][0]["text"] if which == "mcp"
            else json.dumps(r.json(), ensure_ascii=False))
    assert "RULE:tags-missing-prefix" in text and "[gateway 경고]" not in text
