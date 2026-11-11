"""launchd 라벨 접두의 출처는 하나다 (issue #470).

운영 사이드카 plist 경로(bench/replica.py)와 라벨을 적은 주석·안내는 설치기와 같은 출처를 쓴다:
`HC_LAUNCHD_PREFIX`(환경 변수, 없으면 `.env.host.local`, scripts/hc_host_env.py), 둘 다 없으면 설치기 기본 접두
(scripts/install/hc_install.py `DEFAULT_PREFIX`). 접두 값은 합성 값만 쓴다.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "install"))
import hc_install  # noqa: E402

from bench import replica as R  # noqa: E402

PLIST_DIR = os.path.join("Library", "LaunchAgents")


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("HC_LAUNCHD_PREFIX", raising=False)
    return h


def _plist(home: Path, prefix: str) -> str:
    return str(home / PLIST_DIR / (prefix + ".hindsight-rerank.plist"))


@pytest.mark.parametrize("host_file,env,expected", [
    (None, None, "<installer default>"),                                     # 환경 변수·파일 둘 다 없음
    ("", None, "<installer default>"),                                       # 파일은 있지만 키가 없음
    ("HC_LAUNCHD_PREFIX=\n", None, "<installer default>"),                   # 빈 값은 없는 것과 같다
    ("HC_LAUNCHD_PREFIX=org.example.file\n", None, "org.example.file"),      # .env.host.local
    (None, "org.example.env", "org.example.env"),                            # 환경 변수
    ("HC_LAUNCHD_PREFIX=org.example.file\n", "org.example.env", "org.example.env"),  # 환경 변수가 이긴다
])
def test_replica_prod_plist_prefix_comes_from_the_installer_source(tmp_path, home, monkeypatch, host_file, env, expected):
    root = tmp_path / "repo"
    root.mkdir()
    if host_file is not None:
        (root / ".env.host.local").write_text(host_file, encoding="utf-8")
    if env is not None:
        monkeypatch.setenv("HC_LAUNCHD_PREFIX", env)
    want = hc_install.DEFAULT_PREFIX if expected == "<installer default>" else expected
    assert R.launchd_prefix(root) == want
    assert R.prod_sidecar_plist(root) == _plist(home, want)


def test_replica_default_is_the_installer_constant_not_a_copy(tmp_path, home, monkeypatch):
    """설치기 기본 접두를 바꾸면 replica 기본값도 따라 바뀐다(복사한 리터럴이 아니다)."""
    monkeypatch.setattr(hc_install, "DEFAULT_PREFIX", "org.example.moved")
    assert R.launchd_prefix(tmp_path) == "org.example.moved"


def test_replica_reads_the_repo_host_file_by_default(home, monkeypatch):
    """인자 없는 호출은 리포 루트의 .env.host.local 을 render-launchd.py 와 같은 함수로 읽는다."""
    seen = []
    monkeypatch.setattr(R, "_host_env_get", lambda key, root: seen.append((key, root)) or "org.example.seen")
    assert R.prod_sidecar_plist() == _plist(home, "org.example.seen")
    assert seen == [("HC_LAUNCHD_PREFIX", R.REPO)]


def test_replica_unreadable_host_file_stops_with_config_invalid(tmp_path, home):
    (tmp_path / ".env.host.local").write_bytes(b"HC_LAUNCHD_PREFIX=\xff\n")
    with pytest.raises(SystemExit, match="config_invalid"):
        R.prod_sidecar_plist(tmp_path)


def test_sidecar_up_opens_the_resolved_plist_when_not_overridden(tmp_path, home, monkeypatch):
    monkeypatch.setattr(R, "PROD_SIDECAR_PLIST", None)
    monkeypatch.setattr(R, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HC_LAUNCHD_PREFIX", "org.example.env")
    opened = []

    def fake_open(path, mode="r", *a, **k):
        opened.append(path)
        raise FileNotFoundError(path)

    monkeypatch.setattr(R, "open", fake_open, raising=False)      # 모듈 전역이 내장 open 을 가린다
    with pytest.raises(FileNotFoundError):
        R.sidecar_up("s1", 18790, None)
    assert opened == [_plist(home, "org.example.env")]


# 라벨을 적는 주석·안내: 접두는 리터럴이 아니라 HC_LAUNCHD_PREFIX(또는 템플릿 토큰 __PREFIX__)로 적는다.
LABEL_DOC_FILES = ["profiles/ko-full-mac-mps.env", "rerank-server/probe.sh", "scripts/profile-detect.sh"]
_LABEL = re.compile(r"(\S*)\.hindsight-rerank\b")
_OK_PREFIX = re.compile(r"(?:^|[/(`'\"])(?:\$HC_LAUNCHD_PREFIX|\$\{HC_LAUNCHD_PREFIX\}|<HC_LAUNCHD_PREFIX>|__PREFIX__)$")


@pytest.mark.parametrize("rel", LABEL_DOC_FILES)
def test_label_mentions_name_the_prefix_variable(rel):
    text = (ROOT / rel).read_text(encoding="utf-8")
    bad = [f"{rel}:{n}" for n, line in enumerate(text.splitlines(), 1)
           for m in _LABEL.finditer(line) if not _OK_PREFIX.search(m.group(1))]
    assert not bad, "launchd 라벨 접두를 리터럴로 적었다(HC_LAUNCHD_PREFIX 로): " + ", ".join(bad)


@pytest.mark.parametrize("prefix,ok", [
    ("$HC_LAUNCHD_PREFIX", True), ("${HC_LAUNCHD_PREFIX}", True), ("<HC_LAUNCHD_PREFIX>", True),
    ("__PREFIX__", True), ("~/Library/LaunchAgents/$HC_LAUNCHD_PREFIX", True),
    ("org.example", False), ("launchd org.example", False), ("~/Library/LaunchAgents/org.example", False),
])
def test_label_prefix_check_self(prefix, ok):
    m = _LABEL.search(prefix + ".hindsight-rerank")
    assert bool(_OK_PREFIX.search(m.group(1))) is ok
