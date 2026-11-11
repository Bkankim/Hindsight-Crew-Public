"""model-refresh.sh clears a model before a full-pass refresh ONLY when trigger.mode is exactly "delta" (issue #272).

A failed refresh leaves a cleared model empty (upstream v1.0.0 has no API to write content back), while a full-mode
model is rebuilt from scratch on every refresh and keeps its old content when that refresh fails - so for full-mode,
absent or unknown modes clear is all risk and no benefit. Reuses the Sandbox harness of test_script_token_argv.py.
"""
import json
import time

import pytest

from test_script_token_argv import model_refresh_box


def list_with_mode(mode):
    trigger = {"refresh_after_consolidation": False}
    if mode is not None:
        trigger["mode"] = mode
    return {"items": [{"id": "m1", "tags": ["project:x"], "trigger": trigger}]}


def post_paths(box):
    bank = "/v1/default/banks/bank-x"
    return [c["argv"][-1].split(bank)[1] for c in box.calls("curl")
            if c["argv"][c["argv"].index("-X") + 1] == "POST"]


def run_refresh(box, *args):
    return box.run("scripts/model-refresh.sh", *args, HC_PERSONAL_BANK="bank-x", HC_OWNER="demo")


def start_as_delta_pass(box):
    # a full rebuild finished an hour ago: far inside FULL_INTERVAL_SECONDS, so this run is a delta pass
    state = box.home / "Library" / "Logs" / "hindsight-model-refresh"
    state.mkdir(parents=True)
    (state / "last-full-epoch").write_text(str(int(time.time()) - 3600))


# (mode in the list response, token the full pass must log, whether clear is called)
FULL_PASS_MATRIX = [
    ("full", "MODEL_REFRESH_FULL m1 clear_skipped mode=full", False),
    ("delta", "MODEL_REFRESH_FULL m1", True),
    (None, "MODEL_REFRESH_FULL m1 clear_skipped mode=none", False),
    ("weird", "MODEL_REFRESH_FULL m1 clear_skipped mode=weird", False),
    # only exactly "delta" clears: whitespace is replaced, never trimmed
    ("delta ", "MODEL_REFRESH_FULL m1 clear_skipped mode=delta_", False),
]


@pytest.mark.parametrize("mode,token,clears", FULL_PASS_MATRIX)
def test_full_pass_clears_only_delta_mode_models(tmp_path, mode, token, clears):
    box = model_refresh_box(tmp_path, list_with_mode(mode))
    r = run_refresh(box)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "MODEL_REFRESH_RUN_DONE full_pass=1 all_ok=1" in r.stdout
    posts = post_paths(box)
    assert ("/mental-models/m1/clear" in posts) is clears, posts
    assert "/mental-models/m1/refresh" in posts
    if clears:
        assert posts.index("/mental-models/m1/clear") < posts.index("/mental-models/m1/refresh")
    full_lines = [ln for ln in r.stdout.splitlines() if "MODEL_REFRESH_FULL" in ln]
    assert len(full_lines) == 1
    assert full_lines[0].split(" ", 2)[2] == token, full_lines
    if clears:
        assert "clear_skipped" not in r.stdout
    assert "MODEL_REFRESH_OK m1 42" in r.stdout


@pytest.mark.parametrize("mode", ["full", "delta", None, "weird", "delta "])
def test_delta_pass_never_clears_whatever_the_mode(tmp_path, mode):
    box = model_refresh_box(tmp_path, list_with_mode(mode))
    start_as_delta_pass(box)
    r = run_refresh(box)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "MODEL_REFRESH_RUN_DONE full_pass=0 all_ok=1" in r.stdout
    assert post_paths(box) == ["/mental-models/m1/refresh"]
    assert "MODEL_REFRESH_FULL" not in r.stdout and "clear_skipped" not in r.stdout


def test_a_model_without_tags_does_not_shift_its_mode_column(tmp_path):
    # rows are "<id> <mode> <tags>": an empty tag list must leave mode intact (read -r m mode tags)
    body = {"items": [{"id": "m1", "tags": [], "trigger": {"refresh_after_consolidation": False, "mode": "delta"}}]}
    box = model_refresh_box(tmp_path, body)
    r = run_refresh(box)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "/mental-models/m1/clear" in post_paths(box)
    assert "clear_skipped" not in r.stdout


def test_failed_refresh_of_a_full_mode_model_neither_clears_nor_advances_the_epoch(tmp_path):
    # the issue's failure: a full-pass refresh that ends failed must leave the model uncleared and retry next run
    box = model_refresh_box(tmp_path, list_with_mode("full"))
    box.set_routes([
        {"match": ["/mental-models/m1/refresh"], "out": '{"operation_id": "op1"}'},
        {"match": ["/operations/op1"], "out": '{"status": "failed"}'},
        {"match": ["/memories/list"], "out": '{"total": 3}'},
        {"match": ["/mental-models"], "out": json.dumps(list_with_mode("full"))},
    ])
    r = run_refresh(box)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "/mental-models/m1/clear" not in post_paths(box)
    assert "MODEL_REFRESH_FULL m1 clear_skipped mode=full" in r.stdout
    assert "MODEL_REFRESH_FAIL m1 operation_op1" in r.stdout
    assert "MODEL_REFRESH_RUN_DONE full_pass=1 all_ok=0" in r.stdout
    assert not (box.home / "Library" / "Logs" / "hindsight-model-refresh" / "last-full-epoch").exists()
