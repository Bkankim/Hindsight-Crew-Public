# LaunchAgent templates (mental model automation, issues #10 / #11)

Two agents drive the externally-capped mental models (the ones created with
`refresh_after_consolidation=false` - the daily driver discovers them live from
the gateway, so adding a model needs no edit here):

- `<prefix>.hindsight-model-refresh` - daily 03:30, `scripts/model-refresh.sh`.
  Delta refresh per model; every 48h a full pass: `trigger.mode=delta` models clear -> rebuild (delta-drift rule), all other modes refresh without clear (#272).
  Activity-gated (#11): a model whose tags gained no memories since its last
  refresh logs `MODEL_REFRESH_SKIP` instead of spending an LLM call. State lives
  in `~/Library/Logs/hindsight-model-refresh/activity/`; `--force` bypasses the gate.
- `<prefix>.hindsight-model-audit` - Monday 09:00, `scripts/model-audit.sh`.
  Reports reflect DEMAND from the gateway audit log (tags reflect asked for with
  no covering model = first-priority candidates), a 15,000-char SIZE GUARD, and the
  uncovered rule/decision SUPPLY clusters (threshold 15, secondary). Notifies
  Discord (osascript fallback; `HC_NOTIFY_DISCORD_*` from `.env.host.local`, else the legacy `GGG_*` of `~/.env`). Notify-then-approve: it never creates a model.
- `<prefix>.hindsight-model-watch` - daily 09:30, `scripts/model-watch.sh --grace-hours 2` (#428). Read-only refresh failure watch:
  `last_refresh_failed_at`, observation failures, and missed `refresh_cron` fires; same notification target as
  model-audit. Log `~/Library/Logs/hindsight-model-refresh/watch.log` (grep `MODEL_WATCH_OK|ALERT|FAIL`).

Logs: `~/Library/Logs/hindsight-model-refresh/{refresh,audit}.log`
(grep `MODEL_REFRESH_OK|FULL|SKIP|FAIL|RUN_DONE` and `MODEL_AUDIT_OK|DEMAND|SIZE_WARN|FAIL`).

LaunchAgents start at GUI login only; a run missed
while the machine is off fires on the next matching schedule, not on boot.

## Install

Run from the repo root. Render to temp + `plutil -lint` BEFORE replacing
anything (writing straight into `~/Library/LaunchAgents/` with `>` would
truncate a loaded agent if the render failed):

```sh
cd "$(git rev-parse --show-toplevel)"
. scripts/hc-host-env.sh; hc_load_host_env .   # HC_LAUNCHD_PREFIX from .env.host.local (keys: scripts/hc_host_env.py)
: "${HC_LAUNCHD_PREFIX:?config_missing HC_LAUNCHD_PREFIX (set it in .env.host.local)}"
for n in model-refresh model-audit; do
  f="$HC_LAUNCHD_PREFIX.hindsight-$n"
  tmp="$(mktemp -t "$f").plist"
  python3 scripts/render-launchd.py --template "scripts/launchd/__PREFIX__.hindsight-$n.plist.example" \
      --set PREFIX="$HC_LAUNCHD_PREFIX" --out "$tmp" \
    || { echo "render failed for $f, install aborted"; rm -f "$tmp"; continue; }
  plutil -lint "$tmp" || { echo "render failed for $f, install aborted"; rm -f "$tmp"; continue; }
  mv "$tmp" "$HOME/Library/LaunchAgents/$f.plist"
  launchctl bootout "gui/$(id -u)/$f" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$f.plist"
done
```

The labels are `<prefix>.hindsight-*`; the template file names start with the `__PREFIX__` token.
`HC_LAUNCHD_PREFIX` (and the bank, owner and notification keys the jobs read) live in
`.env.host.local` (keys: `scripts/hc_host_env.py`).

Force one run now (from the repo root, after the `hc_load_host_env` / `HC_LAUNCHD_PREFIX` lines of the
install block): `launchctl kickstart "gui/$(id -u)/$HC_LAUNCHD_PREFIX.hindsight-model-refresh"`
(gated - only models with new memories refresh). Full manual procedure, including
`--force`: `docs/operations.md` 6절.

## Uninstall

```sh
cd "$(git rev-parse --show-toplevel)"
. scripts/hc-host-env.sh; hc_load_host_env .   # HC_LAUNCHD_PREFIX from .env.host.local
: "${HC_LAUNCHD_PREFIX:?config_missing HC_LAUNCHD_PREFIX (set it in .env.host.local)}"
launchctl bootout "gui/$(id -u)/$HC_LAUNCHD_PREFIX.hindsight-model-refresh" 2>/dev/null
launchctl bootout "gui/$(id -u)/$HC_LAUNCHD_PREFIX.hindsight-model-audit" 2>/dev/null
rm -f ~/Library/LaunchAgents/"$HC_LAUNCHD_PREFIX".hindsight-model-{refresh,audit}.plist
```

Models then keep their last content (stale but present); worst case refresh
manually via MCP `refresh_mental_model`.

## Dated follow-up checks (issues #22, #44)

- `<prefix>.hindsight-scheduled-checks` - daily 09:00, `scripts/scheduled-checks.sh`
  (same Discord channel as model-audit: `HC_NOTIFY_DISCORD_*` from `.env.host.local`, else the legacy
  `GGG_*` lines of `~/.env`).
  - `canary-22`: dumps the hindsight log and runs `bench.canary judge` read-only.
    Before 2026-10-06 01:15 KST it sends a one-line interim, or an alarm on
    ROLLBACK or on INVALID whose reasons are not all a stale log tail (retry
    exhausted = immediate rollback; a stale-tail-only INVALID is just an interim
    line). From then on it sends the verdict. The done marker
    (`canary-22.done`, one line `<ISO time> <PASS|ROLLBACK|HOLD|INVALID>`) is written
    only from that judgment time on and only for a final result: PASS, ROLLBACK,
    HOLD, or an INVALID that is not transient (rotated-away logs, mode
    mismatch or invalid baseline never heal on their own, so it ends the job
    after one final alert and manual judgment is needed). A stale-tail-only
    INVALID (every reason starts with `log-coverage: sidecar log ends at`) and
    PENDING write no marker and repeat the next day, since the tail heals once
    requests are logged again. An INVALID whose reasons are only
    `server.log.1.gz ... truncated or corrupt` (rotation gzip in progress when
    the judge read it) also repeats the next day, but the same damage reason on
    two different days in a row ends the job (permanent damage); the previous
    reason is kept in `canary-22.damaged` (date and reasons) and removed on a
    healthy verdict, another reason, or the final alert. No marker is written on
    `--dry-run`. To re-judge after fixing the cause, delete `canary-22.done`
    (and `canary-22.damaged` if present); the next run judges again and writes a
    new marker.
  - `tag-hygiene-44`: from 2026-10-14T19:45:41Z (#43 baseline + 14 days) exports
    personal-demo read-only and runs `scripts/tag_hygiene.py report --since <baseline>`,
    reports pass/fail once.
  - State and markers: `~/Library/Logs/hindsight-scheduled/` (`<job>.done`, and `canary-22.damaged` while a `.1.gz` damage streak is open); log
    `checks.log` (grep `SCHEDULED_`). `--dry-run` prints instead of sending,
    `--now <ISO>` shifts the date logic for testing.
  - Network wait (issue #266): launchd runs a missed 09:00 job in a DarkWake, before Wi-Fi is up.
    Before any job starts, the run waits until `pypi.org` and `discord.com` resolve (`--net-wait`, default 600 s of awake time).
    If they never resolve, no export/report starts, `SCHEDULED_NET_UNAVAILABLE` is logged and the run exits 75.
    The next day's run is the retry (no marker is written).
  - Resend (issue #266): a message Discord did not accept is kept in `notify-pending.jsonl` in the state dir.
    The next run re-sends it first (prefixed `[재전송 - 원래 <time>]`) and removes it once Discord accepts it.
    A network-unavailable run queues one notice there too, so the skipped day is reported.
  - Install with the loop above using `n=scheduled-checks`.
    Remove the agent once both markers exist.

## Item-roles observe (issue #415, R7-8) - not installed by default

- `<prefix>.hindsight-item-roles-observe` - hourly (`StartInterval 3600`), `scripts/item-roles-observe.sh --expect-github`.
  Read-only check of the S3 role store for the first week after the 3-state table is re-enabled:
  per-repo GitHub evidence age (`github_repos.fetched_at` of ok/missing rows, limit `--max-age-hours`, default 2),
  stale repos (over the limit or never fetched), `status=error` repos, store `key_rules`, the sidecar agent's
  `last exit code` (only that field is read from `launchctl print`), and the GitHub-phase error lines of the
  sidecar log's last run. The store is opened with a `?mode=ro` URI only.
  An anomaly sends one line to the same Discord channel as scheduled-checks (`scheduled_checks.notify`) and exits 1;
  the same anomaly code is re-sent at most every 6 hours (`--repeat-hours`, state
  `~/Library/Logs/hindsight-item-roles/observe-state.json`, 0600). Normal = one stdout line `ITEM_ROLES_OBSERVE ok ...`, exit 0.
  No evidence tables in the store (sidecar runs without `--github`) prints `github=off`: normal before
  re-enabling, an anomaly (`github-off`) with `--expect-github`, which the template passes.
  Other anomaly codes: `launchd-missing` (agent not loaded), `launchd-probe-failed` (launchctl timed out or is not
  executable), `launchd-signal` (`last terminating signal` present), `launchd-unknown`, `launchd-label-missing`
  (no `HC_LAUNCHD_PREFIX` under `--expect-github`), `log-missing`, `sidecar-stalled` (sidecar log not written for more
  than 3 x `--sidecar-interval`, default 900 s = 45 min), `store-<missing|locked|schema|unreadable>`, `key-rules`.
  A code re-alerts inside the 6 hours when its size grows (stale/error repo count, log error lines). Suppression state is
  recorded only for alerts Discord accepted; without `HC_NOTIFY_DISCORD_*` every run prints the alert again.
- Log: `~/Library/Logs/hindsight-item-roles/observe.log` (grep `ITEM_ROLES_OBSERVE`).
- Install only in the approval window that re-enables S3 (same render -> lint -> bootstrap as above):

```sh
cd "$(git rev-parse --show-toplevel)"
. scripts/hc-host-env.sh; hc_load_host_env .
: "${HC_LAUNCHD_PREFIX:?config_missing HC_LAUNCHD_PREFIX (set it in .env.host.local)}"
bash scripts/item-roles-observe.sh --expect-github --dry-run      # one read-only run first, nothing sent or recorded
f="$HC_LAUNCHD_PREFIX.hindsight-item-roles-observe"; tmp="$(mktemp -t "$f").plist"
python3 scripts/render-launchd.py --template "scripts/launchd/__PREFIX__.hindsight-item-roles-observe.plist.example" \
    --set PREFIX="$HC_LAUNCHD_PREFIX" --out "$tmp" && plutil -lint "$tmp" \
  && mkdir -p ~/Library/Logs/hindsight-item-roles && mv "$tmp" "$HOME/Library/LaunchAgents/$f.plist" \
  && launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$f.plist"
launchctl print "gui/$(id -u)/$f" | grep -E '^\s*(state|runs|last exit code) ='   # never the full output
```

- Remove after the first week (or on rollback):

```sh
cd "$(git rev-parse --show-toplevel)"
. scripts/hc-host-env.sh; hc_load_host_env .
: "${HC_LAUNCHD_PREFIX:?config_missing HC_LAUNCHD_PREFIX (set it in .env.host.local)}"
launchctl bootout "gui/$(id -u)/$HC_LAUNCHD_PREFIX.hindsight-item-roles-observe" 2>/dev/null
rm -f ~/Library/LaunchAgents/"$HC_LAUNCHD_PREFIX".hindsight-item-roles-observe.plist \
      ~/Library/Logs/hindsight-item-roles/observe-state.json
```
