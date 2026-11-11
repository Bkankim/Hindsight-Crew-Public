"""S4 역할 판정기 순수 함수 (issue #376): 언급 찾기의 #372 동치, 병합 규칙, 사이드 저장소 스키마 계약 v1.

네트워크 0. 언급 찾기는 #372 `bench.r5_roles` 를 import 해 같은 고정 입력 묶음에서 결과를 대조한다(게이트웨이 모듈은
bench 를 import 하지 않는다 - 테스트만 둘을 같이 본다). 상수 회귀도 함께 둔다.
"""
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from bench import r5_roles as B
from gateway import item_roles as IR

REPO = Path(__file__).resolve().parent.parent
ALIASES = {"hc": "hindsight-crew", "hindsight-crew": "hindsight-crew", "dmd": "larch-yard"}

# (id, text, tags) 고정 입력 묶음. 펜스·URL·라벨·여러 문장·item 태그·project 태그·키 없음·한 문장 여러 키를 섞는다.
CASES = [
    ("m01", "[완료] HC#251 머지했다. 다음 단계는 HC#252 착수.", ["project:hindsight-crew"]),
    ("m02", "[인계] #12 남은 일 정리\n#12 는 근거로만 본다.", ["project:hindsight-crew"]),
    ("m03", "[결정] 키 없음 기억이다.", ["project:hindsight-crew"]),
    ("m04", "코드 예:\n```\nHC#999 는 펜스 안\n```\nHC#7 배포 완료!", []),
    ("m05", "[메모] 링크 https://github.com/x/hindsight-crew/issues/31 참고. dmd#3 과 HC#3 비교.", []),
    ("m06", "[진행] 정리 중.", ["item:hindsight-crew-40", "project:hindsight-crew"]),
    ("m07", "[완료] HC#5 done. HC#5 follow-up 남음. HC#6 closed.", []),
    ("m08", "owner/hindsight-crew#77 는 참조다。 그리고 #8 은 project 태그 둘이라 키가 아니다.",
     ["project:hindsight-crew", "project:larch-yard"]),
    ("m09", "[todo] HC#9", ["ITEM:hc-9"]),
    ("m10", "", ["item:hindsight-crew-10"]),
    ("m11", "[대기] HC#11 은 아직? 그렇다! HC#12 해결.", []),
    ("m12", "첫 줄\n\n\n[완료] HC#13 은 라벨이 첫 문장이 아니다.", []),
]


@pytest.mark.parametrize("mid, text, tags", CASES, ids=[c[0] for c in CASES])
def test_mentions_match_bench_r5_roles(mid, text, tags):
    assert IR.split_sentences(text) == B.split_sentences(text)
    assert IR.label_of(text) == B.label_of(text)
    assert IR.memory_mentions(mid, text, tags, ALIASES) == B.memory_mentions(mid, text, tags, ALIASES)


def test_memory_roles_and_merge_match_bench_on_the_whole_batch():
    allm = [m for mid, text, tags in CASES for m in IR.memory_mentions(mid, text, tags, ALIASES)["mentions"]]
    assert IR.memory_roles(allm) == B.memory_roles(allm)
    for roles in (["ref"], ["done"], ["open"], ["ref", "done"], ["done", "open"], ["ref", "open", "done"], []):
        assert IR.merge_roles(roles) == B.merge_roles(roles)


@pytest.mark.parametrize("sentence, label, first", [
    ("HC#1 남은 일", None, False), ("HC#1 완료", None, False), ("HC#1 미완료", None, False),
    ("[완료] HC#1", "완료", True), ("[진행] HC#1", "진행", True), ("[완료] HC#1", "완료", False),
    ("HC#1 merged", None, False), ("HC#1 next", None, False), ("HC#1 대기업", None, False),
])
def test_sentence_role_matches_bench(sentence, label, first):
    assert IR.sentence_role(sentence, label, first) == B.sentence_role(sentence, label, first)


def test_constant_regression_on_fixed_inputs():
    """bench 가 바뀌어도 게이트웨이 동작이 조용히 따라가지 않게 고정 기대값을 둔다."""
    got = {mid: IR.memory_roles(IR.memory_mentions(mid, text, tags, ALIASES)["mentions"]).get(mid, {})
           for mid, text, tags in CASES}
    assert got == {
        "m01": {"hindsight-crew#251": "done", "hindsight-crew#252": "open"},
        "m02": {"hindsight-crew#12": "open"},
        "m03": {},
        "m04": {"hindsight-crew#7": "done"},
        "m05": {"larch-yard#3": "ref", "hindsight-crew#3": "ref"},
        "m06": {"hindsight-crew#40": "ref"},
        "m07": {"hindsight-crew#5": "open", "hindsight-crew#6": "done"},
        "m08": {"hindsight-crew#77": "ref"},
        "m09": {"hindsight-crew#9": "open"},
        "m10": {"hindsight-crew#10": "ref"},
        "m11": {"hindsight-crew#11": "open", "hindsight-crew#12": "done"},
        "m12": {"hindsight-crew#13": "done"},
    }
    assert IR.split_sentences("a. b! c? d。 e\nf ```x. y``` g") == ["a.", "b!", "c?", "d。", "e", "f   g"]


def test_item_tag_only_key_is_ref_without_sentence():
    p = IR.memory_mentions("m", "[완료] 끝냈다.", ["item:hindsight-crew-40"], ALIASES)
    assert p["mentions"] == [{"mid": "m", "key": "hindsight-crew#40", "sent": None, "role": "ref",
                              "source": "item_tag"}]
    # 같은 키가 본문에도 있으면 태그 언급은 따로 붙지 않는다(본문 언급 역할이 쓰인다)
    p = IR.memory_mentions("m", "[완료] HC#40 끝냈다.", ["item:hindsight-crew-40"], ALIASES)
    assert [(m["key"], m["sent"], m["role"]) for m in p["mentions"]] == [("hindsight-crew#40", 0, "done")]


def test_merge_open_beats_done_beats_ref_within_one_memory():
    assert IR.merge_roles(["ref", "done"]) == "done"
    assert IR.merge_roles(["done", "open", "ref"]) == "open"
    assert IR.merge_roles(["ref", "ref"]) == "ref"


def test_aliases_sha256_is_sorted_lowercase_compact_json():
    import hashlib
    import json
    a = IR.aliases_sha256({"HC": "Hindsight-Crew", "dmd": "larch-yard"})
    b = IR.aliases_sha256({"dmd": "larch-yard", "hc": "hindsight-crew"})
    canon = json.dumps({"dmd": "larch-yard", "hc": "hindsight-crew"}, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":"))
    assert a == b == hashlib.sha256(canon.encode("utf-8")).hexdigest()


def test_gateway_module_does_not_import_bench_or_network():
    src = (REPO / "gateway" / "item_roles.py").read_text(encoding="utf-8")
    assert "bench" not in src
    for bad in ("httpx", "urllib", "socket", "subprocess", "requests"):
        assert "import %s" % bad not in src


# --- 사이드 저장소 스키마 ---------------------------------------------------------------------------------

META = {"model": "claude-haiku-5-5", "prompt_sha256": "p" * 64, "aliases_sha256": "a" * 64,
        "updated_at": "2026-11-02T00:00:00Z"}
MEMS = {"m1": {"document_id": "d1", "mentioned_at": "2026-10-01T00:00:00+00:00", "state": "valid",
               "text_sha256": "t" * 64, "judged_at": "2026-11-02T00:00:00Z"},
        "m2": {"document_id": None, "mentioned_at": None, "state": "invalidated", "text_sha256": "u" * 64,
               "judged_at": "2026-11-02T00:00:00Z"}}
ROLES = {"m1": {"hindsight-crew#1": "done", "hindsight-crew#2": "open"}, "m2": {"hindsight-crew#1": "ref"}}


def _columns(conn, table):
    return [(r[1], r[2], r[3], r[5]) for r in conn.execute("PRAGMA table_info(%s)" % table)]


def test_publish_creates_contract_v1_schema(tmp_path):
    db = tmp_path / "item-roles.sqlite"
    sha = IR.publish(str(db), META, MEMS, ROLES)
    assert len(sha) == 64
    conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        tables = sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"))
        assert tables == ["memories", "mention_roles", "meta"]
        assert _columns(conn, "meta") == [("k", "TEXT", 0, 1), ("v", "TEXT", 1, 0)]
        assert _columns(conn, "memories") == [
            ("memory_id", "TEXT", 0, 1), ("document_id", "TEXT", 0, 0), ("mentioned_at", "TEXT", 0, 0),
            ("state", "TEXT", 1, 0), ("text_sha256", "TEXT", 1, 0), ("judged_at", "TEXT", 1, 0)]
        assert _columns(conn, "mention_roles") == [
            ("memory_id", "TEXT", 1, 1), ("key", "TEXT", 1, 2), ("role", "TEXT", 1, 0)]
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='mention_roles'").fetchone()[0]
        assert "CHECK(role IN ('done','open','ref'))" in sql
        meta = dict(conn.execute("SELECT k, v FROM meta"))
        assert meta == dict(META, schema_version="1")
        assert conn.execute("SELECT count(*) FROM memories").fetchone()[0] == 2
        assert sorted(conn.execute("SELECT memory_id, key, role FROM mention_roles")) == [
            ("m1", "hindsight-crew#1", "done"), ("m1", "hindsight-crew#2", "open"), ("m2", "hindsight-crew#1", "ref")]
    finally:
        conn.close()
    assert stat.S_IMODE(db.stat().st_mode) == 0o644
    assert sorted(p.name for p in tmp_path.iterdir()) == ["item-roles.sqlite"]      # 임시·저널 파일이 남지 않는다


def test_role_check_constraint_rejects_other_values(tmp_path):
    db = tmp_path / "s.sqlite"
    IR.publish(str(db), META, MEMS, ROLES)
    conn = sqlite3.connect(str(db))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO mention_roles VALUES('m1', 'x#1', 'maybe')")
    finally:
        conn.close()
    with pytest.raises(IR.StoreError):
        IR.publish(str(tmp_path / "t.sqlite"), META, MEMS, {"m1": {"x#1": "maybe"}})
    assert not (tmp_path / "t.sqlite").exists()


def test_publish_replaces_the_file_not_in_place(tmp_path):
    db = tmp_path / "s.sqlite"
    IR.publish(str(db), META, MEMS, ROLES)
    ino = os.stat(db).st_ino
    held = open(db, "rb")                       # 옛 파일을 열고 있는 리더는 옛 내용을 그대로 본다
    try:
        old = held.read()
        IR.publish(str(db), META, {"m1": MEMS["m1"]}, ROLES)
        assert os.stat(db).st_ino != ino
        assert held.seek(0) == 0 and held.read() == old
    finally:
        held.close()
    meta, mems, roles = IR.load_store(str(db))
    assert set(mems) == {"m1"} and set(roles) == {"m1"}          # 행 없는 기억의 역할은 쓰지 않는다


def test_publish_requires_meta_keys(tmp_path):
    with pytest.raises(IR.StoreError, match="aliases_sha256"):
        IR.publish(str(tmp_path / "s.sqlite"), {k: v for k, v in META.items() if k != "aliases_sha256"}, {}, {})
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("version", ["2", "0", None])
def test_existing_file_with_other_schema_version_is_refused(tmp_path, version):
    db = tmp_path / "s.sqlite"
    IR.publish(str(db), META, MEMS, ROLES)
    conn = sqlite3.connect(str(db))
    if version is None:
        conn.execute("DELETE FROM meta WHERE k='schema_version'")
    else:
        conn.execute("UPDATE meta SET v=? WHERE k='schema_version'", (version,))
    conn.commit()
    conn.close()
    with pytest.raises(IR.StoreError, match="schema_version"):
        IR.load_store(str(db))


def test_non_store_file_is_refused(tmp_path):
    db = tmp_path / "s.sqlite"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE other(x)")
    conn.commit()
    conn.close()
    with pytest.raises(IR.StoreError):
        IR.load_store(str(db))


def test_load_store_round_trip(tmp_path):
    db = tmp_path / "s.sqlite"
    IR.publish(str(db), META, MEMS, ROLES)
    meta, mems, roles = IR.load_store(str(db))
    assert meta == dict(META, schema_version="1")
    assert mems == {k: dict(v, memory_id=k) for k, v in MEMS.items()}
    assert roles == ROLES


def test_default_db_path_is_inside_the_policies_mount():
    assert IR.DEFAULT_DB_PATH == "/app/policies/item-roles.sqlite"
    assert IR.DB_ENV == "HC_ITEM_ROLES_DB"
    assert "policies/item-roles.sqlite*" in (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()


# --- final-review #376 재작업 회귀 ------------------------------------------------------------------------

def _spy_connect(monkeypatch, seen):
    real = sqlite3.connect

    def spy(target, *a, **kw):
        if not kw.get("uri"):
            seen.append((target, stat.S_IMODE(os.stat(target).st_mode)))   # 연결 전에 파일이 0600 으로 있어야 한다
        return real(target, *a, **kw)
    monkeypatch.setattr(IR.sqlite3, "connect", spy)


def test_publish_temp_db_is_0600_before_sqlite_opens_it(tmp_path, monkeypatch):
    """Bugs #1: 임시 DB 를 지우고 다시 만들면 umask 권한(0644)으로 열린다."""
    seen = []
    _spy_connect(monkeypatch, seen)
    old = os.umask(0o022)
    try:
        IR.publish(str(tmp_path / "item-roles.sqlite"), META, MEMS, ROLES, mode=0o600)
    finally:
        os.umask(old)
    assert seen and all(mode == 0o600 for _, mode in seen)
    assert stat.S_IMODE((tmp_path / "item-roles.sqlite").stat().st_mode) == 0o600


def test_publish_temp_name_is_covered_by_the_gitignore_line(tmp_path, monkeypatch):
    """Bugs #2: 남은 임시·저널 파일도 `policies/item-roles.sqlite*` 에 걸려야 한다."""
    import fnmatch
    seen = []
    _spy_connect(monkeypatch, seen)
    IR.publish(str(tmp_path / "item-roles.sqlite"), META, MEMS, ROLES)
    for target, _ in seen:
        name = os.path.basename(target)
        assert fnmatch.fnmatch(name, "item-roles.sqlite*") and fnmatch.fnmatch(name + "-journal", "item-roles.sqlite*")


def test_aliases_sha256_drops_empty_entries_like_item_keys_table():
    """Bugs #3: 키 추출이 실제로 쓰는 표(item_keys._table)와 같은 sha256."""
    import hashlib
    import json
    from gateway import item_keys
    table = {"HC": "Hindsight-Crew", "": "x", "y": " ", "dmd": "larch-yard"}
    canon = json.dumps(item_keys._table(table), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    assert IR.aliases_sha256(table) == hashlib.sha256(canon.encode("utf-8")).hexdigest()
    assert IR.aliases_sha256(table) == IR.aliases_sha256({"hc": "hindsight-crew", "dmd": "larch-yard"})


@pytest.mark.parametrize("dirname", ["run#1", "a%41b", "q?x"])
def test_load_store_handles_uri_special_characters_in_the_path(tmp_path, dirname):
    """Bugs #8: 경로의 #·%·? 가 URI 로 해석되면 다른 파일을 열거나 실패한다."""
    d = tmp_path / dirname
    d.mkdir()
    db = d / "item-roles.sqlite"
    IR.publish(str(db), META, MEMS, ROLES)
    meta, mems, roles = IR.load_store(str(db))
    assert set(mems) == {"m1", "m2"} and roles == ROLES


# --- #383 판정 개선: 다수결·판정 방식 meta(공용 순수 함수) ---------------------------------------------------------

@pytest.mark.parametrize("roles, want", [
    (["open"], "open"),
    (["done", "done", "open"], "done"),
    (["ref", "open", "open"], "open"),
    (["done", "open", "ref"], "ref"),           # 셋이 다 다르면 ref
    (["done", "open"], "ref"),                  # 과반 없음(짝수 동률)
    (["done", "done", "open", "open"], "ref"),
    ([], "ref"),
])
def test_majority_role_needs_a_strict_majority(roles, want):
    assert IR.majority_role(roles) == want


def test_judge_mode_text_and_legacy_value():
    assert IR.judge_mode("sentence", 1) == IR.JUDGE_MODE_LEGACY == "input=sentence;votes=1"
    assert IR.judge_mode("memory", 3) == "input=memory;votes=3"
    assert "judge_mode" not in IR.META_KEYS          # 선택 키(필수 키·스키마 v1 그대로)
