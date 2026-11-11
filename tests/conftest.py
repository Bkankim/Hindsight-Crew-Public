"""Test harness: gateway with a MOCK Hindsight upstream (no Docker).

The mock records every forwarded request (path, headers, body) so tests can assert
path-pinning, header allowlist, upstream-key injection, body sanitization, and
attribution overwrite. This stands in for contract-probe-generated fixtures in unit tests.
"""
import json
import os
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))  # repo root on path

from gateway.acl import ACL
from gateway.audit import Audit
from gateway.app import create_app

UPSTREAM_KEY = "UPSTREAM-SECRET-do-not-leak"


@pytest.fixture(autouse=True)
def _isolate_issue12_state(monkeypatch, tmp_path):
    """Issue #12 ledgers default to repo-relative audit/ paths (the same files the live
    container appends to through the ./audit mount). Review D3 (2026-09-21) found 113 alice
    test rows in the real audit/recall-queries.log. Every app a test builds - through any
    helper in any test module - must therefore get tmp paths and no notifier credentials."""
    monkeypatch.setenv("HC_RECALL_QUERY_LOG", str(tmp_path / "recall-queries.log"))
    monkeypatch.setenv("HC_PENDING_TRIGGERS", str(tmp_path / "pending-triggers.jsonl"))
    monkeypatch.delenv("HC_NOTIFY_DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("HC_NOTIFY_DISCORD_CHANNEL_ID", raising=False)

@pytest.fixture(autouse=True)
def _default_off_gateway_flags(monkeypatch):
    """Default-off gateway flags (issue #302) must not leak in from the shell that runs the suite
    (e.g. after `set -a; . ./.env.local` once ops has turned them on): the deny and byte-exact tests
    pin the OFF state. A test that needs a flag on sets it itself."""
    monkeypatch.delenv("HC_RECALL_KIND_LABEL", raising=False)
    monkeypatch.delenv("HC_KB_READ_TOOLS", raising=False)
    monkeypatch.delenv("HC_RETAIN_ITEM_KEY", raising=False)
    monkeypatch.delenv("HC_ITEM_OPEN_TABLE", raising=False)


@pytest.fixture(autouse=True, scope="session")
def _host_settings_for_scripts():
    """Host scripts (scripts/*.py, *.sh) take the owner's bank and identity from HC_PERSONAL_BANK /
    HC_OWNER (.env.host.local, issue #186) and have no default. The suite runs them with the values it
    always used, and never with a developer's notification target (HC_NOTIFY_* / the legacy GGG_*).
    Session scope: several test modules load a script in a module-scoped fixture, before any function
    scope fixture runs. A test that needs a key absent uses its own monkeypatch.delenv."""
    patch = pytest.MonkeyPatch()
    patch.setenv("HC_PERSONAL_BANK", "personal-demo")
    patch.setenv("HC_OWNER", "demo")
    for key in ("HC_LAUNCHD_PREFIX", "GGG_DISCORD_BOT_TOKEN", "GGG_DISCORD_CHANNEL_ID"):
        patch.delenv(key, raising=False)
    yield
    patch.undo()


ACL_DATA = {
    "tokens": {
        "tok-alice": {"identity": "alice", "personal": "personal-alice", "teams": ["team-eng"]},
        "tok-bob": {"identity": "bob", "personal": "personal-bob", "teams": ["team-eng"]},
    }
}


def bank_config_reply(request: httpx.Request, mcp_enabled_tools=None):
    """Issue #473 b: the gateway reads upstream's bank config before every REST retain. A mock upstream
    answers that read here (fixtures/rest/bank_config.json shape, `mcp_enabled_tools` null = no
    restriction by default) and returns None for every other request. Only the gateway's own read
    carries BANK_CONFIG_LOOKUP_HEADER; a forwarded client GET .../config does not."""
    from gateway.app import BANK_CONFIG_LOOKUP_HEADER
    name, value = BANK_CONFIG_LOOKUP_HEADER
    if request.headers.get(name) != value:
        return None
    body = json.loads(json.dumps(load_rest_fixture("bank_config")["response"]))
    parts = request.url.path.split("/")
    body["bank_id"] = parts[4] if len(parts) > 4 else body["bank_id"]
    body["config"]["mcp_enabled_tools"] = mcp_enabled_tools
    return httpx.Response(200, json=body)


class Recorder:
    def __init__(self):
        self.calls = []
        self.config_calls = []     # the gateway's bank-config reads (#473 b), kept out of `calls`

    def handler(self, request: httpx.Request) -> httpx.Response:
        config = bank_config_reply(request)
        if config is not None:
            self.config_calls.append({"path": request.url.path,
                                      "headers": {k.lower(): v for k, v in request.headers.items()}})
            return config
        body = request.content.decode("utf-8") if request.content else ""
        try:
            parsed = json.loads(body) if body else None
        except json.JSONDecodeError:
            parsed = None
        self.calls.append({
            "path": request.url.path,
            "query": str(request.url.query, "utf-8") if isinstance(request.url.query, bytes) else str(request.url.query),
            "headers": {k.lower(): v for k, v in request.headers.items()},
            "body": parsed,
        })
        return httpx.Response(
            200,
            content=b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}',
            headers={"content-type": "application/json", "mcp-session-id": "sess-upstream-1"},
        )

    @property
    def last(self):
        return self.calls[-1] if self.calls else None


@pytest.fixture
def recorder():
    return Recorder()


@pytest.fixture
def make_app(recorder, tmp_path):
    def _make(attribution_enabled=True, bank_policies=None):
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(recorder.handler),
            base_url="http://upstream",
            timeout=5.0,
        )
        audit = Audit(path=str(tmp_path / "audit.log"))
        app = create_app(
            acl=ACL.from_dict(ACL_DATA),
            audit=audit,
            upstream_key=UPSTREAM_KEY,
            http_client=client,
            attribution_enabled=attribution_enabled,
            bank_policies=bank_policies,   # None = defaults-only (loose), pre-N1 behaviour
        )
        app.state._audit_path = str(tmp_path / "audit.log")
        return app
    return _make


@pytest.fixture
def client(make_app):
    return TestClient(make_app())


FIXTURES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "fixtures")


def _unwrap_sse(raw):
    """Return the last SSE `data:` payload; pass raw text through when unframed.

    upstream(v0.8.3)은 MCP 응답을 text/event-stream 으로 프레이밍한다 (N0 실측).
    """
    payloads = [ln[len("data:"):].strip() for ln in raw.splitlines() if ln.startswith("data:")]
    return payloads[-1] if payloads else raw


def load_mcp_fixture(name):
    """fixtures/mcp/<name>.raw (upstream 원문) -> parsed JSON-RPC object."""
    with open(os.path.join(FIXTURES_DIR, "mcp", f"{name}.raw"), encoding="utf-8") as fh:
        return json.loads(_unwrap_sse(fh.read()))


def load_rest_fixture(name):
    """fixtures/rest/<name>.json ({request, status, response}) -> dict."""
    with open(os.path.join(FIXTURES_DIR, "rest", f"{name}.json"), encoding="utf-8") as fh:
        return json.load(fh)


def call(client, bank, token, method="tools/call", name="recall", arguments=None, headers=None, raw=None):
    body = raw if raw is not None else {
        "jsonrpc": "2.0", "id": 1, "method": method,
        "params": ({"name": name, "arguments": arguments or {}} if method == "tools/call" else {}),
    }
    h = {"content-type": "application/json"}
    if token:
        h["authorization"] = f"Bearer {token}"
    if headers:
        h.update(headers)
    return client.post(f"/mcp/{bank}/", content=json.dumps(body), headers=h)
