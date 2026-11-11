"""런 6 종단 재확인(R6-2, issue #388, 상위 #386). R6-1(#387) 머지 코드의 S3 를 D10 사본 같은 세션 짝(켬·끔)으로 잰다.

  r6_endtoend.py knobs  --out <json> [--c7-header-file <txt>]           (손잡이 파일, 정규 sha256 대조)
  r6_endtoend.py audit  --log <gateway.log> --windows <json> --knobs-sha256 <64자> [--aliases <json>] --out <json>
  r6_endtoend.py judge-pair (r5_tune 판정 인자) --on-gen-dir --on-label --on-recalls --off-gen-dir --off-label
                        --off-recalls --base-gen-dir [--base base1] [--c1-recalls <dir>] [--reasons] --out
  r6_endtoend.py c7-verdict --judge <c7 r1 json> --judge <c7 r2 json> --ref <측정 1 json>

recall·d-llm·원래 증상 채점은 `bench/r5_endtoend.py`(`recall`·`dllm`·`symptom`)를 그대로 쓴다. 그 하네스는 payload 표
객체를 모양과 무관하게 저장하고, d-llm 입력은 #365·#366 규칙(저장 표 `text` 를 질의 순으로 같은 텍스트 1회 이어 recall 행
뒤)이다(코디네이터 결정 plan-review 20261102-132427: 소비자가 여러 recall 을 받는 실제 모양). 이 모듈은 R6-1 이 바꾼 것만 더한다.
  * payload 모양: `{t, fmt, keys, truncated, text}`. 열린 키는 `keys`(런 5 모양 `rows` 도 읽는다, `table_keys`).
  * 감사 줄 판정(코디네이터 결정): S3 켬 측정마다 `attached` 수 = 상태 어휘가 있고 결과에 키가 있는 recall 수,
    `attached` + `not_attached` 사유별 수 = 전체 recall 수(사유 = `no-status-term` | `no-result-keys`). 런 5 `audit_ok`
    (전부 attached 요구)는 쓰지 않는다(`audit_check`).
  * 짝 판정: 같은 사본 세션의 켬·끔 d-llm(K=7) 지표, 켬 - 끔 짝 차(키 군집 재표집, 씨앗 20261101, 2000회), 두 팔 각각의
    base1 대비 짝 차. 추가 토큰은 (i) 이은 표 텍스트 / d-llm 입력, (ii) 질의당 payload 최대 / d-llm 입력 두 가지.
    새 범위에서 C1(런 5 저장 표) 대비 빠진 키가 R 에 주는 영향을 R 누락의 표 안·밖(빠진 키)으로 따로 센다.
  * C7(선택): 머리에 지시 한 문장을 더한 손잡이(원문 리포 밖, 텍스트 sha256 고정 `C7_HEADER_SHA256`).
원문(질의·응답·표 텍스트·d-llm 출력)은 리포 밖 0600 파일에만 쓴다. 표준 출력에는 수치·sha256 만 낸다.
기존 bench 모듈(`r5_*`, 동결 파일)은 import 만 하고 바꾸지 않는다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from bench import r5_endtoend as E  # noqa: E402
from bench import r5_metrics as R  # noqa: E402
from bench import r5_roles as X  # noqa: E402
from gateway import item_open_table as OT  # noqa: E402

DEFAULT_KNOBS_SHA256 = "0ca334da8e68e695b1248da30df7616e7818c97ead3041e4d8cb9026c01883b7"   # R6-1 산출물 값
C7_HEADER_SHA256 = "9155cd04ccf246252113ebecd2be77c7a04c7d809ae5e9311c5160b35e11db58"      # 끝 줄바꿈 제거 텍스트
ATTACHED, NOT_ATTACHED = "attached", "not-attached"
SUB_REASONS = (OT.NOT_ATTACHED_NO_TERM, OT.NOT_ATTACHED_NO_KEYS)
P_BANK_MIN_C7 = 0.95


class Fail(Exception):
    pass


# --- payload 표 읽기 ----------------------------------------------------------------------------------

def table_keys(tab: Mapping[str, Any]) -> List[str]:
    """payload 표 객체의 열린 키. R6-1 모양은 `keys`, 런 5 모양은 `rows` 의 열림 행."""
    if isinstance(tab.get("keys"), list):
        return [str(k) for k in tab["keys"]]
    if isinstance(tab.get("rows"), list):
        return [r[0] for r in tab["rows"] if len(r) > 1 and r[1] == OT.STATE_OPEN]
    raise Fail("open table has neither keys nor rows")


def doc_keys(doc: Mapping[str, Any]) -> Dict[str, Any]:
    """recall 저장 하나(한 t)의 표 키: 질의별 키 수, 합집합(d-llm 입력에 실리는 키)."""
    per_q: List[int] = []
    union: Set[str] = set()
    for r in sorted(doc.get("runs") or [], key=lambda x: x.get("q", 0)):
        tab = r.get("open_table")
        if not tab:
            continue
        ks = table_keys(tab)
        per_q.append(len(ks))
        union.update(ks)
    return {"union": union, "per_query": per_q}


# --- 감사 줄 ------------------------------------------------------------------------------------------

def audit_counts(lines: Iterable[str], start_utc: str, end_utc: str) -> Dict[str, Any]:
    """[start, end] UTC 구간의 decision item-open-table 줄. 런 5 집계에 `not_attached` 하위 사유 수를 더한다.
    하위 사유는 문자열(요소 1개) 또는 사유별 개수(요소 여럿)다. 하위 사유가 없는 not-attached 줄은 `none` 으로 센다."""
    lines = list(lines)
    base = E.audit_counts(lines, start_utc, end_utc)
    sub: Counter = Counter()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("decision") != OT.DECISION or not (start_utc <= (e.get("ts") or "") <= end_utc):
            continue
        if e.get("reason") != NOT_ATTACHED:
            continue
        na = (e.get("item_open_table") or {}).get("not_attached")
        if isinstance(na, str):
            sub[na] += 1
        elif isinstance(na, dict):
            for k, v in na.items():
                sub[k] += int(v)
        else:
            sub["none"] += 1
    return dict(base, not_attached=dict(sub))


def expected_attached(doc: Mapping[str, Any], instruction: Optional[str], knobs: Mapping[str, Any],
                      aliases: Mapping[str, str]) -> Dict[str, int]:
    """저장 recall 의 질의 인자(지시문 + 문안, tags 없음)와 결과 행으로 게이트웨이 판정을 다시 한다:
    상태 어휘가 있고(`wants_table`) 범위가 붙일 수 있으면(`table_scope` != no-result-keys) attached."""
    plan_q = doc.get("_queries")
    if plan_q is None:
        raise Fail("recall doc has no _queries (attach the plan queries first)")
    out: Counter = Counter()
    by_q = {q["q"]: q for q in plan_q}
    for r in doc.get("runs") or []:
        q = by_q[r["q"]]
        args = E.query_args(instruction, q["text"], doc.get("query_timestamp") or "", 0)
        if not OT.wants_table(args, knobs):
            out[OT.NOT_ATTACHED_NO_TERM] += 1
            continue
        _, source = OT.table_scope(args, r.get("results") or [], knobs, aliases)
        out[OT.NOT_ATTACHED_NO_KEYS if source == OT.NOT_ATTACHED_NO_KEYS else ATTACHED] += 1
    return dict(out)


def audit_check(c: Mapping[str, Any], recalls: int, knobs_sha: str, *, saved_tables: Optional[int] = None,
                expected: Optional[Mapping[str, int]] = None) -> Dict[str, Any]:
    """S3 켬 측정 판정(코디네이터 결정). 실패 항목 목록이 비면 ok.
    * 감사 줄 수 = recall 수, 사유는 attached / not-attached 뿐(fail-open·bank-unset·other-bank 0)
    * attached + not_attached 하위 사유(no-status-term | no-result-keys) 합 = recall 수(하위 사유 없는 줄 0)
    * attached = 저장 payload 표가 있는 recall 수(주면), = 다시 한 게이트웨이 판정(주면, 하위 사유별로도 같음)
    * knobs_source file|env, knobs_sha256 하나 = 기대값, 경고·aliases_mismatch 0"""
    bad: List[str] = []
    reason = dict(c.get("reason") or {})
    att = reason.get(ATTACHED, 0)
    sub = dict(c.get("not_attached") or {})
    if c.get("lines") != recalls:
        bad.append(f"lines {c.get('lines')} != recalls {recalls}")
    other = {k: v for k, v in reason.items() if k not in (ATTACHED, NOT_ATTACHED)}
    if other:
        bad.append(f"other reasons {sorted(other)}")
    if set(sub) - set(SUB_REASONS):
        bad.append(f"unknown not_attached {sorted(set(sub) - set(SUB_REASONS))}")
    if att + sum(sub.get(k, 0) for k in SUB_REASONS) != recalls:
        bad.append("attached + not_attached != recalls")
    if saved_tables is not None and att != saved_tables:
        bad.append(f"attached {att} != saved tables {saved_tables}")
    if expected is not None:
        want = {ATTACHED: expected.get(ATTACHED, 0), **{k: expected.get(k, 0) for k in SUB_REASONS}}
        got = {ATTACHED: att, **{k: sub.get(k, 0) for k in SUB_REASONS}}
        if want != got:
            bad.append(f"audit {got} != expected {want}")
    if not set(c.get("knobs_source") or {}) <= {"file", "env"}:
        bad.append(f"knobs_source {sorted(c.get('knobs_source') or {})}")
    if att and set(c.get("knobs_sha256") or {}) != {knobs_sha}:
        bad.append("knobs_sha256 differs")
    if c.get("knob_warnings") or c.get("aliases_mismatch"):
        bad.append("knob warnings or aliases mismatch")
    return {"ok": not bad, "fail": bad}


# --- 토큰 ---------------------------------------------------------------------------------------------

def tokens(D, docs: Mapping[str, Mapping[str, Any]], ts: Sequence[str], max_chars: int, counter=None) -> Dict[str, Any]:
    """추가 토큰 두 가지(코디네이터 결정): (i) 이은 표 텍스트(d-llm 입력에 실리는 것) / 필터 뒤 d-llm 입력 행 토큰,
    (ii) 질의당 payload 표 객체(`"gateway_item_open_table":` 제외) 최대 / 같은 분모."""
    from bench import r5_tune as T
    counter = counter or D.tiktoken_counter()
    if counter is None:
        raise Fail("tiktoken is required (uv run --with tiktoken)")
    per = {}
    for t in ts:
        rows, _ = R.t_input(docs[t], t)
        asm = D.assemble({"runs": [{"q": 1, "max_tokens": 0, "results": rows}]}, max_input_chars=max_chars)
        den = counter("\n".join(D.render_row(r) for r in asm["rows"]))
        tab = E.tables_of(docs[t])
        tt = counter(tab["text"]) if tab["text"] else 0
        payloads = [counter(T.payload_json(r["open_table"])) for r in docs[t].get("runs") or [] if r.get("open_table")]
        pt = max(payloads, default=0)
        k = doc_keys(docs[t])
        per[t] = {"table_tokens": tt, "payload_tokens_max": pt, "input_tokens": den,
                  "ratio": tt / den if den else 0.0, "payload_ratio": pt / den if den else 0.0,
                  "texts": len(tab["texts"]), "attached": tab["attached"], "union_keys": len(k["union"]),
                  "keys_per_query": [min(k["per_query"], default=0), max(k["per_query"], default=0)]}
    return {"per_t": per, "ratio_max": max((v["ratio"] for v in per.values()), default=0.0),
            "payload_ratio_max": max((v["payload_ratio"] for v in per.values()), default=0.0)}


# --- 짝 집계 ------------------------------------------------------------------------------------------

def arm_metrics(st: Mapping[Tuple[str, str], str], jc, reasons: Mapping[Tuple[str, str, str], str]) -> Dict[str, Any]:
    """한 팔의 지표: P·R·R_all, 사유가 다 있으면 P_bank·R_bank2, R_bank(S4·#374 분모), G1, 사유 없는 오답 쌍."""
    errs = X.error_pairs(st, jc.spos, jc.sctl, jc.present)
    missing = [(s, t, X.key_code(k)) for s in X.SIDES for (k, t) in errs[s] if (t, X.key_code(k), s) not in reasons]
    adj = X.bank_adjusted(st, jc.spos, jc.sctl, jc.present, reasons) if not missing else None
    return {"pr": R.pr_summary(st, jc.spos, jc.sctl, jc.present), "adj": adj,
            "P_bank": adj["P_bank"] if adj else None, "P_bank_den": adj["P_bank_den"] if adj else None,
            "unreasoned": {"P": sum(1 for m in missing if m[0] == "P"), "R": sum(1 for m in missing if m[0] == "R")},
            "G1": R.dllm_rates(st, jc.spos, jc.present),
            "R_bank_s4": X.rbank_split(jc.s4, st, jc.sctl), "R_bank_374": X.rbank_split(jc.b374, st, jc.sctl),
            "errors": {s: sorted(errs[s]) for s in X.SIDES}}


def paired(a: Mapping[Tuple[str, str], str], b: Mapping[Tuple[str, str], str], jc) -> Dict[str, Any]:
    """a - b 짝 차(키 군집 재표집, 씨앗·횟수는 `r5_metrics` 기본 20261101·2000): R_bank(S4), R, P."""
    return {"R_bank_s4": R.cluster_bootstrap(X.rbank_values(a, jc.s4, jc.sctl), X.rbank_values(b, jc.s4, jc.sctl)),
            "R": R.cluster_bootstrap(R.pr_values(a, jc.spos, jc.sctl, jc.present)["R"],
                                     R.pr_values(b, jc.spos, jc.sctl, jc.present)["R"]),
            "P": R.cluster_bootstrap(R.pr_values(a, jc.spos, jc.sctl)["P"], R.pr_values(b, jc.spos, jc.sctl)["P"])}


def miss_split(r_misses: Iterable[Tuple[str, str]], union_by_t: Mapping[str, Set[str]],
               c1_by_t: Optional[Mapping[str, Set[str]]], reasons: Mapping[Tuple[str, str, str], str]) -> Dict[str, Any]:
    """S3 켬 R 누락(대조 쌍 (키, t))을 표 안·밖으로. 표 밖은 C1 표에는 있던 키(새 범위에서 빠진 키)와 C1 에도 없던 키로.
    표 안 누락은 사유별((iv) 소비자 생략 등)로 센다."""
    out: Counter = Counter()
    by_reason: Counter = Counter()
    for k, t in r_misses:
        if k in union_by_t.get(t, set()):
            out["in_table"] += 1
            by_reason[reasons.get((t, X.key_code(k), "R"), "unreasoned")] += 1
        elif c1_by_t is not None and k in c1_by_t.get(t, set()):
            out["out_of_scope_dropped"] += 1
        else:
            out["out_of_table"] += 1
    return {**{k: out.get(k, 0) for k in ("in_table", "out_of_scope_dropped", "out_of_table")},
            "in_table_by_reason": dict(by_reason)}


def dropped_controls(jc, union_by_t: Mapping[str, Set[str]], c1_by_t: Mapping[str, Set[str]]) -> Dict[str, Any]:
    """C1 대비 빠진 키 수와, 그중 R 분모(기준선 결과에 나온 대조)·R_bank(S4 열림) 분모에 드는 쌍 수."""
    dropped = {t: c1_by_t.get(t, set()) - union_by_t.get(t, set()) for t in union_by_t}
    extra = {t: union_by_t[t] - c1_by_t.get(t, set()) for t in union_by_t}
    in_r = sum(1 for k, t in jc.sctl & jc.present if k in dropped.get(t, set()))
    in_rb = sum(1 for (k, t) in jc.sctl if jc.s4.get((k, t)) == X.S_OPEN and k in dropped.get(t, set()))
    return {"dropped_keys": {t: len(v) for t, v in dropped.items()}, "extra_keys": {t: len(v) for t, v in extra.items()},
            "dropped_R_den_pairs": in_r, "dropped_R_bank_s4_den_pairs": in_rb}


def c7_verdict(c7: Sequence[Mapping[str, Any]], ref_rbank: float) -> Dict[str, Any]:
    """C7 자격: 두 회 모두 P_bank >= 95% 이고 R_bank(S4) 평균 >= 측정 1 S3 켬 값. 사유 없는 오답이 있으면 판정 불가."""
    pb = [r.get("P_bank") for r in c7]
    rb = [r.get("R_bank_s4") for r in c7]
    if len(c7) != 2:
        raise Fail("C7 verdict needs exactly two reps")
    if any(v is None for v in pb + rb):
        return {"verdict": "undecidable", "P_bank": pb, "R_bank_s4": rb}
    mean = sum(rb) / len(rb)
    ok = all(v >= P_BANK_MIN_C7 for v in pb) and mean >= ref_rbank
    return {"verdict": "replace-recommended" if ok else "keep-default", "P_bank": pb, "R_bank_s4": rb,
            "R_bank_s4_mean": mean, "ref_R_bank_s4": ref_rbank}


# --- 명령 ---------------------------------------------------------------------------------------------

def c7_header(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        text = f.read().rstrip("\n")
    if E.sha256_text(text) != C7_HEADER_SHA256:
        raise Fail("C7 header text sha256 differs from C7_HEADER_SHA256")
    if not OT._valid_header(text):
        raise Fail("C7 header is not a valid header template")
    return text


def knobs_values(c7_header_file: Optional[str] = None) -> Dict[str, Any]:
    # 기본(비 C7) 경로는 R6-2 기록값(DEFAULT_KNOBS_SHA256)을 재현해야 한다. R6-3(#391)에서 게이트웨이 기본 머리가 C7 로
    # 바뀌었으므로 R6-1 머리를 명시한다. C7 경로는 새 게이트웨이 기본값과 같다.
    values = OT._defaults()
    values["header"] = c7_header(c7_header_file) if c7_header_file else OT.HEADER_R6_1
    return values


def cmd_knobs(args) -> int:
    """손잡이 파일(리포 밖 0600). 기본값은 R6-1 기본 손잡이를 명시한 것(정규 sha256 = DEFAULT_KNOBS_SHA256)."""
    values = knobs_values(args.c7_header_file)
    parsed, warnings = OT.parse_knobs(json.dumps(values, ensure_ascii=False))
    canon = OT._canon_sha256(parsed)
    if warnings:
        raise Fail(f"knobs warnings {warnings}")
    if not args.c7_header_file and canon != DEFAULT_KNOBS_SHA256:
        raise Fail("default knobs canonical sha256 differs from the R6-1 value")
    sha = X.write_private_json(args.out, values)
    print(json.dumps({"file_sha256": sha, "canonical_sha256": canon, "c7": bool(args.c7_header_file),
                      "compact": len(json.dumps(values, ensure_ascii=False, separators=(",", ":")))}))
    return 0


def cmd_audit(args) -> int:
    """windows: {이름: {start_utc, end_utc, recalls?, arm?, recalls_dir?, plan?, knobs_sha256?}}. arm on 이면 판정.
    recalls_dir(+ plan 또는 symptom 질의)이 있으면 저장 표 수와 다시 한 게이트웨이 판정까지 대조한다."""
    windows = X.read_json(args.windows)
    aliases = R.aliases_from(args) if args.aliases else {}
    with open(args.log, encoding="utf-8") as f:
        lines = f.readlines()
    out: Dict[str, Any] = {"log_lines": len(lines), "windows": {}}
    for name, w in windows.items():
        c = audit_counts(lines, w["start_utc"], w["end_utc"])
        if "recalls" in w:
            c["recalls"] = w["recalls"]
        if w.get("arm") == "on":
            saved = expected = None
            if w.get("recalls_dir"):
                saved, expected = _saved_and_expected(w, aliases)
                c["saved_tables"], c["expected"] = saved, expected
            c["check"] = audit_check(c, w["recalls"], w.get("knobs_sha256") or args.knobs_sha256,
                                     saved_tables=saved, expected=expected)
        out["windows"][name] = dict(c, **{k: w[k] for k in ("start_utc", "end_utc")})
    sha = R.write_private(args.out, out)
    print(json.dumps(out["windows"], ensure_ascii=False))
    print(json.dumps({"out_sha256": sha, "all_ok": all(v["check"]["ok"] for v in out["windows"].values()
                                                        if "check" in v)}))
    return 0


def _saved_and_expected(w: Mapping[str, Any], aliases: Mapping[str, str]) -> Tuple[int, Dict[str, int]]:
    knobs = OT._defaults() if not w.get("knobs_file") else OT.parse_knobs(open(w["knobs_file"], encoding="utf-8").read())[0]
    saved = 0
    exp: Counter = Counter()
    names = sorted(n for n in os.listdir(w["recalls_dir"]) if n.startswith("t-") and n.endswith(".json"))
    plan = X.read_json(w["plan"]) if w.get("plan") else None
    for n in names:
        doc = X.read_json(os.path.join(w["recalls_dir"], n))
        saved += sum(1 for r in doc.get("runs") or [] if r.get("open_table"))
        if plan is not None:
            spec = plan["per_t"][doc["t"]]
            instr, queries = spec["instruction"], spec["queries"]
        else:
            with open(w["symptom_file"], encoding="utf-8") as f:
                instr, queries = None, [{"text": f.read().strip()}]
        d = dict(doc, _queries=[dict(q, q=i) for i, q in enumerate(queries, 1)])
        exp.update(expected_attached(d, instr, knobs, aliases))
    return saved, dict(exp)


def cmd_judge_pair(args) -> int:
    from bench import d_llm as D
    from bench import r5_tune as T
    jc = T.JudgeCtx(args)
    on_docs = R.load_recalls(args.on_recalls, jc.ts)
    off_docs = R.load_recalls(args.off_recalls, jc.ts)
    for docs, gdir, lab in ((on_docs, args.on_gen_dir, args.on_label), (off_docs, args.off_gen_dir, args.off_label)):
        for t in jc.ts:
            gen = X.read_json(os.path.join(gdir, f"{lab}-{t}-gen.json"))
            E.check_gen_table(gen, docs[t], E.build_input(D, jc.template, docs[t], t, args.max_input_chars)["prompt"])
    reasons = {}
    if args.reasons:
        T.check_reasons_store(args.reasons, jc.store_sha)
        reasons = X.reason_index(X.load_reasons(args.reasons))
    st_on, info_on = R.judge_label(args.on_gen_dir, args.on_label, jc.ts, jc.spos, jc.sctl, jc.aliases)
    st_off, info_off = R.judge_label(args.off_gen_dir, args.off_label, jc.ts, jc.spos, jc.sctl, jc.aliases)
    st_b1, _ = R.judge_label(args.base_gen_dir, args.base, jc.ts, jc.spos, jc.sctl, jc.aliases)
    m_on, m_off, m_b1 = arm_metrics(st_on, jc, reasons), arm_metrics(st_off, jc, reasons), arm_metrics(st_b1, jc, reasons)
    union_by_t = {t: doc_keys(on_docs[t])["union"] for t in jc.ts}
    c1_by_t = {t: E.open_keys(d) for t, d in R.load_recalls(args.c1_recalls, jc.ts).items()} if args.c1_recalls else None
    rows_same = {t: [r["id"] for r in R.t_input(on_docs[t], t)[0]] == [r["id"] for r in R.t_input(off_docs[t], t)[0]]
                 for t in jc.ts}
    out = {"t": jc.ts, "store_sha256": jc.store_sha, "reasons_sha256": X.sha256_file(args.reasons) if args.reasons else None,
           "labels": {"on": [args.on_gen_dir, args.on_label], "off": [args.off_gen_dir, args.off_label]},
           "input_rows_same_on_off": rows_same,
           "on": {k: v for k, v in m_on.items() if k != "errors"}, "off": {k: v for k, v in m_off.items() if k != "errors"},
           "base1": {k: v for k, v in m_b1.items() if k != "errors"},
           "paired_on_minus_off": paired(st_on, st_off, jc), "paired_on_vs_base1": paired(st_on, st_b1, jc),
           "paired_off_vs_base1": paired(st_off, st_b1, jc),
           "r_miss_split_on": miss_split(m_on["errors"]["R"], union_by_t, c1_by_t, reasons),
           "r_miss_split_off": miss_split(m_off["errors"]["R"], union_by_t, c1_by_t, reasons),
           "dropped_vs_c1": dropped_controls(jc, union_by_t, c1_by_t) if c1_by_t is not None else None,
           "tokens_on": tokens(D, on_docs, jc.ts, args.max_input_chars),
           "cost": {"on": info_on["cost"], "off": info_off["cost"]},
           "invalid_reps": {"on": {t: info_on["per_t"][t]["invalid_reps"] for t in jc.ts},
                            "off": {t: info_off["per_t"][t]["invalid_reps"] for t in jc.ts}}}
    sha = R.write_private(args.out, out)

    def brief_arm(m):
        return {"P": m["pr"]["P"], "P_bank": m["P_bank"], "R": m["pr"]["R"], "R_all": m["pr"]["R_all"],
                "R_bank_s4": m["R_bank_s4"]["R_bank"], "R_bank_374": m["R_bank_374"]["R_bank"],
                "G1": m["G1"]["rate"], "unreasoned": m["unreasoned"]}
    brief = {"on": brief_arm(m_on), "off": brief_arm(m_off), "base1": brief_arm(m_b1),
             "on_minus_off": {k: {x: v.get(x) for x in ("diff", "lo", "hi")}
                              for k, v in out["paired_on_minus_off"].items()},
             "rows_same": all(rows_same.values()), "r_miss_split_on": out["r_miss_split_on"],
             "dropped_vs_c1": out["dropped_vs_c1"],
             "tokens": [round(out["tokens_on"]["ratio_max"], 4), round(out["tokens_on"]["payload_ratio_max"], 4)],
             "cost": [info_on["cost"].get("cost_usd"), info_off["cost"].get("cost_usd")]}
    print(json.dumps(brief, ensure_ascii=False, default=str))
    print(json.dumps({"out_sha256": sha}))
    return 0


def cmd_c7_verdict(args) -> int:
    reps = []
    for p in args.judge:
        j = X.read_json(p)
        reps.append({"P_bank": j["on"]["P_bank"], "R_bank_s4": j["on"]["R_bank_s4"]["R_bank"]})
    ref = X.read_json(args.ref)["on"]["R_bank_s4"]["R_bank"]
    print(json.dumps(c7_verdict(reps, ref)))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    from bench import r5_tune as T
    ap = argparse.ArgumentParser(prog="r6_endtoend.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("knobs")
    p.add_argument("--out", required=True)
    p.add_argument("--c7-header-file")
    p = sub.add_parser("audit")
    p.add_argument("--log", required=True)
    p.add_argument("--windows", required=True)
    p.add_argument("--knobs-sha256", default=DEFAULT_KNOBS_SHA256)
    p.add_argument("--aliases")
    p.add_argument("--policy")
    p.add_argument("--out", required=True)
    p = sub.add_parser("judge-pair")
    T._common(p, judge=True)
    for a in ("--on-gen-dir", "--on-recalls", "--off-gen-dir", "--off-recalls", "--base-gen-dir", "--out"):
        p.add_argument(a, required=True)
    p.add_argument("--on-label", default="dev-on")
    p.add_argument("--off-label", default="dev-off")
    p.add_argument("--base", default="base1")
    p.add_argument("--c1-recalls")
    p.add_argument("--reasons")
    p = sub.add_parser("c7-verdict")
    p.add_argument("--judge", action="append", required=True)
    p.add_argument("--ref", required=True)
    args = ap.parse_args(argv)
    cmds = {"knobs": cmd_knobs, "audit": cmd_audit, "judge-pair": cmd_judge_pair, "c7-verdict": cmd_c7_verdict}
    try:
        return cmds[args.cmd](args)
    except (Fail, E.Fail, X.Fail, OT.StoreError) as e:
        print(f"fail: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - r5_metrics.Fail·r5_tune.Fail 등 하위 모듈 거부
        if type(e).__name__ != "Fail":
            raise
        print(f"fail: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
