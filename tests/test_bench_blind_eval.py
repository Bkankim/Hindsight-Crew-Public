"""issue #13 후보 상한 블라인드 비교 - 판정자가 설정을 알 수 없게 섞고, 판정 자체의 신뢰도를 잰다."""
from bench import blind_eval as B


def mem(i):
    return {"id": f"m{i}", "text": f"memory {i}"}


def item(key, r300, r600, control=False):
    return {"key": key, "query": f"q-{key}", "sides": {"300": r300, "600": r600}, "control": control}


def test_packets_hide_the_setting_and_the_key_recovers_it():
    items = [item(f"k{i}", [mem(1), mem(2)], [mem(1), mem(3)]) for i in range(6)]
    packets, key = B.blind(items, seed=7)

    assert len(packets) == 6
    for p in packets:
        assert set(p) == {"pid", "query", "A", "B"}                     # 설정 라벨은 패킷에 없다
        ans = key[p["pid"]]
        assert {ans["A"], ans["B"]} == {"300", "600"}
        src = next(i for i in items if i["key"] == ans["key"])
        assert [m["id"] for m in p["A"]] == [m["id"] for m in src["sides"][ans["A"]]]
    # 한쪽에만 있는 기억을 표시해 판정자가 차이에 집중하게 한다(라벨은 드러내지 않는다)
    p = packets[0]
    assert [m["unique"] for m in p["A"]] == [False, True] and [m["unique"] for m in p["B"]] == [False, True]
    # 시드 고정 무작위: 6개 중 A=300 과 A=600 이 둘 다 나온다
    assert {key[p["pid"]]["A"] for p in packets} == {"300", "600"}


def test_repeats_swap_sides_to_measure_self_consistency():
    items = [item(f"k{i}", [mem(1)], [mem(2)]) for i in range(4)]
    packets, key = B.blind(items, seed=7, repeats=2)

    assert len(packets) == 6
    reps = [pid for pid, a in key.items() if a["repeat_of"]]
    assert len(reps) == 2
    # 패킷 id 로 반복 여부나 원래 순서를 알 수 없어야 한다(섞은 뒤 순번으로 매긴다)
    assert [p["pid"] for p in packets] == [f"q{n:03d}" for n in range(6)]
    for pid in reps:
        orig = key[key[pid]["repeat_of"]]
        assert key[pid]["key"] == orig["key"] and key[pid]["A"] == orig["B"]     # 같은 쿼리, 좌우만 뒤집힘


def ans(k, a, repeat_of=None, control=False):
    return {"key": k, "A": a, "B": "600" if a == "300" else "300", "control": control, "repeat_of": repeat_of}


KEY = {"q0": ans("k0", "300"), "q1": ans("k1", "600"), "q2": ans("k2", "300"), "q3": ans("k3", "600"),
       "q4": ans("k0", "600", repeat_of="q0"), "q5": ans("k5", "300", control=True)}
JUDGED = {
    # 설정으로 풀면 X: 600, 600, tie, 300 / 반복 q4 는 q0 과 같은 600 / 대조군 tie
    "X": {"q0": "B", "q1": "A", "q2": "tie", "q3": "B", "q4": "A", "q5": "tie"},
    # Y: 600, 600, 300, 300 / 반복 q4 는 300 (q0 과 불일치) / 대조군에서 A(=300) 를 고름
    "Y": {"q0": "B", "q1": "A", "q2": "A", "q3": "B", "q4": "B", "q5": "A"},
}


def test_score_unblinds_preferences_and_measures_judge_reliability():
    s = B.score(KEY, JUDGED)
    assert s["preference"] == {"X": {"600": 2, "300": 1, "tie": 1}, "Y": {"600": 2, "300": 2, "tie": 0}}
    assert s["self_consistency"] == {"X": 1.0, "Y": 0.0}
    assert s["control_tie_rate"] == {"X": 1.0, "Y": 0.0}
    assert abs(s["kappa"] - 0.6) < 1e-9          # po 0.75, pe 0.375 (손계산)
