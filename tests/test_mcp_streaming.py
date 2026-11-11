"""MCP responses that need no gateway rewrite stream through: headers + first byte leave
before upstream finishes.

Why (2026-09-26): Claude Code aborts an MCP HTTP POST whose response HEADERS do not arrive
within max(60s, MCP_TIMEOUT) ("The operation timed out." - a first-byte budget, cleared once
fetch resolves). The gateway used to read upstream's whole body before answering, so any
reflect longer than 60s failed client-side although upstream completed it. Upstream keeps the
SSE stream alive with ': ping' comments; the gateway must pass them on as they come.

TestClient buffers whole responses, so this runs the real app under uvicorn on a loopback
port and holds the mock upstream mid-stream until the client has seen the first bytes.
"""
import asyncio
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from gateway import unescape
from gateway.acl import ACL
from gateway.audit import Audit
from gateway.app import create_app
from tests.conftest import ACL_DATA, UPSTREAM_KEY

PING = b": ping - 2026-09-26 00:00:00\r\n\r\n"
RESULT = {"jsonrpc": "2.0", "id": 7, "result": {"content": [{"type": "text", "text": json.dumps(
    {"text": "answer", "based_on": {"mental-models": [{"id": "demo-rules-x"}]}})}], "isError": False}}
FINAL = b"event: message\r\ndata: " + json.dumps(RESULT).encode() + b"\r\n\r\n"


class HeldUpstream:
    """Mock upstream MCP: sends one SSE ping, then blocks until `release` is set.

    `after` (chunks sent once released) and `media` are per-test knobs (issue #26)."""

    def __init__(self):
        self.release = threading.Event()
        self.after = [FINAL]
        self.media = "text/event-stream"
        self.sent = []          # tools/call arguments as they reached upstream (issue #55)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "healthy"})
        release, after = self.release, list(self.after)
        try:
            self.sent.append(json.loads(request.content)["params"]["arguments"])
        except (ValueError, KeyError, TypeError):
            pass
        first = PING if self.media == "text/event-stream" else b""

        async def body():
            if first:
                yield first
            await asyncio.to_thread(release.wait, 20)
            for chunk in after:
                yield chunk

        return httpx.Response(200, headers={"content-type": self.media,
                                            "mcp-session-id": "sess-up"}, content=body())


@pytest.fixture
def served(tmp_path):
    upstream = HeldUpstream()
    audit_path = str(tmp_path / "audit.log")
    app = create_app(
        acl=ACL.from_dict(ACL_DATA), audit=Audit(path=audit_path), upstream_key=UPSTREAM_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler),
                                      base_url="http://upstream", timeout=30.0))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}", upstream, audit_path
    upstream.release.set()
    server.should_exit = True
    thread.join(5)


def post_reflect(base):
    body = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "reflect", "arguments": {"query": "q", "tags": ["project:x"],
                                      "include_based_on": True}}}
    return httpx.stream("POST", f"{base}/mcp/personal-alice/", json=body, timeout=10.0,
                        headers={"authorization": "Bearer tok-alice",
                                 "accept": "application/json, text/event-stream"})


def post_reflect_default(base):
    """A reflect call that leaves include_based_on out: the gateway fills it (issue #55)."""
    body = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "reflect", "arguments": {"query": "q", "tags": ["project:x"]}}}
    return httpx.stream("POST", f"{base}/mcp/personal-alice/", json=body, timeout=10.0,
                        headers={"authorization": "Bearer tok-alice",
                                 "accept": "application/json, text/event-stream"})


def test_reflect_headers_and_first_byte_arrive_before_upstream_finishes(served):
    base, upstream, _ = served
    with post_reflect(base) as resp:          # returns once headers arrive
        assert not upstream.release.is_set()  # upstream is still mid-stream
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers.get("mcp-session-id") == "sess-up"
        chunks = resp.iter_raw()
        first = next(chunks)
        assert first.startswith(b": ping")    # the keep-alive reached the client live
        assert not upstream.release.is_set()
        upstream.release.set()
        rest = b"".join(chunks)
    assert json.dumps(RESULT).encode() in first + rest


def test_streamed_reflect_still_records_demand_telemetry(served):
    base, upstream, audit_path = served
    upstream.release.set()
    with post_reflect(base) as resp:
        resp.read()
    deadline = time.time() + 5
    events = []
    while time.time() < deadline and not events:
        with open(audit_path, encoding="utf-8") as fh:
            events = [json.loads(line) for line in fh if '"reflect-demand"' in line]
        time.sleep(0.05)
    assert len(events) == 1
    r = events[0]["reflect"]
    assert r["ok"] is True and r["transport"] == "mcp" and r["tags"] == ["project:x"]
    assert r["mental_models"] == ["demo-rules-x"]


# --- issue #26: reflect's Korean \uXXXX escapes are undone per SSE event, still streaming ---

KO_VALUE = {"text": "게이트웨이는 규칙을 강제한다.\n- \"금지\" 표현 없음 😀",
            "based_on": {"mental-models": [{"id": "demo-rules-x"}]}}
KO_TEXT = json.dumps(KO_VALUE, indent=2)            # upstream default: ensure_ascii=True


def _ko_result(el_id=7, is_error=False):
    return {"jsonrpc": "2.0", "id": el_id, "result": {
        "_meta": {}, "content": [{"type": "text", "text": KO_TEXT}],
        "structuredContent": {"result": KO_TEXT}, "isError": is_error}}


def _event(message) -> bytes:
    return b"event: message\r\ndata: " + json.dumps(message).encode() + b"\r\n\r\n"


KO_FINAL = _event(_ko_result())


def _data_messages(raw: bytes) -> list:
    return [json.loads(ln[len("data:"):]) for ln in raw.decode("utf-8").splitlines()
            if ln.startswith("data:")]


def _read_all(base, upstream):
    upstream.release.set()
    with post_reflect(base) as resp:
        return b"".join(resp.iter_raw())


def _rewrite_failures(base):
    return httpx.get(f"{base}/healthz", timeout=5.0).json()["rewrite_failures"]


def _assert_unescaped(message):
    result = message["result"]
    text = result["content"][0]["text"]
    structured = result["structuredContent"]["result"]
    assert "\\u" not in text and "\\u" not in structured
    assert "게이트웨이" in text and structured == text
    assert json.loads(text) == KO_VALUE and json.loads(structured) == KO_VALUE


def test_a_korean_reflect_still_streams_the_first_ping_live(served):
    base, upstream, _ = served
    upstream.after = [KO_FINAL]
    with post_reflect(base) as resp:
        assert not upstream.release.is_set()
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers.get("mcp-session-id") == "sess-up"
        chunks = resp.iter_raw()
        first = next(chunks)
        assert first == PING                   # the ping leaves before upstream's next byte
        assert not upstream.release.is_set()
        upstream.release.set()
        rest = b"".join(chunks)
    [message] = _data_messages(rest)
    _assert_unescaped(message)


def test_b_reflect_text_and_structured_result_are_unescaped_losslessly(served):
    base, upstream, _ = served
    upstream.after = [KO_FINAL]
    raw = _read_all(base, upstream)
    assert raw.startswith(PING)
    [message] = _data_messages(raw)
    _assert_unescaped(message)
    assert message["result"]["_meta"] == {} and message["result"]["isError"] is False
    assert _rewrite_failures(base) == 0


@pytest.mark.parametrize("cuts", [(5, 40), (len(KO_FINAL) - 3, len(KO_FINAL) - 1),
                                  (len(KO_FINAL) - 4, len(KO_FINAL) - 2)])
def test_c_event_split_across_chunks_gives_the_same_result(served, cuts):
    base, upstream, _ = served
    a, b = cuts
    upstream.after = [KO_FINAL[:a], KO_FINAL[a:b], KO_FINAL[b:]]   # incl. a CR|LF split
    raw = _read_all(base, upstream)
    [message] = _data_messages(raw)
    _assert_unescaped(message)
    assert raw.count(b"\r\n\r\n") == 2                          # no spurious boundary


def test_d_broken_data_event_fails_open_and_is_counted(served):
    base, upstream, audit_path = served
    broken = b"event: message\r\ndata: {not json\r\n\r\n"
    upstream.after = [broken, KO_FINAL]
    before = _rewrite_failures(base)
    raw = _read_all(base, upstream)
    assert raw.startswith(PING + broken)       # original bytes, not dropped
    [message] = _data_messages(raw[len(PING + broken):])
    _assert_unescaped(message)                  # one bad event does not poison the next
    assert _rewrite_failures(base) == before + 1
    with open(audit_path, encoding="utf-8") as fh:
        alarms = [json.loads(ln) for ln in fh if '"rewrite-failure"' in ln]
    assert len(alarms) == 1 and alarms[0]["reason"].startswith("response-rewrite:")


def test_d_unterminated_tail_is_passed_through_raw(served):
    base, upstream, _ = served
    tail = KO_FINAL[:-2]                       # upstream closed without the blank line
    upstream.after = [tail]
    assert _read_all(base, upstream) == PING + tail


def test_e_rollback_flag_keeps_reflect_bytes_identical(served, monkeypatch):
    monkeypatch.setenv(unescape.ENV_FLAG, "0")
    base, upstream, _ = served
    upstream.after = [KO_FINAL[:9], KO_FINAL[9:]]
    assert _read_all(base, upstream) == PING + KO_FINAL


def test_f_other_ids_and_error_results_keep_their_bytes(served):
    base, upstream, _ = served
    other = _event(_ko_result(el_id=99))
    error = _event(_ko_result(is_error=True))
    upstream.after = [other, error]
    assert _read_all(base, upstream) == PING + other + error


def test_json_media_reflect_is_unescaped_after_headers_go_out(served):
    base, upstream, _ = served
    upstream.media = "application/json"
    upstream.after = [json.dumps(_ko_result()).encode()]
    with post_reflect(base) as resp:
        assert not upstream.release.is_set()   # headers did not wait for the body
        assert resp.headers["content-type"].startswith("application/json")
        upstream.release.set()
        message = json.loads(resp.read())
    _assert_unescaped(message)


def test_reflect_batched_with_buffered_work_is_unescaped_too(served):
    base, upstream, _ = served
    upstream.after = [KO_FINAL]
    upstream.release.set()
    body = [{"jsonrpc": "2.0", "id": 7, "method": "tools/call",
             "params": {"name": "reflect", "arguments": {"query": "q", "tags": ["project:x"],
                                      "include_based_on": True}}},
            {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
             "params": {"name": "recall", "arguments": {"query": "q"}}}]
    resp = httpx.post(f"{base}/mcp/personal-alice/", json=body, timeout=10.0,
                      headers={"authorization": "Bearer tok-alice",
                               "accept": "application/json, text/event-stream"})
    [message] = _data_messages(resp.content)
    _assert_unescaped(message)


def test_exploding_event_fails_open_and_the_stream_continues(served):
    base, upstream, _ = served
    deep = b"event: message\r\ndata: " + b"[" * 100000 + b"]" * 100000 + b"\r\n\r\n"
    upstream.after = [deep, KO_FINAL]
    before = _rewrite_failures(base)
    raw = _read_all(base, upstream)
    assert raw.startswith(PING + deep)
    [message] = _data_messages(raw[len(PING + deep):])
    _assert_unescaped(message)
    assert _rewrite_failures(base) == before + 1


def test_cr_only_final_event_at_eof_is_unescaped(served):
    base, upstream, _ = served
    upstream.after = [KO_FINAL.replace(b"\r\n", b"\r")]
    raw = _read_all(base, upstream)
    [message] = _data_messages(raw[len(PING):].replace(b"\r", b"\n"))
    _assert_unescaped(message)


# --- issue #55: the default (filled) reflect keeps streaming and loses only its based_on ---

def test_default_reflect_streams_live_and_hides_the_filled_based_on(served):
    base, upstream, audit_path = served
    upstream.after = [KO_FINAL[:9], KO_FINAL[9:]]
    with post_reflect_default(base) as resp:      # returns once headers arrive
        assert not upstream.release.is_set()      # headers did not wait for upstream to finish
        assert resp.status_code == 200
        chunks = resp.iter_raw()
        first = next(chunks)
        assert first.startswith(b": ping")        # the keep-alive reached the client live
        assert not upstream.release.is_set()
        upstream.release.set()
        rest = b"".join(chunks)
    assert upstream.sent[-1]["include_based_on"] is True        # the gateway asked upstream for it
    [message] = _data_messages(first + rest)
    _assert_unescaped_without_based_on(message)
    deadline = time.time() + 5
    events = []
    while time.time() < deadline and not events:
        with open(audit_path, encoding="utf-8") as fh:
            events = [json.loads(line) for line in fh if '"reflect-demand"' in line]
        time.sleep(0.05)
    assert [e["reflect"]["based_on"] for e in events] == ["hit"]     # judged on upstream's original
    assert events[0]["reflect"]["mental_models"] == ["demo-rules-x"]
    assert _rewrite_failures(base) == 0


def _assert_unescaped_without_based_on(message):
    result = message["result"]
    text = result["content"][0]["text"]
    assert "based_on" not in text and "based_on" not in result["structuredContent"]["result"]
    assert json.loads(text) == {k: v for k, v in KO_VALUE.items() if k != "based_on"}
    assert result["structuredContent"]["result"] == text and "\\u" not in text


def test_default_reflect_with_unescape_off_is_passed_through_unfilled(served, monkeypatch):
    """The #26 emergency lever (HC_UNESCAPE=0) must give back old bytes for a plain reflect call too."""
    monkeypatch.setenv(unescape.ENV_FLAG, "0")
    base, upstream, _ = served
    upstream.after = [KO_FINAL[:9], KO_FINAL[9:]]
    upstream.release.set()
    with post_reflect_default(base) as resp:
        raw = b"".join(resp.iter_raw())
    assert raw == PING + KO_FINAL
    assert "include_based_on" not in upstream.sent[-1]
