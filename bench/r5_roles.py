"""런 5 역할 구분 원장 오라클(R5-A3, issue #372, 상위 #360 "결정됨"의 "R5-A3 역할 구분 원장 오라클").

  r5_roles.py bodies    --meta <jsonl> --bank <bank> --out <jsonl>                   (hc-next 사본만, 읽기만, 1회)
  r5_roles.py mentions  --meta <jsonl> --bodies <jsonl> --aliases <json> [--policy <json>] --out <json>
  r5_roles.py llm-roles --mentions <json> --out <json> [--limit N] [--max-usd X] [--spent-dir <dir>]
  r5_roles.py tables    --labelset <jsonl> --aliases <json> --meta <jsonl> --mentions <json> [--llm-roles <json>]
                        --plan <json> --families <json> --tag-aliases <json> --recalls <dir> --t <t> ... --out <json>
  r5_roles.py dllm      (tables 인자) --variant o3c|o3c_llm|o3a|o3b --form open|full [--o1c] --k 3
                        --out-dir <dir> --label L --max-usd X [--spent-dir <dir>]
  r5_roles.py judge     --labelset <jsonl> --aliases <json> --meta <jsonl> --mentions <json> --llm-roles <json>
                        --recalls <dir> --t <t> ... --base-gen-dir <dir> --base base1 ... --gen-dir <dir>
                        --setting <name>=<label> ... --out <json>
  r5_roles.py sample    --mentions <json> --llm-roles <json> --n 40 --out <json>
  r5_roles.py agree     --sample <json> --verdicts <json> --out <json>

원문(본문·언급 문장·LLM 출력·d-llm 출력)은 리포 밖 0600 파일에만 쓴다. 표준 출력에는 수치·sha256 만 낸다.

입력 경계(봉인 보호): 본문은 기억 메타에서 키가 나오고 mentioned_at <= dev 마지막 t(2026-10-13) 끝 시각인 기억만 모은다
(`bodies`). 다른 명령은 그보다 늦은 본문이 섞여 있으면 거부하고(`load_bodies`), t 는 `r5_metrics.check_not_sealed` 로 거른다.
GitHub·라벨셋은 상태·역할 판정 입력이 아니다(채점 `judge`·`tables` 의 P·R 셈에만 쓴다). 라벨 쪽 규칙 모듈
(`bench/r5_labels.py`)은 직접 import 하지 않는다(키 정의는 `gateway.item_keys.extract_keys`, 완료 계열은 S1 쪽
`gateway.item_status.is_completion_family`, 시각·쌍·채점 도우미는 `bench/r5_metrics.py`·`bench/r5_oracle.py` 재사용).

언급 찾기(`memory_mentions`): 본문의 펜스 코드 블록을 공백으로 지우고(extract_keys 와 같은 정책) 줄바꿈과 문장 끝(`.`·`!`·`?`·`。`
뒤 공백)에서 문장으로 나눈다. 문장마다 `extract_keys(문장, item: 태그를 뺀 태그, 별칭 표)`로 그 문장의 키 언급을 얻는다.
`item:` 태그에서만 나온 키(본문 위치 없음)는 역할 판정 대상이 아니고 역할은 참조다. 기억마다 (문장 키 합집합 ∪ item 태그 키)가
기억 메타의 `keys` 와 다른 수를 센다(`key_mismatch`).

언급 역할(규칙, `sentence_role`): 끝냄(done) / 열림(open) / 참조(ref).
  * 열림 표지어(남은·남음·후속·진행·대기·미완·미해결·미착수·보류·할 일·예정·착수·블로커·막힘·재오픈·아직·다음 <단계·작업·할 일·
    차례·순서·세션·티켓·이슈>·todo·pending·wip·next·follow-up·remaining)가 문장에 있으면 열림.
  * 아니고 끝냄 표지어(완료·끝냄·끝남·끝났·끝내·마쳤·마침·머지·merge·닫았·닫음·닫힘·close·해결·해소·종료·마감·배포·반영·
    fixed·done·shipped)가 있으면 끝냄. 한 문장에 둘 다 있으면 열림(미완료처럼 열림 표지어가 끝냄 낱말을 품는 경우 포함).
  * 표지어가 없고 그 문장이 기억 맨 앞 라벨 `[...]`을 담은 첫 문장이면 라벨로 정한다(라벨 열림 낱말: 진행·대기·보류·할 일·todo·
    인계·handoff·남은·미완·계획·예정·다음·블로커, 라벨 끝냄 낱말: 완료·끝·done·머지·배포·해결·종료·마감, 둘 다면 열림).
  * 그 밖은 참조. 한 문장의 모든 키 언급이 같은 역할을 받는다(문장 단위).
  * 기억 하나 안에서 한 키의 역할 = 열림 언급이 있으면 열림, 아니고 끝냄 언급이 있으면 끝냄, 아니면 참조(열림이 이긴다).
언급 역할(LLM, `llm_roles`, 상한 측정 전용, 게이트웨이에 넣지 않는다): `claude-haiku-5-5` 에 언급 문장과 키만 묶음으로 준다
  (`bench/d_llm.py` 의 claude 호출 경로). 출력 실패·빠진 항목은 1회 재시도 뒤 참조로 두고 수를 센다.

키 상태(o3c, `role_states`): t 끝 시각 이하 유효(state=valid) 기억만. 키 상태 = 마지막 비참조 언급(기억 단위 역할)의 역할.
  같은 mentioned_at 의 서로 다른 기억이 갈리면 열림. t 이전 언급이 모두 참조면 미정(열림만 표에 넣지 않고 전체 표에는 `미정`).
  날짜·근거 id = 그 기억(같은 시각 여럿이면 이긴 역할 쪽 id 최대).
o3a / o3b(`o3_variant_states`): R5-A2 o3 규칙(t 이하 유효 완료 계열 기억이 있으면 끝남, 없고 언급이 있으면 열림)에서
  o3a = 키가 하나인 완료 계열 기억만 끝남 근거(키가 여럿이면 일반 언급), o3b = 완료 계열 기억 본문 중 그 키 언급 문장에
  '남은 일·다음·후속·진행·대기·미완' 표지어가 있으면 그 기억을 끝남 근거에서 뺀다(일반 언급으로 센다).
표(`render_table`): 범위 = R5-A2 o3 와 같은 질의 범위 프로젝트(`r5_metrics.query_projects`) 안의 키.
  전체 = 범위 안 키 전체 상태, 열림만 = 열림 키 + 최근 끝남 N=7일(끝남 날짜 d 가 t-6 ~ t, KST 날짜).
  표 토큰 = 표 텍스트 tiktoken cl100k_base / 그 t 의 필터 뒤 d-llm 입력(200,000자 상한 적용 뒤 행 렌더) 토큰.
o1c(`o1c`): o3c 상태가 t 에 끝남인 키를 말하는 recall 행마다 o1 형식 주석 `[항목 상태: <키> 끝남 <날짜> 근거 <id>]` 을 붙인다.
  근거 = 그 키의 최신 끝냄 언급(o3c 상태의 근거). 라벨셋으로 키를 고르지 않는다.
d-llm: R5-A2 와 같은 t·K=3·`claude-sonnet-5-5`·동결 기준선 프롬프트·`d_llm_match` 대응. 표는 recall 행 뒤
  (`r5_metrics.build_prompt_with_table`). 지출 한도(`SpendGuard`): 설정 시작 전에 `--spent-dir` 누계 + 이 설정 호출 수 x
  호출당 평균(측정된 d-llm 호출 평균, 없으면 0.30달러)이 `--max-usd` 를 넘으면 시작하지 않고(exit 3), 호출 전마다
  `--spent-dir` 의 d-llm 출력·LLM 역할 판정 파일 비용 합을 다시 읽어(같은 디렉터리를 쓰는 다른 프로세스 지출 포함)
  한도면 멈춘다(exit 3). o3c_llm 은 역할 파일의 mentions_sha256 이 지금 언급 파일과 같아야 쓴다.
판정(`judge`): P·R·R_all 은 `r5_metrics.pr_summary`. R_bank 분모 = 표본 t 대조 쌍 중 o3c-llm(보조로 o3c 규칙) 상태가 열림인 쌍,
  끝남 = 라벨-뱅크 불일치, 미정·언급 없음 = 뱅크 언급 없음(둘 다 분모 밖, 수만 적는다).
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import random
import re
import sys
import urllib.parse
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from gateway.item_keys import extract_keys  # noqa: E402  (키 정의만)

KST = timezone(timedelta(hours=9))
DEV_LAST_T = date(2026, 10, 13)              # dev 마지막 t. 그 뒤(간격·봉인 구간) 기억 본문은 수집·판독하지 않는다
FACT_TYPES = ("world", "experience")
SEED = 20261101
RECENT_DONE_DAYS = 7                         # 열림만 표의 최근 끝남 N(모든 변형에 같은 값)
ROLE_MODEL = "claude-haiku-5-5"
ROLE_BATCH = 40
DONE, OPEN, REF = "done", "open", "ref"
S_DONE, S_OPEN, S_UNDET = "끝남", "열림", "미정"
VARIANTS = ("o3c", "o3c_llm", "o3a", "o3b")
FORMS = ("open", "full")


class Fail(Exception):
    pass


def parse_instant(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        v = datetime.fromisoformat(text)
    except ValueError:
        return None
    return v if v.tzinfo else v.replace(tzinfo=timezone.utc)


def t_end(t: date) -> datetime:
    return datetime.combine(t, time(23, 59, 59, 999999), tzinfo=KST)


def kst_day(at: datetime) -> str:
    return at.astimezone(KST).date().isoformat()


def _day(t: Any) -> date:
    return t if isinstance(t, date) else date.fromisoformat(str(t))


def write_private_text(path: str, text: str) -> str:
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, 0o600)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_private_json(path: str, obj: Any) -> str:
    return write_private_text(path, json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True))


def read_jsonl(path: str) -> List[Dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def read_json(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


# --- 본문 수집(사본, 1회) -----------------------------------------------------------------------------

def body_targets(meta: Iterable[Mapping[str, Any]], cutoff: date = DEV_LAST_T) -> Dict[str, Mapping[str, Any]]:
    """기억 메타에서 키가 나오고 mentioned_at <= cutoff 끝 시각인 기억(id -> 메타 줄). 시각 없는 기억은 뺀다."""
    end = t_end(cutoff)
    out = {}
    for m in meta:
        if not m.get("keys"):
            continue
        at = parse_instant(m.get("mentioned_at"))
        if at is None or at > end:
            continue
        out[str(m["id"])] = m
    return out


def load_bodies(path: str, cutoff: date = DEV_LAST_T) -> Dict[str, str]:
    """본문 파일 -> {id: 본문}. cutoff 뒤 기억이 섞여 있으면 거부한다(봉인 보호)."""
    out = {}
    end = t_end(cutoff)
    for r in read_jsonl(path):
        at = parse_instant(r.get("mentioned_at"))
        if at is None or at > end:
            raise Fail(f"body {r.get('id')} is outside mentioned_at <= {cutoff} end; refusing (sealed window guard)")
        out[str(r["id"])] = r.get("text") or ""
    return out


def _bank_config():
    spec = importlib.util.spec_from_file_location("bank_config", os.path.join(REPO, "scripts", "bank-config.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _list_memories(bc, container, key, bank, fact_type, page_size=500):
    """memories/list 전 쪽(GET 만). id 수가 total 과 맞아야 한다(r5_labels.list_memories 와 같은 방식)."""
    seen, offset, total = {}, 0, None
    while True:
        q = urllib.parse.urlencode([("type", fact_type), ("limit", page_size), ("offset", offset)])
        page = bc.upstream(container, key, "GET",
                           "/v1/default/banks/%s/memories/list?%s" % (urllib.parse.quote(bank, safe=""), q))
        if not isinstance(page.get("total"), int):
            raise Fail("memories/list answered without an integer total")
        total = page["total"] if total is None else total
        if page["total"] != total:
            raise Fail("memories/list total moved while paging")
        items = page.get("items") or []
        for it in items:
            seen[it["id"]] = it
        offset += len(items)
        if not items or offset >= total:
            break
    if len(seen) != total:
        raise Fail("memories/list ids do not add up to the total")
    return list(seen.values())


def cmd_bodies(args) -> int:
    meta = read_jsonl(args.meta)
    want = body_targets(meta)
    bc = _bank_config()
    container, is_copy, key = bc.resolve_target()
    if not is_copy:
        raise Fail("bodies reads the hc-next copy only (HC_TARGET=next); nothing was read")
    if os.path.exists(args.out):
        raise Fail(f"{args.out} exists")
    got: Dict[str, Dict[str, Any]] = {}
    listed = 0
    for ft in FACT_TYPES:
        for mem in _list_memories(bc, container, key, args.bank, ft):
            listed += 1
            rid = str(mem.get("id"))
            if rid not in want or rid in got:
                continue
            text = mem.get("text") if isinstance(mem.get("text"), str) else mem.get("content")
            got[rid] = {"id": rid, "text": text if isinstance(text, str) else "",
                        "mentioned_at": mem.get("mentioned_at")}
    missing = sorted(set(want) - set(got))
    moved = sum(1 for rid, r in got.items() if r["mentioned_at"] != want[rid].get("mentioned_at"))
    text = "".join(json.dumps(got[rid], sort_keys=True, ensure_ascii=False) + "\n" for rid in sorted(got))
    sha = write_private_text(args.out, text)
    print(json.dumps({"targets": len(want), "listed": listed, "collected": len(got), "missing": len(missing),
                      "mentioned_at_differs": moved, "cutoff": DEV_LAST_T.isoformat(), "sha256": sha}))
    return 0 if not missing else 1


# --- 문장·언급·역할(규칙) ------------------------------------------------------------------------------

_FENCE_RE = re.compile(r"(`{3,}|~{3,}).*?(?:(?<![`~])\1|\Z)", re.S)     # gateway/item_keys.py 와 같은 펜스 정책
_SENT_SPLIT_RE = re.compile(r"\n+|(?<=[.!?。])\s+")
_LABEL_RE = re.compile(r"^\s*\[([^\]]{1,24})\]")
_OPEN_RE = re.compile(
    r"남은|남음|남아|후속|진행|대기(?!업)|미완|미해결|미착수|보류|할\s*일|예정|착수|블로커|막힘|막혀|재오픈|아직|"
    r"다음\s*(?:단계|작업|할\s*일|차례|순서|세션|티켓|이슈)|"
    r"(?<![a-z0-9_])(?:todo|to-do|pending|wip|next|follow-?up|remaining)(?![a-z0-9_])", re.I)
_DONE_RE = re.compile(
    r"완료|끝냄|끝남|끝났|끝내|마쳤|마침|머지|닫았|닫음|닫힘|해결|해소|종료|마감|배포|반영|"
    r"(?<![a-z0-9_])(?:merged?|closed?|closes|fixed|done|shipped)(?![a-z0-9_])", re.I)
_LABEL_OPEN_RE = re.compile(r"진행|대기(?!업)|보류|할\s*일|todo|인계|handoff|남은|미완|계획|예정|다음|블로커", re.I)
_LABEL_DONE_RE = re.compile(r"완료|끝|done|머지|배포|해결|종료|마감", re.I)
_O3B_RE = re.compile(r"남은\s*일|다음|후속|진행|대기|미완")              # 이슈 #372 o3b 표지어 6개


def split_sentences(text: str) -> List[str]:
    masked = _FENCE_RE.sub(" ", text or "")
    return [s.strip() for s in _SENT_SPLIT_RE.split(masked) if s and s.strip()]


def label_of(text: str) -> Optional[str]:
    m = _LABEL_RE.match(text or "")
    return m.group(1) if m else None


def sentence_role(sentence: str, label: Optional[str] = None, is_label_sentence: bool = False) -> str:
    """한 문장의 역할(모듈 머리 주석 규칙). 열림 표지어가 끝냄보다 먼저다. 표지어가 없으면 라벨 문장만 라벨로 정한다."""
    body = _LABEL_RE.sub(" ", sentence, count=1) if is_label_sentence else sentence
    if _OPEN_RE.search(body):
        return OPEN
    if _DONE_RE.search(body):
        return DONE
    if is_label_sentence and label:
        if _LABEL_OPEN_RE.search(label):
            return OPEN
        if _LABEL_DONE_RE.search(label):
            return DONE
    return REF


def merge_roles(roles: Iterable[str]) -> str:
    """기억 하나 안에서 한 키의 역할: 열림이 이긴다, 아니면 끝냄, 아니면 참조."""
    rs = set(roles)
    return OPEN if OPEN in rs else DONE if DONE in rs else REF


def _key_text(k: Tuple[str, int]) -> str:
    return "%s#%d" % k


def memory_mentions(mid: str, text: str, tags: Sequence[str], aliases: Mapping[str, str]) -> Dict[str, Any]:
    """기억 하나 -> 문장별 키 언급과 규칙 역할. item 태그에서만 나온 키는 참조(문장 없음)."""
    tags = [t for t in tags if isinstance(t, str)]
    item_tags = [t for t in tags if t.strip().lower().startswith("item:")]
    other_tags = [t for t in tags if t not in item_tags]
    sents = split_sentences(text)
    label = label_of(text)
    mentions: List[Dict[str, Any]] = []
    in_text: Set[str] = set()
    for i, s in enumerate(sents):
        ks = sorted(_key_text(k) for k in extract_keys(s, other_tags, aliases))
        if not ks:
            continue
        role = sentence_role(s, label, is_label_sentence=(i == 0 and label is not None))
        for k in ks:
            mentions.append({"mid": mid, "key": k, "sent": i, "role": role})
            in_text.add(k)
    tag_keys = {_key_text(k) for k in extract_keys("", item_tags, aliases)}
    for k in sorted(tag_keys - in_text):
        mentions.append({"mid": mid, "key": k, "sent": None, "role": REF, "source": "item_tag"})
    return {"mid": mid, "sentences": sents, "label": label, "mentions": mentions,
            "keys": sorted(in_text | tag_keys)}


def memory_roles(mentions: Iterable[Mapping[str, Any]], role_of: Optional[Callable[[Mapping[str, Any]], str]] = None
                 ) -> Dict[str, Dict[str, str]]:
    """언급 목록 -> {기억 id: {키: 기억 단위 역할}}. role_of 를 주면 언급마다 그 역할(LLM 판본)을 쓴다."""
    acc: Dict[str, Dict[str, List[str]]] = defaultdict(lambda: defaultdict(list))
    for m in mentions:
        r = m["role"] if role_of is None else role_of(m)
        acc[m["mid"]][m["key"]].append(r)
    return {mid: {k: merge_roles(rs) for k, rs in ks.items()} for mid, ks in acc.items()}


# --- 키 상태 -------------------------------------------------------------------------------------------

def _valid_at(meta_by_id: Mapping[str, Mapping[str, Any]], mid: str, end: datetime) -> Optional[datetime]:
    m = meta_by_id.get(mid)
    if not m or m.get("state") != "valid":
        return None
    at = parse_instant(m.get("mentioned_at"))
    return at if at is not None and at <= end else None


def role_states(roles: Mapping[str, Mapping[str, str]], meta_by_id: Mapping[str, Mapping[str, Any]],
                t: Any) -> Dict[str, Dict[str, str]]:
    """o3c: 키마다 t 끝 시각 이하 마지막 비참조 언급의 역할(같은 시각이 갈리면 열림). 참조만 있으면 미정."""
    end = t_end(_day(t))
    per: Dict[str, List[Tuple[datetime, str, str]]] = defaultdict(list)
    for mid, ks in roles.items():
        at = _valid_at(meta_by_id, mid, end)
        if at is None:
            continue
        for k, r in ks.items():
            per[k].append((at, mid, r))
    out: Dict[str, Dict[str, str]] = {}
    for k, lst in per.items():
        nonref = [x for x in lst if x[2] != REF]
        if not nonref:
            at, mid, _ = max(lst)
            out[k] = {"state": S_UNDET, "date": kst_day(at), "id": mid}
            continue
        last = max(x[0] for x in nonref)
        tied = [x for x in nonref if x[0] == last]
        win = OPEN if any(x[2] == OPEN for x in tied) else DONE
        mid = max(x[1] for x in tied if x[2] == win)
        out[k] = {"state": S_OPEN if win == OPEN else S_DONE, "date": kst_day(last), "id": mid}
    return out


def o3b_open_context(sentences: Sequence[str], mentions: Iterable[Mapping[str, Any]], key: str) -> bool:
    """o3b: 그 기억에서 그 키 언급 문장 중 하나에 o3b 표지어가 있는가."""
    for m in mentions:
        if m["key"] == key and m.get("sent") is not None and _O3B_RE.search(sentences[m["sent"]]):
            return True
    return False


def o3_variant_states(meta: Iterable[Mapping[str, Any]], t: Any, tag_aliases: Mapping[str, str],
                      done_pred: Callable[[Mapping[str, Any], str], bool]) -> Dict[str, Dict[str, str]]:
    """R5-A2 o3 규칙의 변형: 끝남 근거가 되는 (기억, 키)를 done_pred 로 고른다. 근거가 있으면 끝남(최신), 없고 언급이 있으면
    열림(최신 언급). done_pred(메타 줄, 키) 에는 완료 계열 판정까지 들어 있다."""
    end = t_end(_day(t))
    done: Dict[str, Tuple[datetime, str]] = {}
    seen: Dict[str, Tuple[datetime, str]] = {}
    for m in meta:
        if m.get("state") != "valid":
            continue
        at = parse_instant(m.get("mentioned_at"))
        if at is None or at > end:
            continue
        for k in m.get("keys") or []:
            cur = (at, str(m.get("id")))
            if k not in seen or cur > seen[k]:
                seen[k] = cur
            if done_pred(m, k) and (k not in done or cur > done[k]):
                done[k] = cur
    out = {}
    for k in seen:
        at, rid = done.get(k) or seen[k]
        out[k] = {"state": S_DONE if k in done else S_OPEN, "date": kst_day(at), "id": rid}
    return out


def variant_pred(variant: str, tag_aliases: Mapping[str, str],
                 parsed: Optional[Mapping[str, Mapping[str, Any]]] = None) -> Callable[[Mapping[str, Any], str], bool]:
    from gateway.item_status import is_completion_family

    def is_done(m):
        return is_completion_family({"tags": list(m.get("tags") or [])}, tag_aliases)
    if variant == "o3":
        return lambda m, k: is_done(m)
    if variant == "o3a":
        return lambda m, k: is_done(m) and len(m.get("keys") or []) == 1
    if variant == "o3b":
        if parsed is None:
            raise Fail("o3b needs parsed mentions")

        def pred(m, k):
            if not is_done(m):
                return False
            p = parsed.get(str(m.get("id")))
            if p is None:
                raise Fail(f"o3b: no body for completion memory {m.get('id')} (t beyond the body cutoff?)")
            return not o3b_open_context(p["sentences"], p["mentions"], k)
        return pred
    raise Fail(f"unknown o3 variant {variant}")


# --- 표 렌더와 o1c ---------------------------------------------------------------------------------------

def _key_sort(k: str) -> Tuple[str, int]:
    repo, _, n = k.rpartition("#")
    return repo, int(n) if n.isdigit() else 0


def table_rows(states: Mapping[str, Mapping[str, str]], scope: Iterable[str], t: Any, form: str,
               n_days: int = RECENT_DONE_DAYS) -> List[Dict[str, str]]:
    """범위 안 키의 표 줄. full = 전체(미정 포함), open = 열림 + 끝남 날짜가 t-(N-1) ~ t 인 키."""
    sc = set(scope)
    lo = (_day(t) - timedelta(days=n_days - 1)).isoformat()
    out = []
    for k in sorted(states, key=_key_sort):
        if k.rpartition("#")[0] not in sc:
            continue
        s = states[k]
        if form == "open" and not (s["state"] == S_OPEN or (s["state"] == S_DONE and s["date"] >= lo)):
            continue
        if form not in FORMS:
            raise Fail(f"unknown form {form}")
        out.append({"key": k, "state": s["state"], "date": s["date"], "id": s["id"]})
    return out


def render_table(rows: Sequence[Mapping[str, str]], t: Any, form: str, n_days: int = RECENT_DONE_DAYS) -> str:
    if form == "open":
        head = (f"<item_status_table> 뱅크 기억 기준 열린 작업 키와 최근 {n_days}일 안에 끝난 키(기준일 {t} 이전 기억만). "
                "여기 없는 키는 뱅크 기억 기준 열림이 아니거나 질의 범위 밖이다. 줄: 키 | 상태 | 날짜 | 근거 id")
    else:
        head = (f"<item_status_table> 뱅크 기억 기준 작업 키별 최신 상태(기준일 {t} 이전 기억만). "
                "상태: 열림 / 끝남 / 미정(참조만 있음). 줄: 키 | 상태 | 날짜 | 근거 id")
    body = [f"{r['key']} | {r['state']} | {r['date']} | {r['id']}" for r in rows]
    return "\n".join([head] + body + ["</item_status_table>"])


def _with_prefix(row: Mapping[str, Any], prefix: str) -> Dict[str, Any]:
    out = dict(row)
    fld = "text" if isinstance(row.get("text"), str) or not isinstance(row.get("content"), str) else "content"
    out[fld] = f"{prefix} {row.get(fld) or ''}"
    return out


def o1c(rows: Sequence[Mapping[str, Any]], states: Mapping[str, Mapping[str, str]],
        row_keys: Callable[[Mapping[str, Any]], Set[str]]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """o3c 상태가 끝남인 키를 말하는 recall 행마다 o1 형식 주석(근거 = 그 키의 최신 끝냄 언급)."""
    from bench.r5_oracle import note
    out, n_rows, keys_done = [], 0, set()
    for row in rows:
        ks = sorted(k for k in row_keys(row) if (states.get(k) or {}).get("state") == S_DONE)
        if not ks:
            out.append(dict(row))
            continue
        out.append(_with_prefix(row, " ".join(note(k, states[k]) for k in ks)))
        n_rows += 1
        keys_done.update(ks)
    return out, {"rows": len(rows), "annotated_rows": n_rows, "annotated_keys": len(keys_done)}


# --- LLM 역할 판정(상한 측정 전용) -----------------------------------------------------------------------

ROLE_PROMPT = """너는 작업 기록 문장에서 작업 키 언급의 역할을 분류한다.
각 항목은 기록에서 뽑은 문장 하나와 그 문장에 나온 작업 키 하나다. 그 문장이 그 키의 일을 어떻게 말하는지 하나만 고른다.
- done: 그 키의 일이 끝났다고 말한다(완료, 머지, 닫음, 해결, 배포 등).
- open: 그 키의 일이 아직 남았다고 말한다(남은 일, 다음 할 일, 진행 중, 대기, 보류, 미완, 후속 등).
- ref: 끝남도 남음도 말하지 않는 참조다(근거, 관련, 출처, 비교 대상, 단순 언급).
문장 맨 앞의 [괄호 라벨]은 그 기록 전체의 종류다.
설명 없이 JSON 배열 하나만 출력한다. 모든 항목에 하나씩: [{"i": <번호>, "role": "done" | "open" | "ref"}]

항목:
"""


def role_items(parsed: Mapping[str, Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """LLM 판정 대상 언급(문장 있는 언급만). i = 안정 순서 번호."""
    items = []
    for mid in sorted(parsed):
        p = parsed[mid]
        for m in p["mentions"]:
            if m.get("sent") is None:
                continue
            items.append({"mid": mid, "key": m["key"], "sent": m["sent"], "sentence": p["sentences"][m["sent"]],
                          "rule": m["role"]})
    for i, it in enumerate(items, 1):
        it["i"] = i
    return items


def role_prompt(batch: Sequence[Mapping[str, Any]]) -> str:
    lines = [json.dumps({"i": it["i"], "key": it["key"], "sentence": it["sentence"]}, ensure_ascii=False) for it in batch]
    return ROLE_PROMPT + "\n".join(lines)


def parse_roles(text: str, want: Iterable[int]) -> Dict[int, str]:
    """출력에서 첫 JSON 배열을 읽어 {i: role}. 모양이 아닌 항목과 요청 밖 번호는 버린다."""
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
        if isinstance(x, dict) and isinstance(x.get("i"), int) and x.get("role") in (DONE, OPEN, REF) and x["i"] in w:
            out[x["i"]] = x["role"]
    return out


def llm_roles(items: Sequence[Mapping[str, Any]], caller: Callable[[str], Dict[str, Any]], batch: int = ROLE_BATCH,
              budget: Optional[Callable[[float], bool]] = None) -> Dict[str, Any]:
    """묶음마다 1회 호출, 빠진 항목만 1회 재시도. 그래도 없으면 참조(fallback)로 두고 센다. budget(누계)가 False 면 멈춘다."""
    roles: Dict[int, str] = {}
    calls: List[Dict[str, Any]] = []
    spent = 0.0
    stopped = False
    for b in range(0, len(items), batch):
        todo = list(items[b:b + batch])
        for _attempt in range(2):
            if not todo:
                break
            if budget is not None and not budget(spent):
                stopped = True
                break
            raw = caller(role_prompt(todo))
            u = raw.get("usage") or {}
            cost = raw.get("total_cost_usd") or 0.0
            spent += cost
            got = {} if raw.get("is_error") else parse_roles(raw.get("result") or "", [it["i"] for it in todo])
            roles.update(got)
            calls.append({"batch": b // batch, "items": len(todo), "parsed": len(got), "cost_usd": cost,
                          "is_error": bool(raw.get("is_error")), "input_tokens": u.get("input_tokens"),
                          "cache_creation_input_tokens": u.get("cache_creation_input_tokens"),
                          "cache_read_input_tokens": u.get("cache_read_input_tokens"),
                          "output_tokens": u.get("output_tokens"), "duration_ms": raw.get("duration_ms"),
                          "raw_result": raw.get("result") or ""})
            todo = [it for it in todo if it["i"] not in got]
        if stopped:
            break
    fallback = [it["i"] for it in items if it["i"] not in roles]
    return {"roles": {str(i): r for i, r in sorted(roles.items())}, "fallback_ref": len(fallback) if not stopped else None,
            "stopped": stopped, "calls": calls, "cost_usd": spent, "n_items": len(items)}


def llm_role_lookup(items: Sequence[Mapping[str, Any]], llm: Mapping[str, Any]) -> Callable[[Mapping[str, Any]], str]:
    """언급 -> LLM 역할(문장 없는 item 태그 언급은 참조, 판정 없는 언급은 참조)."""
    idx = {(it["mid"], it["key"], it["sent"]): str(it["i"]) for it in items}
    roles = llm.get("roles") or {}

    def role_of(m):
        if m.get("sent") is None:
            return REF
        return roles.get(idx.get((m["mid"], m["key"], m["sent"]), ""), REF)
    return role_of


def agreement(a: Sequence[str], b: Sequence[str]) -> Dict[str, Any]:
    n = len(a)
    same = sum(1 for x, y in zip(a, b) if x == y)
    conf = Counter(f"{x}->{y}" for x, y in zip(a, b))
    return {"n": n, "agree": same, "rate": (same / n) if n else None, "confusion": dict(sorted(conf.items()))}


# --- 공용 적재 -------------------------------------------------------------------------------------------

def parse_all(meta: Sequence[Mapping[str, Any]], bodies: Mapping[str, str],
              aliases: Mapping[str, str]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """본문이 있는 기억마다 언급 파싱 + 메타 keys 와의 차이 수."""
    by_id = {str(m["id"]): m for m in meta}
    parsed, mism, extra, lack = {}, 0, 0, 0
    for mid, text in bodies.items():
        m = by_id.get(mid)
        if m is None:
            raise Fail(f"body {mid} not in meta")
        p = memory_mentions(mid, text, m.get("tags") or [], aliases)
        parsed[mid] = p
        mk = set(m.get("keys") or [])
        if set(p["keys"]) != mk:
            mism += 1
            extra += len(set(p["keys"]) - mk)
            lack += len(mk - set(p["keys"]))
    return parsed, {"memories": len(parsed), "key_mismatch_memories": mism, "keys_only_in_sentences": extra,
                    "keys_only_in_meta": lack}


def _aliases(args) -> Dict[str, str]:
    from bench import r5_metrics as R
    return R.aliases_from(args)


def _load_parsed(args) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]], Dict[str, Any]]:
    doc = read_json(args.mentions)
    meta = read_jsonl(args.meta)
    if doc.get("meta_sha256") != sha256_file(args.meta):
        raise Fail("mentions file was built from another meta")
    return meta, doc["parsed"], doc


def states_for(variant: str, t: str, meta, parsed, tag_aliases, llm_doc=None,
               mentions_sha256: Optional[str] = None) -> Dict[str, Dict[str, str]]:
    """변형별 키 상태. o3c_llm 은 LLM 역할 파일이 지금 언급 파일(sha256)에서 만든 것이고 항목 수가 같을 때만 쓴다
    (역할은 언급 순번으로 매핑되므로 다른 언급 파일의 역할이 섞이면 조용히 오염된다, final-review #372 Codex P2)."""
    by_id = {str(m["id"]): m for m in meta}
    if variant == "o3c":
        return role_states(memory_roles(m for p in parsed.values() for m in p["mentions"]), by_id, t)
    if variant == "o3c_llm":
        if llm_doc is None:
            raise Fail("o3c_llm needs --llm-roles")
        if not mentions_sha256 or llm_doc.get("mentions_sha256") != mentions_sha256:
            raise Fail("llm roles file was built from another mentions file (mentions_sha256 differs)")
        items = role_items(parsed)
        if llm_doc.get("n_items") != len(items) or llm_doc.get("stopped"):
            raise Fail("llm roles file does not cover the current mention list")
        role_of = llm_role_lookup(items, llm_doc)
        return role_states(memory_roles((m for p in parsed.values() for m in p["mentions"]), role_of), by_id, t)
    return o3_variant_states(meta, t, tag_aliases, variant_pred(variant, tag_aliases, parsed))


def spent_total(spent_dir: Optional[str]) -> Tuple[float, Dict[str, float]]:
    """--spent-dir 아래 d-llm 출력(gen/*-gen.json)과 LLM 역할 판정(roles-llm*.json) 비용 합(이 티켓 누계)."""
    from bench import r5_metrics as R
    total, parts = 0.0, Counter()
    if not spent_dir or not os.path.isdir(spent_dir):
        return 0.0, {}
    for root, _dirs, files in os.walk(spent_dir):
        for name in files:
            p = os.path.join(root, name)
            if name.endswith("-gen.json"):
                c = R.gen_cost(read_json(p))["cost_usd"]
                parts["dllm"] += c
            elif name.startswith("roles-llm") and name.endswith(".json"):
                c = float(read_json(p).get("cost_usd") or 0.0)
                parts["roles_llm"] += c
            else:
                continue
            total += c
    return total, dict(parts)


DEFAULT_CALL_USD = 0.30          # 측정된 d-llm 호출이 없을 때 호출당 예상 비용(R5-A2 실측 설정당 약 $3.4 / 18 호출보다 크게)


class SpendGuard:
    """티켓 지출 한도(final-review #372 Spec c / Codex P1).
    total() = `--spent-dir` 아래 d-llm 출력과 LLM 역할 판정 파일 비용 합을 부를 때마다 다시 읽은 값
    (같은 디렉터리를 쓰는 다른 프로세스의 지출 포함) + 이 프로세스가 `--spent-dir` 밖에 쓴 지출.
    preflight(n) = 지금 누계 + n 호출 x 호출당 평균(측정된 d-llm 호출 평균, 없으면 DEFAULT_CALL_USD)이 한도 이하인가."""

    def __init__(self, spent_dir: Optional[str], max_usd: Optional[float], out_dir: Optional[str] = None):
        self.spent_dir = spent_dir
        self.max_usd = max_usd
        self.own_outside = 0.0
        sd = os.path.realpath(spent_dir) if spent_dir else None
        od = os.path.realpath(out_dir) if out_dir else None
        self.out_inside = bool(sd and od and (od == sd or od.startswith(sd + os.sep)))

    def total(self) -> float:
        return spent_total(self.spent_dir)[0] + self.own_outside

    def add_own(self, cost: float) -> None:
        if not self.out_inside:
            self.own_outside += cost or 0.0

    def over(self) -> bool:
        return self.max_usd is not None and self.total() >= self.max_usd

    def per_call(self) -> Tuple[float, int]:
        from bench import r5_metrics as R
        cost = calls = 0
        if self.spent_dir and os.path.isdir(self.spent_dir):
            for root, _dirs, files in os.walk(self.spent_dir):
                for name in files:
                    if name.endswith("-gen.json"):
                        c = R.gen_cost(read_json(os.path.join(root, name)))
                        cost += c["cost_usd"]
                        calls += c["calls"]
        return ((cost / calls) if calls else DEFAULT_CALL_USD), calls

    def preflight(self, n_calls: int) -> Dict[str, Any]:
        avg, measured = self.per_call()
        now = self.total()
        est = n_calls * avg
        ok = self.max_usd is None or now + est <= self.max_usd
        return {"ok": ok, "spent_usd": round(now, 4), "calls": n_calls, "per_call_usd": round(avg, 4),
                "measured_calls": measured, "estimate_usd": round(est, 4), "max_usd": self.max_usd}


def run_setting(jobs: Sequence[Mapping[str, Any]], caller: Callable[[str], Dict[str, Any]], guard: SpendGuard,
                k: int, generate: Optional[Callable] = None, write: Optional[Callable[[str, Any], Any]] = None,
                now: Optional[Callable[[], str]] = None) -> int:
    """설정 하나(t 마다 job = {t, out, head, prompt})를 K 반복. 시작 전 preflight, 호출 전마다 누계를 다시 읽어 한도면 exit 3."""
    if generate is None or write is None or now is None:
        from bench import d_llm as D
        from bench import r5_metrics as R
        generate = generate or D.generate_one
        write = write or R.write_private
        now = now or D.now_kst
    pf = guard.preflight(len(jobs) * k)
    print(json.dumps({"preflight": pf}))
    if not pf["ok"]:
        print(f"stop: spent {pf['spent_usd']:.3f} + estimate {pf['estimate_usd']:.3f} > max-usd {guard.max_usd}; "
              "setting not started", file=sys.stderr)
        return 3
    for job in jobs:
        gens: List[Dict[str, Any]] = []
        for r in range(k):
            if guard.over():
                print(f"stop: spent {guard.total():.3f} >= max-usd {guard.max_usd} at {job['t']} rep {r + 1}",
                      file=sys.stderr)
                return 3
            g = generate(job["prompt"], caller)
            gens.append(g)
            for c in g.get("calls") or []:
                guard.add_own(c.get("cost_usd") or 0.0)
                print(json.dumps({"t": job["t"], "rep": r + 1, **{x: v for x, v in c.items() if x != "raw_result"}},
                                 ensure_ascii=False))
            write(job["out"], dict(job["head"], generations=gens))
        write(job["out"], dict(job["head"], generations=gens, finished_kst=now()))
        print(json.dumps({"t": job["t"], "spent_usd_cum": round(guard.total(), 4), "out": os.path.basename(job["out"])}))
    return 0


# --- 명령 -------------------------------------------------------------------------------------------------

def cmd_mentions(args) -> int:
    meta = read_jsonl(args.meta)
    bodies = load_bodies(args.bodies)
    aliases = _aliases(args)
    parsed, km = parse_all(meta, bodies, aliases)
    allm = [m for p in parsed.values() for m in p["mentions"]]
    dist = dict(Counter(m["role"] for m in allm if m.get("sent") is not None))
    mem_roles = memory_roles(allm)
    out = {"meta_sha256": sha256_file(args.meta), "bodies_sha256": sha256_file(args.bodies), "key_check": km,
           "mentions": len(allm), "item_tag_only": sum(1 for m in allm if m.get("sent") is None),
           "rule_role_dist": dist,
           "memory_key_role_dist": dict(Counter(r for ks in mem_roles.values() for r in ks.values())),
           "parsed": parsed}
    sha = write_private_json(args.out, out)
    print(json.dumps({k: out[k] for k in ("key_check", "mentions", "item_tag_only", "rule_role_dist",
                                           "memory_key_role_dist")}, ensure_ascii=False))
    print(json.dumps({"out_sha256": sha}))
    return 0


def cmd_llm_roles(args) -> int:
    from bench import d_llm as D
    doc = read_json(args.mentions)
    items = role_items(doc["parsed"])
    if args.limit:
        items = items[:args.limit]
    if os.path.exists(args.out):
        raise Fail(f"{args.out} exists")
    base, parts = spent_total(args.spent_dir)
    print(json.dumps({"items": len(items), "spent_before_usd": round(base, 4), "parts": parts}))

    def budget(spent):                   # 호출마다 --spent-dir 누계를 다시 읽는다(역할 파일은 끝에만 쓰므로 자기 지출은 더한다)
        return args.max_usd is None or spent_total(args.spent_dir)[0] + spent < args.max_usd
    res = llm_roles(items, D.claude_caller(ROLE_MODEL), batch=args.batch, budget=budget)
    res.update(model=ROLE_MODEL, mentions_sha256=sha256_file(args.mentions), limit=args.limit,
               prompt_sha256=hashlib.sha256(ROLE_PROMPT.encode()).hexdigest())
    if not args.limit:
        rule = [it["rule"] for it in items]
        llm = [(res["roles"].get(str(it["i"])) or REF) for it in items]
        res["agreement_rule_vs_llm"] = agreement(rule, llm)
        res["llm_role_dist"] = dict(Counter(llm))
    sha = write_private_json(args.out, res)
    print(json.dumps({k: res.get(k) for k in ("n_items", "fallback_ref", "stopped", "cost_usd",
                                               "agreement_rule_vs_llm", "llm_role_dist")}, ensure_ascii=False))
    print(json.dumps({"calls": len(res["calls"]), "out_sha256": sha}))
    return 0 if not res["stopped"] else 3


def _table_ctx(args):
    from bench import r5_metrics as R
    from bench import r5_oracle as O
    labelset = read_jsonl(args.labelset)
    R.check_not_sealed(labelset, args.t)
    if any(_day(t) > DEV_LAST_T for t in args.t):
        raise Fail("t beyond the body cutoff")
    meta, parsed, _doc = _load_parsed(args)
    llm_doc = read_json(args.llm_roles) if getattr(args, "llm_roles", None) else None
    tag_aliases = O.load_tag_aliases(args.tag_aliases)
    scopes = R.query_projects(args.plan, args.families)
    return labelset, meta, parsed, llm_doc, tag_aliases, scopes


def _input_rows_text(D, R, docs, t, max_chars):
    rows, st = R.t_input(docs[t], t)
    asm = D.assemble({"runs": [{"q": 1, "max_tokens": 0, "results": rows}]}, max_input_chars=max_chars)
    return rows, st, asm, "\n".join(D.render_row(r) for r in asm["rows"])


def cmd_tables(args) -> int:
    """표 크기·토큰(LLM 없음)과 표 상태만으로 본 라벨 쌍 P·R(채점 참고값). 변형 x 형식 x t."""
    from bench import d_llm as D
    from bench import r5_metrics as R
    labelset, meta, parsed, llm_doc, tag_aliases, scopes = _table_ctx(args)
    counter = D.tiktoken_counter()
    if counter is None:
        raise Fail("tiktoken is required (uv run --with tiktoken)")
    pos, ctl = R.dev_pairs(labelset)
    docs = R.load_recalls(args.recalls, args.t)
    msha = sha256_file(args.mentions)
    variants = [v for v in VARIANTS if v != "o3c_llm" or llm_doc is not None]
    out: Dict[str, Any] = {"t": args.t, "variants": {}, "input_tokens": {}}
    for t in args.t:
        _rows, _st, asm, text = _input_rows_text(D, R, docs, t, args.max_input_chars)
        out["input_tokens"][t] = {"tokens": counter(text), "chars": len(text), "rows": len(asm["rows"])}
    for v in variants:
        for form in FORMS:
            per_t, opened = {}, set()
            for t in args.t:
                states = states_for(v, t, meta, parsed, tag_aliases, llm_doc, mentions_sha256=msha)
                rows = table_rows(states, scopes.get(t, set()), t, form)
                txt = render_table(rows, t, form)
                tok = counter(txt)
                per = Counter(r["key"].rpartition("#")[0] for r in rows)
                per_t[t] = {"keys": len(rows), "state": dict(Counter(r["state"] for r in rows)), "chars": len(txt),
                            "tokens": tok, "ratio": tok / out["input_tokens"][t]["tokens"],
                            "keys_per_project": sorted(per.values(), reverse=True),
                            "all_states_in_scope": dict(Counter(s["state"] for k, s in states.items()
                                                                if k.rpartition("#")[0] in scopes.get(t, set())))}
                opened |= {(r["key"], t) for r in rows if r["state"] == S_OPEN}
            sp = {p for p in pos if p[1] in args.t}
            sc = {p for p in ctl if p[1] in args.t}
            tp = len(opened & sc)
            out["variants"][f"{v}-{form}"] = {
                "per_t": per_t, "max_ratio": max(x["ratio"] for x in per_t.values()),
                "table_only_scoring": {"open_labeled": len(opened & (sp | sc)), "open_control": tp,
                                       "P": tp / len(opened & (sp | sc)) if opened & (sp | sc) else None,
                                       "R_all": tp / len(sc) if sc else None}}
    sha = write_private_json(args.out, out)
    brief = {k: {"max_ratio": round(v["max_ratio"], 4), "table_only": v["table_only_scoring"],
                 "keys": [v["per_t"][t]["keys"] for t in args.t]} for k, v in out["variants"].items()}
    print(json.dumps(brief, ensure_ascii=False))
    print(json.dumps({"out_sha256": sha}))
    return 0


def cmd_dllm(args) -> int:
    from bench import d_llm as D
    from bench import r5_metrics as R
    labelset, meta, parsed, llm_doc, tag_aliases, scopes = _table_ctx(args)
    aliases = _aliases(args)
    counter = D.tiktoken_counter()
    template = D.load_prompt(D.BASELINE_PROMPT)
    docs = R.load_recalls(args.recalls, args.t)
    guard = SpendGuard(args.spent_dir, args.max_usd, args.out_dir)
    print(json.dumps({"spent_before_usd": round(guard.total(), 4), "parts": spent_total(args.spent_dir)[1]}))
    msha = sha256_file(args.mentions)
    jobs = []
    for t in args.t:
        out = os.path.join(args.out_dir, f"{args.label}-{t}-gen.json")
        if os.path.exists(out):
            prev = read_json(out)
            if not R.gen_complete(prev, args.k):
                print(f"refuse: incomplete {os.path.basename(out)}; re-run under a new --label", file=sys.stderr)
                return 1
            want = oracle_name(args.variant, args.form, getattr(args, "table_fmt", "base") or "base", args.o1c)
            if prev.get("oracle") != want:          # 같은 라벨의 다른 설정 출력을 이 설정으로 건너뛰지 않는다
                print(f"refuse: {os.path.basename(out)} oracle {prev.get('oracle')} != requested {want}; "
                      "use a new --label", file=sys.stderr)
                return 1
            print(json.dumps({"t": t, "skip": "exists"}))
            continue
        rows, st, asm0, text0 = _input_rows_text(D, R, docs, t, args.max_input_chars)
        states = states_for(args.variant, t, meta, parsed, tag_aliases, llm_doc, mentions_sha256=msha)
        tstats: Dict[str, Any] = {}
        if args.o1c:
            rows, tstats = o1c(rows, states, lambda r: R.row_keys(r, aliases))
        asm = D.assemble({"runs": [{"q": 1, "max_tokens": 0, "results": rows}]}, max_input_chars=args.max_input_chars)
        fmt = getattr(args, "table_fmt", "base") or "base"
        trows = table_rows_fmt(states, scopes.get(t, set()), t, args.form, fmt)
        table = render_table_fmt(trows, t, args.form, fmt)
        in_tok = counter(text0) if counter else None
        tok = counter(table) if counter else None
        tstats = dict(tstats, table={"keys": len(trows), "state": dict(Counter(r["state"] for r in trows)),
                                     "chars": len(table), "tokens": tok, "input_tokens": in_tok,
                                     "ratio": (tok / in_tok) if tok and in_tok else None},
                      table_sha256=hashlib.sha256(table.encode()).hexdigest())
        prompt = R.build_prompt_with_table(D, template, asm, t, table)
        head = {"label": args.label, "t": t, "oracle": oracle_name(args.variant, args.form, fmt, args.o1c),
                "model": D.MODEL, "k": args.k, "recall_sha256": sha256_file(R.recall_path(args.recalls, t)),
                "prompt_sha256": sha256_file(D.BASELINE_PROMPT),
                "prompt_text_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "filter": st, "oracle_stats": tstats,
                "input_rows": len(asm["rows"]), "truncated_rows": asm["truncated_rows"], "input_chars": asm["chars"],
                "input_ids": [r["id"] for r in asm["rows"]], "max_input_chars": args.max_input_chars,
                "mentions_sha256": msha, "started_kst": D.now_kst()}
        if fmt != "base":                       # 기본 형식 머리는 #372 와 같은 키 집합
            head["table_fmt"] = fmt
        print(json.dumps({k: v for k, v in head.items() if k != "input_ids"}, ensure_ascii=False))
        jobs.append({"t": t, "out": out, "head": head, "prompt": prompt})
    if not jobs:
        return 0
    return run_setting(jobs, D.claude_caller(D.MODEL), guard, args.k)


def rbank_split(states: Mapping[Tuple[str, str], Optional[str]], pair_states: Mapping[Tuple[str, str], str],
                controls: Iterable[Tuple[str, str]]) -> Dict[str, Any]:
    """R_bank: 분모 = 대조 쌍 중 뱅크 상태(o3c 판본) 열림. 끝남 = 라벨-뱅크 불일치, 미정·없음 = 뱅크 언급 없음."""
    ctl = sorted(controls)
    den = [p for p in ctl if states.get(p) == S_OPEN]
    num = sum(1 for p in den if pair_states.get(p) == "open")
    return {"R_bank": (num / len(den)) if den else None, "R_bank_num": num, "R_bank_den": len(den),
            "label_bank_mismatch": sum(1 for p in ctl if states.get(p) == S_DONE),
            "no_bank_mention": sum(1 for p in ctl if states.get(p) not in (S_OPEN, S_DONE)),
            "undetermined": sum(1 for p in ctl if states.get(p) == S_UNDET)}


def cmd_judge(args) -> int:
    from bench import r5_metrics as R
    labelset = read_jsonl(args.labelset)
    aliases = _aliases(args)
    ts = sorted(args.t)
    R.check_not_sealed(labelset, ts)
    meta, parsed, _ = _load_parsed(args)
    llm_doc = read_json(args.llm_roles)
    msha = sha256_file(args.mentions)
    pos, ctl = R.dev_pairs(labelset)
    spos = {p for p in pos if p[1] in ts}
    sctl = {p for p in ctl if p[1] in ts}
    docs = R.load_recalls(args.recalls, ts)
    present = set()
    for t in ts:
        rows, _ = R.t_input(docs[t], t)
        for k, s in R.t_signals(rows, R.keys_at(pos | ctl, t), aliases).items():
            if s["present"]:
                present.add((k, t))
    bank = {}
    for v in ("o3c", "o3c_llm"):
        st = {}
        for t in ts:
            s = states_for(v, t, meta, parsed, {}, llm_doc, mentions_sha256=msha)
            for k in R.keys_at(sctl, t):
                st[(k, t)] = (s.get(k) or {}).get("state")
        bank[v] = st
    out: Dict[str, Any] = {"t": ts, "pairs": {"positive": len(spos), "control": len(sctl)},
                           "present": {"positive": len(spos & present), "control": len(sctl & present)},
                           "base": [], "settings": {},
                           "bank_states": {v: dict(Counter(x or "none" for x in st.values())) for v, st in bank.items()}}
    base_states = []
    for lab in args.base:
        st, info = R.judge_label(args.base_gen_dir, lab, ts, spos, sctl, aliases)
        base_states.append(st)
        out["base"].append({"label": lab, "pr": R.pr_summary(st, spos, sctl, present),
                            "r_bank": {v: rbank_split(bank[v], st, sctl) for v in bank},
                            "cost_usd": info["cost"].get("cost_usd")})
    base_r = [b["pr"]["R"] for b in out["base"]]
    for spec in args.setting or []:
        name, _, lab = spec.partition("=")
        st, info = R.judge_label(args.gen_dir, lab, ts, spos, sctl, aliases)
        pr = R.pr_summary(st, spos, sctl, present)
        ratios = [(info["per_t"][t].get("oracle_stats") or {}).get("table", {}).get("ratio") for t in ts]
        out["settings"][name] = {
            "label": lab, "pr": pr, "r_bank": {v: rbank_split(bank[v], st, sctl) for v in bank},
            "dR_vs_base1": pr["R"] - base_r[0] if pr["R"] is not None and base_r[0] is not None else None,
            "dR_vs_bases": [pr["R"] - b for b in base_r if b is not None and pr["R"] is not None],
            "dP_vs_base1": (pr["P"] - out["base"][0]["pr"]["P"]) if pr["P"] is not None else None,
            "paired_P_vs_base1": R.cluster_bootstrap(R.pr_values(st, spos, sctl)["P"],
                                                     R.pr_values(base_states[0], spos, sctl)["P"]),
            "paired_R_vs_base1": R.cluster_bootstrap(R.pr_values(st, spos, sctl, present)["R"],
                                                     R.pr_values(base_states[0], spos, sctl, present)["R"]),
            "g1": R.dllm_rates(st, spos, present),
            "table_ratio_per_t": dict(zip(ts, ratios)),
            "table_ratio_max": max((x for x in ratios if x is not None), default=None),
            "per_t": {t: {k: v for k, v in info["per_t"][t].items() if k in ("invalid_reps", "empty_reps",
                                                                                "open_items", "open_unmapped")}
                      for t in ts},
            "cost": info["cost"]}
    sha = write_private_json(args.out, out)
    brief = {"present": out["present"], "bank_states": out["bank_states"],
             "base": [{"label": b["label"], "P": b["pr"]["P"], "R": b["pr"]["R"], "R_all": b["pr"]["R_all"],
                       "R_bank_llm": b["r_bank"]["o3c_llm"]["R_bank"], "R_bank_rule": b["r_bank"]["o3c"]["R_bank"]}
                      for b in out["base"]],
             "settings": {k: {"P": v["pr"]["P"], "P_frac": f"{v['pr']['P_num']}/{v['pr']['P_den']}",
                              "R": v["pr"]["R"], "R_frac": f"{v['pr']['R_num']}/{v['pr']['R_den']}",
                              "R_all": v["pr"]["R_all"], "R_bank_llm": v["r_bank"]["o3c_llm"],
                              "R_bank_rule": v["r_bank"]["o3c"], "dR_vs_base1": v["dR_vs_base1"],
                              "dR_vs_bases_minmax": [min(v["dR_vs_bases"]), max(v["dR_vs_bases"])] if v["dR_vs_bases"] else None,
                              "table_ratio_max": v["table_ratio_max"], "cost_usd": v["cost"].get("cost_usd")}
                          for k, v in out["settings"].items()}}
    print(json.dumps(brief, ensure_ascii=False))
    print(json.dumps({"out_sha256": sha}))
    return 0


def cmd_sample(args) -> int:
    doc = read_json(args.mentions)
    items = role_items(doc["parsed"])
    llm = read_json(args.llm_roles)
    rng = random.Random(args.seed)
    pick = sorted(rng.sample(range(len(items)), min(args.n, len(items))))
    out = {"seed": args.seed, "n": len(pick), "mentions_sha256": sha256_file(args.mentions),
           "items": [{"s": f"S{j + 1:02d}", "i": items[x]["i"], "key": items[x]["key"], "sentence": items[x]["sentence"],
                      "rule": items[x]["rule"], "llm": (llm.get("roles") or {}).get(str(items[x]["i"]), REF)}
                     for j, x in enumerate(pick)]}
    sha = write_private_json(args.out, out)
    print(json.dumps({"n": out["n"], "out_sha256": sha}))
    return 0


def cmd_agree(args) -> int:
    smp = read_json(args.sample)
    ver = read_json(args.verdicts)                       # {"S01": "open", ...} 판독자(워커) 직접 판독
    items = [it for it in smp["items"] if it["s"] in ver]
    human = [ver[it["s"]] for it in items]
    out = {"n": len(items), "reader_vs_rule": agreement(human, [it["rule"] for it in items]),
           "reader_vs_llm": agreement(human, [it["llm"] for it in items]),
           "rule_vs_llm": agreement([it["rule"] for it in items], [it["llm"] for it in items]),
           "reader_dist": dict(Counter(human)), "sample_sha256": sha256_file(args.sample),
           "verdicts_sha256": sha256_file(args.verdicts)}
    sha = write_private_json(args.out, out)
    print(json.dumps({k: v for k, v in out.items() if not k.endswith("sha256")}, ensure_ascii=False))
    print(json.dumps({"out_sha256": sha}))
    return 0


# --- R5-A4 저장 역할 원장 오프라인 확인(issue #374) ---------------------------------------------------------
# 위 함수와 기본 출력은 그대로 두고 추가만 한다. 표 형식 변형은 열림만 표(form=open)에만 적용한다.
#   base = #372 열림만 표(render_table 그대로, 바이트 동일)
#   a    = 최소 행 `키 | 상태`                 b = 날짜만 남김 `키 | 상태 | 날짜`(근거 id 생략)
#   c    = 최근 끝남 줄 제외(열림 키만, N=0)   a+c, b+c = 조합
# 머리말은 형식의 열 이름(`줄: ...`)에 맞추고, c 계열은 '와 최근 N일 안에 끝난 키' 구만 뺀다(코디네이터 결정, 나머지 문구 동일).
# 추가 토큰 = (표 토큰 + o1c 주석으로 늘어난 입력 토큰) / 그 t 의 필터 뒤 d-llm 입력 토큰(o1c 없으면 표 토큰과 같다).
# 사유 분해(reasons.json, 리포 밖 0600): 항목 = {setting, t, key_code, side(P|R), reason, evidence}.
#   판정 순서(처음 맞는 사유 하나) (ii) -> (i) -> (iii) -> (iv), `classify_reason` 이 evidence 로 다시 계산해 대조한다.
#   evidence.has_side_mention = 직접 판독 기준 t 이전 그 키의 끝냄(P) / 열림(R) 언급이 있는가
#   evidence.reader_state / llm_state = 직접 판독 역할 / LLM 역할로 본 t 이전 마지막 비참조 언급의 상태(열림|끝남|미정)
#   (ii) 언급 없음 / (i) LLM 역할로 본 상태가 판독 상태와 다름 / (iii) 판독 상태가 라벨과 반대(P: 끝남 아님, R: 열림 아님) /
#   (iv) 그 밖(입력은 맞는데 d-llm 출력이 틀림).
# P_bank = open 으로 나온 대조 쌍 / (open 으로 나온 라벨 쌍 - (ii)·(iii) P 오답 양성 쌍),
# R_bank2 = R 분자 / (R 분모 - (ii)·(iii) R 누락 쌍). R_bank 노이즈 바닥 = base1~5 의 R_bank(o3c-llm 분모) 반복 짝 군집 재표집.

TABLE_FMTS = ("base", "a", "b", "c", "a+c", "b+c")
_FMT_COLS = {"base": ("키", "상태", "날짜", "근거 id"), "a": ("키", "상태"), "b": ("키", "상태", "날짜"),
             "c": ("키", "상태", "날짜", "근거 id"), "a+c": ("키", "상태"), "b+c": ("키", "상태", "날짜")}
_COL_FIELD = {"키": "key", "상태": "state", "날짜": "date", "근거 id": "id"}
REASONS = ("ii", "i", "iii", "iv")            # 판정 순서
SIDES = ("P", "R")


def _check_fmt(fmt: str, form: str) -> None:
    if fmt not in TABLE_FMTS:
        raise Fail(f"unknown table fmt {fmt}")
    if fmt != "base" and form != "open":
        raise Fail("table fmt variants apply to the open-only table only")


def fmt_n_days(fmt: str) -> int:
    return 0 if fmt.endswith("c") else RECENT_DONE_DAYS


def table_rows_fmt(states: Mapping[str, Mapping[str, str]], scope: Iterable[str], t: Any, form: str,
                   fmt: str = "base") -> List[Dict[str, str]]:
    """형식별 표 줄. c 계열은 N=0(끝남 날짜 >= t+1 인 키가 없으므로 열림 키만)."""
    _check_fmt(fmt, form)
    return table_rows(states, scope, t, form, n_days=fmt_n_days(fmt))


def render_table_fmt(rows: Sequence[Mapping[str, str]], t: Any, form: str, fmt: str = "base") -> str:
    _check_fmt(fmt, form)
    if fmt == "base":
        return render_table(rows, t, form)
    cols = _FMT_COLS[fmt]
    what = "열린 작업 키" if fmt.endswith("c") else f"열린 작업 키와 최근 {RECENT_DONE_DAYS}일 안에 끝난 키"
    head = (f"<item_status_table> 뱅크 기억 기준 {what}(기준일 {t} 이전 기억만). "
            "여기 없는 키는 뱅크 기억 기준 열림이 아니거나 질의 범위 밖이다. 줄: " + " | ".join(cols))
    body = [" | ".join(r[_COL_FIELD[c]] for c in cols) for r in rows]
    return "\n".join([head] + body + ["</item_status_table>"])


def table_head(text: str) -> str:
    return text.split("\n", 1)[0]


def extra_ratio(table_tokens: int, rows_tokens: int, rows_o1c_tokens: Optional[int]) -> float:
    """추가 토큰 = (표 토큰 + o1c 로 늘어난 입력 토큰) / 필터 뒤 d-llm 입력 토큰."""
    if not rows_tokens:
        raise Fail("input tokens must be positive")
    add = (rows_o1c_tokens - rows_tokens) if rows_o1c_tokens is not None else 0
    return (table_tokens + add) / rows_tokens


_ORACLE_RE = re.compile(r"^(?P<v>[a-z0-9_]+)-(?P<form>open|full)(?:\[(?P<fmt>[a-z+]+)\])?(?P<o1c>\+o1c)?$")


def oracle_name(variant: str, form: str, fmt: str = "base", o1c_on: bool = False) -> str:
    """gen 머리의 oracle 문자열. base 형식은 #372 와 같은 문자열."""
    return f"{variant}-{form}" + (f"[{fmt}]" if fmt != "base" else "") + ("+o1c" if o1c_on else "")


def parse_oracle(name: Optional[str]) -> Optional[Dict[str, Any]]:
    if not name:
        return None
    m = _ORACLE_RE.match(name)
    if not m:
        raise Fail(f"unknown oracle spec {name}")
    return {"variant": m["v"], "form": m["form"], "fmt": m["fmt"] or "base", "o1c": bool(m["o1c"])}


def key_code(key: str) -> str:
    """키 코드(리포 밖 사유 파일·리포 산출물 공용). 키 이름 대신 sha256 앞 10자."""
    return "k" + hashlib.sha256(key.encode()).hexdigest()[:10]


def classify_reason(side: str, has_side_mention: bool, reader_state: Optional[str], llm_state: Optional[str]) -> str:
    """판정 순서 (ii) -> (i) -> (iii) -> (iv). want = 출력이 맞으려면 뱅크가 말해야 할 상태(P: 끝남, R: 열림)."""
    if side not in SIDES:
        raise Fail(f"unknown side {side}")
    want = S_DONE if side == "P" else S_OPEN
    if not has_side_mention:
        return "ii"
    if llm_state != reader_state:            # LLM 역할로 본 상태가 직접 판독과 다르면 (iii) 보다 먼저 (i)
        return "i"
    if reader_state != want:
        return "iii"
    return "iv"


def load_reasons(path: str) -> List[Dict[str, Any]]:
    """사유 파일을 읽고 항목마다 판정 순서를 다시 계산해 대조한다. 같은 (t, 키, 쪽)의 사유가 갈리면 거부."""
    doc = read_json(path)
    items = doc["items"] if isinstance(doc, dict) else doc
    seen: Dict[Tuple[str, str, str], str] = {}
    for it in items:
        ev = it.get("evidence")
        tag = f"{it.get('setting')}/{it.get('t')}/{it.get('key_code')}"
        if it.get("side") not in SIDES or it.get("reason") not in REASONS:
            raise Fail(f"bad reason item {tag}")
        # 근거 칸이 빠지거나 어긋나면 거부한다(빠진 칸을 '언급 없음' 으로 읽으면 (ii) 가 근거 없이 통과해 분모가 준다)
        if not isinstance(ev, dict) or not isinstance(ev.get("has_side_mention"), bool) or "llm_state" not in ev \
                or ev.get("reader_state") not in (S_OPEN, S_DONE, S_UNDET) \
                or (ev["has_side_mention"] and ev["reader_state"] == S_UNDET):
            raise Fail(f"bad reason evidence {tag}")
        want = classify_reason(it["side"], ev["has_side_mention"], ev["reader_state"], ev["llm_state"])
        if want != it["reason"]:
            raise Fail(f"reason order violated {it['setting']}/{it['t']}/{it['key_code']}/{it['side']}: "
                       f"{it['reason']} != {want}")
        k = (it["t"], it["key_code"], it["side"])
        if seen.setdefault(k, it["reason"]) != it["reason"]:
            raise Fail(f"conflicting reasons for {k}")
    return list(items)


def reason_index(items: Iterable[Mapping[str, Any]]) -> Dict[Tuple[str, str, str], str]:
    return {(it["t"], it["key_code"], it["side"]): it["reason"] for it in items}


def error_pairs(pair_states: Mapping[Tuple[str, str], str], positives: Iterable[Tuple[str, str]],
                controls: Iterable[Tuple[str, str]], present: Iterable[Tuple[str, str]]) -> Dict[str, List[Tuple[str, str]]]:
    """P 오답 = open 으로 나온 양성 쌍, R 누락 = 기준선 결과에 나온 대조 쌍 중 open 이 아닌 쌍(R 분모 49 안)."""
    pres = set(present)
    return {"P": sorted(p for p in positives if pair_states.get(p) == "open"),
            "R": sorted(p for p in controls if p in pres and pair_states.get(p) != "open")}


def bank_adjusted(pair_states: Mapping[Tuple[str, str], str], positives: Iterable[Tuple[str, str]],
                  controls: Iterable[Tuple[str, str]], present: Iterable[Tuple[str, str]],
                  reasons: Mapping[Tuple[str, str, str], str]) -> Dict[str, Any]:
    """P_bank·R_bank2. 사유가 없는 오답 쌍이 있으면 거부(분해가 끝나야 값이 있다)."""
    from bench import r5_metrics as R
    pos, ctl, pres = set(positives), set(controls), set(present)
    pr = R.pr_summary(pair_states, pos, ctl, pres)
    errs = error_pairs(pair_states, pos, ctl, pres)
    dist: Dict[str, Counter] = {"P": Counter(), "R": Counter()}
    missing = []
    for side in SIDES:
        for (k, t) in errs[side]:
            r = reasons.get((t, key_code(k), side))
            if r is None:
                missing.append((side, t, key_code(k)))
                continue
            dist[side][r] += 1
    if missing:
        raise Fail(f"{len(missing)} error pairs have no reason (first {missing[0]})")
    p_drop = dist["P"]["ii"] + dist["P"]["iii"]
    r_drop = dist["R"]["ii"] + dist["R"]["iii"]
    p_den = pr["P_den"] - p_drop
    r_den = pr["R_den"] - r_drop
    return {"P": pr["P"], "P_num": pr["P_num"], "P_den": pr["P_den"], "P_bank": (pr["P_num"] / p_den) if p_den else None,
            "P_bank_den": p_den, "R": pr["R"], "R_num": pr["R_num"], "R_den": pr["R_den"],
            "R_bank2": (pr["R_num"] / r_den) if r_den else None, "R_bank2_den": r_den, "R_all": pr["R_all"],
            "P_errors": len(errs["P"]), "R_misses": len(errs["R"]),
            "P_reasons": {r: dist["P"][r] for r in REASONS}, "R_reasons": {r: dist["R"][r] for r in REASONS}}


def rbank_values(pair_states: Mapping[Tuple[str, str], str], bank_states: Mapping[Tuple[str, str], Optional[str]],
                 controls: Iterable[Tuple[str, str]]) -> Dict[Tuple[str, str], int]:
    """R_bank 쌍별 값(분모 = 뱅크 상태 열림인 대조 쌍) - 군집 재표집 입력."""
    return {p: (1 if pair_states.get(p) == "open" else 0) for p in sorted(controls) if bank_states.get(p) == S_OPEN}


def rbank_noise_floor(base_states: Sequence[Mapping[Tuple[str, str], str]], bank_states, controls,
                      seed: int = SEED) -> Dict[str, Any]:
    from bench import r5_metrics as R
    return R.noise_floor([rbank_values(st, bank_states, controls) for st in base_states], seed=seed)


def _prep_t(D, R, docs, t, max_chars, counter):
    rows, st, asm0, text0 = _input_rows_text(D, R, docs, t, max_chars)
    return rows, st, asm0, text0, counter(text0)


def _o1c_tokens(D, R, rows, states, aliases, max_chars, counter):
    rows2, ost = o1c(rows, states, lambda r: R.row_keys(r, aliases))
    asm = D.assemble({"runs": [{"q": 1, "max_tokens": 0, "results": rows2}]}, max_input_chars=max_chars)
    return asm, counter("\n".join(D.render_row(r) for r in asm["rows"])), ost


def cmd_fmt_tables(args) -> int:
    """열림만 표 형식 변형(a)(b)(c)(a+c)(b+c)와 base 의 t별 표 토큰·추가 토큰(o1c 유무), 표 상태만 채점(LLM 없음, 지출 0)."""
    from bench import d_llm as D
    from bench import r5_metrics as R
    labelset, meta, parsed, llm_doc, tag_aliases, scopes = _table_ctx(args)
    aliases = _aliases(args)
    counter = D.tiktoken_counter()
    if counter is None:
        raise Fail("tiktoken is required (uv run --with tiktoken)")
    pos, ctl = R.dev_pairs(labelset)
    docs = R.load_recalls(args.recalls, args.t)
    msha = sha256_file(args.mentions)
    out: Dict[str, Any] = {"t": args.t, "variant": args.variant, "form": "open", "input_tokens": {}, "o1c": {},
                           "fmts": {}}
    st_by_t = {}
    for t in args.t:
        rows, _st, _asm0, _text0, tok0 = _prep_t(D, R, docs, t, args.max_input_chars, counter)
        states = states_for(args.variant, t, meta, parsed, tag_aliases, llm_doc, mentions_sha256=msha)
        st_by_t[t] = states
        _asm, tok1, ost = _o1c_tokens(D, R, rows, states, aliases, args.max_input_chars, counter)
        out["input_tokens"][t] = tok0
        out["o1c"][t] = dict(ost, tokens=tok1, added_tokens=tok1 - tok0, truncated_rows=_asm["truncated_rows"])
    sp = {p for p in pos if p[1] in args.t}
    sc = {p for p in ctl if p[1] in args.t}
    for fmt in TABLE_FMTS:
        per_t, opened, heads = {}, set(), set()
        for t in args.t:
            rows = table_rows_fmt(st_by_t[t], scopes.get(t, set()), t, "open", fmt)
            txt = render_table_fmt(rows, t, "open", fmt)
            heads.add(hashlib.sha256(table_head(txt).replace(str(t), "<t>").encode()).hexdigest())
            tok = counter(txt)
            tok0 = out["input_tokens"][t]
            per_t[t] = {"keys": len(rows), "state": dict(Counter(r["state"] for r in rows)), "chars": len(txt),
                        "tokens": tok, "ratio": tok / tok0,
                        "extra_o1c": extra_ratio(tok, tok0, out["o1c"][t]["tokens"]),
                        "table_sha256": hashlib.sha256(txt.encode()).hexdigest()}
            opened |= {(r["key"], t) for r in rows if r["state"] == S_OPEN}
        tp = len(opened & sc)
        lab = opened & (sp | sc)
        out["fmts"][fmt] = {"per_t": per_t, "max_ratio": max(x["ratio"] for x in per_t.values()),
                            "max_extra_o1c": max(x["extra_o1c"] for x in per_t.values()),
                            "head_sha256": sorted(heads),
                            "table_only_scoring": {"open_labeled": len(lab), "open_control": tp,
                                                   "P": tp / len(lab) if lab else None,
                                                   "R_all": tp / len(sc) if sc else None}}
    ok = {f: v for f, v in out["fmts"].items() if v["max_ratio"] <= args.limit}
    rank = sorted(ok, key=lambda f: (-(ok[f]["table_only_scoring"]["R_all"] or 0.0), ok[f]["max_ratio"]))
    out["limit"] = args.limit
    out["eligible_rank"] = rank
    sha = write_private_json(args.out, out)
    brief = {f: {"max_ratio": round(v["max_ratio"], 4), "max_extra_o1c": round(v["max_extra_o1c"], 4),
                 "keys": [v["per_t"][t]["keys"] for t in args.t],
                 "ratio_t": [round(v["per_t"][t]["ratio"], 4) for t in args.t],
                 "table_only": v["table_only_scoring"], "head_sha256": v["head_sha256"]}
             for f, v in out["fmts"].items()}
    print(json.dumps(brief, ensure_ascii=False))
    print(json.dumps({"o1c_added_tokens": {t: out["o1c"][t]["added_tokens"] for t in args.t},
                      "eligible_rank": rank, "out_sha256": sha}))
    return 0


def _judge_ctx(args):
    from bench import r5_metrics as R
    labelset = read_jsonl(args.labelset)
    aliases = _aliases(args)
    ts = sorted(args.t)
    R.check_not_sealed(labelset, ts)
    if any(_day(t) > DEV_LAST_T for t in ts):
        raise Fail("t beyond the body cutoff")
    meta, parsed, _ = _load_parsed(args)
    llm_doc = read_json(args.llm_roles)
    msha = sha256_file(args.mentions)
    pos, ctl = R.dev_pairs(labelset)
    spos = {p for p in pos if p[1] in ts}
    sctl = {p for p in ctl if p[1] in ts}
    docs = R.load_recalls(args.recalls, ts)
    present = set()
    for t in ts:
        rows, _ = R.t_input(docs[t], t)
        for k, s in R.t_signals(rows, R.keys_at(pos | ctl, t), aliases).items():
            if s["present"]:
                present.add((k, t))
    return labelset, aliases, ts, meta, parsed, llm_doc, msha, spos, sctl, docs, present


def _setting_specs(specs: Sequence[str]) -> List[Tuple[str, str, str]]:
    """`name=<gen-dir>::<label>` -> (name, dir, label)."""
    out = []
    for s in specs or []:
        name, _, rest = s.partition("=")
        d, sep, lab = rest.rpartition("::")
        if not name or not sep or not lab:
            raise Fail(f"bad --setting {s} (want name=<gen-dir>::<label>)")
        out.append((name, d, lab))
    return out


def _mention_evidence(key: str, t: str, parsed, by_id, role_of) -> List[Dict[str, Any]]:
    """t 이전 유효 기억의 그 키 언급(시각 순): 기억 id, 시각, 규칙·LLM 역할, 문장(리포 밖 판독용)."""
    end = t_end(_day(t))
    out = []
    for mid, p in parsed.items():
        at = _valid_at(by_id, mid, end)
        if at is None:
            continue
        ms = [m for m in p["mentions"] if m["key"] == key]
        if not ms:
            continue
        out.append({"mid": mid, "at": at.isoformat(), "label": p.get("label"),
                    "mentions": [{"sent": m.get("sent"), "rule": m["role"], "llm": role_of(m),
                                  "sentence": p["sentences"][m["sent"]] if m.get("sent") is not None else None}
                                 for m in ms]})
    return sorted(out, key=lambda x: (x["at"], x["mid"]))


def cmd_cands(args) -> int:
    """설정마다 P 오답·R 누락 쌍과 판독 근거(리포 밖 0600). 직접 판독으로 reasons.json 을 쓰기 위한 입력."""
    from bench import r5_metrics as R
    labelset, aliases, ts, meta, parsed, llm_doc, msha, spos, sctl, docs, present = _judge_ctx(args)
    by_id = {str(m["id"]): m for m in meta}
    role_of = llm_role_lookup(role_items(parsed), llm_doc)
    states = {t: states_for("o3c_llm", t, meta, parsed, {}, llm_doc, mentions_sha256=msha) for t in ts}
    rule_states = {t: states_for("o3c", t, meta, parsed, {}, llm_doc, mentions_sha256=msha) for t in ts}
    out: Dict[str, Any] = {"t": ts, "settings": {}, "mentions_sha256": msha}
    for name, d, lab in _setting_specs(args.setting):
        st, info = R.judge_label(d, lab, ts, spos, sctl, aliases)
        errs = error_pairs(st, spos, sctl, present)
        items = []
        for side in SIDES:
            for (k, t) in errs[side]:
                in_rows = (k, t) in present
                s = states[t].get(k) or {}
                items.append({"setting": name, "t": t, "key": k, "key_code": key_code(k), "side": side,
                              "in_rows": in_rows, "llm_state": s.get("state"), "llm_state_date": s.get("date"),
                              "llm_state_id": s.get("id"), "rule_state": (rule_states[t].get(k) or {}).get("state"),
                              "evidence": _mention_evidence(k, t, parsed, by_id, role_of)})
        out["settings"][name] = {"label": lab, "P_errors": len(errs["P"]), "R_misses": len(errs["R"]), "items": items}
    sha = write_private_json(args.out, out)
    print(json.dumps({n: {"P_errors": v["P_errors"], "R_misses": v["R_misses"]} for n, v in out["settings"].items()}))
    print(json.dumps({"out_sha256": sha}))
    return 0


def _setting_extra(args, D, R, docs, gen_dir, lab, ts, meta, parsed, tag_aliases, llm_doc, msha, scopes, aliases,
                   counter, template) -> Dict[str, Any]:
    """gen 머리의 oracle 로 입력을 다시 만들고 prompt_text_sha256 대조 뒤 t별 표 토큰·추가 토큰."""
    per_t = {}
    for t in ts:
        gen = read_json(os.path.join(gen_dir, f"{lab}-{t}-gen.json"))
        spec = parse_oracle(gen.get("oracle"))
        rows, _st, asm0, text0, tok0 = _prep_t(D, R, docs, t, args.max_input_chars, counter)
        if spec is None:
            prompt = R.build_prompt_with_table(D, template, asm0, t, "")
            per_t[t] = {"table_tokens": 0, "o1c_added_tokens": 0, "input_tokens": tok0, "table_ratio": 0.0,
                        "extra": 0.0, "prompt_match": hashlib.sha256(prompt.encode()).hexdigest()
                        == gen.get("prompt_text_sha256")}
            continue
        states = states_for(spec["variant"], t, meta, parsed, tag_aliases, llm_doc, mentions_sha256=msha)
        asm, tok1 = asm0, None
        if spec["o1c"]:
            asm, tok1, _ = _o1c_tokens(D, R, rows, states, aliases, args.max_input_chars, counter)
        trows = table_rows_fmt(states, scopes.get(t, set()), t, spec["form"], spec["fmt"])
        table = render_table_fmt(trows, t, spec["form"], spec["fmt"])
        prompt = R.build_prompt_with_table(D, template, asm, t, table)
        ttok = counter(table)
        per_t[t] = {"table_tokens": ttok, "o1c_added_tokens": (tok1 - tok0) if tok1 is not None else 0,
                    "input_tokens": tok0, "table_ratio": ttok / tok0, "extra": extra_ratio(ttok, tok0, tok1),
                    "prompt_match": hashlib.sha256(prompt.encode()).hexdigest() == gen.get("prompt_text_sha256")}
    if not all(v["prompt_match"] for v in per_t.values()):
        raise Fail(f"{lab}: rebuilt prompt differs from the generated one (prompt_text_sha256)")
    return {"per_t": per_t, "extra_max": max(v["extra"] for v in per_t.values()),
            "table_ratio_max": max(v["table_ratio"] for v in per_t.values())}


def cmd_a4_judge(args) -> int:
    """분기 입력: 설정마다 P, P_bank, R, R_bank, R_bank2, 추가 토큰 최대, R_bank 의 base1 대비 변화(노이즈 바닥 대비)."""
    from bench import d_llm as D
    from bench import r5_metrics as R
    from bench import r5_oracle as O
    if len(set(args.base)) < 2:                # 서로 다른 반복 짝이 없으면 바닥이 0 이 되어 어떤 상승도 '바닥 초과' 가 된다
        raise Fail("a4-judge needs at least two distinct --base labels for the R_bank noise floor")
    labelset, aliases, ts, meta, parsed, llm_doc, msha, spos, sctl, docs, present = _judge_ctx(args)
    reasons = reason_index(load_reasons(args.reasons))
    tag_aliases = O.load_tag_aliases(args.tag_aliases)
    scopes = R.query_projects(args.plan, args.families)
    counter = D.tiktoken_counter()
    if counter is None:
        raise Fail("tiktoken is required (uv run --with tiktoken)")
    template = D.load_prompt(D.BASELINE_PROMPT)
    bank: Dict[Tuple[str, str], Optional[str]] = {}
    for t in ts:
        s = states_for("o3c_llm", t, meta, parsed, {}, llm_doc, mentions_sha256=msha)
        for k in R.keys_at(sctl, t):
            bank[(k, t)] = (s.get(k) or {}).get("state")
    base_states = []
    out: Dict[str, Any] = {"t": ts, "reasons_sha256": sha256_file(args.reasons), "base": [], "settings": {}}
    for lab in args.base:
        st, info = R.judge_label(args.base_gen_dir, lab, ts, spos, sctl, aliases)
        base_states.append(st)
    out["rbank_noise_floor"] = rbank_noise_floor(base_states, bank, sctl)
    for lab, st in zip(args.base, base_states):
        rb = rbank_split(bank, st, sctl)
        row = {"label": lab, "R_bank": rb["R_bank"], "R_bank_num": rb["R_bank_num"], "R_bank_den": rb["R_bank_den"]}
        if lab == args.base[0]:
            row.update(bank_adjusted(st, spos, sctl, present, reasons))
        else:
            row["pr"] = R.pr_summary(st, spos, sctl, present)
        out["base"].append(row)
    base1_rbank = out["base"][0]["R_bank"]
    floor = out["rbank_noise_floor"]["floor"]
    for name, d, lab in _setting_specs(args.setting):
        st, info = R.judge_label(d, lab, ts, spos, sctl, aliases)
        adj = bank_adjusted(st, spos, sctl, present, reasons)
        rb = rbank_split(bank, st, sctl)
        ex = _setting_extra(args, D, R, docs, d, lab, ts, meta, parsed, tag_aliases, llm_doc, msha, scopes, aliases,
                            counter, template)
        d_rb = (rb["R_bank"] - base1_rbank) if rb["R_bank"] is not None and base1_rbank is not None else None
        out["settings"][name] = dict(adj, label=lab, R_bank=rb["R_bank"], R_bank_num=rb["R_bank_num"],
                                     R_bank_den=rb["R_bank_den"], dR_bank_vs_base1=d_rb,
                                     dR_bank_over_floor=(d_rb is not None and d_rb > floor),
                                     paired_R_bank_vs_base1=R.cluster_bootstrap(rbank_values(st, bank, sctl),
                                                                                rbank_values(base_states[0], bank, sctl)),
                                     extra=ex, cost=info["cost"])
    sha = write_private_json(args.out, out)
    keys = ("P", "P_bank", "P_bank_den", "R", "R_bank", "R_bank2", "R_bank2_den", "P_reasons", "R_reasons")
    brief = {"rbank_noise_floor": floor,
             "base1": {k: out["base"][0].get(k) for k in keys},
             "settings": {n: dict({k: v.get(k) for k in keys}, extra_max=v["extra"]["extra_max"],
                                  dR_bank_vs_base1=v["dR_bank_vs_base1"], over_floor=v["dR_bank_over_floor"],
                                  cost_usd=v["cost"].get("cost_usd"))
                          for n, v in out["settings"].items()}}
    print(json.dumps(brief, ensure_ascii=False))
    print(json.dumps({"out_sha256": sha}))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="r5_roles.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("bodies")
    p.add_argument("--meta", required=True)
    p.add_argument("--bank", required=True)
    p.add_argument("--out", required=True)
    p = sub.add_parser("mentions")
    p.add_argument("--meta", required=True)
    p.add_argument("--bodies", required=True)
    p.add_argument("--aliases", required=True)
    p.add_argument("--policy")
    p.add_argument("--out", required=True)
    p = sub.add_parser("llm-roles")
    p.add_argument("--mentions", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--limit", type=int)
    p.add_argument("--batch", type=int, default=ROLE_BATCH)
    p.add_argument("--max-usd", type=float)
    p.add_argument("--spent-dir")
    for name in ("tables", "dllm"):
        p = sub.add_parser(name)
        p.add_argument("--labelset", required=True)
        p.add_argument("--aliases", required=True)
        p.add_argument("--policy")
        p.add_argument("--meta", required=True)
        p.add_argument("--mentions", required=True)
        p.add_argument("--llm-roles")
        p.add_argument("--plan", required=True)
        p.add_argument("--families", required=True)
        p.add_argument("--tag-aliases", required=True)
        p.add_argument("--recalls", required=True)
        p.add_argument("--t", action="append", required=True)
        p.add_argument("--max-input-chars", type=int, default=200000)
        p.add_argument("--out" if name == "tables" else "--out-dir", required=True)
        if name == "dllm":
            p.add_argument("--variant", choices=VARIANTS, required=True)
            p.add_argument("--form", choices=FORMS, required=True)
            p.add_argument("--o1c", action="store_true")
            p.add_argument("--k", type=int, default=3)
            p.add_argument("--label", required=True)
            p.add_argument("--max-usd", type=float)
            p.add_argument("--spent-dir")
            p.add_argument("--table-fmt", choices=TABLE_FMTS, default="base")
    p = sub.add_parser("fmt-tables")
    for a in ("--labelset", "--aliases", "--meta", "--mentions", "--llm-roles", "--plan", "--families", "--tag-aliases",
              "--recalls", "--out"):
        p.add_argument(a, required=True)
    p.add_argument("--policy")
    p.add_argument("--t", action="append", required=True)
    p.add_argument("--max-input-chars", type=int, default=200000)
    p.add_argument("--variant", choices=VARIANTS, default="o3c_llm")
    p.add_argument("--limit", type=float, default=0.10)
    for name in ("cands", "a4-judge"):
        p = sub.add_parser(name)
        for a in ("--labelset", "--aliases", "--meta", "--mentions", "--llm-roles", "--recalls", "--out"):
            p.add_argument(a, required=True)
        p.add_argument("--policy")
        p.add_argument("--t", action="append", required=True)
        p.add_argument("--setting", action="append", required=(name == "cands"))
        if name == "a4-judge":
            for a in ("--reasons", "--plan", "--families", "--tag-aliases", "--base-gen-dir"):
                p.add_argument(a, required=True)
            p.add_argument("--base", action="append", required=True)
            p.add_argument("--max-input-chars", type=int, default=200000)
    p = sub.add_parser("judge")
    p.add_argument("--labelset", required=True)
    p.add_argument("--aliases", required=True)
    p.add_argument("--policy")
    p.add_argument("--meta", required=True)
    p.add_argument("--mentions", required=True)
    p.add_argument("--llm-roles", required=True)
    p.add_argument("--recalls", required=True)
    p.add_argument("--t", action="append", required=True)
    p.add_argument("--base-gen-dir", required=True)
    p.add_argument("--base", action="append", required=True)
    p.add_argument("--gen-dir", required=True)
    p.add_argument("--setting", action="append")
    p.add_argument("--out", required=True)
    p = sub.add_parser("sample")
    p.add_argument("--mentions", required=True)
    p.add_argument("--llm-roles", required=True)
    p.add_argument("--n", type=int, default=40)
    p.add_argument("--seed", type=int, default=SEED)
    p.add_argument("--out", required=True)
    p = sub.add_parser("agree")
    p.add_argument("--sample", required=True)
    p.add_argument("--verdicts", required=True)
    p.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    cmds = {"bodies": cmd_bodies, "mentions": cmd_mentions, "llm-roles": cmd_llm_roles, "tables": cmd_tables,
            "dllm": cmd_dllm, "judge": cmd_judge, "sample": cmd_sample, "agree": cmd_agree,
            "fmt-tables": cmd_fmt_tables, "cands": cmd_cands, "a4-judge": cmd_a4_judge}
    try:
        return cmds[args.cmd](args)
    except Exception as e:                 # 이 모듈과 r5_metrics 의 Fail(가드 거부)만 exit 1 로 바꾼다
        if type(e).__name__ != "Fail":
            raise
        print(f"fail: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
