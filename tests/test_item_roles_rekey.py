"""사이드카 `rekey` (issue #395, R7-1): 원본 저장소에 키 규칙 v2 를 적용해 새 파일에 공개한다. LLM 은 가짜 caller.

고정하는 것: 원본 불변(바이트 같음), 새 파일 meta key_rules=v2, 키 집합 같은 기억 역할 보존, 새 키 언급만 판정,
사라진 키 제외, 원본에 없는 기억 제외, 참조 고정 우선(전부 고정 -> ref, 섞이면 보통 언급만 재판정), 원본 행 state 등 유지,
`--save-listing` 파일이 `--offline-*` 로 다시 읽힘, 지출 상한 멈춤.
참조 고정 우선은 동작이 바뀌는 경우라 `HC_R7_RED=1` 이면 v1 규칙 rekey 로 돌아 단언이 실패한다(red 기록용).
"""
from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest

from gateway import item_roles as IR

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("item_roles_sidecar_rk", REPO / "scripts" / "item_roles_sidecar.py")
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)

HC = "hindsight-crew"
ALIASES = {"hc": HC, HC: HC}
P = ["project:hindsight-crew"]
CHANGE = "v1" if os.environ.get("HC_R7_RED") == "1" else "v2"
T_OLD = "2026-11-01T00:00:00Z"
T_NOW = "2026-11-03T00:00:00Z"


def mem(mid, text, tags=P, state="valid", at="2026-10-01T00:00:00+00:00", doc="d", ft="world"):
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
        out = [{"i": it["i"], "role": rule(it["sentence"])} for it in self.items_in(prompt)]
        return {"is_error": False, "result": json.dumps(out), "total_cost_usd": self.cost}

    @staticmethod
    def items_in(prompt):
        return [json.loads(x) for x in prompt[len(S.ROLE_PROMPT):].splitlines() if x.strip()]

    def items(self):
        return [(it["key"], it["sentence"]) for p in self.prompts for it in self.items_in(p)]


def source(tmp_path, entries, meta_extra=None, mode=0o600):
    """원본 저장소: entries = {mid: (mem dict, {key: role})}. 역할은 일부러 규칙과 다르게 둘 수 있다(보존 확인용)."""
    db = tmp_path / "src.sqlite"
    meta = dict(S.meta_values(ALIASES), updated_at=T_OLD, bank="bank-x", **(meta_extra or {}))
    mems = {mid: {"document_id": m["document_id"], "mentioned_at": m["mentioned_at"], "state": m["state"],
                  "text_sha256": IR.sha256_text(m["text"]), "judged_at": T_OLD} for mid, (m, _) in entries.items()}
    IR.publish(str(db), meta, mems, {mid: r for mid, (_, r) in entries.items()}, mode=mode)
    return db


def rekey(tmp_path, db, listing, rules="v2", caller=None, max_usd=None, **kw):
    caller = caller or Caller()
    out = tmp_path / "out.sqlite"
    rc = S.rekey(str(db), str(out), {m["id"]: m for m in listing}, ALIASES, lambda: caller, max_usd, rules=rules,
                 now=lambda: T_NOW, **kw)
    return rc, out, caller


def k(n, repo=HC):
    return "%s#%d" % (repo, n)


SAME = mem("same", "[메모] HC#1 끝냈다.", tags=[])
RANGE = mem("range", "[완료] #10~#12 끝.")
PRKEY = mem("prkey", "[완료] PR #12 머지, #7 해결.")
FIXED = mem("fixed", "[메모] upstream #5 남음.")
MIXED = mem("mixed", "[메모] upstream #5 근거. #5 남음.")


def test_source_is_untouched_and_out_has_key_rules_v2(tmp_path):
    db = source(tmp_path, {"same": (SAME, {k(1): "open"})})
    before = db.read_bytes()
    rc, out, _ = rekey(tmp_path, db, [SAME])
    assert rc == 0 and db.read_bytes() == before
    meta, _, _ = IR.load_store(str(out))
    src_meta = IR.load_store(str(db))[0]
    assert meta["key_rules"] == "v2" and meta["bank"] == "bank-x" and meta["updated_at"] == T_NOW
    assert {x: meta[x] for x in ("model", "prompt_sha256", "aliases_sha256", "judge_mode")} == \
        {x: src_meta[x] for x in ("model", "prompt_sha256", "aliases_sha256", "judge_mode")}
    assert stat.S_IMODE(out.stat().st_mode) == 0o600                     # 원본 권한을 넓히지 않는다


def test_same_key_set_keeps_the_source_role_without_a_call(tmp_path):
    db = source(tmp_path, {"same": (SAME, {k(1): "open"})})              # 규칙으로는 done 이지만 원본 역할을 지킨다
    rc, out, caller = rekey(tmp_path, db, [SAME])
    _, mems, roles = IR.load_store(str(out))
    assert roles == {"same": {k(1): "open"}} and caller.prompts == []
    assert mems["same"]["judged_at"] == T_OLD


def test_only_new_keys_are_judged(tmp_path):
    db = source(tmp_path, {"range": (RANGE, {k(10): "open", k(12): "open"})})
    rc, out, caller = rekey(tmp_path, db, [RANGE])
    assert caller.items() == [(k(11), "[완료] #10~#12 끝.")]
    assert IR.load_store(str(out))[2] == {"range": {k(10): "open", k(11): "done", k(12): "open"}}
    assert IR.load_store(str(out))[1]["range"]["judged_at"] == T_NOW


def test_vanished_keys_are_dropped(tmp_path):
    db = source(tmp_path, {"prkey": (PRKEY, {k(7): "done", k(12): "done"})})
    rc, out, caller = rekey(tmp_path, db, [PRKEY])
    assert IR.load_store(str(out))[2] == {"prkey": {k(7): "done"}} and caller.prompts == []


def test_fixed_reference_wins_over_the_source_role(tmp_path):
    db = source(tmp_path, {"fixed": (FIXED, {k(5): "open"})})
    rc, out, caller = rekey(tmp_path, db, [FIXED], rules=CHANGE)
    assert IR.load_store(str(out))[2] == {"fixed": {k(5): "ref"}}
    assert caller.prompts == []


def test_mixed_fixed_and_normal_mentions_rejudge_only_the_normal_one(tmp_path):
    db = source(tmp_path, {"mixed": (MIXED, {k(5): "ref"})})
    rc, out, caller = rekey(tmp_path, db, [MIXED], rules=CHANGE)
    assert caller.items() == [(k(5), "#5 남음.")]
    assert IR.load_store(str(out))[2] == {"mixed": {k(5): "open"}}


def test_only_source_memories_are_handled_and_their_rows_are_kept(tmp_path):
    db = source(tmp_path, {"same": (SAME, {k(1): "open"}), "gone": (mem("gone", "[메모] #3"), {k(3): "ref"})})
    listed = dict(SAME, state="invalidated", mentioned_at="2026-10-09T00:00:00+00:00", document_id="other")
    extra = mem("extra", "[메모] #9 남음.")
    rc, out, caller = rekey(tmp_path, db, [listed, extra])
    _, mems, roles = IR.load_store(str(out))
    assert set(mems) == {"same"} and caller.prompts == []                 # 원본에 없는 extra 는 판정·저장 안 함
    src = IR.load_store(str(db))[1]["same"]
    assert {x: mems["same"][x] for x in ("state", "mentioned_at", "document_id")} == \
        {x: src[x] for x in ("state", "mentioned_at", "document_id")}


def test_changed_text_rejudges_its_mentions(tmp_path):
    db = source(tmp_path, {"same": (SAME, {k(1): "open"})})
    changed = dict(SAME, text="[메모] HC#1 은 남음.")
    rc, out, caller = rekey(tmp_path, db, [changed])
    assert caller.items() == [(k(1), "[메모] HC#1 은 남음.")]
    _, mems, roles = IR.load_store(str(out))
    assert roles == {"same": {k(1): "open"}} and mems["same"]["text_sha256"] == IR.sha256_text(changed["text"])


def test_memory_without_v2_keys_is_dropped(tmp_path):
    only_pr = mem("only", "[완료] PR #12 머지.")
    db = source(tmp_path, {"only": (only_pr, {k(12): "done"})})
    rc, out, _ = rekey(tmp_path, db, [only_pr])
    assert IR.load_store(str(out))[1] == {}


def test_spend_cap_stops_and_publishes_what_is_done(tmp_path, capsys):
    entries = {"r%d" % i: (mem("r%d" % i, "[완료] #%d~#%d 끝." % (10 * i + 1, 10 * i + 3)),
                           {k(10 * i + 1): "open", k(10 * i + 3): "open"}) for i in range(1, 6)}
    entries["same"] = (SAME, {k(1): "open"})
    db = source(tmp_path, entries)
    caller = Caller(cost=0.01)
    rc, out, _ = rekey(tmp_path, db, [m for m, _ in entries.values()], caller=caller, max_usd=0.025, batch=1)
    assert rc == 3 and len(caller.prompts) == 2
    _, mems, roles = IR.load_store(str(out))
    assert "same" in mems and len([m for m in mems if m.startswith("r")]) == 2
    done = [json.loads(x) for x in capsys.readouterr().out.splitlines() if '"phase": "done"' in x]
    assert done[-1]["stopped"] is True and done[-1]["unjudged"] == 3


@pytest.mark.parametrize("which", ["exists", "same"])
def test_out_must_be_a_new_path(tmp_path, which):
    db = source(tmp_path, {"same": (SAME, {k(1): "open"})})
    out = db if which == "same" else tmp_path / "taken.sqlite"
    if which == "exists":
        out.write_bytes(b"x")
    with pytest.raises(S.Fail, match="--out"):
        S.rekey(str(db), str(out), {"same": SAME}, ALIASES, lambda: Caller(), None, rules="v2")


def test_source_judged_with_another_prompt_is_refused(tmp_path):
    db = source(tmp_path, {"same": (SAME, {k(1): "open"})}, meta_extra={"prompt_sha256": "0" * 64})
    with pytest.raises(S.Fail, match="prompt_sha256"):
        rekey(tmp_path, db, [SAME])


def test_rekey_carries_and_collects_github(tmp_path):
    db = source(tmp_path, {"same": (SAME, {k(1): "open"})})
    gh_calls = []

    def runner(args):
        gh_calls.append(args)
        return 0, json.dumps([[{"number": 1, "state": "closed", "state_reason": "completed"}]]), ""
    rc, out, _ = rekey(tmp_path, db, [SAME], github=S.GithubConfig(runner, default_owner="o"))
    assert rc == 0 and len(gh_calls) == 2
    assert IR.evidence_for_key(k(1), IR.load_github(str(out))).verdict == IR.GH_CLOSED
    assert IR.load_github(str(db)) is None


def test_save_listing_round_trips_through_the_offline_listing(tmp_path):
    listing = {m["id"]: m for m in [SAME, FIXED, dict(MIXED, type=None, fact_type="experience")]}
    bodies, meta = tmp_path / "b.jsonl", tmp_path / "m.jsonl"
    S.save_listing(listing, str(bodies), str(meta))
    assert stat.S_IMODE(bodies.stat().st_mode) == 0o600 and stat.S_IMODE(meta.stat().st_mode) == 0o600
    back = S.offline_listing(str(bodies), str(meta))
    assert set(back) == set(listing)
    for mid, m in listing.items():
        assert back[mid]["text"] == m["text"] and back[mid]["tags"] == m["tags"]
        assert back[mid]["state"] == m["state"] and back[mid]["mentioned_at"] == m["mentioned_at"]
        assert back[mid]["document_id"] == m["document_id"]
        assert back[mid]["type"] == (m.get("fact_type") or m.get("type"))


def test_cli_rekey_with_offline_listing_and_save_listing(tmp_path, monkeypatch):
    db = source(tmp_path, {"range": (RANGE, {k(10): "open", k(12): "open"})})
    S.save_listing({"range": RANGE}, str(tmp_path / "b.jsonl"), str(tmp_path / "m.jsonl"))
    af = tmp_path / "aliases.json"
    af.write_text(json.dumps(ALIASES))
    caller = Caller()
    monkeypatch.setattr(S, "claude_caller", lambda **kw: caller)
    out = tmp_path / "new.sqlite"
    saved = (tmp_path / "b2.jsonl", tmp_path / "m2.jsonl")
    rc = S.main(["rekey", "--db", str(db), "--out", str(out), "--rules", "v2", "--bank", "bank-x",
                 "--offline-bodies", str(tmp_path / "b.jsonl"), "--offline-meta", str(tmp_path / "m.jsonl"),
                 "--aliases-file", str(af), "--save-listing", str(saved[0]), str(saved[1]), "--max-usd", "1"])
    assert rc == 0 and IR.load_store(str(out))[2]["range"][k(11)] == "done"
    assert set(S.offline_listing(str(saved[0]), str(saved[1]))) == {"range"}


def test_cli_rekey_needs_rules(tmp_path):
    with pytest.raises(SystemExit):
        S.main(["rekey", "--db", "x", "--out", "y"])
