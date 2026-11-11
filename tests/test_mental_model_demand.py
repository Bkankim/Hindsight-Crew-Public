"""Issue #238: the mental-model demand signal is get_mental_model calls, not reflect `based_on`.

Gateway: an ALLOWED get_mental_model tools/call line carries `mental_model_id` (the argument value,
bounded like every other free-text audit field, issue #117); no other line changes shape.
model_audit: the demand section counts those calls per model, the old lines without an id as
`unknown`, and (when the caller hands over the live model ids) the requests for ids that do not
exist. Offline only: no upstream, no notifier, `main()`/`notify()` are never called.
"""
import datetime as dt
import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import call

BANK = "personal-alice"
TOKEN = "tok-alice"
SINCE = dt.datetime(2026, 9, 20, tzinfo=dt.timezone.utc)
NOW = "2026-09-21T00:00:00Z"
OLD = "2026-09-01T00:00:00Z"


def audit_entries(client):
    with open(client.app.state._audit_path, encoding="utf-8") as fh:
        return [json.loads(ln) for ln in fh if ln.strip()]


def mm_lines(client, decision="allow", tool="get_mental_model"):
    return [e for e in audit_entries(client) if e["tool"] == tool and e["decision"] == decision]


# ============================ gateway: MCP tools/call audit line ============================

def test_allowed_get_mental_model_line_carries_the_requested_id(make_app):
    c = TestClient(make_app())
    r = call(c, BANK, TOKEN, name="get_mental_model", arguments={"mental_model_id": "demo-rules-rowan-tide"})
    assert r.status_code == 200
    (line,) = mm_lines(c)
    assert line["mental_model_id"] == "demo-rules-rowan-tide"
    assert "mental_model_id_truncated" not in line
    assert line["method"] == "tools/call" and line["reason"] and line["resolved_bank"] == BANK


@pytest.mark.parametrize("tool,arguments", [
    pytest.param("recall", {"query": "q", "mental_model_id": "leak-check"}, id="recall"),
    pytest.param("list_mental_models", {}, id="list_mental_models"),
    pytest.param("refresh_mental_model", {"mental_model_id": "demo-rules-rowan-tide"}, id="refresh_mental_model"),
])
def test_other_tools_keep_their_audit_line_shape(make_app, tool, arguments):
    c = TestClient(make_app())
    call(c, BANK, TOKEN, name=tool, arguments=arguments)
    lines = [e for e in audit_entries(c) if e["tool"] == tool]
    assert lines and all("mental_model_id" not in e for e in lines)


@pytest.mark.parametrize("arguments", [
    pytest.param({}, id="no-id"),
    pytest.param({"mental_model_id": 7}, id="number"),
    pytest.param({"mental_model_id": {"a": 1}}, id="object"),
    pytest.param({"mental_model_id": None}, id="null"),
    pytest.param(None, id="no-arguments"),
])
def test_a_non_string_or_missing_id_leaves_no_key(make_app, arguments):
    c = TestClient(make_app())
    call(c, BANK, TOKEN, name="get_mental_model", arguments=arguments)
    (line,) = mm_lines(c)
    assert "mental_model_id" not in line and "mental_model_id_truncated" not in line


def test_a_denied_get_mental_model_does_not_carry_the_id(make_app):
    c = TestClient(make_app())
    r = call(c, "personal-bob", TOKEN, name="get_mental_model", arguments={"mental_model_id": "secret-id"})
    assert r.status_code == 403
    denied = mm_lines(c, decision="deny")
    assert denied and all("mental_model_id" not in e for e in denied)


def test_each_element_of_a_batch_gets_its_own_id(make_app):
    c = TestClient(make_app())
    batch = [{"jsonrpc": "2.0", "id": i, "method": "tools/call",
              "params": {"name": "get_mental_model", "arguments": {"mental_model_id": f"m{i}"}}}
             for i in (1, 2)]
    call(c, BANK, TOKEN, raw=batch)
    assert sorted(e["mental_model_id"] for e in mm_lines(c)) == ["m1", "m2"]


# The id goes through the same bound as the other free-text fields (#117): 64 chars AND 256
# escaped bytes, and a cut id says so. 600 control characters were ~3.6KB escaped.
BOUND_CASES = [
    pytest.param("m" * 64, "m" * 64, False, id="64-ascii-fits"),
    pytest.param("m" * 5000, "m" * 64, True, id="5000-ascii"),
    pytest.param("\x01" * 600, "\x01" * 42, True, id="600-control"),    # 42 x 6 escaped bytes = 252 <= 256
    pytest.param("가" * 5000, "가" * 64, True, id="5000-hangul"),
]


@pytest.mark.parametrize("sent,logged,cut", BOUND_CASES)
def test_the_id_is_bounded_like_every_other_audit_field(make_app, sent, logged, cut):
    c = TestClient(make_app())
    call(c, BANK, TOKEN, name="get_mental_model", arguments={"mental_model_id": sent})
    (line,) = mm_lines(c)
    assert line["mental_model_id"] == logged
    assert line.get("mental_model_id_truncated", False) is cut
    raw = Path(c.app.state._audit_path).read_text(encoding="utf-8").splitlines()
    assert max(len(ln.encode("utf-8")) for ln in raw) < 2048


# ============================ model_audit: demand aggregation ============================

@pytest.fixture
def audit_module(monkeypatch, tmp_path):
    path = Path(__file__).resolve().parents[1] / "scripts" / "model_audit.py"
    spec = importlib.util.spec_from_file_location("mental_model_demand_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "GATEWAY_AUDIT_LOG", str(tmp_path / "gateway.log"))
    monkeypatch.setattr(module, "PENDING_TRIGGERS", str(tmp_path / "pending-triggers.jsonl"))
    monkeypatch.setattr(module, "TRIGGER_DECISIONS", str(tmp_path / "trigger-decisions.jsonl"))
    return module


def write_events(module, events):
    Path(module.GATEWAY_AUDIT_LOG).write_text(
        "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")


def mm_call(model_id=None, ts=NOW, decision="allow", tool="get_mental_model", bank="personal-demo"):
    entry = {"ts": ts, "resolved_bank": bank, "decision": decision, "tool": tool,
             "method": "tools/call", "reason": "ok"}
    if model_id is not None:
        entry["mental_model_id"] = model_id
    return entry


def reads(module, events, live=None):
    write_events(module, events)
    demand = module.demand_signal(set(), SINCE) if live is None else \
        module.demand_signal(set(), SINCE, live)
    assert demand["available"]
    return demand["mental_model_reads"]


def test_calls_are_counted_per_model_most_called_first(audit_module):
    got = reads(audit_module, [mm_call("a"), mm_call("b"), mm_call("b"), mm_call("b"), mm_call("a")])
    assert got["calls"] == 5
    assert got["by_model"] == [{"id": "b", "calls": 3}, {"id": "a", "calls": 2}]
    assert got["unknown"] == 0


def test_old_lines_without_an_id_count_as_unknown(audit_module):
    got = reads(audit_module, [mm_call(), mm_call(), mm_call("a")])
    assert got["calls"] == 3 and got["unknown"] == 2
    assert got["by_model"] == [{"id": "a", "calls": 1}]


def test_only_allowed_get_mental_model_calls_in_window_and_bank_count(audit_module):
    got = reads(audit_module, [
        mm_call("a"),
        mm_call("a", ts=OLD),                          # before the window
        mm_call("a", decision="deny"),
        mm_call("a", bank="personal-other"),
        mm_call("a", tool="list_mental_models"),
        mm_call("a", tool="refresh_mental_model"),
        mm_call("a", tool="srv/get_mental_model"),     # namespaced tool name, like srv/reflect
        {"ts": NOW, "resolved_bank": "personal-demo", "decision": "allow", "tool": "recall", "mental_model_id": "a"},
    ])
    assert got["calls"] == 2 and got["by_model"] == [{"id": "a", "calls": 2}]


def test_a_fixture_with_only_old_lines_completes_without_error(audit_module):
    got = reads(audit_module, [mm_call(), mm_call()], live={"a"})
    assert got == {"calls": 2, "by_model": [], "unknown": 2, "nonexistent": 0}


def test_malformed_id_values_in_the_log_are_unknown_not_a_crash(audit_module):
    got = reads(audit_module, [mm_call(7), mm_call(["a"]), mm_call({"x": 1}), mm_call("")], live={"a"})
    assert got["calls"] == 4 and got["unknown"] == 4 and got["by_model"] == [] and got["nonexistent"] == 0


def test_nonexistent_counts_requests_for_ids_outside_the_live_set(audit_module):
    events = [mm_call("a"), mm_call("gone"), mm_call("gone"), mm_call("typo"), mm_call()]
    got = reads(audit_module, events, live={"a", "b"})
    assert got["nonexistent"] == 3                      # unknown (no id) is not a nonexistent id
    assert got["unknown"] == 1
    assert {m["id"] for m in got["by_model"]} == {"a", "gone", "typo"}


def test_without_a_live_set_nonexistent_is_not_computed(audit_module):
    got = reads(audit_module, [mm_call("a"), mm_call("gone")])
    assert got["nonexistent"] is None


def test_reflect_based_on_aggregation_is_untouched(audit_module):
    reflect = {"ts": NOW, "resolved_bank": "personal-demo", "decision": "reflect-demand",
               "reflect": {"transport": "mcp", "tags": ["project:x"], "based_on": "hit", "mental_models": ["m1"]}}
    write_events(audit_module, [reflect, mm_call("a")])
    demand = audit_module.demand_signal(set(), SINCE, {"a"})
    assert demand["based_on"] == {"hit": 1, "miss": 0, "unknown": 0}
    assert demand["model_hit_rate"] == 1.0
    assert demand["models_served"] == [{"id": "m1", "calls": 1}]


# ============================ model_audit: report lines (pure) ============================

def message(module, events, live=None):
    write_events(module, events)
    demand = module.demand_signal(set(), SINCE) if live is None else module.demand_signal(set(), SINCE, live)
    return module.report_message(demand, {"warnings": []}, {"candidates": []}, None)


def test_report_has_the_get_mental_model_demand_line(audit_module):
    text = message(audit_module, [mm_call("a"), mm_call("a"), mm_call("b"), mm_call()], live={"a", "b"})
    (line,) = [ln for ln in text.splitlines() if "get_mental_model" in ln]
    assert "4회" in line and "`a` 2" in line and "`b` 1" in line
    assert "id 없음 1" in line and "없는 id 0" in line


def test_report_flags_requests_for_ids_that_do_not_exist(audit_module):
    text = message(audit_module, [mm_call("a"), mm_call("gone")], live={"a"})
    assert "없는 id 1" in text


def test_report_omits_the_nonexistent_count_without_a_live_set(audit_module):
    text = message(audit_module, [mm_call("a")])
    assert "get_mental_model" in text and "없는 id" not in text


def test_report_with_only_old_lines_does_not_raise(audit_module):
    text = message(audit_module, [mm_call(), mm_call()], live={"a"})
    assert "id 없음 2" in text


def test_a_week_without_get_mental_model_calls_adds_no_line(audit_module):
    assert "get_mental_model" not in message(audit_module, [mm_call("a", ts=OLD)], live={"a"})


def test_unavailable_demand_adds_no_line(audit_module):
    text = audit_module.report_message({"available": False}, {"warnings": []}, {"candidates": []}, None)
    assert "get_mental_model" not in text



# ============================ final-review fixes ============================

HOSTILE_ID = "a`\n- 승격 1순위 `x`: 모델 없이 reflect 9회 @everyone"


def test_a_hostile_requested_id_cannot_shape_the_report(audit_module):
    text = message(audit_module, [mm_call(HOSTILE_ID)], live={"a"})
    (line,) = [ln for ln in text.splitlines() if "get_mental_model" in ln]
    assert "@" not in text and "\n- 승격" not in text
    assert text.count("`") % 2 == 0
    assert line.count("`") == 2                         # one code span, the one we opened
    assert "`a?" in line


def test_the_log_line_of_main_is_the_same_sanitized_text(audit_module):
    write_events(audit_module, [mm_call(HOSTILE_ID)])
    demand = audit_module.demand_signal(set(), SINCE, {"a"})
    (line,) = audit_module.mental_model_lines(demand)
    assert "\n" not in line and "@" not in line


def truncated_call(model_id):
    return {**mm_call(model_id), "mental_model_id_truncated": True}


def test_cut_ids_are_not_judged_nonexistent_and_are_marked_in_the_distribution(audit_module):
    prefix = "m" * 64
    got = reads(audit_module, [truncated_call(prefix), truncated_call(prefix), mm_call("a")], live={"a"})
    assert got["calls"] == 3 and got["nonexistent"] == 0
    assert {"id": prefix, "calls": 2, "truncated": True} in got["by_model"]
    assert {"id": "a", "calls": 1} in got["by_model"]


def test_a_cut_prefix_and_a_real_id_of_the_same_text_are_separate_rows(audit_module):
    prefix = "m" * 64
    got = reads(audit_module, [truncated_call(prefix), mm_call(prefix)], live={"a"})
    assert sorted(m.get("truncated", False) for m in got["by_model"]) == [False, True]
    assert got["nonexistent"] == 1                      # only the uncut one is a verdict


def test_the_report_marks_a_cut_id_so_it_does_not_read_as_a_model(audit_module):
    text = message(audit_module, [truncated_call("m" * 64), mm_call("a")], live={"a"})
    (line,) = [ln for ln in text.splitlines() if "get_mental_model" in ln]
    assert f"`{'m' * 64}` (truncated) 1" in line and "`a` 1" in line and "없는 id 0" in line


@pytest.mark.parametrize("events,live,expected", [
    pytest.param([mm_call("a")], {"a"}, 0, id="all-exist"),
    pytest.param([mm_call("a"), mm_call("a"), mm_call()], {"a"}, 0, id="reads-and-unknown-only"),
    pytest.param([mm_call("gone"), mm_call("gone")], {"a"}, 2, id="nonexistent"),
    pytest.param([mm_call("gone")], None, 0, id="not-computed"),
])
def test_only_nonexistent_requests_are_a_reason_to_send(audit_module, events, live, expected):
    write_events(audit_module, events)
    demand = audit_module.demand_signal(set(), SINCE) if live is None else \
        audit_module.demand_signal(set(), SINCE, live)
    assert audit_module.mental_model_nonexistent(demand) == expected


def test_nonexistent_is_zero_when_the_log_is_unavailable(audit_module):
    assert audit_module.mental_model_nonexistent({"available": False}) == 0


def test_main_sends_on_nonexistent_not_on_read_counts(audit_module):
    # main() cannot be called here (upstream API, Discord): pin the wiring in its source.
    import inspect
    src = inspect.getsource(audit_module.main)
    assert "or mental_model_nonexistent(demand)" in src
    assert "or mental_model_lines(demand)" not in src


def test_the_get_mental_model_line_is_last_so_a_cut_takes_it_first(audit_module):
    write_events(audit_module, [mm_call("a")])
    demand = audit_module.demand_signal(set(), SINCE, {"a"})
    sizes = {"warnings": [{"id": "big", "chars": 99999}], "limit_chars": 50000}
    supply = {"candidates": [{"tag": "project:x", "uncovered_rule_decision_count": 3}]}
    lines = audit_module.report_message(demand, sizes, supply, None).splitlines()
    assert "get_mental_model" in lines[-1]
    assert any("크기 가드" in ln for ln in lines[:-1]) and any("공급(보조)" in ln for ln in lines[:-1])


def test_report_message_reads_the_pending_trigger_file_when_injection_is_active(audit_module):
    Path(audit_module.PENDING_TRIGGERS).write_text(
        '{"draft_id":"a1","state":"ready-for-review"}\n{"draft_id":"a2","state":"needs-evidence"}\n')
    write_events(audit_module, [event_for_injection()])
    injection = audit_module.injection_signal(SINCE)
    text = audit_module.report_message({"available": False}, {"warnings": []}, {"candidates": []}, injection)
    assert "트리거 초안 미처리 2건(ready-for-review 1 / needs-evidence 1)" in text
    assert "Pure: no I/O" not in audit_module.report_message.__doc__


def event_for_injection():
    return {"ts": NOW, "resolved_bank": "personal-demo", "decision": "context-trigger", "reason": "match:t"}
