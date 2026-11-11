"""recall 종류 표시 - 결과 행마다 "스냅샷(말한 시점)"인지 "통합됨(현재 상태 후보)"인지 적어 준다 (issue #302).

왜: upstream recall 행에는 종류 필드(MCP `fact_type`, REST `type`)만 있고 값은 world/experience/observation 이다.
에이전트는 원문 사실이 "그 시점에 말한 스냅샷"이고 관찰이 "통합된 현재 상태 후보"라는 차이를 필드 이름만으로는
읽어내지 못한다. 사람이 읽을 표시를 행에 직접 붙인다.

무엇을: `HC_RECALL_KIND_LABEL` 이 켜졌을 때만(기본 꺼짐) 각 행에 `gateway_kind_label` 필드를 **추가**한다.
  world·experience -> "스냅샷(그 시점에 말한 것)", observation -> "통합됨(현재 상태 후보)".
  모르는 종류·종류 없음·dict 아닌 행은 그대로 둔다(표시를 지어내지 않는다). 결과 집합·순서·기존 필드는 바뀌지 않는다.

표시 위치를 필드 추가로 정한 근거(텍스트 머리 접두어를 버린 이유):
  - 접두어는 `text` 를 바꾼다. `text` 는 클라이언트가 인용하고 invalidate/update 근거로 쓰며 벤치 판정기가 바이트로
    비교하는 값이라, 표시를 위해 본문 바이트를 건드리면 "결과 불변"(recall_prune 의 원칙, 운영자 2026-09-22)이 깨진다.
  - 필드 추가는 recall_prune 이 이미 지키는 "모르는 필드는 값이 있으면 남긴다" 규칙과 같은 방향이고,
    `gateway_` 접두는 REST 가 이미 쓰는 게이트웨이 주입 필드(`gateway_normalized_tags`, `gateway_warnings`)와 같은 이름 규칙이다.

처리 순서(app.py): recall_prune(내부 메타 제거) -> **이 모듈** -> unescape. honesty 는 recall 응답을 손대지 않고(retain 계열
전용), 게이트웨이 recall ledger 는 `id` 만 읽으며 REST 쪽은 ledger 가 원본 바이트를 먼저 읽은 뒤 마지막에 이 모듈이 돈다.
종류 필드는 prune 이 지우지 않으므로(DROP_ALWAYS 는 비어 있다) prune 이 꺼져 있어도 켜져 있어도 같은 입력을 본다.

설계: recall_prune 과 같다. 순수 함수, 표준 라이브러리만, 전역 상태 없음. 어떤 실패에도 원본을 그대로 돌려준다(fail-open).
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from . import recall_prune
from . import reframe

ENV_FLAG = "HC_RECALL_KIND_LABEL"
LABEL_KEY = "gateway_kind_label"

SNAPSHOT = "스냅샷(그 시점에 말한 것)"
CONSOLIDATED = "통합됨(현재 상태 후보)"
LABELS = {"world": SNAPSHOT, "experience": SNAPSHOT, "observation": CONSOLIDATED}

# 표면마다 종류 필드 이름이 다르다(fixtures/mcp/NOTES.md): MCP recall 은 fact_type, REST recall 은 type.
MCP_KIND_KEY = "fact_type"
REST_KIND_KEY = "type"


def enabled() -> bool:
    """기본 OFF. 1/true/yes/on 만 켠다."""
    return os.environ.get(ENV_FLAG, "0").strip().lower() in {"1", "true", "yes", "on"}


def label_row(row: Any, kind_key: str) -> Any:
    """행 1건에 표시를 더한다. 알 수 없으면 같은 객체를 돌려준다."""
    if not isinstance(row, dict) or LABEL_KEY in row:
        return row
    kind = row.get(kind_key)
    label = LABELS.get(kind) if isinstance(kind, str) else None
    if label is None:
        return row
    return {**row, LABEL_KEY: label}


def label_payload(payload: Any, kind_key: str) -> Any:
    """recall payload({results:[...]})의 행에 표시를 더한다. 건수·순서·행 밖 키 불변. 해당 없으면 같은 객체."""
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        return payload
    rows: List[Any] = [label_row(r, kind_key) for r in payload["results"]]
    if all(new is old for new, old in zip(rows, payload["results"])):
        return payload
    return {**payload, "results": rows}


def label_tool_result(result: Any) -> Tuple[Any, bool]:
    """MCP CallToolResult 에 표시를 더한다. -> (result, 변경됐는가)

    recall_prune.prune_tool_result 와 같은 틀이다: upstream 은 payload 를 `structuredContent.result`(JSON 문자열)와
    `content[i].text` 에 같은 문자열로 2벌 싣는다. 같은 문자열이었을 때는 양쪽을 함께 교체한다."""
    if not isinstance(result, dict) or result.get("isError"):
        return result, False

    encoded: Dict[str, str] = {}

    def _label_str(raw: str) -> Optional[str]:
        if raw in encoded:
            return encoded[raw]
        try:
            payload = json.loads(raw)
        except ValueError:
            return None
        labelled = label_payload(payload, MCP_KIND_KEY)
        if labelled is payload:
            return None
        encoded[raw] = recall_prune._dumps(labelled)
        return encoded[raw]

    out = dict(result)
    changed = False

    structured = out.get("structuredContent")
    if isinstance(structured, dict) and isinstance(structured.get("result"), str):
        new = _label_str(structured["result"])
        if new is not None:
            out["structuredContent"] = {**structured, "result": new}
            changed = True
    elif isinstance(structured, dict) and isinstance(structured.get("result"), dict):
        labelled = label_payload(structured["result"], MCP_KIND_KEY)
        if labelled is not structured["result"]:
            out["structuredContent"] = {**structured, "result": labelled}
            changed = True

    blocks = out.get("content")
    if isinstance(blocks, list):
        new_blocks: List[Any] = []
        for block in blocks:
            if (isinstance(block, dict) and block.get("type") == "text"
                    and isinstance(block.get("text"), str)):
                new = _label_str(block["text"])
                if new is not None:
                    new_blocks.append({**block, "text": new})
                    changed = True
                    continue
            new_blocks.append(block)
        if changed:
            out["content"] = new_blocks

    return (out, True) if changed else (result, False)


def label_mcp_element(element: Any, recall_ids: Any) -> Any:
    """JSON-RPC 응답 element 가 recall 호출의 결과면 표시를 더한다. `recall_ids` 밖의 element 는 손대지 않는다."""
    if not isinstance(element, dict) or reframe.element_id(element) not in recall_ids:
        return element
    result = element.get("result")
    if result is None:
        return element
    labelled, changed = label_tool_result(result)
    return {**element, "result": labelled} if changed else element


def label_rest_body(raw: bytes) -> bytes:
    """REST recall 응답 바이트에 표시를 더한다. 어떤 실패에도 원본 바이트를 그대로 돌려준다."""
    if not raw:
        return raw
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return raw
    labelled = label_payload(payload, REST_KIND_KEY)
    if labelled is payload:
        return raw
    try:
        return recall_prune._dumps(labelled).encode("utf-8")
    except (TypeError, ValueError):
        return raw
