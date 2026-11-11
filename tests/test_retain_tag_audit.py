"""Issue #37: 허용된 retain마다 "retain-tags" 감사 줄 - 보낸 태그, 저장된 태그, MCP client 이름.

모든 런타임이 토큰 하나를 쓰므로 감사가 태그가 고쳐졌을 때만 남으면 출처를 가를 수 없었다.
이 파일은 네 흐름을 핀한다:
  ① MCP initialize -> retain: 세션 id에 묶인 clientInfo.name과 두 태그 목록
  ② REST 다건: items[i]마다 한 줄
  ③ 거부 retain: 기존 enforce-reject만, retain-tags 줄 없음
  ④ 감사 줄 어디에도 content 문자열이 없음
"""
import json
import os

import pytest
from fastapi.testclient import TestClient

from conftest import call
from gateway import app as gateway_app
from gateway.bank_policies import BankPolicies

REPO_ROOT = os.path.dirname(os.path.dirname(__file__))
REPO_POLICY_FILE = os.path.join(REPO_ROOT, "tests", "fixtures", "policies", "sample", "bank-policies.json")

BANK = "personal-alice"
TOKEN = "tok-alice"
SECRET_CONTENT = "[decision] 감사줄에는 절대 남으면 안 되는 본문 SECRET-BODY-37"


def strict_client(make_app):
    return TestClient(make_app(bank_policies=BankPolicies.from_file(REPO_POLICY_FILE)))


def audit_entries(client):
    with open(client.app.state._audit_path, encoding="utf-8") as fh:
        return [json.loads(ln) for ln in fh if ln.strip()]


def tag_lines(client):
    return [e for e in audit_entries(client) if e["decision"] == "retain-tags"]


def initialize(client, name="claude-code", session_header=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": name, "version": "9.9"}}}
    if name is None:
        del body["params"]["clientInfo"]
    return call(client, BANK, TOKEN, raw=body, headers=session_header)


def mcp_retain(client, tags, session="sess-upstream-1", tool="retain", content=SECRET_CONTENT):
    args = {"content": content}
    if tags is not None:
        args["tags"] = tags
    headers = {"mcp-session-id": session} if session else None
    return call(client, BANK, TOKEN, name=tool, arguments=args, headers=headers)


def rest_post(client, items):
    return client.post(f"/v1/default/banks/{BANK}/memories",
                       content=json.dumps({"items": items, "async": False}),
                       headers={"content-type": "application/json",
                                "authorization": f"Bearer {TOKEN}"})


# ============================ ① MCP initialize -> retain ============================

def test_mcp_retain_line_carries_client_name_and_both_tag_lists(make_app):
    c = strict_client(make_app)
    assert initialize(c, name="codex-mcp-client").status_code == 200

    r = mcp_retain(c, ["Kind:Gotcha", "project:hindsight-crew"])
    assert r.status_code == 200

    (line,) = tag_lines(c)
    assert line["reason"] == "tags-rewritten"
    assert line["tool"] == "retain"
    assert line["identity"] == "alice"
    assert line["resolved_bank"] == BANK
    assert line["retain_tags"] == {
        "transport": "mcp",
        "client": "codex-mcp-client",
        "sent": ["Kind:Gotcha", "project:hindsight-crew"],
        "stored": ["kind:gotcha", "project:hindsight-crew"],
    }


def test_client_that_normalized_first_still_leaves_a_line(make_app):
    """#35에서 막힌 사각지대: 교정이 없던 retain도 무엇이 들어왔는지 남는다."""
    c = strict_client(make_app)
    initialize(c)

    mcp_retain(c, ["kind:decision", "project:hindsight-crew"])

    (line,) = tag_lines(c)
    assert line["reason"] == "tags-unchanged"
    assert line["retain_tags"]["sent"] == line["retain_tags"]["stored"] == [
        "kind:decision", "project:hindsight-crew"]
    assert not [e for e in audit_entries(c) if e["decision"] == "enforce-normalize"]


def test_sync_retain_is_recorded_too(make_app):
    c = strict_client(make_app)
    initialize(c)
    mcp_retain(c, ["kind:decision"], tool="sync_retain")
    assert [e["tool"] for e in tag_lines(c)] == ["sync_retain"]


def test_unknown_session_or_missing_client_info_logs_unknown(make_app):
    c = strict_client(make_app)
    initialize(c, name="claude-code")

    mcp_retain(c, ["kind:decision"], session="sess-never-initialized")   # 모르는 세션
    mcp_retain(c, ["kind:decision"], session=None)                       # 세션 헤더 없음

    assert [e["retain_tags"]["client"] for e in tag_lines(c)] == ["unknown"] * 2


def test_initialize_without_client_info_logs_unknown(make_app):
    c = strict_client(make_app)
    initialize(c, name=None)                                            # clientInfo 없음
    mcp_retain(c, ["kind:decision"])
    assert [e["retain_tags"]["client"] for e in tag_lines(c)] == ["unknown"]


def test_nameless_reinitialize_does_not_erase_a_known_client_name(make_app):
    c = strict_client(make_app)
    initialize(c, name="claude-code")
    initialize(c, name=None)                                            # 같은 세션 id, 이름 없음

    mcp_retain(c, ["kind:decision"])

    (line,) = tag_lines(c)
    assert line["retain_tags"]["client"] == "claude-code"


def test_non_string_client_name_is_unknown_and_long_or_control_names_are_cleaned(make_app):
    c = strict_client(make_app)
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"clientInfo": {"name": {"x": 1}}}}
    call(c, BANK, TOKEN, raw=body)
    mcp_retain(c, ["kind:decision"])

    body["params"]["clientInfo"]["name"] = "a\nb\x00" + "z" * 200
    call(c, BANK, TOKEN, raw=body)
    mcp_retain(c, ["kind:decision"])

    first, second = [e["retain_tags"]["client"] for e in tag_lines(c)]
    assert first == "unknown"
    assert second.startswith("ab") and len(second) == 64 and "\n" not in second


def test_client_streaming_initialize_path_also_binds(make_app):
    """loose 뱅크는 initialize instructions가 없어 스트리밍 passthrough를 탄다 - 거기서도 묶여야 한다."""
    c = TestClient(make_app())            # 정책 없음(defaults-only)
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"clientInfo": {"name": "yew"}}}
    call(c, "personal-alice", TOKEN, raw=body)
    call(c, "personal-alice", TOKEN, name="retain", arguments={"content": "x"},
         headers={"mcp-session-id": "sess-upstream-1"})

    (line,) = tag_lines(c)
    assert line["retain_tags"] == {"transport": "mcp", "client": "yew", "sent": [], "stored": []}


def test_client_registry_is_bounded_lru():
    reg = gateway_app._ClientSessions(limit=3)
    for i in range(5):
        reg.bind(f"s{i}", f"c{i}")
    assert len(reg) == 3
    assert reg.lookup("s0") == reg.lookup("s1") == gateway_app.CLIENT_UNKNOWN
    assert reg.lookup("s4") == "c4"
    reg.bind("s2", "c2b")            # 재바인딩은 최근 사용으로 갱신
    reg.bind("s5", "c5")
    assert reg.lookup("s2") == "c2b" and reg.lookup("s3") == gateway_app.CLIENT_UNKNOWN
    assert reg.lookup(None) == gateway_app.CLIENT_UNKNOWN


def test_lookup_refreshes_recency_so_a_busy_session_is_not_evicted():
    reg = gateway_app._ClientSessions(limit=2)
    reg.bind("busy", "claude-code")
    reg.bind("idle", "other")
    assert reg.lookup("busy") == "claude-code"      # 계속 retain하는 세션
    reg.bind("new", "third")                        # idle이 밀려나야 한다
    assert reg.lookup("busy") == "claude-code"
    assert reg.lookup("idle") == gateway_app.CLIENT_UNKNOWN


def test_tags_on_a_prefixless_bank_are_logged_as_capped_strings_only(make_app):
    """prefix 없는 뱅크는 tags에 임의 JSON이 통과한다 - 감사 줄이 본문 통로가 되면 안 된다."""
    c = TestClient(make_app())            # 정책 없음(defaults-only): 태그 문법 검사 없음
    mcp_retain(c, [{"note": "SECRET-BODY-37"}, "x" * 1000, "kind:ok"])

    (line,) = tag_lines(c)
    assert line["retain_tags"]["sent"] == ["x" * 256, "kind:ok"]
    assert line["retain_tags"]["stored"] == ["x" * 256, "kind:ok"]
    with open(c.app.state._audit_path, encoding="utf-8") as fh:
        assert "SECRET-BODY-37" not in fh.read()


# ============================ ② REST 다건 ============================

def test_rest_multi_item_request_gets_one_line_per_item(make_app):
    c = strict_client(make_app)
    items = [
        {"content": "[decision] 첫째", "tags": ["Kind:Gotcha"]},
        {"content": "[decision] 둘째", "tags": ["kind:decision", "project:hindsight-crew"]},
        {"content": "[decision] 셋째", "tags": ["Project:Hindsight Crew", "kind:rule"]},
    ]
    assert rest_post(c, items).status_code == 200

    lines = tag_lines(c)
    assert [l["retain_tags"]["transport"] for l in lines] == ["rest"] * 3
    assert [l["retain_tags"]["sent"] for l in lines] == [i["tags"] for i in items]
    assert [l["retain_tags"]["stored"] for l in lines] == [
        ["kind:gotcha"],
        ["kind:decision", "project:hindsight-crew"],
        ["project:hindsight-crew", "kind:rule"],
    ]
    assert [l["reason"] for l in lines] == ["tags-rewritten", "tags-unchanged", "tags-rewritten"]
    assert all("client" not in l["retain_tags"] for l in lines)
    assert all(l["method"] == "POST" and l["tool"].endswith("/memories") for l in lines)


# ============================ ③ 거부 retain ============================

def test_rejected_mcp_retain_keeps_enforce_reject_and_writes_no_tag_line(make_app):
    c = strict_client(make_app)
    initialize(c)

    r = mcp_retain(c, None)                    # tags_required 위반
    assert r.json()["result"]["isError"] is True

    assert tag_lines(c) == []
    assert [e["reason"] for e in audit_entries(c) if e["decision"] == "enforce-reject"] == [
        "rule-violation:RULE:tags-required"]


def test_rejected_rest_batch_writes_no_tag_line_even_for_valid_siblings(make_app):
    c = strict_client(make_app)
    r = rest_post(c, [{"content": "[decision] 정상", "tags": ["kind:decision"]},
                      {"content": "[decision] 태그 없음"}])
    assert r.status_code == 422

    assert tag_lines(c) == []
    assert [e["decision"] for e in audit_entries(c)].count("enforce-reject") == 1


# ============================ ④ content 미기록 ============================

def test_no_audit_line_contains_content_text(make_app):
    c = strict_client(make_app)
    initialize(c)
    mcp_retain(c, ["Kind:Gotcha"])
    mcp_retain(c, None)                        # 거부 경로
    rest_post(c, [{"content": SECRET_CONTENT, "tags": ["kind:decision"]}])
    rest_post(c, [{"content": SECRET_CONTENT}])   # 거부 경로

    with open(c.app.state._audit_path, encoding="utf-8") as fh:
        raw = fh.read()
    assert "SECRET-BODY-37" not in raw
    assert "감사줄에는" not in raw
    assert len(tag_lines(c)) == 2


# ============================ ⑤ 태그 개수 상한 (issue #99) ============================

TAG_CAP = 64        # gateway_app._TAG_LOG_COUNT_MAX와 같은 값 - 상수를 바꾸면 이 테스트가 알려야 한다


def many_tags(n):
    """규칙을 통과하는 n개 태그: 첫 줄 kind(필수 prefix) + topic:t0..."""
    return ["kind:decision"] + [f"topic:t{i}" for i in range(n - 1)]


def upstream_tags(recorder, surface):
    """upstream 모의가 실제로 받은 첫 retain 항목의 태그 목록 - 감사 상한은 이걸 건드리면 안 된다."""
    if surface == "mcp":
        (call_,) = [c for c in recorder.calls if c["path"].startswith("/mcp")
                    and (c["body"] or {}).get("method") == "tools/call"]
        return call_["body"]["params"]["arguments"]["tags"]
    (call_,) = [c for c in recorder.calls if c["path"].endswith("/memories")]
    return call_["body"]["items"][0]["tags"]


def send_retain(c, surface, tags):
    if surface == "mcp":
        r = mcp_retain(c, tags)
    else:
        r = rest_post(c, [{"content": SECRET_CONTENT, "tags": tags}])
    assert r.status_code == 200
    (line,) = tag_lines(c)
    return line["retain_tags"]


@pytest.mark.parametrize("surface", ["mcp", "rest"])
@pytest.mark.parametrize("n", [5000, 65, 64, 63])
def test_audit_tag_lists_are_capped_with_count_and_omitted_while_upstream_keeps_all(
        make_app, recorder, surface, n):
    c = strict_client(make_app)
    tags = many_tags(n)

    info = send_retain(c, surface, tags)

    assert len(upstream_tags(recorder, surface)) == n           # upstream 저장분은 그대로
    assert len(info["sent"]) == len(info["stored"]) == min(n, TAG_CAP)
    assert info["sent"] == tags[:TAG_CAP] and info["stored"] == tags[:TAG_CAP]
    if n <= TAG_CAP:                                            # 경계: 정확히 64개면 키 추가 없음
        assert set(info) == {"transport", "sent", "stored"} | ({"client"} if surface == "mcp" else set())
    else:
        assert info["sent_count"] == info["stored_count"] == n
        assert info["sent_omitted"] == info["stored_omitted"] == n - TAG_CAP


@pytest.mark.parametrize("surface", ["mcp", "rest"])
def test_capped_line_stays_small_and_leaves_sent_and_stored_independent(make_app, recorder, surface):
    """sent는 대문자라 교정되는 5000개, stored는 교정본 - 각각 따로 세고 따로 자른다."""
    c = strict_client(make_app)
    sent = ["kind:decision"] + [f"Topic:T{i}" for i in range(4999)]

    info = send_retain(c, surface, sent)

    assert info["sent"][1] == "Topic:T0" and info["stored"][1] == "topic:t0"
    assert info["sent_count"] == info["stored_count"] == 5000
    assert len(upstream_tags(recorder, surface)) == 5000
    (line,) = tag_lines(c)
    assert len(json.dumps(line)) < 8 * 1024                     # 감사 줄 한 건의 크기가 유한


def test_cap_counts_string_tags_only_on_a_prefixless_bank(make_app):
    """비문자열은 원래도 기록하지 않으므로 개수·생략 건수에도 넣지 않는다."""
    c = TestClient(make_app())            # 정책 없음: 태그 문법 검사 없음
    mcp_retain(c, [{"note": "SECRET-BODY-37"}] * 10 + [f"t{i}" for i in range(TAG_CAP + 6)])

    (line,) = tag_lines(c)
    info = line["retain_tags"]
    assert len(info["sent"]) == TAG_CAP
    assert info["sent_count"] == TAG_CAP + 6 and info["sent_omitted"] == 6


# ============================ ⑥ enforce-normalize 줄의 교정 쌍 상한 (issue #103) ============================

def normalize_lines(client):
    return [e for e in audit_entries(client) if e["decision"] == "enforce-normalize"]


def uppercase_pairs_tags(m):
    """kind:decision 하나 + 대문자라 교정되는 m개 -> 교정 쌍은 정확히 m개."""
    return ["kind:decision"] + [f"Topic:T{i}" for i in range(m)]


def send_normalizing_retain(c, surface, tags):
    if surface == "mcp":
        r = mcp_retain(c, tags)
    else:
        r = rest_post(c, [{"content": SECRET_CONTENT, "tags": tags}])
    assert r.status_code == 200
    (line,) = normalize_lines(c)
    return line, r


@pytest.mark.parametrize("surface", ["mcp", "rest"])
@pytest.mark.parametrize("m", [5000, 65, 64, 63])
def test_enforce_normalize_line_caps_pairs_with_count_and_omitted_while_upstream_keeps_all(
        make_app, recorder, surface, m):
    c = strict_client(make_app)
    tags = uppercase_pairs_tags(m)

    line, _ = send_normalizing_retain(c, surface, tags)

    expected_stored = ["kind:decision"] + [f"topic:t{i}" for i in range(m)]
    assert upstream_tags(recorder, surface) == expected_stored          # upstream으로 가는 태그는 수정 전과 같다
    pairs = line["reason"].removeprefix("tag-normalize:").split(",")
    assert pairs == [f"Topic:T{i}->topic:t{i}" for i in range(min(m, TAG_CAP))]
    if m <= TAG_CAP:                                                    # 경계: 정확히 64쌍이면 키 추가 없음
        assert "normalize_tags" not in line
    else:
        assert line["normalize_tags"] == {"normalized_count": m, "normalized_omitted": m - TAG_CAP}
    assert len(json.dumps(line)) < 8 * 1024


@pytest.mark.parametrize("surface", ["mcp", "rest"])
def test_enforce_normalize_line_size_does_not_grow_with_tag_count(make_app, recorder, surface):
    c = strict_client(make_app)                       # 같은 감사 파일에 1000개, 5000개 순서로 쌓는다
    for m in (1000, 5000):
        if surface == "mcp":
            assert mcp_retain(c, uppercase_pairs_tags(m)).status_code == 200
        else:
            assert rest_post(c, [{"content": SECRET_CONTENT,
                                  "tags": uppercase_pairs_tags(m)}]).status_code == 200
    small, big = normalize_lines(c)
    sizes = [len(json.dumps(small)), len(json.dumps(big))]

    assert max(sizes) < 8 * 1024
    assert abs(sizes[0] - sizes[1]) < 64                                # 개수 자릿수만 다르다


@pytest.mark.parametrize("surface", ["mcp", "rest"])
def test_enforce_normalize_line_caps_a_single_huge_tag_pair(make_app, surface):
    c = strict_client(make_app)
    huge = "Topic:" + "A" * 100_000

    line, _ = send_normalizing_retain(c, surface, ["kind:decision", huge])

    assert len(json.dumps(line)) < 2 * 1024


@pytest.mark.parametrize("surface", ["mcp", "rest"])
def test_enforce_normalize_disclosure_to_the_caller_still_lists_every_rewrite(make_app, surface):
    """감사 줄만 자른다 - 호출자에게 가는 교정 공지는 그대로 전부."""
    c = strict_client(make_app)
    m = 200

    _, r = send_normalizing_retain(c, surface, uppercase_pairs_tags(m))

    assert "Topic:T199" in r.text and "topic:t199" in r.text            # 상한(64)을 넘는 마지막 쌍까지


# ===== 이슈 #127-2: 감사 줄 client는 요청 도착 시점에 고정한다 (retain 게이트 대기 중 재바인딩 무시) =====
# 원장(#107)은 도착 시점 값을 쓰는데 감사 줄만 게이트 await 이후에 lookup해 같은 요청이 둘로 갈렸다.
# 도착 시점 값은 `arrived_client or "unknown"`: 원장 None(이름 없음)과 감사 "unknown"의 구분은 유지한다.

import hashlib

import httpx

from gateway.acl import ACL
from gateway.app import CLIENT_UNKNOWN, create_app
from gateway.audit import Audit
from gateway.recurrence import RecallLedger

from conftest import ACL_DATA, UPSTREAM_KEY

GATE_SESSION = "sess-upstream-1"        # 모의 upstream이 initialize 응답에 실어 주는 세션 id
GATED_TAGS = ["project:hindsight-crew", "kind:gotcha"]   # kind:gotcha -> 게이트가 upstream recall을 기다린다


def _fingerprint(session):
    return hashlib.sha256(session.encode()).hexdigest()


def _rebind(sessions):
    sessions.bind(_fingerprint(GATE_SESSION), "codex")


def _evict(sessions):
    for n in range(gateway_app._CLIENT_SESSIONS_MAX):
        sessions.bind(_fingerprint(f"other-{n}"), "filler")


@pytest.fixture
def gated(tmp_path):
    """게이트의 upstream recall 도중(= `await retain_gates.evaluate_mcp`) 실행할 훅을 거는 게이트웨이."""
    state = {"hook": None}

    def upstream(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/memories/recall"):
            if state["hook"] is not None:
                state["hook"]()
            return httpx.Response(200, json={"results": []})
        encoded = json.dumps({"results": [{"id": "mem-1", "text": "x"}]})
        result = {"content": [{"type": "text", "text": encoded}],
                  "structuredContent": {"result": encoded}, "isError": False}
        return httpx.Response(
            200, content=json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}).encode("utf-8"),
            headers={"content-type": "application/json", "mcp-session-id": GATE_SESSION})

    http = httpx.AsyncClient(transport=httpx.MockTransport(upstream), base_url="http://upstream", timeout=5.0)
    audit_path = tmp_path / "audit.log"
    ledger_path = tmp_path / "ledger.jsonl"
    app = create_app(acl=ACL.from_dict(ACL_DATA), audit=Audit(path=str(audit_path)),
                     upstream_key=UPSTREAM_KEY, http_client=http,
                     bank_policies=BankPolicies.from_file(REPO_POLICY_FILE),
                     recall_ledger=RecallLedger(str(ledger_path)))
    app.state._audit_path = str(audit_path)
    return TestClient(app), state, ledger_path


@pytest.mark.parametrize("mutate", [_rebind, _evict], ids=["reinitialize-other-name", "lru-eviction"])
def test_audit_client_is_the_arrival_name_not_the_one_after_the_gate_await(gated, mutate):
    c, state, _ = gated
    sessions = c.app.state.mcp_clients
    initialize(c, name="claude-code")
    state["hook"] = lambda: mutate(sessions)

    mcp_retain(c, GATED_TAGS)

    (line,) = tag_lines(c)
    assert line["retain_tags"]["client"] == "claude-code"          # 도착 시점 이름
    assert sessions.peek(_fingerprint(GATE_SESSION)) != "claude-code"      # 대기 중 변화는 실제로 일어났다


def test_nameless_session_keeps_audit_unknown_and_ledger_none_even_if_a_name_binds_mid_wait(gated):
    c, state, ledger_path = gated
    sessions = c.app.state.mcp_clients
    initialize(c, name=None)
    assert sessions.peek(_fingerprint(GATE_SESSION)) is None
    state["hook"] = lambda: _rebind(sessions)

    mcp_retain(c, GATED_TAGS)

    (line,) = tag_lines(c)
    assert line["retain_tags"]["client"] == CLIENT_UNKNOWN == "unknown"     # 감사 줄: 이름 없음 = "unknown"
    assert sessions.peek(_fingerprint(GATE_SESSION)) == "codex"            # 대기 중 바인딩은 실제로 일어났다

    state["hook"] = None
    sessions._names.pop(_fingerprint(GATE_SESSION))                         # 다시 이름 없는 세션으로
    call(c, BANK, TOKEN, name="recall", arguments={"query": "원장 None 구분 확인용 쿼리"},
         headers={"mcp-session-id": GATE_SESSION})
    rows = [json.loads(ln) for ln in ledger_path.read_text(encoding="utf-8").splitlines()]
    (row,) = [r for r in rows if r["query"] == "원장 None 구분 확인용 쿼리"]
    assert row["client"] is None                                           # 원장: 이름 없음 = None


def test_audit_unknown_still_distinguishes_a_client_literally_named_unknown(gated):
    c, state, _ = gated
    initialize(c, name="unknown")
    state["hook"] = lambda: _rebind(c.app.state.mcp_clients)

    mcp_retain(c, GATED_TAGS)

    (line,) = tag_lines(c)
    assert line["retain_tags"]["client"] == "unknown"                      # 도착 시점의 실제 이름


def test_retaining_session_keeps_its_lru_slot_while_other_sessions_bind(make_app):
    """감사 줄이 도착 시점 값을 쓰더라도 retain은 세션 LRU 순서를 갱신한다(#37 "still retaining")."""
    c = strict_client(make_app)
    sessions = c.app.state.mcp_clients
    initialize(c, name="claude-code")
    for n in range(gateway_app._CLIENT_SESSIONS_MAX - 1):        # 상한까지 채운다: 이 세션이 가장 오래됐다
        sessions.bind(_fingerprint(f"other-{n}"), "filler")

    mcp_retain(c, ["project:hindsight-crew", "kind:decision"])   # retain이 이 세션을 최신으로 옮긴다
    sessions.bind(_fingerprint("one-more"), "filler")            # 상한 초과: 가장 오래된 세션이 밀려난다

    assert sessions.peek(_fingerprint(GATE_SESSION)) == "claude-code"
