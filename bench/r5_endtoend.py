"""런 5 종단 확인(R5-D, issue #366, 상위 #360 "S4·S3 구현"). S3 를 켠 D10 사본의 실제 게이트웨이 응답으로 잰다.

  r5_endtoend.py sealed-plan --labelset <jsonl> --families <json> --instruction <txt> --project-template <txt> --out <json>
  r5_endtoend.py recall  --stage dev|sealed|symptom|keyless --arm on|off --plan <json> [--t ...] --out-dir <dir>
                         [--symptom-file <txt>] --work <dir>                  (hc-next 사본만, 읽기만)
  r5_endtoend.py dllm    --stage ... --arm ... --recalls <dir> --t ... --k 7 --gen-dir <dir> --spent-dir <dir>
                         --max-usd 25 --soft-usd 20 --work <dir>
  r5_endtoend.py audit   --log <state>/audit/gateway.log --windows <json> --out <json>
  r5_endtoend.py bodies  --meta <jsonl> --labelset <jsonl> --bank <bank> --out <jsonl> --work <dir>   (사본만)
  r5_endtoend.py judge-dev    (r5_tune 판정 인자) --gen-dir --label --s3-recalls <dir> --reasons --out
  r5_endtoend.py judge-sealed --labelset --aliases --store --recalls-on --recalls-off --gen-dir [--reasons] --out --work
  r5_endtoend.py cands-sealed --labelset --aliases --store --recalls-off --gen-dir --bodies --meta --reasons --out --work
  r5_endtoend.py merge-reasons --labelset --store --base <reasons json> --new-reads <json> --out
  r5_endtoend.py symptom --gen-dir --answer-key <C> --answer-key <D> --out

원문(질의 문안·recall 응답·표 텍스트·d-llm 출력·본문)은 리포 밖 0600 파일에만 쓴다. 표준 출력에는 수치·sha256 만 낸다.

왜 새 스크립트인가(plan-review 060032 O1·O2). `bench/d_llm.py` `save_recalls`·`r5_metrics cmd_recall` 은 결과 행만 저장해
S3 표(payload 최상위 `gateway_item_open_table`)를 버리고, `check_not_sealed` 가 봉인 t 를 거부한다. 이 모듈은
  * recall: `save_recalls` 와 같은 인자 `{query, query_timestamp, max_tokens}`(tags 없음, S3 `scope` request_project 가 표를
    좁히지 않게)로 질의마다 응답의 표 객체를 결과 행과 함께 저장한다(`recall_doc`). 질의 = 지시문 + 줄바꿈 + 문안(원래 증상
    경로만 문안 그대로).
  * d-llm 입력: #365 와 같은 규칙. t 의 합집합(질의 순 -> 순위 순, 기억 id 중복 제거) -> 시간여행 필터 -> 200,000자 상한 렌더
    + 저장 payload 표 `text` 를 질의 순으로(같은 텍스트 1회) 줄바꿈으로 이어 recall 행 뒤에(`r5_metrics.build_prompt_with_table`).
    gen 머리에 t별 표 sha256(= 저장 payload `text` 들을 이은 문자열의 sha256)과 `prompt_text_sha256` 을 남긴다(`check_gen_table`).
  * 봉인 가드(코디네이터 결정 plan-review 20261102-105545): 봉인 단계는 (단계, 팔) 단위로 1회만. `recall-on`, `recall-off`,
    `dllm-on`, `dllm-off` 각각 기록 파일(`<work>/sealed-runs.jsonl`)에 줄이 있으면 거부한다. 기록은 실행 **전에** 남긴다
    (중단돼도 다시 돌리지 않는다). 판정·지표 계산은 저장 파일 읽기만이라 여러 번 돌려도 된다. 봉인 본문 수집(`bodies`)은
    recall 두 팔이 끝난 뒤, 본문 판독 입력(`cands-sealed`)은 d-llm 두 팔이 끝난 뒤에만 허용한다.
    rework #366-1: 봉인 단계는 봉인 t 전체를 정확히 요구하고 아니면 기록 전에 거부한다(Codex P1). 기록은 시작 줄
    (`status` started)과 성공 줄(`status` ok)로 나누고, 뒤 단계의 선행 조건(`require`)은 성공 줄만 센다(Codex P2).
  * 감사 줄(`audit`): 측정별 UTC 구간마다 decision `item-open-table` 줄의 사유·`knobs_source`·`knobs_sha256`·경고 수.
기존 bench 모듈(`r5_metrics`, `r5_roles`, `r5_labels`, `r5_tune`, 동결 파일)은 import 만 하고 바꾸지 않는다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from bench import r5_labels as L  # noqa: E402
from bench import r5_metrics as R  # noqa: E402
from bench import r5_roles as X  # noqa: E402
from gateway import item_open_table as OT  # noqa: E402
from gateway import item_status  # noqa: E402

STAGES = ("dev", "sealed", "symptom", "keyless")
ARMS = ("on", "off")
SEAL_STEPS = ("recall-on", "recall-off", "dllm-on", "dllm-off")
SEAL_RECORD = "sealed-runs.jsonl"
SYMPTOM_DAY = "2026-10-28"
SYMPTOM_TS = "2026-10-28T14:59:59Z"          # d_llm.R41_QUERY_TIMESTAMP
P_BANK_MIN = 0.95
MIN_DEN = 5
FIELD = OT.FIELD


class Fail(Exception):
    pass


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- 봉인 1회 가드 ------------------------------------------------------------------------------------

class SealGuard:
    """봉인 단계의 (단계, 팔) 1회 기록. claim 은 실행 전에 줄을 남기고, 이미 있으면 Fail."""

    def __init__(self, work: str):
        self.path = os.path.join(work, SEAL_RECORD)

    def records(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self.path):
            return []
        with open(self.path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def done(self, step: str) -> bool:
        """시도 기록이 있는가(재실행 거부 기준, 성공 여부 무관)."""
        return any(r.get("step") == step for r in self.records())

    def succeeded(self, step: str) -> bool:
        """성공 기록이 있는가(뒤 단계의 선행 조건, rework #366-1 Codex P2).
        `status` 필드가 없는 줄은 성공 줄로 센다: 이 형식 전의 기록(#366 런 1회차 4줄)이고, 그 네 단계는 실행 로그로
        성공(recall 오류 0, d-llm exit 0·gen 완료)이 확인됐다. 새 기록은 시작 줄(`started`)과 성공 줄(`ok`)을 따로 쓴다."""
        return any(r.get("step") == step and r.get("status", "ok") == "ok" for r in self.records())

    def _append(self, rec: Mapping[str, Any]) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), mode=0o700, exist_ok=True)
        line = json.dumps(dict(rec, at_utc=utc_now()), ensure_ascii=False, sort_keys=True) + "\n"
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)

    def claim(self, step: str, **info: Any) -> None:
        if step not in SEAL_STEPS:
            raise Fail(f"unknown sealed step {step}")
        if self.done(step):
            raise Fail(f"refuse: sealed step {step} already ran once (record {SEAL_RECORD})")
        self._append(dict(info, step=step, status="started"))

    def succeed(self, step: str) -> None:
        if not any(r.get("step") == step and r.get("status") == "started" for r in self.records()):
            raise Fail(f"sealed step {step} has no start record")
        self._append({"step": step, "status": "ok"})

    def require(self, *steps: str) -> None:
        missing = [s for s in steps if not self.succeeded(s)]
        if missing:
            raise Fail(f"refuse: sealed step(s) {','.join(missing)} have not finished successfully")


# --- 질의 계획 ----------------------------------------------------------------------------------------

def sealed_pairs(labelset: Sequence[Mapping[str, Any]]) -> Tuple[Set[Tuple[str, str]], Set[Tuple[str, str]]]:
    """봉인 키의 봉인 구간 쌍(split=sealed)의 양성·대조."""
    pos, ctl = set(), set()
    for r in labelset:
        if r.get("split") != "sealed":
            continue
        if r.get("label") == L.LABEL_POSITIVE:
            pos.add((r["key"], r["t"]))
        elif r.get("label") == L.LABEL_CONTROL:
            ctl.add((r["key"], r["t"]))
    return pos, ctl


def sealed_plan(labelset: Sequence[Mapping[str, Any]], families: Mapping[str, Any], instruction: str,
                project_template: str) -> Dict[str, Any]:
    """`r5_metrics.make_plan` 과 같은 질의(요청 가족 + 그 t 에 쌍이 있는 프로젝트마다 상태 질의)를 봉인 쌍의 t 에."""
    if R.REF_DATE not in instruction:
        raise Fail(f"instruction has no reference date {R.REF_DATE}")
    if "{project}" not in project_template:
        raise Fail("project template needs {project}")
    pos, ctl = sealed_pairs(labelset)
    if not pos and not ctl:
        raise Fail("labelset has no sealed pairs")
    projects: Dict[str, Set[str]] = {}
    for k, t in pos | ctl:
        projects.setdefault(t, set()).add(L.parse_key(k)[0])
    fams = [{"qid": q["qid"], "scope": q.get("scope"), "text": q["text"]} for q in families["queries"]]
    per_t = {}
    for t in sorted(projects):
        qs = list(fams) + [{"qid": f"P:{p}", "scope": "project-status", "text": project_template.replace("{project}", p)}
                           for p in sorted(projects[t])]
        per_t[t] = {"instruction": instruction.replace(R.REF_DATE, t), "query_timestamp": R.query_timestamp(R._day(t)),
                    "queries": qs}
    return {"per_t": per_t, "families": len(fams), "max_tokens": R.MAX_TOKENS, "stage": "sealed"}


def query_args(instruction: Optional[str], text: str, query_timestamp: str, max_tokens: int) -> Dict[str, Any]:
    """`d_llm.save_recalls` 와 같은 인자(tags 없음). 지시문이 없으면(원래 증상 경로) 문안 그대로."""
    query = f"{instruction.strip()}\n{text.strip()}" if instruction else text.strip()
    return {"query": query, "query_timestamp": query_timestamp, "max_tokens": max_tokens}


# --- recall 저장(표 객체 포함) -------------------------------------------------------------------------

def recall_doc(call: Callable[[str, Dict[str, Any]], Dict[str, Any]], instruction: Optional[str],
               queries: Sequence[Mapping[str, Any]], query_timestamp: str, max_tokens: int,
               counter: Optional[Callable[[str], int]] = None) -> Dict[str, Any]:
    """질의마다 recall 1회. `save_recalls` 와 같은 runs 모양 + 응답 payload 의 `gateway_item_open_table` 객체(`open_table`).
    실패는 error 로 남기고 빈 결과로 계속한다."""
    runs = []
    started = utc_now()
    for n, q in enumerate(queries, 1):
        args = query_args(instruction, q["text"], query_timestamp, max_tokens)
        t0 = time.monotonic()
        try:
            res, err = call("recall", args), None
        except Exception as e:  # noqa: BLE001 - 저장은 계속, 오류 종류만
            res, err = {"results": []}, type(e).__name__
        secs = round(time.monotonic() - t0, 3)
        rows = res.get("results") or []
        table = res.get(FIELD) if isinstance(res.get(FIELD), dict) else None
        payload = {k: v for k, v in res.items()}
        runs.append({"q": n, "qid": q.get("qid"), "scope": q.get("scope"), "max_tokens": max_tokens,
                     "seconds": float(secs), "error": err, "rows": len(rows),
                     "response_chars": sum(len(r.get("text") or "") for r in rows),
                     "response_tokens": sum(counter(r.get("text") or "") for r in rows) if counter else None,
                     "payload_tokens": counter(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
                     if counter else None,
                     "results": rows, "open_table": table,
                     "open_table_sha256": sha256_text(table["text"]) if table and isinstance(table.get("text"), str)
                     else None})
    return {"query_timestamp": query_timestamp, "max_tokens": max_tokens, "runs": runs, "started_utc": started,
            "finished_utc": utc_now(),
            "totals": {"queries": len(runs), "errors": sum(1 for r in runs if r["error"]),
                       "rows": sum(r["rows"] for r in runs),
                       "tables": sum(1 for r in runs if r["open_table"] is not None),
                       "seconds": sum(r["seconds"] for r in runs)}}


def tables_of(doc: Mapping[str, Any]) -> Dict[str, Any]:
    """저장 payload 표를 질의 순으로, 같은 text 는 1회(#365 `t_tables` 와 같은 규칙)."""
    texts: List[str] = []
    objs: List[Dict[str, Any]] = []
    attached = 0
    for r in sorted(doc.get("runs") or [], key=lambda x: x.get("q", 0)):
        tab = r.get("open_table")
        if not tab:
            continue
        attached += 1
        text = tab.get("text")
        if not isinstance(text, str):
            raise Fail("open table without text")
        if text in texts:
            continue
        texts.append(text)
        objs.append(tab)
    joined = "\n".join(texts)
    return {"texts": texts, "objs": objs, "text": joined, "attached": attached,
            "sha256": sha256_text(joined) if texts else None}


def open_keys(doc: Mapping[str, Any]) -> Set[str]:
    """표 자체 지표용: 저장 payload 표 rows 의 열림 키."""
    return {row[0] for tab in tables_of(doc)["objs"] for row in tab.get("rows") or [] if row[1] == OT.STATE_OPEN}


def build_input(D, template: str, doc: Mapping[str, Any], t: str, max_input_chars: int) -> Dict[str, Any]:
    """d-llm 프롬프트: 합집합 -> 시간여행 필터 -> 렌더(상한) + 저장 표 text. 표가 없으면 기준선 프롬프트와 같다."""
    rows, st = R.t_input(doc, t)
    asm = D.assemble({"runs": [{"q": 1, "max_tokens": 0, "results": rows}]}, max_input_chars=max_input_chars)
    tab = tables_of(doc)
    prompt = R.build_prompt_with_table(D, template, asm, t, tab["text"])
    return {"prompt": prompt, "asm": asm, "filter": st, "table": tab}


def check_gen_table(gen: Mapping[str, Any], doc: Mapping[str, Any], prompt: str) -> None:
    """gen 머리의 표 sha256 = 저장 payload 표 text 의 sha256 이고, 프롬프트가 그 text 를 담고, prompt sha 가 같다."""
    tab = tables_of(doc)
    if gen.get("table_sha256") != tab["sha256"]:
        raise Fail("gen table_sha256 differs from the stored payload table text")
    if tab["text"] and tab["text"] not in prompt:
        raise Fail("prompt does not contain the stored payload table text")
    if gen.get("prompt_text_sha256") != sha256_text(prompt):
        raise Fail("gen prompt_text_sha256 differs from the rebuilt prompt")


# --- 감사 줄 -----------------------------------------------------------------------------------------

def audit_counts(lines: Iterable[str], start_utc: str, end_utc: str) -> Dict[str, Any]:
    """[start, end] UTC 초 구간의 decision item-open-table 줄: 사유·knobs_source·knobs_sha256·경고·aliases_mismatch 수."""
    reason, source, ksha = Counter(), Counter(), Counter()
    warn = mism = n = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("decision") != OT.DECISION:
            continue
        ts = e.get("ts") or ""
        if not (start_utc <= ts <= end_utc):
            continue
        n += 1
        info = e.get("item_open_table") or {}
        reason[e.get("reason")] += 1
        source[info.get("knobs_source")] += 1
        ksha[info.get("knobs_sha256")] += 1
        warn += 1 if info.get("knob_warnings") else 0
        mism += 1 if info.get("aliases_mismatch") else 0
    return {"lines": n, "reason": dict(reason), "knobs_source": dict(source), "knobs_sha256": dict(ksha),
            "knob_warnings": warn, "aliases_mismatch": mism}


def audit_ok(c: Mapping[str, Any], recalls: int, knobs_sha: str) -> bool:
    """S3 켬 측정 조건: attached = recall 수, 다른 사유 0, source file|env, sha 하나 = 기대값, 경고·불일치 0."""
    return (c["reason"] == {"attached": recalls} and set(c["knobs_source"]) <= {"file", "env"}
            and set(c["knobs_sha256"]) == {knobs_sha} and c["knob_warnings"] == 0 and c["aliases_mismatch"] == 0)


# --- 채점(봉인·원래 증상) ------------------------------------------------------------------------------

def seal_verdict(p_bank_on: Optional[float], p_bank_den: int, rb_on: Optional[float], rb_off: Optional[float],
                 rb_den: int) -> Dict[str, Any]:
    """"봉인에서도 유지" = S3 켬 P_bank >= 95% 이고 R_bank(S4 분모) 켬 - 끔 > 0. 분모 5 미만 지표는 판정 불가."""
    out: Dict[str, Any] = {}
    if p_bank_den < MIN_DEN or p_bank_on is None:
        out["P_bank"] = "undecidable"
    else:
        out["P_bank"] = "met" if p_bank_on >= P_BANK_MIN else "not-met"
    if rb_den < MIN_DEN or rb_on is None or rb_off is None:
        out["R_bank"] = "undecidable"
    else:
        out["R_bank"] = "met" if rb_on - rb_off > 0 else "not-met"
    vals = set(out.values())
    out["sealed"] = "undecidable" if "undecidable" in vals else ("met" if vals == {"met"} else "not-met")
    return out


def _norm_name(s: Any) -> str:
    return re.sub(r"\s+", "", str(s or "")).lower()


def union_tasks(docs: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """세트 C·D 정답표의 작업 표 합집합. 같은 항목(작업 키가 겹치거나 이름이 같음)이 두 표에 같은 라벨이면 1개, 다른 라벨이면
    둘 다 뺀다(수를 낸다)."""
    from bench import d_llm as D
    from bench import d_llm_match as M
    items = []
    for i, doc in enumerate(docs):
        names = {it["id"]: (it.get("name") or it.get("item")) for cat in ("closed", "controls", "partial")
                 for it in doc.get(cat) or []}
        for t in D.tasks_from_answer_key(doc):
            items.append({"task": dict(t, id=f"s{i}:{t['id']}"), "keys": M.task_keys(t),
                          "name": _norm_name(names.get(t["id"]))})
    drop: Set[str] = set()
    same = 0
    for a in range(len(items)):
        for b in range(a + 1, len(items)):
            x, y = items[a], items[b]
            if x["task"]["id"].split(":")[0] == y["task"]["id"].split(":")[0]:
                continue
            if not ((x["keys"] & y["keys"]) or (x["name"] and x["name"] == y["name"])):
                continue
            if x["task"]["category"] == y["task"]["category"]:
                drop.add(y["task"]["id"])
                same += 1
            else:
                drop.update({x["task"]["id"], y["task"]["id"]})
    tasks = [it["task"] for it in items if it["task"]["id"] not in drop]
    conflict = len(drop) - same
    return {"tasks": tasks, "duplicates_same_label": same, "excluded_conflicting": conflict,
            "count": dict(Counter(t["category"] for t in tasks))}


def symptom_score(gens: Sequence[Mapping[str, Any]], tasks: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """반복마다 열린 목록(status open 항목) 중 정답표 끝난 일(closed)에 대응한 비율. 원래 증상(22개 중 9개)과 같은 뜻."""
    from bench import d_llm_match as M
    cat = {t["id"]: t["category"] for t in tasks}
    reps = []
    for g in gens:
        if not g.get("valid"):
            reps.append({"valid": False})
            continue
        items = g.get("items") or []
        j = M.judge_run(items, tasks)
        c = Counter()
        for it, m in zip(items, j["mapping"]):
            if it.get("status") != "open":
                continue
            c["open_items"] += 1
            c[cat[m["task"]] if m["task"] else "unmapped"] += 1
        oi = c["open_items"]
        reps.append({"valid": True, "items": len(items), "open_items": oi, "closed": c["closed"],
                     "controls": c["controls"], "partial": c["partial"], "unmapped": c["unmapped"],
                     "done_ratio": (c["closed"] / oi) if oi else None,
                     "done_ratio_mapped": (c["closed"] / (oi - c["unmapped"])) if oi - c["unmapped"] else None})
    res = M.judge_runs([dict(g) for g in gens], tasks)
    need = len(gens) // 2 + 1
    maj_open = {i: v["open"] >= need for i, v in res["tasks"].items()}
    ok = [r for r in reps if r.get("valid")]
    mean = (lambda xs: sum(xs) / len(xs) if xs else None)
    return {"K": len(gens), "invalid": sum(1 for r in reps if not r.get("valid")), "reps": reps,
            "done_ratio_mean": mean([r["done_ratio"] for r in ok if r["done_ratio"] is not None]),
            "open_items_mean": mean([r["open_items"] for r in ok]),
            "majority_open": {c: sum(1 for i, o in maj_open.items() if o and cat[i] == c)
                              for c in ("closed", "controls", "partial")},
            "tasks": dict(Counter(cat.values()))}


# --- 명령 공통 ----------------------------------------------------------------------------------------

def _plan_spec(stage: str, plan: Mapping[str, Any], t: str, symptom_text: Optional[str]) -> Dict[str, Any]:
    if stage == "symptom":
        if not symptom_text:
            raise Fail("symptom stage needs --symptom-file")
        return {"instruction": None, "query_timestamp": SYMPTOM_TS,
                "queries": [{"qid": "S1", "scope": "symptom", "text": symptom_text}]}
    return plan["per_t"][t]


def _stage_ts(stage: str, plan: Optional[Mapping[str, Any]], ts: Optional[Sequence[str]]) -> List[str]:
    if stage == "symptom":
        return [SYMPTOM_DAY]
    if stage == "keyless":
        return [SYMPTOM_DAY]
    return sorted(ts or (plan or {}).get("per_t") or [])


def check_sealed_ts(ts: Sequence[str], want: Iterable[str]) -> None:
    """봉인 단계는 봉인 t 전체를 정확히 요구한다(rework #366-1 Codex P1). 1회 기록을 남기기 전에 부른다."""
    want = sorted(set(want))
    if not want or sorted(set(ts)) != want or len(ts) != len(want):
        raise Fail(f"refuse: sealed stage needs exactly all sealed t {','.join(want) or '-'} (got {','.join(ts) or '-'})")


def _check_stage_plan(stage: str, plan: Mapping[str, Any], ts: Sequence[str]) -> None:
    if stage == "sealed" and plan.get("stage") != "sealed":
        raise Fail("sealed stage needs the sealed plan")
    if stage == "dev" and plan.get("stage") == "sealed":
        raise Fail("dev stage got the sealed plan")
    for t in ts:
        if t not in plan["per_t"]:
            raise Fail(f"t {t} not in the plan")
    if stage == "dev" and any(R._day(t) > X.DEV_LAST_T for t in ts):
        raise Fail("dev t beyond the dev upper bound")


def cmd_sealed_plan(args) -> int:
    labelset = X.read_jsonl(args.labelset)
    with open(args.families, encoding="utf-8") as f:
        fam = json.load(f)
    with open(args.instruction, encoding="utf-8") as f:
        instr = f.read()
    with open(args.project_template, encoding="utf-8") as f:
        ptpl = f.read().strip()
    if os.path.exists(args.out):
        raise Fail(f"{args.out} exists")
    plan = sealed_plan(labelset, fam, instr, ptpl)
    plan.update(labelset_sha256=X.sha256_file(args.labelset), families_sha256=X.sha256_file(args.families),
                instruction_sha256=X.sha256_file(args.instruction),
                project_template_sha256=X.sha256_file(args.project_template))
    sha = R.write_private(args.out, plan)
    print(json.dumps({"t": sorted(plan["per_t"]), "queries": {t: len(v["queries"]) for t, v in plan["per_t"].items()},
                      "plan_sha256": sha}))
    return 0


def cmd_recall(args) -> int:
    from bench import arch_v2_regression as A
    from bench import d_llm as D
    env = dict(os.environ)
    plan = X.read_json(args.plan) if args.plan else {}
    ts = _stage_ts(args.stage, plan, args.t)
    if args.stage in ("dev", "sealed"):
        _check_stage_plan(args.stage, plan, ts)
    if args.stage == "sealed":                   # 대상 확인·기록 전에(일부 t 로 1회 기록을 써 버리지 않게)
        check_sealed_ts(ts, plan["per_t"])
    try:
        target = A.resolve_target(env, False, False)
    except A.Refused as e:
        raise Fail(str(e))
    if target.kind != "next":
        raise Fail("recall runs only against the hc-next copy")
    symptom = None
    if args.stage == "symptom":
        with open(args.symptom_file, encoding="utf-8") as f:
            symptom = f.read().strip()
    guard = SealGuard(args.work) if args.stage == "sealed" else None
    for t in ts:
        if os.path.exists(R.recall_path(args.out_dir, t)):
            raise Fail(f"{os.path.basename(R.recall_path(args.out_dir, t))} exists in --out-dir")
    if guard:
        guard.claim(f"recall-{args.arm}", plan_sha256=X.sha256_file(args.plan), t=ts)
    print(target.line(), file=sys.stderr)

    class _Args:
        bank = None
        owner = None
    ns = _Args()
    ns.bank = A.resolve_bank(ns, env)
    gw, _ = A.build_clients(target, ns, env)
    counter = D.tiktoken_counter()
    os.makedirs(args.out_dir, mode=0o700, exist_ok=True)
    errors = 0
    for t in ts:
        spec = _plan_spec(args.stage, plan, t, symptom)
        doc = recall_doc(gw.call, spec["instruction"], spec["queries"], spec["query_timestamp"],
                         plan.get("max_tokens", R.MAX_TOKENS), counter)
        doc = dict(doc, stage=args.stage, arm=args.arm, t=t, plan_sha256=X.sha256_file(args.plan) if args.plan else None,
                   symptom_sha256=sha256_text(symptom) if symptom else None, target="next")
        sha = R.write_private(R.recall_path(args.out_dir, t), doc)
        errors += doc["totals"]["errors"]
        print(json.dumps({"t": t, **doc["totals"], "started_utc": doc["started_utc"],
                          "finished_utc": doc["finished_utc"], "table_sha256": tables_of(doc)["sha256"],
                          "sha256": sha}))
    if guard and not errors:                     # 오류 0 으로 모든 t 를 저장했을 때만 성공 줄
        guard.succeed(f"recall-{args.arm}")
    return 1 if errors else 0


def cmd_dllm(args) -> int:
    from bench import d_llm as D
    ts = _stage_ts(args.stage, None, args.t)
    if args.stage == "sealed":                   # 봉인 recall 디렉터리의 t 전체(기록 전에)
        have = sorted(n[2:-5] for n in os.listdir(args.recalls) if re.fullmatch(r"t-\d{4}-\d{2}-\d{2}\.json", n)) \
            if os.path.isdir(args.recalls) else []
        check_sealed_ts(ts, have)
    docs = R.load_recalls(args.recalls, ts)
    for t in ts:
        d = docs[t]
        if d.get("stage") != args.stage or d.get("arm") != args.arm:
            raise Fail(f"recall t-{t} is stage {d.get('stage')}/{d.get('arm')}, not {args.stage}/{args.arm}")
        if args.arm == "on" and not tables_of(d)["texts"]:
            raise Fail(f"recall t-{t} (S3 on) has no open table")
        if args.arm == "off" and tables_of(d)["texts"]:
            raise Fail(f"recall t-{t} (S3 off) has an open table")
    guard = X.SpendGuard(args.spent_dir, args.max_usd, args.gen_dir)
    spent = guard.total()
    if spent > args.soft_usd:
        print(f"stop: spent {spent:.3f} > soft-usd {args.soft_usd}; not started", file=sys.stderr)
        return 4
    label = f"{args.stage}-{args.arm}"
    template = D.load_prompt(D.BASELINE_PROMPT)
    jobs = []
    for t in ts:
        out = os.path.join(args.gen_dir, f"{label}-{t}-gen.json")
        if os.path.exists(out):
            raise Fail(f"{os.path.basename(out)} exists")
        b = build_input(D, template, docs[t], t, args.max_input_chars)
        head = {"label": label, "stage": args.stage, "arm": args.arm, "t": t, "model": D.MODEL, "k": args.k,
                "recall_sha256": X.sha256_file(R.recall_path(args.recalls, t)),
                "prompt_sha256": X.sha256_file(D.BASELINE_PROMPT), "prompt_text_sha256": sha256_text(b["prompt"]),
                "table_sha256": b["table"]["sha256"], "table_texts": len(b["table"]["texts"]),
                "table_attached_queries": b["table"]["attached"], "filter": b["filter"],
                "input_rows": len(b["asm"]["rows"]), "truncated_rows": b["asm"]["truncated_rows"],
                "input_chars": b["asm"]["chars"], "input_ids": [r["id"] for r in b["asm"]["rows"]],
                "max_input_chars": args.max_input_chars, "started_kst": D.now_kst()}
        print(json.dumps({x: v for x, v in head.items() if x != "input_ids"}, ensure_ascii=False))
        jobs.append({"t": t, "out": out, "head": head, "prompt": b["prompt"]})
    if args.stage == "sealed":
        SealGuard(args.work).claim(f"dllm-{args.arm}", t=ts, k=args.k)
    rc = X.run_setting(jobs, D.claude_caller(D.MODEL), guard, args.k, write=R.write_private)
    if args.stage == "sealed" and rc == 0 and all(
            os.path.exists(j["out"]) and R.gen_complete(X.read_json(j["out"]), args.k) for j in jobs):
        SealGuard(args.work).succeed(f"dllm-{args.arm}")
    cost = Counter()
    for j in jobs:
        if os.path.exists(j["out"]):
            cost.update(R.gen_cost(X.read_json(j["out"])))
    print(json.dumps({"label": label, "exit": rc, **{k: round(v, 4) if isinstance(v, float) else v
                                                     for k, v in cost.items()},
                      "spent_total_usd": round(guard.total(), 4)}))
    return rc


def cmd_audit(args) -> int:
    windows = X.read_json(args.windows)
    with open(args.log, encoding="utf-8") as f:
        lines = f.readlines()
    out = {"log_lines": len(lines), "windows": {}}
    for name, w in windows.items():
        c = audit_counts(lines, w["start_utc"], w["end_utc"])
        if "recalls" in w:
            c["recalls"] = w["recalls"]
            if w.get("arm") == "on":
                c["ok"] = audit_ok(c, w["recalls"], args.knobs_sha256)
        out["windows"][name] = dict(c, **{k: w[k] for k in ("start_utc", "end_utc")})
    sha = R.write_private(args.out, out)
    print(json.dumps(out["windows"], ensure_ascii=False))
    print(json.dumps({"out_sha256": sha}))
    return 0


def cmd_bodies(args) -> int:
    """봉인 키 기억 본문(봉인 상한까지)을 사본에서 수집해 리포 밖 0600 에만. recall 두 팔이 끝난 뒤에만."""
    SealGuard(args.work).require("recall-on", "recall-off")
    labelset = X.read_jsonl(args.labelset)
    pos, ctl = sealed_pairs(labelset)
    keys = {k for k, _ in pos | ctl}
    upper = max(R._day(t) for _, t in pos | ctl)
    meta = X.read_jsonl(args.meta)
    want = {mid: m for mid, m in X.body_targets(meta, upper).items() if set(m.get("keys") or []) & keys}
    bc = X._bank_config()
    container, is_copy, key = bc.resolve_target()
    if not is_copy:
        raise Fail("bodies reads the hc-next copy only (HC_TARGET=next)")
    if os.path.exists(args.out):
        raise Fail(f"{args.out} exists")
    got: Dict[str, Dict[str, Any]] = {}
    for ft in X.FACT_TYPES:
        for mem in X._list_memories(bc, container, key, args.bank, ft):
            rid = str(mem.get("id"))
            if rid in want and rid not in got:
                text = mem.get("text") if isinstance(mem.get("text"), str) else mem.get("content")
                got[rid] = {"id": rid, "text": text if isinstance(text, str) else "",
                            "mentioned_at": mem.get("mentioned_at")}
    text = "".join(json.dumps(got[r], sort_keys=True, ensure_ascii=False) + "\n" for r in sorted(got))
    sha = X.write_private_text(args.out, text)
    print(json.dumps({"targets": len(want), "collected": len(got), "missing": len(set(want) - set(got)),
                      "upper": upper.isoformat(), "sha256": sha}))
    return 0 if len(got) == len(want) else 1


# --- 판정 ---------------------------------------------------------------------------------------------

def _s4_by_t(store: OT.Store, ts: Iterable[str]) -> Dict[str, Dict[str, OT.KeyState]]:
    return {t: OT.role_states(store.mentions, item_status._instant(R.query_timestamp(R._day(t)))) for t in ts}


def _present(docs: Mapping[str, Mapping[str, Any]], pairs: Set[Tuple[str, str]], aliases) -> Set[Tuple[str, str]]:
    out = set()
    for t, d in docs.items():
        rows, _ = R.t_input(d, t)
        for k, s in R.t_signals(rows, R.keys_at(pairs, t), aliases).items():
            if s["present"]:
                out.add((k, t))
    return out


def _tokens(D, docs: Mapping[str, Mapping[str, Any]], ts: Sequence[str], max_chars: int) -> Dict[str, Any]:
    """추가 토큰: 표 text / 그 t 의 필터 뒤 d-llm 입력 행 토큰, payload 표 객체 토큰 / 같은 분모(t별 최대)."""
    from bench import r5_tune as T
    counter = D.tiktoken_counter()
    if counter is None:
        raise Fail("tiktoken is required (uv run --with tiktoken)")
    per = {}
    for t in ts:
        rows, _ = R.t_input(docs[t], t)
        asm = D.assemble({"runs": [{"q": 1, "max_tokens": 0, "results": rows}]}, max_input_chars=max_chars)
        den = counter("\n".join(D.render_row(r) for r in asm["rows"]))
        tab = tables_of(docs[t])
        tt = counter(tab["text"]) if tab["text"] else 0
        pt = max((counter(T.payload_json(o)) for o in tab["objs"]), default=0)
        per[t] = {"table_tokens": tt, "payload_tokens": pt, "input_tokens": den, "ratio": tt / den,
                  "payload_ratio": pt / den, "rows": sum(len(o["rows"]) for o in tab["objs"])}
    return {"per_t": per, "ratio_max": max(v["ratio"] for v in per.values()),
            "payload_ratio_max": max(v["payload_ratio"] for v in per.values())}


def cmd_judge_dev(args) -> int:
    """dev S3 켬 d-llm(K=7) 지표, 표 자체 지표, 오프라인(R5-C) 표와의 일치. 판정 입력은 #365 와 같은 JudgeCtx(rep1)."""
    from bench import d_llm as D
    from bench import r5_tune as T
    jc = T.JudgeCtx(args)
    s3 = R.load_recalls(args.s3_recalls, jc.ts)
    kn = jc.knobs("C1")
    offline = {}
    open_by_t = {}
    rows_same = {}
    for t in jc.ts:
        gen = X.read_json(os.path.join(args.gen_dir, f"{args.label}-{t}-gen.json"))
        b = build_input(D, jc.template, s3[t], t, args.max_input_chars)
        check_gen_table(gen, s3[t], b["prompt"])
        off = jc.build(t, kn)
        offline[t] = {"payload_table_sha256": tables_of(s3[t])["sha256"], "offline_table_sha256":
                      off["stats"]["table_sha256"], "same": tables_of(s3[t])["sha256"] == off["stats"]["table_sha256"]}
        open_by_t[t] = open_keys(s3[t])
        base_ids = [r["id"] for r in R.t_input(jc.docs[t], t)[0]]
        rows_same[t] = base_ids == [r["id"] for r in R.t_input(s3[t], t)[0]]
    reasons = {}
    if args.reasons:
        T.check_reasons_store(args.reasons, jc.store_sha)
        reasons = X.reason_index(X.load_reasons(args.reasons))
    st, info = R.judge_label(args.gen_dir, args.label, jc.ts, jc.spos, jc.sctl, jc.aliases)
    errs = X.error_pairs(st, jc.spos, jc.sctl, jc.present)
    missing = [(s, t, X.key_code(k)) for s in X.SIDES for (k, t) in errs[s] if (t, X.key_code(k), s) not in reasons]
    adj = X.bank_adjusted(st, jc.spos, jc.sctl, jc.present, reasons) if not missing else None
    base_st, _ = R.judge_label(args.base_gen_dir, args.base, jc.ts, jc.spos, jc.sctl, jc.aliases)
    tab = T.table_score(open_by_t, jc.spos, jc.sctl, jc.present, jc.s4, jc.b374, reasons)
    out = {"t": jc.ts, "label": args.label, "store_sha256": jc.store_sha, "knobs_sha256": T.knobs_sha256(kn),
           "offline_vs_payload": offline, "rows_same_as_rep1": rows_same,
           "pr": R.pr_summary(st, jc.spos, jc.sctl, jc.present), "adj": adj, "unreasoned": len(missing),
           "G1": R.dllm_rates(st, jc.spos, jc.present),
           "R_bank_s4": X.rbank_split(jc.s4, st, jc.sctl), "R_bank_374": X.rbank_split(jc.b374, st, jc.sctl),
           "paired_vs_base1": {
               "R_bank_s4": R.cluster_bootstrap(X.rbank_values(st, jc.s4, jc.sctl), X.rbank_values(base_st, jc.s4, jc.sctl)),
               "P": R.cluster_bootstrap(R.pr_values(st, jc.spos, jc.sctl)["P"], R.pr_values(base_st, jc.spos, jc.sctl)["P"]),
               "R": R.cluster_bootstrap(R.pr_values(st, jc.spos, jc.sctl, jc.present)["R"],
                                        R.pr_values(base_st, jc.spos, jc.sctl, jc.present)["R"])},
           "base1": {"pr": R.pr_summary(base_st, jc.spos, jc.sctl, jc.present),
                     "R_bank_s4": X.rbank_split(jc.s4, base_st, jc.sctl)},
           "tokens": _tokens(D, s3, jc.ts, args.max_input_chars),
           "table_self": {"pr": tab["pr"], "P_errors": tab["P_errors"], "R_misses": tab["R_misses"],
                          "unreasoned": len(tab["missing"]),
                          "P_bank": tab["adj"]["P_bank"] if tab["adj"] else None,
                          "R_bank_s4": {k: tab["R_bank_new"][k] for k in ("R_bank", "R_bank_num", "R_bank_den")}},
           "info": info, "invalid_reps": {t: info["per_t"][t]["invalid_reps"] for t in jc.ts}}
    sha = R.write_private(args.out, out)
    brief = {k: out[k] for k in ("pr", "unreasoned", "G1", "invalid_reps", "rows_same_as_rep1")}
    brief.update(P_bank=adj["P_bank"] if adj else None, R_bank_s4=out["R_bank_s4"]["R_bank"],
                 R_bank_374=out["R_bank_374"]["R_bank"], tokens=round(out["tokens"]["ratio_max"], 4),
                 payload=round(out["tokens"]["payload_ratio_max"], 4),
                 offline_same=all(v["same"] for v in offline.values()),
                 table_self={k: out["table_self"][k] for k in ("P_bank", "R_bank_s4", "unreasoned")},
                 table_self_P=tab["pr"]["P"], cost=info["cost"].get("cost_usd"))
    print(json.dumps(brief, ensure_ascii=False, default=str))
    print(json.dumps({"out_sha256": sha}))
    return 0


class SealedCtx:
    def __init__(self, args):
        self.labelset = X.read_jsonl(args.labelset)
        self.aliases = R.aliases_from(args)
        self.spos, self.sctl = sealed_pairs(self.labelset)
        self.ts = sorted({t for _, t in self.spos | self.sctl})
        self.store = OT.read_store(args.store, 60.0)
        self.store_sha = X.sha256_file(args.store)
        if self.store.meta.get("aliases_sha256") != OT.aliases_sha256(self.aliases):
            raise Fail("alias table differs from the store meta aliases_sha256")
        self.off = R.load_recalls(args.recalls_off, self.ts)
        self.present = _present(self.off, self.spos | self.sctl, self.aliases)
        by_t = _s4_by_t(self.store, self.ts)
        self.s4 = {(k, t): (by_t[t][k].state if k in by_t[t] else None) for k, t in self.spos | self.sctl}


def cmd_judge_sealed(args) -> int:
    from bench import d_llm as D
    from bench import r5_tune as T
    g = SealGuard(args.work)
    g.require("recall-on", "recall-off", "dllm-on", "dllm-off")
    sc = SealedCtx(args)
    on_docs = R.load_recalls(args.recalls_on, sc.ts)
    reasons = {}
    if args.reasons:
        T.check_reasons_store(args.reasons, sc.store_sha)
        reasons = X.reason_index(X.load_reasons(args.reasons))
    template = D.load_prompt(D.BASELINE_PROMPT)
    out: Dict[str, Any] = {"t": sc.ts, "store_sha256": sc.store_sha,
                           "pairs": {"positive": len(sc.spos), "control": len(sc.sctl),
                                     "present_positive": len(sc.spos & sc.present),
                                     "present_control": len(sc.sctl & sc.present)},
                           "s4_control_states": dict(Counter(v or "none" for p, v in sc.s4.items() if p in sc.sctl)),
                           "arms": {}}
    for arm, docs in (("on", on_docs), ("off", sc.off)):
        label = f"sealed-{arm}"
        for t in sc.ts:
            gen = X.read_json(os.path.join(args.gen_dir, f"{label}-{t}-gen.json"))
            check_gen_table(gen, docs[t], build_input(D, template, docs[t], t, args.max_input_chars)["prompt"])
        st, info = R.judge_label(args.gen_dir, label, sc.ts, sc.spos, sc.sctl, sc.aliases)
        errs = X.error_pairs(st, sc.spos, sc.sctl, sc.present)
        missing = [(s, t, X.key_code(k)) for s in X.SIDES for (k, t) in errs[s]
                   if (t, X.key_code(k), s) not in reasons]
        adj = X.bank_adjusted(st, sc.spos, sc.sctl, sc.present, reasons) if not missing else None
        out["arms"][arm] = {"pr": R.pr_summary(st, sc.spos, sc.sctl, sc.present), "adj": adj,
                            "unreasoned": {"P": sum(1 for m in missing if m[0] == "P"),
                                           "R": sum(1 for m in missing if m[0] == "R")},
                            "G1": R.dllm_rates(st, sc.spos, sc.present),
                            "R_bank_s4": X.rbank_split(sc.s4, st, sc.sctl),
                            "invalid_reps": {t: info["per_t"][t]["invalid_reps"] for t in sc.ts},
                            "cost": info["cost"], "prompt_text_sha256": {
                                t: X.read_json(os.path.join(args.gen_dir, f"{label}-{t}-gen.json"))["prompt_text_sha256"]
                                for t in sc.ts}}
    on, off = out["arms"]["on"], out["arms"]["off"]
    out["prompt_differs"] = {t: on["prompt_text_sha256"][t] != off["prompt_text_sha256"][t] for t in sc.ts}
    out["tokens"] = _tokens(D, on_docs, sc.ts, args.max_input_chars)
    tab = T.table_score({t: open_keys(on_docs[t]) for t in sc.ts}, sc.spos, sc.sctl, sc.present, sc.s4, sc.s4, reasons)
    out["table_self"] = {"pr": tab["pr"], "P_errors": tab["P_errors"], "unreasoned": len(tab["missing"]),
                         "P_bank": tab["adj"]["P_bank"] if tab["adj"] else None,
                         "R_bank_s4": {k: tab["R_bank_new"][k] for k in ("R_bank", "R_bank_num", "R_bank_den")}}
    pb = on["adj"]["P_bank"] if on["adj"] else None
    pbd = on["adj"]["P_bank_den"] if on["adj"] else 0
    out["verdict"] = seal_verdict(pb, pbd, on["R_bank_s4"]["R_bank"], off["R_bank_s4"]["R_bank"],
                                  on["R_bank_s4"]["R_bank_den"])
    sha = R.write_private(args.out, out)
    brief = {"pairs": out["pairs"], "verdict": out["verdict"], "prompt_differs": out["prompt_differs"],
             "tokens": round(out["tokens"]["ratio_max"], 4),
             "table_self": {"P": tab["pr"]["P"], "P_bank": out["table_self"]["P_bank"],
                            "R_bank_s4": out["table_self"]["R_bank_s4"]["R_bank"]}}
    for arm in ARMS:
        a = out["arms"][arm]
        brief[arm] = {"P": a["pr"]["P"], "P_den": a["pr"]["P_den"], "R": a["pr"]["R"], "R_den": a["pr"]["R_den"],
                      "R_all": a["pr"]["R_all"], "P_bank": a["adj"]["P_bank"] if a["adj"] else None,
                      "P_bank_den": a["adj"]["P_bank_den"] if a["adj"] else None,
                      "R_bank_s4": a["R_bank_s4"]["R_bank"], "R_bank_den": a["R_bank_s4"]["R_bank_den"],
                      "G1": a["G1"]["rate"], "unreasoned": a["unreasoned"], "cost": a["cost"].get("cost_usd")}
    print(json.dumps(brief, ensure_ascii=False, default=str))
    print(json.dumps({"out_sha256": sha}))
    return 0


def cmd_cands_sealed(args) -> int:
    """봉인 오답(P 오답·R 누락) 중 사유 없는 쌍과 판독 근거(S4 역할 + 본문, 리포 밖 0600). d-llm 두 팔이 끝난 뒤에만."""
    from bench import r5_tune as T
    SealGuard(args.work).require("recall-on", "recall-off", "dllm-on", "dllm-off")
    sc = SealedCtx(args)
    have = set()
    if args.reasons:
        have = {(i["t"], i["key_code"], i["side"]) for i in X.load_reasons(args.reasons)}
    bodies = {r["id"]: r for r in X.read_jsonl(args.bodies)}
    meta = {str(m["id"]): m for m in X.read_jsonl(args.meta)}
    on_docs = R.load_recalls(args.recalls_on, sc.ts)
    items: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    sources = [(f"sealed-{a}", R.judge_label(args.gen_dir, f"sealed-{a}", sc.ts, sc.spos, sc.sctl, sc.aliases)[0])
               for a in ARMS]
    sources.append(("table", T.table_states({t: open_keys(on_docs[t]) for t in sc.ts}, sc.spos | sc.sctl)))
    for lab, st in sources:
        errs = X.error_pairs(st, sc.spos, sc.sctl, sc.present)
        for side in X.SIDES:
            for kk, t in errs[side]:
                k = (t, X.key_code(kk), side)
                if k in have:
                    continue
                if k in items:
                    items[k]["labels"].append(lab)
                    continue
                end = X.t_end(R._day(t))
                roles = [r for r in T._store_roles(args.store, kk) if (X.parse_instant(r["at"]) or end) <= end]
                items[k] = {"labels": [lab], "t": t, "key": kk, "key_code": X.key_code(kk), "side": side,
                            "in_rows": (kk, t) in sc.present, "s4_state": sc.s4.get((kk, t)), "s4_roles": roles,
                            "bodies": [{"mid": r["mid"], "at": r["at"], "role": r["role"],
                                        "text": (bodies.get(r["mid"]) or {}).get("text")} for r in roles],
                            "meta_mentions": sorted(mid for mid, m in meta.items() if kk in (m.get("keys") or [])
                                                    and (X.parse_instant(m.get("mentioned_at")) or end) <= end)}
    out = {"t": sc.ts, "store_sha256": sc.store_sha, "items": list(items.values())}
    sha = R.write_private(args.out, out)
    print(json.dumps({"new": len(items), "by_side": dict(Counter(k[2] for k in items)),
                      "keys": len({k[1] for k in items}), "out_sha256": sha}))
    return 0


def cmd_merge_reasons(args) -> int:
    """사유 파일(base, store_sha256 = --store) + 새 판독(워커 1인) -> 새 사유 파일. 새 판독은 S4 상태로 분류."""
    from bench import r5_tune as T
    labelset = X.read_jsonl(args.labelset)
    store = OT.read_store(args.store, 60.0)
    store_sha = X.sha256_file(args.store)
    T.check_reasons_store(args.base, store_sha)
    base = X.load_reasons(args.base)
    code_to_key = {X.key_code(r["key"]): r["key"] for r in labelset}
    ts = sorted({r["t"] for r in X.read_json(args.new_reads)["items"]})
    by_t = _s4_by_t(store, ts)

    def s4_state(key: str, t: str) -> Optional[str]:
        st = by_t[t].get(key)
        return st.state if st else None
    new = T.new_read_items(X.read_json(args.new_reads)["items"], s4_state, code_to_key)
    have = {(i["t"], i["key_code"], i["side"]) for i in base}
    dup = [n for n in new if (n["t"], n["key_code"], n["side"]) in have]
    if dup:
        raise Fail(f"{len(dup)} new reads duplicate a base reading")
    out = {"base_reasons_sha256": X.sha256_file(args.base), "store_sha256": store_sha, "reader": "worker-1",
           "base_items": len(base), "new_items": len(new), "items": list(base) + new}
    sha = X.write_private_json(args.out, out)
    X.load_reasons(args.out)
    dist = Counter((i["side"], i["reason"]) for i in new)
    print(json.dumps({"base_items": len(base), "new_items": len(new),
                      "new_dist": {f"{s}:{r}": n for (s, r), n in sorted(dist.items())}, "out_sha256": sha}))
    return 0


def cmd_symptom(args) -> int:
    docs = [X.read_json(p) for p in args.answer_key]
    u = union_tasks(docs)
    out: Dict[str, Any] = {"answer_keys_sha256": [X.sha256_file(p) for p in args.answer_key],
                           "tasks": u["count"], "duplicates_same_label": u["duplicates_same_label"],
                           "excluded_conflicting": u["excluded_conflicting"], "arms": {}}
    for arm in ARMS:
        p = os.path.join(args.gen_dir, f"symptom-{arm}-{SYMPTOM_DAY}-gen.json")
        if not os.path.exists(p):
            continue
        gen = X.read_json(p)
        if not R.gen_complete(gen):
            raise Fail(f"incomplete gen symptom-{arm}")
        out["arms"][arm] = dict(symptom_score(gen["generations"], u["tasks"]), **R.gen_cost(gen),
                                prompt_text_sha256=gen["prompt_text_sha256"], table_sha256=gen.get("table_sha256"))
    sha = R.write_private(args.out, out)
    print(json.dumps({"tasks": out["tasks"], "dup": u["duplicates_same_label"], "excluded": u["excluded_conflicting"],
                      "arms": {a: {k: v[k] for k in ("done_ratio_mean", "open_items_mean", "majority_open",
                                                       "invalid", "cost_usd")} for a, v in out["arms"].items()}},
                     ensure_ascii=False))
    print(json.dumps({"out_sha256": sha}))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    from bench import r5_tune as T
    ap = argparse.ArgumentParser(prog="r5_endtoend.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sealed-plan")
    for a in ("--labelset", "--families", "--instruction", "--project-template", "--out"):
        p.add_argument(a, required=True)
    p = sub.add_parser("recall")
    p.add_argument("--stage", required=True, choices=STAGES)
    p.add_argument("--arm", required=True, choices=ARMS)
    p.add_argument("--plan")
    p.add_argument("--t", action="append")
    p.add_argument("--symptom-file")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--work", required=True)
    p = sub.add_parser("dllm")
    p.add_argument("--stage", required=True, choices=STAGES)
    p.add_argument("--arm", required=True, choices=ARMS)
    p.add_argument("--recalls", required=True)
    p.add_argument("--t", action="append")
    p.add_argument("--k", type=int, default=7)
    p.add_argument("--gen-dir", required=True)
    p.add_argument("--spent-dir", required=True)
    p.add_argument("--max-usd", type=float, required=True)
    p.add_argument("--soft-usd", type=float, required=True)
    p.add_argument("--max-input-chars", type=int, default=200000)
    p.add_argument("--work", required=True)
    p = sub.add_parser("audit")
    p.add_argument("--log", required=True)
    p.add_argument("--windows", required=True)
    p.add_argument("--knobs-sha256", required=True)
    p.add_argument("--out", required=True)
    p = sub.add_parser("bodies")
    for a in ("--meta", "--labelset", "--bank", "--out", "--work"):
        p.add_argument(a, required=True)
    p = sub.add_parser("judge-dev")
    T._common(p, judge=True)
    p.add_argument("--gen-dir", required=True)
    p.add_argument("--label", default="dev-on")
    p.add_argument("--s3-recalls", required=True)
    p.add_argument("--base-gen-dir", required=True)
    p.add_argument("--base", default="base1")
    p.add_argument("--reasons")
    p.add_argument("--out", required=True)
    for name in ("judge-sealed", "cands-sealed"):
        p = sub.add_parser(name)
        for a in ("--labelset", "--aliases", "--store", "--recalls-on", "--recalls-off", "--gen-dir", "--out", "--work"):
            p.add_argument(a, required=True)
        p.add_argument("--policy")
        p.add_argument("--reasons")
        p.add_argument("--max-input-chars", type=int, default=200000)
        if name == "cands-sealed":
            p.add_argument("--bodies", required=True)
            p.add_argument("--meta", required=True)
    p = sub.add_parser("merge-reasons")
    for a in ("--labelset", "--store", "--base", "--new-reads", "--out"):
        p.add_argument(a, required=True)
    p = sub.add_parser("symptom")
    p.add_argument("--gen-dir", required=True)
    p.add_argument("--answer-key", action="append", required=True)
    p.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    cmds = {"sealed-plan": cmd_sealed_plan, "recall": cmd_recall, "dllm": cmd_dllm, "audit": cmd_audit,
            "bodies": cmd_bodies, "judge-dev": cmd_judge_dev, "judge-sealed": cmd_judge_sealed,
            "cands-sealed": cmd_cands_sealed, "merge-reasons": cmd_merge_reasons, "symptom": cmd_symptom}
    try:
        return cmds[args.cmd](args)
    except (Fail, X.Fail, OT.StoreError) as e:
        print(f"fail: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - r5_metrics.Fail·r5_tune.Fail 등 하위 모듈 거부
        if type(e).__name__ != "Fail":
            raise
        print(f"fail: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
