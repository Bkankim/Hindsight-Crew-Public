#!/usr/bin/env bash
# secret-hygiene.sh — block secret leakage in a PUBLIC repo.
# Use as a pre-commit hook (scans staged files) or standalone (scans tracked files).
#   pre-commit:   scripts/secret-hygiene.sh --staged
#   full scan:    scripts/secret-hygiene.sh
# Exit 0 = clean, 1 = potential secret found (commit should be blocked).
# Findings print file:line and the check kind only - never the matched text (issue #471).
# --staged reads the INDEX blob (`git show :<path>`), i.e. what the commit will record, not the
# working tree: staging a secret and then tidying or deleting the working copy must still block.
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

mode="${1:-full}"
staged=0; [ "$mode" = "--staged" ] && staged=1
# Portable file collection (bash 3.2 compatible - no mapfile/readarray). NUL-separated (-z), so a path
# with spaces, tabs or non-ASCII bytes arrives verbatim instead of core.quotepath's quoted form.
# --staged reads `diff --raw -z` to also get each entry's index mode (160000 = submodule, no blob);
# --no-renames turns a staged rename into an add, so the renamed file's content is scanned too;
# T (type change, e.g. symlink -> regular file) is included because the commit records that content.
# A listing git cannot produce (not a repo, unreadable index, refused safe.directory) is a failure, not
# "no files to scan": the loops below would see an empty list and report clean.
if [ "$staged" = 1 ]; then lister=(git diff --cached --raw -z --no-renames --diff-filter=ACMT); else lister=(git ls-files -z); fi
"${lister[@]}" >/dev/null 2>&1 || { echo "secret-hygiene: ERROR git could not list the files to scan - not a git repository or unreadable index. Commit blocked."; exit 1; }
files=(); modes=()
if [ "$staged" = 1 ]; then
  while IFS= read -r -d '' meta && IFS= read -r -d '' f; do
    set -- $meta                      # ":<old mode> <new mode> <old sha> <new sha> <status>"
    files+=("$f"); modes+=("$2")
  done < <(git diff --cached --raw -z --no-renames --diff-filter=ACMT 2>/dev/null)
else
  while IFS= read -r -d '' f; do
    [ -n "$f" ] && { files+=("$f"); modes+=(""); }
  done < <(git ls-files -z 2>/dev/null)
fi
[ "${#files[@]}" -eq 0 ] && { echo "secret-hygiene: no files to scan"; exit 0; }

declare -i hits=0
flag() { echo "  LEAK[$1]: $2"; hits+=1; }

# 1) Files that must NEVER be tracked.
for f in "${files[@]}"; do
  case "$f" in
    .env.local|*.local|secrets/*|*.key|*.pem|audit*.log)
      flag "forbidden-path" "$f (must be gitignored, never committed)";;
  esac
done

# 2) Content patterns. .env.example is allowed to contain REPLACE_* placeholders only.
# The content the scan judges: the index blob in --staged mode, otherwise the working-tree file.
# ":0:<path>" is the stage-0 index entry, read literally (a "1:x" file name is not taken as stage 1).
# A staged blob that cannot be read is a finding, not a skip: the commit would record content nobody saw.
has_content() {
  if [ "$staged" = 1 ]; then
    [ "$2" = "160000" ] && return 1
    git cat-file -e ":0:$1" 2>/dev/null && return 0
    flag "unreadable" "$1 (staged blob could not be read)"
    return 1
  fi
  [ -f "$1" ]
}
content() {
  if [ "$staged" = 1 ]; then git cat-file blob ":0:$1" 2>/dev/null; else cat -- "$1" 2>/dev/null; fi
}
# grep -n prefixes "N:". -a: a NUL or non-UTF-8 byte must not turn a match into a "binary file matches"
# notice (GNU grep >= 3.5 sends it to stderr, which is discarded) - the line number is all that is printed.
lineno() { case "$1" in ''|*[!0-9]*) echo "binary";; *) echo "$1";; esac; }
scan() {
  local f="$1" hit n line
  has_content "$f" "${2:-}" || return 0
  # Private keys
  while IFS= read -r hit; do
    flag "private-key" "$f:$(lineno "${hit%%:*}")"
  done < <(content "$f" | grep -aEn -- '-----BEGIN (RSA|OPENSSH|EC|DSA|PGP) PRIVATE KEY-----' 2>/dev/null)
  # Hindsight bearer / generic high-entropy assignments with a real-looking value (`=` or `:` form)
  while IFS= read -r hit; do
    n="${hit%%:*}"; line="${hit#*:}"
    # skip obvious placeholders
    case "$line" in *REPLACE*|*AUTOSEED*|*example*|*EXAMPLE*|*your-*|*xxxx*) continue;; esac
    flag "kv-secret" "$f:$(lineno "$n")"
  done < <(content "$f" | grep -aEn -- '(API_KEY|SECRET|TOKEN|BEARER|PASSWORD|PASSWD)[A-Z_]*\s*[:=]\s*[A-Za-z0-9_/+\-]{16,}' 2>/dev/null)
}
i=0
while [ "$i" -lt "${#files[@]}" ]; do
  f="${files[$i]}"; m="${modes[$i]}"; i=$((i + 1))
  case "$f" in *.env.example) continue;; esac   # template is allowed placeholders
  scan "$f" "$m"
done

if [ "$hits" -gt 0 ]; then
  echo "secret-hygiene: FAIL — $hits potential secret(s). Commit blocked."
  exit 1
fi
echo "secret-hygiene: clean (${#files[@]} files scanned)"
exit 0
