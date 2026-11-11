"""d-llm 오프라인 목록 경로(issue #350, 상위 #349 "d-llm 경로").

저장된 recall 결과 행으로 LLM 목록을 K회 만들고(질의 0회), bench/d_llm_match.py 대응 규칙으로 판정한다.

  python3 -B -m bench.d_llm run   --input <저장 recall json> --out <리포 밖 디렉터리> --k 5 [--max-tokens N] [--label L]
  python3 -B -m bench.d_llm judge --gen <run 출력> --answer-key <정답표> [--expect-key-sha256 S] [--out <json>]
  python3 -B -m bench.d_llm blind --gen <run 출력> --out <판독 파일> --map <대응 파일> [--seed N]
  python3 -B -m bench.d_llm agree --map <대응 파일> --judge <judge 출력> --reader <판독1> --reader <판독2> [--answer-key ...]
  R4-3(issue #352) 처치: run [--add <저장 recall>] [--add-names <이름 recall> --add-names-cap N] [--inject F] [--drop-ids F]
  python3 -B -m bench.d_llm diag --set <세트 D> --gen <기준선 run> --done-rows <판독> --rows-from <저장 recall>... --out-inject F --out-drop F

산출물(LLM 출력 원문, 판독 파일, 판정 결과)은 기억 내용을 담으므로 리포 밖에 0600 으로만 쓴다. 표준 출력에는 id·수치·sha256 만 낸다.
LLM 호출: `claude -p --model claude-sonnet-5-5 --output-format json`, 도구·MCP·사용자 설정 없음(--safe-mode --tools "" --strict-mcp-config),
토큰은 자식 env 의 CLAUDE_CODE_OAUTH_TOKEN 에만 둔다(값은 출력·기록하지 않는다).
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from bench import d_llm_match as M
from bench import d_llm_stats as S
from bench.arch_v2_regression import item_keywords

HERE = os.path.dirname(os.path.abspath(__file__))
BASELINE_PROMPT = os.path.join(HERE, "prompts", "d_llm_baseline.txt")
MODEL = "claude-sonnet-5-5"
QUERY_DATE = "2026-10-28"
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
TOKEN_CMD = os.path.expanduser("~/.claude/demoproj02-token.sh")
CALL_TIMEOUT = 900
KST = dt.timezone(dt.timedelta(hours=9))
STATUSES = ("open", "done", "residual")
ROW_FIELDS = (("id", "id"), ("type", "fact_type"), ("mentioned_at", "mentioned_at"), ("tags", "tags"),
              ("context", "context"), ("text", "text"))


def sha256_file(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def now_kst() -> str:
    return dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


def write_private(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        if isinstance(obj, str):
            f.write(obj)
        else:
            json.dump(obj, f, ensure_ascii=False, indent=1)
    os.chmod(path, 0o600)


# ---------------------------------------------------------------- 입력 조립

def render_row(row: Dict[str, Any]) -> str:
    return json.dumps({k: row.get(src) for k, src in ROW_FIELDS}, ensure_ascii=False)


def assemble(stored: Dict[str, Any], max_tokens: Optional[int] = None,
             max_input_chars: Optional[int] = None) -> Dict[str, Any]:
    """질의 순(q, 같은 q 안에서는 max_tokens 오름차순) -> 순위 순, 기억 id 중복 제거(처음 나온 자리). 글자 상한을 넘는 뒤 행은 잘라 수를 센다."""
    runs = [r for r in stored.get("runs") or [] if max_tokens is None or r.get("max_tokens") == max_tokens]
    runs = sorted(runs, key=lambda r: (r.get("q", 0), r.get("max_tokens", 0)))
    seen, rows, source, no_id = set(), [], 0, 0
    for r in runs:
        for row in r.get("results") or []:
            source += 1
            if not row.get("id"):
                no_id += 1          # 기억 id 가 없으면 중복 제거 기준이 없어 넣지 않고 수만 센다
                continue
            if row.get("id") in seen:
                continue
            seen.add(row.get("id"))
            rows.append(row)
    kept, chars = [], 0
    for row in rows:
        n = len(render_row(row)) + 1
        if max_input_chars is not None and chars + n > max_input_chars:
            break
        kept.append(row)
        chars += n
    return {"rows": kept, "source_rows": source, "no_id_rows": no_id, "union_rows": len(rows),
            "truncated_rows": len(rows) - len(kept),
            "chars": chars, "recalls": [(r.get("q"), r.get("max_tokens")) for r in runs]}


def load_prompt(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def build_prompt(template: str, asm: Dict[str, Any], query_date: str = QUERY_DATE) -> str:
    return template.format(query_date=query_date, recall_rows="\n".join(render_row(r) for r in asm["rows"]))


# ---------------------------------------------------------------- 출력 검증

def _extract_json(text: str) -> Optional[Any]:
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    cands = [fence.group(1)] if fence else []
    cands.append(text)
    i, j = text.find("{"), text.rfind("}")
    if 0 <= i < j:
        cands.append(text[i:j + 1])
    for c in cands:
        try:
            return json.loads(c)
        except (ValueError, TypeError):
            continue
    return None


def validate_item(it: Any) -> Optional[str]:
    if not isinstance(it, dict):
        return "item-not-object"
    if not isinstance(it.get("name"), str) or not it["name"].strip():
        return "name"
    if it.get("status") not in STATUSES:
        return "status"
    if not isinstance(it.get("evidence_ids"), list) or not all(isinstance(x, str) for x in it["evidence_ids"]):
        return "evidence_ids"
    if not isinstance(it.get("quote"), str):
        return "quote"
    if "key" in it and it["key"] is not None and not isinstance(it["key"], str):
        return "key"
    if "unverified" in it and not isinstance(it["unverified"], bool):
        return "unverified"
    return None


def parse_output(text: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    doc = _extract_json(text)
    if not isinstance(doc, dict) or not isinstance(doc.get("items"), list):
        return [], "not-items-object"
    for n, it in enumerate(doc["items"]):
        err = validate_item(it)
        if err:
            return [], f"item{n}:{err}"
    return doc["items"], None


# ---------------------------------------------------------------- LLM 호출

def claude_caller(model: str = MODEL, timeout: int = CALL_TIMEOUT) -> Callable[[str], Dict[str, Any]]:
    """중첩 claude -p 호출기. 토큰은 자식 env 에만 넣고 어디에도 쓰지 않는다. 빈 임시 cwd 에서 돈다."""
    token = subprocess.run([TOKEN_CMD], capture_output=True, text=True, check=True).stdout.strip()
    env = dict(os.environ)
    env[TOKEN_ENV] = token
    cmd = ["claude", "-p", "--model", model, "--output-format", "json", "--safe-mode", "--tools", "",
           "--strict-mcp-config", "--no-session-persistence"]

    def call(prompt: str) -> Dict[str, Any]:
        with tempfile.TemporaryDirectory() as cwd:
            try:
                p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, env=env, cwd=cwd,
                                   timeout=timeout)
            except subprocess.TimeoutExpired:
                return {"is_error": True, "result": "", "error": f"timeout {timeout}s"}
        try:
            return json.loads(p.stdout)
        except ValueError:
            return {"is_error": True, "result": "", "error": f"exit={p.returncode} non-json stdout"}
    return call


def call_stats(raw: Dict[str, Any]) -> Dict[str, Any]:
    u = raw.get("usage") or {}
    return {"input_tokens": u.get("input_tokens"), "cache_creation_input_tokens": u.get("cache_creation_input_tokens"),
            "cache_read_input_tokens": u.get("cache_read_input_tokens"), "output_tokens": u.get("output_tokens"),
            "duration_ms": raw.get("duration_ms"), "cost_usd": raw.get("total_cost_usd"),
            "is_error": bool(raw.get("is_error")), "stop_reason": raw.get("stop_reason")}


def generate_one(prompt: str, caller: Callable[[str], Dict[str, Any]], attempts: int = 2) -> Dict[str, Any]:
    """한 반복: 스키마 실패면 1회 재시도, 그래도 실패면 invalid."""
    calls, err, items, text = [], None, [], ""
    for _ in range(attempts):
        started, t0 = now_kst(), time.monotonic()
        raw = caller(prompt)
        stats = dict(call_stats(raw), started_kst=started, wall_s=round(time.monotonic() - t0, 2))
        text = raw.get("result") or ""
        if raw.get("is_error"):
            items, err = [], "is_error"
        else:
            items, err = parse_output(text)
        stats["valid"] = err is None
        stats["error"] = err
        calls.append(dict(stats, raw_result=text))
        if err is None:
            break
    return {"valid": err is None, "attempts": len(calls), "error": err, "items": items, "calls": calls}


# ---------------------------------------------------------------- 채점

def tasks_from_answer_key(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """채점용 작업 표. keyword 는 판정기 v2 item_keywords(프로젝트 이름 keyword 제외). 항목 이름은 넣지 않는다."""
    out = []
    for cat in ("closed", "controls", "partial"):
        for it in doc.get(cat) or []:
            out.append({"id": it["id"], "keywords": item_keywords(it), "project_hint": it.get("project_hint"),
                        "category": cat})
    return out


def judge_summary(gens: Sequence[Dict[str, Any]], tasks: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    res = M.judge_runs(gens, tasks)
    cat = {t["id"]: t["category"] for t in tasks}
    closed = [t["id"] for t in tasks if t["category"] == "closed"]
    controls = [t["id"] for t in tasks if t["category"] == "controls"]
    k = res["K"]

    def per_run(ids, pred):
        return [sum(1 for i in ids if pred(res["tasks"][i]["states"][r])) for r in range(k)]
    judged = res["runs"]
    n_items = [len(g.get("items") or []) if g.get("valid") else 0 for g in gens]
    n_unv = [sum(1 for it in (g.get("items") or []) if it.get("unverified") is True) if g.get("valid") else 0
             for g in gens]
    return {
        "K": k, "invalid": sum(1 for g in gens if not g.get("valid")),
        "reproduction": M.reproduction(res, closed),
        "open_rate": {i: res["tasks"][i]["open"] / k for i in res["tasks"]},
        "states": {i: res["tasks"][i]["states"] for i in res["tasks"]},
        "category": cat,
        "closed_in_list": per_run(closed, lambda s: s in ("open", "done", "partial")),
        "closed_open": per_run(closed, lambda s: s == "open"),
        "controls_kept": per_run(controls, lambda s: s == "open"),
        "unmapped": [j["unmapped"] if j else 0 for j in judged],
        "unmapped_reasons": [j["unmapped_reasons"] if j else {} for j in judged],
        "items": n_items, "unverified_items": n_unv,
        "unverified_open_tasks": {i: res["tasks"][i]["unverified_open"] for i in res["tasks"]
                                  if res["tasks"][i]["unverified_open"]},
        "mapping": [[{"task": m["task"], "by": m["by"], "reason": m["reason"]} for m in j["mapping"]] if j else None
                    for j in judged],
    }


# ---------------------------------------------------------------- 판독 파일과 일치율

def blind_file(gen: Dict[str, Any], seed: int) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """K회 출력의 목록 항목 전체를 섞어 판독 id 를 붙인다. run 식별자·반복 번호는 판독 파일에 넣지 않는다."""
    rows = []
    for g_i, g in enumerate(gen["generations"]):
        if not g.get("valid"):
            continue
        for i_i, it in enumerate(g["items"]):
            rows.append((g_i, i_i, it))
    random.Random(seed).shuffle(rows)
    blind, mp = [], []
    for n, (g_i, i_i, it) in enumerate(rows, 1):
        rid = f"R{n:03d}"
        blind.append({"rid": rid, "key": it.get("key"), "name": it.get("name"), "status": it.get("status"),
                      "unverified": bool(it.get("unverified")), "quote": it.get("quote")})
        mp.append({"rid": rid, "gen": g_i, "item": i_i})
    return blind, mp


READING_GUIDE = (
    "각 항목은 LLM이 기준일 현재 열린 일 목록으로 쓴 줄 하나다(key, name, status, unverified, quote). 항목마다 두 단계를 서로 따로 판독한다. "
    "1) work: 이 항목이 가리키는 작업 하나. 이슈·PR 번호를 key·name·quote에서 알 수 있으면 \"<repo>#<n>\"(repo는 저장소 이름), "
    "번호가 없으면 \"label:<짧은 작업 이름>\", 여러 작업을 한 줄에 묶었으면 \"multi\", 무엇인지 모르겠으면 \"unclear\". "
    "2) claims_open: 이 항목이 그 작업이 기준일 현재 아직 열려 있다(끝나지 않았다)고 주장하면 true, 끝났다·잔여만 남았다고 하면 false. "
    "주장 판독이지 사실 확인이 아니다(status 필드와 name·quote 문장을 함께 본다. unverified는 표시일 뿐 주장을 바꾸지 않는다). "
    "회신: {\"items\": [{\"rid\": \"R001\", \"work\": \"...\", \"claims_open\": true}, ...]} 를 모든 rid 에 대해."
)


def agreement(pairs: Sequence[Tuple[Any, Any]]) -> Dict[str, Any]:
    n = len(pairs)
    agree = sum(1 for a, b in pairs if a == b)
    return {"n": n, "agree": agree, "rate": (agree / n) if n else None}


def run_cmd(a: argparse.Namespace) -> int:
    with open(a.input, encoding="utf-8") as f:
        stored = json.load(f)
    template = load_prompt(a.prompt)
    try:
        asm = _assemble_for_run(a, stored)      # 처치 옵션이 없으면 assemble() 그대로(R4-3, issue #352)
    except ValueError as e:
        print(f"refuse: {e}", file=sys.stderr)
        return 1
    prompt = build_prompt(template, asm)
    out = os.path.join(a.out, f"{a.label}-gen.json")
    if os.path.exists(out):
        print(f"refuse: {out} exists", file=sys.stderr)
        return 1
    head = {"label": a.label, "model": a.model, "input_sha256": sha256_file(a.input),
            "prompt_file": os.path.basename(a.prompt), "prompt_sha256": sha256_file(a.prompt),
            "prompt_text_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "max_tokens_filter": a.max_tokens, "max_input_chars": a.max_input_chars,
            "source_rows": asm["source_rows"], "no_id_rows": asm["no_id_rows"], "union_rows": asm["union_rows"], "input_rows": len(asm["rows"]),
            "truncated_rows": asm["truncated_rows"], "input_chars": asm["chars"], "prompt_chars": len(prompt),
            "recalls": asm["recalls"], "started_kst": now_kst()}
    if "treatment" in asm:
        head.update(treatment=asm["treatment"], input_ids=[r["id"] for r in asm["rows"]])
    print(json.dumps({k: v for k, v in head.items() if k not in ("recalls", "input_ids")}, ensure_ascii=False))
    caller = claude_caller(a.model)
    gens = []
    for r in range(a.k):
        g = generate_one(prompt, caller)
        gens.append(g)
        write_private(out, dict(head, generations=gens))
        for c in g["calls"]:
            print(json.dumps({"rep": r + 1, **{k: v for k, v in c.items() if k != "raw_result"}}, ensure_ascii=False))
        print(json.dumps({"rep": r + 1, "valid": g["valid"], "items": len(g["items"])}))
    write_private(out, dict(head, generations=gens, finished_kst=now_kst()))
    print(f"wrote {out}")
    return 0


def judge_cmd(a: argparse.Namespace) -> int:
    with open(a.gen, encoding="utf-8") as f:
        gen = json.load(f)
    if a.expect_key_sha256 and sha256_file(a.answer_key) != a.expect_key_sha256:
        print("refuse: answer key sha256 mismatch", file=sys.stderr)
        return 1
    with open(a.answer_key, encoding="utf-8") as f:
        doc = json.load(f)
    s = judge_summary(gen["generations"], tasks_from_answer_key(doc))
    s.update(match_sha256=sha256_file(M.__file__), prompt_sha256=gen.get("prompt_sha256"),
             answer_key_sha256=sha256_file(a.answer_key), gen_sha256=sha256_file(a.gen), label=gen.get("label"))
    if a.out:
        write_private(a.out, s)
    rep = s["reproduction"]
    print(json.dumps({"K": s["K"], "invalid": s["invalid"], "reproduced": rep["reproduced"],
                      "majority": rep["majority"], "items": rep["items"],
                      "per_run_closed_open": rep["per_run_closed_open"], "closed_in_list": s["closed_in_list"],
                      "controls_kept": s["controls_kept"], "unmapped": s["unmapped"], "list_items": s["items"],
                      "unverified_items": s["unverified_items"]}, ensure_ascii=False))
    for i, r in sorted(s["open_rate"].items()):
        if r or any(x != "absent" for x in s["states"][i]):
            print(f"{s['category'][i]:9s} {i:6s} open_rate={r:.2f} states={','.join(s['states'][i])}")
    return 0


def blind_cmd(a: argparse.Namespace) -> int:
    with open(a.gen, encoding="utf-8") as f:
        gen = json.load(f)
    blind, mp = blind_file(gen, a.seed)
    write_private(a.out, {"query_date": QUERY_DATE, "guide": READING_GUIDE, "items": blind})
    write_private(a.map, {"gen_sha256": sha256_file(a.gen), "seed": a.seed, "map": mp})
    print(json.dumps({"items": len(blind), "out_sha256": sha256_file(a.out)}))
    return 0


def agree_cmd(a: argparse.Namespace) -> int:
    """판독 파일 형식: {"items": [{"rid", "work": "<repo>#<n>"|"label:..."|"multi"|"unclear", "claims_open": bool}]}.
    (가) 판독자 간: work(정규화 key 또는 같은 문자열) 일치, claims_open 일치. (나) 코드 대 판독: 열림 주장 =
    status=open 대 claims_open, 작업 식별 = 코드가 대응한 작업의 key 집합에 판독 work 가 드는지(코드 unmapped 면 판독 work 가
    어느 작업 key 에도 없어야 일치). key 로 비교할 수 없는 판독(label)은 manual 로 따로 센다."""
    with open(a.map, encoding="utf-8") as f:
        mdoc = json.load(f)
    mp, map_sha = {m["rid"]: m for m in mdoc["map"]}, mdoc.get("gen_sha256")
    with open(a.judge, encoding="utf-8") as f:
        js = json.load(f)
    readers = []
    for p in a.reader:
        with open(p, encoding="utf-8") as f:
            readers.append({r["rid"]: r for r in json.load(f)["items"]})
    tasks = None
    if a.answer_key:
        with open(a.answer_key, encoding="utf-8") as f:
            tasks = {t["id"]: t for t in tasks_from_answer_key(json.load(f))}
    with open(a.gen, encoding="utf-8") as f:
        gen = json.load(f)
    gsha = sha256_file(a.gen)
    srcs = {"map": map_sha, "judge": js.get("gen_sha256")}
    bad = [k for k, v in srcs.items() if v != gsha]
    if bad:
        print(f"refuse: {','.join(bad)} not made from --gen (gen_sha256 mismatch)", file=sys.stderr)
        return 1

    def wnorm(w):
        k = M.norm_key(w) if isinstance(w, str) else None
        return k or (w.strip().casefold() if isinstance(w, str) else None)
    common = sorted(set(readers[0]) & set(readers[1]))
    # 분모 = 두 판독자가 모두 판독한 단위(#349). 판독 파일에 있는데 회신에 없는 rid 와 빈 판독은 따로 센다.
    out = {"common": len(common), "reader_n": [len(r) for r in readers],
           "unread": [len(set(mp) - set(r)) for r in readers]}
    wpairs, wmanual = [], []
    for r in common:
        w1, w2 = readers[0][r].get("work"), readers[1][r].get("work")
        if not (isinstance(w1, str) and w1.strip() and isinstance(w2, str) and w2.strip()):
            continue
        k1, k2 = M.norm_key(w1), M.norm_key(w2)
        if k1 or k2 or wnorm(w1) == wnorm(w2):
            wpairs.append((k1 or wnorm(w1), k2 or wnorm(w2)))
        else:
            wmanual.append({"rid": r, "w1": w1, "w2": w2})
    out["a_work"] = dict(agreement(wpairs), manual=len(wmanual))
    out["a_work_manual"] = wmanual
    out["a_open"] = agreement([(readers[0][r]["claims_open"], readers[1][r]["claims_open"]) for r in common
                               if isinstance(readers[0][r].get("claims_open"), bool)
                               and isinstance(readers[1][r].get("claims_open"), bool)])
    all_keys = set().union(*(M.task_keys(t) for t in tasks.values())) if tasks else set()
    for n, rd in enumerate(readers, 1):
        op, wk, manual = [], [], []
        for rid, r in rd.items():
            m = mp[rid]
            it = gen["generations"][m["gen"]]["items"][m["item"]]
            if isinstance(r.get("claims_open"), bool):
                op.append((it.get("status") == "open", r["claims_open"]))
            code_task = js["mapping"][m["gen"]][m["item"]]["task"]
            w = M.norm_key(r.get("work"))
            if tasks is None:
                continue
            if w is None or (code_task and not M.task_keys(tasks[code_task])):
                # label 판독, 또는 코드가 고른 작업에 key 가 없어 key 로 비교할 수 없으면 사람이 근거로 대조한다
                manual.append({"rid": rid, "code_task": code_task, "work": r.get("work")})
                continue
            ok = (w in M.task_keys(tasks[code_task])) if code_task else (w not in all_keys)
            wk.append((ok, True))
        out[f"b_open_r{n}"] = agreement(op)
        out[f"b_work_r{n}"] = dict(agreement(wk), manual=len(manual))
        out[f"b_work_r{n}_manual"] = manual
    # manual 목록은 판독 label 원문(기억 내용 파생)을 담으므로 표준 출력에 내지 않고 0600 파일에만 쓴다
    manual = {k: out.pop(k) for k in [k for k in out if k.endswith("_manual")]}
    mpath = re.sub(r"(\.json)?$", "-manual.json", a.out, count=1)
    write_private(a.out, out)
    write_private(mpath, manual)
    print(json.dumps(dict(out, manual_file=mpath), ensure_ascii=False))
    return 0


# ---------------------------------------------------------------- R4-1(issue #351): recall 저장, 판독 표, 세트 D, stats
# 기본값은 R4-0 제안(artifacts/arch-v2/r4-0-dllm.md 7절). 사전 등록 v4 1부 나머지가 확정하며 인자로 바꿀 수 있다.
R41_K = 7
R41_MAX_INPUT_CHARS = 200000
R41_MAX_TOKENS = 16384
R41_QUERY_TIMESTAMP = "2026-10-28T14:59:59Z"     # #349 "recall 인자"
TRUTHS = ("done", "open", "partial", "unknown")
TRUTH_CAT = {"done": "closed", "open": "controls", "partial": "partial", "unknown": "unknown"}


def tiktoken_counter() -> Optional[Callable[[str], int]]:
    """응답 토큰 = 결과 행 text 의 tiktoken cl100k_base 수 합(upstream max_tokens 예산과 같은 셈). 없으면 None."""
    try:
        import tiktoken
    except ImportError:
        return None
    enc = tiktoken.get_encoding("cl100k_base")
    return lambda s: len(enc.encode(s or ""))


def save_recalls(call: Callable[[str, Dict[str, Any]], Dict[str, Any]], instruction: str,
                 queries: Sequence[Dict[str, Any]], max_tokens: int = R41_MAX_TOKENS,
                 query_timestamp: str = R41_QUERY_TIMESTAMP,
                 counter: Optional[Callable[[str], int]] = None) -> Dict[str, Any]:
    """질의마다 게이트웨이 MCP recall 1회. query = 고정 지시문 + 줄바꿈 + 문안. 실패는 error 로 남기고 빈 결과로 계속한다.
    출력 runs 는 assemble() 입력 모양(q 는 질의 순서 1부터)."""
    runs = []
    for n, q in enumerate(queries, 1):
        args = {"query": f"{instruction.strip()}\n{q['text'].strip()}", "query_timestamp": query_timestamp,
                "max_tokens": max_tokens}
        t0 = time.monotonic()
        try:
            res, err = call("recall", args), None
        except Exception as e:          # noqa: BLE001 - 저장은 계속하고 오류 종류만 남긴다(원문 없음)
            res, err = {"results": []}, type(e).__name__
        secs = round(time.monotonic() - t0, 3)
        rows = res.get("results") or []
        texts = [r.get("text") or "" for r in rows]
        runs.append({"q": n, "qid": q.get("qid"), "scope": q.get("scope"), "max_tokens": max_tokens,
                     "seconds": float(secs), "error": err, "rows": len(rows),
                     "response_chars": sum(len(t) for t in texts),
                     "response_tokens": sum(counter(t) for t in texts) if counter else None,
                     "results": rows})
    tok = [r["response_tokens"] for r in runs]
    return {"query_timestamp": query_timestamp, "max_tokens": max_tokens, "runs": runs,
            "totals": {"queries": len(runs), "errors": sum(1 for r in runs if r["error"]),
                       "rows": sum(r["rows"] for r in runs),
                       "response_tokens": sum(tok) if all(t is not None for t in tok) else None,
                       "seconds": sum(r["seconds"] for r in runs)}}


def name_queries(setd: Dict[str, Any]) -> List[Dict[str, Any]]:
    """T1b 입력: 세트 D 고정 후보(끝난 일 + 대조군 + partial, unknown 제외)마다 이름 recall 문안 = 고정 후보 이름."""
    return [{"qid": it["id"], "scope": "name", "text": it["name"]}
            for cat in ("closed", "controls", "partial") for it in setd.get(cat) or []]


def recall_cmd(a: argparse.Namespace) -> int:
    from bench import arch_v2_regression as A
    env = dict(os.environ)
    try:
        target = A.resolve_target(env, False, False)     # 사본(HC_TARGET=next)만. 운영은 거부한다
    except A.Refused as e:
        print(f"refuse: {e}", file=sys.stderr)
        return 2
    if target.kind != "next":
        print("refuse: recall save runs only against the copy", file=sys.stderr)
        return 2
    if os.path.exists(a.out):
        print(f"refuse: {a.out} exists", file=sys.stderr)
        return 1
    instruction = load_prompt(a.instruction)
    if a.from_set:
        with open(a.from_set, encoding="utf-8") as f:
            queries, qsrc = name_queries(json.load(f)), a.from_set
    else:
        with open(a.queries, encoding="utf-8") as f:
            queries, qsrc = json.load(f)["queries"], a.queries
    print(target.line(), file=sys.stderr)

    class _Args:
        bank = None
        owner = None
    ns = _Args()
    ns.bank = A.resolve_bank(ns, env)
    gw, _ = A.build_clients(target, ns, env)
    head = {"label": a.label, "target": target.kind, "instruction_sha256": sha256_file(a.instruction),
            "queries_sha256": sha256_file(qsrc), "queries_kind": "names" if a.from_set else "file",
            "token_counter": "tiktoken cl100k_base(text)", "started_kst": now_kst()}
    counter = tiktoken_counter()
    if counter is None:
        head["token_counter"] = None
    doc = save_recalls(gw.call, instruction, queries, a.max_tokens, a.query_timestamp, counter)
    doc = dict(head, **doc, finished_kst=now_kst())
    write_private(a.out, doc)
    for r in doc["runs"]:
        print(json.dumps({k: r[k] for k in ("q", "qid", "seconds", "rows", "response_tokens", "error")},
                         ensure_ascii=False))
    print(json.dumps(dict(totals=doc["totals"], out_sha256=sha256_file(a.out)), ensure_ascii=False))
    if doc["totals"]["errors"]:
        # 실패한 질의는 빈 결과로 저장돼 있다(증거). 측정 입력으로 쓰지 않게 실패로 끝낸다(final-review #351-1)
        print(f"fail: {doc['totals']['errors']} recall error(s); do not use {a.out} as a measurement input",
              file=sys.stderr)
        return 1
    return 0


def work_table(gen: Dict[str, Any], mdoc: Dict[str, Any], reader: Dict[str, Any]) -> Dict[str, Any]:
    """판독자 1의 work 판독으로 목록 항목을 작업별로 묶은 참값 판독 표 뼈대. multi·unclear 는 묶지 않고 센다.
    채울 칸: truth(done/open/partial/unknown), keywords(목록 항목 이름·이슈 번호에서만), project_hint, 근거 칸."""
    mp = {m["rid"]: m for m in mdoc["map"]}
    groups: Dict[str, Dict[str, Any]] = {}
    skipped = {"multi": 0, "unclear": 0}
    for r in reader["items"]:
        w = r.get("work")
        if not isinstance(w, str) or not w.strip():
            continue
        if w.strip() in skipped:
            skipped[w.strip()] += 1
            continue
        wk = M.norm_key(w) or w.strip()
        m = mp[r["rid"]]
        it = gen["generations"][m["gen"]]["items"][m["item"]]
        g = groups.setdefault(wk, {"work": wk, "rids": [], "gens": set(), "names": [], "keys": [],
                                   "quotes": [], "evidence_ids": [], "statuses": {}, "claims_open": 0})
        g["rids"].append(r["rid"])
        g["gens"].add(m["gen"])
        for fld, val in (("names", it.get("name")), ("keys", M.norm_key(it.get("key"))), ("quotes", it.get("quote"))):
            if val and val not in g[fld]:
                g[fld].append(val)
        for e in it.get("evidence_ids") or []:
            if e not in g["evidence_ids"]:
                g["evidence_ids"].append(e)
        g["statuses"][it.get("status")] = g["statuses"].get(it.get("status"), 0) + 1
        g["claims_open"] += 1 if r.get("claims_open") is True else 0
    works = []
    for n, g in enumerate(sorted(groups.values(), key=lambda x: (-len(x["gens"]), x["work"])), 1):
        gens = g.pop("gens")
        works.append(dict(g, wid=f"W{n:02d}", n_items=len(g["rids"]), runs=len(gens), truth=None, keywords=[],
                          project_hint=None, ext_evidence=None, bank_evidence=None))
    return {"gen_sha256": mdoc.get("gen_sha256"), "skipped": skipped, "works": works}


def _keyword_ok(kw: str, w: Dict[str, Any]) -> bool:
    """keyword 출처 가드: 그 작업 목록 항목의 name 안의 말, 또는 key(정규화) 자체나 그 맨 번호."""
    k = kw.casefold().strip()
    if not k:
        return False
    if any(k in (n or "").casefold() for n in w.get("names") or []):
        return True
    keys = {M.norm_key(x) for x in w.get("keys") or []}
    if M.norm_key(kw) in keys:
        return True
    return any(x and x.endswith(k) and k.startswith("#") for x in keys)


def fix_set(table: Dict[str, Any]) -> Dict[str, Any]:
    """채운 판독 표 -> 세트 D(closed=끝난 일, controls=열린 일=대조군, partial, unknown=제외). judge 작업 표 모양과 같다."""
    out: Dict[str, Any] = {"closed": [], "controls": [], "partial": [], "unknown": []}
    for w in table["works"]:
        t = w.get("truth")
        if t not in TRUTHS:
            raise ValueError(f"{w.get('wid')}: truth must be one of {TRUTHS}")
        row = {"id": w["wid"], "name": (w.get("names") or [""])[0], "work": w.get("work"),
               "keys": [k for k in (w.get("keys") or []) if k], "keywords": list(w.get("keywords") or []),
               "project_hint": w.get("project_hint")}
        if t != "unknown":
            if not row["keywords"]:
                raise ValueError(f"{w['wid']}: keyword list is empty")
            bad = [k for k in row["keywords"] if not _keyword_ok(k, w)]
            if bad:
                raise ValueError(f"{w['wid']}: keyword not from list item name or issue number")
        out[TRUTH_CAT[t]].append(row)
    cnt = {c: len(out[c]) for c in ("closed", "controls", "partial", "unknown")}
    for c in ("closed", "controls"):
        cnt[f"{c}_keyed"] = sum(1 for r in out[c] if r["keys"])
        cnt[f"{c}_unkeyed"] = cnt[c] - cnt[f"{c}_keyed"]
    out["counts"] = cnt
    return out


def table_cmd(a: argparse.Namespace) -> int:
    with open(a.gen, encoding="utf-8") as f:
        gen = json.load(f)
    with open(a.map, encoding="utf-8") as f:
        mdoc = json.load(f)
    if mdoc.get("gen_sha256") != sha256_file(a.gen):
        print("refuse: map not made from --gen (gen_sha256 mismatch)", file=sys.stderr)
        return 1
    for p in (a.out, a.truth_blind):
        if p and os.path.exists(p):
            print(f"refuse: {p} exists", file=sys.stderr)     # 채운 판독 표를 빈 뼈대로 덮어쓰지 않는다
            return 1
    with open(a.reader, encoding="utf-8") as f:
        reader = json.load(f)
    t = work_table(gen, mdoc, reader)
    write_private(a.out, t)
    if a.truth_blind:
        # 두 번째 판독자용 참값 판독 파일: 작업별 이름·key·인용만(반복 수·상태 집계·run 식별자 없음)
        write_private(a.truth_blind, {"query_timestamp": R41_QUERY_TIMESTAMP, "works": [
            {"wid": w["wid"], "keys": w["keys"], "names": w["names"], "quotes": w["quotes"]} for w in t["works"]]})
    print(json.dumps({"works": len(t["works"]), "skipped": t["skipped"], "out_sha256": sha256_file(a.out)}))
    return 0


def fixset_cmd(a: argparse.Namespace) -> int:
    with open(a.table, encoding="utf-8") as f:
        table = json.load(f)
    try:
        sd = fix_set(table)
    except ValueError as e:
        print(f"refuse: {e}", file=sys.stderr)
        return 1
    sd["table_sha256"] = sha256_file(a.table)
    if os.path.exists(a.out):
        print(f"refuse: {a.out} exists", file=sys.stderr)
        return 1
    write_private(a.out, sd)
    print(json.dumps(dict(sd["counts"], out_sha256=sha256_file(a.out))))
    return 0


def stats_cmd(a: argparse.Namespace) -> int:
    def load(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    sd = load(a.set)
    base = [load(p) for p in a.base]
    treat = [load(p) for p in a.treat or []]
    qualified = load(a.qualified)["pairs"] if a.qualified else None
    offtable = load(a.offtable)["pairs"] if a.offtable else None
    try:
        rep = S.evaluate(sd, base, treat, qualified=qualified, offtable=offtable, x2=a.x2, perms=a.perms,
                         seed=a.seed)
    except ValueError as e:
        print(f"refuse: {e}", file=sys.stderr)
        return 1
    rep["inputs"] = {"set_sha256": sha256_file(a.set), "base_sha256": [sha256_file(p) for p in a.base],
                     "treat_sha256": [sha256_file(p) for p in a.treat or []],
                     "qualified_sha256": sha256_file(a.qualified) if a.qualified else None,
                     "offtable_sha256": sha256_file(a.offtable) if a.offtable else None,
                     "stats_sha256": sha256_file(S.__file__), "match_sha256": sha256_file(M.__file__)}
    write_private(a.out, rep)
    for line in S.summary_lines(rep):
        print(line)
    print(f"wrote {a.out} sha256={sha256_file(a.out)}")
    return 0


# ---------------------------------------------------------------- R4-3(issue #352): 처치 변형, 근거 주입·제거
# 조립 규칙은 사전 등록 v4 2부 2.4(코디네이터 결정 A): 기준선 입력(--max-input-chars 적용 결과)을 그대로 앞에 두고
# 추가 행(기억 id 중복 제거)을 뒤에 붙인다. 상한은 기준선 몫에만. T1b 이름 recall 은 후보 순서(세트 D 고정 순 = 저장 q 순)
# 라운드로빈으로 순위 1행씩 넣고 추가분 R43_NAMES_ADD_CAP 자에서 멈춘다. 제거는 기준선 입력에서 id 를 지운다(잘린 행 복귀 없음).
T1LITE_PROMPT = os.path.join(HERE, "prompts", "d_llm_t1_lite.txt")
T1A_PROMPT = os.path.join(HERE, "prompts", "d_llm_t1a.txt")
R43_NAMES_ADD_CAP = 100000


def _row_len(row: Dict[str, Any]) -> int:
    return len(render_row(row)) + 1


def assemble_treat(base_stored: Dict[str, Any], max_input_chars: Optional[int] = None,
                   max_tokens: Optional[int] = None, add_stored: Optional[Dict[str, Any]] = None,
                   add_names: Optional[Dict[str, Any]] = None, names_cap: int = R43_NAMES_ADD_CAP,
                   inject_rows: Optional[Sequence[Dict[str, Any]]] = None,
                   drop_ids: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """처치 입력. 처치 옵션이 하나도 없으면 assemble() 결과 그대로(기준선과 같은 입력)."""
    base = assemble(base_stored, max_tokens=max_tokens, max_input_chars=max_input_chars)
    if add_stored is None and add_names is None and inject_rows is None and drop_ids is None:
        return base
    drop = set(drop_ids or [])
    rows = [r for r in base["rows"] if r.get("id") not in drop]
    seen = {r["id"] for r in rows} | drop          # 지운 id 는 추가·주입으로도 다시 들이지 않는다
    t: Dict[str, Any] = {"base_input_rows": len(base["rows"]), "base_input_chars": base["chars"],
                         "base_truncated_rows": base["truncated_rows"], "drop_ids": len(drop),
                         "dropped_rows": len(base["rows"]) - len(rows)}

    def take(row: Dict[str, Any]) -> bool:
        if not row.get("id") or row["id"] in seen:
            return False
        seen.add(row["id"])
        rows.append(row)
        return True
    if add_stored is not None:
        n0, c0 = len(rows), sum(_row_len(r) for r in rows)
        for row in assemble(add_stored)["rows"]:
            take(row)
        t.update(added_rows=len(rows) - n0, added_chars=sum(_row_len(r) for r in rows) - c0)
    if add_names is not None:
        runs = [r.get("results") or [] for r in sorted(add_names.get("runs") or [], key=lambda r: r.get("q", 0))]
        added, chars, stopped = 0, 0, False
        for rank in range(max((len(r) for r in runs), default=0)):
            for res in runs:
                if rank >= len(res):
                    continue
                row = res[rank]
                if not row.get("id") or row["id"] in seen:
                    continue
                if chars + _row_len(row) > names_cap:
                    stopped = True
                    break
                take(row)
                added, chars = added + 1, chars + _row_len(row)
            if stopped:
                break
        fresh = {row["id"] for res in runs for row in res if row.get("id")} - seen
        t.update(names_added_rows=added, names_added_chars=chars, names_unadded_rows=len(fresh),
                 names_cap=names_cap)
    if inject_rows is not None:
        n0 = len(rows)
        for row in inject_rows:
            take(row)
        t.update(injected_rows=len(rows) - n0)
    out = dict(base, rows=rows, chars=sum(_row_len(r) for r in rows))
    out["treatment"] = t
    return out


def _load_json(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _assemble_for_run(a: argparse.Namespace, stored: Dict[str, Any]) -> Dict[str, Any]:
    """run 의 입력 조립. 처치 옵션이 없으면 기준선 assemble() 그대로. 처치 파일 모양이 틀리면 ValueError."""
    opt = {"add_stored": getattr(a, "add", None), "add_names": getattr(a, "add_names", None),
           "inject_rows": getattr(a, "inject", None), "drop_ids": getattr(a, "drop_ids", None)}
    if not any(opt.values()):
        return assemble(stored, max_tokens=a.max_tokens, max_input_chars=a.max_input_chars)
    kw: Dict[str, Any] = {}
    shas: Dict[str, Any] = {}
    for name, fld, shape in (("add_stored", "add", "runs"), ("add_names", "names", "runs"),
                             ("inject_rows", "inject", "rows"), ("drop_ids", "drop", "ids")):
        if not opt[name]:
            continue
        doc = _load_json(opt[name])
        if not isinstance(doc, dict) or not isinstance(doc.get(shape), list):
            raise ValueError(f"{fld} file must be an object with a '{shape}' list")
        want = {"runs": dict, "rows": dict, "ids": str}[shape]
        if not all(isinstance(x, want) for x in doc[shape]):
            raise ValueError(f"{fld} file '{shape}' items must be {want.__name__}")
        kw[name] = doc if shape == "runs" else doc[shape]
        shas[f"{fld}_sha256"] = sha256_file(opt[name])
    asm = assemble_treat(stored, max_input_chars=a.max_input_chars, max_tokens=a.max_tokens,
                         names_cap=a.add_names_cap, **kw)
    asm["treatment"].update(shas)
    return asm


def diag_inputs(setd: Dict[str, Any], gen: Dict[str, Any], done_rows: Dict[str, Any],
                stores: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """인과 진단 입력(판정 입력 아님). done_rows = 판독자가 고른 끝난 일별 완료 근거 기억 id {"tasks": {작업 id: [id]}}.
    inject = 끝난 일(세트 D closed 순)의 완료 근거 행 전체(stores 에서 처음 찾은 행, 없는 id 는 missing).
    drop = 기준선 유효 반복에서 끝난 일에 대응된 status=open 항목의 evidence_ids 합집합(완료 근거 행은 뺀다)."""
    rows: Dict[str, Dict[str, Any]] = {}
    for st in stores:
        for run in sorted(st.get("runs") or [], key=lambda r: r.get("q", 0)):
            for row in run.get("results") or []:
                if row.get("id"):
                    rows.setdefault(row["id"], row)
    closed = [it["id"] for it in setd.get("closed") or []]
    want = {c: list(dict.fromkeys((done_rows.get("tasks") or {}).get(c) or [])) for c in closed}
    done_all = {i for v in want.values() for i in v}
    tasks = S.tasks_of(setd)
    stale: Dict[str, List[str]] = {c: [] for c in closed}
    for g in gen.get("generations") or []:
        if not g.get("valid"):
            continue
        j = M.judge_run(g.get("items") or [], tasks)
        for it, m in zip(g.get("items") or [], j["mapping"]):
            if m["task"] in stale and it.get("status") == "open":
                for e in it.get("evidence_ids") or []:
                    if e not in done_all and e not in stale[m["task"]]:
                        stale[m["task"]].append(e)
    inject, missing, seen = [], [], set()
    for c in closed:
        for i in want[c]:
            if i in seen:
                continue
            seen.add(i)
            (inject.append(rows[i]) if i in rows else missing.append(i))
    drop = sorted({e for v in stale.values() for e in v})
    per = {c: {"done_rows": len(want[c]), "inject": sum(1 for i in want[c] if i in rows), "stale": len(stale[c])}
           for c in closed}
    return {"inject": inject, "missing": missing, "drop": drop, "per_task": per}


def diag_cmd(a: argparse.Namespace) -> int:
    if os.path.abspath(a.out_inject) == os.path.abspath(a.out_drop):
        print("refuse: --out-inject and --out-drop are the same path", file=sys.stderr)
        return 1
    for p in (a.out_inject, a.out_drop):
        if os.path.exists(p):
            print(f"refuse: {p} exists", file=sys.stderr)
            return 1
    out = diag_inputs(_load_json(a.set), _load_json(a.gen), _load_json(a.done_rows),
                      [_load_json(p) for p in a.rows_from])
    src = {"set_sha256": sha256_file(a.set), "gen_sha256": sha256_file(a.gen),
           "done_rows_sha256": sha256_file(a.done_rows), "rows_from_sha256": [sha256_file(p) for p in a.rows_from]}
    write_private(a.out_inject, dict(src, rows=out["inject"], missing=out["missing"], per_task=out["per_task"]))
    write_private(a.out_drop, dict(src, ids=out["drop"], per_task=out["per_task"]))
    print(json.dumps({"inject": len(out["inject"]), "missing": len(out["missing"]), "drop": len(out["drop"]),
                      "per_task": out["per_task"], "inject_sha256": sha256_file(a.out_inject),
                      "drop_sha256": sha256_file(a.out_drop)}, ensure_ascii=False))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="bench.d_llm")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--input", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--k", type=int, default=5)
    r.add_argument("--label", default="main")
    r.add_argument("--max-tokens", type=int, default=None, help="이 max_tokens 의 recall 만 쓴다(보고용 입력)")
    r.add_argument("--max-input-chars", type=int, default=None)
    r.add_argument("--prompt", default=BASELINE_PROMPT)
    r.add_argument("--model", default=MODEL)
    # R4-3(issue #352) 처치 변형: 기준선 입력 뒤에 붙이거나 기준선 입력에서 지운다(상한은 기준선 몫에만)
    r.add_argument("--add", help="T0: 추가 질의 저장 recall(run --input 모양). 새 행을 질의 순 -> 순위 순으로 뒤에 붙인다")
    r.add_argument("--add-names", help="T1b: 이름 recall 저장. 후보 순서 라운드로빈으로 순위 1행씩")
    r.add_argument("--add-names-cap", type=int, default=R43_NAMES_ADD_CAP, help="T1b 추가분 글자 상한")
    r.add_argument("--inject", help='인과 진단: 주입 행 {"rows": [...]}(diag 출력)')
    r.add_argument("--drop-ids", help='인과 진단: 기준선 입력에서 지울 기억 id {"ids": [...]}(diag 출력)')
    j = sub.add_parser("judge")
    j.add_argument("--gen", required=True)
    j.add_argument("--answer-key", required=True)
    j.add_argument("--expect-key-sha256")
    j.add_argument("--out")
    b = sub.add_parser("blind")
    b.add_argument("--gen", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--map", required=True)
    b.add_argument("--seed", type=int, default=350)
    g = sub.add_parser("agree")
    g.add_argument("--gen", required=True)
    g.add_argument("--map", required=True)
    g.add_argument("--judge", required=True)
    g.add_argument("--reader", action="append", required=True)
    g.add_argument("--answer-key")
    g.add_argument("--out", required=True, help="집계 JSON(0600). manual 목록은 <out>-manual.json")
    # R4-1(issue #351) 하위 명령
    rc = sub.add_parser("recall", help="사본 게이트웨이 MCP recall 저장(질의마다 지연·응답 토큰)")
    rc.add_argument("--instruction", required=True, help="d-llm 고정 지시문 파일(리포 밖)")
    qsrc = rc.add_mutually_exclusive_group(required=True)
    qsrc.add_argument("--queries", help='{"queries": [{"qid", "text"}]} 파일(리포 밖)')
    qsrc.add_argument("--from-set", help="세트 D 파일: 고정 후보마다 이름 recall(T1b)")
    rc.add_argument("--out", required=True)
    rc.add_argument("--label", default="recall")
    rc.add_argument("--max-tokens", type=int, default=R41_MAX_TOKENS)
    rc.add_argument("--query-timestamp", default=R41_QUERY_TIMESTAMP)
    tb = sub.add_parser("table", help="판독자 1 회신으로 작업별 참값 판독 표 뼈대")
    tb.add_argument("--gen", required=True)
    tb.add_argument("--map", required=True)
    tb.add_argument("--reader", required=True)
    tb.add_argument("--out", required=True)
    tb.add_argument("--truth-blind", help="두 번째 판독자용 참값 판독 파일(작업 이름·key·인용만)")
    fx = sub.add_parser("fixset", help="채운 판독 표 -> 세트 D")
    fx.add_argument("--table", required=True)
    fx.add_argument("--out", required=True)
    st = sub.add_parser("stats", help="주 통계량·영분포(bench/d_llm_stats.py)")
    st.add_argument("--set", required=True)
    st.add_argument("--base", action="append", required=True, help="측정 복원별 기준선 run 출력(복원 순서)")
    st.add_argument("--treat", action="append", help="같은 순서의 처치 run 출력")
    st.add_argument("--x2", action="store_true", help="데이터 처치: 상한에 처치 두 반복 사이 차를 더한다")
    st.add_argument("--qualified", help='이득 판독 {"pairs": [{작업 id: new_done_row|stale_open_dropped|vanished}, ...]}')
    st.add_argument("--offtable", help='표 밖 항목 {"pairs": [[{"id", "label", "rate_b", "rate_t"}], ...]}')
    st.add_argument("--perms", type=int, default=S.PERMS)
    st.add_argument("--seed", type=int, default=S.SEED)
    st.add_argument("--out", required=True)
    dg = sub.add_parser("diag", help="인과 진단 입력: 완료 근거 주입 행·낡은 열림 근거 제거 id(판정 입력 아님)")
    dg.add_argument("--set", required=True)
    dg.add_argument("--gen", required=True, help="같은 복원의 기준선 run 출력")
    dg.add_argument("--done-rows", required=True, help='판독: {"tasks": {작업 id: [완료 근거 기억 id]}}(리포 밖)')
    dg.add_argument("--rows-from", action="append", required=True, help="행 원문을 찾을 저장 recall(여럿)")
    dg.add_argument("--out-inject", required=True)
    dg.add_argument("--out-drop", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "agree" and len(a.reader) != 2:
        ap.error("agree needs exactly two --reader")
    if a.cmd == "stats" and a.treat and len(a.treat) != len(a.base):
        ap.error("--treat count must equal --base count")
    return {"run": run_cmd, "judge": judge_cmd, "blind": blind_cmd, "agree": agree_cmd, "recall": recall_cmd,
            "table": table_cmd, "fixset": fixset_cmd, "stats": stats_cmd, "diag": diag_cmd}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
