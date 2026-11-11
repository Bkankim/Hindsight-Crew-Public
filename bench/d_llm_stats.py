"""d-llm 주 통계량과 영분포(issue #351, 상위 #349 "주 통계량과 영분포"·"T1-lite 판정"). R4-1 1단계 커밋으로 고정한다.

R4-3·R4-4 는 이 모듈만 쓰고 다시 구현하지 않는다(`python3 -B -m bench.d_llm stats`).

정의(#349, 사전 등록 v4 1부 나머지가 정본):
- 열림율: 한 복원의 K 반복 중 그 작업이 open 인 비율. invalid 반복도 분모 K 에 든다. 대응·상태는 동결 `bench/d_llm_match.py`.
  `unverified` 는 표시라 open 으로 센다(주 통계량). T1-lite 보조 지표만 `unverified=true` 로만 열린 반복을 not-open 으로 센다.
- 짝: 같은 측정 복원의 기준선 대 처치(복원 1끼리, 복원 2끼리). 두 짝을 각각 보고한다.
- 적격 이득: 이득 판독이 new_done_row(완료 근거 행이 입력에 새로 들어옴) 또는 stale_open_dropped(낡은 열림 행이 빠짐)인
  고정 끝난 항목의 열림율 감소분. vanished(그냥 목록에서 사라짐)는 0.
- 새 오답: 고정 끝난 항목의 열림율 증가분 + 고정 표 밖 항목 중 끝난 일(라벨 불가 포함)로 판독된 것의 열림율 증가분(증가분만).
- 순이득 = 적격 이득 합 - 새 오답.
- 영분포 = 판독 조건 없는 보수적 순이득(모든 감소를 이득으로, 표 밖 항목 없음)을
  (i) 같은 복원 기준선 K 반복을 순열마다 섞어 앞 floor(K/2) 대 다음 floor(K/2)(홀수면 남는 1회 제외), 순열 PERMS 회(씨앗 고정)의
  95 백분위(가장 가까운 순위: 정렬 뒤 ceil(0.95 n) 번째), (ii) 측정 복원 2회 기준선 A/A(두 방향 중 큰 값)에 적용해 큰 값을 상한으로.
  데이터 처치(X2)는 상한에 X2 두 반복 사이 같은 식의 값(두 방향 중 큰 값)을 더한다.
- 대조군 가드: 기준선 열림율 > 0 인 대조군만 분모(0 인 대조군 수는 따로). 대조군 열림율 합 감소가 같은 방식(순열·A/A·X2)의
  대조군 상한 이하면 통과. 분모 < 6 이면 판정 불가.
- 효과: 두 짝 모두 순이득 > 상한, 두 짝 모두 가드 통과, 두 짝 모두 순이득 >= 3.0, 이득 판독 있음. 최소 표본(끝난 >= 20,
  대조군 >= 6) 미달이면 판정 불가.
"""
import math
import random
from typing import Any, Dict, List, Optional, Sequence, Tuple

from bench import d_llm_match as M
from bench.arch_v2_regression import item_keywords

SEED = 20261101          # 순열 난수 씨앗(사전 등록 v4 1부 나머지에 적는다)
PERMS = 1000
MIN_CLOSED = 20
MIN_CONTROLS = 6
NET_MIN = 3.0
QUALIFYING = ("new_done_row", "stale_open_dropped")
READINGS = QUALIFYING + ("vanished",)
OFFTABLE_WRONG = ("done", "unlabelable")
OFFTABLE_LABELS = OFFTABLE_WRONG + ("open", "partial")

Flags = Dict[str, List[int]]
Rates = Dict[str, float]


# ---------------------------------------------------------------- 작업 표와 열림 플래그

def tasks_of(setd: Dict[str, Any]) -> List[Dict[str, Any]]:
    """세트 D(또는 같은 모양의 정답표)에서 고정 후보 작업 표. unknown 은 넣지 않는다."""
    out = []
    for cat in ("closed", "controls", "partial"):
        for it in setd.get(cat) or []:
            out.append({"id": it["id"], "keywords": item_keywords(it), "project_hint": it.get("project_hint"),
                        "keys": list(it.get("keys") or []), "category": cat})
    return out


def ids_of(setd: Dict[str, Any], cat: str) -> List[str]:
    return [it["id"] for it in setd.get(cat) or []]


def open_flags(gen: Dict[str, Any], tasks: Sequence[Dict[str, Any]], strict_unverified: bool = False) -> Flags:
    """작업별 반복마다 open(1)/아님(0). invalid 반복은 0 이고 분모에 남는다.
    strict_unverified: 그 반복의 open 항목이 전부 unverified=true 이면 0(T1-lite 보조 지표)."""
    flags: Flags = {t["id"]: [] for t in tasks}
    for g in gen["generations"]:
        if not g.get("valid"):
            for t in tasks:
                flags[t["id"]].append(0)
            continue
        j = M.judge_run(g.get("items") or [], tasks)
        verified_open = set()
        for it, m in zip(g.get("items") or [], j["mapping"]):
            if m["task"] and it.get("status") == "open" and it.get("unverified") is not True:
                verified_open.add(m["task"])
        for t in tasks:
            st = j["states"][t["id"]] == "open"
            if strict_unverified:
                st = st and t["id"] in verified_open
            flags[t["id"]].append(1 if st else 0)
    return flags


def open_rates(flags: Flags, runs: Optional[Sequence[int]] = None) -> Rates:
    out = {}
    for tid, v in flags.items():
        sel = [v[i] for i in runs] if runs is not None else v
        out[tid] = (sum(sel) / len(sel)) if sel else 0.0
    return out


# ---------------------------------------------------------------- 순이득과 대조군

def net_gain(rb: Rates, rt: Rates, closed: Sequence[str], qualified: Optional[Dict[str, str]] = None,
             offtable: Sequence[Dict[str, Any]] = ()) -> Dict[str, Any]:
    """qualified=None 이면 판독 조건 없는 보수적 판본(모든 감소가 이득). 아니면 {작업 id: 판독} 에서 QUALIFYING 만 이득."""
    if qualified is not None:
        bad = {k: v for k, v in qualified.items() if v not in READINGS}
        if bad:
            raise ValueError(f"unknown gain reading {sorted(set(bad.values()))}")
    gain, wrong, per = 0.0, 0.0, {}
    for c in closed:
        d = rb.get(c, 0.0) - rt.get(c, 0.0)
        if d > 0:
            g = d if qualified is None or qualified.get(c) in QUALIFYING else 0.0
            gain += g
            if g:
                per[c] = g
        elif d < 0:
            wrong += -d
            per[c] = d
    off = 0.0
    for o in offtable:
        if o.get("label") not in OFFTABLE_LABELS:
            raise ValueError(f"unknown off-table label {o.get('label')!r}")
        if o["label"] in OFFTABLE_WRONG:
            off += max(0.0, float(o.get("rate_t") or 0.0) - float(o.get("rate_b") or 0.0))
    return {"gain": gain, "new_wrong": wrong + off, "new_wrong_offtable": off, "net": gain - wrong - off,
            "per_task": per}


def control_drop(rb: Rates, rt: Rates, controls: Sequence[str]) -> Dict[str, Any]:
    den = [c for c in controls if rb.get(c, 0.0) > 0]
    return {"denom": len(den), "zero": len(controls) - len(den),
            "drop": sum(rb[c] - rt.get(c, 0.0) for c in den)}


# ---------------------------------------------------------------- 영분포

def percentile95(values: Sequence[float]) -> float:
    s = sorted(values)
    return s[max(0, math.ceil(0.95 * len(s)) - 1)]


def perm_split(idx: List[int], rng: random.Random) -> Tuple[List[int], List[int]]:
    idx = list(idx)
    rng.shuffle(idx)
    h = len(idx) // 2
    return idx[:h], idx[h:2 * h]


def perm_null(flags: Flags, closed: Sequence[str], controls: Sequence[str], perms: int = PERMS,
              seed: int = SEED) -> Dict[str, Any]:
    k = len(next(iter(flags.values()))) if flags else 0
    rng = random.Random(seed)
    nets, ctrls = [], []
    for _ in range(perms):
        a, b = perm_split(list(range(k)), rng)
        ra, rb = open_rates(flags, a), open_rates(flags, b)
        nets.append(net_gain(ra, rb, closed)["net"])
        ctrls.append(control_drop(ra, rb, controls)["drop"])
    return {"perms": perms, "seed": seed, "K": k, "half": k // 2,
            "p95_net": percentile95(nets) if nets else 0.0, "p95_ctrl": percentile95(ctrls) if ctrls else 0.0,
            "net_values": nets, "ctrl_values": ctrls}


def aa(r1: Rates, r2: Rates, closed: Sequence[str], controls: Sequence[str]) -> Dict[str, Any]:
    """두 기준선(또는 X2 두 반복) 사이 같은 식의 값, 두 방향 중 큰 값."""
    n12, n21 = net_gain(r1, r2, closed)["net"], net_gain(r2, r1, closed)["net"]
    c12, c21 = control_drop(r1, r2, controls)["drop"], control_drop(r2, r1, controls)["drop"]
    return {"net": max(n12, n21), "ctrl": max(c12, c21), "net_dirs": [n12, n21], "ctrl_dirs": [c12, c21]}


# ---------------------------------------------------------------- T1-lite 보조

def unverified_ratio(gen: Dict[str, Any], tasks: Sequence[Dict[str, Any]], closed: Sequence[str],
                     controls: Sequence[str]) -> Dict[str, Any]:
    """open 인 (작업, 반복) 중 unverified=true 로만 열린 비율. 끝난 항목 대 대조군과 그 차."""
    loose, strict = open_flags(gen, tasks), open_flags(gen, tasks, strict_unverified=True)

    def ratio(ids):
        n = sum(sum(loose[i]) for i in ids)
        u = sum(sum(1 for a, b in zip(loose[i], strict[i]) if a and not b) for i in ids)
        return (u / n) if n else None
    c, k = ratio(closed), ratio(controls)
    return {"closed": c, "controls": k, "diff": (c - k) if c is not None and k is not None else None}


# ---------------------------------------------------------------- 평가

def evaluate(setd: Dict[str, Any], base: Sequence[Dict[str, Any]], treat: Sequence[Dict[str, Any]] = (),
             qualified: Optional[Sequence[Dict[str, str]]] = None,
             offtable: Optional[Sequence[Sequence[Dict[str, Any]]]] = None, x2: bool = False,
             perms: int = PERMS, seed: int = SEED) -> Dict[str, Any]:
    """base: 측정 복원별 기준선 gen(복원 순서). treat: 같은 순서의 처치 gen(없으면 기준선 보고만).
    입력 검증(final-review #351-1, 계산식 불변): qualified·offtable 길이가 짝 수와 다르거나 x2 인데 처치가 2개가 아니면
    ValueError. 짝이 정확히 2개가 아니면 effect=invalid-pairs(상한·짝 계산 없음). offtable=None 이면 effect 는
    pending-reading(표 밖 오답을 0으로 세지 않는다. 빈 판독은 명시적 빈 목록으로 준다)."""
    if x2 and len(treat) != 2:
        raise ValueError("x2 needs exactly two treatment gens")
    if treat:
        if len(treat) != len(base):
            raise ValueError("treat gens must pair with base gens (same restore order)")
        for name, v in (("qualified", qualified), ("offtable", offtable)):
            if v is not None and len(v) != len(treat):
                raise ValueError(f"{name} has {len(v)} entries but there are {len(treat)} pairs")
    tasks = tasks_of(setd)
    closed, controls = ids_of(setd, "closed"), ids_of(setd, "controls")
    sample = {"closed": len(closed), "controls": len(controls),
              "ok": len(closed) >= MIN_CLOSED and len(controls) >= MIN_CONTROLS}
    bflags = [open_flags(g, tasks) for g in base]
    brates = [open_rates(f) for f in bflags]
    nulls = [perm_null(f, closed, controls, perms, seed) for f in bflags]
    rep: Dict[str, Any] = {"sample": sample, "seed": seed, "perms": perms,
                           "base": [{"K": len(g["generations"]), "invalid": sum(1 for x in g["generations"]
                                                                                 if not x.get("valid")),
                                     "open_rate": r, "null": n} for g, r, n in zip(base, brates, nulls)]}
    rep["aa"] = aa(brates[0], brates[1], closed, controls) if len(brates) == 2 else None
    rep["stable_positive"] = sorted(c for c in closed if bflags and all(all(f[c]) for f in bflags))
    if not treat:
        return rep
    if len(treat) != 2:
        rep["effect"] = "invalid-pairs"
        return rep
    tflags = [open_flags(g, tasks) for g in treat]
    trates = [open_rates(f) for f in tflags]
    rep["x2_diff"] = aa(trates[0], trates[1], closed, controls) if x2 and len(trates) == 2 else None
    pairs = []
    for i, (rb, rt) in enumerate(zip(brates, trates)):
        aa_net = rep["aa"]["net"] if rep["aa"] else 0.0
        aa_ctrl = rep["aa"]["ctrl"] if rep["aa"] else 0.0
        bound = max(nulls[i]["p95_net"], aa_net) + (rep["x2_diff"]["net"] if rep.get("x2_diff") else 0.0)
        cbound = max(nulls[i]["p95_ctrl"], aa_ctrl) + (rep["x2_diff"]["ctrl"] if rep.get("x2_diff") else 0.0)
        q = qualified[i] if qualified is not None else None
        ng = net_gain(rb, rt, closed, qualified=q if q is not None else {},
                      offtable=offtable[i] if offtable is not None else [])
        cd = control_drop(rb, rt, controls)
        guard = "undecidable" if cd["denom"] < MIN_CONTROLS else ("pass" if cd["drop"] <= cbound else "fail")
        strict_t = open_rates(open_flags(treat[i], tasks, strict_unverified=True))
        strict_b = open_rates(open_flags(base[i], tasks, strict_unverified=True))
        pairs.append({
            "pair": i + 1, "net": ng["net"], "gain": ng["gain"], "new_wrong": ng["new_wrong"],
            "new_wrong_offtable": ng["new_wrong_offtable"], "per_task": ng["per_task"],
            "conservative_net": net_gain(rb, rt, closed)["net"], "bound": bound, "ctrl_bound": cbound,
            "control": cd, "guard": guard, "reading": q is not None, "treat_open_rate": rt,
            "t1_lite": {"strict_treat_net": net_gain(rb, strict_t, closed)["net"],
                        "strict_both_net": net_gain(strict_b, strict_t, closed)["net"],
                        "unverified_ratio": unverified_ratio(treat[i], tasks, closed, controls),
                        "unverified_ratio_base": unverified_ratio(base[i], tasks, closed, controls)},
        })
    rep["pairs"] = pairs
    if not sample["ok"] or any(p["guard"] == "undecidable" for p in pairs):
        rep["effect"] = "undecidable"
    elif any(p["guard"] == "fail" for p in pairs) or any(p["net"] <= p["bound"] or p["net"] < NET_MIN
                                                         for p in pairs if p["reading"]):
        rep["effect"] = "no-effect"
    elif not all(p["reading"] for p in pairs) or offtable is None:
        rep["effect"] = "pending-reading"
    else:
        rep["effect"] = "effect"
    return rep


def summary_lines(rep: Dict[str, Any]) -> List[str]:
    """표준 출력용: id·수치만."""
    lines = [f"sample closed={rep['sample']['closed']} controls={rep['sample']['controls']} ok={rep['sample']['ok']}"
             f" seed={rep['seed']} perms={rep['perms']}"]
    for n, b in enumerate(rep["base"], 1):
        nz = {k: round(v, 3) for k, v in sorted(b["open_rate"].items()) if v}
        lines.append(f"base{n} K={b['K']} invalid={b['invalid']} p95_net={b['null']['p95_net']:.3f} "
                     f"p95_ctrl={b['null']['p95_ctrl']:.3f} open_rate_nonzero={nz}")
    if rep.get("aa"):
        lines.append(f"aa net={rep['aa']['net']:.3f} ctrl={rep['aa']['ctrl']:.3f}")
    lines.append(f"stable_positive={rep['stable_positive']}")
    if rep.get("x2_diff"):
        lines.append(f"x2_diff net={rep['x2_diff']['net']:.3f} ctrl={rep['x2_diff']['ctrl']:.3f}")
    for p in rep.get("pairs") or []:
        lines.append(f"pair{p['pair']} net={p['net']:.3f} gain={p['gain']:.3f} new_wrong={p['new_wrong']:.3f} "
                     f"bound={p['bound']:.3f} guard={p['guard']} ctrl_denom={p['control']['denom']} "
                     f"ctrl_zero={p['control']['zero']} ctrl_drop={p['control']['drop']:.3f} "
                     f"ctrl_bound={p['ctrl_bound']:.3f} conservative_net={p['conservative_net']:.3f} "
                     f"t1lite_strict_treat_net={p['t1_lite']['strict_treat_net']:.3f} "
                     f"unverified_ratio_diff={p['t1_lite']['unverified_ratio']['diff']}")
    if "effect" in rep:
        lines.append(f"effect={rep['effect']}")
    return lines
