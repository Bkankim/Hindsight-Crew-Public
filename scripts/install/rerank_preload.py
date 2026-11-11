#!/usr/bin/env python3
"""Rerank model preload helper for `./install` (issue #215). Runs inside rerank-server's uv environment
(`uv run --project rerank-server python scripts/install/rerank_preload.py ...`), where huggingface_hub comes
with sentence-transformers. Never imports server.py.

    rerank_preload.py check    <model> <revision>   exit 0 when the pinned revision is fully in the local cache,
                                                    1 when a file is missing, 3 on any other error
    rerank_preload.py download <model> <revision>   fetch that revision (the one online step of the install);
                                                    exit 4 when it returned but the cache is still incomplete

The sidecar runs with HF_HUB_OFFLINE=1, so "cached" means every file it needs to load the model offline can be
found by `hf_hub_download(..., local_files_only=True)`. `snapshot_download(local_files_only=True)` is not used for
the check: with no tree cache it passes a partial cache.
"""
import sys

# What CrossEncoder(model, revision=...) reads for bge-reranker-v2-m3 (XLM-R sequence classification).
REQUIRED_FILES = (
    "config.json",
    "model.safetensors",
    "tokenizer_config.json",
    "tokenizer.json",
    "sentencepiece.bpe.model",
    "special_tokens_map.json",
)


def is_missing(e):
    """hf_hub_download(local_files_only=True) on a file that is not cached. LocalEntryNotFoundError is a
    FileNotFoundError in current hubs; the name check covers a version where it is not."""
    return isinstance(e, FileNotFoundError) or type(e).__name__ in ("LocalEntryNotFoundError", "EntryNotFoundError")


def cached(model, revision):
    from huggingface_hub import hf_hub_download
    for name in REQUIRED_FILES:
        try:
            hf_hub_download(repo_id=model, filename=name, revision=revision, local_files_only=True)
        except Exception as e:
            if not is_missing(e):
                raise   # a broken environment is not a cache miss
            print("rerank-preload: %s not cached" % name)
            return False
    return True


def describe(e):
    lines = str(e).splitlines()
    return "%s: %s" % (type(e).__name__, lines[0][:200] if lines else "")


def main(argv):
    if len(argv) != 3 or argv[0] not in ("check", "download"):
        print(__doc__, file=sys.stderr)
        return 2
    mode, model, revision = argv
    if mode == "check":
        try:
            return 0 if cached(model, revision) else 1
        except Exception as e:   # the dry run reports 3 as "not checked"; a real run downloads anyway
            print("rerank-preload: check error: " + describe(e))
            return 3
    from huggingface_hub import snapshot_download
    snapshot_download(repo_id=model, revision=revision)
    # snapshot_download can return an incomplete local snapshot instead of raising when the hub is unreachable
    try:
        complete = cached(model, revision)
    except Exception as e:
        print("rerank-preload: re-check error: " + describe(e))
        return 4
    if not complete:
        print("rerank-preload: download returned but the cache is still incomplete")
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
