"""공개판 내보내기 도구 (#426, ADR 0002).

임시 git 리포 픽스처만 쓴다(운영 리포·네트워크 비의존). 이름 표·식별 패턴·주소는 전부 가짜다.
git-filter-repo(2.47 이상)·gitleaks 가 없으면 실제 내보내기 테스트는 skip 하고, 도구 부재·실행 오류는 대역으로 본다.
"""
import base64
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "public-export" / "export.py"

spec = importlib.util.spec_from_file_location("public_export_tool", SCRIPT)
ex = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ex)


def _has_tools() -> bool:
    if not shutil.which("gitleaks"):
        return False
    r = subprocess.run(["git", "filter-repo", "-h"], capture_output=True)
    return r.returncode == 0 and b"--file-info-callback" in r.stdout


needs_tools = pytest.mark.skipif(not _has_tools(), reason="git-filter-repo(2.47 이상)·gitleaks 없음")

OWNER = "owner@example.invalid"
PUBLIC = "12345+demo-owner@users.noreply.github.com"
TABLE = "# 가짜 표\nzorbix-bank\tdemo-bank\nquuxproj\tdemo-proj\nquuxproj-old\tdemo-proj-old\nzorbix-trig\tdemo-trig\n"
DENY = "# 가짜 식별 패턴\n(?i)plughwort\n"
MAILMAP = f"Demo Owner <{PUBLIC}> Owner Real <{OWNER}>\n"
# 가짜 비밀은 실행 때 이어 붙인다(이 파일 자체가 gitleaks 에 걸리지 않게).
FAKE_SECRET = "api" + "_key = " + '"QZX-' + "Zx9Kq2Lm8Np4" + "Rt6Vw1Ys3Bc5Df7Gh0Jk" + '"'
AWS_SECRET = "aws = " + '"' + "AKIA" + "Z7Q3XK2LMRPV4WND" + '"'
GITLEAKS = """[extend]
useDefault = true

[[allowlists]]
description = "가짜 비밀"
targetRules = ["generic-api-key"]
condition = "AND"
regexTarget = "line"
paths = ['''^tests/test_fake\\.py$''']
regexes = ['''"QZX-[A-Za-z0-9]+"''']
"""
BASE_MANIFEST = {
    "include": ["src/", "docs/", "tests/", "policies/", ".gitleaks.toml", "README.md"],
    "exclude_history": [{"path": "ops/", "reason": "r"}, {"path": "tests/fixtures/policies/b?/", "reason": "r"}],
    "history_rewrite_paths": ["docs/", "tests/", "policies/bank-policies.json", "policies/context-triggers.json"],
    "deny_patterns": [{"id": "w", "regex": "for[b]idden"}],
    "accept": [],
    "message_rewrites": [{"regex": "for[b]idden", "replace": "fine"}],
    "public_words": ["aside", "hindsight-crew"],        # 픽스처 project 어휘의 공개 식별자
}


def _env(**extra) -> dict:
    env = dict(os.environ)
    env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_COMMITTER_NAME": "Tagger", "GIT_COMMITTER_EMAIL": "tagger@example.invalid"})
    env.update(extra)
    return env


def git(repo, *args, env=None) -> str:
    r = subprocess.run(["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", *args],
                       capture_output=True, env=env or _env(), check=True)
    return r.stdout.decode()


class Src:
    """날짜를 고정해 커밋하는 원본 리포(같은 조작이면 같은 sha)."""

    def __init__(self, path: Path):
        self.path = path
        self.n = 0
        path.mkdir()
        git(path, "init", "-q", "-b", "main")

    def commit(self, files: dict, msg: str, email: str = OWNER, name: str = "Owner Real",
               committer: tuple = None) -> str:
        for rel, text in files.items():
            p = self.path / rel
            if text is None:
                p.unlink()
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        git(self.path, "add", "-A")
        self.n += 1
        date = f"2020-01-{self.n:02d}T00:00:00+0000"
        cn, ce = committer or (name, email)
        env = _env(GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email, GIT_AUTHOR_DATE=date,
                   GIT_COMMITTER_NAME=cn, GIT_COMMITTER_EMAIL=ce, GIT_COMMITTER_DATE=date)
        git(self.path, "commit", "-q", "--allow-empty", "-m", msg, env=env)
        return git(self.path, "rev-parse", "HEAD").strip()

    def merge(self, branch: str, msg: str) -> str:
        self.n += 1
        date = f"2020-01-{self.n:02d}T00:00:00+0000"
        env = _env(GIT_AUTHOR_NAME="Owner Real", GIT_AUTHOR_EMAIL=OWNER, GIT_AUTHOR_DATE=date,
                   GIT_COMMITTER_NAME="GitHub", GIT_COMMITTER_EMAIL="noreply@github.com", GIT_COMMITTER_DATE=date)
        git(self.path, "merge", "-q", "--no-ff", branch, "-m", msg, env=env)
        return git(self.path, "rev-parse", "HEAD").strip()


def base_source(tmp_path: Path, *, b1_extra: dict = None, pre_split_trigger: str = "zorbix-trig") -> tuple:
    """(Src, split sha). 개인 정책 분리 커밋·웹 머지·trailer 가 있는 작은 이력."""
    src = Src(tmp_path / "src")
    b1 = {"banks": {"zorbix-bank": {}, **(b1_extra or {})}}
    src.commit({
        "policies/bank-policies.json": json.dumps({"banks": {
            "zorbix-bank": {"tag_aliases": {"project:quuxproj-old": "project:quuxproj", "kind:done": "kind:completion"},
                            "tag_vocabulary": {"project": ["quuxproj", "aside", "hindsight-crew"]}},
            "demo-bank": {}}}),
        "policies/context-triggers.json": json.dumps({"triggers": [{"id": pre_split_trigger, "bank": "zorbix-bank"}]}),
        "tests/fixtures/policies/b1/bank-policies.json": json.dumps(b1),
        "tests/fixtures/policies/sample/bank-policies.json": json.dumps(
            {"banks": {"demo-bank": {"tag_aliases": {"kind:done": "kind:completion"}}}}),
        "docs/notes.md": "zorbix-bank notes, forbidden word, quuxproj-old and quuxproj.\n",
        "src/app.py": "print('hello')\n",
        "ops/run.log": "plughwort zorbix-bank\n",
        "notes.txt": "zorbix-bank\n",
        ".gitleaks.toml": GITLEAKS,
        "tests/test_fake.py": FAKE_SECRET + "  # zorbix-bank\n",
    }, "init forbidden zorbix-bank\n\nbody\n\nCo-authored-by: Helper <helper@example.invalid>\n")
    split = src.commit({"policies/bank-policies.json": json.dumps({"banks": {"personal-alice": {}}}),
                        "policies/context-triggers.json": json.dumps({"triggers": []})}, "split policies")
    src.commit({"docs/notes.md": "zorbix-bank notes v2\n", "ops/run.log": "v2\n"}, "update docs")
    git(src.path, "checkout", "-q", "-b", "feature")
    src.commit({"src/app.py": "print('hi')\n"}, "feature change")
    git(src.path, "checkout", "-q", "main")
    src.merge("feature", "Merge pull request #1 from owner/feature")
    return src, split


def inputs(tmp_path: Path, table: str = TABLE, manifest: dict = None, deny: str = DENY, mailmap: str = MAILMAP) -> dict:
    d = tmp_path / "inputs"
    d.mkdir(exist_ok=True)
    (d / "deny.txt").write_text(deny, encoding="utf-8")
    (d / "table.tsv").write_text(table, encoding="utf-8")
    (d / "mailmap").write_text(mailmap, encoding="utf-8")
    (d / "manifest.json").write_text(json.dumps(manifest or BASE_MANIFEST), encoding="utf-8")
    return {"deny": d / "deny.txt", "table": d / "table.tsv", "mailmap": d / "mailmap", "manifest": d / "manifest.json"}


def run_export(src: Src, split: str, inp: dict, out: Path, *extra: str, env=None, home: Path = None):
    """--reviewed-tokens·--local-rules 를 주지 않은 호출은 픽스처 안 빈 파일을 넘긴다(기본 경로 ~/.config/... 의 실제
    사용자 파일을 읽지 않게). HOME 은 바꾸지 않는다(HOME 에 기대는 도구 해석이 깨지지 않게). 기본 경로 자체를 보는 테스트만
    home 을 준다."""
    env = dict(env or _env())
    if home is not None:
        env["HOME"] = str(home)
    else:
        if "--reviewed-tokens" not in extra:
            empty = Path(inp["table"]).parent / "reviewed-empty.tsv"
            empty.write_text("", encoding="utf-8")
            extra = (*extra, "--reviewed-tokens", str(empty))
        if "--local-rules" not in extra:
            rules = Path(inp["table"]).parent / "local-rules-empty.json"
            rules.write_text("{}", encoding="utf-8")
            extra = (*extra, "--local-rules", str(rules))
    cmd = [sys.executable, "-I", str(SCRIPT), "--repo", str(src.path), "--source-ref", "main", "--out", str(out),
           "--manifest", str(inp["manifest"]), "--local-deny", str(inp["deny"]),
           "--rename-table", str(inp["table"]), "--mailmap", str(inp["mailmap"]), "--split-rev", split, *extra]
    r = subprocess.run(cmd, capture_output=True, env=env, cwd=str(src.path))
    return r.returncode, r.stdout.decode(), r.stderr.decode()


def head_of(out: Path) -> str:
    return git(out / "repo", "rev-parse", "refs/heads/main").strip()


def assert_no_secrets(text: str) -> None:
    low = text.lower()
    for s in ("plughwort", "zorbix", "quuxproj", OWNER, "stranger", "unlisted", "lost-trig", "qvz"):
        assert s not in low, s


# ---------------------------------------------------------------- 단위

def test_help_lists_arguments():
    r = subprocess.run([sys.executable, "-I", str(SCRIPT), "--help"], capture_output=True, text=True)
    assert r.returncode == 0
    for arg in ("--source-ref", "--out", "--manifest", "--local-deny", "--rename-table", "--mailmap",
                "--prev-public", "--gitleaks-config", "--reviewed-tokens", "--apply"):
        assert arg in r.stdout


@pytest.mark.parametrize("msg, want", [
    ("subject\n", "subject\n\nExported-From: abc\n"),
    ("subject\n\nbody text\n", "subject\n\nbody text\n\nExported-From: abc\n"),
    ("s\n\nbody\n\nCo-authored-by: A <a@x>\n", "s\n\nbody\n\nCo-authored-by: A <a@x>\nExported-From: abc\n"),
    ("s\n\nCloses #1\n", "s\n\nCloses #1\n\nExported-From: abc\n"),
    ("s\n\nRejected: x | y\n  more\nCo-Authored-By: C <c@x>\n\n",
     "s\n\nRejected: x | y\n  more\nCo-Authored-By: C <c@x>\nExported-From: abc\n"),
])
def test_add_trailer(msg, want):
    assert ex.add_trailer(msg, "Exported-From", "abc") == want


def test_rewriter_names_first_then_message_rewrites_with_alnum_boundary():
    rw = ex.Rewriter([("forbiddenproj", "demo-p"), ("zorbix-bank", "demo-bank")],
                     [{"regex": "for[b]idden", "replace": "fine"}], ["docs/"])
    assert rw.text("forbiddenproj and forbidden") == "demo-p and fine"
    # 밑줄·하이픈은 경계, 영숫자에 붙으면 치환하지 않는다(그 잔존은 독립 유출 검사가 잡는다)
    assert rw.text("zorbix-bank-x zorbix-bank_x xzorbix-bank zorbix-bank2") == \
        "demo-bank-x demo-bank_x xzorbix-bank zorbix-bank2"
    assert rw.data(b"\xff zorbix-bank") == b"\xff demo-bank"
    assert rw.in_scope("docs/a.md") and not rw.in_scope("src/a.py")


def test_mailmap_targets_and_noreply():
    targets = {PUBLIC.lower()}
    assert ex.email_allowed(PUBLIC, targets)
    assert ex.email_allowed("noreply@github.com", targets)
    assert not ex.email_allowed(OWNER, targets)


# ---------------------------------------------------------------- 도구 부재·실행 오류 (대역)

def _fake_bin(tmp_path: Path, *, filter_repo: str = None, gitleaks: str = None, real_filter_repo=False) -> dict:
    """git 만 있는 PATH. filter_repo/gitleaks 는 대역 스크립트 본문(sh)."""
    exec_path = subprocess.run(["git", "--exec-path"], capture_output=True, text=True).stdout.strip()
    if (Path(exec_path) / "git-filter-repo").exists():
        pytest.skip("git exec-path 에 git-filter-repo 가 있어 부재를 만들 수 없음")
    b = tmp_path / "bin"
    b.mkdir()
    (b / "git").symlink_to(shutil.which("git"))
    for name, body in (("git-filter-repo", filter_repo), ("gitleaks", gitleaks)):
        if body is not None:
            (b / name).write_text("#!/bin/sh\n" + body, encoding="utf-8")
            (b / name).chmod(0o755)
    if real_filter_repo:
        (b / "git-filter-repo").symlink_to(shutil.which("git-filter-repo"))
    dirs = [str(b)] + [d for d in ("/usr/bin", "/bin")
                       if not (Path(d) / "git-filter-repo").exists() and not (Path(d) / "gitleaks").exists()]
    return _env(PATH=os.pathsep.join(dirs))


FR_OK = 'case "$1" in --version) echo a40bce548d2c;; -h) echo "  --file-info-callback FUNCTION_BODY_OR_FILE";; *) exit 3;; esac\n'
FR_OLD = 'case "$1" in --version) echo 0123456789ab;; -h) echo "  --blob-callback FUNCTION_BODY_OR_FILE";; *) exit 3;; esac\n'
GL_OK = 'case "$1" in version) echo 8.30.1;; *) exit 0;; esac\n'
GL_FAIL = 'case "$1" in version) echo 8.30.1;; *) exit 2;; esac\n'


@pytest.mark.parametrize("case, want", [
    ("filter-repo 없음", "git filter-repo 없음"),
    ("filter-repo 2.47 미만", "2.47 이상 필요"),
    ("filter-repo 실행 오류", "git filter-repo 실행 오류"),
    ("gitleaks 없음", "gitleaks 없음"),
    ("gitleaks 실행 오류", "gitleaks 실행 오류"),
])
def test_missing_or_failing_tools_fail(tmp_path, case, want):
    if case == "gitleaks 실행 오류" and not shutil.which("git-filter-repo"):
        pytest.skip("실제 git-filter-repo 필요")
    fakes = {
        "filter-repo 없음": dict(gitleaks=GL_OK),
        "filter-repo 2.47 미만": dict(filter_repo=FR_OLD, gitleaks=GL_OK),
        "filter-repo 실행 오류": dict(filter_repo=FR_OK, gitleaks=GL_OK),
        "gitleaks 없음": dict(filter_repo=FR_OK),
        "gitleaks 실행 오류": dict(gitleaks=GL_FAIL, real_filter_repo=True),
    }[case]
    env = _fake_bin(tmp_path, **fakes)
    src, split = base_source(tmp_path)
    rc, out, err = run_export(src, split, inputs(tmp_path), tmp_path / "out", env=env)
    assert rc == 2, (out, err)
    assert want in err
    if case == "filter-repo 실행 오류":                        # 거르기 전 원본 이력이 --out 에 남지 않는다
        assert not (tmp_path / "out" / "repo").exists()


# ---------------------------------------------------------------- 내보내기 (실제 도구)

@needs_tools
def test_same_input_twice_same_head_and_all_checks_pass(tmp_path):
    src, split = base_source(tmp_path)
    inp = inputs(tmp_path)
    rc1, out1, err1 = run_export(src, split, inp, tmp_path / "o1")
    rc2, out2, _ = run_export(src, split, inp, tmp_path / "elsewhere" / "o2")
    assert rc1 == 0, (out1, err1)
    assert rc2 == 0, out2
    assert head_of(tmp_path / "o1") == head_of(tmp_path / "elsewhere" / "o2")
    assert "[FAIL]" not in out1
    for name in ("주소 집합", "독립 유출 검사", "표 누락 검사", "제외 경로", "gitleaks", "Exported-From"):
        assert f"[PASS] {name}" in out1
    assert_no_secrets(out1 + err1)

    repo = tmp_path / "o1" / "repo"
    paths = set(git(repo, "log", "--format=", "--name-only", "refs/heads/main").split())
    assert not any(p.startswith(("ops/", "tests/fixtures/policies/b1/")) or p == "notes.txt" for p in paths)
    assert git(repo, "show", "refs/heads/main:src/app.py") == "print('hi')\n"
    first = git(repo, "rev-list", "--max-parents=0", "refs/heads/main").strip()
    assert git(repo, "show", f"{first}:docs/notes.md") == "demo-bank notes, fine word, demo-proj-old and demo-proj.\n"
    emails = set(git(repo, "log", "--format=%ae%n%ce", "refs/heads/main").split())
    assert emails == {PUBLIC, "noreply@github.com"}
    assert git(repo, "for-each-ref", "--format=%(refname)").split() == ["refs/heads/main"]


@needs_tools
def test_trailers_preserved_and_every_commit_exported_from_matches(tmp_path):
    src, split = base_source(tmp_path)
    rc, out, err = run_export(src, split, inputs(tmp_path), tmp_path / "o")
    assert rc == 0, (out, err)
    repo = tmp_path / "o" / "repo"
    source = set(git(src.path, "rev-list", "main").split())
    cmap = {}
    for line in (repo / ".git" / "filter-repo" / "commit-map").read_text().splitlines()[1:]:
        old, new = line.split()
        cmap[new] = old
    commits = git(repo, "rev-list", "refs/heads/main").split()
    assert len(commits) == 5
    for c in commits:
        msg = git(repo, "log", "-1", "--format=%B", c)
        parsed = subprocess.run(["git", "interpret-trailers", "--parse"], input=msg, capture_output=True,
                                text=True, check=True).stdout
        vals = [x.split(": ", 1)[1] for x in parsed.splitlines() if x.startswith("Exported-From: ")]
        assert vals == [cmap[c]] and vals[0] in source
    first = git(repo, "rev-list", "--max-parents=0", "refs/heads/main").strip()
    msg = git(repo, "log", "-1", "--format=%B", first)
    parsed = subprocess.run(["git", "interpret-trailers", "--parse"], input=msg, capture_output=True,
                            text=True, check=True).stdout
    assert parsed.splitlines()[0] == "Co-authored-by: Helper <helper@example.invalid>"
    assert msg.startswith("init fine demo-bank\n")
    assert "[PASS] Exported-From: 불일치 0 / 커밋 5" in out


@needs_tools
def test_comment_body_has_range_head_and_checks(tmp_path):
    src, split = base_source(tmp_path)
    rc, out, _ = run_export(src, split, inputs(tmp_path), tmp_path / "o")
    assert rc == 0, out
    body = (tmp_path / "o" / "comment.md").read_text(encoding="utf-8")
    sha = git(src.path, "rev-parse", "main").strip()
    assert f"원본 범위: 처음..{sha}" in body
    assert f"공개 HEAD: {head_of(tmp_path / 'o')}" in body
    for name in ("주소 집합", "독립 유출 검사", "표 누락 검사", "제외 경로", "gitleaks", "Exported-From", "조상 검사"):
        assert f"| {name} |" in body
    assert "매니페스트 sha256:" in body
    assert_no_secrets(body)
    result = json.loads((tmp_path / "o" / "result.json").read_text(encoding="utf-8"))
    assert result["ok"] is True and result["public_head"] == head_of(tmp_path / "o")


@needs_tools
def test_appended_source_commit_keeps_previous_public_head_as_ancestor(tmp_path):
    src, split = base_source(tmp_path)
    inp = inputs(tmp_path)
    assert run_export(src, split, inp, tmp_path / "o1")[0] == 0
    prev = head_of(tmp_path / "o1")
    src.commit({"docs/more.md": "zorbix-bank more\n"}, "more docs")
    rc, out, err = run_export(src, split, inp, tmp_path / "o2", "--prev-public", str(tmp_path / "o1" / "repo"))
    assert rc == 0, (out, err)
    assert "[PASS] 조상 검사" in out
    new = head_of(tmp_path / "o2")
    assert new != prev
    git(tmp_path / "o1" / "repo", "fetch", "-q", str(tmp_path / "o2" / "repo"), "main:refs/check/new")
    git(tmp_path / "o1" / "repo", "merge-base", "--is-ancestor", prev, new)
    assert git(tmp_path / "o2" / "repo", "for-each-ref", "--format=%(refname)").split() == ["refs/heads/main"]


@needs_tools
def test_rule_change_breaks_ancestry_and_names_force_push_owner(tmp_path):
    src, split = base_source(tmp_path)
    assert run_export(src, split, inputs(tmp_path), tmp_path / "o1")[0] == 0
    changed = TABLE.replace("zorbix-bank\tdemo-bank", "zorbix-bank\tother-bank")
    rc, out, _ = run_export(src, split, inputs(tmp_path, table=changed), tmp_path / "o2",
                            "--prev-public", str(tmp_path / "o1" / "repo"))
    assert rc == 1
    assert "[FAIL] 조상 검사" in out
    assert ex.ANCESTRY_FAIL_MSG in out
    assert "규칙 변경으로 이력이 바뀜 - force push는 공개판 관리 주체(에이전트)가 관문 통과 뒤 실행하고 운영자에게 보고" in out


def _leak_case(src: Src, case: str) -> None:
    if case == "과거 blob 의 식별 금칙":
        src.commit({"src/tmp.txt": "x plughwort y\n"}, "add tmp")
        src.commit({"src/tmp.txt": None}, "remove tmp")
    elif case == "과거 커밋 메시지의 식별 금칙":
        src.commit({"src/app.py": "print('again')\n"}, "touch app for plughwort")
    elif case == "과거 비밀":
        src.commit({"src/old.txt": AWS_SECRET + "\n"}, "add old")
        src.commit({"src/old.txt": None}, "remove old")
    elif case == "치환 경로 밖 표 이름":
        src.commit({"src/app.py": "bank = 'zorbix-bank'\n"}, "name in src")
    elif case == "매핑 밖 주소":
        src.commit({"src/app.py": "print('x')\n"}, "by stranger", email="stranger@example.invalid", name="Stranger")


@needs_tools
@pytest.mark.parametrize("case, check", [
    ("과거 blob 의 식별 금칙", "독립 유출 검사"),
    ("과거 커밋 메시지의 식별 금칙", "독립 유출 검사"),
    ("과거 비밀", "gitleaks"),
    ("치환 경로 밖 표 이름", "독립 유출 검사"),
    ("매핑 밖 주소", "주소 집합"),
])
def test_history_leaks_fail(tmp_path, case, check):
    src, split = base_source(tmp_path)
    _leak_case(src, case)
    rc, out, err = run_export(src, split, inputs(tmp_path), tmp_path / "o")
    assert rc == 1, (out, err)
    assert f"[FAIL] {check}" in out
    assert_no_secrets(out + err + (tmp_path / "o" / "comment.md").read_text(encoding="utf-8"))


@needs_tools
@pytest.mark.parametrize("handling, want_rc", [("export-rewrite", 1), ("keep", 0)])
def test_export_rewrite_accept_is_not_applied_after_export(tmp_path, handling, want_rc):
    src, split = base_source(tmp_path)
    src.commit({"src/app.py": "word = 'forbidden'\n"}, "general word in src")
    manifest = dict(BASE_MANIFEST, accept=[{"path": "src/app.py", "pattern": "w", "handling": handling, "reason": "r"}])
    rc, out, _ = run_export(src, split, inputs(tmp_path, manifest=manifest), tmp_path / "o")
    assert rc == want_rc, out
    assert ("[FAIL] 독립 유출 검사" in out) == (want_rc == 1)


@needs_tools
@pytest.mark.parametrize("kind", ["옛 fixture", "분리 전 정책 파일"])
def test_name_missing_from_table_fails_with_count_only(tmp_path, kind):
    if kind == "옛 fixture":
        src, split = base_source(tmp_path, b1_extra={"unlisted-bank": {}})
    else:
        src, split = base_source(tmp_path, pre_split_trigger="lost-trig")
    rc, out, err = run_export(src, split, inputs(tmp_path), tmp_path / "o")
    assert rc == 1, out
    assert "[FAIL] 표 누락 검사: 표에 없는 값 1 /" in out
    assert_no_secrets(out + err)


@needs_tools
def test_secret_allowed_in_source_stays_allowed_after_rewrite(tmp_path):
    src, split = base_source(tmp_path)
    rc, out, _ = run_export(src, split, inputs(tmp_path), tmp_path / "o")
    assert rc == 0, out
    line = git(tmp_path / "o" / "repo", "show", "refs/heads/main:tests/test_fake.py")
    assert line == FAKE_SECRET + "  # demo-bank\n"           # 치환된 줄인데도 허용이 유지된다
    assert "[PASS] gitleaks: 0" in out
    # 대조: 허용 없는 기본 규칙만이면 같은 줄이 걸린다
    plain = tmp_path / "plain.toml"
    plain.write_text("[extend]\nuseDefault = true\n", encoding="utf-8")
    rc2, out2, _ = run_export(src, split, inputs(tmp_path), tmp_path / "o2", "--gitleaks-config", str(plain))
    assert rc2 == 1 and "[FAIL] gitleaks: 1" in out2


# ---------------------------------------------------------------- 제외 경로 (별도 픽스처)

def _exclusion_source(tmp_path: Path) -> tuple:
    src = Src(tmp_path / "src")
    root = src.commit({"src/a.py": "a\n", "ops/x.log": "x\n", "stray.txt": "s\n", ".gitleaks.toml": GITLEAKS,
                       "tests/fixtures/policies/b2/bank-policies.json": "{}"}, "root")
    src.commit({"ops/x.log": None, "src/a.py": "b\n"}, "drop ops")
    return src, root


def test_exclusion_check_counts_remaining_paths(tmp_path):
    src, _ = _exclusion_source(tmp_path)
    m = ex.cm.load_manifest(inputs(tmp_path)["manifest"])
    assert ex.exclusion_counts(src.path, m, "main") == (2, 2)    # ops/x.log, b2 fixture / ops/x.log, stray.txt


@needs_tools
def test_excluded_paths_removed_from_whole_history(tmp_path):
    src, root = _exclusion_source(tmp_path)
    rc, out, err = run_export(src, root, inputs(tmp_path), tmp_path / "o")
    assert rc == 0, (out, err)
    assert "[PASS] 제외 경로: exclude_history 경로 0, include 밖 경로 0" in out
    paths = set(git(tmp_path / "o" / "repo", "log", "--format=", "--name-only", "refs/heads/main").split())
    assert paths == {"src/a.py", ".gitleaks.toml"}


# ---------------------------------------------------------------- --apply (origin 은 로컬 bare)

@needs_tools
def test_apply_tags_next_number_and_pushes_to_origin_only(tmp_path):
    src, split = base_source(tmp_path)
    origin = tmp_path / "origin.git"
    public = tmp_path / "public.git"
    git(tmp_path, "init", "-q", "--bare", str(origin))
    git(tmp_path, "init", "-q", "--bare", str(public))
    git(src.path, "remote", "add", "origin", str(origin))
    git(src.path, "remote", "add", "public", str(public))
    git(src.path, "tag", "-a", "export/001", split, "-m", "first")
    git(src.path, "push", "-q", "origin", "main", "refs/tags/export/001")
    sha = git(src.path, "rev-parse", "main").strip()

    rc, out, err = run_export(src, split, inputs(tmp_path), tmp_path / "o", "--apply")
    assert rc == 0, (out, err)
    assert git(origin, "rev-parse", "export/002^{commit}").strip() == sha
    assert git(origin, "cat-file", "-t", "export/002").strip() == "tag"
    assert git(public, "for-each-ref", "--format=%(refname)").strip() == ""
    body = (tmp_path / "o" / "comment.md").read_text(encoding="utf-8")
    assert f"원본 범위: export/001 {split}..{sha}" in body
    assert "공개판 내보내기 export/002" in body

    # 다음 번호가 origin 에 이미 있으면 실패하고 로컬 태그를 남기지 않는다
    git(origin, "tag", "export/003", sha)
    rc2, out2, err2 = run_export(src, split, inputs(tmp_path), tmp_path / "o2", "--apply")
    assert rc2 == 2 and "이미 있음" in err2, (out2, err2)
    assert git(src.path, "tag", "-l", "export/003").strip() == ""


@needs_tools
def test_dry_run_creates_no_tag(tmp_path):
    src, split = base_source(tmp_path)
    before = git(src.path, "for-each-ref")
    rc, out, _ = run_export(src, split, inputs(tmp_path), tmp_path / "o")
    assert rc == 0 and "dry-run" in out
    assert git(src.path, "for-each-ref") == before


# ---------------------------------------------------------------- 경로 이름 치환

@needs_tools
def test_path_names_in_rewrite_paths_are_renamed_including_deletions(tmp_path):
    src, split = base_source(tmp_path)
    src.commit({"docs/zorbix-bank-plan.md": "plan\n", "docs/quuxproj-notes.md": "see zorbix-bank-plan.md\n"},
               "add named docs")
    src.commit({"docs/zorbix-bank-plan.md": None}, "drop plan")
    rc, out, err = run_export(src, split, inputs(tmp_path), tmp_path / "o")
    assert rc == 0, (out, err)
    repo = tmp_path / "o" / "repo"
    head_paths = set(git(repo, "ls-tree", "-r", "--name-only", "refs/heads/main").split())
    assert "docs/demo-proj-notes.md" in head_paths
    assert "docs/demo-bank-plan.md" not in head_paths          # 치환된 이름에도 삭제가 걸린다
    hist = set(git(repo, "log", "--format=", "--name-only", "refs/heads/main").split())
    assert "docs/demo-bank-plan.md" in hist and not any("zorbix" in p or "quuxproj" in p for p in hist)
    assert git(repo, "show", "refs/heads/main:docs/demo-proj-notes.md") == "see demo-bank-plan.md\n"


@needs_tools
def test_path_name_outside_rewrite_paths_fails_leak_check(tmp_path):
    src, split = base_source(tmp_path)
    src.commit({"src/zorbix-bank.py": "x = 1\n"}, "named module")
    rc, out, err = run_export(src, split, inputs(tmp_path), tmp_path / "o")
    assert rc == 1, out
    assert "[FAIL] 독립 유출 검사" in out and "path 1" in out
    assert_no_secrets(out + err)


# ---------------------------------------------------------------- final-review 반영 (#426 fr-fix)

def test_missing_manifest_is_usage_error_without_traceback(tmp_path):
    env = _fake_bin(tmp_path, filter_repo=FR_OK, gitleaks=GL_OK)
    src, split = base_source(tmp_path)
    inp = inputs(tmp_path)
    inp["manifest"] = tmp_path / "nope" / "manifest.json"
    rc, out, err = run_export(src, split, inp, tmp_path / "o", env=env)
    assert rc == 2, (out, err)
    assert "오류: 입력을 읽지 못함(FileNotFoundError)" in err
    assert "Traceback" not in err


def test_out_inside_source_worktree_is_rejected(tmp_path):
    env = _fake_bin(tmp_path, filter_repo=FR_OK, gitleaks=GL_OK)
    src, split = base_source(tmp_path)
    rc, out, err = run_export(src, split, inputs(tmp_path), src.path / "export-out", env=env)
    assert rc == 2 and "운영 리포 작업 트리 밖" in err, (out, err)
    assert not (src.path / "export-out").exists()


def test_rename_clash_in_same_tree_fails(tmp_path):
    env = _fake_bin(tmp_path, filter_repo=FR_OK, gitleaks=GL_OK)
    src, split = base_source(tmp_path)
    src.commit({"docs/zorbix-bank-plan.md": "a\n", "docs/demo-bank-plan.md": "b\n"}, "both names")
    rc, out, err = run_export(src, split, inputs(tmp_path), tmp_path / "o", env=env)
    assert rc == 2 and "같은 커밋 안의 서로 다른 경로가 겹친다" in err, (out, err)


@needs_tools
def test_rename_clash_only_across_time_passes(tmp_path):
    src, split = base_source(tmp_path)
    src.commit({"docs/zorbix-bank-plan.md": "plan\n"}, "named plan")
    git(src.path, "mv", "docs/zorbix-bank-plan.md", "docs/demo-bank-plan.md")
    src.commit({}, "rename to demo name")
    rc, out, err = run_export(src, split, inputs(tmp_path), tmp_path / "o")
    assert rc == 0, (out, err)
    head_paths = git(tmp_path / "o" / "repo", "ls-tree", "-r", "--name-only", "refs/heads/main").split()
    assert "docs/demo-bank-plan.md" in head_paths


@needs_tools
def test_secret_added_only_in_merge_commit_fails_gitleaks(tmp_path):
    src, split = base_source(tmp_path)
    git(src.path, "checkout", "-q", "-b", "side")
    src.commit({"src/side.py": "x = 1\n"}, "side change")
    git(src.path, "checkout", "-q", "main")
    src.commit({"src/main2.py": "y = 2\n"}, "main change")
    src.n += 1
    date = f"2020-01-{src.n:02d}T00:00:00+0000"
    env = _env(GIT_AUTHOR_NAME="Owner Real", GIT_AUTHOR_EMAIL=OWNER, GIT_AUTHOR_DATE=date,
               GIT_COMMITTER_NAME="Owner Real", GIT_COMMITTER_EMAIL=OWNER, GIT_COMMITTER_DATE=date)
    git(src.path, "merge", "-q", "--no-ff", "--no-commit", "side", env=env)
    (src.path / "src" / "evil.txt").write_text(AWS_SECRET + "\n", encoding="utf-8")   # 충돌 해결처럼 병합에서만 들어온다
    git(src.path, "add", "-A")
    git(src.path, "commit", "-q", "-m", "merge side", env=env)
    rc, out, err = run_export(src, split, inputs(tmp_path), tmp_path / "o")
    assert rc == 1, (out, err)
    assert "[FAIL] gitleaks" in out


@needs_tools
def test_existing_exported_from_line_in_source_message_still_matches(tmp_path):
    src, split = base_source(tmp_path)
    src.commit({"docs/x.md": "x\n"}, "cherry-picked\n\nExported-From: abc1234")
    rc, out, err = run_export(src, split, inputs(tmp_path), tmp_path / "o")
    assert rc == 0, (out, err)
    assert "[PASS] Exported-From: 불일치 0" in out


@needs_tools
def test_self_mapped_row_fails_table_lint_with_row_number_only(tmp_path):
    """#439: 자기 매핑 행(이름==데모)은 유출 검사 예외가 아니라 표 검사(lint) 실패다(exit 2, 행 번호만). 공개 값은 public_words 로."""
    src, split = base_source(tmp_path)
    table = TABLE.replace("zorbix-trig\tdemo-trig", "zorbix-trig\tzorbix-trig")
    rc, out, err = run_export(src, split, inputs(tmp_path, table=table), tmp_path / "o")
    assert rc == 2, (out, err)
    assert "데모 안에 표 왼쪽 이름이 든 행(자기 매핑 포함): 5(왼쪽 이름 행 5)" in err
    assert_no_secrets(out + err)
    assert not (tmp_path / "o" / "repo").exists()


@needs_tools
def test_hit_path_masking_hides_names_as_case_insensitive_substrings(tmp_path):
    """#439: 출력 경로는 유출 검사와 같은 부분 문자열(대소문자 무시) 가림을 거친다. 자기 매핑이 아닌 표로 확인한다."""
    src, split = base_source(tmp_path)
    src.commit({"src/zorbix-trig.txt": "plughwort\n", "src/xZorbix-Trigs/a.txt": "plughwort\n"}, "leak in named path")
    rc, out, err = run_export(src, split, inputs(tmp_path), tmp_path / "o")
    assert rc == 1, out
    assert "유출 적중 blob 경로: src/<이름>.txt 1" in out
    assert "유출 적중 blob 경로: src/x<이름>s/a.txt 1" in out
    assert_no_secrets(out + err)


@needs_tools
def test_hit_path_matching_identity_pattern_is_hidden_whole(tmp_path):
    src, split = base_source(tmp_path)
    src.commit({"src/Plughwort-notes.txt": "plughwort\n"}, "leak in identity path")
    rc, out, err = run_export(src, split, inputs(tmp_path), tmp_path / "o")
    assert rc == 1, out
    assert "(경로 이름 자체가 적중, 비공개)" in out
    assert_no_secrets(out + err)


@needs_tools
def test_unreadable_policy_version_fails_table_check(tmp_path):
    src, split = base_source(tmp_path)
    src.commit({"tests/fixtures/policies/b1/bank-policies.json": "{broken"}, "half edit")
    src.commit({"tests/fixtures/policies/b1/bank-policies.json": "{}"}, "fix json")
    rc, out, err = run_export(src, split, inputs(tmp_path), tmp_path / "o")
    assert rc == 1, out
    assert "[FAIL] 표 누락 검사: 표에 없는 값 0" in out and "읽지 못한 판본 1" in out


# ---------------------------------------------------------------- #439 치환(영숫자 경계·꼴 보존)과 독립 유출 검사(부분 문자열) 분리

def _table(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "t.tsv"
    p.write_text(text, encoding="utf-8")
    return p


@pytest.mark.parametrize("text, want", [
    ("owner-x_bank", "demo-x_bank"),                 # 밑줄 접미
    ("bank_owner-x", "bank_demo-x"),                 # 밑줄 접두
    ("owner-x-bank owner-x.", "demo-x-bank demo-x."),
    ("OWNER-X", "DEMO-X"),                           # 전부 대문자
    ("Owner-x", "Demo-x"),                           # 첫 글자만 대문자
    ("oWner-X OwNER-x", "demo-x demo-x"),            # 그 밖: 표의 데모 그대로
    ("owner-x-old", "demo-old"),                     # 긴 이름 먼저
    ("OWNER-X-OLD_v2 Owner-x-old", "DEMO-OLD_v2 Demo-old"),
    ("xowner-x owner-xy owner-x9 9owner-x", "xowner-x owner-xy owner-x9 9owner-x"),   # 영숫자에 붙으면 그대로
])
def test_rewriter_alnum_boundary_case_shape_and_longest_first(text, want):
    rw = ex.Rewriter([("owner-x", "demo-x"), ("owner-x-old", "demo-old")], [], ["docs/"])
    assert rw.text(text) == want


@pytest.mark.parametrize("text, want", [("FOO", "BAR"), ("Foo", "Bar"), ("foo", "bar"), ("fOO", "Bar")])
def test_explicit_row_wins_then_first_row_with_case_shape(tmp_path, text, want):
    rows = ex.load_rename_pairs(_table(tmp_path, "Foo\tBar\nfoo\tbar\n"))
    assert rows == [(1, "Foo", "Bar"), (2, "foo", "bar")]
    assert ex.Rewriter([(n, d) for _, n, d in rows], [], []).text(f"x {text}_y") == f"x {want}_y"


@pytest.mark.parametrize("case, table, want", [
    ("키 충돌 데모 둘", "# c\nOwner-X\tdemo-a\nowner-x\tdemo-b\n", "데모가 둘 이상인 행: 2, 3"),
    ("데모 안에 다른 왼쪽 이름", "owner-x\tdemo-x\nowner-y\tthe-OWNER-X-demo\n", "든 행(자기 매핑 포함): 2(왼쪽 이름 행 1)"),
    ("데모 = 다른 왼쪽", "owner-x\towner-y\nowner-y\tdemo-y\n", "든 행(자기 매핑 포함): 1(왼쪽 이름 행 2)"),
    ("자기 매핑", "owner-x\towner-x\n", "든 행(자기 매핑 포함): 1(왼쪽 이름 행 1)"),
    ("데모 안에 자기 이름", "owner-x\towner-x-demo\n", "든 행(자기 매핑 포함): 1(왼쪽 이름 행 1)"),
    ("빈 데모", "owner-x\n\nowner-y\tdemo-y\n", "데모 이름이 빈 행: 1"),
])
def test_table_lint_fails_with_row_numbers_only(tmp_path, case, table, want):
    with pytest.raises(ex.ToolError) as e:
        ex.load_rename_pairs(_table(tmp_path, table))
    msg = str(e.value)
    assert want in msg, msg
    assert "owner" not in msg.lower() and "demo" not in msg.lower()


def test_table_lint_allows_same_key_same_demo_and_skips_public_words(tmp_path):
    t = _table(tmp_path, "Foo\tBar\nfoo\tbar\nfoo\tbar\ngithub\tgithub\nGitHub-owner\tdemo-gh\n")
    rows = ex.load_rename_pairs(t, frozenset({"github"}))
    # 정확히 같은 행은 하나로, public_words 와 같은 이름(자기 매핑이라도)은 빠진다. 그 단어를 품은 다른 이름은 남는다.
    assert rows == [(1, "Foo", "Bar"), (2, "foo", "bar"), (5, "GitHub-owner", "demo-gh")]
    rw = ex.Rewriter([(n, d) for _, n, d in rows], [], [])
    assert rw.text("github GITHUB-OWNER_x") == "github DEMO-GH_x"


def test_path_names_commit_messages_and_trailers_use_the_same_rules(tmp_path):
    m = ex.cm.load_manifest(inputs(tmp_path)["manifest"])
    rw = ex.Rewriter([("zorbix-bank", "demo-bank")], m["message_rewrites"], m["history_rewrite_paths"])
    paths = ["docs/zorbix-bank_notes.md", "docs/ZORBIX-BANK.md", "docs/Zorbix-bank/a.md", "src/zorbix-bank_x.py",
             "docs/xzorbix-bank.md"]
    drop, renames = ex.path_changes(m, rw, paths)
    assert drop == []
    assert renames == [("docs/zorbix-bank_notes.md", "docs/demo-bank_notes.md"), ("docs/ZORBIX-BANK.md", "docs/DEMO-BANK.md"),
                       ("docs/Zorbix-bank/a.md", "docs/Demo-bank/a.md")]       # src/ 는 범위 밖, 붙은 이름은 그대로
    msg = b"Fix Zorbix-bank_x forbidden\n\nCo-authored-by: ZORBIX-BANK Bot <zorbix-bank_bot@example.invalid>\n"
    assert rw.data(msg) == b"Fix Demo-bank_x fine\n\nCo-authored-by: DEMO-BANK Bot <demo-bank_bot@example.invalid>\n"


def _scanner(tmp_path, **kw) -> "ex.Scanner":
    m = ex.cm.load_manifest(inputs(tmp_path)["manifest"])
    return ex.Scanner(m, [], [(2, "zorbix-bank"), (3, "quuxproj"), (4, "quuxproj-old")], set(), **kw)


@pytest.mark.parametrize("surface", ["blob", "path", "message"])
@pytest.mark.parametrize("text, want", [
    ("xzorbix-banky", {"rename-table:2": 1}),                   # 영숫자에 붙은 이름
    ("ZORBIX-BANK Zorbix-Bank_x", {"rename-table:2": 2}),       # 대소문자 변형
    ("aQuuxProj-OLDz", {"rename-table:3": 1, "rename-table:4": 1}),   # 경계 없음: 짧은 이름도 함께 센다
    ("demo-bank quux-proj", {}),
])
def test_scanner_counts_names_as_case_insensitive_substrings(tmp_path, surface, text, want):
    sc = _scanner(tmp_path)
    sc.text(surface, text)
    assert {lbl: n for (s, lbl), n in sc.counts.items() if s == surface} == want


@pytest.mark.parametrize("name, email, want", [
    ("Zorbix-Bank Owner", "12345+zorbix-bank@users.noreply.github.com", 0),       # mailmap 대상 이름·주소
    ("Zorbix-Bank Owner", "999+zorbix-bank@users.noreply.github.com", 1),         # 주소만 다른 신원
    ("Zorbix-Bank Helper", "12345+ZORBIX-BANK@users.noreply.github.com", 1),      # 이름만 다르다(주소는 대소문자 무시로 같음)
    ("zorbix-bank owner", "x@example.invalid", 1),                                # 이름은 정확히 같아야 뺀다
])
def test_scanner_ident_skips_only_exact_mailmap_targets(tmp_path, name, email, want):
    sc = _scanner(tmp_path, keep_names={"Zorbix-Bank Owner"}, keep_emails={"12345+zorbix-bank@users.noreply.github.com"})
    assert sc.ident(name, email) == want
    assert sc.total("ident") == want


def test_suggest_rows_wrap_hit_in_alnum_token_and_mark_short_names():
    table = ex.NameTable([("zorbix-bank", "demo-bank"), ("qvz", "demo-q")])
    tokens = {"xzorbix-banky", "XZORBIX-BANK9", "Xqvz9", "zorbix-bank", "QVZ", "a" * 70 + "qvz"}
    rows, too_long = ex.suggest_rows(tokens, table)
    assert rows == ["XZORBIX-BANK9\tXDEMO-BANK9", "Xqvz9\tXdemo-q9\tshort", "xzorbix-banky\txdemo-banky"]
    assert too_long == 1                     # 표 이름과 같은 토큰(대소문자 무시)은 제안하지 않는다


def test_local_deny_patterns_ignore_case(tmp_path):
    p = tmp_path / "deny.txt"
    p.write_text("plughwort\n", encoding="utf-8")
    (rx,) = ex.cm.load_local_deny(p)
    assert len(rx.findall("PLUGHWORT Plughwort plughwort")) == 3


@pytest.mark.parametrize("extra, missing", [
    ({"ZORBIX-BANK": {}}, 0),            # 표 이름의 대소문자 변형은 표에 있는 것으로 센다
    ({"Aside": {}}, 0),                  # public_words(대소문자 무시)
    ({"Unlisted-Bank": {}}, 1),
])
def test_table_missing_ignores_case(tmp_path, extra, missing):
    src, split = base_source(tmp_path, b1_extra=extra)
    sha = git(src.path, "rev-parse", "main").strip()
    left = {"zorbix-bank", "quuxproj", "quuxproj-old", "zorbix-trig"}
    n_missing, _, _, n_bad = ex.table_missing(src.path, sha, split, left, frozenset({"aside", "hindsight-crew"}))
    assert (n_missing, n_bad) == (missing, 0)


LEAK_MAILMAP = MAILMAP + f"<{PUBLIC}> <{OWNER}>\n"        # 이름은 그대로 두고 주소만 바꾸는 줄(작성자 이름 표면 확인용)


def _leak_on(src: Src, surface: str, v: str) -> None:
    if surface == "blob":
        src.commit({"src/app.py": f"x = '{v}'\n"}, "blob leak")
    elif surface == "path":
        src.commit({f"src/{v}.py": "x = 1\n"}, "path leak")
    elif surface == "message":
        src.commit({"src/app.py": "print(2)\n"}, f"touch {v}\n\nCo-authored-by: Helper <helper@example.invalid>")
    elif surface == "ident-name":
        src.commit({"src/app.py": "print(3)\n"}, "ident name", name=f"{v} Bot")
    elif surface == "ident-email":
        src.commit({"src/app.py": "print(4)\n"}, "ident email", name="Helper", email=f"999+{v}@users.noreply.github.com")


@needs_tools
@pytest.mark.parametrize("surface, v", [
    (s, v) for s in ("blob", "path", "message", "ident-name", "ident-email")
    for v in ("xzorbix-banky", "XZorbix-BANKy", "ZORBIX-BANK")
    if not (s == "message" and v == "ZORBIX-BANK")         # 메시지는 치환 범위라 경계 맞는 대문자는 바뀐다(아래 회귀 테스트)
])
def test_leak_check_finds_glued_and_case_variant_names_on_every_surface(tmp_path, surface, v):
    src, split = base_source(tmp_path)
    _leak_on(src, surface, v)
    rc, out, err = run_export(src, split, inputs(tmp_path, mailmap=LEAK_MAILMAP), tmp_path / "o")
    assert rc == 1, (out, err)
    label = surface.split("-")[0]
    line = next(x for x in out.splitlines() if x.startswith("[FAIL] 독립 유출 검사"))
    assert f"{label}/rename-table:2 " in line, line                 # 표 행 번호(가짜 표 2행)와 표면
    assert f" {label} 0" not in line
    assert "[PASS] 주소 집합" in out
    assert_no_secrets(out + err + (tmp_path / "o" / "comment.md").read_text(encoding="utf-8"))


@needs_tools
@pytest.mark.parametrize("trailer, label", [
    (f"Co-authored-by: Owner Real <{OWNER.upper()}>", "message/mailmap-source"),
    ("Co-authored-by: Plughwort <p@example.invalid>", "message/local-deny"),
])
def test_coauthor_trailer_addresses_are_checked(tmp_path, trailer, label):
    src, split = base_source(tmp_path)
    src.commit({"src/app.py": "print(5)\n"}, f"pair work\n\n{trailer}")
    rc, out, err = run_export(src, split, inputs(tmp_path), tmp_path / "o")
    assert rc == 1, (out, err)
    assert label in out
    assert_no_secrets(out + err)


@needs_tools
@pytest.mark.parametrize("other, want_rc", [(False, 0), (True, 1)])
def test_mailmap_target_identity_is_the_only_ident_exception(tmp_path, other, want_rc):
    src, split = base_source(tmp_path)
    if other:
        src.commit({"src/app.py": "print(6)\n"}, "by helper", name="Zorbix-Bank Helper",
                   email="999+zorbix-bank@users.noreply.github.com")
    mailmap = f"Zorbix-Bank Owner <12345+zorbix-bank@users.noreply.github.com> Owner Real <{OWNER}>\n"
    rc, out, err = run_export(src, split, inputs(tmp_path, mailmap=mailmap), tmp_path / "o")
    assert rc == want_rc, (out, err)
    if other:
        assert "ident/rename-table:2 2" in out                   # 다른 신원의 이름·주소
    else:
        assert "[PASS] 독립 유출 검사: 0 (blob 0, message 0, path 0, ident 0)" in out
    assert_no_secrets(out + err)


@needs_tools
def test_public_words_are_neither_rewritten_nor_scanned(tmp_path):
    src, split = base_source(tmp_path)
    manifest = dict(BASE_MANIFEST, public_words=["aside", "hindsight-crew", "zorbix-trig", "zorbix"])
    inp = inputs(tmp_path, manifest=manifest)
    rc, out, err = run_export(src, split, inp, tmp_path / "o")
    assert rc == 0, (out, err)
    assert "[PASS] 독립 유출 검사" in out and "[PASS] 표 누락 검사" in out
    repo = tmp_path / "o" / "repo"
    first = git(repo, "rev-list", "--max-parents=0", "refs/heads/main").strip()
    assert '"zorbix-trig"' in git(repo, "show", f"{first}:policies/context-triggers.json")   # 치환하지 않는다
    assert git(repo, "show", f"{first}:docs/notes.md").startswith("demo-bank notes")       # 단어를 품은 다른 이름은 치환
    src.commit({"src/app.py": "x = 'zorbix-bank'\n"}, "other name in src")
    rc2, out2, _ = run_export(src, split, inp, tmp_path / "o2")
    assert rc2 == 1 and "blob/rename-table:2 1" in out2                                   # 그대로 검사


@needs_tools
def test_local_deny_without_inline_flag_ignores_case(tmp_path):
    src, split = base_source(tmp_path)
    src.commit({"src/app.py": "x = 'PlughWORT'\n"}, "identity word in src")
    rc, out, err = run_export(src, split, inputs(tmp_path, deny="plughwort\n"), tmp_path / "o")
    assert rc == 1 and "blob/local-deny 1" in out, (out, err)
    assert_no_secrets(out + err)


@needs_tools
def test_suggest_rows_file_is_private_and_only_counted(tmp_path):
    src, split = base_source(tmp_path)
    src.commit({"src/app.py": "a = 'xzorbix-banky'\nb = 'Xqvz9'\nc = 'zorbix-bank'\n"}, "leaks")
    sug = tmp_path / "sug" / "rows.tsv"
    sug.parent.mkdir()
    sug.write_text("old\n", encoding="utf-8")
    sug.chmod(0o644)
    rc, out, err = run_export(src, split, inputs(tmp_path, table=TABLE + "qvz\tdemo-q\n"), tmp_path / "o",
                              "--suggest-rows", str(sug))
    assert rc == 1, (out, err)
    assert (sug.stat().st_mode & 0o777) == 0o600
    assert sug.read_text(encoding="utf-8").splitlines() == ["Xqvz9\tXdemo-q9\tshort", "xzorbix-banky\txdemo-banky"]
    assert "제안 행: 2 (긴 토큰 제외 0" in out
    assert_no_secrets(out + err)


@needs_tools
def test_suggest_rows_inside_source_worktree_is_rejected(tmp_path):
    src, split = base_source(tmp_path)
    rc, out, err = run_export(src, split, inputs(tmp_path), tmp_path / "o", "--suggest-rows", str(src.path / "new" / "s.tsv"))
    assert rc == 2 and "--suggest-rows 는 원본 리포 작업 트리 밖" in err, (out, err)
    assert not (src.path / "new").exists() and not (tmp_path / "o").exists()


REGRESSION_TEST = (
    'BANK = "zorbix-bank"\n'
    'KEY = "zorbix-bank_tokens"\n'            # 밑줄 접미 리터럴(#429 실패 1과 같은 꼴)
    'TITLE = "Zorbix-bank"\n'                 # 대문자 시작 표기(#429 실패 2와 같은 꼴)
    'CONST = "ZORBIX-BANK"\n'
    "def test_same_owner_forms():\n"
    '    assert KEY == BANK + "_tokens"\n'
    "    assert TITLE.lower() == BANK and CONST.lower() == BANK\n"
)


def _run_regression(tmp_path: Path, name: str, body: str) -> None:
    f = tmp_path / f"{name}.py"
    f.write_text(body, encoding="utf-8")
    sp = importlib.util.spec_from_file_location(name, f)
    mod = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(mod)
    mod.test_same_owner_forms()


@needs_tools
def test_underscore_suffix_and_capitalized_forms_stay_consistent_after_export(tmp_path):
    """#429 후보 pytest 실패 2건과 같은 꼴: 치환 뒤에도 같은 이름의 여러 꼴이 서로 맞는다."""
    src, split = base_source(tmp_path)
    src.commit({"tests/test_regress.py": REGRESSION_TEST},
               "Zorbix-bank_forms regression\n\nCo-authored-by: ZORBIX-BANK Bot <bot@example.invalid>")
    _run_regression(tmp_path, "regress_src", REGRESSION_TEST)        # 원본에서도 통과
    rc, out, err = run_export(src, split, inputs(tmp_path), tmp_path / "o")
    assert rc == 0, (out, err)
    assert "[PASS] 독립 유출 검사: 0" in out
    repo = tmp_path / "o" / "repo"
    body = git(repo, "show", "refs/heads/main:tests/test_regress.py")
    assert 'KEY = "demo-bank_tokens"' in body and 'TITLE = "Demo-bank"' in body and 'CONST = "DEMO-BANK"' in body
    _run_regression(tmp_path, "regress_out", body)                   # 치환 뒤에도 통과
    msg = git(repo, "log", "-1", "--format=%B", "refs/heads/main")
    assert msg.startswith("Demo-bank_forms regression\n") and "Co-authored-by: DEMO-BANK Bot <bot@example.invalid>" in msg



# ---------------------------------------------------------------- #439 final-review 반영 (fr-fix)

@needs_tools
@pytest.mark.parametrize("where", ["out 안 repo", "out 안 comment.md"])
def test_suggest_rows_inside_out_is_rejected(tmp_path, where):
    src, split = base_source(tmp_path)
    out = tmp_path / "o"
    target = out / "repo" / "s.tsv" if where == "out 안 repo" else out / "comment.md"
    rc, o, err = run_export(src, split, inputs(tmp_path), out, "--suggest-rows", str(target))
    assert rc == 2 and "--suggest-rows 는 --out 밖이어야 한다" in err, (o, err)
    assert not out.exists()


@needs_tools
@pytest.mark.parametrize("kind", ["상위 디렉터리 없음", "디렉터리"])
def test_suggest_rows_unwritable_location_fails_before_filtering(tmp_path, kind):
    src, split = base_source(tmp_path)
    target = tmp_path / "nope" / "s.tsv" if kind == "상위 디렉터리 없음" else tmp_path
    rc, o, err = run_export(src, split, inputs(tmp_path), tmp_path / "o", "--suggest-rows", str(target))
    assert rc == 2 and "있는 디렉터리 안의 파일 경로" in err, (o, err)
    assert not (tmp_path / "o").exists()                       # 거르기 전에 멈춘다


@needs_tools
@pytest.mark.parametrize("which", ["table", "deny", "mailmap", "manifest", "gitleaks", "reviewed"])
def test_suggest_rows_same_file_as_an_input_is_rejected(tmp_path, which):
    src, split = base_source(tmp_path)
    inp = inputs(tmp_path)
    gl = tmp_path / "inputs" / "gitleaks.toml"
    gl.write_text(GITLEAKS, encoding="utf-8")
    rv = tmp_path / "inputs" / "reviewed.tsv"
    rv.write_text("# 빈 검토 토큰\n", encoding="utf-8")
    target = {"gitleaks": gl, "reviewed": rv}.get(which) or inp[which]
    before = target.read_bytes()
    rc, o, err = run_export(src, split, inp, tmp_path / "o", "--gitleaks-config", str(gl),
                            "--reviewed-tokens", str(rv), "--suggest-rows", str(target))
    assert rc == 2 and "입력 파일과 같은 파일" in err, (o, err)
    assert target.read_bytes() == before


@pytest.mark.parametrize("text, want", [
    ("ZORBİX-BANK_v2", "DEMO-BANK_v2"),          # U+0130: re.I 로 맞고 lower() 로는 키가 안 된다
    ("zorbıx-bank", "demo-bank"),                # U+0131
    ("zorbix-banK", "demo-bank"),                # U+212A KELVIN SIGN
])
def test_unicode_case_folding_variants_are_rewritten(text, want):
    rw = ex.Rewriter([("zorbix-bank", "demo-bank")], [], [])
    assert rw.text(text) == want


@pytest.mark.parametrize("text", ["xZORBİX-BANKy", "zorbıx-bank9", "QUUXPROJ-OLD"])
def test_unicode_case_folding_variants_are_counted_by_leak_check(tmp_path, text):
    sc = _scanner(tmp_path)
    assert sc.text("blob", text) >= 1


def test_write_private_tightens_existing_file_before_writing(tmp_path, monkeypatch):
    f = tmp_path / "s.tsv"
    f.write_text("old\n", encoding="utf-8")
    f.chmod(0o644)
    seen = []
    real = ex.os.fdopen

    def spy(fd, *a, **kw):
        seen.append(ex.os.fstat(fd).st_mode & 0o777)           # 내용을 쓰기 직전의 권한
        return real(fd, *a, **kw)

    monkeypatch.setattr(ex.os, "fdopen", spy)
    ex.write_private(f, "new\n")
    assert seen == [0o600] and (f.stat().st_mode & 0o777) == 0o600
    assert f.read_text(encoding="utf-8") == "new\n"


@pytest.mark.parametrize("name, email, keep, want", [
    ("Plugh Real", "p@example.invalid", set(), 1),                                   # 이름·주소에 걸친 패턴
    ("Plugh Real", "p@example.invalid", {("Plugh Real", "p@example.invalid")}, 0),   # 대상 신원과 같은 결합 값
    ("Plugh Real", "p@other.invalid", {("Plugh Real", "p@example.invalid")}, 1),      # 주소만 다른 신원
])
def test_ident_local_deny_also_checks_joined_name_and_address(tmp_path, name, email, keep, want):
    m = ex.cm.load_manifest(inputs(tmp_path)["manifest"])
    import re as _re
    sc = ex.Scanner(m, [_re.compile(r"real <p@", _re.I)], [], set(), keep_pairs=keep)
    assert sc.ident(name, email) == want
    assert sc.counts.get(("ident", "local-deny"), 0) == want


def test_leak_check_finds_separator_variants_of_hyphenated_names(tmp_path):
    sc = _scanner(tmp_path)
    sc.text("blob", "zorbix_bank_tokens ZorbixBank zorbixbank9 Zorbix-Bank")
    assert sc.counts[("blob", "rename-table:2")] == 4                       # 같은 표 행으로 센다
    rows, _ = ex.suggest_rows(sc.tokens, ex.NameTable([("zorbix-bank", "demo-bank")]))
    # 토큰은 영숫자에서 끊긴다(밑줄은 새 치환 규칙의 경계라 'zorbix_bank' 행 하나가 '_tokens' 꼴도 덮는다)
    assert rows == ["ZorbixBank\tdemobank", "zorbix_bank\tdemo_bank", "zorbixbank9\tdemobank9"]
    # 치환은 원래 철자만 바꾼다(변형 치환은 표 소유자가 행으로 더한다)
    assert ex.Rewriter([("zorbix-bank", "demo-bank")], [], []).text("zorbix_bank zorbix-bank") == "zorbix_bank demo-bank"
    assert ex.Scanner.mask(sc, "src/zorbix_bank.py") == "src/<이름>.py"


@pytest.mark.parametrize("msg, want", [
    ("subject\n\nExported-From: 00beadff00beadff00beadff00beadff00beadff\n", 0),     # 도구가 더한 sha 줄만
    ("bead subject\n\nExported-From: 0011223344556677889900112233445566778899\n", 1),
    ("subject\n\nNote: bead\nExported-From: 00beadff00beadff00beadff00beadff00beadff\n", 1),
])
def test_exported_from_trailer_is_not_counted(tmp_path, msg, want):
    m = ex.cm.load_manifest(inputs(tmp_path)["manifest"])
    sc = ex.Scanner(m, [], [(7, "bead")], set())
    sc.text("message", ex.drop_exported_from(msg))
    assert sc.total("message") == want


# ---------------------------------------------------------------- #441 검토 토큰과 base64 해독 재검사 (가짜 이름·토큰·덩어리)

RT_NAMES = [(2, "zorbix-bank"), (3, "zib")]          # 'zib' 는 가짜 일반 단어 'zibbering' 안에 우연히 드는 짧은 이름
RT_NAMES_BER = RT_NAMES + [(4, "ber")]               # 같은 토큰 안에 다른 이름도 드는 경우


def _reviewed(tmp_path: Path, text: str, names=RT_NAMES):
    p = tmp_path / "reviewed.tsv"
    p.write_text(text, encoding="utf-8")
    return ex.load_reviewed_tokens(p, names)


def _rt_scanner(tmp_path: Path, reviewed: str = "", names=RT_NAMES, manifest=None, deny=(), sources=()):
    m = ex.cm.load_manifest(inputs(tmp_path, manifest=manifest)["manifest"])
    return ex.Scanner(m, list(deny), names, set(sources), reviewed=_reviewed(tmp_path, reviewed, names))


def _by_label(d: dict, surface: str) -> dict:
    return {lbl: n for (s, lbl), n in d.items() if s == surface}


@pytest.mark.parametrize("surface", ["blob", "message", "path", "ident"])
@pytest.mark.parametrize("case, names, reviewed, text, exempt, fail", [
    ("정확 일치", RT_NAMES, "zibbering\tzib\n", "a zibbering b", {"reviewed:3": 1}, {}),
    ("정확 일치 2회", RT_NAMES, "# 주석\nzibbering\tZIB\n", "zibbering, zibbering", {"reviewed:3": 2}, {}),
    ("대소문자 다름", RT_NAMES, "zibbering\tzib\n", "a Zibbering b", {}, {"rename-table:3": 1}),
    ("다른 이름 적중은 같은 토큰이어도", RT_NAMES_BER, "zibbering\tzib\n", "a zibbering b",
     {"reviewed:3": 1}, {"rename-table:4": 1}),
    ("토큰 = 이름", RT_NAMES, "zib\tzib\n", "a zib b", {}, {"rename-table:3": 1}),
    ("토큰 = 하이픈 이름의 변형", RT_NAMES, "zorbix_bank\tzorbix-bank\n", "a Zorbix_Bank zorbix_bank b",
     {}, {"rename-table:2": 2}),
    ("하이픈 이름 변형을 품은 토큰", RT_NAMES, "xzorbix_banky\tzorbix-bank\n", "a xzorbix_banky b", {"reviewed:2": 1}, {}),
    ("다른 토큰", RT_NAMES, "zibbering\tzib\n", "a zibberings b", {}, {"rename-table:3": 1}),
])
def test_reviewed_token_exempts_only_exact_token_same_name_on_blob_and_message(
        tmp_path, surface, case, names, reviewed, text, exempt, fail):
    sc = _rt_scanner(tmp_path, reviewed, names)
    n = sc.text(surface, text)
    got = (_by_label(sc.exempt, surface), _by_label(sc.counts, surface))
    if surface in ("blob", "message"):
        assert got == (exempt, fail)
    else:                                               # 경로·작성자 표면에는 면제가 없다
        moved = {lbl.replace("reviewed", "rename-table"): k for lbl, k in exempt.items()}
        assert got == ({}, {**fail, **moved})
    assert n == sum(got[1].values()) == sc.total()


def test_reviewed_token_never_applies_to_author_identity(tmp_path):
    sc = _rt_scanner(tmp_path, "zibbering\tzib\n")
    # 주소 속 토큰은 검토 토큰과 정확히 같아도, 작성자 표면이라 빼지 않는다(이름 1 + 주소 1)
    assert sc.ident("Zibbering Bot", "zibbering@example.invalid") == 2
    assert sc.counts == {("ident", "rename-table:3"): 2} and sc.exempt == {}


@pytest.mark.parametrize("text, want", [
    ("# c\nzibbering\tnope\n", "표에 없는 이름: 2"),
    ("zibbering\tzib\nbering\tzib\n", "이름을 포함하지 않는 토큰: 2"),
    ("x" * 62 + "zib\tzib\n", "64자 초과: 1"),
    ("zibbering\n\nzibbering\tzib\textra\n", "형식 오류: 1, 3"),
    ("zibbering\tnope\nbering\tzib\n" + "x" * 62 + "zib\tzib\n",
     "표에 없는 이름: 1; 이름을 포함하지 않는 토큰: 2; 64자 초과: 3"),
])
def test_reviewed_tokens_lint_fails_with_line_numbers_only(tmp_path, text, want):
    with pytest.raises(ex.ToolError) as e:
        _reviewed(tmp_path, text)
    msg = str(e.value)
    assert want in msg, msg
    assert "zib" not in msg.lower() and "nope" not in msg and "xxx" not in msg


def test_reviewed_tokens_lint_accepts_64_chars_and_hyphen_variants(tmp_path):
    rows = _reviewed(tmp_path, "x" * 61 + "zib\tzib\nxzorbix_banky\tZorbix-Bank\nzorbixbank9\tzorbix-bank\n")
    assert rows == [(1, "x" * 61 + "zib", "zib"), (2, "xzorbix_banky", "zorbix-bank"), (3, "zorbixbank9", "zorbix-bank")]


def test_unused_reviewed_lines_are_only_counted(tmp_path):
    sc = _rt_scanner(tmp_path, "zibbering\tzib\nzibber\tzib\nzibbering\tzib\n")
    sc.text("blob", "zibbering")
    sc.text("path", "zibber")                            # 경로 표면 적중은 줄을 쓴 것으로 치지 않는다
    assert sc.total() == 1 and sc.reviewed_unused() == 2          # 2행(경로만), 3행(1행과 같은 줄)


def test_reviewed_tokens_default_path_missing_is_empty(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    assert ex.reviewed_tokens_path(None, repo) is None                     # 기본 경로에 파일이 없으면 빈 목록
    with pytest.raises(ex.ToolError, match="--reviewed-tokens 파일 없음"):
        ex.reviewed_tokens_path(str(tmp_path / "nope.tsv"), repo)            # 명시한 경로가 없으면 오류
    inside = repo / "r.tsv"
    inside.write_text("", encoding="utf-8")
    with pytest.raises(ex.ToolError, match="리포 밖"):
        ex.reviewed_tokens_path(str(inside), repo)
    default = home / ".config" / "hindsight-crew" / "public-export" / "reviewed-tokens.tsv"
    default.parent.mkdir(parents=True)
    default.write_text("", encoding="utf-8")
    assert ex.reviewed_tokens_path(None, repo) == default.resolve()


def test_reviewed_tokens_default_path_broken_symlink_is_an_error(tmp_path, monkeypatch):
    """기본 경로가 깨진 심링크(대상이 옮겨짐)면 빈 목록으로 넘어가지 않고 오류(exit 2)다."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    default = home / ".config" / "hindsight-crew" / "public-export" / "reviewed-tokens.tsv"
    default.parent.mkdir(parents=True)
    default.symlink_to(tmp_path / "moved-away.tsv")
    with pytest.raises(ex.ToolError, match="깨진 심링크"):
        ex.reviewed_tokens_path(None, repo)


def _b64_with(make, names=("zib",), avoid=("zorbix", "quux", "ber", "plugh", "owner", "forb"), token_max=None):
    """make(i) 바이트를 base64 로 바꿔 덩어리가 names 를 (대소문자 무시) 품고 avoid 를 품지 않는 첫 i (결정적)."""
    for i in range(500000):
        raw = make(i)
        enc = base64.b64encode(raw).decode()
        low = enc.lower()
        if len(enc) <= 64 or not all(n in low for n in names) or any(a in low for a in avoid):
            continue
        if any(n in raw.decode("utf-8", "replace").lower() for n in names):
            continue                                     # 해독본에 가짜 이름이 우연히 들면 건너뛴다
        if token_max is not None and len(_alnum_token(enc, low.index("zib"))) > token_max:
            continue
        return enc
    raise AssertionError("가짜 덩어리를 찾지 못함")


def _alnum_token(text: str, at: int) -> str:
    s, e = at, at + 3
    while s > 0 and text[s - 1].isascii() and text[s - 1].isalnum():
        s -= 1
    while e < len(text) and text[e].isascii() and text[e].isalnum():
        e += 1
    return text[s:e]


_H_ALPH = ([chr(c) for c in range(0x20, 0x7f) if chr(c) not in '"\\'] + [chr(c) for c in range(0xA1, 0x100)]
           + [chr(0x4E00 + k * 37) for k in range(64)] + [chr(0x1F600 + k) for k in range(32)])


def _h(i: int) -> str:
    """덩어리 전체가 i 마다 달라지게 하는 가짜 12자(여러 바이트 길이의 UTF-8). 고정 문구만이면 base64 꼴이 좁아
    가짜 이름이 들지 않는다."""
    import hashlib
    return "".join(_H_ALPH[b % len(_H_ALPH)] for b in hashlib.sha256(str(i).encode()).digest()[:12])


B64_CASES = {
    "깨끗한 해독본": (lambda i: f'{{"n": "{_h(i)}", "body": "hello plain demo payload text"}}'.encode(), None),
    "해독본 이름": (lambda i: f'{{"n": "{_h(i)}", "body": "see zorbix-bank notes here"}}'.encode(), "rename-table:2"),
    "해독본 local-deny": (lambda i: f'{{"n": "{_h(i)}", "body": "note plughwort here and there"}}'.encode(), "local-deny"),
    "해독본 mailmap 원주소": (lambda i: f'{{"n": "{_h(i)}", "by": "{OWNER}", "x": "pad pad"}}'.encode(), "mailmap-source"),
    "해독본 deny_patterns": (lambda i: f'{{"n": "{_h(i)}", "body": "a forbidden word here"}}'.encode(), "deny_patterns"),
}


def _b64_scanner(tmp_path, reviewed: str = "", manifest=None):
    return _rt_scanner(tmp_path, reviewed, manifest=manifest, deny=[re.compile("plughwort", re.I)], sources={OWNER})


@pytest.mark.parametrize("case", list(B64_CASES))
def test_base64_chunk_is_decoded_and_rechecked_with_the_same_rules(tmp_path, case):
    make, label = B64_CASES[case]
    enc = _b64_with(make)
    sc = _b64_scanner(tmp_path)
    n_raw = len(re.findall("zib", enc, re.I))
    n = sc.text("blob", f'{{"bodyBase64": "{enc}"}}\n', "src/rec.json")
    if label is None:
        assert (sc.counts, _by_label(sc.exempt, "blob")) == ({}, {"encoded-checked:3": n_raw})
        assert n == 0
    else:                       # 해독본 적중은 기존 라벨로, 덩어리 안 원래 적중도 전부 실패
        assert (_by_label(sc.counts, "blob"), sc.exempt) == ({"rename-table:3": n_raw, label: 1}, {})
        assert n == n_raw + 1
    assert sc.tokens == set() or label is not None        # 면제된 적중은 표 보강 제안에 넣지 않는다


@pytest.mark.parametrize("accept, want_fail", [(False, True), (True, False)])
def test_decoded_text_uses_original_blob_path_for_deny_accept(tmp_path, accept, want_fail):
    enc = _b64_with(B64_CASES["해독본 deny_patterns"][0])
    acc = [{"path": "src/rec.json", "pattern": "w", "handling": "keep", "reason": "r"}] if accept else []
    sc = _b64_scanner(tmp_path, manifest=dict(BASE_MANIFEST, accept=acc))
    sc.text("blob", enc, "src/rec.json")
    assert ("deny_patterns" in _by_label(sc.counts, "blob")) == want_fail
    assert ("encoded-checked:3" in _by_label(sc.exempt, "blob")) == (not want_fail)


@pytest.mark.parametrize("case", ["길이 오류", "UTF-8 아님"])
def test_undecodable_chunk_hits_still_fail(tmp_path, case):
    if case == "길이 오류":
        enc = _b64_with(B64_CASES["깨끗한 해독본"][0]).rstrip("=")
        enc += "A" * ((1 - len(enc)) % 4)                          # 길이 % 4 == 1: 엄격 해독 실패
    else:
        enc = _b64_with(lambda i: b"\xff\xfe\xfd" + f" binary-ish payload {_h(i)} pad pad pad".encode())
    sc = _b64_scanner(tmp_path)
    sc.text("blob", enc, "src/rec.json")
    assert (sc.counts, sc.exempt) == ({("blob", "rename-table:3"): len(re.findall("zib", enc, re.I))}, {})


def test_undecodable_chunk_hits_go_on_to_reviewed_tokens(tmp_path):
    chunk = "A" * 40 + "/zibbering/" + "B" * 42                        # 93자: 엄격 해독 실패
    assert ex.strict_b64_text(chunk) is None
    sc = _b64_scanner(tmp_path, "zibbering\tzib\n")
    sc.text("blob", chunk, "src/rec.json")
    assert (sc.counts, sc.exempt) == ({}, {("blob", "reviewed:3"): 1})


def test_base64_recheck_is_one_level_only(tmp_path):
    inner_hit = "zorbix-bank"
    for i in range(500000):
        inner = base64.b64encode(f"see {inner_hit} notes {_h(i)}".encode()).decode()
        if "zib" in inner.lower() or "zorbix" in inner.lower() or "zib" in _h(i).lower():
            continue
        outer = base64.b64encode(inner.encode()).decode()
        if "zib" in outer.lower() and "zorbix" not in outer.lower():
            break
    sc = _b64_scanner(tmp_path)
    sc.text("blob", outer, "src/rec.json")
    assert (sc.counts, _by_label(sc.exempt, "blob")) == ({}, {"encoded-checked:3": len(re.findall("zib", outer, re.I))})


def test_base64_rule_is_blob_only(tmp_path):
    enc = _b64_with(B64_CASES["깨끗한 해독본"][0])
    sc = _b64_scanner(tmp_path)
    sc.text("message", enc)
    assert (sc.counts, sc.exempt) == ({("message", "rename-table:3"): len(re.findall("zib", enc, re.I))}, {})


@pytest.mark.parametrize("case", ["깨끗한 해독본", "해독본 이름"])
def test_base64_verdict_comes_before_reviewed_tokens(tmp_path, case):
    make, label = B64_CASES[case]
    enc = _b64_with(make, token_max=64)
    tok = _alnum_token(enc, enc.lower().index("zib"))
    sc = _b64_scanner(tmp_path, f"{tok}\tzib\n")                        # 덩어리 안 토큰을 검토 토큰으로 둬도
    sc.text("blob", enc, "src/rec.json")
    n_raw = len(re.findall("zib", enc, re.I))
    if label is None:
        assert _by_label(sc.exempt, "blob") == {"encoded-checked:3": n_raw} and sc.reviewed_unused() == 1
    else:
        assert sc.exempt == {} and _by_label(sc.counts, "blob") == {"rename-table:3": n_raw, label: 1}


# ---------------------------------------------------------------- #448 원문 적중 없는 base64 덩어리도 해독 재검사

B64_RAW_AVOID = ("zib", "zorbix", "quux", "ber", "plugh", "owner", "forb")


def _b64_plain(make):
    """make(i) 바이트의 base64 가 64자를 넘고 원문(덩어리)에 가짜 이름·패턴이 하나도 들지 않는 첫 i (결정적).
    해독본에는 make 가 넣은 적중만 있게, 해독본에 'zib' 가 우연히 들면 건너뛴다."""
    for i in range(500000):
        raw = make(i)
        enc = base64.b64encode(raw).decode()
        if len(enc) <= 64 or any(a in enc.lower() for a in B64_RAW_AVOID):
            continue
        if "zib" in raw.decode("utf-8", "replace").lower():
            continue
        return enc
    raise AssertionError("가짜 덩어리를 찾지 못함")


@pytest.mark.parametrize("where", ["덩어리만", "다른 곳에 원문 적중"])
@pytest.mark.parametrize("case", [c for c in B64_CASES if B64_CASES[c][1] is not None])
def test_encoded_name_without_raw_hit_fails(tmp_path, case, where):
    """원문에 이름 적중이 없는 덩어리도 해독해 같은 검사기로 본다(인코딩된 이름 사각지대, #448)."""
    make, label = B64_CASES[case]
    enc = _b64_plain(make)
    assert ex.strict_b64_text(enc) is not None
    sc = _b64_scanner(tmp_path)
    extra = "a zib b\n" if where == "다른 곳에 원문 적중" else ""      # 덩어리 밖 원문 적중은 그대로 실패
    n = sc.text("blob", f'{extra}{{"bodyBase64": "{enc}"}}\n', "src/rec.json")
    want = {label: 1}
    if extra:
        want["rename-table:3"] = 1
    assert (_by_label(sc.counts, "blob"), sc.exempt) == (want, {})
    assert n == sum(want.values()) == sc.total()


@pytest.mark.parametrize("case", ["평범한 base64", "해독 불가(이진)", "해독 불가(길이)"])
def test_chunk_without_raw_hit_passes_when_decoded_clean_or_undecodable(tmp_path, case):
    import hashlib
    if case == "평범한 base64":
        enc = _b64_plain(B64_CASES["깨끗한 해독본"][0])
        assert ex.strict_b64_text(enc) is not None
    elif case == "해독 불가(이진)":
        enc = _b64_plain(lambda i: hashlib.sha256(str(i).encode()).digest() * 3)
        assert ex.strict_b64_text(enc) is None                       # 엄격 UTF-8 실패
    else:
        enc = "Qx7" * 31                                              # 93자: 엄격 base64 실패
        assert ex.strict_b64_text(enc) is None
    sc = _b64_scanner(tmp_path)
    n = sc.text("blob", f'{{"bodyBase64": "{enc}"}}\n', "src/rec.json")
    assert (n, sc.counts, sc.exempt, sc.tokens) == (0, {}, {}, set())


def test_encoded_name_after_json_newline_escape_fails(tmp_path):
    """JSON `\\n` 이스케이프의 n 이 다음 줄 덩어리 앞에 붙어 4자 경계가 1자 어긋나도 해독본을 본다(fail-closed)."""
    import hashlib

    def make(i):                                   # 57바이트 = 76자 한 줄(줄바꿈한 base64 의 한 줄)
        head = hashlib.sha256(str(i).encode()).hexdigest()[:10 + i % 3]
        return f"{head} see zorbix-bank notes ".ljust(57, ".").encode()

    line2 = _b64_plain(make)
    line1 = base64.b64encode(b"." * 57).decode()
    assert len(line1) == len(line2) == 76
    assert ex.strict_b64_text("n" + line2) is None and ex.strict_b64_text(line2) is not None
    sc = _b64_scanner(tmp_path)
    n = sc.text("blob", '{"body": "' + line1 + "\\n" + line2 + '"}\n', "src/rec.json")
    assert (n, sc.counts, sc.exempt) == (1, {("blob", "rename-table:2"): 1}, {})


def test_encoded_name_rule_is_blob_only(tmp_path):
    enc = _b64_plain(B64_CASES["해독본 이름"][0])
    sc = _b64_scanner(tmp_path)
    assert sc.text("message", enc) == 0 and sc.text("path", enc) == 0 and sc.text("ident", enc) == 0
    assert (sc.counts, sc.exempt) == ({}, {})


@needs_tools
def test_encoded_name_blob_fails_export_end_to_end(tmp_path):
    src, split = base_source(tmp_path)
    enc = _b64_plain(B64_CASES["해독본 이름"][0])
    src.commit({"src/enc.json": json.dumps({"bodyBase64": enc}) + "\n"}, "add encoded record")
    rc, out, err = run_export(src, split, inputs(tmp_path), tmp_path / "o")
    line = next(x for x in out.splitlines() if "독립 유출 검사" in x)
    assert rc == 1, (out, err)
    assert line.startswith("[FAIL] 독립 유출 검사: 1 (blob 1, message 0, path 0, ident 0)"), line
    assert "blob/rename-table:2 1" in line, line
    assert_no_secrets(out + err)


# ---------------------------------------------------------------- #441 내보내기 (실제 도구)

RT_TABLE = TABLE + "zib\tdemo-z\n"                                     # 6행


def _rt_source(tmp_path: Path) -> tuple:
    src, split = base_source(tmp_path)
    enc = _b64_with(B64_CASES["깨끗한 해독본"][0])
    src.commit({"src/words.py": "WORD = 'zibbering'\n", "src/rec.json": json.dumps({"bodyBase64": enc}) + "\n"},
               "add zibbering words")
    return src, split, len(re.findall("zib", enc, re.I))


@needs_tools
@pytest.mark.parametrize("with_reviewed", [True, False])
def test_reviewed_tokens_default_path_and_encoded_check_end_to_end(tmp_path, with_reviewed):
    src, split, n_b64 = _rt_source(tmp_path)
    inp = inputs(tmp_path, table=RT_TABLE)
    if with_reviewed:                                    # 기본 경로(HOME 아래)의 파일을 읽는다
        d = tmp_path / "inputs" / "home" / ".config" / "hindsight-crew" / "public-export"
        d.mkdir(parents=True)
        (d / "reviewed-tokens.tsv").write_text("# 가짜\nzibbering\tzib\nzibbers\tzib\n", encoding="utf-8")
    rc, out, err = run_export(src, split, inp, tmp_path / "o", home=tmp_path / "inputs" / "home")
    line = next(x for x in out.splitlines() if "독립 유출 검사" in x)
    if with_reviewed:
        assert rc == 0, (out, err)
        assert line == ("[PASS] 독립 유출 검사: 0 (blob 0, message 0, path 0, ident 0); "
                        f"면제 blob/encoded-checked:6 {n_b64}, blob/reviewed:6 1, message/reviewed:6 1"), line
        assert "검토 토큰: 줄 2, 쓰이지 않은 줄 1" in out
    else:
        assert rc == 1, (out, err)
        assert "blob/rename-table:6 1, message/rename-table:6 1;" in line and f"blob/encoded-checked:6 {n_b64}" in line
        assert "검토 토큰: 줄 0, 쓰이지 않은 줄 0" in out
    assert "zib" not in (out + err).lower()
    assert_no_secrets(out + err + (tmp_path / "o" / "comment.md").read_text(encoding="utf-8"))


@needs_tools
@pytest.mark.parametrize("case, want", [
    ("lint", "검토 토큰 파일 검사 실패: 표에 없는 이름: 2"),
    ("명시 경로 없음", "--reviewed-tokens 파일 없음"),
    ("리포 안", "--reviewed-tokens 는 리포 밖 파일이어야 한다"),
])
def test_reviewed_tokens_input_errors_stop_before_filtering(tmp_path, case, want):
    src, split = base_source(tmp_path)
    rv = tmp_path / "rv.tsv"
    if case == "lint":
        rv.write_text("zibbering\tzib\nzibbering\tnope\n", encoding="utf-8")
    elif case == "리포 안":
        rv = src.path / "rv.tsv"
        rv.write_text("", encoding="utf-8")
    rc, out, err = run_export(src, split, inputs(tmp_path, table=RT_TABLE), tmp_path / "o", "--reviewed-tokens", str(rv))
    assert rc == 2 and want in err, (out, err)
    assert not (tmp_path / "o" / "repo").exists()
    assert "zib" not in (out + err).lower().replace(str(tmp_path).lower(), "")
