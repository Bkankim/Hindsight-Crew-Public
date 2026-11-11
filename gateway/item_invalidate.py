"""완료 retain 뒤 같은 작업 키의 열림 원문 무효화 훅 (issue #321, 아키텍처 v2 런 2 의 B 처치, 기능 플래그 기본 꺼짐).

`HC_ITEM_INVALIDATE=1` 일 때만, 개인 뱅크의 retain(MCP `retain`·`sync_retain`, REST `POST .../memories`) 항목마다:
  1. 저장될 태그(별칭 정규화 뒤)에 `kind:completion` 이 있고 `item_keys.extract_keys` 가 키를 하나 이상 내면 계획(Plan)을
     만든다. 이 준비는 요청 안에서 한다(순수 계산, upstream 호출 없음 - gateway/retain_gates.py 와 같은 자리).
  2. 저장 확인 뒤(gate_effects.finalize, 응답 밖 BackgroundTask) 후보를 찾아 `item_keys.find_targets` 로 대상을 고르고
     upstream `PATCH /memories/{id} {"state": "invalidated", "reason": ...}` 로 무효화한다. upstream 은 그 원문의
     파생 관찰을 지우고 공동 출처를 재통합 큐에 넣는다(통합 LLM 호출이 생긴다, v1.1.0 `api/memories.mdx` 178-179행).
  3. 완료 하나에 감사 줄 하나(decision `item-invalidate`): 뱅크, 완료 쪽 operation id, 키, 대상 memory id, over_cap, 찾기 방식,
     RULE_VERSION, upstream 상태 코드. 기억 본문·retain 본문은 남기지 않는다.

모든 실패는 fail-open 이다: retain 응답과 상태 코드는 바뀌지 않고 감사 줄만 남는다. 팀 뱅크는 계획 자체가 만들어지지 않는다
(policy.CORRECTIVE_TOOLS 규칙: 교정은 개인 뱅크만). LLM 판정은 없다(D8, 규칙은 item_keys 가 전부).

찾기 방식(`HC_ITEM_INVALIDATE_LOOKUP`, 기본 `list`, 사본 비교 뒤 확정 - artifacts/arch-v2/r2-t2b-lookup.md):
  * `list`: `GET memories/list?q=#<n>&tags=<project 태그들>&tags_match=any_strict&state=valid`(색인 없는 전수 스캔, 결정적). 키마다 한 번,
    `#<n>` 이 `<repo>#<n>`·`<repo> #<n>`·`#<n>` 표기를 모두 덮는다(과한 후보는 is_target 이 거른다). 키가 `item:<repo>-<n>` 태그로만
    있고 본문에 `#<n>` 이 없는 후보는 q 가 못 찾으므로, 키마다 (그 repo 의 별칭 표기 x project 태그)별로
    `tags=[item:<표기>-<n>, <project>]&tags_match=all_strict&state=valid` 조회를 더해 합집합으로 판정한다(같은 id 는 한 번).
    항목에 `state`·`fact_type`·`tags`·`mentioned_at`·`document_id` 가 다 실려 와 그대로 판정에 넘긴다.
  * `recall`: `POST memories/recall`(query `<repo>#<n>`, 같은 project 태그, world·experience)은 후보 id 찾기에만 쓴다. recall 행에는
    `state` 가 없어서 후보마다 `GET memories/{id}` 로 읽은 응답(상태 포함)을 판정에 넘긴다. state 를 지어 넣지 않는다. GET 이
    실패한 후보는 비대상이고 건수가 감사 줄에 남는다.
  후보 조회가 하나라도 실패·잘리면(페이지 상한) 이 완료는 아무것도 무효화하지 않는다(부분 후보 집합은 over_cap 을 속인다).
  recall 방식에서 적중이 MAX_HYDRATE 를 넘으면 잘린 목록으로 판정하지 않고 `skip:hydrate-cap` 으로 아무것도 하지 않는다.

운영에서 바꾸지 않는 값: `MAX_KEYS`(키가 5개를 넘는 완료는 넓은 표기로 보고 `skip:too-many-keys`)와 `HC_ITEM_INVALIDATE_LOOKUP`
(기본 list, docker-compose 에 배선하지 않았다. recall 방식은 사본 비교용 코드다). 요청당 계획 상한(MAX_PLANS_PER_REQUEST)을
넘는 완료는 조용히 빠지지 않고 `skip:plan-cap` 감사 줄(키, 완료 operation id, RULE_VERSION)을 남긴다. skip 줄의 operation id 는
없으면 null 이다.

알려진 한계(고치지 않기로 함, 모두 실패 방향이 "무효화 안 함"이거나 기존 게이트와 같은 방식):
  * list 방식 q 는 부분 일치(`#3` 이 `#30`·`#300` 에도 걸린다)라 한 질의가 500행(LIST_MAX_PAGES x LIST_PAGE_SIZE)을 넘으면
    `lookup-failed:truncated` 로 그 완료는 무효화하지 않는다(project 범위가 any_strict 라 행 수는 줄어 있다).
  * 감사 줄 `keys` 는 완료 본문의 `<owner>/<repo>#<n>` repo slug 를 그대로 싣는다(감사 로그는 리포 밖 운영 로컬 파일이고 길이는
    MAX_AUDIT_LIST 로 묶인다).
  * MCP 계획·저장 확인은 JSON-RPC id 로만 짝짓는다(기존 recurrence 게이트와 같은 방식). 같은 id 의 원소가 한 배치에 둘 이상이면
    뒤 계획이 앞 계획을 덮거나 다른 원소의 operation 에 묶일 수 있다. 운영 적용 창 전에 다룰 후보다.
  * memories/list 는 offset 페이지라 읽는 사이 동시 PATCH·retain 이 있으면 행이 밀려 후보를 놓칠 수 있고, 여러 완료가 한 배치에
    있을 때 앞 완료의 조회·PATCH 가 공유 operation 대기 기한(300초)을 소진하면 뒤 완료의 저장 확인이 실패한다. PATCH 는 대상마다
    순차(최대 PATCH_TIMEOUT_SECONDS)라 finalize 슬롯(MAX_ACTIVE_FINALIZE)을 오래 잡을 수 있다.

완료 시각: retain 에 timestamp 가 없으면(정책 `timestamp_autofill` 이 꺼진 뱅크) 게이트웨이가 요청을 받은 시각(현재)을 완료의
`mentioned_at` 으로 쓴다. upstream 도 같은 시각을 기록하는 정상 동작이라 건너뛰지 않는다.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote

import httpx

from . import item_keys, policy, reframe, rules
from .audit import Audit
from .retain_gates import _audit

ENV_FLAG = "HC_ITEM_INVALIDATE"
ENV_LOOKUP = "HC_ITEM_INVALIDATE_LOOKUP"
DECISION = "item-invalidate"
LOOKUP_LIST = "list"
LOOKUP_RECALL = "recall"
DEFAULT_LOOKUP = LOOKUP_LIST

MAX_PLANS_PER_REQUEST = 3        # retain_gates.MAX_ITEMS_PER_REQUEST 와 같은 상한
MAX_KEYS = 5                     # 한 완료가 가리키는 키가 더 많으면 넓은 표기로 보고 건드리지 않는다
LIST_PAGE_SIZE = 100
LIST_MAX_PAGES = 5               # 키마다 500행까지. 넘으면 후보 집합이 불완전하니 중단
RECALL_MAX_TOKENS = 4096
MAX_HYDRATE = 30                 # recall 방식에서 GET 으로 채울 후보 상한(키 합계)
MAX_AUDIT_LIST = 20              # 감사 줄 한 필드(keys·target_ids)에 싣는 항목 상한. 넘으면 앞쪽만 싣고 `*_count` 를 남긴다
CALL_TIMEOUT_SECONDS = 8.0
LOOKUP_BUDGET_SECONDS = 30.0
PATCH_TIMEOUT_SECONDS = 10.0
_REF_CLEAN = re.compile(r"[^A-Za-z0-9._:-]")


def enabled() -> bool:
    """기본 OFF. 1/true/yes/on 만 켠다."""
    return os.environ.get(ENV_FLAG, "0").strip().lower() in {"1", "true", "yes", "on"}


def lookup_mode() -> str:
    value = os.environ.get(ENV_LOOKUP, DEFAULT_LOOKUP).strip().lower()
    return value if value in (LOOKUP_LIST, LOOKUP_RECALL) else DEFAULT_LOOKUP


@dataclass(frozen=True)
class Plan:
    """요청 안에서 만든 완료 하나의 판정 준비. 본문은 메모리에만 있고 감사 줄에는 안 간다."""

    completion: Mapping[str, Any]            # is_target 에 넘기는 완료 쪽(text·tags·mentioned_at·document_id)
    keys: Tuple[item_keys.ItemKey, ...]
    projects: Tuple[str, ...]                # 완료의 project 태그(조회 범위)
    aliases: Mapping[str, str]
    transport: str
    tool: str
    skip: Optional[str] = None               # 계획 상한 등으로 실행하지 않는 완료의 사유(감사 줄만 남긴다)


def _audit_keys(plan: Plan) -> Dict[str, Any]:
    """감사 줄의 키 필드: 앞 MAX_AUDIT_LIST 개만 싣고 전체 개수는 keys_count 로 남긴다(줄 크기 상한, 회전 없는 로그)."""
    return {"keys": [f"{repo}#{n}" for repo, n in plan.keys[:MAX_AUDIT_LIST]], "keys_count": len(plan.keys)}


def _clean_str(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def prepare_item(item: Any, bank_policy: Any, *, transport: str, tool: str) -> Optional[Plan]:
    """retain 항목 하나(규칙 통과 뒤 저장될 모양)가 계획 대상이면 Plan, 아니면 None."""
    if not isinstance(item, dict):
        return None
    tags = item.get("tags")
    content = item.get("content")
    if not isinstance(content, str) or not item_keys.is_completion({"tags": tags}):
        return None
    aliases = getattr(bank_policy, "item_repo_aliases", None) or {}
    keys = item_keys.extract_keys(content, tags, aliases)
    if not keys:
        return None
    tag_set = [t.strip().lower() for t in tags if isinstance(t, str) and t.strip()]
    projects = tuple(sorted({t for t in tag_set if t.startswith("project:")}))
    raw_stamp = item.get("timestamp")
    stamp = (datetime.now(timezone.utc).isoformat() if rules.is_unset_timestamp(raw_stamp)
             else _clean_str(raw_stamp) or datetime.now(timezone.utc).isoformat())
    completion = {"text": content, "tags": tag_set, "mentioned_at": stamp,
                  "document_id": _clean_str(item.get("document_id"))}
    return Plan(completion=completion, keys=tuple(sorted(keys)), projects=projects,
                aliases=dict(aliases), transport=transport, tool=tool)


def _capped(plan: Plan) -> Plan:
    """상한을 넘은 완료: 본문(completion)은 버리고 키만 들고 `skip:plan-cap` 감사 줄 하나를 남긴다."""
    return replace(plan, completion={}, skip="plan-cap")


def _personal(principal: Any, bank: str) -> bool:
    return principal is not None and bank == getattr(principal, "personal", None)


def prepare_mcp(*, batch: Sequence[Any], decisions: Sequence[Any], verdicts: Sequence[Any], bank: str,
                principal: Any, bank_policy: Any) -> Dict[Any, Plan]:
    """MCP 요청의 retain·sync_retain 원소별 계획(응답 id 키). 꺼짐·팀 뱅크면 빈 dict."""
    if not enabled() or not _personal(principal, bank):
        return {}
    plans: Dict[Any, Plan] = {}
    for pos, (el, d) in enumerate(zip(batch, decisions)):
        try:
            if not (isinstance(el, dict) and el.get("method") == "tools/call"
                    and getattr(d, "tool", None) in policy.RETAIN_TOOLS):
                continue
            el_id = reframe.element_id(el)
            if el_id is None:
                continue
            item = getattr(verdicts[pos], "item", None) if pos < len(verdicts) else None
            plan = prepare_item(item, bank_policy, transport="mcp", tool=d.tool)
            if plan is not None:
                plans[el_id] = _capped(plan) if len(plans) >= MAX_PLANS_PER_REQUEST else plan
        except Exception:  # noqa: BLE001 - 준비 버그가 retain 을 막으면 안 된다
            continue
    return plans


def prepare_rest(*, items: Any, bank: str, principal: Any, bank_policy: Any) -> Dict[int, Plan]:
    """REST `POST .../memories` 항목별 계획(항목 index 키). 꺼짐·팀 뱅크면 빈 dict."""
    if not enabled() or not _personal(principal, bank) or not isinstance(items, list):
        return {}
    plans: Dict[int, Plan] = {}
    for idx, item in enumerate(items):
        try:
            plan = prepare_item(item, bank_policy, transport="rest", tool="retain")
            if plan is not None:
                plans[idx] = _capped(plan) if len(plans) >= MAX_PLANS_PER_REQUEST else plan
        except Exception:  # noqa: BLE001
            continue
    return plans


# --- 후보 찾기 -------------------------------------------------------------------------------------

class LookupFailed(Exception):
    """후보 조회가 실패·불완전. `code` 만 감사 줄에 간다(upstream 메시지는 질의를 되풀이할 수 있다)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _headers(upstream_key: str) -> Dict[str, str]:
    headers = {"accept": "application/json"}
    if upstream_key:
        headers["authorization"] = f"Bearer {upstream_key}"
    return headers


def _bank_path(bank: str, suffix: str) -> str:
    return f"/v1/default/banks/{quote(bank, safe='')}{suffix}"


async def _request(client: httpx.AsyncClient, method: str, path: str, *, upstream_key: str,
                   deadline: float, statuses: List[int], **kwargs) -> Any:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise LookupFailed("deadline")
    try:
        resp = await client.request(method, path, headers=_headers(upstream_key),
                                    timeout=min(CALL_TIMEOUT_SECONDS, remaining), **kwargs)
    except httpx.TimeoutException:
        raise LookupFailed("timeout") from None
    except (httpx.HTTPError, OSError):
        raise LookupFailed("transport") from None
    statuses.append(resp.status_code)
    if resp.status_code != 200:
        raise LookupFailed(f"http-{resp.status_code}")
    try:
        return resp.json()
    except ValueError:
        raise LookupFailed("bad-json") from None


def _spellings(repo: str, aliases: Mapping[str, str]) -> List[str]:
    """repo 를 가리키는 `item:` 태그 표기들: 정규 repo 와 그 repo 로 가는 별칭 키(소문자 slug)."""
    names = {repo} | {k.strip().lower() for k, v in aliases.items()
                      if isinstance(k, str) and isinstance(v, str) and v.strip().lower() == repo}
    return sorted(n for n in names if n)


async def _list_pages(client, upstream_key, bank, base_params, deadline, info, rows) -> None:
    """memories/list 한 질의를 페이지 끝까지 읽어 rows(id 키)에 합친다. 상한을 넘으면 LookupFailed(truncated)."""
    offset, pages = 0, 0
    while True:
        params = [*base_params, ("limit", LIST_PAGE_SIZE), ("offset", offset)]
        data = await _request(client, "GET", _bank_path(bank, "/memories/list"), params=params,
                              upstream_key=upstream_key, deadline=deadline, statuses=info["lookup_status"])
        items, total = (data.get("items"), data.get("total")) if isinstance(data, dict) else (None, None)
        if not isinstance(items, list) or not isinstance(total, int):
            raise LookupFailed("bad-shape")
        for row in items:
            if isinstance(row, dict) and isinstance(row.get("id"), str):
                rows.setdefault(row["id"], row)
        offset += len(items)
        pages += 1
        if not items or offset >= total:
            return
        if pages >= LIST_MAX_PAGES:
            raise LookupFailed("truncated")


async def _lookup_list(client, upstream_key, bank, plan: Plan, deadline, info) -> List[Dict[str, Any]]:
    rows: Dict[str, Dict[str, Any]] = {}
    for repo, number in plan.keys:
        text_params: List[Tuple[str, Any]] = [("q", f"#{number}"), ("state", "valid")]
        text_params.extend(("tags", t) for t in plan.projects)
        if plan.projects:
            text_params.append(("tags_match", "any_strict"))      # `any` 는 태그 없는 행까지 포함한다
        await _list_pages(client, upstream_key, bank, text_params, deadline, info, rows)
        # 키가 `item:` 태그에만 있는 후보(본문에 `#<n>` 없음): 표기마다·project 마다 태그 조회
        for spelling in _spellings(repo, plan.aliases):
            for project in plan.projects:
                tag_params = [("tags", f"item:{spelling}-{number}"), ("tags", project),
                              ("tags_match", "all_strict"), ("state", "valid")]
                await _list_pages(client, upstream_key, bank, tag_params, deadline, info, rows)
    return list(rows.values())


async def _lookup_recall(client, upstream_key, bank, plan: Plan, deadline, info) -> List[Dict[str, Any]]:
    ids: List[str] = []
    for repo, number in plan.keys:
        body: Dict[str, Any] = {"query": f"{repo}#{number}", "types": ["world", "experience"],
                                "max_tokens": RECALL_MAX_TOKENS}
        if plan.projects:
            body.update(tags=list(plan.projects), tags_match="any")
        data = await _request(client, "POST", _bank_path(bank, "/memories/recall"), json=body,
                              upstream_key=upstream_key, deadline=deadline, statuses=info["lookup_status"])
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            raise LookupFailed("bad-shape")
        for row in results:
            if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"] not in ids:
                ids.append(row["id"])
    info["recall_hits"] = len(ids)
    if len(ids) > MAX_HYDRATE:
        raise LookupFailed("hydrate-cap")      # 잘린 후보로 판정하면 over_cap 을 속인다
    rows: List[Dict[str, Any]] = []
    failed = 0
    for mem_id in ids:
        try:
            detail = await _request(client, "GET", _bank_path(bank, f"/memories/{quote(mem_id, safe='')}"),
                                    upstream_key=upstream_key, deadline=deadline,
                                    statuses=info["hydrate_status"])
        except LookupFailed:
            failed += 1                 # 읽지 못한 후보는 비대상(fail-open)
            continue
        if isinstance(detail, dict):
            rows.append({**detail, "id": detail.get("id", mem_id)})
        else:
            failed += 1
    info["hydrate_failed"] = failed
    return rows


# --- 실행(저장 확인 뒤, 응답 밖) ------------------------------------------------------------------------

def _ref(plan: Plan, operation: Any) -> str:
    """무효화 reason 에 남길 완료 쪽 출처: 문서 id, 없으면 operation id, 없으면 `unknown`."""
    ops = (operation,) if isinstance(operation, str) else tuple(operation or ())
    raw = plan.completion.get("document_id") or (ops[0] if ops else "unknown")
    return _REF_CLEAN.sub("", str(raw))[:64] or "unknown"


def _operation_ids(operation: Any) -> List[str]:
    return [operation] if isinstance(operation, str) else [o for o in (operation or ()) if isinstance(o, str)]


def skip_extra(plan: Plan, operation: Any, **more: Any) -> Dict[str, Any]:
    """skip 감사 줄의 `item_invalidate` 값: 키·완료 operation id(없으면 null)·RULE_VERSION."""
    return {"item_invalidate": {"transport": plan.transport, "rule_version": item_keys.RULE_VERSION,
                                **_audit_keys(plan),
                                "operation_id": _operation_ids(operation) or None, **more}}


async def execute(*, client: httpx.AsyncClient, upstream_key: str, bank: str, plan: Plan, operation: Any,
                  audit: Audit, token: Optional[str], identity: Optional[str]) -> None:
    """저장이 확인된 완료 하나의 무효화. 절대 raise 하지 않고, 결과는 감사 줄 하나다."""
    mode = lookup_mode()
    info: Dict[str, Any] = {
        "transport": plan.transport, "lookup": mode, "rule_version": item_keys.RULE_VERSION,
        "operation_id": _operation_ids(operation), **_audit_keys(plan),
        "lookup_status": [], "hydrate_status": [], "candidates": 0, "target_ids": [],
        "over_cap": False, "invalidated_ids": [], "patch_status": {},
    }
    started = time.monotonic()

    def record(reason: str) -> None:
        info["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        _audit(audit, token=token, identity=identity, bank=bank, tool=plan.tool, decision=DECISION,
               reason=reason, extra={"item_invalidate": info})

    try:
        if plan.skip:
            return record(f"skip:{plan.skip}")
        if len(plan.keys) > MAX_KEYS:
            return record("skip:too-many-keys")
        if not plan.projects:
            return record("skip:no-project-tag")      # is_target 은 같은 project 태그를 요구한다
        deadline = started + LOOKUP_BUDGET_SECONDS
        try:
            finder = _lookup_recall if mode == LOOKUP_RECALL else _lookup_list
            candidates = await finder(client, upstream_key, bank, plan, deadline, info)
        except LookupFailed as exc:
            return record("skip:hydrate-cap" if exc.code == "hydrate-cap" else f"lookup-failed:{exc.code}")
        info["candidates"] = len(candidates)
        result = item_keys.find_targets(plan.completion, candidates, plan.aliases)
        target_ids = [t["id"] for t in result.targets if isinstance(t.get("id"), str)]
        info["target_ids"], info["target_count"] = target_ids[:MAX_AUDIT_LIST], len(target_ids)
        info["over_cap"] = result.over_cap
        if result.over_cap:
            return record("over-cap")
        if not result.targets:
            return record("no-targets")
        reason = f"{item_keys.RULE_VERSION} completion={_ref(plan, operation)}"
        for target in result.invalidate:
            mem_id = target.get("id")
            if not isinstance(mem_id, str) or not mem_id:
                continue
            try:
                resp = await client.patch(_bank_path(bank, f"/memories/{quote(mem_id, safe='')}"),
                                          json={"state": "invalidated", "reason": reason},
                                          headers=_headers(upstream_key), timeout=PATCH_TIMEOUT_SECONDS)
                code: Any = resp.status_code
            except httpx.TimeoutException:
                code = "timeout"
            except (httpx.HTTPError, OSError):
                code = "transport"
            info["patch_status"][mem_id] = code
            if isinstance(code, int) and 200 <= code < 300:
                info["invalidated_ids"].append(mem_id)
        record("invalidated" if len(info["invalidated_ids"]) == len(result.invalidate) else "patch-failed")
    except (asyncio.CancelledError, KeyboardInterrupt):
        raise
    except Exception as exc:  # noqa: BLE001 - 어떤 실패도 retain 결과를 바꾸지 않는다
        record(f"failed:{type(exc).__name__}")
