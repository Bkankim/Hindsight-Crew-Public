"""`./install --owner <slug>` (issue #189).

Hermetic: the install runs from a scratch copy of the repo (so .env.local, secrets/, policies/local/ land
under tmp_path), HOME is a tmp directory, and the tools it calls (launchctl, docker, colima, claude, uv, uname,
sysctl, df) are stubs at the front of PATH that log their argv. `./bootstrap` in the scratch copy is a stub too.
Nothing here touches the real HOME, ~/Library/LaunchAgents, ~/.claude.json or a docker stack. Every credential
is a short fake string.
"""
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from gateway.bank_policies import BankPolicies  # noqa: E402
from gateway.rules import evaluate_item  # noqa: E402

FAKE_TOKEN = "sk-ant-oat01-FAKE-setup-token-0123456789abcdef"
TOKVAR = "HC_TEST_CLAUDE_SETUP_TOKEN"
COPIED = ("install", "scripts", "profiles", "templates", "policies", "gateway", "rerank-server/launchd")
LABELS = ("rerank", "rerank-probe", "rerank-logrotate", "model-refresh", "model-audit", "model-watch")
PREFIX = "com.hindsight-crew"
REAL_TOOLS = ("bash", "openssl", "env", "dirname", "mkdir", "grep", "cut", "tail", "head", "sort", "basename", "mv",
              "touch", "cat", "id", "sed", "tr", "awk", "chmod", "rm", "uname", "ls", "date", "wc")

STUBS = {
    "uname": '''case "$1" in -s) echo "${STUB_UNAME_S:-Darwin}";; -m) echo "${STUB_UNAME_M:-arm64}";; *) /usr/bin/uname "$@";; esac''',
    "sysctl": '''echo "$STUB_NAME $*" >> "$STUB_LOG"; echo "${STUB_MEM_BYTES:-34359738368}"''',
    "df": '''printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\n/dev/x 976490576 1 %s 1%% /\\n' "${STUB_DF_AVAIL_KB:-209715200}"''',
    "colima": '''exit 0''',
    "uv": '''echo "HOME=$HOME HF_HOME=${HF_HOME-UNSET} HF_HUB_CACHE=${HF_HUB_CACHE-UNSET} HUGGINGFACE_HUB_CACHE=${HUGGINGFACE_HUB_CACHE-UNSET} XDG_CACHE_HOME=${XDG_CACHE_HOME-UNSET} HF_HUB_OFFLINE=${HF_HUB_OFFLINE-UNSET}" >> "$STUB_STATE/uv.env"
case " $* " in
  *" check "*) [ -n "$STUB_UV_CHECK_MSG" ] && printf 'first line\n%s\n' "$STUB_UV_CHECK_MSG" >&2; exit "${STUB_UV_CHECK_RC:-0}" ;;
  *" download "*) exit "${STUB_UV_DL_RC:-0}" ;;
esac
exit 0''',
    "claude": '''[ "$1" = "--version" ] && echo "2.1.282 (Claude Code)"; exit 0''',
    "git": '''exit 0''',
    "docker": '''case "$*" in
  *" ps -q gateway"*) [ -n "$STUB_GW_ID" ] && echo "$STUB_GW_ID"; exit 0 ;;
  inspect*) if [ -f "$STUB_STATE/gw.started" ]; then cat "$STUB_STATE/gw.started"; else echo "$STUB_GW_STARTED"; fi; exit 0 ;;
  *" restart gateway"*) [ "${STUB_RESTART_RC:-0}" = 0 ] && date -u +%Y-%m-%dT%H:%M:%S.000000000Z > "$STUB_STATE/gw.started"; exit "${STUB_RESTART_RC:-0}" ;;
esac
[ "$1" = "info" ] && exit "${STUB_DOCKER_INFO_RC:-0}"; exit 0''',
    "launchctl": '''state="$STUB_STATE/launchctl.loaded"; touch "$state"
case "$1" in
  bootstrap) basename "$3" .plist >> "$state" ;;
  bootout) l="${2##*/}"; grep -qx "$l" "$state" || exit 113; grep -vx "$l" "$state" > "$state.new"; mv "$state.new" "$state" ;;
  print) l="${2##*/}"; echo "environment = { $STUB_LEAK_NAME => $STUB_LEAK_VALUE }"; grep -qx "$l" "$state" || exit 113 ;;
esac''',
}
BOOTSTRAP_STUB = '''#!/bin/bash
echo "bootstrap $*" >> "$STUB_LOG"
env | cut -d= -f1 | sort > "$STUB_STATE/bootstrap.envnames"
echo "bootstrap stub: verify-all stub"
exit "${STUB_BOOTSTRAP_RC:-0}"
'''


class Box:
    """One scratch repo + HOME + stub dir."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.root = tmp_path / "repo"
        self.home = tmp_path / "home"
        self.bin = tmp_path / "stubs"
        self.state = tmp_path / "state"
        self.log = self.state / "calls.log"
        for d in (self.root, self.home, self.bin, self.state):
            d.mkdir()
        for rel in COPIED:
            src = REPO / rel
            dst = self.root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "tests"))
            else:
                shutil.copy2(src, dst)
        (self.root / "bootstrap").write_text(BOOTSTRAP_STUB)
        (self.root / "bootstrap").chmod(0o755)
        for name, body in STUBS.items():
            f = self.bin / name
            f.write_text('#!/bin/bash\nSTUB_NAME=%s\necho "%s $*" >> "$STUB_LOG"\n%s\n' % (name, name, body))
            f.chmod(0o755)
        self.log.write_text("")
        # PATH = stubs + these real tools only, so nothing on the developer's PATH (a real docker, colima,
        # launchctl, uv...) can be reached, and "tool X is missing" is a deterministic case
        self.tools = tmp_path / "tools"
        self.tools.mkdir()
        for name in REAL_TOOLS:
            real = shutil.which(name)
            if real:
                (self.tools / name).symlink_to(real)
        (self.tools / "python3").symlink_to(os.path.realpath(sys.executable))
        self.extra_env = {}

    def env(self, **extra):
        env = {
            "PATH": "%s:%s" % (self.bin, self.tools),
            "HOME": str(self.home),
            "STUB_LOG": str(self.log),
            "STUB_STATE": str(self.state),
            "STUB_LEAK_NAME": TOKVAR,       # `launchctl print` output carries the session env (#176)
            "STUB_LEAK_VALUE": FAKE_TOKEN,
            TOKVAR: FAKE_TOKEN,
        }
        env.update(self.extra_env)
        env.update(extra)
        return env

    def run(self, *args, **env_extra):
        return subprocess.run([str(self.root / "install"), *args], cwd=str(self.root), env=self.env(**env_extra),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)

    def install(self, owner="alice", *flags, **env_extra):
        return self.run("--owner", owner, "--claude-token-env", TOKVAR, *flags, **env_extra)

    # --- inspection
    def calls(self):
        return self.log.read_text().splitlines()

    def snapshot(self):
        out = {}
        for base in (self.root, self.home):
            for p in sorted(base.rglob("*")):
                if p.is_symlink():
                    out[str(p.relative_to(self.tmp))] = "symlink->" + os.readlink(str(p))
                elif p.is_file():
                    out[str(p.relative_to(self.tmp))] = p.read_bytes()
        return out

    def acl(self):
        return json.loads((self.root / "secrets" / "acl.json").read_text())

    def owner_token(self, owner="alice"):
        return next(t for t, s in self.acl()["tokens"].items()
                    if s["identity"] == owner and s["personal"] == "personal-" + owner and not s["teams"])

    def agents(self):
        d = self.home / "Library" / "LaunchAgents"
        return sorted(p.name for p in d.glob("*.plist")) if d.exists() else []


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


def ok(r):
    assert r.returncode == 0, "exit %d\n--- stdout\n%s\n--- stderr\n%s" % (r.returncode, r.stdout, r.stderr)
    return r


# --- the full flow ------------------------------------------------------------------------------

@pytest.mark.parametrize("owner", ["alice", "carol"])
def test_full_install_creates_everything(box, owner):
    r = ok(box.install(owner))
    bank = "personal-" + owner
    # 2 .env.local: demo seed in profile ko-full-mac-mps, then the claude-code LLM keys
    env = {k: v for k, v in (l.split("=", 1) for l in (box.root / ".env.local").read_text().splitlines()
                              if "=" in l and not l.startswith("#"))}
    assert env["HC_PROFILE"] == "ko-full-mac-mps"
    assert env["HINDSIGHT_API_LLM_PROVIDER"] == "claude-code"
    assert env["HINDSIGHT_API_LLM_MODEL"] == "claude-sonnet-5-5"
    assert env["HINDSIGHT_API_LLM_API_KEY"] == FAKE_TOKEN
    assert stat.S_IMODE((box.root / ".env.local").stat().st_mode) == 0o600
    # 3 acl entry, .env.host.local, rendered policy
    acl = box.acl()
    token = box.owner_token(owner)
    assert re.fullmatch(r"mt_[0-9a-f]{40}", token)
    assert acl["tokens"][token] == {"identity": owner, "personal": bank, "teams": []}
    assert next(iter(acl["tokens"])) == token                       # first, for "first token of HC_OWNER" readers
    assert (box.root / "secrets" / "demo-tokens.env").exists()      # the verify gates' demo tokens survive
    host = (box.root / ".env.host.local").read_text().splitlines()
    assert host == ["HC_OWNER=" + owner, "HC_PERSONAL_BANK=" + bank, "HC_LAUNCHD_PREFIX=" + PREFIX]
    assert stat.S_IMODE((box.root / ".env.host.local").stat().st_mode) == 0o600
    local = json.loads((box.root / "policies" / "local" / "bank-policies.json").read_text())
    assert list(local["banks"]) == [bank]
    assert local["banks"][bank]["tag_vocabulary"]["user"] == [owner]
    assert "__OWNER__" not in json.dumps(local)
    merged = BankPolicies.from_files(str(box.root / "policies" / "bank-policies.json"))
    assert bank in merged.bank_names()
    # 4 launchd: six plists, bootstrapped, scheduled-checks absent
    expect = sorted("%s.hindsight-%s.plist" % (PREFIX, n) for n in LABELS)
    assert box.agents() == expect
    for name in expect:
        pl = plistlib.loads((box.home / "Library" / "LaunchAgents" / name).read_bytes())
        assert pl["Label"] + ".plist" == name
        assert "__" not in json.dumps(pl)
        assert str(box.root) in json.dumps(pl) and str(box.home) in json.dumps(pl)
    assert sorted((box.state / "launchctl.loaded").read_text().split()) == sorted(n[:-6] for n in expect)
    assert not any("scheduled-checks" in c for c in box.calls())
    assert (box.home / "Library" / "Logs" / "hindsight-rerank").is_dir()
    # 5 bootstrap ran once with the sidecar profile
    assert [c for c in box.calls() if c.startswith("bootstrap")] == ["bootstrap --mac-mps"]
    # 6 Claude Code connection
    cfg = json.loads((box.home / ".claude.json").read_text())
    assert cfg["mcpServers"]["memory-vault"] == {
        "type": "http", "url": "http://127.0.0.1:8888/mcp/" + bank, "headers": {"Authorization": "Bearer " + token}}
    # 7 not without --write-claude-md
    assert not (box.home / ".claude" / "CLAUDE.md").exists()
    assert "templates/CLAUDE.memory.md" in r.stdout


def test_claude_md_block_is_written_replaced_and_kept_single(box):
    ok(box.install("alice", "--write-claude-md"))
    md = box.home / ".claude" / "CLAUDE.md"
    text = md.read_text()
    assert text.count("<!-- hindsight-crew:begin -->") == 1 and text.count("<!-- hindsight-crew:end -->") == 1
    assert "personal-alice" in text and "`kind:`" in text and "__" not in text
    assert "`probe`" in text            # kind values come from the policy template, not a copy
    md.write_text(text.replace("recall first", "STALE"))
    ok(box.install("alice", "--write-claude-md"))
    assert md.read_text() == text       # a stale block is replaced, never duplicated


# --- idempotent ---------------------------------------------------------------------------------

@pytest.mark.parametrize("flags", [(), ("--write-claude-md",)])
def test_second_run_changes_nothing(box, flags):
    ok(box.install("alice", *flags))
    first = box.snapshot()
    first_calls = len(box.calls())
    r = ok(box.install("alice", *flags))
    assert box.snapshot() == first
    second = box.calls()[first_calls:]
    assert not [c for c in second if c.startswith("launchctl bootstrap") or c.startswith("launchctl bootout")]
    assert "unchanged and loaded - skipped" in r.stdout
    assert list((box.home).glob(".claude.json.hindsight-crew-backup-*")) == []   # nothing existed to back up


def test_rerun_reloads_an_agent_whose_plist_changed(box):
    ok(box.install("alice"))
    plist = box.home / "Library" / "LaunchAgents" / ("%s.hindsight-model-audit.plist" % PREFIX)
    good = plist.read_bytes()
    plist.write_bytes(good + b"<!-- drift -->")
    before = len(box.calls())
    ok(box.install("alice"))
    assert plist.read_bytes() == good
    calls = box.calls()[before:]
    assert [c for c in calls if c.startswith("launchctl bootstrap")] == [
        "launchctl bootstrap gui/%d %s" % (os.getuid(), plist)]


def test_rerun_keeps_an_existing_customized_policy_bank(box):
    ok(box.install("alice"))
    path = box.root / "policies" / "local" / "bank-policies.json"
    data = json.loads(path.read_text())
    data["banks"]["personal-alice"]["tag_vocabulary"]["project"] = ["mine"]
    data["banks"]["team-like"] = data["banks"]["personal-alice"]
    path.write_text(json.dumps(data))
    ok(box.install("alice"))
    assert json.loads(path.read_text()) == data


# --- uninstall restores ---------------------------------------------------------------------------

CLAUDE_JSON_STATES = {
    "absent": None,
    "bare": {"numStartups": 3},
    "other-servers": {"numStartups": 3, "mcpServers": {"playwright": {"type": "stdio", "command": "x", "args": []}}},
}
CLAUDE_MD_STATES = {"absent": None, "user-text": "# mine\n\nrule one\n", "symlink": "# willow copy\n",
                    "symlink-empty": ""}


@pytest.mark.parametrize("md_state", sorted(CLAUDE_MD_STATES))
@pytest.mark.parametrize("json_state", sorted(CLAUDE_JSON_STATES))
def test_uninstall_restores_home(box, json_state, md_state):
    cfg0, md0 = CLAUDE_JSON_STATES[json_state], CLAUDE_MD_STATES[md_state]
    if cfg0 is not None:
        (box.home / ".claude.json").write_text(json.dumps(cfg0, indent=2) + "\n")
    md = box.home / ".claude" / "CLAUDE.md"
    if md0 is not None:
        md.parent.mkdir()
        if md_state.startswith("symlink"):
            real = box.tmp / "willow" / "CLAUDE.md"
            real.parent.mkdir()
            real.write_text(md0)
            md.symlink_to(real)
        else:
            md.write_text(md0)
    ok(box.install("alice", "--write-claude-md"))
    assert "hindsight-crew:begin" in md.read_text()
    if md_state.startswith("symlink"):
        assert md.is_symlink()                               # written through, the link survives
    if cfg0 is not None:
        backups = list(box.home.glob(".claude.json.hindsight-crew-backup-*"))
        assert len(backups) == 1 and json.loads(backups[0].read_text()) == cfg0
        assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600

    r = ok(box.run("--uninstall"))                           # owner and prefix come from .env.host.local
    backups = sorted(box.home.glob(".claude.json.hindsight-crew-backup-*"))
    assert len(backups) == (cfg0 is not None) + 1            # install backed up the original, uninstall the installed state
    assert any("memory-vault" in b.read_text() for b in backups)
    assert all(stat.S_IMODE(b.stat().st_mode) == 0o600 for b in backups)
    assert box.agents() == []
    assert (box.state / "launchctl.loaded").read_text().split() == []
    if cfg0 is None:
        assert not (box.home / ".claude.json").exists()
    else:
        assert json.loads((box.home / ".claude.json").read_text()) == cfg0
    if md0 is None:
        assert not md.exists() and not md.parent.exists()
    else:
        assert md.read_text() == md0
        assert md.is_symlink() == md_state.startswith("symlink")
        assert md.exists()                                   # a symlinked CLAUDE.md target is never deleted
    # repo-side config and the stack stay; no volume is touched without --purge
    assert (box.root / ".env.local").exists() and (box.root / "secrets" / "acl.json").exists()
    assert not any(c.startswith("docker compose") and " down" in c for c in box.calls())
    assert "data volumes" in r.stdout


def test_uninstall_leaves_a_foreign_memory_vault_entry(box):
    ok(box.install("alice"))
    cfgp = box.home / ".claude.json"
    cfg = json.loads(cfgp.read_text())
    cfg["mcpServers"]["memory-vault"]["url"] = "http://127.0.0.1:8888/mcp/personal-someone"
    cfgp.write_text(json.dumps(cfg))
    ok(box.run("--uninstall"))
    assert json.loads(cfgp.read_text()) == cfg


def test_purge_runs_compose_down_with_volumes(box):
    ok(box.install("alice"))
    ok(box.run("--uninstall", "--purge"))
    assert "docker compose --env-file .env.local down -v" in box.calls()


def test_replaced_memory_vault_entry_is_backed_up(box):
    old = {"mcpServers": {"memory-vault": {"type": "http", "url": "http://127.0.0.1:8888/mcp/personal-other",
                                           "headers": {"Authorization": "Bearer old"}}}}
    (box.home / ".claude.json").write_text(json.dumps(old))
    r = ok(box.install("alice"))
    assert "pointed at http://127.0.0.1:8888/mcp/personal-other" in r.stdout
    (backup,) = box.home.glob(".claude.json.hindsight-crew-backup-*")
    assert json.loads(backup.read_text()) == old


# --- secrets stay off argv, stdout, stderr --------------------------------------------------------

def test_no_secret_reaches_argv_stdout_stderr_or_children(box):
    r = ok(box.install("alice", "--write-claude-md"))
    r2 = ok(box.install("alice", "--write-claude-md"))
    r3 = ok(box.run("--uninstall"))
    owner_tok = box.owner_token()          # the owner entry stays in acl.json after uninstall
    upstream = next(l.split("=", 1)[1] for l in (box.root / ".env.local").read_text().splitlines()
                    if l.startswith("HC_HINDSIGHT_API_KEY="))
    demo = [t for t in box.acl()["tokens"] if t != owner_tok]
    secrets_ = [FAKE_TOKEN, owner_tok, upstream, *demo]
    assert len(secrets_) >= 5
    seen = "\n".join(box.calls())            # argv of every stub the install called
    for out in (seen, r.stdout, r.stderr, r2.stdout, r2.stderr, r3.stdout, r3.stderr):
        for s in secrets_:
            assert s not in out
    # the setup-token variable is not inherited by children (bootstrap stub dumps its env var names)
    assert TOKVAR not in (box.state / "bootstrap.envnames").read_text().split()
    # and the secrets that must exist in files do
    assert FAKE_TOKEN in (box.root / ".env.local").read_text()


def test_token_flag_takes_a_variable_name_never_a_value(box):
    r = box.run("--owner", "alice", "--claude-token-env", "HC_TEST_UNSET_VARIABLE")
    assert r.returncode == 1 and "$HC_TEST_UNSET_VARIABLE is empty or unset" in r.stderr
    assert not (box.root / ".env.local").exists()


def test_without_token_flag_llm_keys_stay(box):
    ok(box.run("--owner", "alice"))
    env = (box.root / ".env.local").read_text()
    assert "HINDSIGHT_API_LLM_PROVIDER=none" in env and "HINDSIGHT_API_LLM_API_KEY" not in env
    ok(box.install("alice"))                    # adding the token later switches to claude-code
    assert "HINDSIGHT_API_LLM_PROVIDER=claude-code" in (box.root / ".env.local").read_text()
    ok(box.run("--owner", "alice"))             # and a rerun without the flag leaves it
    assert "HINDSIGHT_API_LLM_API_KEY=" + FAKE_TOKEN in (box.root / ".env.local").read_text()


# --- dry-run ------------------------------------------------------------------------------------

@pytest.mark.parametrize("args", [
    ("--owner", "alice", "--claude-token-env", TOKVAR, "--write-claude-md"),
    ("--uninstall", "--owner", "alice", "--purge"),
])
@pytest.mark.parametrize("installed_first", [False, True])
def test_dry_run_changes_nothing(box, args, installed_first):
    if installed_first:
        ok(box.install("alice", "--write-claude-md"))
    before, calls = box.snapshot(), len(box.calls())
    r = ok(box.run(*args, "--dry-run"))
    assert box.snapshot() == before
    mutating = ("bootstrap", "launchctl bootstrap", "launchctl bootout", "docker compose --env-file", "docker compose up")
    assert not [c for c in box.calls()[calls:] if c.startswith(mutating)]
    assert "DRY RUN" in r.stdout
    assert "would:" in r.stdout or (args[0] == "--uninstall" and not installed_first)
    assert FAKE_TOKEN not in r.stdout + r.stderr


def test_dry_run_on_an_empty_checkout_lists_every_step(box):
    r = ok(box.install("alice", "--write-claude-md", "--dry-run"))
    for marker in ("[1/7] preflight", "[2/7] .env.local", "[3/7] owner identity", "[4/7] launchd agents",
                   "[5/7] bootstrap", "[6/7] Claude Code connection", "[7/7] CLAUDE.md block"):
        assert marker in r.stdout
    assert not (box.root / ".env.local").exists() and not (box.root / "secrets").exists()
    assert box.agents() == []


# --- preflight ----------------------------------------------------------------------------------

@pytest.mark.parametrize("case,env,remove,msg", [
    ("intel", {"STUB_UNAME_M": "x86_64"}, None, "needs macOS on Apple Silicon"),
    ("linux", {"STUB_UNAME_S": "Linux"}, None, "needs macOS on Apple Silicon"),
    ("no-claude", {}, "claude", "claude not found on PATH"),
    ("no-colima", {}, "colima", "colima not found on PATH"),
    ("no-uv", {}, "uv", "uv not found on PATH"),
    ("docker-down", {"STUB_DOCKER_INFO_RC": "1"}, None, "docker daemon is not reachable"),
])
def test_preflight_failure_stops_before_any_change(box, case, env, remove, msg):
    if remove:
        (box.bin / remove).unlink()
    before = box.snapshot()
    r = box.install("alice", **env)
    assert r.returncode == 1, r.stdout
    assert msg in r.stdout and "preflight failed" in r.stderr
    assert box.snapshot() == before


@pytest.mark.parametrize("env,msg", [
    ({"STUB_MEM_BYTES": str(8 << 30)}, "RAM 8 GB is below the 16 GB"),
    ({"STUB_DF_AVAIL_KB": str(10 << 20)}, "free disk 10 GB is below 30 GB"),
])
def test_low_ram_or_disk_only_warns(box, env, msg):
    r = ok(box.install("carol", **env))
    assert "WARN " + msg in r.stdout and "warnings (1):" in r.stdout


def test_dry_run_reports_a_failed_precheck_with_exit_1_and_still_changes_nothing(box):
    before = box.snapshot()
    r = box.install("alice", "--dry-run", STUB_UNAME_M="x86_64")
    assert r.returncode == 1 and "a real run would stop here" in r.stdout
    assert box.snapshot() == before


def test_model_watch_job_renders_daily_0930_with_the_watch_arguments(box):
    """issue #428: the daily watch agent, rendered under a temporary HOME; the refresh job stays registered."""
    sys.path.insert(0, str(REPO / "scripts" / "install"))
    import hc_install
    assert ("scripts/launchd", "model-watch") in hc_install.JOBS
    assert ("scripts/launchd", "model-refresh") in hc_install.JOBS      # removed only after #251's observation
    assert "hindsight-model-refresh" in hc_install.LOG_DIRS             # the watch log's directory
    ok(box.install("alice"))
    label = "%s.hindsight-model-watch" % PREFIX
    pl = plistlib.loads((box.home / "Library" / "LaunchAgents" / (label + ".plist")).read_bytes())
    assert pl["Label"] == label
    assert pl["ProgramArguments"] == ["/bin/bash", str(box.root / "scripts" / "model-watch.sh"), "--grace-hours", "2"]
    assert pl["StartCalendarInterval"] == {"Hour": 9, "Minute": 30}     # every day: no Weekday/Day key
    log = str(box.home / "Library" / "Logs" / "hindsight-model-refresh" / "watch.log")
    assert pl["StandardOutPath"] == log and pl["StandardErrorPath"] == log
    assert (box.home / "Library" / "Logs" / "hindsight-model-refresh").is_dir()
    assert label in (box.state / "launchctl.loaded").read_text().split()     # the stub launchctl, not the real one
    assert (box.root / "scripts" / "model-watch.sh").exists() and (box.root / "scripts" / "model_watch.py").exists()


# --- failure paths ------------------------------------------------------------------------------

def test_failed_bootstrap_skips_the_claude_connection(box):
    (box.home / ".claude.json").write_text('{"numStartups": 1}\n')
    r = box.install("alice", "--write-claude-md", STUB_BOOTSTRAP_RC="1")
    assert r.returncode == 1 and "./bootstrap exited 1" in r.stderr
    assert json.loads((box.home / ".claude.json").read_text()) == {"numStartups": 1}
    assert not (box.home / ".claude").exists()
    assert len(box.agents()) == 6            # steps 2-4 stay in place, as the message says


@pytest.mark.parametrize("slug", ["Alice", "a_b", "1abc", "al ice", "", "x" * 33, "../x"])
def test_invalid_slug_is_refused(box, slug):
    r = box.run("--owner", slug)
    assert r.returncode == 1
    assert "invalid owner slug" in r.stderr or "--owner <slug> is required" in r.stderr
    assert not (box.root / ".env.local").exists()


def test_reinstall_for_another_owner_is_refused_before_anything_is_written(box):
    ok(box.install("alice"))
    before = box.snapshot()
    r = box.install("carol", **{TOKVAR: "another-fake-token"})
    assert r.returncode == 1 and "installed for another owner" in r.stderr
    assert box.snapshot() == before            # .env.local, acl.json, policies, claude.json all untouched


def test_prefix_flag_and_conflict(box):
    ok(box.install("alice", "--launchd-prefix", "org.example"))
    assert box.agents() == sorted("org.example.hindsight-%s.plist" % n for n in LABELS)
    r = box.install("alice", "--launchd-prefix", "org.other")
    assert r.returncode == 1 and "labelled with the old prefix" in r.stderr


def test_existing_host_file_keys_are_preserved(box):
    (box.root / ".env.host.local").write_text("# mine\nHC_NOTIFY_DISCORD_CHANNEL_ID=123\n")
    ok(box.install("alice"))
    text = (box.root / ".env.host.local").read_text()
    assert text.startswith("# mine\nHC_NOTIFY_DISCORD_CHANNEL_ID=123\n") and "HC_OWNER=alice" in text


def test_broken_claude_json_is_left_alone(box):
    (box.home / ".claude.json").write_text("{not json")
    r = box.install("alice")
    assert r.returncode == 1 and "not valid JSON" in r.stderr
    assert (box.home / ".claude.json").read_text() == "{not json"


# --- slug that overlaps the demo identities (#187 final-review 8) ---------------------------------

GATE3_PAYLOAD = {"content": "[검증] verify roundtrip marker rt-1 regarding quarterly budget",
                 "tags": ["project:hindsight-crew", "kind:probe"]}
GATE7_UNTAGGED = {"content": "[검증] gate7 untagged probe g7-1"}
NOW = "2026-10-08T00:00:00Z"


@pytest.mark.parametrize("owner,replaces,demo_member", [
    ("alice", True, True),     # tracked demo bank personal-alice + demo member alice
    ("bob", False, True),      # demo member bob only
    ("carol", False, False),   # no overlap
])
def test_demo_slug_collision_rule(box, owner, replaces, demo_member):
    tracked = box.root / "policies" / "bank-policies.json"
    tracked_before = tracked.read_bytes()
    demo_before = (REPO / "policies" / "bank-policies.json").read_bytes()
    r = ok(box.install(owner))
    bank = "personal-" + owner
    assert (tracked.read_bytes() == tracked_before == demo_before)          # tracked policy never edited
    assert ("REPLACES the tracked demo bank" in r.stdout) is replaces
    assert ("overlaps the demo install" in r.stdout) is (replaces or demo_member)
    if replaces or demo_member:
        assert "Pick another slug for real use" in r.stdout
    # the demo member entry keeps its token and teams; the owner has its own separate token
    acl = box.acl()["tokens"]
    owner_tok = box.owner_token(owner)
    demo = [t for t, s in acl.items() if s["identity"] == owner and s["teams"]]
    assert bool(demo) is demo_member and owner_tok not in demo
    if demo_member:
        assert acl[demo[0]] == {"identity": owner, "personal": bank, "teams": ["team-eng"]}
        demo_env = (box.root / "secrets" / "demo-tokens.env").read_text()
        assert demo[0] in demo_env                                          # the gates' demo token is intact
    # merged policy as the gateway loads it
    merged = BankPolicies.from_files(str(tracked))
    assert bank in merged.bank_names() and "team-eng" in merged.bank_names() and "personal-alice" in merged.bank_names()
    if owner == "alice":
        # gate3/gate7 still pass against the owner template that replaced the demo bank
        policy = merged.for_bank("personal-alice")
        assert evaluate_item(dict(GATE3_PAYLOAD), policy, NOW).ok
        assert "RULE:tags-required" in evaluate_item(dict(GATE7_UNTAGGED), policy, NOW).rule_ids
        assert policy.allowed_strategies == ()                             # gate7 step 5 compares this with upstream
        assert policy.tags_required and policy.recall_max_tokens_floor == 4096
    # MCP connection uses the owner token, not the demo token
    cfg = json.loads((box.home / ".claude.json").read_text())
    assert cfg["mcpServers"]["memory-vault"]["headers"]["Authorization"] == "Bearer " + owner_tok
    # rerun: same decision, no second owner entry
    ok(box.install(owner))
    assert len([t for t, s in box.acl()["tokens"].items() if s["identity"] == owner and not s["teams"]]) == 1


# --- templates ----------------------------------------------------------------------------------

def test_tracked_files_have_no_placeholders_left_and_install_is_executable():
    assert os.access(str(REPO / "install"), os.X_OK)
    assert (REPO / "templates" / "CLAUDE.memory.md").read_text().count("__OWNER__") >= 1


# --- review fixes -------------------------------------------------------------------------------

@pytest.mark.parametrize("break_file", ["policy-overlay", "host-file-not-utf8", "acl-shape"])
def test_malformed_config_is_a_clean_error_not_a_traceback(box, break_file):
    ok(box.install("alice"))
    if break_file == "policy-overlay":
        (box.root / "policies" / "local" / "bank-policies.json").write_text('{"unknown": 1}')
        owner = "carol"
    elif break_file == "host-file-not-utf8":
        (box.root / ".env.host.local").write_bytes(b"HC_OWNER=\xff\xfe\n")
        owner = "alice"
    else:
        (box.root / "secrets" / "acl.json").write_text('{"tokens": ["not", "a", "dict"]}')
        owner = "alice"
    r = box.install(owner)
    assert r.returncode == 1
    assert "install: ERROR" in r.stderr and "Traceback" not in r.stderr and FAKE_TOKEN not in r.stderr
    if break_file == "policy-overlay":      # the bad overlay is found before an ACL token is written for carol
        assert not any(s["identity"] == "carol" for s in box.acl()["tokens"].values())


def test_claude_md_kinds_follow_the_local_policy_not_the_template(box):
    ok(box.install("alice", "--write-claude-md"))
    md = box.home / ".claude" / "CLAUDE.md"
    assert "`probe`" in md.read_text()
    path = box.root / "policies" / "local" / "bank-policies.json"
    data = json.loads(path.read_text())
    data["banks"]["personal-alice"]["tag_vocabulary"]["kind"].remove("probe")
    path.write_text(json.dumps(data))
    ok(box.install("alice", "--write-claude-md"))
    text = md.read_text()
    assert "`probe`" not in text and "`decision`" in text


def restarts(box):
    return [c for c in box.calls() if c == "docker compose --env-file .env.local restart gateway"]


def test_gateway_restart_is_decided_from_file_times_so_a_failed_run_does_not_lose_it(box):
    env = {"STUB_GW_ID": "gw1", "STUB_GW_STARTED": "2000-01-01T00:00:00Z"}
    r = box.install("alice", STUB_RESTART_RC="1", **env)           # acl.json and policy are written, then the restart fails
    assert r.returncode == 1 and "restart gateway failed" in r.stderr
    assert box.owner_token() and len(restarts(box)) == 1
    r = ok(box.install("alice", **env))                           # token now "present - kept", yet the stale gateway restarts
    assert "newer than the running gateway" in r.stdout and len(restarts(box)) == 2
    ok(box.install("alice", **env))                               # the restart refreshed StartedAt: nothing to do
    assert len(restarts(box)) == 2


@pytest.mark.parametrize("env,expect", [
    ({}, 0),                                                      # no gateway container yet (fresh machine)
    ({"STUB_GW_ID": "gw1", "STUB_GW_STARTED": "2099-01-01T00:00:00Z"}, 0),   # gateway newer than the files
    ({"STUB_GW_ID": "gw1", "STUB_GW_STARTED": ""}, 0),            # StartedAt unreadable: no blind restart
    ({"STUB_GW_ID": "gw1", "STUB_GW_STARTED": "2000-01-01T00:00:00Z"}, 1),
])
def test_gateway_restart_cases(box, env, expect):
    ok(box.install("alice", **env))
    assert len(restarts(box)) == expect
    # always before ./bootstrap
    calls = box.calls()
    if expect:
        assert calls.index("docker compose --env-file .env.local restart gateway") < calls.index("bootstrap --mac-mps")


def test_llm_keys_are_backed_up_before_a_real_llm_setup_is_replaced(box):
    ok(box.install("alice"))
    assert list(box.root.glob(".env.local.hindsight-crew-backup-*")) == []     # replacing the seeded `none` needs no backup
    ok(box.install("alice"))
    assert list(box.root.glob(".env.local.hindsight-crew-backup-*")) == []     # same token: nothing changed, no backup
    ok(box.install("alice", **{TOKVAR: "rotated-fake-token"}))
    (backup,) = box.root.glob(".env.local.hindsight-crew-backup-*")
    assert "HINDSIGHT_API_LLM_API_KEY=" + FAKE_TOKEN in backup.read_text()
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert "HINDSIGHT_API_LLM_API_KEY=rotated-fake-token" in (box.root / ".env.local").read_text()


# --- rerank model preload (issue #215) ----------------------------------------------------------

TEMPLATE_ENV = plistlib.loads(
    (REPO / "rerank-server" / "launchd" / "__PREFIX__.hindsight-rerank.plist.example").read_bytes())["EnvironmentVariables"]
MODEL, REVISION = TEMPLATE_ENV["RERANK_MODEL"], TEMPLATE_ENV["RERANK_MODEL_REVISION"]
HF_LEAK = {"HF_HOME": "/nonexistent/hf-home", "HF_HUB_CACHE": "/nonexistent/hub", "HUGGINGFACE_HUB_CACHE": "/nonexistent/old",
           "XDG_CACHE_HOME": "/nonexistent/xdg", "HF_HUB_OFFLINE": "1"}


def uv_calls(box, kind):
    return [c for c in box.calls() if c.startswith("uv ") and (" %s " % kind) in (c + " ")]


def uv_envs(box):
    f = box.state / "uv.env"
    return f.read_text().splitlines() if f.exists() else []


def test_preload_downloads_once_with_the_pinned_revision_before_launchctl_bootstrap(box):
    ok(box.install("alice", STUB_UV_CHECK_RC="1"))
    checks, downloads = uv_calls(box, "check"), uv_calls(box, "download")
    assert len(checks) == 1 and len(downloads) == 1
    for c in checks + downloads:
        assert "--project %s/rerank-server" % box.root in c
        assert c.endswith(" %s %s" % (MODEL, REVISION))       # values come from the plist template
    calls = box.calls()
    first_bootstrap = next(i for i, c in enumerate(calls) if c.startswith("launchctl bootstrap"))
    assert calls.index(checks[0]) < calls.index(downloads[0]) < first_bootstrap


@pytest.mark.parametrize("check_rc", ["1", "2"])   # nothing cached / only part of it cached: both are a miss
def test_preload_downloads_on_any_failed_offline_check(box, check_rc):
    ok(box.install("alice", STUB_UV_CHECK_RC=check_rc))
    assert len(uv_calls(box, "download")) == 1


def test_preload_skips_the_download_when_the_cache_is_complete(box):
    r = ok(box.install("alice", STUB_UV_CHECK_RC="0"))
    assert len(uv_calls(box, "check")) == 1 and uv_calls(box, "download") == []
    assert "cached" in r.stdout


def test_preload_failure_stops_before_any_launchd_agent_is_installed(box):
    r = box.install("alice", STUB_UV_CHECK_RC="1", STUB_UV_DL_RC="1")
    assert r.returncode == 1 and "rerank" in r.stderr
    assert box.agents() == []
    assert not [c for c in box.calls() if c.startswith("launchctl bootstrap")]


def test_preload_env_has_no_hf_cache_overrides_and_the_rendered_home(box):
    ok(box.install("alice", STUB_UV_CHECK_RC="1", **HF_LEAK))
    envs = uv_envs(box)
    assert len(envs) >= 2
    pre = [e for e, c in zip(envs, [c for c in box.calls() if c.startswith("uv ")]) if " check " in c + " " or " download " in c + " "]
    assert len(pre) == 2
    for e in pre:
        assert "HOME=%s " % box.home in e + " "
        for name in ("HF_HOME", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "XDG_CACHE_HOME"):
            assert "%s=UNSET" % name in e
    assert "HF_HUB_OFFLINE=1" in pre[0] and "HF_HUB_OFFLINE=0" in pre[1]   # the download is the one online step


def make_rerank_venv(box):
    py = box.root / "rerank-server" / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("")


@pytest.mark.parametrize("venv, check_rc, wording", [
    (True, "0", "cached, skip"),
    (True, "1", "would download %s@%s" % (MODEL, REVISION)),
    (False, "1", "would sync rerank-server env and check/download %s@%s (cache not checked: env missing)" % (MODEL, REVISION)),
    (True, "3", "cache not checked: check failed (exit 3)"),
])
def test_preload_dry_run_only_reports_and_never_downloads_or_syncs(box, venv, check_rc, wording):
    if venv:
        make_rerank_venv(box)
    before = box.snapshot()
    r = ok(box.install("alice", "--dry-run", STUB_UV_CHECK_RC=check_rc, **HF_LEAK))
    assert wording in r.stdout
    assert uv_calls(box, "download") == []
    uv = [c for c in box.calls() if c.startswith("uv ")]
    assert all(" run " in c and "--no-sync" in c for c in uv)       # nothing that could sync the env
    assert len(uv) == (1 if venv else 0)
    for e in uv_envs(box):
        assert "HOME=%s " % box.home in e + " " and "HF_HUB_OFFLINE=1" in e
        for name in ("HF_HOME", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "XDG_CACHE_HOME"):
            assert "%s=UNSET" % name in e
    assert box.snapshot() == before


def test_preload_download_failure_reports_the_helper_exit_and_stops(box):
    r = box.install("alice", STUB_UV_CHECK_RC="1", STUB_UV_DL_RC="4")
    assert r.returncode == 1 and "exit 4" in r.stderr
    assert box.agents() == []


def test_preload_announces_the_sync_and_the_download_size(box):
    r = ok(box.install("alice", STUB_UV_CHECK_RC="1"))
    assert "rerank-server env sync + cache check" in r.stdout
    assert "downloading %s@%s (~2.3GB, once)" % (MODEL, REVISION) in r.stdout
    assert r.stdout.index("env sync + cache check") < r.stdout.index("downloading")


def test_preload_check_that_could_not_run_downloads_and_logs_its_last_line_without_secrets(box):
    r = ok(box.install("alice", STUB_UV_CHECK_RC="3", STUB_UV_CHECK_MSG="rerank-preload: check error: AttributeError: mismatched"))
    assert len(uv_calls(box, "download")) == 1
    assert "check could not run (exit 3): rerank-preload: check error: AttributeError: mismatched" in r.stdout
    assert "first line" not in r.stdout
    assert FAKE_TOKEN not in r.stdout + r.stderr


def test_preload_plain_cache_miss_logs_no_error_line(box):
    r = ok(box.install("alice", STUB_UV_CHECK_RC="1", STUB_UV_CHECK_MSG="rerank-preload: config.json not cached"))
    assert "check could not run" not in r.stdout


def test_preload_dry_run_with_an_unrunnable_uv_fails_but_still_prints_the_plan(box):
    make_rerank_venv(box)
    (box.bin / "uv").write_text("#!/nonexistent/interpreter\n")        # on PATH (preflight passes) but exec fails
    r = box.install("alice", "--dry-run")
    assert r.returncode == 1
    assert "uv not runnable" in r.stdout and "dry run done" in r.stdout


def test_preload_dry_run_with_an_unreadable_template_fails_but_still_prints_the_plan(box):
    (box.root / "rerank-server" / "launchd" / "__PREFIX__.hindsight-rerank.plist.example").write_text("not a plist")
    r = box.install("alice", "--dry-run")
    assert r.returncode == 1
    assert "FAIL cannot read RERANK_MODEL" in r.stdout and "dry run done" in r.stdout


def test_preload_revision_is_not_a_literal_in_the_installer():
    for f in (REPO / "scripts" / "install").glob("*.py"):
        assert REVISION not in f.read_text(), f.name


# the offline check itself, with a fake huggingface_hub (the real one is only in rerank-server's env)

def load_preload(monkeypatch, cached, snapshot_adds=()):
    """`cached` is the set of files present; snapshot_download adds `snapshot_adds` to it (a download that
    returns normally but leaves files missing is the C8 case)."""
    import importlib.util
    import types
    calls = []
    cached = set(cached)

    def hf_hub_download(repo_id, filename, revision=None, local_files_only=False, **kw):
        calls.append((repo_id, filename, revision, local_files_only))
        if filename not in cached:
            raise FileNotFoundError(filename)
        return "/x/" + filename

    fake = types.ModuleType("huggingface_hub")
    fake.hf_hub_download = hf_hub_download

    def snapshot_download(repo_id, revision=None, **kw):
        calls.append(("snapshot", repo_id, revision))
        cached.update(snapshot_adds)
        return "/x/snapshot"

    fake.snapshot_download = snapshot_download
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake)
    spec = importlib.util.spec_from_file_location("rerank_preload_under_test", REPO / "scripts" / "install" / "rerank_preload.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, calls


def test_cache_check_needs_every_required_file(monkeypatch):
    probe_mod, _ = load_preload(monkeypatch, set())
    required = set(probe_mod.REQUIRED_FILES)
    assert {"config.json", "model.safetensors"} <= required and any("tokenizer" in f for f in required)
    mod, calls = load_preload(monkeypatch, required)
    assert mod.main(["check", MODEL, REVISION]) == 0
    assert {c[1] for c in calls} == required
    assert all(c[0] == MODEL and c[2] == REVISION and c[3] is True for c in calls)
    for missing in sorted(required):
        mod, _ = load_preload(monkeypatch, required - {missing})
        assert mod.main(["check", MODEL, REVISION]) == 1, missing
    mod, _ = load_preload(monkeypatch, set())
    assert mod.main(["check", MODEL, REVISION]) == 1


def test_cache_check_without_huggingface_hub_is_not_a_miss(monkeypatch):
    mod, _ = load_preload(monkeypatch, set())
    monkeypatch.setitem(sys.modules, "huggingface_hub", None)      # import raises ImportError
    assert mod.main(["check", MODEL, REVISION]) == 3


def test_cache_check_exits_1_only_for_a_missing_file_and_3_for_any_other_error(monkeypatch, capsys):
    mod, _ = load_preload(monkeypatch, set())
    import huggingface_hub

    class LocalEntryNotFoundError(Exception):       # a hub version whose class is not a FileNotFoundError
        pass

    def boom(exc):
        def f(**kw):
            raise exc
        return f

    huggingface_hub.hf_hub_download = boom(LocalEntryNotFoundError("nope"))
    assert mod.main(["check", MODEL, REVISION]) == 1
    for exc in (AttributeError("mismatched dependency"), RuntimeError("boom"), PermissionError("denied")):
        huggingface_hub.hf_hub_download = boom(exc)
        capsys.readouterr()
        assert mod.main(["check", MODEL, REVISION]) == 3, exc
        assert type(exc).__name__ in capsys.readouterr().out


def test_download_that_returns_with_files_missing_exits_nonzero(monkeypatch, capsys):
    probe_mod, _ = load_preload(monkeypatch, set())
    required = set(probe_mod.REQUIRED_FILES)
    mod, calls = load_preload(monkeypatch, set(), snapshot_adds=required - {"model.safetensors"})
    assert mod.main(["download", MODEL, REVISION]) == 4          # snapshot_download returned, the cache is still partial
    assert "model.safetensors" in capsys.readouterr().out
    assert ("snapshot", MODEL, REVISION) in calls
    mod, _ = load_preload(monkeypatch, set(), snapshot_adds=required)
    assert mod.main(["download", MODEL, REVISION]) == 0
