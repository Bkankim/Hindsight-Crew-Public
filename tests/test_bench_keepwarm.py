"""issue #79 벤치 run 중 운영 사이드카 keep-warm - 시계·송신기를 주입해 간격·정지·실패 기록·meta 구간을 고정한다.
실제 네트워크 송신은 어떤 테스트에서도 일어나지 않는다(autouse 소켓 차단 + KeepWarm 은 송신기를 필수 인자로 받는다)."""
import socket

import pytest

from bench import canary
from bench import cold_replay
from bench import keepwarm as KW

T0 = 1_790_000_000.0       # 2026-09-21 경, 임의 epoch


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """이 파일의 모든 테스트에서 소켓 연결을 막고 시도를 모은다. 운영 사이드카(8787)에는 한 번도 닿지 않아야 한다."""
    attempts = []

    def blocked(self, address, *a, **k):
        attempts.append(address)
        raise ConnectionRefusedError("blocked in tests: no real network send")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", lambda self, address, *a: attempts.append(address) or 111)
    return attempts


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


class Sender:
    """주입 송신기: 호출 시각을 적고 정해 둔 응답(상태 코드 또는 예외)을 차례로 돌려준다. 다 쓰면 200."""

    def __init__(self, clock, script=()):
        self.clock, self.script, self.at = clock, list(script), []

    def __call__(self):
        self.at.append(self.clock.t)
        out = self.script.pop(0) if self.script else 200
        if isinstance(out, BaseException):
            raise out
        return out


def make(script=(), interval_s=30.0):
    c = Clock()
    s = Sender(c, script)
    return KW.KeepWarm(s, now=c, interval_s=interval_s), c, s


# ---------------------------------------------------------------- 간격

def test_sends_on_the_interval_and_not_between():
    kw, c, s = make()
    kw.start()
    for dt_ in (0, 10, 20, 30, 40, 59, 60, 89, 90):          # 손계산: 0, 30, 60, 90 에서만 나간다
        c.t = T0 + dt_
        kw.tick()
    assert [t - T0 for t in s.at] == [0, 30, 60, 90]


def test_interval_is_shorter_than_the_cold_request_threshold_and_under_a_minute():
    # 근거(bench/README keep-warm 표): cold 요청 기준 5분보다 짧고, 지연 상승이 시작하는 1분 부근보다 짧다
    assert KW.INTERVAL_S < cold_replay.IDLE_S == 300.0
    assert KW.INTERVAL_S <= 30.0 < 60.0


def test_request_is_one_query_and_one_text_to_the_production_sidecar_by_default():
    assert KW.BODY == {"query": "keep-warm", "texts": ["keep-warm"], "return_text": False}
    assert KW.PROD_RERANK_URL == "http://127.0.0.1:8787/rerank"


# ---------------------------------------------------------------- run 동안만

def test_nothing_is_sent_before_start_or_after_stop():
    kw, c, s = make()
    kw.tick()
    assert s.at == []                                         # start 전
    kw.start()
    kw.tick()
    c.t += 100
    kw.stop()
    kw.tick()
    c.t += 100
    kw.tick()
    assert len(s.at) == 1                                     # stop 뒤에는 간격이 한참 지나도 보내지 않는다


def test_stop_and_start_are_idempotent():
    kw, c, _ = make()
    kw.stop()                                                 # start 전 stop 은 구간을 만들지 않는다
    assert kw.meta()["exclude_line"] is None
    kw.start()
    c.t += 5
    kw.start()                                                # 두 번째 start 는 시작 시각을 바꾸지 않는다
    c.t += 5
    kw.stop()
    c.t += 5
    kw.stop()
    m = kw.meta()
    assert m["started"] == T0 and m["stopped"] == T0 + 10


# ---------------------------------------------------------------- 실패는 기록하고 계속

def test_failures_are_recorded_and_sending_continues():
    kw, c, s = make([503, ConnectionRefusedError("x"), 200])
    kw.start()
    for _ in range(4):
        kw.tick()
        c.t += 30
    m = kw.meta()
    assert m["sent"] == 4 and m["failed"] == 2                # 4번째는 스크립트가 끝나 200
    assert [f["error"] for f in m["failures"]] == ["http-503", "ConnectionRefusedError"]
    assert [f["t"] - T0 for f in m["failures"]] == [0, 30]
    assert [t - T0 for t in m["sent_at"]] == [0, 30, 60, 90]


def test_a_sender_that_always_raises_never_raises_out_of_tick():
    kw, c, _ = make([RuntimeError("boom")] * 3)
    kw.start()
    for _ in range(3):
        kw.tick()                                             # 예외가 run 으로 번지면 보호 장치 고장이 측정 실패가 된다
        c.t += 30
    assert kw.meta()["failed"] == 3


# ---------------------------------------------------------------- meta 구간과 제외 줄

def test_meta_records_the_window_as_a_canary_exclude_line():
    kw, c, _ = make()
    c.t = T0 + 0.4
    kw.start()
    kw.tick()
    c.t = T0 + 900.2
    kw.stop()
    m = kw.meta("run-x")
    assert m["started"] == T0 + 0.4 and m["stopped"] == T0 + 900.2 and m["sent"] == 1
    (s, e, why), = canary.parse_exclude(m["exclude_line"])    # 카나리 제외 파일 파서를 그대로 통과한다
    assert s <= m["started"] and e >= m["stopped"]            # 구간을 덮는다(시작 내림, 끝 올림)
    assert s == int(T0) and e == int(T0 + 1201)               # 손계산: floor(T0+0.4)=T0, ceil(T0+900.2+300)=T0+1201 (끝 + cold 판정 꼬리 300s)
    assert "keep-warm" in why and "run-x" in why


def test_exclude_window_extends_past_the_run_by_the_canary_cold_threshold():
    # 카나리의 idle 은 제외 구간의 요청도 활동으로 센다 - 마지막 송신 뒤 5분 안의 recall 은 warm 이 된다. 그 꼬리까지 덮는다
    assert KW.WARM_TAIL_S == canary.COLD_IDLE_S == 300.0


def test_exclude_line_is_still_a_valid_nonempty_window_when_the_run_is_instant():
    kw, c, _ = make()
    kw.start()
    kw.stop()                                                 # 시작=끝 - parse_exclude 는 끝 <= 시작을 거부한다
    (s, e, _), = canary.parse_exclude(kw.meta()["exclude_line"])
    assert e > s


@pytest.mark.parametrize("tz", ["UTC", "Asia/Seoul", "America/Los_Angeles"])
def test_exclude_line_is_kst_whatever_the_host_timezone(tz, monkeypatch):
    import time
    monkeypatch.setenv("TZ", tz)
    time.tzset()
    try:
        kw, c, _ = make()
        c.t = 1_790_000_000.0                                 # 2026-09-21 14:13:20 UTC = 23:13:20 KST (date -u -r 로 확인)
        kw.start()
        c.t += 600
        kw.stop()
        line = kw.meta()["exclude_line"]
        assert line.startswith("2026-09-21 23:13:20 2026-09-21 23:28:20  #")      # 끝 = run 끝 23:23:20 + 꼬리 300s
        (s, e, _), = canary.parse_exclude(line)
        assert (s, e) == (1_790_000_000.0, 1_790_000_900.0)
    finally:
        monkeypatch.undo()                                    # TZ 를 원래대로 되돌리고 프로세스 시간대에 다시 반영
        time.tzset()


def test_own_requests_are_removed_from_production_rerank_intervals():
    kw, c, _ = make()
    kw.start()
    kw.tick()                                                 # T0 에 송신(순간)
    own = (T0 - 0.02, T0 + 0.02)                              # 우리 요청이 운영 로그에 남긴 구간
    foreign = (T0 - 0.02, T0 + 3.0)                           # 같은 시각에 시작했지만 길게 도는 남의 요청 - 남긴다
    other = (T0 + 50.0, T0 + 52.0)
    assert kw.without_own([own, foreign, other]) == [foreign, other]
    assert kw.without_own(None) is None                       # 로그를 못 읽은 신호는 그대로 전달


# ---------------------------------------------------------------- 실제 송신 없음

def test_the_real_sender_targets_the_production_sidecar_but_the_test_network_is_blocked(no_real_network):
    send = KW.http_sender()                                   # 기본 대상 = 운영 사이드카
    with pytest.raises(OSError):
        send()
    assert no_real_network == [("127.0.0.1", 8787)]           # 시도는 막혔고 어디에도 닿지 않았다


def test_keepwarm_requires_an_injected_sender():
    with pytest.raises(TypeError):
        KW.KeepWarm()                                          # 기본 송신기가 없다 - 테스트가 실수로 운영 사이드카를 치지 못한다


# ---------------------------------------------------------------- 자기 요청 판별(후보 수)과 자기 실패 수 (final-review 2026-10-02)

def test_without_own_removes_only_one_candidate_requests_inside_the_send_window():
    kw, c, _ = make()
    kw.start()
    kw.tick()                                                 # T0 에 송신
    own = (T0 - 0.02, T0 + 0.02, 1)                           # 우리 요청: 후보 1
    concurrent = (T0 - 0.02, T0 + 0.02, 128)                  # 같은 창 안에서 끝난 남의 요청(큐에서 기다리다 끝남) - 남긴다
    unknown = (T0 - 0.02, T0 + 0.02, None)                    # 줄에 candidates 가 없어 모른다 - 지금까지처럼 송신 구간만 본다
    plain = (T0 - 0.02, T0 + 0.02)                            # 후보 수 없는 (시작, 끝)도 같다
    assert kw.without_own([own, concurrent, unknown, plain]) == [concurrent]


def test_failure_reason_from_the_sidecar_body_is_recorded_and_counted_like_the_log_patterns():
    kw, c, _ = make([(503, "rerank_queue_timeout"), (503, "rerank_queue_full"), 503, ConnectionRefusedError("x"), 500])
    kw.start()
    for _ in range(5):
        kw.tick()
        c.t += 30
    m = kw.meta()
    assert [f["error"] for f in m["failures"]] == ["rerank_queue_timeout", "rerank_queue_full", "http-503",
                                                  "ConnectionRefusedError", "http-500"]
    assert [f["status"] for f in m["failures"]] == [503, 503, 503, None, 500]
    # 손계산: 503 응답 3건(사유 있는 2 + 사유 모르는 1) -> http_503 3, 타임아웃 사유 1 -> deadline 1, 큐 포화 사유 1 -> queue_full 1.
    # 연결 실패(상태 없음)와 500 은 사이드카 503 줄이 아니라 세지 않는다
    assert kw.own_sidecar_errors() == {"http_503": 3, "deadline": 1, "queue_full": 1}


def test_http_sender_reads_the_reason_from_a_503_body_without_touching_the_network(monkeypatch):
    import io
    import urllib.error
    import urllib.request

    def fake(body, code=503):
        def urlopen(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(body))
        return urlopen

    monkeypatch.setattr(urllib.request, "urlopen", fake(b'{"error": "rerank_queue_timeout", "waited_ms": 7500}'))
    assert KW.http_sender()() == (503, "rerank_queue_timeout")
    monkeypatch.setattr(urllib.request, "urlopen", fake(b"not json"))
    assert KW.http_sender()() == 503                          # 본문을 못 읽으면 상태만


def test_off_keepwarm_has_no_own_errors_key_to_subtract():
    kw, _, _ = make()
    assert kw.own_sidecar_errors() == {"http_503": 0, "deadline": 0, "queue_full": 0}
