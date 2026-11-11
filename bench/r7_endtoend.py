"""런 7 단계 0 원래 증상 새 채점식·항목 분해·저장소 준비·수정 전후 렌더 비교(R7-3a, issue #403, 상위 #393).

  r7_endtoend.py score --gen-dir <dir> --recalls on=<dir> --recalls off=<dir> --answer-key <C> --answer-key <D>
                       --store <역할 저장소> --aliases <json> --github-cache <R7-0 캐시> --work <dir> [--offline]
  r7_endtoend.py add-github --pair <원본 저장소> <새 저장소> [--pair ...] --aliases <json> --owners <json>
                       --default-owner-file <txt> --work <dir>
  r7_endtoend.py render --store <근거 테이블 저장소> --recalls <dir> [--recalls ...] --plan <json> --symptom-file <txt>
                       --aliases <json> --github-cache <캐시> --before-module <py> --work <dir> [--label <이름>] [--offline]

  r7_endtoend.py score --gen <arm>=<gen 파일> ...   (단계 A: 팔 라벨과 d-llm 라벨이 다른 팔, 생성 머리 recall_sha256 대조)
  r7_endtoend.py knobs --github use|ignore --out <json>                 (단계 A 손잡이 파일, 정규 sha256)
  r7_endtoend.py audit --log <gateway.log> --windows <json> --aliases <json> --out <json>   (단계 A 감사 줄 판정)
  r7_endtoend.py store-check --store <sqlite> [--at <UTC>] --out <json>    (저장소 meta·근거 테이블·근거 나이)
  r7_endtoend.py g3-tables --run <bench run dir> [--base base --cand cand] --out <json>   (부착 질의 표 토큰)
  r7_endtoend.py dev-score --gen on=<dir> --gen off=<dir> --recalls on=<dir> --recalls off=<dir> --store <sqlite>
                       --aliases <json> --github-cache <R7-0 캐시> [--labelset <jsonl>] --work <dir>
                       (단계 B: dev t 마다 P_key(t 시점 GitHub)·R(t 시점 실제 열린 이슈 쌍, K 과반)·하위군, R 켬 - 끔 키 군집
                       재표집 95% 구간과 반폭 = R 비열화 최소 검출 차)

  운영 시점 연막(issue #408, 아래 "운영 시점 연막 실행기" 절):
  r7_endtoend.py smoke-recall --query <이름>=<문안 파일> ... --out-dir <dir> [--allow-prod --read-only] [--t0 <UTC>]
  r7_endtoend.py smoke-attach --recall <저장 recall> --store <역할 저장소> --aliases <json> --out <파일> [--knobs <json>]
  r7_endtoend.py smoke-dllm --recall <표 부착 recall> --t0 <UTC> --k 3 --gen-out <파일> --spent-dir <dir> --max-usd 1 --soft-usd 0.8
  r7_endtoend.py smoke --t0 <UTC> --gen <파일> --recall <표 부착 recall> [--table-recall <파일> ...] --store <sqlite>
                       --aliases <json> --github-owners <json> --github-default-owner <owner> --work <dir>
  r7_endtoend.py owners-check --store <sqlite> --owners <json> --default-owner <owner> --aliases <json> --out <json>
  r7_endtoend.py score ... --t0 <UTC>   (기본 10-28 SYMPTOM_TS: 기존 경로와 바이트 같은 요약)

  v2.1 드라이런(issue #412, 아래 "R7-6" 절): 렌더는 게이트웨이처럼 리포 최대 번호(`store_max_numbers`)를 넘긴다(over-max drop).
  r7_endtoend.py smoke ... --listing <본문 JSONL> <메타 JSONL>   (교차 충돌 재계산용 저장 나열, 필수)
  r7_endtoend.py table-gate --t0 <UTC> --recall <표 부착 recall> [--recall ...] --store <sqlite> --aliases <json>
                       --listing <본문> <메타> --github-owners <json> --github-default-owner <owner> --work <dir> [--label]
                       [--gh-timeout 60]   (결정적 검사: 표 안 over-max 줄·교차 충돌 종결 줄·표 정확도, LLM 0)
  표 정확도(issue #414, table-gate 만): 표마다 열림 후보 GitHub 연결 키 정밀도 < 95% / 확인된 종결 오류 > 0 / 미계획 오류 > 0
  이면 롤백, 값 없음·연결 열림 후보 0줄·표 줄 파싱 수 != counts 합은 실패. GitHub 호출은 `--gh-timeout` 초 시한(smoke·table-gate·owners-check, 넘기면 실패).
  관문(#412 재정의): 롤백(0 허용) = t0 GitHub 끝남 키를 열림으로 / 그 응답 표 반증 줄 키를 열림으로 / 표 안 over-max 줄 /
  교차 충돌 종결 줄 / 실행기 실패 조건. 공개만 = 미연결 키, 재오픈 미상, 없는 번호(최대 번호 이하·초과·모름), 키 없는 끝남,
  열린 PR 유래, residual 종결 키. exit 0 통과 / 4 롤백 / 1 실패(요약 gate.verdict 는 rollback 을 함께 낸다).

지출 0(LLM·d-llm 0), 사본 0, GitHub 는 GET 만. 원문(키·항목·응답·기억 본문)은 리포 밖 `--work`(0700/0600)에만 쓰고,
표준 출력과 요약에는 수치·sha256·분류 코드만 낸다.

새 원래 증상 정밀도(`score_items`, 상위 #393 "오프라인 단계 뒤 결정", 코디네이터 결정 1·2):
  분모 = 출력한 열린 항목 전체(status open). 정답표 대응 없음·PR·없는 번호·번호(키) 없는 일도 분모에 넣는다.
  미확인 항목을 정답으로 치거나 분모에서 빼지 않는다. 항목마다 판정(verdict) 하나:
    * 키 있음 + GitHub 연결(평가 캐시 리포 200): t 시점 범주(`r7_eval.item_at`, 재오픈은 리포 단위 events).
      open_issue = open(분자), reopened_unknown = hold(판정 보류), FINISHED 범주 = finished, 그 밖(열린 PR·없는 번호·
      t 뒤 생성) = wrong.
    * 키 있음 + GitHub 없음(리포 404·캐시에 없음): 뱅크 규칙. 역할 저장소의 t 시점 키 상태(열림 = open, 끝남 = finished),
      저장소에 상태가 없으면 아래 근거 규칙.
    * 키 없음: 근거 규칙(`evidence_verdict`). 항목의 evidence_ids 기억(그 팔 recall 결과 행, t 이하) 중 항목 quote 를 담은
      문장의 규칙 역할(`item_roles.sentence_role`, 열림 표지어 우선), quote 문장이 없으면 kind:completion 기억은 끝남.
      시각이 가장 늦은 열림·끝남 신호가 판정이다. 신호가 없으면 hold. LLM 판정은 인터페이스(`judge=`)만 두고 이 티켓은
      부르지 않는다(코디네이터 결정 1).
  P_all = open / 열린 항목, P_all_github = GitHub open_issue / 열린 항목, P_key = 키 있는 항목 중 open / 키 있는 항목
  (GitHub 없는 키는 뱅크 규칙 open 을 분자에), P_key_linked = open_issue / GitHub 연결 키 항목. 키 없는 항목 수·그중 끝남·
  열림·보류 수를 따로 낸다.
  표 밖 끝남 키 유입(코디네이터 결정 2): 열린 항목 중 키가 있고 t 시점 GitHub FINISHED 범주이며, 그 팔 응답의 표에 그 키의
  `확인된 종결`·`종결(미계획)` 줄이 없던 항목. 반복당 평균. 보조로 뱅크 기준(표 밖 + 저장소 끝남) 수도 낸다.
  항목 분해: (키 유무, 표 줄 상태, 표 밖이면 그 키의 뱅크 상태, GitHub 범주) 반복별·합계.
  옛 채점(`r5_endtoend.symptom_score`)을 같은 입력으로 나란히 낸다.

저장소 준비(`add-github`, 코디네이터 결정 C(2)): 원본 저장소를 새 경로로 복사하고 #395 수집 함수
  (`item_roles_sidecar.collect_github`, GET)로 근거 테이블만 더한다. 기억·언급 행은 원본과 같아야 한다(복사 전후
  memories·mention_roles 행 수와 행 sha256 을 낸다).

0-3 렌더 비교(`render`): 저장 recall 응답마다 게이트웨이와 같은 순서(`wants_table` -> `table_scope` ->
  `github_read_scope` -> `read_store` -> `rescope_for_key_rules` -> `build_table`)로 수정 전(`--before-module`, 리포 밖
  임시 파일) · 수정 후(지금 `gateway.item_open_table`, #402 `result_keys`·읽는 시각을 넘김) 표를 만들고, 결과 행에 보이는
  키(수정 후 게이트웨이 `result_keys` 규칙) 중 t 시점 GitHub 끝난 키인데 표에 반증 줄(`확인된 종결`·`종결(미계획)`)이 없는
  키 수를 센다. 손잡이는 기본값 + `github_max_age_hours` 0(저장 근거가 오래돼 기본 2시간이면 전부 stale 로 덮인다,
  코디네이터 결정). 수정 전 모듈은 이 손잡이를 모른다(무시).

봉인(상위 #393 제약): 입력 경로 이름에 `sealed` 가 있으면 exit 2. 기존 bench 모듈은 import 만 한다.
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import importlib.util
import inspect
import json
import os
import re
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from bench import d_llm_match as M  # noqa: E402
from bench import r5_endtoend as E  # noqa: E402
from bench import r7_eval as V  # noqa: E402
from bench.r5_labels import parse_instant  # noqa: E402
from gateway import item_keys as IK  # noqa: E402
from gateway import item_open_table as OT  # noqa: E402
from gateway import item_roles as IR  # noqa: E402

SYMPTOM_TS = E.SYMPTOM_TS
OPEN, FINISHED, WRONG, HOLD, UNCONFIRMED = "open", "finished", "wrong", "hold", "unconfirmed"
VERDICTS = (OPEN, FINISHED, WRONG, HOLD, UNCONFIRMED)
RULE_VERDICTS = (OPEN, FINISHED, HOLD)      # 미연결 키 보조 규칙 판정(분자 아님)
CACHE_SCORE, CACHE_RENDER = "r7-s0-score-github.json", "r7-s0-render-github.json"   # 명령별 확장 캐시(rework #403-1)
_BARE_RE = re.compile(r"^\s*#\s*[0-9]+\s*$")
NO_KEY = "no_key"
NO_TABLE, OUT_OF_TABLE = "no-table", "out"
REFUTE_STATES = frozenset({OT.ROW_CLOSED, OT.ROW_NOT_PLANNED})
_STATE_ORDER = (OT.ROW_CLOSED, OT.ROW_NOT_PLANNED, OT.ROW_UNKNOWN, OT.ROW_CANDIDATE, OT.STATE_OPEN, OT.STATE_DONE)
_ROW_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*#[1-9][0-9]*$")
MIN_QUOTE_SENT = 8          # 문장이 quote 안에 들어 있다고 볼 최소 길이(짧은 조각 오탐 방지)


class Fail(Exception):
    pass


# --- 키·표 --------------------------------------------------------------------------------------------

def item_key(raw: Any, aliases: Mapping[str, str]) -> Optional[str]:
    """d-llm 항목 key -> 정규 `<repo>#<n>`(별칭 표 -> slug, 저장소·표 키와 같은 정규화). 모양이 아니면 None."""
    if not isinstance(raw, str):
        return None
    m = M.KEY_RE.match(raw)
    if not m:
        return None
    repo = IK._canonical(m.group(1).rstrip("/").split("/")[-1], IK._table(aliases))
    n = int(m.group(2))
    if not repo or n < 1:
        return None
    return "%s#%d" % (repo, n)


def table_rows(tab: Optional[Mapping[str, Any]]) -> Dict[str, str]:
    """payload 표 객체 -> {키: 줄 상태}. `text` 의 줄(`키 | 상태 ...`)을 읽고, `keys` 는 열림(3상태면 열림 후보)으로 더한다."""
    out: Dict[str, str] = {}
    if not isinstance(tab, Mapping):
        return out
    known = set(OT.ROW_STATES) | {OT.STATE_OPEN, OT.STATE_DONE}
    for line in str(tab.get("text") or "").splitlines():
        cells = [c.strip() for c in line.split(" | ")]
        if len(cells) >= 2 and _ROW_KEY_RE.match(cells[0]) and cells[1] in known:
            out[cells[0]] = cells[1]
    open_state = OT.ROW_CANDIDATE if "counts" in tab else OT.STATE_OPEN
    for k in tab.get("keys") or []:
        out.setdefault(str(k), open_state)
    return out


def response_rows(doc: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Set[str]]]:
    """recall 저장 하나(한 t)의 표 줄 합집합 {키: 상태 집합}. 표가 하나도 없으면 None(표 없음)."""
    if not doc:
        return None
    seen = False
    out: Dict[str, Set[str]] = defaultdict(set)
    for r in doc.get("runs") or []:
        tab = r.get("open_table")
        if not tab:
            continue
        seen = True
        for k, s in table_rows(tab).items():
            out[k].add(s)
    return dict(out) if seen else None


def row_state(states: Optional[Set[str]]) -> str:
    for s in _STATE_ORDER:
        if states and s in states:
            return s
    return sorted(states)[0] if states else OUT_OF_TABLE


# --- 근거 규칙(키 없는 항목·GitHub 없는 키) -------------------------------------------------------------

def _ws(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def memory_signal(mem: Mapping[str, Any], quote: Any) -> str:
    """기억 하나가 항목에 주는 규칙 신호: quote 를 담은(또는 quote 안에 든) 문장들의 규칙 역할 병합. quote 문장이 없으면
    kind:completion 기억은 done, 아니면 ref(신호 없음)."""
    text = str(mem.get("text") or "")
    label = IR.label_of(text)
    q = _ws(quote)
    sents = IR.split_sentences(text)
    hits = []
    for i, s in enumerate(sents):
        w = _ws(s)
        if q and w and (q in w or (len(w) >= MIN_QUOTE_SENT and w in q)):
            hits.append(IR.sentence_role(s, label, i == 0 and label is not None))
    if hits:
        return IR.merge_roles(hits)
    return IR.DONE if IK.is_completion(mem) else IR.REF


def evidence_verdict(item: Mapping[str, Any], mems: Mapping[str, Mapping[str, Any]], t: datetime,
                     judge: Optional[Callable[[Mapping[str, Any], Sequence[Mapping[str, Any]]], Optional[str]]] = None
                     ) -> Tuple[str, str]:
    """키 없는 항목(또는 저장소 상태 없는 GitHub 없는 키)의 규칙 판정 -> (verdict, 사유 코드).
    t 이하 근거 기억 중 시각이 가장 늦은 열림·끝남 신호. judge(LLM 판정 인터페이스)는 규칙이 못 가린 항목에만 부른다
    (이 티켓은 넘기지 않는다: 보류로 남는다)."""
    found, late = [], 0
    for mid in item.get("evidence_ids") or []:
        mem = mems.get(str(mid))
        if mem is None:
            continue
        at = parse_instant(mem.get("mentioned_at") or mem.get("occurred_start"))
        if at is None or at > t:
            late += 1
            continue
        found.append((at, str(mid), mem))
    if not found:
        reason = "after-t" if late else "no-evidence"
    else:
        found.sort(key=lambda x: (x[0], x[1]))
        for at, _, mem in reversed(found):
            sig = memory_signal(mem, item.get("quote"))
            if sig == IR.OPEN:
                return OPEN, "rule-open"
            if sig == IR.DONE:
                return FINISHED, "rule-done"
        reason = "no-signal"
    if judge is not None:
        got = judge(item, [m for _, _, m in found])
        if got in (OPEN, FINISHED):
            return got, "llm-" + got
    return HOLD, reason


# --- 항목 판정·반복 집계 -------------------------------------------------------------------------------

def classify_item(item: Mapping[str, Any], *, aliases: Mapping[str, str], gh: Mapping[str, Any], t: datetime,
                  bank: Mapping[str, Any], rows: Optional[Mapping[str, Set[str]]],
                  mems: Mapping[str, Mapping[str, Any]], judge=None) -> Dict[str, Any]:
    """열린 항목 하나 -> 판정과 분해 칸. bank = 저장소 t 시점 키 상태 {키: KeyState}. rows = 응답 표 줄(None = 표 없음)."""
    key = item_key(item.get("key"), aliases)
    rec: Dict[str, Any] = {"has_key": key is not None, "key": key}
    if key is None:
        verdict, why = evidence_verdict(item, mems, t, judge)
        rec.update(github=NO_KEY, linked=False, table="-", bank=None, rule=None,
                   bare_number=isinstance(item.get("key"), str) and bool(_BARE_RE.match(item["key"])),
                   verdict=verdict, source="evidence", reason=why, leak=False, bank_leak=False)
        return rec
    repo, n = V.repo_of(key), V.num_of(key)
    cat, _ = V.item_at(gh.get(repo), n, t)
    linked = V.linked(key, gh)
    st = bank.get(key)
    bank_state = st.state if st is not None else None
    tstate = NO_TABLE if rows is None else row_state(rows.get(key))
    rule = None
    if linked:
        source, why = "github", cat
        verdict = (OPEN if cat == "open_issue" else HOLD if cat == "reopened_unknown"
                   else FINISHED if cat in V.FINISHED else WRONG)
    else:
        # GitHub 미연결 키(리포 없음·조회 실패·캐시에 리포 없음, `PR #N` 처럼 리포가 아닌 이름 포함): t 시점 열린 이슈로
        # 확인할 수 없으므로 P_key 분모에만 넣는 미확인(rework #403-1). 뱅크·근거 규칙 판정은 보조 열(rule)로만 낸다.
        verdict = UNCONFIRMED
        if bank_state is not None:
            source, why = "bank", "bank-" + ("open" if bank_state == OT.STATE_OPEN else "done")
            rule = OPEN if bank_state == OT.STATE_OPEN else FINISHED
        else:
            source = "evidence"
            rule, why = evidence_verdict(item, mems, t, judge)
    refuted = rows is not None and bool(REFUTE_STATES & set(rows.get(key) or ()))
    rec.update(github=cat, linked=linked, table=tstate, rule=rule, bare_number=False,
               bank=(bank_state or "none") if tstate in (OUT_OF_TABLE, NO_TABLE) else None,
               verdict=verdict, source=source, reason=why,
               leak=cat in V.FINISHED and not refuted,
               bank_leak=tstate in (OUT_OF_TABLE, NO_TABLE) and bank_state == OT.STATE_DONE)
    return rec


def _ratio(a: int, b: int) -> Optional[float]:
    return a / b if b else None


def score_items(recs: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """한 반복의 열린 항목 판정 목록 -> 분자·분모와 정밀도."""
    c: Counter = Counter()
    for r in recs:
        c["open_items"] += 1
        c["v_" + r["verdict"]] += 1
        if r["has_key"]:
            c["keyed"] += 1
            c["keyed_" + r["verdict"]] += 1
            if r["linked"]:
                c["linked"] += 1
                c["linked_open"] += int(r["verdict"] == OPEN)
            else:
                c["unconfirmed"] += 1
                c["unconfirmed_rule_" + str(r.get("rule"))] += 1
            c["leak"] += int(r["leak"])
            c["bank_leak"] += int(r["bank_leak"])
        else:
            c["keyless"] += 1
            c["keyless_" + r["verdict"]] += 1
            c["keyless_bare"] += int(bool(r.get("bare_number")))
    out = {"open_items": c["open_items"], "verdicts": {v: c["v_" + v] for v in VERDICTS},
           "keyed": c["keyed"], "keyed_verdicts": {v: c["keyed_" + v] for v in VERDICTS},
           "linked": c["linked"], "linked_open": c["linked_open"],
           "unconfirmed": c["unconfirmed"],
           "unconfirmed_rule": {v: c["unconfirmed_rule_" + v] for v in RULE_VERDICTS},
           "keyless": c["keyless"], "keyless_verdicts": {v: c["keyless_" + v] for v in VERDICTS},
           "keyless_bare_number": c["keyless_bare"],
           "leak": c["leak"], "bank_leak": c["bank_leak"],
           "github_categories": dict(sorted(Counter(r["github"] for r in recs).items())),
           "P_all": _ratio(c["v_open"], c["open_items"]),
           "P_all_github": _ratio(c["linked_open"], c["open_items"]),
           "P_key": _ratio(c["linked_open"], c["keyed"]),          # 분자 = GitHub 로 확인된 열린 이슈만
           "P_key_linked": _ratio(c["linked_open"], c["linked"])}
    return out


def decomposition(recs: Iterable[Mapping[str, Any]]) -> Dict[str, int]:
    """(키 유무, 표 줄 상태, 뱅크 상태, GitHub 범주) -> 수. 키 문자열은 `key|table|bank|github` 꼴(코드만)."""
    c = Counter("%s|%s|%s|%s" % ("key" if r["has_key"] else "nokey", r["table"], r["bank"] or "-", r["github"])
                for r in recs)
    return dict(sorted(c.items()))


def _mean(xs: Sequence[Optional[float]]) -> Optional[float]:
    vs = [x for x in xs if x is not None]
    return sum(vs) / len(vs) if vs else None


def rows_for_reps(rows: Any, k: int) -> List[Optional[Mapping[str, Set[str]]]]:
    """응답 표(하나) 또는 응답 표 목록 -> 반복별 표. 목록이 1개면 모든 반복이 같은 응답(팔당 recall 1회 + 생성 K회,
    런 6·단계 A 설계), K개면 반복마다 그 응답, 그 밖은 어느 응답이 어느 반복의 입력인지 몰라 거부한다(rework #403-1)."""
    if not isinstance(rows, list):
        return [rows] * k
    if len(rows) == 1:
        return rows * k
    if len(rows) != k:
        raise Fail("saved recall responses (%d) match neither 1 nor the repetitions (%d)" % (len(rows), k))
    return list(rows)


def score_arm(gens: Sequence[Mapping[str, Any]], *, aliases: Mapping[str, str], gh: Mapping[str, Any], t: datetime,
              bank: Mapping[str, Any], rows: Any,
              mems: Mapping[str, Mapping[str, Any]], judge=None) -> Dict[str, Any]:
    """반복(K)마다 열린 항목 판정 -> 반복별 점수, 평균, 합계(풀링), 반복당 표 밖 끝남 키 유입 평균, 분해 표.
    rows = 응답 표 하나(None = 표 없음) 또는 응답 표 목록(`rows_for_reps`)."""
    per_rep = rows_for_reps(rows, len(gens))
    reps, all_recs, details = [], [], []
    for g, rows in zip(gens, per_rep):
        if not g.get("valid"):
            reps.append({"valid": False})
            details.append(None)
            continue
        recs = [classify_item(it, aliases=aliases, gh=gh, t=t, bank=bank, rows=rows, mems=mems, judge=judge)
                for it in g.get("items") or [] if it.get("status") == "open"]
        s = score_items(recs)
        reps.append(dict(s, valid=True, decomposition=decomposition(recs)))
        all_recs.extend(recs)
        details.append(recs)
    ok = [r for r in reps if r.get("valid")]
    pooled = score_items(all_recs)
    return {"K": len(gens), "invalid": len(gens) - len(ok), "reps": reps,
            "mean": {k: _mean([r[k] for r in ok]) for k in ("P_all", "P_all_github", "P_key", "P_key_linked")},
            "leak_per_rep": _mean([float(r["leak"]) for r in ok]),
            "bank_leak_per_rep": _mean([float(r["bank_leak"]) for r in ok]),
            "open_items_mean": _mean([float(r["open_items"]) for r in ok]),
            "keyless_per_rep": _mean([float(r["keyless"]) for r in ok]),
            "keyless_finished_per_rep": _mean([float(r["keyless_verdicts"][FINISHED]) for r in ok]),
            "unconfirmed_per_rep": _mean([float(r["unconfirmed"]) for r in ok]),
            "pooled": pooled, "decomposition": decomposition(all_recs), "_details": details}


def old_vs_new(gens: Sequence[Mapping[str, Any]], tasks: Sequence[Mapping[str, Any]], new: Mapping[str, Any]
               ) -> Dict[str, Any]:
    """옛 채점(`symptom_score`) 요지와 새 채점 요지를 나란히. 옛 '정밀도' = 1 - done_ratio(대응 없음은 오류로 안 셈)."""
    old = E.symptom_score(gens, tasks)
    ok = [r for r in old["reps"] if r.get("valid")]
    return {"old": {"done_ratio_mean": old["done_ratio_mean"],
                    "precision_like_mean": _mean([1 - r["done_ratio"] for r in ok if r["done_ratio"] is not None]),
                    "open_items_mean": old["open_items_mean"],
                    "unmapped_total": sum(r["unmapped"] for r in ok), "open_items_total": sum(r["open_items"] for r in ok),
                    "closed_total": sum(r["closed"] for r in ok), "controls_total": sum(r["controls"] for r in ok),
                    "partial_total": sum(r["partial"] for r in ok), "majority_open": old["majority_open"]},
            "new": {k: new[k] for k in ("mean", "leak_per_rep", "bank_leak_per_rep", "open_items_mean",
                                        "keyless_per_rep", "keyless_finished_per_rep", "unconfirmed_per_rep")},
            "new_pooled": {k: v for k, v in new["pooled"].items() if k != "github_categories"},
            "new_github_categories": new["pooled"]["github_categories"]}


# --- GitHub 캐시(R7-0 방식, GET 만) ---------------------------------------------------------------------

def load_cache(work: str, base: str, name: str) -> Tuple[Dict[str, Any], str]:
    """기준 캐시(`--github-cache`, 읽기만)와 확장 캐시(`--work/<name>`) -> (읽을 캐시 문서, 쓸 경로). rework #403-1 규칙:
    확장 캐시가 없으면 기준 캐시를 읽는다. 있으면 그 `base_sha256` 이 기준 캐시 파일 sha256 과 같고, 기준 캐시의 리포·
    리포 상태·항목 번호를 전부 담을 때(상위 집합)만 확장 캐시를 읽고, 아니면 거부한다(다른 기준에서 넓힌 캐시를 말없이
    쓰지 않는다). 쓸 때는 `base_sha256` 을 기준 캐시 sha256 으로 둔다."""
    out = os.path.join(work, name)
    base_sha = V.file_sha256(base)
    with open(base, encoding="utf-8") as fh:
        base_doc = json.load(fh)
    if not os.path.exists(out):
        return dict(base_doc, base_sha256=base_sha), out
    with open(out, encoding="utf-8") as fh:
        ext = json.load(fh)
    if ext.get("base_sha256") != base_sha:
        raise Fail("extended cache %s was not built from this --github-cache" % name)
    er = ext.get("repos") or {}
    for repo, rec in (base_doc.get("repos") or {}).items():
        got = er.get(repo)
        if not isinstance(got, dict) or got.get("status") != rec.get("status") or \
                not set((rec.get("items") or {})) <= set((got.get("items") or {})):
            raise Fail("extended cache %s is not a superset of --github-cache" % name)
    return ext, out


def extend_cache(gh_doc: Dict[str, Any], wanted: Mapping[str, Set[int]], offline: bool,
                 fetch=V.fetch_repo, fill=V.fill_gaps) -> int:
    """캐시에 없는 리포를 받고(소유자는 캐시 값, 없으면 DEFAULT_OWNER), 키 번호의 빈 이벤트·머지 시각을 채운다. 호출 수."""
    repos = gh_doc.setdefault("repos", {})
    before = sum(r.get("calls", 0) for r in repos.values())
    owners = {r: v.get("owner") for r, v in repos.items() if isinstance(v, dict) and v.get("owner")}
    for repo in sorted(wanted):
        if repo not in repos:
            if offline:
                raise Fail("offline run but a repo is missing from the cache")
            repos[repo] = fetch(owners.get(repo, V.DEFAULT_OWNER), repo)
        if not offline:
            fill(repos[repo], repo, set(wanted[repo]))
    after = sum(r.get("calls", 0) for r in repos.values())
    gh_doc["calls_total"] = after
    return after - before


# --- 입력 읽기 -----------------------------------------------------------------------------------------

def guard_paths(paths: Iterable[Optional[str]]) -> None:
    V.guard_paths(paths)


def read_recall_dir(d: str) -> Dict[str, Dict[str, Any]]:
    out = {}
    for n in sorted(os.listdir(d)):
        if n.startswith("t-") and n.endswith(".json"):
            with open(os.path.join(d, n), encoding="utf-8") as fh:
                doc = json.load(fh)
            out[doc["t"]] = doc
    return out


def recall_memories(doc: Optional[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    mems: Dict[str, Dict[str, Any]] = {}
    for r in (doc or {}).get("runs") or []:
        for row in r.get("results") or []:
            if isinstance(row, Mapping) and row.get("id"):
                mems.setdefault(str(row["id"]), dict(row))
    return mems


def _kv(text: str) -> Tuple[str, str]:
    if "=" not in text:
        raise argparse.ArgumentTypeError("expected <arm>=<path>")
    a, _, p = text.partition("=")
    return a, p


def _strip_private(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _strip_private(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_strip_private(x) for x in obj]
    return obj


# --- score ---------------------------------------------------------------------------------------------

def load_symptom_gens(recalls: Mapping[str, str], gen_dir: Optional[str], gen_files: Mapping[str, str],
                      day: str = E.SYMPTOM_DAY) -> Dict[str, Dict[str, Any]]:
    """팔마다 원래 증상 생성 파일. 기본은 `<gen_dir>/symptom-<팔>-<t>-gen.json`(머리 arm = 팔). 단계 A(#397): 팔 라벨과
    d-llm 라벨이 다를 때(켬-ignore 팔은 r5 dllm `--arm on` 으로 만든다) `--gen <팔>=<파일>` 로 직접 받고, 그 팔 recall
    파일 sha256 이 생성 머리 `recall_sha256` 과 같아야 한다(다른 팔 생성을 섞지 않게)."""
    from bench import r5_roles as X
    if set(gen_files) - set(recalls):
        raise Fail("--gen label has no --recalls")
    gens: Dict[str, Dict[str, Any]] = {}
    for arm in recalls:
        if arm in gen_files:
            g = X.read_json(gen_files[arm])
            rec = os.path.join(recalls[arm], "t-%s.json" % day)
            if g.get("stage") != "symptom" or not os.path.exists(rec) or g.get("recall_sha256") != V.file_sha256(rec):
                raise Fail("gen file %s is not the symptom generation of its recall" % arm)
        else:
            if not gen_dir:
                raise Fail("arm %s needs --gen-dir or --gen" % arm)
            g = X.read_json(os.path.join(gen_dir, "symptom-%s-%s-gen.json" % (arm, day)))
            if g.get("stage") != "symptom" or g.get("arm") != arm:
                raise Fail("gen file stage/arm mismatch")
        gens[arm] = g
    return gens


def cmd_score(args) -> int:
    from bench import r5_roles as X
    recalls = dict(args.recalls)
    guard_paths([args.gen_dir, args.store, args.aliases, args.github_cache, *args.answer_key, *recalls.values(),
                 *[p for _, p in (getattr(args, "gen", None) or [])]])
    t = t0_instant(getattr(args, "t0", None) or SYMPTOM_TS)     # #408: 기준 시각 t0(기본 10-28 경로)
    day = t0_day(t)
    aliases = V.load_aliases(args.aliases)
    view = V.store_view(args.store, t, github_mode=OT.GITHUB_IGNORE)
    work = os.path.abspath(args.work)
    u = E.union_tasks([X.read_json(p) for p in args.answer_key])
    gens = load_symptom_gens(recalls, args.gen_dir, dict(getattr(args, "gen", None) or []), day)
    for arm, g in gens.items():                  # #408: 요청·프롬프트·채점 기준 시각이 같은 t0 인가(지출 0, 저장물만)
        rdoc = read_recall_dir(recalls[arm]).get(day)
        bad = t0_check(t, rdoc, g) + (check_prompt(g, rdoc) if rdoc else [])
        if bad:
            raise Fail("arm %s: t0 or prompt check failed (%s)" % (arm, ",".join(bad)))
    wanted: Dict[str, Set[int]] = defaultdict(set)
    for g in gens.values():
        for gg in g["generations"]:
            for it in gg.get("items") or []:
                k = item_key(it.get("key"), aliases) if it.get("status") == "open" else None
                if k:
                    wanted[V.repo_of(k)].add(V.num_of(k))
    gh_doc, cache_out = load_cache(work, args.github_cache, CACHE_SCORE)
    try:
        calls = extend_cache(gh_doc, wanted, args.offline)
    finally:
        gh_sha = V.write_private(cache_out, json.dumps(gh_doc, sort_keys=True, ensure_ascii=False))
    gh = gh_doc["repos"]
    summary: Dict[str, Any] = {
        "inputs": {"t": _utc_text(t), "store_sha256": V.file_sha256(args.store), "store_key_rules": view.key_rules,
                   "aliases_canonical_sha256": OT.aliases_sha256(aliases),
                   "answer_keys_sha256": [V.file_sha256(p) for p in args.answer_key],
                   "github_cache_r70_sha256": V.file_sha256(args.github_cache), "github_cache_sha256": gh_sha},
        "github": {"calls_this_run": calls, "calls_total": gh_doc.get("calls_total"),
                   "repos_wanted": len(wanted), "repos_linked": sum(1 for r in wanted if (gh.get(r) or {}).get("status") == 200)},
        "tasks": u["count"], "arms": {}}
    private: Dict[str, Any] = {}
    for arm, g in gens.items():
        doc = read_recall_dir(recalls[arm]).get(day)
        if doc is None or doc.get("stage") != "symptom":
            raise Fail("symptom recall missing for arm %s" % arm)
        rows = response_rows(doc)
        new = score_arm(g["generations"], aliases=aliases, gh=gh, t=t, bank=view.states, rows=rows,
                        mems=recall_memories(doc))
        private[arm] = new["_details"]
        summary["arms"][arm] = dict(old_vs_new(g["generations"], u["tasks"], new), table_present=rows is not None,
                                    table_rows=len(rows or {}), gen_prompt_text_sha256=g.get("prompt_text_sha256"),
                                    reps=_strip_private(new["reps"]), decomposition=new["decomposition"])
    det_sha = V.write_private(os.path.join(work, "r7-s0-score-details.json"),
                              json.dumps(private, sort_keys=True, ensure_ascii=False))
    sum_sha = V.write_private(os.path.join(work, "r7-s0-score.json"),
                              json.dumps(summary, sort_keys=True, ensure_ascii=False, indent=1))
    for arm, a in summary["arms"].items():
        n, o = a["new"], a["old"]
        print("%s: old done_ratio_mean=%s precision_like=%s | new P_all=%s P_key=%s P_key_linked=%s "
              "leak/rep=%s keyless/rep=%s keyless_finished/rep=%s" % (
                  arm, V._fmt(o["done_ratio_mean"]), V._fmt(o["precision_like_mean"]), V._fmt(n["mean"]["P_all"]),
                  V._fmt(n["mean"]["P_key"]), V._fmt(n["mean"]["P_key_linked"]), _f(n["leak_per_rep"]),
                  _f(n["keyless_per_rep"]), _f(n["keyless_finished_per_rep"])))
    print("github: calls_this_run=%d cache_sha256=%s" % (calls, gh_sha))
    print("summary_sha256=%s details_sha256=%s" % (sum_sha, det_sha))
    return 0


def _f(x: Optional[float]) -> str:
    return "-" if x is None else "%.2f" % x


# --- add-github ----------------------------------------------------------------------------------------

def store_digest(path: str) -> Dict[str, Any]:
    """memories·mention_roles 행 수와 정렬 행 sha256, meta(값 sha256), 근거 테이블 유무."""
    conn = OT.connect_ro(path)
    try:
        out: Dict[str, Any] = {}
        for table, cols in (("memories", "*"), ("mention_roles", "*"), ("meta", "k, v")):
            rows = sorted(tuple("" if x is None else str(x) for x in r)
                          for r in conn.execute("SELECT %s FROM %s" % (cols, table)))
            out[table] = {"rows": len(rows), "sha256": hashlib.sha256(
                json.dumps(rows, ensure_ascii=False).encode("utf-8")).hexdigest()}
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        out["github_tables"] = {"github_items", "github_repos"} <= tables
        if out["github_tables"]:
            out["github_rows"] = {t: conn.execute("SELECT count(*) FROM %s" % t).fetchone()[0]
                                  for t in ("github_repos", "github_items")}
        return out
    finally:
        conn.close()


def write_github_tables(path: str, github: Mapping[str, Mapping[Any, Mapping[str, Any]]]) -> None:
    """복사본에 근거 테이블만 더한다(기억·언급 행은 건드리지 않는다). 이미 테이블이 있으면 거부."""
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables & {"github_items", "github_repos"}:
            raise Fail("the copy already has GitHub tables")
        conn.execute("BEGIN IMMEDIATE")
        try:
            IR._write_github(conn, github)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def subset_github(github: Mapping[str, Mapping[Any, Mapping[str, Any]]], repos: Set[str]) -> Dict[str, Dict]:
    return {"repos": {r: dict(v) for r, v in (github.get("repos") or {}).items() if r in repos},
            "items": {k: dict(v) for k, v in (github.get("items") or {}).items() if k[0] in repos}}


def cmd_add_github(args, cfg_factory=None, now_text: Optional[str] = None) -> int:
    S = V._sidecar()
    guard_paths([p for pair in args.pair for p in pair])
    work = os.path.abspath(args.work)
    pairs = []
    for src, out in args.pair:
        if os.path.exists(out) or os.path.abspath(out) == os.path.abspath(src):
            raise Fail("--pair output must be a new path")
        if os.path.realpath(os.path.dirname(os.path.abspath(out))) != os.path.realpath(work):
            raise Fail("--pair output must be inside --work")
        _, _, roles = IR.load_store(src)[:3]
        pairs.append((src, out, set(S.store_repos(roles))))
    aliases = V.load_aliases(args.aliases)
    if cfg_factory is None:
        with open(args.owners, encoding="utf-8") as fh:
            owners = json.load(fh)
        table = IK._table(aliases)
        owners = {IK._canonical(k, table) or k.strip().lower(): v for k, v in owners.items()}
        with open(args.default_owner_file, encoding="utf-8") as fh:
            default_owner = fh.read().strip()
        cfg = S.GithubConfig(S.gh_runner(), owners=owners, default_owner=default_owner, min_interval=0)
    else:
        cfg = cfg_factory(S)
    calls = {"n": 0}
    runner = cfg.runner

    def counting(a):
        calls["n"] += 1
        return runner(a)
    cfg.runner = counting
    repos = sorted(set().union(*(p[2] for p in pairs)))
    now_text = now_text or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    github = S.collect_github(repos, None, cfg, now_text)
    report: Dict[str, Any] = {"fetched_at": now_text, "gh_calls": calls["n"], "repos": len(repos),
                              "repo_status": dict(Counter(r.get("status") for r in github["repos"].values())),
                              "items": len(github["items"]), "pairs": []}
    for src, out, rs in pairs:
        before = store_digest(src)
        src_sha = V.file_sha256(src)
        shutil.copyfile(src, out)
        os.chmod(out, 0o600)
        write_github_tables(out, subset_github(github, rs))
        after = store_digest(out)
        same = all(before[t] == after[t] for t in ("memories", "mention_roles", "meta"))
        report["pairs"].append({"src": os.path.basename(src), "out": os.path.basename(out), "src_sha256": src_sha,
                                "src_sha256_after": V.file_sha256(src), "out_sha256": V.file_sha256(out),
                                "repos": len(rs), "before": before, "after": after, "rows_same": same})
        if not same:
            raise Fail("memory/mention rows changed in the copy")
    sha = V.write_private(os.path.join(work, "r7-s0-add-github.json"), json.dumps(report, sort_keys=True, indent=1))
    print(json.dumps({"gh_calls": report["gh_calls"], "repos": report["repos"], "repo_status": report["repo_status"],
                      "items": report["items"],
                      "pairs": [{k: p[k] for k in ("out", "out_sha256", "rows_same", "repos")} | {
                          "memories": p["after"]["memories"]["rows"], "mention_roles": p["after"]["mention_roles"]["rows"],
                          "github_rows": p["after"].get("github_rows")} for p in report["pairs"]],
                      "report_sha256": sha}))
    return 0


# --- render (0-3) --------------------------------------------------------------------------------------

def load_module_file(path: str, name: str = "gateway._r7_item_open_table_before"):
    """리포 밖 파일(예 `git show <sha>:gateway/item_open_table.py` 결과)을 gateway 패키지 안 모듈로 읽는다(상대 import 가
    지금 gateway 를 가리킨다). 리포에 사본을 두지 않는다."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def render_table(mod, args: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], store_path: str,
                 aliases: Mapping[str, str], knobs: Mapping[str, Any], now: Optional[datetime] = None
                 ) -> Tuple[Optional[Dict[str, Any]], str]:
    """게이트웨이 `attach_mcp_body` 의 요소 하나 경로를 모듈 mod 의 공개 함수로 재현 -> (build_table 결과, 사유).
    mod 에 #402 `result_keys` 가 있으면(수정 후) 3상태 모드에서 결과 행 키와 읽는 시각 now 를 `build_table` 에 넘긴다."""
    if not mod.wants_table(args, knobs):
        return None, mod.NOT_ATTACHED_NO_TERM
    raw_ts = args.get("query_timestamp")
    cutoff = parse_instant(raw_ts) if raw_ts else None
    if cutoff is None:
        return None, "bad-query-timestamp"
    repos, source = mod.table_scope(args, rows, knobs, aliases)
    if source == mod.NOT_ATTACHED_NO_KEYS:
        return None, source
    targets = {0: (cutoff, repos, source)}
    gh_repos = mod.github_read_scope(targets, {0: rows}, aliases, knobs)
    store = mod.read_store(store_path, 60.0, gh_repos)          # 오프라인: 시간 상한만 넉넉히
    repos, source = mod.rescope_for_key_rules((repos, source), rows, aliases, store.key_rules)
    if source == mod.NOT_ATTACHED_NO_KEYS_V2:
        return None, source
    github = store.github if mod.github_mode(knobs) == mod.GITHUB_USE else None
    extra: Dict[str, Any] = {}
    if github is not None and hasattr(mod, "result_keys"):     # #402 이후 게이트웨이와 같은 인자
        extra = {"result_keys": mod.result_keys(rows, aliases, store.key_rules),
                 "now": now or datetime.now(timezone.utc)}
    if github is not None and "max_numbers" in inspect.signature(mod.build_table).parameters:   # #410 이후(#412)
        extra["max_numbers"] = store_max_numbers(store_path, mod)
    table = mod.build_table(mod.role_states(store.mentions, cutoff, store.key_rules), cutoff, repos, knobs,
                            github=github, **extra)
    return table, "attached"


def store_max_numbers(store_path: str, mod=OT) -> Dict[str, int]:
    """리포별 수집 최대 번호(#410 공개 이름, 코디네이터 결정 #412-2): `repo_max_numbers(item_roles.load_github(<저장소>))`.
    근거 테이블 전체(뱅크가 언급하지 않은 번호 포함)라 게이트웨이 `Store.github_max`(범위 안 리포)와 범위 안에서 같다."""
    return mod.repo_max_numbers(IR.load_github(store_path))


def result_keys(rows: Sequence[Mapping[str, Any]], aliases: Mapping[str, str], key_rules: Optional[str]) -> Set[str]:
    """결과 행에 보이는 키. 수정 후 게이트웨이 `result_keys`(#402)와 같은 규칙(저장소 key_rules v2 면 참조 고정 아닌
    v2 키, 그 밖은 v1 키)이라 수정 전·후 표를 같은 키 집합으로 잰다."""
    return OT.result_keys(list(rows or []), aliases, key_rules)


def missing_refutations(table: Optional[Mapping[str, Any]], keys: Set[str], gh: Mapping[str, Any], t: datetime,
                        bank: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """결과 행 키 중 t 시점 GitHub 끝난 키와, 그중 표에 반증 줄이 없는 키. 없는 키마다 사유 코드(`by`): 표 줄이 있으면 그 줄
    상태, 줄이 없으면 `no-row:bank-<열림|끝남|none>`(bank = 저장소 t 시점 키 상태, 주면)."""
    states = {r[0]: r[1] for r in (table or {}).get("rows") or []}
    finished = sorted(k for k in keys if V.item_at(gh.get(V.repo_of(k)), V.num_of(k), t)[0] in V.FINISHED)
    miss = [k for k in finished if states.get(k) not in REFUTE_STATES]
    by: Counter = Counter()
    for k in miss:
        if k in states:
            by["row:" + states[k]] += 1
        else:
            st = (bank or {}).get(k)
            by["no-row:bank-" + (st.state if st is not None else "none")] += 1
    return {"finished": len(finished), "missing": len(miss), "missing_keys": miss, "missing_by": dict(sorted(by.items())),
            "open_finished": sum(1 for k in miss if states.get(k) in (OT.ROW_CANDIDATE, OT.STATE_OPEN))}


def queries_for(doc: Mapping[str, Any], plan: Optional[Mapping[str, Any]], symptom: Optional[str]
                ) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    if doc.get("stage") == "symptom":
        if symptom is None or E.sha256_text(symptom) != doc.get("symptom_sha256"):
            raise Fail("symptom text sha256 differs from the saved recall")
        return None, [{"text": symptom}]
    if plan is None:
        raise Fail("dev recalls need --plan")
    spec = plan["per_t"][doc["t"]]
    return spec["instruction"], list(spec["queries"])


def cmd_render(args) -> int:
    guard_paths([args.store, args.plan, args.symptom_file, args.github_cache, args.before_module, *args.recalls])
    aliases = V.load_aliases(args.aliases)
    before = load_module_file(args.before_module)
    after = OT
    knobs = dict(OT._defaults(), github_max_age_hours=0)      # 저장 근거는 오래돼 stale 로 덮이므로 나이 검사 끔(코디네이터)
    key_rules = OT.read_store(args.store, 60.0, None).key_rules
    plan = None
    if args.plan:
        with open(args.plan, encoding="utf-8") as fh:
            plan = json.load(fh)
    symptom = None
    if args.symptom_file:
        with open(args.symptom_file, encoding="utf-8") as fh:
            symptom = fh.read().strip()
    work = os.path.abspath(args.work)
    docs = []
    for d in args.recalls:
        for t_day, doc in read_recall_dir(d).items():
            if doc.get("stage") == "dev":
                V.guard_dev_t(parse_instant(doc["query_timestamp"]).astimezone(V.KST).date())
            docs.append((os.path.basename(os.path.normpath(d)), t_day, doc))
    wanted: Dict[str, Set[int]] = defaultdict(set)
    for _, _, doc in docs:
        for r in doc.get("runs") or []:
            for k in result_keys(r.get("results") or [], aliases, key_rules):
                wanted[V.repo_of(k)].add(V.num_of(k))
    gh_doc, cache_out = load_cache(work, args.github_cache, CACHE_RENDER)
    try:
        calls = extend_cache(gh_doc, wanted, args.offline)
    finally:
        gh_sha = V.write_private(cache_out, json.dumps(gh_doc, sort_keys=True, ensure_ascii=False))
    gh = gh_doc["repos"]
    per: List[Dict[str, Any]] = []
    banks: Dict[datetime, Dict[str, Any]] = {}
    for name, t_day, doc in docs:
        instr, queries = queries_for(doc, plan, symptom)
        t = parse_instant(doc["query_timestamp"])
        for r in doc.get("runs") or []:
            q = queries[r["q"] - 1]
            qargs = E.query_args(instr, q["text"], doc["query_timestamp"], 0)
            rows = r.get("results") or []
            keys = result_keys(rows, aliases, key_rules)
            row = {"set": name, "stage": doc["stage"], "t": t_day, "q": r["q"], "result_keys": len(keys),
                   "_finished_keys": sorted(k for k in keys
                                            if V.item_at(gh.get(V.repo_of(k)), V.num_of(k), t)[0] in V.FINISHED)}
            if t not in banks:
                banks[t] = V.store_view(args.store, t, github_mode=OT.GITHUB_IGNORE).states
            for label, mod in (("before", before), ("after", after)):
                table, why = render_table(mod, qargs, rows, args.store, aliases, knobs)
                m = missing_refutations(table, keys, gh, t, banks[t])
                row[label] = {"reason": why, "rows": len(table["rows"]) if table else 0,
                              "counts": dict(table.get("counts") or {}) if table else {},
                              "text_sha256": E.sha256_text(table["text"]) if table else None, **m}
            per.append(row)
    agg: Dict[str, Any] = {}
    for row in per:
        a = agg.setdefault(row["set"], {"responses": 0, "attached": {"before": 0, "after": 0},
                                        "result_finished": {"before": 0, "after": 0},
                                        "missing": {"before": 0, "after": 0},
                                        "open_finished": {"before": 0, "after": 0},
                                        "responses_with_missing": {"before": 0, "after": 0},
                                        "missing_by": {"before": Counter(), "after": Counter()},
                                        "unique_missing": {"before": set(), "after": set()},
                                        "unique_finished": set(), "same_table": 0})
        a["responses"] += 1
        for lab in ("before", "after"):
            a["attached"][lab] += int(row[lab]["reason"] == "attached")
            a["result_finished"][lab] += row[lab]["finished"]
            a["missing"][lab] += row[lab]["missing"]
            a["open_finished"][lab] += row[lab]["open_finished"]
            a["responses_with_missing"][lab] += int(row[lab]["missing"] > 0)
            a["missing_by"][lab].update(row[lab]["missing_by"])
            a["unique_missing"][lab].update((row["t"], k) for k in row[lab]["missing_keys"])
        a["same_table"] += int(row["before"]["text_sha256"] == row["after"]["text_sha256"])
        a["unique_finished"].update((row["t"], k) for k in row["_finished_keys"])
    for a in agg.values():                       # 키 문자열은 리포 밖 상세에만: 요약은 수만
        a["missing_by"] = {lab: dict(sorted(c.items())) for lab, c in a["missing_by"].items()}
        a["unique_missing"] = {lab: len(v) for lab, v in a["unique_missing"].items()}
        a["unique_finished"] = len(a["unique_finished"])
    label = args.label or "render"
    private = {"responses": per}
    det_sha = V.write_private(os.path.join(work, "r7-s0-%s-details.json" % label),
                              json.dumps(private, sort_keys=True, ensure_ascii=False))
    public = {"store_sha256": V.file_sha256(args.store), "before_module_sha256": V.file_sha256(args.before_module),
              "after_module_sha256": V.file_sha256(OT.__file__), "github_cache_sha256": gh_sha,
              "github_calls_this_run": calls, "knobs_sha256": OT._canon_sha256(knobs), "key_rules": key_rules,
              "knobs": {k: knobs[k] for k in ("scope", "attach", "table_fmt", "max_rows", "recent_done_days",
                                              "github_max_age_hours")}, "sets": agg,
              "responses": [_strip_private(_strip_keys(r)) for r in per]}
    sum_sha = V.write_private(os.path.join(work, "r7-s0-%s.json" % label),
                              json.dumps(public, sort_keys=True, ensure_ascii=False, indent=1))
    for name, a in agg.items():
        print("%s: responses=%d attached before=%d after=%d | result finished keys before=%d after=%d | "
              "missing refutation before=%d after=%d (open rows before=%d after=%d) | unique (t,key) finished=%d "
              "missing before=%d after=%d | same_table=%d" % (
                  name, a["responses"], a["attached"]["before"], a["attached"]["after"],
                  a["result_finished"]["before"], a["result_finished"]["after"], a["missing"]["before"],
                  a["missing"]["after"], a["open_finished"]["before"], a["open_finished"]["after"],
                  a["unique_finished"], a["unique_missing"]["before"], a["unique_missing"]["after"], a["same_table"]))
        print("  missing_by before=%s after=%s" % (json.dumps(a["missing_by"]["before"], ensure_ascii=False),
                                                   json.dumps(a["missing_by"]["after"], ensure_ascii=False)))
    print("github: calls_this_run=%d cache_sha256=%s" % (calls, gh_sha))
    print("summary_sha256=%s details_sha256=%s" % (sum_sha, det_sha))
    return 0


def _strip_keys(row: Mapping[str, Any]) -> Dict[str, Any]:
    return {k: ({kk: vv for kk, vv in v.items() if kk != "missing_keys"} if isinstance(v, dict) else v)
            for k, v in row.items()}


# --- 단계 A(issue #397): 손잡이·감사 줄·저장소 확인·G3 표 토큰 ------------------------------------------

ATTACHED = "attached"
STORE_KEY_RULES = frozenset({IR.KEY_RULES_V2, IR.KEY_RULES_V21})     # 단계 A·연막이 받는 저장소 키 규칙(#412: v2.1 추가)
SUB_REASONS_V2 = (OT.NOT_ATTACHED_NO_TERM, OT.NOT_ATTACHED_NO_KEYS, OT.NOT_ATTACHED_NO_KEYS_V2)
STAGE_A_ARMS = {"on": OT.GITHUB_USE, "ignore": OT.GITHUB_IGNORE}


def knobs_stage_a(github: str) -> Dict[str, Any]:
    """단계 A 손잡이(코디네이터 결정 2): 게이트웨이 기본값(후보형 머리, fmt a) + #402 선택 키를 명시
    (`done_closed_rows` result, `github_max_age_hours` 2) + `github_evidence`(use = 켬 팔, ignore = 켬-ignore 팔)."""
    if github not in (OT.GITHUB_USE, OT.GITHUB_IGNORE):
        raise Fail("github evidence must be use or ignore")
    values = OT._defaults()
    values.update({"github_evidence": github, "done_closed_rows": OT.DONE_CLOSED_RESULT,
                   "github_max_age_hours": OT.GITHUB_MAX_AGE_HOURS_DEFAULT})
    return values


def cmd_knobs(args) -> int:
    values = knobs_stage_a(args.github)
    parsed, warnings = OT.parse_knobs(json.dumps(values, ensure_ascii=False))
    if warnings or parsed != values:
        raise Fail("knobs do not parse back to themselves")
    text = json.dumps(values, ensure_ascii=False, sort_keys=True, indent=1)
    sha = V.write_private(args.out, text)
    print(json.dumps({"github_evidence": args.github, "file_sha256": sha,
                      "canonical_sha256": OT._canon_sha256(parsed), "header_is_default": values["header"] == OT.HEADER_DEFAULT}))
    return 0


def expected_attach_v2(doc: Mapping[str, Any], instruction: Optional[str], queries: Sequence[Mapping[str, Any]],
                       knobs: Mapping[str, Any], aliases: Mapping[str, str], key_rules: str) -> Dict[str, int]:
    """저장 recall 의 질의 인자와 결과 행으로 게이트웨이 부착 판정을 공개 함수로 다시 한다(#396 v2 범위):
    `wants_table` -> `table_scope`(v1, no-result-keys) -> 저장소 key_rules 로 `rescope_for_key_rules`(no-result-keys-v2)."""
    out: Counter = Counter()
    for r in doc.get("runs") or []:
        q = queries[int(r["q"]) - 1]
        args = E.query_args(instruction, q["text"], doc.get("query_timestamp") or "", 0)
        rows = r.get("results") or []
        if not OT.wants_table(args, knobs):
            out[OT.NOT_ATTACHED_NO_TERM] += 1
            continue
        repos, source = OT.table_scope(args, rows, knobs, aliases)
        if source == OT.NOT_ATTACHED_NO_KEYS:
            out[source] += 1
            continue
        _, source = OT.rescope_for_key_rules((repos, source), rows, aliases, key_rules)
        out[OT.NOT_ATTACHED_NO_KEYS_V2 if source == OT.NOT_ATTACHED_NO_KEYS_V2 else ATTACHED] += 1
    return dict(out)


def audit_counts_v2(lines: Iterable[str], start_utc: str, end_utc: str) -> Dict[str, Any]:
    """[start, end] UTC 구간의 decision item-open-table 줄. 런 6 집계(사유·손잡이·하위 사유)에 #396·#402 필드를 더한다:
    `key_rules`·`github_evidence` 값별 줄 수, 3상태 `counts`·`unknown_reasons` 합, `github_fetched_at` 값별 줄 수."""
    lines = list(lines)
    base = E.audit_counts(lines, start_utc, end_utc)
    sub: Counter = Counter()
    kr: Counter = Counter()
    ge: Counter = Counter()
    counts: Counter = Counter()
    unknown: Counter = Counter()
    fetched: Counter = Counter()
    with_counts = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("decision") != OT.DECISION or not (start_utc <= (e.get("ts") or "") <= end_utc):
            continue
        info = e.get("item_open_table") or {}
        if e.get("reason") == "not-attached":
            na = info.get("not_attached")
            if isinstance(na, str):
                sub[na] += 1
            elif isinstance(na, dict):
                for k, v in na.items():
                    sub[k] += int(v)
            else:
                sub["none"] += 1
        if "key_rules" in info:
            kr[info["key_rules"]] += 1
        ge[info.get("github_evidence")] += 1
        if "counts" in info:
            with_counts += 1
            counts.update({k: int(v) for k, v in (info.get("counts") or {}).items()})
            unknown.update({k: int(v) for k, v in (info.get("unknown_reasons") or {}).items()})
        if "github_fetched_at" in info:
            fetched[info.get("github_fetched_at")] += 1
    return dict(base, not_attached=dict(sub), key_rules=dict(kr), github_evidence=dict(ge), counts=dict(counts),
                unknown_reasons=dict(unknown), lines_with_counts=with_counts, github_fetched_at=dict(fetched))


def audit_check_v2(c: Mapping[str, Any], recalls: int, knobs_sha: str, github: str, *,
                   saved_tables: Optional[int] = None, expected: Optional[Mapping[str, int]] = None) -> Dict[str, Any]:
    """S3 켬 측정 판정(코디네이터 결정 4). 실패 항목 목록이 비면 ok.
    * 감사 줄 수 = recall 수, 사유는 attached / not-attached 뿐, 하위 사유는 no-status-term|no-result-keys|no-result-keys-v2
    * attached + 하위 사유 합 = recall 수, attached = 저장 표 수, 사유별 수 = 다시 한 게이트웨이 판정(v2 범위)
    * knobs_source file|env, knobs_sha256 하나 = 기대값, 경고·aliases_mismatch 0, github_evidence 하나 = 팔 값
    * 저장소를 연 줄(attached + no-result-keys-v2)마다 같은 key_rules, 값은 v2 또는 v2.1(#412)
    * use: attached 줄마다 counts·github_fetched_at, unknown_reasons 에 stale 0 / ignore: counts·fetched_at 없음"""
    bad: List[str] = []
    reason = dict(c.get("reason") or {})
    att = reason.get(ATTACHED, 0)
    sub = dict(c.get("not_attached") or {})
    if c.get("lines") != recalls:
        bad.append("lines %s != recalls %s" % (c.get("lines"), recalls))
    other = sorted(k for k in reason if k not in (ATTACHED, "not-attached"))
    if other:
        bad.append("other reasons %s" % other)
    if set(sub) - set(SUB_REASONS_V2):
        bad.append("unknown not_attached %s" % sorted(set(sub) - set(SUB_REASONS_V2)))
    if att + sum(sub.get(k, 0) for k in SUB_REASONS_V2) != recalls:
        bad.append("attached + not_attached != recalls")
    if saved_tables is not None and att != saved_tables:
        bad.append("attached %d != saved tables %d" % (att, saved_tables))
    if expected is not None:
        want = {k: expected.get(k, 0) for k in (ATTACHED,) + SUB_REASONS_V2}
        got = {ATTACHED: att, **{k: sub.get(k, 0) for k in SUB_REASONS_V2}}
        if want != got:
            bad.append("audit %s != expected %s" % (got, want))
    if not set(c.get("knobs_source") or {}) <= {"file", "env"}:
        bad.append("knobs_source %s" % sorted(c.get("knobs_source") or {}))
    if set(c.get("knobs_sha256") or {}) != {knobs_sha}:
        bad.append("knobs_sha256 differs")
    if c.get("knob_warnings") or c.get("aliases_mismatch"):
        bad.append("knob warnings or aliases mismatch")
    if set(c.get("github_evidence") or {}) != {github}:
        bad.append("github_evidence %s != %s" % (sorted(map(str, c.get("github_evidence") or {})), github))
    opened = att + sub.get(OT.NOT_ATTACHED_NO_KEYS_V2, 0)
    kr = dict(c.get("key_rules") or {})
    if opened and not (len(kr) == 1 and set(kr) <= STORE_KEY_RULES and sum(kr.values()) == opened):   # v2·v2.1(#412)
        bad.append("key_rules %s (store-opened lines %d)" % (kr, opened))
    fetched = dict(c.get("github_fetched_at") or {})
    if github == OT.GITHUB_USE:
        if c.get("lines_with_counts", 0) != att:
            bad.append("3-state counts on %s of %d attached lines" % (c.get("lines_with_counts"), att))
        if sum(fetched.values()) != att or None in fetched:
            bad.append("github_fetched_at missing on attached lines")
        if (c.get("unknown_reasons") or {}).get(OT.REASON_STALE, 0):
            bad.append("stale rows %d" % c["unknown_reasons"][OT.REASON_STALE])
    elif c.get("lines_with_counts") or fetched:
        bad.append("ignore arm has 3-state counts or github_fetched_at")
    return {"ok": not bad, "fail": bad}


def _utc_text(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def store_check(path: str, at: datetime, max_age_hours: int = OT.GITHUB_MAX_AGE_HOURS_DEFAULT) -> Dict[str, Any]:
    """사본에 넣은 저장소 파일을 직접 본다(코디네이터 결정 4): 파일 sha256, 행 수·행 sha256(`store_digest`), meta
    key_rules·aliases_sha256, GitHub 테이블 존재·리포 상태별 수·항목 수, 근거 시각(`github_fetched_at` = 가장 이른 리포 시각)과
    at 시점 나이(시간), at 시점 stale 리포 수(게이트웨이 `stale_repos`)."""
    out: Dict[str, Any] = {"sha256": V.file_sha256(path), **store_digest(path)}
    conn = OT.connect_ro(path)
    try:
        meta = {k: v for k, v in conn.execute("SELECT k, v FROM meta")}
    finally:
        conn.close()
    out["key_rules"] = IR.key_rules_of(meta)
    out["aliases_sha256"] = meta.get("aliases_sha256")
    gh = IR.load_github(path)
    if gh is None:
        out["github"] = None
        return out
    repos = gh.get("repos") or {}
    fetched = OT.github_fetched_at(gh)
    age = None
    if fetched:
        age = round((at - parse_instant(fetched)).total_seconds() / 3600.0, 3)
    out["github"] = {"repos": len(repos), "repo_status": dict(sorted(Counter(r.get("status") for r in repos.values()).items())),
                     "items": len(gh.get("items") or {}), "fetched_at": fetched, "at": _utc_text(at),
                     "age_hours": age, "stale_repos": len(OT.stale_repos(gh, at, max_age_hours)),
                     "max_age_hours": max_age_hours}
    return out


def cmd_store_check(args) -> int:
    guard_paths([args.store])
    at = parse_instant(args.at) if args.at else datetime.now(timezone.utc)
    if at is None:
        raise Fail("--at is not an instant")
    out = store_check(args.store, at)
    sha = V.write_private(args.out, json.dumps(out, sort_keys=True, ensure_ascii=False, indent=1))
    g = out["github"] or {}
    print(json.dumps({"sha256": out["sha256"], "key_rules": out["key_rules"], "memories": out["memories"]["rows"],
                      "mention_roles": out["mention_roles"]["rows"], "github_tables": out["github_tables"],
                      "github_repos": g.get("repos"), "github_items": g.get("items"), "repo_status": g.get("repo_status"),
                      "fetched_at": g.get("fetched_at"), "age_hours": g.get("age_hours"),
                      "stale_repos": g.get("stale_repos"), "out_sha256": sha}, ensure_ascii=False))
    return 0


def _window_expected(w: Mapping[str, Any], aliases: Mapping[str, str], knobs: Mapping[str, Any], key_rules: str
                     ) -> Tuple[int, Dict[str, int]]:
    saved = 0
    exp: Counter = Counter()
    plan = None
    if w.get("plan"):
        with open(w["plan"], encoding="utf-8") as fh:
            plan = json.load(fh)
    symptom = None
    if w.get("symptom_file"):
        with open(w["symptom_file"], encoding="utf-8") as fh:
            symptom = fh.read().strip()
    for doc in read_recall_dir(w["recalls_dir"]).values():
        saved += sum(1 for r in doc.get("runs") or [] if r.get("open_table"))
        instr, queries = queries_for(doc, plan, symptom)
        exp.update(expected_attach_v2(doc, instr, queries, knobs, aliases, key_rules))
    return saved, dict(exp)


def cmd_audit(args) -> int:
    """windows: {이름: {start_utc, end_utc, recalls, arm on|ignore|off, recalls_dir?, plan?|symptom_file?, knobs_file?,
    store?}}. on·ignore 는 `audit_check_v2`(knobs_file 의 정규 sha256, store 의 meta key_rules 로 기대 부착 수),
    store 가 있으면 end_utc 시점 `store_check` 도 싣는다. off 는 감사 줄 0 이어야 한다."""
    with open(args.windows, encoding="utf-8") as fh:
        windows = json.load(fh)
    guard_paths([args.log, *[w.get(k) for w in windows.values() for k in ("recalls_dir", "plan", "symptom_file",
                                                                            "knobs_file", "store")]])
    aliases = V.load_aliases(args.aliases)
    with open(args.log, encoding="utf-8") as fh:
        lines = fh.readlines()
    out: Dict[str, Any] = {"log_lines": len(lines), "windows": {}}
    for name, w in windows.items():
        c = audit_counts_v2(lines, w["start_utc"], w["end_utc"])
        c["recalls"] = w.get("recalls")
        arm = w.get("arm")
        if arm in STAGE_A_ARMS:
            with open(w["knobs_file"], encoding="utf-8") as fh:
                knobs, warnings = OT.parse_knobs(fh.read())
            if warnings:
                raise Fail("knobs file warnings %s" % warnings)
            key_rules = IR.KEY_RULES_V2
            if w.get("store"):
                sc = store_check(w["store"], parse_instant(w["end_utc"]))
                key_rules = sc["key_rules"]
                c["store"] = {k: v for k, v in sc.items() if k not in ("memories", "mention_roles", "meta")}
            saved = expected = None
            if w.get("recalls_dir"):
                saved, expected = _window_expected(w, aliases, knobs, key_rules)
                c["saved_tables"], c["expected"] = saved, expected
            c["check"] = audit_check_v2(c, w["recalls"], OT._canon_sha256(knobs), STAGE_A_ARMS[arm],
                                        saved_tables=saved, expected=expected)
            if w.get("store") and key_rules not in STORE_KEY_RULES:
                c["check"]["ok"] = False
                c["check"]["fail"].append("store key_rules %s" % key_rules)
        elif arm == "off":
            c["check"] = {"ok": c["lines"] == 0, "fail": [] if c["lines"] == 0 else ["S3 off window has audit lines"]}
        out["windows"][name] = dict(c, start_utc=w["start_utc"], end_utc=w["end_utc"], arm=arm)
    sha = V.write_private(args.out, json.dumps(out, sort_keys=True, ensure_ascii=False, indent=1))
    for name, v in out["windows"].items():
        print(json.dumps({"window": name, "arm": v["arm"], "lines": v["lines"], "reason": v["reason"],
                          "not_attached": v["not_attached"], "expected": v.get("expected"),
                          "saved_tables": v.get("saved_tables"), "knobs_sha256": v["knobs_sha256"],
                          "key_rules": v["key_rules"], "counts": v["counts"], "unknown_reasons": v["unknown_reasons"],
                          "github_fetched_at": v["github_fetched_at"], "check": v.get("check")}, ensure_ascii=False))
    print(json.dumps({"out_sha256": sha, "all_ok": all(v["check"]["ok"] for v in out["windows"].values()
                                                        if "check" in v)}))
    return 0


def g3_tables(calls: Iterable[Mapping[str, Any]], results: Mapping[Tuple[str, str], Sequence[Mapping[str, Any]]],
              base: str, cand: str, counter: Optional[Callable[[str], int]] = None) -> Dict[str, Any]:
    """벤치 run 의 상태 질의(부착 질의) 표 토큰. 질의 = 측정 호출의 key, 토큰 = 라운드 중앙값(`arm_compare` 와 같은 셈).
    부착 질의 = cand 저장 결과(payload_rest)에 표 객체가 있는 질의. 부착 질의만 따로 토큰 증감 합·질의당 평균·최대, 표 payload
    토큰(counter, 압축 JSON)과 표 줄 상태 수."""
    import statistics
    meas = [c for c in calls if c.get("phase") == "measure"]
    keys = sorted({c["key"] for c in meas})
    tok: Dict[str, Tuple[float, float]] = {}
    for k in keys:
        bt = [c["tokens"] for c in meas if c["key"] == k and c["arm"] == base and "tokens" in c]
        ct = [c["tokens"] for c in meas if c["key"] == k and c["arm"] == cand and "tokens" in c]
        if bt and ct:
            tok[k] = (statistics.median(bt), statistics.median(ct))
    attached: Dict[str, Dict[str, Any]] = {}
    for (arm, k), docs in results.items():
        if arm != cand or k not in tok:
            continue
        for d in docs:
            tab = (d.get("payload_rest") or {}).get(OT.FIELD)
            if isinstance(tab, dict):
                rows = tab.get("rows") or []
                attached[k] = {"payload_tokens": counter(json.dumps(tab, ensure_ascii=False, separators=(",", ":")))
                               if counter else None,
                               "keys": len(tab.get("keys") or []),
                               "row_states": dict(tab.get("counts") or Counter(
                                   r[1] for r in rows if isinstance(r, list) and len(r) > 1))}   # payload 는 counts
                break
    deltas = {k: c - b for k, (b, c) in tok.items()}
    att = sorted(attached)
    att_d = [deltas[k] for k in att]
    other_d = [deltas[k] for k in tok if k not in attached]
    tb, tc = sum(b for b, _ in tok.values()), sum(c for _, c in tok.values())
    return {"queries": len(tok), "base_total": tb, "cand_total": tc,
            "rel_change": round((tc - tb) / tb, 5) if tb else None,
            "attached_queries": len(att), "attached_delta_total": sum(att_d),
            "attached_delta_mean": round(sum(att_d) / len(att_d), 1) if att_d else None,
            "attached_delta_max": max(att_d) if att_d else None,
            "attached_payload_tokens": [attached[k]["payload_tokens"] for k in att],
            "attached_keys": [attached[k]["keys"] for k in att],
            "attached_row_states": [attached[k]["row_states"] for k in att],
            "other_delta_total": sum(other_d), "other_queries_increased": sum(1 for d in other_d if d > 0)}


def cmd_g3_tables(args) -> int:
    calls = []
    with open(os.path.join(args.run, "calls.jsonl"), encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                calls.append(json.loads(line))
    results: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    rdir = os.path.join(args.run, "results")
    for n in sorted(os.listdir(rdir)):
        parts = n.split(".")
        if len(parts) == 4 and parts[3] == "json":
            with open(os.path.join(rdir, n), encoding="utf-8") as fh:
                results[(parts[0], parts[1])].append(json.load(fh))
    counter = None
    try:                                         # runner 와 같은 o200k_base(없으면 표 토큰만 비운다)
        import tiktoken
        enc = tiktoken.get_encoding("o200k_base")
        counter = lambda s: len(enc.encode(s))  # noqa: E731
    except ImportError:
        counter = None
    out = g3_tables(calls, results, args.base, args.cand, counter)
    sha = V.write_private(args.out, json.dumps(out, sort_keys=True, indent=1))
    print(json.dumps(dict(out, out_sha256=sha)))
    return 0


# --- main ----------------------------------------------------------------------------------------------


# --- 단계 B: dev 짝 채점(issue #397 단계 B) -------------------------------------------------------------

DEV_CACHE = "r7-b-dev-github.json"
PAIR_OPEN = "open"


def recall_denominator(gh: Mapping[str, Any], t: datetime, repos: Iterable[str]) -> Tuple[Set[str], int]:
    """R 분모(R7-0 회수율과 같은 정의): t 시점 실제 열린 이슈(PR 제외) 키, 캐시 200 리포만. 이력이 없어 못 정한 수도 낸다."""
    den: Set[str] = set()
    unknown = 0
    for repo in sorted(set(repos)):
        opened, unk = V.open_issues_at(gh.get(repo), t)
        den |= {"%s#%d" % (repo, n) for n in opened}
        unknown += len(unk)
    return den, unknown


def rep_open_keys(gens: Sequence[Mapping[str, Any]], aliases: Mapping[str, str]) -> List[Optional[Set[str]]]:
    """반복마다 열린 항목의 정규 키 집합(무효 반복은 None)."""
    out: List[Optional[Set[str]]] = []
    for g in gens:
        if not g.get("valid"):
            out.append(None)
            continue
        ks = {item_key(it.get("key"), aliases) for it in g.get("items") or [] if it.get("status") == "open"}
        out.append({k for k in ks if k})
    return out


def pair_states(reps: Sequence[Optional[Set[str]]], keys: Iterable[str]) -> Dict[str, str]:
    """키마다 K 반복 과반 상태(런 5·6 `dllm_pair_state` 와 같은 규칙: 무효 반복은 invalid 로 센다)."""
    from bench import r5_metrics as RM
    return {k: RM.dllm_pair_state(["invalid" if r is None else ("open" if k in r else "absent") for r in reps])
            for k in sorted(set(keys))}


def label_controls(rows: Iterable[Mapping[str, Any]], ts: Iterable[str], aliases: Mapping[str, str]
                   ) -> Dict[str, Set[str]]:
    """런 5 라벨셋 dev 대조(실제 열림) 쌍 -> {t: 정규 키}. GitHub 없는 키 하위군 R 의 참고 분모."""
    from bench import r5_metrics as RM
    want = set(ts)
    _, ctl = RM.dev_pairs(list(rows))
    out: Dict[str, Set[str]] = {t: set() for t in want}
    for k, t in ctl:
        nk = item_key(k, aliases)
        if t in want and nk:
            out[t].add(nk)
    return out


def _rv(num: int, den: int) -> Dict[str, Any]:
    return {"den": den, "num": num, "value": _ratio(num, den)}


def dev_arm(gens_by_t: Mapping[str, Sequence[Mapping[str, Any]]], docs_by_t: Mapping[str, Optional[Mapping[str, Any]]],
            *, aliases: Mapping[str, str], gh: Mapping[str, Any], ts: Mapping[str, datetime],
            banks: Mapping[str, Mapping[str, Any]], den: Mapping[str, Set[str]], ctl: Mapping[str, Set[str]],
            mems: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> Dict[str, Any]:
    """한 팔의 dev 지표(t 마다 recall 1회 + 생성 K회). P 는 항목 단위(단계 A `score_arm` 판정, t 시점 GitHub),
    R 은 (키, t) 쌍 단위 과반 상태(분모 = t 시점 실제 열린 이슈). 하위군: GitHub 연결 키 / GitHub 없는 키 / 번호 없는 일."""
    all_recs: List[Mapping[str, Any]] = []
    rep_scores: List[Mapping[str, Any]] = []
    per_t: Dict[str, Any] = {}
    pairs: Dict[Tuple[str, str], int] = {}
    ctl_pairs: Dict[Tuple[str, str], int] = {}
    rep_hits: Dict[int, List[int]] = defaultdict(lambda: [0, 0])
    details: Dict[str, Any] = {}
    unknown_raised = 0
    unknown_row_hits = 0          # R 분자 쌍 중 그 팔 응답 표에서 키 줄이 미확인·충돌인 쌍
    for t in sorted(gens_by_t):
        gens = gens_by_t[t]
        rows = response_rows(docs_by_t.get(t))
        sa = score_arm(gens, aliases=aliases, gh=gh, t=ts[t], bank=banks.get(t) or {}, rows=rows,
                       mems=mems.get(t) or {})
        for recs in sa["_details"]:
            if recs is None:
                continue
            all_recs.extend(recs)
            unknown_raised += sum(1 for r in recs if r["table"] == OT.ROW_UNKNOWN)
        rep_scores.extend(r for r in sa["reps"] if r.get("valid"))
        reps = rep_open_keys(gens, aliases)
        st = pair_states(reps, den.get(t, set()) | ctl.get(t, set()))
        for k in den.get(t, set()):
            pairs[(k, t)] = int(st[k] == PAIR_OPEN)
            if pairs[(k, t)] and rows is not None and OT.ROW_UNKNOWN in (rows.get(k) or ()):
                unknown_row_hits += 1
        for k in ctl.get(t, set()):
            if not V.linked(k, gh):
                ctl_pairs[(k, t)] = int(st[k] == PAIR_OPEN)
        for i, r in enumerate(reps):
            if r is not None:
                rep_hits[i][0] += len(r & den.get(t, set()))
                rep_hits[i][1] += len(den.get(t, set()))
        per_t[t] = {"den": len(den.get(t, set())), "num": sum(pairs[(k, t)] for k in den.get(t, set())),
                    "P_key_pooled": sa["pooled"]["P_key"], "keyed": sa["pooled"]["keyed"],
                    "linked_open": sa["pooled"]["linked_open"], "open_items": sa["pooled"]["open_items"],
                    "invalid": sa["invalid"], "leak": sa["pooled"]["leak"], "table_present": rows is not None,
                    "table_rows": len(rows or {})}
        details[t] = sa["_details"]
    pooled = score_items(all_recs)
    r_num = sum(pairs.values())
    rc_num = sum(ctl_pairs.values())
    sub = {"linked": {"P_den": pooled["linked"], "P_num": pooled["linked_open"],
                      "P": _ratio(pooled["linked_open"], pooled["linked"]), "R_den": len(pairs), "R_num": r_num,
                      "R": _ratio(r_num, len(pairs))},
           "unlinked": {"P_den": pooled["unconfirmed"], "P_num": None, "rule": pooled["unconfirmed_rule"],
                        "R_label": _rv(rc_num, len(ctl_pairs))},
           "no_key": {"P_den": pooled["keyless"], "verdicts": pooled["keyless_verdicts"],
                      "bare_number": pooled["keyless_bare_number"], "R_den": None}}
    return {"P_key": pooled["P_key"], "P_key_linked": pooled["P_key_linked"], "P_all": pooled["P_all"],
            "P_key_rep_mean": _mean([r["P_key"] for r in rep_scores]),
            "R": _rv(r_num, len(pairs)),
            "R_rep_mean": _mean([_ratio(h, d) for h, d in rep_hits.values()]),
            "leak_per_rep": _mean([float(r["leak"]) for r in rep_scores]),
            "bank_leak_per_rep": _mean([float(r["bank_leak"]) for r in rep_scores]),
            "open_items_per_rep": _mean([float(r["open_items"]) for r in rep_scores]),
            "valid_reps": len(rep_scores), "unknown_raised": unknown_raised, "R_hits_unknown_row": unknown_row_hits,
            "pooled": pooled, "decomposition": decomposition(all_recs), "subgroups": sub, "per_t": per_t,
            "_pairs": pairs, "_ctl_pairs": ctl_pairs, "_details": details}


def paired_recall(a: Mapping[Tuple[str, str], int], b: Mapping[Tuple[str, str], int]) -> Dict[str, Any]:
    """R(a) - R(b) 짝 차, 키 군집 재표집(런 6 `r5_metrics.cluster_bootstrap`, 씨앗 20261101, 2000회). 반폭 = 95% 구간 폭 / 2
    (상위 #393 단계 B 의 R 비열화 최소 검출 차)."""
    from bench import r5_metrics as RM
    out = RM.cluster_bootstrap(a, b)
    out["half_width"] = (out["hi"] - out["lo"]) / 2
    return out


def load_dev_gens(gen_dirs: Mapping[str, str], labels: Mapping[str, str], recalls: Mapping[str, str],
                  ts: Sequence[str]) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """팔·t 마다 dev 생성 파일 `<gen_dir>/<label>-<t>-gen.json`. 생성 머리 stage dev·t 같음·K 개 생성, recall_sha256 =
    그 팔 recall 파일 sha256 이어야 한다(다른 팔·다른 세션 생성을 섞지 않게)."""
    from bench import r5_roles as X
    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for arm, gdir in gen_dirs.items():
        if arm not in recalls:
            raise Fail("--gen label has no --recalls")
        lab = labels.get(arm, "dev-" + arm)
        out[arm] = {}
        for t in ts:
            g = X.read_json(os.path.join(gdir, "%s-%s-gen.json" % (lab, t)))
            rec = os.path.join(recalls[arm], "t-%s.json" % t)
            if g.get("stage") != "dev" or g.get("t") != t or not os.path.exists(rec) or \
                    g.get("recall_sha256") != V.file_sha256(rec):
                raise Fail("gen %s %s is not the dev generation of its recall" % (arm, t))
            if len(g.get("generations") or []) != g.get("k"):
                raise Fail("incomplete gen %s %s" % (arm, t))
            out[arm][t] = g
    return out


def cmd_dev_score(args) -> int:
    gen_dirs, recalls, labels = dict(args.gen), dict(args.recalls), dict(args.label or [])
    guard_paths([args.store, args.aliases, args.github_cache, args.labelset, *gen_dirs.values(), *recalls.values()])
    if set(gen_dirs) != {"on", "off"} or set(recalls) != {"on", "off"}:
        raise Fail("dev-score needs arms on and off")
    ts = sorted(args.t or V.DEV_TS)
    for t in ts:
        V.guard_dev_t(datetime.strptime(t, "%Y-%m-%d").date())
    aliases = V.load_aliases(args.aliases)
    work = os.path.abspath(args.work)
    gens = load_dev_gens(gen_dirs, labels, recalls, ts)
    ks = {g.get("k") for by_t in gens.values() for g in by_t.values()}
    if len(ks) != 1:
        raise Fail("arms or t use different K %s" % sorted(map(str, ks)))
    docs = {arm: read_recall_dir(recalls[arm]) for arm in recalls}
    inst: Dict[str, datetime] = {}
    for t in ts:
        got = {parse_instant((docs[arm].get(t) or {}).get("query_timestamp")) for arm in docs}
        if len(got) != 1 or None in got:
            raise Fail("recall query_timestamp missing or differs between arms at %s" % t)
        inst[t] = got.pop()
        day = inst[t].astimezone(V.KST).date()
        V.guard_dev_t(day)                       # 실제로 쓰는 시각도 봉인 검사(리뷰 메모 1)
        if day.isoformat() != t:
            raise Fail("recall query_timestamp is not on its t (%s)" % t)
    wanted: Dict[str, Set[int]] = defaultdict(set)
    for repo in set(aliases.values()):
        wanted[repo]
    for by_t in gens.values():
        for g in by_t.values():
            for gg in g["generations"]:
                if not gg.get("valid"):
                    continue
                for itm in gg.get("items") or []:
                    k = item_key(itm.get("key"), aliases) if itm.get("status") == "open" else None
                    if k:
                        wanted[V.repo_of(k)].add(V.num_of(k))
    gh_doc, cache_out = load_cache(work, args.github_cache, DEV_CACHE)
    try:
        calls = extend_cache(gh_doc, wanted, args.offline)
    finally:
        gh_sha = V.write_private(cache_out, json.dumps(gh_doc, sort_keys=True, ensure_ascii=False))
    gh = gh_doc["repos"]
    den, unknown = {}, {}
    for t in ts:
        den[t], unknown[t] = recall_denominator(gh, inst[t], wanted)
    ctl = {t: set() for t in ts}
    if args.labelset:
        with open(args.labelset, encoding="utf-8") as fh:
            ctl = label_controls([json.loads(x) for x in fh if x.strip()], ts, aliases)
    views = {t: V.store_view(args.store, inst[t], github_mode=OT.GITHUB_IGNORE) for t in ts}
    arms: Dict[str, Any] = {}
    private: Dict[str, Any] = {}
    for arm in ("on", "off"):
        res = dev_arm({t: g["generations"] for t, g in gens[arm].items()}, docs[arm], aliases=aliases, gh=gh, ts=inst, banks={t: views[t].states for t in ts},
                      den=den, ctl=ctl, mems={t: recall_memories(docs[arm].get(t)) for t in ts})
        arms[arm] = res
        private[arm] = {"details": res["_details"], "pairs": sorted([k, t, v] for (k, t), v in res["_pairs"].items())}
    pr = paired_recall(arms["on"]["_pairs"], arms["off"]["_pairs"])
    prl = paired_recall(arms["on"]["_ctl_pairs"], arms["off"]["_ctl_pairs"]) if arms["on"]["_ctl_pairs"] else None
    summary = {
        "inputs": {"t": {t: _utc_text(inst[t]) for t in ts}, "store_sha256": V.file_sha256(args.store),
                   "store_key_rules": views[ts[0]].key_rules, "aliases_canonical_sha256": OT.aliases_sha256(aliases),
                   "labelset_sha256": V.file_sha256(args.labelset) if args.labelset else None,
                   "github_cache_r70_sha256": V.file_sha256(args.github_cache), "github_cache_sha256": gh_sha,
                   "gen_prompt_text_sha256": {a: {t: g.get("prompt_text_sha256") for t, g in by_t.items()}
                                              for a, by_t in gens.items()},
                   "recall_sha256": {a: {t: g.get("recall_sha256") for t, g in by_t.items()} for a, by_t in gens.items()}},
        "github": {"calls_this_run": calls, "calls_total": gh_doc.get("calls_total"), "repos_range": len(wanted),
                   "repos_linked": sum(1 for r in wanted if (gh.get(r) or {}).get("status") == 200),
                   "den_unknown": unknown},
        "arms": {a: _strip_private(r) for a, r in arms.items()},
        "paired_on_minus_off": {"R": pr, "R_label_unlinked": prl}}
    det_sha = V.write_private(os.path.join(work, args.out_name + "-details.json"),
                              json.dumps(private, sort_keys=True, ensure_ascii=False))
    sum_sha = V.write_private(os.path.join(work, args.out_name + ".json"),
                              json.dumps(summary, sort_keys=True, ensure_ascii=False, indent=1))
    for arm in ("on", "off"):
        a = arms[arm]
        print("%s: P_key=%s (%d/%d) P_key_rep_mean=%s R=%s (%d/%d) R_rep_mean=%s leak/rep=%s unknown_raised=%d valid=%d"
              % (arm, V._fmt(a["P_key"]), a["pooled"]["linked_open"], a["pooled"]["keyed"],
                 V._fmt(a["P_key_rep_mean"]), V._fmt(a["R"]["value"]), a["R"]["num"], a["R"]["den"],
                 V._fmt(a["R_rep_mean"]), _f(a["leak_per_rep"]), a["unknown_raised"], a["valid_reps"]))
    print("R on-off: diff=%.4f 95%% [%.4f, %.4f] half_width=%.4f clusters=%d pairs=%d"
          % (pr["diff"], pr["lo"], pr["hi"], pr["half_width"], pr["clusters"], pr["pairs"]))
    print("github: calls_this_run=%d cache_sha256=%s" % (calls, gh_sha))
    print("summary_sha256=%s details_sha256=%s" % (sum_sha, det_sha))
    return 0


# --- 운영 시점 연막 실행기(issue #408) ------------------------------------------------------------------------
#
# 원래 증상 경로를 임의 기준 시각 t0 로 돌린다(10-28 경로는 `score` 기본값 t0 = SYMPTOM_TS 로 그대로 재현).
#   smoke-recall  게이트웨이 recall 읽기만(질의마다 파일 하나, 모든 질의의 query_timestamp = t0 = 요청 시각)
#   smoke-attach  저장 recall 에 역할 저장소로 표를 오프라인 렌더해 붙인다(게이트웨이 공개 함수, 결과 행 키·읽는 시각 t0)
#   smoke-dllm    d-llm K회(기준선 동결 프롬프트, 기준일 = t0 의 KST 날짜, 생성 머리에 t0, 출력 경로는 인자 그대로)
#   smoke         t0 일치 검사 -> 입력 검사 -> t0 에 새로 받은 GitHub 캐시(GET) -> 거짓 열림 관문·참고 지표·표 키 전체 대조
# 실패(exit 1): t0 불일치(t0-request / t0-prompt / t0-score), 표 미부착(no-table), 필드 누락(field:<칸>), 빈 분모
# (empty:<분모>), 유효 생성 부족(valid-reps), 프롬프트·표·recall sha256 불일치. 관문(exit 4): 거짓 열림 1건 이상(rollback).

SMOKE_STAGE = "smoke"
SMOKE_TABLE_FIELDS = ("t", "fmt", "keys", "text", "counts", "fetched_at")      # 3상태 payload 표
SMOKE_HEAD_FIELDS = ("stage", "t", "t0", "k", "recall_sha256", "prompt_text_sha256", "table_sha256",
                     "max_input_chars")
FALSE_GITHUB, FALSE_MISSING, FALSE_REFUTED = "github-finished", "missing-number", "table-refuted"
FALSE_REASONS = (FALSE_GITHUB, FALSE_MISSING, FALSE_REFUTED)
MISSING_AT_T0 = frozenset({"missing_number", "not_yet"})     # t0 시점에 없던 번호(t0 뒤 생성 포함)
ROLLBACK_REASONS = (FALSE_GITHUB, FALSE_REFUTED)               # #412: 없는 번호는 공개만
ROLLBACK_CATEGORIES = ("github_finished", "table_refuted", "over_max_rows", "cross_conflict_rows", "runner_failure")
MISSING_BUCKETS = ("le_max", "over_max", "max_unknown")
CROSS_OPEN = frozenset({"open_issue", "pr_open", "reopened_unknown"})   # 교차 충돌: 다른 리포 같은 번호가 t0 열림
CLOSED_OK = frozenset({"closed_completed", "pr_merged"})       # `확인된 종결` 줄이 맞는 범주
NOT_PLANNED_OK = frozenset({"closed_not_planned", "pr_closed"})  # `종결(미계획)` 줄이 맞는 범주
EXIT_ROLLBACK = 4


def t0_instant(text: Optional[str]) -> datetime:
    t = parse_instant(text)
    if t is None:
        raise Fail("t0 is not an instant")
    return t


def t0_day(t0: datetime) -> str:
    """소비자 프롬프트 기준일(`{query_date}`) = t0 의 KST 날짜."""
    return t0.astimezone(V.KST).date().isoformat()


def t0_check(t0: datetime, doc: Optional[Mapping[str, Any]], gen: Optional[Mapping[str, Any]] = None) -> List[str]:
    """recall 요청 시각(저장 recall `query_timestamp`) · 소비자 프롬프트 기준일(생성 머리 `t`) · 채점 t0 가 같은가.
    생성 머리에 `t0` 가 있으면 그것도 채점 t0 와 같아야 한다(t0-score). 어긋난 칸의 코드 목록."""
    bad: List[str] = []
    if parse_instant((doc or {}).get("query_timestamp")) != t0:
        bad.append("t0-request")
    if gen is not None:
        if gen.get("t") != t0_day(t0):
            bad.append("t0-prompt")
        if "t0" in gen and parse_instant(gen.get("t0")) != t0:
            bad.append("t0-score")
    return bad


def check_prompt(gen: Mapping[str, Any], doc: Mapping[str, Any]) -> List[str]:
    """생성 머리 기준일(`t`)·상한으로 d-llm 입력을 다시 조립해 프롬프트·표 sha256 이 머리와 같은지(동결 기준선 프롬프트)."""
    from bench import d_llm as D
    bad: List[str] = []
    if "prompt_sha256" in gen and gen["prompt_sha256"] != V.file_sha256(D.BASELINE_PROMPT):
        bad.append("prompt-file")
    try:
        b = E.build_input(D, D.load_prompt(D.BASELINE_PROMPT), doc, gen.get("t"), int(gen.get("max_input_chars")))
    except (TypeError, ValueError, KeyError, E.Fail):
        return bad + ["prompt-rebuild"]
    if gen.get("prompt_text_sha256") != E.sha256_text(b["prompt"]):
        bad.append("prompt-sha")
    if gen.get("table_sha256") != b["table"]["sha256"]:
        bad.append("table-sha")
    return bad


def table_lines(tab: Optional[Mapping[str, Any]]) -> List[List[str]]:
    """payload 표 `text` -> 줄 [키, 상태, 셋째 칸(날짜·사유, 없으면 "")]. 머리·설명·생략 줄은 건너뛴다."""
    known = set(OT.ROW_STATES) | {OT.STATE_OPEN, OT.STATE_DONE}
    out: List[List[str]] = []
    for line in str((tab or {}).get("text") or "").splitlines():
        cells = [c.strip() for c in line.split(" | ")]
        if len(cells) >= 2 and _ROW_KEY_RE.match(cells[0]) and cells[1] in known:
            out.append([cells[0], cells[1], cells[2] if len(cells) > 2 else ""])
    return out


def smoke_fields(doc: Optional[Mapping[str, Any]], gen: Optional[Mapping[str, Any]]) -> List[str]:
    """입력 칸 검사: 표 미부착(no-table), 3상태 표·생성 머리·생성·항목 칸 누락(field:<자리>.<칸>)."""
    bad: List[str] = []
    doc = doc or {}
    for k in ("query_timestamp", "runs"):
        if k not in doc:
            bad.append("field:recall.%s" % k)
    tabs = [r.get("open_table") for r in doc.get("runs") or [] if r.get("open_table")]
    if not tabs:
        bad.append("no-table")
    for tab in tabs:
        bad.extend("field:table.%s" % k for k in SMOKE_TABLE_FIELDS if k not in tab)
    if gen is not None:
        bad.extend("field:gen.%s" % k for k in SMOKE_HEAD_FIELDS if k not in gen)
        if not isinstance(gen.get("generations"), list):
            bad.append("field:gen.generations")
        else:
            for g in gen["generations"]:
                if "valid" not in g:
                    bad.append("field:generation.valid")
                    break
                if g.get("valid") and any(not isinstance(x, Mapping) or "status" not in x
                                          for x in g.get("items") or []):
                    bad.append("field:item.status")
                    break
    return sorted(set(bad), key=bad.index)


def false_open_reason(rec: Mapping[str, Any], rows: Optional[Mapping[str, Set[str]]]) -> Optional[str]:
    """키 있는 열린 항목이 t0 시점 거짓 열림인가: GitHub 끝남(FINISHED) > 그 응답 표의 반증 줄(`확인된 종결`·`종결(미계획)`,
    GitHub 는 끝나지 않음) > 없는 번호(t0 뒤 생성 포함). 미연결 키(리포 없음·조회 실패)는 거짓 열림으로 치지 않는다.
    #412 관문 재정의: 앞 둘은 롤백, 없는 번호는 공개만(최대 번호 이하·초과·모름으로 나눠 낸다). 그래서 반증 줄을 없는 번호보다
    먼저 본다(반증 줄이 있는 없는 번호 키도 롤백)."""
    if not rec.get("has_key"):
        return None
    if rec.get("github") in V.FINISHED:
        return FALSE_GITHUB
    if rows is not None and REFUTE_STATES & set(rows.get(rec["key"]) or ()):
        return FALSE_REFUTED
    if rec.get("github") in MISSING_AT_T0:
        return FALSE_MISSING
    return None


def gate_of(by_reason: Mapping[str, int], over_max_rows: int, cross_conflict_rows: int, fail: Sequence[str],
            table_accuracy: Optional[Mapping[str, int]] = None) -> Dict[str, Any]:
    """롤백 관문(#412 재정의, 0 허용): 키 있는 열린 항목 중 t0 GitHub 끝남 / 그 응답 표 반증 줄 키를 열림으로 낸 것 / 표 안
    over-max 줄 / 교차 충돌 종결 줄 / 실행기 실패 조건(실패 코드 수). 하나라도 1 이상이면 rollback.
    `table_accuracy`(table-gate 만, issue #414) = `table_accuracy()` 범주(정밀도 문턱 미달 표 수·종결 오류·미계획 오류)를 더한다.
    없으면(smoke) 범주 칸 자체를 넣지 않는다(요약 바이트 불변)."""
    cats = {"github_finished": int(by_reason.get(FALSE_GITHUB, 0)), "table_refuted": int(by_reason.get(FALSE_REFUTED, 0)),
            "over_max_rows": int(over_max_rows), "cross_conflict_rows": int(cross_conflict_rows),
            "runner_failure": len(fail)}
    if table_accuracy is not None:
        cats.update({k: int(table_accuracy.get(k, 0)) for k in ACCURACY_CATS})
    total = sum(cats.values())
    return {"verdict": "rollback" if total else "pass", "rollback": cats, "rollback_total": total}


def missing_bucket(key: str, max_numbers: Optional[Mapping[str, int]]) -> str:
    """없는 번호 키 -> 저장소 리포 최대 번호 대비 le_max / over_max / max_unknown(그 리포 최대 번호 없음)."""
    top = (max_numbers or {}).get(V.repo_of(key))
    if not isinstance(top, int):
        return "max_unknown"
    return "over_max" if V.num_of(key) > top else "le_max"


def smoke_score(gens: Sequence[Mapping[str, Any]], *, aliases: Mapping[str, str], gh: Mapping[str, Any], t0: datetime,
                bank: Mapping[str, Any], rows: Optional[Mapping[str, Set[str]]],
                mems: Mapping[str, Mapping[str, Any]], min_valid: int,
                max_numbers: Optional[Mapping[str, int]] = None, over_max_rows: int = 0,
                cross_conflict_rows: int = 0) -> Dict[str, Any]:
    """생성 K개(응답 표 하나) -> 롤백 관문(`gate_of`, 표 안 over-max 줄·교차 충돌 줄 수는 호출자가 표에서 센 값),
    공개 칸(미연결 키·재오픈 미상·없는 번호 최대 번호 대비·키 없는 끝남·열린 PR 유래·residual 종결 키), 참고 지표(P_key·P_all),
    실패 코드(빈 분모·유효 생성 부족). max_numbers = 저장소 리포 최대 번호(`store_max_numbers`). 키 목록은 `_` 칸(리포 밖 상세)에만."""
    sa = score_arm(gens, aliases=aliases, gh=gh, t=t0, bank=bank, rows=rows, mems=mems)
    per_rep: List[Dict[str, Any]] = []
    false_keys: Set[str] = set()
    resid_keys: Set[str] = set()
    missing_keys: Set[str] = set()
    tot: Counter = Counter()
    for g, recs in zip(gens, sa["_details"]):
        if recs is None:
            continue
        c: Counter = Counter()
        for r in recs:
            why = false_open_reason(r, rows)
            if why in ROLLBACK_REASONS:
                c[why] += 1
                c["total"] += 1
                c["out_of_table" if r["table"] in (OUT_OF_TABLE, NO_TABLE) else "in_table"] += 1
                false_keys.add(r["key"])
            elif why == FALSE_MISSING:
                c["missing_" + missing_bucket(r["key"], max_numbers)] += 1
                missing_keys.add(r["key"])
            c["open_pr"] += int(r["github"] == "pr_open")
            c["reopened_unknown"] += int(r["github"] == "reopened_unknown")
        for x in g.get("items") or []:
            k = item_key(x.get("key"), aliases) if x.get("status") == "residual" else None
            if k and V.item_at(gh.get(V.repo_of(k)), V.num_of(k), t0)[0] in V.FINISHED:
                c["residual_finished"] += 1
                resid_keys.add(k)
        per_rep.append({k: c[k] for k in ("total", "in_table", "out_of_table")})
        tot.update(c)
    pooled = sa["pooled"]
    valid = len(gens) - sa["invalid"]
    fail: List[str] = []
    for name, n in (("open-items", pooled["open_items"]), ("keyed", pooled["keyed"]), ("linked", pooled["linked"])):
        if not n:
            fail.append("empty:" + name)
    if valid < min_valid:
        fail.append("valid-reps")
    fo = {"total": tot["total"], "in_table": tot["in_table"], "out_of_table": tot["out_of_table"],
          "by_reason": {k: tot[k] for k in ROLLBACK_REASONS if tot[k]}, "per_rep": per_rep,
          "per_rep_mean": _mean([float(r["total"]) for r in per_rep]), "unique_keys": len(false_keys)}
    kv = pooled["keyless_verdicts"]
    ref = {"P_key": pooled["P_key"], "P_all": pooled["P_all"], "P_key_linked": pooled["P_key_linked"],
           "mean": sa["mean"], "open_items": pooled["open_items"], "keyed": pooled["keyed"], "linked": pooled["linked"],
           "unconfirmed": pooled["unconfirmed"], "verdicts": pooled["verdicts"],
           "keyless": {"items": pooled["keyless"], "finished": kv[FINISHED], "open": kv[OPEN], "hold": kv[HOLD]},
           "open_pr_items": tot["open_pr"],
           "residual_finished": {"items": tot["residual_finished"], "unique_keys": len(resid_keys)},
           "valid_reps": valid, "K": len(gens), "github_categories": pooled["github_categories"]}
    gate = dict(gate_of(fo["by_reason"], over_max_rows, cross_conflict_rows, fail), false_open=fo)
    disclosure = {"unconfirmed": pooled["unconfirmed"], "reopened_unknown": tot["reopened_unknown"],
                  "missing_number_open": {b: tot["missing_" + b] for b in MISSING_BUCKETS},
                  "keyless_finished": kv[FINISHED], "open_pr_items": tot["open_pr"],
                  "residual_finished": {"items": tot["residual_finished"], "unique_keys": len(resid_keys)}}
    return {"gate": gate, "disclosure": disclosure, "reference": ref,
            "fail": fail, "decomposition": sa["decomposition"], "_details": sa["_details"],
            "_false_keys": sorted(false_keys), "_residual_keys": sorted(resid_keys),
            "_missing_keys": sorted(missing_keys)}


def table_check(tab: Mapping[str, Any], gh: Mapping[str, Any], t0: datetime) -> Dict[str, Any]:
    """표 하나의 모든 키를 t0 시점 GitHub(새 캐시)과 대조(상위 #393 checkpoint (3)·(4)): `열림 후보` 정밀도(전체·GitHub 연결
    키), `확인된 종결`·`종결(미계획)` 오류(그 줄 상태와 맞지 않는 범주), 미확인·충돌 사유별 수, 줄 상태 x 범주."""
    lines = table_lines(tab)
    rows = [[k, s, "", x] if s == OT.ROW_UNKNOWN else [k, s, x, ""] for k, s, x in lines]
    dist = V.row_categories(rows, gh, t0)
    cand = [k for k, s, _ in lines if s == OT.ROW_CANDIDATE]
    cats = {k: V.item_at(gh.get(V.repo_of(k)), V.num_of(k), t0)[0] for k, _, _ in lines}
    linked = [k for k in cand if V.linked(k, gh)]
    oi = sum(1 for k in cand if cats[k] == "open_issue")
    loi = sum(1 for k in linked if cats[k] == "open_issue")
    closed = [k for k, s, _ in lines if s == OT.ROW_CLOSED]
    npl = [k for k, s, _ in lines if s == OT.ROW_NOT_PLANNED]
    counts = dict(tab.get("counts") or {})
    parsed = dict(Counter(s for _, s, _ in lines))
    return {"rows": len(lines), "truncated": tab.get("truncated"), "counts": counts,
            "parsed_matches_counts": parsed == counts,
            "candidate": {"rows": len(cand), "open_issue": oi, "precision": _ratio(oi, len(cand)),
                          "linked_rows": len(linked), "linked_open_issue": loi,
                          "linked_precision": _ratio(loi, len(linked))},
            "closed_rows": len(closed), "closed_errors": sum(1 for k in closed if cats[k] not in CLOSED_OK),
            "closed_errors_open_issue": sum(1 for k in closed if cats[k] == "open_issue"),
            "not_planned_rows": len(npl), "not_planned_errors": sum(1 for k in npl if cats[k] not in NOT_PLANNED_OK),
            "not_planned_errors_open_issue": sum(1 for k in npl if cats[k] == "open_issue"),
            "unknown_reasons": dist["unknown_reasons"], "by_state": dist["by_state"],
            "fetched_at": tab.get("fetched_at")}


def over_max_rows(tab: Optional[Mapping[str, Any]], store_github: Optional[Mapping[str, Any]],
                  max_numbers: Mapping[str, int]) -> List[str]:
    """표 안 over-max 줄(#412 롤백 범주): `미확인·충돌` 사유 no-number 이고 게이트웨이 `over_max`(저장소 근거, 신선도 예외 없이)가
    참인 줄의 키. drop 렌더면 신선도 예외(수집 뒤 언급)로 남은 줄만 여기 걸린다."""
    return [k for k, st, x in table_lines(tab)
            if st == OT.ROW_UNKNOWN and x == OT.REASON_NO_NUMBER and OT.over_max(k, store_github, max_numbers)]


def cross_candidates(tab: Optional[Mapping[str, Any]], mem_keys: Mapping[str, Iterable[str]],
                     listing: Mapping[str, Mapping[str, Any]], aliases: Mapping[str, str], rules: str
                     ) -> Tuple[List[Tuple[str, str, str]], int]:
    """교차 충돌 후보(#412): 표의 `확인된 종결`·`종결(미계획)` 줄 키마다 그 키를 언급한 저장소 기억(mem_keys = {기억 id: 키들},
    t0 이하 valid)의 본문(listing)을 문장으로 나눠(`item_roles.split_sentences`, 저장소 언급과 같은 경계) 저장소 키 규칙의
    `key_hits_for` 로 다시 푼다. 그 키가 나온 문장에 명시된(접두 known·owner) 다른 리포의 같은 번호 -> (줄 키, 다른 키, 기억 id).
    item 태그는 본문 규칙에 넣지 않는다(`memory_mentions` 와 같다). -> (후보, 본문이 없는 기억 수)."""
    closed = [k for k, st, _ in table_lines(tab) if st in REFUTE_STATES]
    by_key: Dict[str, List[str]] = defaultdict(list)
    for mid, keys in mem_keys.items():
        for k in keys:
            by_key[k].append(mid)
    out: List[Tuple[str, str, str]] = []
    missing: Set[str] = set()
    seen: Set[Tuple[str, str, str]] = set()
    for key in closed:
        repo, n = V.repo_of(key), V.num_of(key)
        for mid in sorted(by_key.get(key) or ()):
            mem = listing.get(mid)
            if mem is None:
                missing.add(mid)
                continue
            tags = [t for t in (mem.get("tags") or []) if isinstance(t, str)
                    and not t.strip().lower().startswith("item:")]
            for sent in IR.split_sentences(mem.get("text") or ""):
                hits = IR.key_hits_for(sent, tags, aliases, rules)
                if key not in {IR.key_text(h.key) for h in hits if h.source != IK.SOURCE_TAG}:
                    continue
                for other in sorted({h.key[0] for h in hits if h.source in (IK.SOURCE_KNOWN, IK.SOURCE_OWNER)} - {repo}):
                    c = (key, "%s#%d" % (other, n), mid)
                    if c not in seen:
                        seen.add(c)
                        out.append(c)
    return out, len(missing)


def cross_conflict_rows(cands: Iterable[Tuple[str, str, str]], gh: Mapping[str, Any], t0: datetime) -> List[str]:
    """교차 충돌 종결 줄 키: 후보의 다른 키가 t0 GitHub(평가 캐시)에서 열림(열린 이슈·열린 PR·재오픈 미상)인 줄."""
    return sorted({k for k, other, _ in cands
                   if V.item_at(gh.get(V.repo_of(other)), V.num_of(other), t0)[0] in CROSS_OPEN})


EXPLICIT_SOURCES = frozenset({IK.SOURCE_KNOWN, IK.SOURCE_OWNER, IK.SOURCE_TAG})   # 리포가 글자로 적힌 언급(rework #412-1)


def key_mention_sources(key: str, mids: Iterable[str], listing: Mapping[str, Mapping[str, Any]],
                        aliases: Mapping[str, str], rules: str) -> Set[str]:
    """줄 키의 근거 언급 출처 집합: 그 키를 언급한 기억마다 문장별 `key_hits_for` 의 source, item 태그로만 나온 키는 tag."""
    out: Set[str] = set()
    for mid in mids:
        mem = listing.get(mid)
        if mem is None:
            continue
        tags = [t for t in (mem.get("tags") or []) if isinstance(t, str)]
        other = [t for t in tags if not t.strip().lower().startswith("item:")]
        for sent in IR.split_sentences(mem.get("text") or ""):
            out |= {h.source for h in IR.key_hits_for(sent, other, aliases, rules) if IR.key_text(h.key) == key}
        if key in {IR.key_text(k) for k in IK.extract_keys("", [t for t in tags if t not in other], aliases)}:
            out.add(IK.SOURCE_TAG)
    return out


def other_key_in_sentence(cand: Tuple[str, str, str], listing: Mapping[str, Mapping[str, Any]],
                          aliases: Mapping[str, str], rules: str) -> bool:
    """후보(줄 키, 다른 키, 기억)의 다른 키가 줄 키와 같은 문장에 키로 나오는가."""
    key, other, mid = cand
    mem = listing.get(mid) or {}
    tags = [t for t in (mem.get("tags") or []) if isinstance(t, str) and not t.strip().lower().startswith("item:")]
    for sent in IR.split_sentences(mem.get("text") or ""):
        ks = {IR.key_text(h.key) for h in IR.key_hits_for(sent, tags, aliases, rules)}
        if key in ks and other in ks:
            return True
    return False


def cross_rules(key_rules: str) -> str:
    """교차 충돌 재계산 규칙: 저장소가 v2·v2.1 이면 그 규칙, v1 이면 v2.1."""
    return key_rules if key_rules in STORE_KEY_RULES else IR.KEY_RULES_V21


class TableGate:
    """표마다 over-max 줄·교차 충돌 줄을 세는 준비물(저장소 근거·최대 번호·t0 기억 키·나열 본문). `wanted` 로 t0 캐시에 받을
    다른 리포 번호를 더하고, 캐시를 받은 뒤 `check` 로 센다."""

    def __init__(self, store: str, aliases: Mapping[str, str], listing: Mapping[str, Mapping[str, Any]], t0: datetime):
        self.store_github = IR.load_github(store)
        self.max_numbers = OT.repo_max_numbers(self.store_github)
        view = V.store_view(store, t0, github_mode=OT.GITHUB_IGNORE)
        self.view = view
        self.mem_keys = {mid: set(roles) for mid, roles in view.mem_roles.items()}
        self.rules = cross_rules(view.key_rules)
        self.aliases, self.listing, self.t0 = aliases, listing, t0
        self._cands: Dict[int, Tuple[List[Tuple[str, str, str]], int]] = {}

    def candidates(self, tab: Optional[Mapping[str, Any]]) -> Tuple[List[Tuple[str, str, str]], int]:
        if id(tab) not in self._cands:
            self._cands[id(tab)] = cross_candidates(tab, self.mem_keys, self.listing, self.aliases, self.rules)
        return self._cands[id(tab)]

    def wanted(self, tabs: Iterable[Optional[Mapping[str, Any]]]) -> Dict[str, Set[int]]:
        out: Dict[str, Set[int]] = defaultdict(set)
        for tab in tabs:
            for _, other, _ in self.candidates(tab)[0]:
                out[V.repo_of(other)].add(V.num_of(other))
        return out

    def check(self, tab: Optional[Mapping[str, Any]], gh: Mapping[str, Any]) -> Dict[str, Any]:
        """over-max 줄, 교차 충돌 줄 두 정의(rework #412-1): `cross_conflict_rows_all` = 원래 정의,
        `cross_conflict_rows` = 좁힌 정의(관문용, 줄 키 근거 언급이 전부 명시 접두면 제외). 분해는 `cross_detail`."""
        cands, missing = self.candidates(tab)
        over = over_max_rows(tab, self.store_github, self.max_numbers)
        cross_all = cross_conflict_rows(cands, gh, self.t0)
        by_key: Dict[str, List[str]] = defaultdict(list)
        for mid, keys in self.mem_keys.items():
            for k in keys:
                by_key[k].append(mid)
        src: Dict[str, Set[str]] = {k: key_mention_sources(k, sorted(by_key.get(k) or ()), self.listing, self.aliases,
                                                           self.rules) for k in cross_all}
        narrow = [k for k in cross_all if src[k] - EXPLICIT_SOURCES]
        pairs = [c for c in cands if c[0] in set(cross_all)
                 and V.item_at(gh.get(V.repo_of(c[1])), V.num_of(c[1]), self.t0)[0] in CROSS_OPEN]
        in_sent = {k: any(other_key_in_sentence(c, self.listing, self.aliases, self.rules) for c in pairs if c[0] == k)
                   for k in cross_all}
        detail = {"row_sources": dict(sorted(Counter("explicit-only" if not (src[k] - EXPLICIT_SOURCES) else "mixed"
                                                     for k in cross_all).items())),
                  "source_codes": dict(sorted(Counter(s for k in cross_all for s in src[k]).items())),
                  "other_key_in_sentence": dict(sorted(Counter("yes" if in_sent[k] else "no"
                                                               for k in cross_all).items()))}
        return {"over_max_rows": len(over), "cross_conflict_rows": len(narrow),
                "cross_conflict_rows_all": len(cross_all), "cross_detail": detail, "cross_candidates": len(cands),
                "missing_bodies": missing, "_over_max_keys": over, "_cross_keys": narrow, "_cross_keys_all": cross_all,
                "_cross_pairs": [list(c) for c in pairs], "_cross_sources": {k: sorted(v) for k, v in src.items()}}


def load_listing(paths: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """`--listing <본문 JSONL> <메타 JSONL>`(사이드카 `--save-listing` 모양) -> 기억 id 별 본문·태그."""
    return V._sidecar().offline_listing(paths[0], paths[1])


def _private(d: Mapping[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in d.items() if not k.startswith("_")}


def _norm_owners(owners: Mapping[str, str], aliases: Mapping[str, str]) -> Dict[str, str]:
    table = IK._table(aliases)
    return {IK._canonical(k, table) or k.strip().lower(): v.strip() for k, v in owners.items()}


def fresh_cache(path: str, t0: datetime, wanted: Mapping[str, Set[int]], owners: Mapping[str, str],
                default_owner: Optional[str], offline: bool, fetch=V.fetch_repo, fill=V.fill_gaps
                ) -> Tuple[Dict[str, Any], int]:
    """t0 평가 캐시(R7-0 캐시 재사용 금지): `path` 가 없으면 새로 받는다(GET). 있으면 같은 t0 로 받은 것만 읽고 빠진 리포를
    채운다. 리포마다 소유자 = 소유자 표, 없으면 기본 소유자(캐시 레코드 소유자가 다르면 거부). 리포 `fetched_at` 이 t0 보다
    이르면 거부(t0 시점 상태를 못 본다). (캐시 문서, 이번 호출 수)."""
    t0_text = _utc_text(t0)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        if doc.get("t0") != t0_text:
            raise Fail("evaluation cache was fetched for another t0")
    else:
        doc = {"t0": t0_text, "repos": {}}
    repos = doc.setdefault("repos", {})
    before = sum(r.get("calls", 0) for r in repos.values())
    try:
        for repo in sorted(wanted):
            owner = owners.get(repo) or default_owner
            if not owner:
                raise Fail("no owner for a repo")
            if repo in repos and repos[repo].get("owner") != owner:
                raise Fail("cached repo owner differs from the owner table")
            if repo not in repos:
                if offline:
                    raise Fail("offline run but a repo is missing from the t0 cache")
                repos[repo] = fetch(owner, repo)
            if not offline:
                fill(repos[repo], repo, set(wanted[repo]))
    finally:
        doc["calls_total"] = sum(r.get("calls", 0) for r in repos.values())
        V.write_private(path, json.dumps(doc, sort_keys=True, ensure_ascii=False))
    for repo in wanted:
        at = parse_instant(repos[repo].get("fetched_at"))
        if at is None or at < t0:
            raise Fail("evaluation cache repo was fetched before t0")
    return doc, doc["calls_total"] - before


def _read(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def cmd_smoke_recall(args, call=None, now: Optional[datetime] = None) -> int:
    """게이트웨이 recall 읽기만(MCP `recall` 도구 하나). 질의마다 `--out-dir/<이름>.json`(0600). t0 = 요청 시각(초 단위,
    `--t0` 로 고정 가능)이 모든 질의의 query_timestamp 다. 운영 대상은 `--allow-prod --read-only` 둘 다 있어야 한다."""
    from bench import r5_metrics as R
    queries = []
    for name, p in args.query:
        with open(p, encoding="utf-8") as fh:
            queries.append((name, fh.read().strip()))
    if len({n for n, _ in queries}) != len(queries):
        raise Fail("duplicate --query name")
    outs = {n: os.path.join(args.out_dir, "%s.json" % n) for n, _ in queries}
    if any(os.path.exists(p) for p in outs.values()):
        raise Fail("an output already exists in --out-dir")
    target_kind = "test"
    if call is None:
        from bench import arch_v2_regression as A
        env = dict(os.environ)
        try:
            target = A.resolve_target(env, args.allow_prod, args.read_only)
        except A.Refused as e:
            raise Fail(str(e))
        print(target.line(), file=sys.stderr)

        class _Ns:
            bank = None
            owner = None
        ns = _Ns()
        ns.bank = A.resolve_bank(ns, env)
        gw, _ = A.build_clients(target, ns, env)
        call, target_kind = gw.call, target.kind
    t0 = parse_instant(args.t0) if args.t0 else (now or datetime.now(timezone.utc)).replace(microsecond=0)
    t0_text = _utc_text(t0)
    os.makedirs(args.out_dir, mode=0o700, exist_ok=True)
    errors = 0
    for name, text in queries:
        doc = E.recall_doc(call, None, [{"qid": name, "scope": name, "text": text}], t0_text, args.max_tokens)
        for r in doc["runs"]:
            r["query"] = text
        doc = dict(doc, stage=SMOKE_STAGE, t0=t0_text, t=t0_day(t0), name=name, query_sha256=E.sha256_text(text),
                   target=target_kind)
        sha = R.write_private(outs[name], doc)
        errors += doc["totals"]["errors"]
        print(json.dumps({"name": name, "t0": t0_text, **doc["totals"], "sha256": sha}))
    return 1 if errors else 0


def attach_offline(doc: Mapping[str, Any], store: str, aliases: Mapping[str, str], knobs: Mapping[str, Any]
                   ) -> Dict[str, Any]:
    """저장 recall 의 질의마다 게이트웨이 순서(`render_table`)로 표를 다시 만들어 `open_table`(payload 모양)에 싣는다.
    읽는 시각 = t0(= query_timestamp), 결과 행 키를 넘긴다(#402). 응답에 이미 표가 있으면 거부(S3 켜진 응답에 덧씌우지 않는다)."""
    t0 = t0_instant(doc.get("query_timestamp"))
    out = json.loads(json.dumps(doc))
    for r in out.get("runs") or []:
        if r.get("open_table") is not None:
            raise Fail("the saved recall already has an open table")
        if not isinstance(r.get("query"), str):
            raise Fail("saved recall run has no query text")
        qargs = E.query_args(None, r["query"], doc["query_timestamp"], 0)
        table, why = render_table(OT, qargs, r.get("results") or [], store, aliases, knobs, now=t0)
        r["attach_reason"] = why
        if table is not None:
            pt = OT.payload_table(table)
            r["open_table"] = pt
            r["open_table_sha256"] = E.sha256_text(pt["text"])
            r["open_table_unknown_reasons"] = dict(Counter(x[3] for x in table["rows"] if x[1] == OT.ROW_UNKNOWN))
            r["open_table_over_max"] = int(table.get("over_max") or 0)     # #412: 게이트웨이 감사 over-max 와 같은 수
    out["totals"] = dict(out.get("totals") or {}, tables=sum(1 for r in out.get("runs") or [] if r.get("open_table")))
    return out


def cmd_smoke_attach(args) -> int:
    guard_paths([args.recall, args.store])
    aliases = V.load_aliases(args.aliases)
    if os.path.exists(args.out):
        raise Fail("--out exists")
    knobs = knobs_stage_a(OT.GITHUB_USE)
    if args.knobs:
        with open(args.knobs, encoding="utf-8") as fh:
            knobs, warnings = OT.parse_knobs(fh.read())
        if warnings:
            raise Fail("knobs file warnings %s" % warnings)
    doc = _read(args.recall)
    out = attach_offline(doc, args.store, aliases, knobs)
    out["attached_offline"] = {"source_recall_sha256": V.file_sha256(args.recall),
                               "store_sha256": V.file_sha256(args.store), "knobs_sha256": OT._canon_sha256(knobs),
                               "aliases_canonical_sha256": OT.aliases_sha256(aliases),
                               "module_sha256": V.file_sha256(OT.__file__)}
    sha = V.write_private(args.out, json.dumps(out, sort_keys=True, ensure_ascii=False, indent=1))
    for r in out["runs"]:
        tab = r.get("open_table") or {}
        print(json.dumps({"q": r.get("q"), "name": out.get("name"), "reason": r["attach_reason"],
                          "rows": len(table_lines(tab)) if tab else 0, "counts": tab.get("counts"),
                          "unknown_reasons": r.get("open_table_unknown_reasons"), "fetched_at": tab.get("fetched_at"),
                          "table_sha256": r.get("open_table_sha256")}, ensure_ascii=False))
    print(json.dumps({"out_sha256": sha, "knobs_sha256": out["attached_offline"]["knobs_sha256"]}))
    return 0


def cmd_smoke_dllm(args, caller=None, generate=None) -> int:
    """연막 d-llm: 저장(표 부착) recall 하나 -> K 생성. 기준일 = t0 KST 날짜, 머리에 t0. 지출 전에 t0·표·칸을 검사한다."""
    from bench import d_llm as D
    from bench import r5_metrics as R
    from bench import r5_roles as X
    try:
        guard_paths([args.recall, args.gen_out])
        t0 = t0_instant(args.t0)
        if os.path.exists(args.gen_out):
            raise Fail("--gen-out exists")
        doc = _read(args.recall)
        bad = t0_check(t0, doc) + smoke_fields(doc, None)
        if bad:
            raise Fail("recall input refused: %s" % ",".join(bad))
        day = t0_day(t0)
        b = E.build_input(D, D.load_prompt(D.BASELINE_PROMPT), doc, day, args.max_input_chars)
        head = {"label": args.label, "stage": SMOKE_STAGE, "arm": "on", "t": day, "t0": _utc_text(t0),
                "model": D.MODEL, "k": args.k, "recall_sha256": V.file_sha256(args.recall),
                "prompt_sha256": V.file_sha256(D.BASELINE_PROMPT), "prompt_text_sha256": E.sha256_text(b["prompt"]),
                "table_sha256": b["table"]["sha256"], "table_texts": len(b["table"]["texts"]),
                "table_attached_queries": b["table"]["attached"], "filter": b["filter"],
                "input_rows": len(b["asm"]["rows"]), "truncated_rows": b["asm"]["truncated_rows"],
                "input_chars": b["asm"]["chars"], "input_ids": [r["id"] for r in b["asm"]["rows"]],
                "max_input_chars": args.max_input_chars, "started_kst": D.now_kst()}
        guard = X.SpendGuard(args.spent_dir, args.max_usd, os.path.dirname(os.path.abspath(args.gen_out)))
        if guard.total() > args.soft_usd:
            print("stop: spent %.3f > soft-usd %s; not started" % (guard.total(), args.soft_usd), file=sys.stderr)
            return 4
        print(json.dumps({x: v for x, v in head.items() if x != "input_ids"}, ensure_ascii=False))
        rc = X.run_setting([{"t": day, "out": args.gen_out, "head": head, "prompt": b["prompt"]}],
                           caller or D.claude_caller(D.MODEL), guard, args.k, generate=generate,
                           write=R.write_private)
        if os.path.exists(args.gen_out):
            g = _read(args.gen_out)
            c = R.gen_cost(g)
            print(json.dumps({"exit": rc, "valid": sum(1 for x in g["generations"] if x.get("valid")),
                              "generations": len(g["generations"]), "cost_usd": round(c["cost_usd"], 4),
                              "spent_total_usd": round(guard.total(), 4), "gen_sha256": V.file_sha256(args.gen_out)}))
        return rc
    except (Fail, V.Fail, E.Fail) as e:
        print("r7_endtoend: %s" % e, file=sys.stderr)
        return 1


def _wanted_keys(gen: Mapping[str, Any], docs: Sequence[Mapping[str, Any]], aliases: Mapping[str, str],
                 key_rules: Optional[str]) -> Dict[str, Set[int]]:
    """새 캐시로 받을 리포·번호: 유효 생성의 열린·residual 항목 키 + 표 줄 키 + 결과 행 키(모든 저장 recall)."""
    wanted: Dict[str, Set[int]] = defaultdict(set)

    def add(k: Optional[str]) -> None:
        if k:
            wanted[V.repo_of(k)].add(V.num_of(k))
    for g in gen.get("generations") or []:
        if g.get("valid"):
            for x in g.get("items") or []:
                if x.get("status") in ("open", "residual"):
                    add(item_key(x.get("key"), aliases))
    for d in docs:
        for r in d.get("runs") or []:
            for k, _, _ in table_lines(r.get("open_table")):
                add(k)
            for k in result_keys(r.get("results") or [], aliases, key_rules):
                add(k)
    return wanted


def cmd_smoke(args, fetch=None, fill=None) -> int:
    """연막 채점. 요약(`--work/<label>-smoke.json`, 수치·코드·sha256)은 실패여도 쓴다. exit 0 통과 / 1 실패 / 4 관문.
    GitHub 호출은 `--gh-timeout` 초 시한(넘기면 실패, issue #414)."""
    fetch, fill = timed_github(getattr(args, "gh_timeout", V.GH_TIMEOUT_S), fetch, fill)
    work = os.path.abspath(args.work)
    out_path = os.path.join(work, "%s-smoke.json" % args.label)
    summary: Dict[str, Any] = {"fail": []}
    try:
        guard_paths([args.gen, args.recall, args.store, args.aliases, args.github_owners, *(args.table_recall or []),
                     *args.listing])
        t0 = t0_instant(args.t0)
        aliases = V.load_aliases(args.aliases)
        doc, gen = _read(args.recall), _read(args.gen)
        extra = [(os.path.basename(p), _read(p)) for p in args.table_recall or []]
        summary["inputs"] = {"t0": _utc_text(t0), "t0_kst_day": t0_day(t0), "recall_sha256": V.file_sha256(args.recall),
                             "table_sha256": E.tables_of(doc)["sha256"] if doc.get("runs") else None,
                             "gen_sha256": V.file_sha256(args.gen), "gen_k": gen.get("k"),
                             "prompt_text_sha256": gen.get("prompt_text_sha256"),
                             "store_sha256": V.file_sha256(args.store), "owners_sha256": V.file_sha256(args.github_owners),
                             "aliases_canonical_sha256": OT.aliases_sha256(aliases),
                             "table_recall_sha256": {n: V.file_sha256(p) for (n, _), p in
                                                     zip(extra, args.table_recall or [])}}
        summary["t0_check"] = {"request": doc.get("query_timestamp"), "prompt_day": gen.get("t"),
                               "gen_t0": gen.get("t0"), "score": _utc_text(t0)}
        fail = t0_check(t0, doc, gen) + smoke_fields(doc, gen)
        if gen.get("recall_sha256") != summary["inputs"]["recall_sha256"]:
            fail.append("recall-sha")
        if "no-table" not in fail:
            fail += check_prompt(gen, doc)
        for n, d in extra:
            fail += ["%s:%s" % (n, c) for c in t0_check(t0, d)]
        if fail:
            summary["fail"] = fail
            return 1
        key_rules = OT.read_store(args.store, 60.0, None).key_rules
        owners = _norm_owners(_read(args.github_owners), aliases)
        wanted = _wanted_keys(gen, [doc] + [d for _, d in extra], aliases, key_rules)
        tg = TableGate(args.store, aliases, load_listing(args.listing), t0)
        named = [("symptom", doc)] + extra
        all_tabs = [r.get("open_table") for _, d in named for r in d.get("runs") or [] if r.get("open_table")]
        for repo, nums in tg.wanted(all_tabs).items():
            wanted[repo] |= nums
        summary["inputs"]["listing_sha256"] = [V.file_sha256(p) for p in args.listing]
        gh_doc, calls = fresh_cache(os.path.join(work, "%s-github.json" % args.label), t0, wanted, owners,
                                    args.github_default_owner, args.offline, fetch=fetch, fill=fill)
        gh = gh_doc["repos"]
        fetched = sorted(r.get("fetched_at") for r in gh.values() if r.get("fetched_at"))
        summary["github"] = {"calls_this_run": calls, "calls_total": gh_doc.get("calls_total"), "repos": len(wanted),
                             "repo_status": dict(sorted(Counter(str((gh.get(r) or {}).get("status"))
                                                                for r in wanted).items())),
                             "fetched_first": fetched[0] if fetched else None,
                             "fetched_last": fetched[-1] if fetched else None,
                             "fetched_after_t0": all((parse_instant(x) or t0) >= t0 for x in fetched),
                             "cache_sha256": V.file_sha256(os.path.join(work, "%s-github.json" % args.label))}
        view = tg.view
        rows = response_rows(doc)
        tables, private_tables = check_tables(named, tg, gh, t0)
        main = [v for name, v in tables.items() if name == "symptom" or name.startswith("symptom#")]
        over = sum(v.get("over_max_rows", 0) for v in main)
        cross = sum(v.get("cross_conflict_rows", 0) for v in main)
        min_valid = args.min_valid if args.min_valid is not None else int(gen.get("k") or 0)
        res = smoke_score(gen["generations"], aliases=aliases, gh=gh, t0=t0, bank=view.states, rows=rows,
                          mems=recall_memories(doc), min_valid=min_valid, max_numbers=tg.max_numbers,
                          over_max_rows=over, cross_conflict_rows=cross)
        main_names = [name for name in tables if name == "symptom" or name.startswith("symptom#")]
        res["disclosure"].update(                   # rework #412-1: 원래 정의 값과 분해는 공개 칸
            cross_conflict_rows_all=sum(tables[n].get("cross_conflict_rows_all", 0) for n in main_names),
            cross_detail={n: tables[n].get("cross_detail") for n in main_names if tables[n].get("attached")})
        summary.update(gate=res["gate"], disclosure=res["disclosure"],
                       reference=dict(res["reference"], min_valid=min_valid), decomposition=res["decomposition"])
        summary["tables"] = tables
        summary["fail"] = res["fail"] + missing_bodies_fail(tables)
        V.write_private(os.path.join(work, "%s-smoke-details.json" % args.label), json.dumps(
            {"false_open_keys": res["_false_keys"], "residual_finished_keys": res["_residual_keys"],
             "missing_number_keys": res["_missing_keys"], "tables": private_tables,
             "details": res["_details"]}, sort_keys=True, ensure_ascii=False))
        if summary["fail"]:
            return 1
        return EXIT_ROLLBACK if res["gate"]["verdict"] == "rollback" else 0
    except (Fail, V.Fail, OT.StoreError) as e:
        summary["fail"] = summary.get("fail", []) + ["error:" + str(e)]
        print("r7_endtoend: %s" % e, file=sys.stderr)
        return 1
    except BaseException as e:                     # rework #412-2: 예상 밖 예외·봉인도 요약을 pass 로 남기지 않는다
        summary["fail"] = summary.get("fail", []) + ["error:" + type(e).__name__]
        raise
    finally:
        if summary["fail"]:                         # 실패 조건도 롤백 범주(#412): exit 1 이어도 gate.verdict rollback
            g0 = summary.get("gate") or {}
            rb = g0.get("rollback") or {}
            summary["gate"] = dict(g0, **gate_of((g0.get("false_open") or {}).get("by_reason") or {},
                                                 rb.get("over_max_rows", 0), rb.get("cross_conflict_rows", 0),
                                                 summary["fail"]))
        sha = V.write_private(out_path, json.dumps(summary, sort_keys=True, ensure_ascii=False, indent=1))
        g, ref = summary.get("gate") or {}, summary.get("reference") or {}
        fo = g.get("false_open") or {}
        print(json.dumps({"fail": summary["fail"], "verdict": g.get("verdict"), "rollback": g.get("rollback"),
                          "disclosure": summary.get("disclosure"), "false_open": fo.get("total"),
                          "false_in_table": fo.get("in_table"), "false_out_of_table": fo.get("out_of_table"),
                          "P_key": ref.get("P_key"), "P_all": ref.get("P_all"), "keyless": ref.get("keyless"),
                          "open_pr_items": ref.get("open_pr_items"), "residual_finished": ref.get("residual_finished"),
                          "valid_reps": ref.get("valid_reps"), "github_calls": (summary.get("github") or {}).get(
                              "calls_this_run"), "summary_sha256": sha}, ensure_ascii=False))


TABLE_MIN_PRECISION = 0.95       # 표마다 `열림 후보` 정밀도 문턱(issue #414, 코디네이터 결정: 표마다 적용, 같으면 통과)
ACCURACY_CATS = ("candidate_precision_low", "closed_errors", "not_planned_errors")


def _count(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool) and x >= 0


def table_accuracy(tables: Mapping[str, Dict[str, Any]], min_precision: float = TABLE_MIN_PRECISION
                   ) -> Tuple[Dict[str, int], List[str]]:
    """부착 표마다 표 정확도 판정(issue #414, 상위 #393 checkpoint (3)·(4)) -> (롤백 범주, 실패 코드). 표 칸에 `accuracy` 를 단다.
    롤백: `열림 후보` GitHub 연결 키 정밀도(`linked_precision`, checkpoint (3) 정의, rework #414-1) < min_precision(표 수),
    `확인된 종결` 오류 > 0(줄 수 합), `종결(미계획)` 오류 > 0(줄 수 합). 전체 정밀도는 공개 칸 `precision_all`.
    실패(헛통과 방지): 칸이 없거나 수가 아님 -> `missing:<칸>:<표>`, 연결 열림 후보 0줄(빈 분모) ->
    `empty:candidate.linked:<표>`, 표 줄 파싱 수 != counts 합 -> `counts-mismatch:<표>`(rework #414-1).
    미부착 표는 판정하지 않는다(이미 `no-table:` 실패)."""
    cats = dict.fromkeys(ACCURACY_CATS, 0)
    fail: List[str] = []
    for name, v in tables.items():
        if not v.get("attached"):
            continue
        bad: List[str] = []
        why: List[str] = []
        cand = v.get("candidate")
        p = cand.get("linked_precision") if isinstance(cand, Mapping) else None
        if not isinstance(cand, Mapping):
            bad.append("missing:candidate")
        elif not _count(cand.get("linked_rows")):
            bad.append("missing:candidate.linked_rows")
        elif cand["linked_rows"] == 0:
            bad.append("empty:candidate.linked")
        elif isinstance(p, bool) or not isinstance(p, (int, float)):
            bad.append("missing:candidate.linked_precision")
        elif p < min_precision:
            why.append("candidate_precision_low")
        pm = v.get("parsed_matches_counts")
        if not isinstance(pm, bool):
            bad.append("missing:parsed_matches_counts")
        elif not pm:
            bad.append("counts-mismatch")
        errs: Dict[str, Any] = {}
        for f in ("closed_errors", "not_planned_errors"):
            x = v.get(f)
            errs[f] = x
            if not _count(x):
                bad.append("missing:" + f)
            elif x:
                why.append(f)
        if not bad:
            cats["candidate_precision_low"] += int("candidate_precision_low" in why)
            for f in ("closed_errors", "not_planned_errors"):
                cats[f] += errs[f]
        cd = cand if isinstance(cand, Mapping) else {}
        v["accuracy"] = {"verdict": "fail" if bad else "rollback" if why else "pass", "precision": p,
                         "min_precision": min_precision, "candidate_rows": cd.get("linked_rows"),
                         "precision_all": cd.get("precision"), "candidate_rows_all": cd.get("rows"),
                         "parsed_matches_counts": pm, **errs, "reasons": why, "fail": bad}
        fail += ["%s:%s" % (c, name) for c in bad]
    return cats, fail


def timed_github(timeout_s: float, fetch=None, fill=None):
    """기본 수집 함수(`V.fetch_repo`·`V.fill_gaps`)에 gh 시한을 묶는다(issue #414). 주입한 가짜는 그대로 쓴다."""
    get = functools.partial(V.gh_paged, timeout_s=timeout_s)
    return (fetch or functools.partial(V.fetch_repo, get=get)), (fill or functools.partial(V.fill_gaps, get=get))


def missing_bodies_fail(tables: Mapping[str, Mapping[str, Any]]) -> List[str]:
    """근거 기억 본문이 나열에 없으면 실패(rework #412-2): 교차 충돌 후보·출처 집합에서 조용히 빠지지 않게."""
    n = sum(int(v.get("missing_bodies") or 0) for v in tables.values())
    return ["missing-bodies:%d" % n] if n else []


def check_tables(named: Sequence[Tuple[str, Mapping[str, Any]]], tg: TableGate, gh: Mapping[str, Any], t0: datetime
                 ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """저장 recall 들의 부착 표마다 `table_check` + over-max 줄 + 교차 충돌 줄 -> (요약 칸, 리포 밖 키 목록)."""
    tables: Dict[str, Any] = {}
    private: Dict[str, Any] = {}
    for n, d in named:
        for r in d.get("runs") or []:
            name = n if len(d.get("runs") or []) == 1 else "%s#%s" % (n, r.get("q"))
            tab = r.get("open_table")
            if not tab:
                tables[name] = {"attached": False, "reason": r.get("attach_reason")}
                continue
            g = tg.check(tab, gh)
            tables[name] = dict(table_check(tab, gh, t0), attached=True, **_private(g))
            private[name] = {k[1:]: v for k, v in g.items() if k.startswith("_")}
    return tables, private


def cmd_table_gate(args, fetch=None, fill=None) -> int:
    """결정적 검사(#412 목표 2, LLM 0): 저장 recall(표 부착)마다 표 키 전체 t0 대조 + 표 안 over-max 줄 + 교차 충돌 종결 줄 +
    표 정확도(`table_accuracy`, issue #414: 열림 후보 정밀도 95% 미만·확인된 종결 오류·미계획 오류, 표마다).
    t0 평가 캐시는 `smoke` 와 같은 규칙(`--work/<label>-github.json`, t0 뒤에 받은 것만). exit 0 = 롤백 범주 모두 0,
    4 = 하나라도 있음, 1 = 실패(t0 불일치·미부착·정확도 값 없음·빈 분모·gh 시한 초과 포함)."""
    fetch, fill = timed_github(getattr(args, "gh_timeout", V.GH_TIMEOUT_S), fetch, fill)
    work = os.path.abspath(args.work)
    summary: Dict[str, Any] = {"fail": []}
    out_path = os.path.join(work, "%s-tables.json" % args.label)
    try:
        guard_paths([*args.recall, args.store, args.aliases, args.github_owners, *args.listing])
        t0 = t0_instant(args.t0)
        aliases = V.load_aliases(args.aliases)
        named = [(os.path.basename(p).rsplit(".json", 1)[0], _read(p)) for p in args.recall]
        fail = []
        for n, d in named:
            fail += ["%s:%s" % (n, c) for c in t0_check(t0, d)]
        if fail:
            summary["fail"] = fail
            return 1
        key_rules = OT.read_store(args.store, 60.0, None).key_rules
        tg = TableGate(args.store, aliases, load_listing(args.listing), t0)
        tabs = [r.get("open_table") for _, d in named for r in d.get("runs") or [] if r.get("open_table")]
        wanted: Dict[str, Set[int]] = defaultdict(set)
        for tab in tabs:
            for k, _, _ in table_lines(tab):
                wanted[V.repo_of(k)].add(V.num_of(k))
        for repo, nums in tg.wanted(tabs).items():
            wanted[repo] |= nums
        gh_doc, calls = fresh_cache(os.path.join(work, "%s-github.json" % args.label), t0, wanted,
                                    _norm_owners(_read(args.github_owners), aliases), args.github_default_owner,
                                    args.offline, fetch=fetch, fill=fill)
        tables, private = check_tables(named, tg, gh_doc["repos"], t0)
        summary.update(inputs={"t0": _utc_text(t0), "store_sha256": V.file_sha256(args.store), "key_rules": key_rules,
                               "cross_rules": tg.rules, "listing_sha256": [V.file_sha256(p) for p in args.listing],
                               "recall_sha256": {n: V.file_sha256(p) for (n, _), p in zip(named, args.recall)},
                               "max_numbers_repos": len(tg.max_numbers)},
                       github={"calls_this_run": calls, "repos": len(wanted),
                               "cache_sha256": V.file_sha256(os.path.join(work, "%s-github.json" % args.label))},
                       tables=tables,
                       totals={k: sum(v.get(k, 0) for v in tables.values())
                               for k in ("over_max_rows", "cross_conflict_rows", "cross_conflict_rows_all",
                                         "missing_bodies")})
        unattached = [n for n, v in tables.items() if not v.get("attached")]
        acc, acc_fail = table_accuracy(tables)
        summary["table_accuracy"] = acc
        summary["fail"] = ["no-table:%s" % n for n in unattached] + missing_bodies_fail(tables) + acc_fail
        V.write_private(os.path.join(work, "%s-tables-details.json" % args.label),
                        json.dumps(private, sort_keys=True, ensure_ascii=False))
        if summary["fail"]:
            return 1
        return EXIT_ROLLBACK if (summary["totals"]["over_max_rows"] or summary["totals"]["cross_conflict_rows"]
                                 or any(acc.values())) else 0
    except (Fail, V.Fail, OT.StoreError) as e:
        summary["fail"] = summary.get("fail", []) + ["error:" + str(e)]
        print("r7_endtoend: %s" % e, file=sys.stderr)
        return 1
    except BaseException as e:                     # rework #412-2
        summary["fail"] = summary.get("fail", []) + ["error:" + type(e).__name__]
        raise
    finally:
        tot = summary.get("totals") or {}             # rework #412-2: 요약만 읽는 소비자용 판정
        summary["gate"] = gate_of({}, tot.get("over_max_rows", 0), tot.get("cross_conflict_rows", 0), summary["fail"],
                                  table_accuracy=summary.get("table_accuracy") or {})
        sha = V.write_private(out_path, json.dumps(summary, sort_keys=True, ensure_ascii=False, indent=1))
        print(json.dumps({"fail": summary["fail"], "verdict": summary["gate"]["verdict"], "totals": summary.get("totals"),
                          "rollback": summary["gate"]["rollback"],
                          "tables": {n: {k: v.get(k) for k in ("rows", "over_max_rows", "cross_conflict_rows",
                                                                "cross_conflict_rows_all", "cross_detail",
                                                                "cross_candidates", "missing_bodies", "closed_rows",
                                                                "not_planned_rows", "accuracy")}
                                     for n, v in (summary.get("tables") or {}).items()},
                          "github_calls": (summary.get("github") or {}).get("calls_this_run"),
                          "summary_sha256": sha}, ensure_ascii=False))


def owners_check(store: str, owners: Mapping[str, str], default_owner: Optional[str], aliases: Mapping[str, str],
                 get=V.gh_paged) -> Dict[str, Any]:
    """소유자 표 커버(저장소 리포 전체 대비)와 표에 없는 리포의 근거 상태(저장소 GitHub 테이블), 소유자 표 리포마다
    `repos/<owner>/<repo>` GET 으로 포크 여부, 포크면 `pulls?state=closed` GET 으로 그 소유자의 닫힌 PR 수. 이름은 `_names`."""
    S = V._sidecar()
    roles = IR.load_store(store)[2]
    repos = set(S.store_repos(roles))
    own = _norm_owners(owners, aliases)
    gh = IR.load_github(store) or {}
    gst = {r: v.get("status") for r, v in (gh.get("repos") or {}).items()}
    missing = sorted(repos - set(own))
    calls = 0
    forks: Dict[str, Any] = {}
    status: Counter = Counter()
    for repo in sorted(own):
        st, items, c = get("repos/%s/%s" % (own[repo], repo))
        calls += c
        status[st] += 1
        if st == 200 and items and isinstance(items[0], Mapping) and items[0].get("fork"):
            st2, prs, c2 = get("repos/%s/%s/pulls?state=closed&per_page=100" % (own[repo], repo))
            calls += c2
            forks[repo] = {"closed_prs": len(prs), "own_closed_prs": sum(
                1 for p in prs if ((p.get("user") or {}).get("login") or "").lower() == own[repo].lower()),
                "status": st2}
    return {"store_repos": len(repos), "owner_entries": len(own), "covered": len(repos & set(own)),
            "not_covered": len(missing), "not_covered_status": dict(sorted(Counter(gst.get(r) or "absent"
                                                                                  for r in missing).items())),
            "extra_owner_entries": len(set(own) - repos), "default_owner_set": bool(default_owner),
            "owner_repo_status": dict(sorted((str(k), v) for k, v in status.items())),
            "forks": len(forks), "forks_with_closed_pr": sum(1 for f in forks.values() if f["own_closed_prs"]),
            "fork_closed_prs": [forks[r]["own_closed_prs"] for r in sorted(forks)],
            "fork_closed_prs_any_author": [forks[r]["closed_prs"] for r in sorted(forks)], "gh_calls": calls,
            "_names": {"not_covered": missing, "forks": sorted(forks)}}


def cmd_owners_check(args, get=None) -> int:
    get = get or functools.partial(V.gh_paged, timeout_s=getattr(args, "gh_timeout", V.GH_TIMEOUT_S))   # issue #414
    guard_paths([args.store, args.owners])
    aliases = V.load_aliases(args.aliases)
    out = owners_check(args.store, _read(args.owners), args.default_owner, aliases, get=get)
    V.write_private(args.out + ".names.json", json.dumps(out.pop("_names"), sort_keys=True, ensure_ascii=False))
    sha = V.write_private(args.out, json.dumps(out, sort_keys=True, ensure_ascii=False, indent=1))
    print(json.dumps(dict(out, out_sha256=sha), ensure_ascii=False))
    return 0


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="r7_endtoend.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("score")
    sc.add_argument("--gen-dir")
    sc.add_argument("--gen", type=_kv, action="append", help="<arm>=<symptom gen file> (stage A, overrides --gen-dir)")
    sc.add_argument("--recalls", type=_kv, action="append", required=True, help="<arm>=<symptom recall dir>")
    sc.add_argument("--answer-key", action="append", required=True)
    sc.add_argument("--store", required=True)
    sc.add_argument("--aliases", required=True)
    sc.add_argument("--github-cache", required=True)
    sc.add_argument("--work", required=True)
    sc.add_argument("--offline", action="store_true")
    sc.add_argument("--t0", default=SYMPTOM_TS, help="reference instant (UTC); default the 10-28 symptom path")
    ag = sub.add_parser("add-github")
    ag.add_argument("--pair", nargs=2, action="append", required=True, metavar=("SRC", "OUT"))
    ag.add_argument("--aliases", required=True)
    ag.add_argument("--owners", required=True)
    ag.add_argument("--default-owner-file", required=True)
    ag.add_argument("--work", required=True)
    rd = sub.add_parser("render")
    rd.add_argument("--store", required=True)
    rd.add_argument("--recalls", action="append", required=True)
    rd.add_argument("--plan")
    rd.add_argument("--symptom-file")
    rd.add_argument("--aliases", required=True)
    rd.add_argument("--github-cache", required=True)
    rd.add_argument("--before-module", required=True)
    rd.add_argument("--work", required=True)
    rd.add_argument("--label")
    rd.add_argument("--offline", action="store_true")
    kn = sub.add_parser("knobs")
    kn.add_argument("--github", required=True, choices=(OT.GITHUB_USE, OT.GITHUB_IGNORE))
    kn.add_argument("--out", required=True)
    au = sub.add_parser("audit")
    au.add_argument("--log", required=True)
    au.add_argument("--windows", required=True)
    au.add_argument("--aliases", required=True)
    au.add_argument("--out", required=True)
    sk = sub.add_parser("store-check")
    sk.add_argument("--store", required=True)
    sk.add_argument("--at", help="UTC instant for the evidence age (default now)")
    sk.add_argument("--out", required=True)
    g3 = sub.add_parser("g3-tables")
    g3.add_argument("--run", required=True)
    g3.add_argument("--base", default="base")
    g3.add_argument("--cand", default="cand")
    g3.add_argument("--out", required=True)
    ds = sub.add_parser("dev-score")
    ds.add_argument("--gen", type=_kv, action="append", required=True, help="<arm>=<dev gen dir> (arms on, off)")
    ds.add_argument("--label", type=_kv, action="append", help="<arm>=<d-llm label> (default dev-<arm>)")
    ds.add_argument("--recalls", type=_kv, action="append", required=True, help="<arm>=<dev recall dir>")
    ds.add_argument("--t", action="append")
    ds.add_argument("--store", required=True)
    ds.add_argument("--aliases", required=True)
    ds.add_argument("--github-cache", required=True)
    ds.add_argument("--labelset")
    ds.add_argument("--work", required=True)
    ds.add_argument("--out-name", default="r7-b-dev-score")
    ds.add_argument("--offline", action="store_true")
    sr = sub.add_parser("smoke-recall")
    sr.add_argument("--query", type=_kv, action="append", required=True, help="<name>=<query text file>")
    sr.add_argument("--out-dir", required=True)
    sr.add_argument("--t0", help="fixed t0 (UTC); default the request time")
    sr.add_argument("--max-tokens", type=int, default=16384)
    sr.add_argument("--allow-prod", action="store_true")
    sr.add_argument("--read-only", action="store_true")
    sa = sub.add_parser("smoke-attach")
    sa.add_argument("--recall", required=True)
    sa.add_argument("--store", required=True)
    sa.add_argument("--aliases", required=True)
    sa.add_argument("--knobs", help="knobs file (default: stage A knobs, github_evidence use)")
    sa.add_argument("--out", required=True)
    sd = sub.add_parser("smoke-dllm")
    sd.add_argument("--recall", required=True)
    sd.add_argument("--t0", required=True)
    sd.add_argument("--k", type=int, default=3)
    sd.add_argument("--gen-out", required=True)
    sd.add_argument("--spent-dir", required=True)
    sd.add_argument("--max-usd", type=float, required=True)
    sd.add_argument("--soft-usd", type=float, required=True)
    sd.add_argument("--max-input-chars", type=int, default=200000)
    sd.add_argument("--label", default="smoke")
    sm = sub.add_parser("smoke")
    sm.add_argument("--t0", required=True)
    sm.add_argument("--gen", required=True)
    sm.add_argument("--recall", required=True, help="symptom recall with the attached table")
    sm.add_argument("--table-recall", action="append", help="other saved recalls whose tables are checked too")
    sm.add_argument("--store", required=True)
    sm.add_argument("--aliases", required=True)
    sm.add_argument("--github-owners", required=True)
    sm.add_argument("--github-default-owner", required=True)
    sm.add_argument("--work", required=True)
    sm.add_argument("--label", default="r7-4")
    sm.add_argument("--min-valid", type=int, help="valid generations needed (default K)")
    sm.add_argument("--listing", nargs=2, required=True, metavar=("BODIES", "META"),
                    help="saved bank listing (sidecar --save-listing) for the cross-conflict check")
    sm.add_argument("--offline", action="store_true")
    sm.add_argument("--gh-timeout", type=float, default=V.GH_TIMEOUT_S, help="seconds per gh api call (issue #414)")
    tg = sub.add_parser("table-gate")
    tg.add_argument("--t0", required=True)
    tg.add_argument("--recall", action="append", required=True, help="saved recall with the attached table")
    tg.add_argument("--store", required=True)
    tg.add_argument("--aliases", required=True)
    tg.add_argument("--listing", nargs=2, required=True, metavar=("BODIES", "META"))
    tg.add_argument("--github-owners", required=True)
    tg.add_argument("--github-default-owner", required=True)
    tg.add_argument("--work", required=True)
    tg.add_argument("--label", default="r7-6")
    tg.add_argument("--offline", action="store_true")
    tg.add_argument("--gh-timeout", type=float, default=V.GH_TIMEOUT_S, help="seconds per gh api call (issue #414)")
    oc = sub.add_parser("owners-check")
    oc.add_argument("--store", required=True)
    oc.add_argument("--owners", required=True)
    oc.add_argument("--default-owner", required=True)
    oc.add_argument("--aliases", required=True)
    oc.add_argument("--out", required=True)
    oc.add_argument("--gh-timeout", type=float, default=V.GH_TIMEOUT_S, help="seconds per gh api call (issue #414)")
    return ap.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        return {"dev-score": cmd_dev_score, "score": cmd_score, "add-github": cmd_add_github, "render": cmd_render, "knobs": cmd_knobs,
                "audit": cmd_audit, "store-check": cmd_store_check, "g3-tables": cmd_g3_tables,
                "smoke-recall": cmd_smoke_recall, "smoke-attach": cmd_smoke_attach, "smoke-dllm": cmd_smoke_dllm,
                "smoke": cmd_smoke, "table-gate": cmd_table_gate, "owners-check": cmd_owners_check}[args.cmd](args)
    except V.SealGuard as e:
        print("r7_endtoend: sealed guard: %s" % e, file=sys.stderr)
        return 2
    except (Fail, V.Fail) as e:
        print("r7_endtoend: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
