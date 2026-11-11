"""scripts/trigger_drafts.py: 트리거 초안 처리 기록 CLI (issue #276). 임시 디렉터리 사본만 쓴다."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "trigger_drafts.py"


def draft(draft_id, state="ready-for-review", preview="미리보기", created_at="2026-10-26T01:00:00Z"):
    return {"draft_id": draft_id, "created_at": created_at, "state": state,
            "evidence": {"new_content_preview": preview}}


@pytest.fixture
def files(tmp_path, monkeypatch):
    """5줄 합성 pending 파일 + 빈 decisions 파일. 환경은 두 파일만 가리키게 고정한다."""
    pending = tmp_path / "pending.jsonl"
    decisions = tmp_path / "decisions.jsonl"
    pending.write_text("".join(json.dumps(draft(f"id{n}")) + "\n" for n in range(1, 6)), encoding="utf-8")
    decisions.write_text("", encoding="utf-8")
    monkeypatch.setenv("HC_PENDING_TRIGGERS", str(pending))
    monkeypatch.setenv("HC_TRIGGER_DECISIONS", str(decisions))
    monkeypatch.delenv("HC_GATEWAY_AUDIT_LOG", raising=False)
    return pending, decisions


def run(*args, env=None):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                          env={**os.environ, **(env or {})}, timeout=30)


def listed(*args, env=None):
    out = run("list", *args, env=env)
    assert out.returncode == 0, out.stderr
    return out.stdout.splitlines()


def decision_rows(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def test_list_prints_one_line_per_pending_draft_without_header(files):
    lines = listed()
    assert len(lines) == 5
    assert lines[0] == "id1 ready-for-review 2026-10-26T01:00:00Z 미리보기"


def test_resolve_removes_drafts_from_list_and_keeps_all_view(files):
    pending, decisions = files
    before = pending.read_bytes()
    out = run("resolve", "id1", "id2", "--decision", "rejected", "--reason", "키워드가 넓다")
    assert out.returncode == 0, out.stderr
    assert len(listed()) == 3
    assert not any(line.startswith(("id1 ", "id2 ")) for line in listed())
    all_lines = listed("--all")
    assert len(all_lines) == 5
    assert all_lines[0] == "id1 ready-for-review 2026-10-26T01:00:00Z rejected 미리보기"
    assert all_lines[2] == "id3 ready-for-review 2026-10-26T01:00:00Z - 미리보기"
    rows = decision_rows(decisions)
    assert [(r["draft_id"], r["decision"], r["reason"]) for r in rows] == [
        ("id1", "rejected", "키워드가 넓다"), ("id2", "rejected", "키워드가 넓다")]
    assert all(r["ts"].endswith("Z") for r in rows)
    assert pending.read_bytes() == before                       # 대기 파일은 절대 다시 쓰지 않는다


def test_the_last_decision_for_a_draft_wins(files):
    _, decisions = files
    assert run("resolve", "id1", "--decision", "approved", "--reason", "등록").returncode == 0
    assert run("resolve", "id1", "--decision", "rejected", "--reason", "번복").returncode == 0
    assert len(listed()) == 4
    assert listed("--all")[0].split()[3] == "rejected"
    assert [r["decision"] for r in decision_rows(decisions)] == ["approved", "rejected"]


@pytest.mark.parametrize("ids", [["nope"], ["id1", "nope"], ["nope", "id1", "id2"]])
def test_unknown_id_exits_2_and_writes_nothing(files, ids):
    pending, decisions = files
    before = pending.read_bytes()
    out = run("resolve", *ids, "--decision", "approved", "--reason", "x")
    assert out.returncode == 2
    assert "nope" in out.stderr
    assert decisions.read_text(encoding="utf-8") == ""          # 존재하는 id 도 기록하지 않는다
    assert pending.read_bytes() == before
    assert len(listed()) == 5


@pytest.mark.parametrize("given", ["id3", "draft-id3"])
def test_resolve_takes_bare_and_prefixed_ids_and_records_the_bare_one(files, given):
    _, decisions = files
    assert run("resolve", given, "--decision", "approved", "--reason", "등록").returncode == 0
    assert [r["draft_id"] for r in decision_rows(decisions)] == ["id3"]
    assert len(listed()) == 4


def test_resolving_the_same_id_twice_in_one_call_writes_it_once(files):
    _, decisions = files
    assert run("resolve", "id1", "draft-id1", "--decision", "approved", "--reason", "x").returncode == 0
    assert len(decision_rows(decisions)) == 1


def test_a_missing_pending_file_lists_nothing_and_resolve_exits_2(tmp_path):
    env = {"HC_PENDING_TRIGGERS": str(tmp_path / "none.jsonl"),
           "HC_TRIGGER_DECISIONS": str(tmp_path / "decisions.jsonl")}
    assert listed(env=env) == []
    out = run("resolve", "id1", "--decision", "approved", "--reason", "x", env=env)
    assert out.returncode == 2
    assert not (tmp_path / "decisions.jsonl").exists()


def test_a_missing_decisions_file_means_everything_is_pending_and_resolve_creates_it(files):
    _, decisions = files
    decisions.unlink()
    assert len(listed()) == 5
    assert run("resolve", "id1", "--decision", "approved", "--reason", "x").returncode == 0
    assert len(listed()) == 4


def test_lines_without_id_print_a_dash_and_cannot_be_resolved(files):
    pending, decisions = files
    with open(pending, "a", encoding="utf-8") as fh:
        fh.write('{"state": "needs-evidence"}\n')                    # draft_id 없음
        fh.write('{"draft_id": "noState"}\n')                       # state 없음
        fh.write('{"draft_id": "id9", "state": "needs-ev')          # 쓰는 중인 마지막 줄
    lines = listed()
    assert len(lines) == 8
    assert [line.split()[0] for line in lines[5:]] == ["-", "noState", "-"]   # id 가 있으면 state 가 없어도 보인다
    assert lines[6].split()[:3] == ["noState", "-", "-"]
    assert run("resolve", "noState", "--decision", "rejected", "--reason", "x").returncode == 2
    assert run("resolve", "id9", "--decision", "rejected", "--reason", "x").returncode == 2
    assert len(listed("--all")) == 8


def test_preview_is_cut_to_60_chars_and_stays_on_one_line(files):
    pending, _ = files
    long_preview = "가" * 59 + "\n" + "나" * 40
    pending.write_text(json.dumps(draft("long1", preview=long_preview)) + "\n", encoding="utf-8")
    (line,) = listed()
    preview = line.split(" ", 3)[3]
    assert len(preview) == 60 and preview.startswith("가" * 59 + " ")


def test_resolve_after_a_decisions_file_without_trailing_newline_keeps_both_rows(files):
    _, decisions = files
    decisions.write_text(json.dumps({"ts": "t", "draft_id": "id1", "decision": "rejected", "reason": "r"}),
                         encoding="utf-8")                           # 끝 개행 없음
    assert run("resolve", "id2", "--decision", "approved", "--reason", "x").returncode == 0
    assert [r["draft_id"] for r in decision_rows(decisions)] == ["id1", "id2"]


def test_malformed_decision_lines_are_ignored(files):
    _, decisions = files
    decisions.write_text('garbage\n{"draft_id": "id1", "decision": "maybe"}\n'
                         '{"draft_id": "id2", "decision": "rejected"}\n[1]\n', encoding="utf-8")
    assert len(listed()) == 4                                        # id2 만 처리됨


@pytest.mark.parametrize("args", [
    ["resolve", "id1", "--decision", "maybe", "--reason", "x"],
    ["resolve", "id1", "--decision", "approved"],
    ["resolve", "--decision", "approved", "--reason", "x"],
])
def test_bad_resolve_arguments_are_rejected_and_write_nothing(files, args):
    _, decisions = files
    assert run(*args).returncode == 2
    assert decisions.read_text(encoding="utf-8") == ""


def test_default_paths_follow_the_audit_log_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("HC_GATEWAY_AUDIT_LOG", str(tmp_path / "gateway.log"))
    monkeypatch.delenv("HC_PENDING_TRIGGERS", raising=False)
    monkeypatch.delenv("HC_TRIGGER_DECISIONS", raising=False)
    (tmp_path / "pending-triggers.jsonl").write_text(json.dumps(draft("id1")) + "\n", encoding="utf-8")
    assert run("resolve", "id1", "--decision", "rejected", "--reason", "x").returncode == 0
    assert [r["draft_id"] for r in decision_rows(tmp_path / "trigger-decisions.jsonl")] == ["id1"]
    assert listed() == []


def test_env_overrides_win_over_the_audit_log_directory(tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("HC_GATEWAY_AUDIT_LOG", str(tmp_path / "gateway.log"))
    monkeypatch.setenv("HC_PENDING_TRIGGERS", str(other / "p.jsonl"))
    monkeypatch.setenv("HC_TRIGGER_DECISIONS", str(other / "d.jsonl"))
    (tmp_path / "pending-triggers.jsonl").write_text(json.dumps(draft("zz")) + "\n", encoding="utf-8")
    (other / "p.jsonl").write_text(json.dumps(draft("id1")) + "\n", encoding="utf-8")
    assert [line.split()[0] for line in listed()] == ["id1"]
    assert run("resolve", "id1", "--decision", "approved", "--reason", "x").returncode == 0
    assert (other / "d.jsonl").exists() and not (tmp_path / "trigger-decisions.jsonl").exists()


def test_the_audit_count_agrees_with_the_cli_list(files, monkeypatch):
    """model_audit.py keeps its own copy of the reading rules (it must run alone); this pins the two."""
    import importlib.util
    pending, decisions = files
    with open(pending, "a", encoding="utf-8") as fh:
        fh.write('{"state": "needs-evidence"}\n{"draft_id": "noState"}\nnot json\n[1]\n'
                 '{"draft_id": "draft-x1", "state": "weird"}\n')
    decisions.write_text('{"draft_id": "draft-id1", "decision": "rejected"}\n'
                         '{"draft_id": "id2", "decision": "approved"}\n'
                         '{"draft_id": "id2", "decision": "maybe"}\n{"draft_id": "x1", "decision": "approved"}\n',
                         encoding="utf-8")
    spec = importlib.util.spec_from_file_location("model_audit_agreement_under_test",
                                                  SCRIPT.with_name("model_audit.py"))
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
    monkeypatch.setattr(audit, "PENDING_TRIGGERS", str(pending))
    monkeypatch.setattr(audit, "TRIGGER_DECISIONS", str(decisions))
    counts = audit._pending_drafts()
    assert counts["total"] == len(listed()) == 3 + 4                 # id3..id5 + 기타 4줄 (x1 은 처리됨)
    assert counts["other"] == 4


def test_an_unwritable_decisions_path_fails_cleanly_without_a_traceback(files, tmp_path):
    env = {"HC_TRIGGER_DECISIONS": str(tmp_path / "no-such-dir" / "d.jsonl")}
    out = run("resolve", "id1", "--decision", "approved", "--reason", "x", env=env)
    assert out.returncode == 1
    assert "Traceback" not in out.stderr and "처리 기록을 쓰지 못했다" in out.stderr


def test_deeply_nested_garbage_lines_are_other_lines_not_a_crash(files):
    pending, decisions = files
    with open(pending, "a", encoding="utf-8") as fh:
        fh.write("[" * 100000 + "\n")                                # json.loads -> RecursionError
    decisions.write_text("[" * 100000 + '\n{"draft_id": "id1", "decision": "rejected"}\n', encoding="utf-8")
    lines = listed()
    assert len(lines) == 5                                          # id1 처리됨 + 쓰레기 줄 1개
    assert lines[-1].split()[0] == "-"
    assert run("resolve", "id2", "--decision", "approved", "--reason", "x").returncode == 0


def test_control_characters_in_every_listed_field_become_one_space_each(files):
    pending, _ = files
    row = {"draft_id": "idx", "state": "ready\x1bfor", "created_at": "2026\x00-10",
           "evidence": {"new_content_preview": "a\x1b[31mred\x07\x7fend"}}
    pending.write_text(json.dumps(row) + "\n", encoding="utf-8")
    out = run("list")
    assert out.returncode == 0
    (line,) = out.stdout.splitlines()
    assert not any(ord(c) < 0x20 or ord(c) == 0x7f for c in line)
    assert line == "idx ready for 2026 -10 a [31mred end"
    assert len(run("list", "--all").stdout.splitlines()) == 1


@pytest.mark.parametrize("reason", ["", "   ", "\t\n"])
def test_a_blank_reason_exits_2_and_writes_nothing(files, reason):
    _, decisions = files
    out = run("resolve", "id1", "--decision", "rejected", "--reason", reason)
    assert out.returncode == 2
    assert out.stderr.strip() and "Traceback" not in out.stderr
    assert decisions.read_text(encoding="utf-8") == ""
    assert len(listed()) == 5
