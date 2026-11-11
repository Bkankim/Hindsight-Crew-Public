"""scripts/backup.sh picks the pg0 volume from the target-selection contract, not from a hard-coded name (issue #245).

Source volume = caller env HC_PG0_VOLUME > HC_ENV_FILE > `docker compose ... config --format json` volumes.pg0.name.
The container that is backed up must have mounted THAT volume at /home/hindsight/.pg0 and be running; with none
the script fails loudly instead of backing up some other volume. Success keeps the stdout contract (exactly the
backup file path, gate6 reads it) and puts the one `target:` line on stderr.

Hermetic like tests/test_script_token_argv.py: the script runs from a scratch copy of the repo, PATH holds only
recording stubs (docker, sh, curl, sleep) and an explicit list of real tools, no real docker is reachable.
"""
import json
import re
import shutil
import subprocess

import pytest

from test_script_token_argv import REPO, Sandbox

DEFAULT_VOL = "hindsight-crew_pg0"
NEXT_VOL = "hc-next_pg0"
PG0_DEST = "/home/hindsight/.pg0"
TARGET_LINE = re.compile(r"^target: gw=(\S+) project=(\S+) container=(\S+) pg0=(\S+)$")
ARCHIVE = "x" * 3000      # backup.sh refuses archives under 2000 bytes


def backup_box(tmp_path, *, containers, host_volumes, compose_name=DEFAULT_VOL, compose_project="hindsight-crew",
               env_file=None):
    """containers: {id: (name, [(volume, destination), ...])} - the running containers the stub docker reports."""
    box = Sandbox(tmp_path, ("backup.sh", "bank_stats.py", "hc-python.sh"))
    for name in ("stat", "find"):
        (box.tools / name).symlink_to(shutil.which(name))
    (box.root / "docker-compose.yml").write_text("services: {}\n")
    if env_file is not None:
        (box.root / ".env.local").write_text(env_file)
    by_volume = {}
    for cid, (_, mounts) in containers.items():
        for vol, _dest in mounts:
            by_volume.setdefault(vol, []).append(cid)
    routes = [{"match": ["compose", "config", "--format", "json"],
               "out": json.dumps({"name": compose_project, "volumes": {"pg0": {"name": compose_name}}})},
              {"match": ["volume", "ls"], "out": "".join(v + "\n" for v in host_volumes)}]
    for vol in sorted(by_volume, key=len, reverse=True):        # longest first: volume=x_v1 also contains volume=x
        routes.append({"match": ["ps", "volume=%s" % vol], "out": "".join(c + "\n" for c in by_volume[vol])})
    routes.append({"match": ["ps"], "out": ""})
    for cid, (name, mounts) in containers.items():
        routes.append({"match": ["inspect", cid],
                       "out": "/%s|%s" % (name, "".join(" %s:%s" % m for m in mounts) + " ")})
        routes.append({"match": ["exec -i", cid], "out": "{}"})
        routes.append({"match": ["exec", cid, "sh -c"], "out": ARCHIVE})
    box.set_routes(routes)
    return box


def two_volume_box(tmp_path, named, **kw):
    return backup_box(
        tmp_path,
        containers={"cid-default": ("hindsight-crew-hindsight-1", [(DEFAULT_VOL, PG0_DEST), ("hindsight-crew_hfcache", "/x")]),
                    "cid-named": ("hindsight-crew-named-1", [(named, PG0_DEST)])},
        host_volumes=[DEFAULT_VOL, named], **kw)


def execs(box):
    return [c["argv"] for c in box.calls("docker") if c["argv"][:1] == ["exec"]]


def exec_targets(box):
    """Container ids that `docker exec` reached (with or without the -i flag)."""
    return {a[2] if a[1] == "-i" else a[1] for a in execs(box)}


def target_lines(stderr):
    return [ln for ln in stderr.splitlines() if ln.startswith("target:")]


def assert_one_path_on_stdout(box, r):
    lines = r.stdout.splitlines()
    assert len(lines) == 1, r.stdout
    assert re.fullmatch(r"backups/\d{8}-\d{6}/pg0\.tar\.gz", lines[0]), lines[0]
    assert (box.root / lines[0]).stat().st_size == len(ARCHIVE)


# --------------------------------------------------------------------------- source volume selection

def test_named_volume_wins_over_the_default_one_when_both_exist(tmp_path):
    box = two_volume_box(tmp_path, "hindsight-crew_pg0_v1")
    r = box.run("scripts/backup.sh", HC_PG0_VOLUME="hindsight-crew_pg0_v1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert exec_targets(box) == {"cid-named"}
    assert_one_path_on_stdout(box, r)
    assert len(target_lines(r.stderr)) == 1 and not target_lines(r.stdout)
    gw, project, container, pg0 = TARGET_LINE.match(target_lines(r.stderr)[0]).groups()
    assert (gw, container, pg0) == ("-", "hindsight-crew-named-1", "hindsight-crew_pg0_v1")


def test_without_a_named_volume_the_compose_rendered_name_is_used(tmp_path):
    box = two_volume_box(tmp_path, "hindsight-crew_pg0_v1")
    r = box.run("scripts/backup.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    assert exec_targets(box) == {"cid-default"}
    assert_one_path_on_stdout(box, r)
    assert TARGET_LINE.match(target_lines(r.stderr)[0]).group(4) == DEFAULT_VOL


@pytest.mark.parametrize("caller, in_file, expect_cid", [
    ("vol-caller", "vol-file", "cid-caller"),     # caller env beats the env file
    (None, "vol-file", "cid-file"),               # env file beats the compose-rendered default
    ("vol-caller", None, "cid-caller"),
], ids=["caller-over-file", "file-over-compose", "caller-without-file-value"])
def test_volume_precedence_caller_then_env_file_then_compose(tmp_path, caller, in_file, expect_cid):
    box = backup_box(
        tmp_path,
        containers={"cid-default": ("c-default", [(DEFAULT_VOL, PG0_DEST)]),
                    "cid-caller": ("c-caller", [("vol-caller", PG0_DEST)]),
                    "cid-file": ("c-file", [("vol-file", PG0_DEST)])},
        host_volumes=[DEFAULT_VOL, "vol-caller", "vol-file"],
        env_file=("HC_PG0_VOLUME=%s\n" % in_file) if in_file else "")
    env = {"HC_PG0_VOLUME": caller} if caller else {}
    r = box.run("scripts/backup.sh", **env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert exec_targets(box) == {expect_cid}


def test_hc_env_file_points_the_script_at_another_file_and_the_relative_path_is_repo_rooted(tmp_path):
    box = two_volume_box(tmp_path, "vol-other")
    (box.root / "envs").mkdir()
    (box.root / "envs" / "next.env").write_text("HC_PG0_VOLUME=vol-other\n")
    (box.root / ".env.local").write_text("HC_PG0_VOLUME=%s\n" % DEFAULT_VOL)    # must NOT be read
    r = box.run("scripts/backup.sh", HC_ENV_FILE="envs/next.env")
    assert r.returncode == 0, r.stdout + r.stderr
    assert exec_targets(box) == {"cid-named"}


# --------------------------------------------------------------------------- fail loudly

@pytest.mark.parametrize("containers, host_volumes", [
    ({"cid-default": ("c-default", [(DEFAULT_VOL, PG0_DEST)])}, [DEFAULT_VOL, "vol-x"]),
    ({"cid-default": ("c-default", [(DEFAULT_VOL, PG0_DEST)]), "cid-x": ("c-x", [("vol-x", "/elsewhere")])},
     [DEFAULT_VOL, "vol-x"]),
    ({"cid-default": ("c-default", [(DEFAULT_VOL, PG0_DEST)])}, [DEFAULT_VOL]),
], ids=["volume-idle", "mounted-at-wrong-destination", "volume-missing"])
def test_missing_running_container_for_the_named_volume_is_exit_1_and_backs_up_nothing(
        tmp_path, containers, host_volumes):
    box = backup_box(tmp_path, containers=containers, host_volumes=host_volumes)
    r = box.run("scripts/backup.sh", HC_PG0_VOLUME="vol-x")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "vol-x" in r.stderr
    assert r.stdout == ""
    assert not execs(box), "must not fall back to another volume"
    assert not (box.root / "backups").exists() or not list((box.root / "backups").glob("*/pg0.tar.gz"))


def test_compose_render_failure_is_exit_1(tmp_path):
    box = two_volume_box(tmp_path, "vol-x")
    box.set_routes([{"match": ["compose", "config"], "rc": 1, "out": ""}])
    r = box.run("scripts/backup.sh")
    assert r.returncode == 1 and r.stdout == ""
    assert not execs(box)


# --------------------------------------------------------------------------- HC_TARGET guard

def next_box(tmp_path, volume, project="hc-next", container="hc-next-hindsight-1"):
    return backup_box(
        tmp_path,
        containers={"cid-next": (container, [(volume, PG0_DEST)])},
        host_volumes=[volume], compose_name=volume, compose_project=project)


@pytest.mark.parametrize("volume, project, container, code", [
    (NEXT_VOL, "hc-next", "hc-next-hindsight-1", 0),
    ("hc-next_pg0_v1", "hc-next", "hc-next-hindsight-1", 1),        # prefix is not a match
    (DEFAULT_VOL, "hc-next", "hc-next-hindsight-1", 1),
    ("hc-next_pg0", "hindsight-crew", "hc-next-hindsight-1", 1),    # right volume, wrong project
    ("hc-next_pg0", "hc-next", "hindsight-crew-hindsight-1", 1),    # right volume, container is not a copy's
], ids=["copy-ok", "volume-prefix", "production-volume", "wrong-project", "wrong-container"])
def test_target_next_requires_the_exact_copy_target(tmp_path, volume, project, container, code):
    box = next_box(tmp_path, volume, project, container)
    r = box.run("scripts/backup.sh", HC_TARGET="next", HC_COMPOSE_PROJECT=project, HC_PG0_VOLUME=volume)
    assert r.returncode == code, r.stdout + r.stderr
    if code:
        assert r.stdout == ""
        assert not execs(box)
    else:
        assert_one_path_on_stdout(box, r)
    assert len(target_lines(r.stderr)) == 1
    assert TARGET_LINE.match(target_lines(r.stderr)[0]).groups() == ("-", project, container, volume)


def test_target_next_applies_to_the_compose_rendered_name_too(tmp_path):
    box = next_box(tmp_path, "hc-next_pg0_v1")
    r = box.run("scripts/backup.sh", HC_TARGET="next", HC_COMPOSE_PROJECT="hc-next")
    assert r.returncode == 1 and r.stdout == ""
    assert not execs(box)


@pytest.mark.parametrize("volume, project", [
    (NEXT_VOL, "hindsight-crew"),
    ("hc-next_pg0_v1", "hindsight-crew"),
    (DEFAULT_VOL, "hc-next"),
    (DEFAULT_VOL, "hc-next-2"),
], ids=["copy-volume", "copy-volume-prefix", "copy-project", "copy-project-prefix"])
def test_copy_names_without_the_target_marker_are_refused(tmp_path, volume, project):
    box = next_box(tmp_path, volume, project, container="c-1")
    r = box.run("scripts/backup.sh", HC_COMPOSE_PROJECT=project, HC_PG0_VOLUME=volume)
    assert r.returncode == 1 and r.stdout == ""
    assert not execs(box)


def test_copy_container_without_the_target_marker_is_refused(tmp_path):
    """Names look ordinary, but the only container that mounts the volume belongs to the copy stack."""
    box = backup_box(tmp_path, containers={"cid-x": ("hc-next-hindsight-1", [("plain_pg0", PG0_DEST)])},
                     host_volumes=["plain_pg0"], compose_name="plain_pg0", compose_project="plain")
    r = box.run("scripts/backup.sh", HC_COMPOSE_PROJECT="plain", HC_PG0_VOLUME="plain_pg0")
    assert r.returncode == 1 and r.stdout == ""
    assert "HC_TARGET=next" in r.stderr
    assert not execs(box)


@pytest.mark.parametrize("env, compose_name", [
    ({"HC_PG0_VOLUME": "_pg0"}, DEFAULT_VOL),       # from the caller
    ({}, "_pg0"),                                   # compose rendered ${COMPOSE_PROJECT_NAME} as empty
    ({}, ""),
], ids=["caller-underscore", "compose-underscore", "compose-empty"])
def test_unprefixed_or_empty_volume_name_is_exit_1(tmp_path, env, compose_name):
    box = backup_box(tmp_path, containers={"cid-x": ("c-x", [("_pg0", PG0_DEST)])}, host_volumes=["_pg0"],
                     compose_name=compose_name)
    r = box.run("scripts/backup.sh", **env)
    assert r.returncode == 1 and r.stdout == ""
    assert "project prefix" in r.stderr or "empty" in r.stderr
    assert not execs(box)


@pytest.mark.parametrize("env_file", ["envs/typo.env", "/nonexistent/next.env"], ids=["relative", "absolute"])
def test_explicit_env_file_that_does_not_exist_is_exit_1(tmp_path, env_file):
    box = two_volume_box(tmp_path, "vol-x")
    r = box.run("scripts/backup.sh", HC_ENV_FILE=env_file)
    assert r.returncode == 1 and r.stdout == ""
    assert "HC_ENV_FILE" in r.stderr
    assert not box.calls("docker")


def test_default_env_file_may_be_absent(tmp_path):
    box = two_volume_box(tmp_path, "vol-x")
    assert not (box.root / ".env.local").exists()
    r = box.run("scripts/backup.sh")
    assert r.returncode == 0, r.stdout + r.stderr


# --------------------------------------------------------------------------- compose invocation + project resolution

def config_call(box):
    return [c["argv"] for c in box.calls("docker") if c["argv"][:1] == ["compose"] and "config" in c["argv"]][0]


def test_compose_call_shape_and_resolved_project_with_defaults(tmp_path):
    box = two_volume_box(tmp_path, "vol-x", env_file="")
    r = box.run("scripts/backup.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    argv = config_call(box)
    assert argv[:7] == ["compose", "--env-file", str(box.root / ".env.local"), "--project-directory", str(box.root),
                        "-f", str(box.root / "docker-compose.yml")]
    assert "-p" not in argv                                    # default: compose names the project itself
    assert TARGET_LINE.match(target_lines(r.stderr)[0]).group(2) == "repo"    # basename of the repo root


def test_compose_call_omits_env_file_when_there_is_none_and_passes_project_and_files(tmp_path):
    box = two_volume_box(tmp_path, "vol-x")
    (box.root / "override.yml").write_text("services: {}\n")
    r = box.run("scripts/backup.sh", HC_COMPOSE_PROJECT="hindsight-crew",
                HC_COMPOSE_FILES="docker-compose.yml:%s" % (box.root / "override.yml"))
    assert r.returncode == 0, r.stdout + r.stderr
    argv = config_call(box)
    assert "--env-file" not in argv
    assert argv[:9] == ["compose", "--project-directory", str(box.root), "-p", "hindsight-crew",
                        "-f", str(box.root / "docker-compose.yml"), "-f", str(box.root / "override.yml")]
    assert TARGET_LINE.match(target_lines(r.stderr)[0]).group(2) == "hindsight-crew"


def test_caller_project_beats_the_env_file_and_compose_project_name_comes_after(tmp_path):
    box = two_volume_box(tmp_path, "vol-x", env_file="HC_COMPOSE_PROJECT=from-file\n")
    r = box.run("scripts/backup.sh", HC_COMPOSE_PROJECT="from-caller")
    assert TARGET_LINE.match(target_lines(r.stderr)[0]).group(2) == "from-caller"
    assert config_call(box)[config_call(box).index("-p") + 1] == "from-caller"
    box = two_volume_box(tmp_path / "b", "vol-x", env_file="HC_COMPOSE_PROJECT=from-file\n")
    r = box.run("scripts/backup.sh")
    assert TARGET_LINE.match(target_lines(r.stderr)[0]).group(2) == "from-file"
    box = two_volume_box(tmp_path / "c", "vol-x", env_file="COMPOSE_PROJECT_NAME=named-by-env\n")
    r = box.run("scripts/backup.sh")
    assert TARGET_LINE.match(target_lines(r.stderr)[0]).group(2) == "named-by-env"
    assert "-p" not in config_call(box)


# --------------------------------------------------------------------------- static lock

def test_no_hard_coded_production_volume_name_in_backup_or_compose():
    r = subprocess.run(["git", "grep", "-n", "hindsight-crew_pg0", "--", "scripts/backup.sh", "docker-compose.yml"],
                       cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    assert r.returncode == 1 and r.stdout == "", r.stdout
