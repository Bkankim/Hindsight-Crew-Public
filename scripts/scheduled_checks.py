#!/usr/bin/env python3
"""Dated follow-up checks that report to the same Discord channel as model-audit.

Two jobs, both run daily by one LaunchAgent (scripts/launchd, 09:00):

- canary-22   B1 canary (issue #22). Dumps the hindsight log, runs
              `bench.canary judge` read-only and reports. Before the judgment time
              only an immediate-rollback signal (exit ROLLBACK, or INVALID other than
              a stale log tail) or the daily one-line interim is sent (a stale-tail-only
              INVALID is an interim line, not an alarm); from the judgment time on, the verdict is
              sent and a final verdict (PASS/ROLLBACK/HOLD) ends the job. INVALID
              ends it too (final alert once, marker): the window start (since) is fixed,
              so rotated-away logs, a mode mismatch or an invalid baseline never
              heal by themselves and a daily repeat only adds noise. The exceptions
              are a stale log tail, which heals once requests resume, and a
              server.log.1.gz that was being gzipped while the judge read it (only
              `server.log.1.gz ... truncated or corrupt` reasons): it heals on the
              next run, so it ends the job only when the same damage repeats on two
              different days (record: <state dir>/canary-22.damaged).
- tag-hygiene-44  #44 re-measurement. From the due time on, exports personal-demo
              documents read-only, runs `scripts/tag_hygiene.py report --since
              <baseline>` and reports pass/fail once.

A job that finished writes a marker under the state dir and stays quiet after.
Before any job starts, the run waits for the network (DNS of pypi.org and discord.com,
issue #266): launchd fires a missed StartCalendarInterval job in a DarkWake, before
Wi-Fi is up. When the network never comes up within --net-wait seconds, no job starts,
SCHEDULED_NET_UNAVAILABLE is logged and the run exits 75 (EX_TEMPFAIL).
A Discord message that failed to send is kept in <state dir>/notify-pending.jsonl
and re-sent at the start of the next run (removed once Discord accepts it).
Delivery is Discord bot REST with GGG_* from the environment (the wrapper
extracts them from ~/.env); without them the message goes to stdout only.
Never prints or logs credentials.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple

KST = dt.timezone(dt.timedelta(hours=9))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.expanduser("~/Library/Logs/hindsight-scheduled")
CANARY_DIR = os.path.expanduser("~/.local/share/hindsight-bench/canary")

CANARY = {
    "since_kst": "2026-09-29 01:15:19",
    "judge_after": dt.datetime(2026, 10, 6, 1, 15, 19, tzinfo=KST),
    "baseline": os.path.join(CANARY_DIR, "baseline-20260929T011427.json"),
}
HYGIENE = {
    "baseline_utc": "2026-09-30T19:45:41Z",
    "due": dt.datetime(2026, 10, 14, 19, 45, 41, tzinfo=dt.timezone.utc),
}
# issue #266: DarkWake 에서는 DNS 가 없다. 작업 전에 이 호스트들이 해석될 때까지 기다린다.
# pypi.org 는 report 단계의 uv, discord.com 은 notify 가 쓴다.
NET_HOSTS = ("pypi.org", "discord.com")
NET_WAIT_SECONDS = 600       # 깨어 있는 시간 기준(macOS monotonic 은 잠자기 동안 멈춘다 - 다시 잠들면 다음 깨어남에서 이어 기다린다)
NET_POLL_SECONDS = 10
EX_TEMPFAIL = 75
OUTBOX_NAME = "notify-pending.jsonl"
# notify 실패 메시지를 남길 파일. main() 만 켠다 - notify 를 빌려 쓰는 item_roles_observe 는 자기 반복 억제가 있어 큐에 넣지 않는다
OUTBOX: Optional[str] = None
# bench.canary judge exit codes
CANARY_STATUS = {0: "PASS", 1: "ROLLBACK", 2: "HOLD", 3: "PENDING", 4: "INVALID"}
CANARY_FINAL = {0, 1, 2}
# bench.canary.coverage_problems 의 꼬리 사유 머리말. INVALID 사유가 전부 이것이면 다음 요청이 로그에 찍힐 때 풀리므로 종결로 보지 않는다
CANARY_TAIL_STALE = "log-coverage: sidecar log ends at"
# 가장 최근 회전본 server.log.1.gz 의 손상 사유 머리말(issue #179). rotate-logs.sh 의 gzip -f 도중 읽히면 나고 다음 실행이면 풀린다.
# .2.gz 이상은 gzip 이 끝난 세대라 영구 손상이므로 일시 사유가 아니다. 같은 사유가 서로 다른 날 2회 이어지면 영구 손상으로 보고 종결한다
CANARY_GZ1_DAMAGED = "log-coverage: sidecar log server.log.1.gz is truncated or corrupt ("


def log(msg: str) -> None:
    print(f"{dt.datetime.now(KST).isoformat(timespec='seconds')} {msg}", flush=True)


def marker(job: str) -> str:
    return os.path.join(STATE_DIR, f"{job}.done")


def damaged_path(job: str) -> str:
    return os.path.join(STATE_DIR, f"{job}.damaged")


def canary_tail_only(judge: Dict) -> bool:
    """True when every INVALID reason is a stale log tail (heals once the next request is logged)."""
    reasons = judge.get("reasons") or []
    return bool(reasons) and all(r.startswith(CANARY_TAIL_STALE) for r in reasons)


def canary_gz1_damage(judge: Dict) -> List[str]:
    """The server.log.1.gz damage reasons of a judge result."""
    return [r for r in judge.get("reasons") or [] if r.startswith(CANARY_GZ1_DAMAGED)]


def canary_transient_only(judge: Dict) -> bool:
    """True when every INVALID reason is transient: a stale log tail (heals once the next request is logged) or a being-written server.log.1.gz."""
    reasons = judge.get("reasons") or []
    return bool(reasons) and all(r.startswith((CANARY_TAIL_STALE, CANARY_GZ1_DAMAGED)) for r in reasons)


def canary_is_final(code: int, judge: Dict, now: dt.datetime, prev_damaged: Optional[Dict] = None) -> bool:
    """True when this run ends the canary job (final alert + done marker). Only counts from the judgment time on.
    prev_damaged is the previous run's server.log.1.gz damage record ({"date", "reasons"}): the same damage on a different day twice
    in a row is permanent and ends the job."""
    if now < CANARY["judge_after"]:
        return False
    if code in CANARY_FINAL:
        return True
    if code != 4:
        return False
    if not canary_transient_only(judge):
        return True
    damage = canary_gz1_damage(judge)
    return bool(damage and prev_damaged and prev_damaged.get("date") != now.astimezone(KST).date().isoformat()
                and prev_damaged.get("reasons") == damage)


def read_damaged(job: str) -> Optional[Dict]:
    try:
        rec = json.load(open(damaged_path(job)))
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def update_damaged(job: str, code: int, judge: Dict, now: dt.datetime, final: bool) -> None:
    """Record this run's server.log.1.gz damage for the next run; any other outcome (healthy verdict, other reason, final) clears it."""
    damage = canary_gz1_damage(judge) if code == 4 and not final and canary_transient_only(judge) else []
    path = damaged_path(job)
    tmp = f"{path}.tmp"
    try:
        if damage:
            with open(tmp, "w") as fh:
                json.dump({"date": now.astimezone(KST).date().isoformat(), "reasons": damage}, fh, ensure_ascii=False)
            os.replace(tmp, path)
        elif os.path.exists(path):
            os.remove(path)
    except OSError as e:                         # 기록 실패가 알림 뒤 다른 점검(tag-hygiene-44)을 막지 않는다. 연속 횟수는 다음 실행에서 다시 센다
        log(f"SCHEDULED_CANARY damaged-record-error {type(e).__name__}")
        try:
            os.remove(tmp)
        except OSError:
            pass


def canary_message(code: int, judge: Dict, now: dt.datetime, prev_damaged: Optional[Dict] = None) -> Optional[str]:
    """What to send for one canary run, or None to stay quiet."""
    status = CANARY_STATUS.get(code, f"exit{code}")
    c, b = judge.get("candidate") or {}, judge.get("baseline") or {}
    retries = (judge.get("retries") or {}).get("exhausted")
    errs = judge.get("sidecar_errors") or {}
    line = (f"cold {c.get('cold_recalls')}건 p95 {(c.get('cold_recall_p95') or 0):.2f}s(기준 {(b.get('cold_recall_p95') or 0):.2f}s) "
            f"7.5s 초과 {c.get('cold_exceed')}건 | warm p95 {(c.get('warm_recall_p95') or 0):.2f}s(기준 {(b.get('warm_recall_p95') or 0):.2f}s) "
            f"p99 {(c.get('warm_recall_p99') or 0):.2f}s(기준 {(b.get('warm_recall_p99') or 0):.2f}s) | 재시도 소진 {retries} | "
            f"503 {errs.get('http_503')} deadline {errs.get('deadline')}")
    reasons = "; ".join(judge.get("reasons") or [])
    if now < CANARY["judge_after"]:
        if code == 1 or (code == 4 and not canary_tail_only(judge)):
            return f"🚨 [#22 카나리] 즉시 확인 필요: {status}\n{line}\n사유: {reasons}"
        return f"[#22 카나리] 중간 {status} ({reasons})\n{line}"
    final = canary_is_final(code, judge, now, prev_damaged)
    head = f"[#22 카나리] 판정 {status}" + (" - 최종" if final else "")
    damage = canary_gz1_damage(judge)
    tail = ("\n다음: 세션에서 #22 판정 절차(롤백이면 .env.local 키 제거 후 hindsight 재생성)를 진행하세요."
            if code in CANARY_FINAL else
            "\n다음: 자동 점검은 여기서 끝납니다. 세션에서 INVALID 사유(로그 회전·방식 불일치·기준선 무효)를 확인하고 수동으로 판정하세요."
            + ("\n손상된 server.log.1.gz 를 확인하세요." if damage else "")
            if final else
            "\n다음 실행에서 재판정합니다(쓰는 중인 회전본 손상은 보통 스스로 풀립니다)."
            if code == 4 and damage else "")
    return f"{head}\n{line}\n사유: {reasons}{tail}"


def hygiene_message(report: Dict) -> Tuple[str, bool]:
    """(message, passed) for the #44 re-measurement."""
    problems = report.get("problem_types") or {}
    counts = {k: (v.get("documents") if isinstance(v, dict) else v) for k, v in problems.items()}
    missing_kind = (report.get("missing_kind") or {})
    missing_kind = missing_kind.get("documents") if isinstance(missing_kind, dict) else missing_kind
    total_docs = (report.get("reference") or {}).get("documents_scanned", 0)
    passed = bool(total_docs) and all(not n for n in counts.values()) and not missing_kind
    verdict = "통과" if passed else ("보류(새 문서 0건)" if not total_docs else "실패")
    msg = (f"[#44 태그 위생 2주 재측정] {verdict} (기준선 {HYGIENE['baseline_utc']} 이후 새 문서 {total_docs}건)\n"
           f"문제 유형 {counts} | kind 누락 {missing_kind}\n"
           + ("다음: #44 닫고 #35 완료 기준 갱신 후 #35 닫기." if passed
              else "다음: 기간을 늘려 다시 잰다." if not total_docs
              else "다음: tag_hygiene --json 과 --audit 로 출처를 붙여 관찰 이슈 발행(#35는 열어 둠)."))
    return msg, passed


# issue #129: compose-go dotenv 파서는 형식이 틀린 .env 줄 원문(`=값`·따옴표 값)을 오류에 싣는다. stderr 는 기록·전송하기 전에
# 이 함수를 거친다 - 줄 번호·오류 종류·일반 문구는 남기고 값만 가린다. import 경로를 묶지 않으려고 파일마다 같은 헬퍼를 둔다.
_DOTENV_LINE = re.compile(r"\bline \d+: ")
_UNTERMINATED = re.compile(r"(unterminated quoted value ).*")
_QUOTED = re.compile(r'"(?:[^"\\]|\\.)*"')                  # %q 가 만든 따옴표 문자열(이스케이프 포함)
_ASSIGN = re.compile(r"""(?P<k>[A-Za-z_][\w.\[\]-]*)=(?:"(?P<q>(?:[^"\\]|\\.)*)"?|'[^']*'?|\S*)""")
# compose 경고 줄(time="..." level=warning msg="...")은 허용 목록에 맞는 값만 남긴다. 허용 밖은 전부 가린다(유출보다 진단 손실이 낫다).
_LOG_LEVELS = frozenset(("trace", "debug", "info", "warn", "warning", "error", "fatal", "panic"))
_TIMESTAMP = re.compile(r"[0-9T:.Z+-]+")


def _redact_assign(a, logfmt_ok):
    k, q = a["k"], a["q"]
    if logfmt_ok and (a.start() == 0 or a.string[a.start() - 1].isspace()):      # 키 앞이 줄 시작 또는 공백일 때만
        v = q if q is not None else a.group(0)[len(k) + 1:]
        if (k == "time" and _TIMESTAMP.fullmatch(v)) or (k == "level" and v in _LOG_LEVELS) \
                or (k == "msg" and q is not None and a.group(0) == f'{k}="{q}"' and "=" not in q):
            return a.group(0)
    return k + "=<redacted>"


def _mask_stderr(text: str) -> str:
    out = []
    for ln in (text or "").removesuffix("\n").split("\n"):    # Go 는 \n 에서만 줄을 자른다 - splitlines() 는 U+2028 등에서도 쪼갠다
        ln = ln.removesuffix("\r")
        m = _DOTENV_LINE.search(ln)
        if m:                                             # dotenv 오류 줄: `line N:` 뒤의 따옴표 값은 전부 가린다
            rest = _UNTERMINATED.sub(r"\1<redacted>", ln[m.end():])
            rest = re.sub(r'"[^"]*$', "<redacted>", _QUOTED.sub("<redacted>", rest))
            ln = ln[:m.end()] + rest
        out.append(_ASSIGN.sub(lambda a: _redact_assign(a, m is None), ln))
    return "\n".join(out)


def stderr_tail(text: str, n: int = 300) -> str:
    """notify 본문에 싣는 stderr 꼬리. 마스킹을 먼저 하고 자른다(먼저 자르면 시크릿 중간부터 남아 패턴이 안 잡힌다)."""
    return _mask_stderr(text)[-n:]


def _credentials() -> Tuple[Optional[str], Optional[str]]:
    return os.environ.get("GGG_DISCORD_BOT_TOKEN"), os.environ.get("GGG_DISCORD_CHANNEL_ID")


def _post(message: str, bot: str, channel: str) -> bool:
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{channel}/messages",
        data=json.dumps({"content": message[:1900]}).encode(),
        headers={"Authorization": "Bot " + bot, "Content-Type": "application/json",
                 # Discord 403s the default Python-urllib user agent.
                 "User-Agent": "DiscordBot (hindsight-crew scheduled-checks, 1.0)"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15):
            pass
        log("notified via Discord")
        return True
    except (urllib.error.URLError, OSError) as e:
        log(f"SCHEDULED_NOTIFY_FAILED discord {e}")
        return False


def notify(message: str, dry_run: bool) -> bool:
    """True only when Discord accepted the message (issue #415 keys its repeat-suppression state on it).
    When OUTBOX is set (scheduled-checks runs), a message Discord did not accept is queued for the next run (issue #266)."""
    bot, channel = _credentials()
    print(message, flush=True)
    if dry_run or not (bot and channel):
        log("notify skipped (dry-run or no GGG_* credentials)")
        return False
    if _post(message, bot, channel):
        return True
    if OUTBOX:
        enqueue(message)
    return False


def enqueue(message: str, now: Optional[dt.datetime] = None) -> None:
    """Append one unsent message to OUTBOX. A write failure is logged and never breaks the run."""
    if not OUTBOX:
        return
    stamp = (now or dt.datetime.now(KST)).isoformat(timespec="seconds")
    try:
        with open(OUTBOX, "a") as fh:
            fh.write(json.dumps({"queued_at": stamp, "message": message}, ensure_ascii=False) + "\n")
        log("SCHEDULED_NOTIFY_QUEUED")
    except OSError as e:
        log(f"SCHEDULED_NOTIFY_QUEUE_ERROR {type(e).__name__}")


def _read_outbox() -> List[Dict]:
    try:
        lines = open(OUTBOX).read().splitlines() if OUTBOX else []
    except OSError:
        return []
    entries = []
    for ln in lines:
        try:
            rec = json.loads(ln)
        except ValueError:
            rec = None
        if isinstance(rec, dict) and isinstance(rec.get("message"), str):
            entries.append(rec)
        elif ln.strip():
            log("SCHEDULED_NOTIFY_QUEUE_DROP unreadable-line")
    return entries


def flush_outbox() -> int:
    """Re-send queued messages at the start of a run; keep only the ones Discord still refused. Returns how many remain."""
    entries = _read_outbox()
    if not entries:
        return 0
    bot, channel = _credentials()
    if not (bot and channel):
        log(f"SCHEDULED_NOTIFY_RESEND skipped (no GGG_* credentials) pending={len(entries)}")
        return len(entries)
    left = [e for e in entries
            if not _post(f"[재전송 - 원래 {e.get('queued_at')}]\n{e['message']}", bot, channel)]
    tmp = f"{OUTBOX}.tmp"
    try:
        if left:
            with open(tmp, "w") as fh:
                fh.writelines(json.dumps(e, ensure_ascii=False) + "\n" for e in left)
            os.replace(tmp, OUTBOX)
        else:
            os.remove(OUTBOX)
    except OSError as e:
        log(f"SCHEDULED_NOTIFY_QUEUE_ERROR {type(e).__name__}")
    log(f"SCHEDULED_NOTIFY_RESEND sent={len(entries) - len(left)} pending={len(left)}")
    return len(left)


def _resolve(host: str) -> None:
    socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)


def wait_for_network(hosts=NET_HOSTS, timeout: float = NET_WAIT_SECONDS, poll: float = NET_POLL_SECONDS,
                     sleep=time.sleep, clock=time.monotonic) -> List[str]:
    """Wait until every host resolves. Returns the hosts still unresolved at the deadline ([] = ready)."""
    deadline = clock() + timeout
    while True:
        missing = []
        for host in hosts:
            try:
                _resolve(host)
            except OSError:              # socket.gaierror 포함
                missing.append(host)
        if not missing or clock() >= deadline:
            return missing
        sleep(max(0.0, min(poll, deadline - clock())))


def run(cmd: List[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, **kw)


def job_canary(now: dt.datetime, dry_run: bool) -> int:
    stamp = now.strftime("%Y%m%dT%H%M%S")
    hlog = os.path.join(CANARY_DIR, f"hindsight-{stamp}.log")
    out = os.path.join(CANARY_DIR, f"judge-{stamp}.json")
    with open(hlog, "w") as fh:
        dump = subprocess.run(["docker", "compose", "--env-file", ".env.local", "logs", "--no-color",
                               "--timestamps", "hindsight"], cwd=ROOT, stdout=fh, stderr=subprocess.PIPE, text=True)
    if dump.returncode != 0:
        notify(f"[#22 카나리] 로그 덤프 실패 rc={dump.returncode}: {stderr_tail(dump.stderr)}", dry_run)
        return 1
    res = run([sys.executable, "-m", "bench.canary", "judge", "--baseline", CANARY["baseline"],
               "--since", CANARY["since_kst"], "--hindsight-log", hlog, "--out", out])
    try:
        judge = json.load(open(out))
    except (OSError, ValueError):
        notify(f"[#22 카나리] judge 실행 실패 rc={res.returncode}: {stderr_tail(res.stderr)}", dry_run)
        return 1
    prev = read_damaged("canary-22")
    msg = canary_message(res.returncode, judge, now, prev)
    if msg:
        notify(msg, dry_run)
    final = canary_is_final(res.returncode, judge, now, prev)
    if final and not dry_run:
        open(marker("canary-22"), "w").write(f"{now.isoformat()} {CANARY_STATUS[res.returncode]}\n")
    if now >= CANARY["judge_after"] and not dry_run:
        update_damaged("canary-22", res.returncode, judge, now, final)
    log(f"SCHEDULED_CANARY status={CANARY_STATUS.get(res.returncode)} out={out}")
    return 0


def job_hygiene(now: dt.datetime, dry_run: bool) -> int:
    if now < HYGIENE["due"]:
        log("SCHEDULED_HYGIENE not-due")
        return 0
    bank = os.environ.get("HC_PERSONAL_BANK")   # .env.host.local via scheduled-checks.sh (issue #186)
    if not bank:
        log("SCHEDULED_FAIL - config_missing HC_PERSONAL_BANK")
        notify("[#44 재측정] SCHEDULED_FAIL - config_missing HC_PERSONAL_BANK", dry_run)
        return 1
    stamp = now.strftime("%Y%m%dT%H%M%S")
    docs = os.path.join(STATE_DIR, f"docs-{stamp}.jsonl")
    with open(os.path.join(ROOT, "scripts", "tag_hygiene.py")) as src, open(docs, "w") as fh:
        exp = subprocess.run(["docker", "compose", "--env-file", ".env.local", "exec", "-T", "hindsight",
                              "python", "-", "export", "--bank", bank], cwd=ROOT, stdin=src, stdout=fh,
                             stderr=subprocess.PIPE, text=True)
    if exp.returncode != 0:
        notify(f"[#44 재측정] export 실패 rc={exp.returncode}: {stderr_tail(exp.stderr)}", dry_run)
        return 1
    rep = run(["uv", "run", "--isolated", "--python", "3.12", "--with-requirements", "gateway/requirements.txt",
               "python", "scripts/tag_hygiene.py", "report", docs, "--since", HYGIENE["baseline_utc"], "--json"])
    try:
        report = json.loads(rep.stdout)
    except ValueError:
        notify(f"[#44 재측정] report 실패 rc={rep.returncode}: {stderr_tail(rep.stderr)}", dry_run)
        return 1
    msg, passed = hygiene_message(report)
    notify(msg, dry_run)
    if not dry_run and (report.get("reference") or {}).get("documents_scanned"):
        open(marker("tag-hygiene-44"), "w").write(f"{now.isoformat()} {'PASS' if passed else 'FAIL'}\n")
    log(f"SCHEDULED_HYGIENE passed={passed}")
    return 0


JOBS = {"canary-22": job_canary, "tag-hygiene-44": job_hygiene}


def main(argv: Optional[List[str]] = None) -> int:
    global OUTBOX
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("jobs", nargs="*", default=list(JOBS), help="기본: 전부")
    ap.add_argument("--dry-run", action="store_true", help="Discord 대신 stdout만, 완료 표시 안 함")
    ap.add_argument("--now", help="테스트용 ISO 시각(타임존 포함)")
    ap.add_argument("--net-wait", type=float, default=NET_WAIT_SECONDS,
                    help=f"작업 전 네트워크(DNS)를 기다리는 최대 초, 깨어 있는 시간 기준(기본 {NET_WAIT_SECONDS})")
    args = ap.parse_args(argv)
    os.makedirs(STATE_DIR, exist_ok=True)
    OUTBOX = None if args.dry_run else os.path.join(STATE_DIR, OUTBOX_NAME)
    pending = []
    for job in args.jobs:
        if os.path.exists(marker(job)):
            log(f"SCHEDULED_SKIP {job} done ({open(marker(job)).read().strip()})")
        else:
            pending.append(job)
    if not pending and not _read_outbox():
        return 0
    missing = wait_for_network(timeout=args.net_wait)
    if missing:
        log(f"SCHEDULED_NET_UNAVAILABLE dns={','.join(missing)} waited={args.net_wait:g}s jobs={','.join(pending) or '-'}")
        if OUTBOX and all(_credentials()):
            enqueue(f"[scheduled-checks] 네트워크 없음(DNS 실패: {', '.join(missing)})으로 점검을 시작하지 않았습니다. "
                    f"대상 {', '.join(pending) or '-'} - 다음 실행에서 다시 합니다.")
        return EX_TEMPFAIL
    if OUTBOX:
        flush_outbox()
    # 기다리는 동안 잠들었다 깼으면 시각이 밀렸다 - 날짜 판정은 기다린 뒤의 시각으로 한다
    now = dt.datetime.fromisoformat(args.now) if args.now else dt.datetime.now(KST)
    rc = 0
    for job in pending:
        rc |= JOBS[job](now, args.dry_run)
    return rc


if __name__ == "__main__":
    sys.exit(main())
