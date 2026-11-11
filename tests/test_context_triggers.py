"""길목 주입 (issue #12, 1차): trigger table + POST /context-triggers/<bank>.

Contracts pinned here:
  1. Table validation is fail-fast: unknown keys, loose keywords (< 3 chars), caps above the
     gateway ceilings (500 tokens / 3 memories), duplicate ids are all load errors.
  2. Matching = tool axis AND every all_of group (any-of, case-insensitive), minus none_of;
     bank-scoped. The committed policies/context-triggers.json parses and its demo row fires
     on aside + demo product text and NOT on unrelated text or on a different tool.
  3. A match runs exactly one upstream recall with the ROW's query (never the task text),
     and the answer is framed "[hindsight 참고, <date> 관측, id:<8>]" per memory, closed by
     the "현장이 우선" footer, capped at 500 estimated tokens, secrets scrubbed.
  4. Miss -> HTTP 200 with an EMPTY body and no upstream call. Recall failure -> empty body
     (fail-open) with an audit reason "recall-failed:<id>".
  5. Auth is the ordinary ACL: unknown token / bank not in ACL -> 403.
  6. The audit line never carries the task text or memory text.
"""
import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import ACL_DATA, UPSTREAM_KEY
from gateway import context_triggers as ct
from gateway.acl import ACL
from gateway.audit import Audit
from gateway.app import create_app

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TASK_TEXT = "dash.democloud.example 에 로그인해서 데모 사이트 캐시를 퍼지해줘 TASK-SECRET-MARKER"
MEMORY_TEXT = "도구 A는 격리세션에서 원격 콘솔 로그인의 2단계 코드를 만들지 못한다 - 코드는 호출자가 만들고 페이지 조작은 도구 A가 한다."


def row(**over):
    base = {
        "id": "demo-dash-login", "bank": "personal-alice", "tools": ["aside"],
        "all_of": [["democloud", "dash.democloud", "2단계"]],
        "recall": {"query": "대시보드 로그인 절차 - 2단계 코드는 누가 만드나"},
    }
    base.update(over)
    return base


def table(*rows):
    return ct.TriggerTable.from_dict({"version": 1, "triggers": list(rows)})


class Upstream:
    """Routes the gate's REST recall; records every request."""

    def __init__(self, results=None, status=200):
        self.results, self.status, self.calls = results, status, []

    def handler(self, request):
        self.calls.append({"path": request.url.path, "headers": dict(request.headers),
                           "body": json.loads(request.content or b"{}")})
        if request.url.path.endswith("/memories/recall"):
            return httpx.Response(self.status, json={"results": self.results or []},
                                  headers={"content-type": "application/json"})
        return httpx.Response(404)


def make(upstream, tmp_path, trig_table):
    http = httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler),
                             base_url="http://upstream", timeout=5.0)
    log = tmp_path / "audit.log"
    app = create_app(acl=ACL.from_dict(ACL_DATA), audit=Audit(path=str(log)),
                     upstream_key=UPSTREAM_KEY, http_client=http, trigger_table=trig_table,
                     notifier=None)
    return TestClient(app), log


def post(client, text, *, tool="aside", bank="personal-alice", token="tok-alice", fmt=None):
    url = f"/context-triggers/{bank}" + ("?format=json" if fmt else "")
    return client.post(url, content=json.dumps({"text": text, "tool": tool}),
                       headers={"content-type": "application/json",
                                "authorization": f"Bearer {token}"})


def memory(mid="8ac1598d-b79c-4421-9453-a88c2857b6f2", text=MEMORY_TEXT, rr=0.98,
           occurred="2026-09-21T08:55:48+00:00", tags=("kind:gotcha",)):
    return {"id": mid, "text": text, "tags": list(tags), "occurred_start": occurred,
            "scores": {"reranker": rr, "final": rr}}


# --- table validation ---------------------------------------------------------------

def test_committed_table_parses_and_demo_row_is_tool_scoped():
    t = ct.TriggerTable.load(os.path.join(ROOT, "tests", "fixtures", "policies", "sample", "context-triggers.json"))
    assert [r.id for r in t.rows] == ["demo-dash-login"]
    demo = t.rows[0]
    assert demo.bank == "personal-demo" and demo.tools == ("aside",)
    assert t.match(bank="personal-demo", tool="aside", text="DemoCloud 로그인 2단계")
    assert t.match(bank="personal-demo", tool="aside", text="데모클라우드 로그인")
    assert not t.match(bank="personal-demo", tool="aside", text="다른 사이트 계정의 2단계 인증 설정 확인")
    assert not t.match(bank="personal-demo", tool="aside", text="메일 로그인 2단계 화면")
    assert demo.max_tokens == 500 and demo.max_memories == 2
    # the table stores a QUERY, never an answer: no seed/path payload in the row
    assert "코드는 누가" in demo.query and "otpauth" not in demo.query


@pytest.mark.parametrize("bad, fragment", [
    (row(extra=1), "unknown key"),
    (row(max_tokens=501), ", 500]"),
    (row(max_tokens=50), ", 500]"),               # below footer + one frame (review P3)
    (row(max_memories=4), "[1, 3]"),
    (row(all_of=[["zz"]]), "at least 3 chars"),
    (row(all_of=[]), "non-empty array"),
    (row(recall={"query": "short"}), "8..500"),
    (row(recall={"query": "ok query here", "answer": "x"}), "unknown key"),
    (row(recall={"query": "ok query here", "tags": ["bad tag"]}), "<prefix>:<slug>"),
    (row(tools=[]), "non-empty array of tool names"),
    (row(id="Bad_ID"), "must match"),
])
def test_table_rejects_malformed_rows(bad, fragment):
    with pytest.raises(ct.TriggerTableError) as exc:
        table(bad)
    assert fragment in str(exc.value)


def test_table_rejects_duplicate_ids_and_bad_version():
    with pytest.raises(ct.TriggerTableError, match="duplicate"):
        table(row(), row())
    with pytest.raises(ct.TriggerTableError, match="version"):
        ct.TriggerTable.from_dict({"version": 2, "triggers": []})


def test_missing_file_is_empty_table(tmp_path):
    assert len(ct.TriggerTable.load(str(tmp_path / "nope.json"))) == 0


# --- matching -----------------------------------------------------------------------

def test_match_requires_tool_axis_and_keyword_group():
    t = table(row())
    hit = lambda text, tool="aside", bank="personal-alice": bool(t.match(bank=bank, tool=tool, text=text))
    assert hit("DemoCloud 대시보드 로그인")            # case-insensitive
    assert hit("dash.democloud.example purge")
    assert hit("다른 사이트 2단계 설정")                # 2단계 is part of the any-of group
    assert not hit("gmail 확인해줘")                    # keyword miss
    assert not hit("democloud purge", tool="bash")      # tool axis miss
    assert not hit("democloud purge", tool=None)
    assert not hit("democloud purge", bank="team-eng")  # bank-scoped


def test_none_of_and_multi_group_all_of():
    t = table(row(all_of=[["democloud"], ["purge", "퍼지"]], none_of=["developers.democloud.example"]))
    assert t.match(bank="personal-alice", tool="aside", text="democloud 퍼지")
    assert not t.match(bank="personal-alice", tool="aside", text="democloud dns")
    assert not t.match(bank="personal-alice", tool="aside", text="developers.democloud.example 에서 purge 문서")


# --- injection builder --------------------------------------------------------------

def test_injection_frames_each_memory_and_closes_with_footer():
    text, used, tokens = ct.build_injection([memory(), memory(mid="22aebc9e-1111", text="[정정+완료] 두 번째", rr=0.9)],
                                            max_memories=2, max_tokens=500)
    assert text.startswith("[hindsight 참고, 2026-09-21 관측, id:8ac1598d]\n" + MEMORY_TEXT)
    assert "[hindsight 참고, 2026-09-21 관측, id:22aebc9e]\n[정정+완료] 두 번째" in text
    assert text.endswith(ct.FOOTER) and "현장이 우선" in ct.FOOTER
    assert used == ["8ac1598d-b79c-4421-9453-a88c2857b6f2", "22aebc9e-1111"]
    assert 0 < tokens <= 500


def test_injection_respects_token_cap_and_memory_cap():
    long = memory(mid="aaaaaaaa-1", text="가" * 900)
    text, used, tokens = ct.build_injection([long, memory(mid="bbbbbbbb-2")], max_memories=2, max_tokens=500)
    assert tokens <= 500 and used == ["aaaaaaaa-1"]           # first memory truncated, second skipped
    assert "…(잘림 - 전문은 recall id:aaaaaaaa)" in text
    three = [memory(mid=f"{i}{i}{i}{i}{i}{i}{i}{i}-x", text=f"m{i}") for i in range(3)]
    _, used, _ = ct.build_injection(three, max_memories=1, max_tokens=500)
    assert len(used) == 1
    assert ct.build_injection([], max_memories=2, max_tokens=500) == ("", [], 0)


def test_injection_scrubs_secret_shapes_visibly():
    leaky = memory(text="시드 JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP 토큰 mt_abcdefghij0123456789 uri otpauth://demo/x?secret=Y")
    text, _, _ = ct.build_injection([leaky], max_memories=1, max_tokens=500)
    assert "JBSWY3DP" not in text and "mt_abcdef" not in text and "otpauth://" not in text
    assert text.count("[redacted]") == 3


def test_estimate_tokens_upper_bounds_real_cl100k_counts():
    """Pinned tiktoken cl100k_base counts (2026-09-23). The old weights (1 / 0.25) returned 496
    for an injection that measured 617, so the 500 cap was not a cap (review P3). The first
    sample was later rewritten as a synthetic sentence and re-measured with tiktoken cl100k_base."""
    pinned = [
        ("[정정+완료] site.demo.example CSS 엣지 캐시 퍼지 성공 - 호출자 Bash가 원격 콘솔 로그인의 2단계 코드"
         "를 6자리로 만들어 도구 A exec에 넘긴다.", 72),
        ('gateway/context_triggers.py:302 _dedup_key(body) -> re.sub(r"[\\s\\W_]+", "", s)[:60]', 30),
    ]
    for text, real in pinned:
        assert ct.estimate_tokens(text) >= real, text
    assert ct.estimate_tokens("") == 0


def test_row_max_tokens_floor_leaves_room_for_a_memory():
    assert ct._min_row_max_tokens() > ct.estimate_tokens(ct.FOOTER) + 50


def test_injection_skips_near_duplicate_units_from_one_retain():
    # measured 2026-09-21: upstream stored "[보강] X - ..." (world) and "[보강] X: ..." (observation)
    a = memory(mid="85d42076-aaaa", text="[보강] 원격 콘솔 로그인 2단계 코드 분담 구조(정본) - 코드는 호출자가 만든다")
    b = memory(mid="200fc756-bbbb", text="[보강] 원격 콘솔 로그인 2단계 코드 분담 구조(정본): 코드는 호출자가 만든다", rr=0.97)
    c = memory(mid="22aebc9e-cccc", text="[정정+완료] 전혀 다른 기억", rr=0.9)
    text, used, _ = ct.build_injection([a, b, c], max_memories=2, max_tokens=500)
    assert used == ["85d42076-aaaa", "22aebc9e-cccc"] and "id:200fc756" not in text


def test_correction_sharing_a_prefix_with_the_stale_memory_is_still_served():
    """Review P3 (2026-09-23): the stale memory ranks first; the [정정] that shares its opening
    must not be dropped as its duplicate (self-healing)."""
    head = ("원격 콘솔 로그인 2단계 코드 분담 구조 - 코드를 만드는 주체와 페이지를 다루는 도구 A, 도구 A 세션"
            " 파일 접근 범위 변경 이후의 역할 분담, 그 변경을 확인한 절차와 재현 조건은 다음과 같다 ")
    stale = memory(mid="aaaaaaaa-1", text="[gotcha] " + head + "도구 A가 직접 만든다", rr=0.97)
    fix = memory(mid="bbbbbbbb-2", text="[정정] " + head + "호출자가 만들고 코드만 넘긴다", rr=0.95)
    _, used, _ = ct.build_injection([stale, fix], max_memories=2, max_tokens=500)
    assert used == ["aaaaaaaa-1", "bbbbbbbb-2"]


# --- endpoint -----------------------------------------------------------------------

def test_match_runs_row_query_recall_and_returns_framed_injection(tmp_path):
    up = Upstream(results=[memory(), memory(mid="22aebc9e-2222", text="[정정+완료] 퍼지 성공 경로", rr=0.95)])
    client, log = make(up, tmp_path, table(row()))
    resp = post(client, TASK_TEXT)
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/plain")
    body = resp.text
    assert body.startswith("[hindsight 참고, 2026-09-21 관측, id:8ac1598d]")
    assert MEMORY_TEXT in body and body.endswith(ct.FOOTER)
    assert resp.headers["x-hc-trigger"] == "demo-dash-login"
    assert resp.headers["x-hc-memory-ids"].startswith("8ac1598d-")
    assert int(resp.headers["x-hc-tokens-est"]) <= 500
    # exactly one upstream call: the row's query, on the bank's REST recall, with the upstream key
    assert len(up.calls) == 1
    call = up.calls[0]
    assert call["path"] == "/v1/default/banks/personal-alice/memories/recall"
    assert call["body"]["query"] == "대시보드 로그인 절차 - 2단계 코드는 누가 만드나"
    assert "TASK-SECRET-MARKER" not in json.dumps(call["body"])
    assert call["headers"]["authorization"] == f"Bearer {UPSTREAM_KEY}"
    # audit: decision + ids only, never the task text or memory text
    lines = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()]
    ev = [l for l in lines if l["decision"] == "context-trigger"]
    assert len(ev) == 1 and ev[0]["reason"] == "match:demo-dash-login"
    assert ev[0]["context_trigger"]["memory_ids"][0].startswith("8ac1598d-")
    assert ev[0]["context_trigger"]["tool"] == "aside" and ev[0]["context_trigger"]["recall_ok"] is True
    raw = log.read_text(encoding="utf-8")
    assert "TASK-SECRET-MARKER" not in raw and "격리세션" not in raw


def test_miss_is_empty_200_without_upstream_call(tmp_path):
    up = Upstream(results=[memory()])
    client, log = make(up, tmp_path, table(row()))
    resp = post(client, "gmail에서 안 읽은 메일 확인")
    assert resp.status_code == 200 and resp.content == b"" and resp.headers["x-hc-trigger"] == ""
    assert up.calls == []
    ev = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()
          if json.loads(l)["decision"] == "context-trigger"]
    assert ev[0]["reason"] == "miss" and ev[0]["context_trigger"]["matched_ids"] == []


def test_json_format_and_recall_failure_fail_open(tmp_path):
    ok = Upstream(results=[memory()])
    client, _ = make(ok, tmp_path, table(row()))
    data = post(client, TASK_TEXT, fmt="json").json()
    assert data["matched"] is True and data["trigger_id"] == "demo-dash-login"
    assert data["injection"].endswith(ct.FOOTER) and data["memory_ids"] and data["recall_ok"] is True

    down = Upstream(status=500)
    client, log = make(down, tmp_path / "down", table(row()))
    resp = post(client, TASK_TEXT)
    assert resp.status_code == 200 and resp.content == b""
    ev = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()
          if json.loads(l)["decision"] == "context-trigger"]
    assert ev[0]["reason"] == "recall-failed:demo-dash-login" and ev[0]["context_trigger"]["recall_ok"] is False


def test_trigger_recall_gives_up_before_the_client_does(tmp_path):
    """#268: the CC hook (curl --max-time 6) and the aside shim (HC_SHIM_TIMEOUT 6) stop waiting
    at 6 s. A recall the gateway still waits on after that is audited as a match that no client
    received, so the trigger path must time out first."""
    assert ct.TRIGGER_RECALL_TIMEOUT_SECONDS < 6
    seen = []

    class Timed(Upstream):
        def handler(self, request):
            seen.append(request.extensions.get("timeout"))
            return super().handler(request)

    client, log = make(Timed(results=[memory()]), tmp_path, table(row()))
    post(client, TASK_TEXT)
    assert seen and set(seen[0].values()) == {ct.TRIGGER_RECALL_TIMEOUT_SECONDS}
    ev = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()
          if json.loads(l)["decision"] == "context-trigger"]
    assert ev[0]["reason"] == "match:demo-dash-login" and ev[0]["context_trigger"]["recall_error"] is None


def test_trigger_recall_deadline_is_wall_clock(tmp_path, monkeypatch):
    """#268 final-review: httpx's timeout bounds each phase (connect / pool / read), not the
    whole call, and the "query too long" retry gets a fresh one - so a recall could still finish
    after the client left. The trigger path wraps the whole recall in one deadline. MockTransport
    applies no httpx timeouts at all, so only that deadline can stop this slow upstream."""
    import asyncio
    monkeypatch.setattr(ct, "TRIGGER_RECALL_TIMEOUT_SECONDS", 0.2)

    class Slow(Upstream):
        async def handler(self, request):
            await asyncio.sleep(0.5)
            return Upstream.handler(self, request)

    up = Slow(results=[memory()])
    client, log = make(up, tmp_path, table(row()))
    data = post(client, TASK_TEXT, fmt="json").json()
    assert data["recall_ok"] is False and data["injection"] == ""
    ev = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()
          if json.loads(l)["decision"] == "context-trigger"]
    assert ev[0]["reason"] == "recall-failed:demo-dash-login"
    assert ev[0]["context_trigger"]["recall_error"] == "timeout"


@pytest.mark.parametrize("failure,expected", [
    ("read-timeout", "timeout"), ("connect", "transport"), ("status-500", "http-500"),
    ("bad-json", "bad-json"), ("bad-shape", "bad-shape")])
def test_trigger_recall_failure_reason_is_audited(tmp_path, failure, expected):
    """#268: a failed recall used to leave only "recall-failed:<id>"; the cause had to be dug out
    of the reranker log. The audit line now names it (a code, never the upstream message)."""
    class Broken(Upstream):
        def handler(self, request):
            if failure == "read-timeout":
                raise httpx.ReadTimeout("slow SECRET-IN-MESSAGE", request=request)
            if failure == "connect":
                raise httpx.ConnectError("down SECRET-IN-MESSAGE", request=request)
            if failure == "status-500":
                return httpx.Response(500, text="boom SECRET-IN-MESSAGE")
            if failure == "bad-json":
                return httpx.Response(200, content=b"not json",
                                      headers={"content-type": "application/json"})
            return httpx.Response(200, json={"results": "not a list"})

    client, log = make(Broken(), tmp_path, table(row()))
    data = post(client, TASK_TEXT, fmt="json").json()
    assert data["recall_ok"] is False and data["injection"] == "" and "recall_error" not in data
    raw = log.read_text(encoding="utf-8")
    ev = [json.loads(l) for l in raw.splitlines() if json.loads(l)["decision"] == "context-trigger"]
    assert ev[0]["reason"] == "recall-failed:demo-dash-login"
    assert ev[0]["context_trigger"]["recall_error"] == expected
    assert "SECRET-IN-MESSAGE" not in raw


def test_auth_is_the_ordinary_acl(tmp_path):
    client, _ = make(Upstream(), tmp_path, table(row()))
    assert post(client, TASK_TEXT, token="tok-nobody").status_code == 403
    assert post(client, TASK_TEXT, bank="personal-bob").status_code == 403      # not in alice's ACL
    assert client.post("/context-triggers/personal-alice", content="{}",
                       headers={"content-type": "application/json",
                                "authorization": "Bearer tok-alice"}).status_code == 400


def test_live_table_catches_demo_spellings_and_normalizes_tool_axis():
    """Review P3 (2026-09-23) against the shipped policies/context-triggers.json."""
    import pathlib
    t = ct.TriggerTable.from_file(str(pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "policies" / "sample" / "context-triggers.json"))
    hit = lambda text, tool="aside": bool(t.match(bank="personal-demo", tool=tool, text=text))
    assert hit("DC 대시보드 2단계 로그인") and hit("데모 클라우드 캐시 퍼지") and hit("dash.democloud.example")
    assert hit("democloud purge", tool=" Aside ")
    assert not hit("메일 2단계") and not hit("dcg 대시보드 설정") and not hit("democloud purge", tool="bash")


def test_table_hot_reloads_on_file_change_and_keeps_last_good_on_broken_edit(tmp_path):
    """Review P3 (2026-09-23): approving a trigger must not need a gateway restart."""
    import os
    path = tmp_path / "t.json"
    path.write_text(json.dumps({"version": 1, "triggers": [row()]}), encoding="utf-8")
    t = ct.TriggerTable.from_file(str(path))
    assert t.refreshed() is t                                  # unchanged file: same object
    second = row(id="demo-two", all_of=[["zzzuniq"]])
    path.write_text(json.dumps({"version": 1, "triggers": [row(), second]}), encoding="utf-8")
    os.utime(path, ns=(1, 1))                                  # force a distinct mtime
    t2 = t.refreshed()
    assert len(t2) == 2
    path.write_text("{broken", encoding="utf-8")
    os.utime(path, ns=(2, 2))
    t3 = t2.refreshed()
    assert t3 is t2 and len(t3) == 2 and t3.refreshed() is t3


# --- review 2nd pass (2026-09-23): hot-reload robustness ------------------------------------

def test_save_landing_mid_read_is_picked_up_by_the_next_request(tmp_path, monkeypatch):
    import builtins
    import os
    path = tmp_path / "t.json"
    path.write_text(json.dumps({"version": 1, "triggers": [row()]}), encoding="utf-8")
    t = ct.TriggerTable.from_file(str(path))
    v2 = json.dumps({"version": 1, "triggers": [row(), row(id="second-row")]})
    os.utime(path, ns=(1, 1))                                   # "changed" -> reload
    real_open = builtins.open

    class SaveAfterRead:                                        # editor saves v2 right after our read
        def __init__(self, fh):
            self.fh = fh
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            self.fh.close()
            path.write_text(v2, encoding="utf-8")
            os.utime(path, ns=(2, 2))
        def fileno(self):
            return self.fh.fileno()
        def read(self):
            return self.fh.read()

    monkeypatch.setattr(builtins, "open", lambda *a, **k: SaveAfterRead(real_open(*a, **k)))
    t2 = t.refreshed()
    monkeypatch.setattr(builtins, "open", real_open)
    assert len(t2) == 1                                         # read the old bytes...
    assert len(t2.refreshed()) == 2                             # ...but not pinned to them


@pytest.mark.parametrize("payload", [b"\xff\xfe broken", ("[" * 100000).encode(),
                                     b'{"version": 1, "x": ' + b"9" * 5000 + b"}",
                                     json.dumps({"version": 1, "triggers": [{"note": "한글"}]}).encode("cp949")])
def test_any_broken_edit_keeps_last_good_table(tmp_path, payload):
    import os
    path = tmp_path / "t.json"
    path.write_text(json.dumps({"version": 1, "triggers": [row()]}), encoding="utf-8")
    t = ct.TriggerTable.from_file(str(path))
    path.write_bytes(payload)
    os.utime(path, ns=(3, 3))
    assert t.refreshed() is t and len(t) == 1


def test_table_file_created_after_startup_is_picked_up(tmp_path):
    path = tmp_path / "later.json"
    t = ct.TriggerTable.load(str(path))
    assert len(t) == 0 and t.refreshed() is t
    path.write_text(json.dumps({"version": 1, "triggers": [row()]}), encoding="utf-8")
    assert len(t.refreshed()) == 1
