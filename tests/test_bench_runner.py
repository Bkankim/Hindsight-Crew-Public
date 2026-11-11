"""issue #13 재생 하네스 측정기 - 운영 보호·워밍업 격리·간섭 신호·결과 정규화 (2026-09-23 재리뷰)."""
import gzip
import json

import pytest

from bench import runner as R


def _msg(payload, structured=None):
    res = {"content": [{"type": "text", "text": json.dumps(payload)}]}
    if structured is not None:
        res["structuredContent"] = structured
    return {"msg": {"result": res}, "latency_ms": 1.0}


def test_classify_keeps_whole_payload_and_structured_state():
    p = {"results": [{"id": "a"}], "chunks": {"c": 1}}
    out = R.classify(_msg(p, {"result": json.dumps(p)}))
    assert out["outcome"] == "ok"
    assert out["stored"]["payload_rest"] == {"chunks": {"c": 1}}
    assert out["stored"]["structured"] == "consistent"
    assert R.classify(_msg(p, {"result": json.dumps({"results": []})}))["stored"]["structured"] == "inconsistent"
    assert R.classify(_msg(p))["stored"]["structured"] == "absent"


def test_upstream_error_payload_is_not_ok():
    assert R.classify(_msg({"error": "x", "results": []}))["outcome"] == "upstream-error"


def test_prod_port_is_refused():
    with pytest.raises(SystemExit):
        R.check_arm_ports({"base": 8888})


def test_rerank_kind():
    assert R.rerank_kind({"tool": "recall"}) == "recall"
    assert R.rerank_kind({"tool": "/v1/default/banks/team-eng/memories/recall"}) == "recall"
    assert R.rerank_kind({"tool": "/v1/default/banks/x/mental-models/m/refresh"}) == "mental-models"
    assert R.rerank_kind({"tool": "retain"}) == "retain"                       # 게이트 retain 은 리랭크 recall 을 부른다
    assert R.rerank_kind({"tool": "/v1/default/banks/x/memories", "method": "POST"}) == "retain"
    assert R.rerank_kind({"tool": "/v1/default/banks/x/memories/list", "method": "GET"}) is None
    assert R.rerank_kind({"tool": "tools/list"}) is None
    # issue #37: retain-tags 줄은 같은 retain의 부가 기록이라 세지 않는다.
    assert R.rerank_kind({"tool": "retain", "decision": "retain-tags"}) is None
    assert R.rerank_kind({"tool": "/v1/default/banks/x/memories", "method": "POST",
                          "decision": "retain-tags"}) is None
    assert R.rerank_kind({"tool": "retain", "decision": "allow"}) == "retain"
    # issue #48: 요청당 1줄(allow)만 센다. 같은 retain의 교정 기록은 세지 않는다.
    assert R.rerank_kind({"tool": "retain", "decision": "enforce-normalize"}) is None


# issue #48: 감사 줄 한 종류(decision) x 요청 표면(tool). 기대값은 gateway 의 audit.record 호출 지점에서 나온다:
#   allow           - MCP 요소마다 1줄(app.py proxy, decide 직후), REST 요청마다 1줄(rest_proxy, rules 이전)
#   context-trigger - POST /context-triggers 1줄뿐. allow 줄이 따로 없다(context_trigger 핸들러)
#   reflect-demand  - 허용된 reflect 마다 붙는 텔레메트리(reflect_demand.record) -> allow 와 한 쌍
#   그 외           - 같은 요청의 부가 기록(autofill·normalize·retain-tags·contradiction-warn·recurrence·
#                     injection-correction·rewrite-failure)이거나 거부(deny·enforce-reject)
#   decision 키 없음 - 옛 행·합성 행: tool 기준으로 센다(기존 동작)
_SURFACES = {
    "mcp-retain": ({"tool": "retain"}, "retain"),
    "mcp-sync_retain": ({"tool": "sync_retain"}, "retain"),
    "rest-retain": ({"tool": "/v1/default/banks/x/memories", "method": "POST"}, "retain"),
    "mcp-recall": ({"tool": "recall"}, "recall"),
    "rest-recall": ({"tool": "/v1/default/banks/x/memories/recall", "method": "POST"}, "recall"),
    "reflect": ({"tool": "reflect"}, "reflect"),
    "context-triggers": ({"tool": "context-triggers", "method": "POST"}, "context-triggers"),
    "mental-models": ({"tool": "/v1/default/banks/x/mental-models/m/refresh", "method": "POST"}, "mental-models"),
}
_DECISIONS = ["allow", "enforce-autofill", "enforce-normalize", "contradiction-warn", "enforce-reject", "deny",
              "retain-tags", "context-trigger", "reflect-demand", "recurrence", "injection-correction",
              "rewrite-failure", None]
_COUNTED = {"allow", None}                       # 요청당 1줄 기준 줄(None = decision 키 없음)
_COUNTED_EXTRA = {"context-triggers": {"context-trigger"}}


@pytest.mark.parametrize("decision", _DECISIONS, ids=lambda d: "no-decision" if d is None else d)
@pytest.mark.parametrize("surface", list(_SURFACES))
def test_rerank_kind_counts_one_line_per_request(surface, decision):
    row, kind = _SURFACES[surface]
    row = dict(row)
    if decision is not None:
        row["decision"] = decision
    counted = _COUNTED | _COUNTED_EXTRA.get(surface, set())
    assert R.rerank_kind(row) == (kind if decision in counted else None)


# issue #62: context-trigger 줄은 reason 으로 갈린다. miss 는 upstream recall 을 부르지 않는다(gateway/app.py
# context_trigger 핸들러: 트리거가 매칭될 때만 upstream_recall). match·match-empty·recall-failed 는 recall 을 불렀다.
@pytest.mark.parametrize("decision", ["context-trigger", "deny"])
@pytest.mark.parametrize("reason", ["miss", "match:t1", "match-empty:t1", "recall-failed:t1"])
def test_context_trigger_miss_is_not_counted(reason, decision):
    row = {"tool": "context-triggers", "method": "POST", "decision": decision, "reason": reason}
    counted = decision == "context-trigger" and reason != "miss"
    assert R.rerank_kind(row) == ("context-triggers" if counted else None)


def test_context_trigger_without_reason_is_still_counted():
    # reason 키가 없는 줄은 miss 임을 알 수 없으니 보수적으로 센다.
    assert R.rerank_kind({"tool": "context-triggers", "decision": "context-trigger"}) == "context-triggers"


# issue #62: 멘탈 모델은 리랭크(reflect)를 부르는 조작만 센다. REST 는 메서드 + 경로 끝 세그먼트로 판정한다.
#   센다 : REST POST 컬렉션(생성)·POST <id>/refresh, MCP create_mental_model·refresh_mental_model
#   안 셈: 조회(GET)·clear·delete 와 MCP get/list/delete/clear
_MM = "/v1/default/banks/x/mental-models"
_MM_SURFACES = {
    "rest-get-collection": ({"tool": _MM, "method": "GET"}, None),
    "rest-get-id": ({"tool": _MM + "/m1", "method": "GET"}, None),
    "rest-post-collection": ({"tool": _MM, "method": "POST"}, "mental-models"),
    "rest-post-refresh": ({"tool": _MM + "/m1/refresh", "method": "POST"}, "mental-models"),
    "rest-get-refresh": ({"tool": _MM + "/m1/refresh", "method": "GET"}, None),
    "rest-post-clear": ({"tool": _MM + "/m1/clear", "method": "POST"}, None),
    "rest-delete-id": ({"tool": _MM + "/m1", "method": "DELETE"}, None),
    "mcp-create_mental_model": ({"tool": "create_mental_model"}, "mental-models"),
    "mcp-refresh_mental_model": ({"tool": "refresh_mental_model"}, "mental-models"),
    "mcp-get_mental_model": ({"tool": "get_mental_model"}, None),
    "mcp-list_mental_models": ({"tool": "list_mental_models"}, None),
    "mcp-delete_mental_model": ({"tool": "delete_mental_model"}, None),
    "mcp-clear_mental_model": ({"tool": "clear_mental_model"}, None),
    # update: upstream update_mental_model 은 source_query 를 SQL UPDATE 로 바꿀 뿐 reflect 를 돌리지 않는다
    # (MCP 도구 docstring: 재생성은 refresh_mental_model 로). 그래서 세지 않는다.
    "mcp-update_mental_model": ({"tool": "update_mental_model"}, None),
    "rest-patch-id": ({"tool": _MM + "/m1", "method": "PATCH"}, None),
    "rest-put-id": ({"tool": _MM + "/m1", "method": "PUT"}, None),
}


@pytest.mark.parametrize("decision", ["allow", "deny"])
@pytest.mark.parametrize("surface", list(_MM_SURFACES))
def test_mental_models_counts_only_rerank_operations(surface, decision):
    row, kind = _MM_SURFACES[surface]
    assert R.rerank_kind({**row, "decision": decision}) == (kind if decision == "allow" else None)


@pytest.mark.parametrize("tool,method", [
    ("/v1/default/banks/personal-demo/mental-models/recall-notes", "GET"),
    ("/v1/default/banks/personal-demo/mental-models/reflect-notes", "GET"),
    ("/v1/default/banks/personal-demo/mental-models/recall-x/clear", "POST"),
    ("/v1/default/banks/recall-lab/mental-models/m1", "GET"),
    ("/v1/default/banks/reflect-lab/mental-models", "GET"),
])
def test_non_rerank_mental_model_path_does_not_leak_to_recall_or_reflect(tool, method):
    assert R.rerank_kind({"tool": tool, "method": method, "decision": "allow"}) is None


@pytest.mark.parametrize("tool", [_MM + "/m1/refresh-x", _MM + "-archive", _MM + "/m1/refresh/extra"])
def test_mental_models_path_is_matched_by_last_segment_not_substring(tool):
    assert R.rerank_kind({"tool": tool, "method": "POST", "decision": "allow"}) is None


# issue #106: 뱅크 이름이 mental-models 여도 분류는 banks/<bank>/ 다음 리소스 자리로만 정한다.
# 리소스 자리의 mental-models 는 (리랭크 없는 경로가 recall·reflect 로 새지 않게) 그대로 지킨다.
_BANK_NAMES = ["x", "personal-demo", "mental-models", "recall-lab", "reflect-lab", "reflections"]   # recall·reflect 로 시작하는 뱅크도 분류를 바꾸지 않는다
_BANK_SURFACES = {
    "rest-recall": ("/memories/recall", "POST", "recall"),
    "rest-reflect": ("/reflect", "POST", "reflect"),
    "rest-retain": ("/memories", "POST", "retain"),
    "mm-post-collection": ("/mental-models", "POST", "mental-models"),
    "mm-post-refresh": ("/mental-models/m1/refresh", "POST", "mental-models"),
    "mm-get-collection": ("/mental-models", "GET", None),
    "mm-get-id": ("/mental-models/m1", "GET", None),
    "mm-get-recall-named-id": ("/mental-models/recall-notes", "GET", None),
    "mm-get-reflect-named-id": ("/mental-models/reflect-notes", "GET", None),
    "mm-clear": ("/mental-models/m1/clear", "POST", None),
    "mm-refresh-extra": ("/mental-models/m1/refresh/extra", "POST", None),
    "documents-get": ("/documents", "GET", None),
    "bank-level-post": ("", "POST", None),                       # POST banks/<bank> 자체는 리랭크가 없다
}


@pytest.mark.parametrize("surface", list(_BANK_SURFACES))
@pytest.mark.parametrize("bank", _BANK_NAMES)
def test_rerank_kind_is_decided_by_the_resource_after_the_bank_name(bank, surface):
    suffix, method, kind = _BANK_SURFACES[surface]
    row = {"tool": f"/v1/default/banks/{bank}{suffix}", "method": method, "decision": "allow"}
    assert R.rerank_kind(row) == kind


def test_prod_events_counts_each_request_once(tmp_path, monkeypatch):
    """감사 로그에 실제로 남는 줄 묶음 -> 요청 1건 = 이벤트 1건."""
    def line(tool, decision, ts, **kw):
        return {"ts": ts, "tool": tool, "decision": decision, **kw}
    rows = [
        # MCP retain 1건: allow + autofill + normalize + retain-tags + 게이트 기록
        line("retain", "allow", "t1"), line("retain", "enforce-autofill", "t1"),
        line("retain", "enforce-normalize", "t1"), line("retain", "retain-tags", "t1"),
        line("retain", "contradiction-warn", "t1"), line("retain", "recurrence", "t1"),
        # MCP reflect 1건: allow + reflect-demand
        line("reflect", "allow", "t2"), line("reflect", "reflect-demand", "t2"),
        # 거부된 recall(MCP·REST)은 사이드카에 닿지 않는다
        line("recall", "deny", "t3"), line("/v1/default/banks/x/memories/recall", "deny", "t3", method="POST"),
        # REST retain 거부: allow 1줄(rules 이전 기록)은 남고 enforce-reject 는 세지 않는다
        line("/v1/default/banks/x/memories", "allow", "t4", method="POST"),
        line("/v1/default/banks/x/memories", "enforce-reject", "t4", method="POST"),
        # context-triggers 1건: allow 없이 context-trigger 1줄
        line("context-triggers", "context-trigger", "t5", method="POST"),
    ]
    log = tmp_path / "gateway.log"
    log.write_text("")
    monkeypatch.setattr(R, "PROD_AUDIT", str(log))
    off = R.prod_audit_offset()
    with open(log, "a") as fh:
        fh.writelines(json.dumps(r) + "\n" for r in rows)
    assert R.prod_events_since(off) == [
        {"ts": "t1", "kind": "retain"}, {"ts": "t2", "kind": "reflect"},
        {"ts": "t4", "kind": "retain"}, {"ts": "t5", "kind": "context-triggers"}]


def test_prod_events_since_offset_and_rotation(tmp_path, monkeypatch):
    log = tmp_path / "gateway.log"
    log.write_text(json.dumps({"tool": "recall", "ts": "2026-01-01T00:00:00Z", "reason": "secret?"}) + "\n")
    monkeypatch.setattr(R, "PROD_AUDIT", str(log))
    off = R.prod_audit_offset()
    with open(log, "a") as fh:
        fh.write(json.dumps({"tool": "initialize", "ts": "2026-01-01T00:00:01Z"}) + "\n")
        fh.write(json.dumps({"tool": "reflect", "ts": "2026-01-01T00:00:02Z"}) + "\n")
    assert R.prod_events_since(off) == [{"ts": "2026-01-01T00:00:02Z", "kind": "reflect"}]   # 시각·종류만
    log.write_text(json.dumps({"tool": "recall", "ts": "2026-01-01T00:00:03Z"}) + "\n")      # 회전(작아짐)
    assert R.prod_events_since({**off, "offset": off["offset"] + 10_000}) == [{"ts": "2026-01-01T00:00:03Z", "kind": "recall"}]
    assert R.prod_events_since(None) is None


def _audit_row(i):
    return json.dumps({"tool": "reflect", "ts": f"2026-01-01T00:00:{i:02d}Z"}) + "\n"


def _rotate_copytruncate(log, nul_hole=0):
    """rotate-logs.sh 모양: 복사 + gzip + `: >`. nul_hole>0 이면 colima virtiofs 의 truncate 뒤 NUL 구멍을 흉내낸다(#166)."""
    import shutil
    with open(log, "rb") as src, gzip.open(f"{log}.1.gz", "wb") as dst:
        shutil.copyfileobj(src, dst)
    with open(log, "wb") as fh:
        fh.write(b"\x00" * nul_hole)


# (회전 모양, 회전 뒤 새 파일에 쓰는 줄 수, NUL 구멍 바이트, offset 시점에 이미 .1.gz 가 있었나)
# A: 회전 뒤 새 파일 < offset. B: 새 파일 >= offset. NUL: 새 파일 첫 줄 앞 구멍.
@pytest.mark.parametrize("rotate,after_lines,nul_hole,prior_gz", [
    pytest.param(False, 0, 0, False, id="no-rotation"),
    pytest.param(True, 1, 0, False, id="A-new-file-smaller"),
    pytest.param(True, 12, 0, False, id="B-new-file-not-smaller"),
    pytest.param(True, 1, 400, False, id="A-first-line-nul"),
    pytest.param(True, 12, 400, False, id="B-first-line-nul"),
    pytest.param(True, 12, 0, True, id="B-prior-gz-replaced"),
])
def test_prod_events_survive_rotation_between_offset_and_read(tmp_path, monkeypatch, rotate, after_lines, nul_hole,
                                                              prior_gz):
    log = tmp_path / "gateway.log"
    monkeypatch.setattr(R, "PROD_AUDIT", str(log))
    log.write_text("".join(_audit_row(i) for i in range(10)))                # run 시작 전 줄(세면 안 된다)
    if prior_gz:
        _rotate_copytruncate(log)
        log.write_text("".join(_audit_row(i) for i in range(10)))
    off = R.prod_audit_offset()
    with open(log, "a") as fh:
        fh.writelines(_audit_row(i) for i in range(20, 23))                  # run 중, 회전 전
    if rotate:
        _rotate_copytruncate(log, nul_hole)
    with open(log, "a") as fh:
        fh.writelines(_audit_row(i) for i in range(30, 30 + after_lines))    # run 중, 회전 뒤
    expected = [f"2026-01-01T00:00:{i:02d}Z" for i in [20, 21, 22] + list(range(30, 30 + after_lines))]
    assert [e["ts"] for e in R.prod_events_since(off)] == expected


def _gz_prefix_mark(log):
    log.write_text("".join(_audit_row(i) for i in range(10)))
    return {"offset": log.stat().st_size, "rotated": None}


def _live_rows_ts(rows):
    return [f"2026-01-01T00:00:{i:02d}Z" for i in rows]


@pytest.mark.parametrize("make_gz", [
    pytest.param(lambda body: b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff" + b"\xff" * 64, id="corrupt-deflate"),
    pytest.param(lambda body: gzip.compress(body)[:-12], id="partial-gzip-still-writing"),
])
def test_unreadable_rotated_archive_falls_back_to_live_file(tmp_path, monkeypatch, make_gz):
    log = tmp_path / "gateway.log"
    monkeypatch.setattr(R, "PROD_AUDIT", str(log))
    mark = _gz_prefix_mark(log)
    old_body = "".join(_audit_row(i) for i in range(10)).encode() + b"".join(_audit_row(i).encode() for i in range(20, 23))
    (tmp_path / "gateway.log.1.gz").write_bytes(make_gz(old_body))
    log.write_text("".join(_audit_row(i) for i in (30, 31)))
    assert [e["ts"] for e in R.prod_events_since(mark)] == _live_rows_ts([30, 31])


def test_rotated_archive_ending_mid_line_does_not_fuse_with_first_new_row(tmp_path, monkeypatch):
    log = tmp_path / "gateway.log"
    monkeypatch.setattr(R, "PROD_AUDIT", str(log))
    mark = _gz_prefix_mark(log)
    old_body = ("".join(_audit_row(i) for i in range(10)) + _audit_row(20)
                + _audit_row(21)[:-8]).encode()                       # 마지막 줄이 개행·닫는 괄호 없이 끝난다
    (tmp_path / "gateway.log.1.gz").write_bytes(gzip.compress(old_body))
    log.write_text("\x00" * 50 + _audit_row(30))
    assert [e["ts"] for e in R.prod_events_since(mark)] == _live_rows_ts([20, 30])


def test_prod_audit_offset_is_json_serializable(tmp_path, monkeypatch):
    log = tmp_path / "gateway.log"
    log.write_text(_audit_row(1))
    monkeypatch.setattr(R, "PROD_AUDIT", str(log))
    off = R.prod_audit_offset()
    assert json.loads(json.dumps(off)) == off
    monkeypatch.setattr(R, "PROD_AUDIT", str(tmp_path / "absent.log"))
    assert R.prod_audit_offset() is None and R.prod_events_since(None) is None


def test_warmup_pool_never_overlaps_measured(tmp_path, monkeypatch):
    dev = tmp_path / "dev.jsonl"
    rows = [{"key": f"k{i}", "split_key": f"s{i}", "args": {"query": f"q{i}"}} for i in range(6)]
    dev.write_text("".join(json.dumps(r) + "\n" for r in rows))
    monkeypatch.setattr(R, "DEV", str(dev))
    corpus = rows[:4]
    pool = R.warmup_pool(corpus, 1)
    assert sorted(r["args"]["query"] for r in pool) == ["q4", "q5"]
    assert all(r["key"].startswith("warmup-") for r in pool)
    synth = R.warmup_pool(rows, 1)                                   # dev 전체 측정이면 합성 쿼리
    assert synth and all(r["key"].startswith("warmup-synth-") for r in synth)


def test_non_ok_responses_store_client_text():
    out = R.classify(_msg({"error": "x", "results": []}))
    assert out["stored"] == {"text": json.dumps({"error": "x", "results": []})}
    msg = {"msg": {"result": {"isError": True, "content": [{"type": "text", "text": "RULE:foo bad"}]}}, "latency_ms": 1}
    assert R.classify(msg)["stored"] == {"text": "RULE:foo bad"}


def test_read_token_uses_bench_client_key_not_production(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "STATE_DIR", str(tmp_path))
    monkeypatch.setenv("HINDSIGHT_DEMO_TOKEN", "prod-token-should-not-be-used")
    with pytest.raises(SystemExit):
        R.read_token()
    (tmp_path / "bench-client.key").write_text("hcb_x\n")
    assert R.read_token() == "hcb_x"


# ---------------------------------------------------------------- 세트 팔 (issue #13 B)

def test_arm_watches_production_and_its_own_sidecar_queue():
    assert R.arm_queue_urls({"sidecar_port": None}) == [R.TEI_INFO]
    assert R.arm_queue_urls({"sidecar_port": 18788}) == [R.TEI_INFO, "http://127.0.0.1:18788/info"]
    assert R.combined_queue_depth([0, 0]) == 0
    assert R.combined_queue_depth([0, 2]) == 2
    assert R.combined_queue_depth([0, None]) is None          # 못 읽으면 오염으로 본다(판정기가 쌍을 뺀다)


def test_all_sets_must_share_one_bench_clock():
    same = {"": {"bench_clock": "2026-09-22T20:00:00Z"}, "cand": {"bench_clock": "2026-09-22T20:00:00Z"}}
    assert R.common_bench_clock(same) == "2026-09-22T20:00:00Z"
    with pytest.raises(SystemExit):
        R.common_bench_clock({**same, "ctrl": {"bench_clock": "2026-09-22T21:00:00Z"}})   # 다른 백업·시점
    with pytest.raises(SystemExit):
        R.common_bench_clock({**same, "ctrl": None})                                         # 세트 state 없음


def test_provenance_records_each_arm_set_and_its_replica(tmp_path, monkeypatch):
    (tmp_path / "arms").mkdir()
    for arm, prov in {"base": {"set": "base", "sidecar_port": 18781},
                      "cand": {"set": "cand", "sidecar_port": 18783}}.items():
        (tmp_path / "arms" / f"{arm}.json").write_text(json.dumps({"arm": arm, **prov}))
    for s, sc in (("base", {"RERANK_BATCH_SIZE": "32"}), ("cand", {"RERANK_BATCH_SIZE": "16"})):
        (tmp_path / f"replica-{s}.json").write_text(json.dumps({"bench_clock": "C", "sidecar": sc}))
    monkeypatch.setattr(R, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(R, "arm_container", lambda port: {18891: "hc-bench-gw-base", 18893: "hc-bench-gw-cand"}[port])
    prov = R.provenance({"base": 18891, "cand": 18893})
    assert prov["arms"]["cand"]["set"] == "cand" and prov["arms"]["cand"]["sidecar_port"] == 18783
    assert prov["replicas"]["cand"]["sidecar"] == {"RERANK_BATCH_SIZE": "16"}
    assert set(prov["replicas"]) == {"base", "cand"}


def test_queue_urls_follow_the_sets_current_sidecar_not_the_gw_up_snapshot():
    """팔 json 의 사이드카 포트는 gw-up 시점 값이다 - 세트를 다른 사이드카로 다시 띄웠으면 세트 state 가 맞다."""
    urls = R.arm_queue_urls({"sidecar_port": 18782})
    assert urls == [R.TEI_INFO, "http://127.0.0.1:18782/info"]
    assert R.arm_queue_urls(None) == [R.TEI_INFO]


# ---------------------------------------------------------------- 벤치 간섭 가드 (issue #19)

class _FakeGuard:
    """n 번째 check 에서 중단을 낸다(None 이면 끝까지 정상)."""
    def __init__(self, abort_at=None):
        self.checks, self.abort_at = 0, abort_at
        self.aborted = None

    def check(self):
        self.checks += 1
        if self.abort_at is not None and self.checks >= self.abort_at:
            from bench.guard import GuardAbort
            self.aborted = "swap-growth"
            raise GuardAbort(self.aborted)

    def meta(self):
        return {"windows": [[1.0, 2.0]], "pauses": [], "aborted": self.aborted}


def _fake_run_env(tmp_path, monkeypatch, recalls):
    class Client:
        def __init__(self, port, token):
            self.port = port

        def open(self):
            pass

        def recall(self, args):
            recalls.append((self.port, args["query"]))
            return {"latency_ms": 1.0, "outcome": "transport-error", "detail": "fake"}

    class Tok:
        def count(self, text):
            return len(text)

    corpus = [{"key": f"k{i}", "args": {"query": f"q{i}"}} for i in range(3)]
    monkeypatch.setattr(R, "RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(R, "McpClient", Client)
    monkeypatch.setattr(R, "Tokenizer", Tok)
    monkeypatch.setattr(R, "load_corpus", lambda split, limit, seed: corpus)
    monkeypatch.setattr(R, "provenance", lambda arms: {"replicas": {"": {}}, "arms": {}})
    monkeypatch.setattr(R, "common_bench_clock", lambda reps: "2026-09-26T00:00:00Z")
    monkeypatch.setattr(R, "tei_queue_depth", lambda url: 0)
    monkeypatch.setattr(R, "rewrite_failures", lambda port: 0)
    monkeypatch.setattr(R, "prod_audit_offset", lambda: None)
    monkeypatch.setattr(R, "sidecar_log_offset", lambda: 0)
    monkeypatch.setattr(R, "sidecar_errors_since", lambda off: {"http_503": 2, "deadline": 1, "queue_full": 0})
    return dict(split="dev", rounds=1, warmup=0, limit=None, seed=1, token="t", fingerprint=lambda sets: {},
                check_ports=lambda arms: None, gap_ms=0, selfcheck=lambda: None)


def test_runner_checks_the_guard_before_start_and_before_every_call(tmp_path, monkeypatch):
    recalls, g = [], _FakeGuard()
    kw = _fake_run_env(tmp_path, monkeypatch, recalls)
    out = R.run({"a": 1, "b": 2}, run_id="r1", guard=g, **kw)
    meta = json.load(open(f"{out}/meta.json"))
    assert len(recalls) == 6 and g.checks == 1 + 6          # 시작 전 1 + 호출마다 1
    assert meta["guard"]["windows"] == [[1.0, 2.0]] and meta["guard"]["aborted"] is None
    assert meta["prod_sidecar"] == {"http_503": 2, "deadline": 1, "queue_full": 0}


def test_runner_stops_measuring_when_the_guard_aborts(tmp_path, monkeypatch):
    recalls, g = [], _FakeGuard(abort_at=3)                  # 시작 전 ok, 첫 호출 ok, 두 번째 호출 전 중단
    kw = _fake_run_env(tmp_path, monkeypatch, recalls)
    with pytest.raises(SystemExit):
        R.run({"a": 1, "b": 2}, run_id="r2", guard=g, **kw)
    assert len(recalls) == 1
    meta = json.load(open(tmp_path / "runs" / "r2" / "meta.json"))
    assert meta["guard"]["aborted"] == "swap-growth" and meta["prod_sidecar"]["http_503"] == 2


def test_runner_refuses_to_start_when_the_guard_aborts_before_start(tmp_path, monkeypatch):
    recalls, g = [], _FakeGuard(abort_at=1)
    kw = _fake_run_env(tmp_path, monkeypatch, recalls)
    with pytest.raises(SystemExit):
        R.run({"a": 1, "b": 2}, run_id="r3", guard=g, **kw)
    assert recalls == []


# ---------------------------------------------------------------- 운영 사이드카 keep-warm (issue #79)
# 실제 송신은 어디에도 없다: KeepWarm 은 송신기를 주입받고, 아래 autouse 픽스처가 소켓 연결을 막는다.

@pytest.fixture(autouse=True)
def _no_real_network_send(monkeypatch):
    import socket
    attempts = []

    def blocked(self, address, *a, **k):
        attempts.append(address)
        raise ConnectionRefusedError("blocked in tests: no real network send")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    return attempts


class _KwWorld:
    """가짜 시계 + 송신기. 호출(recall)마다 시계가 5초 가고, 송신·호출 순서를 events 에 남긴다."""
    def __init__(self, send_script=()):
        self.t, self.events, self.script = 1_790_000_000.0, [], list(send_script)
        self.crash = RuntimeError("client broke")

    def now(self):
        return self.t

    def send(self):
        self.events.append("kw")
        out = self.script.pop(0) if self.script else 200
        if isinstance(out, BaseException):
            raise out
        return out

    def client_cls(self, fail_at=None):
        world = self

        class Client:
            def __init__(self, port, token):
                pass

            def open(self):
                pass

            def recall(self, args):
                world.events.append("recall")
                if fail_at is not None and world.events.count("recall") >= fail_at:
                    raise world.crash
                world.t += 5.0
                return {"latency_ms": 1.0, "outcome": "transport-error", "detail": "fake"}
        return Client


def _kw_run(tmp_path, monkeypatch, world, guard=None, run_id="kw1", send_script=(), fail_at=None, interval_s=10.0,
            use_keepwarm=True):
    from bench.keepwarm import KeepWarm
    kw = KeepWarm(world.send, now=world.now, interval_s=interval_s) if use_keepwarm else None
    env = _fake_run_env(tmp_path, monkeypatch, [])
    monkeypatch.setattr(R, "McpClient", world.client_cls(fail_at))
    world.kw = kw
    out = R.run({"a": 1, "b": 2}, run_id=run_id, guard=guard or _FakeGuard(), keepwarm=kw, **env)
    return out, kw


def test_keepwarm_runs_between_calls_only_while_the_run_runs(tmp_path, monkeypatch):
    w = _KwWorld()
    out, kw = _kw_run(tmp_path, monkeypatch, w)
    # 호출 6개(3쿼리 x 2팔), 호출마다 시계 +5s, 호출 직전 tick, 간격 10s: 손계산 - t=0,10,20 에서 송신
    assert w.events == ["kw", "recall", "recall", "kw", "recall", "recall", "kw", "recall", "recall"]
    n = len(w.events)
    kw.tick()                                                  # run 이 끝난 뒤에는 간격이 지나도 보내지 않는다
    w.t += 1000
    kw.tick()
    assert len(w.events) == n
    meta = json.load(open(f"{out}/meta.json"))["keepwarm"]
    assert meta["sent"] == 3 and meta["failed"] == 0
    assert meta["started"] == 1_790_000_000.0 and meta["stopped"] == 1_790_000_030.0


def test_keepwarm_window_is_a_canary_exclude_line_in_run_meta(tmp_path, monkeypatch):
    from bench import canary
    w = _KwWorld()
    out, _ = _kw_run(tmp_path, monkeypatch, w, run_id="kw-line")
    line = json.load(open(f"{out}/meta.json"))["keepwarm"]["exclude_line"]
    (s, e, why), = canary.parse_exclude(line)
    assert (s, e) == (1_790_000_000.0, 1_790_000_330.0) and "kw-line" in why     # 끝 = run 끝 +30s + 꼬리 300s


def test_keepwarm_failure_is_recorded_and_the_run_continues(tmp_path, monkeypatch):
    w = _KwWorld(send_script=[503, ConnectionRefusedError("x")])
    out, _ = _kw_run(tmp_path, monkeypatch, w, run_id="kw-fail")
    assert w.events.count("recall") == 6                       # 송신이 실패해도 호출은 전부 돈다
    meta = json.load(open(f"{out}/meta.json"))["keepwarm"]
    assert meta["sent"] == 3 and meta["failed"] == 2
    assert [f["error"] for f in meta["failures"]] == ["http-503", "ConnectionRefusedError"]
    # meta.prod_sidecar 의 503 에는 keep-warm 자신의 실패도 들어 있다 - 옆의 prod_sidecar_keepwarm 으로 뺄 수 있다(연결 실패는 응답이 없어 안 센다)
    saved = json.load(open(f"{out}/meta.json"))
    assert saved["prod_sidecar"]["http_503"] == 2 and saved["prod_sidecar_keepwarm"] == {"http_503": 1, "deadline": 0, "queue_full": 0}


def test_keepwarm_stops_when_the_guard_aborts_mid_run(tmp_path, monkeypatch):
    w = _KwWorld()
    with pytest.raises(SystemExit):
        _kw_run(tmp_path, monkeypatch, w, guard=_FakeGuard(abort_at=3), run_id="kw-abort")
    assert w.events == ["kw", "recall"]                        # 시작 전 ok, 첫 호출 ok, 두 번째 호출 전 중단
    meta = json.load(open(tmp_path / "runs" / "kw-abort" / "meta.json"))["keepwarm"]
    assert meta["stopped"] is not None and meta["exclude_line"]
    n = len(w.events)
    w.t += 1000
    w.kw.tick()
    assert len(w.events) == n                                  # 중단 뒤에는 간격이 지나도 보내지 않는다


@pytest.mark.parametrize("crash", [RuntimeError("client broke"), KeyboardInterrupt()], ids=["exception", "ctrl-c"])
def test_keepwarm_stops_even_when_the_run_dies_with_an_exception(tmp_path, monkeypatch, crash):
    from bench.keepwarm import KeepWarm
    w = _KwWorld()
    w.crash = crash
    kw = KeepWarm(w.send, now=w.now, interval_s=10.0)
    env = _fake_run_env(tmp_path, monkeypatch, [])
    monkeypatch.setattr(R, "McpClient", w.client_cls(fail_at=2))
    with pytest.raises(type(crash)):
        R.run({"a": 1, "b": 2}, run_id="kw-exc", guard=_FakeGuard(), keepwarm=kw, **env)
    assert kw.stopped is not None
    n = len(w.events)
    w.t += 1000
    kw.tick()
    assert len(w.events) == n
    # 죽은 run 도 이미 나간 keep-warm 의 제외 줄은 남는다(keepwarm.json). meta.json 은 없다 - 판정기가 부분 run 을 판정하지 않게
    from bench import canary
    saved = json.load(open(tmp_path / "runs" / "kw-exc" / "keepwarm.json"))
    assert saved["sent"] == 1 and saved["stopped"] is not None
    assert canary.parse_exclude(saved["exclude_line"])
    assert not (tmp_path / "runs" / "kw-exc" / "meta.json").exists()


def test_keepwarm_does_not_start_when_the_guard_refuses_to_start(tmp_path, monkeypatch):
    w = _KwWorld()
    with pytest.raises(SystemExit):
        _kw_run(tmp_path, monkeypatch, w, guard=_FakeGuard(abort_at=1), run_id="kw-refuse")
    assert w.events == []                                      # 시작 전 거부 - 벤치가 안 도니 운영 사이드카에 아무것도 안 보낸다


def test_run_without_keepwarm_sends_nothing_and_records_it_as_off(tmp_path, monkeypatch, _no_real_network_send):
    w = _KwWorld()
    out, _ = _kw_run(tmp_path, monkeypatch, w, run_id="kw-off", use_keepwarm=False)
    assert "kw" not in w.events and _no_real_network_send == []
    assert json.load(open(f"{out}/meta.json"))["keepwarm"] is None
    assert json.load(open(f"{out}/meta.json"))["prod_sidecar_keepwarm"] is None


def test_cli_turns_on_the_production_keepwarm(monkeypatch):
    from bench.keepwarm import PROD_RERANK_URL, KeepWarm
    got = {}
    monkeypatch.setattr(R, "read_token", lambda: "t")
    monkeypatch.setattr(R, "run", lambda *a, **k: got.update(k) or "out")
    assert R.main(["--arm", "a=1", "--arm", "b=2", "--run-id", "x"]) == 0
    assert isinstance(got["keepwarm"], KeepWarm) and got["keepwarm"].url == PROD_RERANK_URL


# ---------------------------------------------------------------- issue #297: 세트 공통 시계 옵션

_REAL_COMMON_CLOCK = R.common_bench_clock        # _fake_run_env 가 바꾸기 전 원본
TWO_SETS = {"s1": {"bench_clock": "2026-10-27T04:31:00Z"}, "s2": {"bench_clock": "2026-10-30T09:00:00Z"}}


def test_without_a_clock_set_differing_set_clocks_are_still_refused():
    with pytest.raises(SystemExit):
        R.resolve_bench_clock(TWO_SETS)
    with pytest.raises(SystemExit):
        R.resolve_bench_clock(TWO_SETS, None)


def test_clock_set_uses_the_named_sets_clock_and_records_all_clocks():
    clock, info = R.resolve_bench_clock(TWO_SETS, "s1")
    assert clock == "2026-10-27T04:31:00Z"
    assert info == {"from_set": "s1", "set_clocks": {"s1": "2026-10-27T04:31:00Z", "s2": "2026-10-30T09:00:00Z"},
                    "clocks_differ": True}


@pytest.mark.parametrize("reps, s", [(TWO_SETS, "s3"), ({**TWO_SETS, "s1": {}}, "s1"), ({**TWO_SETS, "s2": None}, "s1")])
def test_clock_set_must_be_a_run_set_with_a_clock_and_every_set_needs_a_clock(reps, s):
    with pytest.raises(SystemExit):
        R.resolve_bench_clock(reps, s)


def test_same_clocks_without_option_keep_the_old_meta_shape():
    clock, info = R.resolve_bench_clock({"s1": {"bench_clock": "c"}, "s2": {"bench_clock": "c"}})
    assert clock == "c" and info is None


def test_run_meta_records_the_common_clock_and_calls_carry_it(tmp_path, monkeypatch):
    recalls = []
    kw = _fake_run_env(tmp_path, monkeypatch, recalls)
    monkeypatch.setattr(R, "provenance", lambda arms: {"replicas": TWO_SETS, "arms": {}})
    monkeypatch.setattr(R, "common_bench_clock", _REAL_COMMON_CLOCK)
    sent = []
    orig = R.run

    class Client:
        def __init__(self, port, token):
            pass

        def open(self):
            pass

        def recall(self, args):
            sent.append(args["query_timestamp"])
            return {"latency_ms": 1.0, "outcome": "transport-error", "detail": "fake"}

    monkeypatch.setattr(R, "McpClient", Client)
    out = orig({"base": 1, "cand": 2}, run_id="r1", guard=_FakeGuard(), clock_set="s1", **kw)
    meta = json.loads(open(f"{out}/meta.json").read())
    assert meta["bench_clock"] == "2026-10-27T04:31:00Z"
    assert meta["bench_clock_common"]["from_set"] == "s1" and meta["bench_clock_common"]["clocks_differ"] is True
    assert sent and set(sent) == {"2026-10-27T04:31:00Z"}
    with pytest.raises(SystemExit):
        orig({"base": 1, "cand": 2}, run_id="r2", guard=_FakeGuard(), **kw)


def test_cli_passes_the_clock_set_to_run(monkeypatch):
    seen = {}
    monkeypatch.setattr(R, "run", lambda *a, **k: seen.update(k) or "out")
    monkeypatch.setattr(R, "read_token", lambda: "t")
    monkeypatch.setattr(R.KeepWarm, "production", staticmethod(lambda: None))
    assert R.main(["--arm", "base=18891", "--arm", "cand=18892", "--clock-from-set", "s1"]) == 0
    assert seen["clock_set"] == "s1"
