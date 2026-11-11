"""scripts/model_watch.py (issue #428): 매일 멘탈 모델 갱신 실패 감시.

대역 upstream 만 쓴다(실제 게이트웨이·Discord·osascript 없음). model_audit 와 model_watch 의 진입점(main 함수)은
부르지 않는다: 판정은 watch()/evaluate() 로 본다. 시각 기준: 지금 2026-11-09T01:00Z(KST 10:00),
cron "30 18 * * *"(KST 03:30) -> 회차 2026-11-08T18:30Z, 유예 2시간이면 마감 회차도 같은 값.
"""
import copy
import datetime as dt
import sys
import urllib.parse
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import model_watch  # noqa: E402

BANK = "personal-test"
CRON = "30 18 * * *"
UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 9, 1, 0, tzinfo=UTC)
GRACE = dt.timedelta(hours=2)
FIRE = "2026-11-08T18:30:00+00:00"


def ts(s):
    return dt.datetime.fromisoformat(s)


def model(mid, cron=None, stale=False, seen="2026-11-08T10:00:00+00:00", failed=None, **trig):
    t = {"mode": "full", "refresh_after_consolidation": False, "refresh_cron": cron, "fact_types": None,
         "tags_match": None, "tag_groups": None}
    t.update(trig)
    return {"id": mid, "tags": ["project:" + mid], "trigger": t, "last_refreshed_at": "2026-11-08T10:05:00+00:00",
            "last_memory_seen_at": seen, "last_refresh_failed_at": failed, "is_stale": stale}


class FakeUpstream:
    """GET-only upstream: mental-model list (capped pages), refresh operations, memories/list counts."""

    def __init__(self, models, memories=None, ops=None, page_cap=2):
        self.models = [copy.deepcopy(m) for m in models]
        self.memories = memories or []            # [(tags set, updated_at iso)]
        self.ops = ops or []                       # [(status, mental_model_id)]
        self.page_cap = page_cap
        self.calls = []
        self.fail = {}                             # path prefix -> status
        self.list_hook = None

    def request(self, method, path, body=None):
        assert method == "GET", "the watch must never write"
        self.calls.append(path)
        base, _, query = path.partition("?")
        for prefix, status in self.fail.items():
            if base.endswith(prefix):
                return status, {"error": "x"}
        q = urllib.parse.parse_qs(query)
        root = "/v1/default/banks/%s" % BANK
        if base == root + "/mental-models":
            offset = int(q["offset"][0])
            items = self.models[offset:offset + min(int(q["limit"][0]), self.page_cap)]
            total = len(self.models)
            if self.list_hook:
                items, total = self.list_hook(offset, items, total)
            return 200, {"items": copy.deepcopy(items), "total": total}
        if base == root + "/operations":
            assert q["type"] == ["refresh_mental_model"]
            ops = [{"id": "op%d" % i, "mental_model_id": mid, "status": st}
                   for i, (st, mid) in enumerate(self.ops) if st == q["status"][0]]
            return 200, {"operations": ops, "total": len(ops)}
        if base == root + "/memories/list":
            tags = set(q.get("tags", []))
            assert q["tags_match"] == ["all_strict"] and q["time_field"] == ["updated_at"]
            lo = ts(q["start_date"][0]) if "start_date" in q else None
            hi = ts(q["end_date"][0])
            n = sum(1 for mtags, up in self.memories
                    if tags <= mtags and ts(up) < hi and (lo is None or ts(up) >= lo))
            return 200, {"items": [], "total": n}
        raise AssertionError("unexpected GET " + path)


def run_watch(up, now=NOW, grace=GRACE):
    return model_watch.watch(up, BANK, now, grace)


def kinds(report):
    return sorted((a["id"], a["kind"]) for a in report["alerts"])


# --------------------------------------------------------------------------- normal: 0 alerts

def test_all_normal_gives_zero_alerts():
    up = FakeUpstream([model("a"), model("b", cron=CRON), model("c", cron=CRON, stale=True)],
                      memories=[({"project:c"}, "2026-11-08T20:00:00+00:00")])     # after the fire only
    r = run_watch(up)
    assert r["alerts"] == []
    assert r["models"] == {"a": "no_cron", "b": "ok", "c": "ok"}
    assert r["cron_models"] == ["b", "c"]


# --------------------------------------------------------------------------- 1. failure field

@pytest.mark.parametrize("cron", [None, CRON], ids=["no-cron", "cron"])
def test_failure_field_alerts(cron):
    up = FakeUpstream([model("a", cron=cron, failed="2026-11-08T18:40:00+00:00"), model("b")])
    r = run_watch(up)
    assert ("a", "refresh_failed") in kinds(r) and r["models"]["b"] == "no_cron"
    assert len([a for a in r["alerts"] if a["id"] == "a" and a["kind"] == "refresh_failed"]) == 1


def test_cleared_failure_field_after_recovery_is_quiet():
    """upstream writes last_refresh_failed_at back to NULL on success (memory_engine.py 19555)."""
    r = run_watch(FakeUpstream([model("a", cron=CRON, failed=None)]))
    assert r["alerts"] == []


# --------------------------------------------------------------------------- 3. observation failures

def _missing_field(name):
    def build():
        m = model("a")
        del m[name]
        return FakeUpstream([m, model("b")])
    return build


def _page_dropped():
    up = FakeUpstream([model("a"), model("b"), model("c")])
    up.list_hook = lambda offset, items, total: ([] if offset else items, total)
    return up


def _page_shifted():
    up = FakeUpstream([model("a"), model("b"), model("c")])
    up.list_hook = lambda offset, items, total: ([dict(items[0], id="a")] if offset else items, total)
    return up


def _list_down():
    up = FakeUpstream([model("a")])
    up.fail["/mental-models"] = 503
    return up


def _ops_down():
    up = FakeUpstream([model("a", cron=CRON)])
    up.fail["/operations"] = 500
    return up


def _count_down():
    up = FakeUpstream([model("a", cron=CRON, stale=True)])
    up.fail["/memories/list"] = 500
    return up


def _bad_cron():
    return FakeUpstream([model("a", cron="0 3 * * MON")])


def _tag_groups_scope():
    return FakeUpstream([model("a", cron=CRON, stale=True, tag_groups=[{"tags": ["x"], "match": "any"}])])


def _stale_unknown():
    return FakeUpstream([model("a", cron=CRON, stale=None)])


def _cron_without_recent_fire():
    return FakeUpstream([model("a", cron="0 0 29 2 *", stale=True)])        # no Feb 29 in the lookback window


OBS_CASES = [
    ("list-http-503", _list_down, "*"),
    ("page-dropped", _page_dropped, "*"),
    ("page-row-shift", _page_shifted, "*"),
    ("field-missing-failed_at", _missing_field("last_refresh_failed_at"), "a"),
    ("field-missing-seen_at", _missing_field("last_memory_seen_at"), "a"),
    ("field-missing-trigger", _missing_field("trigger"), "a"),
    ("field-missing-is_stale", _missing_field("is_stale"), "a"),
    ("operations-http-500", _ops_down, "*"),
    ("memory-count-http-500", _count_down, "a"),
    ("cron-not-evaluable", _bad_cron, "a"),
    ("scope-tag-groups", _tag_groups_scope, "a"),
    ("is_stale-null", _stale_unknown, "a"),
    ("cron-no-fire-in-lookback", _cron_without_recent_fire, "a"),
]


@pytest.mark.parametrize("build, who", [(b, w) for _, b, w in OBS_CASES], ids=[c[0] for c in OBS_CASES])
def test_observation_failure_alerts(build, who):
    r = run_watch(build())
    assert (who, "observation_failed") in kinds(r), r["alerts"]


# --------------------------------------------------------------------------- 2. cron model missed

def test_cron_model_with_input_before_the_fire_and_no_refresh_alerts():
    up = FakeUpstream([model("a", cron=CRON, stale=True)],
                      memories=[({"project:a"}, "2026-11-08T12:00:00+00:00")])
    r = run_watch(up)
    assert kinds(r) == [("a", "cron_missed")]
    assert FIRE in r["alerts"][0]["detail"]


def test_input_before_the_fire_is_not_hidden_by_newer_input_after_it():
    """The case a 'newest memory vs fire' check gets wrong: the scope also gained a memory after the fire."""
    up = FakeUpstream([model("a", cron=CRON, stale=True)],
                      memories=[({"project:a"}, "2026-11-08T12:00:00+00:00"),
                                ({"project:a"}, "2026-11-08T20:00:00+00:00"),
                                ({"project:a"}, "2026-11-09T00:30:00+00:00")])
    r = run_watch(up)
    assert kinds(r) == [("a", "cron_missed")]
    assert r["alerts"][0]["detail"].startswith("1 in-scope memories")


def test_only_input_after_the_fire_is_quiet():
    up = FakeUpstream([model("a", cron=CRON, stale=True)],
                      memories=[({"project:a"}, "2026-11-08T20:00:00+00:00")])
    assert run_watch(up)["alerts"] == []


def test_refresh_after_the_fire_moved_the_watermark_past_the_input():
    up = FakeUpstream([model("a", cron=CRON, stale=True, seen="2026-11-08T12:00:00+00:00")],
                      memories=[({"project:a"}, "2026-11-08T12:00:00+00:00"),       # = watermark: consumed
                                ({"project:a"}, "2026-11-08T20:00:00+00:00")])
    assert run_watch(up)["alerts"] == []


def test_grace_not_elapsed_judges_the_previous_fire():
    now = dt.datetime(2026, 11, 8, 19, 0, tzinfo=UTC)            # 30 min after the fire, grace 2h
    up = FakeUpstream([model("a", cron=CRON, stale=True)],
                      memories=[({"project:a"}, "2026-11-08T12:00:00+00:00")])     # after the 11-07 fire
    assert run_watch(up, now=now)["alerts"] == []
    later = dt.datetime(2026, 11, 8, 20, 31, tzinfo=UTC)
    assert kinds(run_watch(up, now=later)) == [("a", "cron_missed")]


def test_null_watermark_falls_back_to_last_refreshed_at():
    """upstream: since = last_memory_seen_at or last_refreshed_at (memory_engine.py:2109)."""
    old = FakeUpstream([model("a", cron=CRON, stale=True, seen=None)],                  # refreshed 11-08T10:05
                       memories=[({"project:a"}, "2026-10-01T00:00:00+00:00"),
                                 ({"project:a"}, "2026-11-08T20:00:00+00:00")])
    assert run_watch(old)["alerts"] == []
    pre_fire = FakeUpstream([model("a", cron=CRON, stale=True, seen=None)],
                            memories=[({"project:a"}, "2026-11-08T12:00:00+00:00")])
    assert kinds(run_watch(pre_fire)) == [("a", "cron_missed")]


def test_never_stamped_model_counts_from_the_beginning():
    m = model("a", cron=CRON, stale=True, seen=None)
    m["last_refreshed_at"] = None
    up = FakeUpstream([m], memories=[({"project:a"}, "2026-10-01T00:00:00+00:00")])
    assert kinds(run_watch(up)) == [("a", "cron_missed")]
    assert "start_date" not in [c for c in up.calls if "memories/list" in c][0]


def test_out_of_scope_memory_does_not_count():
    up = FakeUpstream([model("a", cron=CRON, stale=True)],
                      memories=[({"project:other"}, "2026-11-08T12:00:00+00:00")])
    assert run_watch(up)["alerts"] == []


# --------------------------------------------------------------------------- not alerts

@pytest.mark.parametrize("status", ["pending", "processing"])
def test_running_refresh_is_not_a_failure(status):
    up = FakeUpstream([model("a", cron=CRON, stale=True)],
                      memories=[({"project:a"}, "2026-11-08T12:00:00+00:00")], ops=[(status, "a")])
    r = run_watch(up)
    assert r["alerts"] == [] and r["models"]["a"] == "running"
    assert not any("memories/list" in c for c in up.calls)


def test_cron_less_model_without_refresh_is_quiet():
    up = FakeUpstream([model("a", stale=True)], memories=[({"project:a"}, "2026-11-01T00:00:00+00:00")])
    r = run_watch(up)
    assert r["alerts"] == [] and r["models"]["a"] == "no_cron"
    assert not any("/operations" in c or "memories/list" in c for c in up.calls)


def test_not_stale_skips_the_memory_count():
    up = FakeUpstream([model("a", cron=CRON, stale=False)])
    assert run_watch(up)["alerts"] == []
    assert not any("memories/list" in c for c in up.calls)


# --------------------------------------------------------------------------- pieces

@pytest.mark.parametrize("expr, at, expected", [
    ("30 18 * * *", "2026-11-09T01:00:00+00:00", "2026-11-08T18:30:00+00:00"),
    ("30 18 * * *", "2026-11-08T18:30:00+00:00", "2026-11-08T18:30:00+00:00"),     # at the boundary: inclusive
    ("*/15 * * * *", "2026-11-09T01:07:59+00:00", "2026-11-09T01:00:00+00:00"),
    ("0 0 * * 1", "2026-11-09T01:00:00+00:00", "2026-11-09T00:00:00+00:00"),       # 2026-11-09 is a Monday
    ("0 0 * * 0", "2026-11-09T01:00:00+00:00", "2026-11-08T00:00:00+00:00"),
    ("0 0 * * 7", "2026-11-09T01:00:00+00:00", "2026-11-08T00:00:00+00:00"),       # 7 = Sunday too
    ("0 0 1 * *", "2026-11-09T01:00:00+00:00", "2026-11-01T00:00:00+00:00"),
    ("5/20 1-3 * * *", "2026-11-09T02:50:00+00:00", "2026-11-09T02:45:00+00:00"),
    ("0 0 31 2 *", "2026-11-09T01:00:00+00:00", None),                             # never fires
    ("0 0 * * 1/2", "2026-11-09T00:30:00+00:00", "2026-11-09T00:00:00+00:00"),     # Mon/Wed/Fri, croniter dow 0-6
    ("0 0 * * 1/2", "2026-11-08T23:00:00+00:00", "2026-11-06T00:00:00+00:00"),     # Sunday is not a fire
])
def test_latest_fire(expr, at, expected):
    got = model_watch.latest_fire_at_or_before(expr, ts(at))
    assert (got.isoformat() if got else None) == expected


@pytest.mark.parametrize("expr", ["@daily", "0 3 * * MON", "0 3 1 * 1", "60 * * * *", "* * * *", "*/0 * * * *", ""])
def test_unsupported_cron_raises(expr):
    with pytest.raises(model_watch.CronError):
        model_watch.parse_cron(expr)


@pytest.mark.parametrize("tags, trig, expected", [
    (["p:a"], {}, (["p:a"], "all_strict", None)),
    (["p:a"], {"tags_match": "any"}, (["p:a"], "any_strict", None)),
    (["p:a"], {"tags_match": "all"}, (["p:a"], "all_strict", None)),
    ([], {}, ([], "any", None)),
    (["p:a"], {"fact_types": ["world"]}, (["p:a"], "all_strict", "world")),
])
def test_stale_scope_mirrors_upstream(tags, trig, expected):
    assert model_watch.stale_scope({"tags": tags, "trigger": trig}) == expected


@pytest.mark.parametrize("trig", [{"tag_groups": [{"tags": ["x"]}]}, {"fact_types": ["world", "experience"]}])
def test_stale_scope_refuses_what_the_list_cannot_express(trig):
    with pytest.raises(model_watch.ObservationError):
        model_watch.stale_scope({"tags": ["p:a"], "trigger": trig})


def test_memory_count_query_excludes_the_watermark_itself():
    up = FakeUpstream([])
    count = model_watch.make_memory_counter(up, BANK)
    count(model("a"), ts("2026-11-08T10:00:00+00:00"), ts(FIRE))
    q = urllib.parse.parse_qs(up.calls[0].partition("?")[2])
    assert q["start_date"] == ["2026-11-08T10:00:00.000001+00:00"] and q["end_date"] == ["2026-11-08T18:30:00.000000+00:00"]
    assert q["tags"] == ["project:a"] and q["limit"] == ["1"]


def test_report_message_names_every_alert():
    msg = model_watch.report_message({"alerts": [{"id": "a", "kind": "cron_missed", "detail": "d1"},
                                                 {"id": "*", "kind": "observation_failed", "detail": "d2"}]})
    assert "예정 회차 미갱신 `a`: d1" in msg and "관측 실패 `*`: d2" in msg


def test_wrapper_fails_loudly_without_host_config(tmp_path):
    """scripts/model-watch.sh in a scratch tree with an empty HOME: no bank, no run, a greppable failure line."""
    import shutil
    import subprocess
    scripts = tmp_path / "repo" / "scripts"
    scripts.mkdir(parents=True)
    for name in ("model-watch.sh", "hc-python.sh", "hc-host-env.sh", "model_watch.py", "hc_host_env.py"):
        shutil.copy(REPO / "scripts" / name, scripts / name)
    (tmp_path / "home").mkdir()
    r = subprocess.run(["/bin/bash", str(scripts / "model-watch.sh"), "--grace-hours", "2"],
                       env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home")},
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 1
    assert "MODEL_WATCH_FAIL - config_missing HC_PERSONAL_BANK" in r.stdout


def test_fallback_watermark_is_the_one_printed():
    """final-review C9 follow-up: with last_memory_seen_at null the alert names the watermark actually used."""
    up = FakeUpstream([model("a", cron=CRON, stale=True, seen=None)],                  # refreshed 11-08T10:05
                      memories=[({"project:a"}, "2026-11-08T12:00:00+00:00")])
    r = run_watch(up)
    assert kinds(r) == [("a", "cron_missed")]
    detail = r["alerts"][0]["detail"]
    assert "after watermark 2026-11-08T10:05:00+00:00" in detail and "None" not in detail


def test_successful_refresh_after_the_fire_is_not_a_miss():
    """final-review C1: the spec alerts only when no successful refresh came after the fire. A refresh that
    read before the fire and finished after it can leave a pre-fire memory behind the watermark."""
    m = model("a", cron=CRON, stale=True, seen="2026-11-08T10:00:00+00:00")
    m["last_refreshed_at"] = "2026-11-08T18:35:00+00:00"                               # after the 18:30 fire
    up = FakeUpstream([m], memories=[({"project:a"}, "2026-11-08T12:00:00+00:00")])
    r = run_watch(up)
    assert r["alerts"] == [] and r["models"]["a"] == "ok"
