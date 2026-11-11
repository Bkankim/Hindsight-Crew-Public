#!/usr/bin/env bash
# Fetch the PINNED linux-arm64 Claude Code CLI that docker-compose.yml bind-mounts over the
# Agent SDK's bundled binary inside the hindsight image.
#
# Why: the image bundles CLI 2.1.150 (claude-agent-sdk 0.2.87), which the API refuses for
# claude-opus-5-5 ("version 2.1.280 or newer is required", measured 2026-09-26). The SDK
# prefers its bundled binary over PATH, so replacing that file is the only seam.
# Bumping: change VERSION + INTEGRITY together (pnpm view @anthropic-ai/claude-code-linux-arm64@<v> dist.integrity).
set -euo pipefail
VERSION=2.1.282
INTEGRITY="sha512-H2hNFmAZFVwVroTEBrnw7h3dYiGeZcVT5Y8dpamxq2kyuwZ9gNgFSrteyLXmvdxnmb3TBuig7AqxQR1JvPghvw=="
URL="https://registry.npmjs.org/@anthropic-ai/claude-code-linux-arm64/-/claude-code-linux-arm64-${VERSION}.tgz"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$ROOT/vendor/claude-cli/claude"   # gitignored; path is referenced by docker-compose.yml
STAMP="$ROOT/vendor/claude-cli/VERSION"

if [ -x "$DEST" ] && [ "$(cat "$STAMP" 2>/dev/null)" = "$VERSION" ]; then
  echo "claude-cli $VERSION already present"; exit 0
fi

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
curl -fsSL "$URL" -o "$tmp/pkg.tgz"
got="sha512-$(openssl dgst -sha512 -binary "$tmp/pkg.tgz" | openssl base64 -A)"
[ "$got" = "$INTEGRITY" ] || { echo "!! integrity mismatch for $URL" >&2; exit 1; }

tar -xzf "$tmp/pkg.tgz" -C "$tmp"
bin="$(find "$tmp/package" -type f -name claude | head -1)"
[ -n "$bin" ] || { echo "!! no claude binary in package" >&2; exit 1; }
mkdir -p "$(dirname "$DEST")"
install -m 0755 "$bin" "$DEST"
echo "$VERSION" > "$STAMP"
echo "claude-cli $VERSION -> $DEST"
