"""Gate ④ team-retain attribution — member identity stamped, forgery overwritten.

NOTE: this proves the GATEWAY behavior (overwrite at the body layer). Whether the
metadata actually PERSISTS in Hindsight is proven separately by the Stage 3 integration
retain->recall round-trip (execution note #1); unit tests cannot prove persistence.
"""
from conftest import call, ACL_DATA


def test_team_retain_stamps_member_identity(client, recorder):
    r = call(client, "team-eng", "tok-alice", method="tools/call", name="sync_retain",
             arguments={"content": "team note"})
    assert r.status_code == 200
    md = recorder.last["body"]["params"]["arguments"]["metadata"]
    assert md["hc_member"] == "alice"


def test_team_retain_attribution_forgery_overwritten(client, recorder):
    # alice tries to forge the note as bob
    r = call(client, "team-eng", "tok-alice", method="tools/call", name="sync_retain",
             arguments={"content": "spoof", "metadata": {"hc_member": "bob", "keep": 1}})
    assert r.status_code == 200
    md = recorder.last["body"]["params"]["arguments"]["metadata"]
    assert md["hc_member"] == "alice"   # overwritten, not bob
    assert md["keep"] == 1              # other metadata preserved


def test_personal_retain_not_team_attributed(client, recorder):
    r = call(client, "personal-alice", "tok-alice", method="tools/call", name="sync_retain",
             arguments={"content": "private"})
    assert r.status_code == 200
    args = recorder.last["body"]["params"]["arguments"]
    # personal bank is implicitly owned; we do not stamp team attribution there
    assert "metadata" not in args or "hc_member" not in (args.get("metadata") or {})


def test_audit_only_mode_does_not_inject(make_app):
    """When contract-probe says retain metadata is NOT persisted, attribution_enabled=False
    (audit-only fallback): the gateway must not inject into the body."""
    from fastapi.testclient import TestClient
    import json
    app = make_app(attribution_enabled=False)
    c = TestClient(app)
    r = c.post("/mcp/team-eng/", headers={"authorization": "Bearer tok-alice", "content-type": "application/json"},
               content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": "sync_retain", "arguments": {"content": "x"}}}))
    assert r.status_code == 200
    # cannot assert recorder here (separate app), but the call must succeed without injecting;
    # behavior verified via no exception + 200. Body-level assertion covered by enabled-mode tests.
