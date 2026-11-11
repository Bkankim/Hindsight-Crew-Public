"""이슈 #80: recall 원장 줄에 `budget`·`max_tokens`가 남는다.

2026-10-02 분석에서 같은 쿼리의 결과가 일부만 겹친 34건이 인자 차이 때문인지 가를 수 없었다.
원장이 요청에 실린 두 인자를 그대로 남기면 같은 요청인지 확정할 수 있다. 요청에 없으면 null이다
(게이트웨이가 upstream 기본값을 추측해 쓰지 않는다). 공개 경계 = 게이트웨이 요청 뒤 원장 줄.
"""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.acl import ACL
from gateway.app import create_app
from gateway.audit import Audit
from gateway.recurrence import RecallLedger

from conftest import ACL_DATA, UPSTREAM_KEY

BANK = "personal-alice"
QUERY = "recall 원장 인자 기록 확인용 쿼리"
SURFACES = ["mcp", "rest"]

# (요청에 실린 인자, 원장에 기대하는 (budget, max_tokens))
CASES = [
    pytest.param({"budget": "mid", "max_tokens": 8192}, ("mid", 8192), id="both"),
    pytest.param({"budget": "low"}, ("low", None), id="budget-only"),
    pytest.param({"max_tokens": 5000}, (None, 5000), id="max-tokens-only"),
    pytest.param({}, (None, None), id="neither"),
]

SECRET_LIKE = "sk-" + "a1" * 12          # 시크릿 형태 가짜 값 (query와 같은 여과 대상)

# upstream이 강제 변환해 성공시킬 수 있는 값은 받은 그대로 남긴다. null은 "요청에 없음 또는 기록하지
# 않는 값"(명시 null, 비스칼라, 상한 초과)이고 둘을 구분하지 않는다. 문자열은 여과·절단한다.
ODD_CASES = [
    pytest.param({"max_tokens": "4096"}, (None, "4096"), id="numeric-string-kept"),
    pytest.param({"budget": ["low"], "max_tokens": {"n": 1}}, (None, None), id="non-scalar-null"),
    pytest.param({"budget": None, "max_tokens": None}, (None, None), id="explicit-null"),
    pytest.param({"budget": SECRET_LIKE}, ("[redacted]", None), id="secret-like-string-scrubbed"),
    pytest.param({"budget": "x" * 500, "max_tokens": "9" * 500}, ("x" * 64, "9" * 64),
                 id="long-string-truncated"),
    pytest.param({"max_tokens": 2 ** 53}, (None, 2 ** 53), id="int-at-cap-kept"),
    pytest.param({"max_tokens": 2 ** 53 + 1}, (None, None), id="int-over-cap-null"),
    pytest.param({"max_tokens": 10 ** 400}, (None, None), id="huge-int-null"),
]


def _sse(payload):
    return b"event: message\r\ndata: " + json.dumps(payload).encode("utf-8") + b"\r\n\r\n"


def _upstream(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content or b"{}")
    results = {"results": [{"id": "mem-1", "text": "x"}]}
    if request.url.path.endswith("/memories/recall"):
        return httpx.Response(200, json=results)
    encoded = json.dumps(results)
    result = {"content": [{"type": "text", "text": encoded}],
              "structuredContent": {"result": encoded}, "isError": False}
    return httpx.Response(200, content=_sse({"jsonrpc": "2.0", "id": body.get("id"), "result": result}),
                          headers={"content-type": "text/event-stream", "mcp-session-id": "s1"})


@pytest.fixture
def gateway(tmp_path):
    ledger_path = tmp_path / "ledger.jsonl"
    http = httpx.AsyncClient(transport=httpx.MockTransport(_upstream),
                             base_url="http://upstream", timeout=5.0)
    app = create_app(acl=ACL.from_dict(ACL_DATA), audit=Audit(path=str(tmp_path / "audit.log")),
                     upstream_key=UPSTREAM_KEY, http_client=http,
                     recall_ledger=RecallLedger(str(ledger_path)))
    return TestClient(app), ledger_path, tmp_path / "audit.log"


def _recall(client, surface, extra):
    headers = {"content-type": "application/json", "authorization": "Bearer tok-alice"}
    if surface == "mcp":
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "recall", "arguments": {"query": QUERY, **extra}}}
        resp = client.post(f"/mcp/{BANK}/", content=json.dumps(body), headers=headers)
    else:
        resp = client.post(f"/v1/default/banks/{BANK}/memories/recall",
                           content=json.dumps({"query": QUERY, **extra}), headers=headers)
    assert resp.status_code == 200
    return resp


def _rows(ledger_path):
    return [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("extra, expected", CASES + ODD_CASES)
def test_ledger_row_carries_budget_and_max_tokens(gateway, surface, extra, expected):
    client, ledger_path, _ = gateway
    _recall(client, surface, extra)

    (row,) = _rows(ledger_path)
    assert "budget" in row and "max_tokens" in row            # 키 자체는 항상 있다 (값 null의 뜻은 ODD_CASES 주석)
    assert (row["budget"], row["max_tokens"]) == expected
    # 나머지 필드와 쿼리 텍스트 취급은 그대로다
    assert row["query"] == QUERY and row["result_ids"] == ["mem-1"]
    assert row["transport"] == surface and row["identity"] == "alice" and row["bank"] == BANK
    assert row["tags"] == [] and row["tags_match"] == "any" and row["reusable"] is True


@pytest.mark.parametrize("surface", SURFACES)
def test_audit_log_stays_metadata_only(gateway, surface):
    client, _, audit_path = gateway
    _recall(client, surface, {"budget": "mid", "max_tokens": 8192})

    audit_text = audit_path.read_text(encoding="utf-8")
    assert QUERY not in audit_text                             # 쿼리 텍스트는 원장에만 있다
    assert '"budget"' not in audit_text and '"max_tokens"' not in audit_text
