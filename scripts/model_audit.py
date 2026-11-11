#!/usr/bin/env python3
"""model_audit.py - weekly mental-model audit (issues #10, #11).

Three signals, reported in this priority order (notify-then-approve: this script
NEVER creates, refreshes or deletes a model):

1. DEMAND (primary, #11). reflect calls of the last WINDOW_DAYS read from the
   gateway audit log. `reflect-demand` lines (metadata only: tags asked, mental
   models that served the call, based_on state) give the tag distribution and the
   model hit rate; a tag reflect asked for that no live model covers is a
   first-priority promotion candidate. Plain `allow` lines for reflect predate the
   telemetry (or are batched siblings) and only count as calls with unknown tags.
   based_on "unknown" (caller did not request based_on) is reported separately -
   it is not a miss.
2. SIZE GUARD (#11). Live model content above SIZE_GUARD_CHARS (15,000) is
   flagged so the 45K/77K bloat the split ended cannot silently return.
3. SUPPLY (secondary, #10). project:*/topic:* tags whose kind:rule + kind:decision
   memories are not covered by any live model and exceed THRESHOLD (15). A tag's
   total from /tags is a cheap upper bound, so tags below the threshold are
   skipped without extra queries; survivors get the rule/decision intersection
   with memories that also carry a covered seed tag excluded.
4. INJECTION (observability, #12). Counts of the 길목 주입 machinery from the same
   audit log: /context-triggers fires (matched vs miss, empty recall results kept separate),
   contradiction warnings, recurrence drafts, and successful correction retains associated
   with recently injected memory ids. The correction association uses served ids + reranker
   evidence; it is not semantically confirmed truth or an estimate of the false-positive
   rate. Report + one log token, no threshold.

Bank and token owner come from .env.host.local (HC_PERSONAL_BANK, HC_OWNER; issue #186) and are
never defaulted: a missing key logs `MODEL_AUDIT_FAIL - config_missing <key>`.

Delivery: Discord bot REST (HC_NOTIFY_DISCORD_BOT_TOKEN / _CHANNEL_ID from the environment, else
the legacy GGG_DISCORD_* names - the wrapper resolves them from .env.host.local or ~/.env line by
line; nothing is committed) with osascript local-notification fallback. Log tokens: MODEL_AUDIT_OK candidates=<n>
demand=<n> size_warn=<n> reflect_calls=<n> / MODEL_AUDIT_DEMAND <tag> <calls> /
MODEL_AUDIT_SIZE_WARN <id> <chars> / MODEL_AUDIT_FAIL <reason> /
MODEL_AUDIT_NOTIFY_FAILED. JSON report to stdout, narration to stderr.
"""
import collections
import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import audit_log_reader  # noqa: E402  (같은 디렉터리, 표준 라이브러리만)
import hc_host_env  # noqa: E402  (같은 디렉터리, 표준 라이브러리만)

BASE_URL = os.environ.get("HC_GATEWAY_URL", "http://127.0.0.1:8888")
try:
    BANK = hc_host_env.get("HC_PERSONAL_BANK")   # None when unset; main() fails with config_missing
except hc_host_env.ConfigMissing:                # unreadable .env.host.local: main() reports config_invalid
    BANK = None
THRESHOLD = int(os.environ.get("HC_AUDIT_THRESHOLD", "15"))
WINDOW_DAYS = int(os.environ.get("HC_AUDIT_WINDOW_DAYS", "7"))
SIZE_GUARD_CHARS = int(os.environ.get("HC_AUDIT_SIZE_GUARD_CHARS", "15000"))
PREFIXES = ("project:", "topic:")
KINDS = ("kind:rule", "kind:decision")
FETCH_LIMIT = 500
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Host-side path of the gateway's append-only audit log (docker-compose mounts ./audit:/audit).
GATEWAY_AUDIT_LOG = os.environ.get("HC_GATEWAY_AUDIT_LOG", os.path.join(ROOT, "audit", "gateway.log"))
# Drafts still awaiting approval (the log counts drafts CREATED, which may since be deleted).
PENDING_TRIGGERS = os.path.join(os.path.dirname(GATEWAY_AUDIT_LOG), "pending-triggers.jsonl")
# Approve/reject records (issue #276, scripts/trigger_drafts.py resolve). The pending file is append-only
# for the gateway, so a draft is "processed" when its last record here says so.
TRIGGER_DECISIONS = os.path.join(os.path.dirname(GATEWAY_AUDIT_LOG), "trigger-decisions.jsonl")
# Same value as gateway.audit.TAG_LOG_MAX: the gateway cuts every tag on a reflect-demand line to
# this length (issue #103). Copied, not imported - this script runs standalone (scripts/ on sys.path,
# not the repo root); tests/test_injection_audit.py pins the two values together.
TAG_LOG_MAX = 256


def log(msg):
    print(msg, file=sys.stderr)


def read_token():
    owner = hc_host_env.require("HC_OWNER")["HC_OWNER"]
    acl = json.load(open(os.path.join(ROOT, "secrets", "acl.json")))
    for tok, p in acl["tokens"].items():
        if p.get("identity") == owner:
            return tok
    raise RuntimeError("no %s token in secrets/acl.json" % owner)


def api(token, method, path, body=None):
    req = urllib.request.Request(
        BASE_URL + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
        method=method,
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def items_of(payload):
    if isinstance(payload, dict):
        return payload.get("items") or []
    return payload or []


def tagged_memories(token, tag):
    """All memories carrying `tag`, paginated.

    Single-tag query + client-side kind intersection. The gateway's repeated
    query-param collapse that forced this was fixed in cef5bc5 (multi_items());
    the client-side intersection is kept because it is harmless and lets the
    covered-seed exclusion below run on the same item list.
    """
    out, offset = [], 0
    while True:
        query = urllib.parse.urlencode(
            [("tags", tag), ("tags_match", "any_strict"),
             ("limit", FETCH_LIMIT), ("offset", offset)])
        items = items_of(api(token, "GET",
                             f"/v1/default/banks/{BANK}/memories/list?{query}"))
        out.extend(items)
        if len(items) < FETCH_LIMIT:
            return out
        offset += FETCH_LIMIT


def _read_gateway_log():
    """게이트웨이 감사 로그를 회전 세대(.N.gz)까지 이어 읽는다. 줄 앞 NUL 은 걷고, 손상 .gz 는 읽히는 앞부분만 쓴다(issue #177)."""
    return audit_log_reader.read_log(GATEWAY_AUDIT_LOG, errors="replace")   # 예전 open() 과 같은 디코딩


def _log_read_info(read, earliest, since_ts):
    """회전 세대·NUL·손상이 있을 때만 리포트에 넣는 읽기 메타. coverage_short 는 회전 세대를 읽었는데도 가장 이른 ts 가 창 시작보다
    늦은 경우(= 창 앞쪽이 이미 지워졌을 수 있다)다. 회전 세대가 없는 로그가 늦게 시작하는 경우는 지금처럼 표시하지 않는다."""
    info = read.info()
    info["earliest_ts"] = earliest
    info["coverage_short"] = bool(read.rotated and earliest is not None and earliest > since_ts)
    return info


def _is_reflect_call(entry):
    tool = entry.get("tool")
    if entry.get("decision") != "allow" or not isinstance(tool, str):
        return False
    return tool == "reflect" or tool.endswith("/reflect")


def injection_signal(since):
    """Injection counters and subsequent correction associations for the audited bank.

    Each injection-correction event represents one successful correction retain. Repeated
    trigger/memory ids within an event count once; the same memory in separate events adds
    to its event count but counts once in unique_memories across the whole window. Trigger
    and memory breakdowns are independent: an event containing several of each does not
    establish a complete mapping between those triggers and memories.
    """
    since_ts = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    fires = collections.Counter()
    served = collections.Counter()
    empty = collections.Counter()
    recall_failed = 0
    warns = {"checked": 0, "with_precedent": 0}
    drafts = collections.Counter()
    corrections = 0
    corrected_triggers = collections.Counter()
    corrected_memories = collections.Counter()

    def ids(value):
        return {item for item in value if isinstance(item, str) and item.strip()} \
            if isinstance(value, list) else set()

    if not os.path.exists(GATEWAY_AUDIT_LOG):
        return {"available": False, "since": since_ts}
    read = _read_gateway_log()
    earliest = None
    for line in read.lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("ts"), str) \
                and (earliest is None or entry["ts"] < earliest):
            earliest = entry["ts"]
        if not isinstance(entry, dict) or str(entry.get("ts", "")) < since_ts:
            continue
        if entry.get("resolved_bank") not in (None, BANK):
            continue
        decision = entry.get("decision")
        reason = str(entry.get("reason", ""))
        if decision == "context-trigger":
            if reason.startswith("match:"):
                fires["matched"] += 1
                served[reason.split(":", 1)[1]] += 1
            elif reason.startswith("match-empty:"):
                fires["matched"] += 1
                empty[reason.split(":", 1)[1]] += 1
            elif reason.startswith("recall-failed:"):
                fires["matched"] += 1
                recall_failed += 1
            elif reason == "miss":
                fires["miss"] += 1
        elif decision == "contradiction-warn" and reason != "recall-failed":
            warns["checked"] += 1
            if reason.startswith("precedent:"):
                warns["with_precedent"] += 1
        elif decision == "recurrence" and reason.startswith("draft:"):
            drafts[reason.split(":", 1)[1]] += 1
        elif decision == "injection-correction":
            evidence = entry.get("injection_correction")
            if not isinstance(evidence, dict):
                continue
            trigger_ids = ids(evidence.get("trigger_ids"))
            memory_ids = ids(evidence.get("memory_ids"))
            if not trigger_ids or not memory_ids:
                continue
            corrections += 1
            corrected_triggers.update(sorted(trigger_ids))
            corrected_memories.update(sorted(memory_ids))
    total = fires["matched"] + fires["miss"]
    report = {
        "available": True, "since": since_ts,
        "context_trigger_calls": total, "matched": fires["matched"], "miss": fires["miss"],
        "match_rate": (round(fires["matched"] / total, 3) if total else None),
        "recall_failed": recall_failed,
        "empty": sum(empty.values()),
        "empty_by_trigger": [{"id": t, "fires": n} for t, n in empty.most_common()],
        "served_by_trigger": [{"id": t, "fires": n} for t, n in served.most_common()],
        "contradiction": warns,
        "recurrence_drafts": dict(drafts),
        "injection_corrections": {
            "events": corrections,
            "unique_memories": len(corrected_memories),
            "by_trigger": [{"id": t, "events": n} for t, n in corrected_triggers.most_common()],
            "by_memory": [{"id": m, "events": n} for m, n in corrected_memories.most_common()],
        },
    }
    if read.extras:
        report["log_read"] = _log_read_info(read, earliest, since_ts)
    return report


def _is_get_mental_model_call(entry):
    tool = entry.get("tool")
    if entry.get("decision") != "allow" or not isinstance(tool, str):
        return False
    return tool == "get_mental_model" or tool.endswith("/get_mental_model")


def demand_signal(covered, since, live_model_ids=None):
    """Aggregate reflect and get_mental_model usage since `since` from the gateway audit log (metadata only).

    `live_model_ids` (the ids of the models that exist now) is optional: given, the report also counts
    get_mental_model requests for an id outside it (`mental_model_reads.nonexistent`); None = not computed."""
    since_ts = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    calls = 0
    events = []
    # The log holds tags cut to TAG_LOG_MAX, so compare against model tags cut the same way;
    # otherwise a model tag over that length would look uncovered (issue #103).
    covered = {c[:TAG_LOG_MAX] for c in covered if isinstance(c, str)}
    if not os.path.exists(GATEWAY_AUDIT_LOG):
        log(f"gateway audit log missing: {GATEWAY_AUDIT_LOG} - demand signal unavailable")
        return {"available": False, "log": GATEWAY_AUDIT_LOG, "since": since_ts}
    read = _read_gateway_log()
    earliest = None
    mm_calls = 0
    mm_by_id = collections.Counter()    # (id, cut by the gateway's field bound?) -> calls
    mm_unknown = 0
    for line in read.lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("ts"), str) \
                and (earliest is None or entry["ts"] < earliest):
            earliest = entry["ts"]
        if not isinstance(entry, dict) or str(entry.get("ts", "")) < since_ts:
            continue
        if entry.get("resolved_bank") not in (None, BANK):
            continue
        if _is_get_mental_model_call(entry):
            mm_calls += 1
            mid = entry.get("mental_model_id")
            if isinstance(mid, str) and mid:
                # A cut id is only a prefix of what was asked (the gateway bounds the field, issue #117):
                # it can neither be matched against the live ids nor stand for one model.
                mm_by_id[(mid, entry.get("mental_model_id_truncated") is True)] += 1
            else:
                mm_unknown += 1     # a line from before issue #238, or a call that sent no usable id
        elif _is_reflect_call(entry):
            calls += 1
        elif entry.get("decision") == "reflect-demand" and isinstance(entry.get("reflect"), dict):
            events.append(entry["reflect"])

    by_tag = collections.Counter()
    hits_by_tag = collections.Counter()
    miss_by_tag = collections.Counter()
    served = collections.Counter()
    based_on = collections.Counter()
    untagged = 0
    truncated = 0
    for ev in events:
        omitted = ev.get("tags_omitted")
        if isinstance(omitted, int) and omitted > 0:
            truncated += 1      # 게이트웨이는 reflect.tags 를 앞 64개만 싣는다(issue #103)
        state = ev.get("based_on") or "unknown"
        based_on[state] += 1
        for mid in ev.get("mental_models") or []:
            served[mid] += 1
        tags = [t for t in (ev.get("tags") or []) if isinstance(t, str)]
        if not tags:
            untagged += 1
        for t in tags:
            by_tag[t] += 1
            if state == "hit":
                hits_by_tag[t] += 1
            elif state == "miss":
                miss_by_tag[t] += 1
    known = based_on["hit"] + based_on["miss"]
    candidates = [
        {"tag": t, "reflect_calls": n, "covered": False}
        for t, n in by_tag.most_common() if t not in covered
    ]
    # A covered tag that reflect asked for and got NO model back (based_on present, empty):
    # tags_match/all_strict mismatch or a stale model. "unknown" calls never count here.
    covered_but_miss = [
        {"tag": t, "reflect_calls": n, "hits": hits_by_tag[t], "misses": miss_by_tag[t]}
        for t, n in by_tag.most_common()
        if known and t in covered and miss_by_tag[t] > 0
    ]
    report = {
        "available": True, "log": GATEWAY_AUDIT_LOG, "since": since_ts,
        "reflect_calls": calls, "instrumented": len(events),
        "legacy_uninstrumented": max(calls - len(events), 0),
        "based_on": {"hit": based_on["hit"], "miss": based_on["miss"], "unknown": based_on["unknown"]},
        "model_hit_rate": (round(based_on["hit"] / known, 3) if known else None),
        "untagged_calls": untagged,
        "truncated_tag_lines": truncated,    # 0보다 크면 일부 호출의 64번째 뒤 태그가 수요에서 빠졌다
        "tags": [{"tag": t, "reflect_calls": n, "hits": hits_by_tag[t], "covered": t in covered}
                 for t, n in by_tag.most_common()],
        "models_served": [{"id": m, "calls": n} for m, n in served.most_common()],
        "candidates": candidates,
        "covered_but_miss": covered_but_miss,
        "mental_model_reads": {
            "calls": mm_calls,
            "by_model": [{"id": m, "calls": n, **({"truncated": True} if cut else {})}
                         for (m, cut), n in mm_by_id.most_common()],
            "unknown": mm_unknown,
            "nonexistent": (None if live_model_ids is None
                            else sum(n for (m, cut), n in mm_by_id.items()
                                     if not cut and m not in live_model_ids)),
        },
    }
    if read.extras:
        report["log_read"] = _log_read_info(read, earliest, since_ts)
    return report


def size_guard(token, models):
    """Content length per live model; anything above SIZE_GUARD_CHARS is a warning."""
    sizes, warnings = [], []
    for m in models:
        detail = api(token, "GET", f"/v1/default/banks/{BANK}/mental-models/{m['id']}")
        chars = len(detail.get("content") or "")
        sizes.append({"id": m["id"], "chars": chars, "tags": m.get("tags") or []})
        if chars > SIZE_GUARD_CHARS:
            warnings.append({"id": m["id"], "chars": chars})
            log(f"MODEL_AUDIT_SIZE_WARN {m['id']} {chars}")
    return {"limit_chars": SIZE_GUARD_CHARS, "models": sizes, "warnings": warnings}


def supply_signal(token, covered):
    tags = items_of(api(token, "GET", f"/v1/default/banks/{BANK}/tags?limit=1000"))
    prospects = [t["tag"] for t in tags
                 if t.get("tag", "").startswith(PREFIXES)
                 and t.get("count", 0) >= THRESHOLD
                 and t["tag"] not in covered]
    log(f"prospect tags (total >= {THRESHOLD}, uncovered): {len(prospects)}")

    candidates = []
    for tag in prospects:
        seen = {m["id"]: m for m in tagged_memories(token, tag)
                if set(m.get("tags") or []) & set(KINDS)}
        uncovered = [m for m in seen.values()
                     if not (covered & set(m.get("tags") or []))]
        if len(uncovered) >= THRESHOLD:
            uncovered.sort(key=lambda m: m.get("mentioned_at") or "", reverse=True)
            candidates.append({
                "tag": tag,
                "uncovered_rule_decision_count": len(uncovered),
                "recent_titles": [
                    (m.get("text") or "")[:80] for m in uncovered[:5]
                ],
            })
        log(f"  {tag}: rule+decision={len(seen)}, uncovered={len(uncovered)}")

    candidates.sort(key=lambda c: -c["uncovered_rule_decision_count"])
    return {"threshold": THRESHOLD, "candidates": candidates}


def main():
    hc_host_env.require("HC_PERSONAL_BANK", "HC_OWNER")
    token = read_token()
    now = dt.datetime.now(dt.timezone.utc)

    models = items_of(api(token, "GET", f"/v1/default/banks/{BANK}/mental-models"))
    covered = {t for m in models for t in (m.get("tags") or [])
               if t.startswith(PREFIXES)}
    log(f"live models: {len(models)}, covered seed tags: {sorted(covered)}")

    demand = demand_signal(covered, now - dt.timedelta(days=WINDOW_DAYS), {m["id"] for m in models})
    if demand.get("log_read"):
        read_info = demand["log_read"]
        log(f"audit log generations read: {read_info['files']} nul_bytes={read_info['nul_bytes']}")
        for why in read_info["damaged"]:
            log(f"log-coverage: {why}")
        if read_info["coverage_short"]:
            log(f"log-coverage: earliest ts {read_info['earliest_ts']} is later than the window start "
                f"{demand['since']} - the {WINDOW_DAYS}d signals undercount")
    if demand.get("available"):
        log(f"demand ({WINDOW_DAYS}d): reflect calls={demand['reflect_calls']} "
            f"instrumented={demand['instrumented']} based_on={demand['based_on']} "
            f"hit_rate={demand['model_hit_rate']}")
        for c in demand["candidates"]:
            log(f"MODEL_AUDIT_DEMAND {c['tag']} {c['reflect_calls']}")
        for mm_line in mental_model_lines(demand):
            log(mm_line)
    sizes = size_guard(token, models)
    supply = supply_signal(token, covered)
    injection = injection_signal(now - dt.timedelta(days=WINDOW_DAYS))
    if injection.get("available"):
        log(f"MODEL_AUDIT_INJECTION calls={injection['context_trigger_calls']} "
            f"matched={injection['matched']} miss={injection['miss']} "
            f"empty={injection['empty']} "
            f"warns={injection['contradiction']['with_precedent']}/{injection['contradiction']['checked']} "
            f"drafts={sum(injection['recurrence_drafts'].values())} "
            f"correction_events={injection['injection_corrections']['events']} "
            f"corrected_memories={injection['injection_corrections']['unique_memories']}")

    report = {"generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "window_days": WINDOW_DAYS,
              "covered": sorted(covered), "demand": demand, "size_guard": sizes,
              "supply": supply, "injection": injection,
              # kept for readers of the #10-era report shape
              "threshold": THRESHOLD, "candidates": supply["candidates"]}
    print(json.dumps(report, ensure_ascii=False, indent=2))

    demand_candidates = demand.get("candidates") or []
    # 길목 주입 지표도 알림에 싣는다(#12 리뷰 P3, 2026-09-23): 로그에만 있으면 "임계값 2~4주
    # 관찰"을 아무도 보지 않는다. 주입 활동이 있던 주에는 멘탈 모델 신호가 없어도 보낸다.
    # issue #238: requests for a model id that does not exist are sent too (reflect has been 0 since
    # 2026-09-28, so nothing else would). Plain read counts are not a reason to send; when the report
    # goes out for another reason it carries the get_mental_model line as well.
    if (demand_candidates or sizes["warnings"] or supply["candidates"] or injection_active(injection)
            or mental_model_nonexistent(demand)):
        notify(demand, sizes, supply, injection)
    log(f"MODEL_AUDIT_OK candidates={len(supply['candidates'])} demand={len(demand_candidates)} "
        f"size_warn={len(sizes['warnings'])} reflect_calls={demand.get('reflect_calls', 'n/a')}")
    return 0


def injection_active(injection):
    if not injection.get("available"):
        return False
    return bool(injection["context_trigger_calls"] or injection["contradiction"]["checked"]
                or sum(injection["recurrence_drafts"].values())
                or injection["injection_corrections"]["events"])


def injection_lines(injection):
    """Weekly 길목 주입 summary for the notification; ⚠ lines flag what needs a human look."""
    if not injection_active(injection):
        return []
    w = injection["contradiction"]
    drafts = sum(injection["recurrence_drafts"].values())
    corr = injection["injection_corrections"]
    lines = [f"길목 주입 {WINDOW_DAYS}일: 호출 {injection['context_trigger_calls']} · "
             f"매칭 {injection['matched']} · 미스 {injection['miss']} · 빈 검색 {injection['empty']} · "
             f"recall 실패 {injection['recall_failed']} | 모순 경고 {w['with_precedent']}/{w['checked']} | "
             f"초안 생성 {drafts} | 주입 후 정정 {corr['events']}"]
    if corr["events"]:
        mems = ", ".join(f"`{m['id'][:8]}`" for m in corr["by_memory"][:3])
        trigs = ", ".join(f"`{t['id']}`" for t in corr["by_trigger"][:3])
        lines.append(f"- ⚠ 주입한 기억이 정정됨: 기억 {mems} / 트리거 {trigs} - 주입 내용이 낡았는지 확인")
    if injection["recall_failed"]:
        lines.append(f"- ⚠ 트리거 recall 실패 {injection['recall_failed']}건 - 게이트웨이 로그 확인")
    for t in injection["empty_by_trigger"][:3]:
        lines.append(f"- ⚠ 빈 검색 트리거 `{t['id']}` {t['fires']}회 - 쿼리가 기억과 어긋남")
    pending = _pending_drafts()
    if pending["total"]:
        split = f"ready-for-review {pending['ready-for-review']} / needs-evidence {pending['needs-evidence']}"
        if pending["other"]:
            split += f" / 기타 {pending['other']}"
        lines.append(f"- 트리거 초안 미처리 {pending['total']}건({split}) - scripts/trigger_drafts.py list")
    return lines


def _pending_drafts():
    """Unprocessed drafts, split by state. `other` = no draft_id/state, broken JSON (a line still being
    written included) or an unknown state; it counts in `total`. Only the rows without draft_id/state cannot be
    resolved; an unknown state still can (trigger_drafts.py resolve). The parsing rules
    are scripts/trigger_drafts.py's, copied rather than imported: test_host_env.py copies model_audit.py
    alone into scratch trees; tests/test_trigger_drafts.py pins the two readers together."""
    decided = _processed_draft_ids()
    counts = {"total": 0, "ready-for-review": 0, "needs-evidence": 0, "other": 0}
    for line in _nonblank_lines(PENDING_TRIGGERS):
        draft_id = state = None
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):      # RecursionError: 깊게 중첩된 손상 줄
            row = None
        if isinstance(row, dict) and _text(row.get("draft_id")) and _text(row.get("state")):
            draft_id, state = _bare_draft_id(row["draft_id"]), row["state"]
        if draft_id is not None and draft_id in decided:
            continue
        counts["total"] += 1
        counts[state if state in ("ready-for-review", "needs-evidence") else "other"] += 1
    return counts


def _processed_draft_ids():
    ids = {}
    for line in _nonblank_lines(TRIGGER_DECISIONS):
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(row, dict) and _text(row.get("draft_id")) and row.get("decision") in ("approved", "rejected"):
            ids[_bare_draft_id(row["draft_id"])] = row["decision"]      # same id again: the last line wins
    return ids


def _nonblank_lines(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return [line for line in fh if line.strip()]
    except OSError:
        return []


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _bare_draft_id(draft_id):
    draft_id = draft_id.strip()
    return draft_id[len("draft-"):] if draft_id.startswith("draft-") else draft_id


def notify_target():
    """(bot token, channel id): the HC_NOTIFY_DISCORD_* pair (environment or .env.host.local), else the
    legacy GGG_DISCORD_* pair of the environment (the wrapper reads ~/.env), else (None, None).
    A pair is used whole or not at all."""
    bot = hc_host_env.get("HC_NOTIFY_DISCORD_BOT_TOKEN")
    channel = hc_host_env.get("HC_NOTIFY_DISCORD_CHANNEL_ID")
    if bot and channel:
        return bot, channel
    bot = os.environ.get("GGG_DISCORD_BOT_TOKEN")
    channel = os.environ.get("GGG_DISCORD_CHANNEL_ID")
    return (bot, channel) if bot and channel else (None, None)


def _display_id(model_id):
    """A requested id as shown in the report and the log: the id is the caller's own argument, so
    anything outside [A-Za-z0-9_.:-] (backtick, newline, @) becomes "?" and cannot break the code
    span, forge a report line or mention anyone."""
    return re.sub(r"[^A-Za-z0-9_.:-]", "?", model_id)


def mental_model_nonexistent(demand):
    """Requests for a model id that does not exist (0 when unavailable or not computed)."""
    reads = demand.get("mental_model_reads") if demand.get("available") else None
    return (reads or {}).get("nonexistent") or 0


def mental_model_lines(demand):
    """The get_mental_model demand line of the weekly report (issue #238); [] when the log is
    unavailable or no model was read in the window. The nonexistent-id count appears only when
    demand_signal was given the live model ids."""
    reads = demand.get("mental_model_reads") if demand.get("available") else None
    if not reads or not reads["calls"]:
        return []
    top = ", ".join(f"`{_display_id(m['id'])}`{' (truncated)' if m.get('truncated') else ''} {m['calls']}"
                    for m in reads["by_model"][:5])
    parts = [f"수요: get_mental_model {WINDOW_DAYS}일 {reads['calls']}회"]
    if top:
        parts.append(f"모델별 {top}")
    parts.append(f"id 없음 {reads['unknown']}")
    if reads["nonexistent"] is not None:
        parts.append(f"없는 id {reads['nonexistent']}")
    return [" | ".join(parts)]


def report_message(demand, sizes, supply, injection=None):
    """The weekly report text (untrimmed); it sends nothing. Not free of I/O: when injection is active,
    injection_lines() reads the pending-trigger and decisions files (PENDING_TRIGGERS, TRIGGER_DECISIONS), so tests point those at a fixture."""
    lines = ["**[Hindsight] 주간 감사** (멘탈 모델·길목 주입, 생성·삭제·등록은 승인 후 수동)"]
    if demand.get("available"):
        b = demand["based_on"]
        lines.append(f"수요: reflect {WINDOW_DAYS}일 {demand['reflect_calls']}회 "
                     f"(계측 {demand['instrumented']}건: 히트 {b['hit']} / 미스 {b['miss']} / 미확인 {b['unknown']})")
        for c in demand["candidates"]:
            lines.append(f"- 승격 1순위 `{c['tag']}`: 모델 없이 reflect {c['reflect_calls']}회")
    for w in sizes["warnings"]:
        lines.append(f"- 크기 가드 `{w['id']}`: {w['chars']:,}자 > {sizes['limit_chars']:,}")
    for c in supply["candidates"]:
        lines.append(f"- 공급(보조) `{c['tag']}`: 미커버 rule·decision {c['uncovered_rule_decision_count']}건")
    lines.extend(injection_lines(injection or {}))
    # Last on purpose: notify() cuts at 1900 chars, and the older lines must not be the ones cut first.
    lines.extend(mental_model_lines(demand))
    return "\n".join(lines)


def notify(demand, sizes, supply, injection=None):
    message = report_message(demand, sizes, supply, injection)[:1900]

    bot, channel = notify_target()
    if bot and channel:
        try:
            req = urllib.request.Request(
                f"https://discord.com/api/v10/channels/{channel}/messages",
                data=json.dumps({"content": message}).encode(),
                headers={"Authorization": "Bot " + bot,
                         "Content-Type": "application/json",
                         # Discord 403s the default Python-urllib user agent.
                         "User-Agent": "DiscordBot (hindsight-crew model-audit, 1.0)"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15):
                pass
            log("notified via Discord")
            return
        except (urllib.error.URLError, OSError) as e:
            log(f"MODEL_AUDIT_NOTIFY_FAILED discord {e}")
    else:
        log("MODEL_AUDIT_NOTIFY_FAILED discord missing_env")

    try:
        subprocess.run(
            ["/usr/bin/osascript", "-e",
             'on run argv\ndisplay notification (item 1 of argv) '
             'with title "Hindsight Model Audit"\nend run', "--",
             f"수요 {len(demand.get('candidates') or [])} / 크기 {len(sizes['warnings'])} / "
             f"공급 {len(supply['candidates'])} / 주입 {len(injection_lines(injection or {}))}줄 - audit.log 확인"],
            check=True, capture_output=True, timeout=10,
        )
        log("notified via osascript fallback")
    except (subprocess.SubprocessError, OSError) as e:
        log(f"MODEL_AUDIT_NOTIFY_FAILED osascript {e}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except hc_host_env.ConfigMissing as e:
        log(f"MODEL_AUDIT_FAIL - {e}")
        sys.exit(1)
    except Exception as e:  # noqa: BLE001 - cron job must log, not stacktrace-silently
        log(f"MODEL_AUDIT_FAIL {type(e).__name__}: {e}")
        sys.exit(1)
