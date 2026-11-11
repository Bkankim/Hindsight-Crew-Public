"""벤치 run 중 운영 리랭크 사이드카 keep-warm (issue #79).

벤치(runner, cold 재생 도구)가 도는 동안 운영 사이드카가 idle 이 되면(#21 warm 교차 run: 46~58분 idle) 첫 포화 recall 이
벤치 MPS 계산과 겹쳐 7.5s 데드라인을 넘고 503 이 난다. 그래서 run 동안 운영 사이드카에 아주 작은 리랭크 요청(쿼리 1, 텍스트 1)을
주기적으로 보내 cold 로 떨어지지 않게 한다. 간격과 근거는 bench/README "운영 규칙"의 벤치 간섭 가드 항목.

설계: **호출 사이 끼워넣기**. 벤치 호출 앞(runner)·idle 대기 걸음(cold 재생)에서 tick() 을 부르고, 간격이 지났을 때만 보낸다.
- 스레드 타이머는 측정 호출과 겹쳐 보낸다 - runner 에서는 측정 지연을 흔들고, cold 재생에서는 그 요청이 운영 rerank 로
  잡혀 프로브가 `interference` 로 빠진다. 종료·예외 때 join 도 필요하고, 가짜 시계 테스트가 비결정적이다.
- asyncio 태스크는 하네스 전체가 동기 코드라 runner·execute 를 다시 써야 한다.
- 끼워넣기는 결정적이고(시계·송신기 주입), run 이 끝나면 stop() 뒤 tick 이 아무것도 보내지 않는다. 대가는 한 호출(보통 약 6초,
  상한 CALL_TIMEOUT 90초)이나 가드 정지 안에서는 못 보낸다는 것이다 - 둘 다 벤치가 계산하지 않는 시간이거나 호출 직전 tick 이
  다시 데우므로 충돌 조건(운영 cold + 벤치 계산 중)을 만들지 않는다.

기록: 보낸 시각·실패는 meta 에 남고, 실패해도 run 을 멈추지 않는다(보호 장치 고장이 측정 실패로 번지지 않게). run 이 도는 구간
(start ~ stop + WARM_TAIL_S)은 `bench/fixtures/canary-exclude.txt` 형식 한 줄(KST, 시작은 내림·끝은 올림한 초 단위)로 meta 에 남아
카나리·기준선 추출이 벤치 창으로 뺀다. 끝을 5분 늘린 것은 카나리의 idle 이 제외 구간의 요청도 활동으로 세기 때문이다 - 마지막 keep-warm 뒤
5분 안에 온 운영 recall 은 keep-warm 이 없었다면 cold 인데 warm 으로 분류된다. 시각은 epoch 로 들고 있다가 KST 로 명시 변환한다(호스트 시간대와 무관, canary.parse_exclude 와 같은 해석).

실제 송신기(http_sender)의 기본 대상은 운영 사이드카다. KeepWarm 은 송신기를 필수 인자로 받고 라이브러리 함수(run, execute)는
기본값으로 켜지 않는다 - 테스트가 실수로 운영 사이드카를 치지 않게, 실제 송신기는 CLI(main)만 만든다(KeepWarm.production).
"""
from __future__ import annotations

import datetime as dt
import json
import math
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

PROD_RERANK_URL = "http://127.0.0.1:8787/rerank"
INTERVAL_S = 30.0          # cold 요청 기준(5분)보다 짧게, 지연 상승이 1분 부근에서 시작하므로 그 절반(근거 표는 bench/README)
TIMEOUT_S = 10.0           # 송신 상한 - 운영 사이드카 데드라인 7.5s 보다 길게. 실패해도 이 시간만큼만 호출이 밀린다
OWN_SLACK_S = 0.25         # 운영 로그 줄(끝 시각, ms)과 우리가 잰 송신 구간을 맞출 때의 허용
WARM_TAIL_S = 300.0        # 제외 구간을 run 끝 뒤로 늘리는 길이 = canary.COLD_IDLE_S. 카나리는 직전 요청 뒤 5분 안의 recall 을 warm 으로 센다
KST = dt.timezone(dt.timedelta(hours=9))   # bench/canary.KST 와 같다 - canary 를 import 하지 않고 같은 값을 둔다
BODY = {"query": "keep-warm", "texts": ["keep-warm"], "return_text": False}   # 쿼리 1, 텍스트 1

# HTTP 상태, 또는 (상태, 사이드카가 본문에 적은 사유 - rerank_queue_timeout·rerank_queue_full)를 돌려준다(200 이 아니면 실패).
# 연결 실패 등은 예외로 올려도 된다
Sender = Callable[[], Union[int, Tuple[int, str]]]


def http_sender(url: str = PROD_RERANK_URL, timeout: float = TIMEOUT_S) -> Sender:
    """실제 송신기. 기본 대상은 운영 사이드카다 - 테스트는 이것을 쓰지 않고 송신기를 주입한다."""
    data = json.dumps(BODY).encode()

    def send() -> int:
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                r.read()
                return r.status
        except urllib.error.HTTPError as e:
            try:
                reason = json.loads(e.read()).get("error")          # 사이드카 503 본문 {"error": "rerank_queue_timeout" | "rerank_queue_full"}
            except (ValueError, AttributeError, OSError):
                reason = None
            return (e.code, reason) if isinstance(reason, str) else e.code

    return send


def _kst(t: float) -> str:
    return dt.datetime.fromtimestamp(t, KST).strftime("%Y-%m-%d %H:%M:%S")


def exclude_line(start: float, end: float, reason: str) -> str:
    """canary 제외 파일 한 줄(`시작 끝  # 사유`, KST). 구간을 덮도록 시작은 내림, 끝은 올림하고, 끝이 시작보다 뒤가 되게 한다
    (parse_exclude 는 끝 <= 시작을 거부한다)."""
    s = math.floor(start)
    e = max(math.ceil(end), s + 1)
    return f"{_kst(s)} {_kst(e)}  # {' '.join(reason.split())}"


class KeepWarm:
    def __init__(self, send: Sender, now: Callable[[], float] = time.time, interval_s: float = INTERVAL_S,
                 url: str = PROD_RERANK_URL) -> None:
        self._send, self._now, self.interval_s, self.url = send, now, interval_s, url
        self.started: Optional[float] = None
        self.stopped: Optional[float] = None
        self._last = float("-inf")
        self.sends: List[Dict[str, Any]] = []

    @classmethod
    def production(cls) -> "KeepWarm":
        """CLI 전용: 운영 사이드카로 실제로 보낸다."""
        return cls(http_sender())

    def start(self) -> None:
        if self.started is None:
            self.started = self._now()

    def tick(self) -> None:
        """간격이 지났으면 한 번 보낸다. start() 전이나 stop() 뒤에는 아무것도 보내지 않는다. 실패는 기록하고 삼킨다."""
        if self.started is None or self.stopped is not None:
            return
        t = self._now()
        if t - self._last < self.interval_s:
            return
        self._last = t
        rec: Dict[str, Any] = {"t": t}
        try:
            out = self._send()
            status, reason = out if isinstance(out, tuple) else (out, None)
            rec["status"] = status
            if status != 200:
                rec["error"] = reason or f"http-{status}"
        except Exception as e:                       # 보호 장치 고장이 측정을 멈추면 안 된다(KeyboardInterrupt 는 그대로 올린다)
            rec["status"], rec["error"] = None, type(e).__name__
        rec["end"] = self._now()
        self.sends.append(rec)

    def stop(self) -> None:
        if self.started is not None and self.stopped is None:
            self.stopped = self._now()

    def without_own(self, intervals: Optional[List[Tuple[Any, ...]]]) -> Optional[List[Tuple[Any, ...]]]:
        """운영 사이드카 rerank 구간 목록에서 우리 keep-warm 요청을 뺀다(cold 재생이 자기 keep-warm 을 운영 간섭으로 세지 않게).
        구간은 (시작, 끝) 또는 (시작, 끝, 후보 수). 우리 송신 구간(+허용)에 통째로 들어가고, 후보 수를 알면 그것이 1(우리 요청의 텍스트 수)인
        구간만 뺀다 - 그 밖의 운영 rerank(같은 순간 큐에 있던 다른 요청)는 그대로 남는다. 후보 수를 모르는 (시작, 끝)은 송신 구간만 본다."""
        if intervals is None:
            return None
        mine = [(s["t"] - OWN_SLACK_S, s["end"] + OWN_SLACK_S) for s in self.sends]
        return [iv for iv in intervals
                if not (any(lo <= iv[0] and iv[1] <= hi for lo, hi in mine) and (len(iv) < 3 or iv[2] in (None, 1)))]

    def own_sidecar_errors(self) -> Dict[str, int]:
        """우리 keep-warm 요청이 운영 사이드카 로그에 남겼을 503·데드라인·큐 포화 수(`meta.prod_sidecar` 와 같은 키). 응답을 받은 실패만 센다 -
        연결 실패·클라이언트 타임아웃은 사이드카 로그와 대응을 알 수 없다. 운영 구성에서 타임아웃 503 한 건은 `rerank_queue_timeout_absolute`
        경고 한 줄을 남긴다(rerank-server/server.py)."""
        out = {"http_503": 0, "deadline": 0, "queue_full": 0}
        for s in self.sends:
            if s.get("status") == 503:
                out["http_503"] += 1
                if s.get("error") == "rerank_queue_timeout":
                    out["deadline"] += 1
                elif s.get("error") == "rerank_queue_full":
                    out["queue_full"] += 1
        return out

    def meta(self, label: str = "") -> Dict[str, Any]:
        failures = [{"t": s["t"], "error": s["error"], "status": s["status"]} for s in self.sends if "error" in s]
        out: Dict[str, Any] = {"url": self.url, "interval_s": self.interval_s, "started": self.started,
                               "stopped": self.stopped, "sent": len(self.sends), "failed": len(failures),
                               "sent_at": [s["t"] for s in self.sends], "failures": failures,
                               "exclude_line": None}
        if self.started is not None and self.stopped is not None:
            out["exclude_line"] = exclude_line(self.started, self.stopped + WARM_TAIL_S,
                                               f"벤치 keep-warm 구간{(' ' + label) if label else ''} (끝 +{WARM_TAIL_S:g}s: cold 판정 꼬리)")
        return out
