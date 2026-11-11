"""recall 상태 질의 보강 X1 (issue #339, 아키텍처 v2 런 3 의 R3-4a, 기능 플래그 기본 꺼짐).

왜: 상태를 묻는 recall 에서 완료 기록이 결과에 들지 않으면(검색 누락) 낡은 열림 기록이 이긴다. 런 2 진단에서 (c) 실패
13건의 판별 요인이 이것이었다(완료 근거가 결과에 든 run 에서만 not-open). 같은 도구에서 항목 번호만으로 찾으면 완료
근거가 나왔다(`memories/list?q=#<n>` 재현율 1.0, 지연 중앙값 141 ms, artifacts/arch-v2/r2-t2b-lookup.md).

무엇을(`HC_ITEM_STATUS=1` 일 때만, 기본 꺼짐): 게이트웨이 MCP `tools/call` `recall` 의 **성공 응답**에서
  1. 결과 행 본문·태그에서 작업 키를 뽑는다(`item_keys.extract_keys`, 새 규칙 없음). 한 응답에서 최대
     `HC_ITEM_STATUS_MAX_KEYS`(기본 8)개 키, 결과 순서로 먼저 나온 것부터.
  2. 키마다 `item_invalidate._lookup_list`(`q=#<n>` + project 태그 + `item:` 태그 조회의 합집합, state=valid)로 후보를 받는다.
     조회 범위 project 태그 = 그 키를 낸 결과 행의 `project:` 태그, 없으면 recall 요청 `tags` 의 `project:`. 둘 다 없으면
     그 키는 보강하지 않는다(조회도 안 한다, 키 상한에도 안 센다).
  3. 후보 중 같은 키를 가진 완료 계열(`kind:completion`, 또는 뱅크 `tag_aliases` 가 그것으로 바꾸는 옛 kind)만 고른다.
     요청에 `query_timestamp` 가 있으면 `mentioned_at` 이 그 시각 이하인 것만(시각 없음·읽을 수 없음은 제외, 요청 시각을
     읽을 수 없으면 그 recall 에는 아무것도 덧붙이지 않는다). 이미 결과에 있는 id 를 뺀 뒤 키당 `mentioned_at` 최신 1개.
  4. 고른 행을 결과 **끝**에 붙이고 `gateway_item_status: true` 를 단다. 원래 행의 집합·순서·내용은 바뀌지 않는다
     (upstream 이 payload 를 `structuredContent.result` 와 `content[].text` 에 같은 문자열로 2벌 싣는 것은 양쪽을 함께 바꾼다).
  5. 전체에 벽시계 상한 `HC_ITEM_STATUS_TIMEOUT_MS`(기본 1500)를 하나만 둔다(키별 합산이 아니다). 넘거나 upstream 오류가
     하나라도 있으면 보강 없이 원래 바이트 그대로 낸다(fail-open, 부분 보강 없음). 감사 줄(decision `item-status`)에
     사유 한 줄: `enriched` / `no-extras` / `timeout` / `lookup-failed:<code>` / `failed:<예외 이름>`. 본문은 남기지 않는다.
     조회할 키가 없으면 줄도 없다.

도달 범위(한계, 문서에도 적는다):
  * MCP `recall` 응답만이다. REST recall(`POST .../memories/recall`)은 범위 밖이다.
  * reflect·지식 페이지 내부 검색은 upstream 안에서 일어나 게이트웨이가 닿지 못한다(도달 범위 = recall 경로, 키 있는 항목).
  * 키 없는 항목은 보강하지 않는다. 호출자가 `types`·`tag_groups` 로 좁힌 요청에도 덧붙는다(`query_timestamp` 만 따른다).
  * 덧붙인 행은 memories/list 항목이라 recall 행의 `scores` 가 없다. 그 뒤 recall_prune·recall_label 이 같은 규칙으로 돈다.
  * 덧붙인 id 는 보강 뒤에 도는 recall ledger 의 result_ids 에도 들어간다(클라이언트에 실제로 돌려준 행이라 수용, #339 final-review).
  * 덧붙인 행은 호출자의 `tags`·`tags_match` 필터와 `max_tokens` 예산을 따르지 않는다: 필터 밖 kind 행이 붙을 수 있고 키 상한(기본 8)만큼의 본문이
    upstream 이 맞춘 예산 위에 더해진다(스펙 동작 1-4의 키별 덧붙임 설계 귀결, #339 final-review 2차).
  * list 조회의 `q=#<n>` 은 부분 일치(`#3` 이 `#30`·`#300` 에도 걸린다)라 한 질의가 500행(item_invalidate LIST_MAX_PAGES x LIST_PAGE_SIZE)을 넘으면
    `lookup-failed:truncated` 가 되고, 보강은 전부 아니면 전무라 그 응답의 다른 키 보강도 함께 없다(#339 final-review 2차).
  * 후보에 `fact_type` 필터가 없고 요청 `types` 도 따르지 않는다: `kind:completion` 을 가진 다른 종류 행(관찰 등)도 붙을 수 있다(#339 final-review 2차).

처리 순서(app.py): upstream 응답 전체를 읽은 직후(**이 모듈**) -> S1 원장(`item_ledger`, `HC_ITEM_LEDGER`, 덧붙인 행은 건드리지 않는다)
-> honesty/prune/label/unescape 합성 패스. 덧붙인 행도 그 패스를 같이 거친다.
설계: 판정은 규칙뿐이다(LLM·임베딩 없음). 키 추출·후보 조회는 item_keys·item_invalidate 를 그대로 쓴다.
정답표(세트 A·B·C)는 입력이 아니다.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple

import httpx

from . import item_invalidate, item_keys, recall_prune, recurrence, reframe
from .audit import Audit
from .retain_gates import _audit

ENV_FLAG = "HC_ITEM_STATUS"
ENV_MAX_KEYS = "HC_ITEM_STATUS_MAX_KEYS"
ENV_TIMEOUT_MS = "HC_ITEM_STATUS_TIMEOUT_MS"
DECISION = "item-status"
MARK_KEY = "gateway_item_status"
DEFAULT_MAX_KEYS = 8
DEFAULT_TIMEOUT_MS = 1500
_PROJECT_PREFIX = "project:"
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def enabled() -> bool:
    """기본 OFF. 1/true/yes/on 만 켠다."""
    return os.environ.get(ENV_FLAG, "0").strip().lower() in {"1", "true", "yes", "on"}


def _positive_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, "").strip())
    except ValueError:
        return default
    return value if value > 0 else default


def max_keys() -> int:
    return _positive_int(ENV_MAX_KEYS, DEFAULT_MAX_KEYS)


def timeout_ms() -> int:
    return _positive_int(ENV_TIMEOUT_MS, DEFAULT_TIMEOUT_MS)


# --- 순수 규칙 ------------------------------------------------------------------------------------

def _tags(row: Mapping[str, Any]) -> List[str]:
    raw = row.get("tags")
    if not isinstance(raw, list):
        return []
    return [t.strip().lower() for t in raw if isinstance(t, str) and t.strip()]


def _projects(tags: Sequence[str]) -> Tuple[str, ...]:
    return tuple(sorted({t for t in tags if t.startswith(_PROJECT_PREFIX)}))


def _text(row: Mapping[str, Any]) -> str:
    for key in ("text", "content"):
        if isinstance(row.get(key), str):
            return row[key]
    return ""


def _label(key: item_keys.ItemKey) -> str:
    return f"{key[0]}#{key[1]}"


def collect_keys(rows_by_element: Mapping[Any, Sequence[Any]], request_projects: Mapping[Any, Tuple[str, ...]],
                 aliases: Optional[Mapping[str, str]], limit: int
                 ) -> Tuple[List[item_keys.ItemKey], Dict[Any, Dict[item_keys.ItemKey, Tuple[str, ...]]]]:
    """결과 행에서 작업 키를 모은다. -> (조회할 키(결과 순서, 상한 적용), 요소별 {키: 그 요소의 조회 범위}).

    범위 = 키를 낸 행의 project 태그, 행에 없으면 요청 tags 의 project. 범위가 빈 키는 어디에도 싣지 않는다."""
    per_element: Dict[Any, Dict[item_keys.ItemKey, Set[str]]] = {}
    order: List[item_keys.ItemKey] = []
    for el_id, rows in rows_by_element.items():
        scopes = per_element.setdefault(el_id, {})
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            tags = _tags(row)
            scope = _projects(tags) or request_projects.get(el_id, ())
            if not scope:
                continue
            for key in sorted(item_keys.extract_keys(_text(row), tags, aliases)):
                scopes.setdefault(key, set()).update(scope)
                if key not in order:
                    order.append(key)
    chosen = order[:max(limit, 0)]
    keep = set(chosen)
    return chosen, {el_id: {k: tuple(sorted(v)) for k, v in scopes.items() if k in keep}
                    for el_id, scopes in per_element.items()}


def is_completion_family(row: Mapping[str, Any], tag_aliases: Optional[Mapping[str, str]]) -> bool:
    """`kind:completion`, 또는 뱅크 별칭 표가 그것으로 바꾸는 kind 를 가진 행."""
    table = tag_aliases if isinstance(tag_aliases, Mapping) else {}
    return any(table.get(t, t) == item_keys.KIND_COMPLETION for t in _tags(row))


def _instant(value: Any) -> Optional[datetime]:
    return item_keys._instant(value)


def select_extras(*, key_scopes: Mapping[item_keys.ItemKey, Tuple[str, ...]],
                  candidates: Mapping[item_keys.ItemKey, Sequence[Any]], result_ids: FrozenSet[str],
                  cutoff: Optional[datetime], aliases: Optional[Mapping[str, str]],
                  tag_aliases: Optional[Mapping[str, str]], skip_ids: Optional[Set[str]] = None) -> List[Dict[str, Any]]:
    """요소 하나의 덧붙일 행(키 순서, 키당 최대 1개). `skip_ids` 는 같은 요소에서 이미 덧붙인 id 이고 갱신된다."""
    taken = skip_ids if skip_ids is not None else set()
    out: List[Dict[str, Any]] = []
    for key, scope in key_scopes.items():
        eligible = []
        for cand in candidates.get(key, ()):
            if not isinstance(cand, Mapping) or not isinstance(cand.get("id"), str) or not cand["id"]:
                continue
            state = cand.get("state")
            if isinstance(state, str) and state != "valid":
                continue
            tags = _tags(cand)
            if not is_completion_family(cand, tag_aliases) or not set(scope) & set(_projects(tags)):
                continue
            if key not in item_keys.extract_keys(_text(cand), tags, aliases):
                continue
            at = _instant(cand.get("mentioned_at"))
            if cutoff is not None and (at is None or at > cutoff):
                continue
            eligible.append((at or _EPOCH, cand["id"], cand))
        eligible = [e for e in eligible if e[1] not in result_ids]
        if not eligible:
            continue
        _, cand_id, cand = max(eligible, key=lambda e: (e[0], e[1]))
        if cand_id in taken:
            continue
        taken.add(cand_id)
        out.append({**cand, MARK_KEY: True})
    return out


# --- 응답 읽기·쓰기 ---------------------------------------------------------------------------------

def _recall_rows(parsed: reframe.Parsed, wanted: Set[Any]) -> Dict[Any, List[Any]]:
    """{요소 id: 결과 행 목록}. 오류 결과·읽을 수 없는 payload 는 빠진다."""
    out: Dict[Any, List[Any]] = {}
    seen: Set[Any] = set()
    duplicated: Set[Any] = set()
    for frame in parsed.frames:
        elements = frame.payload if isinstance(frame.payload, list) else [frame.payload]
        for el in elements:
            if not isinstance(el, dict) or "method" in el:
                continue
            el_id = reframe.element_id(el)
            if el_id is None or el_id not in wanted:
                continue
            if el_id in seen:
                duplicated.add(el_id)
            seen.add(el_id)
            payload = recurrence.mcp_recall_payload(el.get("result"))
            if isinstance(payload, dict) and isinstance(payload.get("results"), list):
                out.setdefault(el_id, payload["results"])
    for el_id in duplicated:        # 같은 id 의 응답 요소가 둘 이상이면 어느 요청의 인자(cutoff 등)인지 알 수 없다: 보강하지 않는다
        out.pop(el_id, None)
    return out


def _extended(payload: Any, extras: Sequence[Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        return None
    return {**payload, "results": [*payload["results"], *extras]}


def append_tool_result(result: Any, extras: Sequence[Any]) -> Tuple[Any, bool]:
    """MCP CallToolResult 의 결과 끝에 `extras` 를 붙인다. -> (result, 변경됐는가)"""
    if not extras:
        return result, False
    return rewrite_tool_result(result, lambda payload: _extended(payload, extras))


def rewrite_tool_result(result: Any, rewrite: Callable[[Any], Optional[Dict[str, Any]]]) -> Tuple[Any, bool]:
    """MCP CallToolResult 의 recall payload 를 `rewrite(payload) -> 새 payload | None(그대로)` 로 바꾼다. -> (result, 변경됐는가)

    recall_label.label_tool_result 와 같은 틀: payload 는 `structuredContent.result`(JSON 문자열 또는 dict)와
    `content[i].text` 에 같은 내용으로 2벌 실려 있어 양쪽을 함께 바꾼다. X1(append_tool_result)과 S1(item_ledger)이 같이 쓴다."""
    if not isinstance(result, dict) or result.get("isError"):
        return result, False
    encoded: Dict[str, Optional[str]] = {}

    def _rewrite_str(raw: str) -> Optional[str]:
        if raw not in encoded:
            try:
                new = rewrite(json.loads(raw))
            except ValueError:
                new = None
            encoded[raw] = None if new is None else recall_prune._dumps(new)
        return encoded[raw]

    out = dict(result)
    changed = False
    structured = out.get("structuredContent")
    if isinstance(structured, dict) and isinstance(structured.get("result"), str):
        new = _rewrite_str(structured["result"])
        if new is not None:
            out["structuredContent"] = {**structured, "result": new}
            changed = True
    elif isinstance(structured, dict) and isinstance(structured.get("result"), dict):
        new_payload = rewrite(structured["result"])
        if new_payload is not None:
            out["structuredContent"] = {**structured, "result": new_payload}
            changed = True
    elif isinstance(structured, dict) and "result" not in structured:
        new_payload = rewrite(structured)     # recurrence.mcp_recall_payload 가 읽는 직접 payload 모양
        if new_payload is not None:
            out["structuredContent"] = new_payload
            changed = True
    blocks = out.get("content")
    if isinstance(blocks, list):
        new_blocks: List[Any] = []
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                new = _rewrite_str(block["text"])
                if new is not None:
                    new_blocks.append({**block, "text": new})
                    changed = True
                    continue
            new_blocks.append(block)
        out["content"] = new_blocks
    return (out, True) if changed else (result, False)


# --- 후보 찾기(비동기) -----------------------------------------------------------------------------

async def _lookup_key(client: httpx.AsyncClient, upstream_key: str, bank: str, key: item_keys.ItemKey,
                      projects: Tuple[str, ...], aliases: Mapping[str, str], deadline: float) -> List[Dict[str, Any]]:
    plan = item_invalidate.Plan(completion={}, keys=(key,), projects=projects, aliases=dict(aliases),
                                transport="mcp", tool="recall")
    return await item_invalidate._lookup_list(client, upstream_key, bank, plan, deadline,
                                              {"lookup_status": []})


async def _lookup_all(client, upstream_key, bank, keys, scopes, aliases, budget_seconds
                      ) -> Dict[item_keys.ItemKey, List[Dict[str, Any]]]:
    """키마다 동시에 조회하고 전체에 벽시계 상한 하나를 둔다. 하나라도 실패하면 예외(남은 조회는 취소한다)."""
    deadline = time.monotonic() + budget_seconds
    tasks = [asyncio.ensure_future(_lookup_key(client, upstream_key, bank, key, scopes[key], aliases, deadline))
             for key in keys]
    try:
        found = await asyncio.wait_for(asyncio.gather(*tasks), timeout=budget_seconds)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return dict(zip(keys, found))


def _union_scopes(per_element: Mapping[Any, Mapping[item_keys.ItemKey, Tuple[str, ...]]],
                  keys: Sequence[item_keys.ItemKey]) -> Dict[item_keys.ItemKey, Tuple[str, ...]]:
    merged: Dict[item_keys.ItemKey, Set[str]] = {k: set() for k in keys}
    for scopes in per_element.values():
        for key, scope in scopes.items():
            merged[key].update(scope)
    return {k: tuple(sorted(v)) for k, v in merged.items()}


def _failure_reason(exc: BaseException) -> str:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    if isinstance(exc, item_invalidate.LookupFailed):
        return "timeout" if exc.code in ("timeout", "deadline") else f"lookup-failed:{exc.code}"
    return f"failed:{type(exc).__name__}"


async def enrich_mcp_body(*, media: Optional[str], body: bytes, recall_calls: Mapping[Any, Any],
                          client: httpx.AsyncClient, upstream_key: str, bank: str, bank_policy: Any,
                          audit: Audit, token: Optional[str], identity: Optional[str]) -> bytes:
    """MCP 응답 바이트(SSE 또는 JSON)에서 recall 성공 결과를 보강한 새 바이트. 어떤 실패에도 원본 바이트를 돌려준다."""
    started = time.monotonic()
    info: Dict[str, Any] = {"transport": "mcp", "rule_version": item_keys.RULE_VERSION,
                            "max_keys": max_keys(), "timeout_ms": timeout_ms(), "keys": [], "keys_count": 0,
                            "candidates": 0, "appended": 0, "appended_ids": []}

    def record(reason: str) -> None:
        info["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        _audit(audit, token=token, identity=identity, bank=bank, tool="recall", decision=DECISION,
               reason=reason, extra={"item_status": info})

    try:
        parsed = reframe.parse_response(media, body)
        if parsed.failure is not None or not parsed.frames:
            return body
        aliases = getattr(bank_policy, "item_repo_aliases", None) or {}
        tag_aliases = getattr(bank_policy, "tag_aliases", None) or {}
        rows_by_el = _recall_rows(parsed, set(recall_calls))
        cutoffs: Dict[Any, Optional[datetime]] = {}
        request_projects: Dict[Any, Tuple[str, ...]] = {}
        for el_id in list(rows_by_el):
            args = recall_calls.get(el_id)
            args = args if isinstance(args, Mapping) else {}
            raw_cutoff = args.get("query_timestamp")
            if raw_cutoff:
                cutoff = _instant(raw_cutoff)
                if cutoff is None:                 # 기준 시각을 못 읽으면 그 뒤 기억을 못 거른다: 덧붙이지 않는다
                    del rows_by_el[el_id]
                    continue
                cutoffs[el_id] = cutoff
            else:
                cutoffs[el_id] = None
            tags = args.get("tags")
            request_projects[el_id] = _projects(
                [t.strip().lower() for t in tags if isinstance(t, str)] if isinstance(tags, list) else [])
        keys, per_element = collect_keys(rows_by_el, request_projects, aliases, max_keys())
        if not keys:
            return body
        info["keys"] = [_label(k) for k in keys[:item_invalidate.MAX_AUDIT_LIST]]
        info["keys_count"] = len(keys)
        try:
            found = await _lookup_all(client, upstream_key, bank, keys, _union_scopes(per_element, keys),
                                      aliases, timeout_ms() / 1000.0)
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception as exc:  # noqa: BLE001 - 시간 초과·upstream 오류: 원래 응답 그대로
            record(_failure_reason(exc))
            return body
        info["candidates"] = sum(len(v) for v in found.values())
        extras_by_el: Dict[Any, List[Dict[str, Any]]] = {}
        for el_id, scopes in per_element.items():
            taken: Set[str] = set()                # 중복 제거는 요소 안에서만(배치의 다른 recall 은 따로 소비된다)
            result_ids = frozenset(r["id"] for r in rows_by_el[el_id]
                                   if isinstance(r, Mapping) and isinstance(r.get("id"), str))
            extras = select_extras(key_scopes=scopes, candidates=found, result_ids=result_ids,
                                   cutoff=cutoffs[el_id], aliases=aliases, tag_aliases=tag_aliases, skip_ids=taken)
            if extras:
                extras_by_el[el_id] = extras
        if not extras_by_el:
            record("no-extras")
            return body
        applied: Set[Any] = set()

        def add(el: dict) -> dict:
            extras = extras_by_el.get(reframe.element_id(el))
            if not extras:
                return el
            new, changed = append_tool_result(el.get("result"), extras)
            if not changed:
                return el
            applied.add(reframe.element_id(el))
            return {**el, "result": new}

        rewritten = reframe.transform_results(parsed, add)
        _, new_body, serialize_failure = reframe.try_serialize(rewritten)
        failure = rewritten.failure or serialize_failure
        if failure:
            record("failed:rewrite")
            return body
        done = [e for el_id in applied for e in extras_by_el[el_id]]
        info["appended"] = len(done)
        info["appended_ids"] = [e["id"] for e in done[:item_invalidate.MAX_AUDIT_LIST]]
        record("enriched" if done else "no-extras")
        return new_body
    except (asyncio.CancelledError, KeyboardInterrupt):
        raise
    except Exception as exc:  # noqa: BLE001 - 보강 실패가 recall 실패가 되면 안 된다
        record(f"failed:{type(exc).__name__}")
        return body
