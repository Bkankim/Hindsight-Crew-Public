"""issue #20 cold 재생 도구 - 계획·분석(순수 함수)은 합성 타이밍으로, 실행(IO 경계)은 가짜 사이드카·가드로 고정한다."""
import pytest

from bench import cold_replay as C

A, B = "128x3", "300x1"


# ---------------------------------------------------------------- 계획

def test_plan_alternates_modes_in_abba_pairs_sharing_one_input():
    p = C.plan(n_per_mode=4, n_inputs=10, seed=1)
    assert [x["mode"] for x in p] == [A, B, B, A, A, B, B, A]
    for k in range(4):
        a, b = [x for x in p if x["pair"] == k]
        assert a["input"] == b["input"]           # 쌍은 같은 후보 텍스트로 두 방식을 잰다
    assert len({x["input"] for x in p}) == 4      # 입력이 넉넉하면 쌍마다 다른 입력


def test_plan_defaults_to_40_per_mode_and_cycles_inputs_when_short():
    p = C.plan(n_inputs=3)
    assert sum(x["mode"] == A for x in p) == 40 and sum(x["mode"] == B for x in p) == 40
    assert {x["input"] for x in p} == {0, 1, 2}


def test_idle_wait_guarantees_five_minutes_since_last_rerank_activity():
    # 합성 타이밍: 프로브 길이가 제각각이어도 다음 프로브 시작은 직전 활동 끝에서 idle_s 이상 떨어진다
    t, last, starts = 1000.0, 1000.0, []
    for dur in (4.0, 12.5, 0.3, 30.0):
        t += C.idle_wait(t, last, C.IDLE_S)
        starts.append(t)
        assert t - last >= C.IDLE_S
        t += dur
        last = t
    assert C.idle_wait(last + 400, last, C.IDLE_S) == 0.0      # 이미 충분히 쉬었으면 기다리지 않는다
    assert C.idle_wait(last + 100, last, C.IDLE_S) == pytest.approx(200.0)


def test_modes_split_300_candidates_like_upstream_batching():
    texts = [f"t{i}" for i in range(300)]
    assert [len(c) for c in C.split(texts, A)] == [128, 128, 44]
    assert [len(c) for c in C.split(texts, B)] == [300]
    assert sum(C.split(texts, A), []) == texts                  # 순서 그대로(upstream 은 RRF 순서로 자른다)


def test_default_plan_is_about_seven_hours():
    hours = C.estimate_s(C.plan(n_inputs=5), idle_s=C.IDLE_S, probe_s=10.0) / 3600
    assert 6.5 < hours < 7.5


# ---------------------------------------------------------------- 분석: upstream 재시도 모델

def test_request_within_deadline_is_its_own_latency():
    assert C.retry_model([3000.0], [900.0]) == {"ms": 3000.0, "attempts": [1], "failed": False}


def test_deadline_miss_retries_behind_the_still_running_cold_job():
    # 9s cold: 7.5s 에 503 -> 0.5s 백오프 -> 8.0s 에 재시도가 도착해 9.0s 까지 줄 서고 warm 2s 계산 -> 11.0s
    assert C.retry_model([9000.0], [2000.0]) == {"ms": 11000.0, "attempts": [2], "failed": False}


def test_retries_are_skipped_while_queued_past_deadline_then_recall_fails():
    # 40s cold: 재시도 3번이 모두 대기만으로 7.5s 를 넘겨 skip -> 7.5+0.5+7.5+1+7.5+2+7.5 = 33.5s 에 recall 오류
    assert C.retry_model([40000.0], [2000.0]) == {"ms": 33500.0, "attempts": [4], "failed": True}


def test_sequential_requests_add_up_with_concurrency_one():
    assert C.retry_model([3000.0, 2000.0, 700.0], [900.0, 900.0, 300.0])["ms"] == 5700.0
    r = C.retry_model([9000.0, 2000.0, 700.0], [2000.0, 900.0, 300.0])
    assert r == {"ms": 11000.0 + 2000.0 + 700.0, "attempts": [2, 1, 1], "failed": False}


# ---------------------------------------------------------------- 분석: 제외·분포·지표

def rec(pair, mode, cold, warm=None, idle=310.0, foreign_before=0, foreign_during=0, guard=False, status=None):
    return {"probe": 2 * pair + (mode == B), "pair": pair, "mode": mode, "input": pair, "idle_before_s": idle,
            "cold_ms": cold, "warm_ms": warm or [c / 3 for c in cold], "status": status or [200] * len(cold),
            "foreign_before": foreign_before, "foreign_during": foreign_during, "guard_overlap": guard}


def pairs_of(base_recall_ms, factor):
    out = []
    for k, ms in enumerate(base_recall_ms):
        out += [rec(k, A, [ms * 0.4, ms * 0.4, ms * 0.2]), rec(k, B, [ms * factor])]
    return out


def test_probe_exclusions_name_why_the_sample_is_not_a_clean_cold_request():
    assert C.exclusions(rec(0, A, [1000.0]), idle_s=300) == []
    assert C.exclusions(rec(0, A, [1000.0], foreign_before=2), idle_s=300) == ["idle-broken"]
    assert C.exclusions(rec(0, A, [1000.0], idle=250.0), idle_s=300) == ["idle-broken"]
    assert C.exclusions(rec(0, A, [1000.0], foreign_during=1, guard=True), idle_s=300) == ["interference"]
    assert C.exclusions(rec(0, A, [1000.0], status=[503]), idle_s=300) == ["error"]


def test_analysis_reports_per_mode_distribution_and_deadline_miss_rate():
    rs = pairs_of([3000.0, 4000.0, 5000.0, 6000.0], 0.9)
    rs[2].update(cold_ms=[8000.0, 2000.0, 700.0], warm_ms=[900.0, 900.0, 300.0])   # 쌍 1 의 128x3: 첫 요청이 7.5s 초과
    rep = C.analyze(rs)
    a, b = rep["modes"][A], rep["modes"][B]
    assert a["n"] == 4 and b["n"] == 4
    assert a["deadline_exceeded"] == {"requests": 1, "probes": 1, "rate": 0.25}
    assert b["deadline_exceeded"]["probes"] == 0
    assert b["cold_request_ms"]["p50"] == pytest.approx(0.9 * 4500.0)
    # 7.5s 에 503 -> 8.0s 재시도가 8.0s 에 끝나는 cold 계산 뒤에서 warm 0.9s -> 이어서 2.0s, 0.7s
    assert a["recall_ms"]["max"] == pytest.approx(8000.0 + 900.0 + 2000.0 + 700.0)
    assert a["retried_probes"] == 1 and a["failed_recalls"] == 0


def test_excluded_probe_is_listed_and_its_pair_leaves_the_ratio_metrics():
    rs = pairs_of([3000.0 + 100 * k for k in range(20)], 0.9)
    rs[5]["foreign_before"] = 1                                  # 쌍 2 의 300x1 이 idle 을 깨졌다
    rep = C.analyze(rs)
    assert rep["excluded"] == [{"probe": 5, "pair": 2, "mode": B, "reasons": ["idle-broken"]}]
    assert rep["modes"][B]["n"] == 19 and rep["modes"][A]["n"] == 20
    assert rep["metrics"]["cold_p50"]["n"] == 19


def test_cold_metrics_are_ratio_and_ci_for_judge_noninferiority():
    from bench import judge
    better = C.analyze(pairs_of([3000.0 + 100 * k for k in range(20)], 0.9))["metrics"]
    assert set(better) == {"cold_p50", "cold_p95"}
    m = better["cold_p50"]
    assert m["ratio"] == pytest.approx(0.9) and m["ci95"][0] <= 0.9 <= m["ci95"][1]
    assert judge.noninferiority(m["ratio"], *m["ci95"], noise=judge.SPEED_MARGIN) == m["verdict"] == "ok"
    worse = C.analyze(pairs_of([3000.0 + 100 * k for k in range(20)], 1.3))["metrics"]["cold_p95"]
    assert worse["verdict"] == "regressed"


def test_too_few_clean_pairs_hold_instead_of_passing_on_a_zero_width_ci():
    # 2026-09-26 시험 실행: 가드 창에 걸린 프로브를 빼자 쌍이 1개 남았고 CI 폭 0 으로 ok 가 나왔다
    m = C.analyze(pairs_of([3000.0, 4000.0], 0.9))["metrics"]["cold_p50"]
    assert m["n"] == 2 and m["verdict"] == "hold" and m["short"] == 18
    enough = C.analyze(pairs_of([3000.0 + 100 * k for k in range(20)], 0.9))["metrics"]["cold_p50"]
    assert enough["verdict"] == "ok" and enough["short"] == 0


def test_short_idle_trial_is_marked_as_not_meeting_the_cold_definition():
    rs = [dict(r, idle_before_s=20.0) for r in pairs_of([3000.0, 4000.0], 0.9)]
    rep = C.analyze(rs, idle_s=15.0)
    assert rep["cold_definition_met"] is False and rep["excluded"] == []
    assert C.analyze(pairs_of([3000.0, 4000.0], 0.9))["cold_definition_met"] is True


# ---------------------------------------------------------------- 실행 (IO 경계: 가짜 사이드카·시계·가드)

GOOD_OVERRIDES = {"RERANK_MAX_TEXTS": "600", "RERANK_TOTAL_DEADLINE_SECONDS": "120", "RERANK_QUEUE_TIMEOUT_SECONDS": "120"}


def state(port=18781, **ov):
    return {"name": "cold", "port": port, "overrides": dict(GOOD_OVERRIDES, **ov)}


def test_production_sidecar_is_refused_even_if_a_state_claims_its_port():
    with pytest.raises(SystemExit, match="8787"):
        C.bench_sidecar(8787, [state(port=8787)])


def test_bench_sidecar_must_be_a_sidecar_up_state_with_untruncated_deadline_and_300_texts():
    assert C.bench_sidecar(18781, [state()])["name"] == "cold"
    with pytest.raises(SystemExit, match="sidecar-up"):
        C.bench_sidecar(18782, [state()])
    with pytest.raises(SystemExit, match="RERANK_TOTAL_DEADLINE_SECONDS"):
        C.bench_sidecar(18781, [state(RERANK_TOTAL_DEADLINE_SECONDS="7.5")])
    with pytest.raises(SystemExit, match="RERANK_MAX_TEXTS"):
        C.bench_sidecar(18781, [state(RERANK_MAX_TEXTS="256")])


class World:
    """가짜 시계 + 가짜 벤치 사이드카(요청 수를 로그 줄처럼 센다). foreign_at = 이 시각을 지나는 대기 중에 남의 요청 1건."""

    def __init__(self, ms=1000.0, foreign_at=()):
        self.t, self.ms, self.calls, self.log, self.foreign_at = 1000.0, ms, [], 0, set(foreign_at)

    def now(self):
        return self.t

    def sleep(self, s):
        self.log += sum(1 for x in self.foreign_at if self.t < x <= self.t + s)
        self.t += s

    def post(self, port, query, texts):
        self.calls.append((self.t, port, len(texts)))
        self.t += self.ms / 1000
        self.log += 1
        return 200, self.ms

    def activity(self):
        return self.log


class FakeGuard:
    def __init__(self, abort_on=None, windows=()):
        self.checks, self.abort_on, self.windows = 0, abort_on, list(windows)

    def check(self):
        self.checks += 1
        if self.abort_on and self.checks >= self.abort_on:
            from bench.guard import GuardAbort
            raise GuardAbort("swap-growth")

    def meta(self):
        return {"windows": [list(w) for w in self.windows], "aborted": None}


INPUTS = [{"key": "q0", "query": "q", "texts": [f"t{i}" for i in range(300)]}]


def run(world, guard, probes=None, idle_s=300.0, **kw):
    return C.execute(probes or C.plan(n_per_mode=2, n_inputs=1), INPUTS, state(), idle_s=idle_s, guard=guard,
                     post=world.post, activity=world.activity, now=world.now, sleep=world.sleep,
                     prod_mark=lambda: {"offset": 0}, prod_errors=lambda m: {"http_503": 1, "deadline": 2}, **kw)


def test_execute_waits_idle_before_every_probe_and_checks_the_guard():
    w, g = World(), FakeGuard()
    records, meta = run(w, g)
    assert [r["mode"] for r in records] == [A, B, B, A]
    assert g.checks >= 4
    firsts = [r["t_start"] for r in records]
    ends = [1000.0] + [r["t_end"] for r in records[:-1]]
    assert all(f - e >= 300.0 for f, e in zip(firsts, ends))       # 도구 시작 시점부터 매 프로브 전 5분
    assert [len(r["cold_ms"]) for r in records] == [3, 1, 1, 3]    # 128x3 은 3 요청, 300x1 은 1 요청
    assert all(len(r["warm_ms"]) == len(r["cold_ms"]) for r in records)
    assert {p for _, p, _ in w.calls} == {18781}
    assert meta["prod_sidecar"] == {"http_503": 1, "deadline": 2} and meta["guard"]["aborted"] is None
    assert C.analyze(records)["excluded"] == []


def test_foreign_request_during_the_idle_wait_restarts_the_wait():
    # 첫 프로브(1300~1306) 뒤 둘째 프로브의 idle 대기 중 1400 에 남의 요청 -> 그 뒤로 다시 5분을 채우고 잰다(쌍을 잃지 않는다)
    w = World(foreign_at={1400.0})
    records, _ = run(w, FakeGuard())
    assert records[1]["t_start"] - 1400.0 >= 300.0
    assert [r["idle_restarts"] for r in records] == [0, 1, 0, 0]
    assert [r["foreign_before"] for r in records] == [0, 0, 0, 0] and C.analyze(records)["excluded"] == []


def test_guard_pause_overlapping_the_idle_window_marks_interference():
    w = World()
    records, _ = run(w, FakeGuard(windows=[(1100.0, 1150.0)]))
    assert records[0]["guard_overlap"] is True and not any(r["guard_overlap"] for r in records[1:])


def test_guard_abort_stops_the_replay_and_keeps_meta():
    w = World()
    records, meta = run(w, FakeGuard(abort_on=3))
    assert meta["aborted"] == "swap-growth" and len(records) < 4
    assert meta["prod_sidecar"] == {"http_503": 1, "deadline": 2}


# ---------------------------------------------------------------- 입력: trace -> 리랭커 입력 재구성

def test_reranker_inputs_rebuild_upstream_doc_text_in_rrf_order():
    # 날짜는 upstream 처럼 occurred_start 로만 붙인다. trace 의 event_date 는 다른 칼럼이라 occurred_start 가 없는 기억에도
    # 채워져 있다(2026-09-26 시험 실행: 이것으로 붙이면 61/300 이 운영 점수와 어긋났고, 빼면 61/61 bit 일치)
    trace = {"query": {"query_text": "배포 절차"},
             "retrieval_results": [{"results": [
                 {"node_id": "a", "context": "deploy", "event_date": "2026-09-05T03:00:00+00:00"},
                 {"node_id": "b", "context": "", "event_date": "2026-09-01T00:00:00+00:00"}]},
                 {"results": [{"node_id": "c", "context": "ops", "event_date": None}]}],
             "reranked": [{"node_id": "b", "text": "B", "rrf_rank": 2}, {"node_id": "a", "text": "A", "rrf_rank": 1},
                          {"node_id": "c", "text": "C", "rrf_rank": 3}]}
    occurred = {"a": "2026-09-05T03:00:00+00:00", "b": None, "c": "2026-08-30T23:00:00+00:00"}
    assert C.reranker_inputs(trace, occurred) == {"query": "배포 절차", "texts": [
        "[Date: September 05, 2026 (2026-09-05)] deploy: A", "B", "[Date: August 30, 2026 (2026-08-30)] ops: C"]}


def test_reranker_inputs_refuse_a_candidate_with_unknown_occurred_start():
    trace = {"query": {"query_text": "q"}, "retrieval_results": [{"results": [{"node_id": "a", "context": ""}]}],
             "reranked": [{"node_id": "a", "text": "A", "rrf_rank": 1}]}
    with pytest.raises(KeyError):
        C.reranker_inputs(trace, {})


def test_own_request_logged_late_is_not_counted_as_foreign_in_the_next_idle_window():
    # 사이드카 access log 가 프로브 끝 직후 아직 마지막 줄을 안 썼어도, 그 줄을 다음 프로브의 남의 요청으로 세지 않는다
    w = World()
    lag = {"n": 0}

    def post(port, query, texts):
        lag["n"] += 1                         # 이 요청의 로그 줄은 다음 sleep 때 찍힌다
        w.calls.append((w.t, port, len(texts)))
        w.t += w.ms / 1000
        return 200, w.ms

    def sleep(s):
        w.log += lag["n"]
        lag["n"] = 0
        w.t += s

    records, _ = C.execute(C.plan(n_per_mode=2, n_inputs=1), INPUTS, state(), idle_s=300.0, guard=FakeGuard(),
                           post=post, activity=w.activity, now=w.now, sleep=sleep,
                           prod_mark=lambda: None, prod_errors=lambda m: None)
    assert [r["foreign_before"] for r in records] == [0, 0, 0, 0]


# ---------------------------------------------------------------- 운영 사이드카 rerank 와 겹친 프로브 (같은 MPS 장치)

def test_prod_rerank_intervals_come_from_processed_lines_end_minus_elapsed():
    import datetime as dt
    text = ("2026-09-26 08:00:16,461 INFO rerank_processed candidates=44 chars=36433 batches=2 elapsed_ms=894.9 device=mps\n"
            'INFO:     127.0.0.1:51197 - "POST /rerank HTTP/1.1" 200 OK\n'
            "2026-09-26 08:00:20,000 WARNING rerank_queue_full queue_depth=4\n")
    end = dt.datetime(2026, 9, 26, 8, 0, 16, 461000).timestamp()            # 로그는 호스트 현지 시각
    assert C.prod_rerank_intervals(text) == [(pytest.approx(end - 0.8949), pytest.approx(end))]


def test_prod_rerank_in_idle_window_or_during_probe_marks_the_probe():
    assert C.exclusions(rec(0, A, [1000.0]) | {"prod_before": 1}, idle_s=300) == ["idle-broken"]
    assert C.exclusions(rec(0, A, [1000.0]) | {"prod_during": 2}, idle_s=300) == ["interference"]
    w = World()
    busy = [(1450.0, 1452.0), (1606.2, 1607.0)]     # 둘째 프로브의 idle 창(1306~1606) 안, 그 프로브 측정(1606~1608) 안
    records, _ = run(w, FakeGuard(), prod_busy=lambda mark: busy)
    assert [(r["prod_before"], r["prod_during"]) for r in records] == [(0, 0), (1, 1), (0, 0), (0, 0)]


# ---------------------------------------------------------------- final-review 2026-09-26 (#20) 수정 고정

def test_unexpected_error_mid_run_still_returns_meta_marked_aborted():
    # 7시간 run 도중 Ctrl-C·예외로 끊겨도 가드·운영 집계를 담은 meta 가 남아야 analyze 할 수 있다
    w = World()
    n = {"calls": 0}

    def post(port, query, texts):
        n["calls"] += 1
        if n["calls"] > 6:                                   # 첫 프로브(128x3 = cold 3 + warm 3) 뒤에 터진다
            raise KeyboardInterrupt
        return w.post(port, query, texts)

    records, meta = C.execute(C.plan(n_per_mode=2, n_inputs=1), INPUTS, state(), idle_s=300.0, guard=FakeGuard(),
                              post=post, activity=w.activity, now=w.now, sleep=w.sleep,
                              prod_mark=lambda: {"offset": 0}, prod_errors=lambda m: {"http_503": 0})
    assert len(records) == 1 and meta["aborted"] == "error:KeyboardInterrupt"
    assert meta["prod_sidecar"] == {"http_503": 0} and "guard" in meta


def test_guard_window_recorded_after_the_probe_still_excludes_it():
    # 가드 창은 마지막 정상 표본 시각으로 거슬러 기록된다 - 기록 뒤에 생긴 창이 이미 쓴 프로브를 덮으면 뺀다
    w = World()
    records, _ = run(w, FakeGuard())
    assert not any(r["guard_overlap"] for r in records)
    rep = C.analyze(records, idle_s=300.0, guard_windows=[(records[1]["t_start"] + 0.2, records[1]["t_start"] + 0.8)])
    assert [x["probe"] for x in rep["excluded"]] == [1] and rep["excluded"][0]["reasons"] == ["interference"]


def test_unreadable_production_log_excludes_probes_instead_of_passing_them():
    w = World()
    records, _ = run(w, FakeGuard(), prod_busy=lambda mark: None)
    assert all(r["prod_before"] is None and r["prod_during"] is None for r in records)
    assert all(x["reasons"] == ["no-prod-signal"] for x in C.analyze(records)["excluded"])
    assert len(C.analyze(records)["excluded"]) == 4


def test_production_rerank_logged_after_probe_end_is_recounted_from_run_end_intervals():
    # 운영 rerank 줄은 계산이 끝나야 찍힌다 - 프로브 끝에 돌던 것은 run 끝의 전체 구간으로 다시 센다
    w = World()
    records, meta = run(w, FakeGuard(), prod_busy=lambda mark: [])
    assert meta["prod_intervals"] == []
    late = [(records[1]["t_end"] - 0.5, records[1]["t_end"] + 2.0)]       # 둘째 프로브 끝에 걸친 운영 rerank
    rep = C.analyze(records, idle_s=300.0, prod_intervals=late)
    # 둘째 프로브는 측정 중 겹침, 끝난 뒤 이어진 부분은 셋째 프로브의 idle 창을 깬다
    assert [(x["probe"], x["reasons"]) for x in rep["excluded"]] == [(1, ["interference"]), (2, ["idle-broken"])]
    assert all(x["reasons"] == ["no-prod-signal"] for x in C.analyze(records, idle_s=300.0, prod_intervals=None)["excluded"])


def test_failed_recalls_are_judged_separately_from_the_quantile_ratio():
    rs = pairs_of([3000.0 + 100 * k for k in range(40)], 0.9)
    rs[1].update(cold_ms=[40000.0], warm_ms=[2000.0])            # 쌍 0 의 300x1: 재시도 소진(33.5s 에 실패)
    rep = C.analyze(rs)
    assert rep["modes"][B]["failed_recalls"] == 1
    assert rep["metrics"]["cold_p95"]["verdict"] != "regressed"   # 분위수 비는 실패를 악화로 말하지 않는다(넓은 CI -> hold)
    assert rep["failures"] == {"baseline": 0, "candidate": 1, "verdict": "regressed"}


def test_sidecar_error_probes_count_against_their_mode():
    rs = pairs_of([3000.0 + 100 * k for k in range(40)], 0.9)
    for r in rs:
        if r["mode"] == B and r["pair"] < 25:
            r["status"] = [503]                                   # 300x1 이 계통적으로 503
    rep = C.analyze(rs)
    assert rep["modes"][B]["errors"] == 25 and rep["modes"][A]["errors"] == 0
    assert rep["metrics"]["cold_p50"]["verdict"] == "hold"        # 쌍이 모자라 지표만 보면 보류로 숨는다
    assert rep["failures"]["verdict"] == "regressed"


def test_bench_sidecar_with_latency_changing_override_is_refused():
    with pytest.raises(SystemExit, match="IDLE_TRIM_SECONDS"):
        C.bench_sidecar(18781, [state(IDLE_TRIM_SECONDS="60")])
    with pytest.raises(SystemExit, match="RERANK_BATCH_SIZE"):
        C.bench_sidecar(18781, [state(RERANK_BATCH_SIZE="16")])


def test_extract_recall_args_restore_mcp_defaults_and_keep_row_timestamp():
    # 캠페인은 MCP(budget high, max_tokens 4096)로 부르고 코퍼스는 그 기본값을 지워 둔다 - REST(budget mid)로 새지 않게
    clock = "2026-09-22T20:00:00Z"
    assert C.recall_args({"query": "q"}, clock) == {
        "query": "q", "budget": "high", "max_tokens": 4096, "trace": True, "query_timestamp": clock}
    got = C.recall_args({"query": "q", "budget": "low", "query_timestamp": "2026-09-14T00:00:00+00:00"}, clock)
    assert got["budget"] == "low" and got["query_timestamp"] == "2026-09-14T00:00:00+00:00"   # runner.call 과 같다


# ---------------------------------------------------------------- final-review 수정 리뷰(04·06) 후속

def test_run_end_production_log_read_error_still_returns_meta():
    w = World()
    calls = {"n": 0}

    def busy(mark):
        calls["n"] += 1
        if calls["n"] > 4:                                     # 프로브마다 1번씩 4번 읽고, run 끝 읽기에서 터진다
            raise ValueError("corrupt .1.gz")
        return []

    records, meta = run(w, FakeGuard(), prod_busy=busy)
    assert len(records) == 4 and meta["prod_intervals"] is None   # 못 읽음 = 신호 없음(닫힌 쪽)


def test_run_end_waits_for_production_reranks_still_running_at_the_last_probe():
    # 마지막 프로브 끝(t_end)에 돌던 운영 rerank 는 끝나야 로그에 찍힌다 - run 끝 읽기 전에 기다린다
    w = World()
    end = {}

    def busy(mark):
        t = end.get("t")
        return [(t - 1.0, t + 5.0)] if t is not None and w.now() >= t + 5.0 else []

    def post(port, query, texts):
        out = w.post(port, query, texts)
        end["t"] = w.now()
        return out

    records, meta = C.execute(C.plan(n_per_mode=2, n_inputs=1), INPUTS, state(), idle_s=300.0, guard=FakeGuard(),
                              post=post, activity=w.activity, now=w.now, sleep=w.sleep,
                              prod_mark=lambda: {"offset": 0}, prod_errors=lambda m: {}, prod_busy=busy)
    assert meta["prod_intervals"]                              # run 끝 읽기가 늦게 끝난 운영 rerank 를 본다
    rep = C.analyze(records, idle_s=300.0, prod_intervals=meta["prod_intervals"])
    assert [(x["probe"], x["reasons"]) for x in rep["excluded"]] == [(3, ["interference"])]


def test_connection_failures_are_not_counted_as_the_modes_sidecar_errors():
    rs = pairs_of([3000.0 + 100 * k for k in range(40)], 0.9)
    for r in rs:
        if r["mode"] == B and r["pair"] >= 30:
            r["status"] = [0]                                  # 벤치 사이드카가 죽어 연결 실패 - 300x1 의 성질이 아니다
    rep = C.analyze(rs)
    assert rep["modes"][B]["errors"] == 0 and rep["failures"]["verdict"] == "ok"
    assert {tuple(x["reasons"]) for x in rep["excluded"]} == {("no-response",)}


def test_sidecar_error_overlapping_interference_is_not_blamed_on_the_mode():
    rs = pairs_of([3000.0 + 100 * k for k in range(40)], 0.9)
    rs[1].update(status=[503], guard_overlap=True)            # 가드 정지 구간의 503 - 간섭 탓일 수 있다
    rep = C.analyze(rs)
    assert rep["modes"][B]["errors"] == 0 and rep["failures"]["verdict"] == "ok"


# ---------------------------------------------------------------- 운영 사이드카 keep-warm (issue #79)
# 실제 송신은 어디에도 없다: KeepWarm 은 송신기를 주입받고, 아래 autouse 픽스처가 소켓 연결을 막는다.

@pytest.fixture(autouse=True)
def _no_real_network_send(monkeypatch):
    import socket
    attempts = []

    def blocked(self, address, *a, **k):
        attempts.append(address)
        raise ConnectionRefusedError("blocked in tests: no real network send")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    return attempts


def kw_run(world, guard=None, send_script=(), n_per_mode=1, idle_s=60.0, prod_log=None, with_kw=True):
    """keep-warm 을 켠 재생. 송신기는 순간(시계 안 감)이라 송신 시각 = 끝 시각. prod_log = 운영 rerank 로그에 우리 요청 말고
    따로 있는 남의 구간 목록(None 이면 운영 로그를 안 읽는 run). 우리 요청은 보낸 만큼만 로그에 찍힌다.
    반환: (records, meta, 송신 시각들, kw)."""
    from bench.keepwarm import KeepWarm
    sent, script = [], list(send_script)

    def send():
        sent.append(world.now())
        out = script.pop(0) if script else 200
        if isinstance(out, BaseException):
            raise out
        return out

    kw = KeepWarm(send, now=world.now, interval_s=30.0) if with_kw else None

    def prod_busy(mark):                                        # 지금까지 보낸 keep-warm 이 운영 로그에 남긴 줄 + 남의 줄
        return [(t - 0.02, t + 0.02, 1) for t in sent] + list(prod_log or [])        # 우리 요청은 후보 1 로 찍힌다

    records, meta = C.execute(C.plan(n_per_mode=n_per_mode, n_inputs=1), INPUTS, state(), idle_s=idle_s,
                              guard=guard or FakeGuard(), post=world.post, activity=world.activity, now=world.now,
                              sleep=world.sleep, prod_mark=lambda: {"offset": 0}, prod_errors=lambda m: {},
                              prod_busy=prod_busy if prod_log is not None else None, keepwarm=kw)
    return records, meta, sent, kw


def test_keepwarm_ticks_during_idle_waits_not_during_probes_and_stops_at_the_end():
    # 손계산(World: 요청 1건 1s, 대기 걸음 10s, 간격 30s, idle 60s, 쌍 1개 = 프로브 둘):
    #  tick 은 걸음의 sleep 과 가드 확인 뒤에 한다. 프로브0 idle 창 1000~1060: 1010 송신, 1040 송신(1020·1030 은 아직), 1050·1060 은 아직.
    #  프로브0(128x3, 요청 6개) 1060~1066. 프로브1 idle 창 1066~1126: 1076 에서 36s 경과라 송신, 1086·1096 은 아직, 1106 송신,
    #  1116·1126 은 아직. 프로브1(300x1, 요청 2개) 1126~1128.
    w = World()
    records, meta, sent, kw = kw_run(w)
    assert sent == [1010.0, 1040.0, 1076.0, 1106.0]
    assert [(r["t_start"], r["t_end"]) for r in records] == [(1060.0, 1066.0), (1126.0, 1128.0)]
    assert not any(r["t_start"] < t < r["t_end"] for r in records for t in sent)      # 측정 도중에는 보내지 않는다
    m = meta["keepwarm"]
    assert m["started"] == 1000.0 and m["stopped"] == 1128.0 and m["sent"] == 4 and m["failed"] == 0
    kw.tick()
    w.t += 1000
    kw.tick()
    assert len(sent) == 4                                       # 끝난 뒤에는 보내지 않는다


def test_keepwarm_window_is_a_canary_exclude_line_in_cold_replay_meta():
    from bench import canary
    w = World()
    _, meta, _, _ = kw_run(w)
    (s, e, why), = canary.parse_exclude(meta["keepwarm"]["exclude_line"])
    assert s <= 1000.0 and e >= 1128.0 and "keep-warm" in why


def test_own_keepwarm_requests_do_not_break_idle_or_mark_interference():
    # 운영 rerank 로그에는 우리 keep-warm 요청도 찍힌다(송신 시각 끝, 같은 MPS 장치). 그것까지 간섭으로 세면 모든 프로브가 빠진다.
    w = World()
    records, meta, _, _ = kw_run(w, prod_log=[])
    assert [(r["prod_before"], r["prod_during"]) for r in records] == [(0, 0), (0, 0)]
    assert C.analyze(records, idle_s=60.0, prod_intervals=meta["prod_intervals"])["excluded"] == []
    assert meta["prod_intervals"] == []


def test_a_foreign_production_rerank_is_still_counted_next_to_own_keepwarm():
    w = World()
    foreign = (1100.0, 1102.0)                                  # 프로브1 의 idle 창(1066~1126) 안
    records, meta, _, _ = kw_run(w, prod_log=[foreign])
    assert [(r["prod_before"], r["prod_during"]) for r in records] == [(0, 0), (1, 0)]
    assert meta["prod_intervals"] == [foreign]
    assert [(x["probe"], x["reasons"]) for x in C.analyze(records, idle_s=60.0, prod_intervals=meta["prod_intervals"])["excluded"]] \
        == [(1, ["idle-broken"])]


def test_keepwarm_failure_is_recorded_and_the_replay_continues():
    w = World()
    records, meta, sent, _ = kw_run(w, send_script=[503, ConnectionRefusedError("x")])
    assert len(records) == 2 and meta["aborted"] is None        # 송신 실패가 재생을 멈추지 않는다
    assert meta["keepwarm"]["failed"] == 2 and meta["keepwarm"]["sent"] == 4


def test_keepwarm_stops_when_the_guard_aborts_the_replay():
    w = World()
    records, meta, sent, kw = kw_run(w, guard=FakeGuard(abort_on=3))
    assert meta["aborted"] == "swap-growth"
    assert meta["keepwarm"]["stopped"] is not None and meta["keepwarm"]["exclude_line"]
    n = len(sent)
    w.t += 1000
    kw.tick()
    assert len(sent) == n


def test_replay_without_keepwarm_sends_nothing_and_records_it_as_off(_no_real_network_send):
    w = World()
    _, meta, sent, _ = kw_run(w, with_kw=False)
    assert sent == [] and _no_real_network_send == [] and meta["keepwarm"] is None


def test_cold_replay_cli_run_turns_on_the_production_keepwarm(monkeypatch, tmp_path):
    from bench import replica, runner
    from bench.keepwarm import PROD_RERANK_URL, KeepWarm
    inputs = tmp_path / "in.json"
    inputs.write_text('{"inputs": [{"key": "q", "query": "q", "texts": ["t"]}]}')
    got = {}
    monkeypatch.setattr(replica, "sidecar_states", lambda: [state()])
    monkeypatch.setattr(replica, "_sidecar_process_ok", lambda s: True)
    monkeypatch.setattr(replica, "WORK_DIR", str(tmp_path))
    monkeypatch.setattr(runner, "HOLDOUT_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(C, "execute", lambda *a, **k: got.update(k) or ([], {"probes_done": 0, "aborted": None,
                                                                              "prod_sidecar": {}}))
    assert C.main(["run", "--port", "18781", "--inputs", str(inputs), "--n-per-mode", "1", "--run-id", "kw-cli"]) == 0
    assert isinstance(got["keepwarm"], KeepWarm) and got["keepwarm"].url == PROD_RERANK_URL


class PausingGuard(FakeGuard):
    """pause_at 번째 check 에서 시계를 pause_s 만큼 보낸다(가드 정지 -> 재개 모사). world 시계를 공유한다."""
    def __init__(self, world, pause_at, pause_s):
        super().__init__()
        self.world, self.pause_at, self.pause_s = world, pause_at, pause_s

    def check(self):
        super().check()
        if self.checks == self.pause_at:
            self.world.t += self.pause_s


def test_no_keepwarm_request_is_sent_when_the_guard_aborts_at_the_first_check():
    # 시작 시점에 가드가 중단이면 벤치가 돌지 않았다 - 운영 사이드카에 아무것도 보내지 않는다(첫 tick 이 첫 가드 확인보다 앞서면 1건 나간다)
    w = World()
    records, meta, sent, _ = kw_run(w, guard=FakeGuard(abort_on=1))
    assert records == [] and meta["aborted"] == "swap-growth"
    assert sent == [] and meta["keepwarm"]["sent"] == 0


def test_keepwarm_warms_right_after_a_long_guard_pause_in_the_idle_wait():
    # 손계산(idle 60s, 걸음 10s): 1010 송신. 둘째 걸음(1020) 가드 확인이 600s 막혔다 재개 -> 시계 1620, 곧바로 tick(1010 뒤 610s 경과)으로 송신.
    # 그다음 w<=0 이라 프로브가 시작하므로, 재개 직후 송신이 프로브(cold 요청) 앞에 온다.
    w = World()
    records, meta, sent, _ = kw_run(w, guard=PausingGuard(w, pause_at=2, pause_s=600.0))
    assert sent[:2] == [1010.0, 1620.0]
    assert records[0]["t_start"] == 1620.0


def test_keepwarm_warms_after_a_long_guard_pause_before_the_probe_too():
    # 프로브 앞 가드 확인이 한 걸음(10s) 넘게 막혔다 재개하면, 그동안 idle 이던 운영 사이드카를 프로브 시작 전에 데운다
    w = World()
    # check 순서: 1(1010) 2(1020) 3(1030) 4(1040) 5(1050) 6(1060) 7 = 프로브0 앞 가드 확인(1060 직후 w<=0)
    records, meta, sent, _ = kw_run(w, guard=PausingGuard(w, pause_at=7, pause_s=600.0))
    assert sent[:3] == [1010.0, 1040.0, 1660.0]
    assert records[0]["t_start"] == 1660.0


def test_prod_rerank_requests_carry_the_candidate_count_when_the_log_line_has_it():
    import datetime as dt
    text = ("2026-09-26 08:00:16,461 INFO rerank_processed candidates=44 chars=36433 batches=2 elapsed_ms=894.9 device=mps\n"
            "2026-09-26 08:00:20,000 INFO rerank_processed elapsed_ms=500.0 device=mps\n")
    end = dt.datetime(2026, 9, 26, 8, 0, 16, 461000).timestamp()
    end2 = dt.datetime(2026, 9, 26, 8, 0, 20).timestamp()
    assert C.prod_rerank_requests(text) == [(pytest.approx(end - 0.8949), pytest.approx(end), 44),
                                            (pytest.approx(end2 - 0.5), pytest.approx(end2), None)]
    assert C.prod_rerank_intervals(text) == [(pytest.approx(end - 0.8949), pytest.approx(end)),
                                             (pytest.approx(end2 - 0.5), pytest.approx(end2))]      # 기존 모양 그대로


def test_a_concurrent_foreign_request_inside_an_own_send_window_is_still_counted():
    # 1076 에 우리 keep-warm 이 나가는 순간 같은 창에서 끝난 후보 128 의 남의 요청은 지우지 않는다(프로브1 idle 창 1066~1126 안)
    w = World()
    concurrent = (1076.0 - 0.02, 1076.0 + 0.02, 128)
    records, meta, _, _ = kw_run(w, prod_log=[concurrent])
    assert [(r["prod_before"], r["prod_during"]) for r in records] == [(0, 0), (1, 0)]
    assert meta["prod_intervals"] == [(concurrent[0], concurrent[1])]                 # 후보 수는 떼고 (시작, 끝)만 남는다
    assert [(x["probe"], x["reasons"]) for x in C.analyze(records, idle_s=60.0, prod_intervals=meta["prod_intervals"])["excluded"]] \
        == [(1, ["idle-broken"])]


def test_own_keepwarm_failures_are_listed_next_to_prod_sidecar_in_meta():
    w = World()
    _, meta, _, _ = kw_run(w, send_script=[(503, "rerank_queue_timeout"), 503])
    assert meta["prod_sidecar_keepwarm"] == {"http_503": 2, "deadline": 1, "queue_full": 0}
    _, off, _, _ = kw_run(World(), with_kw=False)
    assert off["prod_sidecar_keepwarm"] is None
