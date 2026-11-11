"""verify/gate9_llm_fallback.sh judgment (issue #248): every LLM slot runs claude-code/claude-sonnet-5-5 on a failover
chain account1 -> account2; the none profile (CI, bootstrap) is consistent without touching the container.

Hermetic. The gate runs for real in a tmp copy of verify/. A stub `docker` answers `compose ps`/`inspect`, reports a CLI
version for `exec <container> <cli> -v`, and runs the gate's in-container script for `exec <container> python -c ...` with
the host python, a FAKE `hindsight_api` package (this file's model of upstream: get_config() read from the container env,
and `_build_llm` with the same inheritance as v1.0.0 memory_engine.py 119-168) and a container environment made from an
env file by `set -a; . file`. The judgment code therefore runs for real; the live proof that the model matches the image
is the run against the copy stack (PR table). All tokens are fake strings; no test may reach a real docker or the gateway.
"""
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ACCOUNT1, ACCOUNT2, OTHER = "FAKE-ACCOUNT1-TOKEN-1111aaaa", "FAKE-ACCOUNT2-TOKEN-2222bbbb", "FAKE-OTHER-TOKEN-3333cccc"
CONTAINER = "hindsight-crew-hindsight-1"
SLOTS = ("default", "retain", "reflect", "consolidation", "mental_model_refresh")

FAKE_CONFIG = '''
import json, os
from types import SimpleNamespace

def _members(prefix):
    out, n = [], 1
    while "HINDSIGHT_API_%sLLM_%d_PROVIDER" % (prefix, n) in os.environ:
        g = lambda f: os.environ.get("HINDSIGHT_API_%sLLM_%d_%s" % (prefix, n, f)) or None
        out.append(SimpleNamespace(provider=g("PROVIDER"), model=g("MODEL"), api_key=g("API_KEY")))
        n += 1
    return out

def _strategy(prefix):
    raw = os.environ.get("HINDSIGHT_API_%sLLM_STRATEGY" % prefix)
    return SimpleNamespace(mode=json.loads(raw)["mode"]) if raw else None

def get_config():
    if os.environ.get("FAKE_CONFIG_RAISE"):   # an upstream config error that quotes a value
        raise ValueError("bad value %s" % os.environ.get("HINDSIGHT_API_LLM_API_KEY"))
    c = SimpleNamespace()
    for env_prefix, attr in (("", ""), ("RETAIN_", "retain_"), ("REFLECT_", "reflect_"),
                             ("CONSOLIDATION_", "consolidation_"), ("MENTAL_MODEL_REFRESH_", "mental_model_refresh_")):
        for f in ("provider", "model", "api_key"):
            setattr(c, attr + "llm_" + f, os.environ.get("HINDSIGHT_API_%sLLM_%s" % (env_prefix, f.upper())) or None)
        setattr(c, attr + "llm_members", _members(env_prefix))
        setattr(c, attr + "llm_strategy", _strategy(env_prefix))
    return c
'''

FAKE_ENGINE = '''
from collections import namedtuple
_LLMCallDefaults = namedtuple("_LLMCallDefaults", "timeout max_retries initial_backoff max_backoff")

class MultiLLMProvider:
    def __init__(self, members, strategy):
        self.members, self.strategy = members, strategy

def _build_llm(base, config, prefix, defaults, fallback_prefix=""):
    members = getattr(config, prefix + "llm_members")
    strategy = getattr(config, prefix + "llm_strategy")
    if prefix:
        if not members:
            members = getattr(config, fallback_prefix + "llm_members")
        if strategy is None:
            strategy = getattr(config, fallback_prefix + "llm_strategy")
    if not strategy or not members:
        return base
    return MultiLLMProvider([base, *members], strategy)
'''

STUB_DOCKER = r"""#!/bin/bash
{ printf 'argv='; printf '[%s]' "$@"; echo; } >> "${STUB_LOG:?}"
case "$1" in
  compose)
    case " $* " in
      *" ps -q hindsight "*) [ -n "${STUB_PS_ID:-}" ] && echo "$STUB_PS_ID"; exit 0 ;;
    esac
    exit 1 ;;
  inspect) echo "/${STUB_NAME:-${@: -1}}" ;;
  exec)
    c="$2"; shift 2
    if [ "$c" != "${STUB_EXPECT_CONTAINER:?}" ]; then echo "Error: No such container: $c" >&2; exit 1; fi
    case "$1" in
      */claude) v="${STUB_CLI_VERSION-2.1.282}"; [ -z "$v" ] || echo "$v (Claude Code)" ;;
      python)
        shift
        exec env -i PATH="$PATH" PYTHONPATH="${STUB_FAKE_API:?}" bash -c 'set -a; . "$1"; set +a; shift; exec "$@"' _ \
          "${STUB_CONTAINER_ENV:?}" "${STUB_PYTHON:?}" "$@" ;;
    esac ;;
  *) echo "stub docker: unexpected call: $*" >&2; exit 97 ;;
esac
"""

BASE_ENV = [
    "HC_PROFILE=ko-full",
    "HINDSIGHT_API_LLM_PROVIDER=claude-code",
    "HINDSIGHT_API_LLM_MODEL=claude-sonnet-5-5",
    "HINDSIGHT_API_LLM_API_KEY=" + ACCOUNT1,
    "HINDSIGHT_API_LLM_STRATEGY='{\"mode\":\"failover\"}'",
    "HINDSIGHT_API_LLM_1_PROVIDER=claude-code",
    "HINDSIGHT_API_LLM_1_MODEL=claude-sonnet-5-5",
    "HINDSIGHT_API_LLM_1_API_KEY=" + ACCOUNT2,
    "HINDSIGHT_API_MENTAL_MODEL_REFRESH_LLM_STRATEGY='{\"mode\":\"failover\"}'",
    "HINDSIGHT_API_MENTAL_MODEL_REFRESH_LLM_1_PROVIDER=claude-code",
    "HINDSIGHT_API_MENTAL_MODEL_REFRESH_LLM_1_MODEL=claude-sonnet-5-5",
    "HINDSIGHT_API_MENTAL_MODEL_REFRESH_LLM_1_API_KEY=" + ACCOUNT2,
]


def key_of(line):
    return line.split("=", 1)[0]


def edit(lines, drop=(), set_=None, add=()):
    """BASE_ENV edits: drop keys (exact name), replace values of existing keys, append raw lines."""
    out = []
    for ln in lines:
        k = key_of(ln)
        if k in drop:
            continue
        if set_ and k in set_:
            ln = "%s=%s" % (k, set_[k])
        out.append(ln)
    return out + list(add)


def write_exec(path, text):
    path.write_text(text)
    path.chmod(0o755)


@pytest.fixture
def box(tmp_path):
    root = tmp_path / "repo"
    shutil.copytree(REPO / "verify", root / "verify")
    (root / "secrets").mkdir()
    (root / "secrets" / "demo-tokens.env").write_text("HC_DEMO_TOKEN_ALICE=demo\n")
    api = tmp_path / "fake-api" / "hindsight_api"
    (api / "engine").mkdir(parents=True)
    (api / "__init__.py").write_text("")
    (api / "config.py").write_text(textwrap.dedent(FAKE_CONFIG))
    (api / "engine" / "__init__.py").write_text("")
    (api / "engine" / "memory_engine.py").write_text(textwrap.dedent(FAKE_ENGINE))
    (tmp_path / "bin").mkdir()
    write_exec(tmp_path / "bin" / "docker", STUB_DOCKER)
    return Box(tmp_path, root)


class Box:
    def __init__(self, tmp, root):
        self.tmp, self.root = tmp, root
        self.log = tmp / "docker.log"

    def write_env(self, lines, name=".env.local"):
        p = self.root / name
        p.write_text("\n".join(lines) + "\n")
        return p

    def run(self, lines=None, container_lines=None, **extra):
        env_file = self.write_env(BASE_ENV if lines is None else lines)
        container_env = env_file if container_lines is None else self.write_env(container_lines, "container.env")
        env = {
            "PATH": "%s:%s" % (self.tmp / "bin", os.environ["PATH"]), "HOME": str(self.tmp),
            "STUB_LOG": str(self.log), "STUB_PS_ID": "cid", "STUB_NAME": CONTAINER, "STUB_EXPECT_CONTAINER": CONTAINER,
            "STUB_CONTAINER_ENV": str(container_env), "STUB_FAKE_API": str(self.tmp / "fake-api"),
            "STUB_PYTHON": sys.executable,
        }
        env.update(extra)
        env = {k: v for k, v in env.items() if v is not None}
        return subprocess.run(["bash", str(self.root / "verify" / "gate9_llm_fallback.sh")], capture_output=True,
                              text=True, env=env, cwd=self.tmp, timeout=60)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []


def no_token_leak(r, box):
    blob = r.stdout + r.stderr + "\n".join(box.calls())
    for t in (ACCOUNT1, ACCOUNT2, OTHER):
        assert t not in blob, "a token value reached the output or a docker argv"


# ---------------------------------------------------------------- the target configuration

def test_target_configuration_is_green_on_all_five_slots(box):
    r = box.run()
    assert r.returncode == 0, r.stderr
    for slot in SLOTS:
        assert re.search(r"slot %s=claude-code/claude-sonnet-5-5>claude-code/claude-sonnet-5-5 \[failover\]" % slot, r.stderr), slot
    assert "cli=2.1.282" in r.stderr
    no_token_leak(r, box)
    execs = [c for c in box.calls() if c.startswith("argv=[exec]")]
    assert len(execs) == 2 and all(c.startswith("argv=[exec][%s]" % CONTAINER) for c in execs)
    assert not any(c.startswith("argv=[run]") for c in box.calls()), "no throwaway container: the image is not needed"


def test_only_one_way_fingerprints_of_the_tokens_reach_the_container_argv(box):
    r = box.run()
    assert r.returncode == 0
    m = re.search(r"\]\[([0-9a-f]{12})\]\[([0-9a-f]{12})\]\[claude-sonnet-5-5\]\[chain\]\n", box.log.read_text())
    assert m and m.group(1) != m.group(2)


# ---------------------------------------------------------------- negative cases (each must be RED)

NEG = {
    "members-share-one-token": dict(set_={"HINDSIGHT_API_LLM_1_API_KEY": ACCOUNT1, "HINDSIGHT_API_MENTAL_MODEL_REFRESH_LLM_1_API_KEY": ACCOUNT1},
                                    want="same token"),
    "primary-is-xai-oauth": dict(set_={"HINDSIGHT_API_LLM_PROVIDER": "xai-oauth"}, want="member 0 provider=xai-oauth"),
    "fallback-member-is-not-claude-code": dict(set_={"HINDSIGHT_API_LLM_1_PROVIDER": "anthropic"}, want="member 1 provider=anthropic"),
    "primary-model-mismatch": dict(set_={"HINDSIGHT_API_LLM_MODEL": "claude-opus-4-8"}, want="model=claude-opus-4-8"),
    "member-model-mismatch": dict(set_={"HINDSIGHT_API_LLM_1_MODEL": "claude-opus-5-5"}, want="model=claude-opus-5-5"),
    "strategy-missing": dict(drop=("HINDSIGHT_API_LLM_STRATEGY",), want="chain has 1 member(s)"),
    "strategy-round-robin": dict(set_={"HINDSIGHT_API_LLM_STRATEGY": "'{\"mode\":\"round-robin\"}'"}, want="strategy=round-robin"),
    "no-global-chain": dict(drop=("HINDSIGHT_API_LLM_STRATEGY", "HINDSIGHT_API_LLM_1_PROVIDER", "HINDSIGHT_API_LLM_1_MODEL",
                                  "HINDSIGHT_API_LLM_1_API_KEY"), want="default: chain has 1 member(s)"),
    "refresh-slot-without-chain": dict(drop=("HINDSIGHT_API_MENTAL_MODEL_REFRESH_LLM_STRATEGY",
                                             "HINDSIGHT_API_MENTAL_MODEL_REFRESH_LLM_1_PROVIDER",
                                             "HINDSIGHT_API_MENTAL_MODEL_REFRESH_LLM_1_MODEL",
                                             "HINDSIGHT_API_MENTAL_MODEL_REFRESH_LLM_1_API_KEY"),
                                       want="mental_model_refresh: chain has 1 member(s)"),
    "refresh-slot-chain-has-account1-twice": dict(set_={"HINDSIGHT_API_MENTAL_MODEL_REFRESH_LLM_1_API_KEY": ACCOUNT1},
                                            want="mental_model_refresh: chain members share a token"),
    "slot-primary-overridden-to-another-token": dict(add=["HINDSIGHT_API_RETAIN_LLM_API_KEY=" + OTHER],
                                                     want="retain: member 0 token is not the env file account1 key"),
    "xai-key-remains": dict(add=["XAI_API_KEY=" + OTHER], want="xai keys remain in the env file: XAI_API_KEY"),
    "xai-named-llm-key-remains": dict(add=["HINDSIGHT_API_LLM_XAI_AUTH_DIR=/x"], want="HINDSIGHT_API_LLM_XAI_AUTH_DIR"),
    "slot-provider-is-xai-oauth": dict(add=["HINDSIGHT_API_REFLECT_LLM_PROVIDER=xai-oauth"],
                                       want="xai provider declared in the env file: HINDSIGHT_API_REFLECT_LLM_PROVIDER"),
    "primary-token-empty": dict(set_={"HINDSIGHT_API_LLM_API_KEY": ""}, want="HINDSIGHT_API_LLM_API_KEY (account1) is empty"),
    "member-token-empty": dict(set_={"HINDSIGHT_API_LLM_1_API_KEY": ""}, want="HINDSIGHT_API_LLM_1_API_KEY (account2) is empty"),
    "member-key-not-declared": dict(drop=("HINDSIGHT_API_LLM_1_API_KEY",), want="HINDSIGHT_API_LLM_1_API_KEY is not declared"),
}


@pytest.mark.parametrize("case", sorted(NEG))
def test_a_broken_configuration_is_red_with_a_reason(box, case):
    spec = NEG[case]
    lines = edit(BASE_ENV, drop=spec.get("drop", ()), set_=spec.get("set_"), add=spec.get("add", ()))
    r = box.run(lines)
    assert r.returncode == 1, r.stderr
    assert spec["want"] in r.stderr, r.stderr
    no_token_leak(r, box)


def test_chain_order_swapped_in_the_container_is_red(box):
    """The container serves account2 first although the env file says account1 first (a stale container, or the env file edited)."""
    swapped = edit(BASE_ENV, set_={"HINDSIGHT_API_LLM_API_KEY": ACCOUNT2, "HINDSIGHT_API_LLM_1_API_KEY": ACCOUNT1,
                                   "HINDSIGHT_API_MENTAL_MODEL_REFRESH_LLM_1_API_KEY": ACCOUNT1})
    r = box.run(BASE_ENV, container_lines=swapped)
    assert r.returncode == 1
    assert "member 0 token is not the env file account1 key" in r.stderr and "member 1 token is not the env file account2 key" in r.stderr
    no_token_leak(r, box)


def test_xai_key_in_the_container_environment_only_is_red(box):
    r = box.run(BASE_ENV, container_lines=BASE_ENV + ["HINDSIGHT_API_XAI_OAUTH_DIR=/x"])
    assert r.returncode == 1 and "xai keys in the container environment: HINDSIGHT_API_XAI_OAUTH_DIR" in r.stderr


@pytest.mark.parametrize("version,rc", [("2.1.150", 1), ("2.1.279", 1), ("2.1.280", 0), ("2.1.282", 0), ("2.2.0", 0), ("3.0.1", 0)])
def test_cli_version_floor(box, version, rc):
    r = box.run(STUB_CLI_VERSION=version)
    assert r.returncode == rc, r.stderr
    if rc:
        assert "CLI %s < 2.1.280" % version in r.stderr


def test_cli_that_reports_nothing_is_red(box):
    r = box.run(STUB_CLI_VERSION="")
    assert r.returncode == 1 and "did not report a version" in r.stderr


# ---------------------------------------------------------------- none profile

NONE_ENV = ["HC_PROFILE=cpu-en", "HINDSIGHT_API_EMBEDDINGS_PROVIDER=local", "HINDSIGHT_API_LLM_PROVIDER=none"]


@pytest.mark.parametrize("env", [NONE_ENV, [NONE_ENV[0], "HINDSIGHT_API_LLM_PROVIDER='none'"],
                                 ["# HINDSIGHT_API_REFLECT_LLM_PROVIDER=xai-oauth"] + NONE_ENV],
                         ids=["plain", "quoted", "commented-slot-key-does-not-count"])
def test_none_profile_is_consistent_without_exec_or_run(box, env):
    r = box.run(env)
    assert r.returncode == 0, r.stderr
    assert "LLM none profile - consistent" in r.stderr
    assert not [c for c in box.calls() if c.startswith("argv=[exec]") or c.startswith("argv=[run]")]
    assert r.stderr.count("target: ") == 1, "lib.sh still reports the target"


def test_none_profile_with_no_container_running_is_still_consistent(box):
    r = box.run(NONE_ENV, STUB_PS_ID="")
    assert r.returncode == 0 and "LLM none profile - consistent" in r.stderr


NONE_PLUS = {
    "slot-provider": "HINDSIGHT_API_REFLECT_LLM_PROVIDER=xai-oauth",
    "slot-provider-none": "HINDSIGHT_API_RETAIN_LLM_PROVIDER=none",
    "member-provider": "HINDSIGHT_API_LLM_1_PROVIDER=claude-code",
    "member-key": "HINDSIGHT_API_LLM_1_API_KEY=" + ACCOUNT2,
    "slot-member-key": "HINDSIGHT_API_CONSOLIDATION_LLM_1_API_KEY=" + ACCOUNT2,
    "refresh-strategy": "HINDSIGHT_API_MENTAL_MODEL_REFRESH_LLM_STRATEGY='{\"mode\":\"failover\"}'",
    "global-model": "HINDSIGHT_API_LLM_MODEL=claude-sonnet-5-5",
}


@pytest.mark.parametrize("extra", sorted(NONE_PLUS))
def test_global_none_with_any_other_llm_key_is_judged_in_full(box, extra):
    """Global none does not excuse a slot-level provider or a member key: the gate execs and finds the problem."""
    r = box.run(NONE_ENV + [NONE_PLUS[extra]])
    assert r.returncode == 1, r.stderr
    assert "LLM none profile" not in r.stderr
    assert any(c.startswith("argv=[exec]") for c in box.calls())
    no_token_leak(r, box)


def test_global_none_plus_reflect_slot_xai_names_the_slot_and_the_key(box):
    r = box.run(NONE_ENV + ["HINDSIGHT_API_REFLECT_LLM_PROVIDER=xai-oauth"])
    assert r.returncode == 1
    assert "xai provider declared in the env file: HINDSIGHT_API_REFLECT_LLM_PROVIDER" in r.stderr
    assert "reflect: member 0 provider=xai-oauth" in r.stderr


def test_the_caller_environment_cannot_make_an_env_file_without_the_key_a_none_profile(box):
    """HINDSIGHT_API_LLM_PROVIDER=none in the caller's shell is not the env file's declaration."""
    r = box.run(["HC_PROFILE=x"], HINDSIGHT_API_LLM_PROVIDER="none")
    assert "LLM none profile" not in r.stderr and r.returncode == 1


# ---------------------------------------------------------------- single-account profile (what ./install --claude-token-env writes)

SINGLE = ["HC_PROFILE=ko-full", "HINDSIGHT_API_LLM_PROVIDER=claude-code", "HINDSIGHT_API_LLM_MODEL=claude-sonnet-5-5",
          "HINDSIGHT_API_LLM_API_KEY=" + ACCOUNT1]


def test_single_account_profile_is_green_without_a_chain(box):
    r = box.run(SINGLE)
    assert r.returncode == 0, r.stderr
    assert "single-account profile - no failover chain" in r.stderr
    for slot in SLOTS:
        assert re.search(r"slot %s=claude-code/claude-sonnet-5-5 \[no-chain\]" % slot, r.stderr), slot
    no_token_leak(r, box)


SINGLE_RED = {
    "model-mismatch": (SINGLE[:2] + ["HINDSIGHT_API_LLM_MODEL=claude-opus-4-8", SINGLE[3]], "model=claude-opus-4-8"),
    "model-missing": (SINGLE[:2] + [SINGLE[3]], "model="),
    "xai-key-remains": (SINGLE + ["XAI_API_KEY=" + OTHER], "xai keys remain in the env file: XAI_API_KEY"),
    "xai-named-llm-key": (SINGLE + ["HINDSIGHT_API_LLM_XAI_AUTH_DIR=/x"], "HINDSIGHT_API_LLM_XAI_AUTH_DIR"),
}


@pytest.mark.parametrize("case", sorted(SINGLE_RED))
def test_single_account_profile_still_judges_model_and_xai(box, case):
    lines, want = SINGLE_RED[case]
    r = box.run(lines)
    assert r.returncode == 1 and want in r.stderr, r.stderr
    assert "single-account profile" in r.stderr
    no_token_leak(r, box)


def test_single_account_profile_still_judges_the_cli(box):
    r = box.run(SINGLE, STUB_CLI_VERSION="2.1.150")
    assert r.returncode == 1 and "CLI 2.1.150 < 2.1.280" in r.stderr


def test_single_account_with_an_xai_key_only_in_the_container_environment_is_red(box):
    r = box.run(SINGLE, container_lines=SINGLE + ["HINDSIGHT_API_XAI_OAUTH_DIR=/x"])
    assert r.returncode == 1 and "xai keys in the container environment" in r.stderr


HALF = {
    "member-1-token-only": SINGLE + ["HINDSIGHT_API_LLM_1_API_KEY=" + ACCOUNT2],
    "member-1-without-strategy": SINGLE + ["HINDSIGHT_API_LLM_1_PROVIDER=claude-code", "HINDSIGHT_API_LLM_1_MODEL=claude-sonnet-5-5",
                                           "HINDSIGHT_API_LLM_1_API_KEY=" + ACCOUNT2],
    "member-1-provider-only": SINGLE + ["HINDSIGHT_API_LLM_1_PROVIDER=claude-code"],
    "slot-key": SINGLE + ["HINDSIGHT_API_RETAIN_LLM_MODEL=claude-sonnet-5-5"],
    "refresh-chain-keys-only": SINGLE + ["HINDSIGHT_API_MENTAL_MODEL_REFRESH_LLM_STRATEGY='{\"mode\":\"failover\"}'",
                                         "HINDSIGHT_API_MENTAL_MODEL_REFRESH_LLM_1_PROVIDER=claude-code",
                                         "HINDSIGHT_API_MENTAL_MODEL_REFRESH_LLM_1_MODEL=claude-sonnet-5-5",
                                         "HINDSIGHT_API_MENTAL_MODEL_REFRESH_LLM_1_API_KEY=" + ACCOUNT2],
}


@pytest.mark.parametrize("case", sorted(HALF))
def test_one_member_or_slot_key_makes_it_the_strict_chain_judgment(box, case):
    r = box.run(HALF[case])
    assert r.returncode == 1, r.stderr
    assert "single-account profile" not in r.stderr
    no_token_leak(r, box)


def test_the_primary_other_than_claude_code_is_never_a_single_account_profile(box):
    r = box.run([SINGLE[0], "HINDSIGHT_API_LLM_PROVIDER=xai-oauth", SINGLE[2], SINGLE[3]])
    assert r.returncode == 1 and "single-account profile" not in r.stderr


# ---------------------------------------------------------------- introspection failures leak nothing

def test_a_config_error_prints_only_the_exception_class_never_a_traceback_or_a_value(box):
    r = box.run(BASE_ENV, container_lines=BASE_ENV + ["FAKE_CONFIG_RAISE=1"])
    assert r.returncode == 1
    assert "config introspection failed: ValueError" in r.stderr
    assert "Traceback" not in r.stderr and "bad value" not in r.stderr
    no_token_leak(r, box)


def test_python_that_cannot_even_start_in_the_container_is_a_fixed_message(box):
    r = box.run(BASE_ENV, STUB_PYTHON="/nonexistent/python")
    assert r.returncode == 1 and "config introspection failed" in r.stderr
    assert "nonexistent" not in r.stderr
