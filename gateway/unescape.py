"""도구 결과 한글 이스케이프 해제 - 값은 그대로 두고 `\\uXXXX` 표기만 글자로 되돌린다 (issue #23).

왜 (2026-09-28 실측, o200k):
  upstream v0.8.3 은 도구 결과를 기본 json.dumps(ensure_ascii=True)로 싣는다. 한글 1자가 `\\uc5d0`
  6자가 되어 get_mental_model 1건이 4,219 토큰이면 될 것을 12,737 토큰으로 보낸다(3배).
  recall 은 recall_prune 이 ensure_ascii=False 로 재직렬화해 가려져 있었을 뿐, 다른 조회 도구는
  전부 upstream 문자열을 그대로 통과시켰다.

무엇을:
  JSON 문자열 안의 비ASCII `\\uXXXX`(서로게이트 쌍 포함)만 글자로 바꾼다. ASCII 범위 이스케이프
  (`\\u0022` 등)·짝 없는 서로게이트·`\\\\u` (역슬래시 문자 + 텍스트)는 남긴다. 들여쓰기·`\\n` 같은
  나머지 바이트는 건드리지 않는다 - 재직렬화가 아니라 치환이라 upstream 형식이 그대로 남는다.
  바꾼 뒤 json.loads 결과가 원본과 다르면 원본을 돌려준다(호출마다 스스로 무손실을 증명한다).

어디에:
  TOOLS 허용 목록(LLM 없는 조회 도구) + app.py 가 이미 버퍼링하는 recall·retain 계열.
  reflect 는 버퍼링하지 않고 SSE 이벤트 단위로 푼다(issue #26, EventRewriter) - 응답 전체를 모으면
  2026-09-26 스트리밍 통과(`: ping` 프레임이 클라이언트 첫 바이트 예산 안에 도달)가 깨지기 때문이다.
  버퍼링 대상과 한 배치로 온 reflect 는 버퍼링 경로에서 함께 푼다.
  REST 는 upstream(FastAPI)이 이미 ensure_ascii=False 라 대상이 아니다.

설계: 순수 함수, 표준 라이브러리만, 전역 상태 없음, 실패는 "원본 그대로 반환"(fail-open).
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import reframe

# LLM 을 쓰지 않는 조회 도구. 응답이 빨라 버퍼링 비용이 없다.
TOOLS: frozenset = frozenset({
    "get_mental_model", "list_mental_models",
    "list_memories", "get_memory",
    "list_documents", "get_document",
    # upstream v1.1.0 지식 베이스 읽기 3개(issue #302). HC_KB_READ_TOOLS 가 꺼져 있으면 policy.decide 가
    # 먼저 거부하므로 여기 있어도 꺼짐 동작은 그대로다(허용된 응답만 이 목록을 본다).
    "get_knowledge_base_tree", "search_knowledge_base", "get_knowledge_page",
})

ENV_FLAG = "HC_UNESCAPE"

# 앞의 역슬래시가 짝수 개일 때만 진짜 이스케이프다(홀수면 `\\\\u` = 역슬래시 문자 + 'u').
_ESCAPE = re.compile(
    r"(?<!\\)((?:\\\\)*)\\u(?:([dD][89abAB][0-9a-fA-F]{2})\\u([dD][c-fC-F][0-9a-fA-F]{2})"
    r"|([0-9a-fA-F]{4}))"
)


def enabled() -> bool:
    """기본 ON. `HC_UNESCAPE=0` 으로 즉시 롤백할 수 있다."""
    return os.environ.get(ENV_FLAG, "1").strip().lower() not in {"0", "false", "no", "off"}


def _replace(match: "re.Match[str]") -> str:
    backslashes, high, low, single = match.groups()
    if high is not None:
        cp = 0x10000 + ((int(high, 16) - 0xD800) << 10) + (int(low, 16) - 0xDC00)
    else:
        cp = int(single, 16)
        if cp < 0x80 or 0xD800 <= cp <= 0xDFFF:
            return match.group(0)
    return backslashes + chr(cp)


def unescape_text(raw: str) -> str:
    """JSON 문자열의 비ASCII `\\uXXXX` 를 글자로 바꾼다. JSON 이 아니거나 값이 달라지면 원본."""
    if "\\u" not in raw:
        return raw
    try:
        before = json.loads(raw)
    except ValueError:
        return raw
    out = _ESCAPE.sub(_replace, raw)
    if out == raw:
        return raw
    try:
        if json.loads(out) != before:
            return raw
    except ValueError:
        return raw
    return out


def unescape_tool_result(result: Any) -> Tuple[Any, bool]:
    """MCP CallToolResult 의 structuredContent.result 와 text 블록을 함께 바꾼다. -> (result, 변경됐는가)

    upstream v0.8.3 은 같은 JSON 문자열을 2벌 싣는다(recall_prune.prune_tool_result 참조). 클라이언트마다
    읽는 쪽이 달라 한쪽만 바꾸면 두 표현이 어긋나므로, 같은 문자열은 같은 결과로 함께 바꾼다.
    """
    if not isinstance(result, dict) or result.get("isError"):
        return result, False

    memo: Dict[str, str] = {}         # 같은 문자열 2벌을 한 번만 처리하고 같은 결과로 맞춘다

    def _fix(raw: str) -> Optional[str]:
        if raw not in memo:
            memo[raw] = unescape_text(raw)
        new = memo[raw]
        return None if new == raw else new

    out = dict(result)
    changed = False

    structured = out.get("structuredContent")
    if isinstance(structured, dict) and isinstance(structured.get("result"), str):
        new = _fix(structured["result"])
        if new is not None:
            out["structuredContent"] = {**structured, "result": new}
            changed = True

    blocks = out.get("content")
    if isinstance(blocks, list):
        new_blocks: List[Any] = []
        blocks_changed = False
        for block in blocks:
            if (isinstance(block, dict) and block.get("type") == "text"
                    and isinstance(block.get("text"), str)):
                new = _fix(block["text"])
                if new is not None:
                    new_blocks.append({**block, "text": new})
                    blocks_changed = True
                    continue
            new_blocks.append(block)
        if blocks_changed:
            out["content"] = new_blocks
            changed = True

    return (out, True) if changed else (result, False)


def unescape_mcp_element(element: Any, ids: Any) -> Any:
    """JSON-RPC 응답 element 가 대상 호출의 결과면 이스케이프를 푼다. `ids` 밖은 손대지 않는다."""
    if not isinstance(element, dict) or reframe.element_id(element) not in ids:
        return element
    result = element.get("result")
    if result is None:
        return element
    fixed, changed = unescape_tool_result(result)
    if not changed:
        return element
    return {**element, "result": fixed}


def unescape_mcp_body(content_type: Optional[str], body: bytes, ids: Any) -> Tuple[bytes, Optional[str]]:
    """MCP 응답 바이트(본문 전체 또는 SSE 이벤트 1개)의 `ids` 결과를 푼다. -> (바이트, 실패 사유)

    실패(파싱·콜백·직렬화)면 원본 바이트와 사유를 돌려준다(fail-open, 카운터·audit 는 호출자 몫).
    바꿀 게 없으면 원본 바이트 그대로다.
    """
    parsed = reframe.parse_response(content_type, body)
    rewritten = reframe.transform_results(parsed, lambda el: unescape_mcp_element(el, ids))
    _, out, serialize_failure = reframe.try_serialize(rewritten)
    return out, rewritten.failure or serialize_failure


class EventRewriter:
    """스트리밍 reflect 응답을 SSE 이벤트 경계마다 푼다(issue #26).

    feed() 는 청크를 받아 이번에 완결된 이벤트들을 (바이트, 실패 사유) 로 돌려준다. data 없는
    이벤트(`: ping`)는 파싱해도 바뀌지 않아 도착한 그대로 곧바로 나간다. finish() 는 EOF 의 끝 `\r`
    을 종결자로 보고 완결 이벤트는 변환하고, 종결 빈 줄 없는 꼬리는 원본 그대로 돌려준다. SSE 가
    아닌 media(application/json 등)는 본문을 전부 모아 finish() 에서 한 번에 푼다. 출력을 모두 이으면
    변환 안 된 이벤트는 입력과 바이트 동일하다. 어떤 예외도 밖으로 던지지 않는다 - 그 이벤트(분할
    실패면 남은 버퍼)의 원본 바이트와 "rewrite-exception:<클래스>" 사유를 돌려준다(스트림 유지).
    """

    def __init__(self, content_type: Optional[str], ids: Any,
                 convert: Optional[Callable[[Optional[str], bytes], Tuple[bytes, Optional[str]]]] = None):
        """`convert(content_type, 이벤트 또는 본문 바이트) -> (바이트, 실패 사유)` 를 주면 이벤트마다 그것을
        쓴다(issue #55: 같은 경계에서 based_on 지우기를 이스케이프 해제와 함께 돌린다). 없으면 #26 그대로다."""
        self._content_type = content_type
        self._ids = ids
        self._convert_fn = convert
        self._sse = reframe._media_type(content_type or "") == reframe.SSE_MEDIA_TYPE
        self._splitter = reframe.EventSplitter()
        self._body = bytearray()      # 비 SSE 본문

    def _convert(self, event: bytes) -> Tuple[bytes, Optional[str]]:
        try:
            if self._convert_fn is not None:
                return self._convert_fn(self._content_type, event)
            return unescape_mcp_body(self._content_type, event, self._ids)
        except Exception as exc:  # noqa: BLE001 - fail-open: 이 이벤트만 원본으로
            return event, "rewrite-exception:" + type(exc).__name__

    def _bail(self, exc: Exception) -> List[Tuple[bytes, Optional[str]]]:
        raw = self._splitter.pending()
        self._splitter = reframe.EventSplitter()
        return [(raw, "rewrite-exception:" + type(exc).__name__)] if raw else []

    def feed(self, chunk: bytes) -> List[Tuple[bytes, Optional[str]]]:
        if not self._sse:
            self._body += chunk
            return []
        try:
            events = self._splitter.feed(chunk)
        except Exception as exc:  # noqa: BLE001 - 분할 실패면 받은 바이트 전부를 원본으로
            return self._bail(exc)
        return [self._convert(event) for event in events]

    def finish(self) -> List[Tuple[bytes, Optional[str]]]:
        if not self._sse:
            body, self._body = bytes(self._body), bytearray()
            return [self._convert(body)] if body else []
        try:
            events, tail = self._splitter.finish()
        except Exception as exc:  # noqa: BLE001
            return self._bail(exc)
        out = [self._convert(event) for event in events]
        if tail:
            # issue #55: with a convert callback the unterminated tail is converted too (fail-open),
            # since the callback may have to remove something; without one it stays raw (#26).
            out.append(self._convert(tail) if self._convert_fn is not None else (tail, None))
        return out
