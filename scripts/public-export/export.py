#!/usr/bin/env python3
"""공개판 내보내기 도구 (ADR 0002 하이브리드 개정, #426).

운영 리포의 --source-ref 하나만 새 임시 리포로 가져와 git filter-repo 로 거르고, 전부 통과해야 성공하는
검사를 돌린다. 산출 리포·댓글 본문은 --out 에 남긴다(지우지 않는다). 기본은 dry-run 이다.
--apply 는 검사가 모두 통과했을 때만 운영 리포에 다음 export/NNN 주석 태그를 만들고 origin 에만 push 한다.
공개판 push 는 하지 않는다(docs/public-export.md "사용법").

걸러 내기(같은 입력·규칙이면 같은 sha):
  - 매니페스트 include 밖 경로와 exclude_history 경로를 이력 전체에서 뺀다(경로 해석은 check_manifest.path_matches).
  - history_rewrite_paths 에 걸리는 경로의 blob·경로 이름과 커밋 메시지(trailer 포함)에 이름 표 -> message_rewrites 순으로
    치환한다. 이름은 대소문자 무시로 맞추고 앞뒤가 [A-Za-z0-9]가 아닐 때만(밑줄·하이픈은 경계), 긴 이름 먼저 바꾼다.
    맞은 문자열이 표의 어떤 행과 꼴까지 같으면 그 행의 데모, 아니면 꼴(전부 대문자·첫 글자만 대문자)을 데모에 옮긴다.
    매니페스트 public_words 와 같은 이름은 치환·표 누락 검사·유출 검사에서 뺀다.
  - 그 뒤 경로별 구조 치환(#459, history_rewrite_paths 와 상관없이 이력 전체 판본): 정책 JSON(FREETEXT_PATHS)의
    defaults·뱅크별 자유 문장 필드(FREETEXT_FIELDS)는 HEAD 데모 fixture 의 같은 뱅크 값, 없으면 중립 문구로(KEEP_TEMPLATES 의
    HEAD 공개 템플릿 값과 같은 값은 그대로). 트리거 JSON 의 note·recall.query 는 같은 경로 HEAD 판본(없으면
    policies/context-triggers.json HEAD)의 같은 id 값, 없으면 중립 문구로.
    docs/public-export.md 의 '## 리허설 기록' 절은 지우고(코드 블록 안 제목 줄은 제목이 아니다), 매니페스트의
    deny_patterns·message_rewrites·accept 값은 빈 목록으로. 값 리터럴·절 자리만 바꿔 다른 바이트는 그대로 둔다.
    원래 JSON 으로 읽히지 않는 판본은 그대로 두고 건수만 출력하고, 이름 표 치환 뒤 깨진 판본은 '구조 치환' 검사 실패다.
  - public_literals 의 정확한 문자열은 치환 전에 자리표시자로 감췄다가 모든 치환 뒤 되돌린다('/' 또는 '-'가 든 값만, 이름 표 이름
    자체나 그 안의 더 짧은 문자열은 exit 2).
  - 작성자·커미터는 --mailmap 으로 바꾸고, 커밋 메시지 trailer 블록에 Exported-From: <원본 sha> 를 더한다.
  - 빈 커밋 처리는 --prune-empty auto --prune-degenerate auto 로 고정한다.

독립 유출 검사는 치환 정규식을 쓰지 않는다: 표 왼쪽 열 모든 이름을 경계 없는 부분 문자열, 대소문자 무시로 센다.
좁은 예외 네 가지(#441·#459·#468, 면제 건수는 실패와 따로 센다):
  - base64 해독 재검사(blob 표면만, 먼저): 64자 넘는 base64 덩어리는 이름 적중 여부와 상관없이 엄격 해독 -> 엄격
    UTF-8 해독본을 같은 검사기로 한 단계만 다시 검사한다(#448). 해독본 적중은 실패다. 덩어리 안 이름 적중은 해독본이
    깨끗할 때만 뺀다(encoded-checked). 해독이 안 되면 원문 적중이 없는 덩어리는 무시하고, 있는 덩어리는 아래로 넘긴다.
  - 검토 토큰(--reviewed-tokens, blob·메시지 표면만): 적중을 감싼 영숫자 토큰이 목록의 토큰과 대소문자까지 같고
    그 줄의 이름이 적중한 이름일 때만 뺀다(reviewed).
  - public_literals(#459): 이름·이름 밖 검사기 적중이 그 정확한 문자열 범위 안에 있으면 뺀다(public-literal).
  - public_words(#468): 이름 적중만, 그 단어의 온전한 출현(대소문자 무시, 앞뒤가 [A-Za-z0-9]가 아님) 안에 완전히 들 때
    뺀다(public-word, 표 행 번호 없음). 이름 밖 검사기와 base64 해독본에는 쓰지 않는다. 값은 이미 공개된 토큰만 더한다.
사설 IPv4 리터럴 검사(private-ipv4, #456)는 이름 유출과 별도의 검사 줄이다: blob·경로·커밋/태그 메시지에서 점으로 이은
IPv4 리터럴 중 RFC1918(10/8, 172.16/12, 192.168/16)과 100.64/10 을 센다(loopback·문서 예시 대역·공인 주소는 세지 않는다).
검토 토큰 파일의 '<IPv4>\tprivate-ipv4' 줄은 그 리터럴 전체와 정확히 같은 적중만 모든 표면에서 뺀다(오탐용).
금칙 deny_patterns·message_rewrites·accept 는 매니페스트 값 뒤에 리포 밖 로컬 규칙 파일(--local-rules)을 합친 것이다.
출력은 검사별 통과/실패와 건수, 표 행 번호, 가린 경로뿐이다. 금칙 패턴·이름 표·주소·맞은 문자열은 출력하지 않는다.
--suggest-rows 는 남은 이름 적중을 감싼 영숫자 토큰의 표 행 제안을 리포 밖 파일(600)에 쓰고 건수만 출력한다.
exit: 성공 0, 검사 실패 1, 사용·도구 오류 2.
"""
from __future__ import annotations

import argparse
import base64
import bisect
import hashlib
import importlib.util
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("check_manifest", HERE / "check_manifest.py")
cm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cm)

MANIFEST_REL = "scripts/public-export/manifest.json"
GITLEAKS_REL = ".gitleaks.toml"
DEFAULT_RENAME_TABLE = "~/.config/hindsight-crew/public-export/rename-table.tsv"
DEFAULT_REVIEWED_TOKENS = "~/.config/hindsight-crew/public-export/reviewed-tokens.tsv"
REVIEWED_IPV4_KEY = "private-ipv4"      # 검토 토큰 파일 둘째 칸이 이 값이면 사설 IPv4 면제 줄(#456)
# 점으로 이은 숫자 덩어리(앞뒤가 숫자가 아님). 사설 IPv4 는 덩어리 안 연속한 네 조각마다 본다(fail-closed):
# 'host-1.<a>.<b>.<c>.<d>.nip.io' 처럼 숫자·점 뒤에 붙은 주소도 놓치지 않는다. 대신 다섯 조각 이상 버전 문자열 안의
# 사설 대역 네 조각도 적중한다(오탐은 검토 토큰 IPv4 줄로 뺀다).
DOTTED_RUN_RE = re.compile(r"(?<![0-9])[0-9]+(?:\.[0-9]+)+")
IPV4_LITERAL_RE = re.compile(r"([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})")
# RFC1918 + 100.64/10. 점 리터럴로 쓰지 않는다(이 파일 자신이 공개판 private-ipv4 검사에 걸리지 않게).
PRIVATE_IPV4_NETS = tuple(ipaddress.IPv4Network((bytes(octets), prefix)) for octets, prefix in (
    ((10, 0, 0, 0), 8), ((172, 16, 0, 0), 12), ((192, 168, 0, 0), 16), ((100, 64, 0, 0), 10)))
REVIEWED_MAX_TOKEN = 64                 # 검토 토큰 최대 길이(넘으면 검토 토큰 파일 검사 실패)
B64_CHUNK_RE = re.compile(r"[A-Za-z0-9+/]+={0,2}")
B64_MIN_CHUNK = 64                      # 이 길이를 넘는 blob 의 base64 덩어리는 이름 적중 여부와 상관없이 해독해 다시 검사한다(#448)
DEFAULT_SPLIT_REV = "252320e"           # 개인 정책이 policies/local/ 로 분리된 커밋(#187)
OLD_FIXTURE = "tests/fixtures/policies/b?/"
SAMPLE_FIXTURE = "tests/fixtures/policies/sample/"
SPLIT_POLICY_FILES = ("policies/bank-policies.json", "policies/context-triggers.json")
ALNUM = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")
SUGGEST_MAX_TOKEN = 64                  # 이보다 긴 토큰(이진 blob 의 base64 등)은 제안하지 않고 건수만 센다
SHORT_NAME = 4                          # 이름 길이가 이 이하면 제안 행 셋째 칸에 short(일반 단어 치환 검토용)
WEB_MERGE_EMAIL = "noreply@github.com"
USERS_NOREPLY_RE = re.compile(r"^\d+\+[^@\s]+@users\.noreply\.github\.com$", re.I)
TRAILER_KEY = "Exported-From"
TRAILER_LINE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*:[ \t]")
EXPORTED_FROM_RE = re.compile(r"^Exported-From: (\S*)\s*$", re.M)
EXPORT_TAG_RE = re.compile(r"^export/(\d+)$")
PREV_REF = "refs/hc-export/prev-public"
CB_MODULE = "hc_export_cb"
ANCESTRY_FAIL_MSG = "규칙 변경으로 이력이 바뀜 - force push는 공개판 관리 주체(에이전트)가 관문 통과 뒤 실행하고 운영자에게 보고"
ZERO_SHA = "0" * 40


class ToolError(Exception):
    """사용·환경·외부 도구 오류(exit 2)."""


# ---------------------------------------------------------------- 치환 규칙 (filter-repo 콜백과 검사가 함께 쓴다)

def shape(matched: str, demo: str) -> str:
    """맞은 문자열의 꼴을 데모에 옮긴다: 전부 대문자 -> 데모 전부 대문자, 첫 글자만 대문자 -> 데모 첫 글자 대문자, 그 밖 -> 데모 그대로."""
    if matched.isupper():
        return demo.upper()
    if matched[:1].isupper() and matched[1:] == matched[1:].lower():
        return demo[:1].upper() + demo[1:]
    return demo


class NameTable:
    """검사(lint)를 마친 (이름, 데모) 쌍. 맞은 문자열이 어떤 행의 왼쪽과 꼴까지 같으면 그 행의 데모를 그대로,
    아니면 소문자화한 키의 파일 순서상 첫 행 데모에 꼴 보존 규칙을 쓴다."""

    def __init__(self, pairs: list[tuple[str, str]]):
        self.explicit: dict[str, str] = {}
        self.first: dict[str, str] = {}
        for name, demo in pairs:
            self.explicit.setdefault(name, demo)
            self.first.setdefault(name.lower(), demo)
        self.names = list(self.explicit)

    def demo_for(self, matched: str) -> str:
        if matched in self.explicit:
            return self.explicit[matched]
        demo = self.first.get(matched.lower())
        if demo is None:
            # re.I 는 lower() 와 접기가 다르다(İ·ı·ſ·K 등). 정규식이 맞춘 키를 같은 re.I 등가로 다시 찾는다.
            demo = next((d for k, d in self.first.items() if re.fullmatch(re.escape(k), matched, re.I)), None)
        return matched if demo is None else shape(matched, demo)   # 못 찾으면 그대로 둔다(독립 유출 검사가 잡는다)


# ---- 경로별 구조 치환(#459): 이름 표가 못 보는 자유 문장·리허설 절·해독표를 이력 전체에서 바꾼다.
# 대상 필드 근거: gateway/bank_policies.py _VALIDATORS 중 값 검증이 길이뿐인 자유 문자열은 _as_instructions
# (instructions_text) 하나다. 나머지는 bool·정수·접두어·slug·정규식(content_prefix_pattern)이다. 과거 판본에도
# 다른 자유 문장 키는 없었다(이력의 정책 JSON 판본 전체에서 문자열 값을 가진 뱅크 키는 이 둘뿐).
FREETEXT_FIELDS = ("instructions_text",)
FREETEXT_PATHS = ("policies/bank-policies.json", "tests/fixtures/policies/")    # cm.path_matches 표기
FREETEXT_FIXTURE = "tests/fixtures/policies/sample/bank-policies.json"          # 치환 값의 출처(HEAD 판본)
FREETEXT_NEUTRAL = "Demo bank instructions (neutral text set by the public export)."
REHEARSAL_DOC = "docs/public-export.md"
REHEARSAL_TITLE = "리허설 기록"                    # 이 제목의 2수준 절을 지운다(다음 2수준 이하 제목 전까지)
MANIFEST_RULE_KEYS = ("deny_patterns", "message_rewrites", "accept")   # 옛 판본의 값(해독표)을 빈 목록으로
# 트리거 정책 JSON(#459 rework): 파일 이름이 context-triggers.json 인 TRIGGER_PATHS 판본. 대상 필드 근거:
# gateway/context_triggers.py _parse_row 중 자유 문장은 note(문자열 또는 null, 내용 검증 없음)와 recall.query
# (8~500자 질의 문장) 둘이다. id·bank·tools·all_of·none_of·tags 등은 구조 값이라 이름 표 몫으로 둔다.
TRIGGER_FILE = "context-triggers.json"
TRIGGER_PATHS = ("policies/context-triggers.json", "tests/fixtures/policies/")
TRIGGER_BASE = "policies/context-triggers.json"   # 같은 경로 HEAD 판본에 없는 트리거 id 는 이 HEAD 판본 값을 쓴다
TRIGGER_NEUTRAL_NOTE = "Demo trigger (neutral text set by the public export)."
TRIGGER_NEUTRAL_QUERY = "demo trigger recall query"
# 이 fixture 판본의 자유 문장이 HEAD 공개 템플릿의 같은 뱅크·같은 필드 값과 정확히 같으면 그대로 둔다(이미 공개 문구)
KEEP_TEMPLATES = {"tests/fixtures/policies/personal-bank-pre-item.json": "policies/templates/personal-bank.json"}
_JSON_WS = re.compile(r"[ \t\n\r]*")
_JSON_DEC = json.JSONDecoder()
_FENCE_RE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")
_HEADING_RE = re.compile(r" {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*")


def structural_kind(path: str) -> str | None:
    if path == REHEARSAL_DOC:
        return "doc"
    if path == MANIFEST_REL:
        return "manifest"
    if path.rsplit("/", 1)[-1] == TRIGGER_FILE and any(cm.path_matches(p, path) for p in TRIGGER_PATHS):
        return "trigger"
    if any(cm.path_matches(p, path) for p in FREETEXT_PATHS):
        return "policy"
    return None


def _ws(s: str, i: int) -> int:
    return _JSON_WS.match(s, i).end()


def _members(s: str, i: int) -> list[tuple[str, int, int]]:
    """s[i] 가 '{' 인 객체의 (키, 값 시작, 값 끝) 목록. 전체가 json.loads 로 읽힌 문서에만 쓴다."""
    out = []
    i = _ws(s, i + 1)
    if s[i] == "}":
        return out
    while True:
        key, i = json.decoder.scanstring(s, i + 1)
        i = _ws(s, _ws(s, i) + 1)                   # ':' 건너뜀
        _, end = _JSON_DEC.raw_decode(s, i)
        out.append((key, i, end))
        i = _ws(s, end)
        if s[i] == "}":
            return out
        i = _ws(s, i + 1)                           # ',' 건너뜀


def _elements(s: str, i: int) -> list[tuple[int, int]]:
    """s[i] 가 '[' 인 배열의 (값 시작, 값 끝) 목록. 전체가 json.loads 로 읽힌 문서에만 쓴다."""
    out = []
    i = _ws(s, i + 1)
    if s[i] == "]":
        return out
    while True:
        _, end = _JSON_DEC.raw_decode(s, i)
        out.append((i, end))
        i = _ws(s, end)
        if s[i] == "]":
            return out
        i = _ws(s, i + 1)                           # ',' 건너뜀


def _load_object(raw: bytes) -> tuple[str | None, bool]:
    """(텍스트, JSON 으로 읽혔나). 최상위가 객체가 아니면 텍스트는 None(읽히긴 했다)."""
    try:
        s = raw.decode("utf-8")
        doc = json.loads(s)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None, False
    return (s if isinstance(doc, dict) else None), True


def _splice(s: str, edits: list[tuple[int, int, str]]) -> str:
    for start, end, lit in sorted(edits, reverse=True):
        s = s[:start] + lit + s[end:]
    return s


def freetext_fixture(raw: bytes | None) -> dict:
    """HEAD 데모 fixture 에서 치환 값만: {"defaults": {필드: 값}, "banks": {뱅크: {필드: 값}}}. 읽지 못하면 {}(전부 중립 문구)."""
    try:
        doc = json.loads(raw.decode("utf-8")) if raw is not None else None
    except (UnicodeDecodeError, ValueError):
        doc = None
    if not isinstance(doc, dict):
        return {}
    pick = lambda spec: {f: spec[f] for f in FREETEXT_FIELDS if f in spec} if isinstance(spec, dict) else {}  # noqa: E731
    banks = doc.get("banks")
    return {"defaults": pick(doc.get("defaults")),
            "banks": {k: pick(v) for k, v in banks.items()} if isinstance(banks, dict) else {}}


def policy_freetext(raw: bytes, fixture: dict, keep: dict | None = None) -> tuple[bytes, int, bool]:
    """(새 바이트, 바꾼 필드 수, JSON 으로 읽혔나). defaults·뱅크별 자유 문장 필드의 문자열 값만 바꾼다: fixture 의
    같은 뱅크(defaults 는 defaults)에 그 필드가 있으면 그 값, 없으면 FREETEXT_NEUTRAL. 값 리터럴 자리만 갈아 끼워
    다른 바이트(키 순서·들여쓰기·이스케이프)는 그대로 둔다. 같은 키가 여러 번 나오면 전부 바꾼다.
    keep(fixture 와 같은 꼴, KEEP_TEMPLATES 의 HEAD 공개 템플릿)의 같은 뱅크·같은 필드 값과 정확히 같은 값은 그대로 둔다."""
    s, ok = _load_object(raw)
    if s is None:
        return raw, 0, ok
    edits = []
    keep = keep or {}

    def fields_of(start: int, given: dict, kept: dict) -> None:
        for key, vs, ve in _members(s, start):
            if key not in FREETEXT_FIELDS or not isinstance(json.loads(s[vs:ve]), str):
                continue
            if key in kept and json.loads(s[vs:ve]) == kept[key]:
                continue
            want = given.get(key, FREETEXT_NEUTRAL)
            if json.loads(s[vs:ve]) != want:
                edits.append((vs, ve, json.dumps(want, ensure_ascii=False)))

    for key, vs, _ in _members(s, _ws(s, 0)):
        if s[vs] != "{":
            continue
        if key == "defaults":
            fields_of(vs, fixture.get("defaults", {}), keep.get("defaults", {}))
        elif key == "banks":
            for bank, bs, _ in _members(s, vs):
                if s[bs] == "{":
                    fields_of(bs, fixture.get("banks", {}).get(bank, {}), keep.get("banks", {}).get(bank, {}))
    if not edits:
        return raw, 0, True
    return _splice(s, edits).encode("utf-8"), len(edits), True


def trigger_fixture(raw: bytes | None) -> dict:
    """HEAD 트리거 판본에서 치환 값만: {트리거 id: {"note": 값, "query": 값}}(있는 필드만, 같은 id 는 첫 행). 읽지 못하면 {}."""
    try:
        doc = json.loads(raw.decode("utf-8")) if raw is not None else None
    except (UnicodeDecodeError, ValueError):
        doc = None
    rows = doc.get("triggers") if isinstance(doc, dict) else None
    out: dict = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or row["id"] in out:
            continue
        got = {"note": row["note"]} if "note" in row else {}
        if isinstance(row.get("recall"), dict) and "query" in row["recall"]:
            got["query"] = row["recall"]["query"]
        out[row["id"]] = got
    return out


def trigger_freetext(raw: bytes, given: dict) -> tuple[bytes, int, bool]:
    """(새 바이트, 바꾼 필드 수, JSON 으로 읽혔나). triggers[] 행의 note 와 recall.query 문자열 값만 바꾼다: given
    (trigger_fixture 꼴)의 같은 id 에 그 필드가 있으면 그 값, 없으면 중립 문구. id·bank·all_of 등 구조 값은 두고,
    값 리터럴 자리만 갈아 끼운다(정책 치환과 같은 계약)."""
    s, ok = _load_object(raw)
    if s is None:
        return raw, 0, ok
    edits = []

    def edit(vs: int, ve: int, got: dict, field: str, neutral: str) -> None:
        cur = json.loads(s[vs:ve])
        if isinstance(cur, str):
            want = got.get(field, neutral)
            if cur != want:
                edits.append((vs, ve, json.dumps(want, ensure_ascii=False)))

    for key, vs, _ in _members(s, _ws(s, 0)):
        if key != "triggers" or s[vs] != "[":
            continue
        for es, _ in _elements(s, vs):
            if s[es] != "{":
                continue
            members = _members(s, es)
            tid = next((json.loads(s[a:b]) for k, a, b in members if k == "id"), None)
            got = given.get(tid, {}) if isinstance(tid, str) else {}
            for k, a, b in members:
                if k == "note":
                    edit(a, b, got, "note", TRIGGER_NEUTRAL_NOTE)
                elif k == "recall" and s[a] == "{":
                    for k2, a2, b2 in _members(s, a):
                        if k2 == "query":
                            edit(a2, b2, got, "query", TRIGGER_NEUTRAL_QUERY)
    if not edits:
        return raw, 0, True
    return _splice(s, edits).encode("utf-8"), len(edits), True


def clear_manifest_rules(raw: bytes) -> tuple[bytes, int, bool]:
    """(새 바이트, 비운 키 수, JSON 으로 읽혔나). 최상위 MANIFEST_RULE_KEYS 값을 [] 로(다른 바이트 그대로)."""
    s, ok = _load_object(raw)
    if s is None:
        return raw, 0, ok
    edits = [(vs, ve, "[]") for key, vs, ve in _members(s, _ws(s, 0))
             if key in MANIFEST_RULE_KEYS and s[vs:ve] != "[]"]
    if not edits:
        return raw, 0, True
    return _splice(s, edits).encode("utf-8"), len(edits), True


def strip_rehearsal(raw: bytes) -> tuple[bytes, int]:
    """(새 바이트, 지운 절 수). '## 리허설 기록' 제목 줄부터 다음 1·2수준 제목 줄 전까지(없으면 끝까지) 지운다.
    제목은 울타리 코드 블록(``` 또는 ~~~, 닫는 울타리는 같은 문자로 여는 길이 이상) 밖에서만 센다."""
    s = raw.decode("utf-8", "surrogateescape")
    lines = re.findall(r"[^\n]*\n|[^\n]+$", s)
    keep, fence, removing, n = [], None, False, 0
    for line in lines:
        body = line.rstrip("\n").rstrip("\r")
        heading = None
        if fence is not None:
            mo = _FENCE_RE.fullmatch(body)
            if mo and mo.group(1)[0] == fence[0] and len(mo.group(1)) >= len(fence) and not mo.group(2).strip():
                fence = None
        else:
            mo = _FENCE_RE.fullmatch(body)
            if mo and not (mo.group(1)[0] == "`" and "`" in mo.group(2)):
                fence = mo.group(1)
            else:
                hm = _HEADING_RE.fullmatch(body)
                if hm:
                    heading = (len(hm.group(1)), re.sub(r"[ \t]+#+$", "", hm.group(2) or "").strip())
        if heading is not None and heading[0] <= 2:
            removing = heading == (2, REHEARSAL_TITLE)
            n += removing
        if not removing:
            keep.append(line)
    if not n:
        return raw, 0
    return "".join(keep).encode("utf-8", "surrogateescape"), n


def literal_regex(literals) -> re.Pattern[str] | None:
    """매니페스트 public_literals: 정확 일치(대소문자 구분), 긴 문자열 먼저."""
    lits = sorted(set(literals), key=lambda x: (-len(x), x))
    return re.compile("|".join(re.escape(x) for x in lits)) if lits else None


def public_literal_conflicts(literals, names) -> list[int]:
    """public_literals 중 이름 표 왼쪽 이름과 같거나(대소문자 무시) 그 이름 안에 든 더 짧은 문자열의 항목 번호(1부터).
    그런 값은 사실상 이름 자체를 보호해 치환·유출 검사를 끈다. 값은 돌려주지 않는다."""
    low = [n.lower() for n in names]
    return [i for i, lit in enumerate(literals, 1)
            if any(lit.lower() == n or (len(lit) < len(n) and lit.lower() in n) for n in low)]


def literal_spans(rx: re.Pattern[str] | None, text: str) -> list[tuple[int, int]]:
    return [mo.span() for mo in rx.finditer(text)] if rx is not None else []


def public_word_regex(words) -> re.Pattern[str] | None:
    """매니페스트 public_words 의 온전한 출현: 대소문자 무시, 앞뒤가 [A-Za-z0-9]가 아닐 때만, 긴 단어 먼저(#468)."""
    ws = sorted(set(words), key=lambda x: (-len(x), x))
    return re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(map(re.escape, ws)) + r")(?![A-Za-z0-9])", re.I) if ws else None


def _inside(spans: list[tuple[int, int]], s: int, e: int) -> bool:
    return any(ps <= s and e <= pe for ps, pe in spans)


class Rewriter:
    """이름 표(cm.rename_regex: 대소문자 무시, 영숫자 경계, 긴 이름 먼저, 꼴 보존) -> message_rewrites(순서대로).
    blob·경로 이름·커밋 메시지에 같은 순서로 쓴다. blob 은 그 뒤에 경로별 구조 치환(structural)을 더 건다.
    매니페스트 public_literals 의 정확한 문자열은 보호한다: 치환 전에 그 출현을 고유 자리표시자(NUL 과 사용자 영역
    문자뿐이라 이름 경계 판정을 바꾸지 않는다)로 바꾸고, 이름 표·message_rewrites 를 모두 쓴 뒤 되돌린다. 넓은 규칙이
    자리표시자까지 덮으면 규칙이 이긴다(보호 문자열도 같이 지워진다)."""

    def __init__(self, pairs: list[tuple[str, str]], message_rewrites: list[dict], rewrite_paths: list[str],
                 fixture: dict | None = None, literals=(), triggers: dict | None = None, keep: dict | None = None):
        self.table = NameTable(pairs)
        self.rx = cm.rename_regex(self.table.names) if self.table.names else None
        self.msg = [(re.compile(r["regex"]), r["replace"]) for r in message_rewrites]
        self.paths = list(rewrite_paths)
        self.fixture = fixture or {}
        self.lit_rx = literal_regex(literals)
        self.triggers = triggers or {"base": {}, "paths": {}}   # 트리거 치환 값(경로별 HEAD 판본 + TRIGGER_BASE)
        self.keep = keep or {}                                   # 경로 -> 그대로 둘 공개 템플릿 값(KEEP_TEMPLATES)

    def structural(self, kind: str | None, raw: bytes, path: str | None = None) -> tuple[bytes, int, bool]:
        """(새 바이트, 바꾼 수, JSON 으로 읽혔나). 이름 표 치환 뒤 바이트에 쓴다(뱅크 키·트리거 id 는 치환 뒤 키로 대조)."""
        if kind == "policy":
            return policy_freetext(raw, self.fixture, self.keep.get(path))
        if kind == "trigger":
            return trigger_freetext(raw, self.triggers["paths"].get(path, self.triggers["base"]))
        if kind == "manifest":
            return clear_manifest_rules(raw)
        if kind == "doc":
            new, n = strip_rehearsal(raw)
            return new, n, True
        return raw, 0, True

    def blob(self, path: str, raw: bytes) -> bytes:
        """경로 하나의 blob 치환: history_rewrite_paths 안이면 이름 표 -> message_rewrites, 그 뒤 경로별 구조 치환."""
        if self.in_scope(path):
            raw = self.data(raw)
        return self.structural(structural_kind(path), raw, path)[0]

    def _hide(self, s: str) -> tuple[str, list[tuple[str, str]]]:
        """보호 문자열 출현을 자리표시자로: (바꾼 문자열, [(자리표시자, 보호 문자열)]). 자리표시자 테두리는 입력에 없는
        사용자 영역 문자를 고른다(같은 입력이면 같은 문자, 결정적)."""
        found = sorted({mo.group(0) for mo in self.lit_rx.finditer(s)}, key=lambda x: (-len(x), x))
        if not found:
            return s, []
        edge = next((chr(c) for c in range(0xE000, 0xE800) if chr(c) not in s), None)
        if edge is None or len(found) > 0x700:
            raise ToolError("보호 문자열 자리표시자로 쓸 문자가 없다")
        ph = {lit: f"\0{edge}{chr(0xE800 + i)}{edge}\0" for i, lit in enumerate(found)}
        return self.lit_rx.sub(lambda mo: ph[mo.group(0)], s), [(p, lit) for lit, p in ph.items()]

    def text(self, s: str) -> str:
        restore: list[tuple[str, str]] = []
        if self.lit_rx is not None:
            s, restore = self._hide(s)
        if self.rx is not None:
            s = self.rx.sub(lambda mo: self.table.demo_for(mo.group(0)), s)
        for rx, rep in self.msg:
            s = rx.sub(rep, s)
        for p, lit in restore:
            s = s.replace(p, lit)
        return s

    def data(self, raw: bytes) -> bytes:
        # surrogateescape: utf-8 이 아닌 바이트도 그대로 되돌린다
        return self.text(raw.decode("utf-8", "surrogateescape")).encode("utf-8", "surrogateescape")

    def in_scope(self, path: str) -> bool:
        return any(cm.path_matches(p, path) for p in self.paths)


def add_trailer(message: str, key: str, value: str) -> str:
    """마지막 문단이 trailer 블록이면 거기에 한 줄 더하고, 아니면 새 trailer 문단을 만든다."""
    body = message.rstrip("\n")
    line = f"{key}: {value}"
    if not body:
        return line + "\n"
    paras = body.split("\n\n")
    lines = paras[-1].split("\n")
    is_block = (len(paras) > 1 and TRAILER_LINE_RE.match(lines[0]) is not None
                and all(TRAILER_LINE_RE.match(x) or x[:1] in (" ", "\t") for x in lines))
    return body + ("\n" if is_block else "\n\n") + line + "\n"


_CB: Rewriter | None = None


def cb_init(config_path: str) -> None:
    global _CB
    cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
    _CB = Rewriter([tuple(p) for p in cfg["rename"]], cfg["message_rewrites"], cfg["rewrite_paths"],
                   cfg.get("fixture"), cfg.get("public_literals", ()), cfg.get("triggers"), cfg.get("keep"))


def cb_file_info(filename, mode, blob_id, value):
    """--file-info-callback: history_rewrite_paths 경로와 구조 치환 경로(#459)의 텍스트 blob 만 치환한다."""
    if mode == b"160000":
        return filename, mode, blob_id
    path = filename.decode("utf-8", "surrogateescape")
    key = (blob_id, _CB.in_scope(path), structural_kind(path))   # 같은 blob 이라도 경로에 따라 결과가 다르다
    if not key[1] and key[2] is None:
        return filename, mode, blob_id
    cache = value.data.setdefault("hc_export", {})
    if key not in cache:
        raw = value.get_contents_by_identifier(blob_id)
        new_id = blob_id
        if b"\0" not in raw[:8192]:              # 이진 blob 은 건드리지 않는다(남은 이름은 독립 유출 검사가 잡는다)
            new = _CB.blob(path, raw)
            if new != raw:
                new_id = value.insert_file_with_contents(new)
        cache[key] = new_id
    return filename, mode, cache[key]


def cb_message(message: bytes) -> bytes:
    return _CB.data(message)


def cb_commit(commit) -> None:
    text = commit.message.decode("utf-8", "surrogateescape")
    commit.message = add_trailer(text, TRAILER_KEY, commit.original_id.decode()).encode("utf-8", "surrogateescape")


def _callback_body(config_path: Path, call: str) -> str:
    return ("import sys\n"
            f"m = sys.modules.get({CB_MODULE!r})\n"
            "if m is None:\n"
            "  import importlib.util\n"
            f"  s = importlib.util.spec_from_file_location({CB_MODULE!r}, {str(Path(__file__).resolve())!r})\n"
            "  m = importlib.util.module_from_spec(s)\n"
            f"  sys.modules[{CB_MODULE!r}] = m\n"
            "  s.loader.exec_module(m)\n"
            f"  m.cb_init({str(config_path)!r})\n"
            f"return {call}\n")


# ---------------------------------------------------------------- git·외부 도구

def _run(cmd: list[str], cwd: Path | None = None, input: bytes | None = None):
    try:
        return subprocess.run(cmd, cwd=cwd, input=input, capture_output=True)
    except FileNotFoundError:
        raise ToolError(f"{cmd[0]} 없음")


def _last_line(raw: bytes) -> str:
    lines = raw.decode("utf-8", "replace").strip().splitlines()
    return lines[-1][:200] if lines else ""


def git(repo: Path, *args: str, binary: bool = False, check: bool = True):
    r = _run(["git", "-C", str(repo), *args])
    if check and r.returncode:
        raise ToolError(f"git {args[0]} 실패(rc={r.returncode}): {_last_line(r.stderr)}")
    return r.stdout if binary else r.stdout.decode("utf-8", "replace")


def check_tools() -> tuple[str, str]:
    """git filter-repo(2.47 이상, --file-info-callback)와 gitleaks 가 실행되는지 본다. (filter-repo 버전, gitleaks 버전)."""
    r = _run(["git", "filter-repo", "--version"])
    if r.returncode:
        raise ToolError("git filter-repo 없음 또는 실행 오류(2.47 이상 필요)")
    fr_version = r.stdout.decode("utf-8", "replace").strip()
    h = _run(["git", "filter-repo", "-h"])
    if h.returncode or b"--file-info-callback" not in h.stdout:
        # --version 은 버전 번호가 아니라 소스 해시를 찍는다. 2.47 에서 들어온 옵션 유무로 판정한다.
        raise ToolError(f"git filter-repo 2.47 이상 필요(--file-info-callback 없음, --version {fr_version})")
    g = _run(["gitleaks", "version"])
    if g.returncode:
        raise ToolError(f"gitleaks 실행 오류(rc={g.returncode})")
    return fr_version, g.stdout.decode("utf-8", "replace").strip()


# ---------------------------------------------------------------- 입력

def load_mailmap(path: Path) -> tuple[set[str], set[str], set[str], set[tuple[str, str]]]:
    """mailmap 의 (바뀐 뒤 주소, 바뀌기 전 주소, 바뀐 뒤 이름, 바뀐 뒤 (이름, 주소) 쌍). 첫째 칸(대상)이 도구가 만드는
    공개 신원이다. '이름 <주소>' 한 칸짜리 줄은 주소를 바꾸지 않는다(뒤 주소에만 넣는다)."""
    targets, sources, names, pairs = set(), set(), set(), set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        emails = re.findall(r"<([^>]*)>", line)
        if not emails:
            continue
        targets.add(emails[0].strip().lower())
        name = line.split("<", 1)[0].strip()
        if name:
            names.add(name)
            pairs.add((name, emails[0].strip().lower()))
        if len(emails) > 1:
            sources.add(emails[1].strip().lower())
    return targets, sources - targets, names, pairs


def email_allowed(email: str, targets: set[str]) -> bool:
    e = email.strip().lower()
    return e in targets or e == WEB_MERGE_EMAIL or USERS_NOREPLY_RE.match(e) is not None


def _outside_path(label: str, value: str, repo: Path) -> Path:
    """없어도 되는 출력 경로. 존재는 보지 않고 위치만 본다(원본 리포 작업 트리 안이면 오류)."""
    rp = Path(value).expanduser().resolve()
    if rp == repo or repo in rp.parents:
        raise ToolError(f"{label} 는 원본 리포 작업 트리 밖이어야 한다")
    return rp


def _local_file(label: str, value: str, repo: Path) -> Path:
    p = Path(value).expanduser()
    if not p.is_file():
        raise ToolError(f"{label} 파일 없음: {p}")
    rp = p.resolve()
    if rp == repo or repo in rp.parents:
        raise ToolError(f"{label} 는 리포 밖 파일이어야 한다")
    return rp


def _rows_text(lines: list[int]) -> str:
    return ", ".join(map(str, lines))


def _table_rows(path: Path) -> list[tuple[int, str, str]]:
    """(파일 줄 번호, 이름, 데모). 읽기 규칙은 cm.load_rename_table 과 같다(어긋나면 오류)."""
    rows = []
    for no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        name = parts[0].strip()
        if name:
            rows.append((no, name, parts[1].strip() if len(parts) > 1 else ""))
    if [(n, d) for _, n, d in rows] != cm.load_rename_table(path):
        raise ToolError("이름 표 읽기 규칙 불일치(도구 결함)")
    return rows


def load_rename_pairs(path: Path, public: frozenset[str] = frozenset()) -> list[tuple[int, str, str]]:
    """이름 표 검사(lint) 뒤 (줄 번호, 이름, 데모) 목록(파일 순서, 정확히 같은 행은 하나로). 실패 메시지는 행 번호만 낸다.
    - 데모가 빈 행은 실패.
    - public_words 와 같은 이름(대소문자 무시)의 행은 뺀다(치환·검사 대상 아님).
    - 소문자화한 이름이 같은 행끼리는 데모를 소문자화해 모두 같아야 한다.
    - 데모를 소문자화한 문자열에 (자기 행 포함) 어느 왼쪽 이름의 소문자 부분 문자열이라도 있으면 실패(자기 매핑 행 포함).
      치환 뒤 데모가 독립 유출 검사에 걸리지 않게 하는 규칙이라 유출 검사와 같은 부분 문자열 비교다."""
    rows = _table_rows(path)
    empty = [no for no, _, d in rows if not d]
    if empty:
        raise ToolError(f"이름 표에 데모 이름이 빈 행: {_rows_text(empty)}")
    rows = [r for r in rows if r[1].lower() not in public]
    by_key: dict[str, list[tuple[int, str]]] = {}
    for no, name, demo in rows:
        by_key.setdefault(name.lower(), []).append((no, demo))
    clash = sorted(no for group in by_key.values() if len({d.lower() for _, d in group}) > 1 for no, _ in group)
    if clash:
        raise ToolError(f"이름 표의 같은 왼쪽 이름(대소문자 무시)에 데모가 둘 이상인 행: {_rows_text(clash)}")
    inner = []
    for no, _, demo in rows:
        low = demo.lower()
        hit = sorted({group[0][0] for key, group in by_key.items() if key in low})
        if hit:
            inner.append(f"{no}(왼쪽 이름 행 {_rows_text(hit)})")
    if inner:
        raise ToolError(f"이름 표의 데모 안에 표 왼쪽 이름이 든 행(자기 매핑 포함): {', '.join(inner)}")
    out, seen = [], set()
    for no, name, demo in rows:
        if (name, demo) not in seen:
            seen.add((name, demo))
            out.append((no, name, demo))
    return out


def is_private_ipv4(octets) -> bool:
    """네 조각(1~3자리 숫자 문자열)이 0~255 이고 PRIVATE_IPV4_NETS 안인가."""
    nums = [int(o) for o in octets]
    if any(n > 255 for n in nums):
        return False
    addr = ipaddress.IPv4Address(".".join(map(str, nums)))
    return any(addr in net for net in PRIVATE_IPV4_NETS)


def private_ipv4_literal(token: str) -> bool:
    """토큰 전체가 점으로 이은 사설 IPv4 리터럴 하나인가(검토 토큰 IPv4 줄 검사)."""
    mo = IPV4_LITERAL_RE.fullmatch(token)
    return mo is not None and is_private_ipv4(mo.groups())


def private_ipv4_spans(text: str) -> list[tuple[int, int]]:
    """사설 IPv4 네 조각 창의 (시작, 끝). 점으로 이은 숫자 덩어리마다 연속한 네 조각을 하나씩 밀며 본다(창끼리 겹칠 수 있다)."""
    out = []
    for mo in DOTTED_RUN_RE.finditer(text):
        parts, pos = [], mo.start()
        for part in mo.group(0).split("."):
            parts.append((part, pos))
            pos += len(part) + 1
        for i in range(len(parts) - 3):
            win = parts[i:i + 4]
            if all(len(p) <= 3 for p, _ in win) and is_private_ipv4([p for p, _ in win]):
                out.append((win[0][1], win[3][1] + len(win[3][0])))
    return out


def reviewed_tokens_path(value: str | None, repo: Path) -> Path | None:
    """--reviewed-tokens 경로. 인자가 없으면 기본 경로이고 그 파일이 없으면 None(빈 목록). 명시한 경로가 없거나
    기본 경로가 깨진 심링크면 오류(입력 누락이 오탐 회귀처럼 보이지 않게). 파일이 있으면 리포 밖이어야 한다."""
    p = Path(value if value is not None else DEFAULT_REVIEWED_TOKENS).expanduser()
    if value is None and p.is_symlink() and not p.exists():
        raise ToolError(f"--reviewed-tokens 기본 경로가 깨진 심링크: {p}")
    if value is None and not p.exists():
        return None
    if not p.is_file():
        raise ToolError(f"--reviewed-tokens 파일 없음: {p}")
    rp = p.resolve()
    if rp == repo or repo in rp.parents:
        raise ToolError("--reviewed-tokens 는 리포 밖 파일이어야 한다")
    return rp


def load_reviewed_tokens(path: Path | None, names: list[tuple[int, str]]) -> list[tuple[int, str, str]]:
    """검토 토큰 파일 검사(lint) 뒤 (줄 번호, 토큰, 소문자 이름) 목록. 한 줄 '<토큰>\t<표 왼쪽 이름>', '#' 주석.
    실패(exit 2)는 줄 번호만 낸다: 형식 오류(칸이 둘이 아니거나 빈 칸), 이름이 표에 없음(대소문자 무시),
    토큰이 그 이름의 어떤 구분자 변형도 (대소문자 무시) 품지 않음, 토큰이 REVIEWED_MAX_TOKEN 자 초과.
    키는 표 행 번호가 아니라 이름이다(행 번호는 표 편집에 밀린다).
    둘째 칸이 'private-ipv4'(대소문자 무시)인 줄은 사설 IPv4 면제 줄이다(#456): 이름 lint 둘(표에 없는 이름,
    이름을 포함하지 않는 토큰)에서 빠지고, 토큰이 점으로 이은 사설 IPv4 리터럴 하나가 아니면 실패다.
    이 줄은 (줄 번호, 토큰, 'private-ipv4') 로 돌려준다."""
    if path is None:
        return []
    keys = {name.lower() for _, name in names}
    rxs: dict[str, re.Pattern[str]] = {}
    bad: dict[str, list[int]] = {"형식 오류": [], "표에 없는 이름": [], "이름을 포함하지 않는 토큰": [],
                                 f"{REVIEWED_MAX_TOKEN}자 초과": [], "사설 IPv4 리터럴이 아닌 토큰": []}
    out = []
    for no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [x.strip() for x in line.split("\t")]
        if len(parts) != 2 or not all(parts):
            bad["형식 오류"].append(no)
            continue
        token, key = parts[0], parts[1].lower()
        if key == REVIEWED_IPV4_KEY:
            if not private_ipv4_literal(token):
                bad["사설 IPv4 리터럴이 아닌 토큰"].append(no)
            out.append((no, token, key))
            continue
        if key not in keys:
            bad["표에 없는 이름"].append(no)
            continue
        rx = rxs.setdefault(key, _variants_rx([v for v, _ in name_variants(key)]))
        if rx.search(token) is None:
            bad["이름을 포함하지 않는 토큰"].append(no)
        if len(token) > REVIEWED_MAX_TOKEN:
            bad[f"{REVIEWED_MAX_TOKEN}자 초과"].append(no)
        out.append((no, token, key))
    failed = [f"{k}: {_rows_text(v)}" for k, v in bad.items() if v]
    if failed:
        raise ToolError("검토 토큰 파일 검사 실패: " + "; ".join(failed))
    return out


def strict_b64_text(chunk: str) -> str | None:
    """엄격 base64 해독(validate=True, 길이·문자 검사) -> 엄격 UTF-8 해독. 어느 쪽이든 실패하면 None."""
    try:
        return base64.b64decode(chunk, validate=True).decode("utf-8")
    except ValueError:                           # binascii.Error, UnicodeDecodeError 모두 ValueError
        return None


# ---------------------------------------------------------------- 표 누락 검사 (원본 이력)

def _strip_prefix(v: str) -> str:
    return v.split(":", 1)[1] if ":" in v else v


def _walk(node, key: str):
    """node 안의 모든 dict 에서 key 의 값."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                yield v
            yield from _walk(v, key)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v, key)


def policy_values(doc) -> set[str]:
    """뱅크 이름, 별칭 키·값, project 어휘, 트리거 id·bank."""
    out: set[str] = set()
    for banks in _walk(doc, "banks"):
        if isinstance(banks, dict):
            out.update(k for k in banks if isinstance(k, str))
    for al in _walk(doc, "tag_aliases"):
        if isinstance(al, dict):
            for k, v in al.items():
                out.add(k)
                if isinstance(v, str):
                    out.add(v)
    for voc in _walk(doc, "tag_vocabulary"):
        if isinstance(voc, dict) and isinstance(voc.get("project"), list):
            out.update(x for x in voc["project"] if isinstance(x, str))
    for trig in _walk(doc, "triggers"):
        if isinstance(trig, list):
            for t in trig:
                if isinstance(t, dict):
                    out.update(t[k] for k in ("id", "bank") if isinstance(t.get(k), str))
    return {s for s in (_strip_prefix(v).strip() for v in out) if s}


def _all_strings(node, out: set[str]) -> None:
    if isinstance(node, str):
        out.add(node)
        out.add(_strip_prefix(node))
    elif isinstance(node, dict):
        for k, v in node.items():
            _all_strings(k, out)
            _all_strings(v, out)
    elif isinstance(node, list):
        for v in node:
            _all_strings(v, out)


def _json_blobs(repo: Path, shas: list[str]) -> tuple[list, int]:
    """(파싱한 문서, JSON 으로 읽지 못한 판본 수)."""
    docs, bad = [], 0
    for raw in cm._read_blobs(repo, shas).values():
        try:
            docs.append(json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, ValueError):
            bad += 1
    return docs, bad


def table_missing(src: Path, sha: str, split_sha: str, left: set[str],
                  public: frozenset[str] = frozenset()) -> tuple[int, int, int, int]:
    """(표에 없는 값 개수, 대상 값 개수, 대상 판본 수, JSON 으로 읽지 못한 대상 판본 수). 값 자체는 돌려주지 않는다.
    비교는 대소문자 무시(소문자화한 값의 개수)다. 데모 fixture 에 그대로 있는 값과 public_words 는 대상에서 뺀다.
    읽지 못한 판본은 값을 뽑을 수 없으므로 검사 실패로 센다(조용히 건너뛰면 그 판본에만 있는 이름이 빠진다)."""
    shas = [s for p, s in cm.history_pairs(src, sha) if cm.path_matches(OLD_FIXTURE, p)]
    parents = git(src, "rev-parse", f"{split_sha}^@").split()
    if parents:
        shas += [s for p, s in cm.history_pairs(src, *parents) if p in SPLIT_POLICY_FILES]
    shas = sorted(set(shas))
    values: set[str] = set()
    docs, n_unreadable = _json_blobs(src, shas)
    for doc in docs:
        values |= policy_values(doc)
    sample: set[str] = set()
    rows = [s for p, s in cm._tree_files(src, sha) if cm.path_matches(SAMPLE_FIXTURE, p)]
    for doc in _json_blobs(src, rows)[0]:
        _all_strings(doc, sample)
    sample_l = {v.lower() for v in sample}
    targets = {v.lower() for v in values} - sample_l - set(public)
    return len(targets - {x.lower() for x in left}), len(targets), len(shas), n_unreadable


# ---------------------------------------------------------------- 산출 리포 검사

def _accepted_after_export(m: dict, path: str, pid: str) -> bool:
    """내보낸 뒤에는 handling 이 export-rewrite 인 accept 를 쓰지 않는다(그 경로는 치환으로 없어져야 한다)."""
    return any(a["pattern"] == pid and a.get("handling") != "export-rewrite" and cm.path_matches(a["path"], path)
               for a in m["accept"])


def name_variants(key: str) -> list[tuple[str, str]]:
    """(변형, 구분자). 유출 검사가 찾는 표 이름의 꼴: 원래 철자, 하이픈이 있으면 하이픈을 '_'로 바꾼 꼴과 없앤 꼴.
    치환 규칙은 원래 철자만 쓴다(변형 치환은 표 소유자가 행으로 더한다)."""
    out = [(key, "-")]
    if "-" in key:
        out += [(key.replace("-", "_"), "_"), (key.replace("-", ""), "")]
    return list(dict.fromkeys(out))


def _variants_rx(variants: list[str]) -> re.Pattern[str]:
    return re.compile("|".join(map(re.escape, sorted(set(variants), key=lambda v: (-len(v), v)))), re.I)


def drop_exported_from(msg: str) -> str:
    """도구가 cb_commit 으로 더한 마지막 'Exported-From: <sha>' 줄을 뺀 메시지(그 밖은 그대로)."""
    body = msg.strip("\n")
    head, _, last = body.rpartition("\n")
    return head if EXPORTED_FROM_RE.match(last) else msg


class Scanner:
    """독립 유출 검사: 표면(blob·메시지·경로·작성자)마다 검사기별 건수.

    이름은 치환 정규식(cm.rename_regex)을 쓰지 않고 표 왼쪽 열 모든 이름을 경계 없는 부분 문자열, 대소문자 무시로 센다
    (치환이 놓친 것을 같은 규칙으로 다시 놓치지 않게). 허용 목록은 없다. 예외는 다섯뿐이다.
      - 작성자·커미터 표면에서 mailmap 첫째 칸(대상) 이름·주소와 정확히 같은 값(도구가 mailmap 으로 만든 공개 신원,
        이름과 주소를 따로 비교).
      - base64 해독 재검사(#441·#448, blob 표면만, 검토 토큰보다 먼저): B64_MIN_CHUNK 자 넘는 base64 덩어리는 이름 적중
        여부와 상관없이 엄격 해독(base64 -> UTF-8)해 같은 검사기(이름·local-deny·mailmap 원주소·deny_patterns, 원 blob
        경로)로 한 단계만 다시 검사한다(인코딩된 이름 사각지대 방지). 해독본 적중은 원래 라벨로 실패로 센다. 해독본이
        깨끗하면 덩어리 안 이름 적중을 encoded-checked 로 따로 세고, 해독본에 적중이 있으면 덩어리 안 적중도 실패로 센다.
        해독이 안 되면 원문 적중이 없는 덩어리는 아무것도 세지 않고, 덩어리 안 적중은 검토 토큰 판정으로 넘어간다.
        단 전체 해독이 실패하면 앞 1~3자를 뗀 해독본도 같은 검사기로 보고, 적중이 있으면 위 dirty 와 같이 실패로 센다.
      - 검토 토큰(#441, blob·메시지 표면만): 적중을 감싼 영숫자 토큰이 검토 토큰과 대소문자까지 같고 그 줄의 이름이
        적중한 이름일 때만 reviewed 로 따로 센다. 토큰이 그 이름(구분자 변형 포함, 대소문자 무시)과 같으면 빼지 않는다.
      - public_literals(#459): 그 정확한 문자열 범위 안 적중(이름·이름 밖 검사기 모두)을 public-literal 로 따로 센다.
      - public_words(#468): 그 단어의 온전한 출현(대소문자 무시, 앞뒤가 [A-Za-z0-9]가 아님) 범위 안에 완전히 든 이름
        적중만 public-word(표 행 번호 없음)로 따로 센다. 범위를 걸친 적중, 이름 밖 검사기, base64 해독본은 그대로 실패다.
    적중별 면제는 이름 정규식 위치(finditer)로 판정한다. 위치 없이 lower() 셈으로만 잡힌 적중은 면제하지 않는다.
    면제 건수(exempt)는 실패 건수(counts)와 따로 두고 total() 에 더하지 않는다."""

    def __init__(self, m: dict, local_deny: list, names: list[tuple[int, str]], mail_sources: set[str],
                 keep_names: set[str] = frozenset(), keep_emails: set[str] = frozenset(),
                 keep_pairs: set[tuple[str, str]] = frozenset(), reviewed: list[tuple[int, str, str]] = ()):
        self.m = m
        self.plain = [("local-deny", rx) for rx in local_deny]
        self.plain += [("mailmap-source", re.compile(re.escape(e), re.I)) for e in sorted(mail_sources)]
        first: dict[str, int] = {}
        for no, name in names:
            first.setdefault(name.lower(), no)
        self.names = sorted(first.items(), key=lambda kv: (kv[1], kv[0]))     # (소문자 이름, 표 행 번호)
        # 경계 없는 리터럴, re.I(치환 정규식과 같은 대소문자 접기라 İ·ı·ſ 변형도 센다). lower() 셈과 합쳐 본다.
        # 하이픈이 든 이름은 구분자 변형(_, 없음)도 같은 표 행으로 센다.
        self.name_rx = {key: _variants_rx([v for v, _ in name_variants(key)]) for key, _ in self.names}
        self.local_deny = list(local_deny)
        self.keep_pairs = {(n, e.strip().lower()) for n, e in keep_pairs}
        self.keep_names = set(keep_names)
        self.keep_emails = {e.strip().lower() for e in keep_emails}
        self.counts: dict[tuple[str, str], int] = {}
        self.exempt: dict[tuple[str, str], int] = {}     # (표면, reviewed:N | encoded-checked:N) -> 건수. 실패에 넣지 않는다
        self.tokens: set[str] = set()           # 남은 이름 적중을 감싼 영숫자 토큰(--suggest-rows 용, 출력하지 않는다)
        self.reviewed: dict[tuple[str, str], int] = {}   # (토큰, 소문자 이름) -> 검토 토큰 파일 줄 번호(같은 줄은 첫 줄)
        self.reviewed_ipv4: dict[str, int] = {}           # 사설 IPv4 면제 리터럴 -> 줄 번호(#456)
        for no, token, key in reviewed:
            if key == REVIEWED_IPV4_KEY:
                self.reviewed_ipv4.setdefault(token, no)
            else:
                self.reviewed.setdefault((token, key), no)
        self.ipv4_counts: dict[str, int] = {}             # 표면 -> 사설 IPv4 적중(이름 유출 counts 와 따로)
        self.ipv4_exempt: dict[str, int] = {}             # 표면 -> 검토 토큰 IPv4 줄로 뺀 건수
        self.ipv4_paths: dict[str, int] = {}              # blob 경로 -> 사설 IPv4 적중
        self.reviewed_lines = len(reviewed)
        self.reviewed_used: set[int] = set()
        self.lit_rx = literal_regex(m.get("public_literals", ()))    # 그 정확한 문자열 범위 안 적중만 뺀다(public-literal)
        self.word_rx = public_word_regex(m.get("_public", ()))       # 그 온전한 출현 범위 안 이름 적중만 뺀다(public-word)

    def _add(self, surface: str, label: str, n: int) -> None:
        if n:
            self.counts[(surface, label)] = self.counts.get((surface, label), 0) + n

    def _exempt(self, surface: str, label: str) -> None:
        self.exempt[(surface, label)] = self.exempt.get((surface, label), 0) + 1

    @staticmethod
    def _token(text: str, s: int, e: int) -> str:
        """적중 [s, e) 를 감싼 영숫자 토큰(--suggest-rows 제안과 검토 토큰 비교가 같은 규칙을 쓴다)."""
        while s > 0 and text[s - 1] in ALNUM:
            s -= 1
        while e < len(text) and text[e] in ALNUM:
            e += 1
        return text[s:e]

    def _hits(self, text: str, path: str | None, spans: list[tuple[int, int]] = (),
              surface: str | None = None) -> dict[str, int]:
        """면제 없는 검사기별 건수(이름·local-deny·mailmap 원주소·deny_patterns). base64 해독본 검사에 쓴다.
        spans(public_literals 범위)를 주면 이름 밖 검사기의 그 범위 안 적중은 빼고 면제(public-literal)로 센다."""
        out: dict[str, int] = {}
        low = text.lower()
        for key, no in self.names:
            n = len(self.name_rx[key].findall(text)) or low.count(key)
            if n:
                out[f"rename-table:{no}"] = n

        def count(rx) -> int:
            if not spans:
                return len(rx.findall(text))
            n = 0
            for mo in rx.finditer(text):
                if _inside(spans, *mo.span()):
                    self._exempt(surface, "public-literal")
                else:
                    n += 1
            return n

        for label, rx in self.plain:
            n = count(rx)
            if n:
                out[label] = out.get(label, 0) + n
        if path is None or path not in cm.SELF_PATHS:
            for pid, rx in self.m["_deny"]:
                if path is not None and _accepted_after_export(self.m, path, pid):
                    continue
                n = count(rx)
                if n:
                    out["deny_patterns"] = out.get("deny_patterns", 0) + n
        return out

    def _reviewed_line(self, token: str, key: str) -> int | None:
        if self.name_rx[key].fullmatch(token):           # 토큰이 이름(변형 포함, 대소문자 무시)과 같으면 빼지 않는다
            return None
        return self.reviewed.get((token, key))

    def text(self, surface: str, text: str, path: str | None = None) -> int:
        total = 0
        low = text.lower()
        found: dict[str, list[re.Match[str]]] = {}
        for key, no in self.names:
            ms = list(self.name_rx[key].finditer(text))
            if ms:
                found[key] = ms
            else:
                n = low.count(key)                       # 위치 없는 대체 셈(re.I 와 lower() 접기 차이): 면제하지 않는다
                if n:
                    self._add(surface, f"rename-table:{no}", n)
                    total += n
        chunks: list[tuple[int, int]] = []
        verdicts: list[str] = []                         # 덩어리 순번 -> clean | dirty | undecodable
        if surface == "blob":
            # 원문 적중이 없는 덩어리도 해독해 본다(#448, 인코딩된 이름 사각지대). 해독본 적중은 _chunk_verdict 가
            # 실패에 더하고, 해독 불가는 원문 적중이 없으면 아무것도 세지 않는다.
            chunks = [mo.span() for mo in B64_CHUNK_RE.finditer(text) if mo.end() - mo.start() > B64_MIN_CHUNK]
            for cs, ce in chunks:
                verdict, n_decoded = self._chunk_verdict(surface, text[cs:ce], path)
                verdicts.append(verdict)
                total += n_decoded
        starts = [c[0] for c in chunks]
        lit = literal_spans(self.lit_rx, text)
        words = literal_spans(self.word_rx, text)       # 이름 적중에만 쓴다(_hits·해독본 재검사에는 넘기지 않는다)
        for key, no in self.names:
            for mo in found.get(key, ()):
                s, e = mo.span()
                if _inside(lit, s, e):                   # public_literals 정확한 문자열 범위 안 적중만 뺀다
                    self._exempt(surface, f"public-literal:{no}")
                    continue
                if _inside(words, s, e):                 # public_words 온전한 출현 안에 완전히 든 이름 적중만 뺀다
                    self._exempt(surface, "public-word")  # 표 행 번호 없는 라벨(면제 문자열은 공개 추적 이슈 댓글에 실린다)
                    continue
                i = bisect.bisect_right(starts, s) - 1
                if i >= 0 and e <= chunks[i][1]:
                    if verdicts[i] == "clean":
                        self._exempt(surface, f"encoded-checked:{no}")
                        continue
                    if verdicts[i] == "dirty":
                        self._add(surface, f"rename-table:{no}", 1)
                        self.tokens.add(self._token(text, s, e))
                        total += 1
                        continue
                token = self._token(text, s, e)
                line = self._reviewed_line(token, key) if surface in ("blob", "message") else None
                if line is not None:
                    self.reviewed_used.add(line)
                    self._exempt(surface, f"reviewed:{no}")
                    continue
                self._add(surface, f"rename-table:{no}", 1)
                self.tokens.add(token)
                total += 1
        for label, n in self._hits(text, path, lit, surface).items():
            if not label.startswith("rename-table:"):
                self._add(surface, label, n)
                total += n
        return total

    def _chunk_verdict(self, surface: str, chunk: str, path: str | None) -> tuple[str, int]:
        """(clean | dirty | undecodable, 해독본 적중 수). 해독본 적중(dirty)은 여기서 원래 라벨로 실패에 더한다.
        원문 적중이 없는 덩어리에도 부른다(#448). undecodable 은 아무것도 세지 않는다.
        재귀 없음: 해독본은 _hits 로만 보므로 그 안의 base64 는 다시 풀지 않는다."""
        decoded = strict_b64_text(chunk)
        if decoded is None:
            # 덩어리 앞에 붙은 글자(JSON `\n` 의 n, `\/` 의 / 등)로 4자 경계가 어긋난 경우: 앞 1~3자를 뗀 해독본도
            # 같은 검사기로 본다. 적중이 있을 때만 실패(dirty)로 세고, 깨끗해도 면제(clean)에는 쓰지 않는다(fail-closed).
            for k in (1, 2, 3):
                alt = strict_b64_text(chunk[k:])
                hits = self._hits(alt, path) if alt is not None else {}
                if hits:
                    for label, n in hits.items():
                        self._add(surface, label, n)
                    return "dirty", sum(hits.values())
            return "undecodable", 0
        hits = self._hits(decoded, path)
        for label, n in hits.items():
            self._add(surface, label, n)
        return ("dirty" if hits else "clean"), sum(hits.values())

    def private_ipv4(self, surface: str, text: str, path: str | None = None) -> int:
        """사설 IPv4 리터럴 적중 수(검토 토큰 IPv4 줄과 리터럴 전체가 정확히 같은 적중은 면제로 따로 센다).
        이름 유출 검사(counts·total)와 섞지 않는다. 작성자 표면에는 부르지 않는다."""
        n = 0
        for s, e in private_ipv4_spans(text):
            line = self.reviewed_ipv4.get(text[s:e])
            if line is not None:
                self.reviewed_used.add(line)
                self.ipv4_exempt[surface] = self.ipv4_exempt.get(surface, 0) + 1
                continue
            n += 1
        if n:
            self.ipv4_counts[surface] = self.ipv4_counts.get(surface, 0) + n
            if surface == "blob" and path is not None:
                self.ipv4_paths[path] = self.ipv4_paths.get(path, 0) + n
        return n

    def ipv4_total(self) -> int:
        return sum(self.ipv4_counts.values())

    def reviewed_unused(self) -> int:
        """한 번도 면제에 쓰이지 않은 검토 토큰 줄 수(실패 아님, 건수만 출력)."""
        return self.reviewed_lines - len(self.reviewed_used)

    def ident(self, name: str, email: str) -> int:
        """작성자·커미터·태거 한 명. mailmap 대상 이름·주소와 정확히 같은 값만 뺀다(이름과 주소를 따로 비교)."""
        n = 0
        if name and name not in self.keep_names:
            n += self.text("ident", name)
        if email and email.strip().lower() not in self.keep_emails:
            n += self.text("ident", email)
        # local-deny 는 '이름 <주소>' 결합 문자열에도 쓴다(이름·주소에 걸친 패턴). 대상 신원과 정확히 같은 결합 값만 뺀다.
        # 이름·주소 각각에서 이미 셀 수 있는 적중을 다시 세지 않도록 결합에서만 생긴 만큼만 더한다.
        if name and email and (name, email.strip().lower()) not in self.keep_pairs:
            joined = f"{name} <{email}>"
            for rx in self.local_deny:
                extra = len(rx.findall(joined)) - len(rx.findall(name)) - len(rx.findall(email))
                if extra > 0:
                    self._add("ident", "local-deny", extra)
                    n += extra
        return n

    def total(self, surface: str | None = None) -> int:
        return sum(n for (s, _), n in self.counts.items() if surface is None or s == surface)

    def mask(self, path: str) -> str:
        """출력할 경로: 이름은 같은 부분 문자열 규칙으로 '<이름>'으로 가리고, 그래도 식별 패턴·원주소·금칙이 맞으면 통째로 숨긴다."""
        rx = cm.mask_names([v for k, _ in self.names for v, _ in name_variants(k)])
        shown = rx.sub("<이름>", path) if rx is not None else path
        merged: list[list[int]] = []                               # 경로 이름 속 사설 주소도 가린다(겹친 창은 합친다)
        for s, e in private_ipv4_spans(shown):
            if merged and s <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        for s, e in reversed(merged):
            shown = shown[:s] + "<사설IPv4>" + shown[e:]
        if any(r.search(shown) for _, r in self.plain) or any(r.search(shown) for _, r in self.m["_deny"]):
            return "(경로 이름 자체가 적중, 비공개)"
        return shown


def suggest_rows(tokens: set[str], table: NameTable) -> tuple[list[str], int]:
    """(제안 행, 너무 길어 뺀 토큰 수). 행: '<토큰>\t<이름 부분만 데모로 바꾼 토큰>'(이름 길이 SHORT_NAME 이하면 셋째 칸 short).
    토큰이 표의 이름과 (대소문자 무시로) 같으면 표가 이미 덮으므로 뺀다(그 적중은 치환 범위 문제다).
    하이픈 이름의 구분자 변형(_, 없음)은 데모의 하이픈을 같은 구분자로 바꿔 꼴을 옮긴다."""
    keys = set(table.first)
    variants = [(v, k, sep) for k in table.first for v, sep in name_variants(k)]
    rx = _variants_rx([v for v, _, _ in variants]) if variants else None

    def demo_of(matched: str) -> str:
        for v, k, sep in variants:
            if re.fullmatch(re.escape(v), matched, re.I):
                return table.demo_for(matched) if sep == "-" else shape(matched, table.first[k].replace("-", sep))
        return matched

    rows, too_long = set(), 0
    for tok in tokens:
        if tok.lower() in keys or rx is None:
            continue
        if len(tok) > SUGGEST_MAX_TOKEN:
            too_long += 1
            continue
        short = False

        def sub(mo):
            nonlocal short
            if len(mo.group(0)) <= SHORT_NAME:
                short = True
            return demo_of(mo.group(0))

        new = rx.sub(sub, tok)
        if new != tok:
            rows.add(f"{tok}\t{new}" + ("\tshort" if short else ""))
    return sorted(rows), too_long


def write_private(path: Path, text: str) -> None:
    """권한 600 으로 쓴다(있으면 덮어쓴다)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)                         # 있던 파일(0644 등)도 내용을 쓰기 전에 600 으로
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.chmod(path, 0o600)


def scan_repo(repo: Path, sc: Scanner) -> list[tuple[str, int]]:
    """산출 리포 전 이력. blob 적중 경로 목록(경로 이름 자체가 적중하면 이름을 숨긴다)을 돌려준다."""
    pairs = sorted(cm.history_pairs(repo, "refs/heads/main"))
    blobs = cm._read_blobs(repo, [s for _, s in pairs])
    hit_paths: dict[str, int] = {}
    for path, sha in pairs:
        raw = blobs.get(sha)
        if raw is None:
            continue
        text = raw.decode("utf-8", "replace")
        n = sc.text("blob", text, path)                  # 이진 blob 도 훑는다
        if n:
            hit_paths[path] = hit_paths.get(path, 0) + n
        sc.private_ipv4("blob", text, path)
    for path in sorted({p for p, _ in pairs}):
        sc.text("path", path)
        sc.private_ipv4("path", path)
    for msg in git(repo, "log", "--format=%B%x00", "refs/heads/main").split("\0"):
        msg = drop_exported_from(msg)                    # 도구가 더한 원본 sha 줄은 세지 않는다
        sc.text("message", msg)
        sc.private_ipv4("message", msg)
    for msg in git(repo, "for-each-ref", "--format=%(contents)%00", "refs/tags/").split("\0"):
        sc.text("message", msg)
        sc.private_ipv4("message", msg)
    for name, email in idents(repo):
        sc.ident(name, email)
    return sorted(hit_paths.items())


def idents(repo: Path) -> list[tuple[str, str]]:
    """작성자·커미터·태거의 (이름, 주소) 집합."""
    out: set[tuple[str, str]] = set()
    log = git(repo, "log", "--format=%an%x00%ae%x00%cn%x00%ce%x01", "refs/heads/main")
    tags = git(repo, "for-each-ref", "--format=%(taggername)%00%(taggeremail:trim)%01", "refs/tags/")
    for rec in log.split("\x01") + tags.split("\x01"):
        f = rec.lstrip("\n").split("\0")
        for i in range(0, len(f) - 1, 2):
            if f[i].strip() or f[i + 1].strip():
                out.add((f[i].strip(), f[i + 1].strip()))
    return sorted(out)


def bad_emails(repo: Path, targets: set[str]) -> int:
    emails = set()
    log = git(repo, "log", "--format=%ae%x00%ce%x00", "refs/heads/main")
    emails |= {e.strip() for e in log.split("\0") if e.strip()}
    tags = git(repo, "for-each-ref", "--format=%(taggeremail:trim)%00", "refs/tags/")
    emails |= {e.strip() for e in tags.split("\0") if e.strip()}
    return sum(1 for e in emails if not email_allowed(e, targets))


def exclusion_counts(repo: Path, m: dict, rev: str = "refs/heads/main") -> tuple[int, int]:
    """(exclude_history 경로 수, include 밖 경로 수) - 걸러진 이력에서 둘 다 0 이어야 한다."""
    paths = {p for p, _ in cm.history_pairs(repo, rev)}
    return (sum(1 for p in paths if cm.excluded(m, p)),
            sum(1 for p in paths if not cm.included(m, p)))


def exported_from_mismatches(repo: Path, source_commits: set[str]) -> tuple[int, int]:
    """(Exported-From 이 없거나 원본과 다른 커밋 수, 전체 커밋 수). 원본은 filter-repo commit-map 으로 대조한다."""
    new_to_old = {}
    cmap = Path(git(repo, "rev-parse", "--git-dir").strip())
    if not cmap.is_absolute():
        cmap = repo / cmap
    for line in (cmap / "filter-repo" / "commit-map").read_text(encoding="utf-8").splitlines()[1:]:
        old, new = line.split()
        if new != ZERO_SHA:
            new_to_old[new] = old
    bad = total = 0
    for rec in git(repo, "log", "--format=%H%x00%B%x01", "refs/heads/main").split("\x01"):
        rec = rec.lstrip("\n")
        if not rec:
            continue
        total += 1
        h, _, body = rec.partition("\0")
        # cb_commit 이 메시지 마지막 줄에 더한 trailer 만 본다(원본 본문에 같은 꼴의 줄이 있어도 셈에 넣지 않는다).
        mo = EXPORTED_FROM_RE.match(body.rstrip("\n").rsplit("\n", 1)[-1])
        if mo is None or mo.group(1) != new_to_old.get(h) or mo.group(1) not in source_commits:
            bad += 1
    return bad, total


def run_gitleaks(repo: Path, config: Path, tmp: Path) -> int:
    report = tmp / "gitleaks.json"
    # 기본 git log -p 는 병합 커밋 diff 를 내지 않는다. 충돌 해결로 병합에서 처음 들어간 비밀도 보도록 첫 부모와 비교한다.
    r = _run(["gitleaks", "git", str(repo), "--config", str(config), "--redact", "--no-banner",
              "--log-opts=--full-history --all --diff-merges=first-parent",
              "--exit-code", "0", "--log-level", "error", "--report-format", "json", "--report-path", str(report)],
             cwd=repo)                              # 실행 위치의 .gitleaksignore 가 결과를 지우지 않게
    if r.returncode:
        raise ToolError(f"gitleaks 실행 오류(rc={r.returncode})")
    try:
        findings = json.loads(report.read_text(encoding="utf-8") or "[]")
    except (OSError, ValueError):
        raise ToolError("gitleaks 보고서를 읽지 못함")
    return len(findings)


def is_ancestor(repo: Path, prev_public: Path) -> tuple[bool, str]:
    """--prev-public 클론의 HEAD 를 산출 리포로 가져와 새 HEAD 의 조상인지 본다. 임시 ref 는 지운다."""
    git(repo, "fetch", "-q", "--no-tags", str(prev_public), f"+HEAD:{PREV_REF}")
    try:
        prev = git(repo, "rev-parse", PREV_REF).strip()
        r = _run(["git", "-C", str(repo), "merge-base", "--is-ancestor", prev, "refs/heads/main"])
        if r.returncode not in (0, 1):
            raise ToolError(f"git merge-base 실패(rc={r.returncode})")
        return r.returncode == 0, prev
    finally:
        git(repo, "update-ref", "-d", PREV_REF, check=False)


# ---------------------------------------------------------------- 이전 상태 (export/NNN 태그)

def export_tags(src: Path) -> list[tuple[int, str, str]]:
    """운영 리포의 export/NNN 태그: (번호, 이름, 커밋 sha) 번호순."""
    rows = []
    out = git(src, "for-each-ref", "--format=%(refname:strip=2)%00%(objectname)%00%(*objectname)", "refs/tags/export/")
    for line in out.splitlines():
        name, obj, peeled = (line.split("\0") + ["", ""])[:3]
        mo = EXPORT_TAG_RE.match(name)
        if mo:
            rows.append((int(mo.group(1)), name, peeled or obj))
    return sorted(rows)


# ---------------------------------------------------------------- 실행

def _filter(repo: Path, tmp: Path, cfg: dict, drop: list[str], renames: list[tuple[str, str]], mailmap: Path) -> None:
    config = tmp / "rewrite.json"
    config.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    config.chmod(0o600)
    cbs = {}
    for name, call in (("file_info", "m.cb_file_info(filename, mode, blob_id, value)"),
                       ("message", "m.cb_message(message)"),
                       ("commit", "m.cb_commit(commit)")):
        p = tmp / f"cb_{name}.py"
        p.write_text(_callback_body(config, call), encoding="utf-8")
        cbs[name] = str(p)
    cmd = ["git", "-C", str(repo), "filter-repo", "--force", "--quiet",
           "--file-info-callback", cbs["file_info"], "--message-callback", cbs["message"],
           "--commit-callback", cbs["commit"], "--mailmap", str(mailmap),
           "--prune-empty", "auto", "--prune-degenerate", "auto"]
    if drop or renames:
        # 뺄 경로(filter) 먼저, 경로 이름 치환(rename) 뒤. --invert-paths 라 filter 에 걸린 경로만 빠진다.
        # rename 은 삭제 기록에도 걸리므로 치환된 경로가 나중에 지워진 이력도 그대로 지워진다.
        enc = lambda x: x.encode("utf-8", "surrogateescape")  # noqa: E731
        lines = [b"literal:" + enc(p) for p in drop] + [b"literal:" + enc(o) + b"==>" + enc(n) for o, n in renames]
        pf = tmp / "path-changes.txt"
        pf.write_bytes(b"".join(x + b"\n" for x in lines))
        cmd += ["--paths-from-file", str(pf), "--invert-paths"]
    r = _run(cmd)
    if r.returncode:
        raise ToolError(f"git filter-repo 실행 오류(rc={r.returncode})")


def path_changes(m: dict, rw: Rewriter, paths: list[str], src: Path | None = None,
                 sha: str | None = None) -> tuple[list[str], list[tuple[str, str]]]:
    """(뺄 경로, (원래 경로, 치환 경로)). 뺄 경로는 include 밖과 exclude_history, 치환은 history_rewrite_paths 안의
    경로 이름에 blob 과 같은 규칙(이름 표 -> message_rewrites)을 쓴다."""
    drop = [p for p in paths if not cm.included(m, p) or cm.excluded(m, p)]
    dropset = set(drop)
    keep = [p for p in paths if p not in dropset]
    renames = [(p, rw.text(p)) for p in keep if rw.in_scope(p) and rw.text(p) != p]
    for p in drop + [x for pair in renames for x in pair]:
        if "\n" in p or "\r" in p or "==>" in p:
            raise ToolError("filter-repo 경로 목록에 쓸 수 없는 문자가 든 경로가 있다")
    special = dropset | {o for o, _ in renames}
    for p in keep:                                # literal 경로는 디렉터리 접두로도 걸린다
        parts = p.split("/")
        if any("/".join(parts[:i]) in special for i in range(1, len(parts))):
            raise ToolError("빼거나 이름을 바꿀 경로가 남길 경로의 디렉터리 접두와 같다")
    final = dict(renames)
    groups: dict[str, set[str]] = {}
    for p in keep:
        groups.setdefault(final.get(p, p), set()).add(p)
    clashes = [g for g in groups.values() if len(g) > 1]
    if clashes:
        # 치환 뒤 이름이 같아도 한 커밋 트리에 함께 있지 않으면(예: 데모 이름으로 git mv 된 이력) filter-repo 가 처리한다.
        # 같은 트리에 함께 있을 때만 막는다(나중 쓰기가 앞 파일을 덮는다).
        if src is None or sha is None:
            raise ToolError("경로 이름 치환 뒤 서로 다른 경로가 겹친다")
        for c in git(src, "rev-list", sha).split():
            present = set(git(src, "ls-tree", "-r", "--name-only", "-z", c).split("\0"))
            if any(len(present & g) > 1 for g in clashes):
                raise ToolError("경로 이름 치환 뒤 같은 커밋 안의 서로 다른 경로가 겹친다")
    return drop, renames


def structural_stats(src: Path, m: dict, rw: Rewriter, source_pairs) -> dict:
    """구조 치환(#459)이 남길 이력의 판본 건수. 콜백과 같은 함수(rw.data -> rw.structural)를 원본 blob 에 돌려 센다
    (값은 세지 않는다). 원래부터 JSON 으로 읽히지 않는 정책·트리거·매니페스트 판본은 그대로 두고 건수만 센다.
    이름 표·message_rewrites 치환 전에는 읽히던 판본이 치환 뒤 읽히지 않으면 "_broken"(경로)에 모은다(검사 실패)."""
    st = {k: 0 for k in ("policy_versions", "policy_changed", "policy_fields", "policy_unreadable",
                         "trigger_versions", "trigger_changed", "trigger_fields", "trigger_unreadable",
                         "doc_versions", "doc_sections", "manifest_versions", "manifest_keys", "manifest_unreadable",
                         "broken")}
    broken: set[str] = set()
    todo = []
    for p, blob in sorted(source_pairs):
        if not cm.included(m, p) or cm.excluded(m, p):
            continue
        final = rw.text(p) if rw.in_scope(p) else p
        kind = structural_kind(final)
        if kind is not None:
            todo.append((final, blob, kind))
    blobs = cm._read_blobs(src, sorted({b for _, b, _ in todo}))
    for path, blob, kind in todo:
        raw = before = blobs[blob]
        if b"\0" in raw[:8192]:
            continue
        if rw.in_scope(path):
            raw = rw.data(raw)
        new, n, ok = rw.structural(kind, raw, path)
        st[f"{kind}_versions"] += 1
        if kind in ("policy", "trigger", "manifest") and not ok and _load_object(before)[1]:
            st["broken"] += 1
            broken.add(path)
            continue
        if kind in ("policy", "trigger"):
            st[f"{kind}_changed"] += new != raw
            st[f"{kind}_fields"] += n
            st[f"{kind}_unreadable"] += not ok
        elif kind == "doc":
            st["doc_sections"] += n
        else:
            st["manifest_keys"] += n
            st["manifest_unreadable"] += not ok
    st["_broken"] = sorted(broken)
    return st


def structural_text(st: dict) -> str:
    return (f"정책 판본 {st['policy_versions']}(바뀐 판본 {st['policy_changed']}, 자유 문장 필드 {st['policy_fields']}, "
            f"JSON 아닌 판본 {st['policy_unreadable']}), 트리거 판본 {st['trigger_versions']}(바뀐 판본 "
            f"{st['trigger_changed']}, 자유 문장 필드 {st['trigger_fields']}, JSON 아닌 판본 {st['trigger_unreadable']}), "
            f"문서 판본 {st['doc_versions']}(리허설 절 {st['doc_sections']}), "
            f"매니페스트 판본 {st['manifest_versions']}(해독표 키 {st['manifest_keys']}, "
            f"읽지 못한 판본 {st['manifest_unreadable']}), 치환 뒤 깨진 판본 {st['broken']}")


class Check:
    def __init__(self, name: str, ok: bool | None, detail: str):
        self.name, self.ok, self.detail = name, ok, detail

    @property
    def mark(self) -> str:
        return {True: "PASS", False: "FAIL", None: "SKIP"}[self.ok]


def build_comment(*, tag_label: str, range_text: str, head: str, manifest_sha: str, checks: list[Check],
                  versions: tuple[str, str], ok: bool) -> str:
    rows = "\n".join(f"| {c.name} | {c.mark} | {c.detail} |" for c in checks)
    return (f"## 공개판 내보내기 {tag_label}\n\n"
            f"- 원본 범위: {range_text}\n"
            f"- 공개 HEAD: {head}\n"
            f"- 매니페스트 sha256: {manifest_sha}\n"
            f"- 도구: git filter-repo {versions[0]}, gitleaks {versions[1]}\n"
            f"- 검사 결과: {'전부 통과' if ok else '실패 있음'}\n\n"
            "| 검사 | 결과 | 건수 |\n|---|---|---|\n" + rows + "\n")


def export(a: argparse.Namespace) -> int:
    versions = check_tools()
    src = Path(git(Path(a.repo), "rev-parse", "--show-toplevel").strip()).resolve()
    sha = git(src, "rev-parse", "--verify", "--end-of-options", f"{a.source_ref}^{{commit}}").strip()
    local_deny = _local_file("--local-deny", a.local_deny, src)
    rename_table = _local_file("--rename-table", a.rename_table, src)
    mailmap = _local_file("--mailmap", a.mailmap, src)
    reviewed_path = reviewed_tokens_path(a.reviewed_tokens, src)
    try:
        rules_path = cm.local_rules_path(a.local_rules, src)
        local_rules = cm.load_local_rules(rules_path)          # 거르기 전에 검사(lint)
    except cm.LocalRulesError as e:
        raise ToolError(str(e))
    suggest = _outside_path("--suggest-rows", a.suggest_rows, src) if a.suggest_rows else None
    out = Path(a.out).expanduser().resolve()
    if out == src or src in out.parents:
        raise ToolError("--out 은 운영 리포 작업 트리 밖이어야 한다")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ToolError(f"--out 이 비어 있지 않음: {out}")
    if suggest is not None and (suggest == out or out in suggest.parents):
        # 산출 리포(공개 후보)나 comment.md 자리에 실명 토큰 파일이 생기지 않게 한다
        raise ToolError("--suggest-rows 는 --out 밖이어야 한다")
    if suggest is not None and (suggest.is_dir() or not suggest.parent.is_dir()):
        # 파일 자신은 없어도 되지만, 쓰기 실패가 거르기·검사를 다 돈 뒤에야 드러나지 않게 미리 본다
        raise ToolError("--suggest-rows 는 있는 디렉터리 안의 파일 경로여야 한다")
    if suggest is not None and suggest.exists():
        inputs = [local_deny, rename_table, mailmap] + [Path(x).expanduser() for x in (a.manifest, a.gitleaks_config) if x]
        inputs += [p for p in (reviewed_path, rules_path) if p is not None]
        if any(i.exists() and os.path.samefile(suggest, i) for i in inputs):
            # O_TRUNC 로 덮어쓰므로 입력 파일(리포 밖 이름 표 등)을 지우지 않게 막는다
            raise ToolError("--suggest-rows 가 입력 파일과 같은 파일이다")
    split = git(src, "rev-parse", "--verify", "--end-of-options", f"{a.split_rev}^{{commit}}", check=False).strip()
    if not split:
        raise ToolError(f"--split-rev 를 원본에서 찾지 못함: {a.split_rev}")

    deny_pats = cm.load_local_deny(local_deny)
    targets, mail_sources, target_names, target_pairs = load_mailmap(mailmap)

    tmp = Path(tempfile.mkdtemp(prefix="hc-export-"))
    try:
        if a.manifest:
            manifest_bytes = Path(a.manifest).expanduser().read_bytes()
        else:
            manifest_bytes = git(src, "show", f"{sha}:{MANIFEST_REL}", binary=True)
        manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
        mpath = tmp / "manifest.json"
        mpath.write_bytes(manifest_bytes)
        try:
            m = cm.load_manifest(mpath)
        except (ValueError, KeyError, re.error) as e:
            raise ToolError(f"manifest 오류: {e}")
        cm.merge_local_rules(m, local_rules)             # 로컬 규칙은 매니페스트 값 뒤(#456)
        rows = load_rename_pairs(rename_table, m["_public"])
        bad_lits = public_literal_conflicts(m["public_literals"], [n for _, n, _ in rows])
        if bad_lits:
            raise ToolError(f"public_literals 검사 실패: {', '.join(map(str, bad_lits))}번 값이 이름 표 이름과 같거나 "
                            "그 이름 안에 든 더 짧은 문자열이다(이름 자체는 보호할 수 없다)")
        reviewed = load_reviewed_tokens(reviewed_path, [(no, n) for no, n, _ in rows])   # 거르기 전에 검사(lint)
        pairs = [(n, d) for _, n, d in rows]
        left = [n for n, _ in pairs]
        if a.gitleaks_config:
            gl_config = Path(a.gitleaks_config).expanduser()
            if not gl_config.is_file():
                raise ToolError(f"--gitleaks-config 파일 없음: {gl_config}")
        else:
            gl_config = tmp / "gitleaks.toml"
            gl_config.write_bytes(git(src, "show", f"{sha}:{GITLEAKS_REL}", binary=True))

        tags = export_tags(src)
        prev_tag = tags[-1] if tags else None
        next_name = f"export/{(prev_tag[0] if prev_tag else 0) + 1:03d}"
        range_text = (f"{prev_tag[1]} {prev_tag[2]}..{sha}" if prev_tag else f"처음..{sha}")

        # 1. --source-ref 하나만 새 리포로(태그·다른 ref 없음)
        # 거르기 전 원본 이력이 --out 에 남지 않도록 임시 위치에서 거르고, 성공한 뒤에만 --out/repo 로 옮긴다.
        work = tmp / "repo"
        work.mkdir()
        git(work, "init", "-q", "-b", "main")
        git(work, "fetch", "-q", "--no-tags", "--update-head-ok", str(src), f"{sha}:refs/heads/main")

        # 2~4. 경로 제거, 경로 한정 치환, mailmap, 메시지 치환과 Exported-From
        source_pairs = cm.history_pairs(src, sha)
        rw = Rewriter(pairs, m["message_rewrites"], m["history_rewrite_paths"], literals=m["public_literals"])

        def head_rewritten(path: str) -> bytes | None:
            # 치환 값은 HEAD 판본을 blob 과 같은 이름 표 치환을 거친 꼴로 읽는다(HEAD 판본 자신은 그대로 남는다)
            raw = git(src, "show", f"{sha}:{path}", binary=True, check=False) or None
            return rw.data(raw) if raw is not None and rw.in_scope(path) else raw

        final_path = lambda p: rw.text(p) if rw.in_scope(p) else p        # noqa: E731  콜백이 받는 경로
        rw.fixture = freetext_fixture(head_rewritten(FREETEXT_FIXTURE))
        base = trigger_fixture(head_rewritten(TRIGGER_BASE))
        head_files = git(src, "ls-tree", "-r", "--name-only", "-z", sha).split("\0")
        rw.triggers = {"base": base, "paths": {
            final_path(p): {**base, **trigger_fixture(head_rewritten(p))}
            for p in head_files if p and structural_kind(final_path(p)) == "trigger"}}
        rw.keep = {final_path(fx): freetext_fixture(head_rewritten(tpl)) for fx, tpl in KEEP_TEMPLATES.items()}
        drop, renames = path_changes(m, rw, sorted({p for p, _ in source_pairs}), src, sha)
        st = structural_stats(src, m, rw, source_pairs)
        cfg = {"rename": pairs, "message_rewrites": m["message_rewrites"], "rewrite_paths": m["history_rewrite_paths"],
               "fixture": rw.fixture, "public_literals": m["public_literals"], "triggers": rw.triggers,
               "keep": rw.keep}
        _filter(work, tmp, cfg, drop, renames, mailmap)
        out.mkdir(parents=True, exist_ok=True)
        repo = out / "repo"
        shutil.move(str(work), str(repo))
        head = git(repo, "rev-parse", "--verify", "refs/heads/main").strip()

        # 5. 검사
        checks: list[Check] = []
        n_bad = bad_emails(repo, targets)
        checks.append(Check("주소 집합", n_bad == 0, f"허용 밖 주소 {n_bad}"))

        sc = Scanner(m, deny_pats, [(no, n) for no, n, _ in rows], mail_sources,
                     keep_names=target_names, keep_emails=targets, keep_pairs=target_pairs, reviewed=reviewed)
        hit_paths = scan_repo(repo, sc)
        n_suggest = too_long = None
        if suggest is not None:
            lines, too_long = suggest_rows(sc.tokens, rw.table)
            write_private(suggest, "".join(x + "\n" for x in lines))
            n_suggest = len(lines)
        parts = ", ".join(f"{s} {sc.total(s)}" for s in ("blob", "message", "path", "ident"))
        by_label = ", ".join(f"{s}/{lbl} {n}" for (s, lbl), n in sorted(sc.counts.items()))
        exempt = ", ".join(f"{s}/{lbl} {n}" for (s, lbl), n in sorted(sc.exempt.items()))   # 실패에 넣지 않은 면제
        checks.append(Check("독립 유출 검사", sc.total() == 0,
                            f"{sc.total()} ({parts})" + (f"; {by_label}" if by_label else "")
                            + (f"; 면제 {exempt}" if exempt else "")))
        ip_parts = ", ".join(f"{s} {sc.ipv4_counts.get(s, 0)}" for s in ("blob", "message", "path"))
        ip_exempt = ", ".join(f"{s} {sc.ipv4_exempt[s]}" for s in ("blob", "message", "path") if s in sc.ipv4_exempt)
        checks.append(Check("private-ipv4", sc.ipv4_total() == 0,
                            f"{sc.ipv4_total()} ({ip_parts})" + (f"; 면제 {ip_exempt}" if ip_exempt else "")))

        n_missing, n_targets, n_versions, n_unreadable = table_missing(src, sha, split, set(left), m["_public"])
        checks.append(Check("표 누락 검사", n_missing == 0 and n_unreadable == 0,
                            f"표에 없는 값 {n_missing} / 대상 값 {n_targets} (판본 {n_versions}, 읽지 못한 판본 {n_unreadable})"))

        n_excl, n_outside = exclusion_counts(repo, m)
        checks.append(Check("제외 경로", n_excl == 0 and n_outside == 0,
                            f"exclude_history 경로 {n_excl}, include 밖 경로 {n_outside}"))

        n_leaks = run_gitleaks(repo, gl_config, tmp)
        checks.append(Check("gitleaks", n_leaks == 0, f"{n_leaks}"))

        source_commits = set(git(src, "rev-list", sha).split())
        n_mis, n_commits = exported_from_mismatches(repo, source_commits)
        checks.append(Check("Exported-From", n_mis == 0, f"불일치 {n_mis} / 커밋 {n_commits}"))
        checks.append(Check("구조 치환", st["broken"] == 0, f"치환 뒤 JSON 이 깨진 판본 {st['broken']}"))

        ancestry_failed = False
        if a.prev_public:
            ok, prev = is_ancestor(repo, Path(a.prev_public).expanduser())
            ancestry_failed = not ok
            checks.append(Check("조상 검사", ok, f"이전 공개 HEAD {prev} {'조상임' if ok else '조상 아님'}"))
        else:
            checks.append(Check("조상 검사", None, "--prev-public 없음(첫 내보내기)"))

        all_ok = all(c.ok is not False for c in checks)

        # 6. 산출
        tag_label = next_name if a.apply else f"(dry-run, 다음 번호 {next_name})"
        comment = build_comment(tag_label=tag_label, range_text=range_text, head=head, manifest_sha=manifest_sha,
                                checks=checks, versions=versions, ok=all_ok)
        (out / "comment.md").write_text(comment, encoding="utf-8")
        (out / "result.json").write_text(json.dumps({
            "source_sha": sha, "public_head": head, "range": range_text, "manifest_sha256": manifest_sha,
            "next_tag": next_name, "ok": all_ok,
            "checks": [{"name": c.name, "result": c.mark, "detail": c.detail} for c in checks],
            "structural": {**{k: v for k, v in st.items() if k != "_broken"},
                           "broken_paths": [sc.mask(p) for p in st["_broken"]]},
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        print(f"원본: {sha} ({a.source_ref})")
        print(f"매니페스트 sha256: {manifest_sha}")
        print(f"로컬 규칙: {cm.rules_summary(local_rules if rules_path is not None else None)}")
        for c in checks:
            print(f"[{c.mark}] {c.name}: {c.detail}")
        for path, n in hit_paths:
            print(f"  유출 적중 blob 경로: {sc.mask(path)} {n}")
        for path, n in sorted(sc.ipv4_paths.items()):
            print(f"  private-ipv4 적중 blob 경로: {sc.mask(path)} {n}")
        print(f"검토 토큰: 줄 {sc.reviewed_lines}, 쓰이지 않은 줄 {sc.reviewed_unused()}")
        print(f"구조 치환: {structural_text(st)}")
        for path in st["_broken"]:
            print(f"  구조 치환 깨진 판본 경로: {sc.mask(path)}")
        if n_suggest is not None:
            print(f"제안 행: {n_suggest} (긴 토큰 제외 {too_long}, 내용은 --suggest-rows 파일에만)")
        if ancestry_failed:
            print(ANCESTRY_FAIL_MSG)
        print(f"공개 HEAD: {head}")
        print(f"원본 범위: {range_text}")
        print(f"산출 리포: {repo}")
        print(f"댓글 본문: {out / 'comment.md'}")
        if not all_ok:
            print("결과: 실패")
            return 1

        # 8. --apply: 운영 리포에 다음 export/NNN 태그, origin 에만 push
        if a.apply:
            apply_tag(src, next_name, sha, head, manifest_sha)
            print(f"태그: {next_name} -> {sha} (origin push 완료)")
        print("결과: 통과" + ("" if a.apply else " (dry-run, 태그·push 없음)"))
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def apply_tag(src: Path, name: str, sha: str, head: str, manifest_sha: str) -> None:
    ref = f"refs/tags/{name}"
    if git(src, "rev-parse", "--verify", "--quiet", ref, check=False).strip():
        raise ToolError(f"태그가 이미 있음: {name}")
    remote = git(src, "ls-remote", "--tags", "origin", ref)
    if remote.strip():
        raise ToolError(f"origin 에 태그가 이미 있음: {name}")
    msg = f"공개판 내보내기 {name}\n\n원본: {sha}\n공개 HEAD: {head}\n매니페스트 sha256: {manifest_sha}\n"
    git(src, "tag", "-a", name, sha, "-m", msg)
    r = _run(["git", "-C", str(src), "push", "-q", "origin", ref])
    if r.returncode:
        git(src, "tag", "-d", name, check=False)       # push 가 안 됐으면 만든 태그를 되돌린다
        raise ToolError(f"origin 태그 push 실패(rc={r.returncode}): {_last_line(r.stderr)}")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="공개판 내보내기(ADR 0002): --source-ref 를 임시 리포로 가져와 filter-repo 로 거르고 검사한다. 기본 dry-run.")
    ap.add_argument("--source-ref", default="origin/main", help="내보낼 운영 리포 ref (기본 origin/main)")
    ap.add_argument("--out", required=True, help="산출 디렉터리(비어 있거나 없어야 한다). repo/, comment.md, result.json 을 남긴다")
    ap.add_argument("--manifest", help=f"경계 매니페스트 파일 (기본: --source-ref 의 {MANIFEST_REL})")
    ap.add_argument("--local-deny", required=True, help="식별 금칙 패턴 파일(리포 밖, 한 줄 한 정규식, '#' 주석). 검출 전용")
    ap.add_argument("--rename-table", default=DEFAULT_RENAME_TABLE,
                    help=f"이름 치환 표(리포 밖, '<원래>\\t<데모>', '#' 주석, 기본 {DEFAULT_RENAME_TABLE})")
    ap.add_argument("--mailmap", required=True, help="작성자·커미터 주소 치환 mailmap 파일(리포 밖)")
    ap.add_argument("--prev-public", help="직전 공개판 로컬 클론 경로(첫 내보내기면 생략). 그 HEAD 가 새 HEAD 의 조상이어야 한다")
    ap.add_argument("--gitleaks-config", help=f"gitleaks 설정 (기본: --source-ref 의 {GITLEAKS_REL})")
    ap.add_argument("--split-rev", default=DEFAULT_SPLIT_REV,
                    help=f"표 누락 검사에서 정책 파일 판본을 이 커밋 이전으로 한정 (기본 {DEFAULT_SPLIT_REV})")
    ap.add_argument("--suggest-rows",
                    help="남은 이름 적중을 감싼 영숫자 토큰의 표 행 제안('<토큰>\\t<데모로 바꾼 토큰>[\\tshort]')을 쓸 파일"
                         "(리포 밖, 권한 600, 있으면 덮어씀). 내용은 출력하지 않고 건수만 낸다")
    ap.add_argument("--reviewed-tokens",
                    help="독립 유출 검사 검토 토큰 파일(리포 밖, '<토큰>\\t<표 왼쪽 이름>', '#' 주석, "
                         f"기본 {DEFAULT_REVIEWED_TOKENS}, 기본 경로에 없으면 빈 목록). blob·메시지 표면에서 토큰이 "
                         "대소문자까지 같고 이름이 같은 적중만 실패에서 뺀다")
    ap.add_argument("--local-rules",
                    help="로컬 규칙 파일(리포 밖, JSON {\"deny_patterns\": [...], \"message_rewrites\": [...], \"accept\": [...]}, "
                         f"기본 {cm.DEFAULT_LOCAL_RULES}, 기본 경로에 없으면 빈 규칙). 매니페스트 값 뒤에 합친다")
    ap.add_argument("--repo", default=".", help="운영 리포 경로 (기본 .)")
    ap.add_argument("--apply", action="store_true",
                    help="검사 통과 시 운영 리포에 다음 export/NNN 주석 태그를 만들고 origin 에만 push (공개판 push 는 하지 않음)")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    a = parse_args(argv)
    try:
        return export(a)
    except ToolError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 2
    except (OSError, re.error, UnicodeDecodeError, ValueError) as e:
        # 입력 파일 없음·잘못된 정규식·비 UTF-8 입력은 사용 오류(2). 메시지는 패턴·내용이 섞일 수 있어 종류만 낸다.
        print(f"오류: 입력을 읽지 못함({type(e).__name__})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
