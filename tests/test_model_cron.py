"""scripts/model_cron.py (issue #428): refresh_cron 적용·해제 도구.

대역 upstream(FakeUpstream)만 쓴다. 실제 게이트웨이·토큰·launchd 는 건드리지 않는다. PATCH 병합은 upstream v1.1.0
_merge_trigger(memory_engine.py 19960~)대로 흉내 낸다: 보낸 필드만 덮어쓰고, refresh_cron 을 보냈는데
refresh_after_consolidation 을 안 보냈으면 그 키를 뺀다.
"""
import copy
import http.client
import io
import json
import os
import sys
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import model_cron  # noqa: E402
import model_watch  # noqa: E402

BANK = "personal-test"
CRON = "30 18 * * *"


def trigger(mode="full", **extra):
    t = {"budget": None, "mode": mode, "refresh_after_consolidation": False, "refresh_cron": None,
         "full_refresh_cron": None, "min_refresh_interval_seconds": None, "fact_types": None,
         "exclude_mental_models": False, "tags_match": None, "tag_groups": None}
    t.update(extra)
    return t


def model(mid, **trig):
    return {"id": mid, "bank_id": BANK, "name": mid, "tags": ["project:" + mid], "trigger": trigger(**trig),
            "last_refreshed_at": "2026-11-08T18:35:00+00:00", "last_memory_seen_at": "2026-11-08T10:00:00+00:00",
            "last_refresh_failed_at": None, "is_stale": False}


class FakeUpstream:
    """request(method, path, body) -> (status, payload), like model_watch.HttpClient."""

    def __init__(self, models, page_cap=2):
        self.models = {m["id"]: copy.deepcopy(m) for m in models}
        self.order = [m["id"] for m in models]
        self.page_cap = page_cap
        self.calls = []
        self.patch_status = {}      # (mid, nth patch of that model) -> status
        self.patch_lost = set()     # (mid, nth): applied, then the answer is lost
        self.patch_lost_unapplied = set()
        self.patch_mangle = {}      # mid or (mid, nth) -> fn(stored trigger) applied after a successful merge
        self.list_hook = None       # fn(offset, items, total) -> (items, total)
        self.list_fail_after = None  # fail list calls after this many
        self._patch_n = {}
        self._list_n = 0

    @property
    def patches(self):
        return [c for c in self.calls if c[0] == "PATCH"]

    def request(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        base, _, query = path.partition("?")
        q = urllib.parse.parse_qs(query)
        prefix = "/v1/default/banks/%s/mental-models" % BANK
        if method == "GET" and base == prefix:
            self._list_n += 1
            if self.list_fail_after is not None and self._list_n > self.list_fail_after:
                return 503, {"error": "down"}
            assert q["detail"] == ["content"]
            offset = int(q.get("offset", ["0"])[0])
            limit = min(int(q.get("limit", ["100"])[0]), self.page_cap)
            items = [copy.deepcopy(self.models[i]) for i in self.order][offset:offset + limit]
            total = len(self.order)
            if self.list_hook:
                items, total = self.list_hook(offset, items, total)
            return 200, {"items": items, "total": total, "limit": limit, "offset": offset}
        if method == "PATCH" and base.startswith(prefix + "/"):
            mid = base[len(prefix) + 1:]
            n = self._patch_n[mid] = self._patch_n.get(mid, 0) + 1
            if mid not in self.models:
                return 404, {"detail": "not found"}
            status = self.patch_status.get((mid, n), 200)
            if status >= 300:
                return status, {"detail": "boom"}
            if (mid, n) in self.patch_lost_unapplied:
                raise model_watch.TransportError("timed out")
            supplied = body["trigger"]
            merged = dict(self.models[mid]["trigger"], **supplied)
            if supplied.get("refresh_cron") and "refresh_after_consolidation" not in supplied:
                merged.pop("refresh_after_consolidation", None)
            mangle = self.patch_mangle.get((mid, n)) or self.patch_mangle.get(mid)
            if mangle:
                merged = mangle(merged)
            self.models[mid]["trigger"] = merged
            if (mid, n) in self.patch_lost:
                raise model_watch.TransportError("connection reset after send")
            return 200, copy.deepcopy(self.models[mid])
        raise AssertionError("unexpected call %s %s" % (method, path))


def three():
    return [model("a"), model("b", fact_types=["world"]), model("c")]


def run(up, tmp_path, cron=CRON, wanted=None, apply=True):
    return model_cron.run(up, BANK, cron, wanted, apply, str(tmp_path / "backups"))


# --------------------------------------------------------------------------- success

def test_apply_all_success_sets_cron_and_keeps_every_other_field(tmp_path):
    up = FakeUpstream(three())
    before = {k: copy.deepcopy(v["trigger"]) for k, v in up.models.items()}
    code, res = run(up, tmp_path)
    assert code == model_cron.EXIT_OK and res["outcome"] == "applied"
    assert len(up.patches) == 3 and res["writes"] == 3
    for mid, old in before.items():
        new = up.models[mid]["trigger"]
        assert new["refresh_cron"] == CRON
        assert new["mode"] == "full"                                    # mode preserved
        assert new["refresh_after_consolidation"] is False              # restated, so the merge did not drop it
        assert {k: v for k, v in new.items() if k != "refresh_cron"} == {k: v for k, v in old.items() if k != "refresh_cron"}
    saved = json.loads(Path(res["backup"]).read_text())
    assert saved["triggers"] == before and saved["target_cron"] == CRON and saved["bank"] == BANK


def test_backup_is_written_before_the_first_patch(tmp_path):
    up = FakeUpstream(three())
    seen = []
    orig = up.request

    def spy(method, path, body=None):
        if method == "PATCH":
            seen.append(any((tmp_path / "backups").glob("triggers-*.json")))
        return orig(method, path, body)
    up.request = spy
    code, _ = run(up, tmp_path)
    assert code == model_cron.EXIT_OK and seen and all(seen)


def test_unset_clears_cron_and_keeps_mode(tmp_path):
    up = FakeUpstream([model("a", refresh_cron=CRON), model("d", mode="delta", refresh_cron=CRON)])
    code, res = run(up, tmp_path, cron=None)
    assert code == model_cron.EXIT_OK and res["outcome"] == "applied"
    assert up.models["a"]["trigger"]["refresh_cron"] is None
    assert up.models["d"]["trigger"]["mode"] == "delta"                 # unset does not refuse or touch mode
    assert [c[2] for c in up.patches] == [{"trigger": {"refresh_cron": None}}] * 2


def test_targets_subset_only(tmp_path):
    up = FakeUpstream(three())
    code, res = run(up, tmp_path, wanted=["b"])
    assert code == model_cron.EXIT_OK and res["targets"] == ["b"]
    assert [p[1].rsplit("/", 1)[1] for p in up.patches] == ["b"]
    assert up.models["a"]["trigger"]["refresh_cron"] is None


# --------------------------------------------------------------------------- failure -> restore

def test_partial_failure_restores_previous_values(tmp_path):
    up = FakeUpstream(three())
    before = {k: copy.deepcopy(v["trigger"]) for k, v in up.models.items()}
    up.patch_status[("b", 1)] = 500
    code, res = run(up, tmp_path)
    assert code == model_cron.EXIT_RESTORED and res["outcome"] == "failed-restored"
    assert res["write_results"] == {"a": "ok", "b": "failed:http_500"}
    assert set(res["restore_results"]) == {"a", "b"}                   # the failed one is restored too
    assert "c" not in res["write_results"]                              # writes stop at the first failure
    assert {k: v["trigger"] for k, v in up.models.items()} == before
    assert res["unrestored"] == []


@pytest.mark.parametrize("applied", [True, False], ids=["landed", "not-landed"])
def test_lost_write_answer_is_treated_as_unknown_and_restored(tmp_path, applied):
    up = FakeUpstream(three())
    before = {k: copy.deepcopy(v["trigger"]) for k, v in up.models.items()}
    (up.patch_lost if applied else up.patch_lost_unapplied).add(("b", 1))
    code, res = run(up, tmp_path)
    assert code == model_cron.EXIT_RESTORED
    assert res["write_results"]["b"].startswith("unknown:")
    assert set(res["restore_results"]) == {"a", "b"}
    assert {k: v["trigger"] for k, v in up.models.items()} == before


def test_reread_mismatch_fails_and_restores(tmp_path):
    up = FakeUpstream(three())
    before = {k: copy.deepcopy(v["trigger"]) for k, v in up.models.items()}
    up.patch_mangle[("c", 1)] = lambda t: dict(t, fact_types=["observation"])   # the server stored something else
    code, res = run(up, tmp_path)
    assert code == model_cron.EXIT_RESTORED
    assert res["mismatched"] == ["c"] and res["problem"].startswith("verify mismatch")
    assert set(res["restore_results"]) == {"a", "b", "c"}
    assert {k: v["trigger"] for k, v in up.models.items()} == before          # the whole saved trigger went back
    assert [c[2] for c in up.patches[3:]] == [{"trigger": before[m]} for m in ("a", "b", "c")]


def test_verify_reread_failure_restores(tmp_path):
    up = FakeUpstream(three())
    up.list_fail_after = 2           # the first list (2 pages) works, the verify read gets 503
    code, res = run(up, tmp_path)
    assert res["problem"].startswith("verify re-read failed")
    assert code == model_cron.EXIT_UNRESTORED                            # the restore check cannot read either
    assert res["unrestored"] == ["a", "b", "c"] and "re-read failed" in res["restore_check"]


def test_restore_call_failure_is_reported(tmp_path):
    up = FakeUpstream(three())
    up.patch_status[("b", 1)] = 500
    up.patch_status[("a", 2)] = 502          # a's restore PATCH fails
    code, res = run(up, tmp_path)
    assert code == model_cron.EXIT_UNRESTORED and res["outcome"] == "failed-unrestored"
    assert res["restore_results"]["a"] == "failed:http_502"
    assert res["restore_failed_calls"] == ["a"]
    assert res["unrestored"] == ["a"]                                    # its trigger still carries the cron
    assert up.models["a"]["trigger"]["refresh_cron"] == CRON


def test_restore_that_answers_ok_but_does_not_restore_is_reported(tmp_path):
    up = FakeUpstream(three())
    up.patch_status[("b", 1)] = 500
    up.patch_mangle["a"] = lambda t: dict(t, refresh_cron=CRON)          # every write of a keeps the cron
    code, res = run(up, tmp_path)
    assert code == model_cron.EXIT_UNRESTORED
    assert res["restore_results"]["a"] == "ok" and res["unrestored"] == ["a"]


# --------------------------------------------------------------------------- refusals (zero writes)

def _drop_second_page(offset, items, total):
    return ([] if offset else items), total


def _duplicate_on_second_page(offset, items, total):
    return (items if not offset else [dict(items[0], id="a")]), total


def _total_changes(offset, items, total):
    return items, (total + 1 if offset else total)


@pytest.mark.parametrize("hook", [_drop_second_page, _duplicate_on_second_page, _total_changes],
                         ids=["empty-page", "row-shift-duplicate", "total-changed"])
@pytest.mark.parametrize("apply", [True, False], ids=["apply", "dry-run"])
def test_page_missing_fails_before_any_write(tmp_path, hook, apply):
    up = FakeUpstream(three())
    up.list_hook = hook
    code, res = run(up, tmp_path, apply=apply)
    assert code == model_cron.EXIT_REFUSED and res["outcome"] == "refused"
    assert "model list" in res["reasons"][0]
    assert up.patches == [] and not (tmp_path / "backups").exists()


def test_dry_run_makes_zero_writes(tmp_path):
    up = FakeUpstream(three())
    before = copy.deepcopy(up.models)
    code, res = run(up, tmp_path, apply=False)
    assert code == model_cron.EXIT_OK and res["outcome"] == "dry-run"
    assert up.patches == [] and res["writes"] == 0
    assert all(c[0] == "GET" for c in up.calls)
    assert up.models == before and not (tmp_path / "backups").exists()
    assert res["plan"]["a"] == {"trigger": {"refresh_cron": CRON, "mode": "full", "refresh_after_consolidation": False}}


@pytest.mark.parametrize("apply", [True, False], ids=["apply", "dry-run"])
def test_non_full_mode_is_refused(tmp_path, apply):
    up = FakeUpstream([model("a"), model("d", mode="delta")])
    code, res = run(up, tmp_path, apply=apply)
    assert code == model_cron.EXIT_REFUSED
    assert res["reasons"] == ["d: trigger.mode is 'delta', not 'full'"]
    assert up.patches == []


def test_refresh_after_consolidation_true_is_refused(tmp_path):
    up = FakeUpstream([model("a", refresh_after_consolidation=True)])
    code, res = run(up, tmp_path)
    assert code == model_cron.EXIT_REFUSED and "mutually exclusive" in res["reasons"][0] and up.patches == []


@pytest.mark.parametrize("cron", ["@daily", "0 3 * * MON", "0 3 1 * 1", "61 * * * *", "* * * *"])
def test_cron_the_watch_cannot_evaluate_is_refused(tmp_path, cron):
    up = FakeUpstream(three())
    code, res = run(up, tmp_path, cron=cron)
    assert code == model_cron.EXIT_REFUSED and res["reasons"][0].startswith("cron ") and up.patches == []


def test_unknown_target_is_refused(tmp_path):
    up = FakeUpstream(three())
    code, res = run(up, tmp_path, wanted=["a", "zzz"])
    assert code == model_cron.EXIT_REFUSED and "zzz" in res["reasons"][0] and up.patches == []


def test_backup_failure_refuses_before_any_write(tmp_path):
    up = FakeUpstream(three())
    blocker = tmp_path / "backups"
    blocker.write_text("not a directory")
    code, res = model_cron.run(up, BANK, CRON, None, True, str(blocker / "x"))
    assert code == model_cron.EXIT_REFUSED and "backup not written" in res["reasons"][0] and up.patches == []


def test_same_trigger_counts_null_and_absent_alike():
    assert model_cron.same_trigger({"a": 1, "b": None}, {"a": 1})
    assert not model_cron.same_trigger({"a": 1}, {"a": 2})
    assert not model_cron.same_trigger(None, {})


# --------------------------------------------------------------------------- real HttpClient over a fake transport

class _Resp:
    def __init__(self, status, raw, truncated=False):
        self.status, self._raw, self._truncated = status, raw, truncated

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        if self._truncated:
            raise http.client.IncompleteRead(self._raw[:3])
        return self._raw


class _BrokenBody:
    def read(self, *args):
        raise http.client.IncompleteRead(b"")

    def close(self):
        pass


@pytest.mark.parametrize("where", ["2xx-body", "error-body"])
def test_transport_exception_mid_apply_takes_the_restore_path(tmp_path, monkeypatch, where):
    """final-review C2: an IncompleteRead while reading a PATCH answer (2xx body, or the body of a 4xx/5xx
    HTTPError) must reach model_cron as TransportError -> restore + report, never a traceback."""
    up = FakeUpstream(three())
    before = {k: copy.deepcopy(v["trigger"]) for k, v in up.models.items()}
    if where == "error-body":
        up.patch_status[("b", 1)] = 500
    base = "http://gw.test"

    def fake_urlopen(req, timeout=None):
        path = req.full_url[len(base):]
        body = json.loads(req.data) if req.data else None
        status, payload = up.request(req.get_method(), path, body)
        raw = json.dumps(payload).encode()
        target = req.get_method() == "PATCH" and path.endswith("/b") and up._patch_n.get("b") == 1
        if status >= 400:
            fp = _BrokenBody() if target else io.BytesIO(raw)
            raise urllib.error.HTTPError(req.full_url, status, "err", {}, fp)
        return _Resp(status, raw, truncated=target)

    monkeypatch.setattr(model_watch.urllib.request, "urlopen", fake_urlopen)
    client = model_watch.HttpClient("fake-token", base_url=base)
    code, res = model_cron.run(client, BANK, CRON, None, True, str(tmp_path / "backups"))
    assert code in (model_cron.EXIT_RESTORED, model_cron.EXIT_UNRESTORED)
    assert res["outcome"] in ("failed-restored", "failed-unrestored")
    assert res["write_results"]["b"].startswith("unknown:")
    assert set(res["restore_results"]) == {"a", "b"}
    assert code == model_cron.EXIT_RESTORED and {k: v["trigger"] for k, v in up.models.items()} == before
