"""issue #30 카나리 기준선 추출·판정 도구 - 합성 사이드카·hindsight 로그 줄로 규칙과 판정을 고정한다(운영 로그 원문 없음)."""
import datetime as dt
import gzip
import json

import pytest

from bench import canary as K
from bench import cold_replay

T0 = dt.datetime(2026, 9, 10, 12, 0, 0, tzinfo=K.KST).timestamp()


def kst(t: float) -> str:
    ms = round(t * 1000)
    return dt.datetime.fromtimestamp(ms // 1000, K.KST).strftime("%Y-%m-%d %H:%M:%S") + f",{ms % 1000:03d}"


def proc(end: float, cand: int, chars: int, elapsed_s: float) -> str:
    return (f"{kst(end)} INFO rerank_processed candidates={cand} chars={chars} batches=4 "
            f"elapsed_ms={elapsed_s * 1000:.1f} device=mps")


def deadline(t: float) -> str:
    return (f"Batches:  25%|##   | 1/4 [00:05<00:15,  5.31s/it]{kst(t)} WARNING rerank_queue_timeout_absolute "
            f"waited_ms=7503.0 queue_depth=0\n"
            f'INFO:     127.0.0.1:62721 - "POST /rerank HTTP/1.1" 503 Service Unavailable')


def recall_128x3(start: float, chars: int = 1000, el=(2.0, 1.5, 0.5), gap: float = 0.1):
    """128+128+44 순차 요청. (로그 줄, 마지막 끝 시각)"""
    out, t = [], start
    for c, e, k in zip((128, 128, 44), el, (chars, chars + 1, chars + 2)):
        out.append(proc(t + e, c, k, e))
        t += e + gap
    return out, t - gap


def idle_trim(t: float) -> str:
    """요청이 없는 구간에도 찍히는 줄(idle trim). 로그 앞·뒤가 어디까지 닿는지 요청 줄과 따로 보이게 한다."""
    return f"{kst(t)} INFO rerank_idle_trim device=mps idle_seconds=1800.0"


def text(*chunks) -> str:
    lines = []
    for c in chunks:
        lines.extend(c if isinstance(c, list) else [c])
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 규칙 상수 고정

def test_rule_constants_are_pinned():
    assert K.GROUP_GAP_S == 1.0
    assert K.COLD_IDLE_S == 300.0 and K.DEEP_COLD_IDLE_S == 4 * 3600.0
    assert K.BASELINE_DAYS == 14
    assert K.SATURATED_CANDIDATES == 300
    assert K.DEADLINE_S == 7.5
    assert K.MIN_COLD_RECALLS == 20 and K.MIN_CANARY_DAYS == 7
    assert K.COLD_P95_SLOWDOWN == 0.10 and K.MIN_EXCEED_COUNT == 2
    assert K.REPEAT_WINDOW_S == 3600.0
    assert K.RETRY_COVERAGE_TOLERANCE_S == 1.0
    assert K.LOG_TAIL_TOLERANCE_S == 3 * 86400.0
    # 꼬리 허용은 RULES 밖이다: judge 는 기준선 JSON 의 rules 와 RULES 가 같은지 보므로 키가 늘면 운영 기준선이 baseline-invalid 가 된다
    assert set(K.RULES) == {"GROUP_GAP_S", "COLD_IDLE_S", "DEEP_COLD_IDLE_S", "BASELINE_DAYS", "SATURATED_CANDIDATES",
                            "DEADLINE_S", "MIN_COLD_RECALLS", "MIN_CANARY_DAYS", "COLD_P95_SLOWDOWN", "MIN_EXCEED_COUNT",
                            "REPEAT_WINDOW_S", "RETRY_COVERAGE_TOLERANCE_S"}
    assert K.EXIT == {"PASS": 0, "ROLLBACK": 1, "HOLD": 2, "PENDING": 3, "INVALID": 4}


# ---------------------------------------------------------------- 로그 읽기

def test_parse_requests_takes_start_as_end_minus_elapsed_and_keeps_candidates():
    t = text(proc(T0 + 2.0, 128, 5000, 2.0), "Batches: 100%|###| 4/4 [00:02<00:00]", proc(T0 + 5.0, 44, 800, 0.5))
    r = K.parse_requests(t)
    assert [(x["candidates"], x["chars"]) for x in r] == [(128, 5000), (44, 800)]
    assert r[0]["start"] == pytest.approx(T0) and r[0]["end"] == pytest.approx(T0 + 2.0)
    assert r[1]["start"] == pytest.approx(T0 + 4.5)
    # 기존 파서와 같은 줄을 같은 길이로 읽는다(시각 해석만 호스트 시간대 대신 KST 로 고정)
    iv = cold_replay.prod_rerank_intervals(t)
    assert len(iv) == len(r)
    assert [e - s for s, e in iv] == pytest.approx([x["end"] - x["start"] for x in r])


def test_read_sidecar_logs_concatenates_all_rotations_oldest_first(tmp_path):
    (tmp_path / "server.log").write_text(proc(T0 + 30, 1, 30, 0.1) + "\n")
    with gzip.open(tmp_path / "server.log.1.gz", "wt") as f:
        f.write(proc(T0 + 20, 1, 20, 0.1) + "\n")
    with gzip.open(tmp_path / "server.log.3.gz", "wt") as f:         # .2 없음 - 있는 것만 읽는다
        f.write(proc(T0 + 10, 1, 10, 0.1) + "\n")
    t, files, _ = K.read_sidecar_logs(str(tmp_path))
    assert [x["chars"] for x in K.parse_requests(t)] == [10, 20, 30]
    assert [f.rsplit("/", 1)[1] for f in files] == ["server.log.3.gz", "server.log.1.gz", "server.log"]


def test_sidecar_events_time_warnings_mid_line_and_503_by_last_timestamp():
    t = text(proc(T0, 1, 1, 0.1), deadline(T0 + 10), "WARNING-free noise", f"{kst(T0 + 20)} WARNING rerank_queue_full depth=4")
    ev = K.sidecar_events(t)
    assert [(k, round(x - T0, 3)) for x, k in ev] == [("deadline", 10.0), ("http_503", 10.0), ("queue_full", 20.0)]


# ---------------------------------------------------------------- recall 묶기

def test_group_gap_boundary_one_second_inside_and_outside():
    a = [proc(T0 + 1, 128, 1, 1.0), proc(T0 + 3, 172, 2, 1.0)]         # 끝 T0+1, 다음 시작 T0+2 -> 간격 1.0 = 안
    assert [len(g["requests"]) for g in K.group_recalls(K.parse_requests(text(a)))] == [2]
    b = [proc(T0 + 1, 128, 1, 1.0), proc(T0 + 3.002, 172, 2, 1.0)]     # 간격 1.002 = 밖
    assert [len(g["requests"]) for g in K.group_recalls(K.parse_requests(text(b)))] == [1, 1]


def test_both_128x3_and_300x1_are_saturated_recalls_with_their_mode():
    r1, e1 = recall_128x3(T0)
    t = text(r1, proc(e1 + 100, 300, 9000, 3.0), proc(e1 + 200, 128, 1, 1.0), proc(e1 + 201.5, 128, 2, 1.0))
    gs = K.group_recalls(K.parse_requests(t))
    assert [(g["saturated"], g["mode"]) for g in gs] == [(True, "128x3"), (True, "300x1"), (False, None)]
    assert gs[0]["latency_s"] == pytest.approx(4.0) and gs[0]["first_s"] == pytest.approx(2.0)
    assert gs[1]["latency_s"] == pytest.approx(3.0)


def test_retry_after_deadline_is_part_of_the_recall_not_a_new_saturated_sum():
    # 7.5s 에 503 -> upstream 이 같은 128 을 다시 보낸다: 128,128(재시도),128,44. 재시도는 합에서 빼고 지연에는 넣는다
    lines = [proc(T0 + 9.0, 128, 111, 9.0), deadline(T0 + 7.5), proc(T0 + 10.5, 128, 111, 1.5),
             proc(T0 + 12.0, 128, 222, 1.4), proc(T0 + 12.6, 44, 333, 0.5)]
    g, = K.group_recalls(K.parse_requests(text(lines)), K.sidecar_events(text(lines)))
    assert g["saturated"] and g["mode"] == "128x3" and g["retries"] == 1 and g["deadline"] == 1
    assert g["latency_s"] == pytest.approx(9.0 + 1.5 + 1.4 + 0.5)
    # 데드라인 없이 같은 recall 이 1초 안에 두 번 붙은 것(반복 부하)은 재시도가 아니라 recall 두 개다
    r1, e1 = recall_128x3(T0 + 100, chars=5)
    r2, _ = recall_128x3(e1 + 0.5, chars=5)
    gs = K.group_recalls(K.parse_requests(text(r1, r2)), [])
    assert [(g["saturated"], g["retries"], len(g["requests"])) for g in gs] == [(True, 0, 3), (True, 0, 3)]


def test_300x1_retry_after_the_recall_closed_is_attached_to_it():
    lines = [proc(T0 + 9.0, 300, 111, 9.0), deadline(T0 + 7.5), proc(T0 + 12.0, 300, 111, 3.0)]
    g, = K.group_recalls(K.parse_requests(text(lines)), K.sidecar_events(text(lines)))
    assert g["saturated"] and g["mode"] == "300x1" and g["retries"] == 1 and g["latency_s"] == pytest.approx(12.0)


# ---------------------------------------------------------------- 버스트 나누기 (연속 recall)

def _burst(start, shape, chars0=100, el=0.5, gap=0.05):
    """1초 안에 이어진 요청들. shape = 후보 수 목록. chars 는 요청마다 다르다."""
    out, t = [], start
    for k, c in enumerate(shape):
        out.append(proc(t + el, c, chars0 + k, el))
        t += el + gap
    return out, t - gap


def test_burst_of_consecutive_recalls_splits_at_every_cumulative_300():
    r0 = proc(T0, 1, 1, 0.1)
    b, _ = _burst(T0 + 400, [128, 128, 44, 128, 128, 44, 300])
    gs = K.group_recalls(K.parse_requests(text(r0, b)))[1:]
    assert [(g["saturated"], g["mode"], K.classify(g)) for g in gs] == [
        (True, "128x3", "cold"), (True, "128x3", "warm"), (True, "300x1", "warm")]
    assert len({g["burst"] for g in gs}) == 1
    assert gs[1]["idle_s"] == pytest.approx(0.05, abs=1e-3)


@pytest.mark.parametrize("shape,sat,overflow", [
    ([128, 128, 72, 128, 128, 44], [], 1),                   # 누적 328: 경계를 넘으면 그 recall 부터 버스트 끝까지 비포화
    ([128, 128, 44, 128, 100], [True], 0),                   # 뒤 recall 이 300 에 못 닿음 -> 나머지 비포화(원래 비포화 recall)
    ([1, 128, 128, 44, 1], [], 1),                           # 앞에 작은 요청이 끼면 301 -> 전체 비포화
    ([128, 22, 128, 22], [], 1),                             # 150 recall 둘이 우연히 300 - 방식 모양이 아니라 비포화
])
def test_burst_overflow_or_shortfall_drops_the_rest_as_unsaturated(shape, sat, overflow):
    r0 = proc(T0, 1, 1, 0.1)
    b, end = _burst(T0 + 400, shape)
    gs = K.group_recalls(K.parse_requests(text(r0, b)))[1:]
    assert [g["saturated"] for g in gs] == sat + [False]
    assert sum(len(g["requests"]) for g in gs) == len(shape)
    m = K.analyze(text(r0, b), since=T0 + 1, until=end + 1, exclude=[])
    assert m["unsaturated_segments"] == 1
    assert m["overflow_dropped"] == overflow                 # 300 을 넘어 나눌 수 없어 뺀 조각
    assert m["cold_overflow_dropped"] == (overflow if not sat else 0)   # cold 후보(버스트 선두)가 빠졌는지 따로 센다


def test_retry_in_the_middle_of_a_burst_stays_with_its_own_recall():
    r0 = proc(T0, 1, 1, 0.1)
    s = T0 + 400
    lines = [r0, proc(s + 1.0, 128, 10, 1.0), proc(s + 2.0, 128, 11, 0.95), proc(s + 2.5, 44, 12, 0.45),
             proc(s + 12.0, 128, 20, 9.4), deadline(s + 10.1), proc(s + 13.5, 128, 20, 1.5),
             proc(s + 14.5, 128, 21, 0.95), proc(s + 15.0, 44, 22, 0.45)]
    gs = K.group_recalls(K.parse_requests(text(lines)), K.sidecar_events(text(lines)))[1:]
    assert [(g["saturated"], g["retries"], g["deadline"], K.classify(g)) for g in gs] == [
        (True, 0, 0, "cold"), (True, 1, 1, "warm")]
    assert gs[1]["latency_s"] == pytest.approx(9.4 + 1.5 + 0.95 + 0.45)


# ---------------------------------------------------------------- cold/warm 분류 + 제외

def _one_after_idle(idle: float):
    r0 = proc(T0, 1, 1, 0.1)
    r, _ = recall_128x3(T0 + idle)
    return K.group_recalls(K.parse_requests(text(r0, r)))[-1]


@pytest.mark.parametrize("idle,kind,deep", [(299.9, "warm", False), (300.0, "cold", False),
                                            (4 * 3600 - 1, "cold", False), (4 * 3600, "cold", True)])
def test_cold_and_deep_cold_boundaries(idle, kind, deep):
    g = _one_after_idle(idle)
    assert g["idle_s"] == pytest.approx(idle)
    assert K.classify(g) == kind and g["deep_cold"] is deep


def test_first_request_ever_has_unknown_idle_and_is_not_classified():
    r, _ = recall_128x3(T0)
    g, = K.group_recalls(K.parse_requests(text(r)))
    assert g["idle_s"] is None and K.classify(g) is None


def test_parse_exclude_file_accepts_optional_seconds_and_comments():
    ex = K.parse_exclude("# 벤치 구간\n2026-09-26 02:04 2026-09-26 03:42  # cold\n\n"
                         "2026-09-26 21:56:30 2026-09-27 01:47:00\n")
    assert len(ex) == 2 and ex[0][2] == "cold"
    assert ex[1][0] == dt.datetime(2026, 9, 26, 21, 56, 30, tzinfo=K.KST).timestamp()
    with pytest.raises(ValueError):
        K.parse_exclude("2026-09-26 02:04\n")
    with pytest.raises(ValueError):
        K.parse_exclude("2026-09-26 03:00 2026-09-26 02:00\n")          # 끝이 시작보다 앞


def test_default_exclude_fixture_holds_the_known_bench_windows():
    ex = K.parse_exclude(open(K.DEFAULT_EXCLUDE).read())
    got = [(kst(s)[:16], kst(e)[:16]) for s, e, _ in ex[:3]]
    assert got == [("2026-09-26 02:04", "2026-09-26 03:42"), ("2026-09-26 21:56", "2026-09-27 01:47"),
                   ("2026-09-27 03:18", "2026-09-27 04:00")]


def test_recall_overlapping_an_exclude_window_is_dropped_and_adjacent_one_kept():
    r0 = proc(T0, 1, 1, 0.1)
    a, ea = recall_128x3(T0 + 400)                      # cold, 창 [ea-1, ea+50] 과 겹친다
    b, eb = recall_128x3(ea + 400, chars=2000)          # cold, 창 뒤
    t = text(r0, a, b)
    ex = [(ea - 1, ea + 50, "bench")]
    m = K.analyze(t, since=T0 - 1, until=eb + 1, exclude=ex)
    assert m["modes"]["128x3"]["cold_recalls"] == 1
    assert m["excluded_recalls"] == 1


def _warm_filler(start, end, step=200.0):
    """사이드카를 데워 두는 작은 요청(1 후보, 고유 chars) - 그 사이 recall 이 warm 이 되게 한다."""
    return [proc(t + 0.1, 1, 50000 + int(t - start), 0.1) for t in [start + k * step for k in range(int((end - start) // step) + 1)]]


def test_repeated_load_is_dropped_from_warm_only_within_the_window():
    r0 = proc(T0, 1, 1, 0.1)
    a, ea = recall_128x3(T0 + 400, chars=7000)             # cold - 반복이어도 cold 에는 남는다
    b, eb = recall_128x3(ea + 30, chars=7000)              # warm, 같은 첫 chars 가 a 시작 뒤 약 34s -> 반복 부하
    b0 = ea + 30
    c0 = b0 + K.REPEAT_WINDOW_S                            # 창 경계와 같다 -> 안(반복)
    c, ec = recall_128x3(c0, chars=7000)
    d0 = c0 + K.REPEAT_WINDOW_S + 1                        # 가장 가까운 같은 입력(c)과 창 + 1s -> 밖(남는다)
    d, ed = recall_128x3(d0, chars=7000)
    filler = _warm_filler(eb + 50, d0 - 50)
    t = text(r0, a, b, sorted(filler + c + d))
    m = K.analyze(t, since=T0 - 1, until=ed + 1, exclude=[])
    mm = m["modes"]["128x3"]
    assert mm["cold_recalls"] == 1
    assert m["repeated_warm_dropped"] == 2                  # b, c
    assert mm["warm_recalls"] == 1                          # d


# ---------------------------------------------------------------- 지표

def _cold_series(n, first_el, sat_mode="128x3", start=T0, gap_s=600.0, chars0=10000, dl_at=()):
    """n 개의 cold recall. dl_at 의 인덱스는 첫 요청에서 데드라인 초과(재시도 포함)."""
    lines, t = [proc(start, 1, 1, 0.1)], start + gap_s
    for i in range(n):
        ch = chars0 + 10 * i
        if sat_mode == "300x1":
            if i in dl_at:
                lines += [proc(t + 9.0, 300, ch, 9.0), deadline(t + 7.5), proc(t + 12.0, 300, ch, 3.0)]
                end = t + 12.0
            else:
                lines.append(proc(t + first_el, 300, ch, first_el))
                end = t + first_el
        else:
            if i in dl_at:
                lines += [proc(t + 9.0, 128, ch, 9.0), deadline(t + 7.5), proc(t + 10.5, 128, ch, 1.5),
                          proc(t + 12.0, 128, ch + 1, 1.4), proc(t + 12.6, 44, ch + 2, 0.5)]
                end = t + 12.6
            else:
                r, end = recall_128x3(t, chars=ch, el=(first_el, 1.0, 0.5))
                lines += r
        t = end + gap_s
    return lines, t


def test_metrics_report_counts_percentiles_and_exceed_rate():
    lines, end = _cold_series(10, 2.0, dl_at=(3,))
    m = K.analyze(text(lines), since=T0 - 1, until=end, exclude=[])
    mm = m["modes"]["128x3"]
    assert mm["cold_recalls"] == 10 and mm["deep_cold_recalls"] == 0
    assert mm["cold_exceed"] == 1 and mm["cold_exceed_rate"] == pytest.approx(0.1)
    assert mm["cold_first_p50"] == pytest.approx(2.0)
    assert mm["cold_recall_p50"] == pytest.approx(3.5)
    assert mm["cold_recall_p95"] > 3.5                   # 재시도한 recall(12.4s)이 꼬리에 있다
    assert m["sidecar_errors"] == {"http_503": 1, "deadline": 1, "queue_full": 0}
    assert m["retries"] == {"status": "unmeasured"}


def test_retry_file_parsing_counts_attempts_exhaustion_and_filters_utc_window():
    since = dt.datetime(2026, 9, 26, 0, 0, tzinfo=K.KST).timestamp()
    log = "\n".join([
        "hindsight-1  | 2026-09-25 14:00:00,000 - WARNING - x - TEI transient error (attempt 1/4): boom",   # 23:00 KST 전날 - 밖
        "hindsight-1  | 2026-09-25 17:30:47,741 - WARNING - x - TEI transient error (attempt 1/4): boom",
        "hindsight-1  | 2026-09-25 17:30:55,944 - WARNING - x - TEI transient error (attempt 2/4): boom",
        "hindsight-1  | 2026-09-25 17:31:10,000 - WARNING - x - TEI request failed (attempt 3/4): boom",
        "hindsight-1  | 2026-09-25 17:31:30,000 - ERROR - x - recall failed: TEI rerank request failed: boom",
    ])
    r = K.parse_retries(log, since=since, until=since + 86400, exclude=[])
    assert r["status"] == "measured"                      # 파일 첫 줄(09-25 23:00 KST)이 since 앞 = 구간 전체를 덮는다
    assert r["attempts"] == {"1": 1, "2": 1, "3": 1} and r["exhausted"] == 1
    wide = K.parse_retries(log, since=since - 86400, until=since + 86400, exclude=[])
    assert wide["status"] == "partial"                    # 컨테이너 재생성으로 앞부분이 없다 -> partial
    assert wide["attempts"]["1"] == 2
    # 제외 구간(KST)에 든 재시도는 뺀다: 17:30:55Z = 02:30:55 KST
    ex = [(since + 2.5 * 3600, since + 2.6 * 3600, "b")]
    assert K.parse_retries(log, since=since, until=since + 86400, exclude=ex)["attempts"]["2"] == 0


def test_retry_status_measured_when_file_covers_window_and_unmeasured_without_file():
    log = "hindsight-1  | 2026-09-25 10:00:00,000 - INFO - x - started\n"
    since = dt.datetime(2026, 9, 26, 0, 0, tzinfo=K.KST).timestamp()
    r = K.parse_retries(log, since=since, until=since + 3600, exclude=[])
    assert r["status"] == "measured" and r["exhausted"] == 0
    assert K.parse_retries(None, since=since, until=since + 1, exclude=[]) == {"status": "unmeasured"}


# ---------------------------------------------------------------- 판정

BASE = {"cold_recalls": 100, "cold_exceed": 2, "cold_exceed_rate": 0.02, "cold_recall_p95": 10.0,
        "warm_recall_p95": 5.0, "warm_recall_p99": 6.0}


def cand(**kw):
    c = {"cold_recalls": 25, "cold_exceed": 0, "cold_exceed_rate": 0.0, "cold_recall_p95": 9.0,
         "warm_recall_p95": 4.8, "warm_recall_p99": 5.9}
    c.update(kw)
    return c


MEASURED = {"status": "measured", "attempts": {"1": 0, "2": 0, "3": 0}, "exhausted": 0}
WEEK = 7 * 86400.0


def verdict(c, days=WEEK, retries=MEASURED):
    return K.decide(BASE, c, elapsed_s=days, retries=retries)


def test_pass_when_nothing_is_worse_after_20_cold_and_7_days():
    v = verdict(cand())
    assert v["status"] == "PASS" and K.EXIT[v["status"]] == 0


@pytest.mark.parametrize("c,days", [(cand(cold_recalls=19), WEEK), (cand(), WEEK - 1)])
def test_pending_before_the_later_of_20_cold_recalls_and_7_days(c, days):
    assert verdict(c, days)["status"] == "PENDING"


def test_immediate_rollback_on_retry_exhaustion_even_while_pending():
    v = verdict(cand(cold_recalls=3), days=3600, retries={**MEASURED, "exhausted": 1})
    assert v["status"] == "ROLLBACK" and "retry-exhausted" in v["reasons"][0]


@pytest.mark.parametrize("kw,reason", [
    ({"cold_exceed": 3, "cold_exceed_rate": 0.12}, "exceed"),
    ({"cold_recall_p95": 11.01}, "cold-p95"),
    ({"warm_recall_p95": 5.01}, "warm-p95"),
    ({"warm_recall_p99": 6.01}, "warm-p99"),
])
def test_rollback_conditions(kw, reason):
    v = verdict(cand(**kw))
    assert v["status"] == "ROLLBACK" and any(r.startswith(reason) for r in v["reasons"])


@pytest.mark.parametrize("kw,reason", [
    ({"cold_exceed": 2, "cold_exceed_rate": 0.08}, "exceed"),        # 건수가 문턱(2)과 같다
    ({"cold_exceed": 3, "cold_exceed_rate": 0.02}, "exceed"),        # 초과율이 기준선과 같다
    ({"cold_recall_p95": 11.0}, "cold-p95"),                         # 정확히 +10%
    ({"warm_recall_p95": 5.0}, "warm-p95"),
    ({"warm_recall_p99": 6.0}, "warm-p99"),
])
def test_boundary_values_hold(kw, reason):
    v = verdict(cand(**kw))
    assert v["status"] == "HOLD" and any(r.startswith(reason) for r in v["reasons"])


def test_exceed_below_count_threshold_is_not_a_rollback_even_if_rate_is_higher():
    assert verdict(cand(cold_exceed=1, cold_exceed_rate=0.04))["status"] == "PASS"


@pytest.mark.parametrize("retries", [{"status": "unmeasured"}, {**MEASURED, "status": "partial"}])
def test_unmeasured_or_partial_retries_hold_at_decision_time(retries):
    v = verdict(cand(), retries=retries)
    assert v["status"] == "HOLD" and any(r.startswith("retries-") for r in v["reasons"])


def test_missing_metric_holds():
    assert verdict(cand(warm_recall_p99=None))["status"] == "HOLD"


# ---------------------------------------------------------------- 방식 확인 (judge)

def test_judge_rejects_a_candidate_window_without_300x1_recalls():
    lines, end = _cold_series(25, 2.0)                    # 배포 전 모양(128x3)만 있는 구간
    base = K.baseline(text(lines), since=T0, until=end, exclude=[])
    v = K.judge(base, text(lines), since=T0 - 1, until=end, exclude=[], retry_text=None)
    assert v["status"] == "INVALID" and K.EXIT["INVALID"] == 4
    assert v["reasons"][0].startswith("mode-mismatch")
    assert v["candidate_modes"] == {"128x3": 25}


def test_judge_reads_300x1_window_with_same_rules():
    bl, bend = _cold_series(25, 2.0)
    base = K.baseline(text(bl), since=T0, until=bend, exclude=[])
    base["days"] = K.BASELINE_DAYS                        # 합성 기준선은 짧은 창이다 - judge 의 14일 대조만 통과시킨다
    cl, cend = _cold_series(25, 3.0, sat_mode="300x1", start=bend + 10, dl_at=(0, 1))
    since = bend + 5
    log = "hindsight-1  | 2020-01-01 00:00:00,000 - INFO - x - started\n"
    until = max(cend, since + WEEK)
    v = K.judge(base, text(bl, cl, idle_trim(until - 60)), since=since, until=until, exclude=[], retry_text=log)
    assert v["candidate_modes"] == {"300x1": 25}
    assert v["candidate"]["cold_recalls"] == 25 and v["candidate"]["cold_exceed"] == 2
    # 재시도한 recall 2건(12.0s)이 꼬리를 끌어올린다: 25건 중 상위 2건 -> 보간 p95 = 3.0 + 9.0 x 0.8
    assert v["candidate"]["cold_recall_p95"] == pytest.approx(10.2)
    # 기준선 cold p95 3.5 x 1.1 을 넘는다 -> ROLLBACK (초과 건수 2 는 문턱과 같아 그 조건만으로는 보류였다)
    assert v["status"] == "ROLLBACK" and [r.split(":")[0] for r in v["reasons"]] == ["cold-p95"]


def test_baseline_refuses_a_window_containing_300x1_recalls():
    cl, cend = _cold_series(3, 3.0, sat_mode="300x1")
    with pytest.raises(ValueError, match="mode-mismatch"):
        K.baseline(text(cl), since=T0, until=cend, exclude=[], retry_text=None)


def test_cli_baseline_writes_json(tmp_path):
    lines, end = _cold_series(5, 2.0)
    early = proc(end - 15 * 86400, 1, 1, 0.1)             # 로그가 기준선 시작(until - 14일)보다 앞까지 닿는다
    (tmp_path / "server.log").write_text(text(early, lines))
    ex = tmp_path / "ex.txt"
    ex.write_text("# none\n")
    out = tmp_path / "b.json"
    rc = K.main(["baseline", "--until", kst(end)[:19], "--days", "14", "--exclude", str(ex),
                 "--log-dir", str(tmp_path), "--out", str(out)])
    assert rc == 0
    d = json.loads(out.read_text())
    assert d["modes"]["128x3"]["cold_recalls"] == 5 and d["rules"]["GROUP_GAP_S"] == 1.0
    assert d["retries"] == {"status": "unmeasured"}


# ---------------------------------------------------------------- final-review 20260929-000946 반영

def utc(t: float) -> str:
    return dt.datetime.fromtimestamp(int(t), dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S") + ",000"


def hs(t: float, msg: str = "INFO - x - tick") -> str:
    return f"hindsight-1  | {utc(t)} - {msg}"


EXHAUSTED = "ERROR - x - recall failed: TEI rerank request failed: boom"


def test_fr1_retry_file_starting_after_since_but_before_first_request_is_measured():
    # 배포 때 hindsight 를 재생성하면 로그는 since 뒤에 시작한다 - 그 앞에 재시도를 낼 rerank 요청이 없으면 전체를 덮은 것이다
    lines, end = _cold_series(3, 2.0)                     # 첫 요청 시작 T0 - 0.1
    sidecar = text(idle_trim(T0 - 2000), lines)           # 사이드카 로그는 since(T0 - 1000) 앞부터 있다 - 그 사이 요청이 없다는 근거
    log = "\n".join([hs(T0 - 500), hs(end + 5)])
    m = K.analyze(sidecar, since=T0 - 1000, until=end + 10, exclude=[], retry_text=log)
    assert m["retries"]["status"] == "measured"
    log_late = "\n".join([hs(T0 + 300), hs(end + 5)])    # 첫 요청 뒤에 시작 -> 앞이 빠졌다
    assert K.analyze(sidecar, since=T0 - 1000, until=end + 10, exclude=[], retry_text=log_late)["retries"]["status"] == "partial"


@pytest.mark.parametrize("last_offset,status", [
    (0.0, "measured"),       # 마지막 줄이 마지막 요청 끝과 같은 초(초 단위 내림) -> 1초 허용 안
    (-1.0, "partial"),       # 1초 앞 초 -> 내림 차이(0.5s)를 넘어 1.5s 모자람
])
def test_fr2_retry_file_must_reach_the_last_request_with_one_second_tolerance(last_offset, status):
    s0 = int(T0) + 5000
    need_to = s0 + 0.5
    log = "\n".join([hs(T0 - 10), hs(s0 + last_offset)])
    r = K.parse_retries(log, since=T0, until=need_to + 100, exclude=[], need_from=T0, need_to=need_to)
    assert r["status"] == status


def test_fr2_one_second_tolerance_boundary_is_inclusive():
    s0 = int(T0) + 5000
    log = "\n".join([hs(T0 - 10), hs(s0)])
    assert K.parse_retries(log, T0, s0 + 100, [], need_from=T0, need_to=s0 + 1.0)["status"] == "measured"
    assert K.parse_retries(log, T0, s0 + 100, [], need_from=T0, need_to=s0 + 1.001)["status"] == "partial"


def test_fr2_stale_snapshot_through_analyze_is_partial():
    lines, end = _cold_series(3, 2.0)
    log = "\n".join([hs(T0 - 500), hs(T0 + 700)])        # 두 번째 recall 전에 떠 둔 스냅샷
    assert K.analyze(text(lines), since=T0 - 1000, until=end + 10, exclude=[], retry_text=log)["retries"]["status"] == "partial"


def _cli_logs(tmp_path, lines, end):
    (tmp_path / "server.log").write_text(text(proc(end - 15 * 86400, 1, 1, 0.1), lines))
    ex = tmp_path / "ex.txt"
    ex.write_text("# none\n")
    return ex


def test_fr3_missing_hindsight_log_path_is_invalid_not_unmeasured(tmp_path):
    lines, end = _cold_series(3, 2.0)
    ex = _cli_logs(tmp_path, lines, end)
    rc = K.main(["baseline", "--until", kst(end)[:19], "--exclude", str(ex), "--log-dir", str(tmp_path),
                 "--hindsight-log", str(tmp_path / "nope.log"), "--out", str(tmp_path / "b.json")])
    assert rc == K.EXIT["INVALID"] and not (tmp_path / "b.json").exists()


def test_fr4_cold_shortfall_segment_is_counted_as_dropped_cold():
    r0 = proc(T0, 1, 1, 0.1)
    b, end = _burst(T0 + 400, [128, 128])                  # cold 인데 300 에 못 닿음
    m = K.analyze(text(r0, b), since=T0 + 1, until=end + 1, exclude=[])
    assert m["cold_overflow_dropped"] == 0 and m["cold_unsaturated_segments"] == 1


def test_fr5_baseline_records_days_and_judge_rejects_a_non_14_day_baseline():
    bl, bend = _cold_series(25, 2.0)
    base = K.baseline(text(bl), since=T0, until=bend, exclude=[])
    assert base["days"] == pytest.approx((bend - T0) / 86400)
    cl, cend = _cold_series(25, 3.0, sat_mode="300x1", start=bend + 10)
    v = K.judge(base, text(bl, cl), since=bend + 5, until=cend + WEEK, exclude=[], retry_text=None)
    assert v["status"] == "INVALID" and v["reasons"][0].startswith("baseline-invalid")


@pytest.mark.parametrize("argv", [
    ["judge", "--out", "x.json"],                                        # 필수 인자 누락(argparse exit 2 = HOLD 와 겹침)
    ["judge", "--baseline", "{missing}", "--since", "2026-09-29 00:00", "--out", "{out}"],   # 없는 기준선 파일
    ["judge", "--baseline", "{bad}", "--since", "2026-09-29 00:00", "--out", "{out}"],       # 깨진 JSON
    ["judge", "--baseline", "{bad}", "--since", "yesterday", "--out", "{out}"],             # 시각 형식 오류
])
def test_fr6_input_errors_exit_invalid_not_rollback_or_hold(tmp_path, argv):
    lines, end = _cold_series(1, 2.0)
    ex = _cli_logs(tmp_path, lines, end)
    (tmp_path / "bad.json").write_text("{not json")
    sub = {"{missing}": str(tmp_path / "none.json"), "{bad}": str(tmp_path / "bad.json"), "{out}": str(tmp_path / "o.json")}
    args = [sub.get(x, x) for x in argv] + ["--exclude", str(ex), "--log-dir", str(tmp_path)]
    assert K.main(args) == K.EXIT["INVALID"]


_GOOD = object()


def _judge_cli(tmp_path, base=_GOOD, cold_el=2.0, exhausted=False):
    """합성 후보 창(_candidate_week)으로 `judge` CLI 인자를 만든다. 기준선 파일 내용만 바꿔 입력 검증을 따로 본다."""
    good, t, since, until = _candidate_week(0, cold_el=cold_el)
    (tmp_path / "server.log").write_text(text(proc(since - 3600, 1, 1, 0.1), t))      # 로그가 since 앞부터 있다 = 커버리지 충족
    (tmp_path / "ex.txt").write_text("# none\n")
    (tmp_path / "hs.log").write_text(_retry_log(since, until, *([since + 5 * 86400] if exhausted else [])))
    (tmp_path / "base.json").write_text(json.dumps(good if base is _GOOD else base))
    return ["judge", "--baseline", str(tmp_path / "base.json"), "--since", kst(since)[:19], "--until", kst(until)[:19],
            "--exclude", str(tmp_path / "ex.txt"), "--log-dir", str(tmp_path), "--hindsight-log", str(tmp_path / "hs.log"),
            "--out", str(tmp_path / "o.json")]


# issue #106: 모양이 틀린 기준선 파일은 TypeError·AttributeError 로 판정기를 죽여 exit 1(ROLLBACK)로 읽히면 안 된다
_HEADER = {"mode": "128x3", "rules": K.RULES, "days": K.BASELINE_DAYS}
_BAD_BASELINES = {
    "empty-list": [],
    "modes-empty-list": {"modes": []},
    "string": "baseline",
    "number": 7,
    "null": None,
    "modes-string": {"modes": "128x3"},
    "modes-null": {"modes": None},
    # 검증이 없으면 아래는 mode·rules·days 가 정상이라 baseline-invalid 단락을 지나 decide()·isclose 안에서 터진다
    "mode-metrics-list": {**_HEADER, "modes": {"128x3": []}},
    "mode-metrics-string": {**_HEADER, "modes": {"128x3": "x"}},
    "days-string": {**_HEADER, "days": "14", "modes": {"128x3": {}}},
    "days-list": {**_HEADER, "days": [14], "modes": {"128x3": {}}},
    "days-bool": {**_HEADER, "days": True, "modes": {"128x3": {}}},
    "metric-string": {**_HEADER, "modes": {"128x3": {"cold_exceed_rate": "0.01"}}},       # decide() 가 isclose 에 넘기는 값
    "metric-list": {**_HEADER, "modes": {"128x3": {"cold_recall_p95": [3.5]}}},          # decide() 가 곱하는 값
}


@pytest.mark.parametrize("shape", list(_BAD_BASELINES))
def test_issue106_malformed_baseline_exits_invalid_not_rollback(tmp_path, shape):
    assert K.main(_judge_cli(tmp_path, _BAD_BASELINES[shape])) == K.EXIT["INVALID"] == 4
    res = json.loads((tmp_path / "o.json").read_text())                  # 결과 파일을 남겨 알림이 사유를 읽을 수 있다
    assert res["status"] == "INVALID"
    assert res["reasons"][0].startswith("baseline-invalid:") and res["reasons"][-1].startswith("baseline-shape:")


@pytest.mark.parametrize("shape", list(_BAD_BASELINES))
def test_issue106_malformed_baseline_does_not_hide_retry_exhaustion_rollback(tmp_path, shape):
    """기준선 무효보다 재시도 소진 ROLLBACK 이 먼저다(README 계약): 모양이 틀린 기준선도 같은 규칙을 탄다."""
    assert K.main(_judge_cli(tmp_path, _BAD_BASELINES[shape], exhausted=True)) == K.EXIT["ROLLBACK"] == 1
    res = json.loads((tmp_path / "o.json").read_text())
    assert res["status"] == "ROLLBACK" and res["reasons"][0].startswith("retry-exhausted:1")
    assert res["reasons"][-1].startswith("baseline-shape:")


@pytest.mark.parametrize("cold_el,status", [(2.0, "PASS"), (5.25, "ROLLBACK")])
def test_issue106_valid_baseline_still_passes_and_rolls_back_through_main(tmp_path, cold_el, status):
    """입력 검증은 정상 기준선을 막지 않고 실제 ROLLBACK 경로를 약하게 만들지 않는다: 같은 CLI 로 PASS(0)와 ROLLBACK(1)."""
    assert K.main(_judge_cli(tmp_path, cold_el=cold_el)) == K.EXIT[status]


def test_issue106_a_bug_inside_judge_is_not_turned_into_invalid(tmp_path, monkeypatch):
    """INVALID 변환은 입력 검증으로 한정한다: 올바른 모양의 기준선에서 judge() 가 TypeError 를 내면 그대로 전파된다."""
    def boom(*a, **k):
        raise TypeError("real bug")
    monkeypatch.setattr(K, "judge", boom)
    with pytest.raises(TypeError, match="real bug"):
        K.main(_judge_cli(tmp_path))


def test_fr6_malformed_exclude_line_exits_invalid(tmp_path):
    lines, end = _cold_series(1, 2.0)
    ex = _cli_logs(tmp_path, lines, end)
    ex.write_text("2026-09-26 02:04\n")
    assert K.main(["baseline", "--until", kst(end)[:19], "--exclude", str(ex), "--log-dir", str(tmp_path),
                   "--out", str(tmp_path / "b.json")]) == K.EXIT["INVALID"]


def _mixed_window():
    """배포 전 128x3 cold 3건 뒤 300x1 cold 25건. (사이드카 줄, 기준선, 300x1 첫 시작, 끝)"""
    bl, bend = _cold_series(25, 2.0)
    base = K.baseline(text(bl), since=T0, until=bend, exclude=[])
    base["days"] = K.BASELINE_DAYS
    pre, pend = _cold_series(3, 2.0, start=bend + 10, chars0=30000)
    post, cend = _cold_series(25, 3.0, sat_mode="300x1", start=pend + 10, chars0=40000)
    first300 = pend + 10 + 600
    return text(bl, pre, post), base, bend + 5, first300, cend


def test_fr7_retry_exhaustion_in_the_300x1_part_rolls_back_before_mode_mismatch():
    t, base, since, first300, cend = _mixed_window()
    log = "\n".join([hs(since - 10), hs(first300 + 30, EXHAUSTED), hs(cend + 5)])
    v = K.judge(base, t, since=since, until=cend + 10, exclude=[], retry_text=log)
    assert v["status"] == "ROLLBACK"
    assert v["reasons"][0].startswith("retry-exhausted") and any(r.startswith("INVALID:mode-mismatch") for r in v["reasons"])


def test_fr7_retry_exhaustion_only_in_the_128x3_part_does_not_roll_back():
    t, base, since, first300, cend = _mixed_window()
    log = "\n".join([hs(since - 10), hs(first300 - 100, EXHAUSTED), hs(cend + 5)])
    v = K.judge(base, t, since=since, until=cend + 10, exclude=[], retry_text=log)
    assert v["status"] == "INVALID" and v["reasons"][0].startswith("mode-mismatch")


def test_fr7_retry_exhaustion_beats_baseline_invalid():
    t, base, since, first300, cend = _mixed_window()
    base["days"] = 3.0
    pre_free = K.analyze(t, since, cend + 10, [])          # 참고: 창에 128x3 이 섞여 있다
    assert pre_free["mode_counts"].get("128x3")
    log = "\n".join([hs(since - 10), hs(first300 + 30, EXHAUSTED), hs(cend + 5)])
    v = K.judge(base, t, since=first300 - 1, until=cend + 10, exclude=[], retry_text=log)
    assert v["status"] == "ROLLBACK" and any(r.startswith("INVALID:baseline-invalid") for r in v["reasons"])


# ---------------------------------------------------------------- 후보 구간 로그 커버리지 (issue #98)

def _candidate_week(front_lost_days, cold_el=2.0, tail_gap=60.0):
    """7일 후보 창(since..until) 의 300x1 cold·warm 합성 로그. 사이드카 로그는 앞 front_lost_days 일이 없다(회전·잘못된
    --log-dir). 남은 뒷부분 지표는 기본적으로 기준선보다 좋고, cold_el 로 cold recall 지연을 올린다. 로그의 마지막 줄(idle trim)은
    until - tail_gap 에 찍힌다(기본은 until 바로 앞 = 꼬리 커버리지 충족). (기준선, 사이드카 로그 문자열, since, until)"""
    bl, bend = _cold_series(25, 2.0)
    base = K.baseline(text(bl), since=T0, until=bend, exclude=[])
    base["days"] = K.BASELINE_DAYS
    base["modes"]["128x3"].update(warm_recall_p95=5.0, warm_recall_p99=6.0)     # 합성 기준선에 warm 문턱을 채운다
    since = bend + 100
    until = since + WEEK
    start = since + front_lost_days * 86400 + 10
    lines = [proc(start - 5, 1, 1, 0.1)]
    if not front_lost_days:
        lines.insert(0, idle_trim(since - 60))             # 로그가 since 앞부터 있다(앞 커버리지 충족)
    for i in range(25):                                    # cold 1건(2.0s) + 60초 뒤 warm 1건(1.0s), 10분 간격
        t = start + 600 * i
        lines += [proc(t + cold_el, 300, 40000 + 10 * i, cold_el), proc(t + 61.0, 300, 50000 + 10 * i, 1.0)]
    assert t + 61.0 < until
    lines.append(idle_trim(until - tail_gap))
    return base, text(lines), since, until


def _retry_log(since, until, *exhausted_at):
    return "\n".join([hs(since - 10)] + [hs(t, EXHAUSTED) for t in exhausted_at] + [hs(until)])


def test_issue98_control_log_reaching_back_before_since_still_passes():
    base, t, since, until = _candidate_week(0)
    early = proc(since - 3600, 1, 1, 0.1)                  # 로그가 since 앞부터 있다 = 커버리지 충족
    v = K.judge(base, text(early, t), since=since, until=until, exclude=[], retry_text=_retry_log(since, until))
    assert v["status"] == "PASS" and v["reasons"] == []


def _coverage_msg(log_from, since):
    return (f"log-coverage: sidecar log starts at {log_from} after candidate start {since} - "
            "rotated files are missing or the window is longer than the logs")


@pytest.mark.parametrize("front_lost_days", [3, 6])
def test_issue98_front_lost_candidate_log_is_invalid_not_pass(front_lost_days):
    base, t, since, until = _candidate_week(front_lost_days)
    log_from = K.analyze(t, since, until, [])["log_from_ts"]
    v = K.judge(base, t, since=since, until=until, exclude=[], retry_text=_retry_log(since, until))
    assert v["status"] == "INVALID" and K.EXIT["INVALID"] == 4
    assert v["reasons"] == [_coverage_msg(log_from, since)]
    assert v["reasons"][0].startswith("log-coverage:")


def test_issue98_no_sidecar_log_at_all_is_invalid():
    base, _, since, until = _candidate_week(0)
    v = K.judge(base, "", since=since, until=until, exclude=[], retry_text=_retry_log(since, until))
    assert v["status"] == "INVALID" and v["reasons"] == [_coverage_msg(None, since)]


@pytest.mark.parametrize("front_lost_days,where", [
    (3, "covered"),        # 로그가 남은 구간(뒷부분)에서 소진
    (3, "lost"),           # 사이드카 로그가 사라진 앞부분에서 소진 - 재시도 로그는 since..until 전체를 본다
    (None, "anywhere"),    # 사이드카 로그가 전혀 없다
])
def test_issue98_retry_exhaustion_still_rolls_back_over_coverage_invalid(front_lost_days, where):
    base, t, since, until = _candidate_week(front_lost_days or 0)
    if front_lost_days is None:
        t = ""
    at = {"covered": since + 5 * 86400, "lost": since + 86400, "anywhere": since + 86400}[where]
    v = K.judge(base, t, since=since, until=until, exclude=[], retry_text=_retry_log(since, until, at))
    assert v["status"] == "ROLLBACK"
    assert v["reasons"][0] == "retry-exhausted:1"
    assert any(r.startswith("INVALID:log-coverage:") for r in v["reasons"])


def test_issue98_front_lost_log_does_not_hide_a_metric_rollback_in_the_covered_part():
    # 커버리지 부족은 PASS·HOLD·PENDING 만 막는다 - 덮인 구간의 지표 악화(cold p95 기준선 3.5 x 1.1 초과)는 수정 전처럼 ROLLBACK
    base, t, since, until = _candidate_week(2, cold_el=5.25)
    v = K.judge(base, t, since=since, until=until, exclude=[], retry_text=_retry_log(since, until))
    assert v["status"] == "ROLLBACK" and K.EXIT["ROLLBACK"] != K.EXIT["INVALID"]
    assert v["reasons"][0].startswith("cold-p95:") and v["reasons"][-1].startswith("INVALID:log-coverage:")
    assert len(v["reasons"]) == 2
    good = _candidate_week(2)
    v = K.judge(good[0], good[1], since=good[2], until=good[3], exclude=[], retry_text=_retry_log(good[2], good[3]))
    assert v["status"] == "INVALID"                         # 같은 손실에 뒷부분이 양호하면 INVALID(PASS 아님)


@pytest.mark.parametrize("exhausted,status,first_reason", [
    (False, "INVALID", "baseline-invalid"),
    (True, "ROLLBACK", "retry-exhausted:1"),                 # 방식 불일치가 없으니 since..until 전체가 후보 구간이다(라벨 없음)
])
def test_issue98_baseline_invalid_with_front_lost_log_counts_exhaustion_over_the_whole_window(exhausted, status, first_reason):
    base, t, since, until = _candidate_week(3)
    base["days"] = 13.0
    log = _retry_log(since, until, *([since + 86400] if exhausted else []))
    v = K.judge(base, t, since=since, until=until, exclude=[], retry_text=log)
    assert v["status"] == status and v["reasons"][0].startswith(first_reason)
    assert v["reasons"][-1].startswith(("INVALID:" if exhausted else "") + "log-coverage:")
    assert any(r.removeprefix("INVALID:").startswith("baseline-invalid") for r in v["reasons"])


def test_issue98_mode_mismatch_keeps_its_own_retry_window_and_adds_coverage_reason():
    # 창 시작 전 로그가 없고(커버리지 부족) 128x3 도 섞인 창: 소진 판정은 기존대로 300x1 이 돈 구간만 본다
    t, base, _, first300, cend = _mixed_window()
    since = T0 - 1000
    in300 = "\n".join([hs(since - 10), hs(first300 + 30, EXHAUSTED), hs(cend + 5)])
    v = K.judge(base, t, since=since, until=cend + 10, exclude=[], retry_text=in300)
    assert v["status"] == "ROLLBACK" and v["reasons"][0] == "retry-exhausted:1(300x1 part)"
    assert [r.split(":")[0] for r in v["reasons"][1:]] == ["INVALID", "INVALID"]
    assert v["reasons"][1].startswith("INVALID:mode-mismatch") and v["reasons"][2].startswith("INVALID:log-coverage:")
    in128 = "\n".join([hs(since - 10), hs(first300 - 100, EXHAUSTED), hs(cend + 5)])
    v = K.judge(base, t, since=since, until=cend + 10, exclude=[], retry_text=in128)
    assert v["status"] == "INVALID"
    assert v["reasons"][0].startswith("mode-mismatch") and v["reasons"][1].startswith("log-coverage:")


def test_fr8_warm_exceed_is_reported_but_not_judged():
    v = verdict(cand(warm_exceed=5))
    assert v["status"] == "PASS"
    lines, end = _cold_series(3, 2.0)
    assert "warm_exceed" in K.analyze(text(lines), T0, end, [])["modes"]["128x3"]


def test_fr9_baseline_is_invalid_when_the_log_does_not_reach_the_window_start():
    lines, end = _cold_series(3, 2.0)
    with pytest.raises(ValueError, match="log-coverage"):
        K.baseline(text(lines), since=T0 - 3600, until=end, exclude=[])


def test_fr10_deadline_outside_any_computing_request_is_unattached():
    r0 = proc(T0, 1, 1, 0.1)
    a, ea = recall_128x3(T0 + 400, chars=100)
    dl = deadline(ea + 2.0)                                # 계산 중인 요청 없음
    b, eb = recall_128x3(ea + 5.0, chars=200)              # 7.5s 안에 시작하는 다음 버스트
    t = text(r0, a, dl, b)
    gs = K.group_recalls(K.parse_requests(t), K.sidecar_events(t))
    assert [g["deadline"] for g in gs if g["saturated"]] == [0, 0]
    m = K.analyze(t, since=T0 - 1, until=eb + 1, exclude=[])
    assert m["unattached_deadlines"] == 1


# ---------------------------------------------------------------- 로그 커버리지 잔여 구멍 (issue #104, #98 후속)

TOL = K.LOG_TAIL_TOLERANCE_S
_TAIL_PREFIX = "log-coverage: sidecar log ends at"


def _tail_msg(log_to, until):
    return (f"{_TAIL_PREFIX} {log_to} before candidate end {until} - "
            "the log copy is stale or the files after it are missing")


def _judge(base, t, since, until, retry_text=None):
    return K.judge(base, t, since=since, until=until, exclude=[],
                   retry_text=_retry_log(since, until) if retry_text is None else retry_text)


# --- C4: 꼬리(until 근처까지 닿는지). baseline 과 judge 가 같은 규칙(coverage_problems)을 쓴다

@pytest.mark.parametrize("gap_days", [4, 5.5])
def test_issue104_c4_tail_lost_candidate_log_is_invalid_not_pass(gap_days):
    # 7일 창인데 로그가 until 보다 gap_days 일 앞에서 끊겼다(낡은 --log-dir 복사본). 수정 전에는 PASS 였다
    base, t, since, until = _candidate_week(0, tail_gap=gap_days * 86400)
    a = K.analyze(t, since, until, [])
    v = _judge(base, t, since, until)
    assert v["status"] == "INVALID" and K.EXIT[v["status"]] == 4
    assert v["reasons"] == [_tail_msg(a["log_to_ts"], until)]
    assert a["log_to_ts"] == pytest.approx(until - gap_days * 86400, abs=1)


def test_issue104_c4_quiet_weekend_tail_is_not_a_coverage_loss():
    # 금 18:30 마지막 줄(idle trim) -> 월 09:00 점검: 약 63시간 조용한 주말은 로그 손실이 아니다(허용 사흘 미만)
    gap = (2 * 24 + 14.5) * 3600
    assert gap < TOL
    base, t, since, until = _candidate_week(0, tail_gap=gap)
    v = _judge(base, t, since, until)
    assert v["status"] == "PASS" and v["reasons"] == []


def test_issue104_c4_tail_gap_boundary_is_inclusive_at_the_tolerance():
    # 경계 자체는 정수 초 값으로 결정적으로 본다(ms 반올림·부동소수에 기대지 않는다): 마지막 줄 + 허용 == until 이면 통과, 1초 모자라면 꼬리 사유
    since, until = T0, T0 + 14 * 86400.0
    assert K.coverage_problems(since - 60, until - TOL, since, until, "candidate") == []
    short = K.coverage_problems(since - 60, until - TOL - 1, since, until, "candidate")
    assert len(short) == 1 and short[0].startswith(_TAIL_PREFIX)
    # judge 끝에서 끝까지는 로그 줄 ms 반올림보다 큰 1초 여유로 양쪽을 본다
    base, t, since, until = _candidate_week(0, tail_gap=TOL - 1)
    assert _judge(base, t, since, until)["status"] == "PASS"
    base, t, since, until = _candidate_week(0, tail_gap=TOL + 1)
    assert _judge(base, t, since, until)["reasons"][0].startswith(_TAIL_PREFIX)


def test_issue104_c4_front_and_tail_lost_report_both_reasons():
    base, t, since, until = _candidate_week(3, tail_gap=4 * 86400)
    v = _judge(base, t, since, until)
    assert v["status"] == "INVALID"
    assert [r.split(" sidecar log ")[1].split(" ")[0] for r in v["reasons"]] == ["starts", "ends"]


def test_issue104_c4_tail_lost_does_not_hide_a_metric_rollback():
    # 커버리지 부족은 PASS·HOLD·PENDING 만 막는다 - 꼬리 손실이어도 덮인 구간의 지표 악화(cold p95 기준선 3.5 x 1.1 초과)는 ROLLBACK
    base, t, since, until = _candidate_week(0, cold_el=5.25, tail_gap=4 * 86400)
    v = _judge(base, t, since, until)
    assert v["status"] == "ROLLBACK" and K.EXIT["ROLLBACK"] != K.EXIT["INVALID"]
    assert v["reasons"][0].startswith("cold-p95:") and v["reasons"][-1].startswith("INVALID:" + _TAIL_PREFIX)
    good = _candidate_week(0, tail_gap=4 * 86400)
    assert _judge(*good)["status"] == "INVALID"                   # 같은 손실에 지표가 양호하면 INVALID(PASS 아님)


@pytest.mark.parametrize("where", ["covered", "lost-tail"])
def test_issue104_c4_tail_lost_does_not_hide_retry_exhaustion(where):
    # 재시도 소진은 즉시 ROLLBACK 이다 - 사이드카 로그가 남은 구간이든 끊긴 꼬리 구간이든 since..until 전체의 소진을 본다
    base, t, since, until = _candidate_week(0, tail_gap=4 * 86400)
    at = {"covered": since + 3600, "lost-tail": until - 86400}[where]
    v = _judge(base, t, since, until, _retry_log(since, until, at))
    assert v["status"] == "ROLLBACK" and v["reasons"][0] == "retry-exhausted:1"
    assert any(r.startswith("INVALID:" + _TAIL_PREFIX) for r in v["reasons"])


@pytest.mark.parametrize("front_ok,tail_ok", [(True, True), (False, True), (True, False), (False, False)])
def test_issue104_baseline_and_judge_share_the_coverage_rule(front_ok, tail_ok):
    """같은 로그·같은 창을 baseline 과 judge 에 넣으면 커버리지 판정이 같다(기준선과 후보의 기준이 갈라지면 비교가 무의미)."""
    since = T0
    until = since + 14 * 86400.0
    first = since - 60 if front_ok else since + 600
    lines = [idle_trim(first), proc(first + 5, 1, 1, 0.1), idle_trim(until - 60 if tail_ok else until - TOL - 60)]
    t = text(lines)
    expect = [] if front_ok and tail_ok else (["starts"] if not front_ok else []) + (["ends"] if not tail_ok else [])
    got = K.coverage_problems(*K.log_span(t), since, until, "baseline")
    assert [m.split(" sidecar log ")[1].split(" ")[0] for m in got] == expect
    if expect:
        with pytest.raises(ValueError) as e:
            K.baseline(t, since, until, exclude=[])
        assert str(e.value) == "; ".join(got)
    else:
        assert K.baseline(t, since, until, exclude=[])["log_from_ts"] == pytest.approx(first, abs=0.001)
    # judge: 후보 구간도 같은 문장(who 만 다르다)
    cand = K.coverage_problems(*K.log_span(t), since, until, "candidate")
    assert [m.replace("candidate", "baseline") for m in cand] == got


@pytest.mark.parametrize("front_lost_days,tail_gap,expect", [
    (0, 60.0, []), (3, 60.0, ["starts"]), (0, 4 * 86400.0, ["ends"]), (3, 4 * 86400.0, ["starts", "ends"])])
def test_issue104_judge_applies_front_and_tail_coverage_with_the_baseline_rule(front_lost_days, tail_gap, expect):
    base, t, since, until = _candidate_week(front_lost_days, tail_gap=tail_gap)
    v = _judge(base, t, since, until)
    assert [r.split(" sidecar log ")[1].split(" ")[0] for r in v["reasons"] if r.startswith("log-coverage")] == expect
    assert v["status"] == ("PASS" if not expect else "INVALID")
    got = K.coverage_problems(*K.log_span(t), since, until, "candidate")       # judge 가 낸 사유 == 공용 규칙 함수의 사유
    assert [r for r in v["reasons"] if r.startswith("log-coverage")] == got


def test_issue104_c4_missing_middle_generation_is_a_documented_limit():
    # 의도된 한계(bench/README.md): 로그 중간 세대(.2.gz) 누락은 시각만으로는 조용한 주말과 구별되지 않아 보지 않는다.
    # 앞(since)과 꼬리(until)가 닿으면 통과한다 - 이 테스트는 그 한계를 고정해 두어 고치려는 사람이 README 근거를 먼저 읽게 한다
    since, until = T0, T0 + WEEK
    t = text(idle_trim(since - 60), proc(since + 100, 1, 1, 0.1), idle_trim(until - 60))   # 사이에 4일 공백
    assert K.coverage_problems(*K.log_span(t), since, until, "candidate") == []


# --- C11: 회전 세대 수 제한 없음

def test_issue104_c11_read_sidecar_logs_discovers_every_generation_oldest_first(tmp_path):
    def gz(name, line):
        with gzip.open(tmp_path / name, "wt") as f:
            f.write(line + "\n")
    for n, chars in ((5, 5), (4, 4), (2, 2), (1, 1), (10, 10)):                        # .10 은 .9 보다 오래됐다(숫자순). .3 은 없다
        gz(f"server.log.{n}.gz", proc(T0 + 100 - n, 1, chars, 0.1))
    (tmp_path / "server.log").write_text(proc(T0 + 200, 1, 0, 0.1) + "\n")
    for noise in ("server.log.bak", "server.log.x.gz", "probe.log", "server.log.1.gz.tmp", "xserver.log.2.gz",
                  "server.log.3", "server.log.7"):                                     # 압축 전 세대는 회전 중간 산물이라 읽지 않는다
        (tmp_path / noise).write_text(proc(T0 + 1, 1, 99, 0.1) + "\n")
    t, files, _ = K.read_sidecar_logs(str(tmp_path))
    assert [f.rsplit("/", 1)[1] for f in files] == [f"server.log.{n}.gz" for n in (10, 5, 4, 2, 1)] + ["server.log"]
    assert [x["chars"] for x in K.parse_requests(t)] == [10, 5, 4, 2, 1, 0]


def test_issue104_c11_line_only_in_an_old_generation_keeps_the_front_covered(tmp_path):
    # 수정 전: .4.gz 의 since 직후 줄이 빠져 log-coverage INVALID 가 났다
    with gzip.open(tmp_path / "server.log.4.gz", "wt") as f:
        f.write(idle_trim(T0 - 3600) + "\n" + proc(T0 + 10, 1, 4, 0.1) + "\n")
    (tmp_path / "server.log").write_text(proc(T0 + 500, 1, 0, 0.1) + "\n")
    t, files, _ = K.read_sidecar_logs(str(tmp_path))
    assert files[0].endswith("server.log.4.gz")
    assert [x["chars"] for x in K.parse_requests(t)] == [4, 0]
    assert K.coverage_problems(*K.log_span(t), T0, T0 + 600, "candidate") == []


def test_issue104_c11_uncompressed_generation_is_ignored_so_rotation_never_double_counts(tmp_path):
    # rotate-logs.sh 는 cp -> truncate -> gzip 순서다. cp 뒤 truncate 전에는 server.log.1 이 server.log 와 같은 줄을 담아 중복 집계되고,
    # gzip 이 끝나면 server.log.1 이 지워져 listdir 뒤 open 이 FileNotFoundError 가 난다. 압축본(.gz)만 읽는 편이 덜 읽어도 안전하다
    (tmp_path / "server.log.1").write_text(proc(T0 + 10, 1, 7, 0.1) + "\n")                # 압축 전 단독: 무시
    (tmp_path / "server.log.2").write_text(proc(T0 + 5, 1, 6, 0.1) + "\n")
    with gzip.open(tmp_path / "server.log.2.gz", "wt") as f:                              # 같은 세대 압축본이 함께 있어도 한 번만
        f.write(proc(T0 + 5, 1, 6, 0.1) + "\n")
    (tmp_path / "server.log").write_text(proc(T0 + 20, 1, 8, 0.1) + "\n")
    t, files, _ = K.read_sidecar_logs(str(tmp_path))
    assert [f.rsplit("/", 1)[1] for f in files] == ["server.log.2.gz", "server.log"]
    assert [x["chars"] for x in K.parse_requests(t)] == [6, 8]
    assert K.sidecar_log_files(str(tmp_path))[:-1] == [str(tmp_path / "server.log.2.gz")]


def test_issue104_c11_missing_log_dir_is_empty_not_a_crash(tmp_path):
    assert K.read_sidecar_logs(str(tmp_path / "nope")) == ("", [], [])


# --- issue #119 C3: 회전 세대 수를 줄이면 rotate-logs.sh 가 더는 덮지 않는 고아 고세대가 낡은 줄을 남긴다
# 고아는 체인(.1.gz ... server.log)보다 항상 오래돼 시각 단조성으로는 안 걸러진다. 대신 since(집계 창 시작)가 파일 경계의 사흘 넘는 공백
# 안에 있으면(앞 파일은 통째로 since 앞, 다음 파일은 since 뒤) 그 앞 세대(와 더 오래된 것)를 읽지 않는다. 그 밖에는 자르지 않는다 - 앞 세대의
# 요청은 지표·idle 입력이라 버리면 ROLLBACK 신호가 사라진다(final-review). 허용치·세대 발견은 #104 것을 그대로 쓰고 RULES 는 늘리지 않는다.
# 모든 입력은 tmp_path 의 합성 로그다(운영 로그 디렉터리를 열지 않는다).

def _gz_log(path, *lines):
    with gzip.open(path, "wt") as f:
        f.write("\n".join(lines) + "\n")


def _orphan_dir(tmp_path, gap_s, orphan_gens=(5,), chain_text=None, since=T0):
    """고아 세대 orphan_gens(.N.gz, 마지막 줄 = 체인 첫 줄 - gap_s)와 체인(.1.gz 와 server.log)을 쓴다. 체인은 since 뒤에서 시작한다
    (since 앞 로그가 실제로는 없다). (체인 첫 줄 시각, server.log 에 chain_text 를 주면 그 본문)."""
    chain_first = since + 600
    for n in orphan_gens:                                  # 더 큰 N 일수록 더 오래된 고아 - 모두 체인 첫 줄 gap_s 앞에서 끝난다
        _gz_log(tmp_path / f"server.log.{n}.gz", idle_trim(chain_first - gap_s - (n - min(orphan_gens)) * 60))
    _gz_log(tmp_path / "server.log.1.gz", idle_trim(chain_first), proc(chain_first + 5, 1, 1, 0.1))
    (tmp_path / "server.log").write_text(chain_text if chain_text is not None else idle_trim(since + 3 * 86400) + "\n")
    return chain_first


def _front_reason(problems):
    return [p for p in problems if p.startswith("log-coverage: sidecar log starts at")]


@pytest.mark.parametrize("orphan_gens", [(4,), (5,), (4, 5), (7, 12)])
def test_issue119_c3_orphan_high_generation_does_not_fake_the_front_coverage(tmp_path, orphan_gens):
    """재현: since 보다 훨씬 이른 줄 하나뿐인 고아 .N.gz 와 since 뒤에서 시작하는 체인. 수정 전: 앞 커버리지 통과(log_from 이 고아 줄)."""
    since, until = T0, T0 + WEEK
    chain_first = _orphan_dir(tmp_path, 10 * 86400, orphan_gens, since=since)
    t, files, _ = K.read_sidecar_logs(str(tmp_path), since)
    assert [f.rsplit("/", 1)[1] for f in files] == ["server.log.1.gz", "server.log"]      # sources 에 고아가 없다
    assert len(K.read_sidecar_logs(str(tmp_path))[1]) == len(orphan_gens) + 2             # since 를 안 주면 자르지 않는다
    assert K.log_span(t)[0] == pytest.approx(chain_first, abs=0.001)
    assert len(_front_reason(K.coverage_problems(*K.log_span(t), since, until, "candidate"))) == 1


@pytest.mark.parametrize("gap_s,kept", [(K.LOG_TAIL_TOLERANCE_S, True), (K.LOG_TAIL_TOLERANCE_S + 1, False),
                                         (3600.0, True)])
def test_issue119_c3_generation_boundary_gap_is_inclusive_at_the_tail_tolerance(tmp_path, gap_s, kept):
    # 허용치는 꼬리와 같은 값(조용한 주말 약 63시간 < 사흘)이다. 경계 자체는 정수 초라 결정적이다. 허용 안쪽 공백은 이어진 로그로 본다
    # (고아와 구별되지 않고, 앞 커버리지도 그 허용만큼은 닿은 것으로 본다)
    chain_first = _orphan_dir(tmp_path, gap_s, (4,))
    t, files, _ = K.read_sidecar_logs(str(tmp_path), T0)
    assert (len(files) == 3) is kept
    assert K.log_span(t)[0] == pytest.approx(chain_first - gap_s if kept else chain_first, abs=0.001)


def test_issue119_c3_contiguous_generations_are_all_read_unchanged(tmp_path):
    # 진행 중 카나리와 같은 정상 입력: 세대 사이 공백이 허용 안쪽이면 이전과 같은 파일 목록·본문이다
    for n, base in ((3, 0), (2, 40000), (1, 80000)):
        _gz_log(tmp_path / f"server.log.{n}.gz", idle_trim(T0 + base), proc(T0 + base + 5, 1, n, 0.1))
    (tmp_path / "server.log").write_text(idle_trim(T0 + 120000) + "\n")
    t, files, _ = K.read_sidecar_logs(str(tmp_path), T0 + 1)
    assert [f.rsplit("/", 1)[1] for f in files] == ["server.log.3.gz", "server.log.2.gz", "server.log.1.gz", "server.log"]
    assert [x["chars"] for x in K.parse_requests(t)] == [3, 2, 1]
    assert K.log_span(t) == (pytest.approx(T0, abs=0.001), pytest.approx(T0 + 120000, abs=0.001))


def test_issue119_c3_orphan_older_than_a_gap_is_found_by_time_not_by_generation_number(tmp_path):
    # 세대 번호 상한(예: 3)은 쓰지 않는다: RERANK_LOG_GENERATIONS 는 설정이라 올리면 .4·.5 도 정상 세대다 - 이어져 있으면 읽는다
    for n, base in ((5, 0), (4, 40000), (3, 80000)):
        _gz_log(tmp_path / f"server.log.{n}.gz", idle_trim(T0 + base), proc(T0 + base + 5, 1, n, 0.1))
    (tmp_path / "server.log").write_text(idle_trim(T0 + 120000) + "\n")
    t, files, _ = K.read_sidecar_logs(str(tmp_path), T0 + 100000)
    assert [f.rsplit("/", 1)[1] for f in files][:3] == ["server.log.5.gz", "server.log.4.gz", "server.log.3.gz"]


def test_issue119_c3_orphans_are_monotone_so_time_ordering_cannot_find_them(tmp_path):
    # 기각한 가설(세대 간 시각 단조성): 고아는 체인보다 항상 이전이라 순서가 어긋나지 않는다 - 단조성 위반으로는 걸러지지 않는다
    _orphan_dir(tmp_path, 10 * 86400, (5,))
    spans = []
    for name in ("server.log.5.gz", "server.log.1.gz", "server.log"):
        p = tmp_path / name
        body = gzip.open(p, "rt").read() if name.endswith(".gz") else p.read_text()
        spans.append(K.log_span(body))
    assert all(a[1] <= b[0] for a, b in zip(spans, spans[1:]))


@pytest.mark.parametrize("consumer", ["baseline", "judge"])
def test_issue119_c3_baseline_and_judge_get_the_same_log_so_the_same_coverage_verdict(tmp_path, consumer):
    """baseline() 과 judge() 는 같은 read_sidecar_logs 본문·같은 coverage_problems 를 쓴다 - 고아를 둔 같은 디렉터리에서 사유가 같다."""
    since, until = T0, T0 + 14 * 86400.0
    _orphan_dir(tmp_path, 10 * 86400, (5,), since=since, chain_text=text(idle_trim(since + 700), idle_trim(until - 60)))   # 파일 경계는 이어지고(공백 허용 안), 꼬리는 until 에 닿는다
    t, _, _ = K.read_sidecar_logs(str(tmp_path), since)
    expect = K.coverage_problems(*K.log_span(t), since, until, "baseline")
    assert len(_front_reason(expect)) == 1
    if consumer == "baseline":
        with pytest.raises(ValueError) as e:
            K.baseline(t, since, until, exclude=[])
        assert str(e.value) == "; ".join(expect)
    else:
        base, _, _, _ = _candidate_week(0)
        v = K.judge(base, t, since, until, [], None)
        assert [r for r in v["reasons"] if r.startswith("log-coverage")] == \
            [m.replace("baseline", "candidate") for m in expect]


@pytest.mark.parametrize("outcome", ["INVALID", "retry-exhausted", "cold-p95"])
def test_issue119_c3_orphan_front_shortfall_never_hides_a_rollback_signal(tmp_path, outcome):
    """커버리지 부족(고아가 가렸던 앞)은 ROLLBACK 을 가리지 않는다: 같은 입력에서 소진·지표 악화는 ROLLBACK, 양호하면 INVALID."""
    base, t, since, until = _candidate_week(3, cold_el=5.25 if outcome == "cold-p95" else 2.0)
    _gz_log(tmp_path / "server.log.5.gz", idle_trim(since - 10 * 86400))
    (tmp_path / "server.log").write_text(t)
    read, files, _ = K.read_sidecar_logs(str(tmp_path), since)
    assert [f.rsplit("/", 1)[1] for f in files] == ["server.log"]
    retry = _retry_log(since, until, *([since + 86400] if outcome == "retry-exhausted" else []))
    v = K.judge(base, read, since, until, [], retry)
    if outcome == "INVALID":
        assert v["status"] == "INVALID" and K.EXIT["INVALID"] == 4
    else:
        assert v["status"] == "ROLLBACK" and K.EXIT["ROLLBACK"] == 1
        assert v["reasons"][0].startswith("retry-exhausted:1" if outcome == "retry-exhausted" else "cold-p95:")
    assert v["reasons"][-1].removeprefix("INVALID:").startswith("log-coverage: sidecar log starts at")


def test_issue119_c3_normal_generation_before_a_long_stop_is_kept_so_its_rollback_signal_survives(tmp_path):
    """final-review(Codex·Spec c): 사이드카가 사흘 넘게 멈춘 뒤 재개돼 회전 경계에 큰 공백이 생겨도, since 를 덮는 세대는 고아가 아니다.
    그 세대의 지표 악화(cold p95)는 그대로 ROLLBACK 이다(무조건 자르면 앞 커버리지 INVALID 로 바뀌고 이 신호가 사라진다)."""
    base, t, since, until = _candidate_week(0, cold_el=5.25)
    old = t.splitlines()[:-1]                                                      # 마지막 줄(until 앞 idle trim)은 재개 뒤 줄로 옮긴다
    _gz_log(tmp_path / "server.log.1.gz", *old)                                    # since 앞 줄부터 since 뒤 4시간까지(since 를 덮는다)
    (tmp_path / "server.log").write_text(idle_trim(until - 60) + "\n")             # 약 6.8일 뒤에야 재개 - 파일 경계 공백이 허용을 넘는다
    read, files, _ = K.read_sidecar_logs(str(tmp_path), since)
    assert [f.rsplit("/", 1)[1] for f in files] == ["server.log.1.gz", "server.log"]
    assert until - 60 - K.log_span(text(old))[1] > K.LOG_TAIL_TOLERANCE_S
    v = K.judge(base, read, since, until, [], _retry_log(since, until))
    assert v["status"] == "ROLLBACK" and v["reasons"][0].startswith("cold-p95:")
    assert not any("log-coverage" in r for r in v["reasons"])


def test_issue119_c3_old_requests_before_a_gap_are_kept_when_the_chain_already_reaches_since(tmp_path):
    """final-review(C2): 체인이 since 에 닿으면 앞 세대는 자르지 않는다. 자르면 공백 뒤 첫 recall 의 idle 이 None 이 돼 deep cold 표본이 빠진다."""
    since = T0
    _gz_log(tmp_path / "server.log.3.gz", idle_trim(since - 6 * 86400), proc(since - 5 * 86400, 1, 1, 0.1))
    (tmp_path / "server.log").write_text(text(idle_trim(since - 86400), proc(since + 100, 300, 777, 2.0)))
    t, files, _ = K.read_sidecar_logs(str(tmp_path), since)
    assert [f.rsplit("/", 1)[1] for f in files] == ["server.log.3.gz", "server.log"]
    a = K.analyze(t, since, since + 3600, [])
    assert a["modes"]["300x1"]["cold_recalls"] == 1 and a["modes"]["300x1"]["deep_cold_recalls"] == 1
    assert K.coverage_problems(a["log_from_ts"], a["log_to_ts"], since, since + 3600, "candidate") == []


def test_issue119_c3_cli_judge_ignores_the_orphan_and_lists_only_the_read_files(tmp_path):
    since = "2026-09-10 12:00:00"
    logs = tmp_path / "logs"
    logs.mkdir()
    _orphan_dir(logs, 10 * 86400, (5,), chain_text=text(idle_trim(T0 + 700), idle_trim(T0 + 8 * 86400)))
    base, _, _, _ = _candidate_week(0)
    (tmp_path / "base.json").write_text(json.dumps(base))
    (tmp_path / "ex.txt").write_text("")
    out = tmp_path / "o.json"
    rc = K.main(["judge", "--baseline", str(tmp_path / "base.json"), "--since", since, "--until", "2026-09-17 12:00:00",
                 "--exclude", str(tmp_path / "ex.txt"), "--log-dir", str(logs), "--out", str(out)])
    res = json.loads(out.read_text())
    assert rc == K.EXIT["INVALID"] and res["status"] == "INVALID"
    assert [s.rsplit("/", 1)[1] for s in res["sources"]] == ["server.log.1.gz", "server.log"]
    assert any(r.startswith("log-coverage: sidecar log starts at") for r in res["reasons"])


# --- C12: log_from_ts 는 파일 실제 시작(첫 시각 줄)이다

def test_issue104_c12_first_log_line_before_since_covers_the_idle_gap_before_the_first_request():
    since = T0
    t = text(idle_trim(since - 600), proc(since + 3601, 1, 1, 0.1))                    # 새벽 idle: since 뒤 첫 요청은 한 시간 뒤
    a = K.analyze(t, since, since + 7200, [])
    assert a["log_from_ts"] == pytest.approx(since - 600, abs=0.001)                  # 수정 전: 요청 끝 since + 3601
    assert K.coverage_problems(a["log_from_ts"], a["log_to_ts"], since, since + 7200, "baseline") == []
    K.baseline(t, since, since + 7200, [])                                              # 수정 전: log-coverage ValueError


def test_issue104_c12_message_reports_the_actual_first_line_not_the_first_request():
    since = T0
    t = text(idle_trim(since + 300), proc(since + 3601, 1, 1, 0.1))                    # 첫 줄도 since 뒤 -> 부족, 시각은 첫 줄
    a = K.analyze(t, since, since + 7200, [])
    msg = K.coverage_problems(a["log_from_ts"], a["log_to_ts"], since, since + 7200, "candidate")[0]
    assert f"starts at {a['log_from_ts']} after candidate start {since}" in msg
    assert a["log_from_ts"] == pytest.approx(since + 300, abs=0.001)


def test_issue104_c12_timestamp_after_a_progress_bar_carriage_return_counts_as_a_log_line():
    since = T0
    t = text("Batches:  25%|##   | 1/4 [00:05<00:15,  5.31s/it]\r" + idle_trim(since - 30), proc(since + 100, 1, 1, 0.1))   # tqdm 은 \r 로 줄을 덮는다
    assert K.log_span(t)[0] == pytest.approx(since - 30, abs=0.001)
    assert K.log_span("no timestamps here\n") == (None, None)


def test_issue104_c12_timestamp_strings_inside_a_message_do_not_move_the_log_span():
    # 줄 머리(또는 \r 뒤)가 아닌 시각 문자열(traceback·메시지 본문)은 커버리지 증거가 아니다. 이른 값은 앞을, 미래 값은 꼬리를 거짓 통과시킨다
    since, until = T0, T0 + WEEK
    real_from, real_to = since + 600, since + 700
    early = f"WARNING something retried from {kst(since - 5 * 86400)} and gave up"
    future = f"ERROR deadline was {kst(until + 5 * 86400)}"
    t = text(idle_trim(real_from), early, future, f"File \"x.py\", line 1 ({kst(since - 86400)})", idle_trim(real_to))
    lo, hi = K.log_span(t)
    assert lo == pytest.approx(real_from, abs=0.001) and hi == pytest.approx(real_to, abs=0.001)
    assert K.coverage_problems(lo, hi, since, until, "candidate")[0].startswith("log-coverage: sidecar log starts at")
    assert K.log_span(early + "\n") == (None, None)


def test_issue104_c12_unparseable_timestamp_strings_are_skipped_not_raised():
    # 형식만 맞는 날짜(달 13, 일 39, 시 25)가 줄 머리에 있어도 ValueError 로 판정을 깨지 않는다 - 건너뛰고 유효한 값만 쓴다
    since = T0
    t = text("2026-13-45 99:99:99,123 INFO bogus", idle_trim(since + 5), "2026-02-30 00:00:00,000 INFO also bogus",
             "9999-99-99 24:61:61,999 INFO future bogus")
    assert K.log_span(t) == (pytest.approx(since + 5, abs=0.001), pytest.approx(since + 5, abs=0.001))
    assert K.log_span("2026-13-45 99:99:99,123 INFO only bogus\n") == (None, None)


# --- C1: 사이드카 앞이 빠졌는데 hindsight 앞도 빠진 로그가 measured 로 나오지 않는다

def test_issue104_c1_front_lost_sidecar_with_late_hindsight_log_is_partial_not_measured():
    base, t, since, until = _candidate_week(3)
    late = "\n".join([hs(since + 3 * 86400 + 5), hs(until)])                           # hindsight 로그도 since 에서 3일 뒤에 시작
    v = _judge(base, t, since, until, late)
    assert v["retries"]["status"] == "partial"                                          # 수정 전: measured (need_from 이 남은 첫 요청)
    assert v["status"] == "INVALID"                                                     # 판정은 같다 - 보고만 바로잡는다
    # 대조: 사이드카 앞이 닿으면 need_from 은 그대로 첫 요청 시작이다(배포 때 재생성한 hindsight 로그가 첫 요청 앞이면 measured)
    base0, t0, since0, until0 = _candidate_week(0)
    ok = "\n".join([hs(since0 + 5), hs(until0)])
    assert _judge(base0, text(idle_trim(since0 - 60), t0), since0, until0, ok)["retries"]["status"] == "measured"


def test_issue104_c1_coverage_shortfall_never_changes_the_rollback_signal():
    # C1 은 보고 상태만 바꾼다: 재시도 소진은 since..until 전체를 세므로 partial 이어도 ROLLBACK
    base, t, since, until = _candidate_week(3)
    late = "\n".join([hs(since + 3 * 86400 + 5), hs(since + 4 * 86400, EXHAUSTED), hs(until)])
    v = _judge(base, t, since, until, late)
    assert v["status"] == "ROLLBACK" and v["reasons"][0] == "retry-exhausted:1" and v["retries"]["status"] == "partial"


# ---------------------------------------------------------------- 벤치 keep-warm 구간 제외 (issue #79)

def test_keepwarm_meta_exclude_line_drops_the_recall_inside_the_window_from_the_canary_sample():
    """run meta 의 제외 줄이 제외 파일 파서를 그대로 통과하고, 그 구간의 recall·keep-warm 요청이 표본에서 빠진다."""
    from bench.keepwarm import KeepWarm
    clock = {"t": T0 + 100.4}
    sent = []
    kw = KeepWarm(lambda: sent.append(clock["t"]) or 200, now=lambda: clock["t"], interval_s=30.0)
    kw.start()
    for k in range(21):                                                        # run 이 도는 동안 30초마다: 100.4 ... 700.4
        clock["t"] = T0 + 100.4 + 30 * k
        kw.tick()
    clock["t"] = T0 + 700.6
    kw.stop()
    ex = K.parse_exclude(kw.meta("r1")["exclude_line"])                       # 창: [T0+100, ceil(stop 700.6 + 300) = T0+1001]
    assert (ex[0][0], ex[0][1]) == (T0 + 100, T0 + 1001)
    # 로그: 창 앞 작은 요청, keep-warm 요청들(1 후보), 창 안 포화 recall, 창 뒤 포화 recall
    before = proc(T0, 1, 1, 0.1)
    warm = [proc(t + 0.05, 1, 50000 + i, 0.05) for i, t in enumerate(sent)]
    inside, ei = recall_128x3(T0 + 450)                                        # 창 안 - keep-warm 이 데워 둬서 warm 으로 분류된다
    tail, et = recall_128x3(T0 + 800, chars=3000)                              # run 끝(T0+700) 뒤 100초 - 마지막 keep-warm 때문에 warm 이 될 꼬리
    after, ea = recall_128x3(T0 + 1001 + 400, chars=2000)                      # 창 뒤 - 마지막 keep-warm 뒤 5분 넘게 idle 이라 cold
    t = text(before, warm, inside, tail, after)
    kept = K.analyze(t, since=T0 - 1, until=ea + 1, exclude=ex)
    plain = K.analyze(t, since=T0 - 1, until=ea + 1, exclude=[])
    assert plain["saturated_recalls"] == 3
    assert (plain["modes"]["128x3"]["cold_recalls"], plain["modes"]["128x3"]["warm_recalls"]) == (1, 2)   # 창 안·꼬리는 warm 으로 센다
    assert kept["saturated_recalls"] == 3 and kept["excluded_recalls"] == 2    # 창 안 recall 과 run 끝 뒤 warm 꼬리 recall 이 빠진다
    assert (kept["modes"]["128x3"]["cold_recalls"], kept["modes"]["128x3"]["warm_recalls"]) == (1, 0)   # 창 뒤 cold 는 남는다
    assert plain["unsaturated_segments"] == len(sent) + 1                      # keep-warm 요청 조각 + 창 앞 작은 요청
    assert kept["unsaturated_segments"] == 1                                   # keep-warm 조각은 제외 구간 안이라 표본에서 빠진다
    assert kept["exclude"][0][2].startswith("벤치 keep-warm 구간 r1")


# --- issue #142: 끝이 잘린(손상된) 회전본 .gz 하나가 judge 전체를 EOFError 로 죽이지 않게 한다
# 손상 세대는 읽히는 앞부분까지만 쓰고(반쯤 디코드된 마지막 줄은 버린다) 사유에 파일 이름을 남긴다. 읽는 세대(커버리지 안)의 손상은
# 커버리지 부족과 같게 PASS·HOLD·PENDING 만 INVALID 로 막고 ROLLBACK 은 가리지 않는다. 고아 컷으로 빠지는 세대(커버리지 밖)는 판정 불변.
# 모든 입력은 tmp_path 의 합성 로그다(운영 로그 디렉터리를 열지 않는다).

_CUTS = ("header", "body", "trailer", "empty")


def _truncate_gz(path, cut):
    """정상 gz 를 바이트 단위로 자른다. header: gzip 헤더 안, body: 압축 본문 중간, trailer: CRC·길이 꼬리 안, empty: 0 바이트."""
    raw = path.read_bytes()
    keep = {"header": 5, "body": len(raw) // 2, "trailer": len(raw) - 4, "empty": 0}[cut]
    path.write_bytes(raw[:keep])


def _names(files):
    return [f.rsplit("/", 1)[1] for f in files]


def _damage_reasons(reasons):
    return [r for r in reasons if "truncated or corrupt" in r]


@pytest.mark.parametrize("cut", _CUTS)
def test_issue142_truncated_gz_judge_finishes_and_names_the_damaged_file(tmp_path, cut):
    """재현: since 앞부터 덮는 server.log.1.gz 가 끝이 잘렸다. 수정 전: gzip 이 EOFError 를 내 judge CLI 가 traceback 으로 죽었다
    (main 은 OSError·ValueError 만 INVALID 로 바꾼다). 수정 뒤: 예외 없이 판정 JSON 을 쓰고 사유에 손상 파일 이름이 남는다."""
    good, t, since, until = _candidate_week(0)
    lines = t.splitlines()
    _gz_log(tmp_path / "server.log.1.gz", proc(since - 3600, 1, 1, 0.1), *lines[:-1])
    (tmp_path / "server.log").write_text(lines[-1] + "\n")
    _truncate_gz(tmp_path / "server.log.1.gz", cut)
    (tmp_path / "ex.txt").write_text("")
    (tmp_path / "base.json").write_text(json.dumps(good))
    (tmp_path / "hs.log").write_text(_retry_log(since, until))
    out = tmp_path / "o.json"
    rc = K.main(["judge", "--baseline", str(tmp_path / "base.json"), "--since", kst(since)[:19], "--until", kst(until)[:19],
                 "--exclude", str(tmp_path / "ex.txt"), "--log-dir", str(tmp_path), "--hindsight-log", str(tmp_path / "hs.log"),
                 "--out", str(out)])
    res = json.loads(out.read_text())
    assert rc == K.EXIT["INVALID"] and res["status"] == "INVALID"
    assert _names(res["sources"]) == ["server.log.1.gz", "server.log"]
    assert len(_damage_reasons(res["reasons"])) == 1 and "server.log.1.gz" in _damage_reasons(res["reasons"])[0]


def _older_gen(path, since, n=200):
    """since 앞에서 끝나는 정상 세대(.gz). 줄이 많아 본문 중간을 잘라도 앞부분이 읽힌다. 다음 세대와의 공백은 허용 안쪽이다."""
    _gz_log(path, proc(since - 7200, 1, 1, 0.1), *[idle_trim(since - 7000 + 30 * k) for k in range(n)])


def _run_judge(log_dir, surface, base, since, until, retry_text):
    """같은 로그 디렉터리를 두 표면(api: read_sidecar_logs + judge, cli: main judge)으로 판정한다. (status, reasons, sources 이름)."""
    if surface == "api":
        t, files, damaged = K.read_sidecar_logs(str(log_dir), since)
        v = K.judge(base, t, since, until, [], retry_text, damaged)
        return v["status"], v["reasons"], _names(files)
    work = log_dir.parent / f"{log_dir.name}-cli"
    work.mkdir()
    (work / "base.json").write_text(json.dumps(base))
    (work / "ex.txt").write_text("")
    argv = ["judge", "--baseline", str(work / "base.json"), "--since", kst(since)[:19], "--until", kst(until)[:19],
            "--exclude", str(work / "ex.txt"), "--log-dir", str(log_dir), "--out", str(work / "o.json")]
    if retry_text is not None:
        (work / "hs.log").write_text(retry_text)
        argv += ["--hindsight-log", str(work / "hs.log")]
    rc = K.main(argv)
    res = json.loads((work / "o.json").read_text())
    assert rc == K.EXIT[res["status"]]
    return res["status"], res["reasons"], _names(res["sources"])


_INSIDE = {   # 손상 없는 입력의 판정 -> (cold_el, until 을 since 뒤 며칠로, 재시도 로그: None 이면 없음, 소진 시각 오프셋)
    "PASS": (2.0, 7, True, ()),
    "HOLD": (2.0, 7, False, ()),                       # 재시도 로그 없음 = unmeasured -> 판정 시점에 HOLD
    "PENDING": (2.0, 3, True, ()),                     # 7일 전
    "ROLLBACK-retry-exhausted": (2.0, 7, True, (86400,)),
    "ROLLBACK-cold-p95": (5.25, 7, True, ()),
}


@pytest.mark.parametrize("surface", ["api", "cli"])
@pytest.mark.parametrize("cut", _CUTS)
@pytest.mark.parametrize("outcome", list(_INSIDE))
def test_issue142_damage_inside_coverage_blocks_pass_hold_pending_but_never_hides_rollback(tmp_path, outcome, cut, surface):
    """읽는 세대(since 앞에서 since 직전까지 이어지는 .1.gz)가 손상: PASS·HOLD·PENDING 은 INVALID(사유 = 손상 하나), ROLLBACK 은 그대로
    ROLLBACK(원래 사유 뒤에 INVALID: 손상 병기). 같은 입력의 손상 없는 대조군이 기대 판정을 먼저 증명한다."""
    cold_el, days, with_retry, exhausted = _INSIDE[outcome]
    base, t, since, _ = _candidate_week(0, cold_el=cold_el)
    until = since + days * 86400
    retry = _retry_log(since, until, *[since + x for x in exhausted]) if with_retry else None
    got = {}
    for kind in ("control", "damaged"):
        d = tmp_path / kind
        d.mkdir()
        _older_gen(d / "server.log.1.gz", since)
        (d / "server.log").write_text(t)
        if kind == "damaged":
            _truncate_gz(d / "server.log.1.gz", cut)
        got[kind] = _run_judge(d, surface, base, since, until, retry)
    (cs, cr, cf), (ds, dr, df) = got["control"], got["damaged"]
    assert cs == outcome.split("-")[0] and _damage_reasons(cr) == []
    assert cf == df == ["server.log.1.gz", "server.log"]
    msg = (f"log-coverage: sidecar log server.log.1.gz is truncated or corrupt "
           f"({'empty file' if cut == 'empty' else 'EOFError'}) - only its readable first part was used")
    if cs == "ROLLBACK":
        assert ds == "ROLLBACK" and dr == cr + [f"INVALID:{msg}"]
    else:
        assert ds == "INVALID" and dr == [msg]


@pytest.mark.parametrize("surface", ["api", "cli"])
@pytest.mark.parametrize("cut", _CUTS)
@pytest.mark.parametrize("outcome", ["INVALID", "retry-exhausted", "cold-p95"])
def test_issue142_damaged_orphan_outside_coverage_does_not_change_the_verdict(tmp_path, outcome, cut, surface):
    """고아 컷으로 빠지는 세대(since 열흘 앞, 체인은 since 사흘 뒤 시작)가 손상이어도 판정은 손상 없는 같은 입력과 같다. 줄이 읽히면
    (body·trailer) 고아로 잘려 sources 는 같고, 컷 경계 세대라 'not read' 손상 사유만 더해진다(절단이 공백을 만들었을 수 있다).
    줄이 하나도 안 읽히면(header·empty) 시각을 몰라 고아로 가를 수 없어 읽는 쪽에 남고 손상 사유가 붙는다. 어느 쪽이든 체인이 since 에
    닿지 않아 원래도 앞 커버리지 부족이라 판정(INVALID 또는 ROLLBACK)은 같다."""
    base, t, since, until = _candidate_week(3, cold_el=5.25 if outcome == "cold-p95" else 2.0)
    retry = _retry_log(since, until, *([since + 4 * 86400] if outcome == "retry-exhausted" else []))
    got = {}
    for kind in ("control", "damaged"):
        d = tmp_path / kind
        d.mkdir()
        _gz_log(d / "server.log.5.gz", *[idle_trim(since - 10 * 86400 + 30 * k) for k in range(200)])
        (d / "server.log").write_text(t)
        if kind == "damaged":
            _truncate_gz(d / "server.log.5.gz", cut)
        got[kind] = _run_judge(d, surface, base, since, until, retry)
    (cs, cr, cf), (ds, dr, df) = got["control"], got["damaged"]
    assert cs == ("INVALID" if outcome == "INVALID" else "ROLLBACK") and cf == ["server.log"]
    assert ds == cs
    assert df == (cf if cut in ("body", "trailer") else ["server.log.5.gz", "server.log"])
    assert [r for r in dr if "truncated or corrupt" not in r] == cr
    assert len(_damage_reasons(dr)) == 1 and "server.log.5.gz" in _damage_reasons(dr)[0]
    assert ("not read: dropped as an orphan" in _damage_reasons(dr)[0]) is (cut in ("body", "trailer"))


def _boundary_gen(path, since, damaged):
    """since-5일부터 since+2일 직전까지 1시간마다 줄을 쓴 정상 세대(since 를 덮는다). damaged 면 since-4일 줄까지만 풀리게 무압축 gzip 을
    잘라 꼬리 6일을 잃는다(읽힌 마지막 줄 < since < 다음 세대 첫 줄 공백이 허용을 넘어 고아 컷이 난다). 손상이 아니면 같은 읽히는 줄만 담은
    온전한 gz 를 쓴다 - 같은 내용·같은 컷에서 손상 여부만 다른 대조군이다."""
    import zlib
    lines = [idle_trim(since - 5 * 86400 + 3600 * k) for k in range(7 * 24 - 2)]
    if not damaged:
        _gz_log(path, *lines[:25])
        return
    body = ("\n".join(lines) + "\n").encode()
    raw = gzip.compress(body, compresslevel=0, mtime=0)
    stop = body.index(lines[24].encode()) + len(lines[24]) + 1                          # since-4일 줄까지만 풀린다
    path.write_bytes(raw[:next(n for n in range(len(raw)) if zlib.decompressobj(31).decompress(raw[:n]) == body[:stop])])


@pytest.mark.parametrize("surface", ["api", "cli"])
@pytest.mark.parametrize("outcome", ["INVALID", "retry-exhausted", "cold-p95"])
def test_issue142_truncation_that_itself_makes_the_orphan_gap_still_names_the_damaged_file(tmp_path, outcome, surface):
    """final-review C1: since 를 걸쳐 덮던 .1.gz 의 꼬리가 사흘 넘게 잘리면 그 절단이 파일 경계 공백을 만들어 고아로 잘린다. 컷 경계 손상
    세대의 이름이 사유에 남고('not read: dropped as an orphan'), 판정 status 는 같은 읽히는 줄을 담은 온전한 세대(대조군)와 같다."""
    base, t, since, until = _candidate_week(3, cold_el=5.25 if outcome == "cold-p95" else 2.0)
    retry = _retry_log(since, until, *([since + 4 * 86400] if outcome == "retry-exhausted" else []))
    got = {}
    for kind in ("control", "damaged"):
        d = tmp_path / kind
        d.mkdir()
        _boundary_gen(d / "server.log.1.gz", since, kind == "damaged")
        (d / "server.log").write_text(t)
        got[kind] = _run_judge(d, surface, base, since, until, retry)
    (cs, cr, cf), (ds, dr, df) = got["control"], got["damaged"]
    assert cs == ("INVALID" if outcome == "INVALID" else "ROLLBACK") and ds == cs
    assert cf == df == ["server.log"]                                                    # 둘 다 고아로 잘렸다
    assert _damage_reasons(cr) == [] and any("log-coverage: sidecar log starts at" in r for r in cr)   # ROLLBACK 이면 INVALID: 접두
    assert [r for r in dr if "truncated or corrupt" not in r] == cr
    assert len(_damage_reasons(dr)) == 1
    assert "server.log.1.gz" in _damage_reasons(dr)[0] and "not read: dropped as an orphan" in _damage_reasons(dr)[0]


def test_issue142_half_decoded_last_line_is_dropped_not_parsed(tmp_path):
    """본문 중간에서 잘리면 마지막 줄이 반쯤 풀린다(예: elapsed_ms=4500.0 이 elapsed_ms=4 로). 그 줄이 4ms 요청으로 집계되면 안 된다.
    무압축(level 0) gzip 으로 잘림 위치를 풀린 바이트 위치에 맞춘다."""
    import zlib
    first, second = proc(T0 + 1, 300, 111, 2.0), proc(T0 + 9, 300, 222, 4.5)
    body = (first + "\n" + second + "\n").encode()
    raw = gzip.compress(body, compresslevel=0, mtime=0)
    stop = body.index(b"elapsed_ms=4") + len(b"elapsed_ms=4")
    cut = next(n for n in range(len(raw)) if zlib.decompressobj(31).decompress(raw[:n]) == body[:stop])
    assert [x["chars"] for x in K.parse_requests(body[:stop].decode())] == [111, 222]    # 대조: 잘린 줄을 두면 222 가 들어간다
    (tmp_path / "server.log.1.gz").write_bytes(raw[:cut])
    t, files, damaged = K.read_sidecar_logs(str(tmp_path))
    assert t == first + "\n" and [x["chars"] for x in K.parse_requests(t)] == [111]
    assert _names(files) == ["server.log.1.gz"] and len(damaged) == 1 and "server.log.1.gz" in damaged[0]


def test_issue142_intact_gz_reads_exactly_like_gzip_text_mode(tmp_path):
    """정상 .gz 는 수정 전 읽기(gzip.open(..., "rt", errors="replace"))와 같은 본문이다 - \\r(진행 막대)·\\r\\n 줄바꿈과 깨진 UTF-8 포함."""
    raw = (f"{idle_trim(T0)}\r\nBatches: 1/4\r{proc(T0 + 5, 300, 7, 2.0)}\n".encode() + b"bad \xff byte\n")
    with gzip.open(tmp_path / "server.log.1.gz", "wb") as f:
        f.write(raw)
    t, files, damaged = K.read_sidecar_logs(str(tmp_path))
    with gzip.open(tmp_path / "server.log.1.gz", "rt", errors="replace") as f:
        assert t == f.read()
    assert damaged == []


@pytest.mark.parametrize("surface", ["api", "cli"])
@pytest.mark.parametrize("cut", _CUTS)
def test_issue142_baseline_rejects_a_damaged_generation_inside_its_window(tmp_path, cut, surface):
    """baseline 도 같은 읽기·같은 사유다: 손상 없는 대조군은 기준선을 만들고, 손상이면 거부(api: ValueError, cli: INVALID 4)."""
    until = T0 + 86400
    lines = text(idle_trim(T0 - 60), recall_128x3(T0 + 1000)[0], idle_trim(until - 60))
    for kind in ("control", "damaged"):
        d = tmp_path / kind
        d.mkdir()
        _older_gen(d / "server.log.1.gz", T0)
        (d / "server.log").write_text(lines)
        if kind == "damaged":
            _truncate_gz(d / "server.log.1.gz", cut)
        if surface == "api":
            t, files, damaged = K.read_sidecar_logs(str(d), T0)
            if kind == "control":
                assert K.baseline(t, T0, until, [], None, damaged)["mode"] == "128x3"
            else:
                with pytest.raises(ValueError, match=r"server\.log\.1\.gz is truncated or corrupt"):
                    K.baseline(t, T0, until, [], None, damaged)
        else:
            (d.parent / f"{kind}-ex.txt").write_text("")
            rc = K.main(["baseline", "--until", kst(until)[:19], "--days", "1", "--exclude", str(d.parent / f"{kind}-ex.txt"),
                         "--log-dir", str(d), "--out", str(d.parent / f"{kind}-b.json")])
            assert rc == (0 if kind == "control" else K.EXIT["INVALID"])


def test_issue142_rules_are_unchanged_literal_snapshot():
    """RULES 키·값은 수정 전(origin/main f8541fe)과 같다 - judge 가 기준선 JSON 의 rules 와 비교하므로 바뀌면 운영 기준선이 무효가 된다."""
    assert K.RULES == {"GROUP_GAP_S": 1.0, "COLD_IDLE_S": 300.0, "DEEP_COLD_IDLE_S": 14400.0, "BASELINE_DAYS": 14,
                       "SATURATED_CANDIDATES": 300, "DEADLINE_S": 7.5, "MIN_COLD_RECALLS": 20, "MIN_CANARY_DAYS": 7,
                       "COLD_P95_SLOWDOWN": 0.1, "MIN_EXCEED_COUNT": 2, "REPEAT_WINDOW_S": 3600.0,
                       "RETRY_COVERAGE_TOLERANCE_S": 1.0}
    assert K.LOG_TAIL_TOLERANCE_S == 259200.0
