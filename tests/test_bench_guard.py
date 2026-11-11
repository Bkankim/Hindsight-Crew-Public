"""issue #19 벤치 간섭 가드 - 임계 판정(순수 함수)과 정지·재개·최대 대기를 합성 샘플로 고정한다."""
import pytest

from bench import guard as G

L = G.Limits()
MB = 1024 * 1024
PAGE = 16384


def s(t, swapouts=0, pressure=1, free=40.0, swap_mb=8000.0):
    return G.Sample(t=t, pressure=pressure, swap_used_mb=swap_mb, swapouts=swapouts, page_size=PAGE, free_pct=free)


def pages(mb):
    return int(mb * MB / PAGE)


# ---------------------------------------------------------------- 임계 판정

def test_idle_host_with_sticky_swap_and_warn_pressure_is_ok():
    # 2026-09-26 실측: 벤치 없이 스왑 7.9GB 사용 중, 압박 수준 2(warn), swapout 0 - 멈출 이유가 아니다
    base = s(0, pressure=2, swap_mb=7900)
    assert G.assess(base, base, s(10, pressure=2, swap_mb=7900), L) == []


def test_active_swapout_pauses():
    prev = s(0)
    cur = s(10, swapouts=pages(L.swapout_mb_s * 10 * 2))
    assert G.assess(prev, prev, cur, L) == ["swapout"]


def test_swapout_below_rate_is_ok():
    prev = s(0)
    assert G.assess(prev, prev, s(10, swapouts=pages(L.swapout_mb_s * 10 / 2)), L) == []


def test_critical_pressure_low_free_and_unreadable_signal_pause():
    prev = s(0)
    assert G.assess(prev, prev, s(10, pressure=4), L) == ["pressure"]
    assert G.assess(prev, prev, s(10, free=L.min_free_pct - 1), L) == ["free"]
    assert G.assess(prev, prev, None, L) == ["no-signal"]          # 신호를 못 읽으면 닫힌 쪽으로
    assert G.assess(prev, prev, s(10, free=None), L) == ["no-signal"]


def test_swap_growth_since_start_is_an_abort_reason():
    base = s(0, swap_mb=8000)
    assert "swap-growth" in G.assess(base, s(10, swap_mb=8000), s(20, swap_mb=8000 + L.swap_growth_abort_mb + 1), L)


# ---------------------------------------------------------------- 정지·재개·최대 대기

def run_guard(samples, limits=L):
    """합성 샘플 열로 check() 한 번을 돌린다. 시계는 샘플 시각을 따른다."""
    it = iter(samples)
    clock = {"t": samples[0].t if samples[0] else 0.0}

    def sample():
        x = next(it)
        if x is not None:
            clock["t"] = x.t
        return x

    def sleep(sec):
        clock["t"] += sec

    g = G.Guard(limits, sample=sample, now=lambda: clock["t"], sleep=sleep)
    return g


def test_resume_needs_consecutive_ok_samples_and_records_a_window():
    bad = pages(L.swapout_mb_s * L.interval_s * 3)
    seq = [s(0), s(10, swapouts=bad), s(20, swapouts=bad), s(30, swapouts=bad), s(40, swapouts=bad),
           s(50, swapouts=bad)]
    g = run_guard(seq)
    g.check()                                  # 시작 전 확인 - 정상
    g._last = -1e9                             # 다음 check 가 곧바로 표본을 뜨게
    g.check()
    # 10 에서 정지 -> 20 에서 ok 1 (bad 는 누적값이라 속도 0) -> 30 ok 2 -> 40 ok 3 = 재개
    assert g.windows == [(0, 40)]              # 간섭 구간은 마지막 정상 표본부터 재개까지
    assert g.aborted is None


def test_pause_longer_than_max_wait_aborts():
    lim = G.Limits(max_wait_s=30)
    rate = pages(lim.swapout_mb_s * lim.interval_s * 3)
    seq = [s(0)] + [s(10 * i, swapouts=rate * i) for i in range(1, 10)]
    g = run_guard(seq, lim)
    g.check()
    g._last = -1e9
    with pytest.raises(G.GuardAbort):
        g.check()
    assert g.aborted and g.aborted.startswith("max-wait")
    assert g.windows and g.windows[-1][0] == 0


def test_swap_growth_aborts_without_waiting():
    seq = [s(0, swap_mb=8000), s(10, swap_mb=8000 + L.swap_growth_abort_mb + 1)]
    g = run_guard(seq)
    g.check()
    g._last = -1e9
    with pytest.raises(G.GuardAbort):
        g.check()
    assert g.aborted.startswith("swap-growth")


def test_check_is_throttled_to_the_sampling_interval():
    calls = []
    g = G.Guard(L, sample=lambda: calls.append(1) or s(0), now=lambda: 5.0, sleep=lambda x: None)
    g.check()
    g.check()
    g.check()
    assert len(calls) == 1


def test_meta_is_json_ready():
    g = run_guard([s(0)])
    g.check()
    m = g.meta()
    assert m["windows"] == [] and m["aborted"] is None and m["limits"]["swapout_mb_s"] == L.swapout_mb_s


# ---------------------------------------------------------------- 운영 사이드카 로그

LOG = """2026-09-26 03:32:54,946 WARNING rerank_queue_timeout_absolute waited_ms=7505.0 queue_depth=0
INFO:     127.0.0.1:49800 - "POST /rerank HTTP/1.1" 503 Service Unavailable
INFO:     127.0.0.1:50503 - "GET /info HTTP/1.1" 200 OK
2026-09-26 03:33:03,197 WARNING rerank_queue_timeout waited_ms=7502.1 queue_depth=0
2026-09-26 03:33:04,000 WARNING rerank_queue_full queue_depth=9
INFO:     127.0.0.1:49800 - "POST /rerank HTTP/1.1" 503 Service Unavailable
INFO:     127.0.0.1:60716 - "POST /rerank HTTP/1.1" 200 OK
"""


def test_sidecar_errors_are_counted_from_an_offset(tmp_path):
    log = tmp_path / "server.log"
    log.write_text("old 503 line\n")
    off = G.sidecar_log_offset(str(log))
    with open(log, "a") as fh:
        fh.write(LOG)
    assert G.sidecar_errors_since(off, str(log)) == {"http_503": 2, "deadline": 2, "queue_full": 1}


def test_sidecar_log_rotation_reads_from_start_and_missing_log_is_none(tmp_path):
    log = tmp_path / "server.log"
    log.write_text("x" * 5000)
    off = G.sidecar_log_offset(str(log))
    log.write_text(LOG)                       # 회전 - 작아졌다
    assert G.sidecar_errors_since(off, str(log))["http_503"] == 2
    assert G.sidecar_errors_since(None, str(log)) is None
    assert G.sidecar_log_offset(str(tmp_path / "nope.log")) is None


def _copytruncate(log):
    """rerank-server/rotate-logs.sh 와 같은 회전: cp -> 비우기 -> gzip (inode 유지)."""
    import gzip
    import shutil
    with open(log, "rb") as src, gzip.open(f"{log}.1.gz", "wb") as dst:
        shutil.copyfileobj(src, dst)
    open(log, "w").close()


@pytest.mark.parametrize("pad_after", [0, 20000])       # 회전 뒤 새 파일이 오프셋보다 작을 때(H1) / 클 때(H2)
def test_sidecar_errors_survive_a_copytruncate_rotation_mid_run(tmp_path, pad_after):
    log = tmp_path / "server.log"
    log.write_text("x" * 10000 + "\n")
    off = G.sidecar_log_offset(str(log))
    with open(log, "a") as fh:
        fh.write(LOG)                                   # 회전 전: 503 2건
    _copytruncate(str(log))
    with open(log, "a") as fh:
        fh.write(LOG + "y" * pad_after + "\n")          # 회전 뒤: 503 2건
    assert G.sidecar_errors_since(off, str(log)) == {"http_503": 4, "deadline": 4, "queue_full": 2}
