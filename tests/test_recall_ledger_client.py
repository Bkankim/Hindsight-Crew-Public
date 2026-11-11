"""이슈 #89: recall 원장 줄에 MCP 클라이언트 이름(`client`)과 요청 도착 시각(`arrived_ts`)이 남는다.

#81 중복 recall 분석이 출처를 클라이언트별로 나누지 못했고, 동시 중복은 감사 로그 도착 줄과 시간 순서로
붙이는 근사에 기댔다. 원장이 세션의 클라이언트 이름(#37)과 게이트웨이가 요청을 받은 시각을 직접 남기면
그 근사가 필요 없다. 공개 경계 = 게이트웨이 요청 뒤 원장 줄.

- `client`: MCP 줄에만. 그 요청의 mcp-session-id가 가리키는 세션에 #37이 묶어 둔(정리된) 이름. 세션이
  없거나 모르는 세션이거나 이름이 없으면 null(이름이 실제로 "unknown"인 클라이언트는 그대로 "unknown").
  REST 줄은 세션 헤더를 실어도 항상 null이다. 조회는 #37 LRU 순서를 바꾸지 않는다.
- `arrived_ts`: 핸들러 진입 시각, 밀리초 정밀도(`...:SS.mmmZ`). 기존 `ts`(응답 완료 시각, 초 단위)와 다른
  필드다. 시계가 요청 도중 뒤로 가도 `ts`보다 늦게 기록되지 않는다(min).
"""
import hashlib
import json
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.acl import ACL
from gateway.app import create_app
from gateway.audit import Audit
from gateway.recurrence import RecallLedger

from conftest import ACL_DATA, UPSTREAM_KEY

BANK = "personal-alice"
QUERY = "recall 원장 클라이언트 이름 기록 확인용 쿼리"
SURFACES = ["mcp", "rest"]
SESSION = "sess-upstream-1"                 # upstream이 initialize 응답에 실어 주는 세션 id
HEADERS = {"content-type": "application/json", "authorization": "Bearer tok-alice"}

# 시계 시작 1000.25초 = 1970-01-01T00:16:40.250Z, 업스트림 지연 5초 -> 1005.25초 = 00:16:45Z (손 계산).
# 지연 -10초(시계 역행) -> 990.25초 = 00:16:30Z, 이때 arrived_ts는 ts까지 당겨진다 = 00:16:30.250Z
CLOCK_START = 1000.25
UPSTREAM_DELAY = 5.0
ARRIVED_ISO = "1970-01-01T00:16:40.250Z"
DONE_ISO = "1970-01-01T00:16:45Z"
CLOCK_BACK_DELAY = -10.0
CLAMPED_ARRIVED_ISO = "1970-01-01T00:16:30.250Z"
CLAMPED_DONE_ISO = "1970-01-01T00:16:30Z"
ARRIVED_FORMAT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


class FakeClock:
    """진행하지 않는 시계. 테스트(업스트림 모의)가 직접 앞으로 돌릴 때만 움직인다."""

    def __init__(self, now: float):
        self.now = now
        self.delay = UPSTREAM_DELAY
        self.on_upstream = None                      # 업스트림이 일하는 도중 실행할 훅(재-initialize 재현용)

    def __call__(self) -> float:
        return self.now


def _sse(payload):
    return b"event: message\r\ndata: " + json.dumps(payload).encode("utf-8") + b"\r\n\r\n"


def _make_upstream(clock):
    def upstream(request: httpx.Request) -> httpx.Response:
        clock.now += clock.delay                     # 업스트림이 일하는 동안 시간이 흐른다
        if clock.on_upstream is not None:
            clock.on_upstream()
        body = json.loads(request.content or b"{}")
        results = {"results": [{"id": "mem-1", "text": "x"}]}
        if request.url.path.endswith("/memories/recall"):
            return httpx.Response(200, json=results)
        if isinstance(body, dict) and body.get("method") == "initialize":
            result = {"protocolVersion": "2025-03-26", "capabilities": {},
                      "serverInfo": {"name": "hindsight", "version": "0"}}
        else:
            encoded = json.dumps(results)
            result = {"content": [{"type": "text", "text": encoded}],
                      "structuredContent": {"result": encoded}, "isError": False}
        return httpx.Response(200, content=_sse({"jsonrpc": "2.0", "id": body.get("id"), "result": result}),
                              headers={"content-type": "text/event-stream", "mcp-session-id": SESSION})
    return upstream


@pytest.fixture
def gateway(tmp_path):
    clock = FakeClock(CLOCK_START)
    ledger_path = tmp_path / "ledger.jsonl"
    http = httpx.AsyncClient(transport=httpx.MockTransport(_make_upstream(clock)),
                             base_url="http://upstream", timeout=5.0)
    app = create_app(acl=ACL.from_dict(ACL_DATA), audit=Audit(path=str(tmp_path / "audit.log")),
                     upstream_key=UPSTREAM_KEY, http_client=http,
                     recall_ledger=RecallLedger(str(ledger_path)), clock=clock)
    return TestClient(app), ledger_path, tmp_path / "audit.log", clock


def _initialize(client, name):
    info = {"version": "9.9"} if name is None else {"name": name, "version": "9.9"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": info}}
    resp = client.post(f"/mcp/{BANK}/", content=json.dumps(body), headers=HEADERS)
    assert resp.status_code == 200 and resp.headers["mcp-session-id"] == SESSION


def _recall(client, surface, session=None):
    headers = dict(HEADERS)
    if session is not None:
        headers["mcp-session-id"] = session
    if surface == "mcp":
        body = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "recall", "arguments": {"query": QUERY}}}
        resp = client.post(f"/mcp/{BANK}/", content=json.dumps(body), headers=headers)
    else:
        resp = client.post(f"/v1/default/banks/{BANK}/memories/recall",
                           content=json.dumps({"query": QUERY}), headers=headers)
    assert resp.status_code == 200
    return resp


def _rows(ledger_path):
    return [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]


def _recall_rows(ledger_path):
    return [r for r in _rows(ledger_path) if r["query"] == QUERY]


# (initialize에서 보낸 name, recall이 싣는 세션 헤더, 원장에 기대하는 client)
# expected_client는 MCP 줄 기준이다. REST 줄은 같은 세션 헤더를 실어도 항상 null이다.
CLIENT_CASES = [
    pytest.param("claude-code", SESSION, "claude-code", id="named-session"),
    pytest.param("claude-code", None, None, id="no-session-header"),
    pytest.param("claude-code", "sess-never-initialized", None, id="unknown-session"),
    pytest.param(None, SESSION, None, id="initialize-without-name"),
    # #37 정리 함수를 거친 값만: 제어 문자는 빠지고 64자에서 잘린다. "ab" + "z" 62개 (손 계산)
    pytest.param("a\nb\x00" + "z" * 200, SESSION, "ab" + "z" * 62, id="name-is-cleaned"),
    pytest.param("\n\x00", SESSION, None, id="name-cleans-to-nothing"),
    pytest.param("unknown", SESSION, "unknown", id="name-literally-unknown"),
]


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("init_name, session, expected_client", CLIENT_CASES)
def test_ledger_row_carries_client_and_arrived_ts(gateway, surface, init_name, session, expected_client):
    client, ledger_path, _, _ = gateway
    _initialize(client, init_name)
    _recall(client, surface, session)

    (row,) = _recall_rows(ledger_path)
    assert "client" in row and "arrived_ts" in row            # 키 자체는 항상 있다 (null의 뜻은 모듈 docstring)
    assert row["client"] == (expected_client if surface == "mcp" else None)
    assert ARRIVED_FORMAT.match(row["arrived_ts"])
    assert row["arrived_ts"][:19] <= row["ts"][:19]            # ts는 초 단위로 잘려 있어 초까지만 견준다
    # 나머지 필드와 쿼리 텍스트 취급은 그대로다
    assert row["transport"] == surface and row["identity"] == "alice" and row["bank"] == BANK
    assert row["result_ids"] == ["mem-1"] and row["tags"] == [] and row["reusable"] is True


@pytest.mark.parametrize("surface", SURFACES)
def test_arrived_ts_is_taken_on_arrival_not_on_completion(gateway, surface):
    client, ledger_path, _, clock = gateway
    _initialize(client, "claude-code")
    clock.now = CLOCK_START                                    # initialize가 흘린 시간을 되돌린다
    _recall(client, surface, SESSION)

    (row,) = _recall_rows(ledger_path)
    assert row["arrived_ts"] == ARRIVED_ISO                    # 업스트림을 부르기 전 시각, 밀리초 정밀도
    assert row["ts"] == DONE_ISO                               # 응답을 받은 뒤 시각 (기존 의미·초 형식 그대로)


@pytest.mark.parametrize("surface", SURFACES)
def test_arrived_ts_never_later_than_ts_when_the_clock_steps_back(gateway, surface):
    client, ledger_path, _, clock = gateway
    _initialize(client, "claude-code")
    clock.now, clock.delay = CLOCK_START, CLOCK_BACK_DELAY     # 요청 도중 벽시계가 10초 뒤로 간다
    _recall(client, surface, SESSION)

    (row,) = _recall_rows(ledger_path)
    assert row["ts"] == CLAMPED_DONE_ISO
    assert row["arrived_ts"] == CLAMPED_ARRIVED_ISO            # 도착(1000.25)이 아니라 ts(990.25)로 당겨짐
    assert row["arrived_ts"][:19] <= row["ts"][:19]


def _fingerprint(sid):
    return hashlib.sha256(sid.encode()).hexdigest()            # 게이트웨이가 세션 id 대신 쥐는 지문


def test_reading_the_client_for_the_ledger_does_not_reorder_the_lru(gateway):
    client, ledger_path, _, _ = gateway
    sessions = client.app.state.mcp_clients
    sessions.__init__(limit=2)                                 # 자리 둘짜리 #37 저장소로 바꿔 퇴출을 본다
    sessions.bind(_fingerprint("sess-a"), "client-a")          # 가장 오래된 세션
    sessions.bind(_fingerprint("sess-b"), "client-b")
    _recall(client, "mcp", "sess-a")                           # recall이 a를 읽는다 (lookup이면 a가 최신이 된다)
    sessions.bind(_fingerprint("sess-c"), "client-c")          # 자리가 모자라 가장 오래된 하나가 밀린다

    _recall(client, "mcp", "sess-a")
    _recall(client, "mcp", "sess-b")
    first, second, third = _recall_rows(ledger_path)
    assert first["client"] == "client-a"                       # 읽을 때는 있었다
    assert (second["client"], third["client"]) == (None, "client-b")   # 읽기가 순서를 안 바꿨으니 a가 밀렸다


@pytest.mark.parametrize("surface", SURFACES)
def test_audit_log_gets_no_client_or_arrival_fields(gateway, surface):
    client, _, audit_path, _ = gateway
    _initialize(client, "claude-code")
    _recall(client, surface, SESSION)

    audit_text = audit_path.read_text(encoding="utf-8")
    assert QUERY not in audit_text
    assert '"arrived_ts"' not in audit_text
    recall_lines = [json.loads(ln) for ln in audit_text.splitlines() if '"recall"' in ln or "memories/recall" in ln]
    assert recall_lines and all("client" not in ln.get("extra", {}) and "client" not in ln for ln in recall_lines)


# --- 이슈 #107-1: 이름 없는 initialize가 이름 있는 세션의 LRU 칸을 밀어내지 않는다 -----------------

def test_nameless_initialize_does_not_occupy_an_lru_slot(gateway):
    client, ledger_path, _, _ = gateway
    sessions = client.app.state.mcp_clients
    sessions.__init__(limit=2)                                 # 자리 둘짜리 저장소: 이름 있는 세션 둘이 꽉 채운다
    sessions.bind(_fingerprint("sess-a"), "client-a")
    sessions.bind(_fingerprint("sess-b"), "client-b")
    _initialize(client, None)                                  # 이름 없는 initialize (세션 SESSION)

    assert len(sessions) == 2                                  # 이름 없는 세션은 칸을 차지하지 않는다
    _recall(client, "mcp", "sess-a")
    _recall(client, "mcp", "sess-b")
    _recall(client, "mcp", SESSION)
    first, second, third = _recall_rows(ledger_path)
    assert (first["client"], second["client"]) == ("client-a", "client-b")   # 밀려나지 않았다
    assert third["client"] is None                                           # 이름 없는 세션은 여전히 null


def test_nameless_initialize_keeps_the_nameless_vs_unknown_distinction(gateway):
    """506e0e8: 이름 없음(None)과 실제 이름 "unknown"은 계속 다르다 (원장 null vs "unknown")."""
    client, _, _, _ = gateway
    sessions = client.app.state.mcp_clients
    sessions.bind(_fingerprint("sess-literal"), "unknown")
    sessions.bind(_fingerprint("sess-nameless"), None)

    assert sessions.peek(_fingerprint("sess-literal")) == "unknown"
    assert sessions.peek(_fingerprint("sess-nameless")) is None
    assert sessions.lookup(_fingerprint("sess-literal")) == "unknown"
    assert sessions.lookup(_fingerprint("sess-nameless")) == "unknown"     # 감사 줄은 둘 다 "unknown"


def test_nameless_reinitialize_still_keeps_the_known_name(gateway):
    client, _, _, _ = gateway
    sessions = client.app.state.mcp_clients
    sessions.bind(_fingerprint(SESSION), "claude-code")
    _initialize(client, None)                                  # 같은 세션의 이름 없는 재-initialize

    assert sessions.peek(_fingerprint(SESSION)) == "claude-code"


# --- 이슈 #107-2: client는 요청 도착 시점에 고정한다 (업스트림 대기 중 재-initialize 무시) --------

def test_ledger_client_is_pinned_on_arrival_not_after_the_upstream_await(gateway):
    client, ledger_path, _, clock = gateway
    sessions = client.app.state.mcp_clients
    sessions.bind(_fingerprint(SESSION), "old-client")
    clock.on_upstream = lambda: sessions.bind(_fingerprint(SESSION), "new-client")   # 대기 중 재-initialize

    _recall(client, "mcp", SESSION)

    (row,) = _recall_rows(ledger_path)
    assert sessions.peek(_fingerprint(SESSION)) == "new-client"            # 재-initialize는 실제로 일어났다
    assert row["client"] == "old-client"                                   # 도착 시점의 이름이 기록된다
