"""공개판 내보내기 이력의 정책 자유 문장·리허설 절·해독표 결정적 치환 (#459).

가짜 뱅크·가짜 문장·가짜 문서·가짜 매니페스트만 쓴다. 실제 정책 문장·이름은 없다.
  - 정책 JSON 판본(policies/bank-policies.json, tests/fixtures/policies/**): 뱅크별·defaults 의 자유 문장 필드를
    HEAD 데모 fixture 의 같은 뱅크 값으로, 없으면 고정 중립 문구로. 나머지 바이트는 그대로.
  - docs/public-export.md 판본: '## 리허설 기록' 절만 지운다(코드 블록 안 '## ' 줄에서 멈추지 않는다).
  - scripts/public-export/manifest.json 판본: deny_patterns·message_rewrites·accept 값을 빈 목록으로.
"""
import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "public-export" / "export.py"

spec = importlib.util.spec_from_file_location("public_export_freetext", SCRIPT)
ex = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ex)

_tspec = importlib.util.spec_from_file_location("public_export_tool_helpers", ROOT / "tests" / "test_public_export_tool.py")
th = importlib.util.module_from_spec(_tspec)
_tspec.loader.exec_module(th)

FAKE_A = "FAKE-PRIVATE-SENTENCE alpha: 가짜 맥락 문장 \"인용\" \\ 끝"
FAKE_B = "FAKE-PRIVATE-SENTENCE orphan"
FAKE_D = "FAKE-PRIVATE-SENTENCE defaults"
FIXTURE_A = "Demo instructions for demo-alpha."
FIXTURE = {"defaults": {"instructions_text": None},
           "banks": {"demo-alpha": {"instructions_text": FIXTURE_A}, "demo-plain": {}}}


def _lit(v) -> str:
    return json.dumps(v, ensure_ascii=False)


def _policy_text(a=FAKE_A, b=FAKE_B, d=FAKE_D) -> str:
    # 들여쓰기 3칸·키 순서 섞음·탭·비ASCII·이스케이프: 바이트 비교가 재직렬화와 구별되게
    return ("{\n"
            '   "banks" : {\n'
            '      "demo-orphan": {"tags_required": true,\t"instructions_text": ' + _lit(b) + ', "z": 1},\n'
            '      "demo-alpha": {\n'
            '         "instructions_text":' + _lit(a) + ',\n'
            '         "tag_prefixes": ["kind"], "note": "남는 값 \\u00e9"\n'
            "      },\n"
            '      "demo-plain": {"instructions_text": null}\n'
            "   },\n"
            '   "defaults": {"instructions_text": ' + _lit(d) + ', "tags_required": false}\n'
            "}\n")


# ---------------------------------------------------------------- 대상 필드 상수(스키마 근거)

def test_freetext_fields_constant_is_the_schema_string_fields():
    """gateway/bank_policies.py 의 키 파서 중 자유 문자열(검증이 길이뿐인 것)은 instructions_text 하나다.
    스키마에 자유 문장 필드가 늘면 이 테스트가 깨져 상수를 같이 고치게 한다."""
    from gateway import bank_policies as bp
    free = sorted(k for k, f in bp._VALIDATORS.items() if f is bp._as_instructions)
    assert free == ["instructions_text"]
    assert ex.FREETEXT_FIELDS == ("instructions_text",)
    assert len(ex.FREETEXT_NEUTRAL.encode("utf-8")) <= bp.INSTRUCTIONS_MAX_BYTES
    assert ex.FREETEXT_PATHS == ("policies/bank-policies.json", "tests/fixtures/policies/")


@pytest.mark.parametrize("path, kind", [
    ("policies/bank-policies.json", "policy"),
    ("tests/fixtures/policies/sample/bank-policies.json", "policy"),
    ("tests/fixtures/policies/personal-bank-pre-item.json", "policy"),
    ("tests/fixtures/policies/b1/bank-policies.json", "policy"),
    ("policies/context-triggers.json", "trigger"),
    ("tests/fixtures/policies/sample/context-triggers.json", "trigger"),
    ("docs/context-triggers.json", None),
    ("docs/public-export.md", "doc"),
    ("docs/public-export-rehearsals.md", None),
    ("scripts/public-export/manifest.json", "manifest"),
    ("scripts/public-export/export.py", None),
])
def test_structural_kind_by_path(path, kind):
    assert ex.structural_kind(path) == kind


# ---------------------------------------------------------------- 정책 자유 문장

def test_policy_freetext_fixture_value_or_neutral_and_other_bytes_kept():
    src = _policy_text().encode("utf-8")
    new, n, ok = ex.policy_freetext(src, FIXTURE)
    want = (_policy_text().replace(_lit(FAKE_A), _lit(FIXTURE_A))
            .replace(_lit(FAKE_B), _lit(ex.FREETEXT_NEUTRAL))
            .replace(_lit(FAKE_D), _lit(None)))        # fixture defaults 값(null)
    assert new == want.encode("utf-8")                  # 다른 필드·키 순서·들여쓰기 바이트 그대로
    assert (n, ok) == (3, True)
    assert b"FAKE-PRIVATE" not in new
    again, n2, _ = ex.policy_freetext(src, FIXTURE)
    assert again == new and n2 == 3                     # 같은 입력 같은 출력
    assert ex.policy_freetext(new, FIXTURE) == (new, 0, True)    # 이미 바뀐 판본은 그대로(멱등)


def test_policy_freetext_without_fixture_uses_neutral_everywhere():
    new, n, ok = ex.policy_freetext(_policy_text().encode("utf-8"), {})
    assert ok and n == 3
    assert json.loads(new)["banks"]["demo-alpha"]["instructions_text"] == ex.FREETEXT_NEUTRAL
    assert json.loads(new)["defaults"]["instructions_text"] == ex.FREETEXT_NEUTRAL


def test_policy_freetext_fixture_bank_without_field_gets_neutral():
    raw = json.dumps({"banks": {"demo-plain": {"instructions_text": FAKE_B}}}).encode()
    new, n, _ = ex.policy_freetext(raw, FIXTURE)
    assert json.loads(new)["banks"]["demo-plain"]["instructions_text"] == ex.FREETEXT_NEUTRAL and n == 1


def test_policy_freetext_duplicate_keys_all_replaced():
    raw = ('{"banks": {"demo-x": {"instructions_text": ' + _lit(FAKE_A) + ', "instructions_text": '
           + _lit(FAKE_B) + '}}}').encode()
    new, n, _ = ex.policy_freetext(raw, FIXTURE)
    assert b"FAKE-PRIVATE" not in new and n == 2


@pytest.mark.parametrize("raw", [b"{broken", b"\xff\xfe{}", b"", "\ufeff{}".encode("utf-8")])
def test_policy_freetext_unreadable_json_left_as_is(raw):
    assert ex.policy_freetext(raw, FIXTURE) == (raw, 0, False)


@pytest.mark.parametrize("raw", [b"[1, 2]", b'"text"', b'{"triggers": [{"note": "x"}]}', b'{"banks": []}',
                                 b'{"banks": {"demo-x": "not-an-object"}}', b'{"banks": {"demo-x": {"instructions_text": 3}}}'])
def test_policy_freetext_non_policy_shapes_untouched(raw):
    assert ex.policy_freetext(raw, FIXTURE) == (raw, 0, True)


def test_fixture_map_keeps_only_freetext_fields():
    raw = json.dumps({"defaults": {"tags_required": True},
                      "banks": {"demo-alpha": {"instructions_text": FIXTURE_A, "tag_prefixes": ["kind"]},
                                "demo-plain": {}}}).encode()
    assert ex.freetext_fixture(raw) == {"defaults": {}, "banks": {"demo-alpha": {"instructions_text": FIXTURE_A},
                                                                  "demo-plain": {}}}
    assert ex.freetext_fixture(b"{broken") == {}
    assert ex.freetext_fixture(None) == {}


# ---------------------------------------------------------------- 리허설 절

DOC_HEAD = "# 공개판 내보내기\n\n## 1. 사용법\n\n본문 하나.\n\n"
DOC_SECTION = ("## 리허설 기록\n\n"
               "hc-rehearsal-demo 표지 줄.\n\n"
               "```sh\n"
               "## docker ps -a\n"
               "# 주석 줄\n"
               "```\n\n"
               "~~~~\n"
               "## 물결 코드 블록 안\n"
               "~~~\n"
               "## 아직 블록 안(닫는 울타리가 짧다)\n"
               "~~~~\n\n"
               "    ## 들여쓴 코드 블록 줄\n\n"
               "### 하위 절도 같이 빠진다\n\n"
               "끝 줄.\n\n")
DOC_TAIL = "## 2. 다음 절\n\n남는 본문.\n"


def test_rehearsal_section_removed_only_until_next_same_level_heading():
    raw = (DOC_HEAD + DOC_SECTION + DOC_TAIL).encode("utf-8")
    new, n = ex.strip_rehearsal(raw)
    assert new == (DOC_HEAD + DOC_TAIL).encode("utf-8") and n == 1
    assert ex.strip_rehearsal(raw) == (new, 1)          # 결정적
    assert ex.strip_rehearsal(new) == (new, 0)


def test_rehearsal_section_at_end_removed_to_end():
    raw = (DOC_HEAD + DOC_SECTION).encode("utf-8")
    assert ex.strip_rehearsal(raw) == (DOC_HEAD.encode("utf-8"), 1)


def test_rehearsal_section_ends_at_higher_level_heading_and_crlf_kept():
    raw = ("a\r\n## 리허설 기록\r\nx\r\n```\r\n## y\r\n```\r\n# 최상위\r\nb\r\n").encode("utf-8")
    assert ex.strip_rehearsal(raw) == (b"a\r\n" + "# 최상위\r\nb\r\n".encode("utf-8"), 1)


@pytest.mark.parametrize("doc", [
    DOC_HEAD + DOC_TAIL,
    DOC_HEAD + "```\n## 리허설 기록\n```\n" + DOC_TAIL,              # 코드 블록 안 제목은 제목이 아니다
    DOC_HEAD + "### 리허설 기록\n본문\n" + DOC_TAIL,                  # 다른 수준
    DOC_HEAD + "## 리허설 기록 요약\n본문\n" + DOC_TAIL,              # 다른 제목
    "",
])
def test_doc_without_rehearsal_section_untouched(doc):
    raw = doc.encode("utf-8")
    assert ex.strip_rehearsal(raw) == (raw, 0)


def test_rehearsal_heading_with_closing_hashes_and_two_sections():
    raw = ("x\n## 리허설 기록 ##\n본문1\n## 중간\nm\n##  리허설 기록\n본문2\n").encode("utf-8")
    assert ex.strip_rehearsal(raw) == ("x\n## 중간\nm\n".encode("utf-8"), 2)


# ---------------------------------------------------------------- 매니페스트 해독표

MANIFEST_TEXT = ('{\n  "version": 1,\n  "deny_patterns": [\n    {"id": "fake-1", "regex": "(?i)fakeword"}\n  ],\n'
                 '  "include": ["a/", "b/"],\n  "accept": [{"path": "a/", "pattern": "fake-1"}],\n'
                 '  "message_rewrites": [{"regex": "fake", "replace": "demo"}],\n  "about": "남는 값"\n}\n')


def test_manifest_rule_values_cleared_other_bytes_kept():
    new, n, ok = ex.clear_manifest_rules(MANIFEST_TEXT.encode("utf-8"))
    want = ('{\n  "version": 1,\n  "deny_patterns": [],\n  "include": ["a/", "b/"],\n  "accept": [],\n'
            '  "message_rewrites": [],\n  "about": "남는 값"\n}\n')
    assert new == want.encode("utf-8") and (n, ok) == (3, True)
    assert ex.clear_manifest_rules(new) == (new, 0, True)
    assert ex.clear_manifest_rules(MANIFEST_TEXT.encode("utf-8"))[0] == new


@pytest.mark.parametrize("raw", [b"{broken", b'{"include": []}', b"[]"])
def test_manifest_without_rules_or_unreadable_untouched(raw):
    new, n, _ = ex.clear_manifest_rules(raw)
    assert (new, n) == (raw, 0)
    assert ex.clear_manifest_rules(b"{broken")[2] is False


# ---------------------------------------------------------------- Rewriter 순서(이름 표 뒤의 뱅크 키로 대조)

def test_structural_rewrite_matches_bank_key_after_name_table():
    rw = ex.Rewriter([("zorbix-bank", "demo-alpha")], [], ["policies/"], FIXTURE)
    raw = json.dumps({"banks": {"zorbix-bank": {"instructions_text": FAKE_A}}}).encode()
    new = rw.blob("policies/bank-policies.json", raw)
    assert json.loads(new) == {"banks": {"demo-alpha": {"instructions_text": FIXTURE_A}}}
    # 이름 표 경로 밖이어도 구조 치환은 건다(이름은 독립 유출 검사 몫)
    rw2 = ex.Rewriter([("zorbix-bank", "demo-alpha")], [], [], FIXTURE)
    out2 = json.loads(rw2.blob("policies/bank-policies.json", raw))
    assert out2 == {"banks": {"zorbix-bank": {"instructions_text": ex.FREETEXT_NEUTRAL}}}
    assert rw.blob("src/app.py", b"zorbix-bank") == b"zorbix-bank"


# ---------------------------------------------------------------- 실제 내보내기(가짜 원본 이력)

SAMPLE_FIXTURE = json.dumps({"defaults": {"instructions_text": None},
                             "banks": {"demo-bank": {"tag_aliases": {"kind:done": "kind:completion"},
                                                       "instructions_text": FIXTURE_A}}}, indent=2) + "\n"


def _freetext_source(tmp_path):
    src, split = th.base_source(tmp_path)
    src.commit({
        "policies/bank-policies.json": json.dumps({"banks": {
            "zorbix-bank": {"instructions_text": FAKE_A}, "demo-orphan": {"instructions_text": FAKE_B}}}, indent=4),
        "tests/fixtures/policies/sample/bank-policies.json": SAMPLE_FIXTURE,
        "tests/fixtures/policies/broken.json": "{broken " + FAKE_D,
        "docs/public-export.md": DOC_HEAD + DOC_SECTION + DOC_TAIL,
        "scripts/public-export/manifest.json": MANIFEST_TEXT,
    }, "add policy text")
    src.commit({"policies/bank-policies.json": json.dumps({"banks": {"personal-alice": {}}}),
                "tests/fixtures/policies/broken.json": "{}",
                "docs/public-export.md": DOC_HEAD + DOC_TAIL,
                "scripts/public-export/manifest.json": '{"include": []}\n'}, "drop policy text")
    return src, split


def _all_blobs(repo: Path, path: str) -> list[bytes]:
    shas = th.git(repo, "log", "--format=%H", "refs/heads/main", "--", path).split()
    out = []
    for c in shas:
        r = subprocess.run(["git", "-C", str(repo), "show", f"{c}:{path}"], capture_output=True)
        if r.returncode == 0:
            out.append(r.stdout)
    return out


@th.needs_tools
def test_export_rewrites_freetext_rehearsal_and_manifest_in_whole_history(tmp_path):
    src, split = _freetext_source(tmp_path)
    manifest = dict(th.BASE_MANIFEST)
    manifest["include"] = manifest["include"] + ["scripts/"]
    inp = th.inputs(tmp_path, manifest=manifest)
    rc1, out1, err1 = th.run_export(src, split, inp, tmp_path / "o1")
    rc2, out2, _ = th.run_export(src, split, inp, tmp_path / "o2")
    assert rc1 == 0, (out1, err1)
    assert rc2 == 0, out2
    assert th.head_of(tmp_path / "o1") == th.head_of(tmp_path / "o2")
    repo = tmp_path / "o1" / "repo"

    policies = _all_blobs(repo, "policies/bank-policies.json")
    assert len(policies) >= 3
    texts = []
    for b in policies:
        d = json.loads(b)
        texts += [v.get("instructions_text") for v in d.get("banks", {}).values()]
    assert FIXTURE_A in texts and ex.FREETEXT_NEUTRAL in texts
    assert not any(isinstance(t, str) and "FAKE-PRIVATE" in t for t in texts)
    # 원래 들여쓰기 4칸 그대로(재직렬화하지 않았다)
    assert any(b.startswith(b'{\n    "banks"') for b in policies)

    broken = _all_blobs(repo, "tests/fixtures/policies/broken.json")
    assert ("{broken " + FAKE_D).encode() in broken          # JSON 아닌 판본은 그대로
    assert "JSON 아닌 판본 1" in out1

    for b in _all_blobs(repo, "docs/public-export.md"):
        assert "## 리허설 기록".encode() not in b and b"hc-rehearsal-" not in b
    log_p = th.git(repo, "log", "-p", "refs/heads/main", "--", "scripts/public-export/manifest.json")
    assert "fakeword" not in log_p and "fake-1" not in log_p
    assert "리허설 절 1" in out1 and "해독표 키 3" in out1
    assert "FAKE-PRIVATE" not in out1 + err1


# ---------------------------------------------------------------- [scope #459-1] public_literals 보호

FAKE_LIT = "git.example.invalid/Zorbix-Bank/Demo-Public"     # 가짜 공개 리포 URL 접두(가짜 계정명 zorbix-bank)


def _lit_rewriter(**kw):
    return ex.Rewriter([("zorbix-bank", "demo-bank")], kw.get("msg", []), ["docs/"], literals=[FAKE_LIT])


@pytest.mark.parametrize("text, want", [
    (f"see https://{FAKE_LIT}/issues and zorbix-bank", f"see https://{FAKE_LIT}/issues and demo-bank"),
    (f"{FAKE_LIT}{FAKE_LIT}", f"{FAKE_LIT}{FAKE_LIT}"),
    # 대소문자가 다르면 보호 문자열이 아니다(정확 일치)
    (FAKE_LIT.lower(), "git.example.invalid/demo-bank/demo-public"),
    ("zorbix-bank/Demo-Public", "demo-bank/Demo-Public"),
])
def test_rewriter_protects_public_literal_exactly(text, want):
    rw = _lit_rewriter()
    assert rw.text(text) == want
    assert rw.text(text) == rw.text(text)                # 결정적
    assert rw.data(text.encode()) == want.encode()


def test_rewriter_protects_public_literal_from_message_rewrites_and_paths():
    rw = _lit_rewriter(msg=[{"regex": "(Demo)-Public", "replace": r"\1-X"}])
    assert rw.text(f"{FAKE_LIT} Demo-Public") == f"{FAKE_LIT} Demo-X"
    assert rw.blob("docs/a.md", f"{FAKE_LIT} zorbix-bank".encode()) == f"{FAKE_LIT} demo-bank".encode()
    assert rw.text(f"docs/{FAKE_LIT}/zorbix-bank.md") == f"docs/{FAKE_LIT}/demo-bank.md"
    # 보호 문자열이 없으면 이전과 같다(템플릿 확장 포함)
    plain = ex.Rewriter([("zorbix-bank", "demo-bank")], [{"regex": "(Demo)-Public", "replace": r"\1-X"}], [])
    assert plain.text(f"{FAKE_LIT}") == "git.example.invalid/demo-bank/Demo-X"


def _lit_scanner(tmp_path, literals=(FAKE_LIT,)):
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"include": [], "exclude_history": [], "history_rewrite_paths": [],
                             "public_literals": list(literals),
                             "deny_patterns": [{"id": "w", "regex": "(?i)zorbix"}]}), encoding="utf-8")
    m = ex.cm.load_manifest(p)
    return ex.Scanner(m, [re.compile("(?i)zorbix-bank")], [(1, "zorbix-bank")], set())


@pytest.mark.parametrize("surface", ["blob", "message", "path"])
def test_scanner_skips_hits_only_inside_public_literal(tmp_path, surface):
    sc = _lit_scanner(tmp_path)
    assert sc.text(surface, f"x https://{FAKE_LIT}/y", "docs/a.md") == 0
    assert sc.total() == 0 and sc.exempt                  # 이름·local-deny·deny_patterns 적중 모두 면제로만 센다
    sc2 = _lit_scanner(tmp_path)
    n = sc2.text(surface, f"x https://{FAKE_LIT}/y and zorbix-bank", "docs/a.md")
    assert n == 3 and sc2.total() == 3                    # 다른 위치의 같은 이름(이름·local-deny·deny 각 1)은 검출
    sc3 = _lit_scanner(tmp_path)
    assert sc3.text(surface, FAKE_LIT.lower(), "docs/a.md") == 3     # 정확 일치가 아니면 면제 없음
    sc4 = _lit_scanner(tmp_path, literals=())
    assert sc4.text(surface, FAKE_LIT, "docs/a.md") == 3


@pytest.mark.parametrize("value", ["x", [""], ["  "], [1], {"a": 1}, ["noslashliteral"]])
def test_manifest_public_literals_lint(tmp_path, value):
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"include": [], "exclude_history": [], "history_rewrite_paths": [],
                             "public_literals": value}), encoding="utf-8")
    with pytest.raises(ValueError, match="public_literals"):
        ex.cm.load_manifest(p)


def test_manifest_public_literals_optional_and_head_manifest_value():
    p = ROOT / "scripts" / "public-export" / "manifest.json"
    m = ex.cm.load_manifest(p)
    assert m["public_literals"] == ["github.com/Bkankim/Hindsight-Crew-Public", "memory-vault"]
    assert "public_literals" in ex.cm.MANIFEST_KEYS


def test_check_manifest_cli_exits_2_on_bad_public_literals(tmp_path):
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"include": [], "exclude_history": [], "history_rewrite_paths": [],
                             "public_literals": [""]}), encoding="utf-8")
    rules = tmp_path / "local-rules-empty.json"          # 기본 경로(리포 밖 실제 파일)를 읽지 않게
    rules.write_text("{}", encoding="utf-8")
    r = subprocess.run([__import__("sys").executable, "-I", str(ROOT / "scripts" / "public-export" / "check_manifest.py"),
                        "--manifest", str(p), "--local-rules", str(rules)], capture_output=True, cwd=str(ROOT))
    assert b"public_literals" in r.stderr
    assert r.returncode == 2, (r.stdout, r.stderr)


@th.needs_tools
def test_export_keeps_public_literal_and_rewrites_other_names(tmp_path):
    src, split = th.base_source(tmp_path)
    src.commit({"docs/links.md": f"repo: https://{FAKE_LIT}/issues\nowner zorbix-bank\n"},
               f"link https://{FAKE_LIT} for zorbix-bank")
    manifest = dict(th.BASE_MANIFEST, public_literals=[FAKE_LIT])
    inp = th.inputs(tmp_path, manifest=manifest)
    rc1, out1, err1 = th.run_export(src, split, inp, tmp_path / "o1")
    rc2, out2, _ = th.run_export(src, split, inp, tmp_path / "o2")
    assert rc1 == 0, (out1, err1)
    assert rc2 == 0, out2
    assert th.head_of(tmp_path / "o1") == th.head_of(tmp_path / "o2")
    repo = tmp_path / "o1" / "repo"
    assert th.git(repo, "show", "refs/heads/main:docs/links.md") == f"repo: https://{FAKE_LIT}/issues\nowner demo-bank\n"
    assert th.git(repo, "log", "-1", "--format=%s", "refs/heads/main") == f"link https://{FAKE_LIT} for demo-bank\n"
    assert "[PASS] 독립 유출 검사: 0" in out1 and "public-literal" in out1


# ---------------------------------------------------------------- [rework #459-1] 자리표시자 보호(C7)

WIDE = [{"regex": r"(?m)^.*fake-private-marker.*$", "replace": "[redacted]"}]


@pytest.mark.parametrize("line", [
    f"fake-private-marker notes https://{FAKE_LIT} tracks fake-secret-context",   # 표지 앞, 사적 문맥 뒤
    f"fake-secret-context https://{FAKE_LIT} fake-private-marker",                 # 사적 문맥 앞, 표지 뒤
    f"see {FAKE_LIT} for fake-private-marker",
])
def test_wide_message_rule_spanning_public_literal_rewrites_whole_line(line):
    rw = _lit_rewriter(msg=WIDE)
    out = rw.text(f"{line}\nnext {FAKE_LIT} zorbix-bank")
    assert out == f"[redacted]\nnext {FAKE_LIT} demo-bank"     # 규칙이 이긴다(보호 문자열도 같이 지워진다)
    assert rw.text(f"{line}\nnext {FAKE_LIT} zorbix-bank") == out          # 결정적
    assert "fake-secret-context" not in out and "\0" not in out


def test_lookaround_rule_across_public_literal_still_applies():
    rw = _lit_rewriter(msg=[{"regex": r"fake-secret-context(?=.*fake-private-marker)", "replace": "[ctx]"}])
    assert rw.text(f"fake-secret-context {FAKE_LIT} fake-private-marker") == f"[ctx] {FAKE_LIT} fake-private-marker"


def test_placeholder_avoids_characters_already_in_input():
    rw = _lit_rewriter()
    text = "\0\0 " + chr(0xE001) + f" {FAKE_LIT} zorbix-bank"
    assert rw.text(text) == "\0\0 " + chr(0xE001) + f" {FAKE_LIT} demo-bank"


# ---------------------------------------------------------------- [rework #459-1] 트리거 자유 문장(C2)

T_FAKE_NOTE = "FAKE-PRIVATE-SENTENCE trigger note"
T_FAKE_QUERY = "FAKE-PRIVATE-SENTENCE trigger query"
T_GIVEN = {"demo-trig": {"note": "Fixture note for demo-trig.", "query": "fixture recall query for demo"}}


def _trig_text(note=T_FAKE_NOTE, query=T_FAKE_QUERY) -> str:
    return ('{\n  "version": 1,\n  "triggers": [\n'
            '    {"id": "demo-trig", "bank": "demo-bank", "all_of": [["alpha"]],\n'
            '     "recall": {"tags": ["kind:rule"], "query": ' + _lit(query) + '}, "note": ' + _lit(note) + '},\n'
            '    {"note": ' + _lit(note) + ',   "id": "orphan-trig", "bank": "demo-bank", "all_of": [["beta"]],\n'
            '     "recall": {"query":' + _lit(query) + '}, "max_memories": 2},\n'
            '    {"id": "null-note", "bank": "demo-bank", "all_of": [["gamma"]], "recall": {"query": "x"}, "note": null}\n'
            '  ]\n}\n')


def test_trigger_freetext_head_value_or_neutral_and_other_bytes_kept():
    raw = _trig_text().encode("utf-8")
    new, n, ok = ex.trigger_freetext(raw, T_GIVEN)
    t = _trig_text()
    want = (t.replace(_lit(T_FAKE_NOTE), _lit(T_GIVEN["demo-trig"]["note"]), 1)
             .replace(_lit(T_FAKE_QUERY), _lit(T_GIVEN["demo-trig"]["query"]), 1)
             .replace(_lit(T_FAKE_NOTE), _lit(ex.TRIGGER_NEUTRAL_NOTE))
             .replace(_lit(T_FAKE_QUERY), _lit(ex.TRIGGER_NEUTRAL_QUERY))
             .replace('"query": "x"', '"query": ' + _lit(ex.TRIGGER_NEUTRAL_QUERY)))
    assert new == want.encode("utf-8") and (n, ok) == (5, True)
    d = json.loads(new)
    assert [r["id"] for r in d["triggers"]] == ["demo-trig", "orphan-trig", "null-note"]   # 구조 값 그대로
    assert d["triggers"][0]["all_of"] == [["alpha"]] and d["triggers"][2]["note"] is None
    assert ex.trigger_freetext(raw, T_GIVEN) == (new, 5, True)            # 결정적
    assert ex.trigger_freetext(new, T_GIVEN) == (new, 0, True)            # 멱등


@pytest.mark.parametrize("raw", [b"{broken", b"[]", b'{"triggers": {}}', b'{"version": 1}'])
def test_trigger_freetext_unreadable_or_other_shapes_untouched(raw):
    new, n, _ = ex.trigger_freetext(raw, T_GIVEN)
    assert (new, n) == (raw, 0)


def test_trigger_fixture_and_neutral_values_pass_trigger_schema():
    from gateway import context_triggers as ct
    raw = json.dumps({"version": 1, "triggers": [
        {"id": "demo-trig", "bank": "demo-bank", "all_of": [["alpha"]], "recall": {"query": "q" * 9}, "note": "n"},
        {"id": "demo-trig", "bank": "demo-bank", "all_of": [["x"]], "recall": {"query": "other query"}},
        {"id": "no-note", "bank": "demo-bank", "all_of": [["y"]], "recall": {"query": "plain query"}}]}).encode()
    assert ex.trigger_fixture(raw) == {"demo-trig": {"note": "n", "query": "q" * 9}, "no-note": {"query": "plain query"}}
    assert ex.trigger_fixture(b"{broken") == {} and ex.trigger_fixture(None) == {}
    row = {"id": "demo-trig", "bank": "demo-bank", "all_of": [["alpha"]],
           "recall": {"query": ex.TRIGGER_NEUTRAL_QUERY}, "note": ex.TRIGGER_NEUTRAL_NOTE}
    t = ct._parse_row(row, "test")                       # 중립 문구도 트리거 스키마를 통과한다
    assert t.note == ex.TRIGGER_NEUTRAL_NOTE and t.query == ex.TRIGGER_NEUTRAL_QUERY
    assert "note" in ct._ROW_KEYS and "query" in ct._RECALL_KEYS


# ---------------------------------------------------------------- [rework #459-1] 공개 템플릿과 같은 값은 그대로

def test_policy_value_equal_to_public_template_is_kept():
    keep = {"defaults": {}, "banks": {"personal-x": {"instructions_text": "Public template text."}}}
    raw = json.dumps({"banks": {"personal-x": {"instructions_text": "Public template text."},
                                "personal-y": {"instructions_text": "Public template text."}}}).encode()
    new, n, _ = ex.policy_freetext(raw, FIXTURE, keep)
    d = json.loads(new)
    assert d["banks"]["personal-x"]["instructions_text"] == "Public template text."      # 같은 뱅크·같은 값
    assert d["banks"]["personal-y"]["instructions_text"] == ex.FREETEXT_NEUTRAL          # 다른 뱅크는 중립
    assert n == 1
    other = json.dumps({"banks": {"personal-x": {"instructions_text": FAKE_B}}}).encode()
    assert json.loads(ex.policy_freetext(other, FIXTURE, keep)[0])["banks"]["personal-x"]["instructions_text"] \
        == ex.FREETEXT_NEUTRAL
    assert ex.KEEP_TEMPLATES == {"tests/fixtures/policies/personal-bank-pre-item.json":
                                 "policies/templates/personal-bank.json"}


# ---------------------------------------------------------------- [rework #459-1] public_literals 이름 충돌(C5)

def test_public_literal_conflicts_with_table_names():
    names = ["zorbix/bank-x", "quuxproj"]
    assert ex.public_literal_conflicts(["Zorbix/Bank-X"], names) == [1]          # 같은 이름(대소문자 무시)
    assert ex.public_literal_conflicts(["ok/x", "zorbix/bank"], names) == [2]    # 이름 안에 든 더 짧은 값
    assert ex.public_literal_conflicts([FAKE_LIT, "host/quuxproj/repo"], names) == []   # 이름을 품은 더 긴 값은 허용


@th.needs_tools
def test_export_rejects_public_literal_equal_to_table_name(tmp_path):
    src, split = th.base_source(tmp_path)
    manifest = dict(th.BASE_MANIFEST, public_literals=["Zorbix/Bank-X"])
    inp = th.inputs(tmp_path, table=th.TABLE + "zorbix/bank-x\tdemo-slash\n", manifest=manifest)
    rc, out, err = th.run_export(src, split, inp, tmp_path / "o")
    assert rc == 2, (out, err)
    assert "public_literals 검사 실패: 1번" in err + out and "zorbix" not in (err + out).lower()


# ---------------------------------------------------------------- [rework #459-1] 치환 뒤 깨진 JSON 은 실패(C4)

@th.needs_tools
def test_export_fails_when_rewrite_breaks_policy_json(tmp_path):
    src, split = th.base_source(tmp_path)
    src.commit({"policies/bank-policies.json": json.dumps({"banks": {"demo-bank": {"instructions_text": "breakme"}}}),
                "tests/fixtures/policies/broken.json": "{still broken"}, "policy with break word")
    manifest = dict(th.BASE_MANIFEST,
                    message_rewrites=th.BASE_MANIFEST["message_rewrites"] + [{"regex": "breakme", "replace": '"'}])
    rc, out, err = th.run_export(src, split, th.inputs(tmp_path, manifest=manifest), tmp_path / "o")
    assert rc == 1, (out, err)
    assert "[FAIL] 구조 치환: 치환 뒤 JSON 이 깨진 판본 1" in out
    assert "구조 치환 깨진 판본 경로: policies/bank-policies.json" in out
    assert "JSON 아닌 판본 1" in out                     # 원래부터 JSON 아닌 판본은 건수만


# ---------------------------------------------------------------- [rework #459-1] 트리거 이력 실제 내보내기

@th.needs_tools
def test_export_rewrites_trigger_freetext_in_whole_history(tmp_path):
    src, split = th.base_source(tmp_path)
    head_note, head_query = "Head note for the demo trigger.", "head recall query for demo"
    trig = lambda note, query: json.dumps({"version": 1, "triggers": [   # noqa: E731
        {"id": "demo-trig", "bank": "demo-bank", "all_of": [["alpha"]], "recall": {"query": query}, "note": note},
        {"id": "old-trig", "bank": "demo-bank", "all_of": [["beta"]], "recall": {"query": query}, "note": note}]},
        indent=2)
    src.commit({"policies/context-triggers.json": trig(T_FAKE_NOTE, T_FAKE_QUERY)}, "private triggers")
    src.commit({"policies/context-triggers.json": json.dumps({"version": 1, "triggers": [
        {"id": "demo-trig", "bank": "demo-bank", "all_of": [["alpha"]], "recall": {"query": head_query},
         "note": head_note}]}, indent=2)}, "demo triggers")
    inp = th.inputs(tmp_path)
    rc1, out1, err1 = th.run_export(src, split, inp, tmp_path / "o1")
    rc2, out2, _ = th.run_export(src, split, inp, tmp_path / "o2")
    assert rc1 == 0, (out1, err1)
    assert rc2 == 0 and th.head_of(tmp_path / "o1") == th.head_of(tmp_path / "o2")
    assert "트리거 판본" in out1
    allowed = {head_note, head_query, ex.TRIGGER_NEUTRAL_NOTE, ex.TRIGGER_NEUTRAL_QUERY}
    seen = set()
    for b in _all_blobs(tmp_path / "o1" / "repo", "policies/context-triggers.json"):
        for row in json.loads(b).get("triggers", []):
            vals = [row.get("note"), row.get("recall", {}).get("query")]
            seen |= {v for v in vals if isinstance(v, str)}
    assert seen and seen <= allowed and head_note in seen and ex.TRIGGER_NEUTRAL_NOTE in seen
