"""중복 recall 출처 분석 스크립트(scripts/duplicate_recall.py) 테스트 (issue #81).

가짜 원장·감사 로그만 쓴다(쿼리·세션 문자열은 전부 지어낸 값). 기대값은 입력에서 손으로 센 값이다.
공개 경계는 analyze()의 결과 JSON과 CLI(main) 출력이다.

시간은 기준 시각 T0 + 초 오프셋으로 쓴다. 원장 ts는 응답 완료 시각, 감사 로그 allow 줄 ts는
요청 도착 시각이다(둘 다 1초 내림).
"""
import datetime as dt
import importlib.util
import json
from pathlib import Path

import pytest

T0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
DEMO = "demo/personal-demo"


@pytest.fixture(scope="module")
def dup():
    path = Path(__file__).resolve().parents[1] / "scripts" / "duplicate_recall.py"
    spec = importlib.util.spec_from_file_location("duplicate_recall_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def iso(offset):
    return (T0 + dt.timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%SZ")


def led(done, query, session, *, transport="mcp", identity="demo", bank="personal-demo",
        tags=(), reusable=None, tags_match="any"):
    row = {"ts": iso(done), "identity": identity, "bank": bank, "transport": transport,
           "query": query, "tags": list(tags), "result_ids": ["m1"], "session_id": session,
           "tags_match": tags_match}
    if reusable is not None:
        row["reusable"] = reusable
    return row


def arrive(at, *, transport="mcp", identity="demo", bank="personal-demo"):
    if transport == "mcp":
        return {"ts": iso(at), "identity": identity, "method": "tools/call", "tool": "recall",
                "resolved_bank": bank, "attempted_bank": bank, "decision": "allow", "reason": "allow:recall"}
    return {"ts": iso(at), "identity": identity, "method": "POST",
            "tool": f"/v1/default/banks/{bank}/memories/recall", "resolved_bank": bank,
            "attempted_bank": bank, "decision": "allow", "reason": "rest-allow"}


def retain_line(at, *, bank="personal-demo", transport="mcp", decision="allow"):
    if transport == "mcp":
        return {"ts": iso(at), "identity": "demo", "method": "tools/call", "tool": "retain",
                "resolved_bank": bank, "attempted_bank": bank, "decision": decision,
                "reason": "allow:retain" if decision == "allow" else "rule-violation:RULE:x"}
    return {"ts": iso(at), "identity": "demo", "method": "POST", "tool": f"/v1/default/banks/{bank}/memories",
            "resolved_bank": bank, "attempted_bank": bank, "decision": decision,
            "reason": "rest-allow" if decision == "allow" else "rule-violation:RULE:x"}


def primary(result):
    return result["populations"][DEMO]


def detail(result):
    return primary(result)["within_60s_detail"]


# --- 케이스 x 표면 행렬 -------------------------------------------------------------------
# 표면: mcp 같은 세션 / mcp 다른 세션 / rest(세션 없음). 케이스는 A(도착, 완료)와 B(도착, 완료).
SURFACES = [
    ("mcp-same", "mcp", "s1", "s1", "same_session"),
    ("mcp-cross", "mcp", "s1", "s2", "cross_session"),
    ("rest", "rest", None, None, "unknown_session"),
]
SCENARIOS = [
    # 이름,            A도착, A완료, B도착, B완료, 60초 이내 쌍 수, 동시성
    ("sequential",         0,     5,    20,    25, 1, "sequential"),     # B 도착 20 > A 완료 5
    ("concurrent",         0,     5,     2,     7, 1, "concurrent"),     # B 도착 2 < A 완료 5
    ("same-second",        0,     5,     5,    10, 1, "ambiguous"),      # B 도착 5 == A 완료 5
    ("over-60s",           0,     5,    70,    75, 0, None),             # 간격 70초: 쌍이지만 60초 밖
]


@pytest.mark.parametrize("surface", SURFACES, ids=[s[0] for s in SURFACES])
@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s[0] for s in SCENARIOS])
def test_pair_classification_matrix(dup, surface, scenario):
    _, transport, sess_a, sess_b, expected_class = surface
    _, a_in, a_out, b_in, b_out, within, concurrency = scenario
    ledger = [led(a_out, "q1", sess_a, transport=transport), led(b_out, "q1", sess_b, transport=transport)]
    audit = [arrive(a_in, transport=transport), arrive(b_in, transport=transport)]

    pop = primary(dup.analyze(ledger, audit))

    assert pop["repeat_pairs"] == 1
    assert pop["within_60s"] == within
    assert pop["over_60s"] == 1 - within
    assert pop["join"]["ledger_rows_unmatched"] == 0
    classes = pop["within_60s_detail"]["by_class"]
    for name, cls in classes.items():
        if within and name == expected_class:
            assert cls["total"] == 1
            assert cls["by_concurrency"] == {concurrency: 1}
            assert cls["by_transport"] == {transport: 1}
        else:
            assert cls["total"] == 0, name


# --- 쌍이 되지 않는 경우 ------------------------------------------------------------------

@pytest.mark.parametrize("transport", ["mcp", "rest"])
def test_different_tags_are_not_a_pair_but_count_under_query_only_key(dup, transport):
    ledger = [led(5, "q1", "s1", transport=transport, tags=["t:a"]),
              led(25, "q1", "s1", transport=transport, tags=["t:b"])]
    audit = [arrive(0, transport=transport), arrive(20, transport=transport)]

    pop = primary(dup.analyze(ledger, audit))

    assert pop["repeat_pairs"] == 0
    assert pop["query_only_key"]["repeat_pairs"] == 1
    assert pop["query_only_key"]["within_60s"] == 1


@pytest.mark.parametrize("transport", ["mcp", "rest"])
def test_tag_order_does_not_split_a_request(dup, transport):
    ledger = [led(5, "q1", None, transport=transport, tags=["t:a", "t:b"]),
              led(25, "q1", None, transport=transport, tags=["t:b", "t:a"])]

    assert primary(dup.analyze(ledger, []))["repeat_pairs"] == 1


@pytest.mark.parametrize("transport", ["mcp", "rest"])
@pytest.mark.parametrize("first,second", [
    (["topic:x"], ["topic:x", "topic:x"]),
    (["topic:x", "topic:x"], ["topic:x"]),
    (["topic:x", "topic:y", "topic:x"], ["topic:y", "topic:x"]),
])
def test_duplicate_tag_elements_do_not_split_a_request(dup, transport, first, second):
    """이슈 #109 항목 4: 태그 키는 집합 기준이다. 중복 원소만 다른 두 요청은 같은 요청이다."""
    ledger = [led(5, "q1", None, transport=transport, tags=first),
              led(25, "q1", None, transport=transport, tags=second)]

    assert dup._request_key(ledger[0]) == dup._request_key(ledger[1])
    assert primary(dup.analyze(ledger, []))["repeat_pairs"] == 1


@pytest.mark.parametrize("transport", ["mcp", "rest"])
def test_retain_between_breaks_the_pair(dup, transport):
    ledger = [led(5, "q1", "s1", transport=transport), led(105, "q1", "s1", transport=transport)]
    audit = [arrive(0, transport=transport), retain_line(50), arrive(100, transport=transport)]

    pop = primary(dup.analyze(ledger, audit))

    assert pop["repeat_pairs"] == 0
    assert pop["repeats_broken_by_retain"] == 1
    assert pop["repeats_broken_by_retain_within_60s"] == 0       # 간격 100초라 60초 밖


@pytest.mark.parametrize("retain_at,broken", [(5, False), (6, True), (25, True), (26, False)])
def test_retain_boundary_is_after_a_completion_and_up_to_b_completion(dup, retain_at, broken):
    """A 완료 5, B 완료 25. 같은 초(5)의 retain은 A보다 먼저 온 것으로 보고(#76 정의와 같은 경계) 끊지 않는다."""
    ledger = [led(5, "q1", "s1"), led(25, "q1", "s1")]
    audit = [arrive(0), retain_line(retain_at), arrive(20)]

    pop = primary(dup.analyze(ledger, audit))

    assert pop["repeat_pairs"] == (0 if broken else 1)
    assert pop["repeats_broken_by_retain"] == (1 if broken else 0)


@pytest.mark.parametrize("transport", ["mcp", "rest"])
@pytest.mark.parametrize("allow_at,reject_at,broken", [
    (50, 50, False),      # 거부 줄이 같은 초의 allow 줄을 지운다: 실제로는 아무것도 안 쓰였다
    (49, 50, False),      # allow는 거부 줄보다 최대 1초 앞선다
    (48, 50, True),       # 2초 앞선 allow 줄은 이 거부와 짝이 아니다
])
def test_rejected_retain_does_not_break_the_pair(dup, transport, allow_at, reject_at, broken):
    ledger = [led(5, "q1", "s1"), led(105, "q1", "s1")]
    audit = [arrive(0), retain_line(allow_at, transport=transport),
             retain_line(reject_at, transport=transport, decision="enforce-reject"), arrive(100)]

    pop = primary(dup.analyze(ledger, audit))

    assert pop["repeat_pairs"] == (0 if broken else 1)
    assert pop["repeats_broken_by_retain"] == (1 if broken else 0)


@pytest.mark.parametrize("transport", ["mcp", "rest"])
def test_one_reject_cancels_only_one_allow_line(dup, transport):
    """같은 초에 allow 2줄 + 거부 1줄: 하나는 실제로 쓰였으므로 쌍이 끊긴다."""
    ledger = [led(5, "q1", "s1"), led(105, "q1", "s1")]
    audit = [arrive(0), retain_line(50, transport=transport), retain_line(50, transport=transport),
             retain_line(50, transport=transport, decision="enforce-reject"), arrive(100)]

    pop = primary(dup.analyze(ledger, audit))

    assert pop["repeat_pairs"] == 0
    assert pop["repeats_broken_by_retain"] == 1


def test_rest_request_with_two_rejected_items_cancels_only_its_own_allow_line(dup):
    """REST는 요청당 allow 1줄 + 위반 item마다 거부 1줄. 같은 초의 정상 retain은 쌍을 끊는다."""
    ledger = [led(5, "q1", None, transport="rest"), led(105, "q1", None, transport="rest")]
    audit = [arrive(0, transport="rest"),
             retain_line(50, transport="rest"),                                    # 위반 item 2개짜리 요청의 allow
             retain_line(50, transport="rest", decision="enforce-reject"),
             retain_line(50, transport="rest", decision="enforce-reject"),
             retain_line(50, transport="rest"),                                    # 같은 초의 정상 요청
             arrive(100, transport="rest")]

    pop = primary(dup.analyze(ledger, audit))

    assert pop["repeat_pairs"] == 0
    assert pop["repeats_broken_by_retain"] == 1


def test_rest_dedupe_does_not_swallow_a_rest_reject_after_an_mcp_reject_in_the_same_second(dup):
    """같은 identity·bank·초에 MCP reject가 먼저 오면 뒤의 REST reject가 합쳐져 버리면 안 된다(transport별 구분)."""
    ledger = [led(5, "q1", "s1"), led(105, "q1", "s1")]
    audit = [arrive(0),
             retain_line(50), retain_line(50, transport="rest"),                       # allow 둘
             retain_line(50, decision="enforce-reject"),                                # MCP reject
             retain_line(50, transport="rest", decision="enforce-reject"),              # REST reject
             arrive(100)]

    pop = primary(dup.analyze(ledger, audit))

    assert pop["repeat_pairs"] == 1                  # 둘 다 취소돼 쌍이 남는다
    assert pop["repeats_broken_by_retain"] == 0


def test_rejected_retain_of_another_bank_does_not_cancel_an_allow_line(dup):
    ledger = [led(5, "q1", "s1"), led(105, "q1", "s1")]
    audit = [arrive(0), retain_line(50),
             retain_line(50, bank="team-eng", decision="enforce-reject"), arrive(100)]

    assert primary(dup.analyze(ledger, audit))["repeats_broken_by_retain"] == 1


def test_retain_to_another_bank_does_not_break_the_pair(dup):
    ledger = [led(5, "q1", "s1"), led(25, "q1", "s1")]
    audit = [arrive(0), retain_line(10, bank="team-eng"), arrive(20)]

    assert primary(dup.analyze(ledger, audit))["repeat_pairs"] == 1


def test_gap_over_one_hour_is_not_a_pair(dup):
    ledger = [led(5, "q1", "s1"), led(3606, "q1", "s1")]          # 간격 3601초

    assert primary(dup.analyze(ledger, []))["repeat_pairs"] == 0


def test_exactly_one_hour_is_a_pair_and_exactly_60s_is_within(dup):
    ledger = [led(0, "q1", "s1"), led(60, "q1", "s1"), led(3660, "q1", "s1")]   # 간격 60, 3600

    pop = primary(dup.analyze(ledger, []))

    assert pop["repeat_pairs"] == 2
    assert pop["within_60s"] == 1
    assert pop["gap_hist"]["31-60s"] == 1
    assert pop["gap_hist"]["301-3600s"] == 1


def test_non_reusable_rows_are_excluded_and_missing_field_counts_as_reusable(dup):
    ledger = [led(5, "q1", "s1", reusable=False), led(25, "q1", "s1", reusable=False),   # 제외
              led(105, "q2", "s1"), led(125, "q2", "s1", reusable=True)]                 # 필드 없음 + True

    pop = primary(dup.analyze(ledger, []))

    assert (pop["rows"], pop["reusable_rows"], pop["non_reusable_rows"]) == (4, 2, 2)
    assert pop["repeat_pairs"] == 1


def test_period_is_reported_per_population_and_overall(dup):
    day = 86400
    ledger = [led(2 * day, "q1", "s1"), led(5 * day, "q2", "s1"),                       # demo: 3일
              led(0, "q1", None, transport="rest", identity="alice", bank="personal-alice"),
              led(8 * day, "q1", None, transport="rest", identity="alice", bank="personal-alice")]   # alice: 8일

    result = dup.analyze(ledger, [])

    assert result["period"] == {"ledger_first": iso(0), "ledger_last": iso(8 * day), "days": 8.0}
    assert primary(result)["period"] == {"ledger_first": iso(2 * day), "ledger_last": iso(5 * day), "days": 3.0}


def test_other_identity_and_bank_are_separate_populations(dup):
    ledger = [led(5, "q1", "s1"), led(25, "q1", "s1"),
              led(5, "q1", None, transport="rest", identity="alice", bank="personal-alice"),
              led(9, "q1", None, transport="rest", identity="alice", bank="personal-alice"),
              led(5, "q1", None, transport="rest", identity="alice", bank="team-eng")]

    result = dup.analyze(ledger, [])

    assert sorted(result["populations"]) == ["alice/personal-alice", "alice/team-eng", DEMO]
    assert result["populations"][DEMO]["within_60s"] == 1
    assert result["populations"]["alice/personal-alice"]["within_60s"] == 1
    assert result["populations"]["alice/team-eng"]["repeat_pairs"] == 0


# --- 결합(원장 완료 시각 <-> 감사 도착 시각) -----------------------------------------------

def test_without_audit_lines_concurrency_is_unjoined(dup):
    pop = primary(dup.analyze([led(5, "q1", "s1"), led(25, "q1", "s1")], []))

    assert pop["join"] == {"ledger_rows_matched": 0, "ledger_rows_unmatched": 2,
                           "audit_recall_lines_without_ledger_row": 0}
    assert pop["within_60s_detail"]["by_concurrency"] == {"unjoined": 1}


def test_back_to_back_calls_keep_their_own_arrival_second(dup):
    """연속 호출: 다음 요청이 앞 요청 완료와 같은 초에 도착해도 줄이 한 칸씩 밀리지 않는다."""
    ledger = [led(5, "q1", "s1"), led(10, "q1", "s1"), led(15, "q1", "s1")]
    audit = [arrive(0), arrive(5), arrive(10)]

    pop = primary(dup.analyze(ledger, audit))

    assert pop["latency_hist"]["4-7s"] == 3          # 셋 모두 5초. 한 칸 밀리면 0초가 섞인다
    assert pop["within_60s_detail"]["by_concurrency"] == {"ambiguous": 2}


def test_failed_attempt_line_without_ledger_row_is_skipped_not_shifted(dup):
    """원장에 안 남은 실패 요청의 allow 줄(도착 2)이 성공 요청(도착 3, 완료 8)과 안 섞인다."""
    ledger = [led(8, "q1", "s1")]
    audit = [arrive(2), arrive(3)]

    pop = primary(dup.analyze(ledger, audit))

    assert pop["join"]["audit_recall_lines_without_ledger_row"] == 1
    assert pop["latency_hist"]["4-7s"] == 1          # 8 - 3 = 5초


def test_rejected_recall_does_not_count_as_a_failed_attempt(dup):
    reject = dict(arrive(2), decision="enforce-reject", reason="rule-violation:RULE:x")
    audit = [arrive(2), reject, arrive(3)]

    pop = primary(dup.analyze([led(8, "q1", "s1")], audit))

    assert pop["join"]["audit_recall_lines_without_ledger_row"] == 0


def test_audit_lines_outside_ledger_span_are_ignored(dup):
    audit = [arrive(-1000), arrive(3)]               # 원장 도입 전의 줄

    pop = primary(dup.analyze([led(8, "q1", "s1")], audit))

    assert pop["join"]["audit_recall_lines_without_ledger_row"] == 0


# --- 연쇄(loop) ---------------------------------------------------------------------------

@pytest.mark.parametrize("transport,rows,expected", [
    ("mcp", 5, {"same_session": 4}),        # 5행 = 쌍 4개 < 5: loop 아님
    ("mcp", 6, {"loop": 5}),                # 6행 = 쌍 5개: loop
    ("rest", 5, {"unknown_session": 4}),
    ("rest", 6, {"loop": 5}),
])
def test_chain_of_five_pairs_is_a_loop_regardless_of_surface(dup, transport, rows, expected):
    session = "s1" if transport == "mcp" else None
    ledger = [led(10 * (k + 1), "q1", session, transport=transport) for k in range(rows)]
    audit = [arrive(10 * k + 5, transport=transport) for k in range(rows)]

    detail_ = detail(dup.analyze(ledger, audit))

    got = {name: c["total"] for name, c in detail_["by_class"].items() if c["total"]}
    assert got == expected
    assert detail_["chain_pair_counts"] == [rows - 1]


def test_chain_breaks_when_a_gap_exceeds_60s(dup):
    # 간격 10, 10, 100, 10, 10: 쌍 5개 중 60초 이내는 4개, 연쇄는 2쌍 + 2쌍
    done = [10, 20, 30, 130, 140, 150]
    ledger = [led(t, "q1", "s1") for t in done]

    detail_ = detail(dup.analyze(ledger, []))

    assert detail_["total"] == 4
    assert detail_["chain_pair_counts"] == [2, 2]
    assert detail_["by_class"]["same_session"]["total"] == 4


# --- 전체 합산 ----------------------------------------------------------------------------

def fixed_dataset():
    """손으로 센 작은 데이터. 쌍은 (r1,r2) 같은 세션 순차, (r3,r4) 다른 세션 동시."""
    ledger = [
        led(5, "q1", "s1"),                                   # r1  도착 0
        led(25, "q1", "s1"),                                  # r2  도착 20
        led(105, "q2", "s1"),                                 # r3  도착 100
        led(108, "q2", "s2"),                                 # r4  도착 102 (< r3 완료 105: 동시)
        led(200, "q3", "s3"),                                 # r6  도착 195
        led(300, "q3", "s3"),                                 # r7  도착 295 (사이 250에 retain)
        led(400, "q4", "s4", tags=["t:a"]),                   # r8  도착 395
        led(410, "q4", "s4", tags=["t:b"]),                   # r9  도착 405 (태그 다름)
        led(500, "q5", "s5", reusable=False),                 # r10 도착 495 (제외)
        led(510, "q5", "s5", reusable=False),                 # r11 도착 505 (제외)
        led(4000, "q1", "s1"),                                # r5  도착 3995 (r2와 3975초 차)
        led(5, "q1", None, transport="rest", identity="alice", bank="personal-alice"),
        led(9, "q1", None, transport="rest", identity="alice", bank="personal-alice"),
    ]
    audit = [arrive(t) for t in (0, 20, 100, 102, 195, 295, 395, 405, 495, 505, 3995)]
    audit += [arrive(600),                                    # 원장에 안 남은 요청
              retain_line(250),
              arrive(4, transport="rest", identity="alice", bank="personal-alice"),
              arrive(7, transport="rest", identity="alice", bank="personal-alice")]
    return ledger, audit


def test_fixed_dataset_totals(dup):
    ledger, audit = fixed_dataset()

    result = dup.analyze(ledger, audit)
    pop = primary(result)

    assert (pop["rows"], pop["reusable_rows"], pop["non_reusable_rows"]) == (11, 9, 2)
    assert pop["repeat_pairs"] == 2
    assert (pop["within_60s"], pop["over_60s"], pop["same_session_pairs"]) == (2, 0, 1)
    assert (pop["repeats_broken_by_retain"], pop["repeats_broken_by_retain_within_60s"]) == (1, 0)
    # (r1,r2) 같은 세션, (r3,r4) 다른 세션, (r8,r9) 같은 세션: 태그를 무시하면 쌍이 3개
    assert pop["query_only_key"] == {"repeat_pairs": 3, "within_60s": 3, "same_session_pairs": 2}
    assert pop["join"] == {"ledger_rows_matched": 11, "ledger_rows_unmatched": 0,
                           "audit_recall_lines_without_ledger_row": 1}

    det = pop["within_60s_detail"]
    assert det["total"] == 2
    assert det["by_concurrency"] == {"concurrent": 1, "sequential": 1}
    assert det["by_class"]["same_session"]["by_concurrency"] == {"sequential": 1}
    assert det["by_class"]["cross_session"]["by_concurrency"] == {"concurrent": 1}
    assert det["by_class"]["loop"]["total"] == 0
    assert det["gap_hist"]["11-30s"] == 1 and det["gap_hist"]["2-5s"] == 1
    assert det["a_latency_hist"]["4-7s"] == 2          # r1 5초, r3 5초
    assert det["b_latency_hist"]["4-7s"] == 2          # r2 5초, r4 6초
    assert det["interarrival_hist"]["11-30s"] == 1 and det["interarrival_hist"]["2-5s"] == 1
    assert det["concurrent_interarrival_hist"]["2-5s"] == 1
    assert det["distinct_request_keys"] == 2
    assert det["pairs_per_session_top5"] == [1, 1]
    assert det["failed_attempt_within_30s_before_b"] == 0
    assert det["concurrent_upper_bound_with_unmatched_lines"] == 1      # 짝 없는 줄(600)은 어느 B의 결합 창에도 없다
    assert pop["by_day"] == {"2026-01-01": {"reusable_rows": 9, "repeat_pairs": 2, "within_60s": 2, "loop": 0}}

    alice = result["populations"]["alice/personal-alice"]
    # 기간은 population별로 따로 나온다(전체 period는 두 population을 합친 범위)
    assert pop["period"] == {"ledger_first": iso(5), "ledger_last": iso(4000), "days": 0.0}
    assert alice["period"] == {"ledger_first": iso(5), "ledger_last": iso(9), "days": 0.0}
    assert alice["within_60s"] == 1
    assert alice["within_60s_detail"]["by_class"]["unknown_session"]["by_concurrency"] == {"sequential": 1}


def test_unmatched_line_inside_the_join_window_can_hide_a_concurrent_pair(dup):
    """한계의 고정: A 도착 0·완료 5, B 도착 2·완료 7(실제로는 동시). 원장에 안 남은 실패 요청이 도착 6.
    결합이 B에 도착 6을 붙여 sequential로 집계된다. 상한 필드는 이 쌍을 위험으로 센다."""
    ledger = [led(5, "q1", "s1"), led(7, "q1", "s1")]
    audit = [arrive(0), arrive(2), arrive(6)]

    det = detail(dup.analyze(ledger, audit))

    assert det["by_concurrency"] == {"sequential": 1}
    assert det["concurrent_upper_bound_with_unmatched_lines"] == 1


def test_upper_bound_counts_a_leftover_line_beyond_the_join_window_of_b(dup):
    """A 완료 90, B 완료 130, 도착 줄 0·80·95(95는 원장에 없는 실패 요청). 결합은 B에 95, A에 80을 붙이고
    0이 남는다(실제로는 B 도착 80 < A 완료 90: 동시). 남은 0은 B 완료 기준 120초 창 밖이지만 연쇄 안이다."""
    ledger = [led(90, "q1", "s1"), led(130, "q1", "s1")]
    audit = [arrive(0), arrive(80), arrive(95)]

    det = detail(dup.analyze(ledger, audit))

    assert det["by_concurrency"] == {"sequential": 1}
    assert det["concurrent_upper_bound_with_unmatched_lines"] == 1


def test_upper_bound_follows_a_shifted_chain_down_to_a_far_leftover_line(dup):
    """도착 줄 0·90·190·290·330, 완료 R1 100·R2 200·A 300·B 340(줄 330은 원장에 없는 실패 요청).
    결합은 B=330, A=290, R2=190, R1=90이고 0이 남는다. 모두 한 칸씩 밀렸을 수 있으므로 B의 가장 이른
    도착은 290 < A 완료 300이다. 0은 B 완료 기준 120초 창(220~340) 밖이라 창만 보면 놓친다."""
    ledger = [led(100, "q9", "s1"), led(200, "q8", "s1"), led(300, "q1", "s1"), led(340, "q1", "s1")]
    audit = [arrive(t) for t in (0, 90, 190, 290, 330)]

    det = detail(dup.analyze(ledger, audit))

    assert det["by_concurrency"] == {"sequential": 1}
    assert det["concurrent_upper_bound_with_unmatched_lines"] == 1


def test_upper_bound_ignores_a_leftover_line_behind_a_chain_break(dup):
    """도착 줄 0·5·500·520, 완료 r0 10·A 510·B 560. 0이 남지만 A는 0 쪽으로 못 옮겨진다(A 완료 510 기준 120초
    창 밖). 연쇄가 거기서 끊기므로 B의 가장 이른 도착은 520 >= A 완료 510이라 위험이 아니다."""
    ledger = [led(10, "q0", "s1"), led(510, "q1", "s1"), led(560, "q1", "s1")]
    audit = [arrive(t) for t in (0, 5, 500, 520)]

    det = detail(dup.analyze(ledger, audit))

    assert det["by_concurrency"] == {"sequential": 1}
    assert det["concurrent_upper_bound_with_unmatched_lines"] == 0


def test_pair_without_any_joined_line_counts_as_at_risk_in_the_upper_bound(dup):
    det = detail(dup.analyze([led(5, "q1", "s1"), led(25, "q1", "s1")], []))

    assert det["by_concurrency"] == {"unjoined": 1}
    assert det["concurrent_upper_bound_with_unmatched_lines"] == 1


def test_upper_bound_equals_confirmed_concurrent_when_every_line_is_matched(dup):
    ledger = [led(5, "q1", "s1"), led(7, "q1", "s1")]            # B 도착 2 < A 완료 5
    audit = [arrive(0), arrive(2)]

    det = detail(dup.analyze(ledger, audit))

    assert det["by_concurrency"] == {"concurrent": 1}
    assert det["concurrent_upper_bound_with_unmatched_lines"] == 1


def test_b_arriving_before_a_across_transports_lands_in_the_zero_second_bucket(dup):
    """키에 transport가 없어 mcp A(도착 10, 완료 30)와 rest B(도착 5, 완료 35)가 쌍이 된다. 도착 간격 -5는 0초 칸."""
    ledger = [led(30, "q1", "s1"), led(35, "q1", None, transport="rest")]
    audit = [arrive(10), arrive(5, transport="rest")]

    det = detail(dup.analyze(ledger, audit))

    assert det["by_concurrency"] == {"concurrent": 1}
    assert det["interarrival_hist"]["0s"] == 1
    assert det["concurrent_interarrival_hist"]["0s"] == 1
    assert "over" not in det["interarrival_hist"]
    assert "over" not in det["concurrent_interarrival_hist"]


def test_failed_attempt_right_before_b_is_counted(dup):
    """B(도착 20) 바로 앞 5초에 원장에 안 남은 allow 줄(도착 15)이 있으면 재시도 흔적 1건."""
    ledger = [led(5, "q1", "s1"), led(25, "q1", "s1")]
    audit = [arrive(0), arrive(15), arrive(20)]

    det = detail(dup.analyze(ledger, audit))

    assert det["failed_attempt_within_30s_before_b"] == 1


def hook_line(at, reason, *, identity="demo", bank="personal-demo"):
    return {"ts": iso(at), "identity": identity, "method": "POST", "tool": "context-triggers",
            "attempted_bank": bank, "resolved_bank": bank, "decision": "context-trigger",
            "reason": reason, "context_trigger": {"tool": "Bash", "matched_ids": ["t1"]}}


def test_context_trigger_recalls_are_counted_with_the_ones_followed_by_a_ledger_recall(dup):
    """길목 주입 recall은 원장 밖이다. 끝난 뒤 60초 안에 같은 (identity, bank) 원장 recall이 이어졌는지만 센다."""
    ledger = [led(10, "q1", "s1"), led(100, "q2", "s1")]
    audit = [hook_line(20, "match-empty:t1"),          # 다음 원장 행(100)까지 80초: 이어지지 않음
             hook_line(30, "miss"),                    # recall을 돌리지 않았다: 세지 않음
             hook_line(60, "match:t1"),                # 100까지 40초: 이어짐
             hook_line(95, "recall-failed:t1"),        # 100까지 5초: 이어짐
             hook_line(61, "match:t1", identity="alice", bank="personal-alice")]   # 다른 population

    result = dup.analyze(ledger, audit)

    assert primary(result)["context_trigger"] == {"hook_recalls": 3, "followed_by_ledger_recall_within_60s": 2}


# --- CLI ----------------------------------------------------------------------------------

def write_jsonl(path, rows, junk=False):
    lines = [json.dumps(r) for r in rows]
    if junk:
        lines.insert(1, "{not json")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_cli_prints_aggregates_only_and_leaves_inputs_untouched(dup, tmp_path, capsys):
    ledger, audit = fixed_dataset()
    ledger_path, audit_path = tmp_path / "recall-queries.log", tmp_path / "gateway.log"
    write_jsonl(ledger_path, ledger, junk=True)
    write_jsonl(audit_path, audit)
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in (ledger_path, audit_path)}

    code = dup.main(["--ledger", str(ledger_path), "--audit", str(audit_path)])
    out = capsys.readouterr().out

    assert code == 0
    parsed = json.loads(out)
    assert parsed["input"] == {"ledger_lines": 13, "ledger_skipped": 1,
                               "audit_lines": 15, "audit_skipped": 0}
    assert parsed["populations"][DEMO]["within_60s"] == 2
    assert parsed["period"] == {"ledger_first": iso(5), "ledger_last": iso(4000), "days": 0.0}
    # 쿼리 텍스트·세션 id·결과 id 원문은 출력에 없다
    for secret in ("q1", "q2", "q3", "q4", "q5", "s1", "s2", "s3", "s4", "s5", "m1", "t:a", "t:b"):
        assert f'"{secret}"' not in out
    assert not any(p.name not in ("recall-queries.log", "gateway.log") for p in tmp_path.iterdir())
    after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in (ledger_path, audit_path)}
    assert after == before


def test_cli_until_cuts_later_ledger_rows(dup, tmp_path, capsys):
    ledger, audit = fixed_dataset()
    ledger_path, audit_path = tmp_path / "l.log", tmp_path / "a.log"
    write_jsonl(ledger_path, ledger)
    write_jsonl(audit_path, audit)

    dup.main(["--ledger", str(ledger_path), "--audit", str(audit_path), "--until", iso(1000)])
    parsed = json.loads(capsys.readouterr().out)

    assert parsed["period"]["ledger_last"] == iso(510)
    assert parsed["populations"][DEMO]["rows"] == 10      # r5(4000)만 빠진다


def test_cli_rejects_a_malformed_bound(dup, tmp_path, capsys):
    p = tmp_path / "x.log"
    p.write_text("", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        dup.main(["--ledger", str(p), "--audit", str(p), "--until", "yesterday"])

    assert exc.value.code == 2
