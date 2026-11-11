"""런 6 R6-1 오프라인 payload 판정(issue #387, 상위 #386). 지출 0, 사본 0, LLM 호출 0.

  r6_payload.py --store <sqlite> --aliases <json> --bench-run <run 디렉터리> --bench-corpus <dev.jsonl>
                --bench-compare <compare-<run>.json>
                --plan <r5-a2-plan.json> --dev-recalls <rec-dev-on 디렉터리> --symptom-recall <rec-symptom-on 디렉터리>
                --symptom-file <txt> --out <json>

S3(`gateway/item_open_table`)의 새 기본 손잡이(attach status_query, scope request_or_result_project, payload
`{t, fmt, keys, truncated, text}`)를 런 5 저장물에 그대로 적용해 세 가지를 잰다. 게이트웨이 공개 함수(`wants_table`,
`table_scope`, `role_states`, `build_table`, `payload_table`)만 부르고 규칙을 복사하지 않는다.

  1. 벤치: 런 5 G3 run 의 cand 질의(벤치 dev 분할) 인자(`--bench-corpus` 의 `args`, `query_timestamp` 가 없으면 run
     meta `bench_clock`, runner 와 같다)와 cand 저장 결과 행으로 질의마다 부착 여부·사유·표 키 수, 붙으면 추가 토큰
     (payload 기준 = `"gateway_item_open_table":` + 표 객체 압축 JSON). 토큰은 `cl100k_base`(런 5 산출물과 같은 셈)와
     `o200k_base`(벤치 runner 응답 토큰과 같은 셈). G3 추정 = 붙은 질의 추가 토큰(o200k) 합 / base 응답 토큰 합(compare
     기록값. `arm_compare` 는 질의마다 라운드 중앙값을 내서 질의 합을 낸다). 보정: 런 5 표 객체(저장된 cand payload)로 같은
     셈을 한 추정이 compare 기록 증가율과 맞는지 본다.
  2. dev 상태 질의: R5-A2 계획의 dev t(저장된 `rec-dev-on` 의 t, 봉인 아님)마다 질의 인자 `지시문 + 줄바꿈 + 문안`
     (태그 없음, `r5_tune.query_args` 와 같다)과 저장 결과 행으로 부착률, 새 범위 표 키 집합(그 t 에서 붙은 질의 표 키의
     합집합 = d-llm 입력에 실리는 키)과 런 5 C1 저장 표(같은 t, 뱅크 전체) 키 집합의 키 수·자카드·빠진 키 수, 추가 토큰
     (표 텍스트 = 그 t 의 서로 다른 표 텍스트를 줄바꿈으로 이은 것, payload = 질의당 표 객체). 문안만(지시문 없이)의
     부착률도 함께 낸다(지시문이 상태 낱말을 담아 부착률을 혼자 채우는지 보려는 것).
  3. 원래 증상: 저장된 `rec-symptom-on` 질의(문안 그대로, 태그 없음)의 부착 여부·표 키 수·토큰과 런 5 표 키 수.

일관성 확인: 같은 저장소·기준 시각으로 scope all 표를 다시 만들어 런 5 저장 표 키 집합과 같은지(`c1_reproduced`) 본다.
봉인 저장물(`sealed` 가 든 경로)은 열지 않는다(exit 2). 출력 파일(0600)과 표준 출력에는 수치·sha256·분류 코드만 둔다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from gateway import item_open_table as OT  # noqa: E402
from gateway import item_status  # noqa: E402
from gateway import recall_prune  # noqa: E402

Counter = Callable[[str], int]
FIELD_PREFIX = json.dumps(OT.FIELD) + ":"
SYMPTOM_TS = "2026-10-28T14:59:59Z"            # r5_endtoend.SYMPTOM_TS(= d_llm.R41_QUERY_TIMESTAMP)


class Fail(Exception):
    pass


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def refuse_sealed(*paths: Optional[str]) -> None:
    """경로의 어느 성분이든 `sealed` 가 들어 있으면 거부한다(봉인 디렉터리 안 파일·하위 디렉터리 포함)."""
    for p in paths:
        if p and any("sealed" in part.lower() for part in os.path.normpath(os.path.abspath(p)).split(os.sep)):
            raise Fail("sealed inputs are out of scope")


def read_json(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def jaccard(a: Set[str], b: Set[str]) -> float:
    return 1.0 if not a and not b else len(a & b) / len(a | b)


def payload_tokens(obj: Mapping[str, Any], counter: Counter) -> int:
    """응답 payload 에 붙는 양: `"gateway_item_open_table":` + 표 객체 압축 JSON(게이트웨이 재직렬화와 같은 함수)."""
    return counter(FIELD_PREFIX + recall_prune._dumps(dict(obj)))


def _cutoff(raw: Any) -> datetime:
    at = item_status._instant(raw)
    if at is None:
        raise Fail("unreadable query_timestamp")
    return at


def decide(args: Mapping[str, Any], rows: Sequence[Any], mentions: Sequence[OT.Mention], knobs: Mapping[str, Any],
           aliases: Mapping[str, str]) -> Dict[str, Any]:
    """질의 하나에 게이트웨이가 하는 판단(`attach_mcp_body` 의 요소 단계와 같은 순서). 붙으면 `table` 은 payload 모양."""
    if not OT.wants_table(args, knobs):
        return {"attached": False, "reason": OT.NOT_ATTACHED_NO_TERM, "table": None}
    cutoff = _cutoff(args.get("query_timestamp"))
    repos, source = OT.table_scope(args, rows, knobs, aliases)
    if source == OT.NOT_ATTACHED_NO_KEYS:
        return {"attached": False, "reason": source, "table": None}
    built = OT.build_table(OT.role_states(mentions, cutoff), cutoff, repos, knobs)
    return {"attached": True, "reason": source, "table": OT.payload_table(built)}


def r6_1_knobs() -> Dict[str, Any]:
    """R6-1 기본 손잡이(정규 sha256 0ca334da...). #391 에서 게이트웨이 기본 머리가 C7 로 바뀌어 머리를 명시한다."""
    return {**OT._defaults(), "header": OT.HEADER_R6_1}


def _all_keys(mentions: Sequence[OT.Mention], cutoff_raw: str) -> Set[str]:
    cutoff = _cutoff(cutoff_raw)
    knobs = {**r6_1_knobs(), "scope": "all"}
    return {r[0] for r in OT.build_table(OT.role_states(mentions, cutoff), cutoff, None, knobs)["rows"]}


# --- 1. 벤치 -------------------------------------------------------------------------------------------

def bench_inputs(run_dir: str, corpus_path: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """cand 저장 결과(질의 key 하나에 파일 하나) + 코퍼스 인자. [{key, args, rows, old_table}]."""
    meta = read_json(os.path.join(run_dir, "meta.json"))
    corpus: Dict[str, Any] = {}
    with open(corpus_path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                corpus[r["key"]] = r
    out = []
    rdir = os.path.join(run_dir, "results")
    for name in sorted(os.listdir(rdir)):
        if not name.startswith("cand."):
            continue
        key = name.split(".")[1]
        if key in {o["key"] for o in out}:
            raise Fail("more than one cand result per query (nondeterministic run)")
        if key not in corpus:
            raise Fail("cand query missing from the corpus")
        doc = read_json(os.path.join(rdir, name))
        args = dict(corpus[key]["args"])
        args.setdefault("query_timestamp", meta["bench_clock"])
        out.append({"key": key, "args": args, "rows": doc.get("results") or [],
                    "old_table": (doc.get("payload_rest") or {}).get(OT.FIELD)})
    if len(out) != meta.get("n_queries"):
        raise Fail("cand result count differs from run meta n_queries")
    return out, meta


def bench_report(items: Sequence[Mapping[str, Any]], mentions: Sequence[OT.Mention], knobs: Mapping[str, Any],
                 aliases: Mapping[str, str], cl100k: Counter, o200k: Counter,
                 compare: Mapping[str, Any]) -> Dict[str, Any]:
    per = []
    for it in items:
        d = decide(it["args"], it["rows"], mentions, knobs, aliases)
        old = it["old_table"]
        per.append({
            "attached": d["attached"], "reason": d["reason"], "tagged": bool(OT.request_repos(it["args"].get("tags"),
                                                                                              aliases)),
            "keys": len(d["table"]["keys"]) if d["table"] else 0,
            "add_cl100k": payload_tokens(d["table"], cl100k) if d["table"] else 0,
            "add_o200k": payload_tokens(d["table"], o200k) if d["table"] else 0,
            "old_o200k": payload_tokens(old, o200k) if old else 0,
            "old_rows": len(old["rows"]) if old else 0,
        })
    n = len(per)
    attached = [p for p in per if p["attached"]]
    reasons: Dict[str, int] = {}
    for p in per:
        reasons[p["reason"]] = reasons.get(p["reason"], 0) + 1
    base_total = (compare.get("tokens") or {}).get("base_total")
    old_delta = sorted(p["old_o200k"] for p in per)
    added_o200k = sum(p["add_o200k"] for p in attached)
    term_hits: Dict[str, int] = {}
    for it in items:
        q = str(it["args"].get("query") or "").lower()
        for term in knobs["status_terms"]:
            if term.strip().lower() in q:
                term_hits[term] = term_hits.get(term, 0) + 1
    return {
        "queries": n, "attached": len(attached), "attach_rate": len(attached) / n if n else None,
        "reasons": dict(sorted(reasons.items())), "tagged_queries": sum(p["tagged"] for p in per),
        "term_hits": dict(sorted(term_hits.items())),
        "attached_keys": sorted(p["keys"] for p in attached),
        "added_cl100k_per_attached": sorted(p["add_cl100k"] for p in attached),
        "added_o200k_per_attached": sorted(p["add_o200k"] for p in attached),
        "added_cl100k_sum": sum(p["add_cl100k"] for p in attached),
        "added_o200k_sum": added_o200k,
        "base_total_o200k": base_total,
        "g3_estimate": added_o200k / base_total if base_total else None,
        "calibration": {"old_table_o200k_p50": old_delta[n // 2] if n else None,
                        "old_table_o200k_min": old_delta[0] if n else None,
                        "compare_per_query_delta": (compare.get("tokens") or {}).get("per_query_delta"),
                        "old_table_rows": sorted({p["old_rows"] for p in per}),
                        "old_estimate": sum(old_delta) / base_total if base_total else None,
                        "compare_rel_change": (compare.get("tokens") or {}).get("rel_change")},
    }


# --- 2. dev 상태 질의 ----------------------------------------------------------------------------------

def dev_t(spec: Mapping[str, Any], doc: Mapping[str, Any], mentions: Sequence[OT.Mention], knobs: Mapping[str, Any],
          aliases: Mapping[str, str], counter: Counter, denominator: Optional[int]) -> Dict[str, Any]:
    """한 t: 질의 인자 재구성(`r5_tune.query_args`) + 저장 결과 행 -> 부착·키 집합·토큰을 C1 저장 표와 비교."""
    runs = sorted(doc.get("runs") or [], key=lambda r: r.get("q", 0))
    if [q.get("qid") for q in spec["queries"]] != [r.get("qid") for r in runs]:
        raise Fail("stored recall query order differs from the plan")
    if doc.get("query_timestamp") != spec["query_timestamp"]:
        raise Fail("stored recall query_timestamp differs from the plan")
    instruction = spec["instruction"].strip()
    attached = text_only = 0
    union: Set[str] = set()
    texts: List[str] = []
    per_query_keys: List[int] = []
    per_query_jac: List[float] = []
    payload_new: List[int] = []
    c1_keys: Set[str] = set()
    c1_texts: List[str] = []
    payload_c1: List[int] = []
    reasons: Dict[str, int] = {}
    for q, r in zip(spec["queries"], runs):
        args = {"query": f"{instruction}\n{q['text'].strip()}", "query_timestamp": spec["query_timestamp"]}
        if OT.wants_table({"query": q["text"].strip()}, knobs):
            text_only += 1
        old = r.get("open_table")
        if old:
            c1_keys |= {row[0] for row in old["rows"]}
            if old["text"] not in c1_texts:
                c1_texts.append(old["text"])
            payload_c1.append(payload_tokens(old, counter))
        d = decide(args, r.get("results") or [], mentions, knobs, aliases)
        reasons[d["reason"]] = reasons.get(d["reason"], 0) + 1
        if not d["attached"]:
            continue
        attached += 1
        keys = set(d["table"]["keys"])
        union |= keys
        per_query_keys.append(len(keys))
        payload_new.append(payload_tokens(d["table"], counter))
        if d["table"]["text"] not in texts:
            texts.append(d["table"]["text"])
    for r in runs:
        if r.get("open_table"):
            per_query_jac.append(jaccard({x[0] for x in r["open_table"]["rows"]}, c1_keys))
    new_text_tok = counter("\n".join(texts)) if texts else 0
    c1_text_tok = counter("\n".join(c1_texts)) if c1_texts else 0
    rate = (lambda v: v / denominator if denominator else None)
    return {
        "queries": len(runs), "attached": attached, "text_only_attached": text_only,
        "reasons": dict(sorted(reasons.items())),
        "c1_keys": len(c1_keys), "new_keys": len(union), "jaccard": jaccard(union, c1_keys),
        "missing": len(c1_keys - union), "extra": len(union - c1_keys),
        "c1_reproduced": _all_keys(mentions, spec["query_timestamp"]) == c1_keys,
        "per_query_keys_min": min(per_query_keys) if per_query_keys else 0,
        "per_query_keys_max": max(per_query_keys) if per_query_keys else 0,
        "distinct_texts": len(texts), "c1_distinct_texts": len(c1_texts),
        "text_tokens": new_text_tok, "c1_text_tokens": c1_text_tok,
        "payload_tokens_max": max(payload_new) if payload_new else 0,
        "c1_payload_tokens_max": max(payload_c1) if payload_c1 else 0,
        "denominator": denominator, "text_rate": rate(new_text_tok), "c1_text_rate": rate(c1_text_tok),
        "payload_rate": rate(max(payload_new) if payload_new else 0),
        "c1_payload_rate": rate(max(payload_c1) if payload_c1 else 0),
        "c1_tables_consistent": len(c1_texts) <= 1 and all(j == 1.0 for j in per_query_jac),
    }


def dev_all(dev_dir: str, plan_sha: str, plan: Mapping[str, Any], mentions: Sequence[OT.Mention],
            knobs: Mapping[str, Any], aliases: Mapping[str, str], counter: Counter) -> Dict[str, Any]:
    """저장 dev recall 디렉터리 -> t 별 `dev_t`. dev t 6개(`DEV_DENOMINATORS`)만, 파일명 `t-<t>.json` 과 doc `t` 가 맞고,
    모두 이 계획으로 저장됐고, 6개가 다 있어야 한다(봉인·간격 t 거부, 빠진 t 로 조용히 끝나지 않게)."""
    dev: Dict[str, Any] = {}
    for name in sorted(os.listdir(dev_dir)):
        if not (name.startswith("t-") and name.endswith(".json")):
            continue
        doc = read_json(os.path.join(dev_dir, name))
        t = doc.get("t")
        if not isinstance(t, str) or t not in DEV_DENOMINATORS or name != f"t-{t}.json":
            raise Fail("stored recall t is not one of the dev t")
        if doc.get("plan_sha256") != plan_sha:
            raise Fail("stored dev recall was saved from another plan")
        dev[t] = dev_t(plan["per_t"][t], doc, mentions, knobs, aliases, counter, DEV_DENOMINATORS[t])
    if set(dev) != set(DEV_DENOMINATORS):
        raise Fail("stored dev recalls do not cover all dev t")
    return dev


# --- 3. 원래 증상 -------------------------------------------------------------------------------------

def symptom_report(doc: Mapping[str, Any], symptom_text: str, mentions: Sequence[OT.Mention],
                   knobs: Mapping[str, Any], aliases: Mapping[str, str], counter: Counter) -> Dict[str, Any]:
    if doc.get("symptom_sha256") != sha256_text(symptom_text):
        raise Fail("symptom text differs from the stored recall")
    (run,) = doc["runs"]
    args = {"query": symptom_text, "query_timestamp": doc.get("query_timestamp") or SYMPTOM_TS}
    d = decide(args, run.get("results") or [], mentions, knobs, aliases)
    old = run.get("open_table")
    old_keys = {r[0] for r in old["rows"]} if old else set()
    new_keys = set(d["table"]["keys"]) if d["table"] else set()
    return {"attached": d["attached"], "reason": d["reason"], "keys": len(new_keys), "c1_keys": len(old_keys),
            "jaccard": jaccard(new_keys, old_keys), "missing": len(old_keys - new_keys),
            "c1_reproduced": _all_keys(mentions, args["query_timestamp"]) == old_keys,
            "text_tokens": counter(d["table"]["text"]) if d["table"] else 0,
            "c1_text_tokens": counter(old["text"]) if old else 0,
            "payload_tokens": payload_tokens(d["table"], counter) if d["table"] else 0,
            "c1_payload_tokens": payload_tokens(old, counter) if old else 0}


# --- CLI ----------------------------------------------------------------------------------------------

# 런 5 R5-C(#365) `prep` 기록값: dev t 별 필터 뒤 d-llm 입력 토큰(cl100k, `artifacts/arch-v2/r5-c-tune.md` 3절).
DEV_DENOMINATORS = {"2026-09-28": 60525, "2026-10-01": 60683, "2026-10-04": 68067, "2026-10-07": 64070,
                    "2026-10-10": 64007, "2026-10-13": 76724}


def _encoders() -> Tuple[Counter, Counter]:
    try:
        import tiktoken
    except ImportError as exc:                                   # uv run --with tiktoken
        raise Fail("tiktoken is required (uv run --with tiktoken)") from exc
    cl, o2 = tiktoken.get_encoding("cl100k_base"), tiktoken.get_encoding("o200k_base")
    return (lambda s: len(cl.encode(s or ""))), (lambda s: len(o2.encode(s or "")))


def _aliases(path: str) -> Dict[str, str]:
    from bench import r5_labels as L
    exp, _ = L.load_alias_table(path)
    merged, _ = L.merged_aliases({}, exp)
    return merged


def _write_private(path: str, obj: Any) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.chmod(path, 0o600)


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for name in ("store", "aliases", "bench-run", "bench-corpus", "bench-compare", "plan", "dev-recalls", "symptom-recall",
                 "symptom-file", "out"):
        p.add_argument(f"--{name}", required=True)
    a = p.parse_args(argv)
    try:
        refuse_sealed(a.store, a.aliases, a.bench_run, a.bench_corpus, a.bench_compare, a.dev_recalls,
                      a.symptom_recall, a.plan, a.symptom_file)
        cl100k, o200k = _encoders()
        aliases = _aliases(a.aliases)
        store = OT.read_store(a.store, 30.0)
        if store.meta.get("aliases_sha256") != OT.aliases_sha256(aliases):
            raise Fail("alias table differs from the store meta aliases_sha256")
        knobs = r6_1_knobs()
        items, meta = bench_inputs(a.bench_run, a.bench_corpus)
        compare = read_json(a.bench_compare)
        if compare.get("run_id") != meta.get("run_id"):
            raise Fail("bench compare is from another run")
        bench = bench_report(items, store.mentions, knobs, aliases, cl100k, o200k, compare)
        dev = dev_all(a.dev_recalls, sha256_file(a.plan), read_json(a.plan), store.mentions, knobs, aliases, cl100k)
        sym_docs = [n for n in sorted(os.listdir(a.symptom_recall)) if n.endswith(".json")]
        if len(sym_docs) != 1:
            raise Fail("expected one stored symptom recall")
        with open(a.symptom_file, encoding="utf-8") as f:
            symptom_text = f.read().strip()
        symptom = symptom_report(read_json(os.path.join(a.symptom_recall, sym_docs[0])), symptom_text,
                                 store.mentions, knobs, aliases, cl100k)
    except (Fail, OT.StoreError, KeyError, ValueError) as exc:          # 입력 모양 오류도 exit 2(traceback 없이)
        print(f"r6_payload: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    out = {"knobs_sha256": OT.Knobs(knobs, "default").sha256, "store_sha256": sha256_file(a.store),
           "plan_sha256": sha256_file(a.plan), "aliases_sha256": OT.aliases_sha256(aliases),
           "gateway_sha256": sha256_file(os.path.join(REPO, "gateway", "item_open_table.py")),
           "bench_run": meta.get("run_id"), "bench_corpus_keys_sha256": meta.get("corpus_keys_sha256"),
           "bench": bench, "dev": dev, "symptom": symptom}
    _write_private(a.out, out)
    print(json.dumps(out, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
