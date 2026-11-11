"""카나리 기준선 추출·판정 도구 (issue #30, #22 준비).

B1(upstream TEI 배치 128 -> 300)의 **카나리 판정**을 운영 로그로 한다. 기준선(128x3)은 배포 전 운영 사이드카 로그에서, 후보(300x1)는
배포 뒤 같은 로그에서 **같은 규칙**으로 뽑는다. 규칙과 기준선 수치를 배포 전에 고정해 판정 뒤에 규칙을 고칠 여지를 없앤다
(CONTEXT.md: 보류를 사후에 풀지 않는다).

세 부분 (cold_replay 와 같은 구조)
- 읽기(순수): 사이드카 로그 본문 -> 요청(시작 = 끝 - elapsed, 줄 시각은 KST 계산 끝), 경고·503 사건, 제외 구간, hindsight 재시도.
- 분석(순수): 요청을 recall 로 묶고(1초 창) cold/warm 을 가르고 방식별 지표를 같은 함수로 센다. 판정은 #22 운영자 승인 롤백 기준.
- IO 경계: 로그 파일(회전본 전부) 읽기와 CLI 뿐. 운영 컨테이너·사이드카에 요청을 보내지 않는다.
"""
from __future__ import annotations

import datetime as dt
import math
import os
import re
from collections import Counter
from typing import Any, Dict, List, Optional, Sequence, Tuple

from bench.guard import SIDECAR_PATTERNS
from bench.judge import quantile

KST = dt.timezone(dt.timedelta(hours=9))    # 운영 사이드카 로그 시각(호스트 현지 시각)

# ---------------------------------------------------------------- 규칙 상수 (배포 전 고정, 테스트로 고정)
GROUP_GAP_S = 1.0             # 앞 요청 끝 뒤 이 초 안에 다음 요청이 시작되면 같은 recall
COLD_IDLE_S = 300.0           # cold recall: 첫 요청 시작이 직전 요청 끝 뒤 5분 이상
DEEP_COLD_IDLE_S = 4 * 3600.0  # deep-cold: 4시간 이상 (cold 의 부분집합)
BASELINE_DAYS = 14            # 기준선 = 배포 직전 14일
SATURATED_CANDIDATES = 300    # 묶음 후보 합(재시도 제외)이 300 이면 포화 recall
DEADLINE_S = 7.5              # 사이드카 절대 데드라인(rerank_queue_timeout_absolute -> 503)
MIN_COLD_RECALLS = 20         # 판정 시점: 배포 뒤 cold recall 20건과
MIN_CANARY_DAYS = 7           # 7일 중 늦은 쪽
COLD_P95_SLOWDOWN = 0.10      # cold recall 단위 p95 가 기준선 대비 +10% 이상이면 롤백
MIN_EXCEED_COUNT = 2          # 7.5s 초과율이 기준선보다 높고 초과 건수 2건 이상이면 롤백
REPEAT_WINDOW_S = 3600.0      # warm 에서 뺄 합성 반복 부하: 첫 요청과 같은 chars 의 다른 요청이 1시간 안에 시작
RETRY_COVERAGE_TOLERANCE_S = 1.0  # hindsight 로그 시각은 초 단위로 읽는다(ms 버림) - 마지막 요청 끝과의 비교에 두는 허용
# 사이드카 로그의 마지막 줄 시각이 구간 끝보다 이 초 넘게 앞이면 꼬리가 빠진 것(낡은 --log-dir 복사본·중간 세대 손실)이다.
# 사이드카는 요청·경고·idle trim 이 있을 때만 줄을 남기므로 조용한 주말(금 저녁~월 아침 약 63시간)은 정상이라 그보다 긴 사흘(72시간)을 허용한다.
# RULES 에 넣지 않는다: judge 가 기준선 JSON 의 rules 와 RULES 가 같은지 보므로 키를 더하면 운영 중인 기준선이 baseline-invalid 가 된다.
LOG_TAIL_TOLERANCE_S = 3 * 86400.0

MODE_SHAPES = {(128, 128, 44): "128x3", (300,): "300x1"}   # 재시도 뺀 요청 후보 수 순서 -> 방식
BASELINE_MODE, CANDIDATE_MODE = "128x3", "300x1"
EXIT = {"PASS": 0, "ROLLBACK": 1, "HOLD": 2, "PENDING": 3, "INVALID": 4}

SIDECAR_LOG_DIR = os.path.expanduser("~/Library/Logs/hindsight-rerank")
DEFAULT_EXCLUDE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "canary-exclude.txt")
OUT_DIR = os.path.expanduser("~/.local/share/hindsight-bench/canary")

RULES = {k: globals()[k] for k in ("GROUP_GAP_S", "COLD_IDLE_S", "DEEP_COLD_IDLE_S", "BASELINE_DAYS",
                                    "SATURATED_CANDIDATES", "DEADLINE_S", "MIN_COLD_RECALLS", "MIN_CANARY_DAYS",
                                    "COLD_P95_SLOWDOWN", "MIN_EXCEED_COUNT", "REPEAT_WINDOW_S",
                                    "RETRY_COVERAGE_TOLERANCE_S")}


# ---------------------------------------------------------------- 읽기 (순수)

# cold_replay._PROCESSED 와 같은 줄을 읽되 candidates·chars 도 뽑고 시각을 호스트 시간대가 아니라 KST 로 해석한다
_PROCESSED = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3}) INFO rerank_processed "
                        r"candidates=(\d+) chars=(\d+) .*?elapsed_ms=([\d.]+)", re.M)
_TS = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3})")
# 사이드카 로그 줄은 logging 형식(`%(asctime)s ...`)이라 시각이 줄 머리에 온다. 진행 막대(tqdm)는 \r 로 줄을 덮으므로 \r 뒤도 줄 머리다
_TS_LINE_HEAD = re.compile(r"(?:^|\r)(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3})", re.M)


def _kst(ts: str, ms: str = "0") -> float:
    return dt.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST).timestamp() + int(ms) / 1000


def parse_requests(text: str) -> List[Dict[str, Any]]:
    """사이드카 로그 본문 -> rerank 요청(시작 순). 줄 시각은 계산 끝이라 시작 = 끝 - elapsed."""
    out = []
    for ts, ms, cand, chars, elapsed in _PROCESSED.findall(text):
        end = _kst(ts, ms)
        out.append({"start": end - float(elapsed) / 1000, "end": end, "candidates": int(cand), "chars": int(chars),
                    "elapsed_s": float(elapsed) / 1000})
    out.sort(key=lambda r: (r["start"], r["end"]))
    return out


def sidecar_events(text: str) -> List[Tuple[float, str]]:
    """guard.SIDECAR_PATTERNS 사건(503·데드라인·큐 포화)에 시각을 붙인다. 경고는 진행 막대 뒤 줄 중간에 찍히기도 하고,
    uvicorn 503 접근 줄에는 시각이 없어 그 앞 마지막 시각을 쓴다(503 은 데드라인 경고 바로 뒤에 찍힌다)."""
    out: List[Tuple[float, str]] = []
    last: Optional[float] = None
    for line in text.splitlines():
        stamps = list(_TS.finditer(line))
        for kind, pat in SIDECAR_PATTERNS.items():
            m = pat.search(line)
            if not m:
                continue
            before = [s for s in stamps if s.start() < m.start()]
            t = _kst(*before[-1].groups()) if before else last
            if t is not None:
                out.append((t, kind))
        if stamps:
            last = _kst(*stamps[-1].groups())
    out.sort(key=lambda e: e[0])
    return out


def log_span(text: str) -> Tuple[Optional[float], Optional[float]]:
    """로그 본문에서 줄 머리(또는 \\r 뒤)에 로그 형식 시각이 찍힌 모든 줄(요청·경고·idle trim 등)의 가장 이른·늦은 시각. 요청 줄만
    보면 로그 앞뒤의 idle 이 없는 것으로 읽힌다 - 파일 실제 시작·끝에 더 가깝다. 메시지 본문·traceback 중간의 시각 문자열은 증거가
    아니다(이른 값이 앞을, 미래 값이 꼬리를 거짓 통과시킨다). 날짜로 해석되지 않는 값은 건너뛴다. 유효한 줄이 없으면 (None, None).
    시각 문자열은 사전순이 곧 시간순이라 양 끝에서부터 유효한 값을 찾는다(전 줄 strptime 을 피한다)."""
    stamps = sorted(set(_TS_LINE_HEAD.findall(text)))

    def first_valid(seq):
        for st in seq:
            try:
                return _kst(*st)
            except ValueError:
                continue
        return None
    return first_valid(stamps), first_valid(reversed(stamps))


def coverage_problems(log_from: Optional[float], log_to: Optional[float], since: float, until: float,
                      who: str) -> List[str]:
    """baseline(who="baseline")과 judge(who="candidate")가 같이 쓰는 로그 커버리지 규칙 - 한 곳에서만 정의해 기준선과 후보의
    기준이 갈라지지 않게 한다. 앞: 로그 첫 줄이 since 보다 늦다(없으면 None). 꼬리: 마지막 줄이 until 보다 LOG_TAIL_TOLERANCE_S 넘게
    앞이다(로그가 아예 없으면 앞 사유 하나만 낸다). 중간 세대 공백은 보지 않는다(bench/README.md 의도된 한계)."""
    out = []
    if log_from is None or log_from > since:
        out.append(f"log-coverage: sidecar log starts at {log_from} after {who} start {since} - "
                   "rotated files are missing or the window is longer than the logs")
    if log_to is not None and log_to + LOG_TAIL_TOLERANCE_S < until:
        out.append(f"log-coverage: sidecar log ends at {log_to} before {who} end {until} - "
                   "the log copy is stale or the files after it are missing")
    return out


_EXCL = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?)\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?)\s*(?:#\s*(.*))?$")


def _kst_minute_or_second(s: str) -> float:
    return _kst(s if s.count(":") == 2 else s + ":00")


def parse_exclude(text: str) -> List[Tuple[float, float, str]]:
    """제외 구간 파일: 한 줄에 `YYYY-MM-DD HH:MM[:SS] YYYY-MM-DD HH:MM[:SS]  # 사유`(KST). 빈 줄과 # 줄은 건너뛴다."""
    out = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _EXCL.match(line)
        if not m:
            raise ValueError(f"exclude line {n}: expected 'YYYY-MM-DD HH:MM[:SS] YYYY-MM-DD HH:MM[:SS]  # reason'")
        s, e = _kst_minute_or_second(m.group(1)), _kst_minute_or_second(m.group(2))
        if e <= s:
            raise ValueError(f"exclude line {n}: end is not after start")
        out.append((s, e, (m.group(3) or "").strip()))
    return out


def _excluded(s: float, e: float, exclude: Sequence[Tuple[float, float, str]]) -> bool:
    return any(s <= we and e >= ws for ws, we, _ in exclude)


_ATTEMPT = re.compile(r"TEI (?:transient error|request failed) \(attempt (\d+)/(\d+)\)")
_EXHAUSTED = re.compile(r"TEI rerank request failed")
_UTC = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})")


def parse_retries(text: Optional[str], since: float, until: float,
                  exclude: Sequence[Tuple[float, float, str]], need_from: Optional[float] = None,
                  need_to: Optional[float] = None) -> Dict[str, Any]:
    """hindsight 컨테이너 로그를 떠 둔 파일(UTC) -> 재시도 시도 번호 n 분포와 재시도 소진 수. 소진은 마지막 시도가
    실패해 upstream 이 올리는 `TEI rerank request failed` 로 센다(마지막 시도는 경고 줄을 남기지 않는다).
    파일이 없으면 unmeasured(0 이 아니다). 파일 첫 시각이 need_from(기본 since) 뒤면(컨테이너 재생성으로 앞이 소실) partial.
    analyze 는 need_from 에 구간 첫 rerank 요청 시작을 넘긴다 - 배포 때 재생성한 컨테이너 로그는 since 뒤에 시작하지만
    그 앞에는 재시도를 낼 요청이 없다. need_to(analyze 는 구간 마지막 rerank 요청 끝)가 있으면 파일 마지막 시각이 그 앞일 때도
    partial - 판정 시각보다 먼저 떠 둔 파일은 그 뒤의 재시도 소진을 담지 못한다."""
    need_from = since if need_from is None else need_from
    if text is None:
        return {"status": "unmeasured"}
    first: Optional[float] = None
    last: Optional[float] = None
    attempts: Counter = Counter()
    exhausted = 0
    for line in text.splitlines():
        m = _UTC.search(line)
        if not m:
            continue
        t = dt.datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=dt.timezone.utc).timestamp()
        first = t if first is None else min(first, t)
        last = t if last is None else max(last, t)
        if not (since <= t < until) or _excluded(t, t, exclude):
            continue
        a = _ATTEMPT.search(line)
        if a:
            attempts[a.group(1)] += 1
        elif _EXHAUSTED.search(line):
            exhausted += 1
    short = (first is None or first > need_from
             or (need_to is not None and last + RETRY_COVERAGE_TOLERANCE_S < need_to))
    return {"status": "partial" if short else "measured",
            "attempts": {str(n): attempts.get(str(n), 0) for n in (1, 2, 3)}, "exhausted": exhausted,
            "covered_from": None if first is None else dt.datetime.fromtimestamp(first, KST).isoformat()}


# ---------------------------------------------------------------- recall 묶기·분류 (순수)

def _segment(reqs: List[Dict[str, Any]], retries: int, dl: int, saturated: bool, idle: Optional[float],
             burst: int) -> Dict[str, Any]:
    kept = [r for r in reqs if not r.get("_retry")]
    return {"start": reqs[0]["start"], "end": max(r["end"] for r in reqs), "requests": reqs, "retries": retries,
            "deadline": dl, "candidates": sum(r["candidates"] for r in kept), "saturated": saturated,
            "mode": MODE_SHAPES.get(tuple(r["candidates"] for r in kept), "other") if saturated else None,
            "first_s": reqs[0]["elapsed_s"], "latency_s": sum(r["elapsed_s"] for r in reqs),
            "idle_s": idle, "deep_cold": idle is not None and idle >= DEEP_COLD_IDLE_S,
            "first_chars": reqs[0]["chars"], "burst": burst}


def group_recalls(reqs: List[Dict[str, Any]], events: Sequence[Tuple[float, str]] = ()) -> List[Dict[str, Any]]:
    """요청 -> recall 조각.
    1 버스트: 버스트 끝(지금까지 가장 늦은 끝) 뒤 GROUP_GAP_S 안에 시작한 요청은 같은 버스트.
    2 재시도: 데드라인 경고가 붙은 버스트에서 바로 앞 요청과 (후보 수, chars) 가 같은 요청은 upstream 재시도다(503 뒤 같은
      배치를 다시 보내고, 사이드카는 앞 계산을 끝낸 뒤 이어서 처리한다). 앞 요청의 recall 에 붙여 후보 합에서는 빼고 지연에는 넣는다.
      데드라인 없이 같은 입력이 이어진 것(반복 부하)은 재시도로 보지 않는다.
    3 나누기: 버스트를 앞에서부터 누적 후보 합(재시도 제외)이 정확히 SATURATED_CANDIDATES 에 닿을 때마다 끊어 포화 recall 로 둔다.
      닿은 recall 의 요청 모양이 방식 모양(MODE_SHAPES: 128+128+44, 300)이 아니면(150+150 두 recall 이 우연히 300) 넘은 것과 같다.
      누적 합이 경계를 넘거나 버스트 끝까지 닿지 못하면 그 recall 첫 요청부터 버스트 나머지는 비포화 조각 하나로 판정에서 뺀다.
    4 idle: 조각 첫 요청 시작 - 앞선 모든 요청의 가장 늦은 끝. 버스트 뒤 recall 은 1초 안이라 warm 이다.
    데드라인 경고는 그 시각에 계산 중인 요청(구간 포함)의 조각에만 붙인다. 못 붙인 경고는 analyze 가 unattached 로 센다
    (다음 버스트에 붙이면 무관한 버스트가 재시도·초과로 바뀐다. 09-29 실로그 45건은 전부 계산 중 요청에 붙었다)."""
    bursts: List[List[Dict[str, Any]]] = []
    for r in reqs:
        if bursts and r["start"] - max(x["end"] for x in bursts[-1]) <= GROUP_GAP_S:
            bursts[-1].append(r)
        else:
            bursts.append([r])
    dl_at: Counter = Counter()      # 요청 인덱스(reqs 순) -> 데드라인 경고 수
    idx = {id(r): i for i, r in enumerate(reqs)}
    for t, kind in events:
        if kind != "deadline":
            continue
        hit = next((i for i, r in enumerate(reqs) if r["start"] <= t <= r["end"]), None)
        if hit is not None:
            dl_at[hit] += 1
    out: List[Dict[str, Any]] = []
    prev_end: Optional[float] = None
    for bi, burst in enumerate(bursts):
        has_dl = any(dl_at[idx[id(r)]] for r in burst)
        segs: List[Tuple[bool, List[Dict[str, Any]]]] = []
        cur: List[Dict[str, Any]] = []
        total, overflow = 0, False
        for k, r in enumerate(burst):
            r = dict(r, _retry=False, _i=idx[id(r)])
            if has_dl and k > 0 and (r["candidates"], r["chars"]) == (burst[k - 1]["candidates"], burst[k - 1]["chars"]):
                r["_retry"] = True
                (cur if cur or not segs else segs[-1][1]).append(r)
                continue
            if overflow:
                cur.append(r)
                continue
            cur.append(r)
            total += r["candidates"]
            if total == SATURATED_CANDIDATES and tuple(x["candidates"] for x in cur if not x["_retry"]) in MODE_SHAPES:
                segs.append((True, cur))
                cur, total = [], 0
            elif total >= SATURATED_CANDIDATES:
                overflow = True
        if cur:
            segs.append((False, cur))
        for sat, rs in segs:
            idle = None if prev_end is None else rs[0]["start"] - prev_end
            dl = sum(dl_at[x["_i"]] for x in rs)
            out.append(_segment(rs, sum(1 for x in rs if x["_retry"]), dl, sat, idle, bi))
            prev_end = max(prev_end if prev_end is not None else -math.inf, max(x["end"] for x in rs))
    return out


def classify(g: Dict[str, Any]) -> Optional[str]:
    """cold / warm. 로그 첫 요청처럼 직전 요청을 모르면 None(어느 쪽에도 넣지 않는다)."""
    if g["idle_s"] is None:
        return None
    return "cold" if g["idle_s"] >= COLD_IDLE_S else "warm"


def chars_index(reqs: Sequence[Dict[str, Any]]) -> Dict[int, List[Tuple[float, int]]]:
    """chars -> [(시작, 요청 인덱스)] (repeated_load 용)."""
    out: Dict[int, List[Tuple[float, int]]] = {}
    for i, r in enumerate(reqs):
        out.setdefault(r["chars"], []).append((r["start"], i))
    return out


def repeated_load(g: Dict[str, Any], index: Dict[int, List[Tuple[float, int]]]) -> bool:
    """합성 반복 부하: recall 첫 요청과 같은 chars 의 다른 조각 요청이 첫 요청 시작 앞뒤 REPEAT_WINDOW_S 안에 시작했다."""
    own = {r["_i"] for r in g["requests"]}
    t = g["requests"][0]["start"]
    return any(i not in own and abs(st - t) <= REPEAT_WINDOW_S for st, i in index.get(g["first_chars"], ()))


# ---------------------------------------------------------------- 지표 (순수)

def _q(xs: Sequence[float], q: float) -> Optional[float]:
    return None if not xs else quantile(xs, q)


def mode_metrics(cold: Sequence[Dict[str, Any]], warm: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """한 방식의 지표. 128x3 과 300x1 에 같은 함수를 쓴다."""
    cf = [g["first_s"] for g in cold]
    cl = [g["latency_s"] for g in cold]
    wl = [g["latency_s"] for g in warm]
    exceed = sum(1 for g in cold if g["deadline"] > 0)
    return {"cold_recalls": len(cold), "deep_cold_recalls": sum(1 for g in cold if g["deep_cold"]),
            "cold_first_p50": _q(cf, 0.5), "cold_first_p95": _q(cf, 0.95),
            "cold_recall_p50": _q(cl, 0.5), "cold_recall_p95": _q(cl, 0.95),
            "cold_exceed": exceed, "cold_exceed_rate": exceed / len(cold) if cold else None,
            "cold_retried_recalls": sum(1 for g in cold if g["retries"]),
            "warm_recalls": len(warm), "warm_recall_p50": _q(wl, 0.5), "warm_recall_p95": _q(wl, 0.95),
            "warm_recall_p99": _q(wl, 0.99),
            "warm_exceed": sum(1 for g in warm if g["deadline"] > 0)}   # 참고 정보 - 초과율 판정의 분모는 cold recall(#22)


def analyze(text: str, since: float, until: float, exclude: Sequence[Tuple[float, float, str]],
            retry_text: Optional[str] = None) -> Dict[str, Any]:
    """[since, until) 에 첫 요청이 시작한 포화 recall 의 방식별 지표. idle 은 구간 앞 요청까지 보고 잰다(제외 구간의 요청도
    사이드카를 데운 활동이다). 제외 구간과 겹친 recall 은 빼고, warm 에서는 반복 부하도 뺀다."""
    reqs = parse_requests(text)
    events = sidecar_events(text)
    groups = group_recalls(reqs, events)
    index = chars_index(reqs)
    segs = [g for g in groups if since <= g["start"] < until]
    log_from, log_to = log_span(text)
    # hindsight 로그가 덮어야 할 앞은 구간 첫 rerank 요청 시작이다(배포 때 컨테이너를 재생성하면 그 앞에는 재시도를 낼 요청이 없다).
    # 단 사이드카 로그 자체가 since 뒤에 시작하면 "그 앞에 요청이 없다"는 근거가 없으니 since 부터 덮어야 한다(issue #104 C1)
    front_covered = log_from is not None and log_from <= since
    inwin = [g for g in segs if g["saturated"]]
    kept = [g for g in inwin if not _excluded(g["start"], g["end"], exclude)]
    unsat = [g for g in segs if not g["saturated"] and not _excluded(g["start"], g["end"], exclude)]
    # 비포화 조각 중 누적 합이 300 에 닿거나 넘은 것(포화 recall 이 섞였을 수 있으나 나눌 수 없어 뺀 것). 300 미만은 원래 비포화 recall
    overflow = [g for g in unsat if g["candidates"] >= SATURATED_CANDIDATES]
    by_mode: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    repeated = 0
    for g in kept:
        kind = classify(g)
        if kind is None:
            continue
        if kind == "warm" and repeated_load(g, index):
            repeated += 1
            continue
        by_mode.setdefault(g["mode"], {"cold": [], "warm": []})[kind].append(g)
    errors = Counter(k for t, k in events if since <= t < until and not _excluded(t, t, exclude))
    unattached = sum(1 for t, k in events if k == "deadline" and since <= t < until and not _excluded(t, t, exclude)
                     and not any(r["start"] <= t <= r["end"] for r in reqs))
    return {"since": dt.datetime.fromtimestamp(since, KST).isoformat(),
            "until": dt.datetime.fromtimestamp(until, KST).isoformat(),
            "rules": RULES, "requests_in_window": sum(1 for r in reqs if since <= r["start"] < until),
            "saturated_recalls": len(inwin), "excluded_recalls": len(inwin) - len(kept),
            "repeated_warm_dropped": repeated,
            "unsaturated_segments": len(unsat), "cold_unsaturated_segments": sum(1 for g in unsat if classify(g) == "cold"),
            "overflow_dropped": len(overflow),
            "cold_overflow_dropped": sum(1 for g in overflow if classify(g) == "cold"),
            "mode_counts": dict(Counter(g["mode"] for g in kept)),
            "mode_first_start": {m: min(g["start"] for g in kept if g["mode"] == m) for m in {g["mode"] for g in kept}},
            "unattached_deadlines": unattached,
            "log_from_ts": log_from, "log_to_ts": log_to,
            "modes": {m: mode_metrics(v["cold"], v["warm"]) for m, v in sorted(by_mode.items())},
            "sidecar_errors": {k: errors.get(k, 0) for k in SIDECAR_PATTERNS},
            "retries": parse_retries(retry_text, since, until, exclude,
                                     need_from=(min((r["start"] for r in reqs if since <= r["start"] < until), default=until)
                                                if front_covered else since),
                                     need_to=max((r["end"] for r in reqs if since <= r["start"] < until), default=None)),
            "exclude": [[dt.datetime.fromtimestamp(s, KST).isoformat(), dt.datetime.fromtimestamp(e, KST).isoformat(), why]
                        for s, e, why in exclude]}


def baseline(text: str, since: float, until: float, exclude: Sequence[Tuple[float, float, str]],
             retry_text: Optional[str] = None, damaged: Sequence[str] = ()) -> Dict[str, Any]:
    """배포 전 기준선. 구간에 128x3 이 아닌 포화 recall 이 있으면 방식이 섞인 것이라 거부한다. 사이드카 로그(회전본 포함)의
    가장 이른 줄이 구간 시작보다 늦거나 가장 늦은 줄이 구간 끝보다 사흘 넘게 앞이면(낡은 복사본) 기준선이 조용히 짧아지므로 거부한다.
    커버리지 규칙은 judge 와 같은 coverage_problems 다. damaged(read_sidecar_logs 의 손상 세대 사유)도 같은 이유로 거부한다."""
    a = analyze(text, since, until, exclude, retry_text)
    other = {m: n for m, n in a["mode_counts"].items() if m != BASELINE_MODE}
    if other:
        raise ValueError(f"mode-mismatch: baseline window must be {BASELINE_MODE} only, found {other}")
    problems = coverage_problems(a["log_from_ts"], a["log_to_ts"], since, until, "baseline") + list(damaged)
    if problems:
        raise ValueError("; ".join(problems))
    a["mode"] = BASELINE_MODE
    a["days"] = (until - since) / 86400      # RULES 의 BASELINE_DAYS 가 아니라 실제 창 길이(judge 가 대조한다)
    return a


# ---------------------------------------------------------------- 판정 (순수)

def _cmp(c: Optional[float], b: Optional[float]) -> Optional[str]:
    """후보 c 와 문턱 b: worse / boundary(같다) / ok. 값이 없으면 None(측정 불가)."""
    if c is None or b is None:
        return None
    if math.isclose(c, b, rel_tol=1e-9, abs_tol=1e-12):
        return "boundary"
    return "worse" if c > b else "ok"


def decide(base: Dict[str, Any], cand: Dict[str, Any], elapsed_s: float, retries: Dict[str, Any]) -> Dict[str, Any]:
    """#22 운영자 승인 롤백 기준. base·cand 는 mode_metrics 결과.
    1 즉시 롤백: 재시도 소진 1건 이상(판정 시점 전에도 본다)
    2 판정 시점: cold recall MIN_COLD_RECALLS 건과 MIN_CANARY_DAYS 일 중 늦은 쪽. 그 전은 PENDING
    3 롤백: 7.5s 초과율 > 기준선이고 초과 건수 >= 2 / cold recall p95 >= 기준선 x 1.1 / warm p95·p99 > 기준선
    경계(문턱과 같은 값)와 측정 불가는 HOLD(기간 연장). 어느 것에도 걸리지 않으면 PASS."""
    reasons: List[str] = []
    if retries.get("exhausted", 0) >= 1:
        return {"status": "ROLLBACK", "reasons": [f"retry-exhausted:{retries['exhausted']}"]}
    n = cand.get("cold_recalls") or 0
    if n < MIN_COLD_RECALLS or elapsed_s < MIN_CANARY_DAYS * 86400:
        return {"status": "PENDING",
                "reasons": [f"not-yet:cold={n}/{MIN_COLD_RECALLS},days={elapsed_s / 86400:.2f}/{MIN_CANARY_DAYS}"]
                + ([f"retries-{retries.get('status')}"] if retries.get("status") != "measured" else [])}
    verdicts: List[Tuple[str, Optional[str]]] = []
    rate = _cmp(cand.get("cold_exceed_rate"), base.get("cold_exceed_rate"))
    count = _cmp(float(cand.get("cold_exceed") or 0), float(MIN_EXCEED_COUNT))
    if rate is None:
        ex = None
    elif "ok" in (rate, count):
        ex = "ok"
    elif rate == count == "worse":
        ex = "worse"
    else:
        ex = "boundary"
    verdicts.append((f"exceed:rate={cand.get('cold_exceed_rate')}vs{base.get('cold_exceed_rate')},"
                     f"count={cand.get('cold_exceed')}", ex))
    b95 = base.get("cold_recall_p95")
    verdicts.append((f"cold-p95:{cand.get('cold_recall_p95')}vs{b95}x{1 + COLD_P95_SLOWDOWN}",
                     _cmp(cand.get("cold_recall_p95"), None if b95 is None else b95 * (1 + COLD_P95_SLOWDOWN))))
    for k in ("warm_recall_p95", "warm_recall_p99"):
        verdicts.append((f"warm-{k[-3:]}:{cand.get(k)}vs{base.get(k)}", _cmp(cand.get(k), base.get(k))))
    worse = [r for r, v in verdicts if v == "worse"]
    if worse:
        return {"status": "ROLLBACK", "reasons": worse}
    reasons = [f"{r}:boundary" for r, v in verdicts if v == "boundary"]
    reasons += [f"{r}:unmeasured" for r, v in verdicts if v is None]
    if retries.get("status") != "measured":
        reasons.append(f"retries-{retries.get('status')}")
    return {"status": "HOLD" if reasons else "PASS", "reasons": reasons}


def judge(base: Dict[str, Any], text: str, since: float, until: float, exclude: Sequence[Tuple[float, float, str]],
          retry_text: Optional[str], damaged: Sequence[str] = ()) -> Dict[str, Any]:
    """배포 뒤 [since, until) 를 기준선과 같은 규칙으로 재고 판정한다. 후보 구간에 128x3 포화 recall 이 있으면
    (배포 전 구간을 넣었거나 배포가 안 됐다) 방식 불일치로 거부한다(INVALID) - 섞인 분포로 판정하지 않는다.
    사이드카 로그의 가장 이른 줄이 since 보다 늦거나(로그 회전·잘못된 --log-dir) 가장 늦은 줄이 until 보다 사흘 넘게 앞이면(낡은
    복사본·꼬리 손실) 일부가 집계에서 빠진 채 until - since 로 기간을 채우게 되므로 baseline() 과 같은 coverage_problems 로
    거부한다(INVALID). 커버리지 부족은 PASS·HOLD·PENDING 만 막고 ROLLBACK 신호는 가리지 않는다: 커버리지만 부족하면 decide() 를 until - since 로 불러 재시도 소진·지표 악화 ROLLBACK 을
    그대로 내고(사유에 INVALID 병기), 그 밖의 무효 사유와 겹치면 아래 소진 규칙을 따른다. damaged(read_sidecar_logs 가 읽는 세대 중
    끝이 잘렸거나 깨진 .gz 의 사유, issue #142)는 그 세대 뒷부분이 집계에서 빠진 것이라 커버리지 부족과 같게 다룬다."""
    a = analyze(text, since, until, exclude, retry_text)
    out = {"since": a["since"], "until": a["until"], "candidate_modes": a["mode_counts"],
           "candidate": a["modes"].get(CANDIDATE_MODE, mode_metrics([], [])),
           "baseline": base["modes"].get(BASELINE_MODE), "sidecar_errors": a["sidecar_errors"], "retries": a["retries"],
           "excluded_recalls": a["excluded_recalls"], "repeated_warm_dropped": a["repeated_warm_dropped"], "rules": RULES}
    wrong = {m: n for m, n in a["mode_counts"].items() if m != CANDIDATE_MODE}
    invalid: List[str] = []
    if wrong:
        invalid.append(f"mode-mismatch:candidate window has {wrong} "
                       f"({CANDIDATE_MODE}={a['mode_counts'].get(CANDIDATE_MODE, 0)}) - "
                       "pre-deploy window or deploy not applied")
    elif (out["baseline"] is None or base.get("mode") != BASELINE_MODE or base.get("rules") != RULES
            or not math.isclose(base.get("days") or 0.0, BASELINE_DAYS)):
        invalid.append("baseline-invalid:not a 128x3 baseline made with the same rules "
                       f"over {BASELINE_DAYS} days (days={base.get('days')})")
    coverage = coverage_problems(a["log_from_ts"], a["log_to_ts"], since, until, "candidate") + list(damaged)
    uncovered = bool(coverage)
    invalid += coverage
    if invalid and uncovered and len(invalid) == len(coverage):
        # 커버리지만 부족(방식 일치, 기준선 유효. 앞·꼬리 모두): 판정 문턱은 수정 전과 같게 until - since 로 decide() 를 불러 ROLLBACK 만 살린다
        d = decide(out["baseline"], out["candidate"], until - since, a["retries"])
        if d["status"] == "ROLLBACK":
            out.update(status="ROLLBACK", reasons=d["reasons"] + [f"INVALID:{r}" for r in invalid])
        else:
            out.update(status="INVALID", reasons=invalid)
        return out
    if invalid:
        # 사용자에게 recall 오류가 나간 사실(재시도 소진)은 판정 형식 문제보다 우선한다. 300x1 이 돈 구간(창 안 첫 300x1
        # 포화 recall 시작부터)의 소진만 본다 - 섞인 창의 128x3 쪽 소진은 후보 탓이 아니다
        first = a["mode_first_start"].get(CANDIDATE_MODE)
        ex = 0 if first is None else parse_retries(retry_text, first, until, exclude).get("exhausted", 0)
        label = "(300x1 part)"
        if uncovered and not wrong:
            # 기준선 무효 + 커버리지 부족: 방식 불일치가 없어 창 전체가 후보 구간이다. since..until 전체의 소진을 본다(로그 없는 앞부분 포함)
            ex, label = a["retries"].get("exhausted", 0), ""
        if ex >= 1:
            out.update(status="ROLLBACK", reasons=[f"retry-exhausted:{ex}{label}"] + [f"INVALID:{r}" for r in invalid])
        else:
            out.update(status="INVALID", reasons=invalid)
        return out
    out.update(decide(out["baseline"], out["candidate"], until - since, a["retries"]))
    return out


# ---------------------------------------------------------------- IO 경계 + CLI

def sidecar_log_files(log_dir: str) -> List[str]:
    """압축된 회전본 server.log.N.gz 를 세대 수 제한 없이 전부 찾아 오래된 순(N 큰 것부터)으로, 마지막에 server.log. 로그 회전 세대
    수는 설정(RERANK_LOG_GENERATIONS)이라 고정 목록을 두지 않는다. 압축 전 server.log.N 은 읽지 않는다: rotate-logs.sh 는
    cp -> truncate -> gzip 이라 그 파일은 cp 뒤 truncate 전에 server.log 와 같은 줄을 담아 중복 집계되고, gzip 이 끝나면 지워져
    listdir 뒤 open 이 실패한다. 덜 읽는 쪽이 안전하다(앞·꼬리 커버리지가 부족하면 INVALID)."""
    gens: Dict[int, str] = {}
    for name in (os.listdir(log_dir) if os.path.isdir(log_dir) else ()):     # 디렉터리가 없으면 로그 없음(judge 가 INVALID)
        m = re.fullmatch(r"server\.log\.(\d+)\.gz", name)
        if m:
            gens[int(m.group(1))] = name
    names = [gens[n] for n in sorted(gens, reverse=True)]
    if os.path.exists(os.path.join(log_dir, "server.log")):
        names.append("server.log")
    return [os.path.join(log_dir, n) for n in names]


def _orphan_cut(spans: Sequence[Tuple[Optional[float], Optional[float]]], since: Optional[float]) -> int:
    """오래된 순 파일별 log_span 에서 고아 고세대를 가르는 경계 인덱스(이 앞은 읽지 않는다). RERANK_LOG_GENERATIONS 를 줄이면
    rotate-logs.sh 가 더는 덮지 않는 .N.gz 가 낡은 줄로 남는데, 이 줄이 log_from_ts 를 since 앞으로 당겨 실제로는 since 부터 없는 로그의
    앞 커버리지를 거짓 통과시켰다. 고아는 체인보다 항상 오래돼 시각 순서(단조성)로는 안 걸리므로 파일 경계의 시각 공백을 본다.
    끊는 조건은 셋이 **모두** 맞을 때다: 앞 파일 마지막 줄 ~ 다음(새 쪽) 파일 첫 줄이 LOG_TAIL_TOLERANCE_S 를 넘고, 앞 파일이 통째로
    since 앞이며, 다음 파일이 since 뒤에 시작한다(= since 가 그 공백 안에 있다). 이때만 앞 파일의 낡은 줄이 거짓 커버리지 증거가 되기 때문이다.
    그 밖에는 자르지 않는다: since 를 덮는 파일(마지막 줄 >= since)이나 체인이 이미 since 에 닿은 경우는 앞 세대의 요청이 집계·idle 계산의
    입력이라 버리면 지표 악화(ROLLBACK 신호)와 cold 분류가 달라진다. 정상 세대와 고아는 시각만으로 가를 수 없어(사이드카는 요청이 있을 때만
    줄을 쓰니 사흘 넘는 정지도 정상이다) since 가 사흘 넘는 공백 안에 있으면 보수적으로 앞 커버리지 부족(INVALID)이다 - 재시도 소진·지표
    악화 ROLLBACK 은 가리지 않는다. 세대 번호 상한은 쓰지 않는다(세대 수는 설정이라 올리면 큰 N 도 정상이다). 줄이 없는 파일은 건너뛴다.
    since 가 None 이면 자르지 않는다."""
    if since is None:
        return 0
    newer_first: Optional[float] = None
    for i in range(len(spans) - 1, -1, -1):
        lo, hi = spans[i]
        if lo is None or hi is None:
            continue
        if newer_first is not None and newer_first - hi > LOG_TAIL_TOLERANCE_S and hi < since < newer_first:
            return i + 1
        newer_first = lo
    return 0


def _read_gz_prefix(path: str) -> Tuple[str, Optional[str]]:
    """압축 회전본을 읽히는 데까지 읽는다. (본문, 손상 사유). 정상이면 사유는 None 이고 본문은 gzip.open(path, "rt", errors="replace")
    와 같다. 끝이 잘렸거나(EOFError) 깨졌으면(BadGzipFile·zlib.error) 그 앞까지 풀린 바이트만 쓰고, 반쯤 풀린 마지막 줄은 버린다(잘린
    elapsed_ms 같은 값이 집계·시각 파싱에 들어가지 않게). 0 바이트 .gz 도 손상이다(빈 입력을 gzip 해도 헤더·꼬리가 있다)."""
    import gzip
    import io
    import zlib
    chunks: List[bytes] = []
    problem: Optional[str] = None
    try:
        if os.path.getsize(path) == 0:
            problem = "empty file"
        else:
            with gzip.open(path, "rb") as fh:
                while True:                           # read1 로 조각씩 받아 예외 직전까지 풀린 바이트를 잃지 않는다
                    b = fh.read1(1 << 16)
                    if not b:
                        break
                    chunks.append(b)
    except (EOFError, gzip.BadGzipFile, zlib.error) as e:
        problem = type(e).__name__
    data = b"".join(chunks)
    if problem is not None:
        data = data[:data.rfind(b"\n") + 1]
    return io.TextIOWrapper(io.BytesIO(data), errors="replace").read(), problem


def read_sidecar_logs(log_dir: str = SIDECAR_LOG_DIR,
                      since: Optional[float] = None) -> Tuple[str, List[str], List[str]]:
    """회전본(server.log.N.gz, 있는 것 전부)과 server.log 를 오래된 순으로 잇는다. 회전은 copytruncate 라
    크기로 회전을 알 수 없다 - 매번 전부 읽는다. since(집계 창 시작)를 주면 고아 고세대(_orphan_cut)는 읽지 않아 files(판정 JSON 의
    sources)에도 없다. baseline 과 judge 는 각자의 창 시작을 넘기고 같은 coverage_problems 를 쓴다.
    끝이 잘렸거나 깨진 .gz(issue #142)는 읽히는 앞부분까지만 쓰고 세 번째 값(damaged)에 파일 이름을 담은 사유를 남긴다 - judge·baseline 은
    이를 커버리지 부족과 같게 다룬다(PASS·HOLD·PENDING 만 막고 ROLLBACK 은 가리지 않는다). 고아 컷 바로 앞(경계) 세대가 손상이면 그 절단이
    공백을 만들어 정상 세대가 잘렸을 수 있으므로 그 사유도 남긴다(컷이 나면 앞 커버리지가 이미 부족이라 판정은 같다). 더 오래된 고아의 손상은
    사유에 넣지 않는다(읽지 않는 파일이라 판정 입력이 아니다). 줄이 하나도 안 읽히는 손상 세대(헤더 안 절단·0 바이트)는 span 이 없어
    _orphan_cut 이 건너뛴다: 더 새로운 경계에서 컷이 나면 함께 잘리고 컷 경계 세대가 아니라 사유가 없으며, 컷이 안 나면 읽는 쪽에 남아 사유가 붙는다."""
    parts, files, problems = [], [], []
    for p in sidecar_log_files(log_dir):
        if p.endswith(".gz"):
            body, problem = _read_gz_prefix(p)
        else:
            with open(p, "rt", errors="replace") as fh:
                body, problem = fh.read(), None
        parts.append(body)
        files.append(p)
        problems.append(problem)
    cut = _orphan_cut([log_span(part) for part in parts], since)
    damaged = [f"log-coverage: sidecar log {os.path.basename(f)} is truncated or corrupt ({why}) - "
               "only its readable first part was used"
               for f, why in zip(files[cut:], problems[cut:]) if why is not None]
    if cut and problems[cut - 1] is not None:
        damaged.insert(0, f"log-coverage: sidecar log {os.path.basename(files[cut - 1])} is truncated or corrupt "
                          f"({problems[cut - 1]}) - not read: dropped as an orphan at a generation gap its lost end may have made")
    return "\n".join(parts[cut:]), files[cut:], damaged


def _when(s: str) -> float:
    return _kst_minute_or_second(s.strip().replace("T", " "))


def _dump(obj: Any, path: str) -> None:
    import json
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)


def _summary(m: Optional[Dict[str, Any]]) -> str:
    if not m:
        return "(none)"
    f = lambda v: "-" if v is None else (f"{v:.3f}" if isinstance(v, float) else str(v))
    keys = ("cold_recalls", "deep_cold_recalls", "cold_first_p50", "cold_first_p95", "cold_recall_p50",
            "cold_recall_p95", "cold_exceed", "cold_exceed_rate", "warm_recalls", "warm_recall_p50",
            "warm_recall_p95", "warm_recall_p99", "warm_exceed")
    return " ".join(f"{k}={f(m.get(k))}" for k in keys)


def baseline_shape_problem(base: Any) -> Optional[str]:
    """--baseline 파일이 judge() 가 읽는 모양(객체, modes 객체, 128x3 지표는 없거나 객체, days 와 decide() 가 쓰는 지표 값은
    없거나 숫자)이 아니면 사유. 입력 검증만 한다 - 값이 맞는지(방식·규칙·기간)는 judge() 가 baseline-invalid 로 따진다."""
    if not isinstance(base, dict):
        return f"top level is {type(base).__name__}, expected object"
    modes = base.get("modes")
    if not isinstance(modes, dict):
        return f"modes is {type(modes).__name__}, expected object"
    metrics = modes.get(BASELINE_MODE)
    if metrics is not None and not isinstance(metrics, dict):
        return f"modes.{BASELINE_MODE} is {type(metrics).__name__}, expected object"
    days = base.get("days")
    if days is not None and (isinstance(days, bool) or not isinstance(days, (int, float))):
        return f"days is {type(days).__name__}, expected number"
    for k in ("cold_exceed_rate", "cold_recall_p95", "warm_recall_p95", "warm_recall_p99"):    # decide() 가 비교·곱하는 값
        v = (metrics or {}).get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))):
            return f"modes.{BASELINE_MODE}.{k} is {type(v).__name__}, expected number"
    return None


def main(argv: Optional[List[str]] = None) -> int:
    """입력 오류(argparse 사용법 오류 = exit 2, 파일·형식 예외 traceback = exit 1)가 판정 종료 코드 HOLD(2)·ROLLBACK(1)로
    읽히지 않게 INVALID(4)로 바꾼다."""
    try:
        return _main(argv)
    except SystemExit as e:
        if e.code in (0, None):
            raise
        return EXIT["INVALID"]
    except (OSError, ValueError, KeyError) as e:
        print(f"INVALID {type(e).__name__}: {e}")
        return EXIT["INVALID"]


def _main(argv: Optional[List[str]] = None) -> int:
    import argparse
    import json
    import time
    ap = argparse.ArgumentParser(prog="python3 -m bench.canary")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("baseline", "judge"):
        p = sub.add_parser(name)
        p.add_argument("--exclude", default=DEFAULT_EXCLUDE, help="제외 구간 파일(KST)")
        p.add_argument("--log-dir", default=SIDECAR_LOG_DIR, help="운영 사이드카 로그 디렉터리")
        p.add_argument("--hindsight-log", default=None, help="hindsight 컨테이너 로그를 떠 둔 파일(UTC). 없으면 unmeasured")
        p.add_argument("--out", required=True)
    b = sub.choices["baseline"]
    b.add_argument("--until", required=True, help="배포 시각(KST, 'YYYY-MM-DD HH:MM[:SS]')")
    b.add_argument("--days", type=int, default=BASELINE_DAYS)
    j = sub.choices["judge"]
    j.add_argument("--baseline", required=True)
    j.add_argument("--since", required=True, help="배포 시각(KST)")
    j.add_argument("--until", default=None, help="기본 지금")
    a = ap.parse_args(argv)
    exclude = parse_exclude(open(a.exclude).read())
    win_since = _when(a.until) - a.days * 86400 if a.cmd == "baseline" else _when(a.since)   # 고아 세대 판정에 쓰는 집계 창 시작
    text, files, damaged = read_sidecar_logs(a.log_dir, win_since)
    retry_text = None
    if a.hindsight_log:
        if not os.path.exists(a.hindsight_log):          # 경로 오타가 즉시 롤백 검사를 조용히 끄지 않게 한다
            print(f"INVALID hindsight-log-missing: {a.hindsight_log}")
            return EXIT["INVALID"]
        retry_text = open(a.hindsight_log, errors="replace").read()
    if a.cmd == "baseline":
        until = _when(a.until)
        try:
            res = baseline(text, until - a.days * 86400, until, exclude, retry_text, damaged)
        except ValueError as e:
            print(f"INVALID {e}")
            return EXIT["INVALID"]
        res["sources"] = files
        _dump(res, a.out)
        m = res["modes"].get(BASELINE_MODE)
        print(f"baseline {res['since']} .. {res['until']} saturated={res['saturated_recalls']} "
              f"excluded={res['excluded_recalls']} repeated_warm_dropped={res['repeated_warm_dropped']} "
              f"overflow_dropped={res['overflow_dropped']} (cold {res['cold_overflow_dropped']})")
        print(f"128x3 {_summary(m)}")
        print(f"sidecar_errors {res['sidecar_errors']} retries {res['retries']}")
        print(f"-> {a.out}")
        return 0
    base = json.load(open(a.baseline))
    problem = baseline_shape_problem(base)
    if problem:
        # 모양이 틀린 기준선이 TypeError 로 터져 exit 1(ROLLBACK)로 읽히지 않게 한다(issue #106). 그냥 INVALID 로 돌아가면
        # "기준선 무효보다 재시도 소진 ROLLBACK 이 먼저" 인 judge() 규칙을 건너뛰므로, 기준선 없음으로 바꿔 judge() 에 맡긴다
        # (baseline-invalid 사유 + 소진이 있으면 ROLLBACK, 없으면 INVALID). 형식 사유는 reasons 끝에 붙인다.
        base = {"modes": {}}
    since = _when(a.since)
    until = time.time() if a.until is None else _when(a.until)
    res = judge(base, text, since, until, exclude, retry_text, damaged)
    if problem:
        res["reasons"].append(f"baseline-shape: {problem}")
    res["sources"] = files
    _dump(res, a.out)
    print(f"{res['status']} {res['since']} .. {res['until']} modes={res['candidate_modes']}")
    for r in res["reasons"]:
        print(f"  {r}")
    print(f"300x1 {_summary(res['candidate'])}")
    print(f"-> {a.out}")
    return EXIT[res["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
