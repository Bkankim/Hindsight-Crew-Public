"""리랭크 후보 상한(HINDSIGHT_API_RERANKER_MAX_CANDIDATES) 분석 (issue #13).

upstream 은 RRF 상위 cap 개만 크로스인코더에 넣고, 최종 점수는 후보마다 독립적으로 계산한다
(reranking.py: rrf_normalized=0 고정, combined = CE x recency x temporal x proof). 그래서 한 쿼리의
반환 결과가 모두 RRF 순위 cap 이내면, 상한을 cap 으로 낮춰도 그 쿼리의 결과는 같다.

전제 두 가지(어긋나면 이 분석은 무효):
- 상한 절단은 boosted_rrf_score 순이다. HINDSIGHT_API_RECALL_STRATEGY_BOOSTS 가 비어 있어야(기본값) RRF 순위와 같다.
- 크로스인코더 점수가 배치 구성과 무관해야 한다. 2026-09-25 실측(dev 7쿼리, fp16 MPS)에서 배치 크기·요청 분할·
  길이 정렬을 바꿔도 점수가 bit 단위로 같았다 - 경험적 근거이지 보장은 아니다. 채택 판정은 하네스로 한다.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Sequence


def required_cap(trace: Dict[str, Any]) -> Optional[int]:
    """이 쿼리의 결과가 불변이려면 필요한 최소 상한 = 반환 결과 중 가장 깊은 RRF 순위.
    결과가 없으면 0(어느 상한이든 불변), 출처 순위를 모르는 결과가 있으면 None(증명 불가)."""
    ranks = [r.get("rrf_rank") for r in trace["final_results"]]
    if any(k is None for k in ranks):
        return None
    return max(ranks, default=0)


def profile(required: Sequence[Optional[int]], caps: Sequence[int]) -> Dict[int, Dict[str, int]]:
    """상한별로 결과가 불변인 쿼리 수. 증명 불가(None) 쿼리는 어느 상한에서도 불변으로 세지 않는다."""
    return {c: {"unchanged": sum(1 for r in required if r is not None and r <= c), "total": len(required)}
            for c in caps}


# ---------------------------------------------------------------- CLI (복제본 실측 - 단위 테스트 대상 아님)

_TRACE_RECALL = r'''
import sys,json,urllib.request as u
k=sys.stdin.readline().strip(); body=sys.stdin.read()
d=json.loads(u.urlopen(u.Request("http://localhost:8888/v1/default/banks/personal-demo/memories/recall",data=body.encode(),
  headers={"Authorization":"Bearer "+k,"Content-Type":"application/json"}),timeout=60).read())
t=d.get("trace") or {}
print(json.dumps({"final_results":[{"id":r.get("id"),"rrf_rank":r.get("rrf_rank")} for r in t.get("final_results") or []],
                  "reranked":len(t.get("reranked") or []),"n_results":len(d.get("results") or [])}))
'''


def main(argv: Optional[Sequence[str]] = None) -> int:
    """동결 복제본에 dev 쿼리를 trace 로 재생해 쿼리별 필요 상한과 상한별 불변 비율을 낸다(본문은 남기지 않는다)."""
    import argparse
    import json
    import os
    import subprocess

    from bench import replica, runner

    ap = argparse.ArgumentParser(description=main.__doc__)
    ap.add_argument("--split", choices=("dev",), default="dev")      # holdout 은 판정 전용이라 분석에 쓰지 않는다
    ap.add_argument("--limit", type=int)
    ap.add_argument("--caps", default="50,100,150,200,250,300")
    ap.add_argument("--out", default=os.path.join(runner.REPO, "bench", ".data", "cap-profile.json"))
    a = ap.parse_args(argv)

    clock, key = runner.bench_clock(), replica.bench_key()
    rows = []
    for row in runner.load_corpus(a.split, a.limit, seed=13):
        args = dict(row["args"], trace=True, query_timestamp=clock)
        out = subprocess.run(["docker", "exec", "-i", replica.HS, "python", "-c", _TRACE_RECALL],
                             input=key + "\n" + json.dumps(args), capture_output=True, text=True, check=True).stdout
        t = json.loads(out)
        rows.append({"key": row["key"], "required_cap": required_cap(t), "reranked": t["reranked"],
                     "n_results": t["n_results"]})
    caps = [int(c) for c in a.caps.split(",")]
    report = {"split": a.split, "bench_clock": clock, "queries": len(rows), "profile": profile([r["required_cap"] for r in rows], caps),
              "rows": rows}
    with open(a.out, "w") as fh:
        json.dump(report, fh, indent=1)
    print(json.dumps({k: report[k] for k in ("split", "queries", "profile")}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
