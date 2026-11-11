"""Retain response contracts checked against the installed upstream on 2026-09-23.

api/http.py RetainResponse and api_retain return all mixed-strategy operation_ids;
mcp_tools.py sync_retain can return completed with an empty memory_ids list.
All requests below use MockTransport and the existing tmp-only test harness.
"""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from conftest import bank_config_reply
from gateway import gate_effects
from gateway.retain_gates import GateOutcome
from test_retain_effects_regressions import ScenarioUpstream, pending
from test_retain_gates import BANK, GOTCHA_NEW, GOTCHA_OLD, Notifier, make, mem, retain_args


@pytest.mark.parametrize("payload,expected", [
    ({"status": "completed", "memory_ids": []}, False),
    ({"success": True, "bank_id": BANK, "items_count": 0, "async": False,
      "operation_id": None, "operation_ids": None, "usage": None}, False),
    ({"status": "completed", "memory_ids": ["new-1"]}, True),
    ({"success": True, "bank_id": BANK, "items_count": 1, "async": False,
      "operation_id": None, "operation_ids": None, "usage": None}, True),
    ({"success": True, "items_count": 1, "async": True, "operation_id": None}, False),
])
def test_storage_status_distinguishes_empty_storage_from_sync_success(payload, expected):
    assert gate_effects.storage_status(payload) == (expected, None)


@pytest.mark.parametrize("terminal,expected", [("completed", 1), ("failed", 0)])
def test_rest_waits_for_every_strategy_operation_before_publishing(tmp_path, terminal, expected):
    class MultiStrategy(ScenarioUpstream):
        def __init__(self):
            super().__init__(gate_results=[mem("old-1", GOTCHA_OLD, 0.99)])
            self.reads = []

        def handler(self, request):
            if (config := bank_config_reply(request)) is not None:   # #473 b upstream config read
                return config
            if request.method == "GET" and "/operations/" in request.url.path:
                operation_id = request.url.path.rsplit("/", 1)[1]
                self.reads.append(operation_id)
                return httpx.Response(200, json={
                    "operation_id": operation_id,
                    "status": "completed" if operation_id == "operation-a" else terminal,
                    "result_metadata": {"unit_ids_count": 1, "extraction_errors_count": 0},
                    "child_operations": None,
                })
            if request.url.path.endswith("/memories"):
                return httpx.Response(200, json={
                    "success": True, "bank_id": BANK, "items_count": 2, "async": True,
                    "operation_id": "operation-a", "operation_ids": ["operation-a", "operation-b"],
                })
            return super().handler(request)

    up = MultiStrategy()
    notifier = Notifier()
    client, _ = make(up, tmp_path, notifier)
    with client:
        result = client.post(f"/v1/default/banks/{BANK}/memories", json={
            "async": True,
            "items": [retain_args("[완료] unrelated first strategy", tags=("kind:completion",)),
                      {**retain_args(GOTCHA_NEW), "strategy": "exact"}],
        }, headers={"authorization": "Bearer tok-alice"})
    assert result.status_code == 200
    assert up.reads == ["operation-a", "operation-b"]
    assert len(pending(tmp_path)) == expected and len(notifier.drafts) == expected


def test_multiple_operations_share_one_wait_budget(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(gate_effects, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(gate_effects, "OPERATION_WAIT_SECONDS", 5.0)
    monkeypatch.setattr(gate_effects, "OPERATION_POLL_SECONDS", 0)
    reads, records = [], []

    def handler(request):
        operation_id = request.url.path.rsplit("/", 1)[1]
        reads.append(operation_id)
        now[0] += 3.0
        # The second operation would complete on another read, outside the shared budget.
        status = "processing" if operation_id == "operation-b" and reads.count(operation_id) == 1 \
            else "completed"
        return httpx.Response(200, json={"status": status, "child_operations": None})

    class Audit:
        def record(self, **record):
            records.append(record)

    async def run():
        async with httpx.AsyncClient(base_url="http://mock-only", transport=httpx.MockTransport(handler)) as client:
            outcome = GateOutcome(events=[("recurrence", "confirmed-test", {})])
            await gate_effects.finalize(app=SimpleNamespace(), client=client, upstream_key="",
                plans=[(outcome, "operation-a"), (outcome, "operation-b")],
                identity="alice", bank=BANK, token=None, audit=Audit(), transport="mcp")

    asyncio.run(run())
    assert reads == ["operation-a", "operation-b"]
    assert [r["reason"] for r in records] == ["confirmed-test", "skip:retain-not-completed"]
