"""recall 재생 하네스 - 동시성 탐침 (issue #13 B1).

runner 는 직렬이라 두 recall 이 같은 사이드카를 동시에 쓰는 경우를 재지 못한다. 사이드카는 MPS 워커가 하나라
요청을 줄 세우고, 데드라인(RERANK_TOTAL_DEADLINE_SECONDS 7.5s)은 대기 + 처리 합이다. upstream TEI 배치를
키워 recall 하나를 요청 하나로 보내면(B1) 직렬에서는 빨라지지만, 겹친 두 번째 recall 은 첫 번째 요청 전체를
기다린다 - 운영 사이드카 로그(2026-07-26~09-26)에서 포화 recall 5,819건 중 154건(2.6%)이 겹쳤다.
실측(2026-09-26, dev 20쌍): upstream TEI_MAX_CONCURRENT=1 이 인스턴스 안에서 TEI 요청을 줄 세워 두 번째 recall 은
사이드카가 아니라 upstream 세마포어에서 기다린다 - 사이드카 503 은 0 건이었다(데드라인은 요청 전송 뒤부터 잰다).

탐침: dev 쿼리 두 개를 같은 순간에 보내는 쌍을 설정(세트 게이트웨이)마다 번갈아 보내고, 쌍의 느린 쪽 지연과
오류를 설정별로 요약한다(사이드카 데드라인 위반은 upstream 재시도 뒤 실패 = 오류로 드러난다). 판정기가 아니라 근거 수집 도구다(트레이드오프 여부를 운영자가 본다).
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
from typing import Any, Dict, List, Optional


def summarize(pairs: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for cfg in sorted({p["config"] for p in pairs}):
        ps = [p for p in pairs if p["config"] == cfg]
        slower = [max(p["latencies_ms"]) for p in ps]
        out[cfg] = {"pairs": len(ps), "slower_median_ms": float(statistics.median(slower)),
                    "slower_max_ms": float(max(slower)),
                    "errors": sum(1 for p in ps for o in p["outcomes"] if o != "ok")}
    return out


def run(configs: Dict[str, int], n_pairs: int, seed: int, gap_s: float = 1.0) -> List[Dict[str, Any]]:
    from bench import runner                                   # tiktoken 은 안 쓴다(토큰은 재지 않는다)
    prov = runner.provenance(configs)
    clock = runner.common_bench_clock(prov["replicas"])
    corpus = runner.load_corpus("dev", None, seed)             # holdout 은 쓰지 않는다
    token = runner.read_token()
    clients = {c: [runner.McpClient(port, token), runner.McpClient(port, token)] for c, port in configs.items()}
    for pair in clients.values():
        for cl in pair:
            cl.open()
    names = sorted(configs)
    out = []
    for i in range(n_pairs):
        rows = [corpus[(2 * i) % len(corpus)], corpus[(2 * i + 1) % len(corpus)]]
        order = names if i % 2 == 0 else names[::-1]           # 설정 순서를 번갈아 - 앞뒤 자리 효과 균형
        for cfg in order:
            res: List[Optional[Dict[str, Any]]] = [None, None]
            barrier = threading.Barrier(2)

            def fire(j: int) -> None:
                args = dict(rows[j]["args"])
                args.setdefault("query_timestamp", clock)
                barrier.wait()
                raw = clients[cfg][j].recall(args)
                res[j] = {"latency_ms": raw["latency_ms"], "outcome": runner.classify(raw)["outcome"]}

            ts = [threading.Thread(target=fire, args=(j,)) for j in (0, 1)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            out.append({"config": cfg, "pair": i, "latencies_ms": [r["latency_ms"] for r in res],  # type: ignore[index]
                        "outcomes": [r["outcome"] for r in res]})                                 # type: ignore[index]
            time.sleep(gap_s)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", action="append", required=True, help="name=port (세트 게이트웨이)")
    ap.add_argument("--pairs", type=int, default=20)
    ap.add_argument("--seed", type=int, default=13)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    pairs = run({kv.split("=")[0]: int(kv.split("=")[1]) for kv in a.config}, a.pairs, a.seed)
    json.dump(pairs, open(a.out, "w"), indent=1)
    print(json.dumps(summarize(pairs), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
