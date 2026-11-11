#!/usr/bin/env python3
"""S3 역할 저장소·사이드카 매시간 관찰 점검 (issue #415, 상위 #393 R7-8). 읽기 전용, 설치하지 않는 것이 기본이다.

  item_roles_observe.py [--db <저장소>] [--max-age-hours 2] [--label <사이드카 launchd 라벨>] [--log <사이드카 로그>]
                        [--state <상태 파일>] [--repeat-hours 6] [--expect-github] [--expect-key-rules <규칙>]
                        [--sidecar-interval <초>] [--busy-timeout <초>] [--dry-run] [--json] [--now <ISO>]

S3 3상태 표를 다시 켠 뒤 첫 1주 동안 GitHub 근거 신선도를 매시간 본다(사이드카가 멈추면 2시간 뒤 모든 줄이 stale).
본다:
  * 저장소(`gateway/item_roles.py` 계약 v1, `?mode=ro` URI 로만 연다 - 사이드카와 동시 쓰기 충돌·운영 데이터 변경 방지):
    meta `key_rules`, 저장소 키(mention_roles)의 리포마다 GitHub 근거 테이블 `github_repos` 의 마지막 수집 성공 시각
    (status ok·missing 의 fetched_at)과 근거 나이. 상한(--max-age-hours)을 넘은 리포와 근거 행이 없는 리포 = stale,
    status error 리포 = error(마지막 성공 시각이 덮여 나이를 모른다, stale 에는 세지 않는다).
  * 근거 테이블이 없으면(사이드카가 `--github` 수집을 켜지 않음) `github=off` 로 따로 보고한다. 재활성화 전에는 정상이고,
    `--expect-github`(재활성화 뒤 plist 가 준다)면 이상 github-off 다.
  * 사이드카 launchd 마지막 종료 코드: `launchctl print gui/<uid>/<label>` 출력에서 `last exit code =` 줄(설명 접미
    `78: EX_CONFIG` 가 붙어도 앞 정수)과 `last terminating signal =` 줄(있으면 시그널 종료 = 이상 launchd-signal)만 읽고
    나머지(launchd 세션 env 포함)는 버린다. 출력 원문은 어디에도 남기지 않는다. launchctl 시간 초과·실행 불가 =
    launchd-probe-failed(미등록 launchd-missing 과 따로). 라벨을 못 정하면 생략하되 --expect-github 면 launchd-label-missing.
  * 사이드카 로그(JSON 줄)의 마지막 실행(마지막 `"phase": "plan"` 줄부터 끝까지)에서 GitHub 단계 줄 중 error > 0 인 줄 수.
  * 사이드카 정지: 로그 마지막 쓰기 시각(mtime, 사이드카 줄에는 시각 필드가 없고 매 실행 plan 줄이 파일을 갱신한다)이
    --sidecar-interval(기본 900초)의 3배보다 오래면 이상 sidecar-stalled. 시각을 못 읽으면 stall=unknown(알림 없음).
이상(stale > 0, error > 0, 종료 코드 != 0, 로그 GitHub 오류 > 0, 저장소 읽기 실패, launchd 미등록, 로그 없음,
  --expect-* 불일치)이면 `scripts/scheduled_checks.py` 의 notify(같은 Discord 경로, GGG_* env, 자격증명 출력 없음)로 한 줄
  알림을 보내고 exit 1. 정상이면 stdout 한 줄(`ITEM_ROLES_OBSERVE ok ...`)만 내고 exit 0.
반복 억제: 상태 파일(0600)에 이상 코드별 마지막 알림 시각과 그때 크기(stale·error = 리포 수, log-github = 줄 수)를 둔다.
  같은 코드는 --repeat-hours(기본 6시간)에 한 번만 알리고, 새 코드가 생기거나 크기가 직전 알림 때보다 커지면 바로 알린다.
  지금 없는 코드는 상태에서 지운다(회복 뒤 재발은 바로 알린다). 시각·크기는 알림이 실제로 전달됐을 때만(notify 가 True)
  적고, 전달 실패여도 정리는 쓴다(다음 실행이 다시 알린다). --dry-run 은 실제 전송도 상태 기록도 하지 않는다.
출력에는 수치·코드만 낸다(리포 이름은 --json 의 repos 에만, 키·기억 본문·토큰은 어디에도 없다).
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(_HERE)
for _p in (REPO, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from gateway import item_roles as IR  # noqa: E402  (표준 라이브러리만)
from scheduled_checks import notify as default_notifier  # noqa: E402  (같은 Discord 경로, 자격증명 출력 없음)

DEFAULT_DB = os.path.join(REPO, "policies", "item-roles.sqlite")      # 사이드카 plist 의 --db 와 같은 경로
LOG_DIR = os.path.expanduser("~/Library/Logs/hindsight-item-roles")
DEFAULT_LOG = os.path.join(LOG_DIR, "sidecar.log")
DEFAULT_STATE = os.path.join(LOG_DIR, "observe-state.json")
LABEL_SUFFIX = ".hindsight-item-roles-sidecar"
DEFAULT_MAX_AGE_HOURS = 2.0
DEFAULT_REPEAT_HOURS = 6.0
DEFAULT_BUSY_TIMEOUT = 5.0
LOG_TAIL_BYTES = 1 << 20          # 로그 꼬리만 읽는다(마지막 실행이 들어갈 만큼)
LAUNCHCTL_TIMEOUT = 15
DEFAULT_SIDECAR_INTERVAL = 900    # 사이드카 plist StartInterval(초). 로그가 이 3배보다 오래 안 쓰이면 sidecar-stalled
STALL_FACTOR = 3
TAG = "[R7-8 역할 관찰]"

Runner = Callable[[Sequence[str]], Tuple[int, str, str]]

_EXIT_RE = re.compile(r"^\s*last exit code = (.+?)\s*$", re.M)
_SIGNAL_RE = re.compile(r"^\s*last terminating signal = (.+?)\s*$", re.M)
_LEAD_INT_RE = re.compile(r"^(-?\d+)(?:\s*:.*)?$")       # `78` 또는 `78: EX_CONFIG`
_SIG_NUM_RE = re.compile(r"(\d+)\s*$")                    # `Killed: 9` -> 9


# --- 저장소(읽기 전용) --------------------------------------------------------------------------------------

def connect_ro(path: str, timeout: float) -> sqlite3.Connection:
    """읽기 전용 URI 로만 연다. 경로의 %·?·# 가 URI 로 해석되지 않게 퍼센트 인코딩한다(`item_roles.load_store` 와 같다)."""
    return sqlite3.connect(Path(os.path.abspath(path)).as_uri() + "?mode=ro", uri=True, timeout=timeout)


def read_store(path: str, timeout: float) -> Dict[str, Any]:
    """-> {"ok": True, "key_rules", "repos": [저장소 키의 리포], "github": load_github 모양 또는 None}
       또는 {"ok": False, "code": missing|locked|schema|unreadable}."""
    if not os.path.exists(path):
        return {"ok": False, "code": "missing"}
    try:
        conn = connect_ro(path, timeout)
    except sqlite3.Error:
        return {"ok": False, "code": "unreadable"}
    try:
        # meta 를 직접 읽는다(`IR.read_meta` 는 잠금도 StoreError 로 감싸 잠김과 계약 불일치를 가를 수 없다)
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"meta", "mention_roles"} <= names:
            return {"ok": False, "code": "schema"}
        meta = {k: v for k, v in conn.execute("SELECT k, v FROM meta")}
        if meta.get("schema_version") != IR.SCHEMA_VERSION:
            return {"ok": False, "code": "schema"}
        keys = [r[0] for r in conn.execute("SELECT DISTINCT key FROM mention_roles")]
        github = IR._read_github(conn)
    except sqlite3.OperationalError as e:
        return {"ok": False, "code": "locked" if "locked" in str(e) or "busy" in str(e) else "unreadable"}
    except sqlite3.Error:
        return {"ok": False, "code": "unreadable"}
    finally:
        conn.close()
    repos = sorted({sk[0] for sk in (IR.split_key(k) for k in keys) if sk})
    return {"ok": True, "key_rules": IR.key_rules_of(meta), "repos": repos, "github": github}


def parse_time(text: Any) -> Optional[dt.datetime]:
    try:
        t = dt.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def evidence_ages(store: Dict[str, Any], now: dt.datetime, max_age_h: float) -> Dict[str, Any]:
    """저장소 키 리포마다 근거 나이(시간). stale = 상한 초과·근거 행 없음·시각 불명, error = status error."""
    repos_tbl = (store.get("github") or {}).get("repos") or {}
    per: Dict[str, Dict[str, Any]] = {}
    for repo in store["repos"]:
        row = repos_tbl.get(repo)
        if not row:
            per[repo] = {"status": "unfetched", "age_h": None, "stale": True}
            continue
        status = row.get("status")
        if status == "error":
            per[repo] = {"status": "error", "age_h": None, "stale": False}
            continue
        t = parse_time(row.get("fetched_at"))
        age = None if t is None else round((now - t).total_seconds() / 3600.0, 2)
        per[repo] = {"status": status, "age_h": age, "stale": age is None or age > max_age_h}
    ages = [v["age_h"] for v in per.values() if v["age_h"] is not None]
    return {"per_repo": per, "stale": sum(1 for v in per.values() if v["stale"]),
            "error": sum(1 for v in per.values() if v["status"] == "error"),
            "max_age_h": max(ages) if ages else None}


# --- launchd ----------------------------------------------------------------------------------------------

def launchctl_runner(timeout: int = LAUNCHCTL_TIMEOUT) -> Runner:
    def run(args: Sequence[str]) -> Tuple[int, str, str]:
        try:
            p = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, "", ""
        except OSError:
            return 127, "", ""
        return p.returncode, p.stdout, ""          # stderr 는 쓰지 않는다
    return run


def launchd_exit(label: Optional[str], runner: Runner) -> Dict[str, Any]:
    """-> {"state": skip|missing|probe-failed|signal|never|ok|unknown, "code": int 또는 None}. `last exit code` 줄 말고는 아무것도 남기지 않는다."""
    if not label:
        return {"state": "skip", "code": None}
    rc, out, _ = runner(["launchctl", "print", "gui/%d/%s" % (os.getuid(), label)])
    if rc in (124, 127):                 # 실행기 시간 초과·launchctl 실행 불가 = 조회 실패(미등록이 아니다)
        return {"state": "probe-failed", "code": None}
    if rc != 0:
        return {"state": "missing", "code": None}
    text = out or ""
    m = _EXIT_RE.search(text)
    sig = _SIGNAL_RE.search(text)
    del out, text
    if sig:                              # 시그널 종료(OOM SIGKILL 등)는 종료 코드와 따로 적힌다 - 이상으로 본다
        num = _SIG_NUM_RE.search(sig.group(1))
        return {"state": "signal", "code": None, "signal": int(num.group(1)) if num else None}
    if not m:
        return {"state": "unknown", "code": None}
    val = m.group(1)
    lead = _LEAD_INT_RE.match(val)       # 설명 접미(`78: EX_CONFIG`)가 붙어도 앞 정수를 읽는다
    if lead:
        return {"state": "ok", "code": int(lead.group(1))}
    if "never" in val:
        return {"state": "never", "code": None}
    return {"state": "unknown", "code": None}


# --- 사이드카 로그 ----------------------------------------------------------------------------------------

def log_age(path: str, now: dt.datetime) -> Optional[int]:
    """사이드카 로그 마지막 쓰기 시각(mtime)의 나이(초). 사이드카 JSON 줄에는 시각 필드가 없어 매 실행 첫 줄(plan)이
    파일을 갱신하는 것을 쓴다. 못 읽으면 None(unknown)."""
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return None
    return int(round(now.timestamp() - mtime))


def scan_log(path: str) -> Dict[str, Any]:
    """마지막 실행(마지막 plan 줄부터 끝)의 GitHub 단계 줄 수와 error > 0 인 줄 수. -> {"state": missing|no-run|ok, ...}."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - LOG_TAIL_BYTES))
            data = fh.read()
    except OSError:
        return {"state": "missing", "github_lines": 0, "github_error_lines": 0}
    rows: List[Optional[Dict[str, Any]]] = []
    for ln in data.decode("utf-8", "replace").splitlines():
        ln = ln.strip()
        obj = None
        if ln.startswith("{"):
            try:
                obj = json.loads(ln)
            except ValueError:
                obj = None
        rows.append(obj if isinstance(obj, dict) else None)
    starts = [i for i, r in enumerate(rows) if r and r.get("phase") == "plan"]
    if not starts:
        return {"state": "no-run", "github_lines": 0, "github_error_lines": 0}
    last = [r for r in rows[starts[-1]:] if r and r.get("phase") == "github"]
    errs = sum(1 for r in last if not isinstance(r.get("error"), int) or r.get("error") > 0)
    return {"state": "ok", "github_lines": len(last), "github_error_lines": errs}


# --- 판정·반복 억제 ---------------------------------------------------------------------------------------

def evaluate(store: Dict[str, Any], launchd: Dict[str, Any], logscan: Dict[str, Any], now: dt.datetime,
             max_age_h: float, expect_github: bool, expect_key_rules: Optional[str],
             log_age_s: Optional[int] = None, stall_after_s: Optional[float] = None) -> Tuple[List[str], Dict[str, Any]]:
    """-> (이상 코드 목록, 요약). 요약은 출력 한 줄·--json 에 쓴다."""
    codes: List[str] = []
    summary: Dict[str, Any] = {"launchd": launchd["state"], "exit": launchd["code"], "log": logscan["state"],
                               "log_github_err": logscan["github_error_lines"]}
    if not store["ok"]:
        codes.append("store-" + store["code"])
        summary.update(store="fail:" + store["code"], github=None, key_rules=None)
    else:
        summary.update(store="ok", key_rules=store["key_rules"], github="on" if store["github"] is not None else "off",
                       repos=len(store["repos"]))
        if store["github"] is None:
            if expect_github:
                codes.append("github-off")
        else:
            ev = evidence_ages(store, now, max_age_h)
            summary.update(stale=ev["stale"], error=ev["error"], max_age_h=ev["max_age_h"], per_repo=ev["per_repo"])
            if ev["stale"]:
                codes.append("stale")
            if ev["error"]:
                codes.append("error")
        if expect_key_rules and store["key_rules"] != expect_key_rules:
            codes.append("key-rules")
    if launchd["state"] == "missing":
        codes.append("launchd-missing")
    elif launchd["state"] == "unknown":
        codes.append("launchd-unknown")
    elif launchd["state"] == "probe-failed":
        codes.append("launchd-probe-failed")
    elif launchd["state"] == "signal":
        codes.append("launchd-signal")
        summary["signal"] = launchd.get("signal")
    elif launchd["state"] == "skip" and expect_github:     # 재활성화 뒤 라벨을 못 정함 = 사이드카 종료 점검이 빠진다
        codes.append("launchd-label-missing")
    elif launchd["state"] == "ok" and launchd["code"] != 0:
        codes.append("exit")
    if logscan["state"] == "missing":
        codes.append("log-missing")
    else:
        if logscan["github_error_lines"]:
            codes.append("log-github")
        summary["log_age_s"] = log_age_s
        if log_age_s is None or stall_after_s is None:
            summary["stall"] = "unknown"
        elif log_age_s > stall_after_s:
            summary["stall"] = "stalled"
            codes.append("sidecar-stalled")
        else:
            summary["stall"] = "ok"
    return codes, summary


def fmt_iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_state(path: str) -> Tuple[Dict[str, str], Dict[str, int]]:
    """-> ({코드: 마지막 알림 시각}, {코드: 그때 크기}). 옛 상태 파일(sent 만)도 읽는다."""
    try:
        with open(path, encoding="utf-8") as fh:
            rec = json.load(fh)
    except (OSError, ValueError):
        return {}, {}
    if not isinstance(rec, dict):
        return {}, {}
    sent, size = rec.get("sent"), rec.get("size")
    sent = {k: v for k, v in sent.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(sent, dict) else {}
    size = {k: v for k, v in size.items() if isinstance(k, str) and isinstance(v, int)} if isinstance(size, dict) else {}
    return sent, size


def save_state(path: str, sent: Dict[str, str], size: Optional[Dict[str, int]] = None) -> None:
    """0600 임시 파일에 쓰고 os.replace. 실패는 한 줄 기록만 한다(다음 실행이 다시 알린다)."""
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"sent": dict(sorted(sent.items())), "size": dict(sorted((size or {}).items()))}, fh,
                      sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError as e:
        print("ITEM_ROLES_OBSERVE state-write-error %s" % type(e).__name__, file=sys.stderr)
        try:
            os.remove(tmp)
        except OSError:
            pass


def code_sizes(codes: Sequence[str], summary: Dict[str, Any]) -> Dict[str, int]:
    """이상 코드마다 크기(stale·error = 리포 수, log-github = 오류 줄 수, 그 밖 = 1). 크기가 커지면 다시 알린다."""
    field = {"stale": "stale", "error": "error", "log-github": "log_github_err"}
    out = {}
    for c in codes:
        v = summary.get(field[c]) if c in field else 1
        out[c] = v if isinstance(v, int) else 1
    return out


def due_codes(codes: Sequence[str], sent: Dict[str, str], now: dt.datetime, repeat_h: float,
              sizes: Optional[Dict[str, int]] = None, prev_sizes: Optional[Dict[str, int]] = None) -> List[str]:
    """알릴 코드: 처음 보는 코드, 마지막 알림에서 repeat_h 이상 지난 코드, 직전 알림 때보다 크기가 커진 코드."""
    sizes, prev_sizes = sizes or {}, prev_sizes or {}
    out = []
    for c in codes:
        t = parse_time(sent.get(c)) if sent.get(c) else None
        grown = c in prev_sizes and sizes.get(c, 1) > prev_sizes[c]
        if t is None or grown or (now - t).total_seconds() >= repeat_h * 3600 - 1e-6:
            out.append(c)
    return out


def summary_line(summary: Dict[str, Any]) -> str:
    keys = ("store", "repos", "github", "stale", "error", "max_age_h", "key_rules", "launchd", "exit", "log",
            "log_github_err", "log_age_s", "stall", "signal")
    return " ".join("%s=%s" % (k, summary[k]) for k in keys if k in summary)


# --- 실행 -------------------------------------------------------------------------------------------------

def default_label() -> Optional[str]:
    prefix = os.environ.get("HC_LAUNCHD_PREFIX")
    return prefix + LABEL_SUFFIX if prefix else None


def main(argv: Optional[Sequence[str]] = None, runner: Optional[Runner] = None,
         notifier: Optional[Callable[[str, bool], None]] = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--db", default=DEFAULT_DB, help="역할 저장소(읽기 전용으로 연다)")
    ap.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS, help="근거 나이 상한(기본 2시간)")
    ap.add_argument("--label", default=None,
                    help="사이드카 launchd 라벨(기본 $HC_LAUNCHD_PREFIX%s, 둘 다 없으면 launchd 점검 생략)" % LABEL_SUFFIX)
    ap.add_argument("--log", default=DEFAULT_LOG, help="사이드카 로그")
    ap.add_argument("--state", default=DEFAULT_STATE, help="반복 억제 상태 파일(0600)")
    ap.add_argument("--repeat-hours", type=float, default=DEFAULT_REPEAT_HOURS, help="같은 이상 재알림 간격(기본 6시간)")
    ap.add_argument("--expect-github", action="store_true", help="근거 테이블이 없으면 이상(재활성화 뒤)")
    ap.add_argument("--expect-key-rules", default=None, help="저장소 key_rules 가 이 값이 아니면 이상")
    ap.add_argument("--sidecar-interval", type=float, default=DEFAULT_SIDECAR_INTERVAL,
                    help="사이드카 launchd StartInterval(초, 기본 900). 로그가 이 %d배보다 오래되면 sidecar-stalled" % STALL_FACTOR)
    ap.add_argument("--busy-timeout", type=float, default=DEFAULT_BUSY_TIMEOUT, help="sqlite 잠금 대기(초)")
    ap.add_argument("--dry-run", action="store_true", help="알림 전송·상태 기록 없이 stdout 만")
    ap.add_argument("--json", action="store_true", help="요약을 JSON 한 줄로(리포별 근거 나이 포함)")
    ap.add_argument("--now", default=None, help="테스트용 ISO 시각(타임존 포함)")
    args = ap.parse_args(argv)
    now = parse_time(args.now) if args.now else dt.datetime.now(dt.timezone.utc)
    if now is None:
        ap.error("--now must be an ISO time")
    runner = runner or launchctl_runner()
    notifier = notifier or default_notifier
    label = args.label if args.label is not None else default_label()

    store = read_store(args.db, args.busy_timeout)
    codes, summary = evaluate(store, launchd_exit(label, runner), scan_log(args.log), now, args.max_age_hours,
                              args.expect_github, args.expect_key_rules, log_age(args.log, now),
                              args.sidecar_interval * STALL_FACTOR)
    per_repo = summary.pop("per_repo", None)
    line = summary_line(summary)

    def out(status: str) -> None:
        if args.json:
            rec = dict(summary, status=status, anomalies=codes, now=fmt_iso(now))
            if per_repo is not None:
                rec["repos_detail"] = per_repo
            print(json.dumps(rec, ensure_ascii=False, sort_keys=True), flush=True)
        else:
            print("ITEM_ROLES_OBSERVE %s%s %s at=%s" % (status, (" codes=" + ",".join(codes)) if codes else "", line,
                                                         fmt_iso(now)), flush=True)

    if not codes:
        if not args.dry_run and os.path.exists(args.state):
            save_state(args.state, {})
        out("ok")
        return 0
    sent, prev_sizes = load_state(args.state)
    sizes = code_sizes(codes, summary)
    due = due_codes(codes, sent, now, args.repeat_hours, sizes, prev_sizes)
    # 지금 없는 코드는 지운다(회복 뒤 재발은 바로 알린다). 남은 코드는 직전 알림 시각·크기를 그대로 둔다
    keep = {c: sent[c] for c in codes if c in sent}
    keep_sizes = {c: prev_sizes[c] for c in codes if c in prev_sizes and c in keep}
    if not due:
        out("anomaly-suppressed")
        if not args.dry_run:
            save_state(args.state, keep, keep_sizes)
        return 1
    out("anomaly")
    msg = "%s 이상: %s | %s" % (TAG, ",".join(codes), line)
    if args.json:                        # --json 의 stdout 은 JSON 한 줄만: 알림기의 출력(메시지·전송 로그)은 stderr 로
        with contextlib.redirect_stdout(sys.stderr):
            delivered = notifier(msg, args.dry_run)
    else:
        delivered = notifier(msg, args.dry_run)
    if not args.dry_run:
        # 전달된 알림만 시각·크기를 적는다(자격증명 없음·Discord 실패면 다음 실행이 다시 알린다). 실패여도 정리는 쓴다
        if delivered is True:
            keep.update({c: fmt_iso(now) for c in due})
            keep_sizes.update({c: sizes[c] for c in due})
        save_state(args.state, keep, keep_sizes)
    return 1

if __name__ == "__main__":
    sys.exit(main())
