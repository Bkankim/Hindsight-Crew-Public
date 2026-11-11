"""recall 재생 하네스 - 시험장(격리 복제본) 관리 (issue #13).

운영 hindsight 를 **백업에서 복원한 동결 복제본**으로 띄우고, 그 앞에 비교할 게이트웨이(팔, arm)를
git ref 단위로 세운다. 운영 컨테이너·볼륨·자격증명은 읽기만 한다.

동일성 원칙: 복제본의 설정은 .env.local 을 다시 해석하지 않고 **실행 중인 운영 컨테이너의 실제 env**
(이미지 기본 env 를 뺀 런타임 주입분)를 복제한다. 그 위에 아래 OVERRIDES 만 얹는다. 각 override 는
"결과에 영향이 없거나(동결·격리), 결과를 결정적으로 만드는(시드)" 것이어야 하며 이유를 적는다.

동결 원칙 (2026-09-23 upstream v0.8.3 코드 조사 근거):
  - worker poller 가 복원된 pending 작업을 집어 실행한다       -> WORKER_ENABLED=false
  - MaintenanceLoop 가 consolidation 재조정·멘탈모델 갱신·llm_requests 삭제를 한다 -> 각 주기 0 / trace off
  - LLM 자격증명(hsauth, xai-oauth)은 refresh 시 회전한다 - 공유하면 운영 자격증명을 깬다
                                                             -> hsauth 미마운트, LLM_PROVIDER=none
    recall 경로에는 LLM 호출이 없으므로(memory_engine 5290-7000) 결과에 영향 없다.
  - graph 동점 순서가 문자열 해시 시드에 의존한다             -> PYTHONHASHSEED=0
동결은 주장이 아니라 측정한다: freeze_fingerprint() 를 verify() 와 runner 의 run 시작·끝에서 뜨고, judge 가
run 전후 지문이 다르면 판정을 거부한다.

운영 안전 (2026-09-23 재리뷰 반영):
  - 복제본은 메모리 상한(HS_MEMORY)과 oom_score_adj=1000 으로 띄운다 - OOM 이 나면 운영이 아니라 복제본이 죽는다.
  - 팔은 운영 secrets/ 를 마운트하지 않는다. 운영 acl.json 은 모든 identity 의 평문 운영 토큰을 담고 있어서,
    임의 ref·patch 로 빌드한 팔 코드가 읽으면 운영(8888) 토큰이 샌다. 대신 벤치 전용 ACL(벤치 클라이언트 토큰 1개 ->
    운영 demo 와 같은 identity·뱅크)을 state 에 만들어 마운트하고, runner 는 이 벤치 토큰으로 호출한다(final-review 2026-09-23).
  - 운영 비밀(HC_HINDSIGHT_API_KEY·HC_PG_PASSWORD)은 복제본 env 에 복제하지 않는다. 팔 env 는 TOKEN·SECRET·
    PASSWORD·DISCORD 이름을 일괄 제거하고, 보호 키(GW_OVERRIDES)는 --env 로 바꿀 수 없다.
  - 팔 감사 로그(recall-queries.log 는 쿼리 원문을 담는다)는 컨테이너 tmpfs 에만 쓴다 - holdout 쿼리가
    디스크에 남지 않는다. WORK_DIR 은 0700.
  - 볼륨이 이미 있으면(다른 컨테이너가 잡고 있어 지우지 못했으면) 복원을 거부한다 - 옛 클러스터 위에 덮어쓰지 않는다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import plistlib
import re
import secrets
import signal
import socket
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.expanduser("~/.local/share/hindsight-bench/state")   # 벤치 전용 키 등 (0700)
WORK_DIR = os.path.expanduser("~/.local/share/hindsight-bench/work")     # 팔 빌드 소스(git archive 추출)·감사 로그

PROD_HS = "hindsight-crew-hindsight-1"
PROD_GW = "hindsight-crew-gateway-1"
PROD_HFCACHE = "hindsight-crew_hfcache"

NET = "hc-bench-net"
HS = "hc-bench-hindsight"
PG = "hc-bench-pg0"
GW_PREFIX = "hc-bench-gw-"
BANK = "personal-demo"
HS_MEMORY = "3g"             # 복제본 상한. 운영 RSS 실측 1.9GB + 모델 로드 피크 여유
MIN_AVAIL_MB = 3500          # 복제본 hindsight 실측 RSS ~1.9GB + 모델 로드 피크 여유. 모자라면 중단(운영은 절대 안 멈춘다)

SET_NAME_MAX = 20


def instance_names(set_name: str) -> Dict[str, str]:
    """세트(복제본 인스턴스) 이름 -> 컨테이너·볼륨·네트워크 alias·state 파일. 빈 이름 = 기본 세트(기존 이름 그대로).
    upstream·사이드카 설정이 다른 팔은 각자 세트를 가진다(issue #13 B - 게이트웨이만 바꾸는 팔로는 판정 불가)."""
    if set_name == "":
        return {"hs": HS, "pg": PG, "alias": "hindsight", "state": "replica.json"}
    if len(set_name) > SET_NAME_MAX or not all(c.islower() or c.isdigit() or c == "-" for c in set_name) \
            or not set_name[0].isalnum():
        raise SystemExit(f"set name must be [a-z0-9-], <= {SET_NAME_MAX} chars: {set_name!r}")
    return {"hs": f"{HS}-{set_name}", "pg": f"{PG}-{set_name}", "alias": f"hindsight-{set_name}",
            "state": f"replica-{set_name}.json"}


# 복제본 hindsight 에 얹는 override. 값 None = 키 제거.
HS_OVERRIDES: Dict[str, Optional[str]] = {
    "HINDSIGHT_API_WORKER_ENABLED": "false",                          # 복원된 pending 작업 실행 금지
    "HINDSIGHT_API_WORKER_ID": "hc-bench",                            # 운영 워커 소유 작업 복구 금지
    "HINDSIGHT_API_ENABLE_AUTO_CONSOLIDATION": "false",
    # 두 주기 모두 0 = 끔 (upstream maintenance.py:121 의 `interval > 0` 검사)
    "HINDSIGHT_API_CONSOLIDATION_RECONCILE_INTERVAL_SECONDS": "0",
    "HINDSIGHT_API_MENTAL_MODEL_REFRESH_TICK_SECONDS": "0",
    "HINDSIGHT_API_LLM_TRACE_ENABLED": "false",                       # llm_requests 보존 삭제 루프
    "HINDSIGHT_API_LLM_PROVIDER": "none",                             # 자격증명 회전 차단 (recall 무관)
    "HINDSIGHT_API_LLM_MODEL": None,
    "HINDSIGHT_API_LLM_REASONING_EFFORT": None,
    "HINDSIGHT_API_MCP_AUTH_TOKEN": "<bench-key>",                    # 운영 키와 분리
    "PYTHONHASHSEED": "0",                                            # graph 동점 순서 고정
    "HF_HUB_OFFLINE": "1",                                            # 모델 캐시는 읽기 전용 공유
    "HC_HINDSIGHT_API_KEY": None,                                     # .env.local 이 함께 주입한 운영 비밀 -
    "HC_PG_PASSWORD": None,                                           #   upstream 은 HC_* 를 읽지 않는다
}
# 게이트웨이 팔: 운영 게이트웨이 env 에서 반드시 바꾸거나 지울 키
GW_OVERRIDES: Dict[str, Optional[str]] = {
    "HC_UPSTREAM_BASE": "http://hindsight:8888",
    "HC_HINDSIGHT_API_KEY": "<bench-key>",
    "HC_AUDIT_LOG": "/audit/gateway.log",
    "HC_RECALL_QUERY_LOG": "/audit/recall-queries.log",
    "HC_PENDING_TRIGGERS": "/audit/pending-triggers.jsonl",
    "HC_NOTIFY_DISCORD_BOT_TOKEN": None,                              # 벤치가 Discord 로 새지 않게
    "HC_NOTIFY_DISCORD_CHANNEL_ID": None,
}
# 팔 env 에서 이름으로 일괄 제거하는 비밀 패턴(보호 키로 다시 넣는 것은 예외). 새 비밀 이름이 생겨도 새지 않게.
GW_SECRET_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "DISCORD")
# 게이트웨이 컨테이너 env 중 이미지(python 베이스)가 넣은 것 - 팔에 복제하지 않는다
GW_IMAGE_BASE_KEYS = ("PATH", "LANG", "GPG_KEY", "PYTHON_VERSION", "PYTHON_SHA256")


# ---------------------------------------------------------------- docker 헬퍼

# issue #129: compose-go dotenv 파서는 형식이 틀린 .env 줄 원문(`=값`·따옴표 값)을 오류에 싣는다. stderr 는 기록·전송하기 전에
# 이 함수를 거친다 - 줄 번호·오류 종류·일반 문구는 남기고 값만 가린다. import 경로를 묶지 않으려고 파일마다 같은 헬퍼를 둔다.
_DOTENV_LINE = re.compile(r"\bline \d+: ")
_UNTERMINATED = re.compile(r"(unterminated quoted value ).*")
_QUOTED = re.compile(r'"(?:[^"\\]|\\.)*"')                  # %q 가 만든 따옴표 문자열(이스케이프 포함)
_ASSIGN = re.compile(r"""(?P<k>[A-Za-z_][\w.\[\]-]*)=(?:"(?P<q>(?:[^"\\]|\\.)*)"?|'[^']*'?|[^ \t\n]*)""")   # 값은 공백·탭·개행에서만 끝난다 - \r·\v·\f·U+2028 등 단독 구분자는 값의 일부(issue #165)
# compose 경고 줄(time="..." level=warning msg="...")은 허용 목록에 맞는 값만 남긴다. 허용 밖은 전부 가린다(유출보다 진단 손실이 낫다).
_LOG_LEVELS = frozenset(("trace", "debug", "info", "warn", "warning", "error", "fatal", "panic"))
_TIMESTAMP = re.compile(r"[0-9T:.Z+-]+")
# msg 는 알려진 compose 경고 문구만 남긴다. 자유 문구는 값(시크릿)이 들어 있어도 모양으로 알 수 없다(issue #165). 변수 이름 자리는 식별자만 허용한다.
_SAFE_MSG = re.compile(r'The \\"[A-Za-z_]\w*\\" variable is not set\. Defaulting to a blank string\.'
                       r"|the attribute `version` is obsolete, it will be ignored, please remove it to avoid potential confusion")
# 값 기준 마스킹 대상 env 키 이름 조각과 최소 값 길이(true·1 같은 짧은 값을 가리면 로그가 망가진다)
_SECRET_ENV_MARKERS = GW_SECRET_MARKERS + ("KEY", "AUTH", "DSN", "CREDENTIAL")
_SECRET_MIN_LEN = 6
_URL_USERINFO = re.compile(r"^[A-Za-z][\w+.-]*://[^/\s@:]*:(?P<pw>[^/\s@]+)@")      # scheme://user:password@host


def _secret_values(env: Dict[str, str]) -> List[str]:
    """env 에서 값 기준으로 가릴 시크릿 값 - _mask_stderr 가 KEY= 없이 찍힌 값도 가리게 한다.
    키 이름에 TOKEN·SECRET·PASSWORD·KEY·DISCORD·AUTH·DSN·CREDENTIAL 이 있으면 대상. 단 TOKENS(복수, 예: MAX_TOKENS)는 개수 설정이라 뺀다.
    이름이 *_URL 인 키는 값에 userinfo 의 password(user:pass@)가 있을 때만 대상(URL 전체와 password 단독). 없으면 호스트 진단을 남긴다."""
    out = []
    for k, v in env.items():
        if not isinstance(v, str) or len(v) < _SECRET_MIN_LEN:
            continue
        ku = k.upper()
        if "TOKENS" not in ku and any(m in ku for m in _SECRET_ENV_MARKERS):
            out.append(v)
        elif ku.endswith("_URL") and (m := _URL_USERINFO.match(v)):
            out.append(v)
            if len(m["pw"]) >= _SECRET_MIN_LEN:
                out.append(m["pw"])
    return out


def _redact_assign(a, logfmt_ok):
    k, q = a["k"], a["q"]
    if logfmt_ok and (a.start() == 0 or a.string[a.start() - 1].isspace()):      # 키 앞이 줄 시작 또는 공백일 때만
        v = q if q is not None else a.group(0)[len(k) + 1:]
        if (k == "time" and _TIMESTAMP.fullmatch(v)) or (k == "level" and v in _LOG_LEVELS) \
                or (k == "msg" and q is not None and a.group(0) == f'{k}="{q}"' and _SAFE_MSG.fullmatch(q)):
            return a.group(0)
    return k + "=<redacted>"


def _mask_stderr(text: str, secrets: Tuple[str, ...] = ()) -> str:
    """secrets = 호출자가 아는 시크릿 값(예: 사이드카 env). 줄 모양과 무관하게 값 자체를 가린다(issue #165)."""
    text = text or ""
    for v in sorted(set(secrets), key=len, reverse=True):     # 긴 값 먼저 - 다른 값을 포함한 값이 조각으로 남지 않게
        text = text.replace(v, "<redacted>")
    out = []
    for ln in text.removesuffix("\n").split("\n"):    # Go 는 \n 에서만 줄을 자른다 - splitlines() 는 U+2028 등에서도 쪼갠다
        ln = ln.removesuffix("\r")
        m = _DOTENV_LINE.search(ln)
        if m:                                             # dotenv 오류 줄: `line N:` 뒤의 따옴표 값은 전부 가린다
            rest = _UNTERMINATED.sub(r"\1<redacted>", ln[m.end():])
            rest = re.sub(r'"[^"]*$', "<redacted>", _QUOTED.sub("<redacted>", rest))
            ln = ln[:m.end()] + rest
        out.append(_ASSIGN.sub(lambda a: _redact_assign(a, m is None), ln))
    return "\n".join(out)


def _decode(b, translate_newlines: bool) -> str:
    """바이트 -> 문자열. 비 UTF-8 은 대체 문자. text=True 와 같은 줄바꿈 정규화(\\r\\n·\\r -> \\n)는 translate_newlines 일 때만."""
    s = b.decode("utf-8", errors="replace") if isinstance(b, bytes) else (b or "")
    return s.replace("\r\n", "\n").replace("\r", "\n") if translate_newlines else s


def sh(*args: str, check: bool = True, capture: bool = True, input_: Optional[str] = None, keep_cr: bool = False) -> str:
    """keep_cr: stdout 의 단독 \\r 을 개행으로 바꾸지 않는다 - 마스커에 넣을 docker logs 용(issue #165). stderr 는 항상 원형으로 마스킹한다.
    text=True 가 아니라 바이트로 받는 이유: 텍스트 모드는 마스커가 보기 전에 \\r 을 \\n 으로 바꿔 값 뒤쪽 조각이 줄 규칙을 피한다."""
    r = subprocess.run(list(args), capture_output=capture, input=None if input_ is None else input_.encode("utf-8"))
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:4])}... exit {r.returncode}: {_mask_stderr(_decode(r.stderr, False))[-400:]}")
    return _decode(r.stdout, not keep_cr).strip()


def inspect(name: str) -> Dict[str, Any]:
    return json.loads(sh("docker", "inspect", name))[0]


def exists(name: str) -> bool:
    return subprocess.run(["docker", "inspect", name], capture_output=True).returncode == 0


def env_of(container: Dict[str, Any]) -> Dict[str, str]:
    return dict(e.split("=", 1) for e in container["Config"]["Env"])


def runtime_env(container_name: str) -> Dict[str, str]:
    """컨테이너 env 에서 이미지 기본 env 를 뺀 것 = compose/env_file 이 주입한 실제 설정."""
    c = inspect(container_name)
    image_env = dict(e.split("=", 1) for e in (json.loads(sh("docker", "image", "inspect", c["Image"]))[0]
                                                ["Config"].get("Env") or []))
    return {k: v for k, v in env_of(c).items() if image_env.get(k) != v}


def apply_overrides(env: Dict[str, str], overrides: Dict[str, Optional[str]], key: str) -> Dict[str, str]:
    out = dict(env)
    for k, v in overrides.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = key if v == "<bench-key>" else v
    return out


def vh(value: str) -> str:
    """값 지문(평문을 보고서에 남기지 않는다)."""
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def write_envfile(env: Dict[str, str]) -> str:
    bad = [k for k, v in env.items() if "\n" in v]
    if bad:                                   # 파일을 만들기 전에 거부 - 비밀이 일부 담긴 파일을 남기지 않는다
        raise ValueError(f"multi-line env values cannot go through --env-file: {bad}")
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="hc-bench-env-", dir=STATE_DIR)      # mkstemp = 0600
    with os.fdopen(fd, "w") as fh:
        for k, v in sorted(env.items()):
            fh.write(f"{k}={v}\n")
    return path


def check_user_env(user_env: Optional[Dict[str, str]]) -> None:
    clash = sorted(set(user_env or {}) & set(GW_OVERRIDES))
    if clash:
        raise SystemExit(f"--env cannot override protected keys: {clash}")


TEI_URL_KEY = "HINDSIGHT_API_RERANKER_TEI_URL"


def hs_env(prod_env: Dict[str, str], user_env: Optional[Dict[str, str]], key: str,
           tei_url: Optional[str] = None) -> Dict[str, str]:
    """복제본 hindsight env = 운영 env + 동결 override + 실험용 upstream 설정(--hs-env).
    동결 override 키는 실험 설정으로 덮을 수 없다(워커·consolidation 이 켜지면 복제본 데이터가 바뀐다).
    tei_url = 세트의 벤치 사이드카. 리랭커 주소는 이 경로로만 바꾼다(--hs-env 로 돌리면 출처 기록을 우회한다)."""
    clash = sorted(set(user_env or {}) & (set(HS_OVERRIDES) | {TEI_URL_KEY}))
    if clash:
        raise SystemExit(f"--hs-env cannot override freeze keys: {clash}")
    # 교차 --configs 는 JSON 이라 숫자가 올 수 있다 - reconf 가 컨테이너를 지우기 전에 거부한다(verify 도 문자열로 비교)
    non_str = sorted(k for k, v in (user_env or {}).items() if not isinstance(v, str))
    if non_str:
        raise SystemExit(f"--hs-env / --configs values must be strings: {non_str}")
    env = apply_overrides(apply_overrides(prod_env, HS_OVERRIDES, key), dict(user_env or {}), key)
    if tei_url is not None:
        env[TEI_URL_KEY] = tei_url
    return env


def compose_gateway_env(prod_gw_env: Dict[str, str], user_env: Optional[Dict[str, str]], key: str,
                        upstream_alias: str = "hindsight") -> Dict[str, str]:
    """팔 env = 운영 게이트웨이 env 전체(이미지 베이스 키 제외) - 비밀 패턴 + 사용자 env + 보호 키.
    이미지 기본값과 같은 키를 빼는 방식은 후보 ref 가 Dockerfile ENV 를 바꾸면 compose 가 명시한 값을 잃는다.
    upstream_alias = 팔이 붙는 세트 복제본의 네트워크 alias(보호 키라 --env 로는 못 바꾼다)."""
    check_user_env(user_env)
    env = {k: v for k, v in prod_gw_env.items()
           if k not in GW_IMAGE_BASE_KEYS and not any(m in k.upper() for m in GW_SECRET_MARKERS)}
    env.update(user_env or {})
    return apply_overrides(env, {**GW_OVERRIDES, "HC_UPSTREAM_BASE": f"http://{upstream_alias}:8888"}, key)


def gateway_env(user_env: Optional[Dict[str, str]], key: str, upstream_alias: str = "hindsight") -> Dict[str, str]:
    return compose_gateway_env(env_of(inspect(PROD_GW)), user_env, key, upstream_alias)


PROD_SIDECAR_PORT = 8787
SIDECAR_PORTS = range(18780, 18800)        # 벤치 사이드카 포트 대역(게이트웨이 팔 1889x 와 겹치지 않는다)
SIDECAR_LOCKED = ("RERANK_PORT", "RERANK_BIND")
AUX_PORT_KEY, AUX_BATCH_KEY = "RERANK_AUX_PORT", "RERANK_AUX_BATCH_SIZE"      # 사이드카 보조 포트(벤치 전용, issue #78)


def _int_env(value: Any) -> Optional[int]:
    """override 문자열 -> 정수. 정수가 아니면 None (bool 은 정수가 아니다)."""
    try:
        return int(value) if not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None


def check_bench_port(port: Any, what: str) -> int:
    """벤치 사이드카가 열 수 있는 포트인가. 운영 포트(8787)와 벤치 대역 밖은 거부한다."""
    if not isinstance(port, int) or isinstance(port, bool) or port == PROD_SIDECAR_PORT or port not in SIDECAR_PORTS:
        raise SystemExit(f"{what} must be a bench sidecar port in {SIDECAR_PORTS.start}-{SIDECAR_PORTS.stop - 1} "
                         f"(never the production sidecar {PROD_SIDECAR_PORT}): {port!r}")
    return port


def check_arm_sidecar(port: Any, batch_size: Any) -> None:
    """팔 설정 = 세트가 붙을 벤치 사이드카 포트(주 또는 보조)와 그 포트의 기대 배치 크기. 둘은 함께 준다(issue #83)."""
    check_bench_port(port, "arm sidecar port")
    if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size < 1:
        raise SystemExit(f"arm sidecar batch size must be an integer >= 1: {batch_size!r}")


def sidecar_ports(state: Dict[str, Any]) -> List[int]:
    """벤치 사이드카 state 가 여는 포트: 주 포트 + (설정했다면) 보조 포트."""
    ports = [state["port"]] if state.get("port") is not None else []
    aux = _int_env((state.get("overrides") or {}).get(AUX_PORT_KEY))
    return ports + ([aux] if aux is not None else [])


def sidecar_launch(plist_env: Dict[str, str], plist_argv: List[str], overrides: Optional[Dict[str, str]],
                   port: int) -> Tuple[Dict[str, str], List[str]]:
    """벤치 사이드카 = 운영 launchd plist 의 env·실행 인자 복제 + 실험 override, 포트만 벤치 대역으로 바꾼다.
    운영 포트(8787)·bind 는 override 로 바꿀 수 없다 - 벤치가 운영 사이드카 자리를 뺏거나 외부에 열리지 않게."""
    if port == PROD_SIDECAR_PORT or port not in SIDECAR_PORTS:
        raise SystemExit(f"bench sidecar port must be in {SIDECAR_PORTS.start}-{SIDECAR_PORTS.stop - 1}: {port}")
    clash = sorted(set(overrides or {}) & set(SIDECAR_LOCKED))
    if clash:
        raise SystemExit(f"sidecar overrides cannot touch {clash}")
    aux_set = {k for k in (AUX_PORT_KEY, AUX_BATCH_KEY) if k in (overrides or {})}
    if aux_set:       # 보조 포트도 운영 포트·대역 밖·주 포트와 같은 값을 거부한다 - 둘은 함께, 배치는 1 이상
        if aux_set != {AUX_PORT_KEY, AUX_BATCH_KEY}:
            raise SystemExit(f"{AUX_PORT_KEY} and {AUX_BATCH_KEY} must be set together")
        aux, aux_batch = _int_env(overrides[AUX_PORT_KEY]), _int_env(overrides[AUX_BATCH_KEY])
        check_bench_port(aux, AUX_PORT_KEY)
        if aux == port:
            raise SystemExit(f"{AUX_PORT_KEY}={aux} must differ from the sidecar port")
        if aux_batch is None or aux_batch < 1:
            raise SystemExit(f"{AUX_BATCH_KEY}={overrides[AUX_BATCH_KEY]!r} must be an integer >= 1")
    if "--port" not in plist_argv:
        raise SystemExit("production sidecar argv has no --port")
    env = {**plist_env, **(overrides or {}), "RERANK_PORT": str(port)}
    i = plist_argv.index("--port")
    argv = plist_argv[:i + 1] + [str(port)] + plist_argv[i + 2:]
    if len(argv) > 1 and os.path.basename(argv[0]) == "uv" and argv[1] == "run" and "--no-sync" not in argv:
        # 운영과 같은 .venv 를 그대로 쓴다 - uv 가 lock 에 맞춰 공유 환경을 다시 동기화하면 운영이 다음 재기동 때 그 환경을 쓴다
        argv = argv[:2] + ["--no-sync"] + argv[2:]
    return env, argv


BENCH_IDENTITY = "demo"              # 벤치가 재생하는 identity(운영 원장·코퍼스의 identity 와 같다)


def bench_client_token() -> str:
    """runner 가 팔에 보내는 벤치 전용 bearer 토큰. 운영 토큰과 무관하고 벤치 ACL 에만 있다."""
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    path = os.path.join(STATE_DIR, "bench-client.key")
    if not os.path.exists(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write("hcb_" + secrets.token_urlsafe(32))
    return open(path).read().strip()


def bench_acl_dir() -> str:
    """벤치 전용 ACL 디렉터리. 운영 acl.json 에서 BENCH_IDENTITY 항목의 권한(identity·personal·teams)만 복사하고
    키는 벤치 클라이언트 토큰으로 바꾼다 - 운영 토큰 값은 파일에 들어가지 않는다."""
    with open(os.path.join(REPO, "secrets", "acl.json")) as fh:
        prod = json.load(fh)
    grants = [v for v in (prod.get("tokens") or {}).values()
              if isinstance(v, dict) and v.get("identity") == BENCH_IDENTITY]
    if not grants:
        raise SystemExit(f"production ACL has no identity {BENCH_IDENTITY!r}")
    grant = {k: grants[0][k] for k in ("identity", "personal", "teams") if k in grants[0]}
    d = os.path.join(STATE_DIR, "arm-secrets")
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d)                                     # 0600
    with os.fdopen(fd, "w") as fh:
        json.dump({"tokens": {bench_client_token(): grant}}, fh)
    os.replace(tmp, os.path.join(d, "acl.json"))
    os.chmod(os.path.join(d, "acl.json"), 0o644)      # 컨테이너 안 게이트웨이(root 아님일 수도)가 읽도록. 벤치 토큰뿐
    return d


def bench_key() -> str:
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    path = os.path.join(STATE_DIR, "bench.key")
    if not os.path.exists(path):
        with open(path, "w") as fh:
            fh.write(secrets.token_urlsafe(32))
        os.chmod(path, 0o600)
    return open(path).read().strip()


def mem_available_mb() -> Optional[int]:
    out = sh("docker", "run", "--rm", "--network", "none", "alpine", "sh", "-c",
             "awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo", check=False)
    return int(out) if out.isdigit() else None


def http_json(url: str, key: str, method: str = "GET", body: Optional[dict] = None,
              timeout: float = 30) -> Tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, ConnectionError, OSError):
        return 0, None                    # 기동 중(연결 거부·끊김) - 호출측이 재시도한다


def upstream_get_via(container: str, path: str) -> Any:
    """운영/복제본 hindsight 는 호스트에 포트가 없다 - 같은 네트워크의 컨테이너 안에서 읽는다(GET 전용)."""
    code = ("import os,sys,json,urllib.request as u;"
            "r=u.Request(os.environ['HC_UPSTREAM_BASE']+sys.argv[1],"
            "headers={'Authorization':'Bearer '+os.environ['HC_HINDSIGHT_API_KEY']});"
            "print(u.urlopen(r,timeout=30).read().decode())")
    return json.loads(sh("docker", "exec", container, "python", "-c", code, path))


# ---------------------------------------------------------------- 복제본 hindsight

def latest_backup() -> str:
    root = os.path.join(REPO, "backups")
    # 디렉터리 이름(YYYYMMDD-HHMMSS)이 백업 시각이다 - mtime 은 복사·touch 로 바뀐다
    cands = sorted(os.path.join(root, d, "pg0.tar.gz") for d in os.listdir(root)
                   if os.path.exists(os.path.join(root, d, "pg0.tar.gz")))
    if not cands:
        raise SystemExit("no backups/*/pg0.tar.gz")
    return cands[-1]


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _start_replica(n: Dict[str, str], env: Dict[str, str], image: str) -> None:
    """세트 컨테이너를 (이미 복원된) 볼륨 위에 띄우고 health 200 까지 기다린다."""
    envfile = write_envfile(env)
    try:
        sh("docker", "run", "-d", "--name", n["hs"], "--network", NET, "--network-alias", n["alias"],
           "--memory", HS_MEMORY, "--oom-score-adj", "1000",
           "--env-file", envfile, "-v", f"{n['pg']}:/home/hindsight/.pg0",
           "-v", f"{PROD_HFCACHE}:/home/hindsight/.cache/huggingface:ro",
           "--label", "hc-bench=1", image)
    finally:
        os.remove(envfile)
    deadline = time.time() + 300
    while time.time() < deadline:
        st = inspect(n["hs"])["State"]
        if not st["Running"]:
            raise SystemExit(f"{n['hs']} exited: {_mask_stderr(sh('docker', 'logs', '--tail', '20', n['hs'], check=False, keep_cr=True), tuple(_secret_values(env)))}")
        ok = sh("docker", "exec", n["hs"], "python", "-c",
                "import urllib.request as u;print(u.urlopen('http://localhost:8888/health',timeout=5).status)",
                check=False)
        if ok == "200":
            return
        time.sleep(3)
    raise SystemExit(f"{n['hs']} not healthy within 300s")


def reconf(set_name: str, user_env: Optional[Dict[str, str]], sidecar_port: Optional[int] = None,
           sidecar_batch_size: Optional[int] = None) -> Dict[str, Any]:
    """이름 있는 세트의 upstream 설정만 바꿔 컨테이너를 같은 볼륨 위에 다시 띄운다(순환 교차 단계 전환, issue #13 B).
    백업을 다시 풀지 않는다 - 동결 복제본이라 데이터는 그대로이고, verify·runner 의 동결 지문이 이를 확인한다.
    sidecar_port·sidecar_batch_size = 이 팔이 쓰는 벤치 사이드카 포트(주 또는 보조)와 그 포트의 기대 배치(issue #83).
    둘은 함께 주고, 컨테이너를 지우기 전에 검사한다(운영 포트 8787·떠 있지 않은 포트 거부). 주지 않으면 세트 state 에 마지막으로 기록된 포트·배치를 그대로 쓴다
    (up 때 값, 또는 그 뒤 팔 지정 reconf 가 남긴 값 - up 시점으로 되돌리지 않는다: 교차 단계 사이의 일반 reconf 가 팔 설정을 잃지 않게)."""
    if not set_name:
        raise SystemExit("reconf is for named sets")
    if (sidecar_port is None) != (sidecar_batch_size is None):
        raise SystemExit("an arm sidecar needs both a port and its expected batch size")
    n = instance_names(set_name)
    path = os.path.join(STATE_DIR, n["state"])
    try:
        meta = json.load(open(path))
    except FileNotFoundError:
        raise SystemExit(f"set {set_name} has no replica state - `up --set {set_name}` first")
    arm_sidecar = None
    if sidecar_port is not None:
        check_arm_sidecar(sidecar_port, sidecar_batch_size)
        arm_sidecar = sidecar_for_port(sidecar_states(), sidecar_port)     # `sidecar-up` 으로 띄운 사이드카의 주·보조 포트만
    if subprocess.run(["docker", "volume", "inspect", n["pg"]], capture_output=True).returncode != 0:
        raise SystemExit(f"volume {n['pg']} is gone - `up --set {set_name}` again")
    prod = inspect(PROD_HS)
    if prod["Image"] != meta.get("image"):      # 캠페인 도중 운영 재배포 - 단계마다 다른 upstream 을 재게 된다
        raise SystemExit(f"production image changed since `up --set {set_name}` "
                         f"({meta.get('image')} -> {prod['Image']}) - bring the sets up again")
    port = sidecar_port if sidecar_port is not None else meta["sidecar_port"]
    tei_url = f"http://host.lima.internal:{port}"
    env = hs_env(runtime_env(PROD_HS), user_env, bench_key(), tei_url)      # 동결 키 보호는 그대로
    sh("docker", "rm", "-f", n["hs"], check=False)
    _start_replica(n, env, meta["image"])
    meta.update({"hs_env": dict(user_env or {}), "started_at": inspect(n["hs"])["State"]["StartedAt"],
                 "prod_hs_started_at": prod["State"]["StartedAt"]})
    if arm_sidecar is not None:
        meta.update({"sidecar_port": sidecar_port, "sidecar_batch_size": sidecar_batch_size,
                     "sidecar_name": arm_sidecar["name"], "sidecar": dict(arm_sidecar["overrides"])})
    with open(path, "w") as fh:
        json.dump(meta, fh, indent=2)
    if arm_sidecar is not None:       # gw-up 이 남긴 이 세트 게이트웨이의 출처(sidecar_port 스냅숏)도 지금 포트로 맞춘다 - run meta 의 팔·세트 출처가 어긋나지 않게
        arms_dir = os.path.join(STATE_DIR, "arms")
        for f in sorted(os.listdir(arms_dir)) if os.path.isdir(arms_dir) else []:
            ap = os.path.join(arms_dir, f)
            try:
                with open(ap) as fh:
                    prov = json.load(fh)
            except (OSError, ValueError):
                continue
            if prov.get("set") == set_name and "sidecar_port" in prov:
                prov["sidecar_port"] = sidecar_port
                with open(ap, "w") as fh:
                    json.dump(prov, fh, indent=2)
    return meta


def hs_up(backup: Optional[str] = None, force: bool = False, user_env: Optional[Dict[str, str]] = None,
          set_name: str = "", sidecar_port: Optional[int] = None,
          sidecar_batch_size: Optional[int] = None, expected_overrides: Optional[str] = None) -> Dict[str, Any]:
    """세트 복제본을 띄운다. 기본 세트("")는 운영 사이드카를 공유하는 기존 동작 그대로다.
    이름 있는 세트는 이미 떠 있는 벤치 사이드카(sidecar_port)로 리랭크한다 - 다른 세트를 내리지 않는다.
    expected_overrides = arm 백업의 LINEAGE 경로. 그 기대 뱅크 override 를 state 에 남겨 verify 가 운영 대신 그것과 대조한다(issue #297).
    벤치 사이드카는 세트끼리 공유한다: 세트마다 모델을 올리면 통합 메모리를 넘겨 가중치가 스왑되고, 사이드카를
    오가는 호출이 수 초씩 느려져 속도 판정이 무너진다(selftest3-sets 2026-09-26: A/A CI [0.83, 1.26])."""
    n = instance_names(set_name)
    if not set_name:
        # 기본 세트 teardown(hs_down)은 이름 있는 세트·벤치 사이드카까지 치운다 - 교차 캠페인을 조용히 지우지 않게 거부
        others = sorted(f for f in (os.listdir(STATE_DIR) if os.path.isdir(STATE_DIR) else [])
                        if f.startswith(("replica-", "sidecar-")) and f.endswith(".json"))
        if others:
            raise SystemExit(f"named sets / bench sidecars are up ({', '.join(others)}) - "
                             f"run `down` (or set-down / sidecar-down) first")
    if set_name and sidecar_port is None:
        raise SystemExit("a named set needs a bench sidecar (--sidecar-port of a `sidecar-up` sidecar)")
    if not set_name and sidecar_port is not None:
        raise SystemExit("the default set shares the production sidecar - name a set to use a bench sidecar")
    if sidecar_batch_size is not None:      # 기대 배치(verify 가 사이드카 info 와 대조, issue #83)는 벤치 사이드카에 붙은 세트에만 뜻이 있다
        if sidecar_port is None:
            raise SystemExit("an expected sidecar batch size needs the bench sidecar port it applies to")
        check_arm_sidecar(sidecar_port, sidecar_batch_size)
    sidecar = sidecar_for_port(sidecar_states(), sidecar_port) if set_name else None   # type: ignore[arg-type]
    expected = load_expected_overrides(expected_overrides) if expected_overrides else None   # 복원 전에 읽어 본다
    if exists(n["hs"]) and not force:
        raise SystemExit(f"{n['hs']} already exists (use down first or --force)")
    avail = mem_available_mb()
    if avail is None or avail < MIN_AVAIL_MB:
        raise SystemExit(f"VM MemAvailable {avail}MB < {MIN_AVAIL_MB}MB - refusing (production is never stopped)")
    backup = backup or latest_backup()
    prod = inspect(PROD_HS)
    key = bench_key()
    if set_name:
        set_down(set_name)
    else:
        hs_down()
    if subprocess.run(["docker", "volume", "inspect", n["pg"]], capture_output=True).returncode == 0:
        raise SystemExit(f"volume {n['pg']} still exists after down (held by another container?) - refusing to "
                         f"extract a backup over an old cluster")
    tei_url = f"http://host.lima.internal:{sidecar_port}" if set_name else None
    sh("docker", "network", "create", NET, check=False)
    sh("docker", "volume", "create", "--label", "hc-bench=1", n["pg"])
    bdir, bfile = os.path.split(os.path.abspath(backup))
    sh("docker", "run", "--rm", "-v", f"{n['pg']}:/data", "-v", f"{bdir}:/b:ro", "alpine",
       "sh", "-c", f"tar xzf /b/{bfile} -C /data")
    _start_replica(n, hs_env(runtime_env(PROD_HS), user_env, key, tei_url), prod["Image"])
    meta = {"set": set_name, "backup": os.path.relpath(backup, REPO), "backup_sha256": sha256_file(backup),
            "image": prod["Image"], "started_at": inspect(n["hs"])["State"]["StartedAt"],
            "prod_hs_started_at": prod["State"]["StartedAt"],
            "hs_env": dict(user_env or {}),          # 실험용 upstream 설정 - 비어 있지 않으면 운영과 다른 복제본이다
            "sidecar_port": sidecar_port,            # None = 운영 사이드카 공유(기본 세트). 보조 포트일 수 있다
            **({} if sidecar_batch_size is None else {"sidecar_batch_size": sidecar_batch_size}),   # verify 가 info 와 대조
            "sidecar_name": sidecar["name"] if sidecar else None,
            "sidecar": dict(sidecar["overrides"]) if sidecar else None,   # 벤치 사이드카의 운영 대비 override
            **({} if expected is None else {"expected_overrides": expected})}   # arm 백업의 기대 뱅크 override(LINEAGE)
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    with open(os.path.join(STATE_DIR, n["state"]), "w") as fh:
        json.dump(meta, fh, indent=2)
    meta["bench_clock"] = set_clock(set_name)
    return meta


def set_clock(set_name: str = "") -> str:
    """벤치 시계를 복제본 데이터의 마지막 쓰기 시각 + 1분으로 고정해 replica.json 에 기록한다.
    recall 의 recency 점수와 상대 날짜 해석이 이 시계를 기준으로 계산된다(runner 가 query_timestamp 로 주입)."""
    last = hs_get(f"/v1/default/banks/{BANK}/stats", set_name).get("last_memory_write_at")
    if not last:
        raise SystemExit("replica bank stats has no last_memory_write_at - cannot pin the bench clock")
    t = dt.datetime.fromisoformat(last.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    clock = (t.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    path = os.path.join(STATE_DIR, instance_names(set_name)["state"])
    meta = json.load(open(path))
    meta["bench_clock"] = clock
    with open(path, "w") as fh:
        json.dump(meta, fh, indent=2)
    return clock


def hs_down() -> None:
    for name in [c for c in sh("docker", "ps", "-a", "--filter", "label=hc-bench=1",
                                "--format", "{{.Names}}").split() if c]:
        # 라벨 + 이름 이중 확인: 벤치가 만든 이름이 아니면 라벨이 붙어 있어도 건드리지 않는다
        if name == HS or name.startswith(HS + "-") or name.startswith(GW_PREFIX):
            sh("docker", "rm", "-f", name, check=False)
    for vol in [v for v in sh("docker", "volume", "ls", "--filter", "label=hc-bench=1",
                              "--format", "{{.Name}}").split() if v]:
        if vol == PG or vol.startswith(PG + "-"):
            sh("docker", "volume", "rm", vol, check=False)
    sh("docker", "volume", "rm", PG, check=False)
    for f in sorted(os.listdir(STATE_DIR)) if os.path.isdir(STATE_DIR) else []:
        if f.startswith("sidecar-") and f.endswith(".json"):
            sidecar_down(f[len("sidecar-"):-len(".json")])
        if (f.startswith("replica-") and f.endswith(".json")) or f == "replica.json":   # 기본 세트 state 도(남으면 run meta 에 옛 값이 실린다)
            os.remove(os.path.join(STATE_DIR, f))
    sh("docker", "network", "rm", NET, check=False)
    shutil.rmtree(os.path.join(STATE_DIR, "arms"), ignore_errors=True)     # 팔 출처는 컨테이너와 수명을 같이 한다
    if os.path.isdir(WORK_DIR):                                             # 팔 빌드 소스도 함께 치운다
        for d in sorted(os.listdir(WORK_DIR)):
            if d.startswith("wt-"):
                remove_arm_source(os.path.join(WORK_DIR, d))
        sh("git", "-C", REPO, "worktree", "prune", check=False)             # 옛 run 이 남긴 worktree 등록도 치운다


# ---------------------------------------------------------------- 벤치 사이드카 (세트 전용 리랭커)

PROD_SIDECAR_PLIST: Optional[str] = None   # None 이면 prod_sidecar_plist() 가 정한다(테스트는 경로를 끼운다)
RERANK_LABEL_SUFFIX = ".hindsight-rerank"


def _host_env_get(key: str, root: str) -> Optional[str]:
    """scripts/render-launchd.py 가 `__PREFIX__` 를 채우는 같은 함수(scripts/hc_host_env.get)로 읽는다."""
    scripts = os.path.join(REPO, "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import hc_host_env
    try:
        return hc_host_env.get(key, root)
    except hc_host_env.ConfigMissing as e:   # config_invalid: .env.host.local 을 UTF-8 로 읽지 못함
        raise SystemExit(str(e))


def _installer_default_prefix() -> str:
    """설치기 기본 접두(scripts/install/hc_install.py DEFAULT_PREFIX). 값을 복사하지 않고 그 상수를 읽는다."""
    install = os.path.join(REPO, "scripts", "install")
    if install not in sys.path:
        sys.path.insert(0, install)
    keep = sys.dont_write_bytecode          # hc_install 은 import 때 이 값을 True 로 바꾼다 - 벤치 프로세스엔 남기지 않는다
    try:
        import hc_install
    finally:
        sys.dont_write_bytecode = keep
    return hc_install.DEFAULT_PREFIX


def launchd_prefix(root: Optional[str] = None) -> str:
    """운영 LaunchAgent 라벨 접두: HC_LAUNCHD_PREFIX(환경 변수, 없으면 `<root>/.env.host.local`), 둘 다 없으면 설치기 기본 접두.
    `./install` 이 라벨을 붙이는 규칙과 같다 - 운영자가 HC_LAUNCHD_PREFIX 를 주면 그 라벨 그대로다."""
    return _host_env_get("HC_LAUNCHD_PREFIX", root or REPO) or _installer_default_prefix()


def prod_sidecar_plist(root: Optional[str] = None) -> str:
    """운영 사이드카 plist 경로 `~/Library/LaunchAgents/<HC_LAUNCHD_PREFIX>.hindsight-rerank.plist`(읽기만 한다)."""
    return os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", launchd_prefix(root) + RERANK_LABEL_SUFFIX + ".plist")


PROD_SIDECAR_INFO = f"http://127.0.0.1:{PROD_SIDECAR_PORT}/info"


def _port_in_use(port: int) -> bool:
    with socket.socket() as so:
        so.settimeout(1)
        return so.connect_ex(("127.0.0.1", port)) == 0


def sidecar_state(name: str) -> str:
    if not name:
        raise SystemExit("bench sidecar needs a name")
    instance_names(name)                                            # 같은 이름 규칙([a-z0-9-])
    return os.path.join(STATE_DIR, f"sidecar-{name}.json")


def sidecar_states() -> List[Dict[str, Any]]:
    out = []
    for f in sorted(os.listdir(STATE_DIR)) if os.path.isdir(STATE_DIR) else []:
        if f.startswith("sidecar-") and f.endswith(".json"):
            out.append(json.load(open(os.path.join(STATE_DIR, f))))
    return out


def sidecar_for_port(states: List[Dict[str, Any]], port: int) -> Dict[str, Any]:
    """세트가 붙을 벤치 사이드카. `sidecar-up` 으로 띄운 것만 - 운영 사이드카에는 기본 세트만 붙는다."""
    if port == PROD_SIDECAR_PORT:
        raise SystemExit("named sets cannot use the production sidecar")
    hits = [st_ for st_ in states if port in sidecar_ports(st_)]       # 주 포트 또는 보조 포트
    if len(hits) > 1:
        raise SystemExit(f"port {port} is claimed by several bench sidecar states "
                         f"{[h.get('name') for h in hits]} - `sidecar-down` the stale one")
    if hits:
        return hits[0]
    raise SystemExit(f"no bench sidecar on port {port} - run `sidecar-up` first")


def recorded_sidecar(meta: Dict[str, Any], states: List[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], bool]:
    """세트가 up 때 기록한 벤치 사이드카를 이름으로 찾고, 지금 포트·override 가 그 기록과 같은지.
    포트로 찾으면 재기동·잔여 state 로 다른 사이드카를 보고도 출처는 옛 기록으로 남는다."""
    sc = next((x for x in states if x.get("name") == meta.get("sidecar_name")), None)
    return sc, bool(sc) and meta.get("sidecar_port") in sidecar_ports(sc) \
        and (sc.get("overrides") or {}) == (meta.get("sidecar") or {})


def sidecar_up(name: str, port: int, overrides: Optional[Dict[str, str]]) -> Dict[str, Any]:
    """운영 launchd 사이드카와 같은 코드·모델·설정으로 벤치 사이드카를 띄운다(override·포트만 다르다).
    운영 사이드카(8787)는 건드리지 않는다. 프로세스는 새 세션으로 분리해 이 명령이 끝나도 살아 있다."""
    set_name = name
    if os.path.exists(sidecar_state(name)):
        raise SystemExit(f"bench sidecar {name} already has state - `sidecar-down {name}` first")
    want_ports = [port] + ([_int_env(overrides[AUX_PORT_KEY])] if AUX_PORT_KEY in (overrides or {}) else [])
    claimed = [s.get("name") for s in sidecar_states() if set(sidecar_ports(s)) & set(want_ports)]
    if claimed:     # 죽은 사이드카의 state 가 남은 포트(주·보조) - 세트가 이름·override 를 잘못 기록하게 된다
        raise SystemExit(f"port {want_ports} is still claimed by bench sidecar state {claimed} - `sidecar-down` it first")
    with open(PROD_SIDECAR_PLIST or prod_sidecar_plist(), "rb") as fh:
        plist = plistlib.load(fh)
    env, argv = sidecar_launch(plist.get("EnvironmentVariables") or {}, plist["ProgramArguments"], overrides, port)
    busy = [p for p in want_ports if _port_in_use(p)]
    if busy:
        raise SystemExit(f"port {busy} is already in use")
    os.makedirs(WORK_DIR, mode=0o700, exist_ok=True)
    log = os.path.join(WORK_DIR, f"sidecar-{set_name}.log")
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    proc = subprocess.Popen(argv, env=env, cwd=plist.get("WorkingDirectory"), stdin=subprocess.DEVNULL,
                            stdout=fd, stderr=subprocess.STDOUT, start_new_session=True)
    os.close(fd)
    state = {"name": set_name, "pid": proc.pid, "port": port, "overrides": dict(overrides or {}),
             "argv": argv, "started_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    with open(sidecar_state(set_name), "w") as fh:
        json.dump(state, fh, indent=2)
    deadline = time.time() + 240
    while time.time() < deadline:
        if proc.poll() is not None:
            os.remove(sidecar_state(set_name))                # 로그 읽기·디코딩이 어떻게 되든 stale state 를 남기지 않는다(issue #165)
            with open(log, "rb") as fh:                       # 바이트로 읽어 단독 \r 을 보존하고 비 UTF-8 은 대체 문자로 받는다
                raw = fh.read().decode("utf-8", errors="replace")
            tail = _mask_stderr(raw, tuple(_secret_values(env)))[-600:]    # 마스킹이 먼저 - 꼬리가 시크릿 중간에서 시작하면 못 가린다
            raise SystemExit(f"bench sidecar {set_name} exited: {tail}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as r:
                if r.status == 200:
                    return state
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(2)
    sidecar_down(set_name)
    raise SystemExit(f"bench sidecar {set_name} not healthy within 240s")


def _sidecar_process_ok(state: Dict[str, Any]) -> bool:
    """pid 가 아직 그 벤치 사이드카인지(포트 인자로 확인) - pid 재사용으로 남의 프로세스를 죽이지 않게."""
    cmd = sh("ps", "-o", "command=", "-p", str(state["pid"]), check=False)
    return f"--port {state['port']}" in cmd and state["port"] != PROD_SIDECAR_PORT


def sidecar_down(set_name: str) -> None:
    path = sidecar_state(set_name)
    try:
        state = json.load(open(path))
    except FileNotFoundError:
        return
    if _sidecar_process_ok(state):
        try:
            os.killpg(state["pid"], signal.SIGTERM)             # uv -> uvicorn 자식까지(새 세션의 프로세스 그룹)
            for _ in range(20):
                time.sleep(0.5)
                if not _sidecar_process_ok(state):
                    break
            else:
                os.killpg(state["pid"], signal.SIGKILL)
        except ProcessLookupError:
            pass
    os.remove(path)


def set_down(set_name: str) -> None:
    """이름 있는 세트 하나(복제본·볼륨·state)만 내린다. 공유 벤치 사이드카·다른 세트·팔은 그대로 둔다."""
    if not set_name:
        raise SystemExit("use `down` for the default set")
    n = instance_names(set_name)
    sh("docker", "rm", "-f", n["hs"], check=False)
    sh("docker", "volume", "rm", n["pg"], check=False)
    try:
        os.remove(os.path.join(STATE_DIR, n["state"]))
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------- 게이트웨이 팔

def _rm_path(path: str) -> None:
    """파일·심링크(끊긴 것 포함)·디렉터리 무엇이든 지운다. 없으면 아무 일 없다."""
    if os.path.islink(path) or os.path.isfile(path):
        os.remove(path)
    elif os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)


def export_commit(sha: str, dest: str) -> None:
    """sha 의 트리를 dest 에 푼다(git archive | tar -x). worktree 와 달리 리포의 worktree 목록에 등록되지 않아
    Orca 사이드바에 "Detached HEAD" 항목이 남지 않는다(issue #171). 빌드 이후 단계는 .git 을 쓰지 않는다."""
    _rm_path(dest)                                              # 파일·끊긴 심링크 잔여물이 makedirs 를 막지 않게
    if os.path.lexists(dest):
        raise RuntimeError(f"cannot clear {dest} before extracting {sha[:12]}")
    os.makedirs(dest, mode=0o700)
    ok = False
    with tempfile.TemporaryFile() as err:                       # PIPE 로 받으면 tar 가 끝나길 기다리는 동안 64KB 를 넘는 stderr 에 git 이 막힌다
        ga = subprocess.Popen(["git", "-C", REPO, "archive", "--format=tar", sha], stdout=subprocess.PIPE, stderr=err)
        try:
            tx = subprocess.run(["tar", "-x", "-f", "-", "-C", dest], stdin=ga.stdout, capture_output=True)
            ga.stdout.close()
            ok = ga.wait() == 0 and tx.returncode == 0
        finally:
            ga.stdout.close()
            if ga.poll() is None:                               # tar 실행 실패·중단 시 git archive 를 남기지 않는다
                ga.kill()
                ga.wait()
            if not ok:
                shutil.rmtree(dest, ignore_errors=True)         # 부분 추출물은 빌드 컨텍스트로 쓰이면 안 된다
        if not ok:
            err.seek(0)
            raise RuntimeError(f"git archive {sha[:12]} -> tar exit {ga.returncode}/{tx.returncode}: "
                               f"{_mask_stderr(_decode(err.read() + tx.stderr, False))[-400:]}")


def _registered_worktrees() -> List[str]:
    out = sh("git", "-C", REPO, "worktree", "list", "--porcelain", check=False)
    return [os.path.realpath(ln[len("worktree "):]) for ln in out.splitlines() if ln.startswith("worktree ")]


def remove_arm_source(wt: str) -> None:
    """팔 빌드 소스 디렉터리를 지운다. 이 변경 이전 run 이 worktree 로 만들어 둔 것이면 등록도 같이 푼다.
    등록을 못 풀면(잠금 등) 디렉터리를 지우지 않고 이유와 함께 끝낸다 - 등록이 흔적 없이 영구 잔존하지 않게."""
    if os.path.realpath(wt) in _registered_worktrees() and os.path.isdir(wt):
        try:
            sh("git", "-C", REPO, "worktree", "remove", "--force", wt)
        except RuntimeError as e:
            raise RuntimeError(f"{wt} is a registered git worktree and could not be removed "
                               f"(if locked, run `git worktree unlock {wt}` and retry): {e}") from None
    _rm_path(wt)
    sh("git", "-C", REPO, "worktree", "prune", check=False)     # 디렉터리가 사라진 옛 등록도 치운다
    if os.path.realpath(wt) in _registered_worktrees():
        raise RuntimeError(f"{wt} is still a registered git worktree after prune "
                           f"(if locked, run `git worktree unlock {wt}` and retry)")


def apply_patch(wt: str, patch: str) -> None:
    """추출본(.git 없음)에 patch 를 적용한다. git 이 상위 디렉터리의 다른 리포를 잡아 경로를 엉뚱하게 해석하면
    패치가 조용히 건너뛰어진 채 exit 0 이 되므로, 리포 탐색을 wt 에서 막고 적용 결과를 되짚어 본다."""
    parent = os.path.realpath(os.path.dirname(wt))
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    env["GIT_CEILING_DIRECTORIES"] = parent
    patch = os.path.abspath(patch)

    def run(*flags: str) -> "subprocess.CompletedProcess[bytes]":
        return subprocess.run(["git", "-C", wt, "apply", *flags, patch], capture_output=True, env=env)

    r = run("--whitespace=nowarn")
    if r.returncode != 0:
        raise RuntimeError(f"git apply exit {r.returncode}: {_mask_stderr(_decode(r.stderr, False))[-400:]}")
    if run("--check", "-R").returncode != 0:                    # 적용됐다면 되돌리기 검사가 통과해야 한다
        raise RuntimeError(f"git apply exit 0 but the patch is not present in {wt} (skipped by repo discovery?)")


def gw_up(arm: str, ref: str, port: int, env: Optional[Dict[str, str]] = None,
          patch: Optional[str] = None, set_name: str = "", local_policies: Optional[str] = None) -> Dict[str, Any]:
    """git ref(+선택 patch)로 게이트웨이 이미지를 빌드해 세트 복제본 앞에 세운다.
    local_policies: 로컬 정책 원본 디렉터리(비우면 env, 그다음 이 리포의 policies/local/ - resolve_local_policies_dir)."""
    check_arm_name(arm)
    check_user_env(env)                     # 소스 추출·이미지 빌드 전에 거부 - 잔여물을 남기지 않는다
    resolve_local_policies_dir(local_policies)
    n = instance_names(set_name)
    try:
        set_meta = json.load(open(os.path.join(STATE_DIR, n["state"])))
    except FileNotFoundError:
        raise SystemExit(f"set {set_name!r} has no replica - run `up{' --set ' + set_name if set_name else ''}` first")
    name = GW_PREFIX + arm
    sh("docker", "rm", "-f", name, check=False)
    wt = os.path.join(WORK_DIR, "wt-" + arm)
    remove_arm_source(wt)
    os.makedirs(WORK_DIR, mode=0o700, exist_ok=True)
    os.chmod(WORK_DIR, 0o700)
    sha = sh("git", "-C", REPO, "rev-parse", "--verify", ref + "^{commit}")
    export_commit(sha, wt)
    patch_sha = None
    if patch:
        apply_patch(wt, patch)
        patch_sha = sha256_file(patch)[:12]
    local = inject_local_policies(wt, local_policies)       # 패치 뒤, 마운트(docker run) 전
    image = f"hc-bench-gw:{arm}"
    sh("docker", "build", "-q", "-t", image, os.path.join(wt, "gateway"))
    genv = gateway_env(env, bench_key(), n["alias"])
    envfile = write_envfile(genv)
    try:
        sh("docker", "run", "-d", "--name", name, "--network", NET, "--label", "hc-bench=1",
           "--env-file", envfile,
           "-v", f"{bench_acl_dir()}:/secrets:ro",                      # 운영 secrets/ 가 아니다
           "-v", f"{os.path.join(wt, 'policies')}:/app/policies:ro",
           "--tmpfs", "/audit:rw,size=64m", "-p", f"127.0.0.1:{port}:8888",
           image, "python", "-m", "gateway.server")
    finally:
        os.remove(envfile)
    for _ in range(60):
        code, body = http_json(f"http://127.0.0.1:{port}/healthz", "")
        if code == 200 and isinstance(body, dict) and body.get("upstream_ok"):
            break
        time.sleep(1)
    else:
        raise SystemExit(f"{name} not healthy: {_mask_stderr(sh('docker', 'logs', '--tail', '20', name, check=False, keep_cr=True), tuple(_secret_values(genv)))}")
    prov = {"arm": arm, "ref": ref, "commit": sha, "patch_sha256": patch_sha, "port": port,
            "image": inspect(name)["Image"], "env_overrides": sorted((env or {}).keys()),
            "env_override_values": {k: vh(v) for k, v in (env or {}).items()},
            "container_started_at": inspect(name)["State"]["StartedAt"],
            "set": set_name, "sidecar_port": set_meta.get("sidecar_port"), "local_policies": local}
    # runner 가 run meta 에 팔 출처로 싣는다(다른 코드·설정의 run 끼리 비교되는 것을 판정기가 알 수 있게)
    os.makedirs(os.path.join(STATE_DIR, "arms"), mode=0o700, exist_ok=True)
    with open(os.path.join(STATE_DIR, "arms", arm + ".json"), "w") as fh:
        json.dump(prov, fh, indent=2)
    return prov


LOCAL_POLICIES_ENV = "HC_BENCH_LOCAL_POLICIES"
LOCAL_POLICY_FILES = ("bank-policies.json", "context-triggers.json")


def resolve_local_policies_dir(flag: Optional[str] = None) -> Tuple[str, str]:
    """로컬 정책 원본 디렉터리(절대 경로)와 출처. 우선순위: CLI 플래그 > env HC_BENCH_LOCAL_POLICIES > 벤치를 실행하는 리포의 policies/local/.
    플래그·env 로 명시한 경로가 디렉터리가 아니거나 두 정책 파일이 하나도 없으면 거부한다 - 오타가 조용히 '없음'이 되면
    템플릿 정책으로 판정하게 된다. 기본 경로는 없어도 지금처럼 '없음'으로 진행한다."""
    for src, val in (("flag", flag), ("env", os.environ.get(LOCAL_POLICIES_ENV))):
        if val:
            if not os.path.isdir(val):
                raise SystemExit(f"local policies dir from {src} is not a directory: {val}")
            if not any(os.path.isfile(os.path.join(val, n)) for n in LOCAL_POLICY_FILES):
                raise SystemExit(f"local policies dir from {src} has none of {', '.join(LOCAL_POLICY_FILES)}: {val}")
            return os.path.abspath(val), src
    return os.path.join(REPO, "policies", "local"), "default"


def _check_inside_arm_tree(wt: str, *paths: str) -> None:
    """추출본(wt)의 policies·policies/local·대상 파일이 심링크이거나 realpath 가 wt 밖이면 거부한다.
    ref·patch 가 심링크를 심어 두면 복사가 링크를 따라가 호스트 파일을 덮어쓴다(원본은 읽기만 한다는 격리가 깨진다)."""
    root = os.path.realpath(wt)
    for p in paths:
        if os.path.islink(p):
            raise SystemExit(f"arm tree path is a symlink, refusing to copy local policies through it: {os.path.relpath(p, wt)}")
        real = os.path.realpath(p)
        if real != root and not real.startswith(root + os.sep):
            raise SystemExit(f"arm tree path resolves outside the arm tree, refusing to copy local policies: {os.path.relpath(p, wt)}")


def inject_local_policies(wt: str, flag: Optional[str] = None) -> Dict[str, Any]:
    """gitignore 된 policies/local/ 은 git archive 에 없으므로(issue #187) 원본의 두 파일 중 있는 것만 추출본에 복사한다.
    원본은 읽기만 하고 복사본은 새 파일이다. 내용은 출력하지 않고 파일별 sha256 만 돌려준다(run 메타데이터용)."""
    src_dir, source = resolve_local_policies_dir(flag)
    found = [n for n in LOCAL_POLICY_FILES if os.path.isfile(os.path.join(src_dir, n))]
    files: Dict[str, str] = {}
    if found:
        pol = os.path.join(wt, "policies")
        dest = os.path.join(pol, "local")
        _check_inside_arm_tree(wt, pol, dest, *(os.path.join(dest, n) for n in found))
        os.makedirs(dest, exist_ok=True)
        for n in found:
            shutil.copyfile(os.path.join(src_dir, n), os.path.join(dest, n))
            files[n] = sha256_file(os.path.join(dest, n))
            if files[n] != sha256_file(os.path.join(src_dir, n)):
                raise RuntimeError(f"local policy {n} changed while copying")
    return {"source": source, "dir": src_dir, "result": "복사" if files else "없음", "files": files}


def check_arm_name(arm: str) -> None:
    if not arm or not arm.replace("-", "").isalnum():
        raise SystemExit("arm name must be [A-Za-z0-9-]")


def gw_down(arm: str) -> None:
    check_arm_name(arm)
    sh("docker", "rm", "-f", GW_PREFIX + arm, check=False)
    try:
        os.remove(os.path.join(STATE_DIR, "arms", arm + ".json"))
    except FileNotFoundError:
        pass
    remove_arm_source(os.path.join(WORK_DIR, "wt-" + arm))


# ---------------------------------------------------------------- 검증

def diff_env(prod: Dict[str, str], bench: Dict[str, str], overrides: Dict[str, Optional[str]]
             ) -> Dict[str, List[str]]:
    """운영 대비 차이를 분류한다. unexpected 가 비어야 동일성 통과."""
    expected, unexpected = [], []
    for k in sorted(set(prod) | set(bench)):
        if prod.get(k) == bench.get(k):
            continue
        (expected if k in overrides else unexpected).append(k)
    return {"expected": expected, "unexpected": unexpected}


def hs_get(path: str, set_name: str = "") -> Any:
    """복제본 hindsight 에서 GET. 키는 argv 가 아니라 stdin 으로 넘긴다(ps 노출 방지)."""
    code = ("import sys,urllib.request as u;k=sys.stdin.read().strip();"
            "print(u.urlopen(u.Request('http://localhost:8888'+sys.argv[1],"
            "headers={'Authorization':'Bearer '+k}),timeout=30).read().decode())")
    return json.loads(sh("docker", "exec", "-i", instance_names(set_name)["hs"], "python", "-c", code, path,
                         input_=bench_key()))


def freeze_fingerprint(set_name: str = "") -> Dict[str, Any]:
    """복제본의 데이터 지문. 동결이면 run 전후로 같아야 한다.
    operations 는 최신순 100건(업스트림 상한)의 (id, status, updated_at) - 새 작업이나 상태 변화가 있으면 바뀐다.
    멘탈모델은 전체(상한 1000)의 (id, last_refreshed_at, content 해시) - 개수가 같은 채 내용이 바뀌는 것도 잡는다.
    개별 기억의 내용 수정은 stats·operations 로 간접 관측할 뿐이다(전수 해시는 run 마다 수만 건 조회라 뺐다)."""
    stats = hs_get(f"/v1/default/banks/{BANK}/stats", set_name)
    ops_raw = hs_get(f"/v1/default/banks/{BANK}/operations?limit=100", set_name)
    mm_raw = hs_get(f"/v1/default/banks/{BANK}/mental-models?detail=content&limit=1000", set_name)
    mms = mm_raw.get("items") if isinstance(mm_raw, dict) else mm_raw
    mm_digest = hashlib.sha256(json.dumps(
        sorted((m.get("id"), m.get("last_refreshed_at"), vh(json.dumps(m.get("content"), sort_keys=True)))
               for m in (mms or [])), sort_keys=True).encode()).hexdigest()[:16]
    keep = {k: stats.get(k) for k in ("total_nodes", "total_links", "total_documents", "nodes_by_fact_type",
                                        "links_by_link_type", "pending_operations", "failed_operations",
                                        "operations_by_status", "last_consolidated_at",
                                        "last_memory_write_at", "pending_consolidation")}
    ops = ops_raw.get("operations") if isinstance(ops_raw, dict) else ops_raw
    ops_digest = hashlib.sha256(json.dumps(
        [(o.get("id"), o.get("status"), o.get("updated_at")) for o in (ops or [])],
        sort_keys=True).encode()).hexdigest()[:16]
    return {"stats": keep, "operations_digest": ops_digest, "operations_seen": len(ops or []),
            "mental_models_digest": mm_digest, "mental_models_seen": len(mms or [])}


def sidecar_batch_report(meta: Dict[str, Any], tei_info: Any) -> Optional[Dict[str, Any]]:
    """세트가 기록한 기대 배치(팔 설정)와 세트가 실제로 붙은 포트의 사이드카 info 를 대조한다(issue #83).
    기대 배치가 없으면 None - 대조하지 않는다(기존 동작). info.batch_size 는 요청이 도착한 포트의 배치이고,
    batch_sizes 는 모든 포트의 배치다. 기대 배치와 info 가 같아야 하고, batch_sizes 에 그 포트가 있으면 그 값도 같아야 한다.
    보조 포트가 없는 사이드카의 info 에는 이 필드들이 없다 - 말하지 않으면 확인할 수 없으므로 불일치다."""
    want = meta.get("sidecar_batch_size")
    if want is None:
        return None
    info = tei_info if isinstance(tei_info, dict) else {}
    got = info.get("batch_size")
    by_port = (info.get("batch_sizes") or {}).get(str(meta.get("sidecar_port")))
    match = got == want and not isinstance(got, bool) and (by_port is None or by_port == want)
    return {"expected": want, "port": meta.get("sidecar_port"), "info_batch_size": got,
            "info_batch_sizes_entry": by_port, "match": match}


def _url_port(url: str) -> Optional[int]:
    try:
        return urllib.parse.urlsplit(url).port
    except ValueError:
        return None


EXPECTED_OVERRIDES_KEY = "expected_bank_overrides"       # LINEAGE JSON 의 기대 뱅크 override 키(bench/README.md "arm 백업")


def load_expected_overrides(path: str) -> Dict[str, Any]:
    """arm 백업 LINEAGE 에서 기대 뱅크 override 를 읽는다. 키가 없거나 객체가 아니면 거부한다 - 빈 기대로 조용히
    바뀌면 운영과 다른 복제본이 통과한다. 출처(절대 경로·sha256)를 함께 남긴다."""
    try:
        with open(path) as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as e:
        raise SystemExit(f"cannot read expected overrides from {path}: {e}")
    ovr = doc.get(EXPECTED_OVERRIDES_KEY) if isinstance(doc, dict) else None
    if not isinstance(ovr, dict):
        raise SystemExit(f"{path} has no {EXPECTED_OVERRIDES_KEY} object")
    return {"source": os.path.abspath(path), "sha256": sha256_file(path), "overrides": ovr}


# 설정 차이 중 env override 로 설명되는 것(llm·consolidation 계열)만 허용
ENV_EXPLAINED_CFG = ("llm", "enable_auto_consolidation", "consolidation", "enable_observations",
                     "retain_extraction_mode", "reflect")


def bank_override_report(prod_cfg: Dict[str, Any], bench_cfg: Dict[str, Any],
                         expected: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """뱅크 override·해석된 설정 대조. expected 가 없으면 운영과 같아야 한다(기존 동작).
    expected(LINEAGE 의 기대 override)가 있으면 복제본 override 가 그것과 같아야 하고, 운영 override 와 기대가 다른 키는
    해석된 설정 차이에서도 설명된 것으로 본다(arm 의 범위·미션·성향·관찰 우선, D10 의 override 없음, issue #297)."""
    prod_o, bench_o = prod_cfg.get("overrides") or {}, bench_cfg.get("overrides") or {}
    cfg_diff = sorted(k for k in set(prod_cfg["config"]) | set(bench_cfg["config"])
                      if prod_cfg["config"].get(k) != bench_cfg["config"].get(k))
    allowed = {k for k in cfg_diff if k.startswith(ENV_EXPLAINED_CFG)}
    if expected is None:
        equal = prod_o == bench_o
    else:
        equal = bench_o == expected
        allowed |= {k for k in set(prod_o) | set(expected) if prod_o.get(k) != expected.get(k)}
    return {"bank_overrides_equal": equal, "bank_config_diff": cfg_diff,
            "bank_config_diff_unexplained": sorted(set(cfg_diff) - allowed)}


def verify(set_name: str = "", expected_overrides: Optional[str] = None) -> Dict[str, Any]:
    """expected_overrides = LINEAGE 경로(주면 up 때 기록한 기대보다 우선). 둘 다 없으면 운영 override 와 대조한다."""
    key = bench_key()
    n = instance_names(set_name)
    prod, bench = inspect(PROD_HS), inspect(n["hs"])
    prod_env, bench_env = runtime_env(PROD_HS), runtime_env(n["hs"])
    try:
        with open(os.path.join(STATE_DIR, n["state"])) as fh:
            meta = json.load(fh)
    except FileNotFoundError:
        meta = {}
    user = meta.get("hs_env") or {}
    overrides: Dict[str, Optional[str]] = {**HS_OVERRIDES, **user}
    if meta.get("sidecar_port"):
        overrides[TEI_URL_KEY] = f"http://host.lima.internal:{meta['sidecar_port']}"
    envd = diff_env(prod_env, bench_env, overrides)
    wrong_override = [k for k, v in overrides.items()
                      if (bench_env.get(k) != (key if v == "<bench-key>" else v))]
    prod_cfg = upstream_get_via(PROD_GW, f"/v1/default/banks/{BANK}/config")
    bench_cfg = hs_get(f"/v1/default/banks/{BANK}/config", set_name)
    expected = load_expected_overrides(expected_overrides) if expected_overrides else meta.get("expected_overrides")
    bank = bank_override_report(prod_cfg, bench_cfg, (expected or {}).get("overrides") if expected else None)
    tei = sh("docker", "exec", n["hs"], "python", "-c",
             "import os,urllib.request as u;print(u.urlopen(os.environ['HINDSIGHT_API_RERANKER_TEI_URL']"
             "+'/info',timeout=5).read().decode())", check=False)
    report = {
        "image_equal": prod["Image"] == bench["Image"],
        "env": envd,
        "env_wrong_override": wrong_override,
        **bank,
        "expected_overrides": None if expected is None else {k: expected[k] for k in ("source", "sha256")},
        "recall_budget_function": bench_cfg["config"].get("recall_budget_function"),
        "hsauth_mounted": any("hsauth" in (m.get("Name") or "") for m in bench["Mounts"]),
        "hfcache_rw": [m.get("RW") for m in bench["Mounts"] if m.get("Name") == PROD_HFCACHE],
        "tei_info": json.loads(tei) if tei.startswith("{") else tei,
        "freeze": freeze_fingerprint(set_name),
        "prod_hs_started_at": prod["State"]["StartedAt"],
    }
    report["pass"] = (report["image_equal"] and not envd["unexpected"] and not wrong_override
                      and not report["bank_config_diff_unexplained"] and report["bank_overrides_equal"]
                      and not report["hsauth_mounted"] and report["hfcache_rw"] == [False] and isinstance(report["tei_info"], dict)
                      and not report["tei_info"].get("degraded"))
    if set_name:
        # 벤치 사이드카가 운영과 같은 모델·정밀도·장치인지, 기록한 그 프로세스가 살아 있는지
        try:
            with urllib.request.urlopen(PROD_SIDECAR_INFO, timeout=5) as r:
                prod_info = json.loads(r.read())
        except (urllib.error.URLError, ConnectionError, OSError, ValueError):
            prod_info = None
        sc, matches = recorded_sidecar(meta, sidecar_states())
        same = ("model_id", "revision", "dtype", "device")
        report["sidecar"] = {"state": sc, "alive": bool(sc) and _sidecar_process_ok(sc), "matches_record": matches,
                             "same_model_as_prod": isinstance(prod_info, dict) and isinstance(report["tei_info"], dict)
                             and all(prod_info.get(k) == report["tei_info"].get(k) for k in same),
                             # 이름 있는 세트는 운영 사이드카를 칠 수 없다 - 기록도, 컨테이너가 실제로 가리키는 주소도 (issue #83)
                             "targets_production": PROD_SIDECAR_PORT in (meta.get("sidecar_port"),
                                                                         _url_port(bench_env.get(TEI_URL_KEY, "")))}
        report["pass"] = report["pass"] and report["sidecar"]["alive"] and report["sidecar"]["matches_record"] \
            and report["sidecar"]["same_model_as_prod"] and not report["sidecar"]["targets_production"]
        batch = sidecar_batch_report(meta, report["tei_info"])        # 팔 설정의 기대 배치 == 세트가 붙은 포트의 info 배치
        if batch is not None:
            report["sidecar_batch"] = batch
            report["pass"] = report["pass"] and batch["match"]
    return report


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("up"); up.add_argument("--backup"); up.add_argument("--force", action="store_true")
    up.add_argument("--hs-env", action="append", default=[], help="K=V 실험용 upstream 설정(동결 키 제외)")
    up.add_argument("--set", default="", help="세트 이름(자기 복제본·벤치 사이드카). 비우면 기본 세트")
    up.add_argument("--sidecar-port", type=int, help="세트가 붙을 벤치 사이드카 포트(`sidecar-up` 으로 띄운 것의 주 또는 보조 포트)")
    up.add_argument("--sidecar-batch-size", type=int, help="그 포트의 기대 배치 크기 - verify 가 사이드카 info 와 대조한다")
    up.add_argument("--expected-overrides", help="arm 백업 LINEAGE 경로 - verify 가 운영 대신 그 기대 뱅크 override 와 대조한다")
    su = sub.add_parser("sidecar-up"); su.add_argument("name"); su.add_argument("--port", type=int, required=True)
    su.add_argument("--env", action="append", default=[], help="K=V 벤치 사이드카 override(운영 plist 대비)")
    sdn = sub.add_parser("sidecar-down"); sdn.add_argument("name")
    sd = sub.add_parser("set-down"); sd.add_argument("set")
    rc = sub.add_parser("reconf"); rc.add_argument("set")
    rc.add_argument("--hs-env", action="append", default=[], help="K=V 새 upstream 설정(비우면 운영과 같은 설정)")
    rc.add_argument("--sidecar-port", type=int, help="이 설정(팔)이 쓸 벤치 사이드카 포트(주 또는 보조). 주면 --sidecar-batch-size 도 준다")
    rc.add_argument("--sidecar-batch-size", type=int, help="그 포트의 기대 배치 크기")
    sub.add_parser("down")
    v = sub.add_parser("verify"); v.add_argument("--set", default="")
    v.add_argument("--expected-overrides", help="LINEAGE 경로(up 때 기록한 기대보다 우선)")
    c = sub.add_parser("clock"); c.add_argument("--set", default="")
    g = sub.add_parser("gw-up")
    g.add_argument("arm"); g.add_argument("--ref", default="HEAD"); g.add_argument("--port", type=int, required=True)
    g.add_argument("--env", action="append", default=[]); g.add_argument("--patch")
    g.add_argument("--local-policies", help=f"로컬 정책 원본 디렉터리(우선순위: 이 플래그 > env {LOCAL_POLICIES_ENV} > 이 리포의 policies/local/)")
    g.add_argument("--set", default="", help="팔이 붙을 세트 복제본")
    gd = sub.add_parser("gw-down"); gd.add_argument("arm")
    # 갱신 replica 모드 (issue #229) - recall 동결 복제본과 이름·state 가 분리된 별도 명령. 구현은 bench/refresh.py
    ru = sub.add_parser("refresh-up", help="갱신 전용 복제본 기동(provider 하나, 폴백 없음)")
    ru.add_argument("arm"); ru.add_argument("--backup", required=True)
    ru.add_argument("--provider", required=True, help="claude-code | xai-oauth")
    ru.add_argument("--model", required=True); ru.add_argument("--sidecar-port", type=int, required=True)
    ru.add_argument("--token-env-file", help="claude-code: KEY=VALUE 줄 파일"); ru.add_argument("--token-key")
    ru.add_argument("--xai-auth", help="xai-oauth: 별도 로그인한 볼륨 이름 또는 절대 디렉터리(운영 hsauth 거부)")
    ru.add_argument("--claude-cli", help="기본 = 메인 체크아웃 vendor/claude-cli/claude")
    rr = sub.add_parser("refresh-run", help="지정 멘탈 모델 full rebuild(clear -> refresh -> terminal 대기)")
    rr.add_argument("arm"); rr.add_argument("--model-id", action="append", default=[], required=True)
    rr.add_argument("--out", required=True, help="산출물 디렉터리(리포 밖)")
    rr.add_argument("--bank", default=BANK); rr.add_argument("--timeout", type=float, default=900)
    rd = sub.add_parser("refresh-down", help="갱신 복제본 컨테이너·볼륨·state 삭제(호출자 인증 저장소는 보존)")
    rd.add_argument("arm")
    a = ap.parse_args(argv)
    if a.cmd.startswith("refresh-"):
        if REPO not in sys.path:                     # `python bench/replica.py` 로 실행해도 bench 패키지를 찾게
            sys.path.insert(0, REPO)
        from bench import refresh as F
        if a.cmd == "refresh-up":
            out = F.refresh_up(a.arm, a.backup, a.provider, a.model, a.sidecar_port, a.token_env_file,
                               a.token_key, a.xai_auth, a.claude_cli)
        elif a.cmd == "refresh-run":
            out = F.refresh_run(a.arm, a.model_id, a.out, a.bank, a.timeout)
        else:
            out = F.refresh_down(a.arm)
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return 0 if out.get("pass", True) else 1
    if a.cmd == "up":
        out: Any = hs_up(a.backup, a.force, dict(e.split("=", 1) for e in a.hs_env), a.set, a.sidecar_port,
                    a.sidecar_batch_size, a.expected_overrides)
    elif a.cmd == "sidecar-up":
        out = sidecar_up(a.name, a.port, dict(e.split("=", 1) for e in a.env))
    elif a.cmd == "sidecar-down":
        sidecar_down(a.name); out = {"down": a.name}
    elif a.cmd == "reconf":
        out = reconf(a.set, dict(e.split("=", 1) for e in a.hs_env), a.sidecar_port, a.sidecar_batch_size)
    elif a.cmd == "set-down":
        set_down(a.set); out = {"down": a.set}
    elif a.cmd == "down":
        hs_down(); out = {"down": True}
    elif a.cmd == "verify":
        out = verify(a.set, a.expected_overrides)
    elif a.cmd == "clock":
        out = {"bench_clock": set_clock(a.set)}
    elif a.cmd == "gw-up":
        out = gw_up(a.arm, a.ref, a.port, dict(e.split("=", 1) for e in a.env), a.patch, a.set, a.local_policies)
    else:
        gw_down(a.arm); out = {"down": a.arm}
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0 if not isinstance(out, dict) or out.get("pass", True) else 1


if __name__ == "__main__":
    sys.exit(main())
