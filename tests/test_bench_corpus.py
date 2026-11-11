"""issue #13 재생 하네스 코퍼스 추출기 - 수집·정규화·제외·분할 규칙."""
import json

from bench import corpus as C


def test_canonical_key_folds_mcp_defaults_and_order():
    k = C.canonical_key
    assert k({"query": "a  b"}) == k({"query": "a b", "max_tokens": 4096, "budget": "high"})
    assert k({"query": "q", "tags": ["y", "x"]}) == k({"query": "q", "tags": ["x", "y"], "tags_match": "any"})
    assert k({"query": "q", "tags_match": "all"}) == k({"query": "q"})          # 태그 없으면 매칭 무의미
    assert k({"query": "q", "budget": "low"}) != k({"query": "q"})
    assert k({"query": "q", "max_tokens": 8192}) != k({"query": "q"})


def test_exclusion_reasons():
    r = C.exclusion_reason
    assert r({"query": ""}) == "malformed"
    assert r({"query": None}) == "malformed"
    assert r({"query": "seed JBSWY3DPEHPK3PXPJBSWY3DP"}) == "secret-shape"
    assert r({"query": "probe #12 검증"}) == "probe"
    assert r({"query": "정상", "max_tokens": 500}) == "policy-rejected"
    assert r({"query": "정상", "max_tokens": 4096}) is None


def test_replay_args_drop_bank_and_unknown():
    out = C.replay_args({"query": "q", "bank_id": "x", "budget": "low", "weird": 1, "tags": None})
    assert out == {"query": "q", "budget": "low"}


def test_split_is_deterministic_and_salt_dependent():
    keys = [f"{i:016x}" for i in range(2000)]
    s1, s2 = b"a" * 32, b"b" * 32
    h1 = [C.is_holdout(s1, k, 0.3) for k in keys]
    assert h1 == [C.is_holdout(s1, k, 0.3) for k in keys]
    assert 0.25 < sum(h1) / len(keys) < 0.35
    assert h1 != [C.is_holdout(s2, k, 0.3) for k in keys]


def test_collectors_parse_transcripts(tmp_path):
    cc = tmp_path / "cc" / "p"
    cc.mkdir(parents=True)
    (cc / "s.jsonl").write_text(json.dumps({
        "timestamp": "2026-09-01T00:00:00Z",
        "message": {"content": [{"type": "tool_use", "name": "mcp__memory-vault__recall",
                                 "input": {"query": "cc q", "max_tokens": 8192}}]}}) + "\n")
    cx = tmp_path / "cx"
    cx.mkdir()
    item = {"type": "McpToolCall", "id": "x1", "server": "memory-vault", "tool": "recall",
            "arguments": {"query": "codex q"}}
    line = json.dumps({"timestamp": "2026-09-02T00:00:00Z", "payload": {"item": item}}, separators=(",", ":"))
    (cx / "r.jsonl").write_text(line + "\n" + line + "\n")          # started/completed 중복 기록
    calls = C.collect_cc(str(tmp_path / "cc")) + C.collect_codex(str(cx))
    assert [(c.source, c.args["query"]) for c in calls] == [("cc", "cc q"), ("codex", "codex q")]


def test_build_dedups_and_counts():
    calls = [C.Call("cc", "2026-09-01", {"query": "q"}, "a"),
             C.Call("ledger", "2026-09-03", {"query": "q"}, "b"),
             C.Call("cc", "2026-09-02", {"query": "q", "max_tokens": 4096}, "c"),
             C.Call("cc", None, {"query": "probe x"}, "d")]
    entries, stats = C.build(calls)
    (e,) = entries.values()
    # 원장 행은 같은 텍스트가 transcript 에 있으면 그림자로 빠진다(2026-09-23 재리뷰)
    assert e.count == 2 and dict(e.sources) == {"cc": 2}
    assert (e.first_seen, e.last_seen) == ("2026-09-01", "2026-09-02")
    assert stats["excluded"] == {"probe": 1, "ledger-shadow": 1}


# ---------------------------------------------------------------- 재리뷰(2026-09-23) - holdout 누출·중복

def test_split_key_is_query_text_only():
    # 인자만 다른 같은 텍스트는 같은 분할 키 -> dev/holdout 에 갈라지지 않는다
    a = {"query": "같은  질문", "max_tokens": 8192}
    b = {"query": "같은 질문", "budget": "low", "tags": ["project:x"]}
    assert C.canonical_key(a) != C.canonical_key(b)
    assert C.split_key(a) == C.split_key(b)


def test_limit_is_not_replayed():
    assert "limit" not in C.replay_args({"query": "q", "limit": 5})


def test_ledger_shadow_and_session_repeats(tmp_path):
    led = tmp_path / "recall-queries.log"
    rows = [{"identity": "demo", "bank": "personal-demo", "query": "q1", "session_id": "s", "ts": "2026-09-22T00:00:0%dZ" % i}
            for i in range(3)]
    rows.append({"identity": "demo", "bank": "personal-demo", "query": "only ledger", "session_id": "s",
                 "ts": "2026-09-22T00:01:00Z"})
    led.write_text("".join(json.dumps(r) + "\n" for r in rows))
    ledger = C.collect_ledger(str(led))
    assert [c.args["query"] for c in ledger] == ["q1", "only ledger"]          # 같은 세션 반복은 1회
    entries, stats = C.build([C.Call("cc", "2026-09-22", {"query": "q1", "max_tokens": 8192}, "x")] + ledger)
    assert sorted(e.args["query"] for e in entries.values()) == ["only ledger", "q1"]
    assert stats["excluded"] == {"ledger-shadow": 1}                            # q1 원장 행은 그림자


def test_cc_tool_use_counted_once_by_id(tmp_path):
    cc = tmp_path / "cc" / "p"
    cc.mkdir(parents=True)
    line = json.dumps({"timestamp": "2026-09-01T00:00:00Z", "message": {"content": [
        {"type": "tool_use", "id": "toolu_1", "name": "mcp__memory-vault__recall", "input": {"query": "q"}}]}})
    (cc / "a.jsonl").write_text(line + "\n")
    (cc / "b.jsonl").write_text(line + "\n")                                   # 서브에이전트 transcript 복제
    assert len(C.collect_cc(str(tmp_path / "cc"))) == 1


def test_split_version_change_requires_reset(tmp_path):
    (tmp_path / "split.version").write_text("key-v0\n")
    import pytest
    with pytest.raises(SystemExit):
        C.check_split_version(str(tmp_path), reset=False)
    C.check_split_version(str(tmp_path), reset=True)
    assert (tmp_path / "split.version").read_text().strip() == C.SPLIT_VERSION
    C.check_split_version(str(tmp_path), reset=False)                          # 같은 버전은 통과


def test_missing_version_with_legacy_holdout_requires_reset(tmp_path):
    import pytest
    (tmp_path / "holdout.jsonl").write_text("{}\n")                         # eecef81 분할(버전 파일 없음)
    with pytest.raises(SystemExit):
        C.check_split_version(str(tmp_path), reset=False)
    C.check_split_version(str(tmp_path), reset=True)
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    C.check_split_version(str(fresh), reset=False)                            # 처음 만드는 분할은 통과
