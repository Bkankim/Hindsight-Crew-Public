"""MCP handshake passthrough - standard clients must be able to initialize.

initialize / notifications/initialized / ping carry no tool arguments and no bank
data, so they are safe to forward AFTER the same principal+bank ACL gate as every
other method. Everything else stays deny-by-default. The streamable-HTTP session
header (Mcp-Session-Id) must round-trip client<->upstream or the handshake is moot.
"""
import json
import os

from conftest import call


def test_initialize_allowed_for_valid_principal(client, recorder):
    r = call(client, "personal-alice", "tok-alice", method="initialize")
    assert r.status_code == 200
    assert recorder.last["path"] == "/mcp"
    assert recorder.last["headers"]["x-bank-id"] == "personal-alice"


def test_initialized_notification_allowed(client, recorder):
    r = call(client, "personal-alice", "tok-alice", method="notifications/initialized")
    assert r.status_code == 200


def test_ping_allowed(client, recorder):
    r = call(client, "personal-alice", "tok-alice", method="ping")
    assert r.status_code == 200


def test_initialize_denied_without_token(client, recorder):
    r = call(client, "personal-alice", None, method="initialize")
    assert r.status_code == 403
    assert recorder.last is None


def test_initialize_denied_on_foreign_bank(client, recorder):
    r = call(client, "personal-bob", "tok-alice", method="initialize")
    assert r.status_code == 403
    assert recorder.last is None


def test_non_handshake_methods_still_denied(client, recorder):
    # 17f4316 이후 화이트리스트 밖 메서드는 403이 아니라 JSON-RPC -32601이다
    # (프로토콜상 없는 메서드 - authz 실패가 아님). 업스트림 미전달 불변식은 그대로.
    for method in ("resources/list", "prompts/list", "completion/complete"):
        r = call(client, "personal-alice", "tok-alice", method=method)
        assert r.status_code == 200, method
        assert r.json()["error"]["code"] == -32601, method
    assert recorder.last is None


def test_session_id_header_forwarded_upstream(client, recorder):
    r = call(client, "personal-alice", "tok-alice", method="initialize",
             headers={"mcp-session-id": "sess-from-client"})
    assert r.status_code == 200
    assert recorder.last["headers"]["mcp-session-id"] == "sess-from-client"


def test_session_id_header_returned_to_client(client, recorder):
    r = call(client, "personal-alice", "tok-alice", method="initialize")
    assert r.status_code == 200
    assert r.headers.get("mcp-session-id") == "sess-upstream-1"


# --------------------------------------------------------------------------
# initialize instructions - 뱅크 규칙을 클라이언트가 첫 호출 전에 알게 한다
# --------------------------------------------------------------------------

def test_initialize_carries_bank_rule_instructions(make_app):
    from fastapi.testclient import TestClient
    from gateway.bank_policies import BankPolicies
    import os

    repo_root = os.path.dirname(os.path.dirname(__file__))
    c = TestClient(make_app(bank_policies=BankPolicies.from_file(
        os.path.join(repo_root, "tests", "fixtures", "policies", "sample", "bank-policies.json"))))
    r = call(c, "personal-alice", "tok-alice", method="initialize")

    assert r.status_code == 200
    instructions = r.json()["result"]["instructions"]
    assert "project" in instructions and "kind" in instructions      # 허용 접두사
    assert "topic" in instructions and "user" in instructions
    assert "[" in instructions                                       # content 접두 라벨 규칙
    assert "tag" in instructions.lower()


def test_initialize_without_declared_rules_is_untouched(make_app, recorder):
    from fastapi.testclient import TestClient
    c = TestClient(make_app())                       # 정책 미주입 = 느슨한 defaults
    r = call(c, "personal-bob", "tok-bob", method="initialize")
    assert r.status_code == 200
    assert "instructions" not in r.json()["result"]


# --------------------------------------------------------------------------
# tools/list 규칙 주입 - instructions 를 노출하지 않는 클라이언트까지 닿는 유일한 채널
# --------------------------------------------------------------------------
# 실측(2026-07-27): initialize 의 instructions 를 모델 컨텍스트에 올리는 클라이언트는
# Claude Code 뿐이고 YEW·aside 는 받고 버린다. 툴 description 은 어느 클라이언트든
# 모델에게 전달하므로, 규칙이 실제로 도착하는 공통 채널은 여기다.

def _tools_list_app(tmp_path, upstream_raw):
    import httpx
    from fastapi.testclient import TestClient as _TC
    from conftest import ACL_DATA, UPSTREAM_KEY
    from gateway.acl import ACL
    from gateway.app import create_app
    from gateway.audit import Audit
    from gateway.bank_policies import BankPolicies
    import os

    def handler(request):
        return httpx.Response(200, content=upstream_raw,
                              headers={"content-type": "text/event-stream"})

    repo_root = os.path.dirname(os.path.dirname(__file__))
    app = create_app(
        acl=ACL.from_dict(ACL_DATA), audit=Audit(path=str(tmp_path / "a.log")),
        upstream_key=UPSTREAM_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                      base_url="http://upstream", timeout=5.0),
        bank_policies=BankPolicies.from_file(
            os.path.join(repo_root, "tests", "fixtures", "policies", "sample", "bank-policies.json")))
    app.state._audit_path = str(tmp_path / "a.log")
    return _TC(app)


def _tools_list_req(el_id=2):
    """픽스처 tools_list.raw 의 응답 id 가 2 라 요청 id 도 2 로 맞춘다(실제 클라이언트는
    자기 요청 id 를 그대로 돌려받는다). conftest.call 은 id 를 1 로 고정한다."""
    return {"jsonrpc": "2.0", "id": el_id, "method": "tools/list", "params": {}}


def _tools_from(resp):
    payload = [ln[len("data:"):].strip() for ln in resp.content.decode().splitlines()
               if ln.startswith("data:")]
    return {t["name"]: t for t in json.loads(payload[-1])["result"]["tools"]}


def test_retain_tool_description_carries_the_bank_rules(tmp_path):
    with open(os.path.join(os.path.dirname(os.path.dirname(__file__)),
                           "fixtures", "mcp", "tools_list.raw"), "rb") as fh:
        raw = fh.read()
    c = _tools_list_app(tmp_path, raw)
    r = call(c, "personal-alice", "tok-alice", raw=_tools_list_req())

    tools = _tools_from(r)
    desc = tools["retain"]["description"]
    assert "project" in desc and "kind" in desc and "topic" in desc and "user" in desc
    assert "<prefix>:<slug>" in desc
    assert "tags are REQUIRED" in desc
    tags_schema = tools["retain"]["inputSchema"]["properties"]["tags"]
    assert "<prefix>:<slug>" in tags_schema.get("description", "")


def test_non_retain_tool_descriptions_are_untouched(tmp_path):
    with open(os.path.join(os.path.dirname(os.path.dirname(__file__)),
                           "fixtures", "mcp", "tools_list.raw"), "rb") as fh:
        raw = fh.read()
    c = _tools_list_app(tmp_path, raw)
    tools = _tools_from(call(c, "personal-alice", "tok-alice", raw=_tools_list_req()))
    assert "enforces" not in tools["recall"]["description"]


def test_tools_list_on_a_rule_less_bank_is_byte_identical(tmp_path):
    with open(os.path.join(os.path.dirname(os.path.dirname(__file__)),
                           "fixtures", "mcp", "tools_list.raw"), "rb") as fh:
        raw = fh.read()
    c = _tools_list_app(tmp_path, raw)
    r = call(c, "personal-bob", "tok-bob", raw=_tools_list_req())   # 정책 미선언 뱅크
    assert r.content == raw
