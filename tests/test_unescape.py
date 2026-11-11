"""한글 이스케이프 해제 회귀 (issue #23) - 핵심 계약은 '값이 바뀌지 않는다'이다.

upstream v0.8.3 은 도구 결과를 기본 json.dumps(ensure_ascii=True)로 싣는다. 그래서 한글 1자가
`\\uc5d0` 6자가 되고, 멘탈 모델 1건이 12,737 -> 4,219 토큰(o200k, 2026-09-28 실측)으로 3배가 된다.
이 모듈은 표기만 바꾼다: 디코드한 값은 원본과 같아야 하고, 그 밖의 바이트(들여쓰기·ASCII 이스케이프)는
그대로 남아야 한다.
"""
import json

import pytest

from gateway import unescape


def _blob(value, **kw):
    return json.dumps(value, **kw)          # upstream 기본값 = ensure_ascii=True


def _tool_result(blob):
    """upstream 이 같은 JSON 문자열을 2벌(structuredContent.result + content[0].text) 싣는 shape."""
    return {"_meta": {}, "content": [{"type": "text", "text": blob}],
            "structuredContent": {"result": blob}, "isError": False}


# --- 계약 1: 값 불변, 표기만 바뀐다 --------------------------------------------------

def test_korean_escapes_become_literal_and_value_is_identical():
    value = {"id": "demo-rules", "content": "# 규칙\n- 금지 표현: \"6주 만에\""}
    raw = _blob(value, indent=2)
    out = unescape.unescape_text(raw)
    assert "\\u" not in out
    assert "규칙" in out
    assert json.loads(out) == value


def test_formatting_and_ascii_escapes_are_preserved():
    """들여쓰기·줄바꿈 이스케이프·따옴표 이스케이프는 upstream 바이트 그대로 남는다."""
    value = {"a": "줄1\n\"인용\"\t탭", "b": [1, 2]}
    out = unescape.unescape_text(_blob(value, indent=2))
    assert out == json.dumps(value, indent=2, ensure_ascii=False)
    assert "\\n" in out and '\\"' in out and "\\t" in out and "\n  " in out


def test_ascii_range_escapes_are_left_alone():
    raw = '{"a": "\\u0022\\u003c\\u001f"}'
    assert unescape.unescape_text(raw) == raw


def test_surrogate_pair_becomes_one_character():
    value = {"e": "기록 😀 끝"}
    out = unescape.unescape_text(_blob(value))
    assert "😀" in out and json.loads(out) == value


def test_lone_surrogate_escape_is_left_alone():
    raw = '{"a": "\\ud83d x \\uc5d0"}'
    out = unescape.unescape_text(raw)
    assert "\\ud83d" in out and "에" in out
    assert json.loads(out) == json.loads(raw)


def test_escaped_backslash_before_u_is_not_an_escape():
    """`\\\\uc5d0` 는 역슬래시 문자 + 'uc5d0' 텍스트다 - 풀면 값이 바뀐다."""
    value = {"path": "C:\\uc5d0", "k": "한"}
    raw = _blob(value)
    out = unescape.unescape_text(raw)
    assert json.loads(out) == value
    assert "\\\\uc5d0" in out and "한" in out


def test_non_json_text_is_returned_untouched():
    """JSON 이 아니면 `\\uXXXX` 는 이스케이프가 아니라 글자 그대로다."""
    raw = "error: \\uc5d0 not json"
    assert unescape.unescape_text(raw) == raw


def test_already_literal_text_is_returned_as_is():
    raw = json.dumps({"a": "한글"}, ensure_ascii=False)
    assert unescape.unescape_text(raw) == raw


# --- 계약 2: 2벌 표현은 함께 바뀐다 --------------------------------------------------

def test_tool_result_rewrites_both_copies_in_sync():
    blob = _blob({"content": "본문"}, indent=2)
    out, changed = unescape.unescape_tool_result(_tool_result(blob))
    assert changed
    assert out["content"][0]["text"] == out["structuredContent"]["result"]
    assert json.loads(out["content"][0]["text"]) == {"content": "본문"}
    assert "\\u" not in out["content"][0]["text"]


def test_tool_result_without_escapes_is_unchanged_object():
    result = _tool_result(json.dumps({"a": "x"}))
    out, changed = unescape.unescape_tool_result(result)
    assert not changed and out is result


def test_error_result_passes_through():
    result = {**_tool_result(_blob({"a": "오류"})), "isError": True}
    out, changed = unescape.unescape_tool_result(result)
    assert not changed and out is result


# --- 계약 3: 대상 id 가 아닌 element 는 건드리지 않는다 -------------------------------

def test_element_outside_ids_is_untouched():
    el = {"jsonrpc": "2.0", "id": 7, "result": _tool_result(_blob({"a": "한"}))}
    assert unescape.unescape_mcp_element(el, {1}) is el


def test_element_in_ids_is_rewritten():
    el = {"jsonrpc": "2.0", "id": 1, "result": _tool_result(_blob({"a": "한"}))}
    out = unescape.unescape_mcp_element(el, {1})
    assert out["result"]["content"][0]["text"] == '{"a": "한"}'


@pytest.mark.parametrize("flag,expected", [(None, True), ("1", True), ("0", False), ("off", False)])
def test_env_flag_default_on_and_rollback(monkeypatch, flag, expected):
    if flag is None:
        monkeypatch.delenv(unescape.ENV_FLAG, raising=False)
    else:
        monkeypatch.setenv(unescape.ENV_FLAG, flag)
    assert unescape.enabled() is expected


def test_tool_allowlist_excludes_reflect():
    """reflect 는 스트리밍 통과(2026-09-26)를 지키기 위해 대상에서 뺐다(#23 합의)."""
    assert "reflect" not in unescape.TOOLS
    assert {"get_mental_model", "list_mental_models", "list_memories", "get_memory",
            "list_documents", "get_document"} <= unescape.TOOLS


# --- issue #26: reflect SSE 이벤트 단위 변환 (EventRewriter / reframe.split_events) ---

from gateway import reframe  # noqa: E402

_PING = b": ping - 2026-09-28 00:00:00\r\n\r\n"
_KO = json.dumps({"text": "한글 답변"}, indent=2)
_EVENT = (b"event: message\r\ndata: " + json.dumps(
    {"jsonrpc": "2.0", "id": 7, "result": _tool_result(_KO)}).encode() + b"\r\n\r\n")


@pytest.mark.parametrize("sep", [b"\r\n", b"\n", b"\r"])
def test_split_events_honours_every_newline_style(sep):
    stream = b": a" + sep + sep + b"data: {}" + sep + sep + b"tail"
    events, rest = reframe.split_events(stream)
    assert events == [b": a" + sep + sep, b"data: {}" + sep + sep] and rest == b"tail"


def test_split_events_holds_a_trailing_cr_until_the_next_byte():
    events, rest = reframe.split_events(b": a\r\n\r")
    assert events == [] and rest == b": a\r\n\r"          # the \r may pair with a coming \n
    events, rest = reframe.split_events(rest + b"\n")
    assert events == [b": a\r\n\r\n"] and rest == b""


def test_event_rewriter_emits_ping_at_once_and_unescapes_data_events():
    rw = unescape.EventRewriter("text/event-stream", {7})
    assert rw.feed(_PING) == [(_PING, None)]
    out = []
    for i in range(0, len(_EVENT), 7):                  # byte-level chunking
        out += rw.feed(_EVENT[i:i + 7])
    out += rw.finish()
    [(body, failure)] = out
    assert failure is None
    payload = json.loads(body.split(b"data: ", 1)[1])
    text = payload["result"]["content"][0]["text"]
    assert "\\u" not in text and json.loads(text) == json.loads(_KO)
    assert payload["result"]["structuredContent"]["result"] == text


def test_event_rewriter_fails_open_per_event_and_passes_tail_raw():
    rw = unescape.EventRewriter("text/event-stream", {7})
    broken = b"data: {nope\n\n"
    [(body, failure)] = rw.feed(broken)
    assert body == broken and failure
    assert rw.feed(b"data: {\"id\"") == []
    assert rw.finish() == [(b"data: {\"id\"", None)]


def test_event_rewriter_leaves_other_ids_byte_identical():
    rw = unescape.EventRewriter("text/event-stream", {8})
    assert rw.feed(_EVENT) == [(_EVENT, None)]


def test_event_rewriter_buffers_non_sse_media_until_finish():
    body = json.dumps({"jsonrpc": "2.0", "id": 7, "result": _tool_result(_KO)}).encode()
    rw = unescape.EventRewriter("application/json", {7})
    assert rw.feed(body[:10]) == [] and rw.feed(body[10:]) == []
    [(out, failure)] = rw.finish()
    assert failure is None and "한글" in json.loads(out)["result"]["content"][0]["text"]


# --- issue #26 리뷰 반영: 예외 fail-open, 증분 분할, EOF CR 종결 ---

import random  # noqa: E402
import time  # noqa: E402

_DEEP = b"data: " + b"[" * 100000 + b"]" * 100000 + b"\r\n\r\n"


def test_event_rewriter_turns_any_exception_into_a_fail_open_event():
    rw = unescape.EventRewriter("text/event-stream", {7})
    [(body, failure)] = rw.feed(_DEEP)                  # json.loads -> RecursionError
    assert body == _DEEP
    assert failure == "rewrite-exception:RecursionError"
    [(body, failure)] = rw.feed(_EVENT)                 # the stream keeps going
    assert failure is None and "한글".encode() in body


def _stream_sample():
    parts = [_PING, _EVENT, b": c\n\n", b"data: {}\r\r", _EVENT.replace(b"\r\n", b"\n"),
             b"data: {\"a\":1}\r\n\r\n", _PING, b"tail\r"]
    return b"".join(parts)


@pytest.mark.parametrize("seed", range(20))
def test_incremental_splitter_matches_split_events_under_random_chunking(seed):
    stream = _stream_sample()
    rng = random.Random(seed)
    cuts = sorted(rng.sample(range(1, len(stream)), rng.randint(1, 40)))
    splitter = reframe.EventSplitter()
    events = []
    for a, b in zip([0] + cuts, cuts + [len(stream)]):
        events += splitter.feed(stream[a:b])
    want_events, want_rest = reframe.split_events(stream)
    assert events == want_events
    assert splitter.pending() == want_rest


def test_event_rewriter_feed_is_linear_on_a_large_event():
    big = json.dumps({"text": "한글 " * 60000}, indent=2)     # ~600KB escaped
    event = (b"data: " + json.dumps({"jsonrpc": "2.0", "id": 7,
                                     "result": _tool_result(big)}).encode() + b"\r\n\r\n")
    assert len(event) > 600_000
    rw = unescape.EventRewriter("text/event-stream", {8})     # isolate split cost
    started = time.perf_counter()
    out = []
    for i in range(0, len(event), 4096):
        out += rw.feed(event[i:i + 4096])
    elapsed = time.perf_counter() - started
    assert out == [(event, None)]
    assert elapsed < 0.3, elapsed


def test_event_rewriter_finish_treats_a_final_cr_as_a_terminator():
    event = _EVENT.replace(b"\r\n", b"\r")               # ... data: {...}\r\r at EOF
    rw = unescape.EventRewriter("text/event-stream", {7})
    assert rw.feed(event) == []                           # the last \r is held
    [(body, failure)] = rw.finish()
    assert failure is None and body.endswith(b"\r\r")
    text = json.loads(body.split(b"data: ", 1)[1])["result"]["content"][0]["text"]
    assert "\\u" not in text and json.loads(text) == json.loads(_KO)


def test_event_rewriter_finish_splits_complete_events_from_a_real_tail():
    rw = unescape.EventRewriter("text/event-stream", {7})
    rw.feed(b": a\r")
    assert rw.finish() == [(b": a\r", None)]              # one line end, no blank line
