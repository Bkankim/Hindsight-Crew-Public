"""N0: contract fixtures — pin the RAW upstream contract (live v0.8.3 capture).

These tests fail until scripts/mcp-probe.sh has collected fixtures/ and the
conftest loaders exist. They encode the plan's shape assumptions so a re-probe
against a different upstream version surfaces drift as test failures.
"""
import json
import os

import pytest

from conftest import FIXTURES_DIR, load_mcp_fixture, load_rest_fixture


def test_tools_list_pins_retain_timestamp_param():
    # D1-① 전제: retain류 inputSchema에 timestamp 파라미터가 실재해야 autofill이 성립.
    result = load_mcp_fixture("tools_list")
    tools = {t["name"]: t for t in result["result"]["tools"]}
    for name in ("retain", "sync_retain"):
        assert name in tools
        props = tools[name]["inputSchema"]["properties"]
        assert "timestamp" in props
        assert "tags" in props
        assert "content" in props


def test_initialize_has_no_upstream_instructions():
    # S3 전제: upstream은 initialize instructions를 발신하지 않는다.
    result = load_mcp_fixture("initialize")
    assert "instructions" not in result["result"]


def test_retain_ok_and_fail_results_captured():
    ok = load_mcp_fixture("sync_retain_ok")
    assert ok["result"].get("isError") is not True
    fail = load_mcp_fixture("sync_retain_fail")
    # upstream(FastMCP)은 검증 실패를 isError:true로 반환한다 (SEP-1303 정합).
    assert fail["result"].get("isError") is True


def test_retain_async_returns_operation_reference():
    # N5 전제: 비동기 retain 응답에서 operation 참조를 안내에 쓸 수 있어야 한다.
    res = load_mcp_fixture("retain_async_ok")
    text = str(res["result"])
    assert "operation" in text.lower() or "pending" in text.lower()


def test_rest_memories_shape_and_config():
    ok = load_rest_fixture("memories_post_ok")
    assert ok["status"] in (200, 201)
    assert isinstance(ok["request"].get("items"), list)  # N3 매핑 전제: items[] 배열
    fail = load_rest_fixture("memories_post_fail")
    assert fail["status"] == 422
    cfg = load_rest_fixture("bank_config")
    assert cfg["status"] == 200
    # N1 드리프트 게이트 전제: config API가 retain_strategies를 반환.
    assert "retain_strategies" in str(cfg["response"])


# --- v1.1.0 재채집 (issue #285, 방식은 #247) -------------------------------------------------------

V1_1_DIGEST = "sha256:50311d02b4d5d2b7c5242bd336edf8735df8374eaad496a92f49c83b37d2aa06"


def test_notes_record_the_v1_capture_conditions():
    with open(os.path.join(FIXTURES_DIR, "mcp", "NOTES.md"), encoding="utf-8") as fh:
        notes = fh.read()
    assert V1_1_DIGEST in notes                     # 채집한 upstream 이미지
    assert "--llm opus" in notes                    # reflect·성공 retain 을 실제 모양으로 채집한 LLM 모드
    assert "mcp-probe.sh" in notes and "seed_cases.json" in notes
    assert load_mcp_fixture("initialize")["result"]["serverInfo"]["version"] == "1.1.0"


def _tool_schema(name):
    return next(t for t in load_mcp_fixture("tools_list")["result"]["tools"] if t["name"] == name)["inputSchema"]


def test_v1_1_recall_schema_has_the_source_options_off_by_default():
    # upstream #5678: MCP recall 이 chunks·entities·source facts 를 돌려줄 수 있다. 전부 기본 꺼짐이라 기존 응답 크기 불변.
    props = _tool_schema("recall")["properties"]
    for opt in ("include_chunks", "include_entities", "include_source_facts"):
        assert props[opt] == {"default": False, "type": "boolean"}, opt
    assert _tool_schema("recall")["required"] == ["query"]


@pytest.mark.parametrize("tool", ["recall", "reflect"])
def test_every_tags_match_the_gateway_sends_is_in_the_v1_1_enum(tool):
    # upstream #5653: 모르는 tags_match 는 거부된다. 게이트웨이가 스스로 보내는 값(트리거 검증 집합 + 기본 "any")은 전부 enum 안이다.
    from gateway import context_triggers
    enum = set(_tool_schema(tool)["properties"]["tags_match"]["enum"])
    assert enum == {"any", "all", "any_strict", "all_strict", "exact"}
    assert set(context_triggers._TAGS_MATCH) | {"any"} <= enum


def test_recall_prune_keeps_the_v1_1_source_sections():
    # v1.1.0 사본 실측(upstream 직결, include_* 셋 true): 최상위 키 chunks·entities·source_facts(dict)·source_facts_truncated.
    # prune 은 results 행만 다듬고 값이 있는 다른 최상위 키는 그대로 둔다.
    from gateway import recall_prune
    payload = {"results": [{"id": "m1", "text": "t", "attachments": None}],
               "chunks": {"c1": {"chunk_index": 0, "chunk_text": "x", "truncated": False}},
               "entities": {"e1": {"canonical_name": "n", "entity_id": "e1", "observations": []}},
               "source_facts": {"f1": {"id": "f1", "text": "s"}}, "source_facts_truncated": False}
    out = recall_prune.prune_payload(payload, "probe-n0")
    for k in ("chunks", "entities", "source_facts", "source_facts_truncated"):
        assert out[k] == payload[k], k
    assert "attachments" not in out["results"][0]


def _reflect_has_based_on(name):
    result = load_mcp_fixture(name)["result"]
    carriers = [result["content"][0]["text"], result["structuredContent"]["result"]]
    return [("based_on" in json.loads(c)) for c in carriers]


def test_reflect_fixtures_pair_include_based_on_true_has_based_on():
    # 제거 시험이 공허하지 않으려면 true 원본에 based_on 이 있고(두 운반체 모두), 미지정 원본에는 없어야 한다.
    assert _reflect_has_based_on("reflect_based_on") == [True, True]
    assert _reflect_has_based_on("reflect_default") == [False, False]


def test_rest_recall_fixture_keeps_replay_fields():
    fx = load_rest_fixture("recall_gate")
    assert fx["method"] == "POST" and fx["path"].endswith("/memories/recall") and fx["status"] == 200
    assert fx["contentType"].startswith("application/json") and fx["bodyBase64"]
