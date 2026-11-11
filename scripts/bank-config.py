#!/usr/bin/env python3
"""bank-config.py - compare a bank's live config with the tracked template, or apply the difference (issue #286).

  bank-config.py [diff] --bank <id>                  read only; exit 0 = live equals template, 3 = any `change` or `unmanaged`
  bank-config.py apply  --bank <id> [--allow-prod]   PATCH the differing template keys, re-read, require diff 0

Template = templates/bank-config/base.json (tracked) with templates/bank-config/local/<bank>.json (gitignored,
the private per-bank values) laid over it, key by key. Both follow the `version: "1"` / `bank` shape of upstream
Bank Templates; only the `bank` clause is accepted here (mental models are #251). A key the template does not name
inherits the server default and is never sent, so an upgrade still brings the new default.

diff output, one line per key, template keys first:
  same <key>                              live value equals the template
  change <key>: <live> -> <template>      differs (exit 3; `apply` sends exactly these keys)
  server-default <key>                    not in the template and not overridden on the bank
  unmanaged <key>: <live>                 not in the template but overridden on the bank (exit 3, so exit 0 means the
                                          template covers every override; apply never sends or removes it)

Target (the #242 contract, same as scripts/backup.sh): HC_TARGET=next marks the hc-next copy, HC_ENV_FILE names the
env file, and HC_COMPOSE_PROJECT / HC_HINDSIGHT_CONTAINER / HC_GATEWAY_BIND / HC_PG0_VOLUME set in the process
beat the file. Standard call for the copy: `next_env=$(bash scripts/next-stack.sh env) || exit 1; eval "$next_env"`.
One `target:` line goes to stderr. `apply` on anything that is not the copy needs --allow-prod.

The upstream API is reached from INSIDE the hindsight container (scripts/bank_stats.py style); the API key
(HC_HINDSIGHT_API_KEY) and the request go in on stdin, never on a command line.
"""
import argparse
import json
import os
import re
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import hc_host_env  # noqa: E402  (same directory, standard library only)

ROOT = os.path.dirname(_HERE)
BANK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
TARGET_KEYS = ("HC_GATEWAY_BIND", "HC_PG0_VOLUME", "HC_HINDSIGHT_CONTAINER", "HC_COMPOSE_PROJECT", "HC_COMPOSE_FILES")
COPY_PROJECT, COPY_GATEWAY, COPY_PG0 = "hc-next", "127.0.0.1:18888", "hc-next_pg0"

# Runs inside the hindsight container. stdin: {"key", "method", "path", "body"?, "base"?}; stdout: {"status", "body"}.
CONTAINER_SCRIPT = r'''
import json, sys, urllib.error, urllib.request
req = json.load(sys.stdin)
data = None if req.get("body") is None else json.dumps(req["body"]).encode()
r = urllib.request.Request((req.get("base") or "http://localhost:8888") + req["path"], data=data, method=req["method"],
                           headers={"Authorization": "Bearer " + req["key"], "Content-Type": "application/json"})
try:
    resp = urllib.request.urlopen(r, timeout=30)
    status, raw = resp.status, resp.read()
except urllib.error.HTTPError as e:
    status, raw = e.code, e.read()
print(json.dumps({"status": status, "body": raw.decode("utf-8", "replace")}))
'''


class Fail(Exception):
    """A refusal or a failed step: message to stderr, exit 1."""


def canon(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


# ---------------------------------------------------------------- template


def load_manifest(path, label):
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except OSError as e:
        raise Fail("%s: cannot read %s: %s" % (label, path, e.strerror))
    except ValueError as e:
        raise Fail("%s: %s is not valid JSON: %s" % (label, path, e))
    if not isinstance(doc, dict) or doc.get("version") != "1":
        raise Fail('%s: %s must be an object with version "1"' % (label, path))
    extra = sorted(set(doc) - {"version", "bank"})
    if extra:
        raise Fail("%s: %s has sections this tool does not manage: %s (only `bank`)" % (label, path, ", ".join(extra)))
    if not isinstance(doc.get("bank"), dict):
        raise Fail("%s: %s needs a `bank` object" % (label, path))
    return doc["bank"]


def load_template(bank, template_path, overlay_path):
    merged = dict(load_manifest(template_path, "template"))
    overlay = overlay_path or os.path.join(ROOT, "templates", "bank-config", "local", bank + ".json")
    if os.path.exists(overlay):
        merged.update(load_manifest(overlay, "overlay"))
    elif overlay_path:
        raise Fail("overlay: %s does not exist" % overlay_path)
    else:
        print("bank-config: warning: no overlay templates/bank-config/local/%s.json - comparing against base only; "
              "overrides this bank has will show as unmanaged" % bank, file=sys.stderr)
    return merged


# ---------------------------------------------------------------- target


def parse_env(text):
    """HC_* settings plus COMPOSE_PROJECT_NAME, a repeated key keeping its LAST value as `set -a; . file` and docker
    compose do (hc_host_env.parse alone keeps the first non-empty one, which can pick a stale key after a rotation)."""
    values = {}
    for raw in text.splitlines():
        values.update(hc_host_env.parse(raw))
        m = re.match(r"^\s*(?:export\s+)?COMPOSE_PROJECT_NAME\s*=\s*(.*?)\s*$", raw)
        if m:
            v = m.group(1)
            values["COMPOSE_PROJECT_NAME"] = v[1:-1] if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'" else v
    return values


def read_env_file():
    explicit = "HC_ENV_FILE" in os.environ and os.environ["HC_ENV_FILE"] != ""
    path = os.environ["HC_ENV_FILE"] if explicit else os.path.join(ROOT, ".env.local")
    if not os.path.isabs(path):
        path = os.path.join(ROOT, path)
    if not os.path.isfile(path):
        if explicit:
            raise Fail("HC_ENV_FILE %s does not exist" % path)
        return path, {}
    try:
        with open(path, encoding="utf-8") as fh:
            return path, parse_env(fh.read())
    except (OSError, UnicodeDecodeError) as e:
        raise Fail("cannot read HC_ENV_FILE %s: %s" % (path, e))


def run_docker(args):
    try:
        return subprocess.run(["docker"] + args, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        raise Fail("docker %s failed: %s" % (args[0], e))


def resolve_container(env_path, file_env, setting):
    if setting("HC_HINDSIGHT_CONTAINER"):
        return setting("HC_HINDSIGHT_CONTAINER")
    cmd = ["compose"]
    if os.path.isfile(env_path):
        cmd += ["--env-file", env_path]
    cmd += ["--project-directory", ROOT]
    if setting("HC_COMPOSE_PROJECT"):
        cmd += ["-p", setting("HC_COMPOSE_PROJECT")]
    for f in (setting("HC_COMPOSE_FILES") or "docker-compose.yml").split(":"):
        if f:
            cmd += ["-f", f if os.path.isabs(f) else os.path.join(ROOT, f)]
    ps = run_docker(cmd + ["ps", "-q", "hindsight"])
    cid = ps.stdout.split()[0] if ps.returncode == 0 and ps.stdout.split() else ""
    if not cid:
        return "-"
    named = run_docker(["inspect", "-f", "{{.Name}}", cid])
    name = named.stdout.strip().lstrip("/") if named.returncode == 0 else ""
    return name or "-"


def resolve_target():
    """Returns (container, is_copy, key). Prints the single `target:` line; raises Fail on a guard."""
    env_path, file_env = read_env_file()

    def setting(name):
        # a variable set in the process (even empty) beats the env file, as in scripts/backup.sh
        return os.environ[name] if name in os.environ else file_env.get(name, "")

    container = resolve_container(env_path, file_env, setting)
    project = setting("HC_COMPOSE_PROJECT") or os.environ.get("COMPOSE_PROJECT_NAME") or file_env.get(
        "COMPOSE_PROJECT_NAME") or re.sub(r"^[-_]+", "", re.sub(r"[^a-z0-9_-]", "", os.path.basename(ROOT).lower()))
    gateway, pg0 = setting("HC_GATEWAY_BIND") or "-", setting("HC_PG0_VOLUME") or "-"
    print("target: gw=%s project=%s container=%s pg0=%s" % (gateway, project, container, pg0), file=sys.stderr)
    sys.stderr.flush()

    copy_cells = {
        "project %s" % project: project.startswith("hc-next"),
        "container %s" % container: container.startswith("hc-next"),
        "gateway %s" % gateway: gateway.endswith(":18888"),
        "pg0 volume %s" % pg0: pg0.startswith("hc-next"),
    }
    if os.environ.get("HC_TARGET") == "next":
        if project != COPY_PROJECT:
            raise Fail("HC_TARGET=next but project is %s, not %s" % (project, COPY_PROJECT))
        if not container.startswith("hc-next-"):
            raise Fail("HC_TARGET=next but container is %s, not hc-next-*" % container)
        if gateway not in ("-", COPY_GATEWAY):
            raise Fail("HC_TARGET=next but gateway is %s, not %s" % (gateway, COPY_GATEWAY))
        if pg0 not in ("-", COPY_PG0):
            raise Fail("HC_TARGET=next but pg0 volume is %s, not %s" % (pg0, COPY_PG0))
        is_copy = True
    else:
        looks_like_copy = [cell for cell, hit in copy_cells.items() if hit]
        if looks_like_copy:
            raise Fail("%s looks like the hc-next copy but HC_TARGET is not next - set HC_TARGET=next (standard call: "
                       "next_env=$(bash scripts/next-stack.sh env) || exit 1; eval \"$next_env\")" % looks_like_copy[0])
        is_copy = False
    if container == "-":
        raise Fail("no running hindsight container for this target")

    key = file_env.get("HC_HINDSIGHT_API_KEY") or os.environ.get("HC_HINDSIGHT_API_KEY", "")
    if not key:
        raise Fail("HC_HINDSIGHT_API_KEY is not set in the process or in %s" % env_path)
    return container, is_copy, key


# ---------------------------------------------------------------- upstream


def upstream(container, key, method, path, body=None):
    request = {"key": key, "method": method, "path": path}
    if body is not None:
        request["body"] = body
    try:
        p = subprocess.run(["docker", "exec", "-i", container, "python3", "-c", CONTAINER_SCRIPT],
                           input=json.dumps(request), capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        raise Fail("docker exec into %s failed for %s %s: %s" % (container, method, path, e))
    if p.returncode != 0:
        raise Fail("docker exec into %s failed (exit %d): %s" % (container, p.returncode, p.stderr.strip()[-300:]))
    try:
        reply = json.loads(p.stdout)
        status, text = reply["status"], reply["body"]
    except (ValueError, KeyError, TypeError):
        raise Fail("unreadable reply from the container for %s %s" % (method, path))
    if not 200 <= status < 300:
        raise Fail("%s %s -> HTTP %s: %s" % (method, path, status, text[:300]))
    if not text.strip():
        return {}                       # a write that answers 2xx with no body is still a success; the caller re-reads
    try:
        return json.loads(text)
    except ValueError:
        raise Fail("%s %s -> HTTP %s but the body is not JSON" % (method, path, status))


def config_path(bank):
    return "/v1/default/banks/%s/config" % bank


# ---------------------------------------------------------------- diff


def compare(template, current):
    """-> (lines, changed keys, unmanaged keys). `current` is the GET reply: resolved `config` plus the bank's own
    `overrides`."""
    config, overrides = current.get("config") or {}, current.get("overrides") or {}
    lines, changed, unmanaged = [], {}, []
    for key in sorted(template):
        want = template[key]
        if key in config and canon(config[key]) == canon(want):
            lines.append("same %s" % key)
        else:
            lines.append("change %s: %s -> %s" % (key, canon(config[key]) if key in config else "(absent)", canon(want)))
            changed[key] = want
    for key in sorted(set(config) - set(template)):
        if key in overrides:
            lines.append("unmanaged %s: %s" % (key, canon(config[key])))
            unmanaged.append(key)
        else:
            lines.append("server-default %s" % key)
    return lines, changed, unmanaged


def cmd_diff(template, container, key, bank):
    lines, changed, unmanaged = compare(template, upstream(container, key, "GET", config_path(bank)))
    print("\n".join(lines))
    return 3 if changed or unmanaged else 0


def cmd_apply(template, container, key, bank):
    lines, changed, _unmanaged = compare(template, upstream(container, key, "GET", config_path(bank)))
    if not changed:
        print("\n".join(lines))
        print("apply: no difference, nothing sent")
        return 0
    upstream(container, key, "PATCH", config_path(bank), {"updates": changed})
    print("patched: %s" % ", ".join(sorted(changed)))
    lines, still, _unmanaged = compare(template, upstream(container, key, "GET", config_path(bank)))
    print("\n".join(lines))
    if still:
        print("apply: the re-read still differs for: %s" % ", ".join(sorted(still)), file=sys.stderr)
        return 1
    return 0


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", nargs="?", default="diff", choices=("diff", "apply"))
    ap.add_argument("--bank", required=True, help="bank id")
    ap.add_argument("--template", help="base template (default templates/bank-config/base.json)")
    ap.add_argument("--overlay", help="per-bank overlay (default templates/bank-config/local/<bank>.json, optional)")
    ap.add_argument("--allow-prod", action="store_true", help="let `apply` write to a target that is not the copy")
    args = ap.parse_args(argv)
    try:
        if not BANK_ID.match(args.bank):
            raise Fail("--bank %r is not a plain bank id" % args.bank)
        template = load_template(args.bank, args.template or os.path.join(ROOT, "templates", "bank-config", "base.json"),
                                 args.overlay)
        container, is_copy, key = resolve_target()
        if args.command == "apply" and not is_copy and not args.allow_prod:
            raise Fail("apply on a target that is not the hc-next copy needs --allow-prod; nothing was sent")
        return (cmd_apply if args.command == "apply" else cmd_diff)(template, container, key, args.bank)
    except Fail as e:
        print("bank-config: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
