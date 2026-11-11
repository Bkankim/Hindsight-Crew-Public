#!/usr/bin/env bash
# seed-demo.sh - make the demo UNATTENDED: if .env.local is absent, generate the upstream
# key (CSPRNG), 2 demo member tokens, and a team-bank ACL. An existing .env.local is kept, except
# that an empty, placeholder or missing HC_HINDSIGHT_API_KEY is replaced by a generated key (atomic
# rewrite, mode 600); a real key is never touched. secrets/acl.json is never overwritten.
# Idempotent. bash 3.2 compatible.
set -eu
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
mkdir -p secrets audit

rand() { openssl rand -hex "${1:-24}"; }   # CSPRNG

if [ ! -f .env.local ]; then
  KEY="hs_$(rand 24)"
  PROFILE="${HC_PROFILE:-ko-full}"
  ( umask 077 && : > .env.local )      # owner-only before the key is written into it
  {
    echo "# AUTO-SEEDED by seed-demo.sh — DO NOT COMMIT (gitignored)"
    echo "HC_PROFILE=$PROFILE"
    echo "HC_HINDSIGHT_API_KEY=$KEY"
    echo "HC_GATEWAY_BIND=127.0.0.1:8888"
    echo "HC_ATTRIBUTION_MODE=body-inject"
    # demo default = the v1.1.0 digest production verified (rollback coordinates: docs/operations.md 1절);
    # refresh it deliberately together with docker-compose.yml, restore-test.sh and .env.example
    echo "HC_HINDSIGHT_IMAGE=ghcr.io/vectorize-io/hindsight@sha256:50311d02b4d5d2b7c5242bd336edf8735df8374eaad496a92f49c83b37d2aa06"
  } > .env.local
  # merge selected profile model vars
  if [ -f "profiles/$PROFILE.env" ]; then
    grep -E '^(HINDSIGHT_API_|HC_EMBED_DIM)' "profiles/$PROFILE.env" >> .env.local || true
  fi
  echo "seed-demo: wrote .env.local (profile=$PROFILE, key auto-generated)"
else
  echo "seed-demo: .env.local exists - kept (only an empty or placeholder upstream key is replaced below)"
fi

# Upstream key guard (issue #471). A hand-made .env.local (`cp .env.example .env.local`) keeps the
# template placeholder, and compose's `${HC_HINDSIGHT_API_KEY:?}` only rejects an EMPTY value, so the
# placeholder would boot as the real shared Bearer key. An empty, placeholder-looking or missing key is
# replaced by a generated one; a real key is never touched. A file carrying both a real and a
# placeholder line is ambiguous and refused. Values are never printed.
is_placeholder_key() {
  local v="$1"
  v="${v#"${v%%[![:space:]]*}"}"
  case "$v" in
    \"*) v="${v#\"}"; v="${v%%\"*}" ;;     # quoted: the value ends at the closing quote, the rest is a comment
    \'*) v="${v#\'}"; v="${v%%\'*}" ;;
    '#'*) v="" ;;                            # nothing before an inline comment = empty
    *) v="${v%%[[:space:]]#*}" ;;            # unquoted inline comment
  esac
  v="${v#"${v%%[![:space:]]*}"}"; v="${v%"${v##*[![:space:]]}"}"
  v="$(printf '%s' "$v" | tr '[:upper:]' '[:lower:]')"
  case "$v" in
    # whole-value shapes only (a real key may contain e.g. "xxxx" or "<"); REPLACE/AUTOSEED are the template's words
    ''|*replace*|*autoseed*|'<'*'>'|your-*|your_*|placeholder|changeme|change-me|change_me|todo) return 0 ;;
  esac
  case "$v" in
    *[!x]*) ;;
    *) return 0 ;;                                               # xxxx...
  esac
  return 1
}
# Is "$1" a $KEYVAR declaration in any form compose's dotenv reader and `set -a; .` accept (leading space,
# `export `, spaces before `=`)? Sets kv to the raw value. A missed form would count as "no key" and the
# appended generated key would supersede the real one.
key_line_value() {
  local l="$1"
  l="${l#"${l%%[![:space:]]*}"}"
  case "$l" in export[[:space:]]*) l="${l#export}"; l="${l#"${l%%[![:space:]]*}"}" ;; esac
  case "$l" in "$KEYVAR"=*|"$KEYVAR"[[:space:]]*) ;; *) return 1 ;; esac
  l="${l#"$KEYVAR"}"; l="${l#"${l%%[![:space:]]*}"}"
  case "$l" in =*) kv="${l#=}"; return 0 ;; esac
  return 1
}
if [ -f .env.local ]; then
  KEYVAR=HC_HINDSIGHT_API_KEY
  total=0; bad=0
  while IFS= read -r line || [ -n "$line" ]; do
    if key_line_value "$line"; then
      total=$((total + 1)); if is_placeholder_key "$kv"; then bad=$((bad + 1)); fi
    fi
  done < .env.local
  if [ "$total" -gt 0 ] && [ "$bad" -gt 0 ] && [ "$bad" -lt "$total" ]; then
    echo "seed-demo: ERROR .env.local has $total $KEYVAR lines, $bad of them empty/placeholder - keep exactly one real key line and rerun" >&2
    exit 1
  fi
  if [ "$total" -eq 0 ] || [ "$bad" -eq "$total" ]; then
    KEY="hs_$(rand 24)"
    out=""
    while IFS= read -r line || [ -n "$line" ]; do
      if key_line_value "$line"; then line="$KEYVAR=$KEY"; fi
      out="$out$line"$'\n'
    done < .env.local
    [ "$total" -eq 0 ] && out="$out$KEYVAR=$KEY"$'\n'
    # Atomic rewrite: a same-directory temp file (mktemp creates it 0600) replaces .env.local only after it
    # is fully written, so ENOSPC, a signal or an I/O error leaves the original file as it was.
    tmp="$(mktemp .env.local.XXXXXX 2>/dev/null)" || tmp=""
    if [ -z "$tmp" ] || ! printf '%s' "$out" > "$tmp" || ! chmod 600 "$tmp" || ! mv -f "$tmp" .env.local; then
      [ -n "$tmp" ] && rm -f "$tmp"
      echo "seed-demo: ERROR could not rewrite .env.local (left as it was) - check free space and permissions" >&2
      exit 1
    fi
    if [ "$total" -eq 0 ]; then
      echo "seed-demo: $KEYVAR was missing from .env.local - generated one (value not shown)"
    else
      echo "seed-demo: $KEYVAR in .env.local was empty or a placeholder - generated one (value not shown)"
    fi
  fi
fi

# Non-destructive backfill of model keys. docker-compose.yml deliberately declares
# NO HINDSIGHT_API_* model defaults (an environment: interpolation default silently
# outranks env_file and caused a split-brain - see the comment there), so .env.local is
# the only source. A hand-made .env.local (e.g. `cp .env.example .env.local`) would
# otherwise boot with upstream-default embeddings, i.e. a silent vector-dimension change.
# Existing values are never overwritten; only absent keys are appended.
if [ -f .env.local ]; then
  PROFILE="$(grep -E '^HC_PROFILE=' .env.local | head -1 | cut -d= -f2-)"
  PROFILE="${PROFILE:-${HC_PROFILE:-ko-full}}"
  if [ -f "profiles/$PROFILE.env" ]; then
    # A hand-edited .env.local may lack a trailing newline; appending onto it would
    # splice the first backfilled key onto the last existing line.
    [ -n "$(tail -c1 .env.local 2>/dev/null)" ] && echo >> .env.local
    added=0
    while IFS= read -r line; do
      case "$line" in
        HINDSIGHT_API_*|HC_EMBED_DIM=*) ;;
        *) continue ;;
      esac
      k="${line%%=*}"
      if ! grep -qE "^${k}=" .env.local; then
        printf '%s\n' "$line" >> .env.local
        added=$((added + 1))
      fi
    done < "profiles/$PROFILE.env"
    [ "$added" -gt 0 ] && echo "seed-demo: backfilled $added missing model key(s) from profiles/$PROFILE.env"
  else
    echo "seed-demo: WARNING profiles/$PROFILE.env not found - model keys not backfilled" >&2
  fi
fi

if [ ! -f secrets/acl.json ]; then
  TOK_ALICE="mt_$(rand 20)"
  TOK_BOB="mt_$(rand 20)"
  cat > secrets/acl.json <<EOF
{
  "tokens": {
    "$TOK_ALICE": {"identity": "alice", "personal": "personal-alice", "teams": ["team-eng"]},
    "$TOK_BOB":   {"identity": "bob",   "personal": "personal-bob",   "teams": ["team-eng"]}
  }
}
EOF
  # stash the demo tokens for verify gates (gitignored secrets dir)
  cat > secrets/demo-tokens.env <<EOF
HC_DEMO_TOKEN_ALICE=$TOK_ALICE
HC_DEMO_TOKEN_BOB=$TOK_BOB
EOF
  echo "seed-demo: wrote secrets/acl.json (alice, bob; team-eng) + demo-tokens.env"
else
  echo "seed-demo: secrets/acl.json exists — left untouched"
fi
