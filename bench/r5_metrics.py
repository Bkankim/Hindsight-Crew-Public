"""런 5 지표 2단·노이즈 바닥(R5-A2, issue #362, 상위 #360 "결정됨"의 "지표(R5-A2)"·"plan-review 20261101-194850 반영").

  r5_metrics.py plan   --labelset <jsonl> --families <json> --instruction <txt> --project-template <txt> --out <json>
                       [--extra-t YYYY-MM-DD ...]
  r5_metrics.py recall --plan <json> --out-dir <dir> [--t YYYY-MM-DD ...] [--label L]   (hc-next 사본만, 읽기만)
  r5_metrics.py search --labelset <jsonl> --aliases <json> [--policy <json>] --recalls <dir> [--recalls <dir> ...] --out <json>
  r5_metrics.py dllm   --labelset <jsonl> --aliases <json> [--policy <json>] --recalls <dir> --t <t> [--t ...]
                       --oracle none|o1|o2|o3|o3o1 [--meta <jsonl>] [--plan <json> --families <json> --tag-aliases <json>]
                       --k 3 --out-dir <dir> --label L [--max-usd X]
  r5_metrics.py judge  --labelset <jsonl> --aliases <json> [--policy <json>] --recalls <dir> --gen-dir <dir>
                       --base <label> [--base <label> ...] [--oracle <name>=<label> ...] [--meta <jsonl> --tag-aliases <json>]
                       --out <json>
  r5_metrics.py keyless --gen <gen json> [--gen ...] --answer-key <json> [--answer-key ...] --out <json>

원문(질의 문안·recall 저장·d-llm 출력·쌍별 지표)은 리포 밖 0600 파일에만 쓴다. 표준 출력에는 수치·sha256 만 낸다.

질의(#360 "지표"): t 마다 런 4 요청 가족 8개 문안(`--families`, 리포 밖) + 그 t 에 dev 양성·대조 쌍이 있는 프로젝트(키의
정규 repo)마다 상태 질의 1개(`--project-template`, 리포 밖, 자리표시 `{project}`). 고정 지시문(`--instruction`, 런 4 의
`r4-1-instruction.txt`)의 기준 날짜 2026-10-28 을 t 로 바꿔 질의 앞에 붙인다. `query_timestamp` = t 23:59:59 KST,
`max_tokens` = 16384(`bench/d_llm.py` 의 `save_recalls` 를 그대로 쓴다). 봉인 구간(라벨셋 dev 마지막 t 뒤 ~ 라벨 상한)의 t 는
질의하지 않는다(`check_not_sealed`). 라벨 상한 뒤의 t(키 없는 항목 기준선의 2026-10-28)는 요청 가족만 질의한다.

(키, t) 집계: t 의 모든 질의 결과를 질의 순 -> 순위 순으로 펼쳐 기억 id 중복을 처음 자리만 남긴 합집합 하나(`union_rows`).
그다음 시간여행 필터(`time_filter`): upstream recall 의 `query_timestamp` 는 필터가 아니라 기준 시각이라 `mentioned_at` 이
t 끝 시각(t 23:59:59.999999 KST, 라벨과 같은 경계) 뒤이거나 없는(읽을 수 없는) 행을 뺀다(관찰 포함). 검색 지표와 d-llm 입력이
같은 필터 뒤 합집합을 쓴다(d-llm 은 그 위에 런 4 입력 상한 200,000자를 적용한다, 잘린 행 수를 남긴다).
한계: 순위·결과 상한은 뱅크 전체 기준이라 미래 행이 과거 행을 밀어낼 수 있다(뺀 비율을 (키, t)·t 별로 보고한다).

(i) 검색 지표(LLM 없음, `t_signals`). 행의 키 = `gateway.item_keys.extract_keys`(본문 + 태그, 별칭 표 = 라벨셋과 같은
정책 ∪ 실험 표), 완료 계열 = 라벨 쪽 규칙 `r5_labels.is_done_family`(태그만). 게이트웨이 쪽 완료 판정(X1·S1)은 import 하지 않는다.
  * completion_row: 그 키를 말하는 완료 계열 행이 있다.
  * stale_open: 그 키를 말하는 완료 계열이 아닌 행(낡은 열림 행)이 있다.
  * s1_note: 그 키를 말하는 행의 구조 필드 `gateway_item_ledger`(S1 인식 계약, `[키, 상태, 날짜, 근거 id]` 목록)에 상태 done 인
    그 키가 있다. 본문 주석 문안은 보지 않는다(문안·위치 손잡이가 바뀌어도 지표 불변).
  * 상태: 키가 결과에 없으면 absent, stale_open 이고 completion_row·s1_note 둘 다 없으면 open(recall 경로 열림), 그 밖은 closed.
  * G1_recall = 기준선 결과에 키가 나온 dev 양성 쌍 중 (처치) 결과에서 open 인 비율.
    G2_recall = 기준선 결과에 나온 dev 대조 쌍 중 (처치) 결과에도 나오고 완료 표시(completion_row 또는 s1_note)가 없는 비율.
    기준선끼리 비교(노이즈 바닥)는 첫 반복을 기준선으로 둔다.
(ii) 종단 지표(d-llm): t 의 필터 뒤 합집합으로 동결 기준선 프롬프트(`bench/prompts/d_llm_baseline.txt`, query_date = t)를
  `claude-sonnet-5-5` 로 K 회. 대응은 동결 `bench/d_llm_match.py`(키 일치 1순위)를 import 해 쓴다. 작업 표 = 그 t 의 dev 양성·
  대조 키(key = 정규 키 + 별칭 표기, keyword = 정규 키). 반복이 출력 실패(invalid)거나 빈 목록이면 그 반복은 모든 키에 invalid.
  쌍 상태: open 반복이 과반(K//2+1)이면 open, 아니고 open + invalid 가 과반이면 invalid(별도 칸, 분모에 남는다), 그 밖은 not_open.
  G1_dllm = 기준선 결과(필터 뒤)에 키가 나온 양성 쌍 중 open 비율, G2_dllm = 기준선 결과에 나온 대조 쌍 중 open 비율.
노이즈 바닥: 키 단위 군집 재표집(`cluster_bootstrap`, 같은 키의 여러 t 를 한 군집으로 복원 추출, 씨앗 20261101). 반복 짝마다
  |ΔG| 의 재표집 95 백분위를 내고 그 최댓값을 바닥으로 둔다(`noise_floor`). 관측 최대 |ΔG| 도 함께 낸다.
주 지표 P·R(#362 범위 확장, 운영자 결정, G1 은 보조): d-llm 쌍 상태(과반)로
  P(목록 정밀도) = open 으로 나온 dev 라벨 쌍(양성·대조) 중 대조(실제 열림) 비율(목표 >= 95%),
  R(열린 항목 회수율 = G2) = 기준선 결과에 나온 대조 쌍 중 open 비율(목표 >= 90%). 대조 쌍 전부를 분모로 한 R_all 도 낸다.
  라벨 밖 키(라벨셋에 없는 작업)를 말하는 열림 목록 항목은 P 에 들지 않는다(`judge_t` 의 open_unmapped 로 따로 센다).
  노이즈 바닥은 P 를 (분자, 분모) 쌍 값으로 같은 키 군집 재표집에 넣어 잰다.
R_bank(`r_bank`, #362 코디네이터 결정): 분모를 뱅크 상태(o3 표 규칙)가 열림인 대조 쌍으로 좁힌 R, R_bank2 는 완료 뒤 비완료
  언급이 있으면 열림으로 보는 느슨한 판본. `judge --meta --tag-aliases` 일 때만 낸다.
R 원인(`r_causes`): (b) 대조 쌍마다 필터 뒤 결과에 그 키의 열림 근거 행(완료 계열 아닌 행)이 있는가 / 완료 계열 행만 / absent,
  (a) 열림 근거 행이 입력에 있는데 d-llm 이 open 으로 내지 않은 쌍. (c) 뱅크 기억 판독은 사람(워커) 판독이라 코드 밖이다.
오라클(A4, `bench/r5_oracle.py`): 같은 필터 뒤 합집합을 o1(낡은 행에 완료 근거 주석) / o2(낡은 열림 행 제거)로 고쳐 d-llm 을
  다시 돌리고, 기준선 d-llm 오답(양성 쌍 open) 중 not_open 이 된 비율을 낸다(`oracle_reduction`). 둘 다 50% 미만이면 멈춤.
  o3(뱅크 상태 표)·o3o1(표 + o1 주석)은 recall 행 뒤에 표를 붙인다(`build_prompt_with_table`, 질의 범위 = `query_projects`).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import time as _time
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:                      # `python bench/r5_metrics.py` 로 실행해도 gateway·bench 를 찾게
    sys.path.insert(0, REPO)

from bench import r5_labels as L  # noqa: E402  (라벨 쪽 규칙: 완료 계열·시각·별칭 표)
from gateway.item_keys import extract_keys  # noqa: E402  (키 정의만)

KST = L.KST
SEED = 20261101
BOOT_N = 2000
K_DEFAULT = 3
MAX_INPUT_CHARS = 200000
MAX_TOKENS = 16384
LEDGER_FIELD = "gateway_item_ledger"
REF_DATE = "2026-10-28"                       # 런 4 고정 지시문의 기준 날짜(t 로 바꾼다)
GATE_PCT = 50.0
LABELED_PAIRS = (L.LABEL_POSITIVE, L.LABEL_CONTROL)

Pair = Tuple[str, str]                        # (키 텍스트 "<repo>#<n>", t "YYYY-MM-DD")


class Fail(Exception):
    pass


# --- 공용 -------------------------------------------------------------------------------------------

def write_private(path: str, obj: Any) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        if isinstance(obj, str):
            f.write(obj)
        else:
            json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.chmod(path, 0o600)
    return sha256_file(path)


def sha256_file(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def read_jsonl(path: str) -> List[Dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def query_timestamp(t: date) -> str:
    """t 23:59:59 KST 를 UTC Z 로(#360 "query_timestamp = t 의 23:59:59 KST")."""
    return datetime.combine(t, time(23, 59, 59), tzinfo=KST).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _day(t: Any) -> date:
    return t if isinstance(t, date) else date.fromisoformat(str(t))


# --- 합집합과 시간여행 필터 ----------------------------------------------------------------------------

def union_rows(stored: Mapping[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """질의 순(q, 같은 q 면 max_tokens 오름차순) -> 순위 순, 기억 id 중복은 처음 자리만. id 없는 행은 넣지 않고 센다
    (`bench/d_llm.py` assemble 과 같은 규칙)."""
    runs = sorted(stored.get("runs") or [], key=lambda r: (r.get("q", 0), r.get("max_tokens", 0)))
    seen: Set[str] = set()
    rows: List[Dict[str, Any]] = []
    source = no_id = 0
    for r in runs:
        for row in r.get("results") or []:
            source += 1
            rid = row.get("id")
            if not rid:
                no_id += 1
                continue
            if rid in seen:
                continue
            seen.add(rid)
            rows.append(row)
    return rows, {"source_rows": source, "no_id_rows": no_id, "union_rows": len(rows)}


def time_filter(rows: Sequence[Mapping[str, Any]], t: Any) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """mentioned_at > t 끝 시각(t 23:59:59.999999 KST) 이거나 없는(읽을 수 없는) 행을 뺀다. 관찰도 같다."""
    end = L.t_end(_day(t))
    kept, dropped, no_time = [], 0, 0
    for row in rows:
        at = L.parse_instant(row.get("mentioned_at"))
        if at is None:
            dropped += 1
            no_time += 1
            continue
        if at > end:
            dropped += 1
            continue
        kept.append(dict(row) if not isinstance(row, dict) else row)
    return kept, {"rows": len(rows), "dropped": dropped, "dropped_no_time": no_time, "kept": len(kept)}


def t_input(stored: Mapping[str, Any], t: Any) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """t 의 저장 recall -> 합집합 -> 시간여행 필터. 검색 지표와 d-llm 입력이 같은 결과를 쓴다."""
    rows, st = union_rows(stored)
    kept, fst = time_filter(rows, t)
    return kept, dict(st, **fst)


# --- 행 신호 ----------------------------------------------------------------------------------------

def _text(row: Mapping[str, Any]) -> str:
    v = row.get("text")
    if not isinstance(v, str):
        v = row.get("content")
    return v if isinstance(v, str) else ""


def row_keys(row: Mapping[str, Any], aliases: Mapping[str, str]) -> Set[str]:
    tags = row.get("tags") if isinstance(row.get("tags"), list) else []
    return {L.key_text(k) for k in extract_keys(_text(row), [t for t in tags if isinstance(t, str)], aliases)}


def row_is_completion(row: Mapping[str, Any]) -> bool:
    return L.is_done_family(row.get("tags"))


def ledger_done_keys(row: Mapping[str, Any], aliases: Mapping[str, str]) -> Set[str]:
    """S1 구조 필드에서 상태 done 인 키(별칭 표로 정규화). 모양이 아닌 칸은 무시한다."""
    field = row.get(LEDGER_FIELD)
    out: Set[str] = set()
    if not isinstance(field, list):
        return out
    for ent in field:
        if not isinstance(ent, (list, tuple)) or len(ent) < 2 or ent[1] != "done" or not isinstance(ent[0], str):
            continue
        repo, sep, n = ent[0].strip().rpartition("#")
        if not sep or not repo or not n.isdigit():
            continue
        out.add("%s#%d" % (L.canonical_repo(repo, aliases), int(n)))
    return out


def _state(present: bool, completion_row: bool, stale_open: bool, s1_note: bool) -> str:
    if not present:
        return "absent"
    if stale_open and not (completion_row or s1_note):
        return "open"
    return "closed"


def t_signals(rows: Sequence[Mapping[str, Any]], keys: Iterable[str],
              aliases: Mapping[str, str]) -> Dict[str, Dict[str, Any]]:
    """한 t 의 (필터 뒤) 결과 행 -> 요청 키마다 신호와 상태."""
    want = set(keys)
    acc = {k: {"present": False, "completion_row": False, "stale_open": False, "s1_note": False} for k in want}
    for row in rows:
        ks = row_keys(row, aliases) & want
        if not ks:
            continue
        done = row_is_completion(row)
        notes = ledger_done_keys(row, aliases)
        for k in ks:
            a = acc[k]
            a["present"] = True
            if done:
                a["completion_row"] = True
            else:
                a["stale_open"] = True
            if k in notes:
                a["s1_note"] = True
    return {k: dict(a, state=_state(**a)) for k, a in acc.items()}


# --- 라벨셋과 쌍 --------------------------------------------------------------------------------------

def dev_pairs(labelset: Sequence[Mapping[str, Any]]) -> Tuple[Set[Pair], Set[Pair]]:
    """dev 분할의 양성·대조 쌍만. 봉인·미사용·데이터층·대조 기억 없음은 지표 밖."""
    pos, ctl = set(), set()
    for r in labelset:
        if r.get("split") != "dev":
            continue
        if r.get("label") == L.LABEL_POSITIVE:
            pos.add((r["key"], r["t"]))
        elif r.get("label") == L.LABEL_CONTROL:
            ctl.add((r["key"], r["t"]))
    return pos, ctl


def keys_at(pairs: Iterable[Pair], t: str) -> List[str]:
    return sorted(k for k, tt in pairs if tt == t)


def search_signals(recalls: Mapping[str, Sequence[Mapping[str, Any]]], labelset: Sequence[Mapping[str, Any]],
                   aliases: Mapping[str, str]) -> Dict[Pair, Dict[str, Any]]:
    """{t: 필터 뒤 행} -> dev 양성·대조 쌍마다 신호."""
    pos, ctl = dev_pairs(labelset)
    out: Dict[Pair, Dict[str, Any]] = {}
    for t, rows in recalls.items():
        keys = keys_at(pos | ctl, t)
        if not keys:
            continue
        for k, s in t_signals(rows, keys, aliases).items():
            out[(k, t)] = s
    return out


# --- G1 / G2 ----------------------------------------------------------------------------------------

def g1_values(base: Mapping[Pair, Mapping[str, Any]], treat: Mapping[Pair, Mapping[str, Any]],
              positives: Iterable[Pair]) -> Dict[Pair, int]:
    """분모 = 기준선 결과에 키가 나온 양성 쌍. 값 = 처치 결과에서 recall 경로 열림이면 1."""
    out = {}
    for p in positives:
        if not (base.get(p) or {}).get("present"):
            continue
        out[p] = 1 if (treat.get(p) or {}).get("state") == "open" else 0
    return out


def g2_values(base: Mapping[Pair, Mapping[str, Any]], treat: Mapping[Pair, Mapping[str, Any]],
              controls: Iterable[Pair]) -> Dict[Pair, int]:
    """분모 = 기준선 결과에 나온 대조 쌍. 값 = 처치 결과에도 나오고 완료 표시(완료 행·S1 주석)가 없으면 1."""
    out = {}
    for p in controls:
        if not (base.get(p) or {}).get("present"):
            continue
        s = treat.get(p) or {}
        out[p] = 1 if s.get("present") and not (s.get("completion_row") or s.get("s1_note")) else 0
    return out


def rate(values: Mapping[Pair, int]) -> Optional[float]:
    return (sum(values.values()) / len(values)) if values else None


# --- 키 군집 재표집 ----------------------------------------------------------------------------------

def _pctl(sorted_vals: Sequence[float], q: float) -> float:
    """가장 가까운 순위 백분위(d_llm_stats 와 같은 방식)."""
    if not sorted_vals:
        return 0.0
    idx = max(0, min(len(sorted_vals) - 1, int(round(q * len(sorted_vals) + 0.5)) - 1))
    return sorted_vals[idx]


def cluster_bootstrap(a: Mapping[Pair, int], b: Mapping[Pair, int], seed: int = SEED, n: int = BOOT_N,
                      keep_samples: bool = False) -> Dict[str, Any]:
    """짝 비교 rate(a) - rate(b). 같은 쌍 집합이어야 한다. 키를 군집으로 복원 추출(키 정렬 뒤 씨앗 고정)."""
    if set(a) != set(b):
        raise ValueError("cluster_bootstrap: a and b must cover the same pairs")
    def nd(v):                      # 값 = 0/1(분모 1) 또는 (분자, 분모) - 비율형 지표(P)용
        return (v[0], v[1]) if isinstance(v, (tuple, list)) else (v, 1)
    clusters: Dict[str, List[Tuple[int, int, int, int]]] = defaultdict(list)
    for p in sorted(a):
        clusters[p[0]].append(nd(a[p]) + nd(b[p]))
    keys = sorted(clusters)
    sums = {k: tuple(sum(x[i] for x in v) for i in range(4)) for k, v in clusters.items()}
    tot = [sum(s[i] for s in sums.values()) for i in range(4)]

    def ratio_diff(na, da, nb, db):
        return (na / da if da else 0.0) - (nb / db if db else 0.0)
    diff = ratio_diff(*tot)
    rng = random.Random(seed)
    samples = []
    dropped = 0
    for _ in range(n if keys else 0):
        acc = [0, 0, 0, 0]
        for _k in range(len(keys)):
            c = sums[keys[rng.randrange(len(keys))]]
            acc = [acc[i] + c[i] for i in range(4)]
        if acc[1] == 0 or acc[3] == 0:
            dropped += 1            # 분모 0 = 비율이 정의되지 않음. 0 으로 세지 않고 뺀다(final-review #362-1)
            continue
        samples.append(ratio_diff(*acc))
    ss = sorted(samples)
    absd = sorted(abs(x) for x in samples)
    out = {"diff": diff, "pairs": len(a), "clusters": len(keys), "n": len(samples), "dropped_zero_den": dropped,
           "seed": seed,
           "lo": _pctl(ss, 0.025) if ss else diff, "hi": _pctl(ss, 0.975) if ss else diff,
           "abs_p95": _pctl(absd, 0.95) if absd else 0.0}
    out["lo"], out["hi"] = min(out["lo"], diff), max(out["hi"], diff)
    if keep_samples:
        out["samples"] = samples
    return out


def noise_floor(reps: Sequence[Mapping[Pair, int]], seed: int = SEED, n: int = BOOT_N) -> Dict[str, Any]:
    """반복 짝(i<j)마다 키 군집 재표집 |ΔG| 95 백분위, 그 최댓값 = 바닥. 관측 최대 |ΔG| 도 낸다."""
    pairs = []
    for i in range(len(reps)):
        for j in range(i + 1, len(reps)):
            r = cluster_bootstrap(reps[i], reps[j], seed=seed, n=n)
            pairs.append({"i": i + 1, "j": j + 1, "diff": r["diff"], "abs_p95": r["abs_p95"],
                          "lo": r["lo"], "hi": r["hi"], "dropped_zero_den": r["dropped_zero_den"]})
    return {"floor": max((p["abs_p95"] for p in pairs), default=0.0),
            "observed_max_abs": max((abs(p["diff"]) for p in pairs), default=0.0),
            "rates": [ratio(r) for r in reps], "pairs": pairs, "seed": seed, "n": n}


def ratio(values: Mapping[Pair, Any]) -> Optional[float]:
    """0/1 값이면 평균, (분자, 분모) 값이면 분자 합 / 분모 합."""
    num = den = 0
    for v in values.values():
        x, y = (v[0], v[1]) if isinstance(v, (tuple, list)) else (v, 1)
        num, den = num + x, den + y
    return (num / den) if den else None


# --- d-llm 판정 -------------------------------------------------------------------------------------

def dllm_tasks(keys: Iterable[str], aliases: Mapping[str, str]) -> List[Dict[str, Any]]:
    """작업 표: key = 정규 키 + 별칭 표기(`<별칭>#<n>`), keyword = 정규 키. 항목 이름은 쓰지 않는다."""
    rev: Dict[str, List[str]] = defaultdict(list)
    for al, canon in aliases.items():
        if al != canon:
            rev[canon].append(al)
    out = []
    for k in sorted(set(keys)):
        repo, n = L.parse_key(k)
        out.append({"id": k, "keys": [k] + ["%s#%d" % (al, n) for al in sorted(rev.get(repo, []))],
                    "keywords": [k], "project_hint": None})
    return out


def normalize_empty(gens: Sequence[Mapping[str, Any]]) -> Tuple[List[Mapping[str, Any]], int]:
    """유효하지만 빈 목록인 반복을 invalid 로 바꾼다(키 있는 경로와 키 없는 기준선 공통)."""
    norm: List[Mapping[str, Any]] = []
    empty = 0
    for g in gens:
        if g.get("valid") and not (g.get("items") or []):
            empty += 1
            norm.append({"valid": False, "items": []})
        else:
            norm.append(g)
    return norm, empty


def judge_t(gens: Sequence[Mapping[str, Any]], keys: Sequence[str], aliases: Mapping[str, str]) -> Dict[str, Any]:
    """K 반복 -> 키별 반복 상태(open/done/partial/absent/invalid). 출력 실패·빈 목록 반복은 invalid."""
    from bench import d_llm_match as M
    norm, empty = normalize_empty(gens)
    res = M.judge_runs(norm, dllm_tasks(keys, aliases))
    unmapped = [j["unmapped"] if j else None for j in res["runs"]]
    open_items, open_unmapped = [], []
    for g, j in zip(norm, res["runs"]):
        if not j:
            open_items.append(None)
            open_unmapped.append(None)
            continue
        flags = [it.get("status") == "open" for it in g.get("items") or []]
        open_items.append(sum(flags))
        open_unmapped.append(sum(1 for f, m in zip(flags, j["mapping"]) if f and m["task"] is None))
    return {"K": res["K"], "states": {k: v["states"] for k, v in res["tasks"].items()},
            "invalid_reps": sum(1 for g in gens if not g.get("valid")), "empty_reps": empty, "unmapped": unmapped,
            "open_items": open_items, "open_unmapped": open_unmapped}


def dllm_pair_state(states: Sequence[str]) -> str:
    need = len(states) // 2 + 1
    n_open = sum(1 for s in states if s == "open")
    n_inv = sum(1 for s in states if s == "invalid")
    if n_open >= need:
        return "open"
    if n_open + n_inv >= need:
        return "invalid"
    return "not_open"


def dllm_rates(pair_states: Mapping[Pair, str], pairs: Iterable[Pair], base_present: Iterable[Pair]) -> Dict[str, Any]:
    present = set(base_present)
    den = [p for p in pairs if p in present and p in pair_states]
    c = Counter(pair_states[p] for p in den)
    n = len(den)
    return {"den": n, "open": c["open"], "invalid": c["invalid"], "not_open": c["not_open"],
            "rate": (c["open"] / n) if n else None, "invalid_rate": (c["invalid"] / n) if n else None}


def oracle_reduction(base: Mapping[Pair, str], oracle: Mapping[Pair, str], positives: Iterable[Pair]) -> Dict[str, Any]:
    """기준선 d-llm 오답(양성 쌍 open) 중 오라클에서 not_open 이 된 비율(%). invalid 는 고쳐진 것으로 치지 않는다."""
    ps = [p for p in positives if p in base and p in oracle]
    wrong = [p for p in ps if base[p] == "open"]
    c = Counter(oracle[p] for p in wrong)
    return {"wrong": len(wrong), "fixed": c["not_open"], "still_open": c["open"], "invalid": c["invalid"],
            "pct": (100.0 * c["not_open"] / len(wrong)) if wrong else None,
            "new_open": sum(1 for p in ps if base[p] != "open" and oracle[p] == "open")}


def pr_values(pair_states: Mapping[Pair, str], positives: Iterable[Pair], controls: Iterable[Pair],
              present: Optional[Iterable[Pair]] = None) -> Dict[str, Dict[Pair, Any]]:
    """주 지표(#362 범위 확장, 운영자 결정).
    P(목록 정밀도) = 열림(open 과반)으로 나온 라벨 쌍 중 대조(실제 열림) 비율. 값 = (대조이고 open, open) 쌍별 (분자, 분모).
    R(열린 항목 회수율 = G2) = 대조 쌍 중 open 비율. `present` 를 주면 분모를 기준선 결과에 나온 대조 쌍으로 한정한다.
    invalid 쌍은 open 이 아니다(P 분모 밖, R 분모 안)."""
    pos, ctl = set(positives), set(controls)
    pv = {p: ((1 if (p in ctl and pair_states.get(p) == "open") else 0), (1 if pair_states.get(p) == "open" else 0))
          for p in sorted(pos | ctl) if p in pair_states}
    keep = set(present) if present is not None else None
    rv = {p: (1 if pair_states.get(p) == "open" else 0) for p in sorted(ctl)
          if p in pair_states and (keep is None or p in keep)}
    return {"P": pv, "R": rv}


def pr_summary(pair_states: Mapping[Pair, str], positives: Iterable[Pair], controls: Iterable[Pair],
               present: Iterable[Pair]) -> Dict[str, Any]:
    pos, ctl, pres = set(positives), set(controls), set(present)
    v = pr_values(pair_states, pos, ctl)
    va = pr_values(pair_states, pos, ctl, pres)
    p_num = sum(x for x, _ in v["P"].values())
    p_den = sum(y for _, y in v["P"].values())
    return {"P": ratio(v["P"]), "P_num": p_num, "P_den": p_den, "open_positive": p_den - p_num,
            "R": ratio(va["R"]), "R_num": sum(va["R"].values()), "R_den": len(va["R"]),
            "R_all": ratio(v["R"]), "R_all_num": sum(v["R"].values()), "R_all_den": len(v["R"])}


def r_causes(signals: Mapping[Pair, Mapping[str, Any]], pair_states: Mapping[Pair, str],
             controls: Iterable[Pair]) -> Dict[str, Any]:
    """R 손실 원인(대조 쌍). (b) 필터 뒤 recall 결과에 그 키의 열림 근거 행(완료 계열 아닌 행)이 있는가.
    (a) 열림 근거 행이 입력에 있는데 d-llm 목록에서 open 과반이 아닌 쌍. 나머지는 absent / 완료 계열 행만."""
    c = Counter()
    miss = []
    for p in sorted(controls):
        s = signals.get(p) or {}
        st = pair_states.get(p)
        if not s.get("present"):
            cls = "absent"
        elif s.get("stale_open"):
            cls = "open_row"
        else:
            cls = "completion_only"
        c[cls] += 1
        c[f"{cls}_dllm_open"] += 1 if st == "open" else 0
        if st != "open":
            miss.append(p)
    return {"controls": len(list(controls)) if not isinstance(controls, (set, list, tuple)) else len(controls),
            "b_open_row": c["open_row"], "b_completion_only": c["completion_only"], "b_absent": c["absent"],
            "a_open_row_missed": c["open_row"] - c["open_row_dllm_open"],
            "open_row_dllm_open": c["open_row_dllm_open"], "completion_only_dllm_open": c["completion_only_dllm_open"],
            "absent_dllm_open": c["absent_dllm_open"], "missed": len(miss)}


def r_bank(pair_states: Mapping[Pair, str], bank: Mapping[Pair, Mapping[str, Optional[str]]],
           controls: Iterable[Pair]) -> Dict[str, Any]:
    """R_bank(#362 코디네이터 결정): 분모 = 대조 쌍 중 뱅크 상태(o3 표와 같은 규칙, `r5_oracle.bank_states`)가 열림인 쌍.
    R_bank2 = 완료 계열 기억 뒤에 비완료 언급이 있으면 열림으로 보는 느슨한 판본. 값 = d-llm open 과반."""
    ctl = sorted(controls)
    out: Dict[str, Any] = {"bank_strict": dict(Counter((bank.get(p) or {}).get("strict") or "none" for p in ctl)),
                           "bank_loose": dict(Counter((bank.get(p) or {}).get("loose") or "none" for p in ctl))}
    for name, fld in (("R_bank", "strict"), ("R_bank2", "loose")):
        den = [p for p in ctl if (bank.get(p) or {}).get(fld) == "open"]
        num = sum(1 for p in den if pair_states.get(p) == "open")
        out.update({f"{name}_num": num, f"{name}_den": len(den), name: (num / len(den)) if den else None})
    return out


def gate_line(o1_pct: Optional[float], o2_pct: Optional[float]) -> str:
    def f(x):
        return "n/a" if x is None else f"{x:.1f}"
    ok = any(x is not None and x >= GATE_PCT for x in (o1_pct, o2_pct))
    return f"오라클 관문: o1 {f(o1_pct)}% / o2 {f(o2_pct)}% -> " + ("통과" if ok else "멈춤(둘 다 50% 미만)")


# --- 질의 계획과 봉인 가드 ------------------------------------------------------------------------------

def label_bounds(labelset: Sequence[Mapping[str, Any]]) -> Tuple[date, date]:
    """(dev 마지막 t, 라벨 상한 = 라벨셋 마지막 t + 격자 간격 - 1일)."""
    dev = [_day(r["t"]) for r in labelset if r.get("split") == "dev"]
    allt = [_day(r["t"]) for r in labelset]
    if not dev or not allt:
        raise Fail("labelset has no dev rows")
    return max(dev), max(allt) + timedelta(days=L.T_STEP_DAYS - 1)


def check_not_sealed(labelset: Sequence[Mapping[str, Any]], ts: Iterable[Any]) -> None:
    """dev 마지막 t 뒤 ~ 라벨 상한(봉인 구간과 간격 포함)의 t 는 거부한다. 라벨 상한 뒤(키 없는 기준선)는 된다."""
    last_dev, upper = label_bounds(labelset)
    bad = sorted(str(_day(t)) for t in ts if last_dev < _day(t) <= upper)
    if bad:
        raise Fail(f"refuse: t in sealed/gap window ({last_dev} < t <= {upper}): {','.join(bad)}")


def make_plan(labelset: Sequence[Mapping[str, Any]], families: Mapping[str, Any], instruction: str,
              project_template: str, extra_t: Sequence[str] = ()) -> Dict[str, Any]:
    if REF_DATE not in instruction:
        raise Fail(f"instruction has no reference date {REF_DATE}")
    if "{project}" not in project_template:
        raise Fail("project template needs {project}")
    pos, ctl = dev_pairs(labelset)
    projects: Dict[str, Set[str]] = defaultdict(set)
    for k, t in pos | ctl:
        projects[t].add(L.parse_key(k)[0])
    ts = sorted(projects) + sorted(set(extra_t) - set(projects))
    check_not_sealed(labelset, ts)
    fams = [{"qid": q["qid"], "scope": q.get("scope"), "text": q["text"]} for q in families["queries"]]
    per_t = {}
    for t in ts:
        qs = list(fams) + [{"qid": f"P:{p}", "scope": "project-status", "text": project_template.replace("{project}", p)}
                           for p in sorted(projects.get(t, ()))]
        per_t[t] = {"instruction": instruction.replace(REF_DATE, t), "query_timestamp": query_timestamp(_day(t)),
                    "queries": qs}
    return {"per_t": per_t, "families": len(fams), "max_tokens": MAX_TOKENS}


# --- 저장 recall 읽기 ---------------------------------------------------------------------------------

def recall_path(d: str, t: str) -> str:
    return os.path.join(d, f"t-{t}.json")


def load_recalls(d: str, ts: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    out = {}
    for t in ts:
        p = recall_path(d, t)
        if not os.path.exists(p):
            raise Fail(f"missing recall {os.path.basename(p)} in {d}")
        with open(p, encoding="utf-8") as f:
            doc = json.load(f)
        if (doc.get("totals") or {}).get("errors"):
            raise Fail(f"recall {os.path.basename(p)} has errors; not a measurement input")
        out[t] = doc
    return out


def aliases_from(args) -> Dict[str, str]:
    exp, _ = L.load_alias_table(args.aliases)
    pol, _ = L.policy_repos(getattr(args, "policy", None))
    merged, _ = L.merged_aliases(pol, exp)
    return merged


def _pk(p: Pair) -> str:
    return f"{p[0]}@{p[1]}"


# --- 명령 -------------------------------------------------------------------------------------------

def cmd_plan(args) -> int:
    labelset = read_jsonl(args.labelset)
    with open(args.families, encoding="utf-8") as f:
        fam = json.load(f)
    with open(args.instruction, encoding="utf-8") as f:
        instr = f.read()
    with open(args.project_template, encoding="utf-8") as f:
        ptpl = f.read().strip()
    plan = make_plan(labelset, fam, instr, ptpl, args.extra_t or ())
    plan.update(labelset_sha256=sha256_file(args.labelset), families_sha256=sha256_file(args.families),
                instruction_sha256=sha256_file(args.instruction), project_template_sha256=sha256_file(args.project_template))
    if os.path.exists(args.out):
        print(f"refuse: {args.out} exists", file=sys.stderr)
        return 1
    sha = write_private(args.out, plan)
    print(json.dumps({"t": len(plan["per_t"]), "queries": sum(len(v["queries"]) for v in plan["per_t"].values()),
                      "plan_sha256": sha}, ensure_ascii=False))
    return 0


def cmd_recall(args) -> int:
    from bench import arch_v2_regression as A
    from bench import d_llm as D
    env = dict(os.environ)
    try:
        target = A.resolve_target(env, False, False)
    except A.Refused as e:
        print(f"refuse: {e}", file=sys.stderr)
        return 2
    if target.kind != "next":
        print("refuse: recall save runs only against the copy", file=sys.stderr)
        return 2
    with open(args.plan, encoding="utf-8") as f:
        plan = json.load(f)
    ts = args.t or sorted(plan["per_t"])
    print(target.line(), file=sys.stderr)

    class _Args:
        bank = None
        owner = None
    ns = _Args()
    ns.bank = A.resolve_bank(ns, env)
    gw, _ = A.build_clients(target, ns, env)
    counter = D.tiktoken_counter()
    os.makedirs(args.out_dir, mode=0o700, exist_ok=True)
    errors = 0
    for t in ts:
        p = recall_path(args.out_dir, t)
        if os.path.exists(p):
            print(json.dumps({"t": t, "skip": "exists"}))
            continue
        spec = plan["per_t"][t]
        started = D.now_kst()
        doc = D.save_recalls(gw.call, spec["instruction"], spec["queries"], plan.get("max_tokens", MAX_TOKENS),
                             spec["query_timestamp"], counter)
        doc = dict(doc, label=args.label, t=t, plan_sha256=sha256_file(args.plan), started_kst=started,
                   finished_kst=D.now_kst(), target="next")
        sha = write_private(p, doc)
        errors += doc["totals"]["errors"]
        print(json.dumps({"t": t, **doc["totals"], "sha256": sha}, ensure_ascii=False))
    if errors:
        print(f"fail: {errors} recall error(s)", file=sys.stderr)
        return 1
    return 0


def _recall_sets(args, labelset) -> Tuple[List[str], List[Dict[str, List[Dict[str, Any]]]], List[Dict[str, Any]]]:
    pos, ctl = dev_pairs(labelset)
    ts = sorted({t for _, t in pos | ctl})
    sets, fstats = [], []
    for d in args.recalls:
        docs = load_recalls(d, ts)
        rows, st = {}, {}
        for t in ts:
            rows[t], st[t] = t_input(docs[t], t)
        sets.append(rows)
        fstats.append(st)
    return ts, sets, fstats


def filter_report(rows_by_t: Mapping[str, Sequence[Mapping[str, Any]]], st_by_t: Mapping[str, Mapping[str, int]],
                  raw_docs: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    tot = Counter()
    for st in st_by_t.values():
        tot.update(st)
    return {"per_t": {t: dict(st, dropped_rate=(st["dropped"] / st["rows"]) if st["rows"] else None)
                      for t, st in st_by_t.items()},
            "total": dict(tot, dropped_rate=(tot["dropped"] / tot["rows"]) if tot["rows"] else None)}


def pair_drop_rates(docs: Mapping[str, Mapping[str, Any]], pairs: Iterable[Pair],
                    aliases: Mapping[str, str]) -> Dict[Pair, Dict[str, int]]:
    """(키, t) 마다: 합집합에서 그 키를 말하는 행 수와 필터로 뺀 수."""
    by_t: Dict[str, List[str]] = defaultdict(list)
    for k, t in pairs:
        by_t[t].append(k)
    out = {}
    for t, ks in by_t.items():
        rows, _ = union_rows(docs[t])
        end = L.t_end(_day(t))
        cnt = {k: [0, 0] for k in ks}
        for row in rows:
            hit = row_keys(row, aliases) & set(ks)
            if not hit:
                continue
            at = L.parse_instant(row.get("mentioned_at"))
            dropped = at is None or at > end
            for k in hit:
                cnt[k][0] += 1
                cnt[k][1] += 1 if dropped else 0
        for k, (n, d) in cnt.items():
            out[(k, t)] = {"rows": n, "dropped": d}
    return out


def cmd_search(args) -> int:
    labelset = read_jsonl(args.labelset)
    aliases = aliases_from(args)
    pos, ctl = dev_pairs(labelset)
    ts, sets, fstats = _recall_sets(args, labelset)
    sigs = [search_signals(s, labelset, aliases) for s in sets]
    base = sigs[0]
    g1 = [g1_values(base, s, pos) for s in sigs]
    g2 = [g2_values(base, s, ctl) for s in sigs]
    absent = {"positive": sum(1 for p in pos if not base.get(p, {}).get("present")),
              "control": sum(1 for p in ctl if not base.get(p, {}).get("present"))}
    docs0 = load_recalls(args.recalls[0], ts)
    drops = pair_drop_rates(docs0, pos | ctl, aliases)
    pair_drop = [v["dropped"] / v["rows"] for v in drops.values() if v["rows"]]
    per_t = {}
    for t in ts:
        pp = [p for p in pos if p[1] == t]
        cc = [p for p in ctl if p[1] == t]
        per_t[t] = {"positive": len(pp), "control": len(cc),
                    "g1_den": sum(1 for p in pp if p in g1[0]), "g1_open": sum(g1[0].get(p, 0) for p in pp),
                    "g2_den": sum(1 for p in cc if p in g2[0]), "g2_kept": sum(g2[0].get(p, 0) for p in cc)}
    summary = {
        "reps": len(sets), "recalls": [os.path.basename(os.path.normpath(d)) for d in args.recalls],
        "labelset_sha256": sha256_file(args.labelset), "pairs": {"positive": len(pos), "control": len(ctl)},
        "absent_in_base": absent,
        "absent_rate": {"positive": absent["positive"] / len(pos) if pos else None,
                        "control": absent["control"] / len(ctl) if ctl else None},
        "g1_recall": [{"den": len(v), "open": sum(v.values()), "rate": rate(v)} for v in g1],
        "g2_recall": [{"den": len(v), "kept": sum(v.values()), "rate": rate(v)} for v in g2],
        "signals_base": {lab: dict(Counter(base[p]["state"] for p in ps if p in base))
                         for lab, ps in (("positive", pos), ("control", ctl))},
        "completion_row_base": sum(1 for p in pos if base.get(p, {}).get("completion_row")),
        "filter": [filter_report(s, st) for s, st in zip(sets, fstats)],
        "pair_dropped": {"pairs_with_rows": len(pair_drop), "pairs_any_dropped": sum(1 for x in pair_drop if x > 0),
                         "mean_rate": (sum(pair_drop) / len(pair_drop)) if pair_drop else None,
                         "max_rate": max(pair_drop) if pair_drop else None},
        "per_t": per_t,
    }
    if len(sets) > 1:
        summary["noise_recall"] = {"g1": noise_floor(g1, args.seed, args.boot), "g2": noise_floor(g2, args.seed, args.boot)}
    detail = {"signals": [{_pk(p): s for p, s in sorted(sg.items())} for sg in sigs],
              "pair_dropped": {_pk(p): v for p, v in sorted(drops.items())}}
    sha = write_private(args.out, dict(summary, detail=detail))
    print(json.dumps({k: summary[k] for k in ("reps", "absent_in_base", "g1_recall", "g2_recall")}, ensure_ascii=False))
    if "noise_recall" in summary:
        n = summary["noise_recall"]
        print(json.dumps({"noise_g1": {k: n["g1"][k] for k in ("floor", "observed_max_abs")},
                          "noise_g2": {k: n["g2"][k] for k in ("floor", "observed_max_abs")}}))
    print(json.dumps({"filter_total": summary["filter"][0]["total"], "out_sha256": sha}, ensure_ascii=False))
    return 0


def cmd_dllm(args) -> int:
    from bench import d_llm as D
    from bench import r5_oracle as O
    labelset = read_jsonl(args.labelset)
    check_not_sealed(labelset, args.t)
    aliases = aliases_from(args)
    pos, ctl = dev_pairs(labelset)
    meta = L.read_meta(args.meta) if args.meta else None
    if args.oracle in ("o1", "o3", "o3o1") and meta is None:
        print(f"refuse: {args.oracle} needs --meta", file=sys.stderr)
        return 1
    scopes: Dict[str, Set[str]] = {}
    tag_aliases: Dict[str, str] = {}
    counter = D.tiktoken_counter()
    if args.oracle in ("o3", "o3o1"):
        if not (args.plan and args.families and args.tag_aliases):
            print("refuse: o3 needs --plan --families --tag-aliases", file=sys.stderr)
            return 1
        scopes = query_projects(args.plan, args.families)
        tag_aliases = O.load_tag_aliases(args.tag_aliases)
    template = D.load_prompt(D.BASELINE_PROMPT)
    caller = D.claude_caller(D.MODEL)
    # 지출 한도는 같은 라벨의 이전 실행 출력까지 합산하고 유료 호출마다 확인한다(final-review #362 C2)
    spent = 0.0
    if os.path.isdir(args.out_dir):
        for name in os.listdir(args.out_dir):
            if name.startswith(f"{args.label}-") and name.endswith("-gen.json"):
                with open(os.path.join(args.out_dir, name), encoding="utf-8") as f:
                    spent += gen_cost(json.load(f))["cost_usd"]
    docs = load_recalls(args.recalls, args.t)
    for t in args.t:
        out = os.path.join(args.out_dir, f"{args.label}-{t}-gen.json")
        if os.path.exists(out):
            with open(out, encoding="utf-8") as f:
                prev = json.load(f)
            if not gen_complete(prev, args.k):
                # 중단된 실행의 부분 결과(반복 < K, finished_kst 없음)는 완료로 치지 않는다(final-review #362 C1)
                # 옮기라고 안내하지 않는다: 옮기면 그 파일의 지출이 한도 합산에서 빠진다(final-review #362-1)
                print(f"refuse: incomplete {os.path.basename(out)} ({len(prev.get('generations') or [])}/{args.k}); "
                      "it still counts toward --max-usd for this label; re-run under a new --label",
                      file=sys.stderr)
                return 1
            print(json.dumps({"t": t, "skip": "exists"}))
            continue
        if args.max_usd is not None and spent >= args.max_usd:
            print(f"stop: spent {spent:.3f} >= max-usd {args.max_usd}", file=sys.stderr)
            return 3
        rows, st = t_input(docs[t], t)
        tstats: Dict[str, Any] = {}
        if args.oracle in ("o1", "o3o1"):
            ev = O.completion_evidence(meta, keys_at(pos, t), _day(t))
            rows, tstats = O.o1(rows, set(keys_at(pos, t)), ev, aliases)
        elif args.oracle == "o2":
            rows, tstats = O.o2(rows, set(keys_at(pos, t)), set(keys_at(ctl, t)), aliases)
        asm = D.assemble({"runs": [{"q": 1, "max_tokens": 0, "results": rows}]}, max_input_chars=args.max_input_chars)
        table = ""
        if args.oracle in ("o3", "o3o1"):
            trows = O.status_table(meta, scopes.get(t, set()), _day(t), tag_aliases)
            table = O.table_text(trows, t)
            tstats = dict(tstats, table=O.table_stats(trows, table, counter), table_sha256=hashlib.sha256(table.encode()).hexdigest())
        prompt = build_prompt_with_table(D, template, asm, t, table)
        head = {"label": args.label, "t": t, "oracle": args.oracle, "model": D.MODEL, "k": args.k,
                "recall_sha256": sha256_file(recall_path(args.recalls, t)), "prompt_sha256": sha256_file(D.BASELINE_PROMPT),
                "prompt_text_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "filter": st, "oracle_stats": tstats,
                "input_rows": len(asm["rows"]), "truncated_rows": asm["truncated_rows"], "input_chars": asm["chars"],
                "input_ids": [r["id"] for r in asm["rows"]], "max_input_chars": args.max_input_chars,
                "started_kst": D.now_kst()}
        print(json.dumps({k: v for k, v in head.items() if k != "input_ids"}, ensure_ascii=False))
        gens = []
        for r in range(args.k):
            if args.max_usd is not None and spent >= args.max_usd:
                print(f"stop: spent {spent:.3f} >= max-usd {args.max_usd} at {t} rep {r + 1}; "
                      f"{os.path.basename(out)} left incomplete", file=sys.stderr)
                return 3
            g = D.generate_one(prompt, caller)
            gens.append(g)
            for c in g["calls"]:
                spent += c.get("cost_usd") or 0.0
                print(json.dumps({"t": t, "rep": r + 1, **{k: v for k, v in c.items() if k != "raw_result"}},
                                 ensure_ascii=False))
            write_private(out, dict(head, generations=gens))
        write_private(out, dict(head, generations=gens, finished_kst=D.now_kst()))
        print(json.dumps({"t": t, "spent_usd_cum": round(spent, 4), "out": os.path.basename(out)}))
    return 0


def query_projects(plan_path: str, families_path: str) -> Dict[str, Set[str]]:
    """t 별 질의 범위 project: 그 t 의 프로젝트 상태 질의(P:<repo>) + 요청 가족의 project 범위."""
    with open(plan_path, encoding="utf-8") as f:
        plan = json.load(f)
    with open(families_path, encoding="utf-8") as f:
        fam = {q.get("project") for q in json.load(f)["queries"] if q.get("project")}
    return {t: {q["qid"][2:] for q in spec["queries"] if str(q.get("qid", "")).startswith("P:")} | fam
            for t, spec in plan["per_t"].items()}


def build_prompt_with_table(D, template: str, asm: Mapping[str, Any], t: str, table: str) -> str:
    """표가 없으면 d_llm.build_prompt 와 같다. 있으면 recall 행 뒤에 줄바꿈 하나로 잇는다."""
    if not table:
        return D.build_prompt(template, asm, query_date=t)
    rows = "\n".join(D.render_row(r) for r in asm["rows"])
    return template.format(query_date=t, recall_rows=rows + "\n" + table)


def gen_cost(gen: Mapping[str, Any]) -> Dict[str, Any]:
    calls = [c for g in gen.get("generations") or [] for c in g.get("calls") or []]
    return {"calls": len(calls), "cost_usd": sum(c.get("cost_usd") or 0.0 for c in calls),
            "input_tokens": sum((c.get("input_tokens") or 0) + (c.get("cache_creation_input_tokens") or 0)
                                + (c.get("cache_read_input_tokens") or 0) for c in calls),
            "output_tokens": sum(c.get("output_tokens") or 0 for c in calls),
            "seconds": sum((c.get("duration_ms") or 0) for c in calls) / 1000.0}


def gen_complete(gen: Mapping[str, Any], k: Optional[int] = None) -> bool:
    """끝까지 쓴 출력인가: finished_kst 가 있고 반복 수가 K(주면 그 값, 아니면 머리의 k)와 같다."""
    want = k if k is not None else gen.get("k")
    n = len(gen.get("generations") or [])
    return bool(gen.get("finished_kst")) and (want is None or n == want)


def judge_label(gen_dir: str, label: str, ts: Sequence[str], pos: Set[Pair], ctl: Set[Pair],
                aliases: Mapping[str, str]) -> Tuple[Dict[Pair, str], Dict[str, Any]]:
    states: Dict[Pair, str] = {}
    cost = Counter()
    info = {}
    for t in ts:
        with open(os.path.join(gen_dir, f"{label}-{t}-gen.json"), encoding="utf-8") as f:
            gen = json.load(f)
        if not gen_complete(gen):
            raise Fail(f"incomplete gen {label}-{t} ({len(gen.get('generations') or [])}/{gen.get('k')}); not a measurement input")
        keys = keys_at(pos | ctl, t)
        res = judge_t(gen["generations"], keys, aliases)
        for k in keys:
            states[(k, t)] = dllm_pair_state(res["states"][k])
        c = gen_cost(gen)
        cost.update(c)
        info[t] = {"invalid_reps": res["invalid_reps"], "empty_reps": res["empty_reps"], "unmapped": res["unmapped"],
                   "open_items": res["open_items"], "open_unmapped": res["open_unmapped"],
                   "input_rows": gen.get("input_rows"), "truncated_rows": gen.get("truncated_rows"),
                   "oracle_stats": gen.get("oracle_stats"), "k": len(gen["generations"]), **c}
    return states, {"per_t": info, "cost": dict(cost)}


def cmd_judge(args) -> int:
    labelset = read_jsonl(args.labelset)
    aliases = aliases_from(args)
    pos, ctl = dev_pairs(labelset)
    ts = sorted(args.t)
    check_not_sealed(labelset, ts)
    docs = load_recalls(args.recalls, ts)
    present = set()
    for t in ts:
        rows, _ = t_input(docs[t], t)
        for k, s in t_signals(rows, keys_at(pos | ctl, t), aliases).items():
            if s["present"]:
                present.add((k, t))
    spos = {p for p in pos if p[1] in ts}
    sctl = {p for p in ctl if p[1] in ts}
    out: Dict[str, Any] = {"t": ts, "pairs": {"positive": len(spos), "control": len(sctl)},
                           "present": {"positive": len(spos & present), "control": len(sctl & present)},
                           "base": [], "oracle": {}}
    signals: Dict[Pair, Dict[str, Any]] = {}
    for t in ts:
        rows, _ = t_input(docs[t], t)
        for k, s in t_signals(rows, keys_at(sctl, t), aliases).items():
            signals[(k, t)] = s
    bank: Dict[Pair, Dict[str, Optional[str]]] = {}
    if args.meta and args.tag_aliases:
        from bench import r5_oracle as O      # o3 와 같은 뱅크 규칙(S1 쪽 완료 판정). 지표 모듈 자체는 gateway 완료 규칙을 import 하지 않는다
        meta = read_jsonl(args.meta)
        ta = O.load_tag_aliases(args.tag_aliases)
        for t in ts:
            for k, v in O.bank_states(meta, keys_at(sctl, t), _day(t), ta).items():
                bank[(k, t)] = v
    base_states = []
    for lab in args.base:
        st, info = judge_label(args.gen_dir, lab, ts, spos, sctl, aliases)
        base_states.append(st)
        out["base"].append({"label": lab, "g1": dllm_rates(st, spos, present), "g2": dllm_rates(st, sctl, present),
                            "pr": pr_summary(st, spos, sctl, present), "causes": r_causes(signals, st, sctl),
                            "r_bank": r_bank(st, bank, sctl) if bank else None, "info": info})
    if len(base_states) > 1:
        g1v = [{p: 1 if s[p] == "open" else 0 for p in spos & present} for s in base_states]
        g2v = [{p: 1 if s[p] == "open" else 0 for p in sctl & present} for s in base_states]
        out["noise_dllm"] = {"g1": noise_floor(g1v, args.seed, args.boot), "g2": noise_floor(g2v, args.seed, args.boot),
                             "P": noise_floor([pr_values(s, spos, sctl)["P"] for s in base_states], args.seed, args.boot),
                             "R": noise_floor([pr_values(s, spos, sctl, present)["R"] for s in base_states],
                                              args.seed, args.boot),
                             "R_all": noise_floor([pr_values(s, spos, sctl)["R"] for s in base_states],
                                                  args.seed, args.boot)}
    for spec in args.oracle or []:
        name, _, lab = spec.partition("=")
        st, info = judge_label(args.gen_dir, lab, ts, spos, sctl, aliases)
        red = oracle_reduction(base_states[0], st, spos & present)
        # 관문은 첫 기준선 기준. 기준선 반복마다의 감소율과 G1 짝 비교(키 군집 재표집)를 함께 남긴다(d-llm 반복 노이즈 확인용)
        by_base = [dict(oracle_reduction(b, st, spos & present), base=args.base[i]) for i, b in enumerate(base_states)]
        g1o = {p: 1 if st[p] == "open" else 0 for p in spos & present}
        g1b = {p: 1 if base_states[0][p] == "open" else 0 for p in spos & present}
        out["oracle"][name] = {"label": lab, "reduction": red, "reduction_by_base": by_base,
                               "paired_g1_vs_base1": cluster_bootstrap(g1o, g1b, args.seed, args.boot),
                               "g1": dllm_rates(st, spos, present), "g2": dllm_rates(st, sctl, present),
                               "pr": pr_summary(st, spos, sctl, present), "causes": r_causes(signals, st, sctl),
                               "r_bank": r_bank(st, bank, sctl) if bank else None,
                               "paired_P_vs_base1": cluster_bootstrap(pr_values(st, spos, sctl)["P"],
                                                                      pr_values(base_states[0], spos, sctl)["P"],
                                                                      args.seed, args.boot),
                               "paired_R_vs_base1": cluster_bootstrap(pr_values(st, spos, sctl, present)["R"],
                                                                      pr_values(base_states[0], spos, sctl, present)["R"],
                                                                      args.seed, args.boot),
                               "info": info}
    if "o1" in out["oracle"] or "o2" in out["oracle"]:
        out["gate"] = gate_line((out["oracle"].get("o1") or {}).get("reduction", {}).get("pct"),
                                (out["oracle"].get("o2") or {}).get("reduction", {}).get("pct"))
    out["states"] = {"base": [{_pk(p): s for p, s in sorted(st.items())} for st in base_states]}
    sha = write_private(args.out, out)
    brief = {"present": out["present"], "base": [{"label": b["label"], "g1": b["g1"], "g2": b["g2"], "pr": b["pr"],
                                                   "cost_usd": b["info"]["cost"].get("cost_usd")} for b in out["base"]],
             "oracle": {k: {"reduction": v["reduction"], "g2": v["g2"], "pr": v["pr"],
                            "cost_usd": v["info"]["cost"].get("cost_usd")} for k, v in out["oracle"].items()}}
    if "noise_dllm" in out:
        brief["noise_dllm"] = {g: {k: out["noise_dllm"][g][k] for k in ("floor", "observed_max_abs")}
                               for g in ("g1", "g2", "P", "R", "R_all")}
    if "gate" in out:
        brief["gate"] = out["gate"]
    print(json.dumps(brief, ensure_ascii=False))
    print(json.dumps({"out_sha256": sha}))
    return 0


def keyless_tasks(answer_key: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """세트 C·D 정답표에서 작업 키가 없는 끝난 일·대조군만(d-llm 작업 표 모양)."""
    from bench import d_llm as D
    from bench import d_llm_match as M
    return [t for t in D.tasks_from_answer_key(answer_key) if t["category"] in ("closed", "controls")
            and not M.task_keys(t)]


def keyless_states(gens: Sequence[Mapping[str, Any]], tasks: Sequence[Mapping[str, Any]]) -> Dict[str, str]:
    """키 없는 기준선의 항목별 쌍 상태. 빈 목록 반복 정규화는 키 있는 경로(`judge_t`)와 같다(final-review #362-1)."""
    from bench import d_llm_match as M
    norm, _ = normalize_empty(gens)
    res = M.judge_runs(norm, tasks)
    return {t["id"]: dllm_pair_state(res["tasks"][t["id"]]["states"]) for t in tasks}


def cmd_keyless(args) -> int:
    out: Dict[str, Any] = {"sets": {}}
    gens = []
    for p in args.gen:
        with open(p, encoding="utf-8") as f:
            gens.append(json.load(f))
    for ak in args.answer_key:
        with open(ak, encoding="utf-8") as f:
            doc = json.load(f)
        tasks = keyless_tasks(doc)
        per_gen = []
        for g in gens:
            st = keyless_states(g["generations"], tasks)
            closed = [t["id"] for t in tasks if t["category"] == "closed"]
            ctl = [t["id"] for t in tasks if t["category"] == "controls"]
            per_gen.append({"label": g.get("label"), "t": g.get("t"),
                            "closed": {"n": len(closed), "open": sum(1 for i in closed if st[i] == "open"),
                                       "invalid": sum(1 for i in closed if st[i] == "invalid")},
                            "controls": {"n": len(ctl), "open": sum(1 for i in ctl if st[i] == "open"),
                                         "invalid": sum(1 for i in ctl if st[i] == "invalid")},
                            "states": st, **gen_cost(g)})
            r = per_gen[-1]
            p_den = r["closed"]["open"] + r["controls"]["open"]
            r["P"] = (r["controls"]["open"] / p_den) if p_den else None
            r["R"] = (r["controls"]["open"] / r["controls"]["n"]) if r["controls"]["n"] else None
        out["sets"][os.path.basename(ak)] = {"answer_key_sha256": sha256_file(ak), "runs": per_gen}
    sha = write_private(args.out, out)
    print(json.dumps({k: [{kk: vv for kk, vv in r.items() if kk != "states"} for r in v["runs"]]
                      for k, v in out["sets"].items()}, ensure_ascii=False))
    print(json.dumps({"out_sha256": sha}))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="r5_metrics")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--labelset", required=True)
    p.add_argument("--families", required=True)
    p.add_argument("--instruction", required=True)
    p.add_argument("--project-template", required=True)
    p.add_argument("--extra-t", action="append")
    p.add_argument("--out", required=True)
    p = sub.add_parser("recall")
    p.add_argument("--plan", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--t", action="append")
    p.add_argument("--label", default="base")

    def common(p):
        p.add_argument("--labelset", required=True)
        p.add_argument("--aliases", required=True)
        p.add_argument("--policy")
        p.add_argument("--seed", type=int, default=SEED)
        p.add_argument("--boot", type=int, default=BOOT_N)
    p = sub.add_parser("search")
    common(p)
    p.add_argument("--recalls", action="append", required=True)
    p.add_argument("--out", required=True)
    p = sub.add_parser("dllm")
    common(p)
    p.add_argument("--recalls", required=True)
    p.add_argument("--t", action="append", required=True)
    p.add_argument("--oracle", choices=("none", "o1", "o2", "o3", "o3o1"), default="none")
    p.add_argument("--meta")
    p.add_argument("--plan")
    p.add_argument("--families")
    p.add_argument("--tag-aliases")
    p.add_argument("--k", type=int, default=K_DEFAULT)
    p.add_argument("--max-input-chars", type=int, default=MAX_INPUT_CHARS)
    p.add_argument("--max-usd", type=float)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--label", required=True)
    p = sub.add_parser("judge")
    common(p)
    p.add_argument("--recalls", required=True)
    p.add_argument("--t", action="append", required=True)
    p.add_argument("--gen-dir", required=True)
    p.add_argument("--meta")
    p.add_argument("--tag-aliases")
    p.add_argument("--base", action="append", required=True)
    p.add_argument("--oracle", action="append")
    p.add_argument("--out", required=True)
    p = sub.add_parser("keyless")
    p.add_argument("--gen", action="append", required=True)
    p.add_argument("--answer-key", action="append", required=True)
    p.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    try:
        return {"plan": cmd_plan, "recall": cmd_recall, "search": cmd_search, "dllm": cmd_dllm,
                "judge": cmd_judge, "keyless": cmd_keyless}[args.cmd](args)
    except Fail as e:
        print(str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
