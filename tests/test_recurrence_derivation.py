"""Recurrence gate excludes similar memories derived from the retain's own work (issue #197).

Contracts pinned here:
  1. An above-threshold observation whose source memory body equals the new retain body
     (whitespace collapsed, case folded) drafts nothing; audit reason skip:self-derived.
  2. One whose sources were ALL mentioned within HC_RECURRENCE_WINDOW_MIN drafts nothing
     (skip:sibling-derived). A source outside the window (or only some inside, or dated in
     the future) keeps today's draft.
  3. A failed source lookup keeps today's draft and audits source-lookup-failed.
  4. Only above-threshold observations are looked up; the audit carries no draft id or text.
"""
import json
import time

import httpx
import pytest

from gateway import recurrence
from test_retain_gates import BANK, GOTCHA_NEW, Upstream, audit_lines, make, mcp, mem, retain_args

OTHER = "[gotcha] 다른 작업에서 남긴 기억 - 출처 본문은 새 retain과 다르다"


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(ts))


def obs(mid, rr, fact_type="observation", age=None):
    m = mem(mid, "관측: 도구 A 격리세션 파일 접근 범위가 좁아짐", rr)
    m["fact_type"] = fact_type
    if age is not None:                          # a recall row's own mentioned_at (raw facts)
        m["mentioned_at"] = iso(time.time() - age)
    return m


def src(text, age):
    """`age` seconds before the lookup: mentioned_at is rendered when upstream answers, not at
    collection time (a slow suite would otherwise push a 59-minute source out of the window)."""
    return {"id": f"src-{age}", "text": text, "type": "world", "age": age}


class Detail(Upstream):
    """Adds upstream's GET /v1/default/banks/{bank}/memories/{id} detail view."""

    def __init__(self, details, gate_results, lookup="ok"):
        super().__init__(gate_results=gate_results)
        self.details = details
        self.lookup = lookup
        self.gets = []

    def handler(self, request):
        prefix = f"/v1/default/banks/{BANK}/memories/"
        if request.method == "GET" and request.url.path.startswith(prefix):
            mid = request.url.path[len(prefix):]
            self.gets.append(mid)
            if self.lookup == "error":
                raise httpx.ConnectError("upstream down")
            if self.lookup == "500":
                return httpx.Response(500, json={"detail": "boom"})
            if mid not in self.details:
                return httpx.Response(404, json={"detail": "not found"})
            detail = dict(self.details[mid])
            time.sleep(detail.pop("_delay", 0))          # a slow upstream: spends the lookup budget
            if isinstance(detail.get("source_memories"), list):
                detail["source_memories"] = [
                    {**{k: v for k, v in s.items() if k != "age"}, "mentioned_at": iso(time.time() - s["age"])}
                    if isinstance(s, dict) and "age" in s else s
                    for s in detail["source_memories"]]
            return httpx.Response(200, json={"id": mid, **detail})
        return super().handler(request)


HOUR = 3600.0
SELF_BODY = "  " + GOTCHA_NEW.upper().replace(" ", "\n ") + " "     # same body, re-wrapped and re-cased


@pytest.mark.parametrize("case, sources, lookup, drafted, reason", [
    # completion criterion (1): source body == new retain body -> no draft (even 3 days old)
    ("self-derived", [src(SELF_BODY, 3 * 24 * HOUR)], "ok", False, "skip:self-derived"),
    ("self-derived-among-others", [src(OTHER, 5 * HOUR), src(GOTCHA_NEW, 60)], "ok", False,
     "skip:self-derived"),
    # coordinator extension: every source inside the window -> same work, no draft
    ("sibling-in-window", [src(OTHER, 10 * 60), src(OTHER + " 2", 59 * 60)], "ok", False,
     "skip:sibling-derived"),
    # completion criterion (2): derived from other memories -> draft as today
    ("other-outside-window", [src(OTHER, 2 * HOUR)], "ok", True, None),
    ("partly-in-window", [src(OTHER, 10 * 60), src(OTHER + " 2", 11 * HOUR)], "ok", True, None),
    ("future-dated-source", [src(OTHER, -10 * 60)], "ok", True, None),
    ("no-sources", [], "ok", True, None),
    # completion criterion (3): lookup failure -> draft as today + audit the failure
    ("lookup-500", [src(GOTCHA_NEW, 60)], "500", True, "source-lookup-failed"),
    ("lookup-connect-error", [src(GOTCHA_NEW, 60)], "error", True, "source-lookup-failed"),
    ("lookup-404", None, "ok", True, "source-lookup-failed"),
])
def test_gotcha_recurrence_source_derivation(tmp_path, case, sources, lookup, drafted, reason):
    details = {} if sources is None else {"obs-1": {"type": "observation", "source_memories": sources}}
    up = Detail(details, gate_results=[obs("obs-1", 0.95)], lookup=lookup)
    client, log = make(up, tmp_path)
    resp = mcp(client, "retain", retain_args(GOTCHA_NEW))
    assert resp.status_code == 200
    assert up.gets == ["obs-1"]                                   # one lookup, the candidate only
    pending = tmp_path / "pending.jsonl"
    drafts = [json.loads(l) for l in pending.read_text(encoding="utf-8").splitlines()] if pending.exists() else []
    assert len(drafts) == (1 if drafted else 0)
    if drafted:
        assert drafts[0]["evidence"]["similar_memory_id"] == "obs-1"
    reasons = [l["reason"] for l in audit_lines(log, "recurrence")]
    if reason is None:
        assert reasons == ["draft:gotcha-recurrence"]
    elif drafted:
        assert reasons == [reason, "draft:gotcha-recurrence"]
    else:
        assert reasons == [reason]
    if reason is not None:
        row = audit_lines(log, "recurrence")[0]
        assert row["recurrence"]["similar_memory_id"] == "obs-1"
        assert "draft_id" not in json.dumps(row)                 # reason key only, no draft id
    raw = log.read_text(encoding="utf-8")
    assert "격리세션" not in raw and "다른 작업" not in raw          # audit stays text-free


def test_excluded_candidate_falls_through_to_next_and_raw_facts_are_not_looked_up(tmp_path):
    details = {"obs-1": {"source_memories": [src(GOTCHA_NEW, 60)]}}
    up = Detail(details, gate_results=[obs("obs-1", 0.97), obs("fact-2", 0.93, fact_type="world"),
                                       obs("obs-low", 0.80)])
    client, log = make(up, tmp_path)
    mcp(client, "retain", retain_args(GOTCHA_NEW))
    assert up.gets == ["obs-1"]                    # world fact and below-threshold obs: no GET
    drafts = [json.loads(l) for l in (tmp_path / "pending.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [d["evidence"]["similar_memory_id"] for d in drafts] == ["fact-2"]
    assert [l["reason"] for l in audit_lines(log, "recurrence")] == [
        "skip:self-derived", "draft:gotcha-recurrence"]


def test_all_candidates_excluded_drafts_nothing_and_lookups_are_capped(tmp_path):
    ids = [f"obs-{i}" for i in range(recurrence.MAX_SOURCE_LOOKUPS + 1)]
    details = {i: {"source_memories": [src(OTHER, 60)]} for i in ids}
    up = Detail(details, gate_results=[obs(i, 0.99 - n / 100) for n, i in enumerate(ids)])
    client, log = make(up, tmp_path)
    mcp(client, "retain", retain_args(GOTCHA_NEW))
    assert up.gets == ids[:recurrence.MAX_SOURCE_LOOKUPS]
    drafts = [json.loads(l) for l in (tmp_path / "pending.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [d["evidence"]["similar_memory_id"] for d in drafts] == [ids[-1]]   # past the cap: today's behaviour
    assert [l["reason"] for l in audit_lines(log, "recurrence")] == (
        ["skip:sibling-derived"] * recurrence.MAX_SOURCE_LOOKUPS
        + ["lookup-cap-reached", "draft:gotcha-recurrence"])


@pytest.mark.parametrize("raw_age, drafted, reasons", [
    # final-review #1: the sibling's raw fact is judged on its own mentioned_at, no GET
    (10 * 60, False, ["skip:self-derived", "skip:sibling-derived"]),
    (2 * HOUR, True, ["skip:self-derived", "draft:gotcha-recurrence"]),
    (None, True, ["skip:self-derived", "draft:gotcha-recurrence"]),   # no mentioned_at in the row
])
def test_raw_fact_after_skipped_observation_is_judged_on_itself(tmp_path, raw_age, drafted, reasons):
    details = {"obs-1": {"source_memories": [src(GOTCHA_NEW, 60)]}}
    up = Detail(details, gate_results=[obs("obs-1", 0.97), obs("fact-2", 0.94, "world", age=raw_age)])
    client, log = make(up, tmp_path)
    mcp(client, "retain", retain_args(GOTCHA_NEW))
    assert up.gets == ["obs-1"]
    pending = tmp_path / "pending.jsonl"
    drafts = [json.loads(l) for l in pending.read_text(encoding="utf-8").splitlines()] if pending.exists() else []
    assert [d["evidence"]["similar_memory_id"] for d in drafts] == (["fact-2"] if drafted else [])
    assert [l["reason"] for l in audit_lines(log, "recurrence")] == reasons


@pytest.mark.parametrize("case, detail, reason", [
    # final-review #2: a detail that cannot be judged keeps the draft and is audited
    ("source-memories-not-a-list", {"source_memories": 5}, "source-lookup-failed"),
    ("source-memories-a-dict", {"source_memories": {"id": "x"}}, "source-lookup-failed"),
    ("client-raises", "raise", "source-lookup-failed"),
    # final-review #4: an invalidated source is missing from source_memories but kept in the ids
    ("ids-longer-than-sources", {"source_memories": [src(OTHER, 60)],
                                 "source_memory_ids": ["src-60", "gone"]}, "source-incomplete"),
    ("non-dict-source", {"source_memories": [src(OTHER, 60), "junk"]}, "source-incomplete"),
])
def test_unjudgeable_detail_keeps_draft_and_contradiction_warning(tmp_path, monkeypatch, case, detail, reason):
    from test_retain_gates import BLOCKED, PRECEDENT, mcp_texts
    if detail == "raise":
        async def boom(*a, **k):
            raise RuntimeError("unexpected client failure")
        monkeypatch.setattr(recurrence, "upstream_memory", boom)
        detail = {}
    precedent = mem("22aebc9e-aaaa", PRECEDENT, 0.95, tags=("kind:completion",))
    up = Detail({"obs-1": detail}, gate_results=[precedent, obs("obs-1", 0.95)])
    client, log = make(up, tmp_path)
    resp = mcp(client, "retain", retain_args(BLOCKED))       # negative label AND kind:gotcha
    assert resp.status_code == 200
    assert any(t.startswith("[gateway 경고]") for t in mcp_texts(resp))   # warning not lost
    drafts = [json.loads(l) for l in (tmp_path / "pending.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [d["evidence"]["similar_memory_id"] for d in drafts] == ["obs-1"]
    assert [l["reason"] for l in audit_lines(log, "recurrence")] == [reason, "draft:gotcha-recurrence"]


def test_partial_source_list_still_proves_self_derived(tmp_path):
    details = {"obs-1": {"source_memories": [src(GOTCHA_NEW, 3 * HOUR)], "source_memory_ids": ["a", "b"]}}
    up = Detail(details, gate_results=[obs("obs-1", 0.95)])
    client, log = make(up, tmp_path)
    mcp(client, "retain", retain_args(GOTCHA_NEW))
    assert not (tmp_path / "pending.jsonl").exists()
    assert [l["reason"] for l in audit_lines(log, "recurrence")] == ["skip:self-derived"]


def test_lookup_budget_spent_keeps_remaining_candidates_without_lookup(tmp_path, monkeypatch):
    # final-review #5: one item's lookups share one deadline
    monkeypatch.setattr(recurrence, "SOURCE_LOOKUP_BUDGET_SECONDS", 0.05)
    details = {"obs-1": {"source_memories": [src(OTHER, 60)], "_delay": 0.1},
               "obs-2": {"source_memories": [src(OTHER, 60)]}}
    up = Detail(details, gate_results=[obs("obs-1", 0.97), obs("obs-2", 0.95)])
    client, log = make(up, tmp_path)
    mcp(client, "retain", retain_args(GOTCHA_NEW))
    assert up.gets == ["obs-1"]                            # obs-2 never looked up
    drafts = [json.loads(l) for l in (tmp_path / "pending.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [d["evidence"]["similar_memory_id"] for d in drafts] == ["obs-2"]
    assert [l["reason"] for l in audit_lines(log, "recurrence")] == [
        "skip:sibling-derived", "lookup-deadline-reached", "draft:gotcha-recurrence"]


def test_default_lookup_budget_is_four_seconds():
    assert recurrence.SOURCE_LOOKUP_BUDGET_SECONDS == 4.0


NOW = 1_790_000_000.0


@pytest.mark.parametrize("ages, expected", [
    ([0.0], "skip:sibling-derived"),
    ([HOUR], "skip:sibling-derived"),                 # window boundary is inclusive
    ([HOUR + 1], None),
    ([60.0, HOUR + 1], None),                                   # only some sources inside
    ([-1.0], None),                                             # dated after now
    ([None], None),                                             # unreadable time
])
def test_derivation_window_boundaries(ages, expected):
    sources = [{"text": OTHER, "mentioned_at": None if a is None else iso(NOW - a)} for a in ages]
    assert recurrence.derivation(GOTCHA_NEW, {"source_memories": sources}, now=NOW, window=HOUR) == expected


def test_window_default_and_threshold_default_are_unchanged(monkeypatch):
    monkeypatch.delenv("HC_RECURRENCE_WINDOW_MIN", raising=False)
    monkeypatch.delenv("HC_RECURRENCE_MIN_RERANK", raising=False)
    assert recurrence.window_seconds() == 60 * 60 and recurrence.min_rerank() == 0.92
