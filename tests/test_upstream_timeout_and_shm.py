"""Issue #262 (gateway upstream timeout) and #258 (hindsight /dev/shm) - v1.0.0 opus cutover settings.

#262: with the primary claude-code member failing, v1.0.0 reflect fails over to the second member but
finishes upstream at ~30.4s (copy measurement, 2026-10-15), past the gateway's old 30s httpx cut, so the
caller got HTTP 500 for an answer that did arrive. DEMO decision: 60s.
#258: hindsight's /dev/shm was Docker's 64MiB default; a new bank's first retain failed with
"could not resize shared memory segment" on the copy, and production had the same ShmSize.
"""
import re
from pathlib import Path

from gateway import app as gw_app
from gateway.acl import ACL
from gateway.app import create_app

ROOT = Path(__file__).resolve().parents[1]


def test_default_upstream_client_timeout_is_60s():
    app = create_app(acl=ACL.from_dict({"principals": {}}))
    t = app.state.http_client.timeout
    assert gw_app.UPSTREAM_TIMEOUT_SECONDS == 60.0
    assert (t.connect, t.read, t.write, t.pool) == (60.0, 60.0, 60.0, 60.0)


def test_injected_client_keeps_its_own_timeout():
    import httpx
    c = httpx.AsyncClient(timeout=5.0)
    app = create_app(acl=ACL.from_dict({"principals": {}}), http_client=c)
    assert app.state.http_client is c


def test_hindsight_service_sets_shm_size():
    # No YAML parser in the test deps: take the hindsight service block (2-space indented key up to the
    # next service key) and require the 4-space-indented shm_size line inside it.
    text = (ROOT / "docker-compose.yml").read_text()
    block = re.search(r"^  hindsight:\n(.*?)^  \S", text, re.S | re.M).group(1)
    assert re.search(r"^    shm_size: \"?256m\"?\s*(#.*)?$", block, re.M), "hindsight shm_size 256m missing"
