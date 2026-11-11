"""1단계 arm 벤치 비교기 (issue #297, 스펙 #296 "벤치 판정").

한 run 안의 두 팔(base = D10 세트 팔, cand = arm 세트 팔)을 **속도와 비용 축만** 비교한다. judge.py 의 정확도 축
(결과 불변)은 관찰을 바꾸는 arm 에서 설계상 늘 changed 라서 쓰지 않는다. 결과가 바뀐 쿼리 비율은 보고만 한다.
runner 산출물(meta.json + calls.jsonl + prod_events.jsonl)을 읽고, 표본 정제(운영 간섭·가드 정지·재시도 쌍 제외)는
judge.py 와 같은 함수를 쓴다.

지표
  속도   쿼리별 (cand/base) 지연 비의 라운드 중앙값 -> 쿼리 전체 기하평균. 쿼리 단위 부트스트랩 95% CI.
  p95    섞은 p95 비 = p95(cand 지연 전체) / p95(base 지연 전체), 쌍 단위 부트스트랩 95% CI.
  토큰   쿼리별 라운드 중앙값의 합계(팔별)와 쿼리별 증가 분포(cand - base). 토큰은 결정적이라 허용 폭 0.
  변경   cand 결과 digest 집합이 base 와 다른 쿼리의 비율(보고만).
판정(문턱은 인자, 산식은 artifacts/arch-v2/r1-a-baseline.md 가 정본)
  speed ok  기하평균 비 <= 1 + speed_threshold      p95 ok  섞은 p95 비 <= 1 + p95_threshold
  cost ok   cand 토큰 합계 <= base 토큰 합계
  overall   셋 다 ok = PASS, 하나라도 아니면 FAIL
판정하지 않는 run(INVALID, exit 2) - 문턱 안이어도 다시 잰다
  meta.json 없음(예외·Ctrl-C 로 끝남) · 가드 중단 · 측정 호출 수 부족 · 측정 중 오류 응답(ok 가 아닌 outcome) 또는
  두 팔의 outcome 차이 · 복제본 동결 지문 변화 · rewrite 실패 증가 · 복원원 sha256 이 기대와 다름 · verify 보고서가 없거나
  pass 가 아님 · 오염 쌍 비율 초과 · 운영 간섭 신호 없음
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import os
import statistics
import sys
from typing import Any, Dict, List, Optional

from bench.judge import (MAX_CONTAMINATED, _contaminated, bootstrap_ci, geo_mean_ratio, load_guard_windows,
                         load_prod_windows, p95, quantile)

OK_OUTCOMES = ("ok", "ok-unstructured")
EXIT = {"PASS": 0, "FAIL": 1, "INVALID": 2}


def _r(x: Optional[float], n: int = 5) -> Optional[float]:
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(x, n)


def validity(run_dir: str, meta: Optional[Dict[str, Any]], meas: List[Dict[str, Any]], base: str, cand: str,
             expect_sha: Dict[str, str], verify: Dict[str, str]) -> List[str]:
    """판정하지 않을 사유 목록. 비면 유효한 run 이다."""
    if meta is None:
        return ["aborted:no-meta"]
    bad: List[str] = []
    aborted = (meta.get("guard") or {}).get("aborted")
    if aborted:
        bad.append(f"aborted:guard:{aborted}")
    want = (meta.get("n_queries") or 0) * (meta.get("rounds") or 0)
    for arm in (base, cand):
        got = sum(1 for c in meas if c["arm"] == arm)
        if got < want:
            bad.append(f"incomplete:{arm}:{got}<{want}")
    errors = [c for c in meas if c.get("outcome") not in OK_OUTCOMES]
    if errors:
        bad.append(f"error-responses:{len(errors)}")
    by = collections.defaultdict(dict)
    for c in meas:
        by[(c["key"], c["round"])][c["arm"]] = c.get("outcome")
    diff = sum(1 for v in by.values() if base in v and cand in v and v[base] != v[cand])
    if diff:
        bad.append(f"outcome-diff:{diff}")
    fb, fa = meta.get("freeze_before"), meta.get("freeze_after")
    if fb is None or fa is None or fb != fa:
        bad.append("replica-drift")
    rf_b, rf_a = meta.get("rewrite_failures_before") or {}, meta.get("rewrite_failures_after") or {}
    for arm in (base, cand):
        if rf_a.get(arm) is None or rf_b.get(arm) is None or rf_a[arm] > rf_b[arm]:
            bad.append(f"rewrite-failures:{arm}")
    prov = meta.get("provenance") or {}
    for arm in (base, cand):
        s = ((prov.get("arms") or {}).get(arm) or {}).get("set", "")
        got_sha = ((prov.get("replicas") or {}).get(s) or {}).get("backup_sha256")
        if not expect_sha.get(arm) or got_sha != expect_sha[arm]:
            bad.append(f"backup-sha256:{arm}")
        path = verify.get(arm)
        if not path or not os.path.isfile(path):
            bad.append(f"verify-missing:{arm}")
            continue
        try:
            ok = json.load(open(path)).get("pass") is True
        except (OSError, ValueError, AttributeError):
            ok = False
        if not ok:
            bad.append(f"verify-not-pass:{arm}")
    return bad


MIN_ROUNDS = 3              # 기준선 A/A 와 같은 조건(--rounds 3)
MIN_QUERIES = 40            # 기준선 A/A 와 같은 조건(--limit 40). 문턱은 이 표본 규모에서 정했다


def compare(run_dir: str, base: str, cand: str, expect_sha: Dict[str, str], verify: Dict[str, str],
            speed_threshold: float, p95_threshold: float, min_rounds: int = MIN_ROUNDS,
            min_queries: int = MIN_QUERIES) -> Dict[str, Any]:
    mp = os.path.join(run_dir, "meta.json")
    meta = json.load(open(mp)) if os.path.exists(mp) else None
    calls_path = os.path.join(run_dir, "calls.jsonl")
    calls = [json.loads(x) for x in open(calls_path) if x.strip()] if os.path.exists(calls_path) else []
    meas = [c for c in calls if c.get("phase") == "measure"]
    arms = set((meta or {}).get("arms") or {}) or {c["arm"] for c in meas}
    for a in (base, cand):
        if arms and a not in arms:          # meta 도 측정 행도 없는 중단 run 은 팔을 알 수 없다 - 아래에서 INVALID(aborted:no-meta)
            raise SystemExit(f"arm {a!r} is not in run {run_dir} (arms: {sorted(arms)})")
    invalid = validity(run_dir, meta, meas, base, cand, expect_sha, verify)
    if meta is not None:        # 표본 하한(judge.py MIN_ROUNDS·MIN_QUERIES 와 같은 뜻) - 작은 시험 run 을 판정하지 않는다
        if (meta.get("rounds") or 0) < min_rounds:
            invalid.append(f"too-few-rounds:{meta.get('rounds')}<{min_rounds}")
        if (meta.get("n_queries") or 0) < min_queries:
            invalid.append(f"too-few-queries:{meta.get('n_queries')}<{min_queries}")

    windows = load_prod_windows(run_dir, meta) if meta is not None else []
    if windows is None:
        invalid.append("no-prod-interference-signal")
    excl = (windows or []) + (load_guard_windows(meta) if meta is not None else [])
    rounds = collections.defaultdict(dict)
    for c in meas:
        rounds[(c["key"], c["round"])][c["arm"]] = c
    logs = collections.defaultdict(list)
    lat_pairs, total, dirty, unstructured = [], 0, 0, 0
    for (k, _r_), pair in sorted(rounds.items()):
        b, c = pair.get(base), pair.get(cand)
        if not b or not c or b.get("outcome") != "ok" or c.get("outcome") != "ok":
            if b and c and b.get("outcome") == c.get("outcome") == "ok-unstructured":
                unstructured += 1            # 결과 목록 없는 응답 - 속도 표본에서 빠진다(보고만)
            continue
        total += 1
        if _contaminated(b, excl) or _contaminated(c, excl):
            dirty += 1
            continue
        logs[k].append(math.log(c["latency_ms"] / b["latency_ms"]))
        lat_pairs.append((b["latency_ms"], c["latency_ms"]))
    contam = dirty / total if total else 1.0
    if contam > MAX_CONTAMINATED:
        invalid.append(f"contaminated:{contam:.2f}")
    per_q = [statistics.median(v) for v in logs.values() if v]
    ratio, lo, hi = bootstrap_ci(per_q, geo_mean_ratio)
    p_ratio, p_lo, p_hi = bootstrap_ci(lat_pairs, lambda ps: p95([c for _, c in ps]) / p95([b for b, _ in ps])
                                       if ps else math.nan)

    keys = sorted({c["key"] for c in meas})
    tok = {}
    for k in keys:
        bt = [c["tokens"] for c in meas if c["key"] == k and c["arm"] == base and "tokens" in c]
        ct = [c["tokens"] for c in meas if c["key"] == k and c["arm"] == cand and "tokens" in c]
        if bt and ct:
            tok[k] = (statistics.median(bt), statistics.median(ct))
    deltas = [c - b for b, c in tok.values()]
    tb, tc = sum(b for b, _ in tok.values()), sum(c for _, c in tok.values())
    changed = 0
    for k in keys:
        bd = {c.get("results_digest") for c in meas if c["key"] == k and c["arm"] == base}
        cd = {c.get("results_digest") for c in meas if c["key"] == k and c["arm"] == cand}
        changed += bd != cd
    base_nondet = sum(1 for k in keys if len({c.get("results_digest") for c in meas
                                              if c["key"] == k and c["arm"] == base}) > 1)

    speed_ok = not math.isnan(ratio) and ratio <= 1 + speed_threshold
    p95_ok = not math.isnan(p_ratio) and p_ratio <= 1 + p95_threshold
    cost_ok = bool(tok) and tc <= tb
    verdict = {"speed": "ok" if speed_ok else "regressed", "p95": "ok" if p95_ok else "regressed",
               "cost": "ok" if cost_ok else "regressed"}
    verdict["overall"] = "INVALID" if invalid else ("PASS" if speed_ok and p95_ok and cost_ok else "FAIL")
    return {
        "run_id": (meta or {}).get("run_id", os.path.basename(os.path.normpath(run_dir))), "base": base, "cand": cand,
        "valid": not invalid, "invalid_reasons": invalid,
        "bench_clock": (meta or {}).get("bench_clock"), "bench_clock_common": (meta or {}).get("bench_clock_common"),
        "thresholds": {"speed": speed_threshold, "p95": p95_threshold, "tokens": 0},
        "speed": {"geo_mean_ratio": _r(ratio), "ci95": [_r(lo), _r(hi)], "n_queries": len(per_q),
                  "pairs": total, "contaminated_pairs": dirty, "unstructured_pairs": unstructured, "contamination_rate": _r(contam, 4),
                  "base_median_ms": _r(statistics.median([b for b, _ in lat_pairs])) if lat_pairs else None,
                  "cand_median_ms": _r(statistics.median([c for _, c in lat_pairs])) if lat_pairs else None},
        "p95": {"ratio": _r(p_ratio), "ci95": [_r(p_lo), _r(p_hi)], "n_pairs": len(lat_pairs)},
        "tokens": {"base_total": tb, "cand_total": tc, "rel_change": _r((tc - tb) / tb) if tb else None,
                   "queries": len(tok), "queries_increased": sum(d > 0 for d in deltas),
                   "queries_decreased": sum(d < 0 for d in deltas),
                   "per_query_delta": {"min": min(deltas), "p50": quantile(deltas, 0.5), "p95": quantile(deltas, 0.95),
                                       "max": max(deltas)} if deltas else None},
        "changed": {"queries": changed, "share": _r(changed / len(keys)) if keys else None,
                    "base_nondeterministic": base_nondet},
        "verdict": verdict,
    }


def _pairs(xs: List[str], what: str) -> Dict[str, str]:
    out = {}
    for x in xs:
        if "=" not in x:
            raise SystemExit(f"{what} must be arm=value: {x!r}")
        k, v = x.split("=", 1)
        out[k] = v
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run_dir")
    ap.add_argument("--base", default="base")
    ap.add_argument("--cand", default="cand")
    ap.add_argument("--expect-sha", action="append", default=[], help="arm=<복원원 sha256> (D10 또는 LINEAGE 값)")
    ap.add_argument("--verify", action="append", default=[], help="arm=<그 팔 세트의 replica.py verify 출력 JSON>")
    ap.add_argument("--speed-threshold", type=float, required=True, help="기하평균 지연 비 허용 폭(예: 0.01 = +1%%)")
    ap.add_argument("--p95-threshold", type=float, required=True, help="섞은 p95 비 허용 폭(예: 0.04 = +4%%)")
    ap.add_argument("--min-rounds", type=int, default=MIN_ROUNDS, help="이보다 라운드가 적은 run 은 INVALID")
    ap.add_argument("--min-queries", type=int, default=MIN_QUERIES, help="이보다 쿼리가 적은 run 은 INVALID")
    a = ap.parse_args(argv)
    rep = compare(a.run_dir, a.base, a.cand, _pairs(a.expect_sha, "--expect-sha"), _pairs(a.verify, "--verify"),
                  a.speed_threshold, a.p95_threshold, a.min_rounds, a.min_queries)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    return EXIT[rep["verdict"]["overall"]]


if __name__ == "__main__":
    sys.exit(main())
