#!/usr/bin/env python3
"""duplicate_recall.py - 중복 recall 출처 분석 (issue #81, 읽기 전용).

recall 원장(audit/recall-queries.log)과 게이트웨이 감사 로그(audit/gateway.log)를 읽어,
"같은 요청이 1시간 안에 다시 왔고 그 사이 retain이 없던" 쌍을 찾고 출처별로 센다.
집계 수치만 JSON으로 stdout에 낸다. 쿼리 텍스트·결과 id·세션 id는 출력하지 않는다
(세션은 개수와 쌍 수 분포로만 나온다). 입력 파일은 읽기만 한다.

용어
- 요청 키: (쿼리, 태그 집합, tags_match). 인자가 다르면 같은 요청이 아니다.
  쿼리만 같고 태그가 다른 것까지 세면 #76 실측(58건)보다 많이 잡힌다(95건).
- 쌍: 같은 (identity, bank)에서 요청 키가 같은 직전 행 A와 이번 행 B. 간격 3600초 이내,
  A와 B 사이(A.ts 초과, B.ts 이하)에 그 bank로 향한 retain 허용 줄이 없을 때만 쌍이다.
- reusable: 원장 행의 `reusable`이 False가 아닌 행. 필드가 아직 없던 옛 행은 True로 센다
  (RecallEvent 기본값과 같다).

시각 정밀도
- 두 로그 모두 1초로 내림한 UTC다. 원장 ts는 응답이 끝난 뒤(완료 시각), 감사 로그의
  `allow` 줄은 요청이 도착한 시각이다. 결합 키는 (identity, bank, transport) + 시간 순서뿐이다
  (감사 로그에는 세션·쿼리가 없다). 같은 채널 요청은 도착 순서대로 끝난다고 보고 줄을 순서대로 붙인다.
- 동시 중복: B의 도착 초 < A의 완료 초. 같은 초면 선후를 알 수 없어 `ambiguous`,
  B의 도착 초 > A의 완료 초면 `sequential`. 내림 오차를 따져도 앞의 둘은 확정이다.
- 감사 로그의 recall 줄에는 클라이언트 이름(#37)이 없다(retain-tags 줄에만 있다).
  그래서 클라이언트별 분해는 이 입력만으로는 불가능하고, 결과 JSON에도 넣지 않는다.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import re
import sys
from typing import Any, Dict, Iterable, List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import audit_log_reader  # noqa: E402  (같은 디렉터리, 표준 라이브러리만)
import hc_host_env  # noqa: E402  (같은 디렉터리, 표준 라이브러리만)

HORIZON_SECONDS = 3600          # 쌍으로 치는 최대 간격 (#76 정의)
WINDOW_SECONDS = 60             # 결과 캐시 후보 창
LOOP_MIN_PAIRS = 5              # 60초 이내 연쇄가 이 쌍 수 이상이면 loop (스크립트형 반복)
MAX_LATENCY_SECONDS = 120       # 원장 행에 붙일 audit 도착 줄을 찾는 최대 거리
RETAIN_TOOLS = ("retain", "sync_retain")
RECALL_REST = re.compile(r"/banks/([^/]+)/memories/recall/?$")
RETAIN_REST = re.compile(r"/banks/([^/]+)/memories/?$")

GAP_BUCKETS = ((0, 0, "0s"), (1, 1, "1s"), (2, 5, "2-5s"), (6, 10, "6-10s"), (11, 30, "11-30s"),
               (31, 60, "31-60s"), (61, 300, "61-300s"), (301, HORIZON_SECONDS, "301-3600s"))
LATENCY_BUCKETS = ((0, 1, "0-1s"), (2, 3, "2-3s"), (4, 7, "4-7s"), (8, 15, "8-15s"),
                   (16, 30, "16-30s"), (31, MAX_LATENCY_SECONDS, "31-120s"))


def parse_ts(value: Any) -> Optional[int]:
    """'2026-10-01T17:30:46Z' -> epoch seconds. None when unparsable."""
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return int(parsed.replace(tzinfo=dt.timezone.utc).timestamp())


def load_log(path: str) -> Tuple[List[dict], int, "audit_log_reader.LogRead"]:
    """(JSON object 줄들, 건너뛴 줄 수, 읽기 메타). 파일은 읽기 전용으로만 연다. 회전 세대(path.N.gz ... path.1.gz, path)를 오래된 순으로 잇고 줄 앞 NUL 은 걷는다(issue #177)."""
    read = audit_log_reader.read_log(path)
    rows: List[dict] = []
    skipped = 0
    for line in read.lines:
        try:
            obj = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if isinstance(obj, dict):
            rows.append(obj)
        else:
            skipped += 1
    return rows, skipped, read


def _ts_span(rows: List[dict]) -> Optional[Tuple[int, int]]:
    times = [t for t in (parse_ts(r.get("ts")) for r in rows) if t is not None]
    return (min(times), max(times)) if times else None


def _common_coverage(ledger_span, audit_span) -> Dict[str, Any]:
    """두 로그가 함께 덮는 구간. 원장과 감사 로그는 회전 시점이 달라 한쪽에만 있는 구간의 줄은 짝이 없어 unmatched 로 잡힌다.
    원장 ts 는 응답 완료 시각이고 감사 줄은 도착 시각이라(최대 MAX_LATENCY_SECONDS 앞선다) from 은 감사 로그 시작에 그만큼 더한다.
    겹치는 구간이 없으면 from/to 가 None 이다. 이 구간을 --since/--until 로 주면 그 오탐이 빠진다."""
    if ledger_span is None or audit_span is None:
        return {"from": None, "to": None}
    low = max(ledger_span[0], audit_span[0] + MAX_LATENCY_SECONDS)
    high = min(ledger_span[1], audit_span[1])
    if low > high:
        return {"from": None, "to": None}
    return {"from": _iso(low), "to": _iso(high)}


def _bucket(value: int, buckets: Iterable[Tuple[int, int, str]]) -> str:
    for low, high, label in buckets:
        if low <= value <= high:
            return label
    return "over"


def _hist(values: Iterable[int], buckets: Tuple[Tuple[int, int, str], ...]) -> Dict[str, int]:
    out = {label: 0 for _, _, label in buckets}
    out["over"] = 0
    for v in values:
        out[_bucket(v, buckets)] += 1
    if not out["over"]:
        del out["over"]
    return out


# --- audit parsing ------------------------------------------------------------------------

CONTEXT_TRIGGER_RECALL_PREFIXES = ("match:", "match-empty:", "recall-failed:")


def context_trigger_recalls(audit: List[dict]) -> List[dict]:
    """길목 주입(/context-triggers)이 게이트웨이 안에서 직접 돌린 recall. 원장에는 안 남는다.

    감사 줄은 그 recall이 끝난 뒤에 쓰이므로 ts는 완료 시각이다.
    """
    out = []
    for d in audit:
        ts = parse_ts(d.get("ts"))
        reason = d.get("reason")
        if (ts is not None and isinstance(d.get("context_trigger"), dict) and isinstance(reason, str)
                and reason.startswith(CONTEXT_TRIGGER_RECALL_PREFIXES)):
            out.append({"ts": ts, "identity": d.get("identity"), "bank": d.get("attempted_bank")})
    return out


def _audit_events(audit: List[dict]):
    """(recall 도착 줄, 거부된 recall 줄, retain 허용 줄, 거부된 retain 줄). 모두 정수 초로 정규화."""
    recalls, rejects, retains, retain_rejects = [], [], [], []
    for d in audit:
        ts = parse_ts(d.get("ts"))
        if ts is None:
            continue
        decision, method, tool = d.get("decision"), d.get("method"), d.get("tool")
        identity = d.get("identity")
        if method == "tools/call":
            bank = d.get("resolved_bank") or d.get("attempted_bank")
            if tool == "recall":
                row = {"ts": ts, "identity": identity, "bank": bank, "transport": "mcp"}
                if decision == "allow":
                    recalls.append(row)
                elif decision == "enforce-reject":
                    rejects.append(row)
            elif tool in RETAIN_TOOLS and decision in ("allow", "enforce-reject"):
                row = {"ts": ts, "identity": identity, "bank": bank, "transport": "mcp"}
                (retains if decision == "allow" else retain_rejects).append(row)
        elif method == "POST" and isinstance(tool, str) and decision in ("allow", "enforce-reject"):
            m = RECALL_REST.search(tool)
            if m:
                if decision == "allow":
                    recalls.append({"ts": ts, "identity": identity, "bank": m.group(1), "transport": "rest"})
                continue
            m = RETAIN_REST.search(tool)
            if m:
                row = {"ts": ts, "identity": identity, "bank": m.group(1), "transport": "rest"}
                (retains if decision == "allow" else retain_rejects).append(row)
    # REST retain은 요청당 allow 1줄인데 위반 items[i]마다 거부 줄이 하나씩 남는다(gateway/app.py).
    # 같은 (identity, bank, 초)의 REST 거부 줄은 한 요청으로 합쳐야 무관한 정상 retain의 allow 줄을 안 지운다.
    seen, unique = set(), []
    for row in retain_rejects:
        if row["transport"] == "rest":
            key = (row["identity"], row["bank"], row["ts"])
            if key in seen:
                continue
            seen.add(key)
        unique.append(row)
    return recalls, rejects, retains, unique


def _drop_rejected(recalls: List[dict], rejects: List[dict]) -> List[dict]:
    """enforce-reject는 allow 줄 뒤에 같은 초로 따라온다. 짝이 되는 allow 줄은 실행되지 않았다.

    recall은 원장에 안 남고, retain은 아무것도 쓰지 않으므로 쌍을 끊지 못한다. 거부 줄 하나가
    allow 줄 하나를 지운다(같은 identity, bank, transport, 거부 초 -1 ~ 거부 초).
    """
    taken = set()
    for rej in rejects:
        best = None
        for i, rc in enumerate(recalls):
            if i in taken or rc["identity"] != rej["identity"] or rc["bank"] != rej["bank"]:
                continue
            if rc["transport"] != rej["transport"] or not (rej["ts"] - 1 <= rc["ts"] <= rej["ts"]):
                continue
            best = i if best is None or rc["ts"] >= recalls[best]["ts"] else best
        if best is not None:
            taken.add(best)
    return [rc for i, rc in enumerate(recalls) if i not in taken]


def join_ledger_to_audit(ledger_rows: List[dict], recalls: List[dict]):
    """원장 행마다 (identity, bank, transport)가 같은 allow 줄을 하나씩 붙인다.

    같은 채널의 요청은 도착 순서대로 끝난다고 보고(순서 보존), 완료 순서의 뒤쪽 행부터
    "완료 초 이하, MAX_LATENCY_SECONDS 이내에서 가장 늦게 도착한, 뒤 행이 쓰지 않은 줄"을 붙인다.
    앞 요청이 끝난 같은 초에 다음 요청이 도착하는 연속 호출에서도 줄이 한 칸씩 밀리지 않고,
    원장에 안 남은 실패 요청의 줄은 건너뛴다.

    반환: (행 index -> 도착 초, 쓰이지 않은 allow 줄 리스트, 행 index -> 가능한 가장 이른 도착 초).
    세 번째 값은 같은 순서 보존 가정 아래 다른 유효한 결합이 그 행에 줄 수 있는 가장 이른 도착 시각이다.
    짝 없는 줄 하나가 있으면 그 위로 이어진 행들의 줄이 한 칸씩 밀려 있을 수 있어(연쇄), 짝 없는 줄이
    120초 넘게 떨어져 있어도 같은 연쇄 안의 행은 이른 도착을 받을 수 있다.
    순서 보존 가정이 깨지는 경우(오래 걸린 요청이 뒤에 온 요청에 추월당함)는 한계로 보고서에 적는다.
    """
    channels: Dict[Tuple[Any, Any, str], List[dict]] = collections.defaultdict(list)
    for rc in recalls:
        channels[(rc["identity"], rc["bank"], rc["transport"])].append(dict(rc, used=False))
    for lines in channels.values():
        lines.sort(key=lambda r: r["ts"])
    arrival: Dict[int, int] = {}
    earliest: Dict[int, int] = {}
    rows_by_channel: Dict[Tuple[Any, Any, str], List[int]] = collections.defaultdict(list)
    for i, row in enumerate(ledger_rows):
        rows_by_channel[(row.get("identity"), row.get("bank"), row.get("transport"))].append(i)
    for chan, idxs in rows_by_channel.items():
        lines = channels.get(chan, [])
        idxs.sort(key=lambda i: (ledger_rows[i]["_t"], i))
        bound = len(lines)                       # 이보다 작은 index의 줄만 쓸 수 있다
        line_of: Dict[int, int] = {}             # 행 index -> 붙은 줄 index
        owner_of: Dict[int, int] = {}            # 줄 index -> 붙은 행 index
        for i in reversed(idxs):
            done = ledger_rows[i]["_t"]
            j = bound - 1
            while j >= 0 and lines[j]["ts"] > done:
                j -= 1
            if j < 0 or lines[j]["ts"] < done - MAX_LATENCY_SECONDS:
                continue
            lines[j]["used"] = True
            arrival[i] = lines[j]["ts"]
            line_of[i], owner_of[j] = j, i
            bound = j
        for i, j in line_of.items():
            # j에서 아래로 내려가며 한 칸씩 내려갈 수 있는 행의 연쇄 안에 짝 없는 줄이 몇 개인지 센다.
            free, q = 0, j
            while q >= 0:
                owner = owner_of.get(q)
                if owner is None:
                    free += 1
                elif q == 0 or lines[q - 1]["ts"] < ledger_rows[owner]["_t"] - MAX_LATENCY_SECONDS:
                    break                        # 이 행은 더 이른 줄로 못 옮겨진다: 연쇄 끝
                q -= 1
            k = j - free
            floor = ledger_rows[i]["_t"] - MAX_LATENCY_SECONDS
            while lines[k]["ts"] < floor:
                k += 1
            earliest[i] = lines[k]["ts"]
    unused = [ln for lines in channels.values() for ln in lines if not ln["used"]]
    return arrival, unused, earliest


# --- pairing ------------------------------------------------------------------------------

def _request_key(row: dict) -> Tuple[Any, ...]:
    tags = row.get("tags")
    tag_key = tuple(sorted({t for t in tags if isinstance(t, str)})) if isinstance(tags, list) else ()   # 집합 기준(#109)
    return (str(row.get("query", "")).strip(), tag_key, row.get("tags_match") or "any")


def _retained_between(retain_ts: List[int], low: int, high: int) -> bool:
    """low 초과, high 이하 구간에 retain 허용 줄이 있는가."""
    return any(low < t <= high for t in retain_ts)


def _relation(a: dict, b: dict) -> str:
    sa, sb = a.get("session_id"), b.get("session_id")
    if not sa or not sb:
        return "unknown"
    return "same" if sa == sb else "different"


def _concurrency(a_done: int, b_arrival: Optional[int]) -> str:
    if b_arrival is None:
        return "unjoined"
    if b_arrival < a_done:
        return "concurrent"
    return "ambiguous" if b_arrival == a_done else "sequential"


def _query_only_key(row: dict) -> Tuple[Any, ...]:
    return (str(row.get("query", "")).strip(),)


def find_pairs(rows: List[dict], reusable: List[int], retain_by_bank: Dict[Any, List[int]],
               key_fn=_request_key) -> Dict[str, Any]:
    """reusable(시간순 index 리스트)에서 쌍과 60초 이내 연쇄를 찾는다."""
    last_by_key: Dict[Tuple[Any, ...], int] = {}
    pairs: List[Tuple[int, int]] = []
    chain_of: Dict[int, int] = {}               # b index -> 60초 이내 연쇄 번호
    chain_end: Dict[int, int] = {}              # 연쇄의 마지막 행 index -> 연쇄 번호
    chain_len: Dict[int, int] = collections.defaultdict(int)
    retained = retained_60 = 0
    for i in reusable:
        key = key_fn(rows[i])
        a = last_by_key.get(key)
        last_by_key[key] = i
        if a is None:
            continue
        gap = rows[i]["_t"] - rows[a]["_t"]
        if gap > HORIZON_SECONDS:
            continue
        if _retained_between(retain_by_bank.get(rows[i].get("bank"), []), rows[a]["_t"], rows[i]["_t"]):
            retained += 1
            retained_60 += gap <= WINDOW_SECONDS
            continue
        pairs.append((a, i))
        if gap <= WINDOW_SECONDS:
            cid = chain_end.pop(a, None)
            if cid is None:
                cid = len(chain_len) + 1
            chain_len[cid] += 1
            chain_of[i] = cid
            chain_end[i] = cid
    return {"pairs": pairs, "chain_of": chain_of, "chain_len": chain_len,
            "retain_broken": retained, "retain_broken_within_60s": retained_60}


def analyze(ledger: List[dict], audit: List[dict], *, primary: Optional[str] = None,
            since: Optional[int] = None, until: Optional[int] = None) -> Dict[str, Any]:
    rows: List[dict] = []
    for r in ledger:
        t = parse_ts(r.get("ts"))
        if t is None or not isinstance(r.get("query"), str):
            continue
        if (since is not None and t < since) or (until is not None and t > until):
            continue
        rows.append(dict(r, _t=t))
    recalls, rejects, retains, retain_rejects = _audit_events(audit)
    retains = _drop_rejected(retains, retain_rejects)
    if rows:
        # 원장은 도입 시점부터만 있다. 그 이전의 allow 줄은 짝이 없는 게 당연하므로 뺀다.
        low = min(r["_t"] for r in rows) - MAX_LATENCY_SECONDS
        high = max(r["_t"] for r in rows)
        recalls = [rc for rc in recalls if low <= rc["ts"] <= high]
        rejects = [rj for rj in rejects if low <= rj["ts"] <= high]
    recalls = _drop_rejected(recalls, rejects)
    arrival, unused, earliest = join_ledger_to_audit(rows, recalls)
    hook_recalls = context_trigger_recalls(audit)
    retain_by_bank: Dict[Any, List[int]] = collections.defaultdict(list)
    for rt in retains:
        retain_by_bank[rt["bank"]].append(rt["ts"])

    by_pop: Dict[str, List[int]] = collections.defaultdict(list)
    for i, r in enumerate(rows):
        by_pop[f"{r.get('identity')}/{r.get('bank')}"].append(i)

    populations: Dict[str, Any] = {}
    for pop, idxs in sorted(by_pop.items()):
        idxs.sort(key=lambda i: (rows[i]["_t"], i))
        reusable = [i for i in idxs if rows[i].get("reusable") is not False]
        found = find_pairs(rows, reusable, retain_by_bank)
        pairs, chain_of, chain_len = found["pairs"], found["chain_of"], found["chain_len"]
        loose = find_pairs(rows, reusable, retain_by_bank, key_fn=_query_only_key)["pairs"]
        within = [(a, b) for a, b in pairs if rows[b]["_t"] - rows[a]["_t"] <= WINDOW_SECONDS]
        matched = [i for i in idxs if i in arrival]
        populations[pop] = {
            "rows": len(idxs),
            "period": _period([rows[i]["_t"] for i in idxs]),
            "reusable_rows": len(reusable),
            "non_reusable_rows": len(idxs) - len(reusable),
            "rows_without_session": sum(1 for i in idxs if not rows[i].get("session_id")),
            "distinct_sessions": len({rows[i]["session_id"] for i in idxs if rows[i].get("session_id")}),
            "repeat_pairs": len(pairs),
            "within_60s": len(within),
            "over_60s": len(pairs) - len(within),
            "same_session_pairs": sum(1 for a, b in pairs if _relation(rows[a], rows[b]) == "same"),
            "repeats_broken_by_retain": found["retain_broken"],
            "repeats_broken_by_retain_within_60s": found["retain_broken_within_60s"],
            "gap_hist": _hist((rows[b]["_t"] - rows[a]["_t"] for a, b in pairs), GAP_BUCKETS),
            "query_only_key": {
                "repeat_pairs": len(loose),
                "within_60s": sum(1 for a, b in loose if rows[b]["_t"] - rows[a]["_t"] <= WINDOW_SECONDS),
                "same_session_pairs": sum(1 for a, b in loose if _relation(rows[a], rows[b]) == "same")},
            "context_trigger": _context_trigger_overlap(rows, idxs, hook_recalls, pop),
            "latency_hist": _hist((rows[i]["_t"] - arrival[i] for i in idxs if i in arrival), LATENCY_BUCKETS),
            "by_day": _by_day(rows, reusable, pairs, chain_of, chain_len),
            "join": {"ledger_rows_matched": len(matched), "ledger_rows_unmatched": len(idxs) - len(matched),
                     "audit_recall_lines_without_ledger_row": sum(
                         1 for ln in unused if f"{ln['identity']}/{ln['bank']}" == pop)},
            "within_60s_detail": _detail(rows, within, arrival, unused, earliest, chain_of, chain_len),
        }
    return {
        "primary": primary,
        "period": _period([r["_t"] for r in rows]),          # 전체 population. 결론은 populations[...]["period"]를 쓴다
        "params": {"horizon_s": HORIZON_SECONDS, "window_s": WINDOW_SECONDS,
                   "max_join_latency_s": MAX_LATENCY_SECONDS, "loop_min_pairs": LOOP_MIN_PAIRS},
        "populations": populations,
    }


def _context_trigger_overlap(rows, idxs, hook_recalls, pop) -> Dict[str, int]:
    """길목 주입 recall(원장 밖) 중 끝난 뒤 60초 안에 같은 (identity, bank)의 원장 recall이 이어진 수.

    쿼리를 비교하지 못하므로 훅 + 모델 중복의 상한이다(쿼리가 달라도 센다).
    """
    if not idxs:
        return {"hook_recalls": 0, "followed_by_ledger_recall_within_60s": 0}
    low, high = min(rows[i]["_t"] for i in idxs), max(rows[i]["_t"] for i in idxs)
    mine = [h for h in hook_recalls if f"{h['identity']}/{h['bank']}" == pop and low <= h["ts"] <= high]
    done = [rows[i]["_t"] for i in idxs]
    return {"hook_recalls": len(mine),
            "followed_by_ledger_recall_within_60s": sum(
                1 for h in mine if any(0 <= t - h["ts"] <= WINDOW_SECONDS for t in done))}


def _by_day(rows, reusable, pairs, chain_of, chain_len) -> Dict[str, Dict[str, int]]:
    """UTC 날짜별 reusable 행, 쌍(1시간 이내), 60초 이내 쌍, 그중 loop. 날짜는 쌍의 B 행 기준."""
    out: Dict[str, Dict[str, int]] = collections.defaultdict(
        lambda: {"reusable_rows": 0, "repeat_pairs": 0, "within_60s": 0, "loop": 0})
    for i in reusable:
        out[_iso(rows[i]["_t"])[:10]]["reusable_rows"] += 1
    for a, b in pairs:
        day = out[_iso(rows[b]["_t"])[:10]]
        day["repeat_pairs"] += 1
        if rows[b]["_t"] - rows[a]["_t"] <= WINDOW_SECONDS:
            day["within_60s"] += 1
            day["loop"] += chain_len[chain_of[b]] >= LOOP_MIN_PAIRS
    return dict(sorted(out.items()))


def _period(times: List[int]) -> Dict[str, Any]:
    if not times:
        return {"ledger_first": None, "ledger_last": None, "days": 0}
    return {"ledger_first": _iso(min(times)), "ledger_last": _iso(max(times)),
            "days": round((max(times) - min(times)) / 86400, 1)}


def _iso(t: int) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pair_class(rows, a: int, b: int, chain_pairs: int) -> str:
    """loop: 60초 이내 연쇄가 LOOP_MIN_PAIRS 쌍 이상(스크립트형 반복). 그 밖은 세션 관계로 나눈다."""
    if chain_pairs >= LOOP_MIN_PAIRS:
        return "loop"
    return {"same": "same_session", "different": "cross_session", "unknown": "unknown_session"}[
        _relation(rows[a], rows[b])]


def _detail(rows, within, arrival, unused, earliest, chain_of, chain_len) -> Dict[str, Any]:
    classes = [pair_class(rows, a, b, chain_len[chain_of[b]]) for a, b in within]
    by_class: Dict[str, Any] = {}
    for name in ("loop", "same_session", "cross_session", "unknown_session"):
        members = [(a, b) for (a, b), c in zip(within, classes) if c == name]
        by_class[name] = {
            "total": len(members),
            "by_concurrency": dict(sorted(collections.Counter(
                _concurrency(rows[a]["_t"], arrival.get(b)) for a, b in members).items())),
            "by_transport": dict(sorted(collections.Counter(rows[b].get("transport") for _, b in members).items())),
            "distinct_b_sessions": len({rows[b].get("session_id") for _, b in members if rows[b].get("session_id")}),
            "gap_hist": _hist((rows[b]["_t"] - rows[a]["_t"] for a, b in members), GAP_BUCKETS),
        }
    by_transport = collections.Counter(rows[b].get("transport") for _, b in within)
    by_rel = collections.Counter(_relation(rows[a], rows[b]) for a, b in within)
    conc = [(_concurrency(rows[a]["_t"], arrival.get(b))) for a, b in within]
    by_conc = collections.Counter(conc)
    rel_x_conc: Dict[str, Dict[str, int]] = collections.defaultdict(lambda: collections.defaultdict(int))
    tr_x_rel: Dict[str, Dict[str, int]] = collections.defaultdict(lambda: collections.defaultdict(int))
    for (a, b), c in zip(within, conc):
        rel = _relation(rows[a], rows[b])
        rel_x_conc[rel][c] += 1
        tr_x_rel[str(rows[b].get("transport"))][rel] += 1
    keys = collections.Counter(_request_key(rows[b]) for _, b in within)
    sessions = collections.Counter(rows[b].get("session_id") for _, b in within if rows[b].get("session_id"))
    a_lat = [rows[a]["_t"] - arrival[a] for a, _ in within if a in arrival]
    b_lat = [rows[b]["_t"] - arrival[b] for _, b in within if b in arrival]
    # transport가 다른 쌍은 B가 A보다 먼저 도착할 수 있다: 음수는 0초 칸에 넣는다(버킷 밖 'over'로 새지 않게).
    inter = [max(0, arrival[b] - arrival[a]) for a, b in within if a in arrival and b in arrival]
    inter_concurrent = [max(0, arrival[b] - arrival[a]) for (a, b), c in zip(within, conc)
                        if c == "concurrent" and a in arrival]
    # 앞 시도가 원장에 안 남은 채 B 직전(30초 안)에 같은 채널로 들어온 allow 줄: 재시도 흔적의 상한.
    failed_before = 0
    for _, b in within:
        if b not in arrival:
            continue
        row = rows[b]
        failed_before += any(
            ln["identity"] == row.get("identity") and ln["bank"] == row.get("bank")
            and ln["transport"] == row.get("transport") and arrival[b] - 30 <= ln["ts"] <= arrival[b]
            for ln in unused)
    # 결합 오차의 상한: B가 다른 유효한 결합에서 받을 수 있는 가장 이른 도착(earliest)이 A 완료보다 이르면
    # 그 쌍은 concurrent였을 수 있다. 짝 없는 줄(실패 요청의 줄이든 밀려난 줄이든)이 B 위로 이어진 연쇄에 있을
    # 때만 earliest가 도착 줄보다 이르다. 결합이 안 된 쌍도 동시였을 수 있으므로 위험으로 센다. 확정 동시 수에
    # 더한 값이 동시 중복 수의 상한이다.
    at_risk = 0
    for (a, b), c in zip(within, conc):
        if c == "concurrent":
            continue
        at_risk += b not in earliest or earliest[b] < rows[a]["_t"]
    return {
        "total": len(within),
        "concurrent_upper_bound_with_unmatched_lines": by_conc.get("concurrent", 0) + at_risk,
        "by_class": by_class,
        "chain_pair_counts": sorted(chain_len.values(), reverse=True),
        "by_transport": dict(sorted(by_transport.items())),
        "by_session_relation": dict(sorted(by_rel.items())),
        "by_concurrency": dict(sorted(by_conc.items())),
        "session_relation_x_concurrency": {k: dict(sorted(v.items())) for k, v in sorted(rel_x_conc.items())},
        "transport_x_session_relation": {k: dict(sorted(v.items())) for k, v in sorted(tr_x_rel.items())},
        "distinct_request_keys": len(keys),
        "max_pairs_per_key": max(keys.values()) if keys else 0,
        "distinct_b_sessions": len(sessions),
        "pairs_per_session_top5": sorted(sessions.values(), reverse=True)[:5],
        "gap_hist": _hist((rows[b]["_t"] - rows[a]["_t"] for a, b in within), GAP_BUCKETS),
        "a_latency_hist": _hist(a_lat, LATENCY_BUCKETS),
        "b_latency_hist": _hist(b_lat, LATENCY_BUCKETS),
        "interarrival_hist": _hist(inter, GAP_BUCKETS),
        "concurrent_interarrival_hist": _hist(inter_concurrent, GAP_BUCKETS),
        "failed_attempt_within_30s_before_b": failed_before,
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="중복 recall 출처 분석 (읽기 전용, 집계 수치만 출력)")
    ap.add_argument("--ledger", required=True, help="recall 원장 JSONL (audit/recall-queries.log)")
    ap.add_argument("--audit", required=True, help="게이트웨이 감사 로그 JSONL (audit/gateway.log)")
    ap.add_argument("--primary", default=None,
                    help="결론에 쓸 identity/bank (기본: .env.host.local 의 HC_OWNER/HC_PERSONAL_BANK)")
    ap.add_argument("--since", help="이 UTC 시각(예: 2026-09-21T00:00:00Z) 이후 원장 행만")
    ap.add_argument("--until", help="이 UTC 시각 이전 원장 행만. 운영 로그는 계속 자라므로 보고서는 고정 시각으로 낸다")
    args = ap.parse_args(argv)
    if args.primary is None:
        try:
            cfg = hc_host_env.require("HC_OWNER", "HC_PERSONAL_BANK")
        except hc_host_env.ConfigMissing as e:
            print(f"DUPLICATE_RECALL_FAIL - {e} (set it in .env.host.local or pass --primary identity/bank)",
                  file=sys.stderr)
            return 2
        args.primary = f"{cfg['HC_OWNER']}/{cfg['HC_PERSONAL_BANK']}"
    ledger, ledger_skipped, ledger_read = load_log(args.ledger)
    audit, audit_skipped, audit_read = load_log(args.audit)
    bounds = {}
    for name in ("since", "until"):
        raw = getattr(args, name)
        bounds[name] = parse_ts(raw) if raw else None
        if raw and bounds[name] is None:
            ap.error(f"--{name}은 2026-10-01T17:30:46Z 형식이어야 합니다")
    result = analyze(ledger, audit, primary=args.primary, **bounds)
    result["input"] = {"ledger_lines": len(ledger), "ledger_skipped": ledger_skipped,
                       "audit_lines": len(audit), "audit_skipped": audit_skipped}
    if ledger_read.extras or audit_read.extras:     # 회전 세대·NUL 이 없으면 기존 출력 그대로
        result["input"].update({
            "ledger_log": ledger_read.info(), "audit_log": audit_read.info(),
            "common_coverage": _common_coverage(_ts_span(ledger), _ts_span(audit))})
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
