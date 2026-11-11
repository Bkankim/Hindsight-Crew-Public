"""rerank-server/rotate-logs.sh (issue #2, #141): size-based copytruncate of the sidecar logs and the gateway audit logs.

Every run is hermetic. The script derives its paths from $HOME (sidecar logs) and HC_AUDIT_DIR
(audit logs, default `<repo>/audit`), and in the main checkout that default IS the operational
audit directory. `run_rotate` therefore refuses to start unless both are injected and point
under tmp_path (test_run_rotate_refuses_missing_or_outside_env pins that guard), and every
size/generation knob is set explicitly so no test depends on a default.

The audit logs are written by the gateway container, so the script truncates them INSIDE the
container (`docker exec <container> truncate -s 0 /audit/<name>`, issue #180) instead of from the
host (virtiofs hands the guest a stale size and the next append leaves a NUL hole). No test may
reach a real docker: `run_rotate` puts a stub `docker` first on PATH, points HC_DOCKER_BIN at it
and names a dummy container (the guard below refuses anything that looks operational).
"""
import gzip
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "rerank-server" / "rotate-logs.sh"

# (file name, directory key, env knob for the size threshold, env knob for generations)
GATEWAY = ("gateway.log", "audit", "HC_AUDIT_LOG_MAX_BYTES", "HC_AUDIT_LOG_GENERATIONS")
LEDGER = ("recall-queries.log", "audit", "RERANK_LOG_MAX_BYTES", "RERANK_LOG_GENERATIONS")
SERVER = ("server.log", "logs", "RERANK_LOG_MAX_BYTES", "RERANK_LOG_GENERATIONS")
PROBE = ("probe.log", "logs", "RERANK_LOG_MAX_BYTES", "RERANK_LOG_GENERATIONS")
FILES = [GATEWAY, LEDGER, SERVER, PROBE]
FILE_IDS = [f[0] for f in FILES]

MAX_BYTES = 1000
# Different counts per knob family prove that the audit knob does not leak into the sidecar
# logs and vice versa.
AUDIT_GENERATIONS = 2
RERANK_GENERATIONS = 4


DUMMY_CONTAINER = "hc180-dummy-gateway"
# The only audit-log files the container writes; they are truncated by the container (docker exec),
# the sidecar logs (server.log/probe.log, written by launchd on the host) by the host.
AUDIT_FILES = [GATEWAY, LEDGER]
AUDIT_IDS = [f[0] for f in AUDIT_FILES]
SIDECAR_FILES = [SERVER, PROBE]
SIDECAR_IDS = [f[0] for f in SIDECAR_FILES]

# Stub docker. It records every call in $STUB_LOG and understands two calls only:
#   inspect -f <format naming "/audit"> <$HC_ROTATE_GATEWAY_CONTAINER>
#       prints the container's /audit mount source: $STUB_MOUNT_SOURCE when set (an empty value
#       means "no /audit mount"), else $HC_AUDIT_DIR (STUB_MODE=inspect-fail exits 1 instead)
#   exec <$HC_ROTATE_GATEWAY_CONTAINER> truncate -s 0 /audit/<name>
#       empties $HC_AUDIT_DIR/<name> (what the container does on the shared mount); STUB_MODE=fail
#       exits 1, STUB_MODE=noop exits 0 without touching the file, STUB_MODE=trunc-fail empties the
#       file and THEN exits 1 (the CLI died after the container finished)
STUB_DOCKER = r"""#!/bin/bash
echo "$*" >> "${STUB_LOG:?}"
if [ "$#" -eq 4 ] && [ "$1" = inspect ] && [ "$2" = -f ] && [ "$4" = "${HC_ROTATE_GATEWAY_CONTAINER:?}" ]; then
  case "$3" in *'"/audit"'*) ;; *) exit 2 ;; esac
  [ "${STUB_MODE:-}" = inspect-fail ] && exit 1
  printf '%s' "${STUB_MOUNT_SOURCE-${HC_AUDIT_DIR:?}}"
  exit 0
fi
case "${STUB_MODE:-truncate}" in
  fail) exit 1 ;;
  noop) exit 0 ;;
esac
if [ "$#" -eq 6 ] && [ "$1" = exec ] && [ "$2" = "$HC_ROTATE_GATEWAY_CONTAINER" ] \
   && [ "$3" = truncate ] && [ "$4" = -s ] && [ "$5" = 0 ]; then
  case "$6" in
    /audit/*) : > "${HC_AUDIT_DIR:?}/${6#/audit/}"; [ "${STUB_MODE:-}" = trunc-fail ] && exit 1; exit 0 ;;
  esac
fi
exit 2
"""


def make_stub(tmp_path: Path, name: str = "stubbin") -> Path:
    path = tmp_path / name / "docker"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(STUB_DOCKER)
    path.chmod(0o755)
    return path


def docker_calls(tmp_path: Path) -> list:
    log = tmp_path / "docker-calls.log"
    return log.read_text().splitlines() if log.exists() else []


def exec_calls(tmp_path: Path) -> list:
    return [c for c in docker_calls(tmp_path) if c.startswith("exec ")]


def make_env(tmp_path: Path) -> dict:
    return {
        "HOME": str(tmp_path / "home"),
        "HC_AUDIT_DIR": str(tmp_path / "audit"),
        "RERANK_LOG_MAX_BYTES": str(MAX_BYTES),
        "RERANK_LOG_GENERATIONS": str(RERANK_GENERATIONS),
        "HC_AUDIT_LOG_MAX_BYTES": str(MAX_BYTES),
        "HC_AUDIT_LOG_GENERATIONS": str(AUDIT_GENERATIONS),
        "HC_DOCKER_BIN": str(make_stub(tmp_path)),
        "HC_ROTATE_GATEWAY_CONTAINER": DUMMY_CONTAINER,
        "STUB_LOG": str(tmp_path / "docker-calls.log"),
    }


def guard_hermetic(tmp_path: Path, env: dict) -> None:
    for key in ("HOME", "HC_AUDIT_DIR", "HC_DOCKER_BIN"):
        value = env.get(key)
        if not value or not Path(value).resolve().is_relative_to(tmp_path.resolve()):
            raise AssertionError(f"{key} must be injected under tmp_path, got {value!r}")
    container = env.get("HC_ROTATE_GATEWAY_CONTAINER", "")
    if not container or container.startswith("hindsight-crew"):
        raise AssertionError(f"HC_ROTATE_GATEWAY_CONTAINER must be a dummy name, got {container!r}")


def run_rotate(tmp_path: Path, **overrides) -> subprocess.CompletedProcess:
    env = {**make_env(tmp_path), **overrides}
    guard_hermetic(tmp_path, env)
    # The stub dir is first on PATH too, so even a script that ignored HC_DOCKER_BIN would find the
    # stub before any real docker.
    path = f"{Path(env['HC_DOCKER_BIN']).parent}{os.pathsep}{os.environ['PATH']}"
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env={"PATH": path, **env},
        capture_output=True, text=True, timeout=30, check=True,
    )


def dirs(tmp_path: Path) -> dict:
    d = {"audit": tmp_path / "audit", "logs": tmp_path / "home" / "Library" / "Logs" / "hindsight-rerank"}
    for p in d.values():
        p.mkdir(parents=True, exist_ok=True)
    return d


def generations(name: str, knob_gens: str) -> int:
    return AUDIT_GENERATIONS if knob_gens.startswith("HC_AUDIT") else RERANK_GENERATIONS


def read_gz(path: Path) -> bytes:
    with gzip.open(path, "rb") as fh:
        return fh.read()


def test_run_rotate_refuses_missing_or_outside_env(tmp_path):
    # Hermeticity guard: a forgotten HC_AUDIT_DIR (-> <repo>/audit) or HOME must fail the test, not
    # point the script at operational files.
    with pytest.raises(AssertionError):
        run_rotate(tmp_path, HC_AUDIT_DIR="")
    with pytest.raises(AssertionError):
        run_rotate(tmp_path, HOME="/Users/someone-else")
    with pytest.raises(AssertionError):
        run_rotate(tmp_path, HC_AUDIT_DIR=str(tmp_path.parent / "elsewhere"))
    # ... and so must a real docker or an operational container name (#180).
    with pytest.raises(AssertionError):
        run_rotate(tmp_path, HC_DOCKER_BIN="/opt/homebrew/bin/docker")
    with pytest.raises(AssertionError):
        run_rotate(tmp_path, HC_DOCKER_BIN="")
    for name in ("", "hindsight-crew-gateway-1", "hindsight-crew-hindsight-1"):
        with pytest.raises(AssertionError):
            run_rotate(tmp_path, HC_ROTATE_GATEWAY_CONTAINER=name)
    assert docker_calls(tmp_path) == []


@pytest.mark.parametrize("fname,where,knob_max,knob_gens", FILES, ids=FILE_IDS)
@pytest.mark.parametrize("size,rotates", [(MAX_BYTES + 500, True), (MAX_BYTES, True), (MAX_BYTES - 1, False)],
                         ids=["over", "at-threshold", "below"])
def test_rotates_only_at_or_over_threshold(tmp_path, fname, where, knob_max, knob_gens, size, rotates):
    path = dirs(tmp_path)[where] / fname
    payload = (b"x" * (size - 1)) + b"\n"
    path.write_bytes(payload)
    out = run_rotate(tmp_path).stdout
    if rotates:
        assert f"LOG_ROTATE_OK {fname} {size}" in out
        assert path.read_bytes() == b""                       # truncated in place
        assert read_gz(Path(f"{path}.1.gz")) == payload       # the whole pre-rotation content, gzipped
        assert not Path(f"{path}.1").exists()                 # no uncompressed leftover
    else:
        assert f"LOG_ROTATE_SKIP {fname} {size}" in out
        assert path.read_bytes() == payload
        assert not Path(f"{path}.1.gz").exists()


@pytest.mark.parametrize("fname,where,knob_max,knob_gens", FILES, ids=FILE_IDS)
def test_threshold_knob_is_per_family(tmp_path, fname, where, knob_max, knob_gens):
    # Raising only this file's own knob keeps it from rotating; the other families' knobs do not matter.
    path = dirs(tmp_path)[where] / fname
    path.write_bytes(b"x" * (MAX_BYTES + 500))
    out = run_rotate(tmp_path, **{knob_max: str(MAX_BYTES * 10)}).stdout
    assert f"LOG_ROTATE_SKIP {fname} " in out
    assert path.stat().st_size == MAX_BYTES + 500


@pytest.mark.parametrize("fname,where,knob_max,knob_gens", FILES, ids=FILE_IDS)
def test_generation_cap(tmp_path, fname, where, knob_max, knob_gens):
    path = dirs(tmp_path)[where] / fname
    cap = generations(fname, knob_gens)
    rounds = cap + 2                                          # N+1 and N+2 rotations must not grow past .N.gz
    bodies = []
    for n in range(rounds):
        body = (f"gen-{n} ".encode() * 300)[:MAX_BYTES + 100] + b"\n"
        bodies.append(body)
        path.write_bytes(body)
        run_rotate(tmp_path)
    # newest rotation is .1.gz, oldest kept is .cap.gz, nothing beyond
    for i in range(1, cap + 1):
        assert read_gz(Path(f"{path}.{i}.gz")) == bodies[rounds - i]
    assert not Path(f"{path}.{cap + 1}.gz").exists()
    assert path.read_bytes() == b""



@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
@pytest.mark.parametrize("fname,where,knob_max,knob_gens", FILES, ids=FILE_IDS)
def test_failed_rotation_is_reported_and_keeps_generations(tmp_path, fname, where, knob_max, knob_gens):
    # cp cannot create <file>.1 in a read-only directory: that must not print LOG_ROTATE_OK, must not
    # truncate the live file, and must not shift (and so eventually drop) the existing generations.
    d = dirs(tmp_path)[where]
    path = d / fname
    payload = b"x" * (MAX_BYTES + 100)
    path.write_bytes(payload)
    Path(f"{path}.1.gz").write_bytes(gzip.compress(b"older-1"))
    Path(f"{path}.2.gz").write_bytes(gzip.compress(b"older-2"))
    d.chmod(0o555)
    try:
        out = run_rotate(tmp_path).stdout
    finally:
        d.chmod(0o755)
    assert f"LOG_ROTATE_FAIL {fname} " in out and f"LOG_ROTATE_OK {fname}" not in out
    assert path.read_bytes() == payload
    assert read_gz(Path(f"{path}.1.gz")) == b"older-1" and read_gz(Path(f"{path}.2.gz")) == b"older-2"
    assert not Path(f"{path}.1").exists()


@pytest.mark.parametrize("fname,where,knob_max,knob_gens", FILES, ids=FILE_IDS)
def test_non_numeric_threshold_falls_back_to_default(tmp_path, fname, where, knob_max, knob_gens):
    # "10M" used to make `[ -lt ]` error out and rotate on every run; it must mean the 10MB default.
    path = dirs(tmp_path)[where] / fname
    path.write_bytes(b"x" * (MAX_BYTES + 500))
    out = run_rotate(tmp_path, **{knob_max: "10M"}).stdout
    assert f"LOG_ROTATE_SKIP {fname} " in out
    assert path.stat().st_size == MAX_BYTES + 500


@pytest.mark.parametrize("fname,where,knob_max,knob_gens", FILES, ids=FILE_IDS)
@pytest.mark.parametrize("bad", ["0", "abc", ""], ids=["zero", "text", "empty"])
def test_degenerate_generation_knob_keeps_one_generation_or_default(tmp_path, fname, where, knob_max, knob_gens, bad):
    path = dirs(tmp_path)[where] / fname
    for n in range(3):
        path.write_bytes((f"gen-{n} ".encode() * 300)[:MAX_BYTES + 100] + b"\n")
        run_rotate(tmp_path, **{knob_gens: bad})
    assert Path(f"{path}.1.gz").exists()                      # never "no generation at all"
    assert path.read_bytes() == b""


@pytest.mark.parametrize("fname,where,knob_max,knob_gens", FILES, ids=FILE_IDS)
@pytest.mark.parametrize("zeros", ["08", "09", "008"], ids=["08", "09", "008"])
def test_leading_zero_knobs_are_decimal_and_never_abort_after_truncate(tmp_path, fname, where, knob_max, knob_gens, zeros):
    # bash arithmetic reads 08/09 as a bad octal and would abort the script AFTER the truncate, leaving an
    # uncompressed .1 behind and no token. The knobs must be normalised to base 10 before the cp.
    path = dirs(tmp_path)[where] / fname
    payload = (b"y" * (MAX_BYTES + 100)) + b"\n"
    path.write_bytes(payload)
    out = run_rotate(tmp_path, **{knob_max: "0" + str(MAX_BYTES), knob_gens: zeros}).stdout
    assert f"LOG_ROTATE_OK {fname} {len(payload)}" in out
    assert path.read_bytes() == b""
    assert read_gz(Path(f"{path}.1.gz")) == payload
    assert not Path(f"{path}.1").exists()                      # no half-finished rotation


@pytest.mark.parametrize("fname,where,knob_max,knob_gens", FILES, ids=FILE_IDS)
def test_leading_zero_generation_knob_keeps_its_decimal_value(tmp_path, fname, where, knob_max, knob_gens):
    path = dirs(tmp_path)[where] / fname
    for n in range(4):
        path.write_bytes((f"gen-{n} ".encode() * 300)[:MAX_BYTES + 100] + b"\n")
        run_rotate(tmp_path, **{knob_gens: "02"})
    assert Path(f"{path}.1.gz").exists() and Path(f"{path}.2.gz").exists()
    assert not Path(f"{path}.3.gz").exists()

WRITER = r"""
import sys
path, phase = sys.argv[1], sys.argv[2]
def emit(i): return "line-%06d %s\n" % (i, "p" * 40)
if phase == "handshake":
    n_pre, n_post = int(sys.argv[3]), int(sys.argv[4])
    for i in range(n_pre):
        with open(path, "a", encoding="utf-8") as fh:        # the gateway's per-line open (gateway/audit.py)
            fh.write(emit(i))
    print("READY", flush=True)
    sys.stdin.readline()                                      # parent rotates now
    for i in range(n_pre, n_pre + n_post):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(emit(i))
    print("DONE", flush=True)
else:                                                         # "stream": write until told to stop
    import os, time
    stop = sys.argv[3]
    i = 0
    while not os.path.exists(stop):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(emit(i))
        i += 1
        time.sleep(0.0005)
    print("TOTAL", i, flush=True)
"""


def test_writer_without_restart_loses_and_duplicates_nothing(tmp_path):
    # Deterministic version of the #2 precedent: a live writer process that opens the file in append
    # mode per line (like gateway/audit.py) is parked while the rotation runs, then resumes WITHOUT
    # being restarted. pre-rotation bytes == the gunzipped .1.gz, post-rotation lines == the live file.
    path = dirs(tmp_path)["audit"] / "gateway.log"
    n_pre, n_post = 40, 7
    writer = subprocess.Popen(
        [sys.executable, "-c", WRITER, str(path), "handshake", str(n_pre), str(n_post)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert writer.stdout.readline().strip() == "READY"
        before = path.read_bytes()
        assert len(before) >= MAX_BYTES                        # over the threshold, so it must rotate
        out = run_rotate(tmp_path).stdout
        assert f"LOG_ROTATE_OK gateway.log {len(before)}" in out
        pid_before = writer.pid
        writer.stdin.write("go\n")
        writer.stdin.flush()
        assert writer.stdout.readline().strip() == "DONE"
        assert writer.wait(timeout=10) == 0 and writer.pid == pid_before
    finally:
        if writer.poll() is None:
            writer.kill()
    assert read_gz(Path(f"{path}.1.gz")) == before
    expected_post = "".join("line-%06d %s\n" % (i, "p" * 40) for i in range(n_pre, n_pre + n_post))
    assert path.read_text(encoding="utf-8") == expected_post


def test_writer_streaming_through_rotation_has_no_duplicates_and_a_bounded_gap(tmp_path):
    # Honest version: the writer never pauses. copytruncate cannot be atomic (cp, then truncate), so lines
    # appended between the two are lost (the pre-existing limit of this script, same for server.log and
    # recall-queries.log). Pinned here: nothing is duplicated, order is preserved, the loss is one
    # contiguous gap of at most a few lines, and the writer's later lines do land in the new file.
    path = dirs(tmp_path)["audit"] / "gateway.log"
    stop = tmp_path / "stop"
    writer = subprocess.Popen(
        [sys.executable, "-c", WRITER, str(path), "stream", str(stop)],
        stdout=subprocess.PIPE, text=True)
    try:
        deadline = time.time() + 20
        while (not path.exists() or path.stat().st_size < MAX_BYTES) and time.time() < deadline:
            time.sleep(0.01)
        out = run_rotate(tmp_path).stdout
        assert "LOG_ROTATE_OK gateway.log" in out
        deadline = time.time() + 20
        while (path.stat().st_size < 400) and time.time() < deadline:   # lines keep arriving after the rotation
            time.sleep(0.01)
    finally:
        stop.write_text("")
        total_line = writer.communicate(timeout=20)[0].strip()
        if writer.poll() is None:
            writer.kill()
    total = int(total_line.split()[1])
    rotated = read_gz(Path(f"{path}.1.gz")).decode("utf-8").splitlines()
    live = path.read_text(encoding="utf-8").splitlines()
    # cp may catch the last line mid-append; a torn fragment is not a line, so only whole lines count
    # (its id then simply lands in the contiguous gap below).
    whole = re.compile(r"line-(\d{6}) p{40}")
    ids = [int(m.group(1)) for m in map(whole.fullmatch, rotated + live) if m]
    assert live, "writer's appends after the rotation must land in the live file"
    assert len(ids) == len(set(ids)), "duplicate lines"
    assert ids == sorted(ids), "order not preserved"
    assert ids[0] == 0 and ids[-1] == total - 1
    missing = sorted(set(range(total)) - set(ids))
    if missing:
        assert missing == list(range(missing[0], missing[-1] + 1)), "loss is not one contiguous gap"
    assert len(missing) <= 200, f"copytruncate window lost {len(missing)} lines"


# ---- issue #180: the container-written audit logs are truncated inside the container ----

@pytest.mark.parametrize("fname,where,knob_max,knob_gens", AUDIT_FILES, ids=AUDIT_IDS)
def test_audit_log_is_truncated_by_docker_exec_in_the_container(tmp_path, fname, where, knob_max, knob_gens):
    path = dirs(tmp_path)[where] / fname
    payload = (b"x" * (MAX_BYTES + 500)) + b"\n"
    path.write_bytes(payload)
    out = run_rotate(tmp_path).stdout
    assert f"LOG_ROTATE_OK {fname} {len(payload)}" in out
    calls = docker_calls(tmp_path)
    assert calls[0].startswith("inspect -f ") and calls[0].endswith(f" {DUMMY_CONTAINER}")   # mount check first
    assert exec_calls(tmp_path) == [f"exec {DUMMY_CONTAINER} truncate -s 0 /audit/{fname}"]
    assert len(calls) == 2
    assert path.read_bytes() == b""
    assert read_gz(Path(f"{path}.1.gz")) == payload
    assert not Path(f"{path}.1").exists()


@pytest.mark.parametrize("fname,where,knob_max,knob_gens", AUDIT_FILES, ids=AUDIT_IDS)
def test_mount_source_is_compared_by_realpath(tmp_path, fname, where, knob_max, knob_gens):
    # The container reports a symlink (or ./ and .. spelling) of the audit dir: same directory, so go.
    path = dirs(tmp_path)[where] / fname
    path.write_bytes(b"x" * (MAX_BYTES + 5))
    link = tmp_path / "audit-link"
    link.symlink_to(path.parent)
    out = run_rotate(tmp_path, STUB_MOUNT_SOURCE=f"{link}/../audit").stdout
    assert f"LOG_ROTATE_OK {fname} " in out
    assert exec_calls(tmp_path) == [f"exec {DUMMY_CONTAINER} truncate -s 0 /audit/{fname}"]


@pytest.mark.parametrize("fname,where,knob_max,knob_gens", AUDIT_FILES, ids=AUDIT_IDS)
@pytest.mark.parametrize("failure", ["exit-1", "missing-binary", "not-executable", "no-shrink",
                                     "inspect-fails", "mount-mismatch", "no-audit-mount", "mount-dir-missing"])
def test_failed_container_truncate_keeps_live_file_generations_and_reports_fail(
        tmp_path, fname, where, knob_max, knob_gens, failure):
    # No fallback to the host truncate: the live file, the generations and (absent) .1 stay as they
    # were, and LOG_ROTATE_FAIL is printed with the pre-rotation size. Mount failures never exec.
    path = dirs(tmp_path)[where] / fname
    payload = b"x" * (MAX_BYTES + 100)
    path.write_bytes(payload)
    Path(f"{path}.1.gz").write_bytes(gzip.compress(b"older-1"))
    Path(f"{path}.2.gz").write_bytes(gzip.compress(b"older-2"))
    prod = tmp_path / "prod-audit"           # stands for the operational dir the container really mounts
    prod.mkdir()
    (prod / fname).write_bytes(b"operational ledger\n")
    overrides = {}
    if failure == "exit-1":
        overrides["STUB_MODE"] = "fail"
    elif failure == "no-shrink":              # exec exits 0 but empties nothing (other mount, or no-op)
        overrides["STUB_MODE"] = "noop"
    elif failure == "inspect-fails":
        overrides["STUB_MODE"] = "inspect-fail"
    elif failure == "mount-mismatch":
        overrides["STUB_MOUNT_SOURCE"] = str(prod)
    elif failure == "no-audit-mount":
        overrides["STUB_MOUNT_SOURCE"] = ""
    elif failure == "mount-dir-missing":
        overrides["STUB_MOUNT_SOURCE"] = str(tmp_path / "no-such-dir")
    elif failure == "missing-binary":
        overrides["HC_DOCKER_BIN"] = str(tmp_path / "stubbin" / "no-such-docker")
    else:
        plain = tmp_path / "stubbin" / "plain-file"
        plain.parent.mkdir(parents=True, exist_ok=True)
        plain.write_text("#!/bin/bash\nexit 0\n")
        plain.chmod(0o644)
        overrides["HC_DOCKER_BIN"] = str(plain)
    out = run_rotate(tmp_path, **overrides).stdout
    assert f"LOG_ROTATE_FAIL {fname} {len(payload)}" in out and f"LOG_ROTATE_OK {fname}" not in out
    assert path.read_bytes() == payload
    assert read_gz(Path(f"{path}.1.gz")) == b"older-1" and read_gz(Path(f"{path}.2.gz")) == b"older-2"
    assert not Path(f"{path}.3.gz").exists()
    assert not Path(f"{path}.1").exists()
    assert (prod / fname).read_bytes() == b"operational ledger\n"
    if failure in ("inspect-fails", "mount-mismatch", "no-audit-mount", "mount-dir-missing"):
        assert exec_calls(tmp_path) == []


@pytest.mark.parametrize("fname,where,knob_max,knob_gens", AUDIT_FILES, ids=AUDIT_IDS)
def test_audit_log_is_never_truncated_from_the_host(tmp_path, fname, where, knob_max, knob_gens):
    # A docker that reports success but empties nothing: the live file keeps its bytes (a host-side
    # `: >` would have emptied it) and the run is a failure, not an OK that duplicates a generation.
    path = dirs(tmp_path)[where] / fname
    payload = (b"x" * (MAX_BYTES + 500)) + b"\n"
    path.write_bytes(payload)
    out = run_rotate(tmp_path, STUB_MODE="noop").stdout
    assert f"LOG_ROTATE_FAIL {fname} {len(payload)}" in out
    assert path.read_bytes() == payload
    assert exec_calls(tmp_path) == [f"exec {DUMMY_CONTAINER} truncate -s 0 /audit/{fname}"]


@pytest.mark.parametrize("fname,where,knob_max,knob_gens", AUDIT_FILES, ids=AUDIT_IDS)
def test_audit_log_below_threshold_does_not_call_docker(tmp_path, fname, where, knob_max, knob_gens):
    path = dirs(tmp_path)[where] / fname
    path.write_bytes(b"x" * (MAX_BYTES - 1))
    out = run_rotate(tmp_path).stdout
    assert f"LOG_ROTATE_SKIP {fname} " in out
    assert docker_calls(tmp_path) == []


@pytest.mark.parametrize("fname,where,knob_max,knob_gens", SIDECAR_FILES, ids=SIDECAR_IDS)
def test_sidecar_logs_are_truncated_on_the_host_without_docker(tmp_path, fname, where, knob_max, knob_gens):
    # launchd writes server.log/probe.log on the host (no virtiofs), so they keep the host truncate.
    # A failing docker therefore changes nothing for them.
    path = dirs(tmp_path)[where] / fname
    payload = (b"x" * (MAX_BYTES + 500)) + b"\n"
    path.write_bytes(payload)
    out = run_rotate(tmp_path, STUB_MODE="fail").stdout
    assert f"LOG_ROTATE_OK {fname} {len(payload)}" in out
    assert path.read_bytes() == b""
    assert read_gz(Path(f"{path}.1.gz")) == payload
    assert docker_calls(tmp_path) == []


def test_one_failing_audit_log_does_not_block_the_others(tmp_path):
    d = dirs(tmp_path)
    for name in ("recall-queries.log", "gateway.log", "server.log"):
        (d["audit"] if name != "server.log" else d["logs"]).joinpath(name).write_bytes(b"x" * (MAX_BYTES + 1))
    out = run_rotate(tmp_path, STUB_MODE="fail").stdout
    assert "LOG_ROTATE_FAIL recall-queries.log" in out and "LOG_ROTATE_FAIL gateway.log" in out
    assert "LOG_ROTATE_OK server.log" in out
    assert len(exec_calls(tmp_path)) == 2


def test_container_name_and_default(tmp_path):
    path = dirs(tmp_path)["audit"] / "gateway.log"
    path.write_bytes(b"x" * (MAX_BYTES + 1))
    run_rotate(tmp_path, HC_ROTATE_GATEWAY_CONTAINER="hc180-other-name")
    assert exec_calls(tmp_path) == ["exec hc180-other-name truncate -s 0 /audit/gateway.log"]
    # The default is the compose service's container; it is only read from the source because a run
    # with the default name is exactly what no test may do.
    assert 'HC_ROTATE_GATEWAY_CONTAINER:-hindsight-crew-gateway-1' in SCRIPT.read_text()


# LaunchAgent environment (issue #180): no PATH entry for docker, no .env.local, no cwd. The tools the
# script itself needs go into a private bin dir so no docker on the host PATH can be found by accident.
NEEDED_TOOLS = ["bash", "date", "cp", "wc", "tr", "basename", "dirname", "mv", "rm", "gzip"]


def min_env_run(tmp_path: Path, extra: dict, stub_where: str) -> subprocess.CompletedProcess:
    import shutil
    d = dirs(tmp_path)
    tools = tmp_path / "tools"
    tools.mkdir()
    for tool in NEEDED_TOOLS:
        (tools / tool).symlink_to(shutil.which(tool))
    home = tmp_path / "home"
    stubs = {
        "home-docker-bin": home / ".docker" / "bin",     # a location the script appends by default
        "home-ignored": home / ".docker" / "bin",        # same, but HC_DOCKER_FALLBACKS="" must hide it
        "path": tools,                                    # plain PATH lookup
        "empty-bin": tools,                               # on PATH too, but HC_DOCKER_BIN="" must not reach it
        "elsewhere": tmp_path / "elsewhere",              # only reachable through HC_DOCKER_BIN
        "fallbacks": tmp_path / "fallbacks",              # only reachable through HC_DOCKER_FALLBACKS
        "none": None,                                     # no docker anywhere
    }
    stub_path = None
    if stubs[stub_where] is not None:
        stubs[stub_where].mkdir(parents=True, exist_ok=True)
        stub_path = stubs[stub_where] / "docker"
        stub_path.write_text(STUB_DOCKER)
        stub_path.chmod(0o755)
    (d["audit"] / "gateway.log").write_bytes(b"x" * (MAX_BYTES + 1))
    # HC_DOCKER_FALLBACKS is explicit in EVERY case (the stub's location or empty), so no case can
    # reach a real docker candidate ($HOME/.docker/bin, /opt/homebrew/bin, ...) even if find_docker regresses.
    fallbacks = ""
    if stub_where == "home-docker-bin":
        fallbacks = str(stub_path)
    elif stub_where == "fallbacks":
        fallbacks = f"/no/such/docker {stub_path}"
    env = {
        "HOME": str(home), "HC_AUDIT_DIR": str(d["audit"]), "PATH": str(tools),
        "HC_DOCKER_FALLBACKS": fallbacks,
        "HC_AUDIT_LOG_MAX_BYTES": str(MAX_BYTES), "HC_ROTATE_GATEWAY_CONTAINER": DUMMY_CONTAINER,
        "STUB_LOG": str(tmp_path / "docker-calls.log"),
    }
    if stub_where == "elsewhere":
        env["HC_DOCKER_BIN"] = str(stub_path)
    if stub_where == "empty-bin":
        env["HC_DOCKER_BIN"] = ""
    env.update(extra)
    assert "HC_DOCKER_FALLBACKS" in env
    if stub_where in ("none", "home-ignored"):
        # HC_DOCKER_BIN unset and no docker on PATH: the only way to a real docker would be the
        # appended default locations, so the list must be forced empty.
        assert env["HC_DOCKER_FALLBACKS"] == "" and "HC_DOCKER_BIN" not in env
    elif stub_where == "empty-bin":
        assert env["HC_DOCKER_FALLBACKS"] == "" and env["HC_DOCKER_BIN"] == ""
    else:
        guard_hermetic(tmp_path, {**env, "HC_DOCKER_BIN": env.get("HC_DOCKER_BIN", str(stub_path))})
    return subprocess.run(["env", "-i", *[f"{k}={v}" for k, v in env.items()],
                           str(tools / "bash"), str(SCRIPT)],
                          cwd="/", capture_output=True, text=True, timeout=30, check=True)


@pytest.mark.parametrize("stub_where", ["home-docker-bin", "path", "elsewhere", "fallbacks"],
                         ids=["found-in-appended-location", "found-on-PATH", "found-via-HC_DOCKER_BIN",
                              "found-via-HC_DOCKER_FALLBACKS"])
def test_minimal_launchagent_env_finds_docker(tmp_path, stub_where):
    out = min_env_run(tmp_path, {}, stub_where).stdout
    assert "LOG_ROTATE_OK gateway.log " in out
    assert exec_calls(tmp_path) == [f"exec {DUMMY_CONTAINER} truncate -s 0 /audit/gateway.log"]
    assert (tmp_path / "audit" / "gateway.log").read_bytes() == b""


@pytest.mark.parametrize("stub_where", ["none", "home-ignored", "empty-bin"],
                         ids=["no-docker-anywhere", "empty-fallbacks-hide-home-stub", "empty-docker-bin-does-not-fall-through"])
def test_minimal_env_without_a_findable_docker_reports_fail(tmp_path, stub_where):
    # Nothing on PATH, HC_DOCKER_BIN unset, HC_DOCKER_FALLBACKS="" (no real docker can be reached, whatever
    # is installed on the host): not found is a rotation failure, never a host-side truncate. "empty-bin":
    # a stub IS on PATH, but HC_DOCKER_BIN="" (set, empty) is a bad value, not "unset", so it is not used.
    out = min_env_run(tmp_path, {}, stub_where).stdout
    assert f"LOG_ROTATE_FAIL gateway.log {MAX_BYTES + 1}" in out
    assert (tmp_path / "audit" / "gateway.log").read_bytes() == b"x" * (MAX_BYTES + 1)
    assert not (tmp_path / "audit" / "gateway.log.1").exists()
    assert docker_calls(tmp_path) == []


def test_docker_bin_knob_beats_path(tmp_path):
    other = make_stub(tmp_path, "other-bin")
    (tmp_path / "other-bin" / "docker").write_text("#!/bin/bash\necho PATH-docker-used >> \"$STUB_LOG\"\nexit 0\n")
    path = dirs(tmp_path)["audit"] / "gateway.log"
    path.write_bytes(b"x" * (MAX_BYTES + 1))
    env = {**make_env(tmp_path)}
    guard_hermetic(tmp_path, env)
    subprocess.run(["bash", str(SCRIPT)], env={"PATH": f"{other.parent}{os.pathsep}{os.environ['PATH']}", **env},
                   capture_output=True, text=True, timeout=30, check=True)
    assert exec_calls(tmp_path) == [f"exec {DUMMY_CONTAINER} truncate -s 0 /audit/gateway.log"]
    assert "PATH-docker-used" not in "\n".join(docker_calls(tmp_path))


@pytest.mark.parametrize("fname,where,knob_max,knob_gens", AUDIT_FILES, ids=AUDIT_IDS)
def test_exec_that_truncated_but_exited_nonzero_still_rotates(tmp_path, fname, where, knob_max, knob_gens):
    # The container emptied the file, then the docker CLI died (non-zero). The .1 is now the only copy of
    # the old content: it must be gzipped and the generations shifted, not deleted.
    path = dirs(tmp_path)[where] / fname
    payload = (b"x" * (MAX_BYTES + 500)) + b"\n"
    path.write_bytes(payload)
    Path(f"{path}.1.gz").write_bytes(gzip.compress(b"older-1"))
    out = run_rotate(tmp_path, STUB_MODE="trunc-fail").stdout
    assert f"LOG_ROTATE_OK {fname} {len(payload)}" in out and "LOG_ROTATE_FAIL" not in out
    assert path.read_bytes() == b""
    assert read_gz(Path(f"{path}.1.gz")) == payload
    assert read_gz(Path(f"{path}.2.gz")) == b"older-1"
    assert not Path(f"{path}.1").exists()


def test_default_fallback_locations_include_home_docker_bin():
    # min_env_run pins HC_DOCKER_FALLBACKS in every case (no test may reach a real docker), so the
    # default list itself is only checked against the source.
    assert '${HOME}/.docker/bin/docker /opt/homebrew/bin/docker' in SCRIPT.read_text()
