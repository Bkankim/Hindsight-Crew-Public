"""Host install settings from `.env.host.local` (issue #186).

Hermetic: every script runs from a scratch copy of the files it needs (so the repo root, and the
`.env.host.local` it reads, is under tmp_path), HOME is a tmp directory (the real `~/.env` is never
read), curl and python are stubs, and every credential here is a short fake string.
"""
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
BASH = "/bin/bash"

COPIED = ("model-refresh.sh", "model-audit.sh", "scheduled-checks.sh", "hc-python.sh", "hc-host-env.sh",
          "hc_host_env.py", "model_audit.py", "audit_log_reader.py", "render-launchd.py")


@pytest.fixture(autouse=True)
def _private_script_modules():
    """tag_retag_plan.py does `import tag_hygiene`, and tag_hygiene computes DEFAULT_BANK at import. Tests here
    import them with HC_PERSONAL_BANK deleted, so the cached copies must not leak into other test modules."""
    names = ("tag_hygiene", "tag_retag_plan", "hc_host_env")
    saved = {n: sys.modules.pop(n) for n in names if n in sys.modules}
    yield
    for n in names:
        sys.modules.pop(n, None)
    sys.modules.update(saved)


def load_hc_host_env():
    spec = importlib.util.spec_from_file_location("hc_host_env_under_test", SCRIPTS / "hc_host_env.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_tree(tmp_path, host_file=None, acl=None) -> Path:
    """A scratch repo root with the host scripts, an acl.json and (optionally) `.env.host.local`."""
    root = tmp_path / "tree"
    (root / "scripts").mkdir(parents=True)
    for name in COPIED:
        shutil.copy(SCRIPTS / name, root / "scripts" / name)
    shutil.copytree(REPO / "scripts" / "launchd", root / "scripts" / "launchd")
    (root / "secrets").mkdir()
    (root / "secrets" / "acl.json").write_text(json.dumps(acl or {"tokens": {
        "tok-demo": {"identity": "demo"}, "tok-carol": {"identity": "carol"}}}))
    if host_file is not None:
        (root / ".env.host.local").write_text(host_file, encoding="utf-8")
    return root


def home_dir(tmp_path, dotenv=None) -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    if dotenv is not None:
        (home / ".env").write_text(dotenv, encoding="utf-8")
    return home


def run_script(root, name, *args, env=None, home=None):
    base = {"PATH": "/usr/bin:/bin", "HOME": str(home or root.parent / "home"), "HC_PYTHON": sys.executable}
    base.update(env or {})
    return subprocess.run([BASH, str(root / "scripts" / name), *args], cwd=root, env=base,
                          capture_output=True, text=True, timeout=60)


# ------------------------------------------------------------------ the file format (python and shell agree)

FORMAT_TEXT = "\n".join([
    "# comment",
    "",
    "HC_PERSONAL_BANK=bank-a",
    "  HC_OWNER = carol  ",
    'HC_NOTIFY_DISCORD_CHANNEL_ID="12345"',
    "export HC_LAUNCHD_PREFIX='org.example'",
    "HC_EMPTY=",
    "NOT_HC_KEY=ignored",
    "hc_lower=ignored",
    "HC_=ignored",
    "HC_BAD-KEY=ignored",
    "no equals sign",
    'HC_QUOTE_INSIDE="a\'b"',
    "HC_ONE_CHAR_QUOTE=\"",
    "HC_NO_NEWLINE_AT_END=last",
])
FORMAT_EXPECTED = {
    "HC_PERSONAL_BANK": "bank-a", "HC_OWNER": "carol", "HC_NOTIFY_DISCORD_CHANNEL_ID": "12345",
    "HC_LAUNCHD_PREFIX": "org.example", "HC_EMPTY": "", "HC_QUOTE_INSIDE": "a'b",
    "HC_ONE_CHAR_QUOTE": '"', "HC_NO_NEWLINE_AT_END": "last",
}


def shell_load(root, env=None):
    """Keys of FORMAT_EXPECTED plus the ignored ones, as hc_load_host_env leaves them (None = unset)."""
    keys = sorted(set(FORMAT_EXPECTED) | {"NOT_HC_KEY", "hc_lower", "HC_", "HC_BAD-KEY"})
    snippet = '. "%s/scripts/hc-host-env.sh"; hc_load_host_env "%s"; ' % (root, root) + "; ".join(
        'if [ -n "${%s+x}" ]; then printf "%%s=%%s\\n" "%s" "${%s}"; fi' % (k, k, k)
        for k in keys if k.replace("_", "").isalnum())
    r = subprocess.run([BASH, "-c", snippet], env={"PATH": "/usr/bin:/bin", **(env or {})},
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return dict(line.split("=", 1) for line in r.stdout.splitlines())


def test_python_parser_reads_only_hc_keys():
    mod = load_hc_host_env()
    parsed = mod.parse(FORMAT_TEXT)
    assert parsed == FORMAT_EXPECTED


def test_shell_loader_reads_the_same_keys_as_the_python_parser(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    (root / ".env.host.local").write_text(FORMAT_TEXT, encoding="utf-8")
    shutil.copy(SCRIPTS / "hc-host-env.sh", root / "hc-host-env.sh")
    (root / "scripts").mkdir()
    shutil.copy(SCRIPTS / "hc-host-env.sh", root / "scripts" / "hc-host-env.sh")
    assert shell_load(root) == FORMAT_EXPECTED


DUPLICATE_CASES = [
    ("HC_PERSONAL_BANK=a\nHC_PERSONAL_BANK=b\n", "a"),
    ("HC_PERSONAL_BANK=\nHC_PERSONAL_BANK=b\n", "b"),
    ("HC_PERSONAL_BANK=a\nHC_PERSONAL_BANK=\n", "a"),
]


@pytest.mark.parametrize("text, expected", DUPLICATE_CASES, ids=["first-wins", "empty-first-then-value", "value-then-empty"])
@pytest.mark.parametrize("side", ["python", "shell"])
def test_a_repeated_key_resolves_the_same_in_python_and_shell(tmp_path, text, expected, side):
    root = tmp_path / "r"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPTS / "hc-host-env.sh", root / "scripts" / "hc-host-env.sh")
    (root / ".env.host.local").write_text(text, encoding="utf-8")
    if side == "python":
        assert load_hc_host_env().parse(text)["HC_PERSONAL_BANK"] == expected
    else:
        assert shell_load(root)["HC_PERSONAL_BANK"] == expected


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh not installed")
def test_shell_loader_works_when_sourced_from_zsh(tmp_path):
    """The install loops in the READMEs (scripts/launchd/README.md and friends) source the loader from the operator's zsh."""
    root = tmp_path / "r"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPTS / "hc-host-env.sh", root / "scripts" / "hc-host-env.sh")
    (root / ".env.host.local").write_text(FORMAT_TEXT, encoding="utf-8")
    snippet = '. "%s/scripts/hc-host-env.sh"; hc_load_host_env "%s"; printf "%%s|%%s" "$HC_LAUNCHD_PREFIX" "$HC_PERSONAL_BANK"' % (root, root)
    r = subprocess.run([shutil.which("zsh"), "-c", snippet], env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout == "org.example|bank-a", r.stderr
    r = subprocess.run([shutil.which("zsh"), "-c", snippet], env={"PATH": "/usr/bin:/bin", "HC_PERSONAL_BANK": "env-bank"},
                       capture_output=True, text=True)
    assert r.stdout == "org.example|env-bank", r.stderr      # a non-empty environment value still wins


@pytest.mark.parametrize("side", ["python", "shell"])
def test_a_non_empty_environment_value_wins_over_the_file_and_an_empty_one_does_not(tmp_path, monkeypatch, side):
    root = tmp_path / "scripts-root"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPTS / "hc-host-env.sh", root / "scripts" / "hc-host-env.sh")
    (root / ".env.host.local").write_text("HC_PERSONAL_BANK=from-file\nHC_OWNER=owner-file\n", encoding="utf-8")
    if side == "python":
        mod = load_hc_host_env()
        monkeypatch.setenv("HC_PERSONAL_BANK", "from-env")
        monkeypatch.setenv("HC_OWNER", "")
        assert mod.get("HC_PERSONAL_BANK", root) == "from-env"
        assert mod.get("HC_OWNER", root) == "owner-file"
    else:
        got = shell_load(root, env={"HC_PERSONAL_BANK": "from-env", "HC_OWNER": ""})
        assert got["HC_PERSONAL_BANK"] == "from-env" and got["HC_OWNER"] == "owner-file"


@pytest.mark.parametrize("file_text", [None, "", "# only a comment\n", "HC_OWNER=carol\n", "HC_PERSONAL_BANK=\n"])
def test_require_names_the_first_missing_key_and_never_defaults(tmp_path, monkeypatch, file_text):
    mod = load_hc_host_env()
    monkeypatch.delenv("HC_PERSONAL_BANK", raising=False)
    monkeypatch.delenv("HC_OWNER", raising=False)
    root = tmp_path / "r"
    root.mkdir()
    if file_text is not None:
        (root / ".env.host.local").write_text(file_text, encoding="utf-8")
    with pytest.raises(mod.ConfigMissing) as err:
        mod.require("HC_PERSONAL_BANK", "HC_OWNER", root=root)
    assert err.value.key == "HC_PERSONAL_BANK" and str(err.value) == "config_missing HC_PERSONAL_BANK"


# ------------------------------------------------------------------ config_missing: shell wrappers

WRAPPER_FAIL = [
    ("model-refresh.sh", "MODEL_REFRESH_FAIL - config_missing"),
    ("model-audit.sh", "MODEL_AUDIT_FAIL - config_missing"),
]


@pytest.mark.parametrize("script, token", WRAPPER_FAIL, ids=[w[0] for w in WRAPPER_FAIL])
@pytest.mark.parametrize("host_file, missing", [
    (None, "HC_PERSONAL_BANK"),
    ("", "HC_PERSONAL_BANK"),
    ("HC_OWNER=demo\n", "HC_PERSONAL_BANK"),
    ("HC_PERSONAL_BANK=bank-x\n", "HC_OWNER"),
    ("HC_PERSONAL_BANK=bank-x\nHC_OWNER=\n", "HC_OWNER"),
], ids=["no-file", "empty-file", "owner-only", "bank-only", "empty-owner"])
def test_wrapper_fails_with_config_missing_and_no_demo_default(tmp_path, script, token, host_file, missing):
    root = make_tree(tmp_path, host_file)
    home_dir(tmp_path)
    r = run_script(root, script)
    assert r.returncode == 1
    assert f"{token} {missing}" in r.stdout
    assert "personal-demo" not in r.stdout + r.stderr


def test_model_audit_py_run_directly_fails_with_config_missing(tmp_path):
    root = make_tree(tmp_path)
    home_dir(tmp_path)
    for host_file, missing in ((None, "HC_PERSONAL_BANK"), ("HC_PERSONAL_BANK=bank-x\n", "HC_OWNER")):
        if host_file is not None:
            (root / ".env.host.local").write_text(host_file, encoding="utf-8")
        r = subprocess.run([sys.executable, str(root / "scripts" / "model_audit.py")], cwd=root, capture_output=True,
                           text=True, env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home")}, timeout=60)
        assert r.returncode == 1
        assert f"MODEL_AUDIT_FAIL - config_missing {missing}" in r.stderr


# ------------------------------------------------------------------ bank and token selected from the setting

# argv goes to curl.args, stdin (where `-H @-` delivers the Authorization header, issue #214) to curl.args.stdin
CURL_STUB = '#!/bin/sh\necho "$@" >> "%s"\n[ -t 0 ] || cat >> "%s.stdin"\nprintf \'{"items": []}\\n200\'\n'


def curl_stub(tmp_path) -> Path:
    d = tmp_path / "stubbin"
    d.mkdir()
    log = tmp_path / "curl.args"
    curl = d / "curl"
    curl.write_text(CURL_STUB % (log, log))
    curl.chmod(0o755)
    return d


@pytest.mark.parametrize("source", ["env", "host-file"])
@pytest.mark.parametrize("bank, owner, token", [
    ("personal-demo", "demo", "Bearer tok-demo"),
    ("bank-x", "carol", "Bearer tok-carol"),
])
def test_model_refresh_uses_the_configured_bank_and_the_owners_token(tmp_path, source, bank, owner, token):
    root = make_tree(tmp_path, f"HC_PERSONAL_BANK={bank}\nHC_OWNER={owner}\n" if source == "host-file" else None)
    home_dir(tmp_path)
    stubs = curl_stub(tmp_path)
    env = {"PATH": f"{stubs}:/usr/bin:/bin"}
    if source == "env":
        env.update(HC_PERSONAL_BANK=bank, HC_OWNER=owner)
    r = run_script(root, "model-refresh.sh", env=env)
    assert "MODEL_REFRESH_FAIL - no_target_models" in r.stdout, r.stdout + r.stderr   # got past config and token
    calls = (tmp_path / "curl.args").read_text()
    sent = (tmp_path / "curl.args.stdin").read_text()
    assert f"/v1/default/banks/{bank}/mental-models" in calls
    assert f"Authorization: {token}" in sent
    other = "tok-carol" if owner == "demo" else "tok-demo"
    assert other not in calls and other not in sent
    assert token.split()[1] not in calls            # the token itself never reaches curl's argv


def test_model_refresh_with_an_owner_missing_from_acl_is_token_missing_not_a_fallback(tmp_path):
    root = make_tree(tmp_path, "HC_PERSONAL_BANK=bank-x\nHC_OWNER=nobody\n")
    home_dir(tmp_path)
    r = run_script(root, "model-refresh.sh", env={"PATH": f"{curl_stub(tmp_path)}:/usr/bin:/bin"})
    assert r.returncode == 1 and "MODEL_REFRESH_FAIL - token_missing" in r.stdout
    assert not (tmp_path / "curl.args").exists()


def load_model_audit(root, monkeypatch, **env):
    for key in ("HC_PERSONAL_BANK", "HC_OWNER"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.syspath_prepend(str(root / "scripts"))
    spec = importlib.util.spec_from_file_location("model_audit_host_env_test", root / "scripts" / "model_audit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("owner, expected", [("demo", "tok-demo"), ("carol", "tok-carol")])
def test_model_audit_read_token_follows_hc_owner(tmp_path, monkeypatch, owner, expected):
    root = make_tree(tmp_path)
    module = load_model_audit(root, monkeypatch, HC_PERSONAL_BANK="bank-x", HC_OWNER=owner)
    monkeypatch.setattr(module, "ROOT", str(root))
    assert module.BANK == "bank-x"
    assert module.read_token() == expected


def test_model_audit_read_token_for_an_unknown_owner_raises(tmp_path, monkeypatch):
    root = make_tree(tmp_path)
    module = load_model_audit(root, monkeypatch, HC_PERSONAL_BANK="bank-x", HC_OWNER="nobody")
    monkeypatch.setattr(module, "ROOT", str(root))
    with pytest.raises(RuntimeError, match="no nobody token"):
        module.read_token()


# ------------------------------------------------------------------ the bank-bound python tools

def load_script(name, mod_name):
    spec = importlib.util.spec_from_file_location(mod_name, SCRIPTS / name)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(SCRIPTS))
    return module


def isolate_from_host_file(monkeypatch, tmp_path):
    """The scripts also read the repo's own `.env.host.local`; point them at an empty root."""
    empty = tmp_path / "emptyroot"
    empty.mkdir(exist_ok=True)
    monkeypatch.setattr(load_hc_host_env_module(), "ROOT", empty, raising=False)


def load_hc_host_env_module():
    sys.path.insert(0, str(SCRIPTS))
    try:
        import hc_host_env
    finally:
        sys.path.remove(str(SCRIPTS))
    return hc_host_env


@pytest.mark.parametrize("argv", [["export"], ["report", "docs.jsonl"]], ids=["export", "report"])
def test_tag_hygiene_without_a_bank_fails_with_config_missing(tmp_path, monkeypatch, capsys, argv):
    monkeypatch.delenv("HC_PERSONAL_BANK", raising=False)
    isolate_from_host_file(monkeypatch, tmp_path)
    module = load_script("tag_hygiene.py", "tag_hygiene_cfg_test")
    assert module.DEFAULT_BANK is None
    assert module.main(argv) == 2
    assert "TAG_HYGIENE_FAIL - config_missing HC_PERSONAL_BANK" in capsys.readouterr().err


def test_tag_hygiene_default_bank_follows_the_setting_and_the_flag_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("HC_PERSONAL_BANK", "bank-x")
    module = load_script("tag_hygiene.py", "tag_hygiene_cfg_test2")
    assert module.DEFAULT_BANK == "bank-x"
    assert module.build_parser().parse_args(["export"]).bank == "bank-x"
    assert module.build_parser().parse_args(["export", "--bank", "bank-y"]).bank == "bank-y"


def test_tag_migrate_without_a_bank_fails_with_config_missing(monkeypatch):
    monkeypatch.delenv("HC_PERSONAL_BANK", raising=False)
    module = load_script("tag_migrate.py", "tag_migrate_cfg_test")
    with pytest.raises(module.MigrationError, match="config_missing HC_PERSONAL_BANK"):
        module.main(["--dry-run"])


@pytest.mark.parametrize("injected, env_bank", [("bank-x", None), (None, "bank-y")], ids=["injected", "env"])
def test_tag_migrate_bank_comes_from_the_injection_or_the_environment(monkeypatch, injected, env_bank):
    monkeypatch.delenv("HC_PERSONAL_BANK", raising=False)
    if env_bank:
        monkeypatch.setenv("HC_PERSONAL_BANK", env_bank)
    spec = importlib.util.spec_from_file_location("tag_migrate_cfg_test2", SCRIPTS / "tag_migrate.py")
    module = importlib.util.module_from_spec(spec)
    if injected:
        module.__dict__["PERSONAL_BANK"] = injected         # what the wrapper prelude does
    spec.loader.exec_module(module)
    bank = injected or env_bank
    assert module.BANK_NAME == bank and module.BANK_PATH == "/v1/default/banks/" + bank
    assert module.manifest_for([], set())["bank"] == bank


def test_duplicate_recall_primary_comes_from_the_settings(tmp_path, monkeypatch, capsys):
    module = load_script("duplicate_recall.py", "duplicate_recall_cfg_test")
    ledger, audit = tmp_path / "ledger.log", tmp_path / "gateway.log"
    ledger.write_text("")
    audit.write_text("")
    argv = ["--ledger", str(ledger), "--audit", str(audit)]
    monkeypatch.setenv("HC_PERSONAL_BANK", "bank-x")
    monkeypatch.setenv("HC_OWNER", "carol")
    assert module.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["primary"] == "carol/bank-x"
    assert module.main(argv + ["--primary", "alice/personal-alice"]) == 0
    assert json.loads(capsys.readouterr().out)["primary"] == "alice/personal-alice"


@pytest.mark.parametrize("missing", ["HC_PERSONAL_BANK", "HC_OWNER"])
def test_duplicate_recall_without_settings_fails_with_config_missing(tmp_path, monkeypatch, capsys, missing):
    isolate_from_host_file(monkeypatch, tmp_path)
    module = load_script("duplicate_recall.py", "duplicate_recall_cfg_test2")
    ledger = tmp_path / "ledger.log"
    ledger.write_text("")
    monkeypatch.setenv("HC_PERSONAL_BANK", "bank-x")
    monkeypatch.setenv("HC_OWNER", "carol")
    monkeypatch.delenv(missing)
    assert module.main(["--ledger", str(ledger), "--audit", str(ledger)]) == 2
    assert f"DUPLICATE_RECALL_FAIL - config_missing {missing}" in capsys.readouterr().err


# ------------------------------------------------------------------ launchd prefix

def render(root, out, *args, env=None):
    base = {"PATH": "/usr/bin:/bin", "HOME": "/home/fake"}
    base.update(env or {})
    template = root / "scripts" / "launchd" / "__PREFIX__.hindsight-model-audit.plist.example"
    return subprocess.run([sys.executable, str(root / "scripts" / "render-launchd.py"), "--template", str(template),
                           "--out", str(out), *args], env=base, capture_output=True, text=True, cwd=root)


@pytest.mark.parametrize("host_file, env, args, expected", [
    ("HC_LAUNCHD_PREFIX=org.example\n", {}, [], "org.example"),
    (None, {"HC_LAUNCHD_PREFIX": "org.env"}, [], "org.env"),
    ("HC_LAUNCHD_PREFIX=org.example\n", {"HC_LAUNCHD_PREFIX": "org.env"}, [], "org.env"),
    ("HC_LAUNCHD_PREFIX=org.example\n", {"HC_LAUNCHD_PREFIX": "org.env"}, ["--set", "PREFIX=org.flag"], "org.flag"),
    (None, {}, ["--set", "PREFIX=org.flag"], "org.flag"),
], ids=["host-file", "env", "env-over-file", "flag-over-all", "flag-only"])
def test_label_prefix_precedence_flag_then_env_then_host_file(tmp_path, host_file, env, args, expected):
    import plistlib
    root = make_tree(tmp_path, host_file)
    out = tmp_path / "o.plist"
    r = render(root, out, "--set", "REPO=/r", *args, env=env)
    assert r.returncode == 0, r.stderr
    assert plistlib.loads(out.read_bytes())["Label"] == expected + ".hindsight-model-audit"


@pytest.mark.parametrize("host_file", [None, "", "HC_LAUNCHD_PREFIX=\n", "HC_OWNER=demo\n"])
def test_missing_prefix_fails_the_render_and_keeps_the_existing_plist(tmp_path, host_file):
    root = make_tree(tmp_path, host_file)
    out = tmp_path / "o.plist"
    out.write_text("keep me")
    r = render(root, out, "--set", "REPO=/r")
    assert r.returncode == 1
    assert "config_missing HC_LAUNCHD_PREFIX" in r.stderr
    assert out.read_text() == "keep me"


ALL_TEMPLATES = sorted((REPO / "rerank-server" / "launchd").glob("*.plist.example")) + sorted(
    (REPO / "scripts" / "launchd").glob("*.plist.example"))


DEMO_PREFIX = "org.example.ops"


@pytest.mark.parametrize("template", ALL_TEMPLATES, ids=[t.name for t in ALL_TEMPLATES])
def test_a_prefix_renders_exactly_the_literal_labels(tmp_path, template):
    """With HC_LAUNCHD_PREFIX=<p> the only change versus a template with literal <p>.* labels is the token.
    The prefix is a synthetic one: the operator's real prefix comes from .env.host.local, never from the repo."""
    out = tmp_path / "o.plist"
    r = subprocess.run([sys.executable, str(SCRIPTS / "render-launchd.py"), "--template", str(template), "--out", str(out),
                        "--set", "PREFIX=" + DEMO_PREFIX, "--set", "REPO=/r", "--set", "UV=/opt/homebrew/bin/uv"],
                       env={"PATH": "/usr/bin:/bin", "HOME": "/h"}, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    expected = (template.read_text(encoding="utf-8").replace("__PREFIX__", DEMO_PREFIX).replace("__REPO__", "/r")
                .replace("__HOME__", "/h").replace("__UV__", "/opt/homebrew/bin/uv").replace(
                    "__PATH__", "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"))
    assert out.read_text(encoding="utf-8") == expected
    assert template.name.startswith("__PREFIX__.hindsight-") and DEMO_PREFIX not in template.read_text(encoding="utf-8")


# ------------------------------------------------------------------ notification target order

BOT, CHAN = "fake-bot-1", "111"
LEGACY_BOT, LEGACY_CHAN = "fake-legacy-bot", "222"
FILE_BOT, FILE_CHAN = "fake-file-bot", "333"
LEGACY_ENV = f'GGG_DISCORD_BOT_TOKEN="{LEGACY_BOT}"\nGGG_DISCORD_CHANNEL_ID={LEGACY_CHAN}\nOTHER=$(not shell clean\n'

# (env, host file, ~/.env, expected (bot, channel) after hc_resolve_notify; None = both unset)
NOTIFY_CASES = [
    pytest.param({"HC_NOTIFY_DISCORD_BOT_TOKEN": BOT, "HC_NOTIFY_DISCORD_CHANNEL_ID": CHAN}, None, LEGACY_ENV,
                 (BOT, CHAN), id="env-pair-beats-legacy"),
    pytest.param({}, f"HC_NOTIFY_DISCORD_BOT_TOKEN={FILE_BOT}\nHC_NOTIFY_DISCORD_CHANNEL_ID={FILE_CHAN}\n", LEGACY_ENV,
                 (FILE_BOT, FILE_CHAN), id="host-file-pair-beats-legacy"),
    pytest.param({"HC_NOTIFY_DISCORD_BOT_TOKEN": BOT}, f"HC_NOTIFY_DISCORD_CHANNEL_ID={FILE_CHAN}\n", None,
                 (BOT, FILE_CHAN), id="pair-may-span-env-and-file"),
    pytest.param({}, None, LEGACY_ENV, (LEGACY_BOT, LEGACY_CHAN), id="legacy-only"),
    pytest.param({"HC_NOTIFY_DISCORD_BOT_TOKEN": BOT}, None, LEGACY_ENV, (LEGACY_BOT, LEGACY_CHAN),
                 id="half-a-pair-falls-back-to-the-whole-legacy-pair"),
    pytest.param({}, "HC_NOTIFY_DISCORD_CHANNEL_ID=\n", LEGACY_ENV, (LEGACY_BOT, LEGACY_CHAN), id="empty-value-is-unset"),
    pytest.param({}, None, 'GGG_DISCORD_BOT_TOKEN="only-a-bot"\n', None, id="legacy-half-pair-is-unusable"),
    pytest.param({}, None, None, None, id="nothing"),
    pytest.param({"HC_NOTIFY_DISCORD_BOT_TOKEN": BOT}, None, None, None, id="half-pair-and-no-legacy"),
]


def resolve_notify(tmp_path, env, host_file, dotenv):
    root = make_tree(tmp_path, host_file)
    home = home_dir(tmp_path, dotenv)
    snippet = ('. "{r}/scripts/hc-host-env.sh"; hc_load_host_env "{r}"; hc_resolve_notify; '
               'printf "%s|%s" "${{HC_NOTIFY_DISCORD_BOT_TOKEN-}}" "${{HC_NOTIFY_DISCORD_CHANNEL_ID-}}"').format(r=root)
    r = subprocess.run([BASH, "-c", snippet], env={"PATH": "/usr/bin:/bin", "HOME": str(home), **env},
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


@pytest.mark.parametrize("env, host_file, dotenv, expected", NOTIFY_CASES)
def test_notify_target_order_env_or_host_file_then_legacy_then_nothing(tmp_path, env, host_file, dotenv, expected):
    got = resolve_notify(tmp_path, env, host_file, dotenv)
    if expected is None:
        bot, chan = got.split("|")
        assert not (bot and chan)          # no usable pair
    else:
        assert got == "|".join(expected)


@pytest.mark.parametrize("wrapper", ["model-audit.sh", "scheduled-checks.sh"])
@pytest.mark.parametrize("env, host_file, dotenv, expected", NOTIFY_CASES)
def test_wrappers_hand_the_resolved_target_to_python(tmp_path, wrapper, env, host_file, dotenv, expected):
    """The python stub prints what the real script would see: HC_NOTIFY_* for model_audit.py, and for
    scheduled_checks.py (which reads the legacy names) GGG_*."""
    root = make_tree(tmp_path, "HC_PERSONAL_BANK=bank-x\nHC_OWNER=demo\n" + (host_file or ""))
    home = home_dir(tmp_path, dotenv)
    stub = tmp_path / "stubpython"
    stub.write_text('#!/bin/sh\nprintf "%s|%s|%s|%s" "${HC_NOTIFY_DISCORD_BOT_TOKEN-}" "${HC_NOTIFY_DISCORD_CHANNEL_ID-}" '
                    '"${GGG_DISCORD_BOT_TOKEN-}" "${GGG_DISCORD_CHANNEL_ID-}"\n')
    stub.chmod(0o755)
    r = run_script(root, wrapper, env={"HC_PYTHON": str(stub), **env}, home=home)
    assert r.returncode == 0, r.stdout + r.stderr
    hc_bot, hc_chan, ggg_bot, ggg_chan = r.stdout.split("|")
    if expected is None:
        assert not (hc_bot and hc_chan)
        assert not (ggg_bot and ggg_chan) if wrapper == "scheduled-checks.sh" else True
    else:
        assert (hc_bot, hc_chan) == expected
        if wrapper == "scheduled-checks.sh":
            assert (ggg_bot, ggg_chan) == expected


@pytest.mark.parametrize("env, expected", [
    ({"HC_NOTIFY_DISCORD_BOT_TOKEN": BOT, "HC_NOTIFY_DISCORD_CHANNEL_ID": CHAN,
      "GGG_DISCORD_BOT_TOKEN": LEGACY_BOT, "GGG_DISCORD_CHANNEL_ID": LEGACY_CHAN}, (BOT, CHAN)),
    ({"GGG_DISCORD_BOT_TOKEN": LEGACY_BOT, "GGG_DISCORD_CHANNEL_ID": LEGACY_CHAN}, (LEGACY_BOT, LEGACY_CHAN)),
    ({"HC_NOTIFY_DISCORD_BOT_TOKEN": BOT, "GGG_DISCORD_BOT_TOKEN": LEGACY_BOT, "GGG_DISCORD_CHANNEL_ID": LEGACY_CHAN},
     (LEGACY_BOT, LEGACY_CHAN)),
    ({"HC_NOTIFY_DISCORD_BOT_TOKEN": BOT}, (None, None)),
    ({}, (None, None)),
], ids=["hc-pair", "legacy-pair", "half-hc-pair-uses-legacy", "half-pair-only", "nothing"])
def test_model_audit_notify_target_python_side(tmp_path, monkeypatch, env, expected):
    isolate_from_host_file(monkeypatch, tmp_path)
    for key in ("HC_NOTIFY_DISCORD_BOT_TOKEN", "HC_NOTIFY_DISCORD_CHANNEL_ID", "GGG_DISCORD_BOT_TOKEN",
                "GGG_DISCORD_CHANNEL_ID"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    module = load_script("model_audit.py", "model_audit_notify_test")
    assert module.notify_target() == expected


def test_model_audit_without_any_target_logs_notify_failed_missing_env(tmp_path, monkeypatch, capsys):
    isolate_from_host_file(monkeypatch, tmp_path)
    for key in ("HC_NOTIFY_DISCORD_BOT_TOKEN", "HC_NOTIFY_DISCORD_CHANNEL_ID", "GGG_DISCORD_BOT_TOKEN",
                "GGG_DISCORD_CHANNEL_ID"):
        monkeypatch.delenv(key, raising=False)
    module = load_script("model_audit.py", "model_audit_notify_test2")
    osascript = []
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: osascript.append(a))   # never pop a real notification
    module.notify({"available": False, "candidates": []}, {"warnings": [], "limit_chars": 1},
                  {"candidates": []})
    assert "MODEL_AUDIT_NOTIFY_FAILED discord missing_env" in capsys.readouterr().err
    assert len(osascript) == 1


# ------------------------------------------------------------------ tag-hygiene-export.sh (runs export in the container)

def export_tree(tmp_path, host_file=None) -> Path:
    """Scratch repo for tag-hygiene-export.sh with a fake docker first on PATH that records its argv."""
    root = tmp_path / "exptree"
    (root / "scripts").mkdir(parents=True)
    for name in ("tag-hygiene-export.sh", "hc-host-env.sh", "tag_hygiene.py"):
        shutil.copy(SCRIPTS / name, root / "scripts" / name)
    (root / ".env.local").write_text("", encoding="utf-8")
    if host_file is not None:
        (root / ".env.host.local").write_text(host_file, encoding="utf-8")
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text('#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a"; done > "ARGS_FILE"\ncat > /dev/null\n'
                      .replace("ARGS_FILE", str(tmp_path / "docker.args")))
    docker.chmod(0o755)
    return root


def run_export_sh(tmp_path, root, *args, env=None):
    base = {"PATH": f"{tmp_path / 'fakebin'}:/usr/bin:/bin", "HOME": str(tmp_path)}
    base.update(env or {})
    r = subprocess.run([BASH, str(root / "scripts" / "tag-hygiene-export.sh"), *args], cwd=tmp_path, env=base,
                       capture_output=True, text=True, timeout=60)
    argv = (tmp_path / "docker.args").read_text().splitlines() if (tmp_path / "docker.args").exists() else None
    return r, argv


BASE_ARGV = ["compose", "exec", "-T", "hindsight", "python", "-", "export"]


@pytest.mark.parametrize("source", ["env", "host-file"])
def test_export_sh_passes_the_configured_bank_when_the_caller_gave_none(tmp_path, source):
    root = export_tree(tmp_path, "HC_PERSONAL_BANK=bank-x\n" if source == "host-file" else None)
    r, argv = run_export_sh(tmp_path, root, env={"HC_PERSONAL_BANK": "bank-x"} if source == "env" else None)
    assert r.returncode == 0, r.stderr
    assert argv == BASE_ARGV + ["--bank", "bank-x"]


@pytest.mark.parametrize("caller_args", [["--bank", "bank-y"], ["--bank=bank-y"]], ids=["space", "equals"])
def test_export_sh_keeps_the_callers_bank_untouched(tmp_path, caller_args):
    root = export_tree(tmp_path, "HC_PERSONAL_BANK=bank-x\n")
    r, argv = run_export_sh(tmp_path, root, *caller_args)
    assert r.returncode == 0, r.stderr
    assert argv == BASE_ARGV + caller_args


def test_export_sh_without_any_bank_adds_none_and_lets_the_container_report_config_missing(tmp_path):
    root = export_tree(tmp_path)
    r, argv = run_export_sh(tmp_path, root)
    assert r.returncode == 0, r.stderr
    assert argv == BASE_ARGV


# ------------------------------------------------------------------ inline comments, both loaders and both shells

# (file line, value the key must get). Only an unquoted value has comments: whitespace then `#`.
INLINE_COMMENT_CASES = [
    ("HC_PERSONAL_BANK=personal-demo  # 운영자 bank", "personal-demo"),
    ("HC_PERSONAL_BANK=personal-demo\t# tab", "personal-demo"),
    ("HC_PERSONAL_BANK=bank #", "bank"),
    ("HC_PERSONAL_BANK=bank#frag", "bank#frag"),
    ("HC_PERSONAL_BANK=#lead", "#lead"),
    ('HC_PERSONAL_BANK="bank # kept"', "bank # kept"),
    ("HC_PERSONAL_BANK='bank # kept'", "bank # kept"),
    ('HC_PERSONAL_BANK="bank" # after a quoted value', '"bank" # after a quoted value'),
    ("export HC_PERSONAL_BANK=bank   # exported", "bank"),
    ("HC_PERSONAL_BANK=a b # c", "a b"),
]
SHELLS = [pytest.param(BASH, id="bash"),
          pytest.param(shutil.which("zsh"), id="zsh",
                       marks=pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh not installed"))]


@pytest.mark.parametrize("line, expected", INLINE_COMMENT_CASES)
def test_inline_comments_python_parser(line, expected):
    assert load_hc_host_env().parse(line)["HC_PERSONAL_BANK"] == expected


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("line, expected", INLINE_COMMENT_CASES)
def test_inline_comments_shell_loader_matches_the_python_parser(tmp_path, shell, line, expected):
    root = tmp_path / "r"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPTS / "hc-host-env.sh", root / "scripts" / "hc-host-env.sh")
    (root / ".env.host.local").write_text(line + "\n", encoding="utf-8")
    snippet = '. "%s/scripts/hc-host-env.sh"; hc_load_host_env "%s"; printf "%%s" "$HC_PERSONAL_BANK"' % (root, root)
    r = subprocess.run([shell, "-c", snippet], env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout == expected == load_hc_host_env().parse(line)["HC_PERSONAL_BANK"]


# ------------------------------------------------------------------ an unreadable settings file is a config failure

BAD_BYTES = b"HC_PERSONAL_BANK=bank-x\n# \xff\xfe not utf-8\nHC_OWNER=demo\n"


def bad_tree(tmp_path) -> Path:
    root = make_tree(tmp_path)
    (root / ".env.host.local").write_bytes(BAD_BYTES)
    return root


def test_load_raises_config_invalid_for_a_non_utf8_file(tmp_path):
    mod = load_hc_host_env()
    root = tmp_path / "r"
    root.mkdir()
    (root / ".env.host.local").write_bytes(BAD_BYTES)
    with pytest.raises(mod.ConfigInvalid, match="config_invalid .*env.host.local") as err:
        mod.load(root)
    assert isinstance(err.value, mod.ConfigMissing)           # callers that report a missing key report this too


def test_model_audit_py_reports_config_invalid_not_a_traceback(tmp_path):
    root = bad_tree(tmp_path)
    r = subprocess.run([sys.executable, str(root / "scripts" / "model_audit.py")], cwd=root, capture_output=True,
                       text=True, env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}, timeout=60)
    assert r.returncode == 1
    assert "MODEL_AUDIT_FAIL - config_invalid" in r.stderr and "Traceback" not in r.stderr


def test_render_reports_config_invalid_and_keeps_the_existing_plist(tmp_path):
    root = bad_tree(tmp_path)
    out = tmp_path / "o.plist"
    out.write_text("keep me")
    r = render(root, out, "--set", "REPO=/r")
    assert r.returncode == 1 and "config_invalid" in r.stderr and "Traceback" not in r.stderr
    assert out.read_text() == "keep me"


def test_tag_hygiene_and_duplicate_recall_report_config_invalid(tmp_path, monkeypatch, capsys):
    root = bad_tree(tmp_path)
    monkeypatch.delenv("HC_PERSONAL_BANK", raising=False)
    monkeypatch.delenv("HC_OWNER", raising=False)
    monkeypatch.setattr(load_hc_host_env_module(), "ROOT", root)
    hygiene = load_script("tag_hygiene.py", "tag_hygiene_invalid_test")        # imports without a traceback
    assert hygiene.DEFAULT_BANK is None
    assert hygiene.main(["report", "docs.jsonl"]) == 2
    assert "TAG_HYGIENE_FAIL - config_invalid" in capsys.readouterr().err
    dup = load_script("duplicate_recall.py", "duplicate_recall_invalid_test")
    ledger = tmp_path / "l.log"
    ledger.write_text("")
    assert dup.main(["--ledger", str(ledger), "--audit", str(ledger)]) == 2
    assert "DUPLICATE_RECALL_FAIL - config_invalid" in capsys.readouterr().err


# ------------------------------------------------------------------ tag_retag_plan: no default bank either

def test_tag_retag_plan_without_a_bank_fails_with_config_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("HC_PERSONAL_BANK", raising=False)
    isolate_from_host_file(monkeypatch, tmp_path)
    module = load_script("tag_retag_plan.py", "tag_retag_plan_cfg_test")
    monkeypatch.setattr(module, "DEFAULT_BANK", None)           # tag_hygiene may be cached from an earlier import
    docs = tmp_path / "docs.jsonl"
    docs.write_text("")
    for command in ("plan", "label-targets"):
        assert module.main([command, str(docs)]) == 2
        err = capsys.readouterr().err
        assert "TAG_RETAG_PLAN_FAIL - config_missing HC_PERSONAL_BANK" in err and "None" not in err


def test_tag_retag_plan_with_an_explicit_bank_does_not_need_the_setting(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("HC_PERSONAL_BANK", raising=False)
    isolate_from_host_file(monkeypatch, tmp_path)
    module = load_script("tag_retag_plan.py", "tag_retag_plan_cfg_test2")
    docs = tmp_path / "docs.jsonl"
    docs.write_text("")
    assert module.main(["label-targets", str(docs), "--bank", "no-such-bank"]) == 2
    err = capsys.readouterr().err
    assert "config_missing" not in err and "bank 'no-such-bank' is not declared" in err
