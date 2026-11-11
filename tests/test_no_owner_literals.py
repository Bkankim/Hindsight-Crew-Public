"""운영자 리터럴 회귀 게이트 (issue #188).

git 추적 런타임 파일에 운영자 머신 전용 리터럴이 다시 들어오면 실패한다. 범용 패키지가 시간이 지나며
운영자 전용으로 굳는 것을 막는다.

- 검사 대상: `git ls-files`(추적 파일만). gitignore된 설치별 파일(.env.*.local, secrets/,
  policies/local/)에는 운영자 값이 정상이라 보지 않는다.
- 제외: tests/ docs/ bench/ artifacts/ (최상위), fixtures/ (어느 깊이든), *.md.
- 허용: tests/fixtures/owner_literal_allowlist.txt 한 곳, `경로:패턴:이유`. 경로+패턴으로 매칭한다.
  허용 항목은 주석·docstring 줄에만 걸린다. 실행 코드 줄은 허용 목록에 넣지 말고 설정값으로 바꾼다.
- 허용 목록에 쓰이지 않는 항목이 있으면 실패한다(허용 목록이 썩지 않게).

패턴 세 갈래 (#470):
- PATTERNS: 운영 머신 값 리터럴(옛 줄 그대로). 공개판에서는 내보내기가 값을 바꿔 뜻이 옅어진다.
- STRUCTURAL_PATTERNS: 값이 아니라 모양을 막는다 - 공개판에서도 그대로 뜻이 있다.
  홈 절대 경로(`/Users/<이름>`, 이름이 무엇이든)와 리터럴 launchd 라벨(`<역DNS>.hindsight-<작업>` - 접두는
  `__PREFIX__`·`HC_LAUNCHD_PREFIX` 에서 온다).
- 리포 밖 패턴 파일: 환경 변수 HC_OWNER_LITERALS_LOCAL 이 가리키는 파일(한 줄 한 정규식, '#' 주석, 대소문자 무시 -
  scripts/public-export/check_manifest.py `load_local_deny` 와 같은 형식). 운영자 실제 값은 리포에 적지 않고 여기서만
  읽는다. 허용 목록이 없고(목록이 리포 안이라 값이 샌다), 실패 메시지에는 경로·줄·패턴 번호만 낸다. 변수가 없으면 이 갈래는 비어 있다.
"""
from __future__ import annotations

import ast
import importlib.util
import io
import os
import re
import subprocess
import tokenize
from pathlib import Path
from typing import Iterable

import pytest

ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = ROOT / "tests" / "fixtures" / "owner_literal_allowlist.txt"

PATTERNS = [
    r"/Users/demoalt",
    r"personal-demo",
    r"com\.demoalt",
    r"""identity.{0,6}['"]demo['"]""",
    r"/opt/homebrew/bin/uv",
    r"/usr/bin/python3",
]
STRUCTURAL_PATTERNS = [
    r"/Users/[A-Za-z0-9._-]+",
    # 대소문자 무시(설치기 PREFIX_RE 는 대문자를 받는다). 한 마디 접두(`acme.hindsight-x`)는 잡지 않는다 - 설치기
    # 기본 접두 자체(`com.hindsight-crew`)와 `$VAR.hindsight-x` 셸 참조가 같은 모양이라 오탐이 된다.
    r"(?i)\b[a-z][a-z0-9-]*(?:\.[a-z0-9-]+)+\.hindsight-[a-z]",
]
ALL_PATTERNS = PATTERNS + STRUCTURAL_PATTERNS
LOCAL_PATTERNS_ENV = "HC_OWNER_LITERALS_LOCAL"
EXCLUDED_TOP = {"tests", "docs", "bench", "artifacts"}
EXCLUDED_ANY = {"fixtures"}


def _is_excluded(rel: str) -> bool:
    parts = rel.split("/")
    return (
        parts[0] in EXCLUDED_TOP
        or bool(EXCLUDED_ANY & set(parts[:-1]))
        or rel.endswith(".md")
    )


def _check_manifest():
    spec = importlib.util.spec_from_file_location(
        "check_manifest_for_owner_literals", ROOT / "scripts" / "public-export" / "check_manifest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def local_patterns(environ=os.environ) -> list[re.Pattern[str]]:
    """리포 밖 패턴 파일의 정규식. 변수가 없으면 빈 목록, 변수가 가리키는 파일이 없으면 FileNotFoundError(설정 오류)."""
    path = environ.get(LOCAL_PATTERNS_ENV)
    if not path:
        return []
    return _check_manifest().load_local_deny(Path(path).expanduser())


def tracked_runtime_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout
    return sorted(p for p in out.split("\0") if p and not _is_excluded(p))


def _split_lines(source: str) -> list[str]:
    """ast/tokenize와 같은 규칙(\\r\\n, \\r, \\n만 줄바꿈)으로 나눈다.

    str.splitlines()는 \\f \\v \\x85 U+2028 등에서도 갈라 줄 번호가 어긋난다.
    """
    return re.split(r"\r\n|\r|\n", source)


def _python_comment_or_docstring_lines(source: str) -> set[int] | None:
    """1-based 줄 번호 집합: 주석 줄 + docstring이 걸친 줄. 파싱 실패면 None."""
    # tokenize는 \n으로만 줄을 나누므로 \r\n·\r을 \n으로 통일해 ast와 줄 번호를 맞춘다.
    source = "\n".join(_split_lines(source))
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    lines: set[int] = set()
    src_lines = _split_lines(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr):
                v = body[0].value
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    first, last = v.lineno, v.end_lineno
                    # col_offset은 UTF-8 바이트 오프셋이다. 같은 줄에 코드가 섞인 첫·끝 줄은 제외한다.
                    head = src_lines[first - 1].encode("utf-8")[: v.col_offset]
                    tail = src_lines[last - 1].encode("utf-8")[v.end_col_offset:]
                    lines.update(range(first, last + 1))
                    if head.strip():
                        lines.discard(first)
                    if tail.strip():
                        lines.discard(last)
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        # 줄 전체가 주석인 줄만. 코드 뒤 인라인 주석 줄은 코드가 섞여 있으니 제외한다.
        if tok.type == tokenize.COMMENT and tok.line.lstrip().startswith("#"):
            lines.add(tok.start[0])
    return lines


def is_comment_or_docstring_line(rel: str, source: str, lineno: int) -> bool:
    if lineno == 1 and source.startswith("#!"):
        return False  # shebang은 주석이 아니라 실행 인터프리터 지정이다
    if rel.endswith(".py"):
        lines = _python_comment_or_docstring_lines(source)
        return lines is not None and lineno in lines
    text = _split_lines(source)[lineno - 1]
    return text.lstrip().startswith("#")


def runtime_sources() -> Iterable[tuple[str, str, bool]]:
    """(경로, 본문, 심링크 여부). 심링크의 본문은 링크 대상 문자열이다(git은 그것을 blob으로 저장한다)."""
    for rel in tracked_runtime_files():
        path = ROOT / rel
        if path.is_symlink():
            yield rel, os.readlink(path), True
        elif path.is_file():
            # 디코드 실패로 파일 전체가 검사에서 빠지지 않게 대체 문자로 읽는다(패턴은 ASCII).
            yield rel, path.read_text(encoding="utf-8", errors="replace"), False


def scan_sources(sources: Iterable[tuple[str, str, bool]], patterns) -> list[tuple[str, str, int, str, bool]]:
    """(경로, 패턴, 줄, 줄 내용, 주석·docstring 여부) 히트. patterns: 문자열 또는 컴파일된 정규식."""
    hits = []
    for rel, source, is_link in sources:
        if is_link:
            # 링크 대상은 주석일 수 없으니 히트는 항상 실행 줄로 본다.
            hits.extend((rel, pat, 1, source, False) for pat in patterns if re.search(pat, source))
            continue
        for lineno, line in enumerate(_split_lines(source), 1):
            for pat in patterns:
                if re.search(pat, line):
                    hits.append((rel, pat, lineno, line.strip(),
                                 is_comment_or_docstring_line(rel, source, lineno)))
    return hits


def scan(patterns=None) -> list[tuple[str, str, int, str, bool]]:
    """추적 런타임 파일 전체 히트(기본: PATTERNS + STRUCTURAL_PATTERNS)."""
    return scan_sources(runtime_sources(), ALL_PATTERNS if patterns is None else patterns)


def violations(hits, allowed: set[tuple[str, str]]) -> list[str]:
    """허용 목록(경로, 패턴)에 없거나, 있어도 실행 코드 줄인 히트."""
    bad = []
    for rel, pat, lineno, line, is_comment in hits:
        if (rel, pat) not in allowed:
            bad.append(f"{rel}:{lineno}: {line}")
        elif not is_comment:
            bad.append(f"{rel}:{lineno}: [허용 목록에 있어도 실행 코드 줄은 불가] {line}")
    return bad


def local_violations(sources: Iterable[tuple[str, str, bool]], patterns: list[re.Pattern[str]]) -> list[str]:
    """리포 밖 패턴 히트. 값이 새지 않게 줄 내용 대신 패턴 번호만 낸다. 허용 목록은 없다."""
    # 번호는 파일 안 위치로 매긴다. 같은 정규식이 두 줄이면 re.compile 캐시가 같은 객체를 돌려주므로 객체로 찾지 않는다.
    sources = list(sources)
    hits = [(rel, lineno, i) for i, pat in enumerate(patterns, 1)
            for rel, _, lineno, _, _ in scan_sources(sources, [pat])]
    return [f"{rel}:{lineno}: local#{i}" for rel, lineno, i in sorted(hits)]


def parse_allowlist(text: str, name: str = "allowlist") -> list[tuple[str, str, str]]:
    entries = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        parts = raw.split(":", 2)
        assert len(parts) == 3, f"{name}:{lineno}: `경로:패턴:이유` 형식이 아니다: {raw!r}"
        entries.append((parts[0], parts[1], parts[2].strip()))
    return entries


def load_allowlist() -> list[tuple[str, str, str]]:
    return parse_allowlist(ALLOWLIST.read_text(encoding="utf-8"), str(ALLOWLIST.relative_to(ROOT)))


@pytest.mark.parametrize("pattern", ALL_PATTERNS)
def test_no_owner_literal(pattern):
    allowed = {(p, pat) for p, pat, _ in load_allowlist()}
    bad = violations(scan([pattern]), allowed)
    assert not bad, (
        f"운영자 리터럴 /{pattern}/ 발견. 실행 코드면 설정값으로 바꾸고, 주석·docstring이면 "
        f"tests/fixtures/owner_literal_allowlist.txt에 `경로:패턴:이유`로 허용:\n" + "\n".join(bad)
    )


def test_no_local_owner_literal():
    """리포 밖 패턴 파일(HC_OWNER_LITERALS_LOCAL)의 값이 추적 런타임 파일에 없다. 변수가 없으면 검사할 패턴이 없다."""
    bad = local_violations(runtime_sources(), local_patterns())
    assert not bad, (f"{LOCAL_PATTERNS_ENV} 패턴 발견(값은 생략). 실행 코드면 설정값으로, 주석이면 문구를 바꾼다:\n"
                     + "\n".join(bad))


def test_allowlist_entries_are_well_formed_and_used():
    entries = load_allowlist()
    used = {(rel, pat) for rel, pat, _, _, _ in scan()}
    problems = []
    for path, pat, reason in entries:
        if pat not in ALL_PATTERNS:
            problems.append(f"알 수 없는 패턴: {path}:{pat}")
        if not reason:
            problems.append(f"이유 없음: {path}:{pat}")
        if (path, pat) not in used:
            problems.append(f"사용되지 않는 허용 항목(삭제할 것): {path}:{pat}")
    dup = [e[:2] for e in entries]
    problems += [f"중복 항목: {d}" for d in {d for d in dup if dup.count(d) > 1}]
    assert not problems, "\n".join(problems)


def test_allowlist_paths_are_not_excluded_dirs():
    # 제외 경로는 어차피 검사하지 않으니 허용 항목이 있으면 썩은 항목이다.
    stale = [p for p, _, _ in load_allowlist() if _is_excluded(p)]
    assert not stale, stale


@pytest.mark.parametrize("pat,line,expected", [
    (PATTERNS[0], "x = '/Users/demoalt/y'", True),
    (PATTERNS[0], "x = '/Users/alice/y'", False),
    (PATTERNS[1], "bank = 'personal-demo'", True),
    (PATTERNS[1], "bank = 'personal-alice'", False),
    (PATTERNS[2], "label com.demoalt.hindsight-rerank", True),
    (PATTERNS[2], "label com.example.rerank", False),
    (PATTERNS[3], 'identity = "demo"', True),
    (PATTERNS[3], "identity: 'demo'", True),
    (PATTERNS[3], 'identity = "demox"', False),
    (PATTERNS[4], "/opt/homebrew/bin/uv run", True),
    (PATTERNS[4], "/usr/local/bin/uv run", False),
    (PATTERNS[5], "#!/usr/bin/python3", True),
    (PATTERNS[5], "#!/usr/bin/env python3", False),
    (STRUCTURAL_PATTERNS[0], "x = '/Users/alice/y'", True),
    (STRUCTURAL_PATTERNS[0], "cd /Users/Shared", True),
    (STRUCTURAL_PATTERNS[0], "x = '/Users/<이름>/y'", False),
    (STRUCTURAL_PATTERNS[0], "x = os.path.expanduser('~/y')", False),
    (STRUCTURAL_PATTERNS[1], "launchctl print gui/501/org.example.hindsight-rerank", True),
    (STRUCTURAL_PATTERNS[1], "~/Library/LaunchAgents/io.demo.ops.hindsight-model-refresh.plist", True),
    (STRUCTURAL_PATTERNS[1], "launchctl kickstart gui/501/com.Acme.hindsight-rerank", True),
    (STRUCTURAL_PATTERNS[1], 'f="$PREFIX.hindsight-rerank"', False),
    (STRUCTURAL_PATTERNS[1], 'launchctl print "gui/501/$HC_LAUNCHD_PREFIX.hindsight-rerank"', False),
    (STRUCTURAL_PATTERNS[1], "__PREFIX__.hindsight-rerank.plist.example", False),
    (STRUCTURAL_PATTERNS[1], "LABEL_SUFFIX = '.hindsight-rerank'", False),
    (STRUCTURAL_PATTERNS[1], "~/Library/Logs/hindsight-rerank", False),
])
def test_pattern_self_check(pat, line, expected):
    assert bool(re.search(pat, line)) is expected


@pytest.mark.parametrize("rel,source,lineno,expected", [
    ("a.py", '"""doc\npersonal-demo\n"""\nx = 1\n', 2, True),
    ("a.py", 'x = "personal-demo"\n', 1, False),
    ("a.py", "# personal-demo\nx = 1\n", 1, True),
    ("a.py", 'x = 1  # personal-demo\n', 1, False),
    ("a.py", 'def f():\n    """personal-demo"""\n    return 1\n', 2, True),
    ("a.py", 'x = """\npersonal-demo\n"""\n', 2, False),
    ("a.sh", "# com.demoalt\n", 1, True),
    ("a.sh", "launchctl x com.demoalt\n", 1, False),
    ("a.sh", "echo hi # com.demoalt\n", 1, False),
    ("a.py", 'def f(b="personal-demo"): """doc"""\n', 1, False),
    ("a.py", '"""a\nb"""; X = "personal-demo"\n', 2, False),
    ("a.py", 'x = 1; """doc\npersonal-demo\n"""\n', 2, False),
    ("a.py", '"""a\npersonal-demo\n"""\n', 2, True),
    ("a.py", "#!/usr/bin/python3\nx = 1\n", 1, False),
    ("a.sh", "#!/usr/bin/python3\n", 1, False),
    ("a.sh", "echo hi\n#!/usr/bin/python3\n", 2, True),
    ("a.py", '"""a\rpersonal-demo\r"""\rx = 1\r', 2, True),
    ("a.py", 'x = "a"\r# personal-demo\r', 2, True),
    ("a.sh", "echo hi\r# com.demoalt\r", 2, True),
    ("a.sh", "echo hi\rcom.demoalt\r", 2, False),
    ("a.py", '"""a\n\fb"""\nx = "personal-demo"\n', 3, False),
])
def test_comment_classifier(rel, source, lineno, expected):
    assert is_comment_or_docstring_line(rel, source, lineno) is expected


@pytest.mark.parametrize("text,ok", [
    ("# 주석\n\na/b.py:personal-demo:이유\n", True),
    ("a/b.py:personal-demo:이유:콜론 포함\n", True),
    ("a/b.py personal-demo 이유\n", False),
    ("a/b.py:personal-demo\n", False),
])
def test_parse_allowlist_format(text, ok):
    if ok:
        assert parse_allowlist(text)
    else:
        with pytest.raises(AssertionError, match=r"allowlist:\d+: "):
            parse_allowlist(text)


@pytest.mark.parametrize("rel,excluded", [
    ("tests/x.py", True), ("docs/a.txt", True), ("bench/x.py", True), ("artifacts/x", True),
    ("a/fixtures/x.json", True), ("README.md", True), ("gateway/x.md", True),
    ("gateway/server.py", False), ("policies/bank-policies.json", False),
    ("scripts/tests/x.sh", False),
])
def test_exclusion_rules(rel, excluded):
    assert _is_excluded(rel) is excluded


# ------------------------------------------------------------------ 가드가 무엇을 막는지 (#470, 합성 입력)

SYNTHETIC_VIOLATIONS = [
    ("scripts/x.sh", "launchctl print gui/501/org.example.hindsight-rerank\n", False, STRUCTURAL_PATTERNS[1]),
    ("bootstrap", 'PLIST="$HOME/Library/LaunchAgents/io.demo.hindsight-rerank.plist"\n', False, STRUCTURAL_PATTERNS[1]),
    ("gateway/x.py", 'ROOT = "/Users/alice/hc"\n', False, STRUCTURAL_PATTERNS[0]),
    ("scripts/y.sh", "# see /Users/alice/hc\n", False, STRUCTURAL_PATTERNS[0]),     # 주석이어도 허용 목록 없이는 실패
    ("scripts/link", "/Users/alice/hc/target", True, STRUCTURAL_PATTERNS[0]),
]
SYNTHETIC_CLEAN = [
    ("scripts/x.sh", 'launchctl print "gui/$(id -u)/$HC_LAUNCHD_PREFIX.hindsight-rerank"\n', False),
    ("rerank-server/launchd/__PREFIX__.hindsight-rerank.plist.example", "<string>__PREFIX__.hindsight-rerank</string>\n", False),
    ("gateway/x.py", 'ROOT = os.path.expanduser("~/hc")\n', False),
    ("scripts/y.sh", "# see <운영 체크아웃>/hc\n", False),
]


@pytest.mark.parametrize("rel,source,is_link,pat", SYNTHETIC_VIOLATIONS, ids=[v[0] for v in SYNTHETIC_VIOLATIONS])
def test_guard_fails_on_a_synthetic_violation(rel, source, is_link, pat):
    hits = scan_sources([(rel, source, is_link)], ALL_PATTERNS)
    assert pat in {h[1] for h in hits}
    assert violations(hits, set()), "합성 위반이 가드를 통과했다"


def test_guard_passes_clean_synthetic_input():
    assert scan_sources([(r, s, l) for r, s, l in SYNTHETIC_CLEAN], ALL_PATTERNS) == []


def test_guard_allowlist_covers_comment_lines_only():
    pat = STRUCTURAL_PATTERNS[1]
    comment = [("scripts/x.sh", "# label org.example.hindsight-rerank\n", False)]
    code = [("scripts/x.sh", "launchctl print gui/501/org.example.hindsight-rerank\n", False)]
    assert violations(scan_sources(comment, [pat]), {("scripts/x.sh", pat)}) == []
    assert violations(scan_sources(code, [pat]), {("scripts/x.sh", pat)}) != []


def test_local_patterns_from_an_out_of_repo_file(tmp_path):
    f = tmp_path / "owner-literals.txt"
    f.write_text("# 합성 패턴\n\nacme-corp\nzz[0-9]{3}\n", encoding="utf-8")
    pats = local_patterns({LOCAL_PATTERNS_ENV: str(f)})
    assert [p.pattern for p in pats] == ["acme-corp", "zz[0-9]{3}"]
    bad = local_violations([("gateway/x.py", "x = 1\nname = 'ACME-Corp ops'\n", False),
                            ("scripts/y.sh", "echo zz123\n", False)], pats)
    assert bad == ["gateway/x.py:2: local#1", "scripts/y.sh:1: local#2"]       # 대소문자 무시, 줄 내용(값)은 내지 않는다
    assert local_violations([("gateway/x.py", "name = 'demo'\n", False)], pats) == []


def test_local_pattern_numbers_follow_file_position_even_for_duplicates(tmp_path):
    f = tmp_path / "owner-literals.txt"
    f.write_text("acme-corp\nzz[0-9]{3}\nacme-corp\n", encoding="utf-8")
    pats = local_patterns({LOCAL_PATTERNS_ENV: str(f)})
    assert local_violations([("gateway/x.py", "name = 'acme-corp'\n", False)], pats) == [
        "gateway/x.py:1: local#1", "gateway/x.py:1: local#3"]


def test_local_patterns_absent_or_broken_config(tmp_path):
    assert local_patterns({}) == [] and local_patterns({LOCAL_PATTERNS_ENV: ""}) == []
    with pytest.raises(FileNotFoundError):
        local_patterns({LOCAL_PATTERNS_ENV: str(tmp_path / "missing.txt")})
