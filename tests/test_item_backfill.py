"""scripts/item-backfill.py invalidates the open memories a completion memory closed, on the hc-next copy (issue #322).

Hermetic, same harness as tests/test_scope_migrate.py: the script runs from a scratch copy of the repo, PATH holds a fake
`docker` whose `exec -i <c> python3 -c <script>` runs the REAL in-container script against a mock upstream HTTP server
(only the base URL differs), so the listings, PATCH bodies and reads the mock sees are what the real server would see.
No real docker, no real bank. The API key only travels on stdin. The matching rule is the real gateway/item_keys.py.

Completion criteria of #322: (a) the listing goes to the last page and a listing that does not add up to `total` is
refused, (b) a production target without --allow-prod is exit 1 with zero PATCH, (c) dry-run sends zero PATCH,
(d) an over-cap completion sends zero PATCH, (e) the batch size holds, (f) the PATCH body is
`{"state": "invalidated", ...}`.
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
sys.path.insert(0, str(REPO / "gateway"))
import item_keys  # noqa: E402

KEY = "test-api-key-7a2f"
BANK = "demo-bank"
BASE = "/v1/default/banks/%s" % BANK
LIST_PATH = BASE + "/memories/list"

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

ALIASES = {"hc": "hindsight-crew", "hindsight-crew": "hindsight-crew"}


def mem(mid, label, tags, at, fact_type="world", state="valid", doc=None, body="hc#251 work"):
    return {"id": mid, "text": "[%s] %s" % (label, body), "fact_type": fact_type, "state": state,
            "tags": list(tags), "mentioned_at": at, "document_id": doc or "doc-" + mid}


def done(mid, n=251, at="2026-10-20T00:00:00Z", fact_type="experience"):
    return mem(mid, "완료", ["project:hindsight-crew", "kind:completion"], at, fact_type=fact_type,
               body="HC#%d 머지 끝" % n)


def handoff(mid, n=251, at="2026-10-10T00:00:00Z", kind="kind:handoff", label="인계", fact_type="world"):
    return mem(mid, label, ["project:hindsight-crew", kind], at, fact_type=fact_type, body="HC#%d 남은 일" % n)


def unrelated(mid, at="2026-10-01T00:00:00Z"):
    return mem(mid, "메모", ["project:hindsight-crew", "kind:note"], at, body="관계없는 기록")


class Upstream:
    """Mock of memories/list (paged per type), PATCH and GET one memory.

    `memories` are served per `type` in the caller's pages; `lie_total` maps a type to the `total` it reports instead;
    `drop_state` serves the rows without `state`; `ignore_type` serves every row whatever `type` asks for; `dup_ids`
    are served under both types, each time labelled with the type asked for. PATCH turns the row's state into the
    body's state."""

    def __init__(self, memories=(), lie_total=None, drop_state=False, ignore_type=False, dup_ids=()):
        self.memories = {m["id"]: dict(m) for m in memories}
        self.order = [m["id"] for m in memories]
        self.lie_total, self.drop_state = dict(lie_total or {}), drop_state
        self.ignore_type, self.dup_ids = ignore_type, set(dup_ids)
        self.requests = []          # (method, path, body)
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
                owner.requests.append((method, self.path, body))
                if self.headers.get("Authorization") != "Bearer " + KEY:
                    return self._reply(401, {"detail": "bad key"})
                url = urllib.parse.urlsplit(self.path)
                q = urllib.parse.parse_qs(url.query)
                if method == "GET" and url.path == LIST_PATH:
                    t = q.get("type", [None])[0]
                    rows = [owner.memories[i] for i in owner.order
                            if t is None or owner.ignore_type or owner.memories[i]["fact_type"] == t
                            or i in owner.dup_ids]
                    limit, offset = int(q.get("limit", ["100"])[0]), int(q.get("offset", ["0"])[0])
                    items = [dict(r, fact_type=t) if r["id"] in owner.dup_ids else dict(r)
                             for r in rows[offset:offset + limit]]
                    if owner.drop_state:
                        for it in items:
                            it.pop("state", None)
                    return self._reply(200, {"items": items, "total": owner.lie_total.get(t, len(rows)),
                                             "limit": limit, "offset": offset})
                if url.path.startswith(BASE + "/memories/"):
                    mid = urllib.parse.unquote(url.path[len(BASE + "/memories/"):])
                    if mid not in owner.memories:
                        return self._reply(404, {"detail": "no such memory"})
                    if method == "PATCH":
                        owner.memories[mid]["state"] = body["state"]
                        return self._reply(200, owner.memories[mid])
                    if method == "GET":
                        return self._reply(200, owner.memories[mid])
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
                if r[0] == "GET" and urllib.parse.urlsplit(r[1]).path == LIST_PATH]


class Box:
    def __init__(self, tmp_path):
        self.root = tmp_path / "repo"
        (self.root / "scripts").mkdir(parents=True)
        (self.root / "gateway").mkdir()
        for name in ("item-backfill.py", "bank-config.py", "hc_host_env.py"):
            if (REPO / "scripts" / name).exists():          # item-backfill.py is absent in the red run
                shutil.copy(REPO / "scripts" / name, self.root / "scripts" / name)
        shutil.copy(REPO / "gateway" / "item_keys.py", self.root / "gateway" / "item_keys.py")
        self.env_file = tmp_path / "env"
        self.env_file.write_text("HC_HINDSIGHT_API_KEY=%s\n" % KEY)
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        fake = self.bin / "docker"
        fake.write_text(FAKE_DOCKER % {"python": sys.executable})
        fake.chmod(0o755)
        self.log = tmp_path / "docker.jsonl"
        self.log.write_text("")
        self.aliases = tmp_path / "aliases.json"
        self.aliases.write_text(json.dumps(ALIASES))
        self.out = tmp_path / "targets.jsonl"
        self.tmp = tmp_path

    def run(self, upstream, *args, target="next", timeout=60, aliases=True, out=True):
        env = {"PATH": "%s:/usr/bin:/bin" % self.bin, "HOME": str(self.root), "HC_ENV_FILE": str(self.env_file),
               "FAKE_DOCKER_LOG": str(self.log), "FAKE_UPSTREAM": upstream.url, "FAKE_KEY": KEY,
               "FAKE_CONTAINER_NAME": "ctr"}
        env.update(TARGETS[target])
        cmd = [sys.executable, str(self.root / "scripts" / "item-backfill.py"), "--bank", BANK] + list(args)
        if aliases:
            cmd += ["--aliases", str(self.aliases)]
        if out:
            cmd += ["--out", str(self.out)]
        return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)

    def rows(self):
        return [json.loads(ln) for ln in self.out.read_text().splitlines() if ln.strip()]

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


def summary(r):
    (line,) = [ln for ln in r.stdout.splitlines() if ln.startswith("summary: ")]
    return json.loads(line[len("summary: "):])


def bank_of(n_filler, n_targets=1):
    """One completion, `n_targets` open handoffs it closes, and `n_filler` unrelated memories split across both types."""
    fill = [unrelated("f%04d" % i) if i % 2 else dict(unrelated("f%04d" % i), fact_type="experience")
            for i in range(n_filler)]
    return [done("c1")] + [handoff("h%02d" % i) for i in range(n_targets)] + fill


# ---------------------------------------------------------------- (a) every page, total must add up


@pytest.mark.parametrize("n_filler,page", [(0, 500), (3, 2), (250, 100), (7, 3), (40, 1)])
@pytest.mark.parametrize("mode", ["dry-run", "apply"])
def test_a_lists_every_page_of_both_types(box, make_upstream, n_filler, page, mode):
    up = make_upstream(memories=bank_of(n_filler))
    extra = ["--apply"] if mode == "apply" else []
    r = box.run(up, "--page-size", str(page), *extra)
    assert r.returncode == 0, r.stderr
    for t in ("world", "experience"):
        n = sum(1 for m in up.memories.values() if m["fact_type"] == t)
        offsets = [int(q["offset"][0]) for q in up.listings() if q["type"] == [t]]
        assert offsets == (list(range(0, n, page)) if n else [0])
        assert all(int(q["limit"][0]) == page for q in up.listings())
    assert {q["type"][0] for q in up.listings()} == {"world", "experience"}
    s = summary(r)
    assert s["memories"] == n_filler + 2 and s["targets"] == 1 and s["completions"] == 1
    assert up.patched_ids() == (["h00"] if mode == "apply" else [])
    assert_no_secret(box, r)


@pytest.mark.parametrize("fact_type,lie", [("world", 99), ("world", 1), ("experience", 0), ("experience", 5)])
@pytest.mark.parametrize("mode", ["dry-run", "apply"])
def test_a_listing_that_disagrees_with_total_is_refused(box, make_upstream, fact_type, lie, mode):
    up = make_upstream(memories=bank_of(6), lie_total={fact_type: lie})
    extra = ["--apply"] if mode == "apply" else []
    r = box.run(up, "--page-size", "2", *extra)
    assert r.returncode == 1
    assert "total" in r.stderr
    assert up.patches() == []


def test_a_row_of_another_type_is_refused(box, make_upstream):
    """A server that ignores the type filter serves experience rows under type=world: refused, nothing sent (#3)."""
    up = make_upstream(memories=bank_of(4), ignore_type=True)
    r = box.run(up, "--apply")
    assert r.returncode == 1 and "of type" in r.stderr
    assert up.patches() == []


def test_a_id_listed_under_both_types_counts_once(box, make_upstream):
    """The completion and its target come back under both types: one memory each, one pair, one PATCH (#3)."""
    up = make_upstream(memories=bank_of(2), dup_ids=["c1", "h00"])
    r = box.run(up, "--apply")
    assert r.returncode == 0, r.stderr
    s = summary(r)
    assert (s["memories"], s["completions"], s["pairs"], s["targets"]) == (4, 1, 1, 1)
    assert up.patched_ids() == ["h00"]
    assert len(box.rows()) == 1


def test_a_listing_without_state_is_refused(box, make_upstream):
    """is_target needs `state`; a reply without it cannot tell valid from invalidated rows."""
    up = make_upstream(memories=bank_of(2), drop_state=True)
    r = box.run(up, "--apply")
    assert r.returncode == 1 and "state" in r.stderr
    assert up.patches() == []


# ---------------------------------------------------------------- (b) production needs --allow-prod


@pytest.mark.parametrize("target", ["prod", "copy-shaped"])
@pytest.mark.parametrize("mode", ["dry-run", "apply", "check"])
def test_b_prod_without_allow_prod_is_exit_1_and_sends_nothing(box, make_upstream, target, mode):
    up = make_upstream(memories=bank_of(3))
    box.out.write_text(json.dumps({"completion": "c1", "target": "h00", "over_cap": False}) + "\n")
    args = {"dry-run": [], "apply": ["--apply"], "check": ["--check", str(box.out)]}[mode]
    r = box.run(up, *args, target=target)
    assert r.returncode == 1
    assert up.patches() == []
    assert up.requests == []                          # refused before the first request, not after a listing
    assert box.execs() == []
    assert ("--allow-prod" if target == "prod" else "HC_TARGET=next") in r.stderr
    assert len([ln for ln in r.stderr.splitlines() if ln.startswith("target:")]) == 1
    assert_no_secret(box, r)


def test_b_allow_prod_opens_production(box, make_upstream):
    up = make_upstream(memories=bank_of(1))
    r = box.run(up, "--apply", "--allow-prod", target="prod")
    assert r.returncode == 0, r.stderr
    assert up.patched_ids() == ["h00"]
    assert {c["container"] for c in box.execs()} == {PROD["HC_HINDSIGHT_CONTAINER"]}


def test_b_copy_target_reaches_the_copy_container(box, make_upstream):
    up = make_upstream(memories=bank_of(1))
    r = box.run(up, "--apply")
    assert r.returncode == 0, r.stderr
    assert {c["container"] for c in box.execs()} == {NEXT["HC_HINDSIGHT_CONTAINER"]}
    assert [ln for ln in r.stderr.splitlines() if ln.startswith("target:")] == [
        "target: gw=127.0.0.1:18888 project=hc-next container=hc-next-hindsight-1 pg0=hc-next_pg0"]


# ---------------------------------------------------------------- (c) dry-run sends no PATCH


@pytest.mark.parametrize("flags", [[], ["--dry-run"]], ids=["default", "explicit"])
@pytest.mark.parametrize("n_targets", [0, 1, 4])
def test_c_dry_run_lists_only(box, make_upstream, flags, n_targets):
    up = make_upstream(memories=bank_of(5, n_targets))
    r = box.run(up, *flags)
    assert r.returncode == 0, r.stderr
    assert up.patches() == []
    assert {m for m, *_ in up.requests} == {"GET"}
    assert "dry-run" in r.stdout
    rows = box.rows()
    assert sorted(row["target"] for row in rows) == ["h%02d" % i for i in range(n_targets)]
    assert all(row["completion"] == "c1" and row["keys"] == ["hindsight-crew#251"] and row["kind"] == ["kind:handoff"]
               and row["over_cap"] is False for row in rows)
    s = summary(r)
    assert (s["completions"], s["completions_with_keys"], s["targets"], s["over_cap_completions"]) == (
        1, 1, n_targets, 0)
    assert s["rule_version"] == item_keys.RULE_VERSION and len(s["aliases_sha256"]) == 64
    assert (box.out.stat().st_mode & 0o777) == 0o600


def test_c_dry_run_and_apply_are_exclusive(box, make_upstream):
    up = make_upstream(memories=bank_of(1))
    r = box.run(up, "--dry-run", "--apply")
    assert r.returncode == 2 and "not allowed with" in r.stderr
    assert up.requests == []


@pytest.mark.parametrize("table", ['["hc"]', '{}', '{"HC": "hindsight-crew"}', 'not json'])
def test_c_malformed_alias_table_is_refused_before_the_target(box, make_upstream, table):
    up = make_upstream(memories=bank_of(1))
    box.aliases.write_text(table)
    r = box.run(up, "--apply")
    assert r.returncode == 1 and "--aliases" in r.stderr
    assert up.requests == [] and box.execs() == []


def test_c_expected_alias_sha256(box, make_upstream):
    """--expect-aliases-sha256: the same digest goes on, another one is refused before the target (#4)."""
    import hashlib
    good = hashlib.sha256(box.aliases.read_bytes()).hexdigest()
    up = make_upstream(memories=bank_of(1))
    r = box.run(up, "--apply", "--expect-aliases-sha256", good.upper())
    assert r.returncode == 0, r.stderr
    assert up.patched_ids() == ["h00"]
    up2 = make_upstream(memories=bank_of(1))
    r = box.run(up2, "--apply", "--expect-aliases-sha256", "0" * 64)
    assert r.returncode == 1 and good in r.stderr
    assert up2.requests == [] and up2.patches() == []


def test_c_only_the_module_rule_decides(box, make_upstream):
    """Non-targets by the #320 rule stay out: newer handoff, same document, other project, invalidated, completion."""
    c = done("c1")
    newer = handoff("newer", at="2026-10-25T00:00:00Z")
    same_doc = dict(handoff("same"), document_id=c["document_id"])
    other = dict(handoff("other"), tags=["project:other", "kind:handoff"])
    gone = dict(handoff("gone"), state="invalidated")
    other_done = done("c2", at="2026-10-05T00:00:00Z")      # an earlier completion is never a target
    resolved = handoff("resolved", kind="kind:decision", label="보류 해제")
    pending = handoff("pending", kind="kind:decision", label="재개 대기")
    up = make_upstream(memories=[c, newer, same_doc, other, gone, other_done, resolved, pending])
    r = box.run(up, "--apply")
    assert r.returncode == 0, r.stderr
    assert up.patched_ids() == ["pending"]
    assert summary(r)["completions"] == 2


# ---------------------------------------------------------------- (d) an over-cap completion sends nothing


@pytest.mark.parametrize("n_targets,patched", [
    (item_keys.MAX_TARGETS_PER_COMPLETION, item_keys.MAX_TARGETS_PER_COMPLETION),
    (item_keys.MAX_TARGETS_PER_COMPLETION + 1, 0),
    (item_keys.MAX_TARGETS_PER_COMPLETION + 7, 0),
])
def test_d_over_cap_completion_sends_no_patch(box, make_upstream, n_targets, patched):
    up = make_upstream(memories=bank_of(2, n_targets))
    r = box.run(up, "--apply", "--batch-size", "3")
    assert r.returncode == 0, r.stderr
    assert len(up.patches()) == patched
    s = summary(r)
    assert s["over_cap_completions"] == (1 if patched == 0 else 0)
    assert s["targets"] == patched and s["pairs"] == n_targets
    assert all(row["over_cap"] is (patched == 0) for row in box.rows())


def test_d_over_cap_completion_does_not_block_another(box, make_upstream):
    """c1 is over the cap and invalidates nothing; c2 (another key) still invalidates its own target once."""
    cap = item_keys.MAX_TARGETS_PER_COMPLETION
    mems = [done("c1")] + [handoff("h%02d" % i) for i in range(cap + 1)] + [done("c2", n=300), handoff("x", n=300)]
    up = make_upstream(memories=mems)
    r = box.run(up, "--apply")
    assert r.returncode == 0, r.stderr
    assert up.patched_ids() == ["x"]
    assert summary(r)["over_cap_completions"] == 1


def test_d_target_of_two_completions_is_patched_once(box, make_upstream):
    h = mem("h", "인계", ["project:hindsight-crew", "kind:handoff"], "2026-10-01T00:00:00Z", body="HC#251, HC#252")
    up = make_upstream(memories=[done("c1", n=251), done("c2", n=252), h])
    r = box.run(up, "--apply")
    assert r.returncode == 0, r.stderr
    assert up.patched_ids() == ["h"]
    assert summary(r)["pairs"] == 2 and summary(r)["targets"] == 1


# ---------------------------------------------------------------- (e) the batch size holds


@pytest.mark.parametrize("n_targets,batch", [(1, 1), (7, 3), (9, 3), (10, 4), (10, 20)])
def test_e_batch_size_holds(box, make_upstream, n_targets, batch):
    up = make_upstream(memories=bank_of(1, n_targets))
    r = box.run(up, "--apply", "--batch-size", str(batch))
    assert r.returncode == 0, r.stderr
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith("batch ")]
    sizes = [int(ln.split(": ")[1].split(" ")[0]) for ln in lines]
    expect = [min(batch, n_targets - i) for i in range(0, n_targets, batch)]
    assert sizes == expect
    assert sorted(up.patched_ids()) == ["h%02d" % i for i in range(n_targets)]
    assert any(ln.startswith("apply: start ") for ln in r.stdout.splitlines())


def test_e_batch_sleep_spaces_the_batches(box, make_upstream):
    up = make_upstream(memories=bank_of(0, 4))
    t0 = time.monotonic()
    r = box.run(up, "--apply", "--batch-size", "2", "--batch-sleep", "0.6")
    assert r.returncode == 0, r.stderr
    assert time.monotonic() - t0 >= 0.6


# ---------------------------------------------------------------- (f) the PATCH body


def test_f_patch_body_invalidates_with_the_rule_version(box, make_upstream):
    up = make_upstream(memories=bank_of(2, 3))
    r = box.run(up, "--apply")
    assert r.returncode == 0, r.stderr
    bodies = [b for _m, _p, b in up.patches()]
    assert len(bodies) == 3
    assert all(b == {"state": "invalidated", "reason": "%s backfill" % item_keys.RULE_VERSION} for b in bodies)
    assert all(urllib.parse.urlsplit(p).path.startswith(BASE + "/memories/") for _m, p, _b in up.patches())
    assert_no_secret(box, r)


def test_f_check_counts_states_after_apply(box, make_upstream):
    up = make_upstream(memories=bank_of(2, 3))
    assert box.run(up, "--apply").returncode == 0
    r = box.run(up, "--check", str(box.out), aliases=False, out=False)
    assert r.returncode == 0, r.stderr
    (line,) = [ln for ln in r.stdout.splitlines() if ln.startswith("check: ")]
    got = json.loads(line[len("check: "):])
    assert got["targets"] == 3 and got["states"] == {"invalidated": 3}
    assert len(up.patches()) == 3                     # --check only reads


# ---------------------------------------------------------------- --targets: --apply sends the reviewed list (#6)


def test_targets_same_as_the_reviewed_dry_run_goes_on(box, make_upstream):
    up = make_upstream(memories=bank_of(2, 3))
    assert box.run(up, "--dry-run").returncode == 0
    reviewed = box.tmp / "reviewed.jsonl"
    reviewed.write_text(box.out.read_text())
    r = box.run(up, "--apply", "--targets", str(reviewed))
    assert r.returncode == 0, r.stderr
    assert sorted(up.patched_ids()) == ["h00", "h01", "h02"]
    assert "warning" not in r.stderr


def test_targets_that_differ_send_nothing(box, make_upstream):
    """The bank moved between the dry-run and --apply (a new open memory): exit 1 with both sha256, zero PATCH."""
    up = make_upstream(memories=bank_of(2, 3))
    assert box.run(up, "--dry-run").returncode == 0
    reviewed = box.tmp / "reviewed.jsonl"
    reviewed.write_text(box.out.read_text())
    up2 = make_upstream(memories=bank_of(2, 4))
    r = box.run(up2, "--apply", "--targets", str(reviewed))
    assert r.returncode == 1
    assert up2.patches() == []
    assert "differ" in r.stderr and len([w for w in r.stderr.split() if len(w.strip("(),")) == 64]) >= 2


def test_apply_without_targets_warns(box, make_upstream):
    up = make_upstream(memories=bank_of(1))
    r = box.run(up, "--apply")
    assert r.returncode == 0, r.stderr
    assert "warning" in r.stderr and "--targets" in r.stderr
