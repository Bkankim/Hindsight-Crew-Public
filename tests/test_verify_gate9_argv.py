"""verify/ (issue #216): gate9 finds the hindsight container through compose, and no verify
script puts the Bearer token on a curl argv.

Every run is hermetic. The tests copy verify/ (and scripts/mcp-probe.sh, which verify/lib.sh
sources) into a tmp root, put stub `docker` and `curl` first on PATH and name a dummy image. No
test may reach a real docker, the gateway or the operational stack.

Stub docker answers three calls only and records every call (cwd + argv) in $STUB_LOG:
  exec <STUB_EXPECT_CONTAINER> <cli> -v      -> "2.1.282 (Claude Code)"
  exec <STUB_EXPECT_CONTAINER> python -c ... -> an empty problem report; any other container name is
                                  "No such container" (exit 1) - this is what a fixed name sees
                                  when the clone lives in a differently named directory
  compose --env-file <root>/.env.local --project-directory <root> -f <root>/docker-compose.yml ps -q hindsight
                               -> $STUB_PS_ID (STUB_PS_FAIL=1 prints a marker on stderr, exits 1);
                                  any other compose call is rejected
  inspect ... <id>             -> "/<id>" (the container name verify/lib.sh converts the id to)
Stub curl records one argv element per line in $STUB_ARGV_LOG, copies stdin to $STUB_STDIN_LOG when
a header is read from it (`-H @-`), and answers like the gateway ($STUB_CODE as the status code).
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FAKE_TOKEN = "fake-tok-0123456789abcdef"
FIXED_NAME = "hindsight-crew-hindsight-1"
COMPOSE_ID = "otherproj-hindsight-42"

STUB_DOCKER = r"""#!/bin/bash
{ printf 'cwd=%s argv=' "$PWD"; printf '[%s]' "$@"; echo; } >> "${STUB_LOG:?}"
case "$1" in
  exec)
    if [ "$2" != "${STUB_EXPECT_CONTAINER:?}" ]; then echo "Error: No such container: $2" >&2; exit 1; fi
    case "$3" in
      */claude) echo "2.1.282 (Claude Code)" ;;
      python) echo "slot default=claude-code/claude-sonnet-5-5>claude-code/claude-sonnet-5-5 [failover]"; echo "PROBLEMS=" ;;
    esac ;;
  compose)
    case "$*" in
      "compose --env-file ${STUB_ROOT:?}/.env.local --project-directory ${STUB_ROOT} -f ${STUB_ROOT}/docker-compose.yml ps -q hindsight")
                          [ "${STUB_PS_FAIL:-}" = 1 ] && { echo "SECRET-IN-COMPOSE-STDERR" >&2; exit 1; }
                          [ -n "${STUB_PS_ID:-}" ] && echo "$STUB_PS_ID"; exit 0 ;;
    esac
    echo "stub docker: unexpected compose call: $*" >&2; exit 1 ;;
  inspect) echo "/${@: -1}" ;;
  *) echo "stub docker: unexpected call: $*" >&2; exit 1 ;;
esac
"""

STUB_CURL = r"""#!/bin/bash
printf '%s\n' "$@" >> "${STUB_ARGV_LOG:?}"
w=""; hdr_stdin=0; incl=0; prev=""
for a in "$@"; do
  [ "$prev" = -H ] && [ "$a" = "@-" ] && hdr_stdin=1
  [ "$prev" = -w ] && w="$a"
  [ "$a" = -i ] && incl=1
  prev="$a"
done
[ "$hdr_stdin" = 1 ] && cat >> "${STUB_STDIN_LOG:?}"
[ "$incl" = 1 ] && printf 'HTTP/1.1 200 OK\r\nMcp-Session-Id: sess-1\r\n\r\n{}'
[ -n "$w" ] && printf '%b' "${w//%\{http_code\}/${STUB_CODE:-200}}"
exit 0
"""


def _write_exec(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


@pytest.fixture
def root(tmp_path):
    """A tmp copy of the repo parts the gates need, plus stubs on `bin/`."""
    r = tmp_path / "clone-with-another-dirname"
    shutil.copytree(REPO / "verify", r / "verify")
    (r / "scripts").mkdir()
    shutil.copy(REPO / "scripts" / "mcp-probe.sh", r / "scripts" / "mcp-probe.sh")
    (r / "secrets").mkdir()
    (r / "secrets" / "demo-tokens.env").write_text(f"HC_DEMO_TOKEN_ALICE={FAKE_TOKEN}\n")
    # a declared chain, so the gate runs its full judgment (the none profile never execs); the stub reports no problem
    (r / ".env.local").write_text("HINDSIGHT_API_LLM_API_KEY=fake-account1\nHINDSIGHT_API_LLM_1_API_KEY=fake-account2\n")
    (r / "bin").mkdir()
    _write_exec(r / "bin" / "docker", STUB_DOCKER)
    _write_exec(r / "bin" / "curl", STUB_CURL)
    return r


def _env(root: Path, **extra):
    env = {
        "PATH": f"{root / 'bin'}:{os.environ['PATH']}",
        "HOME": str(root),
        "STUB_ROOT": str(root),
        "STUB_LOG": str(root / "docker.log"),
        "STUB_ARGV_LOG": str(root / "curl-argv.log"),
        "STUB_STDIN_LOG": str(root / "curl-stdin.log"),
    }
    env.update(extra)
    return env


def _gate9(root: Path, **extra):
    return subprocess.run(["bash", str(root / "verify" / "gate9_llm_fallback.sh")],
                          capture_output=True, text=True, env=_env(root, **extra), cwd=root.parent, timeout=60)


def _docker_calls(root: Path):
    p = root / "docker.log"
    return p.read_text().splitlines() if p.exists() else []


# ---- container lookup -----------------------------------------------------------------------

def test_gate9_uses_the_container_id_compose_reports_for_this_clone(root):
    """The clone lives in another directory, so compose's project (and container) name differs
    from the old fixed `hindsight-crew-hindsight-1`; the gate must take compose's answer."""
    r = _gate9(root, STUB_PS_ID=COMPOSE_ID, STUB_EXPECT_CONTAINER=COMPOSE_ID)
    assert r.returncode == 0, r.stderr
    calls = _docker_calls(root)
    assert any(f"[exec][{COMPOSE_ID}][python][-c]" in c for c in calls)
    assert not any(FIXED_NAME in c for c in calls)
    ps = [c for c in calls if "[ps][-q][hindsight]" in c]
    assert len(ps) == 1
    # the contract's compose shape: env file, project directory and compose file of THIS clone, so a
    # stray .env or COMPOSE_* in the caller's shell cannot select another project (no -p unless asked)
    assert ps[0].endswith(f"argv=[compose][--env-file][{root}/.env.local][--project-directory][{root}]"
                          f"[-f][{root}/docker-compose.yml][ps][-q][hindsight]")


def test_env_container_name_wins_over_compose(root):
    r = _gate9(root, HC_HINDSIGHT_CONTAINER="from-env-name", STUB_PS_ID=COMPOSE_ID,
               STUB_EXPECT_CONTAINER="from-env-name")
    assert r.returncode == 0, r.stderr
    calls = _docker_calls(root)
    assert any("[exec][from-env-name][python][-c]" in c for c in calls)
    assert not any("[ps]" in c for c in calls), "env override must not even ask compose"


@pytest.mark.parametrize("extra", [{"STUB_PS_ID": ""}, {"STUB_PS_FAIL": "1"}], ids=["no-container", "compose-fails"])
def test_missing_container_is_red_with_a_reason(root, extra):
    r = _gate9(root, STUB_EXPECT_CONTAINER=COMPOSE_ID, **extra)
    assert r.returncode == 1
    assert "hindsight container not found" in r.stderr
    assert "HC_HINDSIGHT_CONTAINER" in r.stderr, "the reason must name the env override"
    assert "SECRET-IN-COMPOSE-STDERR" not in r.stderr, "compose's stderr can quote .env.local lines"
    if "STUB_PS_FAIL" in extra:
        assert "exit 1" in r.stderr
    assert not any("[exec]" in c for c in _docker_calls(root)), "must stop before docker exec"


# ---- token never on a curl argv -------------------------------------------------------------

def _run_bash(root: Path, script: str, **extra):
    env = _env(root, HC_TEST_TOKEN=FAKE_TOKEN, **extra)
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, cwd=root, timeout=60)


TOKEN_CASES = {
    "rest_code": '. verify/lib.sh; rest_code POST "$HC_TEST_TOKEN" /v1/default/banks/b/memories/recall \'{"query":"x"}\'',
    "rest_body": '. verify/lib.sh; rest_body GET "$HC_TEST_TOKEN" /v1/default/banks',
    "mcp_call": '. verify/lib.sh; mcp_call "$HC_TEST_TOKEN" personal-alice \'{"jsonrpc":"2.0","id":2,"method":"tools/list"}\'',
    "gate5": "bash verify/gate5_adversarial.sh",
}


@pytest.mark.parametrize("case", list(TOKEN_CASES))
def test_token_reaches_curl_on_stdin_not_argv(root, case):
    r = _run_bash(root, TOKEN_CASES[case], STUB_CODE="403" if case == "gate5" else "200")
    assert r.returncode == 0, r.stdout + r.stderr
    argv = (root / "curl-argv.log").read_text()
    assert argv, "the stub curl was never called"
    assert FAKE_TOKEN not in argv
    assert "authorization" not in argv.lower()
    stdin = (root / "curl-stdin.log").read_text()
    assert f"authorization: Bearer {FAKE_TOKEN}" in stdin, "the header must arrive on stdin (-H @-)"


def test_empty_token_sends_no_authorization_header(root):
    r = _run_bash(root, '. verify/lib.sh; rest_code POST "" /v1/default/banks/b/memories/recall \'{"query":"x"}\'')
    assert r.returncode == 0, r.stderr
    assert "@-" not in (root / "curl-argv.log").read_text()
    assert not (root / "curl-stdin.log").exists()


# ---- source-level guards (the issue's completion greps) -------------------------------------

def _verify_scripts():
    return sorted((REPO / "verify").glob("*.sh"))


def test_no_fixed_container_name_in_verify_code():
    for f in _verify_scripts():
        for n, line in enumerate(f.read_text().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            assert FIXED_NAME not in line, f"{f.name}:{n}"


def test_no_authorization_header_literal_in_verify_scripts():
    pat = re.compile(r'-H "authorization|authorization: bearer \$', re.I)
    for f in _verify_scripts():
        for n, line in enumerate(f.read_text().splitlines(), 1):
            assert not pat.search(line), f"{f.name}:{n}"
