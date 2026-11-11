"""S4 역할 판정기 호스트 사이드카 (issue #376): backfill·poll·status, 실패·재시도, 지출 상한, meta, 토큰 비노출.

네트워크 0. upstream 은 가짜 GET 함수(memories/list 페이징), LLM 은 가짜 caller(프롬프트 항목을 읽어 정해진 역할을 낸다)다.
토큰 비노출은 진짜 호출기 생성 경로(`main` -> `claude_caller`)를 서브프로세스로 돌리고 가짜 토큰 명령(HOME 아래
`.claude/demoproj02-token.sh`), PATH 의 가짜 `claude`·`docker` 로 확인한다(tests/test_script_token_argv.py 방식).
"""
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import urllib.parse
from pathlib import Path

import pytest

from gateway import item_roles as IR

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("item_roles_sidecar", REPO / "scripts" / "item_roles_sidecar.py")
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)

ALIASES = {"hc": "hindsight-crew", "hindsight-crew": "hindsight-crew"}
BANK = "bank-x"


def mem(mid, text, ft="world", state="valid", at="2026-10-01T00:00:00+00:00", doc="d", tags=()):
    return {"id": mid, "text": text, "type": ft, "state": state, "mentioned_at": at, "document_id": doc,
            "tags": list(tags)}


class Upstream:
    """memories/list 를 type·limit·offset 으로 페이징해 답하는 가짜 GET."""

    def __init__(self, mems, page=2, break_total=False):
        self.mems = {m["id"]: m for m in mems}
        self.page = page
        self.break_total = break_total
        self.paths = []

    def __call__(self, path):
        self.paths.append(path)
        u = urllib.parse.urlparse(path)
        assert u.path == "/v1/default/banks/%s/memories/list" % BANK
        q = dict(urllib.parse.parse_qsl(u.query))
        rows = [m for m in self.mems.values() if m["type"] == q["type"]]
        off, lim = int(q["offset"]), min(int(q["limit"]), self.page)
        total = len(rows) + (1 if self.break_total else 0)
        return {"items": rows[off:off + lim], "total": total}

    def listing(self):
        return S.list_all(self, BANK)


def items_in(prompt):
    assert prompt.startswith(S.ROLE_PROMPT)
    return [json.loads(line) for line in prompt[len(S.ROLE_PROMPT):].splitlines() if line.strip()]


def rule(sentence):
    return "done" if "끝" in sentence else "open" if "남" in sentence else "ref"


class Caller:
    """가짜 claude 호출기. errors = 오류로 답할 호출 순번(1부터) 집합, drop = 응답에서 뺄 (호출 순번, i) 집합 또는 함수."""

    def __init__(self, errors=(), drop=None, cost=0.01, error_when=None):
        self.prompts = []
        self.errors = set(errors)
        self.drop = drop or (lambda n, it: False)
        self.cost = cost
        self.error_when = error_when

    def __call__(self, prompt):
        self.prompts.append(prompt)
        n = len(self.prompts)
        its = items_in(prompt)
        if n in self.errors or (self.error_when and self.error_when(its)):
            return {"is_error": True, "result": "", "total_cost_usd": self.cost}
        out = [{"i": it["i"], "role": rule(it["sentence"])} for it in its if not self.drop(n, it)]
        return {"is_error": False, "result": "결과:\n" + json.dumps(out), "total_cost_usd": self.cost,
                "usage": {"input_tokens": 10, "output_tokens": 5}, "duration_ms": 3}

    def items(self):
        return [it for p in self.prompts for it in items_in(p)]


def run(mode, db, up, caller, max_usd=None, batch=40, aliases=ALIASES):
    return S.run_sync(mode, str(db), up.listing(), aliases, lambda: caller, max_usd, batch=batch)


def store(db):
    meta, mems, roles = IR.load_store(str(db))
    return meta, mems, roles


BASE = [
    mem("a", "[완료] HC#1 끝냈다. HC#2 남은 일."),
    mem("b", "[메모] HC#3 참고.", ft="experience"),
    mem("c", "[메모] 키 없는 기억."),
    mem("d", "[진행] 정리.", tags=["item:hindsight-crew-4"]),
    mem("e", "[완료] HC#5 끝.", state="invalidated"),
]


# --- backfill·poll: 신규·변경·삭제 ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["backfill", "poll"])
def test_new_store_judges_every_keyed_memory(tmp_path, mode):
    db = tmp_path / "item-roles.sqlite"
    up, caller = Upstream(BASE), Caller()
    assert run(mode, db, up, caller) == 0
    meta, mems, roles = store(db)
    assert set(mems) == {"a", "b", "d", "e"}                       # 키 없는 c 는 행이 없다
    assert roles == {"a": {"hindsight-crew#1": "done", "hindsight-crew#2": "open"},
                     "b": {"hindsight-crew#3": "ref"}, "d": {"hindsight-crew#4": "ref"},
                     "e": {"hindsight-crew#5": "done"}}
    assert mems["e"]["state"] == "invalidated" and mems["a"]["document_id"] == "d"
    assert mems["a"]["text_sha256"] == IR.sha256_text(BASE[0]["text"])
    assert len(caller.items()) == 4                                # item 태그만 있는 d 는 LLM 에 가지 않는다
    assert meta["model"] == "claude-haiku-5-5" and meta["prompt_sha256"] == S.PROMPT_SHA256
    assert meta["aliases_sha256"] == IR.aliases_sha256(ALIASES) and meta["schema_version"] == "1"
    assert meta["updated_at"].endswith("Z")


def test_backfill_refuses_an_existing_store(tmp_path):
    db = tmp_path / "s.sqlite"
    run("backfill", db, Upstream(BASE), Caller())
    before = db.read_bytes()
    with pytest.raises(S.Fail, match="exists"):
        run("backfill", db, Upstream(BASE), Caller())
    assert db.read_bytes() == before


def test_poll_without_changes_calls_nothing_and_keeps_the_file(tmp_path):
    db = tmp_path / "s.sqlite"
    run("backfill", db, Upstream(BASE), Caller())
    ino = os.stat(db).st_ino
    caller = Caller()
    assert run("poll", db, Upstream(BASE), caller) == 0
    assert caller.prompts == [] and os.stat(db).st_ino == ino


@pytest.mark.parametrize("change, judged_keys, expect", [
    ("new", ["hindsight-crew#9"], {"f": {"hindsight-crew#9": "open"}}),
    ("text", ["hindsight-crew#1"], {"a": {"hindsight-crew#1": "open"}}),
    ("keys", ["hindsight-crew#1", "hindsight-crew#2"],
     {"a": {"hindsight-crew#1": "done", "hindsight-crew#2": "open", "hindsight-crew#7": "ref"}}),
])
def test_poll_judges_only_new_or_changed_memories(tmp_path, change, judged_keys, expect):
    db = tmp_path / "s.sqlite"
    run("backfill", db, Upstream(BASE), Caller())
    mems = [dict(m) for m in BASE]
    if change == "new":
        mems.append(mem("f", "[인계] HC#9 남음."))
    elif change == "text":
        mems[0]["text"] = "[인계] HC#1 남음."
    else:                                                          # 본문 그대로, item 태그로 키 집합이 바뀜
        mems[0]["tags"] = ["item:hindsight-crew-7"]
    caller = Caller()
    assert run("poll", db, Upstream(mems), caller) == 0
    assert sorted(it["key"] for it in caller.items()) == sorted(judged_keys)
    _, rows, roles = store(db)
    for mid, r in expect.items():
        assert roles[mid] == r
    assert roles["b"] == {"hindsight-crew#3": "ref"}


def test_poll_meta_only_change_updates_the_row_without_llm(tmp_path):
    db = tmp_path / "s.sqlite"
    run("backfill", db, Upstream(BASE), Caller(), )
    _, before, _ = store(db)
    mems = [dict(m) for m in BASE]
    mems[0].update(state="invalidated", mentioned_at="2026-10-02T00:00:00+00:00", document_id="d2")
    caller = Caller()
    assert run("poll", db, Upstream(mems), caller) == 0
    assert caller.prompts == []
    _, after, roles = store(db)
    assert (after["a"]["state"], after["a"]["mentioned_at"], after["a"]["document_id"]) == (
        "invalidated", "2026-10-02T00:00:00+00:00", "d2")
    assert after["a"]["judged_at"] == before["a"]["judged_at"]
    assert after["a"]["text_sha256"] == before["a"]["text_sha256"]
    assert roles["a"] == {"hindsight-crew#1": "done", "hindsight-crew#2": "open"}


@pytest.mark.parametrize("how", ["gone", "keyless"])
def test_poll_drops_vanished_or_keyless_memories(tmp_path, how):
    db = tmp_path / "s.sqlite"
    run("backfill", db, Upstream(BASE), Caller())
    mems = [dict(m) for m in BASE]
    if how == "gone":
        mems = [m for m in mems if m["id"] != "b"]
    else:
        mems[1]["text"] = "[메모] 이제 키가 없다."
    caller = Caller()
    assert run("poll", db, Upstream(mems), caller) == 0
    _, rows, roles = store(db)
    assert "b" not in rows and "b" not in roles and caller.prompts == []


def test_memory_judged_once_per_poll_series(tmp_path):
    """기억마다 한 번: 같은 본문은 다음 poll 에서 다시 판정하지 않는다."""
    db = tmp_path / "s.sqlite"
    up = Upstream(BASE)
    c1, c2 = Caller(), Caller()
    run("poll", db, up, c1)
    run("poll", db, up, c2)
    assert len(c1.items()) == 4 and c2.items() == []


# --- 실패·재시도 --------------------------------------------------------------------------------------------

MANY = [mem("m%02d" % i, "[완료] HC#%d 끝. HC#%d 남음." % (i, i + 100)) for i in range(1, 7)]   # 기억당 언급 2개


def test_llm_error_twice_leaves_batch_memories_unjudged_then_poll_retries(tmp_path):
    db = tmp_path / "s.sqlite"
    up = Upstream(MANY)
    # batch=4: 묶음 1 = m01·m02, 묶음 2 = m03·m04, 묶음 3 = m05·m06. 묶음 2 의 두 시도(호출 2·3)가 오류.
    caller = Caller(errors={2, 3})
    assert run("backfill", db, up, caller, batch=4) == 3
    _, rows, _ = store(db)
    assert set(rows) == {"m01", "m02", "m05", "m06"}
    retry = Caller()
    assert run("poll", db, up, retry, batch=4) == 0
    assert sorted({it["key"] for it in retry.items()}) == ["hindsight-crew#103", "hindsight-crew#104",
                                                           "hindsight-crew#3", "hindsight-crew#4"]
    _, rows, roles = store(db)
    assert set(rows) == {m["id"] for m in MANY}
    assert roles["m03"] == {"hindsight-crew#3": "done", "hindsight-crew#103": "open"}


def test_changed_memory_whose_rejudge_fails_loses_its_old_row(tmp_path):
    """본문이 바뀐 기억의 재판정이 실패하면 옛 행이 판정된 값처럼 남지 않고 미판정(행 없음)이 된다(final-review #376 C11)."""
    db = tmp_path / "s.sqlite"
    run("backfill", db, Upstream(BASE), Caller())
    mems = [dict(m) for m in BASE]
    mems[0].update(text="[인계] HC#1 남음.", state="invalidated")
    assert run("poll", db, Upstream(mems), Caller(errors={1, 2})) == 3
    _, rows, roles = store(db)
    assert "a" not in rows and "a" not in roles and "b" in rows


def test_memory_split_across_batches_is_unjudged_if_either_batch_fails(tmp_path):
    db = tmp_path / "s.sqlite"
    # batch=3: 묶음 1 = m01 두 언급 + m02 첫 언급, 묶음 2 = m02 둘째 + m03 두 언급. 묶음 2 실패.
    caller = Caller(errors={2, 3})
    assert run("backfill", db, Upstream(MANY[:3]), caller, batch=3) == 3
    _, rows, _ = store(db)
    assert set(rows) == {"m01"}


def test_error_then_success_on_retry_judges_the_batch(tmp_path):
    db = tmp_path / "s.sqlite"
    caller = Caller(errors={1})
    assert run("backfill", db, Upstream(MANY[:2]), caller) == 0
    assert len(caller.prompts) == 2 and len(items_in(caller.prompts[1])) == 4
    _, rows, _ = store(db)
    assert set(rows) == {"m01", "m02"}


def test_missing_item_is_retried_once_alone_then_becomes_ref(tmp_path):
    db = tmp_path / "s.sqlite"
    target = "hindsight-crew#101"                                   # m01 의 open 언급
    caller = Caller(drop=lambda n, it: it["key"] == target)          # 두 시도 모두 빠뜨린다
    assert run("backfill", db, Upstream(MANY[:2]), caller) == 0
    assert len(caller.prompts) == 2
    assert [it["key"] for it in items_in(caller.prompts[1])] == [target]   # 재시도는 빠진 항목만
    _, _, roles = store(db)
    assert roles["m01"] == {"hindsight-crew#1": "done", target: "ref"}
    assert roles["m02"] == {"hindsight-crew#2": "done", "hindsight-crew#102": "open"}


def test_missing_item_answered_on_retry_keeps_its_role(tmp_path):
    db = tmp_path / "s.sqlite"
    target = "hindsight-crew#101"
    caller = Caller(drop=lambda n, it: n == 1 and it["key"] == target)
    assert run("backfill", db, Upstream(MANY[:1]), caller) == 0
    _, _, roles = store(db)
    assert roles["m01"][target] == "open"


def test_missing_then_error_on_retry_gets_its_error_retry(tmp_path):
    """부분 응답 뒤 빠진 항목 재시도가 오류면 오류 재시도 1회를 따로 받는다(final-review #376 C7)."""
    db = tmp_path / "s.sqlite"
    target = "hindsight-crew#101"
    caller = Caller(drop=lambda n, it: n == 1 and it["key"] == target, errors={2})
    assert run("backfill", db, Upstream(MANY[:1]), caller) == 0
    assert len(caller.prompts) == 3
    _, _, roles = store(db)
    assert roles["m01"][target] == "open"


def test_missing_then_two_errors_leaves_memory_unjudged(tmp_path):
    db = tmp_path / "s.sqlite"
    target = "hindsight-crew#101"
    caller = Caller(drop=lambda n, it: it["key"] == target, errors={2, 3})
    assert run("backfill", db, Upstream(MANY[:1]), caller) == 3
    _, rows, _ = store(db)
    assert rows == {}


def test_error_then_partial_answer_retries_the_missing_items(tmp_path):
    db = tmp_path / "s.sqlite"
    target = "hindsight-crew#101"
    caller = Caller(errors={1}, drop=lambda n, it: n == 2 and it["key"] == target)
    assert run("backfill", db, Upstream(MANY[:1]), caller) == 0
    assert len(caller.prompts) == 3 and [it["key"] for it in items_in(caller.prompts[2])] == [target]
    _, _, roles = store(db)
    assert roles["m01"][target] == "open"


def test_unparseable_output_counts_as_all_missing():
    calls = []

    def caller(prompt):
        calls.append(prompt)
        return {"is_error": False, "result": "모르겠다", "total_cost_usd": 0.0}
    res, log, cap = S.judge_batch([{"i": 1, "key": "k#1", "sentence": "s"}], caller, S.Spend(None))
    assert res == {1: "ref"} and len(calls) == 2 and not cap


# --- 지출 상한 ----------------------------------------------------------------------------------------------

def test_max_usd_stops_before_a_call_that_would_pass_it(tmp_path):
    db = tmp_path / "s.sqlite"
    up = Upstream(MANY)
    caller = Caller(cost=0.03)
    # batch=4 -> 3 묶음. 호출당 0.03: 첫 호출 전 예상 0.02(기본) <= 0.07, 둘째 전 0.03+0.03 <= 0.07, 셋째 전 0.06+0.03 > 0.07.
    assert run("backfill", db, up, caller, max_usd=0.07, batch=4) == 3
    assert len(caller.prompts) == 2
    _, rows, _ = store(db)
    assert set(rows) == {"m01", "m02", "m03", "m04"}
    more = Caller(cost=0.03)
    assert run("poll", db, up, more, max_usd=0.07, batch=4) == 0
    assert len(more.prompts) == 1
    _, rows, _ = store(db)
    assert len(rows) == 6


def test_max_usd_uses_the_conservative_default_before_any_call(tmp_path):
    db = tmp_path / "s.sqlite"
    caller = Caller()
    assert run("backfill", db, Upstream(MANY[:1]), caller, max_usd=S.DEFAULT_CALL_USD / 2) == 3
    assert caller.prompts == []
    _, rows, _ = store(db)
    assert rows == {}


def test_spend_estimate_is_the_running_average():
    sp = S.Spend(1.0)
    assert sp.estimate() == S.DEFAULT_CALL_USD
    sp.add(0.1)
    sp.add(0.3)
    assert sp.estimate() == pytest.approx(0.2) and sp.spent == pytest.approx(0.4)


# --- meta·프롬프트 ------------------------------------------------------------------------------------------

def test_prompt_and_model_match_the_372_judge():
    from bench import r5_roles as B
    assert S.ROLE_PROMPT == B.ROLE_PROMPT and S.ROLE_MODEL == B.ROLE_MODEL and S.ROLE_BATCH == B.ROLE_BATCH
    assert S.PROMPT_SHA256 == "3baf354a9232636d9ebd2088ffd8c0ac81bc457d7aac23d424b8a6c2fd070b74"
    batch = [{"i": 1, "key": "hindsight-crew#1", "sentence": "[완료] HC#1 끝."}]
    assert S.role_prompt(batch) == B.role_prompt(batch)
    for text in ('[{"i": 1, "role": "done"}, {"i": 9, "role": "open"}]', "x", '[{"i": 1, "role": "maybe"}]'):
        assert S.parse_roles(text, [1]) == B.parse_roles(text, [1])


def test_llm_items_match_372_role_items_order():
    from bench import r5_roles as B
    parsed = {m["id"]: IR.memory_mentions(m["id"], m["text"], m["tags"], ALIASES) for m in BASE + MANY}
    keyed = {k: v for k, v in parsed.items() if any(x.get("sent") is not None for x in v["mentions"])}
    mine = [(it["i"], it["mid"], it["key"], it["sentence"]) for it in S.items_of(keyed)]
    theirs = [(it["i"], it["mid"], it["key"], it["sentence"]) for it in B.role_items(keyed)]
    assert mine == theirs


@pytest.mark.parametrize("field", ["aliases", "model", "prompt"])
def test_poll_refuses_a_store_judged_with_other_settings(tmp_path, monkeypatch, field):
    db = tmp_path / "s.sqlite"
    run("backfill", db, Upstream(BASE), Caller())
    before = db.read_bytes()
    aliases = ALIASES
    if field == "aliases":
        aliases = dict(ALIASES, x="hindsight-crew")
    elif field == "model":
        monkeypatch.setattr(S, "ROLE_MODEL", "claude-other")
    else:
        monkeypatch.setattr(S, "PROMPT_SHA256", "0" * 64)
    with pytest.raises(S.Fail, match="delete it and run backfill"):
        run("poll", db, Upstream(BASE), Caller(), aliases=aliases)
    assert db.read_bytes() == before


def test_upstream_total_mismatch_fails_before_touching_the_store(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.sqlite"
    run("backfill", db, Upstream(BASE), Caller())
    before = db.read_bytes()
    monkeypatch.setattr(S, "upstream_getter", lambda allow_prod: Upstream(BASE, break_total=True))
    monkeypatch.setattr(S, "claude_caller", lambda: pytest.fail("no LLM on upstream failure"))
    af = tmp_path / "aliases.json"
    af.write_text(json.dumps(ALIASES))
    rc = S.main(["poll", "--db", str(db), "--bank", BANK, "--aliases-file", str(af)])
    assert rc == 1 and "total" in capsys.readouterr().err
    assert db.read_bytes() == before


def test_status_offline_and_online(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.sqlite"
    run("backfill", db, Upstream(BASE), Caller())
    capsys.readouterr()
    assert S.main(["status", "--db", str(db), "--offline"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["judged_memories"] == 4 and out["mentions"] == 5 and out["meta"]["schema_version"] == "1"
    assert out["role_dist"] == {"done": 2, "open": 1, "ref": 2}
    mems = BASE + [mem("f", "[인계] HC#9 남음.")]
    monkeypatch.setattr(S, "upstream_getter", lambda allow_prod: Upstream(mems))
    af = tmp_path / "aliases.json"
    af.write_text(json.dumps(ALIASES))
    assert S.main(["status", "--db", str(db), "--bank", BANK, "--aliases-file", str(af)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["unjudged"] == 1 and out["keyed"] == 5 and out["aliases_match"] is True


def test_policy_aliases_come_from_the_merged_bank_policy(tmp_path):
    pol = tmp_path / "bank-policies.json"
    pol.write_text(json.dumps({"defaults": {}, "banks": {}}))
    (tmp_path / "local").mkdir()
    (tmp_path / "local" / "bank-policies.json").write_text(json.dumps(
        {"banks": {BANK: {"item_repo_aliases": {"hc": "hindsight-crew"}}}}))
    assert S.policy_aliases(str(pol), BANK) == {"hc": "hindsight-crew"}
    with pytest.raises(S.Fail, match="no entry"):
        S.policy_aliases(str(pol), "other-bank")


def test_non_copy_target_needs_allow_prod(monkeypatch):
    class BC:
        class Fail(Exception):
            pass

        @staticmethod
        def resolve_target():
            return "hindsight-crew-hindsight-1", False, "k"
    monkeypatch.setattr(S, "_bank_config", lambda: BC)
    with pytest.raises(S.Fail, match="allow-prod"):
        S.upstream_getter(False)
    assert callable(S.upstream_getter(True))


# --- 토큰 비노출(진짜 호출기 생성 경로) ------------------------------------------------------------------------

FAKE_TOKEN = "FAKE-DEMOPROJ02-TOKEN-3b9e51d0"
FAKE_KEY = "FAKE-UPSTREAM-KEY-a71c04"

FAKE_CLAUDE = r'''#!%(py)s
import json, os, sys
prompt = sys.stdin.read()
log = os.environ["SIDECAR_STUB_LOG"]
with open(log, "a") as fh:
    fh.write(json.dumps({"tool": "claude", "argv": sys.argv[1:],
                         "token_ok": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") == os.environ["SIDECAR_WANT"]}) + "\n")
head = "항목:\n"
items = [json.loads(l) for l in prompt[prompt.index(head) + len(head):].splitlines() if l.strip()]
out = [{"i": it["i"], "role": "done" if "끝" in it["sentence"] else "ref"} for it in items]
print(json.dumps({"is_error": False, "result": json.dumps(out), "total_cost_usd": 0.001,
                  "usage": {"input_tokens": 1, "output_tokens": 1}, "duration_ms": 1}))
'''

FAKE_DOCKER = r'''#!%(py)s
import json, os, sys, urllib.parse
req = json.loads(sys.stdin.read())
with open(os.environ["SIDECAR_STUB_LOG"], "a") as fh:
    fh.write(json.dumps({"tool": "docker", "argv": sys.argv[1:], "method": req["method"],
                         "key_ok": req["key"] == os.environ["SIDECAR_KEY_WANT"]}) + "\n")
mems = json.load(open(os.environ["SIDECAR_MEMS"]))
q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(req["path"]).query))
rows = [m for m in mems if m["type"] == q["type"]]
off, lim = int(q["offset"]), int(q["limit"])
print(json.dumps({"status": 200, "body": json.dumps({"items": rows[off:off + lim], "total": len(rows)})}))
'''


def test_real_caller_path_keeps_the_token_out_of_every_output(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    tok = home / ".claude" / "demoproj02-token.sh"
    tok.write_text("#!/bin/sh\nprintf '%s\\n' '" + FAKE_TOKEN + "'\n")
    tok.chmod(0o700)
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for name, src in (("claude", FAKE_CLAUDE), ("docker", FAKE_DOCKER)):
        p = stubs / name
        p.write_text(src % {"py": sys.executable})
        p.chmod(0o755)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    log = out_dir / "stub.log"
    mems_file = out_dir / "mems.json"
    mems_file.write_text(json.dumps(BASE, ensure_ascii=False))
    envfile = out_dir / "env.local"
    envfile.write_text("HC_HINDSIGHT_API_KEY=%s\n" % FAKE_KEY)
    aliases = out_dir / "aliases.json"
    aliases.write_text(json.dumps(ALIASES))
    db = out_dir / "item-roles.sqlite"
    env = {"PATH": "%s:/usr/bin:/bin" % stubs, "HOME": str(home), "HC_TARGET": "next",
           "HC_ENV_FILE": str(envfile), "HC_COMPOSE_PROJECT": "hc-next", "HC_HINDSIGHT_CONTAINER": "hc-next-hindsight-1",
           "HC_GATEWAY_BIND": "127.0.0.1:18888", "HC_PG0_VOLUME": "hc-next_pg0",
           "SIDECAR_STUB_LOG": str(log), "SIDECAR_WANT": FAKE_TOKEN, "SIDECAR_KEY_WANT": FAKE_KEY,
           "SIDECAR_MEMS": str(mems_file)}
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "item_roles_sidecar.py"), "backfill", "--db", str(db),
                        "--bank", BANK, "--aliases-file", str(aliases), "--max-usd", "1"],
                       env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, "exit %d" % r.returncode
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    claude = [c for c in calls if c["tool"] == "claude"]
    assert claude and all(c["token_ok"] for c in claude)               # 토큰은 자식 env 로 갔다
    assert all(c["argv"] == list(S.CLAUDE_ARGS) for c in claude)
    assert all(c["key_ok"] and c["method"] == "GET" for c in calls if c["tool"] == "docker")
    for secret in (FAKE_TOKEN, FAKE_KEY):
        assert secret not in r.stdout and secret not in r.stderr
        assert all(secret not in json.dumps(c["argv"]) for c in calls)
        assert secret not in db.read_bytes().decode("latin-1")
    for p in out_dir.iterdir():
        if p.name != "env.local":
            assert FAKE_TOKEN.encode() not in p.read_bytes(), p.name
            assert FAKE_KEY.encode() not in p.read_bytes(), p.name
    _, rows, roles = store(db)
    assert set(rows) == {"a", "b", "d", "e"} and roles["a"]["hindsight-crew#1"] == "done"


# --- launchd 예시 ---------------------------------------------------------------------------------------

def test_launchd_example_runs_poll_once_and_is_not_installed():
    import plistlib
    tpl = REPO / "scripts" / "launchd" / "__PREFIX__.hindsight-item-roles-sidecar.plist.example"
    data = plistlib.loads(tpl.read_bytes())
    assert data["Label"] == "__PREFIX__.hindsight-item-roles-sidecar"
    args = data["ProgramArguments"]
    assert "__REPO__/scripts/item_roles_sidecar.py" in args and "poll" in args
    assert "--max-usd" in args and "backfill" not in args
    assert data.get("RunAtLoad") is not True
    assert "StartInterval" in data


# --- final-review #376 재작업 회귀 ------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["0", "-40"])
def test_batch_below_one_is_a_usage_error(tmp_path, monkeypatch, value):
    """Bugs #6: --batch 0 은 ZeroDivisionError, 음수는 아무것도 판정하지 않았다."""
    monkeypatch.setattr(S, "upstream_getter", lambda allow_prod: Upstream(BASE))
    monkeypatch.setattr(S, "claude_caller", lambda: Caller())
    af = tmp_path / "aliases.json"
    af.write_text(json.dumps(ALIASES))
    with pytest.raises(SystemExit) as e:
        S.main(["backfill", "--db", str(tmp_path / "s.sqlite"), "--bank", BANK, "--aliases-file", str(af),
                "--batch", value])
    assert e.value.code == 2
    assert not (tmp_path / "s.sqlite").exists()


def test_poll_keeps_an_existing_0600_store_mode_by_default(tmp_path):
    """Bugs #7: 기본 --file-mode 가 0600 저장소를 0644 로 넓혔다."""
    db = tmp_path / "s.sqlite"
    assert S.run_sync("backfill", str(db), Upstream(BASE).listing(), ALIASES, lambda: Caller(), None,
                      file_mode=0o600) == 0
    mems = BASE + [mem("f", "[인계] HC#9 남음.")]
    assert run("poll", db, Upstream(mems), Caller()) == 0
    assert oct(os.stat(db).st_mode & 0o777) == oct(0o600)


def test_spend_estimate_ignores_error_calls():
    """Bugs #10: 비용 0 으로 기록된 오류·시간 초과 호출은 호출당 예상 평균에서 뺀다."""
    sp = S.Spend(1.0)
    sp.add(0.0, ok=False)
    assert sp.estimate() == S.DEFAULT_CALL_USD               # 응답한 호출이 없으면 기본값
    sp.add(0.03, ok=True)
    sp.add(0.0, ok=False)
    sp.add(0.0, ok=False)
    assert sp.estimate() == pytest.approx(0.03) and sp.calls == 4 and sp.spent == pytest.approx(0.03)


def test_error_calls_do_not_dilute_the_cap_estimate(tmp_path):
    db = tmp_path / "s.sqlite"
    # batch=4 -> 3 묶음. 호출 1·2(묶음 1) 오류 비용 0, 호출 3(묶음 2) 응답 0.03. 묶음 3 앞에서 오류를 평균에 넣으면
    # 예상 0.01 이라 0.04 <= 0.05 로 통과했고, 응답 호출 평균이면 0.03 + 0.03 > 0.05 라 멈춘다.
    caller = Caller(cost=0.03, errors={1, 2})
    caller_cost = caller.__call__

    def call(prompt):
        r = caller_cost(prompt)
        if r.get("is_error"):
            r["total_cost_usd"] = 0.0
        return r
    assert S.run_sync("backfill", str(db), Upstream(MANY).listing(), ALIASES, lambda: call, 0.05, batch=4) == 3
    assert len(caller.prompts) == 3


def test_meta_bank_is_written_and_another_bank_is_refused(tmp_path):
    """C14: 같은 --db 에 다른 뱅크로 poll 하면 기존 행이 모두 지워졌다."""
    db = tmp_path / "s.sqlite"
    assert S.run_sync("backfill", str(db), Upstream(BASE).listing(), ALIASES, lambda: Caller(), None, bank="b1") == 0
    meta, _, _ = store(db)
    assert meta["bank"] == "b1"
    before = db.read_bytes()
    with pytest.raises(S.Fail, match="another bank"):
        S.run_sync("poll", str(db), Upstream(BASE[:1]).listing(), ALIASES, lambda: Caller(), None, bank="b2")
    assert db.read_bytes() == before


def test_old_store_without_meta_bank_is_filled_on_poll(tmp_path):
    db = tmp_path / "s.sqlite"
    assert S.run_sync("backfill", str(db), Upstream(BASE).listing(), ALIASES, lambda: Caller(), None) == 0
    assert "bank" not in store(db)[0]
    caller = Caller()
    assert S.run_sync("poll", str(db), Upstream(BASE).listing(), ALIASES, lambda: caller, None, bank="b1") == 0
    assert store(db)[0]["bank"] == "b1" and caller.prompts == []


def test_unexpected_caller_exception_is_a_batch_error_and_other_batches_publish(tmp_path, capsys):
    """C4: 호출기의 예상 밖 예외가 실행 전체를 죽여 판정한 다른 묶음 결과까지 잃었다."""
    db = tmp_path / "s.sqlite"
    inner = Caller()

    def call(prompt):
        if any("HC#3 " in it["sentence"] for it in items_in(prompt)):
            raise RuntimeError("secret-ish detail FAKE-TOKEN-xyz")
        return inner(prompt)
    rc = S.run_sync("backfill", str(db), Upstream(MANY).listing(), ALIASES, lambda: call, None, batch=4)
    assert rc == 3
    _, rows, _ = store(db)
    assert set(rows) == {"m01", "m02", "m05", "m06"}
    out = capsys.readouterr()
    assert "RuntimeError" in out.out and "FAKE-TOKEN-xyz" not in out.out + out.err


# --- #383 판정 개선 옵션: 모델·입력 확장(기억 전문)·다수결·오프라인 나열 ---------------------------------------------

MEM_PROMPT = "테스트용 기억 단위 판정 지시.\n기록:\n"


def mem_blocks(prompt):
    """기억 단위 프롬프트 -> 기억 블록 목록(지시문 뒤 JSON 줄)."""
    assert prompt.startswith(MEM_PROMPT)
    return [json.loads(line) for line in prompt[len(MEM_PROMPT):].splitlines() if line.strip()]


class MemCaller:
    """기억 단위 가짜 호출기. votes = 호출 순번(1부터) -> {키: 역할}(없으면 by_key), errors = 오류로 답할 순번."""

    def __init__(self, by_key=None, votes=None, errors=(), cost=0.02, model="claude-sonnet-5-5"):
        self.by_key = by_key or {}
        self.votes = votes or {}
        self.errors = set(errors)
        self.cost = cost
        self.model = model
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        n = len(self.prompts)
        if n in self.errors:
            return {"is_error": True, "result": "", "total_cost_usd": 0.0}
        table = self.votes.get(n, self.by_key)
        out = [{"i": k["i"], "role": table.get(k["key"], "ref")} for b in mem_blocks(prompt) for k in b["keys"]]
        return {"is_error": False, "result": json.dumps(out), "total_cost_usd": self.cost,
                "usage": {"input_tokens": 10, "output_tokens": 5}, "duration_ms": 3,
                "modelUsage": {self.model: {"costUSD": self.cost}}}


def mcfg(votes=1, model="claude-sonnet-5-5"):
    return S.JudgeConfig(model=model, input_mode="memory", votes=votes, prompt=MEM_PROMPT)


def run_cfg(mode, db, mems, caller, cfg, **kw):
    return S.run_sync(mode, str(db), Upstream(mems).listing(), ALIASES, lambda: caller, kw.pop("max_usd", None),
                      cfg=cfg, **kw)


def test_default_config_is_the_376_haiku_sentence_judge():
    cfg = S.JudgeConfig()
    assert (cfg.model, cfg.input_mode, cfg.votes) == ("claude-haiku-5-5", "sentence", 1)
    assert cfg.prompt == S.ROLE_PROMPT and cfg.prompt_sha256 == S.PROMPT_SHA256
    assert cfg.judge_mode == IR.JUDGE_MODE_LEGACY
    assert cfg.claude_args() == S.CLAUDE_ARGS


@pytest.mark.parametrize("model", ["claude-sonnet-5-5", "claude-opus-5-5"])
def test_model_option_goes_into_argv_and_meta(tmp_path, model):
    cfg = S.JudgeConfig(model=model)
    args = cfg.claude_args()
    assert args[args.index("--model") + 1] == model and len(args) == len(S.CLAUDE_ARGS)
    db = tmp_path / "s.sqlite"
    assert run_cfg("backfill", db, BASE, Caller(), cfg) == 0
    meta, _, _ = store(db)
    assert meta["model"] == model and meta["prompt_sha256"] == S.PROMPT_SHA256
    assert meta["judge_mode"] == "input=sentence;votes=1"


def test_memory_input_sends_whole_text_date_label_and_keys(tmp_path):
    mems = [mem("a", "[완료] HC#1 끝냈다.\nHC#2 남은 일. HC#1 다시 언급.", at="2026-09-30T16:30:00+00:00"),
            mem("b", "[메모] HC#3 참고.", tags=["item:hindsight-crew-7"]),
            mem("d", "[진행] 정리.", tags=["item:hindsight-crew-4"])]
    caller = MemCaller(by_key={"hindsight-crew#1": "done", "hindsight-crew#2": "open", "hindsight-crew#3": "ref"})
    db = tmp_path / "s.sqlite"
    assert run_cfg("backfill", db, mems, caller, mcfg()) == 0
    blocks = [b for p in caller.prompts for b in mem_blocks(p)]
    a = next(b for b in blocks if b["text"].startswith("[완료]"))
    assert a["text"] == mems[0]["text"] and a["date"] == "2026-10-01" and a["label"] == "완료"   # KST 날짜
    assert [k["key"] for k in a["keys"]] == ["hindsight-crew#1", "hindsight-crew#2"]          # 키당 1항목
    assert all("item:" not in json.dumps(b) for b in blocks)                                 # 태그만 키는 항목 아님
    assert len(blocks) == 2                                                                  # d(태그만)는 호출 없음
    meta, rows, roles = store(db)
    assert roles["a"] == {"hindsight-crew#1": "done", "hindsight-crew#2": "open"}
    assert roles["b"] == {"hindsight-crew#3": "ref", "hindsight-crew#7": "ref"}
    assert roles["d"] == {"hindsight-crew#4": "ref"} and set(rows) == {"a", "b", "d"}
    assert meta["judge_mode"] == "input=memory;votes=1" and meta["model"] == "claude-sonnet-5-5"
    assert meta["prompt_sha256"] == IR.sha256_text(MEM_PROMPT)


def test_memory_batches_never_split_a_memory(tmp_path):
    text = "[메모] " + " ".join("HC#%d 끝." % n for n in range(1, 8))
    mems = [mem("m%d" % j, text) for j in range(3)]
    caller = MemCaller()
    assert run_cfg("backfill", tmp_path / "s.sqlite", mems, caller, mcfg(), batch=10) == 0
    per_call = [[b["text"] for b in mem_blocks(p)] for p in caller.prompts]
    assert [len(x) for x in per_call] == [1, 1, 1]             # 7키 기억 2개는 10 항목을 넘으므로 기억마다 1호출
    assert all(len(k) == 7 for p in caller.prompts for k in [[x for b in mem_blocks(p) for x in b["keys"]]])


@pytest.mark.parametrize("votes_by_call, want", [
    ({1: {"hindsight-crew#1": "done"}, 2: {"hindsight-crew#1": "done"}, 3: {"hindsight-crew#1": "open"}}, "done"),
    ({1: {"hindsight-crew#1": "open"}, 2: {"hindsight-crew#1": "ref"}, 3: {"hindsight-crew#1": "open"}}, "open"),
    ({1: {"hindsight-crew#1": "done"}, 2: {"hindsight-crew#1": "open"}, 3: {"hindsight-crew#1": "ref"}}, "ref"),
])
@pytest.mark.parametrize("input_mode", ["sentence", "memory"])
def test_three_votes_take_the_majority_and_all_different_is_ref(tmp_path, votes_by_call, want, input_mode):
    mems = [mem("a", "[메모] HC#1 이야기.")]
    if input_mode == "memory":
        caller, cfg = MemCaller(votes=votes_by_call), mcfg(votes=3)
    else:
        inner = MemCaller(votes=votes_by_call)

        def caller(prompt, _inner=inner):
            _inner.prompts.append(prompt)
            table = _inner.votes[len(_inner.prompts)]
            out = [{"i": it["i"], "role": table.get(it["key"], "ref")} for it in items_in(prompt)]
            return {"is_error": False, "result": json.dumps(out), "total_cost_usd": 0.01}
        cfg = S.JudgeConfig(model="claude-sonnet-5-5", votes=3)
    db = tmp_path / "s.sqlite"
    assert run_cfg("backfill", db, mems, caller, cfg) == 0
    meta, _, roles = store(db)
    assert roles["a"]["hindsight-crew#1"] == want
    assert meta["judge_mode"] == "input=%s;votes=3" % input_mode


def test_a_failed_vote_leaves_the_batch_unjudged(tmp_path):
    # 투표 2 의 호출(2)과 그 오류 재시도(3)가 모두 오류 -> 묶음 실패, 기억 미판정(exit 3)
    caller = MemCaller(by_key={"hindsight-crew#1": "done"}, errors={2, 3})
    db = tmp_path / "s.sqlite"
    assert run_cfg("backfill", db, [mem("a", "[완료] HC#1 끝.")], caller, mcfg(votes=3)) == 3
    _, rows, _ = store(db)
    assert rows == {}


@pytest.mark.parametrize("change", ["model", "input", "votes", "prompt"])
def test_poll_refuses_a_store_judged_with_another_judge_mode(tmp_path, change):
    db = tmp_path / "s.sqlite"
    assert run_cfg("backfill", db, BASE, MemCaller(), mcfg(votes=3)) == 0
    before = db.read_bytes()
    other = {"model": S.JudgeConfig(model="claude-opus-5-5", input_mode="memory", votes=3, prompt=MEM_PROMPT),
             "input": S.JudgeConfig(model="claude-sonnet-5-5", input_mode="sentence", votes=3),
             "votes": mcfg(votes=1),
             "prompt": S.JudgeConfig(model="claude-sonnet-5-5", input_mode="memory", votes=3,
                                     prompt=MEM_PROMPT + "x\n")}[change]
    with pytest.raises(S.Fail, match="delete it and run backfill"):
        run_cfg("poll", db, BASE, MemCaller(), other)
    assert db.read_bytes() == before


def test_old_store_without_judge_mode_is_the_legacy_sentence_judge(tmp_path):
    db = tmp_path / "s.sqlite"
    meta = {"model": S.ROLE_MODEL, "prompt_sha256": S.PROMPT_SHA256, "aliases_sha256": IR.aliases_sha256(ALIASES),
            "updated_at": "2026-11-01T00:00:00Z"}
    IR.publish(str(db), meta, {}, {})
    assert "judge_mode" not in store(db)[0]
    assert run("poll", db, Upstream(BASE), Caller()) == 0           # 기본 설정(문장·1표)은 옛 저장소를 이어 쓴다
    assert store(db)[0]["judge_mode"] == IR.JUDGE_MODE_LEGACY
    IR.publish(str(db), meta, {}, {})
    with pytest.raises(S.Fail, match="judge_mode"):
        run_cfg("poll", db, BASE, MemCaller(), S.JudgeConfig(votes=3))


def test_memory_input_needs_a_prompt():
    with pytest.raises(S.Fail, match="prompt"):
        S.JudgeConfig(input_mode="memory")


def test_spend_log_sums_costs_and_records_the_models(tmp_path):
    log = tmp_path / "roles-llm-x.json"
    caller = MemCaller(by_key={"hindsight-crew#1": "done"}, cost=0.02)
    assert run_cfg("backfill", tmp_path / "s.sqlite", BASE, caller, mcfg(votes=3), spend_log=str(log)) == 0
    doc = json.loads(log.read_text())
    assert doc["cost_usd"] == pytest.approx(0.02 * len(caller.prompts)) and len(doc["calls"]) == len(caller.prompts)
    assert doc["models_used"] == {"claude-sonnet-5-5": len(caller.prompts)}
    assert doc["model"] == "claude-sonnet-5-5" and doc["judge_mode"] == "input=memory;votes=3"
    assert {c["vote"] for c in doc["calls"]} == {1, 2, 3}
    assert oct(os.stat(log).st_mode & 0o777) == oct(0o600)


def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    return str(path)


def offline_files(tmp_path, mems):
    bodies = write_jsonl(tmp_path / "bodies.jsonl", [{"id": m["id"], "mentioned_at": m["mentioned_at"],
                                                      "text": m["text"]} for m in mems])
    meta = write_jsonl(tmp_path / "meta.jsonl", [{"id": m["id"], "mentioned_at": m["mentioned_at"], "tags": m["tags"],
                                                  "state": m["state"], "document_id": m["document_id"],
                                                  "fact_type": m["type"], "keys": [], "completion": False,
                                                  "owner_refs": []} for m in mems]
                       + [{"id": "only-meta", "mentioned_at": "2026-10-01T00:00:00+00:00", "tags": [],
                           "state": "valid", "document_id": "x", "fact_type": "world"}])
    return bodies, meta


def test_offline_listing_joins_bodies_and_meta_and_cuts_by_kst_day(tmp_path):
    mems = [mem("a", "[완료] HC#1 끝.", at="2026-10-13T14:59:59+00:00", tags=["project:x"]),
            mem("b", "[메모] HC#3 참고.", at="2026-10-13T15:00:01+00:00")]      # KST 10-14 00:00:01
    bodies, meta = offline_files(tmp_path, mems)
    full = S.offline_listing(bodies, meta)
    assert set(full) == {"a", "b"} and full["a"]["tags"] == ["project:x"] and full["a"]["text"] == mems[0]["text"]
    assert full["a"]["state"] == "valid" and full["a"]["document_id"] == "d"
    assert set(S.offline_listing(bodies, meta, until="2026-10-13")) == {"a"}


def test_offline_listing_refuses_a_body_without_meta(tmp_path):
    bodies = write_jsonl(tmp_path / "b.jsonl", [{"id": "zz", "mentioned_at": "x", "text": "HC#1"}])
    meta = write_jsonl(tmp_path / "m.jsonl", [])
    with pytest.raises(S.Fail, match="meta"):
        S.offline_listing(bodies, meta)


def test_offline_store_equals_the_upstream_store(tmp_path):
    """같은 기억이면 오프라인 나열로 만든 저장소의 행·역할이 upstream 나열과 같다(meta updated_at·judged_at 제외)."""
    live = tmp_path / "live.sqlite"
    off = tmp_path / "off.sqlite"
    assert run("backfill", live, Upstream(BASE), Caller()) == 0
    bodies, meta = offline_files(tmp_path, BASE)
    assert S.run_sync("backfill", str(off), S.offline_listing(bodies, meta), ALIASES, lambda: Caller(), None) == 0
    a, b = store(live), store(off)
    strip = lambda rows: {k: {x: v for x, v in r.items() if x != "judged_at"} for k, r in rows.items()}  # noqa: E731
    assert strip(a[1]) == strip(b[1]) and a[2] == b[2]


FAKE_CLAUDE_MEM = r'''#!%(py)s
import json, os, sys
prompt = sys.stdin.read()
with open(os.environ["SIDECAR_STUB_LOG"], "a") as fh:
    fh.write(json.dumps({"tool": "claude", "argv": sys.argv[1:],
                         "token_ok": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") == os.environ["SIDECAR_WANT"]}) + "\n")
out = []
for line in prompt.splitlines():
    try:
        b = json.loads(line)
    except ValueError:
        continue
    if isinstance(b, dict) and "keys" in b:
        out += [{"i": k["i"], "role": "done" if "끝" in b["text"] else "ref"} for k in b["keys"]]
model = sys.argv[sys.argv.index("--model") + 1]
print(json.dumps({"is_error": False, "result": json.dumps(out), "total_cost_usd": 0.001,
                  "usage": {"input_tokens": 1, "output_tokens": 1}, "duration_ms": 1,
                  "modelUsage": {model: {"costUSD": 0.001}}}))
'''


def test_offline_memory_vote_path_keeps_the_token_out_of_every_output(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    tok = home / ".claude" / "demoproj02-token.sh"
    tok.write_text("#!/bin/sh\nprintf '%s\\n' '" + FAKE_TOKEN + "'\n")
    tok.chmod(0o700)
    stubs = tmp_path / "bin"
    stubs.mkdir()
    (stubs / "claude").write_text(FAKE_CLAUDE_MEM % {"py": sys.executable})
    (stubs / "claude").chmod(0o755)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    log = out_dir / "stub.log"
    bodies, meta = offline_files(out_dir, BASE)
    aliases = out_dir / "aliases.json"
    aliases.write_text(json.dumps(ALIASES))
    prompt = out_dir / "prompt.txt"
    prompt.write_text(MEM_PROMPT)
    db = out_dir / "item-roles.sqlite"
    spend = out_dir / "roles-llm-test.json"
    env = {"PATH": "%s:/usr/bin:/bin" % stubs, "HOME": str(home), "SIDECAR_STUB_LOG": str(log),
           "SIDECAR_WANT": FAKE_TOKEN}
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "item_roles_sidecar.py"), "backfill", "--db", str(db),
                        "--aliases-file", str(aliases), "--offline-bodies", bodies, "--offline-meta", meta,
                        "--mentioned-until", "2026-10-13", "--model", "claude-sonnet-5-5", "--input", "memory",
                        "--votes", "3", "--prompt-file", str(prompt), "--spend-log", str(spend), "--max-usd", "1",
                        "--file-mode", "600"], env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, "exit %d" % r.returncode
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls and all(c["token_ok"] for c in calls) and len(calls) % 3 == 0
    assert all(c["argv"][c["argv"].index("--model") + 1] == "claude-sonnet-5-5" for c in calls)
    assert FAKE_TOKEN not in r.stdout + r.stderr and all(FAKE_TOKEN not in json.dumps(c["argv"]) for c in calls)
    for p in out_dir.iterdir():
        assert FAKE_TOKEN.encode() not in p.read_bytes(), p.name
    meta_s, rows, roles = store(db)
    assert meta_s["judge_mode"] == "input=memory;votes=3" and meta_s["model"] == "claude-sonnet-5-5"
    assert set(rows) == {"a", "b", "d", "e"} and roles["a"]["hindsight-crew#1"] == "done"
    assert json.loads(spend.read_text())["models_used"] == {"claude-sonnet-5-5": len(calls)}
    assert oct(os.stat(db).st_mode & 0o777) == oct(0o600)


# --- final-review #383-1: 입력 검증 ----------------------------------------------------------------------------

@pytest.mark.parametrize("until", ["2026/10/13", "2026-13-01", "2026-10-1", "20261013", ""])
def test_mentioned_until_must_be_a_real_yyyy_mm_dd(tmp_path, until):
    bodies, meta = offline_files(tmp_path, BASE)
    with pytest.raises(S.Fail, match="YYYY-MM-DD"):
        S.offline_listing(bodies, meta, until=until)
    af = tmp_path / "aliases.json"
    af.write_text(json.dumps(ALIASES))
    rc = S.main(["backfill", "--db", str(tmp_path / "s.sqlite"), "--aliases-file", str(af), "--offline-bodies", bodies,
                 "--offline-meta", meta, "--mentioned-until", until])
    assert rc == 1 and not (tmp_path / "s.sqlite").exists()


@pytest.mark.parametrize("votes", [0, -1])
def test_votes_below_one_is_refused_not_defaulted(votes):
    with pytest.raises(ValueError):
        S.JudgeConfig(votes=votes)
    assert S.JudgeConfig(votes=None).votes == S.DEFAULT_VOTES
