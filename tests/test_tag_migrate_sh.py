"""scripts/tag-migrate.sh 가 호스트 파일 플래그 다섯 개를 하나의 표 기반 분기로 주입·회수한다 (issue #43, #109, #128, #143).

플래그 -> 방향 표: --apply-manifest / --ids-file / --manifest 는 입력(주입), --manifest-out 은 출력(회수),
--state 는 입력+출력(있으면 주입, 실행 뒤 회수). 컨테이너는 호스트 파일을 못 보므로 래퍼가 컨테이너 임시 디렉터리에
스테이징하고, 실행이 끝나면(성공·실패 무관) 출력 파일을 호스트 경로로 되돌린다.
원인 판정(#109): argparse 는 `--flag=value` 를 받지만 셸 case 는 공백 형태만 잡았다. 원인 판정(#143): --manifest·--manifest-out·--state
는 주입 분기에 아예 없어 호스트 경로가 그대로 컨테이너로 넘어갔다.

운영 스택은 건드리지 않는다: 스크립트 복사본을 임시 디렉터리에 두고, PATH 앞에 가짜 `docker` 를 둔다. 가짜 docker 는
`docker compose exec -T hindsight python ...` 만 받아 컨테이너 대신 로컬 python 으로 돌리고(TMPDIR 은 임시 디렉터리),
그 밖의 호출은 99 로 실패한다. 호출마다 인자·표준입력을 기록한다. `.env.local` 은 임시 디렉터리 안의 빈 파일이고 HOME 도 가짜다.
"""
import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "tag-migrate.sh"

FAKE_DOCKER = """#!/usr/bin/env bash
n=$(find "$FAKE_DOCKER_CALLS" -name '*.args' | wc -l | tr -d ' ')
n=$((n + 1))
printf '%s\\0' "$@" > "$FAKE_DOCKER_CALLS/$n.args"
cat > "$FAKE_DOCKER_CALLS/$n.stdin"
[ "${1-} ${2-} ${3-} ${4-} ${5-}" = "compose exec -T hindsight python" ] || {
  echo "fake docker: unexpected invocation: $*" >&2
  exit 99
}
if [ -n "${FAKE_FAIL_ON-}" ] && grep -q -- "$FAKE_FAIL_ON" "$FAKE_DOCKER_CALLS/$n.stdin"; then
  exit 5
fi
exec "$FAKE_PYTHON" "${@:6}" < "$FAKE_DOCKER_CALLS/$n.stdin"
"""

# 컨테이너 쪽 프로그램 대역: 인자로 받은 경로의 내용을 기록하고, 출력 플래그가 가리키는 경로에 파일을 쓴다.
PROBE_PROGRAM = '''\
import json, os, sys
argv = sys.argv[1:]


def value(flag):
    return argv[argv.index(flag) + 1] if flag in argv else None


read = {}
for flag in ("--apply-manifest", "--ids-file", "--manifest", "--state"):
    path = value(flag)
    if path is not None and os.path.isfile(path):
        read[flag] = open(path, encoding="utf-8").read()
manifest, state = value("--manifest"), value("--state")
if manifest and not state and os.path.isfile(manifest + ".state.json"):
    read["implicit-state"] = open(manifest + ".state.json", encoding="utf-8").read()
with open(os.environ["PROBE_OUT"], "w", encoding="utf-8") as handle:
    json.dump({"argv": argv, "read": read}, handle)
out = value("--manifest-out")
if out and os.environ.get("PROBE_WRITE_OUT", "1") == "1":
    open(out, "w", encoding="utf-8").write('{"written": "manifest-out"}')
    print("MANIFEST_PATH " + out)
target = state or (manifest + ".state.json" if manifest else None)
if target and os.environ.get("PROBE_WRITE_STATE", "1") == "1":
    open(target, "w", encoding="utf-8").write('{"written": "state"}')
sys.exit(int(os.environ.get("PROBE_EXIT", "0")))
'''


def test_script_passes_bash_syntax_check():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


class Result:
    def __init__(self, proc, calls, probe):
        self.proc = proc
        self.calls = calls        # [{"args": [...], "stdin": str}] 호출 순서
        self.probe = probe        # 컨테이너 대역이 기록한 {"argv", "read"} 또는 None(실행 안 됨)


class Sandbox:
    def __init__(self, tmp_path, program, with_policies):
        self.tmp = tmp_path
        self.repo = tmp_path / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        shutil.copy(SCRIPT, self.repo / "scripts" / "tag-migrate.sh")
        shutil.copy(SCRIPT.parent / "hc-host-env.sh", self.repo / "scripts" / "hc-host-env.sh")
        (self.repo / "scripts" / "tag_migrate.py").write_text(program, encoding="utf-8")
        (self.repo / ".env.local").write_text("", encoding="utf-8")
        if with_policies:
            (self.repo / "policies").mkdir()
            shutil.copy(ROOT / "tests" / "fixtures" / "policies" / "sample" / "bank-policies.json", self.repo / "policies" / "bank-policies.json")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text(FAKE_DOCKER, encoding="utf-8")
        docker.chmod(0o755)
        self.caller = tmp_path / "caller"
        self.caller.mkdir()
        (self.caller / "input.json").write_text(json.dumps({"version": 2, "ids": ["a"]}), encoding="utf-8")
        (self.caller / "state-in.json").write_text(json.dumps({"version": 2, "ids": ["a"]}), encoding="utf-8")
        self.home = tmp_path / "home"                    # 가짜 HOME: 실 HOME 의 파일은 읽지 않는다
        (self.home / "plans").mkdir(parents=True)
        (self.home / "plans" / "input.json").write_text(json.dumps({"version": 2, "ids": ["h"]}), encoding="utf-8")
        self.ctmp = tmp_path / "container-tmp"           # 컨테이너 /tmp 대역
        self.ctmp.mkdir()
        self.calls_dir = tmp_path / "calls"
        self.calls_dir.mkdir()
        self.probe_out = tmp_path / "probe.json"
        self.env = {
            "HOME": str(self.home),
            "PATH": "%s%s%s" % (bin_dir, os.pathsep, os.environ["PATH"]),
            "TMPDIR": str(self.ctmp),
            "FAKE_DOCKER_CALLS": str(self.calls_dir),
            "FAKE_PYTHON": sys.executable,
            "PROBE_OUT": str(self.probe_out),
            "HC_PERSONAL_BANK": "personal-demo",   # no default in the wrapper (issue #186)
        }

    def run(self, *args, **extra_env):
        env = dict(self.env, **extra_env)
        proc = subprocess.run(["bash", str(self.repo / "scripts" / "tag-migrate.sh"), *args],
                              cwd=self.caller, env=env, capture_output=True, text=True)
        calls = []
        for args_file in sorted(self.calls_dir.glob("*.args"), key=lambda p: int(p.stem)):
            raw = args_file.read_bytes().decode("utf-8")
            calls.append({"args": raw.split("\0")[:-1],
                          "stdin": args_file.with_suffix(".stdin").read_text(encoding="utf-8")})
        probe = json.loads(self.probe_out.read_text(encoding="utf-8")) if self.probe_out.exists() else None
        return Result(proc, calls, probe)

    def reset(self):
        for path in list(self.calls_dir.iterdir()):
            path.unlink()
        if self.probe_out.exists():
            self.probe_out.unlink()


@pytest.fixture
def sandbox(tmp_path):
    return Sandbox(tmp_path, PROBE_PROGRAM, with_policies=True)


def main_call(result):
    """본 실행 exec: 스테이징(mkdtemp)·회수(b64encode)·정리(-c) 호출이 아니라 tag_migrate.py 를 받는 `python -` 호출."""
    candidates = [c for c in result.calls if c["args"][5:6] == ["-"]
                  and "mkdtemp" not in c["stdin"] and "b64encode" not in c["stdin"]]
    assert len(candidates) == 1, result.calls
    return candidates[0]


def staging_dir_leftovers(sandbox):
    return sorted(p.name for p in sandbox.ctmp.iterdir())


def assert_container_path(sandbox, path, host_name):
    """컨테이너에는 호스트 경로가 아니라 컨테이너 임시 디렉터리 안의 경로가 간다."""
    assert path.startswith(str(sandbox.ctmp) + os.sep), path
    assert host_name not in path


def test_fake_docker_rejects_everything_but_compose_exec(sandbox):
    """실 docker 호출 0의 근거: PATH 앞의 docker 는 가짜이고, compose exec 이 아닌 호출은 99 로 실패한다."""
    assert shutil.which("docker", path=sandbox.env["PATH"]) == str(sandbox.tmp / "bin" / "docker")
    proc = subprocess.run(["docker", "ps"], env=sandbox.env, capture_output=True, text=True)
    assert proc.returncode == 99 and "unexpected invocation" in proc.stderr


# --- 입력 플래그: 호스트 파일이 컨테이너 임시 경로로 주입된다 ----------------------------------------------------------
# (인자, 컨테이너가 읽어야 할 플래그, 호스트에서 읽혀야 할 내용)
INPUT_JSON = json.dumps({"version": 2, "ids": ["a"]})
INPUT_FORMS = [
    pytest.param(["--apply-manifest", "input.json"], "--apply-manifest", id="apply-manifest-space"),
    pytest.param(["--apply-manifest=input.json"], "--apply-manifest", id="apply-manifest-equals"),
    pytest.param(["--export-labels", "--ids-file", "input.json"], "--ids-file", id="ids-file-space"),
    pytest.param(["--export-labels", "--ids-file=input.json"], "--ids-file", id="ids-file-equals"),
    pytest.param(["--live", "--manifest", "input.json"], "--manifest", id="manifest-space"),
    pytest.param(["--live", "--manifest=input.json"], "--manifest", id="manifest-equals"),
    pytest.param(["--live", "--manifest", "input.json", "--state", "state-in.json"], "--state", id="state-space"),
    pytest.param(["--live", "--manifest=input.json", "--state=state-in.json"], "--state", id="state-equals"),
]


@pytest.mark.parametrize("args,flag", INPUT_FORMS)
def test_input_flags_inject_the_host_file_into_a_container_temp_path(sandbox, args, flag):
    result = sandbox.run(*args)
    assert result.proc.returncode == 0, result.proc.stderr
    argv = result.probe["argv"]
    assert not any(a.startswith(flag + "=") for a in argv)
    path = argv[argv.index(flag) + 1]
    assert_container_path(sandbox, path, "input.json")
    assert result.probe["read"][flag] == INPUT_JSON
    # 호스트 경로 리터럴은 컨테이너 인자 어디에도 가지 않는다(상대 경로 input.json·state-in.json·호스트 절대 경로 모두)
    for call in result.calls:
        assert not any("input.json" in a or "state-in.json" in a for a in call["args"])
        assert str(sandbox.caller) not in " ".join(call["args"])
    assert staging_dir_leftovers(sandbox) == []        # 임시 디렉터리는 끝나면 지워진다


# --- 출력 플래그: 컨테이너가 쓴 파일이 호스트 경로로 회수된다 ------------------------------------------------------------
OUTPUT_FORMS = [
    pytest.param(["--dry-run", "--manifest-out", "out/plan.json"], "out/plan.json", '{"written": "manifest-out"}',
                 id="manifest-out-space"),
    pytest.param(["--dry-run", "--manifest-out=out/plan.json"], "out/plan.json", '{"written": "manifest-out"}',
                 id="manifest-out-equals"),
    pytest.param(["--live", "--manifest", "input.json", "--state", "st.json"], "st.json", '{"written": "state"}',
                 id="state-space"),
    pytest.param(["--live", "--manifest=input.json", "--state=st.json"], "st.json", '{"written": "state"}',
                 id="state-equals"),
    # --state 를 생략하면 기본 경로 MANIFEST.state.json 을 호스트 쪽에서 같은 규칙으로 회수한다
    pytest.param(["--live", "--manifest", "input.json"], "input.json.state.json", '{"written": "state"}',
                 id="default-state-space"),
    pytest.param(["--live", "--manifest=input.json"], "input.json.state.json", '{"written": "state"}',
                 id="default-state-equals"),
]


@pytest.mark.parametrize("args,host_rel,content", OUTPUT_FORMS)
def test_output_flags_bring_the_container_file_back_to_the_host_path(sandbox, args, host_rel, content):
    result = sandbox.run(*args)
    assert result.proc.returncode == 0, result.proc.stderr
    host = sandbox.caller / host_rel
    assert host.read_text(encoding="utf-8") == content
    # 호스트 상대 경로가 컨테이너로 가지 않았고(주입 분기를 탔다), 컨테이너가 쓴 곳은 컨테이너 임시 경로다
    flag_values = [a for a in result.probe["argv"] if a.startswith(str(sandbox.ctmp))]
    assert flag_values and all(host_rel not in v for v in flag_values)
    assert not (sandbox.repo / host_rel).exists()      # 리포 루트(스크립트가 cd 한 곳)에 새지 않는다
    assert staging_dir_leftovers(sandbox) == []
    assert not list(sandbox.caller.glob("**/*.tmp"))


def test_state_given_explicitly_suppresses_the_default_state_retrieval(sandbox):
    result = sandbox.run("--live", "--manifest", "input.json", "--state", "st.json")
    assert result.proc.returncode == 0, result.proc.stderr
    assert (sandbox.caller / "st.json").exists()
    assert not (sandbox.caller / "input.json.state.json").exists()


# --- --state 는 입력이기도 하다: 호스트에 있으면 주입돼 재개 체크포인트가 된다 ---------------------------------------------
STATE_FORMS = [
    pytest.param(["--live", "--manifest", "input.json", "--state", "st.json"], id="manifest-state-space"),
    pytest.param(["--live", "--manifest=input.json", "--state=st.json"], id="manifest-state-equals"),
    pytest.param(["--apply-manifest", "input.json", "--state", "st.json"], id="apply-manifest-state-space"),
    pytest.param(["--apply-manifest=input.json", "--state=st.json"], id="apply-manifest-state-equals"),
]


@pytest.mark.parametrize("args", STATE_FORMS)
def test_existing_host_state_is_injected_and_the_updated_state_comes_back(sandbox, args):
    """--apply-manifest + --state 는 정상 용례(재개 체크포인트)라 '한 실행에 하나' 제약에 걸리지 않는다."""
    (sandbox.caller / "st.json").write_text('{"processed": ["x"]}', encoding="utf-8")
    result = sandbox.run(*args)
    assert result.proc.returncode == 0, result.proc.stderr
    assert result.probe["read"]["--state"] == '{"processed": ["x"]}'
    assert INPUT_JSON in result.probe["read"].values()
    assert (sandbox.caller / "st.json").read_text(encoding="utf-8") == '{"written": "state"}'


def test_state_is_brought_back_even_when_the_run_fails(sandbox):
    """체크포인트는 실패한 실행이 남긴 것이 가장 중요하다: 종료 코드는 그대로, 상태 파일은 회수한다."""
    result = sandbox.run("--live", "--manifest", "input.json", "--state", "st.json", PROBE_EXIT="3")
    assert result.proc.returncode == 3
    assert (sandbox.caller / "st.json").read_text(encoding="utf-8") == '{"written": "state"}'
    assert staging_dir_leftovers(sandbox) == []


def test_manifest_out_that_the_container_never_wrote_is_not_created_or_overwritten(sandbox):
    result = sandbox.run("--dry-run", "--manifest-out", "plan.json", PROBE_WRITE_OUT="0")
    assert result.proc.returncode == 0, result.proc.stderr
    assert not (sandbox.caller / "plan.json").exists()         # 빈 파일·부분 파일 금지
    (sandbox.caller / "plan.json").write_text("KEEP", encoding="utf-8")
    sandbox.reset()
    result = sandbox.run("--dry-run", "--manifest-out", "plan.json", PROBE_WRITE_OUT="0")
    assert result.proc.returncode == 0, result.proc.stderr
    assert (sandbox.caller / "plan.json").read_text(encoding="utf-8") == "KEEP"


def test_retrieval_failure_is_not_swallowed_and_does_not_mask_the_run_status(sandbox):
    # 회수 exec 만 실패(회수 스크립트에만 있는 문자열로 지목): 본 실행은 성공했어도 종료 코드가 0 이면 안 된다
    result = sandbox.run("--dry-run", "--manifest-out", "plan.json", FAKE_FAIL_ON="b64encode")
    assert result.proc.returncode != 0
    assert not (sandbox.caller / "plan.json").exists()
    # 회수가 실패하면 컨테이너 쪽이 유일한 사본일 수 있다: 임시 디렉터리를 지우지 않고 복구 경로를 알린다
    leftovers = staging_dir_leftovers(sandbox)
    assert len(leftovers) == 1 and str(sandbox.ctmp / leftovers[0]) in result.proc.stderr
    assert "manual recovery" in result.proc.stderr
    # 본 실행이 실패했으면 그 종료 코드가 그대로다(회수 실패가 덮지도, 성공으로 만들지도 않는다)
    sandbox.reset()
    result = sandbox.run("--dry-run", "--manifest-out", "plan.json", FAKE_FAIL_ON="b64encode", PROBE_EXIT="3")
    assert result.proc.returncode == 3
    assert len(staging_dir_leftovers(sandbox)) == 2        # 앞 실행이 남긴 것과 이번 실행이 남긴 것


# --- 입력 검증 --------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("flag", ["--apply-manifest", "--ids-file", "--manifest", "--manifest-out", "--state"])
def test_equals_form_without_a_value_is_rejected(sandbox, flag):
    result = sandbox.run(flag + "=")
    assert result.proc.returncode == 2 and "requires a FILE" in result.proc.stderr
    assert result.calls == []


@pytest.mark.parametrize("flag", ["--apply-manifest", "--ids-file", "--manifest", "--manifest-out", "--state"])
def test_flag_at_the_end_without_a_value_is_rejected(sandbox, flag):
    result = sandbox.run(flag)
    assert result.proc.returncode == 2 and "requires a FILE" in result.proc.stderr
    assert result.calls == []


@pytest.mark.parametrize("args", [
    pytest.param(["--apply-manifest=input.json", "--ids-file", "input.json"], id="mixed-forms"),
    pytest.param(["--ids-file", "input.json", "--apply-manifest", "input.json"], id="space-forms"),
    pytest.param(["--apply-manifest", "input.json", "--apply-manifest=input.json"], id="same-flag-twice"),
])
def test_only_one_of_apply_manifest_and_ids_file_per_run(sandbox, args):
    result = sandbox.run(*args)
    assert result.proc.returncode == 2 and "only one" in result.proc.stderr
    assert result.calls == []


@pytest.mark.parametrize("flag", ["--manifest", "--manifest-out", "--state"])
def test_the_same_host_file_flag_twice_is_rejected(sandbox, flag):
    result = sandbox.run("--dry-run", flag, "input.json", flag + "=input.json")
    assert result.proc.returncode == 2 and "more than once" in result.proc.stderr
    assert result.calls == []


@pytest.mark.parametrize("args", [
    pytest.param(["--apply-manifest", "nope.json"], id="apply-manifest"),
    pytest.param(["--export-labels", "--ids-file=nope.json"], id="ids-file"),
    pytest.param(["--live", "--manifest", "nope.json"], id="manifest"),
])
def test_missing_host_input_file_fails_before_any_docker_call(sandbox, args):
    result = sandbox.run(*args)
    assert result.proc.returncode == 2 and "nope.json" in result.proc.stderr
    assert result.calls == []


# --- final-review: 값 누락·같은 파일·--dry-run 에서의 --manifest·stdout 경로 ----------------------------------------------
@pytest.mark.parametrize("flag", ["--apply-manifest", "--ids-file", "--manifest", "--manifest-out", "--state"])
def test_option_like_next_token_is_not_consumed_as_the_file(sandbox, flag):
    """`--state --dry-run` 처럼 값을 빠뜨리면 다음 옵션이 경로로 먹혀 사라지고 live 가 실제로 돈다(argparse 는 값 누락으로 거부했다)."""
    result = sandbox.run("--live", "--manifest", "input.json", flag, "--dry-run") if flag != "--manifest" else \
        sandbox.run("--live", flag, "--dry-run")
    assert result.proc.returncode == 2 and "requires a FILE" in result.proc.stderr
    assert result.calls == []
    assert not (sandbox.caller / "--dry-run").exists()


def test_a_file_named_like_an_option_still_works_in_the_equals_form(sandbox):
    (sandbox.caller / "--odd.json").write_text(INPUT_JSON, encoding="utf-8")
    result = sandbox.run("--live", "--manifest=--odd.json")
    assert result.proc.returncode == 0, result.proc.stderr
    assert result.probe["read"]["--manifest"] == INPUT_JSON


@pytest.mark.parametrize("args,pair", [
    pytest.param(["--live", "--manifest", "input.json", "--state", "input.json"], "--manifest and --state", id="state-is-manifest"),
    pytest.param(["--live", "--manifest=input.json", "--state=./input.json"], "--manifest and --state", id="state-is-manifest-dot"),
    pytest.param(["--live", "--manifest=input.json", "--state=sub/../input.json"], "--manifest and --state", id="state-is-manifest-dotdot"),
    pytest.param(["--live", "--manifest=input.json", "--state=INPUT.JSON"], "--manifest and --state", id="state-differs-only-by-case"),
    pytest.param(["--dry-run", "--manifest-out", "o.json", "--state", "o.json"], "--manifest-out and --state", id="out-is-state"),
    pytest.param(["--live", "--manifest", "input.json", "--manifest-out", "input.json.state.json"],
                 "--manifest-out and (default state)", id="out-is-default-state"),
])
def test_two_flags_pointing_at_the_same_host_file_are_rejected(sandbox, args, pair):
    """회수가 입력(승인된 manifest)을 체크포인트로 덮어쓰는 것을 막는다."""
    before = (sandbox.caller / "input.json").read_text(encoding="utf-8")
    result = sandbox.run(*args)
    assert result.proc.returncode == 2 and "%s point to the same file" % pair in result.proc.stderr
    assert result.calls == []
    assert (sandbox.caller / "input.json").read_text(encoding="utf-8") == before


def test_dry_run_does_not_need_or_touch_the_manifest_or_its_default_state(sandbox):
    """--dry-run 은 --manifest 를 읽지 않고 기본 state 도 쓰지 않는다: 없는 manifest 를 거부하지 않고, 있는 체크포인트를 다시 쓰지 않는다."""
    result = sandbox.run("--dry-run", "--manifest", "plan.json", "--manifest-out", "plan.json")
    assert result.proc.returncode == 0, result.proc.stderr
    assert (sandbox.caller / "plan.json").read_text(encoding="utf-8") == '{"written": "manifest-out"}'
    sandbox.reset()
    state = sandbox.caller / "input.json.state.json"
    state.write_text("CHECKPOINT", encoding="utf-8")
    link = sandbox.caller / "linked.json.state.json"
    link.symlink_to(state)
    (sandbox.caller / "linked.json").write_text(INPUT_JSON, encoding="utf-8")
    result = sandbox.run("--dry-run", "--manifest", "linked.json")
    assert result.proc.returncode == 0, result.proc.stderr
    assert link.is_symlink() and state.read_text(encoding="utf-8") == "CHECKPOINT"      # 다시 쓰이지 않았다
    assert all(base64.b64encode(b"CHECKPOINT").decode() not in call["stdin"] for call in result.calls)


@pytest.mark.parametrize("code", [0, 1, 3, 42])
def test_stdout_rewrite_pipe_preserves_the_container_exit_status(sandbox, code):
    """stdout 치환 파이프(| sed)가 본 실행 종료 코드를 가리거나 바꾸지 않는다(성공·실패·임의 코드), 실패 때도 치환은 적용된다."""
    result = sandbox.run("--dry-run", "--manifest-out", "plan.json", PROBE_EXIT=str(code))
    assert result.proc.returncode == code, result.proc.stderr
    assert "MANIFEST_PATH %s" % (sandbox.caller / "plan.json") in result.proc.stdout
    assert str(sandbox.ctmp) not in result.proc.stdout


def test_stdout_shows_the_host_path_not_the_container_temp_path(sandbox):
    result = sandbox.run("--dry-run", "--manifest-out", "out/plan.json")
    assert result.proc.returncode == 0, result.proc.stderr
    assert "MANIFEST_PATH %s" % (sandbox.caller / "out" / "plan.json") in result.proc.stdout
    assert str(sandbox.ctmp) not in result.proc.stdout


# --- `~` 확장: 호출 셸은 `--flag=~/x` 의 ~ 를 펼치지 않으므로 스크립트가 $HOME 기준으로 펼친다 -------------------------------
TILDE_FORMS = [
    pytest.param(["--apply-manifest=~/plans/input.json"], "--apply-manifest", id="apply-manifest-equals-tilde"),
    pytest.param(["--export-labels", "--ids-file=~/plans/input.json"], "--ids-file", id="ids-file-equals-tilde"),
    pytest.param(["--live", "--manifest=~/plans/input.json"], "--manifest", id="manifest-equals-tilde"),
    pytest.param(["--live", "--manifest=~/plans/input.json", "--state=~/plans/st.json"], "--state",
                 id="state-equals-tilde"),
]


@pytest.mark.parametrize("args,flag", TILDE_FORMS)
def test_equals_form_expands_a_leading_tilde_to_home(sandbox, args, flag):
    """인자는 shell=False 로 그대로 넘어가 ~ 는 리터럴이고, HOME 은 임시 디렉터리의 가짜 값이다(final-review C5)."""
    result = sandbox.run(*args)
    assert result.proc.returncode == 0, result.proc.stderr
    if flag == "--state":
        assert (sandbox.home / "plans" / "st.json").read_text(encoding="utf-8") == '{"written": "state"}'
    else:
        assert json.loads(result.probe["read"][flag]) == {"version": 2, "ids": ["h"]}


def test_manifest_out_equals_form_expands_a_leading_tilde_to_home(sandbox):
    result = sandbox.run("--dry-run", "--manifest-out=~/plans/plan.json")
    assert result.proc.returncode == 0, result.proc.stderr
    assert (sandbox.home / "plans" / "plan.json").read_text(encoding="utf-8") == '{"written": "manifest-out"}'
    assert not (sandbox.caller / "~").exists()


def test_absolute_host_paths_work_for_every_flag(sandbox):
    out = sandbox.tmp / "elsewhere" / "plan.json"
    state = sandbox.tmp / "elsewhere" / "st.json"
    result = sandbox.run("--dry-run", "--manifest", str(sandbox.caller / "input.json"),
                         "--manifest-out", str(out), "--state", str(state))
    assert result.proc.returncode == 0, result.proc.stderr
    assert out.read_text(encoding="utf-8") == '{"written": "manifest-out"}'
    assert state.read_text(encoding="utf-8") == '{"written": "state"}'


# --- 정책 alias prelude: 기존 경로는 그대로, manifest 모드(#43)는 주입하지 않는다 ---------------------------------------------
def test_alias_prelude_is_sent_for_plan_runs_but_not_for_manifest_mode(sandbox):
    plan = sandbox.run("--dry-run", "--manifest-out", "plan.json")
    assert "ALIAS_MAP = " in main_call(plan)["stdin"] and "DELETE_TAGS = set(" in main_call(plan)["stdin"]
    sandbox.reset()
    apply_run = sandbox.run("--apply-manifest", "input.json")
    assert all("ALIAS_MAP = " not in call["stdin"] for call in apply_run.calls)


# --- 뱅크 id: HC_PERSONAL_BANK 하나, 기본값 없음 (issue #186) ------------------------------------------------------------
def test_missing_bank_fails_with_config_missing_before_any_docker_call(sandbox):
    del sandbox.env["HC_PERSONAL_BANK"]
    result = sandbox.run("--dry-run")
    assert result.proc.returncode == 2
    assert "TAG_MIGRATE_FAIL - config_missing HC_PERSONAL_BANK" in result.proc.stderr
    assert result.calls == []


@pytest.mark.parametrize("bank", ["bad/bank", "../x", "a b", "x;y", "a'b"])
def test_bank_with_path_or_quote_characters_is_rejected(sandbox, bank):
    result = sandbox.run("--dry-run", HC_PERSONAL_BANK=bank)
    assert result.proc.returncode == 2
    assert "invalid HC_PERSONAL_BANK" in result.proc.stderr
    assert result.calls == []


@pytest.mark.parametrize("source", ["env", "host-file"])
@pytest.mark.parametrize("args", [["--dry-run"], ["--apply-manifest", "input.json"]], ids=["plan-run", "manifest-mode"])
def test_bank_comes_from_the_setting_and_reaches_the_container_prelude(sandbox, source, args):
    """Both paths get `PERSONAL_BANK = '<bank>'`; the plan path also picks the alias table of THAT bank."""
    (sandbox.repo / "policies" / "bank-policies.json").write_text(
        json.dumps({"banks": {"bank-x": {"tag_aliases": {"aside": "project:aside"}}}}), encoding="utf-8")
    if source == "env":
        sandbox.env["HC_PERSONAL_BANK"] = "bank-x"
    else:
        del sandbox.env["HC_PERSONAL_BANK"]
        (sandbox.repo / ".env.host.local").write_text("# host settings\nHC_PERSONAL_BANK = \"bank-x\"\n", encoding="utf-8")
    result = sandbox.run(*args)
    assert result.proc.returncode == 0, result.proc.stderr
    stdin = main_call(result)["stdin"]
    assert stdin.startswith("PERSONAL_BANK = 'bank-x'\n")
    assert ("ALIAS_MAP = {'aside': 'project:aside'}" in stdin) == (args[0] == "--dry-run")


def test_alias_table_comes_from_the_local_overlay_when_it_declares_the_bank(sandbox):
    """issue #187: policies/local/ 에 그 뱅크가 있으면 게이트웨이 로더처럼 뱅크 통째로 대체한다."""
    (sandbox.repo / "policies" / "bank-policies.json").write_text(
        json.dumps({"banks": {"bank-x": {"tag_aliases": {"aside": "project:aside"}}}}), encoding="utf-8")
    (sandbox.repo / "policies" / "local").mkdir()
    (sandbox.repo / "policies" / "local" / "bank-policies.json").write_text(
        json.dumps({"banks": {"bank-x": {"tag_aliases": {"teak-comet": "project:teak-comet"}}}}), encoding="utf-8")
    sandbox.env["HC_PERSONAL_BANK"] = "bank-x"
    result = sandbox.run("--dry-run")
    assert result.proc.returncode == 0, result.proc.stderr
    assert "ALIAS_MAP = {'teak-comet': 'project:teak-comet'}" in main_call(result)["stdin"]


def _overlay(sandbox, tracked, local, local_rel=("policies", "local", "bank-policies.json")):
    (sandbox.repo / "policies" / "bank-policies.json").write_text(json.dumps(tracked), encoding="utf-8")
    path = sandbox.repo.joinpath(*local_rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(local), encoding="utf-8")
    sandbox.env["HC_PERSONAL_BANK"] = "bank-x"
    return path


def test_alias_table_honours_hc_bank_policies_local_like_the_gateway(sandbox):
    """final-review #187 #2: the overlay path comes from HC_BANK_POLICIES_LOCAL when it is set."""
    _overlay(sandbox, {"banks": {"bank-x": {"tag_aliases": {"aside": "project:aside"}}}},
             {"banks": {"bank-x": {"tag_aliases": {"teak-comet": "project:teak-comet"}}}})
    other = _overlay(sandbox, {"banks": {"bank-x": {"tag_aliases": {"aside": "project:aside"}}}},
                     {"banks": {"bank-x": {"tag_aliases": {"willow": "project:willow"}}}},
                     local_rel=("elsewhere", "overlay.json"))
    result = sandbox.run("--dry-run", HC_BANK_POLICIES_LOCAL=str(other))
    assert result.proc.returncode == 0, result.proc.stderr
    assert "ALIAS_MAP = {'willow': 'project:willow'}" in main_call(result)["stdin"]


def test_overlay_with_keys_other_than_banks_is_refused_like_the_gateway(sandbox):
    _overlay(sandbox, {"banks": {"bank-x": {"tag_aliases": {"aside": "project:aside"}}}},
             {"defaults": {"tags_required": False}, "banks": {}})
    result = sandbox.run("--dry-run")
    assert result.proc.returncode != 0
    assert "may hold only banks" in result.proc.stderr
    assert not any("ALIAS_MAP" in call["stdin"] for call in result.calls)


def test_local_bank_without_tag_aliases_inherits_the_tracked_defaults(sandbox):
    _overlay(sandbox, {"defaults": {"tag_aliases": {"aside": "project:aside"}}, "banks": {}},
             {"banks": {"bank-x": {"tags_required": True}}})
    result = sandbox.run("--dry-run")
    assert result.proc.returncode == 0, result.proc.stderr
    assert "ALIAS_MAP = {'aside': 'project:aside'}" in main_call(result)["stdin"]


def test_plain_dry_run_without_host_file_flags_is_a_single_exec(sandbox):
    result = sandbox.run("--dry-run")
    assert result.proc.returncode == 0, result.proc.stderr
    assert len(result.calls) == 1
    assert result.calls[0]["args"][:6] == ["compose", "exec", "-T", "hindsight", "python", "-"]


def test_staged_content_is_not_passed_on_the_command_line(sandbox):
    """매니페스트·상태 내용은 표준입력으로만 간다(프로세스 인자에 남지 않는다)."""
    (sandbox.caller / "st.json").write_text('{"processed": ["doc-id-x"]}', encoding="utf-8")
    result = sandbox.run("--live", "--manifest", "input.json", "--state", "st.json")
    assert result.proc.returncode == 0, result.proc.stderr
    staged = base64.b64encode(b'{"processed": ["doc-id-x"]}').decode()
    assert any(staged in call["stdin"] for call in result.calls)
    for call in result.calls:
        assert staged not in " ".join(call["args"]) and "doc-id-x" not in " ".join(call["args"])


# --- 이슈 #128/#143: 약어는 주입 분기에 들어가지 않고, 컨테이너 쪽 argparse 가 거부한다 -----------------------------------------
# 실제 tag_migrate.py 의 parse_args 까지 태운다(parse_args 는 네트워크·토큰 이전에 끝난다: 약어는 거기서 exit 2).
@pytest.fixture
def real_parser_sandbox(tmp_path):
    box = Sandbox(tmp_path, "", with_policies=True)
    shutil.copy(ROOT / "scripts" / "tag_migrate.py", box.repo / "scripts" / "tag_migrate.py")
    return box


ABBREV_SHELL_FORMS = [
    pytest.param(["--dry-run", "--apply", "input.json"], "--apply", id="apply-space"),
    pytest.param(["--dry-run", "--apply=input.json"], "--apply=input.json", id="apply-equals"),
    pytest.param(["--export-labels", "--ids", "input.json"], "--ids", id="ids-space"),
    pytest.param(["--export-labels", "--ids=input.json"], "--ids=input.json", id="ids-equals"),
    pytest.param(["--live", "--manif", "input.json"], "--manif", id="manifest-space"),
    pytest.param(["--live", "--manif=input.json"], "--manif=input.json", id="manifest-equals"),
    pytest.param(["--dry-run", "--manifest-o", "plan.json"], "--manifest-o", id="manifest-out-space"),
    pytest.param(["--dry-run", "--manifest-o=plan.json"], "--manifest-o=plan.json", id="manifest-out-equals"),
    pytest.param(["--live", "--manifest", "input.json", "--stat", "st.json"], "--stat", id="state-space"),
    pytest.param(["--live", "--manifest", "input.json", "--stat=st.json"], "--stat=st.json", id="state-equals"),
]


@pytest.mark.parametrize("args,forwarded", ABBREV_SHELL_FORMS)
def test_abbreviated_flags_skip_the_injection_branch_and_fail_in_argparse(real_parser_sandbox, args, forwarded):
    result = real_parser_sandbox.run(*args)
    # 약어 토큰은 경로를 포함해 그대로 컨테이너로 간다. 전체 이름 플래그(있다면)만 주입 대상이다.
    main_args = main_call(result)["args"]
    assert forwarded in main_args
    # 컨테이너 쪽 argparse 가 거부한다(exit 2, 컨테이너가 파일을 열기 전)
    assert result.proc.returncode == 2
    assert "unrecognized arguments" in result.proc.stderr
    # 거부된 것이 약어 토큰 자체임을 확인(다른 토큰 때문에 우연히 통과하지 않게)
    rejected = result.proc.stderr.split("unrecognized arguments:", 1)[1]
    assert rejected.split()[0] == forwarded
    # 약어 토큰은 컨테이너 임시 경로로 바뀌지 않았다(주입 분기가 약어를 플래그로 인식하지 않았다). 임시 디렉터리는 정리된다.
    assert not any(a.startswith(str(real_parser_sandbox.ctmp)) for a in main_args if a == forwarded)
    assert staging_dir_leftovers(real_parser_sandbox) == []
