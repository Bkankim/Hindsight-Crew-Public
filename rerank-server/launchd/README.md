# LaunchAgent templates (macOS residency)

These are the exact agents running the production sidecar, with absolute paths
tokenized. They are committed so the sidecar deployment
is reproducible from the repo - the sidecar's
operational envelope (`MAX_QUEUE_DEPTH`, `RERANK_MAX_TEXTS`, `IDLE_TRIM_SECONDS`,
`RERANK_TOTAL_DEADLINE_SECONDS`) lives in `EnvironmentVariables` here, and those
same values are the code defaults in `rerank-server/server.py`, so template and
code cannot drift silently.

## Install

Run from the repo root. Each template is rendered to a temp file and validated
BEFORE it replaces anything: writing straight to `~/Library/LaunchAgents/` with
`>` would truncate a currently loaded agent to zero bytes if the render failed.

```sh
cd "$(git rev-parse --show-toplevel)"
. scripts/hc-host-env.sh; hc_load_host_env .   # HC_LAUNCHD_PREFIX from .env.host.local (keys: scripts/hc_host_env.py)
: "${HC_LAUNCHD_PREFIX:?config_missing HC_LAUNCHD_PREFIX (set it in .env.host.local)}"
for n in rerank rerank-probe rerank-logrotate; do
  f="$HC_LAUNCHD_PREFIX.hindsight-$n"
  tmp="$(mktemp -t "$f").plist"
  python3 scripts/render-launchd.py --template "rerank-server/launchd/__PREFIX__.hindsight-$n.plist.example" \
      --set PREFIX="$HC_LAUNCHD_PREFIX" --out "$tmp" \
    || { echo "render failed for $f, install aborted"; rm -f "$tmp"; continue; }
  plutil -lint "$tmp" || { echo "render failed for $f, install aborted"; rm -f "$tmp"; continue; }
  mv "$tmp" "$HOME/Library/LaunchAgents/$f.plist"
  launchctl bootout "gui/$(id -u)/$f" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$f.plist"
done
```

Verify: `bash verify/gate8_rerank_provider.sh` from the repo root.

Uninstall: `launchctl bootout gui/$(id -u)/<label>`. Note the shared-resource
caveat: after the TEI cutover the sidecar backs
production recall, so booting it out is a production outage, not a local test.

The template file names start with the `__PREFIX__` token, which the label prefix replaces
(`__PREFIX__.hindsight-rerank` -> `<prefix>.hindsight-rerank`). `scripts/render-launchd.py` fills
`__PREFIX__` (`--set PREFIX=...`, else `HC_LAUNCHD_PREFIX` from the environment or `.env.host.local`;
no default, an unset prefix fails the render), `__REPO__` (repo root), `__HOME__` (`$HOME`), `__UV__`
(`command -v uv`) and `__PATH__` (`<uv prefix>/bin:<uv prefix>/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin`,
duplicates removed; the shell's own PATH is not used) and fails if any `__NAME__` is left over.
Override with `--set KEY=VALUE` (e.g. `--set UV=/path/to/uv`). `uv` must be on your `PATH` when you render.

## Log rotation (issue #2)

`<prefix>.hindsight-rerank-logrotate` runs `rotate-logs.sh` daily 04:00:
size-based (default 10MB, `RERANK_LOG_MAX_BYTES`) copytruncate of
`server.log`/`probe.log`, gzipped generations (default 3) kept in the same
directory - no sidecar restart needed (launchd holds the fds O_APPEND, verified
2026-09-18), payload non-recording invariant untouched. Tokens:
`LOG_ROTATE_OK|SKIP|FAIL <file> <bytes>` in `rotate.log` (`FAIL`: cp, truncate or gzip
failed; when cp or truncate failed nothing was shifted and the live file is untouched).

The same script also rotates the gateway's audit directory (`HC_AUDIT_DIR`, default
`<repo>/audit`, the directory the compose file bind-mounts at `/audit`):

- `recall-queries.log` (issue #12) uses the sidecar knobs above.
- `gateway.log` (issue #141, the audit log) has its own knobs: `HC_AUDIT_LOG_MAX_BYTES`
  (default 10485760, 10MB) and `HC_AUDIT_LOG_GENERATIONS` (default 5, `gateway.log.1.gz` is the
  newest). It is the attribution record, and the repo's readers (`scripts/model_audit.py`,
  `duplicate_recall.py`, `tag_hygiene.py`, `bench/runner.py`) also read the `.N.gz`
  generations (#177, #178), so its retention is not tied to the sidecar's diagnostic logs.
- The two audit logs are written by the gateway container on a colima virtiofs bind mount, so
  the script truncates them INSIDE the container (issue #180): `docker exec
  $HC_ROTATE_GATEWAY_CONTAINER truncate -s 0 /audit/<name>` (default container
  `hindsight-crew-gateway-1`). A host-side `: >` leaves the guest a stale size and its next
  append writes NUL bytes up to the old length. The docker binary is looked up as
  `HC_DOCKER_BIN` (used alone when set) > `PATH` > `~/.docker/bin`, `/opt/homebrew/bin`,
  `/usr/local/bin`, Docker.app (`HC_DOCKER_FALLBACKS`, space separated, replaces that last
  list; empty means none), so the LaunchAgent's minimal env needs no plist change.
  Two checks guard the exec: the container's `/audit` mount source (`docker inspect`) must
  resolve to the same directory as `HC_AUDIT_DIR` (otherwise a run with another
  `HC_AUDIT_DIR` would empty the operational ledger it never copied), and the host file must be
  smaller afterwards (otherwise the exec hit another mount). No container, no docker, a mount
  mismatch, a failed exec or a file that did not shrink is `LOG_ROTATE_FAIL` (no host
  fallback; `.1` removed, generations not shifted, live file untouched). `server.log`/`probe.log`
  are still truncated on the host.
  Known limits (not handled): `docker exec` has no timeout, the cp-to-truncate window is now a
  `docker exec` round trip (the 0-1 lost lines below were measured for the host `: >`), the
  LaunchAgent's env carries no `DOCKER_HOST`/`DOCKER_CONTEXT` (a colima selected only through an
  exported variable fails every night), and the FAIL line does not say why.
- No gateway restart, no MCP session cut: `gateway/audit.py` reopens the file with
  `open(path, "a")` for every line (one process, one lock), so copytruncate lands the next
  line at the new EOF. Limit (shared by every file here): a line appended between the `cp`
  read and the truncate is lost. Measured for #141 on a 10MB file (15 trials, one write per
  ms): 0-1 lost lines, never a duplicate. That is a measurement for small lines; a line of
  several MB (#134) can be torn by the cp/truncate window, leaving a JSON fragment at the end
  of `.1.gz`.
- The size check uses `wc -c`, so it also works where `stat -f%z` does not (Linux CI).
