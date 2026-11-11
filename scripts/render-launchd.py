#!/usr/bin/env python3
"""Render a LaunchAgent template (`*.plist.example`) into an installable plist (issue #185).

    render-launchd.py --template <plist.example> --out <plist> [--set KEY=VALUE ...]

Placeholders are `__NAME__` tokens. Defaults, computed only for the tokens the template uses:
  __REPO__  repository root (the parent of this script's directory)
  __HOME__  $HOME (an empty HOME is an error: nothing sensible can be derived from it)
  __UV__    `command -v uv`, made absolute
  __PREFIX__  the LaunchAgent label prefix: HC_LAUNCHD_PREFIX from the environment or from
            `.env.host.local` (issue #186). There is no default: an unset prefix fails the render
            with `config_missing HC_LAUNCHD_PREFIX`.
  __PATH__  `<P>/bin:<P>/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin` without duplicates, where
            P = dirname(dirname(__UV__)); the current shell PATH is deliberately not used, so the
            same uv location always renders the same agent environment.
`--set KEY=VALUE` (KEY or __KEY__) adds or overrides a token, e.g. `--set PREFIX=...` (#186).
`--set UV=/x/bin/uv` also drives the derived __PATH__ unless `--set PATH=...` is given too.
Any `__NAME__` token left unresolved fails the render, and so does an empty `--set` value. The result is parsed with `plistlib`
before it is written (atomically: a failed render never touches an existing `--out`). Values are
XML-escaped. Stdlib only; works on the system python3 (3.8+).
"""
from __future__ import annotations

import argparse
import os
import plistlib
import re
import shutil
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hc_host_env  # noqa: E402  (same directory, stdlib only)

REPO = Path(__file__).resolve().parent.parent
TOKEN = re.compile(r"__[A-Za-z][A-Za-z0-9_]*__")   # mixed case too: a typo like __Home__ must fail, not slip through
KEY = re.compile(r"_*([A-Z][A-Z0-9_]*?)_*$")
SYSTEM_PATH = ("/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")


class RenderError(Exception):
    pass


def agent_path(uv: str) -> str:
    """The __PATH__ rule from issue #185."""
    prefix = os.path.dirname(os.path.dirname(os.path.abspath(uv)))
    parts = [prefix + "/bin", prefix + "/sbin", *SYSTEM_PATH]
    return ":".join(dict.fromkeys(parts))


def parse_set(items: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in items:
        key, sep, value = item.partition("=")
        m = KEY.match(key)
        if not sep or not m:
            raise RenderError(f"--set expects KEY=VALUE with KEY like PREFIX or __PREFIX__, got {item!r}")
        if not value:
            raise RenderError(f"--set {key}= has an empty value (an empty path or name is never what the plist wants)")
        values["__%s__" % m.group(1)] = value
    return values


def build_values(used: set[str], overrides: dict[str, str]) -> dict[str, str]:
    """Defaults for the tokens in `used`, with `overrides` on top."""
    values = dict(overrides)
    if "__REPO__" in used and "__REPO__" not in values:
        values["__REPO__"] = str(REPO)
    if "__HOME__" in used and "__HOME__" not in values:
        home = os.environ.get("HOME", "")
        if not home:
            raise RenderError("HOME is empty; cannot fill __HOME__ (use --set HOME=... to override)")
        values["__HOME__"] = home
    if "__PREFIX__" in used and "__PREFIX__" not in values:
        try:
            prefix = hc_host_env.get("HC_LAUNCHD_PREFIX", REPO)
        except hc_host_env.ConfigMissing as e:   # config_invalid: unreadable .env.host.local
            raise RenderError(str(e))
        if not prefix:
            raise RenderError("config_missing HC_LAUNCHD_PREFIX (set it in .env.host.local or pass --set PREFIX=...)")
        values["__PREFIX__"] = prefix
    if used & {"__UV__", "__PATH__"} and "__UV__" not in values:
        uv = shutil.which("uv")
        if uv:
            # which() keeps a relative PATH entry as is; launchd would resolve it against the agent's own cwd
            values["__UV__"] = uv if os.path.isabs(uv) else os.path.abspath(uv)
        elif "__UV__" in used or "__PATH__" not in values:
            raise RenderError("uv not found on PATH; install uv or pass --set UV=/path/to/uv")
    if "__PATH__" in used and "__PATH__" not in values:
        values["__PATH__"] = agent_path(values["__UV__"])
    return values


def render(template: str, overrides: dict[str, str]) -> str:
    used = set(TOKEN.findall(template))
    values = build_values(used, overrides)
    missing = sorted(used - set(values))
    if missing:
        raise RenderError("unresolved placeholders: " + " ".join(missing))
    return TOKEN.sub(lambda m: escape(values[m.group(0)]), template)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Render a LaunchAgent plist template.")
    ap.add_argument("--template", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--set", dest="sets", action="append", default=[], metavar="KEY=VALUE")
    args = ap.parse_args(argv)
    try:
        rendered = render(args.template.read_text(encoding="utf-8"), parse_set(args.sets))
    except (OSError, RenderError) as e:
        print(f"render-launchd: {e}", file=sys.stderr)
        return 1
    try:
        plistlib.loads(rendered.encode("utf-8"))
    except Exception as e:  # noqa: BLE001 - plistlib raises expat/ValueError types by version
        print(f"render-launchd: rendered plist does not parse: {e}", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(args.out.parent), prefix=args.out.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(rendered)
        os.chmod(tmp, 0o644)
        os.replace(tmp, args.out)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
