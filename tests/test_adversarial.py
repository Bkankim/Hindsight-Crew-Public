"""Gate ⑤ adversarial — every input must be rejected or neutralized. Mock upstream."""
import json

import pytest

from conftest import call

BODY_BANK_TOOLS = ["recall", "list_memories", "get_memory", "clear_memories", "delete_bank"]


def test_unknown_token_denied(client, recorder):
    r = call(client, "personal-alice", "tok-nope", name="recall", arguments={"query": "x"})
    assert r.status_code == 403
    assert recorder.last is None


def test_missing_token_denied(client, recorder):
    r = call(client, "personal-alice", None, name="recall", arguments={"query": "x"})
    assert r.status_code == 403
    assert recorder.last is None


def test_cross_tenant_bank_denied(client, recorder):
    # alice tries bob's personal bank
    r = call(client, "personal-bob", "tok-alice", name="recall", arguments={"query": "x"})
    assert r.status_code == 403
    assert recorder.last is None


@pytest.mark.parametrize("bank", ["..", "...", "%2e%2e", "personal-alice;rm", "a/b"])
def test_path_traversal_or_bad_bank_denied(client, recorder, bank):
    # Use raw post so weird bank values reach the route verbatim where routable.
    import urllib.parse
    enc = urllib.parse.quote(bank, safe="")
    r = client.post(f"/mcp/{enc}/", content="{}",
                    headers={"authorization": "Bearer tok-alice", "content-type": "application/json"})
    assert r.status_code in (403, 404)  # rejected by format guard or unrouted
    if r.status_code == 403:
        assert recorder.last is None


@pytest.mark.parametrize("tool", BODY_BANK_TOOLS)
def test_body_arguments_bank_smuggling_stripped(client, recorder, tool):
    # alice acts on her OWN bank but smuggles a victim bank in the body arguments.
    bank = "personal-alice"  # destructive tools allowed only here; read tools fine too
    r = call(client, bank, "tok-alice", name=tool, arguments={"bank": "personal-bob", "bank_id": "team-eng", "query": "x"})
    assert r.status_code == 200, f"{tool} on own bank should be allowed"
    args = recorder.last["body"]["params"]["arguments"]
    # all client bank hints must be stripped before forwarding
    assert "bank" not in args
    assert "bank_id" not in args
    # bank pinned via header to the ACL bank; smuggled body/header values never win
    assert recorder.last["headers"]["x-bank-id"] == bank
    assert recorder.last["path"] == "/mcp"


def test_batch_with_one_denied_rejects_whole(client, recorder):
    batch = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "recall", "arguments": {"query": "ok"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "delete_bank", "arguments": {}}},  # destructive on team
    ]
    r = call(client, "team-eng", "tok-alice", raw=batch)
    assert r.status_code == 403          # fail-closed: one bad element kills the batch
    assert recorder.last is None         # nothing forwarded
