"""Offline weekly injection telemetry tests; no gateway, token, or notifier is used."""
import datetime as dt
import importlib.util
import json
from pathlib import Path

import pytest


SINCE = dt.datetime(2026, 9, 20, tzinfo=dt.timezone.utc)
NOW = "2026-09-21T00:00:00Z"


@pytest.fixture
def audit_module(monkeypatch, tmp_path):
    path = Path(__file__).resolve().parents[1] / "scripts" / "model_audit.py"
    spec = importlib.util.spec_from_file_location("injection_audit_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "GATEWAY_AUDIT_LOG", str(tmp_path / "gateway.log"))
    monkeypatch.setattr(module, "PENDING_TRIGGERS", str(tmp_path / "pending-triggers.jsonl"))
    monkeypatch.setattr(module, "TRIGGER_DECISIONS", str(tmp_path / "trigger-decisions.jsonl"))
    return module


def write_events(module, events):
    Path(module.GATEWAY_AUDIT_LOG).write_text(
        "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")


def event(decision, reason="", **extra):
    return {"ts": NOW, "resolved_bank": "personal-demo", "decision": decision,
            "reason": reason, **extra}


def test_empty_recall_is_a_match_with_a_separate_empty_counter(audit_module):
    write_events(audit_module, [
        event("context-trigger", "match:served"),
        event("context-trigger", "match-empty:empty"),
        event("context-trigger", "recall-failed:failed"),
        event("context-trigger", "miss"),
        event("context-trigger", "future-unknown-reason"),
    ])
    signal = audit_module.injection_signal(SINCE)
    assert signal["context_trigger_calls"] == 4
    assert signal["matched"] == 3 and signal["miss"] == 1
    assert signal["match_rate"] == 0.75
    assert signal["empty"] == 1 and signal["recall_failed"] == 1
    assert signal["served_by_trigger"] == [{"id": "served", "fires": 1}]
    assert signal["empty_by_trigger"] == [{"id": "empty", "fires": 1}]


def test_correction_associations_count_distinct_ids_per_event(audit_module):
    first = {"trigger_ids": ["t-a", "t-a", "t-b"],
             "memory_ids": ["m-a", "m-a", "m-b"], "transport": "mcp",
             "evidence_method": "recent-served-id+reranker"}
    second = {"trigger_ids": ["t-a"], "memory_ids": ["m-a"], "transport": "rest",
              "evidence_method": "recent-served-id+reranker"}
    write_events(audit_module, [
        event("injection-correction", injection_correction=first),
        event("injection-correction", injection_correction=second),
        # A generic correction draft does not prove association with an injected memory.
        event("recurrence", "draft:correction-after-recall"),
    ])
    signal = audit_module.injection_signal(SINCE)
    assert signal["injection_corrections"] == {
        "events": 2,
        "unique_memories": 2,
        "by_trigger": [{"id": "t-a", "events": 2}, {"id": "t-b", "events": 1}],
        "by_memory": [{"id": "m-a", "events": 2}, {"id": "m-b", "events": 1}],
    }
    assert signal["recurrence_drafts"] == {"correction-after-recall": 1}


def test_other_banks_old_and_malformed_events_are_excluded(audit_module):
    evidence = {"trigger_ids": ["t-a"], "memory_ids": ["m-a"]}
    write_events(audit_module, [
        event("context-trigger", "match:other", resolved_bank="personal-other"),
        event("injection-correction", injection_correction=evidence,
              resolved_bank="personal-other"),
        event("injection-correction", injection_correction=evidence,
              ts="2026-09-19T00:00:00Z"),
        event("injection-correction", injection_correction=None),
        event("injection-correction", injection_correction={}),
        event("injection-correction", injection_correction={
            "trigger_ids": "t-a", "memory_ids": ["m-a"]}),
        event("injection-correction", injection_correction={
            "trigger_ids": ["t-a"], "memory_ids": [None, "", 17]}),
        event("context-trigger", "miss"),
    ])
    signal = audit_module.injection_signal(SINCE)
    assert signal["context_trigger_calls"] == 1
    assert signal["matched"] == 0 and signal["miss"] == 1
    assert signal["injection_corrections"] == {
        "events": 0, "unique_memories": 0, "by_trigger": [], "by_memory": []}


def test_absent_log_stays_unavailable(audit_module):
    assert audit_module.injection_signal(SINCE) == {
        "available": False, "since": "2026-09-20T00:00:00Z"}


# --- #12 review P3 (2026-09-23): injection metrics reach the weekly notification ------------

def test_injection_summary_and_warning_lines_for_notification(audit_module):
    write_events(audit_module, [
        event("context-trigger", "match:demo-dash-login"),
        event("context-trigger", "match-empty:demo-dash-login"),
        event("context-trigger", "recall-failed:demo-dash-login"),
        event("context-trigger", "miss"),
        event("contradiction-warn", "precedent:22aebc9e"),
        event("recurrence", "draft:gotcha-recurrence"),
        event("injection-correction", injection_correction={
            "trigger_ids": ["demo-dash-login"], "memory_ids": ["85d42076-aaaa"]}),
    ])
    lines = audit_module.injection_lines(audit_module.injection_signal(SINCE))
    assert lines[0].startswith("길목 주입 7일: 호출 4 · 매칭 3 · 미스 1 · 빈 검색 1 · recall 실패 1")
    assert "모순 경고 1/1" in lines[0] and "초안 생성 1" in lines[0] and "주입 후 정정 1" in lines[0]
    body = "\n".join(lines)
    assert "`85d42076`" in body and "`demo-dash-login`" in body and "recall 실패 1건" in body
    assert "빈 검색 트리거 `demo-dash-login` 1회" in body
    assert "트리거 초안" not in body                     # created drafts != pending ones
    write_pending(audit_module, [("a1", "ready-for-review"), ("a2", "needs-evidence")])
    body = "\n".join(audit_module.injection_lines(audit_module.injection_signal(SINCE)))
    assert "- 트리거 초안 미처리 2건(ready-for-review 1 / needs-evidence 1) - scripts/trigger_drafts.py list" in body


# --- issue #276: 처리 기록에 있는 초안은 감사 미처리 집계에서 빠진다 ---------------------------

def write_pending(module, drafts, extra_lines=()):
    rows = [json.dumps({"draft_id": i, "state": state, "created_at": NOW}) for i, state in drafts]
    Path(module.PENDING_TRIGGERS).write_text("\n".join([*rows, *extra_lines]) + "\n", encoding="utf-8")


def write_decisions(module, rows, raw_lines=()):
    lines = [json.dumps({"ts": NOW, "draft_id": i, "decision": d, "reason": "r"}) for i, d in rows]
    Path(module.TRIGGER_DECISIONS).write_text("\n".join([*lines, *raw_lines]) + "\n", encoding="utf-8")


def pending_line(module):
    """주입이 활동 중일 때 주간 보고서의 초안 줄(없으면 None)."""
    write_events(module, [event("context-trigger", "match:served")])
    lines = module.injection_lines(module.injection_signal(SINCE))
    found = [ln for ln in lines if "트리거 초안" in ln]
    assert len(found) <= 1
    return found[0] if found else None


FIVE = [("d1", "ready-for-review"), ("d2", "ready-for-review"), ("d3", "needs-evidence"),
        ("d4", "needs-evidence"), ("d5", "needs-evidence")]
TAIL = " - scripts/trigger_drafts.py list"


def test_the_constant_sits_next_to_the_pending_path_with_the_same_rule(audit_module, monkeypatch, tmp_path):
    path = Path(__file__).resolve().parents[1] / "scripts" / "model_audit.py"
    monkeypatch.setenv("HC_GATEWAY_AUDIT_LOG", str(tmp_path / "x" / "gateway.log"))
    spec = importlib.util.spec_from_file_location("injection_audit_paths_under_test", path)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    assert fresh.PENDING_TRIGGERS == str(tmp_path / "x" / "pending-triggers.jsonl")
    assert fresh.TRIGGER_DECISIONS == str(tmp_path / "x" / "trigger-decisions.jsonl")


def test_without_a_decisions_file_every_draft_is_pending_as_before(audit_module):
    write_pending(audit_module, FIVE)
    assert not Path(audit_module.TRIGGER_DECISIONS).exists()
    assert pending_line(audit_module) == (
        "- 트리거 초안 미처리 5건(ready-for-review 2 / needs-evidence 3)" + TAIL)


@pytest.mark.parametrize("rows,line", [
    ([("d1", "approved")], "4건(ready-for-review 1 / needs-evidence 3)"),
    ([("d1", "rejected"), ("d3", "approved")], "3건(ready-for-review 1 / needs-evidence 2)"),
    ([("d1", "rejected"), ("d1", "approved")], "4건(ready-for-review 1 / needs-evidence 3)"),   # 같은 id: 마지막
    ([("d1", "approved"), ("zz", "rejected")], "4건(ready-for-review 1 / needs-evidence 3)"),   # 모르는 id 무시
    ([(i, "rejected") for i, _ in FIVE[:4]], "1건(ready-for-review 0 / needs-evidence 1)"),
])
def test_processed_drafts_leave_the_count_split_by_state(audit_module, rows, line):
    write_pending(audit_module, FIVE)
    write_decisions(audit_module, rows)
    assert pending_line(audit_module) == "- 트리거 초안 미처리 " + line + TAIL


def test_resolving_every_draft_drops_the_line(audit_module):
    write_pending(audit_module, FIVE)
    write_decisions(audit_module, [(i, "rejected") for i, _ in FIVE])
    assert pending_line(audit_module) is None
    assert "승인 대기" not in "\n".join(audit_module.injection_lines(audit_module.injection_signal(SINCE)))


def test_an_empty_pending_file_or_none_adds_no_line(audit_module):
    assert pending_line(audit_module) is None                      # 파일 없음
    Path(audit_module.PENDING_TRIGGERS).write_text("\n  \n", encoding="utf-8")
    assert pending_line(audit_module) is None


def test_the_last_decision_wins_even_after_a_garbled_decisions_line(audit_module):
    write_pending(audit_module, FIVE)
    write_decisions(audit_module, [("d1", "rejected"), ("d2", "approved")],
                    raw_lines=['{"draft_id": "d1", "decision": "maybe"}', "garbage", '{"draft_id": "d3"'])
    assert pending_line(audit_module).startswith("- 트리거 초안 미처리 3건(ready-for-review 0 / needs-evidence 3)")


def test_draft_prefixed_ids_in_the_decisions_file_match_too(audit_module):
    write_pending(audit_module, FIVE)
    write_decisions(audit_module, [("draft-d1", "rejected")])
    assert pending_line(audit_module).startswith("- 트리거 초안 미처리 4건(")


@pytest.mark.parametrize("junk", [
    '{"state": "needs-evidence"}',            # draft_id 없음
    '{"draft_id": "nostate"}',                # state 없음
    '{"draft_id": "d9", "state": "needs-ev',  # 쓰는 중인 마지막 줄
    "not json",
    "[1, 2]",
    pytest.param("[" * 100000, id="deeply-nested"),   # json.loads -> RecursionError
])
def test_unclassifiable_lines_are_counted_as_other_and_never_resolved(audit_module, junk):
    write_pending(audit_module, FIVE[:2], extra_lines=[junk])
    assert pending_line(audit_module) == (
        "- 트리거 초안 미처리 3건(ready-for-review 2 / needs-evidence 0 / 기타 1)" + TAIL)
    write_decisions(audit_module, [("d1", "rejected"), ("d2", "rejected"), ("nostate", "rejected"), ("d9", "rejected")])
    assert pending_line(audit_module) == (
        "- 트리거 초안 미처리 1건(ready-for-review 0 / needs-evidence 0 / 기타 1)" + TAIL)


def test_the_other_bucket_stays_hidden_at_zero(audit_module):
    write_pending(audit_module, FIVE[:1])
    assert "기타" not in pending_line(audit_module)


def test_the_line_still_needs_an_active_injection_week(audit_module):
    write_pending(audit_module, FIVE)
    write_events(audit_module, [event("reflect", "x")])           # 주입 활동 없음 -> 줄 자체가 없다
    assert audit_module.injection_lines(audit_module.injection_signal(SINCE)) == []


def test_quiet_week_or_absent_log_adds_nothing(audit_module):
    assert audit_module.injection_lines(audit_module.injection_signal(SINCE)) == []   # no log
    write_events(audit_module, [event("reflect", "x")])
    signal = audit_module.injection_signal(SINCE)
    assert not audit_module.injection_active(signal) and audit_module.injection_lines(signal) == []


def test_notify_message_carries_injection_lines(audit_module, monkeypatch):
    sent = {}

    class Resp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        sent["content"] = json.loads(req.data)["content"]
        return Resp()

    monkeypatch.setenv("GGG_DISCORD_BOT_TOKEN", "synthetic")
    monkeypatch.setenv("GGG_DISCORD_CHANNEL_ID", "1")
    monkeypatch.setattr(audit_module.urllib.request, "urlopen", fake_urlopen)
    write_events(audit_module, [event("context-trigger", "match:demo-dash-login")])
    audit_module.notify({"available": False}, {"warnings": []}, {"candidates": []},
                        audit_module.injection_signal(SINCE))
    assert "길목 주입 7일: 호출 1 · 매칭 1" in sent["content"]


# ---- reflect-demand 태그 상한이 수요 집계에 미치는 영향 (issue #103) ----

def reflect_event(tags, **reflect):
    return event("reflect-demand", "based_on:hit",
                 reflect={"transport": "mcp", "tags": tags, "based_on": "hit",
                          "mental_models": [], **reflect})


def test_demand_counts_reflect_lines_whose_tags_were_capped_by_the_gateway(audit_module):
    """게이트웨이가 reflect.tags 를 앞 64개만 싣는다 - 65번째 뒤 태그는 수요에서 빠지므로 잘린 줄 수를 드러낸다."""
    write_events(audit_module, [
        reflect_event([f"topic:t{i}" for i in range(64)], tags_count=100, tags_omitted=36),
        reflect_event(["topic:a"]),
    ])

    demand = audit_module.demand_signal(set(), SINCE)

    assert demand["truncated_tag_lines"] == 1
    assert demand["instrumented"] == 2


def test_demand_without_capped_lines_keeps_the_counter_at_zero(audit_module):
    write_events(audit_module, [reflect_event(["topic:a"])])

    assert audit_module.demand_signal(set(), SINCE)["truncated_tag_lines"] == 0


def test_a_model_tag_longer_than_the_audit_cap_is_still_covered(audit_module):
    """감사 줄 태그는 256자로 잘린다 - 300자 모델 태그와 같은 태그의 reflect 줄이 미커버 후보가 되면 안 된다."""
    long_tag = "topic:" + "a" * 294                       # 300자
    write_events(audit_module, [reflect_event([long_tag[:audit_module.TAG_LOG_MAX]])])

    demand = audit_module.demand_signal({long_tag}, SINCE)

    assert demand["candidates"] == []
    assert demand["tags"][0]["covered"] is True and demand["tags"][0]["hits"] == 1


def test_the_audit_cap_copy_matches_the_gateway_constant(audit_module):
    from gateway.audit import TAG_LOG_MAX
    assert audit_module.TAG_LOG_MAX == TAG_LOG_MAX


def test_a_deeply_nested_decisions_line_is_ignored_and_does_not_stop_the_report(audit_module):
    write_pending(audit_module, FIVE)
    write_decisions(audit_module, [("d1", "rejected")], raw_lines=["[" * 100000])
    assert pending_line(audit_module).startswith("- 트리거 초안 미처리 4건(")
