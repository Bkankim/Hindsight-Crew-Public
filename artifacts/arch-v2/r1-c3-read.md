# R1-c3 읽기 시점 설정 arm - reflect 지시문·미션·성향(O4)과 `recall_prefer_observations`(O9) (issue #301, 상위 #296)

**범위 (i) 위에서 잼, 운영자가 다른 범위를 고르면 O9 효과는 다시 확인 필요.** 시작 arm 은 #298 의 arm 백업(`default_observation_scopes=[["project:*"]]`, 재통합 1회)이다.
#299 (ii)가 상한 초과 전망으로 중단됐고 채택 O2 는 운영자 결정을 기다린다(코디네이터 결정, #296 댓글). 그래서 이 티켓을 먼저 했다.

- 비교 기준은 시작 arm 백업 그대로(설정 불변)다. 같은 사본에서 설정만 바꿔 가며 쟀다. **재통합은 하지 않았고, 관찰을 다시 만드는 문서 PATCH 도 보내지 않았다.**
  1단계 재통합 상한(4회, 누적 12시간)은 쓰지 않았다.
- 이 파일에는 항목 id·판정·해시·집계만 쓴다(정답표 항목 이름, 질의·응답 원문, 미션 문안, 개인 경로 없음). 원문은 리포 밖 `~/.local/share/hindsight-bench/arch-v2/`(0700)에만 있다.
- 벤치 판정 해석(스펙 #296 "벤치 판정"): 속도·비용 축만 본다. 상태 정답성은 회귀 세트로 판정한다. 결과가 바뀐 쿼리 비율은 보고만 한다. 문턱은 `r1-a-baseline.md` "확정 문턱"(속도 3.0%, 섞은 p95 6.0%, 토큰 허용 0)이다.

## 요약

- 조합 4개(base = 설정 불변, O4, O9, O4+O9)를 같은 사본에서 쟀다. 세 경로 모두 invalid 0 이다. **합격 조건(끝난 항목 open 0, 대조군 3/3)을 채운 조합·경로는 없다.**
- (a) reflect 나열은 1회 응답 흔들림이 커서 조합마다 3~6회 반복했다. 끝난 항목 open 합은 base 12/90, O4 2/45, **O9 1/90**, O4+O9 1/45 다.
  대조군 open 합은 base 13/18, O4 3/9, O9 10/18, O4+O9 4/9 다. O9 는 6 run 모두 대조군을 1 이상 잡으면서 끝난 항목 open 을 줄였다. 다만 대조군 3/3 인 run 이 base 4/6 -> O9 2/6 으로 줄고 OPEN 절도 짧아져(3절), O9 의 감소에도 목록 축소가 일부 섞였을 수 있다.
  O4 는 끝난 항목 open 과 함께 대조군 open 도 줄였다. 목록 자체를 덜 내는 쪽의 효과로 본다.
- (b) full(조합마다 새 접미사 `-c3-<조합>` 페이지): 끝난 항목 open 은 base 2, O4 1, O9 2, O4+O9 1 이고 대조군은 모두 3/3 이다.
  페이지는 `fact_types=["observation"]`이라 O9 가 작용하지 않는다. 따라서 base 와 O9 의 차이(항목이 바뀜)는 1회 생성 흔들림이다. O4 의 1 차이도 그 흔들림 범위 안이다.
- (c) recall: O4 는 영향이 없다(판정·토큰이 base 와 같다). **O9 는 끝난 항목 open 이 3 에서 4 가 됐다**(K08 해소, K04·K16 새로 open, 결정적: O9·O4+O9 가 같다).
  응답 토큰 합은 119,187 에서 115,447 로 -3.1% 다. 결과 행은 240 에서 230 행이고, 관찰 비중은 94 에서 135 행으로 늘었다.
- 지시문 적용 범위(실측): **reflect, 지식 페이지 생성, 멘탈 모델 full refresh 모두에 들어간다**(`based_on.directives` 1건, `llm-requests` 프롬프트에 지시문 이름이 있다).
  문서의 "reflect 한정"은 "reflect 루프를 쓰는 모든 작업"이라는 뜻이다. 통합(consolidation)·retain 프롬프트는 지시문을 읽지 않는다(upstream 코드).
- 벤치(O9, 동시 두 세트, 한 run): **PASS**. 속도 비 0.998, 섞은 p95 비 1.002, 토큰 -1.9%(284,523 -> 279,093).
- 채택 제안: **O4 불채택, O9 보류**(8절).

## 1. 사본과 복원원

- up 전(09:44 KST): `bash scripts/next-stack.sh status` exit 3, `docker ps -aq --filter label=hc-bench=1` 빈 출력.
- up(2026-10-30 09:44:46~09:45:18 KST):
  `HC_NEXT_BACKUP_SHA256=51386018de6b363d7b66fd0499692dd8307bee4fcaf670ee61ac7e08cc04fa8a HC_NEXT_BACKUP_DIR=~/.local/share/hindsight-bench/arm-backups/r1-b-20261030-070107 HC_NEXT_REQUIRED_BANK=personal-demo bash scripts/next-stack.sh up --llm opus` exit 0,
  마지막 줄 `up ok: commit=56c7f06aa9e01519bf90b871e703b64835e8abfd gateway=http://127.0.0.1:18888 ui=http://127.0.0.1:9998 llm=opus`.
  - 출력은 `tail -15`로만 받아서 `restore:` 줄이 잘렸다. sha256 대조는 다음 두 가지로 확인했다. `next-stack.sh`는 `HC_NEXT_BACKUP_SHA256`이 아카이브와 다르면 아무것도 복원하지 않고 멈춘다(`select_backup`, 153-154행). 그리고 up 직전 `shasum -a 256 <arm>/pg0.tar.gz` = `51386018de6b...`(LINEAGE `backup.pg0_sha256`과 같다)이다.
- 복원 직후 stats: documents 2,633, observations 3,373, 원문 2,635(world 1,916, experience 719), pending_consolidation 0, failed_operations 40. 백업 옆 `stats.json`(관찰 3,373, `last_memory_write_at` 2026-10-29T21:50:15Z)과 같다.
- 시작 설정: 뱅크 override `{"default_observation_scopes": [["project:*"]], "memory_defense": {"enabled": false, "rules": []}}`, 지시문 0개,
  `reflect_mission` null, `disposition_skepticism` null(서버 기본 3), `recall_prefer_observations` false.
- down: `bash scripts/next-stack.sh down` exit 0(11:00:30), 직후 `status` exit 3, `docker ps -aq --filter name=hc-next`·`docker volume ls -q --filter name=hc-next` 빈 출력(11:00:31).
- 실행 위치: 이 워크트리(origin/main 56c7f06)에 운영 체크아웃의 `bench/.data`·`audit`·`secrets` 심링크를 달았다. 끝나고 지웠다.

## 2. 설정 조합과 적용 명령

| 조합 | 뱅크 설정(시작 override 에 더한 것) | 지시문 | (b) 페이지 접미사 |
|---|---|---|---|
| base | 없음 | 0 | `-c3-base` |
| O4 | `reflect_mission`(sha256 `bbeaed40d998fcddb8ed1283b9fbecd1483f68987e0ee10159579f5087d3b973`, 375자), `disposition_skepticism=4` | 1 | `-c3-o4` |
| O9 | `recall_prefer_observations=true` | 0 | `-c3-o9` |
| O4+O9 | O4 + O9 | 1 | `-c3-o4o9` |

- 순서(UTC): base 채점 00:46:07 -> O4 적용 00:59:09 -> O4 되돌림 01:17:38 -> O9 적용 01:17:48 -> O4+O9 적용 01:28:34 -> O4 되돌림(O9 만) 01:43:51 -> arm 백업 01:52:16 -> base 로 되돌림 01:52:45.
- 지시문(스펙 #296 O4 출발 후보 그대로): 이름 `status-latest-record`, 본문 "상태(열림·완료·유효)를 단정하기 전에 그 대상의 최신 기록을 대상 이름으로 검색해 확인하고 근거 날짜를 밝힌다",
  저장된 본문 sha256 `347944c221f690c24f1890fed216911fc7550f21481543a1b160b4ab317d0aca`(62자), 태그 없음(전역), priority 0.
- `reflect_mission` 후보는 스펙에 문안이 없어 이 티켓에서 썼다. 요지: 항목 상태를 그 항목의 가장 늦은 기록으로 정하고(뒤의 완료가 앞의 인계·할 일을 닫는다), 근거 날짜를 밝힌다. 원문은 리포 밖 `r1-c3/reflect_mission.txt`와 완료 보고에만 있다.

### 뱅크 설정 (bank-config.py)

워크트리 gitignore 오버레이 `templates/bank-config/local/personal-demo.json` = 운영 오버레이 + 시작 arm 키 + 조합 키다. O9 예(사적 값 없음, 원문 그대로):

```json
{"version": "1", "bank": {"memory_defense": {"enabled": false, "rules": []}, "default_observation_scopes": [["project:*"]], "recall_prefer_observations": true}}
```

```bash
next_env=$(bash scripts/next-stack.sh env) || exit 1; eval "$next_env"
python3 -B scripts/bank-config.py diff --bank personal-demo    # O4: change disposition_skepticism: null -> 4, change reflect_mission ..., exit 3
python3 -B scripts/bank-config.py apply --bank personal-demo   # patched: disposition_skepticism, reflect_mission -> 다시 읽어 same, exit 0
```

- 실측 출력: O4 `patched: disposition_skepticism, reflect_mission`, O9 `patched: recall_prefer_observations`. 다시 읽은 diff 는 모두 `same`이다.

### 되돌림 (override 삭제)

`bank-config.py apply`는 템플릿에 있는 키만 보낸다. 오버레이에서 키를 빼도 override 는 남는다(스펙 #296 H). 그래서 **값을 `null`로 PATCH** 했다.
그러면 키가 override 에서 사라지고 서버 기본값으로 돌아간다(실측: `override_keys`가 `default_observation_scopes`, `memory_defense` 둘로 돌아왔다).
`DELETE .../config`(openapi 에 있다)는 override 전체를 지우므로(`memory_defense`·범위 포함) 쓰지 않았다.

```bash
# 사본 env 안에서, 워크트리 루트에서. o4-off.json = {"updates": {"reflect_mission": null, "disposition_skepticism": null}}
#                                          o9-off.json = {"updates": {"recall_prefer_observations": null}}
python3 -B cfgpatch.py personal-demo o4-off.json
```

`cfgpatch.py`(리포 밖 스크래치, 원문 그대로. `scripts/bank-config.py`의 대상 가드와 컨테이너 안 요청(키는 stdin)을 그대로 쓴다).
final-review(rework #301-1) 뒤 보강: 뱅크 id 가 `^[A-Za-z0-9_-]{1,128}$`가 아니면 보내기 전에 exit 1. PATCH 가 2xx 여도 다시 읽은 override 가 요청과 다르거나(null 은 키 없음, 값은 같음) 응답에 `overrides`·`config`가 없으면 "쓰기 여부 불명"으로 exit 2(다시 읽은 상태를 출력한다). 대상 가드·HTTP 오류(`bank-config.py`의 `Fail`)는 한 줄 메시지로 exit 1.
측정은 보강 전 판(대상 가드·PATCH·GET 은 같고 위 두 검사가 없었다)으로 했다. 보강 판은 가짜 `scripts/bank-config.py`로 거부·불명 경로(잘못된 뱅크 id, 빈 updates, 다시 읽기 불일치 exit 2, HTTP 오류 exit 1)만 확인했다(사본에서 다시 돌리지 않았다):

```python
"""PATCH bank config keys from a JSON file (`{"updates": {...}}`), via scripts/bank-config.py target guard; prints override keys after.

  python3 -B cfgpatch.py <bank> <updates.json> [--allow-prod]
Exit 0 = re-read matches the requested updates, 1 = refused or failed before/at the write,
2 = write sent but the re-read does not show the requested state (write result unknown).
A null value means "remove this override" (server default comes back).
"""
import importlib.util
import json
import os
import re
import sys

spec = importlib.util.spec_from_file_location("bank_config", os.path.join("scripts", "bank-config.py"))
bc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bc)

BANK = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def main():
    args = sys.argv[1:]
    allow_prod = bool(args) and args[-1] == "--allow-prod"
    if allow_prod:
        args = args[:-1]
    if len(args) != 2:
        sys.exit("cfgpatch: usage: cfgpatch.py <bank> <updates.json> [--allow-prod]")
    bank, path = args
    if not BANK.match(bank):
        sys.exit("cfgpatch: bank id %r is not ^[A-Za-z0-9_-]{1,128}$; nothing was sent" % bank)
    with open(path, encoding="utf-8") as fh:
        body = json.load(fh)
    updates = body.get("updates") if isinstance(body, dict) else None
    if not isinstance(updates, dict) or not updates:
        sys.exit("cfgpatch: %s must be {\"updates\": {<key>: <value or null>, ...}}; nothing was sent" % path)
    try:
        container, is_copy, key = bc.resolve_target()
        if not is_copy and not allow_prod:
            sys.exit("cfgpatch: not the hc-next copy (add --allow-prod for the window); nothing sent")
        bc.upstream(container, key, "PATCH", bc.config_path(bank), body)
        cur = bc.upstream(container, key, "GET", bc.config_path(bank))
    except bc.Fail as e:
        sys.exit("cfgpatch: %s" % e)
    overrides, config = cur.get("overrides"), cur.get("config")
    if not isinstance(overrides, dict) or not isinstance(config, dict):
        print("cfgpatch: re-read has no overrides/config - write result unknown", file=sys.stderr)
        sys.exit(2)
    # null = override removed; any other value = override present and equal
    bad = []
    for k, v in updates.items():
        if v is None:
            if k in overrides:
                bad.append(k)
        elif k not in overrides or bc.canon(overrides[k]) != bc.canon(v):
            bad.append(k)
    print(json.dumps({"override_keys": sorted(overrides), "resolved": {k: ("set" if k == "reflect_mission" and config.get(k) else config.get(k)) for k in sorted(updates)}}, ensure_ascii=False))
    if bad:
        print("cfgpatch: PATCH answered 2xx but the re-read differs for: %s - write result unknown" % ", ".join(bad), file=sys.stderr)
        sys.exit(2)


main()
```

출력 예(O4 되돌림, 01:17:38Z): `{"override_keys": ["default_observation_scopes", "memory_defense"], "resolved": {"reflect_mission": null, "disposition_skepticism": null, "recall_prefer_observations": false}}`.

### 지시문 생성·삭제 (upstream 지시문 API, 컨테이너 안 python, 키는 stdin)

upstream v1.1.0 openapi: `GET|POST /v1/default/banks/{bank_id}/directives`, `GET|PATCH|DELETE /v1/default/banks/{bank_id}/directives/{directive_id}`.
본문 필드는 `name`, `content`, `priority`(기본 0), `is_active`(기본 true), `tags`(빈 목록 = 전역)다. `bank-config.py`는 지시문을 다루지 않는다.

```bash
next_env=$(bash scripts/next-stack.sh env) || exit 1; eval "$next_env"
python3 -B directive.py create personal-demo status-latest-record <본문 파일>   # 본문은 파일에서 읽는다(명령줄에 쓰지 않는다)
python3 -B directive.py list personal-demo
python3 -B directive.py delete personal-demo <directive id>
# 운영(창)에서는 마지막 인자로 --allow-prod 를 준다. 없으면 사본이 아닌 대상에 쓰지 않는다
```

`directive.py`(리포 밖 스크래치, 원문 그대로. 워크트리 루트에서 실행).
final-review(rework #301-1) 뒤 보강: 뱅크 id·지시문 id 가 `^[A-Za-z0-9_-]{1,128}$`가 아니거나 인자 수·본문 파일이 맞지 않으면 보내기 전에 exit 1. POST 가 2xx 여도 본문에 `id`·`name`이 없거나, DELETE 뒤 목록에 그 id 가 남아 있으면 "쓰기 여부 불명"으로 exit 2 이고, 이어서 GET 으로 읽은 지시문 목록을 출력한다. `Fail`은 한 줄 메시지로 exit 1.
측정은 보강 전 판(같은 POST·DELETE·GET, 위 검사 없음)으로 했다. 보강 판은 가짜 `scripts/bank-config.py`로 거부·불명 경로만 확인했다(사본에서 다시 돌리지 않았다). #304 창에서는 exit 2 면 출력된 목록으로 상태를 확인한 뒤 다음 단계로 간다:

```python
"""Directive helper (issue #301). Reuses scripts/bank-config.py target guard and in-container request (key on stdin).

  python3 -B directive.py list   <bank>
  python3 -B directive.py create <bank> <name> <content-file>     (content read from a file, never the command line)
  python3 -B directive.py delete <bank> <directive-id>
Write commands need HC_TARGET=next unless --allow-prod is the last argument.
Exit 0 = done, 1 = refused or failed before/at the write, 2 = write sent but its result is unknown (read state printed).
"""
import hashlib
import importlib.util
import json
import os
import re
import sys

spec = importlib.util.spec_from_file_location("bank_config", os.path.join("scripts", "bank-config.py"))
bc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bc)

BANK = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def show(d):
    c = d.get("content") or ""
    return {"id": d["id"], "name": d["name"], "is_active": d.get("is_active"), "tags": d.get("tags"),
            "priority": d.get("priority"), "sha256": hashlib.sha256(c.encode()).hexdigest(), "len": len(c)}


def listing(container, key, base):
    r = bc.upstream(container, key, "GET", base)
    return {"total": r.get("total"), "items": [show(d) for d in r.get("items", [])]}


def unknown(container, key, base, what):
    # 2xx without the expected body: the write may or may not have happened. Read the state back and stop non-zero.
    print("directive: %s answered 2xx without the expected body - write result unknown; current state:" % what,
          file=sys.stderr)
    print(json.dumps(listing(container, key, base), ensure_ascii=False))
    sys.exit(2)


def main():
    args = sys.argv[1:]
    allow_prod = bool(args) and args[-1] == "--allow-prod"
    if allow_prod:
        args = args[:-1]
    need = {"list": 2, "create": 4, "delete": 3}
    if not args or args[0] not in need or len(args) != need[args[0]]:
        sys.exit("directive: usage: list <bank> | create <bank> <name> <content-file> | delete <bank> <id> [--allow-prod]")
    cmd, bank = args[0], args[1]
    if not BANK.match(bank):
        sys.exit("directive: bank id %r is not ^[A-Za-z0-9_-]{1,128}$; nothing was sent" % bank)
    if cmd == "delete" and not ID.match(args[2]):
        sys.exit("directive: directive id %r is not ^[A-Za-z0-9_-]{1,128}$; nothing was sent" % args[2])
    content = None
    if cmd == "create":
        with open(args[3], encoding="utf-8") as fh:
            content = fh.read().strip()
        if not content:
            sys.exit("directive: content file is empty; nothing was sent")
    try:
        container, is_copy, key = bc.resolve_target()
        if cmd != "list" and not is_copy and not allow_prod:
            sys.exit("directive: %s on a target that is not the hc-next copy needs --allow-prod; nothing was sent" % cmd)
        base = "/v1/default/banks/%s/directives" % bank
        if cmd == "list":
            print(json.dumps(listing(container, key, base), ensure_ascii=False))
        elif cmd == "create":
            r = bc.upstream(container, key, "POST", base, {"name": args[2], "content": content})
            if not isinstance(r, dict) or not r.get("id") or "name" not in r:
                unknown(container, key, base, "POST")
            print(json.dumps(show(r), ensure_ascii=False))
        else:
            bc.upstream(container, key, "DELETE", "%s/%s" % (base, args[2]))
            state = listing(container, key, base)
            if any(d["id"] == args[2] for d in state["items"]):
                print("directive: DELETE answered 2xx but %s is still listed" % args[2], file=sys.stderr)
                print(json.dumps(state, ensure_ascii=False))
                sys.exit(2)
            print(json.dumps({"deleted": args[2], "remaining": state["total"]}))
    except bc.Fail as e:
        sys.exit("directive: %s" % e)


main()
```

실측 출력:
- create(00:59:09Z): `{"id": "92984039-115d-436e-8385-f07e7ca8878b", "name": "status-latest-record", "is_active": true, "tags": [], "priority": 0, "sha256": "347944c2...", "len": 62}`
- delete(01:17:38Z): `{"deleted": "92984039-...", "remaining": 0}`. O4+O9 에서 다시 만들고(`c0b8d41c-...`) 01:43:51Z 에 지웠다(`remaining: 0`).

## 3. 회귀 세 경로 (#297 고정 세트 18건)

- 고정 세트: `~/.local/share/hindsight-bench/arch-v2/r1-fixed-set.json`(evidence 는 다시 만들지 않았다). 명령은 `bench/README.md` 그대로다.
  - 조합 채점: `python3 -B -m bench.arch_v2_regression score --answer-key "$KEY" --evidence <고정 세트> --page-suffix -c3-<조합>`
  - (a) 반복: `... score --answer-key "$KEY" --evidence <고정 세트> --paths a`
- **(b) 페이지 방식: 조합마다 새 접미사**(`-c3-base`, `-c3-o4`, `-c3-o9`, `-c3-o4o9`)로 첫 생성(full)했다. 시작 백업의 `-pre`·접미사 없는 페이지는 건드리지 않았다.
  재통합이 없으므로 delta 결과는 없다(시작 arm 의 delta 값은 `r1-b-scope.md` 5절). `deleted_evidence` 합은 네 조합 모두 0 이다.
- 리포 밖 출력(`score-<시각>/`): base `20261030-094607`, O4 `20261030-095910`, O9 `20261030-101748`, O4+O9 `20261030-102834`.
  (a) 반복: base `095403`·`095712`·`105246`·`105448`·`105736`, O4 `100751`·`101037`, O9 `102427`·`102619`·`104352`·`104640`·`104912`, O4+O9 `103655`·`104008`.

### 경로별 판정 (조합 채점 1회)

| 경로 | base | O4 | O9 | O4+O9 | 시작 arm(#298) |
|---|---|---|---|---|---|
| (a) 끝난 항목 open / 대조군 open | 3/15 (K01, K09, K12) / 3/3 | 0/15 / 0/3 | 0/15 / 1/3 | 0/15 / 0/3 | 2/15 / 3/3 |
| (b) full 끝난 항목 open / 대조군 open | 2/15 (K01, K11) / 3/3 | 1/15 (K01) / 3/3 | 2/15 (K02, K15) / 3/3 | 1/15 (K09) / 3/3 | 1/15 / 3/3 |
| (c) 끝난 항목 open / 대조군 open | 3/15 (K05, K08, K09) / 2/3 | 3/15 (같음) / 2/3 | 4/15 (K04, K05, K09, K16) / 2/3 | 4/15 (O9 와 같음) / 2/3 | 3/15 / 2/3 |
| invalid | 0 | 0 | 0 | 0 | 0 |
| 판정 | 세 경로 불합격 | 세 경로 불합격 | 세 경로 불합격 | 세 경로 불합격 | 세 경로 불합격 |

| id | (b) base | (b) O4 | (b) O9 | (b) O4+O9 | (c) base | (c) O4 | (c) O9 | (c) O4+O9 |
|---|---|---|---|---|---|---|---|---|
| C01 | open | open | open | open | not-open | not-open | not-open | not-open |
| C02 | open | open | open | open | open | open | open | open |
| C03 | open | open | open | open | open | open | open | open |
| K01 | open | open | not-open | not-open | not-open | not-open | not-open | not-open |
| K02 | not-open | not-open | open | not-open | not-open | not-open | not-open | not-open |
| K03 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K04 | not-open | not-open | not-open | not-open | not-open | not-open | open | open |
| K05 | not-open | not-open | not-open | not-open | open | open | open | open |
| K06 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K07 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K08 | not-open | not-open | not-open | not-open | open | open | not-open | not-open |
| K09 | not-open | not-open | not-open | open | open | open | open | open |
| K10 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K11 | open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K12 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K14 | not-open | not-open | not-open | not-open | not-open | not-open | not-open | not-open |
| K15 | not-open | not-open | open | not-open | not-open | not-open | not-open | not-open |
| K16 | not-open | not-open | not-open | not-open | not-open | not-open | open | open |

- base 의 (c)는 시작 arm(#298 `score-20261030-065427`)과 판정·응답 토큰이 같다. (c)는 같은 사본·같은 설정에서 결정적이다.
  base 의 (a)·(b)는 #298 과 다르다(1회 응답 흔들림).
- (c) O9 사유: K04·K16 은 base `latest-done` -> O9 `latest-open`, K08 은 base `latest-open` -> O9 `latest-done`.
  원문 사실이 같은 사실에서 만든 관찰로 바뀌면서, 가장 늦은 상태 기억이 바뀐 결과다(원문은 리포 밖 detail.json).

### (a) 반복 (reflect 1회 응답의 흔들림)

| 조합 | run 수 | 끝난 항목 open 합 | 대조군 open 합 | run 별 (끝난, 대조군) | OPEN 절 줄 수 | reflect LLM 호출 |
|---|---|---|---|---|---|---|
| base | 6 | **12/90** | 13/18 | (3,3) (0,0) (3,3) (3,3) (3,3) (0,1) | 105, 107, 107, 100, 90, 72 | 5, 14, 8, 5, 6, 9 |
| O4 | 3 | 2/45 | 3/9 | (0,0) (2,3) (0,0) | 84, 140, 47 | 15, 17, 9 |
| O9 | 6 | **1/90** | 10/18 | (0,1) (0,3) (1,3) (0,1) (0,1) (0,1) | 95, 94, 93, 56, 57, 77 | 5, 8, 10, 6, 6, 7 |
| O4+O9 | 3 | 1/45 | 4/9 | (0,0) (0,1) (1,3) | 65, 62, 93 | 12, 12, 25 |

- 읽는 법: 대조군 open 이 0 인 run 은 그 응답이 대조군까지 놓친 run 이다. 그런 run 의 "끝난 항목 open 0"은 개선이 아니라 감도 상실이다.
  base 는 대조군을 잡은 run 5/6 중 4 run 에서 끝난 항목 open 이 3 이었다(나머지 1 run 은 (0,1)). O9 는 6 run 모두 대조군을 1 이상 잡았고, 끝난 항목 open 은 1 run 의 1건뿐이다.
- O4 는 3 run 중 2 run 이 대조군 0 이다. 끝난 항목 open 감소가 상태 판단 개선인지 목록 축소인지 가를 수 없다.
- 소요: O4 가 든 조합의 reflect 는 더 오래 걸리고 LLM 호출이 늘었다(base 중앙값 7회·145초, O4 15회·166초, O4+O9 12회·184초, O9 6.5회·143초).
- O9 도 감도가 일부 줄었다: 대조군 3/3 인 run 이 base 4/6 -> O9 2/6 이고, OPEN 절 줄 수 중앙값이 base 102.5줄(72~107) 대 O9 85줄(56~95)이고, O9 의 대조군 1/3 run 4개는 중앙값 67줄(56~95)이다(범위는 겹친다). 그래서 O9 의 끝난 항목 open 감소 일부도 목록 축소일 수 있다. O4 와 다른 점은 6 run 모두 대조군을 1 이상 잡았다는 것이다.
- 표본 한계: 조합당 3~6 run 이다. O9 의 (a) 개선은 run 수준에서 일관되지만(대조군을 잡은 run 기준 base 4/5, O9 1/6 이 끝난 항목 open 1 이상) 통계 검정은 하지 않았다.

### 비용 (조합 채점 1회, `llm-requests` total)

| 경로 | base | O4 | O9 | O4+O9 |
|---|---|---|---|---|
| (a) 1회 | 5건, 126.8초 | 15건, 176.0초 | 5건, 111.1초 | 12건, 175.1초 |
| (b) 7 페이지 | 115건, 237.6초 | 176건, 264.9초 | 138건, 208.4초 | 183건, 245.3초 |
| (c) 18 질의 | 35건, 87.0초 | 35건, 80.5초 | 35건, 79.4초 | 35건, 79.5초 |

(c)의 35건은 리랭크·임베딩 추적 행이다(생성형 0, `r1-a-baseline.md` 6절). O4 는 (b) 페이지 생성 LLM 호출을 약 1.5배로 늘렸다.

## 4. 지시문 적용 범위 실측

방법: 지시문이 있는 구간의 `GET .../llm-requests`(start_date·end_date, 페이지 전부)에서 각 행의 `input`(실제 프롬프트)에 지시문 이름·본문, 미션 앞 80자가 들어 있는지 셌다.
`input`이 길면 upstream 이 잘라서 저장한다(`truncat` 표시). 잘린 행에서는 본문이 빠질 수 있어서, 판정은 이름 기준으로 한다. 대조 구간(base 채점, 지시문 없음)에서는 157행 모두 0 이었다.

| 작업 | 구간 (UTC) | 생성형 행 | 지시문 이름 있음 | 미션 있음 | `based_on.directives` |
|---|---|---|---|---|---|
| reflect((a) 3회) | 00:59:10~01:13:20 안 | reflect 8 + reflect_tool_call 17 | 6/8 + 17/17 | 6/8 + 17/17 | 3회 모두 1 |
| 지식 페이지 생성((b) 7개, full) | 같은 구간 | refresh_mental_model reflect 11 + reflect_tool_call 55 | 11/11 + 55/55 | 11/11 + 55/55 | 확인한 3 페이지(전체 1, 프로젝트 2) 모두 1 |
| 멘탈 모델 full refresh(8개 중 1개, mode full, project 태그 하나, `POST .../mental-models/<id>/refresh`) | 01:14:19~01:17:22 | reflect 2 + reflect_tool_call 6 | 1/2 + 6/6 | 1/2 + 6/6 | 0(전) -> 1(후) |
| 임베딩·리랭크 행 | 같은 구간 | (생성형 아님) | 0 | 0 | - |

- 결론: 지시문은 reflect 루프를 쓰는 세 작업 모두에 들어간다. 지식 페이지에는 프로젝트 태그(`project:<hint>`)가 있지만, 태그 없는 지시문은 전역이라 함께 적용된다(upstream `api/reflect.mdx` "untagged directives are global").
- 지시문이 없는 reflect 행 3건(reflect 2, 멘탈 모델 1)은 `reflect` scope 의 한 단계다. 미션도 같은 행에서 빠졌다. 최종 합성 단계 프롬프트로 추정한다(미검증).
  - 주의: 이 해석은 가설이다. 개수가 잘 맞지 않는다(O4 (a) 3 run 에 빠진 행 2, 페이지 생성 reflect 11행에는 0). 확인 방법: upstream `engine/reflect/agent.py`에서 지시문 없이 보내는 최종 프롬프트의 조건(반복 상한 도달 등)을 읽고, 빠진 3행이 잘린(`truncat`) 행인지 그 run 의 마지막 행인지 `llm-requests`의 `trace_id`·`started_at`으로 대조한다. 그 전까지 #304 창에서 지시문 없는 행이 나오면 정상으로 단정하지 말고 이 확인을 한다.
- delta 갱신: 재통합이 없어 실측하지 않았다. upstream 코드(`engine/memory_engine.py` `_DeltaGate`)는 delta 게이트가 남긴 근거에 "뱅크 지시문이 있으면 함께" 담는다. 지시문이 delta 판단에 들어가는지는 1단계 창의 첫 delta 갱신에서 같은 방법(llm-requests 이름 검사)으로 확인할 수 있다.
- 통합·retain: `engine/consolidation/prompts.py`·`engine/retain/fact_extraction.py`의 "directive"는 출력 언어 지시(`output_language_directive`)뿐이고, 뱅크 지시문을 읽지 않는다(코드 확인. 이 사본에서 통합은 돌지 않았다).
- 부수: `reflect_mission`도 같은 세 작업에 들어간다(위 표). upstream 문서 "Disposition traits and `reflect_mission` only affect the `reflect` operation"의 reflect 도 같은 뜻이다.
- 영향: 운영에서 지시문·미션을 넣으면 멘탈 모델 8개의 다음 refresh 와 지식 페이지 갱신에도 들어간다. 이 티켓의 측정 범위(고정 세트 회귀)는 멘탈 모델 내용 변화를 채점하지 않는다.

## 5. O9 켬·끔의 (c) 판정과 응답 토큰

- 토큰: (c) 18 질의 결과 행(`detail.json`의 `results`를 `json.dumps(ensure_ascii=False)`)을 `tiktoken` `o200k_base`로 센 합이다(runner 와 같은 인코딩. 게이트웨이 응답 본문 그대로가 아니라 결과 행 JSON 이다).

| | O9 끔 (base) | O9 켬 |
|---|---|---|
| (c) 끝난 항목 open / 대조군 open | 3/15 / 2/3 | 4/15 / 2/3 |
| 토큰 합(18 질의) | 119,187 | 115,447 (-3.1%) |
| 결과 행 | 240 | 230 |
| 행 종류 observation / world / experience | 94 / 63 / 83 | 135 / 45 / 50 |
| 질의별 토큰 | - | 줄어든 질의 15, 늘어난 질의 3(최대 +702, 최소 -755) |

- O4 는 (c)에 영향이 없다(O4 와 base, O4+O9 와 O9 가 판정·질의별 토큰까지 같다). recall 은 지시문·미션·성향을 읽지 않는다.
- 해석: O9 는 원문 사실을 그 사실에서 만든 관찰로 바꾼다. 범위 (i)에서는 관찰이 project 범위로 합쳐져 있다. 그래서 같은 항목의 상태 기억 중 "가장 늦은 것"이 바뀌어 한 건씩 좋아지고 나빠졌다(순 +1).
  (a)에서는 reflect 의 recall 도구가 이 뱅크 설정을 따르므로 같은 바뀜이 일어나고, 반복 결과는 좋아졌다(3절).
- 2026-09-22 실측(`gateway/recall_prune.py` 머리 주석, 이전 upstream)의 "prefer_observations +82%"와 반대 방향이다. v1.1.0 + 범위 (i)에서는 토큰이 줄었다.

## 6. 기타 관찰

- 지식 페이지·멘탈 모델의 `trigger.fact_types`: arch-v2 페이지는 `["observation"]`이라 O9 가 작용하지 않는다. 멘탈 모델 8개는 `null`(전 종류)이라 refresh 때 O9 가 작용할 수 있다(이 티켓은 재지 않았다).
- 테스트를 위해 사본의 멘탈 모델 1개(4절)를 지시문이 있는 상태로 full refresh 했다. 그래서 arm 백업 `r1-c3-o9`의 그 모델 내용은 시작 백업과 다르다(벤치 recall 은 멘탈 모델을 읽지 않는다).

## 7. 벤치 (O9, 동시 두 세트, 한 run)

- 방식: `bench/README.md` "1단계 arm 벤치" 절 명령 그대로다. 공유 벤치 사이드카 `r1`(18781), **s1 = 시작 arm 백업**(`r1-b-20261030-070107`, `--expected-overrides <그 LINEAGE>`), **s2 = O9 arm 백업**(`r1-c3-o9-20261030-105216`, `--expected-overrides <그 LINEAGE>`).
  팔 base(s1, 18891)·cand(s2, 18892) 모두 `gw-up --ref 270242e --local-policies <운영 체크아웃>/policies/local`(#296 코디네이터 결정 ref).
  run: `bench.runner --arm base=18891 --arm cand=18892 --split dev --limit 40 --seed 13 --rounds 3 --run-id r1c3-o9-1 --clock-from-set s1`,
  판정: `bench.arm_compare ... --expect-sha base=51386018... --expect-sha cand=6b90d398... --verify base=<verify-s1> --verify cand=<verify-s2> --speed-threshold 0.03 --p95-threshold 0.06`.
- 세트: s1 `backup_sha256` = `51386018de6b...`, s2 = `6b90d398d363...`(각 LINEAGE `backup.pg0_sha256`과 같다). 이미지 둘 다 `sha256:50311d02...`(v1.1.0).
  세트 시계 s1·s2 모두 `2026-10-29T21:51:00Z`(`clocks_differ: false`, 설정 PATCH·페이지 생성은 `last_memory_write_at`을 바꾸지 않았다).
- 팔: commit `270242e288ac20e48644ccf28bc03c79fb426996`, 두 팔 이미지 같음(`sha256:575ac07347f3...`, 이번 로컬 빌드). 정책 sha256 `bank-policies.json` `0587ecbb315e...`, `context-triggers.json` `8ed1ebaa4e27...`(#297·#298 과 같다).
- verify(run 직전): s1·s2 모두 `pass: true`, `bank_overrides_equal: true`, `bank_config_diff_unexplained: []`.

| run | 시작~끝 (KST) | 속도 기하평균 비 [95% CI] | 섞은 p95 비 [95% CI] | 토큰 base / cand | 결과 변경 | 오염 쌍 | arm_compare |
|---|---|---|---|---|---|---|---|
| r1c3-o9-1 | 11:01:29~11:25:27 | 0.9982 [0.9918, 1.0022] | 1.0018 [0.9873, 1.0042] | 284,523 / 279,093 | 39/40 | 4/120 | PASS (valid) |

- 속도: 중앙값 4,406.8ms -> 4,422.2ms, 문턱 3.0% 안(A/A 흔들림 0.983~1.007 안). 섞은 p95 문턱 6.0% 안.
- 비용: 토큰 -1.9%(쿼리 27개 감소, 12개 증가, 쿼리별 증가 최대 +536, p95 +398, 최소 -717). 비용 축 ok(cand <= base).
- 결과 변경 39/40 은 예상된 값이다(원문 행을 관찰로 바꾸는 설정, 스펙 #296 - 보고만 한다).
- base 토큰 284,523 은 #298 run 의 cand(같은 r1-b 백업) 284,530 과 7 토큰 다르다. #298 은 D10 시계(2026-10-26T19:30:00Z)를, 이 run 은 r1-b 시계(2026-10-29T21:51:00Z)를 썼다. 질의 시각이 달라서 생긴 차이로 본다(미검증).
- p99 는 내지 않았다(참고용).
- O4 는 벤치하지 않았다. 벤치는 recall 만 재고, recall 은 O4 설정을 읽지 않는다(5절: O4 켬·끔의 (c) 질의별 토큰이 같다).

## 8. 채택 O4·O9 와 근거

- **O4(지시문 + `reflect_mission` + `disposition_skepticism=4`): 불채택 제안.**
  - (a): 끝난 항목 open 은 줄었지만(base 12/90 -> 2/45) 대조군 open 도 13/18 -> 3/9 로 줄었다. 3 run 중 2 run 이 대조군을 하나도 잡지 못했다. 상태 판단 개선이 아니라 목록 축소로 본다.
  - (b): 1/15 대 base 2/15 는 1회 생성 흔들림 범위 안이다. 같은 r1-b 관찰 상태에서 설정 불변으로 만든 full 페이지가 #298 full 1/15, 이 티켓 base 2/15 로 이미 O4 의 1 을 포함한다.
  - (c): 영향 없음.
  - 비용: LLM 호출이 reflect 약 2~3배(반복 중앙값 7 -> 15회, 조합 채점 1회 5 -> 15회), 페이지 생성 약 1.5배(115 -> 176건)로 늘었다(3절). 그리고 지시문·미션이 멘탈 모델 8개 refresh 에도 들어간다(4절). 즉 이득 없이 영향 범위만 넓다.
  - 다시 볼 조건: 지시문 문안을 "OPEN 목록에서 빼라"는 결과 형식 쪽으로 바꾸는 안. 이 티켓은 스펙 출발 후보만 쟀다.
- **O9(`recall_prefer_observations=true`): 보류 제안**(범위 (i) 기준. 운영자가 다른 범위를 고르면 다시 잰다).
  - 득: (a) reflect 나열의 끝난 항목 open 이 12/90 -> 1/90 으로 줄었다. 대조군 감도는 13/18 -> 10/18 이고 대조군 3/3 run 은 4/6 -> 2/6 이다(목록 축소가 일부 섞였을 수 있다. O4 보다 감도 손실은 작다). (c) 응답 토큰 -3.1%. 벤치 PASS(속도·p95 차이 없음, dev 40 질의 토큰 -1.9%).
  - 실: (c) recall 상태가 3/15 -> 4/15 로 한 건 나빠졌다(결정적). (c)는 MCP recall 을 쓰는 모든 클라이언트의 기본 경로다(MCP recall 은 `prefer_observations`를 생략하면 이 뱅크 설정을 따른다, #285).
  - 그래서 세 경로 합격이라는 목표에는 어느 쪽도 닿지 않는다. 채택 여부는 "reflect 나열 개선 + 토큰 감소"와 "recall 상태 1건 악화"의 교환이다.
    운영자 결정이 필요하다. 다만 채택 O2 가 바뀌면 관찰 모양이 달라지므로, O2 확정 뒤 그 arm 에서 (a)(c)를 다시 재고 정하는 것을 권한다.

## 9. arm 백업 (O9)

- `next_env=$(bash scripts/next-stack.sh env) || exit 1; eval "$next_env"; bash scripts/backup.sh` -> `backup ok: backups/20261030-105216/pg0.tar.gz (186701132 bytes)`(10:52:16~10:52:24 KST, O9 만 켠 상태. O4 설정·지시문은 먼저 되돌렸다).
  워크트리 `backups/`에 쓰였다. 운영 체크아웃 `backups/`는 그대로다(전후 `ls` 비교). 곧바로 리포 밖으로 옮기고 워크트리 `backups/`를 지웠다.
- 위치: `~/.local/share/hindsight-bench/arm-backups/r1-c3-o9-20261030-105216/`(0700). `pg0.tar.gz` sha256 **`6b90d398d363e7648bcce6307766d8d9387a86bb271ffae48a22e094a112e886`**, `LINEAGE`(0600, schema_version 1, arm `r1-c3-o9`).
- LINEAGE 요지: source_backup = 시작 arm 백업(sha256 `51386018...`, 그 원천 D10 `8a4900ea...`), settings = 시작 override + `recall_prefer_observations: true`, directives [], document_patches 0, reconsolidation null(재통합 없음),
  pages `-pre`/``(시작 arm) + `-c3-base`·`-c3-o4`·`-c3-o9`·`-c3-o4o9`, expected_bank_overrides =
  `{"default_observation_scopes": [["project:*"]], "memory_defense": {"enabled": false, "rules": []}, "recall_prefer_observations": true}`(백업 직전 `GET .../config` `overrides`).

## 10. 부하 구간 (canary-exclude 형식)

```
2026-10-30 09:44:46 2026-10-30 11:05:31  # #301 R1-c3 hc-next r1-b arm 백업 사본(up 09:44:46 ~ down 확인 11:00:31 +300s): 조합 4개 score·(a) 반복·지시문·멘탈 모델 refresh·arm 백업(Opus)
2026-10-30 11:00:45 2026-10-30 11:30:39  # #301 R1-c3 벤치 복제본 s1 r1-b arm 백업 / s2 r1-c3-o9 arm 백업 + 벤치 사이드카(up 11:00:45 ~ down 확인 11:25:39 +300s): run r1c3-o9-1
```

정리 확인(11:25:39): `docker ps -aq --filter label=hc-bench=1`·`docker volume ls -q --filter label=hc-bench=1`·`docker network ls -q --filter name=hc-bench`·`lsof -nP -iTCP:18780-18799 -sTCP:LISTEN` 모두 빈 출력, `next-stack.sh status` exit 3.
워크트리 심링크(`bench/.data`·`audit`·`secrets`)는 지웠다.

## 11. 범위 밖 발견

- `bench/arch_v2_regression.py`의 (a) 채점은 OPEN 절 안에서 keyword 가 있는지만 본다. 그래서 대조군을 하나도 잡지 못한 응답도 "끝난 항목 open 0"으로 좋아 보인다.
  arm 비교에 run 별 대조군 open 수를 같이 보지 않으면 O4 같은 "목록 축소"를 개선으로 읽게 된다. 이 보고서는 run 별 (끝난, 대조군) 쌍으로 냈다. #304 보고서에서 (a) 판정 규칙(대조군 0 인 run 은 판정 불가)을 정할지 검토할 일이다.
