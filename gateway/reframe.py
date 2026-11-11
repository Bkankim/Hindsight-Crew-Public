"""N4: 응답 리프레이머 - upstream 응답의 완결 바이트 변환기 (순수 함수, 표준 라이브러리만).

upstream(Hindsight v0.8.3)은 MCP 응답을 `text/event-stream` 으로 프레이밍한다(N0 실측).
다만 `notifications/initialized` 는 **HTTP 202 + application/json + 0바이트 본문**이고,
게이트웨이가 합성하는 isError 응답은 `application/json` 단발이다. 그래서 파서는 SSE 확정이되
JSON 단발·빈 본문·비대상 바이트를 모두 통과시켜야 한다.

  parse_response(content_type, body) -> Parsed
  transform_results(parsed, fn)      -> Parsed
  serialize(parsed)                  -> (content_type, body)

설계 원칙
---------
* **전역 상태 없음.** 실패는 예외가 아니라 `Parsed.failure` / `try_serialize()` 3번째 값의
  사유 문자열로 표면화한다. fail-open(원본 바이트 그대로 전달)과 카운터 증가는 호출자 몫이다.
  실패해도 `Parsed.raw` 에 원본 바이트가 그대로 남아 있어 무손실 통과가 가능하다.
* **무변환 왕복은 바이트 동일.** 각 프레임이 원문 라인(내용 + 개행 바이트)을 그대로 들고 있고,
  변조되지 않은 프레임은 재렌더링 없이 원문을 그대로 되돌려준다. 따라서 개행 스타일
  (initialize.raw 는 LF, 나머지 fixture 는 CRLF), 프레임 경계, `event:`/`id:`/`retry:`/주석 등
  비 data 라인, 라인 순서, 종결 개행 없는 마지막 프레임까지 보존된다.
* **바이트 동일이 불가한 유일한 케이스**: 하나의 SSE 프레임이 `data:` 라인을 2줄 이상으로
  쪼개 보냈고(SSE 스펙상 값은 `\n` 으로 이어붙인다) 그 프레임을 **변조**한 경우. 재직렬화는
  첫 data 라인의 접두·개행을 재사용해 **단일 data 라인으로 합친다**(의미 동등, 바이트 비동일).
  무변환이면 다중 data 라인도 바이트 동일이다. 회귀는
  tests/test_reframe.py::test_transformed_multi_data_line_frame_collapses_to_one_data_line 이 고정한다.
* 변조 프레임의 JSON 은 upstream 관례에 맞춰 compact separators 로 재렌더링한다.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Tuple

KIND_SSE = "sse"
KIND_JSON = "json"
KIND_EMPTY = "empty"
KIND_OPAQUE = "opaque"  # 변조 비대상(또는 파싱 실패) - 원본 바이트만 통과시킨다

SSE_MEDIA_TYPE = "text/event-stream"
_DATA_FIELD = b"data:"
_JSON_SEPARATORS = (",", ":")


@dataclass(frozen=True)
class Line:
    """SSE 원문 한 줄: 내용과 개행 바이트를 분리 보관한다(마지막 줄은 term=b"")."""

    content: bytes
    term: bytes

    def render(self) -> bytes:
        return self.content + self.term


@dataclass(frozen=True)
class Frame:
    """직렬화 단위. SSE 는 프레임 1개 = 이벤트 1개, JSON 단발은 프레임 1개 = 본문 전체."""

    payload: Any = None  # 파싱된 JSON 값(dict/list) 또는 None(데이터 없는 프레임)
    lines: Tuple[Line, ...] = ()  # SSE 전용 원문 라인 (JSON 본문은 빈 튜플)
    raw: bytes = b""  # 이 프레임의 원문 바이트
    dirty: bool = False  # payload 가 변조됐는가

    def render(self) -> bytes:
        if not self.dirty:
            return self.raw
        body = json.dumps(self.payload, separators=_JSON_SEPARATORS, ensure_ascii=False)
        # a lone surrogate leaves as the JSON escape \udXXX the caller sent (issue #135), not a crash
        encoded = body.encode("utf-8", "backslashreplace")
        if not self.lines:
            return encoded
        out = []
        emitted = False
        for line in self.lines:
            if _is_data_line(line):
                if emitted:
                    continue  # 다중 data 라인은 첫 줄로 합친다 (모듈 docstring 참조)
                emitted = True
                out.append(_data_prefix(line) + encoded + line.term)
            else:
                out.append(line.render())
        return b"".join(out)


@dataclass(frozen=True)
class Parsed:
    """파싱 결과 + 실패 표식. 어떤 경우에도 `raw` 는 원본 바이트를 그대로 들고 있다."""

    kind: str
    content_type: str
    raw: bytes
    frames: Tuple[Frame, ...] = field(default=())
    failure: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.failure is None

    @property
    def dirty(self) -> bool:
        return any(f.dirty for f in self.frames)


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------

def element_id(el: Any) -> Any:
    """JSON-RPC id usable as a bookkeeping key, else None ("no id": no per-element response
    handling). A list/object id is unhashable and keying a dict or set with it raised
    TypeError -> 500 (review P3, 2026-09-23). Upstream itself treats such a call as a
    notification and stores nothing, so the 500 was noise, not a duplicate. Such elements are
    still forwarded untouched."""
    if not isinstance(el, dict):
        return None
    value = el.get("id")
    # Upstream's RequestId is `int | str` (mcp 1.28.1): a float or bool id validates as a
    # notification there and never runs, so it is "no id" here too (review 2nd pass).
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    return value


def parse_response(content_type: Optional[str], body: Optional[bytes]) -> Parsed:
    """응답 본문을 프레이밍별로 분해한다. 예외를 던지지 않는다.

    - 빈 본문(202 notifications 경로) -> KIND_EMPTY
    - text/event-stream -> KIND_SSE (프레임 시퀀스, 비 data 라인·순서 보존)
    - application/json 계열 -> KIND_JSON (단일 객체 또는 배치 배열)
    - 그 밖의 content-type -> KIND_OPAQUE, failure=None (변조 비대상일 뿐 실패가 아니다)
    - 파싱 실패 -> KIND_OPAQUE + failure=<사유 문자열> (원본 보존, fail-open 은 호출자 책임)
    """
    ct = content_type or ""
    raw = body or b""
    if not raw:
        return Parsed(kind=KIND_EMPTY, content_type=ct, raw=b"")

    media = _media_type(ct)
    if media == SSE_MEDIA_TYPE:
        return _parse_sse(ct, raw)
    if media == "application/json" or media.endswith("+json"):
        return _parse_json(ct, raw)
    return Parsed(kind=KIND_OPAQUE, content_type=ct, raw=raw)


def _parse_json(ct: str, raw: bytes) -> Parsed:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return Parsed(kind=KIND_OPAQUE, content_type=ct, raw=raw, failure="json-decode-error: %s" % exc)
    if not isinstance(payload, (dict, list)):
        return Parsed(
            kind=KIND_OPAQUE, content_type=ct, raw=raw,
            failure="json-not-a-jsonrpc-message: %s" % type(payload).__name__,
        )
    return Parsed(
        kind=KIND_JSON, content_type=ct, raw=raw,
        frames=(Frame(payload=payload, raw=raw),),
    )


def _parse_sse(ct: str, raw: bytes) -> Parsed:
    frames: List[Frame] = []
    for lines in _group_frames(_split_lines(raw)):
        frame_raw = b"".join(line.render() for line in lines)
        data_lines = [ln for ln in lines if _is_data_line(ln)]
        if not data_lines:
            frames.append(Frame(payload=None, lines=tuple(lines), raw=frame_raw))
            continue
        blob = b"\n".join(_data_value(ln) for ln in data_lines)
        try:
            payload = json.loads(blob.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            return Parsed(
                kind=KIND_OPAQUE, content_type=ct, raw=raw,
                failure="sse-data-decode-error: %s" % exc,
            )
        if not isinstance(payload, (dict, list)):
            return Parsed(
                kind=KIND_OPAQUE, content_type=ct, raw=raw,
                failure="sse-data-not-a-jsonrpc-message: %s" % type(payload).__name__,
            )
        frames.append(Frame(payload=payload, lines=tuple(lines), raw=frame_raw))
    return Parsed(kind=KIND_SSE, content_type=ct, raw=raw, frames=tuple(frames))


def _split_lines(raw: bytes) -> List[Line]:
    """CRLF/LF/CR 을 모두 인식하되 원문 개행 바이트를 그대로 보존한다."""
    lines: List[Line] = []
    start = 0
    i = 0
    size = len(raw)
    while i < size:
        char = raw[i : i + 1]
        if char == b"\r":
            term = b"\r\n" if raw[i + 1 : i + 2] == b"\n" else b"\r"
        elif char == b"\n":
            term = b"\n"
        else:
            i += 1
            continue
        lines.append(Line(content=raw[start:i], term=term))
        i += len(term)
        start = i
    if start < size:
        lines.append(Line(content=raw[start:], term=b""))
    return lines


def _group_frames(lines: List[Line]) -> List[List[Line]]:
    """빈 줄을 경계로 프레임을 자른다. 경계 빈 줄은 앞 프레임에 귀속.

    종결 빈 줄 없이 끝나는 마지막 프레임도 프레임으로 취급한다(전량 버퍼링된 응답 전제 -
    스트리밍 파서와 달리 잘린 이벤트를 유실시키지 않는다).
    """
    frames: List[List[Line]] = []
    current: List[Line] = []
    for line in lines:
        current.append(line)
        if line.content == b"":
            frames.append(current)
            current = []
    if current:
        frames.append(current)
    return frames


def _is_data_line(line: Line) -> bool:
    return line.content.startswith(_DATA_FIELD)


def _data_prefix(line: Line) -> bytes:
    """`data:` + 스펙상 무시되는 선행 공백 1칸까지를 접두로 본다."""
    rest = line.content[len(_DATA_FIELD) :]
    return _DATA_FIELD + (b" " if rest.startswith(b" ") else b"")


def _data_value(line: Line) -> bytes:
    return line.content[len(_data_prefix(line)) :]


def _media_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


# ---------------------------------------------------------------------------
# transform
# ---------------------------------------------------------------------------

def _is_jsonrpc_response(element: Any) -> bool:
    """JSON-RPC 응답 element 판정.

    id 없는 element(notification 류)는 물론, id 는 있으나 `method` 를 가진
    **서버 발신 요청**(sampling/createMessage 등)도 응답이 아니므로 변조 대상에서 제외한다.
    """
    return isinstance(element, dict) and "id" in element and "method" not in element


def transform_results(parsed: Parsed, fn: Callable[[dict], dict]) -> Parsed:
    """JSON-RPC 응답 element 마다 `fn` 을 적용한 새 Parsed 를 돌려준다.

    * 단일 객체·배치 배열 양쪽 지원. 배열은 shape(길이·순서)을 유지한다.
    * id 없는 element(notification 류)와 서버 발신 요청은 fn 미적용 통과.
    * 이미 실패 표식이 붙은 Parsed 는 손대지 않고 그대로 반환한다(fail-open 보존).
    * fn 이 예외를 던지면 예외를 전파하지 않고 failure 를 붙여 원본 Parsed 를 반환한다.
    * fn 은 element dict 의 **사본**을 받는다. 반환값이든 in-place 변형이든 원본과 달라진
      경우에만 dirty 로 표시되므로, 무변환(항등) fn 은 바이트 동일 왕복을 깨지 않는다.
    """
    if parsed.failure is not None or not parsed.frames:
        return parsed

    new_frames: List[Frame] = []
    for frame in parsed.frames:
        if frame.payload is None:
            new_frames.append(frame)
            continue
        try:
            payload, changed = _apply(frame.payload, fn)
        except Exception as exc:  # noqa: BLE001 - fail-open: 사유만 남기고 원본 유지
            return Parsed(
                kind=KIND_OPAQUE, content_type=parsed.content_type, raw=parsed.raw,
                failure="transform-callback-error: %s: %s" % (type(exc).__name__, exc),
            )
        if changed:
            new_frames.append(Frame(payload=payload, lines=frame.lines, raw=frame.raw, dirty=True))
        else:
            new_frames.append(frame)
    return Parsed(
        kind=parsed.kind, content_type=parsed.content_type, raw=parsed.raw,
        frames=tuple(new_frames), failure=None,
    )


def _apply(payload: Any, fn: Callable[[dict], dict]) -> Tuple[Any, bool]:
    if isinstance(payload, list):
        out = []
        changed = False
        for element in payload:
            if not _is_jsonrpc_response(element):
                out.append(element)
                continue
            new = fn(copy.deepcopy(element))
            out.append(new)
            changed = changed or new != element
        return out, changed
    if not _is_jsonrpc_response(payload):
        return payload, False
    new = fn(copy.deepcopy(payload))
    return new, new != payload


# ---------------------------------------------------------------------------
# serialize
# ---------------------------------------------------------------------------

def try_serialize(parsed: Parsed) -> Tuple[str, bytes, Optional[str]]:
    """`serialize()` + 직렬화 실패 사유. 실패 시 원본 바이트로 폴백한다.

    호출자는 3번째 값이 not None 일 때 `rewrite_failures` 카운터를 올리면 된다
    (모듈은 전역 카운터를 두지 않는다).
    """
    if parsed.failure is not None or not parsed.dirty:
        return parsed.content_type, parsed.raw, None
    try:
        body = b"".join(frame.render() for frame in parsed.frames)
    except (TypeError, ValueError) as exc:
        return parsed.content_type, parsed.raw, "serialize-error: %s: %s" % (type(exc).__name__, exc)
    return parsed.content_type, body, None


def serialize(parsed: Parsed) -> Tuple[str, bytes]:
    """원래 프레이밍 그대로 재직렬화한다. 무변환이면 원본 바이트를 그대로 돌려준다."""
    content_type, body, _ = try_serialize(parsed)
    return content_type, body


# ---------------------------------------------------------------------------
# incremental SSE event split (issue #26)
# ---------------------------------------------------------------------------

def split_events(buffer: bytes) -> Tuple[List[bytes], bytes]:
    """스트리밍 버퍼에서 빈 줄로 완결된 SSE 이벤트만 잘라낸다. -> (완결 이벤트 원문들, 남은 바이트)

    개행 규칙은 `_split_lines` 와 같다(CRLF/LF/CR). 버퍼 끝의 `\\r` 은 다음 청크의 `\\n` 과
    CRLF 한 쌍일 수 있어 판정을 미루고 남은 바이트에 둔다. 이어붙이면 항상 입력과 바이트 동일.
    """
    held = buffer.endswith(b"\r")
    scan = buffer[:-1] if held else buffer
    events: List[bytes] = []
    start = 0
    offset = 0
    for line in _split_lines(scan):
        offset += len(line.content) + len(line.term)
        if line.content == b"" and line.term:
            events.append(buffer[start:offset])
            start = offset
    return events, buffer[start:]


class EventSplitter:
    """`split_events` 의 증분판: 청크마다 버퍼 전체를 다시 훑지 않는다(선형 비용, 이슈 #26 리뷰).

    경계 규칙은 `split_events` 와 같다(CRLF/LF/CR, 버퍼 끝 `\\r` 은 다음 바이트까지 보류).
    feed() 는 이번에 완결된 이벤트 원문들을, finish() 는 EOF 에서 끝 `\\r` 을 종결자로 확정한 뒤
    완결된 이벤트들과 종결 빈 줄 없는 꼬리를 돌려준다. 출력을 모두 이으면 입력과 바이트 동일.
    """

    _NEWLINE = re.compile(rb"\r\n|\r|\n")

    def __init__(self) -> None:
        self._buf = bytearray()
        self._scan = 0         # 다음 개행 탐색 시작 위치
        self._line_start = 0   # 아직 끝나지 않은 줄의 시작 위치

    def feed(self, chunk: bytes) -> List[bytes]:
        self._buf += chunk
        return self._cut(eof=False)

    def pending(self) -> bytes:
        return bytes(self._buf)

    def finish(self) -> Tuple[List[bytes], bytes]:
        events = self._cut(eof=True)
        tail = bytes(self._buf)
        self._buf.clear()
        self._scan = self._line_start = 0
        return events, tail

    def _cut(self, eof: bool) -> List[bytes]:
        buf = self._buf
        size = len(buf)
        events: List[bytes] = []
        event_start = 0
        scan = self._scan
        for match in self._NEWLINE.finditer(buf, scan):
            if not eof and match.end() == size and match.group() == b"\r":
                scan = match.start()          # CR 뒤에 LF 가 올 수 있다 - 보류
                break
            if match.start() == self._line_start:   # 빈 줄 = 이벤트 경계
                events.append(bytes(buf[event_start:match.end()]))
                event_start = match.end()
            self._line_start = match.end()
            scan = match.end()
        else:
            scan = size
        if event_start:
            del buf[:event_start]
            scan -= event_start
            self._line_start -= event_start
        self._scan = scan
        return events
