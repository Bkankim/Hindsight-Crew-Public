"""reflect include_based_on 기본값의 채우기·지우기 규칙 (issue #55).

app.py 연결(요청 인자, 응답 경로 3종, audit)은 tests/test_reflect_based_on_default_app.py 가 고정한다.
여기는 순수 함수만 본다:
  - fill_include_based_on: 없음/null 일 때만 true, 명시값·비 dict·다른 도구·킬스위치 0 은 받은 객체 그대로,
  - strip_*: based_on 만 지우고 나머지 바이트(들여쓰기·이스케이프 포함)는 그대로, id 밖·isError 는 손대지 않음,
  - fail-open: 깨진 입력은 원본 바이트와 사유 없이(비 JSON) 또는 사유와 함께 돌려줌,
  - EventRewriter(convert=...) 가 청크가 쪼개져도 이벤트 경계마다 같은 결과를 내는지.
"""
import json

import pytest

from gateway import reflect_based_on_default as rbd
from gateway import unescape

BASED_ON = {"world": [{"id": "w1", "text": "근거"}], "mental-models": [{"id": "m1", "text": "모델"}]}
PAYLOAD = {"text": "답변", "usage": {"input_tokens": 5}}
WITH = {**PAYLOAD, "based_on": BASED_ON}


# --------------------------------------------------------------------------
# fill_include_based_on
# --------------------------------------------------------------------------

@pytest.mark.parametrize("arguments, expected, filled", [
    ({"query": "q"}, {"query": "q", "include_based_on": True}, True),
    ({"query": "q", "include_based_on": None}, {"query": "q", "include_based_on": True}, True),
    ({}, {"include_based_on": True}, True),
    (None, {"include_based_on": True}, True),
    ({"query": "q", "include_based_on": True}, {"query": "q", "include_based_on": True}, False),
    ({"query": "q", "include_based_on": False}, {"query": "q", "include_based_on": False}, False),
    ({"query": "q", "include_based_on": 0}, {"query": "q", "include_based_on": 0}, False),
    ({"query": "q", "include_based_on": "no"}, {"query": "q", "include_based_on": "no"}, False),
])
def test_fill_rule(arguments, expected, filled):
    out, did = rbd.fill_include_based_on("reflect", arguments)
    assert out == expected and did is filled


@pytest.mark.parametrize("arguments", [["a"], [], "x", 7, True])
def test_non_dict_arguments_are_returned_as_the_same_object(arguments):
    out, did = rbd.fill_include_based_on("reflect", arguments)
    assert out is arguments and did is False


@pytest.mark.parametrize("tool", ["recall", "list_mental_models", "retain", "get_mental_model", None, ""])
def test_other_tools_are_untouched(tool):
    arguments = {"query": "q"}
    out, did = rbd.fill_include_based_on(tool, arguments)
    assert out is arguments and did is False


def test_fill_returns_a_new_dict_and_leaves_the_input_alone():
    arguments = {"query": "q"}
    out, _ = rbd.fill_include_based_on("reflect", arguments)
    assert out is not arguments and arguments == {"query": "q"}


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off", " Off "])
def test_kill_switch_off_values(monkeypatch, value):
    monkeypatch.setenv(rbd.ENV_FLAG, value)
    assert rbd.enabled() is False
    arguments = {"query": "q"}
    out, did = rbd.fill_include_based_on("reflect", arguments)
    assert out is arguments and did is False


@pytest.mark.parametrize("value", [None, "1", "true", "on", "yes", ""])
def test_enabled_by_default_and_for_other_values(monkeypatch, value):
    if value is None:
        monkeypatch.delenv(rbd.ENV_FLAG, raising=False)
    else:
        monkeypatch.setenv(rbd.ENV_FLAG, value)
    assert rbd.enabled() is True


def test_constants_match_the_issue():
    assert rbd.TOOL == "reflect"
    assert rbd.ENV_FLAG == "HC_REFLECT_BASED_ON_DEFAULT"
    assert rbd.AUDIT_REASON == "based-on-default:true"


# --------------------------------------------------------------------------
# strip_text: 들여쓰기·이스케이프까지 채우지 않았을 때의 모양으로
# --------------------------------------------------------------------------

FORMATS = {
    "compact-ascii": dict(),
    "indent2-ascii": dict(indent=2),
    "indent4-ascii": dict(indent=4),
    "compact-unicode": dict(ensure_ascii=False),
    "indent2-unicode": dict(indent=2, ensure_ascii=False),
    "tight-separators": dict(separators=(",", ":")),
}


@pytest.mark.parametrize("fmt", sorted(FORMATS))
def test_strip_text_reproduces_the_unfilled_bytes(fmt):
    kwargs = FORMATS[fmt]
    filled = json.dumps(WITH, **kwargs)
    unfilled = json.dumps(PAYLOAD, **kwargs)
    assert rbd.strip_text(filled) == unfilled


@pytest.mark.parametrize("raw", [
    json.dumps(PAYLOAD),                  # based_on 없음
    json.dumps({**PAYLOAD, "x": "based_on"}),   # 값에만 나오는 문자열
    "not json based_on",                  # 비 JSON
    "Error: boom based_on",
    json.dumps(["based_on"]),             # dict 아님
    json.dumps("based_on"),
    "",
])
def test_strip_text_leaves_everything_else_as_the_same_string(raw):
    assert rbd.strip_text(raw) == raw


def test_strip_text_keeps_key_order_and_other_values():
    raw = json.dumps({"a": 1, "based_on": BASED_ON, "z": [3, 2, 1], "text": "한글"}, indent=2)
    assert list(json.loads(rbd.strip_text(raw))) == ["a", "z", "text"]
    assert rbd.strip_text(raw) == json.dumps({"a": 1, "z": [3, 2, 1], "text": "한글"}, indent=2)


def test_strip_text_unknown_format_still_strips_with_equal_values():
    """어떤 포맷에도 안 맞는 본문(공백이 제멋대로)은 값이 같은 재직렬화로 지운다 - 남기는 쪽이 더 나쁘다."""
    raw = '{ "text":"답변",\n   "based_on" : {"world": []} }'
    out = rbd.strip_text(raw)
    assert json.loads(out) == {"text": "답변"}


# --------------------------------------------------------------------------
# strip_tool_result: structuredContent.result(문자열/dict)·structuredContent 자체·text 블록
# --------------------------------------------------------------------------

def _str_result(payload, **kwargs):
    blob = json.dumps(payload, indent=2)
    return {"content": [{"type": "text", "text": blob}], "structuredContent": {"result": blob},
            "isError": False, **kwargs}


def _dict_result(payload):
    return {"content": [{"type": "text", "text": json.dumps(payload)}],
            "structuredContent": {"result": payload}, "isError": False}


def _flat_result(payload):
    return {"content": [{"type": "text", "text": json.dumps(payload)}],
            "structuredContent": payload, "isError": False}


def _payloads(result):
    structured = result["structuredContent"]
    inner = structured.get("result", structured)
    if isinstance(inner, str):
        inner = json.loads(inner)
    return inner, json.loads(result["content"][0]["text"])


@pytest.mark.parametrize("build", [_str_result, _dict_result, _flat_result],
                         ids=["structured-str", "structured-dict", "structured-flat"])
def test_strip_tool_result_removes_both_representations(build):
    result = build(WITH)
    out, changed = rbd.strip_tool_result(result)
    assert changed is True
    structured, text = _payloads(out)
    assert structured == PAYLOAD and text == PAYLOAD
    assert _payloads(result)[0] == WITH        # 입력은 바꾸지 않는다


def test_strip_tool_result_matches_the_unfilled_result_exactly():
    out, _ = rbd.strip_tool_result(_str_result(WITH))
    assert out == _str_result(PAYLOAD)


@pytest.mark.parametrize("result", [
    _str_result(PAYLOAD),
    _dict_result(PAYLOAD),
    _str_result(WITH, isError=True),
    {"content": [{"type": "text", "text": "Error: x"}], "isError": False},
    {"content": [], "isError": False},
    {"isError": False},
    "not-a-dict",
    None,
], ids=["no-based-on-str", "no-based-on-dict", "is-error", "non-json", "empty-content",
         "no-content", "string", "none"])
def test_strip_tool_result_leaves_other_results_as_the_same_object(result):
    out, changed = rbd.strip_tool_result(result)
    assert out is result and changed is False


def test_strip_tool_result_only_strips_text_blocks():
    result = _str_result(WITH)
    result["content"].append({"type": "image", "data": json.dumps(WITH)})
    out, _ = rbd.strip_tool_result(result)
    assert out["content"][1] == result["content"][1]


# --------------------------------------------------------------------------
# strip_mcp_element / strip_mcp_body: id 밖은 손대지 않음
# --------------------------------------------------------------------------

def _element(el_id, payload):
    return {"jsonrpc": "2.0", "id": el_id, "result": _str_result(payload)}


def test_element_outside_ids_is_untouched():
    element = _element(2, WITH)
    assert rbd.strip_mcp_element(element, {1}) is element
    assert rbd.strip_mcp_element(element, set()) is element


def test_element_in_ids_is_stripped_and_keeps_its_envelope():
    out = rbd.strip_mcp_element(_element(1, WITH), {1})
    assert out == _element(1, PAYLOAD)


@pytest.mark.parametrize("element", [
    {"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "x"}},
    {"jsonrpc": "2.0", "id": 1},
    "x",
    None,
])
def test_element_without_result_is_untouched(element):
    assert rbd.strip_mcp_element(element, {1}) is element


def _sse(element):
    return b"event: message\r\ndata: " + json.dumps(element).encode() + b"\r\n\r\n"


def test_body_strips_only_listed_ids_in_a_sse_batch():
    body = _sse(_element(1, WITH)) + _sse(_element(2, WITH))
    out, failure = rbd.strip_mcp_body("text/event-stream", body, {1})
    assert failure is None
    frames = [json.loads(ln[5:]) for ln in out.decode().splitlines() if ln.startswith("data:")]
    assert _payloads(frames[0]["result"])[0] == PAYLOAD
    assert _payloads(frames[1]["result"])[0] == WITH


def test_body_with_nothing_to_strip_is_byte_identical():
    body = _sse(_element(1, PAYLOAD))
    assert rbd.strip_mcp_body("text/event-stream", body, {1}) == (body, None)
    assert rbd.strip_mcp_body("text/event-stream", body, set()) == (body, None)
    assert rbd.strip_mcp_body("text/event-stream", b": ping\r\n\r\n", {1}) == (b": ping\r\n\r\n", None)


def test_json_body_single_and_batch():
    single, _ = rbd.strip_mcp_body("application/json", json.dumps(_element(1, WITH)).encode(), {1})
    assert _payloads(json.loads(single)["result"])[0] == PAYLOAD
    batch, _ = rbd.strip_mcp_body("application/json",
                                  json.dumps([_element(1, WITH), _element(2, WITH)]).encode(), {2})
    first, second = json.loads(batch)
    assert _payloads(first["result"])[0] == WITH and _payloads(second["result"])[0] == PAYLOAD


@pytest.mark.parametrize("ct, body", [
    ("text/event-stream", b"event: message\r\ndata: {broken\r\n\r\n"),
    ("application/json", b"{broken"),
    ("application/json", b"[1, 2]"),
    ("text/plain", b"plain"),
])
def test_body_failure_returns_the_original_bytes(ct, body):
    out, _failure = rbd.strip_mcp_body(ct, body, {1})
    assert out == body


def test_body_failure_carries_a_reason_for_unparsable_frames():
    out, failure = rbd.strip_mcp_body("text/event-stream", b"data: {broken\r\n\r\n", {1})
    assert out == b"data: {broken\r\n\r\n" and failure


# --------------------------------------------------------------------------
# EventRewriter(convert=...): 스트림 청크가 어떻게 쪼개져도 같은 결과, ping 은 그대로
# --------------------------------------------------------------------------

def _convert(strip_ids, unescape_ids):
    def convert(content_type, event):
        out, f1 = rbd.strip_mcp_body(content_type, event, strip_ids)
        out, f2 = unescape.unescape_mcp_body(content_type, out, unescape_ids)
        return out, f1 or f2
    return convert


def _stream(element):
    return b": ping\r\n\r\n" + _sse(element) + b": ping\r\n\r\n"


def _drain(rewriter, chunks):
    outs = []
    for chunk in chunks:
        outs.extend(rewriter.feed(chunk))
    outs.extend(rewriter.finish())
    return outs


@pytest.mark.parametrize("size", [1, 3, 7, 64, 10_000])
def test_rewriter_result_does_not_depend_on_chunking(size):
    stream = _stream(_element(1, WITH))
    chunks = [stream[i:i + size] for i in range(0, len(stream), size)]
    outs = _drain(unescape.EventRewriter("text/event-stream", {1}, convert=_convert({1}, {1})), chunks)
    assert all(failure is None for _, failure in outs)
    joined = b"".join(out for out, _ in outs)
    frames = [json.loads(ln[5:]) for ln in joined.decode().splitlines() if ln.startswith("data:")]
    assert len(frames) == 1
    text = frames[0]["result"]["content"][0]["text"]
    assert "based_on" not in text and "\\u" not in text and "답변" in text
    assert joined.startswith(b": ping\r\n\r\n") and joined.endswith(b": ping\r\n\r\n")


def test_rewriter_default_convert_is_the_plain_unescape():
    """convert 를 안 주면 #26 그대로: based_on 은 건드리지 않고 이스케이프만 푼다."""
    stream = _sse(_element(1, WITH))
    outs = _drain(unescape.EventRewriter("text/event-stream", {1}), [stream])
    frame = json.loads(b"".join(o for o, _ in outs).decode().splitlines()[1][5:])
    text = frame["result"]["content"][0]["text"]
    assert "based_on" in text and "\\u" not in text


def test_rewriter_fails_open_per_event_when_convert_raises():
    def boom(content_type, event):
        if b"data:" not in event:
            return event, None
        raise RuntimeError("x")

    stream = _stream(_element(1, WITH))
    outs = _drain(unescape.EventRewriter("text/event-stream", {1}, convert=boom), [stream])
    assert b"".join(o for o, _ in outs) == stream
    assert [f for _, f in outs if f] == ["rewrite-exception:RuntimeError"]


def test_rewriter_non_sse_body_is_converted_once_at_finish():
    body = json.dumps(_element(1, WITH)).encode()
    rewriter = unescape.EventRewriter("application/json", {1}, convert=_convert({1}, set()))
    assert rewriter.feed(body[:10]) == [] and rewriter.feed(body[10:]) == []
    (out, failure), = rewriter.finish()
    assert failure is None
    assert _payloads(json.loads(out)["result"])[0] == PAYLOAD


# --------------------------------------------------------------------------
# final-review 반영: 꼬리 변환, 형식 탐색 순서
# --------------------------------------------------------------------------

def test_rewriter_with_convert_converts_the_unterminated_tail():
    stream = _sse(_element(1, WITH))[:-2]            # 종결 빈 줄 없음
    rewriter = unescape.EventRewriter("text/event-stream", {1}, convert=_convert({1}, set()))
    outs = _drain(rewriter, [stream])
    assert all(failure is None for _, failure in outs)
    frame = json.loads(b"".join(o for o, _ in outs).decode().splitlines()[1][5:])
    assert "based_on" not in frame["result"]["content"][0]["text"]


def test_rewriter_without_convert_keeps_the_tail_raw():
    stream = _sse(_element(1, WITH))[:-2]
    outs = _drain(unescape.EventRewriter("text/event-stream", {1}), [stream])
    assert b"".join(o for o, _ in outs) == stream and all(f is None for _, f in outs)


def test_rewriter_tail_convert_failure_is_fail_open():
    def boom(content_type, event):
        raise RuntimeError("x")

    stream = _sse(_element(1, WITH))[:-2]
    outs = _drain(unescape.EventRewriter("text/event-stream", {1}, convert=boom), [stream])
    assert b"".join(o for o, _ in outs) == stream
    assert [f for _, f in outs if f] == ["rewrite-exception:RuntimeError"]


def test_upstream_format_is_tried_first():
    assert rbd._FORMATS[0] == (2, True, None)
    assert len(rbd._FORMATS) == len(set(rbd._FORMATS))
