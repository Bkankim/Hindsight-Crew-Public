"""공개판 내보내기 로컬 규칙 파일과 사설 IPv4 검사 (#456).

로컬 규칙 파일(리포 밖 `--local-rules`)은 매니페스트 `deny_patterns`·`message_rewrites`·`accept` 와 같은 모양이고
매니페스트 값 뒤에 합친다. 독립 유출 검사에는 이름 유출과 별도인 `private-ipv4` 검사 줄이 있다.
규칙·주소·이름은 전부 가짜다. 사설 주소는 소스에 점으로 이은 리터럴로 쓰지 않고 실행 때 만든다
(이 파일 자체가 공개판 후보의 private-ipv4 검사에 걸리지 않게). 운영자 기본 경로(~/.config/...)는 읽지 않는다.
"""
import json
from pathlib import Path

import pytest

from test_public_export_tool import (ROOT, Src, base_source, ex, git, inputs, needs_tools, run_export,
                                     assert_no_secrets, BASE_MANIFEST)

cm = ex.cm


def ip(*octets) -> str:
    return ".".join(str(o) for o in octets)


FAKE_RULES = {
    "deny_patterns": [{"id": "w", "regex": "for[b]idden"}],
    "message_rewrites": [{"regex": "for[b]idden", "replace": "fine"}],
    "accept": [{"path": "src/app.py", "pattern": "w", "handling": "keep", "reason": "r"}],
}
EMPTY_RULE_KEYS = {"deny_patterns": [], "message_rewrites": [], "accept": []}


@pytest.fixture(autouse=True)
def _fake_home(tmp_path, monkeypatch):
    """기본 경로를 읽는 호출이 운영자 파일 대신 빈 임시 HOME 을 보게 한다."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def _write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------- 로컬 규칙 파일: 경로·읽기·검사(lint)

def test_default_path_missing_is_empty_rules(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert cm.local_rules_path(None, repo) is None
    assert cm.load_local_rules(None) == EMPTY_RULE_KEYS
    default = tmp_path / "home" / ".config" / "hindsight-crew" / "public-export" / "local-rules.json"
    _write(default, FAKE_RULES)
    assert cm.local_rules_path(None, repo) == default.resolve()
    assert cm.load_local_rules(default) == FAKE_RULES


@pytest.mark.parametrize("case, want", [
    ("명시 경로 없음", "--local-rules 파일 없음"),
    ("기본 경로 깨진 심링크", "깨진 심링크"),
    ("리포 안", "리포 밖"),
])
def test_local_rules_path_errors(tmp_path, case, want):
    repo = tmp_path / "repo"
    repo.mkdir()
    value = None
    if case == "명시 경로 없음":
        value = str(tmp_path / "nope.json")
    elif case == "기본 경로 깨진 심링크":
        default = tmp_path / "home" / ".config" / "hindsight-crew" / "public-export" / "local-rules.json"
        default.parent.mkdir(parents=True)
        default.symlink_to(tmp_path / "moved-away.json")
    else:
        value = str(_write(repo / "rules.json", "{}"))
    with pytest.raises(cm.LocalRulesError, match=want):
        cm.local_rules_path(value, repo)


@pytest.mark.parametrize("text, want", [
    ("{}", EMPTY_RULE_KEYS),
    ('{"accept": []}', EMPTY_RULE_KEYS),
    (json.dumps(FAKE_RULES), FAKE_RULES),
])
def test_valid_rules_fill_missing_keys_with_empty_lists(tmp_path, text, want):
    assert cm.load_local_rules(_write(tmp_path / "r.json", text)) == want


@pytest.mark.parametrize("data, want", [
    ("{\n  \"deny_patterns\": [\n", "JSON 형식 오류: 3번 줄"),
    ("[]", "최상위가 객체가 아님"),
    ({"deny_patterns": [], "zzsecretkey": [], "other": 1}, "키 밖 값 2개"),
    ({"deny_patterns": {"id": "w"}}, "deny_patterns: 목록이 아님"),
    ({"deny_patterns": [{"id": "w", "regex": "ok"}, {"id": "v", "regex": "(zzleak"}]}, "deny_patterns 2번: 정규식 오류"),
    ({"deny_patterns": [{"id": "w", "regex": "a{99999999999}"}]}, "deny_patterns 1번: 정규식 오류"),   # OverflowError
    ({"deny_patterns": [{"id": "w"}]}, "deny_patterns 1번: 형식 오류"),
    ({"deny_patterns": [{"id": "w", "regex": "a", "zzextra": "b"}]}, "deny_patterns 1번: 형식 오류"),
    ({"deny_patterns": [{"id": "", "regex": "a"}]}, "deny_patterns 1번: 형식 오류"),
    ({"message_rewrites": [{"regex": "[zzleak", "replace": "x"}]}, "message_rewrites 1번: 정규식 오류"),
    ({"message_rewrites": [{"regex": "a", "replace": "\\9"}]}, "message_rewrites 1번: 정규식 오류"),
    ({"message_rewrites": [{"regex": "a", "replace": 3}]}, "message_rewrites 1번: 형식 오류"),
    ({"accept": [{"path": "src/", "reason": "r"}]}, "accept 1번: 형식 오류"),
    ({"accept": [{"path": "src/", "pattern": "w", "handling": 1}]}, "accept 1번: 형식 오류"),
    ({"accept": [{"path": "src/", "pattern": "w", "reason": "r"}]}, "accept 1번: 형식 오류"),          # handling 누락
    ({"accept": [{"path": "src/", "pattern": "w", "handling": "keep"},
                 {"path": "src/", "pattern": "w", "handling": "zzexport-rewirte"}]}, "accept 2번: handling 허용 값 아님"),
])
def test_rules_lint_fails_with_item_numbers_only(tmp_path, data, want):
    with pytest.raises(cm.LocalRulesError) as e:
        cm.load_local_rules(_write(tmp_path / "r.json", data))
    msg = str(e.value)
    assert want in msg, msg
    assert "zz" not in msg                      # 키 이름·정규식 내용은 출력하지 않는다


@pytest.mark.parametrize("handling", ["export-rewrite", "keep"])
def test_accept_handling_allowed_values_pass_lint(tmp_path, handling):
    rules = {"accept": [{"path": "src/", "pattern": "w", "handling": handling}]}
    assert cm.load_local_rules(_write(tmp_path / "r.json", rules))["accept"] == rules["accept"]


def test_manifest_unknown_top_level_key_is_rejected(tmp_path, capsys):
    """매니페스트 키 오타(예: message_rewrite)는 조용히 빈 규칙이 되지 않고 exit 2 다. 키 이름은 내지 않는다."""
    path = _write(tmp_path / "manifest.json", dict(BASE_MANIFEST, zzmessage_rewrite=[{"regex": "a", "replace": "b"}]))
    with pytest.raises(ValueError, match=r"unknown top-level keys \(1\)"):
        cm.load_manifest(path)
    repo = _cm_repo(tmp_path)
    assert cm.main(["--repo", str(repo), "--manifest", str(path)]) == 2
    assert "zz" not in capsys.readouterr().err
    known = dict(BASE_MANIFEST, version=1, about="x")             # 알려진 키는 모두 통과한다
    assert cm.load_manifest(_write(tmp_path / "known.json", known))["about"] == "x"


def test_head_b_line_prints_merged_rule_number_not_pattern_id(tmp_path, capsys):
    """(b) 실패 줄은 로컬 규칙 값인 패턴 id 대신 합친 목록 번호(매니페스트 뒤 로컬)만 낸다."""
    repo = _cm_repo(tmp_path)
    base = {"include": ["src/", "docs/"], "exclude_history": [], "history_rewrite_paths": ["docs/"]}
    man = _write(tmp_path / "m.json", {**base, "deny_patterns": [{"id": "zzmanifestid", "regex": "nevermatch[x]"}]})
    rules = _write(tmp_path / "r.json", {"deny_patterns": [{"id": "zzlocalctx", "regex": "for[b]idden"}]})
    assert cm.main(["--repo", str(repo), "--manifest", str(man), "--local-rules", str(rules)]) == 1
    out = capsys.readouterr().out
    assert "(b) src/a.py: deny_patterns #2 x1" in out and "(b) src/app.py: deny_patterns #2 x1" in out
    assert "zz" not in out


def test_rules_lint_reports_every_bad_item(tmp_path):
    data = {"deny_patterns": [{"id": "w", "regex": "("}, {"id": "v", "regex": "ok"}, {"id": "u", "regex": ")"}],
            "accept": ["nope"], "zz": 1}
    with pytest.raises(cm.LocalRulesError) as e:
        cm.load_local_rules(_write(tmp_path / "r.json", data))
    msg = str(e.value)
    assert "키 밖 값 1개" in msg and "deny_patterns 1번: 정규식 오류" in msg and "deny_patterns 3번: 정규식 오류" in msg
    assert "accept 1번: 형식 오류" in msg and "deny_patterns 2번" not in msg


def test_merge_appends_local_rules_after_manifest_rules(tmp_path):
    man = dict(BASE_MANIFEST, deny_patterns=[{"id": "m", "regex": "manif[e]st"}],
               message_rewrites=[{"regex": "aa", "replace": "bb"}])
    m = cm.load_manifest(_write(tmp_path / "manifest.json", man))
    local = {"deny_patterns": [{"id": "w", "regex": "for[b]idden"}],
             "message_rewrites": [{"regex": "bb", "replace": "cc"}],
             "accept": [{"path": "src/", "pattern": "w"}]}
    cm.merge_local_rules(m, local)
    assert [pid for pid, _ in m["_deny"]] == ["m", "w"]
    assert [r["regex"] for r in m["message_rewrites"]] == ["aa", "bb"]
    msg = "aa"
    for rx, rep in m["_msg"]:                    # 순서대로 적용: 로컬이 뒤라 aa -> bb -> cc
        msg = rx.sub(rep, msg)
    assert msg == "cc"
    assert cm.accepted(m, "src/a.py", "w")


def test_manifest_without_rule_keys_loads_as_empty(tmp_path):
    man = {k: v for k, v in BASE_MANIFEST.items() if k not in EMPTY_RULE_KEYS}
    m = cm.load_manifest(_write(tmp_path / "manifest.json", man))
    assert (m["deny_patterns"], m["message_rewrites"], m["accept"], m["_deny"], m["_msg"]) == ([], [], [], [], [])


def test_repository_manifest_carries_no_rule_values():
    """해독표 값(금칙·치환·accept)은 리포 밖 로컬 규칙 파일로 간다. 매니페스트에는 빈 목록만 남는다."""
    data = json.loads((ROOT / "scripts" / "public-export" / "manifest.json").read_text(encoding="utf-8"))
    assert all(data.get(k, []) == [] for k in EMPTY_RULE_KEYS)


# ---------------------------------------------------------------- 로컬 규칙 = 매니페스트 규칙 (같은 판정)

def _cm_repo(tmp_path: Path) -> Path:
    src = Src(tmp_path / "r")
    src.commit({"src/a.py": "a forbidden thing", "src/app.py": "forbidden ok", "docs/n.md": "x"}, "forbidden msg")
    return src.path


def _where(tmp_path: Path, where: str) -> tuple[Path, list[str]]:
    """규칙을 매니페스트에 둔 (매니페스트, 추가 인자) 또는 로컬 규칙 파일에 둔 쪽."""
    base = {"include": ["src/", "docs/"], "exclude_history": [], "history_rewrite_paths": ["docs/"]}
    d = tmp_path / where
    if where == "manifest":
        return _write(d / "manifest.json", {**base, **FAKE_RULES}), ["--local-rules", str(_write(d / "rules.json", "{}"))]
    return _write(d / "manifest.json", {**base, **EMPTY_RULE_KEYS}), ["--local-rules", str(_write(d / "rules.json", FAKE_RULES))]


def test_local_rules_give_the_same_head_and_history_verdicts_as_manifest_rules(tmp_path, capsys):
    repo = _cm_repo(tmp_path)
    got = {}
    for where in ("manifest", "local"):
        man, extra = _where(tmp_path, where)
        m = cm.merge_local_rules(cm.load_manifest(man), cm.load_local_rules(Path(extra[1])))
        rc = cm.main(["--repo", str(repo), "--manifest", str(man), "--history", "--rev", "HEAD", *extra])
        out = capsys.readouterr().out
        verdict = "\n".join(x for x in out.splitlines() if not x.startswith("로컬 규칙:"))   # 규칙 개수 줄만 다르다
        got[where] = (cm.check_head(repo, m), cm.check_history(repo, m, "HEAD"), rc, verdict, out)
    assert got["manifest"][:4] == got["local"][:4]
    head, hist, rc, _, out = got["local"]
    assert head == ([], ["(b) src/a.py: deny_patterns #1 x1"]) and hist == ({"src/a.py": 1}, 0) and rc == 1   # accept 는 src/app.py 만 덮는다
    assert "로컬 규칙: deny_patterns 1, message_rewrites 1, accept 1" in out


def test_check_manifest_default_local_rules_missing_is_empty(tmp_path, capsys):
    repo = _cm_repo(tmp_path)
    man, _ = _where(tmp_path, "local")
    assert cm.main(["--repo", str(repo), "--manifest", str(man)]) == 0       # 기본 경로(임시 HOME)에 파일 없음
    assert "로컬 규칙: 없음(빈 규칙)" in capsys.readouterr().out


@pytest.mark.parametrize("bad", ['{"deny_patterns": [{"id": "w", "regex": "(zz"}]}', '{"zz": []}', "not json"])
def test_check_manifest_bad_local_rules_exit_2(tmp_path, capsys, bad):
    repo = _cm_repo(tmp_path)
    man, _ = _where(tmp_path, "local")
    rules = _write(tmp_path / "bad.json", bad)
    assert cm.main(["--repo", str(repo), "--manifest", str(man), "--local-rules", str(rules)]) == 2
    err = capsys.readouterr().err
    assert "로컬 규칙 파일 검사 실패" in err and "zz" not in err


@needs_tools
def test_export_with_local_rules_matches_export_with_manifest_rules(tmp_path):
    """같은 규칙을 매니페스트에 두든 로컬 규칙 파일에 두든 산출 sha 와 검사 판정이 같다."""
    src, split = base_source(tmp_path)
    src.commit({"src/app.py": "print('forbidden')\n"}, "accept 로 덮는 금칙 적중(keep 이라 내보낸 뒤에도 덮는다)")
    res = {}
    for where in ("manifest", "local"):
        rules = FAKE_RULES if where == "manifest" else EMPTY_RULE_KEYS
        (tmp_path / where).mkdir()
        inp = inputs(tmp_path / where, manifest=dict(BASE_MANIFEST, **rules))
        local = _write(tmp_path / where / "inputs" / "rules.json", "{}" if where == "manifest" else FAKE_RULES)
        rc, out, err = run_export(src, split, inp, tmp_path / where / "o", "--local-rules", str(local))
        assert rc == 0, (out, err)
        result = json.loads((tmp_path / where / "o" / "result.json").read_text(encoding="utf-8"))
        res[where] = (result["public_head"], result["checks"])
        assert "로컬 규칙: deny_patterns" in out
    assert res["manifest"] == res["local"]
    first = git(tmp_path / "local" / "o" / "repo", "rev-list", "--max-parents=0", "refs/heads/main").strip()
    msg = git(tmp_path / "local" / "o" / "repo", "log", "-1", "--format=%B", first)
    assert msg.startswith("init fine demo-bank")           # 로컬 message_rewrites 가 커밋 메시지에 적용됐다


@needs_tools
@pytest.mark.parametrize("case, want", [
    ("lint", "로컬 규칙 파일 검사 실패: deny_patterns 1번: 정규식 오류"),
    ("명시 경로 없음", "--local-rules 파일 없음"),
])
def test_export_bad_local_rules_exit_2_before_filtering(tmp_path, case, want):
    src, split = base_source(tmp_path)
    rules = tmp_path / "rules.json"
    if case == "lint":
        _write(rules, {"deny_patterns": [{"id": "w", "regex": "(zzleak"}]})
    rc, out, err = run_export(src, split, inputs(tmp_path), tmp_path / "o", "--local-rules", str(rules))
    assert rc == 2 and want in err, (out, err)
    assert not (tmp_path / "o" / "repo").exists()
    assert "zzleak" not in out + err


@needs_tools
def test_export_default_local_rules_missing_is_empty(tmp_path):
    src, split = base_source(tmp_path)
    rc, out, err = run_export(src, split, inputs(tmp_path), tmp_path / "o", home=tmp_path / "home")
    assert rc == 0, (out, err)
    assert "로컬 규칙: 없음(빈 규칙)" in out


# ---------------------------------------------------------------- 사설 IPv4 리터럴 (가짜 주소)

PRIVATE = [ip(10, 0, 0, 1), ip(10, 255, 255, 254), ip(172, 16, 0, 1), ip(172, 31, 255, 9),
           ip(192, 168, 0, 1), ip(192, 168, 77, 200), ip(100, 64, 0, 1), ip(100, 127, 255, 254)]
NOT_PRIVATE = [
    ip(127, 0, 0, 1), ip(192, 0, 2, 10), ip(198, 51, 100, 7), ip(203, 0, 113, 99),   # loopback, 문서 예시 대역
    ip(8, 8, 8, 8), ip(1, 1, 1, 1), ip(172, 15, 0, 1), ip(172, 32, 0, 1), ip(100, 63, 255, 1), ip(100, 128, 0, 1),
    ip(192, 169, 0, 1), ip(11, 0, 0, 1),                                               # 공인 주소·대역 경계 밖
]
NOT_LITERAL = [ip(10, 0, 0, 256), ip(10, 0, 0) + ".x", ip(10, 0, 1000, 1), ip(1, 2, 3, 4, 5)]


def _ip_scanner(tmp_path: Path, reviewed: str = "", names=((1, "zorbix-bank"),)):
    rv = _write(tmp_path / "reviewed.tsv", reviewed)
    m = cm.load_manifest(inputs(tmp_path)["manifest"])
    return ex.Scanner(m, [], list(names), set(), reviewed=ex.load_reviewed_tokens(rv, list(names)))


@pytest.mark.parametrize("surface", ["blob", "path", "message"])
@pytest.mark.parametrize("addr", PRIVATE)
def test_private_ipv4_hits_on_every_surface(tmp_path, surface, addr):
    sc = _ip_scanner(tmp_path)
    for text in (f"host {addr} up", f"x={addr}:8080", f"[{addr}]", f"see {addr}.", f"v{addr}"):
        sc.private_ipv4(surface, text)
    assert sc.ipv4_counts == {surface: 5} and sc.ipv4_exempt == {}
    assert sc.total() == 0                                # 이름 유출 검사 건수와 섞이지 않는다


@pytest.mark.parametrize("surface", ["blob", "path", "message"])
@pytest.mark.parametrize("addr", NOT_PRIVATE + NOT_LITERAL)
def test_excluded_ranges_public_and_non_literals_do_not_hit(tmp_path, surface, addr):
    sc = _ip_scanner(tmp_path)
    assert sc.private_ipv4(surface, f"a {addr} b") == 0
    assert sc.ipv4_counts == {}


@pytest.mark.parametrize("text, want", [
    ("host-1." + ip(192, 168, 0, 12) + ".nip.io", 1),        # 숫자·점 뒤에 붙은 주소(와일드카드 DNS 호스트명)
    ("a1." + ip(10, 0, 0, 7), 1),
    ("v" + ip(1, 10, 0, 0, 1), 1),                            # 다섯 조각 버전 문자열 안의 사설 네 조각도 센다(fail-closed)
    (ip(10, 10, 10, 10, 10), 2),                              # 겹친 창은 창마다 센다
    (ip(1, 2, 3, 4, 5, 6), 0),
])
def test_private_ipv4_windows_inside_longer_dotted_runs(tmp_path, text, want):
    sc = _ip_scanner(tmp_path)
    assert sc.private_ipv4("blob", text) == want
    assert "<사설IPv4>" in sc.mask("d/" + text) or want == 0


def test_reviewed_ipv4_line_exempts_window_inside_longer_dotted_run(tmp_path):
    a = ip(10, 0, 0, 7)
    sc = _ip_scanner(tmp_path, f"{a}\tprivate-ipv4\n")
    assert sc.private_ipv4("blob", "pkg v1." + a + " and host-1." + a + ".nip.io") == 0
    assert sc.ipv4_exempt == {"blob": 2} and sc.ipv4_counts == {}


def test_private_ipv4_is_not_checked_on_author_identity(tmp_path):
    sc = _ip_scanner(tmp_path)
    sc.ident("Bot " + ip(10, 1, 2, 3), "bot@" + ip(10, 1, 2, 3))
    assert sc.ipv4_counts == {} and sc.total() == 0


# ---------------------------------------------------------------- 검토 토큰 파일의 IPv4 면제 줄

@pytest.mark.parametrize("surface", ["blob", "path", "message"])
def test_reviewed_ipv4_line_exempts_only_the_exact_literal_on_every_surface(tmp_path, surface):
    a, b = ip(10, 1, 2, 3), ip(10, 1, 2, 30)
    sc = _ip_scanner(tmp_path, f"# 가짜 오탐\n{a}\tprivate-ipv4\n{ip(192, 168, 9, 9)}\tPRIVATE-IPV4\n")
    n = sc.private_ipv4(surface, f"pkg {a} and {b} and {a}")
    assert n == 1 and sc.ipv4_counts == {surface: 1} and sc.ipv4_exempt == {surface: 2}
    assert sc.reviewed_unused() == 1


def test_reviewed_ipv4_lines_skip_name_lints_and_do_not_exempt_names(tmp_path):
    names = [(1, "zorbix-bank")]
    a = ip(10, 1, 2, 3)
    rows = ex.load_reviewed_tokens(_write(tmp_path / "rv.tsv", f"{a}\tprivate-ipv4\nxzorbix-banky\tzorbix-bank\n"), names)
    assert rows == [(1, a, "private-ipv4"), (2, "xzorbix-banky", "zorbix-bank")]
    sc = ex.Scanner(cm.load_manifest(inputs(tmp_path)["manifest"]), [], names, set(), reviewed=rows)
    sc.text("blob", f"{a} zorbix-bank")
    assert sc.counts == {("blob", "rename-table:1"): 1}


@pytest.mark.parametrize("text, want", [
    (f"{ip(10, 1, 2)}\tprivate-ipv4\n", "사설 IPv4 리터럴이 아닌 토큰: 1"),
    (f"# c\n{ip(8, 8, 8, 8)}\tprivate-ipv4\n", "사설 IPv4 리터럴이 아닌 토큰: 2"),
    (f"{ip(10, 1, 2, 3)}x\tprivate-ipv4\n", "사설 IPv4 리터럴이 아닌 토큰: 1"),
    (f"{ip(10, 1, 2, 300)}\tprivate-ipv4\n", "사설 IPv4 리터럴이 아닌 토큰: 1"),
    (f"{ip(10, 1, 2, 3)}\tprivate-ipv4\textra\n", "형식 오류: 1"),
])
def test_reviewed_ipv4_lint_fails_with_line_numbers_only(tmp_path, text, want):
    with pytest.raises(ex.ToolError) as e:
        ex.load_reviewed_tokens(_write(tmp_path / "rv.tsv", text), [(1, "zorbix-bank")])
    msg = str(e.value)
    assert want in msg and "10." not in msg and "8." not in msg, msg


# ---------------------------------------------------------------- 내보내기: private-ipv4 검사 줄 (실제 도구)

def _ip_source(tmp_path: Path, addr: str) -> tuple:
    src, split = base_source(tmp_path)
    src.commit({"src/net.py": f"HOST = '{addr}'\n", f"docs/hosts-{addr}.md": "x\n"}, f"bind {addr}")
    src.commit({"src/net.py": "HOST = 'localhost'\n"}, "use localhost")         # 과거 판본에만 남은 주소도 센다
    return src, split


@needs_tools
def test_export_private_ipv4_is_its_own_failing_check_line(tmp_path):
    addr = ip(10, 20, 30, 40)
    src, split = _ip_source(tmp_path, addr)
    rc, out, err = run_export(src, split, inputs(tmp_path), tmp_path / "o")
    assert rc == 1, (out, err)
    assert "[PASS] 독립 유출 검사" in out                            # 이름 유출과 별도 줄
    line = next(x for x in out.splitlines() if x.startswith("[FAIL] private-ipv4"))
    assert line == "[FAIL] private-ipv4: 3 (blob 1, message 1, path 1)", line
    assert "  private-ipv4 적중 blob 경로: src/net.py 1" in out
    assert addr not in out + err                                     # 주소는 출력하지 않는다(경로 이름 안도 가린다)
    assert addr not in (tmp_path / "o" / "comment.md").read_text(encoding="utf-8")
    assert_no_secrets(out + err)


@needs_tools
def test_export_reviewed_ipv4_line_exempts_false_positive(tmp_path):
    addr = ip(10, 20, 30, 40)
    src, split = _ip_source(tmp_path, addr)
    rv = _write(tmp_path / "rv.tsv", f"{addr}\tprivate-ipv4\n")
    rc, out, err = run_export(src, split, inputs(tmp_path), tmp_path / "o", "--reviewed-tokens", str(rv))
    assert rc == 0, (out, err)
    assert "[PASS] private-ipv4: 0 (blob 0, message 0, path 0); 면제 blob 1, message 1, path 1" in out, out
    assert "검토 토큰: 줄 1, 쓰이지 않은 줄 0" in out
