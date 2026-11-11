"""런 5 S3 손잡이 튜닝(R5-C, issue #365, 상위 #360 "S4·S3 구현").

  r5_tune.py prep    (공통 인자) [--families <json>] --out <json>                 지출 0: 조합 x t 표·토큰·C4 동일성
  r5_tune.py run     (공통 인자) --combo C1|C2|C3|C4 --rep N [--c4-base C1|C2|C3] --k 3
                     --gen-dir <dir> --spent-dir <dir> --ledger <jsonl> --max-usd 30 --soft-usd 25
  r5_tune.py cands   (판정 인자) --label <gen 라벨> ... --out <json>                새로 생긴 P 오답·R 누락(판독 입력)
  r5_tune.py reasons (판정 인자) --base-reasons <#374 reasons.json> [--new-reads <json>] --out <json>
  r5_tune.py judge   (판정 인자) --reasons <json> --run C1=C1-r1,C1-r2 ... --base base1 ... --out <json> --ledger <jsonl>
  r5_tune.py best    --judge <json> --out <knobs json>
  r5_tune.py outside (판정 인자) --gen-dir <#365 gen> --reasons <#365 reasons.json> --label C3-r1 ... --out <json>
                     지출 0(#381 1단계): P 오답 쌍의 키가 그 t 의 C3 표(열림 행)에 있었는지, 쌍·고유 키 수만
  r5_tune.py table   (판정 인자) --ref-store <S4a 저장소> [--reasons <json>] --out <json>
                     지출 0(#383 3단계): --store 의 C1 표 열림 행을 소비자 없이 그대로 목록으로 본 P·P_bank·R·R_bank
                     (분모 = --store 열림 / --ref-store 열림). 사유 없는 오답 쌍은 수와 판독 근거(리포 밖)만

공통 인자: --labelset --aliases --plan --recalls(R5-A2 rep1) --store(S4 역할 저장소) --t ...(dev 표본 t)
          --c3-header-file(C3 머리 문안, 리포 밖) [--c5-header-file --c6-header-file](#381, 리포 밖)
          [--max-input-chars 200000].
판정 인자: 공통 인자 + --meta --mentions --llm-roles(#372, R_bank #374 분모와 판독 근거 문장) --gen-dir --base-gen-dir.

원문(표 텍스트·프롬프트·d-llm 출력·판독 근거)은 리포 밖 0600 파일에만 쓴다. 표준 출력에는 수치·sha256 만 낸다.

방식(코디네이터 결정, 사본 0). S3 는 저장 역할만으로 결정적이므로 사본 없이 같은 입력을 오프라인으로 만든다.
  * 질의 인자 재구성: R5-A2 recall 은 태그 없이 `{query: 지시문 + 줄바꿈 + 문안, query_timestamp, max_tokens}` 로 보냈다
    (`bench/d_llm.py` `save_recalls`). 여기서도 같은 인자(`query_args`)로 표를 만든다. 태그가 없어 `request_repos` 가 빈
    집합이고 범위는 뱅크 전체다. 그래서 한 t 의 모든 질의가 같은 표를 받는다(같은 표 텍스트는 1회).
  * 표: 질의마다 `gateway/item_open_table` 의 `wants_table` -> `role_states`(기준 시각 = 그 질의 `query_timestamp`) ->
    `request_repos` -> `build_table` 를 그대로 부른다(게이트웨이 코드를 복사하거나 고치지 않는다). 저장소는 `read_store`.
  * d-llm 입력 = R5-A2 합집합(질의 순 -> 순위 순, 기억 id 중복 제거) -> 시간여행 필터 -> 200,000자 상한 렌더 + 표 텍스트를
    질의 순으로(같은 텍스트 1회) 줄바꿈으로 이어 recall 행 뒤에(`r5_metrics.build_prompt_with_table`). 표가 하나도 없으면
    기준선 프롬프트와 바이트가 같다. `claude-sonnet-5-5` K=3, 동결 기준선 프롬프트, `d_llm_match` 대응.
  * 손잡이 공간(고정, 손으로 바꾸지 않는다): C1 a·기본 머리·always, C2 b·기본 머리·always(= b+c), C3 a·소비자 겨냥 머리
    (원문은 리포 밖 파일, sha256 만)·always, C4 = C1~C3 1차 최선의 fmt·header + attach status_query. 모두
    recent_done_days 0, max_rows 300, scope request_project, timeout_ms 기본. C4 는 지출 전에 t별 d-llm 입력을 기준 조합과
    바이트 비교해 모든 t 에서 같으면 돌리지 않는다(원장에 skip 줄).
  * sha 정지: 하네스·판정 코드·라벨셋·역할 저장소·`gateway/item_open_table.py`(`GUARD_FILES` + 입력 파일)의 sha256 을 원장
    첫 줄(start)에 남기고, 반복 시작 전과 d-llm 호출 전마다 다시 계산해 다르면 멈춘다(exit 5).
  * 원장: 리포 밖 append-only JSONL(0600). 기존 줄을 다시 쓰지 않는다(`O_APPEND`).
  * 지출: `r5_roles.SpendGuard`(호출 전마다 `--spent-dir` 누계 재읽기, 상한 `--max-usd`, exit 3). 반복 시작 전 누계가
    `--soft-usd` 를 넘으면 시작하지 않는다(exit 4, 코디네이터 보고 지점).

지표(#372·#374 정의, 역할 출처만 S4 저장소).
  * P·R(/기준선 결과에 나온 대조 쌍)·R_all 은 `r5_metrics.pr_summary`.
  * R_bank(S4) 분모 = 표본 t 대조 쌍 중 S4 저장소 역할로 기준 시각 이하 마지막 비참조 언급이 열림인 쌍. R_bank(#374) 분모 =
    #372 LLM 역할(o3c-llm) 상태가 열림인 쌍. 둘 다 `r5_roles.rbank_split`.
  * P_bank·R_bank2 = `r5_roles.bank_adjusted`. 사유는 #374 `reasons.json` 의 판독(사람 판정)만 재사용하고 사유를 S4 상태로
    다시 분류한다(`classify_reason` 순서, LLM 상태 자리에 S4 상태). 새로 생긴 오답 쌍은 `cands` 출력을 워커 1인이 같은 판독
    규칙으로 읽어 `--new-reads` 로 넣는다.
  * 추가 토큰(텍스트 기준) = 표 텍스트 `cl100k_base` 토큰 / 그 t 의 필터 뒤 d-llm 입력(행 렌더) 토큰, t별 최대.
    payload 기준 = 표 객체(`{t, fmt, rows, truncated, text}`) 압축 JSON 토큰 / 같은 분모(응답 하나에 실리는 양).
  * 짝 비교: base1 대비 키 군집 재표집(씨앗 20261101, 2000회). R_bank(S4) 노이즈 바닥 = base1~5 반복 짝의 같은 재표집.
  * 최선(`select_best`): 두 반복 모두 P_bank >= 95% 이고 추가 토큰(텍스트, 두 반복 최대) <= 10% 인 조합 중 R_bank(S4) 평균
    최대, 같으면 토큰 최소. 없으면 P_bank(두 반복 최솟값) 최대 조합을 `no-eligible` 로 낸다.

R5-C2 머리 문구 중간안(issue #381, 코디네이터 결정 plan-review 20261102-080245).
  * C1 은 #365 기본 머리 문안을 명시 값(`C1_HEADER`, sha256 `82149001...`)으로 고정한다. 게이트웨이 `HEADER_DEFAULT` 가
    바뀌어도 #365 C1 이 재현된다(정규 sha256 `f7fcf626...` 그대로).
  * C5 = C1 머리 첫 줄을 그대로 두고 지시 1문장을 덧붙임(마지막 줄 `줄: {cols}` 유지), C6 = C5 첫 줄 + 지시 1문장. 나머지
    손잡이는 C1 과 같다. 원문은 리포 밖 파일(`--c5-header-file`·`--c6-header-file`, 끝 줄바꿈 제거)이고, 그 텍스트 sha256 이
    `EXPECTED_HEADER_SHA256` 과 다르거나 구조가 위와 다르면 거부한다.
  * `judge --ref-judge <#365 judge.json>`: C1 값은 #365 판정 출력을 참고값으로 쓴다(재측정 없음, t·저장소·knobs sha 대조).
    `select_vs_ref`: 자격(select_best 와 같은 문턱) 조합 중 R_bank(S4) 평균이 참고 조합보다 엄격히 높은 것의 최대, 없거나
    동률이면 참고 조합(C1) 유지. `select_best` 자체는 바꾸지 않는다.
  * `outside`(1단계, 지출 0): C3 저장 gen 의 P 오답 쌍마다 그 키가 그 t 의 C3 표 열림 행에 있었는지 센다(쌍·고유 키,
    전체와 P_bank 분모에 남는 (i)·(iv) 따로, 사유별). 출력은 수만, 키는 리포 밖 파일에도 키 코드로만.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from bench import r5_roles as X  # noqa: E402
from gateway import item_open_table as OT  # noqa: E402
from gateway import item_status  # noqa: E402
from gateway import recall_prune  # noqa: E402

SEED = 20261101
DEV_LAST_T = X.DEV_LAST_T
P_BANK_MIN = 0.95
TOKEN_MAX = 0.10
STORE_TIMEOUT_S = 60.0
COMBOS = ("C1", "C2", "C3", "C4", "C5", "C6")
HEADER_COMBOS = ("C5", "C6")
# #365 의 기본 머리(= 그때 `HEADER_DEFAULT`). C1 은 이 값으로 고정한다(#381).
C1_HEADER = ("기준일 {t} 현재 뱅크 기억 기준 열린 작업 키다(기준일 이전 기억만). "
             "여기 없는 키는 뱅크 기억 기준 열림이 아니거나 질의 범위 밖이다.\n줄: {cols}")
# C5·C6 머리 텍스트(끝 줄바꿈 제거)의 기대 sha256. 원문은 리포 밖(#381 산출물에는 sha256 만).
EXPECTED_HEADER_SHA256 = {"C5": "9a3f9342ab6dd602068ece7a0c3c4fc33080bc0b566d2ca1f81b2587d4c45a09",
                          "C6": "a950bab3820a329d966e3f18923546f339c88cda34c9292bdd1ea8017b9ab0f1"}
COLS_LINE = "줄: {cols}"
FIXED = {"recent_done_days": 0, "max_rows": 300, "scope": "request_project"}
GUARD_FILES = ("bench/r5_tune.py", "bench/r5_metrics.py", "bench/r5_labels.py", "bench/r5_oracle.py",
               "bench/r5_roles.py", "bench/d_llm.py", "bench/d_llm_match.py", "bench/prompts/d_llm_baseline.txt",
               "gateway/item_open_table.py")


class Fail(Exception):
    pass


class ShaMismatch(Fail):
    pass


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- 손잡이 ---------------------------------------------------------------------------------------------

def combo_raw(combo: str, c3_header: Optional[str], c4_base: Optional[Mapping[str, Any]] = None,
              headers: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """조합의 손잡이 원문(JSON 객체). 고정 손잡이 공간 밖 값은 만들지 않는다."""
    if combo == "C1":
        raw = {"table_fmt": "a", "header": C1_HEADER, "attach": "always"}
    elif combo in HEADER_COMBOS:
        text = (headers or {}).get(combo)
        if not text:
            raise Fail(f"{combo} needs --{combo.lower()}-header-file")
        raw = {"table_fmt": "a", "header": text, "attach": "always"}
    elif combo == "C2":
        raw = {"table_fmt": "b", "attach": "always"}
    elif combo == "C3":
        if not c3_header:
            raise Fail("C3 needs --c3-header-file")
        raw = {"table_fmt": "a", "header": c3_header, "attach": "always"}
    elif combo == "C4":
        if not c4_base:
            raise Fail("C4 needs the first-pass best combo (--c4-base)")
        raw = {"table_fmt": c4_base["table_fmt"], "attach": "status_query"}
        if c4_base.get("header") is not None:
            raw["header"] = c4_base["header"]
    else:
        raise Fail(f"unknown combo {combo}")
    return dict(raw, **FIXED)


def check_header(combo: str, text: str, c5_text: Optional[str] = None) -> None:
    """C5·C6 문안 구조(코디네이터 결정): 두 줄, 마지막 줄 `줄: {cols}`, 첫 줄 = C1 첫 줄(C6 은 C5 첫 줄) + 공백 + 지시."""
    lines = text.split("\n")
    base = (c5_text if combo == "C6" and c5_text else C1_HEADER).split("\n")[0]
    ok = (len(lines) == 2 and lines[1] == COLS_LINE and lines[0].startswith("기준일 {t}")
          and lines[0].startswith(base + " ") and len(lines[0]) > len(base) + 1)
    # 덧붙인 문장 수: C5 = C1 + 1문장, C6 = C5 + 1문장(C5 없이 보면 C1 + 2문장). 문장 = '.' 로 끝나는 마디
    want = 2 if combo == "C6" and not c5_text else 1
    added = lines[0][len(base):].strip() if ok else ""
    ok = ok and added.endswith(".") and added.count(".") == want
    if not ok:
        raise Fail(f"{combo} header structure differs from the decided form")


def read_header(path: str, combo: str) -> str:
    """리포 밖 문안 파일(끝 줄바꿈 제거). 텍스트 sha256 이 기대 상수와 다르면 거부."""
    with open(path, encoding="utf-8") as f:
        text = f.read().rstrip("\n")
    if sha256_text(text) != EXPECTED_HEADER_SHA256[combo]:
        raise Fail(f"{combo} header sha256 differs from the expected constant")
    return text


def knobs_of(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """게이트웨이와 같은 검증(`parse_knobs`). 경고가 있으면 거부(조용히 기본값으로 떨어진 조합을 재지 않는다)."""
    values, warnings = OT.parse_knobs(json.dumps(raw, ensure_ascii=False))
    if warnings:
        raise Fail(f"knobs warnings {warnings}")
    return values


def knobs_sha256(values: Mapping[str, Any]) -> str:
    """게이트웨이 감사 줄 `knobs_sha256` 과 같은 값(`Knobs.sha256`)."""
    return OT.Knobs(dict(values), "file").sha256


# --- 질의 인자와 표 -----------------------------------------------------------------------------------

def query_args(spec: Mapping[str, Any], q: Mapping[str, Any], max_tokens: int) -> Dict[str, Any]:
    """R5-A2 `save_recalls` 가 게이트웨이 recall 에 보낸 인자 그대로(태그 없음)."""
    return {"query": f"{spec['instruction'].strip()}\n{q['text'].strip()}", "query_timestamp": spec["query_timestamp"],
            "max_tokens": max_tokens}


def check_recall_doc(doc: Mapping[str, Any], spec: Mapping[str, Any], plan_sha: str, max_tokens: int) -> None:
    """저장 recall 이 이 계획의 이 t 질의 순서·기준 시각으로 저장된 것인지(아니면 재구성 인자가 실제와 다르다)."""
    if doc.get("plan_sha256") != plan_sha:
        raise Fail("recall was saved from another plan")
    if doc.get("query_timestamp") != spec["query_timestamp"] or doc.get("max_tokens") != max_tokens:
        raise Fail("recall query_timestamp/max_tokens differ from the plan")
    want = [q.get("qid") for q in spec["queries"]]
    got = [r.get("qid") for r in sorted(doc.get("runs") or [], key=lambda r: r.get("q", 0))]
    if want != got:
        raise Fail("recall query order differs from the plan")


def t_tables(mentions: Sequence[OT.Mention], spec: Mapping[str, Any], max_tokens: int, knobs: Mapping[str, Any],
             aliases: Mapping[str, str]) -> Dict[str, Any]:
    """한 t 의 질의마다 게이트웨이 공개 함수로 표를 만들고 같은 표 텍스트는 1회만(질의 순)."""
    texts: List[str] = []
    objs: List[Dict[str, Any]] = []
    attached = 0
    for q in spec["queries"]:
        args = query_args(spec, q, max_tokens)
        if not OT.wants_table(args, knobs):
            continue
        cutoff = item_status._instant(args["query_timestamp"])
        if cutoff is None:
            raise Fail("unreadable query_timestamp")
        attached += 1
        repos = OT.request_repos(args.get("tags"), aliases) if knobs["scope"] == "request_project" else set()
        table = OT.build_table(OT.role_states(mentions, cutoff), cutoff, repos or None, knobs)
        if table["text"] in texts:
            continue
        texts.append(table["text"])
        objs.append(table)
    return {"queries": len(spec["queries"]), "attached_queries": attached, "texts": texts, "objs": objs,
            "text": "\n".join(texts)}


def payload_json(obj: Mapping[str, Any]) -> str:
    """응답에 실리는 표 객체의 압축 JSON(`recall_prune._dumps`, 게이트웨이 재직렬화와 같은 함수)."""
    return recall_prune._dumps(dict(obj))


# --- 입력 컨텍스트 -------------------------------------------------------------------------------------

class Ctx:
    """공통 입력. 봉인·간격 t 는 `r5_metrics.check_not_sealed` 와 dev 상한으로 거부한다."""

    def __init__(self, args):
        from bench import d_llm as D
        from bench import r5_metrics as R
        self.D, self.R = D, R
        self.args = args
        self.ts = sorted(args.t)
        self.labelset = X.read_jsonl(args.labelset)
        R.check_not_sealed(self.labelset, self.ts)
        if any(X._day(t) > DEV_LAST_T for t in self.ts):
            raise Fail("t beyond the dev upper bound")
        self.aliases = R.aliases_from(args)
        self.plan = X.read_json(args.plan)
        self.plan_sha = X.sha256_file(args.plan)
        self.max_tokens = self.plan.get("max_tokens", R.MAX_TOKENS)
        self.docs = R.load_recalls(args.recalls, self.ts)
        for t in self.ts:
            check_recall_doc(self.docs[t], self.plan["per_t"][t], self.plan_sha, self.max_tokens)
        self.store = OT.read_store(args.store, STORE_TIMEOUT_S)
        self.store_sha = X.sha256_file(args.store)
        # 저장소 키는 meta aliases_sha256 의 별칭 표로 정규화됐다. 다른 표로 라벨 키를 만들면 쌍이 조용히 R_bank 분모에서 빠진다.
        if self.store.meta.get("aliases_sha256") != OT.aliases_sha256(self.aliases):
            raise Fail("alias table differs from the store meta aliases_sha256")
        self.c3_header = None
        if getattr(args, "c3_header_file", None):
            with open(args.c3_header_file, encoding="utf-8") as f:
                self.c3_header = f.read().rstrip("\n")
        self.headers: Dict[str, str] = {}
        for combo in HEADER_COMBOS:
            path = getattr(args, f"{combo.lower()}_header_file", None)
            if path:
                self.headers[combo] = read_header(path, combo)
        for combo, text in self.headers.items():
            check_header(combo, text, self.headers.get("C5"))
        self.template = D.load_prompt(D.BASELINE_PROMPT)
        self.counter = D.tiktoken_counter()
        if self.counter is None:
            raise Fail("tiktoken is required (uv run --with tiktoken)")
        self._rows: Dict[str, Any] = {}

    def guard_paths(self) -> Dict[str, str]:
        files = {f: os.path.join(REPO, f) for f in GUARD_FILES}
        files.update({"labelset": self.args.labelset, "store": self.args.store, "plan": self.args.plan,
                      "aliases": self.args.aliases})
        for t in self.ts:
            files[f"recall:{t}"] = self.R.recall_path(self.args.recalls, t)
        if getattr(self.args, "c3_header_file", None):
            files["c3_header"] = self.args.c3_header_file
        for combo in HEADER_COMBOS:
            path = getattr(self.args, f"{combo.lower()}_header_file", None)
            if path:
                files[f"{combo.lower()}_header"] = path
        return files

    def rows(self, t: str):
        if t not in self._rows:
            rows, st = self.R.t_input(self.docs[t], t)
            asm = self.D.assemble({"runs": [{"q": 1, "max_tokens": 0, "results": rows}]},
                                  max_input_chars=self.args.max_input_chars)
            text = "\n".join(self.D.render_row(r) for r in asm["rows"])
            self._rows[t] = (asm, st, self.counter(text))
        return self._rows[t]

    def knobs(self, combo: str, c4_base: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        return knobs_of(combo_raw(combo, self.c3_header, c4_base, self.headers))

    def build(self, t: str, knobs: Mapping[str, Any]) -> Dict[str, Any]:
        """t 의 d-llm 프롬프트와 표 통계."""
        asm, st, in_tok = self.rows(t)
        tab = t_tables(self.store.mentions, self.plan["per_t"][t], self.max_tokens, knobs, self.aliases)
        prompt = self.R.build_prompt_with_table(self.D, self.template, asm, t, tab["text"])
        ttok = self.counter(tab["text"]) if tab["text"] else 0
        ptok = max((self.counter(payload_json(o)) for o in tab["objs"]), default=0)
        rows = [r for o in tab["objs"] for r in o["rows"]]
        stats = {"queries": tab["queries"], "attached_queries": tab["attached_queries"], "tables": len(tab["objs"]),
                 "rows": len(rows), "state": dict(Counter(r[1] for r in rows)),
                 "truncated": sum(o["truncated"] for o in tab["objs"]), "chars": len(tab["text"]),
                 "tokens": ttok, "payload_tokens": ptok, "input_tokens": in_tok,
                 "ratio": ttok / in_tok, "payload_ratio": ptok / in_tok,
                 "table_sha256": sha256_text(tab["text"])}
        return {"prompt": prompt, "asm": asm, "filter": st, "stats": stats, "open_keys": sorted(
            r[0] for o in tab["objs"] for r in o["rows"] if r[1] == OT.STATE_OPEN)}

    def s4_states(self, t: str) -> Dict[str, OT.KeyState]:
        cutoff = item_status._instant(self.plan["per_t"][t]["query_timestamp"])
        return OT.role_states(self.store.mentions, cutoff)


def same_inputs(ctx: Ctx, ka: Mapping[str, Any], kb: Mapping[str, Any]) -> Dict[str, bool]:
    """t 마다 두 손잡이의 d-llm 입력(프롬프트) 바이트가 같은가."""
    return {t: ctx.build(t, ka)["prompt"].encode("utf-8") == ctx.build(t, kb)["prompt"].encode("utf-8")
            for t in ctx.ts}


# --- 원장·sha 정지 ------------------------------------------------------------------------------------

def sha_map(paths: Mapping[str, str]) -> Dict[str, str]:
    return {name: X.sha256_file(p) for name, p in sorted(paths.items())}


def ledger_lines(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def ledger_append(path: str, rec: Mapping[str, Any]) -> None:
    """append-only(0600). 기존 바이트를 다시 쓰지 않는다."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o700, exist_ok=True)
    line = json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


class ShaGuard:
    """원장 첫 start 줄의 sha 를 시작값으로 두고, check() 마다 다시 계산해 다르면 ShaMismatch."""

    def __init__(self, paths: Mapping[str, str], ledger: str, now: Callable[[], str]):
        self.paths = dict(paths)
        start = next((r for r in ledger_lines(ledger) if r.get("event") == "start"), None)
        current = sha_map(self.paths)
        if start is None:
            ledger_append(ledger, {"event": "start", "sha256": current, "at_kst": now()})
            self.start = current
        else:
            self.start = start["sha256"]
        self.check()

    def check(self) -> None:
        now = sha_map(self.paths)
        # 핵심 파일은 늘 대조한다. 인자에 따라 생기는 키(c3_header, t별 recall)는 양쪽에 다 있을 때만 대조한다
        # (첫 run 의 선택 인자가 기준 키 집합을 정해 파일 변경 없이 멈추는 거짓 양성을 막는다).
        core = set(GUARD_FILES) | {"labelset", "store", "plan", "aliases"}
        keys = core | (set(now) & set(self.start))
        diff = sorted(k for k in keys if now.get(k) != self.start.get(k))
        if diff:
            raise ShaMismatch(f"stop: sha256 differs from the start value: {','.join(diff)}")


# --- 실행 ---------------------------------------------------------------------------------------------

def label_of(combo: str, rep: int) -> str:
    return f"{combo}-r{rep}"


def run_rep(ctx: Ctx, combo: str, rep: int, *, gen_dir: str, spent_dir: str, ledger: str, max_usd: float,
            soft_usd: float, k: int, c4_base: Optional[str] = None,
            caller: Optional[Callable[[str], Dict[str, Any]]] = None,
            generate: Optional[Callable] = None, now: Optional[Callable[[], str]] = None) -> int:
    """조합 x 반복 하나. 0 = 끝(또는 C4 동일 입력 생략), 3 = 지출 상한, 4 = soft 상한, 5 = sha 정지, 1 = 거부."""
    D, R = ctx.D, ctx.R
    now = now or D.now_kst
    generate = generate or D.generate_one
    try:
        guard_sha = ShaGuard(ctx.guard_paths(), ledger, now)
    except ShaMismatch as e:
        print(str(e), file=sys.stderr)
        ledger_append(ledger, {"event": "sha-stop", "combo": combo, "rep": rep, "at_kst": now(), "detail": str(e)})
        return 5
    if combo == "C4" and c4_base not in ("C1", "C2", "C3"):
        raise Fail("C4 needs --c4-base C1|C2|C3")
    base_knobs = ctx.knobs(c4_base) if combo == "C4" else None
    knobs = ctx.knobs(combo, base_knobs)
    ksha = knobs_sha256(knobs)
    label = label_of(combo, rep)
    if combo == "C4":
        same = same_inputs(ctx, knobs, base_knobs)
        if all(same.values()):
            ledger_append(ledger, {"event": "skip", "combo": combo, "rep": rep, "c4_base": c4_base,
                                   "reason": "identical-input", "knobs_sha256": ksha, "same_by_t": same,
                                   "at_kst": now()})
            print(json.dumps({"combo": combo, "skip": "identical-input", "c4_base": c4_base, "same_by_t": same}))
            return 0
    guard = X.SpendGuard(spent_dir, max_usd, gen_dir)
    spent = guard.total()
    if spent > soft_usd:
        print(f"stop: spent {spent:.3f} > soft-usd {soft_usd}; {label} not started", file=sys.stderr)
        ledger_append(ledger, {"event": "soft-stop", "combo": combo, "rep": rep, "spent_usd": round(spent, 4),
                               "at_kst": now()})
        return 4
    jobs = []
    for t in ctx.ts:
        out = os.path.join(gen_dir, f"{label}-{t}-gen.json")
        b = ctx.build(t, knobs)
        if os.path.exists(out):                  # 완료 출력은 손잡이와 입력(프롬프트)이 모두 같을 때만 건너뛴다
            prev = X.read_json(out)
            if not R.gen_complete(prev, k) or prev.get("knobs_sha256") != ksha \
                    or prev.get("prompt_text_sha256") != sha256_text(b["prompt"]):
                print(f"refuse: {os.path.basename(out)} incomplete or another setting/input; use a new rep",
                      file=sys.stderr)
                return 1
            continue
        head = {"label": label, "combo": combo, "rep": rep, "t": t, "knobs": knobs, "knobs_sha256": ksha,
                "c4_base": c4_base, "model": D.MODEL, "k": k,
                "recall_sha256": X.sha256_file(R.recall_path(ctx.args.recalls, t)),
                "prompt_sha256": X.sha256_file(D.BASELINE_PROMPT), "prompt_text_sha256": sha256_text(b["prompt"]),
                "filter": b["filter"], "table": b["stats"], "input_rows": len(b["asm"]["rows"]),
                "truncated_rows": b["asm"]["truncated_rows"], "input_chars": b["asm"]["chars"],
                "input_ids": [r["id"] for r in b["asm"]["rows"]], "max_input_chars": ctx.args.max_input_chars,
                "store_sha256": ctx.store_sha, "started_kst": now()}
        print(json.dumps({x: v for x, v in head.items() if x not in ("input_ids", "knobs")}, ensure_ascii=False))
        jobs.append({"t": t, "out": out, "head": head, "prompt": b["prompt"]})

    def checked_generate(prompt, call):          # d-llm 호출 전마다 sha 를 다시 본다
        guard_sha.check()
        return generate(prompt, call)
    started = now()
    rc = 0
    if jobs:
        try:
            rc = X.run_setting(jobs, caller or D.claude_caller(D.MODEL), guard, k, generate=checked_generate,
                               write=R.write_private, now=now)
        except ShaMismatch as e:
            print(str(e), file=sys.stderr)
            rc = 5
    cost = Counter()
    for t in ctx.ts:
        p = os.path.join(gen_dir, f"{label}-{t}-gen.json")
        if os.path.exists(p):
            cost.update(R.gen_cost(X.read_json(p)))
    ledger_append(ledger, {"event": "run", "combo": combo, "rep": rep, "label": label, "c4_base": c4_base,
                           "knobs_sha256": ksha, "exit": rc, "calls": cost.get("calls", 0),
                           "cost_usd": round(cost.get("cost_usd", 0.0), 6), "input_tokens": cost.get("input_tokens", 0),
                           "output_tokens": cost.get("output_tokens", 0), "seconds": cost.get("seconds", 0.0),
                           "spent_total_usd": round(guard.total(), 4), "sha256": sha_map(ctx.guard_paths()),
                           "started_kst": started, "finished_kst": now()})
    print(json.dumps({"label": label, "exit": rc, "calls": cost.get("calls", 0),
                      "cost_usd": round(cost.get("cost_usd", 0.0), 4), "spent_total_usd": round(guard.total(), 4)}))
    return rc


# --- 판정 ---------------------------------------------------------------------------------------------

class JudgeCtx(Ctx):
    """판정 입력: 라벨 쌍, 기준선 결과에 나온 쌍, S4 상태, #372 LLM 역할 상태(R_bank #374 분모)."""

    def __init__(self, args):
        super().__init__(args)
        R = self.R
        pos, ctl = R.dev_pairs(self.labelset)
        self.spos = {p for p in pos if p[1] in self.ts}
        self.sctl = {p for p in ctl if p[1] in self.ts}
        self.present: Set[Tuple[str, str]] = set()
        for t in self.ts:
            rows, _ = R.t_input(self.docs[t], t)
            for kk, s in R.t_signals(rows, R.keys_at(pos | ctl, t), self.aliases).items():
                if s["present"]:
                    self.present.add((kk, t))
        self.meta, self.parsed, _ = X._load_parsed(args)
        self.llm_doc = X.read_json(args.llm_roles)
        self.msha = X.sha256_file(args.mentions)
        self.by_id = {str(m["id"]): m for m in self.meta}
        self.s4: Dict[Tuple[str, str], Optional[str]] = {}
        self.b374: Dict[Tuple[str, str], Optional[str]] = {}
        self._s4_by_t: Dict[str, Dict[str, OT.KeyState]] = {}
        for t in self.ts:
            st4 = self.s4_states(t)
            self._s4_by_t[t] = st4
            s374 = X.states_for("o3c_llm", t, self.meta, self.parsed, {}, self.llm_doc, mentions_sha256=self.msha)
            for kk in R.keys_at(self.spos | self.sctl, t):
                self.s4[(kk, t)] = st4[kk].state if kk in st4 else None
                self.b374[(kk, t)] = (s374.get(kk) or {}).get("state")
        self.code_to_key = {X.key_code(kk): kk for kk, _ in self.spos | self.sctl}

    def s4_state(self, key: str, t: str) -> Optional[str]:
        st = self._s4_by_t[t].get(key)
        return st.state if st else None


def reclassify(base_items: Iterable[Mapping[str, Any]], s4_state: Callable[[str, str], Optional[str]],
               code_to_key: Mapping[str, str]) -> Tuple[List[Dict[str, Any]], int]:
    """#374 판독(언급 유무·판독 상태)은 그대로, LLM 상태 자리에 S4 상태를 넣어 사유를 다시 분류한다.
    (t, 키 코드, 쪽) 마다 1항목. 돌려주는 수 = #374 사유와 달라진 항목 수."""
    seen: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    changed = 0
    for it in base_items:
        k = (it["t"], it["key_code"], it["side"])
        if k in seen:
            continue
        key = code_to_key.get(it["key_code"])
        if key is None:
            raise Fail(f"key code {it['key_code']} not in the dev sample pairs")
        ev = dict(it["evidence"])
        st = s4_state(key, it["t"])
        ev["llm_state_374"] = ev.get("llm_state")
        ev["llm_state"] = st
        reason = X.classify_reason(it["side"], ev["has_side_mention"], ev["reader_state"], st)
        changed += reason != it["reason"]
        seen[k] = {"setting": "r5c", "t": it["t"], "key_code": it["key_code"], "side": it["side"], "reason": reason,
                   "reason_374": it["reason"], "evidence": ev}
    return list(seen.values()), changed


def new_read_items(reads: Iterable[Mapping[str, Any]], s4_state: Callable[[str, str], Optional[str]],
                   code_to_key: Mapping[str, str]) -> List[Dict[str, Any]]:
    """새로 판독한 쌍(근거 칸: has_side_mention, reader_state, memory_id, reader_role) -> 사유 항목(S4 상태로 분류)."""
    out = []
    for r in reads:
        key = code_to_key.get(r["key_code"])
        if key is None:
            raise Fail(f"key code {r['key_code']} not in the dev sample pairs")
        ev = dict(r["evidence"])
        ev["llm_state"] = s4_state(key, r["t"])
        out.append({"setting": "r5c-new", "t": r["t"], "key_code": r["key_code"], "side": r["side"],
                    "reason": X.classify_reason(r["side"], ev["has_side_mention"], ev["reader_state"], ev["llm_state"]),
                    "evidence": ev})
    return out


def rep_metrics(jc: JudgeCtx, gen_dir: str, label: str, reasons: Mapping[Tuple[str, str, str], str]) -> Dict[str, Any]:
    R = jc.R
    st, info = R.judge_label(gen_dir, label, jc.ts, jc.spos, jc.sctl, jc.aliases)
    adj = X.bank_adjusted(st, jc.spos, jc.sctl, jc.present, reasons)
    rb4 = X.rbank_split(jc.s4, st, jc.sctl)
    rb374 = X.rbank_split(jc.b374, st, jc.sctl)
    return {"states": st, "info": info, "adj": adj, "R_bank_s4": rb4, "R_bank_374": rb374}


def verify_prompts(jc: JudgeCtx, gen_dir: str, label: str) -> Dict[str, Any]:
    """gen 머리의 손잡이로 입력을 다시 만들어 prompt_text_sha256 대조, t별 토큰 비율."""
    per_t = {}
    for t in jc.ts:
        gen = X.read_json(os.path.join(gen_dir, f"{label}-{t}-gen.json"))
        b = jc.build(t, gen["knobs"])
        if sha256_text(b["prompt"]) != gen.get("prompt_text_sha256"):
            raise Fail(f"{label}-{t}: rebuilt prompt differs from the generated one")
        per_t[t] = {k: b["stats"][k] for k in ("rows", "tokens", "payload_tokens", "input_tokens", "ratio",
                                               "payload_ratio", "attached_queries", "tables")}
    return {"per_t": per_t, "ratio_max": max(v["ratio"] for v in per_t.values()),
            "payload_ratio_max": max(v["payload_ratio"] for v in per_t.values())}


def check_combo_gens(gen_dir: str, combo: str, labels: Sequence[str], ts: Sequence[str]) -> str:
    """--run 라벨의 gen 이 모두 그 조합(머리 combo)이고 t·반복 사이 knobs_sha256 이 하나인지. 그 sha 를 돌려준다."""
    shas = set()
    for lab in labels:
        for t in ts:
            gen = X.read_json(os.path.join(gen_dir, f"{lab}-{t}-gen.json"))
            if gen.get("combo") != combo:
                raise Fail(f"{lab}-{t}: gen combo {gen.get('combo')} != {combo}")
            shas.add(gen.get("knobs_sha256"))
    if len(shas) != 1:
        raise Fail(f"{combo}: knobs_sha256 differs across reps/t")
    return shas.pop()


def select_best(combos: Mapping[str, Mapping[str, Any]], p_min: float = P_BANK_MIN,
                tok_max: float = TOKEN_MAX) -> Dict[str, Any]:
    """combos[name] = {"P_bank": [반복별], "R_bank": [반복별], "tokens": 텍스트 기준 추가 토큰 최대}."""
    def mean(xs):
        xs = [x for x in xs if x is not None]
        return sum(xs) / len(xs) if xs else None
    rows = {}
    for name, c in combos.items():
        pb = list(c["P_bank"])
        ok = len(pb) >= 2 and all(x is not None and x >= p_min for x in pb) and c["tokens"] <= tok_max   # 두 반복 모두
        rows[name] = {"eligible": ok, "R_bank_mean": mean(c["R_bank"]), "P_bank_min": min(
            (x for x in pb if x is not None), default=None), "tokens": c["tokens"]}
    elig = [n for n, r in rows.items() if r["eligible"]]
    if elig:
        best = sorted(elig, key=lambda n: (-(rows[n]["R_bank_mean"] or 0.0), rows[n]["tokens"], n))[0]
        return {"best": best, "verdict": "eligible", "rows": rows}
    best = sorted(rows, key=lambda n: (-(rows[n]["P_bank_min"] or 0.0), -(rows[n]["R_bank_mean"] or 0.0),
                                       rows[n]["tokens"], n))[0] if rows else None
    return {"best": best, "verdict": "no-eligible", "rows": rows}


def select_vs_ref(combos: Mapping[str, Mapping[str, Any]], ref: str, p_min: float = P_BANK_MIN,
                  tok_max: float = TOKEN_MAX) -> Dict[str, Any]:
    """#381 선택: `select_best` 의 자격 판정 그대로, 자격 조합 중 R_bank 평균이 참고 조합(ref)보다 엄격히 높은 것의
    최대(같으면 토큰 최소). 없거나 동률이면 ref 유지."""
    sel = select_best(combos, p_min, tok_max)
    rows = sel["rows"]
    ref_r = rows[ref]["R_bank_mean"]
    cands = [n for n, r in rows.items() if n != ref and r["eligible"] and r["R_bank_mean"] is not None
             and ref_r is not None and r["R_bank_mean"] > ref_r + 1e-12]
    best = sorted(cands, key=lambda n: (-rows[n]["R_bank_mean"], rows[n]["tokens"], n))[0] if cands else ref
    return {"best": best, "replace": best != ref, "ref": ref, "select_best": sel}


def load_ref(path: str, combo: str, ts: Sequence[str], store_sha: str, knobs_sha: str,
             pairs: Optional[Mapping[str, int]] = None) -> Dict[str, Any]:
    """#365 judge 출력의 조합 값(재측정 없음). t·저장소 sha·손잡이 sha·쌍 수(라벨셋·기준선 결과)가 지금 입력과 같아야 한다."""
    doc = X.read_json(path)
    if list(doc.get("t") or []) != list(ts) or doc.get("store_sha256") != store_sha:
        raise Fail("reference judge was made from other t or store")
    if pairs is not None and doc.get("pairs") != dict(pairs):
        raise Fail("reference judge was made from other labelset pairs")
    reps = ((doc.get("combos") or {}).get(combo) or {}).get("reps") or []
    if len(reps) < 2:
        raise Fail(f"reference judge has fewer than two {combo} reps")
    if any(r.get("knobs_sha256") != knobs_sha for r in reps):
        raise Fail(f"reference {combo} knobs_sha256 differs from the harness {combo}")
    return {"P_bank": [r["P_bank"] for r in reps], "R_bank": [r["R_bank_s4"]["R_bank"] for r in reps],
            "tokens": max(r["tokens"]["ratio_max"] for r in reps), "labels": [r["label"] for r in reps],
            "judge_sha256": X.sha256_file(path)}


def outside_split(p_errors: Iterable[Tuple[str, str]], open_by_t: Mapping[str, Set[str]],
                  reasons: Mapping[Tuple[str, str, str], str]) -> Dict[str, Any]:
    """#381 1단계. P 오답 (키, t) 마다 그 t 표의 열림 행에 키가 있었나(in/out). 전체와 P_bank 분모에 남는 (i)·(iv)."""
    def summary(pairs: List[Tuple[str, str, bool]]) -> Dict[str, Any]:
        where: Dict[str, Set[bool]] = {}
        for k, _t, inside in pairs:
            where.setdefault(k, set()).add(inside)
        n, n_in = len(pairs), sum(1 for p in pairs if p[2])
        keys_in = sum(1 for v in where.values() if v == {True})
        keys_out = sum(1 for v in where.values() if v == {False})
        return {"pairs": n, "pairs_in": n_in, "pairs_out": n - n_in,
                "pair_out_ratio": (n - n_in) / n if n else None, "keys": len(where), "keys_in": keys_in,
                "keys_out": keys_out, "keys_mixed": len(where) - keys_in - keys_out,
                "key_out_ratio": keys_out / len(where) if where else None}
    rows, by_reason = [], {r: {"in": 0, "out": 0} for r in X.REASONS}
    for k, t in sorted(p_errors):
        r = reasons.get((t, X.key_code(k), "P"))
        if r is None:
            raise Fail(f"P error {t}/{X.key_code(k)} has no reason")
        inside = k in open_by_t.get(t, set())
        by_reason[r]["in" if inside else "out"] += 1
        rows.append((k, t, inside, r))
    return {"all": summary([(k, t, i) for k, t, i, _ in rows]),
            "pbank": summary([(k, t, i) for k, t, i, r in rows if r in ("i", "iv")]), "by_reason": by_reason,
            "pairs": [{"t": t, "key_code": X.key_code(k), "in_table": i, "reason": r} for k, t, i, r in rows]}


def parse_runs(specs: Sequence[str]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for s in specs or []:
        name, _, labs = s.partition("=")
        if name not in COMBOS or not labs:
            raise Fail(f"bad --run {s} (want C1=C1-r1,C1-r2)")
        labels = [x for x in labs.split(",") if x]
        if len(set(labels)) != len(labels):
            raise Fail(f"bad --run {s}: duplicate label")
        out[name] = labels
    return out


# --- 명령 ---------------------------------------------------------------------------------------------

def cmd_prep(args) -> int:
    ctx = Ctx(args)
    out: Dict[str, Any] = {"t": ctx.ts, "store_sha256": ctx.store_sha, "plan_sha256": ctx.plan_sha,
                           "store_meta": {k: v for k, v in ctx.store.meta.items() if k != "bank"},
                           "store_mentions": len(ctx.store.mentions), "combos": {}, "c4_identical": {}}
    if ctx.c3_header:
        out["c3_header_sha256"] = sha256_text(ctx.c3_header)
    for combo in ("C1", "C2", "C3") + tuple(c for c in HEADER_COMBOS if c in ctx.headers):
        kn = ctx.knobs(combo)
        per_t = {t: ctx.build(t, kn)["stats"] for t in ctx.ts}
        out["combos"][combo] = {"knobs_sha256": knobs_sha256(kn), "per_t": per_t,
                                "ratio_max": max(v["ratio"] for v in per_t.values()),
                                "payload_ratio_max": max(v["payload_ratio"] for v in per_t.values())}
        if combo in HEADER_COMBOS:               # #381: C4 동일성은 C1~C3 기준만
            continue
        c4 = ctx.knobs("C4", kn)
        same = same_inputs(ctx, c4, kn)
        out["c4_identical"][combo] = {"all": all(same.values()), "by_t": same, "c4_knobs_sha256": knobs_sha256(c4),
                                      "attached_queries": {t: ctx.build(t, c4)["stats"]["attached_queries"]
                                                           for t in ctx.ts}}
    if getattr(args, "families", None):        # #374 표 범위(질의 범위 프로젝트)와의 키 수 차이
        scopes = ctx.R.query_projects(args.plan, args.families)
        out["scope_compare"] = {}
        for t in ctx.ts:
            st = ctx.s4_states(t)
            opened = [kk for kk, s in st.items() if s.state == OT.STATE_OPEN]
            out["scope_compare"][t] = {"bank_all_open": len(opened),
                                       "query_projects_open": sum(1 for kk in opened
                                                                  if kk.rpartition("#")[0] in scopes.get(t, set()))}
    sha = X.write_private_json(args.out, out)
    brief = {c: {"ratio_max": round(v["ratio_max"], 4), "payload_ratio_max": round(v["payload_ratio_max"], 4),
                 "rows": [v["per_t"][t]["rows"] for t in ctx.ts], "knobs_sha256": v["knobs_sha256"]}
             for c, v in out["combos"].items()}
    print(json.dumps({"combos": brief, "c4_identical": {c: v["all"] for c, v in out["c4_identical"].items()},
                      "scope_compare": out.get("scope_compare")}, ensure_ascii=False))
    print(json.dumps({"out_sha256": sha}))
    return 0


def cmd_run(args) -> int:
    ctx = Ctx(args)
    return run_rep(ctx, args.combo, args.rep, gen_dir=args.gen_dir, spent_dir=args.spent_dir, ledger=args.ledger,
                   max_usd=args.max_usd, soft_usd=args.soft_usd, k=args.k, c4_base=args.c4_base)


def _reason_items(path: str) -> List[Dict[str, Any]]:
    return X.load_reasons(path)


def cmd_reasons(args) -> int:
    jc = JudgeCtx(args)
    base = _reason_items(args.base_reasons)
    items, changed = reclassify(base, jc.s4_state, jc.code_to_key)
    new = []
    if args.new_reads:
        doc = X.read_json(args.new_reads)
        new = new_read_items(doc["items"] if isinstance(doc, dict) else doc, jc.s4_state, jc.code_to_key)
        have = {(i["t"], i["key_code"], i["side"]) for i in items}
        dup = [n for n in new if (n["t"], n["key_code"], n["side"]) in have]
        if dup:
            raise Fail(f"{len(dup)} new reads duplicate a #374 reading")
    out = {"base_reasons_sha256": X.sha256_file(args.base_reasons), "store_sha256": jc.store_sha,
           "reader": "worker-1", "changed_vs_374": changed, "base_items": len(items), "new_items": len(new),
           "items": items + new}
    sha = X.write_private_json(args.out, out)
    X.load_reasons(args.out)                     # 판정 순서 재검증
    dist = Counter((i["side"], i["reason"]) for i in out["items"])
    print(json.dumps({"base_items": len(items), "changed_vs_374": changed, "new_items": len(new),
                      "dist": {f"{s}:{r}": n for (s, r), n in sorted(dist.items())}}))
    print(json.dumps({"out_sha256": sha}))
    return 0


def _store_roles(path: str, key: str) -> List[Dict[str, Any]]:
    conn = OT.connect_ro(path)
    try:
        return [{"mid": m, "role": r, "at": a, "state": s} for m, r, a, s in conn.execute(
            "SELECT r.memory_id, r.role, m.mentioned_at, m.state FROM mention_roles r JOIN memories m "
            "ON m.memory_id = r.memory_id WHERE r.key = ? ORDER BY m.mentioned_at, r.memory_id", (key,))]
    finally:
        conn.close()


def cmd_cands(args) -> int:
    """라벨마다 P 오답·R 누락 중 사유 파일(#374 재분류 + 새 판독)에 없는 쌍과 근거(리포 밖 0600, 판독 입력)."""
    jc = JudgeCtx(args)
    have = {(i["t"], i["key_code"], i["side"]) for i in _reason_items(args.reasons)}
    role_of = X.llm_role_lookup(X.role_items(jc.parsed), jc.llm_doc)
    out: Dict[str, Any] = {"t": jc.ts, "labels": {}, "store_sha256": jc.store_sha}
    for lab in args.label:
        st, _ = jc.R.judge_label(args.gen_dir, lab, jc.ts, jc.spos, jc.sctl, jc.aliases)
        errs = X.error_pairs(st, jc.spos, jc.sctl, jc.present)
        items = []
        for side in X.SIDES:
            for (kk, t) in errs[side]:
                if (t, X.key_code(kk), side) in have:
                    continue
                end = X.t_end(X._day(t))
                items.append({"label": lab, "t": t, "key": kk, "key_code": X.key_code(kk), "side": side,
                              "in_rows": (kk, t) in jc.present, "s4_state": jc.s4_state(kk, t),
                              "s4_roles": [r for r in _store_roles(args.store, kk)
                                           if (X.parse_instant(r["at"]) or end) <= end],
                              "evidence": X._mention_evidence(kk, t, jc.parsed, jc.by_id, role_of)})
        out["labels"][lab] = {"P_errors": len(errs["P"]), "R_misses": len(errs["R"]), "new": items}
    sha = X.write_private_json(args.out, out)
    uniq = {(i["t"], i["key_code"], i["side"]) for v in out["labels"].values() for i in v["new"]}
    print(json.dumps({lab: {"P_errors": v["P_errors"], "R_misses": v["R_misses"], "new": len(v["new"])}
                      for lab, v in out["labels"].items()}))
    print(json.dumps({"new_unique": len(uniq), "out_sha256": sha}))
    return 0


def cmd_judge(args) -> int:
    jc = JudgeCtx(args)
    R = jc.R
    if len(set(args.base)) < 2:
        raise Fail("judge needs at least two distinct --base labels for the R_bank noise floor")
    reasons = X.reason_index(_reason_items(args.reasons))
    runs = parse_runs(args.run)
    out: Dict[str, Any] = {"t": jc.ts, "reasons_sha256": X.sha256_file(args.reasons), "store_sha256": jc.store_sha,
                           "pairs": {"positive": len(jc.spos), "control": len(jc.sctl),
                                     "present_control": len(jc.sctl & jc.present)},
                           "bank_states": {"s4": dict(Counter(v or "none" for p, v in jc.s4.items() if p in jc.sctl)),
                                           "374": dict(Counter(v or "none" for p, v in jc.b374.items()
                                                               if p in jc.sctl))},
                           "base": [], "combos": {}}
    base_states = []
    for lab in args.base:
        st, info = R.judge_label(args.base_gen_dir, lab, jc.ts, jc.spos, jc.sctl, jc.aliases)
        base_states.append(st)
        row = {"label": lab, "R_bank_s4": X.rbank_split(jc.s4, st, jc.sctl),
               "R_bank_374": X.rbank_split(jc.b374, st, jc.sctl), "pr": R.pr_summary(st, jc.spos, jc.sctl, jc.present)}
        if lab == args.base[0]:
            row["adj"] = X.bank_adjusted(st, jc.spos, jc.sctl, jc.present, reasons)
        out["base"].append(row)
    b1 = base_states[0]
    out["noise_floor"] = {"R_bank_s4": R.noise_floor([X.rbank_values(s, jc.s4, jc.sctl) for s in base_states]),
                          "R_bank_374": R.noise_floor([X.rbank_values(s, jc.b374, jc.sctl) for s in base_states])}
    sel_in = {}
    for combo, labs in runs.items():
        reps = []
        check_combo_gens(args.gen_dir, combo, labs, jc.ts)
        for lab in labs:
            m = rep_metrics(jc, args.gen_dir, lab, reasons)
            tok = verify_prompts(jc, args.gen_dir, lab)
            st = m["states"]
            rep = {"label": lab, **{k: m["adj"][k] for k in ("P", "P_num", "P_den", "P_bank", "P_bank_den", "R",
                                                             "R_num", "R_den", "R_bank2", "R_bank2_den", "R_all",
                                                             "P_errors", "R_misses", "P_reasons", "R_reasons")},
                   "R_bank_s4": m["R_bank_s4"], "R_bank_374": m["R_bank_374"], "tokens": tok,
                   "paired_vs_base1": {
                       "R_bank_s4": R.cluster_bootstrap(X.rbank_values(st, jc.s4, jc.sctl),
                                                        X.rbank_values(b1, jc.s4, jc.sctl)),
                       "R": R.cluster_bootstrap(R.pr_values(st, jc.spos, jc.sctl, jc.present)["R"],
                                                R.pr_values(b1, jc.spos, jc.sctl, jc.present)["R"]),
                       "P": R.cluster_bootstrap(R.pr_values(st, jc.spos, jc.sctl)["P"],
                                                R.pr_values(b1, jc.spos, jc.sctl)["P"])},
                   "invalid_reps": {t: m["info"]["per_t"][t]["invalid_reps"] for t in jc.ts},
                   "cost": m["info"]["cost"],
                   "knobs_sha256": X.read_json(os.path.join(args.gen_dir, f"{lab}-{jc.ts[0]}-gen.json"))["knobs_sha256"]}
            reps.append(rep)
        out["combos"][combo] = {"reps": reps}
        sel_in[combo] = {"P_bank": [r["P_bank"] for r in reps], "R_bank": [r["R_bank_s4"]["R_bank"] for r in reps],
                         "tokens": max(r["tokens"]["ratio_max"] for r in reps)}
    out["selection"] = select_best(sel_in)
    if getattr(args, "ref_judge", None):         # #381: C1 은 #365 판정 출력 참고값(재측정 없음)
        ref = args.ref_combo
        if ref in sel_in:
            raise Fail(f"{ref} is both --run and --ref-combo")
        out["reference"] = dict(load_ref(args.ref_judge, ref, jc.ts, jc.store_sha,
                                         knobs_sha256(jc.knobs(ref)), out["pairs"]), combo=ref)
        out["selection_vs_ref"] = select_vs_ref(dict(sel_in, **{ref: {k: out["reference"][k] for k in (
            "P_bank", "R_bank", "tokens")}}), ref)
    sha = X.write_private_json(args.out, out)
    if args.ledger:
        for combo, v in out["combos"].items():
            for r in v["reps"]:
                ledger_append(args.ledger, {"event": "judge", "combo": combo, "label": r["label"],
                                            "knobs_sha256": r["knobs_sha256"],
                                            "metrics": {k: r[k] for k in ("P", "P_bank", "R", "R_bank2", "R_all")},
                                            "R_bank_s4": r["R_bank_s4"]["R_bank"],
                                            "R_bank_374": r["R_bank_374"]["R_bank"],
                                            "tokens_ratio_max": r["tokens"]["ratio_max"],
                                            "payload_ratio_max": r["tokens"]["payload_ratio_max"],
                                            "cost_usd": r["cost"].get("cost_usd"), "judge_sha256": sha,
                                            "reasons_sha256": out["reasons_sha256"]})
    brief = {"noise_floor_R_bank_s4": out["noise_floor"]["R_bank_s4"]["floor"],
             "base1": {"P": out["base"][0]["adj"]["P"], "P_bank": out["base"][0]["adj"]["P_bank"],
                       "R": out["base"][0]["adj"]["R"], "R_bank_s4": out["base"][0]["R_bank_s4"]["R_bank"],
                       "R_bank_374": out["base"][0]["R_bank_374"]["R_bank"]},
             "combos": {c: [{"label": r["label"], "P": r["P"], "P_bank": r["P_bank"], "R": r["R"],
                             "R_bank_s4": r["R_bank_s4"]["R_bank"], "R_bank_374": r["R_bank_374"]["R_bank"],
                             "R_all": r["R_all"], "tok": round(r["tokens"]["ratio_max"], 4),
                             "tok_payload": round(r["tokens"]["payload_ratio_max"], 4),
                             "cost": r["cost"].get("cost_usd")} for r in v["reps"]]
                        for c, v in out["combos"].items()},
             "selection": {k: out["selection"][k] for k in ("best", "verdict")}}
    if "selection_vs_ref" in out:
        brief["selection_vs_ref"] = {k: out["selection_vs_ref"][k] for k in ("best", "replace", "ref")}
    print(json.dumps(brief, ensure_ascii=False))
    print(json.dumps({"out_sha256": sha}))
    return 0


def table_states(open_by_t: Mapping[str, Set[str]], pairs: Iterable[Tuple[str, str]]) -> Dict[Tuple[str, str], str]:
    """#383 3단계: 소비자 없이 표 열림 행을 그대로 목록으로 본 쌍 상태(열림 행에 있으면 open, 아니면 closed)."""
    return {(k, t): ("open" if k in open_by_t.get(t, set()) else "closed") for k, t in pairs}


def table_score(open_by_t: Mapping[str, Set[str]], spos: Set[Tuple[str, str]], sctl: Set[Tuple[str, str]],
                present: Set[Tuple[str, str]], bank_new: Mapping[Tuple[str, str], Optional[str]],
                bank_ref: Mapping[Tuple[str, str], Optional[str]],
                reasons: Mapping[Tuple[str, str, str], str]) -> Dict[str, Any]:
    """표 자체 정밀도(지출 0): P·R·R_all(`pr_summary`), 사유가 다 있으면 P_bank·R_bank2(`bank_adjusted`),
    R_bank 는 두 분모(새 저장소 열림 / 참고 저장소 열림, `rbank_split`). 사유 없는 오답 쌍은 `missing` 으로 낸다."""
    from bench import r5_metrics as R
    st = table_states(open_by_t, spos | sctl)
    errs = X.error_pairs(st, spos, sctl, present)
    missing = [(side, t, k) for side in X.SIDES for (k, t) in errs[side] if (t, X.key_code(k), side) not in reasons]
    out: Dict[str, Any] = {"states": st, "pr": R.pr_summary(st, spos, sctl, present),
                           "P_errors": len(errs["P"]), "R_misses": len(errs["R"]), "missing": missing,
                           "R_bank_new": X.rbank_split(bank_new, st, sctl), "R_bank_ref": X.rbank_split(bank_ref, st, sctl),
                           "adj": None}
    if not missing:
        out["adj"] = X.bank_adjusted(st, spos, sctl, present, reasons)
    return out


def check_reasons_store(path: str, store_sha: str) -> None:
    """사유는 이 --store 상태로 분류된 것이어야 한다((i)/(ii)/(iii) 가 저장소 상태에 달림, final-review #383-1).
    `reasons` 가 쓴 객체의 store_sha256 만 대조한다. 목록 형식이나 sha 가 없는 파일은 대조할 수 없어 거부한다."""
    doc = X.read_json(path)
    got = doc.get("store_sha256") if isinstance(doc, dict) else None
    if got is None:
        raise Fail("reasons file has no store_sha256 to compare with --store (make it with `reasons --store`)")
    if got != store_sha:
        raise Fail("reasons file was classified with another store (store_sha256 differs from --store)")


def cmd_table(args) -> int:
    """#383 3단계(지출 0, 읽기 전용): --store 의 C1 표(태그 없는 인자 = 뱅크 전체) 열림 행을 그대로 목록으로 채점.
    --ref-store(S4a) 는 R_bank 참고 분모로만 쓴다. 사유 없는 오답 쌍은 `cands` 식 근거를 리포 밖 --out 에 쓴다."""
    jc = JudgeCtx(args)
    kn = jc.knobs("C1")
    ref = OT.read_store(args.ref_store, STORE_TIMEOUT_S)
    if ref.meta.get("aliases_sha256") != OT.aliases_sha256(jc.aliases):
        raise Fail("alias table differs from the reference store meta aliases_sha256")
    open_by_t: Dict[str, Set[str]] = {}
    bank_ref: Dict[Tuple[str, str], Optional[str]] = {}
    rows_by_t: Dict[str, int] = {}
    for t in jc.ts:
        b = jc.build(t, kn)
        open_by_t[t] = set(b["open_keys"])
        rows_by_t[t] = b["stats"]["rows"]
        if b["stats"]["truncated"]:
            raise Fail(f"{t}: table truncated - open rows are not the whole bank")
        st_ref = OT.role_states(ref.mentions, item_status._instant(jc.plan["per_t"][t]["query_timestamp"]))
        for kk in jc.R.keys_at(jc.spos | jc.sctl, t):
            bank_ref[(kk, t)] = st_ref[kk].state if kk in st_ref else None
    reasons = {}
    if args.reasons:
        check_reasons_store(args.reasons, jc.store_sha)
        reasons = X.reason_index(_reason_items(args.reasons))
    sc = table_score(open_by_t, jc.spos, jc.sctl, jc.present, jc.s4, bank_ref, reasons)
    role_of = X.llm_role_lookup(X.role_items(jc.parsed), jc.llm_doc)
    new = []
    for side, t, kk in sc["missing"]:
        end = X.t_end(X._day(t))
        new.append({"t": t, "key": kk, "key_code": X.key_code(kk), "side": side, "in_rows": (kk, t) in jc.present,
                    "s4_state": jc.s4_state(kk, t), "ref_state": bank_ref.get((kk, t)),
                    "s4_roles": [r for r in _store_roles(args.store, kk) if (X.parse_instant(r["at"]) or end) <= end],
                    "evidence": X._mention_evidence(kk, t, jc.parsed, jc.by_id, role_of)})
    adj = sc["adj"]
    summary = {"t": jc.ts, "store_sha256": jc.store_sha, "ref_store_sha256": X.sha256_file(args.ref_store),
               "knobs_sha256": knobs_sha256(kn), "reasons_sha256": X.sha256_file(args.reasons) if args.reasons else None,
               "table_rows": rows_by_t, "table_open_keys": {t: len(v) for t, v in open_by_t.items()},
               "pairs": {"positive": len(jc.spos), "control": len(jc.sctl), "present_control": len(jc.sctl & jc.present)},
               "P": sc["pr"]["P"], "P_num": sc["pr"]["P_num"], "P_den": sc["pr"]["P_den"], "R": sc["pr"]["R"],
               "R_all": sc["pr"]["R_all"], "P_errors": sc["P_errors"], "R_misses": sc["R_misses"],
               "P_bank": adj["P_bank"] if adj else None, "P_bank_den": adj["P_bank_den"] if adj else None,
               "R_bank2": adj["R_bank2"] if adj else None,
               "P_reasons": adj["P_reasons"] if adj else None, "R_reasons": adj["R_reasons"] if adj else None,
               "R_bank_new": {k: sc["R_bank_new"][k] for k in ("R_bank", "R_bank_num", "R_bank_den")},
               "R_bank_ref": {k: sc["R_bank_ref"][k] for k in ("R_bank", "R_bank_num", "R_bank_den")},
               "unreasoned": {"P": sum(1 for m in sc["missing"] if m[0] == "P"),
                              "R": sum(1 for m in sc["missing"] if m[0] == "R")}}
    sha = X.write_private_json(args.out, dict(summary, new=new))
    print(json.dumps(summary, ensure_ascii=False))
    print(json.dumps({"out_sha256": sha}))
    return 0


def cmd_outside(args) -> int:
    """#381 1단계(지출 0, 읽기 전용): C3 저장 gen 의 P 오답이 그 t 의 C3 표 밖 키였는가. 출력은 수만."""
    jc = JudgeCtx(args)
    check_combo_gens(args.gen_dir, args.combo, args.label, jc.ts)
    reasons = X.reason_index(_reason_items(args.reasons))
    kn = jc.knobs(args.combo)
    open_by_t = {t: set(jc.build(t, kn)["open_keys"]) for t in jc.ts}
    out: Dict[str, Any] = {"t": jc.ts, "combo": args.combo, "knobs_sha256": knobs_sha256(kn),
                           "reasons_sha256": X.sha256_file(args.reasons), "store_sha256": jc.store_sha,
                           "table_open_keys": {t: len(v) for t, v in open_by_t.items()}, "labels": {}}
    for lab in args.label:
        st, _ = jc.R.judge_label(args.gen_dir, lab, jc.ts, jc.spos, jc.sctl, jc.aliases)
        errs = X.error_pairs(st, jc.spos, jc.sctl, jc.present)
        out["labels"][lab] = outside_split(errs["P"], open_by_t, reasons)
    sha = X.write_private_json(args.out, out)
    print(json.dumps({"table_open_keys": out["table_open_keys"],
                      "labels": {lab: {k: v[k] for k in ("all", "pbank", "by_reason")}
                                 for lab, v in out["labels"].items()}}, ensure_ascii=False))
    print(json.dumps({"out_sha256": sha}))
    return 0


def cmd_best(args) -> int:
    """판정 출력의 최선 조합 손잡이를 리포 밖 0600 JSON 으로. 파일 sha256 과 정규 sha256(감사 줄 knobs_sha256)."""
    doc = X.read_json(args.judge)
    best = doc["selection"]["best"]
    vs_ref = doc.get("selection_vs_ref")
    if vs_ref is not None:                       # #381: 참고 대비 판정(--ref-judge)이 있으면 그 결정을 따른다
        if not vs_ref.get("replace"):
            raise Fail(f"selection_vs_ref keeps the reference {vs_ref.get('ref')}; use its knobs file, not --run combos")
        best = vs_ref["best"]
    if best is None:
        raise Fail("no combo to pick")
    lab = doc["combos"][best]["reps"][0]["label"]
    gen = X.read_json(os.path.join(args.gen_dir, f"{lab}-{doc['t'][0]}-gen.json"))
    values = gen["knobs"]
    if knobs_sha256(values) != gen["knobs_sha256"]:
        raise Fail("gen knobs do not match their sha")
    sha_file = X.write_private_json(args.out, values)
    with open(args.out, encoding="utf-8") as f:
        parsed, warnings = OT.parse_knobs(f.read())
    if warnings:
        raise Fail(f"knobs file warnings {warnings}")
    verdict = "replace-vs-ref" if vs_ref is not None else doc["selection"]["verdict"]
    print(json.dumps({"best": best, "verdict": verdict, "file_sha256": sha_file,
                      "canonical_sha256": knobs_sha256(parsed), "table_fmt": parsed["table_fmt"],
                      "attach": parsed["attach"], "header_sha256": sha256_text(parsed["header"])}))
    return 0


def _common(p, judge: bool = False) -> None:
    p.add_argument("--labelset", required=True)
    p.add_argument("--aliases", required=True)
    p.add_argument("--policy")
    p.add_argument("--plan", required=True)
    p.add_argument("--recalls", required=True)
    p.add_argument("--store", required=True)
    p.add_argument("--t", action="append", required=True)
    p.add_argument("--c3-header-file")
    p.add_argument("--c5-header-file")
    p.add_argument("--c6-header-file")
    p.add_argument("--max-input-chars", type=int, default=200000)
    if judge:
        p.add_argument("--meta", required=True)
        p.add_argument("--mentions", required=True)
        p.add_argument("--llm-roles", required=True)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="r5_tune.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prep")
    _common(p)
    p.add_argument("--families")
    p.add_argument("--out", required=True)
    p = sub.add_parser("run")
    _common(p)
    p.add_argument("--combo", required=True, choices=COMBOS)
    p.add_argument("--rep", required=True, type=int)
    p.add_argument("--c4-base", choices=("C1", "C2", "C3"))
    p.add_argument("--k", type=int, default=3)
    p.add_argument("--gen-dir", required=True)
    p.add_argument("--spent-dir", required=True)
    p.add_argument("--ledger", required=True)
    p.add_argument("--max-usd", type=float, required=True)
    p.add_argument("--soft-usd", type=float, required=True)
    p = sub.add_parser("cands")
    _common(p, judge=True)
    p.add_argument("--gen-dir", required=True)
    p.add_argument("--reasons", required=True)
    p.add_argument("--label", action="append", required=True)
    p.add_argument("--out", required=True)
    p = sub.add_parser("reasons")
    _common(p, judge=True)
    p.add_argument("--base-reasons", required=True)
    p.add_argument("--new-reads")
    p.add_argument("--out", required=True)
    p = sub.add_parser("judge")
    _common(p, judge=True)
    p.add_argument("--reasons", required=True)
    p.add_argument("--gen-dir", required=True)
    p.add_argument("--base-gen-dir", required=True)
    p.add_argument("--base", action="append", required=True)
    p.add_argument("--run", action="append", required=True)
    p.add_argument("--ledger")
    p.add_argument("--ref-judge")
    p.add_argument("--ref-combo", default="C1", choices=COMBOS)
    p.add_argument("--out", required=True)
    p = sub.add_parser("outside")
    _common(p, judge=True)
    p.add_argument("--gen-dir", required=True)
    p.add_argument("--reasons", required=True)
    p.add_argument("--combo", default="C3", choices=COMBOS)
    p.add_argument("--label", action="append", required=True)
    p.add_argument("--out", required=True)
    p = sub.add_parser("table")
    _common(p, judge=True)
    p.add_argument("--ref-store", required=True)
    p.add_argument("--reasons")
    p.add_argument("--out", required=True)
    p = sub.add_parser("best")
    p.add_argument("--judge", required=True)
    p.add_argument("--gen-dir", required=True)
    p.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    try:
        return {"prep": cmd_prep, "run": cmd_run, "cands": cmd_cands, "reasons": cmd_reasons, "judge": cmd_judge,
                "best": cmd_best, "outside": cmd_outside, "table": cmd_table}[args.cmd](args)
    except (Fail, X.Fail, OT.StoreError) as e:
        print(f"fail: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - r5_metrics.Fail 등 하위 모듈 거부
        if type(e).__name__ != "Fail":
            raise
        print(f"fail: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
