"""R7-8 역할 저장소 매시간 관찰 점검(issue #415) - 네트워크·launchd·운영 저장소 없이.

저장소는 tmp_path 의 sqlite 고정 입력(`gateway.item_roles.publish` 로 만든 계약 v1 파일), launchctl 은 가짜 실행기,
Discord 는 가짜 알림기다. 상태 파일·로그도 tmp_path 아래다.
"""
import datetime as dt
import hashlib
import importlib.util
import json
import os
import sqlite3
from pathlib import Path

import pytest

from gateway import item_roles as IR

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("item_roles_observe", REPO / "scripts" / "item_roles_observe.py")
OBS = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(OBS)

NOW = "2026-11-06T03:00:00Z"
FRESH = "2026-11-06T02:30:00Z"        # 0.5시간
OLD = "2026-11-06T00:30:00Z"          # 2.5시간(기본 상한 2시간 초과)
LABEL = "com.test.hindsight-item-roles-sidecar"
META = {"model": "m", "prompt_sha256": "p", "aliases_sha256": "a", "updated_at": FRESH, "key_rules": "v2.1"}


def make_store(path, repos=None, keys=("alpha#1", "beta#2"), key_rules="v2.1"):
    """계약 v1 저장소. repos = {repo: (status, fetched_at)} 또는 None(근거 테이블 없음 = 수집 꺼짐)."""
    meta = dict(META)
    if key_rules is None:
        meta.pop("key_rules")
    else:
        meta["key_rules"] = key_rules
    mems = {"m1": {"document_id": "d", "mentioned_at": FRESH, "state": "valid", "text_sha256": "t", "judged_at": FRESH}}
    roles = {"m1": {k: "open" for k in keys}}
    github = None
    if repos is not None:
        github = {"repos": {r: {"owner": "o", "status": s, "fetched_at": f, "error_code": None if s != "error" else "http-500"}
                            for r, (s, f) in repos.items()}, "items": {}}
    IR.publish(str(path), meta, mems, roles, mode=0o644, github=github)
    return path


def launchctl(code="0", rc=0):
    """가짜 `launchctl print` 실행기. 진짜 출력처럼 env 줄(시크릿 자리)을 섞어 둔다 - 그 줄이 어디에도 남지 않아야 한다."""
    calls = []

    def run(args):
        calls.append(list(args))
        if rc != 0:
            return rc, "", "Could not find service"
        body = ("gui/501/%s = {\n\tactive count = 0\n\tstate = not running\n\tenvironment = {\n"
                "\t\tSECRET_TOKEN => SHOULD-NOT-LEAK\n\t}\n\truns = 4\n\tlast exit code = %s\n}\n" % (LABEL, code))
        return 0, body, ""
    run.calls = calls
    return run


class Notifier:
    def __init__(self, delivered=True):
        self.sent = []
        self.delivered = delivered

    def __call__(self, message, dry_run):
        self.sent.append((message, dry_run))
        return self.delivered and not dry_run


def log_lines(*runs):
    """사이드카 로그(emit JSON 줄). runs = 실행마다 github 단계 error 값(None = github 줄 없음)."""
    out = []
    for err in runs:
        out.append(json.dumps({"phase": "plan", "mode": "poll", "listed": 3}, sort_keys=True))
        if err is not None:
            out.append(json.dumps({"phase": "github", "repos": 2, "ok": 2 - err, "error": err, "missing": 0},
                                  sort_keys=True))
        out.append(json.dumps({"phase": "done", "mode": "poll", "judged_now": 0}, sort_keys=True))
    return "\n".join(out) + "\n"


@pytest.fixture
def env(tmp_path):
    """정상 환경: 근거 신선, error 0, 종료 코드 0, 마지막 실행 GitHub 오류 0."""
    db = make_store(tmp_path / "item-roles.sqlite", repos={"alpha": ("ok", FRESH), "beta": ("missing", FRESH)})
    log = tmp_path / "sidecar.log"
    log.write_text(log_lines(0), encoding="utf-8")
    return {"db": db, "log": log, "state": tmp_path / "observe-state.json", "tmp": tmp_path}


def touch_log(env, now=NOW, ago=60):
    """로그 마지막 쓰기 시각을 --now 기준 ago 초 전으로(사이드카가 도는 중인 정상 상태)."""
    t = OBS.parse_time(now).timestamp() - ago
    if env["log"].exists():
        os.utime(env["log"], (t, t))


def run_main(env, *extra, runner=None, notifier=None, now=NOW, touch=True):
    notifier = notifier if notifier is not None else Notifier()
    if touch:
        touch_log(env, now)
    runner = runner if runner is not None else launchctl()
    argv = ["--db", str(env["db"]), "--log", str(env["log"]), "--state", str(env["state"]), "--label", LABEL,
            "--now", now, *extra]
    rc = OBS.main(argv, runner=runner, notifier=notifier)
    return rc, notifier


# --- 케이스 행렬: 정상·stale·error·종료 코드·로그 GitHub 오류·저장소 없음·잠김 ---------------------------------

def _stale(env):
    make_store(env["db"], repos={"alpha": ("ok", OLD), "beta": ("ok", FRESH)})


def _unfetched(env):
    make_store(env["db"], repos={"alpha": ("ok", FRESH)})          # beta 는 근거 행이 없다(받은 적 없음 = stale)


def _error(env):
    make_store(env["db"], repos={"alpha": ("ok", FRESH), "beta": ("error", FRESH)})


def _log_error(env):
    env["log"].write_text(log_lines(0, 1), encoding="utf-8")        # 마지막 실행의 github 단계 error 1


def _missing(env):
    os.remove(env["db"])


def _not_store(env):
    env["db"].write_bytes(b"not a sqlite file at all, just bytes" * 20)


CASES = [
    # (id, 준비, launchctl 종료 코드, 기대 이상 코드 집합)
    ("normal", None, "0", set()),
    ("stale-repo", _stale, "0", {"stale"}),
    ("unfetched-repo", _unfetched, "0", {"stale"}),
    ("error-repo", _error, "0", {"error"}),
    ("exit-code-nonzero", None, "1", {"exit"}),
    ("exit-code-3", None, "3", {"exit"}),
    ("log-github-error", _log_error, "0", {"log-github"}),
    ("store-missing", _missing, "0", {"store-missing"}),
    ("store-not-sqlite", _not_store, "0", {"store-unreadable"}),
]


@pytest.mark.parametrize("case_id,prep,code,want", CASES, ids=[c[0] for c in CASES])
def test_case_matrix_anomalies_and_alert(env, case_id, prep, code, want):
    if prep:
        prep(env)
    rc, n = run_main(env, runner=launchctl(code))
    if want:
        assert rc == 1
        assert len(n.sent) == 1
        msg, dry = n.sent[0]
        assert dry is False and "\n" not in msg
        for w in want:
            assert w in msg
    else:
        assert rc == 0 and n.sent == []


def test_normal_prints_exactly_one_stdout_line(env, capsys):
    rc, n = run_main(env)
    out = capsys.readouterr().out
    assert rc == 0 and n.sent == []
    assert out.count("\n") == 1 and out.startswith("ITEM_ROLES_OBSERVE ok ")
    assert "key_rules=v2.1" in out and "github=on" in out and "exit=0" in out


def test_store_locked_is_a_read_failure_not_a_hang(env):
    """사이드카 쓰기 도중(배타 잠금) 읽기 실패 = 이상 store-locked."""
    w = sqlite3.connect(str(env["db"]))
    try:
        w.execute("BEGIN EXCLUSIVE")
        rc, n = run_main(env, "--busy-timeout", "0.05")
    finally:
        w.rollback()
        w.close()
    assert rc == 1 and len(n.sent) == 1 and "store-locked" in n.sent[0][0]


def test_stale_threshold_is_configurable(env):
    _stale(env)
    rc, n = run_main(env, "--max-age-hours", "3")
    assert rc == 0 and n.sent == []


# --- 수집 꺼짐(근거 테이블 없음)은 따로 보고한다: 재활성화 전엔 정상, 켠 뒤엔 이상 ------------------------------

@pytest.mark.parametrize("expect,want_rc", [(False, 0), (True, 1)], ids=["before-reactivation", "after-reactivation"])
def test_github_off_is_its_own_state(env, capsys, expect, want_rc):
    make_store(env["db"], repos=None)
    env["log"].write_text(log_lines(None), encoding="utf-8")
    rc, n = run_main(env, *(["--expect-github"] if expect else []))
    out = capsys.readouterr().out
    assert rc == want_rc
    assert "github=off" in out
    if expect:
        assert len(n.sent) == 1 and "github-off" in n.sent[0][0]
    else:
        assert n.sent == []


def test_key_rules_expectation(env):
    rc, n = run_main(env, "--expect-key-rules", "v2")
    assert rc == 1 and "key-rules" in n.sent[0][0]
    rc, n = run_main(env, "--expect-key-rules", "v2.1")
    assert rc == 0


# --- launchctl: 필드 하나만, 출력 전체를 남기지 않는다 ---------------------------------------------------------

def test_launchctl_output_never_leaks(env, capsys):
    runner = launchctl("1")
    rc, n = run_main(env, "--json", runner=runner)
    out = capsys.readouterr().out
    assert runner.calls and runner.calls[0][:2] == ["launchctl", "print"] and runner.calls[0][2].endswith("/" + LABEL)
    for text in (out, n.sent[0][0], env["state"].read_text()):
        assert "SHOULD-NOT-LEAK" not in text and "SECRET_TOKEN" not in text and "environment" not in text


def test_launchd_never_exited_is_not_an_anomaly(env):
    rc, n = run_main(env, runner=launchctl("(never exited)"))
    assert rc == 0 and n.sent == []


def test_launchd_not_loaded_is_an_anomaly(env):
    rc, n = run_main(env, runner=launchctl(rc=113))
    assert rc == 1 and "launchd-missing" in n.sent[0][0]


# --- 반복 알림 억제 ----------------------------------------------------------------------------------------

def _at(hours):
    t = dt.datetime(2026, 11, 6, 3, 0, tzinfo=dt.timezone.utc) + dt.timedelta(hours=hours)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


REPEAT = [
    # (id, 두 번째 실행 시각(시간 뒤), 두 번째 준비, 두 번째에 알림이 가는가)
    ("same-anomaly-1h-later", 1, None, False),
    ("same-anomaly-5h-later", 5, None, False),
    ("same-anomaly-6h-later", 6, None, True),
    ("new-anomaly-1h-later", 1, _error, True),
]


@pytest.mark.parametrize("case_id,later,prep2,want_sent", REPEAT, ids=[c[0] for c in REPEAT])
def test_repeat_alert_suppression(env, capsys, case_id, later, prep2, want_sent):
    # 근거 나이가 시각과 함께 자라지 않게 상한을 크게 두고 stale 대신 종료 코드 이상으로 반복을 본다
    rc1, n1 = run_main(env, runner=launchctl("1"), now=_at(0))
    assert rc1 == 1 and len(n1.sent) == 1
    if prep2:
        prep2(env)
    capsys.readouterr()
    rc2, n2 = run_main(env, "--max-age-hours", "24", runner=launchctl("1"), now=_at(later))
    out = capsys.readouterr().out
    assert rc2 == 1
    assert (len(n2.sent) == 1) is want_sent
    if not want_sent:
        assert out.count("\n") == 1 and "suppressed" in out


def test_recovery_clears_state_so_recurrence_alerts_again(env):
    run_main(env, runner=launchctl("1"), now=_at(0))
    rc, n = run_main(env, runner=launchctl("0"), now=_at(1))
    assert rc == 0 and n.sent == []
    rc, n = run_main(env, runner=launchctl("1"), now=_at(2))
    assert rc == 1 and len(n.sent) == 1


def test_dry_run_neither_notifies_for_real_nor_writes_state(env):
    rc, n = run_main(env, "--dry-run", runner=launchctl("1"))
    assert rc == 1 and n.sent and n.sent[0][1] is True
    assert not env["state"].exists()


def test_state_file_is_private(env):
    run_main(env, runner=launchctl("1"))
    assert (env["state"].stat().st_mode & 0o777) == 0o600


# --- 저장소는 읽기 전용으로만 연다 --------------------------------------------------------------------------

def test_store_is_opened_read_only_uri_and_left_unchanged(env, monkeypatch):
    seen = []
    real = sqlite3.connect

    def spy(database, *a, **kw):
        seen.append((database, kw.get("uri")))
        return real(database, *a, **kw)
    monkeypatch.setattr(OBS.sqlite3, "connect", spy)
    before = hashlib.sha256(env["db"].read_bytes()).hexdigest()
    mtime = env["db"].stat().st_mtime_ns
    listing = sorted(os.listdir(env["tmp"]))
    rc, _ = run_main(env)
    assert rc == 0
    assert seen, "store was never opened"
    for database, uri in seen:
        assert uri is True and str(database).startswith("file:") and str(database).endswith("?mode=ro")
    assert hashlib.sha256(env["db"].read_bytes()).hexdigest() == before
    assert env["db"].stat().st_mtime_ns == mtime
    assert sorted(f for f in os.listdir(env["tmp"]) if f != "observe-state.json") == listing   # -journal·-wal 없음


def test_read_only_connection_refuses_writes(env):
    conn = OBS.connect_ro(str(env["db"]), 0.1)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE x(a)")
    finally:
        conn.close()


# --- 알림 경로 재사용 --------------------------------------------------------------------------------------

def test_default_notifier_is_the_scheduled_checks_discord_path():
    assert OBS.default_notifier.__module__ == "scheduled_checks"
    assert OBS.default_notifier.__name__ == "notify"


# --- plist 템플릿(설치하지 않음) -----------------------------------------------------------------------------

def test_launchd_template_is_hourly_and_not_installed_by_installer():
    import plistlib
    tpl = REPO / "scripts" / "launchd" / "__PREFIX__.hindsight-item-roles-observe.plist.example"
    text = tpl.read_text(encoding="utf-8")
    data = plistlib.loads(text.encode())
    assert data["Label"] == "__PREFIX__.hindsight-item-roles-observe"
    assert data["StartInterval"] == 3600
    assert data["ProgramArguments"][-1].endswith("scripts/item-roles-observe.sh") or \
        "__REPO__/scripts/item-roles-observe.sh" in data["ProgramArguments"]
    assert data["StandardOutPath"].endswith("/observe.log")
    assert "bootout" in text            # 첫 1주 뒤 끄는 방법이 주석에 있다
    installer = (REPO / "scripts" / "install" / "hc_install.py").read_text(encoding="utf-8")
    assert "item-roles-observe" not in installer


def test_wrapper_is_valid_bash_and_execs_the_observer():
    sh = REPO / "scripts" / "item-roles-observe.sh"
    import subprocess
    assert subprocess.run(["bash", "-n", str(sh)]).returncode == 0
    text = sh.read_text(encoding="utf-8")
    assert 'exec "$PY" scripts/item_roles_observe.py "$@"' in text
    assert "GGG_DISCORD_BOT_TOKEN" in text and "echo" not in text.split("hc_resolve_notify", 1)[1]


def test_undelivered_alert_is_not_recorded_so_next_run_retries(env):
    """Discord 실패·자격증명 없음(notify 가 True 를 돌려주지 않음)이면 억제 상태를 쓰지 않는다(final-review C1)."""
    rc, n = run_main(env, runner=launchctl("1"), notifier=Notifier(delivered=False), now=_at(0))
    assert rc == 1 and len(n.sent) == 1
    assert "exit" not in json.loads(env["state"].read_text())["sent"]       # 정리만 쓰고 전송 시각은 없다
    rc, n = run_main(env, runner=launchctl("1"), now=_at(1))
    assert rc == 1 and len(n.sent) == 1


@pytest.mark.parametrize("creds,want", [(False, False)], ids=["no-credentials"])
def test_scheduled_checks_notify_reports_delivery(monkeypatch, capsys, creds, want):
    monkeypatch.delenv("GGG_DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("GGG_DISCORD_CHANNEL_ID", raising=False)
    assert OBS.default_notifier("x", False) is want
    assert OBS.default_notifier("x", True) is False


@pytest.mark.parametrize("rc", [124, 127], ids=["timeout", "not-executable"])
def test_launchctl_probe_failure_is_not_reported_as_missing(env, rc):
    """조회 실패(시간 초과·실행 불가)는 launchd-missing 이 아니라 launchd-probe-failed(final-review C4)."""
    rc_, n = run_main(env, runner=launchctl(rc=rc))
    assert rc_ == 1 and "launchd-probe-failed" in n.sent[0][0] and "launchd-missing" not in n.sent[0][0]


def test_json_mode_stdout_stays_one_json_line_when_alerting(env, capsys, monkeypatch):
    """--json 이상 경로에서 기본 notify 의 출력이 stdout 에 섞이지 않는다(final-review C10)."""
    monkeypatch.delenv("GGG_DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("GGG_DISCORD_CHANNEL_ID", raising=False)
    touch_log(env)
    rc = OBS.main(["--db", str(env["db"]), "--log", str(env["log"]), "--state", str(env["state"]), "--label", LABEL,
                   "--now", NOW, "--json"], runner=launchctl("1"))
    out = capsys.readouterr().out
    assert rc == 1 and out.count("\n") == 1
    assert json.loads(out)["anomalies"] == ["exit"]


# --- rework #415-1 ------------------------------------------------------------------------------------------

def test_undelivered_alert_still_prunes_recovered_codes(env):
    """전달 실패여도 회복된 코드는 상태에서 지운다(fix review 지적): A 전달 -> A 회복·B 전달 실패 -> A 재발은 바로 알린다."""
    rc, n = run_main(env, runner=launchctl("1"), now=_at(0))                     # A = exit, 전달됨
    assert rc == 1 and len(n.sent) == 1
    _error(env)                                                                  # B = error 생김
    rc, n = run_main(env, "--max-age-hours", "24", runner=launchctl("0"), notifier=Notifier(delivered=False),
                     now=_at(1))
    assert rc == 1 and len(n.sent) == 1                                          # B 전달 실패, A 회복
    st = json.loads(env["state"].read_text())
    assert "exit" not in st["sent"] and "error" not in st["sent"]
    rc, n = run_main(env, "--max-age-hours", "24", runner=launchctl("1"), now=_at(2))   # A 재발(6h 안)
    assert rc == 1 and len(n.sent) == 1 and "exit" in n.sent[0][0]


EXIT_FORMATS = [
    # (id, last exit code 값, 추가 줄, 기대 이상 코드(없으면 None), 기대 exit)
    ("bare-0", "0", "", None, 0),
    ("described-78", "78: EX_CONFIG", "", "exit", 78),
    ("described-3", "3: No such process", "", "exit", 3),
    ("described-0", "0: Undefined error: 0", "", None, 0),
    ("never", "(never exited)", "", None, None),
    ("signal-killed", "(never exited)", "\tlast terminating signal = Killed: 9\n", "launchd-signal", None),
    ("signal-only", None, "\tlast terminating signal = Terminated: 15\n", "launchd-signal", None),
    ("garbage", "???", "", "launchd-unknown", None),
]


@pytest.mark.parametrize("case_id,value,extra,want_code,want_exit", EXIT_FORMATS, ids=[c[0] for c in EXIT_FORMATS])
def test_launchctl_exit_field_formats(env, capsys, case_id, value, extra, want_code, want_exit):
    def run(args):
        body = "gui/501/x = {\n\tstate = not running\n"
        if value is not None:
            body += "\tlast exit code = %s\n" % value
        return 0, body + extra + "}\n", ""
    rc, n = run_main(env, "--json", runner=run)
    rec = json.loads(capsys.readouterr().out.splitlines()[0])
    assert rec["exit"] == want_exit
    if want_code is None:
        assert rc == 0 and rec["anomalies"] == []
    else:
        assert rc == 1 and want_code in rec["anomalies"]


@pytest.mark.parametrize("expect,want", [(False, []), (True, ["launchd-label-missing"])],
                         ids=["before-reactivation", "after-reactivation"])
def test_missing_label_is_an_anomaly_only_after_reactivation(env, capsys, monkeypatch, expect, want):
    monkeypatch.delenv("HC_LAUNCHD_PREFIX", raising=False)
    argv = ["--db", str(env["db"]), "--log", str(env["log"]), "--state", str(env["state"]), "--now", NOW, "--json"]
    touch_log(env)
    rc = OBS.main(argv + (["--expect-github"] if expect else []), runner=launchctl(), notifier=Notifier())
    rec = json.loads(capsys.readouterr().out.splitlines()[0])
    assert rec["launchd"] == "skip" and rec["anomalies"] == want and rc == (1 if want else 0)


GROWTH = [
    # (id, 첫 실행 stale 리포, 두 번째 실행 stale 리포, 두 번째에 알림이 가는가)
    ("grows-1-to-2", {"alpha": OLD, "beta": FRESH}, {"alpha": OLD, "beta": OLD}, True),
    ("same-size", {"alpha": OLD, "beta": FRESH}, {"alpha": OLD, "beta": FRESH}, False),
    ("shrinks-2-to-1", {"alpha": OLD, "beta": OLD}, {"alpha": OLD, "beta": FRESH}, False),
]


@pytest.mark.parametrize("case_id,first,second,want_sent", GROWTH, ids=[c[0] for c in GROWTH])
def test_growing_anomaly_realerts_inside_the_window(env, case_id, first, second, want_sent):
    make_store(env["db"], repos={r: ("ok", t) for r, t in first.items()})
    rc, n = run_main(env, now=NOW)
    assert rc == 1 and len(n.sent) == 1
    make_store(env["db"], repos={r: ("ok", t) for r, t in second.items()})
    rc, n = run_main(env, now=NOW)
    assert rc == 1 and (len(n.sent) == 1) is want_sent


STALL = [
    # (id, 로그 마지막 쓰기가 now 보다 몇 초 전, --sidecar-interval, 기대 이상)
    ("fresh-10min", 600, None, False),
    ("just-under-45min", 2690, None, False),
    ("stalled-46min", 2760, None, True),
    ("long-interval-ok", 2760, "1800", False),
]


@pytest.mark.parametrize("case_id,ago,interval,want", STALL, ids=[c[0] for c in STALL])
def test_sidecar_stalled_when_log_is_older_than_three_intervals(env, capsys, case_id, ago, interval, want):
    now = dt.datetime(2026, 11, 6, 3, 0, tzinfo=dt.timezone.utc)
    t = (now - dt.timedelta(seconds=ago)).timestamp()
    os.utime(env["log"], (t, t))
    extra = ["--json"] + (["--sidecar-interval", interval] if interval else [])
    rc, n = run_main(env, *extra, now=NOW, touch=False)
    rec = json.loads(capsys.readouterr().out.splitlines()[0])
    assert ("sidecar-stalled" in rec["anomalies"]) is want
    assert rec["log_age_s"] == ago


def test_sidecar_stall_unknown_when_log_time_unreadable(env, capsys, monkeypatch):
    real = os.stat

    def bad_stat(p, *a, **kw):
        if str(p) == str(env["log"]):
            raise OSError("no stat")
        return real(p, *a, **kw)
    touch_log(env)
    monkeypatch.setattr(OBS.os, "stat", bad_stat)
    rc, n = run_main(env, "--json", touch=False)
    rec = json.loads(capsys.readouterr().out.splitlines()[0])
    assert rec["stall"] == "unknown" and "sidecar-stalled" not in rec["anomalies"]


# --- scheduled_checks.notify 가 bool 을 돌려주게 바뀐 뒤 기존 호출자 동작 불변 ------------------------------------

def _sc():
    import sys as _sys
    return _sys.modules["scheduled_checks"]


class _Resp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.mark.parametrize("outcome,want_ret,want_log", [
    ("ok", True, "notified via Discord"),
    ("fail", False, "SCHEDULED_NOTIFY_FAILED discord"),
    ("no-creds", False, "notify skipped"),
], ids=["delivered", "http-error", "no-credentials"])
def test_notify_return_value_and_unchanged_output(monkeypatch, capsys, outcome, want_ret, want_log):
    sc = _sc()
    if outcome == "no-creds":
        monkeypatch.delenv("GGG_DISCORD_BOT_TOKEN", raising=False)
        monkeypatch.delenv("GGG_DISCORD_CHANNEL_ID", raising=False)
    else:
        monkeypatch.setenv("GGG_DISCORD_BOT_TOKEN", "fake-not-a-token")
        monkeypatch.setenv("GGG_DISCORD_CHANNEL_ID", "123")

    def fake_urlopen(req, timeout=None):
        if outcome == "fail":
            raise sc.urllib.error.URLError("down")
        return _Resp()
    monkeypatch.setattr(sc.urllib.request, "urlopen", fake_urlopen)
    assert sc.notify("hello", False) is want_ret
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "hello" and len(out) == 2 and want_log in out[1]      # 출력 모양은 전과 같다(메시지 + 로그 한 줄)
    assert "fake-not-a-token" not in "\n".join(out)


def test_scheduled_checks_callers_ignore_notify_return():
    """기존 호출자(job_canary·job_hygiene)는 notify 반환값을 쓰지 않는다 - 반환형 변경이 동작을 바꾸지 않는다."""
    import ast
    tree = ast.parse((REPO / "scripts" / "scheduled_checks.py").read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "notify"]
    stmts = [n.value for n in ast.walk(tree) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)]
    assert calls and all(c in stmts for c in calls)
