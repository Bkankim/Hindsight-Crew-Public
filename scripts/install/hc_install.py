#!/usr/bin/env python3
"""`./install` implementation (issue #189). Stdlib only, python 3.8+ (runs on the system python3).

    ./install --owner <slug> [--claude-token-env VAR] [--write-claude-md] [--launchd-prefix P] [--dry-run]
    ./install --uninstall [--owner <slug>] [--purge] [--dry-run]

Install steps, in this order (every step is idempotent; a rerun changes nothing that is already right):
  1 preflight   macOS + arm64, colima/docker/docker compose/uv/git/claude present, docker daemon up;
                RAM < 16 GB and free disk < 30 GB only warn.
  2 .env.local  scripts/seed-demo.sh (profile ko-full-mac-mps; also seeds the demo ACL + demo tokens the
                verify gates use), then, with --claude-token-env, the claude-code LLM keys.
  3 owner       secrets/acl.json token (identity=<slug>, personal=personal-<slug>), .env.host.local,
                policies/local/bank-policies.json rendered from policies/templates/personal-bank.json.
  4 launchd     the rerank sidecar runs offline, so its pinned model revision is fetched into the HF cache first
                if it is not there; then rerank, rerank-probe, rerank-logrotate, model-refresh, model-audit, model-watch
                rendered with scripts/render-launchd.py (scheduled-checks is a maintainer canary, not installed).
  5 bootstrap   ./bootstrap --mac-mps; verify-all GREEN is the pass condition (red: steps 6-7 are skipped).
  6 claude code mcpServers.memory-vault in ~/.claude.json (backup first).
  7 CLAUDE.md   only with --write-claude-md: templates/CLAUDE.memory.md between hindsight-crew markers.

`--uninstall` boots out the six agents and removes their plists, the memory-vault entry and the CLAUDE.md
block. Repo-side config (.env.local, secrets/, .env.host.local, policies/local/) and the data volumes stay;
`--purge` also runs `docker compose down -v`. `--dry-run` prints the plan and changes nothing.

Secrets: no token is ever put on a command line or printed. The setup-token is read from the environment
variable named by --claude-token-env, written to .env.local (mode 600), and removed from the environment of
every child process.
"""
from __future__ import annotations

import argparse
import calendar
import datetime
import json
import os
import plistlib
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.dont_write_bytecode = True   # a dry run must not leave __pycache__ behind in the checkout
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import hc_host_env  # noqa: E402  (stdlib only, same repo)

MIN_RAM_GB = 16
MIN_DISK_GB = 30
PROFILE = "ko-full-mac-mps"
LLM_PROVIDER = "claude-code"
LLM_MODEL = "claude-sonnet-5-5"
DEFAULT_PREFIX = "com.hindsight-crew"
DEFAULT_BIND = "127.0.0.1:8888"
MCP_NAME = "memory-vault"
BLOCK_BEGIN = "<!-- hindsight-crew:begin -->"
BLOCK_END = "<!-- hindsight-crew:end -->"
# (template directory, agent name). The label is `<prefix>.hindsight-<name>`.
JOBS = (
    ("rerank-server/launchd", "rerank"),
    ("rerank-server/launchd", "rerank-probe"),
    ("rerank-server/launchd", "rerank-logrotate"),
    ("scripts/launchd", "model-refresh"),
    ("scripts/launchd", "model-audit"),
    # daily refresh failure watch (issue #428); logs into hindsight-model-refresh/, already in LOG_DIRS
    ("scripts/launchd", "model-watch"),
)
RERANK_TEMPLATE = ROOT / "rerank-server" / "launchd" / "__PREFIX__.hindsight-rerank.plist.example"
RERANK_PRELOAD = ROOT / "scripts" / "install" / "rerank_preload.py"
# the sidecar plist sets none of these, so the preload must not see them either: same default cache
HF_CACHE_ENV = ("HF_HOME", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "XDG_CACHE_HOME")
LOG_DIRS = ("hindsight-rerank", "hindsight-model-refresh")
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,63}$")
TEMPLATE_POLICY = ROOT / "policies" / "templates" / "personal-bank.json"
TRACKED_POLICY = ROOT / "policies" / "bank-policies.json"
LOCAL_POLICY = ROOT / "policies" / "local" / "bank-policies.json"
SNIPPET = ROOT / "templates" / "CLAUDE.memory.md"


class InstallError(Exception):
    pass


class Ctx:
    def __init__(self, args):
        self.args = args
        self.dry = args.dry_run
        home = os.environ.get("HOME", "")
        if not home:
            raise InstallError("HOME is empty")
        self.home = Path(home)
        self.owner = args.owner
        self.token_env = args.claude_token_env
        self.failed = False          # dry-run: a precheck that would abort a real run
        self.warnings = []

    @property
    def bank(self):
        return "personal-" + self.owner

    def say(self, msg=""):
        print(msg, flush=True)

    def step(self, n, total, title):
        self.say("")
        self.say("==> [%d/%d] %s" % (n, total, title))

    def note(self, msg):
        self.say("    " + msg)

    def act(self, msg):
        """A change: executed (prefix `-`) or, in dry-run, only announced."""
        self.say("    %s %s" % ("would:" if self.dry else "-", msg))

    def warn(self, msg):
        self.warnings.append(msg)
        self.say("    WARN " + msg)

    def child_env(self, extra=None):
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if self.token_env:
            env.pop(self.token_env, None)   # the setup-token never reaches a child process
        if extra:
            env.update(extra)
        return env


# --- small file helpers -------------------------------------------------------------------------

def atomic_write(path, data, mode=None):
    """Write `data` next to `path`, then rename. `mode` defaults to the existing file's mode, else 0o600.
    A symlinked path is written through (the link stays a link)."""
    path = Path(os.path.realpath(str(path)))
    if mode is None:
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp, str(path))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def backup_file(path, ctx):
    """Timestamped copy next to `path` (mode 600). Returns the backup path."""
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(path.name + ".hindsight-crew-backup-" + stamp)
    n = 1
    while backup.exists():   # two backups in one second must not overwrite each other
        n += 1
        backup = path.with_name("%s.hindsight-crew-backup-%s-%d" % (path.name, stamp, n))
    shutil.copyfile(str(path), str(backup))
    os.chmod(str(backup), 0o600)
    ctx.note("backup: %s" % backup)
    return backup


def read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError:
        raise InstallError("%s is not valid UTF-8" % path)


def read_json(path, what):
    text = read_text(path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError as e:
        raise InstallError("%s is not valid JSON (%s); left untouched" % (what, e))


def dump_json(obj):
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def run(ctx, argv, **kw):
    kw.setdefault("env", ctx.child_env())
    kw.setdefault("cwd", str(ROOT))
    return subprocess.run(argv, **kw)


def probe(ctx, argv):
    """stdout of a read-only tool, or None when it fails."""
    try:
        r = run(ctx, argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True)
    except OSError:
        return None
    return r.stdout.strip() if r.returncode == 0 else None


# --- 1 preflight --------------------------------------------------------------------------------

def preflight(ctx):
    problems = []

    def ok(msg):
        ctx.note("ok   " + msg)

    def bad(msg):
        problems.append(msg)
        ctx.note("FAIL " + msg)

    system, machine = probe(ctx, ["uname", "-s"]), probe(ctx, ["uname", "-m"])
    if system == "Darwin" and machine == "arm64":
        ok("macOS arm64")
    else:
        bad("needs macOS on Apple Silicon (arm64); found %s/%s" % (system or "?", machine or "?"))
    for tool, hint in (("colima", "brew install colima"), ("docker", "brew install docker"),
                       ("uv", "brew install uv"), ("git", "xcode-select --install"),
                       ("claude", "see https://docs.claude.com/en/docs/claude-code")):
        found = shutil.which(tool)
        if found:
            ok("%s (%s)" % (tool, found))
        else:
            bad("%s not found on PATH (%s)" % (tool, hint))
    if shutil.which("docker"):
        if probe(ctx, ["docker", "compose", "version"]) is not None:
            ok("docker compose")
        else:
            bad("`docker compose` is not available (brew install docker-compose)")
        if probe(ctx, ["docker", "info"]) is not None:
            ok("docker daemon reachable")
        else:
            bad("docker daemon is not reachable (colima start --cpu 6 --memory 10)")
    mem = probe(ctx, ["sysctl", "-n", "hw.memsize"])
    if mem and mem.isdigit():
        gb = int(mem) / float(1 << 30)
        (ok if gb >= MIN_RAM_GB else ctx.warn)("RAM %.0f GB%s" % (
            gb, "" if gb >= MIN_RAM_GB else " is below the %d GB this stack is sized for" % MIN_RAM_GB))
    else:
        ctx.warn("could not read RAM size (sysctl hw.memsize)")
    df = probe(ctx, ["df", "-Pk", str(ROOT)])
    try:
        free_gb = int(df.splitlines()[-1].split()[3]) / float(1 << 20)
    except (AttributeError, IndexError, ValueError):
        free_gb = None
    if free_gb is None:
        ctx.warn("could not read free disk space")
    else:
        (ok if free_gb >= MIN_DISK_GB else ctx.warn)("free disk %.0f GB%s" % (
            free_gb, "" if free_gb >= MIN_DISK_GB else " is below %d GB (images and models need room)" % MIN_DISK_GB))
    if ctx.token_env:
        if os.environ.get(ctx.token_env):
            ok("setup-token variable $%s is set (value not shown)" % ctx.token_env)
        else:
            bad("$%s is empty or unset (export your claude setup-token into it)" % ctx.token_env)
    if problems:
        if ctx.dry:
            ctx.failed = True
            ctx.note("a real run would stop here")
        else:
            raise InstallError("preflight failed: " + "; ".join(problems))


# --- 2 .env.local -------------------------------------------------------------------------------

def set_env_keys(path, updates, ctx=None):
    """Set KEY=VALUE lines in a dotenv file (replace the first match, else append). Returns changed key names.
    The file is rewritten at mode 600 when it changed: it now holds a credential."""
    text = read_text(path) or ""
    lines = text.splitlines()
    changed = []
    for key, value in updates.items():
        new = "%s=%s" % (key, value)
        for i, line in enumerate(lines):
            if line.startswith(key + "="):
                if line != new:
                    lines[i] = new
                    changed.append(key)
                break
        else:
            lines.append(new)
            changed.append(key)
    if changed:
        if ctx is not None and path.exists() and current_llm_provider(path) not in ("", "none"):
            backup_file(path, ctx)   # a real LLM setup is about to be replaced: keep the old keys
        atomic_write(path, "\n".join(lines) + "\n", mode=0o600)
    return changed


def current_llm_provider(path):
    for line in (read_text(path) or "").splitlines():
        if line.startswith("HINDSIGHT_API_LLM_PROVIDER="):
            return line.split("=", 1)[1].strip()
    return ""


def env_local(ctx):
    path = ROOT / ".env.local"
    exists = path.exists()
    if ctx.dry:
        ctx.act("%s via scripts/seed-demo.sh (profile %s; demo ACL + demo tokens for the verify gates)" % (
            "check .env.local" if exists else "create .env.local", PROFILE))
    else:
        r = run(ctx, ["bash", "scripts/seed-demo.sh"], env=ctx.child_env({"HC_PROFILE": PROFILE}),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        for line in r.stdout.splitlines():
            ctx.note(line)
        if r.returncode != 0:
            raise InstallError("scripts/seed-demo.sh failed (exit %d)" % r.returncode)
    current = ""
    for line in (read_text(path) or "").splitlines():
        if line.startswith("HC_PROFILE="):
            current = line.split("=", 1)[1]
    if exists and current and current != PROFILE:
        ctx.warn("existing .env.local is locked to profile %s (a boot-0 choice); install keeps it, "
                 "so the host rerank agents may be unused" % current)
    if not ctx.token_env:
        ctx.note("no --claude-token-env: LLM keys left as they are (without a claude-code LLM, retain/reflect "
                 "run without one; rerun with --claude-token-env VAR to add it)")
        return
    ctx.act("set HINDSIGHT_API_LLM_PROVIDER=%s, HINDSIGHT_API_LLM_MODEL=%s, HINDSIGHT_API_LLM_API_KEY "
            "(from $%s, value not shown) in .env.local" % (LLM_PROVIDER, LLM_MODEL, ctx.token_env))
    if not ctx.dry:
        changed = set_env_keys(path, {
            "HINDSIGHT_API_LLM_PROVIDER": LLM_PROVIDER,
            "HINDSIGHT_API_LLM_MODEL": LLM_MODEL,
            "HINDSIGHT_API_LLM_API_KEY": os.environ[ctx.token_env],
        }, ctx=ctx)
        ctx.note("LLM keys changed: %s" % (", ".join(changed) if changed else "none (already set)"))


# --- 3 owner identity, host settings, policy overlay --------------------------------------------

def demo_collision(ctx, acl):
    """Why this slug overlaps the demo install, or []. The demo identities come from seed-demo.sh (alice,
    bob) and the tracked policy file (personal-alice); both are read, none is hardcoded here."""
    reasons = []
    tracked = read_json(TRACKED_POLICY, "policies/bank-policies.json") or {}
    if ctx.bank in (tracked.get("banks") or {}):
        reasons.append("the tracked demo policy already declares bank %s" % ctx.bank)
    for spec in ((acl or {}).get("tokens") or {}).values():
        if spec.get("identity") == ctx.owner and spec.get("teams"):
            reasons.append("secrets/acl.json already has a demo member %s (team token)" % ctx.owner)
            break
    return reasons


def owner_token_of(acl, owner, bank):
    for tok, spec in ((acl or {}).get("tokens") or {}).items():
        if spec.get("identity") == owner and spec.get("personal") == bank and not spec.get("teams"):
            return tok
    return None


def owner_acl(ctx):
    path = ROOT / "secrets" / "acl.json"
    acl = read_json(path, "secrets/acl.json")
    if acl is None and not ctx.dry:
        acl = {"tokens": {}}
    token = owner_token_of(acl, ctx.owner, ctx.bank)
    reasons = demo_collision(ctx, acl)
    if reasons:
        ctx.warn("slug '%s' overlaps the demo install: %s. Pick another slug for real use: the verify gates "
                 "write probe memories into %s on every run." % (ctx.owner, "; ".join(reasons), ctx.bank))
    if token:
        ctx.note("acl: owner token for %s (%s) present - kept" % (ctx.owner, ctx.bank))
        return
    ctx.act("add a CSPRNG token to secrets/acl.json: identity=%s personal=%s (no teams)" % (ctx.owner, ctx.bank))
    if ctx.dry:
        return
    token = "mt_" + secrets.token_hex(20)
    old = (acl or {}).get("tokens") or {}
    new = dict(acl or {})
    # first, so scripts that pick "the first token of HC_OWNER" (model-refresh) use the owner's own token
    new["tokens"] = dict([(token, {"identity": ctx.owner, "personal": ctx.bank, "teams": []})] + list(old.items()))
    atomic_write(path, dump_json(new), mode=None)


def host_env_resolve(ctx):
    """Read-only: the launchd prefix to use and the keys .env.host.local still lacks. Refuses a conflicting
    owner/prefix, so install runs it before anything is written."""
    path = ROOT / hc_host_env.FILENAME
    have = hc_host_env.load(ROOT)
    prefix = ctx.args.launchd_prefix or have.get("HC_LAUNCHD_PREFIX") or DEFAULT_PREFIX
    if not PREFIX_RE.match(prefix):
        raise InstallError("invalid launchd prefix %r" % prefix)
    if have.get("HC_LAUNCHD_PREFIX") and prefix != have["HC_LAUNCHD_PREFIX"]:
        raise InstallError("%s has HC_LAUNCHD_PREFIX=%s but --launchd-prefix is %s; installed agents are labelled "
                           "with the old prefix (run ./install --uninstall first)" % (path.name, have["HC_LAUNCHD_PREFIX"], prefix))
    for key, want in (("HC_OWNER", ctx.owner), ("HC_PERSONAL_BANK", ctx.bank)):
        if have.get(key) and have[key] != want:
            raise InstallError("%s has %s=%s but --owner is %s; this checkout is installed for another owner "
                               "(./install --uninstall first)" % (path.name, key, have[key], ctx.owner))
    missing = [(k, v) for k, v in (("HC_OWNER", ctx.owner), ("HC_PERSONAL_BANK", ctx.bank),
                                    ("HC_LAUNCHD_PREFIX", prefix)) if not have.get(k)]
    return prefix, missing


def host_env(ctx):
    path = ROOT / hc_host_env.FILENAME
    prefix, missing = host_env_resolve(ctx)
    if not missing:
        ctx.note("%s complete - kept" % path.name)
    else:
        ctx.act("write %s to %s (mode 600)" % (", ".join("%s=%s" % kv for kv in missing), path.name))
        if not ctx.dry:
            text = read_text(path) or ""
            if text and not text.endswith("\n"):
                text += "\n"
            atomic_write(path, text + "".join("%s=%s\n" % kv for kv in missing), mode=None)
    return prefix


def render_policy(ctx):
    raw = read_text(TEMPLATE_POLICY)
    if raw is None:
        raise InstallError("missing %s" % TEMPLATE_POLICY.relative_to(ROOT))
    return json.loads(raw.replace("__OWNER__", ctx.owner))["banks"][ctx.bank]


def policy_overlay(ctx):
    bank = render_policy(ctx)
    local = read_json(LOCAL_POLICY, "policies/local/bank-policies.json") or {}
    banks = local.get("banks") or {}
    tracked = (read_json(TRACKED_POLICY, "policies/bank-policies.json") or {}).get("banks") or {}
    if ctx.bank in tracked:
        ctx.warn("the rendered %s REPLACES the tracked demo bank of the same name (the local overlay wins "
                 "wholesale); verify gates 3 and 7 then run against the owner template" % ctx.bank)
    if ctx.bank in banks:
        ctx.note("policies/local/bank-policies.json already declares %s - kept (it may carry your own tag "
                 "vocabulary)" % ctx.bank)
        return
    ctx.act("render policies/templates/personal-bank.json (__OWNER__=%s) into policies/local/bank-policies.json" % ctx.owner)
    if ctx.dry:
        return
    banks[ctx.bank] = bank
    out = dict(local)
    out["banks"] = banks
    try:   # same loader the gateway uses; a bad overlay would be a gateway startup failure
        sys.path.insert(0, str(ROOT))
        from gateway.bank_policies import BankPolicies
    except Exception:  # noqa: BLE001 - gateway package unusable on this python: skip the pre-check
        BankPolicies = None
    if BankPolicies is not None:
        try:
            BankPolicies.from_file(str(TRACKED_POLICY)).with_local(out, source="policies/local/bank-policies.json")
        except ValueError as e:   # BankPolicyError
            raise InstallError("policies/local/bank-policies.json would not load in the gateway: %s" % e)
    # the gateway container reads this file through its bind mount: keep it world-readable like the tracked one
    atomic_write(LOCAL_POLICY, dump_json(out), mode=0o644)


# --- 4 launchd ----------------------------------------------------------------------------------

def launchd_dir(ctx):
    return ctx.home / "Library" / "LaunchAgents"


def render_plist(ctx, template, prefix, out):
    r = run(ctx, [sys.executable, "scripts/render-launchd.py", "--template", str(template),
                  "--set", "PREFIX=" + prefix, "--out", str(out)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    if r.returncode != 0:
        raise InstallError("render failed for %s: %s" % (template.name, r.stdout.strip()))


def loaded(ctx, label):
    """Exit status only: `launchctl print` output carries the session's environment (tokens, #176)."""
    r = run(ctx, ["launchctl", "print", "gui/%d/%s" % (os.getuid(), label)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return r.returncode == 0


def rerank_model(ctx):
    """(model, revision) the sidecar plist pins; the template is the only place the values live."""
    try:
        env = plistlib.loads(RERANK_TEMPLATE.read_bytes())["EnvironmentVariables"]
        return env["RERANK_MODEL"], env["RERANK_MODEL_REVISION"]
    except (OSError, KeyError, ValueError, plistlib.InvalidFileException) as e:
        raise InstallError("cannot read RERANK_MODEL / RERANK_MODEL_REVISION from %s: %s" % (RERANK_TEMPLATE.name, e))


def rerank_preload(ctx):
    """The sidecar runs HF_HUB_OFFLINE=1 and is KeepAlive: without its pinned revision in the cache it fails and
    waits out ThrottleInterval, and gate8 goes RED. So fetch the revision once, online, before the agents load.
    A dry run only says whether it would: it checks the cache offline with `--no-sync` when the env already exists,
    and never downloads or syncs anything."""
    try:
        model, revision = rerank_model(ctx)
    except InstallError as e:
        if not ctx.dry:
            raise
        ctx.failed = True
        ctx.note("FAIL %s (a real run would stop here)" % e)
        return
    what = "rerank model %s @ %s" % (model, revision)
    env = ctx.child_env()
    for name in HF_CACHE_ENV:
        env.pop(name, None)
    env["HOME"] = str(ctx.home)   # the rendered __HOME__: the sidecar's own default cache
    project = ROOT / "rerank-server"
    base = ["uv", "run", "--project", str(project)]
    tail = ["python", str(RERANK_PRELOAD)]
    if ctx.dry:
        if not (project / ".venv" / "bin" / "python").exists():
            ctx.note("would sync rerank-server env and check/download %s@%s (cache not checked: env missing)" % (model, revision))
            return
        try:
            r = run(ctx, base + ["--no-sync"] + tail + ["check", model, revision], env=dict(env, HF_HUB_OFFLINE="1"),
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:   # uv is on PATH (preflight passed) but cannot run: a real run would crash here too
            ctx.failed = True
            ctx.note("FAIL would check/download %s@%s (cache not checked: uv not runnable: %s)" % (model, revision, e))
            return
        if r.returncode == 0:
            ctx.note("%s is cached, skip" % what)
        elif r.returncode == 1:
            ctx.note("would download %s@%s (online, once)" % (model, revision))
        else:
            ctx.note("would check/download %s@%s (cache not checked: check failed (exit %d))" % (model, revision, r.returncode))
        return
    ctx.note("rerank-server env sync + cache check (the first run installs torch etc. and can take a while)")
    r = run(ctx, base + tail + ["check", model, revision], env=dict(env, HF_HUB_OFFLINE="1"),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    if r.returncode == 0:
        ctx.note("%s is cached - skipped" % what)
        return
    if r.returncode != 1:   # 1 is a plain cache miss; anything else means the check itself could not run
        last = next((l for l in reversed(r.stdout.splitlines()) if l.strip()), "")
        ctx.note("check could not run (exit %d): %s" % (r.returncode, last.strip()[:200]))
    ctx.act("downloading %s@%s (~2.3GB, once)" % (model, revision))
    r = run(ctx, base + tail + ["download", model, revision], env=dict(env, HF_HUB_OFFLINE="0"),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    if r.returncode != 0:
        raise InstallError("rerank model download failed (exit %d): %s" % (r.returncode, r.stdout.strip()[-300:]))


def launchd_install(ctx, prefix):
    agents = launchd_dir(ctx)
    if not ctx.dry:
        agents.mkdir(parents=True, exist_ok=True)
        for d in LOG_DIRS:   # launchd does not create the directory of StandardOutPath
            (ctx.home / "Library" / "Logs" / d).mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="hc-install-plist-"))
    try:
        for src, name in JOBS:
            label = "%s.hindsight-%s" % (prefix, name)
            template = ROOT / src / ("__PREFIX__.hindsight-%s.plist.example" % name)
            rendered = tmp / (label + ".plist")
            try:
                render_plist(ctx, template, prefix, rendered)
            except InstallError as e:
                if not ctx.dry:
                    raise
                ctx.failed = True
                ctx.note("FAIL %s (a real run would stop here)" % e)
                continue
            dest = agents / (label + ".plist")
            same = dest.exists() and dest.read_bytes() == rendered.read_bytes()
            if same and loaded(ctx, label):
                ctx.note("%s unchanged and loaded - skipped" % label)
                continue
            ctx.act("%s %s -> %s, launchctl bootstrap gui/%d" % (
                "reload" if same else "install", label, dest, os.getuid()))
            if ctx.dry:
                continue
            atomic_write(dest, rendered.read_text(encoding="utf-8"), mode=0o644)
            run(ctx, ["launchctl", "bootout", "gui/%d/%s" % (os.getuid(), label)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            r = run(ctx, ["launchctl", "bootstrap", "gui/%d" % os.getuid(), str(dest)],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
            if r.returncode != 0:
                raise InstallError("launchctl bootstrap %s failed (exit %d): %s" % (label, r.returncode, r.stdout.strip()[:300]))
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)


# --- 5 bootstrap --------------------------------------------------------------------------------

def container_started_epoch(ctx, container):
    """StartedAt of a container as epoch seconds (UTC), or None."""
    out = probe(ctx, ["docker", "inspect", "-f", "{{.State.StartedAt}}", container])
    try:
        return calendar.timegm(time.strptime(out[:19], "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return None


def restart_gateway(ctx):
    """The gateway reads secrets/acl.json and the bank policies only at startup, and bootstrap's
    `compose up -d --build` leaves an unchanged container alone, so a stack that is already up would keep
    rejecting the new owner token. Decide from state that survives a rerun (a step that failed after the files
    were written must still restart the next time): restart when acl.json or the policy overlay is newer than
    the gateway container's StartedAt."""
    compose = ["docker", "compose", "--env-file", ".env.local"]
    if ctx.dry:
        ctx.act("docker compose --env-file .env.local restart gateway   (only if a running gateway started before "
                "secrets/acl.json or policies/local/bank-policies.json was last written)")
        return
    cid = (probe(ctx, compose + ["ps", "-q", "gateway"]) or "").split("\n")[0].strip()
    if not cid:
        ctx.note("no running gateway: bootstrap starts it with the current acl.json and policy overlay")
        return
    started = container_started_epoch(ctx, cid)
    if started is None:
        ctx.note("could not read the gateway's StartedAt - not restarting")
        return
    newer = [p.name for p in (ROOT / "secrets" / "acl.json", LOCAL_POLICY) if p.exists() and int(p.stat().st_mtime) > started]
    if not newer:
        ctx.note("gateway started after acl.json and the policy overlay were written - no restart")
        return
    ctx.act("docker compose --env-file .env.local restart gateway   (%s newer than the running gateway)" % ", ".join(newer))
    r = run(ctx, compose + ["restart", "gateway"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    if r.returncode != 0:
        raise InstallError("docker compose restart gateway failed (exit %d): %s" % (r.returncode, r.stdout.strip()[:300]))


def run_bootstrap(ctx):
    ctx.act("./bootstrap --mac-mps   (stack up + verify-all; GREEN is the pass condition)")
    if ctx.dry:
        return
    ctx.say("")
    rc = run(ctx, [str(ROOT / "bootstrap"), "--mac-mps"]).returncode
    ctx.say("")
    if rc != 0:
        raise InstallError("./bootstrap exited %d (verify-all not GREEN); the agents and config from steps 2-4 "
                           "are in place, the Claude Code connection (steps 6-7) was skipped. Fix the cause and "
                           "rerun ./install --owner %s" % (rc, ctx.owner))


# --- 6 Claude Code connection -------------------------------------------------------------------

def mcp_url(ctx):
    bind = DEFAULT_BIND
    for line in (read_text(ROOT / ".env.local") or "").splitlines():
        if line.startswith("HC_GATEWAY_BIND=") and line.split("=", 1)[1].strip():
            bind = line.split("=", 1)[1].strip()
    return "http://%s/mcp/%s" % (bind, ctx.bank)


def claude_json_path(ctx):
    return ctx.home / ".claude.json"


def claude_connect(ctx):
    path = claude_json_path(ctx)
    cfg = read_json(path, "~/.claude.json")
    if cfg is not None and not isinstance(cfg, dict):
        raise InstallError("~/.claude.json is not a JSON object; left untouched")
    acl = read_json(ROOT / "secrets" / "acl.json", "secrets/acl.json")
    token = owner_token_of(acl, ctx.owner, ctx.bank)
    url = mcp_url(ctx)
    if token is None:
        if not ctx.dry:
            raise InstallError("no owner token in secrets/acl.json")
        ctx.act("set mcpServers.%s in ~/.claude.json -> %s (Bearer token of %s; value not shown)" % (MCP_NAME, url, ctx.owner))
        return
    entry = {"type": "http", "url": url, "headers": {"Authorization": "Bearer " + token}}
    servers = (cfg or {}).get("mcpServers") or {}
    if servers.get(MCP_NAME) == entry:
        ctx.note("~/.claude.json: mcpServers.%s already -> %s - unchanged" % (MCP_NAME, url))
        return
    was = servers.get(MCP_NAME, {}).get("url") if isinstance(servers.get(MCP_NAME), dict) else None
    ctx.act("%s mcpServers.%s in ~/.claude.json -> %s (Bearer token; value not shown)%s" % (
        "replace" if MCP_NAME in servers else "add", MCP_NAME, url,
        ", backup first" if path.exists() else ", file does not exist yet"))
    if was and was != url:
        ctx.note("the entry being replaced pointed at %s" % was)
    if ctx.dry:
        return
    if path.exists():
        backup_file(path, ctx)
    new = dict(cfg or {})
    servers = dict(servers)
    servers[MCP_NAME] = entry
    new["mcpServers"] = servers
    atomic_write(path, dump_json(new), mode=None)
    ctx.note("restart Claude Code to pick up the server")


# --- 7 CLAUDE.md block --------------------------------------------------------------------------

def claude_md_path(ctx):
    return ctx.home / ".claude" / "CLAUDE.md"


def block_re():
    return re.compile(re.escape(BLOCK_BEGIN) + r".*?" + re.escape(BLOCK_END) + r"\n?", re.DOTALL)


def render_block(ctx):
    body = read_text(SNIPPET)
    if body is None:
        raise InstallError("missing templates/CLAUDE.memory.md")
    # the bank as the gateway enforces it: the local overlay when it declares the bank (it may carry an edited
    # vocabulary), else what the template renders to
    local_bank = ((read_json(LOCAL_POLICY, "policies/local/bank-policies.json") or {}).get("banks") or {}).get(ctx.bank)
    kinds = (local_bank or render_policy(ctx)).get("tag_vocabulary", {}).get("kind", [])
    for k, v in (("__BANK__", ctx.bank), ("__OWNER__", ctx.owner), ("__MCP_NAME__", MCP_NAME),
                 ("__KIND_VALUES__", ", ".join("`%s`" % x for x in kinds))):
        body = body.replace(k, v)
    return "%s\n%s\n%s\n" % (BLOCK_BEGIN, body.strip(), BLOCK_END)


def claude_md_write(ctx):
    path = claude_md_path(ctx)
    block = render_block(ctx)
    text = read_text(path) or ""
    m = block_re().search(text)
    if m:
        new = text[:m.start()] + block + text[m.end():]
    elif not text:
        new = block
    else:
        new = text + ("\n" if text.endswith("\n") else "\n\n") + block
    if new == text:
        ctx.note("%s: block already current - unchanged" % path)
        return
    ctx.act("%s the hindsight-crew block in %s" % ("replace" if m else "append", path))
    if not ctx.dry:
        atomic_write(path, new, mode=None)


def claude_md_remove(ctx):
    path = claude_md_path(ctx)
    text = read_text(path)
    m = block_re().search(text) if text else None
    if not m:
        ctx.note("%s: no hindsight-crew block" % path)
        return
    ctx.act("remove the hindsight-crew block from %s" % path)
    if ctx.dry:
        return
    before, after = text[:m.start()], text[m.end():]
    if before.endswith("\n\n") and not after.strip():
        before = before[:-1]   # the blank line the install put in front of the block
    new = before + after
    real = Path(os.path.realpath(str(path)))
    if not new.strip() and not path.is_symlink():   # a symlinked CLAUDE.md points at a file someone else owns: never delete it
        real.unlink()
        try:
            real.parent.rmdir()   # only succeeds when we created it and nothing else lives there
        except OSError:
            pass
        return
    atomic_write(path, new, mode=None)


# --- uninstall ----------------------------------------------------------------------------------

def mcp_remove(ctx):
    path = claude_json_path(ctx)
    cfg = read_json(path, "~/.claude.json")
    servers = (cfg or {}).get("mcpServers") if isinstance(cfg, dict) else None
    entry = (servers or {}).get(MCP_NAME)
    suffix = "/mcp/" + ctx.bank
    if not isinstance(entry, dict) or not str(entry.get("url", "")).endswith(suffix):
        ctx.note("~/.claude.json: no mcpServers.%s pointing at %s - nothing to remove" % (MCP_NAME, ctx.bank))
        return
    ctx.act("remove mcpServers.%s from ~/.claude.json" % MCP_NAME)
    if ctx.dry:
        return
    backup_file(path, ctx)
    new = dict(cfg)
    servers = dict(servers)
    del servers[MCP_NAME]
    if servers:
        new["mcpServers"] = servers
    else:
        del new["mcpServers"]
    if new:
        atomic_write(path, dump_json(new), mode=None)
    else:
        Path(os.path.realpath(str(path))).unlink()


def launchd_remove(ctx, prefix):
    for _src, name in JOBS:
        label = "%s.hindsight-%s" % (prefix, name)
        dest = launchd_dir(ctx) / (label + ".plist")
        if not dest.exists() and not loaded(ctx, label):
            ctx.note("%s not installed" % label)
            continue
        ctx.act("launchctl bootout gui/%d/%s, remove %s" % (os.getuid(), label, dest))
        if ctx.dry:
            continue
        run(ctx, ["launchctl", "bootout", "gui/%d/%s" % (os.getuid(), label)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if dest.exists():
            dest.unlink()


def purge(ctx):
    if not (ROOT / ".env.local").exists():
        ctx.warn("--purge: no .env.local, so `docker compose down -v` cannot be interpolated; volumes untouched")
        return
    ctx.act("docker compose --env-file .env.local down -v   (deletes the data volumes)")
    if ctx.dry:
        return
    r = run(ctx, ["docker", "compose", "--env-file", ".env.local", "down", "-v"])
    if r.returncode != 0:
        raise InstallError("docker compose down -v failed (exit %d)" % r.returncode)


def uninstall(ctx):
    have = hc_host_env.load(ROOT)
    owner = ctx.owner or have.get("HC_OWNER")
    if not owner:
        raise InstallError("owner unknown: pass --owner <slug> (no HC_OWNER in .env.host.local)")
    if not SLUG_RE.match(owner):
        raise InstallError("invalid owner slug %r" % owner)
    ctx.owner = owner
    prefix = ctx.args.launchd_prefix or have.get("HC_LAUNCHD_PREFIX") or DEFAULT_PREFIX
    if not PREFIX_RE.match(prefix):
        raise InstallError("invalid launchd prefix %r" % prefix)
    total = 4 if ctx.args.purge else 3
    ctx.say("hindsight-crew uninstall: owner=%s prefix=%s%s" % (owner, prefix, "  [DRY RUN - nothing is changed]" if ctx.dry else ""))
    ctx.step(1, total, "launchd agents")
    launchd_remove(ctx, prefix)
    ctx.step(2, total, "Claude Code connection")
    mcp_remove(ctx)
    ctx.step(3, total, "CLAUDE.md block")
    claude_md_remove(ctx)
    if ctx.args.purge:
        ctx.step(4, total, "data volumes (--purge)")
        purge(ctx)
    ctx.say("")
    ctx.say("kept: repo-side config (.env.local, secrets/, .env.host.local, policies/local/) and the ~/.claude.json backups.")
    if not ctx.args.purge:
        ctx.say("kept: the docker stack and its data volumes (--purge deletes the volumes).")


# --- install ------------------------------------------------------------------------------------

def install(ctx):
    if not ctx.owner:
        raise InstallError("--owner <slug> is required")
    if not SLUG_RE.match(ctx.owner):
        raise InstallError("invalid owner slug %r: lowercase letters, digits and '-', starting with a letter, "
                           "at most 32 characters (it becomes the bank personal-<slug> and a tag slug)" % ctx.owner)
    ctx.say("hindsight-crew install: owner=%s bank=%s profile=%s%s" % (
        ctx.owner, ctx.bank, PROFILE, "  [DRY RUN - nothing is changed]" if ctx.dry else ""))
    total = 7 if ctx.args.write_claude_md else 6
    ctx.step(1, total, "preflight")
    preflight(ctx)
    host_env_resolve(ctx)   # a conflicting owner/prefix must refuse before any file is written
    ctx.step(2, total, ".env.local and LLM")
    env_local(ctx)
    ctx.step(3, total, "owner identity, host settings, policy overlay")
    policy_overlay(ctx)     # validates the overlay first: a bad file must not leave an ACL token behind
    owner_acl(ctx)
    prefix = host_env(ctx)
    ctx.step(4, total, "launchd agents (prefix %s)" % prefix)
    rerank_preload(ctx)
    launchd_install(ctx, prefix)
    ctx.step(5, total, "bootstrap + verify-all")
    restart_gateway(ctx)
    run_bootstrap(ctx)
    ctx.step(6, total, "Claude Code connection")
    claude_connect(ctx)
    if ctx.args.write_claude_md:
        ctx.step(7, total, "CLAUDE.md block")
        claude_md_write(ctx)
    else:
        ctx.say("")
        ctx.note("memory-usage snippet: templates/CLAUDE.memory.md (add --write-claude-md to put it in ~/.claude/CLAUDE.md)")
    ctx.say("")
    if ctx.dry:
        ctx.say("dry run done: nothing was changed%s." % (" (a real run would stop at preflight)" if ctx.failed else ""))
    else:
        ctx.say("install done: bank %s is served at %s" % (ctx.bank, mcp_url(ctx)))
    if ctx.warnings:
        ctx.say("warnings (%d):" % len(ctx.warnings))
        for w in ctx.warnings:
            ctx.say("  - " + w)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="install", description="Install Hindsight-Crew for one owner on this Mac.")
    ap.add_argument("--owner", metavar="SLUG", help="owner slug: bank personal-<slug>, ACL identity <slug>")
    ap.add_argument("--claude-token-env", metavar="VAR", help="name of the environment variable holding the claude "
                    "setup-token (the value is never taken from the command line)")
    ap.add_argument("--write-claude-md", action="store_true", help="put templates/CLAUDE.memory.md into ~/.claude/CLAUDE.md")
    ap.add_argument("--launchd-prefix", metavar="PREFIX", help="LaunchAgent label prefix (default %s)" % DEFAULT_PREFIX)
    ap.add_argument("--uninstall", action="store_true", help="remove agents, MCP entry and CLAUDE.md block")
    ap.add_argument("--purge", action="store_true", help="with --uninstall: also delete the docker data volumes")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    args = ap.parse_args(argv)
    if args.purge and not args.uninstall:
        ap.error("--purge only applies to --uninstall")
    if args.uninstall and (args.claude_token_env or args.write_claude_md):
        ap.error("--claude-token-env and --write-claude-md only apply to an install")
    try:
        ctx = Ctx(args)
        (uninstall if args.uninstall else install)(ctx)
    except InstallError as e:
        print("install: ERROR %s" % e, file=sys.stderr)
        return 1
    except (hc_host_env.ConfigMissing, OSError, ValueError, TypeError, AttributeError, KeyError) as e:
        # a config file with an unexpected shape or encoding: say which kind of error, not a traceback
        print("install: ERROR unexpected %s: %s (a config file is malformed or unreadable)" % (type(e).__name__, e),
              file=sys.stderr)
        return 1
    return 1 if ctx.failed else 0


if __name__ == "__main__":
    sys.exit(main())
