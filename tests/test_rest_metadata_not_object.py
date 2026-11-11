"""REST items[].metadata 비 dict 거부 (issue #73).

#70은 MCP retain 계열의 비 dict `metadata`를 막았지만, REST `POST .../memories`의 items는
그 가드를 거치지 않았다. 팀 뱅크 attribution의 `dict(it.get("metadata") or {})`가
`["a"]`에서 ValueError(500)를 내고 `["ab"]`를 `{"a": "b"}`로 조용히 바꿨다.

계약: 두 표면이 같은 판정을 쓴다(P1). metadata 형식 판정은 `rules.evaluate_item`에 있고,
REST는 기존 422 `rule-violation` 채널로, MCP는 isError 채널로 같은 `RULE:metadata-not-object`를 낸다.
null/누락/dict metadata는 기존 동작 그대로다.
"""
import pytest
from fastapi.testclient import TestClient

from gateway import rules
from gateway.bank_policies import BankPolicy

from test_rest_rules import (STRICT_BANK, TEAM_BANK, body, decisions, forwarded_items, item,
                             rest_post, strict_app)

RULE = "RULE:metadata-not-object"
NOW = "2026-10-02T00:00:00Z"
NON_DICT_METADATA = [["a"], ["ab"], "x", 5, True, [], 0, False, ""]


@pytest.mark.parametrize("metadata", NON_DICT_METADATA, ids=repr)
def test_non_dict_metadata_on_rest_team_bank_is_422_not_500(make_app, recorder, metadata):
    c = TestClient(make_app(attribution_enabled=True))
    r = rest_post(c, TEAM_BANK, "tok-alice", body(item(metadata=metadata)))

    assert r.status_code == 422
    data = r.json()
    assert data["error"] == "rule-violation"
    assert data["rule"] == RULE
    assert data["rule_detail"][0]["index"] == 0
    assert RULE in data["rule_detail"][0]["rules"]
    assert recorder.calls == []                      # ["ab"]가 {"a": "b"}로 새어 나가지 않는다
    assert "enforce-reject" in decisions(c)


def test_non_dict_metadata_on_rest_strict_personal_bank_is_422(make_app, recorder):
    c = TestClient(strict_app(make_app))
    r = rest_post(c, STRICT_BANK, "tok-alice", body(item(), item(metadata=["a"])))

    assert r.status_code == 422
    detail = r.json()["rule_detail"]
    assert [d["index"] for d in detail] == [1]       # 위반 item만 지목
    assert detail[0]["rules"] == [RULE]
    assert recorder.calls == []


@pytest.mark.parametrize("metadata", [None, {}, {"k": "v"}], ids=repr)
def test_null_or_dict_metadata_on_rest_team_bank_keeps_attribution(make_app, recorder, metadata):
    c = TestClient(make_app(attribution_enabled=True))
    r = rest_post(c, TEAM_BANK, "tok-alice", body(item(metadata=metadata)))

    assert r.status_code == 200
    md = forwarded_items(recorder)[0]["metadata"]
    assert md["hc_member"] == "alice"
    assert {k: v for k, v in md.items() if k != "hc_member"} == (metadata or {})


def test_mcp_and_rest_share_one_metadata_verdict():
    """같은 item을 두 입구에 넣으면 같은 규칙 id가 나온다."""
    it = {"content": "x", "metadata": ["a"]}
    assert RULE in rules.evaluate_item(it, BankPolicy(), NOW).rule_ids
    assert RULE in rules.evaluate_tool_call("retain", it, BankPolicy(), NOW).rule_ids
