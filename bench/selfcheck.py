"""recall 재생 하네스 - 자기검증 fixture 게이트 (issue #13).

fixture 는 "결과를 아는" 악화다. 현재 게이트웨이 코드에 적용되지 않으면 하네스는 자기 판정이 맞는지 증명할 수 없다
(결함 03: prune_result 에 bank 인자가 생긴 뒤 reverse/pad/tail-cut 이 strict git apply 에서 조용히 깨져 있었다).
그래서 판정 run 을 시작하기 전에 fixture 가 전부 적용되는지 확인한다. 등급 대응은 tests/test_bench_fixture_selfcheck.py.
"""
from __future__ import annotations

import os
import subprocess
from typing import Dict, List

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX_DIR = os.path.join(REPO, "bench", "fixtures")

# recall 프루너를 바꾸는 fixture 와 판정기가 내야 할 정확도 등급
PRUNER_FIXTURES: Dict[str, str] = {"reverse.patch": "changed", "pad.patch": "added", "tail-cut.patch": "prefix-cut",
                                   "drop-scores.patch": "lossy"}
LATENCY_FIXTURES = ("sleep.patch",)
# 결과 캐시는 #81 결론에 따라 불채택이라 그 fixture 는 두지 않는다
REQUIRED = tuple(sorted(PRUNER_FIXTURES)) + LATENCY_FIXTURES


def broken_fixtures(repo: str = REPO, fix_dir: str = FIX_DIR) -> List[str]:
    """현재 작업 트리에 strict 하게 적용되지 않는 필수 fixture."""
    return [f for f in REQUIRED
            if subprocess.run(["git", "apply", "--check", os.path.join(fix_dir, f)], cwd=repo,
                              capture_output=True).returncode != 0]


def require_fixtures(repo: str = REPO, fix_dir: str = FIX_DIR) -> None:
    broken = broken_fixtures(repo, fix_dir)
    if broken:
        raise SystemExit(f"self-check fixtures no longer apply: {broken} - regenerate them before judging runs")
