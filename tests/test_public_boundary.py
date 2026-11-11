"""공개판 경계: 공개용으로 쓴 것만 싣는다 (#455, ADR 0002).

- 운영 전용 문서·표(런북, 계획, 전환 창, 리허설 기록, 운영자 재태깅 표)는 include 밖이고 exclude_history 안이다.
- 공개판에 실리는 scripts/ 최상위 항목은 scripts/README.md 의 세 분류 중 정확히 하나에 있고, 표의 항목은 실제로 실린다.
- 공개 문서의 상대 링크는 공개판에 실리는 파일을 가리킨다.
운영판 HEAD 와 공개판 후보 클론 양쪽에서 같은 결과가 나와야 한다(후보에는 뺀 파일이 없다).
"""
import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "public-export" / "check_manifest.py"
MANIFEST = ROOT / "scripts" / "public-export" / "manifest.json"
SCRIPTS_README = ROOT / "scripts" / "README.md"

spec = importlib.util.spec_from_file_location("check_manifest", SCRIPT)
cm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cm)

CATEGORIES = ("설치자용", "운영자 전용", "공개판 관리용")


@pytest.fixture(scope="module")
def manifest():
    return cm.load_manifest(MANIFEST)


def _in_git_repo() -> bool:
    try:
        return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--is-inside-work-tree"],
                              capture_output=True).returncode == 0
    except OSError:                              # git 자체가 없다
        return False


IN_GIT = _in_git_repo()
needs_git = pytest.mark.skipif(not IN_GIT, reason="git 리포가 아닌 사본(추적 파일 목록이 없다)")


def tracked(prefix: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--", prefix],
                         capture_output=True, check=True).stdout.decode()
    return [p for p in out.split("\0") if p]


def public(m, path: str) -> bool:
    return cm.included(m, path) and not cm.excluded(m, path)


@pytest.mark.parametrize("path", [
    "docs/plans/2026-01-01-demo-plan.md",
    "docs/plans/demo-checklist.md",
    "docs/deploy-procedure.md",
    "docs/RUNBOOK.md",
    "docs/arch-v2-phase1-window.md",
    "docs/upgrade-v1.1-window.md",
    "docs/demo-next-window.md",
    "docs/public-export-rehearsals.md",
    "docs/analysis/2026-01-demo-analysis.md",
    "scripts/retag-map.json",
])
def test_operator_only_paths_are_outside_include_and_excluded_from_history(manifest, path):
    assert not cm.included(manifest, path), path
    assert cm.excluded(manifest, path), path


@pytest.mark.parametrize("path", [
    "docs/operating-guide.md",
    "docs/operations.md",
    "docs/public-export.md",
    "docs/adr/0002-public-edition-is-a-one-way-export.md",
    "scripts/README.md",
    "scripts/public-export/export.py",
    "scripts/install/hc_install.py",
    "scripts/launchd/__PREFIX__.hindsight-model-refresh.plist.example",
])
def test_public_paths_are_included(manifest, path):
    assert public(manifest, path), path


@pytest.mark.parametrize("path", ["docs/new-note.md", "scripts/new_tool.py",
                                  "scripts/launchd/com.example.hindsight-model-refresh.plist.example"])
def test_new_docs_and_scripts_are_not_public_until_listed(manifest, path):
    """docs/·scripts/ 는 디렉터리째 담지 않는다. 새 파일은 include 에 적어야 실린다(안 적으면 HEAD 검사 (a) 가 실패)."""
    assert not cm.included(manifest, path), path


def readme_sections() -> dict[str, list[str]]:
    sections, current = {}, None
    for line in SCRIPTS_README.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
        elif current and line.startswith("| `"):
            first = line.split("|")[1]
            sections[current] += re.findall(r"`([^`]+)`", first)
    return sections


def top_level_entry(path: str) -> str:
    rest = path[len("scripts/"):]
    head, sep, _ = rest.partition("/")
    return head + "/" if sep else head


def test_scripts_readme_has_exactly_the_three_categories():
    assert tuple(readme_sections()) == CATEGORIES


@needs_git
def test_every_public_scripts_entry_is_in_exactly_one_category(manifest):
    sections = readme_sections()
    listed = [e for c in CATEGORIES for e in sections[c]]
    assert len(listed) == len(set(listed)), "같은 항목이 두 번 나온다"
    entries = {top_level_entry(p) for p in tracked("scripts") if public(manifest, p)}
    assert entries, "scripts/ 공개 항목이 없다"
    assert sorted(entries - set(listed)) == [], "scripts/README.md 분류에 없는 공개 항목"
    assert sorted(set(listed) - entries) == [], "분류에 있는데 공개판에 실리지 않는 항목"


PUBLIC_DOCS = ["CONTEXT.md", "docs/operating-guide.md", "docs/operations.md", "docs/public-export.md",
               "scripts/README.md"]


ADR_DOCS = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "docs" / "adr").glob("*.md"))


@needs_git
@pytest.mark.parametrize("doc", PUBLIC_DOCS + ADR_DOCS)
def test_public_doc_relative_links_point_at_public_files(manifest, doc):
    text = (ROOT / doc).read_text(encoding="utf-8")
    bad = []
    for target in re.findall(r"\]\(([^)\s]+)\)", text):
        if re.match(r"[a-z]+:", target) or target.startswith("#"):
            continue
        rel = (Path(doc).parent / target.split("#", 1)[0]).as_posix()
        norm = Path(rel).as_posix()
        parts, outside = [], False
        for seg in norm.split("/"):
            if seg == "..":
                if not parts:                    # 리포 루트 밖으로 나가는 링크는 공개판에서 닫히지 않는다
                    outside = True
                    break
                parts.pop()
            elif seg not in ("", "."):
                parts.append(seg)
        if outside:
            bad.append(target)
            continue
        path = "/".join(parts)
        is_dir = (ROOT / path).is_dir()
        ok = (ROOT / path).exists() and (
            any(public(manifest, p) for p in tracked(path)) if is_dir else public(manifest, path))
        if not ok:
            bad.append(target)
    assert bad == []
