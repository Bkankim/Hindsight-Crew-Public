"""리랭크 후보 상한 블라인드 비교 (issue #13).

상한을 바꾸면 recall 결과가 바뀐다(cap_profile). 바뀐 결과가 나아졌는지 나빠졌는지는 결과 불변 판정으로 알 수 없으므로,
두 설정의 결과를 블라인드 A/B 로 판정자(LLM)에게 보이고 판정 자체의 신뢰도(판정자 간 일치·자기일관성·대조군)를 함께 잰다.
이 결과는 채택 기준이 아니라 "상한이 정보를 잃고 있나"의 근거다.

데이터(쿼리·기억 본문)는 bench/.data/blind/ (gitignore, 0600) 에만 둔다.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Sequence

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(REPO, "bench", ".data", "blind")

_RECALL = r'''
import sys,json,urllib.request as u
k=sys.stdin.readline().strip(); body=sys.stdin.read()
d=json.loads(u.urlopen(u.Request("http://localhost:8888/v1/default/banks/personal-demo/memories/recall",data=body.encode(),
  headers={"Authorization":"Bearer "+k,"Content-Type":"application/json"}),timeout=120).read())
keep=("id","text","context","fact_type","occurred_start","mentioned_at")
print(json.dumps([{f:r.get(f) for f in keep} for r in d.get("results") or []]))
'''


def _mark(side: List[Dict[str, Any]], other: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    theirs = {m["id"] for m in other}
    return [dict(m, unique=m["id"] not in theirs) for m in side]


def blind(items: Sequence[Dict[str, Any]], seed: int, repeats: int = 0
          ) -> "tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]":
    """쿼리별 두 설정의 결과를 A/B 로 무작위 배정한 패킷과, 판정자에게 주지 않는 정답키를 만든다.
    repeats 개 쿼리는 좌우를 뒤집어 한 번 더 넣는다(자기일관성 측정)."""
    import random

    rng = random.Random(seed)
    packets, key = [], {}
    for n, it in enumerate(items):
        a, b = ("300", "600") if rng.random() < 0.5 else ("600", "300")
        pid = f"p{n:03d}"
        sa, sb = it["sides"][a], it["sides"][b]
        packets.append({"pid": pid, "query": it["query"], "A": _mark(sa, sb), "B": _mark(sb, sa)})
        key[pid] = {"key": it["key"], "A": a, "B": b, "control": bool(it.get("control")), "repeat_of": None}
    for n, orig in enumerate(rng.sample([p["pid"] for p in packets], repeats)):
        src = next(p for p in packets if p["pid"] == orig)
        pid = f"r{n:03d}"
        packets.append({"pid": pid, "query": src["query"], "A": src["B"], "B": src["A"]})
        k = key[orig]
        key[pid] = {"key": k["key"], "A": k["B"], "B": k["A"], "control": k["control"], "repeat_of": orig}
    rng.shuffle(packets)
    # 섞은 순서로 새 id 를 매긴다 - 내부 id(p/r 접두어·원래 순번)가 반복 여부와 순서를 드러내지 않게
    rename = {p["pid"]: f"q{n:03d}" for n, p in enumerate(packets)}
    packets = [dict(p, pid=rename[p["pid"]]) for p in packets]
    key = {rename[pid]: dict(a, repeat_of=rename.get(a["repeat_of"])) for pid, a in key.items()}
    return packets, key


LABELS = ("600", "300", "tie")


def _setting(answer: Dict[str, Any], choice: str) -> str:
    return "tie" if choice == "tie" else answer[choice]


def cohen_kappa(x: Sequence[str], y: Sequence[str]) -> float:
    n = len(x)
    po = sum(a == b for a, b in zip(x, y)) / n
    pe = sum((x.count(c) / n) * (y.count(c) / n) for c in LABELS)
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)


def score(key: Dict[str, Dict[str, Any]], judged: Dict[str, Dict[str, str]]) -> Dict[str, Any]:
    """판정(A/B/tie)을 정답키로 풀어 설정별 선호와 판정 신뢰도를 낸다.
    본 판정 = 대조군도 반복도 아닌 패킷. 자기일관성 = 반복 패킷이 원본과 같은 설정을 골랐나.
    대조군(결과 동일) 은 tie 가 정답이다. kappa 는 판정자가 둘일 때 본 판정에서 잰다."""
    primary = sorted(p for p, a in key.items() if not a["control"] and not a["repeat_of"])
    repeats = [p for p, a in key.items() if a["repeat_of"]]
    controls = [p for p, a in key.items() if a["control"] and not a["repeat_of"]]
    out: Dict[str, Any] = {"preference": {}, "self_consistency": {}, "control_tie_rate": {}, "n_primary": len(primary)}
    mapped = {}
    for judge, j in judged.items():
        mapped[judge] = [_setting(key[p], j[p]) for p in primary]
        out["preference"][judge] = {c: mapped[judge].count(c) for c in LABELS}
        out["self_consistency"][judge] = (sum(_setting(key[p], j[p]) == _setting(key[key[p]["repeat_of"]], j[key[p]["repeat_of"]])
                                              for p in repeats) / len(repeats)) if repeats else None
        out["control_tie_rate"][judge] = (sum(j[p] == "tie" for p in controls) / len(controls)) if controls else None
    names = sorted(judged)
    out["kappa"] = cohen_kappa(mapped[names[0]], mapped[names[1]]) if len(names) == 2 and primary else None
    return out


def _write_private(path: str, rows: List[Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def collect(label: str, limit: Optional[int] = None) -> str:
    """현재 복제본 설정으로 dev 쿼리를 재생해 결과(순서 포함)를 <label>.jsonl 로 남긴다."""
    import subprocess

    from bench import replica, runner

    clock, key = runner.bench_clock(), replica.bench_key()
    rows = []
    for row in runner.load_corpus("dev", limit, seed=13):
        args = dict(row["args"], query_timestamp=clock)
        out = subprocess.run(["docker", "exec", "-i", replica.HS, "python", "-c", _RECALL],
                             input=key + "\n" + json.dumps(args), capture_output=True, text=True, check=True).stdout
        rows.append({"key": row["key"], "query": row["args"].get("query"), "results": json.loads(out)})
    path = os.path.join(DATA, f"{label}.jsonl")
    _write_private(path, rows)
    return path


def _read(path: str) -> List[Dict[str, Any]]:
    return [json.loads(line) for line in open(path) if line.strip()]


def render(packet: Dict[str, Any]) -> str:
    """판정자용 텍스트. 공통 기억은 앞부분만, 한쪽에만 있는 기억은 전문(차이에 집중)."""
    def line(m: Dict[str, Any], full: bool) -> str:
        t = (m.get("text") or "").replace("\n", " ")
        meta = f"[{m.get('fact_type') or '-'} {str(m.get('occurred_start') or m.get('mentioned_at') or '-')[:10]}]"
        return f"  - {meta} {t if full else t[:150] + ('...' if len(t) > 150 else '')}"
    shared = [m for m in packet["A"] if not m["unique"]]
    out = [f"### {packet['pid']}", f"QUERY: {packet['query']}", f"공통 기억 {len(shared)}건 (순서는 A 기준):"]
    out += [line(m, False) for m in shared]
    for side in ("A", "B"):
        only = [m for m in packet[side] if m["unique"]]
        out.append(f"{side}에만 있는 기억 {len(only)}건 (A 전체 {len(packet['A'])}건 / B 전체 {len(packet['B'])}건):")
        out += [line(m, True) for m in only]
    return "\n".join(out)


def pack(base: str, other: str, seed: int, controls: int, repeat_frac: float) -> Dict[str, str]:
    import random

    b, o = {r["key"]: r for r in _read(os.path.join(DATA, base + ".jsonl"))}, \
           {r["key"]: r for r in _read(os.path.join(DATA, other + ".jsonl"))}
    ids = lambda r: [m["id"] for m in r["results"]]
    changed = sorted(k for k in b if k in o and ids(b[k]) != ids(o[k]))
    same = sorted(k for k in b if k in o and ids(b[k]) == ids(o[k]))
    ctrl = random.Random(seed).sample(same, min(controls, len(same)))
    items = [{"key": k, "query": b[k]["query"], "sides": {"300": b[k]["results"], "600": o[k]["results"]},
              "control": k in ctrl} for k in changed + ctrl]
    packets, key = blind(items, seed=seed, repeats=round(repeat_frac * len(changed)))
    os.makedirs(DATA, mode=0o700, exist_ok=True)
    paths = {"packets": os.path.join(DATA, "packets.md"), "key": os.path.join(DATA, "key.json")}
    for name, body in (("packets", "\n\n".join(render(p) for p in packets)), ("key", json.dumps(key, indent=1))):
        fd = os.open(paths[name], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(body)
    paths.update(changed=str(len(changed)), unchanged=str(len(same)), controls=str(len(ctrl)), packets_n=str(len(packets)))
    return paths


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect"); c.add_argument("label"); c.add_argument("--limit", type=int)
    p = sub.add_parser("pack"); p.add_argument("base"); p.add_argument("other"); p.add_argument("--seed", type=int, default=13)
    p.add_argument("--controls", type=int, default=8); p.add_argument("--repeat-frac", type=float, default=0.2)
    s = sub.add_parser("score"); s.add_argument("judgments", nargs="+", help="name=path.json ({pid: A|B|tie})")
    a = ap.parse_args(argv)
    if a.cmd == "collect":
        print(collect(a.label, a.limit))
    elif a.cmd == "pack":
        print(json.dumps(pack(a.base, a.other, a.seed, a.controls, a.repeat_frac), indent=1))
    elif a.cmd == "score":
        key = json.load(open(os.path.join(DATA, "key.json")))
        judged = {n: {pid: v if isinstance(v, str) else v["choice"] for pid, v in json.load(open(path)).items()}
                  for n, path in (x.split("=", 1) for x in a.judgments)}
        print(json.dumps(score(key, judged), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
