"""reflect 의 include_based_on 기본값을 게이트웨이가 채우고, 응답에서는 지운다 (issue #55).

왜 (2026-09-18 ~ 10-01 reflect-demand 계측 10건):
  upstream v0.8.3 의 MCP reflect 는 include_based_on 기본값이 false 라(넓은 reflect 의 근거 사실이
  MCP 클라이언트 결과 한도를 넘을 수 있어서) 호출자가 옵션을 빼면 based_on 이 응답에 없다. 그래서
  reflect_demand 가 hit/miss 를 판정하지 못하고 10건 중 7건이 "unknown" 으로 남았다.

무엇을:
  요청: MCP tools/call reflect 의 arguments 에 include_based_on 이 없거나 null 일 때만 true 를 채운다.
        호출자가 명시한 값(true·false·그 밖의 무엇이든)은 그대로 둔다.
  응답: 채운 호출의 결과에서 based_on 키만 지운다 - structuredContent.result 와 text 블록 JSON 둘 다.
        호출자가 받는 응답은 채우지 않았을 때와 같은 모양·크기다(upstream 이 기본값 false 로 둔 이유가 그것이다).
        지울 때 들여쓰기·ensure_ascii·구분자를 원본 문자열에 맞춰 다시 써서 나머지 글자는 그대로다.
        계측(reflect_demand)은 app.py 가 upstream 원본 바이트를 읽으므로 지워도 hit/miss 가 남는다.
  채우기 전제(app.py 몫): 응답을 다시 찾을 id 가 있고 HC_UNESCAPE 가 켜져 있을 것 - 끄면 reflect 는 옛 바이트 통과다.
  채웠다는 사실은 audit 1줄(enforce-autofill, AUDIT_REASON)로 드러낸다 - 문구는 app.py 가 쓴다(P5).

어디에:
  MCP 표면만. REST(/v1/.../reflect)는 운영 스크립트가 원형을 쓰므로 절대 건드리지 않는다.

설계: 순수 함수, 표준 라이브러리 + reframe 만, 전역 상태 없음. 킬스위치 `HC_REFLECT_BASED_ON_DEFAULT`.
  변환 실패는 fail-open(원본 바이트 + 사유)이고 예외를 던지지 않는다 - reflect 는 약 100초 걸려서
  계측 결함이 호출을 실패시키면 안 된다(#26 EventRewriter 와 같은 원칙).
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional, Tuple

from . import reframe

TOOL = "reflect"
ARGUMENT = "include_based_on"
RESPONSE_KEY = "based_on"
AUDIT_REASON = "based-on-default:true"
ENV_FLAG = "HC_REFLECT_BASED_ON_DEFAULT"

# 원본 문자열이 어떤 json.dumps 설정으로 만들어졌는지 찾을 때 시도하는 (indent, ensure_ascii, separators).
# 실측 upstream v0.8.3 형식(indent=2, ensure_ascii=True, 기본 구분자)을 1순위로 둔다.
_UPSTREAM_FORMAT = (2, True, None)
_FORMATS: Tuple[Tuple[Optional[int], bool, Optional[Tuple[str, str]]], ...] = (_UPSTREAM_FORMAT,) + tuple(
    fmt
    for fmt in (
        (indent, ascii_, seps)
        for indent in (None, 2, 4)
        for ascii_ in (True, False)
        for seps in (None, (",", ":"))
    )
    if fmt != _UPSTREAM_FORMAT
)


def enabled() -> bool:
    """기본 ON. `HC_REFLECT_BASED_ON_DEFAULT=0` 으로 즉시 롤백할 수 있다."""
    return os.environ.get(ENV_FLAG, "1").strip().lower() not in {"0", "false", "no", "off"}


def fill_include_based_on(tool: Any, arguments: Any) -> Tuple[Any, bool]:
    """include_based_on 이 없거나 null 이면 true 를 채운 새 dict 를 돌려준다. -> (arguments, 채웠는가)

    대상 밖(다른 도구, 명시된 값, dict 가 아닌 인자, 킬스위치 0)은 받은 객체를 그대로 돌려준다.
    None 인자는 "인자 없음"이라 채운다.
    """
    if tool != TOOL or not enabled():
        return arguments, False
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict) or arguments.get(ARGUMENT) is not None:
        return arguments, False
    return {**arguments, ARGUMENT: True}, True


def _dump_like(raw: str, parsed: Dict[str, Any], stripped: Dict[str, Any]) -> str:
    """`stripped` 를 `raw` 와 같은 json.dumps 설정으로 쓴다. 맞는 설정이 없으면 값이 같은 근사 형식."""
    for indent, ensure_ascii, separators in _FORMATS:
        kwargs: Dict[str, Any] = {"indent": indent, "ensure_ascii": ensure_ascii}
        if separators is not None:
            kwargs["separators"] = separators
        if json.dumps(parsed, **kwargs) == raw:
            return json.dumps(stripped, **kwargs)
    return json.dumps(stripped, indent=2 if "\n" in raw else None, ensure_ascii=True)


def _strip_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in payload.items() if key != RESPONSE_KEY}


def strip_text(raw: str) -> str:
    """JSON 객체 문자열에서 based_on 키만 지운다. 지울 게 없거나 JSON 객체가 아니면 같은 문자열."""
    if RESPONSE_KEY not in raw:
        return raw
    try:
        parsed = json.loads(raw)
    except ValueError:
        return raw
    if not isinstance(parsed, dict) or RESPONSE_KEY not in parsed:
        return raw
    return _dump_like(raw, parsed, _strip_payload(parsed))


def strip_tool_result(result: Any) -> Tuple[Any, bool]:
    """MCP CallToolResult 의 structuredContent 와 text 블록에서 based_on 을 지운다. -> (result, 변경됐는가)

    upstream 은 같은 JSON 을 2벌 싣는다(structuredContent.result 문자열 + text 블록). reflect_demand 는
    structuredContent.result(문자열 또는 dict) → structuredContent 자체 → text 블록 순으로 읽으므로
    그 표현을 모두 지운다. 같은 문자열은 같은 결과로 맞춘다. isError 결과와 대상 밖 형태는 그대로다.
    """
    if not isinstance(result, dict) or result.get("isError"):
        return result, False

    memo: Dict[str, str] = {}

    def _fix(raw: str) -> Optional[str]:
        if raw not in memo:
            memo[raw] = strip_text(raw)
        return None if memo[raw] == raw else memo[raw]

    out = dict(result)
    changed = False

    structured = out.get("structuredContent")
    if isinstance(structured, dict):
        if "result" in structured:
            inner = structured["result"]
            if isinstance(inner, str):
                new = _fix(inner)
                if new is not None:
                    out["structuredContent"] = {**structured, "result": new}
                    changed = True
            elif isinstance(inner, dict) and RESPONSE_KEY in inner:
                out["structuredContent"] = {**structured, "result": _strip_payload(inner)}
                changed = True
        elif RESPONSE_KEY in structured:
            out["structuredContent"] = _strip_payload(structured)
            changed = True

    blocks = out.get("content")
    if isinstance(blocks, list):
        new_blocks = []
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


def strip_mcp_element(element: Any, ids: Any) -> Any:
    """JSON-RPC 응답 element 가 `ids`(게이트웨이가 채운 호출)의 결과면 based_on 을 지운다. `ids` 밖은 그대로."""
    if not isinstance(element, dict) or reframe.element_id(element) not in ids:
        return element
    result = element.get("result")
    if result is None:
        return element
    fixed, changed = strip_tool_result(result)
    if not changed:
        return element
    return {**element, "result": fixed}


def strip_mcp_body(content_type: Optional[str], body: bytes, ids: Any) -> Tuple[bytes, Optional[str]]:
    """MCP 응답 바이트(본문 전체 또는 SSE 이벤트 1개)에서 `ids` 결과의 based_on 을 지운다. -> (바이트, 실패 사유)

    실패(파싱·콜백·직렬화)면 원본 바이트와 사유를 돌려준다(fail-open, 카운터·audit 는 호출자 몫).
    지울 게 없으면 원본 바이트 그대로다. `ids` 가 비면 파싱도 하지 않는다.
    """
    if not ids:
        return body, None
    parsed = reframe.parse_response(content_type, body)
    rewritten = reframe.transform_results(parsed, lambda el: strip_mcp_element(el, ids))
    _, out, serialize_failure = reframe.try_serialize(rewritten)
    return out, rewritten.failure or serialize_failure
