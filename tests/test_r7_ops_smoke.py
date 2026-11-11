"""운영 연막 한 회차 래퍼 `bench/r7_ops_smoke.sh`(issue #414, 상위 #393). 가짜 하위 명령으로 순서·종료 코드 합성만 본다.
네트워크 0, LLM 0, 운영 호출 0(R7_CMD 를 가짜로 바꾼다. 스냅샷은 임시 sqlite).
"""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "bench", "r7_ops_smoke.sh")
CATS = ("candidate_precision_low", "closed_errors", "cross_conflict_rows", "github_finished", "not_planned_errors",
        "over_max_rows", "runner_failure", "table_refuted")

FAKE = r'''
import json, os, sys
a = sys.argv[1:]
sub = a[0]
def opt(k):
    return a[a.index(k) + 1] if k in a else None
if sub in ("smoke-dllm", "smoke"):
    name = opt("--label").rsplit("-", 1)[-1]
elif sub == "smoke-attach":
    name = os.path.basename(opt("--recall")).rsplit(".json", 1)[0]
else:
    name = None
key = sub if name is None else "%s:%s" % (sub, name)
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write(json.dumps({"key": key, "argv": a}) + "\n")
full = json.loads(os.environ.get("FAKE_SPEC") or "{}")
spec = full.get(key) or {}
TAB = {"text": "t", "counts": {}}
def doc(name, table):
    return {"query_timestamp": opt("--t0"), "runs": [{"q": 1, "open_table": TAB if table else None}], "name": name}
if sub == "smoke-recall" and spec.get("rc", 0) == 0:       # 운영 응답 모양: 게이트웨이가 낸 표(open_table) 유무
    tabs = full["_tables"] if "_tables" in full else {"symptom": True, "project": True}
    for q in [a[i + 1] for i, x in enumerate(a) if x == "--query"]:
        n = q.split("=")[0]
        with open(os.path.join(opt("--out-dir"), n + ".json"), "w") as fh:
            json.dump(doc(n, tabs.get(n, False)), fh)
if sub == "smoke-attach" and spec.get("rc", 0) == 0:       # 드라이런: 스냅샷으로 다시 그린 표
    src = json.load(open(opt("--recall")))
    if any(r.get("open_table") for r in src["runs"]):
        print("r7_endtoend: the saved recall already has an open table", file=sys.stderr)
        sys.exit(1)
    att = full["_attach"] if "_attach" in full else {"symptom": True, "project": True}
    for r in src["runs"]:
        r["open_table"] = TAB if att.get(name, False) else None
    with open(opt("--out"), "w") as fh:
        json.dump(src, fh)
cats = dict.fromkeys(CATS_HERE, 0)
cats.update(spec.get("cats") or {})
if "raw" in spec:
    print(spec["raw"])
elif sub in ("table-gate", "smoke"):
    print(json.dumps({"fail": spec.get("fail", []), "verdict": spec.get("verdict", "pass"), "rollback": cats}))
else:
    print(json.dumps({"ok": True}))
sys.exit(spec.get("rc", 0))
'''.replace("CATS_HERE", repr(CATS))

# 운영 모드(기본, rework #414-1): 운영 응답 표를 그대로 판정, smoke-attach 없음. 가짜 하위 명령 호출 순서.
ORDER = ["smoke-recall", "table-gate", "smoke-dllm:symptom", "smoke:symptom", "smoke-dllm:project", "smoke:project"]
ATTACH = ["smoke-attach:symptom", "smoke-attach:project", "smoke-attach:general1", "smoke-attach:general2"]
ORDER_DRY = ORDER[:1] + ATTACH + ORDER[1:]


def steps_of(calls):
    """STEP 줄 순서 = snapshot + 가짜 호출, 표 확인(table-presence)은 table-gate 바로 앞."""
    out = ["snapshot"]
    for c in calls:
        if c == "table-gate":
            out.append("table-presence")
        out.append(c)
    return out


def _env(tmp_path, spec):
    fake = tmp_path / "fake_r7.py"
    fake.write_text(FAKE)
    env = dict(os.environ, R7_CMD="%s -I %s" % (sys.executable, fake), R7_PY=sys.executable,
               FAKE_LOG=str(tmp_path / "calls.jsonl"), FAKE_SPEC=json.dumps(spec))
    return env


def _store(tmp_path):
    p = tmp_path / "ops-store.sqlite"
    c = sqlite3.connect(p)
    c.execute("create table meta (k text, v text)")
    c.execute("insert into meta values ('key_rules', 'v2.1')")
    c.commit()
    c.close()
    return str(p)


def _argv(tmp_path, store=None, label="10m", work=None, extra=()):
    for n in ("symptom", "project", "general1", "general2"):
        (tmp_path / ("q-%s.txt" % n)).write_text("q")
    q = lambda n: "%s=%s" % (n, tmp_path / ("q-%s.txt" % n))  # noqa: E731
    return ["bash", SCRIPT, "--label", label, "--store", store or _store(tmp_path),
            "--listing", str(tmp_path / "b.jsonl"), str(tmp_path / "m.jsonl"), "--owners", str(tmp_path / "o.json"),
            "--default-owner", "o", "--aliases", str(tmp_path / "al.json"), "--work", work or str(tmp_path / "work"),
            "--state-query", q("symptom"), "--state-query", q("project"),
            "--general-query", q("general1"), "--general-query", q("general2"), *extra]


def _run(tmp_path, spec, **kw):
    p = subprocess.run(_argv(tmp_path, **kw), env=_env(tmp_path, spec), capture_output=True, text=True)
    log = tmp_path / "calls.jsonl"
    calls = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
    return p, calls


# 이름, 가짜 사양, 종료 코드, VERDICT, 범주 기대(일부), 불린 단계 수(앞에서부터), CAUSE 단계
CASES = [
    ("all-pass", {}, 0, "pass", {"runner_failure": 0, "closed_errors": 0}, len(ORDER), None),
    ("table-gate-rollback", {"table-gate": {"rc": 4, "verdict": "rollback", "cats": {"closed_errors": 2}}},
     4, "rollback", {"closed_errors": 2}, len(ORDER), None),
    ("smoke-rollback", {"smoke:project": {"rc": 4, "verdict": "rollback", "cats": {"github_finished": 1}}},
     4, "rollback", {"github_finished": 1}, len(ORDER), None),
    ("rollback-sums", {"table-gate": {"rc": 4, "verdict": "rollback", "cats": {"candidate_precision_low": 1}},
                       "smoke:symptom": {"rc": 4, "verdict": "rollback", "cats": {"table_refuted": 2}},
                       "smoke:project": {"rc": 4, "verdict": "rollback", "cats": {"table_refuted": 1}}},
     4, "rollback", {"candidate_precision_low": 1, "table_refuted": 3}, len(ORDER), None),
    ("table-gate-fail", {"table-gate": {"rc": 1, "verdict": "rollback", "fail": ["empty:candidate:project"],
                                        "cats": {"runner_failure": 1}}},
     1, "runner_failure", {"runner_failure": 1}, ORDER.index("table-gate") + 1, "table-gate"),
    ("recall-fail", {"smoke-recall": {"rc": 1}}, 1, "runner_failure", {"runner_failure": 1}, 1, "smoke-recall"),
    # rework #414-1: 상태 질의 운영 응답에 표가 없으면 실패(다시 그린 표로 헛통과 금지), 일반 질의에 표가 있으면 롤백
    ("no-open-table", {"_tables": {"symptom": True, "project": False}}, 1, "runner_failure", {"runner_failure": 1},
     1, "table-presence"),
    ("general-attached", {"_tables": {"symptom": True, "project": True, "general2": True}}, 4, "rollback",
     {"general_attached": 1}, len(ORDER), None),
    ("dllm-fail", {"smoke-dllm:symptom": {"rc": 1}}, 1, "runner_failure", {"runner_failure": 1},
     ORDER.index("smoke-dllm:symptom") + 1, "smoke-dllm:symptom"),
    ("smoke-fail-after-rollback", {"table-gate": {"rc": 4, "verdict": "rollback", "cats": {"over_max_rows": 1}},
                                   "smoke:symptom": {"rc": 1, "verdict": "rollback", "fail": ["valid-reps"],
                                                     "cats": {"runner_failure": 1}}},
     1, "runner_failure", {"over_max_rows": 1, "runner_failure": 1}, ORDER.index("smoke:symptom") + 1, "smoke:symptom"),
    ("rc0-but-rollback", {"smoke:symptom": {"rc": 0, "verdict": "rollback"}}, 1, "runner_failure",
     {"runner_failure": 1}, len(ORDER), "smoke:symptom"),
    ("rc0-no-json", {"table-gate": {"rc": 0, "raw": "not json"}}, 1, "runner_failure", {"runner_failure": 1},
     len(ORDER), "table-gate"),
    ("rc-odd", {"table-gate": {"rc": 3, "verdict": "rollback"}}, 1, "runner_failure", {"runner_failure": 1},
     ORDER.index("table-gate") + 1, "table-gate"),
]


@pytest.mark.parametrize("name,spec,rc,verdict,cats,n_calls,cause", CASES, ids=[c[0] for c in CASES])
def test_wrapper_order_and_verdict(tmp_path, name, spec, rc, verdict, cats, n_calls, cause):
    p, calls = _run(tmp_path, spec)
    assert p.returncode == rc, p.stderr
    lines = p.stdout.strip().splitlines()
    last = lines[-1].split()
    assert last[0] == "VERDICT" and last[1] == verdict
    got = dict(x.split("=") for x in last[2:])
    assert got["mode"] == "ops"
    for k, v in cats.items():
        assert int(got[k]) == v, (k, got)
    assert [c["key"] for c in calls] == ORDER[:n_calls]
    steps = [x.split()[1] for x in lines if x.startswith("STEP ")]
    want = steps_of(ORDER[:n_calls])
    if cause == "table-presence":
        want = want + ["table-presence"]
    assert steps == want
    causes = [x for x in lines if x.startswith("CAUSE ")]
    if cause:
        assert len(causes) == 1 and causes[0].split()[1] == cause
        f = (spec.get(cause) or {}).get("fail")
        if f:
            assert json.dumps(f) in causes[0]
        if cause == "table-presence":
            assert "no-open-table:project" in causes[0]
    else:
        assert causes == []


def test_wrapper_arguments_passed(tmp_path):
    store = _store(tmp_path)
    p, calls = _run(tmp_path, {}, store=store, extra=("--gh-timeout", "45", "--k", "5"))
    assert p.returncode == 0, p.stderr
    by = {c["key"]: c["argv"] for c in calls}
    rec = by["smoke-recall"]
    assert "--allow-prod" in rec and "--read-only" in rec
    assert [rec[i + 1].split("=")[0] for i, x in enumerate(rec) if x == "--query"] == [
        "symptom", "project", "general1", "general2"]
    t0 = rec[rec.index("--t0") + 1]
    d = tmp_path / "work" / "10m"
    snap = str(d / "store.sqlite")
    for k in ("table-gate", "smoke-dllm:symptom", "smoke:symptom", "smoke-dllm:project", "smoke:project"):
        assert by[k][by[k].index("--t0") + 1] == t0
    tg = by["table-gate"]
    assert [tg[i + 1] for i, x in enumerate(tg) if x == "--recall"] == [
        str(d / "rec" / "symptom.json"), str(d / "rec" / "project.json")]
    for k in ("table-gate", "smoke:symptom", "smoke:project"):
        a = by[k]
        assert a[a.index("--store") + 1] == snap and a[a.index("--gh-timeout") + 1] == "45"
    assert not any(k.startswith("smoke-attach") for k in by)                    # 운영 모드: 다시 그리지 않는다
    sm = by["smoke:project"]
    assert sm[sm.index("--recall") + 1] == str(d / "rec" / "project.json")
    assert by["smoke-dllm:project"][by["smoke-dllm:project"].index("--recall") + 1] == str(d / "rec" / "project.json")
    assert [sm[i + 1] for i, x in enumerate(sm) if x == "--table-recall"] == [
        str(d / "rec" / "general1.json"), str(d / "rec" / "general2.json")]
    assert by["smoke-dllm:project"][by["smoke-dllm:project"].index("--k") + 1] == "5"
    rec_doc = json.loads((d / "store-copy.json").read_text())
    src_sha = hashlib.sha256(open(store, "rb").read()).hexdigest()
    assert rec_doc["src_sha256_before"] == rec_doc["src_sha256_after"] == src_sha
    assert rec_doc["copy_sha256"] == hashlib.sha256(open(snap, "rb").read()).hexdigest()
    assert rec_doc["integrity"] == "ok"
    assert oct(os.stat(d).st_mode & 0o777) == "0o700" and oct(os.stat(snap).st_mode & 0o777) == "0o600"


def test_wrapper_snapshot_missing_store(tmp_path):
    p, calls = _run(tmp_path, {}, store=str(tmp_path / "nope.sqlite"))
    assert p.returncode == 1
    lines = p.stdout.strip().splitlines()
    assert lines[-1].startswith("VERDICT runner_failure") and "CAUSE snapshot rc=1" in p.stdout
    assert calls == []


@pytest.mark.parametrize("case", ["bad-label", "work-in-repo", "round-exists", "no-state-query"])
def test_wrapper_argument_errors(tmp_path, case):
    kw = {}
    if case == "bad-label":
        kw["label"] = "5m"
    elif case == "work-in-repo":
        kw["work"] = os.path.join(REPO, "bench")
    elif case == "round-exists":
        (tmp_path / "work" / "10m").mkdir(parents=True)
    argv = _argv(tmp_path, **kw)
    if case == "no-state-query":
        argv = [x for i, x in enumerate(argv) if x != "--state-query" and argv[i - 1] != "--state-query"]
    p = subprocess.run(argv, env=_env(tmp_path, {}), capture_output=True, text=True)
    assert p.returncode == 2
    assert not (tmp_path / "calls.jsonl").exists()


# --- rework #414-1: 드라이런 모드(--dry-run-attach)는 S3 꺼진 운영에서만, 스냅샷으로 다시 그린 표를 판정 ------------------

DRY = [
    ("dry-pass", {"_tables": {}}, 0, "pass", ORDER_DRY, None),
    ("dry-attach-fail", {"_tables": {}, "smoke-attach:project": {"rc": 1}}, 1, "runner_failure", ORDER_DRY[:3],
     "smoke-attach:project"),
    ("dry-on-s3-response", {"_tables": {"symptom": True, "project": True}}, 1, "runner_failure", ORDER_DRY[:2],
     "smoke-attach:symptom"),
    ("dry-general-attached", {"_tables": {}, "_attach": {"symptom": True, "project": True, "general1": True}}, 4,
     "rollback", ORDER_DRY, None),
]


@pytest.mark.parametrize("name,spec,rc,verdict,calls_want,cause", DRY, ids=[c[0] for c in DRY])
def test_wrapper_dry_run_attach(tmp_path, name, spec, rc, verdict, calls_want, cause):
    p, calls = _run(tmp_path, spec, extra=("--dry-run-attach",))
    assert p.returncode == rc, p.stderr
    lines = p.stdout.strip().splitlines()
    last = lines[-1].split()
    assert last[:2] == ["VERDICT", verdict] and "mode=dry-run-attach" in last
    assert [c["key"] for c in calls] == calls_want
    by = {c["key"]: c["argv"] for c in calls}
    d = tmp_path / "work" / "10m"
    if "table-gate" in by:
        tg = by["table-gate"]
        assert [tg[i + 1] for i, x in enumerate(tg) if x == "--recall"] == [
            str(d / "rec" / "symptom-attached.json"), str(d / "rec" / "project-attached.json")]
        assert by["smoke-attach:general1"][by["smoke-attach:general1"].index("--store") + 1] == str(d / "store.sqlite")
    causes = [x for x in lines if x.startswith("CAUSE ")]
    assert [c.split()[1] for c in causes] == ([cause] if cause else [])
