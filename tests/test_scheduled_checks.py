"""scheduled_checks 메시지 판정(#22 카나리, #44 재측정) - 네트워크·도커 없이 순수 함수만."""
import datetime as dt
import gzip
import importlib.util
import json
import os
import types

import pytest

from bench import canary as K

_spec = importlib.util.spec_from_file_location(
    "scheduled_checks", os.path.join(os.path.dirname(__file__), "..", "scripts", "scheduled_checks.py"))
sc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sc)

KST = sc.KST
BEFORE = dt.datetime(2026, 10, 2, 9, 0, tzinfo=KST)
AFTER = dt.datetime(2026, 10, 6, 9, 0, tzinfo=KST)
JUDGE = {"candidate": {"cold_recalls": 37, "cold_recall_p95": 6.87, "cold_exceed": 2,
                       "warm_recall_p95": 5.62, "warm_recall_p99": 5.9},
         "baseline": {"cold_recall_p95": 5.55, "warm_recall_p95": 5.53, "warm_recall_p99": 5.65},
         "retries": {"exhausted": 0}, "sidecar_errors": {"http_503": 3, "deadline": 3},
         "reasons": ["not-yet:cold=37/20,days=2.16/7"]}


def test_before_judgment_pending_sends_a_quiet_interim_line():
    msg = sc.canary_message(3, JUDGE, BEFORE)
    assert msg.startswith("[#22 카나리] 중간 PENDING")
    assert "🚨" not in msg


def test_before_judgment_rollback_or_invalid_is_an_alarm():
    for code in (1, 4):
        assert sc.canary_message(code, JUDGE, BEFORE).startswith("🚨 [#22 카나리] 즉시 확인 필요")


def test_after_judgment_final_verdicts_say_final_and_next_step():
    for code, name in ((0, "PASS"), (1, "ROLLBACK"), (2, "HOLD")):
        msg = sc.canary_message(code, JUDGE, AFTER)
        assert f"판정 {name} - 최종" in msg and "다음:" in msg


def test_after_judgment_pending_is_not_final():
    msg = sc.canary_message(3, JUDGE, AFTER)
    assert "최종" not in msg and "판정 PENDING" in msg


def test_missing_numbers_do_not_crash_the_message():
    assert sc.canary_message(3, {}, BEFORE)


def _report(scanned, merged=0, missing=0):
    return {"problem_types": {"merged-slug": {"documents": merged}, "misfiled-slot": {"documents": 0},
                              "user-split": {"documents": 0}},
            "missing_kind": {"documents": missing}, "reference": {"documents_scanned": scanned},
            "documents": []}


def test_hygiene_pass_fail_and_empty_window():
    assert sc.hygiene_message(_report(6))[1] is True
    msg, ok = sc.hygiene_message(_report(6, merged=1))
    assert ok is False and "실패" in msg
    msg, ok = sc.hygiene_message(_report(6, missing=2))
    assert ok is False
    msg, ok = sc.hygiene_message(_report(0))
    assert ok is False and "보류" in msg


# ---------------------------------------------------------------- INVALID 종결 (issue #104 C10)

STALE_TAIL = "log-coverage: sidecar log ends at 1.0 before candidate end 2.0 - the log copy is stale or the files after it are missing"
FRONT_LOST = "log-coverage: sidecar log starts at 2.0 after candidate start 1.0 - rotated files are missing or the window is longer than the logs"


def _judge(*reasons):
    return dict(JUDGE, reasons=list(reasons))


def test_c10_tail_reason_prefix_matches_what_bench_canary_emits():
    tail = K.coverage_problems(0.0, 1.0, 0.0, 1.0 + K.LOG_TAIL_TOLERANCE_S + 1, "candidate")
    assert len(tail) == 1 and tail[0].startswith(sc.CANARY_TAIL_STALE)
    assert not K.coverage_problems(2.0, None, 1.0, 3.0, "candidate")[0].startswith(sc.CANARY_TAIL_STALE)


@pytest.mark.parametrize("code,reasons,now,final", [
    (4, [FRONT_LOST], AFTER, True),                       # 회전으로 사라진 앞은 since 가 고정이라 스스로 안 풀린다 -> 종결
    (4, ["mode-mismatch:candidate window has {'128x3': 1}"], AFTER, True),
    (4, ["baseline-invalid:x"], AFTER, True),
    (4, [FRONT_LOST, STALE_TAIL], AFTER, True),           # 꼬리만이 아니면 종결
    (4, [STALE_TAIL], AFTER, False),                      # 꼬리만 낡았으면 다음 요청이 찍히면 풀린다 -> 내일 다시
    (4, [], AFTER, True),
    (4, [FRONT_LOST], BEFORE, False),                     # 판정 시각 전에는 종결 없음(알람만)
    (3, ["not-yet"], AFTER, False),
    (0, [], AFTER, True), (1, ["x"], AFTER, True), (2, ["x"], AFTER, True),
    (1, ["x"], BEFORE, False),
])
def test_c10_canary_is_final(code, reasons, now, final):
    assert sc.canary_is_final(code, _judge(*reasons), now) is final


def test_c10_final_invalid_message_says_final_and_points_to_manual_judgment():
    msg = sc.canary_message(4, _judge(FRONT_LOST), AFTER)
    assert msg.startswith("[#22 카나리] 판정 INVALID - 최종") and "수동으로 판정" in msg
    assert "롤백이면" not in msg                           # ROLLBACK 절차 안내가 INVALID 에 붙지 않는다
    tail_only = sc.canary_message(4, _judge(STALE_TAIL), AFTER)
    assert "최종" not in tail_only and "다음:" not in tail_only
    assert sc.canary_message(4, _judge(FRONT_LOST), BEFORE).startswith("🚨 [#22 카나리] 즉시 확인 필요")


def test_c10_stale_tail_only_invalid_before_judgment_is_a_quiet_interim_not_an_alarm():
    # 조용한 주말 뒤 꼬리만 낡은 INVALID 는 다음 요청에 풀린다 - 판정 시각 전에도 🚨 가 아니라 중간 보고다
    msg = sc.canary_message(4, _judge(STALE_TAIL), BEFORE)
    assert msg.startswith("[#22 카나리] 중간 INVALID") and "🚨" not in msg
    assert sc.canary_message(4, _judge(FRONT_LOST, STALE_TAIL), BEFORE).startswith("🚨")
    assert sc.canary_message(1, _judge(STALE_TAIL), BEFORE).startswith("🚨")      # ROLLBACK 은 항상 알람


def _wire(monkeypatch, tmp_path, code, reasons):
    """job_canary 를 임시 디렉터리·가짜 docker/judge·가짜 알림으로 돌린다(운영 로그 디렉터리·Discord 에 닿지 않는다)."""
    state, canary = tmp_path / "state", tmp_path / "canary"
    state.mkdir(), canary.mkdir()
    for k in ("GGG_DISCORD_BOT_TOKEN", "GGG_DISCORD_CHANNEL_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(sc, "STATE_DIR", str(state))
    monkeypatch.setattr(sc, "CANARY_DIR", str(canary))
    sent = []
    monkeypatch.setattr(sc, "notify", lambda message, dry_run: sent.append(message))
    monkeypatch.setattr(sc, "subprocess", types.SimpleNamespace(
        PIPE=-1, run=lambda cmd, **kw: types.SimpleNamespace(returncode=0, stderr="")))        # docker compose logs 대체

    def fake_run(cmd, **kw):
        json.dump(_judge(*reasons), open(cmd[cmd.index("--out") + 1], "w"))
        return types.SimpleNamespace(returncode=code, stderr="")
    monkeypatch.setattr(sc, "run", fake_run)
    return state, sent


@pytest.mark.parametrize("reasons,done", [([FRONT_LOST], True), ([STALE_TAIL], False)])
def test_c10_invalid_after_judgment_writes_marker_once_unless_only_the_tail_is_stale(monkeypatch, tmp_path, reasons, done):
    state, sent = _wire(monkeypatch, tmp_path, 4, reasons)
    assert sc.main(["canary-22", "--now", AFTER.isoformat()]) == 0
    assert len(sent) == 1
    assert (state / "canary-22.done").exists() is done
    assert sc.main(["canary-22", "--now", (AFTER + dt.timedelta(days=1)).isoformat()]) == 0
    assert len(sent) == (1 if done else 2)                # 종결이면 다음 날은 SCHEDULED_SKIP - 알림이 더 나가지 않는다
    if done:
        assert (state / "canary-22.done").read_text().split()[1] == "INVALID"


def test_c10_invalid_before_judgment_and_dry_run_write_no_marker(monkeypatch, tmp_path):
    state, sent = _wire(monkeypatch, tmp_path, 4, [FRONT_LOST])
    sc.main(["canary-22", "--now", BEFORE.isoformat()])
    sc.main(["canary-22", "--now", AFTER.isoformat(), "--dry-run"])
    assert not (state / "canary-22.done").exists() and len(sent) == 2


def test_c10_existing_final_verdicts_still_write_the_marker(monkeypatch, tmp_path):
    state, sent = _wire(monkeypatch, tmp_path, 2, ["x:boundary"])
    sc.main(["canary-22", "--now", AFTER.isoformat()])
    assert (state / "canary-22.done").read_text().split()[1] == "HOLD" and "판정 HOLD - 최종" in sent[0]


# ---------------------------------------------------------------- 쓰는 중 .1.gz 손상 사유는 일시 사유 (issue #179)

GZ1_DAMAGED = ("log-coverage: sidecar log server.log.1.gz is truncated or corrupt (EOFError) - "
               "only its readable first part was used")


def test_i179_repro_gz1_damage_only_invalid_must_not_write_the_marker_and_is_rejudged_next_day(monkeypatch, tmp_path):
    # rotate-logs.sh 의 gzip -f 도중에 읽힌 server.log.1.gz 는 다음 실행이면 스스로 풀린다 - 마커를 쓰면 다음 날 PASS 입력을 SKIP 한다
    state, sent = _wire(monkeypatch, tmp_path, 4, [GZ1_DAMAGED])
    assert sc.main(["canary-22", "--now", AFTER.isoformat()]) == 0
    assert not (state / "canary-22.done").exists() and "최종" not in sent[0]
    _set_judge(monkeypatch, 0)
    assert sc.main(["canary-22", "--now", (AFTER + dt.timedelta(days=1)).isoformat()]) == 0
    assert (state / "canary-22.done").read_text().split()[1] == "PASS" and "판정 PASS - 최종" in sent[-1]


def test_i179_gz1_prefix_matches_what_bench_canary_emits_and_only_for_the_newest_generation(tmp_path):
    # 실제 회전본을 잘라 judge 쪽 사유를 만든다: .1.gz 손상만 접두사에 걸리고 .2.gz 손상은 걸리지 않는다
    def damaged(name):
        d = tmp_path / name
        d.mkdir()
        raw = b"".join(f"2026-10-06T00:0{i}:00 line {i}\n".encode() for i in range(5))
        for gen in (2, 1):
            with gzip.open(d / f"server.log.{gen}.gz", "wb") as f:
                f.write(raw)
        (d / "server.log").write_text("2026-10-06T01:00:00 tail\n")
        return d
    for gen, matches in ((1, True), (2, False)):
        d = damaged(f"gen{gen}")
        gz = d / f"server.log.{gen}.gz"
        gz.write_bytes(gz.read_bytes()[:-12])               # gzip 도중처럼 꼬리(CRC·ISIZE)를 자른다
        reasons = K.read_sidecar_logs(str(d))[2]
        assert len(reasons) == 1 and f"server.log.{gen}.gz is truncated or corrupt" in reasons[0]
        assert reasons[0].startswith(sc.CANARY_GZ1_DAMAGED) is matches


GZ2_DAMAGED = GZ1_DAMAGED.replace("server.log.1.gz", "server.log.2.gz")
GZ10_DAMAGED = GZ1_DAMAGED.replace("server.log.1.gz", "server.log.10.gz")
GZ1_BAD_GZIP = GZ1_DAMAGED.replace("EOFError", "BadGzipFile")
GZ1_ORPHAN = ("log-coverage: sidecar log server.log.1.gz is truncated or corrupt (EOFError) - not read: dropped as an orphan "
              "at a generation gap its lost end may have made")
SAME_DAY = AFTER + dt.timedelta(hours=12)
NEXT_DAY = AFTER + dt.timedelta(days=1)
PREV_GZ1 = {"date": AFTER.date().isoformat(), "reasons": [GZ1_DAMAGED]}


@pytest.mark.parametrize("code,reasons,now,prev,final", [
    # (1) .1.gz 손상 사유만 + 직전 기록 없음 -> 일시 사유
    (4, [GZ1_DAMAGED], AFTER, None, False),
    (4, [GZ1_DAMAGED], AFTER, {"date": AFTER.date().isoformat(), "reasons": [GZ1_DAMAGED]}, False),   # 같은 날 재실행은 2회가 아니다
    (4, [GZ1_ORPHAN], AFTER, None, False),
    (4, [GZ1_DAMAGED, STALE_TAIL], AFTER, None, False),   # 꼬리 낡음과 섞여도 둘 다 일시 사유
    # (2) 같은 사유가 서로 다른 날 2회 연속 -> 종결
    (4, [GZ1_DAMAGED], NEXT_DAY, PREV_GZ1, True),
    (4, [GZ1_DAMAGED], AFTER + dt.timedelta(days=3), PREV_GZ1, True),
    (4, [GZ1_ORPHAN], NEXT_DAY, {"date": AFTER.date().isoformat(), "reasons": [GZ1_ORPHAN]}, True),
    (4, [GZ1_DAMAGED, STALE_TAIL], NEXT_DAY, PREV_GZ1, True),
    (4, [GZ1_BAD_GZIP], NEXT_DAY, PREV_GZ1, False),       # 사유가 달라지면 새로 센다
    (4, [GZ1_DAMAGED], NEXT_DAY, {"date": AFTER.date().isoformat(), "reasons": []}, False),
    (4, [STALE_TAIL], NEXT_DAY, PREV_GZ1, False),         # 손상 사유가 없으면 직전 기록과 무관하게 일시 사유
    # (3) 다른 사유와 섞이면 지금처럼 종결
    (4, [GZ1_DAMAGED, FRONT_LOST], AFTER, None, True),
    (4, [FRONT_LOST, GZ1_DAMAGED], NEXT_DAY, PREV_GZ1, True),
    (4, [GZ1_DAMAGED, "mode-mismatch:candidate window has {'128x3': 1}"], AFTER, None, True),
    (4, [GZ1_DAMAGED, "baseline-invalid:x"], AFTER, None, True),
    # (4) .2.gz 이상 세대 손상은 지금처럼 종결
    (4, [GZ2_DAMAGED], AFTER, None, True),
    (4, [GZ10_DAMAGED], AFTER, None, True),
    (4, [GZ1_DAMAGED, GZ2_DAMAGED], AFTER, None, True),
    # 판정 시각 전·정상 판정은 손상 기록과 무관
    (4, [GZ1_DAMAGED], BEFORE, PREV_GZ1, False),
    (3, ["not-yet"], NEXT_DAY, PREV_GZ1, False),
    (0, [], NEXT_DAY, PREV_GZ1, True), (2, ["x"], NEXT_DAY, PREV_GZ1, True),
])
def test_i179_canary_is_final_matrix(code, reasons, now, prev, final):
    assert sc.canary_is_final(code, _judge(*reasons), now, prev) is final


def test_i179_default_prev_damaged_keeps_the_existing_three_argument_call():
    assert sc.canary_is_final(4, _judge(FRONT_LOST), AFTER) is True
    assert sc.canary_is_final(4, _judge(GZ1_DAMAGED), AFTER) is False
    assert sc.canary_message(4, _judge(GZ1_DAMAGED), AFTER).startswith("[#22 카나리] 판정 INVALID")


def test_i179_before_judgment_gz1_damage_keeps_the_existing_alarm_only_the_stale_tail_is_quiet():
    # 이슈는 종결 판단만 바꾼다: 판정 시각 전 알림 분기는 그대로(꼬리 낡음만 중간 보고)
    assert sc.canary_message(4, _judge(GZ1_DAMAGED), BEFORE).startswith("🚨 [#22 카나리] 즉시 확인 필요")
    assert sc.canary_message(4, _judge(GZ1_DAMAGED, FRONT_LOST), BEFORE).startswith("🚨")
    assert sc.canary_message(4, _judge(GZ2_DAMAGED), BEFORE).startswith("🚨")


def test_i179_gz1_damage_twice_on_different_days_finalizes_and_clears_the_record(monkeypatch, tmp_path):
    state, sent = _wire(monkeypatch, tmp_path, 4, [GZ1_DAMAGED])
    sc.main(["canary-22", "--now", AFTER.isoformat()])
    assert not (state / "canary-22.done").exists() and "최종" not in sent[0]
    assert json.loads((state / "canary-22.damaged").read_text()) == {"date": "2026-10-06", "reasons": [GZ1_DAMAGED]}
    sc.main(["canary-22", "--now", SAME_DAY.isoformat()])                       # 같은 날 재실행은 종결이 아니다
    assert not (state / "canary-22.done").exists() and len(sent) == 2
    sc.main(["canary-22", "--now", NEXT_DAY.isoformat()])
    assert (state / "canary-22.done").read_text().split()[1] == "INVALID"
    assert "판정 INVALID - 최종" in sent[-1] and "수동으로 판정" in sent[-1]
    assert not (state / "canary-22.damaged").exists()
    sc.main(["canary-22", "--now", (AFTER + dt.timedelta(days=2)).isoformat()])  # 마커가 있으면 SKIP
    assert len(sent) == 3


@pytest.mark.parametrize("second", [
    pytest.param((0, []), id="healthy-pass"),
    pytest.param((3, ["not-yet"]), id="pending"),
    pytest.param((4, [STALE_TAIL]), id="stale-tail-only"),
    pytest.param((4, [GZ1_BAD_GZIP]), id="different-damage-reason"),
])
def test_i179_a_non_repeating_run_breaks_the_streak(monkeypatch, tmp_path, second):
    state, sent = _wire(monkeypatch, tmp_path, 4, [GZ1_DAMAGED])
    sc.main(["canary-22", "--now", AFTER.isoformat()])
    _set_judge(monkeypatch, second[0], *second[1])
    sc.main(["canary-22", "--now", NEXT_DAY.isoformat()])
    assert ((state / "canary-22.done").exists()) is (second[0] == 0)
    if second[0] == 4 and second[1] == [GZ1_BAD_GZIP]:                         # 새 사유로 다시 센다
        assert json.loads((state / "canary-22.damaged").read_text())["reasons"] == [GZ1_BAD_GZIP]
    else:
        assert not (state / "canary-22.damaged").exists()
    if second[0] == 4:                                                         # 그다음 날 손상이 다시 나도 1회째다
        _set_judge(monkeypatch, 4, GZ1_DAMAGED)
        sc.main(["canary-22", "--now", (AFTER + dt.timedelta(days=2)).isoformat()])
        assert not (state / "canary-22.done").exists()


@pytest.mark.parametrize("reasons", [
    pytest.param([GZ1_DAMAGED, FRONT_LOST], id="mixed-front-lost"),
    pytest.param([GZ2_DAMAGED], id="gen2"),
    pytest.param([GZ10_DAMAGED], id="gen10"),
    pytest.param([GZ1_DAMAGED, GZ2_DAMAGED], id="gen1-and-gen2"),
])
def test_i179_other_or_older_damage_finalizes_on_the_initial_pass_without_a_record(monkeypatch, tmp_path, reasons):
    state, sent = _wire(monkeypatch, tmp_path, 4, reasons)
    sc.main(["canary-22", "--now", AFTER.isoformat()])
    assert (state / "canary-22.done").read_text().split()[1] == "INVALID" and "판정 INVALID - 최종" in sent[0]
    assert not (state / "canary-22.damaged").exists()


def test_i179_dry_run_and_before_judgment_leave_no_record(monkeypatch, tmp_path):
    state, sent = _wire(monkeypatch, tmp_path, 4, [GZ1_DAMAGED])
    sc.main(["canary-22", "--now", BEFORE.isoformat()])
    sc.main(["canary-22", "--now", AFTER.isoformat(), "--dry-run"])
    assert not (state / "canary-22.damaged").exists() and not (state / "canary-22.done").exists()
    (state / "canary-22.damaged").write_text(json.dumps(PREV_GZ1))
    sc.main(["canary-22", "--now", NEXT_DAY.isoformat(), "--dry-run"])          # dry-run 은 기록도 마커도 바꾸지 않는다
    assert json.loads((state / "canary-22.damaged").read_text()) == PREV_GZ1 and not (state / "canary-22.done").exists()


@pytest.mark.parametrize("content", ["", "not json", "[1, 2]", "null"])
def test_i179_unreadable_record_counts_as_no_record(monkeypatch, tmp_path, content):
    state, sent = _wire(monkeypatch, tmp_path, 4, [GZ1_DAMAGED])
    (state / "canary-22.damaged").write_text(content)
    sc.main(["canary-22", "--now", NEXT_DAY.isoformat()])
    assert not (state / "canary-22.done").exists()
    assert json.loads((state / "canary-22.damaged").read_text())["date"] == "2026-10-07"


def test_i179_non_final_gz1_alert_says_it_is_rejudged_and_final_alert_points_at_the_damaged_file():
    first = sc.canary_message(4, _judge(GZ1_DAMAGED), AFTER)
    assert "최종" not in first and "다음 실행에서 재판정" in first and GZ1_DAMAGED in first   # 사유 문자열은 그대로
    final = sc.canary_message(4, _judge(GZ1_DAMAGED), NEXT_DAY, PREV_GZ1)
    assert "판정 INVALID - 최종" in final and "수동으로 판정" in final and "손상된 server.log.1.gz 를 확인" in final
    other = sc.canary_message(4, _judge(FRONT_LOST), AFTER)                       # 손상 사유가 없으면 안내가 붙지 않는다
    assert "server.log.1.gz" not in other and "재판정" not in other
    assert "재판정" not in sc.canary_message(4, _judge(STALE_TAIL), AFTER)


@pytest.mark.parametrize("blocker", ["dir-at-record-path", "read-only-state-dir"])
def test_i179_record_write_failure_is_logged_and_never_breaks_the_run(monkeypatch, tmp_path, capsys, blocker):
    state, sent = _wire(monkeypatch, tmp_path, 4, [GZ1_DAMAGED])
    if blocker == "dir-at-record-path":
        (state / "canary-22.damaged").mkdir()
    else:
        if os.geteuid() == 0:
            pytest.skip("root 는 쓰기 권한 검사를 우회한다")
        state.chmod(0o500)
    try:
        assert sc.main(["canary-22", "--now", AFTER.isoformat()]) == 0
    finally:
        state.chmod(0o700)
    assert len(sent) == 1 and not (state / "canary-22.done").exists()
    assert "SCHEDULED_CANARY damaged-record-error" in capsys.readouterr().out
    assert not (state / "canary-22.damaged.tmp").exists()


def test_i179_record_is_written_atomically_without_leaving_a_temp_file(monkeypatch, tmp_path):
    state, sent = _wire(monkeypatch, tmp_path, 4, [GZ1_DAMAGED])
    sc.main(["canary-22", "--now", AFTER.isoformat()])
    assert sorted(p.name for p in state.iterdir()) == ["canary-22.damaged"]
    _set_judge(monkeypatch, 0)
    sc.main(["canary-22", "--now", NEXT_DAY.isoformat()])
    assert sorted(p.name for p in state.iterdir()) == ["canary-22.done"]


# ---------------------------------------------------------------- 종결 판정 잔여 한계 (issue #119 C5·C7 - 유지)
# 판정: 둘 다 고치지 않는다(근거는 bench/README.md 카나리 절 "종결 판정 잔여 한계"). 아래 테스트는 현재 동작과 운영 절차(마커 삭제로 재판정)를
# 고정해 두어 고치려는 사람이 그 근거를 먼저 읽게 한다. 마커 의미를 바꾸면 진행 중인 카나리(판정 시각 2026-10-06 01:15 KST)의 종결 동작이 바뀐다.
# 모든 입력은 tmp_path·가짜 judge·가짜 알림이다(운영 로그 디렉터리·Discord 에 닿지 않는다).

def _set_judge(monkeypatch, code, *reasons):
    def fake_run(cmd, **kw):
        json.dump(_judge(*reasons), open(cmd[cmd.index("--out") + 1], "w"))
        return types.SimpleNamespace(returncode=code, stderr="")
    monkeypatch.setattr(sc, "run", fake_run)


def test_c5_repro_stale_tail_only_invalid_repeats_daily_without_a_marker_but_never_silently(monkeypatch, tmp_path):
    # 사이드카 로그가 영구히 멈추면 판정 시각 이후 매일 INVALID 알림이 나가고 마커는 생기지 않는다(조용한 실패가 아니라 반복 알림)
    state, sent = _wire(monkeypatch, tmp_path, 4, [STALE_TAIL])
    assert sc.canary_is_final(4, _judge(STALE_TAIL), AFTER) is False
    for day in range(4):
        assert sc.main(["canary-22", "--now", (AFTER + dt.timedelta(days=day)).isoformat()]) == 0
    assert len(sent) == 4 and all(m.startswith("[#22 카나리] 판정 INVALID") and "최종" not in m for m in sent)
    assert not (state / "canary-22.done").exists()


def test_c5_stale_tail_heals_when_the_next_request_is_logged_and_that_run_finalizes(monkeypatch, tmp_path):
    # 운영 절차: 사이드카를 되살려 요청이 로그에 찍히면 다음 점검이 판정(PASS)으로 스스로 종결한다
    state, sent = _wire(monkeypatch, tmp_path, 4, [STALE_TAIL])
    sc.main(["canary-22", "--now", AFTER.isoformat()])
    assert not (state / "canary-22.done").exists()
    _set_judge(monkeypatch, 0)
    sc.main(["canary-22", "--now", (AFTER + dt.timedelta(days=1)).isoformat()])
    assert (state / "canary-22.done").read_text().split()[1] == "PASS" and "판정 PASS - 최종" in sent[-1]


def test_c7_repro_first_non_tail_invalid_after_judgment_finalizes_even_when_the_log_dir_was_missing(monkeypatch, tmp_path):
    # `starts at None`(로그 디렉터리 일시 부재 포함)도 판정 시각 뒤 첫 실행에서 바로 종결된다 - 마커가 있으면 다음 실행은 SKIP
    missing = K.coverage_problems(None, None, 1.0, 2.0, "candidate")
    assert missing and "starts at None" in missing[0]
    state, sent = _wire(monkeypatch, tmp_path, 4, missing)
    assert sc.canary_is_final(4, _judge(*missing), AFTER) is True
    sc.main(["canary-22", "--now", AFTER.isoformat()])
    assert (state / "canary-22.done").read_text().split()[1] == "INVALID" and "판정 INVALID - 최종" in sent[0]
    _set_judge(monkeypatch, 0)                                       # 원인이 풀려도 마커가 있는 한 다시 판정하지 않는다
    sc.main(["canary-22", "--now", (AFTER + dt.timedelta(days=1)).isoformat()])
    assert len(sent) == 1


def test_c7_operator_procedure_deleting_the_marker_rejudges_on_the_next_run(monkeypatch, tmp_path):
    # 운영 절차(bench/README.md): 원인을 고친 뒤 canary-22.done 을 지우면 다음 점검이 다시 판정하고 새 결과로 마커를 쓴다
    state, sent = _wire(monkeypatch, tmp_path, 4, [FRONT_LOST])
    sc.main(["canary-22", "--now", AFTER.isoformat()])
    marker = state / "canary-22.done"
    assert marker.read_text().split()[1] == "INVALID"
    marker.unlink()
    _set_judge(monkeypatch, 2, "x:boundary")
    sc.main(["canary-22", "--now", (AFTER + dt.timedelta(days=1)).isoformat()])
    assert marker.read_text().split()[1] == "HOLD" and len(sent) == 2 and "판정 HOLD - 최종" in sent[1]


@pytest.mark.parametrize("status", ["PASS", "ROLLBACK", "HOLD", "INVALID"])
def test_existing_markers_are_never_invalidated_and_the_job_is_skipped(monkeypatch, tmp_path, status):
    """이미 있는 마커(진행 중 카나리가 쓴 것 포함)는 내용과 무관하게 SKIP 이다 - 이 이슈는 마커 의미를 바꾸지 않는다."""
    state, sent = _wire(monkeypatch, tmp_path, 1, ["x"])
    calls = []
    monkeypatch.setattr(sc, "run", lambda cmd, **kw: calls.append(cmd))
    (state / "canary-22.done").write_text(f"2026-10-06T09:00:00+09:00 {status}\n")
    assert sc.main(["canary-22", "--now", AFTER.isoformat()]) == 0
    assert calls == [] and sent == []
    assert (state / "canary-22.done").read_text().split()[1] == status


@pytest.mark.parametrize("code,reasons,now,final", [
    (4, [FRONT_LOST], BEFORE, False), (4, [STALE_TAIL], BEFORE, False), (1, ["x"], BEFORE, False),
    (3, ["not-yet"], BEFORE, False), (0, [], BEFORE, False),
])
def test_before_judgment_time_nothing_is_final_whatever_the_reason(code, reasons, now, final):
    # 판정 시각 이전 동작은 그대로다: 종결·마커 없음(알람·중간 보고만)
    assert sc.canary_is_final(code, _judge(*reasons), now) is final


# ---------------------------------------------------------------- compose stderr 마스킹 (issue #129)
# compose-go dotenv 파서는 형식이 틀린 .env 줄 원문을 오류에 싣는다. notify 는 stdout(launchd 로그)과 Discord 로 내보내므로
# 보내기 전에 마스킹한다. notify·docker 는 모두 가짜 - 실 Discord·compose·.env.local 에 닿지 않는다. 시크릿 값은 가짜다.
_SECRET = "FAKE-SECRET-xyz123"
_STDERR_CASES = [
    pytest.param(f'failed to read .env.local: line 3: unexpected character "-" in variable name "BAD-KEY={_SECRET}"',
                 ["failed to read .env.local", "line 3", "unexpected character", "in variable name"], id="bad-key-char"),
    pytest.param(f'failed to read .env.local: line 5: unterminated quoted value "{_SECRET}',
                 ["failed to read .env.local", "line 5", "unterminated quoted value"], id="unterminated-quote"),
    pytest.param('failed to read .env.local: line 7: unexpected character "\\"" in variable name "K=\\"%s"' % _SECRET,
                 ["line 7", "unexpected character", "in variable name"], id="escaped-quote-in-line"),
    pytest.param('time="x" level=warning msg="y"\nfailed to read .env.local: line 9: unterminated quoted value \'%s\n'
                 'HC_OTHER=%s' % (_SECRET, _SECRET), ["line 9", "unterminated quoted value"], id="multi-line"),
    pytest.param('time="2026-10-02T12:00:00+09:00" level=warning msg="The \\"HC_X\\" variable is not set. Defaulting to a blank string."',
                 ['level=warning', 'time="2026-10-02T12:00:00+09:00"',
                  'The \\"HC_X\\" variable is not set. Defaulting to a blank string.'], id="logfmt-warning"),
    pytest.param('level=warning msg="bad HC_OTHER=\\"abc %s\\""' % _SECRET, ['level=warning'], id="logfmt-escaped-quote-inner"),
    pytest.param('level=info msg=%s' % _SECRET, ['level=info'], id="logfmt-msg-unquoted"),
    pytest.param("level=info msg='%s'" % _SECRET, ['level=info'], id="logfmt-msg-single-quoted"),
    pytest.param('/msg=%s' % _SECRET, ['msg=<redacted>'], id="logfmt-key-after-slash"),
    pytest.param(':level=%s' % _SECRET, ['level=<redacted>'], id="logfmt-key-after-colon"),
    pytest.param('level=%s' % _SECRET, ['level=<redacted>'], id="logfmt-level-secret"),
    pytest.param('time=%s' % _SECRET, ['time=<redacted>'], id="logfmt-time-secret"),
    # Go 는 값을 \n 에서만 자르지만 Python splitlines() 는 이 문자들에서도 줄을 쪼갠다
    *[pytest.param(f'failed to read .env.local: line 5: unterminated quoted value "abc{sep}{_SECRET}',
                   ['line 5', 'unterminated quoted value'], id=f"separator-{ord(sep):04x}")
      for sep in (" ", "\x0b", "\x0c", "\x85")],
    pytest.param('service "hindsight" is not running', ['service "hindsight" is not running'], id="plain-error"),
]


def _assert_masked(text, keeps):
    assert _SECRET not in text and "SECRET-xyz" not in text, text
    for frag in keeps:
        assert frag in text, (frag, text)


def _wire_failing(monkeypatch, tmp_path, stderr, *, docker_fails=True):
    """docker compose(logs·exec)가 rc=1 + stderr 로 실패하는 job. 하위 판정 프로세스(sc.run)는 stderr 만 가진 실패로 둔다."""
    state, canary = tmp_path / "state", tmp_path / "canary"
    state.mkdir(), canary.mkdir()
    for k in ("GGG_DISCORD_BOT_TOKEN", "GGG_DISCORD_CHANNEL_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(sc, "STATE_DIR", str(state))
    monkeypatch.setattr(sc, "CANARY_DIR", str(canary))
    sent = []
    monkeypatch.setattr(sc, "notify", lambda message, dry_run: sent.append(message))
    rc = 1 if docker_fails else 0
    monkeypatch.setattr(sc, "subprocess", types.SimpleNamespace(
        PIPE=-1, run=lambda cmd, **kw: types.SimpleNamespace(returncode=rc, stderr=stderr)))
    monkeypatch.setattr(sc, "run", lambda cmd, **kw: types.SimpleNamespace(returncode=1, stdout="not json", stderr=stderr))
    return sent


@pytest.mark.parametrize("job,now,prefix", [
    pytest.param("canary-22", AFTER, "[#22 카나리] 로그 덤프 실패 rc=1", id="compose-logs"),
    pytest.param("tag-hygiene-44", dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc), "[#44 재측정] export 실패 rc=1",
                 id="compose-exec"),
])
@pytest.mark.parametrize("stderr,keeps", _STDERR_CASES)
def test_compose_stderr_is_masked_in_the_notify_body(monkeypatch, tmp_path, stderr, keeps, job, now, prefix):
    sent = _wire_failing(monkeypatch, tmp_path, stderr)
    assert sc.main([job, "--now", now.isoformat(), "--dry-run"]) == 1
    (body,) = sent
    assert body.startswith(prefix)                                     # 실패 사실(rc)은 남는다
    _assert_masked(body, keeps)


@pytest.mark.parametrize("job,now,prefix", [
    pytest.param("canary-22", AFTER, "[#22 카나리] judge 실행 실패 rc=1", id="judge"),
    pytest.param("tag-hygiene-44", dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc), "[#44 재측정] report 실패 rc=1",
                 id="report"),
])
@pytest.mark.parametrize("stderr,keeps", _STDERR_CASES)
def test_subprocess_failure_stderr_is_masked_too(monkeypatch, tmp_path, stderr, keeps, job, now, prefix):
    """compose 이후 단계(judge·report)의 stderr 도 같은 notify 로 나간다 - 같은 마스킹을 거친다."""
    sent = _wire_failing(monkeypatch, tmp_path, stderr, docker_fails=False)
    assert sc.main([job, "--now", now.isoformat(), "--dry-run"]) == 1
    (body,) = sent
    assert body.startswith(prefix)
    _assert_masked(body, keeps)


def test_masking_happens_before_the_300_char_tail(monkeypatch, tmp_path):
    head = f'failed to read .env.local: line 5: unterminated quoted value "{_SECRET}'
    sent = _wire_failing(monkeypatch, tmp_path, head + "\n" + "w" * 286)    # 원문 꼬리 300자가 시크릿 중간부터 시작
    assert "SECRET-xyz123" in (head + "\n" + "w" * 286)[-300:]
    sc.main(["canary-22", "--now", AFTER.isoformat(), "--dry-run"])
    assert "SECRET-xyz" not in sent[0]


# --- tag-hygiene-44 export needs the bank from .env.host.local (issue #186) --------------------------------
# export runs inside the hindsight container, which cannot read the host settings: job_hygiene passes --bank.
HYGIENE_DUE = sc.HYGIENE["due"] + dt.timedelta(hours=1)


def _wire_hygiene(monkeypatch, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(sc, "STATE_DIR", str(state))
    sent, calls = [], []
    monkeypatch.setattr(sc, "notify", lambda message, dry_run: sent.append(message))

    def fake_subprocess_run(cmd, **kw):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=1, stderr="stop here")      # ends the job right after export
    monkeypatch.setattr(sc, "subprocess", types.SimpleNamespace(PIPE=-1, run=fake_subprocess_run))
    return sent, calls


def test_hygiene_job_without_a_bank_fails_alone_and_never_calls_export(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("HC_PERSONAL_BANK", raising=False)
    sent, calls = _wire_hygiene(monkeypatch, tmp_path)
    assert sc.job_hygiene(HYGIENE_DUE, False) == 1
    assert calls == []
    assert "SCHEDULED_FAIL - config_missing HC_PERSONAL_BANK" in capsys.readouterr().out
    assert any("config_missing HC_PERSONAL_BANK" in m for m in sent)
    assert not list((tmp_path / "state").glob("docs-*"))


def test_hygiene_job_not_due_needs_no_bank(monkeypatch, tmp_path):
    monkeypatch.delenv("HC_PERSONAL_BANK", raising=False)
    sent, calls = _wire_hygiene(monkeypatch, tmp_path)
    assert sc.job_hygiene(sc.HYGIENE["due"] - dt.timedelta(hours=1), False) == 0
    assert calls == [] and sent == []


@pytest.mark.parametrize("bank", ["personal-demo", "bank-x"])
def test_hygiene_job_passes_the_bank_to_the_container_export(monkeypatch, tmp_path, bank):
    monkeypatch.setenv("HC_PERSONAL_BANK", bank)
    sent, calls = _wire_hygiene(monkeypatch, tmp_path)
    assert sc.job_hygiene(HYGIENE_DUE, False) == 1                           # the fake export fails
    (cmd,) = calls
    assert cmd[cmd.index("export"):] == ["export", "--bank", bank]
    assert not any("config_missing" in m for m in sent)


# ---------------------------------------------------------------- 네트워크 대기·미전송 재알림 (issue #266)

@pytest.fixture(autouse=True)
def _network_up_and_no_credentials(monkeypatch):
    """기본은 DNS 성공 스텁·자격증명 없음.
    다른 테스트의 main() 이 실제 DNS·Discord 에 닿지 않는다.
    OUTBOX 는 테스트 뒤 되돌린다."""
    monkeypatch.setattr(sc, "_resolve", lambda host: None)
    monkeypatch.setattr(sc, "OUTBOX", None)
    monkeypatch.delenv("GGG_DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("GGG_DISCORD_CHANNEL_ID", raising=False)


def _dns_down(monkeypatch, down=("pypi.org", "discord.com"), calls=None):
    def resolve(host):
        if calls is not None:
            calls.append(host)
        if host in down:
            raise sc.socket.gaierror(8, "nodename nor servname provided, or not known")
    monkeypatch.setattr(sc, "_resolve", resolve)


class _Clock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def test_i266_wait_for_network_ready_at_once_never_sleeps():
    clock = _Clock()
    assert sc.wait_for_network(timeout=600, poll=10, sleep=clock.sleep, clock=clock) == []
    assert clock.slept == []


def test_i266_wait_for_network_retries_until_dns_comes_up(monkeypatch):
    clock = _Clock()
    tries = []

    def resolve(host):
        tries.append(host)
        if clock.t < 25:
            raise sc.socket.gaierror(8, "no dns")
    monkeypatch.setattr(sc, "_resolve", resolve)
    assert sc.wait_for_network(timeout=600, poll=10, sleep=clock.sleep, clock=clock) == []
    assert clock.slept == [10, 10, 10] and set(tries) == set(sc.NET_HOSTS)


def test_i266_wait_for_network_gives_up_at_the_deadline_and_names_the_missing_hosts(monkeypatch):
    clock = _Clock()
    _dns_down(monkeypatch, down=("pypi.org",))
    assert sc.wait_for_network(timeout=25, poll=10, sleep=clock.sleep, clock=clock) == ["pypi.org"]
    assert clock.slept == [10, 10, 5] and clock.t == 25          # 마감을 넘겨 자지 않는다


def test_i266_default_hosts_cover_report_and_notify():
    assert set(sc.NET_HOSTS) == {"pypi.org", "discord.com"}


def _hygiene_env(monkeypatch, tmp_path):
    state = tmp_path / "state"
    monkeypatch.setattr(sc, "STATE_DIR", str(state))
    started = []
    monkeypatch.setattr(sc, "subprocess", types.SimpleNamespace(
        PIPE=-1, run=lambda *a, **kw: started.append(a[0]) or types.SimpleNamespace(returncode=1, stderr="")))
    monkeypatch.setattr(sc, "run", lambda cmd, **kw: started.append(cmd))
    monkeypatch.setenv("HC_PERSONAL_BANK", "bank-x")
    return state, started


def test_i266_repro_dns_down_starts_no_export_or_report_logs_and_exits_nonzero(monkeypatch, tmp_path, capsys):
    """10-15 DarkWake 재현: DNS 가 끝내 없으면 export(docker)·report(uv) 를 시작하지 않는다."""
    state, started = _hygiene_env(monkeypatch, tmp_path)
    _dns_down(monkeypatch)
    sent = []
    monkeypatch.setattr(sc, "notify", lambda message, dry_run: sent.append(message))
    rc = sc.main(["tag-hygiene-44", "canary-22", "--now", "2026-10-15T09:02:01+09:00", "--net-wait", "0"])
    assert rc == sc.EX_TEMPFAIL and rc != 0
    assert started == [] and sent == []
    out = capsys.readouterr().out
    assert "SCHEDULED_NET_UNAVAILABLE dns=pypi.org,discord.com" in out and "jobs=tag-hygiene-44,canary-22" in out
    assert not (state / "tag-hygiene-44.done").exists()


def test_i266_dns_down_with_credentials_queues_a_notice_for_the_next_run(monkeypatch, tmp_path):
    state, _ = _hygiene_env(monkeypatch, tmp_path)
    _dns_down(monkeypatch)
    monkeypatch.setenv("GGG_DISCORD_BOT_TOKEN", "fake-not-a-token")
    monkeypatch.setenv("GGG_DISCORD_CHANNEL_ID", "123")
    assert sc.main(["tag-hygiene-44", "--net-wait", "0"]) == sc.EX_TEMPFAIL
    raw = (state / sc.OUTBOX_NAME).read_text()
    queued = [json.loads(ln) for ln in raw.splitlines()]
    assert len(queued) == 1 and "네트워크 없음" in queued[0]["message"] and "tag-hygiene-44" in queued[0]["message"]
    assert "fake-not-a-token" not in raw


def test_i266_all_jobs_done_and_empty_outbox_never_touch_the_network(monkeypatch, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(sc, "STATE_DIR", str(state))
    for job in sc.JOBS:
        (state / f"{job}.done").write_text("x PASS\n")
    calls = []
    _dns_down(monkeypatch, calls=calls)
    assert sc.main(["--net-wait", "0"]) == 0 and calls == []


def test_i266_main_passes_net_wait_to_the_wait(monkeypatch, tmp_path):
    monkeypatch.setattr(sc, "STATE_DIR", str(tmp_path / "state"))
    seen = []
    monkeypatch.setattr(sc, "wait_for_network", lambda timeout: seen.append(timeout) or ["pypi.org"])
    assert sc.main(["tag-hygiene-44", "--net-wait", "42"]) == sc.EX_TEMPFAIL
    assert seen == [42.0]
    seen.clear()
    sc.main(["tag-hygiene-44"])
    assert seen == [sc.NET_WAIT_SECONDS]


class _Discord:
    """urlopen 스텁: 보낸 본문을 모으고, up=False 면 DNS 오류를 낸다."""
    def __init__(self, up=True):
        self.up, self.sent = up, []

    def __call__(self, req, timeout=None):
        if not self.up:
            raise sc.urllib.error.URLError(OSError(8, "nodename nor servname provided, or not known"))
        self.sent.append(json.loads(req.data)["content"])
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _discord_env(monkeypatch, tmp_path, up):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    monkeypatch.setattr(sc, "STATE_DIR", str(state))
    monkeypatch.setenv("GGG_DISCORD_BOT_TOKEN", "fake-not-a-token")
    monkeypatch.setenv("GGG_DISCORD_CHANNEL_ID", "123")
    discord = _Discord(up)
    monkeypatch.setattr(sc.urllib.request, "urlopen", discord)
    return state, discord


def _hygiene_report_fails(monkeypatch):
    monkeypatch.setenv("HC_PERSONAL_BANK", "bank-x")
    monkeypatch.setattr(sc, "subprocess", types.SimpleNamespace(
        PIPE=-1, run=lambda *a, **kw: types.SimpleNamespace(returncode=0, stderr="")))
    monkeypatch.setattr(sc, "run", lambda cmd, **kw: types.SimpleNamespace(
        returncode=2, stdout="", stderr="Failed to fetch: `https://pypi.org/simple/httpx/`"))


def test_i266_repro_failed_notify_is_queued_and_resent_first_next_run_then_cleared(monkeypatch, tmp_path, capsys):
    """10-15 재현: report 실패 알림이 Discord DNS 오류로 못 나가면 큐에 남는다.
    다음 실행 첫머리에 재전송하고, 성공하면 지운다."""
    state, discord = _discord_env(monkeypatch, tmp_path, up=False)
    _hygiene_report_fails(monkeypatch)
    assert sc.main(["tag-hygiene-44", "--now", "2026-10-15T09:02:01+09:00", "--net-wait", "0"]) == 1
    outbox = state / sc.OUTBOX_NAME
    queued = [json.loads(ln) for ln in outbox.read_text().splitlines()]
    assert len(queued) == 1 and queued[0]["message"].startswith("[#44 재측정] report 실패 rc=2")
    assert "SCHEDULED_NOTIFY_FAILED discord" in capsys.readouterr().out

    discord.up = True
    assert sc.main(["tag-hygiene-44", "--now", "2026-10-16T09:00:00+09:00", "--net-wait", "0"]) == 1
    assert discord.sent[0].startswith("[재전송 - 원래 ") and "[#44 재측정] report 실패 rc=2" in discord.sent[0]
    assert discord.sent[1].startswith("[#44 재측정] report 실패")     # 재전송이 이번 실행 알림보다 먼저다
    assert len(discord.sent) == 2 and not outbox.exists()
    assert "fake-not-a-token" not in capsys.readouterr().out


def test_i266_resend_keeps_only_what_discord_still_refuses(monkeypatch, tmp_path):
    state, discord = _discord_env(monkeypatch, tmp_path, up=True)
    outbox = state / sc.OUTBOX_NAME
    outbox.write_text("".join(json.dumps({"queued_at": f"t{i}", "message": f"m{i}"}) + "\n" for i in range(3))
                      + "not json\n")
    monkeypatch.setattr(sc, "OUTBOX", str(outbox))
    calls = []

    def flaky(req, timeout=None):
        calls.append(1)
        if len(calls) == 2:
            raise sc.urllib.error.URLError("down")
        return discord(req, timeout)
    monkeypatch.setattr(sc.urllib.request, "urlopen", flaky)
    assert sc.flush_outbox() == 1
    assert [json.loads(ln)["message"] for ln in outbox.read_text().splitlines()] == ["m1"]
    assert discord.sent == ["[재전송 - 원래 t0]\nm0", "[재전송 - 원래 t2]\nm2"]
    assert not (state / f"{sc.OUTBOX_NAME}.tmp").exists()


def test_i266_outbox_only_with_credentials_and_never_on_dry_run(monkeypatch, tmp_path):
    state, _ = _discord_env(monkeypatch, tmp_path, up=False)
    _hygiene_report_fails(monkeypatch)
    sc.main(["tag-hygiene-44", "--dry-run", "--net-wait", "0"])
    assert not (state / sc.OUTBOX_NAME).exists()
    monkeypatch.delenv("GGG_DISCORD_BOT_TOKEN")
    sc.main(["tag-hygiene-44", "--net-wait", "0"])                    # 자격증명 없음 = stdout 전용, 미전송이 아니다
    assert not (state / sc.OUTBOX_NAME).exists()


def test_i266_queued_messages_without_credentials_stay_for_later(monkeypatch, tmp_path):
    outbox = tmp_path / sc.OUTBOX_NAME
    outbox.write_text(json.dumps({"queued_at": "t0", "message": "m0"}) + "\n")
    monkeypatch.setattr(sc, "OUTBOX", str(outbox))
    assert sc.flush_outbox() == 1 and outbox.exists()


def test_i266_pending_outbox_alone_makes_a_done_run_wait_and_resend(monkeypatch, tmp_path):
    state, discord = _discord_env(monkeypatch, tmp_path, up=True)
    for job in sc.JOBS:
        (state / f"{job}.done").write_text("x PASS\n")
    (state / sc.OUTBOX_NAME).write_text(json.dumps({"queued_at": "t0", "message": "m0"}) + "\n")
    assert sc.main(["--net-wait", "0"]) == 0
    assert discord.sent == ["[재전송 - 원래 t0]\nm0"] and not (state / sc.OUTBOX_NAME).exists()


def test_i266_notify_outside_scheduled_runs_never_queues(monkeypatch, tmp_path, capsys):
    """item_roles_observe 가 빌려 쓰는 notify 는 OUTBOX 가 꺼져 있어 큐를 만들지 않는다.
    출력 모양(메시지 + 로그 한 줄)도 그대로다."""
    state, _ = _discord_env(monkeypatch, tmp_path, up=False)
    assert sc.OUTBOX is None
    assert sc.notify("hello", False) is False
    assert len(capsys.readouterr().out.splitlines()) == 2
    assert list(state.iterdir()) == []
