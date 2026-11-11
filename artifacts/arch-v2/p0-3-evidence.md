# P0-3 evidence: D10 사본 안 완료 근거 (issue #287, 상위 #284)

정답표(`bench/.data/regression-arch-v2.json`, 운영 체크아웃 gitignore)의 완료 16건마다 keyword 로 recall 해 뱅크 안 완료 근거를 찾았다.
이 파일에는 항목 id·근거 유무·날짜만 쓴다(항목 이름·keyword·질의·응답 원문은 리포 밖 출력에만 있다).

## 대상과 복원원

- 사본: hc-next, `bash scripts/next-stack.sh up --llm opus`(운영판 창 문서의 게이트웨이 플래그 표 "없음" - 추가 설정 없음. 창 문서는 운영판 전용이라 공개판에 없다)
- up 전: `bash scripts/next-stack.sh status` exit 3, `docker ps -aq --filter label=hc-bench=1` 빈 출력, 워크트리 HEAD 에 #285 머지 커밋(7f7a6fa) 포함
- up 출력(stdout, 홈 경로는 `<운영 체크아웃>`으로 바꿨다):

```
restore: backup=<운영 체크아웃>/backups/keep-arch-v2-20261027-043001/pg0.tar.gz sha256=8a4900ea913daf046b7fd3192d658d934fcb0a9248f02548ced2161e878613f4
pg_verifybackup ok
up ok: commit=b633c946a0c5d17304a70eb604b00746069216d6 gateway=http://127.0.0.1:18888 ui=http://127.0.0.1:9998 llm=opus
```

- 이미지: `docker inspect -f '{{.Config.Image}}' hc-next-hindsight-1` = `ghcr.io/vectorize-io/hindsight@sha256:50311d02b4d5d2b7c5242bd336edf8735df8374eaad496a92f49c83b37d2aa06`
  (`scripts/next-stack.sh` 40행 `NEXT_DEFAULT_IMAGE`와 같다. 측정 전후 두 번 확인)
- 명령: `next_env=$(bash scripts/next-stack.sh env) || exit 1; eval "$next_env"; python3 -B -m bench.arch_v2_regression evidence --answer-key <정답표>`
  (stderr `target: gw=127.0.0.1:18888 project=hc-next container=hc-next-hindsight-1 pg0=-`)
- 근거 규칙: keyword(정답표 전부, 영숫자 경계, 맨 번호는 프로젝트 이름 동반)가 들어 있고, 이 항목에 대해 완료를 말하고(`item_state` = done:
  keyword 문장의 완료 단서, 없으면 `kind:completion`·라벨), mentioned_at < `ignore_mentioned_at_from`(2026-10-28T15:00:00Z)인 기억.
  recall 시각 인자 `query_timestamp=2026-10-28T14:59:59Z` 고정. 질의 37회(keyword 수 합).
- 날짜 없는 기억: D10 recall 행의 일부는 `mentioned_at`이 null 이다(K04 keyword recall 약 50행 중 21행 실측). 사본 정책(D10 에는 2026-10-27 04:30 KST 이후 기억이 없다)으로
  근거에 넣었다. K05 는 날짜 없는 완료 기억 3건만 근거다(표의 날짜 `-`). 운영 대상에서는 같은 기억이 `undated`로 빠진다.
- 근거 종류: `kind:completion` = 태그, `text` = 태그 없이 본문 단서·라벨로 완료.
- 리포 밖 출력(0700): `~/.local/share/hindsight-bench/arch-v2/evidence-20261029-205831/`(항목별 `<id>.json` = 근거 memory id·mentioned_at·한 줄 요지, `summary.json`)

## 항목별 근거

| id | 뱅크 안 완료 근거 | mentioned_at (UTC 날짜) | 근거 종류 |
|---|---|---|---|
| K01 | 근거 있음 | 2026-10-17 | kind:completion |
| K02 | 근거 있음 | 2026-10-19 | text |
| K03 | 근거 있음 | 2026-09-27 | kind:completion |
| K04 | 근거 있음 | 2026-10-20 | text |
| K05 | 근거 있음 | - | kind:completion |
| K06 | 근거 있음 | 2026-09-26 | kind:completion |
| K07 | 근거 있음 | 2026-10-20 | text |
| K08 | 근거 있음 | 2026-09-30 | kind:completion |
| K09 | 근거 있음 | 2026-10-06 | kind:completion |
| K10 | 근거 있음 | 2026-09-17 | kind:completion |
| K11 | 근거 있음 | 2026-09-30 | kind:completion |
| K12 | 근거 있음 | 2026-09-30 | kind:completion |
| K13 | 없음 -> 세트에서 뺌 | - | - |
| K14 | 근거 있음 | 2026-10-16 | kind:completion |
| K15 | 근거 있음 | 2026-10-20 | kind:completion |
| K16 | 근거 있음 | 2026-10-16 | kind:completion |
| C01 | 대조군(expect open, 근거 확인 대상 아님) | - | - |
| C02 | 대조군(expect open, 근거 확인 대상 아님) | - | - |
| C03 | 대조군(expect open, 근거 확인 대상 아님) | - | - |

남은 세트: 완료 15건 + 대조군 3건 = 18건 (정답표 완료 16건 중 1건 뺌)

- 기준 시각 이후 기억: 0건(전 항목 `post_cutoff` 0). D10 스냅숏(2026-10-27 04:30 KST)이 기준 시각보다 앞서므로 예상대로다.
- K13 은 keyword 가 영숫자 경계로 맞는 기억이 D10 recall 결과에 없다. 규칙 v1(부분 문자열)에서는 다른 번호(`#4x`)의 완료 기억이 근거로 잡혔다 - 오탐이라 뺐다.
- 규칙 이력: 같은 사본에서 evidence 를 5번 돌렸다(20:34 v1, 20:37 날짜 없는 기억 정책, 20:52 본문 단서·프로젝트 이름 keyword 빼기, 20:55 evidence 는 keyword 전부, 20:58 맨 번호 규칙 = 위 표).
  v1 표는 16건 중 15건(K05 없음, 날짜 없는 근거를 기준 이후로 잘못 셌다). 위 표가 최종이다.
- 규칙 v3 재판정(final-review rework #287-1): `[미완료]` 라벨을 done 으로 보지 않게 고쳤다. evidence 재판정 명령은 없다(recall 원문 행을 저장하지 않는다).
  이 수정은 완료 판정을 없애기만 하므로 받아들인 근거 116건의 라벨을 확인했다: `미완` 라벨 0건 -> 위 표는 바뀌지 않는다. upstream error 본문 거부는 저장 원문으로 확인할 수 없다.

## O10 판정: 분리

O10 = 회귀 세트(hc-next)와 벤치 dev(bench 복제본)를 같은 사본에서 잴 수 있나. **분리** - 지금 코드로는 같은 사본에서 잴 수 없다.

- `bench/runner.py:373-381` `check_arm_ports`: 팔 포트는 `label=hc-bench=1`이고 이름이 `hc-bench-gw-`로 시작하는 컨테이너만 받는다.
  사본 가동 중 실행: `uv run --python 3.12 --no-project python -B -c "from bench import runner; runner.check_arm_ports({'next': 18888})"`
  -> `arm next: port 18888 is not a running bench gateway (found [])`. hc-next 컨테이너(`hc-next-gateway-1` 등)에는 `hc-bench` 라벨이 없다.
- `bench/runner.py:648-658` `read_token`: 팔에 보내는 토큰은 벤치 ACL 전용 `bench-client.key`다. hc-next 게이트웨이는 운영 `secrets/acl.json`을 마운트하므로 그 토큰을 모른다.
- `bench/replica.py:85-93` `HS_OVERRIDES`: 벤치 복제본은 `HINDSIGHT_API_WORKER_ENABLED=false`, `ENABLE_AUTO_CONSOLIDATION=false`,
  `CONSOLIDATION_RECONCILE_INTERVAL_SECONDS=0`, `MENTAL_MODEL_REFRESH_TICK_SECONDS=0`, `LLM_PROVIDER=none`으로 동결된다. 재통합이 돌지 않으므로
  1단계 arm(관찰 범위 설정 변경 -> 재통합)의 효과는 벤치 복제본에 나타나지 않는다.
- hc-next 실측(사본 가동 중, 컨테이너 env 필드만): `HINDSIGHT_API_WORKER_ENABLED` 미설정(기본 켜짐), `HINDSIGHT_API_LLM_PROVIDER=claude-code`,
  `HINDSIGHT_API_ENABLE_AUTO_CONSOLIDATION=true` - 재통합이 돌고, 벤치 동결 계약(팔 사이 데이터 동일성)과 반대다.
- 비용 추정(분리일 때): 1단계 arm 마다 재통합이 사본별로 두 번 든다 - hc-next(회귀 세트) 1회 + 벤치용 복제본 1회. 지금 벤치 복제본은 동결이라
  재통합할 수 없으니, 벤치 쪽은 갱신 replica 모드(`bench/refresh.py`)처럼 동결을 푼 별도 변형이 필요하다. 재통합 1회 시간은 이 런에서 재지 않았다
  (D10 은 복원 직후 `pending_consolidation` 0 - 쌓인 작업이 없었다). 관찰 4,924개 전체 재통합이면 LLM 호출이 관찰 수에 비례한다.
- 1단계 티켓으로 넘길 변경 초안(이 티켓은 고치지 않는다):
  1. `bench/runner.py` `check_arm_ports`에 `--allow-next` 경로: `HC_TARGET=next` + 포트 18888 + 컨테이너 `hc-next-gateway-1`이면 받고, 토큰은 HC_SECRETS_DIR ACL 의 owner 토큰.
     단 동결이 아니므로 판정기의 A/A 대조·동일성 가정은 쓸 수 없다(재통합 뒤 1회 측정으로만).
  2. 또는 `bench/replica.py`에 "동결 해제 -> 재통합 drain -> 다시 동결" 단계(`reconsolidate <set>`): 세트마다 같은 arm 설정으로 재통합한 뒤 동결 상태로 벤치를 잰다.
     세트 사이 재통합 결과(LLM 비결정)가 달라 동일성 검증(`verify --set`)이 깨질 수 있다 - 세트 하나로 재통합 뒤 볼륨 복제가 필요하다.
