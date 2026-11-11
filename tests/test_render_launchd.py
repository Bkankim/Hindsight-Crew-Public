"""scripts/render-launchd.py and the host-script path de-hardcoding (issue #185).

Hermetic: every render goes to tmp_path, HOME and uv are fakes, nothing touches
~/Library/LaunchAgents and no real gateway/sidecar is contacted. The shell scripts are run
only down paths that exit before any network call (HOME check, python lookup) or against a
copied script tree with a stub curl.
"""
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
RENDERER = REPO / "scripts" / "render-launchd.py"
TEMPLATES = sorted((REPO / "rerank-server" / "launchd").glob("*.plist.example")) + sorted(
    (REPO / "scripts" / "launchd").glob("*.plist.example"))
TEMPLATE_IDS = [t.name.replace(".plist.example", "") for t in TEMPLATES]
RERANK_TEMPLATE = REPO / "rerank-server" / "launchd" / "__PREFIX__.hindsight-rerank.plist.example"
LEFTOVER = re.compile(r"__[A-Z][A-Z0-9_]*__")
BASH = "/bin/bash"
DEMO_SETTINGS = {"HC_PERSONAL_BANK": "personal-demo", "HC_OWNER": "demo"}   # no defaults in the scripts (issue #186)

# (fake uv path, expected __PATH__): P = dirname(dirname(uv)), duplicates dropped
UV_PATH_CASES = [
    ("/opt/homebrew/bin/uv",
     "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"),
    ("/usr/local/bin/uv", "/usr/local/bin:/usr/local/sbin:/usr/bin:/bin:/usr/sbin:/sbin"),
    ("/home/ci/.local/bin/uv",
     "/home/ci/.local/bin:/home/ci/.local/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"),
    ("/usr/bin/uv", "/usr/bin:/usr/sbin:/usr/local/bin:/bin:/sbin"),
]


def run_render(template, out, *args, home="/home/fake", path=None, extra_env=None, cwd=None, prefix="com.test"):
    """`prefix` is passed as `--set PREFIX=` first (a later --set in `args` overrides it); None passes none."""
    env = {"PATH": path if path is not None else "/usr/bin:/bin"}
    if home is not None:
        env["HOME"] = home
    env.update(extra_env or {})
    pre = ["--set", f"PREFIX={prefix}"] if prefix is not None else []
    return subprocess.run([sys.executable, str(RENDERER), "--template", str(template), "--out", str(out), *pre, *args],
                          env=env, capture_output=True, text=True, cwd=cwd)


def fake_uv(tmp_path) -> Path:
    d = tmp_path / "uvhome" / "bin"
    d.mkdir(parents=True)
    uv = d / "uv"
    uv.write_text("#!/bin/sh\n")
    uv.chmod(0o755)
    return uv


def program_arguments(plist):
    return plist["ProgramArguments"]


# ---------------------------------------------------------------- renderer


@pytest.mark.parametrize("template", TEMPLATES, ids=TEMPLATE_IDS)
@pytest.mark.parametrize("uv_how", ["set", "on-path"])
def test_render_every_template_parses_with_fake_home_and_uv(tmp_path, template, uv_how):
    uv = fake_uv(tmp_path)
    out = tmp_path / "out.plist"
    if uv_how == "set":
        r = run_render(template, out, "--set", f"UV={uv}", home="/home/fake")
    else:
        r = run_render(template, out, home="/home/fake", path=f"{uv.parent}:/usr/bin:/bin")
    assert r.returncode == 0, r.stderr
    text = out.read_text(encoding="utf-8")
    assert not LEFTOVER.search(text)
    data = plistlib.loads(text.encode("utf-8"))
    assert data["Label"] == "com.test." + template.name[len("__PREFIX__."):-len(".plist.example")]
    assert str(REPO) in text
    assert "/home/fake/Library/Logs/" in data["StandardOutPath"]
    if shutil.which("plutil"):
        lint = subprocess.run(["plutil", "-lint", str(out)], capture_output=True, text=True)
        assert lint.returncode == 0, lint.stdout + lint.stderr
    assert stat.S_IMODE(out.stat().st_mode) == 0o644


@pytest.mark.parametrize("uv, expected_path", UV_PATH_CASES, ids=[c[0] for c in UV_PATH_CASES])
def test_rerank_template_uv_and_path_rule(tmp_path, uv, expected_path):
    template = RERANK_TEMPLATE
    out = tmp_path / "rerank.plist"
    r = run_render(template, out, "--set", f"UV={uv}", "--set", "__REPO__=/srv/hc")
    assert r.returncode == 0, r.stderr
    data = plistlib.loads(out.read_bytes())
    assert program_arguments(data)[0] == uv
    assert data["EnvironmentVariables"]["PATH"] == expected_path
    assert data["EnvironmentVariables"]["HOME"] == "/home/fake"
    assert data["WorkingDirectory"] == "/srv/hc/rerank-server"
    assert program_arguments(data)[3] == "/srv/hc/rerank-server"


def test_demo_machine_render_matches_the_pre_template_literals(tmp_path):
    """On a /opt/homebrew uv the new tokens reproduce the strings the old template hard-coded."""
    template = RERANK_TEMPLATE
    out = tmp_path / "rerank.plist"
    assert run_render(template, out, "--set", "UV=/opt/homebrew/bin/uv", home="/Users/demoalt").returncode == 0
    data = plistlib.loads(out.read_bytes())
    assert program_arguments(data)[0] == "/opt/homebrew/bin/uv"
    assert data["EnvironmentVariables"]["PATH"] == (
        "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")


def test_rerank_template_renders_batch_size_16(tmp_path):
    """Issue #228: B2 (#84) passed with batch 16; a re-render must not revert the sidecar to the code default 32."""
    out = tmp_path / "rerank.plist"
    r = run_render(RERANK_TEMPLATE, out, "--set", "UV=/opt/homebrew/bin/uv")
    assert r.returncode == 0, r.stderr
    assert plistlib.loads(out.read_bytes())["EnvironmentVariables"]["RERANK_BATCH_SIZE"] == "16"


def test_set_overrides_defaults_and_accepts_both_key_spellings(tmp_path):
    tpl = tmp_path / "t.plist.example"
    tpl.write_text('<?xml version="1.0"?><plist version="1.0"><dict><key>a</key><string>__REPO__</string>'
                   '<key>b</key><string>__PREFIX__</string><key>c</key><string>__HOME__</string></dict></plist>')
    out = tmp_path / "o.plist"
    r = run_render(tpl, out, "--set", "REPO=/r", "--set", "__PREFIX__=com.x", "--set", "__HOME__=/h")
    assert r.returncode == 0, r.stderr
    assert plistlib.loads(out.read_bytes()) == {"a": "/r", "b": "com.x", "c": "/h"}


def test_explicit_path_needs_no_uv(tmp_path):
    tpl = tmp_path / "t.plist.example"
    tpl.write_text('<?xml version="1.0"?><plist version="1.0"><dict><key>p</key><string>__PATH__</string></dict></plist>')
    out = tmp_path / "o.plist"
    r = run_render(tpl, out, "--set", "PATH=/only/this", path=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert plistlib.loads(out.read_bytes()) == {"p": "/only/this"}


def test_values_are_xml_escaped(tmp_path):
    tpl = tmp_path / "t.plist.example"
    tpl.write_text('<?xml version="1.0"?><plist version="1.0"><dict><key>p</key><string>__REPO__</string></dict></plist>')
    out = tmp_path / "o.plist"
    assert run_render(tpl, out, "--set", "REPO=/a&b/<c>").returncode == 0
    assert plistlib.loads(out.read_bytes()) == {"p": "/a&b/<c>"}


@pytest.mark.parametrize("args, home, path, message", [
    (["--set", "UV=/x/bin/uv"], "/home/fake", None, None),  # control: succeeds, see below
    ([], "/home/fake", "EMPTY", "uv not found"),
    ([], "", None, "HOME is empty"),
    ([], None, None, "HOME is empty"),
    (["--set", "BOGUS"], "/home/fake", None, "KEY=VALUE"),
], ids=["control-ok", "no-uv", "empty-home", "unset-home", "bad-set"])
def test_render_failures_keep_the_existing_out_untouched(tmp_path, args, home, path, message):
    template = RERANK_TEMPLATE
    out = tmp_path / "o.plist"
    out.write_text("PREVIOUS")
    empty = tmp_path / "empty"
    empty.mkdir()
    r = run_render(template, out, *args, home=home, path=str(empty) if path == "EMPTY" else path)
    if message is None:
        assert r.returncode == 0, r.stderr
        return
    assert r.returncode == 1
    assert message in r.stderr
    assert out.read_text() == "PREVIOUS"


@pytest.mark.parametrize("key", ["UV", "HOME", "REPO", "PREFIX", "__PATH__"])
def test_empty_set_value_fails(tmp_path, key):
    template = RERANK_TEMPLATE
    out = tmp_path / "o.plist"
    out.write_text("PREVIOUS")
    r = run_render(template, out, "--set", "UV=/x/bin/uv", "--set", f"{key}=")
    assert r.returncode == 1
    assert "empty value" in r.stderr
    assert out.read_text() == "PREVIOUS"


@pytest.mark.parametrize("token", ["__Home__", "__repo__", "__Prefix_1__"])
def test_mixed_case_placeholder_is_not_silently_kept(tmp_path, token):
    tpl = tmp_path / "t.plist.example"
    tpl.write_text('<?xml version="1.0"?><plist version="1.0"><dict><key>p</key><string>' + token + '/x</string>'
                   '</dict></plist>')
    r = run_render(tpl, tmp_path / "o.plist")
    assert r.returncode == 1
    assert token in r.stderr
    assert not (tmp_path / "o.plist").exists()


def test_templates_carry_only_the_known_uppercase_tokens():
    """The mixed-case leftover check is only safe while no template uses a lowercase-bearing __x__ literal."""
    seen = set()
    for t in TEMPLATES:
        seen |= set(re.findall(r"__[A-Za-z][A-Za-z0-9_]*__", t.read_text(encoding="utf-8")))
    assert seen <= {"__REPO__", "__HOME__", "__UV__", "__PATH__", "__PREFIX__"}


@pytest.mark.parametrize("path_entry", [".", "bin", "./bin"])
def test_uv_found_through_a_relative_path_entry_is_rendered_absolute(tmp_path, path_entry):
    work = tmp_path / "work"
    (work / "bin").mkdir(parents=True)
    uv = work / "bin" / "uv"
    uv.write_text("#!/bin/sh\n")
    uv.chmod(0o755)
    cwd = work / "bin" if path_entry == "." else work
    out = tmp_path / "o.plist"
    template = RERANK_TEMPLATE
    r = run_render(template, out, path=f"{path_entry}:/usr/bin:/bin", cwd=str(cwd))
    assert r.returncode == 0, r.stderr
    data = plistlib.loads(out.read_bytes())
    rendered_uv = program_arguments(data)[0]
    assert os.path.isabs(rendered_uv)
    assert os.path.realpath(rendered_uv) == os.path.realpath(uv)
    assert data["EnvironmentVariables"]["PATH"].startswith("/")


def test_leftover_placeholder_fails_and_names_it(tmp_path):
    template = tmp_path / "t.plist.example"
    template.write_text('<?xml version="1.0"?><plist version="1.0"><dict><key>p</key><string>__NOPE__/__REPO__</string>'
                        '</dict></plist>')
    out = tmp_path / "o.plist"
    r = run_render(template, out)
    assert r.returncode == 1
    assert "__NOPE__" in r.stderr and "__REPO__" not in r.stderr
    assert not out.exists()


def test_a_value_that_looks_like_a_placeholder_is_not_rescanned(tmp_path):
    tpl = tmp_path / "t.plist.example"
    tpl.write_text('<?xml version="1.0"?><plist version="1.0"><dict><key>p</key><string>__REPO__</string></dict></plist>')
    out = tmp_path / "o.plist"
    assert run_render(tpl, out, "--set", "REPO=/x/__REPO__/y").returncode == 0
    assert plistlib.loads(out.read_bytes()) == {"p": "/x/__REPO__/y"}


def test_unparseable_template_fails(tmp_path):
    tpl = tmp_path / "t.plist.example"
    tpl.write_text("<plist><dict>__REPO__")
    r = run_render(tpl, tmp_path / "o.plist")
    assert r.returncode == 1 and "does not parse" in r.stderr


# ---------------------------------------------------------------- shell side


def run_sh(script, *args, env=None, cwd=REPO):
    base = {"PATH": "/usr/bin:/bin"}
    base.update(env or {})
    return subprocess.run([BASH, str(REPO / script), *args], env=base, cwd=cwd, capture_output=True, text=True,
                          timeout=60)


HOME_UNSET_CASES = [
    ("scripts/model-refresh.sh", "MODEL_REFRESH_FAIL - home_unset"),
    ("scripts/model-audit.sh", "MODEL_AUDIT_FAIL - home_unset"),
    ("scripts/scheduled-checks.sh", "SCHEDULED_FAIL - home_unset"),
    ("rerank-server/probe.sh", "RERANK_PROBE_FAIL home_unset"),
    ("rerank-server/rotate-logs.sh", "LOG_ROTATE_FAIL - home_unset"),
]


@pytest.mark.parametrize("script, token", HOME_UNSET_CASES, ids=[c[0] for c in HOME_UNSET_CASES])
@pytest.mark.parametrize("home", [None, ""], ids=["unset", "empty"])
def test_empty_home_fails_with_a_log_line_not_a_demo_default(tmp_path, script, token, home):
    env = {} if home is None else {"HOME": home}
    r = run_sh(script, env=env)
    assert r.returncode == 1
    assert token in r.stdout
    assert "/Users/demoalt" not in r.stdout + r.stderr


PYTHON_MISSING_CASES = [
    ("scripts/model-refresh.sh", "MODEL_REFRESH_FAIL - python_missing"),
    ("scripts/model-audit.sh", "MODEL_AUDIT_FAIL - python_missing"),
    ("scripts/scheduled-checks.sh", "SCHEDULED_FAIL - python_missing"),
]


@pytest.mark.parametrize("script, token", PYTHON_MISSING_CASES, ids=[c[0] for c in PYTHON_MISSING_CASES])
def test_bad_hc_python_fails_loudly_instead_of_falling_through(tmp_path, script, token):
    r = run_sh(script, env={"HOME": str(tmp_path), "HC_PYTHON": str(tmp_path / "no-such-python")})
    assert r.returncode == 1
    assert token in r.stdout


def test_gate1_fails_with_a_message_when_hc_python_is_bad(tmp_path):
    # gate8 is covered by test_gate8_needs_python_only_on_the_tei_path (it reaches docker before python)
    r = run_sh("verify/gate1_health.sh", env={"HOME": str(tmp_path), "HC_PYTHON": str(tmp_path / "no-such-python")})
    assert r.returncode == 1
    assert "python3 not found" in r.stderr


def stub_docker(tmp_path, provider) -> Path:
    """docker stand-in for gate8: verify/lib.sh finds the container (`compose ... ps -q hindsight`, then
    `inspect` for its name) and dc_env asks it `exec <name> printenv VAR`; every answer comes from a fixed table."""
    d = tmp_path / "dockerbin"
    d.mkdir()
    docker = d / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do last="$a"; done\n'
        'case "$last" in\n'
        "  hindsight) printf 'stub-cid\\n' ;;\n"
        "  stub-cid) printf '/stub-hindsight\\n' ;;\n"
        f"  HINDSIGHT_API_RERANKER_PROVIDER) printf '{provider}' ;;\n"
        "  HINDSIGHT_API_EMBEDDINGS_PROVIDER) printf 'local' ;;\n"
        "  HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL) printf 'BAAI/bge-small-en-v1.5' ;;\n"
        "  HINDSIGHT_API_RERANKER_LOCAL_MODEL) printf 'fake/local-reranker' ;;\n"
        "esac\n")
    docker.chmod(0o755)
    return d


@pytest.mark.parametrize("provider, rc, expect", [
    ("local", 0, "MPS sidecar not required"),   # python is not needed on this path
    ("tei", 1, "python3 not found"),            # python is needed from the tei branch on
])
def test_gate8_needs_python_only_on_the_tei_path(tmp_path, provider, rc, expect):
    docker = stub_docker(tmp_path, provider)
    r = run_sh("verify/gate8_rerank_provider.sh",
               env={"HOME": str(tmp_path), "HC_PYTHON": str(tmp_path / "no-such-python"), "HC_EMBED_DIM": "384",
                    "PATH": f"{docker}:/usr/bin:/bin"})
    assert r.returncode == rc, r.stderr
    assert expect in r.stderr


def copied_tree(tmp_path) -> Path:
    """A scratch copy of the files model-refresh.sh needs, so ROOT (and secrets/acl.json) is under tmp."""
    root = tmp_path / "tree"
    (root / "scripts").mkdir(parents=True)
    for name in ("model-refresh.sh", "hc-python.sh", "hc-host-env.sh"):
        shutil.copy(REPO / "scripts" / name, root / "scripts" / name)
    (root / "secrets").mkdir()
    (root / "secrets" / "acl.json").write_text('{"tokens": {"FAKE-TEST-TOKEN": {"identity": "demo"}}}')
    return root


def stub_curl(tmp_path) -> Path:
    """curl stand-in: records the argv (curl.args) and stdin (curl.args.stdin, where `-H @-` delivers the
    Authorization header, issue #214) and answers every call with an empty mental-model list."""
    d = tmp_path / "stubbin"
    d.mkdir()
    log = tmp_path / "curl.args"
    curl = d / "curl"
    curl.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\n[ -t 0 ] || cat >> "{log}.stdin"\nprintf \'{{"items": []}}\\n200\'\n')
    curl.chmod(0o755)
    return d


@pytest.mark.skipif(not Path("/usr/bin/python3").exists() and not shutil.which("python3"),
                    reason="needs a python3 on the host")
def test_model_refresh_finds_python_under_a_minimal_env(tmp_path):
    """`env -i HOME=<tmp> PATH=/usr/bin:/bin bash scripts/model-refresh.sh` (launchd-like env).

    The token comes out of secrets/acl.json through python, and the Authorization header reaches
    curl, so the failure that follows (empty model list = no gateway) proves python was found."""
    root = copied_tree(tmp_path)
    stubs = stub_curl(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    r = subprocess.run([BASH, str(root / "scripts" / "model-refresh.sh")], cwd=root, capture_output=True, text=True,
                       env={"HOME": str(home), "PATH": f"{stubs}:/usr/bin:/bin", **DEMO_SETTINGS}, timeout=60)
    assert r.returncode == 1
    assert "MODEL_REFRESH_FAIL - no_target_models" in r.stdout
    assert "python_missing" not in r.stdout and "token_missing" not in r.stdout
    assert "Authorization: Bearer FAKE-TEST-TOKEN" in (tmp_path / "curl.args.stdin").read_text()
    assert "FAKE-TEST-TOKEN" not in (tmp_path / "curl.args").read_text()
    assert (home / "Library/Logs/hindsight-model-refresh").is_dir()


def test_model_refresh_honors_hc_python_over_path(tmp_path):
    root = copied_tree(tmp_path)
    stubs = stub_curl(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    marker = tmp_path / "python.used"
    wrapper = tmp_path / "mypython"
    wrapper.write_text(f'#!/bin/sh\necho used >> "{marker}"\nexec "{sys.executable}" "$@"\n')
    wrapper.chmod(0o755)
    r = subprocess.run([BASH, str(root / "scripts" / "model-refresh.sh")], cwd=root, capture_output=True, text=True,
                       env={"HOME": str(home), "PATH": f"{stubs}:/usr/bin:/bin", "HC_PYTHON": str(wrapper), **DEMO_SETTINGS},
                       timeout=60)
    assert "MODEL_REFRESH_FAIL - no_target_models" in r.stdout
    assert marker.read_text().count("used") >= 2  # token lookup + model-list parse


def source_helper(env, snippet='hc_find_python; echo "rc=$? PY=$PY"'):
    r = subprocess.run([BASH, "-c", f'. "{REPO}/scripts/hc-python.sh"; {snippet}'], env=env, capture_output=True,
                       text=True)
    return r.stdout.strip()


def test_hc_python_bare_name_is_resolved_through_path(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    p = d / "python3.99"
    p.write_text("#!/bin/sh\n")
    p.chmod(0o755)
    assert source_helper({"PATH": str(d), "HOME": str(tmp_path), "HC_PYTHON": "python3.99"}) == f"rc=0 PY={p}"


@pytest.mark.parametrize("value", ["python3.99-nope", "./python3.99-nope", "/nope/python3"])
def test_hc_python_that_resolves_to_nothing_is_a_failure_not_a_fallthrough(tmp_path, value):
    d = tmp_path / "bin"
    d.mkdir()
    p = d / "python3"  # a perfectly good python3 on PATH must not rescue a bad HC_PYTHON
    p.write_text("#!/bin/sh\n")
    p.chmod(0o755)
    assert source_helper({"PATH": str(d), "HOME": str(tmp_path), "HC_PYTHON": value}) == "rc=1 PY="


def test_helper_prefers_path_python3_over_candidates(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    p = d / "python3"
    p.write_text("#!/bin/sh\n")
    p.chmod(0o755)
    assert source_helper({"PATH": str(d), "HOME": str(tmp_path)}) == f"rc=0 PY={p}"


def test_helper_falls_back_to_common_locations_when_path_is_minimal(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    candidates = ["/opt/homebrew/bin/python3", "/usr/local/bin/python3"]
    existing = [c for c in candidates if os.access(c, os.X_OK)]
    if not existing:
        pytest.skip("no python3 in the common install locations on this host")
    assert source_helper({"PATH": str(empty), "HOME": str(tmp_path)}) == f"rc=0 PY={existing[0]}"


def test_helper_finds_the_home_local_bin_candidate(tmp_path):
    if any(os.access(c, os.X_OK) for c in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3")):
        pytest.skip("an earlier candidate exists on this host")
    empty = tmp_path / "empty"
    empty.mkdir()
    local = tmp_path / "home" / ".local" / "bin"
    local.mkdir(parents=True)
    p = local / "python3"
    p.write_text("#!/bin/sh\n")
    p.chmod(0o755)
    assert source_helper({"PATH": str(empty), "HOME": str(tmp_path / "home")}) == f"rc=0 PY={p}"


def test_helper_fails_when_nothing_is_found_and_hc_python_is_empty_string_means_unset(tmp_path):
    if any(os.access(c, os.X_OK) for c in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3")):
        pytest.skip("a candidate exists on this host")
    empty = tmp_path / "empty"
    empty.mkdir()
    assert source_helper({"PATH": str(empty), "HOME": str(tmp_path), "HC_PYTHON": ""}) == "rc=1 PY="


def test_helper_skips_the_xcode_shim_when_command_line_tools_are_missing(tmp_path):
    if not Path("/usr/bin/python3").exists():
        pytest.skip("no /usr/bin/python3 to shadow")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    x = stubs / "xcode-select"
    x.write_text("#!/bin/sh\nexit 2\n")  # `xcode-select -p` fails: no CLT
    x.chmod(0o755)
    out = source_helper({"PATH": f"{stubs}:/usr/bin:/bin", "HOME": str(tmp_path)})
    assert "PY=/usr/bin/python3" not in out


# ---------------------------------------------------------------- the grep from the issue


def test_no_demo_machine_paths_left_in_runtime_files():
    if not shutil.which("git"):
        pytest.skip("git not available")
    files = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True).stdout.split("\n")
    skip = re.compile(r"^(tests|docs|bench|artifacts|fixtures)/|\.md$")
    pat = re.compile(r"/Users/demoalt|/opt/homebrew/bin/uv|/usr/bin/python3")
    hits = []
    for f in files:
        if not f or skip.search(f) or not (REPO / f).is_file():
            continue
        try:
            text = (REPO / f).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        hits += [f"{f}:{i}: {line}" for i, line in enumerate(text.splitlines(), 1) if pat.search(line)]
    assert hits == []
