"""Target selection contract of #242, gate side (issue #246): verify/lib.sh, verify-all.sh, gate8, gate9 and the sourced
scripts/mcp-probe.sh pick the production stack or the `hc-next` copy from the CALLER's environment, never from a file
they re-read behind the caller's back.

Hermetic: a tmp copy of verify/ and the two scripts lib.sh needs, a stub docker (records every call) and a stub curl.
Nothing real is contacted.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
NEXT_FILES = "%(root)s/docker-compose.yml:%(root)s/docker-compose.next.yml"

STUB_DOCKER = r"""#!/bin/bash
{ printf 'argv='; printf '[%s]' "$@"; echo; } >> "${STUB_LOG:?}"
case "$1" in
  compose)
    case " $* " in
      *" ps -q hindsight "*) [ -n "${STUB_PS_ID:-}" ] && echo "$STUB_PS_ID"; exit "${STUB_PS_RC:-0}" ;;
    esac
    exit 1 ;;
  inspect) echo "/${STUB_NAME:-${@: -1}}" ;;
  exec)
    shift; shift
    case "$1" in
      printenv)
        case "$2" in
          HINDSIGHT_API_RERANKER_PROVIDER|HINDSIGHT_API_EMBEDDINGS_PROVIDER) printf local ;;
          HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL) printf 'BAAI/bge-small-en-v1.5' ;;
          HINDSIGHT_API_RERANKER_LOCAL_MODEL) printf 'fake/local' ;;
        esac ;;
      */claude) echo "2.1.282 (Claude Code)" ;;
      python) printf 'PROBLEMS=\n' ;;
    esac ;;
  *) exit 1 ;;
esac
"""

STUB_CURL = r"""#!/bin/bash
printf '%s\n' "$*" >> "${STUB_CURL_LOG:?}"
printf '{"ok": true, "upstream_ok": true, "upstream": "ok"}\n200'
"""


def write_exec(path, text):
    path.write_text(text)
    path.chmod(0o755)


class Box:
    def __init__(self, tmp_path, name="repo"):
        self.tmp = tmp_path.resolve()
        self.root = self.tmp / name
        self.bin = self.tmp / "bin"
        self.log = self.tmp / "docker.log"
        self.curl_log = self.tmp / "curl.log"
        shutil.copytree(REPO / "verify", self.root / "verify")
        (self.root / "scripts").mkdir()
        for n in ("mcp-probe.sh", "hc-python.sh"):
            shutil.copy2(REPO / "scripts" / n, self.root / "scripts" / n)
        (self.root / "secrets").mkdir()
        (self.root / "secrets" / "demo-tokens.env").write_text("HC_DEMO_TOKEN_ALICE=default-token\n")
        self.bin.mkdir()
        write_exec(self.bin / "docker", STUB_DOCKER)
        write_exec(self.bin / "curl", STUB_CURL)

    def env(self, **extra):
        env = {
            "PATH": "%s:/usr/bin:/bin" % self.bin, "HOME": str(self.tmp), "HC_PYTHON": sys.executable,
            "STUB_LOG": str(self.log), "STUB_CURL_LOG": str(self.curl_log),
            "HC_EMBED_DIM": "384", "HC_HINDSIGHT_IMAGE": "dummy/image:0",
        }
        env.update(extra)
        return {k: v for k, v in env.items() if v is not None}

    def bash(self, snippet, **env):
        return subprocess.run(["/bin/bash", "-c", snippet], cwd=str(self.root), env=self.env(**env),
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)

    def script(self, rel, **env):
        return subprocess.run(["/bin/bash", str(self.root / rel)], cwd=str(self.root), env=self.env(**env),
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)

    def docker_calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def next_env(self, **override):
        """What `next-stack.sh env` exports for a running copy (the env file itself holds no HC_TARGET)."""
        envfile = self.tmp / "next.env"
        envfile.write_text("HC_GATEWAY_BIND=127.0.0.1:18888\nHC_HINDSIGHT_IMAGE=dummy/image:0\nHC_NEXT_ENV=1\n")
        env = {
            "HC_TARGET": "next", "HC_ENV_FILE": str(envfile), "HC_COMPOSE_PROJECT": "hc-next",
            "HC_COMPOSE_FILES": NEXT_FILES % {"root": self.root}, "HC_GATEWAY_BIND": "127.0.0.1:18888",
            "HC_PG0_VOLUME": "hc-next_pg0", "HC_SECRETS_DIR": str(self.root / "secrets"),
        }
        env.update(override)
        return env


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


def target_lines(r):
    return [ln for ln in r.stderr.splitlines() if ln.startswith("target:")]


# ---------------------------------------------------------------- reproduction of the reported defect

def test_caller_gateway_bind_beats_the_env_file(box):
    """Before the fix lib.sh re-read the root .env.local and printed http://127.0.0.1:8888 even when the caller gave
    18888 (plan-review 2026-10-14)."""
    (box.root / ".env.local").write_text("HC_GATEWAY_BIND=127.0.0.1:8888\n")
    r = box.bash('. verify/lib.sh; echo "$HC_GW"', HC_GATEWAY_BIND="127.0.0.1:9100")
    assert r.stdout.strip() == "http://127.0.0.1:9100", r.stderr
    r = box.bash('. verify/lib.sh; echo "$HC_GW"', HC_GATEWAY_BIND="127.0.0.1:18888", HC_TARGET="next",
                 HC_COMPOSE_PROJECT="hc-next")
    assert r.stdout.strip() == "http://127.0.0.1:18888", r.stderr


# ---------------------------------------------------------------- precedence: caller env > HC_ENV_FILE > default

VARS = ["HC_GATEWAY_BIND", "HC_PG0_VOLUME", "HC_HINDSIGHT_CONTAINER", "HC_COMPOSE_PROJECT", "HC_COMPOSE_FILES", "HC_SECRETS_DIR"]


def value_for(box, var, source):
    if var == "HC_SECRETS_DIR":
        d = box.root / ("secrets-" + source)
        d.mkdir(exist_ok=True)
        (d / "demo-tokens.env").write_text("HC_DEMO_TOKEN_ALICE=token-from-%s\n" % source)
        return str(d)
    if var == "HC_GATEWAY_BIND":
        return {"env": "127.0.0.1:9101", "file": "127.0.0.1:9102"}[source]
    return "%s-%s" % (var.lower().replace("_", "-"), source)


@pytest.mark.parametrize("var", VARS)
@pytest.mark.parametrize("source", ["env", "file", "env-empty", "default"])
def test_precedence_of_the_six_contract_variables(box, var, source):
    file_val = value_for(box, var, "file")
    (box.root / "custom.env").write_text("%s=%s\n" % (var, file_val))
    env = {"HC_ENV_FILE": "custom.env"}            # relative: relative to the repo root
    expect_default = {"HC_SECRETS_DIR": str(box.root / "secrets")}.get(var, "<unset>")
    if source == "env":
        env[var] = value_for(box, var, "env")
        expected = env[var]
    elif source == "env-empty":
        env[var] = ""          # set-but-empty beats the file value; the default then applies where there is one
        expected = str(box.root / "secrets") if var == "HC_SECRETS_DIR" else ""
    elif source == "file":
        expected = file_val
    else:
        env["HC_ENV_FILE"] = None
        expected = expect_default
    r = box.bash('. verify/lib.sh; printf "%s" "${%s-<unset>}"' % ("%s", var), **env)
    assert r.returncode == 0, r.stderr
    assert r.stdout == expected, r.stderr
    if var == "HC_SECRETS_DIR" and source != "env-empty":
        r = box.bash('. verify/lib.sh; printf "%s" "$HC_DEMO_TOKEN_ALICE"', **env)
        assert r.stdout == {"env": "token-from-env", "file": "token-from-file", "default": "default-token"}[source]


def test_with_no_environment_at_all_the_default_target_is_unchanged(box):
    (box.root / ".env.local").write_text("HC_PROFILE=from-default-env-file\n")
    r = box.bash('. verify/lib.sh; printf "%s|%s|%s|%s" "$HC_GW" "$HC_SECRETS_DIR" "$HC_DEMO_TOKEN_ALICE" "$HC_PROFILE"')
    assert r.stdout == "http://127.0.0.1:8888|%s/secrets|default-token|from-default-env-file" % box.root
    assert target_lines(r) == ["target: gw=127.0.0.1:8888 project=repo container=- pg0=-"]
    assert box.docker_calls() == []


def test_the_default_env_file_may_be_absent_but_a_named_one_may_not(box):
    assert box.bash('. verify/lib.sh; echo survived').stdout.strip() == "survived"
    for name in ("missing.env", str(box.root / "abs-missing.env")):
        r = box.bash('. verify/lib.sh; echo survived', HC_ENV_FILE=name)
        assert r.returncode == 1 and "survived" not in r.stdout and "does not exist" in r.stderr
        assert target_lines(r) == [], "refused before the target is announced"
    assert box.bash('. verify/lib.sh; echo survived', HC_ENV_FILE="").stdout.strip() == "survived", "empty = not named"


# ---------------------------------------------------------------- target line and the two-way guard

GUARD_ROWS = [
    # id, HC_TARGET, bind, project, container column wanted (None = not needed), expected rc
    ("next-ok", "next", "127.0.0.1:18888", "hc-next", None, 0),
    ("next-prod-bind", "next", "127.0.0.1:8888", "hc-next", None, 1),
    ("next-near-bind", "next", "0.0.0.0:18888", "hc-next", None, 1),
    ("next-prod-project", "next", "127.0.0.1:18888", "hindsight-crew", None, 1),
    ("next-dirname-project", "next", "127.0.0.1:18888", "", None, 1),
    ("next-prefix-project", "next", "127.0.0.1:18888", "hc-next-x", None, 1),
    ("next-container-ok", "next", "127.0.0.1:18888", "hc-next", "hc-next-hindsight-1", 0),
    ("next-prod-container", "next", "127.0.0.1:18888", "hc-next", "hindsight-crew-hindsight-1", 1),
    ("next-container-not-found", "next", "127.0.0.1:18888", "hc-next", "", 1),
    ("prod-copy-bind", "", "127.0.0.1:18888", "", None, 1),
    ("prod-copy-project", "", "127.0.0.1:8888", "hc-next", None, 1),
    ("prod-copy-project-prefix", "", "127.0.0.1:8888", "hc-next-x", None, 1),
    ("prod-copy-container", "", "127.0.0.1:8888", "hindsight-crew", "hc-next-hindsight-1", 1),
    ("prod-ok", "", "127.0.0.1:8888", "hindsight-crew", None, 0),
    ("prod-default", "", "", "", None, 0),
    ("prod-container-ok", "", "127.0.0.1:8888", "hindsight-crew", "hindsight-crew-hindsight-1", 0),
    ("prod-container-not-found-keeps-going", "", "127.0.0.1:8888", "hindsight-crew", "", 0),
    ("other-marker-is-not-next", "yes", "127.0.0.1:18888", "hc-next", None, 1),
]


@pytest.mark.parametrize("row", GUARD_ROWS, ids=[r[0] for r in GUARD_ROWS])
def test_two_way_guard_and_one_target_line(box, row):
    _, target, bind, project, container, rc = row
    env = {"HC_TARGET": target or None, "HC_GATEWAY_BIND": bind or None, "HC_COMPOSE_PROJECT": project or None}
    if container is None:
        snippet = ". verify/lib.sh; echo survived"
    else:
        snippet = "HC_GATE_NEEDS_CONTAINER=1; . verify/lib.sh; echo survived"
        if container:
            env["STUB_PS_ID"] = container
        env["STUB_PS_RC"] = "0"
    r = box.bash(snippet, **env)
    assert r.returncode == rc, r.stderr
    assert ("survived" in r.stdout) == (rc == 0)
    assert len(target_lines(r)) == 1, r.stderr
    line = target_lines(r)[0]
    assert "gw=%s " % (bind or "127.0.0.1:8888") in line
    assert "pg0=-" in line
    if container is None:
        assert "container=-" in line


def test_the_container_column_is_the_name_not_the_id(box):
    r = box.bash("HC_GATE_NEEDS_CONTAINER=1; . verify/lib.sh; echo \"$HC_HINDSIGHT_CONTAINER\"",
                 STUB_PS_ID="0123abcd", STUB_NAME="hc-next-hindsight-1", HC_TARGET="next", HC_COMPOSE_PROJECT="hc-next",
                 HC_GATEWAY_BIND="127.0.0.1:18888")
    assert r.stdout.strip() == "hc-next-hindsight-1"
    assert target_lines(r) == ["target: gw=127.0.0.1:18888 project=hc-next container=hc-next-hindsight-1 pg0=-"]


def test_a_container_given_by_the_caller_is_not_looked_up(box):
    r = box.bash("HC_GATE_NEEDS_CONTAINER=1; . verify/lib.sh", HC_HINDSIGHT_CONTAINER="mine-1", STUB_PS_ID="ignored")
    assert "container=mine-1" in r.stderr and box.docker_calls() == []


def test_compose_shape_follows_the_contract(box):
    (box.root / ".env.local").write_text("HC_PROFILE=x\n")
    box.bash("HC_GATE_NEEDS_CONTAINER=1; . verify/lib.sh", STUB_PS_ID="cid")
    ps = box.docker_calls()[0]
    assert ps == "argv=[compose][--env-file][%s/.env.local][--project-directory][%s][-f][%s/docker-compose.yml][ps][-q][hindsight]" % (
        (box.root,) * 3)
    box.log.write_text("")
    (box.root / ".env.local").unlink()
    box.bash("HC_GATE_NEEDS_CONTAINER=1; . verify/lib.sh", STUB_PS_ID="cid", HC_COMPOSE_PROJECT="p", HC_COMPOSE_FILES="a.yml:/abs/b.yml::")
    assert box.docker_calls()[0] == "argv=[compose][--project-directory][%s][-p][p][-f][%s/a.yml][-f][/abs/b.yml][ps][-q][hindsight]" % (
        box.root, box.root), "no --env-file when the file is absent; relative files are repo-root relative"


# ---------------------------------------------------------------- gates under both targets

def test_gate1_default_target_and_the_0_9_1_command(box):
    """The 0.9.1 compatibility command of the issue: the copy's exports unset, the production files named explicitly."""
    (box.root / ".env.local").write_text("HC_GATEWAY_BIND=127.0.0.1:8888\n")
    r = box.script("verify/gate1_health.sh", HC_ENV_FILE=str(box.root / ".env.local"), HC_SECRETS_DIR=str(box.root / "secrets"))
    assert r.returncode == 0, r.stderr
    assert target_lines(r) == ["target: gw=127.0.0.1:8888 project=repo container=- pg0=-"]
    assert "http://127.0.0.1:8888/healthz" in box.curl_log.read_text()


def test_gate1_against_the_copy_and_both_negative_checks(box):
    env = box.next_env()
    r = box.script("verify/gate1_health.sh", **env)
    assert r.returncode == 0, r.stderr
    assert target_lines(r) == ["target: gw=127.0.0.1:18888 project=hc-next container=- pg0=-"]
    assert "http://127.0.0.1:18888/healthz" in box.curl_log.read_text()
    box.curl_log.write_text("")
    r = box.script("verify/gate1_health.sh", **dict(env, HC_GATEWAY_BIND="127.0.0.1:8888"))
    assert r.returncode == 1 and box.curl_log.read_text() == "", "the guard comes before the first request"
    r = box.script("verify/gate1_health.sh", **dict(env, HC_TARGET=None))
    assert r.returncode == 1 and box.curl_log.read_text() == ""


def test_an_env_file_without_hc_target_cannot_pass_the_marker_check(box):
    """The copy env file holds no HC_TARGET (it would defeat `env -u HC_TARGET`); the shape alone trips the reverse guard."""
    env = box.next_env()
    assert "HC_TARGET" not in Path(env["HC_ENV_FILE"]).read_text()
    r = box.script("verify/gate1_health.sh", **dict(env, HC_TARGET=None, HC_GATEWAY_BIND=None, HC_COMPOSE_PROJECT=None))
    assert r.returncode == 1, "gateway bind from the env file is 18888 and no marker is set"


GATE9_CHAIN = "HINDSIGHT_API_LLM_API_KEY=fake-account1\nHINDSIGHT_API_LLM_1_API_KEY=fake-account2\n"


@pytest.mark.parametrize("gate", ["gate8_rerank_provider.sh", "gate9_llm_fallback.sh"])
@pytest.mark.parametrize("target", ["production-default", "next"])
def test_container_gates_use_the_selected_compose_and_container(box, gate, target):
    chain = GATE9_CHAIN if gate == "gate9_llm_fallback.sh" else ""   # gate9 execs only when the env file is not a none profile
    if target == "next":
        env = box.next_env(STUB_PS_ID="cid-copy", STUB_NAME="hc-next-hindsight-1")
        with open(env["HC_ENV_FILE"], "a") as fh:
            fh.write(chain)
        expect_name = "hc-next-hindsight-1"
    else:
        (box.root / ".env.local").write_text("HC_PROFILE=x\n" + chain)
        env = {"STUB_PS_ID": "cid-prod", "STUB_NAME": "hindsight-crew-hindsight-1"}
        expect_name = "hindsight-crew-hindsight-1"
    r = box.script("verify/" + gate, **env)
    assert r.returncode == 0, r.stderr
    calls = box.docker_calls()
    compose = [c for c in calls if c.startswith("argv=[compose]")]
    assert len(compose) == 1
    execs = [c for c in calls if c.startswith("argv=[exec]")]
    assert execs and all(c.startswith("argv=[exec][%s]" % expect_name) for c in execs)
    assert len(target_lines(r)) == 1 and "container=%s" % expect_name in target_lines(r)[0]
    if target == "next":
        assert "[-p][hc-next]" in compose[0]
        assert "[--env-file][%s]" % env["HC_ENV_FILE"] in compose[0]
        assert "[-f][%s/docker-compose.yml][-f][%s/docker-compose.next.yml]" % (box.root, box.root) in compose[0]
        assert not any("hindsight-crew" in c for c in calls), "no production compose or container is addressed"
    else:
        assert "[-p]" not in compose[0] and "[--env-file][%s/.env.local]" % box.root in compose[0]


def test_container_gates_stop_with_a_reason_when_the_lookup_is_empty(box):
    for gate in ("gate8_rerank_provider.sh", "gate9_llm_fallback.sh"):
        r = box.script("verify/" + gate, STUB_PS_ID="")
        assert r.returncode == 1 and "hindsight container not found" in r.stderr and "HC_HINDSIGHT_CONTAINER" in r.stderr
        assert not any(c.startswith("argv=[exec]") for c in box.docker_calls())


def test_gate9_reads_the_named_env_file_for_the_llm_profile(box):
    """The profile verdict (none, or a declared chain) used to come from the production .env.local whatever the target."""
    (box.root / ".env.local").write_text(GATE9_CHAIN)
    other = box.tmp / "other.env"
    other.write_text("HINDSIGHT_API_LLM_PROVIDER=none\n")
    r = box.script("verify/gate9_llm_fallback.sh", HC_ENV_FILE=str(other), STUB_PS_ID="cid")
    assert r.returncode == 0 and "LLM none profile - consistent" in r.stderr
    assert not any(c.startswith("argv=[exec]") or c.startswith("argv=[run]") for c in box.docker_calls())


# ---------------------------------------------------------------- verify-all

GATE_NAMES = ["gate1_health", "gate2_banks", "gate3_roundtrip", "gate4_isolation", "gate5_adversarial", "gate_contract_drift",
              "gate7_mcp_enforce", "gate8_rerank_provider", "gate9_llm_fallback", "gate6_restore"]


def stub_gates(box):
    for n in GATE_NAMES:
        write_exec(box.root / "verify" / (n + ".sh"),
                   '#!/bin/bash\necho "%s ${HC_GATEWAY_BIND-unset}" >> "${STUB_GATE_LOG:?}"\n' % n)
    return box.tmp / "gates.log"


def test_verify_all_refuses_the_copy_target_before_any_gate(box):
    gates = stub_gates(box)
    r = box.script("verify/verify-all.sh", **box.next_env(), STUB_GATE_LOG=str(gates))
    assert r.returncode == 1 and "one by one" in r.stderr
    assert not gates.exists() and box.docker_calls() == []
    assert target_lines(r) == [], "refused before anything is resolved"


def test_verify_all_runs_every_gate_for_the_default_target_with_the_callers_values_first(box):
    gates = stub_gates(box)
    (box.root / ".env.local").write_text("HC_GATEWAY_BIND=127.0.0.1:9201\n")
    r = box.script("verify/verify-all.sh", STUB_GATE_LOG=str(gates), HC_GATEWAY_BIND="127.0.0.1:9202")
    assert r.returncode == 0, r.stdout + r.stderr
    assert gates.read_text().splitlines() == ["%s 127.0.0.1:9202" % n for n in GATE_NAMES]
    assert len(target_lines(r)) == 1
    gates.unlink()
    r = box.script("verify/verify-all.sh", STUB_GATE_LOG=str(gates))
    assert gates.read_text().splitlines() == ["%s 127.0.0.1:9201" % n for n in GATE_NAMES], "the file value when the caller gives none"


def test_verify_all_stops_on_a_copy_shaped_target_without_the_marker(box):
    gates = stub_gates(box)
    r = box.script("verify/verify-all.sh", STUB_GATE_LOG=str(gates), HC_GATEWAY_BIND="127.0.0.1:18888")
    assert r.returncode == 1 and not gates.exists()


def test_verify_all_source_loads_the_env_file_once():
    text = (REPO / "verify" / "verify-all.sh").read_text()
    assert ".env.local" not in "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#")), \
        "verify-all must not re-read a hardcoded .env.local behind lib.sh"


@pytest.mark.parametrize("gate", sorted(p.name for p in (REPO / "verify").glob("gate*.sh")))
def test_no_gate_hardcodes_the_env_file_or_the_compose_file(gate):
    code = "\n".join(ln for ln in (REPO / "verify" / gate).read_text().splitlines() if not ln.lstrip().startswith("#"))
    assert "--env-file" not in code or gate == "gate6_restore.sh"
    assert "-f \"$ROOT/docker-compose.yml\"" not in code or gate == "gate6_restore.sh"
    if gate != "gate6_restore.sh":
        assert "$ROOT/.env.local" not in code


@pytest.mark.parametrize("target", ["next", "production-default"])
def test_gate6_is_refused_against_the_copy_but_not_against_production(box, target):
    """gate6 runs restore-test.sh, which mounts the production hfcache read-write (and can stop production hindsight);
    running it on its own against the copy would break the copy's read-only hfcache rule."""
    probe = box.root / "gate6-body-ran"
    snippet = 'HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; . "$HERE/lib.sh"; : > "$1"'
    write_exec(box.root / "verify" / "gate6_restore.sh", "#!/bin/bash\n" + snippet + "\n")
    env = box.next_env() if target == "next" else {}
    r = subprocess.run(["/bin/bash", str(box.root / "verify" / "gate6_restore.sh"), str(probe)], cwd=str(box.root),
                       env=box.env(**env), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    if target == "next":
        assert r.returncode == 1 and "gate6" in r.stderr and not probe.exists()
    else:
        assert r.returncode == 0 and probe.exists()


# ---------------------------------------------------------------- scripts/mcp-probe.sh

def test_mcp_probe_refuses_a_named_env_file_that_does_not_exist(box):
    for name in ("missing.env", str(box.root / "abs-missing.env")):
        r = box.script("scripts/mcp-probe.sh", HC_ENV_FILE=name, HC_HINDSIGHT_API_KEY="k")
        assert r.returncode == 1 and "does not exist" in r.stderr
        assert box.docker_calls() == [] and "target:" not in r.stderr
        r = box.script("scripts/mcp-probe.sh", HC_ENV_FILE=name)
        assert r.returncode == 1


def test_mcp_probe_default_env_file_may_be_absent(box):
    r = box.bash('. scripts/mcp-probe.sh; echo survived')
    assert r.stdout.strip() == "survived" and r.stderr == ""


def test_sourcing_mcp_probe_with_a_missing_named_env_file_fails_the_source_not_the_shell(box):
    r = box.bash('. scripts/mcp-probe.sh; echo "source rc=$?"', HC_ENV_FILE="missing.env")
    assert r.returncode == 0 and r.stdout.strip() == "source rc=1" and "does not exist" in r.stderr


def test_lib_sh_default_env_file_is_not_mistaken_for_a_named_one_by_the_probe(box):
    """lib.sh fills in the default HC_ENV_FILE itself; the probe it sources must not take that for a user's choice (the
    default .env.local is absent in CI and in the stub tests, and the first mcp_call would end the shell)."""
    r = box.bash('. verify/lib.sh; _hc_load_mcp_probe; echo "loaded rc=$? env=$HC_ENV_FILE"')
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "loaded rc=0 env=%s/.env.local" % box.root
    assert "does not exist" not in r.stderr


def test_an_exported_empty_hc_env_file_does_not_become_a_named_one_for_child_tools(box):
    """`HC_ENV_FILE= verify-all.sh` exported an empty value; lib.sh's default assignment kept the export attribute, so each
    gate (a child process) saw a non-empty path, took it for a named file and refused the absent default."""
    r = box.bash('. verify/lib.sh; bash -c ". verify/lib.sh; echo child-ok" 2>&1; printenv HC_ENV_FILE || echo not-exported',
                 HC_ENV_FILE="")
    assert "child-ok" in r.stdout and "does not exist" not in r.stdout + r.stderr
    assert r.stdout.strip().endswith("not-exported")


def test_lib_sh_passes_a_named_env_file_through_to_the_probe(box):
    named = box.tmp / "named.env"
    named.write_text("HC_PROBE_SEES=named\n")
    r = box.bash('. verify/lib.sh; _hc_load_mcp_probe; echo "$HC_PROBE_SEES|$HC_ENV_FILE"', HC_ENV_FILE=str(named))
    assert r.stdout.strip() == "named|%s" % named, r.stderr


@pytest.mark.parametrize("via", ["probe-alone", "lib-then-probe"])
def test_the_probe_keeps_the_callers_secrets_dir_like_the_other_five(box, via):
    (box.root / "probe.env").write_text("HC_SECRETS_DIR=/from/the/env/file\nHC_GATEWAY_BIND=127.0.0.1:9301\n")
    snippet = {"probe-alone": '. scripts/mcp-probe.sh; echo "$HC_SECRETS_DIR|$HC_GATEWAY_BIND"',
               "lib-then-probe": '. verify/lib.sh; _hc_load_mcp_probe; echo "$HC_SECRETS_DIR|$HC_GATEWAY_BIND"'}[via]
    r = box.bash(snippet, HC_ENV_FILE="probe.env", HC_SECRETS_DIR=str(box.root / "secrets"))
    assert r.stdout.strip() == "%s|127.0.0.1:9301" % (box.root / "secrets"), r.stderr
