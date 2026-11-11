"""#395 final-review 재작업(rework #395-1) 회귀 테스트. LLM·gh 는 가짜만 쓴다.

고정하는 것:
  * status: meta key_rules 가 v1/v2 밖이면 트레이스백이 아니라 Fail(exit 1)로 거부한다.
  * v2 문맥 낱말: '리포트'(report) 안의 '다른 리포'·'타 리포'·'외부 리포'는 업스트림 문맥이 아니다.
  * --github-owners 키는 저장소 키와 같은 slug 규칙으로 정규화한다.
  * rekey --save-listing: 이미 있는 경로(오프라인 입력 등)나 같은 두 경로는 거부하고 파일을 고치지 않는다.
  * GitHub 수집: 소유자가 바뀌면 min-interval 안이어도 다시 받는다.
  * check_meta: key_rules 만 다르면 backfill(삭제)이 아니라 rekey 를 안내한다.
  * rekey: v2 에서 문장 언급이 사라지고 item 태그로만 남은 키는 원본 역할을 보존하지 않고 ref(새 v2 backfill 과 같음).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

import pytest

from gateway import item_keys as IK
from gateway import item_roles as IR

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("item_roles_sidecar_rw395", REPO / "scripts" / "item_roles_sidecar.py")
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)

HC = "hindsight-crew"
ALIASES = {"hc": HC, HC: HC}
P = ["project:hindsight-crew"]
T0 = "2026-11-03T00:00:00Z"
T_OLD = "2026-11-01T00:00:00Z"


def mem(mid, text, tags=P, state="valid", at="2026-10-01T00:00:00+00:00", doc="d", ft="world"):
    return {"id": mid, "text": text, "type": ft, "state": state, "mentioned_at": at, "document_id": doc,
            "tags": list(tags)}


def k(n, repo=HC):
    return "%s#%d" % (repo, n)


class Caller:
    def __init__(self):
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        items = [json.loads(x) for x in prompt[len(S.ROLE_PROMPT):].splitlines() if x.strip()]
        out = [{"i": it["i"], "role": "done" if "끝" in it["sentence"] else "open" if "남" in it["sentence"]
                else "ref"} for it in items]
        return {"is_error": False, "result": json.dumps(out), "total_cost_usd": 0.01}


class FakeGh:
    def __init__(self, repos):
        self.repos = repos
        self.calls = []

    def __call__(self, args):
        args = list(args)
        self.calls.append(args)
        parts = args[5].split("?")[0].split("/")
        spec = self.repos.get("%s/%s" % (parts[1], parts[2]))
        if spec is None:
            return 1, "", "gh: Not Found (HTTP 404)"
        return 0, json.dumps([spec.get(parts[3], [])]), ""


def issue(n, reason="completed"):
    return {"number": n, "state": "closed", "state_reason": reason, "created_at": "2026-10-01T00:00:00Z",
            "closed_at": "2026-10-02T00:00:00Z"}


def run(db, mems, gh=None, rules="v1", now=T0, mode="poll"):
    return S.run_sync(mode, str(db), {m["id"]: m for m in mems}, ALIASES, lambda: Caller(), None,
                      now=lambda: now, rules=rules, github=gh)


def source(tmp_path, entries):
    db = tmp_path / "src.sqlite"
    meta = dict(S.meta_values(ALIASES), updated_at=T_OLD, bank="bank-x")
    mems = {mid: {"document_id": m["document_id"], "mentioned_at": m["mentioned_at"], "state": m["state"],
                  "text_sha256": IR.sha256_text(m["text"]), "judged_at": T_OLD} for mid, (m, _) in entries.items()}
    IR.publish(str(db), meta, mems, {mid: r for mid, (_, r) in entries.items()}, mode=0o600)
    return db


# --- Bugs #1: status 의 모르는 key_rules -------------------------------------------------------------------

def test_status_refuses_an_unknown_key_rules_with_fail(tmp_path):
    db = tmp_path / "s.sqlite"
    meta = dict(S.meta_values(ALIASES), updated_at=T0, key_rules="v3")
    IR.publish(str(db), meta, {}, {}, mode=0o600)
    listing = {"a": mem("a", "[메모] HC#5 참고.")}
    with pytest.raises(Exception) as e:
        S.status(str(db), listing, ALIASES)
    assert isinstance(e.value, S.Fail) and "key_rules" in str(e.value)


# --- Bugs #2: '리포트' 는 리포 문맥이 아니다 ----------------------------------------------------------------

@pytest.mark.parametrize("text", ["[완료] 기타 리포트 정리 #412 끝.", "[완료] 다른 리포트에 #412 반영 끝.",
                                  "[완료] 외부 리포트 #412 끝."])
def test_report_word_is_not_an_upstream_context(text):
    hits = IK.key_hits_v2(text, P, ALIASES)
    assert [(h.key, h.ref_fixed) for h in hits] == [((HC, 412), False)]


@pytest.mark.parametrize("text", ["[메모] 다른 리포 #412 참고.", "[메모] 타 리포의 #412 참고.", "[메모] 외부 리포에서 #412."])
def test_repo_context_words_still_fix_the_reference(text):
    hits = IK.key_hits_v2(text, P, ALIASES)
    assert [(h.key, h.ref_fixed) for h in hits] == [((HC, 412), True)]


# --- Bugs #3: --github-owners 키 정규화 ---------------------------------------------------------------------

def test_github_owner_keys_use_the_store_slug_rule(monkeypatch):
    monkeypatch.setattr(S, "gh_runner", lambda **kw: FakeGh({}))
    args = argparse.Namespace(github=True, github_owners=json.dumps({"Hindsight_Crew": "o"}),
                              github_default_owner=None, github_min_interval=3600)
    cfg = S._github(args, ALIASES)
    assert cfg.owner_of(HC) == "o"


# --- Bugs #4: --save-listing 은 새 경로만 --------------------------------------------------------------------

@pytest.mark.parametrize("which", ["offline-inputs", "same-path"])
def test_save_listing_refuses_existing_or_equal_paths(tmp_path, monkeypatch, which):
    rng = mem("range", "[완료] #10~#12 끝.")
    late = mem("late", "[완료] #20 끝.", at="2026-10-20T00:00:00+00:00")
    db = source(tmp_path, {"range": (rng, {k(10): "open", k(12): "open"})})
    bodies, meta = tmp_path / "b.jsonl", tmp_path / "m.jsonl"
    S.save_listing({"range": rng, "late": late}, str(bodies), str(meta))
    before = (bodies.read_bytes(), meta.read_bytes())
    af = tmp_path / "aliases.json"
    af.write_text(json.dumps(ALIASES))
    monkeypatch.setattr(S, "claude_caller", lambda **kw: Caller())
    saved = [str(bodies), str(meta)] if which == "offline-inputs" else [str(tmp_path / "x.jsonl")] * 2
    rc = S.main(["rekey", "--db", str(db), "--out", str(tmp_path / "new.sqlite"), "--rules", "v2", "--bank", "bank-x",
                 "--offline-bodies", str(bodies), "--offline-meta", str(meta), "--mentioned-until",
                 "2026-10-10", "--aliases-file", str(af), "--save-listing", *saved])
    assert rc == 1
    assert (bodies.read_bytes(), meta.read_bytes()) == before
    assert not (tmp_path / "new.sqlite").exists() and not (tmp_path / "x.jsonl").exists()


# --- Bugs #5: 소유자가 바뀌면 다시 받는다 --------------------------------------------------------------------

def test_owner_change_refetches_inside_the_interval(tmp_path):
    db = tmp_path / "s.sqlite"
    base = [mem("a", "[메모] HC#5 참고.", tags=())]
    run(db, base, S.GithubConfig(FakeGh({"o/hindsight-crew": {"issues": [issue(5)]}}), owners=None,
                                 default_owner="o", min_interval=3600))
    gh2 = FakeGh({"me/hindsight-crew": {"issues": [issue(5, reason="not_planned")]}})
    run(db, base, S.GithubConfig(gh2, owners=None, default_owner="me", min_interval=3600),
        now="2026-11-03T00:10:00Z")
    tables = IR.load_github(str(db))
    assert len(gh2.calls) == 2 and tables["repos"][HC]["owner"] == "me"
    assert tables["items"][(HC, 5)]["state_reason"] == "not_planned"


# --- Bugs #6: key_rules 만 다르면 rekey 를 안내 ---------------------------------------------------------------

def test_key_rules_mismatch_message_points_to_rekey():
    stored = S.meta_values(ALIASES)
    with pytest.raises(S.Fail) as e:
        S.check_meta(stored, dict(stored, key_rules="v2"))
    msg = str(e.value)
    assert "key_rules" in msg and "rekey --rules v2" in msg and "delete it" not in msg


def test_other_mismatch_still_points_to_backfill():
    stored = S.meta_values(ALIASES)
    with pytest.raises(S.Fail, match="delete it and run backfill"):
        S.check_meta(stored, dict(stored, model="other-model"))


# --- Bugs #7: 문장 언급을 잃고 item 태그로만 남은 키는 ref --------------------------------------------------

def test_rekey_tag_only_key_after_pr_notation_is_ref_like_a_fresh_v2_backfill(tmp_path):
    pr_only = mem("pronly", "[완료] PR #5 머지 완료", tags=P + ["item:hc-5"])
    db = source(tmp_path, {"pronly": (pr_only, {k(5): "done"})})
    out = tmp_path / "out.sqlite"
    caller = Caller()
    rc = S.rekey(str(db), str(out), {"pronly": pr_only}, ALIASES, lambda: caller, None, rules="v2", now=lambda: T0)
    fresh = tmp_path / "fresh.sqlite"
    run(fresh, [pr_only], rules="v2", mode="backfill")
    assert rc == 0 and caller.prompts == []
    assert IR.load_store(str(out))[2] == IR.load_store(str(fresh))[2] == {"pronly": {k(5): "ref"}}
