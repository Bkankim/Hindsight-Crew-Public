"""사이드카 GitHub 종결 수집·키 규칙 v2 선택 (issue #395, R7-1). 네트워크 0: `gh` 는 가짜 호출기, LLM 은 가짜 caller.

GitHub 근거 행렬: 가짜 `gh api` 응답(완료·머지·미머지 PR·not_planned·duplicate·사유 없음·알 수 없는 사유·재오픈·404·
인증 실패·한도·네트워크·깨진 JSON·번호 없음)마다 저장 행과 근거 판정을 본다. 실패는 종결로 저장되지 않는다.
v1 기본 불변: `--github` 가 없으면 gh 호출 0, 기본 프롬프트 sha256 그대로, 기본 poll 은 key_rules 를 쓰지 않는다.
"""
from __future__ import annotations

import importlib.util
import json
import os
import urllib.parse
from pathlib import Path

import pytest

from gateway import item_roles as IR

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("item_roles_sidecar_gh", REPO / "scripts" / "item_roles_sidecar.py")
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)

HC = "hindsight-crew"
ALIASES = {"hc": HC, HC: HC}
P = ["project:hindsight-crew"]
T0 = "2026-11-03T00:00:00Z"


def mem(mid, text, tags=(), state="valid", at="2026-10-01T00:00:00+00:00", doc="d", ft="world"):
    return {"id": mid, "text": text, "type": ft, "state": state, "mentioned_at": at, "document_id": doc,
            "tags": list(tags)}


def rule(sentence):
    return "done" if "끝" in sentence else "open" if "남" in sentence else "ref"


class Caller:
    def __init__(self, cost=0.01):
        self.prompts = []
        self.cost = cost

    def __call__(self, prompt):
        self.prompts.append(prompt)
        its = self.items_in(prompt)
        out = [{"i": it["i"], "role": rule(it["sentence"])} for it in its]
        return {"is_error": False, "result": json.dumps(out), "total_cost_usd": self.cost}

    @staticmethod
    def items_in(prompt):
        assert prompt.startswith(S.ROLE_PROMPT)
        return [json.loads(x) for x in prompt[len(S.ROLE_PROMPT):].splitlines() if x.strip()]

    def items(self):
        return [it for p in self.prompts for it in self.items_in(p)]


def issue(n, state="closed", reason=None, closed="2026-10-02T00:00:00Z"):
    return {"number": n, "state": state, "state_reason": reason, "created_at": "2026-10-01T00:00:00Z",
            "closed_at": closed if state == "closed" else None, "title": "t%d" % n}


def pr(n, state="closed"):
    d = issue(n, state)
    d["pull_request"] = {"url": "x"}
    return d


def pull(n, merged=None):
    return {"number": n, "state": "closed", "merged_at": merged, "closed_at": "2026-10-02T00:00:00Z"}


class FakeGh:
    """`gh api --method GET --paginate --slurp <path>` 가짜. repos = {"owner/repo": {"issues": [...], "pulls": [...]}
    또는 (rc, stdout, stderr) 고정 응답}. 없는 리포는 404. 쪽은 2개씩 나눠 slurp 모양(쪽 배열의 배열)으로 낸다."""

    def __init__(self, repos):
        self.repos = repos
        self.calls = []

    def __call__(self, args):
        args = list(args)
        self.calls.append(args)
        assert args[:5] == ["api", "--method", "GET", "--paginate", "--slurp"] and len(args) == 6
        u = urllib.parse.urlparse(args[5])
        parts = u.path.split("/")
        assert parts[0] == "repos" and parts[3] in ("issues", "pulls") and len(parts) == 4
        q = urllib.parse.parse_qs(u.query)
        assert q["per_page"] == ["100"]
        assert q["state"] == (["all"] if parts[3] == "issues" else ["closed"])
        spec = self.repos.get("%s/%s" % (parts[1], parts[2]))
        if spec is None:
            return 1, "", "gh: Not Found (HTTP 404)"
        if isinstance(spec, tuple):
            return spec
        rows = spec.get(parts[3], [])
        pages = [rows[i:i + 2] for i in range(0, len(rows), 2)] or [[]]
        return 0, json.dumps(pages), ""


def gcfg(gh, owners=None, default_owner="o", min_interval=3600):
    return S.GithubConfig(gh, owners=owners, default_owner=default_owner, min_interval=min_interval)


def run(db, mems, gh=None, rules="v1", now=T0, caller=None, mode="poll"):
    caller = caller or Caller()
    return S.run_sync(mode, str(db), {m["id"]: m for m in mems}, ALIASES, lambda: caller, None,
                      now=lambda: now, rules=rules, github=gh)


def lines(capsys):
    return [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.strip()]


BASE = [mem("a", "[메모] HC#5 참고.")]


# --- GitHub 근거 행렬 -----------------------------------------------------------------------------------------

ERR401 = (1, "", "gh: Bad credentials (HTTP 401)")
ERR403 = (1, "", "gh: API rate limit exceeded (HTTP 403)")
ERR429 = (1, "", "gh: Too Many Requests (HTTP 429)")
NETERR = (1, "", "error connecting to api.github.com")
BADJSON = (0, "<html>", "")


@pytest.mark.parametrize("case, spec, verdict, reason, repo_status, error_code, row", [
    ("completed", {"issues": [issue(5, reason="completed")]}, IR.GH_CLOSED, "completed", "ok", None,
     {"kind": "issue", "state": "closed", "state_reason": "completed", "merged_at": None}),
    ("merged-pr", {"issues": [pr(5)], "pulls": [pull(5, "2026-10-02T00:00:00Z")]}, IR.GH_CLOSED, "merged", "ok", None,
     {"kind": "pr", "state": "closed", "merged_at": "2026-10-02T00:00:00Z"}),
    ("closed-unmerged-pr", {"issues": [pr(5)], "pulls": [pull(5)]}, IR.GH_NOT_PLANNED, "pr-unmerged", "ok", None,
     {"kind": "pr", "state": "closed", "merged_at": None}),
    ("not-planned", {"issues": [issue(5, reason="not_planned")]}, IR.GH_NOT_PLANNED, "not_planned", "ok", None,
     {"kind": "issue", "state_reason": "not_planned"}),
    ("duplicate", {"issues": [issue(5, reason="duplicate")]}, IR.GH_NOT_PLANNED, "duplicate", "ok", None,
     {"kind": "issue", "state_reason": "duplicate"}),
    ("closed-no-reason", {"issues": [issue(5)]}, IR.GH_CLOSED_NO_REASON, "no-reason", "ok", None,
     {"kind": "issue", "state": "closed", "state_reason": None}),
    ("unknown-reason", {"issues": [issue(5, reason="weird")]}, IR.GH_UNKNOWN, "unknown-reason", "ok", None,
     {"state_reason": "weird"}),
    ("reopened", {"issues": [issue(5, state="open", reason="reopened")]}, IR.GH_OPEN, "reopened", "ok", None,
     {"state": "open", "closed_at": None}),
    ("repo-404", None, IR.GH_UNKNOWN, "repo-missing", "missing", "http-404", None),
    ("auth-401", ERR401, IR.GH_UNKNOWN, "repo-error", "error", "http-401", None),
    ("rate-403", ERR403, IR.GH_UNKNOWN, "repo-error", "error", "http-403", None),
    ("rate-429", ERR429, IR.GH_UNKNOWN, "repo-error", "error", "http-429", None),
    ("network", NETERR, IR.GH_UNKNOWN, "repo-error", "error", "exit-1", None),
    ("bad-json", BADJSON, IR.GH_UNKNOWN, "repo-error", "error", "bad-json", None),
    ("no-number", {"issues": [issue(6, reason="completed")]}, IR.GH_UNKNOWN, "no-number", "ok", None, None),
])
def test_github_evidence_matrix(tmp_path, capsys, case, spec, verdict, reason, repo_status, error_code, row):
    gh = FakeGh({} if spec is None else {"o/hindsight-crew": spec})
    db = tmp_path / "s.sqlite"
    assert run(db, BASE, gcfg(gh)) == 0                                  # 조회 실패도 exit 0
    meta, mems, roles, tables = IR.load_store(str(db), with_github=True)
    assert roles == {"a": {"hindsight-crew#5": "ref"}}
    repo = tables["repos"][HC]
    assert (repo["status"], repo["error_code"], repo["owner"], repo["fetched_at"]) == (repo_status, error_code, "o", T0)
    ev = IR.evidence_for_key("hindsight-crew#5", tables)
    assert (ev.verdict, ev.reason) == (verdict, reason)
    got = tables["items"].get((HC, 5))
    if row is None:
        assert got is None
    else:
        assert {k: got[k] for k in row} == row and got["fetched_at"] == T0
    if repo_status != "ok":
        assert not tables["items"] and not IR.is_closed_verdict(ev.verdict)
    out = lines(capsys)
    (g,) = [x for x in out if x.get("phase") == "github"]
    assert g["repos"] == 1 and g[repo_status] == 1
    assert g["gh_calls"] == (2 if repo_status == "ok" else 1)
    assert "hindsight-crew" not in json.dumps(g) and "Bad credentials" not in json.dumps(out)


def test_failure_after_success_keeps_rows_but_is_not_closed(tmp_path):
    db = tmp_path / "s.sqlite"
    run(db, BASE, gcfg(FakeGh({"o/hindsight-crew": {"issues": [issue(5, reason="completed")]}})))
    assert run(db, BASE, gcfg(FakeGh({"o/hindsight-crew": ERR401}), min_interval=0),
               now="2026-11-03T05:00:00Z") == 0
    tables = IR.load_github(str(db))
    assert tables["repos"][HC]["status"] == "error" and tables["items"][(HC, 5)]["state"] == "closed"
    assert IR.evidence_for_key("hindsight-crew#5", tables).verdict == IR.GH_UNKNOWN


def test_pages_are_all_read(tmp_path):
    many = [issue(n, reason="completed") for n in range(1, 8)]
    db = tmp_path / "s.sqlite"
    run(db, [mem("a", "[메모] HC#7 참고.")], gcfg(FakeGh({"o/hindsight-crew": {"issues": many}})))
    assert sorted(n for _, n in IR.load_github(str(db))["items"]) == list(range(1, 8))


# --- 소유자·간격·호출 0 ---------------------------------------------------------------------------------------

def test_owner_map_wins_and_missing_owner_is_an_error_without_a_call(tmp_path, capsys):
    gh = FakeGh({"me/hindsight-crew": {"issues": [issue(5, reason="completed")]}})
    db = tmp_path / "s.sqlite"
    mems = BASE + [mem("b", "[메모] other#3 참고.", tags=["project:other"])]
    run(db, mems, gcfg(gh, owners={HC: "me"}, default_owner=None))
    tables = IR.load_github(str(db))
    assert tables["repos"][HC]["status"] == "ok" and tables["repos"]["other"] == {
        "owner": None, "status": "error", "fetched_at": T0, "error_code": "no-owner"}
    assert all("/other/" not in c[5] for c in gh.calls) and len(gh.calls) == 2


def test_min_interval_skips_recent_repos(tmp_path, capsys):
    db = tmp_path / "s.sqlite"
    spec = {"o/hindsight-crew": {"issues": [issue(5, reason="completed")]}}
    run(db, BASE, gcfg(FakeGh(spec)))
    gh2 = FakeGh(spec)
    run(db, BASE, gcfg(gh2), now="2026-11-03T00:30:00Z")
    assert gh2.calls == []
    gh3 = FakeGh(spec)
    run(db, BASE, gcfg(gh3), now="2026-11-03T01:00:01Z")
    assert len(gh3.calls) == 2
    assert IR.load_github(str(db))["repos"][HC]["fetched_at"] == "2026-11-03T01:00:01Z"


def test_error_repos_are_retried_inside_the_interval(tmp_path):
    db = tmp_path / "s.sqlite"
    run(db, BASE, gcfg(FakeGh({"o/hindsight-crew": NETERR})))
    gh = FakeGh({"o/hindsight-crew": {"issues": [issue(5, reason="completed")]}})
    run(db, BASE, gcfg(gh), now="2026-11-03T00:10:00Z")
    assert len(gh.calls) == 2 and IR.load_github(str(db))["repos"][HC]["status"] == "ok"


def test_poll_without_github_carries_the_tables_over(tmp_path):
    db = tmp_path / "s.sqlite"
    run(db, BASE, gcfg(FakeGh({"o/hindsight-crew": {"issues": [issue(5, reason="completed")]}})))
    before = IR.load_github(str(db))
    assert run(db, BASE + [mem("b", "[메모] HC#6 남음.")]) == 0             # 바뀐 것이 있어 다시 공개된다
    assert IR.load_github(str(db)) == before
    assert "b" in IR.load_store(str(db))[1]


def test_default_poll_makes_no_github_tables(tmp_path):
    db = tmp_path / "s.sqlite"
    run(db, BASE)
    assert IR.load_github(str(db)) is None and "key_rules" not in IR.load_store(str(db))[0]


def test_cli_without_github_never_builds_a_gh_runner(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("gh runner built without --github")
    monkeypatch.setattr(S, "gh_runner", boom)
    monkeypatch.setattr(S, "claude_caller", lambda **k: Caller())
    argv = offline_args(tmp_path)
    assert S.main(["backfill"] + argv) == 0
    assert S.main(["poll"] + argv) == 0


def test_cli_github_flag_uses_the_runner(tmp_path, monkeypatch, capsys):
    gh = FakeGh({"o/hindsight-crew": {"issues": [issue(5, reason="completed")]}})
    monkeypatch.setattr(S, "gh_runner", lambda **k: gh)
    monkeypatch.setattr(S, "claude_caller", lambda **k: Caller())
    owners = tmp_path / "owners.json"
    owners.write_text(json.dumps({"HC": "o"}))
    assert S.main(["backfill"] + offline_args(tmp_path) + ["--github", "--github-owners", str(owners)]) == 0
    assert len(gh.calls) == 2
    assert IR.load_github(str(tmp_path / "s.sqlite"))["repos"][HC]["status"] == "ok"


def test_cli_owner_options_need_github(tmp_path):
    assert S.main(["backfill"] + offline_args(tmp_path) + ["--github-default-owner", "o"]) == 1


def offline_args(tmp_path):
    bodies, meta, af = tmp_path / "b.jsonl", tmp_path / "m.jsonl", tmp_path / "aliases.json"
    if not bodies.exists():
        bodies.write_text(json.dumps({"id": "a", "text": "[메모] HC#5 참고."}, ensure_ascii=False) + "\n")
        meta.write_text(json.dumps({"id": "a", "tags": [], "state": "valid", "document_id": "d",
                                    "mentioned_at": "2026-10-01T00:00:00+00:00", "fact_type": "world"}) + "\n")
        af.write_text(json.dumps(ALIASES))
    return ["--db", str(tmp_path / "s.sqlite"), "--offline-bodies", str(bodies), "--offline-meta", str(meta),
            "--aliases-file", str(af)]


def test_gh_runner_reports_codes_only(monkeypatch):
    import subprocess

    class R:
        returncode, stdout, stderr = 1, "", "gh: Bad credentials (HTTP 401)"
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, kw=kw)
        return R()
    monkeypatch.setattr(subprocess, "run", fake_run)
    rc, out, err = S.gh_runner()(["api", "--method", "GET", "x"])
    assert seen["cmd"][:2] == ["gh", "api"] and "env" not in seen["kw"] and seen["kw"]["capture_output"]
    assert (rc, S.gh_error_code(rc, err)) == (1, "http-401")
    assert S.gh_error_code(1, "boom") == "exit-1"


def test_sidecar_never_reads_a_token_for_gh():
    src = (REPO / "scripts" / "item_roles_sidecar.py").read_text(encoding="utf-8")
    body = src[src.index("def gh_runner"):src.index("def collect_github")]
    for bad in ("TOKEN", "token", "auth", "environ"):
        assert bad not in body


# --- 키 규칙 v2 선택과 meta ----------------------------------------------------------------------------------

def test_default_prompt_is_unchanged():
    assert S.PROMPT_SHA256 == "3baf354a9232636d9ebd2088ffd8c0ac81bc457d7aac23d424b8a6c2fd070b74"


def test_poll_v2_writes_key_rules_and_skips_fixed_mentions(tmp_path, capsys):
    db = tmp_path / "s.sqlite"
    mems = [mem("a", "[메모] upstream #5 남음. #6 남음.", tags=P), mem("b", "[완료] PR #9 머지, #1~#3 끝.", tags=P),
            mem("c", "[메모] hc#10 기준.")]
    caller = Caller()
    assert run(db, mems, rules="v2", caller=caller) == 0
    meta, _, roles = IR.load_store(str(db))
    assert meta["key_rules"] == "v2"
    assert roles["a"] == {"hindsight-crew#5": "ref", "hindsight-crew#6": "open"}
    assert roles["b"] == {"hindsight-crew#%d" % n: "done" for n in (1, 2, 3)}
    assert sorted((it["key"], it["sentence"]) for it in caller.items() if "#5" in it["key"]) == []
    (plan,) = [x for x in lines(capsys) if x.get("phase") == "plan"]
    assert plan["key_rules"] == "v2" and plan["fixed_mentions"] == 1 and plan["suspect_keys"] == 0
    assert len(plan["observed_max_sha256"]) == 64


def test_poll_v2_counts_suspect_keys(tmp_path, capsys):
    db = tmp_path / "s.sqlite"
    run(db, [mem("a", "[메모] hc#10 기준."), mem("b", "[인계] #500 남음.", tags=P)], rules="v2")
    (plan,) = [x for x in lines(capsys) if x.get("phase") == "plan"]
    assert plan["suspect_keys"] == 1
    assert "hindsight-crew#500" in IR.load_store(str(db))[2]["b"]           # 의심 키는 지우지 않는다


def test_default_plan_line_has_no_v2_fields(tmp_path, capsys):
    run(tmp_path / "s.sqlite", BASE)
    (plan,) = [x for x in lines(capsys) if x.get("phase") == "plan"]
    assert not {"key_rules", "fixed_mentions", "suspect_keys", "observed_max_sha256"} & set(plan)


@pytest.mark.parametrize("stored, wanted", [("v2", "v1"), ("v1", "v2")])
def test_poll_refuses_another_key_rules_by_name(tmp_path, stored, wanted):
    db = tmp_path / "s.sqlite"
    run(db, BASE, rules=stored)
    meta = IR.load_store(str(db))[0]
    assert meta["prompt_sha256"] == S.PROMPT_SHA256                       # 프롬프트 sha 는 같다
    before = db.read_bytes()
    with pytest.raises(S.Fail, match="key_rules"):
        run(db, BASE, rules=wanted)
    assert db.read_bytes() == before


def test_status_uses_the_store_key_rules(tmp_path, capsys):
    db = tmp_path / "s.sqlite"
    mems = [mem("b", "[완료] #1~#3 끝.", tags=P)]
    run(db, mems, rules="v2")
    capsys.readouterr()
    assert S.status(str(db), {m["id"]: m for m in mems}, ALIASES) == 0
    (out,) = lines(capsys)
    assert out["unjudged"] == 0 and out["meta_refresh_pending"] == 0
