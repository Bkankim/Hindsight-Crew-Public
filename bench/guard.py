"""벤치 간섭 가드 (issue #19).

벤치(runner, cold 재생 도구)가 맥 호스트 메모리를 잡아 운영 recall 을 늦추거나 503 을 받게 하는 **벤치 간섭**을 막는다.
2026-09-26 selftest3-sets 창에서 MPS 리랭커 4벌이 스왑을 22/22.5GB 까지 채우자 운영 recall 이 503·재시도 3/4 회까지 갔다.

신호와 임계 (근거는 bench/README "벤치 간섭 가드" 절)
- 주 신호는 **swapout 속도**다. 쉬는 사이드카의 가중치가 스왑으로 밀려나는 순간이 간섭의 원인이었다.
  swapout_mb_s(3 MB/s)를 넘으면 정지한다. 벤치 없는 평상시 실측은 0 페이지/30초.
- 스왑 **사용량** 절대값은 쓰지 않는다. macOS 스왑은 한번 차면 줄지 않아 벤치 없이도 7.9/9.2GB(86%)였다.
  대신 가드 시작 뒤 증가분이 swap_growth_abort_mb(4GB)를 넘으면 기다리지 않고 중단한다(selftest3 은 +14GB).
- 압박 수준은 critical(4)만 본다. warn(2)은 벤치 없이도 나온다(2026-09-26 실측, 여유 37%).
- 여유 메모리(memory_pressure 의 free percentage)가 min_free_pct 아래면 정지한다.
- 신호를 못 읽으면 정지한다(닫힌 쪽).

정지 중에는 interval_s 마다 다시 보고, 연속 resume_ok 번 정상이면 재개한다. 정지가 max_wait_s 를 넘으면 중단한다.
간섭 구간 = 마지막 정상 표본 시각부터 재개(또는 중단) 시각까지. 판정기는 이 구간과 겹친 호출을 속도 판정에서 뺀다.

임계 판정(assess)과 상태 전이는 순수 함수이고, 표본 수집(sysctl·vm_stat·memory_pressure)만 IO 경계다.
"""
from __future__ import annotations

import dataclasses
import gzip
import os
import re
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

SIDECAR_LOG = os.path.expanduser("~/Library/Logs/hindsight-rerank/server.log")


@dataclasses.dataclass(frozen=True)
class Limits:
    swapout_mb_s: float = 3.0          # 이 속도를 넘는 swapout 이면 정지
    min_free_pct: float = 10.0         # 여유 메모리 % 가 이 아래면 정지
    critical_pressure: int = 4         # kern.memorystatus_vm_pressure_level 이 이 이상이면 정지
    swap_growth_abort_mb: float = 4096  # 가드 시작 뒤 스왑 사용량 증가가 이를 넘으면 중단
    resume_ok: int = 3                 # 재개에 필요한 연속 정상 표본 수
    interval_s: float = 10.0           # 표본 간격(정지 중 재확인 간격과 같다)
    max_wait_s: float = 1800.0         # 정지가 이보다 길면 중단


@dataclasses.dataclass(frozen=True)
class Sample:
    t: float                           # 벽시계 epoch 초
    pressure: Optional[int]
    swap_used_mb: Optional[float]
    swapouts: Optional[int]            # 누적 swapout 페이지 수
    page_size: Optional[int]
    free_pct: Optional[float]


class GuardAbort(Exception):
    pass


ABORT_REASONS = ("swap-growth",)


def assess(base: Sample, prev: Sample, cur: Optional[Sample], limits: Limits) -> List[str]:
    """cur 표본이 벤치를 멈춰야 하는 이유 목록. 빈 목록 = 정상. base 는 가드 시작 표본, prev 는 직전 표본."""
    if cur is None:
        return ["no-signal"]
    pressure, swap_mb, swapouts, free, page = cur.pressure, cur.swap_used_mb, cur.swapouts, cur.free_pct, cur.page_size
    if pressure is None or swap_mb is None or swapouts is None or free is None or page is None:
        return ["no-signal"]
    out = []
    dt = cur.t - prev.t
    if prev.swapouts is not None and dt > 0 and swapouts >= prev.swapouts:
        if (swapouts - prev.swapouts) * page / (1024 * 1024) / dt > limits.swapout_mb_s:
            out.append("swapout")
    if pressure >= limits.critical_pressure:
        out.append("pressure")
    if free < limits.min_free_pct:
        out.append("free")
    if base.swap_used_mb is not None and swap_mb - base.swap_used_mb > limits.swap_growth_abort_mb:
        out.append("swap-growth")
    return out


# ---------------------------------------------------------------- 표본 수집 (IO 경계)

def _sysctl(name: str) -> Optional[str]:
    try:
        return subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=5).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def host_sample() -> Optional[Sample]:
    try:
        vm = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
        mp = subprocess.run(["memory_pressure", "-Q"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    page = re.search(r"page size of (\d+) bytes", vm)
    so = re.search(r"Swapouts:\s+(\d+)", vm)
    free = re.search(r"free percentage:\s*(\d+)%", mp)
    swap = re.search(r"used = ([\d.]+)M", _sysctl("vm.swapusage") or "")
    level = _sysctl("kern.memorystatus_vm_pressure_level")
    return Sample(t=time.time(), pressure=int(level) if level and level.isdigit() else None,
                  swap_used_mb=float(swap.group(1)) if swap else None,
                  swapouts=int(so.group(1)) if so else None, page_size=int(page.group(1)) if page else None,
                  free_pct=float(free.group(1)) if free else None)


# ---------------------------------------------------------------- 가드

class Guard:
    """벤치 호출 앞에서 check() 를 부른다. 정상이면 곧바로 돌아오고(표본은 interval_s 마다만 뜬다), 정지 조건이면
    재개될 때까지 막고, 중단 조건이면 GuardAbort 를 던진다."""

    def __init__(self, limits: Limits = Limits(), sample: Callable[[], Optional[Sample]] = host_sample,
                 now: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> None:
        self.limits, self._sample, self._now, self._sleep = limits, sample, now, sleep
        self.base: Optional[Sample] = None
        self.prev: Optional[Sample] = None
        self._last = float("-inf")
        self.windows: List[Tuple[float, float]] = []
        self.pauses: List[Dict[str, Any]] = []
        self.aborted: Optional[str] = None

    def _take(self) -> List[str]:
        cur = self._sample()
        self._last = self._now()
        if self.base is None:
            if cur is None:
                return ["no-signal"]
            self.base = self.prev = cur
        reasons = assess(self.base, self.prev, cur, self.limits)  # type: ignore[arg-type]
        if cur is not None:
            self.prev = cur
        return reasons

    def check(self) -> None:
        if self.aborted:
            raise GuardAbort(self.aborted)
        if self._now() - self._last < self.limits.interval_s:
            return
        last_ok = self.prev.t if self.prev is not None else self._now()
        reasons = self._take()
        if not reasons:
            return
        paused_at, ok = self._now(), 0
        pause = {"reasons": sorted(set(reasons))}
        while True:
            hard = [r for r in reasons if r in ABORT_REASONS]
            waited = self._now() - paused_at
            if hard or waited >= self.limits.max_wait_s:
                self.aborted = hard[0] if hard else f"max-wait:{int(waited)}s"
                self.windows.append((last_ok, self._now()))
                self.pauses.append({**pause, "aborted": self.aborted})
                raise GuardAbort(self.aborted)
            self._sleep(self.limits.interval_s)
            reasons = self._take()
            if reasons:
                ok = 0
                pause["reasons"] = sorted(set(pause["reasons"]) | set(reasons))
                continue
            ok += 1
            if ok >= self.limits.resume_ok:
                self.windows.append((last_ok, self._now()))
                self.pauses.append({**pause, "waited_s": round(self._now() - paused_at, 1)})
                return

    def meta(self) -> Dict[str, Any]:
        return {"limits": dataclasses.asdict(self.limits), "windows": [list(w) for w in self.windows],
                "pauses": self.pauses, "aborted": self.aborted}


# ---------------------------------------------------------------- 운영 사이드카 로그

SIDECAR_PATTERNS = {
    "http_503": re.compile(r'"POST /rerank HTTP/[\d.]+" 503'),
    "deadline": re.compile(r"WARNING rerank_queue_timeout(_absolute)? "),
    "queue_full": re.compile(r"WARNING rerank_queue_full "),
}


def _stat(path: str) -> Optional[Tuple[int, int]]:
    try:
        st = os.stat(path)
        return st.st_mtime_ns, st.st_size
    except OSError:
        return None


def sidecar_log_offset(path: str = SIDECAR_LOG) -> Optional[Dict[str, Any]]:
    """집계 시작 표지: 현재 로그 크기 + 회전본(.1.gz) 상태. 회전은 copytruncate 라(rerank-server/rotate-logs.sh,
    매일 04:00·10MB 이상) inode 가 그대로다 - 크기만으로는 회전 뒤 새 파일이 오프셋을 넘겼을 때 알아채지 못한다."""
    size = _stat(path)
    if size is None:
        return None
    return {"offset": size[1], "rotated": _stat(f"{path}.1.gz")}


def sidecar_text_since(mark: Optional[Dict[str, Any]], path: str = SIDECAR_LOG) -> Optional[str]:
    """표지 이후 운영 사이드카 로그 본문. 그 사이 회전했으면 옛 파일의 표지 이후 꼬리(.1.gz 의 offset 이후)와
    새 파일 전체를 잇는다. 한 run 동안 회전은 많아야 한 번이다(하루 1회)."""
    if mark is None:
        return None
    offset = int(mark["offset"])
    rot = _stat(f"{path}.1.gz")
    try:
        with open(path, "rb") as fh:
            if rot is not None and rot != (tuple(mark["rotated"]) if mark.get("rotated") else None):
                with gzip.open(f"{path}.1.gz", "rb") as old:
                    old.seek(offset)
                    return old.read().decode("utf-8", "replace") + fh.read().decode("utf-8", "replace")
            fh.seek(offset if os.path.getsize(path) >= offset else 0)
            return fh.read().decode("utf-8", "replace")
    except (OSError, EOFError):
        return None


def sidecar_errors_since(mark: Optional[Dict[str, Any]], path: str = SIDECAR_LOG) -> Optional[Dict[str, int]]:
    """표지 이후 운영 사이드카 로그의 503·데드라인 초과·큐 포화 수(회전 처리는 sidecar_text_since)."""
    text = sidecar_text_since(mark, path)
    if text is None:
        return None
    return {k: len(p.findall(text)) for k, p in SIDECAR_PATTERNS.items()}
