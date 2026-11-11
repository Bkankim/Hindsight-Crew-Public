"""cold 재생 도구 (issue #20, #13 티켓 7).

B1 이 바꾸는 사이드카 요청 모양(upstream TEI 배치 128 -> 300: 포화 recall 한 번이 128x3 순차 요청에서 300x1 요청 하나로)의
**cold 요청** 지연을 분리해 잰다. 실제 recall 후보 텍스트를 **벤치 사이드카**에 직접 보내고, 운영 사이드카(8787)는 쓰지 않는다.

세 부분
- 계획(순수): 두 방식을 ABBA 로 교대 배치한다. 한 쌍은 같은 후보 텍스트로 두 방식을 잰다. 매 프로브 전 idle 을 보장한다.
- 실행(IO 경계): 사이드카 HTTP 호출만. 벤치 간섭 가드(#19) 아래서 돈다. idle 을 깬 다른 요청이 있으면 그 프로브를 표시한다.
  keep-warm(#79): 도는 동안 idle 대기 걸음의 가드 확인 뒤마다 운영 사이드카에 아주 작은 요청을 간격마다 보낸다(bench/keepwarm.py). 그 요청은 운영 rerank
  구간에서 빼므로 프로브를 `idle-broken` 으로 만들지 않는다.
- 분석(순수): 방식별 cold 지연 분포, 7.5s 데드라인 초과율, upstream 재시도 모델을 적용한 recall 단위 지연,
  판정기 noninferiority 가 받는 (비, CI 하한, 상한) 지표.
"""
from __future__ import annotations

import random
import re
from typing import Any, Dict, List, Optional, Tuple

from bench.keepwarm import KeepWarm

MODES = {"128x3": 128, "300x1": 300}   # 방식 이름 -> upstream TEI 배치 크기(포화 recall 후보 300 기준)
BASELINE, CANDIDATE = "128x3", "300x1"
IDLE_S = 300.0                          # cold 요청 = 직전 rerank 요청 뒤 5분 이상 idle (CONTEXT.md)
N_PER_MODE = 40


# ---------------------------------------------------------------- 계획

def plan(n_per_mode: int = N_PER_MODE, n_inputs: int = 1, seed: int = 20) -> List[Dict[str, Any]]:
    """프로브 목록. 쌍 k 는 같은 입력으로 두 방식을 한 번씩 잰다. 순서는 ABBA 로 번갈아 시간 드리프트가 한 방식에
    몰리지 않게 한다. 입력은 seed 로 섞은 순서대로 쓰고, 쌍이 입력보다 많으면 돌려 쓴다."""
    if n_per_mode < 1 or n_inputs < 1:
        raise ValueError("n_per_mode and n_inputs must be >= 1")
    order = list(range(n_inputs))
    random.Random(seed).shuffle(order)
    out: List[Dict[str, Any]] = []
    for k in range(n_per_mode):
        modes = (BASELINE, CANDIDATE) if k % 2 == 0 else (CANDIDATE, BASELINE)
        for m in modes:
            out.append({"probe": len(out), "pair": k, "mode": m, "input": order[k % n_inputs]})
    return out


def idle_wait(now: float, last_activity: float, idle_s: float = IDLE_S) -> float:
    """다음 프로브 전에 더 기다려야 하는 초. last_activity = 이 사이드카의 마지막 rerank 요청이 끝난 시각."""
    return max(0.0, idle_s - (now - last_activity))


def split(texts: List[str], mode: str) -> List[List[str]]:
    """upstream RemoteTEICrossEncoder 처럼 후보를 순서대로 배치 크기씩 자른다(128x3 = 128+128+44)."""
    bs = MODES[mode]
    return [texts[i:i + bs] for i in range(0, len(texts), bs)]


def estimate_s(probes: List[Dict[str, Any]], idle_s: float = IDLE_S, probe_s: float = 10.0) -> float:
    """계획 소요 시간 추정: 프로브마다 idle + 프로브 자체(cold + warm 반복)."""
    return len(probes) * (idle_s + probe_s)


# ---------------------------------------------------------------- 분석

DEADLINE_MS = 7500.0     # 사이드카 RERANK_TOTAL_DEADLINE_SECONDS: 도착부터 대기 + 계산이 이를 넘으면 503
MAX_RETRIES = 3          # upstream RemoteTEICrossEncoder: 5xx·ReadTimeout 에 최대 3회 재시도
BACKOFF_MS = 500.0       # 0.5s 에서 시작해 2배씩(지터는 위로만 붙으므로 뺀 값 = 하한)


def retry_model(cold_ms: List[float], warm_ms: List[float], deadline_ms: float = DEADLINE_MS,
                max_retries: int = MAX_RETRIES, backoff_ms: float = BACKOFF_MS) -> Dict[str, Any]:
    """재생에서 잰 사이드카 요청 지연(데드라인을 크게 둔 벤치 사이드카라 잘리지 않은 값)을 recall 하나가 겪는 지연으로 옮긴다.

    upstream 은 요청을 세마포어 1 아래 순서대로 보낸다(동시성 1). 사이드카는 데드라인이 지나면 503 을 돌려주지만 이미
    시작한 계산은 끝까지 돌린다(rerank-server/server.py) - 재시도는 그 뒤에 줄을 서고, 줄에서 데드라인을 넘기면 계산 없이
    skip 된다. 첫 시도는 cold 지연, 재시도는 같은 요청의 warm 지연으로 계산한다. 한 요청이 재시도를 다 쓰면 recall 오류다."""
    t = busy = 0.0                 # t = 클라이언트 시계, busy = 사이드카가 앞선 계산을 끝내는 시각
    attempts: List[int] = []
    for cold, warm in zip(cold_ms, warm_ms):
        delay, n = backoff_ms, 0
        while True:
            n += 1
            start = max(t, busy)
            if start - t >= deadline_ms:           # 줄에서만 데드라인을 넘김 - worker 가 settled job 을 건너뛴다
                done = None
            else:
                busy = start + (cold if n == 1 else warm)
                done = busy if busy - t <= deadline_ms else None
            if done is not None:
                t = done
                break
            t += deadline_ms                       # 503 을 받은 시각
            if n > max_retries:
                attempts.append(n)
                return {"ms": t, "attempts": attempts, "failed": True}
            t += delay
            delay *= 2
        attempts.append(n)
    return {"ms": t, "attempts": attempts, "failed": False}


def exclusions(r: Dict[str, Any], idle_s: float = IDLE_S) -> List[str]:
    """프로브가 깨끗한 cold 요청이 아닌 이유. idle-broken = 직전 idle 창에 다른 rerank 요청(벤치 사이드카의 남의 요청,
    또는 같은 MPS 장치를 쓰는 운영 사이드카의 rerank)이 있었거나 idle 이 모자람. interference = 프로브 도중 그런 요청이
    끼었거나 벤치 간섭 가드 정지 구간과 겹침 - 정지가 idle 대기 안에만 있어도 뺀다(정지 사유가 swapout 이면 벤치
    사이드카 가중치가 밀려났을 수 있어 cold 가 아니라 스왑 복귀를 재게 된다). error = 사이드카가 200 이 아닌 응답,
    no-response = 응답 없음(연결 실패·타임아웃)."""
    out = []
    if r.get("foreign_before", 0) or r.get("prod_before", 0) or r.get("idle_before_s", 0.0) < idle_s:
        out.append("idle-broken")
    if r.get("foreign_during", 0) or r.get("prod_during", 0) or r.get("guard_overlap"):
        out.append("interference")
    status = r.get("status") or []
    if any(s == 0 for s in status):
        out.append("no-response")                     # 연결 실패·타임아웃 - 사이드카의 응답이 아니라 인프라 문제
    elif any(s != 200 for s in status) or not r.get("cold_ms"):
        out.append("error")
    if r.get("prod_before", 0) is None or r.get("prod_during", 0) is None:
        out.append("no-prod-signal")                  # 운영 로그를 못 읽었다 - 겹침을 모르면 깨끗하다고 보지 않는다
    return out


COLD_QUANTILES = {"cold_p50": 0.50, "cold_p95": 0.95}
MIN_PAIRS = 20           # 깨끗한 쌍이 이보다 적으면 지표는 hold. 쌍 1개면 부트스트랩 CI 폭이 0 이라 ok 로 새어 나간다
                         # (2026-09-26 시험 실행). 계획 40 쌍의 절반 - p95 가 위 2개 사이 보간이 되는 최소치


def _dist(xs: List[float]) -> Dict[str, Optional[float]]:
    from bench.judge import quantile
    if not xs:
        return {"p50": None, "p90": None, "p95": None, "max": None}
    return {"p50": quantile(xs, 0.5), "p90": quantile(xs, 0.9), "p95": quantile(xs, 0.95), "max": max(xs)}


def analyze(records: List[Dict[str, Any]], idle_s: float = IDLE_S, deadline_ms: float = DEADLINE_MS,
            tolerance: Optional[float] = None, guard_windows: Optional[List[Any]] = None,
            prod_intervals: Any = "not-given") -> Dict[str, Any]:
    """재생 기록 -> 방식별 분포와 cold 지표. 지표는 쌍(같은 입력)마다 recall 단위 지연을 짝지어 후보(300x1)/기준(128x3)
    분위수 비와 부트스트랩 95% CI 를 낸다 - 판정기 noninferiority(ratio, lo, hi, noise) 가 그대로 받는 모양이다.
    verdict 는 tolerance(기본 판정기 허용 폭 m)로 미리 계산한 참고값이고, 최종 허용 폭은 판정 run(#21)이 정한다.
    깨끗한 쌍이 MIN_PAIRS 보다 적으면 verdict 는 hold 이고 short 에 모자란 쌍 수를 싣는다(보류 사유 = 표본 부족)."""
    from bench.judge import SPEED_MARGIN, bootstrap_ci, noninferiority, quantile
    tol = SPEED_MARGIN if tolerance is None else tolerance
    excluded, clean = [], []
    for r in sorted(records, key=lambda x: x["probe"]):
        if guard_windows and r.get("t_end") is not None:   # 가드 창은 마지막 정상 표본 시각으로 거슬러 기록된다 -
            lo = r["t_start"] - r.get("idle_before_s", 0.0)  # 기록 뒤에 생긴 창이 이미 쓴 프로브를 덮을 수 있다
            r = dict(r, guard_overlap=bool(r.get("guard_overlap")) or _overlaps(guard_windows, lo, r["t_end"]))
        if prod_intervals != "not-given" and r.get("t_end") is not None:   # run 끝의 운영 구간으로 다시 센다
            lo = r["t_start"] - r.get("idle_before_s", 0.0)
            if prod_intervals is None:
                r = dict(r, prod_before=None, prod_during=None)
            elif r.get("prod_before") is not None and r.get("prod_during") is not None:
                r = dict(r, prod_before=max(r["prod_before"], sum(1 for b in prod_intervals if _overlaps([b], lo, r["t_start"]))),
                         prod_during=max(r["prod_during"], sum(1 for b in prod_intervals if _overlaps([b], r["t_start"], r["t_end"]))))
        why = exclusions(r, idle_s)
        if why:
            excluded.append({"probe": r["probe"], "pair": r["pair"], "mode": r["mode"], "reasons": why})
        else:
            clean.append(dict(r, model=retry_model(r["cold_ms"], r["warm_ms"], deadline_ms)))
    modes = {}
    for m in MODES:
        rs = [r for r in clean if r["mode"] == m]
        over = [sum(1 for x in r["cold_ms"] if x > deadline_ms) for r in rs]
        modes[m] = {"n": len(rs),
                    "cold_request_ms": _dist([r["cold_ms"][0] for r in rs]),     # idle 뒤 첫 사이드카 요청
                    "probe_ms": _dist([sum(r["cold_ms"]) for r in rs]),          # 재시도 없이 요청들을 이은 합
                    "warm_probe_ms": _dist([sum(r["warm_ms"]) for r in rs]),     # 같은 입력을 곧바로 다시 보낸 합
                    "recall_ms": _dist([r["model"]["ms"] for r in rs]),          # 데드라인·재시도 모델 적용
                    "deadline_exceeded": {"requests": sum(over), "probes": sum(1 for o in over if o),
                                          "rate": (sum(1 for o in over if o) / len(rs)) if rs else None},
                    "retried_probes": sum(1 for r in rs if any(a > 1 for a in r["model"]["attempts"])),
                    "failed_recalls": sum(1 for r in rs if r["model"]["failed"]),
                    # 사이드카가 200 이 아닌 응답을 준 프로브 - 쌍에서 빠지므로 여기서 세지 않으면 방식의 실패가 hold 로 숨는다.
                    # 간섭·신호 없음과 겹친 것은 방식 탓으로 단정할 수 없어 세지 않는다(no-response 는 인프라라 따로 뺀다)
                    "errors": sum(1 for x in excluded if x["mode"] == m and x["reasons"] == ["error"])}
    by_pair: Dict[int, Dict[str, float]] = {}
    for r in clean:
        by_pair.setdefault(r["pair"], {})[r["mode"]] = r["model"]["ms"]
    paired = [(v[BASELINE], v[CANDIDATE]) for _, v in sorted(by_pair.items()) if len(v) == 2]
    metrics = {}
    for name, q in COLD_QUANTILES.items():
        ratio, lo, hi = bootstrap_ci(
            paired, lambda ps, q=q: quantile([c for _, c in ps], q) / quantile([b for b, _ in ps], q) if ps else float("nan"))
        short = max(0, MIN_PAIRS - len(paired))
        metrics[name] = {"ratio": ratio, "ci95": [lo, hi], "n": len(paired), "short": short, "tolerance": tol,
                         "verdict": "hold" if short else noninferiority(ratio, lo, hi, tol)}
    # recall 실패(재시도 소진)는 분위수 비가 가린다(40 쌍 중 1건이면 p95 밖) - 실패 수는 따로 판정한다
    fb, fc = (modes[m]["failed_recalls"] + modes[m]["errors"] for m in (BASELINE, CANDIDATE))
    failures = {"baseline": fb, "candidate": fc, "verdict": "regressed" if fc > fb else "ok"}
    return {"baseline": BASELINE, "candidate": CANDIDATE, "idle_s": idle_s, "deadline_ms": deadline_ms,
            "cold_definition_met": idle_s >= IDLE_S, "modes": modes, "metrics": metrics, "failures": failures,
            "excluded": excluded}


# ---------------------------------------------------------------- 실행 (IO 경계)

PROD_SIDECAR_PORT = 8787
MIN_DEADLINE_S = 60.0    # 벤치 사이드카 데드라인 하한: 운영 7.5s 그대로면 긴 cold 가 503 으로 잘려 지연을 모른다
GUARD_STEP_S = 10.0      # idle 대기 중 가드를 보는 간격(가드 표본 간격과 같다)
PROD_SETTLE_S = 30.0     # run 끝 운영 로그 재독 전 대기: 운영 rerank 는 데드라인(7.5s) 뒤에도 끝까지 계산한다(관측 최대 28s)
SIDECAR_OVERRIDES = ("RERANK_MAX_TEXTS", "RERANK_TOTAL_DEADLINE_SECONDS", "RERANK_QUEUE_TIMEOUT_SECONDS")


def bench_sidecar(port: int, states: List[Dict[str, Any]]) -> Dict[str, Any]:
    """재생 대상 = `replica.py sidecar-up` 으로 띄운 벤치 사이드카만. 운영 사이드카(8787)는 어떤 경우에도 거부한다.
    데드라인은 크게 둔 것만 받는다(분석이 7.5s 데드라인과 재시도를 모델로 적용한다 - 데드라인 값은 대기 상한일 뿐
    계산 시간은 바꾸지 않는다). 300 텍스트 요청이 413 이 되지 않게 RERANK_MAX_TEXTS 도 본다."""
    if port == PROD_SIDECAR_PORT:
        raise SystemExit(f"cold replay never calls the production sidecar ({PROD_SIDECAR_PORT})")
    hits = [s for s in states if s.get("port") == port]
    if len(hits) != 1:
        raise SystemExit(f"no single bench sidecar state on port {port} - run `replica.py sidecar-up` first")
    ov = hits[0].get("overrides") or {}
    extra = sorted(set(ov) - set(SIDECAR_OVERRIDES))
    if extra:        # 배치·max_length·idle_trim 등은 계산 시간이나 cold 상태를 바꿔 운영과 다른 지연을 잰다
        raise SystemExit(f"bench sidecar overrides beyond {SIDECAR_OVERRIDES} change latency vs production: {extra}")

    def num(k: str) -> float:
        try:
            return float(ov.get(k, "nan"))
        except ValueError:
            return float("nan")

    for k in ("RERANK_TOTAL_DEADLINE_SECONDS", "RERANK_QUEUE_TIMEOUT_SECONDS"):
        if not num(k) >= MIN_DEADLINE_S:
            raise SystemExit(f"bench sidecar needs --env {k}>={MIN_DEADLINE_S:g} (untruncated cold latency)")
    if not num("RERANK_MAX_TEXTS") >= MODES[CANDIDATE]:
        raise SystemExit(f"bench sidecar needs --env RERANK_MAX_TEXTS>={MODES[CANDIDATE]} (300x1 would be 413)")
    return hits[0]


def _overlaps(windows: List[Any], lo: float, hi: float) -> bool:
    return any(a <= hi and b >= lo for a, b in windows)


def execute(probes: List[Dict[str, Any]], inputs: List[Dict[str, Any]], sidecar: Dict[str, Any], *, guard: Any,
            post: Any, activity: Any, now: Any, sleep: Any, prod_mark: Any, prod_errors: Any,
            idle_s: float = IDLE_S, on_record: Any = None, prod_busy: Any = None, keepwarm: Any = None) -> Any:
    """계획대로 벤치 사이드카에 재생한다. 프로브마다: idle 대기(GUARD_STEP_S 마다 guard.check, 남의 요청이 보이면 대기를
    처음부터 다시) -> guard.check ->
    cold 요청(방식대로 순서대로) -> 같은 요청을 곧바로 한 번 더(warm, 재시도 모델의 재시도 비용).
    activity() = 벤치 사이드카가 지금까지 받은 rerank 요청 수(로그 줄). 우리 요청 외 증가분이 곧 idle 을 깬 남의 요청이다.
    prod_busy(mark) = 표지 이후 운영 사이드카 rerank 구간 목록(같은 MPS 장치) - idle 창·측정과 겹친 수를 기록에 싣는다.
    도구 시작 시점도 직전 활동으로 보고 첫 프로브 전에도 idle_s 를 기다린다. 가드가 중단하면 거기서 멈추고 meta 를 남긴다.
    keepwarm(bench/keepwarm.KeepWarm, issue #79) = 주면 도는 동안 idle 대기 걸음의 가드 확인 뒤마다 tick() 한다(프로브 측정 중에는 보내지 않고,
    프로브 앞 가드 확인은 한 걸음 넘게 막혔을 때만 tick 한다).
    그 요청은 운영 rerank 구간에서 뺀다(자기 keep-warm 이 모든 프로브를 idle-broken 으로 만들지 않게)."""
    from bench.guard import GuardAbort
    port = sidecar["port"]
    mark = prod_mark()
    started = last = now()
    seen = activity()
    if keepwarm is not None:
        keepwarm.start()

    def busy_since(m: Any) -> Any:
        b = prod_busy(m)
        if b is not None and keepwarm is not None:
            b = keepwarm.without_own(b)
        return None if b is None else [(iv[0], iv[1]) for iv in b]        # 후보 수는 자기 요청을 거르는 데만 쓴다

    records: List[Dict[str, Any]] = []
    aborted = None
    try:
        for p in probes:
            restarts = 0
            while True:
                w = idle_wait(now(), last, idle_s)
                if w <= 0:
                    break
                sleep(min(w, GUARD_STEP_S))
                guard.check()
                if keepwarm is not None:
                    keepwarm.tick()                    # 가드 확인 뒤 - 시작 때 가드가 중단이면 보내지 않고, 정지에서 재개한 직후에도 데운다
                cur = activity()
                if cur > seen:                     # 남의 요청이 idle 을 깼다 - 감지 시각부터 다시 기다린다
                    last, seen, restarts = now(), cur, restarts + 1
            chk = now()
            guard.check()
            if keepwarm is not None and now() - chk >= GUARD_STEP_S:
                keepwarm.tick()                        # 프로브 앞 가드가 한 걸음 넘게 막았다 재개 - 그동안 idle 이던 운영 사이드카를 데운다
            inp = inputs[p["input"]]
            t_start = now()
            before = activity()
            chunks = split(inp["texts"], p["mode"])
            cold, warm, status = [], [], []
            for phase in (cold, warm):
                for ch in chunks:
                    code, ms = post(port, inp["query"], ch)
                    phase.append(ms)
                    status.append(code)
                    if code != 200:
                        break
                if status[-1] != 200:
                    break
            t_end = now()
            after = activity()
            busy = busy_since(mark) if prod_busy else []     # None = 운영 로그를 못 읽음
            rec = {**p, "key": inp.get("key"), "t_start": t_start, "t_end": t_end, "idle_before_s": t_start - last,
                   "idle_restarts": restarts,
                   "cold_ms": cold, "warm_ms": warm, "status": status,
                   "foreign_before": max(0, before - seen), "foreign_during": max(0, after - before - len(status)),
                   "prod_before": None if busy is None else sum(1 for b in busy if _overlaps([b], last, t_start)),
                   "prod_during": None if busy is None else sum(1 for b in busy if _overlaps([b], t_start, t_end)),
                   "guard_overlap": _overlaps(guard.windows, last, t_end)}
            records.append(rec)
            if on_record:
                on_record(rec)
            # access log 가 우리 마지막 요청 줄을 아직 안 썼으면 그 줄이 다음 idle 창의 남의 요청으로 세지지 않게 한다
            last, seen = t_end, max(after, before + len(status))
    except GuardAbort as e:
        aborted = str(e)
    except (Exception, KeyboardInterrupt) as e:      # 7시간 run 이 도중에 끊겨도 meta(가드·운영 집계)는 남긴다
        aborted = f"error:{type(e).__name__}"
    if keepwarm is not None:
        keepwarm.stop()                                # 마지막 프로브가 끝났다 - 뒤의 로그 재독 대기에는 보내지 않는다
    prod_final: Optional[List[Tuple[float, float]]] = []
    if prod_busy:
        try:
            if records:
                sleep(PROD_SETTLE_S)                  # 마지막 프로브 끝에 돌던 운영 rerank 가 끝나 로그에 찍히기를 기다린다
            prod_final = busy_since(mark)
        except (Exception, KeyboardInterrupt):        # 못 읽으면 신호 없음(닫힌 쪽) - meta 는 남긴다
            prod_final = None
    meta = {"sidecar": {k: sidecar.get(k) for k in ("name", "port", "overrides")}, "idle_s": idle_s,
            "started_at": started, "ended_at": now(), "probes_planned": len(probes), "probes_done": len(records),
            "aborted": aborted, "guard": guard.meta(), "prod_sidecar": prod_errors(mark),
            # 운영 rerank 줄은 계산이 끝나야 찍혀 프로브 끝에 돌던 것은 그때 안 보인다 - 끝에서 전체를 다시 싣는다(None = 못 읽음)
            "prod_intervals": prod_final,
            "keepwarm": None if keepwarm is None else keepwarm.meta("cold 재생"),   # None = 꺼짐
            # prod_sidecar 에는 keep-warm 자신의 실패도 들어 있다 - 운영 영향을 볼 때 이 수를 뺀다
            "prod_sidecar_keepwarm": None if keepwarm is None else keepwarm.own_sidecar_errors()}
    return records, meta


# ---------------------------------------------------------------- 입력 (캠페인 고정 입력의 holdout recall 후보)

def reranker_inputs(trace: Dict[str, Any], occurred: Dict[str, Optional[str]]) -> Dict[str, Any]:
    """recall trace 에서 upstream 이 사이드카에 보낸 (query, texts) 를 다시 만든다. 순서 = RRF 순위(upstream 은
    RRF 정렬 뒤 상한 300 에서 자르고 그 순서로 배치를 나눈다). 문서 = "[Date: %B %d, %Y (%Y-%m-%d)] {context}: {text}"
    (reranking.py, occurred_start·context 가 없으면 그 부분 생략).
    날짜는 occurred(node_id -> occurred_start, 복제본 memories/list)에서만 가져온다. trace 의 event_date 는 다른 칼럼이라
    occurred_start 가 없는 기억에도 값이 있다 - 이것으로 붙이면 2026-09-26 시험에서 61/300 이 운영 점수와 어긋났다.
    context 는 trace 의 retrieval_results 에서 node_id 로 찾는다(reranked 에는 없다). occurred 에 없는 후보는 KeyError."""
    import datetime as dt
    ctx: Dict[str, str] = {}
    for method in trace.get("retrieval_results") or []:
        for r in method.get("results") or []:
            ctx.setdefault(r["node_id"], r.get("context") or "")
    texts = []
    for r in sorted(trace["reranked"], key=lambda x: x["rrf_rank"]):
        doc = r["text"]
        if ctx.get(r["node_id"]):
            doc = f"{ctx[r['node_id']]}: {doc}"
        start = occurred[r["node_id"]]
        if start:
            d = dt.datetime.fromisoformat(start)
            doc = f"[Date: {d.strftime('%B %d, %Y')} ({d.strftime('%Y-%m-%d')})] {doc}"
        texts.append(doc)
    return {"query": trace["query"]["query_text"], "texts": texts}


_PROCESSED = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3}) INFO rerank_processed (?:candidates=(\d+) )?.*?elapsed_ms=([\d.]+)", re.M)


def prod_rerank_requests(text: str) -> List[Tuple[float, float, Optional[int]]]:
    """운영 사이드카 로그 본문 -> (시작, 끝, 후보 수) epoch 초. 줄 시각(호스트 현지 시각)은 계산 끝이라 시작 = 끝 - elapsed.
    후보 수는 줄에 candidates= 가 없으면 None (keep-warm 의 자기 요청 판별용, issue #79)."""
    import datetime as dt
    out = []
    for ts, ms, cand, elapsed in _PROCESSED.findall(text):
        end = dt.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").timestamp() + int(ms) / 1000
        out.append((end - float(elapsed) / 1000, end, int(cand) if cand else None))
    return out


def prod_rerank_intervals(text: str) -> List[Tuple[float, float]]:
    """운영 사이드카 로그 본문 -> rerank 계산 구간(epoch 초). 줄 시각(호스트 현지 시각)은 계산 끝이라 시작 = 끝 - elapsed."""
    return [(s, e) for s, e, _ in prod_rerank_requests(text)]


def recall_args(row_args: Dict[str, Any], clock: str) -> Dict[str, Any]:
    """코퍼스 행 -> 복제본 REST recall 인자(trace 포함). 캠페인은 MCP 로 부르는데 코퍼스는 MCP 기본값(budget high,
    max_tokens 4096)을 지워 두었다 - REST 기본값(budget mid)이 끼면 후보 목록이 캠페인과 달라진다. 시각은 runner.call 처럼
    행에 있으면 그 시각, 없으면 벤치 시계."""
    from bench.corpus import MCP_DEFAULTS
    args = {**MCP_DEFAULTS, **row_args, "trace": True}
    args.setdefault("query_timestamp", clock)
    return args


def _prod_busy(text: Optional[str]) -> Optional[List[Tuple[float, float, Optional[int]]]]:
    return None if text is None else prod_rerank_requests(text)      # 후보 수를 함께 - keep-warm 자기 요청만 정확히 뺀다


# ---------------------------------------------------------------- IO 어댑터 + CLI (단위 테스트 대상 아님 - 시험 실행으로 확인)

_TRACE_RECALL = r'''
import sys,json,urllib.request as u
k=sys.stdin.readline().strip(); body=sys.stdin.read()
d=json.loads(u.urlopen(u.Request("http://localhost:8888/v1/default/banks/personal-demo/memories/recall",data=body.encode(),
  headers={"Authorization":"Bearer "+k,"Content-Type":"application/json"}),timeout=120).read())
t=d.get("trace") or {}
print(json.dumps({"query":t.get("query") and {"query_text":t["query"]["query_text"]},
  "retrieval_results":[{"results":[{k2:r.get(k2) for k2 in ("node_id","context")} for r in m.get("results") or []]}
                       for m in t.get("retrieval_results") or []],
  "reranked":[{k2:r.get(k2) for k2 in ("node_id","text","rrf_rank")} for r in t.get("reranked") or []]}))
'''


def http_post(port: int, query: str, texts: List[str], timeout: float = 180.0) -> Any:
    """벤치 사이드카 /rerank 한 번. (HTTP 상태, 벽시계 ms). 연결 실패는 상태 0."""
    import json
    import time
    import urllib.error
    import urllib.request
    if port == PROD_SIDECAR_PORT:                  # bench_sidecar 를 거치지 않은 호출도 막는다(두 번째 방어선)
        raise SystemExit("cold replay never calls the production sidecar")
    body = json.dumps({"query": query, "texts": texts, "return_text": False}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/rerank", data=body, headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    except (urllib.error.URLError, OSError):
        code = 0
    return code, (time.perf_counter() - t0) * 1000.0


def log_activity(path: str) -> Any:
    """벤치 사이드카 로그의 /rerank 요청 줄 수를 세는 함수(uvicorn access log). 남의 요청 감지용."""
    def count() -> int:
        try:
            with open(path, "rb") as fh:
                return fh.read().count(b'"POST /rerank HTTP')
        except OSError:
            return 0
    return count


def extract(set_name: str, limit: Optional[int] = None) -> Dict[str, Any]:
    """세트 복제본(벤치 사이드카로 리랭크하는 이름 있는 세트)에 holdout recall 을 trace 로 보내 포화 쿼리(후보 300)의
    리랭커 입력을 모은다. 기본 세트는 운영 사이드카를 공유하므로 거부한다."""
    import hashlib
    import json
    import os
    import subprocess

    from bench import replica, runner
    if not set_name:
        raise SystemExit("extract needs a named set (the default set reranks on the production sidecar)")
    n = replica.instance_names(set_name)
    meta = json.load(open(os.path.join(replica.STATE_DIR, n["state"])))
    if not meta.get("sidecar_port") or meta["sidecar_port"] == PROD_SIDECAR_PORT:
        raise SystemExit(f"set {set_name} is not on a bench sidecar")
    clock = meta.get("bench_clock") or replica.set_clock(set_name)
    key = replica.bench_key()
    occurred: Dict[str, Optional[str]] = {}
    while True:                                       # 동결 복제본의 전체 기억 -> occurred_start (3,800건, 4쪽)
        page = replica.hs_get(f"/v1/default/banks/{replica.BANK}/memories/list?limit=1000&offset={len(occurred)}",
                              set_name)
        occurred.update({m["id"]: m.get("occurred_start") for m in page["items"]})
        if not page["items"] or len(occurred) >= page["total"]:
            break
    out, skipped = [], 0
    for row in runner.load_corpus("holdout", limit, seed=13):
        args = recall_args(row["args"], clock)
        raw = subprocess.run(["docker", "exec", "-i", n["hs"], "python", "-c", _TRACE_RECALL],
                             input=key + "\n" + json.dumps(args), capture_output=True, text=True, check=True).stdout
        inp = reranker_inputs(json.loads(raw), occurred)
        if len(inp["texts"]) < MODES[CANDIDATE]:
            skipped += 1                              # B1 은 포화 recall 만 바꾼다
            continue
        out.append({"key": row["key"], **inp})
    return {"set": set_name, "backup": meta.get("backup"), "backup_sha256": meta.get("backup_sha256"),
            "bench_clock": clock, "holdout_sha256": hashlib.sha256(open(runner.HOLDOUT, "rb").read()).hexdigest(),
            "saturated": len(out), "skipped_unsaturated": skipped, "inputs": out}


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    import hashlib
    import json
    import os
    import time

    from bench import guard as G
    from bench import replica, runner
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("extract", help="세트 복제본의 holdout recall trace -> 리랭커 입력(포화 쿼리만)")
    ex.add_argument("--set", required=True)
    ex.add_argument("--limit", type=int)
    ex.add_argument("--out", default=os.path.join(os.path.dirname(runner.HOLDOUT), "cold-inputs.json"))
    rn = sub.add_parser("run", help="벤치 사이드카에 교대 재생")
    rn.add_argument("--port", type=int, required=True, help="`replica.py sidecar-up` 벤치 사이드카 포트")
    rn.add_argument("--inputs", default=os.path.join(os.path.dirname(runner.HOLDOUT), "cold-inputs.json"))
    rn.add_argument("--n-per-mode", type=int, default=N_PER_MODE)
    rn.add_argument("--idle-s", type=float, default=IDLE_S, help="시험 실행만 줄인다(cold 정의는 300s)")
    rn.add_argument("--seed", type=int, default=20)
    rn.add_argument("--run-id", required=True)
    an = sub.add_parser("analyze", help="재생 결과 -> 방식별 분포와 cold 지표")
    an.add_argument("run_dir")
    a = ap.parse_args(argv)

    if a.cmd == "extract":
        data = extract(a.set, a.limit)
        fd = os.open(a.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        print(json.dumps({k: v for k, v in data.items() if k != "inputs"}, indent=1))
        return 0
    if a.cmd == "run":
        sidecar = bench_sidecar(a.port, replica.sidecar_states())
        if not replica._sidecar_process_ok(sidecar):   # 죽은 state 로 7시간 동안 연결 거부만 기록하지 않게
            raise SystemExit(f"bench sidecar {sidecar['name']} (pid {sidecar.get('pid')}) is not running on port {a.port}")
        raw = open(a.inputs, "rb").read()
        src = json.loads(raw)
        probes = plan(a.n_per_mode, len(src["inputs"]), a.seed)
        run_dir = os.path.join(runner.HOLDOUT_RUNS_DIR, a.run_id)
        os.makedirs(run_dir, mode=0o700)
        print(f"cold replay {a.run_id}: {len(probes)} probes, idle {a.idle_s:g}s, "
              f"~{estimate_s(probes, a.idle_s) / 3600:.1f}h", flush=True)
        log = open(os.path.join(run_dir, "probes.jsonl"), "w")

        def on_record(r: Dict[str, Any]) -> None:
            log.write(json.dumps(r) + "\n")
            log.flush()
            print(f"  probe {r['probe']} {r['mode']} cold {sum(r['cold_ms']):.0f}ms status {r['status']}", flush=True)

        guard = G.Guard()
        records, meta = execute(probes, src["inputs"], sidecar, guard=guard, post=http_post,
                                keepwarm=KeepWarm.production(),
                                activity=log_activity(os.path.join(replica.WORK_DIR, f"sidecar-{sidecar['name']}.log")),
                                now=time.time, sleep=time.sleep, prod_mark=G.sidecar_log_offset,
                                prod_errors=G.sidecar_errors_since, idle_s=a.idle_s, on_record=on_record,
                                prod_busy=lambda mark: _prod_busy(G.sidecar_text_since(mark)))
        log.close()
        meta.update(run_id=a.run_id, n_per_mode=a.n_per_mode, seed=a.seed, inputs=a.inputs,
                    inputs_sha256=hashlib.sha256(raw).hexdigest(),
                    source={k: v for k, v in src.items() if k != "inputs"})
        with open(os.path.join(run_dir, "meta.json"), "w") as fh:
            json.dump(meta, fh, indent=1)
        print(json.dumps({k: meta[k] for k in ("probes_done", "aborted", "prod_sidecar")}), flush=True)
        return 1 if meta["aborted"] else 0
    records = [json.loads(line) for line in open(os.path.join(a.run_dir, "probes.jsonl")) if line.strip()]
    meta = json.load(open(os.path.join(a.run_dir, "meta.json")))
    rep = analyze(records, idle_s=meta["idle_s"], guard_windows=(meta.get("guard") or {}).get("windows"),
                  prod_intervals=meta.get("prod_intervals", "not-given"))
    rep.update(run_id=meta.get("run_id"), aborted=meta.get("aborted"), prod_sidecar=meta.get("prod_sidecar"),
               guard_windows=len((meta.get("guard") or {}).get("windows") or []))
    with open(os.path.join(a.run_dir, "report.json"), "w") as fh:
        json.dump(rep, fh, indent=1)
    print(json.dumps(rep, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
