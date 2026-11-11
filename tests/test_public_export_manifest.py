"""공개판 경계 매니페스트 검사기 (#423, ADR 0002).

검사기는 임시 git 리포에서 돈다. origin/main 에 기대지 않는다(CI 는 depth 1 체크아웃).
"""
import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "public-export" / "check_manifest.py"
MANIFEST = ROOT / "scripts" / "public-export" / "manifest.json"

spec = importlib.util.spec_from_file_location("check_manifest", SCRIPT)
cm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cm)


@pytest.fixture(autouse=True)
def _fake_home(tmp_path, monkeypatch):
    """main() 이 --local-rules 기본 경로(~/.config/...)로 운영자 파일을 읽지 않게 빈 임시 HOME 을 쓴다(#456)."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    *args], check=True, capture_output=True)


def make_repo(tmp_path, files, message="init"):
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q")
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)
    return repo


def write_manifest(tmp_path, **over):
    data = {"include": ["src/", "docs/", "README.md"], "exclude_history": [{"path": "ops/", "reason": "r"}],
            "history_rewrite_paths": ["docs/"],
            "deny_patterns": [{"id": "w", "regex": "for[b]idden"}],
            "accept": [], "message_rewrites": [{"regex": "for[b]idden", "replace": "fine"}]}
    data.update(over)
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


@pytest.mark.parametrize("pattern, path, want", [
    ("artifacts/arch-v2/", "artifacts/arch-v2/a.md", True),
    ("artifacts/arch-v2/", "artifacts/arch-v2-window/a.md", False),   # 디렉터리 접두는 조각 경계까지
    ("README.md", "README.md", True),
    ("README.md", "docs/README.md", False),                           # 정확한 경로
    ("artifacts/*.json", "artifacts/x.json", True),
    ("artifacts/*.json", "artifacts/sub/x.json", False),              # * 는 한 조각
    ("docs/**", "docs/a/b/c.md", True),
    ("docs/08-*-assessment-*.md", "docs/08-x-assessment-2026.md", True),
    ("tests/fixtures/policies/b?/", "tests/fixtures/policies/zz/a.json", False),
])
def test_path_matches(pattern, path, want):
    assert cm.path_matches(pattern, path) is want


def test_head_ok_when_outside_equals_excluded_and_hits_are_accepted(tmp_path):
    repo = make_repo(tmp_path, {"src/a.py": "x", "ops/run.log": "forbidden", "docs/n.md": "forbidden word"})
    m = cm.load_manifest(write_manifest(tmp_path, accept=[{"path": "docs/n.md", "pattern": "w", "reason": "r"}]))
    assert cm.check_head(repo, m) == ([], [])


def test_head_a_fails_on_unlisted_outside_path(tmp_path):
    repo = make_repo(tmp_path, {"src/a.py": "x", "stray.txt": "x"})
    pa, pb = cm.check_head(repo, cm.load_manifest(write_manifest(tmp_path)))
    assert pa == ["(a) include 밖인데 exclude_history 에 없음: stray.txt"] and pb == []


def test_head_b_fails_on_unaccepted_hit(tmp_path):
    repo = make_repo(tmp_path, {"src/a.py": "a forbidden thing"})
    _, pb = cm.check_head(repo, cm.load_manifest(write_manifest(tmp_path)))
    assert pb == ["(b) src/a.py: deny_patterns #1 x1"]


def test_history_counts_old_blobs_and_messages_after_rewrites(tmp_path):
    repo = make_repo(tmp_path, {"src/a.py": "forbidden forbidden", "ops/x": "forbidden"}, message="forbidden msg")
    (repo / "src" / "a.py").write_text("clean", encoding="utf-8")
    git(repo, "commit", "-qam", "clean up")
    m = cm.load_manifest(write_manifest(tmp_path))
    per_path, msg_hits = cm.check_history(repo, m, "HEAD")
    assert per_path == {"src/a.py": 2}          # ops/ 는 이력에서 빠지므로 세지 않는다
    assert msg_hits == 0                         # 메시지는 message_rewrites 뒤에 센다
    assert cm.check_head(repo, m) == ([], [])


def test_local_deny_prints_counts_only(tmp_path, capsys):
    repo = make_repo(tmp_path, {"src/a.py": "alpha beta alpha"})
    deny = tmp_path / "deny.txt"
    deny.write_text("# comment\nalpha\ngamma\n", encoding="utf-8")
    assert cm.main(["--repo", str(repo), "--manifest", str(write_manifest(tmp_path)), "--local-deny", str(deny)]) == 0
    out = capsys.readouterr().out
    assert "local-deny #1: 2" in out and "local-deny #2: 0" in out and "alpha" not in out


def test_manifest_literals_do_not_match_themselves():
    """내보내기 치환이 매니페스트 자신을 바꾸지 않게, 금칙 리터럴은 자기 일치하지 않는 꼴로 쓴다."""
    text = MANIFEST.read_text(encoding="utf-8")
    m = cm.load_manifest(MANIFEST)
    for pid, rx in m["_deny"]:
        assert not rx.search(text), pid


def test_this_repository_head_passes_a_and_b():
    """경계가 HEAD 와 어긋나면(새 최상위 경로, 새 금칙 적중) 여기서 먼저 깨진다."""
    assert cm.check_head(ROOT, cm.load_manifest(MANIFEST)) == ([], [])


def test_exit_codes(tmp_path):
    repo = make_repo(tmp_path, {"src/a.py": "forbidden"})
    assert cm.main(["--repo", str(repo), "--manifest", str(write_manifest(tmp_path))]) == 1
    assert cm.main(["--repo", str(repo), "--manifest", str(tmp_path / "missing.json")]) == 2


def test_history_counts_the_same_blob_at_every_included_path(tmp_path):
    """같은 내용이 제외 경로와 공개 경로에 함께 있어도 공개 경로 적중을 놓치지 않는다."""
    repo = make_repo(tmp_path, {"ops/a.txt": "forbidden", "src/a.txt": "forbidden", "docs/b.txt": "forbidden"})
    per_path, _ = cm.check_history(repo, cm.load_manifest(write_manifest(tmp_path)), "HEAD")
    assert per_path == {"docs/b.txt": 1, "src/a.txt": 1}


def test_head_reads_the_commit_not_the_working_tree(tmp_path):
    repo = make_repo(tmp_path, {"src/a.py": "forbidden"})
    (repo / "src" / "a.py").write_text("clean", encoding="utf-8")       # 커밋 안 한 수정
    _, pb = cm.check_head(repo, cm.load_manifest(write_manifest(tmp_path)))
    assert pb == ["(b) src/a.py: deny_patterns #1 x1"]


def test_history_handles_non_ascii_paths(tmp_path):
    """git 이 따옴표 처리하는 경로(한글)도 이력 보고에서 빠지지 않는다."""
    repo = make_repo(tmp_path, {"docs/한글.md": "forbidden"})
    per_path, _ = cm.check_history(repo, cm.load_manifest(write_manifest(tmp_path)), "HEAD")
    assert per_path == {"docs/한글.md": 1}


def test_symlink_target_is_checked(tmp_path):
    repo = make_repo(tmp_path, {"src/a.py": "x"})
    (repo / "src" / "link").symlink_to("forbidden-target")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "link")
    _, pb = cm.check_head(repo, cm.load_manifest(write_manifest(tmp_path)))
    assert pb == ["(b) src/link: deny_patterns #1 x1"]


def test_rename_table_names_outside_rewrite_paths_fail(tmp_path, capsys):
    repo = make_repo(tmp_path, {"docs/a.md": "owner-x here", "src/b.py": "owner-x", "src/c.py": "owner-xy"})
    table = tmp_path / "t.tsv"
    table.write_text("# c\nowner-x\tdemo-x\n", encoding="utf-8")
    man = write_manifest(tmp_path)
    assert cm.rename_table_coverage(repo, cm.load_manifest(man), table) == (1, 1, ["src/b.py"])
    assert cm.main(["--repo", str(repo), "--manifest", str(man), "--rename-table", str(table)]) == 1
    assert "owner-x" not in capsys.readouterr().out.replace("src/b.py", "")


@pytest.mark.parametrize("path, want_rc, want", [
    ("src/owner-x-notes.md", 1, (0, 1, ["src/<이름>-notes.md"])),   # history_rewrite_paths 밖: 경로 이름만으로 실패
    ("docs/owner-x-notes.md", 0, (1, 0, [])),                      # 안: inside 로 센다
])
def test_rename_table_counts_names_in_path_names(tmp_path, capsys, path, want_rc, want):
    repo = make_repo(tmp_path, {path: "no names in content\n"})
    table = tmp_path / "t.tsv"
    table.write_text("owner-x\tdemo-x\n", encoding="utf-8")
    man = write_manifest(tmp_path)
    assert cm.rename_table_coverage(repo, cm.load_manifest(man), table) == want
    assert cm.main(["--repo", str(repo), "--manifest", str(man), "--rename-table", str(table)]) == want_rc
    assert "owner-x" not in capsys.readouterr().out                # 경로를 찍어도 이름은 가린다


# ---------------------------------------------------------------- #439 이름 규칙(대소문자 무시·영숫자 경계)과 public_words

def test_rename_table_underscore_and_case_forms_outside_rewrite_paths_fail(tmp_path, capsys):
    """회귀 가드: 밑줄 접미·대문자 꼴 이름이 history_rewrite_paths 밖에 있으면 outside 로 실패한다."""
    repo = make_repo(tmp_path, {"docs/a.md": "Owner-x here", "src/b.py": "KEY = 'owner-x_bank'",
                                "src/c.py": "OWNER_X = 'OWNER-X'", "src/d.py": "xowner-x owner-x9"})
    table = tmp_path / "t.tsv"
    table.write_text("owner-x\tdemo-x\n", encoding="utf-8")
    man = write_manifest(tmp_path)
    assert cm.rename_table_coverage(repo, cm.load_manifest(man), table) == (1, 2, ["src/b.py", "src/c.py"])
    assert cm.main(["--repo", str(repo), "--manifest", str(man), "--rename-table", str(table)]) == 1
    assert "rename-table HEAD: inside 1, outside 2" in capsys.readouterr().out


def test_rename_table_masks_names_in_printed_paths_as_substrings(tmp_path, capsys):
    """출력 경로의 가림은 경계 없는 부분 문자열이다(영숫자에 붙은 이름도 찍지 않는다)."""
    repo = make_repo(tmp_path, {"src/Owner-Xtools/b.py": "owner-x"})
    table = tmp_path / "t.tsv"
    table.write_text("owner-x\tdemo-x\n", encoding="utf-8")
    man = write_manifest(tmp_path)
    assert cm.rename_table_coverage(repo, cm.load_manifest(man), table) == (0, 1, ["src/<이름>tools/b.py"])
    assert cm.main(["--repo", str(repo), "--manifest", str(man), "--rename-table", str(table)]) == 1
    assert "owner-x" not in capsys.readouterr().out.lower()


def test_rename_regex_ignores_case_and_treats_underscore_as_boundary():
    rx = cm.rename_regex(["owner-x", "owner-x-old"])
    assert rx.findall("owner-x_a OWNER-X-OLD b_Owner-x xowner-x owner-x9") == ["owner-x", "OWNER-X-OLD", "Owner-x"]


def test_public_words_is_optional_and_excluded_from_rename_coverage(tmp_path):
    assert cm.load_manifest(write_manifest(tmp_path))["public_words"] == []
    with pytest.raises(ValueError):
        cm.load_manifest(write_manifest(tmp_path, public_words="github"))
    with pytest.raises(ValueError):
        cm.load_manifest(write_manifest(tmp_path, public_words=["github", ""]))
    repo = make_repo(tmp_path, {"src/a.py": "GitHub github-owner"})
    table = tmp_path / "t.tsv"
    table.write_text("github\tdemo-gh\ngithub-owner\tdemo-gho\n", encoding="utf-8")
    m = cm.load_manifest(write_manifest(tmp_path, public_words=["GitHub"]))
    assert m["_public"] == frozenset({"github"})
    assert cm.rename_table_coverage(repo, m, table) == (0, 1, ["src/a.py"])   # 같은 이름만 빠지고 품은 이름은 센다


def test_repository_manifest_public_words():
    assert json.loads(MANIFEST.read_text(encoding="utf-8"))["public_words"] == [
        "aside", "github", "orca", "personal", "hindsight-crew", "Bkankim"]


def test_local_deny_counts_ignore_case(tmp_path, capsys):
    repo = make_repo(tmp_path, {"src/a.py": "ALPHA Alpha alpha"})
    deny = tmp_path / "deny.txt"
    deny.write_text("alpha\n", encoding="utf-8")
    assert cm.main(["--repo", str(repo), "--manifest", str(write_manifest(tmp_path)), "--local-deny", str(deny)]) == 0
    assert "local-deny #1: 3" in capsys.readouterr().out
