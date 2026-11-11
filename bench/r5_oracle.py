"""런 5 오라클 상한(A4, issue #362, 상위 #360 "지표(R5-A2)"의 "오라클 상한").

같은 t 의 필터 뒤 recall 합집합(`r5_metrics.t_input`)을 라벨(정답)을 알고 고친 입력 두 가지로 바꾼다. d-llm 이 그 입력에서
기준선 오답(양성 쌍 과반 열림)을 얼마나 덜 내는지가 처치(S1) 상한이다. 변환은 입력 행을 바꾸지 않고 새 목록을 돌려준다.

  * o1(완료 근거 주석): 그 t 의 dev 양성 키를 말하는 완료 계열이 아닌 행(낡은 열림 행)마다 본문 앞에 짧은 주석
    `[항목 상태: <키> 끝남 <날짜> 근거 <id>]` 를 붙인다(키가 여럿이면 공백으로 잇는다). 근거 = 기억 메타(R5-A1
    `r5-a1-meta.jsonl`)에서 그 키의 유효(state=valid) 완료 계열 기억 중 mentioned_at <= t 끝 시각인 최신 1개(id, KST 날짜).
    근거가 없는 양성 키는 주석을 달지 않는다(양성 정의상 이번 데이터에는 없다). 행 순서·수는 그대로다. S1 구조 필드
    (`gateway_item_ledger`)는 흉내 내지 않는다(검색 지표는 S1 을 그 필드로만 인식한다). S1 과 같은 모양(행 안 주석)에
    키 인식이 완벽한 경우라 S1 의 상한이다.
  * o2(낡은 열림 행 제거): 그 t 의 dev 양성 키를 말하는 완료 계열이 아닌 행을 뺀다. 단 그 t 의 dev 대조 키(열린 일)를 함께
    말하는 행은 남긴다(열린 일 근거를 지우면 G2 를 해친다, `kept_mixed_control` 로 센다). 라벨이 없는 키만 함께 말하면 뺀다.

  * o3(뱅크 상태 표, #362 범위 확장): 질의 범위(그 t 의 프로젝트 상태 질의와 요청 가족의 project 범위) 안 작업 키 전부의 최신
    상태 표(줄: 키 | 상태 | 날짜 | 근거 id)를 recall 행 뒤에 붙인다. 상태는 뱅크 기억(기억 메타의 키·태그·시각)만으로 정한다:
    t 끝 시각 이하 유효 완료 계열 기억이 있으면 끝남(최신 완료 기억), 없고 t 이하 언급이 있으면 열림(최신 언급 기억).
    완료 계열은 S1 쪽 `gateway.item_status.is_completion_family`(뱅크 `tag_aliases` 반영, 리포 템플릿 정책의 표)를 쓴다.
    GitHub·라벨셋은 표 입력이 아니다(순환 방지). 라벨 쪽 규칙(`bench/r5_labels.py`, 기억 메타의 `completion` 칸)도 쓰지 않는다.
    o3+o1 = 표 + o1 행별 주석.

o1·o2 의 완료 계열 판정과 키 추출은 검색 지표와 같다(`r5_metrics.row_is_completion` = 라벨 쪽 태그 규칙, `row_keys` = extract_keys).
이 모듈은 `bench/r5_labels.py` 를 직접 import 하지 않는다(o3 표가 라벨 쪽 규칙을 따라가지 않게, 시각 처리는 아래 자체 함수).
"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from bench import r5_metrics as R
from gateway.item_status import is_completion_family

KST = timezone(timedelta(hours=9))
DONE, OPEN = "끝남", "열림"


def _parse(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        v = datetime.fromisoformat(text)
    except ValueError:
        return None
    return v if v.tzinfo else v.replace(tzinfo=timezone.utc)


def _t_end(t: date) -> datetime:
    return datetime.combine(t, time(23, 59, 59, 999999), tzinfo=KST)


def _kst_day(at: datetime) -> str:
    return at.astimezone(KST).date().isoformat()


def note(key: str, ev: Mapping[str, Any]) -> str:
    return f"[항목 상태: {key} 끝남 {ev['date']} 근거 {ev['id']}]"


def completion_evidence(meta: Iterable[Mapping[str, Any]], keys: Iterable[str], t: date) -> Dict[str, Dict[str, str]]:
    """키마다 t 끝 시각 이하 유효 완료 계열 기억 중 mentioned_at 최신 1개 -> {"id", "date"(KST)}."""
    want = set(keys)
    end = _t_end(t)
    best: Dict[str, Tuple[Any, str]] = {}
    for m in meta:
        if m.get("state") != "valid" or not m.get("completion"):
            continue
        at = _parse(m.get("mentioned_at"))
        if at is None or at > end:
            continue
        for k in m.get("keys") or []:
            if k in want and (k not in best or at > best[k][0] or (at == best[k][0] and m["id"] > best[k][1])):
                best[k] = (at, m["id"])
    return {k: {"id": rid, "date": _kst_day(at)} for k, (at, rid) in best.items()}


def _with_text(row: Mapping[str, Any], prefix: str) -> Dict[str, Any]:
    out = dict(row)
    fld = "text" if isinstance(row.get("text"), str) or not isinstance(row.get("content"), str) else "content"
    out[fld] = f"{prefix} {row.get(fld) or ''}"
    return out


def o1(rows: Sequence[Mapping[str, Any]], positives: Set[str], evidence: Mapping[str, Mapping[str, Any]],
       aliases: Mapping[str, str]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    out, n_rows, keys_done = [], 0, set()
    for row in rows:
        if R.row_is_completion(row):
            out.append(dict(row))
            continue
        ks = sorted(k for k in R.row_keys(row, aliases) & positives if k in evidence)
        if not ks:
            out.append(dict(row))
            continue
        out.append(_with_text(row, " ".join(note(k, evidence[k]) for k in ks)))
        n_rows += 1
        keys_done.update(ks)
    return out, {"rows": len(rows), "annotated_rows": n_rows, "annotated_keys": len(keys_done)}


def o2(rows: Sequence[Mapping[str, Any]], positives: Set[str], controls: Set[str],
       aliases: Mapping[str, str]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    out, dropped, mixed = [], 0, 0
    for row in rows:
        if not R.row_is_completion(row):
            ks = R.row_keys(row, aliases)
            if ks & positives:
                if ks & controls:
                    mixed += 1
                else:
                    dropped += 1
                    continue
        out.append(dict(row))
    return out, {"rows": len(rows), "dropped_rows": dropped, "kept_mixed_control": mixed}


# --- o3: 뱅크 상태 표 ------------------------------------------------------------------------------

def load_tag_aliases(path: str) -> Dict[str, str]:
    """정책 JSON(리포 템플릿 등)에서 처음 나오는 `tag_aliases` 객체."""
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    stack = [doc]
    while stack:
        x = stack.pop(0)
        if isinstance(x, dict):
            if isinstance(x.get("tag_aliases"), dict):
                return {str(k): str(v) for k, v in x["tag_aliases"].items()}
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)
    return {}


def _key_sort(k: str) -> Tuple[str, int]:
    repo, _, n = k.rpartition("#")
    return repo, int(n) if n.isdigit() else 0


def status_table(meta: Iterable[Mapping[str, Any]], projects: Iterable[str], t: date,
                 tag_aliases: Mapping[str, str]) -> List[Dict[str, str]]:
    """질의 범위 프로젝트 안 키마다 t 시점 뱅크 상태(끝남: 최신 완료 계열 기억, 열림: 완료 없고 최신 언급 기억)."""
    scope = set(projects)
    end = _t_end(t)
    done: Dict[str, Tuple[datetime, str]] = {}
    seen: Dict[str, Tuple[datetime, str]] = {}
    for m in meta:
        if m.get("state") != "valid":
            continue
        at = _parse(m.get("mentioned_at"))
        if at is None or at > end:
            continue
        is_done = is_completion_family({"tags": list(m.get("tags") or [])}, tag_aliases)
        for k in m.get("keys") or []:
            if k.rpartition("#")[0] not in scope:
                continue
            cur = (at, str(m.get("id")))
            if k not in seen or cur > seen[k]:
                seen[k] = cur
            if is_done and (k not in done or cur > done[k]):
                done[k] = cur
    rows = []
    for k in sorted(seen, key=_key_sort):
        at, rid = done.get(k) or seen[k]
        rows.append({"key": k, "state": DONE if k in done else OPEN, "date": _kst_day(at), "id": rid})
    return rows


def table_text(rows: Sequence[Mapping[str, str]], t: Any) -> str:
    head = [f"<item_status_table> 뱅크 기억 기준 작업 키별 최신 상태(기준일 {t} 이전 기억만). 줄: 키 | 상태 | 날짜 | 근거 id"]
    body = [f"{r['key']} | {r['state']} | {r['date']} | {r['id']}" for r in rows]
    return "\n".join(head + body + ["</item_status_table>"])


def table_stats(rows: Sequence[Mapping[str, str]], text: str, counter=None) -> Dict[str, Any]:
    per: Dict[str, int] = {}
    for r in rows:
        repo = r["key"].rpartition("#")[0]
        per[repo] = per.get(repo, 0) + 1
    return {"keys": len(rows), "done": sum(1 for r in rows if r["state"] == DONE),
            "open": sum(1 for r in rows if r["state"] == OPEN), "chars": len(text),
            "tokens": counter(text) if counter else None, "keys_per_project": sorted(per.values(), reverse=True)}


def bank_states(meta: Iterable[Mapping[str, Any]], keys: Iterable[str], t: date,
                tag_aliases: Mapping[str, str]) -> Dict[str, Dict[str, Optional[str]]]:
    """키마다 t 시점 뱅크 상태(R_bank 분모용, o3 표와 같은 규칙).
    strict: t 이하 유효 완료 계열 기억이 있으면 done, 없고 언급이 있으면 open, 언급도 없으면 None(= o3 표의 상태).
    loose: strict 가 done 이어도 최신 완료 계열 기억보다 나중의 비완료 언급이 있으면 open."""
    want = set(keys)
    end = _t_end(t)
    last_done: Dict[str, datetime] = {}
    last_other: Dict[str, datetime] = {}
    for m in meta:
        if m.get("state") != "valid":
            continue
        at = _parse(m.get("mentioned_at"))
        if at is None or at > end:
            continue
        is_done = is_completion_family({"tags": list(m.get("tags") or [])}, tag_aliases)
        for k in m.get("keys") or []:
            if k not in want:
                continue
            box = last_done if is_done else last_other
            if k not in box or at > box[k]:
                box[k] = at
    out: Dict[str, Dict[str, Optional[str]]] = {}
    for k in want:
        if k in last_done:
            strict = "done"
            loose = "open" if k in last_other and last_other[k] > last_done[k] else "done"
        elif k in last_other:
            strict = loose = "open"
        else:
            strict = loose = None
        out[k] = {"strict": strict, "loose": loose}
    return out
