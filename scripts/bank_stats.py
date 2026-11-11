"""Print every bank's data-bearing stats as one canonical JSON line (issue #14).

Runs INSIDE a hindsight container (python3 -c "$(cat scripts/bank_stats.py)"), against that
container's own API on localhost:8888; the API key arrives on stdin so it never shows up in a
process list. backup.sh snapshots the live server with it and restore-test.sh snapshots the
restored one, so both sides of the comparison come from the same code.

Only counts and the last write time are kept: operation/consolidation queues describe work
in flight, not the data a backup has to carry."""
import json
import sys
import urllib.parse
import urllib.request

KEYS = ("total_nodes", "total_links", "total_documents", "nodes_by_fact_type",
        "links_by_link_type", "last_memory_write_at")
key = sys.stdin.read().strip()


def get(path):
    req = urllib.request.Request("http://localhost:8888" + path, headers={"Authorization": "Bearer " + key})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


listing = get("/v1/default/banks")
banks = listing.get("banks", listing) if isinstance(listing, dict) else listing
out = {}
for b in banks:
    stats = get("/v1/default/banks/%s/stats" % urllib.parse.quote(b["bank_id"], safe=""))
    out[b["bank_id"]] = {k: stats.get(k) for k in KEYS}
print(json.dumps(out, sort_keys=True))
