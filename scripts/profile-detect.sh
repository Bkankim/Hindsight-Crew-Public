#!/usr/bin/env bash
# profile-detect.sh — pick the embedding/model profile, LOCKED at boot-0.
#
# DEFAULT = ko-full (Korean). The embedding model IS the vector dimension, so the profile
# must be chosen BEFORE any data is loaded — switching later (e.g. en<->ko) forces a full
# reindex/wipe (bge-small-en=384d vs bge-m3=1024d). Hence: pick once, up front.
#   (default)   -> ko-full  (BAAI/bge-m3 + dragonkue/bge-reranker-v2-m3-ko; ~2.3GB, needs ~4GB+ RAM)
#   --cpu-en    -> cpu-en   (lightweight English, ~217MB; CI/constrained-host fallback)
#   --gpu       -> gpu      (Korean models via TEI; needs CUDA GPU)
#   --mac-mps   -> ko-full-mac-mps (ko-full embeddings in-container; reranking on the host
#                 Apple Silicon MPS sidecar via TEI. Same 1024d vectors as ko-full, so this
#                 is NOT a dimension change and needs no reindex. Requires rerank-server/ and
#                 the $HC_LAUNCHD_PREFIX.hindsight-rerank LaunchAgent - see rerank-server/launchd/README.md)
#   --profile=X -> X
# Prints the chosen profile. NEVER silently downgrades on low RAM (a silent en<->ko switch
# across hosts would corrupt dim consistency) — it warns loudly and proceeds.
set -eu
prof="ko-full"
# macOS + Apple Silicon with the sidecar already listening: say so loudly instead of
# silently seeding the ~25x slower in-container CPU reranker. Still opt-in via flag,
# because the profile is a boot-0 decision and must not change under the operator.
if [ "$(uname -s 2>/dev/null)" = "Darwin" ] && [ "$(uname -m 2>/dev/null)" = "arm64" ]; then
  if curl -s -m 2 -o /dev/null http://127.0.0.1:8787/health 2>/dev/null; then
    echo "profile-detect: NOTE a rerank sidecar is live on 127.0.0.1:8787 (Apple Silicon)." >&2
    echo "                './bootstrap --mac-mps' selects ko-full-mac-mps; plain ko-full would" >&2
    echo "                seed the in-container CPU reranker instead (~25x slower recall)." >&2
  fi
fi
for a in "$@"; do
  case "$a" in
    --cpu-en|--light|--en) prof="cpu-en" ;;
    --korean|--ko)         prof="ko-full" ;;
    --gpu)                 prof="gpu" ;;
    --mac-mps)             prof="ko-full-mac-mps" ;;
    --profile=*)           prof="${a#--profile=}" ;;
  esac
done

# Free-RAM check for the heavy (Korean/GPU) profiles — warn, do not auto-switch.
free_mb() {
  # Docker VM total as a proxy when available; else host best-effort.
  docker info --format '{{.MemTotal}}' 2>/dev/null | awk '{printf "%d", $1/1048576}' 2>/dev/null || echo 0
}
if [ "$prof" = "ko-full" ] || [ "$prof" = "ko-full-mac-mps" ] || [ "$prof" = "gpu" ]; then
  mb="$(free_mb)"
  if [ "$mb" -gt 0 ] && [ "$mb" -lt 3500 ]; then
    echo "profile-detect: WARNING '$prof' loads multi-GB models but the Docker VM has only ~${mb}MB RAM." >&2
    echo "                It will likely OOM. Bump the Docker VM memory (>=4GB), or run './bootstrap --cpu-en'" >&2
    echo "                for the lightweight English profile. (Profile is locked at boot-0; switching later = full reindex.)" >&2
  fi
fi
if [ "$prof" = "gpu" ] && ! docker info 2>/dev/null | grep -qi nvidia; then
  echo "profile-detect: WARNING gpu profile requested but no NVIDIA runtime detected; boot may fail" >&2
fi
echo "$prof"
