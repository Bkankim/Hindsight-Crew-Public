"""이슈 #458 (c): REST recall에도 recall_max_tokens_floor가 MCP와 같은 규칙 id로 적용된다.

전에는 같은 뱅크·같은 `max_tokens=1`에서 MCP는 RULE:recall-max-tokens-floor로 거부, REST는
upstream으로 넘겼다. 두 표면 x max_tokens 값 행렬로 판정이 같음을 고정한다.
"""
import json

import pytest
from fastapi.testclient import TestClient

from gateway import rules
from gateway.bank_policies import BankPolicies

BANK = "personal-alice"
HEADERS = {"authorization": "Bearer tok-alice", "content-type": "application/json"}

# (max_tokens 값 또는 생략, 거부 여부) - 기본 정책 floor 4096 기준
CASES = [
    pytest.param(1, True, id="one"),
    pytest.param(4095, True, id="floor-minus-1"),
    pytest.param(4095.5, True, id="float-below"),
    pytest.param(4096, False, id="floor"),
    pytest.param(8192, False, id="above"),
    pytest.param("omit", False, id="omitted"),
    pytest.param("128", False, id="string-left-to-upstream"),
    pytest.param(True, False, id="bool-not-a-budget"),
]


def _args(value):
    args = {"query": "floor probe"}
    if value != "omit":
        args["max_tokens"] = value
    return args


def _call(client, surface, args):
    if surface == "mcp":
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "recall", "arguments": args}}
        return client.post(f"/mcp/{BANK}/", content=json.dumps(body), headers=HEADERS)
    return client.post(f"/v1/default/banks/{BANK}/memories/recall", content=json.dumps(args), headers=HEADERS)


def _rejected_rule(resp, surface):
    """거부면 규칙 id, 아니면 None."""
    if surface == "rest":
        if resp.status_code != 422:
            return None
        data = resp.json()
        assert data["error"] == "rule-violation"
        assert data["rule_detail"][0]["message"].startswith(data["rule"])
        return data["rule"]
    data = resp.json()
    result = data.get("result") or {}
    if not result.get("isError"):
        return None
    return result["content"][0]["text"].split(" ", 1)[0]


@pytest.mark.parametrize("surface", ["mcp", "rest"])
@pytest.mark.parametrize("value, rejected", CASES)
def test_recall_floor_same_verdict_on_both_surfaces(make_app, recorder, surface, value, rejected):
    c = TestClient(make_app())
    resp = _call(c, surface, _args(value))
    rule = _rejected_rule(resp, surface)
    if rejected:
        assert rule == rules.RULE_RECALL_MAX_TOKENS_FLOOR, resp.text
        assert recorder.calls == []
    else:
        assert rule is None, resp.text
        assert len(recorder.calls) == 1


@pytest.mark.parametrize("surface", ["mcp", "rest"])
def test_floor_zero_bank_lets_small_budget_through_on_both_surfaces(make_app, recorder, surface):
    policies = BankPolicies.from_dict({"defaults": {"recall_max_tokens_floor": 0}})
    c = TestClient(make_app(bank_policies=policies))
    resp = _call(c, surface, _args(1))
    assert _rejected_rule(resp, surface) is None
    assert len(recorder.calls) == 1


def test_rest_floor_reject_is_audited(make_app, recorder):
    app = make_app()
    c = TestClient(app)
    assert _call(c, "rest", _args(1)).status_code == 422
    with open(app.state._audit_path, encoding="utf-8") as fh:
        entries = [json.loads(ln) for ln in fh if ln.strip()]
    rejects = [e for e in entries if e["decision"] == "enforce-reject"]
    assert len(rejects) == 1
    assert rejects[0]["reason"] == "rule-violation:" + rules.RULE_RECALL_MAX_TOKENS_FLOOR
    # final-review C1: no `allow` line for a rejected REST recall (duplicate_recall.py would keep it
    # as an arrived recall: its REST branch keeps `allow` lines and drops rejects).
    assert [e for e in entries if e["decision"] == "allow"] == []


@pytest.mark.parametrize("raw", [
    pytest.param('{"query": "x", "max_tokens": ' + "9" * 5000 + "}", id="int-past-max-str-digits"),
    pytest.param("[" * 100000 + "]" * 100000, id="nesting-past-recursion-limit"),
])
def test_rest_recall_body_the_guard_cannot_parse_is_forwarded_not_500(make_app, recorder, raw):
    """final-review C4: a body json.loads rejects with plain ValueError/RecursionError is forwarded as
    before the floor check existed (upstream owns its shape), never an unhandled 500."""
    import httpx

    def handler(request):          # upstream answers such a body with a 4xx, as the real one does
        # (the shared Recorder would json.loads the body itself and hit the same ValueError)
        recorder.calls.append({"path": request.url.path})
        return httpx.Response(422, json={"detail": "bad body"})

    recorder.handler = handler
    c = TestClient(make_app())
    resp = c.post(f"/v1/default/banks/{BANK}/memories/recall", content=raw, headers=HEADERS)
    assert resp.status_code == 422
    assert len(recorder.calls) == 1
