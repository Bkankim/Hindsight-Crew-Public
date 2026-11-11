"""N5: 응답측 실패 정직화 + 성공 주석 + rewrite_failures 배선 (게이트웨이 왕복 E2E).

실사고 ②("성공 프레임 속 실패 본문"을 모델이 성공으로 읽음)의 재발면을 게이트웨이에서 닫는다.
계약 4종:
  1. 성공 프레임에 실패 본문이 담겨 오면 `result.isError:true` 로 정직화한다.
  2. 오탐 금지 - 정상 회고 본문에 'failed' 단어가 들어 있다고 변환하지 않는다(바이트 동일 회귀).
  3. 비동기 retain(operation_id, 미완료)은 get_operation 안내를 동봉한다(P5 조용한 실패 금지).
  4. 요청측 autofill 이 있었으면 성공 응답에 `[gateway: timestamp 자동 기록 <값>]` 을 동봉한다.
그리고 어떤 실패든 fail-open: 원본 바이트 그대로 + `/healthz` `rewrite_failures` 카운터.

conftest 의 Recorder 는 고정 응답이라 응답측 검증에 못 쓴다. 여기서는 테스트마다 upstream
응답을 지정하는 로컬 MockTransport 를 세운다(conftest 는 무수정).
"""
import json
import os
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import ACL_DATA, FIXTURES_DIR, UPSTREAM_KEY
from gateway.acl import ACL
from gateway.app import create_app
from gateway.audit import Audit

SSE_CT = "text/event-stream"
JSON_CT = "application/json"
AUTOFILL_RE = re.compile(r"^\[gateway: timestamp 자동 기록 (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\]$")


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------

def read_raw(name):
    with open(os.path.join(FIXTURES_DIR, "mcp", name + ".raw"), "rb") as fh:
        return fh.read()


def sse(payload):
    """upstream v0.8.3 프레이밍 그대로: event/data 1프레임 + CRLF 종결."""
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    return b"event: message\r\ndata: " + body + b"\r\n\r\n"


def sse_payload(raw):
    lines = [ln[len("data:"):].strip() for ln in raw.decode("utf-8").splitlines()
             if ln.startswith("data:")]
    return json.loads(lines[-1])


def texts(result):
    return [c["text"] for c in result.get("content", []) if c.get("type") == "text"]


class Upstream:
    """지정한 (content_type, body) 를 돌려주고 전달된 요청을 기록하는 mock upstream."""

    def __init__(self, body, content_type=SSE_CT, status=200):
        self.body = body
        self.content_type = content_type
        self.status = status
        self.calls = []

    def handler(self, request):
        try:
            self.calls.append(json.loads(request.content.decode("utf-8")))
        except (ValueError, UnicodeDecodeError):
            self.calls.append(None)
        return httpx.Response(
            self.status, content=self.body,
            headers={"content-type": self.content_type, "mcp-session-id": "sess-upstream-1"},
        )

    @property
    def last(self):
        return self.calls[-1] if self.calls else None


def make_client(upstream, tmp_path):
    http = httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler),
                             base_url="http://upstream", timeout=5.0)
    app = create_app(acl=ACL.from_dict(ACL_DATA), audit=Audit(path=str(tmp_path / "audit.log")),
                     upstream_key=UPSTREAM_KEY, http_client=http)
    return TestClient(app)


def retain_call(el_id=1, timestamp="2026-07-26T00:00:00Z", tool="retain", content="[note] 왕복 검증"):
    args = {"content": content}
    if timestamp is not None:
        args["timestamp"] = timestamp
    return {"jsonrpc": "2.0", "id": el_id, "method": "tools/call",
            "params": {"name": tool, "arguments": args}}


def post(client, body, bank="personal-alice", token="tok-alice"):
    return client.post("/mcp/%s/" % bank, content=json.dumps(body),
                       headers={"content-type": "application/json",
                                "authorization": "Bearer %s" % token})


# --------------------------------------------------------------------------
# ① 성공 프레임 속 실패 본문 -> isError:true (실사고 ② 재발 차단)
# --------------------------------------------------------------------------

def test_failure_body_in_success_frame_becomes_is_error(tmp_path):
    upstream = Upstream(sse({
        "jsonrpc": "2.0", "id": 1,
        "result": {
            # 실패 형상(fixtures/mcp/NOTES.md ④): structuredContent 부재 + 실패 텍스트
            "content": [{"type": "text", "text": "Error: bank quota exceeded, nothing was stored"}],
            "isError": False,
        },
    }))
    resp = post(make_client(upstream, tmp_path), retain_call())

    assert resp.status_code == 200
    result = sse_payload(resp.content)["result"]
    assert result["isError"] is True                      # 정직화됨
    assert texts(result)[0].startswith("Error: bank quota")   # 원문 보존
    assert resp.headers["content-type"].startswith(SSE_CT)    # 프레이밍 유지


def test_operation_failed_phrase_is_also_honestified(tmp_path):
    upstream = Upstream(sse({
        "jsonrpc": "2.0", "id": 1,
        "result": {"content": [{"type": "text", "text": "retain operation failed (upstream 5xx)"}]},
    }))
    resp = post(make_client(upstream, tmp_path), retain_call())
    assert sse_payload(resp.content)["result"]["isError"] is True


def test_already_is_error_response_is_untouched(tmp_path):
    raw = read_raw("sync_retain_fail")          # upstream 실측 실패 응답 (isError 이미 true)
    upstream = Upstream(raw)
    resp = post(make_client(upstream, tmp_path), retain_call(el_id=4, tool="sync_retain"))

    assert resp.content == raw                  # 이미 정직한 응답에는 손대지 않는다
    assert sse_payload(resp.content)["result"]["isError"] is True


# --------------------------------------------------------------------------
# ② 오탐 회귀 - 정상 본문 속 'failed' 는 실패가 아니다
# --------------------------------------------------------------------------

def test_benign_failed_word_in_body_is_not_converted(tmp_path):
    benign = sse({
        "jsonrpc": "2.0", "id": 1,
        "result": {
            "content": [{"type": "text", "text":
                         "저장한 회고: the deployment failed last week, 원인은 캐시 무효화 누락"}],
            "isError": False,
        },
    })
    upstream = Upstream(benign)
    resp = post(make_client(upstream, tmp_path), retain_call())

    assert resp.content == benign                             # 바이트 동일 = 무변조
    assert sse_payload(resp.content)["result"]["isError"] is False


@pytest.mark.parametrize("text", [
    "저장 완료. failed_count=0 이며 재시도는 없었습니다",
    "Errors 필드는 비어 있습니다",
    "'operation' 이라는 단어와 'failed' 라는 단어가 따로 있음",
    "메모리 저장 성공",
])
def test_conservative_heuristic_does_not_fire_on_lookalikes(tmp_path, text):
    body = sse({"jsonrpc": "2.0", "id": 1,
                "result": {"content": [{"type": "text", "text": text}], "isError": False}})
    resp = post(make_client(Upstream(body), tmp_path), retain_call())
    assert resp.content == body


def test_structured_content_present_never_converts(tmp_path):
    # 성공의 구조 신호(structuredContent)가 있으면 텍스트 휴리스틱은 아예 발동하지 않는다.
    body = sse({
        "jsonrpc": "2.0", "id": 3,
        "result": {
            "content": [{"type": "text", "text": "Error-looking text but structured success"}],
            "structuredContent": {"status": "completed", "memory_ids": ["m-1"]},
            "isError": False,
        },
    })
    resp = post(make_client(Upstream(body), tmp_path), retain_call(el_id=3))
    assert resp.content == body


# --------------------------------------------------------------------------
# ③ 비동기 retain 안내 (operation_id + get_operation)
# --------------------------------------------------------------------------

def test_async_retain_response_carries_get_operation_guidance(tmp_path):
    raw = read_raw("retain_async_ok")           # id=5, status=accepted, operation_id 는 채집마다 다르다
    operation_id = sse_payload(raw)["result"]["structuredContent"]["operation_id"]
    upstream = Upstream(raw)
    resp = post(make_client(upstream, tmp_path), retain_call(el_id=5))

    result = sse_payload(resp.content)["result"]
    assert result["isError"] is False
    note = texts(result)[-1]
    assert note == ("[gateway: async retain - operation_id="
                    + operation_id + ", get_operation으로 완료를 확인하세요]")
    # 원문 content 는 JSON 문자열이라 그 안에 끼워 넣으면 클라이언트 파싱이 깨진다 -> 별도 블록.
    assert json.loads(texts(result)[0])["status"] == "accepted"
    assert resp.content.startswith(b"event: message\r\ndata: ") and resp.content.endswith(b"\r\n\r\n")


def test_completed_operation_gets_no_async_guidance(tmp_path):
    raw = read_raw("sync_retain_ok")            # status=completed + memory_ids -> 완료 신호
    resp = post(make_client(Upstream(raw), tmp_path), retain_call(el_id=3, tool="sync_retain"))

    result = sse_payload(resp.content)["result"]
    assert all("async retain" not in t for t in texts(result))
    assert resp.content == raw


# --------------------------------------------------------------------------
# ④ autofill 주석 (P5 조용한 실패 금지)
# --------------------------------------------------------------------------

def test_autofilled_timestamp_is_annotated_on_success(tmp_path):
    upstream = Upstream(read_raw("sync_retain_ok"))     # id=3
    resp = post(make_client(upstream, tmp_path),
                retain_call(el_id=3, tool="sync_retain", timestamp=None))

    forwarded = upstream.last["params"]["arguments"]["timestamp"]
    note = texts(sse_payload(resp.content)["result"])[-1]
    m = AUTOFILL_RE.match(note)
    assert m, note
    assert m.group(1) == forwarded          # 주석 값 = 실제로 upstream 에 기록된 값


def test_no_autofill_means_no_annotation(tmp_path):
    raw = read_raw("sync_retain_ok")
    upstream = Upstream(raw)
    resp = post(make_client(upstream, tmp_path),
                retain_call(el_id=3, tool="sync_retain", timestamp="2026-07-26T00:00:00Z"))

    assert upstream.last["params"]["arguments"]["timestamp"] == "2026-07-26T00:00:00Z"
    assert resp.content == raw


def test_async_and_autofill_annotations_coexist(tmp_path):
    upstream = Upstream(read_raw("retain_async_ok"))    # id=5
    resp = post(make_client(upstream, tmp_path), retain_call(el_id=5, timestamp=None))

    tail = texts(sse_payload(resp.content)["result"])[-2:]
    assert tail[0].startswith("[gateway: async retain - operation_id=")
    assert AUTOFILL_RE.match(tail[1])


# --------------------------------------------------------------------------
# ⑤ fail-open + rewrite_failures 카운터
# --------------------------------------------------------------------------

def test_unparseable_response_passes_through_and_counts_failure(tmp_path):
    garbage = b'event: message\r\ndata: {"jsonrpc":"2.0","id":1,"result":\r\n\r\n'
    client = make_client(Upstream(garbage), tmp_path)

    assert client.get("/healthz").json()["rewrite_failures"] == 0
    resp = post(client, retain_call())

    assert resp.content == garbage              # fail-open: 원본 바이트 그대로
    assert resp.status_code == 200
    health = client.get("/healthz").json()
    assert health["ok"] is True                 # 기존 키 무변경
    assert health["rewrite_failures"] == 1


def test_rewrite_failure_is_audited(tmp_path):
    garbage = b'event: message\r\ndata: {oops\r\n\r\n'
    post(make_client(Upstream(garbage), tmp_path), retain_call())

    with open(str(tmp_path / "audit.log"), encoding="utf-8") as fh:
        lines = [json.loads(ln) for ln in fh if ln.strip()]
    alarms = [ln for ln in lines if ln["decision"] == "rewrite-failure"]
    assert len(alarms) == 1
    assert "sse" in alarms[0]["reason"]
    assert alarms[0]["resolved_bank"] == "personal-alice"


def test_healthz_counter_does_not_move_on_clean_roundtrip(tmp_path):
    client = make_client(Upstream(read_raw("sync_retain_ok")), tmp_path)
    post(client, retain_call(el_id=3, tool="sync_retain"))
    assert client.get("/healthz").json()["rewrite_failures"] == 0


# --------------------------------------------------------------------------
# ⑥ 변조 왕복 산출물의 mcp 공식 SDK 소비 가능성 (SSE 프레이밍 유지)
# --------------------------------------------------------------------------

def test_rewritten_sse_response_is_consumable_by_mcp_sdk(tmp_path):
    pytest.importorskip(
        "mcp.types",
        reason="mcp 공식 SDK 는 requirements-dev.txt(python>=3.10) 전용 - 로컬 .venv 는 3.9",
    )
    from mcp.types import CallToolResult, JSONRPCResponse

    upstream = Upstream(sse({
        "jsonrpc": "2.0", "id": 5,
        "result": {"content": [{"type": "text", "text": "Error: upstream refused"}], "isError": False},
    }))
    resp = post(make_client(upstream, tmp_path), retain_call(el_id=5))

    rpc = JSONRPCResponse.model_validate(sse_payload(resp.content))
    assert rpc.id == 5
    result = CallToolResult.model_validate(rpc.result)
    assert result.isError is True
    assert result.content[0].text == "Error: upstream refused"


# --------------------------------------------------------------------------
# ⑦ 비 retain 응답 무변조 (파싱 시도 자체 없음)
# --------------------------------------------------------------------------

def test_recall_response_is_never_rewritten(tmp_path):
    # recall 응답이 실패 신호를 그대로 담고 있어도 게이트웨이는 손대지 않는다.
    body = sse({"jsonrpc": "2.0", "id": 1,
                "result": {"content": [{"type": "text", "text": "Error: no matches"}],
                           "isError": False}})
    client = make_client(Upstream(body), tmp_path)
    resp = post(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "recall", "arguments": {"query": "x"}}})

    assert resp.content == body
    assert client.get("/healthz").json()["rewrite_failures"] == 0


def test_non_retain_garbage_response_is_not_even_parsed(tmp_path):
    garbage = b'event: message\r\ndata: {"jsonrpc":\r\n\r\n'
    client = make_client(Upstream(garbage), tmp_path)
    resp = post(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})

    assert resp.content == garbage
    assert client.get("/healthz").json()["rewrite_failures"] == 0   # 파싱 자체를 안 했다


def test_batch_rewrites_only_the_retain_element(tmp_path):
    failing = {"content": [{"type": "text", "text": "Error: upstream refused"}], "isError": False}
    body = sse([
        {"jsonrpc": "2.0", "id": 1, "result": dict(failing)},
        {"jsonrpc": "2.0", "id": 2, "result": dict(failing)},
    ])
    resp = post(make_client(Upstream(body), tmp_path), [
        retain_call(el_id=1),
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "recall", "arguments": {"query": "x"}}},
    ])

    got = sse_payload(resp.content)
    assert isinstance(got, list) and len(got) == 2          # shape 보존
    assert got[0]["result"]["isError"] is True              # retain 만 정직화
    assert got[1]["result"]["isError"] is False             # recall 은 무변조


# --------------------------------------------------------------------------
# ⑧ JSON-RPC error 응답 통과 (프로토콜 에러는 건드리지 않는다)
# --------------------------------------------------------------------------

def test_jsonrpc_error_response_passes_through(tmp_path):
    body = sse({"jsonrpc": "2.0", "id": 1,
                "error": {"code": -32602, "message": "Invalid params"}})
    client = make_client(Upstream(body), tmp_path)
    resp = post(client, retain_call())

    assert resp.content == body
    assert client.get("/healthz").json()["rewrite_failures"] == 0


def test_jsonrpc_error_is_not_annotated_even_with_autofill(tmp_path):
    body = sse({"jsonrpc": "2.0", "id": 1,
                "error": {"code": -32603, "message": "Internal error"}})
    resp = post(make_client(Upstream(body), tmp_path), retain_call(timestamp=None))

    assert resp.content == body     # autofill 주석은 성공 응답에만


# --------------------------------------------------------------------------
# ⑥ 태그 정규화 주석 (P5 조용한 정정 금지)
# --------------------------------------------------------------------------

def strict_client(upstream, tmp_path):
    """엄격 프로필(personal-alice)을 실제 정책 파일에서 주입한 클라이언트."""
    import os
    from gateway.bank_policies import BankPolicies

    http = httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler),
                             base_url="http://upstream", timeout=5.0)
    repo_root = os.path.dirname(os.path.dirname(__file__))
    app = create_app(acl=ACL.from_dict(ACL_DATA), audit=Audit(path=str(tmp_path / "audit.log")),
                     upstream_key=UPSTREAM_KEY, http_client=http,
                     bank_policies=BankPolicies.from_file(
                         os.path.join(repo_root, "tests", "fixtures", "policies", "sample", "bank-policies.json")))
    return TestClient(app)


def tagged_retain_call(tags, el_id=3, tool="sync_retain"):
    body = retain_call(el_id=el_id, tool=tool, content="[note] 태그 정규화 왕복")
    body["params"]["arguments"]["tags"] = tags
    return body


def test_normalized_tags_are_annotated_on_success(tmp_path):
    upstream = Upstream(read_raw("sync_retain_ok"))     # id=3
    resp = post(strict_client(upstream, tmp_path),
                tagged_retain_call(["skills", "kind:decision"]))

    assert upstream.last["params"]["arguments"]["tags"] == ["topic:skills", "kind:decision"]
    note = texts(sse_payload(resp.content)["result"])[-1]
    assert note == "[gateway: 태그 정규화 skills -> topic:skills]"


def test_no_tag_rewrite_means_no_normalization_note(tmp_path):
    upstream = Upstream(read_raw("sync_retain_ok"))
    resp = post(strict_client(upstream, tmp_path),
                tagged_retain_call(["project:hindsight-crew"]))

    assert all("태그 정규화" not in t for t in texts(sse_payload(resp.content)["result"]))
