"""recall 프루너 회귀 - 핵심 계약은 '검색 결과가 바뀌지 않는다'이다.

프루닝은 검색이 끝난 뒤의 렌더링만 건드리므로 A/B 없이 결과-불변으로 증명한다.
픽스처는 upstream v0.8.3 의 실측 shape(이중 인코딩)를 합성으로 재현한 것이다 -
실제 기억 내용은 리포에 넣지 않는다.
"""
import json

import pytest

from gateway import recall_prune


def _row(**over):
    row = {
        "id": "mem-1",
        "text": "본문",
        "fact_type": "world",
        "entities": None,
        "context": "맥락",
        "occurred_start": "2026-09-22T07:33:20+00:00",
        "occurred_end": "2026-09-22T07:33:20+00:00",
        "mentioned_at": "2026-09-22T07:33:20+00:00",
        "document_id": "doc-1",
        "metadata": {},
        "chunk_id": "bank_doc-1_0",
        "tags": ["project:x", "kind:decision"],
        "source_fact_ids": None,
        "scores": {"final": 1.05, "reranker": 0.96, "semantic": 0.56, "keyword": 0.6},
    }
    row.update(over)
    return row


def _payload(n=3):
    return {
        "results": [_row(id=f"mem-{i}", text=f"본문 {i}") for i in range(n)],
        "trace": None, "entities": None, "chunks": None,
        "source_facts": None, "source_facts_truncated": None,
    }


def _tool_result(payload):
    """upstream 이 payload 를 2벌(structuredContent.result + content[0].text) 싣는 shape."""
    blob = json.dumps(payload, ensure_ascii=False, indent=2)
    return {
        "_meta": {},
        "content": [{"type": "text", "text": blob}],
        "structuredContent": {"result": blob},
        "isError": False,
    }


# --- 계약 1: 결과 집합과 순서는 절대 바뀌지 않는다 ---------------------------------

def test_result_ids_and_order_are_invariant():
    payload = _payload(5)
    before = [r["id"] for r in payload["results"]]
    after = [r["id"] for r in recall_prune.prune_payload(payload)["results"]]
    assert after == before


def test_result_count_is_invariant():
    payload = _payload(7)
    assert len(recall_prune.prune_payload(payload)["results"]) == 7


def test_text_is_never_touched():
    payload = _payload(3)
    pruned = recall_prune.prune_payload(payload)
    assert [r["text"] for r in pruned["results"]] == [r["text"] for r in payload["results"]]


# --- 계약 2: 제거되는 것은 내부 메타이거나 무손실(null/중복)뿐 ---------------------

def test_lossy_drops_are_off_by_default():
    """운영자 결정(2026-09-22): 정보를 버려서 얻는 9.6%p 는 사지 않는다.

    특히 scores 는 클라이언트가 '이 쿼리가 빗나갔는가'를 알아채는 유일한 신호다.
    """
    assert recall_prune.DROP_ALWAYS == frozenset()
    pruned = recall_prune.prune_result(_row())
    assert pruned["scores"]["final"] == 1.05
    assert pruned["chunk_id"] == "bank_doc-1_0"


def test_drop_always_mechanism_still_works(monkeypatch):
    """평가 하네스가 손실을 정량화하면 다시 채울 수 있도록 메커니즘은 살려 둔다."""
    monkeypatch.setattr(recall_prune, "DROP_ALWAYS", frozenset({"scores"}))
    pruned = recall_prune.prune_result(_row())
    assert "scores" not in pruned
    assert "chunk_id" in pruned


def test_drops_are_lossless_null_or_redundant():
    row = _row()
    pruned = recall_prune.prune_result(row)
    for key in set(row) - set(pruned):
        if key in recall_prune.DROP_ALWAYS:
            continue
        twin = recall_prune.REDUNDANT_WITH.get(key)
        assert row[key] is None or row[key] in ({}, []) or (twin and row[key] == row[twin]), key


def test_keeps_handles_and_content():
    pruned = recall_prune.prune_result(_row())
    for key in ("id", "text", "tags", "occurred_start", "context", "fact_type",
                "document_id", "scores", "chunk_id"):
        assert key in pruned, key


def test_keeps_occurred_end_when_it_differs():
    """기간이 실재하면 남긴다 - 중복일 때만 지운다."""
    pruned = recall_prune.prune_result(_row(occurred_end="2026-09-23T00:00:00+00:00"))
    assert pruned["occurred_end"] == "2026-09-23T00:00:00+00:00"


def test_keeps_unknown_fields_with_values():
    """블랙리스트이지 화이트리스트가 아니다 - 모르는 필드는 값이 있으면 남는다."""
    pruned = recall_prune.prune_result(_row(brand_new_field="값"))
    assert pruned["brand_new_field"] == "값"


@pytest.mark.parametrize("value", [0, False, "", 0.0])
def test_keeps_falsy_but_meaningful_values(value):
    pruned = recall_prune.prune_result(_row(some_metric=value))
    assert pruned["some_metric"] == value


def test_keeps_populated_containers():
    pruned = recall_prune.prune_result(_row(metadata={"k": "v"}, entities=["e"]))
    assert pruned["metadata"] == {"k": "v"}
    assert pruned["entities"] == ["e"]


def test_drops_top_level_nulls_but_keeps_results():
    pruned = recall_prune.prune_payload(_payload(2))
    assert "trace" not in pruned and "source_facts" not in pruned
    assert len(pruned["results"]) == 2


# --- 계약 3: 이중 인코딩 2벌이 어긋나지 않는다 ------------------------------------

def test_both_copies_stay_in_sync():
    result, changed = recall_prune.prune_tool_result(_tool_result(_payload(3)))
    assert changed
    assert result["structuredContent"]["result"] == result["content"][0]["text"]


def test_pruned_payload_still_parses_and_keeps_ids():
    payload = _payload(4)
    result, _ = recall_prune.prune_tool_result(_tool_result(payload))
    out = json.loads(result["structuredContent"]["result"])
    assert [r["id"] for r in out["results"]] == [r["id"] for r in payload["results"]]


def test_structured_content_may_be_a_real_object():
    """upstream 이 이중 인코딩을 고치면 그때도 동작해야 한다."""
    result, changed = recall_prune.prune_tool_result(
        {"structuredContent": {"result": _payload(2)}, "isError": False})
    assert changed
    assert "entities" not in result["structuredContent"]["result"]["results"][0]


def test_shrinks():
    before = _tool_result(_payload(4))
    after, _ = recall_prune.prune_tool_result(before)
    assert len(after["structuredContent"]["result"]) < len(before["structuredContent"]["result"])


# --- 계약 4: fail-open, 그리고 다른 도구의 응답은 건드리지 않는다 ------------------

def test_unparseable_text_passes_through_untouched():
    broken = {"content": [{"type": "text", "text": "not json{{"}],
              "structuredContent": {"result": "not json{{"}, "isError": False}
    result, changed = recall_prune.prune_tool_result(broken)
    assert not changed and result == broken


def test_error_result_is_untouched():
    err = {"content": [{"type": "text", "text": "boom"}], "isError": True}
    assert recall_prune.prune_tool_result(err) == (err, False)


def test_non_recall_element_is_untouched():
    el = {"jsonrpc": "2.0", "id": 9, "result": _tool_result(_payload(2))}
    assert recall_prune.prune_mcp_element(el, {7}) is el


def test_recall_element_is_pruned():
    el = {"jsonrpc": "2.0", "id": 7, "result": _tool_result(_payload(2))}
    out = recall_prune.prune_mcp_element(el, {7})
    assert out is not el
    assert "entities" not in json.loads(out["result"]["structuredContent"]["result"])["results"][0]


def test_element_without_result_is_untouched():
    el = {"jsonrpc": "2.0", "id": 7, "error": {"code": -32601, "message": "nope"}}
    assert recall_prune.prune_mcp_element(el, {7}) is el


def test_already_lean_payload_is_not_marked_changed():
    """무변환이면 dirty 로 표시되지 않아야 reframe 의 바이트 동일 왕복이 깨지지 않는다."""
    lean = {"results": [{"id": "m1", "text": "본문", "tags": ["a:b"]}]}
    assert recall_prune.prune_tool_result(_tool_result(lean))[1] is False


# --- 계약 5: REST 표면(이중 인코딩 없는 shape) --------------------------------------

def test_rest_body_is_pruned_and_ids_survive():
    payload = _payload(3)
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    out = json.loads(recall_prune.prune_rest_body(raw))
    assert [r["id"] for r in out["results"]] == [r["id"] for r in payload["results"]]
    assert "entities" not in out["results"][0]
    assert len(recall_prune.prune_rest_body(raw)) < len(raw)


@pytest.mark.parametrize("raw", [
    b"", b"not json{{", b'"a bare string"', b'{"no_results_key": 1}', b'[1,2,3]',
])
def test_rest_body_fails_open(raw):
    assert recall_prune.prune_rest_body(raw) is raw


def test_rest_body_unchanged_when_already_lean():
    raw = json.dumps({"results": [{"id": "m1", "text": "본문"}]}, ensure_ascii=False).encode()
    assert recall_prune.prune_rest_body(raw) is raw


def test_env_flag_rolls_back(monkeypatch):
    monkeypatch.setenv(recall_prune.ENV_FLAG, "0")
    assert recall_prune.enabled() is False
    monkeypatch.setenv(recall_prune.ENV_FLAG, "1")
    assert recall_prune.enabled() is True


# --- 계약 6: chunk_id 형식 파생 (issue #13) - {bank}_{document_id}_0 이면 bank 와 document_id 로 복원된다 ---

def test_chunk_id_dropped_from_both_copies_when_bank_rebuilds_it():
    result, changed = recall_prune.prune_tool_result(_tool_result(_payload(2)), bank="bank")
    assert changed
    assert result["structuredContent"]["result"] == result["content"][0]["text"]
    rows = json.loads(result["content"][0]["text"])["results"]
    assert all("chunk_id" not in r and r["document_id"] == "doc-1" for r in rows)


def test_rest_body_drops_chunk_id_when_bank_rebuilds_it():
    raw = json.dumps(_payload(2), ensure_ascii=False).encode("utf-8")
    rows = json.loads(recall_prune.prune_rest_body(raw, bank="bank"))["results"]
    assert all("chunk_id" not in r and r["document_id"] == "doc-1" for r in rows)


@pytest.mark.parametrize("bank,over", [
    (None, {}),                                        # bank 모름
    ("other", {}),                                     # 다른 bank
    ("bank", {"chunk_id": "bank_doc-1_3"}),            # 0 번이 아닌 chunk
    ("bank", {"document_id": None}),                   # document_id 가 빈 값이라 함께 빠진다
])
def test_chunk_id_kept_when_not_rebuildable(bank, over):
    row = _row(**over)
    pruned = recall_prune.prune_result(row, bank)
    assert pruned["chunk_id"] == row["chunk_id"]


def test_chunk_id_kept_when_document_id_does_not_survive(monkeypatch):
    """복원 근거는 원본이 아니라 나가는 결과에 남아 있어야 한다."""
    monkeypatch.setattr(recall_prune, "DROP_ALWAYS", frozenset({"document_id"}))
    pruned = recall_prune.prune_result(_row(), "bank")
    assert "document_id" not in pruned and pruned["chunk_id"] == "bank_doc-1_0"


@pytest.mark.parametrize("row,bank", [
    ({"document_id": "doc-1", "chunk_id": "b_doc-1_0"}, "b"),
    ({"document_id": "doc-1", "chunk_id": "b_doc-1_3"}, "b"),
    ({"document_id": "doc-1", "chunk_id": "b_doc-1_0"}, "c"),
    ({"document_id": "doc-1", "chunk_id": "b_doc-1_0"}, None),
    ({"chunk_id": "b_None_0"}, "b"),
    ({"document_id": 7, "chunk_id": "b_7_0"}, "b"),
])
def test_format_rule_matches_bench_judge(row, bank):
    """판정기의 무손실 정의가 게이트웨이보다 느슨해지면 실제 손실이 PASS 로 샌다 - 두 선언을 묶어 둔다."""
    from bench import judge
    assert recall_prune.FORMAT_DERIVABLE.keys() == judge.FORMAT_DERIVABLE.keys()
    dropped = "chunk_id" not in recall_prune.prune_result(row, bank)
    rest = {k: v for k, v in row.items() if k != "chunk_id"}
    assert dropped == (judge.compare_result(row, rest, bank=bank) == "lossless")
