"""recall 재생 하네스 - 판정기 (issue #13).

runner 산출물(calls.jsonl + results/ + prod_events.jsonl)을 읽어 기준 팔(baseline) 대비 후보 팔(candidate)을
세 축으로 판정한다. 표준 라이브러리만 쓰고 판정 함수는 순수 함수다(tests/test_bench_judge.py 가 고정한다).

채택 기준 (운영자 정본, #13): 속도·정확도·비용 3축. 하나라도 개선 + 나머지 불악화 = 합격. 트레이드오프 불허.
2026-09-23 재리뷰(Opus 5.5 3건)로 "판정 분기는 건전하지만 분기에 들어가는 정의가 트레이드오프를 흘린다"는
결함을 막았다: 허용 폭 고정, 꼬리 쿼리 단위, 비용 쿼리 단위 0 허용, 추가·확장은 보류, payload 전체 비교,
캐시(상태 의존 지연) 감지, 운영 간섭 시간 대조, 복제본 동결·rewrite 실패 확인, 종료 코드 분리.

축별 판정
  정확도  결과 불변으로만 증명한다(행동 평가는 판정력이 없다). 쿼리마다 기준/후보의 저장 payload 를 비교한다:
            identical      results·최상위 키·추가 블록 모두 동일
            lossless       id·순서 동일, 후보가 뺀 필드는 전부 "빈 값"이거나 선언된 파생 필드(DERIVABLE)이거나
                           run 의 bank 와 남은 필드로 다시 만들어지는 형식 파생 필드(FORMAT_DERIVABLE)
          보류(HOLD - 근거가 따로 필요):
            added          후보가 기준에 없던 필드를 더했다(임의 텍스트를 끼워 넣을 수 있다)
            extended       기준 목록이 후보 목록의 앞부분(후보가 더 준다) - 이슈의 허용 변경 목록에 없다
            prefix-cut     후보 목록이 기준 목록의 앞부분(꼬리 잘림) - 잘린 꼬리의 실사용 근거가 필요
          악화:
            lossy          복원 불가능한 필드가 빠지거나 값이 바뀜
            changed        그 밖(순서·구성 변경, 추가 text 블록 변경, structuredContent 가 content 와 어긋남)
            outcome-diff   ok/오류 분류가 다름(upstream-error 포함)
          기준 팔이 라운드 간 결과가 달라지는(비결정) 쿼리는 후보 관측이 기준 관측 집합 안에 있으면 불변으로 본다.
  비용    쿼리마다 토큰(o200k, 클라이언트가 받는 텍스트)의 라운드 중앙값을 기준 관측 범위[min, max]와 비교한다.
          결과가 결정적이면 토큰도 결정적이다(자기검증 3 run 에서 흔들림 0) - 그래서 허용 폭은 0 이다.
            악화  한 쿼리라도 기준 최대보다 많다 (합계가 줄어도 - 쿼리별 트레이드오프 불허)
            개선  악화 쿼리 없음 + 합계 감소
  속도    쿼리별 (후보/기준) 지연 비의 라운드 중앙값 -> 쿼리 전체의 기하평균 비. 쿼리 단위 부트스트랩 95% CI.
          허용 폭 m = SPEED_MARGIN(1%, 운영자 결정 2026-09-23) - 고정 상수다. A/A 대조로 넓히지 않는다
          (CI 가 이미 표본 오차를 담으므로 두 번 세는 셈이고, 대조 팔 이상치 하나로 부푼다 - 재리뷰 시뮬레이션).
          A/A 대조 CI 가 [1-m, 1+m] 밖으로 e 만큼 나가면(자리·이월 효과 등 CI 가 못 담는 계통 오차) 개선·불악화
          문턱을 e 만큼 좁힌다 - 더 엄격하게만 쓴다. e > m 이면 run 무효.
            개선      CI 상한 < 1 - m
            불악화    CI 상한 < 1 + m        (비열등성 증명 - "차이 없음"이 아니라 "나쁘지 않음"을 보인다)
            악화      CI 하한 > 1 + m
            미결      그 밖, 또는 쿼리 < MIN_QUERIES, 또는 아래 무효 조건
          꼬리: 쿼리별 비가 TAIL_Q_RATIO 를 넘는 쿼리가 max(TAIL_MIN_COUNT, TAIL_MAX_FRAC*n) 개를 넘으면 악화
                (전체 호출을 섞은 p95 는 원래 느린 쿼리가 지배해 일부 쿼리의 큰 악화를 숨긴다). 섞은 p95 비의
                CI 하한이 1 + 2m 을 넘어도 악화.
          상태 의존 지연(캐시): 팔마다 쿼리의 첫 라운드 지연과 이후 라운드 중앙값의 차 d 를 구해 후보-기준 차의
                CI 가 ±ORDER_MARGIN 밖이면 미결. 같은 쿼리를 되풀이하는 재생은 캐시 적중률을 실사용(~30%)보다
                부풀린다 - 캐시 후보는 전용 프로토콜(README)로 따로 판정한다.
          무효: 팔이 4개 이상(이월 효과 불균형), 운영 간섭 신호 없음.
          바퀴 집계(교차 run, issue #82): 바퀴 = 모든 팔이 모든 세트를 정확히 한 번씩 거치는 연속 단계 n개(n = 팔 수).
                쿼리마다 바퀴별 로그 비 평균(바퀴 안에서 오프셋 상쇄) -> 완전한 바퀴 3개 이상이면 바퀴 값들의 중앙값, 아니면 평균.
                단계가 빠진 바퀴만 그 쿼리에서 빼고, 완전한 바퀴가 하나도 없는 쿼리만 뺀다. 꼬리·섞은 p95 도 같은 바퀴 단위로 모은다.
                MIN_ROUNDS 는 완전한 바퀴(어느 쿼리에서든)에 속한 단계 수로 센다. meta.plan 이 없는 과거 교차 run 은 옛 규칙
                (전 단계 평균, 단계가 빠진 쿼리 제외, meta.rounds) 그대로 판정한다 - 판정 규칙을 사후에 바꾸지 않는다.
          계획 대비(교차 run, issue #77): meta.plan 이 있고 실제 단계가 유효 계획(마지막 축소 기록의 목표, 없으면 처음 계획)보다
                짧으면 plan-truncated 로 미결 - 사유 없이 줄인 run 은 합격도 악화도 말하지 않는다. meta.plan 이 없는 과거 run 은 건너뛴다.
                meta.plan 을 해석할 수 없으면(모르는 version, 필드 누락) plan-invalid 로 미결(KeyError 로 죽지 않는다).
          cold 요청(--cold-report, issue #21): bench/cold_replay analyze 의 쌍별 cold_p50·cold_p95 비를 같은 비열등
                판정으로 본다. cold 는 A/A 대조가 없어 허용 폭은 m 그대로다. 깨끗한 쌍 < 20 이거나 가를 수 없으면 보류,
                CI 하한이 1 + m 을 넘으면 악화, 후보의 recall 실패(재시도 소진 + 사이드카 오류) 수가 기준보다 많으면
                악화. 끊긴 run·idle 300s 미만(시험 실행)은 측정 조건 미충족이라 합격도 cold 악화도 말하지 않는다.
                cold 를 잴 수 없었으면 --cold-unmeasured <사유>: 합격·무변화를 보류로 내린다(warm_verdict 는 그대로 남김).
                셋 다 없으면 cold 를 잰 적이 없는 것이라 --cold-unmeasured no-cold-input 과 같다(합격 -> 보류).
                cold 를 면제하려면 --cold-exempt <사유>(issue #169): 최종 판정 = warm 판정이고 판정 JSON 에 cold.verdict=exempt·
                사유와 reasons 의 cold-exempt:<사유> 가 남는다. upstream 이 사이드카로 보내는 요청 모양(배치당 텍스트 수·요청 수)을
                바꾸지 않는 후보만 쓴다. 요청 모양을 바꾸는 후보는 cold 도 속도 축이라 쓰지 않는다.
                --cold-report·--cold-unmeasured·--cold-exempt 는 셋 중 하나만 받는다. 빈 사유도 "주어짐"으로 본다.

오염 제어: 쌍의 어느 호출이든 (a) 호출 직전 사이드카 큐가 비어 있지 않았거나 읽지 못했거나(None)
(b) 호출 구간이 운영의 리랭크 사용 요청 구간과 겹쳤거나(prod_events.jsonl) (c) 세션 재개 재시도였거나
(d) 호출 구간이 벤치 간섭 가드의 정지 구간(meta.guard.windows, issue #19)과 겹쳤으면 속도 표본에서 뺀다. 제외 비율이 MAX_CONTAMINATED 를 넘으면 속도는 미결.

run 무효(INCONCLUSIVE): 간섭 가드가 run 을 중단함(guard-aborted), 복제본 동결 지문이 run 전후로 다름, 팔의 rewrite 실패가 run 중 늘어남(fail-open 으로
원본이 나가 "변화 없음"처럼 보인다), A/A 대조가 쌍둥이가 아니거나 초과 흔들림 > m, holdout 판정인데 A/A 대조가 없음.

최종 판정 (종료 코드)
  FAIL          (1) 정확도 악화 / 비용 악화 / 속도 악화 중 하나라도
  INCONCLUSIVE  (2) 정확도 보류 또는 속도 미결 또는 run 무효
  PASS          (0) 나머지 불악화 + (비용 개선 또는 속도 개선)
  NO-CHANGE     (3) 아무 축도 개선·악화 없음 (A/A 대조가 여기에 와야 한다). 채택 대상이 아니므로 PASS 와 코드가 다르다.
  정확도의 "개선"은 결과 불변 원칙상 자동 증명 대상이 아니다 - 3축 동시 개선(best)은 하네스가 판정하지 않는다.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import math
import os
import random
import statistics
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

SPEED_MARGIN = 0.01         # 지연 허용 폭 1% (고정 정책 상수, 운영자 2026-09-23)
MAX_CONTAMINATED = 0.2
MIN_QUERIES = 30            # 속도 판정에 필요한 최소 쿼리 수(1쿼리면 부트스트랩 CI 폭이 0)
MIN_ROUNDS = 3              # 쿼리별 라운드 중앙값이 이상치를 누르려면 3 라운드가 필요하다
TAIL_Q_RATIO = 1.08         # 쿼리 단위 꼬리: 이 비를 넘는 쿼리를 센다. 실측 A/A 쿼리별 비 최대 1.061(3 run)
TAIL_MAX_FRAC = 0.05
TAIL_MIN_COUNT = 2
ORDER_MARGIN = 0.10         # 상태 의존 지연 감지 폭(로그 차). 실측 동일 코드 팔끼리 CI 반폭 ~0.06.
                            # 캐시 적중(지연 수 %)은 로그 차 ~3 이라 쿼리 일부만 적중해도 넘는다
PROD_WINDOW_S = {"recall": 15, "context-triggers": 15, "retain": 15, "reflect": 120, "mental-models": 240}
# mental-models 240s: refresh·create 는 비동기 operation 이라 도착 후 종료까지 p90 151s, 240s 가 운영 실측 62건 중 61건을 덮는다 (issue #68)
PROD_SLACK_S = 1            # 운영 감사 로그 ts 는 초 단위
BOOT_ITERS = 4000
BOOT_SEED = 1729
EXIT = {"PASS": 0, "FAIL": 1, "INCONCLUSIVE": 2, "NO-CHANGE": 3}

# 무손실로 인정하는 "복원 가능한 제거"의 화이트리스트: 지운 필드 -> 같은 값을 가진 남은 필드.
# 임의의 두 필드 값이 우연히 같다는 이유로 복원 가능하다고 보지 않는다(리뷰 P1, 2026-09-23).
# gateway/recall_prune.REDUNDANT_WITH 와 같은 선언이며, 새 파생 규칙은 여기에 명시적으로 추가해야 한다.
DERIVABLE = {"occurred_end": "occurred_start", "mentioned_at": "occurred_start"}

# 형식 파생 규칙: 지운 필드 -> (남은 결과, run 의 bank) 로 값을 다시 만드는 함수. upstream 의 chunk 키는
# "{bank_id}_{document_id}_{chunk_index}" 이고(chunks 테이블 PK), 0 번 chunk 만 규칙으로 복원한다.
# gateway/recall_prune.FORMAT_DERIVABLE 과 같은 선언이다.
FORMAT_DERIVABLE = {
    "chunk_id": lambda row, bank: f"{bank}_{row['document_id']}_0" if bank and "document_id" in row else None,
}

ACC_OK = ("identical", "lossless")
ACC_HOLD = ("added", "extended", "prefix-cut")
ACC_BAD = ("lossy", "changed", "outcome-diff")
_RANK = {c: i for i, c in enumerate(ACC_OK + ACC_HOLD + ACC_BAD)}


def _worst(*classes: str) -> str:
    return max(classes, key=lambda c: _RANK[c])


# ---------------------------------------------------------------- 정확도

def _empty(v: Any) -> bool:
    """제거해도 정보가 없는 값. 빈 문자열·0·False 는 의미 있는 값으로 본다(recall_prune._empty 와 동일 정의)."""
    return v is None or v == [] or v == {}


def _same(a: Any, b: Any) -> bool:
    """JSON 표현 기준 동일성. 파이썬 == 는 1 == 1.0 == True 를 같다고 본다."""
    return json.dumps(a, sort_keys=True, ensure_ascii=False) == json.dumps(b, sort_keys=True, ensure_ascii=False)


def compare_result(base: Dict[str, Any], cand: Dict[str, Any], bank: Optional[str] = None) -> str:
    """dict 한 건 비교(결과 1건 또는 payload 최상위): identical / lossless / added / lossy.
    bank = run 의 bank. 모르면 형식 파생 규칙을 쓰지 않는다(지운 필드는 손실)."""
    if _same(base, cand):
        return "identical"
    for k, v in base.items():
        if k in cand:
            if not _same(cand[k], v):
                return "lossy"                      # 값이 바뀜(타입 포함 - 1 과 1.0 은 다른 값)
            continue
        if _empty(v):
            continue                                # 빈 값 제거
        twin = DERIVABLE.get(k)
        if twin is not None and twin in cand and _same(cand[twin], v):
            continue                                # 선언된 파생 필드 - 남은 쌍에서 복원 가능
        rebuild = FORMAT_DERIVABLE.get(k)
        if rebuild is not None and _same(rebuild(cand, bank), v):
            continue                                # 선언된 형식 파생 - 남은 필드와 bank 로 복원 가능
        return "lossy"
    if set(cand) - set(base):
        return "added"                              # 정보 손실은 없지만 임의 내용이 끼어들 수 있다 -> 보류
    return "lossless"


def compare_results(base: List[Dict[str, Any]], cand: List[Dict[str, Any]], bank: Optional[str] = None) -> str:
    bid = [r.get("id") for r in base]
    cid = [r.get("id") for r in cand]
    if bid == cid:
        worst = "identical"
        for b, c in zip(base, cand):
            worst = _worst(worst, compare_result(b, c, bank))
        return worst
    if len(cid) < len(bid) and bid[:len(cid)] == cid:
        return "prefix-cut"
    if len(cid) > len(bid) and cid[:len(bid)] == bid:
        # 공통 앞부분의 필드도 손실이 없어야 extended
        worst = "extended"
        for b, c in zip(base, cand):
            worst = _worst(worst, compare_result(b, c, bank))
        return worst
    return "changed"


def _as_stored(obj: Any) -> Dict[str, Any]:
    """저장 payload 정규형. 예전 run 은 results 목록만 저장했다."""
    if isinstance(obj, list):
        return {"results": obj, "payload_rest": None, "blocks": None, "structured": None}
    return obj


def compare_stored(base: Any, cand: Any, bank: Optional[str] = None) -> str:
    """payload 전체 비교: results + results 밖 최상위 키 + 추가 text 블록 + structuredContent 일관성.
    결과 목록이 없는 응답(오류·비구조)은 클라이언트가 받는 텍스트 전체가 같아야 identical 이다."""
    b, c = _as_stored(base), _as_stored(cand)
    if "results" not in b or "results" not in c:
        return "identical" if _same(b.get("text"), c.get("text")) and "results" not in b and "results" not in c \
            else "changed"
    cls = compare_results(b["results"], c["results"], bank)
    if b.get("payload_rest") is not None and c.get("payload_rest") is not None:
        cls = _worst(cls, compare_result(b["payload_rest"], c["payload_rest"]))
    if b.get("blocks") is not None and c.get("blocks") is not None and not _same(b["blocks"], c["blocks"]):
        cls = _worst(cls, "changed")
    bs, cs = b.get("structured"), c.get("structured")
    if bs is not None and cs is not None and bs != cs:
        # consistent -> absent(structuredContent 삭제) / -> inconsistent(한쪽만 바꿈)는 그 표현을 쓰는 클라이언트에게
        # 정보가 사라지거나 달라진다 -> changed. absent -> consistent 는 표현이 늘어난 것 -> added(보류)
        cls = _worst(cls, "added" if (bs, cs) == ("absent", "consistent") else "changed")
    return cls


def accuracy_for_query(base_obs: List[Tuple[str, Any]], cand_obs: List[Tuple[str, Any]],
                       bank: Optional[str] = None) -> Dict[str, Any]:
    """관측 목록 = [(outcome, stored payload or None)] 라운드별. 기준 비결정 허용: 후보 관측 각각을
    기준 관측 중 가장 좋은 매치로 분류하고, 후보 관측들 중 최악을 쿼리 판정으로 삼는다."""
    base_set = {json.dumps(r, sort_keys=True, ensure_ascii=False) if r is not None else None: (o, r)
                for o, r in base_obs}
    base_has_ok = any(o == "ok" for o, _ in base_obs)
    per_obs = []
    for co, cr in cand_obs:
        if co != "ok" and base_has_ok:
            per_obs.append("outcome-diff")          # 기준이 한 번이라도 성공했으면 후보 오류는 변명 불가
            continue
        best = None
        for bo, br in base_set.values():
            if bo != co:
                cls = "outcome-diff"
            elif cr is None or br is None:
                cls = "identical" if cr == br else "outcome-diff"
            else:
                cls = compare_stored(br, cr, bank)
            if best is None or _RANK[cls] < _RANK[best]:
                best = cls
        per_obs.append(best or "outcome-diff")
    worst = _worst(*per_obs) if per_obs else "outcome-diff"
    return {"class": worst, "base_variants": len(base_set),
            "cand_variants": len({json.dumps(r, sort_keys=True, ensure_ascii=False) for _, r in cand_obs})}


# ---------------------------------------------------------------- 통계

def bootstrap_ci(values: Sequence[Any], stat, iters: int = BOOT_ITERS, seed: int = BOOT_SEED,
                 alpha: float = 0.05) -> Tuple[float, float, float]:
    vals = list(values)
    if not vals:
        return (math.nan, math.nan, math.nan)
    rng = random.Random(seed)
    n = len(vals)
    boots = sorted(stat([vals[rng.randrange(n)] for _ in range(n)]) for _ in range(iters))
    lo = boots[int((alpha / 2) * iters)]
    hi = boots[min(iters - 1, int((1 - alpha / 2) * iters))]
    return (stat(vals), lo, hi)


def geo_mean_ratio(log_ratios: Sequence[float]) -> float:
    return math.exp(statistics.fmean(log_ratios))


def quantile(xs: Sequence[float], q: float) -> float:
    s = sorted(xs)
    if not s:
        return math.nan
    k = q * (len(s) - 1)
    f, c = math.floor(k), math.ceil(k)
    return s[f] + (s[c] - s[f]) * (k - f)


def p95(xs: Sequence[float]) -> float:
    return quantile(xs, 0.95)


TAIL_QUANTILES = {"p95": 0.95, "p99": 0.99}


def noninferiority(ratio: float, lo: float, hi: float, noise: float) -> str:
    """꼬리 지표(후보/기준 비)의 비열등 판정(채택 기준, issue #13 grill). noise = 허용 폭(m 과 A/A 잡음폭 중 큰 것).
    상한이 1 + noise 이하면 ok, 하한이 그 선을 넘으면 regressed, 가를 수 없으면 hold(표본을 늘려 다시 판정)."""
    if any(math.isnan(x) for x in (ratio, lo, hi)):
        return "hold"
    if hi <= 1 + noise:
        return "ok"
    if lo > 1 + noise:
        return "regressed"
    return "hold"


def aa_noise_of(aa_report: Dict[str, Any]) -> Dict[str, float]:
    """A/A 대조의 꼬리 지표 흔들림 = |비 - 1|. 옛 p95 규칙의 허용 폭(2m)보다 넓히지 않는다."""
    out = {}
    for m, t in ((aa_report.get("speed") or {}).get("tail_metrics") or {}).items():
        r = t.get("ratio")
        out[m] = 0.0 if r is None else min(abs(r - 1), 2 * SPEED_MARGIN)
    return out


def speed_verdict(ratio: float, lo: float, hi: float, margin: float = SPEED_MARGIN, excess: float = 0.0) -> str:
    """excess = A/A 대조가 허용 폭을 넘어 흔들린 만큼. 유리한 주장(개선·불악화)의 문턱만 그만큼 좁힌다 -
    넓히는 쪽으로는 절대 쓰지 않는다(재리뷰 P1-2). 악화 문턱은 그대로(보수적)."""
    if any(math.isnan(x) for x in (ratio, lo, hi)):
        return "inconclusive"
    if hi < 1 - margin - excess:
        return "improved"
    if lo > 1 + margin:
        return "regressed"
    if hi < 1 + margin - excess:
        return "non-inferior"
    return "inconclusive"


def cost_verdict(per_query: Dict[str, Dict[str, Any]], total_base: float, total_cand: float) -> str:
    """per_query[k] = {"inc": 후보 중앙값 > 기준 최대, "dec": 후보 중앙값 < 기준 최소}. 허용 폭 0."""
    if any(v["inc"] for v in per_query.values()):
        return "regressed"
    if total_cand < total_base and any(v["dec"] for v in per_query.values()):
        return "improved"
    return "unchanged"


def tail_verdict(per_q_ratio: Sequence[float]) -> Dict[str, Any]:
    n = len(per_q_ratio)
    over = sum(1 for r in per_q_ratio if r > TAIL_Q_RATIO)
    limit = max(TAIL_MIN_COUNT, TAIL_MAX_FRAC * n)
    return {"queries_over": over, "limit": limit, "regressed": over > limit}


# ---------------------------------------------------------------- 로드

def load_run(run_dir: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    meta = json.load(open(os.path.join(run_dir, "meta.json")))
    calls = [json.loads(line) for line in open(os.path.join(run_dir, "calls.jsonl")) if line.strip()]
    return meta, calls


def load_results(run_dir: str, arm: str, key: str, digest: Optional[str]) -> Any:
    if not digest:
        return None
    path = os.path.join(run_dir, "results", f"{arm}.{key}.{digest}.json")
    return json.load(open(path)) if os.path.exists(path) else None


def _ts(s: str) -> float:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def load_prod_windows(run_dir: str, meta: Dict[str, Any]) -> Optional[List[Tuple[float, float]]]:
    """운영 리랭크 사용 구간 목록. meta 에 prod_events 키가 없으면(합성·구버전 run) 빈 목록,
    키가 있는데 None 이면 신호를 못 모은 것 -> None(판정기가 속도를 미결로 둔다)."""
    if "prod_events" not in meta:
        return []
    if meta["prod_events"] is None:
        return None
    out = []
    path = os.path.join(run_dir, "prod_events.jsonl")
    for line in open(path) if os.path.exists(path) else []:
        ev = json.loads(line)
        # 감사 행이 요청 도착 때 찍히는지(recall) 처리 뒤에 찍히는지(context-triggers: recall 이 끝난 뒤) 종류마다
        # 다르므로 ts 앞뒤로 같은 폭을 잡는다(final-review 2026-09-23) - 보수적으로 더 많이 뺀다
        t0 = _ts(ev["ts"])
        w = PROD_WINDOW_S.get(ev.get("kind"), 15)
        out.append((t0 - w - PROD_SLACK_S, t0 + w + PROD_SLACK_S))
    return out


def load_guard_windows(meta: Dict[str, Any]) -> List[Tuple[float, float]]:
    """벤치 간섭 가드가 표시한 정지 구간(epoch 초). 가드 이전 run 은 빈 목록."""
    return [(float(w0), float(w1)) for w0, w1 in (meta.get("guard") or {}).get("windows") or []]


def _overlaps_prod(rec: Dict[str, Any], windows: List[Tuple[float, float]]) -> bool:
    if not windows or "wall" not in rec:
        return False
    s = _ts(rec["wall"])
    e = _ts(rec["wall_end"]) if rec.get("wall_end") else s + rec["latency_ms"] / 1000
    return any(s < w1 and w0 < e for w0, w1 in windows)


def _contaminated(rec: Dict[str, Any], windows: List[Tuple[float, float]]) -> bool:
    if "tei_queue_before" in rec and (rec["tei_queue_before"] is None or rec["tei_queue_before"] > 0):
        return True
    return bool(rec.get("retried")) or _overlaps_prod(rec, windows)


def order_effect(by: Dict[Tuple[str, str], List[Dict[str, Any]]], keys: List[str], baseline: str,
                 candidate: str) -> Dict[str, Any]:
    """상태 의존 지연 감지. 팔마다 d = log(첫 라운드) - median(log(이후 라운드)), 후보-기준 차의 평균 CI."""
    diffs = []
    for k in keys:
        d = {}
        for arm in (baseline, candidate):
            lat = {c["round"]: c["latency_ms"] for c in by[(arm, k)] if c["outcome"] == "ok"}
            if 0 in lat and len(lat) >= 2:
                d[arm] = math.log(lat[0]) - statistics.median(math.log(v) for r, v in lat.items() if r > 0)
        if len(d) == 2:
            diffs.append(d[candidate] - d[baseline])
    if len(diffs) < 2:
        return {"checked": False}
    mean, lo, hi = bootstrap_ci(diffs, statistics.fmean)
    return {"checked": True, "mean_log_diff": _r(mean), "ci95": [_r(lo), _r(hi)],
            "state_dependent": lo > ORDER_MARGIN or hi < -ORDER_MARGIN}


LAP_MEDIAN_MIN = 3          # 쿼리의 완전한 바퀴가 이만큼 있어야 바퀴 값들의 중앙값을 쓴다(그보다 적으면 평균)


def _complete_laps(by_phase: Dict[int, Any], n: int) -> Dict[int, List[Any]]:
    """{단계: 값} -> {바퀴: 그 바퀴의 값 n 개}. 바퀴 L = 단계 L*n .. L*n+n-1(bench/crossover 의 배정 k mod n).
    단계가 하나라도 빠진 바퀴는 뺀다 - 바퀴 안에서 단계를 잘라내면 세트 배정 균형이 깨져 오프셋이 남는다."""
    laps: Dict[int, Dict[int, Any]] = collections.defaultdict(dict)
    for r, v in by_phase.items():
        laps[r // n][r] = v
    return {L: [ph[r] for r in sorted(ph)] for L, ph in sorted(laps.items()) if len(ph) == n}


def _lap_stat(lap_values: List[float]) -> float:
    """바퀴 값들 -> 쿼리 값. 3바퀴 이상이면 중앙값(한 단계의 튀는 값이 끌고 가지 않게), 아니면 평균. 절사 평균은 쓰지 않는다."""
    return statistics.median(lap_values) if len(lap_values) >= LAP_MEDIAN_MIN else statistics.fmean(lap_values)


def _lap_levels(laps: Dict[int, List[Tuple[float, float]]]) -> Tuple[float, float]:
    """쿼리의 완전한 바퀴들 -> (기준, 후보) 대표 지연. 바퀴마다 기하평균(오프셋 상쇄) 뒤, 3바퀴 이상이면 로그 비가 가운데인
    바퀴(짝수면 가운데 둘)의 지연, 아니면 전 바퀴 평균. 기준·후보를 같은 바퀴에서 골라 둘의 비가 _lap_stat 의 쿼리 값과 같다."""
    per = [tuple(statistics.fmean(math.log(p[i]) for p in lap) for i in (0, 1)) for lap in laps.values()]
    if len(per) >= LAP_MEDIAN_MIN:
        per.sort(key=lambda bc: bc[1] - bc[0])
        per = per[(len(per) - 1) // 2:len(per) // 2 + 1]
    return tuple(math.exp(statistics.fmean(bc[i] for bc in per)) for i in (0, 1))


# ---------------------------------------------------------------- 판정

def judge(run_dir: str, baseline: str, candidate: str, min_queries: int = MIN_QUERIES,
          aa_excess: float = 0.0, aa_noise: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    meta, calls = load_run(run_dir)
    meas = [c for c in calls if c.get("phase") == "measure"]
    # warm = 라운드(교차 단계)마다 팔별 첫 측정 호출을 뺀 것. 첫 호출은 idle 뒤라 cold 쪽 지연이다(기록 순서 = 실행 순서)
    first_ids, seen_first = set(), set()
    for c in meas:
        if (c["arm"], c["round"]) not in seen_first:
            seen_first.add((c["arm"], c["round"]))
            first_ids.add(id(c))
    by = collections.defaultdict(list)                       # (arm, key) -> [rec]
    for c in meas:
        by[(c["arm"], c["key"])].append(c)
    keys = sorted({c["key"] for c in meas})
    reasons: List[str] = []

    # 정확도
    acc_rows = {}
    for k in keys:
        b = [(c["outcome"], load_results(run_dir, baseline, k, c.get("results_digest"))) for c in by[(baseline, k)]]
        cnd = [(c["outcome"], load_results(run_dir, candidate, k, c.get("results_digest")))
               for c in by[(candidate, k)]]
        acc_rows[k] = accuracy_for_query(b, cnd, meta.get("bank"))   # 구 run(bank 미기록)은 형식 파생 미적용
    acc_counts = collections.Counter(r["class"] for r in acc_rows.values())
    if any(acc_counts[c] for c in ACC_BAD):
        acc_verdict = "regressed"
    elif any(acc_counts[c] for c in ACC_HOLD):
        acc_verdict = "hold"
    else:
        acc_verdict = "invariant"
    base_nondet = sum(1 for r in acc_rows.values() if r["base_variants"] > 1)

    # 비용 (양쪽 모두 토큰이 있는 쿼리) - 쿼리 단위, 허용 폭 0
    tb, tc, per_q_cost = 0.0, 0.0, {}
    for k in keys:
        bt = [c["tokens"] for c in by[(baseline, k)] if "tokens" in c]
        ct = [c["tokens"] for c in by[(candidate, k)] if "tokens" in c]
        if bt and ct:
            mb, mc = statistics.median(bt), statistics.median(ct)
            tb += mb
            tc += mc
            per_q_cost[k] = {"inc": mc > max(bt), "dec": mc < min(bt), "rel": (mc - mb) / mb if mb else 0.0}
    cost_rel = (tc - tb) / tb if tb else math.nan
    cv = cost_verdict(per_q_cost, tb, tc) if per_q_cost else "inconclusive"
    worst_cost = sorted(((k, v["rel"]) for k, v in per_q_cost.items()), key=lambda kv: -kv[1])[:5]

    # 속도 (쌍 = 같은 라운드 같은 쿼리, 오염 쌍 제외)
    windows = load_prod_windows(run_dir, meta)
    excl = (windows or []) + load_guard_windows(meta)          # 운영 요청 구간 + 벤치 간섭 정지 구간
    pairs = collections.defaultdict(list)                     # key -> [log ratio per round]
    total_pairs, contaminated = 0, 0
    lat_b, lat_c = [], []
    rounds = collections.defaultdict(dict)
    for c in meas:
        rounds[(c["key"], c["round"])][c["arm"]] = c
    for (k, r), arms in rounds.items():
        if baseline not in arms or candidate not in arms:
            continue
        b, cnd = arms[baseline], arms[candidate]
        if b["outcome"] != "ok" or cnd["outcome"] != "ok":
            continue
        total_pairs += 1
        if _contaminated(b, excl) or _contaminated(cnd, excl):
            contaminated += 1
            continue
        pairs[k].append(math.log(cnd["latency_ms"] / b["latency_ms"]))
        lat_b.append(b["latency_ms"])
        lat_c.append(cnd["latency_ms"])
    # 순환 교차 run(meta.crossover): 라운드 = 교차 단계이고 단계마다 팔이 다른 인스턴스에 붙는다. 인스턴스 오프셋은
    # 단계에 걸쳐 합이 0 이 되도록 배정되므로 쿼리별 로그 비를 평균해야 상쇄된다(중앙값은 한쪽으로 쏠린다).
    crossover = bool(meta.get("crossover"))
    # 바퀴 단위 집계(issue #82)는 계획 meta 가 있는 교차 run(#77 이후 병합)에만 쓴다. 과거 run 은 옛 규칙 그대로 판정한다.
    # 팔 수 n 은 계획의 팔 목록에서 정한다 - 한 팔의 호출이 통째로 빠져도 바퀴 경계가 계획의 순환과 어긋나지 않게.
    # 계획의 팔 목록을 읽을 수 없으면 병합 meta.arms(병합이 단계 팔과 대조한 값). 측정 호출에서 본 팔 집합은 쓰지 않는다
    plan_meta = meta.get("plan")
    plan_arms = plan_meta.get("arms") if isinstance(plan_meta, dict) else None
    lap_n = (len(plan_arms) if isinstance(plan_arms, list) and plan_arms else len(meta.get("arms") or {})) \
        if crossover and plan_meta is not None else 0
    if lap_n:
        # 오프셋 상쇄는 바퀴 안에서 성립한다 - 바퀴마다 로그 비를 평균하고, 단계가 빠진 바퀴만 그 쿼리에서 뺀다
        usable = collections.defaultdict(dict)                # key -> {단계: (기준 지연, 후보 지연)}
        for (k, r), arms in rounds.items():
            if baseline in arms and candidate in arms:
                b_, c_ = arms[baseline], arms[candidate]
                if b_["outcome"] == "ok" and c_["outcome"] == "ok" \
                        and not (_contaminated(b_, excl) or _contaminated(c_, excl)):
                    usable[k][r] = (b_["latency_ms"], c_["latency_ms"])
        q_laps = {k: _complete_laps(v, lap_n) for k, v in usable.items()}
        complete = {k for k, v in q_laps.items() if v}
        # 전 단계가 오염돼 pairs 에 흔적이 없는 쿼리도 센다 - queries_with_dropped_laps 와 같은 쿼리 집합(rounds)에서 센다(issue #106)
        incomplete = sum(1 for k in {k for k, _ in rounds} if k not in complete)
        per_q_log = [_lap_stat([statistics.fmean(math.log(c_ / b_) for b_, c_ in lap) for lap in q_laps[k].values()])
                     for k, v in pairs.items() if v and k in complete]
        whole_laps = {L for v in q_laps.values() for L in v}   # 어느 쿼리에서든 완전했던 바퀴
        phases = {r for _, r in rounds}               # run 에서 단계가 모두 있는 바퀴 - 끝에 반만 남은 바퀴(축소·중단)는 세지 않는다
        run_laps = sum(1 for L in {r // lap_n for r in phases} if all(L * lap_n + i in phases for i in range(lap_n)))
        laps_report = {"size": lap_n, "complete_lap_phases": lap_n * len(whole_laps),
                       "queries_with_dropped_laps": sum(1 for k in {k for k, _ in rounds}   # 쓸 단계가 하나도 없는 쿼리 포함
                                                        if len(q_laps.get(k, {})) < run_laps)}
    else:
        per_q_stat = statistics.fmean if crossover else statistics.median
        # 평균이 오프셋을 상쇄하는 건 모든 단계가 남았을 때뿐이다 - 교차 run 은 단계가 빠진 쿼리를 속도 판정에서 뺀다
        complete = {k for k, v in pairs.items() if len(v) == meta.get("rounds")} if crossover else set(pairs)
        incomplete = sum(1 for k, v in pairs.items() if v and k not in complete)
        per_q_log = [per_q_stat(v) for k, v in pairs.items() if v and k in complete]
    ratio, lo, hi = bootstrap_ci(per_q_log, geo_mean_ratio)
    contam_rate = contaminated / total_pairs if total_pairs else 1.0
    sv = speed_verdict(ratio, lo, hi, SPEED_MARGIN, aa_excess)
    tail = tail_verdict([math.exp(x) for x in per_q_log])
    paired = list(zip(lat_b, lat_c))
    warm_pairs = []                                 # (쿼리, 기준 지연, 후보 지연) - 양쪽 다 warm 인 쌍
    for (k, r), arms in rounds.items():
        if baseline in arms and candidate in arms and k in complete:
            b_, c_ = arms[baseline], arms[candidate]
            if b_["outcome"] == "ok" and c_["outcome"] == "ok" and id(b_) not in first_ids and id(c_) not in first_ids \
                    and not (_contaminated(b_, excl) or _contaminated(c_, excl)):
                warm_pairs.append((k, b_["latency_ms"], c_["latency_ms"]))
    if lap_n:
        # 섞은 p95·warm 꼬리도 같은 바퀴 단위로 모은다: 쿼리마다 바퀴별 기하평균 지연 -> 바퀴 값들의 중앙값(3바퀴 이상)·평균
        paired = [_lap_levels(laps) for laps in q_laps.values() if laps]
    elif crossover:
        # 섞은 p95 도 단계별 인스턴스 오프셋에 끌려간다 - 쿼리마다 단계 평균(기하) 지연으로 모은 뒤 p95 를 본다
        qb, qc = collections.defaultdict(list), collections.defaultdict(list)
        for (k, r), arms in rounds.items():
            if baseline in arms and candidate in arms and k in complete:
                b_, c_ = arms[baseline], arms[candidate]
                if b_["outcome"] == "ok" and c_["outcome"] == "ok" \
                        and not (_contaminated(b_, excl) or _contaminated(c_, excl)):
                    qb[k].append(math.log(b_["latency_ms"]))
                    qc[k].append(math.log(c_["latency_ms"]))
        paired = [(math.exp(statistics.fmean(qb[k])), math.exp(statistics.fmean(qc[k]))) for k in qb]
    p95_ratio, p95_lo, p95_hi = bootstrap_ci(
        paired, lambda ps: p95([c for _, c in ps]) / p95([b for b, _ in ps]) if ps else math.nan)
    # 속도 축의 꼬리: warm p95·p99 를 A/A 잡음폭 기준 비열등으로 본다. 교차 run 은 쿼리마다 단계 평균(기하)으로 모은다
    if lap_n:
        wr = collections.defaultdict(dict)                    # key -> {단계: (기준, 후보)} - 양쪽 다 warm 인 쌍만
        for (k, r), arms in rounds.items():
            if r in usable.get(k, {}) and id(arms[baseline]) not in first_ids and id(arms[candidate]) not in first_ids:
                wr[k][r] = usable[k][r]
        warm = [_lap_levels(laps) for laps in (_complete_laps(v, lap_n) for v in wr.values()) if laps]
    elif crossover:
        wq = collections.defaultdict(list)
        for k, b_ms, c_ms in warm_pairs:
            wq[k].append((math.log(b_ms), math.log(c_ms)))
        warm = [(math.exp(statistics.fmean(b for b, _ in v)), math.exp(statistics.fmean(c for _, c in v)))
                for v in wq.values()]
    else:
        warm = [(b_ms, c_ms) for _, b_ms, c_ms in warm_pairs]
    tail_metrics = {}
    for m, q in TAIL_QUANTILES.items():
        r_, lo_, hi_ = bootstrap_ci(
            warm, lambda ps, q=q: quantile([c for _, c in ps], q) / quantile([b for b, _ in ps], q) if ps else math.nan)
        # 허용 폭 = 평균과 같은 불악화 폭 m, A/A 대조가 그보다 더 흔들렸으면 그 잡음폭(최대 2m, aa_noise_of)
        noise = (aa_noise or {}).get(m, 0.0)
        tol = max(SPEED_MARGIN, noise)
        tail_metrics[m] = {"ratio": _r(r_), "ci95": [_r(lo_), _r(hi_)], "aa_noise": noise, "tolerance": tol,
                           "n": len(warm), "verdict": noninferiority(r_, lo_, hi_, tol)}
    p95_regressed = tail_metrics["p95"]["verdict"] == "regressed"
    tail_regressed = tail["regressed"] or any(t["verdict"] == "regressed" for t in tail_metrics.values())
    tail_hold = [f"tail-hold:{m}(n={t['n']})" for m, t in tail_metrics.items() if t["verdict"] == "hold"]
    # 교차 run 은 단계마다 컨테이너를 다시 띄우고 단계당 1 라운드만 재므로 반복 호출로 얻는 캐시 이득이 없다.
    # 라운드 간 차이는 인스턴스 효과라 상태 의존 감지를 쓰면 거짓 경보가 난다 -> 교차 run 에서는 건너뛴다.
    order = {"checked": False, "reason": "crossover"} if crossover else order_effect(by, keys, baseline, candidate)

    speed_blockers = []
    if lap_n:                                       # 교차 run 의 라운드 = 완전한 바퀴에 속한 단계 수
        if laps_report["complete_lap_phases"] < MIN_ROUNDS:
            speed_blockers.append(f"too-few-rounds:{laps_report['complete_lap_phases']}<{MIN_ROUNDS}")
    elif meta.get("rounds", 0) < MIN_ROUNDS:
        speed_blockers.append(f"too-few-rounds:{meta.get('rounds')}<{MIN_ROUNDS}")
    plan = plan_report(meta)                                    # 계획 meta 가 없는 과거 run 은 None - 이 검사를 건너뛴다
    if plan is not None and "invalid" in plan:
        speed_blockers.append(f"plan-invalid:{plan['invalid']}")
    elif plan is not None and plan["unexplained_shortfall"]:
        speed_blockers.append(f"plan-truncated:{plan['actual_phases']}<{plan['effective_planned_phases']}")
    if len(per_q_log) < min_queries:
        speed_blockers.append(f"too-few-queries:{len(per_q_log)}<{min_queries}")
    if contam_rate > MAX_CONTAMINATED:
        speed_blockers.append(f"contaminated:{contam_rate:.2f}")
    if windows is None:
        speed_blockers.append("no-prod-interference-signal")
    if meta.get("carryover_balanced") is False:
        speed_blockers.append("carryover-unbalanced(>3 arms)")
    if order.get("state_dependent"):
        speed_blockers.append("state-dependent-latency(cache?)")
    if speed_blockers:
        sv = "inconclusive"                         # 표본이 판정 조건을 못 채우면 악화·개선 모두 말하지 않는다
    elif tail_regressed:
        sv = "regressed"
    elif tail_hold:
        sv = "inconclusive"                         # 꼬리를 가를 수 없으면 보류 - 라운드를 늘려 다시 판정
        speed_blockers += tail_hold
    reasons += speed_blockers if sv == "inconclusive" else []

    # run 무효 조건
    run_invalid = []
    if (meta.get("guard") or {}).get("aborted"):
        run_invalid.append(f"guard-aborted:{meta['guard']['aborted']}")
    fb, fa = meta.get("freeze_before"), meta.get("freeze_after")
    if fb is not None and fa is not None and fb != fa:
        run_invalid.append("replica-drift")
    rf_b, rf_a = meta.get("rewrite_failures_before") or {}, meta.get("rewrite_failures_after") or {}
    for arm in (baseline, candidate):
        if arm in rf_a and (rf_a.get(arm) is None or rf_b.get(arm) is None or rf_a[arm] > rf_b[arm]):
            run_invalid.append(f"rewrite-failures:{arm}")
    reasons += run_invalid

    # 최종
    if acc_verdict == "regressed" or cv == "regressed" or sv == "regressed":
        verdict = "FAIL"
    elif acc_verdict == "hold" or sv == "inconclusive" or cv == "inconclusive" or run_invalid:
        verdict = "INCONCLUSIVE"
    elif cv == "improved" or sv == "improved":
        verdict = "PASS"
    else:
        verdict = "NO-CHANGE"

    return {
        "run_id": meta["run_id"], "baseline": baseline, "candidate": candidate, "split": meta["split"],
        "n_queries": len(keys), "rounds": meta["rounds"], "bench_clock": meta["bench_clock"],
        "verdict": verdict, "reasons": reasons,
        "accuracy": {"verdict": acc_verdict, "classes": dict(acc_counts), "baseline_nondeterministic": base_nondet,
                     "bad_keys": sorted(k for k, r in acc_rows.items() if r["class"] in ACC_BAD + ACC_HOLD)[:20]},
        "cost": {"verdict": cv, "rel_change": round(cost_rel, 5) if not math.isnan(cost_rel) else None,
                 "baseline_tokens": tb, "candidate_tokens": tc,
                 "queries_increased": sum(1 for v in per_q_cost.values() if v["inc"]),
                 "queries_decreased": sum(1 for v in per_q_cost.values() if v["dec"]),
                 "worst_queries": [(k, round(v, 4)) for k, v in worst_cost]},
        "speed": {"verdict": sv, "crossover": crossover, "geo_mean_ratio": _r(ratio), "ci95": [_r(lo), _r(hi)], "margin": SPEED_MARGIN,
                  "aa_excess": aa_excess,
                  "n_queries": len(per_q_log), "incomplete_phase_queries": incomplete if crossover else None,
                  **({"laps": laps_report} if lap_n else {}),   # 과거 교차 run·일반 run 은 키 자체를 싣지 않는다(판정 JSON 불변)
                  "tail": tail, "tail_regressed": tail_regressed,
                  "p95_ratio": _r(p95_ratio), "p95_ci95": [_r(p95_lo), _r(p95_hi)], "tail_metrics": tail_metrics,
                  "order_effect": order,
                  "pairs": total_pairs, "contaminated_pairs": contaminated, "contamination_rate": round(contam_rate, 4),
                  "prod_windows": None if windows is None else len(windows),
                  "baseline_median_ms": _r(statistics.median(lat_b)) if lat_b else None,
                  "candidate_median_ms": _r(statistics.median(lat_c)) if lat_c else None},
        "provenance": meta.get("provenance"),
        "configs": meta.get("configs"),              # 교차 병합 run 의 팔별 upstream 설정(A/A 쌍둥이 확인용)
        **({} if plan is None else {"plan": plan}),  # 계획 meta 가 없는 과거 run 은 키 자체를 싣지 않는다(판정 JSON 불변)
    }


PLAN_VERSIONS = (1,)        # bench/crossover.PLAN_VERSION - 계획 파일 형식 버전. 모르는 버전은 해석하지 않고 보류한다


def _is_count(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool) and x >= 1


def _is_truncation(t: Any) -> bool:
    """축소 한 건: 목표 단계 수 + 공백이 아닌 사유 + 시각(issue #100). 시각 기준은 crossover._now 가 쓰는 tz 있는 ISO 8601 -
    사유·시각 없는 축소를 인정하면 손으로 지운 기록이 plan-truncated 보류를 우회한다."""
    if not isinstance(t, dict) or not _is_count(t.get("planned_phases")):
        return False
    reason, at = t.get("reason"), t.get("at")
    if not isinstance(reason, str) or not reason.strip() or not isinstance(at, str):
        return False
    try:
        return dt.datetime.fromisoformat(at).tzinfo is not None
    except ValueError:
        return False


def _plan_problem(plan: Any, meta: Dict[str, Any]) -> Optional[str]:
    """meta.plan 이 판정기가 아는 모양이 아니면 사유 한 단어. 손으로 고친 계획·다른 버전·필드 누락이 KeyError 로 판정을 죽이지 않게 한다."""
    if not isinstance(plan, dict):
        return "not-object"
    if plan.get("version") not in PLAN_VERSIONS or isinstance(plan.get("version"), bool):
        return f"version:{plan.get('version')!r}"
    if not _is_count(plan.get("planned_phases")):
        return "planned_phases"
    truncs = plan.get("truncations")
    if not isinstance(truncs, list) or not all(_is_truncation(t) for t in truncs):
        return "truncations"
    if not _is_count(meta.get("rounds")):
        return "rounds"
    return _plan_arms_problem(plan.get("arms"), meta.get("arms"))


def _plan_arms_problem(plan_arms: Any, meta_arms: Any) -> Optional[str]:
    """plan.arms 는 병합 meta.arms 와 개수·이름이 같아야 한다(issue #106). 판정기는 plan.arms 의 길이로 바퀴 경계를 정하므로
    손으로 고친 arms 가 경계를 어긋나게 만든다. meta.arms 가 없으면(한 팔의 호출이 통째로 빠진 run) 대조할 값이 없어 모양만 본다."""
    if not isinstance(plan_arms, list) or not plan_arms:
        return "arms"
    names = [a.get("name") if isinstance(a, dict) else None for a in plan_arms]
    if not all(isinstance(n, str) for n in names):
        return "arms"
    if isinstance(meta_arms, dict) and meta_arms and sorted(names) != sorted(meta_arms):
        return "arms"
    return None


def plan_report(meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """교차 run 의 계획 대비 실제(issue #77). meta.plan 은 bench/crossover merge 가 계획 파일에서 옮겨 적은 것이다:
    planned_phases(처음 계획), truncations(사유·시각과 함께 덧붙은 축소: 각 기록의 planned_phases 가 그때 줄인 목표).
    유효 계획 = 마지막 축소의 목표(없으면 처음 계획). 실제(meta.rounds)가 유효 계획보다 짧으면 사유를 대지 못한 축소다.
    meta.plan 이 없으면(계획 기록 이전 형식 run) None - 과거 run 의 판정 규칙을 사후에 바꾸지 않는다.
    meta.plan 이 있는데 모양을 해석할 수 없으면 {"invalid": 사유} - 판정기가 plan-invalid 로 속도를 보류한다."""
    plan = meta.get("plan")
    if plan is None:
        return None
    bad = _plan_problem(plan, meta)
    if bad:
        return {"invalid": bad}
    truncs = plan["truncations"]
    planned = plan["planned_phases"]
    effective = truncs[-1]["planned_phases"] if truncs else planned
    actual = meta["rounds"]
    return {"planned_phases": planned, "effective_planned_phases": effective, "actual_phases": actual,
            "truncated": actual < planned, "truncations": truncs,
            "unexplained_shortfall": max(0, effective - actual)}


def _r(x: Optional[float], n: int = 4) -> Optional[float]:
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(x, n)


def check_aa_twin(report: Dict[str, Any]) -> None:
    """A/A 대조는 정말 쌍둥이여야 한다: 결과 전부 identical, 토큰 쿼리 단위로 동일.
    (쌍둥이가 아닌 팔을 대조로 쓰면 실제 차이가 노이즈로 둔갑한다 - 2026-09-23 자기검증 실측.)"""
    classes = report["accuracy"]["classes"]
    c = report["cost"]
    if set(classes) != {"identical"} or c["rel_change"] != 0 or c["queries_increased"] or c["queries_decreased"]:
        raise SystemExit(f"A/A control is not a twin of {report['baseline']}: accuracy={classes} "
                         f"cost={c['rel_change']} inc={c['queries_increased']} dec={c['queries_decreased']}")


def aa_excess_of(report: Dict[str, Any]) -> Optional[float]:
    """A/A 대조 속도 CI 가 [1-m, 1+m] 밖으로 나간 만큼(없으면 0). CI 가 없으면 None."""
    lo, hi = report["speed"]["ci95"]
    if lo is None or hi is None:
        return None
    return round(max(0.0, (1 - lo) - SPEED_MARGIN, (hi - 1) - SPEED_MARGIN), 6)


AA_BLOCKERS = ("too-few-rounds", "too-few-queries", "contaminated:", "no-prod-interference-signal", "carryover-unbalanced",
               "state-dependent-latency", "guard-aborted", "replica-drift", "rewrite-failures", "plan-truncated",
               "plan-invalid")


def aa_gate(report: Dict[str, Any]) -> List[str]:
    """A/A 대조는 허용 폭을 넓히지 않는다. 이 run 의 측정 조건(드리프트·자리·이월 효과·간섭)이 m 을 넘어 흔들렸으면
    그 초과분(excess)만큼 유리한 주장의 문턱을 좁히고(main), 2m 을 넘거나 대조가 결과·비용에서 쌍둥이가 아니거나
    속도를 잴 수 없으면 run 무효다."""
    problems = []
    excess = aa_excess_of(report)
    if excess is None:
        problems.append("aa-speed-unmeasured")
    elif excess > SPEED_MARGIN:
        problems.append(f"aa-too-noisy:excess={excess}")
    # 결과를 바꾸지 않는 설정(B1 같은)이 대조 팔에 붙으면 결과·비용 검사를 통과한다 - 기록된 설정으로도 본다
    configs = report.get("configs")
    config_differs = configs is not None and (configs.get(report["baseline"]) or {}) != (configs.get(report["candidate"]) or {})
    if report["accuracy"]["verdict"] != "invariant" or report["cost"]["verdict"] != "unchanged" or config_differs:
        problems.append("aa-not-twin")
    # 대조 자신이 판정 조건을 못 채웠으면(쿼리·라운드 부족, 오염, 간섭 신호 없음, run 무효) 그 초과분·꼬리 잡음도 못 믿는다
    problems += [f"aa-blocked:{r}" for r in report["reasons"] if r.startswith(AA_BLOCKERS)]
    return problems


COLD_METRICS = ("cold_p50", "cold_p95")


def combine_cold(rep: Dict[str, Any], cold: Dict[str, Any]) -> Dict[str, Any]:
    """warm 판정(rep)에 cold 재생 report.json(bench/cold_replay analyze)을 합친다 - 속도 축 = warm p95·p99 + cold 요청."""
    metrics = {}
    for name in COLD_METRICS:
        m = cold["metrics"][name]
        lo, hi = m["ci95"]
        verdict = "hold" if m["short"] else noninferiority(m["ratio"], lo, hi, SPEED_MARGIN)
        metrics[name] = dict(m, ratio=_r(m["ratio"]), ci95=[_r(lo), _r(hi)],   # NaN(쌍 0) -> null: 엄격한 JSON 파서
                             tolerance=SPEED_MARGIN, verdict=verdict)
    rep = dict(rep, reasons=list(rep["reasons"]))
    regressed = [f"cold-regressed:{n}" for n, m in metrics.items() if m["verdict"] == "regressed"]
    fb, fc = cold["failures"]["baseline"], cold["failures"]["candidate"]
    if fc > fb:                     # 재시도 소진·사이드카 오류 - 분위수 비가 가리는 실패는 수로 본다
        regressed.append(f"cold-failures:{fb}->{fc}")
    hold = [f"cold-hold:{n}(n={m['n']},short={m['short']})" for n, m in metrics.items() if m["verdict"] == "hold"]
    from bench.cold_replay import IDLE_S
    invalid = []                    # 측정 조건을 못 채운 cold run 은 합격도 cold 악화도 말하지 않는다
    if cold.get("aborted"):
        invalid.append(f"cold-invalid:aborted:{cold['aborted']}")
    if not cold.get("cold_definition_met"):
        invalid.append(f"cold-invalid:idle-{cold.get('idle_s'):g}s<{IDLE_S:g}s")
    if invalid:
        rep["reasons"] += invalid
        metrics = {n: dict(m, verdict="not-judged") for n, m in metrics.items()}   # 지표별로도 ok·악화를 말하지 않는다
        if rep["verdict"] in ("PASS", "NO-CHANGE"):
            rep["verdict"] = "INCONCLUSIVE"
    else:
        rep["reasons"] += regressed + hold
        if regressed:
            rep["verdict"] = "FAIL"
        elif hold and rep["verdict"] in ("PASS", "NO-CHANGE"):
            rep["verdict"] = "INCONCLUSIVE"
    by_reason = collections.Counter(r for x in cold.get("excluded") or [] for r in x["reasons"])
    rep["cold"] = {"run_id": cold.get("run_id"), "metrics": metrics, "failures": cold["failures"], "invalid": invalid,
                   "excluded_by_reason": dict(by_reason), "prod_sidecar": cold.get("prod_sidecar"),
                   "guard_windows": cold.get("guard_windows")}
    return rep


def cold_unmeasured(rep: Dict[str, Any], reason: str) -> Dict[str, Any]:
    """cold 요청을 잴 수 없었다(#21: 24GB 호스트에서 벤치 사이드카 재생이 swapout 가드에 전부 걸림). 속도 축의 한 부분이
    비었으므로 합격·무변화는 보류로 내리고 사유를 남긴다. warm 판정은 warm_verdict 로 따로 남는다."""
    rep = dict(rep, reasons=list(rep["reasons"]) + [f"cold-unmeasured:{reason}"])
    if rep["verdict"] in ("PASS", "NO-CHANGE"):
        rep["verdict"] = "INCONCLUSIVE"
    rep["cold"] = {"verdict": "unmeasured", "reason": reason}
    return rep


def cold_exempt(rep: Dict[str, Any], reason: str) -> Dict[str, Any]:
    """cold 축을 면제한다(#169: 사이드카로 가는 요청 모양을 안 바꾸는 후보는 cold 측정 대상이 아니다). 최종 판정은 warm 판정
    그대로 두고, 면제 사실과 사유를 cold 와 reasons 에 남긴다. 사람이 인자로 선언한 경우에만 부른다."""
    rep = dict(rep, reasons=list(rep["reasons"]) + [f"cold-exempt:{reason}"])
    rep["cold"] = {"verdict": "exempt", "reason": reason}
    return rep


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run_dir")
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--aa-control", help="같은 run 안의 A/A 대조 팔 - 유효성 게이트이자 문턱을 좁히는 데만 쓴다(넓히지 않는다)")
    ap.add_argument("--aa-twin", help="대조 팔의 쌍둥이(같은 코드·설정) 팔. 기본값 = --baseline")
    ap.add_argument("--min-queries", type=int, default=MIN_QUERIES)
    cold = ap.add_mutually_exclusive_group()
    cold.add_argument("--cold-report", help="bench/cold_replay analyze 의 report.json - 속도 축의 cold 요청 지표를 합친다")
    cold.add_argument("--cold-unmeasured", metavar="REASON", help="cold 요청을 잴 수 없었던 사유 - 합격을 보류로 내린다")
    cold.add_argument("--cold-exempt", metavar="REASON",
                      help="사이드카로 가는 요청 모양(배치당 텍스트 수·요청 수)을 바꾸지 않는 후보의 cold 면제 사유 - warm 판정이 최종 판정이 된다")
    a = ap.parse_args(argv)
    rep = judge(a.run_dir, a.baseline, a.candidate, a.min_queries)
    if a.aa_control:
        aa = judge(a.run_dir, a.aa_twin or a.baseline, a.aa_control, a.min_queries)
        problems = aa_gate(aa)
        try:
            check_aa_twin(aa)
        except SystemExit as e:           # 쌍둥이가 아니면 run 무효(INCONCLUSIVE) - 종료 코드 1(FAIL)로 새지 않게
            problems.append(f"aa-not-twin: {e}")
        excess = aa_excess_of(aa) or 0.0
        if not problems:            # excess 는 문턱을 좁히기만, 꼬리 잡음폭은 옛 p95 허용 폭(2m) 안에서만 쓴다
            rep = judge(a.run_dir, a.baseline, a.candidate, a.min_queries, aa_excess=excess,
                        aa_noise=aa_noise_of(aa))
        rep["aa_control"] = {"arm": a.aa_control, "twin": a.aa_twin or a.baseline, "verdict": aa["verdict"],
                             "speed_ci95": aa["speed"]["ci95"], "excess": excess, "tail_noise": aa_noise_of(aa),
                             "problems": problems}
        # run 무효면 측정에 기대는 판정(합격·무변화·속도 악화)은 말하지 않는다. 정확도·비용 악화는 측정 조건과
        # 무관한 결정적 비교라 FAIL 로 남긴다.
        measured_fail = rep["verdict"] == "FAIL" and rep["accuracy"]["verdict"] != "regressed" \
            and rep["cost"]["verdict"] != "regressed"
        if problems and (rep["verdict"] in ("PASS", "NO-CHANGE") or measured_fail):
            rep["verdict"] = "INCONCLUSIVE"
            rep["reasons"].append("aa-invalid")
    elif rep["split"] == "holdout" and rep["verdict"] in ("PASS", "NO-CHANGE"):
        rep["verdict"] = "INCONCLUSIVE"
        rep["reasons"].append("holdout-verdict-needs-aa-control")
    rep["warm_verdict"] = rep["verdict"]
    if a.cold_report is not None:                 # 빈 문자열도 "주어짐" - 조용히 not-given 으로 새지 않게
        with open(a.cold_report) as fh:
            rep = combine_cold(rep, json.load(fh))
    elif a.cold_unmeasured is not None:
        rep = cold_unmeasured(rep, a.cold_unmeasured)
    elif a.cold_exempt is not None:               # 빈 사유도 면제 선언 - no-cold-input 으로 새지 않게
        rep = cold_exempt(rep, a.cold_exempt)
    else:                                         # cold 입력이 없으면 cold 를 모른다 - 속도 축이 비었으므로 보류
        rep = cold_unmeasured(rep, "no-cold-input")
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    return EXIT[rep["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
