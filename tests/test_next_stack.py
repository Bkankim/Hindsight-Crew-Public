"""scripts/next-stack.sh and docker-compose.next.yml (issue #246).

Hermetic: a stub `docker` (a python script driven by a JSON config, logging every call), a stub `curl` and a stub
`uv` (runs the test interpreter, which has httpx) sit first on PATH. No real docker, no network, nothing outside tmp
is read or written, and every test that could touch the production checkout fixture proves it did not.

The stub's `compose ... up` exits 17, which stops `up` right after the render check and the restore - the
assertions look at what was written (copy env file, policy copy) and which docker calls were made before that.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
IMAGE = "ghcr.io/vectorize-io/hindsight@sha256:50311d02b4d5d2b7c5242bd336edf8735df8374eaad496a92f49c83b37d2aa06"   # v1.1.0 (#285)
V1_0_IMAGE = "ghcr.io/vectorize-io/hindsight@sha256:a88678c038b0e4340822b02a5efc566c56d8176b5bffa731bc433fd37f978e4c"
BANK = "bank-x"
KEEP = "keep-arch-v2-20261027-043001"          # a backup directory outside the daily YYYYMMDD-HHMMSS rule
KEEP_BYTES = b"z" * 10
BUNDLED_DIR = "/app/api/.venv/lib/python3.11/site-packages/claude_agent_sdk/_bundled"
BUNDLED = BUNDLED_DIR + "/claude"
SECRET_VALUES = {
    "HC_HINDSIGHT_API_KEY": "FAKE-GATEWAY-KEY-aaaa1111",
    "HINDSIGHT_API_LLM_API_KEY": "FAKE-GROK-PRIMARY-bbbb2222",
    "HINDSIGHT_API_LLM_1_API_KEY": "FAKE-ACCOUNT1-TOKEN-cccc3333",
    "HINDSIGHT_API_LLM_2_API_KEY": "FAKE-ACCOUNT2-TOKEN-dddd4444",
    "XAI_API_KEY": "FAKE-XAI-eeee5555",
    "HC_NOTIFY_DISCORD_WEBHOOK": "FAKE-DISCORD-ffff6666",
}

PROD_ENV = """# production .env.local (fake)
HC_PROFILE=ko-full
HC_HINDSIGHT_API_KEY={HC_HINDSIGHT_API_KEY}
HC_GATEWAY_BIND=127.0.0.1:8888
HC_UI_BIND=127.0.0.1:9999
HC_PG0_VOLUME=hindsight-crew_pg0
HC_HINDSIGHT_IMAGE=ghcr.io/vectorize-io/hindsight:0.9.1
COMPOSE_PROJECT_NAME=hindsight-crew
HC_BANK_POLICIES=/prod/policies/bank-policies.json
HC_CONTEXT_TRIGGERS=/prod/policies/context-triggers.json
HC_BANK_POLICIES_LOCAL=/prod/policies/local/bank-policies.json
HC_CONTEXT_TRIGGERS_LOCAL=/prod/policies/local/context-triggers.json
HC_ACL_FILE=/prod/secrets/acl.json
HC_AUDIT_LOG=/prod/audit/gateway.log
HC_TARGET=next
HINDSIGHT_API_RERANKER_PROVIDER=tei
HINDSIGHT_API_RERANKER_TEI_URL=http://host.docker.internal:8787
HINDSIGHT_API_EMBEDDINGS_PROVIDER=local
HINDSIGHT_API_LLM_PROVIDER=xai-oauth
HINDSIGHT_API_LLM_API_KEY={HINDSIGHT_API_LLM_API_KEY}
HINDSIGHT_API_RETAIN_LLM_PROVIDER=xai-oauth
HINDSIGHT_API_LLM_STRATEGY={{"mode":"failover"}}
HINDSIGHT_API_LLM_1_PROVIDER=claude-code
HINDSIGHT_API_LLM_1_MODEL=claude-sonnet-5-5
HINDSIGHT_API_LLM_1_API_KEY={HINDSIGHT_API_LLM_1_API_KEY}
HINDSIGHT_API_LLM_2_PROVIDER=claude-code
HINDSIGHT_API_LLM_2_API_KEY={HINDSIGHT_API_LLM_2_API_KEY}
HINDSIGHT_API_REFLECT_LLM_2_API_KEY={HINDSIGHT_API_LLM_2_API_KEY}
XAI_API_KEY={XAI_API_KEY}
hsauth_xai_cache=1
HC_NOTIFY_DISCORD_WEBHOOK={HC_NOTIFY_DISCORD_WEBHOOK}
HC_NEXT_IMAGE_NOTE=drop-me
""".format(**SECRET_VALUES)

STUB_DOCKER = r'''#!__PY__
import json, os, sys
cfg = json.load(open(os.environ["STUB_CFG"]))
argv = sys.argv[1:]
watch = ["HC_HINDSIGHT_IMAGE", "COMPOSE_PROJECT_NAME", "HC_GATEWAY_BIND", "HC_NEXT_COMMIT", "HC_ENV_FILE", "HC_PROD_ROOT",
         "HC_SECRETS_DIR", "HC_NEXT_STATE_DIR", "HC_COMPOSE_PROJECT", "HC_COMPOSE_FILES"]
with open(os.environ["STUB_LOG"], "a") as fh:
    fh.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "env": {k: os.environ.get(k) for k in watch}}) + "\n")
cmd = argv[0] if argv else ""
if " ".join(argv[:2]) in cfg.get("fail", []) or cmd in cfg.get("fail", []):
    sys.stderr.write("stub docker: induced failure\n")
    sys.exit(1)
def out(s):
    if s:
        print(s)
if cmd == "info":
    pass
elif cmd == "ps":
    out(cfg.get("ps_a", ""))
elif cmd == "volume" and argv[1] == "ls":
    out(cfg.get("volumes", ""))
elif cmd == "network" and argv[1] == "ls":
    out(cfg.get("networks", ""))
elif cmd == "images":
    if "--format" in argv:
        out(cfg.get("images_fmt", ""))
    else:
        out(cfg.get("images_q", {}).get(argv[argv.index("--filter") + 1], ""))
elif cmd == "image" and argv[1] == "inspect":
    out(cfg.get("image_tags", {}).get(argv[-1], ""))
elif cmd == "run":
    if "/proc/meminfo" in argv:
        out(cfg.get("mem", "9999"))
    elif "--name" in argv and argv[argv.index("--name") + 1] in cfg.get("fail_run_names", []):
        print("pg_verifybackup: error: bad")
        sys.exit(1)
elif cmd == "inspect":
    names = [a for a in argv if a.startswith("hc-next-")]
    st = cfg.get("containers", {})
    if any("State.Running" in a for a in argv):
        rows = [str(bool(st.get(n, {}).get("running"))).lower() for n in names]
        if not all(n in st for n in names):
            sys.exit(1)
        out("\n".join(rows))
    else:
        n = names[0]
        if n not in st:
            sys.exit(1)
        if any("started=" in a for a in argv):
            out(st[n].get("line", "running"))
        else:
            out(st[n].get("wait", "running healthy"))
elif cmd == "compose":
    if "config" in argv:
        out(cfg["render_raw"] if "--no-interpolate" in argv else cfg["render"])
    elif "up" in argv:
        sys.exit(cfg.get("up_rc", 17))
sys.exit(0)
'''.replace("__PY__", sys.executable)

STUB_UV = r'''#!/bin/sh
# uv run --python 3.12 --with-requirements F python ARGS...  ->  the test interpreter (httpx is installed)
echo "uv $*" >> "${STUB_UV_LOG:?}"
while [ $# -gt 0 ] && [ "$1" != python ]; do shift; done
shift
exec __PY__ "$@"
'''.replace("__PY__", sys.executable)

STUB_CURL = r'''#!/bin/sh
body="$(cat "${STUB_HEALTHZ:?}" 2>/dev/null)"
[ -n "$body" ] || exit 22
printf '%s' "$body"
'''


def write_exec(path, text):
    path.write_text(text)
    path.chmod(0o755)


def tree_state(root):
    return sorted((str(p.relative_to(root)), p.stat().st_size, p.stat().st_mode, p.stat().st_mtime_ns)
                  for p in [root, *root.rglob("*")])


def good_render(prod, state, secrets=None, image=IMAGE):
    secrets = secrets or "%s/secrets" % prod
    env = {"HC_NEXT_ENV": "1", "HC_HINDSIGHT_API_KEY": "x", "HINDSIGHT_API_LLM_PROVIDER": "none"}
    return {
        "name": "hc-next",
        "services": {
            "hindsight": {
                "image": image, "environment": dict(env, HF_HUB_OFFLINE="1"),
                "volumes": [
                    {"type": "volume", "source": "pg0", "target": "/home/hindsight/.pg0", "volume": {}},
                    {"type": "volume", "source": "hfcache", "target": "/home/hindsight/.cache/huggingface", "read_only": True},
                    {"type": "bind", "source": "%s/vendor/claude-cli/claude" % prod, "target": BUNDLED, "read_only": True},
                ]},
            "gateway": {
                "environment": dict(env),
                "volumes": [
                    {"type": "bind", "source": secrets, "target": "/secrets", "read_only": True},
                    {"type": "bind", "source": "%s/policies" % state, "target": "/app/policies", "read_only": True},
                    {"type": "bind", "source": "%s/audit" % state, "target": "/audit"},
                ]},
            "ui-tunnel": {"image": "alpine/socat:latest"},
        },
        "volumes": {"pg0": {"name": "hc-next_pg0"}, "hfcache": {"name": "hindsight-crew_hfcache", "external": True}},
    }


class Box:
    def __init__(self, tmp_path, template):
        self.tmp = tmp_path.resolve()
        shutil.copytree(template, self.tmp / "t", symlinks=True)
        for name in ("wt", "prod", "bin", "home"):
            (self.tmp / "t" / name).rename(self.tmp / name)
        self.root = self.tmp / "wt"
        self.prod = self.tmp / "prod"
        self.state = self.tmp / "state" / "hc-next"
        self.bin = self.tmp / "bin"
        self.home = self.tmp / "home"
        self.log = self.tmp / "docker.jsonl"
        self.cfg_path = self.tmp / "cfg.json"
        write_exec(self.bin / "docker", STUB_DOCKER)
        write_exec(self.bin / "uv", STUB_UV)
        write_exec(self.bin / "curl", STUB_CURL)
        self.healthz = self.tmp / "healthz"
        self.cfg = {"render": json.dumps(good_render(self.prod, self.state)), "render_raw": json.dumps(
            {"services": {s: {"env_file": ["${HC_ENV_FILE:?}"]} for s in ("hindsight", "gateway")}})}
        self.save()

    def save(self):
        self.cfg_path.write_text(json.dumps(self.cfg))

    def set_render(self, doc):
        self.cfg["render"] = json.dumps(doc)
        self.save()

    def env(self, **extra):
        env = {
            "PATH": "%s:/usr/bin:/bin" % self.bin, "HOME": str(self.home),
            "STUB_CFG": str(self.cfg_path), "STUB_LOG": str(self.log), "STUB_UV_LOG": str(self.tmp / "uv.log"),
            "STUB_HEALTHZ": str(self.healthz), "HC_PYTHON": sys.executable, "GIT_CONFIG_GLOBAL": "/dev/null",
            "HC_PROD_ROOT": str(self.prod), "HC_NEXT_STATE_DIR": str(self.state), "HC_NEXT_REQUIRED_BANK": BANK,
        }
        env.update(extra)
        return {k: v for k, v in env.items() if v is not None}

    def run(self, *args, **env):
        return subprocess.run(["/bin/bash", str(self.root / "scripts" / "next-stack.sh"), *args], cwd=str(self.root),
                              env=self.env(**env), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True, timeout=120)

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line]

    def argvs(self):
        return [c["argv"] for c in self.calls()]

    def mutating(self):
        """docker calls that create or delete something (everything but listing, info, inspect, config and the
        --rm precheck containers)."""
        rows = []
        for a in self.argvs():
            if a[:2] in (["volume", "create"], ["volume", "rm"], ["network", "rm"]) or a[0] in ("rm", "rmi") \
                    or (a[0] == "compose" and "up" in a):
                rows.append(a)
            elif a[0] == "run" and "--name" in a and not a[a.index("--name") + 1].startswith("hc-next-precheck"):
                rows.append(a)
        return rows

    def env_text(self):
        return (self.state / "env").read_text()


def build_template(t):
    """The worktree (a real git repo), the production checkout fixture and the stub bin dir, built once per session."""
    root, prod = t / "wt", t / "prod"
    for d in (root / "scripts", t / "bin", t / "home", prod / "secrets", prod / "vendor" / "claude-cli", prod / "policies",
              prod / "backups" / "20261001-010101", prod / "backups" / "20261003-020202", prod / "backups" / KEEP,
              prod / "backups" / "20261002-000000-empty"):
        d.mkdir(parents=True)
    for name in ("next-stack.sh", "hc-python.sh", "policy-split.py", "llm-migrate-opus.py"):
        shutil.copy2(REPO / "scripts" / name, root / "scripts" / name)
    for name in ("docker-compose.yml", "docker-compose.next.yml"):
        shutil.copy2(REPO / name, root / name)
    (root / "gateway").symlink_to(REPO / "gateway")
    subprocess.run("git init -q && git add scripts docker-compose.yml docker-compose.next.yml && "
                   "git -c user.name=t -c user.email=t@example.invalid commit -qm one", shell=True, cwd=root,
                   check=True, env=dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null"))
    (prod / ".env.local").write_text(PROD_ENV)
    (prod / "secrets" / "acl.json").write_text('{"tokens": {}}')
    (prod / "secrets" / "demo-tokens.env").write_text("HC_DEMO_TOKEN_ALICE=FAKE\n")
    write_exec(prod / "vendor" / "claude-cli" / "claude", "#!/bin/sh\n")
    pol = json.loads((REPO / "policies" / "bank-policies.json").read_text())
    pol["banks"][BANK] = pol["banks"]["personal-alice"]
    (prod / "policies" / "bank-policies.json").write_text(json.dumps(pol))
    shutil.copy2(REPO / "policies" / "context-triggers.json", prod / "policies" / "context-triggers.json")
    (prod / "backups" / "20261003-020202" / "pg0.tar.gz").write_bytes(b"x" * 10)
    (prod / "backups" / "20261001-010101" / "pg0.tar.gz").write_bytes(b"y" * 10)
    (prod / "backups" / KEEP / "pg0.tar.gz").write_bytes(KEEP_BYTES)


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    t = tmp_path_factory.mktemp("next-template").resolve()
    build_template(t)
    return t


@pytest.fixture
def box(tmp_path, template):
    return Box(tmp_path, template)


def keys_of(text):
    return {line.split("=", 1)[0] for line in text.splitlines() if "=" in line and not line.startswith("#")}


def assert_no_secrets(r):
    for v in SECRET_VALUES.values():
        assert v not in r.stdout + r.stderr, "a credential value reached the output"


# ---------------------------------------------------------------- state path validation

STATE_CASES = {
    "inside-prod": lambda b: b.prod / "sub" / "hc-next",
    "inside-worktree": lambda b: b.root / "x" / "hc-next",
    "worktree-itself": lambda b: b.root,
    "prod-itself": lambda b: b.prod,
    "contains-prod": lambda b: b.tmp,           # basename is not hc-next either
    "wrong-basename": lambda b: b.tmp / "state" / "other",
    "contains-spaces": lambda b: b.tmp / "a b" / "hc-next",
}


@pytest.mark.parametrize("cmd", [["up"], ["down"]])
@pytest.mark.parametrize("case", list(STATE_CASES))
def test_state_dir_outside_the_allowed_shape_is_refused_and_nothing_is_touched(box, case, cmd):
    state = STATE_CASES[case](box)
    state.mkdir(parents=True, exist_ok=True)
    (state / "sentinel").write_text("keep")
    before = tree_state(box.prod)
    r = box.run(*cmd, HC_NEXT_STATE_DIR=str(state))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "HC_NEXT_STATE_DIR" in r.stderr
    assert (state / "sentinel").read_text() == "keep"
    assert tree_state(box.prod) == before, "the production checkout must not be written"
    assert box.mutating() == []


def test_a_symlinked_state_dir_is_refused(box):
    real = box.tmp / "elsewhere" / "hc-next"
    real.mkdir(parents=True)
    box.state.parent.mkdir(parents=True)
    box.state.symlink_to(real)
    r = box.run("down")
    assert r.returncode == 1 and "symlink" in r.stderr
    assert real.exists()


# ---------------------------------------------------------------- up: pre-checks leave nothing behind

def test_up_refuses_without_a_required_bank(box):
    r = box.run("up", HC_NEXT_REQUIRED_BANK="")
    assert r.returncode == 1 and "required bank" in r.stderr
    assert not box.state.exists() and box.mutating() == []


def test_required_bank_falls_back_to_the_host_env_file_setting(box):
    (box.prod / ".env.host.local").write_text("HC_PERSONAL_BANK='%s'\n" % BANK)
    r = box.run("up", HC_NEXT_REQUIRED_BANK="")
    assert "required bank" not in r.stderr
    assert (box.state / "env").exists()      # got past the pre-checks and into the env assembly


def test_required_bank_must_exist_in_the_merged_policies(box):
    r = box.run("up", HC_NEXT_REQUIRED_BANK="no-such-bank")
    assert r.returncode == 1 and "required bank" in r.stderr
    assert box.mutating() == []


@pytest.mark.parametrize("missing", [".env.local", "secrets/acl.json", "secrets/demo-tokens.env", "vendor/claude-cli/claude"])
def test_up_refuses_when_a_production_file_is_missing(box, missing):
    (box.prod / missing).unlink()
    r = box.run("up")
    assert r.returncode == 1 and missing in r.stderr
    assert not box.state.exists() and box.mutating() == []


@pytest.mark.parametrize("mem, ok", [("9999", True), ("3500", True), ("3499", False), ("", False), ("n/a", False)])
def test_up_checks_the_vm_memory(box, mem, ok):
    box.cfg["mem"] = mem
    box.save()
    r = box.run("up")
    if ok:
        assert "MemAvailable" not in r.stderr
    else:
        assert r.returncode == 1 and "MemAvailable" in r.stderr
        assert not box.state.exists() and box.mutating() == []


def test_the_memory_floor_is_configurable(box):
    box.cfg["mem"] = "2000"
    box.save()
    assert "MemAvailable" in box.run("up").stderr
    assert "MemAvailable" not in box.run("up", HC_NEXT_MIN_AVAIL_MB="1000").stderr


EXISTING = {
    "container": {"ps_a": "hc-next-hindsight-1||"},
    "restore-helper": {"ps_a": "hc-next-restore-extract||1"},
    "volume": {"volumes": "hc-next_pg0||"},
    "network": {"networks": "hc-next_internal||"},
}


@pytest.mark.parametrize("what", list(EXISTING) + ["state-dir"])
def test_up_refuses_when_hc_next_resources_exist(box, what):
    if what == "state-dir":
        box.state.mkdir(parents=True)
    else:
        box.cfg.update(EXISTING[what])
        box.save()
    r = box.run("up")
    assert r.returncode == 1 and "down" in r.stderr
    assert box.mutating() == []


def test_the_precheck_container_is_named_labelled_and_removed_on_exit(box):
    box.run("up")
    run = next(a for a in box.argvs() if a[0] == "run" and "/proc/meminfo" in a)
    assert "--rm" in run and "--network" in run and run[run.index("--network") + 1] == "none"
    assert run[run.index("--name") + 1].startswith("hc-next-precheck-")
    assert run[run.index("--label") + 1] == "hc-next=1"


# ---------------------------------------------------------------- up: the copy env file

EXCLUDED = ["HC_GATEWAY_BIND_PROD", "HC_UI_BIND_PROD", "HC_PG0_VOLUME_PROD", "HC_HINDSIGHT_IMAGE_PROD",
            "COMPOSE_PROJECT_NAME", "HC_BANK_POLICIES", "HC_CONTEXT_TRIGGERS", "HC_BANK_POLICIES_LOCAL",
            "HC_CONTEXT_TRIGGERS_LOCAL", "HC_ACL_FILE", "HC_AUDIT_LOG", "HC_TARGET", "HC_NOTIFY_DISCORD_WEBHOOK",
            "XAI_API_KEY", "HINDSIGHT_API_LLM_API_KEY", "HINDSIGHT_API_RETAIN_LLM_PROVIDER", "HINDSIGHT_API_LLM_2_API_KEY",
            "HINDSIGHT_API_REFLECT_LLM_2_API_KEY", "HC_NEXT_IMAGE_NOTE"]


def test_env_assembly_drops_llm_xai_discord_bind_and_policy_keys_and_adds_the_copy_values(box):
    r = box.run("up")
    assert_no_secrets(r)
    text = box.env_text()
    keys = keys_of(text)
    for k in EXCLUDED:
        if k.endswith("_PROD"):
            continue
        assert k not in keys, k
    assert not any(k.startswith("HINDSIGHT_API_LLM_") and k != "HINDSIGHT_API_LLM_PROVIDER" for k in keys)
    assert {"HC_HINDSIGHT_API_KEY", "HC_PROFILE", "HINDSIGHT_API_RERANKER_PROVIDER",
            "HINDSIGHT_API_RERANKER_TEI_URL", "HINDSIGHT_API_EMBEDDINGS_PROVIDER"} <= keys
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=box.root, capture_output=True, text=True).stdout.strip()
    got = dict(line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith("#"))
    assert got["HC_NEXT_ENV"] == "1"
    assert got["HC_HINDSIGHT_IMAGE"] == "'%s'" % IMAGE
    assert got["HC_PG0_VOLUME"] == "hc-next_pg0"
    assert got["HC_GATEWAY_BIND"] == "127.0.0.1:18888" and got["HC_UI_BIND"] == "127.0.0.1:9998"
    assert got["HC_NEXT_COMMIT"] == commit
    assert got["HC_PROD_ROOT"] == "'%s'" % box.prod and got["HC_SECRETS_DIR"] == "'%s/secrets'" % box.prod
    assert got["HC_NEXT_STATE_DIR"] == "'%s'" % box.state
    assert got["HINDSIGHT_API_LLM_PROVIDER"] == "none"
    assert oct((box.state / "env").stat().st_mode & 0o777) == "0o600" and oct(box.state.stat().st_mode & 0o777) == "0o700"


def test_hc_target_is_never_written_to_a_file(box):
    """lib.sh and mcp-probe source the env file: HC_TARGET in it would let `env -u HC_TARGET` pass the guard."""
    box.run("up")
    assert "HC_TARGET" not in keys_of(box.env_text())


def test_values_survive_both_the_shell_and_compose_readers(box):
    box.run("up", "--llm", "opus")
    r = subprocess.run(["bash", "-c", 'set -a; . "$1"; set +a; printf "%s|%s|%s" "$HINDSIGHT_API_LLM_STRATEGY" '
                       '"$HC_PROD_ROOT" "$HC_HINDSIGHT_IMAGE"', "_", str(box.state / "env")],
                       capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    assert r.stdout == '{"mode":"failover"}|%s|%s' % (box.prod, IMAGE), r.stderr
    assert "HINDSIGHT_API_LLM_STRATEGY='{\"mode\":\"failover\"}'" in box.env_text().splitlines()


def test_llm_opus_maps_the_production_chain_and_prints_no_values(box):
    r = box.run("up", "--llm", "opus")
    assert_no_secrets(r)
    got = dict(line.split("=", 1) for line in box.env_text().splitlines() if "=" in line and not line.startswith("#"))
    assert got["HINDSIGHT_API_LLM_PROVIDER"] == "claude-code"
    assert got["HINDSIGHT_API_LLM_MODEL"] == "claude-sonnet-5-5"
    assert got["CLAUDE_CODE_EFFORT_LEVEL"] == "medium"
    assert got["HINDSIGHT_API_LLM_API_KEY"] == SECRET_VALUES["HINDSIGHT_API_LLM_1_API_KEY"]
    assert got["HINDSIGHT_API_LLM_1_PROVIDER"] == "claude-code" and got["HINDSIGHT_API_LLM_1_MODEL"] == "claude-sonnet-5-5"
    assert got["HINDSIGHT_API_LLM_1_API_KEY"] == SECRET_VALUES["HINDSIGHT_API_LLM_2_API_KEY"]
    assert not any(k.startswith("HINDSIGHT_API_LLM_2_") for k in got)
    assert SECRET_VALUES["HINDSIGHT_API_LLM_API_KEY"] not in box.env_text(), "the production grok key stays out"


def prod_env_target_layout():
    """The production file after scripts/llm-migrate-opus.py: the non-LLM lines of PROD_ENV plus the target block
    (global key = account1, LLM_1 = account2, the mental-model-refresh chain), no xai, no slot keys."""
    keep = [ln for ln in PROD_ENV.splitlines()
            if not (ln.startswith("HINDSIGHT_API_") and "LLM_" in ln.split("=")[0]) and "xai" not in ln.lower()]
    g, p = SECRET_VALUES["HINDSIGHT_API_LLM_1_API_KEY"], SECRET_VALUES["HINDSIGHT_API_LLM_2_API_KEY"]
    return "\n".join(keep + [
        "HINDSIGHT_API_LLM_PROVIDER=claude-code", "HINDSIGHT_API_LLM_MODEL=claude-sonnet-5-5", "HINDSIGHT_API_LLM_API_KEY=" + g,
        "HINDSIGHT_API_LLM_STRATEGY='{\"mode\":\"failover\"}'", "HINDSIGHT_API_LLM_1_PROVIDER=claude-code",
        "HINDSIGHT_API_LLM_1_MODEL=claude-sonnet-5-5", "HINDSIGHT_API_LLM_1_API_KEY=" + p,
        "HINDSIGHT_API_MENTAL_MODEL_REFRESH_LLM_STRATEGY='{\"mode\":\"failover\"}'",
        "HINDSIGHT_API_MENTAL_MODEL_REFRESH_LLM_1_PROVIDER=claude-code",
        "HINDSIGHT_API_MENTAL_MODEL_REFRESH_LLM_1_MODEL=claude-sonnet-5-5", "HINDSIGHT_API_MENTAL_MODEL_REFRESH_LLM_1_API_KEY=" + p,
    ]) + "\n"


@pytest.fixture(params=["pre-migration", "target"])
def layout_box(request, box):
    """box whose production .env.local is in the pre-migration shape or in the shape llm-migrate-opus.py writes."""
    if request.param == "target":
        (box.prod / ".env.local").write_text(prod_env_target_layout())
    box.layout = request.param
    return box


MM = "HINDSIGHT_API_MENTAL_MODEL_REFRESH_LLM_"


def _llm_values(text):
    """name -> unquoted value of every LLM key of an env file text (comments and other keys skipped)."""
    out = {}
    for line in text.splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        k, v = line.split("=", 1)
        if k.startswith("HINDSIGHT_API_") and "LLM_" in k:
            out[k] = v.strip("'\"")
    return out


def test_llm_opus_mental_model_refresh_chain_is_account2(box):
    """v1.0.0 builds a refresh-only LLM that inherits members from the REFLECT_ slot only, so the copy declares the
    refresh chain itself (account1 primary from the global key, account2 as the member)."""
    r = box.run("up", "--llm", "opus")
    assert_no_secrets(r)
    got = _llm_values(box.env_text())
    assert got[MM + "STRATEGY"] == '{"mode":"failover"}'
    assert got[MM + "1_PROVIDER"] == "claude-code" and got[MM + "1_MODEL"] == "claude-sonnet-5-5"
    assert got[MM + "1_API_KEY"] == SECRET_VALUES["HINDSIGHT_API_LLM_2_API_KEY"]
    assert MM + "API_KEY" not in got and MM + "PROVIDER" not in got, "the refresh primary stays the global one"
    assert "HINDSIGHT_API_MENTAL_MODEL_REFRESH_LLM_STRATEGY='{\"mode\":\"failover\"}'" in box.env_text().splitlines()


def test_llm_opus_env_is_what_llm_migrate_opus_considers_the_target(box, tmp_path):
    """One list of target keys, two writers: next-stack.sh assembles the copy env, scripts/llm-migrate-opus.py migrates
    a production-shaped file. The copy env must be 'already migrated' for the script, and migrating the fake production
    env must give the copy env's LLM keys and values."""
    box.run("up", "--llm", "opus")
    script = str(REPO / "scripts" / "llm-migrate-opus.py")
    r = subprocess.run([sys.executable, script, "--env-file", str(box.state / "env")], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.startswith("already migrated"), r.stdout + r.stderr
    prod_like = tmp_path / "prod-like.env"
    prod_like.write_text(PROD_ENV)
    r = subprocess.run([sys.executable, script, "--env-file", str(prod_like), "--apply"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _llm_values(prod_like.read_text()) == _llm_values(box.env_text())


def test_break_primary_replaces_only_the_primary_key(box):
    r = box.run("up", "--llm", "opus", "--break-primary")
    assert_no_secrets(r)
    got = dict(line.split("=", 1) for line in box.env_text().splitlines() if "=" in line and not line.startswith("#"))
    assert got["HINDSIGHT_API_LLM_API_KEY"] == "'hc-next-invalid-primary'"
    assert got["HINDSIGHT_API_LLM_1_API_KEY"] == SECRET_VALUES["HINDSIGHT_API_LLM_2_API_KEY"]


@pytest.mark.parametrize("args", [["--break-primary"], ["--llm", "none", "--break-primary"], ["--llm", "gpt"], ["--llm"], ["--nope"],
                                  ["--backup"]])
def test_bad_up_arguments_are_refused_before_anything_happens(box, args):
    r = box.run("up", *args)
    assert r.returncode == 1 and box.argvs() == [] and not box.state.exists()


@pytest.mark.parametrize("slot", ["1", "2"])
@pytest.mark.parametrize("provider", ["xai-oauth", "none", ""])
def test_llm_opus_is_refused_unless_both_production_members_are_claude_code(box, slot, provider):
    text = PROD_ENV.replace("HINDSIGHT_API_LLM_%s_PROVIDER=claude-code" % slot, "HINDSIGHT_API_LLM_%s_PROVIDER=%s" % (slot, provider))
    (box.prod / ".env.local").write_text(text)
    r = box.run("up", "--llm", "opus")
    assert r.returncode == 1 and "claude-code" in r.stderr
    assert not box.state.exists() and box.mutating() == []
    assert_no_secrets(r)


def test_tei_url_override_replaces_the_copied_value(box):
    box.run("up", HC_NEXT_TEI_URL="http://delay-proxy:9")
    lines = [ln for ln in box.env_text().splitlines() if ln.startswith("HINDSIGHT_API_RERANKER_TEI_URL=")]
    assert lines == ["HINDSIGHT_API_RERANKER_TEI_URL='http://delay-proxy:9'"]


def test_a_single_quote_in_a_written_value_is_refused_without_echoing_it(box):
    r = box.run("up", HC_NEXT_TEI_URL="http://x/'y")
    assert r.returncode == 1 and "single quote" in r.stderr and "http://x/" not in r.stderr
    # the refusal itself must end the run: not a later stub compose failure after an env file with an empty value
    assert "docker compose up failed" not in r.stderr and not (box.state / "env").exists()
    assert box.mutating() == []


# ---------------------------------------------------------------- up: policies

def test_unsplit_policies_are_split_into_the_state_dir_and_checked(box):
    box.run("up")
    tracked = json.loads((box.state / "policies" / "bank-policies.json").read_text())
    local = json.loads((box.state / "policies" / "local" / "bank-policies.json").read_text())
    assert BANK not in tracked["banks"] and BANK in local["banks"]
    assert (box.state / "policies" / "context-triggers.json").exists()
    uv = (box.tmp / "uv.log").read_text()
    assert "policy-split.py check" in uv and "--with-requirements" in uv
    assert not (box.prod / "policies" / "local").exists(), "nothing is written to the production checkout"


def test_existing_local_policies_are_copied_with_the_tracked_pair(box):
    (box.prod / "policies" / "local").mkdir()
    pol = json.loads((box.prod / "policies" / "bank-policies.json").read_text())
    shutil.copy2(box.prod / "policies" / "bank-policies.json", box.tmp / "full.json")
    tracked = dict(pol, banks={k: v for k, v in pol["banks"].items() if k != BANK})
    (box.prod / "policies" / "bank-policies.json").write_text(json.dumps(tracked))
    (box.prod / "policies" / "local" / "bank-policies.json").write_text(json.dumps({"banks": {BANK: pol["banks"][BANK]}}))
    r = box.run("up")
    assert "policy" not in r.stderr.replace("policy copy", ""), r.stderr
    for rel in ("bank-policies.json", "context-triggers.json", "local/bank-policies.json"):
        assert (box.state / "policies" / rel).exists(), rel
    assert not (box.state / "policies" / "local" / "context-triggers.json").exists(), "only local files that exist are copied"
    assert (box.state / "policies" / "bank-policies.json").read_text() == (box.prod / "policies" / "bank-policies.json").read_text()


# ---------------------------------------------------------------- up: render check

def mutate(fn):
    def apply(box):
        doc = good_render(box.prod, box.state)
        fn(doc, box)
        return doc
    return apply


def _add_mount(svc, m):
    def f(doc, box):
        doc["services"][svc]["volumes"].append(m)
    return f


def _env_key(svc, key):
    def f(doc, box):
        doc["services"][svc]["environment"][key] = "VALUE-NOT-TO-BE-PRINTED"
    return f


RENDER_CASES = {
    "pg0-production-name": mutate(lambda d, b: d["volumes"]["pg0"].update(name="hindsight-crew_pg0")),
    "hsauth-mount": mutate(_add_mount("hindsight", {"type": "volume", "source": "hsauth", "target": "/home/hindsight/.hindsight"})),
    "hsauth-production-name": mutate(lambda d, b: (d["volumes"].update(hsauth={"name": "hindsight-crew_hsauth"}),
                                                  d["services"]["hindsight"]["volumes"].append(
                                                      {"type": "volume", "source": "hsauth", "target": "/x"}))),
    "overlay-mount": mutate(_add_mount("hindsight", {"type": "bind", "target": "/x",
                                                     "source": "REPO/overlays/hindsight/placeholder.py"})),
    "no-copy-marker": mutate(lambda d, b: d["services"]["gateway"]["environment"].pop("HC_NEXT_ENV")),
    "hindsight-no-marker": mutate(lambda d, b: d["services"]["hindsight"]["environment"].pop("HC_NEXT_ENV")),
    "discord-key": mutate(_env_key("gateway", "HC_NOTIFY_DISCORD_WEBHOOK")),
    "llm2-key": mutate(_env_key("hindsight", "HINDSIGHT_API_LLM_2_API_KEY")),
    "xai-key": mutate(_env_key("hindsight", "HINDSIGHT_API_XAI_CREDENTIALS")),
    "bank-policies-local-key": mutate(_env_key("gateway", "HC_BANK_POLICIES_LOCAL")),
    "triggers-local-key": mutate(_env_key("gateway", "HC_CONTEXT_TRIGGERS_LOCAL")),
    "hfcache-writable": mutate(lambda d, b: d["services"]["hindsight"]["volumes"][1].pop("read_only")),
    "repo-audit-mount": mutate(_add_mount("gateway", {"type": "bind", "target": "/audit", "source": "REPO/audit"})),
    "prod-audit-mount": mutate(_add_mount("gateway", {"type": "bind", "target": "/audit", "source": "PROD/audit"})),
    "wrong-image": mutate(lambda d, b: d["services"]["hindsight"].update(image="ghcr.io/vectorize-io/hindsight:0.9.1")),
    "unexpected-bind": mutate(_add_mount("gateway", {"type": "bind", "target": "/x", "source": "/etc"})),
}


@pytest.mark.parametrize("case", list(RENDER_CASES))
def test_render_check_rejects_and_starts_nothing(box, case):
    doc = RENDER_CASES[case](box)
    text = json.dumps(doc).replace("REPO", str(box.root)).replace("PROD", str(box.prod))
    box.cfg["render"] = text
    box.save()
    r = box.run("up")
    assert r.returncode == 1 and "render check" in r.stderr, r.stderr
    assert "VALUE-NOT-TO-BE-PRINTED" not in r.stdout + r.stderr, "diagnostics name keys and paths, never values"
    assert box.mutating() == [], "a rejected render must leave no volume, container or restore behind"
    assert not any(a[0] == "run" and "hc-next-restore-extract" in a for a in box.argvs())


@pytest.mark.parametrize("env_file", [".env.local", ".env.gateway.local"])
def test_render_check_rejects_a_production_env_file_in_the_unresolved_render(box, env_file):
    box.cfg["render_raw"] = json.dumps({"services": {"hindsight": {"env_file": ["${HC_ENV_FILE:?}"]},
                                                      "gateway": {"env_file": [{"path": "./" + env_file, "required": False}]}}})
    box.save()
    r = box.run("up")
    assert r.returncode == 1 and "render check" in r.stderr and env_file in r.stderr
    assert box.mutating() == []


def test_a_good_render_reaches_the_restore_and_the_start(box):
    r = box.run("up")
    argvs = box.argvs()
    flat = [" ".join(a) for a in argvs]
    assert r.returncode == 1 and "docker compose up failed" in r.stderr     # the stub's up exits 17
    assert restore_lines(r) == ["restore: backup=%s/backups/20261003-020202/pg0.tar.gz sha256=%s" % (box.prod, sha256_hex(b"x" * 10))], \
        "the newest backup by name"
    assert "pg_verifybackup ok" in r.stdout
    assert any(a[:2] == ["volume", "create"] and a[-1] == "hc-next_pg0" for a in argvs)
    extract = next(a for a in argvs if a[0] == "run" and "hc-next-restore-extract" in a)
    assert f"{box.prod}/backups/20261003-020202:/b:ro" in extract, "the backup is mounted read only"
    assert any("--entrypoint" in a and "ls" in a and BUNDLED_DIR + "/" in a and IMAGE in a for a in argvs), "image mount-point check"
    verify = next(a for a in argvs if a[0] == "run" and "hc-next-restore-verify" in a)
    assert "hc-next=1" in verify and "--network" in verify
    assert flat.index(next(f for f in flat if "restore-extract" in f)) > flat.index(next(f for f in flat if "--entrypoint ls" in f))
    assert "target: gw=127.0.0.1:18888 project=hc-next container=hc-next-hindsight-1 pg0=hc-next_pg0" in r.stderr
    assert r.stderr.count("target:") == 1


def test_a_failed_pg_verifybackup_refuses(box):
    """The restore-verify container fails: `up` stops before it starts anything."""
    box.cfg["fail_run_names"] = ["hc-next-restore-verify"]
    box.save()
    r = box.run("up")
    assert r.returncode == 1 and "not a verified base backup" in r.stderr and "pg_verifybackup ok" not in r.stdout
    assert not any(a[0] == "compose" and "up" in a for a in box.argvs())


# ---------------------------------------------------------------- up: restore source selection (issue #285)

RESTORE_LINE = re.compile(r"^restore: backup=(/\S+)/pg0\.tar\.gz sha256=([0-9a-f]{64})$")


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def restore_lines(r):
    return [ln for ln in r.stdout.splitlines() if ln.startswith("restore:")]


def started_restore(box):
    """True when `up` created the copy volume or extracted into it."""
    return any(a[:2] == ["volume", "create"] or (a[0] == "run" and "hc-next-restore-extract" in a) for a in box.argvs())


def select_backup(box, how, path):
    """(args, env) that name <path> as the restore source: by the environment or by `up --backup`."""
    return ([], {"HC_NEXT_BACKUP_DIR": str(path)}) if how == "env" else (["--backup", str(path)], {})


@pytest.mark.parametrize("how", ["env", "flag"])
def test_a_named_backup_dir_outside_the_daily_rule_is_the_one_restored(box, how):
    """(a) + (d): the named directory is used even though its name is not YYYYMMDD-HHMMSS and a newer daily one exists."""
    args, env = select_backup(box, how, box.prod / "backups" / KEEP)
    r = box.run("up", *args, **env)
    assert "docker compose up failed" in r.stderr, r.stderr          # got through the restore to the start
    assert restore_lines(r) == ["restore: backup=%s/backups/%s/pg0.tar.gz sha256=%s" % (box.prod, KEEP, sha256_hex(KEEP_BYTES))]
    extract = next(a for a in box.argvs() if a[0] == "run" and "hc-next-restore-extract" in a)
    assert f"{box.prod}/backups/{KEEP}:/b:ro" in extract
    assert "20261003-020202" not in r.stdout + " ".join(" ".join(a) for a in box.argvs())


def test_without_a_named_dir_the_newest_daily_backup_is_restored(box):
    """(c) + (d): the old behaviour stays, and the line now carries the sha256 of the file."""
    r = box.run("up")
    assert "docker compose up failed" in r.stderr, r.stderr
    lines = restore_lines(r)
    assert lines == ["restore: backup=%s/backups/20261003-020202/pg0.tar.gz sha256=%s" % (box.prod, sha256_hex(b"x" * 10))]
    assert RESTORE_LINE.match(lines[0])


@pytest.mark.parametrize("how, case", [(h, c) for h in ("env", "flag") for c in ("no-such-dir", "dir-without-archive")]
                         + [("env", "relative")])
def test_a_named_backup_that_is_not_there_is_refused_without_falling_back(box, how, case):
    """(b): exit 1, nothing restored, and never the newest daily backup instead. HC_NEXT_BACKUP_DIR must be absolute
    (an existing relative name is still refused)."""
    path = {"no-such-dir": box.prod / "backups" / "keep-gone",
            "dir-without-archive": box.prod / "backups" / "20261002-000000-empty",
            "relative": "backups/" + KEEP}[case]
    if case == "relative":
        (box.root / "backups" / KEEP).mkdir(parents=True)
        (box.root / "backups" / KEEP / "pg0.tar.gz").write_bytes(KEEP_BYTES)
    args, env = select_backup(box, how, path)
    r = box.run("up", *args, **env)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "backup" in r.stderr
    assert restore_lines(r) == [] and not started_restore(box), "nothing may be restored"
    assert not any(a[0] == "compose" and "up" in a for a in box.argvs())
    assert "20261003-020202" not in r.stdout + r.stderr


@pytest.mark.parametrize("how", ["env", "flag", "default"])
def test_a_sha256_mismatch_refuses_before_anything_is_extracted(box, how):
    """(e): HC_NEXT_BACKUP_SHA256 is compared with the real file before the volume exists; a mismatch ends the run."""
    args, env = ([], {}) if how == "default" else select_backup(box, how, box.prod / "backups" / KEEP)
    r = box.run("up", *args, HC_NEXT_BACKUP_SHA256="0" * 64, **env)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "sha256" in r.stderr
    assert restore_lines(r) == [] and not started_restore(box), "the archive must not be extracted into the volume"
    assert not any(a[0] == "compose" and "up" in a for a in box.argvs())


@pytest.mark.parametrize("given", ["lower", "upper"])
def test_a_matching_sha256_is_accepted_and_printed(box, given):
    want = sha256_hex(KEEP_BYTES)
    r = box.run("up", HC_NEXT_BACKUP_DIR=str(box.prod / "backups" / KEEP),
                HC_NEXT_BACKUP_SHA256=want if given == "lower" else want.upper())
    assert "docker compose up failed" in r.stderr, r.stderr
    assert restore_lines(r) == ["restore: backup=%s/backups/%s/pg0.tar.gz sha256=%s" % (box.prod, KEEP, want)]


@pytest.mark.parametrize("bad", ["abc", "g" * 64, "0" * 63, "0" * 65])
def test_a_malformed_sha256_is_refused_before_anything_happens(box, bad):
    r = box.run("up", HC_NEXT_BACKUP_SHA256=bad)
    assert r.returncode == 1 and "HC_NEXT_BACKUP_SHA256" in r.stderr
    assert not box.state.exists() and box.mutating() == []


def test_the_flag_and_the_env_must_not_name_different_backups(box):
    r = box.run("up", "--backup", str(box.prod / "backups" / KEEP), HC_NEXT_BACKUP_DIR=str(box.prod / "backups" / "20261001-010101"))
    assert r.returncode == 1 and "--backup" in r.stderr
    assert not box.state.exists() and box.mutating() == []


def test_the_restore_line_goes_to_stdout_only(box):
    """The stream is fixed: stdout (the evidence that is attached), never stderr."""
    r = box.run("up", HC_NEXT_BACKUP_DIR=str(box.prod / "backups" / KEEP))
    assert len(restore_lines(r)) == 1 and "restore: backup=" not in r.stderr


def test_every_compose_call_has_the_contract_shape(box):
    box.run("up")
    compose = [a for a in box.argvs() if a[0] == "compose"]
    assert compose
    for a in compose:
        assert a[1:3] == ["--env-file", str(box.state / "env")]
        assert a[3:5] == ["--project-directory", str(box.root)]
        assert a[5:7] == ["-p", "hc-next"], "never without -p hc-next"
        files = [a[i + 1] for i, x in enumerate(a) if x == "-f"]
        assert files == [str(box.root / "docker-compose.yml"), str(box.root / "docker-compose.next.yml")]
        assert "hindsight-crew" not in " ".join(a)


def test_the_callers_shell_values_do_not_beat_the_copy_env_file(box):
    box.run("up", HC_HINDSIGHT_IMAGE="ghcr.io/vectorize-io/hindsight:0.9.1", COMPOSE_PROJECT_NAME="hindsight-crew",
            HC_GATEWAY_BIND="127.0.0.1:8888", HC_COMPOSE_PROJECT="hindsight-crew")
    for c in box.calls():
        if c["argv"][0] == "compose":
            e = c["env"]
            assert e["HC_HINDSIGHT_IMAGE"] is None and e["COMPOSE_PROJECT_NAME"] is None
            assert e["HC_GATEWAY_BIND"] is None and e["HC_COMPOSE_PROJECT"] is None
            assert e["HC_ENV_FILE"] == str(box.state / "env") and e["HC_NEXT_STATE_DIR"] == str(box.state)
            assert e["HC_PROD_ROOT"] == str(box.prod) and e["HC_SECRETS_DIR"] == str(box.prod / "secrets")
            assert len(e["HC_NEXT_COMMIT"]) == 40


# ---------------------------------------------------------------- env / status / rebuild

def up_state(box, running=True):
    """Pretend `up` succeeded: the copy env file and the containers exist."""
    box.run("up")       # writes the state directory (the stub's compose up then fails)
    st = {n: {"running": running, "line": "running (healthy) started=2026-10-14T00:00:00Z"}
          for n in ("hc-next-hindsight-1", "hc-next-gateway-1", "hc-next-ui-tunnel-1")}
    box.cfg["containers"] = st
    box.cfg["ps_a"] = "\n".join("%s||" % n for n in st)
    box.save()
    box.healthz.write_text('{"ok": true, "upstream_ok": true, "upstream": "ok"}')


def test_env_prints_nothing_and_exits_1_when_the_copy_is_not_up(box):
    r = box.run("env")
    assert (r.returncode, r.stdout) == (1, "")
    box.state.mkdir(parents=True)
    (box.state / "env").write_text("HC_NEXT_COMMIT=abc\n")
    r = box.run("env")
    assert (r.returncode, r.stdout) == (1, "")          # state exists, containers do not
    box.cfg["containers"] = {"hc-next-hindsight-1": {"running": True}, "hc-next-gateway-1": {"running": False}}
    box.save()
    r = box.run("env")
    assert (r.returncode, r.stdout) == (1, "")


def test_env_exports_every_contract_variable(box):
    up_state(box)
    r = box.run("env")
    assert r.returncode == 0 and r.stderr == "", r.stderr
    sh = subprocess.run(["bash", "-c", 'eval "$1"; for v in HC_TARGET HC_ENV_FILE HC_COMPOSE_PROJECT HC_COMPOSE_FILES '
                        'HC_GATEWAY_BIND HC_PG0_VOLUME HC_HINDSIGHT_CONTAINER HC_SECRETS_DIR HC_PROD_ROOT '
                        'HC_NEXT_STATE_DIR HC_NEXT_COMMIT; do printf "%s=%s\\n" "$v" "${!v}"; done', "_", r.stdout],
                        capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    got = dict(line.split("=", 1) for line in sh.stdout.splitlines())
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=box.root, capture_output=True, text=True).stdout.strip()
    assert got == {
        "HC_TARGET": "next", "HC_ENV_FILE": str(box.state / "env"), "HC_COMPOSE_PROJECT": "hc-next",
        "HC_COMPOSE_FILES": "%s:%s" % (box.root / "docker-compose.yml", box.root / "docker-compose.next.yml"),
        "HC_GATEWAY_BIND": "127.0.0.1:18888", "HC_PG0_VOLUME": "hc-next_pg0",
        "HC_HINDSIGHT_CONTAINER": "hc-next-hindsight-1", "HC_SECRETS_DIR": str(box.prod / "secrets"),
        "HC_PROD_ROOT": str(box.prod), "HC_NEXT_STATE_DIR": str(box.state), "HC_NEXT_COMMIT": commit}
    assert "HC_TARGET" not in keys_of(box.env_text())


def test_env_takes_the_secrets_dir_from_the_copy_env_file_not_the_callers_shell(box):
    up_state(box)
    r = box.run("env", HC_SECRETS_DIR=str(box.tmp / "other-secrets"))
    assert r.returncode == 0
    assert "other-secrets" not in r.stdout and str(box.prod / "secrets") in r.stdout


@pytest.mark.parametrize("states, healthz, rc", [
    ({"hc-next-hindsight-1": True, "hc-next-gateway-1": True, "hc-next-ui-tunnel-1": True}, '{"upstream_ok": true}', 0),
    ({"hc-next-hindsight-1": True, "hc-next-gateway-1": True, "hc-next-ui-tunnel-1": True}, '{"upstream_ok": false}', 1),
    ({"hc-next-hindsight-1": True, "hc-next-gateway-1": True, "hc-next-ui-tunnel-1": True}, "", 1),
    ({"hc-next-hindsight-1": True, "hc-next-gateway-1": False, "hc-next-ui-tunnel-1": True}, '{"upstream_ok": true}', 1),
    ({"hc-next-hindsight-1": True, "hc-next-gateway-1": True}, '{"upstream_ok": true}', 1),
])
def test_status_exit_codes_when_resources_exist(box, states, healthz, rc):
    box.state.mkdir(parents=True)
    box.cfg["containers"] = {n: {"running": up, "line": "running" if up else "exited"} for n, up in states.items()}
    box.cfg["ps_a"] = "\n".join("%s||" % n for n in states)
    box.save()
    box.healthz.write_text(healthz)
    r = box.run("status")
    assert r.returncode == rc, r.stdout + r.stderr
    assert r.stderr.count("target:") == 1


def test_status_is_3_when_nothing_of_hc_next_exists(box):
    r = box.run("status")
    assert r.returncode == 3 and "no resources" in r.stdout


def test_rebuild_recreates_only_the_gateway_and_stamps_the_new_commit(box):
    up_state(box)
    (box.root / "marker.txt").write_text("new code")
    subprocess.run("git add marker.txt && git -c user.name=t -c user.email=t@example.invalid commit -qm two", shell=True,
                   cwd=box.root, check=True, env=dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null"))
    new = subprocess.run(["git", "rev-parse", "HEAD"], cwd=box.root, capture_output=True, text=True).stdout.strip()
    box.log.write_text("")
    box.cfg["up_rc"] = 0
    box.save()
    r = box.run("rebuild")
    assert r.returncode == 0, r.stderr
    assert "HC_NEXT_COMMIT=%s" % new in box.env_text().splitlines()
    assert f"rebuild ok: commit={new}" in r.stdout
    up = next(a for a in box.argvs() if a[0] == "compose" and "up" in a)
    assert up[up.index("up"):] == ["up", "-d", "--build", "--no-deps", "--force-recreate", "gateway"]
    assert box.calls()[[a[0] for a in box.argvs()].index("compose")]["env"]["HC_NEXT_COMMIT"] == new
    assert not any(a[0] in ("rm", "rmi") or a[:2] == ["volume", "rm"] for a in box.argvs())


def test_a_failed_rebuild_keeps_the_old_commit_in_the_env_file(box):
    """`env` exports HC_NEXT_COMMIT from the file: it must keep naming the code that is actually deployed."""
    up_state(box)
    old = [ln for ln in box.env_text().splitlines() if ln.startswith("HC_NEXT_COMMIT=")]
    (box.root / "marker.txt").write_text("new code")
    subprocess.run("git add marker.txt && git -c user.name=t -c user.email=t@example.invalid commit -qm two", shell=True,
                   cwd=box.root, check=True, env=dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null"))
    r = box.run("rebuild")                      # the stub's compose up exits 17
    assert r.returncode == 1 and "docker compose up gateway failed" in r.stderr
    assert [ln for ln in box.env_text().splitlines() if ln.startswith("HC_NEXT_COMMIT=")] == old


def test_rebuild_without_a_running_copy_is_refused(box):
    r = box.run("rebuild")
    assert r.returncode == 1


# ---------------------------------------------------------------- a failed listing is never "nothing there"

LISTINGS = [(cmd, failing) for cmd in ("up", "status", "down") for failing in ("ps", "volume ls", "network ls")] \
    + [("down", "images")]        # only down lists images


@pytest.mark.parametrize("cmd, failing", LISTINGS)
def test_a_failed_docker_listing_is_an_error_not_an_empty_list(box, failing, cmd):
    box.cfg["fail"] = [failing]
    box.save()
    r = box.run(cmd)
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)       # not 3 (no resources), not 0
    if cmd == "up":
        assert box.mutating() == [] and not box.state.exists()
    if cmd == "down":
        assert removal_names(box) == []


# ---------------------------------------------------------------- down

def removal_names(box):
    return [a[-1] for a in box.argvs() if a[0] in ("rm", "rmi") or a[:2] in (["network", "rm"], ["volume", "rm"])]


def test_down_removes_only_hc_next_resources_and_never_runs_compose(box):
    box.state.mkdir(parents=True)
    (box.state / "env").write_text("x=1\n")
    (box.state / "audit").mkdir()
    (box.state / "audit" / "gateway.log").write_text("log")
    box.cfg.update({
        "ps_a": "hc-next-hindsight-1||\nhc-next-gateway-1||\nhc-next-restore-verify||1\nhindsight-crew-hindsight-1||\nxhc-next-other||",
        "volumes": "hc-next_pg0||\nhindsight-crew_pg0||\nhindsight-crew_hfcache||",
        "networks": "hc-next_internal||\nbridge||",
        "images_fmt": "abc123|hc-next-gateway\ndef456|ghcr.io/vectorize-io/hindsight",
        "images_q": {"label=com.docker.compose.project=hc-next": "abc123\nfff000", "label=hc.next.commit": "abc123"},
        "image_tags": {"abc123": "hc-next-gateway:latest ", "fff000": ""},
    })
    box.save()
    r = box.run("down")
    assert r.returncode == 0, r.stderr
    names = removal_names(box)
    assert sorted(names) == sorted(["hc-next-hindsight-1", "hc-next-gateway-1", "hc-next-restore-verify", "hc-next_internal",
                                    "hc-next_pg0", "abc123", "fff000"])
    assert not any("hindsight-crew" in n for n in names)
    assert not any(a[0] == "compose" for a in box.argvs()), "down must not need a compose render"
    assert not box.state.exists()
    assert r.stderr.count("target:") == 1


def test_down_is_safe_to_repeat_with_nothing_left(box):
    for _ in range(2):
        r = box.run("down")
        assert r.returncode == 0 and "down ok" in r.stdout
    assert removal_names(box) == []


FOREIGN = {
    "container-labelled-hc-next-but-production-named": {"ps_a": "hindsight-crew-gateway-1||1"},
    "container-with-hc-next-project-label": {"ps_a": "somebody-else|hc-next|"},
    "volume-labelled": {"volumes": "hindsight-crew_pg0|hc-next|"},
    "network-labelled": {"networks": "hindsight-crew_internal||1"},
    "image-with-a-production-tag": {"images_q": {"label=hc.next.commit": "abc"}, "image_tags": {"abc": "hindsight-crew-gateway:latest "}},
    "image-with-an-unrelated-tag": {"images_q": {"label=hc.next.commit": "abc"}, "image_tags": {"abc": "hc-next-gateway:latest other:1 "}},
}


@pytest.mark.parametrize("case", list(FOREIGN))
def test_down_refuses_the_whole_run_when_a_foreign_name_is_in_the_list(box, case):
    box.state.mkdir(parents=True)
    box.cfg.update({"ps_a": "hc-next-gateway-1||", "volumes": "hc-next_pg0||"})
    box.cfg.update(FOREIGN[case])
    box.save()
    r = box.run("down")
    assert r.returncode == 1, r.stdout + r.stderr
    assert removal_names(box) == [], "nothing may be deleted when one name is foreign"
    assert box.state.exists()


def test_the_name_guard_helper_accepts_only_hc_next_names():
    src = (REPO / "scripts" / "next-stack.sh").read_text()
    start = src.index("assert_next_name() {")
    helper = src[start:src.index("\n}\n", start) + 3]
    script = ('die() { echo "$*" >&2; exit 1; }\n' + helper +
              'for n in hc-next hc-next-gateway-1 hc-next_pg0; do ( assert_next_name "$n" ) || exit 9; done\n'
              'for n in hindsight-crew_pg0 hc-nextx xhc-next ""; do ( assert_next_name "$n" ) 2>/dev/null && exit 8; done\n'
              'echo ok\n')
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    assert r.stdout.strip() == "ok", r.stdout + r.stderr


# ---------------------------------------------------------------- docker-compose.next.yml (static)

def compose_text():
    return (REPO / "docker-compose.next.yml").read_text()


def code_lines(text):
    return [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def test_override_cuts_the_production_inputs():
    code = "\n".join(code_lines(compose_text()))
    assert code.count("env_file: !override") == 2 and "${HC_ENV_FILE:?}" in code
    for forbidden in (".env.local", ".env.gateway.local", "hsauth", "overlays/", "hindsight-crew_pg0", "./audit",
                      "/Users/"):
        assert forbidden not in code, forbidden
    assert "hindsight-crew_hfcache" in code and "external: true" in code
    assert "name: hc-next_pg0" in code


def test_no_service_of_the_copy_restarts_by_itself():
    """The base file says restart: unless-stopped; a copy that auto-starts after a Docker/colima restart skips the
    MemAvailable pre-check of `up`. Every service the copy runs must override it to "no"."""
    blocks, current = {}, None
    for ln in code_lines(compose_text()):
        if ln.startswith("  ") and not ln.startswith("    ") and ln.rstrip().endswith(":") and current != "volumes":
            current = ln.strip().rstrip(":")
            blocks[current] = []
        elif not ln.startswith(" "):
            current = ln.strip().rstrip(":")
        elif current in blocks:
            blocks[current].append(ln.strip())
    assert set(blocks) == {"hindsight", "gateway", "ui-tunnel"}
    for name, lines in blocks.items():
        assert 'restart: "no"' in lines, name


def test_override_pins_ports_limits_and_commit_label():
    code = "\n".join(code_lines(compose_text()))
    assert '"127.0.0.1:18888:8888"' in code and '"127.0.0.1:9998:9999"' in code
    assert code.count("ports: !override") == 2
    assert "mem_limit: ${HC_NEXT_MEM_LIMIT:-3g}" in code and "oom_score_adj: 1000" in code
    assert 'HF_HUB_OFFLINE: "1"' in code and "read_only: true" in code
    assert 'hc.next.commit: "${HC_NEXT_COMMIT:?}"' in code


@pytest.mark.parametrize("var", ["HC_ENV_FILE", "HC_PROD_ROOT", "HC_SECRETS_DIR", "HC_NEXT_STATE_DIR", "HC_NEXT_COMMIT"])
def test_every_path_variable_of_the_override_is_required(var):
    """An empty path would silently become a VM root path such as /policies (measured); ${VAR:?} fails the render."""
    import re
    uses = re.findall(r"\$\{%s([^}]*)\}" % var, compose_text())
    assert uses and all(u == ":?" for u in uses), uses


def test_the_override_does_not_modify_the_base_compose_file():
    base = (REPO / "docker-compose.yml").read_text()
    assert "hc-next" not in base and "HC_NEXT" not in base


def test_scripts_hold_no_owner_literals_or_em_dashes():
    for f in (REPO / "scripts" / "next-stack.sh", REPO / "docker-compose.next.yml"):
        text = f.read_text()
        assert "\u2014" not in text, f.name
        assert "personal-demo" not in text and "/Users/" not in text, f.name


def test_the_default_image_is_the_pinned_digest():
    assert "NEXT_DEFAULT_IMAGE='%s'" % IMAGE in (REPO / "scripts" / "next-stack.sh").read_text()


def test_hc_next_image_selects_the_v1_0_digest_for_the_rollback_check(box):
    """#285: the same gates run on a v1.0.0 copy (production until the window) chosen by HC_NEXT_IMAGE."""
    box.set_render(good_render(box.prod, box.state, image=V1_0_IMAGE))
    r = box.run("up", HC_NEXT_IMAGE=V1_0_IMAGE)
    assert "render check" not in r.stderr, r.stderr
    assert "HC_HINDSIGHT_IMAGE='%s'" % V1_0_IMAGE in box.env_text().splitlines()
    assert any("--entrypoint" in a and V1_0_IMAGE in a for a in box.argvs())


# ---------------------------------------------------------------- both production layouts (before and after #249)

def test_both_production_layouts_give_the_same_copy_chain(layout_box):
    box = layout_box
    r = box.run("up", "--llm", "opus")
    assert_no_secrets(r)
    assert "production LLM layout: %s" % ("old" if box.layout == "pre-migration" else "target") in r.stderr
    got = _llm_values(box.env_text())
    account1, account2 = SECRET_VALUES["HINDSIGHT_API_LLM_1_API_KEY"], SECRET_VALUES["HINDSIGHT_API_LLM_2_API_KEY"]
    assert got["HINDSIGHT_API_LLM_PROVIDER"] == "claude-code" and got["HINDSIGHT_API_LLM_MODEL"] == "claude-sonnet-5-5"
    assert got["HINDSIGHT_API_LLM_API_KEY"] == account1 and got["HINDSIGHT_API_LLM_1_API_KEY"] == account2
    assert got[MM + "1_API_KEY"] == account2 and got[MM + "STRATEGY"] == '{"mode":"failover"}'
    assert not any(k.startswith("HINDSIGHT_API_LLM_2_") for k in got)
    assert SECRET_VALUES["HINDSIGHT_API_LLM_API_KEY"] not in box.env_text(), "the grok key stays out"


def test_both_production_layouts_break_only_the_global_primary(layout_box):
    box = layout_box
    r = box.run("up", "--llm", "opus", "--break-primary")
    assert_no_secrets(r)
    got = _llm_values(box.env_text())
    assert got["HINDSIGHT_API_LLM_API_KEY"] == "hc-next-invalid-primary"
    account2 = SECRET_VALUES["HINDSIGHT_API_LLM_2_API_KEY"]
    assert got["HINDSIGHT_API_LLM_1_API_KEY"] == account2 and got[MM + "1_API_KEY"] == account2
    assert SECRET_VALUES["HINDSIGHT_API_LLM_1_API_KEY"] not in box.env_text(), "the account1 token is gone from the copy"


@pytest.mark.parametrize("layout", ["pre-migration", "target"])
def test_the_copy_env_of_either_layout_is_what_the_migrate_script_calls_the_target(box, tmp_path, layout):
    if layout == "target":
        (box.prod / ".env.local").write_text(prod_env_target_layout())
    box.run("up", "--llm", "opus")
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "llm-migrate-opus.py"), "--env-file", str(box.state / "env"),
                        "--classify"], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "layout: target", r.stdout + r.stderr


NEITHER = {
    "none-profile": lambda t: t.replace("HINDSIGHT_API_LLM_PROVIDER=claude-code", "HINDSIGHT_API_LLM_PROVIDER=none"),
    "target-primary-not-claude-code": lambda t: t.replace("HINDSIGHT_API_LLM_PROVIDER=claude-code", "HINDSIGHT_API_LLM_PROVIDER=xai-oauth"),
    "target-member-not-claude-code": lambda t: t.replace("HINDSIGHT_API_LLM_1_PROVIDER=claude-code", "HINDSIGHT_API_LLM_1_PROVIDER=anthropic"),
    "target-model-other": lambda t: t.replace("HINDSIGHT_API_LLM_MODEL=claude-sonnet-5-5", "HINDSIGHT_API_LLM_MODEL=claude-opus-4-8"),
    "target-refresh-chain-missing": lambda t: "\n".join(ln for ln in t.splitlines() if "MENTAL_MODEL_REFRESH" not in ln) + "\n",
    "target-tokens-equal": lambda t: t.replace("HINDSIGHT_API_LLM_1_API_KEY=" + SECRET_VALUES["HINDSIGHT_API_LLM_2_API_KEY"],
                                               "HINDSIGHT_API_LLM_1_API_KEY=" + SECRET_VALUES["HINDSIGHT_API_LLM_1_API_KEY"]),
    "target-with-xai-left": lambda t: t + "XAI_API_KEY=x\n",
}


@pytest.mark.parametrize("case", sorted(NEITHER))
def test_a_production_file_in_neither_layout_is_refused_before_anything_is_created(box, case):
    (box.prod / ".env.local").write_text(NEITHER[case](prod_env_target_layout()))
    r = box.run("up", "--llm", "opus")
    assert r.returncode == 1 and "pre-migration layout" in r.stderr and "target layout" in r.stderr
    assert not box.state.exists() and box.mutating() == []
    assert_no_secrets(r)
