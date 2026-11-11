#!/usr/bin/env python3
"""태그 위생 측정 (issue #42, 상위 #35).

저장된 문서의 태그를 게이트웨이의 태그 전용 평가 함수 `gateway.rules.evaluate_tags`에 다시
통과시켜, "지금 규칙이라면 무엇이 바뀌거나 거부되는가"로 문제 유형을 센다. 규칙은 여기서
다시 구현하지 않는다(규칙 사본이 갈라진 것이 #35의 원인 중 하나였다). 이 파일이 직접 판정하는
것은 평가 함수가 일부러 고치지 않는 '이미 합쳐진 slug' 하나뿐이고, 그것도 정책이 선언한
prefix 이름만 쓴다.

두 부분으로 나뉜다.
  report  호스트에서 실행. 문서 JSONL(한 줄에 {"id","created_at","tags"})을 읽어 표/JSON을 낸다.
  export  hindsight 컨테이너 안에서 실행(scripts/tag-hygiene-export.sh가 stdin으로 주입).
          운영 문서 목록을 GET으로만 읽어 JSONL로 stdout에 낸다. gateway 패키지는 컨테이너에
          없으므로 이 파일의 모듈 수준 import는 표준 라이브러리뿐이고, gateway는 report에서만
          지연 import한다.

사용 예:
  scripts/tag-hygiene-export.sh > docs.jsonl
  python3 scripts/tag_hygiene.py report docs.jsonl --since 2026-10-02T00:00:00Z \\
      --audit audit/gateway.log
  python3 scripts/tag_hygiene.py report docs.jsonl --json > hygiene.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone

SCHEMA_VERSION = 1
BASE_URL = "http://localhost:8888"
PAGE_SIZE = 100
_BANK_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def configured_bank(strict=False):
    """HC_PERSONAL_BANK from the environment or .env.host.local (issue #186); None when unset.

    `export` runs inside the container from stdin, where neither hc_host_env.py nor the host's
    variables exist: there the bank must come from `--bank` (tag-hygiene-export.sh passes it)."""
    try:
        import hc_host_env   # host only: scripts/ is sys.path[0] when this file is run directly
    except ImportError:
        return os.environ.get("HC_PERSONAL_BANK") or None
    try:
        return hc_host_env.get("HC_PERSONAL_BANK")
    except hc_host_env.ConfigMissing as exc:     # unreadable .env.host.local (config_invalid)
        if strict:
            raise ConfigMissingError(str(exc))
        return None


DEFAULT_BANK = configured_bank()   # also read by tag_retag_plan.py


def require_bank(bank):
    if not bank:
        configured_bank(strict=True)           # an unreadable settings file is the real cause when it raises
        raise ConfigMissingError("config_missing HC_PERSONAL_BANK (set it in .env.host.local or pass --bank)")
    return bank

TYPE_MERGED = "merged-slug"
TYPE_MISFILE = "misfiled-slot"
TYPE_USER = "user-split"
PROBLEM_TYPES = (TYPE_MERGED, TYPE_MISFILE, TYPE_USER)
TYPE_LABELS = {
    TYPE_MERGED: "합쳐진 slug",
    TYPE_MISFILE: "칸 오배정",
    TYPE_USER: "user 분열",
}


class HygieneError(RuntimeError):
    pass


class ConfigMissingError(HygieneError):
    """A required host setting is absent (issue #186); main() reports it as `TAG_HYGIENE_FAIL - config_missing <key>`."""


# --------------------------------------------------------------------------- export
# 컨테이너 안에서 도는 부분. tag_migrate.list_paginated 와 같은 검증 계약이다
# (tests/test_tag_hygiene.py 가 두 구현에 같은 불량 응답을 먹여 같은 거부를 고정한다).

class HttpTransport:
    """GET 전용 전송. 읽기만 한다는 약속을 메서드 검사로도 강제한다."""

    def __init__(self, base_url, token):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def request(self, method, path, body=None, authenticated=True):
        if method != "GET" or body is not None:
            raise HygieneError("export is read-only: refusing %s" % method)
        headers = {"Accept": "application/json"}
        if authenticated and self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(self.base_url + path, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read().decode("utf-8", "replace")
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            payload = None
        return status, payload, raw


def parse_page(payload, key="items"):
    if not isinstance(payload, dict):
        raise HygieneError("page response is not an object")
    items = payload.get(key)
    for field in ("total", "limit", "offset"):
        if not isinstance(payload.get(field), int):
            raise HygieneError("page response lacks integer %s" % field)
    if not isinstance(items, list):
        raise HygieneError("page response lacks list %s" % key)
    return items, payload["total"]


def list_paginated(transport, path, key="items"):
    accumulated = []
    offset = 0
    expected_total = None
    while expected_total is None or len(accumulated) < expected_total:
        separator = "&" if "?" in path else "?"
        page_path = "%s%slimit=%d&offset=%d" % (path, separator, PAGE_SIZE, offset)
        status, payload, _ = transport.request("GET", page_path)
        if status != 200:
            raise HygieneError("list failed with HTTP %s" % status)
        items, total = parse_page(payload, key)
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise HygieneError("API total changed during pagination: %d -> %d" % (expected_total, total))
        if len(accumulated) + len(items) > expected_total:
            raise HygieneError("pagination returned more items than total")
        if not items and len(accumulated) != expected_total:
            raise HygieneError("pagination stopped before reported total")
        accumulated.extend(items)
        offset += len(items)
    if len(accumulated) != expected_total:
        raise HygieneError("scanned %d != API total %d" % (len(accumulated), expected_total))
    return accumulated, expected_total


def export_documents(transport, bank):
    """Return [{"id","created_at","tags"}] for every document of `bank` (GET only)."""
    if not isinstance(bank, str) or not _BANK_NAME_RE.match(bank):
        raise HygieneError("invalid bank name: %r" % (bank,))
    path = "/v1/default/banks/%s/documents" % urllib.parse.quote(bank, safe="")
    documents, _ = list_paginated(transport, path)
    rows = []
    for document in documents:
        if not isinstance(document, dict):
            raise HygieneError("document entry is not an object: %r" % (document,))
        document_id = document.get("id", document.get("document_id"))
        if not document_id:
            raise HygieneError("document lacks id/document_id: %r" % (document,))
        if "created_at" not in document:
            # 없으면 report --since 가 모든 문서를 '시각 없음'으로 빼고 조용히 0건을 스캔한다.
            raise HygieneError("document lacks created_at: %r" % (document_id,))
        tags = document.get("tags")
        rows.append({
            "id": document_id,
            "created_at": document["created_at"],
            "tags": list(tags) if isinstance(tags, (list, tuple)) else [],
        })
    return rows


def run_export(args, out=None, err=None, transport=None):
    out = out or sys.stdout
    err = err or sys.stderr
    bank = require_bank(args.bank)
    if transport is None:
        token = os.environ.get("HINDSIGHT_API_MCP_AUTH_TOKEN")
        if not token:
            raise HygieneError("HINDSIGHT_API_MCP_AUTH_TOKEN is required")
        transport = HttpTransport(BASE_URL, token)
    rows = export_documents(transport, bank)
    for row in rows:
        out.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    err.write("exported %d documents from bank %s\n" % (len(rows), bank))
    return 0


# --------------------------------------------------------------------------- report

def _load_gateway():
    """gateway 는 report 에서만 필요하다(컨테이너의 export 에는 없다)."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)
    from gateway import rules
    from gateway.bank_policies import BankPolicies
    return rules, BankPolicies


def default_policy_path():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "policies", "bank-policies.json")


def parse_iso(value):
    """ISO 8601 문자열 -> aware datetime(UTC). 해석 못 하면 None. 시간대 없으면 UTC로 본다."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def read_documents(path):
    """문서 JSONL. 깨진 줄은 조용히 넘기지 않고 줄 번호와 함께 중단한다."""
    handle = sys.stdin if path == "-" else open(path, "r", encoding="utf-8")
    documents = []
    try:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise HygieneError("%s:%d: invalid JSON (%s)" % (path, number, exc))
            if not isinstance(row, dict) or not row.get("id"):
                raise HygieneError("%s:%d: each line must be an object with an id" % (path, number))
            documents.append(row)
    finally:
        if handle is not sys.stdin:
            handle.close()
    return documents


def _prefix_of(tag):
    head, sep, _ = tag.strip().partition(":")
    return head.strip().lower() if sep else None


def _embeds_prefix(tag, prefixes):
    """True for `<declared prefix>:<slug>` whose slug contains `-<declared prefix>-`."""
    if _prefix_of(tag) not in prefixes:
        return False
    slug = tag.partition(":")[2]
    return any("-%s-" % prefix in slug for prefix in prefixes)


def _attached_prefix(tag, prefixes):
    """구분자(쉼표·세미콜론·공백) 바로 뒤에 `<선언 prefix>:`가 붙어 있는 문자열인가."""
    # 평가 함수는 앞뒤 공백을 떼고 자르므로 같은 기준으로 본다(앞 공백만 붙은 태그는 분리가 아니다).
    return re.search(r"[\s,;](?:%s):" % "|".join(re.escape(p) for p in prefixes), tag.strip(),
                     re.IGNORECASE) is not None


def classify_tags(old_tags, verdict, policy, plain=None):
    """문서 하나의 문제 유형 태그. 교정 여부는 verdict(평가 함수 출력)에서만 읽는다."""
    forwarded = {}
    for sent, stored in verdict.normalized_tags:
        bucket = forwarded.setdefault(sent, [])
        if stored not in bucket:
            bucket.append(stored)
    plain = plain or {}     # {sent: {alias 없이 저장될 값}} - evaluate_document 가 넘긴다
    prefixes = tuple(policy.tag_prefixes)
    user_slugs = (policy.tag_vocabulary.get("user") or None) if "user" in policy.tag_closed_prefixes else None

    problems = []
    moved_targets = set()   # 이 문서에서 다른 topic 이 이미 옮겨 간 project·kind 태그
    for tag in dict.fromkeys(t for t in old_tags if isinstance(t, str)):
        to = forwarded.get(tag, [])
        if len(to) > 1 or (to and policy.tag_vocabulary and _attached_prefix(tag, prefixes)):
            # 한 문자열에 여러 태그가 붙어 저장됨 - 평가 함수가 쪼갠다(조각이 중복·불량이라 쌍이 하나로
            # 줄어든 경우 포함).
            problems.append({"type": TYPE_MERGED, "tag": tag, "to": to, "how": "split"})
            continue
        # 평가 함수가 내보낼 값(접두어 없는 맨단어·대문자·콜론 섞인 값은 정규화 뒤에야 slug가 된다)으로 본다.
        if any(_embeds_prefix(value, prefixes) for value in (to or [tag])):
            problems.append({"type": TYPE_MERGED, "tag": tag, "to": to, "how": "embedded-prefix"})
        if to and _prefix_of(tag) in (None, "topic") and _prefix_of(to[0]) in ("kind", "project"):
            # duplicate: 이동 대상이 (정규화·alias 뒤) 원래부터 자리에 있던 태그와 같아 topic 쪽이 빠진 경우.
            # moved-merged: 같은 후보로 먼저 옮겨 간 다른 topic 이 있어 함께 합쳐진 경우.
            placed = {value for other in old_tags
                      if isinstance(other, str) and other != tag and _prefix_of(other) not in (None, "topic")
                      for value in (forwarded.get(other) or [other])}
            how = "duplicate" if to[0] in placed else "moved-merged" if to[0] in moved_targets else "moved"
            if how != "duplicate":
                moved_targets.add(to[0])
            problems.append({"type": TYPE_MISFILE, "tag": tag, "to": to, "how": how})
        if _prefix_of(tag) == "user":
            # 저장될 값으로 판정한다: 대소문자·공백 변형은 정규화로 목록에 들어가면 분열이 아니고,
            # alias 로 바뀌었는지는 alias 를 걷어낸 평가 결과(기계적 교정만 한 값)와 비교한다.
            final = to[0] if to else tag
            mechanical = next(iter(plain.get(tag, (tag,))))
            outside = user_slugs is not None and final.partition(":")[2] not in user_slugs
            changed = final != mechanical
            if outside or changed:
                problems.append({"type": TYPE_USER, "tag": tag, "to": to,
                                 "how": "alias" if changed else "outside-list"})
    return problems


def evaluate_document(document, policy, rules):
    tags = document.get("tags")
    old = list(tags) if isinstance(tags, (list, tuple)) else []
    verdict = rules.evaluate_tags(tags if tags is not None else [], policy)
    new = list(verdict.tags) if isinstance(verdict.tags, (list, tuple)) else old
    rule_ids = [v.rule for v in verdict.violations]
    # alias 로 바뀐 태그 = alias 를 걷어낸 같은 평가 결과와 저장값이 달라진 교정 쌍. alias 키는 정규화 뒤
    # 형태라서 저장된 문자열을 키로 바로 찾으면 대소문자·맨단어·쪼개진 문자열의 alias 를 놓친다.
    baseline = rules.evaluate_tags(tags if tags is not None else [], replace(policy, tag_aliases={}))
    plain = {}
    for sent, stored in baseline.normalized_tags:
        plain.setdefault(sent, set()).add(stored)
    alias_rewrites = sorted({(sent, stored) for sent, stored in verdict.normalized_tags
                             if stored not in plain.get(sent, {sent})})
    return {
        "id": document["id"],
        "created_at": document.get("created_at"),
        "old": old,
        "new": new,
        "changed": new != old,
        "problems": classify_tags(old, verdict, policy, plain),
        "missing_kind": rules.RULE_TAGS_MISSING_PREFIX in rule_ids,
        "violations": rule_ids,
        "warnings": list(verdict.warnings),
        "alias_rewrites": [list(pair) for pair in alias_rewrites],
        "has_project": any(isinstance(t, str) and t.startswith("project:") for t in old),
    }


def _type_summary(records, kind):
    documents = set()
    tags = set()
    occurrences = 0
    for record in records:
        for problem in record["problems"]:
            if problem["type"] == kind:
                documents.add(record["id"])
                tags.add(problem["tag"])
                occurrences += 1
    return {"documents": len(documents), "tags": len(tags), "occurrences": occurrences}


def aggregate(records, skipped_no_timestamp=0):
    scanned = len(records)
    problem_tags = {}
    for record in records:
        for problem in record["problems"]:
            entry = problem_tags.setdefault((problem["type"], problem["tag"]), {
                "type": problem["type"], "tag": problem["tag"], "documents": 0, "to": [],
            })
            entry["documents"] += 1
            for value in problem["to"]:
                if value not in entry["to"]:
                    entry["to"].append(value)
    violations = Counter(rule for record in records for rule in set(record["violations"]))
    all_tags = [t for record in records for t in record["old"] if isinstance(t, str)]
    alias_pairs = [tuple(pair) for record in records for pair in record["alias_rewrites"]]
    no_project = sum(1 for record in records if not record["has_project"])
    return {
        "problem_types": {kind: _type_summary(records, kind) for kind in PROBLEM_TYPES},
        "missing_kind": {"documents": sum(1 for r in records if r["missing_kind"])},
        "reference": {
            "documents_scanned": scanned,
            "skipped_no_timestamp": skipped_no_timestamp,
            "tag_occurrences": len(all_tags),
            "distinct_tags": len(set(all_tags)),
            "untagged_documents": sum(1 for r in records if not r["old"]),
            "documents_without_project": no_project,
            "project_missing_ratio": round(no_project / scanned, 4) if scanned else 0.0,
            "alias_rewrites": {"occurrences": len(alias_pairs), "distinct_tags": len({p[0] for p in alias_pairs})},
            "documents_changed": sum(1 for r in records if r["changed"]),
            "documents_with_warnings": sum(1 for r in records if r["warnings"]),
            "violations_by_rule": dict(sorted(violations.items())),
        },
        "problem_tags": sorted(problem_tags.values(),
                               key=lambda e: (-e["documents"], e["type"], e["tag"])),
    }


# 게이트웨이 감사 줄은 태그 한 개를 t[:256] 으로 싣는다(gateway/audit.py TAG_LOG_MAX). 감사 형식은 바꾸지 않으므로
# 여기서는 같은 값을 읽기 쪽 상수로 둔다(tests/test_tag_hygiene.py 가 두 값이 같은지 고정한다).
TAG_LOG_MAX = 256
# 게이트웨이는 태그 한 개를 이스케이프 후 1024바이트(TAG_LOG_BYTES_MAX)로도 자른다(issue #117). 제어 문자가 많은 태그는
# 256자보다 짧게 잘리고, 그 줄의 retain_tags 에는 `stored_truncated`(잘린 태그 수)가 붙는다.
TAG_LOG_BYTES_MAX = 1024
TRUNCATED_NOTE = "길이 잘림 귀속"


def _escaped_len(text):
    """감사 줄에 쓰일 때 문자열이 차지하는 바이트(따옴표 제외). gateway/audit.py _escaped_len 과 같은 기준."""
    return len(json.dumps(text, ensure_ascii=False).encode("utf-8", "surrogatepass")) - 2


def _looks_cut(logged):
    """게이트웨이 절삭(256자 또는 이스케이프 1024바이트)이 남기는 모양인가: 256자이거나, 한 글자만 더 붙여도
    1024바이트를 넘는 길이(가장 긴 접두로 자르므로 마지막 글자 하나가 최대 6바이트라 남은 여유는 6 미만)."""
    return len(logged) == TAG_LOG_MAX or _escaped_len(logged) > TAG_LOG_BYTES_MAX - 6


def attribute_sources(problem_tags, audit_paths, bank, since):
    """문제 태그별 출처(transport, client). 감사 줄은 문서 id 를 갖지 않으므로 태그 문자열이
    `retain-tags` 줄의 저장 태그(`stored`)에 들어 있는 횟수로 센다. 게이트웨이는 `stored` 를 앞 64개만
    싣고 넘으면 `stored_omitted` 를 붙이므로(issue #99), 귀속은 앞 64개 기준이다. 그렇게 잘린 줄 수를
    `truncated_retain_tag_lines` 로 보이며, 0보다 크면 `unattributed_tags` 가 그 잘림 때문일 수 있다.

    감사 줄의 태그는 각각 256자(TAG_LOG_MAX)로도 잘려 있다(issue #109). 256자 이하 문제 태그는 정확 일치로
    귀속한다(표기 없음). 256자를 넘는 문제 태그는 정확 일치가 영영 안 되므로, 감사 줄의 256자짜리 태그가 그 태그의
    앞 256자와 같으면 접두 일치로 귀속하고 그 출처 행에 `attribution: "길이 잘림 귀속"` 을 붙인다. 256자짜리 줄은 진짜
    256자 태그일 수도 더 긴 태그가 잘린 것일 수도 있어서, 같은 접두를 가진 더 긴 문제 태그가 따로 있으면 정확 일치
    행에도 같은 표기를 붙여 모호함을 드러낸다(줄 수는 그대로).

    issue #117: 게이트웨이는 이스케이프 1024바이트로도 태그를 잘라 제어 문자가 많은 태그는 256자보다 짧게 남고, 그
    줄에는 `stored_truncated` 가 붙는다. 그런 줄은 `truncated_retain_tag_lines` 에도 센다(`stored_omitted` 와 같은
    카운터: 저장 목록이 잘린 줄). `stored_truncated` 가 있는 줄에서 절삭 모양(_looks_cut)인 로그 태그는 256자 조건 대신
    "로그 태그가 문제 태그의 진접두"이면 접두 귀속하고 같은 `길이 잘림 귀속` 표기를 붙인다. 절삭 모양이 아닌 짧은
    태그를 접두로 읽으면 무관한 긴 문제 태그에 거짓 귀속되므로 거른다. `stored_truncated` 가 없는 줄은 위 규칙 그대로다."""
    wanted = {entry["tag"] for entry in problem_tags}
    long_by_prefix = {}                     # 앞 256자 -> 256자를 넘는 문제 태그들
    for tag in wanted:
        if len(tag) > TAG_LOG_MAX:
            long_by_prefix.setdefault(tag[:TAG_LOG_MAX], []).append(tag)
    counts = Counter()
    marked = set()                          # 길이 잘림 귀속 표기가 붙는 (tag, transport, client)
    info = {"files": list(audit_paths), "retain_tag_lines": 0, "unparsable_lines": 0,
            "truncated_retain_tag_lines": 0}
    # 헬퍼는 report 경로에서만 지연 import 한다: export 는 이 파일 하나만 hindsight 컨테이너에 흘려 넣어 돌린다(issue #177).
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import audit_log_reader
    read = audit_log_reader.read_log(list(audit_paths))
    if read.extras:
        info["log_read"] = read.info()
    for line in read.lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            info["unparsable_lines"] += 1
            continue
        if not isinstance(entry, dict) or entry.get("decision") != "retain-tags":
            continue
        if entry.get("resolved_bank") != bank:
            continue
        if since is not None:
            stamp = parse_iso(entry.get("ts"))
            if stamp is None or stamp < since:
                continue
        info["retain_tag_lines"] += 1
        meta = entry.get("retain_tags")
        omitted = meta.get("stored_omitted") if isinstance(meta, dict) else None
        cut = meta.get("stored_truncated") if isinstance(meta, dict) else None
        cut_line = isinstance(cut, int) and not isinstance(cut, bool) and cut > 0
        if (isinstance(omitted, int) and omitted > 0) or cut_line:
            info["truncated_retain_tag_lines"] += 1
        if not isinstance(meta, dict) or not isinstance(meta.get("stored"), list):
            continue
        for logged in {t for t in meta["stored"] if isinstance(t, str)}:
            source = (meta.get("transport") or "unknown", meta.get("client"))
            if cut_line and _looks_cut(logged):
                longer = [t for t in wanted if len(t) > len(logged) and t.startswith(logged)]
            else:
                longer = long_by_prefix.get(logged, []) if len(logged) == TAG_LOG_MAX else []
            if logged in wanted:
                counts[(logged,) + source] += 1
                if longer:
                    marked.add((logged,) + source)
            for tag in longer:
                counts[(tag,) + source] += 1
                marked.add((tag,) + source)
    types = {}
    for entry in problem_tags:
        types.setdefault(entry["tag"], []).append(entry["type"])
    sources = []
    for (tag, transport, client), lines in counts.items():
        row = {"tag": tag, "types": sorted(set(types[tag])), "transport": transport,
               "client": client, "lines": lines}
        if (tag, transport, client) in marked:
            row["attribution"] = TRUNCATED_NOTE
        sources.append(row)
    sources.sort(key=lambda s: (s["tag"], -s["lines"], s["transport"], s["client"] or ""))
    attributed = {s["tag"] for s in sources}
    summary = Counter()
    for source in sources:
        summary[(source["transport"], source["client"])] += source["lines"]
    info["sources"] = sources
    info["summary"] = [{"transport": t, "client": c, "lines": n}
                       for (t, c), n in sorted(summary.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1] or ""))]
    info["unattributed_tags"] = sorted(wanted - attributed)
    return info


def build_report(documents, policy, rules, *, bank, since=None, audit_paths=()):
    selected = []
    skipped = 0
    for document in documents:
        if since is not None:
            stamp = parse_iso(document.get("created_at"))
            if stamp is None:
                skipped += 1        # 시각을 모르는 문서는 조용히 넣거나 빼지 않고 센다
                continue
            if stamp < since:
                continue
        selected.append(document)
    records = [evaluate_document(document, policy, rules) for document in selected]
    body = aggregate(records, skipped)
    report = {
        "schema_version": SCHEMA_VERSION,
        "bank": bank,
        "since": since.strftime("%Y-%m-%dT%H:%M:%SZ") if since else None,
        **body,
        # G(#43) 재태깅 dry-run 입력: 바뀌거나 문제가 있는 문서만 old -> new 로 싣는다.
        "documents": [
            {k: r[k] for k in ("id", "created_at", "old", "new", "changed", "problems",
                               "missing_kind", "violations", "warnings")}
            for r in records
            if r["changed"] or r["problems"] or r["missing_kind"] or r["violations"] or r["warnings"]
        ],
    }
    if audit_paths:
        report["audit"] = attribute_sources(report["problem_tags"], audit_paths, bank, since)
    return report


# --------------------------------------------------------------------------- table

def _width(text):
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text, width, right=False):
    gap = " " * max(0, width - _width(text))
    return gap + text if right else text + gap


def _table(header, rows, right=()):
    cells = [header] + rows
    widths = [max(_width(str(row[i])) for row in cells) for i in range(len(header))]
    lines = []
    for index, row in enumerate(cells):
        lines.append("  ".join(_pad(str(v), widths[i], right=(i in right)) for i, v in enumerate(row)).rstrip())
        if index == 0:
            lines.append("  ".join("-" * w for w in widths))
    return lines


def render_table(report):
    ref = report["reference"]
    out = ["태그 위생 보고 - 뱅크 %s" % report["bank"],
           "기준 시각(--since): %s | 대상 문서 %d건%s" % (
               report["since"] or "없음(전체)", ref["documents_scanned"],
               " | 시각 없어 제외 %d건" % ref["skipped_no_timestamp"] if ref["skipped_no_timestamp"] else ""),
           "", "[문제 유형 태그]"]
    rows = [[TYPE_LABELS[kind], s["documents"], s["tags"], s["occurrences"]]
            for kind, s in report["problem_types"].items()]
    out += _table(["유형", "문서 수", "태그 종류", "태그 건수"], rows, right=(1, 2, 3))
    out += ["", "[kind 누락] 문서 %d건 (재평가에서 RULE:tags-missing-prefix)" % report["missing_kind"]["documents"],
            "", "[참고값]"]
    ratio = "%.1f%%" % (ref["project_missing_ratio"] * 100)
    out += ["  태그 총수: %d건 (종류 %d)" % (ref["tag_occurrences"], ref["distinct_tags"]),
            "  project 없는 문서: %d건 (%s)" % (ref["documents_without_project"], ratio),
            "  alias로 바뀌는 태그: %d건 (종류 %d)" % (ref["alias_rewrites"]["occurrences"],
                                               ref["alias_rewrites"]["distinct_tags"]),
            "  태그 없는 문서: %d건 | 재평가로 태그가 바뀌는 문서: %d건 | 경고가 붙는 문서: %d건" % (
                ref["untagged_documents"], ref["documents_changed"], ref["documents_with_warnings"])]
    if ref["violations_by_rule"]:
        out.append("  재평가 거부(문서 수): " + ", ".join(
            "%s %d" % (rule, n) for rule, n in ref["violations_by_rule"].items()))
    if report["problem_tags"]:
        out += ["", "[문제 태그]"]
        out += _table(["유형", "태그", "문서 수", "재평가 결과"],
                      [[TYPE_LABELS[e["type"]], e["tag"], e["documents"], " | ".join(e["to"]) or "(변경 없음)"]
                       for e in report["problem_tags"]], right=(2,))
    audit = report.get("audit")
    if audit:
        out += ["", "[출처] 감사 retain-tags %d줄, 해석 불가 %d줄 (문서 id가 없어 태그 문자열로 대조)" % (
            audit["retain_tag_lines"], audit["unparsable_lines"])]
        if audit["sources"]:
            out += _table(["태그", "transport", "client", "줄 수"],
                          [[s["tag"] + (" (%s)" % s["attribution"] if s.get("attribution") else ""),
                            s["transport"], s["client"] or "-", s["lines"]] for s in audit["sources"]],
                          right=(3,))
        else:
            out.append("  문제 태그가 저장된 감사 줄이 없습니다.")
        if audit["unattributed_tags"]:
            out.append("  출처 미확인 태그: " + ", ".join(audit["unattributed_tags"]))
        read = audit.get("log_read")
        if read:        # 회전 세대·NUL·손상 .gz 가 있을 때만(issue #177)
            out.append("  읽은 감사 파일: %s, 회전 세대 %d, NUL %d바이트 제거" % (
                ", ".join(read["files"]), read["rotated_generations"], read["nul_bytes"]))
            out += ["  손상: " + why for why in read["damaged"]]
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- cli

def run_report(args, out=None):
    out = out or sys.stdout
    since = None
    if args.since:
        since = parse_iso(args.since)
        if since is None:
            raise HygieneError("--since must be an ISO 8601 time, got %r" % args.since)
    require_bank(args.bank)
    rules, policies_cls = _load_gateway()
    # --policy 는 그 파일 하나, 기본은 추적 + policies/local/ 병합(게이트웨이 로더와 같다, issue #187)
    policies = (policies_cls.from_file(args.policy) if args.policy
                else policies_cls.from_files(default_policy_path()))
    if args.bank not in policies.bank_names():
        raise HygieneError("bank %r is not declared in the policy file (declared: %s)" % (
            args.bank, ", ".join(sorted(policies.bank_names()))))
    policy = policies.for_bank(args.bank)
    documents = read_documents(args.documents)
    report = build_report(documents, policy, rules, bank=args.bank, since=since,
                          audit_paths=tuple(args.audit or ()))
    if args.json:
        out.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    else:
        out.write(render_table(report))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        description="태그 위생 측정: 저장된 태그를 게이트웨이 평가 함수에 다시 통과시켜 문제 유형을 센다.")
    commands = parser.add_subparsers(dest="command", required=True)

    report = commands.add_parser("report", help="문서 JSONL을 분석해 표 또는 JSON을 낸다(호스트).")
    report.add_argument("documents", help='문서 JSONL 경로, 한 줄에 {"id","created_at","tags"}. "-"는 stdin')
    report.add_argument("--bank", default=DEFAULT_BANK, help="정책을 고를 뱅크 (기본: HC_PERSONAL_BANK)")
    report.add_argument("--policy", help="정책 파일 하나 (기본 policies/bank-policies.json + policies/local/ 병합)")
    report.add_argument("--since", help="이 ISO 시각 이후 생성된 문서만 본다. 감사 줄도 같은 시각으로 거른다")
    report.add_argument("--audit", action="append",
                        help="게이트웨이 감사 로그(retain-tags 줄). 문제 태그별 출처를 집계한다. 여러 번 줄 수 있다")
    report.add_argument("--json", action="store_true", help="기계용 JSON(문서별 old -> new, 유형 포함)")
    report.set_defaults(handler=run_report)

    export = commands.add_parser(
        "export", help="운영 문서 목록을 JSONL로 낸다(컨테이너 안, 읽기 전용). scripts/tag-hygiene-export.sh가 부른다.")
    export.add_argument("--bank", default=DEFAULT_BANK, help="대상 뱅크 (기본: HC_PERSONAL_BANK)")
    export.set_defaults(handler=run_export)
    return parser


def main(argv=None):
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return args.handler(args)
    except (HygieneError, OSError, ValueError) as exc:
        if isinstance(exc, ConfigMissingError):
            sys.stderr.write("TAG_HYGIENE_FAIL - %s\n" % exc)
            return 2
        sys.stderr.write("tag_hygiene: %s\n" % exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
