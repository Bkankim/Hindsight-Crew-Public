"""bench.prune_effect - #15(chunk_id 형식 파생 제거) 운영 재확인 도구 (issue #31).

합성 응답으로 고정한다: 두 프루너 결과가 chunk_id 외 동일, 복원 불가 chunk_id 는 남음, 토큰 집계.
운영 호출(docker compose exec)은 여기서 돌리지 않는다.
"""
import json
import os
import subprocess

import pytest

from bench import prune_effect as pe
from gateway import recall_prune

BANK = "personal-demo"


def _row(i, doc, chunk, **extra):
    row = {"id": f"m{i}", "text": f"본문 {i}", "fact_type": "world", "context": "", "occurred_start": None,
           "occurred_end": None, "mentioned_at": None, "document_id": doc, "chunk_id": chunk,
           "tags": ["project:x"], "scores": {"final": 0.9 - i / 100, "reranker": 0.5}}
    row.update(extra)
    return row


def _tool_result(rows, extra_payload=None):
    payload = {"results": rows, "entities": None, "chunks": None}
    payload.update(extra_payload or {})
    blob = json.dumps(payload, ensure_ascii=False)
    return {"content": [{"type": "text", "text": blob}], "structuredContent": {"result": blob}, "isError": False}


def _old(result):
    # #15 이전 프루너와 같은 동작: bank 를 모르면 FORMAT_DERIVABLE 이 발동하지 않는다
    return recall_prune.prune_tool_result(result)[0]


def _new(result, bank):
    return recall_prune.prune_tool_result(result, bank)[0]


class _Tok:
    def count(self, text):
        return len(text)


def _mixed():
    return _tool_result([
        _row(0, "d0", f"{BANK}_d0_0"),                 # 복원 가능
        _row(1, "d1", f"{BANK}_d1_3"),                 # 0 번이 아님 - 남는다
        _row(2, "d2", f"other-bank_d2_0"),             # 다른 bank - 남는다
        _row(3, "d3", f"{BANK}_d3_0"),                 # 복원 가능
        _row(4, "d4", "free-form-chunk"),              # 형식이 아님 - 남는다
    ])


def test_same_except_chunk_id_and_counts_restorable():
    rec = pe.compare(_mixed(), BANK, _old, _new, _Tok())
    assert rec["outcome"] == "ok"
    assert rec["invariant"] is True
    assert rec["n_results"] == 5
    assert rec["chunk_id_restorable"] == 2
    assert rec["chunk_id_kept"] == 3
    assert rec["tokens_old"] > rec["tokens_new"]
    assert rec["diff_other"] == []


def test_unrestorable_chunk_ids_survive_new_pruner():
    new_payload = json.loads(_new(_mixed(), BANK)["content"][0]["text"])
    kept = {r["id"]: r.get("chunk_id") for r in new_payload["results"]}
    assert kept == {"m0": None, "m1": f"{BANK}_d1_3", "m2": "other-bank_d2_0", "m3": None,
                    "m4": "free-form-chunk"}


def test_no_restorable_chunk_ids_means_equal_tokens():
    result = _tool_result([_row(0, "d0", f"{BANK}_d0_2"), _row(1, "d1", "x")])
    rec = pe.compare(result, BANK, _old, _new, _Tok())
    assert rec["invariant"] is True
    assert rec["chunk_id_restorable"] == 0
    assert rec["tokens_old"] == rec["tokens_new"]


def test_detects_difference_beyond_chunk_id():
    def bad_new(result, bank):
        out = _new(result, bank)
        payload = json.loads(out["content"][0]["text"])
        payload["results"][1]["text"] = "바뀐 본문"
        blob = json.dumps(payload, ensure_ascii=False)
        return {**out, "content": [{"type": "text", "text": blob}], "structuredContent": {"result": blob}}

    rec = pe.compare(_mixed(), BANK, _old, bad_new, _Tok())
    assert rec["invariant"] is False
    assert any("text" in d for d in rec["diff_other"])


def test_detects_reorder_and_wrong_chunk_removal():
    def reorder(result, bank):
        out = _new(result, bank)
        payload = json.loads(out["content"][0]["text"])
        payload["results"].reverse()
        blob = json.dumps(payload, ensure_ascii=False)
        return {**out, "content": [{"type": "text", "text": blob}]}

    assert pe.compare(_mixed(), BANK, _old, reorder, _Tok())["invariant"] is False

    def drop_all_chunks(result, bank):
        out = _new(result, bank)
        payload = json.loads(out["content"][0]["text"])
        for r in payload["results"]:
            r.pop("chunk_id", None)
        blob = json.dumps(payload, ensure_ascii=False)
        return {**out, "content": [{"type": "text", "text": blob}]}

    rec = pe.compare(_mixed(), BANK, _old, drop_all_chunks, _Tok())
    assert rec["invariant"] is False                   # 복원 불가 chunk_id 를 지우면 손실


def test_structured_content_must_match_content():
    def half(result, bank):
        out = _new(result, bank)
        return {**out, "structuredContent": result["structuredContent"]}    # 한쪽만 프루닝

    assert pe.compare(_mixed(), BANK, _old, half, _Tok())["invariant"] is False


def test_error_result_is_not_ok():
    err = {"content": [{"type": "text", "text": json.dumps({"error": "boom", "results": []})}], "isError": False}
    rec = pe.compare(err, BANK, _old, _new, _Tok())
    assert rec["outcome"] == "upstream-error"
    assert rec["invariant"] is None


def test_summarize_totals_and_saving():
    recs = [pe.compare(_mixed(), BANK, _old, _new, _Tok()),
            pe.compare(_tool_result([_row(0, "d0", "x")]), BANK, _old, _new, _Tok())]
    s = pe.summarize(recs)
    assert s["queries"] == 2 and s["ok"] == 2
    assert s["invariant"] == 2
    assert s["tokens_old"] == recs[0]["tokens_old"] + recs[1]["tokens_old"]
    assert s["tokens_new"] == recs[0]["tokens_new"] + recs[1]["tokens_new"]
    assert s["saving_pct"] == pytest.approx(100.0 * (s["tokens_new"] - s["tokens_old"]) / s["tokens_old"])
    assert s["chunk_id_restorable"] == 2 and s["chunk_id_kept"] == 4
    assert s["results"] == 6


def _ledger_line(ts, query, identity="demo", bank=BANK, **extra):
    row = {"ts": ts, "identity": identity, "bank": bank, "query": query, "session_id": "s"}
    row.update(extra)
    return json.dumps(row, ensure_ascii=False)


def test_select_queries_recent_unique_filtered(tmp_path):
    lines = [_ledger_line(f"2026-09-2{d}T00:00:0{i}Z", f"q{d}{i}") for d in range(1, 8) for i in range(4)]
    lines += [
        _ledger_line("2026-09-28T01:00:00Z", "q70  "),                 # 공백만 다른 중복(최신) - 1개로
        _ledger_line("2026-09-28T02:00:00Z", "smoke probe"),          # probe 제외
        _ledger_line("2026-09-28T03:00:00Z", "alice q", identity="alice"),
        _ledger_line("2026-09-28T04:00:00Z", "other bank", bank="team"),
        _ledger_line("2026-09-28T05:00:00Z", "tagged", tags=["project:x"], tags_match="all"),
        "not json",
    ]
    path = tmp_path / "recall-queries.log"
    path.write_text("\n".join(lines) + "\n")
    picked = pe.select_queries(str(path), n=5)
    assert [p["query"] for p in picked] == ["tagged", "q70  ", "q73", "q72", "q71"]
    assert picked[0] == {"query": "tagged", "tags": ["project:x"], "tags_match": "all"}
    assert all("max_tokens" not in p for p in picked)                 # MCP 기본값 = 생략


def test_parse_line_sse_and_classify():
    tr = _mixed()
    msg = {"jsonrpc": "2.0", "id": 2, "result": tr}
    body = "event: message\ndata: " + json.dumps(msg, ensure_ascii=False) + "\n\n"
    line = json.dumps({"i": 0, "t0": 1.0, "t1": 2.5, "http": 200, "ctype": "text/event-stream", "body": body})
    call = pe.parse_line(line)
    assert call["result"] == tr and call["http"] == 200 and call["elapsed_s"] == pytest.approx(1.5)


def test_kst_format():
    assert pe.kst(0.0) == "1970-01-01T09:00:00.000+09:00"


def test_load_old_pruner_from_git():
    # skip 은 기준 판본을 못 찾을 때만(사유는 실제 원인: git 리포 아님 / HEAD 없음 / 이력에 없음). 그 밖의 git 실패는 테스트 실패
    rev, why = pe.locate_old_rev()
    if rev is None:
        pytest.skip(why)
    old = pe.load_pruner(pe.old_pruner_source())
    assert not hasattr(old, "FORMAT_DERIVABLE")
    rec = pe.compare(_mixed(), BANK, lambda r: old.prune_tool_result(r)[0], _new, _Tok())
    assert rec["invariant"] is True and rec["chunk_id_restorable"] == 2


# issue #447: 공개판은 내보내기로 sha 가 모두 바뀐다. 운영판 sha 가 없으면 Exported-From trailer 로 기준 판본을 찾는다.
# 가짜 리포(데모 이름·데모 주소)로 trailer 경로만 본다 - 운영판 sha 경로는 위 test_load_old_pruner_from_git 이 본다.
# git 은 사용자 설정·훅·GIT_* 환경과 격리한다(훅 안에서 pytest 가 돌아도 실제 리포를 건드리지 않게).
_DEMO_GIT = ["-c", "user.name=demo", "-c", "user.email=demo@example.com", "-c", "commit.gpgsign=false",
             "-c", "core.hooksPath=/dev/null"]


@pytest.fixture
def demo_repo(tmp_path, monkeypatch):
    for key in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    def make(messages):
        """messages 차례로 gateway/recall_prune.py 를 v0, v1, ... 로 바꾸는 커밋을 쌓고 (경로, sha 목록)을 돌려준다."""
        repo = tmp_path / "demo-repo"
        (repo / "gateway").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        shas = []
        for i, msg in enumerate(messages):
            (repo / "gateway" / "recall_prune.py").write_text(f"VERSION = {i}\n")
            subprocess.run(["git", *_DEMO_GIT, "-C", str(repo), "add", "-A"], check=True)
            subprocess.run(["git", *_DEMO_GIT, "-C", str(repo), "commit", "-q", "-m", msg], check=True)
            shas.append(subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True,
                                       check=True).stdout.strip())
        return str(repo), shas
    return make


def _exported(sha, body="feat: demo"):
    return f"{body}\n\nRefs #1\nExported-From: {sha}"


def test_old_rev_found_by_exported_from_trailer(demo_repo):
    repo, shas = demo_repo([_exported("1" * 40), _exported(pe.OLD_COMMIT), _exported("2" * 40)])
    assert pe.resolve_old_rev(repo) == shas[1] + "^"
    assert pe.old_pruner_source(repo) == "VERSION = 0\n"     # #15 커밋의 부모 판본(의미 그대로)


@pytest.mark.parametrize("message", [
    "feat: demo",                                              # trailer 없음
    f"feat: demo\n\nExported-From: {pe.OLD_COMMIT}0",         # 다른 sha(접두만 같음)
    f"feat: demo\n\nNot-Exported-From: {pe.OLD_COMMIT}",      # 다른 키
])
def test_old_rev_absent_without_matching_trailer(demo_repo, message):
    repo, _ = demo_repo(["feat: base", message])
    assert pe.locate_old_rev(repo) == (None, pe.NOT_IN_HISTORY)
    with pytest.raises(RuntimeError, match="HEAD 이력에 없다"):
        pe.old_pruner_source(repo)


def test_old_rev_absent_when_trailer_commit_has_no_parent(demo_repo):
    repo, _ = demo_repo([_exported(pe.OLD_COMMIT)])   # 부모가 없는 커밋(shallow 경계와 같은 꼴)
    assert pe.locate_old_rev(repo) == (None, pe.NOT_IN_HISTORY)


def test_old_rev_ambiguous_trailer_is_an_error(demo_repo):
    repo, _ = demo_repo(["feat: base", _exported(pe.OLD_COMMIT), _exported(pe.OLD_COMMIT)])
    with pytest.raises(RuntimeError, match="모호"):
        pe.resolve_old_rev(repo)


def test_old_rev_ignores_trailer_line_quoted_in_body(demo_repo):
    # 본문에 trailer 줄을 인용한 뒷 커밋은 자기 마지막 Exported-From 이 다르므로 후보가 아니다
    quoted = _exported("3" * 40, body=f"docs: demo\n\nExported-From: {pe.OLD_COMMIT}\nquoted above")
    repo, shas = demo_repo(["feat: base", _exported(pe.OLD_COMMIT), quoted])
    assert pe.resolve_old_rev(repo) == shas[1] + "^"


def test_old_rev_absent_in_repo_without_commits(demo_repo):
    repo, _ = demo_repo([])                                  # git init 만 한 리포(unborn HEAD)
    assert pe.locate_old_rev(repo) == (None, pe.NO_HEAD)


def test_old_rev_absent_outside_git(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))   # 상위 리포로 올라가지 않게
    copy = tmp_path / "zip-copy"
    copy.mkdir()
    assert pe.locate_old_rev(str(copy)) == (None, pe.NOT_A_REPO)
    with pytest.raises(RuntimeError, match="git 리포가 아니다"):
        pe.old_pruner_source(str(copy))


# ---------------------------------------------------------------- final-review 20260929-000114 반영

class _FakeProc:
    def __init__(self, stdout, returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def _main_fakes(monkeypatch, call_upstream):
    import bench.runner
    monkeypatch.setattr(bench.runner, "Tokenizer", _Tok)
    monkeypatch.setattr(pe, "old_pruner_source", lambda *a, **k: "")
    monkeypatch.setattr(pe, "load_pruner", lambda *a, **k: type("Old", (), {
        "prune_tool_result": staticmethod(lambda r: recall_prune.prune_tool_result(r))})())
    monkeypatch.setattr(pe, "provenance", lambda *a, **k: {"repo_head": "x"}, raising=False)
    monkeypatch.setattr(pe, "call_upstream", call_upstream)


def test_main_zero_queries_exits_before_upstream(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("upstream must not be called")
    _main_fakes(monkeypatch, boom)
    ledger = tmp_path / "recall-queries.log"
    ledger.write_text(_ledger_line("2026-09-28T00:00:00Z", "q", identity="alice") + "\n")
    with pytest.raises(SystemExit) as e:
        pe.main(["--compose-dir", str(tmp_path), "--ledger", str(ledger), "--out-root", str(tmp_path / "out")])
    assert "no queries" in str(e.value)


@pytest.mark.parametrize("n", [0, -1])
def test_select_queries_nonpositive_n_selects_nothing(tmp_path, n):
    path = tmp_path / "recall-queries.log"
    path.write_text(_ledger_line("2026-09-28T00:00:00Z", "q1") + "\n")
    assert pe.select_queries(str(path), n=n) == []


def test_raw_records_pair_by_call_index(tmp_path, monkeypatch):
    fatal = {"i": None, "t0": None, "t1": None, "http": 500, "elapsed_s": None, "result": None, "error": "initialize"}
    _main_fakes(monkeypatch, lambda q, d: {"rc": 2, "stderr_tail": "", "t0": 1.0, "t1": 2.0, "calls": [fatal]})
    ledger = tmp_path / "recall-queries.log"
    ledger.write_text(_ledger_line("2026-09-28T00:00:00Z", "q1") + "\n")
    rc = pe.main(["--compose-dir", str(tmp_path), "--ledger", str(ledger), "--out-root", str(tmp_path / "out")])
    assert rc == 1
    (run_dir,) = (tmp_path / "out").iterdir()
    raw = [json.loads(l) for l in (run_dir / "raw.jsonl").read_text().splitlines()]
    assert raw[0]["args"] is None and raw[0]["error"] == "initialize"


def test_select_queries_same_second_prefers_later_line(tmp_path):
    path = tmp_path / "recall-queries.log"
    path.write_text(_ledger_line("2026-09-28T00:00:00Z", "q", tags=["project:a"]) + "\n"
                    + _ledger_line("2026-09-28T00:00:00Z", "q", tags=["project:b"]) + "\n")
    assert pe.select_queries(str(path), n=5) == [{"query": "q", "tags": ["project:b"]}]


def test_select_queries_skips_non_reusable_rows(tmp_path):
    path = tmp_path / "recall-queries.log"
    path.write_text(_ledger_line("2026-09-28T00:00:01Z", "q1", reusable=False) + "\n"
                    + _ledger_line("2026-09-28T00:00:00Z", "q2", reusable=True) + "\n")
    assert [p["query"] for p in pe.select_queries(str(path), n=5)] == ["q2"]


def test_load_old_pruner_independent_of_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)                       # 리포 밖에서도 같은 결과여야 한다
    test_load_old_pruner_from_git()


def test_tokens_use_same_serialization_on_both_sides():
    # null·중복 필드가 없어 이전 프루너가 손대지 않는 응답: raw 는 upstream 헤더 마운트처럼 indent=2 + \u 이스케이프
    row = {"id": "m0", "text": "한글 본문", "document_id": "d0", "chunk_id": f"{BANK}_d0_0"}
    raw = json.dumps({"results": [row]}, indent=2)
    result = {"content": [{"type": "text", "text": raw}], "structuredContent": {"result": raw}, "isError": False}
    assert recall_prune.prune_tool_result(result)[1] is False           # 전제: 이전 쪽은 무변경
    rec = pe.compare(result, BANK, _old, _new, _Tok())
    removed = len(',"chunk_id":"personal-demo_d0_0"')
    assert rec["tokens_old"] - rec["tokens_new"] == removed              # 직렬화 차이가 절감으로 섞이지 않는다
    assert rec["invariant"] is True


def test_structured_content_values_compared_directly():
    rows = [_row(0, "d0", f"{BANK}_d0_0"), _row(1, "d1", f"{BANK}_d1_3")]
    tr = _tool_result(rows)
    skew = json.loads(tr["structuredContent"]["result"])
    skew["results"][1]["tags"] = ["project:skew"]                        # raw 가 이미 inconsistent
    tr = {**tr, "structuredContent": {"result": json.dumps(skew, ensure_ascii=False)}}

    def bad_new(result, bank):
        out = _new(result, bank)
        inner = json.loads(out["structuredContent"]["result"])
        inner["results"][0]["text"] = "structured 쪽만 바뀐 본문"
        return {**out, "structuredContent": {"result": json.dumps(inner, ensure_ascii=False)}}

    assert pe.compare(tr, BANK, _old, _new, _Tok())["invariant"] is True
    assert pe.compare(tr, BANK, _old, bad_new, _Tok())["invariant"] is False


def test_call_upstream_keeps_going_on_non_json_line(monkeypatch):
    good = json.dumps({"i": 0, "t0": 1.0, "t1": 2.0, "http": 200, "ctype": "application/json",
                       "body": json.dumps({"jsonrpc": "2.0", "id": 1, "result": _mixed()})})
    monkeypatch.setattr(pe.subprocess, "run", lambda *a, **k: _FakeProc(good + "\nnot json {\n", returncode=137))
    run = pe.call_upstream([{"query": "q"}], "/nonexistent")
    assert run["rc"] == 137 and len(run["calls"]) == 2
    assert run["calls"][0]["result"] == _mixed()
    assert run["calls"][1]["result"] is None and run["calls"][1]["error"] == "non-json-line"


def test_report_records_provenance(tmp_path, monkeypatch):
    call = {"i": 0, "t0": 1.0, "t1": 2.0, "http": 200, "elapsed_s": 1.0, "result": _mixed(), "error": None}
    _main_fakes(monkeypatch, lambda q, d: {"rc": 0, "stderr_tail": "", "t0": 1.0, "t1": 2.0, "calls": [call]})
    monkeypatch.setattr(pe, "provenance", lambda compose_dir: {"repo_head": "abc", "repo_dirty": False,
                                                               "gateway_image": "sha256:img"})
    ledger = tmp_path / "recall-queries.log"
    ledger.write_text(_ledger_line("2026-09-28T00:00:00Z", "q1") + "\n")
    assert pe.main(["--compose-dir", str(tmp_path), "--ledger", str(ledger), "--out-root", str(tmp_path / "out")]) == 0
    (run_dir,) = (tmp_path / "out").iterdir()
    report = json.loads((run_dir / "report.json").read_text())
    assert report["provenance"] == {"repo_head": "abc", "repo_dirty": False, "gateway_image": "sha256:img"}


# issue #106: 측정은 `docker compose exec hindsight` 로 하므로 결과를 좌우하는 upstream(latest 태그) 이미지도 기록한다.
# docker·git 은 실제로 부르지 않는다 - subprocess.run 을 가짜로 바꿔 명령별로 응답한다.
_GW_ID, _HS_ID = "sha256:" + "a" * 64, "sha256:" + "b" * 64
_HS_REF = "ghcr.io/vectorize-io/hindsight:latest"
_HS_DIGEST = "ghcr.io/vectorize-io/hindsight@sha256:" + "c" * 64


def _fake_docker(monkeypatch, *, ps_hindsight="cid-hs\n", inspect_hindsight=None, image_inspect=None):
    """inspect_hindsight / image_inspect: None 이면 정상 응답, _FakeProc 이면 그것, Exception 이면 raise."""
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        if cmd[0] == "git":
            return _FakeProc("deadbeef\n")
        assert cmd[0] == "docker", cmd
        if cmd[1:3] == ["compose", "--env-file"]:
            svc = cmd[-1]
            if svc == "gateway":
                return _FakeProc("cid-gw\n")
            return ps_hindsight if isinstance(ps_hindsight, _FakeProc) else _FakeProc(ps_hindsight)
        if cmd[1] == "inspect":
            if cmd[-1] == "cid-gw":
                return _FakeProc(_GW_ID + "\n")
            return _resp(inspect_hindsight, f"{_HS_ID}\t{_HS_REF}\n")
        if cmd[1:3] == ["image", "inspect"]:
            assert cmd[-1] == _HS_ID
            return _resp(image_inspect, json.dumps([_HS_DIGEST]) + "\n")
        raise AssertionError(cmd)

    def _resp(spec, ok):
        if isinstance(spec, Exception):
            raise spec
        return spec if spec is not None else _FakeProc(ok)

    monkeypatch.setattr(pe.subprocess, "run", run)
    return calls


def test_provenance_records_the_hindsight_image_id_ref_and_digest(tmp_path, monkeypatch):
    calls = _fake_docker(monkeypatch)
    prov = pe.provenance(str(tmp_path))
    assert prov["gateway_image"] == _GW_ID                                    # 기존 키는 그대로
    assert prov["hindsight_image"] == {"id": _HS_ID, "ref": _HS_REF, "repo_digests": [_HS_DIGEST], "error": None}
    assert all(c[0] in ("git", "docker") for c in calls)


@pytest.mark.parametrize("failure,error_prefix,id_kept", [
    ("ps-empty", "container-not-found", False),
    ("ps-nonzero", "container-not-found", False),
    ("inspect-nonzero", "inspect-failed", False),
    ("inspect-oserror", "inspect-failed", False),
    ("inspect-timeout", "inspect-failed", False),
    ("digest-nonzero", "digest-inspect-failed", True),
    ("digest-oserror", "digest-inspect-failed", True),
])
def test_provenance_records_a_failed_hindsight_inspect_as_a_failure_not_as_absence(tmp_path, monkeypatch, failure, error_prefix, id_kept):
    """inspect 가 실패하면 이미지 값 None 만 남기지 않는다 - 실패 사실과 이유를 error 에 적는다(측정 결과를 해석할 때 구분돼야 한다)."""
    fail = _FakeProc("", returncode=1, stderr="Error: No such object")
    kw = {"ps-empty": {"ps_hindsight": ""},
          "ps-nonzero": {"ps_hindsight": _FakeProc("", returncode=1, stderr="no configuration file provided")},
          "inspect-nonzero": {"inspect_hindsight": fail},
          "inspect-oserror": {"inspect_hindsight": FileNotFoundError("docker")},
          "inspect-timeout": {"inspect_hindsight": subprocess.TimeoutExpired("docker", 30)},
          "digest-nonzero": {"image_inspect": fail},
          "digest-oserror": {"image_inspect": FileNotFoundError("docker")}}[failure]
    _fake_docker(monkeypatch, **kw)
    prov = pe.provenance(str(tmp_path))
    hs = prov["hindsight_image"]
    assert hs["error"].startswith(error_prefix)
    assert (hs["id"] == _HS_ID) is id_kept and (hs["repo_digests"] is None)
    assert prov["gateway_image"] == _GW_ID                                    # 게이트웨이 기록은 영향받지 않는다


def test_provenance_hindsight_image_without_repo_digests_is_not_a_failure(tmp_path, monkeypatch):
    """로컬에서 빌드한 이미지는 RepoDigests 가 빈 목록이다 - 실패가 아니라 빈 목록으로 남긴다."""
    _fake_docker(monkeypatch, image_inspect=_FakeProc("[]\n"))
    assert pe.provenance(str(tmp_path))["hindsight_image"] == {"id": _HS_ID, "ref": _HS_REF, "repo_digests": [], "error": None}


def test_report_carries_the_hindsight_image_provenance(tmp_path, monkeypatch):
    real_provenance = pe.provenance                  # _main_fakes 가 provenance 를 가짜로 바꾸므로 되돌린다
    call = {"i": 0, "t0": 1.0, "t1": 2.0, "http": 200, "elapsed_s": 1.0, "result": _mixed(), "error": None}
    _main_fakes(monkeypatch, lambda q, d: {"rc": 0, "stderr_tail": "", "t0": 1.0, "t1": 2.0, "calls": [call]})
    monkeypatch.setattr(pe, "provenance", real_provenance)
    _fake_docker(monkeypatch)
    ledger = tmp_path / "recall-queries.log"
    ledger.write_text(_ledger_line("2026-09-28T00:00:00Z", "q1") + "\n")
    assert pe.main(["--compose-dir", str(tmp_path), "--ledger", str(ledger), "--out-root", str(tmp_path / "out")]) == 0
    (run_dir,) = (tmp_path / "out").iterdir()
    prov = json.loads((run_dir / "report.json").read_text())["provenance"]
    assert prov["hindsight_image"]["id"] == _HS_ID and prov["hindsight_image"]["repo_digests"] == [_HS_DIGEST]


def test_provenance_fails_soft_without_docker(tmp_path, monkeypatch):
    def no_docker(cmd, **kw):                                           # docker 실행 파일이 없는 환경(실 docker 호출 없음)
        if cmd[0] == "docker":
            raise FileNotFoundError("docker")
        return _FakeProc("deadbeef\n")
    monkeypatch.setattr(pe.subprocess, "run", no_docker)
    prov = pe.provenance(str(tmp_path))
    assert set(prov) >= {"repo_head", "repo_dirty", "recall_prune_sha", "gateway_image", "hindsight_image"}
    assert prov["gateway_image"] is None
    assert prov["hindsight_image"]["id"] is None and prov["hindsight_image"]["error"].startswith("container-not-found")


# issue #129: compose-go dotenv 파서는 형식이 틀린 .env 줄 원문을 오류에 싣는다. stderr 는 report.json·터미널로 나가므로
# 기록 전에 마스킹한다. 시크릿 모양 값은 전부 테스트 안에서 만든 가짜 값이다(실 docker·.env.local 은 건드리지 않는다).
_SECRET = "FAKE-SECRET-xyz123"
_STDERR_CASES = [
    pytest.param(f'failed to read .env.local: line 3: unexpected character "-" in variable name "BAD-KEY={_SECRET}"',
                 ["failed to read .env.local", "line 3", "unexpected character", "in variable name"], id="bad-key-char"),
    pytest.param(f'failed to read .env.local: line 5: unterminated quoted value "{_SECRET}',
                 ["failed to read .env.local", "line 5", "unterminated quoted value"], id="unterminated-quote"),
    pytest.param('failed to read .env.local: line 7: unexpected character "\\"" in variable name "K=\\"%s"' % _SECRET,
                 ["line 7", "unexpected character", "in variable name"], id="escaped-quote-in-line"),
    pytest.param('time="x" level=warning msg="y"\nfailed to read .env.local: line 9: unterminated quoted value \'%s\n'
                 'HC_OTHER=%s' % (_SECRET, _SECRET), ["line 9", "unterminated quoted value"], id="multi-line"),
    pytest.param('time="2026-10-02T12:00:00+09:00" level=warning msg="The \\"HC_X\\" variable is not set. Defaulting to a blank string."',
                 ['level=warning', 'time="2026-10-02T12:00:00+09:00"',
                  'The \\"HC_X\\" variable is not set. Defaulting to a blank string.'], id="logfmt-warning"),
    pytest.param('level=warning msg="bad HC_OTHER=\\"abc %s\\""' % _SECRET, ['level=warning'], id="logfmt-escaped-quote-inner"),
    pytest.param('level=info msg=%s' % _SECRET, ['level=info'], id="logfmt-msg-unquoted"),
    pytest.param("level=info msg='%s'" % _SECRET, ['level=info'], id="logfmt-msg-single-quoted"),
    pytest.param('/msg=%s' % _SECRET, ['msg=<redacted>'], id="logfmt-key-after-slash"),
    pytest.param(':level=%s' % _SECRET, ['level=<redacted>'], id="logfmt-key-after-colon"),
    pytest.param('level=%s' % _SECRET, ['level=<redacted>'], id="logfmt-level-secret"),
    pytest.param('time=%s' % _SECRET, ['time=<redacted>'], id="logfmt-time-secret"),
    # Go 는 값을 \n 에서만 자르지만 Python splitlines() 는 이 문자들에서도 줄을 쪼갠다
    *[pytest.param(f'failed to read .env.local: line 5: unterminated quoted value "abc{sep}{_SECRET}',
                   ['line 5', 'unterminated quoted value'], id=f"separator-{ord(sep):04x}")
      for sep in (" ", "\x0b", "\x0c", "\x85")],
    pytest.param('service "hindsight" is not running', ['service "hindsight" is not running'], id="plain-error"),
]


def _assert_masked(text, keeps):
    assert _SECRET not in text and "SECRET-xyz" not in text, text
    for frag in keeps:
        assert frag in text, (frag, text)


@pytest.mark.parametrize("stderr,keeps", _STDERR_CASES)
def test_call_upstream_stderr_tail_masks_dotenv_values(monkeypatch, stderr, keeps):
    monkeypatch.setattr(pe.subprocess, "run", lambda *a, **k: _FakeProc("", returncode=1, stderr=stderr))
    run = pe.call_upstream([{"query": "q"}], "/nonexistent")
    assert run["rc"] == 1                                              # 실패 사실(rc)은 남는다
    _assert_masked(run["stderr_tail"], keeps)


def test_call_upstream_masks_before_taking_the_tail(monkeypatch):
    """꼬리 500자를 먼저 자르면 시크릿 중간부터 남아 정규식이 못 잡는다 - 마스킹이 먼저다."""
    head = f'failed to read .env.local: line 5: unterminated quoted value "{_SECRET}'
    stderr = head + "\n" + "w" * 486                                    # 원문 꼬리 500자가 시크릿 중간("SECRET-xyz123")부터 시작
    assert "SECRET-xyz123" in stderr[-500:]
    monkeypatch.setattr(pe.subprocess, "run", lambda *a, **k: _FakeProc("", returncode=1, stderr=stderr))
    tail = pe.call_upstream([{"query": "q"}], "/nonexistent")["stderr_tail"]
    assert "SECRET-xyz" not in tail and len(tail) <= 500


@pytest.mark.parametrize("where", ["ps", "inspect", "digest"])
@pytest.mark.parametrize("stderr,keeps", _STDERR_CASES)
def test_provenance_error_masks_dotenv_values(tmp_path, monkeypatch, where, stderr, keeps):
    fail = _FakeProc("", returncode=1, stderr=stderr)
    _fake_docker(monkeypatch, **{"ps": {"ps_hindsight": fail}, "inspect": {"inspect_hindsight": fail},
                                 "digest": {"image_inspect": fail}}[where])
    error = pe.provenance(str(tmp_path))["hindsight_image"]["error"]
    assert "rc=1" in error                                             # rc 는 남는다
    _assert_masked(error, keeps)


@pytest.mark.parametrize("stderr,keeps", _STDERR_CASES)
def test_report_json_and_terminal_never_carry_the_secret(tmp_path, monkeypatch, capsys, stderr, keeps):
    """call_upstream·provenance 를 실제 코드로 두고 docker 만 가짜로 - report.json 과 print 양쪽을 본다."""
    real_provenance, real_call_upstream = pe.provenance, pe.call_upstream
    fail = _FakeProc("", returncode=1, stderr=stderr)
    _fake_docker(monkeypatch, ps_hindsight=fail)                       # compose ps·exec 둘 다 같은 stderr 로 실패한다
    _main_fakes(monkeypatch, real_call_upstream)
    monkeypatch.setattr(pe, "provenance", real_provenance)
    ledger = tmp_path / "recall-queries.log"
    ledger.write_text(_ledger_line("2026-09-28T00:00:00Z", "q1") + "\n")
    pe.main(["--compose-dir", str(tmp_path), "--ledger", str(ledger), "--out-root", str(tmp_path / "out")])
    (run_dir,) = (tmp_path / "out").iterdir()
    report = json.loads((run_dir / "report.json").read_text())
    _assert_masked(report["stderr_tail"], keeps)
    _assert_masked(report["provenance"]["hindsight_image"]["error"], keeps)
    out = capsys.readouterr().out
    assert _SECRET not in out and "SECRET-xyz" not in out
    assert (run_dir / "report.json").read_text().count(_SECRET) == 0


def test_report_json_is_0600_even_with_a_loose_umask(tmp_path, monkeypatch):
    import os
    import stat
    call = {"i": 0, "t0": 1.0, "t1": 2.0, "http": 200, "elapsed_s": 1.0, "result": _mixed(), "error": None}
    _main_fakes(monkeypatch, lambda q, d: {"rc": 0, "stderr_tail": "", "t0": 1.0, "t1": 2.0, "calls": [call]})
    ledger = tmp_path / "recall-queries.log"
    ledger.write_text(_ledger_line("2026-09-28T00:00:00Z", "q1") + "\n")
    old = os.umask(0)                                                  # 가장 느슨한 umask 에서도 0600 이어야 한다
    try:
        pe.main(["--compose-dir", str(tmp_path), "--ledger", str(ledger), "--out-root", str(tmp_path / "out")])
    finally:
        os.umask(old)
    (run_dir,) = (tmp_path / "out").iterdir()
    assert stat.S_IMODE(os.stat(run_dir / "report.json").st_mode) == 0o600
    assert stat.S_IMODE(os.stat(run_dir / "raw.jsonl").st_mode) == 0o600


def test_provenance_null_repo_digests_is_recorded_as_no_repo_digests(tmp_path, monkeypatch):
    """inspect 는 성공했고 RepoDigests 필드가 null 이다 - digest-inspect-failed/empty output 이 아니라 사실대로 적는다."""
    _fake_docker(monkeypatch, image_inspect=_FakeProc("null\n"))
    assert pe.provenance(str(tmp_path))["hindsight_image"] == {
        "id": _HS_ID, "ref": _HS_REF, "repo_digests": None, "error": "no-repo-digests"}
