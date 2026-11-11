"""issue #177: 감사 로그 리더 3개(model_audit·duplicate_recall·tag_hygiene)가 회전 세대(.N.gz)와 줄 앞 NUL을 읽는다.

합성 로그는 전부 tmp_path 에만 만든다(운영 audit/ 는 #54 판정과 카나리가 읽는 파일이라 열지 않는다).
행렬: 리더 x 입력 모양. 입력 모양은 회전 세대 없음 / .1.gz / 헤더만 있는 손상 .2.gz / 첫 줄 앞 NUL 의 조합이다.
"""
import contextlib
import datetime as dt
import gzip
import importlib.util
import io
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
DEMO_POLICY = ROOT / "tests" / "fixtures" / "policies" / "sample" / "bank-policies.json"   # personal-demo (issue #187)
BASE = dt.datetime(2026, 9, 21, 12, 0, 0, tzinfo=dt.timezone.utc)
SINCE = dt.datetime(2026, 9, 20, tzinfo=dt.timezone.utc)
GZ_ROWS, LIVE_ROWS = 3, 2          # .1.gz 에 3줄, 라이브에 2줄

# 입력 모양: (회전본 .1.gz, 헤더만 있는 손상 .2.gz, 라이브 첫 줄 앞 NUL)
SHAPES = {
    "live-only": (False, False, False),
    "live+gz": (True, False, False),
    "live+gz+damaged": (True, True, False),
    "live+nul": (False, False, True),
    "live+gz+damaged+nul": (True, True, True),
}


def load_script(name, module_name):
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def iso(index):
    return (BASE + dt.timedelta(seconds=index * 10)).strftime("%Y-%m-%dT%H:%M:%SZ")


def jsonl(rows):
    return "".join(json.dumps(r) + "\n" for r in rows).encode()


def write_generations(path, shape, make_row):
    """shape 대로 path(+ .1.gz, .2.gz)를 쓰고, 읽혀야 하는 줄 수를 돌려준다."""
    gz, damaged, nul = SHAPES[shape]
    index = 0
    if damaged:     # gzip 헤더 10바이트만 있고 본문이 없다: 읽히는 줄 0
        Path(str(path) + ".2.gz").write_bytes(gzip.compress(b"x")[:10])
    expected = 0
    if gz:
        rows = [make_row(index + i) for i in range(GZ_ROWS)]
        index += GZ_ROWS
        Path(str(path) + ".1.gz").write_bytes(gzip.compress(jsonl(rows)))
        expected += GZ_ROWS
    live = jsonl([make_row(index + i) for i in range(LIVE_ROWS)])
    if nul:         # colima virtiofs 에서 호스트 `: >` 회전 뒤 첫 줄 앞에 생기는 NUL 구멍
        live = b"\x00" * 37 + live
    Path(path).write_bytes(live)
    return expected + LIVE_ROWS


def reflect_row(i):
    return {"ts": iso(i), "resolved_bank": "personal-demo", "decision": "allow", "tool": "reflect",
            "reason": "allow:reflect"}


def match_row(i):
    return {"ts": iso(i), "resolved_bank": "personal-demo", "decision": "context-trigger", "reason": "match:t1"}


def ledger_row(i):
    return {"ts": iso(i), "identity": "demo", "bank": "personal-demo", "transport": "mcp", "query": f"q{i}",
            "tags": [], "result_ids": ["m1"], "session_id": "s1", "tags_match": "any"}


def audit_recall_row(i):
    return {"ts": iso(i), "identity": "demo", "resolved_bank": "personal-demo", "attempted_bank": "personal-demo",
            "method": "tools/call", "tool": "recall", "decision": "allow", "reason": "allow:recall"}


def retain_tags_row(i):
    return {"ts": iso(i), "resolved_bank": "personal-demo", "decision": "retain-tags", "reason": "tags-unchanged",
            "tool": "retain", "retain_tags": {"transport": "mcp", "client": "quince",
                                              "sent": ["topic:gotcha"], "stored": ["topic:gotcha"]}}


@pytest.fixture
def audit_module(monkeypatch, tmp_path):
    module = load_script("model_audit.py", "model_audit_gen_under_test")
    monkeypatch.setattr(module, "GATEWAY_AUDIT_LOG", str(tmp_path / "gateway.log"))
    return module


@pytest.fixture(scope="module")
def dup():
    return load_script("duplicate_recall.py", "duplicate_recall_gen_under_test")


@pytest.fixture(scope="module")
def hygiene():
    return load_script("tag_hygiene.py", "tag_hygiene_gen_under_test")


def run_quiet(fn, *args, **kwargs):
    with contextlib.redirect_stderr(io.StringIO()):
        return fn(*args, **kwargs)


def run_dup(dup, argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert dup.main(argv) == 0
    return json.loads(buf.getvalue())


@pytest.mark.parametrize("shape", SHAPES)
def test_model_audit_demand_counts_every_generation(audit_module, tmp_path, shape):
    expected = write_generations(tmp_path / "gateway.log", shape, reflect_row)
    report = run_quiet(audit_module.demand_signal, set(), SINCE)
    assert report["available"] is True
    assert report["reflect_calls"] == expected


@pytest.mark.parametrize("shape", SHAPES)
def test_model_audit_injection_counts_every_generation(audit_module, tmp_path, shape):
    expected = write_generations(tmp_path / "gateway.log", shape, match_row)
    report = run_quiet(audit_module.injection_signal, SINCE)
    assert report["available"] is True
    assert report["matched"] == expected


@pytest.mark.parametrize("shape", SHAPES)
def test_duplicate_recall_counts_every_audit_generation(dup, tmp_path, shape):
    ledger = tmp_path / "recall-queries.log"
    ledger.write_bytes(jsonl([ledger_row(0)]))
    expected = write_generations(tmp_path / "gateway.log", shape, audit_recall_row)
    result = run_dup(dup, ["--ledger", str(ledger), "--audit", str(tmp_path / "gateway.log")])
    assert result["input"]["audit_lines"] == expected
    assert result["input"]["audit_skipped"] == 0


@pytest.mark.parametrize("shape", SHAPES)
def test_duplicate_recall_counts_every_ledger_generation(dup, tmp_path, shape):
    audit = tmp_path / "gateway.log"
    audit.write_bytes(jsonl([audit_recall_row(0)]))
    expected = write_generations(tmp_path / "recall-queries.log", shape, ledger_row)
    result = run_dup(dup, ["--ledger", str(tmp_path / "recall-queries.log"), "--audit", str(audit)])
    assert result["input"]["ledger_lines"] == expected
    assert result["input"]["ledger_skipped"] == 0


@pytest.mark.parametrize("shape", SHAPES)
def test_tag_hygiene_report_counts_every_audit_generation(hygiene, tmp_path, shape):
    docs = tmp_path / "docs.jsonl"
    docs.write_bytes(jsonl([{"id": "d1", "created_at": iso(0), "tags": ["kind:fix", "topic:gotcha"]}]))
    expected = write_generations(tmp_path / "gateway.log", shape, retain_tags_row)
    out = io.StringIO()
    args = hygiene.build_parser().parse_args(
        ["report", str(docs), "--json", "--audit", str(tmp_path / "gateway.log"), "--policy", str(DEMO_POLICY)])
    assert hygiene.run_report(args, out=out) == 0
    audit = json.loads(out.getvalue())["audit"]
    assert audit["retain_tag_lines"] == expected
    assert audit["unparsable_lines"] == 0


# ---------------------------------------------------------------------------
# 새 키(커버리지 부족·공통 구간·손상 사유·NUL 바이트 수)와 헬퍼 - 회전 세대나 NUL 이 있을 때만 출력에 들어간다

NUL_PREFIX = 37                         # write_generations 가 라이브 첫 줄 앞에 붙이는 NUL 수
EARLIER = SINCE - dt.timedelta(days=3)  # 창 시작보다 앞선 ts


def write_rotated(path, rows_gz, rows_live):
    Path(str(path) + ".1.gz").write_bytes(gzip.compress(jsonl(rows_gz)))
    Path(path).write_bytes(jsonl(rows_live))


def test_model_audit_flags_short_coverage_when_rotated_and_earliest_is_after_window_start(audit_module, tmp_path):
    write_generations(tmp_path / "gateway.log", "live+gz", reflect_row)       # 가장 이른 ts = BASE > SINCE
    for report in (run_quiet(audit_module.demand_signal, set(), SINCE), run_quiet(audit_module.injection_signal, SINCE)):
        assert report["log_read"]["coverage_short"] is True
        assert report["log_read"]["earliest_ts"] == iso(0)
        assert report["log_read"]["rotated_generations"] == 1


def test_model_audit_does_not_flag_short_coverage_when_the_window_start_is_covered(audit_module, tmp_path):
    early = dict(reflect_row(0), ts=EARLIER.strftime("%Y-%m-%dT%H:%M:%SZ"))
    write_rotated(tmp_path / "gateway.log", [early], [reflect_row(1)])
    report = run_quiet(audit_module.demand_signal, set(), SINCE)
    assert report["log_read"]["coverage_short"] is False
    assert report["reflect_calls"] == 1                                       # 창 밖(EARLIER) 줄은 세지 않는다


@pytest.mark.parametrize("signal", ["demand", "injection"])
def test_model_audit_adds_no_new_keys_without_rotation_or_nul(audit_module, tmp_path, signal):
    """회전 세대도 NUL 도 없으면 로그가 창 시작보다 늦게 시작해도 출력은 지금과 같다(새 키 없음)."""
    write_generations(tmp_path / "gateway.log", "live-only", reflect_row if signal == "demand" else match_row)
    report = (run_quiet(audit_module.demand_signal, set(), SINCE) if signal == "demand"
              else run_quiet(audit_module.injection_signal, SINCE))
    assert "log_read" not in report


def test_model_audit_records_nul_bytes_and_damaged_reason(audit_module, tmp_path):
    write_generations(tmp_path / "gateway.log", "live+gz+damaged+nul", reflect_row)
    info = run_quiet(audit_module.demand_signal, set(), SINCE)["log_read"]
    assert info["nul_bytes"] == NUL_PREFIX
    assert info["files"] == ["gateway.log.2.gz", "gateway.log.1.gz", "gateway.log"]
    assert len(info["damaged"]) == 1 and info["damaged"][0].startswith("gateway.log.2.gz: EOFError")


def test_model_audit_nul_alone_adds_log_read_but_not_short_coverage(audit_module, tmp_path):
    write_generations(tmp_path / "gateway.log", "live+nul", reflect_row)
    info = run_quiet(audit_module.demand_signal, set(), SINCE)["log_read"]
    assert info["nul_bytes"] == NUL_PREFIX and info["rotated_generations"] == 0
    assert info["coverage_short"] is False                                     # 회전 세대가 없으면 부족 표시는 하지 않는다


def test_duplicate_recall_prints_the_common_coverage_of_both_logs(dup, tmp_path):
    ledger, audit = tmp_path / "recall-queries.log", tmp_path / "gateway.log"
    write_rotated(ledger, [ledger_row(5)], [ledger_row(6), ledger_row(20)])       # 원장 [5, 20] (초 단위 index*10)
    write_rotated(audit, [audit_recall_row(0)], [audit_recall_row(1), audit_recall_row(22)])   # 감사 [0, 22]
    result = run_dup(dup, ["--ledger", str(ledger), "--audit", str(audit)])
    # from: 원장 응답 완료 ts 는 감사 도착 ts 보다 최대 120초 늦다 -> 감사 시작(0) + 120초 = index 12 (원장 시작 5 보다 늦다)
    assert result["input"]["common_coverage"] == {"from": iso(12), "to": iso(20)}
    assert result["input"]["ledger_log"]["rotated_generations"] == 1
    assert result["input"]["audit_log"]["rotated_generations"] == 1


def test_duplicate_recall_common_coverage_is_null_when_logs_do_not_overlap(dup, tmp_path):
    ledger, audit = tmp_path / "recall-queries.log", tmp_path / "gateway.log"
    write_rotated(ledger, [ledger_row(10)], [ledger_row(11)])
    write_rotated(audit, [audit_recall_row(0)], [audit_recall_row(1)])
    result = run_dup(dup, ["--ledger", str(ledger), "--audit", str(audit)])
    assert result["input"]["common_coverage"] == {"from": None, "to": None}


def test_duplicate_recall_records_damaged_reason_and_nul(dup, tmp_path):
    ledger, audit = tmp_path / "recall-queries.log", tmp_path / "gateway.log"
    ledger.write_bytes(jsonl([ledger_row(0)]))
    write_generations(audit, "live+gz+damaged+nul", audit_recall_row)
    info = run_dup(dup, ["--ledger", str(ledger), "--audit", str(audit)])["input"]["audit_log"]
    assert info["nul_bytes"] == NUL_PREFIX
    assert len(info["damaged"]) == 1 and info["damaged"][0].startswith("gateway.log.2.gz: EOFError")


def test_duplicate_recall_adds_no_new_keys_without_rotation_or_nul(dup, tmp_path):
    ledger, audit = tmp_path / "recall-queries.log", tmp_path / "gateway.log"
    ledger.write_bytes(jsonl([ledger_row(0)]))
    audit.write_bytes(jsonl([audit_recall_row(0)]))
    assert set(run_dup(dup, ["--ledger", str(ledger), "--audit", str(audit)])["input"]) == {
        "ledger_lines", "ledger_skipped", "audit_lines", "audit_skipped"}


def test_duplicate_recall_accepts_a_rotated_generation_path_directly(dup, tmp_path):
    ledger, audit = tmp_path / "recall-queries.log", tmp_path / "gateway.log"
    ledger.write_bytes(jsonl([ledger_row(0)]))
    write_generations(audit, "live+gz", audit_recall_row)
    result = run_dup(dup, ["--ledger", str(ledger), "--audit", str(audit) + ".1.gz"])    # 예전엔 UnicodeDecodeError
    assert result["input"]["audit_lines"] == GZ_ROWS
    assert result["input"]["audit_log"]["rotated_generations"] == 1


def run_hygiene(hygiene, tmp_path, audit_args):
    docs = tmp_path / "docs.jsonl"
    docs.write_bytes(jsonl([{"id": "d1", "created_at": iso(0), "tags": ["kind:fix", "topic:gotcha"]}]))
    out = io.StringIO()
    args = hygiene.build_parser().parse_args(["report", str(docs), "--json", *audit_args, "--policy", str(DEMO_POLICY)])
    assert hygiene.run_report(args, out=out) == 0
    return json.loads(out.getvalue())["audit"]


def test_tag_hygiene_records_nul_and_damaged_reason(hygiene, tmp_path):
    write_generations(tmp_path / "gateway.log", "live+gz+damaged+nul", retain_tags_row)
    audit = run_hygiene(hygiene, tmp_path, ["--audit", str(tmp_path / "gateway.log")])
    assert audit["log_read"]["nul_bytes"] == NUL_PREFIX
    assert len(audit["log_read"]["damaged"]) == 1 and audit["log_read"]["damaged"][0].startswith("gateway.log.2.gz: EOFError")
    assert audit["sources"][0]["lines"] == GZ_ROWS + LIVE_ROWS


def test_tag_hygiene_does_not_double_count_when_live_and_generation_are_both_given(hygiene, tmp_path):
    write_generations(tmp_path / "gateway.log", "live+gz", retain_tags_row)
    audit = run_hygiene(hygiene, tmp_path, ["--audit", str(tmp_path / "gateway.log"),
                                            "--audit", str(tmp_path / "gateway.log.1.gz")])
    assert audit["retain_tag_lines"] == GZ_ROWS + LIVE_ROWS


def test_tag_hygiene_accepts_a_rotated_generation_path_directly(hygiene, tmp_path):
    write_generations(tmp_path / "gateway.log", "live+gz", retain_tags_row)
    audit = run_hygiene(hygiene, tmp_path, ["--audit", str(tmp_path / "gateway.log.1.gz")])    # 예전엔 rc=2
    assert audit["retain_tag_lines"] == GZ_ROWS


def test_tag_hygiene_adds_no_new_keys_without_rotation_or_nul(hygiene, tmp_path):
    write_generations(tmp_path / "gateway.log", "live-only", retain_tags_row)
    assert "log_read" not in run_hygiene(hygiene, tmp_path, ["--audit", str(tmp_path / "gateway.log")])


@pytest.fixture(scope="module")
def reader():
    sys.path.insert(0, str(SCRIPTS))
    try:
        return importlib.import_module("audit_log_reader")
    finally:
        sys.path.remove(str(SCRIPTS))


def test_helper_orders_generations_oldest_first_and_ignores_unrelated_files(reader, tmp_path):
    base = tmp_path / "gateway.log"
    base.write_text("live\n")
    for n in (1, 2, 10):                                    # 10 은 2 보다 오래됐다(숫자 정렬)
        (tmp_path / f"gateway.log.{n}.gz").write_bytes(gzip.compress(f"gen{n}\n".encode()))
    (tmp_path / "gateway.log.bak").write_text("nope\n")
    (tmp_path / "other.log.1.gz").write_bytes(gzip.compress(b"nope\n"))
    assert reader.read_log(str(base)).lines == ["gen10", "gen2", "gen1", "live"]


def test_helper_uses_the_readable_prefix_of_a_truncated_gz_and_drops_the_partial_last_line(reader, tmp_path):
    rows = [{"i": i, "pad": f"{i * 2654435761 % 2**32:08x}" * 8} for i in range(4000)]
    blob = gzip.compress(jsonl(rows))
    (tmp_path / "gateway.log.1.gz").write_bytes(blob[:len(blob) // 2])
    (tmp_path / "gateway.log").write_bytes(jsonl([{"i": "live"}]))
    read = reader.read_log(str(tmp_path / "gateway.log"))
    assert 0 < len(read.lines) - 1 < len(rows)
    assert all(isinstance(json.loads(line), dict) for line in read.lines)       # 반쯤 풀린 줄이 없다
    assert [json.loads(line)["i"] for line in read.lines[:-1]] == list(range(len(read.lines) - 1))
    assert read.damaged and read.damaged[0].startswith("gateway.log.1.gz: EOFError")


@pytest.mark.parametrize("content,reason", [(b"", "empty file"), (b"not a gzip file at all\n", "BadGzipFile")])
def test_helper_reports_empty_and_non_gzip_generations(reader, tmp_path, content, reason):
    (tmp_path / "gateway.log.1.gz").write_bytes(content)
    (tmp_path / "gateway.log").write_bytes(b'{"a": 1}\n')
    read = reader.read_log(str(tmp_path / "gateway.log"))
    assert read.lines == ['{"a": 1}'] and read.damaged[0].startswith(f"gateway.log.1.gz: {reason}")


def test_helper_strips_only_leading_nul_and_counts_them(reader, tmp_path):
    (tmp_path / "gateway.log").write_bytes(b'\x00\x00\x00{"a": 1}\n{"b": "x\x00y"}\n\x00\x00\n')
    read = reader.read_log(str(tmp_path / "gateway.log"))
    assert read.lines == ['{"a": 1}', '{"b": "x\x00y"}']       # 줄 가운데 NUL 은 건드리지 않고, NUL 만 있는 줄은 빈 줄이다
    assert read.nul_bytes == 5 and read.extras


def test_helper_missing_live_file_raises(reader, tmp_path):
    with pytest.raises(FileNotFoundError):
        reader.read_log(str(tmp_path / "gateway.log"))


def test_tag_hygiene_imports_the_helper_lazily():
    """tag_hygiene.py 는 파일 하나로 컨테이너에 흘려 넣어 돌린다: 모듈 최상단 import 에 헬퍼가 없어야 한다."""
    import ast
    tree = ast.parse((SCRIPTS / "tag_hygiene.py").read_text(encoding="utf-8"))
    top = {(a.name if isinstance(n, ast.Import) else n.module).split(".")[0]
           for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    assert "audit_log_reader" not in top


def test_helper_skips_a_generation_that_vanished_after_listing_and_records_it(reader, tmp_path, monkeypatch):
    """rotate-logs.sh 가 세대 목록을 만든 뒤 .N.gz 를 mv 하면: 그 세대만 건너뛰고 사유를 남긴다(FileNotFoundError 로 죽지 않는다)."""
    live = tmp_path / "gateway.log"
    live.write_bytes(b'{"a": 1}\n')
    (tmp_path / "gateway.log.2.gz").write_bytes(gzip.compress(b'{"old": 1}\n'))
    gone = tmp_path / "gateway.log.1.gz"                    # 목록에는 있었지만 읽을 때는 없다
    monkeypatch.setattr(reader, "generation_files", lambda p: [str(tmp_path / "gateway.log.2.gz"), str(gone), p])
    read = reader.read_log(str(live))
    assert read.lines == ['{"old": 1}', '{"a": 1}']
    assert read.rotated == 1 and read.extras
    assert len(read.damaged) == 1 and read.damaged[0].startswith("gateway.log.1.gz: FileNotFoundError")
    with pytest.raises(FileNotFoundError):                  # 호출자가 직접 가리킨 파일은 없으면 예전처럼 오류다
        reader.read_log(str(tmp_path / "missing.log.1.gz"))


@pytest.mark.parametrize("with_gz", [True, False], ids=["with-1.gz", "without-1.gz"])
def test_helper_reads_an_uncompressed_dot_1_between_1_gz_and_live(reader, tmp_path, with_gz):
    """`cp path path.1 && : > path` 직후 gzip 전에 남는 path.1 은 .1.gz 와 라이브 사이 세대다(.1.gz 와 둘 다 있는 창 포함)."""
    live = tmp_path / "gateway.log"
    live.write_bytes(b'{"g": "live"}\n')
    (tmp_path / "gateway.log.1").write_bytes(b'{"g": "dot1"}\n')
    if with_gz:
        (tmp_path / "gateway.log.1.gz").write_bytes(gzip.compress(b'{"g": "gz1"}\n'))
    read = reader.read_log(str(live))
    expected = ['{"g": "gz1"}'] if with_gz else []
    assert read.lines == expected + ['{"g": "dot1"}', '{"g": "live"}']
    assert [os.path.basename(f) for f in read.files][-2:] == ["gateway.log.1", "gateway.log"]
    assert read.rotated == (2 if with_gz else 1) and read.extras and read.damaged == []
