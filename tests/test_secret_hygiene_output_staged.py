"""Issue #471 (a)(b): scripts/secret-hygiene.sh must not echo a detected value, and `--staged` must judge the
index blob (what the commit will contain), not the working tree.

Hermetic: the script runs from a copy inside a fresh temp git repo (it cds to its own repo root), so the real
repository is never scanned. Every value here is a synthetic marker. The assignment lines are assembled at
runtime so this file itself carries no `NAME=value` / `NAME: value` line the scanner would flag.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "secret-hygiene.sh"
BASH = "/bin/bash"

# Synthetic, >=16 chars of [A-Za-z0-9_], none of the placeholder words the scanner skips.
MARKER = "zq7SYNTHleak0marker0demo471"
KEY = "DEMO_API_" + "TOKEN"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPT, r / "scripts" / "secret-hygiene.sh")
    git(r, "init", "-q")
    git(r, "config", "user.email", "demo@example.invalid")
    git(r, "config", "user.name", "demo")
    git(r, "config", "commit.gpgsign", "false")
    (r / "README.txt").write_text("demo\n")
    git(r, "add", "scripts/secret-hygiene.sh", "README.txt")
    git(r, "commit", "-q", "-m", "init")
    return r


def run(repo, *args):
    p = subprocess.run([BASH, str(repo / "scripts" / "secret-hygiene.sh"), *args],
                       capture_output=True, text=True, cwd=repo)
    return p.returncode, p.stdout + p.stderr


def leak_line(sep):
    return f"{KEY}{sep}{MARKER}\n"


# (a) case x surface matrix: separator (`:` was the leaking form, `=` the redacted one) x mode.
@pytest.mark.parametrize("sep", [": ", ":", "=", " = "])
@pytest.mark.parametrize("mode", ["full", "--staged"])
def test_detected_value_never_printed(repo, sep, mode):
    f = repo / "conf.yml"
    f.write_text("first: line\n" + leak_line(sep))
    git(repo, "add", "conf.yml")
    if mode == "full":
        git(repo, "commit", "-q", "-m", "leak")
        rc, out = run(repo)
    else:
        rc, out = run(repo, "--staged")
    assert rc == 1, out
    assert out.count(MARKER) == 0, out
    # file, line and check kind only
    assert "LEAK[kv-secret]: conf.yml:2" in out, out


@pytest.mark.parametrize("mode", ["full", "--staged"])
def test_private_key_reports_file_line_kind(repo, mode):
    body = "x\n-----BEGIN OPENSSH PRIVATE " + "KEY-----\n" + MARKER + "\n"
    (repo / "id_demo.txt").write_text(body)
    git(repo, "add", "id_demo.txt")
    if mode == "full":
        git(repo, "commit", "-q", "-m", "k")
        rc, out = run(repo)
    else:
        rc, out = run(repo, "--staged")
    assert rc == 1, out
    assert MARKER not in out, out
    assert "LEAK[private-key]: id_demo.txt:2" in out, out


# (b) index and working tree differ -> the index decides.
def test_staged_secret_then_worktree_cleaned_is_blocked(repo):
    f = repo / "conf.yml"
    f.write_text(leak_line(": "))
    git(repo, "add", "conf.yml")
    f.write_text("clean: yes\n")          # working tree tidied, index still holds the secret
    rc, out = run(repo, "--staged")
    assert rc == 1, out
    assert "conf.yml:1" in out, out
    assert MARKER not in out, out


def test_staged_secret_then_worktree_deleted_is_blocked(repo):
    f = repo / "conf.yml"
    f.write_text(leak_line("="))
    git(repo, "add", "conf.yml")
    f.unlink()                            # gone from the working tree, still staged as an add
    rc, out = run(repo, "--staged")
    assert rc == 1, out
    assert MARKER not in out, out


def test_staged_clean_but_worktree_dirty_passes(repo):
    f = repo / "conf.yml"
    f.write_text("clean: yes\n")
    git(repo, "add", "conf.yml")
    f.write_text(leak_line(": "))         # unstaged edit is not part of the commit
    rc, out = run(repo, "--staged")
    assert rc == 0, out
    assert MARKER not in out, out


def test_full_scan_clean_repo_passes(repo):
    rc, out = run(repo)
    assert rc == 0, out
    assert "clean" in out, out


# rework #471-1: --staged must not skip paths git would quote (non-ASCII, tab) or that contain spaces.
@pytest.mark.parametrize("name", ["설정-데모.yml", "demo conf.yml", "demo\ttab.yml", "데모 폴더/설정 값.yml"])
def test_staged_unusual_path_secret_then_worktree_cleaned_is_blocked(repo, name):
    f = repo / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(leak_line(": "))
    git(repo, "add", "--", name)
    f.write_text("clean: yes\n")
    rc, out = run(repo, "--staged")
    assert rc == 1, out
    assert "LEAK[kv-secret]: " + name + ":1" in out, out
    assert MARKER not in out, out


def test_full_scan_unusual_path_is_scanned(repo):
    name = "설정-데모.yml"
    (repo / name).write_text(leak_line("="))
    git(repo, "add", "--", name)
    git(repo, "commit", "-q", "-m", "leak")
    rc, out = run(repo)
    assert rc == 1, out
    assert "LEAK[kv-secret]: " + name + ":1" in out, out
    assert MARKER not in out, out


def test_staged_unreadable_blob_fails_not_passes(repo):
    # an index entry whose blob is absent from the object store: the scan cannot read what the commit
    # would record, so it must block instead of silently passing.
    missing = "0123456789abcdef0123456789abcdef01234567"
    git(repo, "update-index", "--add", "--info-only", "--cacheinfo", f"100644,{missing},ghost.yml")
    rc, out = run(repo, "--staged")
    assert rc == 1, out
    assert "LEAK[unreadable]: ghost.yml" in out, out


def test_staged_gitlink_is_not_unreadable(repo):
    # a submodule entry (mode 160000) has no blob to scan; it is skipped, not reported.
    sub = "0123456789abcdef0123456789abcdef01234567"
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{sub},vendor-demo")
    rc, out = run(repo, "--staged")
    assert rc == 0, out


def test_staged_rename_with_secret_is_scanned(repo):
    # with rename detection a staged rename is status R, which --diff-filter=ACM would drop.
    base = "".join(f"demo_line_{i}: plain value number {i}\n" for i in range(40))
    (repo / "old-demo.yml").write_text(base)
    git(repo, "add", "old-demo.yml")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "mv", "old-demo.yml", "new-demo.yml")
    (repo / "new-demo.yml").write_text(base + leak_line(": "))
    git(repo, "add", "new-demo.yml")
    status = git(repo, "-c", "diff.renames=true", "diff", "--cached", "--name-status").stdout
    assert status.startswith("R"), status            # the case under test really is a rename
    rc, out = run(repo, "--staged")
    assert rc == 1, out
    assert "LEAK[kv-secret]: new-demo.yml:41" in out, out
    assert MARKER not in out, out


# final-review 20261110-001256 fixes (Bugs 1, 3, 4).
GNU_GREP_SHIM = r'''#!/bin/bash
# Emulates GNU grep >= 3.5 on input holding a NUL byte: without -a a match is reported only as a
# "binary file matches" notice on stderr, nothing on stdout. Everything else goes to the real grep.
a=0
for x in "$@"; do
  [ "$x" = "--" ] && break
  case "$x" in --*) ;; -*a*) a=1 ;; esac
done
tmp="$(mktemp)"; trap 'rm -f "$tmp"' EXIT
cat > "$tmp"
if [ "$a" = 0 ] && ! LC_ALL=C tr -d '\000' < "$tmp" | cmp -s - "$tmp"; then
  if /usr/bin/grep -a -q "$@" < "$tmp"; then echo "grep: (standard input): binary file matches" >&2; exit 0; fi
  exit 1
fi
/usr/bin/grep "$@" < "$tmp"
'''


def run_with_gnu_grep_shim(repo, tmp_path, *args):
    bindir = tmp_path / "shim-bin"
    bindir.mkdir()
    shim = bindir / "grep"
    shim.write_text(GNU_GREP_SHIM)
    shim.chmod(0o755)
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")
    p = subprocess.run([BASH, str(repo / "scripts" / "secret-hygiene.sh"), *args],
                       capture_output=True, text=True, cwd=repo, env=env)
    return p.returncode, p.stdout + p.stderr


@pytest.mark.parametrize("junk", [b"\x00", b"\xff\xfe"])
def test_binary_classified_private_key_is_flagged(repo, tmp_path, junk):
    # This host's grep (BSD) prints the binary notice on stdout, so the regression only shows with GNU grep
    # >= 3.5 semantics; the shim reproduces them. The NUL case is what the shim treats as binary.
    body = b"x" + junk + b"\n-----BEGIN RSA PRIVATE " + b"KEY-----\n" + MARKER.encode() + b"\n"
    (repo / "blob-demo.bin").write_bytes(body)
    git(repo, "add", "blob-demo.bin")
    rc, out = run_with_gnu_grep_shim(repo, tmp_path, "--staged")
    assert rc == 1, out
    assert "LEAK[private-key]: blob-demo.bin:2" in out, out
    assert MARKER not in out, out


def test_staged_symlink_to_regular_file_type_change_is_scanned(repo):
    link = repo / "conf-demo.yml"
    link.symlink_to("README.txt")
    git(repo, "add", "conf-demo.yml")
    git(repo, "commit", "-q", "-m", "link")
    link.unlink()
    link.write_text(leak_line(": "))
    git(repo, "add", "conf-demo.yml")
    status = git(repo, "diff", "--cached", "--name-status").stdout
    assert status.startswith("T"), status            # the case under test really is a type change
    rc, out = run(repo, "--staged")
    assert rc == 1, out
    assert "LEAK[kv-secret]: conf-demo.yml:1" in out, out
    assert MARKER not in out, out


@pytest.mark.parametrize("mode", ["full", "--staged"])
def test_outside_git_repo_fails_closed(tmp_path, mode):
    plain = tmp_path / "plain"
    (plain / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPT, plain / "scripts" / "secret-hygiene.sh")
    (plain / "conf.yml").write_text(leak_line(": "))
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(tmp_path))
    args = [] if mode == "full" else [mode]
    p = subprocess.run([BASH, str(plain / "scripts" / "secret-hygiene.sh"), *args],
                       capture_output=True, text=True, cwd=plain, env=env)
    assert p.returncode == 1, p.stdout + p.stderr
    assert "no files to scan" not in p.stdout, p.stdout
    assert MARKER not in p.stdout + p.stderr
