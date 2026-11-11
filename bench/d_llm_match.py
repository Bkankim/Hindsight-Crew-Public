"""d-llm 목록 출력의 대응·판정 규칙(issue #350, 상위 #349 "d-llm 판정"). R4-0 머지 커밋으로 동결한다.

대응·판정 함수만 둔다(입력 조립·LLM 호출·출력 검증은 bench/d_llm.py). 작업 표는 호출자가 준다:
`{"id", "keywords", "project_hint"?, "keys"?}`. keyword 매칭 경계 규칙은 판정기 v2(동결)의 것을 그대로 쓴다.

규칙(#349):
(1) `key` 일치가 1순위. 목록 항목 key(`<repo>#<n>`)를 정규화해 작업 key 와 비교한다. 작업 key = 명시 `keys` +
    `<repo>#<n>` 모양 keyword + 맨 번호 keyword(`#n`)에 project_hint 를 붙인 것. key 가 어느 작업과도 맞지 않으면
    (2)로 내려간다(작업 표에 key 가 없는 작업이 있으므로). key 가 둘 이상에 맞으면 그 안에서 (2)의 가장 긴 keyword, 그래도
    동률이면 ambiguous.
(2) `name` 필드에만 keyword 를 적용한다(`quote`·이유 문장 제외). 맨 번호 keyword 는 project_hint 가 있으면 같은 name 에
    프로젝트 이름이 있어야 센다(hint 가 없으면 그대로 센다). key 는 owner 를 버린 마지막 경로 조각을 소문자·기호 제거로 맞춘다.
(3) `evidence_ids` 교집합은 대응을 만들지 않는다(한 기억이 여러 작업을 담는다).
(4) 대응 안 된 항목은 unmapped(사유 none·ambiguous).
(4a) 항목 하나는 작업 하나에만: 맞는 keyword 중 가장 긴 것의 작업, 동률이면 unmapped(ambiguous). 같은 작업에 항목이 여럿이면
    그 반복의 상태는 하나라도 open 이면 open, 아니면 done 이 있으면 done, 아니면 partial.
(5) open -> open, done -> done, residual -> partial, 목록에 없음 -> absent(unknown). `unverified` 는 상태가 아니라 표시다
    (status=open 이면 open 으로 센다).
"""
import re
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Sequence

from bench.arch_v2_regression import BARE_NUMBER, _kw_re, _norm

STATUS_STATE = {"open": "open", "done": "done", "residual": "partial"}
STATE_CATEGORY = {"open": "open", "done": "done", "partial": "partial", "absent": "unknown", "invalid": "invalid"}
KEY_RE = re.compile(r"^\s*([^#]+?)\s*#\s*(\d+)\s*$")


def norm_key(value: Any) -> Optional[str]:
    """`Owner/Repo-Name #12`, `Repo Name#12` -> `reponame#12`(owner 는 버리고 마지막 경로 조각, 소문자·기호·공백 제거). 모양이 아니면 None."""
    if not isinstance(value, str):
        return None
    m = KEY_RE.match(value)
    repo = _norm(m.group(1).rstrip("/").split("/")[-1]) if m else ""
    if not repo:
        return None
    return f"{repo}#{int(m.group(2))}"


def task_keys(task: Dict[str, Any]) -> set:
    keys = {norm_key(k) for k in task.get("keys") or []}
    hint = task.get("project_hint")
    for kw in task.get("keywords") or []:
        if BARE_NUMBER.match(kw):
            if hint:
                keys.add(norm_key(f"{hint}{kw}"))
        else:
            keys.add(norm_key(kw))
    keys.discard(None)
    return keys


def name_hits(name: Any, task: Dict[str, Any]) -> List[str]:
    """name 에 든 이 작업의 keyword. 맨 번호는 project_hint 가 있으면 name 에 그 이름도 있을 때만(판정기 v2 item_matches 와 같다)."""
    text = name.casefold() if isinstance(name, str) else ""
    if not text:
        return []
    hint = task.get("project_hint")
    hits = []
    for kw in task.get("keywords") or []:
        if not kw or not _kw_re(kw).search(text):
            continue
        if BARE_NUMBER.match(kw) and hint and _norm(hint) not in _norm(text):
            continue
        hits.append(kw)
    return hits


def _longest(name: Any, tasks: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    best, best_len, best_kw = [], 0, None
    for t in tasks:
        hits = name_hits(name, t)
        if not hits:
            continue
        kw = max(hits, key=len)
        if len(kw) > best_len:
            best, best_len, best_kw = [t], len(kw), kw
        elif len(kw) == best_len:
            best.append(t)
    if not best:
        return {"task": None, "reason": "none", "keyword": None}
    if len(best) > 1:
        return {"task": None, "reason": "ambiguous", "keyword": best_kw}
    return {"task": best[0]["id"], "reason": None, "keyword": best_kw}


def match_item(item: Dict[str, Any], tasks: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """목록 항목 하나 -> {"task": id|None, "by": "key"|"name"|None, "reason": None|"none"|"ambiguous", "keyword"}."""
    key = norm_key(item.get("key"))
    if key:
        hit = [t for t in tasks if key in task_keys(t)]
        if len(hit) == 1:
            return {"task": hit[0]["id"], "by": "key", "reason": None, "keyword": None}
        if len(hit) > 1:
            r = _longest(item.get("name"), hit)
            if r["task"]:
                return {"task": r["task"], "by": "key", "reason": None, "keyword": r["keyword"]}
            return {"task": None, "by": None, "reason": "ambiguous", "keyword": None}
    r = _longest(item.get("name"), tasks)
    return {"task": r["task"], "by": "name" if r["task"] else None, "reason": r["reason"], "keyword": r["keyword"]}


def combine(states: Iterable[str]) -> str:
    s = set(states)
    if "open" in s:
        return "open"
    if "done" in s:
        return "done"
    if "partial" in s:
        return "partial"
    return "absent"


def judge_run(items: Sequence[Dict[str, Any]], tasks: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """반복 하나(유효 출력)의 판정: 작업별 상태, 항목별 대응, unmapped 수와 사유, open 이면서 unverified 인 작업."""
    mapping, per_task, unv = [], {}, {}
    for it in items:
        m = match_item(it, tasks)
        mapping.append(m)
        if m["task"] is None:
            continue
        st = STATUS_STATE.get(it.get("status"), "absent")
        per_task.setdefault(m["task"], []).append(st)
        if st == "open" and it.get("unverified") is True:
            unv[m["task"]] = True
    states = {t["id"]: combine(per_task.get(t["id"], [])) for t in tasks}
    reasons = Counter(m["reason"] for m in mapping if m["task"] is None)
    return {"states": states, "mapping": mapping, "unmapped": sum(reasons.values()),
            "unmapped_reasons": dict(reasons),
            "unverified_open": {k: v for k, v in unv.items() if states.get(k) == "open"}}


def judge_runs(runs: Sequence[Dict[str, Any]], tasks: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """K 반복. invalid 반복은 상태 invalid 로 남고 분모 K 에 든다."""
    judged = [judge_run(r.get("items") or [], tasks) if r.get("valid") else None for r in runs]
    out = {}
    for t in tasks:
        states = [j["states"][t["id"]] if j else "invalid" for j in judged]
        out[t["id"]] = {"states": states, "open": states.count("open"),
                        "unverified_open": sum(1 for j in judged if j and j["unverified_open"].get(t["id"]))}
    return {"K": len(runs), "tasks": out, "runs": judged}


def majority(k: int) -> int:
    return k // 2 + 1


def reproduction(result: Dict[str, Any], closed_ids: Sequence[str]) -> Dict[str, Any]:
    """#349 재현 기준: 끝난 항목 중 K회 과반 status=open 대응 항목이 1개 이상이면 재현됨."""
    k, need = result["K"], majority(result["K"])
    items = [i for i in closed_ids if result["tasks"][i]["open"] >= need]
    per_run = [sum(1 for i in closed_ids if result["tasks"][i]["states"][r] == "open") for r in range(k)]
    return {"K": k, "majority": need, "reproduced": bool(items), "items": items, "per_run_closed_open": per_run}
