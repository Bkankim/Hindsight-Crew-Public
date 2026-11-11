"""scripts/mcp-probe.sh transport and target contract (issue #244).

The in-container transport is `python -c '<fixed source>'` (upstream 0.10.0+ images ship no curl). Two layers:

* Transport behaviour: the REAL fixed source embedded in the script is executed, with only `urllib.request.urlopen`
  replaced by a fake, and its stdout/exit code is compared with what curl prints for the same arguments.
* Contract: --handshake-only sends exactly initialize, notifications/initialized and tools/list; the target-selection
  contract (#242) picks project/env/compose files and guards production vs the `hc-next` copy; merely sourcing the
  script (verify/lib.sh does) prints no target line and never exits the caller.

Hermetic like test_script_token_argv.py: stub docker/python on PATH, fake credentials, nothing real is contacted.
"""
import json
import os
import subprocess
import sys

import pytest

from test_script_token_argv import KEY, REPO, Sandbox, assert_no_secret_in_argv

SCRIPT = REPO / "scripts" / "mcp-probe.sh"
URL = "http://localhost:8888/mcp"
INIT_ROUTE = {"match": ["-i", "initialize"], "hdr": "HTTP/1.1 200 OK\r\nmcp-session-id: sess-1\r\n\r\n", "out": '{"r": "init"}'}
LIST_ROUTE = {"match": ["tools/list"], "out": '{"result": {"tools": []}}'}


# --------------------------------------------------------------------------- transport (real source, fake urlopen)

RUNNER = r'''
import email.message, http.client, io, json, os, sys, urllib.error, urllib.request
cfg = json.load(open(sys.argv[1]))
seen = {}

def headers(pairs):
    m = email.message.Message()
    for k, v in pairs:
        m[k] = v
    return m

class Resp:   # what http.client.HTTPResponse offers the source
    version = 11
    def __init__(self, status, reason, pairs, body):
        self.status, self.reason, self.headers, self._body = status, reason, headers(pairs), body.encode()
    def getcode(self):
        return self.status
    def read(self):
        return self._body

def fake_urlopen(req, timeout=None):
    seen.update(method=req.get_method(), url=req.full_url, headers=dict(req.header_items()),
                data=None if req.data is None else req.data.decode("utf-8"), timeout=timeout)
    mode = cfg["mode"]
    if mode == "http":
        status, reason, pairs, body = cfg["response"]
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, reason, headers(pairs), io.BytesIO(body.encode()))
        return Resp(status, reason, pairs, body)
    if mode == "conn":
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
    if mode == "timeout":
        raise TimeoutError("timed out")
    raise AssertionError(mode)

class FakeSock:
    def __init__(self, data):
        self.data = data
    def makefile(self, *a, **k):
        return io.BytesIO(self.data)

class FakeConn(http.client.HTTPConnection):   # the wire: urllib's real handlers (redirect, proxy, error) run on top
    def __init__(self, host, *a, **kw):
        seen.setdefault("hosts", []).append(host)
        super().__init__(host, *a, **kw)
    def set_tunnel(self, *a, **k):
        seen.setdefault("tunnels", []).append(a)
    def request(self, method, url, body=None, headers=None, **kw):
        seen.setdefault("wire", []).append([method, url])
    def getresponse(self):
        r = http.client.HTTPResponse(FakeSock(cfg["raw"].encode("latin-1")))
        r.begin()
        return r

if cfg["mode"] == "raw":
    http.client.HTTPConnection = FakeConn
else:
    urllib.request.urlopen = fake_urlopen
sys.argv = ["-c"] + cfg["args"]
try:
    exec(compile(open(cfg["src"]).read(), "<mcp-probe-python-source>", "exec"), {"__name__": "__main__"})
finally:
    json.dump(seen, open(cfg["seen"], "w"))
'''


@pytest.fixture(scope="module")
def pysrc(tmp_path_factory):
    """The fixed python source exactly as the script embeds it."""
    d = tmp_path_factory.mktemp("pysrc")
    (d / "empty.env").write_text("")      # a named env file must exist; an empty one keeps the real .env.local out
    r = subprocess.run(["bash", "-c", '. "$1"; printf %s "$HC_MCP_PYSRC"', "_", str(SCRIPT)],
                       env={"PATH": "/usr/bin:/bin", "HC_ENV_FILE": str(d / "empty.env")},
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    assert r.returncode == 0 and r.stdout, r.stderr
    assert "'" not in r.stdout       # the script holds it in a single-quoted string
    (d / "src.py").write_text(r.stdout)
    (d / "runner.py").write_text(RUNNER)
    return d


def transport(pysrc, tmp_path, args, mode="http", response=(200, "OK", [], ""), stdin="", raw="", env=None):
    cfg = tmp_path / "cfg.json"
    seen = tmp_path / "seen.json"
    cfg.write_text(json.dumps({"mode": mode, "response": response, "args": args, "src": str(pysrc / "src.py"),
                               "seen": str(seen), "raw": raw}))
    r = subprocess.run([sys.executable, "-I", str(pysrc / "runner.py"), str(cfg)], input=stdin.encode(),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
                       env=dict(os.environ, **env) if env else None)
    return r, (json.loads(seen.read_text()) if seen.exists() else {})


HDRS = [("Content-Type", "text/event-stream"), ("Mcp-Session-Id", "sess-1")]
STATUS_LINE = {200: "HTTP/1.1 200 OK\r\n", 422: "HTTP/1.1 422 Unprocessable Entity\r\n"}
RESPONSES = {200: (200, "OK", HDRS, 'data: {"ok": 1}\n'),
             422: (422, "Unprocessable Entity", HDRS, '{"detail": "bad timestamp"}')}
W = "\\n%{http_code}"      # exactly what the call sites pass: a literal backslash-n, curl expands it


@pytest.mark.parametrize("status", [200, 422])
@pytest.mark.parametrize("flags,include,discard,write", [
    ([], False, False, False),
    (["-i"], True, False, False),
    (["-o", "/dev/null"], False, True, False),
    (["-w", W], False, False, True),
    (["-i", "-w", W], True, False, True),
    (["-o", "/dev/null", "-w", W], False, True, True),
])
def test_output_shape_matches_curl(pysrc, tmp_path, status, flags, include, discard, write):
    """Same bytes curl would print: [status line + headers + blank line (-i)] + body, nothing for `-o /dev/null`,
    then the `-w` text. A 4xx/5xx is a normal response (curl without -f), exit 0, body and headers kept."""
    _, _, hdrs, body = RESPONSES[status]
    r, seen = transport(pysrc, tmp_path, ["-X", "POST", URL, "-H", "@-", "-H", "Content-Type: application/json",
                                          "-d", '{"a": 1}'] + flags, response=RESPONSES[status],
                        stdin="Authorization: Bearer %s\n" % KEY)
    expected = ""
    if not discard:
        if include:
            expected += STATUS_LINE[status] + "".join("%s: %s\r\n" % kv for kv in hdrs) + "\r\n"
        expected += body
    if write:
        expected += "\n%d" % status
    assert r.returncode == 0, r.stderr
    assert r.stdout.decode() == expected
    assert KEY not in r.stdout.decode() + r.stderr.decode()
    assert seen["method"] == "POST" and seen["url"] == URL and seen["data"] == '{"a": 1}'


def test_request_carries_the_stdin_header_and_the_given_headers_once(pysrc, tmp_path):
    r, seen = transport(pysrc, tmp_path, ["-X", "POST", URL, "-H", "@-", "-H", "Content-Type: application/json",
                                          "-H", "Accept: application/json, text/event-stream",
                                          "-H", "X-Bank-Id: probe-n0", "-d", "{}"], response=RESPONSES[200],
                        stdin="Authorization: Bearer %s\n" % KEY)
    assert r.returncode == 0, r.stderr
    h = {k.lower(): v for k, v in seen["headers"].items()}
    assert h["authorization"] == "Bearer " + KEY
    assert h["content-type"] == "application/json"
    assert h["accept"] == "application/json, text/event-stream"
    assert h["x-bank-id"] == "probe-n0"
    assert len(seen["headers"]) == len(h)     # no duplicate (case-folded) header, e.g. a default form content type


def test_body_is_sent_as_utf8_and_method_defaults_follow_curl(pysrc, tmp_path):
    r, seen = transport(pysrc, tmp_path, [URL, "-d", "한글 본문"], response=RESPONSES[200])
    assert r.returncode == 0 and seen["method"] == "POST" and seen["data"] == "한글 본문"
    r, seen = transport(pysrc, tmp_path, ["-X", "GET", URL], response=RESPONSES[200])
    assert r.returncode == 0 and seen["method"] == "GET" and seen["data"] is None
    r, seen = transport(pysrc, tmp_path, ["-sS", URL, "-m", "7"], response=RESPONSES[200])
    assert r.returncode == 0 and seen["method"] == "GET" and seen["timeout"] == 7.0


@pytest.mark.parametrize("mode,needle", [("conn", "ConnectionRefusedError"), ("timeout", "TimeoutError")])
def test_connection_failure_exits_1_with_nothing_on_stdout(pysrc, tmp_path, mode, needle):
    r, _ = transport(pysrc, tmp_path, ["-X", "POST", URL, "-H", "@-", "-i", "-w", W], mode=mode,
                     stdin="Authorization: Bearer %s\n" % KEY)
    assert r.returncode == 1
    assert r.stdout == b""
    assert needle in r.stderr.decode() and KEY not in r.stderr.decode()


REDIRECTS = [301, 302, 303, 307]


@pytest.mark.parametrize("code", REDIRECTS)
def test_redirects_are_returned_as_is_never_followed(pysrc, tmp_path, code):
    """curl without -L hands back the 3xx. urllib's default would follow it (POST turned into GET, and the key sent
    to the redirect target); here the first answer is the result and only one connection is made."""
    raw = ("HTTP/1.1 %d Moved\r\nLocation: http://other.example/elsewhere\r\nContent-Length: 0\r\n\r\n" % code)
    r, seen = transport(pysrc, tmp_path, ["-X", "POST", URL, "-H", "@-", "-d", "{}", "-i", "-w", W], mode="raw",
                        raw=raw, stdin="Authorization: Bearer %s\n" % KEY)
    assert r.returncode == 0, r.stderr
    assert r.stdout.decode() == ("HTTP/1.1 %d Moved\r\nLocation: http://other.example/elsewhere\r\n"
                                 "Content-Length: 0\r\n\r\n\n%d" % (code, code))
    assert seen["hosts"] == ["localhost:8888"]                 # nothing was opened towards other.example
    assert seen["wire"] == [["POST", "/mcp"]]
    assert KEY not in r.stdout.decode() + r.stderr.decode()


@pytest.mark.parametrize("var", ["http_proxy", "HTTP_PROXY", "all_proxy"])
def test_environment_proxies_are_ignored(pysrc, tmp_path, var):
    """The call goes to localhost inside the container: a proxy variable must not divert it (or carry the key
    to the proxy), exactly like curl, which only uses a proxy it is told about on its own command line."""
    raw = "HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
    r, seen = transport(pysrc, tmp_path, ["-X", "POST", URL, "-H", "@-", "-d", "{}"], mode="raw", raw=raw,
                        stdin="Authorization: Bearer %s\n" % KEY, env={var: "http://proxy.invalid:3128"})
    assert r.returncode == 0, r.stderr
    assert r.stdout == b"ok"
    assert seen["hosts"] == ["localhost:8888"] and "tunnels" not in seen
    assert seen["wire"] == [["POST", "/mcp"]]               # origin-form path, not an absolute proxy URL


@pytest.mark.parametrize("args", [["--compressed", URL], ["-X"], []])
def test_unsupported_or_malformed_arguments_exit_2(pysrc, tmp_path, args):
    r, _ = transport(pysrc, tmp_path, args)
    assert r.returncode == 2 and r.stdout == b""


# --------------------------------------------------------------------------- contract (script run in a sandbox)

def probe_box(tmp_path, root_name=None):
    box = Sandbox(tmp_path, ("mcp-probe.sh",))
    if root_name:
        renamed = tmp_path / root_name
        box.root.rename(renamed)
        box.root = renamed
    (box.root / "docker-compose.yml").write_text("services: {}\n")
    box.set_routes([INIT_ROUTE, LIST_ROUTE])
    return box


def py_requests(box):
    """(url, parsed JSON-RPC body or None) of every request the in-container python made."""
    rows = []
    for c in box.calls("python"):
        a = c["argv"]
        rows.append((next(x for x in a[2:] if x.startswith("http")), json.loads(a[a.index("-d") + 1]) if "-d" in a else None))
    return rows


def target_lines(r):
    return [ln for ln in r.stderr.splitlines() if ln.startswith("target:")]


ORDERS = {"flag-first": lambda out: ["--handshake-only", "--out", out],
          "flag-last": lambda out: ["--out", out, "--handshake-only"]}


@pytest.mark.parametrize("order", ["flag-first", "flag-last"])
def test_handshake_only_sends_the_three_requests_and_writes_nothing_else(tmp_path, order):
    """Position of --handshake-only must not matter: `--out X --handshake-only` once fell through to the writing
    collection (retain + REST POST on probe-n0, fixtures/ overwritten)."""
    box = probe_box(tmp_path)
    out = tmp_path / "out"
    r = box.run("scripts/mcp-probe.sh", *ORDERS[order](str(out)), HC_HINDSIGHT_API_KEY=KEY)
    assert r.returncode == 0, r.stdout + r.stderr
    reqs = py_requests(box)
    assert [(u, b.get("method")) for u, b in reqs] == [(URL, "initialize"), (URL, "notifications/initialized"),
                                                         (URL, "tools/list")]
    for _, b in reqs:    # no tools/call, no REST (/v1/...), nothing but the handshake
        assert b["method"] in ("initialize", "notifications/initialized", "tools/list")
    assert all("exec" in c["argv"] and "python" in c["argv"] for c in box.calls("docker"))   # no printenv either
    assert sorted(p.name for p in out.iterdir()) == ["initialize.headers", "initialize.raw", "tools_list.raw"]
    assert (out / "tools_list.raw").read_text() == '{"result": {"tools": []}}'
    assert "mcp-session-id: sess-1" in (out / "initialize.headers").read_text()
    assert (out / "initialize.raw").read_text() == '{"r": "init"}\n'
    assert not (box.root / "fixtures").exists()
    assert r.stdout == ""
    assert_no_secret_in_argv(box)


def test_handshake_only_defaults_to_a_fresh_temp_directory(tmp_path):
    box = probe_box(tmp_path)
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    r = box.run("scripts/mcp-probe.sh", "--handshake-only", HC_HINDSIGHT_API_KEY=KEY, TMPDIR=str(tmp))
    assert r.returncode == 0, r.stdout + r.stderr
    made = [p for p in tmp.iterdir() if p.is_dir()]
    assert len(made) == 1 and (made[0] / "tools_list.raw").is_file()
    assert not (box.root / "fixtures").exists()


@pytest.mark.parametrize("target,project,ok", [
    ("next", "hindsight-crew", False),     # the copy marker pointing at production
    ("next", "hc-next-other", False),      # exact match, not a prefix
    ("next", "hc-next", True),
    ("", "hc-next", False),                # no marker but the copy's project
    ("", "hc-next-other", False),
    ("", "hindsight-crew", True),
])
def test_target_guard_for_project(tmp_path, target, project, ok):
    box = probe_box(tmp_path)
    r = box.run("scripts/mcp-probe.sh", "--handshake-only", "--out", str(tmp_path / "o"), HC_HINDSIGHT_API_KEY=KEY,
                HC_COMPOSE_PROJECT=project, **({"HC_TARGET": target} if target else {}))
    assert (r.returncode == 0) == ok, r.stdout + r.stderr
    assert target_lines(r) == ["target: gw=- project=%s container=- pg0=-" % project]
    if not ok:
        assert box.calls() == []          # refused before anything reached docker


@pytest.mark.parametrize("script_args", [["--handshake-only"], []])
def test_main_guard_refuses_before_any_write(tmp_path, script_args):
    """The default collecting main() runs the same guard: HC_TARGET=next + production project -> exit 1, no
    docker call, no fixtures/."""
    box = probe_box(tmp_path)
    r = box.run("scripts/mcp-probe.sh", *script_args, HC_HINDSIGHT_API_KEY=KEY, HC_TARGET="next",
                HC_COMPOSE_PROJECT="hindsight-crew")
    assert r.returncode == 1
    assert len(target_lines(r)) == 1
    assert box.calls() == [] and not (box.root / "fixtures").exists()


@pytest.mark.parametrize("root_name", ["HC-Next!", "_HC-Next!", "-_-hc-next"])
def test_unset_project_resolves_like_compose(tmp_path, root_name):
    """No HC_COMPOSE_PROJECT: COMPOSE_PROJECT_NAME wins, else the repo-root basename normalised the compose way
    (lowercase, characters outside [a-z0-9_-] dropped, leading `_`/`-` trimmed). -p is not passed in either case."""
    box = probe_box(tmp_path, root_name=root_name)
    r = box.run("scripts/mcp-probe.sh", "--handshake-only", "--out", str(tmp_path / "o"), HC_HINDSIGHT_API_KEY=KEY,
                HC_TARGET="next")
    assert r.returncode == 0, r.stdout + r.stderr
    assert target_lines(r) == ["target: gw=- project=hc-next container=- pg0=-"]
    assert all("-p" not in c["argv"] for c in box.calls("docker"))
    r = box.run("scripts/mcp-probe.sh", "--handshake-only", "--out", str(tmp_path / "o2"), HC_HINDSIGHT_API_KEY=KEY,
                COMPOSE_PROJECT_NAME="hindsight-crew")
    assert r.returncode == 0 and target_lines(r) == ["target: gw=- project=hindsight-crew container=- pg0=-"]


def docker_prefix(box):
    """compose argv up to `exec` for the first exec call."""
    argv = next(c["argv"] for c in box.calls("docker") if "exec" in c["argv"])
    return argv[:argv.index("exec")]


def test_caller_project_beats_the_env_file_value(tmp_path):
    box = probe_box(tmp_path)
    (box.root / "envfile").write_text("HC_COMPOSE_PROJECT=from-file\nHC_HINDSIGHT_API_KEY=%s\n" % KEY)
    r = box.run("scripts/mcp-probe.sh", "--handshake-only", "--out", str(tmp_path / "o1"), HC_ENV_FILE="envfile",
                HC_COMPOSE_PROJECT="from-caller")
    assert r.returncode == 0, r.stdout + r.stderr
    assert target_lines(r) == ["target: gw=- project=from-caller container=- pg0=-"]
    pre = docker_prefix(box)
    assert pre[pre.index("-p") + 1] == "from-caller" and "from-file" not in pre
    # without a caller value the file's value is used (relative HC_ENV_FILE = relative to the repo root)
    r = box.run("scripts/mcp-probe.sh", "--handshake-only", "--out", str(tmp_path / "o2"), HC_ENV_FILE="envfile")
    assert r.returncode == 0, r.stdout + r.stderr
    assert target_lines(r) == ["target: gw=- project=from-file container=- pg0=-"]


def test_compose_call_shape(tmp_path):
    box = probe_box(tmp_path)
    (box.root / "extra.yml").write_text("services: {}\n")
    (box.root / "envfile").write_text("HC_HINDSIGHT_API_KEY=%s\n" % KEY)
    r = box.run("scripts/mcp-probe.sh", "--handshake-only", "--out", str(tmp_path / "o"), HC_ENV_FILE="envfile",
                HC_COMPOSE_PROJECT="hindsight-crew", HC_COMPOSE_FILES="docker-compose.yml:/abs/other.yml:extra.yml")
    assert r.returncode == 0, r.stdout + r.stderr
    root = str(box.root)
    assert docker_prefix(box) == ["compose", "--env-file", root + "/envfile", "--project-directory", root,
                                  "-p", "hindsight-crew", "-f", root + "/docker-compose.yml", "-f", "/abs/other.yml",
                                  "-f", root + "/extra.yml"]


def test_default_compose_call_has_no_project_flag_and_skips_a_missing_env_file(tmp_path):
    box = probe_box(tmp_path)    # no .env.local in the sandbox
    r = box.run("scripts/mcp-probe.sh", "--handshake-only", "--out", str(tmp_path / "o"), HC_HINDSIGHT_API_KEY=KEY)
    assert r.returncode == 0, r.stdout + r.stderr
    root = str(box.root)
    assert docker_prefix(box) == ["compose", "--project-directory", root, "-f", root + "/docker-compose.yml"]
    (box.root / ".env.local").write_text("HC_HINDSIGHT_API_KEY=%s\n" % KEY)       # default env file when present
    box.log.write_text("")
    r = box.run("scripts/mcp-probe.sh", "--handshake-only", "--out", str(tmp_path / "o2"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert docker_prefix(box)[:3] == ["compose", "--env-file", root + "/.env.local"]


def source_in_bash(box, snippet, **env):
    return subprocess.run(["/bin/bash", "-c", snippet, "_", str(box.root)], env=box.env(**env),
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True)


def test_caller_values_beat_the_env_file_for_all_five_contract_variables(tmp_path):
    box = probe_box(tmp_path)
    (box.root / ".env.local").write_text("".join("%s=file-%s\n" % (k, k) for k in (
        "HC_GATEWAY_BIND", "HC_PG0_VOLUME", "HC_HINDSIGHT_CONTAINER", "HC_COMPOSE_PROJECT", "HC_COMPOSE_FILES")) +
        "HC_HINDSIGHT_API_KEY=file-key\n")
    r = source_in_bash(box, '. "$1/scripts/mcp-probe.sh"; printf "%s|%s|%s|%s|%s|%s\\n" "$HC_GATEWAY_BIND" '
                            '"$HC_PG0_VOLUME" "$HC_HINDSIGHT_CONTAINER" "$HC_COMPOSE_PROJECT" "$HC_COMPOSE_FILES" '
                            '"$HC_MCP_KEY"',
                       HC_GATEWAY_BIND="127.0.0.1:18888", HC_PG0_VOLUME="hc-next_pg0", HC_COMPOSE_FILES="a.yml:b.yml")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == ("127.0.0.1:18888|hc-next_pg0|file-HC_HINDSIGHT_CONTAINER|file-HC_COMPOSE_PROJECT|"
                                "a.yml:b.yml|file-key")      # unrelated file values (the key) still load as before


def test_sourcing_prints_no_target_line_and_never_exits_the_caller(tmp_path):
    """verify/lib.sh sources this file: even a guard violation must stay silent and not end the gate's shell."""
    box = probe_box(tmp_path)
    r = source_in_bash(box, '. "$1/scripts/mcp-probe.sh"; echo survived', HC_TARGET="next",
                       HC_COMPOSE_PROJECT="hindsight-crew")
    assert r.returncode == 0 and r.stdout == "survived\n"
    assert r.stderr == ""
    assert box.calls() == []


def test_verify_lib_gate_path_is_unchanged_by_the_python_transport(tmp_path):
    """verify/lib.sh's mcp_call swaps HC_MCP_CURL to a host curl against the gateway: it must keep working
    (handshake + call over the stub curl) and reach neither docker nor the in-container python. The `target:` line
    is lib.sh's (one, project column from the directory name, gateway the production default); the probe sourced
    by lib.sh adds none of its own (`gw=-`)."""
    box = probe_box(tmp_path)
    (box.root / "verify").mkdir()
    (box.root / "verify" / "lib.sh").write_text((REPO / "verify" / "lib.sh").read_text())
    box.set_routes([INIT_ROUTE, {"match": ["/mcp/probe-n0/", "tools/list"], "out": 'data: {"result": 1}\n'}])
    snippet = '. "$1/verify/lib.sh"; mcp_call tok probe-n0 \'{"jsonrpc":"2.0","id":2,"method":"tools/list"}\'; echo "rc=$?"'
    r = source_in_bash(box, snippet, HC_HINDSIGHT_API_KEY=KEY)
    assert r.stdout == '{"result": 1}\n200rc=0\n', r.stdout + r.stderr
    assert target_lines(r) == ["target: gw=127.0.0.1:8888 project=repo container=- pg0=-"], r.stderr
    assert "gw=-" not in r.stderr
    assert box.calls("docker") == [] and box.calls("python") == []
    assert [c["argv"][c["argv"].index("-X") + 1] for c in box.calls("curl")] == ["POST"] * 3


def test_relative_out_is_relative_to_the_callers_directory(tmp_path):
    """main() works from the repo root, but a relative --out is the caller's: `--out fixtures/mcp` must never land
    in the repo's fixtures/ just because the script cd's there."""
    box = probe_box(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    r = subprocess.run(["/bin/bash", str(box.root / "scripts" / "mcp-probe.sh"), "--handshake-only", "--out",
                        "fixtures/mcp"], cwd=str(elsewhere), env=box.env(HC_HINDSIGHT_API_KEY=KEY),
                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert sorted(p.name for p in (elsewhere / "fixtures" / "mcp").iterdir()) == [
        "initialize.headers", "initialize.raw", "tools_list.raw"]
    assert not (box.root / "fixtures").exists()


def test_compose_files_split_skips_empty_entries_and_never_globs(tmp_path):
    box = probe_box(tmp_path)
    (box.root / "a1.yml").write_text("services: {}\n")
    (box.root / "a2.yml").write_text("services: {}\n")
    r = box.run("scripts/mcp-probe.sh", "--handshake-only", "--out", str(tmp_path / "o"), HC_HINDSIGHT_API_KEY=KEY,
                HC_COMPOSE_FILES=":docker-compose.yml::*.yml:")
    assert r.returncode == 0, r.stdout + r.stderr
    root = str(box.root)
    assert docker_prefix(box) == ["compose", "--project-directory", root, "-f", root + "/docker-compose.yml",
                                  "-f", root + "/*.yml"]       # no `-f <root>/`, and `*.yml` stays literal
