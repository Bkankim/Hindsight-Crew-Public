# hc-python.sh - sourced by host scripts (launchd jobs, verify gates); sets PY to a python3.
# Lookup order (issue #185): HC_PYTHON (used alone when set and non-empty; a path, or a bare name
# resolved with `command -v`; a bad value is a failure, never a fallthrough) > `command -v python3` > the common install locations below,
# which launchd's minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin) does not carry.
# The python3 in /usr/bin is only an Xcode CLT shim on a Mac without the tools: running it pops
# an install dialog and then fails silently under launchd, so it is skipped while
# `xcode-select -p` fails.
# Usage: . "$ROOT/scripts/hc-python.sh"; hc_find_python || { <log a failure token>; exit 1; }
# bash 3.2 compatible; does not change shell options.

hc_find_python() {
  PY=""
  local c
  if [ -n "${HC_PYTHON:-}" ]; then
    c="$HC_PYTHON"
    case "$c" in */*) ;; *) c="$(command -v "$c" 2>/dev/null)" || return 1 ;; esac
    if [ -x "$c" ] && [ -f "$c" ]; then PY="$c"; return 0; fi
    return 1
  fi
  if c="$(command -v python3 2>/dev/null)" && [ -x "$c" ] && _hc_python_usable "$c"; then
    PY="$c"; return 0
  fi
  for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 "${HOME:-}/.local/bin/python3"; do
    if [ -x "$c" ] && [ -f "$c" ] && _hc_python_usable "$c"; then PY="$c"; return 0; fi
  done
  return 1
}

# The CLT shim is the python3 in /usr/bin (what a minimal launchd PATH resolves first).
_hc_python_usable() {
  [ "${1%/*}" = /usr/bin ] || return 0
  command -v xcode-select >/dev/null 2>&1 || return 0   # not macOS: a real python3
  xcode-select -p >/dev/null 2>&1
}
