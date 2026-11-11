"""갱신 replica 모드 - provider 하나로 멘탈 모델 full rebuild (issue #229, 상위 #200).

recall 벤치 복제본(bench/replica.py)의 동결 계약(HS_OVERRIDES·hs_env)은 건드리지 않는다. 여기서는 같은 백업에서
**갱신 전용** 복제본을 따로 띄운다(이름 `hc-bench-refresh-<arm>`, state `refresh-<arm>.json`). recall 세트의
reconf·set-down·verify 는 이름이 달라 이 복제본을 보지 못하고, 기존 `down` 도 지우지 않는다(`refresh-down` 을 쓴다).

갱신 env = HS_OVERRIDES 그대로 + REFRESH_RELEASED 만 교체. 운영에서 상속된 LLM 키(`^HINDSIGHT_API_([A-Z0-9]+_)*LLM_`)와
provider 인증 env 는 모두 지우고, 남는 LLM 키는 provider·model·trace(+ claude-code 는 API_KEY) 뿐이다 - 폴백 멤버 없음.

고정 이미지 정적 확인 (2026-10-13, 운영 컨테이너 /app/api/hindsight_api 읽기 전용):
  - 워커 claim 은 `async_operations` 하나만 본다(worker/poller.py _claim_batch_for_schema_inner ->
    engine/db/ops_postgresql.py claim_tasks: `WHERE o.status = 'pending' ... FOR UPDATE SKIP LOCKED`).
    graph_maintenance_queue·entity_maintenance_queue 는 별도 실행 큐가 아니라 `graph_maintenance` 작업(이것도
    async_operations 행)이 안에서 비우는 표다 - pending 행을 취소하면 함께 멈춘다. 그래서 정리 대상은
    async_operations 의 pending·processing 뿐이다.
  - `GET /v1/default/banks/{bank}/llm-requests` (api/http.py api_list_llm_requests, engine/llm_trace.py
    LLMRequestListResponse): 응답 {bank_id, total, limit, offset, items[]}, item 필드 provider·model·status·
    started_at·trace_id 등. limit 1..500(기본 50)·offset>=0. start_date 포함(`started_at >= `)·end_date 제외(`started_at < `).

토큰 값은 docker 자식 프로세스 env 의 HINDSIGHT_API_LLM_API_KEY 로만 간다(argv 에는 `-e` 이름만).
env 파일·state·meta·stdout 에 값이 닿지 않는다(#214 와 같은 규칙).
"""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from bench import replica as R

REFRESH_PREFIX = "hc-bench-refresh-"
PROVIDERS = ("claude-code", "xai-oauth")
API_KEY = "HINDSIGHT_API_LLM_API_KEY"
LLM_KEY_RE = re.compile(r"^HINDSIGHT_API_([A-Z0-9]+_)*LLM_")
AUTH_ENV = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")           # + XAI_* 접두사
# HS_OVERRIDES 중 갱신이 바꾸는 키. 나머지 동결 키는 그대로 둔다(test (b)).
REFRESH_RELEASED = ("HINDSIGHT_API_WORKER_ENABLED", "HINDSIGHT_API_LLM_PROVIDER", "HINDSIGHT_API_LLM_MODEL",
                    "HINDSIGHT_API_LLM_TRACE_ENABLED")
MIN_CLI = (2, 1, 280)
HSAUTH_DEST = "/home/hindsight/.hindsight"
CLI_DEST = "/app/api/.venv/lib/python3.11/site-packages/claude_agent_sdk/_bundled/claude"
POLL_SECONDS = 20
DEFAULT_TIMEOUT = 900
PAGE = 500                                                              # llm-requests limit 상한
COUNT_SQL = "SELECT COUNT(*) FROM async_operations WHERE status IN ('pending','processing')"
CANCEL_SQL = ("UPDATE async_operations SET status='cancelled', updated_at=now() "
              "WHERE status IN ('pending','processing')")
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class Poisoned(Exception):
    """refresh 제출 뒤 수락·terminal 을 확정하지 못했다 - 서버 쪽 operation 이 아직 돌 수 있다."""


# ---------------------------------------------------------------- 이름·경로

def refresh_names(arm: str) -> Dict[str, str]:
    if not arm or len(arm) > R.SET_NAME_MAX or not arm[0].isalnum() \
            or not all(c.islower() or c.isdigit() or c == "-" for c in arm):
        raise SystemExit(f"refresh arm name must be [a-z0-9-], <= {R.SET_NAME_MAX} chars: {arm!r}")
    return {"hs": REFRESH_PREFIX + arm, "pg": REFRESH_PREFIX + arm + "-pg0", "state": f"refresh-{arm}.json"}


def state_path(arm: str) -> str:
    return os.path.join(R.STATE_DIR, refresh_names(arm)["state"])


def main_checkout() -> str:
    """git common dir 의 상위 = 메인 체크아웃(워크트리에서 실행해도 운영 정본 경로를 찾는다)."""
    r = subprocess.run(["git", "-C", R.REPO, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                       capture_output=True)
    out = R._decode(r.stdout, True).strip()
    if r.returncode != 0 or not out:
        return R.REPO
    return os.path.dirname(os.path.realpath(out))


def check_out_dir(out_dir: str) -> str:
    """산출물은 리포 밖에만 쓴다 - 실행 리포(REPO)와 메인 체크아웃 둘 다."""
    real = os.path.realpath(out_dir)
    for root in {os.path.realpath(R.REPO), os.path.realpath(main_checkout())}:
        p = real                                    # 대소문자 무시 파일시스템(APFS)도 잡게 실존 조상은 inode 로 비교
        while not (os.path.exists(p) and os.path.exists(root) and os.path.samefile(p, root)):
            if os.path.dirname(p) == p:
                break
            p = os.path.dirname(p)
        else:
            raise SystemExit(f"output dir must be outside the repository ({root}): {out_dir}")
        if real == root or real.startswith(root + os.sep):
            raise SystemExit(f"output dir must be outside the repository ({root}): {out_dir}")
    os.makedirs(real, mode=0o700, exist_ok=True)
    return real


def check_arm_collision(arm: str) -> None:
    """갱신 arm 이름이 recall 세트(state·컨테이너)와 같으면 만들기 전에 거부한다."""
    recall_sets = set()
    if os.path.isdir(R.STATE_DIR):
        recall_sets = {f[len("replica-"):-len(".json")] for f in os.listdir(R.STATE_DIR)
                       if f.startswith("replica-") and f.endswith(".json")}
    if arm in recall_sets or os.path.exists(os.path.join(R.STATE_DIR, f"replica-{arm}.json")) \
            or R.exists(f"{R.HS}-{arm}"):
        raise SystemExit(f"refresh arm {arm!r} collides with a recall set of the same name")
    if os.path.exists(state_path(arm)):
        raise SystemExit(f"refresh arm {arm!r} already has state - `refresh-down {arm}` first")


# ---------------------------------------------------------------- 입력 검사 (docker 실행 전)

def check_provider(provider: str) -> None:
    if provider not in PROVIDERS:
        raise SystemExit(f"provider must be one of {PROVIDERS}: {provider!r}")


def _version_tuple(text: str) -> Optional[Tuple[int, ...]]:
    m = re.fullmatch(r"\s*(\d+)\.(\d+)\.(\d+)\s*", text or "")
    return tuple(int(x) for x in m.groups()) if m else None


def check_cli(path: str) -> Dict[str, str]:
    """CLI 는 linux-arm64 바이너리라 호스트에서 실행하지 않는다. 같은 디렉터리의 VERSION 스탬프로 판정한다."""
    if not os.path.isfile(path):
        raise SystemExit(f"claude CLI is not a regular file: {path} (run scripts/fetch-claude-cli.sh)")
    if not os.access(path, os.X_OK):
        raise SystemExit(f"claude CLI is not executable: {path}")
    stamp = os.path.join(os.path.dirname(path), "VERSION")
    try:
        with open(stamp) as fh:
            raw = fh.read()
    except OSError:
        raise SystemExit(f"claude CLI has no VERSION stamp next to it: {stamp}") from None
    ver = _version_tuple(raw)
    if ver is None or ver < MIN_CLI:
        raise SystemExit(f"claude CLI VERSION {raw.strip()!r} < {'.'.join(map(str, MIN_CLI))}: {stamp}")
    return {"path": os.path.realpath(path), "version": ".".join(map(str, ver))}


def read_token(env_file: str, key: str) -> str:
    """KEY=VALUE 줄 파일에서 key 의 값. 오류 문구에 값을 싣지 않는다."""
    if not key or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        raise SystemExit(f"--token-key must be an env name: {key!r}")
    try:
        with open(env_file) as fh:
            lines = fh.read().splitlines()
    except OSError:
        raise SystemExit(f"cannot read --token-env-file {env_file}") from None
    for ln in lines:
        s = ln.strip()
        if s.startswith("export "):
            s = s[len("export "):].lstrip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        if k.strip() != key:
            continue
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        if not v or any(c in v for c in "\r\n"):
            raise SystemExit(f"{key} in {env_file} is empty or malformed")
        return v
    raise SystemExit(f"{key} not found in {env_file}")


def prod_hsauth_sources(prod: Dict[str, Any]) -> List[str]:
    """운영 hsauth 의 모든 이름: 리터럴, compose 프로젝트 접두사 이름, 운영 컨테이너의 실제 마운트 소스."""
    project = ((prod.get("Config") or {}).get("Labels") or {}).get("com.docker.compose.project") or "hindsight-crew"
    out = ["hsauth", f"{project}_hsauth", "hindsight-crew_hsauth"]
    for m in prod.get("Mounts") or []:
        if m.get("Destination") == HSAUTH_DEST:
            out += [x for x in (m.get("Name"), m.get("Source")) if x]
    return out


def check_xai_auth(auth: str, prod: Dict[str, Any]) -> Dict[str, str]:
    """xai-oauth 인증 저장소 = 호출자가 로그인해 둔 별도 볼륨 또는 디렉터리. 운영 hsauth 는 거부한다
    (refresh token 이 갱신 때 회전하므로 두 소비자가 쓰면 운영 grok 인증이 끊긴다)."""
    if not auth:
        raise SystemExit("xai-oauth needs --xai-auth <volume or absolute dir> (never the production hsauth)")
    bad = prod_hsauth_sources(prod)
    real = os.path.realpath(auth) if auth.startswith("/") else auth
    if auth in bad or real in bad or auth.endswith("_hsauth") \
            or any(b.startswith("/") and real == os.path.realpath(b) for b in bad):
        raise SystemExit(f"--xai-auth must not be the production hsauth store: {auth}")
    if auth.startswith("/"):
        if not os.path.isdir(auth):
            raise SystemExit(f"--xai-auth directory does not exist: {auth}")
        return {"kind": "dir", "source": real}
    if subprocess.run(["docker", "volume", "inspect", auth], capture_output=True).returncode != 0:
        raise SystemExit(f"--xai-auth volume does not exist: {auth}")
    return {"kind": "volume", "source": auth}


# ---------------------------------------------------------------- env 조립

def strip_llm(env: Dict[str, str]) -> Tuple[Dict[str, str], List[str]]:
    """LLM 라우팅·튜닝 키와 provider 인증 env 를 지운다. 지운 키 이름(값 아님)도 돌려준다."""
    gone = sorted(k for k in env if LLM_KEY_RE.match(k) or k in AUTH_ENV or k.startswith("XAI_"))
    return {k: v for k, v in env.items() if k not in gone}, gone


def refresh_env(prod_env: Dict[str, str], provider: str, model: str, key: str, tei_url: str,
                worker: bool = True) -> Tuple[Dict[str, str], List[str]]:
    """갱신 복제본 env 파일 내용(토큰 없음)과 운영 env 에서 지운 LLM·인증 키 이름.
    claude-code 의 HINDSIGHT_API_LLM_API_KEY 는 env 파일이 아니라 docker 자식 프로세스 env 로만 간다(secret_names)."""
    check_provider(provider)
    if not model:
        raise SystemExit("--model is required")
    _, removed = strip_llm(prod_env)
    env, _ = strip_llm(R.apply_overrides(prod_env, R.HS_OVERRIDES, key))
    env.update({"HINDSIGHT_API_WORKER_ENABLED": "true" if worker else "false",
                "HINDSIGHT_API_LLM_PROVIDER": provider, "HINDSIGHT_API_LLM_MODEL": model,
                "HINDSIGHT_API_LLM_TRACE_ENABLED": "true", R.TEI_URL_KEY: tei_url})
    return env, removed


def secret_names(provider: str) -> List[str]:
    return [API_KEY] if provider == "claude-code" else []


# ---------------------------------------------------------------- docker·REST 헬퍼

def _child_env(secret: Optional[str]) -> Dict[str, str]:
    """docker CLI 자식 프로세스 env: 호스트 env 에서 provider 인증 env 를 빼고 토큰만 목적지 키로 얹는다."""
    env, _ = strip_llm(dict(os.environ))
    if secret is not None:
        env[API_KEY] = secret
    return env


def run_container(names: Dict[str, str], env: Dict[str, str], image: str, mounts: List[str],
                  secret: Optional[str]) -> None:
    """갱신 컨테이너를 (복원된) 볼륨 위에 띄우고 health 200 까지 기다린다."""
    secrets_ = tuple(R._secret_values(env)) + ((secret,) if secret else ())
    envfile = R.write_envfile(env)
    try:
        argv = ["docker", "run", "-d", "--name", names["hs"], "--network", R.NET,
                "--memory", R.HS_MEMORY, "--oom-score-adj", "1000", "--env-file", envfile]
        if secret is not None:
            argv += ["-e", API_KEY]                                          # 이름만 - 값은 자식 env 에서 상속
        argv += ["-v", f"{names['pg']}:/home/hindsight/.pg0",
                 "-v", f"{R.PROD_HFCACHE}:/home/hindsight/.cache/huggingface:ro"]
        for m in mounts:
            argv += ["-v", m]
        argv += ["--label", "hc-bench=1", "--label", "hc-bench-refresh=1", image]
        r = subprocess.run(argv, capture_output=True, env=_child_env(secret))
        if r.returncode != 0:
            raise RuntimeError(f"docker run {names['hs']} exit {r.returncode}: "
                               f"{R._mask_stderr(R._decode(r.stderr, False), secrets_)[-400:]}")
    finally:
        os.remove(envfile)
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        st = R.inspect(names["hs"])["State"]
        if not st["Running"]:
            logs = R.sh("docker", "logs", "--tail", "20", names["hs"], check=False, keep_cr=True)
            raise SystemExit(f"{names['hs']} exited: {R._mask_stderr(logs, secrets_)}")
        ok = R.sh("docker", "exec", names["hs"], "python", "-c",
                  "import urllib.request as u;print(u.urlopen('http://localhost:8888/health',timeout=5).status)",
                  check=False)
        if ok == "200":
            return
        time.sleep(3)
    raise SystemExit(f"{names['hs']} not healthy within 300s")


_HTTP_CODE = (
    "import json,sys,urllib.request as u,urllib.error as e\n"
    "a=json.loads(sys.stdin.read());d=None if a['body'] is None else json.dumps(a['body']).encode()\n"
    "r=u.Request('http://localhost:8888'+a['path'],data=d,method=a['method'],"
    "headers={'Authorization':'Bearer '+a['key'],'Content-Type':'application/json'})\n"
    "try:\n"
    " x=u.urlopen(r,timeout=60);b=x.read();print(json.dumps({'status':x.status,'body':json.loads(b) if b else None}))\n"
    "except e.HTTPError as h:\n"
    " print(json.dumps({'status':h.code,'body':None}))\n"
    "except Exception:\n"
    " print(json.dumps({'status':0,'body':None}))\n"
)


def hs_call(container: str, method: str, path: str, body: Optional[dict] = None) -> Tuple[int, Any]:
    """복제본 REST 호출(호스트 포트 없음). 키는 stdin 으로. status 0 = 통신 오류(응답 유실 포함)."""
    payload = json.dumps({"method": method, "path": path, "body": body, "key": R.bench_key()})
    r = subprocess.run(["docker", "exec", "-i", container, "python", "-c", _HTTP_CODE],
                       capture_output=True, input=payload.encode())
    if r.returncode != 0:
        return 0, None
    try:
        out = json.loads(R._decode(r.stdout, True).strip())
        return int(out["status"]), out.get("body")
    except (ValueError, KeyError, TypeError):
        return 0, None


_PSQL_CODE = (
    "import json,os,subprocess,sys\n"
    "d=json.load(open('/home/hindsight/.pg0/instances/hindsight/instance.json'))\n"
    "p=os.path.join(d['installation_dir'],d['version'],'bin','psql')\n"
    "env=dict(os.environ,PGPASSWORD=d['password'])\n"
    "r=subprocess.run([p,'-h','/tmp','-p',str(d['port']),'-U',d['username'],'-d',d['database'],"
    "'-At','-v','ON_ERROR_STOP=1','-c',sys.stdin.read()],env=env,capture_output=True,text=True)\n"
    "sys.stdout.write(r.stdout);sys.stderr.write(r.stderr[-400:]);sys.exit(r.returncode)\n"
)


def pg_sql(container: str, sql: str) -> str:
    """복제본 내장 PostgreSQL(pg0)에 SQL 하나. 비밀번호는 컨테이너 안에서 instance.json 으로 읽어
    psql env 로만 넘긴다 - 호스트 argv·출력에 없다."""
    r = subprocess.run(["docker", "exec", "-i", container, "python", "-c", _PSQL_CODE],
                       capture_output=True, input=sql.encode())
    if r.returncode != 0:
        raise RuntimeError(f"pg0 sql exit {r.returncode}: {R._mask_stderr(R._decode(r.stderr, False))[-300:]}")
    return R._decode(r.stdout, True).strip()


def live_ops(container: str) -> int:
    out = pg_sql(container, COUNT_SQL)
    if not out.isdigit():
        raise RuntimeError(f"unexpected COUNT output: {out[:80]!r}")
    return int(out)


def server_now(container: str) -> str:
    """창 경계는 호스트 시계가 아니라 복제본 컨테이너 시계(pg0 와 같은 커널 시계)로 잡는다."""
    out = R.sh("docker", "exec", container, "python", "-c",
               "import datetime as d;print(d.datetime.now(d.timezone.utc).isoformat())")
    dt.datetime.fromisoformat(out)                      # 형식이 틀리면 ValueError
    return out


def _iso(t: str) -> dt.datetime:
    return dt.datetime.fromisoformat(t.replace("Z", "+00:00"))


# ---------------------------------------------------------------- state (OS 파일 잠금)

class StateLock:
    """갱신 state 파일에 대한 fcntl.flock. 프로세스가 죽으면 풀린다. 쓰기는 같은 파일에 제자리로 해 잠금이 유지된다."""

    def __init__(self, arm: str):
        self.path = state_path(arm)
        try:
            self.fh = open(self.path, "r+")
        except FileNotFoundError:
            raise SystemExit(f"refresh arm {arm!r} has no state - `refresh-up {arm}` first") from None
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            raise SystemExit(f"refresh arm {arm!r} is busy (another refresh holds its state lock)") from None
        self.data = json.load(self.fh)
        self.current_meta: Dict[str, Any] = {}

    def save(self) -> None:
        self.fh.seek(0)
        self.fh.truncate()
        json.dump(self.data, self.fh, indent=2)
        self.fh.flush()
        os.fsync(self.fh.fileno())

    def close(self) -> None:
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


def _new_state(arm: str, data: Dict[str, Any]) -> None:
    os.makedirs(R.STATE_DIR, mode=0o700, exist_ok=True)
    fd = os.open(state_path(arm), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2)


# ---------------------------------------------------------------- 기동

def refresh_up(arm: str, backup: str, provider: str, model: str, sidecar_port: int,
               token_env_file: Optional[str] = None, token_key: Optional[str] = None,
               xai_auth: Optional[str] = None, claude_cli: Optional[str] = None) -> Dict[str, Any]:
    """같은 백업에서 갱신 전용 복제본을 띄운다: 워커 끈 채 복원 -> pending·processing 정리(0건 재확인) -> 워커 켜고 재기동."""
    check_provider(provider)
    names = refresh_names(arm)
    if not backup or not os.path.isfile(backup):
        raise SystemExit(f"--backup must be an existing pg0.tar.gz: {backup!r}")
    R.check_bench_port(sidecar_port, "--sidecar-port")
    mounts: List[str] = []
    secret: Optional[str] = None
    auth_meta: Dict[str, Any] = {}
    if provider == "claude-code":                   # 파일 검사는 docker 를 부르기 전에 끝낸다
        if xai_auth:
            raise SystemExit("--xai-auth is for xai-oauth only")
        cli = check_cli(claude_cli or os.path.join(main_checkout(), "vendor", "claude-cli", "claude"))
        if not token_env_file or not token_key:
            raise SystemExit("claude-code needs --token-env-file and --token-key")
        secret = read_token(token_env_file, token_key)
        mounts += [f"{cli['path']}:{CLI_DEST}:ro"]
        auth_meta = {"token_key": token_key, "claude_cli": cli}
    elif token_env_file or token_key:
        raise SystemExit("--token-env-file/--token-key are for claude-code only")
    check_arm_collision(arm)
    sidecar = R.sidecar_for_port(R.sidecar_states(), sidecar_port)
    prod = R.inspect(R.PROD_HS)
    if provider == "xai-oauth":
        xa = check_xai_auth(xai_auth or "", prod)
        mounts.append(f"{xa['source']}:{HSAUTH_DEST}")
        auth_meta = {"xai_auth": xa}
    if R.exists(names["hs"]):
        raise SystemExit(f"{names['hs']} already exists - `refresh-down {arm}` first")
    if subprocess.run(["docker", "volume", "inspect", names["pg"]], capture_output=True).returncode == 0:
        raise SystemExit(f"volume {names['pg']} already exists - `refresh-down {arm}` first")
    avail = R.mem_available_mb()
    if avail is None or avail < R.MIN_AVAIL_MB:
        raise SystemExit(f"VM MemAvailable {avail}MB < {R.MIN_AVAIL_MB}MB - refusing (production is never stopped)")
    image = prod["Image"]
    img = json.loads(R.sh("docker", "image", "inspect", image))[0]
    key = R.bench_key()
    tei_url = f"http://host.lima.internal:{sidecar_port}"
    prod_env = R.runtime_env(R.PROD_HS)
    frozen_env, removed = refresh_env(prod_env, provider, model, key, tei_url, worker=False)
    live_env, _ = refresh_env(prod_env, provider, model, key, tei_url, worker=True)
    backup = os.path.abspath(backup)
    state: Dict[str, Any] = {
        "arm": arm, "status": "starting", "container": names["hs"], "volume": names["pg"],
        "provider": provider, "model": model, "backup": backup, "backup_sha256": R.sha256_file(backup),
        "image": image, "image_repo_digests": img.get("RepoDigests") or [],
        "sidecar_port": sidecar_port, "sidecar_name": sidecar.get("name"),
        "removed_llm_keys": removed, **auth_meta}
    _new_state(arm, state)
    lock = StateLock(arm)
    try:
        R.sh("docker", "network", "create", R.NET, check=False)
        R.sh("docker", "volume", "create", "--label", "hc-bench=1", "--label", "hc-bench-refresh=1", names["pg"])
        bdir, bfile = os.path.split(backup)
        R.sh("docker", "run", "--rm", "-v", f"{names['pg']}:/data", "-v", f"{bdir}:/b:ro", "alpine",
             "sh", "-c", f"tar xzf /b/{bfile} -C /data")
        run_container(names, frozen_env, image, mounts, secret)          # 워커 off - 복원된 작업이 돌지 않는다
        before = live_ops(names["hs"])
        if before:
            pg_sql(names["hs"], CANCEL_SQL)
        after = live_ops(names["hs"])
        lock.data.update({"cancelled_pending": before, "pending_after_cancel": after})
        if after != 0:
            lock.data["status"] = "failed"
            lock.save()
            R.sh("docker", "rm", "-f", names["hs"], check=False)
            raise SystemExit(f"{after} pending/processing operations remain after cancel - not refreshing "
                             f"(arms would diverge from the same backup)")
        R.sh("docker", "rm", "-f", names["hs"], check=False)
        run_container(names, live_env, image, mounts, secret)            # 워커 on - refresh operation 처리
        lock.data.update({"status": "ready", "started_at": R.inspect(names["hs"])["State"]["StartedAt"]})
        lock.save()
        return dict(lock.data)
    except BaseException:
        if lock.data.get("status") == "starting":
            lock.data["status"] = "failed"
            lock.save()
            R.sh("docker", "rm", "-f", names["hs"], check=False)   # 토큰·인증 저장소를 실은 컨테이너를 남기지 않는다
        raise
    finally:
        lock.close()


# ---------------------------------------------------------------- provider 관측

def observe(container: str, bank: str, start: str, end: str, provider: str, model: str) -> Dict[str, Any]:
    """창 [start, end) 의 llm_requests 를 페이지 끝까지 모은다. 지정 provider·model success 1건 이상이고
    다른 provider·model 호출이 0건이어야 ok. 엔드포인트가 응답하지 않을 때만 status unknown."""
    rows: List[Dict[str, Any]] = []
    offset = 0
    for _ in range(1000):
        q = urllib.parse.urlencode({"start_date": start, "end_date": end, "limit": PAGE, "offset": offset})
        code, body = hs_call(container, "GET", f"/v1/default/banks/{bank}/llm-requests?{q}")
        if code != 200 or not isinstance(body, dict) or not isinstance(body.get("items"), list):
            return {"status": "unknown", "reason": f"llm-requests http {code}", "provider": "unknown",
                    "model": "unknown"}
        items = body["items"]
        rows += [{k: it.get(k) for k in ("provider", "model", "status", "started_at", "trace_id")}
                 for it in items if isinstance(it, dict)]
        offset += len(items)
        total = body.get("total")
        if not items or (isinstance(total, int) and offset >= total):
            break
    pairs = sorted({(r["provider"], r["model"]) for r in rows}, key=lambda p: (str(p[0]), str(p[1])))
    want = [r for r in rows if r["provider"] == provider and r["model"] == model]
    other = [r for r in rows if not (r["provider"] == provider and r["model"] == model)]
    success = [r for r in want if r["status"] == "success"]
    ok = len(success) >= 1 and not other
    return {"status": "ok" if ok else "mismatch" if other else "no_success",
            "provider": sorted({str(p) for p, _ in pairs}), "model": sorted({str(m) for _, m in pairs}),
            "pairs": [list(p) for p in pairs], "calls": len(rows), "success_calls": len(success),
            "failed_calls": sum(1 for r in rows if r["status"] != "success"),
            "other_provider_calls": len(other), "trace_ids": sorted({r["trace_id"] for r in rows if r["trace_id"]})}


# ---------------------------------------------------------------- 갱신

def _poison(lock: StateLock, reason: str) -> None:
    lock.data.update({"status": "poisoned", "poisoned_reason": reason})
    lock.save()
    R.sh("docker", "stop", lock.data["container"], check=False)


def _write(out_dir: str, name: str, data: Any) -> str:
    path = os.path.join(out_dir, name)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=2))
    return path


def refresh_one(lock: StateLock, bank: str, model_id: str, out_dir: str, timeout: float) -> Dict[str, Any]:
    st = lock.data
    c = st["container"]
    meta: Dict[str, Any] = {
        "arm": st["arm"], "mental_model_id": model_id, "bank": bank,
        "backup": st["backup"], "backup_sha256": st["backup_sha256"],
        "image": st["image"], "image_repo_digests": st.get("image_repo_digests"),
        "requested": {"provider": st["provider"], "model": st["model"]},
        "cancelled_pending": st.get("cancelled_pending"), "removed_llm_keys": st.get("removed_llm_keys"),
        "operation_id": None, "status": "failed"}
    lock.current_meta = meta                        # poison 경로가 operation id·출처를 실패 산출물에 싣는다

    def fail(reason: str) -> Dict[str, Any]:
        meta["reason"] = reason
        meta.setdefault("observed", {"status": "unknown", "provider": "unknown", "model": "unknown"})
        _write(out_dir, f"{model_id}.meta.json", meta)
        return meta

    code, _ = hs_call(c, "GET", f"/v1/default/banks/{bank}/mental-models/{model_id}?detail=metadata")
    if code != 200:
        return fail(f"model_missing_http_{code}")
    if live_ops(c) != 0:
        return fail("live_operations_present")
    start = server_now(c)                           # clear 전에 잡는다 - 시계 오류가 clear 된 모델을 ready 로 남기지 않게
    code, _ = hs_call(c, "POST", f"/v1/default/banks/{bank}/mental-models/{model_id}/clear")
    if code not in (200, 202, 204):
        return fail(f"clear_http_{code}")
    st.update({"status": "refreshing", "in_flight": model_id})
    lock.save()
    code, body = hs_call(c, "POST", f"/v1/default/banks/{bank}/mental-models/{model_id}/refresh")
    if code == 0:
        raise Poisoned("refresh_submit_no_response")
    if not 200 <= code < 300:                       # 서버가 거부했다 - operation 이 없으므로 poison 아님
        st.update({"status": "ready", "in_flight": None})
        lock.save()
        return fail(f"refresh_http_{code}")
    op_id = (body or {}).get("operation_id") if isinstance(body, dict) else None
    if not op_id:
        raise Poisoned("refresh_accepted_without_operation_id")
    meta["operation_id"] = op_id
    st["in_flight_operation"] = op_id
    lock.save()
    deadline = time.monotonic() + timeout
    while True:
        code, op = hs_call(c, "GET", f"/v1/default/banks/{bank}/operations/{op_id}")
        if code != 200 or not isinstance(op, dict):
            raise Poisoned(f"operation_poll_http_{code}")
        status = op.get("status")
        if status in ("completed", "failed", "cancelled", "not_found"):
            break
        if time.monotonic() >= deadline:
            raise Poisoned("operation_timeout")
        time.sleep(POLL_SECONDS)
    end = server_now(c)
    st.update({"status": "ready", "in_flight": None, "in_flight_operation": None})
    lock.save()
    meta.update({"started_at": start, "ended_at": end,
                 "duration_seconds": round((_iso(end) - _iso(start)).total_seconds(), 3),
                 "operation": {k: op.get(k) for k in ("status", "created_at", "updated_at", "completed_at",
                                                     "error_message")}})
    meta["observed"] = observe(c, bank, start, end, st["provider"], st["model"])
    if status != "completed":
        return fail(f"operation_{status}")
    code, mm = hs_call(c, "GET", f"/v1/default/banks/{bank}/mental-models/{model_id}?detail=content")
    content = mm.get("content") if code == 200 and isinstance(mm, dict) else None
    if not isinstance(content, str) or not content.strip():
        return fail("empty_content")
    meta.update({"content_sha256": hashlib.sha256(content.encode()).hexdigest(), "content_chars": len(content),
                 "content_file": f"{model_id}.md"})
    if meta["observed"]["status"] != "ok":
        _write(out_dir, f"{model_id}.md", content)
        return fail(f"provider_{meta['observed']['status']}")
    meta["status"] = "ok"
    _write(out_dir, f"{model_id}.md", content)
    _write(out_dir, f"{model_id}.meta.json", meta)
    return meta


def refresh_run(arm: str, model_ids: List[str], out_dir: str, bank: str = R.BANK,
                timeout: float = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    """지정 모델들을 하나씩 clear -> refresh -> terminal 대기 -> 본문·meta 저장. 첫 실패에서 멈춘다."""
    refresh_names(arm)
    if not model_ids:
        raise SystemExit("name at least one --model-id")
    bad = [m for m in model_ids if not MODEL_ID_RE.match(m)]
    if bad:
        raise SystemExit(f"mental model ids must be [A-Za-z0-9._-]: {bad}")
    out = check_out_dir(out_dir)
    lock = StateLock(arm)
    try:
        if lock.data.get("status") == "poisoned":
            raise SystemExit(f"refresh arm {arm!r} is poisoned ({lock.data.get('poisoned_reason')}) - "
                             f"`refresh-down {arm}` and restore a fresh backup")
        if lock.data.get("status") != "ready":
            raise SystemExit(f"refresh arm {arm!r} is not ready (status {lock.data.get('status')!r})")
        results = []
        for m in model_ids:
            try:
                res = refresh_one(lock, bank, m, out, timeout)
            except BaseException as e:  # 제출 뒤(refreshing) 확정 못 한 오류는 모두 poison(Ctrl-C 포함) - 서버 operation 이 덮어쓸 수 있다
                if not isinstance(e, Poisoned) and lock.data.get("status") != "refreshing":
                    raise
                reason = str(e) if isinstance(e, Poisoned) else f"after_submit_{type(e).__name__}"
                _poison(lock, reason)
                res = {**getattr(lock, "current_meta", {}), "mental_model_id": m, "status": "failed",
                       "reason": reason, "poisoned": True,
                       "observed": {"status": "unknown", "provider": "unknown", "model": "unknown"}}
                _write(out, f"{m}.meta.json", res)
                if not isinstance(e, Exception):    # KeyboardInterrupt 등은 poison·실패 산출물을 남긴 뒤 그대로 올린다
                    raise
                results.append(res)
                break
            results.append(res)
            if res["status"] != "ok":
                break
        ok = len(results) == len(model_ids) and all(r["status"] == "ok" for r in results)
        return {"pass": ok, "arm": arm, "out_dir": out,
                "results": [{k: r.get(k) for k in ("mental_model_id", "status", "reason", "operation_id",
                                                     "content_chars")} for r in results]}
    finally:
        lock.close()


# ---------------------------------------------------------------- 내림

def refresh_down(arm: str) -> Dict[str, Any]:
    """갱신이 만든 컨테이너·볼륨·state 만 지운다. 호출자의 xai 인증 저장소(볼륨·디렉터리)는 지우지 않는다."""
    names = refresh_names(arm)
    path = state_path(arm)
    fh = None
    if os.path.exists(path):
        fh = open(path, "r+")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise SystemExit(f"refresh arm {arm!r} is busy - wait for the running refresh") from None
    try:
        R.sh("docker", "rm", "-f", names["hs"], check=False)
        R.sh("docker", "volume", "rm", names["pg"], check=False)
        if fh is not None:
            os.remove(path)
    finally:
        if fh is not None:
            fh.close()
    return {"down": arm, "removed": [names["hs"], names["pg"]]}
