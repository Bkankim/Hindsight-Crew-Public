"""recall 응답 프루너 - 클라이언트로 나가는 검색 결과에서 내부 메타를 걷어낸다.

왜 (2026-09-22 실측, personal-demo 4쿼리/30결과, o200k):
  recall 응답 토큰의 59.5%만이 실제 본문(`text`)이고, 25.7%가 검색엔진 내부 메타다.
  눈에 띄는 `\\n`·`\\"` 이스케이프(upstream 이중 인코딩의 부산물)는 **38토큰 / 1.1%**에
  불과해 손댈 가치가 없다. 호출측 파라미터로는 줄지 않는다는 것도 실측 반증됐다
  (types=["world"] +19%, prefer_observations +82%, budget=low ±0%: 무엇을 걸러도
  upstream이 빈자리를 더 긴 문서로 채운다). 남는 유일한 레버가 이 프루닝이다.

무엇을 (기본값은 **무손실 제거만**):
  값이 null/빈 컨테이너이거나, 다른 필드와 값이 같아 복원 가능한 것만 지운다. 실측 -20.0%.
  모르는 필드는 값이 있으면 무조건 남는다(화이트리스트가 아니라 블랙리스트)ㅡupstream 이
  스키마를 넓혀도 새 필드를 조용히 잃지 않는다.
  손실을 감수하는 제거는 DROP_ALWAYS 에 모아 두되 기본값을 비웠다(아래 상수 주석 참조).

무엇을 남기나: text·id·tags·occurred_start·context·fact_type·document_id·scores·chunk_id.
  단 chunk_id 가 요청 bank 와 document_id 로 다시 만들어지는 0 번 chunk 면 뺀다(FORMAT_DERIVABLE, issue #13).
  `id` 는 invalidate/update_memory 의 핸들이자 게이트웨이 자신의 recall ledger 가 읽는
  필드다(recurrence.recall_result_ids). `scores` 는 클라이언트가 검색 적중을 스스로
  판정하는 신호라 남긴다.

참고: `scores` 를 읽는 내부 소비자(context_triggers.rerank_score → retain_gates·
contradiction)는 **upstream 직결** 호출(context_triggers.upstream_recall 이 httpx base_url
로 `/v1/.../memories/recall` 을 친다)의 응답을 읽지, 게이트웨이 프록시를 통과하는 클라이언트
응답을 읽지 않는다. 그러니 DROP_ALWAYS 를 나중에 채우더라도 내부 게이트는 깨지지 않는다.

설계: 순수 함수, 표준 라이브러리만, 전역 상태 없음. 실패는 예외가 아니라 "원본 그대로
반환"으로 표면화한다(fail-open). 검색 **결과 집합과 순서는 절대 바뀌지 않는다** - 이 모듈은
렌더링만 건드리므로 A/B 없이 결과-불변 테스트로 증명 가능하다.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from . import reframe

# 값이 있어도 제거할 필드. **기본값은 비어 있다**(운영자 결정, 2026-09-22).
# 후보였던 scores·chunk_id 를 넣으면 29.6%까지 가지만(실측) 그 차이 9.6%p 는 정보를 실제로
# 버려서 얻는 몫이다. 특히 scores 는 클라이언트가 "이 쿼리가 빗나갔는가"를 알아채는 유일한
# 신호라(1위 1.059 vs 4위 0.987 같은 격차) 버리면 무관한 결과를 관련 있는 것처럼 읽게 된다.
# 무손실 제거만으로 이미 20.0% 를 얻으므로 나머지는 사지 않는다. 메커니즘은 남겨 둔다 -
# 평가 하네스(Phase 1~3)가 손실을 정량화한 뒤에야 다시 채울 수 있다.
DROP_ALWAYS: frozenset = frozenset()

# 값이 이 필드와 같으면 중복이므로 제거한다(복원 가능 = 무손실).
REDUNDANT_WITH = {"occurred_end": "occurred_start", "mentioned_at": "occurred_start"}

# 값이 (남은 필드, 요청 bank) 로 다시 만들어지면 제거한다(복원 가능 = 무손실, issue #13).
# upstream chunk 키는 "{bank_id}_{document_id}_{chunk_index}"(chunks 테이블 PK)이고 0 번만 규칙으로 복원한다 -
# 0 번이 아니거나 bank 를 모르면 남긴다. bench/judge.FORMAT_DERIVABLE 과 같은 선언이다.
# chunk_id 를 인자로 받는 MCP 도구는 없다(upstream REST /chunks/{id} 핸들뿐).
FORMAT_DERIVABLE = {
    "chunk_id": lambda row, bank: f"{bank}_{row['document_id']}_0" if bank and "document_id" in row else None,
}

ENV_FLAG = "HC_RECALL_PRUNE"


def enabled() -> bool:
    """기본 ON. `HC_RECALL_PRUNE=0` 으로 즉시 롤백할 수 있다."""
    return os.environ.get(ENV_FLAG, "1").strip().lower() not in {"0", "false", "no", "off"}


def _empty(value: Any) -> bool:
    """제거해도 무손실인 '값 없음'. 0·False·빈 문자열은 의미 있는 값이므로 남긴다."""
    return value is None or value == {} or value == []


def prune_result(row: Any, bank: Optional[str] = None) -> Any:
    """결과 1건에서 내부 메타를 걷어낸다. dict 가 아니면 그대로 통과.
    bank = 요청이 고정된 bank. 모르면 형식 파생 필드는 남긴다."""
    if not isinstance(row, dict):
        return row
    out: Dict[str, Any] = {}
    for key, value in row.items():
        if key in DROP_ALWAYS:
            continue
        if _empty(value):
            continue
        twin = REDUNDANT_WITH.get(key)
        if twin is not None and key in row and row.get(twin) == value:
            continue
        out[key] = value
    # 형식 파생은 나가는 결과로 판단한다 - 복원에 쓰는 필드가 위에서 빠졌으면 남긴다
    for key, rebuild in FORMAT_DERIVABLE.items():
        if key in out and rebuild(out, bank) == out[key]:
            del out[key]
    return out


def prune_payload(payload: Any, bank: Optional[str] = None) -> Any:
    """recall payload({results:[...]} 또는 결과 배열)를 프루닝한다. 건수·순서 불변."""
    if isinstance(payload, list):
        return [prune_result(r, bank) for r in payload]
    if not isinstance(payload, dict):
        return payload
    results = payload.get("results")
    if not isinstance(results, list):
        return payload
    out = {k: v for k, v in payload.items() if k == "results" or not _empty(v)}
    out["results"] = [prune_result(r, bank) for r in results]
    return out


def _dumps(value: Any) -> str:
    """압축 JSON 문자열. 유효한 문자열은 ensure_ascii=False 그대로다.

    upstream 내부 JSON 의 `\\ud800` 이스케이프를 json.loads 가 실제 lone surrogate 로 되살린다. 그대로
    다시 쓰면 strict utf-8 인코딩이 죽고(REST 는 조용한 원문 폴백), 되살린 채로 바깥 JSON 에 실리면
    strict 파서(Python mcp SDK)가 메시지 전체를 거부한다. surrogate 만 upstream 이 쓴 수준 그대로
    `\\udXXX` 로 되돌린다 (issue #138)."""
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            .encode("utf-8", "backslashreplace").decode("utf-8"))


def prune_tool_result(result: Any, bank: Optional[str] = None) -> Tuple[Any, bool]:
    """MCP CallToolResult 를 프루닝한다. -> (result, 변경됐는가)

    upstream v0.8.3 은 payload 를 DOUBLE-encode 해서 `structuredContent.result`(JSON 문자열)
    와 `content[0].text` 에 **같은 문자열**을 2벌 싣는다. 한쪽만 고치면 두 표현이 어긋나므로
    (클라이언트마다 읽는 쪽이 다르다) 같은 문자열이었을 때는 양쪽을 함께 교체한다.
    """
    if not isinstance(result, dict) or result.get("isError"):
        return result, False

    encoded: Dict[str, str] = {}      # 원본 문자열 -> 프루닝된 문자열 (2벌 동기화용)

    def _prune_str(raw: str) -> Optional[str]:
        if raw in encoded:
            return encoded[raw]
        try:
            payload = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(payload, (dict, list)):
            return None
        pruned = prune_payload(payload, bank)
        if pruned == payload:
            return None
        encoded[raw] = _dumps(pruned)
        return encoded[raw]

    out = dict(result)
    changed = False

    structured = out.get("structuredContent")
    if isinstance(structured, dict) and isinstance(structured.get("result"), str):
        new = _prune_str(structured["result"])
        if new is not None:
            out["structuredContent"] = {**structured, "result": new}
            changed = True
    elif isinstance(structured, dict) and isinstance(structured.get("result"), (dict, list)):
        pruned = prune_payload(structured["result"], bank)
        if pruned != structured["result"]:
            out["structuredContent"] = {**structured, "result": pruned}
            changed = True

    blocks = out.get("content")
    if isinstance(blocks, list):
        new_blocks: List[Any] = []
        for block in blocks:
            if (isinstance(block, dict) and block.get("type") == "text"
                    and isinstance(block.get("text"), str)):
                new = _prune_str(block["text"])
                if new is not None:
                    new_blocks.append({**block, "text": new})
                    changed = True
                    continue
            new_blocks.append(block)
        if changed:
            out["content"] = new_blocks

    return (out, True) if changed else (result, False)


def prune_rest_body(raw: bytes, bank: Optional[str] = None) -> bytes:
    """REST recall 응답 바이트를 프루닝한다. 어떤 실패에도 원본 바이트를 그대로 돌려준다.

    REST 표면은 이중 인코딩 없이 `{"results":[...]}` 를 바로 싣는다(MCP 쪽 shape 와 다름).
    """
    if not raw:
        return raw
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return raw
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        return raw
    pruned = prune_payload(payload, bank)
    if pruned == payload:
        return raw
    try:
        return _dumps(pruned).encode("utf-8")
    except (TypeError, ValueError):
        return raw


def prune_mcp_element(element: Any, recall_ids: Any, bank: Optional[str] = None) -> Any:
    """JSON-RPC 응답 element 가 recall 호출의 결과면 프루닝한다.

    `recall_ids` 에 없는 element(retain·tools/list 등)는 손대지 않는다 - 이 모듈이 다른
    도구의 응답을 건드릴 수 없게 하는 유일한 관문이다.
    """
    if not isinstance(element, dict) or reframe.element_id(element) not in recall_ids:
        return element
    result = element.get("result")
    if result is None:
        return element
    pruned, changed = prune_tool_result(result, bank)
    if not changed:
        return element
    return {**element, "result": pruned}
