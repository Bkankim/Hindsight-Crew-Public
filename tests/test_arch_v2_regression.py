"""issue #287 상태 회귀 세트 평가 도구 - 합성 정답표와 mock gateway·upstream으로 채점 규칙과 대상 가드를 고정한다.

운영·사본에 닿지 않는다. 정답표·응답은 전부 합성이다(실제 항목 이름 없음).
"""
import json

import pytest

from bench import arch_v2_regression as A

CUTOFF = "2026-10-28T15:00:00Z"
BEFORE = "2026-10-16T03:00:00+00:00"
AFTER = "2026-10-29T01:00:00+00:00"


def key_doc():
    return {
        "schema_version": 1,
        "query_date": "2026-10-28",
        "ignore_mentioned_at_from": CUTOFF,
        "query_instruction": "SYNTH-INSTRUCTION answer as of the query date",
        "canonical_restore": {"backup": "backups/x/pg0.tar.gz", "sha256": "0" * 64},
        "closed": [
            {"id": "K01", "item": "SECRET-ITEM-ALPHA", "keywords": ["alpha-kw", "#901"], "project_hint": "proj-a",
             "done_external": "x", "bank_evidence": None},
            {"id": "K02", "item": "SECRET-ITEM-BETA", "keywords": ["beta-kw"], "project_hint": None,
             "done_external": "x", "bank_evidence": None},
        ],
        "controls": [{"id": "C01", "item": "SECRET-ITEM-GAMMA", "keywords": ["gamma-kw"],
                      "project_hint": "proj-a", "expect": "open"}],
        "excluded": [{"item": "SECRET-ITEM-DELTA", "reason": "r"}],
    }


def mem(text, when=BEFORE, tags=(), mid="m1", fact_type="world"):
    return {"id": mid, "text": text, "mentioned_at": when, "tags": list(tags), "fact_type": fact_type}


# ---------------------------------------------------------------- 기억 분류

@pytest.mark.parametrize("m, want", [
    (mem("[완료] alpha-kw 머지", tags=["kind:completion"]), "done"),
    (mem("alpha-kw 정리", tags=["kind:task-done"]), "done"),          # tag_aliases -> kind:completion
    (mem("[완료] alpha-kw 끝"), "done"),                              # 라벨만
    (mem("[인계] alpha-kw 남은 일", tags=["kind:handoff"]), "open"),
    (mem("alpha-kw 다음에 할 것", tags=["kind:todo"]), "open"),
    (mem("[결정] alpha-kw 방식 채택", tags=["kind:decision"]), "neutral"),
])
def test_classify_memory(m, want):
    assert A.classify_memory(m) == want


# ---------------------------------------------------------------- evidence

def test_evidence_counts_only_done_before_cutoff():
    item = key_doc()["closed"][0]
    results = {
        "alpha-kw": [mem("[인계] alpha-kw 남음", tags=["kind:handoff"], mid="o1"),
                     mem("[완료] alpha-kw 머지", when=AFTER, tags=["kind:completion"], mid="late"),
                     mem("[완료] unrelated 머지", tags=["kind:completion"], mid="nokw")],
        "#901": [mem("[완료] proj-a #901 머지", tags=["kind:completion"], mid="ok")],
    }
    ev = A.evidence_for_item(item, results, A.parse_ts(CUTOFF))
    assert ev["status"] == "present"
    assert [e["memory_id"] for e in ev["evidence"]] == ["ok"]
    assert ev["rejected"] == {"post_cutoff": 1, "undated": 0, "not_done": 1, "no_keyword": 1}


@pytest.mark.parametrize("undated_ok, want", [(True, "present"), (False, "none")])
def test_evidence_undated_done_memory_follows_target_policy(undated_ok, want):
    item = key_doc()["closed"][1]
    results = {"beta-kw": [mem("[완료] beta-kw 끝", when=None, tags=["kind:completion"], mid="u")]}
    ev = A.evidence_for_item(item, results, A.parse_ts(CUTOFF), undated_ok)
    assert ev["status"] == want
    assert ev["rejected"]["undated"] == (0 if undated_ok else 1) and ev["rejected"]["post_cutoff"] == 0


def test_evidence_post_cutoff_only_is_none():
    # (a) 기준 이후 기억은 근거가 아니다: 유일한 완료 기억이 기준 이후면 "없음 -> 세트에서 뺌"
    item = key_doc()["closed"][1]
    results = {"beta-kw": [mem("[완료] beta-kw 끝", when=AFTER, tags=["kind:completion"])]}
    ev = A.evidence_for_item(item, results, A.parse_ts(CUTOFF))
    assert ev["status"] == "none" and ev["reason"] == "no-done-memory-before-cutoff"


def test_active_set_drops_items_without_evidence():
    doc = key_doc()
    summary = {"K01": {"status": "present"}, "K02": {"status": "none"}}
    ids = [i["id"] for i in A.active_items(doc, summary)]
    assert ids == ["K01", "C01"]


# ---------------------------------------------------------------- 섹션 규칙 (a)(b)

OPEN_WITH_ALPHA = "intro alpha-kw mentioned here\n## OPEN\n- alpha-kw still pending\n- gamma-kw\n## DONE\n- beta-kw\n"


def items():
    d = key_doc()
    return d["closed"] + d["controls"]


def test_sections_catch_closed_item_listed_open():
    # 상태 오답을 실제로 잡는다: 끝난 K01 이 OPEN 절에 나오면 open
    v = A.judge_sections(OPEN_WITH_ALPHA, [mem("x")], items(), A.parse_ts(CUTOFF), undated_ok=True)
    assert v["K01"]["verdict"] == "open"
    assert v["K02"]["verdict"] == "not-open"          # DONE 절에만 있다
    assert v["C01"]["verdict"] == "open"


def test_sections_ignore_mentions_outside_open():
    # 경쟁 가설 H1(전체 본문 키워드)이 틀리는 사례: 서두·DONE 절 언급은 열림이 아니다
    text = "alpha-kw finished on 10-16.\n## OPEN\n- gamma-kw\n## DONE\n- alpha-kw\n"
    v = A.judge_sections(text, [mem("x")], items(), A.parse_ts(CUTOFF), undated_ok=True)
    assert v["K01"]["verdict"] == "not-open"
    assert A.keyword_anywhere(text, items()[0]["keywords"])   # H1 이었다면 open


def test_sections_missing_heading_is_invalid():
    v = A.judge_sections("- alpha-kw pending\n", [mem("x")], items(), A.parse_ts(CUTOFF), undated_ok=True)
    assert {x["verdict"] for x in v.values()} == {"invalid"}
    assert v["K01"]["reason"] == "format-no-open-heading"


@pytest.mark.parametrize("based_on, want", [
    ({"world": [mem("old")], "observation": [mem("late", when=AFTER)]}, "post-cutoff-evidence"),
    ({"memories": [mem("late", when=AFTER)]}, "post-cutoff-evidence"),       # 멘탈 모델 reflect_response 모양
    ({"world": [mem("old")], "mental_models": [{"id": "mm", "text": "t"}]}, None),
])
def test_sections_post_cutoff_based_on_is_invalid(based_on, want):
    facts = A.iter_facts(based_on)
    v = A.judge_sections(OPEN_WITH_ALPHA, facts, items(), A.parse_ts(CUTOFF), undated_ok=True)
    if want:
        assert {x["verdict"] for x in v.values()} == {"invalid"}
        assert v["K01"]["reason"] == want
    else:
        assert v["K01"]["verdict"] == "open"


def test_undated_evidence_invalid_on_prod_policy():
    facts = A.iter_facts({"mental_models": [{"id": "mm", "text": "t"}]})
    v = A.judge_sections(OPEN_WITH_ALPHA, facts, items(), A.parse_ts(CUTOFF), undated_ok=False)
    assert v["K01"] == {"verdict": "invalid", "reason": "undated-evidence"}


def test_sections_no_provenance_is_invalid():
    v = A.judge_sections(OPEN_WITH_ALPHA, None, items(), A.parse_ts(CUTOFF), undated_ok=True)
    assert v["K01"]["reason"] == "no-provenance"


# ---------------------------------------------------------------- recall 규칙 (c)

ITEM = {"id": "K01", "keywords": ["alpha-kw"]}


@pytest.mark.parametrize("name, results, want", [
    # 회귀: 완료 기억이 결과에 없고 열린 인계만 있다
    ("regression", [mem("[인계] alpha-kw 남음", "2026-10-10T00:00:00Z", ["kind:handoff"])], "open"),
    # 옛 인계(1위)보다 새 완료(2위): 최신 상태 진술이 완료
    ("newer-done", [mem("[인계] alpha-kw 남음", "2026-10-10T00:00:00Z", ["kind:handoff"], "a"),
                    mem("[완료] alpha-kw 머지", "2026-10-16T00:00:00Z", ["kind:completion"], "b")], "not-open"),
    # 완료 뒤에 다시 열림을 말한 기억(대조군 모양)
    ("reopened", [mem("[완료] alpha-kw 1차", "2026-10-05T00:00:00Z", ["kind:completion"], "a"),
                  mem("[인계] alpha-kw 남은 2차", "2026-10-18T00:00:00Z", ["kind:handoff"], "b")], "open"),
    # 상태를 말하지 않는 기억뿐
    ("neutral", [mem("[결정] alpha-kw 방식", tags=["kind:decision"])], "not-open"),
    # 키워드가 없는 완료 기억은 이 항목의 근거가 아니다
    ("other-item", [mem("[인계] alpha-kw 남음", "2026-10-10T00:00:00Z", ["kind:handoff"], "a"),
                    mem("[완료] other 머지", "2026-10-20T00:00:00Z", ["kind:completion"], "b")], "open"),
])
def test_recall_latest_status_rule(name, results, want):
    assert A.judge_recall(results, ITEM, A.parse_ts(CUTOFF), undated_ok=True)["verdict"] == want


def test_recall_same_date_tie_goes_to_rank():
    r = [mem("[인계] alpha-kw", "2026-10-16T00:00:00Z", ["kind:handoff"], "a"),
         mem("[완료] alpha-kw", "2026-10-16T00:00:00Z", ["kind:completion"], "b")]
    assert A.judge_recall(r, ITEM, A.parse_ts(CUTOFF), undated_ok=True)["verdict"] == "open"


def test_recall_post_cutoff_result_is_invalid():
    r = [mem("[완료] alpha-kw", BEFORE, ["kind:completion"], "a"), mem("unrelated", AFTER, mid="b")]
    v = A.judge_recall(r, ITEM, A.parse_ts(CUTOFF), undated_ok=True)
    assert v == {"verdict": "invalid", "reason": "post-cutoff-evidence"}


# ---------------------------------------------------------------- (c) 결정성

def test_scoring_is_deterministic():
    based_on = {"world": [mem("old")], "observation": [mem("o2", "2026-10-01T00:00:00Z")]}
    recall = [mem("[인계] alpha-kw", "2026-10-10T00:00:00Z", ["kind:handoff"], "a"),
              mem("[완료] alpha-kw", "2026-10-16T00:00:00Z", ["kind:completion"], "b")]

    def once():
        a = A.judge_sections(OPEN_WITH_ALPHA, A.iter_facts(based_on), items(), A.parse_ts(CUTOFF), True)
        c = {i["id"]: A.judge_recall(recall, i, A.parse_ts(CUTOFF), True) for i in items()}
        return json.dumps(A.summarize({"a_reflect_list": a, "c_recall_status": c}), sort_keys=True)

    assert once() == once()


# ---------------------------------------------------------------- (d) 리포 산출물

def test_repo_renderers_leak_no_names_or_queries():
    doc = key_doc()
    ev_summary = {"K01": {"status": "present", "mentioned_at": BEFORE, "kind": "kind:completion"},
                  "K02": {"status": "none", "reason": "no-done-memory-before-cutoff"}}
    sc = A.summarize({p: {i["id"]: {"verdict": "open", "reason": "x"} for i in items()} for p in A.PATHS})
    md = A.render_evidence_md(ev_summary, doc) + A.render_dryrun_md(sc, {"a_reflect_list": {"queries": 1}})
    forbidden = [doc["query_instruction"], "SYNTH-INSTRUCTION"]
    for it in doc["closed"] + doc["controls"] + doc["excluded"]:
        forbidden.append(it["item"])
        forbidden.extend(it.get("keywords", []))
    for q in (A.reflect_query(doc), A.recall_query(doc, items()[0]), A.page_query(doc, "proj-a")):
        forbidden.append(q)
    for f in forbidden:
        assert f not in md, f
    assert "K01" in md and "C01" in md and "세트에서 뺌" in md
    # 요약 JSON 도 id·경로·판정만
    assert set(sc) == set(A.PATHS)
    assert all(set(v) <= {"open", "not-open", "invalid"} for p in sc.values() for v in [set(p.values())])


def test_queries_carry_instruction_and_fixed_timestamp():
    doc = key_doc()
    assert doc["query_instruction"] in A.reflect_query(doc)
    assert doc["query_instruction"] in A.recall_query(doc, items()[0])
    assert doc["query_instruction"] in A.page_query(doc, None)
    assert A.recall_args(doc, items()[0])["query_timestamp"] == "2026-10-28T14:59:59Z"
    assert A.reflect_args(doc)["include_based_on"] is True


# ---------------------------------------------------------------- 대상 선택과 (b) 운영 쓰기 거부

NEXT_ENV = {"HC_TARGET": "next", "HC_GATEWAY_BIND": "127.0.0.1:18888", "HC_COMPOSE_PROJECT": "hc-next",
            "HC_HINDSIGHT_CONTAINER": "hc-next-hindsight-1"}


def test_target_copy_ok():
    t = A.resolve_target(NEXT_ENV, allow_prod=False, read_only=False)
    assert t.kind == "next" and t.writable


@pytest.mark.parametrize("env, allow, ro", [
    ({}, False, False),                                              # 기본은 사본: 사본 env 없이 거부
    ({}, True, False),                                               # --allow-prod 만
    ({}, False, True),                                               # --read-only 만
    (dict(NEXT_ENV, HC_GATEWAY_BIND="127.0.0.1:8888"), False, False),  # 사본 가드 불일치
    (dict(NEXT_ENV, HC_HINDSIGHT_CONTAINER="hindsight-crew-hindsight-1"), False, False),
    (NEXT_ENV, True, True),                                          # 사본 env 와 운영 플래그 동시
    ({"HC_GATEWAY_BIND": "127.0.0.1:18888"}, True, True),            # HC_TARGET 없이 사본 모양
])
def test_target_refusals(env, allow, ro):
    with pytest.raises(A.Refused):
        A.resolve_target(env, allow_prod=allow, read_only=ro)


def test_target_prod_read_only():
    t = A.resolve_target({}, allow_prod=True, read_only=True)
    assert t.kind == "prod" and not t.writable and t.gw_bind == "127.0.0.1:8888"


class FakeUpstream:
    def __init__(self, writable, pages=None):
        self.writable = writable
        self.calls = []
        self.writes = 0
        self.pages = pages or {}

    def request(self, method, path, body=None):
        if method != "GET":
            if not self.writable:
                raise A.Refused("upstream write refused on a read-only target")
            self.writes += 1
        self.calls.append((method, path))
        if path.endswith("/knowledge-base/tree"):
            return {"roots": [{"id": "kp-1", "kind": "page", "name": n, "mental_model_id": mm}
                              for n, mm in self.pages.items()]}
        if "/mental-models/" in path:
            return {"content": "## OPEN\n- gamma-kw\n## DONE\n",
                    "reflect_response": {"based_on": {"memories": [mem("x")]}}}
        if path.endswith("/stats"):
            return {"total_documents": 1, "total_observations": 1, "pending_consolidation": 0,
                    "nodes_by_fact_type": {}}
        if "/llm-requests" in path:
            return {"total": 0}
        return {}


class FakeGateway:
    def __init__(self):
        self.calls = []

    def call(self, tool, args):
        self.calls.append(tool)
        if tool == "reflect":
            return {"text": "## OPEN\n- gamma-kw\n", "based_on": {"world": [mem("x")]}}
        return {"results": [mem("[완료] alpha-kw beta-kw", tags=["kind:completion"])]}


def write_inputs(tmp_path):
    kp = tmp_path / "key.json"
    kp.write_text(json.dumps(key_doc()))
    ev = tmp_path / "evidence-summary.json"
    ev.write_text(json.dumps({"items": {"K01": {"status": "present"}, "K02": {"status": "present"}}}))
    return kp, ev


@pytest.mark.parametrize("argv_extra", [
    ["--allow-prod", "--read-only", "--create-pages"],   # 운영 + 쓰기 경로
    ["--allow-prod"],                                    # read-only 없이 운영
])
def test_prod_write_path_exit1_zero_upstream_writes(tmp_path, monkeypatch, argv_extra):
    kp, ev = write_inputs(tmp_path)
    up = FakeUpstream(writable=False)
    monkeypatch.setattr(A, "build_clients", lambda target, args, env=None: (FakeGateway(), up))
    rc = A.main(["score", "--answer-key", str(kp), "--evidence", str(ev), "--out", str(tmp_path / "o"), "--bank", "synth-bank"]
                + argv_extra, env={})
    assert rc == 1
    assert up.writes == 0 and up.calls == []


def test_prod_read_only_reads_existing_pages_without_writes(tmp_path, monkeypatch):
    kp, ev = write_inputs(tmp_path)
    up = FakeUpstream(writable=False, pages={A.page_name("proj-a"): "mm-a"})
    gw = FakeGateway()
    monkeypatch.setattr(A, "build_clients", lambda target, args, env=None: (gw, up))
    rc = A.main(["score", "--answer-key", str(kp), "--evidence", str(ev), "--out", str(tmp_path / "o"), "--bank", "synth-bank",
                 "--allow-prod", "--read-only"], env={})
    assert rc == 0
    assert up.writes == 0 and all(m == "GET" for m, _ in up.calls)
    summary = json.loads(next((tmp_path / "o").glob("score-*/summary.json")).read_text())
    assert summary["paths"]["b_knowledge_page_status"]["C01"] == "open"
    assert summary["paths"]["b_knowledge_page_status"]["K02"] == "invalid"   # 전체 뱅크 페이지가 없다


def test_upstream_client_refuses_post_when_read_only():
    calls = []
    u = A.Upstream("hindsight-crew-hindsight-1", "k", writable=False, runner=lambda *a, **k: calls.append(a))
    with pytest.raises(A.Refused):
        u.request("POST", "/v1/default/banks/b/knowledge-base/pages", {})
    assert calls == []


def test_out_dir_is_private(tmp_path):
    d = A.ensure_out_dir(str(tmp_path / "out"))
    assert (d.stat().st_mode & 0o777) == 0o700


class CopyUpstream(FakeUpstream):
    def request(self, method, path, body=None):
        if method == "POST" and path.endswith("/knowledge-base/pages"):
            self.writes += 1
            self.calls.append((method, path))
            h = body["name"]
            return {"page_id": "kp-" + h, "mental_model_id": "mm-" + h, "operation_id": "op-" + h}
        if "/operations/" in path:
            self.calls.append((method, path))
            return {"status": "completed"}
        return super().request(method, path, body)


class RegressingGateway(FakeGateway):
    """끝난 K01 을 열린 일로 내놓는 합성 응답(회귀 재현)."""

    def call(self, tool, args):
        self.calls.append(tool)
        if tool == "reflect":
            return {"text": "## OPEN\n- alpha-kw rollout pending\n- gamma-kw\n## DONE\n- beta-kw\n",
                    "based_on": {"world": [mem("x")], "observation": [mem("o")]}}
        return {"results": [mem("[인계] alpha-kw beta-kw gamma-kw 남음", "2026-10-10T00:00:00Z", ["kind:handoff"])]}


def test_copy_score_end_to_end_flags_regression(tmp_path, monkeypatch):
    kp, ev = write_inputs(tmp_path)
    up = CopyUpstream(writable=True)
    monkeypatch.setattr(A, "build_clients", lambda target, args, env=None: (RegressingGateway(), up))
    rc = A.main(["score", "--answer-key", str(kp), "--evidence", str(ev), "--out", str(tmp_path / "o"),
                 "--bank", "synth-bank"], env=NEXT_ENV)
    assert rc == 0
    run = next((tmp_path / "o").glob("score-*"))
    s = json.loads((run / "summary.json").read_text())["paths"]
    assert s["a_reflect_list"] == {"K01": "open", "K02": "not-open", "C01": "open"}
    assert s["b_knowledge_page_status"]["C01"] == "open" and s["b_knowledge_page_status"]["K01"] == "not-open"
    assert s["c_recall_status"] == {"K01": "open", "K02": "open", "C01": "open"}
    assert up.writes == 2                                       # proj-a 페이지 + 전체 페이지
    m = json.loads((run / "metrics.json").read_text())
    assert m["a_reflect_list"]["queries"] == 1 and m["c_recall_status"]["queries"] == 3
    assert m["b_knowledge_page_status"]["queries"] == 2
    # 요약 파일에는 이름·질의가 없다. 원문은 detail.json(리포 밖 출력)에만
    raw = (run / "summary.json").read_text()
    assert "SECRET-ITEM" not in raw and "SYNTH-INSTRUCTION" not in raw and "alpha-kw" not in raw


def test_evidence_command_writes_private_detail_and_id_only_summary(tmp_path, monkeypatch):
    kp, _ = write_inputs(tmp_path)

    class Gw(FakeGateway):
        def call(self, tool, args):
            self.calls.append(tool)
            assert args["query_timestamp"] == A.QUERY_TIMESTAMP
            if args["query"] == "#901":
                return {"results": [mem("[완료] proj-a #901 머지", tags=["kind:completion"], mid="ok")]}
            return {"results": [mem("[완료] beta-kw", when=AFTER, tags=["kind:completion"], mid="late")]}

    monkeypatch.setattr(A, "build_clients", lambda target, args, env=None: (Gw(), FakeUpstream(True)))
    assert A.main(["evidence", "--answer-key", str(kp), "--out", str(tmp_path / "o"), "--bank", "b"], env=NEXT_ENV) == 0
    run = next((tmp_path / "o").glob("evidence-*"))
    s = json.loads((run / "summary.json").read_text())
    assert s["items"]["K01"]["status"] == "present" and s["items"]["K02"]["status"] == "none"
    assert s["remaining"] == 2 and s["queries"] == 3
    assert "SECRET-ITEM" not in (run / "summary.json").read_text()
    assert json.loads((run / "K01.json").read_text())["evidence"][0]["memory_id"] == "ok"
    assert (run.stat().st_mode & 0o777) == 0o700


def test_open_section_keeps_nested_subheadings():
    # 사본 실측 응답 모양: ## OPEN 아래 프로젝트별 ### 하위 제목. 하위 제목에서 절이 끝나면 안 된다
    text = ("## OPEN\nintro\n### proj-a\n- alpha-kw pending\n### other\n- gamma-kw\n"
            "## DONE\n### proj-a\n- beta-kw\n")
    v = A.judge_sections(text, [mem("x")], items(), A.parse_ts(CUTOFF), undated_ok=True)
    assert {k: x["verdict"] for k, x in v.items()} == {"K01": "open", "K02": "not-open", "C01": "open"}
    # 하위 제목 줄 자체는 채점하지 않는다
    assert A.open_section_lines("## OPEN\n### alpha-kw\n- x\n") == ["- x"]


def test_page_based_on_hyphen_mental_models_is_undated():
    facts = A.iter_facts({"world": [mem("w")], "mental-models": [{"id": "mm", "text": "t"}], "directives": [{"id": "d"}]})
    assert [f.get("source") for f in facts] == [None, "mental_model"]
    v = A.judge_sections(OPEN_WITH_ALPHA, facts, items(), A.parse_ts(CUTOFF), undated_ok=False)
    assert v["K01"]["reason"] == "undated-evidence"


def test_rescore_reproduces_score_without_queries(tmp_path, monkeypatch):
    kp, ev = write_inputs(tmp_path)
    monkeypatch.setattr(A, "build_clients", lambda target, args, env=None: (RegressingGateway(), CopyUpstream(writable=True)))
    assert A.main(["score", "--answer-key", str(kp), "--evidence", str(ev), "--out", str(tmp_path / "o"),
                   "--bank", "b"], env=NEXT_ENV) == 0
    run = next((tmp_path / "o").glob("score-*"))
    monkeypatch.setattr(A, "build_clients", lambda *a: pytest.fail("rescore must not open clients"))
    outs = []
    for k in range(2):
        out = tmp_path / f"r{k}"
        assert A.main(["rescore", "--answer-key", str(kp), "--detail", str(run / "detail.json"),
                       "--out", str(out)], env={}) == 0
        outs.append(json.loads(next(out.glob("rescore-*/summary.json")).read_text())["paths"])
    assert outs[0] == outs[1] == json.loads((run / "summary.json").read_text())["paths"]


# ---------------------------------------------------------------- 규칙 v2 (사본 실측 오탐에서 나온 것)

@pytest.mark.parametrize("text, kw, want", [
    ("- **proj #250 cleanup**", "#2", False),        # 번호 접두어
    ("- **proj #2 log rotation**", "#2", True),
    ("projx #42 open", "#4", False),
    ("#4는 동기화 트리거", "#4", True),                    # 한글 조사는 경계가 아니다
    ("v9.9.9 이전은 끝", "v9.9.9 이전", True),
    ("PROJX proxy", "projx", True),
])
def test_keyword_boundaries(text, kw, want):
    assert A.matches(text, [kw]) is want


def test_open_line_subject_ignores_context_mentions():
    text = ("## OPEN\n- **proj-a #250 part 2:** due after the alpha-kw observation ended\n"
            "| gamma-kw | pending |\n## DONE\n")
    v = A.judge_sections(text, [mem("x")], items(), A.parse_ts(CUTOFF), undated_ok=True)
    assert v["K01"]["verdict"] == "not-open"       # 설명 안 언급
    assert v["C01"]["verdict"] == "open"           # 표 첫 칸
    assert A.line_subject("- plain line alpha-kw") == "- plain line alpha-kw"


def test_project_name_keyword_dropped_when_others_remain():
    it = {"id": "K", "keywords": ["Proj-A", "init cmd"], "project_hint": "proj-a"}
    assert A.item_keywords(it) == ["init cmd"]
    only = {"id": "K", "keywords": ["proj-a"], "project_hint": "proj-a"}
    assert A.item_keywords(only) == ["proj-a"]
    text = "## OPEN\n- **proj-a #42: other task**\n"
    v = A.judge_sections(text, [mem("x")], [it], A.parse_ts(CUTOFF), undated_ok=True)
    assert v["K"]["verdict"] == "not-open"


@pytest.mark.parametrize("m, want", [
    # 범위 태그를 물려받은 관찰: 태그는 완료, 본문은 남은 일
    (mem("남은 작업: #77은 관찰, alpha-kw는 동기화 트리거.", tags=["kind:completion"]), "open"),
    # 인계 태그, 본문은 완료
    (mem("[인계] alpha-kw 배포 완료(2026-10-20) - 다음 단계 정리", tags=["kind:handoff"]), "done"),
    # 마지막 문장이 이긴다
    (mem("alpha-kw는 아직 착수하지 않았다. 이후 alpha-kw를 완료했다.", tags=["kind:handoff"]), "done"),
    (mem("alpha-kw 완료되지 않음", tags=["kind:completion"]), "open"),
    # 본문 단서가 없으면 태그
    (mem("alpha-kw 관련 메모", tags=["kind:completion"]), "done"),
    # 한 문장에 두 단서: 그 문장은 상태를 말하지 않는다 -> 태그
    (mem("alpha-kw 완료, beta 남음", tags=["kind:handoff"]), "open"),
])
def test_item_state_text_first(m, want):
    assert A.item_state(m, ["alpha-kw"]) == want


def test_recall_observation_with_inherited_completion_tag_reads_open():
    r = [mem("[완료] other work 머지", "2026-10-18T00:00:00Z", ["kind:completion"], "a"),
         mem("남은 작업: alpha-kw는 동기화 트리거.", "2026-09-17T00:00:00Z", ["kind:completion"], "b")]
    assert A.judge_recall(r, ITEM, A.parse_ts(CUTOFF), undated_ok=True)["verdict"] == "open"


def test_evidence_keeps_project_name_keyword():
    it = {"id": "K", "keywords": ["Proj-A", "teardown kw"], "project_hint": "proj-a"}
    r = {"Proj-A": [mem("proj-a 는 2026-09-02 에 철거됨", tags=["kind:completion"], mid="e")]}
    assert A.evidence_for_item(it, r, A.parse_ts(CUTOFF))["status"] == "present"


@pytest.mark.parametrize("text, scoped, want", [
    ("Side area #2 and #3", False, False),   # 다른 영역의 같은 번호
    ("Proj-A #3 and #4", False, True),
    ("#3 reboot", True, True),                           # 프로젝트 페이지 안
    ("init cmd still pending", False, True),             # 이름 keyword 는 프로젝트 이름 없이도 센다
])
def test_bare_number_keyword_needs_project_context(text, scoped, want):
    it = {"id": "K", "keywords": ["#3", "init cmd"], "project_hint": "proj-a"}
    assert A.item_matches(text, it, scoped) is want


def test_evidence_bare_number_without_project_name_is_not_evidence():
    item = key_doc()["closed"][0]
    r = {"#901": [mem("[완료] #901 머지", tags=["kind:completion"], mid="x")]}
    ev = A.evidence_for_item(item, r, A.parse_ts(CUTOFF))
    assert ev["status"] == "none" and ev["rejected"]["no_keyword"] == 1


# ---------------------------------------------------------------- 규칙 v3 (final-review rework #287-1)

PROJ_ITEM = {"id": "K", "keywords": ["proj-a", "#287"], "project_hint": "proj-a"}


@pytest.mark.parametrize("line, want", [
    ("- **proj-a** #287 regression tool still pending", True),        # 굵은 구간 뒤 구분자 없음 -> 주어를 잇는다
    ("- **proj-a #287** still pending", True),
    ("- **#3** (참고: #287 언급)", False),                              # 지시문 예시 그대로
    ("- **proj-a #3** (#287 언급 참고)", False),                        # 괄호 뒤 설명
    ("- **proj-a #3** \u2014 waits on #287", False),              # em dash(U+2014) 뒤 설명
    ("- **proj-a #3**, after #287", False),                            # 쉼표 뒤 설명
    ("| **proj-a #3** | after #287 |", False),                         # 표 칸 구분자
    ("- **proj-a #3** - after #287", False),
    ("- **proj-a #3**: after #287", False),
    ("- **proj-a #3**. After #287", False),
])
def test_line_subject_stops_at_description_delimiters(line, want):
    text = "## OPEN\n" + line + "\n## DONE\n"
    v = A.judge_sections(text, [mem("x")], [PROJ_ITEM], A.parse_ts(CUTOFF), undated_ok=True)
    assert (v["K"]["verdict"] == "open") is want


@pytest.mark.parametrize("based_on", [{}, {"directives": [{"id": "d"}]}, {"world": [], "experience": []}])
def test_empty_provenance_is_invalid(based_on):
    v = A.judge_sections(OPEN_WITH_ALPHA, A.iter_facts(based_on), items(), A.parse_ts(CUTOFF), undated_ok=True)
    assert {x["verdict"] for x in v.values()} == {"invalid"}
    assert v["K01"]["reason"] == "no-provenance"


def test_existing_page_on_writable_target_stops_before_any_query(tmp_path, monkeypatch):
    kp, ev = write_inputs(tmp_path)
    up = CopyUpstream(writable=True, pages={A.page_name("proj-a"): "mm-old"})
    gw = RegressingGateway()
    monkeypatch.setattr(A, "build_clients", lambda target, args, env=None: (gw, up))
    rc = A.main(["score", "--answer-key", str(kp), "--evidence", str(ev), "--out", str(tmp_path / "o"),
                 "--bank", "b"], env=NEXT_ENV)
    assert rc == 1
    assert up.writes == 0 and gw.calls == []            # 페이지 POST 도, 비싼 reflect 도 하지 않는다


# ---------------------------------------------------------------- issue #297: 페이지 이름 옵션과 읽기 전용 페이지 모드

class TwoPageUpstream(CopyUpstream):
    """같은 사본에 `-pre` 페이지(재통합 전 생성, delta 갱신 대상)와 접미사 없는 페이지(재통합 뒤 full)가 함께 있다.
    두 페이지의 내용이 달라 어느 페이지로 채점했는지 판정에서 드러난다."""

    CONTENT = {"mm-pre": "## OPEN\n- alpha-kw pending\n- gamma-kw\n## DONE\n",        # K01 열림(회귀)
               "mm-full": "## OPEN\n- gamma-kw\n## DONE\n- alpha-kw\n",              # K01 끝남
               "mm-mapped": "## OPEN\n- beta-kw\n## DONE\n"}

    def request(self, method, path, body=None):
        mm = path.split("/mental-models/")[1].split("?")[0] if "/mental-models/" in path else None
        if mm in self.CONTENT:
            self.calls.append((method, path))
            return {"content": self.CONTENT[mm], "last_refreshed_at": f"2026-10-30T0{len(mm) % 10}:00:00Z",
                    "reflect_response": {"based_on": {"memories": [mem("x")]}}}
        return super().request(method, path, body)


def _score(tmp_path, monkeypatch, up, extra, out="o", env=None):
    kp, ev = write_inputs(tmp_path)
    gw = RegressingGateway()
    monkeypatch.setattr(A, "build_clients", lambda target, args, env=None: (gw, up))
    rc = A.main(["score", "--answer-key", str(kp), "--evidence", str(ev), "--out", str(tmp_path / out),
                 "--bank", "b", "--paths", "b"] + extra, env=NEXT_ENV if env is None else env)
    run = sorted(p for p in (tmp_path / out).glob("score-*") if (p / "summary.json").exists())
    return rc, (json.loads((run[-1] / "summary.json").read_text())["paths"] if run else None), gw, run


def test_page_name_suffix():
    assert A.page_name("proj-a", "-pre") == A.page_name("proj-a") + "-pre"
    assert A.page_name(None, "-pre") == A.PAGE_PREFIX + "all-pre"


def test_suffixed_and_plain_pages_coexist_on_one_copy_and_score_separately(tmp_path, monkeypatch):
    pages = {A.page_name("proj-a", "-pre"): "mm-pre", A.page_name("proj-a"): "mm-full"}
    up = TwoPageUpstream(writable=True, pages=pages)
    rc1, pre, _, _ = _score(tmp_path, monkeypatch, up, ["--read-pages", "--page-suffix", "-pre"], out="o1")
    rc2, full, _, _ = _score(tmp_path, monkeypatch, up, ["--read-pages"], out="o2")
    assert rc1 == rc2 == 0
    assert pre["b_knowledge_page_status"]["K01"] == "open" and full["b_knowledge_page_status"]["K01"] == "not-open"
    assert pre["b_knowledge_page_status"]["C01"] == full["b_knowledge_page_status"]["C01"] == "open"
    assert up.writes == 0                                     # 읽기 전용 모드는 사본에서도 POST 하지 않는다


def test_read_pages_on_a_copy_reads_existing_pages_without_writes_and_records_refresh_time(tmp_path, monkeypatch):
    up = TwoPageUpstream(writable=True, pages={A.page_name("proj-a", "-pre"): "mm-pre"})
    rc, s, gw, run = _score(tmp_path, monkeypatch, up, ["--read-pages", "--page-suffix", "-pre"])
    assert rc == 0 and up.writes == 0 and all(m == "GET" for m, _ in up.calls)
    detail = json.loads((run[-1] / "detail.json").read_text())
    pg = detail["b_knowledge_page_status"]["proj-a"]
    assert pg["name"] == A.page_name("proj-a", "-pre") and pg["last_refreshed_at"]
    assert s["b_knowledge_page_status"]["K02"] == "invalid"   # 전체 뱅크 `-pre` 페이지가 없다 = page-missing


def test_create_with_suffix_creates_suffixed_names_and_refuses_when_the_suffixed_name_exists(tmp_path, monkeypatch):
    up = CopyUpstream(writable=True)
    rc, s, _, _ = _score(tmp_path, monkeypatch, up, ["--page-suffix", "-pre"], out="o1")
    assert rc == 0 and up.writes == 2
    assert {p.split("/")[-1] for m, p in up.calls if m == "POST"} == {"pages"}
    # 접미사 없는 페이지만 있는 사본에서 `-pre` 생성은 막히지 않고, `-pre` 가 있으면 막힌다
    up2 = CopyUpstream(writable=True, pages={A.page_name("proj-a"): "mm-old"})
    rc2, _, _, _ = _score(tmp_path, monkeypatch, up2, ["--page-suffix", "-pre"], out="o2")
    assert rc2 == 0
    up3 = CopyUpstream(writable=True, pages={A.page_name("proj-a", "-pre"): "mm-old"})
    rc3, _, gw3, _ = _score(tmp_path, monkeypatch, up3, ["--page-suffix", "-pre"], out="o3")
    assert rc3 == 1 and up3.writes == 0 and gw3.calls == []


def test_created_page_bodies_carry_the_suffix(tmp_path, monkeypatch):
    seen = []

    class Rec(CopyUpstream):
        def request(self, method, path, body=None):
            if method == "POST":
                seen.append(body["name"])
            return super().request(method, path, body)

    _score(tmp_path, monkeypatch, Rec(writable=True), ["--page-suffix", "-pre"])
    assert sorted(seen) == sorted([A.page_name("proj-a", "-pre"), A.page_name(None, "-pre")])


def test_page_map_gives_the_real_page_name_per_hint_in_read_only_scoring(tmp_path, monkeypatch):
    m = tmp_path / "map.json"
    m.write_text(json.dumps({"proj-a": "ops-status-a", "-": "ops-status-all"}))
    up = TwoPageUpstream(writable=False, pages={"ops-status-a": "mm-full", "ops-status-all": "mm-mapped"})
    rc, s, _, run = _score(tmp_path, monkeypatch, up, ["--allow-prod", "--read-only", "--page-map", str(m)], env={})
    assert rc == 0 and up.writes == 0
    assert s["b_knowledge_page_status"] == {"K01": "not-open", "K02": "open", "C01": "open"}
    detail = json.loads((run[-1] / "detail.json").read_text())
    assert detail["b_knowledge_page_status"]["-"]["name"] == "ops-status-all"


@pytest.mark.parametrize("extra", [
    ["--page-map", "MAP"],                                   # 사본 기본은 생성(쓰기) - 매핑은 읽기 전용에서만
    ["--read-pages", "--page-map", "MAP", "--page-suffix", "-pre"],   # 둘 다 이름을 정한다 - 하나만
    ["--read-pages", "--create-pages"],
])
def test_page_option_conflicts_are_refused_before_any_query(tmp_path, monkeypatch, extra):
    m = tmp_path / "map.json"
    m.write_text(json.dumps({"proj-a": "x"}))
    up = CopyUpstream(writable=True)
    rc, _, gw, _ = _score(tmp_path, monkeypatch, up, [str(m) if x == "MAP" else x for x in extra])
    assert rc != 0 and up.writes == 0 and gw.calls == []


def test_page_map_missing_hint_is_page_missing_not_a_guess(tmp_path, monkeypatch):
    m = tmp_path / "map.json"
    m.write_text(json.dumps({"proj-a": "ops-status-a"}))
    up = TwoPageUpstream(writable=True, pages={"ops-status-a": "mm-full", A.page_name(None): "mm-mapped"})
    rc, s, _, _ = _score(tmp_path, monkeypatch, up, ["--read-pages", "--page-map", str(m)])
    assert rc == 0 and s["b_knowledge_page_status"]["K02"] == "invalid"


# ---------------------------------------------------------------- issue #297: (b) delta 갱신 대기(stats --wait-pages)

class PageWaitUpstream(FakeUpstream):
    """호출마다 상태가 한 단계씩 진행되는 사본: 통합 대기 -> 페이지 stale + refresh 진행 -> 모두 끝남."""

    STEPS = [(3, True, "processing"), (0, True, "processing"), (0, False, "completed")]

    def __init__(self):
        super().__init__(writable=True)
        self.i = 0

    def request(self, method, path, body=None):
        pc, stale, op = self.STEPS[min(self.i, len(self.STEPS) - 1)]
        self.calls.append((method, path))
        if path.endswith("/stats"):
            return {"total_documents": 1, "total_observations": 1, "pending_consolidation": pc, "pending_operations": 0,
                    "nodes_by_fact_type": {}}
        if path.endswith("/knowledge-base/tree"):
            return {"roots": [{"kind": "page", "name": A.page_name("proj-a", "-pre"), "is_stale": stale, "children": []},
                              {"kind": "page", "name": "other", "is_stale": False, "children": []}]}
        if "/operations" in path:
            self.i += 1                                  # 한 바퀴(stats -> tree -> operations)마다 한 단계
            return {"operations": [{"operation_type": "refresh_mental_model", "status": op},
                                   {"operation_type": "consolidation", "status": "completed"}]}
        return {}


def test_stats_wait_pages_waits_for_consolidation_refresh_and_staleness(tmp_path, monkeypatch, capsys):
    up = PageWaitUpstream()
    monkeypatch.setattr(A, "build_clients", lambda target, args, env=None: (FakeGateway(), up))
    monkeypatch.setattr(A.time, "sleep", lambda s: None)
    rc = A.main(["stats", "--bank", "b", "--wait", "600", "--wait-pages", "--out", str(tmp_path / "o")], env=NEXT_ENV)
    assert rc == 0 and up.i == 4                 # 완료는 연속 2회 poll(rework #297-2)
    rows = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")]
    assert rows[-1]["pages_stale"] == [] and rows[-1]["refresh_live"] == 0 and rows[-1]["pending_consolidation"] == 0
    assert all(m == "GET" for m, _ in up.calls)


def test_stats_without_wait_pages_keeps_the_old_row(tmp_path, monkeypatch, capsys):
    up = PageWaitUpstream()
    monkeypatch.setattr(A, "build_clients", lambda target, args, env=None: (FakeGateway(), up))
    assert A.main(["stats", "--bank", "b", "--out", str(tmp_path / "o")], env=NEXT_ENV) == 0
    row = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "pages_stale" not in row and not any("/knowledge-base/" in p for _, p in up.calls)


def test_stats_wait_pages_times_out_with_exit_2(tmp_path, monkeypatch, capsys):
    up = PageWaitUpstream()
    up.STEPS = [(0, True, "processing")]
    monkeypatch.setattr(A, "build_clients", lambda target, args, env=None: (FakeGateway(), up))
    monkeypatch.setattr(A.time, "sleep", lambda s: None)
    clock = iter(range(0, 10000, 100))
    monkeypatch.setattr(A.time, "monotonic", lambda: next(clock))
    assert A.main(["stats", "--bank", "b", "--wait", "250", "--wait-pages", "--out", str(tmp_path / "o")], env=NEXT_ENV) == 2


# ---------------------------------------------------------------- rework #297-1: 페이지 based_on 날짜 채우기

class UndatedPageUpstream(FakeUpstream):
    """페이지 reflect_response.based_on 관찰 항목에 mentioned_at 이 없다(v1.1.0 실측 모양). GET memories/{id} 로 날짜를 준다."""

    def __init__(self, writable, dates):
        super().__init__(writable, pages={A.page_name("proj-a"): "mm-a", A.page_name(None): "mm-all"})
        self.dates = dates                       # id -> mentioned_at, 없으면 404

    def request(self, method, path, body=None):
        if "/mental-models/" in path:
            self.calls.append((method, path))
            return {"content": "## OPEN\n- gamma-kw\n- beta-kw\n## DONE\n",
                    "reflect_response": {"based_on": {"observation": [{"id": "o1", "type": "observation", "text": "t"}],
                                                      "directives": []}}}
        if "/memories/" in path:
            self.calls.append((method, path))
            mid = path.rsplit("/", 1)[1]
            if mid == "gone":
                raise A.UpstreamError(404, path)       # 재통합으로 지워진 관찰
            if mid not in self.dates:
                raise A.UpstreamError(500, path)       # 조회 실패
            return {"id": mid, "mentioned_at": self.dates[mid]}
        return super().request(method, path, body)


def _prod_pages(tmp_path, monkeypatch, dates):
    kp, ev = write_inputs(tmp_path)
    up = UndatedPageUpstream(writable=False, dates=dates)
    monkeypatch.setattr(A, "build_clients", lambda target, args, env=None: (FakeGateway(), up))
    rc = A.main(["score", "--answer-key", str(kp), "--evidence", str(ev), "--out", str(tmp_path / "o"), "--bank", "b",
                 "--paths", "b", "--allow-prod", "--read-only"], env={})
    run = next((tmp_path / "o").glob("score-*"))
    return rc, json.loads((run / "detail.json").read_text())["reasons"]["b_knowledge_page_status"], up


def test_page_based_on_dates_are_filled_by_memory_lookup_and_judged(tmp_path, monkeypatch):
    rc, v, up = _prod_pages(tmp_path, monkeypatch, {"o1": BEFORE})
    assert rc == 0 and up.writes == 0 and all(m == "GET" for m, _ in up.calls)
    assert {k: x["verdict"] for k, x in v.items()} == {"K01": "not-open", "K02": "open", "C01": "open"}
    assert any(p.endswith("/memories/o1") for _, p in up.calls)


def test_page_based_on_date_lookup_failure_is_invalid(tmp_path, monkeypatch):
    rc, v, _ = _prod_pages(tmp_path, monkeypatch, {})
    assert rc == 0 and {x["verdict"] for x in v.values()} == {"invalid"}
    assert {x["reason"] for x in v.values()} == {"date-lookup-failed"}


def test_page_based_on_looked_up_date_after_cutoff_is_invalid(tmp_path, monkeypatch):
    rc, v, _ = _prod_pages(tmp_path, monkeypatch, {"o1": AFTER})
    assert rc == 0 and {x["reason"] for x in v.values()} == {"post-cutoff-evidence"}


def test_copy_target_date_lookup_failure_is_also_invalid(tmp_path, monkeypatch):
    kp, ev = write_inputs(tmp_path)
    up = UndatedPageUpstream(writable=True, dates={})
    monkeypatch.setattr(A, "build_clients", lambda target, args, env=None: (FakeGateway(), up))
    assert A.main(["score", "--answer-key", str(kp), "--evidence", str(ev), "--out", str(tmp_path / "o"), "--bank", "b",
                   "--paths", "b", "--read-pages"], env=NEXT_ENV) == 0
    v = json.loads(next((tmp_path / "o").glob("score-*/detail.json")).read_text())["reasons"]["b_knowledge_page_status"]
    assert {x["reason"] for x in v.values()} == {"date-lookup-failed"}


# ---------------------------------------------------------------- rework #297-2 (final-review #297)

class RaisingDateUpstream(UndatedPageUpstream):
    """날짜 조회가 UpstreamError 가 아닌 예외(시간 초과 등)로 끝나는 사본."""

    def __init__(self, writable, exc):
        super().__init__(writable, dates={})
        self.exc = exc

    def request(self, method, path, body=None):
        if "/memories/" in path:
            self.calls.append((method, path))
            raise self.exc
        return super().request(method, path, body)


def _score_pages(tmp_path, monkeypatch, up, extra, env):
    kp, ev = write_inputs(tmp_path)
    monkeypatch.setattr(A, "build_clients", lambda target, args, env=None: (FakeGateway(), up))
    rc = A.main(["score", "--answer-key", str(kp), "--evidence", str(ev), "--out", str(tmp_path / "o"), "--bank", "b",
                 "--paths", "b"] + extra, env=env)
    run = next((tmp_path / "o").glob("score-*"))
    return rc, json.loads((run / "detail.json").read_text()), json.loads((run / "summary.json").read_text())


@pytest.mark.parametrize("exc", [__import__("subprocess").TimeoutExpired(["docker"], 90), KeyError("status")])
def test_date_lookup_any_exception_is_page_invalid_not_a_crash(tmp_path, monkeypatch, exc):
    rc, detail, _ = _score_pages(tmp_path, monkeypatch, RaisingDateUpstream(True, exc), ["--read-pages"], NEXT_ENV)
    assert rc == 0
    assert {x["reason"] for x in detail["reasons"]["b_knowledge_page_status"].values()} == {"date-lookup-failed"}


@pytest.mark.parametrize("env, extra, want", [
    (NEXT_ENV, ["--read-pages"], {"open-section", "absent"}),                 # 사본: 날짜 없음 허용 -> 판정
    ({}, ["--allow-prod", "--read-only"], {"undated-evidence"}),              # 운영: 날짜 없음은 그대로 invalid
])
def test_lookup_success_without_date_follows_target_undated_policy(tmp_path, monkeypatch, env, extra, want):
    up = UndatedPageUpstream(writable=not extra[0].startswith("--allow"), dates={"o1": None})
    rc, detail, _ = _score_pages(tmp_path, monkeypatch, up, extra, env)
    assert rc == 0 and {x["reason"] for x in detail["reasons"]["b_knowledge_page_status"].values()} == want


def test_page_suffix_without_value_is_an_argparse_error_not_a_swallowed_flag(tmp_path, monkeypatch):
    kp, ev = write_inputs(tmp_path)
    up = CopyUpstream(writable=True)
    gw = FakeGateway()
    monkeypatch.setattr(A, "build_clients", lambda target, args, env=None: (gw, up))
    with pytest.raises(SystemExit) as e:
        A.main(["score", "--answer-key", str(kp), "--evidence", str(ev), "--out", str(tmp_path / "o"), "--bank", "b",
                "--paths", "b", "--page-suffix", "--read-pages"], env=NEXT_ENV)
    assert e.value.code == 2 and up.writes == 0 and up.calls == [] and gw.calls == []


class DeletedEvidenceUpstream(UndatedPageUpstream):
    """페이지 based_on 이 재통합으로 지워진 관찰(404)과 살아 있는 관찰을 함께 인용한다."""

    def request(self, method, path, body=None):
        if "/mental-models/" in path:
            self.calls.append((method, path))
            return {"content": "## OPEN\n- gamma-kw\n- beta-kw\n## DONE\n",
                    "reflect_response": {"based_on": {"observation": [{"id": "gone", "type": "observation"},
                                                                      {"id": "o1", "type": "observation"}]}}}
        return super().request(method, path, body)


@pytest.mark.parametrize("env, extra", [(NEXT_ENV, ["--read-pages"]), ({}, ["--allow-prod", "--read-only"])])
def test_deleted_evidence_404_is_left_out_of_the_date_check_and_counted(tmp_path, monkeypatch, env, extra):
    up = DeletedEvidenceUpstream(writable=not extra[0].startswith("--allow"), dates={"o1": BEFORE})
    rc, detail, summary = _score_pages(tmp_path, monkeypatch, up, extra, env)
    assert rc == 0
    v = detail["reasons"]["b_knowledge_page_status"]
    assert {k: x["verdict"] for k, x in v.items()} == {"K01": "not-open", "K02": "open", "C01": "open"}
    assert detail["b_knowledge_page_status"]["proj-a"]["deleted_evidence"] == 1
    assert summary["deleted_evidence"] == {"b_knowledge_page_status": 2}       # 페이지 2개(proj-a, 전체)에 1건씩


def test_only_deleted_evidence_is_no_provenance_and_other_errors_stay_lookup_failed(tmp_path, monkeypatch):
    class OnlyDeleted(DeletedEvidenceUpstream):
        def request(self, method, path, body=None):
            if "/mental-models/" in path:
                self.calls.append((method, path))
                return {"content": "## OPEN\n- gamma-kw\n", "reflect_response": {"based_on": {"observation": [{"id": "gone"}]}}}
            return super().request(method, path, body)
    rc, detail, _ = _score_pages(tmp_path, monkeypatch, OnlyDeleted(True, {}), ["--read-pages"], NEXT_ENV)
    assert {x["reason"] for x in detail["reasons"]["b_knowledge_page_status"].values()} == {"no-provenance"}
    up = RaisingDateUpstream(True, A.UpstreamError(500, "/memories/o1"))
    shutil = __import__("shutil")
    shutil.rmtree(tmp_path / "o")
    rc, detail, _ = _score_pages(tmp_path, monkeypatch, up, ["--read-pages"], NEXT_ENV)
    assert {x["reason"] for x in detail["reasons"]["b_knowledge_page_status"].values()} == {"date-lookup-failed"}


class PagedOpsUpstream(PageWaitUpstream):
    """operations 가 150건이고 진행 중인 refresh 가 첫 페이지(100건) 밖에 있다."""

    def request(self, method, path, body=None):
        if "/operations" in path:
            self.calls.append((method, path))
            self.i += 1
            q = dict(x.split("=") for x in path.split("?", 1)[1].split("&"))
            ops = [{"operation_type": "consolidation", "status": "completed"}] * 120 + \
                  [{"operation_type": "refresh_mental_model", "status": "processing"}] + \
                  [{"operation_type": "retain", "status": "completed"}] * 29
            off, lim = int(q.get("offset", 0)), int(q["limit"])
            return {"operations": ops[off:off + lim], "total": len(ops)}
        return super().request(method, path, body)


def test_page_refresh_state_pages_through_all_operations():
    up = PagedOpsUpstream()
    up.STEPS = [(0, False, "completed")]
    st = A.page_refresh_state(up, "b")
    assert st["refresh_live"] == 1
    assert sum(1 for _, p in up.calls if "/operations" in p) == 2


def test_wait_pages_needs_two_consecutive_done_polls(tmp_path, monkeypatch, capsys):
    up = PageWaitUpstream()
    # 1: 통합 중 / 2: 완료처럼 보임 / 3: 통합 끝난 직후 stale 등장 / 4, 5: 완료 -> 5번째 poll 에서야 끝
    up.STEPS = [(1, False, "completed"), (0, False, "completed"), (0, True, "pending"),
                (0, False, "completed"), (0, False, "completed")]
    monkeypatch.setattr(A, "build_clients", lambda target, args, env=None: (FakeGateway(), up))
    monkeypatch.setattr(A.time, "sleep", lambda s: None)
    rc = A.main(["stats", "--bank", "b", "--wait", "600", "--wait-pages", "--out", str(tmp_path / "o")], env=NEXT_ENV)
    assert rc == 0 and up.i == 5


def test_wait_pages_without_wait_is_an_argparse_error(tmp_path, monkeypatch):
    up = PageWaitUpstream()
    monkeypatch.setattr(A, "build_clients", lambda target, args, env=None: (FakeGateway(), up))
    with pytest.raises(SystemExit) as e:
        A.main(["stats", "--bank", "b", "--wait-pages", "--out", str(tmp_path / "o")], env=NEXT_ENV)
    assert e.value.code == 2 and up.calls == []


# ---------------------------------------------------------------- rework #304-1: 같은 이름 페이지가 둘 이상이면 고르지 않는다

def _folder(fid, name, children):
    return {"id": fid, "kind": "folder", "name": name, "children": children}


def _page(pid, name, mm):
    return {"id": pid, "kind": "page", "name": name, "mental_model_id": mm, "children": []}


@pytest.mark.parametrize("roots, want", [
    ([], None),                                                                       # 0개: 지금처럼 None
    ([_folder("f1", "dir-a", [_page("p1", "status", "mm-1")])], "p1"),               # 1개: 그 페이지
    ([_folder("f1", "dir-a", [_page("p1", "status", "mm-1")]), _page("p2", "other", "mm-2")], "p1"),
], ids=["none", "one-in-folder", "one-among-others"])
def test_find_page_zero_or_one_match_is_unchanged(roots, want):
    hit = A._find_page(roots, "status")
    assert (hit or {}).get("id") == want


def test_find_page_same_name_in_two_folders_is_ambiguous_not_the_first():
    roots = [_folder("f1", "dir-a", [_page("p1", "status", "mm-1")]),
             _folder("f2", "dir-b", [_folder("f3", "deep", [_page("p2", "status", "mm-2")])])]
    with pytest.raises(A.AmbiguousPage) as e:
        A._find_page(roots, "status")
    msg = str(e.value)
    assert "2" in msg and "dir-a" in msg and "dir-b/deep" in msg


class SameNameUpstream(TwoPageUpstream):
    """읽기 전용 운영 대상: 매핑이 가리키는 이름이 두 폴더에 있다(첫 것은 K01 open, 둘째는 끝남)."""

    def request(self, method, path, body=None):
        if path.endswith("/knowledge-base/tree"):
            self.calls.append((method, path))
            return {"roots": [_folder("f1", "dir-a", [_page("p1", "ops-status-a", "mm-pre")]),
                              _folder("f2", "dir-b", [_page("p2", "ops-status-a", "mm-full")]),
                              _page("p3", "ops-status-all", "mm-mapped")]}
        return super().request(method, path, body)


def test_read_only_scoring_with_ambiguous_page_name_is_invalid_for_that_hint(tmp_path, monkeypatch):
    m = tmp_path / "map.json"
    m.write_text(json.dumps({"proj-a": "ops-status-a", "-": "ops-status-all"}))
    up = SameNameUpstream(writable=False, pages={})
    rc, s, _, run = _score(tmp_path, monkeypatch, up, ["--allow-prod", "--read-only", "--page-map", str(m)], env={})
    assert rc == 0 and up.writes == 0
    assert s["b_knowledge_page_status"]["K01"] == "invalid"            # 첫 페이지(K01 open)로 채점하지 않는다
    assert s["b_knowledge_page_status"]["K02"] == "open"               # 모호하지 않은 힌트는 그대로
    detail = json.loads((run[-1] / "detail.json").read_text())
    page = detail["b_knowledge_page_status"]["proj-a"]
    assert page["ambiguous"]["matches"] == 2 and page["ambiguous"]["parents"] == ["dir-a", "dir-b"]
    assert detail["reasons"]["b_knowledge_page_status"]["K01"]["reason"] == "page-ambiguous"
    assert "mm-pre" not in [c[1].split("/mental-models/")[-1].split("?")[0] for c in up.calls if "/mental-models/" in c[1]]


def test_read_only_scoring_query_metric_skips_ambiguous_hints(tmp_path, monkeypatch):
    # rework #304-2: 모호한 힌트는 페이지(멘탈 모델)를 조회하지 않으므로 (b) queries 에 세지 않는다
    m = tmp_path / "map.json"
    m.write_text(json.dumps({"proj-a": "ops-status-a", "-": "ops-status-all"}))
    up = SameNameUpstream(writable=False, pages={})
    rc, _, _, run = _score(tmp_path, monkeypatch, up, ["--allow-prod", "--read-only", "--page-map", str(m)], env={})
    metrics = json.loads((run[-1] / "metrics.json").read_text())
    fetched = sum(1 for c in up.calls if "/mental-models/" in c[1] and "?detail=full" in c[1])
    assert rc == 0 and fetched == 1
    assert metrics["b_knowledge_page_status"]["queries"] == 1


# ---------------------------------------------------------------- issue #323: 세트 B, 합친 세트, 키 분리, 페이지 공유, arm 판정

def key_doc_b(**over):
    """세트 B 합성 정답표: 세 값은 세트 A 와 같고, 공유 대조군 C01 은 세트 A 항목의 바이트 복사다."""
    a = key_doc()
    d = {"schema_version": 1, "query_date": a["query_date"], "ignore_mentioned_at_from": a["ignore_mentioned_at_from"],
         "query_instruction": a["query_instruction"],
         "closed": [{"id": "B01", "item": "SECRET-ITEM-BRAVO", "keywords": ["bravo-kw"], "project_hint": "proj-b",
                     "done_external": "x", "bank_evidence": None}],
         "controls": [json.loads(json.dumps(a["controls"][0])),
                      {"id": "BC01", "item": "SECRET-ITEM-ECHO", "keywords": ["echo-kw"], "project_hint": "proj-b",
                       "expect": "open"}]}
    d.update(over)
    return d


KEY_SPLIT = {"A": {"K01": True, "K02": None, "C01": True}, "B": {"B01": False, "C01": True, "BC01": False}}


def write_two(tmp_path, b_doc=None, split=KEY_SPLIT):
    ka, ev_a = write_inputs(tmp_path)
    kb = tmp_path / "key-b.json"
    kb.write_text(json.dumps(b_doc or key_doc_b()))
    ev_b = tmp_path / "evidence-b.json"
    ev_b.write_text(json.dumps({"items": {"B01": {"status": "present"}}}))
    ks = tmp_path / "split.json"
    ks.write_text(json.dumps(split))
    return ka, ev_a, kb, ev_b, ks


class SetBGateway(FakeGateway):
    """reflect 한 응답이 두 세트 항목을 함께 말한다: K01·B01(끝난 일)이 열림, 대조군 C01 열림, BC01 은 빠짐."""

    def __init__(self, text="## OPEN\n- alpha-kw pending\n- bravo-kw pending\n- gamma-kw\n## DONE\n- beta-kw\n"):
        super().__init__()
        self.text = text

    def call(self, tool, args):
        self.calls.append(tool)
        if tool == "reflect":
            return {"text": self.text, "based_on": {"world": [mem("x")]}}
        return {"results": [mem("[인계] alpha-kw beta-kw gamma-kw bravo-kw echo-kw 남음", tags=["kind:handoff"])]}


def _score_two(tmp_path, monkeypatch, up, extra, gw=None, out="o", split=True, b_doc=None):
    ka, ev_a, kb, ev_b, ks = write_two(tmp_path, b_doc)
    gw = gw or SetBGateway()
    monkeypatch.setattr(A, "build_clients", lambda target, args, env=None: (gw, up))
    argv = ["score", "--answer-key", str(ka), "--answer-key", str(kb), "--evidence", str(ev_a), "--evidence", str(ev_b),
            "--out", str(tmp_path / out), "--bank", "b"] + (["--key-split", str(ks)] if split else []) + extra
    rc = A.main(argv, env=NEXT_ENV)
    run = sorted(p for p in (tmp_path / out).glob("score-*") if (p / "summary.json").exists())
    return rc, (json.loads((run[-1] / "summary.json").read_text()) if run else None), gw, run


def test_combined_set_scores_one_reflect_and_counts_shared_ids_once(tmp_path, monkeypatch):
    rc, s, gw, _ = _score_two(tmp_path, monkeypatch, CopyUpstream(writable=True), ["--paths", "a"])
    assert rc == 0 and gw.calls == ["reflect"]                     # (a) 한 run = reflect 1회를 두 세트가 공유
    assert s["set"] == ["K01", "K02", "B01", "C01", "BC01"]          # 공유 대조군 C01 은 한 번
    assert s["paths"]["a_reflect_list"] == {"B01": "open", "BC01": "not-open", "C01": "open", "K01": "open",
                                            "K02": "not-open"}
    bd = s["breakdown"]["a_reflect_list"]
    assert bd["all"]["closed_open"] == 2 and bd["all"]["controls_open"] == 1 and bd["all"]["controls_total"] == 2
    assert bd["set:A"]["closed_open"] == 1 and bd["set:A"]["controls_total"] == 1
    assert bd["set:B"]["closed_open"] == 1 and bd["set:B"]["controls_total"] == 2


def test_key_split_breaks_counts_by_key_presence(tmp_path, monkeypatch):
    rc, s, _, _ = _score_two(tmp_path, monkeypatch, CopyUpstream(writable=True), ["--paths", "a"])
    bd = s["breakdown"]["a_reflect_list"]
    assert rc == 0
    assert bd["key:true"]["closed_open"] == 1 and bd["key:true"]["closed_total"] == 1        # K01
    assert bd["key:false"]["closed_open"] == 1 and bd["key:false"]["controls_total"] == 1    # B01, BC01
    assert bd["key:null"]["closed_total"] == 1 and bd["key:null"]["closed_open"] == 0        # K02
    assert bd["set:A/key:true"]["closed_open"] == 1 and bd["set:B/key:false"]["closed_open"] == 1
    assert "SECRET-ITEM" not in json.dumps(s) and "alpha-kw" not in json.dumps(s)


def test_key_split_missing_id_or_disagreeing_shared_control_exits(tmp_path, monkeypatch):
    for split in ({"A": {"K01": True, "C01": True}, "B": KEY_SPLIT["B"]},                       # K02 없음
                  {"A": KEY_SPLIT["A"], "B": {"B01": False, "C01": False, "BC01": False}}):    # C01 이 세트마다 다르다
        with pytest.raises(SystemExit):
            A.item_meta(*A.merge_answer_keys([key_doc(), key_doc_b()], ["A", "B"]), split)


@pytest.mark.parametrize("text, want", [
    ("## OPEN\n- alpha-kw pending\n## DONE\n", {"status": "undecidable", "decidable": False, "controls_open": 0}),
    ("## OPEN\n- gamma-kw\n## DONE\n", {"status": "valid", "decidable": True, "controls_open": 1}),
])
def test_a_run_with_zero_open_controls_is_marked_undecidable(tmp_path, monkeypatch, text, want):
    rc, s, _, _ = _score_two(tmp_path, monkeypatch, CopyUpstream(writable=True), ["--paths", "a"], gw=SetBGateway(text))
    assert rc == 0
    assert {k: s["a_run"][k] for k in want} == want


def test_old_single_key_summary_has_no_new_blocks(tmp_path, monkeypatch):
    # 옵션 없는 동작은 그대로(런 1 산출물 rescore 가 같은 결과): breakdown·a_run 은 새 옵션을 줄 때만 붙는다
    rc, s, _, _ = _score(tmp_path, monkeypatch, CopyUpstream(writable=True), [])
    assert rc == 0 and s is not None
    run = sorted((tmp_path / "o").glob("score-*"))[-1]
    full = json.loads((run / "summary.json").read_text())
    assert "breakdown" not in full and "a_run" not in full


# ④ 세트 B id 와 공유 대조군 규칙
@pytest.mark.parametrize("b_over", [
    {"closed": [dict(key_doc()["closed"][0])]},                                         # 세트 A 끝난 항목 id 와 겹침
    {"controls": [dict(key_doc()["controls"][0], keywords=["changed-kw"])]},            # 같은 id 다른 내용
    {"query_instruction": "OTHER-INSTRUCTION"},                                         # 세 값이 다르면 합칠 수 없다
])
def test_set_b_overlap_or_changed_shared_control_exits_1(tmp_path, monkeypatch, b_over):
    up = CopyUpstream(writable=True)
    gw = SetBGateway()
    with pytest.raises(SystemExit) as e:
        _score_two(tmp_path, monkeypatch, up, ["--paths", "a"], gw=gw, b_doc=key_doc_b(**b_over))
    assert isinstance(e.value.code, str) and "answer key" in e.value.code      # 메시지 있는 SystemExit = exit 1
    assert gw.calls == [] and up.writes == 0


def test_shared_control_byte_copy_merges():
    doc, sets = A.merge_answer_keys([key_doc(), key_doc_b()], ["A", "B"])
    assert [i["id"] for i in doc["controls"]] == ["C01", "BC01"]
    assert sets["C01"] == ["A", "B"] and sets["B01"] == ["B"] and sets["K01"] == ["A"]


# ⑤ (b) 페이지는 두 세트 project_hint 합집합으로 한 번
def test_combined_b_creates_union_of_project_pages_once(tmp_path, monkeypatch):
    seen = []

    class Rec(CopyUpstream):
        def request(self, method, path, body=None):
            if method == "POST":
                seen.append(body["name"])
            return super().request(method, path, body)

    rc, s, _, _ = _score_two(tmp_path, monkeypatch, Rec(writable=True), ["--paths", "b"])
    assert rc == 0
    assert sorted(seen) == sorted([A.page_name("proj-a"), A.page_name(None), A.page_name("proj-b")])
    assert set(s["paths"]["b_knowledge_page_status"]) == {"K01", "K02", "B01", "C01", "BC01"}


class SharedPageUpstream(CopyUpstream):
    """이미 있는 페이지의 멘탈 모델이 source_query 를 돌려준다."""

    def __init__(self, writable, pages=None, queries=None):
        super().__init__(writable, pages)
        self.queries = queries or {}

    def request(self, method, path, body=None):
        if "/mental-models/" in path:
            mm = path.split("/mental-models/")[1].split("?")[0]
            if mm in self.queries:
                self.calls.append((method, path))
                return {"content": "## OPEN\n- gamma-kw\n## DONE\n", "source_query": self.queries[mm],
                        "reflect_response": {"based_on": {"memories": [mem("x")]}}}
        return super().request(method, path, body)


@pytest.mark.parametrize("case", ["same-query", "other-query", "no-option"])
def test_page_sharing_same_query_reads_other_query_stops_no_option_keeps_old_stop(tmp_path, monkeypatch, case):
    query = A.page_query(key_doc(), "proj-a") if case != "other-query" else "OTHER QUERY"
    up = SharedPageUpstream(writable=True, pages={A.page_name("proj-a"): "mm-old"}, queries={"mm-old": query})
    rc, s, gw, run = _score(tmp_path, monkeypatch, up, ["--share-pages"] if case != "no-option" else [])
    if case == "same-query":
        assert rc == 0 and up.writes == 1                                  # 전체 페이지만 새로 만든다
        detail = json.loads((run[-1] / "detail.json").read_text())
        assert detail["b_knowledge_page_status"]["proj-a"]["reused"] is True
        assert s["b_knowledge_page_status"]["C01"] == "open"
    else:
        assert rc == 1 and up.writes == 0 and gw.calls == []               # 어떤 질의도 보내기 전에 멈춘다


def test_rescore_two_keys_and_key_split_gives_the_score_summary(tmp_path, monkeypatch):
    rc, s, _, run = _score_two(tmp_path, monkeypatch, CopyUpstream(writable=True), [])
    assert rc == 0
    ka, ev_a, kb, ev_b, ks = write_two(tmp_path)
    monkeypatch.setattr(A, "build_clients", lambda *a: pytest.fail("rescore must not open clients"))
    out = tmp_path / "r"
    assert A.main(["rescore", "--answer-key", str(ka), "--answer-key", str(kb), "--key-split", str(ks),
                   "--detail", str(run[-1] / "detail.json"), "--out", str(out)], env={}) == 0
    r = json.loads(next(out.glob("rescore-*/summary.json")).read_text())
    for k in ("set", "paths", "breakdown", "a_run"):
        assert r[k] == s[k], k


# ---------------------------------------------------------------- judge-arms (합성 summary)

J_CLOSED = ["K01", "K02", "K03", "K04", "K05", "B01", "B02", "B03", "B04", "B05"]
J_CONTROLS = ["C01", "BC01"]


def j_keys(tmp_path):
    def k(closed, controls):
        return {"schema_version": 1, "query_date": "2026-10-28", "ignore_mentioned_at_from": CUTOFF,
                "query_instruction": "SYNTH", "closed": [{"id": i, "item": "SECRET", "keywords": [i.lower()],
                                                          "project_hint": None} for i in closed],
                "controls": [{"id": i, "item": "SECRET", "keywords": [i.lower()], "project_hint": None,
                              "expect": "open"} for i in controls]}
    pa, pb, ps = tmp_path / "ka.json", tmp_path / "kb.json", tmp_path / "ks.json"
    pa.write_text(json.dumps(k(J_CLOSED[:5], ["C01"])))
    pb.write_text(json.dumps(k(J_CLOSED[5:], ["C01", "BC01"])))
    ps.write_text(json.dumps({"A": {"K01": True, "K02": True, "K03": True, "K04": False, "K05": None, "C01": True},
                              "B": {"B01": True, "B02": True, "B03": False, "B04": False, "B05": False,
                                    "C01": True, "BC01": False}}))
    return pa, pb, ps


def j_summary(tmp_path, path, spec, counter=[0]):
    """spec = (끝난 항목 open 수, 대조군 open 수[, invalid 수]). 앞에서부터 open, invalid 는 끝난 항목 뒤쪽."""
    n, c = spec[0], spec[1]
    inv = spec[2] if len(spec) > 2 else 0
    v = {i: ("open" if k < n else "not-open") for k, i in enumerate(J_CLOSED)}
    for i in J_CLOSED[len(J_CLOSED) - inv:]:
        v[i] = "invalid"
    v.update({i: ("open" if k < c else "not-open") for k, i in enumerate(J_CONTROLS)})
    counter[0] += 1
    f = tmp_path / f"s{counter[0]}.json"
    f.write_text(json.dumps({"set": J_CLOSED + J_CONTROLS, "paths": {path: v}}))
    return str(f)


PK = {"a": "a_reflect_list", "b-full": "b_knowledge_page_status", "b-delta": "b_knowledge_page_status",
      "c": "c_recall_status"}


def j_repeat(tmp_path, state="completed", **paths):
    r = {"state": state}
    for p, specs in paths.items():
        p = p.replace("_", "-")
        r[p] = [j_summary(tmp_path, PK[p], s) for s in specs]
    return r


def j_base(tmp_path, a=((3, 1), (2, 1), (4, 1)), b=((3, 2), (3, 2)), c=((4, 2),)):
    return {"role": "baseline", "repeats": [j_repeat(tmp_path, a=a, b_full=b, c=c)]}


def j_treat(tmp_path, reps, baseline="base"):
    return {"role": "treatment", "baseline": baseline, "repeats": reps}


def j_run(tmp_path, arms, rpo=None):
    pa, pb, ps = j_keys(tmp_path)
    m = {"answer_keys": {"A": str(pa), "B": str(pb)}, "key_split": str(ps), "arms": arms}
    if rpo:
        m["read_path_only"] = rpo
    mp = tmp_path / "manifest.json"
    mp.write_text(json.dumps(m))
    return A.judge_arms(json.loads(mp.read_text()), mp.parent)


def rep(tmp_path, a, b, c, state="completed", b_delta=None):
    kw = {"a": a, "b_full": b, "c": c}
    if b_delta:
        kw["b_delta"] = b_delta
    return j_repeat(tmp_path, state=state, **kw)


GOOD = dict(a=((0, 2),) * 3, b=((0, 2),), c=((0, 2),))
BETTER = dict(a=((2, 1),) * 3, b=((0, 2),), c=((1, 2),))          # (b)(c) 가 기준선 - W 아래, (a) 는 + W 안
SAME = dict(a=((3, 1),) * 3, b=((3, 2),), c=((4, 2),))


@pytest.mark.parametrize("reps, want", [
    ([GOOD, GOOD], "pass"),
    ([BETTER, BETTER], "improvement"),
    ([SAME, SAME], "no-effect"),
    ([BETTER, dict(BETTER, c=((1, 1),))], "no-effect"),                                  # 대조군 열림 감소가 개선을 막는다
    ([BETTER, dict(BETTER, state="aborted")], "undecidable"),                             # 완료 반복 1회
    ([dict(BETTER, b=((0, 2, 1),), c=((1, 2, 1),)), BETTER], "undecidable"),             # 판정 불가 경로 2개
    ([dict(BETTER, a=((0, 2, 1), (0, 2), (0, 2))), BETTER], "improvement"),               # (a) 판정 불가 1개 + 나머지 둘 - W
    ([dict(BETTER, a=((0, 2, 1), (0, 2), (0, 2)), c=((4, 2),)), BETTER], "no-effect"),    # (a) 판정 불가 1개 + 하나만 - W
])
def test_judge_arms_verdicts(tmp_path, reps, want):
    rs = [rep(tmp_path, r["a"], r["b"], r["c"], state=r.get("state", "completed")) for r in reps]
    res = j_run(tmp_path, {"base": j_base(tmp_path), "T": j_treat(tmp_path, rs)})
    assert res["arms"]["T"]["verdict"] == want
    for p in ("a", "b-full", "c"):                                     # 판정 불가여도 값은 낸다
        assert "values" in res["arms"]["T"]["paths"][p]


def test_judge_arms_w_is_max_of_repeat_gap_and_baseline_spread(tmp_path):
    base = j_base(tmp_path, a=((1, 1), (4, 1), (2, 1)), b=((3, 2), (5, 2)), c=((4, 2),))
    reps = [rep(tmp_path, ((2, 1),) * 3, ((4, 2),), ((4, 2),)), rep(tmp_path, ((2, 1),) * 3, ((4, 2),), ((4, 2),))]
    res = j_run(tmp_path, {"base": base, "T": j_treat(tmp_path, reps)})
    p = res["arms"]["T"]["paths"]
    assert p["a"]["W"] == 3            # 기준선 (a) 유효 run 범위 4 - 1
    assert p["b-full"]["W"] == 2       # 기준선 두 생성 차 5 - 3
    assert p["c"]["W"] == 0            # (c) 결정적
    assert p["b-full"]["baseline"] == 4.0 and p["a"]["baseline"] == pytest.approx(7 / 3)
    reps2 = [rep(tmp_path, ((2, 1),) * 3, ((1, 2),), ((4, 2),)), rep(tmp_path, ((2, 1),) * 3, ((6, 2),), ((4, 2),))]
    res2 = j_run(tmp_path, {"base": base, "T": j_treat(tmp_path, reps2)})
    assert res2["arms"]["T"]["paths"]["b-full"]["W"] == 5            # 반복 차 |1 - 6| 이 기준선 차 2 보다 크다


@pytest.mark.parametrize("runs, value, valid, zeros, why", [
    (((0, 0), (3, 1), (3, 1), (3, 1)), 3.0, 3, 1, None),                                # 대조군 0 run 은 평균에서 빠진다
    (((1, 0), (2, 1), (2, 1), (2, 1), (9, 1)), 2.0, 3, 1, None),                        # 앞에서부터 유효 3개만
    (((1, 0), (1, 0), (1, 0), (1, 1), (1, 1), (1, 0), (1, 1)), 1.0, 2, 4, "valid-runs-short"),   # 시도 상한 6회
])
def test_judge_arms_a_valid_runs_and_attempt_cap(tmp_path, runs, value, valid, zeros, why):
    res = j_run(tmp_path, {"base": j_base(tmp_path, a=runs)})
    a = res["arms"]["base"]["paths"]["a"]
    assert a["values"][0] == pytest.approx(value) and a["valid_runs"] == [valid] and a["undecidable"] == why
    assert a["zero_control_runs"] == [zeros]


def test_judge_arms_read_path_only_uses_the_main_baseline_as_arm(tmp_path):   # ①
    arms = {"base-v110": j_base(tmp_path),
            "base-main": {"role": "baseline", "repeats": [rep(tmp_path, ((2, 1), (2, 1), (3, 1)), ((0, 2), (0, 2)),
                                                              ((1, 2),))]}}
    res = j_run(tmp_path, arms, rpo={"arm": "base-main", "baseline": "base-v110"})
    r = res["read_path_only"]
    assert r["verdict"] == "improvement"                       # 반복 1회 규칙에 걸리지 않는다(내부 반복으로 W)
    assert r["paths"]["a"]["W"] == 2 and r["paths"]["b-full"]["W"] == 0


def test_judge_arms_treatment_zero_control_runs_block_improvement(tmp_path):   # ②
    a = ((0, 0), (0, 0), (0, 0), (0, 0), (0, 2), (0, 2))
    reps = [rep(tmp_path, a, ((0, 2),), ((1, 2),)), rep(tmp_path, a, ((0, 2),), ((1, 2),))]
    res = j_run(tmp_path, {"base": j_base(tmp_path), "T": j_treat(tmp_path, reps)})
    t = res["arms"]["T"]
    assert t["paths"]["a"]["undecidable"] == "valid-runs-short"
    assert t["a_zero_control_runs"] == [4, 4]
    assert t["verdict"] == "no-effect"                       # (b)(c) 가 - W 여도 개선이 될 수 없다


@pytest.mark.parametrize("reps", [[], ["aborted", "invalid"]])
def test_judge_arms_zero_completed_repeats_is_unmeasured(tmp_path, reps):   # ③
    rs = [rep(tmp_path, **{"a": BETTER["a"], "b": BETTER["b"], "c": BETTER["c"]}, state=s) for s in reps]
    res = j_run(tmp_path, {"base": j_base(tmp_path), "T": j_treat(tmp_path, rs)})
    assert res["arms"]["T"]["verdict"] == "unmeasured"


def test_judge_arms_reports_sets_keys_delta_and_input_hashes(tmp_path):
    reps = [rep(tmp_path, *[BETTER[k] for k in "abc"], b_delta=((5, 2),)),
            rep(tmp_path, *[BETTER[k] for k in "abc"], b_delta=((6, 2),))]
    res = j_run(tmp_path, {"base": j_base(tmp_path), "T": j_treat(tmp_path, reps)})
    t = res["arms"]["T"]
    assert t["paths"]["b-delta"]["values"] == [5.0, 6.0] and "W" not in t["paths"]["b-delta"]
    sc = t["paths"]["b-full"]["scopes"]
    assert sc["all"]["closed_open"] == [0.0, 0.0] and set(sc) >= {"set:A", "set:B", "key:true", "key:false", "key:null"}
    assert len(res["inputs"]) == 6 + 2 * 6 and all(len(h) == 64 for h in res["inputs"].values())


def test_judge_arms_cli_prints_table_and_writes_json(tmp_path, capsys):
    pa, pb, ps = j_keys(tmp_path)
    m = {"answer_keys": {"A": str(pa), "B": str(pb)}, "key_split": str(ps),
         "arms": {"base": j_base(tmp_path), "T": j_treat(tmp_path, [rep(tmp_path, *[GOOD[k] for k in "abc"]) for _ in range(2)])}}
    mp = tmp_path / "m.json"
    mp.write_text(json.dumps(m))
    assert A.main(["judge-arms", "--manifest", str(mp), "--out", str(tmp_path / "o")], env={}) == 0
    out = capsys.readouterr().out
    assert "T" in out and "합격" in out
    assert json.loads(next((tmp_path / "o").glob("judge-*/judgement.json")).read_text())["arms"]["T"]["verdict"] == "pass"


def test_judge_arms_same_summary_listed_twice_exits(tmp_path):
    # 같은 summary 를 기준선 두 생성(또는 (a) 여러 run)으로 세면 W 가 0 으로 줄어든다 - 거부한다
    base = j_base(tmp_path)
    r = base["repeats"][0]
    r["b-full"] = [r["b-full"][0], r["b-full"][0]]
    with pytest.raises(SystemExit):
        j_run(tmp_path, {"base": base})


def test_two_answer_keys_need_one_evidence_each(tmp_path, monkeypatch):
    # 세트 B 고정 세트를 빠뜨리면 B 끝난 항목이 조용히 빠진다 - 질의 전에 argparse 오류
    ka, ev_a, kb, ev_b, ks = write_two(tmp_path)
    gw, up = SetBGateway(), CopyUpstream(writable=True)
    monkeypatch.setattr(A, "build_clients", lambda target, args, env=None: (gw, up))
    with pytest.raises(SystemExit) as e:
        A.main(["score", "--answer-key", str(ka), "--answer-key", str(kb), "--evidence", str(ev_a),
                "--key-split", str(ks), "--out", str(tmp_path / "o"), "--bank", "b", "--paths", "a"], env=NEXT_ENV)
    assert e.value.code == 2 and gw.calls == [] and up.writes == 0


def test_judge_arms_read_path_only_against_itself_exits(tmp_path):
    with pytest.raises(SystemExit):
        j_run(tmp_path, {"base-v110": j_base(tmp_path)}, rpo={"arm": "base-v110", "baseline": "base-v110"})


def test_judge_arms_dot_slash_and_plain_path_are_the_same_file(tmp_path):   # (ii)
    base = j_base(tmp_path)
    r = base["repeats"][0]
    f = r["b-full"][0]
    r["b-full"] = [f, str(tmp_path / "." / f.split("/")[-1])]
    with pytest.raises(SystemExit):
        j_run(tmp_path, {"base": base})


def test_judge_arms_one_summary_under_b_full_and_c_is_allowed(tmp_path):   # (iii)
    # --paths abc 로 만든 summary 하나는 경로마다 다른 판정을 담는다 - 다른 경로 키에 함께 쓰는 것은 정당하다
    f = tmp_path / "abc.json"
    v = {i: "not-open" for i in J_CLOSED}
    v.update({i: "open" for i in J_CONTROLS})
    f.write_text(json.dumps({"set": J_CLOSED + J_CONTROLS,
                             "paths": {"b_knowledge_page_status": v, "c_recall_status": v}}))
    base = j_base(tmp_path)
    base["repeats"][0]["b-full"] = [str(f), base["repeats"][0]["b-full"][1]]
    base["repeats"][0]["c"] = [str(f)]
    res = j_run(tmp_path, {"base": base})
    assert res["arms"]["base"]["paths"]["c"]["values"] == [0.0]


def test_judge_arms_same_file_in_two_repeats_same_path_exits(tmp_path):   # (iv)
    r1 = rep(tmp_path, *[GOOD[k] for k in "abc"])
    r2 = rep(tmp_path, *[GOOD[k] for k in "abc"])
    r2["c"] = list(r1["c"])
    with pytest.raises(SystemExit):
        j_run(tmp_path, {"base": j_base(tmp_path), "T": j_treat(tmp_path, [r1, r2])})


def _ev(tmp_path, name, items):
    p = tmp_path / name
    p.write_text(json.dumps({"items": items}))
    return str(p)


def test_load_evidence_conflicting_status_for_one_id_exits(tmp_path):
    a = _ev(tmp_path, "ea.json", {"K01": {"status": "none"}})
    b = _ev(tmp_path, "eb.json", {"K01": {"status": "present"}})
    with pytest.raises(SystemExit):
        A.load_evidence([a, b])


def test_load_evidence_same_status_for_one_id_passes(tmp_path):
    a = _ev(tmp_path, "ea.json", {"K01": {"status": "present", "kind": "x"}, "C01": {"status": "present"}})
    b = _ev(tmp_path, "eb.json", {"C01": {"status": "present"}, "B01": {"status": "none"}})
    m = A.load_evidence([a, b])
    assert m["K01"]["status"] == "present" and m["C01"]["status"] == "present" and m["B01"]["status"] == "none"


def test_share_pages_node_without_mental_model_is_refused(tmp_path, monkeypatch):
    up = SharedPageUpstream(writable=True, pages={A.page_name("proj-a"): None}, queries={})
    rc, _, gw, _ = _score(tmp_path, monkeypatch, up, ["--share-pages"])
    assert rc == 1 and up.writes == 0 and gw.calls == []


def test_share_pages_mental_model_lookup_error_is_refused_not_a_traceback(tmp_path, monkeypatch):
    class Gone(SharedPageUpstream):
        def request(self, method, path, body=None):
            if "/mental-models/mm-gone" in path:
                raise A.UpstreamError(404, path)
            return super().request(method, path, body)

    up = Gone(writable=True, pages={A.page_name("proj-a"): "mm-gone"}, queries={})
    rc, _, gw, _ = _score(tmp_path, monkeypatch, up, ["--share-pages"])
    assert rc == 1 and up.writes == 0 and gw.calls == []


# ---------------------------------------------------------------- issue #338: 판정 v2 (런 3 R3-1)
# 사례는 런 2 진단(리포 밖 diag-ab·diag-c)의 줄 모양을 합성 문장으로 옮겼다(항목 이름·기억 본문 없음).

import os
import shutil
from pathlib import Path

FIX = Path(__file__).parent / "fixtures" / "arch-v2-judge"
GOLDEN_RUNS = {
    "single": (["key-a.json"], [], "detail-a.json"),
    "two": (["key-a.json", "key-b.json"], [], "detail-ab.json"),
    "split": (["key-a.json", "key-b.json"], ["--key-split", str(FIX / "split.json")], "detail-ab.json"),
}


def _rescore(tmp_path, keys, extra, det, out="o"):
    argv = ["rescore", "--detail", str(det), "--out", str(tmp_path / out)]
    for k in keys:
        argv += ["--answer-key", str(k)]
    assert A.main(argv + extra, env={}) == 0
    run = sorted((tmp_path / out).glob("rescore-*"))[-1]
    return json.loads((run / "summary.json").read_text()), run


@pytest.mark.parametrize("name", sorted(GOLDEN_RUNS))
@pytest.mark.parametrize("judge", [[], ["--judge", "v1"], ["--judge", "v2"]])
def test_v1_rescore_matches_golden_made_before_v2(tmp_path, name, judge):
    # 골든 = 판정 v2 이전 도구(77bca59)로 같은 detail.json 을 옵션 없이 rescore 한 summary(created·source 제외)
    keys, extra, det = GOLDEN_RUNS[name]
    got, _ = _rescore(tmp_path, [FIX / k for k in keys], extra + judge, FIX / det)
    want = json.loads((FIX / f"golden-v1-{name}.json").read_text())
    assert got.pop("source") == str(FIX / det) and got.pop("created")
    v2 = got.pop("v2", None)
    assert got == want                                   # v1 블록·필드는 바이트 단위로 그대로(JSON 비교)
    assert (v2 is not None) == (judge == ["--judge", "v2"])


def test_v2_block_shape_and_three_way_verdicts(tmp_path):
    got, run = _rescore(tmp_path, [FIX / "key-a.json"], ["--judge", "v2"], FIX / "detail-a.json")
    v2 = got["v2"]
    assert v2["judge"] == "v2" and set(v2["paths"]) == set(A.PATHS)
    assert {v for p in v2["paths"].values() for v in p.values()} <= set(A.V2_VERDICTS)
    a = v2["paths"]["a_reflect_list"]
    # 하위 제목 아래 맨 번호 대조군은 open(v1 은 not-open). #901 은 괄호·선후 참조·다른 이슈 하위 줄에만 OPEN 에 있고,
    # DONE 절 맨 번호 줄은 프로젝트 문맥(하위 제목·묶음 줄)이 없어 unknown
    assert got["paths"]["a_reflect_list"]["C01"] == "not-open" and a["C01"] == "open"
    assert a["K01"] == "unknown" and a["K02"] == "open" and a["K03"] == "done"
    assert v2["counts"]["a_reflect_list"]["controls"]["open"] == 2
    assert (run / "reasons-v2.json").exists()
    assert v2["paths"]["b_knowledge_page_status"]["K04"] == "invalid"        # page-missing 은 v1 과 같다


KNOWN = A.v2_known_names({"closed": [], "controls": [{"id": "Z", "keywords": ["other-repo #1"], "project_hint": "proj-z"},
                                                     {"id": "Y", "keywords": ["k"], "project_hint": "proj-a"}]})


def _item(kws, hint="proj-a", iid="X1"):
    return {"id": iid, "keywords": kws, "project_hint": hint}


def _sec(text, item, scoped=False):
    return A.judge_sections_v2(text, [mem("x")], [item], A.parse_ts(CUTOFF), True, KNOWN, scoped)[item["id"]]


@pytest.mark.parametrize("case, text, kws, want", [
    # 진단 5절: 하위 제목(### <프로젝트>) 아래 맨 번호(대조군 open) - v1 은 같은 줄에 프로젝트 이름을 요구해 놓쳤다
    ("subheading-bare-number", "## OPEN\n### proj-a\n- **#77**: still waiting on a decision.\n", ["#77"], "open"),
    # 굵은 묶음 줄(프로젝트 이름) 아래 맨 번호
    ("bold-group-bare-number", "## OPEN\n**proj-a**\n\n- **#77 (trigger awaiting reboot):** no run yet.\n", ["#77"], "open"),
    # 묶음 줄이 다른 프로젝트면 그 아래 맨 번호는 이 프로젝트 것이 아니다
    ("other-group-bare-number", "## OPEN\n**proj-z**\n\n- **#77:** follow-up remains.\n", ["#77"], "unknown"),
    # 바로 위 굵은 묶음 줄(이슈 번호 없음)의 하위 줄 맨 번호
    ("waiting-group-child", "## OPEN\n### proj-a\n- **Waiting on the owner**:\n  - #77\n  - #78\n", ["#77"], "open"),
    # 다른 이름이 붙은 번호(`proj-z #77`)는 하위 제목 문맥을 물려받아도 이 항목이 아니다
    ("qualified-other-number", "## OPEN\n### proj-a\n- **proj-z #77**: open in the other repo.\n", ["#77"], "unknown"),
    # 괄호 안 keyword 는 주어가 아니다(굵은 구간 안 괄호 포함)
    ("paren-in-bold", "## OPEN\n- **proj-a #70 and #71 (checks after #77 shipped)**: pending.\n", ["#77"], "unknown"),
    # 괄호 안이 이 항목 별칭뿐이면 주어다
    ("alias-paren", "## OPEN\n- **Batch of posts (other-repo #68).** Review is pending.\n", ["other-repo #68"], "open"),
    # 선후 참조(after/before/뒤/전) 안 keyword 는 주어가 아니다
    ("after-reference", "## OPEN\n- Cleanup scheduled after the #77 window.\n- proj-a retry is planned.\n", ["#77"], "unknown"),
    ("korean-reference", "## OPEN\n- proj-a 정리는 #77 배포 뒤 진행\n", ["#77"], "unknown"),
    # 다른 이슈 번호가 주어인 줄의 하위 줄은 이 항목 언급이 아니다
    ("other-issue-child", "## OPEN\n### proj-a\n- **#70 (batch run)**\n  - A retry makes sense once #77 lands.\n", ["#77"], "unknown"),
    # DONE 절 주어면 done, 응답에 아예 없으면 unknown + absent
    ("done-section", "## OPEN\n- proj-a other\n## DONE\n- **proj-a #77**: closed 10-16.\n", ["#77"], "done"),
    ("absent", "## OPEN\n- proj-a other\n## DONE\n- proj-a another\n", ["#77"], "absent"),
])
def test_v2_section_rules_from_diagnosis(case, text, kws, want):
    v = _sec(text, _item(kws))
    if want == "absent":
        assert v == {"verdict": "unknown", "reason": "absent", "absent": True}
    else:
        assert v["verdict"] == want, (case, v)


def test_v2_section_context_and_reference_fixes_go_together():
    # 진단 5절 끝: 맥락 상속만 넣으면 다른 이슈 줄의 참조 언급이 새로 열림으로 잡힌다. v2 는 두 보정을 함께 넣는다
    text = "## OPEN\n### proj-a\n- **#70 and #71 (judgments after #77)**\n  - They were blocked until after #77 ended.\n"
    assert _sec(text, _item(["#77"]))["verdict"] == "unknown"
    assert _sec(text, _item(["#70"]))["verdict"] == "open"


def test_v2_project_page_counts_bare_numbers_without_context():
    assert _sec("## OPEN\n- **#77**: pending\n", _item(["#77"]), scoped=True)["verdict"] == "open"
    assert _sec("## OPEN\n- **#77**: pending\n", _item(["#77"]), scoped=False)["verdict"] == "unknown"


def _rec(results, kws=("gamma-kw",), hint=None):
    return A.judge_recall_v2(results, _item(list(kws), hint), A.parse_ts(CUTOFF), True, KNOWN)


@pytest.mark.parametrize("case, results, want, reason", [
    # 진단 diag-c α: 완료 기록 안 후속 문장("남은 과제는 X 관찰")은 X 열림 진술이 아니다
    ("completion-follow-up", [mem("[완료] 배포 확인. 남은 과제는 gamma-kw 7일 관찰이다.", "2026-10-16T00:00:00Z",
                                  ["kind:completion"], "a")], "unknown", "no-status"),
    ("completion-follow-up-vs-older-handoff",
     [mem("[인계] gamma-kw 남음", "2026-10-10T00:00:00Z", ["kind:handoff"], "h"),
      mem("[완료] gamma-kw 머지. 남은 것: gamma-kw 7일 관찰.", "2026-10-16T00:00:00Z", ["kind:completion"], "d")],
     "done", "latest-done"),
    # 선후 참조 안 keyword("gamma-kw 배포 전 ... 필요")는 상태 진술이 아니다
    ("before-reference", [mem("todo 12 gamma-kw 배포 전 정리 필요", "2026-10-10T00:00:00Z", mid="r")], "unknown",
     "no-status"),
    # 따옴표 안 예시 문구는 상태 진술이 아니다
    ("quoted-example", [mem('다음 질문 예: "gamma-kw 남은 작업 이어가자"', None, ["kind:decision"], "q"),
                        mem("[완료] gamma-kw 머지", None, ["kind:completion"], "d")], "done", "undated-done"),
    # diag-c ε: 날짜 없는 열림 행은 날짜 있는 상태 행이 있으면 순위(1위)로 이기지 못한다
    ("undated-cannot-win-by-rank", [mem("[인계] gamma-kw 남음", None, ["kind:handoff"], "u"),
                                    mem("[완료] gamma-kw 머지", "2026-10-01T00:00:00Z", ["kind:completion"], "d")],
     "done", "latest-done"),
    # 모두 날짜가 없고 엇갈리면 순서를 알 수 없다
    ("undated-conflict", [mem("[인계] gamma-kw 남음", None, ["kind:handoff"], "u"),
                          mem("[완료] gamma-kw 머지", None, ["kind:completion"], "d")], "unknown", "undated-conflict"),
    # 근거 없음은 not-open 이 아니라 unknown
    ("no-status", [mem("[결정] gamma-kw 방식", tags=["kind:decision"])], "unknown", "no-status"),
    ("absent", [mem("[완료] unrelated 머지", tags=["kind:completion"])], "unknown", "absent"),
    # 참 열림 진술은 그대로 open
    ("true-open", [mem("[완료] gamma-kw 1차", "2026-10-05T00:00:00Z", ["kind:completion"], "a"),
                   mem("[인계] gamma-kw 2차 남음", "2026-10-18T00:00:00Z", ["kind:handoff"], "b")], "open", "latest-open"),
])
def test_v2_recall_rules_from_diagnosis(case, results, want, reason):
    v = _rec(results)
    assert (v["verdict"], v["reason"]) == (want, reason), case
    assert v.get("absent", False) == (reason == "absent")


def test_v2_recall_tag_only_counts_when_item_is_the_memory_title():
    # 태그(kind:handoff)만으로 열림을 읽는 것은 keyword 가 기억 첫 문장(제목)에 있을 때만 - 다른 일 기억의 지나가는 언급은 아니다
    title = mem("gamma-kw 작업을 새 세션으로 넘겼다. 다른 설명.", "2026-10-10T00:00:00Z", ["kind:handoff"])
    passing = mem("다른 일을 넘겼다. 참고로 gamma-kw 쪽 기록도 있다.", "2026-10-10T00:00:00Z", ["kind:handoff"])
    assert _rec([title])["verdict"] == "open"
    assert _rec([passing])["verdict"] == "unknown"


def test_v2_invalid_reasons_follow_v1():
    assert _rec([mem("[완료] gamma-kw", AFTER, ["kind:completion"])])["verdict"] == "invalid"
    assert _sec("- no heading\n", _item(["#77"]))["reason"] == "format-no-open-heading"


# ---- 경로 등록표: 가짜 경로 d 를 판정 함수 수정 없이 더한다

def _fake_d(doc, items, section, cutoff, undated_ok):
    return {i["id"]: ({"verdict": section[i["id"]]} if i["id"] in section else
                      {"verdict": "unknown", "reason": "absent", "absent": True}) for i in items}


@pytest.fixture
def fake_d(monkeypatch):
    reg = dict(A.V2_REGISTRY)
    reg.pop("d_recall_list", None)       # 실제 (d)(issue #341)를 빼고 가짜 d 로 등록표 확장만 본다
    monkeypatch.setattr(A, "V2_REGISTRY", reg)
    A.register_v2_path(A.V2Path("d_fake_list", "d", "list", "all", _fake_d))
    return reg


def _detail_with_d(tmp_path, name, dverdicts):
    det = json.loads((FIX / "detail-ab.json").read_text())
    det["d_fake_list"] = dverdicts
    p = tmp_path / f"{name}.json"
    p.write_text(json.dumps(det))
    return p


def test_fake_path_d_reaches_summary_pairs_and_table(tmp_path, fake_d):
    keys = [FIX / "key-a.json", FIX / "key-b.json"]
    base = _detail_with_d(tmp_path, "base", {"K01": "open", "K02": "open", "C01": "open", "BC01": "open"})
    treat = _detail_with_d(tmp_path, "treat", {"K01": "done", "C01": "open", "BC01": "open"})     # K02 absent
    sb, _ = _rescore(tmp_path, keys, ["--judge", "v2"], base, out="b")
    st, _ = _rescore(tmp_path, keys, ["--judge", "v2"], treat, out="t")
    assert "d_fake_list" not in sb["paths"]                          # v1 블록은 모른다
    assert sb["v2"]["paths"]["d_fake_list"]["K02"] == "open" and st["v2"]["absent"]["d_fake_list"][:1] == ["B01"]
    assert st["v2"]["counts"]["d_fake_list"]["closed"]["absent"] == 5
    (tmp_path / "sb.json").write_text(json.dumps(sb))
    (tmp_path / "st.json").write_text(json.dumps(st))
    man = {"answer_keys": {"A": str(keys[0]), "B": str(keys[1])},
           "arms": {"base": {"role": "baseline", "repeats": [{"state": "completed", "d": ["sb.json"]}]},
                    "X": {"role": "treatment", "baseline": "base", "repeats": [{"state": "completed", "d": ["st.json"]}]}}}
    (tmp_path / "m.json").write_text(json.dumps(man))
    res = A.judge_pairs(man, tmp_path, {"d": 0})
    r = res["pairs"]["X"]["paths"]["d"]["repeats"][0]
    assert r["transitions"]["closed"]["open_to_done"] == 1 and r["transitions"]["closed"]["open_to_unknown"] == 1
    assert r["gain"] == 2 and r["effect"] is True                    # 목록 경로: done + unknown, 대조군 손실 0 <= 0
    assert "| d | 1 |" in A.render_pairs(res)


# ---- partial 범주

def _key_with_partial(tmp_path, name="key-p.json", pid="P01", kws=("pi-kw",)):
    d = json.loads((FIX / "key-a.json").read_text())
    d["partial"] = [{"id": pid, "item": "SYNTH-ITEM-P", "keywords": list(kws), "project_hint": None,
                     "done_external": "x", "bank_evidence": None}]
    p = tmp_path / name
    p.write_text(json.dumps(d))
    return p


def test_partial_is_its_own_category_and_left_out_of_v1(tmp_path):
    kp = _key_with_partial(tmp_path)
    det = json.loads((FIX / "detail-a.json").read_text())
    det["meta"]["set"].append("P01")
    det["a_reflect_list"]["response"]["text"] += "\n## OPEN (cont)\n- pi-kw remainder still open\n"
    det["c_recall_status"]["P01"] = {"query": "q", "results": [mem("[인계] pi-kw 남음", tags=["kind:handoff"])]}
    dp = tmp_path / "d.json"
    dp.write_text(json.dumps(det))
    got, _ = _rescore(tmp_path, [kp], ["--judge", "v2"], dp)
    gold = json.loads((FIX / "golden-v1-single.json").read_text())
    assert got["paths"] == gold["paths"]                              # v1 집계에서 빠진다(v1 출력 불변)
    assert "P01" in got["set"] and got["v2"]["paths"]["c_recall_status"]["P01"] == "open"
    c = got["v2"]["counts"]["c_recall_status"]
    assert c["partial"] == {"total": 1, "open": 1, "done": 0, "unknown": 0, "absent": 0, "invalid": 0}
    assert c["closed"]["total"] == 4                                  # 끝난 항목 집계에 섞지 않는다


def test_no_partial_field_is_unchanged():
    d = json.loads((FIX / "key-a.json").read_text())
    merged, _ = A.merge_answer_keys([d], ["A"])
    assert "partial" not in merged and A.partial_ids(merged) == set()
    assert [i["id"] for i in A.active_items(merged, {"K01": {"status": "present"}})] == ["K01", "C01", "C02"]


def test_partial_merges_across_keys_and_overlap_exits(tmp_path):
    a = json.loads(_key_with_partial(tmp_path).read_text())
    b = json.loads((FIX / "key-b.json").read_text())
    b["partial"] = [{"id": "P02", "item": "SYNTH-ITEM-Q", "keywords": ["qi-kw"], "project_hint": None}]
    merged, sets = A.merge_answer_keys([a, b], ["A", "B"])
    assert [i["id"] for i in merged["partial"]] == ["P01", "P02"] and sets["P02"] == ["B"]
    assert {i["id"] for i in A.all_items(merged)} >= {"P01", "P02"}
    assert A.item_meta(merged, sets)["P01"] == {"control": False, "sets": ["A"], "partial": True}
    for clash in ("P01", "K01", "B01"):                               # partial 끼리, partial 과 끝난 항목은 겹치면 안 된다
        b2 = dict(b, partial=[{"id": clash, "item": "x", "keywords": ["z"], "project_hint": None}])
        with pytest.raises(SystemExit):
            A.merge_answer_keys([a, b2], ["A", "B"])


# ---- judge-pairs: 기준선 반복 2개 짝 규칙(불안정 제외), 이득, 영분포

def _v2sum(tmp_path, name, verdicts):
    s = {"set": sorted(verdicts), "paths": {}, "v2": {"judge": "v2", "paths": {"c_recall_status": verdicts,
                                                                               "a_reflect_list": verdicts}}}
    (tmp_path / name).write_text(json.dumps(s))
    return name


def _pairs_manifest(tmp_path, base_reps, treat_reps, key="c"):
    def reps(rs):
        return [{"state": "completed", key: [_v2sum(tmp_path, f"{key}-{id(r)}-{k}.json", v) for k, v in enumerate(r)]}
                for r in rs]
    return {"answer_keys": {"A": str(FIX / "key-a.json")},
            "arms": {"base": {"role": "baseline", "repeats": reps(base_reps)},
                     "X": {"role": "treatment", "baseline": "base", "repeats": reps(treat_reps)}}}


def test_pairs_two_baseline_repeats_drop_unstable_items(tmp_path):
    b1 = {"K01": "open", "K02": "open", "K03": "open", "K04": "done", "C01": "open", "C02": "open"}
    b2 = {"K01": "open", "K02": "unknown", "K03": "open", "K04": "unknown", "C01": "open", "C02": "open"}
    t1 = {"K01": "done", "K02": "done", "K03": "open", "K04": "open", "C01": "open", "C02": "unknown"}
    t2 = {"K01": "unknown", "K02": "done", "K03": "done", "K04": "done", "C01": "open", "C02": "open"}
    man = _pairs_manifest(tmp_path, [[b1], [b2]], [[t1], [t2]])
    res = A.judge_pairs(man, tmp_path, {"c": 0})["pairs"]["X"]
    assert res["baseline_repeats"] == 2
    r1, r2 = res["paths"]["c"]["repeats"]
    assert r1["transitions"]["closed"] == {"open_to_done": 1, "open_to_unknown": 0, "open_kept": 1, "notopen_to_open": 1,
                                           "notopen_kept": 0, "unstable": 1, "invalid": 0}
    assert r1["control_loss"] == 1 and r1["gain"] == 1 and r1["effect"] is True     # (c) 는 open -> done 만, 손실 조건 없음
    assert r2["transitions"]["closed"]["open_to_unknown"] == 1 and r2["gain"] == 1
    assert res["paths"]["c"]["baseline_states"] == {"open": 4, "not-open": 1, "unstable": 1, "invalid": 0}
    assert res["paths"]["c"]["effect"] is True


@pytest.mark.parametrize("nulls, loss_item, want_gain, status", [
    (None, "open", None, "no-null"),
    ({"a": 1}, "open", 2, "ok"),
    ({"a": 1}, "loss1", 2, "ok"),                     # 대조군 손실 1 <= 영분포 1
    ({"a": 0}, "loss1", 0, "control-loss>null"),      # 손실이 영분포를 넘으면 이득 0, 효과 False(판정 불가 아님)
])
def test_pairs_list_path_gain_needs_null_and_control_loss_within_it(tmp_path, nulls, loss_item, want_gain, status):
    base = {"K01": "open", "K02": "open", "K03": "done", "K04": "done", "C01": "open", "C02": "open"}
    treat = {"K01": "done", "K02": "unknown", "K03": "done", "K04": "done", "C01": "open", "C02": "open"}
    # (a) 는 반복마다 유효 run 3개(대조군 open 1건 이상) 다수결. loss1: C02 가 3 run 중 1 run 만 open -> 손실 1
    runs = [dict(base)] * 3
    truns = [dict(treat)] * 3 if loss_item != "loss1" else [dict(treat, C02="unknown"), dict(treat, C02="done"), dict(treat)]
    man = _pairs_manifest(tmp_path, [runs], [truns], key="a")
    r = A.judge_pairs(man, tmp_path, nulls)["pairs"]["X"]["paths"]["a"]["repeats"][0]
    assert (r["gain"], r["status"], r["raw"]) == (want_gain, status, 2)
    assert r["effect"] == (None if nulls is None else want_gain > nulls["a"])


def test_pairs_a_majority_tie_goes_open_and_short_valid_runs_is_undecidable(tmp_path):
    st = A.pair_repeat_state(A.V2_REGISTRY["a_reflect_list"],
                             [{"K01": "open", "C01": "open"}, {"K01": "done", "C01": "open"},
                              {"K01": "done", "C01": "unknown"},               # 대조군 open 0 -> 집계 밖
                              {"K01": "open", "C01": "open"}, {"K01": "done", "C01": "open"}], {"C01"})
    assert st["used"] == 3 and st["items"]["K01"] == "open" and st["undecidable"] is None     # 앞 유효 3 run 중 open 2
    b = A.pair_repeat_state(A.V2_REGISTRY["b_knowledge_page_status"],
                            [{"K01": "open"}, {"K01": "unknown"}], {"C01"})
    assert b["items"]["K01"] == "open"                                # b-full 두 생성 동률 -> open
    short = A.pair_repeat_state(A.V2_REGISTRY["a_reflect_list"], [{"K01": "open", "C01": "open"}], {"C01"})
    assert short["undecidable"] == "valid-runs-short"


def test_pairs_need_v2_block_and_cli_needs_judge_v2(tmp_path):
    (tmp_path / "s.json").write_text(json.dumps({"set": [], "paths": {}}))
    man = {"answer_keys": {"A": str(FIX / "key-a.json")},
           "arms": {"base": {"role": "baseline", "repeats": [{"state": "completed", "c": ["s.json"]}]},
                    "X": {"role": "treatment", "baseline": "base", "repeats": [{"state": "completed", "c": ["s.json"]}]}}}
    with pytest.raises(SystemExit, match="rescore --judge v2"):
        A.judge_pairs(man, tmp_path)
    (tmp_path / "m.json").write_text(json.dumps(man))
    with pytest.raises(SystemExit):
        A.main(["judge-pairs", "--manifest", str(tmp_path / "m.json")], env={})


def test_judge_pairs_cli_writes_private_output_and_tilde_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    b = {"K01": "open", "C01": "open"}
    man = _pairs_manifest(tmp_path, [[b]], [[dict(b, K01="done")]])
    (tmp_path / "m.json").write_text(json.dumps(man))
    (tmp_path / "null.json").write_text(json.dumps({"c": 0}))
    rc = A.main(["judge-pairs", "--judge", "v2", "--manifest", str(tmp_path / "m.json"), "--null",
                 str(tmp_path / "null.json"), "--out", str(tmp_path / "o")], env={})
    out = sorted((tmp_path / "o").glob("pairs-*"))[-1]
    res = json.loads((out / "pairs.json").read_text())
    assert rc == 0 and all(k.startswith("~/") for k in res["inputs"]) and res["manifest"]["path"] == "~/m.json"
    assert oct(out.stat().st_mode & 0o777) == "0o700"


def test_display_path_is_repo_relative_or_tilde(monkeypatch, tmp_path):
    assert A.display_path(os.path.join(A.REPO, "bench", "x.json")) == os.path.join("bench", "x.json")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert A.display_path(str(tmp_path / "a" / "s.json")) == "~/a/s.json"


def test_judge_arms_render_lists_inputs_without_home_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    res = {"arms": {}, "inputs": {str(tmp_path / "x" / "s.json"): "ab" * 32}}
    assert A.render_judgement(res).splitlines()[-1] == f"- {'ab' * 32}  ~/x/s.json"


# ---- issue #338 final-review 재작업(20261101-034209 판정)

def test_known_names_are_normalized_for_the_qualifier_check():
    assert {"projz", "otherrepo", "proja"} <= KNOWN


@pytest.mark.parametrize("case, text, want", [
    # 하이픈 낱말은 다른 프로젝트 이름이 아니다
    ("hyphen-word-is-not-a-project", "## OPEN\n### proj-a\n- follow-up #77 pending\n", "open"),
    # 묶음 줄 범위는 들여쓰기 없는 일반 문단에서 끝난다(묶음 줄의 이슈 번호가 뒤 줄을 지우지 않는다)
    ("group-ends-at-paragraph", "## OPEN\n### proj-a\nWaiting on #40:\n- #41 blocked\nOther notes follow here.\n- #77 still pending\n",
     "open"),
    # 표 줄은 묶음 범위를 끊지 않는다(묶음 줄의 프로젝트 문맥을 물려받는다)
    ("table-under-group", "## OPEN\n**proj-a**\n\n| id | state |\n|---|---|\n| #77 | pending |\n", "open"),
    # 조상 줄 주어의 번호가 이 항목 자신이면 그 하위 줄을 빼지 않는다
    ("own-number-ancestor", "## OPEN\n### proj-a\n- **Work after #77 merge**:\n  - #77 rescore tests pending\n",
     "open"),
])
def test_v2_final_review_rework_section_cases(case, text, want):
    assert _sec(text, _item(["#77"]))["verdict"] == want, case


def test_pairs_item_path_without_null_has_no_gain(tmp_path):
    b = {"K01": "open", "C01": "open"}
    man = _pairs_manifest(tmp_path, [[b]], [[dict(b, K01="done")]])
    r = A.judge_pairs(man, tmp_path)["pairs"]["X"]["paths"]["c"]["repeats"][0]
    assert (r["gain"], r["status"], r["raw"], r["effect"]) == (None, "no-null", 1, None)


def test_pairs_item_set_mismatch_or_duplicate_file_exits(tmp_path):
    b = {"K01": "open", "C01": "open"}
    man = _pairs_manifest(tmp_path, [[b]], [[{"K01": "done"}]])
    with pytest.raises(SystemExit, match="different item set"):
        A.judge_pairs(man, tmp_path)
    man = _pairs_manifest(tmp_path, [[b]], [[b]])
    man["arms"]["X"]["repeats"][0]["c"] = man["arms"]["base"]["repeats"][0]["c"] * 2
    with pytest.raises(SystemExit, match="more than once"):
        A.judge_pairs(man, tmp_path)


def test_pairs_missing_path_is_reported_not_dropped(tmp_path):
    b = {"K01": "open", "C01": "open"}
    man = _pairs_manifest(tmp_path, [[b]], [[dict(b, K01="done")], [dict(b, K01="done")]])
    del man["arms"]["X"]["repeats"][1]["c"]
    x = A.judge_pairs(man, tmp_path, {"c": 0})["pairs"]["X"]["paths"]["c"]
    assert [r["status"] for r in x["repeats"]] == ["ok", "missing"] and x["effect"] is None
    man2 = _pairs_manifest(tmp_path, [[b]], [[dict(b, K01="done")]])
    man2["arms"]["X"]["repeats"][0]["a"] = man2["arms"]["X"]["repeats"][0]["c"]     # 기준선에 없는 경로 키
    y = A.judge_pairs(man2, tmp_path, {"c": 0})["pairs"]["X"]["paths"]
    assert y["a"]["repeats"][0]["status"] == "baseline-missing" and y["a"]["effect"] is None


def test_judge_arms_json_has_no_home_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(A, "judge_arms", lambda m, d: {"arms": {}, "inputs": {str(tmp_path / "x" / "s.json"): "ab" * 32}})
    (tmp_path / "m.json").write_text("{}")
    assert A.main(["judge-arms", "--manifest", str(tmp_path / "m.json"), "--out", str(tmp_path / "o")], env={}) == 0
    res = json.loads(next((tmp_path / "o").glob("judge-*/judgement.json")).read_text())
    assert list(res["inputs"]) == ["~/x/s.json"] and res["manifest"]["path"] == "~/m.json"


def test_rescore_with_evidence_keeps_unscored_partial_out_without_exit(tmp_path):
    # 세트 C(R3-2)는 partial 을 가진다. partial 은 근거 확인 대상이 아니므로 --evidence 로 세트를 좁혀도 걸리지 않는다
    kp = _key_with_partial(tmp_path)
    ev = tmp_path / "ev.json"
    ev.write_text(json.dumps({"items": {k: {"status": "present"} for k in ("K01", "K02", "K03", "K04")}}))
    got, _ = _rescore(tmp_path, [kp], ["--evidence", str(ev)], FIX / "detail-a.json")
    assert got["paths"] == json.loads((FIX / "golden-v1-single.json").read_text())["paths"]


# ---------------------------------------------------------------- 경로 (d)와 시험지 v2 선정 (issue #341, 런 3 R3-2)

LIST_QUERY = "SYNTH-LIST-QUERY broad open-work request"


def _d(results, kws=("gamma-kw",), hint=None):
    """경로 (d) 판정: 넓은 질의 recall 결과 하나로 항목마다 (c) 최신 상태 규칙 v2 를 쓴다."""
    doc = {"closed": [], "controls": []}
    it = _item(list(kws), hint)
    return A.V2_REGISTRY["d_recall_list"].judge(doc, [it], {"query": "q", "results": results},
                                               A.parse_ts(CUTOFF), True)[it["id"]]


def test_path_d_is_registered_as_list_path_with_all_runs():
    p = A.V2_REGISTRY["d_recall_list"]
    assert (p.short, p.kind, p.runs) == ("d", "list", "all")
    assert A.pair_path_of("d") is p and A.pair_path_of("d-x") is p


@pytest.mark.parametrize("case, results, want, reason", [
    # 결과 안 최신 상태 진술: 옛 인계(open) 뒤 새 완료(done)면 done, 옛 완료 뒤 새 인계면 open
    ("latest-done", [mem("[인계] gamma-kw 남음", "2026-10-10T00:00:00Z", ["kind:handoff"], "h"),
                     mem("[완료] gamma-kw 머지", "2026-10-16T00:00:00Z", ["kind:completion"], "d")], "done", "latest-done"),
    ("latest-open", [mem("[완료] gamma-kw 1차", "2026-10-05T00:00:00Z", ["kind:completion"], "a"),
                     mem("[인계] gamma-kw 2차 남음", "2026-10-18T00:00:00Z", ["kind:handoff"], "b")], "open", "latest-open"),
    # 결과에 keyword 가 없다 = absent(unknown 의 하위 표시). 목록 경로에서는 끝난 일이 안 나오는 것이 맞는 답이다
    ("absent", [mem("[인계] other-kw 남음", "2026-10-10T00:00:00Z", ["kind:handoff"])], "unknown", "absent"),
    # 날짜 없는 행: 날짜 있는 상태 진술이 있으면 고르지 않는다, 모두 날짜 없으면 하나로 같을 때만, 엇갈리면 unknown
    ("undated-loses-to-dated", [mem("[인계] gamma-kw 남음", None, ["kind:handoff"], "u"),
                                mem("[완료] gamma-kw 머지", "2026-10-01T00:00:00Z", ["kind:completion"], "d")],
     "done", "latest-done"),
    ("undated-only", [mem("[인계] gamma-kw 남음", None, ["kind:handoff"], "u")], "open", "undated-open"),
    ("undated-conflict", [mem("[인계] gamma-kw 남음", None, ["kind:handoff"], "u"),
                          mem("[완료] gamma-kw 머지", None, ["kind:completion"], "d")], "unknown", "undated-conflict"),
    ("no-status", [mem("[결정] gamma-kw 방식", tags=["kind:decision"])], "unknown", "no-status"),
])
def test_path_d_judges_one_result_set_with_c_rule(case, results, want, reason):
    v = _d(results)
    assert (v["verdict"], v["reason"]) == (want, reason), case
    assert v.get("absent", False) == (reason == "absent")
    # (c) 규칙 그대로: 같은 결과를 (c) 판정 함수에 넣은 값과 같다(동결 함수 재사용)
    assert v == A.judge_recall_v2(results, _item(["gamma-kw"], None), A.parse_ts(CUTOFF), True,
                                  A.v2_known_names({"closed": [], "controls": []}))


@pytest.mark.parametrize("results, reason", [([], "no-provenance"),
                                             ([mem("[완료] gamma-kw", AFTER, ["kind:completion"])], "post-cutoff-evidence")])
def test_path_d_invalid_follows_c(results, reason):
    assert _d(results) == {"verdict": "invalid", "reason": reason}


def _d_key(tmp_path, list_query=LIST_QUERY):
    d = key_doc()
    if list_query is not None:
        d["list_query"] = list_query
    kp = tmp_path / "key-d.json"
    kp.write_text(json.dumps(d))
    ev = tmp_path / "ev-d.json"
    ev.write_text(json.dumps({"items": {"K01": {"status": "present"}, "K02": {"status": "present"}}}))
    return kp, ev


class ListGateway(FakeGateway):
    def __init__(self):
        super().__init__()
        self.args = []

    def call(self, tool, args):
        self.calls.append(tool)
        self.args.append(args)
        return {"results": [mem("[인계] alpha-kw 남음", "2026-10-10T00:00:00Z", ["kind:handoff"], "h"),
                            mem("[완료] beta-kw 머지", "2026-10-12T00:00:00Z", ["kind:completion"], "d")]}


def test_score_paths_d_judge_v2_sends_one_gateway_recall(tmp_path, monkeypatch):
    kp, ev = _d_key(tmp_path)
    gw, up = ListGateway(), CopyUpstream(writable=True)
    monkeypatch.setattr(A, "build_clients", lambda target, args, env=None: (gw, up))
    assert A.main(["score", "--answer-key", str(kp), "--evidence", str(ev), "--out", str(tmp_path / "o"),
                   "--bank", "b", "--paths", "d", "--judge", "v2"], env=NEXT_ENV) == 0
    assert gw.calls == ["recall"] and up.writes == 0
    a = gw.args[0]
    assert a["query_timestamp"] == A.QUERY_TIMESTAMP and a["max_tokens"] == A.D_MAX_TOKENS
    assert a["query"] == key_doc()["query_instruction"] + "\n" + LIST_QUERY
    run = next((tmp_path / "o").glob("score-*"))
    s = json.loads((run / "summary.json").read_text())
    assert s["paths"] == {}                                           # v1 블록은 (d)를 모른다
    assert s["v2"]["paths"]["d_recall_list"] == {"K01": "open", "K02": "done", "C01": "unknown"}
    assert s["v2"]["absent"]["d_recall_list"] == ["C01"]
    m = json.loads((run / "metrics.json").read_text())["d_recall_list"]
    assert m["queries"] == 1
    det = json.loads((run / "detail.json").read_text())["d_recall_list"]
    assert det["query"] == a["query"] and len(det["results"]) == 2
    raw = (run / "summary.json").read_text()
    assert "SYNTH-LIST-QUERY" not in raw and "alpha-kw" not in raw
    # 같은 원문을 다시 판정(질의 0회)하면 같은 v2 판정
    monkeypatch.setattr(A, "build_clients", lambda *x, **k: pytest.fail("rescore must not open clients"))
    got, _ = _rescore(tmp_path, [kp], ["--judge", "v2"], run / "detail.json", out="r")
    assert got["v2"]["paths"] == s["v2"]["paths"]


def test_score_paths_d_without_list_query_refuses_before_any_query(tmp_path, monkeypatch):
    kp, ev = _d_key(tmp_path, list_query=None)
    gw = ListGateway()
    monkeypatch.setattr(A, "build_clients", lambda target, args, env=None: (gw, CopyUpstream(writable=True)))
    assert A.main(["score", "--answer-key", str(kp), "--evidence", str(ev), "--out", str(tmp_path / "o"),
                   "--bank", "b", "--paths", "ad", "--judge", "v2"], env=NEXT_ENV) == 1
    assert gw.calls == []


def test_default_paths_stay_abc(tmp_path, monkeypatch):
    kp, ev = _d_key(tmp_path)
    gw = RegressingGateway()
    monkeypatch.setattr(A, "build_clients", lambda target, args, env=None: (gw, CopyUpstream(writable=True)))
    assert A.main(["score", "--answer-key", str(kp), "--evidence", str(ev), "--out", str(tmp_path / "o"),
                   "--bank", "b"], env=NEXT_ENV) == 0
    det = json.loads(next((tmp_path / "o").glob("score-*/detail.json")).read_text())
    assert "d_recall_list" not in det and gw.calls.count("recall") == 3


def test_list_query_merges_from_any_key_and_disagreement_exits():
    a, b = (json.loads((FIX / f).read_text()) for f in ("key-a.json", "key-b.json"))
    b["list_query"] = LIST_QUERY
    merged, _ = A.merge_answer_keys([a, b], ["A", "B"])
    assert merged["list_query"] == LIST_QUERY
    a["list_query"] = "other"
    with pytest.raises(SystemExit):
        A.merge_answer_keys([a, b], ["A", "B"])


# ---- evidence --controls: 대조군 열림 근거, partial 완료 근거

def test_open_evidence_counts_only_open_before_cutoff():
    item = key_doc()["controls"][0]
    results = {"gamma-kw": [mem("[완료] gamma-kw 머지", tags=["kind:completion"], mid="d"),
                            mem("[인계] gamma-kw 남음", when=AFTER, tags=["kind:handoff"], mid="late"),
                            mem("[인계] gamma-kw 남음", tags=["kind:handoff"], mid="ok")]}
    ev = A.open_evidence_for_item(item, results, A.parse_ts(CUTOFF))
    assert ev["status"] == "present" and [e["memory_id"] for e in ev["evidence"]] == ["ok"]
    assert ev["rejected"] == {"post_cutoff": 1, "undated": 0, "not_open": 1, "no_keyword": 0}
    none = A.open_evidence_for_item(item, {"gamma-kw": results["gamma-kw"][:2]}, A.parse_ts(CUTOFF))
    assert none["status"] == "none" and none["reason"] == "no-open-memory-before-cutoff"


def test_evidence_controls_flag_adds_control_and_partial_rows(tmp_path, monkeypatch):
    d = key_doc()
    d["partial"] = [{"id": "P01", "item": "SECRET-ITEM-P", "keywords": ["pi-kw"], "project_hint": None}]
    kp = tmp_path / "k.json"
    kp.write_text(json.dumps(d))

    class Gw(FakeGateway):
        def call(self, tool, args):
            self.calls.append(tool)
            return {"results": [mem("[인계] gamma-kw 남음", tags=["kind:handoff"], mid="o"),
                                mem("[완료] pi-kw 본 작업 머지", tags=["kind:completion"], mid="p")]}

    monkeypatch.setattr(A, "build_clients", lambda target, args, env=None: (Gw(), FakeUpstream(True)))
    for extra, want in (([], {"K01", "K02"}), (["--controls"], {"K01", "K02", "C01", "P01"})):
        out = tmp_path / ("o" + "".join(extra))
        assert A.main(["evidence", "--answer-key", str(kp), "--out", str(out), "--bank", "b"] + extra, env=NEXT_ENV) == 0
        s = json.loads(next(out.glob("evidence-*/summary.json")).read_text())
        assert set(s["items"]) == want
    assert s["items"]["C01"]["status"] == "present" and s["items"]["P01"]["status"] == "present"
    assert s["items"]["K01"]["status"] == "none"
    assert "SECRET-ITEM" not in json.dumps(s)


# ---- select: 선정 run 결과로 세트 C 정답표·고정 세트·키 표를 만든다(질의 0회, 정답표 입력 없음)

def _cand(iid, kws, hint=None):
    return {"id": iid, "item": f"SECRET-{iid}", "keywords": kws, "project_hint": hint}


def _select_inputs(tmp_path, sel_paths, evidence=None):
    doc = {k: v for k, v in key_doc().items() if k not in ("closed", "controls", "excluded")}
    doc["list_query"] = LIST_QUERY
    doc["closed"] = [_cand("K09", ["#3"], "proj-a"),                       # 원래 증상(세트 A 바이트 복사), 선정 run 과 무관하게 넣는다
                     _cand("S01", ["#11"], "proj-a"), _cand("S02", ["#12"], "proj-a"), _cand("S03", ["#13"], "proj-a"),
                     _cand("S04", ["kl-1"]), _cand("S05", ["kl-2"]), _cand("S06", ["kl-3"])]
    doc["controls"] = [_cand("SC01", ["#21"], "proj-a"), _cand("SC02", ["kl-c"]), _cand("SC03", ["kl-d"])]
    doc["partial"] = [_cand("SP01", ["kl-p"]), _cand("SP02", ["kl-q"])]
    doc["strata"] = {"K09": {"key": True, "source": "symptom"}, "S01": {"key": True, "source": "issue"},
                     "S02": {"key": True, "source": "issue"}, "S03": {"key": True, "source": "issue"},
                     "S04": {"key": False, "source": "completion"}, "S05": {"key": False, "source": "completion"},
                     "S06": {"key": False, "source": "completion"}, "SC01": {"key": True, "source": "symptom"},
                     "SC02": {"key": False, "source": "symptom"}, "SC03": {"key": False, "source": "symptom"},
                     "SP01": {"key": False, "source": "symptom"},
                     "SP02": {"key": False, "source": "symptom"}}
    kp = tmp_path / "cand.json"
    kp.write_text(json.dumps(doc, ensure_ascii=False))
    ev_items = evidence or {"K09": "present", "S01": "present", "S02": "present", "S03": "none", "S04": "present",
                            "S05": "present", "S06": "present", "SC01": "present", "SC02": "none", "SC03": "present",
                            "SP01": "present", "SP02": "none"}
    ev = tmp_path / "cand-ev.json"
    ev.write_text(json.dumps({"items": {i: {"status": s} for i, s in ev_items.items()}}))
    sels = []
    for k, (name, verdicts) in enumerate(sel_paths):
        p = tmp_path / f"sel{k}.json"
        p.write_text(json.dumps({"set": sorted(verdicts), "paths": {}, "v2": {"judge": "v2", "paths": {name: verdicts}}}))
        sels.append(p)
    return kp, ev, sels


def _run_select(tmp_path, kp, ev, sels):
    argv = ["select", "--answer-key", str(kp), "--evidence", str(ev), "--out-key", str(tmp_path / "c-key.json"),
            "--out-fixed", str(tmp_path / "c-fixed.json"), "--out-split", str(tmp_path / "c-split.json"),
            "--out", str(tmp_path / "o")]
    for s in sels:
        argv += ["--selection", str(s)]
    return A.main(argv, env={})


SEL_ALL = {"K09": "unknown", "S01": "unknown", "S02": "open", "S04": "open", "S05": "unknown", "S06": "open",
           "SC01": "open", "SC02": "unknown", "SC03": "unknown", "SP01": "unknown", "SP02": "unknown"}


def test_select_keeps_open_in_any_path_balances_strata_and_writes_private_files(tmp_path):
    kp, ev, sels = _select_inputs(tmp_path, [("c_recall_status", SEL_ALL),
                                             ("d_recall_list", dict(SEL_ALL, S01="open", S06="unknown"))])
    assert _run_select(tmp_path, kp, ev, sels) == 0
    key = json.loads((tmp_path / "c-key.json").read_text())
    # 키 있음: K09(원래 증상, 무조건) S01(d open) S02(c open) / S03 근거 없음. 키 없음: S04 S06(c open) / S05 not-open
    # 1:1 이라 키 있음은 후보 순서 앞에서 2개만(K09, S01), S02 는 할당 밖
    assert [i["id"] for i in key["closed"]] == ["K09", "S01", "S04", "S06"]
    assert [i["id"] for i in key["controls"]] == ["SC01"] and [i["id"] for i in key["partial"]] == ["SP01"]
    assert key["closed"][0] == _cand("K09", ["#3"], "proj-a")          # 항목 모양은 바이트 그대로(필드 추가 없음)
    assert "strata" not in key and key["list_query"] == LIST_QUERY
    why = {e["id"]: e["reason"] for e in key["excluded"]}
    # 대조군도 선정 run 어느 경로에서든 open 이어야 남는다(SC03 은 근거가 있어도 어느 경로에도 안 나왔다)
    assert why == {"S02": "stratum-quota", "S03": "no-evidence", "S05": "selection-not-open",
                   "SC02": "no-open-evidence", "SC03": "selection-not-open", "SP02": "no-evidence"}
    A.load_answer_key(str(tmp_path / "c-key.json"))                     # 세트 A·B 스키마로 읽힌다
    fixed = json.loads((tmp_path / "c-fixed.json").read_text())
    assert set(fixed["items"]) == {"K09", "S01", "S04", "S06", "SC01", "SP01"}
    assert json.loads((tmp_path / "c-split.json").read_text()) == {
        "C": {"K09": True, "S01": True, "S04": False, "S06": False, "SC01": True, "SP01": False}}
    for f in ("c-key.json", "c-fixed.json", "c-split.json"):
        assert ((tmp_path / f).stat().st_mode & 0o777) == 0o600
    rep = json.loads(next((tmp_path / "o").glob("select-*/report.json")).read_text())
    assert rep["closed"]["key:true"] == {"candidates": 4, "evidence": 3, "selected": 3, "kept": 2}
    assert rep["closed"]["key:false"] == {"candidates": 3, "evidence": 3, "selected": 2, "kept": 2}
    assert rep["controls"]["key:false"] == {"candidates": 2, "evidence": 1, "selected": 0, "kept": 0}
    assert "SECRET" not in json.dumps(rep)


def test_select_refuses_unscored_candidates_and_missing_strata(tmp_path):
    kp, ev, sels = _select_inputs(tmp_path, [("c_recall_status", {k: v for k, v in SEL_ALL.items() if k != "S05"})])
    with pytest.raises(SystemExit, match="S05"):                       # 근거 있는 후보가 선정 run 에 없다
        _run_select(tmp_path, kp, ev, sels)
    kp, ev, sels = _select_inputs(tmp_path, [("c_recall_status", {k: v for k, v in SEL_ALL.items() if k != "SC03"})])
    with pytest.raises(SystemExit, match="SC03"):                      # 근거 있는 대조군도 선정 run 판정이 있어야 한다
        _run_select(tmp_path, kp, ev, sels)
    kp, ev, sels = _select_inputs(tmp_path, [("c_recall_status", SEL_ALL)])
    d = json.loads(kp.read_text())
    del d["strata"]["S04"]
    kp.write_text(json.dumps(d))
    with pytest.raises(SystemExit, match="S04"):
        _run_select(tmp_path, kp, ev, sels)
    kp, ev, sels = _select_inputs(tmp_path, [("c_recall_status", SEL_ALL)])
    (tmp_path / "c-key.json").write_text("{}")                         # 이미 있는 출력은 덮지 않는다(고정 뒤 바뀌지 않게)
    assert _run_select(tmp_path, kp, ev, sels) == 1


# ---- final-review 반영(issue #341): (d)는 v2 전용, --controls remaining, 대조군 보충 멈춤

def test_score_paths_d_without_judge_v2_refuses_before_any_query(tmp_path, monkeypatch):
    kp, ev = _d_key(tmp_path)
    gw = ListGateway()
    monkeypatch.setattr(A, "build_clients", lambda target, args, env=None: (gw, CopyUpstream(writable=True)))
    assert A.main(["score", "--answer-key", str(kp), "--evidence", str(ev), "--out", str(tmp_path / "o"),
                   "--bank", "b", "--paths", "d"], env=NEXT_ENV) == 1
    assert gw.calls == [] and not list((tmp_path / "o").glob("score-*"))


def test_evidence_controls_remaining_counts_only_controls_with_open_evidence(tmp_path, monkeypatch):
    d = key_doc()
    d["controls"].append({"id": "C02", "item": "SECRET-ITEM-EPS", "keywords": ["eps-kw"], "project_hint": None,
                          "expect": "open"})
    kp = tmp_path / "k.json"
    kp.write_text(json.dumps(d))

    class Gw(FakeGateway):
        def call(self, tool, args):
            self.calls.append(tool)
            return {"results": [mem("[인계] gamma-kw 남음", tags=["kind:handoff"], mid="o"),
                                mem("[완료] proj-a #901 머지", tags=["kind:completion"], mid="k")]}

    monkeypatch.setattr(A, "build_clients", lambda target, args, env=None: (Gw(), FakeUpstream(True)))
    assert A.main(["evidence", "--controls", "--answer-key", str(kp), "--out", str(tmp_path / "o"), "--bank", "b"],
                  env=NEXT_ENV) == 0
    s = json.loads(next((tmp_path / "o").glob("evidence-*/summary.json")).read_text())
    assert s["items"]["C01"]["status"] == "present" and s["items"]["C02"]["status"] == "none"
    assert s["remaining"] == 1 + 1                      # 끝난 K01 1 + 열림 근거 있는 대조군 C01 1(C02 는 빠진다)


def test_select_stops_control_supplement_at_target(tmp_path):
    kp, ev, sels = _select_inputs(tmp_path, [("c_recall_status", SEL_ALL)])
    d = json.loads(kp.read_text())
    evd = json.loads(ev.read_text())
    sel = json.loads(sels[0].read_text())
    extra = [f"SX{k:02d}" for k in range(1, 12)]     # 보충 11개: 원래 증상 통과 1(SC01) + 보충 9 에서 목표 10 에 닿는다
    for iid in extra:
        d["controls"].append(_cand(iid, [f"#{300 + int(iid[2:])}"], "proj-a"))
        d["strata"][iid] = {"key": True, "source": "issue-open"}
        evd["items"][iid] = {"status": "present"}
        sel["v2"]["paths"]["c_recall_status"][iid] = "open"
    sel["set"] = sorted(sel["v2"]["paths"]["c_recall_status"])
    kp.write_text(json.dumps(d, ensure_ascii=False))
    ev.write_text(json.dumps(evd))
    sels[0].write_text(json.dumps(sel))
    assert _run_select(tmp_path, kp, ev, sels) == 0
    key = json.loads((tmp_path / "c-key.json").read_text())
    assert [i["id"] for i in key["controls"]] == ["SC01"] + extra[:9]
    why = {e["id"]: e["reason"] for e in key["excluded"]}
    assert why["SX10"] == why["SX11"] == "supplement-stop"


# ---------------------------------------------------------------- 완료 근거 주입(issue #342, 원인 층 분리 진단)

def _inject_inputs(tmp_path, done_in_results=False):
    """(c)(d) detail: K01 은 옛 인계(open)만 결과에 있다. 주입 행 = 그 뒤 완료 기록. done_in_results 면 그 행이 이미 결과에 있다."""
    kp = tmp_path / "key-i.json"
    kp.write_text(json.dumps(key_doc()))
    handoff = mem("[인계] alpha-kw 남음", "2026-10-10T00:00:00Z", ["kind:handoff"], "h")
    done = mem("[완료] alpha-kw 머지", "2026-10-12T00:00:00Z", ["kind:completion"], "d")
    ctl = mem("[인계] gamma-kw 남음 proj-a", "2026-10-11T00:00:00Z", ["kind:handoff"], "g")
    rows = [handoff] + ([done] if done_in_results else [])
    det = {"meta": {"target": "next", "undated_ok": True, "set": ["K01", "K02", "C01"]},
           "c_recall_status": {"K01": {"query": "q", "results": list(rows)}, "K02": {"query": "q", "results": []},
                               "C01": {"query": "q", "results": [ctl]}},
           "d_recall_list": {"query": "q", "results": list(rows) + [ctl]}}
    dp = tmp_path / "detail.json"
    dp.write_text(json.dumps(det))
    ip = tmp_path / "inject.json"
    ip.write_text(json.dumps({"K01": [done]}))
    return kp, dp, ip


@pytest.mark.parametrize("path", ["c_recall_status", "d_recall_list"])
@pytest.mark.parametrize("case", ["appended", "already-present", "no-option"])
def test_rescore_inject_evidence_matrix(tmp_path, monkeypatch, path, case):
    kp, dp, ip = _inject_inputs(tmp_path, done_in_results=(case == "already-present"))
    before = dp.read_bytes()
    monkeypatch.setattr(A, "build_clients", lambda *x, **k: pytest.fail("rescore must not open clients"))
    extra = ["--judge", "v2"] + ([] if case == "no-option" else ["--inject-evidence", str(ip)])
    got, run = _rescore(tmp_path, [kp], extra, dp)
    v = got["v2"]["paths"][path]
    assert dp.read_bytes() == before                                   # 원본 detail 은 그대로(입력 단계에서만 더한다)
    if case == "no-option":
        assert "inject" not in got and v["K01"] == "open"
        return
    blk = got["inject"]
    assert blk["sha256"] and blk["diagnostic"] is True
    if case == "appended":
        assert v["K01"] == "done"                                      # 근거를 넣으면 open -> done (검색층 원인)
        appended = blk["paths"][path]["K01"]["appended"] if path == "c_recall_status" else blk["paths"][path]["appended"]
        assert appended == ["d"]
    else:
        assert v["K01"] == "done"                                      # 이미 결과에 있으면 넣지 않는다(중복 없음)
        p = blk["paths"][path]
        assert (p["K01"]["already"] if path == "c_recall_status" else p["already"]) == ["d"]
    assert v["C01"] == "open"                                          # 주입하지 않은 항목은 그대로


def test_rescore_inject_evidence_refuses_unscored_item(tmp_path):
    kp, dp, ip = _inject_inputs(tmp_path)
    ip.write_text(json.dumps({"K99": [mem("x", mid="z")]}))
    with pytest.raises(SystemExit, match="inject-evidence"):
        A.main(["rescore", "--detail", str(dp), "--out", str(tmp_path / "o"), "--answer-key", str(kp),
                "--judge", "v2", "--inject-evidence", str(ip)], env={})


def test_inject_evidence_leaves_list_paths_alone(tmp_path):
    # (a)(b)는 합성 단계라 주입 짝을 만들 수 없다: 함수는 (c)(d) 절만 건드린다
    det = {"a_reflect_list": {"response": {"text": "## OPEN\n- alpha-kw\n"}}, "c_recall_status": {"K01": {"results": []}}}
    new, rep = A.inject_evidence(det, {"K01": [mem("[완료] alpha-kw 머지", mid="d")]})
    assert new["a_reflect_list"] == det["a_reflect_list"] and det["c_recall_status"]["K01"]["results"] == []
    assert [r["id"] for r in new["c_recall_status"]["K01"]["results"]] == ["d"]
    assert set(rep) == {"c_recall_status"}


def test_inject_evidence_counts_shared_rows_once_on_list_recall():
    shared = mem("[완료] alpha-kw beta-kw 머지", mid="s")
    det = {"d_recall_list": {"results": [mem("x", mid="h")]}}
    new, rep = A.inject_evidence(det, {"K01": [shared], "K02": [shared, mem("[완료] beta-kw", mid="b")]})
    assert [r["id"] for r in new["d_recall_list"]["results"]] == ["h", "s", "b"]
    assert rep["d_recall_list"] == {"appended": ["s", "b"], "already": [], "skipped_no_results": False}


def test_inject_evidence_skips_items_without_results_so_invalid_stays(tmp_path):
    # results None(원래 invalid/no-provenance)인 (c) 항목과 (d) 절에는 넣지 않는다 - invalid 가 유효 판정으로 바뀌지 않게
    kp, dp, ip = _inject_inputs(tmp_path)
    det = json.loads(dp.read_text())
    det["c_recall_status"]["K01"]["results"] = None
    det["d_recall_list"]["results"] = None
    dp.write_text(json.dumps(det))
    got, _ = _rescore(tmp_path, [kp], ["--judge", "v2", "--inject-evidence", str(ip)], dp)
    assert got["v2"]["paths"]["c_recall_status"]["K01"] == "invalid"
    assert got["v2"]["paths"]["d_recall_list"]["K01"] == "invalid"
    rep = got["inject"]["paths"]
    assert rep["c_recall_status"]["skipped_no_results"] == ["K01"]
    assert rep["d_recall_list"]["skipped_no_results"] is True


def test_inject_evidence_reports_ids_missing_from_recall_section():
    det = {"c_recall_status": {"K01": {"results": []}}}
    _, rep = A.inject_evidence(det, {"K02": [mem("[완료] beta-kw", mid="b")]})
    assert rep["c_recall_status"]["missing"] == ["K02"]


def test_rescore_inject_evidence_refuses_rows_without_id(tmp_path):
    kp, dp, ip = _inject_inputs(tmp_path)
    row = mem("[완료] alpha-kw 머지", mid="d")
    row.pop("id")
    ip.write_text(json.dumps({"K01": [row]}))
    with pytest.raises(SystemExit, match="K01"):
        A.main(["rescore", "--detail", str(dp), "--out", str(tmp_path / "o"), "--answer-key", str(kp),
                "--judge", "v2", "--inject-evidence", str(ip)], env={})
