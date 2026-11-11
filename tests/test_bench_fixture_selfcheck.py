"""issue #13 자기검증 fixture - "결과를 아는" 악화가 현재 게이트웨이 코드에 적용되고, 판정기가 그 악화를 기대한 등급으로 잡는다.

fixture 가 적용되지 않으면 하네스는 자기 판정이 맞는지 증명할 수 없다(결함 03: prune_result 에 bank 인자가 생긴 뒤
reverse/pad/tail-cut 이 strict git apply 에서 조용히 깨져 있었다). 이 테스트가 그 드리프트를 먼저 잡는다.
"""
import importlib
import os
import shutil
import subprocess
import sys

import pytest

from bench import judge as J
from gateway import recall_prune as P

from bench import selfcheck as S

REPO = S.REPO
FIX = S.FIX_DIR
BANK = "personal-demo"
ROWS = [{"id": "a", "text": "t1", "document_id": "d1", "chunk_id": f"{BANK}_d1_0", "tags": ["x"],
         "occurred_start": "2026-01-01", "mentioned_at": "2026-01-01", "scores": {"final": 1.0}},
        {"id": "b", "text": "t2", "document_id": "d2", "chunk_id": f"{BANK}_d2_3", "tags": [],
         "occurred_start": "2026-01-02", "scores": {"final": 0.9}}]

PRUNER_FIXTURES = S.PRUNER_FIXTURES


def test_every_required_fixture_applies_to_the_current_gateway():
    assert S.broken_fixtures() == []


def test_a_fixture_that_no_longer_applies_stops_the_gate(tmp_path):
    (tmp_path / "reverse.patch").write_text("--- a/gateway/recall_prune.py\n+++ b/gateway/recall_prune.py\n"
                                            "@@ -1,1 +1,1 @@\n-no such line\n+x\n")
    for f in S.REQUIRED:
        if f != "reverse.patch":
            shutil.copy(os.path.join(FIX, f), tmp_path / f)
    assert S.broken_fixtures(fix_dir=str(tmp_path)) == ["reverse.patch"]
    with pytest.raises(SystemExit):
        S.require_fixtures(fix_dir=str(tmp_path))


def test_runner_refuses_to_start_when_self_check_fails(monkeypatch):
    from bench import runner as RN

    def broken():
        raise SystemExit("self-check fixtures no longer apply")
    monkeypatch.setattr(RN, "load_corpus", lambda *a, **k: pytest.fail("run started despite a failed self-check"))
    with pytest.raises(SystemExit):
        RN.run({"a": 18891, "b": 18892}, "dev", 1, 0, None, 13, "t", "tok", check_ports=lambda arms: None,
               selfcheck=broken)


def _patched_pruner(tmp_path, fixture):
    """fixture 를 적용한 recall_prune 사본을 별도 패키지로 올린다(실제 게이트웨이 모듈은 그대로)."""
    pkg = f"fx_{fixture.split('.')[0].replace('-', '_')}"
    src = tmp_path / "gateway"
    src.mkdir()
    for f in ("__init__.py", "recall_prune.py", "reframe.py"):
        shutil.copy(os.path.join(REPO, "gateway", f), src / f)
    r = subprocess.run(["git", "apply", "--include=gateway/recall_prune.py", os.path.join(FIX, fixture)],
                       cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    src.rename(tmp_path / pkg)
    sys.path.insert(0, str(tmp_path))
    try:
        return importlib.import_module(f"{pkg}.recall_prune")
    finally:
        sys.path.remove(str(tmp_path))


@pytest.mark.parametrize("fixture,expected", sorted(PRUNER_FIXTURES.items()))
def test_judge_catches_each_known_regression(tmp_path, fixture, expected):
    fx = _patched_pruner(tmp_path, fixture)
    base = P.prune_payload({"results": [dict(r) for r in ROWS]}, BANK)["results"]
    cand = fx.prune_payload({"results": [dict(r) for r in ROWS]}, BANK)["results"]
    assert J.compare_results(base, cand, BANK) == expected
