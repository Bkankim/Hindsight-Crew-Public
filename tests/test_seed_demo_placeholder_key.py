"""Issue #471 (c): a hand-made `.env.local` (e.g. `cp .env.example .env.local`) must not boot with the
template placeholder, or an empty value, as the upstream key. scripts/seed-demo.sh generates a key for it.

Hermetic: seed-demo.sh runs from a copy under tmp_path (it cds to its own repo root), so the real `.env.local`
and secrets/ are never read or written, and no docker command runs (seed-demo.sh calls none). Every key here is
a synthetic value.
"""
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BASH = "/bin/bash"
NAME = "HC_HINDSIGHT_API_KEY"
GEN = re.compile(r"^hs_[0-9a-f]{48}$")
SYNTH_REAL = "hs_" + "0123456789abcdef" * 3   # synthetic, already-seeded key that must be kept


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "root"
    (r / "scripts").mkdir(parents=True)
    shutil.copy2(REPO / "scripts" / "seed-demo.sh", r / "scripts" / "seed-demo.sh")
    shutil.copytree(REPO / "profiles", r / "profiles")
    return r


def seed(root):
    env = {k: v for k, v in os.environ.items() if not k.startswith("HC_")}
    p = subprocess.run([BASH, str(root / "scripts" / "seed-demo.sh")], capture_output=True, text=True,
                       cwd=root, env=env)
    return p.returncode, p.stdout + p.stderr


def key_lines(root):
    return [ln.split("=", 1)[1] for ln in (root / ".env.local").read_text().splitlines()
            if ln.startswith(NAME + "=")]


def write_env(root, key_line):
    base = [ln for ln in (REPO / ".env.example").read_text().splitlines() if not ln.startswith(NAME + "=")]
    if key_line is not None:
        base.insert(0, key_line)
    (root / ".env.local").write_text("\n".join(base) + "\n")
    os.chmod(root / ".env.local", 0o600)


def test_copied_template_gets_generated_key(root):
    shutil.copy2(REPO / ".env.example", root / ".env.local")
    rc, out = seed(root)
    assert rc == 0, out
    vals = key_lines(root)
    assert len(vals) == 1 and GEN.match(vals[0]), vals
    assert vals[0] not in out, out          # the key itself is never printed


@pytest.mark.parametrize("key_line", [
    f"{NAME}=",
    f'{NAME}=""',
    f"{NAME}=''",
    f"{NAME}=   ",
    f"{NAME}=REPLACE_OR_AUTOSEED",
    f"{NAME}=REPLACE_ME",
    f"{NAME}=<your key>",
    f"{NAME}=changeme",
    f"{NAME}=REPLACE_OR_AUTOSEED   # demo comment",
    f"{NAME}=   # demo comment",
    None,                                   # key absent entirely
])
def test_empty_placeholder_or_missing_key_is_generated(root, key_line):
    write_env(root, key_line)
    rc, out = seed(root)
    assert rc == 0, out
    vals = key_lines(root)
    assert len(vals) == 1 and GEN.match(vals[0]), (vals, out)
    assert vals[0] not in out, out
    assert stat.S_IMODE(os.stat(root / ".env.local").st_mode) == 0o600


def test_real_key_untouched_and_idempotent(root):
    write_env(root, f"{NAME}={SYNTH_REAL}")
    rc, out = seed(root)
    assert rc == 0, out
    assert key_lines(root) == [SYNTH_REAL]
    rc, out = seed(root)
    assert rc == 0, out
    assert key_lines(root) == [SYNTH_REAL]


def test_real_key_with_inline_comment_untouched(root):
    line = f"{NAME}={SYNTH_REAL}   # demo comment"
    write_env(root, line)
    rc, out = seed(root)
    assert rc == 0, out
    assert [ln for ln in (root / ".env.local").read_text().splitlines() if ln.startswith(NAME + "=")] == [line]


def test_generated_key_stable_on_rerun(root):
    shutil.copy2(REPO / ".env.example", root / ".env.local")
    assert seed(root)[0] == 0
    first = key_lines(root)
    assert seed(root)[0] == 0
    assert key_lines(root) == first


def test_mixed_real_and_placeholder_refuses(root):
    write_env(root, f"{NAME}={SYNTH_REAL}\n{NAME}=REPLACE_OR_AUTOSEED")
    before = (root / ".env.local").read_text()
    rc, out = seed(root)
    assert rc != 0, out
    assert (root / ".env.local").read_text() == before
    assert SYNTH_REAL not in out, out


def test_fresh_seed_still_generates(root):
    rc, out = seed(root)
    assert rc == 0, out
    vals = key_lines(root)
    assert len(vals) == 1 and GEN.match(vals[0]), vals


# final-review 20261110-001256 fixes (Bugs 6, 7, 8, 9, 10).
REAL_WITH_X = "Zk9qXXXXv2Lm8RtYwPa7Ns4Qb6Hd"          # real-shaped synthetic key that contains "XXXX"


def test_real_key_containing_xxxx_is_kept(root):
    write_env(root, f"{NAME}={REAL_WITH_X}")
    rc, out = seed(root)
    assert rc == 0, out
    assert key_lines(root) == [REAL_WITH_X]


def test_quoted_empty_value_with_inline_comment_is_generated(root):
    write_env(root, f'{NAME}="" # demo fill in')
    rc, out = seed(root)
    assert rc == 0, out
    vals = key_lines(root)
    assert len(vals) == 1 and GEN.match(vals[0]), (vals, out)


def test_export_prefixed_real_key_is_kept(root):
    line = f"export {NAME}={SYNTH_REAL}"
    write_env(root, line)
    before = [ln for ln in (root / ".env.local").read_text().splitlines() if NAME in ln]
    rc, out = seed(root)
    assert rc == 0, out
    after = [ln for ln in (root / ".env.local").read_text().splitlines() if NAME in ln]
    assert before == after == [line]                    # nothing appended, real key untouched


def _seed_with_umask(root, mask):
    env = {k: v for k, v in os.environ.items() if not k.startswith("HC_")}
    cmd = f"umask {mask:03o}; exec {BASH} {root / 'scripts' / 'seed-demo.sh'}"
    p = subprocess.run([BASH, "-c", cmd], capture_output=True, text=True, cwd=root, env=env)
    return p.returncode, p.stdout + p.stderr


@pytest.mark.parametrize("start", ["copied-template", "fresh"])
def test_written_key_file_is_owner_only(root, start):
    if start == "copied-template":
        shutil.copy2(REPO / ".env.example", root / ".env.local")
        os.chmod(root / ".env.local", 0o644)            # what `cp` gives under umask 022
    rc, out = _seed_with_umask(root, 0o022)
    assert rc == 0, out
    assert stat.S_IMODE(os.stat(root / ".env.local").st_mode) == 0o600
    assert GEN.match(key_lines(root)[0])


def test_rewrite_failure_leaves_original_intact(root):
    shutil.copy2(REPO / ".env.example", root / ".env.local")
    (root / "secrets").mkdir()
    (root / "audit").mkdir()
    before = (root / ".env.local").read_bytes()
    os.chmod(root, 0o555)                               # the temp file for the atomic rewrite cannot be created
    try:
        rc, out = seed(root)
    finally:
        os.chmod(root, 0o755)
    assert rc != 0, out
    assert (root / ".env.local").read_bytes() == before
    assert [p.name for p in root.iterdir() if p.name.startswith(".env.local.")] == []
