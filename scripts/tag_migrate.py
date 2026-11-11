#!/usr/bin/env python3
"""Manifest-gated tag migration of the owner's personal bank (stdlib only).

Production configuration is injected by scripts/tag-migrate.sh from the policy SSoT; the bank id is
PERSONAL_BANK, injected from HC_PERSONAL_BANK (.env.host.local, issue #186) - there is no default.
Run this file directly only with --self-test.

Two modes do not use that configuration (issue #43): --apply-manifest executes a version 2 old->new
manifest planned on the host, and --export-labels prints {id,label} JSONL (read-only).
"""
import argparse
import contextlib
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

BASE_URL = "http://localhost:8888"
BACKFILL_ID = "fa3461ad-0166-4790-8baf-ab1c13b2dcb3"
BACKFILL_TAGS = ["kind:fact", "topic:memory-vault", "user:demo"]
VERIFICATION_BACKFILL_TAGS = ["kind:verification", "topic:memory-vault", "user:demo"]
BATCH_SIZE = 25
DRAIN_TIMEOUT = 20 * 60
PAGE_SIZE = 100
PERSONAL_BANK: Any = globals().get("PERSONAL_BANK")   # injected by the wrapper prelude
BANK_NAME = PERSONAL_BANK or os.environ.get("HC_PERSONAL_BANK") or ""   # the env only exists outside the container
BANK_PATH = "/v1/default/banks/" + BANK_NAME
_BANK_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
APPLY_MANIFEST_VERSION = 2
# 라벨 export 가 문서 상세 응답에서 본문을 찾는 후보 필드(앞이 우선). 운영 문서 테이블의 본문 열이
# documents.original_text 이고 업스트림 문서 응답이 그 이름을 따를 가능성이 높아 1순위로 둔다.
# 라이브로 확정하지 못했으므로 어느 것도 없으면 조용히 넘기지 않고 중단한다.
LABEL_BODY_FIELDS = ("original_text", "content", "text")
LABEL_PATTERN = re.compile(r"\[([^\]]{1,12})\]")

# The wrapper's prelude defines these before this source is concatenated. globals().get
# preserves those values while keeping direct --self-test execution possible.
ALIAS_MAP: Any = globals().get("ALIAS_MAP")
DELETE_TAGS: Any = globals().get("DELETE_TAGS")
REMOVE_TAGS: Any = globals().get("REMOVE_TAGS")
# manifest / id 목록 모드에서 tag-migrate.sh 가 호스트 파일을 JSON 문자열로 주입한다(정책 alias 는 주입하지 않는다).
INJECTED_INPUT_JSON: Any = globals().get("INJECTED_INPUT_JSON")


class MigrationError(RuntimeError):
    pass


class HttpTransport:
    def __init__(self, base_url, token, log=True):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.log = log

    def request(self, method, path, body=None, authenticated=True):
        headers = {"Accept": "application/json"}
        if authenticated and self.token:
            headers["Authorization"] = "Bearer " + self.token
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read().decode("utf-8", "replace")
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = exc.read().decode("utf-8", "replace")
        if self.log:
            print("HTTP %s %s -> %s %s" % (method, path, status, raw[:1000]))
        try:
            payload = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            payload = None
        return status, payload, raw


def require_configuration():
    missing = [
        name for name, value in (
            ("ALIAS_MAP", ALIAS_MAP), ("DELETE_TAGS", DELETE_TAGS), ("REMOVE_TAGS", REMOVE_TAGS)
        ) if value is None
    ]
    if missing:
        raise MigrationError("configuration prelude missing: " + ", ".join(missing) + "; use scripts/tag-migrate.sh")
    if not isinstance(ALIAS_MAP, dict) or not isinstance(DELETE_TAGS, (set, list, tuple)) or not isinstance(REMOVE_TAGS, (set, list, tuple)):
        raise MigrationError("invalid injected configuration types")


def slugify(raw):
    slug = re.sub(r"[^a-z0-9-]+", "-", raw.lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug


def transform_tags(tags, aliases, remove_tags):
    """Drop, normalize bare values, alias once, and stable-dedupe."""
    output = []
    seen = set()
    for tag in tags:
        if tag in remove_tags:
            continue
        fixed = tag
        if isinstance(tag, str) and ":" not in tag:
            slug = slugify(tag)
            fixed = "topic:" + slug if slug else tag
        if isinstance(fixed, str):
            fixed = aliases.get(fixed, fixed)
        if fixed not in seen:
            output.append(fixed)
            seen.add(fixed)
    return output


def plan_document(document, aliases, delete_tags, remove_tags):
    document_id = document.get("id", document.get("document_id"))
    if not document_id:
        raise MigrationError("document lacks id/document_id: %r" % document)
    raw_tags = document.get("tags")
    old_tags = list(raw_tags) if isinstance(raw_tags, (list, tuple)) else []
    if any(tag in delete_tags for tag in old_tags):
        return {"id": document_id, "old": old_tags, "new": [], "action": "DELETE"}
    if not old_tags:
        tags = BACKFILL_TAGS if document_id == BACKFILL_ID else VERIFICATION_BACKFILL_TAGS
        return {"id": document_id, "old": old_tags, "new": list(tags), "action": "BACKFILL"}
    new_tags = transform_tags(old_tags, aliases, remove_tags)
    if new_tags != old_tags:
        return {"id": document_id, "old": old_tags, "new": new_tags, "action": "RETAG"}
    return None


def parse_page(payload, key="items"):
    if not isinstance(payload, dict):
        raise MigrationError("page response is not an object")
    items = payload.get(key)
    for field in ("total", "limit", "offset"):
        if not isinstance(payload.get(field), int):
            raise MigrationError("page response lacks integer %s" % field)
    if not isinstance(items, list):
        raise MigrationError("page response lacks list %s" % key)
    return items, payload["total"]


def list_paginated(transport, path, key="items", failure_cap=False):
    accumulated = []
    offset = 0
    expected_total = None
    while expected_total is None or len(accumulated) < expected_total:
        separator = "&" if "?" in path else "?"
        page_path = "%s%slimit=%d&offset=%d" % (path, separator, PAGE_SIZE, offset)
        if failure_cap:
            payload = request_with_failure_cap(transport, "GET", page_path)
        else:
            status, payload, _ = transport.request("GET", page_path)
            if status != 200:
                raise MigrationError("list failed with HTTP %s" % status)
        items, total = parse_page(payload, key)
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise MigrationError("API total changed during pagination: %d -> %d" % (expected_total, total))
        if len(accumulated) + len(items) > expected_total:
            raise MigrationError("pagination returned more items than total")
        if not items and len(accumulated) != expected_total:
            raise MigrationError("pagination stopped before reported total")
        accumulated.extend(items)
        offset += len(items)
    if len(accumulated) != expected_total:
        raise MigrationError("scanned %d != API total %d" % (len(accumulated), expected_total))
    return accumulated, expected_total


def allowed_pipeline_outputs(old_tags, aliases, remove_tags):
    """Derive permitted output values without invoking the transformation under test."""
    allowed = set()
    for tag in old_tags:
        if tag in remove_tags:
            continue
        if isinstance(tag, str) and ":" not in tag:
            slug = slugify(tag)
            if slug:
                normalized = "topic:" + slug
                allowed.add(aliases.get(normalized, normalized))
        elif tag in aliases:
            allowed.add(aliases[tag])
        else:
            allowed.add(tag)
    return allowed


def count_pipeline_violations(changes, aliases, delete_tags, remove_tags):
    """Independently validate provenance and completeness for each emitted row.

    RETAG rows must match the expected output set exactly: a subset-only check
    would silently accept rows that *drop* tags the pipeline should keep.
    """
    violations = 0
    alias_keys = set(aliases)
    approved_deletes = set(delete_tags)
    for change in changes:
        old_tags = change["old"]
        new_tags = change["new"]
        if change["action"] == "DELETE":
            valid = bool(set(old_tags) & approved_deletes)
        elif change["action"] == "BACKFILL":
            expected = BACKFILL_TAGS if change["id"] == BACKFILL_ID else VERIFICATION_BACKFILL_TAGS
            valid = not old_tags and new_tags == expected
        elif change["action"] == "RETAG":
            expected = allowed_pipeline_outputs(old_tags, aliases, remove_tags)
            valid = set(new_tags) == expected and not (set(new_tags) & alias_keys)
        else:
            valid = False
        if not valid:
            violations += 1
    return violations


def make_plan(documents, aliases, delete_tags, remove_tags):
    changes = []
    for document in documents:
        change = plan_document(document, aliases, delete_tags, remove_tags)
        if change:
            changes.append(change)
    out_of_pipeline = count_pipeline_violations(changes, aliases, delete_tags, remove_tags)
    if out_of_pipeline:
        raise MigrationError("%d out-of-pipeline diffs" % out_of_pipeline)
    return changes, out_of_pipeline


def selected_actions(only):
    mapping = {"retag": "RETAG", "delete": "DELETE", "backfill": "BACKFILL"}
    if not only:
        return set(mapping.values())
    values = {value.strip().lower() for value in only.split(",") if value.strip()}
    unknown = values - set(mapping)
    if unknown:
        raise MigrationError("unknown --only action(s): " + ", ".join(sorted(unknown)))
    return {mapping[value] for value in values}


def manifest_for(changes, actions) -> dict[str, Any]:
    result: dict[str, Any] = {"version": 1, "bank": BANK_NAME, "actions": {}}
    for action in ("RETAG", "DELETE", "BACKFILL"):
        ids = [item["id"] for item in changes if item["action"] == action and action in actions]
        result["actions"][action] = {"count": len(ids), "ids": ids}
    result["total_changes"] = sum(entry["count"] for entry in result["actions"].values())
    return result


def print_plan(changes, actions, scanned, total, out_of_pipeline):
    for item in changes:
        if item["action"] in actions:
            print("%s | %s | %s | %s" % (
                item["id"], json.dumps(item["old"], ensure_ascii=False),
                json.dumps(item["new"], ensure_ascii=False), item["action"]))
    print("SUMMARY scanned=%d total=%d" % (scanned, total))
    for action in ("RETAG", "DELETE", "BACKFILL"):
        print("SUMMARY %-8s %d" % (action, sum(1 for item in changes if item["action"] == action and action in actions)))
    print("SUMMARY out_of_pipeline_diffs=%d" % out_of_pipeline)


def save_json(path, value):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


def load_json(path) -> Any:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def preflight(transport):
    if sys.version_info < (3, 11):
        raise MigrationError("Python 3.11+ required")
    status_auth, _, _ = transport.request("GET", BANK_PATH + "/tags")
    status_no_auth, _, _ = transport.request("GET", BANK_PATH + "/tags", authenticated=False)
    print("OBSERVATION auth_probe correct_token_status=%s no_token_status=%s" % (status_auth, status_no_auth))
    if status_auth != 200:
        raise MigrationError("authenticated preflight failed; no mutations attempted")


def request_with_failure_cap(transport, method, path, body=None, sleeper=time.sleep):
    """Retry only network/5xx failures; never blindly replay a rejected request."""
    last = None
    for attempt in range(1, 4):
        try:
            status, payload, raw = transport.request(method, path, body)
        except (OSError, urllib.error.URLError) as exc:
            last = repr(exc)
            print("HTTP %s %s -> transport failure %s" % (method, path, last))
        else:
            if 200 <= status < 300 or (method == "DELETE" and status == 404):
                return payload
            last = "HTTP %s: %s" % (status, raw[:500])
            if 400 <= status < 500:
                raise MigrationError("request rejected without retry: " + last)
            if status < 500:
                raise MigrationError("request failed without retry: " + last)
        if attempt == 3:
            break
        print("retryable HTTP failure %d/3" % attempt)
        sleeper(0.25 * attempt)
    raise MigrationError("request failed after 3 attempts: %s" % last)


def pending_consolidations(transport, status):
    path = BANK_PATH + "/operations?status=%s&type=consolidation" % urllib.parse.quote(status)
    operations, total = list_paginated(transport, path, key="operations", failure_cap=True)
    filtered = [op for op in operations if op.get("task_type", op.get("type")) == "consolidation"]
    if len(filtered) != total:
        raise MigrationError("operations endpoint returned non-consolidation rows despite type filter")
    return len(filtered)


def drain(transport, timeout=DRAIN_TIMEOUT, monotonic=time.monotonic, sleeper=time.sleep):
    deadline = monotonic() + timeout
    while True:
        pending = pending_consolidations(transport, "pending")
        processing = pending_consolidations(transport, "processing")
        print("DRAIN pending=%d processing=%d" % (pending, processing))
        if pending == 0 and processing == 0:
            return
        if monotonic() >= deadline:
            raise MigrationError("terminal drain timeout after %d seconds" % timeout)
        sleeper(min(2.0, max(0.0, deadline - monotonic())))


def validate_manifest(manifest: dict[str, Any], changes, actions) -> dict[str, Any]:
    if manifest.get("version") != 1 or manifest.get("bank") != BANK_NAME:
        raise MigrationError("manifest version/bank mismatch")
    planned = manifest_for(changes, actions)
    for action in actions:
        supplied = manifest.get("actions", {}).get(action)
        if not isinstance(supplied, dict) or supplied.get("count") != len(supplied.get("ids", [])):
            raise MigrationError("invalid manifest entry for %s" % action)
        if set(planned["actions"][action]["ids"]) - set(supplied["ids"]):
            raise MigrationError("current %s plan contains ids outside manifest cap" % action)
        if planned["actions"][action]["count"] > supplied["count"]:
            raise MigrationError("current %s plan exceeds manifest count" % action)
    return planned


def _drain_before_abort(transport, drain_fn):
    """3연속 실패로 중단하기 전에 이미 성공한 PATCH 의 consolidation 을 기다린다(issue #109).
    drain 이 실패해도 중단 사유(3연속 실패)를 가리지 않도록 drain 오류(MigrationError 뿐 아니라 IncompleteRead·
    AttributeError 같은 어떤 Exception 이든)는 출력만 하고 삼킨다."""
    try:
        drain_fn(transport)
    except Exception as drain_exc:
        print("DRAIN FAILED before abort | %s: %s" % (type(drain_exc).__name__, drain_exc))


def run_live(
    transport, changes, actions, manifest, state_path, drain_fn=drain,
    delete_tags=None, request_sleeper=time.sleep,
):
    capped_plan = validate_manifest(manifest, changes, actions)
    approved_delete_tags = set(DELETE_TAGS if delete_tags is None else delete_tags)
    try:
        state = load_json(state_path)
    except FileNotFoundError:
        state = {"version": 1, "processed": []}
    processed_values = state.get("processed", [])
    if not isinstance(processed_values, list) or not all(isinstance(value, str) for value in processed_values):
        raise MigrationError("checkpoint processed field must be a list of ids")
    processed = set(processed_values)
    candidates = [item for item in changes if item["action"] in actions and item["id"] not in processed]
    for item in candidates:
        if item["action"] == "DELETE" and not (set(item["old"]) & approved_delete_tags):
            raise MigrationError("delete candidate lacks approved delete tag: " + item["id"])

    completed_now = 0
    consecutive_failed_documents = 0
    failed_ids = []
    for start in range(0, len(candidates), BATCH_SIZE):
        batch = candidates[start:start + BATCH_SIZE]
        for item in batch:
            quoted_id = urllib.parse.quote(item["id"], safe="")
            path = BANK_PATH + "/documents/" + quoted_id
            try:
                if item["action"] == "DELETE":
                    request_with_failure_cap(
                        transport, "DELETE", path, sleeper=request_sleeper,
                    )
                else:
                    request_with_failure_cap(
                        transport, "PATCH", path, {"tags": item["new"]},
                        sleeper=request_sleeper,
                    )
            except MigrationError as exc:
                failed_ids.append(item["id"])
                consecutive_failed_documents += 1
                print("DOCUMENT FAILED %s | %s" % (item["id"], exc))
                if consecutive_failed_documents >= 3:
                    _drain_before_abort(transport, drain_fn)
                    raise MigrationError("3 consecutive failed documents; stopped batch") from exc
                continue
            consecutive_failed_documents = 0
            processed.add(item["id"])
            completed_now += 1
            state = {"version": 1, "processed": sorted(processed)}
            save_json(state_path, state)
        drain_fn(transport)
    if failed_ids:
        raise MigrationError("%d document mutation(s) failed: %s" % (len(failed_ids), ",".join(failed_ids)))
    print("LIVE complete=%d skipped_checkpoint=%d cap=%d" % (
        completed_now, len([item for item in changes if item["id"] in processed]) - completed_now,
        sum(capped_plan["actions"][action]["count"] for action in actions)))


# --------------------------------------------------------------------------- manifest 실행 모드 (issue #43)
# 호스트 계획기(scripts/tag_retag_plan.py)가 만든 version 2 manifest 의 old -> new 만 그대로 PATCH 한다.
# 태그를 스스로 계산하지 않는다: 위의 transform_tags·plan_document·make_plan·ALIAS_MAP·REMOVE_TAGS·
# DELETE_TAGS 와 require_configuration 을 이 구역의 어떤 함수도 부르거나 참조하지 않는다
# (규칙 사본이 셋으로 갈라진 것이 #35 의 원인 중 하나다. tests/test_tag_retag_manifest.py 가 AST 로 고정한다).

def injected_or_file(path):
    """`-` 는 tag-migrate.sh 가 주입한 호스트 파일, 그 밖에는 이 프로세스가 볼 수 있는 경로."""
    if path == "-":
        if INJECTED_INPUT_JSON is None:
            raise MigrationError("no injected input; use scripts/tag-migrate.sh")
        return json.loads(INJECTED_INPUT_JSON)
    return load_json(path)


def _tag_list(value, what):
    if not isinstance(value, list) or not all(isinstance(tag, str) and tag for tag in value):
        raise MigrationError("manifest %s must be a list of non-empty strings" % what)
    return value


def validate_apply_manifest(manifest):
    """구조·요약 일치를 확인하고 문서 목록을 돌려준다. 어긋나면 아무것도 하기 전에 중단한다."""
    if not isinstance(manifest, dict):
        raise MigrationError("manifest is not an object")
    if manifest.get("version") != APPLY_MANIFEST_VERSION:
        raise MigrationError("manifest version %r is not %d (version 1 is the id-list manifest for --live --manifest)"
                             % (manifest.get("version"), APPLY_MANIFEST_VERSION))
    if manifest.get("bank") != BANK_NAME:
        raise MigrationError("manifest bank %r is not %s" % (manifest.get("bank"), BANK_NAME))
    documents = manifest.get("documents")
    unresolved = manifest.get("unresolved")
    summary = manifest.get("summary")
    if not isinstance(documents, list) or not isinstance(unresolved, list) or not isinstance(summary, dict):
        raise MigrationError("manifest lacks documents/unresolved/summary")
    ids = set()
    by_type = {}
    for entry in documents:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"]:
            raise MigrationError("manifest document lacks an id: %r" % (entry,))
        if entry["id"] in ids:
            raise MigrationError("manifest lists %s twice" % entry["id"])
        ids.add(entry["id"])
        old = _tag_list(entry.get("old"), "old of " + entry["id"])
        new = _tag_list(entry.get("new"), "new of " + entry["id"])
        if not new or sorted(old) == sorted(new):
            raise MigrationError("manifest entry %s has an empty or unchanged new" % entry["id"])
        for kind in _tag_list(entry.get("types"), "types of " + entry["id"]):
            by_type[kind] = by_type.get(kind, 0) + 1
    for entry in unresolved:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"]:
            raise MigrationError("manifest unresolved entry lacks an id: %r" % (entry,))
        if entry["id"] in ids:
            raise MigrationError("%s is both planned and unresolved" % entry["id"])
    if manifest.get("documents_sha256") != documents_digest(documents):
        raise MigrationError("manifest documents_sha256 is missing or does not match its documents "
                             "(edited after planning? re-run scripts/tag_retag_plan.py)")
    if summary.get("documents") != len(documents) or summary.get("unresolved") != len(unresolved):
        raise MigrationError("manifest summary counts do not match its lists")
    if summary.get("by_type") != dict(sorted(by_type.items())):
        raise MigrationError("manifest summary by_type does not match its documents")
    return documents


def documents_digest(documents):
    """계획기(scripts/tag_retag_plan.py documents_digest)와 같은 알고리즘: 계획 뒤 손편집된 manifest 를 거부한다.
    컨테이너가 태그 형식을 다시 검사하면 규칙 사본이 되므로, 형식 판정은 계획기에 두고 변조만 여기서 막는다."""
    canonical = json.dumps(documents, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def manifest_digest(manifest):
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def apply_manifest_lines(manifest):
    """dry-run 요약: 적용 예정 목록 -> unresolved 목록 -> 유형별 건수."""
    lines = []
    for entry in manifest["documents"]:
        lines.append("%s | %s | %s | %s" % (
            entry["id"], json.dumps(entry["old"], ensure_ascii=False),
            json.dumps(entry["new"], ensure_ascii=False), ",".join(entry["types"])))
    for entry in manifest["unresolved"]:
        lines.append("UNRESOLVED %s | %s | %s" % (
            entry["id"], json.dumps(entry.get("old"), ensure_ascii=False), entry.get("reason")))
    summary = manifest["summary"]
    lines.append("SUMMARY documents=%d unresolved=%d" % (summary["documents"], summary["unresolved"]))
    for kind, count in summary["by_type"].items():
        lines.append("SUMMARY type %s %d" % (kind, count))
    if "untargeted_changes" in summary:
        lines.append("SUMMARY untargeted_changes=%s" % summary["untargeted_changes"])
    lines.append("SUMMARY documents_sha256=%s" % manifest["documents_sha256"])
    return lines


def list_documents_tolerant(transport):
    """manifest 모드 전용 목록 조회. list_paginated 와 달리 페이지 사이 total 변화로 중단하지 않는다.

    B 는 다른 세션이 계속 retain 하는 라이브 뱅크에서 돈다(ambient write 는 정상). 새 문서가 끼면 total 이
    늘 뿐이므로 id 로 합치고, 빈 페이지이거나 offset 이 마지막으로 본 total 에 닿으면 끝낸다. 응답 모양이
    깨졌거나 HTTP 오류면 기존처럼 중단한다(parse_page, request_with_failure_cap).
    """
    documents, offset, total = {}, 0, None
    while total is None or offset < total:
        page_path = "%s/documents?limit=%d&offset=%d" % (BANK_PATH, PAGE_SIZE, offset)
        items, latest = parse_page(request_with_failure_cap(transport, "GET", page_path))
        if total is not None and latest != total:
            print("LIST total changed during pagination: %d -> %d (ambient write; continuing)" % (total, latest))
        total = latest
        if not items:
            break
        for document in items:
            document_id = document.get("id", document.get("document_id")) if isinstance(document, dict) else None
            tags = document.get("tags") if isinstance(document, dict) else None
            if document_id:
                documents[document_id] = list(tags) if isinstance(tags, (list, tuple)) else []
        offset += len(items)
    return documents


def apply_manifest_live(
    transport, manifest, state_path, drain_fn=drain, request_sleeper=time.sleep,
):
    """manifest 에 적힌 문서만 PATCH {"tags": new}. unresolved 와 manifest 밖 문서는 건드리지 않는다.

    배치(BATCH_SIZE)마다 문서 목록을 새로 읽어 현재 태그를 본다: old 와 같으면 PATCH, 이미 new 면
    건너뛰고(재실행 안전), 그 밖이면 drift 로 건너뛰며 보고한다. 재시도·3연속 실패 중단·배치 사이 drain·
    checkpoint 는 run_live 와 같은 계약이다(checkpoint 는 manifest 해시에 묶인다). 끝내기 전에 drain 을
    무조건 한 번 더 돈다. 목록 조회는 page 사이 total 변화를 견딘다(list_documents_tolerant).
    """
    documents = validate_apply_manifest(manifest)
    digest = manifest_digest(manifest)
    try:
        state = load_json(state_path)
    except FileNotFoundError:
        state = {"version": 2, "manifest_sha256": digest, "processed": []}
    processed_values = state.get("processed", [])
    if state.get("manifest_sha256") != digest:
        raise MigrationError("checkpoint %s belongs to a different manifest" % state_path)
    if not isinstance(processed_values, list) or not all(isinstance(value, str) for value in processed_values):
        raise MigrationError("checkpoint processed field must be a list of ids")
    processed = set(processed_values)
    candidates = [entry for entry in documents if entry["id"] not in processed]

    patched, already, drifted, missing, failed_ids = [], [], [], [], []
    consecutive_failed_documents = 0
    for start in range(0, len(candidates), BATCH_SIZE):
        batch = candidates[start:start + BATCH_SIZE]
        current = list_documents_tolerant(transport)
        if any(entry["id"] not in current for entry in batch):
            # 페이지 사이에 문서가 끼어 밀렸을 수 있으니 한 번 더 읽고 그래도 없을 때만 MISSING 으로 본다.
            current.update({k: v for k, v in list_documents_tolerant(transport).items() if k not in current})
        patched_in_batch = False
        for entry in batch:
            now = current.get(entry["id"])
            if now is None:
                missing.append(entry["id"])
                print("MISSING %s | not in the current document list" % entry["id"])
                continue
            if not all(isinstance(tag, str) for tag in now):
                # 문자열이 아닌 원소는 old/new 와 비교할 수 없다(sorted 가 TypeError). 건드리지 않고 보고한다.
                drifted.append(entry["id"])
                print("DRIFT %s | current=%s | manifest old=%s" % (
                    entry["id"], json.dumps(now, ensure_ascii=False), json.dumps(entry["old"], ensure_ascii=False)))
                continue
            if sorted(now) == sorted(entry["new"]):
                already.append(entry["id"])
                processed.add(entry["id"])
                save_json(state_path, {"version": 2, "manifest_sha256": digest, "processed": sorted(processed)})
                print("ALREADY %s | current tags already equal new" % entry["id"])
                continue
            if sorted(now) != sorted(entry["old"]):
                drifted.append(entry["id"])
                print("DRIFT %s | current=%s | manifest old=%s" % (
                    entry["id"], json.dumps(now, ensure_ascii=False), json.dumps(entry["old"], ensure_ascii=False)))
                continue
            path = BANK_PATH + "/documents/" + urllib.parse.quote(entry["id"], safe="")
            try:
                request_with_failure_cap(transport, "PATCH", path, {"tags": entry["new"]}, sleeper=request_sleeper)
            except MigrationError as exc:
                failed_ids.append(entry["id"])
                consecutive_failed_documents += 1
                print("DOCUMENT FAILED %s | %s" % (entry["id"], exc))
                if consecutive_failed_documents >= 3:
                    _drain_before_abort(transport, drain_fn)
                    raise MigrationError("3 consecutive failed documents; stopped batch") from exc
                continue
            consecutive_failed_documents = 0
            print("PATCHED %s" % entry["id"])
            patched.append(entry["id"])
            patched_in_batch = True
            processed.add(entry["id"])
            save_json(state_path, {"version": 2, "manifest_sha256": digest, "processed": sorted(processed)})
        if patched_in_batch:
            drain_fn(transport)
    # 재개 실행이 PATCH 없이 끝나도 이전 실행이 남긴 consolidation 을 기다린 뒤에 끝낸다.
    drain_fn(transport)
    if failed_ids:
        raise MigrationError("%d document mutation(s) failed: %s" % (len(failed_ids), ",".join(failed_ids)))
    print("APPLY complete=%d already=%d drift=%d missing=%d skipped_checkpoint=%d planned=%d unresolved_untouched=%d" % (
        len(patched), len(already), len(drifted), len(missing), len(documents) - len(candidates),
        len(documents), len(manifest["unresolved"])))
    return {"patched": patched, "already": already, "drift": drifted, "missing": missing}


def extract_label(text):
    """본문 맨 앞 `[라벨]` 의 라벨만. 본문의 나머지는 이 함수 밖으로 나가지 않는다."""
    match = LABEL_PATTERN.match(text.lstrip())
    return match.group(1).strip() or None if match else None


def document_body(payload, document_id):
    """문서 상세 응답에서 본문 문자열을 찾는다. 후보 필드가 하나도 없으면 중단한다(값은 싣지 않는다)."""
    if isinstance(payload, dict):
        for name in LABEL_BODY_FIELDS:
            if isinstance(payload.get(name), str):
                return payload[name]
    keys = sorted(payload) if isinstance(payload, dict) else type(payload).__name__
    raise MigrationError("document %s has none of the body fields %s (response keys: %s)" % (
        document_id, "/".join(LABEL_BODY_FIELDS), keys))


def export_labels(transport, ids, out, request_sleeper=time.sleep):
    """읽기 전용. ids 각 문서의 맨 앞 라벨만 {"id","label"} JSONL 로 낸다. 본문은 출력·저장하지 않는다."""
    if not isinstance(ids, list) or not all(isinstance(value, str) and value for value in ids):
        raise MigrationError("ids must be a list of non-empty strings")
    unique = sorted(set(ids))
    # 재시도 안내 출력이 JSONL 과 섞이지 않게 진행 출력은 stderr 로 보낸다.
    with contextlib.redirect_stdout(sys.stderr):
        for document_id in unique:
            path = BANK_PATH + "/documents/" + urllib.parse.quote(document_id, safe="")
            payload = request_with_failure_cap(transport, "GET", path, sleeper=request_sleeper)
            label = extract_label(document_body(payload, document_id))
            out.write(json.dumps({"id": document_id, "label": label}, ensure_ascii=False, sort_keys=True) + "\n")
    print("exported %d labels" % len(unique), file=sys.stderr)
    return len(unique)



class FakeTransport:
    def __init__(self, routes=None, fail_mutation_id=None):
        self.routes = routes or {}
        self.calls = []
        self.fail_mutation_id = fail_mutation_id
        self.failed = False

    def request(self, method, path, body=None, authenticated=True):
        self.calls.append((method, path, body, authenticated))
        if self.fail_mutation_id and self.fail_mutation_id in path and not self.failed:
            # One logical mutation receives the mandated three consecutive failures.
            failures = sum(1 for call in self.calls if self.fail_mutation_id in call[1])
            if failures <= 3:
                if failures == 3:
                    self.failed = True
                return 503, {"error": "injected"}, "injected"
        key = (method, path)
        if key in self.routes:
            value = self.routes[key]
            return value() if callable(value) else value
        return 200, {}, "{}"


def require_bank():
    if not BANK_NAME:
        raise MigrationError("config_missing HC_PERSONAL_BANK (injected by scripts/tag-migrate.sh)")
    if not _BANK_NAME_RE.match(BANK_NAME):
        raise MigrationError("invalid bank name: %r" % (BANK_NAME,))


def self_test():
    global BANK_NAME, BANK_PATH
    BANK_NAME = "selftest-bank"   # no host configuration is read or needed by the self-test
    BANK_PATH = "/v1/default/banks/" + BANK_NAME
    aliases = {"topic:aside": "project:aside", "kind:done": "kind:completion"}
    remove = {"topic:remove"}
    deletes = {"project:delete-me"}
    assert transform_tags(["aside"], aliases, remove) == ["project:aside"]
    assert transform_tags(["topic:remove", "kind:done"], aliases, remove) == ["kind:completion"]
    assert transform_tags(["aside", "project:aside", "aside"], aliases, remove) == ["project:aside"]
    assert transform_tags(["Unknown Prefix"], aliases, remove) == ["topic:unknown-prefix"]
    assert transform_tags(["projekt:Keep Me"], aliases, remove) == ["projekt:Keep Me"]
    deleted = plan_document({"id": BACKFILL_ID, "tags": ["project:delete-me", "aside"]}, aliases, deletes, remove)
    assert deleted is not None and deleted["action"] == "DELETE"
    backfill = plan_document({"id": BACKFILL_ID, "tags": []}, aliases, deletes, remove)
    assert backfill is not None and backfill["action"] == "BACKFILL" and backfill["new"] == BACKFILL_TAGS
    verification_backfill = plan_document({"id": "run-created-zero-tag", "tags": []}, aliases, deletes, remove)
    assert verification_backfill is not None and verification_backfill["action"] == "BACKFILL"
    assert verification_backfill["new"] == ["kind:verification", "topic:memory-vault", "user:demo"]
    delete_precedence = plan_document(
        {"id": "otherwise-backfillable", "tags": ["project:delete-me"]}, aliases, deletes, remove,
    )
    assert delete_precedence is not None and delete_precedence["action"] == "DELETE"
    corrupt = [{"id": "corrupt", "old": ["aside"], "new": ["project:invented"], "action": "RETAG"}]
    assert count_pipeline_violations(corrupt, aliases, deletes, remove) == 1
    surviving_alias = [{"id": "alias-key", "old": ["kind:done"], "new": ["kind:done"], "action": "RETAG"}]
    assert count_pipeline_violations(surviving_alias, aliases, deletes, remove) == 1
    missing_output = [{"id": "missing-output", "old": ["aside", "kind:done"], "new": ["project:aside"], "action": "RETAG"}]
    assert count_pipeline_violations(missing_output, aliases, deletes, remove) == 1
    complete_output = [{"id": "complete", "old": ["aside", "kind:done"], "new": ["project:aside", "kind:completion"], "action": "RETAG"}]
    assert count_pipeline_violations(complete_output, aliases, deletes, remove) == 0

    page1 = {"items": [{"id": "a", "tags": []}, {"document_id": "b", "tags": []}], "total": 3, "limit": 2, "offset": 0}
    page2 = {"items": [{"id": "c", "tags": []}], "total": 3, "limit": 2, "offset": 2}
    pager = FakeTransport({
        ("GET", BANK_PATH + "/documents?limit=100&offset=0"): (200, page1, "page1"),
        ("GET", BANK_PATH + "/documents?limit=100&offset=2"): (200, page2, "page2"),
    })
    docs, total = list_paginated(pager, BANK_PATH + "/documents")
    assert len(docs) == total == 3 and len(pager.calls) == 2

    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        state_path = os.path.join(directory, "state.json")
        changes = [
            {"id": "one", "old": ["aside"], "new": ["project:aside"], "action": "RETAG"},
            {"id": "two", "old": ["aside"], "new": ["project:aside"], "action": "RETAG"},
            {"id": "three", "old": ["aside"], "new": ["project:aside"], "action": "RETAG"},
        ]
        manifest = manifest_for(changes, {"RETAG"})
        first = FakeTransport(fail_mutation_id="two")
        try:
            run_live(
                first, changes, {"RETAG"}, manifest, state_path,
                drain_fn=lambda transport: None, delete_tags=deletes,
                request_sleeper=lambda seconds: None,
            )
            raise AssertionError("injected interruption did not stop")
        except MigrationError as exc:
            assert "document mutation(s) failed" in str(exc)
        assert load_json(state_path)["processed"] == ["one", "three"]
        second = first
        calls_before_resume = len(second.calls)
        run_live(
            second, changes, {"RETAG"}, manifest, state_path,
            drain_fn=lambda transport: None, delete_tags=deletes,
            request_sleeper=lambda seconds: None,
        )
        resumed = [call[1] for call in second.calls[calls_before_resume:] if call[0] == "PATCH"]
        assert resumed == [BANK_PATH + "/documents/two"]

        bad_delete = [
            {"id": "would-mutate-first", "old": ["aside"], "new": ["project:aside"], "action": "RETAG"},
            {"id": "bad-delete", "old": ["topic:ordinary"], "new": [], "action": "DELETE"},
        ]
        bad_delete_transport = FakeTransport()
        try:
            run_live(
                bad_delete_transport, bad_delete, {"RETAG", "DELETE"},
                manifest_for(bad_delete, {"RETAG", "DELETE"}), os.path.join(directory, "bad-delete.json"),
                drain_fn=lambda transport: None, delete_tags=deletes,
            )
            raise AssertionError("unapproved delete candidate did not abort")
        except MigrationError as exc:
            assert "lacks approved delete tag" in str(exc)
        assert not bad_delete_transport.calls

        gone_delete = [{"id": "already-gone", "old": ["project:delete-me"], "new": [], "action": "DELETE"}]
        gone_path = BANK_PATH + "/documents/already-gone"
        gone = FakeTransport({("DELETE", gone_path): (404, {"detail": "not found"}, "not found")})
        run_live(
            gone, gone_delete, {"DELETE"}, manifest_for(gone_delete, {"DELETE"}),
            os.path.join(directory, "gone.json"), drain_fn=lambda transport: None,
            delete_tags=deletes,
        )
        assert len([call for call in gone.calls if call[0] == "DELETE"]) == 1

        fourxx_change = [{"id": "fourxx", "old": ["aside"], "new": ["project:aside"], "action": "RETAG"}]
        fourxx_path = BANK_PATH + "/documents/fourxx"
        fourxx = FakeTransport({("PATCH", fourxx_path): (422, {"error": "bad tags"}, "bad tags")})
        try:
            run_live(
                fourxx, fourxx_change, {"RETAG"}, manifest_for(fourxx_change, {"RETAG"}),
                os.path.join(directory, "fourxx.json"), drain_fn=lambda transport: None,
                delete_tags=deletes,
            )
            raise AssertionError("4xx mutation did not fail")
        except MigrationError:
            pass
        assert len([call for call in fourxx.calls if call[0] == "PATCH"]) == 1

        failed_changes: list[dict[str, Any]] = [
            {"id": name, "old": ["aside"], "new": ["project:aside"], "action": "RETAG"}
            for name in ("fail-one", "fail-two", "fail-three", "not-attempted")
        ]
        failed_routes = {
            ("PATCH", BANK_PATH + "/documents/" + item["id"]): (422, {"error": "injected"}, "injected")
            for item in failed_changes
        }
        consecutive = FakeTransport(failed_routes)
        try:
            run_live(
                consecutive, failed_changes, {"RETAG"}, manifest_for(failed_changes, {"RETAG"}),
                os.path.join(directory, "consecutive.json"), drain_fn=lambda transport: None,
                delete_tags=deletes,
            )
            raise AssertionError("three failed documents did not abort the batch")
        except MigrationError as exc:
            assert "3 consecutive failed documents" in str(exc)
        failed_paths = [call[1] for call in consecutive.calls if call[0] == "PATCH"]
        assert failed_paths == [BANK_PATH + "/documents/" + name for name in ("fail-one", "fail-two", "fail-three")]

    clock = [0.0]
    def monotonic():
        return clock[0]
    def sleeper(seconds):
        clock[0] += seconds
    operation = {"id": "op", "task_type": "consolidation", "status": "pending"}
    def operation_page():
        status = "pending" if "status=pending" in hanging.calls[-1][1] else "processing"
        rows = [dict(operation, status=status)] if status == "pending" else []
        payload = {"operations": rows, "total": len(rows), "limit": 100, "offset": 0}
        return 200, payload, json.dumps(payload)
    hanging = FakeTransport()
    hanging.routes = {
        ("GET", BANK_PATH + "/operations?status=pending&type=consolidation&limit=100&offset=0"): operation_page,
        ("GET", BANK_PATH + "/operations?status=processing&type=consolidation&limit=100&offset=0"): operation_page,
    }
    try:
        drain(hanging, timeout=5, monotonic=monotonic, sleeper=sleeper)
        raise AssertionError("drain timeout was not enforced")
    except MigrationError as exc:
        assert "terminal drain timeout" in str(exc) and clock[0] == 5.0

    # manifest 실행 모드(issue #43): 태그를 계산하지 않고 manifest 의 old -> new 만 PATCH 한다.
    planned = {
        "version": APPLY_MANIFEST_VERSION, "bank": BANK_NAME,
        "documents": [
            {"id": "m1", "old": ["topic:gotcha"], "new": ["kind:gotcha"], "types": ["misfiled-slot"]},
            {"id": "m2", "old": ["project:birch"], "new": ["project:fir-easel"], "types": ["project-merge"]},
        ],
        "unresolved": [{"id": "u1", "old": ["x"], "reason": "kind-missing: no label"}],
        "summary": {"documents": 2, "by_type": {"misfiled-slot": 1, "project-merge": 1}, "unresolved": 1},
    }
    planned["documents_sha256"] = documents_digest(planned["documents"])
    listing = {
        "items": [{"id": "m1", "tags": ["topic:gotcha"]}, {"id": "m2", "tags": ["drifted"]},
                  {"id": "u1", "tags": ["x"]}, {"id": "outside", "tags": ["y"]}],
        "total": 4, "limit": 100, "offset": 0,
    }
    applying = FakeTransport({("GET", BANK_PATH + "/documents?limit=100&offset=0"): (200, listing, "listing")})
    with tempfile.TemporaryDirectory() as directory:
        outcome = apply_manifest_live(applying, planned, os.path.join(directory, "apply.json"), drain_fn=lambda t: None)
    assert outcome["patched"] == ["m1"] and outcome["drift"] == ["m2"]
    assert [(call[1], call[2]) for call in applying.calls if call[0] == "PATCH"] == [
        (BANK_PATH + "/documents/m1", {"tags": ["kind:gotcha"]})]
    assert extract_label("[완료] 본문") == "완료" and extract_label("본문 [완료]") is None
    try:
        document_body({"id": "x"}, "x")
        raise AssertionError("missing body field did not abort")
    except MigrationError as exc:
        assert "none of the body fields" in str(exc)
    print("SELF-TEST PASS: independent guard, retry policy, cross-document cap, checkpoint resume, drain timeout, manifest apply")


def parse_args(argv):
    # allow_abbrev=False (issue #128): 모든 플래그의 약어를 거부한다(특히 --apply, --ids 는 tag-migrate.sh 의 호스트 파일 주입 case 를 우회).
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--self-test", action="store_true")
    mode.add_argument("--export-labels", action="store_true",
                      help="read-only: print {id,label} JSONL for the ids in --ids-file (body is never printed)")
    parser.add_argument("--only", help="comma-separated: retag,delete,backfill")
    parser.add_argument("--manifest-out")
    parser.add_argument("--manifest", help="required dry-run manifest for --live")
    parser.add_argument("--state", help="live checkpoint path (default: MANIFEST.state.json)")
    parser.add_argument("--apply-manifest", metavar="FILE",
                        help="with --dry-run/--live: execute a version 2 old->new manifest from scripts/tag_retag_plan.py "
                             "(- = the file injected by scripts/tag-migrate.sh)")
    parser.add_argument("--ids-file", metavar="FILE", help="with --export-labels: JSON list of document ids (- = injected)")
    return parser.parse_args(argv)


def reject_flags(args, names, mode):
    used = [name for name in names if getattr(args, name.lstrip("-").replace("-", "_"))]
    if used:
        raise MigrationError("%s cannot be combined with %s" % (mode, ", ".join(used)))


def require_token():
    token = os.environ.get("HINDSIGHT_API_MCP_AUTH_TOKEN")
    if not token:
        raise MigrationError("HINDSIGHT_API_MCP_AUTH_TOKEN is required")
    return token


def run_export_labels(args):
    reject_flags(args, ("--apply-manifest", "--manifest", "--only", "--manifest-out", "--state"), "--export-labels")
    if not args.ids_file:
        raise MigrationError("--export-labels requires --ids-file")
    ids = injected_or_file(args.ids_file)
    # log=False: HttpTransport 의 응답 로그(본문 앞 1000자)가 문서 본문을 찍지 않게 한다.
    export_labels(HttpTransport(BASE_URL, require_token(), log=False), ids, sys.stdout)
    return 0


def run_apply_manifest(args):
    reject_flags(args, ("--manifest", "--only", "--manifest-out", "--ids-file"), "--apply-manifest")
    manifest = injected_or_file(args.apply_manifest)
    validate_apply_manifest(manifest)
    for line in apply_manifest_lines(manifest):
        print(line)
    if args.dry_run:
        print("DRY-RUN complete; mutations=0")
        return 0
    # log=False: 응답 로그(앞 1000자)가 목록·PATCH 응답의 문서 본문을 찍지 않게 한다. 출력은 상태 줄과 문서 id 뿐이다.
    transport = HttpTransport(BASE_URL, require_token(), log=False)
    preflight(transport)
    state_path = args.state or os.path.join(
        tempfile.gettempdir(), "tag-migrate-apply-%s.state.json" % manifest_digest(manifest)[:12])
    apply_manifest_live(transport, manifest, state_path)
    return 0


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    if args.self_test:
        self_test()
        return 0
    require_bank()
    if args.export_labels:
        return run_export_labels(args)
    if args.apply_manifest:
        return run_apply_manifest(args)
    if args.ids_file:
        raise MigrationError("--ids-file is only for --export-labels")
    require_configuration()
    if args.live and not args.manifest:
        raise MigrationError("--live refuses to run without --manifest")
    token = os.environ.get("HINDSIGHT_API_MCP_AUTH_TOKEN")
    if not token:
        raise MigrationError("HINDSIGHT_API_MCP_AUTH_TOKEN is required")
    transport = HttpTransport(BASE_URL, token)
    preflight(transport)
    documents, total = list_paginated(transport, BANK_PATH + "/documents")
    changes, out_of_pipeline = make_plan(documents, ALIAS_MAP, set(DELETE_TAGS), set(REMOVE_TAGS))
    actions = selected_actions(args.only)
    print_plan(changes, actions, len(documents), total, out_of_pipeline)
    manifest = manifest_for(changes, actions)
    print("MANIFEST " + json.dumps(manifest, sort_keys=True))
    if args.dry_run:
        if args.manifest_out:
            save_json(args.manifest_out, manifest)
            print("MANIFEST_PATH " + args.manifest_out)
        print("DRY-RUN complete; mutations=0")
        return 0
    supplied = load_json(args.manifest)
    state_path = args.state or args.manifest + ".state.json"
    run_live(transport, changes, actions, supplied, state_path)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (MigrationError, OSError, ValueError, json.JSONDecodeError) as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
