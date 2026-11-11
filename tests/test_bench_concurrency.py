"""issue #13 B1 동시성 탐침 - 두 recall 을 동시에 보냈을 때 설정별로 느린 쪽 지연·데드라인 초과·오류를 요약한다."""
from bench import concurrency_probe as C


def test_summary_reports_slower_member_and_errors():
    pairs = [
        {"config": "base", "latencies_ms": [5000, 6000], "outcomes": ["ok", "ok"]},
        {"config": "base", "latencies_ms": [5200, 6400], "outcomes": ["ok", "ok"]},
        {"config": "cand", "latencies_ms": [4000, 8200], "outcomes": ["ok", "ok"]},
        {"config": "cand", "latencies_ms": [4100, 9000], "outcomes": ["ok", "upstream-error"]},
    ]
    # recall 전체 지연은 사이드카 데드라인(요청 하나의 대기 + 처리)과 비교하지 않는다 - 데드라인 위반은
    # 사이드카의 503 으로만 드러나고, 그 경우 upstream 이 재시도 뒤 실패하므로 errors 에 잡힌다
    s = C.summarize(pairs)
    assert s["base"] == {"pairs": 2, "slower_median_ms": 6200.0, "slower_max_ms": 6400.0, "errors": 0}
    assert s["cand"] == {"pairs": 2, "slower_median_ms": 8600.0, "slower_max_ms": 9000.0, "errors": 1}
