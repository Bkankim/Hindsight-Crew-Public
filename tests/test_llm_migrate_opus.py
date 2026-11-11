"""scripts/llm-migrate-opus.py (issue #248): move an env file's LLM block to claude-code/claude-sonnet-5-5 with a account1 -> account2
failover chain (global and mental-model-refresh slot), dry run by default, backup + atomic replace on --apply.

Every file is a tmp file with FAKE tokens; the script is run as a subprocess (its real exit codes and output).
"""
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "llm-migrate-opus.py"
GROK, ACCOUNT1, ACCOUNT2 = "FAKE-GROK-KEY-aaaa1111", "FAKE-ACCOUNT1-TOKEN-bbbb2222", "FAKE-ACCOUNT2-TOKEN-cccc3333"
OTHER = "FAKE-OTHER-TOKEN-dddd4444"
K = "HINDSIGHT_API_LLM_"
MM = "HINDSIGHT_API_MENTAL_MODEL_REFRESH_LLM_"
STRATEGY_LINE = "HINDSIGHT_API_LLM_STRATEGY='{\"mode\":\"failover\"}'"

# the shape of today's production file: grok main, LLM_1 account1, LLM_2 account2, reasoning effort, slot keys, xai leftovers
PROD_SHAPE = f"""# production .env.local (fake)
HC_PROFILE=ko-full
HINDSIGHT_API_EMBEDDINGS_PROVIDER=local
HINDSIGHT_API_LLM_PROVIDER=xai-oauth
HINDSIGHT_API_LLM_MODEL=grok-4.6
HINDSIGHT_API_LLM_API_KEY={GROK}
HINDSIGHT_API_LLM_REASONING_EFFORT=medium
HINDSIGHT_API_LLM_TIMEOUT=120
HINDSIGHT_API_LLM_STRATEGY={{"mode":"failover"}}
HINDSIGHT_API_LLM_1_PROVIDER=claude-code
HINDSIGHT_API_LLM_1_MODEL=claude-sonnet-5-5
HINDSIGHT_API_LLM_1_API_KEY={ACCOUNT1}
HINDSIGHT_API_LLM_2_PROVIDER=claude-code
HINDSIGHT_API_LLM_2_MODEL=claude-sonnet-5-5
HINDSIGHT_API_LLM_2_API_KEY={ACCOUNT2}
HINDSIGHT_API_RETAIN_LLM_PROVIDER=xai-oauth
HINDSIGHT_API_REFLECT_LLM_PROVIDER=xai-oauth
HINDSIGHT_API_REFLECT_LLM_2_API_KEY={ACCOUNT2}
HINDSIGHT_API_CONSOLIDATION_LLM_MODEL=grok-4.5
HINDSIGHT_API_MENTAL_MODEL_REFRESH_LLM_TIMEOUT=300
XAI_API_KEY={GROK}
HINDSIGHT_API_XAI_AUTH_DIR=/x
HC_OTHER=keep-me
"""

TARGET = f"""# target (fake)
HC_PROFILE=ko-full
HINDSIGHT_API_LLM_PROVIDER=claude-code
HINDSIGHT_API_LLM_MODEL=claude-sonnet-5-5
HINDSIGHT_API_LLM_API_KEY={ACCOUNT1}
{STRATEGY_LINE}
HINDSIGHT_API_LLM_1_PROVIDER=claude-code
HINDSIGHT_API_LLM_1_MODEL=claude-sonnet-5-5
HINDSIGHT_API_LLM_1_API_KEY={ACCOUNT2}
HINDSIGHT_API_MENTAL_MODEL_REFRESH_LLM_STRATEGY='{{"mode":"failover"}}'
HINDSIGHT_API_MENTAL_MODEL_REFRESH_LLM_1_PROVIDER=claude-code
HINDSIGHT_API_MENTAL_MODEL_REFRESH_LLM_1_MODEL=claude-sonnet-5-5
HINDSIGHT_API_MENTAL_MODEL_REFRESH_LLM_1_API_KEY={ACCOUNT2}
"""

EXPECTED_DIFF = sorted([
    "~ HINDSIGHT_API_LLM_PROVIDER", "~ HINDSIGHT_API_LLM_MODEL", "~ HINDSIGHT_API_LLM_API_KEY",
    "~ HINDSIGHT_API_LLM_STRATEGY", "~ HINDSIGHT_API_LLM_1_API_KEY",
    "+ " + MM + "STRATEGY", "+ " + MM + "1_PROVIDER", "+ " + MM + "1_MODEL", "+ " + MM + "1_API_KEY",
    "- HINDSIGHT_API_LLM_REASONING_EFFORT", "- HINDSIGHT_API_LLM_2_PROVIDER", "- HINDSIGHT_API_LLM_2_MODEL",
    "- HINDSIGHT_API_LLM_2_API_KEY", "- HINDSIGHT_API_RETAIN_LLM_PROVIDER", "- HINDSIGHT_API_REFLECT_LLM_PROVIDER",
    "- HINDSIGHT_API_REFLECT_LLM_2_API_KEY", "- HINDSIGHT_API_CONSOLIDATION_LLM_MODEL",
    "- HINDSIGHT_API_MENTAL_MODEL_REFRESH_LLM_TIMEOUT", "- XAI_API_KEY", "- HINDSIGHT_API_XAI_AUTH_DIR",
])
FORBIDDEN_IN_OUTPUT = [GROK, ACCOUNT1, ACCOUNT2, "grok", "xai-oauth", "claude-sonnet-5-5", "failover", "medium", "keep-me"]


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=30)


def put(tmp_path, text, mode=0o600, name=".env.local"):
    p = tmp_path / name
    p.write_text(text)
    p.chmod(mode)
    return p


def parse(text):
    out = {}
    for line in text.splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k] = v
    return out


def shell_values(path, *keys):
    r = subprocess.run(["bash", "-c", 'set -a; . "$1"; set +a; shift; for k in "$@"; do printf "%s=%s\\n" "$k" "${!k}"; done',
                        "_", str(path), *keys], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    return parse(r.stdout)


def backups(tmp_path):
    return sorted(p for p in tmp_path.iterdir() if ".bak-" in p.name)


# ---------------------------------------------------------------- dry run

def test_dry_run_prints_key_names_only_and_changes_nothing(tmp_path):
    p = put(tmp_path, PROD_SHAPE)
    r = run("--env-file", str(p))
    assert r.returncode == 0, r.stderr
    lines = [ln for ln in r.stdout.splitlines() if ln[:2] in ("+ ", "- ", "~ ")]
    assert sorted(lines) == EXPECTED_DIFF
    assert "dry-run" in r.stdout
    for needle in FORBIDDEN_IN_OUTPUT:
        assert needle not in r.stdout + r.stderr, needle
    assert p.read_text() == PROD_SHAPE and backups(tmp_path) == []
    assert [x.name for x in tmp_path.iterdir()] == [".env.local"]


# ---------------------------------------------------------------- apply

def test_apply_reaches_the_target_with_a_byte_identical_0600_backup(tmp_path):
    p = put(tmp_path, PROD_SHAPE, mode=0o640)
    r = run("--env-file", str(p), "--apply")
    assert r.returncode == 0, r.stderr
    for needle in FORBIDDEN_IN_OUTPUT:
        assert needle not in r.stdout + r.stderr, needle
    baks = backups(tmp_path)
    assert len(baks) == 1 and re.fullmatch(r"\.env\.local\.bak-\d{8}-\d{6}", baks[0].name)
    assert baks[0].read_bytes() == PROD_SHAPE.encode()
    assert stat.S_IMODE(baks[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(p.stat().st_mode) == 0o640, "the env file keeps its own mode"
    got = parse(p.read_text())
    assert got["HINDSIGHT_API_LLM_PROVIDER"] == "claude-code" and got["HINDSIGHT_API_LLM_MODEL"] == "claude-sonnet-5-5"
    assert got["HINDSIGHT_API_LLM_API_KEY"] == ACCOUNT1 and got["HINDSIGHT_API_LLM_1_API_KEY"] == ACCOUNT2
    assert got[MM + "1_API_KEY"] == ACCOUNT2 and got[MM + "1_PROVIDER"] == "claude-code" and got[MM + "1_MODEL"] == "claude-sonnet-5-5"
    assert got["HINDSIGHT_API_LLM_1_PROVIDER"] == "claude-code" and got["HINDSIGHT_API_LLM_1_MODEL"] == "claude-sonnet-5-5"
    assert STRATEGY_LINE in p.read_text().splitlines()
    assert got[MM + "STRATEGY"] == "'{\"mode\":\"failover\"}'"
    gone = [k for k in got if k.startswith(K + "2_") or k.endswith("REASONING_EFFORT") or "XAI" in k.upper()
            or re.match(r"^HINDSIGHT_API_[A-Z0-9]+_LLM_", k) and not k.startswith(MM)]
    assert gone == []
    # unrelated keys, comments and the global non-chain LLM keys survive in their place
    assert got["HC_OTHER"] == "keep-me" and got[K + "TIMEOUT"] == "120" and got["HC_PROFILE"] == "ko-full"
    assert p.read_text().splitlines()[0] == "# production .env.local (fake)"
    assert [x.name for x in tmp_path.iterdir() if x.name.startswith(".llm-migrate")] == [], "no temp file left"


def test_the_strategy_survives_a_shell_source_and_is_the_same_json(tmp_path):
    p = put(tmp_path, PROD_SHAPE)
    assert run("--env-file", str(p), "--apply").returncode == 0
    vals = shell_values(p, K + "STRATEGY", MM + "STRATEGY", K + "API_KEY")
    assert vals[K + "STRATEGY"] == '{"mode":"failover"}' and vals[MM + "STRATEGY"] == '{"mode":"failover"}'
    assert vals[K + "API_KEY"] == ACCOUNT1


def test_apply_twice_is_a_no_op_the_second_time(tmp_path):
    p = put(tmp_path, PROD_SHAPE)
    assert run("--env-file", str(p), "--apply").returncode == 0
    after = p.read_bytes()
    n = len(backups(tmp_path))
    r = run("--env-file", str(p), "--apply")
    assert r.returncode == 0 and "already migrated" in r.stdout
    assert p.read_bytes() == after and len(backups(tmp_path)) == n


def test_quoted_values_and_export_prefix_are_carried_over(tmp_path):
    text = PROD_SHAPE.replace(f"HINDSIGHT_API_LLM_1_API_KEY={ACCOUNT1}", f"export HINDSIGHT_API_LLM_1_API_KEY='{ACCOUNT1}'")
    p = put(tmp_path, text)
    assert run("--env-file", str(p), "--apply").returncode == 0
    vals = shell_values(p, K + "API_KEY", K + "1_API_KEY", MM + "1_API_KEY")
    assert vals[K + "API_KEY"] == ACCOUNT1 and vals[K + "1_API_KEY"] == ACCOUNT2 and vals[MM + "1_API_KEY"] == ACCOUNT2


def test_a_file_without_a_trailing_newline_and_with_a_duplicate_key(tmp_path):
    text = PROD_SHAPE.rstrip("\n") + "\nHINDSIGHT_API_LLM_MODEL=grok-4.5"
    p = put(tmp_path, text)
    assert run("--env-file", str(p), "--apply").returncode == 0
    lines = p.read_text().splitlines()
    assert sum(1 for ln in lines if ln.startswith("HINDSIGHT_API_LLM_MODEL=")) == 1
    assert parse(p.read_text())["HINDSIGHT_API_LLM_MODEL"] == "claude-sonnet-5-5"
    assert p.read_text().endswith("\n")


def test_apply_to_a_symlink_replaces_the_target_not_the_link(tmp_path):
    real = put(tmp_path, PROD_SHAPE, name="real.env")
    link = tmp_path / "link.env"
    link.symlink_to(real)
    assert run("--env-file", str(link), "--apply").returncode == 0
    assert link.is_symlink() and parse(real.read_text())["HINDSIGHT_API_LLM_API_KEY"] == ACCOUNT1


# ---------------------------------------------------------------- already the target (judged BEFORE the provider validation)

TARGET_VARIANTS = {
    "single-quoted-strategy": TARGET,
    "unquoted-strategy": TARGET.replace(STRATEGY_LINE, 'HINDSIGHT_API_LLM_STRATEGY={"mode":"failover"}'),
    "double-quoted-strategy": TARGET.replace(STRATEGY_LINE, "HINDSIGHT_API_LLM_STRATEGY=\"{\\\"mode\\\":\\\"failover\\\"}\""),
    "spaced-strategy-json": TARGET.replace(STRATEGY_LINE, "HINDSIGHT_API_LLM_STRATEGY='{ \"mode\": \"failover\" }'"),
    "quoted-providers-and-models": TARGET.replace("claude-code", "'claude-code'").replace("claude-sonnet-5-5", '"claude-sonnet-5-5"'),
    "refresh-keys-before-global-keys": "\n".join(reversed(TARGET.strip().splitlines())) + "\n",
}


@pytest.mark.parametrize("flag", [[], ["--apply"]], ids=["dry-run", "apply"])
@pytest.mark.parametrize("variant", sorted(TARGET_VARIANTS))
def test_an_input_that_is_already_the_target_is_left_alone(tmp_path, variant, flag):
    """The target has no LLM_2_PROVIDER: if the old-shape validation ran first it would refuse this input."""
    p = put(tmp_path, TARGET_VARIANTS[variant])
    before = p.read_bytes()
    r = run("--env-file", str(p), *flag)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "already migrated" in r.stdout
    assert p.read_bytes() == before and backups(tmp_path) == []
    for needle in (ACCOUNT1, ACCOUNT2):
        assert needle not in r.stdout + r.stderr


COMMENTED = {
    "unquoted-values": TARGET.replace("HINDSIGHT_API_LLM_MODEL=claude-sonnet-5-5", "HINDSIGHT_API_LLM_MODEL=claude-sonnet-5-5   # main")
                             .replace("HINDSIGHT_API_LLM_PROVIDER=claude-code", "HINDSIGHT_API_LLM_PROVIDER=claude-code # primary"),
    "quoted-strategy": TARGET.replace(STRATEGY_LINE, STRATEGY_LINE + "  # failover"),
    "token-lines": TARGET.replace(f"HINDSIGHT_API_LLM_API_KEY={ACCOUNT1}", f"HINDSIGHT_API_LLM_API_KEY={ACCOUNT1}  # account1")
                         .replace(f"HINDSIGHT_API_LLM_1_API_KEY={ACCOUNT2}", f"HINDSIGHT_API_LLM_1_API_KEY='{ACCOUNT2}' # account2"),
}


@pytest.mark.parametrize("variant", sorted(COMMENTED))
def test_a_trailing_comment_is_not_part_of_the_value(tmp_path, variant):
    """The repo's env files carry inline comments (.env.example); a shell source and compose drop them, so must the judgment."""
    p = put(tmp_path, COMMENTED[variant])
    r = run("--env-file", str(p), "--apply")
    assert r.returncode == 0 and "already migrated" in r.stdout, r.stdout + r.stderr
    assert backups(tmp_path) == []


def test_a_commented_old_shape_migrates_without_carrying_the_comment_to_another_key(tmp_path):
    text = PROD_SHAPE.replace(f"HINDSIGHT_API_LLM_1_API_KEY={ACCOUNT1}", f"HINDSIGHT_API_LLM_1_API_KEY={ACCOUNT1}  # account1").replace(
        f"HINDSIGHT_API_LLM_2_API_KEY={ACCOUNT2}", f"HINDSIGHT_API_LLM_2_API_KEY='{ACCOUNT2}' # account2")
    p = put(tmp_path, text)
    assert run("--env-file", str(p), "--apply").returncode == 0
    assert "#" not in "".join(ln for ln in p.read_text().splitlines() if ln.startswith("HINDSIGHT_API_") and "KEY" in ln)
    vals = shell_values(p, K + "API_KEY", K + "1_API_KEY", MM + "1_API_KEY")
    assert vals[K + "API_KEY"] == ACCOUNT1 and vals[K + "1_API_KEY"] == ACCOUNT2 and vals[MM + "1_API_KEY"] == ACCOUNT2


@pytest.mark.parametrize("stray", ["HINDSIGHT_API_LLM_REASONING_EFFORT=low", "HINDSIGHT_API_REFLECT_LLM_PROVIDER=claude-code",
                                   "XAI_API_KEY=x"])
def test_target_plus_leftover_keys_names_the_leftovers(tmp_path, stray):
    """Spec order keeps the refusal, but the message must say the target is in place and which keys are left over."""
    p = put(tmp_path, TARGET + stray + "\n")
    r = run("--env-file", str(p))
    assert r.returncode == 1 and "refused" in r.stderr
    assert "already in place" in r.stderr and stray.split("=")[0] in r.stderr
    assert ACCOUNT1 not in r.stderr and ACCOUNT2 not in r.stderr and p.read_text() == TARGET + stray + "\n"


@pytest.mark.parametrize("mutate", [
    lambda t: t.replace("HINDSIGHT_API_LLM_MODEL=claude-sonnet-5-5", "HINDSIGHT_API_LLM_MODEL=claude-opus-4-8"),
    lambda t: t.replace("HINDSIGHT_API_LLM_PROVIDER=claude-code", "HINDSIGHT_API_LLM_PROVIDER=xai-oauth"),
    lambda t: t.replace(STRATEGY_LINE, "HINDSIGHT_API_LLM_STRATEGY='{\"mode\":\"round-robin\"}'"),
    lambda t: t.replace(f"HINDSIGHT_API_LLM_1_API_KEY={ACCOUNT2}", f"HINDSIGHT_API_LLM_1_API_KEY={ACCOUNT1}"),
    lambda t: t + "HINDSIGHT_API_LLM_2_PROVIDER=claude-code\n",
    lambda t: t + "HINDSIGHT_API_LLM_REASONING_EFFORT=low\n",
    lambda t: t + "HINDSIGHT_API_REFLECT_LLM_PROVIDER=claude-code\n",
    lambda t: t + "XAI_API_KEY=x\n",
    lambda t: t.replace(f"{MM}1_API_KEY={ACCOUNT2}", f"{MM}1_API_KEY={ACCOUNT1}"),
    lambda t: "\n".join(ln for ln in t.splitlines() if not ln.startswith(MM + "STRATEGY")) + "\n",
], ids=["model", "primary-provider", "strategy-mode", "tokens-equal", "llm2-left", "reasoning-left", "slot-key-left",
        "xai-left", "refresh-member-token-not-account2", "refresh-strategy-missing"])
def test_a_near_target_input_is_not_called_migrated(tmp_path, mutate):
    """Anything short of the exact target is not 'already migrated'. These inputs have no LLM_2 chain to move, so the
    old-shape validation then refuses them (exit 1) instead of guessing."""
    p = put(tmp_path, mutate(TARGET))
    r = run("--env-file", str(p))
    assert "already migrated" not in r.stdout
    assert r.returncode == 1 and "refused" in r.stderr


# ---------------------------------------------------------------- refusals

def with_replaced(old, new):
    return PROD_SHAPE.replace(old, new)


REFUSED = {
    "member1-xai-oauth": with_replaced("HINDSIGHT_API_LLM_1_PROVIDER=claude-code", "HINDSIGHT_API_LLM_1_PROVIDER=xai-oauth"),
    "member2-anthropic": with_replaced("HINDSIGHT_API_LLM_2_PROVIDER=claude-code", "HINDSIGHT_API_LLM_2_PROVIDER=anthropic"),
    "member2-none": with_replaced("HINDSIGHT_API_LLM_2_PROVIDER=claude-code", "HINDSIGHT_API_LLM_2_PROVIDER=none"),
    "member2-empty-provider": with_replaced("HINDSIGHT_API_LLM_2_PROVIDER=claude-code", "HINDSIGHT_API_LLM_2_PROVIDER="),
    "member1-provider-missing": with_replaced("HINDSIGHT_API_LLM_1_PROVIDER=claude-code\n", ""),
    "member2-provider-missing": with_replaced("HINDSIGHT_API_LLM_2_PROVIDER=claude-code\n", ""),
    "member1-token-empty": with_replaced(f"HINDSIGHT_API_LLM_1_API_KEY={ACCOUNT1}", "HINDSIGHT_API_LLM_1_API_KEY="),
    "member2-token-missing": with_replaced(f"HINDSIGHT_API_LLM_2_API_KEY={ACCOUNT2}\n", ""),
    "tokens-identical": with_replaced(f"HINDSIGHT_API_LLM_2_API_KEY={ACCOUNT2}", f"HINDSIGHT_API_LLM_2_API_KEY={ACCOUNT1}"),
}


@pytest.mark.parametrize("flag", [[], ["--apply"]], ids=["dry-run", "apply"])
@pytest.mark.parametrize("case", sorted(REFUSED))
def test_an_input_that_is_not_the_old_shape_is_refused_and_nothing_is_written(tmp_path, case, flag):
    p = put(tmp_path, REFUSED[case])
    r = run("--env-file", str(p), *flag)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "refused" in r.stderr and "HINDSIGHT_API_LLM_" in r.stderr, "the refusal names the key"
    assert p.read_text() == REFUSED[case] and backups(tmp_path) == []
    for needle in (GROK, ACCOUNT1, ACCOUNT2, "xai-oauth", "anthropic"):
        assert needle not in r.stdout + r.stderr, needle


# ---------------------------------------------------------------- usage

def test_env_file_is_required_there_is_no_default_path(tmp_path):
    r = run()
    assert r.returncode == 2 and "--env-file" in r.stderr
    r = run("--apply")
    assert r.returncode == 2


def test_a_missing_env_file_is_an_error(tmp_path):
    r = run("--env-file", str(tmp_path / "nope.env"))
    assert r.returncode == 1 and "cannot read" in r.stderr


def test_a_failing_write_keeps_the_original(tmp_path, monkeypatch):
    """The backup is taken first and the replace is atomic: a failure between them leaves the original intact."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("llm_migrate_opus", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    p = put(tmp_path, PROD_SHAPE)

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(mod.os, "replace", boom)
    assert mod.main(["--env-file", str(p), "--apply"]) == 1
    assert p.read_text() == PROD_SHAPE
    assert len(backups(tmp_path)) == 1
    assert [x.name for x in tmp_path.iterdir() if x.name.startswith(".llm-migrate")] == [], "temp file removed"


# ---------------------------------------------------------------- keys under LLM_1_/LLM_2_ whose move would be a guess

UNMOVABLE = {
    "member1-base-url": "HINDSIGHT_API_LLM_1_BASE_URL=https://example.invalid/v1\n",
    "member1-timeout": "HINDSIGHT_API_LLM_1_TIMEOUT=90\n",
    "member1-reasoning-effort": "HINDSIGHT_API_LLM_1_REASONING_EFFORT=low\n",
    "member2-max-retries": "HINDSIGHT_API_LLM_2_MAX_RETRIES=1\n",
    "member3-provider": "HINDSIGHT_API_LLM_3_PROVIDER=claude-code\n",
    "member3-token": f"HINDSIGHT_API_LLM_3_API_KEY={OTHER}\n",
    "member10-model": "HINDSIGHT_API_LLM_10_MODEL=claude-sonnet-5-5\n",
}

@pytest.mark.parametrize("flag", [[], ["--apply"]], ids=["dry-run", "apply"])
@pytest.mark.parametrize("case", sorted(UNMOVABLE))
def test_member_keys_without_a_defined_move_are_refused_by_name(tmp_path, case, flag):
    text = PROD_SHAPE + UNMOVABLE[case]
    p = put(tmp_path, text)
    r = run("--env-file", str(p), *flag)
    assert r.returncode == 1 and "refused" in r.stderr, r.stdout + r.stderr
    name = UNMOVABLE[case].split("=", 1)[0]
    assert name in r.stderr, "the refusal names the key"
    assert r.stdout == "" and p.read_text() == text and backups(tmp_path) == []
    for needle in (GROK, ACCOUNT1, ACCOUNT2, OTHER, "example.invalid"):
        assert needle not in r.stdout + r.stderr, needle


def test_the_defined_member_keys_and_other_global_llm_keys_are_not_unmovable(tmp_path):
    """PROVIDER/MODEL/API_KEY of members 1 and 2 move; global LLM_TIMEOUT/REASONING_EFFORT are not member keys."""
    p = put(tmp_path, PROD_SHAPE)
    assert run("--env-file", str(p)).returncode == 0


# ---------------------------------------------------------------- --classify (read only; scripts/next-stack.sh uses it)

def test_classify_names_both_layouts_and_changes_nothing(tmp_path):
    old, new = put(tmp_path, PROD_SHAPE, name="old.env"), put(tmp_path, TARGET, name="new.env")
    for path, want in ((old, "layout: old"), (new, "layout: target")):
        before = path.read_bytes()
        r = run("--env-file", str(path), "--classify")
        assert r.returncode == 0 and r.stdout.strip() == want, r.stdout + r.stderr
        assert path.read_bytes() == before and backups(tmp_path) == []


def test_classify_tolerates_member_extras_that_migrate_refuses(tmp_path):
    """next-stack only reads the two tokens; it must not become stricter than it was."""
    p = put(tmp_path, PROD_SHAPE + "HINDSIGHT_API_LLM_1_TIMEOUT=90\nHINDSIGHT_API_LLM_3_PROVIDER=claude-code\n")
    r = run("--env-file", str(p), "--classify")
    assert r.returncode == 0 and r.stdout.strip() == "layout: old"


@pytest.mark.parametrize("text", [
    "HC_PROFILE=x\nHINDSIGHT_API_LLM_PROVIDER=none\n",
    REFUSED["member2-anthropic"], REFUSED["member1-token-empty"], REFUSED["tokens-identical"],
    TARGET + "XAI_API_KEY=x\n",
], ids=["none-profile", "member2-anthropic", "member1-token-empty", "tokens-identical", "target-plus-leftover"])
def test_classify_refuses_anything_else_with_key_names_only(tmp_path, text):
    p = put(tmp_path, text)
    r = run("--env-file", str(p), "--classify")
    assert r.returncode == 1 and "neither the pre-migration layout nor the target layout" in r.stderr
    for needle in (GROK, ACCOUNT1, ACCOUNT2, "anthropic"):
        assert needle not in r.stdout + r.stderr


def test_classify_and_apply_exclude_each_other(tmp_path):
    p = put(tmp_path, PROD_SHAPE)
    assert run("--env-file", str(p), "--classify", "--apply").returncode == 2
