"""S1 읽기 시점 항목 상태 원장 (issue #363, 아키텍처 v2 런 5 R5-B1, 기능 플래그 `HC_ITEM_LEDGER` 기본 꺼짐).

왜: X1(`item_status`, `HC_ITEM_STATUS`)은 완료 행 원문을 recall 결과 **끝**에 덧붙인다. 소비자 LLM 은 낡은 열림 행을
읽는 자리에서 그 항목이 끝났는지 못 보고, 토큰이 크게 는다(런 3 X1 벤치 토큰 +14.3%). S1 은 같은 키의 최신 완료
근거를 그 키를 말하는 **각 결과 행 안에** 짧은 주석으로 단다(원장 출처는 뱅크 안의 완료 기억뿐, GitHub 는 입력이 아니다).

무엇을(`HC_ITEM_LEDGER=1` 일 때만, X1 과 독립으로 켜진다): 게이트웨이 MCP `tools/call` `recall` 의 **성공 응답**에서
  1. 결과 각 행(X1 이 덧붙인 `gateway_item_status` 행은 빼고)에서 `item_keys.extract_keys` 로 작업 키를 뽑는다.
     결과 순서로 먼저 나온 키부터 손잡이 `max_keys` 개. 조회 범위(project 태그)는 손잡이 `scope` 로 정하고, 범위가 빈 행은
     키를 내지 않는다(조회도 안 한다).
  2. 키마다 `item_invalidate._lookup_list` 로 후보를 받는다(X1 과 같은 `item_status._lookup_all`, 전체 벽시계 상한 하나).
  3. 후보 중 같은 키를 가진 완료 계열(`item_status.is_completion_family`, 뱅크 `tag_aliases` 반영), state=valid, 조회 범위
     project 태그가 겹치는 것만. 요청에 `query_timestamp` 가 있으면 `mentioned_at` 이 그 시각 이하인 것만(시각 없음 제외,
     요청 시각을 읽을 수 없으면 그 recall 은 건드리지 않는다). 키당 `mentioned_at` 최신 1개가 그 키의 완료 근거다
     (이미 결과에 든 행이어도 된다. X1 과 다르다).
  4. 그 키를 말하는 결과 행마다(근거 행 자신은 빼고) 본문(`text`, 없으면 `content`)에 주석을 붙이고 구조 필드
     `gateway_item_ledger` = `[["<repo>#<n>", "done", "YYYY-MM-DD"(KST) 또는 null, "<근거 id>"], ...]`(키마다 4칸 목록
     [키, 상태, 완료 날짜, 근거 id], 키 순서)를 단다. 객체 대신 목록인 것은 행마다 붙는 글자 수를 줄이려는 것이다. 구조 필드는 손잡이와 무관하게 늘 붙는다(R5-A2 지표는 이 필드로만
     S1 주석을 인식한다). payload 는 `structuredContent.result` 와 `content[].text` 에 같은 문자열로 2벌 실려 있어 양쪽을
     함께 바꾼다(`item_status.rewrite_tool_result`).
  5. 모드가 강등이면 낡은 열림 행을 뒤로(X1 행 앞, X1 행 위치 불변), 제외면 뺀다. 낡은 열림 행 = 완료 계열이 아니고
     행이 말하는 키가 **모두** 시각 규칙상 행보다 새 완료 근거를 가진 행(아직 열린 키를 함께 말하는 행은 주석만).
     완료 계열 행은 어떤 모드에서도 옮기거나 빼지 않는다.
  6. 실패(시간 초과, upstream 오류, 예외)는 X1 처럼 fail-open: 원래 바이트 그대로(부분 주석 없음). 감사 줄(decision
     `item-ledger`)에 사유 한 줄: `annotated` / `no-evidence` / `timeout` / `lookup-failed:<code>` / `failed:<예외 이름>`.
     조회할 키가 없으면 줄도 없다. 본문은 남기지 않는다.

처리 순서(app.py): upstream 응답 전체를 읽은 직후 X1(`item_status`) -> **S1(이 모듈)** -> honesty/prune/label/unescape
합성 패스. S1 은 X1 출력을 입력으로 받지만 X1 이 덧붙인 행은 읽기만 한다(키를 뽑지 않고, 주석·이동·제외도 없다).
도달 범위는 X1 과 같다: MCP recall 만(REST recall·reflect·지식 페이지 밖), 키 있는 항목만.

손잡이 전달(R5-C 튜닝 루프와 R5-D 가 그대로 쓴다)
----------------------------------------------------
* 우선순위: `HC_ITEM_LEDGER_KNOBS`(env 인라인 JSON 객체, 공백뿐이면 없음) > `HC_ITEM_LEDGER_CONFIG` 파일 > 기본값.
  env 가 있으면 파일은 읽지 않는다(병합 없음).
* 파일 기본 경로 `/app/policies/item-ledger.json`: 게이트웨이 컨테이너의 기존 정책 바인드 마운트
  (`./policies:/app/policies:ro`, hc-next 사본은 상태 디렉터리의 `policies/`) 안이다. 리포·운영에는 이 파일을 두지 않는다
  (파일 없음 = 기본 손잡이, 경고 없음).
* 재빌드·재시작 없는 반영: recall 마다 `os.stat` 으로 (경로, mtime_ns, 크기, ctime_ns, inode)를 보고 바뀌었을 때만 다시 읽는다.
  호스트 쪽 디렉터리 바인드라 호스트에서 파일을 고치면(새 파일로 바꿔 넣어도) 다음 recall 부터 적용된다.
  env 값은 컨테이너 재생성 때만 바뀐다(R5-D 벤치 복제본은 `gw-up --env HC_ITEM_LEDGER_KNOBS=<JSON>`).
* 잘못된 설정: JSON 이 아니거나 객체가 아니면 전부 기본값, 키 하나가 허용값 밖이면 그 키만 기본값, 모르는 키는 무시.
  어느 경우든 경고 코드(키 이름 또는 `invalid-json`/`not-an-object`/`unreadable`/`unknown:<키>`)를 로그 경고 1회
  (같은 설정이면 다시 찍지 않는다)와 감사 줄 `knob_warnings` 에 남긴다. 설정 원문은 남기지 않는다.
* 감사 줄 `item_ledger` 에 실제 적용 값의 `knobs_sha256`(키 정렬·압축 JSON 의 sha256)과 `knobs_source`
  (`env`/`file`/`default`)가 실린다. 튜닝 원장은 이 값으로 어느 손잡이가 적용됐는지 대조한다.

손잡이 스키마(JSON 객체, 모든 키 선택)

| 키 | 기본값 | 허용값 | 뜻 |
|---|---|---|---|
| `mode` | `"annotate"` | `annotate` / `demote` / `drop` | 표시만 / 낡은 열림 행을 뒤로(X1 행 앞) / 낡은 열림 행 제외 |
| `note_template` | `"[{key} 끝남 {date}]"` | 1~200자, 자리표시 `{key}` `{date}` `{evidence_id}` 만(`{{` `}}` 는 중괄호) | 주석 문안. `{date}` 는 근거의 KST 날짜, 시각 없으면 `날짜 없음` |
| `note_position` | `"prefix"` | `prefix` / `suffix` | 본문 앞 / 뒤(공백 하나로 잇는다. 키가 여럿이면 주석을 공백으로 이어 한 덩어리) |
| `max_keys` | `8` | 정수 1~64 | 한 응답에서 조회할 키 상한(결과 순서로 먼저 나온 키부터) |
| `scope` | `"row_or_request"` | `row_or_request` / `row` / `request` | 조회 범위 project 태그: 행의 태그, 없으면 요청 `tags`(X1 과 같음) / 행만 / 요청만. **조회만** 제한한다: 찾은 근거의 주석은 키((repo, n)) 단위로 그 키를 말하는 모든 행(범위가 없거나 다른 project 행 포함)에 단다 |
| `time_rule` | `"strict"` | `strict` / `lenient` | 낡은 행 판정. strict = 행 시각 < 근거 시각(둘 다 있을 때만). lenient = 행 시각 <= 근거 시각, 또는 어느 쪽이든 시각 없음. 어느 규칙이든 `query_timestamp` 뒤 근거는 쓰지 않는다 |
| `use_alias_table` | `true` | `true` / `false` | 뱅크 `item_repo_aliases` 를 키 추출·조회에 쓸지(`tag_aliases` 완료 판정은 늘 쓴다) |
| `x1_combo` | `"always"` | `always` / `skip_if_x1` | X1 이 같은 응답에 그 키의 완료 행을 덧붙였으면 S1 주석을 생략할지. S1 출력만 바꾸고 X1 행은 건드리지 않는다. X1 행의 키는 뱅크 별칭 정규화로 뽑는다(`use_alias_table` 과 무관) |
| `timeout_ms` | `1500` | 정수 100~10000 | S1 조회 전체 벽시계 상한(X1 의 `HC_ITEM_STATUS_TIMEOUT_MS` 와 별개) |

한계(final-review #363-1, 고치지 않고 기록):
  * `skip_if_x1` 은 X1 이 덧붙인 행 본문·태그에서 뽑은 키를 **전부** 생략한다. X1 행에는 어느 키 때문에 덧붙였는지 알리는
    구조 필드가 없어(`gateway_item_status: true` 뿐) 본문에 후속으로만 나온 다른 키(예: "#5 완료, 후속은 #9" 의 #9)도 생략된다.
  * X1 과 S1 을 함께 켜면 같은 키를 각자 조회한다: upstream list 요청이 2배이고, 두 벽시계 상한이 차례로 더해진다
    (최악 `HC_ITEM_STATUS_TIMEOUT_MS` + S1 `timeout_ms`).
  * 기본 위치 `prefix` 는 기억 본문 머리의 `[라벨]` 앞에 주석을 놓는다. 클라이언트가 recall 본문을 update_memory·retain 으로
    되쓰면 주석이 뱅크에 영구히 남는다. 운영 적용 때 위치 기본값을 다시 본다.

설계: 판정은 규칙뿐이다(LLM·임베딩 없음). 키 추출·완료 판정·후보 조회는 item_keys·item_status·item_invalidate 를 그대로
쓴다. 정답표·라벨셋은 입력이 아니다.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import string
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Set, Tuple

import httpx

from . import item_invalidate, item_keys, item_status, reframe
from .audit import Audit
from .retain_gates import _audit

ENV_FLAG = "HC_ITEM_LEDGER"
ENV_CONFIG = "HC_ITEM_LEDGER_CONFIG"
ENV_KNOBS = "HC_ITEM_LEDGER_KNOBS"
DEFAULT_CONFIG_PATH = "/app/policies/item-ledger.json"
DECISION = "item-ledger"
FIELD = "gateway_item_ledger"
STATUS_DONE = "done"
NO_DATE = "날짜 없음"
_KST = timezone(timedelta(hours=9))
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)
_TEMPLATE_FIELDS = frozenset({"key", "date", "evidence_id"})

log = logging.getLogger(__name__)

DEFAULTS: Dict[str, Any] = {
    "mode": "annotate",
    "note_template": "[{key} 끝남 {date}]",
    "note_position": "prefix",
    "max_keys": 8,
    "scope": "row_or_request",
    "time_rule": "strict",
    "use_alias_table": True,
    "x1_combo": "always",
    "timeout_ms": 1500,
}


def enabled() -> bool:
    """기본 OFF. 1/true/yes/on 만 켠다."""
    return os.environ.get(ENV_FLAG, "0").strip().lower() in {"1", "true", "yes", "on"}


# --- 손잡이 --------------------------------------------------------------------------------------

def _one_of(*allowed: Any) -> Callable[[Any], bool]:
    return lambda v: isinstance(v, str) and v in allowed


def _int_between(lo: int, hi: int) -> Callable[[Any], bool]:
    return lambda v: isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def _valid_template(value: Any) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 200:
        return False
    try:
        for _, name, spec, conv in string.Formatter().parse(value):
            if name is not None and (name not in _TEMPLATE_FIELDS or spec or conv):
                return False
        value.format(key="k", date="d", evidence_id="e")
    except (ValueError, KeyError, IndexError):
        return False
    return True


_VALIDATORS: Dict[str, Callable[[Any], bool]] = {
    "mode": _one_of("annotate", "demote", "drop"),
    "note_template": _valid_template,
    "note_position": _one_of("prefix", "suffix"),
    "max_keys": _int_between(1, 64),
    "scope": _one_of("row_or_request", "row", "request"),
    "time_rule": _one_of("strict", "lenient"),
    "use_alias_table": lambda v: isinstance(v, bool),
    "x1_combo": _one_of("always", "skip_if_x1"),
    "timeout_ms": _int_between(100, 10000),
}


@dataclass(frozen=True)
class Knobs:
    values: Dict[str, Any]
    source: str                                   # env / file / default
    warnings: List[str] = field(default_factory=list)

    @property
    def sha256(self) -> str:
        canon = json.dumps(self.values, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _warn_code_for_unknown(key: Any) -> str:
    name = "".join(ch for ch in str(key) if ch.isalnum() or ch in "_-")[:40]
    return f"unknown:{name}"


def parse_knobs(raw: str) -> Tuple[Dict[str, Any], List[str]]:
    """설정 원문 -> (적용 값, 경고 코드). 잘못된 키만 기본값, 전체가 잘못되면 전부 기본값."""
    try:
        data = json.loads(raw)
    except ValueError:
        return dict(DEFAULTS), ["invalid-json"]
    if not isinstance(data, dict):
        return dict(DEFAULTS), ["not-an-object"]
    values = dict(DEFAULTS)
    warnings: List[str] = []
    for key, value in data.items():
        check = _VALIDATORS.get(key)
        if check is None:
            warnings.append(_warn_code_for_unknown(key))
        elif check(value):
            values[key] = value
        else:
            warnings.append(key)
    return values, warnings


_CACHE: Optional[Tuple[Tuple[Any, ...], Knobs]] = None


def _reset_cache() -> None:
    global _CACHE
    _CACHE = None


def _remember(cache_key: Tuple[Any, ...], build: Callable[[], Knobs]) -> Knobs:
    global _CACHE
    if _CACHE is not None and _CACHE[0] == cache_key:
        return _CACHE[1]
    knobs = build()
    if knobs.warnings:                                       # 같은 설정이면 다시 찍지 않는다(캐시 적중은 여기 안 온다)
        log.warning("%s 손잡이 설정(%s) 경고 %s: 해당 키는 기본값으로 둔다", ENV_FLAG, knobs.source,
                    ",".join(knobs.warnings))
    _CACHE = (cache_key, knobs)
    return knobs


def load_knobs() -> Knobs:
    """적용할 손잡이. env 인라인 JSON > 설정 파일(mtime 바뀔 때만 다시 읽음) > 기본값."""
    inline = os.environ.get(ENV_KNOBS, "")
    if inline.strip():
        def from_env() -> Knobs:
            values, warnings = parse_knobs(inline)
            return Knobs(values, "env", warnings)
        return _remember(("env", inline), from_env)
    path = os.environ.get(ENV_CONFIG, "").strip() or DEFAULT_CONFIG_PATH
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return _remember(("default", path), lambda: Knobs(dict(DEFAULTS), "default"))
    except OSError:
        return _remember(("unreadable", path), lambda: Knobs(dict(DEFAULTS), "default", ["unreadable"]))

    def from_file() -> Knobs:
        try:
            with open(path, encoding="utf-8") as fh:
                raw = fh.read()
        except (OSError, UnicodeDecodeError):
            return Knobs(dict(DEFAULTS), "default", ["unreadable"])
        values, warnings = parse_knobs(raw)
        return Knobs(values, "file", warnings)
    # ctime·inode 도 키에 넣는다: 권한만 고친 경우(chmod, mtime·크기 그대로)나 새 파일로 바꿔 넣은 경우에도 다시 읽는다
    return _remember(("file", path, st.st_mtime_ns, st.st_size, st.st_ctime_ns, st.st_ino), from_file)


# --- 순수 규칙 ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Evidence:
    key: item_keys.ItemKey
    memory_id: str
    at: Optional[datetime]

    def record(self) -> List[Any]:
        """[키, 상태, 완료 날짜(KST) 또는 None, 근거 id]."""
        return [item_status._label(self.key), STATUS_DONE,
                None if self.at is None else self.at.astimezone(_KST).date().isoformat(), self.memory_id]


def _row_scope(row_tags: List[str], request_projects: Tuple[str, ...], scope: str) -> Tuple[str, ...]:
    row_projects = item_status._projects(row_tags)
    if scope == "row":
        return row_projects
    if scope == "request":
        return request_projects
    return row_projects or request_projects


def _is_x1_row(row: Any) -> bool:
    return isinstance(row, Mapping) and row.get(item_status.MARK_KEY) is True


def _row_keys(row: Mapping[str, Any], aliases: Mapping[str, str]) -> Set[item_keys.ItemKey]:
    return set(item_keys.extract_keys(item_status._text(row), item_status._tags(row), aliases))


def plan_element(rows: List[Any], request_projects: Tuple[str, ...], aliases: Mapping[str, str], knobs: Mapping[str, Any],
                 x1_aliases: Optional[Mapping[str, str]] = None
                 ) -> Tuple[List[item_keys.ItemKey], Dict[item_keys.ItemKey, Set[str]], Set[item_keys.ItemKey]]:
    """요소 하나 -> (키 등장 순서, {키: 조회 범위}, X1 조합으로 주석을 생략할 키).

    X1 행의 키는 X1 과 같은 뱅크 별칭 정규화(`x1_aliases`)로 뽑는다(`use_alias_table` 과 무관, #363-1)."""
    skip: Set[item_keys.ItemKey] = set()
    if knobs["x1_combo"] == "skip_if_x1":
        for row in rows:
            if _is_x1_row(row):
                skip |= _row_keys(row, aliases if x1_aliases is None else x1_aliases)
    order: List[item_keys.ItemKey] = []
    scopes: Dict[item_keys.ItemKey, Set[str]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or _is_x1_row(row):
            continue
        scope = _row_scope(item_status._tags(row), request_projects, knobs["scope"])
        if not scope:
            continue
        for key in sorted(_row_keys(row, aliases) - skip):
            scopes.setdefault(key, set()).update(scope)
            if key not in order:
                order.append(key)
    return order, scopes, skip


def select_evidence(key: item_keys.ItemKey, scope: Set[str], candidates: List[Any], cutoff: Optional[datetime],
                    aliases: Mapping[str, str], tag_aliases: Mapping[str, str]) -> Optional[Evidence]:
    """키의 최신 완료 근거(`query_timestamp` 이하). 없으면 None."""
    best: Optional[Tuple[datetime, str, Optional[datetime]]] = None
    for cand in candidates:
        if not isinstance(cand, Mapping) or not isinstance(cand.get("id"), str) or not cand["id"]:
            continue
        state = cand.get("state")
        if isinstance(state, str) and state != "valid":
            continue
        tags = item_status._tags(cand)
        if not item_status.is_completion_family(cand, tag_aliases) or not scope & set(item_status._projects(tags)):
            continue
        if key not in item_keys.extract_keys(item_status._text(cand), tags, aliases):
            continue
        at = item_status._instant(cand.get("mentioned_at"))
        if cutoff is not None and (at is None or at > cutoff):
            continue
        rank = (at or _EPOCH, cand["id"], at)
        if best is None or rank[:2] > best[:2]:
            best = rank
    return None if best is None else Evidence(key, best[1], best[2])


def _stale(row: Mapping[str, Any], row_keys: Set[item_keys.ItemKey], evidence: Mapping[item_keys.ItemKey, Evidence],
           tag_aliases: Mapping[str, str], time_rule: str) -> bool:
    """낡은 열림 행 = 완료 계열이 아니고, 행이 말하는 키가 **모두** 시각 규칙상 행보다 새 완료 근거를 가진 행.
    아직 열린 키(근거 없음·조회 안 됨)를 함께 말하는 행은 강등·제외하지 않는다(열린 기록을 지우면 G2 가 깨진다, #363-1)."""
    if not row_keys or item_status.is_completion_family(row, tag_aliases):
        return False
    row_at = item_status._instant(row.get("mentioned_at"))
    for key in row_keys:
        ev = evidence.get(key)
        if ev is None:
            return False
        if time_rule == "lenient":
            if not (row_at is None or ev.at is None or row_at <= ev.at):
                return False
        elif not (row_at is not None and ev.at is not None and row_at < ev.at):
            return False
    return True


def _annotated_row(row: Mapping[str, Any], evidence: List[Evidence], knobs: Mapping[str, Any]) -> Dict[str, Any]:
    records = [ev.record() for ev in evidence]
    notes = " ".join(knobs["note_template"].format(key=key, date=date or NO_DATE, evidence_id=ev_id)
                     for key, _, date, ev_id in records)
    out = dict(row)
    for text_key in ("text", "content"):
        if isinstance(row.get(text_key), str):
            body = row[text_key]
            out[text_key] = f"{notes} {body}" if knobs["note_position"] == "prefix" else f"{body} {notes}"
            break
    out[FIELD] = records
    return out


def _annotate_payload(payload: Any, evidence: Mapping[item_keys.ItemKey, Evidence], aliases: Mapping[str, str],
                      tag_aliases: Mapping[str, str], knobs: Mapping[str, Any],
                      stats: Optional[Dict[str, int]] = None) -> Optional[Dict[str, Any]]:
    """recall payload 하나에 주석·강등·제외를 적용한 새 payload. 바뀐 것이 없으면 None."""
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list) or not evidence:
        return None
    rows = payload["results"]
    slots: List[int] = []                 # X1 행이 아닌 원래 행의 자리(강등은 이 자리 안에서만 순서를 바꾼다)
    keep: List[Any] = []
    stale: List[Any] = []
    annotated = 0
    for i, row in enumerate(rows):
        if not isinstance(row, Mapping) or _is_x1_row(row):
            continue
        slots.append(i)
        row_keys = _row_keys(row, aliases)
        mine = sorted(k for k in row_keys if k in evidence and evidence[k].memory_id != row.get("id"))
        if not mine:
            keep.append(row)
            continue
        evs = [evidence[k] for k in mine]
        new_row = _annotated_row(row, evs, knobs)
        annotated += 1
        if knobs["mode"] != "annotate" and _stale(row, row_keys, evidence, tag_aliases, knobs["time_rule"]):
            stale.append(new_row)
        else:
            keep.append(new_row)
    if not annotated:
        return None
    mode = knobs["mode"]
    ordered = keep if mode == "drop" else keep + stale      # 제외는 뒤쪽 자리를 비운다(X1 행 내용은 그대로)
    filled = dict(zip(slots, ordered))
    slot_set = set(slots)
    new_rows = [filled[i] if i in slot_set else row for i, row in enumerate(rows) if i not in slot_set or i in filled]
    if stats is not None:
        stats["annotated"] = stats.get("annotated", 0) + annotated
        stats["demoted"] = stats.get("demoted", 0) + (len(stale) if mode == "demote" else 0)
        stats["dropped"] = stats.get("dropped", 0) + (len(stale) if mode == "drop" else 0)
    return {**payload, "results": new_rows}


# --- 응답 읽기·쓰기(비동기) ------------------------------------------------------------------------

async def annotate_mcp_body(*, media: Optional[str], body: bytes, recall_calls: Mapping[Any, Any],
                            client: httpx.AsyncClient, upstream_key: str, bank: str, bank_policy: Any,
                            audit: Audit, token: Optional[str], identity: Optional[str]) -> bytes:
    """MCP 응답 바이트(SSE 또는 JSON)에서 recall 성공 결과에 S1 주석을 단 새 바이트. 어떤 실패에도 원본 바이트."""
    started = time.monotonic()
    info: Dict[str, Any] = {"transport": "mcp", "rule_version": item_keys.RULE_VERSION, "keys": [], "keys_count": 0,
                            "candidates": 0, "annotated": 0, "demoted": 0, "dropped": 0, "evidence_ids": []}

    def record(reason: str) -> None:
        info["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        _audit(audit, token=token, identity=identity, bank=bank, tool="recall", decision=DECISION,
               reason=reason, extra={"item_ledger": info})

    try:
        knobs_obj = load_knobs()
        knobs = knobs_obj.values
        info.update({"knobs_source": knobs_obj.source, "knobs_sha256": knobs_obj.sha256, "mode": knobs["mode"],
                     "max_keys": knobs["max_keys"], "timeout_ms": knobs["timeout_ms"]})
        if knobs_obj.warnings:
            info["knob_warnings"] = list(knobs_obj.warnings)
        parsed = reframe.parse_response(media, body)
        if parsed.failure is not None or not parsed.frames:
            return body
        bank_aliases = getattr(bank_policy, "item_repo_aliases", None) or {}
        aliases = bank_aliases if knobs["use_alias_table"] else {}
        tag_aliases = getattr(bank_policy, "tag_aliases", None) or {}
        rows_by_el = item_status._recall_rows(parsed, set(recall_calls))
        cutoffs: Dict[Any, Optional[datetime]] = {}
        plans: Dict[Any, Tuple[List[item_keys.ItemKey], Dict[item_keys.ItemKey, Set[str]]]] = {}
        order: List[item_keys.ItemKey] = []
        for el_id, rows in rows_by_el.items():
            args = recall_calls.get(el_id)
            args = args if isinstance(args, Mapping) else {}
            raw_cutoff = args.get("query_timestamp")
            cutoff = item_status._instant(raw_cutoff) if raw_cutoff else None
            if raw_cutoff and cutoff is None:        # 기준 시각을 못 읽으면 그 뒤 근거를 못 거른다: 건드리지 않는다
                continue
            cutoffs[el_id] = cutoff
            tags = args.get("tags")
            request_projects = item_status._projects(
                [t.strip().lower() for t in tags if isinstance(t, str)] if isinstance(tags, list) else [])
            el_order, el_scopes, _ = plan_element(rows, request_projects, aliases, knobs, bank_aliases)
            plans[el_id] = (el_order, el_scopes)
            order.extend(k for k in el_order if k not in order)
        keys = order[:knobs["max_keys"]]
        if not keys:
            return body
        info["keys"] = [item_status._label(k) for k in keys[:item_invalidate.MAX_AUDIT_LIST]]
        info["keys_count"] = len(keys)
        union: Dict[item_keys.ItemKey, Set[str]] = {k: set() for k in keys}
        for _, el_scopes in plans.values():
            for key, scope in el_scopes.items():
                if key in union:
                    union[key] |= scope
        try:
            found = await item_status._lookup_all(client, upstream_key, bank, keys,
                                                  {k: tuple(sorted(v)) for k, v in union.items()}, aliases,
                                                  knobs["timeout_ms"] / 1000.0)
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception as exc:  # noqa: BLE001 - 시간 초과·upstream 오류: 원래 응답 그대로
            record(item_status._failure_reason(exc))
            return body
        info["candidates"] = sum(len(v) for v in found.values())
        evidence_by_el: Dict[Any, Dict[item_keys.ItemKey, Evidence]] = {}
        stats: Dict[str, int] = {}
        for el_id, (el_order, el_scopes) in plans.items():
            evidence: Dict[item_keys.ItemKey, Evidence] = {}
            for key in el_order:
                if key not in union:
                    continue
                ev = select_evidence(key, el_scopes[key], found.get(key, []), cutoffs[el_id], aliases, tag_aliases)
                if ev is not None:
                    evidence[key] = ev
            if evidence and _annotate_payload({"results": rows_by_el[el_id]}, evidence, aliases, tag_aliases,
                                              knobs, stats) is not None:
                evidence_by_el[el_id] = evidence
        if not evidence_by_el:
            record("no-evidence")
            return body
        applied: Set[Any] = set()

        def rewrite(el: dict) -> dict:
            evidence = evidence_by_el.get(reframe.element_id(el))
            if not evidence:
                return el
            new, changed = item_status.rewrite_tool_result(
                el.get("result"), lambda payload: _annotate_payload(payload, evidence, aliases, tag_aliases, knobs))
            if not changed:
                return el
            applied.add(reframe.element_id(el))
            return {**el, "result": new}

        rewritten = reframe.transform_results(parsed, rewrite)
        _, new_body, serialize_failure = reframe.try_serialize(rewritten)
        failure = rewritten.failure or serialize_failure
        if failure:
            record("failed:rewrite")
            return body
        info.update(stats)
        ev_ids = sorted({ev.memory_id for el_id in applied for ev in evidence_by_el[el_id].values()})
        info["evidence_ids"] = ev_ids[:item_invalidate.MAX_AUDIT_LIST]
        record("annotated" if applied else "no-evidence")
        return new_body if applied else body
    except (asyncio.CancelledError, KeyboardInterrupt):
        raise
    except Exception as exc:  # noqa: BLE001 - 주석 실패가 recall 실패가 되면 안 된다
        record(f"failed:{type(exc).__name__}")
        return body
