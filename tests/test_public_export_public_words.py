"""공개판 내보내기 매니페스트의 공개 단어와 보호 문자열 (#468).

리포 매니페스트(HEAD) 값으로 가짜 표·가짜 문서·가짜 이력을 돌린다.
  - 공개 리포 소유자 계정명은 public_words 다: 이름 표에 행이 있어도 치환하지 않는다.
  - 단독 vault 는 public_words 가 아니다: 표에 행이 있으면 치환되고, 없으면 표 누락 검사에 걸린다.
  - memory-vault(공개 MCP 서버 이름)는 public_literals 로 보호한다: mcp__memory-vault__ 꼴도 그 안이라 그대로 남는다.
  - public_literals 값 규칙: '/' 또는 '-' 가 든 값만 받는다(짧은 단어 하나로 이름을 통째로 보호하지 못하게).
"""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "scripts" / "public-export" / "manifest.json"

spec = importlib.util.spec_from_file_location("public_export_words", ROOT / "scripts" / "public-export" / "export.py")
ex = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ex)

_tspec = importlib.util.spec_from_file_location("public_export_tool_helpers_words",
                                                ROOT / "tests" / "test_public_export_tool.py")
th = importlib.util.module_from_spec(_tspec)
_tspec.loader.exec_module(th)

OWNER = "Bkankim"                     # 공개 리포 소유자 계정명(공개 리포 URL 에 이미 있다)
FAKE_TABLE = f"# 가짜 표\nvault\tdemo-vlt\n{OWNER}\tdemoowner\nzorbix-bank\tdemo-bank\n"
DOC = (f"see {OWNER}/Hindsight-Crew#251 and mcp__memory-vault__recall on memory-vault\n"
       "tags project:vault topic:vault for zorbix-bank\n")
DOC_OUT = (f"see {OWNER}/Hindsight-Crew#251 and mcp__memory-vault__recall on memory-vault\n"
           "tags project:demo-vlt topic:demo-vlt for demo-bank\n")


def head_manifest() -> dict:
    return ex.cm.load_manifest(MANIFEST)


def head_manifest_raw() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_head_manifest_owner_is_public_word_and_vault_is_not():
    m = head_manifest()
    assert OWNER.lower() in m["_public"]
    assert "vault" not in m["_public"]
    assert "memory-vault" in m["public_literals"]


def test_rewriter_keeps_owner_and_memory_vault_and_rewrites_bare_vault(tmp_path):
    m = head_manifest()
    table = tmp_path / "t.tsv"
    table.write_text(FAKE_TABLE, encoding="utf-8")
    rows = ex.load_rename_pairs(table, m["_public"])
    assert [n for _, n, _ in rows] == ["vault", "zorbix-bank"]          # 계정명 행은 공개 단어라 빠진다
    assert ex.public_literal_conflicts(m["public_literals"], [n for _, n, _ in rows]) == []
    rw = ex.Rewriter([(n, d) for _, n, d in rows], [], ["docs/"], literals=m["public_literals"])
    assert rw.text(DOC) == DOC_OUT


def _scanner(m: dict, names: list[str]):
    return ex.Scanner(m, [], list(enumerate(names, 1)), set())


@pytest.mark.parametrize("surface", ["blob", "message", "path"])
def test_scanner_exempts_memory_vault_and_counts_bare_vault(surface):
    m = head_manifest()
    sc = _scanner(m, ["vault"])
    assert sc.text(surface, "mcp__memory-vault__recall memory-vault", "docs/a.md") == 0
    assert sc.text(surface, "project:vault topic:vault", "docs/a.md") == 2
    assert sc.total() == 2


@th.needs_tools
@pytest.mark.parametrize("table_has_vault", [False, True])
def test_export_with_head_manifest_words(tmp_path, table_has_vault):
    """fixture 의 project 어휘 vault(일반어): 표에 행이 있으면 치환되고 없으면 표 누락 검사 실패.
    계정명과 memory-vault 는 어느 쪽이든 그대로 남는다."""
    src, split = th.base_source(tmp_path, b1_extra={"zorbix-bank": {"tag_vocabulary": {"project": ["vault"]}}})
    src.commit({"docs/words.md": DOC}, "words doc")
    raw = head_manifest_raw()
    manifest = dict(th.BASE_MANIFEST, public_words=raw["public_words"] + th.BASE_MANIFEST["public_words"],
                    public_literals=raw["public_literals"])
    table = th.TABLE + "vault\tdemo-vlt\n" if table_has_vault else th.TABLE
    rc, out, err = th.run_export(src, split, th.inputs(tmp_path, table=table + f"{OWNER}\tdemoowner\n",
                                                        manifest=manifest), tmp_path / "o")
    if not table_has_vault:
        assert rc == 1, (out, err)
        assert "[FAIL] 표 누락 검사: 표에 없는 값 1" in out
        return
    assert rc == 0, (out, err)
    assert "[PASS] 표 누락 검사" in out and "[PASS] 독립 유출 검사" in out
    repo = tmp_path / "o" / "repo"
    words = th.git(repo, "show", "refs/heads/main:docs/words.md")
    assert words == DOC_OUT


@pytest.mark.parametrize("value", [["memory-vault"], ["mcp__memory-vault__"], ["github.com/x/y"], ["이름-값"],
                                   ["github.com/x/한글"], ["café-ü"]])
def test_public_literals_accepts_slash_or_hyphen(tmp_path, value):
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"include": [], "exclude_history": [], "history_rewrite_paths": [],
                             "public_literals": value}), encoding="utf-8")
    assert ex.cm.load_manifest(p)["public_literals"] == value


@pytest.mark.parametrize("value", [["vault"], ["mcp__memory__"], ["memory vault"],
                                   ["zorbix-"], ["-zorbix"], ["zorbix/"], ["/zorbix"], ["-"], ["/"]])
def test_public_literals_rejects_value_without_slash_or_hyphen(tmp_path, value):
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"include": [], "exclude_history": [], "history_rewrite_paths": [],
                             "public_literals": value}), encoding="utf-8")
    with pytest.raises(ValueError, match="public_literals"):
        ex.cm.load_manifest(p)


# ---------------------------------------------------------------- [rework #468-1] public-word 면제(독립 유출 검사)
# 가짜 매니페스트의 public_words 를 덮어쓴다(HEAD 매니페스트에 가짜 값을 넣지 않는다). 가짜 계정 Zqxwvut, 가짜 짧은 이름 qxw.

FAKE_WORD = "Zqxwvut"
SURFACES = ["blob", "message", "path"]


def _word_scanner(tmp_path, names=("qxw",), local_deny=(), deny=()):
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"include": [], "exclude_history": [], "history_rewrite_paths": [],
                             "public_words": [FAKE_WORD],
                             "deny_patterns": [{"id": f"d{i}", "regex": r} for i, r in enumerate(deny)]}),
                 encoding="utf-8")
    m = ex.cm.load_manifest(p)
    return ex.Scanner(m, [__import__("re").compile(r) for r in local_deny], list(enumerate(names, 1)), set())


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("text", [f"owner {FAKE_WORD} here", f"x/{FAKE_WORD}/y", "zQXWVUT", f"({FAKE_WORD})"])
def test_name_hit_inside_public_word_is_exempt(tmp_path, surface, text):
    sc = _word_scanner(tmp_path)
    assert sc.text(surface, text, "docs/a.md") == 0
    assert sc.total() == 0
    assert sc.exempt == {(surface, "public-word"): 1}       # 표 행 번호 없는 라벨


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("text", ["qxw alone", f"{FAKE_WORD}x", f"x{FAKE_WORD}", f"{FAKE_WORD}9", f"{FAKE_WORD} and qxw"])
def test_name_hit_outside_whole_public_word_fails(tmp_path, surface, text):
    sc = _word_scanner(tmp_path)
    n = sc.text(surface, text, "docs/a.md")
    exempt = 1 if text.endswith(" and qxw") else 0          # 온전한 출현 안 적중만 면제, 따로 쓴 이름은 실패
    assert n == 1 and sc.total() == 1
    assert sc.exempt.get((surface, "public-word"), 0) == exempt


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("sep", ["-", "_"])
def test_name_variant_crossing_public_word_edge_fails(tmp_path, surface, sep):
    """하이픈 이름의 원래 철자와 '_' 변형이 공개 단어 끝을 걸치면 실패다(하이픈을 없앤 꼴은 공개 단어에 영숫자가 붙어
    온전한 출현이 아니므로 위 영숫자 붙음 사례가 다룬다)."""
    sc = _word_scanner(tmp_path, names=("vut-ab",))
    assert sc.text(surface, f"{FAKE_WORD}{sep}ab", "docs/a.md") == 1
    assert sc.total() == 1 and not sc.exempt


@pytest.mark.parametrize("surface", SURFACES)
def test_local_deny_and_deny_patterns_on_public_word_are_not_exempt(tmp_path, surface):
    sc = _word_scanner(tmp_path, local_deny=[r"(?i)zqxwvut"], deny=[r"(?i)vut-zz"])
    assert sc.text(surface, f"{FAKE_WORD}-zz", "docs/a.md") == 2      # local-deny 1 + deny_patterns 1
    assert sc.counts == {(surface, "local-deny"): 1, (surface, "deny_patterns"): 1}
    assert sc.exempt == {(surface, "public-word"): 1}                 # 이름 적중만 면제


def test_name_hit_in_base64_decoded_text_fails(tmp_path):
    import base64
    decoded = (f"padding text with {FAKE_WORD} inside, " * 4).encode("utf-8")
    chunk = base64.b64encode(decoded).decode("ascii")
    assert len(chunk) > ex.B64_MIN_CHUNK
    sc = _word_scanner(tmp_path)
    assert sc.text("blob", f"data = \"{chunk}\"\n", "tests/fixtures/x.json") == 4
    assert sc.counts == {("blob", "rename-table:1"): 4}               # 해독본에는 public-word 면제를 쓰지 않는다


def test_public_word_exemption_needs_manifest_word(tmp_path):
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"include": [], "exclude_history": [], "history_rewrite_paths": []}), encoding="utf-8")
    sc = ex.Scanner(ex.cm.load_manifest(p), [], [(1, "qxw")], set())
    assert sc.text("blob", f"owner {FAKE_WORD}", "docs/a.md") == 1
