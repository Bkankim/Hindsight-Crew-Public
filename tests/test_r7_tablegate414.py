"""R7-7 table-gate 표 정확도 판정 + GitHub 호출 시한(issue #414, 상위 #393). 네트워크 0, LLM 0.

고정하는 것(parametrize 행렬):
  (a) `table-gate` 가 표마다 `열림 후보` 정밀도 < 95%, `확인된 종결` 오류 > 0, `종결(미계획)` 오류 > 0 이면 rollback(exit 4).
      값이 없거나 열림 후보 분모가 0 이면 실패(runner_failure, exit 1, 요약 `gate.verdict` 는 pass 가 아님). 문턱은 표마다
      적용한다(풀링 정밀도가 95% 이상이어도 한 표가 낮으면 rollback, 코디네이터 결정).
  (b) `r7_eval.gh_paged` 가 subprocess 시간 제한(기본 60초)을 넘기고, 초과는 `V.Fail`(실행기 실패)이다. 결과 모양은 그대로.
      `smoke`·`table-gate`·`owners-check` 의 `--gh-timeout` 이 그 시한으로 간다(가짜 subprocess).
"""
import json
import shutil
import subprocess

import pytest

from bench import r7_endtoend as S
from bench import r7_eval as V
from test_r7_endtoend import T0_TEXT, _fetch_factory, attached_recall, gens_of, item, smoke_gen, write_json
from test_r7_preflight2 import _listing


def _tg_argv(tmp_path, recs, store, al, extra=()):
    owners = write_json(tmp_path / "owners.json", {"hindsight-crew": "o"})
    work = tmp_path / "w"
    work.mkdir(mode=0o700, exist_ok=True)
    b, m = _listing(tmp_path)
    argv = ["table-gate", "--t0", T0_TEXT]
    for r in recs:
        argv += ["--recall", r]
    argv += ["--store", store, "--aliases", al, "--listing", b, m, "--github-owners", owners,
             "--github-default-owner", "o", "--work", str(work), "--label", "tg", *extra]
    return argv, work


def _check(cand_rows, open_issue, closed_err=0, np_err=0, drop=(), unlinked=0, parsed=True):
    """`table_check` 가 낼 칸(정확도 판정에 쓰는 칸만 조절). cand_rows·open_issue = GitHub 연결 열림 후보(관문 분모·분자,
    rework #414-1), unlinked = 미연결 열림 후보 줄(전체 정밀도 분모에만, 분자 0). drop = 지울 칸 경로."""
    d = {"rows": 30, "truncated": False, "counts": {}, "parsed_matches_counts": parsed,
         "candidate": {"rows": cand_rows + unlinked, "open_issue": open_issue,
                       "precision": S._ratio(open_issue, cand_rows + unlinked),
                       "linked_rows": cand_rows, "linked_open_issue": open_issue,
                       "linked_precision": S._ratio(open_issue, cand_rows)},
         "closed_rows": 8, "closed_errors": closed_err, "closed_errors_open_issue": 0,
         "not_planned_rows": 2, "not_planned_errors": np_err, "not_planned_errors_open_issue": 0,
         "unknown_reasons": {}, "by_state": {}, "fetched_at": None}
    for path in drop:
        head, _, tail = path.partition(".")
        if tail:
            d[head].pop(tail)
        else:
            d.pop(head)
    return d


# 이름, table_check 칸, 종료 코드, 요약 verdict, 표 판정, 롤백 범주(값), 실패 코드 앞부분
ACC = [
    ("precision-0", _check(20, 0), 4, "rollback", "rollback", {"candidate_precision_low": 1}, None),
    ("precision-94.9", _check(1000, 949), 4, "rollback", "rollback", {"candidate_precision_low": 1}, None),
    ("precision-95", _check(20, 19), 0, "pass", "pass", {}, None),
    ("precision-100", _check(20, 20), 0, "pass", "pass", {}, None),
    ("closed-0", _check(20, 20, closed_err=0), 0, "pass", "pass", {}, None),
    ("closed-2", _check(20, 20, closed_err=2), 4, "rollback", "rollback", {"closed_errors": 2}, None),
    ("not-planned-0", _check(20, 20, np_err=0), 0, "pass", "pass", {}, None),
    ("not-planned-1", _check(20, 20, np_err=1), 4, "rollback", "rollback", {"not_planned_errors": 1}, None),
    ("missing-precision", _check(20, 20, drop=("candidate.linked_precision",)), 1, "rollback", "fail", {},
     "missing:candidate.linked_precision"),
    # rework #414-1: 관문은 GitHub 연결 키 기준. 미연결 줄 10개로 전체 정밀도가 66.7% 여도 연결 100% 면 통과
    ("unlinked-ignored", _check(20, 20, unlinked=10), 0, "pass", "pass", {}, None),
    ("unlinked-but-linked-low", _check(20, 18, unlinked=0), 4, "rollback", "rollback", {"candidate_precision_low": 1},
     None),
    ("linked-empty", _check(0, 0, unlinked=5), 1, "rollback", "fail", {}, "empty:candidate.linked"),
    # rework #414-1: 표 줄 파싱 수 != counts 합 -> 실패
    ("counts-mismatch", _check(20, 20, parsed=False), 1, "rollback", "fail", {}, "counts-mismatch"),
    ("counts-check-missing", _check(20, 20, drop=("parsed_matches_counts",)), 1, "rollback", "fail", {},
     "missing:parsed_matches_counts"),
    ("missing-candidate", _check(20, 20, drop=("candidate",)), 1, "rollback", "fail", {}, "missing:candidate"),
    ("missing-closed-errors", _check(20, 20, drop=("closed_errors",)), 1, "rollback", "fail", {},
     "missing:closed_errors"),
    ("missing-not-planned-errors", _check(20, 20, drop=("not_planned_errors",)), 1, "rollback", "fail", {},
     "missing:not_planned_errors"),
    ("empty-denominator", _check(0, 0), 1, "rollback", "fail", {}, "empty:candidate.linked"),
]


@pytest.mark.parametrize("name,chk,rc,verdict,tverdict,cats,fail", ACC, ids=[c[0] for c in ACC])
def test_table_gate_accuracy_verdict(tmp_path, monkeypatch, capsys, name, chk, rc, verdict, tverdict, cats, fail):
    rec, store, al = attached_recall(tmp_path)
    monkeypatch.setattr(S, "table_check", lambda tab, gh, t0: json.loads(json.dumps(chk)))
    argv, work = _tg_argv(tmp_path, [rec], store, al)
    fetch, fill = _fetch_factory()
    assert S.cmd_table_gate(S.parse_args(argv), fetch=fetch, fill=fill) == rc
    out = json.loads((work / "tg-tables.json").read_text())
    assert out["gate"]["verdict"] == verdict
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["verdict"] == out["gate"]["verdict"]
    t = out["tables"][next(iter(out["tables"]))]
    assert t["accuracy"]["verdict"] == tverdict
    rb = out["gate"]["rollback"]
    for k in ("candidate_precision_low", "closed_errors", "not_planned_errors"):
        assert rb[k] == cats.get(k, 0)
    if fail:
        assert any(f.startswith(fail) for f in out["fail"]), out["fail"]
        assert rb["runner_failure"] == len(out["fail"]) > 0
    else:
        assert out["fail"] == []


def test_table_gate_threshold_is_per_table(tmp_path, monkeypatch):
    """두 표: 100%(100/100) + 90%(9/10). 풀링 99.1% 이지만 둘째 표가 95% 미만이라 rollback(코디네이터 결정: 표마다)."""
    rec, store, al = attached_recall(tmp_path)
    rec2 = str(tmp_path / "second-attached.json")
    shutil.copyfile(rec, rec2)
    seq = iter([_check(100, 100), _check(10, 9)])
    monkeypatch.setattr(S, "table_check", lambda tab, gh, t0: next(seq))
    argv, work = _tg_argv(tmp_path, [rec, rec2], store, al)
    fetch, fill = _fetch_factory()
    assert S.cmd_table_gate(S.parse_args(argv), fetch=fetch, fill=fill) == S.EXIT_ROLLBACK
    out = json.loads((work / "tg-tables.json").read_text())
    got = {n: v["accuracy"]["verdict"] for n, v in out["tables"].items()}
    assert sorted(got.values()) == ["pass", "rollback"]
    assert out["tables"]["second-attached"]["accuracy"]["verdict"] == "rollback"
    assert out["gate"]["rollback"]["candidate_precision_low"] == 1 and out["gate"]["verdict"] == "rollback"


def test_table_accuracy_unattached_is_not_judged():
    """미부착 표는 정확도 판정 대상이 아니다(이미 `no-table:` 실패)."""
    tables = {"a": {"attached": False}, "b": dict(_check(20, 20), attached=True)}
    cats, fail = S.table_accuracy(tables)
    assert fail == [] and cats == {"candidate_precision_low": 0, "closed_errors": 0, "not_planned_errors": 0}
    assert "accuracy" not in tables["a"] and tables["b"]["accuracy"]["verdict"] == "pass"


def test_real_table_gate_fixture_reports_accuracy(tmp_path):
    """가짜 없이 고정 입력: 표마다 accuracy 칸과 gate 의 새 범주가 있다."""
    rec, store, al = attached_recall(tmp_path)
    argv, work = _tg_argv(tmp_path, [rec], store, al)
    fetch, fill = _fetch_factory()
    rc = S.cmd_table_gate(S.parse_args(argv), fetch=fetch, fill=fill)
    out = json.loads((work / "tg-tables.json").read_text())
    t = out["tables"][next(iter(out["tables"]))]
    acc = t["accuracy"]
    assert acc["precision"] == t["candidate"]["linked_precision"]          # rework #414-1: 관문 = 연결 키 기준
    assert acc["precision_all"] == t["candidate"]["precision"]             # 전체 정밀도는 공개 칸
    assert acc["closed_errors"] == t["closed_errors"] and acc["not_planned_errors"] == t["not_planned_errors"]
    assert set(out["gate"]["rollback"]) >= {"candidate_precision_low", "closed_errors", "not_planned_errors"}
    assert rc == (0 if out["gate"]["verdict"] == "pass" else S.EXIT_ROLLBACK)


# --- (b) GitHub 호출 시한 ---------------------------------------------------------------------------------------

class FakeRun:
    def __init__(self, mode="ok", stdout='[[{"number": 1}], [{"number": 2}]]', stderr=""):
        self.mode, self.stdout, self.stderr, self.timeouts = mode, stdout, stderr, []

    def __call__(self, cmd, **kw):
        self.timeouts.append(kw.get("timeout"))
        if self.mode == "timeout":
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        rc = 0 if self.mode == "ok" else 1
        return subprocess.CompletedProcess(cmd, rc, stdout=self.stdout if rc == 0 else "", stderr=self.stderr)


@pytest.mark.parametrize("kw,want", [({}, 60.0), ({"timeout_s": 5.0}, 5.0)])
def test_gh_paged_passes_timeout_and_result_unchanged(monkeypatch, kw, want):
    fake = FakeRun()
    monkeypatch.setattr(V.subprocess, "run", fake)
    assert V.gh_paged("repos/o/r/issues", **kw) == (200, [{"number": 1}, {"number": 2}], 2)
    assert fake.timeouts == [want]


def test_gh_paged_404_unchanged(monkeypatch):
    monkeypatch.setattr(V.subprocess, "run", FakeRun(mode="err", stderr="gh: Not Found (HTTP 404)"))
    assert V.gh_paged("repos/o/r") == (404, [], 1)


def test_gh_paged_timeout_is_runner_failure(monkeypatch):
    monkeypatch.setattr(V.subprocess, "run", FakeRun(mode="timeout"))
    with pytest.raises(V.Fail, match="timed out"):
        V.gh_paged("repos/o/r/issues?state=all")


@pytest.mark.parametrize("extra,want", [((), 60.0), (("--gh-timeout", "7"), 7.0)], ids=["default", "arg"])
@pytest.mark.parametrize("cmd", ["table-gate", "smoke", "owners-check"])
def test_commands_gh_timeout_is_failure(tmp_path, monkeypatch, capsys, cmd, extra, want):
    fake = FakeRun(mode="timeout")
    monkeypatch.setattr(V.subprocess, "run", fake)
    rec, store, al = attached_recall(tmp_path)
    if cmd == "table-gate":
        argv, work = _tg_argv(tmp_path, [rec], store, al, extra)
        summary = work / "tg-tables.json"
    elif cmd == "smoke":
        argv, work = _tg_argv(tmp_path, [rec], store, al)
        gen = smoke_gen(tmp_path, rec, gens_of(*([[item("hindsight-crew#1")]] * 3)))
        i = argv.index("--label")
        argv = ["smoke", "--gen", gen] + argv[1:i] + ["--label", "sm", *extra]
        summary = work / "sm-smoke.json"
    else:
        owners = write_json(tmp_path / "owners.json", {"hindsight-crew": "o"})
        argv = ["owners-check", "--store", store, "--owners", owners, "--default-owner", "o", "--aliases", al,
                "--out", str(tmp_path / "oc.json"), *extra]
        summary = None
    assert S.main(argv) == 1
    assert fake.timeouts and set(fake.timeouts) == {want}
    if summary is not None:
        out = json.loads(summary.read_text())
        assert out["gate"]["verdict"] != "pass"
        assert any("timed out" in f for f in out["fail"]), out["fail"]
