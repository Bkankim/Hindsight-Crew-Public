"""Host scripts keep credentials out of argv (issue #214).

`curl -H "Authorization: Bearer $TOKEN"` and `docker ... -e NAME="$SECRET"` put the secret on a command line,
which any process of the same user can read with `ps`. The four scripts below must hand it over through stdin
(`curl -H @-`, `docker exec` stdin) or the child's environment (`NAME=value docker run -e NAME`) instead.

Hermetic: each script runs from a scratch copy of the repo (so `.env.local`, `secrets/` and the restore-test marker
file land under tmp_path - restore-test.sh stops/starts hindsight and must never run from the real tree), HOME is a
tmp directory, and PATH holds ONLY recording stubs (curl, docker, sh, python, sleep) plus an explicit list of real tools.
There is no real docker, curl or launchctl to reach. Every credential is a fake string.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

OWNER_TOKEN = "FAKE-OWNER-TOKEN-0f3a9c"
OTHER_TOKEN = "FAKE-OTHER-TOKEN-77b1d2"
KEY = "FAKE-UPSTREAM-KEY-5e8841"
ALL_FAKES = (OWNER_TOKEN, OTHER_TOKEN, KEY)

# Recording stub shared by curl, docker and sh. Every call appends {tool, argv, stdin, env} to $STUB_LOG.
# `env` holds only whether named variables equal the fake secrets (never the values themselves).
STUB_SOURCE = r'''#!%(python)s
import json, os, subprocess, sys
NAME = %(name)r
log = os.environ["STUB_LOG"]
stdin = "" if os.isatty(0) else sys.stdin.read()
argv = sys.argv[1:]
secrets = json.loads(os.environ.get("STUB_WATCH", "{}"))
with open(log, "a") as fh:
    fh.write(json.dumps({"tool": NAME, "argv": argv, "stdin": stdin,
                         "env": {k: os.environ.get(k) == v for k, v in secrets.items()},
                         "envval": {k: os.environ.get(k) for k in os.environ.get("STUB_WATCH_VALUES", "").split(",") if k}}) + "\n")
routes = json.load(open(os.environ["STUB_ROUTES"])) if os.environ.get("STUB_ROUTES") else []

def render_curl():
    # `python -c <src> <curl-args>` (mcp-probe's in-container transport) answers like curl, from its curl-style tail
    cargv = argv[2:] if NAME == "python" and argv[:1] == ["-c"] else argv
    line = " ".join(cargv)
    out = "{}"
    hdr = ""
    code = "200"
    for r in routes:
        if all(m in line for m in r["match"]):
            out, hdr, code = r.get("out", "{}"), r.get("hdr", ""), r.get("code", "200")
            break
    if "-o" in cargv:
        out = ""
    elif "-i" in cargv:
        out = hdr + out
    if "-w" in cargv:
        fmt = cargv[cargv.index("-w") + 1].replace("%%{http_code}", code).replace("\\n", "\n")
        out += fmt
    sys.stdout.write(out)

def run_docker():
    # `docker compose ... exec [-T] [-e NAME[=VAL]]... SERVICE CMD...` runs CMD locally, like a container would
    if "exec" in argv and argv[0] == "compose":
        rest = argv[argv.index("exec") + 1:]
        child_env = dict(os.environ)
        while rest and rest[0].startswith("-"):
            flag = rest.pop(0)
            if flag in ("-e", "--env"):
                item = rest.pop(0)
                name, _, val = item.partition("=")
                child_env[name] = val if "=" in item else os.environ.get(name, "")
        rest.pop(0)  # service name
        sys.stdout.flush()
        # bytes in/out: text mode would turn the CRLF of a `-i` header block into LF (mcp-probe splits on CRLF CRLF)
        r = subprocess.run(rest, input=stdin.encode("utf-8"), env=child_env, stdout=subprocess.PIPE)
        sys.stdout.buffer.write(r.stdout)
        sys.stdout.flush()
        sys.exit(r.returncode)
    for r in routes:
        if all(m in " ".join(argv) for m in r["match"]):
            sys.stdout.write(r.get("out", ""))
            sys.exit(r.get("rc", 0))

if NAME in ("curl", "python"):
    render_curl()
elif NAME == "docker":
    run_docker()
elif NAME == "sh":
    sys.stdout.flush()
    sys.exit(subprocess.run(["/bin/sh"] + argv, input=stdin, universal_newlines=True).returncode)
'''

REAL_TOOLS = ("bash", "env", "dirname", "basename", "mkdir", "mktemp", "grep", "head", "tail", "sed", "tr", "cat", "date",
              "awk", "rm", "mv", "ls", "seq", "wc", "jq", "printenv", "cut", "sort")


class Sandbox:
    def __init__(self, tmp_path, scripts):
        self.root = tmp_path / "repo"
        self.home = tmp_path / "home"
        self.bin = tmp_path / "stubs"
        self.tools = tmp_path / "tools"
        self.log = tmp_path / "calls.jsonl"
        for d in (self.root / "scripts", self.root / "secrets", self.home, self.bin, self.tools):
            d.mkdir(parents=True)
        self.log.write_text("")
        for name in scripts:
            shutil.copy2(REPO / "scripts" / name, self.root / "scripts" / name)
        for name in ("curl", "docker", "sh", "python"):
            f = self.bin / name
            f.write_text(STUB_SOURCE % {"python": sys.executable, "name": name})
            f.chmod(0o755)
        sleep = self.bin / "sleep"
        sleep.write_text("#!/bin/sh\nexit 0\n")
        sleep.chmod(0o755)
        for name in REAL_TOOLS:
            real = shutil.which(name)
            if real:
                (self.tools / name).symlink_to(real)
        (self.tools / "python3").symlink_to(os.path.realpath(sys.executable))
        self.routes = tmp_path / "routes.json"
        self.set_routes([])

    def set_routes(self, routes):
        self.routes.write_text(json.dumps(routes))

    def env(self, **extra):
        env = {
            "PATH": "%s:%s" % (self.bin, self.tools),
            "HOME": str(self.home),
            "STUB_LOG": str(self.log),
            "STUB_ROUTES": str(self.routes),
            "STUB_WATCH": json.dumps({"HINDSIGHT_API_MCP_AUTH_TOKEN": KEY, "HC_MCP_KEY": KEY}),
            "STUB_WATCH_VALUES": "HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL,HINDSIGHT_API_RERANKER_LOCAL_MODEL",
        }
        env.update(extra)
        return env

    def run(self, script, *args, **env):
        return subprocess.run(["/bin/bash", str(self.root / script), *args], cwd=str(self.root), env=self.env(**env),
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, timeout=120)

    def calls(self, tool=None):
        rows = [json.loads(line) for line in self.log.read_text().splitlines() if line]
        return [r for r in rows if tool is None or r["tool"] == tool]


def assert_no_secret_in_argv(box, rows=None):
    for row in rows if rows is not None else box.calls():
        argv = " ".join(row["argv"])
        for fake in ALL_FAKES:
            assert fake not in argv, "%s argv carries a credential: %s" % (row["tool"], row["argv"])


def write_runtime_acl(box):
    (box.root / "secrets" / "acl.json").write_text(json.dumps({"tokens": {
        OTHER_TOKEN: {"identity": "carol"}, OWNER_TOKEN: {"identity": "demo"}}}))


# --------------------------------------------------------------------------- model-refresh.sh

def test_model_refresh_sends_the_owners_token_on_stdin_never_in_argv(tmp_path):
    box = Sandbox(tmp_path, ("model-refresh.sh", "hc-python.sh", "hc-host-env.sh"))
    write_runtime_acl(box)
    box.set_routes([
        {"match": ["/mental-models/m1/clear"], "out": "{}"},
        {"match": ["/mental-models/m1/refresh"], "out": '{"operation_id": "op1"}'},
        {"match": ["/operations/op1"], "out": '{"status": "completed", "result_metadata": {"content_len": 42}}'},
        {"match": ["/memories/list"], "out": '{"total": 3}'},
        {"match": ["/mental-models"], "out": json.dumps(
            {"items": [{"id": "m1", "tags": ["project:x"],
                        # delta mode is the only one that clears (issue #272), so the clear call keeps its token check
                        "trigger": {"refresh_after_consolidation": False, "mode": "delta"}}]})},
    ])
    r = box.run("scripts/model-refresh.sh", HC_PERSONAL_BANK="bank-x", HC_OWNER="demo")
    assert r.returncode == 0, r.stdout + r.stderr
    # HTTP behaviour and the log tokens the monitor greps are unchanged
    assert "MODEL_REFRESH_FULL m1" in r.stdout and "MODEL_REFRESH_OK m1 42" in r.stdout
    assert "MODEL_REFRESH_RUN_DONE full_pass=1 all_ok=1" in r.stdout
    curls = box.calls("curl")
    methods = [(c["argv"][c["argv"].index("-X") + 1], c["argv"][-1].split("/v1/default/banks/bank-x")[1]) for c in curls]
    assert ("POST", "/mental-models/m1/clear") in methods and ("POST", "/mental-models/m1/refresh") in methods
    assert len(curls) >= 5
    assert_no_secret_in_argv(box)
    for c in curls:
        assert "-H" in c["argv"] and c["argv"][c["argv"].index("-H") + 1] == "@-"
        # the identity-matching token is the one that travels, the other principal's never does
        assert "Authorization: Bearer %s" % OWNER_TOKEN in c["stdin"]
        assert OTHER_TOKEN not in c["stdin"]
    assert OWNER_TOKEN not in r.stdout + r.stderr


# Issue #243: upstream >=0.10 lists mental models with detail=metadata by default, and metadata has no `trigger`.
# The script asks for detail=content explicitly so target selection works on 0.9.1 and 1.0.0 alike.
LIST_PATH_SUFFIX = "/mental-models?detail=content&limit=1000"


def model_refresh_box(tmp_path, list_body):
    box = Sandbox(tmp_path, ("model-refresh.sh", "hc-python.sh", "hc-host-env.sh"))
    write_runtime_acl(box)
    box.set_routes([
        {"match": ["/mental-models/m1/clear"], "out": "{}"},
        {"match": ["/mental-models/m1/refresh"], "out": '{"operation_id": "op1"}'},
        {"match": ["/operations/op1"], "out": '{"status": "completed", "result_metadata": {"content_len": 42}}'},
        {"match": ["/memories/list"], "out": '{"total": 3}'},
        {"match": ["/mental-models"], "out": json.dumps(list_body)},
    ])
    return box


CONTENT_SHAPE = {"items": [
    {"id": "m1", "tags": ["project:x"], "trigger": {"refresh_after_consolidation": False}},
    {"id": "m2", "tags": ["project:y"], "trigger": {"refresh_after_consolidation": True}},
]}
METADATA_SHAPE = {"items": [
    {"id": "m1", "tags": ["project:x"], "name": "M1", "created_at": "2026-10-01T00:00:00Z"},
    {"id": "m2", "tags": ["project:y"], "name": "M2", "created_at": "2026-10-01T00:00:00Z"},
]}


def test_model_refresh_list_request_asks_for_detail_content_and_page_limit(tmp_path):
    box = model_refresh_box(tmp_path, CONTENT_SHAPE)
    r = box.run("scripts/model-refresh.sh", HC_PERSONAL_BANK="bank-x", HC_OWNER="demo")
    assert r.returncode == 0, r.stdout + r.stderr
    paths = [c["argv"][-1].split("/v1/default/banks/bank-x")[1] for c in box.calls("curl")
             if c["argv"][c["argv"].index("-X") + 1] == "GET"]
    lists = [p for p in paths if p.split("?")[0] == "/mental-models"]
    assert len(lists) == 1 and lists[0] == LIST_PATH_SUFFIX, lists


@pytest.mark.parametrize("shape,expect_ok", [("content", True), ("metadata", False)])
def test_model_refresh_target_selection_by_list_shape(tmp_path, shape, expect_ok):
    box = model_refresh_box(tmp_path, CONTENT_SHAPE if shape == "content" else METADATA_SHAPE)
    r = box.run("scripts/model-refresh.sh", HC_PERSONAL_BANK="bank-x", HC_OWNER="demo")
    if expect_ok:
        assert r.returncode == 0, r.stdout + r.stderr
        assert "MODEL_REFRESH_FULL m1" in r.stdout
        assert "m2" not in r.stdout  # refresh_after_consolidation=true is server-driven, never a target
    else:
        assert r.returncode == 1, r.stdout + r.stderr
        assert "MODEL_REFRESH_FAIL - no_target_models" in r.stdout


# Issue #255: a non-2xx list (auth failure, 5xx) used to lose its status line and read as an empty list. It now ends
# with its own list_http_<code> line, and no_target_models is left for a 2xx list with zero targets.
@pytest.mark.parametrize("code,body", [("401", {"detail": "unauthorized"}), ("500", {"detail": "boom"})])
def test_model_refresh_non_2xx_list_is_list_http_not_no_target_models(tmp_path, code, body):
    box = model_refresh_box(tmp_path, body)
    box.set_routes([{"match": ["/mental-models?"], "out": json.dumps(body), "code": code}])
    r = box.run("scripts/model-refresh.sh", HC_PERSONAL_BANK="bank-x", HC_OWNER="demo")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "MODEL_REFRESH_FAIL - list_http_%s" % code in r.stdout
    assert "no_target_models" not in r.stdout
    # nothing past the list call: no clear or refresh is attempted on a failed list
    assert [c for c in box.calls("curl") if "/mental-models?" not in c["argv"][-1]] == []


def test_model_refresh_2xx_list_with_zero_targets_stays_no_target_models(tmp_path):
    box = model_refresh_box(tmp_path, {"items": []})
    r = box.run("scripts/model-refresh.sh", HC_PERSONAL_BANK="bank-x", HC_OWNER="demo")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "MODEL_REFRESH_FAIL - no_target_models" in r.stdout
    assert "list_http_" not in r.stdout


# --------------------------------------------------------------------------- contract-probe.sh

def need_jq():
    if not shutil.which("jq"):
        pytest.skip("needs jq on the host")


def contract_box(tmp_path):
    need_jq()
    box = Sandbox(tmp_path, ("contract-probe.sh",))
    box.set_routes([{"match": ["/mcp/probe/"], "out": 'data: {"result": {"tools": [{"name": "retain"}]}}\n'}])
    return box


def test_contract_probe_sends_the_key_on_stdin_never_in_argv(tmp_path):
    box = contract_box(tmp_path)
    r = box.run("scripts/contract-probe.sh", HC_HINDSIGHT_IMAGE="repo/img@sha256:" + "a" * 64,
                HC_HINDSIGHT_API_KEY=KEY)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (box.root / "fixtures" / "contract-probe.out.json").is_file()
    curls = box.calls("curl")
    assert len(curls) >= 6
    assert_no_secret_in_argv(box)
    for c in curls:
        assert c["argv"][c["argv"].index("-H") + 1] == "@-"
        assert "Authorization: Bearer %s" % KEY in c["stdin"]
        assert "-X" in c["argv"] and c["argv"][c["argv"].index("-X") + 1] == "POST"
        assert any(a.startswith("http://127.0.0.1:8899/mcp/") for a in c["argv"])
    assert KEY not in r.stdout + r.stderr


def test_contract_probe_without_a_key_sends_no_authorization_header(tmp_path):
    box = contract_box(tmp_path)
    r = box.run("scripts/contract-probe.sh", HC_HINDSIGHT_IMAGE="repo/img@sha256:" + "a" * 64)
    assert r.returncode == 0, r.stdout + r.stderr
    for c in box.calls("curl"):
        assert "authorization" not in c["stdin"].lower()
        assert "authorization" not in " ".join(c["argv"]).lower()


# --------------------------------------------------------------------------- mcp-probe.sh

SSE_HEAD = "HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n\r\n"


def sse_frame(result):
    return 'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":%s}\n\n' % json.dumps(result, separators=(",", ":"))


def reflect_result(based_on):
    return {"content": [{"type": "text", "text": json.dumps({"text": "t", "based_on": based_on})}], "isError": False}


def probe_routes(*first):
    """#247: main() captures the regression cases with `-i` (headers + body split), so every captured call needs a header block
    and the bodies the capture asserts on (completed seed, \\uXXXX in get_memory, a populated based_on in the include_based_on
    reflect, a success-framed error for the bad timestamp). `first` routes win (first match)."""
    return list(first) + [
        {"match": ["-i", "initialize"], "hdr": "HTTP/1.1 200 OK\r\nmcp-session-id: sess-1\r\n\r\n", "out": "{}"},
        {"match": ["tools/list"], "out": '{"result": {"tools": []}}'},
        {"match": ["-i", "memories/recall"], "hdr": "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n\r\n",
         "out": '{"results":[]}'},
        {"match": ["-i", "get_memory"], "hdr": SSE_HEAD, "out": sse_frame({"content": [{"type": "text", "text": "\\ud55c"}]})},
        {"match": ["-i", "probe-seed"], "hdr": SSE_HEAD, "out": sse_frame({
            "structuredContent": {"status": "completed", "memory_ids": ["m-1"]}, "isError": False})},
        {"match": ["-i", "include_based_on"], "hdr": SSE_HEAD, "out": sse_frame(reflect_result({"world": [{"id": "f1"}]}))},
        {"match": ["-i", "reflect"], "hdr": SSE_HEAD, "out": sse_frame(reflect_result(None))},
        # the success-framed failure carries NO isError key: it still counts as success-shaped
        {"match": ["-i", "not-a-timestamp"], "hdr": SSE_HEAD,
         "out": sse_frame({"content": [], "structuredContent": {"status": "error", "message": "bad"}})},
        {"match": ["-i"], "hdr": SSE_HEAD, "out": sse_frame({"content": [], "isError": False})},
    ]


def probe_box(tmp_path, routes):
    need_jq()
    box = Sandbox(tmp_path, ("mcp-probe.sh",))
    (box.root / "docker-compose.yml").write_text("services: {}\n")
    box.set_routes(routes)
    return box


def test_mcp_probe_hands_the_key_to_the_container_on_stdin_never_in_argv(tmp_path):
    box = probe_box(tmp_path, probe_routes())
    r = box.run("scripts/mcp-probe.sh", HC_HINDSIGHT_API_KEY=KEY)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (box.root / "fixtures" / "mcp" / "tools_list.raw").is_file()
    assert (box.root / "fixtures" / "mcp" / "reflect_based_on.headers").is_file()
    assert (box.root / "fixtures" / "rest" / "recall_gate.json").is_file()
    # a frame without isError but with status:error is still the success-shaped failure: kept, not "UNAVAILABLE"
    assert (box.root / "fixtures" / "mcp" / "sync_retain_error_frame.raw").is_file()
    assert not (box.root / "fixtures" / "mcp" / "sync_retain_error_frame.UNAVAILABLE.md").exists()
    # the stub docker ran `python -c '<fixed source>' <curl args>` locally, so docker and python argv were recorded
    execs = [c for c in box.calls("docker") if "exec" in c["argv"] and "python" in c["argv"]]
    pys = box.calls("python")
    assert execs and pys
    assert len(execs) == len(pys)
    assert not box.calls("curl") and not box.calls("sh")          # no curl (0.10.0+ images have none), no shell hop
    assert_no_secret_in_argv(box)
    for c in pys:
        assert c["argv"][0] == "-c" and "urllib.request" in c["argv"][1]
        assert c["argv"][c["argv"].index("-H") + 1] == "@-"
        assert "Authorization: Bearer %s" % KEY in c["stdin"]
        assert c["stdin"].count("\n") == 1 and c["stdin"].endswith("\n")   # the channel carries the key line only
    for e in execs:   # an `-e NAME` is fine, an `-e NAME=value` is not
        for i, a in enumerate(e["argv"]):
            if a in ("-e", "--env"):
                assert "=" not in e["argv"][i + 1]
    assert KEY not in r.stdout + r.stderr


PRE_EXISTING = b"PRE-EXISTING FIXTURE BYTES\n"


def probe_with_pre_existing(tmp_path, routes, mcp_names=(), rest_names=()):
    """Run the probe where `fixtures/` already holds committed files; returns (box, result)."""
    box = probe_box(tmp_path, routes)
    for sub, names, ext in (("mcp", mcp_names, ".raw"), ("rest", rest_names, ".json")):
        d = box.root / "fixtures" / sub
        d.mkdir(parents=True, exist_ok=True)
        for name in names:
            (d / (name + ext)).write_bytes(PRE_EXISTING)
    return box, box.run("scripts/mcp-probe.sh", HC_HINDSIGHT_API_KEY=KEY)


@pytest.mark.parametrize("status", ["500 Internal Server Error", "404 Not Found", "302 Found"])
def test_mcp_probe_does_not_pin_a_non_2xx_mcp_response(tmp_path, status):
    """An HTTP error body must never become the baseline: the probe stops and the committed fixture stays as it was."""
    bad = {"match": ["-i", "reflect"], "hdr": "HTTP/1.1 %s\r\ncontent-type: text/plain\r\n\r\n" % status, "out": "boom"}
    box, r = probe_with_pre_existing(tmp_path, probe_routes(bad), mcp_names=("reflect_default",))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "reflect_default returned HTTP %s" % status.split()[0] in r.stderr
    assert (box.root / "fixtures" / "mcp" / "reflect_default.raw").read_bytes() == PRE_EXISTING
    assert not (box.root / "fixtures" / "mcp" / "reflect_default.headers").exists()


@pytest.mark.parametrize("hdr,out", [
    ("HTTP/1.1 500 Internal Server Error\r\ncontent-type: text/plain\r\n\r\n", "Internal Server Error"),
    ("HTTP/1.1 200 OK\r\ncontent-type: text/plain\r\n\r\n", "not json at all"),
    ("HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n\r\n", ""),
])
def test_mcp_probe_keeps_the_rest_recall_fixture_when_the_response_is_unusable(tmp_path, hdr, out):
    bad = {"match": ["-i", "memories/recall"], "hdr": hdr, "out": out}
    box, r = probe_with_pre_existing(tmp_path, probe_routes(bad), rest_names=("recall_gate",))
    assert r.returncode == 1, r.stdout + r.stderr
    assert (box.root / "fixtures" / "rest" / "recall_gate.json").read_bytes() == PRE_EXISTING
    assert not (box.root / "fixtures" / "rest" / "recall_gate.json.tmp").exists()


def test_mcp_probe_keeps_the_intentional_failure_captures(tmp_path):
    """sync_retain_fail is an expected failure (isError in the body, HTTP 200): it is captured, not refused."""
    fail = {"match": ["-i", '"arguments":{"tags":["kind:probe"]}'], "hdr": SSE_HEAD,
            "out": sse_frame({"content": [{"type": "text", "text": "1 validation error"}], "isError": True})}
    box = probe_box(tmp_path, probe_routes(fail))
    r = box.run("scripts/mcp-probe.sh", HC_HINDSIGHT_API_KEY=KEY)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (box.root / "fixtures" / "mcp" / "sync_retain_fail.raw").is_file()
    assert (box.root / "fixtures" / "mcp" / "sync_retain_fail.headers").is_file()
    assert not (box.root / "fixtures" / "mcp" / "sync_retain_fail.UNAVAILABLE.md").exists()


@pytest.mark.parametrize("based_on", [None, {}, [], {"world": []}], ids=["null", "empty-object", "empty-list", "empty-values"])
def test_mcp_probe_refuses_a_reflect_original_without_a_populated_based_on(tmp_path, based_on):
    """The strip test is vacuous unless the include_based_on original really carries a based_on value."""
    weak = {"match": ["-i", "include_based_on"], "hdr": SSE_HEAD, "out": sse_frame(reflect_result(based_on))}
    box = probe_box(tmp_path, probe_routes(weak))
    r = box.run("scripts/mcp-probe.sh", HC_HINDSIGHT_API_KEY=KEY)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "based_on" in r.stderr


def test_mcp_probe_keeps_the_transport_interface_verify_lib_relies_on(tmp_path):
    """verify/lib.sh sources mcp-probe.sh and swaps HC_MCP_CURL: mcp_post must still call it as
    `<curl-fn> -X POST <base+path> <-H pairs> -d <body> <extra args>` and print what it returns verbatim."""
    box = Sandbox(tmp_path, ("mcp-probe.sh",))
    snippet = r'''
. "$1/scripts/mcp-probe.sh"
fake_curl() { printf 'ARGV:'; printf ' [%s]' "$@"; printf '\n'; }
HC_MCP_CURL=fake_curl HC_MCP_BASE=http://gw HC_MCP_PATH=/mcp/b/ HC_MCP_BANK_HEADER="" HC_MCP_SESSION=s1
mcp_post '{"a":1}' -w '\n%{http_code}'
'''
    r = subprocess.run(["/bin/bash", "-c", snippet, "_", str(box.root)], env=box.env(HC_HINDSIGHT_API_KEY=KEY),
                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == ("ARGV: [-X] [POST] [http://gw/mcp/b/] [-H] [Content-Type: application/json] "
                                "[-H] [Accept: application/json, text/event-stream] [-H] [Mcp-Session-Id: s1] "
                                "[-d] [{\"a\":1}] [-w] [\\n%{http_code}]")


# --------------------------------------------------------------------------- restore-test.sh

def restore_box(tmp_path, **docker_routes):
    box = Sandbox(tmp_path, ("restore-test.sh", "bank_stats.py"))
    (box.root / ".env.local").write_text("HC_HINDSIGHT_API_KEY=%s\n" % KEY)
    backups = box.root / "backups" / "b1"
    backups.mkdir(parents=True)
    (backups / "pg0.tar.gz").write_bytes(b"not a real archive")
    (backups / "stats.json").write_text("{}")
    box.set_routes([
        {"match": ["exec -i"], "out": "{}"},                          # docker exec -i <c> python3 -c <bank_stats>
        {"match": ["memories/recall"], "out": '{"results": ["marker-xyz"]}'},   # curl recall
    ])
    return box


def test_restore_test_keeps_the_key_out_of_curl_and_docker_argv(tmp_path):
    box = restore_box(tmp_path)
    r = box.run("scripts/restore-test.sh", str(box.root / "backups" / "b1" / "pg0.tar.gz"),
                HC_RESTORE_MARKER="marker-xyz")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "restore-test ok" in r.stdout
    assert_no_secret_in_argv(box)
    dockers = box.calls("docker")
    run_d = [c for c in dockers if c["argv"][:2] == ["run", "-d"]]
    assert len(run_d) == 1
    # the throwaway container still receives the key (and the model defaults) through its environment
    assert run_d[0]["env"]["HINDSIGHT_API_MCP_AUTH_TOKEN"] is True
    assert run_d[0]["envval"] == {"HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL": "BAAI/bge-m3",
                                  "HINDSIGHT_API_RERANKER_LOCAL_MODEL": "BAAI/bge-reranker-v2-m3"}
    assert "HINDSIGHT_API_LLM_PROVIDER=none" in run_d[0]["argv"]
    for name in ("HINDSIGHT_API_MCP_AUTH_TOKEN", "HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL",
                 "HINDSIGHT_API_RERANKER_LOCAL_MODEL"):
        assert name in run_d[0]["argv"]            # name-only `-e NAME`
    # the recall call carries the header on stdin
    recall = [c for c in box.calls("curl") if any("memories/recall" in a for a in c["argv"])]
    assert len(recall) == 1
    assert recall[0]["argv"][recall[0]["argv"].index("-X") + 1] == "POST"
    assert "@-" in recall[0]["argv"]
    assert "Authorization: Bearer %s" % KEY in recall[0]["stdin"] or "authorization: Bearer %s" % KEY in recall[0]["stdin"]
    assert KEY not in r.stdout + r.stderr


def test_restore_test_model_overrides_still_reach_the_container(tmp_path):
    box = restore_box(tmp_path)
    r = box.run("scripts/restore-test.sh", str(box.root / "backups" / "b1" / "pg0.tar.gz"),
                HC_RESTORE_MARKER="marker-xyz", HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL="emb/override",
                HINDSIGHT_API_RERANKER_LOCAL_MODEL="rr/override")
    assert r.returncode == 0, r.stdout + r.stderr
    run_d = [c for c in box.calls("docker") if c["argv"][:2] == ["run", "-d"]][0]
    assert run_d["envval"] == {"HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL": "emb/override",
                               "HINDSIGHT_API_RERANKER_LOCAL_MODEL": "rr/override"}
    assert_no_secret_in_argv(box)


# --------------------------------------------------------------------------- the static guard from the issue

@pytest.mark.parametrize("pattern, paths", [
    (r'-H "authorization|authorization: bearer \$', ("scripts/*.sh", "rerank-server/*.sh", "install", "bootstrap")),
    (r'-e [A-Z_]+="\$', ("scripts/*.sh",)),
])
def test_no_credential_bearing_argv_lines_remain(pattern, paths):
    flags = ["-n", "-i", "-E"] if "authorization" in pattern else ["-n", "-E"]
    args = ["git", "grep", *flags, "--", pattern, "--", *paths]
    r = subprocess.run(args, cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    assert r.returncode == 1 and r.stdout == "", r.stdout
