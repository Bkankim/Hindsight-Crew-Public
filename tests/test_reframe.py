"""N4: 응답 리프레이머 순수 모듈 (SSE/JSON 프레이밍 왕복).

계약 3종:
  1. 무변환 왕복은 **바이트 동일** (SSE 개행·프레임 경계·비 data 라인 포함).
  2. 변조 왕복 산출물은 mcp 공식 SDK 타입으로 소비 가능 (SSE 세션 응답 / JSON 단발 둘 다).
  3. 파싱 실패는 예외가 아니라 실패 표식 - 원본 바이트가 그대로 보존되어 호출자가 fail-open 가능.

fixtures/mcp/*.raw 는 upstream v0.8.3 실측 원문(N0). initialize.raw 는 LF,
나머지는 CRLF 프레이밍이라 개행 보존 회귀를 양쪽으로 잡는다.
"""
import json
import os

import pytest

from conftest import FIXTURES_DIR, load_mcp_fixture
from gateway import reframe

SSE_CT = "text/event-stream"
JSON_CT = "application/json"

SSE_FIXTURES = ["tools_list", "initialize", "sync_retain_ok"]


def read_raw(name):
    with open(os.path.join(FIXTURES_DIR, "mcp", name + ".raw"), "rb") as fh:
        return fh.read()


def annotate(element):
    """N5 변조의 최소 모형: result.content[] 에 게이트웨이 주석 1건 추가 (in-place 변형)."""
    result = element.get("result")
    if isinstance(result, dict) and isinstance(result.get("content"), list):
        result["content"].append({"type": "text", "text": "[gateway: annotated]"})
    return element


def sdk_types():
    pytest.importorskip(
        "mcp.types",
        reason="mcp 공식 SDK 는 requirements-dev.txt(python>=3.10) 전용 - 로컬 .venv 는 3.9",
    )
    from mcp.types import CallToolResult, JSONRPCResponse

    return JSONRPCResponse, CallToolResult


def sse_payload(body):
    """직렬화 산출 SSE 바이트에서 마지막 data 페이로드를 꺼낸다(테스트 소비용)."""
    text = body.decode("utf-8")
    lines = [ln[len("data:"):].strip() for ln in text.splitlines() if ln.startswith("data:")]
    return json.loads(lines[-1])


# --------------------------------------------------------------------------
# 1. 무변환 왕복 = 바이트 동일
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", SSE_FIXTURES)
def test_sse_fixture_roundtrip_is_byte_identical(name):
    raw = read_raw(name)
    parsed = reframe.parse_response(SSE_CT, raw)

    assert parsed.kind == reframe.KIND_SSE
    assert parsed.failure is None
    ct, out = reframe.serialize(parsed)
    assert ct == SSE_CT
    assert out == raw


@pytest.mark.parametrize("name", SSE_FIXTURES)
def test_sse_fixture_payload_matches_conftest_loader(name):
    parsed = reframe.parse_response(SSE_CT, read_raw(name))
    payloads = [f.payload for f in parsed.frames if f.payload is not None]
    assert len(payloads) == 1
    assert payloads[0] == load_mcp_fixture(name)


@pytest.mark.parametrize("name", SSE_FIXTURES)
def test_identity_transform_keeps_bytes_identical(name):
    raw = read_raw(name)
    parsed = reframe.parse_response(SSE_CT, raw)
    out = reframe.serialize(reframe.transform_results(parsed, lambda el: el))[1]
    assert out == raw


def test_line_terminator_style_is_preserved_per_fixture():
    # initialize 는 LF, sync_retain_ok 는 CRLF 로 채집됐다 - 개행 정규화 금지.
    assert read_raw("initialize").endswith(b"}}}\n\n")
    assert read_raw("sync_retain_ok").endswith(b"}}\r\n\r\n")
    for name, tail in (("initialize", b"\n\n"), ("sync_retain_ok", b"\r\n\r\n")):
        parsed = reframe.parse_response(SSE_CT, read_raw(name))
        out = reframe.serialize(reframe.transform_results(parsed, annotate))[1]
        assert out.endswith(tail)
        assert out.startswith(b"event: message" + tail[: len(tail) // 2])


def test_json_body_roundtrip_is_byte_identical():
    raw = b'{"jsonrpc": "2.0",  "id": 9, "result": {"ok": true}}\n'  # 여백/개행 포함 원문
    parsed = reframe.parse_response(JSON_CT, raw)
    assert parsed.kind == reframe.KIND_JSON
    assert reframe.serialize(parsed) == (JSON_CT, raw)


def test_non_data_lines_and_frame_order_are_preserved():
    raw = (
        b": ping\n"
        b"\n"
        b"event: message\n"
        b"id: 42\n"
        b"retry: 3000\n"
        b'data: {"jsonrpc":"2.0","id":1,"result":{"n":1}}\n'
        b"\n"
        b'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/message"}\n\n'
        b'event: message\ndata: {"jsonrpc":"2.0","id":2,"result":{"n":2}}'  # 종결 개행 없는 마지막 프레임
    )
    parsed = reframe.parse_response(SSE_CT, raw)
    assert parsed.failure is None
    assert len(parsed.frames) == 4  # ping 프레임 포함
    assert [f.payload for f in parsed.frames][0] is None
    assert reframe.serialize(parsed)[1] == raw

    seen = []

    def tag(el):
        seen.append(el["id"])
        el["result"]["tagged"] = True
        return el

    out = reframe.serialize(reframe.transform_results(parsed, tag))[1]
    assert seen == [1, 2]  # notification(id 없음)은 fn 미적용
    assert out.count(b'"tagged":true') == 2
    assert out.index(b": ping") == 0
    assert b"retry: 3000" in out
    assert out.index(b'"id":1') < out.index(b"notifications/message") < out.index(b'"id":2')


def test_multi_data_line_frame_joins_with_newline():
    raw = b'event: message\ndata: {"jsonrpc":"2.0","id":1,\ndata: "result":{"ok":true}}\n\n'
    parsed = reframe.parse_response(SSE_CT, raw)
    assert parsed.frames[0].payload == {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
    assert reframe.serialize(parsed)[1] == raw  # 무변환이면 다중 data 라인도 바이트 동일


def test_transformed_multi_data_line_frame_collapses_to_one_data_line():
    # 모듈 docstring 에 기록된 유일한 바이트 비동일 케이스(변조 시에만 발생).
    raw = b'event: message\ndata: {"jsonrpc":"2.0","id":1,\ndata: "result":{"content":[]}}\n\n'
    parsed = reframe.parse_response(SSE_CT, raw)
    out = reframe.serialize(reframe.transform_results(parsed, annotate))[1]
    assert out.count(b"data:") == 1
    assert sse_payload(out)["result"]["content"] == [{"type": "text", "text": "[gateway: annotated]"}]


# --------------------------------------------------------------------------
# 2. 빈 본문 (notifications/initialized = 202 + application/json + 0바이트)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("ct", [JSON_CT, SSE_CT, "", None])
def test_empty_body_passes_through(ct):
    parsed = reframe.parse_response(ct, b"")
    assert parsed.kind == reframe.KIND_EMPTY
    assert parsed.failure is None
    assert parsed.frames == ()
    out_ct, out = reframe.serialize(reframe.transform_results(parsed, annotate))
    assert out == b""
    assert out_ct == (ct or "")


def test_unknown_content_type_is_opaque_passthrough_not_failure():
    raw = b"\x89PNG\r\n\x1a\n"
    parsed = reframe.parse_response("image/png", raw)
    assert parsed.kind == reframe.KIND_OPAQUE
    assert parsed.failure is None  # 변조 비대상일 뿐 실패가 아니다
    assert reframe.serialize(reframe.transform_results(parsed, annotate))[1] == raw


# --------------------------------------------------------------------------
# 3. 변조 왕복 -> mcp 공식 SDK 소비 (SSE 세션 응답 / JSON 단발)
# --------------------------------------------------------------------------

def test_transformed_sse_response_is_consumable_by_mcp_sdk():
    JSONRPCResponse, CallToolResult = sdk_types()

    parsed = reframe.parse_response(SSE_CT, read_raw("sync_retain_ok"))
    ct, out = reframe.serialize(reframe.transform_results(parsed, annotate))
    assert ct == SSE_CT

    rpc = JSONRPCResponse.model_validate(sse_payload(out))
    assert rpc.id == 3
    result = CallToolResult.model_validate(rpc.result)
    assert result.isError is False
    assert result.content[-1].text == "[gateway: annotated]"
    assert result.structuredContent["memory_ids"] == sse_payload(read_raw("sync_retain_ok"))["result"]["structuredContent"]["memory_ids"]


def test_transformed_json_singleshot_response_is_consumable_by_mcp_sdk():
    # Adv3: SSE 세션 중에도 게이트웨이 합성 응답은 application/json 단발로 나간다.
    JSONRPCResponse, CallToolResult = sdk_types()

    raw = json.dumps({
        "jsonrpc": "2.0",
        "id": 7,
        "result": {
            "content": [{"type": "text", "text": "RULE:tags-grammar 위반"}],
            "isError": True,
        },
    }).encode("utf-8")
    parsed = reframe.parse_response(JSON_CT, raw)
    ct, out = reframe.serialize(reframe.transform_results(parsed, annotate))
    assert ct == JSON_CT

    rpc = JSONRPCResponse.model_validate(json.loads(out))
    result = CallToolResult.model_validate(rpc.result)
    assert result.isError is True
    assert [c.text for c in result.content] == ["RULE:tags-grammar 위반", "[gateway: annotated]"]


def test_batch_array_response_transforms_each_element_and_keeps_shape():
    JSONRPCResponse, CallToolResult = sdk_types()

    batch = [
        {"jsonrpc": "2.0", "id": 1, "result": {"content": [], "isError": False}},
        {"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info"}},
        {"jsonrpc": "2.0", "id": 2, "result": {"content": [], "isError": True}},
    ]
    raw = json.dumps(batch).encode("utf-8")
    parsed = reframe.parse_response(JSON_CT, raw)
    out = reframe.serialize(reframe.transform_results(parsed, annotate))[1]

    got = json.loads(out)
    assert isinstance(got, list) and len(got) == 3  # 배열 shape 보존
    assert got[1] == batch[1] or got[1]["method"] == "notifications/message"
    assert "result" not in got[1]
    for i in (0, 2):
        rpc = JSONRPCResponse.model_validate(got[i])
        assert CallToolResult.model_validate(rpc.result).content[0].text == "[gateway: annotated]"


def test_batch_array_inside_sse_frame_is_supported():
    batch = [
        {"jsonrpc": "2.0", "id": 1, "result": {"content": []}},
        {"jsonrpc": "2.0", "id": 2, "result": {"content": []}},
    ]
    raw = b"event: message\r\ndata: " + json.dumps(batch).encode("utf-8") + b"\r\n\r\n"
    parsed = reframe.parse_response(SSE_CT, raw)
    assert reframe.serialize(parsed)[1] == raw

    out = reframe.serialize(reframe.transform_results(parsed, annotate))[1]
    got = sse_payload(out)
    assert isinstance(got, list) and len(got) == 2
    assert all(el["result"]["content"][0]["text"] == "[gateway: annotated]" for el in got)


def test_server_initiated_request_is_not_treated_as_response():
    # id 는 있으나 method 를 가진 서버 발신 요청(sampling 등)에는 fn 미적용.
    raw = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "sampling/createMessage", "params": {},
    }).encode("utf-8")
    parsed = reframe.parse_response(JSON_CT, raw)
    calls = []
    out = reframe.serialize(reframe.transform_results(parsed, lambda el: calls.append(el) or el))[1]
    assert calls == []
    assert out == raw


# --------------------------------------------------------------------------
# 4. 실패 표식 + 원본 보존 (fail-open 은 호출자 책임)
# --------------------------------------------------------------------------

def test_broken_sse_payload_yields_failure_marker_and_preserves_bytes():
    raw = b'event: message\r\ndata: {"jsonrpc":"2.0","id":1,"result":\r\n\r\n'
    parsed = reframe.parse_response(SSE_CT, raw)
    assert parsed.kind == reframe.KIND_OPAQUE
    assert isinstance(parsed.failure, str) and parsed.failure  # 카운터 훅용 사유 문자열
    assert "sse" in parsed.failure
    assert parsed.raw == raw
    assert reframe.serialize(reframe.transform_results(parsed, annotate)) == (SSE_CT, raw)


def test_broken_json_body_yields_failure_marker_and_preserves_bytes():
    raw = b'{"jsonrpc":"2.0",'
    parsed = reframe.parse_response(JSON_CT, raw)
    assert parsed.kind == reframe.KIND_OPAQUE
    assert isinstance(parsed.failure, str) and "json" in parsed.failure
    assert reframe.serialize(parsed) == (JSON_CT, raw)


def test_truncated_fixture_injection_is_reported_not_raised():
    raw = read_raw("sync_retain_ok")[:-40]  # 프레임 중간 절단 주입
    parsed = reframe.parse_response(SSE_CT, raw)
    assert parsed.failure is not None
    assert reframe.serialize(parsed)[1] == raw


def test_transform_callback_error_is_captured_as_failure():
    raw = read_raw("sync_retain_ok")

    def boom(el):
        raise ValueError("nope")

    parsed = reframe.transform_results(reframe.parse_response(SSE_CT, raw), boom)
    assert parsed.failure is not None and "transform" in parsed.failure
    assert reframe.serialize(parsed)[1] == raw  # 원본 통과


def test_serialize_failure_is_reportable_and_falls_back_to_raw():
    raw = read_raw("sync_retain_ok")
    parsed = reframe.parse_response(SSE_CT, raw)

    def unserializable(el):
        el["result"]["content"] = {object()}  # JSON 직렬화 불가
        return el

    mutated = reframe.transform_results(parsed, unserializable)
    ct, body, reason = reframe.try_serialize(mutated)
    assert isinstance(reason, str) and "serialize" in reason
    assert (ct, body) == (SSE_CT, raw)
    assert reframe.serialize(mutated) == (SSE_CT, raw)  # 얇은 래퍼는 사유만 버린다


def test_module_has_no_global_mutable_state():
    # 카운터는 호출자 몫 - 모듈 전역에 가변 집계 상태가 없어야 한다.
    mutable = {
        k: v for k, v in vars(reframe).items()
        if not k.startswith("_") and isinstance(v, (list, dict, set))
    }
    assert mutable == {}
