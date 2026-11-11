#!/usr/bin/env python3
"""공개판 경계 매니페스트 검사기 (ADR 0002, #423).

사용:
  python3 scripts/public-export/check_manifest.py [--repo .] [--manifest <경로>]
  python3 scripts/public-export/check_manifest.py --history [--rev origin/main]
  python3 scripts/public-export/check_manifest.py --local-deny <리포 밖 파일>
  python3 scripts/public-export/check_manifest.py --local-rules <리포 밖 파일>

검사:
  (a) HEAD 커밋 트리의 파일 중 include 밖 목록 = exclude_history 에 걸리는 HEAD 파일 목록.
  (b) include 안 HEAD 내용의 deny_patterns 매치가 accept 로 모두 덮인다.
  (c) --history: rev 이력의 커밋 메시지(message_rewrites 뒤)와 include 안·exclude_history 밖
      과거 blob 에서 accept 로 덮이지 않은 일반어 매치를 경로별 건수로 낸다(정보, exit 에 반영 안 함).
  --rename-table: 이름 치환 표(리포 밖)의 왼쪽 열이 include HEAD 의 내용·경로 이름에서 history_rewrite_paths 밖에 나오면 실패(건수·경로만 출력, 경로의 이름 부분은 가림).
      내보내기 치환과 같은 규칙(`rename_regex`: 대소문자 무시, 앞뒤가 [A-Za-z0-9]가 아닐 때)으로 센다. 매니페스트 public_words 와 같은 이름은 뺀다.
  --local-deny: 식별 패턴 파일(한 줄 한 정규식, '#' 주석, 대소문자 무시)로 include HEAD 를 훑어 패턴 번호별 건수만 낸다.
      패턴 내용과 맞은 문자열은 출력하지 않는다.
  --local-rules: 로컬 규칙 파일(리포 밖, 기본 ~/.config/hindsight-crew/public-export/local-rules.json, 기본 경로에
      없으면 빈 규칙). 매니페스트와 같은 모양의 deny_patterns·message_rewrites·accept 를 매니페스트 값 뒤에 합친다(#456).
      규칙 값은 출력하지 않고 키별 개수만 낸다. 형식 오류는 exit 2(항목 번호만).
exit: (a)(b) 통과 0, 실패 1, 사용 오류 2.

경로 해석(`path_matches`), 이름 표·식별 패턴 읽기(`load_rename_table`·`rename_regex`·`load_local_deny`), 이력 쌍(`history_pairs`),
로컬 규칙 읽기·합치기(`local_rules_path`·`load_local_rules`·`merge_local_rules`)는
#426 내보내기 도구(export.py)가 import 해 같은 규칙을 쓴다.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_MANIFEST = HERE / "manifest.json"
SELF_PATHS = {"scripts/public-export/manifest.json"}
DEFAULT_LOCAL_RULES = "~/.config/hindsight-crew/public-export/local-rules.json"
RULE_KEYS = ("deny_patterns", "message_rewrites", "accept")
# 항목 필드: (반드시 있는 문자열 필드, 있어도 되는 문자열 필드). 이 밖의 필드는 형식 오류다.
RULE_FIELDS = {"deny_patterns": (("id", "regex"), ()),
               "message_rewrites": (("regex", "replace"), ()),
               "accept": (("path", "pattern", "handling"), ("reason",))}
# accept handling: export-rewrite = HEAD 검사 (b)에서만 덮고 내보낸 뒤 독립 유출 검사에서는 쓰지 않는다(치환으로 없어져야 한다),
# keep = 내보낸 뒤에도 덮는다. 로컬 규칙에서는 필수이고 이 밖의 값은 형식 오류다(오타가 가장 느슨한 쪽으로 새지 않게).
ACCEPT_HANDLING = ("export-rewrite", "keep")
# 매니페스트 최상위 키 전부. 이 밖의 키(오타 등)는 거부한다(조용히 빈 규칙이 되지 않게).
MANIFEST_KEYS = frozenset({"version", "about", "include", "exclude_history", "history_rewrite_paths", "public_words",
                           "public_literals", *RULE_KEYS})


class LocalRulesError(ValueError):
    """로컬 규칙 파일 경로·형식 오류(exit 2). 메시지는 키 이름(고정 셋)·항목 번호·줄 번호만 담는다."""


def _glob_to_re(pattern: str) -> re.Pattern[str]:
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def path_matches(pattern: str, path: str) -> bool:
    """매니페스트 경로 표기 하나가 리포 상대 경로 하나에 걸리는가."""
    if pattern.endswith("/"):
        if any(c in pattern for c in "*?"):
            return _glob_to_re(pattern + "**").match(path) is not None
        return path.startswith(pattern)
    if any(c in pattern for c in "*?"):
        return _glob_to_re(pattern).match(path) is not None
    return path == pattern


def load_manifest(path: Path) -> dict:
    """경계 매니페스트. deny_patterns·message_rewrites·accept 는 선택 키다(없으면 빈 목록, #456: 값은 리포 밖
    로컬 규칙 파일에 두고 merge_local_rules 로 뒤에 합친다)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("manifest: top level must be an object")
    unknown = [k for k in data if k not in MANIFEST_KEYS]
    if unknown:
        raise ValueError(f"manifest: unknown top-level keys ({len(unknown)})")   # 키 이름은 내지 않는다
    for key in ("include", "exclude_history", "history_rewrite_paths"):
        if key not in data:
            raise ValueError(f"manifest: missing key {key!r}")
    for key in RULE_KEYS:
        data.setdefault(key, [])
    words = data.setdefault("public_words", [])   # 선택 키. 치환·표 누락 검사·유출 검사에서 빠지는 공개 식별자
    if not isinstance(words, list) or not all(isinstance(w, str) and w.strip() for w in words):
        raise ValueError("manifest: public_words must be a list of non-empty strings")
    data["_public"] = frozenset(w.strip().lower() for w in words)
    # 선택 키. 정확 일치(대소문자 구분) 문자열: 내보내기 치환이 보호하고 유출 검사가 그 범위 안 적중만 뺀다.
    # '/' 또는 '-' 가 든 값만 받고, 첫 글자나 마지막 글자가 '/'·'-' 인 값은 거부한다(URL 접두·하이픈 이름). 짧은 단어
    # 하나로 이름을 통째로 보호하지 못하게 한다(#468). 'name-'·'-name' 꼴은 그 이름의 모든 하이픈 합성어를 보호하므로
    # 안 된다. 'name-x' 처럼 구분자 뒤에 글자를 붙인 값은 막지 못한다(매니페스트는 리포 안에서 리뷰되는 파일이다).
    lits = data.setdefault("public_literals", [])
    if not isinstance(lits, list) or not all(isinstance(x, str) and x.strip() and _inner_separator(x) for x in lits):
        raise ValueError("manifest: public_literals must be a list of non-empty strings containing '/' or '-', "
                         "not starting or ending with '/' or '-'")
    _compile_rules(data)
    return data


def _inner_separator(x: str) -> bool:
    """'/' 또는 '-' 가 들어 있고 첫 글자·마지막 글자는 '/'·'-' 가 아닌가(public_literals 값 규칙)."""
    return ("/" in x or "-" in x) and x[0] not in "/-" and x[-1] not in "/-"


def _compile_rules(m: dict) -> None:
    m["_deny"] = [(d["id"], re.compile(d["regex"])) for d in m["deny_patterns"]]
    m["_msg"] = [(re.compile(r["regex"]), r["replace"]) for r in m["message_rewrites"]]


def local_rules_path(value: str | None, repo: Path) -> Path | None:
    """--local-rules 경로. 인자가 없으면 기본 경로이고 그 파일이 없으면 None(빈 규칙). 명시한 경로가 없거나
    기본 경로가 깨진 심링크면 오류(입력 누락으로 금칙 검사가 조용히 빠지지 않게). 파일은 리포 밖이어야 한다."""
    p = Path(value if value is not None else DEFAULT_LOCAL_RULES).expanduser()
    if value is None and p.is_symlink() and not p.exists():
        raise LocalRulesError(f"--local-rules 기본 경로가 깨진 심링크: {p}")
    if value is None and not p.exists():
        return None
    if not p.is_file():
        raise LocalRulesError(f"--local-rules 파일 없음: {p}")
    rp = p.resolve()
    repo = repo.resolve()
    if rp == repo or repo in rp.parents:
        raise LocalRulesError("--local-rules 는 리포 밖 파일이어야 한다")
    return rp


def _bad_item(key: str, item) -> str | None:
    """항목 하나의 형식·정규식 검사. 문제 종류(형식 오류 | 정규식 오류) 또는 None."""
    need, optional = RULE_FIELDS[key]
    if not isinstance(item, dict) or set(item) - set(need) - set(optional) or any(k not in item for k in need):
        return "형식 오류"
    if not all(isinstance(v, str) for v in item.values()) or not all(item[k] for k in need if k != "replace"):
        return "형식 오류"
    if key == "accept" and item["handling"] not in ACCEPT_HANDLING:
        return "handling 허용 값 아님"
    if "regex" in item:
        try:
            rx = re.compile(item["regex"])
            if key == "message_rewrites":
                rx.sub(item["replace"], "")      # 치환 템플릿의 그룹 참조 오류도 여기서 잡는다
        except (re.error, IndexError, OverflowError, RecursionError):   # 큰 반복 횟수·깊은 중첩도 형식 오류
            return "정규식 오류"
    return None


def load_local_rules(path: Path | None) -> dict:
    """로컬 규칙 파일 검사(lint) 뒤 {deny_patterns, message_rewrites, accept}(없는 키는 빈 목록). path 가 None 이면 빈 규칙.
    형식: 최상위 JSON 객체, 키는 RULE_KEYS 만, 값은 목록, 항목은 매니페스트와 같은 모양(RULE_FIELDS).
    실패는 LocalRulesError 하나로 모아 낸다. 메시지에는 키 이름(고정 셋)·항목 번호(1부터)·JSON 줄 번호만 담는다
    (규칙 값·모르는 키 이름·정규식 오류 문구는 담지 않는다)."""
    out = {k: [] for k in RULE_KEYS}
    if path is None:
        return out
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise LocalRulesError(f"로컬 규칙 파일 검사 실패: JSON 형식 오류: {e.lineno}번 줄")
    except UnicodeDecodeError:
        raise LocalRulesError("로컬 규칙 파일 검사 실패: UTF-8 아님")
    if not isinstance(data, dict):
        raise LocalRulesError("로컬 규칙 파일 검사 실패: 최상위가 객체가 아님")
    bad = []
    unknown = [k for k in data if k not in RULE_KEYS]
    if unknown:
        bad.append(f"키 밖 값 {len(unknown)}개")
    for key in RULE_KEYS:
        items = data.get(key, [])
        if not isinstance(items, list):
            bad.append(f"{key}: 목록이 아님")
            continue
        for i, item in enumerate(items, 1):
            kind = _bad_item(key, item)
            if kind:
                bad.append(f"{key} {i}번: {kind}")
        out[key] = items
    if bad:
        raise LocalRulesError("로컬 규칙 파일 검사 실패: " + "; ".join(bad))
    return out


def merge_local_rules(m: dict, rules: dict) -> dict:
    """로컬 규칙을 매니페스트 규칙 뒤에 합친다(목록 이어 붙이기, message_rewrites 는 이 순서로 적용). m 을 고쳐 돌려준다."""
    for key in RULE_KEYS:
        m[key] = list(m.get(key, [])) + list(rules.get(key, []))
    _compile_rules(m)
    return m


def rules_summary(rules: dict | None) -> str:
    """출력용 규칙 요약: 키별 개수만(값은 내지 않는다). None 이면 파일 없음."""
    if rules is None:
        return "없음(빈 규칙)"
    return ", ".join(f"{k} {len(rules[k])}" for k in RULE_KEYS)


def included(m: dict, path: str) -> bool:
    return any(path_matches(p, path) for p in m["include"])


def excluded(m: dict, path: str) -> bool:
    return any(path_matches(e["path"], path) for e in m["exclude_history"])


def accepted(m: dict, path: str, pattern_id: str) -> bool:
    return any(a["pattern"] == pattern_id and path_matches(a["path"], path) for a in m["accept"])


def deny_hits(m: dict, path: str, text: str) -> list[tuple[str, int]]:
    """(패턴 id, 건수) 중 accept 로 덮이지 않은 것."""
    out = []
    for pid, rx in m["_deny"]:
        n = len(rx.findall(text))
        if n and not accepted(m, path, pid):
            out.append((pid, n))
    return out


def _git(repo: Path, *args: str, binary: bool = False):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)
    return r.stdout if binary else r.stdout.decode("utf-8", "replace")


def _read_text(raw: bytes) -> str | None:
    if b"\0" in raw[:8192]:
        return None
    return raw.decode("utf-8", "replace")


def _read_blobs(repo: Path, shas: list[str]) -> dict[str, bytes]:
    """blob sha -> 내용. 작업 트리가 아니라 객체에서 읽는다(커밋되지 않은 수정은 보지 않는다)."""
    uniq = list(dict.fromkeys(shas))
    if not uniq:
        return {}
    proc = subprocess.run(["git", "-C", str(repo), "cat-file", "--batch"],
                          input="".join(f"{s}\n" for s in uniq).encode(), capture_output=True, check=True)
    out, buf, pos = {}, proc.stdout, 0
    for sha in uniq:
        header_end = buf.index(b"\n", pos)
        parts = buf[pos:header_end].split()
        if len(parts) < 3:                       # missing
            pos = header_end + 1
            continue
        start, end = header_end + 1, header_end + 1 + int(parts[2])
        if parts[1] == b"blob":
            out[sha] = buf[start:end]
        pos = end + 1
    return out


def _tree_files(repo: Path, rev: str) -> list[tuple[str, str]]:
    """rev 트리의 (경로, blob sha). 서브모듈은 뺀다."""
    rows = []
    for ent in _git(repo, "ls-tree", "-r", "-z", rev).split("\0"):
        if not ent:
            continue
        meta, _, path = ent.partition("\t")
        mode, otype, sha = meta.split()
        if otype == "blob":                      # 심링크도 대상 문자열을 내용으로 검사한다
            rows.append((path, sha))
    return rows


def check_head(repo: Path, m: dict, rev: str = "HEAD") -> tuple[list[str], list[str]]:
    rows = _tree_files(repo, rev)
    files = [f for f, _ in rows]
    problems_a, problems_b = [], []
    outside = {f for f in files if not included(m, f)}
    excl = {f for f in files if excluded(m, f)}
    for f in sorted(outside - excl):
        problems_a.append(f"(a) include 밖인데 exclude_history 에 없음: {f}")
    for f in sorted(excl - outside):
        problems_a.append(f"(a) exclude_history 가 include 안 파일에 걸림: {f}")
    targets = [(f, sha) for f, sha in rows if f not in outside and f not in SELF_PATHS]
    blobs = _read_blobs(repo, [sha for _, sha in targets])
    # 패턴 id 는 로컬 규칙 값(숨긴 맥락의 단서)이라 출력하지 않고 합친 목록의 번호(1부터, 같은 id 는 첫 번호)만 낸다(#456)
    number: dict[str, int] = {}
    for i, (pid, _) in enumerate(m["_deny"], 1):
        number.setdefault(pid, i)
    for f, sha in targets:
        text = _read_text(blobs.get(sha, b""))
        if text is None:
            continue
        for pid, n in deny_hits(m, f, text):
            problems_b.append(f"(b) {f}: deny_patterns #{number[pid]} x{n}")
    return problems_a, problems_b


def history_pairs(repo: Path, *revs: str) -> set[tuple[str, str]]:
    """revs 이력에 나온 모든 (경로, 객체 sha) 쌍. 커밋별 raw diff(-m, --root)에서 모으고 삭제는 뺀다."""
    pairs: set[tuple[str, str]] = set()
    raw = _git(repo, "log", "--raw", "-z", "--no-abbrev", "--no-renames", "-m", "--root", "--format=", *revs)
    fields = raw.split("\0")
    i = 0
    while i < len(fields):                       # -z: ":meta\0path\0" 쌍. 경로를 따옴표 처리하지 않는다
        meta = fields[i].lstrip("\n")
        if not meta.startswith(":"):
            i += 1
            continue
        path = fields[i + 1] if i + 1 < len(fields) else ""
        i += 2
        parts = meta.split()
        sha, status = parts[3], parts[4]
        if status.startswith("D") or set(sha) == {"0"}:
            continue
        pairs.add((path, sha))
    return pairs


def check_history(repo: Path, m: dict, rev: str) -> tuple[dict[str, int], int]:
    """이력의 모든 (경로, blob) 쌍을 커밋별 raw diff에서 모은다. 같은 blob이 여러 경로에 있으면 경로마다 센다."""
    pairs = {(path, sha) for path, sha in history_pairs(repo, rev)
             if path not in SELF_PATHS and included(m, path) and not excluded(m, path)}
    blobs = _read_blobs(repo, [sha for _, sha in sorted(pairs)])
    per_path: dict[str, int] = {}
    for path, sha in sorted(pairs):
        text = _read_text(blobs.get(sha, b""))
        if text is None:
            continue
        n = sum(c for _, c in deny_hits(m, path, text))
        if n:
            per_path[path] = per_path.get(path, 0) + n
    msg_hits = 0
    log = _git(repo, "log", "--format=%B%x00", rev)
    for msg in log.split("\0"):
        for rx, rep in m["_msg"]:
            msg = rx.sub(rep, msg)
        msg_hits += sum(len(rx.findall(msg)) for _, rx in m["_deny"])
    return per_path, msg_hits


def load_local_deny(deny_file: Path) -> list[re.Pattern[str]]:
    """식별 패턴 파일: 한 줄에 정규식 하나, 빈 줄과 '#' 주석은 건너뛴다. 대소문자 무시(re.I)로 적용한다."""
    pats = []
    for raw in deny_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            pats.append(re.compile(line, re.I))
    return pats


def load_rename_table(table_file: Path) -> list[tuple[str, str]]:
    """이름 치환 표: 한 줄 '<원래>\\t<데모>', 빈 줄과 '#' 주석은 건너뛴다. (원래, 데모) 목록, 데모가 없으면 ''."""
    rows = []
    for raw in table_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        name = parts[0].strip()
        if name:
            rows.append((name, parts[1].strip() if len(parts) > 1 else ""))
    return rows


def rename_regex(names: list[str]) -> re.Pattern[str]:
    """이름 치환 정규식. 이름은 대소문자 무시로 맞추고, 앞뒤가 [A-Za-z0-9]가 아닐 때만 맞는다(밑줄·하이픈은 경계).
    긴 이름 먼저. 경계 클래스는 대소문자 무시 범위 밖이라 ASCII 영숫자 그대로다.
    치환(export.Rewriter)과 HEAD 검사(rename_table_coverage) 전용이다. 독립 유출 검사는 이 정규식을 쓰지 않는다."""
    ordered = sorted(dict.fromkeys(names), key=len, reverse=True)
    return re.compile(r"(?<![A-Za-z0-9])(?i:" + "|".join(map(re.escape, ordered)) + r")(?![A-Za-z0-9])")


def mask_names(names: list[str]) -> re.Pattern[str] | None:
    """출력 경로 가림용: 이름을 경계 없는 부분 문자열, 대소문자 무시로 찾는다(긴 이름 먼저). 이름이 없으면 None."""
    ordered = sorted({n for n in names if n}, key=lambda n: (-len(n), n))
    return re.compile("|".join(map(re.escape, ordered)), re.I) if ordered else None


def local_deny_counts(repo: Path, m: dict, deny_file: Path, rev: str = "HEAD") -> list[tuple[int, int]]:
    pats = load_local_deny(deny_file)
    counts = [0] * len(pats)
    rows = [(f, sha) for f, sha in _tree_files(repo, rev) if included(m, f)]
    blobs = _read_blobs(repo, [sha for _, sha in rows])
    for _, sha in rows:
        text = _read_text(blobs.get(sha, b""))
        if text is None:
            continue
        for i, rx in enumerate(pats):
            counts[i] += len(rx.findall(text))
    return list(enumerate(counts, 1))


def rename_table_coverage(repo: Path, m: dict, table_file: Path, rev: str = "HEAD") -> tuple[int, int, list[str]]:
    """이름 치환 표 왼쪽 열이 include HEAD 의 파일 내용과 경로 이름에 몇 번 나오나:
    (history_rewrite_paths 안 건수, 밖 건수, 밖 경로). 밖 건수가 0 이어야 내보내기 치환(내용과 경로 이름)이
    HEAD 의 운영자 이름을 모두 덮는다. 이름은 출력하지 않는다(밖 경로의 이름 부분은 경계 없는 부분 문자열로 '<이름>'으로 가린다).
    매니페스트 public_words 와 같은 이름(대소문자 무시)은 치환하지 않으므로 세지 않는다."""
    public = m.get("_public", frozenset())
    left = [name for name, _ in load_rename_table(table_file) if name.lower() not in public]
    if not left:
        return 0, 0, []
    rx = rename_regex(left)
    mask = mask_names(left)
    inside = outside = 0
    out_paths = []
    rows = [(f, sha) for f, sha in _tree_files(repo, rev) if included(m, f) and f not in SELF_PATHS]
    blobs = _read_blobs(repo, [sha for _, sha in rows])
    for f, sha in rows:
        text = _read_text(blobs.get(sha, b""))
        n = (len(rx.findall(text)) if text is not None else 0) + len(rx.findall(f))   # 내용 + 경로 이름
        if not n:
            continue
        if any(path_matches(p, f) for p in m["history_rewrite_paths"]):
            inside += n
        else:
            outside += n
            out_paths.append(mask.sub("<이름>", f))
    return inside, outside, out_paths


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=".")
    ap.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    ap.add_argument("--history", action="store_true")
    ap.add_argument("--rev", default="origin/main")
    ap.add_argument("--local-deny")
    ap.add_argument("--rename-table")
    ap.add_argument("--local-rules", help=f"로컬 규칙 파일(리포 밖, 기본 {DEFAULT_LOCAL_RULES}, 기본 경로에 없으면 빈 규칙)")
    a = ap.parse_args(argv)
    repo = Path(a.repo).resolve()
    try:
        rules_path = local_rules_path(a.local_rules, repo)
        rules = load_local_rules(rules_path)
    except LocalRulesError as e:
        print(f"local rules error: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"local rules error: 읽지 못함({type(e).__name__})", file=sys.stderr)
        return 2
    try:
        m = load_manifest(Path(a.manifest))
    except (OSError, ValueError, KeyError, re.error) as e:
        print(f"manifest error: {e}", file=sys.stderr)
        return 2
    merge_local_rules(m, rules)
    print(f"로컬 규칙: {rules_summary(rules if rules_path is not None else None)}")
    pa, pb = check_head(repo, m)
    for line in pa + pb:
        print(line)
    print(f"HEAD (a) {'ok' if not pa else 'FAIL ' + str(len(pa))}  (b) {'ok' if not pb else 'FAIL ' + str(len(pb))}")
    if a.history:
        per_path, msg_hits = check_history(repo, m, a.rev)
        for path, n in sorted(per_path.items()):
            print(f"(c) blob {path}: {n}")
        print(f"(c) history {a.rev}: blob paths {len(per_path)}, blob hits {sum(per_path.values())}, "
              f"message hits {msg_hits} (정보: #426 치환 범위 입력)")
    if a.local_deny:
        for i, n in local_deny_counts(repo, m, Path(a.local_deny)):
            print(f"local-deny #{i}: {n}")
    uncovered = False
    if a.rename_table:
        inside, outside, out_paths = rename_table_coverage(repo, m, Path(a.rename_table))
        for f in out_paths:
            print(f"rename-table outside history_rewrite_paths: {f}")
        print(f"rename-table HEAD: inside {inside}, outside {outside}")
        uncovered = outside > 0
    return 0 if not (pa or pb or uncovered) else 1


if __name__ == "__main__":
    sys.exit(main())
