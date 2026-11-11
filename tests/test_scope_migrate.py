"""scripts/scope-migrate.py moves a bank's documents to new observation scopes and watches the rebuild drain (issue #298).

Hermetic, same harness as tests/test_bank_config.py: the script runs from a scratch copy of the repo, PATH holds a fake
`docker` whose `exec -i <c> python3 -c <script>` runs the REAL in-container script against a mock upstream HTTP server
(only the base URL differs), so the document listing, PATCH bodies and stats reads the mock sees are what the real
server would see. No real docker, no real bank. The API key only travels on stdin.

Completion criteria of #298: (a) the listing goes to the last page, (b) a production target without --allow-prod is
exit 1 with zero PATCH, (c) dry-run sends zero PATCH, (d) a drain whose five operation states do not add up to the
total fails, (e) the batch size holds, (f) a list of lists and the string `combined` both reach the server verbatim.
"""
import json
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
KEY = "test-api-key-5c1e"
BANK = "demo-bank"
DOCS_PATH = "/v1/default/banks/%s/documents" % BANK

NEXT = {"HC_TARGET": "next", "HC_COMPOSE_PROJECT": "hc-next", "HC_HINDSIGHT_CONTAINER": "hc-next-hindsight-1",
        "HC_GATEWAY_BIND": "127.0.0.1:18888", "HC_PG0_VOLUME": "hc-next_pg0"}
PROD = {"HC_COMPOSE_PROJECT": "hindsight-crew", "HC_HINDSIGHT_CONTAINER": "hindsight-crew-hindsight-1",
        "HC_GATEWAY_BIND": "127.0.0.1:8888", "HC_PG0_VOLUME": "hindsight-crew_pg0"}
# copy-shaped cells with HC_TARGET unset: refused like production, so a forgotten `eval "$next_env"` cannot slip by
LOOKS_LIKE_COPY = {"HC_COMPOSE_PROJECT": "hc-next", "HC_HINDSIGHT_CONTAINER": "hc-next-hindsight-1",
                   "HC_GATEWAY_BIND": "127.0.0.1:18888", "HC_PG0_VOLUME": "hc-next_pg0"}
TARGETS = {"next": NEXT, "prod": PROD, "copy-shaped": LOOKS_LIKE_COPY}

FAKE_DOCKER = r'''#!%(python)s
import json, os, subprocess, sys

argv = sys.argv[1:]


def record(**extra):
    with open(os.environ["FAKE_DOCKER_LOG"], "a") as fh:
        fh.write(json.dumps(dict(argv=argv, **extra)) + "\n")


if argv[:1] == ["compose"] and "ps" in argv:
    record()
    print("cid-1")
    sys.exit(0)
if argv[:1] == ["inspect"]:
    record()
    print("/" + os.environ["FAKE_CONTAINER_NAME"])
    sys.exit(0)
if argv[:1] == ["exec"]:
    i = argv.index("python3")
    payload = json.loads(sys.stdin.read())
    record(container=argv[i - 1], key_ok=payload.get("key") == os.environ["FAKE_KEY"],
           request={k: v for k, v in payload.items() if k != "key"})
    payload["base"] = os.environ["FAKE_UPSTREAM"]
    p = subprocess.run([sys.executable, "-c", argv[i + 2]], input=json.dumps(payload), capture_output=True, text=True)
    sys.stdout.write(p.stdout)
    sys.stderr.write(p.stderr)
    sys.exit(p.returncode)
record(unknown=True)
sys.exit(99)
'''

FIVE = ("pending", "processing", "completed", "failed", "cancelled")


def ops(pending=0, processing=0, completed=10, failed=0, cancelled=0, **extra):
    d = {"pending": pending, "processing": processing, "completed": completed, "failed": failed,
         "cancelled": cancelled}
    d.update(extra)
    return d


def state(by_status, total=None, observations=100, pending_consolidation=0, llm=0, llm_consolidation=0, rows=None,
          obs_tags=(), obs_status=200, obs_delay=0.0):
    """One drain poll as the server answers it. `total` defaults to the honest sum of the five states.

    `rows` counts llm-requests rows by (operation, provider); without it, `llm_consolidation` rows are generative
    consolidation calls and the rest of `llm` are generative calls of another operation. `obs_tags` are the tag lists of
    the observations `memories/list?type=observation` serves."""
    if rows is None:
        rows = {("consolidation", "claude-code"): llm_consolidation, ("reflect", "claude-code"): llm - llm_consolidation}
    return {"by_status": by_status, "total": sum(by_status.get(k, 0) for k in FIVE) if total is None else total,
            "observations": observations, "pending_consolidation": pending_consolidation, "rows": rows,
            "obs_tags": [list(t) for t in obs_tags], "obs_status": obs_status, "obs_delay": obs_delay}


class Upstream:
    """Mock of list/PATCH documents, bank stats, the operations list and llm-requests.

    `docs` are document ids served newest first in pages of the caller's `limit`; `lie_total` overrides the `total`
    the listing reports. `states` is the drain script: each stats read moves one step, the last one sticks."""

    def __init__(self, docs=(), lie_total=None, states=None):
        self.docs, self.lie_total = list(docs), lie_total
        self.states, self.step = list(states or [state(ops())]), -1
        self.requests = []          # (method, path, body, monotonic time)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, status, obj):
                raw = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _serve(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                owner.requests.append((method, self.path, body, time.monotonic()))
                if self.headers.get("Authorization") != "Bearer " + KEY:
                    return self._reply(401, {"detail": "bad key"})
                url = urllib.parse.urlsplit(self.path)
                q = urllib.parse.parse_qs(url.query)
                base = "/v1/default/banks/%s" % BANK
                if method == "GET" and url.path == base + "/documents":
                    limit, offset = int(q.get("limit", ["100"])[0]), int(q.get("offset", ["0"])[0])
                    items = [{"id": d, "bank_id": BANK, "tags": []} for d in owner.docs[offset:offset + limit]]
                    total = len(owner.docs) if owner.lie_total is None else owner.lie_total
                    return self._reply(200, {"items": items, "total": total, "limit": limit, "offset": offset})
                if method == "PATCH" and url.path.startswith(base + "/documents/"):
                    return self._reply(200, {"success": True})
                cur = owner.states[min(max(owner.step, 0), len(owner.states) - 1)]
                if method == "GET" and url.path == base + "/stats":
                    owner.step += 1
                    cur = owner.states[min(owner.step, len(owner.states) - 1)]
                    return self._reply(200, {"bank_id": BANK, "total_observations": cur["observations"],
                                             "pending_consolidation": cur["pending_consolidation"],
                                             "operations_by_status": cur["by_status"],
                                             "pending_operations": cur["by_status"].get("pending", 0)})
                if method == "GET" and url.path == base + "/operations":
                    return self._reply(200, {"bank_id": BANK, "total": cur["total"], "limit": 1, "offset": 0,
                                             "operations": []})
                if method == "GET" and url.path == base + "/llm-requests":
                    rows = [{"id": "%s-%s-%d" % (op, prov, i), "operation": op, "provider": prov}
                            for (op, prov), n in sorted(cur["rows"].items()) for i in range(n)
                            if q.get("operation", [op]) == [op] and q.get("provider", [prov]) == [prov]]
                    limit, offset = int(q.get("limit", ["50"])[0]), int(q.get("offset", ["0"])[0])
                    return self._reply(200, {"items": rows[offset:offset + limit], "total": len(rows), "limit": limit,
                                             "offset": offset})
                if method == "GET" and url.path == base + "/memories/list" and q.get("type") == ["observation"]:
                    time.sleep(cur["obs_delay"])
                    if cur["obs_status"] != 200:
                        return self._reply(cur["obs_status"], {"detail": "listing failed"})
                    obs = [{"id": "obs-%d" % i, "tags": t} for i, t in enumerate(cur["obs_tags"])]
                    limit, offset = int(q.get("limit", ["100"])[0]), int(q.get("offset", ["0"])[0])
                    return self._reply(200, {"items": obs[offset:offset + limit], "total": len(obs), "limit": limit,
                                             "offset": offset})
                self._reply(404, {"detail": "no such path"})

            def do_GET(self):
                self._serve("GET")

            def do_PATCH(self):
                self._serve("PATCH")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def patches(self):
        return [r for r in self.requests if r[0] == "PATCH"]

    def patched_ids(self):
        return [urllib.parse.unquote(urllib.parse.urlsplit(r[1]).path.rsplit("/", 1)[1]) for r in self.patches()]

    def listings(self):
        return [urllib.parse.parse_qs(urllib.parse.urlsplit(r[1]).query) for r in self.requests
                if r[0] == "GET" and urllib.parse.urlsplit(r[1]).path == DOCS_PATH]


class Box:
    def __init__(self, tmp_path):
        self.root = tmp_path / "repo"
        (self.root / "scripts").mkdir(parents=True)
        for name in ("scope-migrate.py", "bank-config.py", "hc_host_env.py"):
            if (REPO / "scripts" / name).exists():          # scope-migrate.py is absent in the red run
                shutil.copy(REPO / "scripts" / name, self.root / "scripts" / name)
        self.env_file = tmp_path / "env"
        self.env_file.write_text("HC_HINDSIGHT_API_KEY=%s\n" % KEY)
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        fake = self.bin / "docker"
        fake.write_text(FAKE_DOCKER % {"python": sys.executable})
        fake.chmod(0o755)
        self.log = tmp_path / "docker.jsonl"
        self.log.write_text("")
        self.tmp = tmp_path

    def run(self, upstream, *args, target="next", bank=BANK, timeout=60, env=None):
        full = {"PATH": "%s:/usr/bin:/bin" % self.bin, "HOME": str(self.root), "HC_ENV_FILE": str(self.env_file),
                "FAKE_DOCKER_LOG": str(self.log), "FAKE_UPSTREAM": upstream.url, "FAKE_KEY": KEY,
                "FAKE_CONTAINER_NAME": "ctr", "SCOPE_MIGRATE_RECHECK_SLEEP": "0"}
        full.update(TARGETS[target])
        full.update(env or {})
        cmd = [sys.executable, str(self.root / "scripts" / "scope-migrate.py")] + list(args)
        if bank is not None:
            cmd += ["--bank", bank]
        return subprocess.run(cmd, env=full, capture_output=True, text=True, timeout=timeout)

    def docker_calls(self):
        return [json.loads(ln) for ln in self.log.read_text().splitlines() if ln.strip()]

    def execs(self):
        return [c for c in self.docker_calls() if c["argv"][:1] == ["exec"]]


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


@pytest.fixture
def make_upstream():
    made = []

    def make(**kw):
        up = Upstream(**kw)
        made.append(up)
        return up

    yield make
    for up in made:
        up.close()


def assert_no_secret(box, r):
    assert KEY not in r.stdout and KEY not in r.stderr
    for call in box.docker_calls():
        assert KEY not in json.dumps(call["argv"])
    assert all(c["key_ok"] for c in box.execs())


def doc_ids(n):
    return ["doc-%04d" % i for i in range(n)]


SCOPE = '[["project:*"]]'


# ---------------------------------------------------------------- (a) the listing goes to the last page


@pytest.mark.parametrize("n,page", [(0, 100), (1, 100), (100, 100), (250, 100), (7, 3)])
@pytest.mark.parametrize("mode", ["dry-run", "apply"])
def test_a_lists_every_page(box, make_upstream, n, page, mode):
    up = make_upstream(docs=doc_ids(n))
    extra = ["--apply", "--batch-size", "1000"] if mode == "apply" else []
    r = box.run(up, "migrate", "--scopes", SCOPE, "--page-size", str(page), *extra)
    assert r.returncode == 0, r.stderr
    offsets = [int(q["offset"][0]) for q in up.listings()]
    assert offsets == list(range(0, max(n, 1), page)) if n else offsets == [0]
    assert all(int(q["limit"][0]) == page for q in up.listings())
    assert "targets: %d" % n in r.stdout
    if mode == "apply":
        assert sorted(up.patched_ids()) == doc_ids(n)
    else:
        assert up.patches() == []
    assert_no_secret(box, r)


@pytest.mark.parametrize("lie_total", [10, 4])
def test_a_listing_that_disagrees_with_total_fails_closed(box, make_upstream, lie_total):
    """total says 10 but 7 rows exist (or the reverse): the target set is unknown, so nothing is sent."""
    up = make_upstream(docs=doc_ids(7), lie_total=lie_total)
    r = box.run(up, "migrate", "--scopes", SCOPE, "--page-size", "3", "--apply")
    assert r.returncode == 1
    assert up.patches() == []
    assert "total" in r.stderr


def test_a_tag_filter_reaches_the_listing(box, make_upstream):
    up = make_upstream(docs=doc_ids(2))
    r = box.run(up, "migrate", "--scopes", SCOPE, "--tag", "project:x", "--tag", "kind:y", "--tags-match", "any")
    assert r.returncode == 0, r.stderr
    q = up.listings()[0]
    assert q["tags"] == ["project:x", "kind:y"] and q["tags_match"] == ["any"]


def test_a_document_ids_are_quoted_in_the_patch_path(box, make_upstream):
    up = make_upstream(docs=["a/b c?d", "plain"])
    r = box.run(up, "migrate", "--scopes", SCOPE, "--apply")
    assert r.returncode == 0, r.stderr
    assert sorted(up.patched_ids()) == ["a/b c?d", "plain"]
    assert all(r_[1].count("/") == DOCS_PATH.count("/") + 1 for r_ in up.patches())


# ---------------------------------------------------------------- (b) production needs --allow-prod


COMMANDS = {
    "migrate-dry-run": ["migrate", "--scopes", SCOPE],
    "migrate-apply": ["migrate", "--scopes", SCOPE, "--apply"],
    "migrate-combined": ["migrate", "--scopes", "combined", "--apply"],
    "drain": ["drain", "--interval", "0.01", "--timeout", "5"],
}


@pytest.mark.parametrize("target", ["prod", "copy-shaped"])
@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_b_prod_without_allow_prod_is_exit_1_and_sends_nothing(box, make_upstream, target, command):
    up = make_upstream(docs=doc_ids(5))
    r = box.run(up, *COMMANDS[command], target=target)
    assert r.returncode == 1
    assert up.patches() == []
    assert up.requests == []                          # refused before the first request, not after a listing
    assert box.execs() == []
    assert len([ln for ln in r.stderr.splitlines() if ln.startswith("target:")]) == 1
    assert_no_secret(box, r)


def test_b_allow_prod_opens_production(box, make_upstream):
    up = make_upstream(docs=doc_ids(3))
    r = box.run(up, "migrate", "--scopes", "combined", "--apply", "--allow-prod", target="prod")
    assert r.returncode == 0, r.stderr
    assert sorted(up.patched_ids()) == doc_ids(3)
    assert {c["container"] for c in box.execs()} == {PROD["HC_HINDSIGHT_CONTAINER"]}


def test_b_copy_target_reaches_the_copy_container(box, make_upstream):
    up = make_upstream(docs=doc_ids(1))
    r = box.run(up, "migrate", "--scopes", SCOPE, "--apply")
    assert r.returncode == 0, r.stderr
    assert {c["container"] for c in box.execs()} == {NEXT["HC_HINDSIGHT_CONTAINER"]}
    assert [ln for ln in r.stderr.splitlines() if ln.startswith("target:")] == [
        "target: gw=127.0.0.1:18888 project=hc-next container=hc-next-hindsight-1 pg0=hc-next_pg0"]


# ---------------------------------------------------------------- (c) dry-run sends no PATCH


@pytest.mark.parametrize("flags", [[], ["--dry-run"]], ids=["default", "explicit"])
@pytest.mark.parametrize("scopes", [SCOPE, "combined"])
def test_c_dry_run_counts_only(box, make_upstream, flags, scopes):
    up = make_upstream(docs=doc_ids(12))
    r = box.run(up, "migrate", "--scopes", scopes, "--batch-size", "5", *flags)
    assert r.returncode == 0, r.stderr
    assert up.patches() == []
    assert {m for m, *_ in up.requests} == {"GET"}
    assert "targets: 12" in r.stdout
    assert "dry-run" in r.stdout


def test_c_dry_run_and_apply_are_exclusive(box, make_upstream):
    up = make_upstream(docs=doc_ids(2))
    r = box.run(up, "migrate", "--scopes", SCOPE, "--dry-run", "--apply")
    assert r.returncode == 2 and "not allowed with" in r.stderr
    assert up.requests == []


# ---------------------------------------------------------------- (d) drain is fail-closed


def drain(box, up, *extra, timeseries=True, env=None):
    args = ["drain", "--interval", "0.01", "--timeout", "10"]
    if timeseries:
        args += ["--timeseries", str(box.tmp / "drain.jsonl")]
    return box.run(up, *args, *extra, env=env)


def rows(box):
    return [json.loads(ln) for ln in (box.tmp / "drain.jsonl").read_text().splitlines() if ln.strip()]


@pytest.mark.parametrize("bad", [
    state(ops(), total=11),                                   # one operation missing from the five states
    state(ops(), total=9),                                    # one too many
    state(ops(completed=9, queued=1), total=10),              # a sixth state the watcher does not know
    state({"pending": 0, "processing": 0, "completed": 10}, total=12),
], ids=["sum-below-total", "sum-above-total", "unknown-state", "missing-states"])
@pytest.mark.parametrize("when", ["first-poll", "mid-drain"])
def test_d_drain_sum_mismatch_fails(box, make_upstream, bad, when):
    states = [bad] if when == "first-poll" else [state(ops(pending=3, processing=1), pending_consolidation=5), bad]
    up = make_upstream(states=states)
    r = drain(box, up)
    assert r.returncode == 1
    assert "total" in r.stderr
    assert up.patches() == []


def test_d_drain_sum_mismatch_rechecked_within_the_poll(box, make_upstream):
    # stats and the operations list are read apart: a page refresh queued in between makes one read disagree.
    # The poll reads both again and goes on with the first agreeing pair (rework #300-1)
    states = [state(ops(pending=1, completed=9), total=12, pending_consolidation=2),
              state(ops(completed=10), llm=1, llm_consolidation=1)]
    up = make_upstream(states=states)
    r = drain(box, up)
    assert r.returncode == 0, r.stderr
    got = rows(box)
    assert len(got) == 1
    assert got[0]["operations_total"] == 10 and got[0]["operations"]["completed"] == 10
    assert sum(1 for m, p, *_ in up.requests if urllib.parse.urlsplit(p).path.endswith("/stats")) == 2


def test_d_drain_sum_mismatch_on_every_recheck_fails_closed(box, make_upstream):
    up = make_upstream(states=[state(ops(), total=12)])
    r = drain(box, up)
    assert r.returncode == 1
    assert "the five operation states add up to 10 but the operations total is 12 (fail-closed)" in r.stderr
    assert len(rows(box)) == 1
    # the first read and the 3 re-reads: a poll without rechecks would read stats once
    assert sum(1 for m, p, *_ in up.requests if urllib.parse.urlsplit(p).path.endswith("/stats")) == 4


@pytest.mark.parametrize("value", ["-1", "nan", "inf", "2s"])
def test_d_drain_refuses_a_bad_recheck_sleep_before_any_request(box, make_upstream, value):
    up = make_upstream(states=[state(ops(), total=12)])
    r = drain(box, up, env={"SCOPE_MIGRATE_RECHECK_SLEEP": value})
    assert r.returncode == 1
    assert "SCOPE_MIGRATE_RECHECK_SLEEP=%r is not a finite number of seconds >= 0; nothing was sent" % value in r.stderr
    assert up.requests == []


def test_c_migrate_ignores_the_recheck_sleep(box, make_upstream):
    up = make_upstream(docs=doc_ids(2))
    r = box.run(up, "migrate", "--scopes", SCOPE, "--apply", env={"SCOPE_MIGRATE_RECHECK_SLEEP": "2s"})
    assert r.returncode == 0, r.stderr
    assert sorted(up.patched_ids()) == doc_ids(2)


def test_d_drain_waits_for_pending_and_processing_and_records_a_timeseries(box, make_upstream):
    states = [
        state(ops(pending=4, processing=1, completed=5), observations=80, pending_consolidation=40, llm=3,
              llm_consolidation=2),
        state(ops(pending=1, processing=1, completed=8), observations=90, pending_consolidation=10, llm=9,
              llm_consolidation=7),
        state(ops(completed=10), observations=95, pending_consolidation=0, llm=12, llm_consolidation=10),
    ]
    up = make_upstream(states=states)
    r = drain(box, up)
    assert r.returncode == 0, r.stderr
    got = rows(box)
    assert [g["observations"] for g in got] == [80, 90, 95]
    assert [g["llm_requests"] for g in got] == [3, 9, 12]
    assert [g["llm_consolidation"] for g in got] == [2, 7, 10]
    assert [g["operations"]["pending"] for g in got] == [4, 1, 0]
    assert all(g["operations_total"] == 10 and "at" in g for g in got)
    assert [json.loads(ln) for ln in r.stdout.splitlines() if ln.startswith("{")] == got
    llm = [urllib.parse.parse_qs(urllib.parse.urlsplit(p).query) for m, p, *_ in up.requests if "llm-requests" in p]
    assert llm and all("start_date" in q for q in llm)


@pytest.mark.parametrize("last", [
    state(ops(pending=1, completed=9)),
    state(ops(processing=1, completed=9)),
    state(ops(), pending_consolidation=3),
], ids=["pending", "processing", "pending-consolidation"])
def test_d_drain_timeout_is_not_success(box, make_upstream, last):
    up = make_upstream(states=[last])
    r = box.run(up, "drain", "--interval", "0.05", "--timeout", "0.3")
    assert r.returncode == 2
    assert "drain: timeout" in r.stderr


def test_d_drain_count_all_stops_at_the_total_llm_cap(box, make_upstream):
    """--count-all keeps the old rule: the cap is on every llm-requests row."""
    up = make_upstream(states=[state(ops(pending=2, completed=8), llm=1999), state(ops(pending=1, completed=9),
                                                                                    llm=2000)])
    r = drain(box, up, "--max-llm", "2000", "--count-all")
    assert r.returncode == 3
    assert "2000" in r.stderr
    assert rows(box)[-1]["llm_requests"] == 2000


def mixed(generative, embedding, rerank=0, other=0):
    """llm-requests rows as a reconsolidation produces them: generative consolidation calls (claude-code), the local
    embedding rows of the same operation (one query + one document per call), reranks and other generative calls."""
    return {("consolidation", "claude-code"): generative, ("consolidation", "local"): embedding,
            ("refresh_mental_model", "tei"): rerank, ("refresh_mental_model", "claude-code"): other}


def test_d_drain_cap_counts_generative_consolidation_only(box, make_upstream):
    """#296 decision B: local embedding rows, reranks and other operations never move the cap."""
    up = make_upstream(states=[
        state(ops(pending=2, completed=8), rows=mixed(1999, 3998, rerank=50, other=40)),   # 6087 rows in all
        state(ops(pending=2, completed=8), rows=mixed(1999, 5000, rerank=90, other=80)),   # embeddings alone grow
        state(ops(pending=1, completed=9), rows=mixed(2000, 5002, rerank=90, other=80)),
    ])
    r = drain(box, up, "--max-llm", "2000")
    assert r.returncode == 3, r.stderr
    got = rows(box)
    assert len(got) == 3                                  # the first two polls stay under the cap despite > 2000 rows
    assert [g["llm_consolidation_generative"] for g in got] == [1999, 1999, 2000]
    assert [g["llm_embedding"] for g in got] == [3998, 5000, 5002]
    assert [g["llm_rerank"] for g in got] == [50, 90, 90]
    assert [g["llm_generative"] for g in got] == [2039, 2079, 2080]
    assert [g["llm_requests"] for g in got] == [6087, 7169, 7172]
    assert "generative consolidation" in r.stderr and "2000" in r.stderr


def test_d_drain_embedding_rows_alone_never_reach_the_cap(box, make_upstream):
    up = make_upstream(states=[state(ops(pending=1, completed=9), rows=mixed(10, 9000)),
                               state(ops(completed=10), rows=mixed(12, 9100))])
    r = drain(box, up, "--max-llm", "2000")
    assert r.returncode == 0, r.stderr
    assert rows(box)[-1]["llm_requests"] == 9112 and rows(box)[-1]["llm_consolidation_generative"] == 12


def test_d_drain_since_is_the_llm_window(box, make_upstream):
    up = make_upstream(states=[state(ops())])
    r = drain(box, up, "--since", "2026-10-30T01:02:03Z", "--allow-empty", "--timeout", "1e9")
    assert r.returncode == 0, r.stderr
    llm = [urllib.parse.parse_qs(urllib.parse.urlsplit(p).query) for m, p, *_ in up.requests if "llm-requests" in p]
    assert {q["start_date"][0] for q in llm} == {"2026-10-30T01:02:03Z"}


# ---------------------------------------------------------------- (e) batch size holds


@pytest.mark.parametrize("n,size", [(7, 3), (6, 3), (5, 10), (4, 1)])
def test_e_batches_are_the_requested_size(box, make_upstream, n, size):
    """The pause between batches is visible to the server: the PATCH arrivals split at every gap of the pause."""
    up = make_upstream(docs=doc_ids(n))
    r = box.run(up, "migrate", "--scopes", SCOPE, "--apply", "--batch-size", str(size), "--batch-sleep", "0.6")
    assert r.returncode == 0, r.stderr
    times = [t for *_, t in up.patches()]
    groups, cur = [], 1
    for a, b in zip(times, times[1:]):
        if b - a >= 0.5:
            groups.append(cur)
            cur = 1
        else:
            cur += 1
    groups.append(cur)
    want = [size] * (n // size) + ([n % size] if n % size else [])
    assert groups == want
    assert len([ln for ln in r.stdout.splitlines() if ln.startswith("batch ")]) == len(want)


@pytest.mark.parametrize("bad", ["0", "-1", "x"])
def test_e_batch_size_must_be_positive(box, make_upstream, bad):
    up = make_upstream(docs=doc_ids(2))
    r = box.run(up, "migrate", "--scopes", SCOPE, "--apply", "--batch-size", bad)
    assert r.returncode == 2 and "--batch-size" in r.stderr
    assert up.patches() == []


# ---------------------------------------------------------------- (f) the PATCH value goes through verbatim


@pytest.mark.parametrize("arg,value", [
    ('[["project:*"]]', [["project:*"]]),
    ('[["project:*"], ["topic:*"]]', [["project:*"], ["topic:*"]]),
    ("combined", "combined"),
    ('"combined"', "combined"),
], ids=["project-glob", "project-and-topic", "combined", "combined-json"])
def test_f_scopes_value_is_sent_verbatim(box, make_upstream, arg, value):
    up = make_upstream(docs=doc_ids(3))
    r = box.run(up, "migrate", "--scopes", arg, "--apply")
    assert r.returncode == 0, r.stderr
    assert [b for _m, _p, b, _t in up.patches()] == [{"observation_scopes": value}] * 3
    assert all(c["request"]["body"] == {"observation_scopes": value} for c in box.execs()
               if c["request"]["method"] == "PATCH")


@pytest.mark.parametrize("arg", ['["project:*"]', "[[1]]", "[]", "[[]]", "nonsense", '{"a": 1}', "[[\"a\"]"])
def test_f_malformed_scopes_send_nothing(box, make_upstream, arg):
    up = make_upstream(docs=doc_ids(2))
    r = box.run(up, "migrate", "--scopes", arg, "--apply")
    assert r.returncode == 1 and "--scopes" in r.stderr
    assert up.requests == []


def test_f_patch_failure_stops_and_reports_progress(box, make_upstream):
    """A PATCH the server refuses stops the run with exit 1; the documents already moved are named by count."""
    up = make_upstream(docs=doc_ids(4))
    real = up.server.RequestHandlerClass._serve

    def flaky(self, method):
        if method == "PATCH" and self.path.endswith("doc-0002"):
            n = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(n)
            up.requests.append((method, self.path, None, time.monotonic()))
            return self._reply(500, {"detail": "boom"})
        return real(self, method)

    up.server.RequestHandlerClass._serve = flaky
    r = box.run(up, "migrate", "--scopes", SCOPE, "--apply", "--batch-size", "10")
    assert r.returncode == 1
    assert len(up.patches()) == 3                     # doc-0000, doc-0001, then the refused doc-0002; doc-0003 never
    assert "2/4" in r.stderr


def test_d_drain_counts_generative_consolidation_by_provider(box, make_upstream):
    """R1-b drain end (#298 report 4): 617 consolidation + 1 consolidation_dedup generative calls = 618. Every
    consolidation-stage operation counts; only the local embedding (and tei rerank) rows and the page refreshes do not."""
    r1b = {("consolidation", "claude-code"): 617, ("consolidation_dedup", "claude-code"): 1,
           ("consolidation", "local"): 1234, ("refresh_mental_model", "claude-code"): 41,
           ("mental_model_delta_ops", "claude-code"): 7, ("refresh_mental_model", "tei"): 25,
           ("refresh_mental_model", "local"): 32}
    up = make_upstream(states=[state(ops(pending=1, completed=9), rows=r1b)])
    r = drain(box, up, "--max-llm", "618")
    assert r.returncode == 3, r.stderr
    row = rows(box)[-1]
    assert row["llm_consolidation_generative"] == 618
    assert row["llm_requests"] == 1957 and row["llm_generative"] == 666
    assert row["llm_embedding"] == 1266 and row["llm_rerank"] == 25
    up2 = make_upstream(states=[state(ops(pending=1, completed=9), rows=r1b)])
    (box.tmp / "drain.jsonl").unlink()
    assert drain(box, up2, "--max-llm", "619", "--timeout", "0").returncode == 2   # 618 < 619: no cap


def test_d_drain_counts_scopes_every_n_polls(box, make_upstream):
    """Scope count = distinct tag sets of all observations (the r1-b report 4 before/after method), every N polls and
    on the last one; the polls in between carry null."""
    a, b, c = ["project:a", "kind:x"], ["project:a"], ["topic:t"]
    tags = [[a, a, b], [a, b, c], [b, c], [b, c, c], [b], [b, c], [c, b, a, a]]
    states = [state(ops(pending=1, completed=9), obs_tags=t) for t in tags[:-1]] + [state(ops(), obs_tags=tags[-1])]
    up = make_upstream(states=states)
    r = drain(box, up, "--scope-every", "3")
    assert r.returncode == 0, r.stderr
    assert [g["scopes"] for g in rows(box)] == [2, None, None, 2, None, None, 3]
    listed = [p for m, p, *_ in up.requests if "/memories/list" in p]
    assert len(listed) == 3


# ---------------------------------------------------------------- rework #298-3: how a drain ends


def test_d_drain_cap_beats_success_on_the_same_poll(box, make_upstream):
    """The poll that sees the work finished also sees the cap passed: exit 3, not 0."""
    up = make_upstream(states=[state(ops(pending=1, completed=9), rows=mixed(1990, 3980)),
                               state(ops(completed=10), rows=mixed(2001, 4002))])
    r = drain(box, up, "--max-llm", "2000")
    assert r.returncode == 3, r.stderr
    assert "drained" not in r.stderr


def test_d_drain_timeout_beats_success_on_the_same_poll(box, make_upstream):
    up = make_upstream(states=[state(ops(completed=10), pending_consolidation=0, rows=mixed(5, 10))])
    r = drain(box, up, "--timeout", "0")
    assert r.returncode == 2, r.stderr
    assert "drain: timeout" in r.stderr


def test_d_drain_last_poll_carries_scopes_when_capped(box, make_upstream):
    up = make_upstream(states=[state(ops(pending=1, completed=9), rows=mixed(10, 20), obs_tags=[["a"]]),
                               state(ops(pending=1, completed=9), rows=mixed(2000, 4000), obs_tags=[["a"], ["b"]])])
    r = drain(box, up, "--max-llm", "2000", "--scope-every", "5")
    assert r.returncode == 3, r.stderr
    assert [g["scopes"] for g in rows(box)] == [1, 2]


def test_d_drain_timeout_is_not_delayed_by_the_scope_count(box, make_upstream):
    """The deadline passes while a slow scope count runs: the drain stops on that poll, it does not sleep another
    --interval first."""
    up = make_upstream(states=[state(ops(pending=1, completed=9), obs_tags=[["a"]], obs_delay=1.5)])
    t0 = time.monotonic()
    r = box.run(up, "drain", "--interval", "30", "--timeout", "1", "--scope-every", "1",
                "--timeseries", str(box.tmp / "drain.jsonl"))
    assert r.returncode == 2, r.stderr
    assert time.monotonic() - t0 < 15
    assert [g["scopes"] for g in rows(box)] == [1]


@pytest.mark.parametrize("args", [["--max-llm", "-1"], ["--interval", "-5"], ["--interval", "nan"]])
def test_d_drain_refuses_negative_numbers(box, make_upstream, args):
    up = make_upstream(states=[state(ops(pending=1, completed=9))])
    r = box.run(up, "drain", *args)
    assert r.returncode == 2 and args[0] in r.stderr
    assert up.requests == []


@pytest.mark.parametrize("bad", ["-1", "nan"])
def test_e_batch_sleep_refuses_negative_numbers(box, make_upstream, bad):
    up = make_upstream(docs=doc_ids(4))
    r = box.run(up, "migrate", "--scopes", SCOPE, "--apply", "--batch-size", "1", "--batch-sleep", bad)
    assert r.returncode == 2 and "--batch-sleep" in r.stderr
    assert up.requests == []


def test_d_drain_scope_count_failure_does_not_end_the_drain(box, make_upstream):
    up = make_upstream(states=[state(ops(pending=1, completed=9), obs_status=500),
                               state(ops(completed=10), obs_tags=[["a"], ["b"]])])
    r = drain(box, up, "--scope-every", "1")
    assert r.returncode == 0, r.stderr
    assert [g["scopes"] for g in rows(box)] == [None, 2]
    assert "scope count failed" in r.stderr


@pytest.mark.parametrize("allow_empty", [False, True])
def test_d_drain_needs_seen_work_before_drained(box, make_upstream, allow_empty):
    """Right after migrate --apply the first poll can look idle before the rebuild is queued: that is not drained
    unless work was seen since the drain started (or --allow-empty)."""
    up = make_upstream(states=[state(ops())])
    # an empty start ends at once with --allow-empty (a short timeout would race the first poll on a slow runner);
    # without it only the timeout ends the drain
    timeout = "30" if allow_empty else "0.3"
    r = drain(box, up, "--timeout", timeout, "--interval", "0.05", *(["--allow-empty"] if allow_empty else []))
    assert r.returncode == (0 if allow_empty else 2), r.stderr


def test_d_drain_idle_after_seen_work_is_drained(box, make_upstream):
    up = make_upstream(states=[state(ops(), pending_consolidation=0), state(ops(), pending_consolidation=4),
                               state(ops())])
    r = drain(box, up)
    assert r.returncode == 0, r.stderr
    assert len(rows(box)) == 3


def test_d_drain_timeout_counts_from_since(box, make_upstream):
    """--timeout is measured from --since (the reconsolidation start) when it is given, not from the drain start."""
    import datetime as dt
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=3600)).strftime("%Y-%m-%dT%H:%M:%SZ")
    up = make_upstream(states=[state(ops(pending=1, completed=9))])
    r = drain(box, up, "--since", since, "--timeout", "1800")
    assert r.returncode == 2, r.stderr
    assert len(rows(box)) == 1
