"""S2 retain 게이트 작업 키 경고·거부 (issue #364, 아키텍처 v2 런 5 R5-B2, 플래그 `HC_RETAIN_ITEM_KEY` 기본 off).

검사 대상(모두 `rules.evaluate_item` 한 곳에서 판정해 MCP·REST 가 같은 결과를 낸다):
  * 보낸 태그에 `kind:todo` (템플릿 별칭이 `kind:decision` 으로 바꾸므로 보낸 태그로 본다)
  * 교정 뒤 `kind:handoff` 또는 `kind:completion` (`kind:done` -> `kind:completion` 별칭 포함)
  * 교정 뒤 `kind:decision` 이면서 맨 앞 라벨이 보류 계열(`item_keys` 규칙)
`extract_keys` 결과가 비면 warn 은 경고만, reject 는 `RULE:item-key-required` 로 거부. off 는 동작 불변.
"""
import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import UPSTREAM_KEY, call
from gateway import retain_item_key, rules
from gateway.acl import ACL
from gateway.app import create_app
from gateway.audit import Audit
from gateway.bank_policies import BankPolicies

REPO_ROOT = os.path.dirname(os.path.dirname(__file__))
POLICY_FILE = os.path.join(REPO_ROOT, "tests", "fixtures", "policies", "sample", "bank-policies.json")
DEMO = BankPolicies.from_file(POLICY_FILE).for_bank("personal-demo")
NOW = "2026-11-01T00:00:00Z"
FLAG = "HC_RETAIN_ITEM_KEY"
RULE = "RULE:item-key-required"
DEMO_TOKEN = "tok-test-demo"        # 테스트 전용 리터럴. 실제 토큰 아님.
ACL_DEMO = {"tokens": {DEMO_TOKEN: {"identity": "demo", "personal": "personal-demo", "teams": ["team-eng"]}}}


@pytest.fixture(autouse=True)
def _flag_unset(monkeypatch):
    """셸에서 켠 값이 새어 들어오지 않게 한다. 켜야 하는 테스트가 직접 설정한다."""
    monkeypatch.delenv(FLAG, raising=False)


# 검사 대상 네 경우: (이름, 보낸 태그, 키 없는 본문)
TARGETS = [
    ("sent-todo", ["project:hindsight-crew", "kind:todo"], "[메모] 다음 세션에 이어서 할 것"),
    ("handoff", ["project:hindsight-crew", "kind:handoff"], "[인계] 다음 세션이 이어서 진행"),
    ("done-alias", ["project:hindsight-crew", "kind:done"], "[완료] 작업을 끝냈다"),
    ("pending-decision", ["project:hindsight-crew", "kind:decision"], "[보류] 운영 반영은 승인 창까지 미룸"),
]
# 대상이 아닌 kind(키가 없어도 영향 없음)
OTHERS = [
    ("fix", ["project:hindsight-crew", "kind:fix"], "[수정] 정규식 경계를 고쳤다"),
    ("plain-decision", ["project:hindsight-crew", "kind:decision"], "[결정] 기본값은 꺼짐으로 둔다"),
    ("resolved-decision", ["project:hindsight-crew", "kind:decision"], "[보류 해제] 운영 반영 진행"),
    ("status", ["project:hindsight-crew", "kind:status"], "[상태] 사본 기동 중"),
]
KEY_SUFFIX = " #364"            # project 태그가 하나라 번호만 규칙으로 (hindsight-crew, 364) 키가 된다
SURFACES = ("mcp", "rest")


def _content(base, keyed):
    return base + KEY_SUFFIX if keyed else base


# --- 단위: 판정 함수 -------------------------------------------------------------------------

def _verdict(tags, content, mode=None, monkeypatch=None):
    if mode is not None:
        monkeypatch.setenv(FLAG, mode)
    return rules.evaluate_item({"content": content, "tags": tags}, DEMO, NOW)


@pytest.mark.parametrize("name,tags,content", TARGETS, ids=[t[0] for t in TARGETS])
@pytest.mark.parametrize("keyed", [False, True], ids=["nokey", "key"])
def test_unit_warn_only_when_key_missing(monkeypatch, name, tags, content, keyed):
    v = _verdict(tags, _content(content, keyed), "warn", monkeypatch)
    assert v.ok
    hits = [w for w in v.warnings if RULE in w]
    assert len(hits) == (0 if keyed else 1)


@pytest.mark.parametrize("name,tags,content", TARGETS, ids=[t[0] for t in TARGETS])
@pytest.mark.parametrize("keyed", [False, True], ids=["nokey", "key"])
def test_unit_reject_only_when_key_missing(monkeypatch, name, tags, content, keyed):
    v = _verdict(tags, _content(content, keyed), "reject", monkeypatch)
    assert not any(RULE in w for w in v.warnings)
    if keyed:
        assert v.ok
    else:
        assert v.rule_ids == (RULE,)
        assert v.message.startswith(RULE) and "Valid example" in v.message


@pytest.mark.parametrize("mode", ["warn", "reject"])
@pytest.mark.parametrize("name,tags,content", OTHERS, ids=[t[0] for t in OTHERS])
def test_unit_other_kinds_unaffected(monkeypatch, mode, name, tags, content):
    v = _verdict(tags, content, mode, monkeypatch)
    assert v.ok and not any(RULE in w for w in v.warnings)


@pytest.mark.parametrize("mode", [None, "off", "OFF", " off ", "", "0", "bogus"])
@pytest.mark.parametrize("name,tags,content", TARGETS, ids=[t[0] for t in TARGETS])
def test_unit_off_is_inert(monkeypatch, mode, name, tags, content):
    v = _verdict(tags, content, mode, monkeypatch)
    assert v.ok and not any(RULE in w for w in v.warnings)
    assert retain_item_key.mode() == "off"


def test_alias_is_applied_before_the_decision(monkeypatch):
    """`kind:done` 은 교정 뒤 `kind:completion` 으로 판정된다(교정이 먼저, 판정이 뒤)."""
    monkeypatch.setenv(FLAG, "reject")
    v = rules.evaluate_item({"content": "[완료] 끝냄", "tags": ["project:hindsight-crew", "kind:done"]}, DEMO, NOW)
    assert ("kind:done", "kind:completion") in v.normalized_tags
    assert "kind:completion" in v.item["tags"] and v.rule_ids == (RULE,)
    assert "kind:completion" in v.message


def test_sent_todo_is_judged_on_the_sent_tag_even_though_it_is_stored_as_decision(monkeypatch):
    monkeypatch.setenv(FLAG, "reject")
    v = rules.evaluate_item({"content": "[메모] 할 것", "tags": ["project:hindsight-crew", "kind:todo"]}, DEMO, NOW)
    assert "kind:decision" in v.item["tags"] and "kind:todo" not in v.item["tags"]
    assert v.rule_ids == (RULE,) and "kind:todo" in v.message


def test_an_aliased_decision_with_a_pending_label_is_a_target(monkeypatch):
    """`kind:plan` -> `kind:decision` 교정 뒤 보류 라벨이면 대상이다."""
    monkeypatch.setenv(FLAG, "reject")
    v = rules.evaluate_item({"content": "[대기] 승인 대기", "tags": ["project:hindsight-crew", "kind:plan"]}, DEMO, NOW)
    assert v.rule_ids == (RULE,)


def test_the_item_tag_form_counts_and_is_advertised_only_where_the_bank_allows_it(monkeypatch):
    """fixture 뱅크는 `item` 접두어를 선언하지 않아 `item:` 태그 자체가 RULE:tags-grammar 다. 그런 뱅크에는
    `item:` 표기를 안내하지 않고(거부될 재시도를 가르치지 않는다), 선언한 뱅크(템플릿)에서는 키로 치고 안내한다."""
    from dataclasses import replace
    monkeypatch.setenv(FLAG, "reject")
    with_item = replace(DEMO, tag_prefixes=DEMO.tag_prefixes + ("item",))
    tagged = {"content": "[인계] 이어서 진행",
              "tags": ["project:hindsight-crew", "kind:handoff", "item:hindsight-crew-364"]}
    assert rules.evaluate_item(tagged, with_item, NOW).ok
    assert rules.RULE_TAGS_GRAMMAR in rules.evaluate_item(tagged, DEMO, NOW).rule_ids
    bare = {"content": "[인계] 이어서 진행", "tags": ["project:hindsight-crew", "kind:handoff"]}
    assert '"item:<repo>-<n>"' in rules.evaluate_item(bare, with_item, NOW).message
    assert "item:" not in rules.evaluate_item(bare, DEMO, NOW).message
    monkeypatch.setenv(FLAG, "warn")
    assert "item:hindsight-crew-<번호>" in rules.evaluate_item(bare, with_item, NOW).warnings[-1]
    assert "item:" not in rules.evaluate_item(bare, DEMO, NOW).warnings[-1]


@pytest.mark.parametrize("content,tags", [
    ("[인계] octo/hindsight-crew#364 이어서", ["kind:handoff"]),
    ("[인계] hindsight-crew#364 이어서", ["project:hindsight-crew", "kind:handoff", "project:other"]),
])
def test_every_extract_keys_form_counts_as_a_key(monkeypatch, content, tags):
    monkeypatch.setenv(FLAG, "reject")
    assert rules.evaluate_item({"content": content, "tags": tags}, DEMO, NOW).ok


@pytest.mark.parametrize("content,tags", [
    ("[인계] 이어서 진행 #364", ["kind:handoff"]),                                       # project 없음
    ("[인계] #364 이어서", ["project:hindsight-crew", "project:other", "kind:handoff"]),  # project 둘
    ("[인계] https://github.com/o/r/issues/364 참고", ["project:hindsight-crew", "kind:handoff"]),
])
def test_what_extract_keys_rejects_is_not_a_key(monkeypatch, content, tags):
    monkeypatch.setenv(FLAG, "reject")
    assert rules.evaluate_item({"content": content, "tags": tags}, DEMO, NOW).rule_ids == (RULE,)


def test_bank_item_repo_aliases_are_used(monkeypatch):
    from dataclasses import replace
    monkeypatch.setenv(FLAG, "reject")
    pol = replace(DEMO, item_repo_aliases={"hc": "hindsight-crew"})
    item = {"content": "[인계] HC#364 이어서", "tags": ["kind:handoff"]}
    assert rules.evaluate_item(item, pol, NOW).ok
    assert rules.evaluate_item(item, DEMO, NOW).rule_ids == (RULE,)


def test_reject_keeps_other_violations_and_their_ids(monkeypatch):
    monkeypatch.setenv(FLAG, "reject")
    v = rules.evaluate_item({"content": "라벨 없음", "tags": ["project:hindsight-crew", "kind:handoff"]}, DEMO, NOW)
    assert v.rule_ids == (rules.RULE_CONTENT_PREFIX, RULE)


def test_warn_keeps_existing_tag_warnings(monkeypatch):
    monkeypatch.setenv(FLAG, "warn")
    v = rules.evaluate_item({"content": "[인계] 이어서", "tags": ["project:hemlock-loom-v2", "kind:handoff"]}, DEMO, NOW)
    assert len(v.warnings) == 2 and RULE in v.warnings[-1] and "비슷합니다" in v.warnings[0]


@pytest.mark.parametrize("tags", [
    ["project:hindsight-crew", "kind: todo"],                 # 콜론 뒤 공백
    ["project:hindsight-crew", " Kind : TODO "],              # 대소문자·공백
    ["kind:todo,project:hindsight-crew"],                     # 쉼표로 붙여 보낸 한 문자열
    ["kind:todo project:hindsight-crew"],                     # 공백으로 붙여 보낸 한 문자열
], ids=["colon-space", "case-space", "comma-attached", "space-attached"])
def test_mechanically_fixed_todo_spellings_are_judged_as_todo(monkeypatch, tags):
    """게이트웨이가 기계 교정으로 kind:todo 로 만든 뒤 별칭으로 kind:decision 이 되는 표기도 보낸 todo 다
    (final-review #364-1 #1). 라벨이 보류 계열이 아니어도 대상이다."""
    monkeypatch.setenv(FLAG, "reject")
    v = rules.evaluate_item({"content": "[메모] 다음에 할 것", "tags": tags}, DEMO, NOW)
    assert "kind:decision" in v.item["tags"]
    assert v.rule_ids == (RULE,) and "kind:todo" in v.message


def test_an_item_tag_the_bank_rejects_is_not_counted_as_a_key(monkeypatch):
    """`item` 접두어를 선언하지 않은 뱅크에서는 그 태그가 같은 판정에서 tags-grammar 로 거부된다. 그 태그를 키로
    치면 S2 가 숨었다가 태그를 뺀 다음 시도에서야 나온다(final-review #364-1 #2). 두 위반이 한 번에 나와야 한다."""
    monkeypatch.setenv(FLAG, "reject")
    v = rules.evaluate_item({"content": "[인계] 이어서 진행",
                             "tags": ["project:hindsight-crew", "kind:handoff", "item:hindsight-crew-364"]}, DEMO, NOW)
    assert v.rule_ids == (rules.RULE_TAGS_GRAMMAR, RULE)


def test_the_guidance_names_only_what_extract_keys_reads(monkeypatch):
    """안내는 `<repo>#<n>` 의 repo 가 이 기억의 project 이름이어야 한다고 말한다. 아무 낱말이나 repo 로 치지 않는
    extract_keys 와 맞아야, 안내를 따른 재시도가 다시 거부되지 않는다(final-review #364-1 #3)."""
    monkeypatch.setenv(FLAG, "reject")
    tags = ["project:hindsight-crew", "kind:handoff"]
    msg = rules.evaluate_item({"content": "[인계] 이어서", "tags": tags}, DEMO, NOW).message
    assert "where <repo> is this memory's own project: tag name" in msg
    assert 'another word before "#" is not read as a repo' in msg
    assert rules.evaluate_item({"content": "[인계] linden#12 이어서", "tags": tags}, DEMO, NOW).rule_ids == (RULE,)
    assert rules.evaluate_item({"content": "[인계] hindsight-crew#12 이어서", "tags": tags}, DEMO, NOW).ok
    monkeypatch.setenv(FLAG, "warn")
    warning = rules.evaluate_item({"content": "[인계] 이어서", "tags": tags}, DEMO, NOW).warnings[-1]
    assert '"hindsight-crew#<번호>"(이 기억의 project 태그 이름)' in warning and "다른 낱말 뒤" in warning


@pytest.mark.parametrize("tags,content,kind", [
    (["project:hindsight-crew", "kind:handoff"], "[인계] 이어서", "kind:handoff"),
    (["project:hindsight-crew", "kind:done"], "[완료] 끝냄", "kind:completion"),
    (["project:hindsight-crew", "kind:todo"], "[메모] 할 것", "kind:decision"),       # 별칭 교정 결과
    (["project:hindsight-crew", "kind:decision"], "[보류] 미룸", "kind:decision"),
    (["kind:done"], "[완료] 끝냄", "kind:completion"),                                 # project 없음
], ids=["handoff", "done-alias", "todo-alias", "pending-decision", "no-project"])
def test_the_valid_example_keeps_the_trigger_kind_and_passes_every_rule(monkeypatch, tags, content, kind):
    """거부 문구의 Valid example 은 계기 kind(교정 결과)를 쓰고, 그대로 다시 보내면 모든 규칙을 통과한다
    (final-review #364-1 #5: 완료 기억이 예시를 베껴 handoff 로 저장되는 극성 뒤집힘을 막는다)."""
    import re
    monkeypatch.setenv(FLAG, "reject")
    msg = rules.evaluate_item({"content": content, "tags": tags}, DEMO, NOW).message
    m = re.search(r'Valid example: "content": "(.*?)", "tags": (\[.*?\])\.', msg)
    assert m, msg
    example = {"content": m.group(1), "tags": json.loads(m.group(2))}
    assert kind in example["tags"]
    redo = rules.evaluate_item(example, DEMO, NOW)
    assert redo.ok and not redo.normalized_tags and kind in redo.item["tags"]


def test_existing_rule_ids_are_unchanged():
    assert (rules.RULE_TAGS_REQUIRED, rules.RULE_TAGS_GRAMMAR, rules.RULE_TAGS_VOCABULARY,
            rules.RULE_TAGS_MISSING_PREFIX, rules.RULE_CONTENT_PREFIX, rules.RULE_STRATEGY_UNKNOWN) == (
        "RULE:tags-required", "RULE:tags-grammar", "RULE:tags-vocabulary",
        "RULE:tags-missing-prefix", "RULE:content-prefix", "RULE:strategy-unknown")
    assert rules.RULE_ITEM_KEY_REQUIRED == RULE


def test_compose_wires_the_flag_default_off():
    with open(os.path.join(REPO_ROOT, "docker-compose.yml"), encoding="utf-8") as fh:
        assert "      HC_RETAIN_ITEM_KEY: ${HC_RETAIN_ITEM_KEY:-off}\n" in fh.read()


# --- 표면: MCP·REST 행렬 -------------------------------------------------------------------

@pytest.fixture
def surface(recorder, tmp_path, monkeypatch):
    monkeypatch.setattr(rules, "utc_now_iso", lambda: NOW)   # 응답 바이트 비교를 위해 시각 고정
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder.handler),
                               base_url="http://upstream", timeout=5.0)
    app = create_app(acl=ACL.from_dict(ACL_DEMO), audit=Audit(path=str(tmp_path / "audit.log")),
                     upstream_key=UPSTREAM_KEY, http_client=client,
                     bank_policies=BankPolicies.from_file(POLICY_FILE))
    return TestClient(app)


def _send(client, which, tags, content):
    if which == "mcp":
        return call(client, "personal-demo", DEMO_TOKEN, name="sync_retain",
                    arguments={"content": content, "tags": tags})
    return client.post("/v1/default/banks/personal-demo/memories",
                       content=json.dumps({"items": [{"content": content, "tags": tags}], "async": False}),
                       headers={"content-type": "application/json", "authorization": f"Bearer {DEMO_TOKEN}"})


def _retains(recorder):
    """upstream 으로 전달된 retain 만(모순 게이트가 decision·fix 앞에 부르는 recall 은 뺀다)."""
    return [c for c in recorder.calls if not c["path"].endswith("/recall")]


def _mcp_text(r):
    return "\n".join(c.get("text", "") for c in r.json()["result"]["content"])


def _item_key_warnings(r, which):
    if which == "mcp":
        return [ln for ln in _mcp_text(r).splitlines() if RULE in ln]
    return [w["text"] for w in r.json().get("gateway_warnings", []) if RULE in w["text"]]


@pytest.mark.parametrize("which", SURFACES)
@pytest.mark.parametrize("keyed", [False, True], ids=["nokey", "key"])
@pytest.mark.parametrize("name,tags,content", TARGETS, ids=[t[0] for t in TARGETS])
def test_surface_warn_matrix(surface, recorder, monkeypatch, which, keyed, name, tags, content):
    monkeypatch.setenv(FLAG, "warn")
    r = _send(surface, which, tags, _content(content, keyed))
    assert r.status_code == 200 and len(_retains(recorder)) == 1          # 경고만, 저장은 진행
    if which == "mcp":
        assert r.json()["result"].get("isError") is not True
    assert len(_item_key_warnings(r, which)) == (0 if keyed else 1)


@pytest.mark.parametrize("which", SURFACES)
@pytest.mark.parametrize("keyed", [False, True], ids=["nokey", "key"])
@pytest.mark.parametrize("name,tags,content", TARGETS, ids=[t[0] for t in TARGETS])
def test_surface_reject_matrix(surface, recorder, monkeypatch, which, keyed, name, tags, content):
    monkeypatch.setenv(FLAG, "reject")
    r = _send(surface, which, tags, _content(content, keyed))
    if keyed:
        assert r.status_code == 200 and len(_retains(recorder)) == 1
        if which == "mcp":
            assert r.json()["result"].get("isError") is not True
        assert _item_key_warnings(r, which) == []
        return
    assert recorder.calls == []                                       # 거부는 upstream 에 아무것도 보내지 않는다
    if which == "mcp":
        assert r.status_code == 200
        res = r.json()["result"]
        assert res["isError"] is True and _mcp_text(r).startswith(RULE)
    else:
        assert r.status_code == 422
        body = r.json()
        assert body["error"] == "rule-violation" and body["rule"] == RULE
        assert body["rule_detail"][0]["rules"] == [RULE]


@pytest.mark.parametrize("which", SURFACES)
@pytest.mark.parametrize("mode", ["warn", "reject"])
@pytest.mark.parametrize("name,tags,content", OTHERS, ids=[t[0] for t in OTHERS])
def test_surface_other_kinds_unaffected(surface, recorder, monkeypatch, which, mode, name, tags, content):
    monkeypatch.setenv(FLAG, mode)
    r = _send(surface, which, tags, content)
    assert r.status_code == 200 and len(_retains(recorder)) == 1
    assert _item_key_warnings(r, which) == []


@pytest.mark.parametrize("which", SURFACES)
@pytest.mark.parametrize("mode", [None, "off", "OFF", "", "bogus"])
@pytest.mark.parametrize("name,tags,content", TARGETS + OTHERS, ids=[t[0] for t in TARGETS + OTHERS])
def test_surface_off_is_byte_identical_to_no_s2(surface, recorder, monkeypatch, which, mode, name, tags, content):
    """off(기본·대소문자·빈 값·모르는 값)는 S2 훅을 아예 빼 버린 게이트웨이와 응답·전달 본문이 같다."""
    with monkeypatch.context() as m:
        m.setattr(rules, "_item_key_rule", lambda sent, out, policy: ((), ()))
        base = _send(surface, which, tags, content)
    base_fwd = _retains(recorder)[-1]["body"]
    if mode is not None:
        monkeypatch.setenv(FLAG, mode)
    r = _send(surface, which, tags, content)
    assert (r.status_code, r.content) == (base.status_code, base.content)
    assert _retains(recorder)[-1]["body"] == base_fwd
    assert len(_retains(recorder)) == 2
