#!/usr/bin/env python3
"""S4 역할 판정기 호스트 사이드카 (issue #376, 상위 #360 "S4·S3 구현").

  item_roles_sidecar.py backfill --db <path> [--bank <id>] [--policies <json> | --aliases-file <json>] [--max-usd X]
  item_roles_sidecar.py poll     --db <path> [같은 인자]
  item_roles_sidecar.py status   --db <path> [--bank <id>] [--offline]
  item_roles_sidecar.py rekey    --db <원본> --out <새 경로> --rules v1|v2 [poll 과 같은 인자] [--save-listing <bodies> <meta>]
  공통: [--allow-prod] (사본이 아닌 대상, 즉 HC_TARGET=next 가 아닌 대상은 이 인자가 있어야 읽는다)
  backfill·poll·rekey: [--rules v1|v2] [--github [--github-owners <json>] [--github-default-owner <owner>]
                        [--github-min-interval <초>]]

기억마다 한 번 haiku 로 키 언급 역할(done/open/ref)을 판정해 sqlite 사이드 저장소(`gateway/item_roles.py` 계약 v1)에 쓴다.
게이트웨이 읽기 경로 밖에서 돌고 upstream 에는 쓰지 않는다(GET 만). 실패는 fail-open 이다: 판정하지 못한 기억은 저장소에
행이 없거나 옛 행 그대로 남고 다음 실행에서 다시 본다. 게이트웨이와 retain 에는 영향이 없다.

  backfill  저장소 파일이 없을 때만. 뱅크의 모든 기억(world·experience) 중 키가 있는 것을 판정한다.
            지출 상한·오류로 멈추면 거기까지 공개하고, 나머지는 poll 이 이어서 판정한다.
  poll      한 번 돌고 끝난다(주기는 launchd 예시). 신규 기억과 text_sha256 이나 키 집합이 바뀐 기억만 LLM 으로 판정한다
            (바뀐 기억의 옛 행은 먼저 빼므로 판정이 실패하면 행 없는 미판정이 된다).
            state·mentioned_at·document_id 만 바뀐 기억은 memories 행만 고친다. upstream 에서 사라졌거나 키가 없어진
            기억은 행을 지운다. 저장소가 없으면 backfill 과 같다.
  status    판정 수·역할 분포·meta 와(--offline 이 아니면 upstream 을 읽어) 미판정 수.

기억 나열: upstream `GET /v1/default/banks/<bank>/memories/list?type=<world|experience>&limit=&offset=` 전수 페이징
  (끝에 id 수 = total 확인). 증분 커서가 없어 매번 id 집합을 비교한다. upstream 접근은 `scripts/bank-config.py` 의
  resolve_target/upstream(docker exec 경유, HC_TARGET=next 사본 가드, API 키는 stdin)을 그대로 쓴다.
별칭 표: 대상 뱅크 정책의 item_repo_aliases(`--policies` 추적 파일 + 같은 디렉터리 local/ 오버레이, gateway.bank_policies 로
  병합). `--aliases-file` 은 실험용 대체 표다. 어느 쪽이든 meta aliases_sha256 에 표 sha256 을 쓴다.
LLM: `claude -p --model claude-haiku-5-5 --output-format json --safe-mode --tools "" --strict-mcp-config
  --no-session-persistence`. 토큰은 `~/.claude/demoproj02-token.sh` 출력을 자식 env CLAUDE_CODE_OAUTH_TOKEN 에만 넣는다(값은 출력·
  기록하지 않는다). 프롬프트는 #372 ROLE_PROMPT 와 같은 문안·같은 입력(언급 문장 + 키, 40개 묶음)이고 sha256 과 모델 id 를
  meta 에 쓴다. 빠진 항목만 1회 재시도하고 그래도 없으면 ref. 호출 오류·시간 초과는 따로 1회 재시도하고, 그 재시도까지
  오류면 그 묶음에 언급이 있는 기억 전부를 미판정으로 둔다. upstream 오류도 미판정(저장소를 바꾸지 않고 exit 1)이다.
지출 상한 `--max-usd`: 매 호출 전에 이번 실행 누계 + 호출당 예상(직전 호출 평균, 기록이 없으면 DEFAULT_CALL_USD)이
  상한을 넘으면 멈춘다(exit 3).
저장소 meta 의 model·prompt_sha256·aliases_sha256 이 지금 값과 다르면 거부한다(exit 1, 섞인 판정 방지 - 파일을 지우고 backfill).
판정 옵션(#383, 기본값 = 위 #376 판정): `--model`(판정 모델), `--input sentence|memory`(memory = 기억 전문 + 기억 날짜(KST) +
  맨 앞 라벨 + 그 기억의 키 목록을 기억 단위로 보내고 (기억, 키)마다 1항목, 기억은 묶음 사이에 나누지 않는다),
  `--votes N`(묶음을 N번 따로 판정해 항목마다 엄격한 과반, 없으면 ref, 한 표라도 실패하면 묶음 실패), `--prompt-file`
  (기본이 아닌 프롬프트, sha256 을 meta 에). meta 선택 키 judge_mode(`input=<방식>;votes=<N>`, 없으면 문장·1표)가 다르면 거부한다.
오프라인 나열(#383): `--offline-bodies <본문 JSONL> --offline-meta <기억 메타 JSONL> [--mentioned-until YYYY-MM-DD]` 를 주면
  upstream 을 읽지 않고 두 파일을 id 로 합쳐 나열을 만든다(별칭 표는 `--aliases-file` 만). `--spend-log` = 호출 기록·비용 합
  JSON(0600, 호출마다 응답 modelUsage 의 실제 모델 id).
표준 출력에는 수치만 낸다(본문·언급 문장·키 목록·LLM 원문 없음).
exit: 0 = 완료, 1 = 거부·upstream 오류, 3 = 지출 상한으로 멈춤 또는 미판정 기억이 남음.

키 규칙 v2·GitHub 근거·재키잉 (issue #395, R7-1). 기본(인자 없음)은 전부 위와 같다(v1 키, 수집 꺼짐, 판정 프롬프트 그대로).
  `--rules v2`: `gateway/item_keys.py` v2 키(범위 전개, PR 표기 제외, 업스트림 문맥 참조 고정)로 언급을 찾고 meta 선택 키
    key_rules=v2 를 쓴다. 참조 고정 언급은 LLM 에 보내지 않고 ref 다. plan 줄에 참조 고정 언급 수·의심 키 수(리포별 관측
    최대 번호를 넘는 번호만 키, 지우지 않음)·상한 표 sha256 을 더 낸다. 저장소 key_rules(없으면 v1)가 지금 규칙과 다르면
    거부한다(poll 은 v1 저장소를 v2 로 이어 쓰지 않는다 - rekey 로 새 파일을 만든다).
  `--github`: 실행 끝(공개 전)에 저장소 키의 리포마다 `gh api --method GET --paginate --slurp` 로
    `repos/<owner>/<repo>/issues?state=all`(PR 포함)과 `repos/<owner>/<repo>/pulls?state=closed`(머지 시각)를 받아 GitHub
    근거 테이블(`gateway/item_roles.py`)을 같은 공개에 싣는다. 인증은 gh 가 알아서 한다(이 스크립트는 토큰을 읽지 않는다).
    404 = missing, 그 밖 실패(인증·한도·네트워크·깨진 JSON, 소유자 없음) = error 로 두고 종결로 치지 않는다(exit 0 유지,
    옛 item 행은 남기되 리포 상태가 error 라 근거는 미확인). `--github-min-interval`(기본 3600초) 안에 ok·missing 으로 받은
    리포는 다시 받지 않는다. 출력은 리포 수·상태별 수·gh 호출 수·쪽 수뿐이다(리포 이름·gh 오류 원문 없음, 오류는 코드만).
    `--github` 가 없어도 기존 근거 테이블은 그대로 옮겨 싣는다(근거가 조용히 사라지지 않게).
  `rekey`: 원본을 고치지 않고 `--out`(없는 경로)에 새 저장소를 공개한다. 원본 memories 행에 있는 기억만 다루고
    state·mentioned_at·document_id 는 원본 행 그대로다(나열은 본문·태그를 얻는 데만, 나열에 없는 기억은 뺀다).
    키마다: 문장 언급이 전부 참조 고정 -> ref / 고정과 보통이 섞임 -> 보통 언급만 다시 판정 / 본문 sha256 이 원본과 다름 ->
    보통 언급 다시 판정 / 원본에 있던 키 -> 원본 역할 그대로 / 새로 생긴 키 -> 그 언급만 판정. 사라진 키는 뺀다.
    `--save-listing` = 나열을 `--offline-bodies`·`--offline-meta` 꼴 JSONL 두 파일(0600)로 저장.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(_HERE)
for _p in (REPO, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from gateway import item_roles as IR  # noqa: E402  (표준 라이브러리만)
from gateway import item_keys as IK  # noqa: E402  (표준 라이브러리만)
from gateway.bank_policies import BankPolicies, BankPolicyError  # noqa: E402

FACT_TYPES = ("world", "experience")
ROLE_MODEL = "claude-haiku-5-5"
ROLE_BATCH = 40
PAGE_SIZE = 500
CALL_TIMEOUT = 300
DEFAULT_CALL_USD = 0.02          # 기록이 없을 때 호출당 예상(#372 실측 40항목 묶음 약 0.0036달러보다 크게)
PUBLISH_EVERY = 20               # 이 묶음 수마다 중간 공개(멈춰도 거기까지 남는다)
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
TOKEN_CMD = "~/.claude/demoproj02-token.sh"
CLAUDE_ARGS = ("-p", "--model", ROLE_MODEL, "--output-format", "json", "--safe-mode", "--tools", "",
               "--strict-mcp-config", "--no-session-persistence")
# 판정 입력 방식(#383): sentence = 언급 문장 + 키(#372·#376), memory = 기억 전문 + 기억 날짜(KST) + 맨 앞 라벨 + 키 목록.
INPUT_SENTENCE, INPUT_MEMORY = "sentence", "memory"
INPUTS = (INPUT_SENTENCE, INPUT_MEMORY)
DEFAULT_INPUT = INPUT_SENTENCE
DEFAULT_VOTES = 1
MEMORY_BATCH_CHARS = 20000       # memory 입력 묶음의 기억 본문 글자 상한(기억 하나는 나누지 않는다)

# #372 bench/r5_roles.py ROLE_PROMPT 와 같은 문안(tests/test_item_roles_sidecar.py 가 bench 상수와 대조한다).
ROLE_PROMPT = """너는 작업 기록 문장에서 작업 키 언급의 역할을 분류한다.
각 항목은 기록에서 뽑은 문장 하나와 그 문장에 나온 작업 키 하나다. 그 문장이 그 키의 일을 어떻게 말하는지 하나만 고른다.
- done: 그 키의 일이 끝났다고 말한다(완료, 머지, 닫음, 해결, 배포 등).
- open: 그 키의 일이 아직 남았다고 말한다(남은 일, 다음 할 일, 진행 중, 대기, 보류, 미완, 후속 등).
- ref: 끝남도 남음도 말하지 않는 참조다(근거, 관련, 출처, 비교 대상, 단순 언급).
문장 맨 앞의 [괄호 라벨]은 그 기록 전체의 종류다.
설명 없이 JSON 배열 하나만 출력한다. 모든 항목에 하나씩: [{"i": <번호>, "role": "done" | "open" | "ref"}]

항목:
"""
PROMPT_SHA256 = hashlib.sha256(ROLE_PROMPT.encode()).hexdigest()
# 입력 방식별 기본 프롬프트. memory 입력의 프롬프트는 기본값이 아니면 리포 밖 파일(--prompt-file)로만 받는다.
DEFAULT_PROMPTS: Dict[str, str] = {}
GH_TIMEOUT = 120
GH_PER_PAGE = 100
DEFAULT_GH_MIN_INTERVAL = 3600


class Fail(Exception):
    """거부나 upstream 오류: stderr 에 한 줄, exit 1."""


def emit(obj: Mapping[str, Any]) -> None:
    print(json.dumps(obj, ensure_ascii=False, sort_keys=True))
    sys.stdout.flush()


class JudgeConfig:
    """판정 설정(#383): 모델, 입력 방식, 투표 수, 프롬프트. 인자를 비우면 모듈 기본값(부를 때 읽는다)이다.

    meta 에는 model·prompt_sha256 과 선택 키 judge_mode(입력 방식·투표 수)를 쓰고, poll 은 셋 중 하나라도 다르면 거부한다."""

    def __init__(self, model: Optional[str] = None, input_mode: Optional[str] = None, votes: Optional[int] = None,
                 prompt: Optional[str] = None):
        self.model = model or ROLE_MODEL
        self.input_mode = input_mode or DEFAULT_INPUT
        self.votes = DEFAULT_VOTES if votes is None else int(votes)
        if self.input_mode not in INPUTS:
            raise Fail("unknown judge input %r" % self.input_mode)
        if self.votes < 1:                       # 0 을 조용히 기본값으로 바꾸지 않는다(final-review #383-1)
            raise ValueError("votes must be 1 or more")
        if prompt is not None:
            self.prompt, self.prompt_sha256 = prompt, hashlib.sha256(prompt.encode()).hexdigest()
        elif self.input_mode == INPUT_SENTENCE:
            self.prompt, self.prompt_sha256 = ROLE_PROMPT, PROMPT_SHA256
        elif self.input_mode in DEFAULT_PROMPTS:
            self.prompt = DEFAULT_PROMPTS[self.input_mode]
            self.prompt_sha256 = hashlib.sha256(self.prompt.encode()).hexdigest()
        else:
            raise Fail("judge input %s has no default prompt - pass --prompt-file" % self.input_mode)

    @property
    def judge_mode(self) -> str:
        return IR.judge_mode(self.input_mode, self.votes)

    def claude_args(self) -> Tuple[str, ...]:
        args = list(CLAUDE_ARGS)
        args[args.index("--model") + 1] = self.model
        return tuple(args)

    def build_prompt(self, batch: Sequence[Mapping[str, Any]]) -> str:
        if self.input_mode == INPUT_SENTENCE:
            lines = [json.dumps({"i": it["i"], "key": it["key"], "sentence": it["sentence"]}, ensure_ascii=False)
                     for it in batch]
            return self.prompt + "\n".join(lines)
        blocks: Dict[str, Dict[str, Any]] = {}
        for it in batch:                 # 기억 순(묶음 안 첫 등장 순), 기억마다 키 항목
            b = blocks.setdefault(it["mid"], {"date": it["date"], "label": it["label"], "text": it["text"], "keys": []})
            b["keys"].append({"i": it["i"], "key": it["key"]})
        return self.prompt + "\n".join(json.dumps(b, ensure_ascii=False) for b in blocks.values())


# --- upstream -------------------------------------------------------------------------------------------

def _bank_config():
    spec = importlib.util.spec_from_file_location("bank_config", os.path.join(_HERE, "bank-config.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def upstream_getter(allow_prod: bool) -> Callable[[str], Dict[str, Any]]:
    """resolve_target(사본 가드 포함) 뒤 GET 만 하는 호출기. 사본이 아니면 --allow-prod 가 있어야 한다."""
    bc = _bank_config()
    try:
        container, is_copy, key = bc.resolve_target()
    except bc.Fail as e:
        raise Fail(str(e))
    if not is_copy and not allow_prod:
        raise Fail("target is not the hc-next copy (HC_TARGET=next); pass --allow-prod to read it")

    def get(path: str) -> Dict[str, Any]:
        try:
            return bc.upstream(container, key, "GET", path)
        except bc.Fail as e:
            raise Fail(str(e))
    return get


def list_memories(get: Callable[[str], Dict[str, Any]], bank: str, fact_type: str,
                  page_size: int = PAGE_SIZE) -> List[Dict[str, Any]]:
    """memories/list 전 쪽(GET 만). id 수가 total 과 맞아야 한다(bench/r5_labels.list_memories 와 같은 방식)."""
    seen: Dict[str, Dict[str, Any]] = {}
    offset, total = 0, None
    while True:
        q = urllib.parse.urlencode([("type", fact_type), ("limit", page_size), ("offset", offset)])
        page = get("/v1/default/banks/%s/memories/list?%s" % (urllib.parse.quote(bank, safe=""), q))
        if not isinstance(page, dict) or not isinstance(page.get("total"), int):
            raise Fail("memories/list answered without an integer total")
        total = page["total"] if total is None else total
        if page["total"] != total:
            raise Fail("memories/list total moved while paging")
        items = page.get("items") or []
        for it in items:
            seen[str(it["id"])] = it
        offset += len(items)
        if not items or offset >= total:
            break
    if len(seen) != total:
        raise Fail("memories/list ids do not add up to the total")
    return list(seen.values())


def list_all(get: Callable[[str], Dict[str, Any]], bank: str) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for ft in FACT_TYPES:
        for mem in list_memories(get, bank, ft):
            out[str(mem["id"])] = mem
    return out


def memory_text(mem: Mapping[str, Any]) -> str:
    text = mem.get("text") if isinstance(mem.get("text"), str) else mem.get("content")
    return text if isinstance(text, str) else ""


def write_private_json(path: str, obj: Any) -> None:
    """리포 밖 기록 파일(0600): 같은 디렉터리 임시 파일에 쓰고 os.replace."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".tmp-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, sort_keys=True, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_jsonl(path: str, what: str) -> List[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
    except (OSError, ValueError) as e:
        raise Fail("cannot read %s: %s" % (what, e))
    if not all(isinstance(r, dict) and r.get("id") for r in rows):
        raise Fail("%s rows must be objects with an id" % what)
    return rows


def offline_listing(bodies_path: str, meta_path: str, until: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """오프라인 나열(#383): 본문 JSONL(id, text) + 기억 메타 JSONL(id, tags, state, document_id, mentioned_at,
    fact_type)을 id 로 합친 upstream 나열 모양. until(YYYY-MM-DD) = 이 KST 날짜 끝 이후 mentioned_at 기억은 뺀다.
    본문에 있는데 메타에 없는 기억은 거부한다(state·태그 없이 판정하지 않는다)."""
    if until is not None:                        # 문자열 비교라 형식이 다르면 봉인 구간이 그대로 들어온다
        try:
            ok = len(until) == 10 and datetime.strptime(until, "%Y-%m-%d").date().isoformat() == until
        except (TypeError, ValueError):
            ok = False
        if not ok:
            raise Fail("--mentioned-until must be YYYY-MM-DD")
    meta = {str(r["id"]): r for r in _read_jsonl(meta_path, "--offline-meta")}
    out: Dict[str, Dict[str, Any]] = {}
    for b in _read_jsonl(bodies_path, "--offline-bodies"):
        mid = str(b["id"])
        m = meta.get(mid)
        if m is None:
            raise Fail("a body has no row in --offline-meta")
        if until is not None and (kst_date(m.get("mentioned_at")) or "9999-12-31") > until:
            continue
        out[mid] = {"id": mid, "text": b.get("text") if isinstance(b.get("text"), str) else "",
                    "tags": m.get("tags") or [], "state": m.get("state"), "document_id": m.get("document_id"),
                    "mentioned_at": m.get("mentioned_at"), "type": m.get("fact_type")}
    return out


def memory_tags(mem: Mapping[str, Any]) -> List[str]:
    tags = mem.get("tags")
    if isinstance(tags, (str, bytes)) or not isinstance(tags, (list, tuple)):
        return []
    return [t for t in tags if isinstance(t, str)]


# --- 별칭 표 ---------------------------------------------------------------------------------------------

def policy_aliases(policies_path: str, bank: str) -> Dict[str, str]:
    try:
        pol = BankPolicies.from_files(policies_path)
    except (BankPolicyError, OSError, ValueError) as e:
        raise Fail("cannot load bank policies %s: %s" % (policies_path, e))
    if bank not in pol.bank_names():
        raise Fail("bank policies have no entry for the target bank")
    return IR.normalize_aliases(pol.for_bank(bank).item_repo_aliases)


def file_aliases(path: str) -> Dict[str, str]:
    try:
        with open(path, encoding="utf-8") as fh:
            value = json.load(fh)
    except (OSError, ValueError) as e:
        raise Fail("cannot read --aliases-file: %s" % e)
    if isinstance(value, dict) and isinstance(value.get("item_repo_aliases"), dict):
        value = value["item_repo_aliases"]
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise Fail("--aliases-file must be a JSON object {alias: repo}")
    return IR.normalize_aliases(value)


# --- LLM -------------------------------------------------------------------------------------------------

def claude_caller(timeout: int = CALL_TIMEOUT, args: Sequence[str] = CLAUDE_ARGS) -> Callable[[str], Dict[str, Any]]:
    """중첩 claude -p 호출기(bench/d_llm.py claude_caller 와 같은 방식). 토큰은 자식 env 에만 넣고 어디에도 쓰지 않는다."""
    try:
        token = subprocess.run([os.path.expanduser(TOKEN_CMD)], capture_output=True, text=True, check=True,
                               timeout=60).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise Fail("token command failed (output not shown)") from None
    if not token:
        raise Fail("token command printed nothing")
    env = dict(os.environ)
    env[TOKEN_ENV] = token
    cmd = ["claude", *args]

    def call(prompt: str) -> Dict[str, Any]:
        with tempfile.TemporaryDirectory() as cwd:
            try:
                p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, env=env, cwd=cwd,
                                   timeout=timeout)
            except subprocess.TimeoutExpired:
                return {"is_error": True, "result": "", "error": "timeout %ds" % timeout}
            except OSError as e:
                return {"is_error": True, "result": "", "error": "spawn failed: %s" % e.strerror}
        try:
            out = json.loads(p.stdout)
        except ValueError:
            return {"is_error": True, "result": "", "error": "exit=%d non-json stdout" % p.returncode}
        return out if isinstance(out, dict) else {"is_error": True, "result": "", "error": "non-object json"}
    return call


def role_prompt(batch: Sequence[Mapping[str, Any]]) -> str:
    lines = [json.dumps({"i": it["i"], "key": it["key"], "sentence": it["sentence"]}, ensure_ascii=False) for it in batch]
    return ROLE_PROMPT + "\n".join(lines)


def parse_roles(text: str, want) -> Dict[int, str]:
    """출력에서 첫 JSON 배열을 읽어 {i: role}. 모양이 아닌 항목과 요청 밖 번호는 버린다(#372 와 같음)."""
    w = set(want)
    s, e = (text or "").find("["), (text or "").rfind("]")
    if s < 0 or e <= s:
        return {}
    try:
        arr = json.loads(text[s:e + 1])
    except ValueError:
        return {}
    out = {}
    for x in arr if isinstance(arr, list) else []:
        if isinstance(x, dict) and isinstance(x.get("i"), int) and x.get("role") in IR.ROLES and x["i"] in w:
            out[x["i"]] = x["role"]
    return out


class Spend:
    """이번 실행의 지출 누계와 호출 전 확인(누계 + 호출당 예상 > 상한이면 멈춘다).

    호출당 예상 = 응답한(오류가 아닌) 호출의 평균. 오류·시간 초과 호출은 비용이 0으로 기록돼 평균을 낮추므로 빼고,
    응답한 호출이 없으면 보수적 기본값을 쓴다(final-review #376 Bugs #10)."""

    def __init__(self, max_usd: Optional[float], default_call: float = DEFAULT_CALL_USD):
        self.max_usd = max_usd
        self.default_call = default_call
        self.spent = 0.0
        self.calls = 0
        self.ok_spent = 0.0
        self.ok_calls = 0

    def estimate(self) -> float:
        return (self.ok_spent / self.ok_calls) if self.ok_calls else self.default_call

    def allows_next(self) -> bool:
        return self.max_usd is None or self.spent + self.estimate() <= self.max_usd

    def add(self, cost: float, ok: bool = True) -> None:
        c = float(cost or 0.0)
        self.spent += c
        self.calls += 1
        if ok:
            self.ok_spent += c
            self.ok_calls += 1


def judge_batch(batch: Sequence[Mapping[str, Any]], caller, spend: Spend,
                prompt_of: Callable[[Sequence[Mapping[str, Any]]], str] = role_prompt
                ) -> Tuple[Optional[Dict[int, str]], List[Dict], bool]:
    """묶음 하나 -> (역할 | None=실패, 호출 기록, 상한 멈춤). 재시도 몫은 둘로 따로 센다.

    오류·시간 초과 재시도 1회: 오류면 같은 항목을 한 번 더 부른다. 그 재시도도 오류면 실패(None, 묶음 기억 미판정).
    빠진 항목 재시도 1회: 응답했는데 빠진 항목만 한 번 더 부른다. 그래도 없으면 ref.
    그래서 오류 뒤 부분 응답이면 빠진 항목은 자기 재시도를 받고, 부분 응답 뒤 재시도가 오류면 오류 재시도를 받는다(최대 3호출)."""
    roles: Dict[int, str] = {}
    calls: List[Dict[str, Any]] = []
    todo = list(batch)
    error_retry, missing_retry = 1, 1
    attempt = 0
    while todo:
        if not spend.allows_next():
            return None, calls, True
        attempt += 1
        try:
            raw = caller(prompt_of(todo))
        except Exception as e:           # 예상 밖 예외도 이 묶음의 오류다(메시지·env 는 토큰을 담을 수 있어 타입 이름만)
            raw = {"is_error": True, "result": "", "error": "exception %s" % type(e).__name__}
        if not isinstance(raw, dict):
            raw = {"is_error": True, "result": "", "error": "non-object reply"}
        cost = raw.get("total_cost_usd") or 0.0
        is_error = bool(raw.get("is_error"))
        spend.add(cost, ok=not is_error)
        got = {} if is_error else parse_roles(raw.get("result") or "", [it["i"] for it in todo])
        roles.update(got)
        u = raw.get("usage") or {}
        calls.append({"attempt": attempt, "items": len(todo), "parsed": len(got), "is_error": is_error,
                      "cost_usd": cost, "duration_ms": raw.get("duration_ms"),
                      "input_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens"),
                      "models": sorted(raw.get("modelUsage") or {}) if isinstance(raw.get("modelUsage"), dict) else [],
                      "error": (str(raw.get("error"))[:80] if is_error and raw.get("error") else None)})
        todo = [it for it in todo if it["i"] not in got]
        if is_error:
            if not error_retry:
                return None, calls, False
            error_retry -= 1
        elif todo:
            if not missing_retry:
                break
            missing_retry -= 1
    for it in todo:
        roles[it["i"]] = IR.REF
    return roles, calls, False


def judge_votes(batch: Sequence[Mapping[str, Any]], caller, spend: Spend, cfg: "JudgeConfig"
                ) -> Tuple[Optional[Dict[int, str]], List[Dict], bool]:
    """묶음을 cfg.votes 번 따로 판정해 항목마다 다수결(`IR.majority_role`, 과반 없으면 ref). 한 표라도 실패하면 묶음 실패."""
    votes: List[Dict[int, str]] = []
    calls: List[Dict[str, Any]] = []
    for v in range(cfg.votes):
        res, cs, hit_cap = judge_batch(batch, caller, spend, cfg.build_prompt)
        for c in cs:
            c["vote"] = v + 1
        calls += cs
        if hit_cap:
            return None, calls, True
        if res is None:
            return None, calls, False
        votes.append(res)
    if len(votes) == 1:
        return votes[0], calls, False
    return {it["i"]: IR.majority_role([r[it["i"]] for r in votes]) for it in batch}, calls, False


# --- 계획·실행 -------------------------------------------------------------------------------------------

def meta_values(aliases: Mapping[str, str], cfg: Optional["JudgeConfig"] = None,
                rules: str = IR.KEY_RULES_V1) -> Dict[str, str]:
    cfg = cfg or JudgeConfig()
    out = {"model": cfg.model, "prompt_sha256": cfg.prompt_sha256, "aliases_sha256": IR.aliases_sha256(aliases),
           "judge_mode": cfg.judge_mode}
    if rules != IR.KEY_RULES_V1:                 # v1 저장소 meta 는 지금과 같다(key_rules 없음 = v1)
        out[IR.META_KEY_RULES] = rules
    return out


def check_bank(stored: Mapping[str, str], bank: Optional[str]) -> None:
    """meta 선택 키 `bank` 가 있고 지금 대상 뱅크와 다르면 거부(다른 뱅크 나열로 행을 지우지 않게). 없으면(옛 저장소) 통과."""
    if bank and stored.get("bank") and stored["bank"] != bank:
        raise Fail("side store belongs to another bank (meta bank differs) - use its own --db")


def check_meta(stored: Mapping[str, str], want: Mapping[str, str]) -> None:
    diff = [k for k in ("model", "prompt_sha256", "aliases_sha256") if stored.get(k) != want[k]]
    if "judge_mode" in want and stored.get("judge_mode", IR.JUDGE_MODE_LEGACY) != want["judge_mode"]:
        diff.append("judge_mode")       # 키가 없는 옛 저장소는 #376 판정(문장·1회)이다
    if IR.key_rules_of(stored) != IR.key_rules_of(want):
        diff.append("key_rules")        # #395: 키 규칙이 다른 저장소는 rekey 로 새 파일을 만든다
    if diff == ["key_rules"]:           # 키 규칙만 다르면 역할을 살려 새 파일로 옮긴다(final-review #6)
        raise Fail("side store was judged with another key_rules - run rekey --rules %s --out <new path> "
                   "(the source is kept)" % IR.key_rules_of(want))
    if diff:
        raise Fail("side store was judged with another %s - delete it and run backfill" % "/".join(diff))


def row_of(mem: Mapping[str, Any], text_sha: str, judged_at: str) -> Dict[str, Any]:
    return {"document_id": mem.get("document_id"), "mentioned_at": mem.get("mentioned_at"),
            "state": str(mem.get("state") or "unknown"), "text_sha256": text_sha, "judged_at": judged_at}


def plan(listing: Mapping[str, Mapping[str, Any]], aliases: Mapping[str, str],
         mems: Mapping[str, Mapping[str, Any]], roles: Mapping[str, Mapping[str, str]],
         rules: str = IR.KEY_RULES_V1) -> Dict[str, Any]:
    """지금 나열과 저장소 -> 할 일. judge = LLM 판정 대상 {mid: parsed}, tag_only = 문장 언급 없이 item 태그 키만,
    refresh = 메타만 바뀐 기억, keep = 그대로, drop = 지울 기억(사라졌거나 키가 없어짐)."""
    out = {"judge": {}, "tag_only": {}, "refresh": [], "keep": [], "drop": [], "keyed": 0, "texts": {}}
    for mid in sorted(listing):
        mem = listing[mid]
        text = memory_text(mem)
        parsed = IR.memory_mentions(mid, text, memory_tags(mem), aliases, rules=rules)
        if not parsed["keys"]:
            if mid in mems:
                out["drop"].append(mid)
            continue
        out["keyed"] += 1
        sha = IR.sha256_text(text)
        out["texts"][mid] = sha
        old = mems.get(mid)
        if old is not None and old["text_sha256"] == sha and set((roles.get(mid) or {})) == set(parsed["keys"]):
            new = row_of(mem, sha, old["judged_at"])
            same = all(old.get(k) == new[k] for k in ("document_id", "mentioned_at", "state"))
            out["keep" if same else "refresh"].append(mid)
            continue
        if IR.judge_mentions(parsed):        # 문장 있고 참조 고정이 아닌 언급이 있어야 LLM 판정 대상이다
            out["judge"][mid] = parsed
        else:
            out["tag_only"][mid] = parsed
    out["drop"] += sorted(set(mems) - set(listing))
    return out


def kst_date(value: Any) -> Optional[str]:
    """mentioned_at -> KST 날짜(YYYY-MM-DD). 읽을 수 없으면 None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at.astimezone(timezone(timedelta(hours=9))).date().isoformat()


def memory_items(judge: Mapping[str, Mapping[str, Any]], listing: Mapping[str, Mapping[str, Any]]
                 ) -> List[Dict[str, Any]]:
    """memory 입력의 판정 대상(#383): 문장 언급이 있는 (기억, 키)마다 1항목(키는 첫 언급 순). i = 1 부터.
    항목에 기억 전문·KST 날짜·맨 앞 라벨을 싣는다. item 태그에서만 나온 키는 항목이 아니다(ref)."""
    items = []
    for mid in sorted(judge):
        p = judge[mid]
        mem = listing[mid]
        seen: List[str] = []
        for m in p["mentions"]:
            if m.get("sent") is not None and not m.get("fixed") and m["key"] not in seen:
                seen.append(m["key"])
        for key in seen:
            items.append({"mid": mid, "key": key, "sent": None, "text": memory_text(mem),
                          "date": kst_date(mem.get("mentioned_at")), "label": p.get("label")})
    for i, it in enumerate(items, 1):
        it["i"] = i
    return items


def batches_of(items: Sequence[Dict[str, Any]], cfg: "JudgeConfig", batch: int) -> List[List[Dict[str, Any]]]:
    """sentence 입력은 batch 항목씩 자른다(#376 과 같음). memory 입력은 기억을 나누지 않고 항목 수 batch·본문
    MEMORY_BATCH_CHARS 를 넘기 전까지 기억을 모은다(기억 하나가 넘으면 그 기억만으로 1묶음)."""
    if cfg.input_mode == INPUT_SENTENCE:
        return [list(items[b:b + batch]) for b in range(0, len(items), batch)]
    out: List[List[Dict[str, Any]]] = []
    cur: List[Dict[str, Any]] = []
    chars = 0
    for mid in dict.fromkeys(it["mid"] for it in items):
        group = [it for it in items if it["mid"] == mid]
        size = len(group[0]["text"])
        if cur and (len(cur) + len(group) > batch or chars + size > MEMORY_BATCH_CHARS):
            out.append(cur)
            cur, chars = [], 0
        cur += group
        chars += size
    if cur:
        out.append(cur)
    return out


def items_of(judge: Mapping[str, Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """LLM 판정 대상 언급(문장 있는 언급만, #372 role_items 와 같은 순서). i = 1 부터."""
    items = []
    for mid in sorted(judge):
        p = judge[mid]
        for m in p["mentions"]:
            if m.get("sent") is None or m.get("fixed"):     # 참조 고정(#395 v2)은 판정하지 않는다(ref)
                continue
            items.append({"mid": mid, "key": m["key"], "sent": m["sent"], "sentence": p["sentences"][m["sent"]]})
    for i, it in enumerate(items, 1):
        it["i"] = i
    return items


def run_sync(mode: str, db: str, listing: Mapping[str, Mapping[str, Any]], aliases: Mapping[str, str],
             caller_factory: Callable[[], Callable[[str], Dict[str, Any]]], max_usd: Optional[float],
             batch: int = ROLE_BATCH, publish_every: int = PUBLISH_EVERY, file_mode: Optional[int] = None,
             now: Callable[[], str] = IR.utc_now, bank: Optional[str] = None, cfg: Optional[JudgeConfig] = None,
             spend_log: Optional[str] = None, rules: str = IR.KEY_RULES_V1,
             github: Optional["GithubConfig"] = None) -> int:
    """backfill/poll 본체(나열은 이미 끝난 상태로 받는다: upstream 또는 오프라인 파일). 저장소 공개는 IR.publish.
    cfg = 판정 설정(없으면 기본값), spend_log = 호출 기록·비용 합 JSON(0600, 공개마다·끝에 다시 쓴다).
    rules = 키 규칙(#395, 기본 v1), github = GitHub 근거 수집 설정(None 이면 수집 0, 기존 근거 테이블은 그대로 옮긴다)."""
    cfg = cfg or JudgeConfig()
    want = meta_values(aliases, cfg, rules)
    if file_mode is None:            # 기존 파일 권한을 넓히지 않는다(0600 사본을 poll 이 0644 로 바꾸지 않게)
        file_mode = (os.stat(db).st_mode & 0o777) if os.path.exists(db) else 0o644
    if os.path.exists(db):
        if mode == "backfill":
            raise Fail("backfill builds a new side store but %s exists - use poll to continue" % os.path.basename(db))
        try:
            stored_meta, mems, roles, gh_tables = IR.load_store(db, with_github=True)
        except IR.StoreError as e:
            raise Fail(str(e))
        check_bank(stored_meta, bank)
        check_meta(stored_meta, want)
        fill_bank = bool(bank and not stored_meta.get("bank"))     # 옛 저장소: 이번 공개 때 meta bank 를 채운다
    else:
        mems, roles, gh_tables = {}, {}, None
        fill_bank = False
    mems = {k: dict(v) for k, v in mems.items()}
    roles = {k: dict(v) for k, v in roles.items()}
    p = plan(listing, aliases, mems, roles, rules=rules)
    stamp = now()
    for mid in p["drop"]:
        mems.pop(mid, None)
        roles.pop(mid, None)
    for mid in p["refresh"]:
        mems[mid] = row_of(listing[mid], mems[mid]["text_sha256"], mems[mid]["judged_at"])
    for mid, parsed in p["tag_only"].items():
        mems[mid] = row_of(listing[mid], p["texts"][mid], stamp)
        roles[mid] = {k: IR.REF for k in parsed["keys"]}
    # 다시 판정할 기억의 옛 행(본문·키가 바뀌기 전 판정)은 먼저 뺀다. 판정이 실패하거나 상한으로 멈추면 그 기억은
    # 행이 없는 미판정으로 공개된다(옛 역할·state 가 판정된 값처럼 남지 않게, final-review #376 C11).
    stale = sorted(mid for mid in p["judge"] if mid in mems)
    for mid in stale:
        mems.pop(mid, None)
        roles.pop(mid, None)
    items = items_of(p["judge"]) if cfg.input_mode == INPUT_SENTENCE else memory_items(p["judge"], listing)
    chunks = batches_of(items, cfg, batch)
    plan_line = {"phase": "plan", "mode": mode, "model": cfg.model, "judge_mode": cfg.judge_mode, "listed": len(listing), "keyed": p["keyed"], "judge_memories": len(p["judge"]),
                 "judge_mentions": len(items), "item_tag_only": len(p["tag_only"]), "meta_refresh": len(p["refresh"]),
                 "unchanged": len(p["keep"]), "drop": len(p["drop"]), "batches": len(chunks)}
    if rules != IR.KEY_RULES_V1:
        plan_line.update(v2_counts(listing, aliases, rules))
    emit(plan_line)
    changed = bool(p["drop"] or p["refresh"] or p["tag_only"] or stale or fill_bank or not os.path.exists(db))
    meta = dict(want)
    if bank:
        meta["bank"] = bank              # 계약 v1 의 선택 키(필수 키·스키마는 그대로)

    def do_publish() -> str:
        meta["updated_at"] = now()
        return IR.publish(db, meta, mems, roles, mode=file_mode, github=gh_tables)

    by_mid: Dict[str, List[Dict[str, Any]]] = {}
    for it in items:
        by_mid.setdefault(it["mid"], []).append(it)
    got: Dict[int, str] = {}
    failed_mids: set = set()
    done_mids: set = set()
    spend = Spend(max_usd)
    stopped = False
    caller = caller_factory() if items else None
    n_batches = 0
    call_log: List[Dict[str, Any]] = []

    def write_spend() -> None:
        if spend_log:
            models = Counter(m for c in call_log for m in (c.get("models") or []))
            write_private_json(spend_log, {"cost_usd": round(spend.spent, 6), "calls": call_log,
                                           "models_used": dict(sorted(models.items())), "model": cfg.model,
                                           "judge_mode": cfg.judge_mode, "prompt_sha256": cfg.prompt_sha256,
                                           "judged_now": len(done_mids), "stopped": stopped})
    for bi, chunk in enumerate(chunks):
        res, calls, hit_cap = judge_votes(chunk, caller, spend, cfg)
        for c in calls:
            call_log.append(dict(c, batch=bi))
            emit({"phase": "call", "batch": bi, **c, "spent_usd": round(spend.spent, 6)})
        if hit_cap:
            stopped = True
            emit({"phase": "stop", "reason": "max-usd", "spent_usd": round(spend.spent, 6),
                  "estimate_usd": round(spend.estimate(), 6), "max_usd": max_usd})
            break
        if res is None:
            failed_mids |= {it["mid"] for it in chunk}
        else:
            got.update(res)
        n_batches += 1
        # 언급이 모두 판정된(실패 묶음에 걸리지 않은) 기억만 저장소에 넣는다.
        for mid in sorted({it["mid"] for it in chunk}):
            if mid in failed_mids or mid in done_mids:
                continue
            if all(x["i"] in got for x in by_mid[mid]):
                if cfg.input_mode == INPUT_SENTENCE:
                    idx = {(x["key"], x["sent"]): x["i"] for x in by_mid[mid]}
                    role_of = lambda m: IR.REF if m.get("sent") is None or m.get("fixed") else got[idx[(m["key"], m["sent"])]]  # noqa: E731
                else:                    # memory 입력: (기억, 키) 하나의 판정이 그 키의 모든 문장 언급 역할
                    kdx = {x["key"]: x["i"] for x in by_mid[mid]}
                    role_of = lambda m: IR.REF if m.get("sent") is None or m.get("fixed") else got[kdx[m["key"]]]  # noqa: E731
                mems[mid] = row_of(listing[mid], p["texts"][mid], now())
                roles[mid] = IR.memory_roles(p["judge"][mid]["mentions"], role_of)[mid]
                done_mids.add(mid)
                changed = True
        if publish_every and n_batches % publish_every == 0 and changed:
            sha = do_publish()
            write_spend()
            emit({"phase": "publish", "judged": len(done_mids), "sha256": sha})
    unjudged = len(p["judge"]) - len(done_mids)
    if github is not None:
        new_tables = collect_github(store_repos(roles), gh_tables, github, now())
        changed = changed or new_tables != gh_tables
        gh_tables = new_tables
    sha = do_publish() if changed else None
    write_spend()
    dist = Counter(r for ks in roles.values() for r in ks.values())
    emit({"phase": "done", "mode": mode, "judged_now": len(done_mids), "unjudged": unjudged,
          "failed_memories": len(failed_mids - done_mids), "stopped": stopped, "calls": spend.calls,
          "cost_usd": round(spend.spent, 6), "store_memories": len(mems),
          "models_used": dict(sorted(Counter(m for c in call_log for m in (c.get("models") or [])).items())),
          "store_mentions": sum(len(v) for v in roles.values()), "role_dist": dict(sorted(dist.items())),
          "published": sha is not None, "sha256": sha})
    return 3 if (stopped or unjudged) else 0


# --- 키 규칙 v2 집계·GitHub 근거 수집 (#395) -----------------------------------------------------------------

def v2_counts(listing: Mapping[str, Mapping[str, Any]], aliases: Mapping[str, str], rules: str) -> Dict[str, Any]:
    """plan 줄에 더할 수치: 참조 고정 언급 수, 의심 키 수(나열 전체의 리포별 관측 최대 번호 기준), 상한 표 sha256."""
    pairs = [(memory_text(m), memory_tags(m)) for _, m in sorted(listing.items())]
    table = IR.observed_max(pairs, aliases)
    mentions = [x for mid in sorted(listing)
                for x in IR.memory_mentions(mid, memory_text(listing[mid]), memory_tags(listing[mid]), aliases,
                                            rules=rules)["mentions"]]
    return {"key_rules": rules, "fixed_mentions": sum(1 for x in mentions if x.get("fixed")),
            "suspect_keys": len(IR.suspect_keys(mentions, table)), "observed_max_sha256": IR.observed_max_sha256(table)}


class GithubConfig:
    """GitHub 근거 수집 설정. runner = `gh` 인자 목록 -> (exit 코드, stdout, stderr), owners = {repo: owner}."""

    def __init__(self, runner: Callable[[Sequence[str]], Tuple[int, str, str]],
                 owners: Optional[Mapping[str, str]] = None, default_owner: Optional[str] = None,
                 min_interval: int = DEFAULT_GH_MIN_INTERVAL):
        self.runner = runner
        self.owners = {k.strip().lower(): v.strip() for k, v in (owners or {}).items()}
        self.default_owner = default_owner
        self.min_interval = int(min_interval)

    def owner_of(self, repo: str) -> Optional[str]:
        return self.owners.get(repo) or self.default_owner or None


def gh_runner(timeout: int = GH_TIMEOUT) -> Callable[[Sequence[str]], Tuple[int, str, str]]:
    """진짜 `gh` 호출기. 인증은 gh 가 스스로 한다(env 를 고치지도 넘기지도 않는다). stderr 는 오류 코드 판정에만 쓴다."""

    def run(args: Sequence[str]) -> Tuple[int, str, str]:
        try:
            p = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, "", ""
        except OSError:
            return 127, "", ""
        return p.returncode, p.stdout, p.stderr
    return run


_HTTP_RE = re.compile(r"\(HTTP (\d{3})\)")


def gh_error_code(rc: int, stderr: str) -> str:
    """gh 실패 -> 코드(`http-<상태>` 또는 `exit-<코드>`). 오류 원문은 어디에도 남기지 않는다."""
    m = _HTTP_RE.search(stderr or "")
    return "http-%s" % m.group(1) if m else "exit-%d" % rc


def gh_list(runner, path: str, stats: Dict[str, int]) -> Tuple[Optional[List[Any]], Optional[str]]:
    """`gh api --method GET --paginate --slurp <path>` -> (모든 쪽의 항목, None) 또는 (None, 오류 코드)."""
    stats["gh_calls"] += 1
    rc, out, err = runner(["api", "--method", "GET", "--paginate", "--slurp", path])
    if rc != 0:
        return None, gh_error_code(rc, err)
    try:
        pages = json.loads(out)
    except ValueError:
        return None, "bad-json"
    if not isinstance(pages, list) or not all(isinstance(pg, list) for pg in pages):
        return None, "bad-json"
    stats["gh_pages"] += len(pages)
    return [x for pg in pages for x in pg], None


def _gh_rows(repo: str, issues: Sequence[Any], pulls: Sequence[Any], fetched_at: str) -> Dict[Tuple[str, int], Dict]:
    merged = {x["number"]: x.get("merged_at") for x in pulls if isinstance(x, dict) and isinstance(x.get("number"), int)}
    rows: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for it in issues:
        if not isinstance(it, dict) or not isinstance(it.get("number"), int) or it["number"] < 1:
            continue
        if it.get("state") not in IR.GH_STATES:
            continue
        is_pr = "pull_request" in it
        merged_at = None
        if is_pr:
            pr_info = it.get("pull_request") if isinstance(it.get("pull_request"), dict) else {}
            merged_at = merged.get(it["number"]) or pr_info.get("merged_at")
        rows[(repo, it["number"])] = {
            "kind": "pr" if is_pr else "issue", "state": it["state"], "state_reason": it.get("state_reason"),
            "created_at": it.get("created_at"), "closed_at": it.get("closed_at"), "merged_at": merged_at,
            "fetched_at": fetched_at}
    return rows


def _age_seconds(then: Any, now_text: str) -> Optional[float]:
    try:
        a = datetime.fromisoformat(str(then).replace("Z", "+00:00"))
        b = datetime.fromisoformat(now_text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if a.tzinfo is None:
        a = a.replace(tzinfo=timezone.utc)
    if b.tzinfo is None:
        b = b.replace(tzinfo=timezone.utc)
    return (b - a).total_seconds()


def store_repos(roles: Mapping[str, Mapping[str, str]]) -> List[str]:
    """저장소 키의 리포 목록(정렬)."""
    return sorted({sk[0] for ks in roles.values() for sk in (IR.split_key(k) for k in ks) if sk})


def collect_github(repos: Sequence[str], old: Optional[Mapping[str, Mapping[Any, Mapping[str, Any]]]],
                   cfg: GithubConfig, now_text: str) -> Dict[str, Dict[Any, Dict[str, Any]]]:
    """리포마다 이슈(PR 포함)·닫힌 PR 을 받아 근거 테이블을 새로 만든다(old 는 고치지 않는다). 수치 한 줄을 낸다.

    ok = 그 리포 item 행을 새로 받은 것으로 바꾼다 / missing(404) = item 행을 지운다 / error = 옛 item 행은 두되 리포 상태가
    error 라 근거는 미확인이다. min_interval 안에 ok·missing 으로 받은 리포는 부르지 않는다."""
    tables = {"repos": {k: dict(v) for k, v in ((old or {}).get("repos") or {}).items()},
              "items": {k: dict(v) for k, v in ((old or {}).get("items") or {}).items()}}
    stats = {"gh_calls": 0, "gh_pages": 0}
    count = Counter()
    for repo in repos:
        prev = tables["repos"].get(repo)
        owner = cfg.owner_of(repo)
        # 소유자가 바뀌었으면 최근에 받았어도 다시 받는다(다른 리포의 근거를 재사용하지 않는다, final-review C13).
        if prev and prev.get("status") in ("ok", "missing") and prev.get("owner") == owner:
            age = _age_seconds(prev.get("fetched_at"), now_text)
            if age is not None and 0 <= age < cfg.min_interval:
                count["skipped_recent"] += 1
                continue
        if not owner:
            tables["repos"][repo] = {"owner": None, "status": "error", "fetched_at": now_text, "error_code": "no-owner"}
            count["error"] += 1
            continue
        base = "repos/%s/%s" % (urllib.parse.quote(owner, safe=""), urllib.parse.quote(repo, safe=""))
        q = "per_page=%d" % GH_PER_PAGE
        issues, code = gh_list(cfg.runner, "%s/issues?state=all&%s" % (base, q), stats)
        pulls: Optional[List[Any]] = None
        missing = issues is None and code == "http-404"         # 리포 없음은 이슈 목록의 404 로만 정한다
        if issues is not None:
            pulls, code = gh_list(cfg.runner, "%s/pulls?state=closed&%s" % (base, q), stats)
        count["fetched"] += 1
        if issues is not None and pulls is not None:
            for k in [k for k in tables["items"] if k[0] == repo]:
                del tables["items"][k]
            tables["items"].update(_gh_rows(repo, issues, pulls, now_text))
            tables["repos"][repo] = {"owner": owner, "status": "ok", "fetched_at": now_text, "error_code": None}
            count["ok"] += 1
        elif missing:
            for k in [k for k in tables["items"] if k[0] == repo]:
                del tables["items"][k]
            tables["repos"][repo] = {"owner": owner, "status": "missing", "fetched_at": now_text, "error_code": code}
            count["missing"] += 1
        else:
            tables["repos"][repo] = {"owner": owner, "status": "error", "fetched_at": now_text, "error_code": code}
            count["error"] += 1
    emit({"phase": "github", "repos": len(repos), "fetched": count["fetched"],
          "skipped_recent": count["skipped_recent"], "ok": count["ok"], "missing": count["missing"],
          "error": count["error"], "gh_calls": stats["gh_calls"], "gh_pages": stats["gh_pages"],
          "items": len(tables["items"])})
    return tables


# --- rekey (#395) ------------------------------------------------------------------------------------------

def save_listing(listing: Mapping[str, Mapping[str, Any]], bodies_path: str, meta_path: str) -> None:
    """나열을 `offline_listing` 이 읽는 두 JSONL(본문: id·text, 메타: id·tags·state·document_id·mentioned_at·fact_type)로
    저장한다(0600, 같은 디렉터리 임시 파일 뒤 os.replace)."""
    bodies, metas = [], []
    for mid in sorted(listing):
        m = listing[mid]
        bodies.append({"id": mid, "text": memory_text(m)})
        metas.append({"id": mid, "tags": memory_tags(m), "state": m.get("state"), "document_id": m.get("document_id"),
                      "mentioned_at": m.get("mentioned_at"), "fact_type": m.get("fact_type") or m.get("type")})
    for path, rows in ((bodies_path, bodies), (meta_path, metas)):
        d = os.path.dirname(os.path.abspath(path)) or "."
        fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".tmp-", dir=d)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for r in rows:
                    fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def rekey_plan(src_mems: Mapping[str, Mapping[str, Any]], src_roles: Mapping[str, Mapping[str, str]],
               listing: Mapping[str, Mapping[str, Any]], aliases: Mapping[str, str], rules: str) -> Dict[str, Any]:
    """원본 저장소 + 나열 -> 기억마다 바로 정해지는 역할(keep)과 다시 판정할 언급(judge). 규칙은 rekey 설명(모듈 머리)."""
    out: Dict[str, Any] = {"keep": {}, "judge": {}, "partial": {}, "texts": {}, "unlisted": 0, "no_keys": 0,
                           "text_changed": 0, "preserved_keys": 0, "fixed_ref_keys": 0, "judge_keys": 0,
                           "dropped_keys": 0}
    for mid in sorted(src_mems):
        mem = listing.get(mid)
        old_roles = src_roles.get(mid) or {}
        if mem is None:
            out["unlisted"] += 1
            continue
        text = memory_text(mem)
        parsed = IR.memory_mentions(mid, text, memory_tags(mem), aliases, rules=rules)
        if not parsed["keys"]:
            out["no_keys"] += 1
            out["dropped_keys"] += len(old_roles)
            continue
        sha = IR.sha256_text(text)
        out["texts"][mid] = sha
        changed = sha != src_mems[mid]["text_sha256"]
        out["text_changed"] += int(changed)
        roles: Dict[str, str] = {}
        to_judge: List[str] = []
        for key in parsed["keys"]:
            textual = [m for m in parsed["mentions"] if m["key"] == key and m.get("sent") is not None]
            normal = [m for m in textual if not m.get("fixed")]
            fixed = len(normal) < len(textual)
            if textual and not normal:
                roles[key] = IR.REF                  # 참조 고정이 역할 보존보다 먼저다
                out["fixed_ref_keys"] += 1
            elif not textual:
                roles[key] = IR.REF                  # item 태그로만 남은 키(새 v2 backfill 과 같다, final-review #7)
            elif fixed or changed or key not in old_roles:
                to_judge.append(key)
            else:
                roles[key] = old_roles[key]
                out["preserved_keys"] += 1
        out["dropped_keys"] += len(set(old_roles) - set(parsed["keys"]))
        out["judge_keys"] += len(to_judge)
        if to_judge:
            out["judge"][mid] = dict(parsed, mentions=[m for m in parsed["mentions"]
                                                       if m["key"] in to_judge and m.get("sent") is not None
                                                       and not m.get("fixed")])
            out["partial"][mid] = roles
        else:
            out["keep"][mid] = roles
    return out


def rekey(db: str, out: str, listing: Mapping[str, Mapping[str, Any]], aliases: Mapping[str, str],
          caller_factory: Callable[[], Callable[[str], Dict[str, Any]]], max_usd: Optional[float],
          rules: str = IR.KEY_RULES_V2, batch: int = ROLE_BATCH, file_mode: Optional[int] = None,
          now: Callable[[], str] = IR.utc_now, bank: Optional[str] = None, cfg: Optional[JudgeConfig] = None,
          spend_log: Optional[str] = None, github: Optional[GithubConfig] = None) -> int:
    """원본 저장소(db)에 키 규칙 rules 를 적용해 out 에 새 저장소를 공개한다(원본은 읽기 전용으로만 연다)."""
    cfg = cfg or JudgeConfig()
    if not os.path.exists(db):
        raise Fail("source side store %s does not exist" % os.path.basename(db))
    if os.path.abspath(out) == os.path.abspath(db) or os.path.exists(out):
        raise Fail("--out must be a new path (the source is never rewritten)")
    try:
        src_meta, src_mems, src_roles, gh_tables = IR.load_store(db, with_github=True)
    except IR.StoreError as e:
        raise Fail(str(e))
    check_bank(src_meta, bank)
    check_meta(src_meta, meta_values(aliases, cfg, IR.key_rules_of(src_meta)))   # 판정 설정은 원본과 같아야 보존이 맞다
    if file_mode is None:
        file_mode = os.stat(db).st_mode & 0o777
    p = rekey_plan(src_mems, src_roles, listing, aliases, rules)
    items = items_of(p["judge"]) if cfg.input_mode == INPUT_SENTENCE else memory_items(p["judge"], listing)
    chunks = batches_of(items, cfg, batch)
    line = {"phase": "plan", "mode": "rekey", "from_key_rules": IR.key_rules_of(src_meta), "key_rules": rules,
            "source_memories": len(src_mems), "keep_memories": len(p["keep"]), "judge_memories": len(p["judge"]),
            "judge_mentions": len(items), "batches": len(chunks)}
    for name in ("unlisted", "no_keys", "text_changed", "preserved_keys", "fixed_ref_keys", "judge_keys",
                 "dropped_keys"):
        line[name] = p[name]
    if rules != IR.KEY_RULES_V1:
        line.update({k: v for k, v in v2_counts(listing, aliases, rules).items() if k != "key_rules"})
    emit(line)

    def row(mid: str, judged_at: str) -> Dict[str, Any]:
        r = dict(src_mems[mid])
        r.pop("memory_id", None)
        r["text_sha256"] = p["texts"][mid]
        r["judged_at"] = judged_at
        return r

    mems = {mid: row(mid, src_mems[mid]["judged_at"]) for mid in p["keep"]}
    roles = {mid: dict(r) for mid, r in p["keep"].items()}
    by_mid: Dict[str, List[Dict[str, Any]]] = {}
    for it in items:
        by_mid.setdefault(it["mid"], []).append(it)
    got: Dict[int, str] = {}
    failed: set = set()
    done: set = set()
    spend = Spend(max_usd)
    stopped = False
    caller = caller_factory() if items else None
    call_log: List[Dict[str, Any]] = []
    for bi, chunk in enumerate(chunks):
        res, calls, hit_cap = judge_votes(chunk, caller, spend, cfg)
        for c in calls:
            call_log.append(dict(c, batch=bi))
            emit({"phase": "call", "batch": bi, **c, "spent_usd": round(spend.spent, 6)})
        if hit_cap:
            stopped = True
            emit({"phase": "stop", "reason": "max-usd", "spent_usd": round(spend.spent, 6),
                  "estimate_usd": round(spend.estimate(), 6), "max_usd": max_usd})
            break
        if res is None:
            failed |= {it["mid"] for it in chunk}
            continue
        got.update(res)
        for mid in sorted({it["mid"] for it in chunk}):
            if mid in failed or mid in done or not all(x["i"] in got for x in by_mid[mid]):
                continue
            if cfg.input_mode == INPUT_SENTENCE:
                idx = {(x["key"], x["sent"]): x["i"] for x in by_mid[mid]}
                role_of = lambda m, idx=idx: got[idx[(m["key"], m["sent"])]]  # noqa: E731
            else:
                kdx = {x["key"]: x["i"] for x in by_mid[mid]}
                role_of = lambda m, kdx=kdx: got[kdx[m["key"]]]  # noqa: E731
            judged = IR.memory_roles(p["judge"][mid]["mentions"], role_of)[mid]
            roles[mid] = dict(p["partial"][mid], **judged)
            mems[mid] = row(mid, now())
            done.add(mid)
    if github is not None:
        gh_tables = collect_github(store_repos(roles), gh_tables, github, now())
    meta = meta_values(aliases, cfg, rules)
    if bank or src_meta.get("bank"):
        meta["bank"] = bank or src_meta["bank"]
    meta["updated_at"] = now()
    sha = IR.publish(out, meta, mems, roles, mode=file_mode, github=gh_tables)
    if spend_log:
        models = Counter(m for c in call_log for m in (c.get("models") or []))
        write_private_json(spend_log, {"cost_usd": round(spend.spent, 6), "calls": call_log,
                                       "models_used": dict(sorted(models.items())), "model": cfg.model,
                                       "judge_mode": cfg.judge_mode, "prompt_sha256": cfg.prompt_sha256,
                                       "judged_now": len(done), "stopped": stopped, "mode": "rekey"})
    unjudged = len(p["judge"]) - len(done)
    dist = Counter(r for ks in roles.values() for r in ks.values())
    emit({"phase": "done", "mode": "rekey", "judged_now": len(done), "unjudged": unjudged,
          "failed_memories": len(failed - done), "stopped": stopped, "calls": spend.calls,
          "cost_usd": round(spend.spent, 6), "store_memories": len(mems),
          "store_mentions": sum(len(v) for v in roles.values()), "role_dist": dict(sorted(dist.items())),
          "published": True, "sha256": sha})
    return 3 if (stopped or unjudged) else 0


def status(db: str, listing: Optional[Mapping[str, Mapping[str, Any]]], aliases: Optional[Mapping[str, str]]) -> int:
    if not os.path.exists(db):
        raise Fail("side store %s does not exist" % os.path.basename(db))
    try:
        meta, mems, roles = IR.load_store(db)
    except IR.StoreError as e:
        raise Fail(str(e))
    out: Dict[str, Any] = {"meta": meta, "judged_memories": len(mems),
                           "mentions": sum(len(v) for v in roles.values()),
                           "role_dist": dict(sorted(Counter(r for ks in roles.values() for r in ks.values()).items())),
                           "state_dist": dict(sorted(Counter(m["state"] for m in mems.values()).items()))}
    if listing is not None and aliases is not None:
        if IR.key_rules_of(meta) not in IR.KEY_RULES:
            raise Fail("side store key_rules %r is not one of %s" % (IR.key_rules_of(meta), IR.KEY_RULES))
        p = plan(listing, aliases, mems, roles, rules=IR.key_rules_of(meta))
        out.update(listed=len(listing), keyed=p["keyed"], unjudged=len(p["judge"]) + len(p["tag_only"]),
                   meta_refresh_pending=len(p["refresh"]), drop_pending=len(p["drop"]),
                   aliases_match=(meta.get("aliases_sha256") == IR.aliases_sha256(aliases)))
    emit(out)
    return 0


# --- CLI -------------------------------------------------------------------------------------------------

def _bank(args) -> str:
    if args.bank:
        return args.bank
    import hc_host_env
    bank = hc_host_env.get("HC_PERSONAL_BANK")
    if not bank:
        raise Fail("--bank not given and HC_PERSONAL_BANK is not set")
    return bank


def _aliases(args, bank: str) -> Dict[str, str]:
    if args.aliases_file:
        return file_aliases(args.aliases_file)
    return policy_aliases(args.policies, bank)


def _github(args, aliases: Mapping[str, str]) -> Optional[GithubConfig]:
    """--github 인자 -> 수집 설정(없으면 None, gh 호출기도 만들지 않는다). 소유자 표의 repo 는 별칭 표로 정규화한다."""
    if not args.github:
        if args.github_owners or args.github_default_owner:
            raise Fail("--github-owners/--github-default-owner need --github")
        return None
    owners: Dict[str, str] = {}
    if args.github_owners:
        raw = args.github_owners
        try:
            if not raw.lstrip().startswith("{"):
                with open(raw, encoding="utf-8") as fh:
                    raw = fh.read()
            owners = json.loads(raw)
        except (OSError, ValueError):
            raise Fail("cannot read --github-owners (JSON object {repo: owner} or a file with one)") from None
        if not isinstance(owners, dict) or not all(isinstance(k, str) and isinstance(v, str) and v.strip()
                                                    for k, v in owners.items()):
            raise Fail("--github-owners must be a JSON object {repo: owner}")
    if args.github_min_interval < 0:
        raise Fail("--github-min-interval must be 0 or more")
    # 저장소 키와 같은 정규화(별칭 표 -> slug, final-review C4)라야 owner_of 가 store_repos 의 repo 를 찾는다.
    table = IK._table(aliases)
    owners = {IK._canonical(k, table) or k.strip().lower(): v for k, v in owners.items()}
    return GithubConfig(gh_runner(), owners=owners, default_owner=args.github_default_owner,
                        min_interval=args.github_min_interval)


def _positive_int(text: str) -> int:
    n = int(text)
    if n < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return n


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("backfill", "poll", "status", "rekey"):
        sp = sub.add_parser(name)
        sp.add_argument("--db", required=True, help="side store path (sqlite)")
        sp.add_argument("--bank", help="bank id (default: HC_PERSONAL_BANK from env or .env.host.local)")
        sp.add_argument("--policies", default=os.path.join(REPO, "policies", "bank-policies.json"),
                        help="tracked bank-policies.json (local/ overlay beside it is merged)")
        sp.add_argument("--aliases-file", help="experimental alias table instead of the bank policy")
        sp.add_argument("--allow-prod", action="store_true", help="allow a target that is not the hc-next copy")
        sp.add_argument("--offline-bodies", help="offline listing: memory bodies JSONL (id, text) instead of upstream")
        sp.add_argument("--offline-meta", help="offline listing: memory meta JSONL (id, tags, state, document_id, "
                                               "mentioned_at, fact_type)")
        sp.add_argument("--mentioned-until", help="offline listing: skip memories after this KST day (YYYY-MM-DD)")
        if name == "status":
            sp.add_argument("--offline", action="store_true", help="read the side store only")
        else:
            sp.add_argument("--max-usd", type=float, default=None, help="stop before a call that would pass this")
            sp.add_argument("--batch", type=_positive_int, default=ROLE_BATCH)
            sp.add_argument("--file-mode", type=lambda s: int(s, 8), default=None,
                            help="octal mode of the published file (default: keep the existing file's mode, new file 644)")
            sp.add_argument("--model", help="judge model id (default %s)" % ROLE_MODEL)
            sp.add_argument("--input", choices=INPUTS, help="judge input (default %s)" % DEFAULT_INPUT)
            sp.add_argument("--votes", type=_positive_int, help="judge each batch N times and take the majority "
                                                                "(default %d)" % DEFAULT_VOTES)
            sp.add_argument("--prompt-file", help="judge prompt text file instead of the default for --input")
            sp.add_argument("--spend-log", help="write call records and the cost sum here (JSON, 0600)")
            sp.add_argument("--rules", choices=IR.KEY_RULES, required=(name == "rekey"),
                            default=None if name == "rekey" else IR.KEY_RULES_V1,
                            help="item key rules (default v1; v2 = ranges, PR numbers, upstream context)")
            sp.add_argument("--github", action="store_true", help="collect GitHub issue/PR state with `gh api` GET")
            sp.add_argument("--github-owners", help="JSON object {repo: owner} (inline or a file path)")
            sp.add_argument("--github-default-owner", help="owner for repos not in --github-owners")
            sp.add_argument("--github-min-interval", type=int, default=DEFAULT_GH_MIN_INTERVAL,
                            help="do not refetch a repo fetched this many seconds ago (default %d)"
                                 % DEFAULT_GH_MIN_INTERVAL)
        if name == "rekey":
            sp.add_argument("--out", required=True, help="new side store path (the source is never rewritten)")
            sp.add_argument("--save-listing", nargs=2, metavar=("BODIES", "META"),
                            help="save the listing as offline JSONL files (0600)")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "status" and args.offline:
            return status(args.db, None, None)
        if bool(args.offline_bodies) != bool(args.offline_meta):
            raise Fail("--offline-bodies and --offline-meta go together")
        if args.mentioned_until and not args.offline_bodies:
            raise Fail("--mentioned-until needs the offline listing")
        if args.offline_bodies:          # 오프라인 나열(upstream 0): 별칭 표는 --aliases-file 로만, 뱅크는 주면 meta 에
            if not args.aliases_file:
                raise Fail("the offline listing needs --aliases-file")
            bank = args.bank
            aliases = file_aliases(args.aliases_file)
            listing = offline_listing(args.offline_bodies, args.offline_meta, args.mentioned_until)
        else:
            bank = _bank(args)
            aliases = _aliases(args, bank)
            get = upstream_getter(args.allow_prod)
            listing = list_all(get, bank)
        if args.cmd == "status":
            return status(args.db, listing, aliases)
        prompt = None
        if args.prompt_file:
            try:
                with open(args.prompt_file, encoding="utf-8") as fh:
                    prompt = fh.read()
            except OSError as e:
                raise Fail("cannot read --prompt-file: %s" % e.strerror)
        cfg = JudgeConfig(model=args.model, input_mode=args.input, votes=args.votes, prompt=prompt)
        github = _github(args, aliases)
        if args.cmd == "rekey":
            if args.save_listing:
                # 새 경로만(원본 저장소·오프라인 입력·다른 파일을 덮어쓰지 않는다, final-review C11).
                sl = [os.path.abspath(x) for x in args.save_listing]
                if sl[0] == sl[1] or any(os.path.lexists(x) for x in sl):
                    raise Fail("--save-listing needs two new, different paths")
                save_listing(listing, args.save_listing[0], args.save_listing[1])
            return rekey(args.db, args.out, listing, aliases, lambda: claude_caller(args=cfg.claude_args()),
                         args.max_usd, rules=args.rules, batch=args.batch, file_mode=args.file_mode, bank=bank,
                         cfg=cfg, spend_log=args.spend_log, github=github)
        return run_sync(args.cmd, args.db, listing, aliases, lambda: claude_caller(args=cfg.claude_args()),
                        args.max_usd, batch=args.batch, file_mode=args.file_mode, bank=bank, cfg=cfg,
                        spend_log=args.spend_log, rules=args.rules, github=github)
    except Fail as e:
        print("item-roles-sidecar: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
