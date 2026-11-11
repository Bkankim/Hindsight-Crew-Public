"""Gate ⑤ (positive ACL) + core forwarding contract — unit, mock upstream."""
import json

from conftest import call, UPSTREAM_KEY
from gateway.acl import Principal
from gateway.bank_policies import BankPolicy
from gateway.policy import decide


def test_allowed_recall_forwards_pinned_path_and_injects_key(client, recorder):
    r = call(client, "personal-alice", "tok-alice", name="recall", arguments={"query": "x"})
    assert r.status_code == 200
    fwd = recorder.last
    assert fwd["path"] == "/mcp"                                  # forwarded to upstream /mcp
    assert fwd["headers"]["x-bank-id"] == "personal-alice"        # bank pinned via header
    assert fwd["headers"]["authorization"] == f"Bearer {UPSTREAM_KEY}"  # upstream key injected
    # client token must NOT appear anywhere in forwarded headers
    assert "tok-alice" not in json.dumps(fwd["headers"])


def test_team_recall_allowed(client, recorder):
    r = call(client, "team-eng", "tok-alice", name="recall", arguments={"query": "x"})
    assert r.status_code == 200
    assert recorder.last["path"] == "/mcp"
    assert recorder.last["headers"]["x-bank-id"] == "team-eng"


def test_batch_all_allowed_forwards_list(client, recorder):
    batch = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "recall", "arguments": {"query": "a"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_memories", "arguments": {}}},
    ]
    r = call(client, "personal-alice", "tok-alice", raw=batch)
    assert r.status_code == 200
    assert isinstance(recorder.last["body"], list) and len(recorder.last["body"]) == 2


def test_root_multibank_blocked(client, recorder):
    r = client.post("/mcp/", content="{}", headers={"authorization": "Bearer tok-alice", "content-type": "application/json"})
    assert r.status_code == 403
    assert recorder.last is None  # never forwarded


def test_destructive_on_own_personal_allowed(client, recorder):
    r = call(client, "personal-alice", "tok-alice", name="delete_bank", arguments={})
    assert r.status_code == 200
    assert recorder.last["path"] == "/mcp"
    assert recorder.last["headers"]["x-bank-id"] == "personal-alice"


def test_destructive_on_team_denied(client, recorder):
    r = call(client, "team-eng", "tok-alice", name="delete_bank", arguments={})
    assert r.status_code == 403
    assert recorder.last is None


def test_bank_policy_blocked_operations_decision_matrix():
    principal = Principal(identity="alice", personal="project", teams=("team-eng",))
    blocked = BankPolicy(
        blocked_operations=frozenset({"delete_bank", "clear_memories"})
    )

    for tool in ("delete_bank", "clear_memories"):
        decision = decide(
            principal, "project", "tools/call", tool, bank_policy=blocked
        )
        assert decision.allow is False
        assert decision.reason.startswith("blocked-by-bank-policy:")

    for tool in (
        "delete_document",
        "update_memory",
        "invalidate_memory",
        "update_bank",
    ):
        decision = decide(
            principal, "project", "tools/call", tool, bank_policy=blocked
        )
        assert decision.allow is True

    unblocked = decide(
        principal,
        "project",
        "tools/call",
        "delete_bank",
        bank_policy=BankPolicy(blocked_operations=frozenset()),
    )
    assert unblocked.allow is True

    nonpersonal = decide(
        principal, "team-eng", "tools/call", "delete_bank", bank_policy=blocked
    )
    assert nonpersonal.allow is False
    assert nonpersonal.reason == "destructive-on-nonpersonal:delete_bank"


def test_unknown_tool_denied(client, recorder):
    r = call(client, "personal-alice", "tok-alice", name="exfiltrate_everything", arguments={})
    assert r.status_code == 403
    assert recorder.last is None


def test_unknown_method_answers_jsonrpc_method_not_found(client, recorder):
    # 화이트리스트 밖 메서드는 authz 실패가 아니라 프로토콜상 "없는 메서드"다.
    # 403을 주면 비표준 메서드를 탐색하는 클라이언트가 연결 자체를 포기한다
    # (2026-09-05 실측: ZCode의 `server/discover`).
    r = call(client, "personal-alice", "tok-alice",
             raw={"jsonrpc": "2.0", "id": 7, "method": "server/discover", "params": {}})
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == 7
    assert body["error"]["code"] == -32601
    assert recorder.last is None                      # 업스트림으로 절대 안 나간다


def test_unknown_method_notification_gets_202_and_no_body(client, recorder):
    r = call(client, "personal-alice", "tok-alice",
             raw={"jsonrpc": "2.0", "method": "server/discover", "params": {}})
    assert r.status_code == 202
    assert r.content == b""
    assert recorder.last is None


def test_unknown_method_inside_batch_stays_fail_closed(client, recorder):
    batch = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "recall", "arguments": {"query": "a"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "server/discover", "params": {}},
    ]
    r = call(client, "personal-alice", "tok-alice", raw=batch)
    assert r.status_code == 403
    assert recorder.last is None


def test_authz_denies_keep_their_403_channel(client, recorder):
    # 토큰 불량과 ACL 밖 뱅크는 여전히 403이어야 한다(-32601로 뭉개지 않는다).
    assert call(client, "personal-alice", "tok-nope", name="recall").status_code == 403
    assert call(client, "personal-bob", "tok-alice", name="recall").status_code == 403
    assert recorder.last is None


def test_xbankid_header_not_forwarded(client, recorder):
    r = call(client, "personal-alice", "tok-alice", name="recall", arguments={"query": "x"},
             headers={"x-bank-id": "team-eng"})
    assert r.status_code == 200
    # gateway OVERWRITES x-bank-id with the ACL-resolved bank; client's value never wins
    assert recorder.last["headers"]["x-bank-id"] == "personal-alice"
    assert recorder.last["path"] == "/mcp"


def test_bank_scoped_read_allowed_on_any_acl_bank(client, recorder):
    # Introspection reads are bank-pinned by path + ACL; allowed on personal AND team.
    for bank in ("personal-alice", "team-eng"):
        for tool in ("get_bank", "get_bank_stats", "list_mental_models", "get_operation"):
            r = call(client, bank, "tok-alice", name=tool, arguments={})
            assert r.status_code == 200, f"{tool} on {bank}"
            assert recorder.last["headers"]["x-bank-id"] == bank


def test_corrective_on_own_personal_allowed(client, recorder):
    r = call(client, "personal-alice", "tok-alice", name="update_memory", arguments={"memory_id": "m1"})
    assert r.status_code == 200
    assert recorder.last["headers"]["x-bank-id"] == "personal-alice"


def test_corrective_on_team_denied(client, recorder):
    r = call(client, "team-eng", "tok-alice", name="invalidate_memory", arguments={"memory_id": "m1"})
    assert r.status_code == 403
    assert recorder.last is None


def test_cross_bank_admin_still_denied(client, recorder):
    # Enumeration/provisioning surface never opens, regardless of bank in path.
    for tool in ("list_banks", "create_bank"):
        r = call(client, "personal-alice", "tok-alice", name=tool, arguments={})
        assert r.status_code == 403
    assert recorder.last is None
