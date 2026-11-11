"""상태 회귀 세트 평가 도구 (issue #287, 상위 #284 P0-3).

"이미 끝난 일이 열린 일로 나온다"를 잰다. 정답표는 운영 체크아웃의 `bench/.data/regression-arch-v2.json`
(gitignore, 읽기 전용, 사적 맥락)이고 `--answer-key`로 경로만 받는다. 리포에는 항목 id와 판정만 남긴다.

명령
  evidence  항목마다 keywords 로 recall 해 "뱅크 안 완료 근거"를 찾는다(근거 없는 항목은 세트에서 뺀다).
  score     세 경로로 질의해 항목별 판정(open / not-open / invalid)을 낸다.
              a_reflect_list          MCP reflect 1회(게이트웨이 경유, include_based_on) - 열린 일 나열
              b_knowledge_page_status 프로젝트별 "현재 상태" 지식 페이지(upstream 직결 REST, 기본 trigger, 첫 생성은 full)
              c_recall_status         항목별 "현재 상태" MCP recall(게이트웨이 경유)
  stats     대상 뱅크 stats 요약(문서·관찰 수·pending_consolidation). --wait 이면 pending_consolidation 0 까지 기다린다
  render    evidence / score 요약 JSON 을 리포 산출물 markdown(id·판정만)으로 찍는다
  judge-arms  arm·반복별 score summary 를 manifest 로 받아 스펙 #318 "arm 판정"(합격·개선·효과 없음·판정 불가·미측정)을 낸다(issue #323)
  judge-pairs  판정 v2 짝 비교(issue #338): 기준선 반복 전부 대 처치 반복별 항목 전이·이득(--judge v2, --null)

판정 v2 (issue #338, 런 3): score/rescore --judge v2 면 summary 에 v2 블록(open/done/unknown, absent)을 더한다. v1 은 그대로다.
  규칙·동결 대상·경로 등록표는 bench/README.md "판정 v2 (런 3)".

세트 두 개 (issue #323)
  --answer-key 를 세트마다 한 번씩(세트 이름은 --set-name, 기본 A, B 순서), --evidence 도 세트마다 준다. 세트들은 합친 세트(같은 id 는
  한 번)로 한 번에 채점한다: (a) reflect 1회, (b) 두 세트 project_hint 합집합 페이지 1회씩, (c) 항목마다 1회. 합치려면 정답표의
  query_date·ignore_mentioned_at_from·query_instruction 이 같아야 하고, 끝난 항목 id 는 세트끼리 겹치면 안 되며, 같은 id 의 대조군은
  내용이 같아야 한다(아니면 exit 1). --key-split 또는 세트 둘 이상이면 summary 에 breakdown(세트·키 유무별 집계)과 a_run(이 (a) run 의
  끝난 항목 open, 대조군 open, 판정 가능 여부: 대조군 open 0 = undecidable)이 붙는다. 옵션이 없으면 summary 모양은 그대로다.

대상 선택 (#242 계약, `gh issue view 247`)
  기본 대상은 사본이다: `next_env=$(bash scripts/next-stack.sh env) || exit 1; eval "$next_env"` 뒤 실행한다.
  HC_TARGET=next 이면 게이트웨이 127.0.0.1:18888, 프로젝트 hc-next, 컨테이너 hc-next-* 가 아니면 거부한다.
  운영 대상은 `--allow-prod --read-only` 둘 다 있을 때만 받는다(연막 채점). 이때 (b)는 이미 있는 페이지를 읽기만 하고,
  쓰기 경로(`--create-pages`)를 부르면 어떤 요청도 보내기 전에 exit 1 이다. upstream 클라이언트도 읽기 전용 대상에서는
  GET 이 아닌 요청을 보내지 않는다(이중 가드). 프로세스마다 stderr 에 `target: ...` 한 줄을 첫 요청 전에 낸다.

채점 규칙 (결정적: 같은 입력이면 같은 판정. 경쟁 가설과 기각 근거는 bench/README.md 이 도구 절)
  공통 - 질의마다 정답표 query_instruction 을 넣고, 시각 인자를 받는 도구(recall)에는 QUERY_TIMESTAMP 를 고정으로 보낸다.
         근거(reflect based_on, 페이지 reflect_response.based_on, recall 결과)에 mentioned_at >= ignore_mentioned_at_from 인
         기억이 하나라도 있으면 그 응답으로 채점한 항목은 전부 invalid(post-cutoff-evidence). 근거 목록이 아예 없으면
         invalid(no-provenance) - (a)(b)는 based_on 이 비어 있어도(`{}`, directives 뿐) 같다. 날짜가 없는 근거(based_on 의 멘탈 모델 등)는 운영 대상에서 invalid(undated-evidence),
         사본(D10)에서는 허용한다 - 사본 뱅크에는 기준 시각 이후 기억이 없어 파생물도 그 전 기억에서만 나온다.
  keyword - 대소문자 무시, 가장자리 영숫자 경계(`#2` 는 `#250` 에 안 걸린다). 채점에서는 project_hint 와 같은 keyword 를
         다른 keyword 가 남을 때 뺀다(item_keywords). 맨 번호(`#3`)는 같은 글에 프로젝트 이름이 있어야 센다(item_matches).
  (a)(b) 절 규칙 - 응답에 `## OPEN` 절(제목이 정확히 OPEN(괄호 덧붙임 허용)인 markdown 제목 줄부터 같은 수준 이상 제목 줄 전까지,
         하위 제목은 절 안)이 없으면 전부 invalid(format-no-open-heading). 있으면 OPEN 절 줄의 주어(첫 굵은 글씨 구간 + 설명 구분자 전까지,
         표면 첫 칸, 둘 다 없으면 줄 전체)에 항목 keyword 가 있으면 open, 없으면 not-open. 절 밖과 설명 안 언급은 세지 않는다.
         (b)는 항목의 project_hint 페이지로 채점하고, hint 가 없으면 뱅크 전체 페이지로 채점한다. 페이지가 없으면 invalid(page-missing),
         이름이 같은 페이지가 트리에 둘 이상이면 첫 것을 고르지 않고 invalid(page-ambiguous).
  (c) 최신 상태 규칙 - 결과 중 keyword 가 든 기억의 이 항목 상태(item_state: keyword 가 든 문장의 완료·열림 단서, 마지막
         문장이 이긴다, 단서가 없으면 태그·라벨)를 본다. done 또는 open 인 것 중 mentioned_at 이 가장 늦은 것(같으면 순위가
         앞선 것)이 open 이면 open, done 이면 not-open. 상태를 말하는 기억이 없으면 not-open(no-status).

출력: 기본 `~/.local/share/hindsight-bench/arch-v2/`(0700). 원문(질의·응답·근거 요지)은 여기에만 쓴다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import functools
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from bench.runner import McpClient

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.expanduser("~/.local/share/hindsight-bench/arch-v2")
QUERY_TIMESTAMP = "2026-10-28T14:59:59Z"        # 스펙 #284 결정: 실행 시각이 아니라 고정값
PATHS = ("a_reflect_list", "b_knowledge_page_status", "c_recall_status")
SCORE_PATHS = PATHS + ("d_recall_list",)          # score 가 질의하는 경로. (d)는 v2 전용(v1 판정·집계는 PATHS 그대로, issue #341)
VERDICTS = ("open", "not-open", "invalid")
NEXT_GW = "127.0.0.1:18888"
PROD_GW = "127.0.0.1:8888"
PROD_CONTAINER = "hindsight-crew-hindsight-1"
PROD_PROJECT = "hindsight-crew"
CALL_TIMEOUT = 900.0                             # reflect 는 LLM 루프라 분 단위가 든다
PAGE_PREFIX = "arch-v2-status-"

# 완료를 말하는 kind 태그: 정본 kind:completion 과 게이트웨이 tag_aliases 가 그것으로 바꾸는 옛 이름(운영 정책 사본 기준).
DONE_TAGS = frozenset({"kind:completion", "kind:done", "kind:task-done", "kind:task-completion",
                       "kind:work-log", "kind:work", "kind:cleanup"})
OPEN_TAGS = frozenset({"kind:handoff", "kind:todo"})
# 본문 첫 대괄호 라벨(뱅크 규약 `[작업 종류] ...`)에 이 말이 들어 있으면 상태 진술로 본다. 태그가 우선한다.
DONE_LABEL = re.compile(r"완료|종료|머지|배포|마감|done|merged|closed|completed", re.I)
OPEN_LABEL = re.compile(r"인계|핸드오프|handoff|할\s?일|todo|미완|남은", re.I)
LABEL = re.compile(r"^\s*\[([^\]]{1,12})\]")
HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")


class Refused(Exception):
    """대상 가드·쓰기 가드 거부. main 이 exit 1 로 바꾼다."""


class ToolError(Exception):
    pass


# ---------------------------------------------------------------- 시각

def parse_ts(value: Any) -> Optional[dt.datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- 정답표

def load_answer_key(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    if doc.get("schema_version") != 1:
        raise SystemExit(f"answer key schema_version {doc.get('schema_version')!r} is not 1")
    for k in ("query_date", "ignore_mentioned_at_from", "query_instruction", "closed", "controls"):
        if k not in doc:
            raise SystemExit(f"answer key lacks {k}")
    if parse_ts(doc["ignore_mentioned_at_from"]) is None:
        raise SystemExit("answer key ignore_mentioned_at_from is not an ISO timestamp")
    if not isinstance(doc.get("partial", []), list):       # 선택 필드(issue #338): 끝난 항목과 같은 모양의 목록
        raise SystemExit("answer key partial must be a list of items")
    return doc


SHARED_KEY_FIELDS = ("query_date", "ignore_mentioned_at_from", "query_instruction")


def _canon(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def merge_answer_keys(docs: List[Dict[str, Any]], names: List[str]) -> Tuple[Dict[str, Any], Dict[str, List[str]]]:
    """세트 정답표들을 합친 세트 하나로(issue #323). 돌려주는 것: 합친 정답표, 항목 id -> 속한 세트 이름 목록.
    정답표가 하나면 그대로 돌려준다(옵션 없는 동작 불변). 셋 중 하나라도 어기면 exit 1(SystemExit 메시지):
    세 값(SHARED_KEY_FIELDS)이 다르다(한 reflect·페이지 질의를 공유할 수 없다 - 세트마다 따로 다른 접미사로 채점),
    끝난 항목 id 가 앞 세트와 겹친다, 같은 id 대조군의 내용이 다르다(공유 대조군은 바이트 복사)."""
    if len(names) != len(docs) or len(set(names)) != len(names):
        raise SystemExit(f"answer key set names {names} must be distinct, one per --answer-key")
    sets: Dict[str, List[str]] = {}
    for d, n in zip(docs, names):
        for i in all_items(d):
            if n not in sets.setdefault(i["id"], []):
                sets[i["id"]].append(n)
    if len(docs) == 1:
        return docs[0], sets
    base = docs[0]
    for d, n in zip(docs[1:], names[1:]):
        for f in SHARED_KEY_FIELDS:
            if d[f] != base[f]:
                raise SystemExit(f"answer key {n}: {f} differs from answer key {names[0]} - the sets cannot share one "
                                 "reflect/page query; score each set alone with its own --page-suffix (bench/README.md)")
    seen: Dict[str, Tuple[str, Dict[str, Any], str]] = {}
    out: Dict[str, List[Dict[str, Any]]] = {"closed": [], "controls": [], "partial": []}
    for d, n in zip(docs, names):
        for kind in ("closed", "controls", "partial"):
            for i in d.get(kind) or []:
                prev = seen.get(i["id"])
                if prev is None:
                    seen[i["id"]] = (kind, i, n)
                    out[kind].append(i)
                elif kind != "controls" or prev[0] != "controls":     # partial 도 끝난 항목처럼 겹치면 안 된다
                    raise SystemExit(f"answer key {n}: id {i['id']} is already in answer key {prev[2]} "
                                     "(only shared controls may repeat)")
                elif _canon(i) != _canon(prev[1]):
                    raise SystemExit(f"answer key {n}: control {i['id']} differs from answer key {prev[2]} "
                                     "(a shared control is a byte copy)")
    merged = dict(base, closed=out["closed"], controls=out["controls"])
    lq = {d["list_query"] for d in docs if d.get("list_query")}      # 경로 (d) 질의 문안(issue #341): 세트 C 정답표에만 있어도 된다
    if len(lq) > 1:
        raise SystemExit("answer keys carry different list_query values - path (d) needs one broad query")
    if lq:
        merged["list_query"] = lq.pop()
    if out["partial"]:
        merged["partial"] = out["partial"]
    return merged, sets


def load_answer_keys(paths: List[str], names: Optional[List[str]] = None) -> Tuple[Dict[str, Any], Dict[str, List[str]], List[str]]:
    names = list(names) if names else [chr(ord("A") + k) for k in range(len(paths))]
    doc, sets = merge_answer_keys([load_answer_key(p) for p in paths], names)
    return doc, sets, names


def item_meta(doc: Dict[str, Any], sets: Dict[str, List[str]], split: Optional[Dict[str, Any]] = None,
              ids: Optional[Iterable[str]] = None) -> Dict[str, Dict[str, Any]]:
    """항목 id -> {control, sets, key}. key 는 키 유무 표(`{"<세트>": {"<id>": true|false|null}}`)에서 온다(표가 없으면 넣지 않는다).
    ids 를 주면 그 항목만(세트 밖 항목은 키 표에 없어도 된다). 세트 표에 id 가 없거나, 공유 항목의 값이 세트마다 다르면 exit 1."""
    ctrl = {i["id"] for i in doc["controls"]}
    part = {i["id"] for i in doc.get("partial") or []}
    want = set(ids) if ids is not None else {i["id"] for i in all_items(doc)}
    meta: Dict[str, Dict[str, Any]] = {}
    for i in all_items(doc):
        iid = i["id"]
        if iid not in want:
            continue
        m: Dict[str, Any] = {"control": iid in ctrl, "sets": list(sets.get(iid) or [])}
        if iid in part:
            m["partial"] = True
        if split is not None:
            vals = []
            for sname in m["sets"]:
                tab = split.get(sname)
                if not isinstance(tab, dict) or iid not in tab or tab[iid] not in (True, False, None):
                    raise SystemExit(f"key split: set {sname} has no true/false/null entry for {iid}")
                vals.append(tab[iid])
            if len({_canon(v) for v in vals}) > 1:
                raise SystemExit(f"key split: {iid} differs between sets {m['sets']}")
            m["key"] = vals[0] if vals else None
        meta[iid] = m
    missing = want - set(meta)
    if missing:
        raise SystemExit(f"items {sorted(missing)} are not in the answer key(s)")
    return meta


KEY_NAME = {True: "true", False: "false", None: "null"}


def sample_counts(verdicts: Dict[str, str], meta: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    """한 경로 판정(id -> verdict)의 집계. 범위: all, set:<이름>, (키 표가 있으면) key:<true|false|null>, set:<이름>/key:<..>.
    같은 id 는 all 에서 한 번만 센다(공유 대조군)."""
    out: Dict[str, Dict[str, int]] = {}
    for iid, v in verdicts.items():
        m = meta.get(iid)
        if m is None:
            raise SystemExit(f"verdict for {iid} has no item in the answer key(s)")
        scopes = ["all"] + [f"set:{s}" for s in m["sets"]]
        if "key" in m:
            k = "key:" + KEY_NAME[m["key"]]
            scopes += [k] + [f"set:{s}/{k}" for s in m["sets"]]
        pre = "controls" if m["control"] else "closed"
        for sc in scopes:
            c = out.setdefault(sc, {"closed_total": 0, "closed_open": 0, "controls_total": 0, "controls_open": 0,
                                    "invalid": 0})
            c[pre + "_total"] += 1
            c[pre + "_open"] += v == "open"
            c["invalid"] += v == "invalid"
    return out


def a_run_status(counts_all: Dict[str, int]) -> Dict[str, Any]:
    """(a) 한 run 의 판정 가능 여부: invalid 가 있으면 invalid, 대조군 open 0 이면 undecidable(끝난 항목 집계에서 뺀다)."""
    status = "invalid" if counts_all["invalid"] else ("undecidable" if counts_all["controls_open"] == 0 else "valid")
    return {"closed_open": counts_all["closed_open"], "controls_open": counts_all["controls_open"],
            "controls_total": counts_all["controls_total"], "invalid": counts_all["invalid"],
            "status": status, "decidable": status == "valid"}


def add_breakdown(summary: Dict[str, Any], meta: Dict[str, Dict[str, Any]]) -> None:
    summary["breakdown"] = {p: sample_counts(v, meta) for p, v in summary["paths"].items()}
    if "a_reflect_list" in summary["breakdown"]:
        summary["a_run"] = a_run_status(summary["breakdown"]["a_reflect_list"]["all"])


def load_evidence(paths: List[str]) -> Dict[str, Any]:
    """evidence 요약(세트마다 하나)을 항목 id -> 근거 표 하나로 합친다."""
    merged: Dict[str, Any] = {}
    for p in paths:
        with open(p, encoding="utf-8") as fh:
            ev = json.load(fh)
        for iid, row in ev.get("items", ev).items():
            # 세트에 들어가는지는 status 가 정한다(active_items). 두 파일이 같은 id 를 다르게 말하면 뒤 파일이 조용히 이기지 않게 멈춘다
            if iid in merged and (merged[iid] or {}).get("status") != (row or {}).get("status"):
                raise SystemExit(f"evidence: {iid} has status {(merged[iid] or {}).get('status')!r} and "
                                 f"{(row or {}).get('status')!r} in different --evidence files")
            merged.setdefault(iid, row)
    return merged


def all_items(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(doc["closed"]) + list(doc["controls"]) + list(doc.get("partial") or [])


def active_items(doc: Dict[str, Any], evidence: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """evidence 단계에서 근거가 있는 완료 항목 + 대조군 + partial(issue #338, 근거 확인 대상 아님). 근거 표에 없는 완료 항목은 뺀다."""
    keep = [i for i in doc["closed"] if (evidence.get(i["id"]) or {}).get("status") == "present"]
    return keep + list(doc["controls"]) + list(doc.get("partial") or [])


# ---------------------------------------------------------------- 기억 분류

@functools.lru_cache(maxsize=None)
def _kw_re(keyword: str) -> "re.Pattern[str]":
    """keyword 를 대소문자 무시로 찾되, 가장자리 글자가 영숫자면 그 바깥에 영숫자가 붙지 않아야 한다.
    `#2` 가 `#250` 에, `#4` 가 `#42` 에 걸리지 않게 한다(사본 실측 오탐). 한글 가장자리는 조사가 붙으니 경계를 두지 않는다."""
    k = keyword.casefold()
    left = r"(?<![0-9a-z])" if re.match(r"[0-9a-z]", k) else ""
    right = r"(?![0-9a-z])" if re.search(r"[0-9a-z]$", k) else ""
    return re.compile(left + re.escape(k) + right)


def matches(text: Any, keywords: Iterable[str]) -> bool:
    t = text.casefold() if isinstance(text, str) else ""
    return any(_kw_re(k).search(t) for k in keywords if k)


BARE_NUMBER = re.compile(r"^#\d+$")


def item_matches(text: Any, item: Dict[str, Any], project_scoped: bool = False,
                 kws: Optional[List[str]] = None) -> bool:
    """항목 keyword 매칭. 맨 번호 keyword(`#3`)는 프로젝트마다 다시 쓰이므로, project_hint 가 있으면 같은 글에
    프로젝트 이름도 있어야 센다(project_scoped = 이미 그 프로젝트 범위의 글, (b) 프로젝트 페이지). 사본 실측 오탐:
    다른 영역의 "#2 and #3" 줄이 걸렸다."""
    kws = item_keywords(item) if kws is None else kws
    hint = item.get("project_hint")
    if not hint or project_scoped:
        return matches(text, kws)
    named = [k for k in kws if not BARE_NUMBER.match(k)]
    if matches(text, named):
        return True
    bare = [k for k in kws if BARE_NUMBER.match(k)]
    return bool(bare) and matches(text, bare) and _norm(hint) in _norm(text if isinstance(text, str) else "")


def _norm(s: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", s.casefold())


def item_keywords(item: Dict[str, Any]) -> List[str]:
    """채점에 쓰는 keyword. 프로젝트 이름과 같은 keyword(정규화 후 project_hint 와 같다)는 그 프로젝트의 다른 일
    전부에 걸리므로 다른 keyword 가 남을 때 뺀다(사본 실측: 프로젝트 이름 keyword 가 다른 열린 일에 걸렸다)."""
    kws = list(item["keywords"])
    hint = _norm(item.get("project_hint") or "")
    rest = [k for k in kws if not hint or _norm(k) != hint]
    return rest or kws


# 상태 단서(문장 단위). 부정형("완료되지 않")은 열림이 먼저 이긴다. 한 문장에 완료·열림 단서가 같이 있으면 그 문장은 상태를 말하지 않는다.
NEG_DONE_CUE = re.compile(r"(완료|머지|종결|처리)(되지|하지)\s?(않|못)|not (yet )?(done|finished|closed|merged)|미완")
DONE_CUE = re.compile(r"완료|끝났|끝냈|종결|머지했|머지됨|머지되|merged|\bclosed\b|\bdone\b|completed|해결했|해결됨")
OPEN_CUE = re.compile(r"남은|남음|남아|아직|대기|예정|보류|미착수|착수하지|\bopen\b|pending|\btodo\b")
SENTENCE = re.compile(r"(?<=[.!?。])\s+|\n+|\s-\s")


def text_state(text: str, keywords: Iterable[str]) -> Optional[str]:
    """keyword 가 든 문장의 상태 단서. 여러 문장이 말하면 마지막 문장(서술 순서 = 시간 순서)을 따른다. 없으면 None."""
    state = None
    for sent in SENTENCE.split(text or ""):
        if not matches(sent, keywords):
            continue
        low = sent.casefold()
        if NEG_DONE_CUE.search(low):
            state = "open"
            continue
        d, o = bool(DONE_CUE.search(low)), bool(OPEN_CUE.search(low))
        if d != o:
            state = "done" if d else "open"
    return state


def item_state(m: Dict[str, Any], keywords: Iterable[str]) -> str:
    """기억이 이 항목에 대해 말하는 상태. 본문 단서가 먼저이고, 없으면 태그·라벨(classify_memory).
    관찰은 범위 태그(kind: 포함)를 물려받아 태그가 본문과 어긋난다(사본 실측: kind:completion 관찰이 "남은 작업"을,
    kind:handoff 기억이 "배포 완료"를 말한다)."""
    return text_state(m.get("text") or "", keywords) or classify_memory(m)


keyword_anywhere = matches   # 기각 가설 H1(전체 본문 키워드) - 테스트가 대조용으로 부른다


def classify_memory(m: Dict[str, Any]) -> str:
    tags = set(m.get("tags") or [])
    label = LABEL.match(m.get("text") or "")
    lab = label.group(1) if label else ""
    # "[미완료]"는 '완료'를 품지만 열림이다 - 라벨의 미완을 완료보다 먼저 본다
    if tags & DONE_TAGS or (not tags & OPEN_TAGS and DONE_LABEL.search(lab) and "미완" not in lab):
        return "done"
    if tags & OPEN_TAGS or OPEN_LABEL.search(lab):
        return "open"
    return "neutral"


def _kind_of(m: Dict[str, Any]) -> str:
    done = sorted(set(m.get("tags") or []) & DONE_TAGS)
    return done[0] if done else "text"     # 본문 단서·라벨로 완료


def evidence_for_item(item: Dict[str, Any], results_by_kw: Dict[str, List[Dict[str, Any]]],
                      cutoff: dt.datetime, undated_ok: bool = False) -> Dict[str, Any]:
    """완료 근거 = keyword 가 들어 있고, 이 항목에 대해 완료를 말하고(item_state == done), mentioned_at < cutoff 인 기억.
    mentioned_at 이 없는 기억(D10 recall 행의 일부가 그렇다)은 undated_ok(사본 정책)일 때만 근거로 친다."""
    seen, found = set(), []
    rejected = {"post_cutoff": 0, "undated": 0, "not_done": 0, "no_keyword": 0}
    # 근거 찾기는 정답표 keyword 전부(식별자 recall). 프로젝트 이름 keyword 빼기는 채점(item_keywords)에만 쓴다 -
    # 항목이 프로젝트 자체(철거 등)면 그 이름이 곧 주어라 빼면 참 근거를 잃는다(사본 실측).
    kws = list(item["keywords"])
    for kw in item["keywords"]:
        for m in results_by_kw.get(kw) or []:
            mid = m.get("id")
            if mid in seen:
                continue
            seen.add(mid)
            if not item_matches(m.get("text"), item, kws=kws):
                rejected["no_keyword"] += 1
                continue
            when = parse_ts(m.get("mentioned_at"))
            if when is None and not undated_ok:
                rejected["undated"] += 1
                continue
            if when is not None and when >= cutoff:
                rejected["post_cutoff"] += 1
                continue
            if item_state(m, kws) != "done":
                rejected["not_done"] += 1
                continue
            found.append({"memory_id": mid, "mentioned_at": m.get("mentioned_at"), "kind": _kind_of(m),
                          "gist": (m.get("text") or "").splitlines()[0][:160] if m.get("text") else ""})
    floor = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    found.sort(key=lambda e: parse_ts(e["mentioned_at"]) or floor, reverse=True)
    out = {"id": item["id"], "evidence": found, "rejected": rejected}
    if found:
        out.update(status="present", mentioned_at=found[0]["mentioned_at"], kind=found[0]["kind"])
    else:
        out.update(status="none", reason="no-done-memory-before-cutoff")
    return out


def open_evidence_for_item(item: Dict[str, Any], results_by_kw: Dict[str, List[Dict[str, Any]]],
                           cutoff: dt.datetime, undated_ok: bool = False) -> Dict[str, Any]:
    """대조군 열림 근거(issue #341 세트 C): evidence_for_item 과 같은 규칙에서 상태만 open. keyword 가 있고, 이 항목이 남았다고
    말하고(item_state == open), mentioned_at < cutoff 인 기억."""
    seen, found = set(), []
    rejected = {"post_cutoff": 0, "undated": 0, "not_open": 0, "no_keyword": 0}
    kws = list(item["keywords"])
    for kw in item["keywords"]:
        for m in results_by_kw.get(kw) or []:
            mid = m.get("id")
            if mid in seen:
                continue
            seen.add(mid)
            if not item_matches(m.get("text"), item, kws=kws):
                rejected["no_keyword"] += 1
                continue
            when = parse_ts(m.get("mentioned_at"))
            if when is None and not undated_ok:
                rejected["undated"] += 1
                continue
            if when is not None and when >= cutoff:
                rejected["post_cutoff"] += 1
                continue
            if item_state(m, kws) != "open":
                rejected["not_open"] += 1
                continue
            found.append({"memory_id": mid, "mentioned_at": m.get("mentioned_at"), "kind": _kind_of(m),
                          "gist": (m.get("text") or "").splitlines()[0][:160] if m.get("text") else ""})
    floor = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    found.sort(key=lambda e: parse_ts(e["mentioned_at"]) or floor, reverse=True)
    out = {"id": item["id"], "evidence": found, "rejected": rejected}
    if found:
        out.update(status="present", mentioned_at=found[0]["mentioned_at"], kind=found[0]["kind"])
    else:
        out.update(status="none", reason="no-open-memory-before-cutoff")
    return out


# ---------------------------------------------------------------- 근거(provenance) 검사

def iter_facts(based_on: Any) -> Optional[List[Dict[str, Any]]]:
    """reflect based_on(MCP: fact_type 별 목록) 또는 멘탈 모델 reflect_response.based_on({memories, mental_models,
    directives})을 기억 목록으로 편다. 멘탈 모델 항목은 날짜가 없어 undated 로 표시된다. directives 는 근거가 아니다."""
    if not isinstance(based_on, dict):
        return None
    out: List[Dict[str, Any]] = []
    for key, rows in based_on.items():
        if key == "directives" or not isinstance(rows, list):
            continue
        for r in rows:
            if not isinstance(r, dict):
                continue
            if key in ("mental_models", "mental-models"):     # MCP reflect 는 밑줄, 페이지 reflect_response 는 하이픈(사본 실측)
                out.append({"id": r.get("id"), "mentioned_at": None, "source": "mental_model"})
            else:
                out.append(r)
    return out


def contamination(facts: Optional[List[Dict[str, Any]]], cutoff: dt.datetime, undated_ok: bool) -> Optional[str]:
    if facts is None:
        return "no-provenance"
    undated = False
    if any(f.get("date_lookup") == "failed" for f in facts):
        return "date-lookup-failed"           # 날짜를 알 수 없으면 대상 정책과 무관하게 판정하지 않는다(fail-closed)
    facts = [f for f in facts if f.get("date_lookup") != "deleted"]   # 재통합으로 지워진 근거(404)는 날짜 검사에서 뺀다
    if not facts:
        return "no-provenance"
    for f in facts:
        when = parse_ts(f.get("mentioned_at"))
        if when is None:
            undated = True
        elif when >= cutoff:
            return "post-cutoff-evidence"
    if undated and not undated_ok:
        return "undated-evidence"
    return None


# ---------------------------------------------------------------- (a)(b) 절 규칙

def open_section_lines(text: str) -> Optional[List[str]]:
    """OPEN 제목 줄 아래 본문 줄. 절은 같은 수준 이상(# 개수가 같거나 적은) 제목에서 끝난다 - OPEN 아래
    프로젝트별 하위 제목(### ...)은 절 안이다(사본 실측 응답 모양). 하위 제목 줄 자체는 채점하지 않는다."""
    lines, level, seen = [], 0, False
    for line in (text or "").splitlines():
        h = HEADING.match(line)
        if h:
            depth = len(line.lstrip()) - len(line.lstrip().lstrip("#"))
            title = h.group(1).strip(" *_:").casefold()
            if re.fullmatch(r"open(\s*\(.*\))?", title):    # "## OPEN"만. "Open questions"·"open follow-ups"는 아니다
                level, seen = depth, True
            elif level and depth <= level:
                level = 0
            continue
        if level:
            lines.append(line)
    return lines if seen else None


SUBJECT_STOP = r":|\.\s| - |\u2014|\(|,|\|"     # 줄 주어 뒤 설명이 시작되는 구분자


def line_subject(line: str) -> str:
    """OPEN 줄의 주어(이 줄이 무슨 일인가): 첫 굵은 글씨 구간, 표 줄이면 첫 칸, 둘 다 아니면 줄 전체.
    굵은 구간 뒤에 설명 구분자(SUBJECT_STOP: `:`, `. `, ` - `, em dash, `(`, `,`, 표 칸 `|`)가 바로 오지 않으면
    (`**proj** #12 남음`) 첫 구분자까지 주어로 이어 본다.
    설명 안의 다른 일 언급("#901 관찰 뒤로 미뤘다")을 그 일의 열림으로 세지 않는다(사본 실측 오탐)."""
    s = line.strip()
    m = re.search(r"\*\*(.+?)\*\*", s)
    if m:
        head, rest = m.group(1), s[m.end():]
        if head.rstrip().endswith((":", ".")) or re.match(r"\s*(?:" + SUBJECT_STOP + r"|-\s|\.$|$)", rest):
            return head
        return head + re.split(SUBJECT_STOP, rest, maxsplit=1)[0]
    if s.startswith("|"):
        cells = [c.strip() for c in s.strip("|").split("|")]
        return next((c for c in cells if c), "")
    return s


def judge_sections(text: str, facts: Optional[List[Dict[str, Any]]], items: List[Dict[str, Any]],
                   cutoff: dt.datetime, undated_ok: bool, project_scoped: bool = False) -> Dict[str, Dict[str, str]]:
    # 근거 기억이 하나도 없으면(based_on 이 비었거나 directives 뿐) 날짜를 검사할 수 없으니 판정하지 않는다(fail-closed)
    bad = "no-provenance" if not facts else contamination(facts, cutoff, undated_ok)
    if bad:
        return {i["id"]: {"verdict": "invalid", "reason": bad} for i in items}
    lines = open_section_lines(text)
    if lines is None:
        return {i["id"]: {"verdict": "invalid", "reason": "format-no-open-heading"} for i in items}
    subjects = [line_subject(line) for line in lines]
    out = {}
    for i in items:
        hit = any(item_matches(sub, i, project_scoped) for sub in subjects)
        out[i["id"]] = {"verdict": "open" if hit else "not-open", "reason": "open-section" if hit else "absent"}
    return out


# ---------------------------------------------------------------- (c) 최신 상태 규칙

def judge_recall(results: List[Dict[str, Any]], item: Dict[str, Any], cutoff: dt.datetime,
                 undated_ok: bool) -> Dict[str, str]:
    bad = contamination(results if isinstance(results, list) else None, cutoff, undated_ok)
    if bad:
        return {"verdict": "invalid", "reason": bad}
    best: Optional[Tuple[dt.datetime, int, str]] = None
    floor = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    kws = item_keywords(item)
    for rank, m in enumerate(results):
        if not item_matches(m.get("text"), item):
            continue
        state = item_state(m, kws)
        if state == "neutral":
            continue
        key = (parse_ts(m.get("mentioned_at")) or floor, -rank, state)
        if best is None or key[:2] > best[:2]:
            best = key
    if best is None:
        return {"verdict": "not-open", "reason": "no-status"}
    return {"verdict": "open", "reason": "latest-open"} if best[2] == "open" else \
        {"verdict": "not-open", "reason": "latest-done"}


def summarize(verdicts: Dict[str, Dict[str, Dict[str, str]]]) -> Dict[str, Dict[str, str]]:
    """요약 = 경로 -> 항목 id -> 판정. 사유·원문은 넣지 않는다(리포에 옮겨도 되는 모양)."""
    return {p: {i: v["verdict"] for i, v in sorted(verdicts[p].items())} for p in PATHS if p in verdicts}


# ---------------------------------------------------------------- 판정 v2 (issue #338, 런 3 R3-1)
# 동결 대상(R3-1 머지 뒤 바꾸지 않는다, bench/README.md "판정 v2 (런 3)"): 아래 상수와 v2_* 함수, judge_sections_v2,
# judge_recall_v2, v2_block, pair_*·judge_pairs. 확장 지점: register_v2_path 로 경로 등록표에 새 경로를 더하는 것.
# v1(위 판정)은 그대로 두고 --judge v2 일 때만 summary 에 v2 블록을 더한다.

V2_VERDICTS = ("open", "done", "unknown", "invalid")
# 선후 참조: keyword 왼쪽 "after/before/until/following/since (the)" 또는 "post-/pre-", 오른쪽 "(한 낱말) 전/뒤/후/이후/직후/직전".
# "전환"·"후속"처럼 낱말 일부인 경우는 참조가 아니다(뒤에 공백·구두점·끝이 와야 한다).
V2_REF_LEFT = re.compile(r"(?:\b(?:after|before|until|following|since)\s+(?:the\s+)?|\b(?:post|pre)-)$", re.I)
V2_REF_RIGHT = re.compile(r"^(?:\s+[^\s,.;:()]+)?\s+(?:전|뒤|후|이후|직후|직전)(?:에는|에|부터|으로|로)?(?=$|[\s,.;:)\]])")
V2_QUOTED = re.compile(r"\"[^\"\n]*\"|“[^”\n]*”|「[^」\n]*」")
V2_PAREN = re.compile(r"\([^()]*\)")
V2_ISSUE_NO = re.compile(r"#\d+")
V2_ALIAS_FILLER = re.compile(r"\b(?:pr|issue|issues|and|or)\b|이슈|및|[\s,/&+·]")
V2_QUALIFIER = re.compile(r"([0-9a-z가-힣][0-9a-z가-힣_.-]*)\s?$")
V2_BULLET = re.compile(r"^(\s*)(?:[-*+]|\d+[.)])\s+")
V2_GROUP = re.compile(r"^\s*(?:\*\*[^*]+\*\*\s*:?\s*|[^-*+|#\s].*:\s*)$")      # 굵은 글씨만 있는 줄, 또는 `:`로 끝나는 머리 문단


def v2_known_names(doc: Dict[str, Any]) -> frozenset:
    """정답표가 아는 프로젝트 이름(정규화): project_hint 전부와 `<이름> #N` 꼴 keyword 의 이름. 맨 번호 앞에 다른 이름이
    붙은 언급(`other-repo #4`)을 이 프로젝트의 맨 번호로 세지 않는 데 쓴다. 응답이 아니라 정답표에서만 온다."""
    names = {_norm(i.get("project_hint") or "") for i in all_items(doc)}
    for i in all_items(doc):
        for k in i["keywords"]:
            m = re.fullmatch(r"(\S+)\s*#\d+", k.strip())
            if m:
                names.add(_norm(m.group(1)))
    return frozenset(n for n in names if n)


def v2_live_hits(text: str, item: Dict[str, Any], known: frozenset, bare_context: bool) -> bool:
    """text 안에 이 항목 keyword 의 "살아 있는" 언급이 있나. 빼는 것: 따옴표 안, 선후 참조 자리(V2_REF_*),
    맨 번호(`#3`) 중 정답표가 아는 다른 프로젝트 이름이 바로 앞에 붙은 것(`other-repo #3`)과 프로젝트 문맥이 없는 것(bare_context False).
    이름 붙은 keyword 는 문맥 없이 센다(v1 item_matches 와 같다)."""
    t = V2_QUOTED.sub(lambda m: " " * len(m.group(0)), (text or "").casefold())
    hint = _norm(item.get("project_hint") or "")
    for kw in item_keywords(item):
        bare = bool(BARE_NUMBER.match(kw))
        if bare and hint and not bare_context:
            continue
        for m in _kw_re(kw).finditer(t):
            left, right = t[:m.start()], t[m.end():]
            if V2_REF_LEFT.search(left) or V2_REF_RIGHT.match(right):
                continue
            if bare:
                q = V2_QUALIFIER.search(left)
                if q and _norm(q.group(1)) != hint and _norm(q.group(1)) in known:
                    continue
            return True
    return False


def v2_strip_parens(subject: str, item: Dict[str, Any]) -> str:
    """주어에서 괄호 설명을 뺀다. 괄호 안이 이 항목의 별칭뿐이면(`posts (other-repo #68)`, `(PR #280)`) 남긴다."""
    kws = item_keywords(item)

    def repl(m: "re.Match[str]") -> str:
        inner = m.group(0)[1:-1].casefold()
        for k in kws:
            inner = _kw_re(k).sub(" ", inner)
        inner = V2_ALIAS_FILLER.sub("", inner)
        return m.group(0) if not inner and matches(m.group(0), kws) else " "
    prev = None
    while prev != subject:
        prev, subject = subject, V2_PAREN.sub(repl, subject)
    return subject


def v2_section_lines(text: str) -> Dict[str, Optional[List[Dict[str, Any]]]]:
    """`## OPEN`·`## DONE` 절의 본문 줄과 줄마다의 문맥(하위 제목, 굵은 묶음 줄·조상 줄 목록). 절 규칙은 open_section_lines 와 같다
    (같은 수준 이상 제목에서 끝, 하위 제목은 절 안). 조상 = 들여쓰기가 더 얕은 앞 줄들, 묶음 = 하위 제목 아래 가장 가까운
    굵은 글씨만 있는 줄 또는 `:`로 끝나는 머리 문단(같은 들여쓰기 목록 줄의 바로 위 묶음)."""
    out: Dict[str, Optional[List[Dict[str, Any]]]] = {"open": None, "done": None}
    cur, level, sub, group, stack = None, 0, "", None, []
    for line in (text or "").splitlines():
        h = HEADING.match(line)
        if h:
            depth = len(line.lstrip()) - len(line.lstrip().lstrip("#"))
            title = h.group(1).strip(" *_:").casefold()
            kind = "open" if re.fullmatch(r"open(\s*\(.*\))?", title) else \
                ("done" if re.fullmatch(r"done(\s*\(.*\))?", title) else None)
            if kind:
                cur, level, sub, group, stack = kind, depth, "", None, []
                out[kind] = out[kind] or []
            elif cur and depth <= level:
                cur = None
            elif cur:
                sub, group, stack = h.group(1), None, []
            continue
        if not cur or not line.strip():
            continue
        b = V2_BULLET.match(line)
        indent = len(b.group(1)) if b else len(line) - len(line.lstrip())
        if not b and V2_GROUP.match(line):
            group, stack = line, []
            out[cur].append({"text": line, "sub": sub, "ancestors": [], "group_line": True})
            continue
        if not b and not indent and not line.lstrip().startswith("|"):
            group = None            # 들여쓰기 없는 일반 문단이 오면 앞 묶음 줄의 범위가 끝난다(표 줄은 묶음 안이다)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        anc = [s[1] for s in reversed(stack)] + ([group] if group else [])
        out[cur].append({"text": line, "sub": sub, "ancestors": anc, "group_line": False})
        stack.append((indent, line))
    return out


def v2_line_hit(row: Dict[str, Any], item: Dict[str, Any], known: frozenset, project_scoped: bool) -> bool:
    """절 줄 하나가 이 항목을 주어로 말하나((a)(b) 절 규칙 v2).
    - 주어 = v1 line_subject 에서 괄호 설명(별칭 괄호는 남김)을 뺀 것. 그 안의 살아 있는 keyword 언급(v2_live_hits)만 센다.
    - 맨 번호는 같은 줄, 조상 줄·굵은 묶음 줄, OPEN 하위 제목 중 하나에 project_hint 가 있으면 문맥이 있다((b) 프로젝트 페이지는 늘 있다).
    - 조상 줄(묶음 줄 포함) 중 주어에 이슈 번호(#N)가 있는 줄이 있으면 그 하위 줄은 이 항목의 언급이 아니다(다른 이슈가 주어)."""
    hint = _norm(item.get("project_hint") or "")
    own = {k.casefold() for k in item_keywords(item) if BARE_NUMBER.match(k)}
    for a in row["ancestors"]:
        nums = {n.casefold() for n in V2_ISSUE_NO.findall(v2_strip_parens(line_subject(V2_BULLET.sub("", a)), item))}
        if nums and not nums & own:     # 다른 이슈가 주어인 줄의 하위 줄만 뺀다(조상 번호가 이 항목 자신이면 뺄 이유가 없다)
            return False
    ctx = project_scoped or not hint or any(hint in _norm(x or "") for x in [row["text"], row["sub"]] + row["ancestors"])
    subject = v2_strip_parens(line_subject(row["text"]), item)
    return v2_live_hits(subject, item, known, ctx)


def judge_sections_v2(text: str, facts: Optional[List[Dict[str, Any]]], items: List[Dict[str, Any]],
                      cutoff: dt.datetime, undated_ok: bool, known: frozenset,
                      project_scoped: bool = False) -> Dict[str, Dict[str, Any]]:
    """(a)(b) 항목 판정 v2: OPEN 절 주어에 있으면 open, 아니고 DONE 절 주어에 있으면 done, 둘 다 아니면 unknown.
    응답 어디에도 keyword 가 없으면 unknown + absent. invalid 사유는 v1 judge_sections 와 같다."""
    bad = "no-provenance" if not facts else contamination(facts, cutoff, undated_ok)
    if bad:
        return {i["id"]: {"verdict": "invalid", "reason": bad} for i in items}
    sec = v2_section_lines(text)
    if sec["open"] is None:
        return {i["id"]: {"verdict": "invalid", "reason": "format-no-open-heading"} for i in items}
    out: Dict[str, Dict[str, Any]] = {}
    for i in items:
        if any(v2_line_hit(r, i, known, project_scoped) for r in sec["open"]):
            out[i["id"]] = {"verdict": "open", "reason": "open-section"}
        elif any(v2_line_hit(r, i, known, project_scoped) for r in sec["done"] or []):
            out[i["id"]] = {"verdict": "done", "reason": "done-section"}
        elif not item_matches(text, i, project_scoped):
            out[i["id"]] = {"verdict": "unknown", "reason": "absent", "absent": True}
        else:
            out[i["id"]] = {"verdict": "unknown", "reason": "no-subject"}
    return out


def v2_item_state(m: Dict[str, Any], item: Dict[str, Any], known: frozenset) -> str:
    """기억이 이 항목을 주어로 말하는 상태((c) 최신 상태 규칙 v2). 문장 단위로 살아 있는 keyword 언급(v2_live_hits)이 있는
    문장만 본다. 완료 기록(태그·라벨이 done)의 열림 단서 문장은 완료 뒤 후속 문장이라 상태로 세지 않는다. 단서 문장이 없으면
    keyword 가 첫 문장(기억의 제목 줄)에 있을 때만 태그·라벨을 쓴다. 그 밖은 neutral."""
    rec = classify_memory(m)
    sents = SENTENCE.split(m.get("text") or "")
    hint = _norm(item.get("project_hint") or "")
    ctx = not hint or hint in _norm(m.get("text") or "")
    state, first_hit = None, False
    for k, sent in enumerate(sents):
        if not v2_live_hits(sent, item, known, ctx):
            continue
        first_hit = first_hit or k == 0
        low = sent.casefold()
        if NEG_DONE_CUE.search(low):
            cue: Optional[str] = "open"
        else:
            d, o = bool(DONE_CUE.search(low)), bool(OPEN_CUE.search(low))
            cue = ("done" if d else "open") if d != o else None
        if cue == "open" and rec == "done":
            continue
        state = cue or state
    if state:
        return state
    return rec if first_hit else "neutral"


def judge_recall_v2(results: Any, item: Dict[str, Any], cutoff: dt.datetime, undated_ok: bool,
                    known: frozenset) -> Dict[str, Any]:
    """(c) 항목 판정 v2. 상태 진술(v2_item_state 가 open·done) 중 mentioned_at 이 가장 늦은 것(같으면 순위가 앞선 것).
    날짜 있는 상태 진술이 하나라도 있으면 날짜 없는 것은 고르지 않는다. 모두 날짜가 없으면 상태가 하나로 같을 때만 그 상태,
    엇갈리면 unknown(undated-conflict, 순서를 알 수 없다). 상태 진술이 없으면 unknown(no-status), keyword 가 든 결과가 아예
    없으면 unknown + absent. invalid 는 v1 judge_recall 과 같다."""
    bad = contamination(results if isinstance(results, list) else None, cutoff, undated_ok)
    if bad:
        return {"verdict": "invalid", "reason": bad}
    dated: List[Tuple[dt.datetime, int, str]] = []
    undated: List[str] = []
    seen = False
    for rank, m in enumerate(results):
        if not item_matches(m.get("text"), item):
            continue
        seen = True
        st = v2_item_state(m, item, known)
        if st == "neutral":
            continue
        when = parse_ts(m.get("mentioned_at"))
        if when is None:
            undated.append(st)
        else:
            dated.append((when, -rank, st))
    if not seen:
        return {"verdict": "unknown", "reason": "absent", "absent": True}
    if dated:
        st = max(dated, key=lambda x: x[:2])[2]
        return {"verdict": st, "reason": "latest-" + st}
    if not undated:
        return {"verdict": "unknown", "reason": "no-status"}
    if len(set(undated)) > 1:
        return {"verdict": "unknown", "reason": "undated-conflict"}
    return {"verdict": undated[0], "reason": "undated-" + undated[0]}


def _v2_judge_a(doc, items, section, cutoff, undated_ok):
    res = section["response"]
    return judge_sections_v2(res.get("text") or "", iter_facts(res.get("based_on")), items, cutoff, undated_ok,
                             v2_known_names(doc))


def _v2_judge_b(doc, items, section, cutoff, undated_ok):
    known, v = v2_known_names(doc), {}
    for i in items:
        pg = section.get(i.get("project_hint") or "-")
        if not pg:
            v[i["id"]] = {"verdict": "invalid", "reason": "page-missing"}
        elif pg.get("ambiguous"):
            v[i["id"]] = {"verdict": "invalid", "reason": "page-ambiguous"}
        else:
            v.update(judge_sections_v2(pg.get("content") or "", iter_facts(pg.get("based_on")), [i], cutoff, undated_ok,
                                       known, project_scoped=bool(i.get("project_hint"))))
    return v


def _v2_judge_c(doc, items, section, cutoff, undated_ok):
    known = v2_known_names(doc)
    return {i["id"]: judge_recall_v2((section.get(i["id"]) or {}).get("results") or [], i, cutoff, undated_ok, known)
            if i["id"] in section else {"verdict": "invalid", "reason": "no-response"} for i in items}


@dataclass(frozen=True)
class V2Path:
    """경로 등록표 한 줄. name = detail·summary 경로 키, short = manifest 경로 키(그 자체 또는 `<short>-<변형>`, 예: b-full),
    kind = list(목록 경로: open -> unknown 도 이득) | item(항목 경로: open -> done 만 이득),
    runs = valid3((a): 반복마다 앞 6시도 중 유효 run 3개 다수결) | all(모든 생성 다수결), judge = (doc, items, detail[name],
    cutoff, undated_ok) -> 항목 id -> {verdict, reason[, absent]}."""
    name: str
    short: str
    kind: str
    runs: str
    judge: Callable[..., Dict[str, Dict[str, Any]]]


V2_REGISTRY: Dict[str, V2Path] = {}


def register_v2_path(p: V2Path) -> None:
    """확장 지점: 새 경로(R3-2 의 (d) 등)는 판정 함수를 등록만 한다. 집계·judge-pairs·표 출력은 등록표를 돈다."""
    if p.kind not in ("list", "item") or p.runs not in ("valid3", "all"):
        raise ValueError(f"v2 path {p.name}: kind {p.kind!r} / runs {p.runs!r}")
    V2_REGISTRY[p.name] = p


register_v2_path(V2Path("a_reflect_list", "a", "list", "valid3", _v2_judge_a))
register_v2_path(V2Path("b_knowledge_page_status", "b", "list", "all", _v2_judge_b))
register_v2_path(V2Path("c_recall_status", "c", "item", "all", _v2_judge_c))


# ---- 경로 (d) (issue #341, 런 3 R3-2): 동결 함수를 고치지 않고 등록표에 더한다(확장 지점)

def _v2_judge_d(doc, items, section, cutoff, undated_ok):
    """(d) 넓은 목록 질의 recall 1회의 결과 집합 하나로 항목마다 (c) 최신 상태 규칙 v2(judge_recall_v2)를 그대로 쓴다.
    결과에 keyword 가 없으면 unknown + absent(목록 경로라 끝난 일에는 맞는 답), 결과가 비었으면 invalid(no-provenance)."""
    known, results = v2_known_names(doc), (section or {}).get("results") or []
    return {i["id"]: judge_recall_v2(results, i, cutoff, undated_ok, known) for i in items}


register_v2_path(V2Path("d_recall_list", "d", "list", "all", _v2_judge_d))


def partial_ids(doc: Dict[str, Any]) -> set:
    return {i["id"] for i in doc.get("partial") or []}


def judge_detail_v2(doc: Dict[str, Any], items: List[Dict[str, Any]], detail: Dict[str, Any],
                    undated_ok: bool) -> Dict[str, Dict[str, Dict[str, Any]]]:
    cutoff = parse_ts(doc["ignore_mentioned_at_from"])
    return {name: p.judge(doc, items, detail[name], cutoff, undated_ok) for name, p in V2_REGISTRY.items() if name in detail}


def v2_block(doc: Dict[str, Any], verdicts: Dict[str, Dict[str, Dict[str, Any]]]) -> Dict[str, Any]:
    """summary 의 v2 블록: 경로 -> 항목 id -> open/done/unknown/invalid, absent(unknown 의 하위 표시) 목록, 범주(closed·controls·
    partial)별 수. partial 은 끝난 항목 집계에 섞지 않는다."""
    ctrl, part = {i["id"] for i in doc["controls"]}, partial_ids(doc)
    paths: Dict[str, Dict[str, str]] = {}
    absent: Dict[str, List[str]] = {}
    counts: Dict[str, Dict[str, Dict[str, int]]] = {}
    for name, vs in verdicts.items():
        paths[name] = {i: v["verdict"] for i, v in sorted(vs.items())}
        absent[name] = sorted(i for i, v in vs.items() if v.get("absent"))
        c = {cat: {"total": 0, "open": 0, "done": 0, "unknown": 0, "absent": 0, "invalid": 0}
             for cat in ("closed", "controls", "partial")}
        for i, v in vs.items():
            row = c["controls" if i in ctrl else ("partial" if i in part else "closed")]
            row["total"] += 1
            row[v["verdict"]] += 1
            row["absent"] += bool(v.get("absent"))
        counts[name] = c
    return {"judge": "v2", "paths": paths, "absent": absent, "counts": counts}


# ---------------------------------------------------------------- 질의

FORMAT_RULE = ("Format: use exactly the markdown headings '## OPEN' and '## DONE'. Under '## OPEN' put one bullet per "
               "item that is still not finished as of that date; under '## DONE' put items that are finished. "
               "Name each item with its project and issue/PR numbers.")


def reflect_query(doc: Dict[str, Any]) -> str:
    return (f"{doc['query_instruction']}\nAs of {doc['query_date']}, list every task, ticket or work item across all "
            f"projects that is still not finished.\n{FORMAT_RULE}")


def reflect_args(doc: Dict[str, Any], budget: str = "mid") -> Dict[str, Any]:
    return {"query": reflect_query(doc), "include_based_on": True, "budget": budget}


def recall_query(doc: Dict[str, Any], item: Dict[str, Any]) -> str:
    return f"{doc['query_instruction']}\nWhat is the current status of: {' / '.join(item['keywords'])}?"


def recall_args(doc: Dict[str, Any], item: Dict[str, Any]) -> Dict[str, Any]:
    return {"query": recall_query(doc, item), "query_timestamp": QUERY_TIMESTAMP, "max_tokens": 4096}


D_MAX_TOKENS = 4096     # 경로 (d): MCP recall 기본 크기(원래 증상 세션의 넓은 목록 recall 과 같은 조건)


def list_recall_query(doc: Dict[str, Any]) -> str:
    """경로 (d) 질의: 정답표 query_instruction + 정답표 `list_query`(항목 이름·이슈 번호·프로젝트 이름 없는 넓은 목록 요청,
    R3-0 질의 후보 문안에서 고른 것. 문안은 리포 밖 정답표에만 둔다)."""
    return f"{doc['query_instruction']}\n{doc['list_query']}"


def list_recall_args(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {"query": list_recall_query(doc), "query_timestamp": QUERY_TIMESTAMP, "max_tokens": D_MAX_TOKENS}


def evidence_args(keyword: str) -> Dict[str, Any]:
    # 식별자 recall(정답표 bank_evidence_rule). 판단 질의가 아니라서 지시문은 넣지 않고 시각 인자만 고정한다.
    return {"query": keyword, "query_timestamp": QUERY_TIMESTAMP, "max_tokens": 4096}


def page_query(doc: Dict[str, Any], hint: Optional[str]) -> str:
    scope = f"the project {hint}" if hint else "all projects"
    return (f"{doc['query_instruction']}\nWhat is the current status of work in {scope} as of {doc['query_date']}? "
            f"{FORMAT_RULE}")


def page_name(hint: Optional[str], suffix: str = "") -> str:
    """(b) 페이지 이름. suffix 는 생성과 읽기 전용 채점에 함께 쓴다(issue #297: 재통합 전에 만든 `-pre` 페이지와
    재통합 뒤 새로 만든 접미사 없는 페이지가 같은 사본에 공존한다)."""
    return PAGE_PREFIX + (hint or "all") + suffix


def page_body(doc: Dict[str, Any], hint: Optional[str], suffix: str = "") -> Dict[str, Any]:
    body: Dict[str, Any] = {"name": page_name(hint, suffix), "source_query": page_query(doc, hint)}
    if hint:
        body["tags"] = [f"project:{hint}"]
    return body


# ---------------------------------------------------------------- 대상 선택

@dataclass
class Target:
    kind: str                 # next | prod
    gw_bind: str
    container: str
    project: str
    writable: bool
    undated_ok: bool

    def line(self) -> str:
        return f"target: gw={self.gw_bind} project={self.project} container={self.container} pg0=-"


def _copy_shaped(gw: str, container: str, project: str) -> bool:
    return gw.endswith(":18888") or container.startswith("hc-next") or project.startswith("hc-next")


def resolve_target(env: Dict[str, str], allow_prod: bool, read_only: bool) -> Target:
    if env.get("HC_TARGET") == "next":
        if allow_prod:
            raise Refused("HC_TARGET=next together with --allow-prod: pick one target")
        gw, c, p = env.get("HC_GATEWAY_BIND", ""), env.get("HC_HINDSIGHT_CONTAINER", ""), env.get("HC_COMPOSE_PROJECT", "")
        if gw != NEXT_GW or p != "hc-next" or not c.startswith("hc-next-"):
            raise Refused(f"HC_TARGET=next but gw={gw or '-'} project={p or '-'} container={c or '-'}")
        return Target("next", gw, c, p, writable=True, undated_ok=True)
    if env.get("HC_TARGET"):
        raise Refused(f"unknown HC_TARGET={env['HC_TARGET']}")
    if not (allow_prod and read_only):
        raise Refused("default target is the copy: run after `eval \"$(bash scripts/next-stack.sh env)\"`, "
                      "or pass both --allow-prod --read-only for a production smoke score")
    gw = env.get("HC_GATEWAY_BIND") or PROD_GW
    c = env.get("HC_HINDSIGHT_CONTAINER") or PROD_CONTAINER
    p = env.get("HC_COMPOSE_PROJECT") or PROD_PROJECT
    if _copy_shaped(gw, c, p):
        raise Refused(f"HC_TARGET is empty but the target looks like the copy (gw={gw} container={c})")
    return Target("prod", gw, c, p, writable=False, undated_ok=False)


# ---------------------------------------------------------------- 클라이언트

class Gateway:
    """게이트웨이 /mcp/<bank> 의 tools/call. 응답 본문(content[0].text)의 JSON 을 돌려준다."""

    def __init__(self, gw_bind: str, bank: str, token: str, timeout: float = CALL_TIMEOUT) -> None:
        self.url = f"http://{gw_bind}/mcp/{urllib.parse.quote(bank, safe='')}"
        self.token = token
        self.timeout = timeout
        self.session: Optional[str] = None
        self.next_id = 1
        self.calls: List[str] = []

    def _post(self, payload: Dict[str, Any]) -> Tuple[int, Dict[str, str], bytes]:
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
        except urllib.error.HTTPError as e:
            return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()

    def _open(self) -> None:
        code, hdr, _ = self._post({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                                   "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                              "clientInfo": {"name": "hc-arch-v2-regression", "version": "1"}}})
        if code != 200:
            raise ToolError(f"initialize HTTP {code}")
        self.session = hdr.get("mcp-session-id")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if self.session is None:
            self._open()
        rid, self.next_id = self.next_id, self.next_id + 1
        code, hdr, body = self._post({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                                      "params": {"name": tool, "arguments": args}})
        self.calls.append(tool)
        msg = McpClient.parse(hdr.get("content-type", ""), body)
        if code != 200 or not msg or "result" not in msg:
            raise ToolError(f"{tool}: HTTP {code} {((msg or {}).get('error') or {}).get('message', '')}")
        res = msg["result"]
        text = "".join(c.get("text", "") for c in res.get("content") or [] if isinstance(c, dict))
        if res.get("isError"):
            raise ToolError(f"{tool}: isError {text[:200]}")
        try:
            out = json.loads(text)
        except ValueError:
            raise ToolError(f"{tool}: non-JSON result")
        if isinstance(out, dict) and out.get("status") == "error":    # 성공형 실패 프레임(#247)
            raise ToolError(f"{tool}: status=error")
        if isinstance(out, dict) and out.get("error") and not out.get("results"):
            # upstream recall 예외는 isError 가 아니라 {"error": .., "results": []} 다(bench/runner.py classify 와 같은 판정)
            raise ToolError(f"{tool}: error body")
        return out


# 컨테이너 안 upstream 직결. API 키는 stdin JSON 으로만 넘긴다(argv·env 에 닿지 않는다, scripts/bank_stats.py 방식).
UPSTREAM_SRC = r'''
import json, sys, urllib.request, urllib.error
a = json.loads(sys.stdin.read())
data = None if a.get("body") is None else json.dumps(a["body"]).encode()
req = urllib.request.Request("http://localhost:8888" + a["path"], data=data, method=a["method"],
                             headers={"Authorization": "Bearer " + a["key"], "Content-Type": "application/json"})
try:
    r = urllib.request.urlopen(req, timeout=a.get("timeout", 60)); code, body = r.status, r.read()
except urllib.error.HTTPError as e:
    code, body = e.code, e.read()
sys.stdout.write(json.dumps({"status": code, "body": body.decode("utf-8", "replace")}))
'''


class UpstreamError(Exception):
    def __init__(self, status: int, path: str) -> None:
        super().__init__(f"upstream HTTP {status} {path}")
        self.status = status


class Upstream:
    def __init__(self, container: str, api_key: str, writable: bool,
                 runner: Callable[..., Any] = subprocess.run) -> None:
        self.container = container
        self.api_key = api_key
        self.writable = writable
        self.runner = runner
        self.calls: List[Tuple[str, str]] = []

    def request(self, method: str, path: str, body: Any = None, timeout: float = 60) -> Any:
        if method != "GET" and not self.writable:
            raise Refused(f"upstream {method} refused on a read-only target")
        payload = json.dumps({"key": self.api_key, "method": method, "path": path, "body": body, "timeout": timeout})
        cp = self.runner(["docker", "exec", "-i", self.container, "python3", "-c", UPSTREAM_SRC],
                         input=payload, capture_output=True, text=True, timeout=timeout + 30)
        self.calls.append((method, path.split("?")[0]))
        if cp.returncode != 0:
            raise ToolError(f"docker exec exit {cp.returncode}")
        out = json.loads(cp.stdout)
        if not 200 <= out["status"] < 300:
            raise UpstreamError(out["status"], path.split("?")[0])
        try:
            return json.loads(out["body"])
        except ValueError:
            return out["body"]


def _env_file_value(path: str, key: str) -> str:
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if line.startswith(key + "="):
                v = line[len(key) + 1:].strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                return v
    return ""


def _host_setting(key: str, env: Dict[str, str]) -> str:
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    import hc_host_env   # noqa: E402 - scripts/ 는 패키지가 아니다
    root = Path(env.get("HC_PROD_ROOT") or REPO)
    return hc_host_env.require(key, root=root)[key]


def build_clients(target: Target, args: argparse.Namespace, env: Optional[Dict[str, str]] = None) -> Tuple[Gateway, Upstream]:
    env = dict(os.environ if env is None else env)
    env_file = env.get("HC_ENV_FILE") or os.path.join(REPO, ".env.local")
    if not os.path.isfile(env_file):
        raise Refused(f"HC_ENV_FILE {env_file} does not exist (production smoke: HC_ENV_FILE=<prod checkout>/.env.local)")
    api_key = env.get("HC_HINDSIGHT_API_KEY") or _env_file_value(env_file, "HC_HINDSIGHT_API_KEY")
    if not api_key:
        raise Refused("HC_HINDSIGHT_API_KEY not found in HC_ENV_FILE")
    owner = args.owner or _host_setting("HC_OWNER", env)
    secrets = env.get("HC_SECRETS_DIR") or os.path.join(env.get("HC_PROD_ROOT") or REPO, "secrets")
    with open(os.path.join(secrets, "acl.json"), encoding="utf-8") as fh:
        acl = json.load(fh)
    token = next((t for t, p in acl.get("tokens", {}).items() if p.get("identity") == owner), None)
    if not token:
        raise Refused(f"no token for identity {owner} in {secrets}/acl.json")
    return Gateway(target.gw_bind, args.bank, token), Upstream(target.container, api_key, target.writable)


def resolve_bank(args: argparse.Namespace, env: Dict[str, str]) -> str:
    return args.bank or env.get("HC_NEXT_REQUIRED_BANK") or _host_setting("HC_PERSONAL_BANK", env)


# ---------------------------------------------------------------- 출력

def ensure_out_dir(path: str) -> Path:
    p = Path(path).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    os.chmod(p, 0o700)
    return p


def _write(path: Path, obj: Any) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")


def _date(ts: Optional[str]) -> str:
    t = parse_ts(ts)
    return t.strftime("%Y-%m-%d") if t else "-"


def render_evidence_md(ev_items: Dict[str, Dict[str, Any]], doc: Dict[str, Any]) -> str:
    """리포 산출물 표: 항목 id, 근거 유무(mentioned_at 날짜, kind), 남은 세트 크기. 이름·keyword·질의는 쓰지 않는다."""
    rows = ["| id | 뱅크 안 완료 근거 | mentioned_at (UTC 날짜) | 근거 종류 |", "|---|---|---|---|"]
    kept = 0
    for it in doc["closed"]:
        e = ev_items.get(it["id"]) or {"status": "none"}
        if e.get("status") == "present":
            kept += 1
            rows.append(f"| {it['id']} | 근거 있음 | {_date(e.get('mentioned_at'))} | {e.get('kind', '-')} |")
        else:
            rows.append(f"| {it['id']} | 없음 -> 세트에서 뺌 | - | - |")
    for it in doc["controls"]:
        rows.append(f"| {it['id']} | 대조군(expect open, 근거 확인 대상 아님) | - | - |")
    rows.append("")
    rows.append(f"남은 세트: 완료 {kept}건 + 대조군 {len(doc['controls'])}건 = {kept + len(doc['controls'])}건 "
                f"(정답표 완료 {len(doc['closed'])}건 중 {len(doc['closed']) - kept}건 뺌)")
    return "\n".join(rows) + "\n"


def render_dryrun_md(paths: Dict[str, Dict[str, str]], metrics: Dict[str, Dict[str, Any]]) -> str:
    present = [p for p in PATHS if p in paths]
    ids = sorted({i for p in present for i in paths[p]}, key=lambda s: (s[0] != "K", s))
    rows = ["| id | " + " | ".join(present) + " |", "|---|" + "---|" * len(present)]
    for i in ids:
        rows.append(f"| {i} | " + " | ".join(paths[p].get(i, "-") for p in present) + " |")
    rows += ["", "| 경로 | open | not-open | invalid | 질의 수 | 소요(초) | LLM 호출 |", "|---|---|---|---|---|---|---|"]
    for p in present:
        vs = list(paths[p].values())
        m = metrics.get(p) or {}
        rows.append(f"| {p} | {vs.count('open')} | {vs.count('not-open')} | {vs.count('invalid')} | "
                    f"{m.get('queries', '-')} | {m.get('seconds', '-')} | {m.get('llm_calls', '-')} |")
    return "\n".join(rows) + "\n"


# ---------------------------------------------------------------- 명령

def cmd_evidence(args, doc, target, gw, up, out: Path) -> int:
    cutoff = parse_ts(doc["ignore_mentioned_at_from"])
    run = ensure_out_dir(str(out / ("evidence-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S"))))
    summary: Dict[str, Any] = {}
    todo = [(it, evidence_for_item) for it in doc["closed"]]
    if args.controls:       # 세트 C 선정(issue #341): 대조군 열림 근거, partial 완료 근거도 같은 식별자 recall 로 본다
        todo += [(it, open_evidence_for_item) for it in doc["controls"]]
        todo += [(it, evidence_for_item) for it in doc.get("partial") or []]
    for it, rule in todo:
        results = {kw: (gw.call("recall", evidence_args(kw)).get("results") or []) for kw in it["keywords"]}
        ev = rule(it, results, cutoff, target.undated_ok)
        _write(run / f"{it['id']}.json", ev)
        summary[it["id"]] = {k: ev[k] for k in ("status", "mentioned_at", "kind", "reason") if k in ev}
    closed = {i["id"] for i in doc["closed"]}
    kept = sum(1 for k, v in summary.items() if k in closed and v["status"] == "present")
    ctrl = len(doc["controls"]) if not args.controls else \
        sum(1 for i in doc["controls"] if summary[i["id"]]["status"] == "present")   # --controls 면 열림 근거 있는 대조군만
    _write(run / "summary.json", {"target": target.kind, "queries": len(gw.calls), "items": summary,
                                  "remaining": kept + ctrl, "created": now_iso()})
    print(run / "summary.json")
    return 0


def _llm_total(up: Upstream, bank: str, start: str, end: str) -> Optional[int]:
    q = urllib.parse.urlencode({"start_date": start, "end_date": end, "limit": 1})
    try:
        return int(up.request("GET", f"/v1/default/banks/{urllib.parse.quote(bank, safe='')}/llm-requests?{q}")["total"])
    except Exception:
        return None


class AmbiguousPage(Exception):
    """이름이 같은 페이지가 트리에 둘 이상이다(rework #304-1). 첫 것을 고르지 않는다(fail-closed)."""

    def __init__(self, name: str, parents: List[str]) -> None:
        super().__init__(f"page name {name!r} matches {len(parents)} pages under: {', '.join(parents)}")
        self.parents = parents


def _find_pages(nodes: List[Dict[str, Any]], name: str, parent: str = "") -> List[Tuple[Dict[str, Any], str]]:
    """이름이 같은 페이지 전부와 각 부모 경로(폴더 이름을 `/`로 이음, 루트는 빈 문자열)."""
    hits: List[Tuple[Dict[str, Any], str]] = []
    for n in nodes or []:
        if n.get("kind") == "page" and n.get("name") == name:
            hits.append((n, parent))
        sub = f"{parent}/{n.get('name')}" if parent else str(n.get("name"))
        hits += _find_pages(n.get("children") or [], name, sub if n.get("kind") != "page" else parent)
    return hits


def _find_page(nodes: List[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    """0개면 None, 1개면 그 페이지, 2개 이상이면 AmbiguousPage(폴더가 다른 같은 이름 페이지를 엉뚱하게 채점하지 않는다)."""
    hits = _find_pages(nodes, name)
    if len(hits) > 1:
        raise AmbiguousPage(name, [p or "(root)" for _, p in hits])
    return hits[0][0] if hits else None


def _wait_op(up: Upstream, bank: str, op_id: str, timeout: float, poll: float = 10.0) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        op = up.request("GET", f"/v1/default/banks/{bank}/operations/{op_id}")
        if op.get("status") in ("completed", "failed", "cancelled", "not_found"):
            return op
        if time.monotonic() > deadline:
            return {"status": "timeout"}
        time.sleep(poll)


def existing_pages(up: Upstream, bank: str, items: List[Dict[str, Any]], suffix: str = "") -> List[str]:
    """이 run 이 만들 페이지 이름 중 대상 트리에 이미 있는 것."""
    tree = up.request("GET", f"/v1/default/banks/{urllib.parse.quote(bank, safe='')}/knowledge-base/tree")
    names = sorted({page_name(i.get("project_hint") or None, suffix) for i in items})
    return [n for n in names if _find_pages(tree.get("roots") or [], n)]


def existing_page_hits(up: Upstream, bank: str, items: List[Dict[str, Any]], suffix: str = "") -> Dict[str, List[Dict[str, Any]]]:
    """이 run 이 만들 페이지 중 이미 있는 것: 힌트("" = 전체) -> 같은 이름 페이지 노드 목록."""
    tree = up.request("GET", f"/v1/default/banks/{urllib.parse.quote(bank, safe='')}/knowledge-base/tree")
    out: Dict[str, List[Dict[str, Any]]] = {}
    for h in sorted({i.get("project_hint") or "" for i in items}):
        hits = _find_pages(tree.get("roots") or [], page_name(h or None, suffix))
        if hits:
            out[h] = [n for n, _ in hits]
    return out


def shared_pages(up: Upstream, bank: str, doc: Dict[str, Any], items: List[Dict[str, Any]],
                 suffix: str = "") -> Dict[str, Dict[str, Any]]:
    """--share-pages: 이미 있는 같은 이름 페이지의 source_query 가 이 run 의 질의와 같으면 새로 만들지 않고 읽는다(issue #323).
    다르거나(없음 포함), 같은 이름이 둘 이상이면 Refused - 어떤 질의도 보내기 전에 멈춘다."""
    reuse: Dict[str, Dict[str, Any]] = {}
    q = urllib.parse.quote(bank, safe="")
    for h, nodes in existing_page_hits(up, bank, items, suffix).items():
        name = page_name(h or None, suffix)
        if len(nodes) > 1:
            raise Refused(f"knowledge page {name} exists {len(nodes)} times: cannot share an ambiguous page")
        mm_id = nodes[0].get("mental_model_id")
        if not mm_id:               # 멘탈 모델이 없는 노드는 source_query 를 확인할 수 없다 - 공유하지 않고 멈춘다
            raise Refused(f"knowledge page {name} has no mental_model_id: cannot check its source_query, not shared")
        try:
            mm = up.request("GET", f"/v1/default/banks/{q}/mental-models/{urllib.parse.quote(str(mm_id), safe='')}")
        except UpstreamError as e:  # 지워진 멘탈 모델(404) 등 - traceback 대신 같은 거부 경로(fail-closed)
            raise Refused(f"knowledge page {name}: mental model lookup failed ({e}): not shared")
        if not isinstance(mm, dict) or mm.get("source_query") != page_query(doc, h or None):
            raise Refused(f"knowledge page {name} already exists with a different source_query: not shared "
                          "(use a per-set --page-suffix, bench/README.md)")
        reuse[h] = nodes[0]
    return reuse


def read_page_name(args, hint: str) -> Optional[str]:
    """읽기 전용 채점에서 힌트의 실제 페이지 이름. --page-map 이 있으면 그 표(힌트 없는 전체 페이지는 키 "-")만 따르고,
    표에 없는 힌트는 None(page-missing) - 기본 이름으로 짐작하지 않는다. 없으면 page_name(+ --page-suffix)."""
    if args.page_map is not None:
        return args.page_map.get(hint or "-")
    return page_name(hint or None, args.page_suffix)


def date_page_facts(up: Upstream, bank: str, based_on: Any) -> Any:
    """페이지(멘탈 모델) reflect_response.based_on 의 기억 항목에는 mentioned_at 이 없다(v1.1.0 실측: id·type·text·context 뿐).
    날짜 없는 항목은 그 id 로 GET memories/{id} 해 mentioned_at 을 채운다(읽기만). 조회가 실패하면 date_lookup=failed 를 남겨
    contamination 이 그 응답을 invalid(date-lookup-failed)로 둔다. 멘탈 모델·directives 항목은 건드리지 않는다."""
    if not isinstance(based_on, dict):
        return based_on
    out: Dict[str, Any] = {}
    for key, rows in based_on.items():
        if key in ("directives", "mental_models", "mental-models") or not isinstance(rows, list):
            out[key] = rows
            continue
        filled = []
        for r in rows:
            if isinstance(r, dict) and not r.get("mentioned_at") and r.get("id"):
                r = dict(r)
                try:
                    m = up.request("GET", f"/v1/default/banks/{bank}/memories/{urllib.parse.quote(str(r['id']), safe='')}")
                    r["mentioned_at"] = m.get("mentioned_at") if isinstance(m, dict) else None
                    if not isinstance(m, dict):
                        r["date_lookup"] = "failed"
                except UpstreamError as e:     # 404 = 재통합으로 지워진 관찰. 날짜 검사에서 빼고 deleted_evidence 로 센다
                    r["date_lookup"] = "deleted" if e.status == 404 else "failed"
                except Exception:              # 시간 초과(subprocess.TimeoutExpired)·깨진 응답도 이 항목의 조회 실패다 - run 을 죽이지 않는다
                    r["date_lookup"] = "failed"    # 조회 실패만 fail-closed. 조회 성공 + 날짜 없음은 undated(대상 정책을 따른다)
            filled.append(r)
        out[key] = filled
    return out


def deleted_evidence(based_on: Any) -> int:
    """based_on 에서 날짜 조회가 404(재통합으로 지워진 관찰)였던 항목 수."""
    if not isinstance(based_on, dict):
        return 0
    return sum(1 for rows in based_on.values() if isinstance(rows, list)
               for r in rows if isinstance(r, dict) and r.get("date_lookup") == "deleted")


def fetch_pages(args, doc, items, up: Upstream) -> Dict[str, Any]:
    """(b) 페이지 원문. 쓰기 대상이면 만들고 완료를 기다린다, 아니면 이름으로 찾아 읽기만 한다. 판정은 judge_detail.
    읽기 전용이면 멘탈 모델의 last_refreshed_at 도 남긴다 - delta 갱신 대기(재통합 뒤 refresh 완료) 확인용(issue #297)."""
    bank = urllib.parse.quote(args.bank, safe="")
    hints = sorted({i.get("project_hint") or "" for i in items})
    pages: Dict[str, Dict[str, Any]] = {}
    if args.create_pages:
        reuse = getattr(args, "reuse_pages", None) or {}
        for h in hints:
            if h in reuse:             # --share-pages: 같은 질의로 이미 만든 페이지를 읽는다(POST 없음)
                pages[h] = {"name": page_name(h or None, args.page_suffix), "page_id": reuse[h].get("id"),
                            "mental_model_id": reuse[h].get("mental_model_id"), "reused": True}
                continue
            body = page_body(doc, h or None, args.page_suffix)
            r = up.request("POST", f"/v1/default/banks/{bank}/knowledge-base/pages", body)
            pages[h] = {"name": body["name"], "page_id": r.get("page_id"), "mental_model_id": r.get("mental_model_id"),
                        "operation_id": r.get("operation_id"), "created": time.monotonic()}
        for h, p in pages.items():
            if p.get("reused"):
                continue
            op = _wait_op(up, bank, p["operation_id"], args.page_timeout)
            p["operation_status"] = op.get("status")
            p["seconds"] = round(time.monotonic() - p.pop("created"), 1)
    else:
        tree = up.request("GET", f"/v1/default/banks/{bank}/knowledge-base/tree")
        for h in hints:
            name = read_page_name(args, h)
            try:
                node = _find_page(tree.get("roots") or [], name) if name else None
            except AmbiguousPage as e:      # judge_detail 이 page-ambiguous 로 판정한다(page-missing 과 같은 경로별 invalid)
                print(f"arch_v2_regression: (b) {e}", file=sys.stderr)
                pages[h] = {"name": name, "ambiguous": {"matches": len(e.parents), "parents": e.parents, "error": str(e)}}
                continue
            if node:
                pages[h] = {"name": name, "page_id": node.get("id"), "mental_model_id": node.get("mental_model_id")}
    detail: Dict[str, Any] = {}
    for h in hints:
        p = pages.get(h)
        if p and p.get("ambiguous"):
            detail[h or "-"] = dict(p, query=page_query(doc, h or None))
            continue
        if not p or not p.get("mental_model_id"):
            continue                                   # judge_detail 이 page-missing 으로 판정한다
        mm = up.request("GET", f"/v1/default/banks/{bank}/mental-models/{p['mental_model_id']}?detail=full")
        rr = mm.get("reflect_response") or {}
        based_on = date_page_facts(up, bank, rr.get("based_on"))
        detail[h or "-"] = dict(p, query=page_query(doc, h or None), content=mm.get("content"), based_on=based_on,
                                last_refreshed_at=mm.get("last_refreshed_at"), deleted_evidence=deleted_evidence(based_on))
    return detail


def judge_detail(doc: Dict[str, Any], items: List[Dict[str, Any]], detail: Dict[str, Any],
                 undated_ok: bool) -> Dict[str, Dict[str, Dict[str, str]]]:
    """저장된 원문(detail.json)만으로 판정한다. score 와 rescore 가 같은 함수를 쓴다(판정은 응답의 순수 함수)."""
    cutoff = parse_ts(doc["ignore_mentioned_at_from"])
    out: Dict[str, Dict[str, Dict[str, str]]] = {}
    if "a_reflect_list" in detail:
        res = detail["a_reflect_list"]["response"]
        out["a_reflect_list"] = judge_sections(res.get("text") or "", iter_facts(res.get("based_on")), items,
                                               cutoff, undated_ok)
    if "b_knowledge_page_status" in detail:
        pages, v = detail["b_knowledge_page_status"], {}
        for i in items:
            pg = pages.get(i.get("project_hint") or "-")
            if not pg:
                v[i["id"]] = {"verdict": "invalid", "reason": "page-missing"}
            elif pg.get("ambiguous"):
                v[i["id"]] = {"verdict": "invalid", "reason": "page-ambiguous"}
            else:
                v.update(judge_sections(pg.get("content") or "", iter_facts(pg.get("based_on")), [i], cutoff, undated_ok,
                                        project_scoped=bool(i.get("project_hint"))))
        out["b_knowledge_page_status"] = v
    if "c_recall_status" in detail:
        rows = detail["c_recall_status"]
        out["c_recall_status"] = {i["id"]: judge_recall((rows.get(i["id"]) or {}).get("results") or [], i, cutoff,
                                                        undated_ok) if i["id"] in rows else
                                  {"verdict": "invalid", "reason": "no-response"} for i in items}
    return out


def v1_items(doc: Dict[str, Any], items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """v1 판정 대상: partial 항목(issue #338)은 v1 집계에서 뺀다(v1 출력 불변)."""
    part = partial_ids(doc)
    return [i for i in items if i["id"] not in part]


def cmd_score(args, doc, target, gw, up, out: Path) -> int:
    items = active_items(doc, load_evidence(args.evidence))
    meta = item_meta(doc, args.sets, args.key_split, [i["id"] for i in items]) if args.new_mode else None
    paths = [p for p in SCORE_PATHS if p[0] in args.paths]
    if "d_recall_list" in paths and not doc.get("list_query"):     # 어떤 질의도 보내기 전에 멈춘다
        raise Refused("path d needs list_query in the answer key (set C)")
    if "d_recall_list" in paths and args.judge != "v2":           # (d)는 v2 전용: v1 이면 질의만 하고 판정이 어디에도 남지 않는다
        raise Refused("path d is judged by v2 only: add --judge v2")
    run = ensure_out_dir(str(out / ("score-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S"))))
    detail: Dict[str, Any] = {"meta": {"target": target.kind, "undated_ok": target.undated_ok,
                                       "set": [i["id"] for i in items],
                                       "pages": {"mode": "create" if args.create_pages else "read",
                                                 "suffix": args.page_suffix, "map": args.page_map}}}
    if args.share_pages:
        detail["meta"]["pages"]["share"] = True
    if args.new_mode:
        detail["meta"]["sets"] = args.set_names
    metrics: Dict[str, Dict[str, Any]] = {}
    if "b_knowledge_page_status" in paths and args.create_pages:
        taken = [] if args.share_pages else existing_pages(up, args.bank, items, args.page_suffix)
        if args.share_pages:     # 같은 source_query 면 읽기, 다르면 여기서 멈춘다(issue #323)
            args.reuse_pages = shared_pages(up, args.bank, doc, items, args.page_suffix)
        if taken:   # 같은 이름을 다시 POST 하지 않는다. 어떤 질의(reflect 포함)도 보내기 전에 멈춘다
            raise Refused(f"knowledge page(s) {', '.join(taken)} already exist on this target: score on a fresh "
                          "copy, or rescore the earlier run's detail.json")
    try:
        _score_paths(args, doc, items, paths, gw, up, detail, metrics)
    finally:                    # 뒤 경로가 실패해도 끝난 경로의 원문은 남긴다(rescore 로 판정할 수 있게)
        _write(run / "detail.json", detail)
        _write(run / "metrics.json", metrics)
    verdicts = judge_detail(doc, v1_items(doc, items), detail, target.undated_ok)
    detail["reasons"] = verdicts
    v2 = judge_detail_v2(doc, items, detail, target.undated_ok) if args.judge == "v2" else None
    if v2 is not None:
        detail["reasons_v2"] = v2
    _write(run / "detail.json", detail)
    summary = {"target": target.kind, "set": [i["id"] for i in items], "paths": summarize(verdicts),
               "query_timestamp": QUERY_TIMESTAMP, "created": now_iso()}
    if meta is not None:
        summary["sets"] = args.set_names
        add_breakdown(summary, meta)
    if v2 is not None:
        summary["v2"] = v2_block(doc, v2)
    if "b_knowledge_page_status" in detail:      # 재통합으로 지워진 근거 수(페이지 합계) - arm 보고서에 적는다
        summary["deleted_evidence"] = {"b_knowledge_page_status": sum(
            (p or {}).get("deleted_evidence") or 0 for p in detail["b_knowledge_page_status"].values())}
    _write(run / "summary.json", summary)
    print(run / "summary.json")
    return 0


def _score_paths(args, doc, items, paths, gw, up, detail: Dict[str, Any], metrics: Dict[str, Dict[str, Any]]) -> None:
    for p in paths:
        t0, start = time.monotonic(), now_iso()
        n0 = len(gw.calls)
        if p == "a_reflect_list":
            res = gw.call("reflect", reflect_args(doc, args.reflect_budget))
            detail[p] = {"query": reflect_query(doc), "response": res}
            queries = len(gw.calls) - n0
        elif p == "b_knowledge_page_status":
            u0 = sum(1 for m, _ in up.calls if m == "POST")
            detail[p] = fetch_pages(args, doc, items, up)
            queries = sum(1 for m, _ in up.calls if m == "POST") - u0 if args.create_pages else \
                sum(1 for v in detail[p].values() if not v.get("ambiguous"))   # 모호한 힌트는 페이지를 조회하지 않았다
        elif p == "d_recall_list":
            res = gw.call("recall", list_recall_args(doc))
            detail[p] = {"query": list_recall_query(doc), "results": res.get("results")}
            queries = len(gw.calls) - n0
        else:
            rows = {}
            for it in items:
                res = gw.call("recall", recall_args(doc, it))
                rows[it["id"]] = {"query": recall_query(doc, it), "results": res.get("results")}
            detail[p] = rows
            queries = len(gw.calls) - n0
        metrics[p] = {"queries": queries, "seconds": round(time.monotonic() - t0, 1),
                      "llm_calls": _llm_total(up, args.bank, start, now_iso()), "start": start, "end": now_iso()}


def inject_evidence(detail: Dict[str, Any], rows: Dict[str, List[Dict[str, Any]]]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """원인 층 분리 진단(issue #342, 판정 입력 아님): 항목 id -> 완료 근거 기억 행을 recall 경로 결과 끝에 더한 사본 detail 을 돌려준다.
    (c)는 그 항목 결과에, (d)는 결과 집합 하나에 모든 항목의 행을 더한다. 이미 결과에 있는 기억(id)은 넣지 않는다.
    (a)(b)는 합성 단계라 손대지 않는다. 판정 함수는 그대로이고 입력만 바뀐다. 원본 detail 은 바꾸지 않는다."""
    new = json.loads(json.dumps(detail))
    report: Dict[str, Any] = {}

    def add(results: List[Dict[str, Any]], extra: List[Dict[str, Any]]) -> Dict[str, List[str]]:
        have = {r.get("id") for r in results}
        rep: Dict[str, List[str]] = {"appended": [], "already": []}
        for r in extra:
            if r.get("id") in rep["appended"] or r.get("id") in rep["already"]:
                continue            # (d): 여러 항목이 같은 근거 기억을 나눠 가지면 한 번만 센다
            rep["already" if r.get("id") in have else "appended"].append(r.get("id"))
            if r.get("id") not in have:
                results.append(json.loads(json.dumps(r)))
                have.add(r.get("id"))
        return rep

    if "c_recall_status" in new:
        sec = new["c_recall_status"]
        rep_c: Dict[str, Any] = {"missing": [], "skipped_no_results": []}
        for iid, extra in rows.items():
            if iid not in sec:            # 그 항목 질의 원문이 없다: 넣지 않고 표시만 한다
                rep_c["missing"].append(iid)
            elif sec[iid].get("results") is None:   # 원래 invalid(no-provenance): 넣으면 유효 판정으로 바뀐다
                rep_c["skipped_no_results"].append(iid)
            else:
                sec[iid]["results"] = list(sec[iid]["results"])
                rep_c[iid] = add(sec[iid]["results"], extra)
        report["c_recall_status"] = rep_c
    if "d_recall_list" in new:
        sec = new["d_recall_list"]
        if sec.get("results") is None:
            report["d_recall_list"] = {"appended": [], "already": [], "skipped_no_results": True}
        else:
            sec["results"] = list(sec["results"])
            report["d_recall_list"] = add(sec["results"], [r for iid in rows for r in rows[iid]])
            report["d_recall_list"]["skipped_no_results"] = False
    return new, report


def cmd_rescore(args, doc, out: Path) -> int:
    """score 가 남긴 detail.json 을 다시 판정한다(질의 0회). 대상 정책(undated_ok)과 세트는 그 run 의 기록을 따른다."""
    src = Path(args.detail)
    with open(src, encoding="utf-8") as fh:
        detail = json.load(fh)
    meta = detail.get("meta")
    if meta is None:            # meta 이전 run: 같은 디렉터리 summary.json 의 target·set
        with open(src.parent / "summary.json", encoding="utf-8") as fh:
            old = json.load(fh)
        meta = {"target": old["target"], "undated_ok": old["target"] == "next", "set": old["set"]}
    by_id = {i["id"]: i for i in all_items(doc)}
    unknown = [i for i in meta["set"] if i not in by_id]
    if unknown:
        raise SystemExit(f"rescore: {unknown} scored in {src} are not in the answer key(s)")
    items = [by_id[i] for i in meta["set"]]
    if args.evidence:           # 새 evidence 로 세트를 좁힌다(원문이 없는 항목은 더할 수 없다)
        keep = {i["id"] for i in active_items(doc, load_evidence(args.evidence))}
        missing = keep - partial_ids(doc) - set(meta["set"])   # partial 은 근거 확인 대상이 아니다: 채점 안 된 partial 은 그냥 빠진다
        if missing:
            raise SystemExit(f"rescore: {sorted(missing)} were not scored in {src}")
        items = [i for i in items if i["id"] in keep]
        meta = dict(meta, set=[i["id"] for i in items])
    inject = None
    if args.inject_evidence:    # 진단 조건(issue #342): 판정 함수가 아니라 입력에만 근거 행을 더한다
        with open(args.inject_evidence, "rb") as fh:
            raw = fh.read()
        rows = json.loads(raw)
        no_id = sorted(iid for iid, rs in rows.items() if any(not isinstance(r, dict) or not r.get("id") for r in rs))
        if no_id:                # id 없는 행은 중복 제거에서 서로 같은 행으로 보여 조용히 빠진다
            raise SystemExit(f"rescore --inject-evidence: rows without id for {no_id}")
        stray = sorted(set(rows) - set(meta["set"]))
        if stray:
            raise SystemExit(f"rescore --inject-evidence: {stray} are not in the scored set of {src}")
        detail, rep = inject_evidence(detail, rows)
        inject = {"source": args.inject_evidence, "sha256": hashlib.sha256(raw).hexdigest(), "diagnostic": True,
                  "paths": rep}
    verdicts = judge_detail(doc, v1_items(doc, items), detail, meta["undated_ok"])
    run = ensure_out_dir(str(out / ("rescore-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S"))))
    _write(run / "reasons.json", verdicts)
    summary = {"target": meta["target"], "set": meta["set"], "paths": summarize(verdicts),
               "query_timestamp": QUERY_TIMESTAMP, "source": str(src), "created": now_iso()}
    if args.new_mode:
        summary["sets"] = args.set_names
        add_breakdown(summary, item_meta(doc, args.sets, args.key_split, meta["set"]))
    if args.judge == "v2":           # v1 블록·필드는 그대로 두고 v2 블록만 더한다(issue #338)
        v2 = judge_detail_v2(doc, items, detail, meta["undated_ok"])
        _write(run / "reasons-v2.json", v2)
        summary["v2"] = v2_block(doc, v2)
    if inject is not None:
        summary["inject"] = inject
    _write(run / "summary.json", summary)
    if (src.parent / "metrics.json").exists():
        _write(run / "metrics.json", json.load(open(src.parent / "metrics.json", encoding="utf-8")))
    print(run / "summary.json")
    return 0


def page_refresh_state(up: Upstream, bank: str) -> Dict[str, Any]:
    """(b) delta 갱신 대기 판정 재료(issue #297 사본 실측): 트리에서 is_stale 인 페이지, 진행 중인 refresh_mental_model 작업 수.
    통합이 끝나면 범위에 새 관찰이 생긴 페이지만 stale 이 되고 곧바로 refresh 작업이 생긴다 - 범위가 조용한 페이지는
    갱신되지 않으므로(last_refreshed_at 이 그대로) "모든 페이지의 갱신 시각이 바뀔 때까지"로 기다리면 끝나지 않는다."""
    tree = up.request("GET", f"/v1/default/banks/{bank}/knowledge-base/tree")
    stale: List[str] = []

    def walk(nodes: List[Dict[str, Any]]) -> None:
        for n in nodes or []:
            if n.get("kind") == "page" and n.get("is_stale"):
                stale.append(n.get("name"))
            walk(n.get("children") or [])
    walk(tree.get("roots") or [])
    ops: List[Dict[str, Any]] = []
    offset, page = 0, 100
    while True:                                  # 끝까지 넘겨 본다 - 재통합은 작업을 수백 건 만들어 첫 페이지 밖에 refresh 가 있을 수 있다
        res = up.request("GET", f"/v1/default/banks/{bank}/operations?limit={page}&offset={offset}")
        rows = (res.get("operations") or res.get("items") or []) if isinstance(res, dict) else (res or [])
        ops += rows
        total = res.get("total") if isinstance(res, dict) else None
        offset += len(rows)
        if len(rows) < page or (isinstance(total, int) and offset >= total):
            break
    live = sum(1 for o in ops if o.get("operation_type") == "refresh_mental_model"
               and o.get("status") in ("pending", "processing"))
    return {"pages_stale": sorted(stale), "refresh_live": live}


def cmd_stats(args, doc, target, gw, up, out: Path) -> int:
    """--wait 초 동안 pending_consolidation 0 을 기다린다. --wait-pages 면 그 뒤 페이지 delta 갱신까지
    (stale 페이지 0, 진행 중인 refresh_mental_model 0) 기다린다. 이 완료는 연속 2회 poll 이 같을 때만 선언한다 -
    통합이 끝난 순간과 페이지 stale 표시·refresh 작업 생성 사이에 poll 이 끼면 한 번은 완료처럼 보인다. 못 채우면 exit 2."""
    bank = urllib.parse.quote(args.bank, safe="")
    deadline = time.monotonic() + args.wait
    streak = 0
    while True:
        s = up.request("GET", f"/v1/default/banks/{bank}/stats")
        row = {"documents": s.get("total_documents"), "observations": s.get("total_observations"),
               "pending_consolidation": s.get("pending_consolidation"), "pending_operations": s.get("pending_operations"),
               "failed_operations": s.get("failed_operations"), "nodes_by_fact_type": s.get("nodes_by_fact_type"),
               "at": now_iso()}
        done = row["pending_consolidation"] == 0
        if args.wait_pages:
            row.update(page_refresh_state(up, bank))
            streak = streak + 1 if done and not row["pages_stale"] and row["refresh_live"] == 0 else 0
            row["done_polls"] = streak
            done = streak >= 2
        if not args.wait or done or time.monotonic() > deadline:
            print(json.dumps(row, sort_keys=True))
            return 0 if not args.wait or done else 2
        if args.wait_pages:
            print(json.dumps(row, sort_keys=True), flush=True)      # 진행 줄(마지막 줄이 판정)
        time.sleep(15 if args.wait_pages else 30)


# ---------------------------------------------------------------- arm 판정 (issue #323, 스펙 #318 "arm 판정")

JUDGE_PATHS = ("a", "b-full", "c")
MANIFEST_PATHS = {"a": "a_reflect_list", "b-delta": "b_knowledge_page_status", "b-full": "b_knowledge_page_status",
                  "c": "c_recall_status"}
A_VALID_RUNS = 3                 # 측정 상태마다 (a) 유효 run(대조군 open 1건 이상) 3개
A_MAX_ATTEMPTS = 6               # 시도 상한
VERDICT_KO = {"pass": "합격", "improvement": "개선", "no-effect": "효과 없음", "undecidable": "판정 불가",
              "unmeasured": "미측정", "baseline": "기준선"}


def _sha256_file(path: str) -> str:
    import hashlib
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _mean(xs: List[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def path_state(path: str, counts: List[Dict[str, Dict[str, int]]], two_generations: bool) -> Dict[str, Any]:
    """한 측정 상태의 한 경로. counts = summary 마다 sample_counts (시도 순서).
    (a) 앞 6개 시도 가운데 앞에서부터 유효 run 3개(invalid 있는 run 은 그 경로를 판정 불가로, 대조군 open 0 run 은 집계에서 뺀다),
    (b) 기준선(two_generations)은 두 생성, 처치는 하나, (c) 하나. value = 쓴 sample 의 합친 세트 끝난 항목 open 평균."""
    st: Dict[str, Any] = {"summaries": len(counts)}
    if path == "a":
        used, zero, bad, tried = [], 0, 0, 0
        for c in counts[:A_MAX_ATTEMPTS]:
            tried += 1
            al = c["all"]
            if al["invalid"]:
                bad += 1
            elif al["controls_open"] == 0:
                zero += 1
            else:
                used.append(c)
            if len(used) == A_VALID_RUNS:
                break
        why = "invalid" if bad else ("valid-runs-short" if len(used) < A_VALID_RUNS else None)
        st.update(attempts=tried, valid_runs=len(used), zero_control_runs=zero, invalid_runs=bad)
    else:
        need = 2 if two_generations else 1
        used = counts[:need]
        why = "invalid" if any(c["all"]["invalid"] for c in used) else \
            (("missing" if not used else "generations-short") if len(used) < need else None)
    st["undecidable"] = why if counts else "missing"
    vals = [c["all"]["closed_open"] for c in used]
    st["value"] = _mean(vals)
    st["controls"] = _mean([c["all"]["controls_open"] for c in used])
    st["spread"] = (max(vals) - min(vals)) if vals else 0
    st["pass"] = st["undecidable"] is None and all(
        c["all"]["closed_open"] == 0 and c["all"]["invalid"] == 0 and c["all"]["controls_open"] == c["all"]["controls_total"]
        for c in used)
    scopes = sorted({sc for c in used for sc in c})
    st["scopes"] = {sc: {"closed_open": _mean([c.get(sc, {}).get("closed_open", 0) for c in used]),
                         "controls_open": _mean([c.get(sc, {}).get("controls_open", 0) for c in used]),
                         "closed_total": used[0].get(sc, {}).get("closed_total", 0) if used else 0,
                         "controls_total": used[0].get(sc, {}).get("controls_total", 0) if used else 0}
                    for sc in scopes}
    return st


def _states_of(arm: Dict[str, Any], load: Callable[[str, str], Dict[str, Dict[str, int]]]) -> List[Dict[str, Any]]:
    """완료 반복만 상태로 편다(중단·무효 반복은 없는 것으로 친다)."""
    base = arm.get("role") == "baseline"
    out = []
    for r in arm.get("repeats") or []:
        if r.get("state") != "completed":
            continue
        st = {}
        for p in MANIFEST_PATHS:
            files = r.get(p)
            if files is None:
                continue
            files = [files] if isinstance(files, str) else list(files)
            st[p] = path_state(p, [load(f, MANIFEST_PATHS[p]) for f in files], two_generations=base and p == "b-full")
        for p in JUDGE_PATHS:
            st.setdefault(p, path_state(p, [], two_generations=base and p == "b-full"))
        out.append(st)
    return out


def judge_one(states: List[Dict[str, Any]], base: Optional[Dict[str, Any]], role: str,
              completed: int) -> Dict[str, Any]:
    """처치 arm(states = 완료 반복 앞 2개) 또는 "읽기 경로만"(states = 기준선 main 상태 1개, role baseline)을 기준선 상태와 비교한다."""
    res: Dict[str, Any] = {"completed_repeats": completed, "paths": {}}
    if completed == 0:
        res.update(verdict="unmeasured")
        return res
    undec: List[str] = []
    for p in JUDGE_PATHS:
        vs = [s[p]["value"] for s in states]
        why = next((s[p]["undecidable"] for s in states if s[p]["undecidable"]), None)
        if why is None and base is None:
            why = "baseline-unmeasured"
        elif why is None and base[p]["undecidable"]:
            why = "baseline-" + base[p]["undecidable"]
        known = [v for v in vs if v is not None]
        gap = (max(known) - min(known)) if len(known) > 1 else 0
        w = max([gap, base[p]["spread"] if base else 0] + ([states[0][p]["spread"]] if role == "baseline" else []))
        scopes = sorted({sc for s in states for sc in s[p]["scopes"]})
        row = {"values": vs, "controls": [s[p]["controls"] for s in states], "W": w, "undecidable": why,
               "baseline": base[p]["value"] if base else None, "baseline_controls": base[p]["controls"] if base else None,
               "scopes": {sc: {k: [s[p]["scopes"].get(sc, {}).get(k) for s in states] for k in ("closed_open", "controls_open")}
                          for sc in scopes},
               "baseline_scopes": base[p]["scopes"] if base else None}
        if p == "a":
            row.update({k: [s[p].get(k) for s in states] for k in ("attempts", "valid_runs", "zero_control_runs")})
        res["paths"][p] = row
        if why:
            undec.append(p)
    if all("b-delta" in s for s in states):        # delta 는 판정에 쓰지 않고 싣기만 한다
        res["paths"]["b-delta"] = {"values": [s["b-delta"]["value"] for s in states],
                                   "controls": [s["b-delta"]["controls"] for s in states],
                                   "undecidable": next((s["b-delta"]["undecidable"] for s in states
                                                        if s["b-delta"]["undecidable"]), None),
                                   "scopes": {sc: {k: [s["b-delta"]["scopes"].get(sc, {}).get(k) for s in states]
                                                   for k in ("closed_open", "controls_open")}
                                              for sc in sorted({sc for s in states for sc in s["b-delta"]["scopes"]})}}
    res["a_zero_control_runs"] = [s["a"].get("zero_control_runs") for s in states]
    res["undecidable_paths"] = undec
    # 처치 arm 의 (a)가 대조군 0 run 때문에 유효 run 을 못 채웠으면 개선이 될 수 없다(처치가 대조군을 닫았을 수 있다)
    blocked = role == "treatment" and any(s["a"].get("zero_control_runs") and (s["a"].get("valid_runs") or 0) < A_VALID_RUNS
                                          for s in states)
    res["improvement_blocked"] = blocked
    if len(undec) >= 2 or (role == "treatment" and completed < 2):
        res["verdict"] = "undecidable"
        res["reason"] = "single-repeat" if role == "treatment" and completed < 2 else "undecidable-paths>=2"
        return res
    if not undec and all(s[p]["pass"] for s in states for p in JUDGE_PATHS):
        res["verdict"] = "pass"
        return res
    dec = [p for p in JUDGE_PATHS if p not in undec]
    rows = res["paths"]
    better = [p for p in dec if all(v < rows[p]["baseline"] - rows[p]["W"] for v in rows[p]["values"])]
    ok = len(better) >= (len(dec) if undec else 2)
    ok = ok and all(v <= rows[p]["baseline"] + rows[p]["W"] for p in dec if p not in better for v in rows[p]["values"])
    ok = ok and all(c >= rows[p]["baseline_controls"] for p in dec for c in rows[p]["controls"])
    res["better_paths"] = better
    res["verdict"] = "improvement" if ok and not blocked else "no-effect"
    return res


def judge_arms(manifest: Dict[str, Any], base_dir: Any) -> Dict[str, Any]:
    """manifest(bench/README.md "judge-arms manifest")의 arm 들을 판정한다. 같은 판정 규칙을 처치 arm 과 "읽기 경로만"에 쓴다."""
    root = Path(base_dir)

    def rp(p: str) -> str:            # realpath: `./s1.json`·`s1.json`·심링크가 같은 파일로 비교된다
        q = Path(os.path.expanduser(p))
        return os.path.realpath(str(q if q.is_absolute() else root / q))

    names = list(manifest["answer_keys"])
    doc, sets, _ = load_answer_keys([rp(manifest["answer_keys"][n]) for n in names], names)
    split = None
    if manifest.get("key_split"):
        with open(rp(manifest["key_split"]), encoding="utf-8") as fh:
            split = json.load(fh)
    inputs: Dict[str, str] = {}
    summaries: Dict[str, Dict[str, Any]] = {}
    pairs = [(p, f) for arm in manifest["arms"].values() for r in arm.get("repeats") or [] if r.get("state") == "completed"
             for p in MANIFEST_PATHS if r.get(p) is not None for f in ([r[p]] if isinstance(r[p], str) else r[p])]
    seen_pairs: set = set()
    ref_set: Optional[List[str]] = None
    for p, f in pairs:
        f = rp(f)
        # 같은 (파일, 경로 키)를 두 run·두 생성·두 반복·두 arm 으로 세면 W 가 거짓으로 줄어든다.
        # 한 summary(--paths abc)를 다른 경로 키(b-full 과 c 등)에 쓰는 것은 다른 판정을 읽으므로 허용한다
        if (f, p) in seen_pairs:
            raise SystemExit(f"judge-arms: {f} is listed more than once under path {p!r} in the manifest")
        seen_pairs.add((f, p))
        if f in summaries:
            continue
        with open(f, encoding="utf-8") as fh:
            summaries[f] = json.load(fh)
        inputs[f] = _sha256_file(f)
        sset = sorted(summaries[f]["set"])
        if ref_set is None:
            ref_set = sset
        elif sset != ref_set:
            raise SystemExit(f"judge-arms: {f} scored a different item set than the other summaries")
    meta = item_meta(doc, sets, split, ref_set or [])

    def load(f: str, key: str) -> Dict[str, Dict[str, int]]:
        sm = summaries[rp(f)]
        if key not in (sm.get("paths") or {}):
            raise SystemExit(f"judge-arms: {f} has no {key} verdicts")
        return sample_counts(sm["paths"][key], meta)

    states = {n: _states_of(arm, load) for n, arm in manifest["arms"].items()}
    out: Dict[str, Any] = {"arms": {}, "inputs": inputs, "set_size": len(ref_set or []), "created": now_iso()}
    for n, arm in manifest["arms"].items():
        if arm.get("role") == "baseline":
            st = states[n][0] if states[n] else None
            out["arms"][n] = {"role": "baseline", "verdict": "baseline" if st else "unmeasured",
                              "paths": {p: {"values": [st[p]["value"]], "spread": st[p]["spread"],
                                            "controls": [st[p]["controls"]], "undecidable": st[p]["undecidable"],
                                            "scopes": st[p]["scopes"],
                                            **({k: [st[p].get(k)] for k in ("attempts", "valid_runs", "zero_control_runs")}
                                               if p == "a" else {})}
                                        for p in JUDGE_PATHS} if st else {}}
            continue
        bname = arm.get("baseline")
        if bname not in manifest["arms"] or manifest["arms"][bname].get("role") != "baseline":
            raise SystemExit(f"judge-arms: arm {n} baseline {bname!r} is not a baseline arm in the manifest")
        base = states[bname][0] if states[bname] else None
        r = judge_one(states[n][:2], base, "treatment", len(states[n]))
        out["arms"][n] = dict(r, role="treatment", baseline=bname)
    rpo = manifest.get("read_path_only")
    if rpo:
        a, b = rpo["arm"], rpo["baseline"]
        if a == b:                  # 자기 자신과 비교하면 측정 없이 판정이 나온다
            raise SystemExit(f"judge-arms: read_path_only arm and baseline are both {a!r}")
        for x in (a, b):
            if manifest["arms"].get(x, {}).get("role") != "baseline":
                raise SystemExit(f"judge-arms: read_path_only {x!r} is not a baseline arm")
        base = states[b][0] if states[b] else None
        r = judge_one(states[a][:1], base, "baseline", len(states[a][:1]))
        out["read_path_only"] = dict(r, arm=a, baseline=b)
    return out


def _f(v: Any) -> str:
    if v is None:
        return "-"
    return f"{v:.2f}" if isinstance(v, float) else str(v)


def render_judgement(res: Dict[str, Any]) -> str:
    """판정 출력: arm 한 줄 판정, 경로별 표(반복 값·기준선·W·대조군), 세트·키 범위별 값, 입력 summary sha256. id·수만 쓴다."""
    lines: List[str] = []
    rows = [(n, r) for n, r in res["arms"].items() if r.get("role") == "treatment"]
    if "read_path_only" in res:
        rows.append(("read-path-only(" + res["read_path_only"]["arm"] + ")", res["read_path_only"]))
    for n, r in rows:
        extra = f" ({r['reason']})" if r.get("reason") else ""
        lines.append(f"## {n} vs {r.get('baseline')}: {VERDICT_KO[r['verdict']]} [{r['verdict']}]{extra}")
        if r["verdict"] == "unmeasured":
            lines.append("")
            continue
        lines.append(f"- 판정 불가 경로: {', '.join(r['undecidable_paths']) or '-'}; (a) 대조군 0 run 수(반복별): "
                     f"{' / '.join(_f(x) for x in r['a_zero_control_runs'])}; 개선 막힘: {r['improvement_blocked']}")
        lines += ["", "| 경로 | 반복 값(끝난 open) | 기준선 | W | 대조군 open(반복별) | 기준선 대조군 | 판정 불가 사유 |",
                  "|---|---|---|---|---|---|---|"]
        for p in JUDGE_PATHS + ("b-delta",):
            x = r["paths"].get(p)
            if not x:
                continue
            lines.append(f"| {p} | {' / '.join(_f(v) for v in x['values'])} | {_f(x.get('baseline'))} | {_f(x.get('W'))} | "
                         f"{' / '.join(_f(v) for v in x['controls'])} | {_f(x.get('baseline_controls'))} | "
                         f"{x.get('undecidable') or '-'} |")
        lines += ["", "| 범위 | " + " | ".join(p for p in JUDGE_PATHS + ("b-delta",) if p in r["paths"]) + " |",
                  "|---|" + "---|" * sum(1 for p in JUDGE_PATHS + ("b-delta",) if p in r["paths"])]
        scopes = sorted({sc for x in r["paths"].values() for sc in x.get("scopes", {})}, key=lambda s: (s != "all", s))
        for sc in scopes:
            cells = []
            for p in JUDGE_PATHS + ("b-delta",):
                x = r["paths"].get(p)
                if not x:
                    continue
                v = x["scopes"].get(sc, {})
                b = ((x.get("baseline_scopes") or {}).get(sc) or {}).get("closed_open")
                cells.append(f"{' / '.join(_f(t) for t in v.get('closed_open', []))} (기준선 {_f(b)})"
                             if "W" in x else ' / '.join(_f(t) for t in v.get('closed_open', [])))
            lines.append(f"| {sc} | " + " | ".join(cells) + " |")
        lines.append("")
    lines.append("입력 summary sha256:")
    lines += [f"- {h}  {display_path(f)}" for f, h in sorted(res["inputs"].items())]
    return "\n".join(lines) + "\n"


def display_path(path: str) -> str:
    """출력용 경로: 리포 안이면 리포 상대, 리포 밖 홈 아래면 `~` 접두(issue #338: 보고서에 개인 경로를 남기지 않는다)."""
    a = os.path.abspath(os.path.expanduser(path))
    repo = os.path.abspath(REPO)
    if a.startswith(repo + os.sep):
        return os.path.relpath(a, repo)
    home = os.path.abspath(os.path.expanduser("~"))
    return "~" + a[len(home):] if a.startswith(home + os.sep) else a


# ---------------------------------------------------------------- 판정 v2 짝 비교 (issue #338, 사전 등록 1부 1.1·1.5). 동결 대상.

def pair_path_of(key: str) -> Optional[V2Path]:
    """manifest 경로 키(a, b-full, b-delta, c, 등록된 새 경로) -> 경로 등록표 줄."""
    for p in V2_REGISTRY.values():
        if key == p.short or key.startswith(p.short + "-"):
            return p
    return None


def pair_repeat_state(p: V2Path, runs: List[Dict[str, str]], ctrl: set) -> Dict[str, Any]:
    """한 반복의 항목 판정. runs = summary 마다 v2 항목 판정(시도 순서).
    valid3((a)): 앞 6시도 중 invalid 0·대조군 open 1건 이상인 run 을 앞에서부터 3개. all: 모든 생성.
    쓴 run 들의 항목별 다수결: open 이 절반 이상이면 open(동률이면 open), 아니면 done 이 unknown 보다 많으면 done, 아니면 unknown.
    all 에서 한 run 이라도 invalid 인 항목은 invalid."""
    if p.runs == "valid3":
        used = []
        for vs in runs[:A_MAX_ATTEMPTS]:
            if "invalid" in vs.values() or not any(vs.get(c) == "open" for c in ctrl):
                continue
            used.append(vs)
            if len(used) == A_VALID_RUNS:
                break
        short = len(used) < A_VALID_RUNS
    else:
        used, short = list(runs), not runs
    items: Dict[str, str] = {}
    for i in sorted({i for vs in used for i in vs}):
        vals = [vs[i] for vs in used if i in vs]
        if "invalid" in vals:
            items[i] = "invalid"
        elif 2 * vals.count("open") >= len(vals):
            items[i] = "open"
        else:
            items[i] = "done" if vals.count("done") > vals.count("unknown") else "unknown"
    return {"items": items, "runs": len(runs), "used": len(used), "undecidable": "valid-runs-short" if short else None}


def pair_baseline(states: List[Dict[str, str]]) -> Dict[str, str]:
    """기준선 반복 k 개의 항목 판정(사전 등록 1부 1.5): 모두 open 이면 open, 모두 not-open 이면 not-open, 섞이면 unstable,
    한 반복이라도 invalid 이거나 없으면 invalid."""
    out: Dict[str, str] = {}
    for i in sorted({i for st in states for i in st}):
        vals = [st.get(i, "invalid") for st in states]
        if "invalid" in vals:
            out[i] = "invalid"
        elif all(v == "open" for v in vals):
            out[i] = "open"
        elif all(v != "open" for v in vals):
            out[i] = "not-open"
        else:
            out[i] = "unstable"
    return out


PAIR_FIELDS = ("open_to_done", "open_to_unknown", "open_kept", "notopen_to_open", "notopen_kept", "unstable", "invalid")


def pair_transitions(base: Dict[str, str], treat: Dict[str, str], cat: Dict[str, str]) -> Dict[str, Dict[str, int]]:
    """범주(closed·controls·partial)별 전이 수. 대조군의 open_to_done + open_to_unknown 이 손실이다."""
    out = {c: {k: 0 for k in PAIR_FIELDS} for c in ("closed", "controls", "partial")}
    for i in sorted(set(base) & set(treat)):
        row = out[cat.get(i, "closed")]
        b, t = base[i], treat[i]
        if b == "invalid" or t == "invalid":
            row["invalid"] += 1
        elif b == "unstable":
            row["unstable"] += 1
        elif b == "open":
            row["open_kept" if t == "open" else ("open_to_done" if t == "done" else "open_to_unknown")] += 1
        else:
            row["notopen_to_open" if t == "open" else "notopen_kept"] += 1
    return out


def pair_gain(p: V2Path, tr: Dict[str, Dict[str, int]], null: Optional[float]) -> Dict[str, Any]:
    """이득(사전 등록 1부 1.1): 항목 경로는 끝난 항목 open -> done, 목록 경로는 open -> done + open -> unknown 이되 그 경로
    대조군 손실이 영분포 이하일 때만. 반대 방향(not-open -> open)은 빼지도 더하지도 않고 따로 싣는다.
    효과 = 이득 > 영분포(1.5). 영분포가 없으면 어느 경로든 이득을 정하지 않는다(no-null). 목록 경로 대조군 손실이
    영분포를 넘으면 이득 0·효과 False(control-loss>null)."""
    c = tr["closed"]
    loss = tr["controls"]["open_to_done"] + tr["controls"]["open_to_unknown"]
    raw = c["open_to_done"] + (c["open_to_unknown"] if p.kind == "list" else 0)
    res: Dict[str, Any] = {"raw": raw, "control_loss": loss, "null": null}
    if null is None:                    # 영분포가 없으면 어느 경로든 이득을 정하지 않는다
        res.update(gain=None, status="no-null")
    elif p.kind == "list" and loss > null:
        res.update(gain=0, status="control-loss>null")     # 손실이 영분포를 넘으면 이득을 세지 않는다(효과 아님, 판정 불가 아님)
    else:
        res.update(gain=raw, status="ok")
    res["effect"] = None if res["gain"] is None else res["gain"] > null
    return res


def judge_pairs(manifest: Dict[str, Any], base_dir: Any, nulls: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """처치 arm(과 read_path_only)마다 기준선 arm 과 항목별로 짝짓는다. 기준선은 반복 전부를 쓴다(judge-arms 는 첫 반복만).
    summary 의 v2 블록(rescore --judge v2)을 읽는다. 경로는 manifest 반복에 있는 키 중 경로 등록표에 있는 것 전부다."""
    root = Path(base_dir)

    def rp(f: str) -> str:
        q = Path(os.path.expanduser(f))
        return os.path.realpath(str(q if q.is_absolute() else root / q))

    names = list(manifest["answer_keys"])
    doc, _, _ = load_answer_keys([rp(manifest["answer_keys"][n]) for n in names], names)
    ctrl = {i["id"] for i in doc["controls"]}
    cat = {i: "controls" for i in ctrl}
    cat.update({i: "partial" for i in partial_ids(doc)})
    nulls = nulls or {}
    for k, v in nulls.items():
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
            raise SystemExit(f"judge-pairs: --null value for {k!r} must be a number >= 0")
    inputs: Dict[str, str] = {}
    cache: Dict[str, Dict[str, Any]] = {}
    seen_pairs: set = set()
    ref_set: List[Optional[List[str]]] = [None]

    def load(f: str, p: V2Path, key: str) -> Dict[str, str]:
        f = rp(f)
        if (f, key) in seen_pairs:     # 같은 (파일, 경로 키)를 두 번 세면 다수결·짝이 거짓으로 바뀐다(judge-arms 와 같은 규칙)
            raise SystemExit(f"judge-pairs: {display_path(f)} is listed more than once under path {key!r} in the manifest")
        seen_pairs.add((f, key))
        if f not in cache:
            with open(f, encoding="utf-8") as fh:
                cache[f] = json.load(fh)
            inputs[f] = _sha256_file(f)
            sset = sorted(cache[f].get("set") or [])
            if ref_set[0] is None:
                ref_set[0] = sset
            elif sset != ref_set[0]:   # 항목 집합이 다르면 교집합만 짝지어 빠진 항목이 조용히 사라진다
                raise SystemExit(f"judge-pairs: {display_path(f)} scored a different item set than the other summaries")
        v2 = cache[f].get("v2")
        if not isinstance(v2, dict) or p.name not in (v2.get("paths") or {}):
            raise SystemExit(f"judge-pairs: {display_path(f)} has no v2 {p.name} verdicts (rescore --judge v2 first)")
        return v2["paths"][p.name]

    def repeat_states(arm: Dict[str, Any]) -> List[Dict[str, Dict[str, Any]]]:
        out = []
        for r in arm.get("repeats") or []:
            if r.get("state") != "completed":
                continue
            st = {}
            for key, files in r.items():
                p = pair_path_of(key) if key != "state" else None
                if p is None or files is None:
                    continue
                files = [files] if isinstance(files, str) else list(files)
                st[key] = pair_repeat_state(p, [load(f, p, key) for f in files], ctrl)
            out.append(st)
        return out

    states = {n: repeat_states(a) for n, a in manifest["arms"].items()}
    jobs = [(n, a.get("baseline")) for n, a in manifest["arms"].items() if a.get("role") != "baseline"]
    rpo = manifest.get("read_path_only")
    if rpo:
        if rpo["arm"] == rpo["baseline"]:
            raise SystemExit(f"judge-pairs: read_path_only arm and baseline are both {rpo['arm']!r}")
        jobs.append((rpo["arm"], rpo["baseline"]))
    out: Dict[str, Any] = {"judge": "v2", "pairs": {}, "null": dict(nulls), "created": now_iso()}
    for n, bname in jobs:
        if manifest["arms"].get(bname, {}).get("role") != "baseline":
            raise SystemExit(f"judge-pairs: arm {n} baseline {bname!r} is not a baseline arm in the manifest")
        bst, tst = states[bname], states[n]
        # 경로가 한 반복에라도 없으면 빼지 않고 missing(판정 불가)으로 싣는다(judge-arms 의 missing 과 같다)
        keys = list(dict.fromkeys(k for st in tst + bst for k in st))
        row: Dict[str, Any] = {"baseline": bname, "baseline_repeats": len(bst), "repeats": len(tst), "paths": {}}
        for key in keys:
            p = pair_path_of(key)
            if bst and all(key in b for b in bst):
                base_items = pair_baseline([b[key]["items"] for b in bst])
                bund = next((b[key]["undecidable"] for b in bst if b[key]["undecidable"]), None)
            else:
                base_items, bund = {}, "missing"
            reps = []
            for st in tst:
                if key not in st:
                    g = pair_gain(p, pair_transitions({}, {}, cat), nulls.get(key))
                    g.update(gain=None, effect=None, status="missing")
                    reps.append({"transitions": pair_transitions({}, {}, cat), "used": 0, "runs": 0, **g})
                    continue
                tr = pair_transitions(base_items, st[key]["items"], cat)
                g = pair_gain(p, tr, nulls.get(key))
                und = bund and ("baseline-" + bund) or st[key]["undecidable"]
                if und:
                    g.update(gain=None, effect=None, status=und)
                reps.append({"transitions": tr, "used": st[key]["used"], "runs": st[key]["runs"], **g})
            effs = [r["effect"] for r in reps]
            row["paths"][key] = {
                "path": p.name, "kind": p.kind, "repeats": reps,
                "baseline_states": {s: sum(1 for v in base_items.values() if v == s)
                                    for s in ("open", "not-open", "unstable", "invalid")},
                "effect": (None if any(e is None for e in effs) or not effs else (len(effs) >= 2 and all(effs)))}
        out["pairs"][n] = row
    out["inputs"] = inputs
    return out


def render_pairs(res: Dict[str, Any]) -> str:
    """judge-pairs 출력: arm 마다 경로 x 처치 반복 표(경로 등록표 순서). id 없이 수만 쓴다."""
    order = {p.short: k for k, p in enumerate(V2_REGISTRY.values())}
    lines: List[str] = []
    for n, row in res["pairs"].items():
        lines += [f"## {n} vs {row['baseline']} (기준선 반복 {row['baseline_repeats']}, 처치 반복 {row['repeats']})", "",
                  "| 경로 | 반복 | 끝난 open->done | open->unknown | not-open->open | 불안정 | invalid | 대조군 손실 | "
                  "대조군 not-open->open | partial open->done/unknown | 이득 | 영분포 | 효과 |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for key in sorted(row["paths"], key=lambda k: (order.get(pair_path_of(k).short, 99), k)):
            x = row["paths"][key]
            for k, r in enumerate(x["repeats"], 1):
                c, ct, pa = r["transitions"]["closed"], r["transitions"]["controls"], r["transitions"]["partial"]
                gain = _f(r["gain"]) if r["status"] == "ok" else f"{_f(r['gain'])} ({r['status']}, 합 {r['raw']})"
                lines.append(f"| {key} | {k} | {c['open_to_done']} | {c['open_to_unknown']} | {c['notopen_to_open']} | "
                             f"{c['unstable'] + ct['unstable'] + pa['unstable']} | {c['invalid'] + ct['invalid'] + pa['invalid']} | "
                             f"{r['control_loss']} | {ct['notopen_to_open']} | {pa['open_to_done']}/{pa['open_to_unknown']} | "
                             f"{gain} | {_f(r['null']) if r['null'] is not None else '영분포 없음'} | {_f(r['effect'])} |")
        lines.append("")
    lines.append("입력 summary sha256:")
    lines += [f"- {h}  {display_path(f)}" for f, h in sorted(res["inputs"].items())]
    return "\n".join(lines) + "\n"


def cmd_judge_pairs(args, out: Path) -> int:
    with open(args.manifest, encoding="utf-8") as fh:
        manifest = json.load(fh)
    nulls = None
    if args.null:
        with open(args.null, encoding="utf-8") as fh:
            nulls = json.load(fh)
        if not isinstance(nulls, dict):
            raise SystemExit("judge-pairs: --null must be a JSON object of manifest path key -> number")
    res = judge_pairs(manifest, Path(args.manifest).resolve().parent, nulls)
    res["manifest"] = {"path": display_path(args.manifest), "sha256": _sha256_file(args.manifest)}
    res["inputs"] = {display_path(f): h for f, h in res["inputs"].items()}
    run = ensure_out_dir(str(out / ("pairs-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S"))))
    _write(run / "pairs.json", res)
    sys.stdout.write(render_pairs(res))
    print(run / "pairs.json")
    return 0


def cmd_judge_arms(args, out: Path) -> int:
    with open(args.manifest, encoding="utf-8") as fh:
        manifest = json.load(fh)
    res = judge_arms(manifest, Path(args.manifest).resolve().parent)
    res["manifest"] = {"path": display_path(args.manifest), "sha256": _sha256_file(args.manifest)}
    text = render_judgement(res)
    res["inputs"] = {display_path(f): h for f, h in res["inputs"].items()}   # judgement.json 에도 홈 절대 경로를 남기지 않는다
    run = ensure_out_dir(str(out / ("judge-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S"))))
    _write(run / "judgement.json", res)
    sys.stdout.write(text)
    print(run / "judgement.json")
    return 0


# ---------------------------------------------------------------- 시험지 v2(세트 C) 선정 (issue #341, 런 3 R3-2)
# 입력은 후보 정답표(세트 A·B 스키마 + `strata`), 후보 evidence(evidence --controls), 선정 run summary(score --judge v2) 뿐이다.
# 정답표(세트 A·B)·진단 라벨은 입력이 아니다. 질의 0회. 규칙 정본은 artifacts/arch-v2/r3-2-setc.md 1절.

SELECT_FIELDS = ("candidates", "evidence", "selected", "kept")
SELECT_CONTROL_TARGET = 10      # 대조군 보충(source=issue-open)은 감도 필터를 통과한 대조군이 이 수에 닿으면 멈춘다(규칙 1.2-4)


def _write_private(path: str, obj: Any) -> None:
    """0600 으로 새로 쓴다. 이미 있으면 덮지 않는다(고정 뒤 sha256 이 바뀌지 않게)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")


def select_setc(doc: Dict[str, Any], evidence: Dict[str, Any], selection: List[Dict[str, Any]]) -> Dict[str, Any]:
    """세트 C 선정 규칙(결정적):
    - 끝난 항목 후보: evidence 완료 근거(present)가 있어야 한다. 원래 증상(source == symptom) 후보는 그것만으로 넣고,
      나머지는 선정 run 의 어느 v2 경로에서든 open 이어야 넣는다(기준선 양성 후보를 모으는 것. 측정은 다른 사본에서 한다).
    - 키 있음:없음 1:1: 두 층에서 위를 통과한 수의 작은 쪽 m 개씩, 후보 목록 순서(원래 증상 먼저, 그다음 표본 순서) 앞에서부터.
    - 대조군 후보: 열림 근거(present)가 있고 선정 run 의 어느 경로에서든 open 이어야 넣는다(기준선에서 어느 경로에도 안 나오는
      대조군은 "대조군 전부 open" 합격 조건을 처치가 결코 채울 수 없게 만든다. 원래 증상 대조군도 같다). partial 후보: 완료 근거만 본다.
    돌려주는 것: 세트 C 정답표(항목 모양은 후보 그대로), 고정 세트(evidence 행), 키 표, 수 보고."""
    strata = doc.get("strata")
    if not isinstance(strata, dict):
        raise SystemExit("select: candidate key needs strata {id: {key, source}}")
    items = all_items(doc)
    for i in items:
        st = strata.get(i["id"])
        if not isinstance(st, dict) or st.get("key") not in (True, False) or not st.get("source"):
            raise SystemExit(f"select: strata has no key true/false and source for {i['id']}")
    ev = lambda iid: (evidence.get(iid) or {}).get("status") == "present"
    opened: Dict[str, bool] = {}
    for s in selection:
        for vs in ((s.get("v2") or {}).get("paths") or {}).values():
            for iid, v in vs.items():
                opened[iid] = opened.get(iid, False) or v == "open"
    if not selection:
        raise SystemExit("select: give the selection run summaries (--selection)")
    unscored = [i["id"] for i in doc["closed"] + doc["controls"] if ev(i["id"]) and i["id"] not in opened
                and not (strata[i["id"]]["source"] == "symptom" and i in doc["closed"])]
    if unscored:
        raise SystemExit(f"select: {unscored} have evidence but no v2 verdict in the selection run")
    excluded: List[Dict[str, Any]] = []
    rep: Dict[str, Any] = {"closed": {}, "controls": {}, "partial": {}}
    picked: Dict[bool, List[Dict[str, Any]]] = {True: [], False: []}
    for k in (True, False):
        row = dict.fromkeys(SELECT_FIELDS, 0)
        for i in doc["closed"]:
            if strata[i["id"]]["key"] is not k:
                continue
            row["candidates"] += 1
            if not ev(i["id"]):
                excluded.append({"id": i["id"], "item": i.get("item"), "reason": "no-evidence"})
                continue
            row["evidence"] += 1
            if strata[i["id"]]["source"] != "symptom" and not opened[i["id"]]:
                excluded.append({"id": i["id"], "item": i.get("item"), "reason": "selection-not-open"})
                continue
            row["selected"] += 1
            picked[k].append(i)
        rep["closed"][f"key:{KEY_NAME[k]}"] = row
    m = min(len(picked[True]), len(picked[False]))
    for k in (True, False):
        rep["closed"][f"key:{KEY_NAME[k]}"]["kept"] = m
        excluded += [{"id": i["id"], "item": i.get("item"), "reason": "stratum-quota"} for i in picked[k][m:]]
    keep_closed = {i["id"] for k in (True, False) for i in picked[k][:m]}
    stopped, passing = set(), 0     # 후보 순서대로: 통과 대조군이 목표에 닿은 뒤의 보충 후보는 뺀다(supplement-stop)
    for i in doc["controls"]:
        if strata[i["id"]]["source"] == "issue-open" and passing >= SELECT_CONTROL_TARGET:
            stopped.add(i["id"])
        elif ev(i["id"]) and opened.get(i["id"]):
            passing += 1
    kept: Dict[str, List[Dict[str, Any]]] = {"closed": [i for i in doc["closed"] if i["id"] in keep_closed]}
    for kind, reason in (("controls", "no-open-evidence"), ("partial", "no-evidence")):
        kept[kind] = []
        for k in (True, False):
            row = dict.fromkeys(SELECT_FIELDS, 0)
            for i in doc.get(kind) or []:
                if strata[i["id"]]["key"] is not k:
                    continue
                if i["id"] in stopped:
                    excluded.append({"id": i["id"], "item": i.get("item"), "reason": "supplement-stop"})
                    continue
                row["candidates"] += 1
                if not ev(i["id"]):
                    excluded.append({"id": i["id"], "item": i.get("item"), "reason": reason})
                    continue
                row["evidence"] += 1
                if kind == "controls" and not opened[i["id"]]:
                    excluded.append({"id": i["id"], "item": i.get("item"), "reason": "selection-not-open"})
                    continue
                row["selected"] += 1
                row["kept"] += 1            # 대조군·partial 은 층 할당이 없다
                kept[kind].append(i)
            rep[kind][f"key:{KEY_NAME[k]}"] = row
    key = {k: v for k, v in doc.items() if k != "strata"}
    key.update(kept)
    key["excluded"] = list(doc.get("excluded") or []) + excluded
    ids = [i["id"] for i in all_items(key)]
    fixed = {"items": {iid: evidence[iid] for iid in ids if iid in evidence}}
    split = {"C": {iid: strata[iid]["key"] for iid in ids}}
    rep["excluded"] = {r: sorted(e["id"] for e in excluded if e["reason"] == r)
                       for r in sorted({e["reason"] for e in excluded})}
    return {"key": key, "fixed": fixed, "split": split, "report": rep}


def cmd_select(args, out: Path) -> int:
    with open(args.answer_key[0], encoding="utf-8") as fh:
        doc = json.load(fh)
    load_answer_key(args.answer_key[0])                       # 스키마 검사
    sel = []
    for f in args.selection:
        with open(f, encoding="utf-8") as fh:
            sel.append(json.load(fh))
    res = select_setc(doc, load_evidence(args.evidence), sel)
    for path in (args.out_key, args.out_fixed, args.out_split):
        if os.path.exists(path):
            raise Refused(f"{display_path(path)} exists: set C files are fixed once written")
    inputs = {display_path(f): _sha256_file(f) for f in [args.answer_key[0]] + list(args.evidence) + list(args.selection)}
    res["key"]["selection"] = {"rule": "artifacts/arch-v2/r3-2-setc.md", "inputs": inputs}
    res["fixed"]["created"] = now_iso()
    _write_private(args.out_key, res["key"])
    _write_private(args.out_fixed, res["fixed"])
    _write_private(args.out_split, res["split"])
    run = ensure_out_dir(str(out / ("select-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S"))))
    report = dict(res["report"], inputs=inputs,
                  outputs={display_path(p): _sha256_file(p) for p in (args.out_key, args.out_fixed, args.out_split)})
    _write(run / "report.json", report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


def main(argv: Optional[List[str]] = None, env: Optional[Dict[str, str]] = None) -> int:
    env = dict(os.environ if env is None else env)
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=("evidence", "score", "rescore", "stats", "render", "judge-arms", "judge-pairs",
                                           "select"))
    ap.add_argument("render_kind", nargs="?", choices=("evidence", "dryrun"))
    ap.add_argument("--answer-key", action="append",
                    help="정답표 JSON(운영 체크아웃 bench/.data/regression-arch-v2.json, 읽기 전용). 세트마다 한 번씩(합친 세트)")
    ap.add_argument("--set-name", action="append", help="--answer-key 순서대로 세트 이름(기본 A, B, ...)")
    ap.add_argument("--key-split", help="키 유무 표 JSON {\"<세트>\": {\"<id>\": true|false|null}} - summary 에 키별 집계")
    ap.add_argument("--evidence", action="append",
                    help="score/render/rescore: evidence 요약 JSON(evidence 출력의 summary.json, 고정 세트). 세트마다 한 번씩")
    ap.add_argument("--manifest", help="judge-arms: arm·반복별 summary 경로 manifest JSON (bench/README.md)")
    ap.add_argument("--judge", choices=("v1", "v2"), default="v1",
                    help="score/rescore: v2 면 summary 에 판정 v2 블록을 더한다(v1 블록 불변). judge-pairs 는 v2 만")
    ap.add_argument("--null", help="judge-pairs: 경로별 영분포 JSON {\"<manifest 경로 키>\": 수} (없으면 목록 경로 이득은 '영분포 없음')")
    ap.add_argument("--share-pages", action="store_true",
                    help="(b) 생성 모드: 같은 이름 페이지가 있고 source_query 가 같으면 만들지 않고 읽는다(다르면 exit 1)")
    ap.add_argument("--detail", help="rescore: score 출력의 detail.json")
    ap.add_argument("--inject-evidence",
                    help="rescore: 원인 층 분리 진단(issue #342) - {\"<id>\": [기억 행]} 을 (c)(d) 결과 끝에 더해 다시 판정(판정 입력 아님)")
    ap.add_argument("--score", help="render dryrun: score 출력의 summary.json (metrics.json 은 같은 디렉터리)")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--bank", help="기본: HC_NEXT_REQUIRED_BANK, 없으면 .env.host.local HC_PERSONAL_BANK")
    ap.add_argument("--owner", help="게이트웨이 토큰 identity (기본: .env.host.local HC_OWNER)")
    ap.add_argument("--paths", default="abc", help="score 할 경로 글자(a, b, c, d). d 는 정답표 list_query 가 있어야 한다")
    ap.add_argument("--controls", action="store_true",
                    help="evidence: 대조군 열림 근거와 partial 완료 근거도 본다(세트 C 선정, issue #341)")
    ap.add_argument("--selection", action="append", default=[], help="select: 선정 run 의 score summary.json(v2 블록). 경로마다")
    ap.add_argument("--out-key", help="select: 세트 C 정답표(0600, 이미 있으면 거부)")
    ap.add_argument("--out-fixed", help="select: 세트 C 고정 세트(0600)")
    ap.add_argument("--out-split", help="select: 세트 C 키 유무 표(0600)")
    ap.add_argument("--reflect-budget", default="mid")
    ap.add_argument("--create-pages", dest="create_pages", action="store_true", default=None,
                    help="(b) 페이지를 만든다(쓰기). 사본 기본값")
    ap.add_argument("--read-pages", action="store_true",
                    help="(b) 이미 있는 페이지를 읽기만 한다(사본에서도 쓰기 없음). delta 갱신 뒤 채점용")
    ap.add_argument("--page-suffix", default="", help="(b) 페이지 이름 접미사(예: -pre). 생성과 읽기 전용 채점에 함께 쓴다")
    ap.add_argument("--page-map", help="(b) 읽기 전용 채점: 힌트 -> 실제 페이지 이름 JSON(전체 뱅크 페이지는 키 \"-\"). 운영 연막 채점용")
    ap.add_argument("--page-timeout", type=float, default=1800.0)
    ap.add_argument("--wait", type=float, default=0.0, help="stats: pending_consolidation 0 까지 기다릴 초")
    ap.add_argument("--wait-pages", action="store_true",
                    help="stats: 통합 뒤 페이지 delta 갱신까지(stale 페이지 0, 진행 중 refresh_mental_model 0) 기다린다")
    ap.add_argument("--allow-prod", action="store_true")
    ap.add_argument("--read-only", action="store_true")
    argv = list(sys.argv[1:] if argv is None else argv)
    for k in range(len(argv) - 1):          # `--page-suffix -pre`: 값이 `-` 로 시작해도 옵션으로 읽히지 않게 붙인다
        if argv[k] == "--page-suffix":
            if argv[k + 1] not in ap._option_string_actions:     # 값을 빠뜨린 `--page-suffix --read-pages` 는 argparse 오류로 둔다
                argv[k:k + 2] = [f"--page-suffix={argv[k + 1]}"]
            break
    a = ap.parse_args(argv)
    if a.wait_pages and not a.wait:              # 대기 없이 한 번 찍고 exit 0 이면 갱신 전 채점으로 넘어간다
        ap.error("--wait-pages needs --wait <seconds> > 0")

    if a.command == "judge-arms":
        if not a.manifest:
            ap.error("judge-arms needs --manifest <json>")
        return cmd_judge_arms(a, ensure_out_dir(a.out))
    if a.command == "judge-pairs":
        if not a.manifest or a.judge != "v2":
            ap.error("judge-pairs needs --manifest <json> --judge v2")
        return cmd_judge_pairs(a, ensure_out_dir(a.out))
    a.answer_key = a.answer_key or []
    if a.command == "select":
        if len(a.answer_key) != 1 or not a.evidence or not (a.out_key and a.out_fixed and a.out_split):
            ap.error("select needs one --answer-key, --evidence, --selection and --out-key/--out-fixed/--out-split")
        try:
            return cmd_select(a, ensure_out_dir(a.out))
        except Refused as e:
            print(f"arch_v2_regression: refused: {e}", file=sys.stderr)
            return 1
    if a.command != "stats" and not a.answer_key:
        ap.error(f"{a.command} needs --answer-key")
    if a.set_name and len(a.set_name) != len(a.answer_key):
        ap.error("--set-name: give one per --answer-key")
    if len(a.answer_key) > 1 and a.evidence and len(a.evidence) != len(a.answer_key):
        # 한 세트의 고정 세트를 빠뜨리면 그 세트 끝난 항목이 조용히 세트에서 빠진다
        ap.error("--evidence: give one per --answer-key (each set's fixed set)")
    a.new_mode = len(a.answer_key) > 1 or bool(a.key_split)
    if a.key_split:
        with open(a.key_split, encoding="utf-8") as fh:
            a.key_split = json.load(fh)
    if a.command == "render":
        doc, _, _ = load_answer_keys(a.answer_key, a.set_name)
        if a.render_kind == "evidence":
            sys.stdout.write(render_evidence_md(load_evidence(a.evidence), doc))
        else:
            sc = json.load(open(a.score, encoding="utf-8"))
            mp = os.path.join(os.path.dirname(a.score), "metrics.json")
            sys.stdout.write(render_dryrun_md(sc["paths"], json.load(open(mp)) if os.path.exists(mp) else {}))
        return 0
    if a.command == "rescore":
        doc, a.sets, a.set_names = load_answer_keys(a.answer_key, a.set_name)
        return cmd_rescore(a, doc, ensure_out_dir(a.out))
    try:
        target = resolve_target(env, a.allow_prod, a.read_only)
        if a.read_pages and a.create_pages:
            raise Refused("--read-pages and --create-pages: pick one")
        if a.read_pages:
            a.create_pages = False
        if a.create_pages is None:
            a.create_pages = target.writable
        if a.page_map:
            if a.create_pages:
                raise Refused("--page-map names pages to read: use it with --read-pages (or on the read-only production target)")
            if a.page_suffix:
                raise Refused("--page-map and --page-suffix both name the pages: pick one")
            with open(a.page_map, encoding="utf-8") as fh:
                a.page_map = json.load(fh)
            if not isinstance(a.page_map, dict) or not all(isinstance(v, str) for v in a.page_map.values()):
                raise Refused("--page-map must be a JSON object of hint -> page name")
        else:
            a.page_map = None
        if a.create_pages and not target.writable:
            raise Refused("--create-pages is a write path; refused on the production target")
        if a.share_pages and not a.create_pages:
            raise Refused("--share-pages decides whether to create a page: use it in create mode")
        print(target.line(), file=sys.stderr)
        doc, a.sets, a.set_names = load_answer_keys(a.answer_key, a.set_name) if a.command != "stats" else ({}, {}, [])
        if a.command == "score" and not a.evidence:
            raise SystemExit("score needs --evidence <evidence summary.json> (items without bank evidence leave the set)")
        if not a.bank:
            a.bank = resolve_bank(a, env)
        gw, up = build_clients(target, a, env)
        out = ensure_out_dir(a.out)
        return {"evidence": cmd_evidence, "score": cmd_score, "stats": cmd_stats}[a.command](a, doc, target, gw, up, out)
    except Refused as e:
        print(f"arch_v2_regression: refused: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
