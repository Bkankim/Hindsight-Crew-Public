"""이슈 #88: 재발 탐지 초안의 `recall` 블록이 근거 recall의 `budget`·`max_tokens`를 싣는다.

초안이 사람 승인을 거쳐 policies/context-triggers.json 행이 되면 컨텍스트 트리거가 그 recall
블록으로 upstream을 다시 부른다. 초안이 두 인자를 버리면 `budget="high"`·`max_tokens=16384`로
찾은 기억이 기본 인자 재생에서 빠질 수 있다. 확인하는 경계는 셋이다.

  1. 초안(build_draft, 게이트웨이 recall -> gotcha retain -> pending 초안): recall 블록 키.
  2. 트리거 표 파서: 초안이 싣는 키를 받고, 유효하지 않은 값은 거부한다(조용히 고치지 않는다).
  3. 컨텍스트 트리거 엔드포인트: 행의 두 인자가 upstream recall 본문에 실린다. 없으면 본문은
     예전과 같다(바이트 호환).
"""
import json

import pytest

from gateway import context_triggers as ct
from gateway import recurrence
from test_context_triggers import Upstream as TriggerUpstream
from test_context_triggers import make as make_trigger_app
from test_context_triggers import memory, post, row, table
from test_retain_gates import (BANK, GOTCHA_NEW, GOTCHA_OLD, RESCUE_QUERY, Notifier, Upstream, make,
                               mcp, mem, retain_args)

OLD = "fb9fc4e3-dddd"

# (근거 recall 요청에 실린 인자, 초안 recall 블록에 기대하는 인자 키-값)
# 비어 있거나 유효하지 않은 값은 키를 생략한다 - 값을 고쳐 싣지 않는다.
DRAFT_CASES = [
    pytest.param({"budget": "high", "max_tokens": 16384}, {"budget": "high", "max_tokens": 16384}, id="both"),
    pytest.param({"budget": "low"}, {"budget": "low"}, id="budget-only"),
    pytest.param({"max_tokens": 8192}, {"max_tokens": 8192}, id="max-tokens-only"),
    pytest.param({}, {}, id="neither"),
    pytest.param({"budget": None, "max_tokens": None}, {}, id="explicit-null"),
    pytest.param({"budget": "mid", "max_tokens": None}, {"budget": "mid"}, id="max-tokens-null"),
    pytest.param({"budget": None, "max_tokens": 4096}, {"max_tokens": 4096}, id="budget-null"),
    pytest.param({"budget": "turbo", "max_tokens": 8192}, {"max_tokens": 8192}, id="budget-not-an-enum-value"),
    pytest.param({"budget": "high", "max_tokens": True}, {"budget": "high"}, id="max-tokens-bool"),
    pytest.param({"budget": "high", "max_tokens": 2048}, {"budget": "high"}, id="max-tokens-below-floor"),
    # 정수로 정확히 변환되는 값은 int로 바꿔 싣고, budget은 소문자로 정규화한다 (final-review C1)
    pytest.param({"budget": "high", "max_tokens": "16384"}, {"budget": "high", "max_tokens": 16384}, id="max-tokens-int-string"),
    pytest.param({"budget": "high", "max_tokens": 16384.0}, {"budget": "high", "max_tokens": 16384}, id="max-tokens-whole-float"),
    pytest.param({"budget": "High", "max_tokens": 8192}, {"budget": "high", "max_tokens": 8192}, id="budget-capitalized"),
    pytest.param({"budget": "MID"}, {"budget": "mid"}, id="budget-upper"),
    # 변환 불가 값은 고쳐 싣지 않고 생략한다
    pytest.param({"budget": " high", "max_tokens": "16384.5"}, {}, id="budget-padded-and-fractional-string"),
    pytest.param({"budget": "high", "max_tokens": 16384.5}, {"budget": "high"}, id="max-tokens-fractional-float"),
    pytest.param({"budget": "high", "max_tokens": "abc"}, {"budget": "high"}, id="max-tokens-non-numeric-string"),
    pytest.param({"budget": "high", "max_tokens": "+16384"}, {"budget": "high"}, id="max-tokens-signed-string"),
    pytest.param({"budget": "high", "max_tokens": float("nan")}, {"budget": "high"}, id="max-tokens-nan"),
    pytest.param({"budget": "high", "max_tokens": "2048"}, {"budget": "high"}, id="max-tokens-string-below-floor"),
    # recall.max_tokens 상한 32768 (ct.RECALL_MAX_TOKENS_CEILING)
    pytest.param({"max_tokens": 32768}, {"max_tokens": 32768}, id="max-tokens-at-ceiling"),
    pytest.param({"max_tokens": 32769}, {}, id="max-tokens-over-ceiling"),
    pytest.param({"max_tokens": "32769"}, {}, id="max-tokens-string-over-ceiling"),
    pytest.param({"max_tokens": 2 ** 53}, {}, id="max-tokens-huge"),
]


def _event(**args):
    return recurrence.RecallEvent(ts=0.0, identity="alice", bank=BANK, transport="mcp",
                                  query=RESCUE_QUERY, tags=("project:aside",), result_ids=(OLD,), **args)


def _draft(event):
    return recurrence.build_draft(bank=BANK, signal=recurrence.SIGNAL_GOTCHA, new_content=GOTCHA_NEW,
                                  similar=mem(OLD, GOTCHA_OLD, 0.958), rescue=event)


@pytest.mark.parametrize("args, expected", DRAFT_CASES)
def test_draft_recall_block_carries_rescue_arguments(args, expected):
    recall = _draft(_event(**args))["trigger"]["recall"]
    assert recall == {"query": RESCUE_QUERY, "tags": ["project:aside"], "tags_match": "any", **expected}


@pytest.mark.parametrize("args", [{}, {"budget": None, "max_tokens": None}], ids=["omitted", "null"])
def test_draft_without_rescue_arguments_is_byte_identical_to_the_old_shape(args):
    """null 키 생략: 인자가 없는 근거로 만든 초안 recall 블록은 이 이슈 이전과 같은 JSON이다."""
    recall = _draft(_event(**args))["trigger"]["recall"]
    assert json.dumps(recall, sort_keys=True, ensure_ascii=False) == (
        '{"query": "%s", "tags": ["project:aside"], "tags_match": "any"}' % RESCUE_QUERY)


def test_draft_without_evidence_has_no_trigger_and_so_no_arguments():
    d = recurrence.build_draft(bank=BANK, signal=recurrence.SIGNAL_GOTCHA, new_content=GOTCHA_NEW,
                               similar=None, rescue=None)
    assert d["trigger"] is None and d["state"] == "needs-evidence"


# --- 게이트웨이 끝에서 끝까지: recall(두 표면) -> 원장 -> gotcha retain -> pending 초안 ------

def _recall_via(client, surface, args):
    if surface == "mcp":
        resp = mcp(client, "recall", {"query": RESCUE_QUERY, "tags": ["project:aside"], **args}, el_id=7)
    else:
        resp = client.post(f"/v1/default/banks/{BANK}/memories/recall",
                           content=json.dumps({"query": RESCUE_QUERY, "tags": ["project:aside"], **args}),
                           headers={"content-type": "application/json", "authorization": "Bearer tok-alice"})
    assert resp.status_code == 200


# 게이트웨이 끝에서 끝까지 닿지 않는 행은 build_draft 행렬만 본다.
#  - floor(4096) 미만 max_tokens의 MCP recall은 게이트웨이가 거부하고(RULE:recall-max-tokens-floor)
#    그 recall은 원장에도 초안에도 닿지 않는다.
#  - nan은 요청 JSON으로 실을 수 없고 원장도 비유한 실수를 null로 기록한다(_ledger_scalar).
E2E_CASES = [c for c in DRAFT_CASES if c.id not in ("max-tokens-below-floor", "max-tokens-nan")]


@pytest.mark.parametrize("surface", ["mcp", "rest"])
@pytest.mark.parametrize("args, expected", E2E_CASES)
def test_pending_draft_carries_the_arguments_of_the_recall_that_rescued_it(tmp_path, surface, args, expected):
    old = mem(OLD, GOTCHA_OLD, 0.958)
    up = Upstream(gate_results=[old], mcp_recall_results=[old])
    client, _log = make(up, tmp_path, Notifier())
    _recall_via(client, surface, args)
    assert mcp(client, "retain", retain_args(GOTCHA_NEW), el_id=8).status_code == 200
    drafts = [json.loads(l) for l in (tmp_path / "pending.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(drafts) == 1
    recall = drafts[0]["trigger"]["recall"]
    assert {k: v for k, v in recall.items() if k in ("budget", "max_tokens")} == expected
    assert recall["query"] == RESCUE_QUERY and recall["tags"] == ["project:aside"]


# --- 승인 경로: 초안 trigger 객체를 표에 옮기면 파싱되고 재생에 인자가 실린다 -------------

def _recall_body(calls):
    recalls = [c for c in calls if c["path"].endswith("/memories/recall")]
    assert len(recalls) == 1
    return recalls[0]["body"]


@pytest.mark.parametrize("args, expected", DRAFT_CASES)
def test_approved_draft_replays_with_the_same_arguments(tmp_path, args, expected):
    trigger = _draft(_event(**args))["trigger"]
    trigger["bank"] = "personal-alice"
    trigger["tools"] = ["aside"]
    trigger["all_of"] = [["democloud"]]
    trig_table = table(trigger)                                  # 사람이 policies/*.json에 옮긴 것과 같다
    up = TriggerUpstream(results=[memory()])
    client, _log = make_trigger_app(up, tmp_path, trig_table)
    assert post(client, "democloud 로그인").status_code == 200
    body = _recall_body(up.calls)
    assert body["query"] == RESCUE_QUERY
    assert body["max_tokens"] == expected.get("max_tokens", ct.RECALL_MAX_TOKENS)
    assert body.get("budget") == expected.get("budget")
    assert ("budget" in body) == ("budget" in expected)


# --- 컨텍스트 트리거 표·실행 경로 --------------------------------------------------------

def test_row_without_arguments_sends_the_legacy_body_byte_for_byte(tmp_path):
    up = TriggerUpstream(results=[memory()])
    client, _log = make_trigger_app(up, tmp_path, table(row()))
    post(client, "democloud 로그인")
    assert list(_recall_body(up.calls)) == ["query", "max_tokens"]
    assert _recall_body(up.calls)["max_tokens"] == ct.RECALL_MAX_TOKENS


def test_row_arguments_reach_upstream_recall(tmp_path):
    rec = {"query": "대시보드 로그인 절차 - 2단계 코드는 누가 만드나", "tags": ["project:aside"], "tags_match": "all",
           "budget": "high", "max_tokens": 16384}
    up = TriggerUpstream(results=[memory()])
    client, _log = make_trigger_app(up, tmp_path, table(row(recall=rec)))
    post(client, "democloud 로그인")
    body = _recall_body(up.calls)
    assert body == {"query": rec["query"], "max_tokens": 16384, "budget": "high",
                    "tags": ["project:aside"], "tags_match": "all"}


@pytest.mark.parametrize("bad, fragment", [
    ({"budget": "turbo"}, "recall.budget"),
    ({"budget": 3}, "recall.budget"),
    ({"budget": "High"}, "recall.budget"),               # 표는 정규화하지 않는다: 사람이 쓴 값은 그대로 검증
    ({"max_tokens": 2048}, "recall.max_tokens"),
    ({"max_tokens": True}, "recall.max_tokens"),
    ({"max_tokens": "16384"}, "recall.max_tokens"),
    ({"max_tokens": 16384.0}, "recall.max_tokens"),
    ({"max_tokens": 32769}, "recall.max_tokens"),
    ({"max_tokens": 2 ** 53}, "recall.max_tokens"),
    ({"answer": "x"}, "unknown key"),
], ids=lambda v: str(v)[:30])
def test_table_rejects_invalid_recall_arguments(bad, fragment):
    with pytest.raises(ct.TriggerTableError) as exc:
        table(row(recall={"query": "ok query here", **bad}))
    assert fragment in str(exc.value)


def test_recall_max_tokens_does_not_touch_the_row_injection_cap():
    """행의 max_tokens(주입 상한, <= 500)와 recall.max_tokens(upstream 응답 예산)는 별개 키다."""
    t = table(row(recall={"query": "ok query here", "max_tokens": 16384}, max_tokens=300))
    assert t.rows[0].max_tokens == 300
    with pytest.raises(ct.TriggerTableError, match=", 500]"):
        table(row(recall={"query": "ok query here", "max_tokens": 16384}, max_tokens=16384))


def test_table_accepts_recall_arguments_up_to_the_ceiling():
    t = table(row(recall={"query": "ok query here", "max_tokens": ct.RECALL_MAX_TOKENS_CEILING}))
    assert t.rows[0].recall_max_tokens == 32768 == ct.RECALL_MAX_TOKENS_CEILING


@pytest.mark.parametrize("rec_extra", [
    {"budget": None}, {"max_tokens": None}, {"budget": None, "max_tokens": None},
    {"tags": None, "tags_match": None, "budget": None, "max_tokens": None},
], ids=lambda v: ",".join(sorted(v)))
def test_explicit_null_recall_arguments_mean_not_declared(tmp_path, rec_extra):
    """tags·tags_match처럼 recall 블록의 null은 '선언 안 함'이다 (final-review C6): 표가 로드되고
    upstream 본문은 인자 없는 행과 같다."""
    up = TriggerUpstream(results=[memory()])
    client, _log = make_trigger_app(up, tmp_path, table(row(recall={"query": "ok query here", **rec_extra})))
    post(client, "democloud 로그인")
    body = _recall_body(up.calls)
    assert body == {"query": "ok query here", "max_tokens": ct.RECALL_MAX_TOKENS}
    assert list(body) == ["query", "max_tokens"]
