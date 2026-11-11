"""S4 역할 판정기의 순수 함수 - 문장·키 언급 찾기와 사이드 저장소 스키마·쓰기 (issue #376, 상위 #360 "S4·S3 구현").

네트워크·LLM·upstream 호출이 없다(D8). 표준 라이브러리와 같은 패키지의 `item_keys` 만 import 한다.
쓰는 쪽은 호스트 사이드카(`scripts/item_roles_sidecar.py`) 하나뿐이다. 키 상태 계산과 읽기 전용 열기는 S3(#377,
`gateway/item_open_table.py`) 소유라 여기 두지 않는다.

언급 찾기(#372 `r5_roles.py` 의 `split_sentences`·`memory_mentions`·`merge_roles` 와 같은 동작, 게이트웨이는 벤치 하네스를
import 하지 않으므로 옮겨 쓴다. 같은 결과인지는 tests/test_item_roles.py 가 그 모듈과 대조한다):
  * 본문의 펜스 코드 블록을 공백으로 지우고 줄바꿈과 문장 끝(`.`·`!`·`?`·`。` 뒤 공백)에서 문장으로 나눈다.
  * 문장마다 `extract_keys(문장, item: 태그를 뺀 태그, 별칭 표)`로 그 문장의 키 언급을 얻는다.
  * `item:` 태그에서만 나온 키(본문 위치 없음)는 판정 대상이 아니고 역할은 ref 다.
  * 언급마다 규칙 역할(`sentence_role`)도 함께 싣는다(#372 와 같은 출력 모양). 저장 역할은 LLM 판정이다.
  * 한 기억 안 같은 키의 여러 언급은 open > done > ref 로 합친다(`merge_roles`).

사이드 저장소 계약(두 티켓 공통, #376 본문 "사이드 저장소 계약"):
  * sqlite3 한 개, 저널 모드 DELETE(WAL 금지 - 읽기 전용 마운트의 리더가 -wal/-shm 을 만들 수 없다).
  * 공개: 기존 파일을 제자리에서 고치지 않는다. 같은 디렉터리의 새 파일에 쓰고 fsync 한 뒤 os.replace 로 바꾼다(`publish`).
  * 스키마 v1: meta(k, v) - schema_version='1', model, prompt_sha256, aliases_sha256, updated_at /
    memories(memory_id, document_id, mentioned_at, state, text_sha256, judged_at) /
    mention_roles(memory_id, key, role CHECK IN ('done','open','ref')).
  * key = extract_keys 의 (repo, n) 을 `<repo>#<n>` 로 쓴 값.
  * 선택 키(필수 키·스키마 v1 그대로): bank(#376 재작업), judge_mode(#383, `judge_mode()` 값, 없으면 JUDGE_MODE_LEGACY).
  * aliases_sha256 = 소문자 정규화한 별칭 표의 정렬 JSON(`sort_keys`, `ensure_ascii=False`, 구분자 `,`·`:`) UTF-8 sha256.

키 규칙 v2·GitHub 근거 (issue #395, R7-1). 기본은 전부 v1 그대로다(인자를 주지 않으면 출력·파일이 바이트까지 같다).
  * `memory_mentions(..., rules="v2")`: `item_keys.key_hits_v2` 로 언급을 찾는다. 언급마다 `fixed`(업스트림·외부 리포
    문맥의 번호만 키 = 참조 고정, 역할 ref, LLM 판정 대상 아님)와 `bare`(번호만 규칙에서만 나옴)를 더 싣는다.
    `judge_mentions` = LLM 판정 대상 언급(문장 있고 고정 아님). `merge_roles` 는 바꾸지 않는다.
  * 의심 키: `observed_max` = 리포별 관측 최대 번호(명시 repo 표기 `<repo>#n`·`owner/repo#n` 만), `suspect_keys` = 번호만
    언급이 그 상한을 넘는 키(지우지 않고 세기만 한다). 상한 표 JSON·sha256 은 `observed_max_json`·`observed_max_sha256`.
  * meta 선택 키 `key_rules`(v2 저장소만 `v2`, 없으면 v1). 스키마 v1 필수 키·테이블은 그대로다.
  * v2.1(#410): `memory_mentions(..., rules="v2.1")` 은 `item_keys.key_hits_v21` 로 언급을 찾고 v2 와 같은 모양(fixed·bare)을
    싣는다. bare 는 번호만 규칙(project 태그)에서만 나온 언급이다(열거 전파·문장 단일 리포로 붙은 번호는 bare 가 아니다).
    meta `key_rules`=v2.1. `key_hits_for` = 저장소 키 규칙별 언급 목록(게이트웨이 결과 행 키 재계산이 쓴다).
    `observed_max`·`suspect_keys` 는 v2 그대로다(관측 최대 번호는 v2 명시 표기로만 센다).
  * GitHub 근거 선택 테이블(publish 에 github 를 줄 때만 만든다, 없으면 지금 파일과 같다):
    github_items(repo, number, kind issue|pr, state open|closed, state_reason, created_at, closed_at, merged_at, fetched_at),
    github_repos(repo, owner, status ok|missing|error, fetched_at, error_code). repo 는 키의 정규 repo slug 다.
    `github_evidence(repo 행, item 행)` 이 근거 판정 순수 함수다(S3 #396 이 import 한다).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .item_keys import SOURCE_BARE, SOURCE_KNOWN, SOURCE_OWNER, extract_keys, key_hits_v2, key_hits_v21

SCHEMA_VERSION = "1"
DEFAULT_DB_PATH = "/app/policies/item-roles.sqlite"      # 게이트웨이 안 경로(env HC_ITEM_ROLES_DB 기본값, S3 가 읽는다)
DB_ENV = "HC_ITEM_ROLES_DB"
DONE, OPEN, REF = "done", "open", "ref"
ROLES = (DONE, OPEN, REF)
META_KEYS = ("schema_version", "model", "prompt_sha256", "aliases_sha256", "updated_at")
JUDGE_MODE_LEGACY = "input=sentence;votes=1"        # meta judge_mode 가 없는 저장소(#376 판정: 언급 문장 + 키, 1회)
KEY_RULES_V1, KEY_RULES_V2, KEY_RULES_V21 = "v1", "v2", "v2.1"
KEY_RULES = (KEY_RULES_V1, KEY_RULES_V2, KEY_RULES_V21)
META_KEY_RULES = "key_rules"                         # meta 선택 키(#395): v2·v2.1(#410) 저장소만 쓴다, 없으면 v1

# --- 문장·언급(#372 r5_roles.py 와 같은 정규식) ---------------------------------------------------

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


def split_sentences(text: str) -> List[str]:
    masked = _FENCE_RE.sub(" ", text or "")
    return [s.strip() for s in _SENT_SPLIT_RE.split(masked) if s and s.strip()]


def label_of(text: str) -> Optional[str]:
    m = _LABEL_RE.match(text or "")
    return m.group(1) if m else None


def sentence_role(sentence: str, label: Optional[str] = None, is_label_sentence: bool = False) -> str:
    """한 문장의 규칙 역할(#372 와 같음). 열림 표지어가 끝냄보다 먼저다. 표지어가 없으면 라벨 문장만 라벨로 정한다."""
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
    """기억 하나 안에서 한 키의 역할: open 이 이긴다, 아니면 done, 아니면 ref."""
    rs = set(roles)
    return OPEN if OPEN in rs else DONE if DONE in rs else REF


def majority_role(roles: Sequence[str]) -> str:
    """여러 번 판정한 한 항목의 역할(#383 다수결): 엄격한 과반(> 절반)인 역할, 없으면(셋이 다 다르면 등) ref."""
    rs = list(roles)
    for role in ROLES:
        if rs.count(role) * 2 > len(rs):
            return role
    return REF


def judge_mode(input_mode: str, votes: int) -> str:
    """meta 선택 키 judge_mode 값(#383): 판정 입력 방식과 투표 수. 이 키가 없는 옛 저장소는 JUDGE_MODE_LEGACY 다."""
    return "input=%s;votes=%d" % (input_mode, votes)


def key_text(k: Tuple[str, int]) -> str:
    return "%s#%d" % k


def memory_mentions(mid: str, text: str, tags: Sequence[str], aliases: Mapping[str, str],
                    rules: str = KEY_RULES_V1) -> Dict[str, Any]:
    """기억 하나 -> 문장별 키 언급과 규칙 역할. item 태그에서만 나온 키는 ref(문장 없음).

    rules="v2"(#395): 키는 v2 규칙으로 찾고, 언급마다 fixed(참조 고정, 역할 ref)·bare(번호만 규칙에서만) 를 더 싣는다.
    한 문장에서 같은 키가 고정·보통 둘 다로 나오면(명시 표기와 번호만이 함께) 보통 언급 하나로 본다."""
    if rules not in KEY_RULES:
        raise ValueError("unknown key rules %r" % (rules,))
    v2 = rules in (KEY_RULES_V2, KEY_RULES_V21)
    tags = [t for t in (tags or []) if isinstance(t, str)]
    item_tags = [t for t in tags if t.strip().lower().startswith("item:")]
    other_tags = [t for t in tags if t not in item_tags]
    sents = split_sentences(text)
    label = label_of(text)
    mentions: List[Dict[str, Any]] = []
    in_text: Set[str] = set()
    for i, s in enumerate(sents):
        if v2:
            flags: Dict[str, Tuple[bool, bool]] = {}
            for h in key_hits_for(s, other_tags, aliases, rules):
                k = key_text(h.key)
                fixed, bare = flags.get(k, (True, True))
                flags[k] = (fixed and h.ref_fixed, bare and h.source == SOURCE_BARE)
            ks = sorted(flags)
        else:
            ks = sorted(key_text(k) for k in extract_keys(s, other_tags, aliases))
        if not ks:
            continue
        role = sentence_role(s, label, is_label_sentence=(i == 0 and label is not None))
        for k in ks:
            if v2:
                fixed, bare = flags[k]
                mentions.append({"mid": mid, "key": k, "sent": i, "role": REF if fixed else role, "fixed": fixed,
                                 "bare": bare})
            else:
                mentions.append({"mid": mid, "key": k, "sent": i, "role": role})
            in_text.add(k)
    tag_keys = {key_text(k) for k in extract_keys("", item_tags, aliases)}
    for k in sorted(tag_keys - in_text):
        m = {"mid": mid, "key": k, "sent": None, "role": REF, "source": "item_tag"}
        if v2:
            m.update(fixed=False, bare=False)
        mentions.append(m)
    return {"mid": mid, "sentences": sents, "label": label, "mentions": mentions,
            "keys": sorted(in_text | tag_keys)}


def key_hits_for(text: str, tags: Sequence[str], aliases: Mapping[str, str], rules: str):
    """저장소 키 규칙(v2·v2.1)의 언급 목록(`item_keys.key_hits_v2` / `key_hits_v21`). v1 은 언급 목록이 없어 ValueError."""
    if rules == KEY_RULES_V2:
        return key_hits_v2(text, tags, aliases)
    if rules == KEY_RULES_V21:
        return key_hits_v21(text, tags, aliases)
    raise ValueError("no key hits for key rules %r" % (rules,))


def judge_mentions(parsed: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """LLM 판정 대상 언급: 문장이 있고 참조 고정이 아닌 언급(v1 언급은 문장 있는 언급 전부)."""
    return [m for m in parsed["mentions"] if m.get("sent") is not None and not m.get("fixed")]


def key_rules_of(meta: Mapping[str, str]) -> str:
    """저장소 meta 의 키 규칙(선택 키 key_rules, 없으면 v1)."""
    return meta.get(META_KEY_RULES) or KEY_RULES_V1


# --- 의심 키(리포별 관측 최대 번호, #395) ----------------------------------------------------------------

def split_key(key: str) -> Optional[Tuple[str, int]]:
    repo, sep, num = (key or "").rpartition("#")
    if not sep or not repo or not num.isdigit():
        return None
    return repo, int(num)


def observed_max(memories: Iterable[Tuple[str, Sequence[str]]], aliases: Mapping[str, str]) -> Dict[str, int]:
    """(본문, 태그) 묶음 -> {repo: 명시 repo 표기(`<repo>#n`, `owner/repo#n`)로 본 최대 번호}. 태그·번호만 키는 세지 않는다."""
    table: Dict[str, int] = {}
    for text, tags in memories:
        for h in key_hits_v2(text, [t for t in (tags or []) if isinstance(t, str)], aliases):
            if h.source in (SOURCE_KNOWN, SOURCE_OWNER):
                repo, n = h.key
                table[repo] = max(n, table.get(repo, 0))
    return dict(sorted(table.items()))


def observed_max_json(table: Mapping[str, int]) -> str:
    return json.dumps(dict(table), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def observed_max_sha256(table: Mapping[str, int]) -> str:
    return sha256_text(observed_max_json(table))


def suspect_keys(mentions: Iterable[Mapping[str, Any]], table: Mapping[str, int]) -> Set[str]:
    """번호만 규칙 언급(`bare`) 중 그 repo 관측 최대 번호를 넘는 키. 관측이 없는 repo 는 의심하지 않는다. 지우지 않는다."""
    out: Set[str] = set()
    for m in mentions:
        if not m.get("bare"):
            continue
        parsed = split_key(m.get("key"))
        if parsed and parsed[0] in table and parsed[1] > table[parsed[0]]:
            out.add(m["key"])
    return out


def memory_roles(mentions: Iterable[Mapping[str, Any]], role_of=None) -> Dict[str, Dict[str, str]]:
    """언급 목록 -> {기억 id: {키: 기억 단위 역할}}. role_of 를 주면 언급마다 그 역할(LLM 판본)을 쓴다."""
    acc: Dict[str, Dict[str, List[str]]] = defaultdict(lambda: defaultdict(list))
    for m in mentions:
        r = m["role"] if role_of is None else role_of(m)
        acc[m["mid"]][m["key"]].append(r)
    return {mid: {k: merge_roles(rs) for k, rs in ks.items()} for mid, ks in acc.items()}


# --- 해시 ------------------------------------------------------------------------------------------------

def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def normalize_aliases(table: Mapping[str, str]) -> Dict[str, str]:
    """item_keys._table 과 같은 정규화(문자열이 아니거나 빈 항목은 버린다). 키 추출이 실제로 쓰는 표와 sha256 이 같아진다."""
    if not isinstance(table, Mapping):
        return {}
    return {k.strip().lower(): v.strip().lower() for k, v in table.items()
            if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip()}


def aliases_sha256(table: Mapping[str, str]) -> str:
    """별칭 표의 정렬 JSON sha256(소문자 정규화 뒤). S3 도 같은 값으로 meta 와 대조한다."""
    canon = json.dumps(normalize_aliases(table), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- 사이드 저장소 ---------------------------------------------------------------------------------------

class StoreError(Exception):
    """저장소 파일이 계약과 다르다(버전 불일치, 필수 meta 없음 등)."""


SCHEMA_SQL = (
    "CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT NOT NULL)",
    "CREATE TABLE memories(memory_id TEXT PRIMARY KEY, document_id TEXT, mentioned_at TEXT, state TEXT NOT NULL, "
    "text_sha256 TEXT NOT NULL, judged_at TEXT NOT NULL)",
    "CREATE TABLE mention_roles(memory_id TEXT NOT NULL, key TEXT NOT NULL, "
    "role TEXT NOT NULL CHECK(role IN ('done','open','ref')), PRIMARY KEY(memory_id, key))",
)


def create_schema(conn: sqlite3.Connection, meta: Mapping[str, str]) -> None:
    """빈 연결에 계약 v1 스키마와 meta 를 만든다. 저널 모드는 DELETE 로 둔다."""
    conn.execute("PRAGMA journal_mode=DELETE")
    for sql in SCHEMA_SQL:
        conn.execute(sql)
    write_meta(conn, meta)


def write_meta(conn: sqlite3.Connection, meta: Mapping[str, str]) -> None:
    full = dict(meta)
    full["schema_version"] = SCHEMA_VERSION
    missing = [k for k in META_KEYS if not full.get(k)]
    if missing:
        raise StoreError("meta is missing required keys: %s" % ", ".join(missing))
    conn.executemany("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", sorted((k, str(v)) for k, v in full.items()))


def read_meta(conn: sqlite3.Connection) -> Dict[str, str]:
    try:
        rows = conn.execute("SELECT k, v FROM meta").fetchall()
    except sqlite3.DatabaseError as e:
        raise StoreError("not a side store (no readable meta table): %s" % e)
    return {k: v for k, v in rows}


def check_version(conn: sqlite3.Connection) -> Dict[str, str]:
    """기존 파일의 meta 를 읽고 schema_version 이 1 이 아니면 거부한다."""
    meta = read_meta(conn)
    if meta.get("schema_version") != SCHEMA_VERSION:
        raise StoreError("side store schema_version is %r, this code writes %r - refusing"
                         % (meta.get("schema_version"), SCHEMA_VERSION))
    return meta


def load_store(path: str, with_github: bool = False):
    """기존 파일 -> (meta, {memory_id: memories 행}, {memory_id: {key: role}}). 읽기 전용 URI 로 연다.

    with_github=True(#395) 면 4번째로 GitHub 근거 테이블(`load_github` 모양, 테이블이 없으면 None)을 더 돌려준다."""
    # 경로의 %·?·# 가 URI 로 해석되지 않게 퍼센트 인코딩한 file: URI 로 연다
    conn = sqlite3.connect(Path(os.path.abspath(path)).as_uri() + "?mode=ro", uri=True)
    try:
        meta = check_version(conn)
        cols = ("memory_id", "document_id", "mentioned_at", "state", "text_sha256", "judged_at")
        mems = {r[0]: dict(zip(cols, r)) for r in conn.execute("SELECT %s FROM memories" % ", ".join(cols))}
        roles: Dict[str, Dict[str, str]] = defaultdict(dict)
        for mid, key, role in conn.execute("SELECT memory_id, key, role FROM mention_roles"):
            roles[mid][key] = role
        if with_github:
            return meta, mems, dict(roles), _read_github(conn)
        return meta, mems, dict(roles)
    finally:
        conn.close()


def load_github(path: str) -> Optional[Dict[str, Dict[Any, Dict[str, Any]]]]:
    """GitHub 근거 테이블 -> {"repos": {repo: 행}, "items": {(repo, number): 행}}. 테이블이 없으면 None."""
    conn = sqlite3.connect(Path(os.path.abspath(path)).as_uri() + "?mode=ro", uri=True)
    try:
        check_version(conn)
        return _read_github(conn)
    finally:
        conn.close()


def _read_github(conn: sqlite3.Connection) -> Optional[Dict[str, Dict[Any, Dict[str, Any]]]]:
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"github_items", "github_repos"} <= names:
        return None
    repos = {r[0]: dict(zip(GH_REPO_COLS[1:], r[1:]))
             for r in conn.execute("SELECT %s FROM github_repos" % ", ".join(GH_REPO_COLS))}
    items = {(r[0], r[1]): dict(zip(GH_ITEM_COLS[2:], r[2:]))
             for r in conn.execute("SELECT %s FROM github_items" % ", ".join(GH_ITEM_COLS))}
    return {"repos": repos, "items": items}


def _fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def publish(path: str, meta: Mapping[str, str], memories: Mapping[str, Mapping[str, Any]],
            roles: Mapping[str, Mapping[str, str]], mode: int = 0o644,
            github: Optional[Mapping[str, Mapping[Any, Mapping[str, Any]]]] = None) -> str:
    """저장소 전체를 같은 디렉터리의 새 파일에 쓰고 fsync 한 뒤 os.replace 로 공개한다. -> 공개 파일 sha256.

    memories 에 없는 기억의 roles 는 쓰지 않는다(판정 행 없이 역할만 남지 않게). 기존 파일은 고치지 않는다.
    github(#395, `load_github` 모양)를 주면 GitHub 근거 선택 테이블을 함께 쓴다. None 이면 지금 파일과 같다."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    # 임시 이름은 저장소 파일 이름으로 시작한다(.gitignore 의 `policies/item-roles.sqlite*` 가 남은 임시·저널 파일도 덮는다)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".tmp-", dir=d)
    os.close(fd)
    try:
        conn = sqlite3.connect(tmp)         # mkstemp 의 빈 0600 파일을 그대로 쓴다(지우고 다시 만들면 umask 권한으로 열린다)
        try:
            create_schema(conn, meta)
            for mid in sorted(memories):
                m = memories[mid]
                conn.execute("INSERT INTO memories(memory_id, document_id, mentioned_at, state, text_sha256, judged_at) "
                             "VALUES(?, ?, ?, ?, ?, ?)",
                             (mid, m.get("document_id"), m.get("mentioned_at"), m["state"], m["text_sha256"],
                              m["judged_at"]))
                for key, role in sorted((roles.get(mid) or {}).items()):
                    if role not in ROLES:
                        raise StoreError("role %r is not one of %s" % (role, ROLES))
                    conn.execute("INSERT INTO mention_roles(memory_id, key, role) VALUES(?, ?, ?)", (mid, key, role))
            if github is not None:
                _write_github(conn, github)
            conn.commit()
        finally:
            conn.close()
        os.chmod(tmp, mode)
        with open(tmp, "rb") as fh:
            data = fh.read()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        _fsync_dir(d)
        return hashlib.sha256(data).hexdigest()
    except BaseException:
        for p in (tmp, tmp + "-journal"):
            try:
                os.unlink(p)
            except OSError:
                pass
        raise


# --- GitHub 근거 (#395) ------------------------------------------------------------------------------------

GH_KINDS = ("issue", "pr")
GH_STATES = ("open", "closed")
GH_REPO_STATUSES = ("ok", "missing", "error")
GH_ITEM_COLS = ("repo", "number", "kind", "state", "state_reason", "created_at", "closed_at", "merged_at", "fetched_at")
GH_REPO_COLS = ("repo", "owner", "status", "fetched_at", "error_code")
GITHUB_SCHEMA_SQL = (
    "CREATE TABLE github_items(repo TEXT NOT NULL, number INTEGER NOT NULL CHECK(number > 0), "
    "kind TEXT NOT NULL CHECK(kind IN ('issue','pr')), state TEXT NOT NULL CHECK(state IN ('open','closed')), "
    "state_reason TEXT, created_at TEXT, closed_at TEXT, merged_at TEXT, fetched_at TEXT NOT NULL, "
    "PRIMARY KEY(repo, number))",
    "CREATE TABLE github_repos(repo TEXT PRIMARY KEY, owner TEXT, "
    "status TEXT NOT NULL CHECK(status IN ('ok','missing','error')), fetched_at TEXT NOT NULL, error_code TEXT)",
)

# 근거 판정 값. 종결 = closed(완료 이슈·머지 PR)와 closed_no_reason(사유 없는 옛 닫힘, 표시 달고 종결).
GH_CLOSED, GH_CLOSED_NO_REASON, GH_NOT_PLANNED, GH_OPEN, GH_UNKNOWN = (
    "closed", "closed_no_reason", "not_planned", "open", "unknown")
GH_VERDICTS = (GH_CLOSED, GH_CLOSED_NO_REASON, GH_NOT_PLANNED, GH_OPEN, GH_UNKNOWN)


def empty_github() -> Dict[str, Dict[Any, Dict[str, Any]]]:
    return {"repos": {}, "items": {}}


def _write_github(conn: sqlite3.Connection, github: Mapping[str, Mapping[Any, Mapping[str, Any]]]) -> None:
    for sql in GITHUB_SCHEMA_SQL:
        conn.execute(sql)
    for repo in sorted(github.get("repos") or {}):
        r = github["repos"][repo]
        conn.execute("INSERT INTO github_repos(%s) VALUES(?, ?, ?, ?, ?)" % ", ".join(GH_REPO_COLS),
                     (repo, r.get("owner"), r.get("status"), r.get("fetched_at"), r.get("error_code")))
    for (repo, number) in sorted(github.get("items") or {}):
        it = github["items"][(repo, number)]
        conn.execute("INSERT INTO github_items(%s) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)" % ", ".join(GH_ITEM_COLS),
                     (repo, int(number), it.get("kind"), it.get("state"), it.get("state_reason"),
                      it.get("created_at"), it.get("closed_at"), it.get("merged_at"), it.get("fetched_at")))


@dataclass(frozen=True)
class GithubEvidence:
    """키 하나의 GitHub 근거. verdict = GH_VERDICTS 하나, reason = 근거 코드, closed_at = 종결·미계획일 때만 닫힌 시각
    (머지 PR 은 merged_at), fetched_at = 근거를 받은 시각(없으면 None)."""

    verdict: str
    reason: str
    kind: Optional[str] = None
    closed_at: Optional[str] = None
    merged_at: Optional[str] = None
    fetched_at: Optional[str] = None


def is_closed_verdict(verdict: str) -> bool:
    """종결 근거인가(closed, closed_no_reason). 미계획·열림·미확인은 종결이 아니다."""
    return verdict in (GH_CLOSED, GH_CLOSED_NO_REASON)


def github_evidence(repo_row: Optional[Mapping[str, Any]], item_row: Optional[Mapping[str, Any]]) -> GithubEvidence:
    """GitHub 근거 판정(순수 함수, 상위 #393 결정):
      종결 = closed+completed 이슈, 머지된 PR(merged_at 있음) / 종결(사유 없음) = closed+state_reason 없음 이슈 /
      미계획 = closed+not_planned·duplicate 이슈, 머지 없이 닫힌 PR / 열림 = 지금 open(재오픈 포함, 종결 근거 없음) /
      미확인 = 리포를 받지 않음·missing·error, 번호 없음, 그 밖의 state_reason, 모양이 틀린 행.
    리포 상태가 ok 가 아니면 옛 item 행이 있어도 미확인이다(조회 실패를 종결로 치지 않는다)."""
    if not isinstance(repo_row, Mapping):
        return GithubEvidence(GH_UNKNOWN, "repo-not-fetched")
    fetched = repo_row.get("fetched_at")
    status = repo_row.get("status")
    if status == "missing":
        return GithubEvidence(GH_UNKNOWN, "repo-missing", fetched_at=fetched)
    if status != "ok":
        return GithubEvidence(GH_UNKNOWN, "repo-error", fetched_at=fetched)
    if not isinstance(item_row, Mapping):
        return GithubEvidence(GH_UNKNOWN, "no-number", fetched_at=fetched)
    kind, state, reason = item_row.get("kind"), item_row.get("state"), item_row.get("state_reason")
    fetched = item_row.get("fetched_at") or fetched
    closed_at, merged_at = item_row.get("closed_at"), item_row.get("merged_at")
    if kind == "pr":
        if merged_at:
            return GithubEvidence(GH_CLOSED, "merged", kind, closed_at or merged_at, merged_at, fetched)
        if state == "open":
            return GithubEvidence(GH_OPEN, "open", kind, fetched_at=fetched)
        if state == "closed":
            return GithubEvidence(GH_NOT_PLANNED, "pr-unmerged", kind, closed_at, None, fetched)
        return GithubEvidence(GH_UNKNOWN, "bad-row", kind, fetched_at=fetched)
    if kind != "issue":
        return GithubEvidence(GH_UNKNOWN, "bad-row", fetched_at=fetched)
    if state == "open":
        return GithubEvidence(GH_OPEN, "reopened" if reason == "reopened" else "open", kind, fetched_at=fetched)
    if state != "closed":
        return GithubEvidence(GH_UNKNOWN, "bad-row", kind, fetched_at=fetched)
    if reason == "completed":
        return GithubEvidence(GH_CLOSED, "completed", kind, closed_at, None, fetched)
    if reason in ("not_planned", "duplicate"):
        return GithubEvidence(GH_NOT_PLANNED, reason, kind, closed_at, None, fetched)
    if reason is None or reason == "":
        return GithubEvidence(GH_CLOSED_NO_REASON, "no-reason", kind, closed_at, None, fetched)
    return GithubEvidence(GH_UNKNOWN, "unknown-reason", kind, fetched_at=fetched)


def evidence_for_key(key: str, github: Optional[Mapping[str, Mapping[Any, Mapping[str, Any]]]]) -> GithubEvidence:
    """저장소 키(`<repo>#<n>`)의 근거. github 는 `load_github` 모양(None 이면 미확인)."""
    parsed = split_key(key)
    if github is None:
        return GithubEvidence(GH_UNKNOWN, "repo-not-fetched")
    if parsed is None:
        return GithubEvidence(GH_UNKNOWN, "no-number")
    repo, n = parsed
    return github_evidence((github.get("repos") or {}).get(repo), (github.get("items") or {}).get((repo, n)))
