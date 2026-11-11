"""scripts/bank-config.py compares a bank's live config with the tracked template and, only on request, applies it (issue #286).

Hermetic: the script runs from a scratch copy of the repo, PATH holds a fake `docker` whose `exec -i <c> python3 -c
<script>` runs the REAL in-container script against a mock upstream HTTP server (the same code path as production,
only the base URL differs), so the PATCH bodies and GET reads that the mock sees are what the real server would see.
No real docker, no real bank. The API key only travels on stdin; the mock checks it and the tests assert it never
appears in argv, stdout or stderr.

Target selection is the #242 contract (HC_TARGET, HC_ENV_FILE, HC_COMPOSE_PROJECT, HC_HINDSIGHT_CONTAINER,
HC_GATEWAY_BIND, HC_PG0_VOLUME), the same one scripts/backup.sh follows.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
KEY = "test-api-key-9f3a"
BANK = "demo-bank"

NEXT = {"HC_TARGET": "next", "HC_COMPOSE_PROJECT": "hc-next", "HC_HINDSIGHT_CONTAINER": "hc-next-hindsight-1",
        "HC_GATEWAY_BIND": "127.0.0.1:18888", "HC_PG0_VOLUME": "hc-next_pg0"}
PROD = {"HC_COMPOSE_PROJECT": "hindsight-crew", "HC_HINDSIGHT_CONTAINER": "hindsight-crew-hindsight-1",
        "HC_GATEWAY_BIND": "127.0.0.1:8888", "HC_PG0_VOLUME": "hindsight-crew_pg0"}
TARGETS = {"next": NEXT, "prod": PROD}

MEMORY_DEFENSE = {"enabled": False, "rules": []}
TEMPLATE_BASE = {"version": "1", "bank": {"retain_extraction_mode": "verbatim"}}
TEMPLATE_OVERLAY = {"version": "1", "bank": {"memory_defense": MEMORY_DEFENSE}}
# What the server reports when it matches the merged template; chunk size is a key the template never mentions.
SAME_CONFIG = {"retain_extraction_mode": "verbatim", "memory_defense": MEMORY_DEFENSE, "retain_chunk_size": 3000,
               "retain_mission": None}
SAME_OVERRIDES = {"memory_defense": MEMORY_DEFENSE}

FAKE_DOCKER = r'''#!%(python)s
import json, os, subprocess, sys

argv = sys.argv[1:]


def record(**extra):
    with open(os.environ["FAKE_DOCKER_LOG"], "a") as fh:
        fh.write(json.dumps(dict(argv=argv, **extra)) + "\n")


if argv[:1] == ["compose"] and "ps" in argv:
    record()
    print(os.environ.get("FAKE_COMPOSE_PS", "cid-1"))
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


class Upstream:
    """Mock of GET/PATCH /v1/default/banks/{id}/config. `sticky` makes PATCH a no-op (a server that ignores it)."""

    def __init__(self, config, overrides, sticky=False):
        self.config, self.overrides, self.sticky = dict(config), dict(overrides), sticky
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

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n)) if n else None

            def _serve(self, method):
                body = self._body()
                owner.requests.append((method, self.path, body))
                if self.headers.get("Authorization") != "Bearer " + KEY:
                    return self._reply(401, {"detail": "bad key"})
                if self.path != "/v1/default/banks/%s/config" % BANK:
                    return self._reply(404, {"detail": "no such path"})
                if method == "PATCH" and not owner.sticky:
                    owner.config.update(body["updates"])
                    owner.overrides.update(body["updates"])
                self._reply(200, {"bank_id": BANK, "config": owner.config, "overrides": owner.overrides})

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


class Box:
    def __init__(self, tmp_path):
        self.root = tmp_path / "repo"
        (self.root / "scripts").mkdir(parents=True)
        for name in ("bank-config.py", "hc_host_env.py"):
            if (REPO / "scripts" / name).exists():          # bank-config.py is absent in the red run
                shutil.copy(REPO / "scripts" / name, self.root / "scripts" / name)
        self.templates = self.root / "templates" / "bank-config"
        (self.templates / "local").mkdir(parents=True)
        (self.templates / "base.json").write_text(json.dumps(TEMPLATE_BASE))
        (self.templates / "local" / (BANK + ".json")).write_text(json.dumps(TEMPLATE_OVERLAY))
        self.env_file = tmp_path / "env"
        self.env_file.write_text("HC_HINDSIGHT_API_KEY=%s\n" % KEY)
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        fake = self.bin / "docker"
        fake.write_text(FAKE_DOCKER % {"python": sys.executable})
        fake.chmod(0o755)
        self.log = tmp_path / "docker.jsonl"
        self.log.write_text("")

    def run(self, upstream, *args, target="next", env=None, bank=BANK):
        full = {"PATH": "%s:/usr/bin:/bin" % self.bin, "HOME": str(self.root), "HC_ENV_FILE": str(self.env_file),
                "FAKE_DOCKER_LOG": str(self.log), "FAKE_UPSTREAM": upstream.url, "FAKE_KEY": KEY,
                "FAKE_CONTAINER_NAME": "ctr"}
        full.update(TARGETS[target] if isinstance(target, str) else target)
        full.update(env or {})
        cmd = [sys.executable, str(self.root / "scripts" / "bank-config.py")] + list(args)
        if bank is not None:
            cmd += ["--bank", bank]
        return subprocess.run(cmd, env=full, capture_output=True, text=True, timeout=60)

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

    def make(config=None, overrides=None, sticky=False):
        up = Upstream(SAME_CONFIG if config is None else config, SAME_OVERRIDES if overrides is None else overrides,
                      sticky)
        made.append(up)
        return up

    yield make
    for up in made:
        up.close()


def assert_no_secret(box, r):
    assert KEY not in r.stdout and KEY not in r.stderr
    for call in box.docker_calls():
        assert KEY not in json.dumps(call["argv"])


def target_lines(r):
    return [ln for ln in r.stderr.splitlines() if ln.startswith("target:")]


# ---------------------------------------------------------------- (a) same config -> diff exit 0


@pytest.mark.parametrize("target", ["next", "prod"])
def test_a_same_config_diff_exit_0(box, make_upstream, target):
    up = make_upstream()
    r = box.run(up, "diff", target=target)
    assert r.returncode == 0, r.stderr
    assert "same retain_extraction_mode" in r.stdout
    assert "same memory_defense" in r.stdout
    assert "change" not in r.stdout
    assert "server-default retain_chunk_size" in r.stdout          # a key the template does not mention
    assert [m for m, _p, _b in up.requests] == ["GET"]
    assert_no_secret(box, r)


def test_a_diff_is_the_default_subcommand(box, make_upstream):
    up = make_upstream()
    r = box.run(up, target="next")
    assert r.returncode == 0, r.stderr
    assert "same retain_extraction_mode" in r.stdout


# ---------------------------------------------------------------- (b) different -> exit 3 and change lines


@pytest.mark.parametrize("target", ["next", "prod"])
def test_b_different_config_diff_exit_3(box, make_upstream, target):
    up = make_upstream(config={**SAME_CONFIG, "retain_extraction_mode": "concise", "memory_defense": None},
                       overrides={})
    r = box.run(up, "diff", target=target)
    assert r.returncode == 3, r.stdout + r.stderr
    lines = r.stdout.splitlines()
    assert 'change retain_extraction_mode: "concise" -> "verbatim"' in lines
    assert 'change memory_defense: null -> {"enabled": false, "rules": []}' in lines
    assert [m for m, _p, _b in up.requests] == ["GET"]              # diff never writes, prod included
    assert_no_secret(box, r)


def test_b_overridden_key_missing_from_template_is_unmanaged_and_a_difference(box, make_upstream):
    up = make_upstream(config={**SAME_CONFIG, "enable_observations": False},
                       overrides={**SAME_OVERRIDES, "enable_observations": False})
    r = box.run(up, "diff")
    assert r.returncode == 3, r.stdout + r.stderr          # exit 0 must mean the template covers every override
    assert "unmanaged enable_observations: false" in r.stdout
    assert "change" not in r.stdout


def test_c_apply_does_not_send_or_fail_on_an_unmanaged_key(box, make_upstream):
    up = make_upstream(config={**SAME_CONFIG, "retain_extraction_mode": "concise", "enable_observations": False},
                       overrides={**SAME_OVERRIDES, "enable_observations": False})
    r = box.run(up, "apply")
    assert r.returncode == 0, r.stdout + r.stderr
    assert [b for _m, _p, b in up.patches()] == [{"updates": {"retain_extraction_mode": "verbatim"}}]
    assert up.config["enable_observations"] is False


# ---------------------------------------------------------------- (c) apply: template keys only, re-read diff 0


@pytest.mark.parametrize("target,flags", [("next", []), ("prod", ["--allow-prod"])])
def test_c_apply_patches_only_changed_template_keys_then_rereads(box, make_upstream, target, flags):
    up = make_upstream(config={**SAME_CONFIG, "retain_extraction_mode": "concise"},
                       overrides={"memory_defense": MEMORY_DEFENSE})
    r = box.run(up, "apply", *flags, target=target)
    assert r.returncode == 0, r.stdout + r.stderr
    patches = up.patches()
    assert len(patches) == 1
    assert patches[0][1] == "/v1/default/banks/%s/config" % BANK
    # memory_defense already matched and retain_chunk_size is not in the template: neither is sent
    assert patches[0][2] == {"updates": {"retain_extraction_mode": "verbatim"}}
    methods = [m for m, _p, _b in up.requests]
    assert methods == ["GET", "PATCH", "GET"]                       # read, write, verify read
    assert up.config["retain_chunk_size"] == 3000
    assert "same retain_extraction_mode" in r.stdout.split("patched", 1)[1]
    assert "change" not in r.stdout.split("patched", 1)[1]
    assert_no_secret(box, r)


def test_c_apply_with_nothing_to_change_sends_no_patch(box, make_upstream):
    up = make_upstream()
    r = box.run(up, "apply")
    assert r.returncode == 0, r.stdout + r.stderr
    assert up.patches() == []


def test_c_apply_sends_every_changed_template_key_in_one_patch(box, make_upstream):
    up = make_upstream(config={**SAME_CONFIG, "retain_extraction_mode": "concise", "memory_defense": None}, overrides={})
    r = box.run(up, "apply")
    assert r.returncode == 0, r.stdout + r.stderr
    assert [b for _m, _p, b in up.patches()] == [
        {"updates": {"retain_extraction_mode": "verbatim", "memory_defense": MEMORY_DEFENSE}}]


def test_c_apply_fails_when_the_reread_still_differs(box, make_upstream):
    up = make_upstream(config={**SAME_CONFIG, "retain_extraction_mode": "concise"}, sticky=True)
    r = box.run(up, "apply")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "change retain_extraction_mode" in r.stdout
    assert len(up.patches()) == 1


# ---------------------------------------------------------------- (d) prod apply needs --allow-prod


@pytest.mark.parametrize("config", [SAME_CONFIG, {**SAME_CONFIG, "retain_extraction_mode": "concise"}],
                         ids=["same", "different"])
def test_d_prod_apply_without_allow_prod_exit_1_and_no_patch(box, make_upstream, config):
    up = make_upstream(config=config)
    r = box.run(up, "apply", target="prod")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "--allow-prod" in r.stderr
    assert up.patches() == []
    assert up.requests == []                                        # refused before any upstream call
    assert box.execs() == []
    assert len(target_lines(r)) == 1


def test_d_allow_prod_does_not_unlock_a_copy_shaped_target_without_hc_target(box, make_upstream):
    up = make_upstream()
    r = box.run(up, "apply", "--allow-prod", target={**NEXT, "HC_TARGET": ""})
    assert r.returncode == 1, r.stdout + r.stderr
    assert up.requests == []


# ---------------------------------------------------------------- (e) target contract guards


COPY_CELLS = [
    {"HC_COMPOSE_PROJECT": "hc-next"},
    {"HC_HINDSIGHT_CONTAINER": "hc-next-hindsight-1"},
    {"HC_GATEWAY_BIND": "127.0.0.1:18888"},
    {"HC_PG0_VOLUME": "hc-next_pg0"},
]
PROD_CELLS = [
    {"HC_COMPOSE_PROJECT": "hindsight-crew"},
    {"HC_HINDSIGHT_CONTAINER": "hindsight-crew-hindsight-1"},
    {"HC_GATEWAY_BIND": "127.0.0.1:8888"},
    {"HC_PG0_VOLUME": "hindsight-crew_pg0"},
]


@pytest.mark.parametrize("command", ["diff", "apply"])
@pytest.mark.parametrize("cell", COPY_CELLS, ids=lambda c: next(iter(c)))
def test_e_copy_shaped_target_with_empty_hc_target_exit_1(box, make_upstream, command, cell):
    up = make_upstream()
    r = box.run(up, command, "--allow-prod", target={**PROD, **cell, "HC_TARGET": ""})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "HC_TARGET" in r.stderr
    assert up.requests == [] and box.execs() == []
    assert len(target_lines(r)) == 1


@pytest.mark.parametrize("command", ["diff", "apply"])
def test_e_copy_shaped_target_with_hc_target_unset_exit_1(box, make_upstream, command):
    up = make_upstream()
    env = {k: v for k, v in NEXT.items() if k != "HC_TARGET"}
    r = box.run(up, command, target=env)
    assert r.returncode == 1, r.stdout + r.stderr
    assert up.requests == []


@pytest.mark.parametrize("command", ["diff", "apply"])
@pytest.mark.parametrize("cell", PROD_CELLS, ids=lambda c: next(iter(c)))
def test_e_hc_target_next_with_a_production_cell_exit_1(box, make_upstream, command, cell):
    up = make_upstream()
    r = box.run(up, command, target={**NEXT, **cell})
    assert r.returncode == 1, r.stdout + r.stderr
    assert up.requests == [] and box.execs() == []


def test_target_line_is_exactly_one_on_stderr_and_has_the_contract_shape(box, make_upstream):
    up = make_upstream()
    r = box.run(up, "diff", target="next", env={"FAKE_CONTAINER_NAME": "hc-next-hindsight-1"})
    assert r.returncode == 0, r.stderr
    lines = target_lines(r)
    assert len(lines) == 1
    assert re.fullmatch(r"target: gw=127\.0\.0\.1:18888 project=hc-next container=hc-next-hindsight-1 pg0=hc-next_pg0",
                        lines[0])
    assert "target:" not in r.stdout


def test_container_is_found_through_compose_when_not_given(box, make_upstream):
    up = make_upstream()
    env = {k: v for k, v in PROD.items() if k != "HC_HINDSIGHT_CONTAINER"}
    r = box.run(up, "diff", target=env, env={"FAKE_CONTAINER_NAME": "hindsight-crew-hindsight-1"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "container=hindsight-crew-hindsight-1" in r.stderr
    assert box.execs()[0]["container"] == "hindsight-crew-hindsight-1"
    ps = [c["argv"] for c in box.docker_calls() if c["argv"][:1] == ["compose"]]
    assert len(ps) == 1 and ps[0][ps[0].index("-p") + 1] == "hindsight-crew" and ps[0][-3:] == ["ps", "-q", "hindsight"]


def test_named_env_file_that_does_not_exist_exit_1(box, make_upstream):
    up = make_upstream()
    r = box.run(up, "diff", env={"HC_ENV_FILE": str(box.root / "nope.env")})
    assert r.returncode == 1
    assert "HC_ENV_FILE" in r.stderr
    assert up.requests == []


def test_missing_api_key_exit_1_without_upstream_call(box, make_upstream):
    up = make_upstream()
    box.env_file.write_text("HC_PG0_VOLUME=x\n")
    r = box.run(up, "diff")
    assert r.returncode == 1
    assert "HC_HINDSIGHT_API_KEY" in r.stderr
    assert up.requests == []


def test_repeated_key_in_env_file_keeps_the_last_value(box, make_upstream):
    box.env_file.write_text("HC_HINDSIGHT_API_KEY=stale-key-0000\nHC_HINDSIGHT_API_KEY=%s\n" % KEY)
    up = make_upstream()
    r = box.run(up, "diff")
    assert r.returncode == 0, r.stdout + r.stderr


def test_compose_project_name_from_the_env_file_feeds_the_target_guard(box, make_upstream):
    box.env_file.write_text("HC_HINDSIGHT_API_KEY=%s\nCOMPOSE_PROJECT_NAME=hc-next\n" % KEY)
    up = make_upstream()
    target = {k: v for k, v in PROD.items() if k != "HC_COMPOSE_PROJECT"}
    r = box.run(up, "diff", target=target)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "project=hc-next" in r.stderr and "HC_TARGET" in r.stderr
    assert up.requests == []


def test_upstream_failures_are_clean_exits(box, make_upstream):
    up = make_upstream(config={**SAME_CONFIG, "retain_extraction_mode": "concise"})
    bad_key = box.run(up, "diff", env={})
    assert bad_key.returncode == 3
    box.env_file.write_text("HC_HINDSIGHT_API_KEY=wrong-key-1111\n")
    r = box.run(up, "diff")
    assert r.returncode == 1 and "HTTP 401" in r.stderr and "Traceback" not in r.stderr


# ---------------------------------------------------------------- template handling


def test_overlay_overrides_base_and_both_are_merged_per_key(box, make_upstream):
    (box.templates / "local" / (BANK + ".json")).write_text(
        json.dumps({"version": "1", "bank": {"retain_extraction_mode": "concise", "reflect_mission": "m"}}))
    up = make_upstream(config={**SAME_CONFIG, "retain_extraction_mode": "concise", "reflect_mission": None})
    r = box.run(up, "diff")
    assert r.returncode == 3, r.stdout + r.stderr
    assert 'change reflect_mission: null -> "m"' in r.stdout
    assert "same retain_extraction_mode" in r.stdout                # the overlay value won over base "verbatim"


def test_missing_overlay_means_base_only(box, make_upstream):
    (box.templates / "local" / (BANK + ".json")).unlink()
    up = make_upstream(config={**SAME_CONFIG, "memory_defense": None}, overrides={})
    r = box.run(up, "diff")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "memory_defense" not in r.stdout.replace("server-default memory_defense", "")
    assert any(ln.startswith("bank-config: warning:") and "overlay" in ln for ln in r.stderr.splitlines()), r.stderr
    assert len([ln for ln in r.stderr.splitlines() if "warning" in ln]) == 1


def test_existing_overlay_gives_no_warning(box, make_upstream):
    up = make_upstream()
    r = box.run(up, "diff")
    assert "warning" not in r.stderr


def test_missing_overlay_warns_but_apply_still_sends_base_keys_only(box, make_upstream):
    (box.templates / "local" / (BANK + ".json")).unlink()
    up = make_upstream(config={**SAME_CONFIG, "retain_extraction_mode": "concise"}, overrides={})
    r = box.run(up, "apply")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "warning" in r.stderr
    assert [b for _m, _p, b in up.patches()] == [{"updates": {"retain_extraction_mode": "verbatim"}}]


def test_explicit_overlay_that_does_not_exist_exit_1(box, make_upstream):
    up = make_upstream()
    r = box.run(up, "diff", "--overlay", str(box.root / "nope.json"))
    assert r.returncode == 1
    assert up.requests == []


@pytest.mark.parametrize("section", ["mental_models", "directives"])
def test_template_sections_other_than_bank_are_rejected(box, make_upstream, section):
    (box.templates / "base.json").write_text(json.dumps({**TEMPLATE_BASE, section: []}))
    up = make_upstream()
    r = box.run(up, "diff")
    assert r.returncode == 1
    assert section in r.stderr
    assert up.requests == []


@pytest.mark.parametrize("body", [{"bank": {}}, {"version": "2", "bank": {}}, {"version": "1", "bank": []}],
                         ids=["no-version", "version-2", "bank-not-object"])
def test_malformed_template_exit_1(box, make_upstream, body):
    (box.templates / "base.json").write_text(json.dumps(body))
    up = make_upstream()
    r = box.run(up, "diff")
    assert r.returncode == 1
    assert up.requests == []


def test_bank_id_that_is_not_a_plain_id_is_refused(box, make_upstream):
    up = make_upstream()
    r = box.run(up, "diff", bank="../x")
    assert r.returncode == 1
    assert up.requests == []


# ---------------------------------------------------------------- the tracked templates themselves


def test_tracked_base_template_is_a_bank_only_v1_manifest():
    base = json.loads((REPO / "templates" / "bank-config" / "base.json").read_text())
    assert base["version"] == "1"
    assert set(base) == {"version", "bank"}
    assert isinstance(base["bank"], dict)


# keys whose value is a server-wide env default (HINDSIGHT_API_<NAME>): base applies to every bank, so putting one
# there would pin an override on banks that should inherit the server value
SERVER_ENV_KEYS = ("retain_extraction_mode",)


@pytest.mark.parametrize("key", SERVER_ENV_KEYS)
def test_tracked_base_template_holds_no_server_env_default(key):
    base = json.loads((REPO / "templates" / "bank-config" / "base.json").read_text())
    assert key not in base["bank"]


def test_local_overlays_are_gitignored():
    r = subprocess.run(["git", "check-ignore", "-q", "templates/bank-config/local/some-bank.json"], cwd=REPO)
    assert r.returncode == 0
