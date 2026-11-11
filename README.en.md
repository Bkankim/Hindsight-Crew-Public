# Hindsight-Crew

[한국어](README.md) · **English**

[![ci](https://github.com/Bkankim/Hindsight-Crew-Public/actions/workflows/ci.yml/badge.svg)](https://github.com/Bkankim/Hindsight-Crew-Public/actions/workflows/ci.yml)

**A gateway server that governs team memory with access control and rules.**

**Guard it, groom it, surface it.**
A policy gateway in front of your agents' memory (the [Hindsight](https://github.com/vectorize-io/hindsight) memory server): it decides who may read and write which memory, keeps what gets written in shape, and brings the right memory back at the right moment.
It is packaged as a self-hosted stack that comes up and verifies itself with one command.
It is designed for a setup where several people each have a `personal` bank and share a `team` bank.

## What it does

| | What happens |
|---|---|
| **Guard** | Each token opens only the banks it is allowed. Attempts to switch banks through the body or headers are denied by default. Every access is audit-logged (token fingerprints only, never raw tokens). |
| **Groom** | Per-bank tag vocabulary and format rules are enforced server-side. Fixable tags are normalized; anything else returns the name of the broken rule. Success-shaped write failures are reported as failures, and contradicting writes get a warning. |
| **Surface** | Related memories are injected automatically at configured points in a task. Repeated traps produce a reminder draft. Recall responses are stripped of internal metadata to save tokens, and rows can say whether each is a snapshot of what was said or a consolidated current-state candidate (opt-in `HC_RECALL_KIND_LABEL`, default 0). |

Features such as the work-status table (done vs. open items) are experimental and off behind feature flags. Evidence is in [`docs/benchmarks.md`](docs/benchmarks.md).

**Recall kind labels are off until you turn them on.**
`HC_RECALL_KIND_LABEL` defaults to 0 (off),
and while it is off the recall response bytes are the same as without the feature.
When it is on, each recall result row gets an added `gateway_kind_label` field.
Raw facts (world, experience) are labeled as a snapshot of what was said at the time,
and observations as a consolidated current-state candidate.
The label text itself is in Korean.
Rows of an unknown kind are left as they are,
and result ids, order and existing fields do not change.
Accepted on-values are `1`, `true`, `yes` and `on`.
The default way to turn it on is a shell variable plus recreating only the gateway (`HC_RECALL_KIND_LABEL=1 docker compose --env-file .env.local up -d gateway`).
This turns it on for that run only.
The next `up` without the variable goes back to the default 0.
Writing `HC_RECALL_KIND_LABEL=1` into `.env.local` also turns it on.
But `.env.local` is the `env_file` shared by the gateway and upstream hindsight,
so `up -d gateway` then recreates the upstream hindsight container too (see the comment on the gateway `env_file` in [`docker-compose.yml`](docker-compose.yml)).
To turn it off, recreate the gateway without the variable (`docker compose --env-file .env.local up -d gateway`).
If you wrote it into `.env.local`, delete that line first.
Evidence: the module docstring of [`gateway/recall_label.py`](gateway/recall_label.py) and the `HC_RECALL_KIND_LABEL` wiring in [`docker-compose.yml`](docker-compose.yml).

**Automatic memory injection needs its own wiring.**
The installer sets up only the gateway's injection endpoint (`POST /context-triggers/<bank>`).
The shared trigger table [`policies/context-triggers.json`](policies/context-triggers.json) is empty,
and no client hook is installed.
To use it, write trigger rows in `policies/local/context-triggers.json`,
and connect a hook (for example a Claude Code PreToolUse hook) that posts the task text to that endpoint.
A minimal trigger row example is [`tests/fixtures/policies/sample/context-triggers.json`](tests/fixtures/policies/sample/context-triggers.json),
and the request and response contract is in the module docstring of [`gateway/context_triggers.py`](gateway/context_triggers.py) and in the example at the end of the Demo section below.

## Who it helps

If you use an AI agent such as Claude Code on your own machine:

1. Your agent carries decisions, traps and preferences into the next session.
2. Tag rules and normalization keep memories findable months later.
3. Starting a task you got stuck on before brings the related memory up first, and repeats get flagged.
4. Recall responses lose internal metadata, cutting token cost (in measurements only 59.5% of recall response tokens were actual text; the evidence is the module docstring of [`gateway/recall_prune.py`](gateway/recall_prune.py)).
5. Writes that fail while looking successful get caught.
6. Several agents can share one memory while personal and team banks stay separated by access control.
7. It is self-hosted, so memories stay on your machine (LLM processing uses your own Claude token).
8. One command installs it, and it checks 10 isolation items against itself.

Constraints: macOS Apple Silicon only, 16GB RAM minimum (24GB recommended), Claude Code required (see Install).

## What it solves

Hindsight's built-in auth is a single shared key.
Nothing enforces which identity may read or write which bank.
If clients talk to Hindsight directly, one person's token can read another person's bank.

Hindsight-Crew puts one gateway in front of it.

- **Per-token bank enforcement:** each token opens only the banks it is allowed.
  Attempts to switch banks through the request body (`arguments.bank`) or a header (`X-Bank-Id`), not just the URL path, are rejected (deny by default).
- **Write-format enforcement:** each bank's declared rules (tag vocabulary, content label) are checked server-side.
  Fixable tags are normalized; anything else comes back as a 422 naming the broken rule.
- **Verifiable isolation:** `verify/verify-all.sh` checks isolation, attribution and bypass rejection against the running stack.
  It exits 0 only when all 10 entries pass: 9 gates plus a contract-drift probe.

The threat model is honest-but-curious.
It stops accidental or curious access to someone else's bank; malicious bypass by someone with host access is out of scope.
Scope and residual risks are in [`docs/THREAT-MODEL.md`](docs/THREAT-MODEL.md).

## Install (4 steps)

**Supported platform: macOS on Apple Silicon (arm64) only.**
The reranker runs as an MPS sidecar on the host, and the in-container Claude Code CLI is fetched for linux-arm64 only.
Memory: 16 GB minimum (tight), 24 GB or more recommended; 30 GB of free disk recommended.
`./install` only warns below 16 GB / 30 GB; it does not stop.

**1. Get the source.**

```sh
git clone https://github.com/Bkankim/Hindsight-Crew-Public
cd Hindsight-Crew-Public
```

If git is missing, run `xcode-select --install` first.
Run every command below from this directory.

**2. Install the prerequisites and start the Docker VM.**

```sh
brew install colima docker docker-compose uv
xcode-select --install      # git
colima start --cpu 6 --memory 10
```

Claude Code is also required.
`./install` checks macOS arm64, colima, docker, `docker compose`, uv, git, claude and docker daemon reachability up front.

**3. Put the LLM token in an environment variable.**

```sh
export CLAUDE_SETUP_TOKEN=...   # the output of `claude setup-token`
```

Only the variable name is passed to the installer.
The token value never goes on a command line.

**4. Install.**

```sh
./install --owner <slug> --dry-run                                   # print the plan, change nothing
./install --owner <slug> --claude-token-env CLAUDE_SETUP_TOKEN       # bank personal-<slug>, ACL identity <slug>
```

`./install` sets up the stack, your personal bank and token, the host reranker LaunchAgents, and the Claude Code MCP connection in one go.
The stack is started by `./bootstrap --mac-mps`, and the installer moves on only when `verify-all` is GREEN.
Only then does it edit `~/.claude.json` (backup first).
It is safe to re-run.

| Option | Meaning |
|---|---|
| `--owner SLUG` | owner slug: bank `personal-<slug>`, ACL identity `<slug>` |
| `--claude-token-env VAR` | name of the environment variable holding the `claude setup-token` value (the value is never taken from the command line) |
| `--write-claude-md` | put `templates/CLAUDE.memory.md` into `~/.claude/CLAUDE.md` |
| `--launchd-prefix PREFIX` | LaunchAgent label prefix (default `com.hindsight-crew`) |
| `--uninstall` | remove agents, MCP entry and CLAUDE.md block |
| `--purge` | with `--uninstall`: also delete the docker data volumes |
| `--dry-run` | print the plan, change nothing |

For real use pick a slug other than the demo members `alice`/`bob`.
The memory-usage snippet is [`templates/CLAUDE.memory.md`](templates/CLAUDE.memory.md).
`policies/local/`, where personal policies and triggers live, is gitignored and therefore not in the public edition (it is where private context goes).
`./install --owner <slug>` creates `policies/local/bank-policies.json` from the template ([`docs/operating-guide.md`](docs/operating-guide.md) section 2, in Korean).

To bring up only the stack without the installer, use `./bootstrap` (default `ko-full`, CPU reranker in the container) or `./bootstrap --cpu-en` (light English profile).
`./bootstrap` also succeeds only when `verify/verify-all.sh` exits 0.

## Demo: one retain, one recall

When `secrets/acl.json` does not exist, `bootstrap` creates tokens for the demo members `alice` and `bob` and writes them to `secrets/demo-tokens.env`.
Below is a request in the same shape as the one `verify/gate3_roundtrip.sh` sends on every verify run.
The token header goes through stdin so it never shows up in `ps`.

```sh
set -a; . secrets/demo-tokens.env; set +a
GW=http://127.0.0.1:8888/v1/default/banks/personal-alice

# retain: content starts with "[label] ", tags are <prefix>:<slug>
printf 'authorization: Bearer %s\n' "$HC_DEMO_TOKEN_ALICE" | curl -s -H @- \
  -H 'content-type: application/json' -X POST "$GW/memories" \
  -d '{"items":[{"content":"[demo] quarterly budget review moved to Thursday","tags":["project:hindsight-crew","kind:probe"]}]}'

# recall
printf 'authorization: Bearer %s\n' "$HC_DEMO_TOKEN_ALICE" | curl -s -H @- \
  -H 'content-type: application/json' -X POST "$GW/memories/recall" \
  -d '{"query":"when is the quarterly budget review"}'
```

Send the same retain to `personal-alice` with `$HC_DEMO_TOKEN_BOB` and the gateway rejects it.
A wrong label or tag format returns 422 `rule-violation` with the name of the broken rule.

This is the request an automatic-injection hook sends.
If no trigger row matches, an empty 200 comes back,
and on a match the body is the injection block the hook prepends to the task (JSON with `?format=json`).

```sh
printf 'authorization: Bearer %s\n' "$HC_DEMO_TOKEN_ALICE" | curl -s -H @- \
  -H 'content-type: application/json' -X POST "http://127.0.0.1:8888/context-triggers/personal-alice" \
  -d '{"text":"<the task you are about to do>","tool":"<tool name>"}'
```

## Acceptance gates (`verify/verify-all.sh`)

| Gate | Checks |
|---|---|
| ① health | the gateway front returns 200 and upstream is reachable (`upstream_ok`) |
| ② banks | personal and team banks are provisioned |
| ③ roundtrip | `recall` after `sync_retain` returns the stored memory |
| ④ isolation + attribution | token A cannot read bank B; a team retain is attributed to its member |
| ⑤ adversarial | ACL-less tokens, path traversal, `X-Bank-Id` and body `arguments.bank` smuggling, and attribution forgery are all rejected |
| ✦ contract-drift | the live contract has not drifted from the declared spec |
| ⑦ MCP enforcement | MCP rule enforcement (reject, autofill, no fail-open) actually works |
| ⑧ rerank provider | the declared profile matches the reranker actually in use (no silent CPU fallback) |
| ⑨ LLM chain | every LLM slot uses the declared provider and model, and the token chain setup and CLI version floor hold |
| ⑥ restore | after restoring a backup into an empty stack, retain and recall work |

The table follows the actual run order.
Gate numbers reflect the order gates were added, not the run order.

## Design decisions

- [`CONTEXT.md`](CONTEXT.md): the project glossary (in Korean).
  It defines the tuning acceptance rule (Pareto improvement), hold, A/A noise band, cold request and other judging terms.
- [`docs/adr/`](docs/adr/): records of decisions that are hard to reverse (in Korean).
  - [0001 Tag normalization resolves slots from the declared vocabulary](docs/adr/0001-tag-normalization-resolves-declared-vocabulary.md)
  - [0002 The public edition is a one-way export from the operations repository](docs/adr/0002-public-edition-is-a-one-way-export.md)
- [`docs/THREAT-MODEL.md`](docs/THREAT-MODEL.md): threat model and upgrade path (mTLS, per-bank keys, rate limiting).
- [`docs/operating-guide.md`](docs/operating-guide.md): how the operator actually uses this system (in Korean): bank layout, policy design, retain format, recall and reflect, deploy habits.

The unattended demo uses the image pinned to the production-verified v1.1.0 digest (`@sha256:50311d02...`). In production, also pin `HC_HINDSIGHT_IMAGE` in `.env.local` to a digest; see `.env.example`.
The embedding model fixes the vector dimension, so switching profiles after data is loaded requires a full reindex.

## Experimental evidence

recall quality and speed tuning was judged with a replay harness against acceptance rules fixed in advance.
The verdicts (adopted, not adopted, hold, rollback) are collected on one page in [`docs/benchmarks.md`](docs/benchmarks.md) (in Korean).
Each row carries the comparison, sample size, code and upstream version at the time, limitations, and a link to the source report.

Harness usage and judging rules are in [`bench/README.md`](bench/README.md).
The harness is an operator tool.
It assumes one operator's real usage records, and there is no synthetic example for outside reproduction yet.

## Measurements (as of the time)

The numbers below are values at the time they were measured.
Upstream is now Hindsight v1.x, and these numbers have not been re-measured since.

- **`ko-full-mac-mps` (measured 2026-10-08, hindsight image `0.9.1` at the time):** Apple M5 Pro 24 GiB, colima 6 vCPU / 10 GiB.
  Whole-Mac usage about 10.5 GiB (colima VM about 6.5 GiB + MPS reranker sidecar about 4.0 GiB).
  Disk about 17 GB (hindsight image 5.93 GB, model cache volume 7.08 GB, host reranker model about 2.3 GB, and more).
- **`ko-full` live verification (Hindsight `v0.8.3` at the time):** Colima 6 vCPU / 10 GiB, `bge-m3` + `bge-reranker-v2-m3`, offline (`LLM=none`).
  Every gate of the gate set at the time was GREEN, and Korean semantic recall was confirmed.
- **`cpu-en` (measured 2026-06-26, not re-measured since):** `bge-small-en-v1.5` + `ms-marco-MiniLM`.
  Runtime about 0.85 GB RAM, about 7 GB disk, runs on a 2 GB Docker VM.
  Verified only with the gate set from before gates ⑦⑧⑨ existed.

## Profiles

| Profile | Embedding / reranker | Use |
|---|---|---|
| `ko-full-mac-mps` | `bge-m3` (1024-dim) / `bge-reranker-v2-m3` on the Mac MPS sidecar | what `./install` sets up |
| `ko-full` | `bge-m3` (1024-dim) / `bge-reranker-v2-m3` on container CPU | no sidecar, slower recall |
| `cpu-en` | `bge-small-en-v1.5` / `ms-marco-MiniLM` | English, constrained hosts, CI runners |
| `gpu` | served by TEI | opt-in, needs a CUDA GPU, unverified |

## Layout

```
gateway/         body-aware, deny-by-default MCP/REST policy gateway
verify/          verify-all.sh + gate scripts + lib
scripts/         bootstrap and install helpers, backup and restore, policy and tag tools
profiles/        ko-full / ko-full-mac-mps / cpu-en / gpu
rerank-server/   Mac MPS reranker sidecar
bench/           recall replay harness (operator tool)
tests/           gateway and harness unit tests
docs/            ADRs, threat model, experiment verdict summary
```

## Reading the commit history

The public edition is a single-commit snapshot with no history.
The operating edition's HEAD tree is exported as one commit,
and the operating edition's work history stays in the private operating repository.
In that commit message, `Source-HEAD:` is the source operating HEAD sha,
and `Exported-From:` is the sha of the snapshot commit used for the export (neither can be opened from the public edition).

`#NNN` in code and docs is a private tracker number.
Those issues and PRs cannot be opened from the public edition,
so the reasoning is kept in [`docs/adr/`](docs/adr/).

## License

MIT. See [`LICENSE`](LICENSE).
