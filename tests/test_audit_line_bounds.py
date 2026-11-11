"""Issue #117: 감사 줄은 입력 크기와 무관한 바이트 상한 안에 든다 (#99·#103 후속).

문자 수 상한(TAG_LOG_MAX=256)은 JSON 이스케이프 팽창을 못 막았다: 제어 문자 태그 64개는 enforce-normalize·
retain-tags 줄 하나가 약 96KB였고, reflect-demand 줄의 tags_match·budget, recall 원장 줄의 tags·tags_match는
절삭 경로가 아예 없었다. 이 파일은 같은 입력으로 아래를 핀한다:
  ① 줄 하나의 `len(line.encode())`가 상한 이하이고 줄 자체는 남는다
  ② 잘렸다는 사실이 `<list>_count`/`<list>_omitted`/`<list>_truncated`, `<field>_truncated` 키로 남는다
  ③ upstream으로 가는 요청 본문은 그대로다, 재발 탐지 초안이 재사용하는 in-memory 원장 이벤트도 그대로다
  ④ 상한 이하 입력(ASCII 64x256)은 키도 값도 수정 전과 같다
"""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import ACL_DATA, UPSTREAM_KEY, call
from gateway import audit as audit_module
from gateway.acl import ACL
from gateway.app import create_app
from gateway.audit import Audit
from gateway.recurrence import RecallLedger, build_draft
from test_reflect_demand import RecordingUpstream, call_reflect, demand_lines, make as make_reflect_app
from test_reflect_demand import reflect_result, sse
from test_retain_tag_audit import (BANK, SECRET_CONTENT, audit_entries, initialize, mcp_retain,
                                   rest_post, strict_client, upstream_tags)

# 줄 하나의 상한. ASCII 64x256 최악(#99·#103이 허용하던 크기, retain-tags 약 34KB)보다 위, 수정 전 제어 문자
# 입력(약 96KB)보다 아래다. 입력을 아무리 키워도 이 값을 넘지 않아야 한다.
LINE_MAX_BYTES = 40 * 1024

CTRL = "\x01"
SURFACES = ["mcp", "rest"]


def line_bytes(entry) -> int:
    """감사 파일에 실제로 쓰인 바이트 수와 같은 기준(Audit.record의 직렬화)."""
    return len(json.dumps(entry, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def ctrl_tags(n):
    return [f"Topic:a{i}" + CTRL * 248 for i in range(n)]


def ascii_tags(n):
    return [f"Topic:{i:02d}" + "a" * 248 for i in range(n)]            # 정확히 256자 - #103 쪽 최악 ASCII


def lines_of(client, decision):
    return [e for e in audit_entries(client) if e["decision"] == decision]


def send_retain(c, surface, tags):
    r = mcp_retain(c, tags) if surface == "mcp" else rest_post(c, [{"content": SECRET_CONTENT, "tags": tags}])
    assert r.status_code == 200
    return r


# ============================ ① retain: retain-tags·enforce-normalize ============================

def _none_of(d, *keys):
    return not any(k in d for k in keys)


RETAIN_CASES = [
    # id, 보낼 태그(kind:decision 하나 + 교정되는 태그들), retain-tags 기대, enforce-normalize 기대
    pytest.param(
        ["kind:decision"] + ctrl_tags(64), "ctrl-64",
        lambda info: info["sent_count"] == 65 and info["sent_omitted"] > 0 and info["sent_truncated"] > 0
        and info["stored_count"] == 65 and info["stored_omitted"] == 1,     # 저장분은 게이트웨이가 제어 문자를 이미 걷어 짧다
        lambda n: n["normalized_count"] == 64 and n["normalized_omitted"] > 0 and n["normalized_truncated"] > 0,
        id="ctrl-64"),
    pytest.param(
        ["kind:decision"] + ctrl_tags(600), "ctrl-600",
        lambda info: info["sent_count"] == 601 and info["sent_omitted"] > 0 and info["sent_truncated"] > 0,
        lambda n: n["normalized_count"] == 600 and n["normalized_omitted"] > 0 and n["normalized_truncated"] > 0,
        id="ctrl-600"),
    pytest.param(
        ["kind:decision"] + ascii_tags(63), "ascii-64x256",
        lambda info: _none_of(info, "sent_count", "sent_omitted", "sent_truncated",
                              "stored_count", "stored_omitted", "stored_truncated"),
        lambda n: n is None,                 # 수정 전과 같다: 64개 이하·256자 이하 ASCII는 어떤 키도 안 붙는다
        id="ascii-64x256"),
    pytest.param(
        ["kind:decision", "Topic:" + "A" * 100_000], "ascii-100KB-tag",
        lambda info: info["sent_truncated"] == 1 and info["stored_truncated"] == 1
        and _none_of(info, "sent_omitted", "sent_count"),
        lambda n: n == {"normalized_truncated": 2},
        id="ascii-100KB-tag"),
]


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("tags, _label, retain_ok, normalize_ok", RETAIN_CASES)
def test_retain_audit_lines_stay_under_the_byte_bound_and_say_what_was_cut(
        make_app, recorder, surface, tags, _label, retain_ok, normalize_ok):
    c = strict_client(make_app)

    send_retain(c, surface, tags)

    (rt,) = lines_of(c, "retain-tags")                       # 줄 자체는 남는다
    (norm,) = lines_of(c, "enforce-normalize")
    assert line_bytes(rt) <= LINE_MAX_BYTES
    assert line_bytes(norm) <= LINE_MAX_BYTES
    assert retain_ok(rt["retain_tags"])
    assert normalize_ok(norm.get("normalize_tags"))
    # upstream으로 가는 태그는 수정 전과 같다: 게이트웨이 교정(제어 문자 제거·소문자)만 거친 전체 목록이다
    assert upstream_tags(recorder, surface) == [t.replace(CTRL, "").lower() for t in tags]


@pytest.mark.parametrize("surface", SURFACES)
def test_retain_audit_line_size_does_not_depend_on_input_size(make_app, surface):
    """같은 감사 파일에 제어 문자 태그 100개, 3000개 순서로 쌓아 두 줄 크기가 자릿수 차이만 나는지 본다."""
    c = strict_client(make_app)
    for n in (100, 3000):
        send_retain(c, surface, ["kind:decision"] + ctrl_tags(n))
    for decision in ("retain-tags", "enforce-normalize"):
        small, big = lines_of(c, decision)
        assert max(line_bytes(small), line_bytes(big)) <= LINE_MAX_BYTES
        assert abs(line_bytes(small) - line_bytes(big)) < 64


@pytest.mark.parametrize("surface", SURFACES)
def test_ascii_64x256_retain_lines_are_untouched_by_the_byte_bounds(make_app, surface):
    """#99·#103의 최악 ASCII 입력은 바이트 상한 도입 뒤에도 64개 전부, 256자 전부, 쌍 63개 전부 그대로다."""
    c = strict_client(make_app)
    tags = ["kind:decision"] + ascii_tags(63)

    send_retain(c, surface, tags)

    (rt,) = lines_of(c, "retain-tags")
    assert rt["retain_tags"]["sent"] == tags
    (norm,) = lines_of(c, "enforce-normalize")
    pairs = norm["reason"].removeprefix("tag-normalize:").split(",")
    assert pairs == [f"{t}->{t.lower()}" for t in tags[1:]]


# ============================ ② clientInfo.name (MCP only) ============================

CLIENT_NAMES = [
    pytest.param("N" * 100_000, id="ascii-100KB"),
    pytest.param(CTRL * 100_000 + "abc", id="ctrl-100KB"),
    pytest.param("한" * 100_000, id="hangul-100KB"),
    pytest.param("\U0001F600" * 100_000, id="supplementary-100KB"),
]


@pytest.mark.parametrize("name", CLIENT_NAMES)
def test_client_name_in_retain_tags_line_stays_bounded(make_app, name):
    """MCP initialize clientInfo.name은 세션에 묶이기 전에 출력 가능 문자 64자로 잘린다(#37) -
    REST에는 client가 없다. 줄 크기가 이름 크기와 무관함을 핀한다."""
    c = strict_client(make_app)
    assert initialize(c, name=name).status_code == 200

    mcp_retain(c, ["project:hindsight-crew", "kind:decision"])

    (rt,) = lines_of(c, "retain-tags")
    assert len(rt["retain_tags"]["client"]) <= 64
    assert line_bytes(rt) <= 1024                            # 태그 두 개짜리 줄: 이름이 줄을 키우지 못한다


# ============================ ③ reflect-demand: tags_match·budget·tags ============================

REFLECT_ARGS = [
    pytest.param({"tags_match": "a" * 100_000}, {"tags_match_truncated": True}, id="tags_match-100KB"),
    pytest.param({"budget": "b" * 100_000}, {"budget_truncated": True}, id="budget-100KB"),
    pytest.param({"tags_match": CTRL * 100_000, "budget": CTRL * 100_000},
                 {"tags_match_truncated": True, "budget_truncated": True}, id="ctrl-100KB-both"),
    pytest.param({"tags": [f"topic:t{i}" + CTRL * 248 for i in range(600)]},
                 {"tags_count": 600, "tags_omitted": None, "tags_truncated": None}, id="ctrl-tags-600"),
    pytest.param({"tags": ["topic:demo-brand"], "tags_match": "all", "budget": "low"}, {}, id="normal-no-markers"),
]


def reflect_with_args(tmp_path, surface, extra):
    """(reflect-demand 줄, upstream이 받은 요청의 인자 dict)."""
    args = {"query": "q", "tags": ["topic:x"], **extra}
    payload = {"text": "x", "based_on": {"mental_models": [{"id": "demo-rules-branding"}]},
               "usage": {"input_tokens": 1}}
    if surface == "mcp":
        upstream = RecordingUpstream(sse(reflect_result(7, payload)))
        client, log = make_reflect_app(upstream, tmp_path)
        assert call_reflect(client, args).status_code == 200
        received = upstream.bodies[-1]["params"]["arguments"]
    else:
        upstream = RecordingUpstream(json.dumps(payload).encode("utf-8"), content_type="application/json")
        client, log = make_reflect_app(upstream, tmp_path)
        resp = client.post("/v1/default/banks/personal-alice/reflect", content=json.dumps(args),
                           headers={"content-type": "application/json", "authorization": "Bearer tok-alice"})
        assert resp.status_code == 200
        received = upstream.bodies[-1]
    (line,) = demand_lines(log)
    return line, args, received


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("extra, markers", REFLECT_ARGS)
def test_reflect_demand_line_stays_under_the_byte_bound_and_marks_cut_fields(
        tmp_path, surface, extra, markers):
    line, args, received = reflect_with_args(tmp_path, surface, extra)

    assert line_bytes(line) <= LINE_MAX_BYTES                # 줄은 남고 크기는 입력과 무관하다
    # upstream 요청 본문은 그대로 (include_based_on은 기존 reflect 기본값 보충이 넣는 키라 감사 상한과 무관)
    assert {k: v for k, v in received.items() if k != "include_based_on"} == args
    ev = line["reflect"]
    assert ev["based_on"] == "hit" and ev["mental_models"] == ["demo-rules-branding"]
    for key in ("tags_match", "budget"):
        if isinstance(ev[key], str):
            assert len(ev[key]) <= audit_module.FIELD_LOG_MAX
    marker_keys = {"tags_match_truncated", "budget_truncated", "tags_count", "tags_omitted", "tags_truncated"}
    assert {k: ev[k] for k in marker_keys if k in ev}.keys() == markers.keys()
    for key, value in markers.items():
        if value is not None:
            assert ev[key] == value
        else:
            assert ev[key] > 0
    if "tags_match_truncated" in markers:                    # 잘린 값은 앞부분만 남는다
        assert ev["tags_match"] == args["tags_match"][:len(ev["tags_match"])]


@pytest.mark.parametrize("surface", SURFACES)
def test_reflect_demand_size_does_not_depend_on_the_string_size(tmp_path, surface):
    sizes = []
    for name, n in (("small", 1_000), ("big", 1_000_000)):
        (tmp_path / name).mkdir()
        line, _, _ = reflect_with_args(tmp_path / name, surface, {"tags_match": "a" * n, "budget": "b" * n})
        sizes.append(line_bytes(line))

    assert sizes[0] == sizes[1] <= LINE_MAX_BYTES


# ============================ ④ recall 원장 줄 (audit/recall-queries.log) ============================

RECALL_QUERY = "recall 원장 줄 상한 확인용 쿼리"
LEDGER_CASES = [
    pytest.param({"tags": [f"topic:t{i}" + CTRL * 248 for i in range(2000)]},
                 lambda row: row["tags_count"] == 2000 and row["tags_omitted"] > 0
                 and row["tags_truncated"] > 0, id="ctrl-tags-2000"),
    pytest.param({"tags": ["topic:" + "t" * 100_000]},
                 lambda row: row["tags_truncated"] == 1 and "tags_omitted" not in row, id="ascii-100KB-tag"),
    pytest.param({"tags": ["topic:x"], "tags_match": "m" * 100_000},
                 lambda row: row["tags_match_truncated"] is True and len(row["tags_match"]) == 64,
                 id="tags_match-100KB"),
    pytest.param({"tags": ["topic:x"], "tags_match": {"k": ["v" * 100_000]}},
                 lambda row: row["tags_match"] is None and row["tags_match_truncated"] is True,
                 id="tags_match-object"),
    pytest.param({"tags": ["topic:x", "kind:gotcha"], "tags_match": "all"},
                 lambda row: row["tags"] == ["topic:x", "kind:gotcha"] and row["tags_match"] == "all"
                 and not any(k.endswith(("_count", "_omitted", "_truncated")) for k in row),
                 id="normal-no-markers"),
]


@pytest.fixture
def recall_gateway(tmp_path):
    seen = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append({"path": request.url.path, "body": json.loads(request.content or b"{}")})
        results = {"results": [{"id": "mem-1", "text": "x"}]}
        if request.url.path.endswith("/memories/recall"):
            return httpx.Response(200, json=results)
        encoded = json.dumps(results)
        result = {"content": [{"type": "text", "text": encoded}],
                  "structuredContent": {"result": encoded}, "isError": False}
        return httpx.Response(200, content=b"event: message\r\ndata: " + json.dumps(
            {"jsonrpc": "2.0", "id": 1, "result": result}).encode("utf-8") + b"\r\n\r\n",
            headers={"content-type": "text/event-stream", "mcp-session-id": "s1"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(upstream), base_url="http://upstream", timeout=5.0)
    ledger_path = tmp_path / "ledger.jsonl"
    app = create_app(acl=ACL.from_dict(ACL_DATA), audit=Audit(path=str(tmp_path / "audit.log")),
                     upstream_key=UPSTREAM_KEY, http_client=http,
                     recall_ledger=RecallLedger(str(ledger_path)))
    return TestClient(app), ledger_path, seen


def send_recall(client, surface, args):
    headers = {"content-type": "application/json", "authorization": "Bearer tok-alice"}
    if surface == "mcp":
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "recall", "arguments": {"query": RECALL_QUERY, **args}}}
        resp = client.post(f"/mcp/{BANK}/", content=json.dumps(body), headers=headers)
    else:
        resp = client.post(f"/v1/default/banks/{BANK}/memories/recall",
                           content=json.dumps({"query": RECALL_QUERY, **args}), headers=headers)
    assert resp.status_code == 200


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("args, row_ok", LEDGER_CASES)
def test_recall_ledger_line_stays_under_the_byte_bound_while_memory_and_upstream_keep_everything(
        recall_gateway, surface, args, row_ok):
    client, ledger_path, seen = recall_gateway

    send_recall(client, surface, args)

    (raw,) = ledger_path.read_text(encoding="utf-8").splitlines()       # 줄은 남는다
    assert len(raw.encode("utf-8")) <= LINE_MAX_BYTES
    row = json.loads(raw)
    assert row["query"] == RECALL_QUERY and row["result_ids"] == ["mem-1"] and row["transport"] == surface
    assert row_ok(row)
    lossy = any(k.endswith(("_omitted", "_truncated")) for k in row)
    assert row["reusable"] is (not lossy)                    # 잘린 행은 파일 소비자(duplicate_recall·bench)가 재사용하지 않는다
    # upstream 요청 본문의 인자는 그대로다
    sent = seen[-1]["body"]
    sent_args = sent["params"]["arguments"] if surface == "mcp" else sent
    assert {k: v for k, v in sent_args.items() if k != "query"} == args
    # in-memory 이벤트는 원본 그대로 - 재발 탐지 초안이 재사용한다
    (ev,) = client.app.state.recall_ledger.recent(identity="alice", bank=BANK, since=0)
    assert list(ev.tags) == args.get("tags", [])
    assert ev.reusable is True                               # in-memory 값은 그대로 - 초안은 원본을 재사용한다
    assert ev.tags_match == args.get("tags_match", "any")


@pytest.mark.parametrize("surface", SURFACES)
def test_recurrence_draft_still_replays_the_original_recall_tags(recall_gateway, surface):
    """원장 파일 줄을 잘라도 초안(trigger.recall.tags/tags_match)은 원본 recall을 그대로 재현한다."""
    client, _, _ = recall_gateway
    tags = [f"topic:t{i}" + CTRL * 248 for i in range(200)]
    send_recall(client, surface, {"tags": tags, "tags_match": "all"})
    (ev,) = client.app.state.recall_ledger.recent(identity="alice", bank=BANK, since=0)

    draft = build_draft(bank=BANK, signal="test", new_content="draft 확인용 본문 content",
                        similar=None, rescue=ev)

    assert draft["trigger"]["recall"]["tags"] == tags
    assert draft["trigger"]["recall"]["tags_match"] == "all"


@pytest.mark.parametrize("surface", SURFACES)
def test_recall_ledger_size_does_not_depend_on_tag_count(recall_gateway, surface):
    client, ledger_path, _ = recall_gateway
    for n in (100, 3000):
        send_recall(client, surface, {"tags": [f"topic:t{i}" + CTRL * 248 for i in range(n)]})
    small, big = [ln.encode("utf-8") for ln in ledger_path.read_text(encoding="utf-8").splitlines()]

    assert max(len(small), len(big)) <= LINE_MAX_BYTES
    assert abs(len(small) - len(big)) < 64
