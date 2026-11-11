"""#38: 붙어 온 태그 분리 - 태그 어휘를 선언한 뱅크에서만.

한 문자열에 여러 태그가 붙어 오면(`"kind:gotcha topic:blog"`) 지금까지는 첫 콜론 뒤를
통째로 slugify해 `kind:gotcha-topic-blog` 하나가 됐다. 어휘(`tag_vocabulary`)를 선언한
뱅크는 구분자(쉼표·세미콜론·공백) 바로 뒤에 선언된 prefix와 콜론이 오면 거기서 나눈다.

기준 값은 main(4026bf5)에서 같은 입력을 돌려 얻은 실측 리터럴이다(어휘 미선언 뱅크).
"""
import json
import os
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import UPSTREAM_KEY, call, load_rest_fixture
from gateway import honesty, rules
from gateway.acl import ACL
from gateway.app import create_app
from gateway.audit import Audit
from gateway.bank_policies import BankPolicies

REPO_ROOT = os.path.dirname(os.path.dirname(__file__))
REPO_POLICY_FILE = os.path.join(REPO_ROOT, "tests", "fixtures", "policies", "sample", "bank-policies.json")
NOW = "2026-10-01T00:00:00Z"

_POLICIES = BankPolicies.from_file(REPO_POLICY_FILE)
DEMO = _POLICIES.for_bank("personal-demo")
ALICE = _POLICIES.for_bank("personal-alice")
TEAM = _POLICIES.for_bank("team-eng")
# #39: personal-demo 은 이제 kind 필수·닫힌 목록도 선언한다. 분리·정규화만 보던 테스트는 입력을 그대로 둔 채
# 그 두 선언만 걷어낸 사본으로 본다(원래 기대값 유지; 새 규칙은 tests/test_tag_closed_vocab.py 소유).
# #40: project 후보 해석 선언(tag_candidate_prefixes)도 같은 방식으로 걷어낸다(새 규칙은
# tests/test_tag_project_resolution.py 소유).
DEMO_SPLIT_ONLY = replace(DEMO, tag_required_prefixes=(), tag_closed_prefixes=(),
                        tag_candidate_prefixes=())
# 어휘를 포함해 선언을 전부 걷어낸 뱅크: 어휘가 분리의 유일한 스위치임을 보는 비교 기준.
DEMO_NO_DECLARATIONS = replace(DEMO_SPLIT_ONLY, tag_vocabulary={})

MERGED = "project:myall-shore, kind:gotcha, topic:blog"
SPACED = "kind:gotcha topic:orchestration"
GOOD = {"content": "[note] 분리 테스트"}


def tags_of(policy, tags):
    v = rules.evaluate_item(dict(GOOD, tags=tags), policy, NOW)
    return v, v.item["tags"]


# ============================ ① 재현: 어휘 선언 뱅크에서 분리 ============================

def test_comma_joined_tags_are_split_on_personal_demo():
    v, tags = tags_of(DEMO, [MERGED])
    assert v.ok
    assert tags == ["project:larch-yard", "kind:gotcha", "topic:blog"]


def test_space_joined_tags_are_split_on_personal_demo():
    v, tags = tags_of(DEMO, [SPACED])
    assert v.ok
    assert tags == ["kind:gotcha", "topic:orchestration"]


def test_tag_only_function_gives_the_same_split_as_evaluate_item():
    got = rules.evaluate_tags([MERGED, SPACED], DEMO)
    assert got.ok
    assert got.tags == ["project:larch-yard", "kind:gotcha", "topic:blog",
                        "topic:orchestration"]
    item = rules.evaluate_item(dict(GOOD, tags=[MERGED, SPACED]), DEMO, NOW)
    assert item.item["tags"] == got.tags and item.normalized_tags == got.normalized_tags


# ============================ ② 교정 공개: 한 입력 -> 여러 값 ============================

def test_a_split_is_disclosed_as_one_pair_per_stored_value_sharing_the_sent_string():
    v, _ = tags_of(DEMO, [MERGED])
    assert v.normalized_tags == (
        (MERGED, "project:larch-yard"),
        (MERGED, "kind:gotcha"),
        (MERGED, "topic:blog"),
    )


def test_a_piece_that_was_already_legal_is_still_disclosed_when_it_came_from_a_split():
    v, tags = tags_of(DEMO, [SPACED])
    assert tags == ["kind:gotcha", "topic:orchestration"]
    assert v.normalized_tags == ((SPACED, "kind:gotcha"), (SPACED, "topic:orchestration"))


def test_repeated_pieces_are_stored_and_disclosed_once():
    s = "kind:gotcha kind:gotcha"
    v, tags = tags_of(DEMO, [s])
    assert tags == ["kind:gotcha"]
    assert v.normalized_tags == ((s, "kind:gotcha"),)


def test_a_split_piece_does_not_duplicate_a_legal_tag_sent_next_to_it():
    v, tags = tags_of(DEMO, ["kind:gotcha topic:x", "kind:gotcha"])
    assert tags == ["kind:gotcha", "topic:x"]
    v, tags = tags_of(DEMO, ["kind:gotcha", "kind:gotcha topic:x"])
    assert tags == ["kind:gotcha", "topic:x"]


# ============================ ③ 분리 경계 ============================

@pytest.mark.parametrize("sent, stored", [
    ("kind:gotcha;topic:blog", ["kind:gotcha", "topic:blog"]),                 # 세미콜론
    ("kind:gotcha,topic:blog", ["kind:gotcha", "topic:blog"]),                 # 쉼표만
    ("kind:gotcha ,  topic:blog", ["kind:gotcha", "topic:blog"]),              # 섞인 구분자
    ("kind:gotcha\ttopic:blog", ["kind:gotcha", "topic:blog"]),                # 탭
    ("Kind:Gotcha, Topic:Blog", ["kind:gotcha", "topic:blog"]),                # 대소문자
    ("topic:Deploy Pipeline kind:fix", ["topic:deploy-pipeline", "kind:fix"]),  # 그 밖의 공백은 하이픈
    ("  kind:gotcha, topic:blog,  ", ["kind:gotcha", "topic:blog"]),           # 양끝 구분자
    ("kind:done, user:demo", ["kind:completion", "user:demo"]),                    # 조각마다 alias
    ("kind:gotcha, topic:a.b/c", ["kind:gotcha", "topic:a-b-c"]),              # 기계적 정규화 그대로
])
def test_split_points_and_per_piece_normalization(sent, stored):
    v, tags = tags_of(DEMO, [sent])
    assert v.ok and tags == stored


@pytest.mark.parametrize("sent, stored", [
    ("topic:foo-kind:bar", "topic:foo-kind-bar"),           # 구분자 바로 뒤가 아니다
    ("topic:foo,kind-x:bar", "topic:foo-kind-x-bar"),       # prefix 로 시작하되 콜론이 바로 오지 않음
    ("topic:a foo:b", "topic:a-foo-b"),                     # 선언되지 않은 prefix 는 경계가 아니다
    ("topic:a xkind:b", "topic:a-xkind-b"),                 # prefix 가 단어의 끝일 뿐
    ("topic:a kind :b", "topic:a-kind-b"),                  # 콜론 앞 공백
])
def test_non_boundaries_stay_inside_the_slug(sent, stored):
    v, tags = tags_of(DEMO_SPLIT_ONLY, [sent])
    assert v.ok and tags == [stored]


def test_a_colonless_legal_slug_is_never_split():
    merged_slug = "project:myall-shore-kind-gotcha-topic-blog"
    v, tags = tags_of(DEMO_SPLIT_ONLY, [merged_slug])
    assert v.ok and tags == [merged_slug] and v.normalized_tags == ()


def test_a_bare_word_before_a_prefixed_tag_is_split_off_as_its_own_piece():
    v, tags = tags_of(DEMO, ["skills kind:fix"])
    assert v.ok and tags == ["topic:skills", "kind:fix"]


def test_an_unfixable_piece_stays_a_violation_with_only_its_own_text():
    v, _ = tags_of(DEMO_SPLIT_ONLY, ["kind:한글, topic:blog"])
    assert not v.ok and v.rule_ids == ("RULE:tags-grammar",)
    assert "'kind:한글'" in v.message and "topic:blog" not in v.message


def test_a_newline_between_pieces_is_not_stored_inside_a_tag():
    v, tags = tags_of(DEMO, ["kind:gotcha\ntopic:blog"])
    assert v.ok and tags == ["kind:gotcha", "topic:blog"]
    assert all("\n" not in t for t in tags)


def test_leading_separators_do_not_become_an_empty_tag_violation():
    for sent in (", kind:gotcha, topic:blog", ";kind:gotcha;topic:blog"):
        v, tags = tags_of(DEMO, [sent])
        assert v.ok and tags == ["kind:gotcha", "topic:blog"]
        assert v.normalized_tags == ((sent, "kind:gotcha"), (sent, "topic:blog"))


def test_non_string_tags_are_left_for_the_grammar_check():
    v, tags = tags_of(DEMO, [5, "kind:gotcha topic:x"])
    assert not v.ok and tags == [5, "kind:gotcha", "topic:x"]


# ============================ ④ 어휘 미선언 뱅크는 그대로 ============================

@pytest.mark.parametrize("policy", [ALICE, TEAM], ids=["personal-alice", "team-eng"])
@pytest.mark.parametrize("sent, stored", [
    (MERGED, "project:myall-shore-kind-gotcha-topic-blog"),
    (SPACED, "kind:gotcha-topic-orchestration"),
    ("kind:gotcha;topic:blog", "kind:gotcha-topic-blog"),
])
def test_banks_without_a_vocabulary_keep_the_single_slug(policy, sent, stored):
    assert policy.tag_vocabulary == {}
    v, tags = tags_of(policy, [sent])
    assert v.ok and tags == [stored]
    assert v.normalized_tags == ((sent, stored),)


# 어휘를 걷어낸 personal-demo 는 분리하지 않는다: 분리의 유일한 스위치가 어휘 선언임을 고정.
def test_the_vocabulary_declaration_is_the_only_switch():
    bare = DEMO_NO_DECLARATIONS
    v, tags = tags_of(bare, [MERGED])
    assert tags == ["project:myall-shore-kind-gotcha-topic-blog"]


# 분리할 것이 없는 입력에서는 어휘 선언이 결과를 바꾸지 않는다(같은 정책, 어휘만 제거).
@pytest.mark.parametrize("tags", [
    ["project:hindsight-crew", "kind:decision"], ["skills"], ["Skills", "skills"],
    ["Project:HICKORY Bell"], ["project:quince", "topic:quince"], ["kind:done"],
    ["project:a", "project:a"], ["한글"], ["::"], [""], ["topic:a foo:b"],
    ["project:myall-shore-kind-gotcha-topic-blog"],
])
def test_inputs_with_nothing_to_split_are_unaffected_by_the_vocabulary(tags):
    bare = DEMO_NO_DECLARATIONS
    with_vocab = rules.evaluate_tags(list(tags), DEMO_SPLIT_ONLY)
    without = rules.evaluate_tags(list(tags), bare)
    assert with_vocab == without


# ============================ ⑤ 표면: MCP 와 REST ============================

DEMO_TOKEN = "tok-test-demo"        # 테스트 전용 리터럴. 실제 토큰 아님.
ACL_DEMO = {"tokens": {
    DEMO_TOKEN: {"identity": "demo", "personal": "personal-demo", "teams": ["team-eng"]},
    "tok-test-alice": {"identity": "alice", "personal": "personal-alice", "teams": []},
}}


@pytest.fixture
def surface(recorder, tmp_path):
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder.handler),
                               base_url="http://upstream", timeout=5.0)
    app = create_app(acl=ACL.from_dict(ACL_DEMO), audit=Audit(path=str(tmp_path / "audit.log")),
                     upstream_key=UPSTREAM_KEY, http_client=client,
                     bank_policies=BankPolicies.from_file(REPO_POLICY_FILE))
    app.state._audit_path = str(tmp_path / "audit.log")
    return TestClient(app)


def mcp_retain(client, bank, token, tags):
    return call(client, bank, token, name="sync_retain",
                arguments=dict(GOOD, tags=tags))


def rest_retain(client, bank, token, tags):
    item = dict(load_rest_fixture("memories_post_ok")["request"]["items"][0])
    item.pop("timestamp", None)
    item["tags"] = tags
    return client.post(f"/v1/default/banks/{bank}/memories",
                       content=json.dumps({"items": [item], "async": False}),
                       headers={"content-type": "application/json",
                                "authorization": f"Bearer {token}"})


@pytest.mark.parametrize("sent, stored", [
    (MERGED, ["project:larch-yard", "kind:gotcha", "topic:blog"]),
    (SPACED, ["kind:gotcha", "topic:orchestration"]),
])
def test_mcp_retain_forwards_the_split_tags_on_personal_demo(surface, recorder, sent, stored):
    r = mcp_retain(surface, "personal-demo", DEMO_TOKEN, [sent])
    assert r.status_code == 200 and recorder.calls
    assert recorder.last["body"]["params"]["arguments"]["tags"] == stored


@pytest.mark.parametrize("sent, stored", [
    (MERGED, ["project:larch-yard", "kind:gotcha", "topic:blog"]),
    (SPACED, ["kind:gotcha", "topic:orchestration"]),
])
def test_rest_items_forward_the_split_tags_and_disclose_each_piece(surface, recorder, sent, stored):
    r = rest_retain(surface, "personal-demo", DEMO_TOKEN, [sent])
    assert r.status_code == 200
    assert recorder.last["body"]["items"][0]["tags"] == stored
    assert r.json()["gateway_normalized_tags"] == [
        {"index": 0, "rewrites": [{"sent": sent, "stored": s} for s in stored]}
    ]


def test_mcp_split_is_audited_as_a_normalization(surface):
    mcp_retain(surface, "personal-demo", DEMO_TOKEN, [MERGED])
    with open(surface.app.state._audit_path, encoding="utf-8") as fh:
        decisions = [json.loads(ln)["decision"] for ln in fh if ln.strip()]
    assert "enforce-normalize" in decisions


def test_personal_alice_surfaces_keep_the_merged_slug(surface, recorder):
    mcp_retain(surface, "personal-alice", "tok-test-alice", [MERGED])
    assert recorder.last["body"]["params"]["arguments"]["tags"] == [
        "project:myall-shore-kind-gotcha-topic-blog"]
    r = rest_retain(surface, "personal-alice", "tok-test-alice", [SPACED])
    assert r.status_code == 200
    assert recorder.last["body"]["items"][0]["tags"] == ["kind:gotcha-topic-orchestration"]
    assert r.json()["gateway_normalized_tags"] == [
        {"index": 0, "rewrites": [{"sent": SPACED, "stored": "kind:gotcha-topic-orchestration"}]}
    ]


# ============================ ⑥ MCP 교정 노트: 같은 sent 묶음 ============================

def _note(pairs):
    return honesty.normalized_tag_note(3, [{"id": 3, "tags": pairs}])


def test_the_correction_note_folds_a_split_into_one_entry():
    v, _ = tags_of(DEMO, [MERGED])
    assert _note(v.normalized_tags) == (
        f"{MERGED} -> project:larch-yard, kind:gotcha, topic:blog")


def test_the_note_of_unsplit_rewrites_is_unchanged():
    assert _note((("skills", "topic:skills"), ("Kind:Fix", "kind:fix"))) == (
        "skills -> topic:skills, Kind:Fix -> kind:fix")
    # 같은 값을 두 번 보낸 경우의 동일 쌍은 예전처럼 따로 적힌다.
    assert _note((("skills", "topic:skills"), ("skills", "topic:skills"))) == (
        "skills -> topic:skills, skills -> topic:skills")
