#!/usr/bin/env python3
"""model_watch.py - daily mental-model refresh failure watch (issue #428, for #251).

Reads only (list mental models, list in-flight refresh operations, count memories). It never
creates, refreshes, patches or deletes a model. The upstream facts the rules rest on (v1.1.0) are in
docs/model-refresh-cron.md; the line numbers there are the reason for each rule below.

Alert conditions, per model:
1. refresh_failed - `last_refresh_failed_at` is set. A successful refresh writes it back to NULL, so a
   set value is an unresolved failure (it also pauses the server's automatic triggers until an explicit
   refresh succeeds). A transient provider failure or a reflect wall-clock timeout does NOT set it.
2. cron_missed - only for a model with `trigger.refresh_cron`: the deadline fire is the latest cron fire
   (UTC, like the server) at or before now - grace. When no successful refresh finished at or after that
   fire (`last_refreshed_at` < fire) and an in-scope memory written after the model's watermark was
   written before that fire, the scheduled refresh should have consumed it and did not. The watermark is
   `last_memory_seen_at` (the newest memory the last successful refresh read); when it is null it falls
   back to `last_refreshed_at`, as upstream's stale check does. Counting memories BEFORE the fire is the
   point: a model that also has newer memories (after the fire) is not "fine" because of them. A model
   with a pending/processing refresh operation is running, not failed.
3. observation_failed - the model list, a page of it, a field, the operation list or a memory count
   could not be read, or a cron/scope cannot be evaluated here. Not seeing is not "fine".
A model without refresh_cron gets rules 1 and 3 only (the launchd driver refreshes it; its own log
carries the transient failures this watch cannot see).

Usage: model_watch.py [--grace-hours H] [--no-notify]
Bank and owner come from .env.host.local (HC_PERSONAL_BANK, HC_OWNER), never defaulted. Notification:
Discord bot REST (HC_NOTIFY_DISCORD_*, else legacy GGG_DISCORD_*), osascript fallback. Log tokens:
MODEL_WATCH_OK models=<n> cron=<n> / MODEL_WATCH_ALERT <id> <kind> <detail> / MODEL_WATCH_FAIL <reason> /
MODEL_WATCH_NOTIFY_FAILED. JSON report to stdout, narration to stderr. Exit 0 = no alert, 1 = alert or
failure. Stdlib only (runs on the system python3).
"""
from __future__ import annotations

import argparse
import datetime as dt
import http.client
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import hc_host_env  # noqa: E402  (같은 디렉터리, 표준 라이브러리만)

ROOT = os.path.dirname(_HERE)
BASE_URL = os.environ.get("HC_GATEWAY_URL", "http://127.0.0.1:8888")
# upstream list 상한(api/http.py limit le=1000). 페이지 처리 검증은 이 값과 무관하게 total 대조로 한다.
MODEL_PAGE_LIMIT = 1000
# upstream operations 목록 상한(api/http.py limit le=100)
OPERATION_PAGE_LIMIT = 100
IN_FLIGHT_STATUSES = ("pending", "processing")
DEFAULT_GRACE_HOURS = 2.0
# 응답 항목마다 있어야 하는 키(값은 None 가능). 없으면 관측 실패다.
REQUIRED_FIELDS = ("id", "trigger", "last_refreshed_at", "last_memory_seen_at", "last_refresh_failed_at", "is_stale")
CRON_LOOKBACK_DAYS = 400


class ObservationError(Exception):
    """A read that could not be completed: the result is unknown, never "nothing to report"."""


class TransportError(Exception):
    """No HTTP answer (connection refused, reset, timeout). For a write this means: outcome unknown."""


def log(msg):
    print(msg, file=sys.stderr)


# --------------------------------------------------------------------------- cron (UTC, 5 fields)

_FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("dom", 1, 31), ("month", 1, 12), ("dow", 0, 7))
# Upper end of `*` and `N/step` per field. croniter (the server, maintenance.py:580) runs day-of-week 0-6 there, so
# "1/2" is Mon/Wed/Fri, not Mon/Wed/Fri/Sun (7 would fold into Sunday below). An explicit 7 stays valid.
_STEP_HI = {"dow": 6}


class CronError(ValueError):
    pass


def _expand(part, lo, hi, step_hi=None):
    step_hi = hi if step_hi is None else step_hi
    out = set()
    for item in part.split(","):
        if not item:
            raise CronError("empty list item")
        step = 1
        if "/" in item:
            item, step_s = item.split("/", 1)
            if not step_s.isdigit() or int(step_s) < 1:
                raise CronError("bad step %r" % step_s)
            step = int(step_s)
        if item == "*":
            a, b = lo, step_hi
        elif "-" in item:
            a_s, b_s = item.split("-", 1)
            if not (a_s.isdigit() and b_s.isdigit()):
                raise CronError("bad range %r" % item)
            a, b = int(a_s), int(b_s)
        elif item.isdigit():
            a = int(item)
            b = step_hi if step > 1 else a   # "5/15" = 5-max/15, as croniter reads it
        else:
            raise CronError("unsupported token %r (names and specials are not supported)" % item)
        if not (lo <= a <= hi and lo <= b <= hi and a <= b):
            raise CronError("value out of range in %r" % item)
        out.update(range(a, b + 1, step))
    return out


def parse_cron(expr):
    """Standard 5-field cron as sets. Only digits, `*`, `,`, `-`, `/`; DOM and DOW may not both be
    restricted (their OR rule differs between implementations, so such an expression is refused)."""
    if not isinstance(expr, str):
        raise CronError("cron is not a string")
    parts = expr.split()
    if len(parts) != 5:
        raise CronError("need 5 fields, got %d" % len(parts))
    sets = {}
    for (name, lo, hi), part in zip(_FIELDS, parts):
        sets[name] = _expand(part, lo, hi, _STEP_HI.get(name))
    if parts[2] != "*" and parts[4] != "*":
        raise CronError("day-of-month and day-of-week both restricted")
    if 7 in sets["dow"]:
        sets["dow"] = (sets["dow"] - {7}) | {0}
    return sets


def latest_fire_at_or_before(expr, t):
    """The latest fire of `expr` (UTC) at or before `t` (aware), or None within CRON_LOOKBACK_DAYS."""
    c = parse_cron(expr)
    t = t.astimezone(dt.timezone.utc).replace(second=0, microsecond=0)
    day = t.date()
    for _ in range(CRON_LOOKBACK_DAYS + 1):
        cron_dow = (day.weekday() + 1) % 7          # Monday=0 -> cron 1, Sunday -> 0
        if day.month in c["month"] and day.day in c["dom"] and cron_dow in c["dow"]:
            for hour in sorted(c["hour"], reverse=True):
                for minute in sorted(c["minute"], reverse=True):
                    cand = dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=dt.timezone.utc)
                    if cand <= t:
                        return cand
        day -= dt.timedelta(days=1)
    return None


# --------------------------------------------------------------------------- HTTP

def read_token():
    owner = hc_host_env.require("HC_OWNER")["HC_OWNER"]
    with open(os.path.join(ROOT, "secrets", "acl.json")) as fh:
        acl = json.load(fh)
    for tok, p in acl["tokens"].items():
        if p.get("identity") == owner:
            return tok
    raise RuntimeError("no token for HC_OWNER in secrets/acl.json")


class HttpClient:
    """request(method, path, body) -> (status, payload). Raises TransportError when no answer came."""

    def __init__(self, token, base_url=BASE_URL, timeout=60):
        self._token = token
        self._base = base_url
        self._timeout = timeout

    def request(self, method, path, body=None):
        req = urllib.request.Request(
            self._base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": "Bearer " + self._token, "Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                status = resp.status
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                raw = e.read().decode("utf-8", "replace") if e.fp else ""
            except (OSError, ValueError, http.client.HTTPException) as body_err:   # body lost: status only
                raise TransportError("http_%d body unreadable: %s" % (status, type(body_err).__name__))
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:   # timeout is an OSError; IncompleteRead is not
            raise TransportError("%s: %s" % (type(e).__name__, e))
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = None
        return status, payload


def _get(client, path):
    try:
        status, payload = client.request("GET", path)
    except TransportError as e:
        raise ObservationError("transport %s" % e)
    if not 200 <= status < 300:
        raise ObservationError("http_%d %s" % (status, path.split("?")[0]))
    return payload


def list_all_models(client, bank, page_limit=MODEL_PAGE_LIMIT):
    """Every mental model with detail=content (the only level carrying `trigger`), page by page until
    `total`. A short page, a changed total, a duplicate id or an id count other than total fails: the
    list is ordered by last_refreshed_at, so a refresh finishing between pages can shift rows."""
    items, offset, total = [], 0, None
    while True:
        query = urllib.parse.urlencode([("detail", "content"), ("limit", page_limit), ("offset", offset)])
        page = _get(client, "/v1/default/banks/%s/mental-models?%s" % (bank, query))
        if not isinstance(page, dict) or not isinstance(page.get("items"), list) \
                or not isinstance(page.get("total"), int) or isinstance(page.get("total"), bool):
            raise ObservationError("model list: no items/total")
        if total is None:
            total = page["total"]
        elif page["total"] != total:
            raise ObservationError("model list: total changed %d -> %d between pages" % (total, page["total"]))
        items.extend(page["items"])
        if len(items) >= total:
            break
        if not page["items"]:
            raise ObservationError("model list: page at offset %d empty, %d/%d read" % (offset, len(items), total))
        offset += len(page["items"])
    ids = [m.get("id") if isinstance(m, dict) else None for m in items]
    if None in ids or len(set(ids)) != len(ids) or len(ids) != total:
        raise ObservationError("model list: page missing (%d unique ids of total %d)" % (len(set(ids) - {None}), total))
    return items


def in_flight_model_ids(client, bank, page_limit=OPERATION_PAGE_LIMIT):
    """ids of models with a pending/processing refresh_mental_model operation."""
    out = set()
    for status in IN_FLIGHT_STATUSES:
        seen, offset, total = 0, 0, None
        while True:
            query = urllib.parse.urlencode([("type", "refresh_mental_model"), ("status", status),
                                            ("limit", page_limit), ("offset", offset)])
            page = _get(client, "/v1/default/banks/%s/operations?%s" % (bank, query))
            ops = page.get("operations") if isinstance(page, dict) else None
            if not isinstance(ops, list) or not isinstance(page.get("total"), int):
                raise ObservationError("operation list: no operations/total")
            total = page["total"] if total is None else total
            for op in ops:
                mid = op.get("mental_model_id") if isinstance(op, dict) else None
                if not mid:
                    raise ObservationError("operation list: %s operation without mental_model_id" % status)
                out.add(mid)
            seen += len(ops)
            if seen >= total or not ops:
                break
            offset += len(ops)
        if seen < total:
            raise ObservationError("operation list: %s page missing (%d/%d)" % (status, seen, total))
    return out


# --------------------------------------------------------------------------- scope + memory count

def stale_scope(model):
    """(tags, tags_match, fact_type) for memories/list, mirroring upstream's staleness scope
    (memory_engine.py _resolve_refresh_tag_filtering + _mental_model_stale_scope). Raises
    ObservationError for a scope the list endpoint cannot express (tag_groups, several fact types)."""
    trig = model.get("trigger") or {}
    if trig.get("tag_groups") or trig.get("scope_tag_groups"):
        raise ObservationError("scope uses tag_groups (not expressible in memories/list)")
    fact_types = list(trig.get("fact_types") or [])
    if len(fact_types) > 1:
        raise ObservationError("scope has %d fact_types (memories/list takes one)" % len(fact_types))
    tags = [t for t in (model.get("tags") or []) if isinstance(t, str)]
    if tags:
        match = trig.get("tags_match") or "all_strict"
        match = {"any": "any_strict", "all": "all_strict"}.get(match, match)
    else:
        match = trig.get("tags_match") or "any"
    return tags, match, (fact_types[0] if fact_types else None)


def parse_ts(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ObservationError("timestamp is not a string: %r" % (value,))
    try:
        t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ObservationError("bad timestamp %r" % value)
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _iso(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def make_memory_counter(client, bank):
    def count(model, after, before):
        """In-scope memories with after < updated_at < before (after None = no lower bound)."""
        tags, match, fact_type = stale_scope(model)
        params = [("tags", t) for t in tags] + [("tags_match", match), ("time_field", "updated_at"),
                                                ("end_date", _iso(before)), ("limit", 1)]
        if after is not None:   # start_date is inclusive; the watermark memory itself is consumed
            params.append(("start_date", _iso(after + dt.timedelta(microseconds=1))))
        if fact_type:
            params.append(("type", fact_type))
        page = _get(client, "/v1/default/banks/%s/memories/list?%s" % (bank, urllib.parse.urlencode(params)))
        total = page.get("total") if isinstance(page, dict) else None
        if not isinstance(total, int) or isinstance(total, bool):
            raise ObservationError("memory count: no total")
        return total
    return count


# --------------------------------------------------------------------------- decision

def evaluate(models, in_flight, now, grace, count_unconsumed):
    """Pure decision over already-read data. Returns (alerts, statuses).

    alerts: [{"id", "kind", "detail"}], kind in refresh_failed / cron_missed / observation_failed.
    statuses: {id: ok | running | no_cron | alert}. count_unconsumed(model, after, before) -> int.
    """
    alerts, statuses = [], {}

    def alert(mid, kind, detail):
        alerts.append({"id": mid, "kind": kind, "detail": detail})
        statuses[mid] = "alert"

    for m in models:
        mid = m.get("id") if isinstance(m, dict) else None
        if not isinstance(m, dict) or not mid:
            alert(str(mid), "observation_failed", "model entry without id")
            continue
        missing = [f for f in REQUIRED_FIELDS if f not in m]
        if missing or not isinstance(m.get("trigger"), dict):
            alert(mid, "observation_failed", "field missing: %s" % ",".join(missing or ["trigger(object)"]))
            continue
        if m["last_refresh_failed_at"] is not None:
            alert(mid, "refresh_failed", "last_refresh_failed_at=%s" % m["last_refresh_failed_at"])
        cron = m["trigger"].get("refresh_cron")
        if not cron:
            statuses.setdefault(mid, "no_cron")
            continue
        if mid in in_flight:
            statuses.setdefault(mid, "running")
            continue
        try:
            deadline = latest_fire_at_or_before(cron, now - grace)
        except CronError as e:
            alert(mid, "observation_failed", "refresh_cron %r not evaluable: %s" % (cron, e))
            continue
        if deadline is None:   # a cron rarer than the lookback (or one that never fires) cannot be judged here
            alert(mid, "observation_failed", "refresh_cron %r has no fire in the %d days before now - grace"
                  % (cron, CRON_LOOKBACK_DAYS))
            continue
        if m["is_stale"] is False:
            statuses.setdefault(mid, "ok")
            continue
        if not isinstance(m["is_stale"], bool):
            alert(mid, "observation_failed", "is_stale is %r" % (m["is_stale"],))
            continue
        try:
            refreshed = parse_ts(m["last_refreshed_at"])
            if refreshed is not None and refreshed >= deadline:
                # the spec's rule: alert only when no successful refresh came after the fire (+ grace)
                statuses.setdefault(mid, "ok")
                continue
            # upstream stale 기준과 같다: last_memory_seen_at, 없으면 last_refreshed_at (memory_engine.py:2109)
            watermark_raw = m["last_memory_seen_at"] or m["last_refreshed_at"]
            watermark = parse_ts(watermark_raw)
            pending = count_unconsumed(m, watermark, deadline)
        except ObservationError as e:
            alert(mid, "observation_failed", "memory count: %s" % e)
            continue
        if pending > 0:
            alert(mid, "cron_missed", "%d in-scope memories written before the %s fire (UTC), after watermark %s, "
                                      "no successful refresh since, still unconsumed %.1fh later"
                  % (pending, deadline.isoformat(), watermark_raw, grace.total_seconds() / 3600))
        else:
            statuses.setdefault(mid, "ok")
    return alerts, statuses


def watch(client, bank, now, grace):
    """Read everything, decide, return the report. An unreadable list or operation list is one alert."""
    report = {"generated_at": _iso(now), "grace_seconds": int(grace.total_seconds()), "alerts": [], "models": {}}
    try:
        models = list_all_models(client, bank)
        in_flight = in_flight_model_ids(client, bank) if any(
            isinstance(m, dict) and isinstance(m.get("trigger"), dict) and m["trigger"].get("refresh_cron")
            for m in models) else set()
    except ObservationError as e:
        report["alerts"].append({"id": "*", "kind": "observation_failed", "detail": str(e)})
        return report
    alerts, statuses = evaluate(models, in_flight, now, grace, make_memory_counter(client, bank))
    report["alerts"] = alerts
    report["models"] = statuses
    report["cron_models"] = sorted(m["id"] for m in models if isinstance(m, dict) and isinstance(m.get("trigger"), dict)
                                   and m["trigger"].get("refresh_cron"))
    return report


# --------------------------------------------------------------------------- notification

def notify_target():
    """Same pair order as model_audit.notify_target (copied, not imported: importing model_audit runs its
    module-level config reads)."""
    bot = hc_host_env.get("HC_NOTIFY_DISCORD_BOT_TOKEN")
    channel = hc_host_env.get("HC_NOTIFY_DISCORD_CHANNEL_ID")
    if bot and channel:
        return bot, channel
    bot = os.environ.get("GGG_DISCORD_BOT_TOKEN")
    channel = os.environ.get("GGG_DISCORD_CHANNEL_ID")
    return (bot, channel) if bot and channel else (None, None)


def report_message(report):
    lines = ["**[Hindsight] 멘탈 모델 갱신 감시** (매일, 읽기 전용 - 조치는 수동)"]
    labels = {"refresh_failed": "갱신 실패", "cron_missed": "예정 회차 미갱신", "observation_failed": "관측 실패"}
    for a in report["alerts"]:
        lines.append("- %s `%s`: %s" % (labels.get(a["kind"], a["kind"]), a["id"], a["detail"]))
    return "\n".join(lines)


def notify(report):
    message = report_message(report)[:1900]
    bot, channel = notify_target()
    if bot and channel:
        try:
            req = urllib.request.Request(
                "https://discord.com/api/v10/channels/%s/messages" % channel,
                data=json.dumps({"content": message}).encode(),
                headers={"Authorization": "Bot " + bot, "Content-Type": "application/json",
                         "User-Agent": "DiscordBot (hindsight-crew model-watch, 1.0)"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15):
                pass
            log("notified via Discord")
            return
        except (urllib.error.URLError, OSError) as e:
            log("MODEL_WATCH_NOTIFY_FAILED discord %s" % type(e).__name__)
    else:
        log("MODEL_WATCH_NOTIFY_FAILED discord missing_env")
    try:
        subprocess.run(["/usr/bin/osascript", "-e",
                        'on run argv\ndisplay notification (item 1 of argv) with title "Hindsight Model Watch"\nend run',
                        "--", "알림 %d건 - watch.log 확인" % len(report["alerts"])],
                       check=True, capture_output=True, timeout=10)
        log("notified via osascript fallback")
    except (subprocess.SubprocessError, OSError) as e:
        log("MODEL_WATCH_NOTIFY_FAILED osascript %s" % type(e).__name__)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--grace-hours", type=float, default=DEFAULT_GRACE_HOURS,
                    help="hours after a cron fire before a missing refresh is an alert (default 2)")
    ap.add_argument("--no-notify", action="store_true", help="print the report only")
    args = ap.parse_args(argv)
    if args.grace_hours < 0:
        ap.error("--grace-hours must be >= 0")
    bank = hc_host_env.require("HC_PERSONAL_BANK", "HC_OWNER")["HC_PERSONAL_BANK"]
    client = HttpClient(read_token())
    report = watch(client, bank, dt.datetime.now(dt.timezone.utc), dt.timedelta(hours=args.grace_hours))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    for a in report["alerts"]:
        log("MODEL_WATCH_ALERT %s %s %s" % (a["id"], a["kind"], a["detail"]))
    if report["alerts"]:
        if not args.no_notify:
            notify(report)
        return 1
    log("MODEL_WATCH_OK models=%d cron=%d" % (len(report["models"]), len(report.get("cron_models", []))))
    return 0


def _notify_failure(reason):
    """A run that could not even start watching is an observation failure too: say so, best effort."""
    try:
        notify({"alerts": [{"id": "*", "kind": "observation_failed", "detail": reason}]})
    except Exception as e:  # noqa: BLE001 - the failure line is already logged
        log("MODEL_WATCH_NOTIFY_FAILED %s" % type(e).__name__)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except hc_host_env.ConfigMissing as e:
        log("MODEL_WATCH_FAIL - %s" % e)
        sys.exit(1)
    except Exception as e:  # noqa: BLE001 - a launchd job must log, not die silently
        log("MODEL_WATCH_FAIL %s: %s" % (type(e).__name__, e))
        _notify_failure("watch run failed: %s" % type(e).__name__)
        sys.exit(1)
