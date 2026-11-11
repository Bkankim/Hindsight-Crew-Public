"""Host install settings from `.env.host.local` (issue #186). Stdlib only, python 3.8+.

The file lives at the repository root, is gitignored (`.env.*.local`) and is read by host scripts
only - never by a container (`.env.local` is the compose env_file; one extra key there recreates
the hindsight container on the next `compose up`). Keys (all `HC_*`):

  HC_PERSONAL_BANK        the owner's bank id (required by the bank-bound scripts)
  HC_OWNER                the acl.json `identity` whose token the scripts use (required likewise)
  HC_LAUNCHD_PREFIX       LaunchAgent label prefix for `__PREFIX__` in the plist templates
  HC_NOTIFY_DISCORD_BOT_TOKEN / HC_NOTIFY_DISCORD_CHANNEL_ID   notification target

Lines are `KEY=VALUE` (optional `export `, `#` comment lines, one pair of matching quotes is
stripped; in an unquoted value, whitespace followed by `#` starts a comment). Other keys are ignored.
A non-empty process environment variable wins over the file ("environment" means exported: a plain,
unexported shell variable of the same name does not count).
scripts/hc-host-env.sh applies the same rules for the shell wrappers; tests/test_host_env.py runs
both on one fixture. A missing required key is never defaulted: `require()` raises ConfigMissing.
"""
import os
import re
from pathlib import Path
from typing import Dict, Optional

ROOT = Path(__file__).resolve().parent.parent
FILENAME = ".env.host.local"
_KEY = re.compile(r"^HC_[A-Z0-9_]+$")
_INLINE_COMMENT = re.compile(r"\s#")


class ConfigMissing(Exception):
    """A required host setting is empty or absent. `key` names it (log token: `config_missing <key>`)."""

    def __init__(self, key: str):
        super().__init__("config_missing " + key)
        self.key = key


class ConfigInvalid(ConfigMissing):
    """`.env.host.local` exists but cannot be read as UTF-8 text (log token: `config_invalid <file>`).
    A ConfigMissing subclass, so every caller that reports a missing key reports this too."""

    def __init__(self, path):
        Exception.__init__(self, "config_invalid %s (not valid UTF-8)" % path)
        self.key = str(path)


def parse(text: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not _KEY.match(key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif value[:1] not in ("\"", "'"):
            value = _INLINE_COMMENT.split(value, 1)[0].rstrip()   # `abc  # note` -> `abc`; `abc#x` is kept
        if not values.get(key):      # a repeated key: the first non-empty value stays (same as hc-host-env.sh)
            values[key] = value
    return values


def load(root: Optional[Path] = None) -> Dict[str, str]:
    """Values of `<root>/.env.host.local` (empty when the file is absent or unreadable)."""
    path = Path(root or ROOT) / FILENAME
    try:
        return parse(path.read_text(encoding="utf-8"))
    except OSError:
        return {}
    except UnicodeDecodeError:
        raise ConfigInvalid(path) from None


def get(key: str, root: Optional[Path] = None) -> Optional[str]:
    """Non-empty environment value, else non-empty file value, else None."""
    value = os.environ.get(key) or load(root).get(key)
    return value or None


def require(*keys: str, root: Optional[Path] = None) -> Dict[str, str]:
    """Every key's value, or ConfigMissing for the first one that is empty."""
    values = load(root)
    out: Dict[str, str] = {}
    for key in keys:
        value = os.environ.get(key) or values.get(key)
        if not value:
            raise ConfigMissing(key)
        out[key] = value
    return out
