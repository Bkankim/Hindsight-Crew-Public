# hc-host-env.sh - sourced by host scripts; reads the host install settings (issue #186).
# Usage: . "$ROOT/scripts/hc-host-env.sh"; hc_load_host_env "$ROOT"; hc_resolve_notify
# Same rules as scripts/hc_host_env.py (see its docstring for the keys): `<root>/.env.host.local`
# is parsed line by line, never sourced (it need not be shell-clean); only HC_* keys are read; a
# non-empty variable already in the environment wins over the file ("environment" = exported: a plain
# unexported shell variable of the same name does not count, as in hc_host_env.py). In an unquoted
# value, whitespace followed by `#` starts a comment. No key has a default.
# bash 3.2 and zsh compatible (the install loops source it from the operator's login shell); does not
# change shell options.

_hc_trim() { # value -> stdout, without leading/trailing whitespace
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

hc_load_host_env() { # repo-root
  local file="${1:-.}/.env.host.local" line key val
  [ -f "$file" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    line="$(_hc_trim "$line")"
    case "$line" in ''|'#'*) continue ;; esac
    case "$line" in 'export '*) line="$(_hc_trim "${line#export }")" ;; esac
    case "$line" in *=*) ;; *) continue ;; esac
    key="$(_hc_trim "${line%%=*}")"
    val="$(_hc_trim "${line#*=}")"
    case "$key" in HC_?*) ;; *) continue ;; esac
    case "$key" in *[!A-Z0-9_]*) continue ;; esac
    if [ "${#val}" -ge 2 ]; then
      case "$val" in
        \"*\") val="${val#\"}"; val="${val%\"}" ;;
        \'*\') val="${val#\'}"; val="${val%\'}" ;;
        \"*|\'*) ;;   # opens with a quote but is not a closed pair: kept as is (hc_host_env.py does the same)
        *) val="$(_hc_trim "${val%%[[:space:]]\#*}")" ;;
      esac
    fi
    [ -n "$(printenv "$key")" ] && continue   # not ${!key}: this file is also sourced from zsh (install loops in the READMEs)
    export "$key=$val"
  done < "$file"
  return 0
}

# Notification target, in this order: HC_NOTIFY_DISCORD_BOT_TOKEN + HC_NOTIFY_DISCORD_CHANNEL_ID
# (environment or .env.host.local, both needed) > the legacy GGG_DISCORD_BOT_TOKEN / _CHANNEL_ID
# lines of ~/.env (both needed; extracted line by line because that file is not shell-clean) >
# nothing, which leaves the HC_NOTIFY_* pair unset so the caller logs NOTIFY_FAILED missing_env.
hc_resolve_notify() {
  if [ -n "${HC_NOTIFY_DISCORD_BOT_TOKEN:-}" ] && [ -n "${HC_NOTIFY_DISCORD_CHANNEL_ID:-}" ]; then
    return 0
  fi
  local legacy="${HOME:-}/.env" bot chan
  [ -n "${HOME:-}" ] && [ -f "$legacy" ] || return 0
  bot="$(grep '^GGG_DISCORD_BOT_TOKEN=' "$legacy" | head -1 | cut -d= -f2- | tr -d '"')"
  chan="$(grep '^GGG_DISCORD_CHANNEL_ID=' "$legacy" | head -1 | cut -d= -f2- | tr -d '"')"
  if [ -n "$bot" ] && [ -n "$chan" ]; then
    export HC_NOTIFY_DISCORD_BOT_TOKEN="$bot" HC_NOTIFY_DISCORD_CHANNEL_ID="$chan"
  fi
  return 0
}
