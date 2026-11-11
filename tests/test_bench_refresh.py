"""issue #229 갱신 replica 모드 - provider 하나로 멘탈 모델 full rebuild. 실제 docker·LLM 없이 가짜 docker·가짜 API 로만 검증한다."""
import fcntl
import json
import os
import types
import urllib.parse

import pytest

from bench import refresh as F
from bench import replica as R

TOKEN = "fake-demoproj02-token-VALUE-1234567890"          # 가짜 토큰 - 어디에도 남으면 안 된다
PROD_DEMOPROJ02 = "prod-inherited-demoproj02-SECRET-0987"         # 가짜 운영 env 의 CLAUDE_CODE_OAUTH_TOKEN
HOST_DEMOPROJ02 = "host-shell-demoproj02-SECRET-5555"             # 호스트 셸 env 의 CLAUDE_CODE_OAUTH_TOKEN
PG_PASSWORD = "fake-pg0-password-xyz"               # 컨테이너 안 instance.json 에만 있다
PROD_IMAGE = "sha256:prodimage"

PROD_ENV = {
    "HINDSIGHT_API_LLM_STRATEGY": '{"mode":"failover"}',
    "HINDSIGHT_API_LLM_PROVIDER": "xai-oauth", "HINDSIGHT_API_LLM_MODEL": "grok-4.6",
    "HINDSIGHT_API_LLM_BASE_URL": "http://x", "HINDSIGHT_API_LLM_TIMEOUT": "120",
    "HINDSIGHT_API_LLM_1_PROVIDER": "claude-code", "HINDSIGHT_API_LLM_1_MODEL": "claude-opus-5-5",
    "HINDSIGHT_API_LLM_1_API_KEY": "member1-secret-aaaa",
    "HINDSIGHT_API_LLM_2_PROVIDER": "claude-code", "HINDSIGHT_API_LLM_2_MODEL": "claude-opus-5-5",
    "HINDSIGHT_API_LLM_2_API_KEY": "member2-secret-bbbb",
    "HINDSIGHT_API_REFLECT_LLM_PROVIDER": "xai-oauth", "HINDSIGHT_API_REFLECT_LLM_1_MODEL": "m",
    "HINDSIGHT_API_CONSOLIDATION_LLM_MODEL": "m", "HINDSIGHT_API_RETAIN_EXTRACTION_LLM_MODEL": "m",
    "HINDSIGHT_API_LLM_REASONING_EFFORT": "high",
    "CLAUDE_CODE_OAUTH_TOKEN": PROD_DEMOPROJ02, "ANTHROPIC_API_KEY": "anthropic-secret-cccc", "XAI_CLIENT_ID": "xai-id",
    "HINDSIGHT_API_WORKER_ENABLED": "true", "HINDSIGHT_API_RERANKER_MAX_CANDIDATES": "300",
    "HINDSIGHT_API_RERANKER_TEI_URL": "http://host.lima.internal:8787",
    "HC_HINDSIGHT_API_KEY": "hc-secret-dddd", "HC_PG_PASSWORD": "pg-secret-eeee",
}
LLM_EXPECTED = {
    "claude-code": {"HINDSIGHT_API_LLM_PROVIDER", "HINDSIGHT_API_LLM_MODEL", "HINDSIGHT_API_LLM_TRACE_ENABLED",
                    "HINDSIGHT_API_LLM_API_KEY"},
    "xai-oauth": {"HINDSIGHT_API_LLM_PROVIDER", "HINDSIGHT_API_LLM_MODEL", "HINDSIGHT_API_LLM_TRACE_ENABLED"},
}
PROD_HSAUTH_SRC = "/var/lib/docker/volumes/hindsight-crew_hsauth/_data"
PROD_CONTAINER = {
    "Image": PROD_IMAGE, "State": {"Running": True, "StartedAt": "prod-t"},
    "Config": {"Env": [f"{k}={v}" for k, v in PROD_ENV.items()],
               "Labels": {"com.docker.compose.project": "hindsight-crew"}},
    "Mounts": [{"Type": "volume", "Name": "hindsight-crew_hsauth", "Source": PROD_HSAUTH_SRC,
                "Destination": "/home/hindsight/.hindsight"}],
}


def llm_keys(env):
    return {k for k in env if F.LLM_KEY_RE.match(k)}


# ---------------------------------------------------------------- (a) (b) env 조립

@pytest.mark.parametrize("provider", ["claude-code", "xai-oauth"])
def test_a_refresh_llm_keys_are_exactly_the_provider_set(provider):
    env, removed = F.refresh_env(PROD_ENV, provider, "claude-opus-5-5", "k", "http://host.lima.internal:18781")
    assert llm_keys(env) | set(F.secret_names(provider)) == LLM_EXPECTED[provider]
    assert API_KEY_NOT_IN_FILE(env)
    for k in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "XAI_CLIENT_ID"):
        assert k not in env
    assert env["HINDSIGHT_API_LLM_PROVIDER"] == provider and env["HINDSIGHT_API_LLM_MODEL"] == "claude-opus-5-5"
    assert env["HINDSIGHT_API_LLM_TRACE_ENABLED"] == "true"
    # 지운 키 이름(값 아님)이 meta 로 간다 - 비라우팅 튜닝 키(TIMEOUT)도 포함
    assert {"HINDSIGHT_API_LLM_STRATEGY", "HINDSIGHT_API_LLM_1_API_KEY", "HINDSIGHT_API_REFLECT_LLM_1_MODEL",
            "HINDSIGHT_API_RETAIN_EXTRACTION_LLM_MODEL", "HINDSIGHT_API_LLM_TIMEOUT", "HINDSIGHT_API_LLM_BASE_URL",
            "CLAUDE_CODE_OAUTH_TOKEN", "XAI_CLIENT_ID"} <= set(removed)
    assert not any(v in removed for v in PROD_ENV.values() if "secret" in v.lower())


def API_KEY_NOT_IN_FILE(env):
    return F.API_KEY not in env


def test_b_released_and_frozen_keys_match_the_spec():
    assert set(F.REFRESH_RELEASED) == {"HINDSIGHT_API_WORKER_ENABLED", "HINDSIGHT_API_LLM_PROVIDER",
                                       "HINDSIGHT_API_LLM_MODEL", "HINDSIGHT_API_LLM_TRACE_ENABLED"}
    env, _ = F.refresh_env(PROD_ENV, "claude-code", "m", "benchkey", "http://host.lima.internal:18781")
    recall = R.hs_env(PROD_ENV, None, "benchkey", "http://host.lima.internal:18781")
    for k, v in R.HS_OVERRIDES.items():
        if k in F.REFRESH_RELEASED:
            continue
        if v is None:
            assert k not in env, k                      # LLM_REASONING_EFFORT·HC_* 는 제거 그대로
        else:
            assert env[k] == recall[k], k               # 동결 유지(WORKER_ID·consolidation·tick·MCP 키·시드·HF)
    assert env["HINDSIGHT_API_WORKER_ENABLED"] == "true"
    assert F.refresh_env(PROD_ENV, "claude-code", "m", "k", "u", worker=False)[0]["HINDSIGHT_API_WORKER_ENABLED"] == "false"
    # 리랭커는 벤치 사이드카로만
    assert env["HINDSIGHT_API_RERANKER_TEI_URL"] == "http://host.lima.internal:18781"
    # 비 LLM 운영 설정은 그대로 상속
    assert env["HINDSIGHT_API_RERANKER_MAX_CANDIDATES"] == "300"


def test_recall_freeze_contract_is_unchanged():
    assert R.HS_OVERRIDES["HINDSIGHT_API_WORKER_ENABLED"] == "false"
    assert R.HS_OVERRIDES["HINDSIGHT_API_LLM_PROVIDER"] == "none"
    with pytest.raises(SystemExit):
        R.hs_env(PROD_ENV, {"HINDSIGHT_API_WORKER_ENABLED": "true"}, "k")


@pytest.mark.parametrize("bad", ["openai", "none", "", "claude_code"])
def test_unknown_provider_is_rejected(bad):
    with pytest.raises(SystemExit):
        F.refresh_env(PROD_ENV, bad, "m", "k", "u")


# ---------------------------------------------------------------- 가짜 docker·가짜 hindsight

class FakeHS:
    """복제본 hindsight 의 REST 와 pg0 를 흉내 낸다."""

    def __init__(self):
        self.models = {"mm1": "old body", "mm2": "old body 2"}
        self.live = {"personal-demo": 2, "team-x": 1}          # 뱅크별 pending·processing
        self.cancel_works = True
        self.clear_code = 200
        self.refresh_mode = "ok"
        self.op_statuses = ["processing", "completed"]
        self.new_content = "# rebuilt\nbody"
        self.llm_rows = [{"provider": "claude-code", "model": "claude-opus-5-5", "status": "success",
                          "started_at": "t", "trace_id": "tr1"}]
        self.llm_mode = "ok"
        self.page_size = None
        self.sqls = []
        self.llm_queries = []
        self.http_log = []
        self.poll_mode = "ok"

    def sql(self, sql):
        self.sqls.append(sql)
        if sql == F.COUNT_SQL:
            return str(sum(self.live.values()))
        if sql == F.CANCEL_SQL:
            if self.cancel_works:
                self.live = {b: 0 for b in self.live}
            return "UPDATE 3"
        raise AssertionError(sql)

    def http(self, method, path, body):
        self.http_log.append((method, path))
        u = urllib.parse.urlsplit(path)
        parts = u.path.strip("/").split("/")
        q = dict(urllib.parse.parse_qsl(u.query))
        if parts[-1] == "llm-requests":
            self.llm_queries.append(q)
            if self.llm_mode == "down":
                return 0, None
            off, lim = int(q["offset"]), int(q["limit"])
            lim = min(lim, self.page_size or lim)
            return 200, {"bank_id": parts[3], "total": len(self.llm_rows), "limit": lim, "offset": off,
                         "items": self.llm_rows[off:off + lim]}
        if "operations" in parts:
            if self.poll_mode == "lost":
                return 0, None
            st = self.op_statuses.pop(0) if len(self.op_statuses) > 1 else self.op_statuses[0]
            if st == "completed":
                self.models["mm1"] = self.models["mm2"] = self.new_content
            return 200, {"operation_id": parts[-1], "status": st, "created_at": "c", "updated_at": "u",
                         "completed_at": "d" if st == "completed" else None, "error_message": None}
        mid = parts[5]
        if parts[-1] == "clear":
            if self.clear_code == 200:
                self.models[mid] = ""
            return self.clear_code, {"id": mid}
        if parts[-1] == "refresh":
            if self.refresh_mode == "lost":
                return 0, None
            if self.refresh_mode == "reject":
                return 400, None
            return 200, {"operation_id": "op-1", "status": "queued"}
        if mid not in self.models:
            return 404, None
        if q.get("detail") == "content":
            return 200, {"id": mid, "content": self.models[mid]}
        return 200, {"id": mid}


class FakeDocker:
    def __init__(self, hs):
        self.hs = hs
        self.containers = {}
        self.volumes = {"my-xai-auth"}
        self.argvs = []
        self.runs = []                 # (argv, child_env, envfile_text)
        self.clock = 0
        self.run_fail_stderr = None

    @staticmethod
    def _ok(out="", rc=0, err=""):
        return types.SimpleNamespace(returncode=rc, stdout=out.encode(), stderr=err.encode())

    def __call__(self, argv, capture_output=True, input=None, env=None, **kw):
        argv = list(argv)
        self.argvs.append(argv)
        if argv[0] == "git":
            return self._ok(self.git_common + "\n")
        assert argv[0] == "docker", argv
        cmd = argv[1]
        if cmd == "inspect":
            name = argv[2]
            if name == R.PROD_HS:
                return self._ok(json.dumps([PROD_CONTAINER]))
            if name in self.containers:
                return self._ok(json.dumps([{"Image": PROD_IMAGE, "Mounts": [],
                                             "State": {"Running": self.containers[name], "StartedAt": "t0"}}]))
            return self._ok(rc=1, err="no such object")
        if cmd == "image":
            return self._ok(json.dumps([{"Id": PROD_IMAGE, "RepoDigests": ["hindsight@sha256:digest1"],
                                         "Config": {"Env": []}}]))
        if cmd == "volume":
            sub, name = argv[2], argv[-1]
            if sub == "inspect":
                return self._ok(rc=0 if name in self.volumes else 1)
            if sub == "create":
                self.volumes.add(name)
            if sub == "rm":
                self.volumes.discard(name)
            return self._ok()
        if cmd == "network":
            return self._ok()
        if cmd == "run":
            if "--rm" in argv:
                return self._ok("8000" if "awk" in " ".join(argv) else "")
            envfile = argv[argv.index("--env-file") + 1]
            with open(envfile) as fh:
                text = fh.read()
            self.runs.append((argv, dict(env) if env is not None else None, text))
            if self.run_fail_stderr is not None:
                return self._ok(rc=125, err=self.run_fail_stderr)
            self.containers[argv[argv.index("--name") + 1]] = True
            return self._ok("cid")
        if cmd == "rm":
            self.containers.pop(argv[-1], None)
            return self._ok()
        if cmd == "stop":
            self.containers[argv[-1]] = False
            return self._ok()
        if cmd in ("logs", "ps"):
            return self._ok("")
        if cmd == "exec":
            code = argv[argv.index("-c") + 1]
            if code == F._PSQL_CODE:
                return self._ok(self.hs.sql(input.decode()) + "\n")
            if code == F._HTTP_CODE:
                a = json.loads(input.decode())
                status, body = self.hs.http(a["method"], a["path"], a["body"])
                return self._ok(json.dumps({"status": status, "body": body}))
            if "health" in code:
                return self._ok("200")
            if "datetime" in code:
                self.clock += 1
                return self._ok(f"2026-10-13T00:00:{self.clock:02d}.000000+00:00")
        raise AssertionError(argv)


@pytest.fixture
def env(tmp_path, monkeypatch):
    hs = FakeHS()
    dk = FakeDocker(hs)
    repo, main = tmp_path / "repo", tmp_path / "main"
    cli_dir = main / "vendor" / "claude-cli"
    cli_dir.mkdir(parents=True)
    (cli_dir / "claude").write_text("bin")
    os.chmod(cli_dir / "claude", 0o755)
    (cli_dir / "VERSION").write_text("2.1.282\n")
    dk.git_common = str(main / ".git")
    backup = tmp_path / "backups" / "20261013-000000" / "pg0.tar.gz"
    backup.parent.mkdir(parents=True)
    backup.write_bytes(b"backup-bytes")
    tokens = tmp_path / "tokens.env"
    tokens.write_text(f"# tokens\nOTHER=zzz\nexport DEMOPROJ02_TOKEN_ACCOUNT1={TOKEN}\n")
    monkeypatch.setattr(R, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(R, "REPO", str(repo))
    monkeypatch.setattr(R, "WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setattr(R.subprocess, "run", dk)
    monkeypatch.setattr(R.time, "sleep", lambda s: None)
    monkeypatch.setattr(R, "sidecar_states", lambda: [{"name": "sc1", "port": 18781, "overrides": {}}])
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", HOST_DEMOPROJ02)
    ns = types.SimpleNamespace(hs=hs, dk=dk, tmp=tmp_path, repo=repo, main=main, backup=str(backup),
                               tokens=str(tokens), out=str(tmp_path / "out"))
    return ns


def up_args(e, arm="cc", provider="claude-code", **extra):
    args = ["refresh-up", arm, "--backup", e.backup, "--provider", provider, "--model", "claude-opus-5-5",
            "--sidecar-port", "18781"]
    if provider == "claude-code":
        args += ["--token-env-file", e.tokens, "--token-key", "DEMOPROJ02_TOKEN_ACCOUNT1"]
    for k, v in extra.items():
        args += [f"--{k.replace('_', '-')}", v]
    return args


def docker_runs(e):
    return [a for a in e.dk.argvs if a[:2] == ["docker", "run"] and "--rm" not in a]


# ---------------------------------------------------------------- (d) 토큰 격리 + (f) 정상 흐름 + (l) pending 정리

def test_d_f_l_token_only_in_child_env_and_full_flow(env, capsys):
    assert R.main(up_args(env)) == 0
    assert R.main(["refresh-run", "cc", "--model-id", "mm1", "--out", env.out]) == 0
    out, err = capsys.readouterr()
    # (d) 토큰은 docker 자식 env 의 HINDSIGHT_API_LLM_API_KEY 에만 있다
    assert len(env.dk.runs) == 2                                   # 워커 off 복원 기동 + 워커 on 재기동
    for argv, child, text in env.dk.runs:
        i = argv.index("-e")
        assert argv[i + 1] == F.API_KEY                            # 이름만
        assert child[F.API_KEY] == TOKEN
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in child              # 호스트 셸 env 의 인증 env 도 넘기지 않는다
        assert TOKEN not in text and PROD_DEMOPROJ02 not in text and HOST_DEMOPROJ02 not in text
        assert F.API_KEY not in text
    assert "HINDSIGHT_API_WORKER_ENABLED=false" in env.dk.runs[0][2]
    assert "HINDSIGHT_API_WORKER_ENABLED=true" in env.dk.runs[1][2]
    blob = json.dumps(env.dk.argvs) + out + err
    for root, _, files in os.walk(env.tmp):
        for f in files:
            if f == "tokens.env":
                continue
            with open(os.path.join(root, f), "rb") as fh:
                blob += fh.read().decode("utf-8", "replace")
    assert TOKEN not in blob and PROD_DEMOPROJ02 not in blob
    # 마운트: CLI 는 ro, hsauth 없음
    run_argv = env.dk.runs[1][0]
    vols = [run_argv[i + 1] for i, a in enumerate(run_argv) if a == "-v"]
    assert any(v.endswith(F.CLI_DEST + ":ro") for v in vols)
    assert not any("hsauth" in v or F.HSAUTH_DEST in v for v in vols)
    # (l) 모든 뱅크를 같은 SQL 로 세고, 취소하고, 0건 재확인 뒤 워커를 켰다
    assert env.hs.sqls[:3] == [F.COUNT_SQL, F.CANCEL_SQL, F.COUNT_SQL]
    assert "bank" not in F.COUNT_SQL.lower() and "bank" not in F.CANCEL_SQL.lower()
    assert "status='cancelled'" in F.CANCEL_SQL and "'pending','processing'" in F.CANCEL_SQL
    sql_argvs = [a for a in env.dk.argvs if F._PSQL_CODE in a]
    assert sql_argvs and all(a == ["docker", "exec", "-i", "hc-bench-refresh-cc", "python", "-c", F._PSQL_CODE]
                             for a in sql_argvs)                  # SQL 은 stdin, 비밀번호는 컨테이너 안 instance.json 에서
    assert not any(PG_PASSWORD in " ".join(a) for a in env.dk.argvs)
    state = json.load(open(F.state_path("cc")))
    assert state["status"] == "ready" and state["cancelled_pending"] == 3 and state["pending_after_cancel"] == 0
    # (f) 존재 확인 -> clear -> refresh -> operation 대기 -> 본문
    seq = [(m, p.split("?")[0].rsplit("/", 1)[-1]) for m, p in env.hs.http_log]
    assert seq[:4] == [("GET", "mm1"), ("POST", "clear"), ("POST", "refresh"), ("GET", "op-1")]
    body = open(os.path.join(env.out, "mm1.md")).read()
    assert body == env.hs.new_content
    meta = json.load(open(os.path.join(env.out, "mm1.meta.json")))
    assert meta["status"] == "ok" and meta["operation_id"] == "op-1"
    assert meta["backup"] == env.backup and len(meta["backup_sha256"]) == 64
    assert meta["image"] == PROD_IMAGE and meta["image_repo_digests"] == ["hindsight@sha256:digest1"]
    assert meta["requested"] == {"provider": "claude-code", "model": "claude-opus-5-5"}
    assert meta["content_chars"] == len(body) and len(meta["content_sha256"]) == 64
    assert meta["cancelled_pending"] == 3 and meta["duration_seconds"] > 0
    assert meta["observed"]["status"] == "ok" and meta["observed"]["provider"] == ["claude-code"]
    assert "CLAUDE_CODE_OAUTH_TOKEN" in meta["removed_llm_keys"]
    # 창 경계는 서버(컨테이너) 시계
    q = env.hs.llm_queries[0]
    assert q["start_date"] == meta["started_at"] and q["end_date"] == meta["ended_at"]
    assert q["start_date"].startswith("2026-10-13T00:00:")


def test_d_token_is_masked_on_the_docker_failure_path(env, capsys):
    env.dk.run_fail_stderr = f"error: bad env HINDSIGHT_API_LLM_API_KEY={TOKEN} raw {TOKEN}"
    with pytest.raises(RuntimeError) as ei:
        R.main(up_args(env))
    out, err = capsys.readouterr()
    assert TOKEN not in str(ei.value) and TOKEN not in out + err
    assert env.dk.runs and TOKEN not in env.dk.runs[0][2] and PROD_DEMOPROJ02 not in env.dk.runs[0][2]
    assert json.load(open(F.state_path("cc")))["status"] == "failed"
    assert TOKEN not in open(F.state_path("cc")).read()


def test_xai_oauth_mounts_only_the_callers_store(env):
    assert R.main(up_args(env, arm="gx", provider="xai-oauth", xai_auth="my-xai-auth")) == 0
    argv, child, text = env.dk.runs[1]
    assert "-e" not in argv and F.API_KEY not in child
    vols = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    assert f"my-xai-auth:{F.HSAUTH_DEST}" in vols
    assert not any("_hsauth" in v for v in vols)
    assert llm_keys(dict(ln.split("=", 1) for ln in text.splitlines())) == LLM_EXPECTED["xai-oauth"]


# ---------------------------------------------------------------- (c) 운영 hsauth 거부

@pytest.mark.parametrize("bad", ["hsauth", "hindsight-crew_hsauth", PROD_HSAUTH_SRC, "otherproj_hsauth"])
def test_c_production_hsauth_is_rejected(env, bad):
    with pytest.raises(SystemExit):
        F.check_xai_auth(bad, PROD_CONTAINER)


def test_c_inspect_mount_source_is_rejected_even_under_another_name(env):
    prod = {**PROD_CONTAINER, "Mounts": [{"Name": "renamed-auth", "Source": "/srv/auth",
                                          "Destination": "/home/hindsight/.hindsight"}]}
    for bad in ("renamed-auth", "/srv/auth"):
        with pytest.raises(SystemExit):
            F.check_xai_auth(bad, prod)
    with pytest.raises(SystemExit):
        R.main(up_args(env, arm="gx", provider="xai-oauth", xai_auth="hsauth"))
    assert not docker_runs(env)


# ---------------------------------------------------------------- (e) (j) CLI 검사 - docker 실행 전, 호스트에서 실행하지 않는다

@pytest.mark.parametrize("case", ["missing", "dir", "old", "nostamp", "noexec", "garbage"])
def test_e_j_cli_is_rejected_before_docker(env, case):
    cli = env.tmp / "cli" / "claude"
    cli.parent.mkdir()
    if case == "dir":
        cli.mkdir()
    elif case != "missing":
        cli.write_text("bin")
        os.chmod(cli, 0o644 if case == "noexec" else 0o755)
    if case not in ("nostamp", "missing", "dir"):
        (cli.parent / "VERSION").write_text({"old": "2.1.150", "garbage": "latest"}.get(case, "2.1.282"))
    with pytest.raises(SystemExit):
        R.main(up_args(env, claude_cli=str(cli)))
    assert not any(a[0] == "docker" for a in env.dk.argvs)          # docker 를 아예 부르지 않았다
    assert not any(str(cli) == a[0] for a in env.dk.argvs)           # CLI 를 실행하지 않았다


def test_j_cli_version_boundary(env):
    d = env.tmp / "c2"
    d.mkdir()
    (d / "claude").write_text("bin")
    os.chmod(d / "claude", 0o755)
    (d / "VERSION").write_text("2.1.280")
    assert F.check_cli(str(d / "claude"))["version"] == "2.1.280"
    (d / "VERSION").write_text("2.1.279")
    with pytest.raises(SystemExit):
        F.check_cli(str(d / "claude"))


def test_default_cli_is_the_main_checkout(env):
    assert R.main(up_args(env)) == 0
    vols = [a for a in env.dk.runs[1][0] if a.endswith(F.CLI_DEST + ":ro")]
    assert vols == [f"{os.path.realpath(env.main / 'vendor' / 'claude-cli' / 'claude')}:{F.CLI_DEST}:ro"]


# ---------------------------------------------------------------- (g) 산출물 경로

@pytest.mark.parametrize("where", ["repo", "main"])
def test_g_output_inside_a_repository_is_rejected(env, where):
    assert R.main(up_args(env)) == 0
    root = env.repo if where == "repo" else env.main
    with pytest.raises(SystemExit):
        R.main(["refresh-run", "cc", "--model-id", "mm1", "--out", str(root / "bench-out")])
    assert ("POST", "/v1/default/banks/personal-demo/mental-models/mm1/clear") not in env.hs.http_log


# ---------------------------------------------------------------- (h) 이름 충돌

def test_h_arm_colliding_with_a_recall_set_is_rejected(env):
    os.makedirs(R.STATE_DIR, exist_ok=True)
    with open(os.path.join(R.STATE_DIR, "replica-cand.json"), "w") as fh:
        json.dump({"set": "cand"}, fh)
    with pytest.raises(SystemExit):
        R.main(up_args(env, arm="cand"))
    env.dk.containers[f"{R.HS}-live"] = True                         # state 없이 컨테이너만 있어도
    with pytest.raises(SystemExit):
        R.main(up_args(env, arm="live"))
    assert not docker_runs(env)


def test_recall_commands_do_not_touch_refresh_replicas(env):
    assert R.main(up_args(env)) == 0
    with pytest.raises(SystemExit):                                   # reconf 는 갱신 state 를 보지 못한다
        R.reconf("cc", None)
    before = list(env.dk.argvs)
    R.set_down("cc")
    touched = [a for a in env.dk.argvs[len(before):] if a[:2] in (["docker", "rm"], ["docker", "volume"])]
    assert all(F.REFRESH_PREFIX not in " ".join(a) for a in touched)
    assert os.path.exists(F.state_path("cc"))
    assert env.dk.containers[F.refresh_names("cc")["hs"]] is True


# ---------------------------------------------------------------- (f) 실패 경로

def run_cc(env, *models):
    args = ["refresh-run", "cc", "--out", env.out]
    for m in models or ("mm1",):
        args += ["--model-id", m]
    return R.main(args)


def meta_of(env, m="mm1"):
    return json.load(open(os.path.join(env.out, f"{m}.meta.json")))


def test_f_missing_model_fails(env):
    assert R.main(up_args(env)) == 0
    assert run_cc(env, "nope") == 1
    assert meta_of(env, "nope")["reason"].startswith("model_missing")


def test_f_clear_failure_is_a_failed_artifact(env):
    assert R.main(up_args(env)) == 0
    env.hs.clear_code = 500
    assert run_cc(env) == 1
    m = meta_of(env)
    assert m["status"] == "failed" and m["reason"] == "clear_http_500"
    assert not any(p.endswith("/refresh") for _, p in env.hs.http_log)
    assert json.load(open(F.state_path("cc")))["status"] == "ready"


def test_f_operation_failure_is_a_failed_artifact(env):
    assert R.main(up_args(env)) == 0
    env.hs.op_statuses = ["failed"]
    assert run_cc(env) == 1
    m = meta_of(env)
    assert m["reason"] == "operation_failed" and m["operation_id"] == "op-1"
    assert not os.path.exists(os.path.join(env.out, "mm1.md"))


def test_f_empty_body_fails(env):
    assert R.main(up_args(env)) == 0
    env.hs.new_content = "  \n"
    assert run_cc(env) == 1
    assert meta_of(env)["reason"] == "empty_content"


# ---------------------------------------------------------------- (m) 직렬 잠금·poisoned

def test_m_timeout_poisons_and_stops_the_container(env):
    assert R.main(up_args(env)) == 0
    env.hs.op_statuses = ["processing"]
    assert R.main(["refresh-run", "cc", "--model-id", "mm1", "--out", env.out, "--timeout", "0"]) == 1
    st = json.load(open(F.state_path("cc")))
    assert st["status"] == "poisoned" and st["poisoned_reason"] == "operation_timeout"
    assert ["docker", "stop", F.refresh_names("cc")["hs"]] in env.dk.argvs
    assert meta_of(env)["poisoned"] is True and meta_of(env)["operation_id"] == "op-1"
    assert meta_of(env)["backup_sha256"] and meta_of(env)["observed"]["provider"] == "unknown"
    with pytest.raises(SystemExit) as ei:                              # poisoned 복제본 갱신 거부
        run_cc(env)
    assert "poisoned" in str(ei.value)


@pytest.mark.parametrize("where", ["submit", "poll"])
def test_m_lost_response_after_submit_poisons(env, where):
    assert R.main(up_args(env)) == 0
    if where == "submit":
        env.hs.refresh_mode = "lost"
    else:
        env.hs.poll_mode = "lost"
    assert run_cc(env) == 1
    st = json.load(open(F.state_path("cc")))
    assert st["status"] == "poisoned"
    assert st["poisoned_reason"] == ("refresh_submit_no_response" if where == "submit" else "operation_poll_http_0")
    assert env.dk.containers[F.refresh_names("cc")["hs"]] is False


def test_m_server_rejecting_refresh_is_not_poison(env):
    assert R.main(up_args(env)) == 0
    env.hs.refresh_mode = "reject"
    assert run_cc(env) == 1
    assert json.load(open(F.state_path("cc")))["status"] == "ready"
    assert meta_of(env)["reason"] == "refresh_http_400"


def test_m_concurrent_refresh_on_the_same_replica_is_rejected(env):
    assert R.main(up_args(env)) == 0
    with open(F.state_path("cc")) as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)            # 다른 갱신이 잠금을 쥐고 있다
        with pytest.raises(SystemExit) as ei:
            run_cc(env)
        assert "busy" in str(ei.value)
        with pytest.raises(SystemExit):
            R.main(["refresh-down", "cc"])
    assert not any(p.endswith("/clear") for _, p in env.hs.http_log)
    assert run_cc(env) == 0                                           # 잠금이 풀리면 진행


# ---------------------------------------------------------------- (l) pending 정리 실패

def test_l_cleanup_that_leaves_live_operations_refuses_refresh(env):
    env.hs.cancel_works = False
    with pytest.raises(SystemExit):
        R.main(up_args(env))
    assert len(env.dk.runs) == 1                                       # 워커 on 재기동을 하지 않았다
    st = json.load(open(F.state_path("cc")))
    assert st["status"] == "failed" and st["pending_after_cancel"] == 3
    with pytest.raises(SystemExit):
        run_cc(env)


def test_l_live_operations_at_run_time_block_clear(env):
    assert R.main(up_args(env)) == 0
    env.hs.live = {"team-x": 1}
    assert run_cc(env) == 1
    assert meta_of(env)["reason"] == "live_operations_present"
    assert not any(p.endswith("/clear") for _, p in env.hs.http_log)


# ---------------------------------------------------------------- (i) provider 관측

ROW = {"provider": "claude-code", "model": "claude-opus-5-5", "status": "success", "started_at": "t", "trace_id": "tr"}


def test_i_observation_pages_through_all_rows(env):
    assert R.main(up_args(env)) == 0
    env.hs.llm_rows = [dict(ROW, trace_id=f"tr{i}") for i in range(5)] + [dict(ROW, status="error")]
    env.hs.page_size = 2
    assert run_cc(env) == 0
    obs = meta_of(env)["observed"]
    assert obs["calls"] == 6 and obs["success_calls"] == 5 and obs["failed_calls"] == 1
    assert [q["offset"] for q in env.hs.llm_queries] == ["0", "2", "4"]
    assert all(q["limit"] == str(F.PAGE) for q in env.hs.llm_queries)


def test_i_a_row_from_another_provider_on_a_later_page_fails(env):
    assert R.main(up_args(env)) == 0
    env.hs.llm_rows = [ROW, ROW, dict(ROW, provider="xai-oauth", model="grok-4.6")]
    env.hs.page_size = 2
    assert run_cc(env) == 1
    m = meta_of(env)
    assert m["reason"] == "provider_mismatch" and m["observed"]["other_provider_calls"] == 1
    assert m["observed"]["provider"] == ["claude-code", "xai-oauth"]


def test_i_other_model_same_provider_fails(env):
    assert R.main(up_args(env)) == 0
    env.hs.llm_rows = [ROW, dict(ROW, model="claude-sonnet-5-5")]
    assert run_cc(env) == 1
    assert meta_of(env)["reason"] == "provider_mismatch"


def test_i_empty_page_fails(env):
    assert R.main(up_args(env)) == 0
    env.hs.llm_rows = []
    assert run_cc(env) == 1
    m = meta_of(env)
    assert m["reason"] == "provider_no_success" and m["observed"]["calls"] == 0
    assert m["observed"]["provider"] == []                             # 요청값으로 채우지 않는다


def test_i_only_failed_calls_fail(env):
    assert R.main(up_args(env)) == 0
    env.hs.llm_rows = [dict(ROW, status="error")]
    assert run_cc(env) == 1
    assert meta_of(env)["reason"] == "provider_no_success"


def test_i_unresponsive_endpoint_is_unknown(env):
    assert R.main(up_args(env)) == 0
    env.hs.llm_mode = "down"
    assert run_cc(env) == 1
    m = meta_of(env)
    assert m["observed"]["status"] == "unknown"
    assert m["observed"]["provider"] == "unknown" and m["observed"]["model"] == "unknown"
    assert m["reason"] == "provider_unknown"


# ---------------------------------------------------------------- (k) (n) 내림

def test_k_n_down_removes_refresh_container_and_volume_but_not_caller_auth(env):
    xdir = env.tmp / "xai-login"
    xdir.mkdir()
    (xdir / "auth.json").write_text("{}")
    assert R.main(up_args(env, arm="gx", provider="xai-oauth", xai_auth="my-xai-auth")) == 0
    assert R.main(up_args(env, arm="gd", provider="xai-oauth", xai_auth=str(xdir))) == 0
    names = F.refresh_names("gx")
    assert names["hs"].startswith("hc-bench-refresh-") and names["pg"].startswith("hc-bench-refresh-")
    assert names["pg"] in env.dk.volumes
    for arm in ("gx", "gd"):
        assert R.main(["refresh-down", arm]) == 0
    assert names["hs"] not in env.dk.containers and names["pg"] not in env.dk.volumes
    assert not os.path.exists(F.state_path("gx"))
    assert "my-xai-auth" in env.dk.volumes                             # 호출자 볼륨 보존
    assert (xdir / "auth.json").exists()                               # 호출자 디렉터리 보존
    removed = [a[-1] for a in env.dk.argvs if a[:3] in (["docker", "volume", "rm"], ["docker", "rm", "-f"])]
    assert "my-xai-auth" not in removed and str(xdir) not in removed


def test_recall_down_leaves_refresh_replica(env):
    assert R.main(up_args(env)) == 0
    R.hs_down()
    assert env.dk.containers.get(F.refresh_names("cc")["hs"]) is True
    assert os.path.exists(F.state_path("cc"))
