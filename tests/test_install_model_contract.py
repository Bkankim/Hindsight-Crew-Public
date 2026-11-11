"""#424: 설치기가 .env.local에 쓰는 LLM 모델과 gate9 기본 기대 모델의 계약.

./install은 HINDSIGHT_API_LLM_MODEL=LLM_MODEL을 쓰고, 이어지는 verify-all의 gate9는
HC_GATE9_MODEL이 없으면 자기 기본값과 그 값을 비교한다. 둘이 어긋나면 새 설치가 gate9에서 실패한다.
tests/test_install.py는 bootstrap을 대역으로 바꿔 이 불일치를 잡지 못하므로 여기서 직접 대조한다.

도커·launchd·네트워크는 쓰지 않는다: 설치기는 AST로 상수만 읽고(import하면 sys 전역을 바꾼다),
gate9는 모델 결정 줄 하나만 격리된 bash에서 평가한다(lib.sh·docker exec를 타지 않는다).
"""
from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INSTALLER = REPO / "scripts" / "install" / "hc_install.py"
GATE9 = REPO / "verify" / "gate9_llm_fallback.sh"


def installer_model() -> str:
    tree = ast.parse(INSTALLER.read_text(encoding="utf-8"))
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "LLM_MODEL"):
            assert isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
            return node.value.value
    raise AssertionError("LLM_MODEL 상수가 hc_install.py 최상위에 없다")


def gate9_model_line() -> str:
    lines = [l for l in GATE9.read_text(encoding="utf-8").splitlines() if re.match(r"^WANT_MODEL=", l)]
    assert len(lines) == 1, lines
    return lines[0]


def gate9_model(override: str | None) -> str:
    bash = shutil.which("bash")
    assert bash
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    if override is not None:
        env["HC_GATE9_MODEL"] = override
    r = subprocess.run([bash, "--noprofile", "--norc", "-c", gate9_model_line() + '\nprintf %s "$WANT_MODEL"'],
                       env=env, capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_installer_model_matches_gate9_default():
    """(a) HC_GATE9_MODEL 없이 gate9가 기대하는 모델 == 설치기가 쓰는 모델."""
    assert installer_model() == gate9_model(None)


def test_installer_writes_llm_model_constant():
    """설치기의 .env.local 쓰기 경로가 LLM_MODEL 상수를 그대로 쓴다(상수만 고치고 리터럴이 남는 회귀 방지)."""
    src = INSTALLER.read_text(encoding="utf-8")
    assert '"HINDSIGHT_API_LLM_MODEL": LLM_MODEL' in src
    assert len(re.findall(r'"claude-(?:opus|sonnet|haiku|fable)-[0-9-]+"', src)) == 1


@pytest.mark.parametrize("override", ["claude-opus-5-5", "claude-haiku-5-5", "some-other-model"])
def test_gate9_override_wins(override):
    """(b) HC_GATE9_MODEL을 주면 그 값이 기본값을 이긴다."""
    assert gate9_model(override) == override


def test_gate9_empty_override_falls_back_to_default():
    """빈 HC_GATE9_MODEL은 기본값으로 떨어진다(${VAR:-default} 의미 고정)."""
    assert gate9_model("") == gate9_model(None) != ""
