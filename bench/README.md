# recall 재생 하네스 (issue #13)

> **운영자 도구:** 운영 환경(personal-demo, demo identity, 실사용 기록) 전제, 외부 재현용 합성 예제는 아직 없음.
> 판정 결과만 한 장으로 보려면 [`docs/benchmarks.md`](../docs/benchmarks.md)를 본다.

recall 튜닝 결과를 **속도·정확도·비용(토큰)** 세 축으로 자동 채점하는 시험지와 채점기다.
채택 기준(운영자 정본): 하나라도 개선 + 나머지 불악화 = 합격, 트레이드오프 불허. 오토리서치의 유일한 판정 기준이다.

## 구성

| 파일 | 역할 |
|---|---|
| `corpus.py` | 시험 문제. 실사용 recall 호출(Claude Code·Codex transcript, 게이트웨이 원장)을 모아 정제·중복 제거하고 dev / holdout 으로 분할 |
| `replica.py` | 시험장. 백업으로 복원한 **동결 복제본** hindsight 와, git ref(+patch)로 빌드한 게이트웨이 팔(arm)들 |
| `runner.py` | 측정기. 코퍼스를 팔들에 실제 클라이언트 경로(MCP, demo 토큰)로 직렬·교차 재생 |
| `judge.py` | 판정기. 기준 팔 대비 후보 팔의 3축 판정 -> PASS / NO-CHANGE / INCONCLUSIVE / FAIL |
| `cold_replay.py` | cold 재생 도구(#20). 실제 recall 후보 텍스트를 벤치 사이드카에 128x3 / 300x1 로 교대 재생해 cold 지표를 낸다 |
| `fixtures/*.patch` | 자기검증용 "결과를 아는" 변경 |

데이터(쿼리 텍스트 포함)는 커밋하지 않는다: `bench/.data/`(gitignore), holdout 은 리포 밖
`~/.local/share/hindsight-bench/holdout/`(0700). holdout run 산출물도 그 아래 `runs/` 에 쓴다.
벤치 키·상태·팔 출처는 `~/.local/share/hindsight-bench/state/`. 팔의 감사 로그(쿼리 원문 포함)는 컨테이너 tmpfs 에만 있다.
팔은 운영 `secrets/` 를 마운트하지 않는다: `gw-up` 이 운영 ACL 의 demo 권한만 복사한 벤치 전용 ACL(벤치 토큰 1개)을
`state/arm-secrets/` 에 만들어 마운트하고, runner 는 운영 토큰이 아니라 `state/bench-client.key` 로 호출한다
(후보 코드가 운영 토큰을 볼 수 없게 - final-review 2026-09-23. 라이브 확인: 벤치 토큰 recall ok, 운영 토큰 403).

## 사용법

```bash
# 0) 코퍼스 (재생성해도 기존 텍스트의 dev/holdout 소속은 salt 로 고정. 분할 규칙이 바뀌면 --reset-split 요구)
.venv/bin/python -B bench/corpus.py

# 1) 시험장: 최신 백업으로 복제본 기동 + 동일성·동결 검증 (pass=true 여야 진행)
.venv/bin/python -B bench/replica.py up
.venv/bin/python -B bench/replica.py verify

# 2) 팔 세우기: 기준(현재 main)·A/A 대조·후보
.venv/bin/python -B bench/replica.py gw-up base --ref main --port 18891
.venv/bin/python -B bench/replica.py gw-up ctrl --ref main --port 18892
.venv/bin/python -B bench/replica.py gw-up cand --ref <branch> --port 18893 [--env K=V] [--patch x.patch]

# 3) 측정 (직렬이라 오래 걸린다: 쿼리 x 팔 x 라운드 x ~5s + 워밍업). 벤치 게이트웨이가 아닌 포트(8888 등)는 거부
uv run --python 3.14 --no-project --with tiktoken python -B -m bench.runner \
  --arm base=18891 --arm ctrl=18892 --arm cand=18893 --split holdout --rounds 3 --run-id <id>

# 4) 판정. A/A 대조는 허용 폭을 넓히지 않는다: 대조 속도 CI 가 [0.99, 1.01] 밖으로 e 만큼 나가면 개선·불악화
#    문턱을 e 만큼 좁히고(더 엄격하게만), e > 1% 거나 대조가 결과·비용 쌍둥이가 아니면 run 무효.
#    holdout 판정은 대조가 없으면 INCONCLUSIVE. 종료 코드: PASS 0 / FAIL 1 / INCONCLUSIVE 2 / NO-CHANGE 3
.venv/bin/python -B -m bench.judge ~/.local/share/hindsight-bench/holdout/runs/<id> \
  --baseline base --candidate cand --aa-control ctrl
#    사이드카 요청 모양을 바꾸는 후보는 cold 도 속도 축이다: --cold-report <cold run>/report.json 을 붙이거나(아래 cold 재생 도구),
#    잴 수 없었으면 --cold-unmeasured <사유>(합격을 보류로 내린다, warm_verdict 는 남는다). 셋 다 없으면 no-cold-input 으로 보류.
#    upstream 이 사이드카로 보내는 요청 모양(배치당 텍스트 수·요청 수)을 바꾸지 않는 후보만 --cold-exempt <사유> 로 cold 를 면제한다
#    (issue #169): 최종 판정 = warm 판정, 판정 JSON 에 cold.verdict=exempt·사유와 reasons 의 cold-exempt:<사유> 가 남는다.
#    요청 모양을 바꾸는 후보는 cold 도 속도 축이므로 쓰지 않는다. 세 인자는 하나만 받고, 빈 사유도 "주어짐"으로 본다

# 정리
.venv/bin/python -B bench/replica.py down
```

오토리서치 핑퐁에서는 연구 에이전트가 `--split dev` 로만 개발하고, 오케스트레이터가 `--split holdout` 으로 판정한다.

### upstream·사이드카 설정이 다른 후보: 세트 + 순환 교차 (2026-09-26)

위 흐름은 팔(게이트웨이)만 바꾼다. upstream env 가 다른 후보는 **세트**(이름 있는 복제본 인스턴스)를 팔마다 두고,
인스턴스마다 있는 고정 지연 오프셋을 **순환 교차**로 상쇄한다(단계 k 에서 팔 i 가 세트 (i+k) mod 3 을 쓴다).

```bash
# 1) 공유 벤치 사이드카 1개(운영 plist 복제 + override, 8787 거부). 세트마다 따로 띄우면 통합 메모리를 넘겨 스왑된다
.venv/bin/python -B bench/replica.py sidecar-up shared --port 18781 --env RERANK_MAX_TEXTS=300
# 2) 세트 3개(같은 백업) + 세트마다 게이트웨이. verify --set 이 세트별 동일성·사이드카 생존·같은 모델을 본다
for s in s1 s2 s3; do .venv/bin/python -B bench/replica.py up --set $s --sidecar-port 18781 --backup <pg0.tar.gz>; done
.venv/bin/python -B bench/replica.py gw-up g1 --set s1 --ref main --port 18891    # g2/s2/18892, g3/s3/18893
# 3) 계획 기록: 첫 단계 전에 팔·세트·바퀴 수를 남긴다(바퀴 = 모든 팔이 모든 세트를 한 번씩 거치는 연속 단계 3개).
#    계획 파일 = 병합 대상 run 디렉터리의 crossover-plan.json. 이 파일이 없으면 병합이 거부된다(issue #77)
CFG='{"cand":{"K":"V"}}'; A=(--arm base --arm ctrl --arm cand --set s1 --set s2 --set s3)
.venv/bin/python -B -m bench.crossover record-plan <runs>/<id> "${A[@]}" --laps 1
#    운영 보호 등으로 계획을 줄여야 하면 사유와 함께 덧붙인다(병합 뒤에는 거부). 줄인 뒤 목표는 바퀴 수로 주고,
#    현재 계획 바퀴 수보다 작아야 한다(최소 1 - 1바퀴 계획은 줄일 수 없다):
#    .venv/bin/python -B -m bench.crossover truncate <runs>/<id> --laps <N> --reason "<사유>"
# 4) 단계 3개: 단계마다 세트를 팔 설정으로 reconf(같은 볼륨 위 재기동) -> verify -> runner 1 라운드
for k in 0 1 2; do uv run --python 3.14 --no-project --with tiktoken python -B -m bench.crossover run-phase "${A[@]}" \
  --gw-port s1=18891 --gw-port s2=18892 --gw-port s3=18893 --configs "$CFG" --phase $k --split holdout --run-id <id>-p$k; done
# 5) 병합(계획 파일 필수. 단계마다 팔 라벨 = 계획한 세트, 세트 hs_env = 팔 설정인지 provenance 로 확인) -> 판정기는 교차 모드
.venv/bin/python -B -m bench.crossover merge <runs>/<id> "${A[@]}" --phase-dir <runs>/<id>-p0 ... --configs "$CFG"
.venv/bin/python -B -m bench.judge <runs>/<id> --baseline base --candidate cand --aa-control ctrl
```

- 교차 판정은 쿼리별 로그 비를 **바퀴마다 평균**하고(중앙값은 오프셋 쪽으로 쏠린다), 완전한 바퀴가 3개 이상이면 바퀴 값들의 중앙값을 쓴다
  (issue #82, 아래 "정밀도 설계"). p95 도 쿼리별 바퀴 단위 지연으로 보며, 상태 의존(캐시) 감지는 건너뛴다(단계마다 재기동 + 1 라운드라
  반복 호출 이득이 없다). `meta.plan` 이 없는 과거 교차 run 은 전 단계 평균 그대로 판정한다.
- warm 표본을 늘리려면 순환을 여러 바퀴 돈다: `--phase 0..3L-1`(배정은 k mod 3), 병합은 바퀴 단위(3, 6, 9 단계)만 받는다.
  교차 run 의 꼬리 표본 수는 쿼리 수 그대로고(쿼리마다 바퀴 단위로 모음), 바퀴를 늘리면 쿼리별 측정 잡음만 준다(issue #21).
  단계가 오염되면 그 바퀴만 그 쿼리에서 빠지고, 완전한 바퀴가 하나도 남지 않은 쿼리만 속도 판정에서 빠진다(`incomplete_phase_queries`,
  바퀴를 잃은 쿼리 수는 `speed.laps.queries_with_dropped_laps`). 과거 교차 run 에서 `incomplete_phase_queries` 는 단계가 하나라도 빠진 쿼리 수다.
- **계획 기록과 축소** (issue #77): `record-plan` 은 첫 단계 전에 팔·세트·바퀴 수·계획 단계 수·기록 시각을 쓰고, 이미 있으면 덮어쓰지
  않는다. 계획을 줄이는 길은 `truncate --laps N --reason "..."` 뿐이다(사유·시각·줄인 목표를 계획 파일에 덧붙이고, 처음 계획은 남는다.
  병합이 시작된 뒤에는 거부). 병합은 계획 파일이 없거나, 팔·세트가 계획과 다르거나, 어느 단계의 `started` 가 계획 기록 시각보다 이르면
  거부하고, `meta.plan` 에 계획 단계 수·실제 단계 수·축소 기록을 싣는다. 판정기는 실제 단계가 유효 계획(마지막 축소의 목표, 없으면
  처음 계획)보다 짧으면 속도를 `plan-truncated:3<6` 꼴(실제<계획)로 보류하고, 판정 결과의 `plan` 에 계획 대비 실제와 축소 사유를 싣는다.
  `meta.plan` 이 없는 과거 run 은 이 검사를 건너뛴다(판정 JSON 그대로). 계획 파일은 팔을 객체(`{"name": ...}`)로 둔다.
  `meta.plan` 을 해석할 수 없으면(모르는 version, 필드 누락) 판정기는 죽지 않고 `plan-invalid:<사유>` 로 속도를 보류한다.
  병합은 실제 단계가 유효 계획보다 **많아도** 거부한다(결과를 보고 더 돌린 연장과 구분할 수 없다 - 더 돌리려면 계획을 새로 기록한다).
  병합이 도중에 실패하면 이번 병합이 만든 산출물(`results/`, `calls.jsonl`, `prod_events.jsonl`, `meta.json`)만 지우고 계획 파일은 남긴다.
  손으로 정리할 때도 **`<runs>/<id>` 를 통째로 지우지 말고 계획 파일은 남긴다** - 계획을 다시 쓰면 기록 시각이 단계 `started` 보다
  늦어져 병합이 거부된다.
  한계: 위협 모델은 실수 방지다. 산출물을 손으로 지우거나 계획 파일을 복사하는 의도적 조작, 병합 전에 단계 run 을 먼저 판정해 본 뒤 하는
  축소(축소 시각을 단계 종료 시각과 비교하면 정당한 운영 보호 축소까지 막힌다)는 막지 않는다. 단계 meta 에 단계 번호가 없어 바퀴 일부만
  골라 병합하는 것도 탐지하지 못한다(#82 바퀴 집계도 바퀴 소속을 병합 순서로 정하므로 이 한계는 그대로다 - 아래 "한계").
- 세트 하나만 내리려면 `set-down <set>`(공유 사이드카는 남는다), 전부는 `down`(사이드카까지).
- macOS `/bin/bash` 3.2 에서 `set -u` 와 빈 배열 `"${X[@]}"` 를 함께 쓰면 unbound variable 로 죽는다 - 드라이버는
  `${X[@]+"${X[@]}"}` 로 쓴다(2026-09-26 holdout 드라이버가 측정 전에 이것으로 멈췄다).

### B2 run 준비: 보조 포트로 사이드카 배치만 다른 팔 (issue #83)

> **준비 절차다. 이 절의 명령은 #22 카나리 판정(2026-10-06 01:15 KST 이후) 전에는 실행하지 않는다.** 벤치 사이드카·복제본을 띄우는
> 것 자체가 운영 사이드카 지연을 오염시킨다. B2 판정은 #22 판정 뒤의 운영 설정 위에서, dev 교차 -> holdout 1회 순서로 한다.
> **실행됨 (2026-10-06, issue #84)**: dev 교차(`--limit 40`)까지 돌렸고 판정은 보류, holdout 은 미사용이다 - 아래 "후보 3 (B2)" 절.
> dev run 에는 `run-phase --limit 40` 을 주었다(아래 4)의 명령에는 없다).
> **재판정 (2026-10-08, 런 3)**: dev 교차 PASS 뒤 holdout 1회를 썼고 holdout 도 PASS 다 - 아래 "후보 3 (B2)" 절의 "런 3 재판정". holdout 은 다시 돌리지 않는다.

B2(사이드카 `RERANK_BATCH_SIZE` 16)는 사이드카 프로세스 단위 설정이라, 세트마다 사이드카를 띄우면 스왑이 난다(22/22.5GB).
그래서 벤치 사이드카 **하나**에 보조 포트(`RERANK_AUX_PORT`, `RERANK_AUX_BATCH_SIZE`, rerank-server README 의 "Auxiliary port")를 열고,
팔마다 세트가 붙을 포트만 바꾼다. 같은 모델·같은 직렬 워커라 두 팔의 차이는 배치 크기 하나다. 팔 -> 포트·배치는 계획 파일에 적고,
세트는 단계마다 그 팔의 포트로 `reconf` 된다. 팔 설정(`--configs`)은 이 경우 upstream env 가 모두 같을 수 있다(`{}`).

```bash
# 1) 벤치 사이드카 1개 + 보조 포트. 데드라인 override 가 필수다(아래 이유). 보조 포트·배치는 운영 포트(8787)·대역(18780-18799) 밖이면 거부된다
.venv/bin/python -B bench/replica.py sidecar-up shared --port 18781 \
  --env RERANK_AUX_PORT=18782 --env RERANK_AUX_BATCH_SIZE=16 \
  --env RERANK_TOTAL_DEADLINE_SECONDS=120 --env RERANK_QUEUE_TIMEOUT_SECONDS=120
# 2) 세트 3개. up 에 준 포트·배치는 처음 상태일 뿐이고 run-phase 가 단계마다 팔의 값으로 다시 붙인다. 8787 은 거부된다
for s in s1 s2 s3; do .venv/bin/python -B bench/replica.py up --set $s --sidecar-port 18781 --sidecar-batch-size 32 \
  --backup <pg0.tar.gz>; done
.venv/bin/python -B bench/replica.py gw-up g1 --set s1 --ref main --port 18891    # g2/s2/18892, g3/s3/18893
# 3) 계획 기록: 팔마다 포트:배치를 함께 적는다(모든 팔 또는 없음). base·ctrl = 주 포트(운영 plist 배치 32), cand = 보조 포트(배치 16)
A=(--arm base --arm ctrl --arm cand --set s1 --set s2 --set s3)
.venv/bin/python -B -m bench.crossover record-plan <runs>/<id> "${A[@]}" --laps 3 \
  --arm-sidecar base=18781:32 --arm-sidecar ctrl=18781:32 --arm-sidecar cand=18782:16
# 4) 단계: --plan-dir 을 주면 계획의 포트·배치로 세트를 reconf 하고, verify 가 세트가 붙은 포트의 사이드카 /info 배치와 대조한다
for k in $(seq 0 8); do uv run --python 3.14 --no-project --with tiktoken python -B -m bench.crossover run-phase "${A[@]}" \
  --gw-port s1=18891 --gw-port s2=18892 --gw-port s3=18893 --configs '{}' --phase $k --split holdout \
  --run-id <id>-p$k --plan-dir <runs>/<id>; done
# 5) 병합: 단계마다 세트 provenance 의 sidecar_port·sidecar_batch_size 가 그 단계에 배정된 팔의 계획값과 다르면 거부
.venv/bin/python -B -m bench.crossover merge <runs>/<id> "${A[@]}" --phase-dir <runs>/<id>-p0 ... --configs '{}'
# 6) 판정: B2 는 upstream 이 보내는 요청 모양이 같으므로 cold 를 면제한다(--cold-exempt 없이는 no-cold-input 으로 보류된다)
.venv/bin/python -B -m bench.judge <runs>/<id> --baseline base --candidate cand --aa-control ctrl \
  --cold-exempt "<사유: 요청 모양 불변을 한 줄로>"
```

- **cold 면제는 요청 모양이 같을 때만 쓴다 (issue #169).** B2 는 사이드카 안에서 배치만 나누고 upstream 이 사이드카로 보내는 요청 모양
  (배치당 텍스트 수·요청 수)은 그대로라 cold 측정 대상이 아니다. 이런 후보만 `--cold-exempt <사유>` 로 판정하면 최종 판정이 warm 판정이 되고,
  사유는 판정 JSON(`cold`, `reasons` 의 `cold-exempt:<사유>`)에 남는다. 요청 모양을 바꾸는 후보(B1)는 cold 도 속도 축이라 쓰지 않는다.
  `--cold-report`·`--cold-unmeasured` 와는 함께 줄 수 없고, 면제 판정에도 A/A 대조(`--aa-control`)·`aa-too-noisy` 허용 폭·오염 상한은 그대로 적용된다.
- **데드라인 override 를 빼먹지 않는다.** 사이드카는 7.5s 절대 데드라인에 503 을 주지만 이미 시작한 계산은 끝까지 돈다. 기본값으로
  벤치 사이드카를 띄우면 cold 벤치 호출이 503 으로 끊기거나 늦은 계산이 다음 호출과 겹친다. `RERANK_TOTAL_DEADLINE_SECONDS`·
  `RERANK_QUEUE_TIMEOUT_SECONDS` 를 둘 다 120 으로 준다(후자는 전자 이상이어야 한다). 보조 포트도 같은 데드라인·큐 설정을 쓴다.
- **verify 가 보는 것** (`replica.py verify --set <s>`, run-phase 가 단계마다 전부 호출): 컨테이너 env 의 리랭커 주소가 세트 state 의 포트와
  같은지(기존), 그 주소의 `/info` 가 말하는 배치(`batch_size`, 있으면 `batch_sizes[<포트>]` 도)가 state 의 `sidecar_batch_size` 와 같은지
  (`sidecar_batch` 항목), 주소가 운영 사이드카(8787)가 아닌지(`sidecar.targets_production`). 기대 배치를 지정하지 않은 세트(`up` 에
  `--sidecar-batch-size` 를 안 줌)는 배치를 대조하지 않는다 - 기존 동작 그대로다. 보조 포트가 없는 사이드카의 `/info` 에는 배치 필드가
  없어 기대 배치가 있으면 불일치로 실패한다.
- **한 번에 하나만 지정한다.** `reconf` 와 `record-plan` 의 포트·배치는 함께 준다. 포트만, 배치만, 일부 팔만 주면 거부된다.
- **병합 대조는 계획 파일 기준이다.** `merge` 인자가 아니라 첫 단계 전에 기록한 계획의 팔 항목(`{"name", "sidecar": {"port", "batch_size"}}`)과
  비교하므로 병합할 때 값을 바꿔 맞출 수 없다. 지정 없는 계획(과거 run 포함)은 팔별 대조 대신
  "한 단계의 세트들이 같은 포트·배치를 가리켰는지"만 본다. run-phase 를 `--plan-dir` 없이 돌리면 세트가 계획의 포트로 붙지 않는다 -
  세트 state 에 기대 배치(`up --sidecar-batch-size`, 앞 reconf)가 있으면 run-phase 가 reconf 전에 거부하지만, 없으면 막지 못하고 병합에서야
  거부된다. 단계를 돌리기 전에 항상 준다. B2 를 한 번 거친 세트를 지정 없는 교차에 다시 쓰려면 `up` 을 다시 한다.
  팔 지정 없는 `reconf` 는 `up` 때 값이 아니라 세트 state 에 마지막으로 기록된 포트·배치를 그대로 쓴다(교차 단계 사이의 일반 reconf 가 팔 설정을 잃지 않게).
- 병합이 보는 포트·배치는 계획 대비 세트 state 의 기록 일치다. 사이드카가 실제로 말한 배치는 단계 시작 때 verify 가 `/info` 로 대조해 실패로 막고
  run meta 에는 싣지 않는다(관측값을 meta 에 싣는 것은 runner 범위 - 이 티켓 밖).
- keep-warm (issue #90): `run-phase` 는 단계가 도는 동안 운영 사이드카 keep-warm 을 켠다(단계 사이 reconf·verify 구간에는 보내지 않는다). 병합 뒤
  `meta.keepwarm.exclude_lines`(단계 순서, keep-warm 기록이 없는 과거 단계는 건너뜀)를 사람이 확인해 `bench/fixtures/canary-exclude.txt` 에 붙인다(자동 아님).
  단계 시도가 크래시해 `keepwarm.json` 만 남았다면 병합이 `meta.keepwarm.unmerged` 와 경고로 알리니 그 줄도 확인해 같이 붙인다.

### 운영 규칙 (자기검증에서 얻은 것)

- **판정 run 은 3팔**: 기준(base) · A/A 대조(ctrl, base 와 같은 ref) · 후보. 팔 순서는 `arm_order`(라틴 방진 회전
  + 역순)가 자리 균형을 잡지만 직전 팔(이월 효과) 균형은 팔 3개까지만 성립한다. 4팔 이상 run 은
  `carryover_balanced=false` 로 남고 속도가 미결이 된다(정확도·비용 fixture 전용).
- **A/A 대조는 쌍둥이만**: 결과 전부 identical + 토큰이 쿼리 단위로 같아야 한다. 아니면 판정기가 거부한다.
- **측정 중 복제본·사이드카에 다른 호출 금지**: 리뷰·디버깅이 같은 사이드카를 치면 지연이 오염된다.
  운영 트래픽은 막을 수 없으므로 판정기가 운영 감사 로그와 시간 대조해 겹친 쌍을 뺀다.
- **벤치 간섭 가드** (`bench/guard.py`, issue #19): runner 는 시작 전과 호출마다 맥 호스트 메모리를 보고
  임계를 넘으면 재개될 때까지 멈춘다. 정지 구간은 `meta.guard.windows` 로 남고 판정기가 겹친 호출을 속도에서 뺀다.
  가드가 중단하면 측정을 멈추고 meta 를 남긴 뒤 종료하며, 판정기는 그 run 을 `guard-aborted` 로 무효 처리한다.
  run 동안 운영 사이드카 로그의 503·데드라인 초과·큐 포화 수는 `meta.prod_sidecar` 에 실린다.
  임계값과 근거:

  | 신호 | 임계 | 근거 |
  |---|---|---|
  | swapout 속도 | 3 MB/s 초과면 정지 | 간섭 원인은 쉬는 사이드카 가중치의 스왑 아웃(selftest3-sets, 스왑 22/22.5GB). 벤치 없는 평상시 swapout 은 0 페이지/30초(2026-09-26 실측) |
  | 스왑 사용량 | 절대값은 안 봄. 가드 시작 뒤 +4GB 면 중단 | macOS 스왑은 줄지 않아 벤치 없이도 7.9/9.2GB(86%). selftest3 은 약 +14GB |
  | 압박 수준 | critical(4) 이상이면 정지 | warn(2)은 벤치 없이도 나온다(2026-09-26 실측, 여유 37%) |
  | 여유 메모리 | 10% 미만이면 정지 | 평상시 37~53% |
  | 신호 못 읽음 | 정지 | 닫힌 쪽 |

  재개는 10초 간격 연속 3회 정상이고, 정지가 30분을 넘으면 중단한다. 임계를 바꾸면 이 표의 근거도 함께 고친다.

  **실부하 관측 (2026-09-26, #20 cold 재생 시험 실행, 호스트 표본 10초 간격 127개)**: 3 MB/s 는 벤치 부하와 평상시를 가른다.
  - 벤치 사이드카 기동 + 세트 up(모델 적재): swapout 352 -> 66 MB/s, 스왑 8.8 -> 12.3GB
  - 세트 복제본에 holdout recall 연속(trace 추출): 6~23 MB/s
  - 세트 내린 직후 재생 시작: 26~46 MB/s -> 가드가 첫 확인에서 정지(`swapout`), 30.1초 뒤 재개. 겹친 프로브 1개가 제외됐다
  - 재생의 idle 대기와 프로브(사이드카 1개만): 0. 전체 중앙값 0, 여유 메모리 최저 29%, 스왑 +2.0GB(중단 임계 4GB 아래)
  - 한계: 이 창에 운영 rerank 요청이 0건이라 "정지가 운영 503 을 막았다"는 직접 증거는 아니다(운영 사이드카 503·데드라인 0)

  **가드가 못 막는 간섭 (2026-09-26~27, #21 warm 교차 run 6단계, 약 3.8시간)**: 운영 503 6건(전부 upstream 재시도로 recall 성공,
  재시도 최대 2/4, recall 실패 0). 모두 같은 모양이다 - 운영 사이드카가 46~58분 idle 인 채로 첫 포화 recall 이 들어오면, 그 순간
  벤치 사이드카도 계산 중이라 운영 첫 128 요청이 9.6~12.4s 로 늘어 대기 7.5s 를 넘는다(같은 순간 벤치 쪽 128 요청도 ~2s -> 6.8~7.9s).
  두 번째 재시도는 사이드카가 끝까지 계산하는 첫 요청 뒤에 줄서서 또 503 이 된다(23:48 건). 영향받은 운영 recall 은 20.6s(평소 5~8s).
  - 가드는 표본 간격마다 보고 호출 사이에서만 멈추므로 이미 도는 벤치 호출과 운영 cold 요청의 충돌을 막지 못한다.
    한 건(23:48)은 가드 정지 창 없이 났다 - 스왑 없이 MPS 동시 계산 + 운영 cold 만으로도 충돌한다
  - 기각: 단순 cold(운영 로그 idle 효과는 p50 +25~30% 라 4배를 설명 못함), MPS 경합 단독(동시 계산은 약 2배)
  - 이번 밤의 정지는 59회 4,822s(xho-b1 때보다 훨씬 잦다) -> 오염 쌍 48/408 로 속도 n 이 68 -> 31 로 줄었다
  - 대응(#76 결정, #79 구현): 운영 사이드카 keep-warm(아래). idle 길이를 보고 벤치 호출을 미루는 선제 가드는 운영 요청이 언제 올지
    예측할 수 없어 이미 도는 벤치 호출과의 충돌을 못 막으므로 기각했다

  **운영 사이드카 keep-warm** (`bench/keepwarm.py`, issue #79): runner 와 cold 재생 도구가 도는 동안 운영 사이드카(8787)에 쿼리 1·텍스트 1의
  리랭크 요청을 간격마다 보낸다. 운영 사이드카가 cold 가 되지 않으면 운영 첫 포화 recall 이 벤치 계산과 겹쳐도 warm 겹침(약 2배, 데드라인 안)이 된다.
  간격과 근거:

  | 항목 | 값 | 근거 |
  |---|---|---|
  | 간격 | 30초 | 사이드카 로그(운영 4,859건)에서 idle 뒤 지연 상승이 1분 부근에 몰리고 15분 뒤 평평하다(`CONTEXT.md` Flagged ambiguities, cold 5분 임계의 근거). 상승이 시작하는 1분보다 짧아야 오르기 전에 다시 데운다. 절반(30초)으로 잡은 것은 tick 이 벤치 호출 사이에서만 불려 실제 최대 간격이 간격 + 호출 한 단위(runner 호출 약 5.3~6초, cold 재생 대기 걸음 10초·프로브 약 10초)가 되기 때문이다 |
  | 상한 | cold 요청 기준 5분보다 짧게 | 직전 요청 뒤 5분 이상 idle 이면 cold 다(`bench/canary.py` `COLD_IDLE_S`, `bench/cold_replay.py` `IDLE_S`) |
  | idle trim | 30분 idle 뒤 MPS `empty_cache` | `rerank-server/README.md` `IDLE_TRIM_SECONDS=1800`. 위 #21 운영 503 6건은 46~58분 idle(trim 뒤) 사이드카에서 났다. keep-warm 은 idle 을 1분 안으로 묶어 trim 도 일으키지 않는다 |
  | 충돌 조건 | 운영 cold 첫 128 요청 9.6~12.4s(대기 7.5s 초과) vs warm 겹침 약 2배(약 2s -> 약 4s) | 위 "가드가 못 막는 간섭" |

  - **한계(근거)**: "1분 부근"은 `CONTEXT.md` 에 적힌 요약 수치다. 원본 분포(운영 로그)는 이 작업에서 읽지 않았고 리포에도 없다.
    1분 미만 간격이 실제로 cold 를 막는지, 쿼리 1·텍스트 1 요청 하나가 128/300 배치 모양의 cold 를 데우는지는 실측하지 않았다 - 효과 실측은
    다음 벤치 run 에서 운영 503·데드라인 수를 #21(6건)과 비교해 확인한다(#79 범위 밖). 간격을 바꾸면 이 표의 근거도 함께 고친다.
  - **호출 사이 끼워넣기**: 스레드 타이머는 측정 호출과 겹쳐 보내(runner 는 측정 지연을 흔들고, cold 재생은 그 요청이 운영 rerank 로 잡혀 프로브가
    `interference` 로 빠진다), asyncio 는 하네스 전체가 동기 코드라 기각했다. runner 는 가드 확인 뒤 호출 직전에, cold 재생은 idle 대기 걸음의
    가드 확인 뒤에 tick 한다(프로브 앞 가드 확인은 한 걸음 넘게 막혔을 때만. 프로브 측정 중에는 보내지 않는다). 대가: 한 호출(상한 90초)이나
    가드 정지 안에서는 못 보낸다. 둘 다 벤치가 계산하지 않거나 재개 직후 tick 이 다시 데우므로 "운영 cold + 벤치 계산 중" 충돌은 만들지 않는다.
  - **run 동안만**: `run()`·`execute()` 는 `keepwarm` 을 주입받을 때만 보낸다. 실제 송신기(기본 대상 운영 사이드카)는 CLI(`runner.main`·
    `cold_replay run`)만 만든다. 시작 전 가드 거부면 보내지 않고(cold 재생은 첫 가드 확인이 중단이면 보내기 전에 끝난다), 끝·가드 중단·예외 어느 쪽이든 run 이 끝나면 멈춘다. 실패(HTTP 비 200·예외)는
    기록하고 run 은 계속한다. keep-warm 은 게이트웨이를 거치지 않아 운영 감사 로그(`prod_events`)에는 남지 않고, 운영 사이드카 로그에만 남는다.
  - **meta 와 카나리 제외**: `meta.keepwarm` = `{url, interval_s, started, stopped, sent, failed, sent_at, failures, exclude_line}`(시각은 epoch).
    `failures` 항목은 `{t, error, status}` 이고 503 은 사이드카가 본문에 적은 사유(`rerank_queue_timeout`·`rerank_queue_full`)를 `error` 로 싣는다.
    `meta.prod_sidecar_keepwarm` 은 `meta.prod_sidecar` 와 같은 키(`http_503`·`deadline`·`queue_full`)로 keep-warm 자신의 실패가 사이드카 로그에 남겼을 수를 센다
    (응답을 받은 실패만. 연결 실패·타임아웃은 대응을 알 수 없다). **#21 6건과 비교할 때는 `meta.prod_sidecar` 에서 이 수를 뺀다.**
    `exclude_line` 은 run 구간에 cold 판정 꼬리 300초를 더해 `bench/fixtures/canary-exclude.txt` 형식 한 줄(KST, 시작 내림·끝 올림한 초)로 적은 것이라
    그대로 그 파일에 붙이면 카나리·기준선 추출이 구간을 벤치 창으로 뺀다(`canary.parse_exclude` 통과를 테스트가 고정). 파일에 붙이는 것은 자동이 아니다.
    run 이 예외·Ctrl-C 로 죽으면 meta.json 은 없고(판정기가 부분 run 을 판정하지 않게) 같은 내용이 `keepwarm.json` 으로 남는다(runner).
    **교차 run** (issue #90): `crossover run-phase` 가 CLI 에서 `KeepWarm.production()` 을 `run_phase(keepwarm=)` 로 넘겨 단계마다 켠다(단계 meta 에 `keepwarm` 이 남는다).
    `crossover merge` 는 단계 meta 의 keep-warm 구간을 병합 meta `keepwarm.phases` 로, 제외 줄을 `keepwarm.exclude_lines` 로 단계 순서대로 옮긴다.
  - **cold 재생 도구**: 운영 rerank 구간(`meta.prod_intervals`)에서 자기 keep-warm 요청(송신 구간에 통째로 들어가고, 로그 줄에 `candidates=` 가 있으면
    그것이 1인 구간)을 빼므로 프로브가 `idle-broken`·`interference` 가 되지 않는다. 같은 창에서 끝난 후보 수가 1 이 아닌 남의 요청은 지우지 않고, 후보 수를
    알 수 없는 줄은 송신 구간만 본다. 그 밖의 남의 운영 rerank 도 그대로 센다. 운영 사이드카와 벤치 사이드카는 같은 MPS 장치라, 대기 걸음
    앞의 작은 요청이 벤치 사이드카의 cold 지연을 바꾸지 않는다는 것은 실측하지 않았다.
  - **카나리 쪽 부작용과 꼬리**: 카나리의 idle 은 제외 구간의 요청도 활동으로 센다. 그래서 제외 줄의 끝에 cold 임계(5분, `COLD_IDLE_S`)를 더했다 - 마지막 keep-warm
    뒤 5분 안의 운영 recall 은 keep-warm 때문에 cold 가 아니라 warm 으로 분류되므로 그 꼬리도 표본에서 뺀다(테스트 `WARM_TAIL_S == canary.COLD_IDLE_S`).
  - **알고 둔 한계 (final-review 2026-10-02에서 기각한 지적)**:
    - cold 재생에서 keep-warm 이 같은 MPS 장치에 넣는 활동을 간섭 신호에서 빼므로, 장치가 데워져 cold 지연이 낮게 나와도 표시되지 않는다. 안 빼면 모든 프로브가
      `idle-broken` 이라 재생 자체가 불가능하다. 실측 없이 규칙을 바꾸면 cold 재생 판정의 뜻이 달라진다. 사후 복원은 `meta.keepwarm.sent_at` 으로 하고, 효과는 다음 run 에서 본다.
    - 데드라인 직전에 계산을 시작해 클라이언트 응답(503) 뒤에 끝난 keep-warm 요청은 송신 구간 밖(허용 0.25초)이라 외부 간섭으로 남을 수 있다. 드문 경로이고
      허용을 넓히면 남의 요청을 지우는 쪽으로 간다.
    - runner 의 tick 은 가드 확인과 측정 recall 사이라, 막힌 송신(최대 사이드카 데드라인 7.5초)이 가드 판단을 낡게 하고 사이드카가 데드라인 뒤에도 도는 계산이 측정
      recall 과 겹쳐도 간섭으로 표시되지 않는다. 표시하려면 `calls.jsonl` 스키마와 판정기를 바꿔야 해 이 티켓 밖이다(`meta.keepwarm.failures` 로 사후 대조).
    - runner 끝의 meta 작성 구간(`meta.json` 쓰기 직전의 로그 집계·지문 읽기)에서 Ctrl-C 가 나면 `meta.json`·`keepwarm.json` 이 모두 없다. run 이 이미 끝난 뒤의 드문 경로다.
- **소요 시간**: 호출당 약 5.3~6초 + 워밍업(최대 30쿼리, 안정되면 일찍 끝남). 40쿼리 x 3팔 x 3라운드 = 약 40분.
  holdout 전체(68) x 3팔 x 3라운드 = 약 60분.
- **holdout 은 후보마다 한 번**: 같은 후보를 PASS 가 나올 때까지 다시 돌리면 거짓 불악화 확률이 쌓인다.
  첫 판정 run 의 결과를 쓴다(재리뷰 P2-5 - 오케스트레이터 규율).
- **캐시 후보는 전용 프로토콜**: 재생은 같은 쿼리를 되풀이하고 복제본은 쓰기가 없어서 캐시 적중률(~100%)과
  무효화 경로가 실사용(~30%, 쓰기 있음)과 다르다. 판정기는 상태 의존 지연을 감지해 PASS 를 막을 뿐이다.
  캐시를 채택하려면 (1) 팔을 라운드마다 재기동해 첫 호출(미스) 지연이 불악화인지 (2) 운영 원장의 실제 반복률로
  가중한 기대 이득 (3) 쓰기 가능한 복제본에서 retain 뒤 recall 이 미스인지 - 를 따로 보여야 한다.
- **max_tokens 하한 후보는 판정 불가**: 하한 미달 호출은 코퍼스에서 policy-rejected 로 빠지고(34건), 기본값을
  낮추면 꼬리 절단(prefix-cut) 보류가 된다. 거부 응답 뒤 재시도 비용까지 묶은 별도 근거가 먼저다.
- **`prod_events` 과대계수 편향 (issue #48, 2026-10-01)**: #48 수정 전 `rerank_kind`는 감사 줄의 tool만 보고 세서, 한 요청이
  남기는 부가 줄(`enforce-autofill`·`enforce-normalize`·`contradiction-warn`·`reflect-demand` 등)과
  거부된 recall(`deny`)까지 요청으로 셌다. 운영 감사 로그 전체를 수정 전(#37 이후, retain-tags 는 이미 제외된 코드)/후 코드로 세면 retain 4,573 -> 2,314,
  recall 2,191 -> 1,863, reflect 28 -> 18, context-triggers 144 -> 133 이다(mental-models 142 불변, 합 7,078 -> 4,470).
  그 전에 끝난 run 의 `meta.prod_events`·`prod_events.jsonl`·크로스오버 합산은 이 편향으로 부풀어 있고(retain은 2배 안팎),
  간섭 겹침 판정이 실제보다 많은 쌍을 뺐을 수 있다. 과거 run 을 다시 해석할 때(#13 카나리 등) 이 편향을 명시하고, 근거 수치가
  달라질 수 있음을 전제로 읽는다. 재판정은 이 수정의 범위 밖이다. 수정 후에도 `allow` 뒤 rules 에서 거부된 요청(retain, max_tokens 하한에 걸린 MCP recall)과
  통째로 거부된 MCP 배치의 allow 줄은 1건씩 남는다(deny 배치는 첫 deny 에서 기록이 멈춰 deny 요소보다 앞선 요소만,
  rules 위반 배치는 allow 가 rules 판정 이전에 모두 기록돼 전체가 남는다) - 소폭 과대계수로 받아들인다.
  issue #62 보정: context-trigger 줄 중 `reason=miss`(매칭 트리거 없음, upstream recall 없음)와 멘탈 모델 REST 조회(GET)·`clear`는
  리랭크가 없는데 셌다(옛 코드는 경로에 `/mental-models` 가 들어 있으면 셌으므로 MCP 도구명 줄은 애초에 세지 않았다). 반대로 리랭크를 도는
  MCP `create_mental_model`(운영 로그 8건)은 세지 않던 과소계수였다. 수정 후에는 match 계열 context-trigger, REST `POST .../mental-models`·
  `POST .../<id>/refresh`, MCP `create_mental_model`·`refresh_mental_model`만 센다(운영 감사 로그 전체 기준 context-triggers 133 -> 48,
  mental-models 142 -> 62). #62 이전 run 의 context-triggers·mental-models 간섭 창은 과대(miss·GET·clear)와 과소(MCP create)가 섞여 있다.
  재판정은 범위 밖이다.
  issue #68 보정: 멘탈 모델 refresh·create 는 upstream 비동기 operation 이라 도착 후 종료까지 p90 151s(120s 초과 62건 중 11건)여서
  `PROD_WINDOW_S["mental-models"]`를 120 -> 240s 로 올렸다(61건 포함, 1,937s 이상치 1건은 한계). 또 2026-09-21 이전에는
  consolidation 이 멘탈 모델 refresh 를 감사 줄 없이 돌렸으므로(감사 줄 없는 refresh 274건 중 268건) 그 전 run 의 mental-models 간섭 창은 과소다.

## 자기검증 기록 1차 (2026-09-23, dev 40쿼리) - 이후 재리뷰로 판정 규칙이 바뀌었다(아래 2차)

| 비교 | 기대 | 판정 | 근거 |
|---|---|---|---|
| A/A (base vs ctrl) | NO-CHANGE | NO-CHANGE | 정확도 identical 40/40, 비용 0.0%, 속도 1.006 [1.000, 1.012] |
| 프루너 off -> on (실사례) | PASS | PASS | 정확도 lossless 40/40, 비용 -17.4%, 속도 1.012 [1.004, 1.019] 불악화 |
| +400ms 지연 fixture | FAIL | FAIL | 속도 1.077 [1.070, 1.085] (400/5200ms ≈ 7.7% 와 일치) |
| 역방향(지연 제거) | PASS | PASS | 속도 0.928 [0.922, 0.935] |
| 패딩 fixture | FAIL | FAIL | 비용 +12.1% |
| scores 제거 fixture | FAIL | FAIL | lossy 40/40 (비용은 -8.3% - 트레이드오프 차단) |
| 꼬리 1건 절단 fixture | INCONCLUSIVE | INCONCLUSIVE | prefix-cut 40/40 (비용 -11.4%, 근거 필요로 보류) |
| 순서 역전 fixture | FAIL | FAIL | changed 40/40 |
| A/A, 라틴 방진 3팔 | NO-CHANGE | NO-CHANGE | 속도 1.0006 [0.996, 1.005] - 구 교대 배치(5팔)의 1.006 [1.000, 1.012] 편향 해소 |
| 프루너 off -> on, 라틴 방진 3팔 | PASS | PASS | 비용 -17.4%, 속도 1.0015 [0.996, 1.006] 불악화 |

run: `selftest-acc-1`, `selftest-speedcost-1` (팔 5개, 구 교대 배치), `selftest-latin-1` (팔 3개, 라틴 방진 배치).
기준선 지연: 복제본 recall 중앙값 5.2~6.0초(run 마다 다름 - 그래서 매 run 기준 팔을 함께 잰다).

## 자기검증 기록 2차 (2026-09-23, 재리뷰 수정 후, dev 40쿼리, 새 텍스트 분할)

Opus 5.5 재리뷰 3건(판정기·복제본·측정기/코퍼스)의 P1/P2 를 고친 뒤 같은 복제본(백업 <벤치 백업>)에서 다시 쟀다.
모든 run 에서 동결 지문 전후 동일, 팔 rewrite 실패 0, 운영 리랭크 요청과 겹친 쌍 0.

| run | 비교 | 기대 | 판정 (exit) | 근거 |
|---|---|---|---|---|
| selftest2-aa-prune | A/A (base, ctrl) | NO-CHANGE | NO-CHANGE (3) | 속도 0.9985 [0.994, 1.003], 정확도 identical 40/40, 토큰 쿼리 단위 동일 |
| selftest2-aa-prune | 프루너 off -> on | PASS | PASS (0) | lossless 40/40, 비용 -17.5% (40쿼리 전부 감소), 속도 불악화 [0.992, 1.004] |
| selftest2-cache | 결과 캐시 fixture | PASS 금지 | INCONCLUSIVE (2) | 속도 비 0.0024 (구 판정기라면 PASS). 상태 의존 지연 d 차 6.25 [6.02, 6.44] |
| selftest2-sleep | +400ms fixture | FAIL | FAIL (1) | 속도 1.141 [1.085, 1.246], 쿼리 단위 꼬리 23/40 |
| selftest2-sleep | 역방향(지연 제거) | PASS | PASS (0) | 0.877 [0.803, 0.922]. A/A 초과 0.35% 만큼 문턱을 좁혀 판정 |
| selftest2-sleep-gap | +400ms, 호출 간 500ms 휴지 | FAIL / 역 PASS | FAIL (1) / PASS (0) | 1.144 / 0.874. A/A 초과 0.89% |
| selftest2-acc (5팔, 1라운드) | 패딩 | FAIL | FAIL (1) | added 40/40 + 비용 +12.2% (쿼리 40개 증가) |
| selftest2-acc | scores 제거 | FAIL | FAIL (1) | lossy 40/40 (비용 -8.4% 는 트레이드오프라 무의미) |
| selftest2-acc | 꼬리 1건 절단 | INCONCLUSIVE | INCONCLUSIVE (2) | prefix-cut 40/40 |
| selftest2-acc | 순서 역전 | FAIL | FAIL (1) | changed 40/40 |

- selftest2-cache 행은 판정기의 상태 의존 감지를 실측한 기록이라 남긴다. 결과 캐시 fixture 는 #81 불채택에 따라 #145 에서 제거했다.

발견 (2차에서 새로 얻은 것):
- **이월 효과**: 응답 뒤에 지연하는 팔(+400ms) 바로 다음 호출이 2~3% 빨라진다(쉬는 동안 upstream·리랭커 뒷정리가
  끝남). 라틴 방진이 횟수는 맞춰도 분산이 두 배가 되어 A/A CI 가 1% 를 넘었다. 호출 간 500ms 휴지로 해당 편향이
  -3.3% -> -2.1% 로 줄었지만 없어지지는 않았다 - 그래서 A/A 초과분은 문턱을 좁히는 데 쓴다(넓히지 않는다).
  무결과·결과 불변 후보(대부분의 튜닝)는 뒤 호출에 주는 영향이 작아 문제가 작다.
- **연속 호출 경합**: 휴지를 넣자 기준 팔 지연 중앙값이 6.15 -> 5.63초로 줄었다. 연달아 치면 서로 느려진다.
  운영의 드문 호출(약 4.7초)에 더 가깝게 재려면 휴지가 맞다.
- **워밍업**: 기준 팔 지연이 5.0 -> 6.3 -> 6.6초로 오르며 30쿼리 안에 5% 안정 조건을 못 채웠다(상한에서 끝남).
  본 측정은 안정 구간(6.4초 전후)에서 시작했다.
- 속도 효과 크기(1.14)는 쿼리 구성으로 설명된다: 쿼리별 기준 지연으로 계산한 기대값 1.121 + 이월 증폭 ~2%.

## holdout 기준선 (2026-09-25, A/A 3팔)

후보 판정 전 리허설이다. holdout 경로를 처음으로 끝까지 돌려 보고 holdout 에서의 A/A 노이즈를 잰다. 후보를 판정하는
run 이 아니므로 "holdout 은 후보마다 한 번" 규칙에 들어가지 않는다. 후보 run 은 base·ctrl 을 매번 다시 재므로 아래
절대값은 판정 입력이 아니라 참고값이다.

- run `holdout-baseline-20260925`: 3팔(base·ctrl·cand) 전부 main `8513640`, 백업 `<벤치 백업>`,
  holdout 68쿼리(sha256 `e28fb71a…`), 3라운드, 16:40~17:48 KST(약 68분). 캠페인 동안 백업과 코퍼스는 이 조합으로 고정한다.
- 판정: **NO-CHANGE (exit 3)**
  - 정확도: identical 68/68
  - 비용: 0.0% (321,742 토큰, 늘거나 준 쿼리 0개)
  - 속도: 0.9982 [0.9941, 1.0024], p95 비 1.0038, 쿼리 단위 꼬리 0
  - A/A ctrl: [0.9918, 0.999], 초과분 0 - 문턱 좁힘 없음
  - 운영 간섭 쌍 0/204, 운영 이벤트 0. 측정 612건 전부 ok. 측정 뒤 `verify` 가 다시 pass(동결 유지)
- 워밍업: 90건. 지연이 4.4초에서 5.2초로 오르며 안정된 뒤 본 측정에 들어갔다.
- 기준 지연: 중앙값 **5.47초**(최소 0.9초, 최대 7.4초)
- 발견: 쿼리별 지연 중앙값이 결과 수(r=0.00)·응답 토큰(r=0.07)과 무관하다. 대부분 5.5~6.0초에 몰리고 두 쿼리만
  약 1초다. 결과량과 무관한 고정비(약 4.5초)가 의심되며, 지연 퇴행(07-26 1.8~2.6초) 조사의 첫 가설이다.
  조사는 dev 쿼리로 재현해서 진행한다(holdout 쿼리를 들여다보지 않는다).

## recall 지연 조사와 후보 상한 분석 (2026-09-25)

기준선에서 확인한 "결과량과 무관하게 약 5.5초"를 조사했다. 결론: **퇴행이 아니라 bank 성장**이고, 결과를 바꾸지 않는 속도 개선은 약 10%가 상한이다.

- **원인 사슬**: 검색 후보(의미·키워드·그래프 합계 500개 이상) → upstream `HINDSIGHT_API_RERANKER_MAX_CANDIDATES`
  기본 300에서 절단(budget 무관) → upstream TEI 배치 128이라 사이드카에 3번 직렬 요청 → 리랭크 4.2~4.6초가 recall의 94%
  (trace `phase_metrics`: 임베딩 0.2초, 검색 0.05초).
- **퇴행이 아니라는 근거**: 사이드카 로그 60일(15,891건)에서 운영 후보당 비용은 07-27 이후 12~14ms로 일정하다.
  128 초과 포화 요청은 07-27부터 나온다. 07-26의 "1.8~2.6초"는 후보 69~100개 시점 하루치 수치다. 벤치는 연속 부하라
  후보당 16~18ms로 운영보다 약 15% 비관적이다. 기억이 길어졌지만(후보당 437→760자) 512토큰 절단이 상쇄한다(토큰 중앙값 390, 28%가 512 상한).
- **무손실 레버 실측** (운영 점수와 128/128 bit 일치하는 입력 재구성, dev 7쿼리 x 3회, 전 변형 점수 bit 동일):
  배치 16 -8%, 단일 300 요청 -6%, 길이 정렬 -3~4%, 배치 64 +4%. upstream·사이드카 설정이라 지금 팔 구조(게이트웨이만 교체)로는 A/B 불가.
- **상한을 낮추면 결과가 바뀐다** (`cap_profile.py`, dev 152쿼리): 결과 불변 비율은 상한 50에서 32%, 100에서 56%,
  150에서 72%, 200에서 80%, 250에서 94%다. 포화 쿼리의 필요 상한은 중앙값 98, 최대 294다.
- **상한을 올려도 얻는 게 작다** (`blind_eval.py`, 300 vs 600): 결과 변화 20/152. 클린 Claude 판정자 2명의 블라인드 판정에서
  일치 판정은 600 우세 5, 300 우세 3, 동률 10이다. κ 0.83, 대조군 8/8 동률. 리랭크 비용은 4.4초에서 10.5초로 늘어난다.
  → **상한 300 유지**.
- **trace 함정**: `reranked[].rerank_score` 는 결합 점수다. 리랭커 원점수는 `score_components.cross_encoder_score` 다.
  리랭커 입력은 `[Date: %B %d, %Y (%Y-%m-%d)] {context}: {text}` 이다(날짜·context 가 없으면 해당 부분을 생략).
  날짜는 기억의 `occurred_start` 다. trace 의 `event_date` 는 다른 칼럼이라 `occurred_start` 가 없는 기억에도 값이 있다 -
  이것으로 날짜를 붙이면 holdout 쿼리 하나에서 61/300 이 운영 점수와 어긋났고, `occurred_start` 로 붙이면 6쿼리 1,799/1,799 bit 일치(2026-09-26).

## 후보 1: chunk_id 형식 파생 (2026-09-25, PASS)

튜닝 축 결정(DEMO): "A 소형 → B". 비용 축에서 결과를 바꾸지 않고 더 줄일 수 있는 몫은 작다.
프루너가 켜진 dev 응답의 토큰 몫(40쿼리/395건, o200k)은 text 66.2%, scores 8.1%, 키 이름 5.0%, id 4.7%, tags 4.0%,
chunk_id 3.4%다. 이 가운데 필드 단위 무손실 정의로 뺄 수 있는 건 chunk_id 하나뿐이다.

- **규칙**: upstream chunk 키는 `{bank_id}_{document_id}_{chunk_index}`(chunks 테이블 PK)다.
  `chunk_id == f"{bank}_{document_id}_0"` 이면 요청 path 의 bank 와 남은 document_id 로 복원되므로 뺀다.
  0 번이 아닌 chunk(복제본 world 1,349건 중 9건), 다른 bank, bank 미상, 나가는 결과에 document_id 가 없는 경우는 남긴다.
  같은 선언이 `gateway/recall_prune.FORMAT_DERIVABLE` 과 `judge.FORMAT_DERIVABLE` 에 있고 패리티 테스트가 둘을 묶는다.
  판정기는 run meta 의 `bank`(runner 가 기록)로만 이 규칙을 쓴다. 구 run 에는 적용하지 않는다.
- **dev** `dev-chunkid-1`(152쿼리, 2팔 1라운드): lossless 152/152, 비용 -3.62%, 늘어난 쿼리 0.
- **holdout** `holdout-chunkid-20260925`(3팔, 68쿼리 x 3라운드, 캠페인 고정 입력 동일, 1회): **PASS (exit 0)**
  - 정확도 lossless 68/68 · 비용 **-3.82%** (321,742 → 309,462, 늘어난 쿼리 0 / 준 쿼리 68)
  - 속도 1.0006 [0.9956, 1.0059] 불악화, p95 비 0.9974, 꼬리 1/3.4, 오염 쌍 2/204
  - A/A ctrl NO-CHANGE [0.9935, 1.0011], 초과분 0. 동결 지문 전후 동일, rewrite 실패 0, 측정 뒤 `verify` pass
- **판정기 사각**: dev 결과의 chunk_id 824건이 전부 0 번이라, 조건 없이 지우는 후보도 벤치에서는 lossless 로 보인다.
  0 번이 아닌 chunk 를 남기는지는 단위 테스트(`tests/test_recall_prune.py`)와 코드 리뷰로만 막는다.

### 운영 재확인 (2026-09-28, issue #31)

배포(09-26) 뒤 운영 데이터로 절감 폭을 다시 쟀다. 도구는 `bench/prune_effect.py`: 원장(`audit/recall-queries.log`)의
demo·personal-demo 최근 고유 쿼리 20개(corpus 제외 규칙 적용, 인자는 원장의 query·tags·tags_match 만, max_tokens 생략 = MCP 기본값)를
운영 upstream MCP 에 직결(`docker compose exec hindsight` 안에서 `POST /mcp` + `X-Bank-Id`, 게이트웨이 미경유)로 직렬 1회씩 recall 하고,
같은 raw 응답에 #15 이전 프루너(`git show 92beb6c^:gateway/recall_prune.py` 를 임시 모듈로 로드)와 현재 프루너를 각각 적용해
클라이언트가 받는 content 텍스트의 o200k 토큰을 센다(runner 와 같은 정의). 산출물은 `~/.local/share/hindsight-bench/prune-effect/20260928T145632Z/`.

- **20쿼리 합계 -3.82%** (89,520 → 86,099, -3,421 토큰). 준 쿼리 20 / 늘어난 쿼리 0.
- **결과 불변 20/20**: 건수·순서·chunk_id 외 모든 필드·payload 나머지·structuredContent 가 두 프루너 사이에 같고,
  사라진 chunk_id 는 전부 `{bank}_{document_id}_0` 형식이었다.
- **chunk_id**: 결과 184건 중 chunk_id 가 있는 105건(world 65·experience 40)이 **전부 복원 가능**해서 빠졌고, 남은 chunk_id 0건.
  나머지 79건은 observation 이라 처음부터 chunk_id 가 없다. 제거 1건당 평균 약 32.6 토큰.
- **holdout -3.82% 와의 차이**: 소수점 둘째 자리까지 같다(holdout -3.817% vs 운영 -3.821%, 0.005%p). 두 측정은 모집단(holdout 68쿼리 x 3라운드,
  캠페인 고정 백업 vs 운영 최근 20쿼리, 09-28 시점 bank)과 방식(벤치 게이트웨이 팔 A/B vs 같은 raw 에 두 프루너 오프라인 적용)이 다르다.
  절감률은 응답에서 chunk_id 를 가진 fact 행의 비율과 text 길이로 정해지는데, 운영에서도 chunk_id 가 전부 0 번이라 벤치에서 본 몫이 그대로
  재현된 것이다. 0.005%p 까지 맞은 정밀도는 우연이다 - 20쿼리 1회 표본이라 신뢰구간을 내지 않았다.
- **호출 창 (KST, #22 카나리 기준선 제외 구간)**: 2026-09-28 23:55:11.077 - 23:56:32.806 (호스트 측 exec 창 시작 ~ 컨테이너 측 마지막 호출 끝.
  컨테이너 시계가 호스트보다 수십 ms 늦게 끝남으로 기록돼 두 창의 합집합을 쓴다). 운영 사이드카 로그에 이 창 안 `rerank_processed` 56줄,
  에러·503 0줄. 호출당 최대 5.19s, 합 81.5s.
- **직렬화 공정성 (final-review 반영)**: raw 응답은 upstream 헤더 마운트 형식(indent=2, 20건 중 6건에 `\u` 이스케이프)이라 한쪽 프루너만
  응답을 바꾸면 compact 재직렬화 차이가 절감으로 섞일 수 있다. 같은 raw.jsonl 을 오프라인으로 다시 보니 "이전 무변경 & 새 변경" 호출은
  **0/20**(20건 모두 양쪽이 변경)이었다. 도구는 이제 두 쪽 모두 같은 compact 직렬화로 다시 쓴 뒤 세고, structuredContent 도 값으로 비교한다.
  재계산 결과는 쿼리별 토큰까지 원 보고서와 같다(89,520 → 86,099, 불변 20/20).
- **표본 한계**: 선택된 20행 중 원장 `reusable=false` 1건 포함(스크럽 자리표시자 0건, 1000자 절단 0건, 최장 93자). 같은 초에 기록된 두 행 중
  나중 행이 아닌 앞 행의 tags 로 재생한 쿼리 1건(원장 ts 초 단위 동률, 당시 선택 규칙의 결함). 두 건 모두 두 프루너가 같은 raw 를 보므로
  불변·절감 비교에는 영향이 없고, 운영 대표성만 그만큼 약하다. 도구는 이제 `reusable=false` 행을 빼고 동률은 원장의 나중 줄을 고른다. 재실행은 하지 않았다.
- **어느 "현재" 프루너인가**: 실행 체크아웃은 b6d214a(bench 새 파일만 미커밋, gateway/ 무변경)이고 `gateway/recall_prune.py` 는 92beb6c 이후 변경이 없다.
  운영 게이트웨이 컨테이너의 `/app/gateway/recall_prune.py` sha256 도 리포 파일과 같다(<sha256 생략>, 이미지 <sha256 생략>).
  이후 실행분은 report.json `provenance` 에 repo HEAD·dirty·recall_prune blob·게이트웨이 이미지 id 를 남긴다.

## 후보 2 (B1): upstream TEI 단일 요청 (2026-09-26, 옛 기준 PASS -> 새 기준 보류 -> warm 재판정 형식상 보류(A/A 대조 쿼리 하한 미달) -> 최종 보류: warm aa-blocked + cold 로컬 측정 불가, #22 카나리로 이관 -> #22 카나리 ROLLBACK, B1 불채택(2026-10-06))

`HINDSIGHT_API_RERANKER_TEI_BATCH_SIZE` 128 -> 300. 포화 recall(후보 300)의 리랭크가 사이드카 요청 3개(128+128+44)에서
1개로 준다. 점수는 bit 동일(2026-09-25 무손실 변형 실측)이라 결과가 바뀌지 않는다. 사이드카 `RERANK_MAX_TEXTS`(256)가
300 을 413 으로 거부하므로 함께 올려야 한다(배포 절 참조).

**하네스 확장 경위**: 게이트웨이만 바꾸는 팔로는 upstream 설정을 A/B 할 수 없어 세트를 만들었다.
- selftest3-sets(세트마다 사이드카): 스왑 22/22.5GB, 사이드카 계산 p90 12.6s, A/A CI [0.83, 1.26] -> 사이드카 1개 공유로 전환(DEMO)
- selftest4-shared(+400ms fixture 1.132 [1.082, 1.221] 검출) 와 selftest5-aa3(3세트 전부 main): 세트 간 A/A 가
  base-ctrl 1.0149 [1.0026, 1.0315] 로 허용 폭 밖. 인스턴스마다 고정 지연 오프셋이 있다(159ms 쿼리에서 한 인스턴스가
  3 라운드 내내 +12~32%). pg 통계 차이(ANALYZE 0회)·차가운 캐시(쿼리 내 sd 가 단일 복제본보다 작다) 가설은 기각
- -> 순환 교차로 상쇄: dev `xdev-b1`(40쿼리) A/A 0.9973 [0.9895, 1.0062], 초과분 0.0005

**판정**
- dev `xdev-b1`: PASS - 속도 0.938 [0.931, 0.946], identical 40/40
- **holdout `xho-b1-20260926`**(교차 3단계 x 68쿼리, 캠페인 고정 입력, 1회): **PASS (exit 0)**
  - 속도 **0.9365 [0.9287, 0.9447]** (-6.4%), p95 비 0.956, 꼬리 0
  - 정확도 identical 68/68 · 비용 0 · A/A 1.0006 [0.9971, 1.0041] 초과분 0 · 동결 drift 없음 · 오염 0/204
- 첫 드라이버 실행은 bash 3.2 빈 배열 오류로 **측정 전에** 멈췄다(run 디렉터리 없음 - holdout 호출 0건). 고친 뒤 한 번 돌렸다
- **새 채택 기준으로 재판정(2026-09-26, 같은 run 데이터, 재측정 없음): INCONCLUSIVE(보류) `tail-hold:p99(n=67)`**
  - 채택 기준 속도 축에 warm p95·p99 비열등이 들어갔다(`CONTEXT.md`). 허용 폭 = max(m, A/A 잡음폭(최대 2m))
  - p95 0.956 [0.927, 0.997] ok / **p99 0.988 [0.947, 1.015]** - 상한이 허용 폭 1% 를 넘어 가를 수 없다
  - 단계 누락 쿼리 0(결함 02 영향 없음). 다음: warm 라운드를 늘린 교차 run + cold 재생으로 다시 판정(배포 보류)
- **warm 재판정 `xho-b1w-20260926`(issue #21, 교차 2바퀴 = 6단계 x 68쿼리, 캠페인 고정 입력, 1회): INCONCLUSIVE(형식상 보류) - 후보 수치는 개선·ok**
  - 처음 판정(f035eac, `judge-cand.json`)은 PASS(exit 0)였다. 그때 대조 게이트는 A/A 대조의 쿼리 수를 보지 않았다. final-review(#21)가
    이를 결함으로 확정해 고쳤고(`aa_gate` 가 대조 자신의 판정 조건 미달을 `aa-blocked:*` 로 무효 처리), 운영자 결정으로 사후 예외를
    두지 않는다: 후보 수치는 개선·ok(속도 0.9431 [0.9329, 0.954], p95 0.944, p99 0.945 [0.930, 0.974], 허용 1%)이나 A/A 대조가
    쿼리 하한 미달(29<30)이라 warm 도 형식상 보류. 대조 부족 원인은 오염 48/408(가드 정지·운영 겹침)
  - 설계: 교차 run 의 꼬리 n 은 쿼리 수(67)로 고정이라 바퀴를 늘려 쿼리별 측정 잡음을 줄인다. xho-b1 을 단계 1·2·3 개로 다시
    판정한 p99 CI 폭이 0.19 / 0.088 / 0.066 이라 폭은 거의 전부 측정 잡음(65/67 쿼리가 4~6.3s 한 덩어리). 점추정이 0.988 에
    머무는 비관 외삽으로 6단계 상한 ~1.007, 9단계 ~1.004 -> 3바퀴(9단계)로 계획
  - **9 -> 6단계 축소(00:20 KST, 판정 보기 전)**: 운영 503 이 되풀이됐다(위 "가드가 못 막는 간섭"). 결과가 아니라 운영 보호로
    정한 것이라 holdout 1회 규율에 걸리지 않는다. 6단계 run-phase 는 뜨자마자 끝냈다(p6 디렉터리 없음, 병합은 p0~p5)
  - 속도 **0.9431 [0.9329, 0.9540]**(-5.7%, improved) · **p95 0.944 [0.930, 0.976] ok · p99 0.945 [0.930, 0.974] ok**(허용 폭 1%)
  - 정확도 identical · 비용 0 · A/A 속도 CI [0.9864, 1.0060] 초과분 0.0036(문턱을 그만큼 좁힘) · A/A 꼬리 잡음 p95 0.0067, p99 0.0036
  - 동결 drift 없음 · rewrite 실패 0 · 가드 중단 0(정지 59회 4,822s) · 운영 사이드카 503 6 · 데드라인 6 · 큐 포화 0 · 운영 겹침 34
  - **주의: 오염 쌍 48/408(11.8%)** 이라 6단계가 다 남은 쿼리는 31/68(단계 누락 37). 속도·꼬리는 n=31 로 판정했다(하한 30).
    A/A 대조(base-ctrl)는 29 쿼리라 단독 판정은 `too-few-queries:29<30` 이고, 대조 게이트(`aa_gate`)가 이를 `aa-blocked` 로 무효 처리한다.
    오염은 시각으로 정해지고 팔은 같은 시각에 교대로 돌아 쿼리 선택이 한 팔에 치우치지 않는다
  - 다음: cold 재생(Loop 2, 방식당 40회) 결과와 합쳐 B1 최종 판정. warm 만으로는 배포하지 않는다
- **최종 판정 (issue #21, 2026-09-27): INCONCLUSIVE(보류) = warm aa-blocked + cold 측정 불가(로컬)**
  - `judge ... --aa-control ctrl --cold-unmeasured local-bench-sidecar-swapout` -> exit 2, `warm_verdict` INCONCLUSIVE,
    사유 `aa-invalid`(`aa-blocked:too-few-queries:29<30`) + `cold-unmeasured:local-bench-sidecar-swapout`
    (`xho-b1w-20260926/judge-final.json`. 후보 속도·꼬리 수치는 `judge-cand.json` 과 같다)
  - cold 재생을 두 번 시작해 두 번 다 조기에 멈췄다(아래 "cold 로컬 측정 불가"). 운영자 결정: cold 는 보류하고 #22 카나리에서
    운영 감시로 잰다. 로컬 재측정은 더 하지 않는다
  - #22 카나리가 사실상 유일한 판정이다 - 로컬 증거는 "warm 후보 수치는 개선·ok 이나 대조가 형식 미달, cold 는 모른다"이고
    합격이 아니다. 카나리 판정 기준은 warm 지연과 cold 초과율 둘 다로 둔다

**cold 로컬 측정 불가 (issue #21, 2026-09-27)**: 두 run 모두 `runs/*-aborted` 로 보관했다. 가드 진단 자료일 뿐 판정에 쓰지 않는다 -
제외되지 않은 프로브도 남은 짝이 제외돼 깨끗한 쌍이 0 이고, 그 raw 지연(프로브별 cold 합 4.3~4.7s)은 판정 근거가 아니다.
- `cold-b1-20260927`(17:22Z 시작): 9/80 에서 운영자 결정으로 멈춤. 9 프로브 중 7 이 가드 정지와 겹쳐(1 은 운영 rerank 도) 제외,
  깨끗한 쌍 0/4. 가드 정지 13회 전부 swapout(창 780s). 스왑 사용 8.1 -> 12.4GB, 압축기 적재 약 43GB, run 중 claude 세션 3개 새로 뜸
- 압박원 정리(쉬는 claude 세션 6개와 앱 5개 종료, 스왑 12.2 -> 8.0GB) + 시작 게이트(사이드카 적재 뒤 10초 표본 60개 연속 swapout 0 -
  통과, 피크 0.00MB/s) 뒤 `cold-b1-20260927b`(18:28:54Z): 5/5 제외(가드 4, 운영 rerank 겹침 1) -> 조기 판단 규칙(첫 10 프로브에서
  깨끗한 비율 < 0.71 이면 멈춤, 운영자 승인)으로 멈춤. 최대 0.5 라 결과가 바뀔 수 없었다. 가드 정지 8회 전부 swapout(371s)
- **원인: 벤치 사이드카의 계산 자체가 swapout 을 만든다.** 두 run 의 가드 창 21개 중 8개가 프로브 계산이 끝난 뒤 10~50초에
  몰렸다(b 에서는 프로브 1·2·3 끝마다 10~20초 뒤, 3/3). 이 창은 다음 프로브의 idle 창 앞부분에 떨어져 그 프로브를 제외시킨다 -
  호스트가 조용해도 매 프로브가 스스로 다음 프로브를 오염시키는 구조다. 나머지 13개는 idle 중간·후반(70~291초)에 흩어져 외부 원인도 있다
- 메모리 여유가 스왑 경계에 붙어 있다: 호스트 24GB = colima VM 10GiB + 운영 사이드카 + 벤치 사이드카(fp16 가동 시 각 약 3GB,
  RSS + Metal) + 나머지. 게이트 통과 뒤 30분 새 스왑 사용 8.45 -> 9.14GB, 총량 9.2 -> 10.2GB. 앱 정리 뒤에도 재현됐다
- 제외 규칙(idle 창 안 가드 정지 = 제외, swapout 이면 cold 가 아니라 스왑 복귀를 잰다)은 그대로 옳다. 이 호스트에서 cold 재생과
  양립하지 않을 뿐이다 - 규칙을 느슨하게 해서 숫자를 만들지 않았다

**동시성 (직렬 벤치가 못 재는 축)**: 운영 사이드카 로그(07-26~09-26)에서 포화 recall 5,819건 중 154건(2.6%)이 다른 포화
recall 과 겹쳤다. 단일 요청이면 겹친 recall 이 사이드카 데드라인(7.5s, 대기 + 처리)을 넘길 수 있다는 가설을
`bench/concurrency_probe.py` 로 직접 쟀다(dev 20쌍, 두 recall 동시 발사):
- 쌍의 느린 쪽 중앙값 base 10.89s / cand 10.07s(-7.5%), 최대 11.96s / 10.94s, 오류 0 / 0, 사이드카 503·413 0건
- 이유: upstream `TEI_MAX_CONCURRENT=1` 이 인스턴스 안에서 TEI 요청을 줄 세운다 - 두 번째 recall 은 사이드카가 아니라
  upstream 세마포어에서 기다린다(데드라인은 요청 전송 뒤부터). 운영도 hindsight 1 인스턴스라 같은 구조다 -> 가설 기각

**배포 (운영자 머지 후 별도 세션)**: 순서가 중요하다 - 사이드카를 먼저 올리지 않으면 300 요청이 413 이 된다.
1. 배포 직전 hindsight 컨테이너 로그를 파일로 보존한다(재생성하면 소실된다, UTC):
   `docker compose --env-file .env.local logs --no-color --timestamps hindsight > ~/.local/share/hindsight-bench/canary/hindsight-<시각>.log`
2. 배포 직전 시각으로 `baseline` 을 다시 돌려 아래 "카나리 기준선" 수치를 교체한다(규칙 상수는 바꾸지 않는다):
   `python3 -m bench.canary baseline --until "<배포 시각 KST>" --days 14 --hindsight-log <1의 파일> --out ~/.local/share/hindsight-bench/canary/baseline-<시각>.json`
3. 사이드카 plist `RERANK_MAX_TEXTS` 256 -> 600(`rerank-server/README.md` 불변식: 클라이언트 배치 상한의 2배 이상) 후 launchd 재기동
4. `.env.local` 에 `HINDSIGHT_API_RERANKER_TEI_BATCH_SIZE=300` -> hindsight 재생성(재연결 공지 선발송)
5. 확인: 사이드카 로그 `rerank_processed candidates=300` 이 1 요청으로 찍히는지, recall 실측, verify-all GREEN
- 롤백: 먼저 1처럼 hindsight 로그를 보존(카나리 재시도 근거)한 뒤 `.env.local` 의 키 제거 후 재생성(사이드카 600 은 그대로 둬도 무해). 2026-10-06 실제 롤백(#22)은 템플릿·위협 모델과 맞추려고 plist 를 256 으로 되돌렸다(아래 롤백 기록)

**카나리 기준선 (issue #30, 배포 직전 고정 2026-09-29 01:14:27 KST - #22 판정 기준)**: `bench/canary.py`.
운영 사이드카 로그(회전본 전부)와 hindsight 로그 파일만 읽는다. 규칙 상수는 모듈 상단에 두고 `tests/test_bench_canary.py` 로 고정했다.
- 규칙(128x3·300x1 동일): 앞 요청 끝 뒤 1초 안에 시작한 요청은 한 버스트. 버스트를 앞에서부터 누적 후보 합이 300 에 닿을 때마다
  끊어 포화 recall 로 둔다(요청 모양이 128+128+44 또는 300 일 때만). 경계를 넘거나 모양이 다르면 그 recall 부터 버스트 나머지를
  비포화로 빼고 `overflow_dropped` 로 센다. 버스트 첫 recall 은 idle(직전 요청 끝 뒤) 5분 이상이면 cold, 4시간 이상이면 deep-cold,
  뒤 recall 은 warm. 7.5s 데드라인 경고가 붙은 버스트에서 바로 앞과 같은 (후보 수, chars) 요청은 upstream 재시도라 합에서 빼고
  지연에 넣는다(없으면 7.5s 초과 recall 이 전부 128+128+128+44 = 428 로 빠진다). 데드라인 경고는 그 시각에 계산 중인 요청에만
  붙이고 못 붙인 경고는 `unattached_deadlines` 로 센다(실로그 45건 전부 계산 중 요청에 붙어 수치 불변)
- 판정은 #22 기준대로 7.5s 초과율을 **cold recall 한정**으로 본다. warm 초과(`warm_exceed`)는 참고로만 출력한다
- 입력 검사: 사이드카 로그의 가장 이른 줄이 기준선 시작보다 늦으면 baseline 은 INVALID(`log-coverage`). hindsight 로그는
  구간의 첫 rerank 요청 앞에서 시작해 마지막 요청 끝(1초 허용, 로그 시각은 초 단위)까지 닿아야 `measured`, 아니면 `partial`.
  `--hindsight-log` 경로가 없거나 인자·파일·형식 오류면 INVALID(종료 코드 4). judge 는 기준선 `days` 가 14 가 아니면 INVALID.
  방식 불일치·기준선 무효여도 창 안 첫 300x1 포화 recall 뒤에 재시도 소진이 있으면 ROLLBACK 이 먼저다(사유에 INVALID 병기)
  judge 도 사이드카 로그의 가장 이른 줄이 `--since` 보다 늦거나 로그가 없으면 같은 문장(`log-coverage: ...`)으로 INVALID 다(앞부분이 빠진 채
  7일을 채운 것으로 보는 거짓 PASS 차단). 커버리지 부족은 PASS·HOLD·PENDING 만 막고 ROLLBACK 은 가리지 않는다: 커버리지만 부족하면
  `since..until` 전체의 재시도 소진과 덮인 구간의 지표 악화(cold p95 등)가 그대로 ROLLBACK 이고 사유에 `INVALID:log-coverage` 가 병기된다.
  기준선 무효와 겹치면 `since..until` 전체의 재시도 소진만 ROLLBACK 으로 본다(방식 불일치가 겹치면 위 첫 300x1 이후 규칙)
- 로그 커버리지 보강 (issue #104, #98 후속). baseline 과 judge 는 같은 `coverage_problems` 를 쓴다(기준선과 후보의 기준이 갈라지면 비교가 무의미):
  - **앞**: `log_from_ts` 는 줄 머리(진행 막대의 `\r` 뒤 포함)에 로그 형식 시각이 찍힌 **모든 줄**(요청·경고·idle trim)의 가장 이른 시각이다. 메시지 본문·traceback 중간의 시각 문자열은 증거로 쓰지 않고(이른 값은 앞을, 미래 값은 꼬리를 거짓 통과시킨다) 날짜로 해석되지 않는 값은 건너뛴다. 요청 줄만 보면 since 앞에 파일이
    있어도 그 사이 요청이 없는 새벽 idle 이 거짓 `log-coverage` 가 되고 메시지의 "starts at" 도 실제 시작과 다르다. 이 시각이 `since` 보다 늦으면 INVALID
  - **꼬리**: 가장 늦은 줄이 `until` 보다 `LOG_TAIL_TOLERANCE_S`(사흘) 넘게 앞이면 INVALID(`log-coverage: sidecar log ends at ...`). 낡은
    `--log-dir` 복사본·꼬리 손실이 `until - since` 로 PASS 가 나던 것을 막는다. 허용을 사흘(72시간)로 둔 근거: 사이드카는 요청·경고·idle trim 이 있을 때만 줄을
    남겨서 조용한 주말(금 저녁~월 아침 약 63시간)은 정상이다. 따라서 **사흘 이내의 꼬리 손실은 잡지 못한다**(의도된 한계 - 더 줄이면 주말마다 거짓 INVALID).
    이 상수는 `RULES` 에 넣지 않았다: judge 가 기준선 JSON 의 `rules` 와 `RULES` 를 대조하므로 키가 늘면 운영 기준선이 baseline-invalid 가 된다
  - 앞·꼬리 모두 **PASS·HOLD·PENDING 만 막고 ROLLBACK 은 가리지 않는다**(재시도 소진, 덮인 구간의 지표 악화는 `until - since` 문턱 그대로 ROLLBACK, 사유에 `INVALID:log-coverage` 병기)
  - **세대 수**: `read_sidecar_logs` 는 압축된 `server.log.N.gz` 를 N 제한 없이 전부 찾아 N 큰(오래된) 순으로 잇는다. 회전 세대 수는 설정(`RERANK_LOG_GENERATIONS`)이라
    고정 목록을 두지 않는다. 압축 전 `server.log.N` 은 읽지 않는다: `rotate-logs.sh` 는 cp -> truncate -> gzip 이라 그 파일은 truncate 전에 `server.log` 와 같은 줄을
    담아 중복 집계되고 gzip 뒤에는 지워져 읽기가 실패한다(덜 읽으면 앞·꼬리 커버리지 INVALID 라 안전하다. 옛 코드도 `.gz` 만 읽었다)
  - **고아 고세대 (issue #119 C3, 고침)**: `RERANK_LOG_GENERATIONS` 를 줄이면 `rotate-logs.sh` 가 더는 덮지 않는 `.N.gz` 가 낡은 줄로 남는다. 이 줄이 `log_from_ts` 를
    since 앞으로 당겨 실제로는 since 부터 없는 로그의 앞 커버리지를 거짓 통과시켰다. `read_sidecar_logs(log_dir, since)` 는 **since 가 파일 경계의 사흘(`LOG_TAIL_TOLERANCE_S`, 꼬리와 같은 값)
    넘는 시각 공백 안에 있을 때만**(앞 파일은 마지막 줄까지 통째로 since 앞, 다음 파일은 since 뒤에 시작) 그 앞 세대부터 전부 읽지 않는다(판정 JSON `sources` 에도 없다 - 디렉터리에 있는데
    `sources` 에 없는 `server.log.N.gz` 가 있으면 고아로 무시된 것이다). 그 밖에는 자르지 않는다: since 를 덮는 세대나 체인이 이미 since 에 닿은 경우 앞 세대의 요청은 지표·idle 계산의
    입력이라, 버리면 지표 악화 ROLLBACK 신호가 사라지거나(사이드카가 사흘 넘게 쉬다 회전 경계에서 재개된 정상 로그) 공백 뒤 첫 recall 의 idle 이 None 이 된다(final-review 에서 확인해 조건을 좁혔다).
    baseline 은 자기 창 시작(`until - days`), judge 는 `--since` 를 넘기고 같은 `coverage_problems` 를 쓴다. 세대 **번호 상한은 두지 않았다**
    (세대 수는 설정이라 올리면 큰 N 도 정상이고, 하드코딩하면 정상 세대를 버려 진행 중 판정이 바뀐다). 세대 간 **시각 단조성**으로도 못 거른다(고아는 체인보다 항상 오래돼 순서가 어긋나지
    않는다 - `test_issue119_c3_orphans_are_monotone_so_time_ordering_cannot_find_them`). 한계: 사이드카는 요청이 있을 때만 줄을 쓰므로 since 가 사흘 넘는 정상 정지 안에 있어도 고아와 구별되지 않아
    앞 커버리지 부족(INVALID)이다(재시도 소진·지표 악화 ROLLBACK 은 가리지 않는다). 허용 안쪽 공백의 고아는 이어진 로그와 구별되지 않아 읽는다 - 꼬리와 같은 허용이다.
    이 경우 잘린 세대의 pre-since 요청이 빠져 공백 뒤 첫 recall 의 cold 분류가 None 이 될 수 있으나 앞 커버리지 INVALID 판정이라 지표 표본 1건 차이에 그친다.
    RULES 키는 늘리지 않았다. 정상 입력의 읽기 결과는 이전과 같다
  - **세대 한 파일 안의 공백과 파일 경계의 허용 안쪽 공백은 보지 않는다 - 의도된 한계.** 시각 공백으로는 조용한 주말과 구별되지 않아 거짓 INVALID 가 나고(앞·꼬리처럼 문턱을
    둘 수 없다), `rotate-logs.sh` 는 세대를 연속으로 밀기 때문에 번호 공백은 수동 삭제로만 생긴다. 앞과 꼬리가 닿으면 통과한다
    (`test_issue104_c4_missing_middle_generation_is_a_documented_limit` 가 고정). 위 고아 규칙은 since 가 **파일 경계** 사흘 넘는 공백 안에 있을 때만 앞쪽을 끊는다. 판정 JSON 의 `sources` 에 읽은 파일 목록이 남으니 의심되면 거기서 세대 번호를 확인한다
  - **손상된(끝이 잘린) 회전본 (issue #142, 고침)**: `server.log.N.gz` 하나가 끝이 잘렸거나 깨지면(gzip 헤더·본문·CRC 꼬리 안 절단, 0 바이트) 전에는 `EOFError` 로 judge·baseline
    전체가 traceback 으로 죽었다(`main` 은 `OSError`·`ValueError` 만 INVALID 로 바꾼다. 0 바이트는 조용히 빈 세대로 읽혔다). 이제 `read_sidecar_logs` 가 그 세대를 **읽히는 앞부분까지만** 쓰고
    반쯤 풀린 마지막 줄은 버린다(잘린 `elapsed_ms` 가 짧은 요청으로 집계되지 않게). 손상 사실은 파일 이름을 담은 사유
    `log-coverage: sidecar log server.log.N.gz is truncated or corrupt (EOFError) - only its readable first part was used` 로 남는다.
    읽는 세대(판정 JSON `sources` 에 있는 것)의 손상은 그 뒷부분이 집계에서 빠진 것이라 **커버리지 부족과 같게** 다룬다: PASS·HOLD·PENDING 은 INVALID, 재시도 소진·지표 악화
    ROLLBACK 은 그대로이고 사유에 `INVALID:log-coverage: ... truncated or corrupt` 가 병기된다. baseline 도 같은 사유로 거부한다. 고아 컷 바로 앞(경계) 세대가 손상이면 그 절단이
    파일 경계 공백을 만들어 정상 세대가 고아로 잘렸을 수 있어 `... - not read: dropped as an orphan ...` 사유를 남긴다(컷이 나면 앞 커버리지가 이미 부족이라 판정은 같다).
    그보다 오래된 고아의 손상은 판정 입력이 아니라 사유에 넣지 않는다. 줄이 하나도 안 읽히는 손상 세대(헤더 안 절단·0 바이트)는 시각이 없어
    고아 컷 판단에서 건너뛴다: 더 새로운 세대 경계에서 컷이 나면 함께 잘리고(컷 경계 세대가 아니라 사유 없음), 컷이 안 나면 읽는 쪽에 남아 사유가 붙는다. 어느 쪽이든
    고아였다면 체인이 since 에 닿지 않아 원래도 앞 커버리지 부족이라 판정은 같다. 보수적 한계(알려진 한계, 고치지 않음): since 보다 통째로 앞인 정상 세대가 손상돼도
    INVALID 다(잘린 뒷부분이 공백 뒤 첫 recall 의 idle·cold 분류 입력일 수 있다). 다음 세대가 since 에 닿는다면 손상 파일을 확인해 치우는 운영자 조치로 풀린다.
    회전(`rotate-logs.sh` 의 cp -> truncate -> gzip)과 judge 가 겹치면 아직 쓰는 중인 `.1.gz` 를 손상으로 읽어 INVALID 이고, 이 사유는 꼬리 사유가 아니라
    `scripts/scheduled_checks.py` 가 판정 시각 이후 종결 판단한다: `server.log.1.gz` 의 손상 사유만 남은 INVALID 는 일시 사유라 종결하지 않고, 같은 손상 사유가 서로 다른 날 2회 연속이면 영구 손상으로 보고 종결한다
    (회전 04:00, 점검 09:00 이라 잠자기 뒤 밀린 실행이 몰릴 때만 겹친다. issue #179. `.2.gz` 이상 세대 손상이나 다른 사유가 섞이면 첫 실행에서 종결). CRC·길이 꼬리만 손상(내용은 다 풀림)이나
    온전한 gzip 뒤 쓰레기 바이트도 `BadGzipFile` 이라 같은 손상으로 보아 INVALID 다. baseline 은 창 안 어느 세대의 손상이든(커버리지가 온전해도) 거부한다. 판정 문턱과 `RULES` 키·값은 바꾸지 않았다
  - **hindsight 앞 커버리지**: 사이드카 로그가 `since` 에 닿지 않으면 hindsight 로그는 첫 요청이 아니라 `since` 부터 덮어야 `measured` 다(이전에는 남은 첫 요청 기준이라
    앞이 빠진 hindsight 로그도 `measured`). 판정(INVALID·ROLLBACK)은 그대로이고 `retries.status` 보고만 바로잡힌다
  - **스케줄 INVALID**(`scripts/scheduled_checks.py`): 판정 시각(2026-10-06 01:15 KST) 이후 INVALID 도 종결이다 - 최종 알림 1회와 `canary-22.done`(내용 `INVALID`).
    `since` 가 고정이라 회전으로 사라진 앞·방식 불일치·기준선 무효는 스스로 풀리지 않고 매일 반복 알림만 만든다. 예외 둘: (1) 사유가 전부 꼬리 손실이면 다음 요청이
    로그에 찍힐 때 풀리므로 종결하지 않고 다음 날 다시 돈다. (2) 사유가 전부 `server.log.1.gz` 손상(쓰는 중인 회전본)이면 다음 실행에서 풀리므로 종결하지 않고 다시 돈다 - 단 같은 손상 사유가
    서로 다른 날 2회 연속이면 영구 손상으로 보고 종결한다(직전 사유는 `~/Library/Logs/hindsight-scheduled/canary-22.damaged` 에 날짜와 함께 남고, 정상 판정·다른 사유·종결 때 지워진다. issue #179). 판정 시각 전 INVALID 는 기존대로 🚨 알람만(마커 없음)
  - **종결 판정 잔여 한계 (issue #119 C5·C7) - 유지(고치지 않음)**. 근거: (1) 둘 다 알림이 나가는 경로라 조용한 실패가 아니다. (2) 운영자 조치로 해소된다 - 아래 절차.
    (3) 고치려면 "판정 시각 이후 N회 연속 같은 INVALID" 같은 **연속 횟수 상태 파일**과 임의의 N 이 필요한데(꼬리 사유 C5·C7 에 한해 이 판단은 유지한다. `.1.gz` 손상 사유만 issue #179 로 2회 상태 파일을 둔다), 이는 `canary-22.done` 마커 의미(첫 INVALID 종결)를 바꿔 진행 중인 카나리
    (판정 시각 2026-10-06 01:15 KST)의 종결 동작·최종 알림 시점을 바꾼다. 한계 자체를 `tests/test_scheduled_checks.py` 의 `test_c5_*`·`test_c7_*` 가 고정한다.
    - **C5 (꼬리만 낡은 INVALID)**: 사이드카 로그가 영구히 멈추면 판정 시각 이후 매일 "판정 INVALID"(최종 아님) 알림이 반복되고 마커가 생기지 않는다. 사이드카가 멈춘 것은 반복 알림이
      맞는 상황이다. 해소: 사이드카를 되살려 요청이 로그에 찍히면 다음 점검이 스스로 판정해 종결한다. 판정을 포기하고 끝내려면 마커를 직접 쓴다
      (`echo "<ISO 시각> INVALID" > ~/Library/Logs/hindsight-scheduled/canary-22.done`).
    - **C7 (꼬리 사유가 아닌 INVALID 첫 실행 종결)**: 판정 시각 이후 첫 실행에서 `log-coverage: ... starts at ...`(로그 디렉터리 일시 부재 포함)·방식 불일치·기준선 무효는 최종 알림 1회와
      `canary-22.done`(`INVALID`)로 끝나고 이후는 SKIP 이다. 로그 덤프 실패·judge 실행 실패(로그 디렉터리 읽기 오류 포함)는 마커를 쓰지 않으니 일시 오류는 이 경로로 종결되지 않는다.
      원인을 고친 뒤(로그 복구, 기준선 재생성 등) **재판정 절차: `~/Library/Logs/hindsight-scheduled/canary-22.done` 을 지우면 다음 점검(매일 09:00)이 다시 판정하고 새 결과로 마커를 쓴다**
      (`.1.gz` 손상으로 종결했다면 손상 파일을 치운 뒤 지운다. `canary-22.damaged` 가 남아 있으면 함께 지운다)
      (바로 보려면 `python3 scripts/scheduled_checks.py canary-22 --dry-run`, 마커·Discord 없음). 이미 있는 마커는 내용과 무관하게 SKIP 이며 이 이슈는 그 의미를 바꾸지 않는다
- 제외: `bench/fixtures/canary-exclude.txt`(KST, 벤치 3창 + #31 upstream 직결 창)와 겹친 recall. warm 에서는 첫 요청과 같은 chars 가 1시간 안에 다시 나온
  recall(합성 반복 부하)을 뺀다. 창을 60s -> 1h 로 늘리면 09-25 캠페인의 잔여 warm 이 78 -> 3 으로 줄고 운영일(09-28)은 26 -> 25,
  2h 도 같은 값이라 1h 로 고정했다
- 기준선 `baseline-20260929T011427.json`(09-15 01:14:27 ~ 09-29 01:14:27, 128x3, 배포 직전 hindsight 로그 `hindsight-20260929T011427.log`):
  - cold recall **127**(deep-cold **18**) - 첫 요청 p50 2.356s / p95 2.748s, recall 단위 p50 4.748s / **p95 5.548s**
  - 7.5s 초과 1건 / 127 = **0.008**. warm recall 131 - p50 4.593s / **p95 5.535s / p99 5.653s**
  - 제외 18(벤치 창), warm 반복 부하 2,239, overflow 176(cold 8), 비포화 조각 387(cold 51, 원래 비포화 recall 포함),
    못 붙인 데드라인 0. 사이드카 503 2 · 데드라인 2 · 큐 포화 0. warm 초과 0(참고)
  - (00:00 컷 사전 실행값: cold 125, cold p95 5.564s - 뒤 1시간 14분의 cold 2건이 더해졌다)
  - 재시도: hindsight 로그가 09-26 02:29 재생성 이후만 있어 `partial`(시도 1 이 2건, 소진 0)
- 사전 조사(14일 cold 약 135, deep-cold 18)와의 대조: 제외 전 cold 포화 recall 이 135(= 125 + 벤치 창 10), deep-cold 18 로 같다.
  이슈 초안 규칙(묶음 합 = 300)만으로는 109 였다 - 버스트 선두 20건이 빠졌다(코디네이터 결정으로 버스트 나누기 채택)
- `judge --since <24시간 전>`(배포 전 구간)은 `INVALID mode-mismatch`(128x3 46, 300x1 0), 종료 코드 4 로 거부한다.
  판정 종료 코드: PASS 0 / ROLLBACK 1 / HOLD 2 / PENDING 3 / INVALID 4

**B1 배포 기록 (#22, 2026-09-29)**: 위 배포 1~5단계대로 진행했다.
- 롤백 좌표 확보, 배포 전 verify-all 10/10 GREEN
- hindsight 로그 보존 + 기준선 고정(위 소절). 사이드카 plist `RERANK_MAX_TEXTS` 600, 재기동 7초(새 프로세스 env 확인)
- `.env.local` 에 `HINDSIGHT_API_RERANKER_TEI_BATCH_SIZE=300`, hindsight 재생성 healthy 25초, 게이트웨이 재기동.
  **카나리 시작 = 2026-09-29 01:15:19 KST**
- 확인: 컨테이너 env `TEI_BATCH_SIZE=300`, 배포 후 verify-all 10/10 GREEN, `rewrite_failures` 0 -> 0, `upstream_ok` true.
  사이드카 로그에서 verify recall 이 128 분할 없이 단일 요청(candidates=291·297)으로 들어오고 413·503 0건.
  upstream 직결 포화 recall 스모크 1회 = `candidates=300` **단일 요청** 4.22s(recall 4.51s)
- 배포 전후 verify-all 과 스모크 창은 `canary-exclude.txt` 에 넣었다. 직후 `judge --since "2026-09-29 01:15:19"` = PENDING(cold 0/20, 0/7일)
- 판정: 7일(10-06 01:15 이후)과 cold recall 20건 중 늦은 쪽에 `judge` 를 돌린다(판정 전 hindsight 로그를 먼저 파일로 뜬다).
  카나리 기간에는 벤치를 돌리지 않는다

**B1 카나리 판정·롤백 기록 (#22, 2026-10-06)**: 카나리 판정은 ROLLBACK, B1은 채택하지 않았다.
- 판정 2026-10-06 09:00 KST(`judge-20261006T090005.json`, 구간 2026-09-29 01:15:19 ~ 2026-10-06 09:00:06 KST, 제외 recall 52).
  배포 후(300x1, 후보) 대 배포 전 기준선(128x3). 기준선은 위 소절 값 그대로이고, 판정 JSON 원문 값과 반올림 값을 병기한다:
  - cold recall **81** 대 **127**(deep-cold 8 대 18)
  - cold 7.5s 초과 **3/81** 대 **1/127** = 초과율 `0.037037037037037035`(0.037) 대 `0.007874015748031496`(0.008)
  - cold 첫 요청 p50 4.7956s 대 2.3563s, recall 단위 cold p50 `4.7956`(4.796) 대 `4.7475000000000005`(4.748)s
  - cold 첫 요청 p95 5.7284s 대 2.7480899999999995s(2.748), recall 단위 cold p95 `5.7284`(5.728) 대 `5.547789999999998`(5.548)s.
    문턱 `5.547789999999998` x 1.1 = `6.102568999999998`(6.1026)s 안이라 cold p95 는 통과
  - warm recall 100 대 131 - p50 `4.16575`(4.166) 대 `4.5932`(4.593)s, p95 `5.529295`(5.529) 대 `5.53485`(5.535)s,
    **p99 `5.795471000000001`(5.795) 대 `5.653379999999999`(5.653)s**. warm 7.5s 초과는 양쪽 0
  - 사이드카 503 **4**(기준선 2) / 데드라인 **4**(기준선 2) / 큐 포화 0. cold 재시도 recall 3 대 1, 로그 시도 번호 분포 `{1: 3, 2: 1, 3: 0}`(시도 번호 1 경고 3건·2 경고 1건), 소진 0
- 판정 ROLLBACK, `reasons` 원문 2개(둘 다 걸렸다):
  - `exceed:rate=0.037037037037037035vs0.007874015748031496,count=3`
  - `warm-p99:5.795471000000001vs5.653379999999999`
- 롤백 실행 2026-10-06(운영자 승인, 상위 #140):
  - pre-flight 롤백 좌표 확보, verify-all 10 GREEN / 0 RED
  - hindsight 로그 보존 -> 재연결 공지
  - `.env.local`에서 `HINDSIGHT_API_RERANKER_TEI_BATCH_SIZE` 줄 제거, hindsight 재생성 healthy 약 28초, 게이트웨이 재기동.
    컨테이너 env 에 `TEI_BATCH_SIZE` 없음(기본 128). 사이드카 로그가 `candidates=300` 1요청에서 `128`, `128`, `44` 3요청으로 복귀
  - 사이드카 plist `RERANK_MAX_TEXTS` 600 -> 256(재기동, 새 프로세스 env `RERANK_MAX_TEXTS=256` 확인). 이후 128x3 요청이 거부 없이 처리
  - 사후 verify-all 1차 9 GREEN / 1 RED(⑥ restore-test, 일시 부하로 판정: 같은 설정 재실행 통과), gate6 단독 재실행 GREEN,
    2차 **10 GREEN / 0 RED**, `rewrite_failures` 0 -> 0, `upstream_ok` true
  - 이 창은 `canary-exclude.txt` 에 넣었다
- 현행 운영값: 클라이언트 배치 128(hindsight 기본, `HINDSIGHT_API_RERANKER_TEI_BATCH_SIZE` 미설정), 사이드카 `RERANK_MAX_TEXTS=256`
- **B1 불채택.**

## 후보 3 (B2): 사이드카 배치 16 (2026-10-06 dev 보류 -> 2026-10-08 런 3 재판정: dev PASS, holdout PASS)

사이드카 `RERANK_BATCH_SIZE` 32 -> 16(issue #84). upstream 이 사이드카로 보내는 요청 모양(128x3)은 그대로이고 사이드카 안의 forward 배치만 줄어든다.
점수는 bit 동일(dev 7쿼리 x 3회 사전 실측 -8%). 팔은 벤치 사이드카 하나의 주 포트(18781, 배치 32)와 보조 포트(18782, 배치 16)로 나눴다(위 "B2 run 준비").

**준비와 조건**
- 기준 배치: 운영 plist(읽기만) `RERANK_BATCH_SIZE=32`, `RERANK_MAX_TEXTS=256` -> base·ctrl = 18781:32, cand = 18782:16. 벤치 사이드카 데드라인·큐 override 120/120, `/info` 의 `batch_sizes` `{"18781":32,"18782":16}`
- 팔 ref: 세 팔 모두 `d1fd202`(워크트리 HEAD = origin/main. 로컬 main `38e302c` 는 뒤처져 쓰지 않았다). 백업: `backups/<벤치 백업>/pg0.tar.gz`(dev·holdout 같은 백업)
- 세트 s1~s3, 게이트웨이 g1~g3(18891~18893), `verify --set` 전부 pass(`sidecar_batch` match). 1차 기동 때 s1 이 내장 PostgreSQL `Instance already running (pid: 89)` 로 한 번 죽어
  `up --force` 로 다시 띄웠다(백업의 pg0 인스턴스 기록 pid 와 컨테이너 안 pid 가 우연히 겹친 것으로 본다 - s2·s3 는 같은 백업으로 정상)
- 운영 게이트웨이는 10-15 창 B(#156) 전까지 10-01 배포본이다. 운영 사이드카 plist·upstream env 는 바꾸지 않았다(keep-warm 만 닿았다)
- 워크트리에서 돌릴 때는 `secrets/`·`audit/` 를 메인 체크아웃으로 가리키는 심링크가 필요하다(`replica.py` 의 벤치 ACL 과 `runner.PROD_AUDIT` 가 REPO 상대 경로다.
  `audit/` 가 없으면 `prod_events` 가 조용히 빠져 운영 겹침 쌍을 못 뺀다). 하네스 수정은 이 이슈 범위 밖이다
- 워크트리에는 `.env.host.local` 이 없다(gitignore). 운영 LaunchAgent 접두가 설치기 기본값이 아니면 `replica.py` 를 돌리기 전에
  `HC_LAUNCHD_PREFIX` 를 export 한다 - 없으면 기본 접두 plist 를 찾다 멈춘다(#470)

**계획 대비 실제 단계** (벤치 창 KST: 14:58:53 ~ 15:27:26, 16:31:56 ~ 22:34:36)
- `xdev-b2-20261006`(dev, limit 없음 = 152쿼리, 3바퀴 계획): p0 은 호스트 스왑 압박(스왑 30.2/30.7GB, swapout 실측 42MB/s, 정지 문턱 3MB/s)으로 가드가 거의 계속
  멈춰 22분에 18호출만 하고 15:25:39 에 코디네이터 승인으로 SIGINT 중단. 벤치를 내리고(15:27, 잔존 0) 압박이 풀린 뒤(#147, swapout 10분 0) 재개. p0b(16:36~17:30)는
  152쿼리라 단계당 약 100분(정지 포함 분당 약 4.8호출), dev 9단계 약 15시간으로 운영 사이드카 노출이 너무 길어 257호출에서 SIGINT 중단. 두 디렉터리는 병합하지 않고 중단 증거로 남겼다
- **limit 40 결정**(코디네이터, 17:30): dev 는 holdout 전 선별 단계이고 과거 dev 교차(`xdev-b1`)도 `limit=40`(rounds 1), 위 자기검증도 dev 40쿼리다. run-phase 는 seed 를 넘기지 않아
  `run_phase` 기본 seed 13 이 고정이고 `load_corpus` 는 key 정렬 뒤 `Random(13).sample` 이라 단계마다 같은 40쿼리다(같은 인자 두 번 호출 비교로 확인). 계획 중간에 limit 을 바꾸면
  단계 간 쿼리 집합이 달라지므로 새 계획으로 다시 돌았다
- `xdev-b2-20261006b`(dev, `--limit 40`, 3바퀴 = 9단계 계획, 17:31:58 기록): 9/9 단계, 축소 없음(`plan.truncated` false). 단계당 약 26~49분, 17:31:58 ~ 22:32:30
- holdout: **미사용**(코디네이터 판정 B, 22:33). holdout run 디렉터리·계획 파일을 만들지 않았다 - holdout 1회 기회는 그대로 남아 있다

**판정**
- **dev `xdev-b2-20261006b`: INCONCLUSIVE(보류), exit 2** - `~/.local/share/hindsight-bench/b2-84/judge-dev.json`, `reasons` = `aa-invalid`, `cold-unmeasured:no-cold-input`, `warm_verdict` INCONCLUSIVE
  - 후보 수치: 속도 **0.9191 [0.9058, 0.9312]** improved(-8.1%), p95 0.9451 [0.9165, 0.965] ok, p99 0.937 [0.9234, 0.9643] ok(허용 폭 1%), 꼬리 초과 0
  - 정확도 identical 40/40 · 비용 0(205,251 토큰 동일) · n=35(단계 누락 5, 바퀴를 잃은 쿼리 32) · 오염 70/360(19.4%) · 운영 겹침 창 37
  - **보류 원인 1 - A/A 대조 무효**: ctrl-base 속도 CI [0.991, 1.0411], 초과분 0.0311 -> `aa-too-noisy`(대조가 문턱을 넘게 흔들리면 run 무효, B1 warm 재판정과 같은 형식 보류).
    A/A 꼬리 잡음 p95 0.0172, p99 0.0098. 원인으로 보는 것은 호스트 압박(가드 정지 99창, 오염 19.4%)이다 - 실측으로 가르지는 않았다
  - **보류 원인 2 - 판정기와 이슈 규칙의 충돌**: 이슈 #84 는 "B2 는 요청 모양을 바꾸지 않으므로 cold 입력 없이 warm 으로 판정"인데 `judge.py` 는 `--cold-report` 가 없으면
    `no-cold-input` 으로 합격을 보류로 내린다(`judge.py` 901행(#169 머지 뒤 기준. 2026-10-06 당시 885행), 위 "사용법"의 `--cold-report`·`--cold-unmeasured` 설명). 그래서 warm 이 깨끗해도 당시(2026-10-06) 판정기로는 B2 PASS(exit 0)가 나올 수 없었다(#169 의 `--cold-exempt` 로 해소, 아래 "런 3 재판정"). 판정기 수정은 이 이슈 범위 밖이다
- **판정: 보류(INCONCLUSIVE) - holdout 미사용.** A/A 대조가 무효인 같은 호스트 조건에서 holdout 1회를 쓰면 또 대조 무효로 끝날 가능성이 커서 쓰지 않았다.
  다음: 조용한 호스트에서 dev 교차를 다시 돌리고, 요청 모양을 바꾸지 않는 후보의 cold 면제 규칙을 판정기에 먼저 넣은 뒤(issue #169) holdout 1회
- 호스트 스왑 포화 원인: macOS `fseventsd` 힙 누수(RSS 2.1GB 관측, 코디네이터 확인 약 20GB)로 스왑이 30GB 가까이 찼다. B2 와 무관한 호스트 문제이고 이 이슈에서 고치지 않았다

**운영 영향** (`meta.prod_sidecar` / `meta.prod_sidecar_keepwarm`)
- dev 병합 meta: 503 0 · 데드라인 0 · 큐 포화 0 / keep-warm 자신 0 · 0 · 0 -> 차감 값 0(#21 의 6건 대비 0). 가드 정지 99창, 가드 abort 0
- keep-warm: dev 9단계 sent 311 · failed 0, 중단된 p0 sent 8 · failed 0, p0b sent 52 · failed 0
- 제외 줄: 병합 meta `keepwarm.exclude_lines` 9줄과 `keepwarm.unmerged`(p0·p0b) 2줄을 `bench/fixtures/canary-exclude.txt` 끝에 붙였다. 벤치 창 2줄(기동 ~ 내림 + 300초)도 함께 붙였다 - 아래 한계의 빈틈을 덮는다

**한계**
- keep-warm 제외 줄은 run-phase 가 도는 구간 + 300초다. 벤치 사이드카·세트만 떠 있고 단계가 돌지 않는 구간(기동·verify·reconf·내림)은 제외 줄에 없다
- 쿼리 원문은 이 문서에 쓰지 않는다. run 산출물은 `~/.local/share/hindsight-bench/b2-84/`(dev, `bench/.data` 심링크 대상)와 holdout 기본 경로에 있다

### 런 3 재판정 (2026-10-08, issue #84 / #201): dev PASS -> holdout 1회 PASS

위 10-06 판정의 보류 원인 2(판정기 충돌)는 #169(PR #182, `--cold-exempt`)로 풀렸고, 원인 1(A/A 무효)은 조용한 호스트에서 다시 돌려 확인했다.
절차는 이슈 #84 "갱신 (2026-10-07, 런 2 스펙 #172)" 절 순서 그대로다.

**준비와 조건**
- 시작 게이트(05:39 KST): `memory_pressure` 여유 72%, 다른 벤치·카나리 없음, 예상 종료 2026-10-14 24:00 KST 이전(최악 추정 10-09 01:00). 스왑 2GB 조건은 운영자 결정(2026-10-07)으로 폐기, 참고값 2680M
- 환경: macOS 27.0.1(26A434, 2026-10-07 재부팅). 판정은 같은 run 안의 팔 비교라 유효하나 10-06 run 의 절대 수치와 섞어 비교하지 않는다
- 팔 ref: 세 팔 모두 `395a98d`(런 3 시작 전 origin/main 고정. #187 이후 ref 로 만든 팔은 `policies/local/` 이 빠진다). 팔 정책 파일 sha256 `fee4533c...` 가 운영 `policies/bank-policies.json` 과 같았다
- 스모크(`xsmoke-b2-20261008`, `--limit 1` 한 단계, exit 0): 팔 게이트웨이 g1~g3 모두 `personal-demo` 등록, 태그 별칭 81개, `kind:done -> kind:completion` 교정 확인(컨테이너 안에서 정책 로더·`normalize_tags` 를 직접 호출, retain 없음)
- 기준 배치·포트·데드라인 override·백업(`backups/<벤치 백업>/pg0.tar.gz`, dev·holdout 같은 백업)은 위 10-06 run 과 같다. 세트 s1~s3·게이트웨이 g1~g3 `verify --set` 전부 pass(`sidecar_batch` match)
- 운영 게이트웨이는 10-15 창 B(#156) 전까지 10-01 배포본이다. 운영 사이드카 plist·upstream env·`.env.local` 은 바꾸지 않았다(keep-warm 만 닿았다)
- `bench/.data` 는 10-06 run 과 같은 `~/.local/share/hindsight-bench/b2-84/dev-data` 를 가리켰다(메인 체크아웃 `bench/.data` 와 dev.jsonl·manifest·cap-profile sha256 동일)

**단계** (벤치 창 KST: 05:40:28 기동 ~ 11:53:19 내림)
- 기존 run 재판정(참고, 새 run 없음): `xdev-b2-20261006b` 를 `--cold-exempt` 로 다시 판정 -> INCONCLUSIVE exit 2, `reasons` = `aa-invalid`, `cold-exempt:<사유>`. 예상대로 `no-cold-input` 만 면제 기록으로 바뀌고 A/A 무효는 남았다
- dev `xdev-b2-20261008`(`--limit 40`, seed 13 고정, 3바퀴 9단계, 05:51:00 ~ 08:12:14, 단계당 약 15~16분): p0 단독 판정의 `speed.contamination_rate` 0.0(중단 문턱 0.10). 9/9 단계, 축소 없음
- holdout `xho-b2-20261008`(68쿼리 전체, 3바퀴 9단계, 08:17:24 ~ 11:50:17, 단계당 약 23~25분): 1회, 중간 중단 없이 끝까지. 9/9 단계, 축소 없음

**판정** (둘 다 `--baseline base --candidate cand --aa-control ctrl --cold-exempt "<요청 모양 불변>"`)

| run | 판정 JSON | exit | 속도(기하평균 비, CI95) | p95 / p99 | 정확도 | 비용 | 오염 | A/A ctrl CI · problems |
|---|---|---|---|---|---|---|---|---|
| dev `xdev-b2-20261008` | `~/.local/share/hindsight-bench/b2-84/run3/judge-dev.json` | 0 (**PASS**) | **0.9404 [0.9355, 0.9452]** improved | 0.9543 ok / 0.9669 ok | identical 40/40 | 0 (205,251 토큰 동일) | 1/360 (0.0028) | [0.9969, 1.0058] · `[]` |
| holdout `xho-b2-20261008` | `~/.local/share/hindsight-bench/b2-84/run3/judge-holdout.json` | 0 (**PASS**) | **0.9335 [0.9278, 0.9395]** improved | 0.916 ok / 0.9482 ok | identical 68/68 | 0 (314,783 토큰 동일) | 2/612 (0.0033) | [0.9934, 1.0] · `[]` |

- 표본: 꼬리 n(p95·p99) dev 39 · holdout 67, 단계 누락 0(`incomplete_phase_queries`), 바퀴를 잃은 쿼리 dev 1 · holdout 2(`laps.queries_with_dropped_laps`)
- 두 판정 모두 `warm_verdict` PASS, `cold.verdict` exempt, `reasons` 는 `cold-exempt:<사유>` 하나, 꼬리 초과 쿼리 0, `plan.truncated` false
- holdout 중앙값 4719.09ms -> 4440.35ms(-5.9%). 10-06 사전 실측(-8%)보다 작지만 같은 방향이다
- **판정: B2 PASS(holdout).** 운영 배포는 이 이슈 범위 밖이라 별도 이슈로 한다. 배포 시점(#156 묶음 vs 별도 창)은 운영자가 정한다

**운영 영향** (`meta.prod_sidecar` / `meta.prod_sidecar_keepwarm`)
- dev·holdout 병합 meta 모두 503 0 · 데드라인 0 · 큐 포화 0 / keep-warm 자신 0 · 0 · 0. 가드 정지 dev 5창 · holdout 8창 · 스모크 2창(전부 swapout, `guard.windows` 창 길이 dev 41~72초 · holdout 41~44초 · 스모크 40초, 그중 대기 `waited_s` 30.1~60.2초), 가드 abort 0
- keep-warm: dev 9단계 sent 256 · failed 0, holdout 9단계 sent 387 · failed 0, 스모크 sent 11 · failed 0
- 제외 줄: 벤치 창 1줄(기동 ~ 내림 + 300초), 스모크 단계 meta 의 keep-warm 1줄, dev·holdout 병합 meta `keepwarm.exclude_lines` 18줄을 `bench/fixtures/canary-exclude.txt` 끝에 붙였다
- dev 병합 meta 의 `keepwarm.unmerged` 에는 이번 run 과 무관한 10-06 중단 단계(`xdev-b2-20261006-p0`, `-p0b`) 2줄이 다시 나왔다(같은 runs 디렉터리의 병합 안 된 keep-warm 기록을 줍는 동작). 그 줄은 10-06 기록 때 이미 붙어 있어 다시 붙이지 않았다

## 후보 4 (배치 150): upstream TEI 배치 128 -> 150 (2026-10-11, issue #149: dev 보류(A/A 무효) - holdout 미사용)

upstream `HINDSIGHT_API_RERANKER_TEI_BATCH_SIZE` 128 -> 150. 포화 recall 300후보가 사이드카로 가는 요청 모양이 128x3 -> 150x2 로 바뀐다.
요청 모양이 바뀌는 후보라 cold 면제(#169) 대상이 아니고, 로컬 cold 재생은 B1 때 스왑으로 잴 수 없었으므로 `--cold-unmeasured` 로 판정하고 `warm_verdict` 를 결과로 쓴다.
기준은 B2 반영 뒤 운영 모양(사이드카 배치 16)이다. 절차는 위 "세트 + 순환 교차"와 "후보 3 (B2)"의 "런 3 재판정" 그대로이고, 다른 점만 적는다.

**절차 차이**
- 벤치 사이드카 **단일 포트**: `sidecar-up shared --port 18781 --env RERANK_BATCH_SIZE=16 --env RERANK_MAX_TEXTS=300 --env RERANK_TOTAL_DEADLINE_SECONDS=120 --env RERANK_QUEUE_TIMEOUT_SECONDS=120`.
  보조 포트가 없으므로 `up --sidecar-batch-size`·`record-plan --arm-sidecar`·`run-phase --plan-dir` 를 주지 않았다(보조 포트 없는 `/info` 에는 배치 필드가 없다). 세트는 `up --force` 로 새로 만들었다
- 팔 설정 `--configs '{"cand":{"HINDSIGHT_API_RERANKER_TEI_BATCH_SIZE":"150"}}'`, base·ctrl 는 운영과 같은 128(설정 없음)
- 인터프리터: 새 워크트리에 `.venv` 가 없어 `uv run --python 3.14 --no-project python -B ...`(run-phase 는 `--with tiktoken`)
- **#187 이후 ref + 운영 정책 split(`--local-policies`)**: 팔 ref 는 #225 머지(`0376059`)를 포함해야 해서 B2 런 3처럼 #187 이전 ref 를 쓸 수 없다. #187 이후 ref 의 추적 `policies/bank-policies.json` 은 템플릿이라 운영과 다르고,
  메인 체크아웃(`38e302c`, #187 이전)에는 `policies/local/` 이 없다. 그래서 메인 `policies/`(읽기만)를 `scripts/policy-split.py split` 으로 리포 밖 scratchpad 에 나눠 그 로컬분을 `gw-up --local-policies` 로 주었다.
  `policy-split.py check`(메인 원본 단일 로드 == 워크트리 추적 + 로컬 병합) `POLICY_SPLIT_CHECK_OK`(뱅크 5, 트리거 1). split 산출물은 벤치 정리 때 지웠다

**준비와 조건**
- 시작 게이트: `git merge-base --is-ancestor 0376059 HEAD` exit 0. `memory_pressure` 여유 00:59 42% · 01:00 47%(미달) -> 10분 뒤 재확인 01:11 60%(통과). `docker ps -a --filter name=hc-bench`·`lsof -nP -iTCP:18780-18799 -sTCP:LISTEN` 빈 출력, 다른 벤치·갱신 replica 없음(#229·#200 BLOCKED)
- 예상 종료: 시작 때 dev 9단계 + holdout 9단계 최악 약 7시간(10-11 오전) -> p0 실측 17.6분으로 다시 추정해 dev 03:55 · holdout 08:30 KST 무렵, 2026-10-14 24:00 KST 이전
- 팔 ref: 세 팔 모두 `0376059`(워크트리 HEAD = origin/main). 백업 `<메인>/backups/<벤치 백업>/pg0.tar.gz`(절대 경로로 줌), 기동 전 존재 확인 155,987,611 바이트, sha256 `<sha256 생략>`(세트 state 의 `backup_sha256` 과 같음)
- 팔 `local_policies`: g1~g3 모두 `source` flag · `result` 복사 · `files` = `bank-policies.json` `56936eb8...`, `context-triggers.json` `a184016e...`(split 산출물 해시와 같음). 메인 `policies/bank-policies.json` sha256 `fee4533c...`(운영, B2 런 3과 같음).
  팔 컨테이너 안 정책 로더(추적 + 로컬 병합)가 운영 뱅크를 싣는 것을 g1~g3 모두 확인했다(retain 없음)
- 세트 s1~s3, 게이트웨이 g1~g3(18891~18893), `verify --set` 전부 pass. `bench/.data` 는 B2 런 3과 같은 `~/.local/share/hindsight-bench/b2-84/dev-data`(메인 체크아웃 `bench/.data` 와 dev.jsonl·manifest·cap-profile sha256 동일)
- 운영 게이트웨이는 10-15 창 B(#156) 전까지 10-01 배포본이다. 운영 사이드카 plist·upstream env·`.env.local`·운영 컨테이너는 바꾸지 않았다(keep-warm 만 닿았다)

**단계** (벤치 창 KST: 01:11:54 기동 ~ 03:39:17 내림 확인, `bench-end.txt`)
- dev `xdev-b150-20261011`(`--limit 40`, seed 13 고정, 3바퀴 9단계, 계획 01:13:45 기록, 01:13:45 ~ 03:36:58, 단계당 약 15~18분): 9/9 단계, 축소 없음. p0 단독 판정(참고)의 `speed.contamination_rate` 0.10 - judge 상한 0.2 안이지만 런 3 중단 문턱(0.10)과는 같은 값이다(이번 run 은 중단하지 않았다)
- holdout: **미사용**. holdout run 디렉터리·계획 파일을 만들지 않았다 - holdout 1회 기회는 그대로 남아 있다

**판정** (`--baseline base --candidate cand --aa-control ctrl --cold-unmeasured "<요청 모양 변경, 로컬 cold 재생 불가(B1 스왑) - 운영 카나리로 판정>"`)

| run | 판정 JSON | exit | 속도(기하평균 비, CI95) | p95 / p99 | 정확도 | 비용 | 오염 | A/A ctrl CI · problems |
|---|---|---|---|---|---|---|---|---|
| dev `xdev-b150-20261011` | `~/.local/share/hindsight-bench/b150-149/judge-dev.json` | 2 (**INCONCLUSIVE**) | 0.9846 [0.9796, 0.9895] (`speed.verdict` improved) | 0.983 ok / 0.9491 ok | identical 40/40 | 0 (205,251 토큰 동일) | 57/360 (0.1583) | [0.9964, 1.0245] · `aa-too-noisy:excess=0.0145` |

- `warm_verdict` INCONCLUSIVE, `cold.verdict` unmeasured, `reasons` = `aa-invalid`, `cold-unmeasured:<사유>`. 꼬리 초과 쿼리 0, `plan.truncated` false
- 표본: n 40(꼬리 n 39), 단계 누락 0(`incomplete_phase_queries`), 바퀴를 잃은 쿼리 39/40(`laps.queries_with_dropped_laps`) - 오염 쌍이 쿼리 전반에 흩어졌다
- 중앙값 base 4457.97ms · cand 4466.47ms(거의 같다). 기하평균 개선 1.5% 는 A/A 초과분 1.45% 와 같은 크기다
- **holdout 미사용 사유(조건 해석 포함)**: 이슈 본문의 holdout 조건 "dev PASS 또는 warm improved/ok" 는 `speed.verdict` 단독이 아니라 `warm_verdict` 기준으로 읽는다(코디네이터 판정).
  `warm_verdict` 는 A/A 무효로 INCONCLUSIVE 이고, 문턱을 A/A 초과분 e=0.0145 만큼 좁히면 개선 문턱 0.99-e=0.9755 를 CI 상단 0.9895 가 넘어 improved 도 성립하지 않는다.
  같은 계산에서 불악화 문턱 1.01-e=0.9955 는 CI 상단이 밑돌아 non-inferior(ok)는 성립한다 - 본문 괄호 조건을 속도 수치로만 읽으면 ok 로 볼 여지가 있지만, A/A 가 무효인 run 의 수치는 판정 근거가 아니라서 `warm_verdict` 기준으로 읽었다.
  같은 호스트 조건에서 holdout 1회를 쓰면 다시 A/A 무효로 끝날 가능성이 커서 쓰지 않았다(B2 10-06 판정 B 선례)
- **판정: 배치 150 warm 판정 INCONCLUSIVE(dev, A/A 무효) - holdout 미사용, 배포·카나리 초안 없음.**
- 오염 원인: 원인별 집계는 하지 않았다. 후보는 호스트 메모리 압박(단계 중 `memory_pressure` 여유 37~48% - 단계 중 수동 관측이라 산출물 없음, 게이트 시점 값만 `gate.txt`)과 운영 겹침 창(`speed.prod_windows` 16) 둘 다다
- 다음 런 재시도 제안: 단계 중 메모리 여유 50% 이상이 유지되고 운영 트래픽 겹침이 적은 시간대에 dev 교차를 다시 돌려 A/A 가 유효해진 뒤 holdout 1회(이번 run 은 가드 정지 6창, 오염 15.8%)

**운영 영향** (`meta.prod_sidecar` / `meta.prod_sidecar_keepwarm`)
- dev 병합 meta: 503 1 · 데드라인 1 · 큐 포화 0 / keep-warm 자신 503 1 · 데드라인 1 · 0 -> 차감 값 0. 두 건 모두 p0 첫 keep-warm 요청(01:14:18 KST, `rerank_queue_timeout` 503) 하나다
- 가드 정지 6창(전부 swapout, `guard.windows` 창 길이 40~102초, 그중 대기 `waited_s` 30.1~90.3초), 가드 abort 0
- keep-warm: dev 9단계 sent 257 · failed 1(위 p0 첫 요청)
- 제외 줄: 벤치 창 1줄(기동 ~ 내림 확인 + 300초), 병합 meta `keepwarm.exclude_lines` 9줄을 `bench/fixtures/canary-exclude.txt` 끝에 붙였다. `keepwarm.unmerged` 는 B2 런 3과 같은 10-06 중단 단계 2줄이라 다시 붙이지 않았다

**정리** (2026-10-11 03:39:16 KST 확인): `replica.py down` exit 0 뒤 `docker ps -a --filter name=hc-bench` 빈 출력, `lsof -nP -iTCP:18780-18799 -sTCP:LISTEN` 빈 출력(exit 1), 벤치 사이드카 PID 없음,
워크트리 심링크 `secrets`·`audit`·`bench/.data` 와 split 산출물 디렉터리 삭제. 운영 기준선(메인 HEAD `38e302c`, `policies/` 파일 해시, `.env.local` 해시, LaunchAgent plist 해시, 운영 컨테이너 3개 ID·StartedAt) 시작 기록과 같다.
run 산출물(드라이버·판정 JSON·게이트·정리 기록)은 `~/.local/share/hindsight-bench/b150-149/`, run 디렉터리는 `~/.local/share/hindsight-bench/b2-84/dev-data/runs/xdev-b150-20261011*` 에 있다. 쿼리 원문은 이 문서에 쓰지 않는다

## 갱신 replica 모드 (issue #229, 상위 #200)

멘탈 모델 품질 A/B 용 **갱신 전용** 복제본. recall 복제본의 동결 계약(`HS_OVERRIDES`·`hs_env`·`up`/`reconf`/`verify`)은 그대로이고,
이름(`hc-bench-refresh-<arm>`, 볼륨 `hc-bench-refresh-<arm>-pg0`)과 state(`state/refresh-<arm>.json`)가 따로다.
구현은 `bench/refresh.py`, 명령은 `replica.py refresh-up / refresh-run / refresh-down`.

- **provider 하나, 폴백 없음**: `claude-code` 또는 `xai-oauth`만 받는다. 운영에서 상속된 `^HINDSIGHT_API_([A-Z0-9]+_)*LLM_` 키 전부
  (STRATEGY·번호 멤버·작업별 체인·timeout 같은 튜닝 키 포함)와 `CLAUDE_CODE_OAUTH_TOKEN`·`ANTHROPIC_API_KEY`·`XAI_*`를 지우고,
  `LLM_PROVIDER`·`LLM_MODEL`·`LLM_TRACE_ENABLED=true`(+ claude-code 는 `LLM_API_KEY`)만 남긴다. 지운 키 **이름**은 state·meta 의 `removed_llm_keys`.
- **풀리는 키**: `WORKER_ENABLED=true`, `LLM_PROVIDER`·`LLM_MODEL`, `LLM_TRACE_ENABLED=true` 뿐. consolidation 끔·주기 0·`WORKER_ID=hc-bench`·벤치 MCP 키·
  `PYTHONHASHSEED`·`HF_HUB_OFFLINE`·`HC_*` 제거는 recall 복제본과 같다. 리랭커는 벤치 사이드카(`--sidecar-port`, 8787 거부)로만 간다.
- **복원된 작업 정리**: 워커를 끈 채 복원 기동 -> pg0 에서 모든 뱅크의 `async_operations` pending·processing 을 세고
  `status='cancelled'` 로 표시 -> 0건 재확인 -> 워커를 켜고 재기동. 0건이 안 되면 실패(팔마다 데이터가 갈라지면 같은 백업 전제가 깨진다).
  비밀번호는 컨테이너 안에서 `instance.json` 으로 읽어 psql env 로만 넘긴다. 워커 claim 은 `async_operations` 하나만 본다(고정 이미지 정적 확인).
- **직렬·poisoned**: 한 복제본에서 갱신은 한 번에 하나(state 파일 `flock`, 겹치면 `busy`). refresh 제출 뒤 timeout·응답 유실·폴링 통신 오류면
  state 를 `poisoned` 로 표시하고 컨테이너를 `docker stop` 한다. poisoned 복제본은 더 갱신하지 않는다 - `refresh-down` 뒤 새 백업 복원부터.
- **provider 관측**: 갱신이 끝나면 `GET .../llm-requests?start_date=<시작>&end_date=<끝>` 을 페이지(limit 500) 끝까지 모은다. 창 경계는 복제본
  컨테이너 시계(start 포함·end 제외). 지정 provider·model 의 success 가 1건 이상이고 다른 provider·model 호출이 0건이어야 성공.
  0건·혼입은 실패, 엔드포인트 무응답만 `unknown`(이것도 0 아닌 종료). 요청값으로 관측값을 채우지 않는다.
- **토큰**: claude-code 는 `--token-env-file <KEY=VALUE 파일> --token-key <KEY>`. 값은 docker 자식 프로세스 env 의 `HINDSIGHT_API_LLM_API_KEY` 로만
  가고 argv 에는 `-e HINDSIGHT_API_LLM_API_KEY` 이름만 있다. env 파일·state·meta·stdout 에는 값이 없다.
- **운영 hsauth 금지**: xai-oauth 는 `--xai-auth <볼륨 이름 | 절대 디렉터리>` 로 **별도 로그인한** 저장소만 마운트한다. `hsauth`·`<project>_hsauth`·
  운영 컨테이너의 `/home/hindsight/.hindsight` 마운트 소스는 거부한다(refresh token 이 회전해 운영 grok 인증이 끊긴다).

준비:

```bash
# 1) CLI (메인 체크아웃에서 - linux-arm64 바이너리라 호스트에서 실행하지 않는다. 버전은 옆의 VERSION 스탬프 >= 2.1.280 으로 판정)
bash scripts/fetch-claude-cli.sh
# 2) 토큰 파일: KEY=VALUE 줄(키 이름은 <토큰 키>). 값을 명령줄에 쓰지 않는다
# 3) xai-oauth 대조군: 운영과 별개의 볼륨/디렉터리에 사람이 따로 로그인해 둔다(범위 밖 사람 작업)
# 4) 벤치 사이드카
.venv/bin/python -B bench/replica.py sidecar-up rf --port 18781
# 5) 백업: 팔끼리 같은 파일을 쓴다(meta 에 경로·sha256)
```

실행:

```bash
.venv/bin/python -B bench/replica.py refresh-up cc --backup backups/<ts>/pg0.tar.gz --provider claude-code \
  --model claude-opus-5-5 --sidecar-port 18781 --token-env-file <토큰 파일> --token-key <토큰 키> \
  [--claude-cli <메인 체크아웃>/vendor/claude-cli/claude]
.venv/bin/python -B bench/replica.py refresh-up gx --backup backups/<ts>/pg0.tar.gz --provider xai-oauth \
  --model grok-4.6 --sidecar-port 18781 --xai-auth <별도 볼륨 또는 /절대/디렉터리>
.venv/bin/python -B bench/replica.py refresh-run cc --model-id <모델 id> [--model-id ...] --out ~/.local/share/hindsight-bench/refresh/cc
```

- `refresh-run` 은 모델마다 존재 확인 -> 남은 pending·processing 0건 확인 -> `clear` -> `refresh` -> operation terminal 대기(`--timeout`, 기본 900초)
  -> 본문 확인. 첫 실패에서 멈추고 0 아닌 종료다. `--out` 은 실행 리포와 메인 체크아웃 **밖**이어야 한다.
- 산출물(모델별): `<id>.md`(본문), `<id>.meta.json` - backup 경로·sha256, 이미지 ID·`RepoDigests`, 요청 provider·model, operation id,
  시작·끝·소요(복제본 시계), 본문 sha256·길이, 취소한 pending 수, 지운 LLM 키 이름, 관측(provider·model·호출 수·success·실패·다른 provider 호출 수).
  실패해도 `<id>.meta.json` 에 `status: failed` 와 `reason` 이 남는다.
- 내림: `refresh-down <arm>` 이 갱신 컨테이너·볼륨·state 를 지운다. **기존 `down` 은 갱신 복제본을 지우지 않는다** - 따로 내린다.
  호출자의 xai 인증 저장소(볼륨·디렉터리)는 갱신이 만든 것이 아니므로 지우지 않는다. 확인은 `docker ps -a --filter name=hc-bench`.
- 이 모드의 실제 기동·LLM 갱신은 #200 본체가 #149 벤치 뒤 단독으로 한다. #229 는 단위 테스트(`tests/test_bench_refresh.py`, 가짜 docker·가짜 API)까지다.
- 알려진 한계(final-review 20261013-042720):
  - 갱신 복제본이 떠 있는 동안 기존 `down`·`sidecar-down` 을 돌리면 갱신이 쓰는 벤치 사이드카도 내려간다. 갱신 중에는 recall 벤치 명령을 쓰지 않는다.
  - `--timeout` 이 0 이하여도 받아 첫 폴링에서 poisoned 가 된다(테스트가 timeout 경로를 이렇게 일으킨다). 양수만 준다.
  - 처리되지 않은 SIGTERM 으로 끊기면 poisoned 표시가 남지 않는다(Ctrl-C 는 표시한다). 그때는 `refresh-down` 뒤 다시 복원한다.
  - 첫 실제 실행(#200) 전에 확인: 복원본 `instance.json` 키(`installation_dir`·`version`·`database`)와 psql 경로, `async_operations` 의 스키마
    (현재 SQL 은 스키마 미지정), 강제 종료 뒤 cancelled UPDATE 가 남는지(워커를 켠 뒤 0건 재확인).

## 상태 회귀 세트 평가 도구 (issue #287, 상위 #284 P0-3)

"이미 끝난 일이 열린 일로 나온다"를 잰다. 구현은 `bench/arch_v2_regression.py`(표준 라이브러리만), 테스트는 `tests/test_arch_v2_regression.py`.

- **정답표**: 운영 체크아웃의 `bench/.data/regression-arch-v2.json`(gitignore, 오케스트레이터 배치, 읽기 전용). `--answer-key`로 경로만 넘긴다.
  리포에 복사하지 않는다. 리포 산출물(`artifacts/arch-v2/p0-3-*.md`)에는 항목 id와 판정만 쓴다.
- **출력**: 기본 `~/.local/share/hindsight-bench/arch-v2/`(0700, `--out`으로 바꾼다). 질의·응답 원문과 근거 요지는 여기에만 남는다.
  `evidence-<시각>/`(항목별 `<id>.json` + `summary.json`), `score-<시각>/`(`detail.json` 원문, `metrics.json` 경로별 질의 수·소요·LLM 호출, `summary.json` id·경로·판정).
- **대상**: 기본은 hc-next 사본이다(#242 계약). 사본 env 없이 부르면 exit 1. 정본 채점은 D10 복원 사본에서만 한다.
- **고정값**: 모든 질의에 정답표 `query_instruction`을 넣는다. recall(유일하게 시각 인자를 받는 도구)에는 `query_timestamp=2026-10-28T14:59:59Z`를 늘 보낸다.

```bash
KEY="$(cd "$(git rev-parse --git-common-dir)/.." && pwd)/bench/.data/regression-arch-v2.json"
next_env=$(bash scripts/next-stack.sh env) || exit 1; eval "$next_env"
# 0) 사본 상태(문서·관찰 수·pending_consolidation). --wait 초 동안 pending_consolidation 0 을 기다린다(못 채우면 exit 2)
python3 -B -m bench.arch_v2_regression stats --wait 3600
# 1) evidence: 완료 항목마다 keyword 별 recall -> 근거 = keyword 가 들어 있고, 완료를 말하고, mentioned_at < ignore_mentioned_at_from
#    (mentioned_at 이 없는 기억은 사본에서만 근거로 친다 - D10 recall 행의 일부가 null 이다. 운영 대상에서는 rejected.undated)
python3 -B -m bench.arch_v2_regression evidence --answer-key "$KEY"
# 2) score: 세 경로(a reflect 나열, b 프로젝트별 지식 페이지 생성, c 항목별 recall). 근거 없는 항목은 evidence 요약을 보고 뺀다
python3 -B -m bench.arch_v2_regression score --answer-key "$KEY" --evidence ~/.local/share/hindsight-bench/arch-v2/evidence-<시각>/summary.json
# 2b) 저장된 원문(detail.json)을 다시 판정(질의 0회). --evidence 를 주면 그 근거 표로 세트를 좁힌다
python3 -B -m bench.arch_v2_regression rescore --answer-key "$KEY" --detail <score detail.json> [--evidence <evidence summary.json>]
# 3) 리포 산출물 표(id·판정만)
python3 -B -m bench.arch_v2_regression render evidence --answer-key "$KEY" --evidence <evidence summary.json>
python3 -B -m bench.arch_v2_regression render dryrun --answer-key "$KEY" --score <score summary.json>
```

- 뱅크는 `--bank`, 없으면 `HC_NEXT_REQUIRED_BANK`, 그것도 없으면 `.env.host.local`의 `HC_PERSONAL_BANK`(`HC_PROD_ROOT` 기준).
  게이트웨이 토큰은 `HC_SECRETS_DIR/acl.json`에서 `--owner`(기본 `HC_OWNER`) identity 의 것을, upstream API 키는 `HC_ENV_FILE`의 `HC_HINDSIGHT_API_KEY`를 읽는다.
  키는 `docker exec -i` 의 stdin JSON 으로만 넘기고 출력하지 않는다.
- (a)(c)는 게이트웨이 MCP(`reflect` `include_based_on: true`, `recall`), (b)는 게이트웨이에 지식베이스 도구가 없어 컨테이너 안 upstream 직결 REST
  (`POST .../knowledge-base/pages` -> `GET .../operations/<id>` 완료 대기 -> `GET .../mental-models/<id>?detail=full`의 본문과 `reflect_response.based_on`).
  페이지 이름은 `arch-v2-status-<project_hint | all>`, 프로젝트 페이지는 `tags: [project:<hint>]`, trigger 는 기본값(delta, 첫 생성은 full).
  쓰기 대상에서 이 run 이 만들 이름의 페이지가 이미 있으면 다시 POST 하지 않고, 어떤 질의(reflect 포함)도 보내기 전에 exit 1 로 멈춘다
  (기존 페이지를 재사용하지 않는 까닭: 이전 run 의 질의·생성 시점으로 만든 페이지라 이번 run 의 측정이 아니다). 같은 원문을 다시 판정하려면 `rescore`를 쓰고, 다시 재려면 새 사본에서 한다.
- LLM 호출 수는 경로마다 `GET .../llm-requests?start_date&end_date`의 `total`(응답이 없으면 null).

채점 규칙(결정적. 같은 입력이면 같은 판정, 테스트로 고정):

| 경로 | open | not-open | invalid |
|---|---|---|---|
| a, b (절 규칙) | 응답의 `## OPEN` 절(제목이 정확히 OPEN(괄호 덧붙임 허용)인 제목 줄부터 같은 수준 이상 제목 줄 전까지, `###` 하위 제목은 절 안) 줄의 주어(첫 굵은 글씨 구간 - 바로 뒤에 설명 구분자 `:`·`. `·` - `·em dash·`(`·`,`·`|`가 없으면 첫 구분자까지 이어 본다, 표면 첫 칸, 둘 다 없으면 줄 전체)에 항목 keyword 가 있다 | OPEN 절 줄 주어에 없다(서두·DONE 절·설명 안 언급은 세지 않는다) | OPEN 제목이 없다(`format-no-open-heading`), (b) 페이지가 없다(`page-missing`) |
| c (최신 상태 규칙) | keyword 가 든 결과 중 이 항목 상태를 말하는 기억(done/open) 가운데 mentioned_at 이 가장 늦은 것(같으면 순위가 앞선 것)이 open | 그것이 done, 또는 상태를 말하는 기억이 없다(`no-status`) | - |
| 공통 | | | 근거(based_on·결과 기억)에 mentioned_at >= `ignore_mentioned_at_from` 이 하나라도 있다(`post-cutoff-evidence`, 그 응답으로 채점한 항목 전부). 근거 목록이 없거나 비었다(based_on 이 `{}`·directives 뿐, `no-provenance`, (a)(b)). 운영 대상에서 날짜 없는 근거(based_on 의 멘탈 모델, mentioned_at 이 없는 recall 행)가 있다(`undated-evidence`) |

- keyword 매칭: 대소문자 무시, 가장자리가 영숫자면 바깥에 영숫자가 붙지 않아야 한다(`#2`가 `#250`에, `#4`가 `#42`에 안 걸린다. 한글 가장자리는 조사 때문에 경계 없음).
  채점에서는 project_hint 와 같은 keyword(정규화 비교)를 다른 keyword 가 남을 때 뺀다. 맨 번호 keyword(`#3`)는 같은 글(줄 주어·기억 본문)에
  프로젝트 이름이 있어야 센다((b) 프로젝트 페이지는 이미 그 범위라 예외). evidence 는 keyword 전부를 쓰고 맨 번호 규칙만 적용한다.
- 항목 상태(`item_state`, (c)와 evidence): keyword 가 든 문장(`. ! ?`·줄바꿈·` - `로 나눈다)의 단서가 먼저다. 완료 단서 완료·끝났·종결·머지·merged·closed·done·completed·해결,
  열림 단서 남은·남음·아직·대기·예정·보류·미착수·open·pending·todo, 부정형(완료되지 않음 등)은 열림. 한 문장에 둘 다 있으면 그 문장은 상태가 아니고,
  여러 문장이 말하면 마지막 문장을 따른다. 단서가 없으면 태그·라벨(`classify_memory`): done = `kind:completion`(또는 `tag_aliases`가 그것으로 바꾸는 옛 kind)이나
  라벨의 완료·종료·머지·배포·마감 등, open = `kind:handoff`·`kind:todo`나 라벨의 인계·할 일 등. 본문을 먼저 보는 까닭: 관찰은 범위 태그(`kind:` 포함)를 물려받아
  태그가 본문과 어긋난다(D10 실측: `kind:completion` 관찰이 "남은 작업"을, `kind:handoff` 기억이 "배포 완료"를 말한다).
- 날짜 없는 기억 정책(`undated_ok`): mentioned_at 이 없는 기억(based_on 의 멘탈 모델 항목, D10 recall 행의 일부 - K04 keyword recall 약 50행 중 21행 실측)은
  사본(HC_TARGET=next)에서만 근거·provenance 로 받고, 운영 대상에서는 근거에서 빼고(evidence `rejected.undated`) 그 응답 판정을 invalid(`undated-evidence`)로 둔다.
  근거: D10 스냅숏(2026-10-27 04:30 KST)이 `ignore_mentioned_at_from`(2026-10-28T15:00:00Z)보다 앞서므로 사본 뱅크의 모든 기억과 그 파생물(멘탈 모델·페이지)은
  기준 시각 전 기억에서만 나온다 - 날짜가 없어도 기준 이후일 수 없다. 운영 뱅크에는 기준 뒤 기억이 있어 날짜 없는 근거를 증명할 수 없다(fail-closed).
  날짜 없는 근거는 날짜 비교에서 가장 오래된 것으로 친다((c) 최신 상태 규칙).
- 규칙 선택(경쟁 가설, 합성 사례는 테스트에 있다):
  - 기각 H1 전체 본문 키워드: "K 는 10-16 에 끝났다" 같은 서두·DONE 절 언급을 open 으로 센다(`test_sections_ignore_mentions_outside_open`).
  - 기각 H2 구조화 출력(`response_schema`): 사용자가 읽는 본문이 아니라 별도 추출 단계를 채점하고, 질의마다 LLM 추출이 더 들며,
    (b)에서는 페이지 trigger 설정을 바꾼다. 절 규칙은 본문을 그대로 채점한다.
  - (c) 기각 순위 규칙(1위 결과가 결정): 옛 인계가 1위, 새 완료가 2위면 open 으로 틀린다(`newer-done`).
    기각 존재 규칙(완료 기억이 하나라도 보이면 not-open): 완료 뒤 다시 열린 대조군 모양을 not-open 으로 틀린다(`reopened`).
  - v1 -> v2(D10 첫 실행 응답으로 고침, `rescore`로 같은 원문을 다시 판정): 하위 제목이 OPEN 절을 끝내 (a) 19건 전부 not-open,
    줄 전체 부분 문자열이 번호 접두어·설명 안 언급·프로젝트 이름 keyword 에 걸림, 태그 우선 분류가 범위 태그를 물려받은 관찰에 속음.
    각 사례는 `test_open_section_keeps_nested_subheadings`, `test_keyword_boundaries`, `test_open_line_subject_ignores_context_mentions`,
    `test_project_name_keyword_dropped_when_others_remain`, `test_bare_number_keyword_needs_project_context`, `test_item_state_text_first`.
  - 알려진 한계: 완료 뒤 다른 일을 인계하며 끝난 항목 keyword 를 함께 쓴 기억이 가장 늦으면 (c)는 open 으로 센다.
    keyword 를 바꿔 쓴(의역) OPEN 줄은 (a)(b)에서 못 잡는다(not-open 쪽 오차) - 대조군 open 수로 경로의 감도를 본다.
    정답표 keyword 가 넓으면(다른 일의 파일 이름과 같은 말) 다른 일의 완료·열림이 섞인다 - 정답표 keyword 를 좁히는 것이 처방이다.

운영 연막 모드(1단계 운영 창의 연막 채점용, 정본 아님):

```bash
HC_ENV_FILE="<운영 체크아웃>/.env.local" HC_SECRETS_DIR="<운영 체크아웃>/secrets" HC_PROD_ROOT="<운영 체크아웃>" \
  python3 -B -m bench.arch_v2_regression score --allow-prod --read-only --answer-key "$KEY" --evidence <D10 evidence summary.json>
```

- `--allow-prod --read-only` 둘 다 있어야 운영(127.0.0.1:8888, `hindsight-crew-hindsight-1`)을 받는다. 하나만 있으면 exit 1.
  `HC_TARGET`이 비어 있는데 사본 모양(`:18888`, `hc-next*`)이면 exit 1.
- (b)는 이미 있는 `arch-v2-status-*` 페이지를 트리에서 찾아 읽기만 한다(없으면 `page-missing`). `--create-pages`(쓰기 경로)를 주면 요청 전에 exit 1이고,
  upstream 클라이언트도 읽기 전용 대상에서 GET 이 아닌 요청을 거부한다(이중 가드). reflect·recall 은 운영 LLM(구독)과 사이드카를 쓰므로 그 구간을 카나리 제외에 적는다.
- 운영 뱅크에는 기준일 뒤 재실측 기억이 정답을 담고 있어 경로가 저절로 통과할 수 있다. 그래서 based_on 날짜 검사가 그 응답을 `invalid`로 돌린다.

### (b) 페이지 이름·읽기 전용 모드와 delta 갱신 (issue #297)

1단계 arm 은 (b)를 두 결과로 낸다(스펙 #296): **delta** = 재통합 전에 만든 페이지(접미사 `-pre`)가 재통합 뒤 delta 로 갱신된 결과,
**full** = 재통합이 끝난 뒤 새 이름(접미사 없음)으로 만든 페이지의 첫 생성 결과. 같은 사본에 두 이름이 공존한다.

- `--page-suffix <s>`: 페이지 이름 `arch-v2-status-<hint|all><s>`. 생성과 읽기 전용 채점에 함께 쓴다. 쓰기 대상의 이름 충돌 검사도 접미사를 붙인 이름으로 한다
  (`-pre` 가 있으면 `-pre` 생성만 막힌다). 값이 `-`로 시작해도 `--page-suffix -pre` 그대로 쓴다.
- `--read-pages`: 사본에서도 페이지를 만들지 않고 이미 있는 페이지를 읽기만 한다(POST 0회, LLM 0회). detail 에 페이지 이름과 `last_refreshed_at`을 남긴다.
  `--create-pages`와 같이 주면 exit 1.
- `--page-map <json>`: 읽기 전용 채점에서 힌트별 실제 페이지 이름(`{"<hint>": "<이름>", "-": "<전체 뱅크 페이지 이름>"}`). 운영 연막 채점처럼 이름이 다른 페이지를 읽을 때 쓴다.
  표에 없는 힌트는 기본 이름으로 짐작하지 않고 `page-missing`이다. 생성 모드나 `--page-suffix`와 함께 주면 exit 1.
- `stats --wait <초> --wait-pages`: `pending_consolidation` 0 뒤에 **stale 페이지 0개 + 진행 중(`pending`·`processing`) `refresh_mental_model` 작업 0개**까지 기다린다.
  이 조건이 **연속 2회 poll** 에서 같아야 끝난다(통합 완료와 stale 표시 사이에 poll 이 끼면 한 번은 완료처럼 보인다). operations 는 페이지를 넘겨 전부 본다.
  진행 줄을 찍고 마지막 줄이 판정이다(못 채우면 exit 2). `--wait` 없이 주면 argparse 오류다. 읽기만 한다.
- (b) 근거 날짜: 페이지 `based_on` 항목의 날짜는 `GET .../memories/{id}`로 채운다. 404(재통합으로 지워진 관찰)는 날짜 검사에서 빼고 페이지별 `deleted_evidence`로 센다
  (score `summary.json`의 `deleted_evidence` 합계). 그 밖의 조회 오류는 그 페이지 판정이 invalid(`date-lookup-failed`)다. **arm 보고서에 delta·full 결과마다 `deleted_evidence` 합계를 적는다.**

1단계 arm 의 (b) 순서(사본 하나 안에서):

```bash
# 1) 재통합 전: -pre 페이지 생성(full 첫 생성, 기본 trigger = delta + refresh_after_consolidation). (a)(c)·full 은 여기서 재지 않는다
python3 -B -m bench.arch_v2_regression score --answer-key "$KEY" --evidence ~/.local/share/hindsight-bench/arch-v2/r1-fixed-set.json --paths b --page-suffix -pre
# 2) arm 설정 적용·문서 PATCH(재통합) - 각 arm 티켓의 도구
# 3) 통합 drain + 페이지 delta 갱신 대기
python3 -B -m bench.arch_v2_regression stats --wait 14400 --wait-pages
# 4) delta 결과: -pre 페이지를 읽기만 해서 채점
python3 -B -m bench.arch_v2_regression score --answer-key "$KEY" --evidence ~/.local/share/hindsight-bench/arch-v2/r1-fixed-set.json --paths b --read-pages --page-suffix -pre
# 5) full 결과와 (a)(c): 접미사 없는 새 페이지 생성 + reflect + recall
python3 -B -m bench.arch_v2_regression score --answer-key "$KEY" --evidence ~/.local/share/hindsight-bench/arch-v2/r1-fixed-set.json
```

갱신 조건과 대기 판정(2026-10-30 D10 사본 실측, `artifacts/arch-v2/r1-a-baseline.md`):
- 문서 3개의 `observation_scopes`를 PATCH 하자 그 문서들의 관찰 12개가 바로 지워지고(`pending_consolidation` 3) 통합이 48초 만에 끝났다.
  통합이 끝난 같은 초에 **범위(페이지 tags, all_strict)에 새 관찰이 생긴 페이지만** `is_stale=true`가 됐고 `refresh_mental_model` 작업이 바로 생겼다(7개 중 2개).
  두 작업은 delta 모드(`mental_model_delta_ops` LLM 호출)로 약 1분 40초 뒤 끝났고 `is_stale`이 false 로 돌아왔다.
- 범위가 조용한 페이지는 갱신되지 않는다(`last_refreshed_at` 그대로). 그래서 "모든 `-pre` 페이지의 갱신 시각이 바뀔 때까지"로 기다리면 끝나지 않는다.
  판정은 위 `--wait-pages` 조건이다. `last_memory_seen_at`은 갱신된 페이지만 통합 시각으로 올라간다(갱신 확인용).
- 대기 상한: 재통합 1회 상한(4시간)과 같게 둔다. 실패한 refresh 는 `last_refresh_failed_at`이 남고 자동 갱신이 멈춘다(upstream 문서) - 그때는 트리에서 그 값을 보고 보고서에 적는다.

날짜 규칙과 재통합(같은 실측):
- 재통합으로 다시 만든 관찰의 `mentioned_at`은 근거 원문 사실의 `mentioned_at`(관찰의 source facts 중 가장 늦은 것)이다. 재통합 시각은 `updated_at`에만 찍힌다.
  recall 결과 행의 `mentioned_at`도 같다(다시 만든 관찰 5개를 직접 recall 해 확인). 그래서 기준 시각 규칙은 재통합한 arm 에서도 그대로 쓴다(도구 변경 없음).
- 페이지(멘탈 모델) `reflect_response.based_on`의 관찰 항목에는 `mentioned_at`이 없다(id·type·text·context 뿐). (b)의 근거 날짜 검사는 사본에서는 날짜 없음 허용으로 통과하고,
  운영 대상에서는 늘 `undated-evidence`로 invalid 였다. 그래서 (b)는 날짜 없는 근거 항목의 id 로 `GET .../memories/{id}`(읽기만)를 조회해 `mentioned_at`을 채운 뒤
  날짜 검사를 한다(조회 1건 = 근거 항목 1건). 조회가 실패하면 사본·운영 모두 그 페이지 판정은 invalid(`date-lookup-failed`, fail-closed)다.

### 세트 두 개, (a) 판정 불가 run, arm 판정 (issue #323, 런 2)

런 2(스펙 #318)는 세트 A(런 1 정답표)와 세트 B(#323)를 **합친 세트**로 채점한다. 경로마다 한 번 질의하고 두 세트가 같은 응답을 나눠 쓴다.

- 입력(전부 리포 밖, 읽기 전용): 세트 A 정답표 `<운영 체크아웃>/bench/.data/regression-arch-v2.json`·고정 세트 `r1-fixed-set.json`,
  세트 B 정답표 `r2-setb-answer-key.json`·고정 세트 `r2-setb-fixed-set.json`, 키 유무 표 `r2-key-split.json`(`{"A": {"<id>": true|false|null}, "B": {...}}`).
  세 파일의 sha256 정본은 `artifacts/arch-v2/r2-t4-setb-baseline.md`다.
- `--answer-key`·`--evidence`를 세트마다 한 번씩 준다. 정답표가 둘 이상이면 `--evidence` 수가 같아야 하고(아니면 argparse 오류), 같은 id 의 `status`가 evidence 파일끼리 다르면 exit 1. 세트 이름은 `--set-name`(기본 `--answer-key` 순서대로 A, B). 합칠 때 exit 1 인 경우:
  두 정답표의 `query_date`·`ignore_mentioned_at_from`·`query_instruction` 가운데 하나라도 다르다, 끝난 항목 id 가 세트끼리 겹친다(세트 B 새 항목은 `B01`~, 새 대조군은 `BC01`~),
  같은 id 대조군의 내용이 다르다(공유 대조군 C01~C03 은 세트 A 항목의 바이트 복사).
- 합친 세트는 같은 id 를 한 번만 센다. (a) reflect 1회, (b) 두 세트 project_hint **합집합** 페이지를 한 번씩 생성, (c) 항목마다 recall 1회.
- `--key-split <json>` 또는 세트 둘 이상이면 `summary.json`에 두 블록이 붙는다(없으면 summary 모양은 런 1 그대로).
  `breakdown`: 경로 -> 범위(`all`, `set:A`, `set:B`, `key:true|false|null`, `set:A/key:true` 등) -> `closed_total`·`closed_open`·`controls_total`·`controls_open`·`invalid`.
  `a_run`: 이 (a) run 의 `closed_open`·`controls_open`·`status`(`valid` / `undecidable` = 대조군 open 0 / `invalid`)·`decidable`.
- `--share-pages`(생성 모드만): 같은 이름 페이지가 이미 있고 그 멘탈 모델 `source_query`가 이번 질의와 같으면 만들지 않고 읽어 채점한다(detail 에 `reused: true`).
  다르거나 같은 이름이 둘 이상이면 어떤 질의도 보내기 전에 exit 1. 옵션이 없으면 지금처럼 같은 이름이 있으면 멈춘다.
  합친 세트 채점은 합집합 페이지를 한 번에 만들어 이 옵션이 필요 없다. 세트를 따로 채점하다 같은 사본에서 다른 세트를 더 잴 때 쓴다.

**세트별 접미사 규칙**: 채점 전에 두 정답표의 세 값이 같은지 확인한다.

```bash
python3 -B -c 'import json,sys; a,b=(json.load(open(p)) for p in sys.argv[1:]); print({k: json.dumps(a[k])==json.dumps(b[k]) for k in ("query_date","ignore_mentioned_at_from","query_instruction")})' "$KA" "$KB"
```

- 셋 다 `True`면 페이지를 공유한다(합친 세트 채점, 접미사는 아래 기본값).
- 하나라도 `False`면 합칠 수 없다(도구가 exit 1). 페이지를 공유하지 않고 세트마다 따로 채점하며 세트별 접미사를 단다: 세트 A `-sa`, 세트 B `-sb`,
  처치 전 페이지 `-pre-sa`·`-pre-sb`, 기준선 두 번째 생성 `-g2-sa`·`-g2-sb`. 이때 (a) 응답 공유가 안 되므로 채점 전에 코디네이터에게 묻는다.

**접미사 기본값**: 처치 전 페이지 `-pre`, 처치 뒤 full(첫 생성) 접미사 없음, **기준선 두 번째 full 생성 `-g2`**(같은 사본, 첫 생성은 접미사 없음).

**측정 상태 하나의 채점**(기준선 사본 또는 처치 반복의 사본, `next_env` 뒤 워크트리에서. 처치 arm 은 위 #297 순서의 `-pre`·delta 단계가 앞에 붙는다):

```bash
A2=(--answer-key "$KA" --answer-key "$KB" --evidence "$FA" --evidence "$FB" --key-split "$KS")
# (a) 유효 run 3개: 한 번에 한 run. 매 run 뒤 a_run.status 를 본다. valid 3개가 모이면 멈춘다. 시도 상한 6회.
#     undecidable(대조군 open 0)은 집계에서 빼고 다음 run 을 돈다. invalid 가 나오면 그 상태의 (a)는 판정 불가로 확정이라 멈춘다.
python3 -B -m bench.arch_v2_regression score "${A2[@]}" --paths a
jq -c .a_run <score-*/summary.json>
# (b) full: 처치 arm 은 접미사 없이 1회. 기준선은 2회(두 번째 -g2)
python3 -B -m bench.arch_v2_regression score "${A2[@]}" --paths b
python3 -B -m bench.arch_v2_regression score "${A2[@]}" --paths b --page-suffix -g2      # 기준선만
# (c) 1회(결정적)
python3 -B -m bench.arch_v2_regression score "${A2[@]}" --paths c
# 다시 판정(질의 0회, 두 정답표와 키 표를 받아 score 와 같은 summary): #326 이 머지 뒤 모든 detail 을 이 명령으로 다시 집계한다
python3 -B -m bench.arch_v2_regression rescore --answer-key "$KA" --answer-key "$KB" --key-split "$KS" --detail <score-*/detail.json>
```

- #323 기준선 v1.1.0(2026-10-31)은 위 명령을 그대로 썼다. (a)는 아래 반복으로 돌렸다(시도 3회, 유효 3개, `artifacts/arch-v2/r2-t4-setb-baseline.md`):

```bash
valid=0
for k in 1 2 3 4 5 6; do
  out=$(python3 -B -m bench.arch_v2_regression score "${A2[@]}" --paths a) || exit 1
  st=$(jq -r .a_run.status "$out"); echo "attempt $k $out $(jq -c .a_run "$out")"
  [ "$st" = valid ] && valid=$((valid+1)); [ "$st" = invalid ] && break; [ $valid -ge 3 ] && break
done
```

- 기준선 순서: (a) run 들을 먼저(사본에 지식 페이지가 없는 상태, 런 1 기준선과 같은 순서), 그다음 (b) 두 생성, 마지막 (c). 경로마다 출력 디렉터리 `score-<시각>`과
  summary.json·detail.json sha256 을 산출물에 한 줄씩 적는다(judge-arms manifest 입력).

**judge-arms manifest**(`judge-arms --manifest <json>`, 상대 경로는 manifest 파일 기준, 질의 0회):

```json
{
  "answer_keys": {"A": "<세트 A 정답표>", "B": "<세트 B 정답표>"},
  "key_split": "<r2-key-split.json>",
  "arms": {
    "baseline-v1.1.0": {"role": "baseline", "repeats": [
      {"state": "completed", "a": ["<run1 summary>", "<run2>", "..."], "b-full": ["<생성1 summary>", "<-g2 summary>"], "c": ["<summary>"]}]},
    "baseline-main": {"role": "baseline", "repeats": [{"state": "completed", "a": ["..."], "b-full": ["...", "..."], "c": ["..."]}]},
    "B": {"role": "treatment", "baseline": "baseline-v1.1.0", "repeats": [
      {"state": "completed", "a": ["..."], "b-delta": ["<-pre 읽기 summary>"], "b-full": ["<summary>"], "c": ["<summary>"]},
      {"state": "aborted"}]}
  },
  "read_path_only": {"arm": "baseline-main", "baseline": "baseline-v1.1.0"}
}
```

- `state`는 `completed`·`aborted`(상한 중단)·`invalid`(무효). 완료 반복만 센다. `a`는 시도 순서 그대로 전부(undecidable·invalid run 포함) 적는다.
- 모든 summary 의 `set`이 같아야 한다(다르면 exit 1). 같은 (파일, 경로 키)가 manifest 전체에서 두 번 나오면 exit 1(경로는 realpath 로 비교, 한 summary 를 다른 경로 키에 함께 쓰는 것은 허용). summary 는 판정만 읽고(`paths`), 세트·키·대조군 구분은 manifest 의 정답표·키 표로 다시 낸다.
- 출력: 처치 arm 과 "읽기 경로만"마다 판정 한 줄, 경로별 표(반복 값·기준선 값·W·대조군 open·판정 불가 사유), 범위별(세트 A·B·합친 값, 키 있음·없음·미정) 끝난 항목 open,
  처치 상태별 (a) 대조군 0 run 수, 입력 summary sha256. `<out>/judge-<시각>/judgement.json`에 같은 내용과 manifest sha256 이 남는다.

판정 산식(스펙 #318 "arm 판정"과 같다. 테스트 `test_judge_arms_*`로 고정):

| 항목 | 규칙 |
|---|---|
| 값(경로별) | 합친 세트 끝난 항목 open 수. (a) 앞 6개 시도 가운데 앞에서부터 유효 run 3개의 평균, (b) `b-full`(기준선은 두 생성 평균), (c) 1회 값. `b-delta`는 표에만 싣는다 |
| 유효 run | invalid 0 이고 대조군 open 1건 이상. 대조군 open 0 run 은 집계에서 빠진다. 앞 6개 안에 3개가 없거나 invalid run 이 있으면 그 상태의 (a)는 판정 불가 |
| W(경로별) | max(\|v1 - v2\|, 기준선 반복 차). 기준선 반복 차 = (a) 유효 run 3개의 최대 - 최소, (b) 두 생성 값 차, (c) 0 |
| 합격 | 판정 불가 경로 없음, 세 경로·두 반복의 모든 sample((a)는 유효 run 3개 전부)에서 끝난 항목 open 0, 대조군 전부 열림, invalid 0 |
| 개선 | 합격 아님. 2개 이상 경로에서 v1·v2 모두 < 기준선 값 - W, 나머지 경로는 v1·v2 모두 <= 기준선 값 + W, 대조군 open 값이 경로마다 두 반복 모두 기준선 이상 |
| 판정 불가 경로 1개 | 합격 불가. 개선은 남은 두 경로가 모두 "- W"를 채울 때만 |
| 효과 없음 | 그 밖 |
| 판정 불가 | 완료 반복 1회(처치 arm), 또는 판정 불가 경로 2개 이상. 경로 판정 불가 = invalid, (a) 유효 run 부족, (b) 기준선 생성 부족, 기준선 값 판정 불가 |
| 개선 막힘 | 처치 arm 의 (a)가 대조군 0 run 때문에 유효 run 3개를 못 채우면 개선이 될 수 없다(처치가 대조군을 닫았을 수 있다) |
| 미측정 | 완료 반복 0회 |
| 읽기 경로만 | 기준선 main 을 arm, 기준선 v1.1.0 을 그 기준선으로 같은 개선 규칙. 반복 대신 기준선 main 의 (a) 유효 run·(b) 두 생성 범위를 W 에 넣고, 반복 1회 규칙은 쓰지 않는다 |

### 판정 v2 (런 3, issue #338)

v1(위 규칙)을 그대로 두고 `--judge v2`일 때만 summary 에 `v2` 블록을 더한다. 옵션이 없거나 `--judge v1`이면 summary·reasons 는 이전 도구와 같다
(`tests/fixtures/arch-v2-judge/golden-v1-*.json`, 77bca59 도구로 만든 골든. 런 2 rescore summary 54개도 v1 부분이 바이트 단위로 같았다).
지표·라벨·동결 문턱의 정본은 리포 밖 사전 등록 1부(`prereg.md`, sha256 `bd60ac85...`, #336 댓글)다.

```bash
python3 -B -m bench.arch_v2_regression rescore --judge v2 --answer-key "$KA" --answer-key "$KB" --key-split "$KS" --detail <score-*/detail.json>
python3 -B -m bench.arch_v2_regression judge-pairs --judge v2 --manifest <manifest.json> [--null <null.json>]
```

**항목 판정**: 모든 경로가 `open` / `done` / `unknown` / `invalid`. 근거·언급 없음은 `unknown`이고 not-open 계열로 합치지 않는다.
결과·응답에 항목 keyword 가 아예 없으면 `unknown`에 하위 표시 `absent`를 단다(R3-2 의 (d)도 이 매핑을 쓴다). `invalid` 사유는 v1 과 같다.

**(a)(b) 절 규칙 v2** (`judge_sections_v2`, `v2_section_lines`, `v2_line_hit`, `v2_strip_parens`, `v2_live_hits`):
- OPEN 절 줄의 주어에 살아 있는 keyword 언급이 있으면 `open`, 아니고 DONE 절(제목이 정확히 DONE) 줄의 주어에 있으면 `done`, 둘 다 아니면 `unknown`.
- 주어 = v1 `line_subject`에서 괄호 설명을 뺀 것. 괄호 안이 그 항목 별칭뿐이면(`(<repo> #68)`, `(PR #280)`) 남긴다.
- 살아 있는 언급에서 빼는 것: 따옴표 안, 선후 참조 자리(왼쪽 after·before·until·following·since(the), post-·pre-, 오른쪽 "(한 낱말) 전·뒤·후·이후·직후·직전"),
  맨 번호(`#N`) 바로 앞에 정답표가 아는 다른 프로젝트 이름이 붙은 것(`v2_known_names`).
- 맨 번호는 같은 줄, 조상 줄(들여쓰기가 얕은 앞 줄)·바로 위 굵은 묶음 줄(굵은 글씨만 있는 줄 또는 `:`로 끝나는 머리 문단), OPEN 하위 제목 중 하나에 project_hint 가 있으면 센다. (b) 프로젝트 페이지는 늘 센다. 묶음 줄의 범위는 다음 하위 제목·다음 묶음 줄·들여쓰기 없는 일반 문단에서 끝난다.
- 조상 줄(묶음 줄 포함)의 주어에 다른 이슈 번호가 있으면(이 항목 자신의 맨 번호가 아니면) 그 하위 줄은 이 항목 언급이 아니다. 맥락 상속과 이 제외는 함께 넣었다(맥락만 넣으면 참조 줄 오탐이 는다, 진단 diag-ab 5절).

**(c) 최신 상태 규칙 v2** (`judge_recall_v2`, `v2_item_state`):
- 상태 진술 = keyword 의 살아 있는 언급이 든 문장의 완료·열림 단서(v1 단서 정규식). 완료 기록(태그·라벨 done)의 열림 단서 문장은 후속 문장이라 세지 않는다.
  단서 문장이 없으면 keyword 가 기억 첫 문장(제목 줄)에 있을 때만 태그·라벨을 쓴다. 그 밖은 상태 진술이 아니다.
- 상태 진술 중 mentioned_at 이 가장 늦은 것(같으면 순위). 날짜 있는 상태 진술이 하나라도 있으면 날짜 없는 것은 고르지 않는다. 모두 날짜가 없으면
  상태가 하나로 같을 때만 그 상태, 엇갈리면 `unknown`(`undated-conflict`). `occurred_start` 대체는 쓰지 않는다(diag-c 8절).
- 상태 진술이 없으면 `unknown`(`no-status`), keyword 가 든 결과가 없으면 `unknown` + `absent`.

**summary `v2` 블록** (`v2_block`): `paths`(경로 -> 항목 id -> 판정), `absent`(경로 -> id 목록), `counts`(경로 -> `closed`·`controls`·`partial` -> total·open·done·unknown·absent·invalid).
rescore 는 사유를 `reasons-v2.json`에 남긴다. score 도 `--judge v2`를 받는다.
`rescore --inject-evidence <json>`(issue #342, 원인 층 분리 진단 전용, 판정 입력 아님): `{"<id>": [기억 행]}`을 (c)는 그 항목 결과 끝에, (d)는 결과 집합 끝에 더해(이미 있는 id 는 빼고) 동결 판정 함수로 다시 판정하고 summary 에 `inject`(sha256·더한/이미 있던 id) 블록을 단다. (a)(b)와 원본 detail 은 그대로다.

**partial 범주**: 정답표 선택 필드 `partial`(끝난 항목과 같은 항목 모양 목록, 없으면 빈 목록). `all_items`·`active_items`(근거 확인 대상 아님, 늘 세트에 든다)·
`merge_answer_keys`(끝난 항목처럼 세트끼리 id 가 겹치면 exit 1)에 들어가 질의·재판정 대상이 되고, v1 판정·집계에서만 빠진다(`v1_items`). v2 집계와 judge-pairs 는 따로 센다.

**경로 등록표** `V2_REGISTRY`(`V2Path(name, short, kind, runs, judge)`, `register_v2_path`): `a_reflect_list`(a, list, valid3), `b_knowledge_page_status`(b, list, all),
`c_recall_status`(c, item, all). 집계(`judge_detail_v2`)·judge-pairs·표 출력이 이 표를 돈다. manifest 경로 키는 `short` 그 자체 또는 `<short>-<변형>`(`b-full`, `b-delta`).
새 경로는 판정 함수와 함께 등록만 한다(테스트 `test_fake_path_d_reaches_summary_pairs_and_table`가 가짜 `d`로 확인).

**judge-pairs** (`judge_pairs`, `pair_repeat_state`, `pair_baseline`, `pair_transitions`, `pair_gain`, `render_pairs`): manifest 모양은 judge-arms 와 같고 summary 의 `v2` 블록을 읽는다
(없으면 exit 1). 모든 summary 의 `set`이 같아야 하고 같은 (파일, 경로 키)가 두 번 나오면 exit 1(judge-arms 와 같다). 처치 arm 과 `read_path_only`마다, manifest 반복에 있는 등록 경로 키마다(어느 반복에 그 키가 없으면 빼지 않고 `missing` 판정 불가로 싣는다):
- 반복 안: valid3((a))는 앞 6시도 중 invalid 0·대조군 open 1건 이상 run 을 앞에서부터 3개, all 은 모든 생성. 쓴 run 의 항목별 다수결(open 이 절반 이상이면 open, 동률 open. 아니면 done 이 unknown 보다 많으면 done, 아니면 unknown). (a) 유효 run 이 3개 못 되면 그 반복은 판정 불가(`valid-runs-short`).
- 기준선: 반복을 전부 쓴다. 모든 반복에서 open 이면 open, 모두 not-open 이면 not-open, 섞이면 불안정(이득에서 빼고 수를 따로 낸다), 한 반복이라도 invalid 면 invalid.
- 처치는 반복마다 따로 기준선과 짝짓는다. 범주별 전이: `open_to_done`·`open_to_unknown`·`open_kept`·`notopen_to_open`·`notopen_kept`·`unstable`·`invalid`. 대조군 손실 = 대조군 open_to_done + open_to_unknown.
- 이득: 항목 경로(kind item)는 끝난 항목 open -> done, 목록 경로(kind list)는 open -> done + open -> unknown 이되 대조군 손실이 영분포 이하일 때만(넘으면 이득 0·효과 False, 사유 `control-loss>null`). 영분포가 없으면 어느 경로든 이득을 정하지 않는다(`no-null`). not-open -> open 은 이득에서 빼고 따로 싣는다. 효과 = 이득 > 영분포, 경로 효과 = 처치 반복 2개 이상 모두 효과(한 반복이라도 판정 불가면 판정 불가).
- `--null <json>` 모양: `{"a": 2, "b-full": 1, "c": 1}` (manifest 경로 키 -> 영분포 값, 0 이상 수). 계산식은 사전 등록 1부 1.5.
- 출력: arm 마다 경로 x 반복 표와 입력 summary sha256, `<out>/pairs-<시각>/pairs.json`. 경로는 리포 상대 또는 `~` 접두(`display_path`, judge-arms 입력 목록도 같다).

**동결 대상**(R3-1 머지 뒤 바꾸지 않는다): 상수 `V2_VERDICTS`·`V2_REF_LEFT`·`V2_REF_RIGHT`·`V2_QUOTED`·`V2_PAREN`·`V2_ISSUE_NO`·`V2_ALIAS_FILLER`·`V2_QUALIFIER`·`V2_BULLET`·`V2_GROUP`·`PAIR_FIELDS`,
함수 `v2_known_names`·`v2_live_hits`·`v2_strip_parens`·`v2_section_lines`·`v2_line_hit`·`judge_sections_v2`·`v2_item_state`·`judge_recall_v2`·`_v2_judge_a`·`_v2_judge_b`·`_v2_judge_c`·`judge_detail_v2`·`v2_block`·`pair_path_of`·`pair_repeat_state`·`pair_baseline`·`pair_transitions`·`pair_gain`·`judge_pairs`,
그리고 이들이 부르는 v1 함수(`line_subject`·`item_matches`·`item_keywords`·`classify_memory`·`contamination`·`iter_facts`와 단서 정규식). 확장 지점: `register_v2_path`로 경로를 더하는 것과 그 경로의 질의 단계.
확인: `git diff <R3-1 머지 sha> HEAD -- bench/arch_v2_regression.py`에서 위 본문에 변경이 없다.

**맞춤·검증 결과**(최종 코드로 런 2 detail 55개 rescore, 질의 0회. 라벨 = 진단 diag-ab·diag-c 판독, 양성 = T·누락 T·P·대조군 open 줄, 음성 = M·(c) 33건. 라벨 표·수치 원본은 리포 밖 `r3-1-labels.json`):

| 세트 | 정의 | 행(양/음) | TP | FP | FN | TN | 정밀도 | 재현율 |
|---|---|---|---|---|---|---|---|---|
| 맞춤 | 기준선 v1.1.0 x 세트 A 전용 항목 | 8 / 6 | 8 | 1 | 0 | 5 | 0.889 | 1.000 |
| **검증** | 세트 B 항목(공유 대조군 포함) 또는 기준선 main | 156 / 22 | 153 | 6 | 3 | 16 | **0.962** | **0.981** |
| 검증, 대조군 줄 제외 | 위에서 대조군 open 줄 141행을 뺀 끝난 항목 행 | 15 / 22 | 12 | 6 | 3 | 16 | 0.667 | 0.800 |
| 검증, (c)만 | (c) 음성 9행 | 0 / 9 | 0 | 4 | 0 | 5 | - | - |
| 그 밖(참고) | arm 반복의 세트 A 전용 항목 | 27 / 30 | 19 | 0 | 8 | 30 | 1.000 | 0.704 |
| 검증, 열람 run 제외 | 검증에서 설계 전에 읽은 run 1개(기준선 main (a) 첫 run)를 뺀 행 | 150 / 22 | 147 | 6 | 3 | 16 | 0.961 | 0.980 |

- 문턱(사전 등록 1부 1.2, 이분 open 대 not-open, 0.85 이상)은 정의대로 잰 검증 세트에서 채웠다. 검증 양성 156행 중 141행이 대조군 줄이라 끝난 항목만 보면 0.667·0.800 이다.
  검증 FP 6 = (c) 4(β 2, α 1, δ 1)와 (a)(b) M 2, FN 3 = P 2·T 1. 맞춤 FP 1 은 K16 (c)(α): 태그 대체를 첫 문장으로 좁힌 규칙 때문에 날짜 있는 완료 행이 상태 진술에서 빠졌다.
- done·unknown 은 참값 라벨이 없어 수만 보고하고 "검증 불가"다(검증 행 판정: open 159, done 18, unknown 1).
- 진행 기록: 규칙은 이슈 본문과 맞춤 세트 줄(K02 별칭 괄호, B12 괄호 줄, K01·K05·K08·K16 (c) 행)과 그 밖 세트의 응답 모양(묶음 줄)을 보고 만들었다. 검증 세트는 규칙을 고정한 뒤 평가했고, 검증 평가 뒤 규칙 변경은 아래 final-review 수정 3건뿐이다.
  단, 규칙 설계 전에 응답 모양을 보려고 검증 run 1개(기준선 main (a) 첫 run)의 OPEN 절을 읽었고, 라벨을 만들려고 BC02 줄 3개를 읽었다.
- **검증 세트: 오염됨.** 사유 두 가지. (1) 위의 설계 전 열람. (2) 검증 평가 뒤 final-review(2026-11-01)가 찾은 결함으로 규칙 3건을 고쳤다:
  맨 번호 앞 하이픈 낱말(`follow-up #12`)을 다른 프로젝트 이름으로 보던 것을 정답표가 아는 이름만으로 좁혔다, 묶음 줄 범위가 들여쓰기 없는 일반 문단(표 줄 제외)에서 끝나게 했다,
  조상 줄 주어의 이슈 번호가 이 항목 자신의 맨 번호면 하위 줄을 빼지 않게 했다. 최종 코드로 런 2 detail 55개를 다시 판정해 처음 커밋(9c9a241) 판정과 비교하면 검증 행 판정 변화는 0건이다(표 값 그대로).
  바뀐 것은 그 밖 세트 K01 3행(done -> open)뿐이고, 그중 지표 행은 P 2행(TP 17 -> 19)이며 나머지 1행은 지표 밖 참고 행(누락 P)이다.

**동결 조건 확인**:

| 조건(사전 등록 1부 1.2) | 결과 |
|---|---|
| (1) 검증 세트 이분 정밀도·재현율 >= 0.85 | 0.962 / 0.981 (채움) |
| (2) 런 2 (a) 30 run 전부에서 C01·C02·C03 이 v2 로 open | 30/30 (채움, v1 은 판정 불가 6 run) |
| (3) BC02 가 (b) 16개 페이지 전부에서 v2 로 open 아님 | open 0 (done 15, unknown 1) (채움) |
| (4) 재판독 일치율(보고만, 0.7 미만이면 ask) | 0.750 (27/36). 불일치 9: (c) 기억 6행 중 5행을 판독자는 open, P 줄 6행 중 3행을 not-open, M 1행을 open |

**알려진 한계**(코드 변경 없이 기록):
- (c) recall 결과가 빈 목록이면 invalid(no-provenance), 결과가 있고 keyword 가 없으면 unknown + absent 다. invalid 사유는 v1 과 같다는 설계를 유지한다.
- `V2_REF_RIGHT`는 keyword 와 전·뒤·후 사이 낱말 하나를 허용해, 항목 자신의 다음 단계 서술(`#12 검토 후 머지`)도 참조로 뺄 수 있다(목록 경로에서 거짓 open -> unknown 이득 가능). 런 2 (a)(b) 46 run 의 OPEN 줄에서 이 규칙으로만 빠진 (줄, 항목)은 1건이고 끝난 항목 판정 1건이 바뀐다(대조군 0).
- (a) 유효 run 선택은 invalid run 을 건너뛰고 뒤 run 을 쓴다(judge-arms 는 그 상태를 판정 불가로 둔다). 사전 등록 1부 1.1 "유효 = invalid 0, 대조군 open 1건 이상"과 같아 유지한다.

- 재판독: 라벨 표에서 36행(T 6·누락 T 5·P 6·M 7·대조군 6·(c) 6, 시드 338)을 뽑아 run·arm 식별자를 지우고 섞은 묶음을 코디네이터가 띄운 독립 판독자가 묶음만 보고 판독했다.

### 경로 (d)와 시험지 v2 (런 3, issue #341)

**경로 (d)** `d_recall_list`(등록표 `V2Path("d_recall_list", "d", "list", "all", _v2_judge_d)`, 판정 v2 전용. v1 summary 의 `paths`에는 없다):
- 질의: 게이트웨이 MCP `recall` 1회. `query` = 정답표 `query_instruction` + 줄바꿈 + 정답표 `list_query`(항목 이름·이슈 번호·프로젝트 이름 없는 넓은 목록 요청. 문안은 리포 밖 정답표에만 둔다), `query_timestamp=2026-10-28T14:59:59Z`, `max_tokens=4096`(`D_MAX_TOKENS`).
  게이트웨이를 거치므로 게이트웨이 recall 후처리(R3-4a `HC_ITEM_STATUS` 등)가 이 경로에 닿는다. 기준선은 그 플래그를 꺼 둔다(미설정).
- 판정: 결과 집합 하나에 항목마다 (c) 최신 상태 규칙 v2(`judge_recall_v2`, 동결)를 그대로 쓴다. keyword 가 든 결과가 없으면 unknown + absent(목록 경로라 끝난 항목에는 맞는 답), 결과가 비었으면 invalid(no-provenance), 기준 뒤 근거는 invalid(post-cutoff-evidence).
  그래서 (c)의 알려진 한계(오탐 성향, 위 맞춤·검증 표의 (c) FP)를 그대로 물려받는다.
- `score --paths d`(또는 `abcd`)로 붙인다. 기본 `--paths`는 `abc` 그대로다. 정답표에 `list_query`가 없으면 어떤 질의도 보내기 전에 exit 1. 합친 세트에서는 `list_query`를 가진 정답표가 하나면 그 값을 쓰고, 둘이 다르면 exit 1.
- manifest 경로 키는 `d`(judge-pairs 가 목록 경로 규칙으로 이득을 센다).

**시험지 v2(세트 C) 선정 도구**(규칙 정본: `artifacts/arch-v2/r3-2-setc.md` 1절):

```bash
# 근거 확인: 끝난 항목은 완료 근거, --controls 면 대조군 열림 근거(같은 규칙에서 상태만 open)와 partial 완료 근거도 본다
python3 -B -m bench.arch_v2_regression evidence --controls --answer-key <후보 정답표>
# 선정 run: 근거 있는 후보 전부를 네 경로로 한 번씩
python3 -B -m bench.arch_v2_regression score --judge v2 --paths abcd --answer-key <후보 정답표> --evidence <후보 evidence summary> [--evidence ...]
# 선정(질의 0회, 결정적): 세트 C 정답표·고정 세트·키 표를 0600 으로 새로 쓴다(이미 있으면 exit 1)
python3 -B -m bench.arch_v2_regression select --answer-key <후보 정답표> --evidence <후보 evidence summary> [--evidence ...] \
  --selection <선정 run summary.json> [--selection ...] --out-key <세트 C 정답표> --out-fixed <세트 C 고정 세트> --out-split <세트 C 키 표>
```

- 후보 정답표 = 세트 A·B 스키마 + `strata`(`{"<id>": {"key": true|false, "source": "symptom|issue|completion|issue-open"}}`(`issue-open` = 대조군 보충), 모든 항목). 세트 C 정답표에는 `strata`가 빠지고 항목 모양은 후보 그대로다(세트 A·B 항목 재사용은 바이트 복사).
- `select` 규칙: 끝난 항목은 근거가 있고 선정 run 의 어느 경로에서든 v2 open(원래 증상 `source=symptom`은 open 조건 없이), 그다음 키 있음·없음 층에서 작은 쪽 수만큼 후보 순서 앞에서부터(1:1). 대조군은 열림 근거 + 선정 run open, partial 은 완료 근거만.
  근거 있는 후보가 선정 run summary 에 없으면 exit 1(not-open 으로 조용히 세지 않는다). 뺀 항목은 정답표 `excluded`에 사유(`no-evidence`·`no-open-evidence`·`selection-not-open`·`stratum-quota`·`supplement-stop`(통과 대조군이 10에 닿은 뒤의 `issue-open` 후보))와 함께 남고, 층별 수는 `<out>/select-<시각>/report.json`(id·수·sha256 만)에 남는다.
- 세트 C 채점은 세트 A·B 와 같은 명령에 `--answer-key <세트 C 정답표> --evidence <세트 C 고정 세트> --key-split <세트 C 키 표> --set-name C --judge v2 --paths abcd`(키 표의 세트 이름이 `C`라 `--set-name C`가 없으면 기본 이름 `A`로 찾다가 exit 1).
  세트 C 는 단독으로 채점한다. 세트 A·B 항목을 원래 id 그대로 바이트 복사해 쓰므로 A·B·C 합친 채점은 같은 id 거부로 exit 1 이다.

### d-llm 오프라인 목록 경로 (런 4, issue #350)

원래 증상 경로(넓은 질의 여러 번의 recall 결과로 LLM이 열린 일 목록을 쓴다)를 저장된 recall 원문에서 질의 0회로 재는 도구다(`bench/d_llm.py`). 규칙 정본은 #349 "d-llm 경로"·"d-llm 판정".

- 입력 조립: 저장 recall 실행을 질의 순(q, 같은 q 안에서는 `max_tokens` 오름차순) -> 순위 순으로 펼치고 기억 id 중복을 처음 나온 자리만 남긴다. 행은 `id`·`type`·`mentioned_at`·`tags`·`context`·`text`만 한 줄 JSON으로 넣는다. `--max-input-chars`를 넘는 뒤 행은 잘라 `truncated_rows`로 센다. `--max-tokens N`은 그 `max_tokens` recall만 쓰는 보고용 입력이다. 기억 id 없는 행은 넣지 않고 `no_id_rows`로 센다.
- 기준선 프롬프트: `bench/prompts/d_llm_baseline.txt`(동결). 처치 프롬프트는 같은 폴더의 새 파일로만 더한다(`--prompt`).
- LLM: `claude -p --model claude-sonnet-5-5 --output-format json --safe-mode --tools "" --strict-mcp-config --no-session-persistence`, 빈 임시 cwd. 토큰은 자식 env `CLAUDE_CODE_OAUTH_TOKEN`에만 넣는다. 반복마다 스키마 검증이 실패하면 1회 재시도, 그래도 실패면 invalid(분모 K에 남는다). 호출별 토큰·시간·비용을 출력 파일과 표준 출력에 남긴다.
- 대응·판정: `bench/d_llm_match.py`(동결, 대응·판정 함수만). key 일치 > `name` keyword(판정기 v2 경계 규칙, 맨 번호는 project_hint 가 있으면 같은 name 에 프로젝트 이름이 있을 때만) 중 가장 긴 것. key 는 owner 를 버린 마지막 경로 조각·소문자로 맞춘다, 동률이면 unmapped(`ambiguous`). key 가 어느 작업과도 맞지 않으면 name 규칙으로 내려간다. `quote`·`evidence_ids`만 겹치는 것은 대응이 아니다. 한 작업에 항목이 여럿이면 open > done > partial(residual), 목록에 없으면 absent. `unverified`는 표시이고 open으로 센다.
- 채점용 작업 표: 정답표 `closed`·`controls`·`partial`의 id 와 `item_keywords()`(판정기 v2) keyword 만 쓴다(항목 이름은 대응에 쓰지 않는다).

```bash
W=~/.local/share/hindsight-bench/arch-v2/r4-0-work     # 리포 밖, 0700
python3 -B -m bench.d_llm run --input <저장 recall json> --out "$W" --k 5 --label main      # 출력 파일 0600, 이미 있으면 exit 1
python3 -B -m bench.d_llm judge --gen "$W/main-gen.json" --answer-key <정답표> --expect-key-sha256 <sha256> --out "$W/main-judge.json"
python3 -B -m bench.d_llm blind --gen "$W/main-gen.json" --out "$W/../r4-0-blind.json" --map "$W/main-blind-map.json"
python3 -B -m bench.d_llm agree --gen "$W/main-gen.json" --map "$W/main-blind-map.json" --judge "$W/main-judge.json" \
  --reader <판독1> --reader <판독2> --answer-key <정답표> --out "$W/main-agree.json"
```

- 판독 파일(`blind`)은 반복 번호를 지우고 섞은 목록 항목(`rid`, `key`, `name`, `status`, `unverified`, `quote`)만 담는다. 판독 회신 형식은 `{"items": [{"rid", "work": "<repo>#<n>" | "label:<짧은 이름>" | "multi" | "unclear", "claims_open": true|false}]}`.
- `agree`: 입력 map·judge가 `--gen`에서 만들어졌는지 gen_sha256으로 확인하고 다르면 exit 1. 분모는 두 판독자가 모두 판독한 단위(빈 work, 불린 아닌 `claims_open` 제외), 미회신 rid 수는 `unread`. manual 목록(판독 label 원문)은 표준 출력에 내지 않고 `<out>-manual.json`(0600)에만 쓴다. (가) 판독자 간 작업 식별·열림 주장 일치, (나) 코드 대 판독(열림 주장 = `status=open` 대 `claims_open`, 작업 식별 = 코드가 대응한 작업의 key 집합에 판독 `work`가 드는지). key 로 비교할 수 없는 판독(label, 또는 코드가 고른 작업에 key 가 없음)은 `manual`로 세고 사람이 근거로 판정한다. `a_work.rate`는 manual 을 뺀 기계 집계라 단독으로 관문에 쓰지 않는다.
- 결과(R4-0): `artifacts/arch-v2/r4-0-dllm.md`.

#### R4-1 recall 저장·판독 표·세트 D·stats (issue #351)

규칙 정본은 #349 "recall 인자"·"시험지 v3"·"주 통계량과 영분포"·"T1-lite 판정". 사전 등록 v4 1부 나머지가 확정한 값은 K=7, 입력 상한 200,000자, recall `max_tokens` 16384다. `recall`의 `--max-tokens` 기본값만 16384이고, `run`의 기본값은 R4-0 그대로(`--k 5`, 입력 상한 없음)라서 R4-1 이후 측정은 `run --k 7 --max-input-chars 200000`을 반드시 명시한다(`R41_K`·`R41_MAX_INPUT_CHARS`는 기록용 상수다).

- `recall`: 사본(`HC_TARGET=next`)에서만 돈다(운영 대상은 exit 2). 질의마다 게이트웨이 MCP `recall` 1회, `query` = 고정 지시문 파일 + 줄바꿈 + 문안, `query_timestamp=2026-10-28T14:59:59Z`. 질의별 지연(초)·결과 행 수·응답 토큰(결과 행 `text`의 tiktoken cl100k_base 수 합, upstream `max_tokens` 예산과 같은 셈)을 남긴다. 출력은 `run --input` 모양(질의 순 `q`). 오류 난 질의가 하나라도 있으면 빈 결과로 저장한 뒤 exit 1(그 파일을 측정 입력으로 쓰지 않는다). `--from-set <세트 D>`는 고정 후보(끝난 일 + 대조군 + partial, unknown 제외)마다 이름 recall(T1b 입력)이다. 문안·지시문 파일은 리포 밖 0600, 리포에는 sha256만.
- `table`: `blind` 판독 회신(판독자 1)의 `work`로 목록 항목을 작업별로 묶은 참값 판독 표 뼈대(0600). `multi`·`unclear`는 묶지 않고 센다. `--out`·`--truth-blind`가 이미 있으면 exit 1. `--truth-blind`는 두 번째 판독자용 참값 판독 파일(작업 이름·key·인용만, 반복 수·상태 집계 없음).
- `fixset`: 채운 판독 표(`truth` = done/open/partial/unknown, `keywords`, `project_hint`) -> 세트 D(`closed`=끝난 일, `controls`=열린 일=대조군, `partial`, `unknown`=제외, 각 수와 키 있음/없음). keyword는 그 작업 목록 항목의 `name` 안의 말이나 key(맨 번호 포함)만 받는다(다른 출처면 exit 1). 세트 D는 `judge --answer-key`에 그대로 쓴다.
- `stats`(`bench/d_llm_stats.py`, R4-1 1단계 커밋으로 고정, R4-3·R4-4는 이 모듈만 쓴다): 항목별 열림율(invalid 반복도 분모 K), 짝별 적격 이득(`--qualified` 이득 판독 `new_done_row`·`stale_open_dropped`만, `vanished`는 0)·새 오답(끝난 항목 증가분 + `--offtable` 표 밖 끝난 일·라벨 불가의 증가분)·순이득, 순열 영분포(같은 복원 기준선 K회를 섞어 floor(K/2) 대 floor(K/2), 홀수면 1회 제외, `--perms` 1000·`--seed` 20261101, 95 백분위 = 가장 가까운 순위), 복원 A/A 두 방향 최대, `--x2`면 처치 두 반복 차를 상한에 더함, 대조군 가드(기준선 열림율 > 0 대조군만 분모, 0인 수 따로, 분모 < 6 판정 불가), 효과 판정(두 짝 모두 순이득 > 상한·가드 통과·순이득 >= 3.0, 최소 표본 끝난 >= 20·대조군 >= 6, 이득 판독이나 `--offtable`이 없으면 `pending-reading`(표 밖 판독이 빈 것이면 `{"pairs": [[], []]}` 파일을 준다), 짝이 정확히 2개가 아니면 `invalid-pairs`, qualified·offtable 길이가 짝 수와 다르거나 `--x2`인데 처치가 2개가 아니면 exit 1), T1-lite 보조(`unverified=true`만으로 열린 반복을 not-open으로 센 순이득과 끝난 항목 대 대조군 `unverified` 비율 차, 효과 판정에 쓰지 않음). `--treat` 없이 부르면 기준선 보고(열림율·영분포·A/A·안정 양성)만 낸다. 표준 출력은 id·수치만, 전체는 `--out`(0600).

```bash
W=~/.local/share/hindsight-bench/arch-v2/r4-1-work        # 리포 밖, 0700
I=~/.local/share/hindsight-bench/arch-v2/r4-1-instruction.txt
next_env=$(bash scripts/next-stack.sh env) || exit 1; eval "$next_env"
python3 -B -m bench.d_llm recall --instruction "$I" --queries <요청 가족 파일> --out "$W/sel-open.json" --label sel-open
python3 -B -m bench.d_llm recall --instruction "$I" --queries <완료 어휘 파일> --out "$W/sel-done.json" --label sel-done
python3 -B -m bench.d_llm run --input "$W/sel-open.json" --out "$W" --k 7 --max-input-chars 200000 --label sel
python3 -B -m bench.d_llm blind --gen "$W/sel-gen.json" --out <판독 파일> --map "$W/sel-blind-map.json" --seed 351
python3 -B -m bench.d_llm table --gen "$W/sel-gen.json" --map "$W/sel-blind-map.json" --reader <판독1> --out "$W/sel-table.json" --truth-blind <참값 판독 파일>
python3 -B -m bench.d_llm fixset --table <채운 판독 표> --out <세트 D>
python3 -B -m bench.d_llm recall --instruction "$I" --from-set <세트 D> --out "$W/m1-names.json" --label m1-names
python3 -B -m bench.d_llm stats --set <세트 D> --base "$W/m1-gen.json" --base "$W/m2-gen.json" --out "$W/stats-base.json"
```

#### R4-3 처치 변형·인과 진단 (issue #352)

규칙 정본은 #349 "처치(층별)"·"T1-lite 판정"·"T1 계열의 지위"와 사전 등록 v4 2부(2.2 순서, 2.4 조립 규칙). 모두 R4-1 측정 복원 저장 원문에서 오프라인으로 돈다(질의 0회, 사본 0). `run --k 7 --max-input-chars 200000`을 명시한다.

- 조립 규칙(2.4): 기준선 입력(`--max-input-chars` 적용 결과)을 그대로 앞에 두고 추가 행(기억 id 중복 제거)을 뒤에 붙인다. 상한은 기준선 몫에만 적용한다. 처치 옵션이 없으면 기준선 `assemble()`과 같은 입력이고 출력 머리에 처치 칸이 없다.
  - `--add <저장 recall>`(T0): 추가 질의 합집합의 새 행을 질의 순 -> 순위 순으로 붙인다. 추가분 상한 없음.
  - `--add-names <이름 recall 저장>`(T1b): 후보 순서(저장 `q` 순 = 세트 D 고정 순) 라운드로빈으로 순위 1행씩 붙이고, 추가분이 `--add-names-cap`(기본 100,000자)을 넘는 첫 행에서 멈춘다(뒤의 짧은 행을 골라 넣지 않는다). 넣은 행·글자와 넣지 못한 새 행 수를 남긴다.
  - `--inject <diag 주입 파일>`: 주입 행을 뒤에 붙인다. `--drop-ids <diag 제거 파일>`: 기준선 입력에서 그 id 를 지운다(잘린 행은 돌아오지 않고, 지운 id 는 추가·주입으로도 다시 들이지 않는다).
  - 처치 run 출력 머리에 `treatment`(기준선 행·글자·잘린 행, 추가·주입·제거 수, 처치 파일 sha256)와 `input_ids`(모델 입력 기억 id 순서)가 붙는다. 표준 출력에는 `input_ids`를 내지 않는다.
- 처치 프롬프트(첫 처치 호출 전 고정, #349 댓글): `bench/prompts/d_llm_t1_lite.txt`(T1-lite, `unverified` 표시 지시), `bench/prompts/d_llm_t1a.txt`(T1a, 확인 지시만). 둘 다 기준선 문안 전체에 recall 행 블록 앞 지시 한 문단만 더했다. T1-lite 의 `unverified`는 주 통계량에서 open 으로 센다(`stats`의 T1-lite 보조 지표만 not-open).
- `diag`(인과 진단 입력, 판정 입력 아님): 판독 파일 `{"tasks": {작업 id: [완료 근거 기억 id]}}`(리포 밖)과 같은 복원의 기준선 run 출력으로 주입 파일(끝난 일 순서의 완료 근거 행 전체, 저장 recall 에서 처음 찾은 행, 없는 id 는 `missing`)과 제거 파일(기준선 유효 반복에서 끝난 일에 대응된 `status=open` 항목의 `evidence_ids` 합집합, 완료 근거 행 제외)을 0600 으로 쓴다. 이미 있으면 exit 1.
- 판정 계산은 `stats`만 쓴다(`--treat` 짝 2개, `--qualified` 이득 판독, `--offtable` 표 밖 항목 라벨).

```bash
W=~/.local/share/hindsight-bench/arch-v2/r4-1-work; O=~/.local/share/hindsight-bench/arch-v2/r4-3-work   # 리포 밖, 0700
python3 -B -m bench.d_llm diag --set <세트 D> --gen "$W/m1-gen.json" --done-rows <완료 근거 판독> \
  --rows-from "$W/m1-names.json" --rows-from "$W/m1-open.json" --out-inject "$O/m1-inject.json" --out-drop "$O/m1-drop.json"
python3 -B -m bench.d_llm run --input "$W/m1-open.json" --out "$O" --k 7 --max-input-chars 200000 --label m1-diag-inj --inject "$O/m1-inject.json"
python3 -B -m bench.d_llm run --input "$W/m1-open.json" --out "$O" --k 7 --max-input-chars 200000 --label m1-t0 --add "$W/m1-done.json"
python3 -B -m bench.d_llm run --input "$W/m1-open.json" --out "$O" --k 7 --max-input-chars 200000 --label m1-t1lite --prompt bench/prompts/d_llm_t1_lite.txt
python3 -B -m bench.d_llm run --input "$W/m1-open.json" --out "$O" --k 7 --max-input-chars 200000 --label m1-t1b --add-names "$W/m1-names.json"
python3 -B -m bench.d_llm stats --set <세트 D> --base "$W/m1-gen.json" --base "$W/m2-gen.json" \
  --treat "$O/m1-t0-gen.json" --treat "$O/m2-t0-gen.json" --qualified <이득 판독> --offtable <표 밖 라벨> --out "$O/stats-t0.json"
```

## 런 5 (상위 #360)

규칙 정본은 #360 "결정됨"·"제약". 원문(이슈 목록, 키 목록, 라벨셋, 기억 메타, recall 저장)은 리포 밖 `~/.local/share/hindsight-bench/arch-v2/r5-*`(디렉터리 0700, 파일 0600)에만 두고 리포에는 수치·sha256·코드만 쓴다.

### 라벨셋 (R5-A1, issue #361)

`bench/r5_labels.py`가 GitHub 이슈 상태를 정답으로 D10 사본 뱅크 기억에 나오는 작업 키 `(repo, n)`마다 기준일 t 의 라벨을 만든다. 규칙은 모듈 머리 주석이 정본이다. 결과: `artifacts/arch-v2/r5-a1-labels.md`.

- 키는 `gateway/item_keys.py`의 `extract_keys`만 import 한다. 완료 계열(`kind:completion`·`kind:done`·`kind:task-done`·`kind:task-completion` 태그)과 "기억 있음"(`has_memory`) 판정은 이 모듈 안 규칙이고 S1 원장 코드와 서로 import 하지 않는다.
- 라벨: 양성 = 끝남(`state_reason=completed`, `closed_at` <= t 23:59:59.999999 KST) + 그 전 완료 계열 기억, 데이터층 = 끝남인데 완료 계열 기억 없음(하위 `data_sub`: `no_completion` 언급은 있음 / `no_mention` 언급도 없음, G 지표 분모 밖), 대조 = 열림 + 그 전 그 키를 말하는 기억. PR·재오픈 이력·`not_planned`·그 밖의 닫힘 사유·동명 repo 충돌은 뺀다(수를 요약에 남긴다). 재오픈 events 를 받지 않은 캐시(`github --meta` 전)로는 `build`가 exit 1.
- t: 2026-09-01부터 3일 간격, 상한 = 마지막 `mentioned_at`의 KST 날짜. 봉인 = 상한 직전 8일에 쌍이 있는 키를 양성 층·대조 층(둘 다면 양성)으로 나눠 씨앗 20261101로 층마다 30%(최소 1키)를 뽑은 봉인 키의 봉인 구간 쌍, dev = 봉인 키가 아닌 키의 쌍 중 t <= 봉인 시작 - 5일(간격 t 는 빼고, 데이터층도 같은 키 분할), fold = 키의 정규 repo(요약·라벨셋에는 r01.. 코드).
- `github`는 GET 만 보낸다(`gh api`, 호출 수를 센다). 이슈 레코드는 번호·상태·사유·생성·닫힘 시각·PR 여부만 저장한다(제목 없음). 같은 `--out`으로 다시 부르면 받은 것은 건너뛴다. `meta`는 사본(`HC_TARGET=next`)에서만 돈다(scripts/item-backfill.py 와 같은 `memories/list` 전수, 컨테이너 안, 키는 stdin). 기억 본문은 저장하지 않는다.
- 세트 C·D(리포 밖 정답표)는 `build --setc/--setd`의 일치율에만 쓴다(기준 시각 = 세트의 질의 날짜 끝). 판정용 일치율은 dev 키만으로 계산하고, 봉인 키 포함 값은 요약의 `agreement_with_sealed_reference`(참고)에 둔다.

```bash
O=~/.local/share/hindsight-bench/arch-v2                  # 리포 밖, 0700
PROD="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")"
# 1) 사본 0: 정답 리포 해석(Bkankim)과 이슈 목록
python3 -B bench/r5_labels.py github --aliases "$O/r2-aliases.json" --policy "$PROD/policies/local/bank-policies.json" --out "$O/r5-a1-github.json"
# 2) D10 사본: 기억 메타 전수(R5-A2 가 다시 쓴다)
next_env=$(bash scripts/next-stack.sh env) || exit 1; eval "$next_env"
BANK="$(grep -E '^HC_PERSONAL_BANK=' "$PROD/.env.host.local" | cut -d= -f2-)"
python3 -B bench/r5_labels.py meta --bank "$BANK" --aliases "$O/r2-aliases.json" --policy "$PROD/policies/local/bank-policies.json" --out "$O/r5-a1-meta.jsonl"
# 3) 사본 0: 기억 owner 표기로 소유자 보충 + 키 이슈의 재오픈 events
python3 -B bench/r5_labels.py github --aliases "$O/r2-aliases.json" --policy "$PROD/policies/local/bank-policies.json" --meta "$O/r5-a1-meta.jsonl" --out "$O/r5-a1-github.json"
# 4) 라벨·분할·일치율 -> r5-a1-labelset.jsonl, r5-a1-summary.json, r5-a1-repo-codes.json (모두 0600)
python3 -B bench/r5_labels.py build --meta "$O/r5-a1-meta.jsonl" --github "$O/r5-a1-github.json" --out-dir "$O" \
  --setc "$O/r3-setc-answer-key.json" --setd "$O/r4-1-setd.json"
```

- 라벨셋 한 줄: `{"key": "<repo>#<n>", "t": "YYYY-MM-DD", "label": positive|data_layer|control|control_no_memory, "data_sub": no_completion|no_mention(데이터층만), "split": dev|sealed|unused, "fold": "rNN"}`. `control_no_memory`는 라벨이 아니다(열림인데 그 전 기억 없음, 수만 센다). 봉인(`split=sealed`) 쌍은 R5-D 전에 recall·d-llm 에 쓰지 않는다.

### 지표·노이즈 바닥·오라클 (R5-A2, issue #362)

`bench/r5_metrics.py`(검색 지표, recall 경로 열림, G1·G2, 키 군집 재표집, d-llm 종단 지표)와 `bench/r5_oracle.py`(o1·o2 변환). 규칙은 모듈 머리 주석이 정본이다. 결과: `artifacts/arch-v2/r5-a2-baseline.md`.

- 질의: t 마다 런 4 요청 가족 8개 + 그 t 에 dev 양성·대조 쌍이 있는 프로젝트별 상태 질의 1개. 고정 지시문(런 4 `r4-1-instruction.txt`)의 기준 날짜를 t 로 바꿔 앞에 붙이고 `query_timestamp` = t 23:59:59 KST, `max_tokens` = 16384. 문안은 리포 밖 0600(`plan` 출력), 리포에는 sha256 만. dev 마지막 t 뒤 ~ 라벨 상한(봉인 구간·간격)의 t 는 `plan`·`dllm`·`judge`가 거부한다(라벨 상한 뒤 t 는 키 없는 기준선용으로 요청 가족만).
- (키, t) 입력: t 의 질의 결과 합집합(질의 순 -> 순위 순, 기억 id 중복 제거) -> 시간여행 필터(`mentioned_at` > t 23:59:59.999999 KST 이거나 없는 행 제거, 관찰 포함). 검색 지표와 d-llm 이 같은 입력을 쓴다(d-llm 은 입력 상한 200,000자).
- 검색 지표(LLM 없음): 행의 키 = `extract_keys`(라벨셋과 같은 별칭 표), 완료 계열 = 라벨 쪽 `r5_labels.is_done_family`. recall 경로 열림 = 낡은 열림 행(그 키를 말하는 완료 계열 아닌 행)이 있고 완료 근거(완료 계열 행 또는 S1 구조 필드 `gateway_item_ledger`의 done 칸)가 없음. 키가 없으면 absent. S1 주석은 구조 필드로만 인식한다(본문 문안은 보지 않는다). G1 = 기준선에 나온 양성 쌍 중 열림, G2 = 기준선에 나온 대조 쌍 중 처치 뒤에도 나오고 완료 표시가 없음.
- d-llm: 동결 기준선 프롬프트(query_date = t), `claude-sonnet-5-5`, K=3, 대응은 동결 `d_llm_match`. `dllm`은 완료된 출력(`finished_kst` + 반복 K개)만 건너뛰고 미완료 출력이 있으면 exit 1로 거부하며, `judge`도 미완료 출력을 거부한다. `--max-usd`는 `--out-dir`의 같은 라벨 출력(완료·미완료) 비용을 합산해 반복마다 확인하고 한도면 exit 3이다. 쌍 상태 = open 과반이면 open, open + invalid 가 과반이면 invalid(별도 칸, 분모에 남음), 그 밖은 not_open. 출력 실패·빈 목록 반복은 invalid.
- 노이즈 바닥: 반복 짝마다 키 단위 군집 재표집(씨앗 20261101, 2000회, 비율 지표의 분모 0 재표집은 빼고 `dropped_zero_den`으로 센다) |ΔG| 95 백분위의 최댓값. 오라클 감소율 = 기준선 d-llm 오답(양성 쌍 open) 중 o1·o2 입력에서 not_open 이 된 비율. 둘 다 50% 미만이면 관문 멈춤.
- 주 지표(#362 범위 확장, G1 은 보조): P(목록 정밀도) = open 으로 나온 dev 라벨 쌍 중 실제 열린(대조) 쌍 비율, R(열린 항목 회수율 = G2) = 기준선 결과에 나온 대조 쌍 중 open 비율(대조 전부 분모는 R_all). `judge`가 기준선·오라클마다 P·R, P·R 노이즈 바닥, base1 대비 짝 비교, R 원인 (a)(b)를 낸다.
- 오라클 o3: 질의 범위(그 t 의 프로젝트 상태 질의 + 요청 가족의 project) 안 키 전부의 뱅크 상태 표(키 | 상태 | 날짜 | 근거 id)를 recall 행 뒤에 붙인다. 상태는 기억 메타의 키·태그·시각만으로 정하고(완료 계열 = `gateway.item_status.is_completion_family`, 표는 리포 템플릿 정책 `tag_aliases`), GitHub·라벨셋·라벨 쪽 규칙은 쓰지 않는다. `o3o1` = 표 + o1 주석. `gateway` 를 import 하므로 `uv run --with-requirements gateway/requirements.txt`로 돌린다.
- R_bank: `judge --meta <기억 메타> --tag-aliases <정책 JSON>`이면 대조 쌍의 뱅크 상태(o3 표와 같은 규칙)로 분모를 좁힌 R_bank(완료 계열 기억 없음 + 언급 있음)와 R_bank2(완료 뒤 비완료 언급이 있어도 열림)를 더 낸다. 이때도 `uv run --with-requirements gateway/requirements.txt`로 돌린다.

```bash
O=~/.local/share/hindsight-bench/arch-v2; W=$O/r5-a2-work            # 리포 밖, 0700
A="--labelset $O/r5-a1-labelset.jsonl --aliases $O/r2-aliases.json"
python3 -B bench/r5_metrics.py plan --labelset $O/r5-a1-labelset.jsonl --families $O/r4-1-families.json \
  --instruction $O/r4-1-instruction.txt --project-template <프로젝트 질의 파일> --extra-t 2026-10-28 --out $O/r5-a2-plan.json
next_env=$(bash scripts/next-stack.sh env) || exit 1; eval "$next_env"      # D10 사본, S1·X1 꺼짐
python3 -B bench/r5_metrics.py recall --plan $O/r5-a2-plan.json --out-dir $W/rep1 --label rep1   # 반복마다 rep2..rep5
python3 -B bench/r5_metrics.py search $A --recalls $W/rep1 --recalls $W/rep2 ... --out $W/search.json
python3 -B bench/r5_metrics.py dllm $A --recalls $W/rep1 --t <t> ... --oracle none --k 3 --out-dir $W/gen --label base1
python3 -B bench/r5_metrics.py dllm $A --recalls $W/rep1 --t <t> ... --oracle o1 --meta $O/r5-a1-meta.jsonl --k 3 --out-dir $W/gen --label o1
python3 -B bench/r5_metrics.py dllm $A --recalls $W/rep1 --t <t> ... --oracle o2 --k 3 --out-dir $W/gen --label o2
uv run --no-project --python 3.12 --with-requirements gateway/requirements.txt --with tiktoken python -B bench/r5_metrics.py dllm $A \
  --recalls $W/rep1 --t <t> ... --oracle o3 --meta $O/r5-a1-meta.jsonl --plan $O/r5-a2-plan.json --families $O/r4-1-families.json \
  --tag-aliases policies/templates/personal-bank.json --k 3 --out-dir $W/gen --label o3        # o3o1 도 같은 인자
python3 -B bench/r5_metrics.py judge $A --recalls $W/rep1 --t <t> ... --gen-dir $W/gen --base base1 --base base2 \
  --oracle o1=o1 --oracle o2=o2 --oracle o3=o3 --oracle o3o1=o3o1 --out $W/judge.json
python3 -B bench/r5_metrics.py keyless --gen $W/gen/keyless-2026-10-28-gen.json --answer-key $O/r3-setc-answer-key.json \
  --answer-key $O/r4-1-setd.json --out $W/keyless.json
```

### 역할 구분 원장 오라클 (R5-A3, issue #372)

`bench/r5_roles.py`(언급 역할 규칙·LLM 역할 판정 래퍼, o3a·o3b·o3c 상태, 열림만·전체 표, o1c, d-llm·판정). 규칙은 모듈 머리 주석이 정본이다. 결과: `artifacts/arch-v2/r5-a3-roles.md`.

- 입력: R5-A2 저장 recall(`r5-a2-work/rep1`, 같은 합집합·시간여행 필터), 기억 메타, 그리고 D10 사본에서 1회 읽은 기억 본문(`bodies`, 메타에서 키가 나오고 `mentioned_at` <= dev 마지막 t 10-13 끝 시각인 기억만). 다른 명령은 그보다 늦은 본문이 섞이면 거부하고 봉인·간격 t 도 거부한다.
- 언급: 본문을 문장으로 나눠 문장마다 `extract_keys(문장, item: 태그를 뺀 태그, 별칭 표)`. item 태그에서만 나온 키는 참조. 역할(끝냄/열림/참조)은 문장 표지어 -> (첫 문장이면) 라벨 순, 한 문장에 둘 다면 열림, 한 기억 안에서는 열림이 이긴다.
- 상태: o3c = t 이하 마지막 비참조 언급의 역할(같은 시각이 갈리면 열림, 참조만이면 미정). o3c_llm = 같은 규칙에 언급 역할만 `claude-haiku-5-5` 판정(상한 측정 전용, 게이트웨이에 넣지 않는다). o3a·o3b = R5-A2 o3 규칙의 변형(단일 키 완료만 / 같은 문장 표지어가 있는 완료 제외). GitHub·라벨셋은 상태·역할 입력이 아니다(`tables`·`judge`의 채점에만).
- 표: 범위는 R5-A2 o3 와 같은 질의 범위 프로젝트. 전체 / 열림만(열림 + 최근 끝남 7일). 표 토큰 = 표 tiktoken / 그 t 의 필터 뒤 d-llm 입력 토큰. o1c = o3c 끝남 키를 말하는 recall 행마다 o1 형식 주석.
- 지출 한도: `--max-usd`는 `--spent-dir` 아래 d-llm 출력과 LLM 역할 판정 파일의 비용 합(티켓 누계)이다. `dllm`은 설정 시작 전에 누계 + 이 설정 호출 수 x 호출당 평균(측정된 d-llm 호출 평균, 없으면 0.30달러)이 한도를 넘으면 시작하지 않고(exit 3), 호출 전마다 누계를 다시 읽어(같은 디렉터리를 쓰는 다른 프로세스 지출 포함) 한도면 멈춘다. `llm-roles`도 호출마다 다시 읽는다. o3c_llm 은 역할 파일의 `mentions_sha256`이 지금 언급 파일과 같아야 쓴다.

```bash
O=~/.local/share/hindsight-bench/arch-v2; W=$O/r5-a3-work                # 리포 밖, 0700
TS="--t 2026-09-28 --t 2026-10-01 --t 2026-10-04 --t 2026-10-07 --t 2026-10-10 --t 2026-10-13"
UV="uv run --no-project --python 3.12 --with-requirements gateway/requirements.txt --with tiktoken python -B"
# 1) D10 사본 1회: 본문만(읽기만, recall 0) -> 곧바로 down
next_env=$(bash scripts/next-stack.sh env) || exit 1; eval "$next_env"
python3 -B bench/r5_roles.py bodies --meta $O/r5-a1-meta.jsonl --bank "$BANK" --out $O/r5-a3-bodies.jsonl
# 2) 사본 0: 언급·규칙 역할, LLM 역할(먼저 --limit 5 로 호출 확인)
python3 -B bench/r5_roles.py mentions --meta $O/r5-a1-meta.jsonl --bodies $O/r5-a3-bodies.jsonl --aliases $O/r2-aliases.json --out $W/mentions.json
python3 -B bench/r5_roles.py llm-roles --mentions $W/mentions.json --out $W/roles-llm.json --spent-dir $W --max-usd 15
# 3) 표 크기·토큰(LLM 없음), d-llm(설정마다), 판정
C="--labelset $O/r5-a1-labelset.jsonl --aliases $O/r2-aliases.json --meta $O/r5-a1-meta.jsonl --mentions $W/mentions.json \
  --llm-roles $W/roles-llm.json --plan $O/r5-a2-plan.json --families $O/r4-1-families.json \
  --tag-aliases policies/templates/personal-bank.json --recalls $O/r5-a2-work/rep1 $TS"
$UV bench/r5_roles.py tables ${=C} --out $W/tables.json
$UV bench/r5_roles.py dllm ${=C} --variant o3c --form open --k 3 --out-dir $W/gen --label o3c-open --max-usd 15 --spent-dir $W
$UV bench/r5_roles.py judge --labelset $O/r5-a1-labelset.jsonl --aliases $O/r2-aliases.json --meta $O/r5-a1-meta.jsonl \
  --mentions $W/mentions.json --llm-roles $W/roles-llm.json --recalls $O/r5-a2-work/rep1 $TS \
  --base-gen-dir $O/r5-a2-work/gen --base base1 ... --base base5 --gen-dir $W/gen --setting o3c-open=o3c-open ... --out $W/judge.json
# 4) 표본 판독(언급 40, 씨앗 20261101, 판독자 워커 1인) 일치
python3 -B bench/r5_roles.py sample --mentions $W/mentions.json --llm-roles $W/roles-llm.json --n 40 --out $W/sample.json
python3 -B bench/r5_roles.py agree --sample $W/sample.json --verdicts $W/sample-verdicts.json --out $W/sample-agree.json
```

### 저장 역할 원장 오프라인 확인 (R5-A4, issue #374)

`bench/r5_roles.py`에 추가만 했다(기존 함수와 `tables`·`dllm` 기본 출력은 바이트 단위로 같다). 규칙은 모듈의 "R5-A4" 절 주석이 정본이다. 결과: `artifacts/arch-v2/r5-a4-offline.md`.

- 입력은 R5-A3 저장물(본문·언급·LLM 역할 `r5-a3-work/`)과 R5-A2 저장 recall(`rep1`)뿐이다. 사본 0, 새 haiku 호출 0. #372 LLM 역할 파일을 "저장된 역할"의 대리값으로 읽기만 한다.
- 표 형식(`--table-fmt`, 열림만 표에만): `base`(#372 그대로), `a`(`키 | 상태`), `b`(`키 | 상태 | 날짜`), `c`(최근 끝남 줄 제외, N=0), `a+c`, `b+c`. 머리말은 열 이름에 맞추고 c 계열은 '와 최근 7일 안에 끝난 키' 구만 뺀다. 형식이 base 가 아니면 gen 머리 `oracle`에 `[형식]`이 붙고 `table_fmt` 칸이 생긴다.
- `fmt-tables`: 형식마다 t별 표 토큰·추가 토큰(o1c 포함)·표 상태만 채점(LLM 없음). 추가 토큰 = (표 토큰 + o1c 주석으로 늘어난 입력 토큰) / 필터 뒤 d-llm 입력 토큰.
- `cands`: 설정마다 P 오답(open으로 나온 양성 쌍)과 R 누락(R 분모 안 대조 쌍 중 open 아님)을 근거 언급(문장·규칙·LLM 역할)과 함께 리포 밖 0600 파일로 낸다. 판독자가 이것을 읽어 `reasons.json`(항목 = 설정, t, 키 코드, P|R, 사유, 근거)을 쓴다. 키 코드 = `k` + 키 sha256 앞 10자.
- `a4-judge`: `reasons.json`을 읽어 항목마다 판정 순서 (ii) 언급 없음 -> (i) 역할 판정 오류 -> (iii) 라벨 불일치 -> (iv) 소비자를 다시 계산해 대조하고(어긋나면 거부), 설정마다 P, P_bank, R, R_bank, R_bank2, 추가 토큰 최대, R_bank의 base1 대비 변화와 R_bank 노이즈 바닥(base1~5, 키 군집 재표집)을 낸다. 사유 없는 오답 쌍이 있으면 거부한다. gen 머리 `oracle`로 입력을 다시 만들어 `prompt_text_sha256`이 같은지 확인한 뒤 토큰을 센다.

```bash
O=~/.local/share/hindsight-bench/arch-v2; W3=$O/r5-a3-work; W=$O/r5-a4-work      # 리포 밖, 0700
TS="--t 2026-09-28 --t 2026-10-01 --t 2026-10-04 --t 2026-10-07 --t 2026-10-10 --t 2026-10-13"
UV="uv run --no-project --python 3.12 --with-requirements gateway/requirements.txt --with tiktoken python -B"
J="--labelset $O/r5-a1-labelset.jsonl --aliases $O/r2-aliases.json --meta $O/r5-a1-meta.jsonl --mentions $W3/mentions.json \
  --llm-roles $W3/roles-llm.json --recalls $O/r5-a2-work/rep1 $TS"
C="$J --plan $O/r5-a2-plan.json --families $O/r4-1-families.json --tag-aliases policies/templates/personal-bank.json"
# 1) 지출 0: 형식별 토큰, 오답 후보(판독 입력)
$UV bench/r5_roles.py fmt-tables ${=C} --out $W/fmt-tables.json
$UV bench/r5_roles.py cands ${=J} --setting o3cllm-open=$W3/gen::o3cllm-open --setting base1=$O/r5-a2-work/gen::base1 --out $W/cands-1.json
# 2) d-llm(설정마다, 티켓 상한 10달러, 누계는 $W)
$UV bench/r5_roles.py dllm ${=C} --variant o3c_llm --form open --o1c --k 3 --out-dir $W/gen --label o3cllm-open-o1c --max-usd 10 --spent-dir $W
$UV bench/r5_roles.py dllm ${=C} --variant o3c_llm --form open --table-fmt a+c --k 3 --out-dir $W/gen --label o3cllm-open-ac --max-usd 10 --spent-dir $W
# 3) 새 설정의 cands -> 새로 생긴 오답만 reasons.json 에 추가 판독 -> 분기 입력
$UV bench/r5_roles.py a4-judge ${=C} --reasons $W/reasons.json --base-gen-dir $O/r5-a2-work/gen --base base1 ... --base base5 \
  --setting o3cllm-open=$W3/gen::o3cllm-open --setting o3cllm-open-o1c=$W/gen::o3cllm-open-o1c ... --out $W/judge.json
```

### S3 손잡이 튜닝 (R5-C, issue #365)

`bench/r5_tune.py`(새). 규칙은 모듈 머리 주석이 정본이다. 결과: `artifacts/arch-v2/r5-c-tune.md`.

- 사본 0(코디네이터 결정): 입력은 R5-A2 저장 recall(`rep1`), 질의 계획, S4 역할 저장소(`r5-b3-item-roles.sqlite`)뿐이다. 질의마다 R5-A2가 보낸 인자(태그 없음, 지시문 + 줄바꿈 + 문안, 저장 `query_timestamp`)를 다시 만들어 `gateway/item_open_table`의 공개 함수(`read_store`, `wants_table`, `role_states`, `request_repos`, `build_table`)로 표를 만든다. 태그가 없으므로 범위는 뱅크 전체이고 t 의 모든 질의가 같은 표를 받는다(같은 텍스트 1회).
- d-llm 입력 = R5-A2 합집합 -> 시간여행 필터 -> 200,000자 상한 렌더 + 표 텍스트(recall 행 뒤). 표가 없으면 R5-A2 기준선 프롬프트와 바이트가 같다(base1 gen 의 `prompt_text_sha256`으로 확인).
- 손잡이 공간은 코드에 고정이다: C1 a, C2 b(= b+c), C3 a + 소비자 겨냥 머리(원문은 리포 밖 `--c3-header-file`), C4 = 1차 최선 + `attach` status_query. 모두 `recent_done_days` 0, `max_rows` 300, `scope` request_project. C4 는 지출 전에 t별 입력을 기준 조합과 바이트 비교해 같으면 원장에 skip 줄만 남긴다.
- `run`은 조합 x 반복 하나(라벨 `<조합>-r<반복>`)다. 원장(append-only JSONL, 0600) 첫 start 줄의 sha256(하네스·판정 코드·라벨셋·역할 저장소·`gateway/item_open_table.py`·입력)과 반복 시작 전·d-llm 호출 전마다 대조해 다르면 exit 5. 지출은 `SpendGuard`(상한 exit 3), 반복 시작 전 누계가 `--soft-usd`를 넘으면 exit 4. 별칭 표 sha256이 저장소 meta `aliases_sha256`과 다르면 모든 명령이 시작 전에 거부한다. 완료 gen 은 손잡이와 프롬프트 sha256이 같을 때만 건너뛴다.
- `reasons`: #374 `reasons.json`의 판독은 그대로 두고 사유만 S4 상태로 다시 분류한다(LLM 상태 자리에 S4 상태). `cands`가 낸 새 오답 쌍을 워커 1인이 읽어 `--new-reads`로 더한다.
- `judge`: 조합 x 반복마다 P, P_bank, R, R_bank(S4 분모·#374 분모), R_bank2, R_all, 추가 토큰(텍스트·payload), base1 대비 짝 비교, R_bank(S4) 노이즈 바닥, 최선 선택. gen 머리 손잡이로 입력을 다시 만들어 `prompt_text_sha256`을 대조하고, `--run` 라벨이 그 조합의 gen 인지(머리 `combo`, 반복 사이 같은 `knobs_sha256`)와 중복 없는 반복 2개 이상인지 확인한다. `best`는 최선 손잡이를 리포 밖 0600 JSON 으로 쓰고 파일 sha256 과 정규 sha256(감사 줄 `knobs_sha256`)을 낸다.

```bash
O=~/.local/share/hindsight-bench/arch-v2; W=$O/r5-c-work                       # 리포 밖, 0700
TS="--t 2026-09-28 --t 2026-10-01 --t 2026-10-04 --t 2026-10-07 --t 2026-10-10 --t 2026-10-13"
UV="uv run --no-project --python 3.12 --with-requirements gateway/requirements.txt --with tiktoken python -B -m bench.r5_tune"
C="--labelset $O/r5-a1-labelset.jsonl --aliases $O/r2-aliases.json --plan $O/r5-a2-plan.json --recalls $O/r5-a2-work/rep1 \
  --store $O/r5-b3-item-roles.sqlite $TS --c3-header-file $O/r5-c-header-c3.txt"
J="$C --meta $O/r5-a1-meta.jsonl --mentions $O/r5-a3-work/mentions.json --llm-roles $O/r5-a3-work/roles-llm.json"
${=UV} prep ${=C} --families $O/r4-1-families.json --out $W/prep.json                 # 지출 0
${=UV} run ${=C} --combo C1 --rep 1 --k 3 --gen-dir $W/gen --spent-dir $W --ledger $O/r5-c-ledger.jsonl \
  --max-usd 30 --soft-usd 25                                                    # C1 r2, C2, C3, C4(--c4-base) 같은 꼴
${=UV} reasons ${=J} --base-reasons $O/r5-a4-work/reasons.json [--new-reads $W/new-reads.json] --out $W/reasons.json
${=UV} cands ${=J} --gen-dir $W/gen --reasons $W/reasons.json --label C1-r1 --label C1-r2 --out $W/cands.json
${=UV} judge ${=J} --reasons $W/reasons.json --gen-dir $W/gen --base-gen-dir $O/r5-a2-work/gen --base base1 ... --base base5 \
  --run C1=C1-r1,C1-r2 --run C2=C2-r1,C2-r2 --run C3=C3-r1,C3-r2 --ledger $O/r5-c-ledger.jsonl --out $W/judge.json
${=UV} best --judge $W/judge.json --gen-dir $W/gen --out $O/r5-c-knobs.json
```

#### 머리 문구 중간안 C5·C6 (R5-C2, issue #381)

같은 하네스에 조합 C5·C6과 1단계 명령 `outside`를 더했다. 판정·선택 로직(`select_best`, 지표, 사유 분류)은 그대로다. 결과: `artifacts/arch-v2/r5-c2-header.md`.

- C1은 #365 기본 머리를 명시 값(`C1_HEADER`)으로 고정한다. 게이트웨이 기본 머리가 바뀌어도 C1 정규 sha256은 `f7fcf626...` 그대로다.
- C5 = C1 머리 첫 줄 + 지시 1문장(마지막 줄 `줄: {cols}` 유지), C6 = C5 첫 줄 + 지시 1문장. 나머지 손잡이는 C1과 같다.
  원문은 리포 밖 파일(`--c5-header-file`, `--c6-header-file`)이고 텍스트(끝 줄바꿈 제거) sha256이 하네스 상수 `EXPECTED_HEADER_SHA256`과 다르거나 구조가 다르면 모든 명령이 거부한다.
- `outside`(지출 0): C3 저장 gen의 P 오답 쌍마다 그 키가 그 t의 C3 표 열림 행에 있었는지 센다(쌍·고유 키, 전체와 P_bank 분모에 남는 (i)·(iv) 따로). 출력은 수만이다.
- `judge --ref-judge <#365 judge.json>`: C1은 #365 판정 출력을 참고값으로 쓴다(재측정 없음, t·저장소·C1 knobs sha256 대조). `selection_vs_ref`는 자격 조합 중 R_bank(S4) 평균이 C1보다 엄격히 높은 것의 최대, 없거나 동률이면 C1이다.

```bash
O=~/.local/share/hindsight-bench/arch-v2; W=$O/r5-c2-work; W1=$O/r5-c-work; H=$O/r5-c2-headers   # 리포 밖, 0700
C="$C --c5-header-file $H/r5-c2-header-c5.txt --c6-header-file $H/r5-c2-header-c6.txt"          # 위 절의 C 에 더함
J="$C --meta $O/r5-a1-meta.jsonl --mentions $O/r5-a3-work/mentions.json --llm-roles $O/r5-a3-work/roles-llm.json"
${=UV} outside ${=J} --gen-dir $W1/gen --reasons $W1/reasons.json --label C3-r1 --label C3-r2 --out $W/outside.json
${=UV} prep ${=C} --out $W/prep.json                                                        # 지출 0, C5·C6 토큰
${=UV} run ${=C} --combo C5 --rep 1 --k 3 --gen-dir $W/gen --spent-dir $W --ledger $O/r5-c2-ledger.jsonl \
  --max-usd 20 --soft-usd 16                                                                # C5 r2, C6 r1·r2 같은 꼴
${=UV} reasons ${=J} --base-reasons $O/r5-a4-work/reasons.json --new-reads $W/new-reads.json --out $W/reasons.json
${=UV} cands ${=J} --gen-dir $W/gen --reasons $W/reasons.json --label C5-r1 ... --label C6-r2 --out $W/cands.json
${=UV} judge ${=J} --reasons $W/reasons.json --gen-dir $W/gen --base-gen-dir $O/r5-a2-work/gen --base base1 ... --base base5 \
  --run C5=C5-r1,C5-r2 --run C6=C6-r1,C6-r2 --ref-judge $W1/judge.json --ledger $O/r5-c2-ledger.jsonl --out $W/judge.json
```

`new-reads.json`은 #365 새 판독(`r5-c-work/new-reads.json`)에 이 티켓 새 판독을 더한 것이다.

#### S4 역할 판정 개선 (R5-C3, issue #383)

사이드카에 판정 옵션과 오프라인 나열을, 하네스에 표 자체 채점 `table`을 더했다. 기본값(haiku, 문장 입력, 1표)과 선택·지표 로직은 그대로다. 결과: `artifacts/arch-v2/r5-c3-roles.md`.

- 사이드카 옵션: `--model`, `--input sentence|memory`(memory = 기억 전문 + KST 날짜 + 맨 앞 라벨 + 키 목록, (기억, 키)마다 1항목), `--votes N`(엄격한 과반, 없으면 ref), `--prompt-file`(기본이 아닌 프롬프트는 리포 밖), `--spend-log`(호출별 비용·응답 modelUsage, 0600).
  meta 선택 키 `judge_mode`(`input=<방식>;votes=<N>`, 없으면 문장·1표)와 model·prompt_sha256·aliases_sha256 중 하나라도 다르면 poll이 거부한다.
- 오프라인 나열(upstream 0): `--offline-bodies <본문 JSONL> --offline-meta <기억 메타 JSONL> [--mentioned-until YYYY-MM-DD]`, 별칭 표는 `--aliases-file`만.
- `table`(지출 0): `--store`의 C1 표(태그 없는 인자 = 뱅크 전체) 열림 행을 소비자 없이 목록으로 보고 P·P_bank·R·R_bank(분모 = `--store` 열림 / `--ref-store` 열림)를 낸다. 사유 없는 오답 쌍은 수와 판독 근거(리포 밖 `--out`)만 낸다.

```bash
O=~/.local/share/hindsight-bench/arch-v2; W=$O/r5-c3-work                      # 리포 밖, 0700
SC="uv run --no-project --python 3.12 --with-requirements gateway/requirements.txt python -B scripts/item_roles_sidecar.py"
OFF="--aliases-file $O/r2-aliases.json --offline-bodies $O/r5-a3-bodies.jsonl --offline-meta $O/r5-a1-meta.jsonl --mentioned-until 2026-10-13"
${=SC} status --db $O/r5-b3-item-roles.sqlite ${=OFF}                          # 오프라인 나열 동등성(미판정 0)
# 1단계: 표본 기억만의 본문·메타(리포 밖)로 안마다 저장소(S4c 예). S4b = --input sentence, S4d = --votes 3, 상한 = --model claude-opus-5-5
${=SC} backfill --db $W/sample-S4c.sqlite --aliases-file $O/r2-aliases.json --offline-bodies $W/sample-bodies.jsonl \
  --offline-meta $W/sample-meta.jsonl --mentioned-until 2026-10-13 --model claude-sonnet-5-5 --input memory --votes 1 \
  --prompt-file $W/prompt-memory.txt --spend-log $W/roles-llm-sample-S4c.json --max-usd 2 --file-mode 600
# 2단계: 채택안으로 dev 430기억
${=SC} backfill --db $O/r5-c3-item-roles.sqlite ${=OFF} --model claude-sonnet-5-5 --input sentence --votes 1 \
  --spend-log $W/roles-llm-dev-S4b.json --max-usd 5 --file-mode 600
# 3단계: 저장소마다 사유 재분류 -> 표 자체 채점(위 절의 J, ${=UV})
${=UV} reasons ${=J} --store $O/r5-c3-item-roles.sqlite --base-reasons $O/r5-a4-work/reasons.json --new-reads $W/new-reads.json --out $W/reasons-new.json
${=UV} table ${=J} --store $O/r5-c3-item-roles.sqlite --ref-store $O/r5-b3-item-roles.sqlite --reasons $W/reasons-new.json --out $W/table-new.json
```

`J`의 `--store`는 위 절 값 대신 각 명령의 `--store`를 쓴다(뒤 인자가 이긴다). `new-reads.json`은 #381 `new-reads.json`에 이 티켓 표 오답 새 판독을 더한 것이다.

### 종단 확인 (R5-D, issue #366)

`bench/r5_endtoend.py`(테스트 `tests/test_r5_endtoend.py`). S3를 켠 D10 사본의 실제 게이트웨이 응답으로 dev·봉인·원래 증상·키 없는 기준선을 잰다. 결과: `artifacts/arch-v2/r5-report.md`.

- `recall`: `save_recalls`와 같은 인자(`{query, query_timestamp, max_tokens}`, tags 없음)로 질의마다 결과 행과 응답 payload의 `gateway_item_open_table` 객체를 함께 저장한다(사본만).
- `dllm`: 합집합 -> 시간여행 필터 -> 렌더 + 저장 표 `text`(질의 순, 같은 텍스트 1회). gen 머리에 표 sha256과 `prompt_text_sha256`. 지출은 `--spent-dir` 누계로 `--max-usd`, 시작 전 `--soft-usd` 넘으면 시작 안 함.
- 봉인 가드: 봉인 단계는 `recall-on`·`recall-off`·`dllm-on`·`dllm-off` 각 1회(`<work>/sealed-runs.jsonl`, 실행 전에 시작 줄, 성공 뒤 성공 줄). 봉인 t 전체를 정확히 요구한다(아니면 기록 전에 거부). `bodies`는 recall 두 팔, `cands-sealed`·`judge-sealed`는 d-llm 두 팔의 성공 줄 뒤에만.
- `audit`: 측정별 UTC 구간의 감사 줄(decision `item-open-table`) 사유·`knobs_source`·`knobs_sha256` 수. down 전에 돌린다(down이 상태 디렉터리를 지운다).

```bash
O=$HOME/.local/share/hindsight-bench/arch-v2; W=$O/r5-d-work                      # 리포 밖, 0700
E="uv run --python 3.12 --with-requirements gateway/requirements.txt --with tiktoken python bench/r5_endtoend.py"
${=E} sealed-plan --labelset $O/r5-a1-labelset.jsonl --families $O/r4-1-families.json \
  --instruction $O/r4-1-instruction.txt --project-template $O/r5-a2-project-query.txt --out $W/sealed-plan.json
next_env=$(bash scripts/next-stack.sh env) || exit 1; eval "$next_env"                     # 사본 대상
${=E} recall --stage dev --arm on --plan $O/r5-a2-plan.json --t 2026-09-28 ... --out-dir $W/rec-dev-on --work $W
${=E} recall --stage sealed --arm on --plan $W/sealed-plan.json --out-dir $W/rec-sealed-on --work $W
${=E} recall --stage symptom --arm on --symptom-file $W/symptom-q1.txt --out-dir $W/rec-symptom-on --work $W
${=E} recall --stage keyless --arm on --plan $O/r5-a2-plan.json --out-dir $W/rec-keyless-on --work $W
${=E} dllm --stage dev --arm on --recalls $W/rec-dev-on --t 2026-09-28 ... --k 7 --gen-dir $W/gen \
  --spent-dir $W --max-usd 25 --soft-usd 20 --work $W
${=E} judge-dev ${=J} --gen-dir $W/gen --s3-recalls $W/rec-dev-on --base-gen-dir $O/r5-a2-work/gen --reasons <사유> --out $W/judge-dev.json
${=E} judge-sealed --labelset ... --aliases $O/r2-aliases.json --store $O/r5-b3-item-roles.sqlite \
  --recalls-on $W/rec-sealed-on --recalls-off $W/rec-sealed-off --gen-dir $W/gen --reasons <사유> --out $W/judge-sealed.json --work $W
${=E} symptom --gen-dir $W/gen --answer-key $O/r3-setc-answer-key.json --answer-key $O/r4-1-setd.json --out $W/symptom.json
```

`J`는 위 "S3 손잡이 튜닝" 절의 판정 인자다(`--recalls`는 R5-A2 `rep1`, R 분모와 사유 재사용 기준). 원래 증상 경로는 지시문 없이 문안 하나, 기준 시각 `2026-10-28T14:59:59Z`다.

## 런 6 (상위 #386)

### S3 payload 축소 오프라인 판정 (R6-1, issue #387)

`bench/r6_payload.py`(테스트 `tests/test_r6_payload.py`). 지출 0, 사본 0, LLM 호출 0. S3 새 기본 손잡이(attach
status_query + 넓힌 `STATUS_TERMS_DEFAULT`, scope request_or_result_project, payload `{t, fmt, keys, truncated, text}`)를
런 5 저장물에 게이트웨이 공개 함수(`wants_table`·`table_scope`·`build_table`·`payload_table`)로 적용한다. 결과:
`artifacts/arch-v2/r6-1-payload.md`.

- 벤치: 런 5 G3 run 의 cand 저장 결과 행 + 코퍼스 인자(태그 포함, `query_timestamp` 없으면 run meta `bench_clock`)로
  질의마다 부착·사유(`no-status-term`/`no-result-keys`/`request`/`result`)와 추가 payload 토큰(cl100k, o200k). G3 추정 =
  추가 o200k 합 / compare `base_total`(질의별 라운드 중앙값의 합이라 라운드를 곱하지 않는다). 런 5 표로 같은 셈을 한
  보정값이 compare 기록 증가율과 맞는지 함께 낸다.
- dev 상태 질의: 저장된 `rec-dev-on` t 마다 `지시문 + 줄바꿈 + 문안`(태그 없음) 부착률, 문안만의 부착률, 새 범위 키
  합집합과 런 5 C1 저장 표의 키 수·자카드·빠진 키, 표 텍스트(서로 다른 텍스트를 이은 것)·질의당 payload 토큰.
- 원래 증상: `rec-symptom-on` 문안 하나(sha 대조)의 부착·키 수·토큰.
- 봉인 경로(`sealed` 가 든 이름)는 exit 2 로 거부한다. 출력은 리포 밖 0600 JSON, 표준 출력은 수치·sha256 만.

```bash
O=$HOME/.local/share/hindsight-bench/arch-v2; W=$O/r6-1-work; D=$O/r5-d-work            # 리포 밖, 0700
uv run --python 3.12 --with-requirements gateway/requirements.txt --with tiktoken python -I bench/r6_payload.py \
  --store $O/r5-b3-item-roles.sqlite --aliases $O/r2-aliases.json \
  --bench-run <운영 체크아웃>/bench/.data/runs/r5d-g3-r1 --bench-corpus <운영 체크아웃>/bench/.data/dev.jsonl \
  --bench-compare $D/bench/compare-r5d-g3-r1.json --plan $O/r5-a2-plan.json --dev-recalls $D/rec-dev-on \
  --symptom-recall $D/rec-symptom-on --symptom-file $D/symptom-q1.txt --out $W/payload.json
```

### 종단 재확인 (R6-2, issue #388)

`bench/r6_endtoend.py`(테스트 `tests/test_r6_endtoend.py`). R6-1 머지 코드의 S3 를 D10 사본 **같은 세션 짝**(켬 -> 플래그만
끄고 `rebuild` -> 끔)으로 잰다. recall·d-llm·원래 증상 채점은 `bench/r5_endtoend.py`(`recall`·`dllm`·`symptom`)를 그대로
쓰고, 이 모듈은 R6-1 이 바꾼 것만 더한다. 결과: `artifacts/arch-v2/r6-report.md`.

- `knobs`: R6-1 기본 손잡이를 명시한 파일(머리는 `HEADER_R6_1` 명시, 정규 sha256 `0ca334da...` 대조, 리포 밖 0600).
  `--c7-header-file`이면 머리만 C7 문안(원문 리포 밖, 텍스트 sha256 `C7_HEADER_SHA256` 고정)으로 바꾼다. R6-3(#391) 뒤
  게이트웨이 기본 머리가 C7이라 C7 손잡이는 새 기본값(정규 sha256 `24044afd...`)과 같다(`artifacts/arch-v2/r6-3-header.md`).
  사본은 `<state>/policies/item-open-table.json`(감사 줄 `knobs_source` file), 벤치 cand 는 `--env HC_ITEM_OPEN_TABLE_KNOBS=<압축 JSON>`(env).
- `audit`: 측정별 UTC 구간의 감사 줄. S3 켬 구간은 `attached` = 상태 어휘가 있고 결과에 키가 있는 recall 수, `attached` +
  `not_attached`(`no-status-term`/`no-result-keys`) = recall 수, 사유가 그 둘뿐, `knobs_sha256` 하나 = 기대값을 본다
  (`audit_check`). 구간에 `recalls_dir`·`plan`(원래 증상은 `symptom_file`)을 주면 저장 표 수와, 저장 질의·결과 행으로 다시 한
  게이트웨이 판정(`wants_table`·`table_scope`)까지 대조한다. down 전에 돌린다.
- `judge-pair`: 같은 세션 두 팔의 P·P_bank·R·R_bank(S4·#374 분모)·R_all·G1, 켬 - 끔과 두 팔의 base1 대비 짝 차(키 군집 재표집,
  씨앗 20261101, 2000회), 추가 토큰((i) 이은 표 텍스트 / d-llm 입력, (ii) 질의당 payload 최대 / d-llm 입력), R 누락의 표 안(사유별)·
  C1 대비 빠진 키·표 밖. 판정 입력(R 분모, 사유)은 #365·#366 과 같은 `JudgeCtx`(R5-A2 `rep1`)다.
- `c7-verdict`: C7 두 회 모두 P_bank >= 95% 이고 R_bank(S4) 평균 >= 측정 1 켬 값이면 `replace-recommended`.

```bash
O=$HOME/.local/share/hindsight-bench/arch-v2; W=$O/r6-2-work; D=$O/r5-d-work                 # 리포 밖, 0700
E5="uv run --python 3.12 --with-requirements gateway/requirements.txt --with tiktoken python bench/r5_endtoend.py"
E6="uv run --python 3.12 --with-requirements gateway/requirements.txt --with tiktoken python bench/r6_endtoend.py"
${=E6} knobs --out $W/knobs-default.json; ${=E6} knobs --c7-header-file $W/r6-2-header-c7.txt --out $W/knobs-c7.json
next_env=$(bash scripts/next-stack.sh env) || exit 1; eval "$next_env"                         # 사본 대상
${=E5} recall --stage dev --arm on --plan $O/r5-a2-plan.json --t 2026-09-28 ... --out-dir $W/rec-dev-on --work $W
${=E5} recall --stage symptom --arm on --symptom-file $D/symptom-q1.txt --out-dir $W/rec-symptom-on --work $W
# C7(선택): 손잡이 파일만 C7 로 바꾸고 같은 세션에서 --out-dir $W/rec-c7-on, 다시 기본 파일로
# 끔: 플래그 없이 rebuild 뒤 --arm off 로 rec-dev-off, rec-symptom-off. 감사 줄 집계는 down 전에
${=E6} audit --log <state>/audit/gateway.log --windows $W/audit-windows.json --aliases $O/r2-aliases.json --out $W/audit.json
${=E5} dllm --stage dev --arm on --recalls $W/rec-dev-on --t ... --k 7 --gen-dir $W/gen --spent-dir $W --max-usd 30 --soft-usd 24 --work $W
${=E6} judge-pair ${=J} --on-gen-dir $W/gen --on-recalls $W/rec-dev-on --off-gen-dir $W/gen --off-recalls $W/rec-dev-off \
  --base-gen-dir $O/r5-a2-work/gen --c1-recalls $D/rec-dev-on --reasons <사유> --out $W/judge-pair.json
${=E5} symptom --gen-dir $W/gen --answer-key $O/r3-setc-answer-key.json --answer-key $O/r4-1-setd.json --out $W/symptom.json
```

`J`는 "S3 손잡이 튜닝" 절의 판정 인자다. C7 의 d-llm 은 반복마다 다른 `--gen-dir`(라벨은 `dev-on` 그대로)로 돌린다.
벤치 G3 는 "1단계 arm 벤치" 절 명령으로 세트 s1(D10) 하나에 팔 base / cand(같은 ref)를 세우고, 역할 저장소는 **gw-up 뒤**
cand 추출본 `policies/item-roles.sqlite`로 복사한다. cand 감사 줄은 gw-down 전에 컨테이너 안 `/audit/gateway.log`에서 받아
`audit`(구간에 runner cand recall 수)으로 본다. 벤치 일반 질의는 대부분 `not-attached`가 기대값이다.

## 런 7 (상위 #393)

### 표 키 전체 GitHub 대조 평가 (R7-0, issue #394)

`bench/r7_eval.py`(테스트 `tests/test_r7_eval.py`). 지출 0, 사본 0, LLM 0, GitHub 는 GET 만. 런 5·6 P_bank(라벨셋 안 키만)
대신 S3 표에 오른 **모든** 키를 기준일 t 시점 GitHub 상태와 대조한다. 결과: `artifacts/arch-v2/r7-0-eval.md`. R7-2·R7-3 이
같은 하네스·캐시·리포 코드를 쓴다.

- 표: 역할 저장소를 게이트웨이 공개 함수(`read_store`·`role_states`·`build_table`)로 읽어 뱅크 전체 표(`scope` all,
  `recent_done_days` 0, `max_rows` 1000)를 만들고 열림 키 전부를 센다. hindsight-crew 꼴 = 같은 표의 hindsight-crew 키.
  `role_states` 호출은 `store_view` 한 곳이고 meta 를 함께 돌려준다. 저장소는 `mode=ro` 로만 연다.
- 대조: 리포마다 `issues?state=all`(PR 포함, 404 = 리포 없음)과 리포 단위 `issues/events`(closed·reopened) 한 번씩
  (`gh api --paginate --slurp`). 범주 open_issue / closed_completed / closed_not_planned / pr_open / pr_merged / pr_closed /
  missing_number / missing_repo / not_yet / reopened_unknown. 표 정밀도 = open_issue / 표 키, 회수율 = t 시점 열린 이슈(PR
  제외, 표 리포 + 별칭 표 정규 리포 중 존재) 중 표에 오른 것, 보조 회수율 = 분모를 저장소에 키가 있는 이슈로 좁힌 값.
- 원인(오류 키마다, 중복 셈 + 주 원인): 1 범위 표기 / 2 같은 시각 동점 / 3 끝냄 뒤 open / 4 번호만 규칙(외부 문맥 낱말·명시
  표기 최대 번호 초과) / 5 PR 표기 / 데이터층 / 기타. 1·4·5 는 기억 본문으로 가른다(dev = 런 5 본문 덤프·메타, 운영 =
  `ops-listing` 저장본).
- 봉인: 입력 이름에 `sealed`, dev t 가 10-13 뒤, 운영 t 가 10-14 ~ 10-28 이면 exit 2. 별칭 정규 sha256 이 저장소 meta 와
  다르면 exit 3. 출력은 리포 밖 0600 JSON(캐시·리포 코드·키별 상세·요약), 표준 출력은 수치·sha256 만.

```bash
O=$HOME/.local/share/hindsight-bench/arch-v2; W=$O/r7-work            # 리포 밖, 0700
PROD="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")"
BANK="$(grep -E '^HC_PERSONAL_BANK=' "$PROD/.env.host.local" | cut -d= -f2-)"
# 운영 저장소 사본은 한 번만 만든다(W/ops-store-<KST>.sqlite + W/ops-store-copy.json 에 sha256·복사 시각)
# 운영 기억 나열: memories/list GET 1회(운영 쓰기 0), 있으면 재사용
HC_ENV_FILE="$PROD/.env.local" HC_COMPOSE_PROJECT=hindsight-crew \
  uv run --python 3.12 --with-requirements gateway/requirements.txt python -I bench/r7_eval.py ops-listing \
  --bank "$BANK" --out $W/ops-listing.jsonl
uv run --python 3.12 --with-requirements gateway/requirements.txt python -I bench/r7_eval.py run \
  --ops-store $W/ops-store-<KST>.sqlite --ops-copy $W/ops-store-copy.json --ops-listing $W/ops-listing.jsonl \
  --dev-store $O/r5-b3-item-roles.sqlite --plan $O/r5-a2-plan.json --bodies $O/r5-a3-bodies.jsonl \
  --meta $O/r5-a1-meta.jsonl --aliases $O/r2-aliases.json --owners $O/r5-a1-github.json \
  --codes $O/r5-a1-repo-codes.json --work $W            # 캐시만으로 다시 계산: --offline
```

### 수집 켬·끔 오프라인 측정 (R7-3 오프라인 단계, issue #397)

`bench/r7_eval.py offline`(테스트 `tests/test_r7_eval_v2.py`). 사본 0, d-llm 0, GitHub 는 GET 만. 결과:
`artifacts/arch-v2/r7-report.md`. R7-0 운영 저장소 사본을 v2 키로 다시 만들고(`rekey`, haiku 새 키 판정만), 그 결과
사본에 GitHub 근거를 모은 뒤, 판본마다 게이트웨이 공개 함수로 표를 만들어 R7-0 캐시(t 시점 GitHub)와 대조한다.

- 판본(`--arm 라벨 저장소 use|ignore`, 여러 번): 손잡이 `github_evidence` 와 같다. use 면 `read_store(..., GITHUB_ALL)` 로
  근거 테이블을 읽고, 테이블이 있으면 `build_table(..., github=...)` 3상태 표다. ignore 면 근거를 읽지 않는다(지금 모양 표).
  `role_states` 에는 저장소 meta `key_rules` 를 넘긴다(v2 동점 = 끝남).
- 분모: 3상태 표는 `열림 후보` 줄(payload `keys`), 그 밖은 열린 키 전부(R7-0 과 같은 뜻). 전체 줄의 줄 상태 x t 시점 범주와
  미확인·충돌 사유별 수는 보조로 낸다. 회수율 분모 리포 = R7-0 캐시 리포 + 판본 표 리포 + 별칭 표 정규 리포.
- 하위군: GitHub 연결 키(평가 캐시에 리포가 있음, 200) / GitHub 없는 키(리포 404·미확인). 번호 없는 일은 종단 단계 몫이다.
- 한계 수: `reopen`(#396 final-review #3, `열림 후보` 인데 평가는 t 시점 닫힘, 그중 t 뒤 재오픈 이벤트), `slug`(#395
  final-review #8, repo-missing 줄 중 리포 이름에 `_`·`.` 이 든 줄).
- 감사: 판본마다 저장소 파일 sha256(읽기 전후), meta `key_rules`, GitHub 테이블 존재, `--work` 안 여부.
- 캐시: R7-0 캐시(`--github-cache`)는 읽기만 하고, 없는 리포·이벤트를 채우면 `--work/r7-3-github.json` 에 쓴다(`--offline` 이면
  호출 0, 없는 리포는 exit 1). 봉인 입력·t 는 exit 2, 별칭 정규 sha256 불일치는 exit 3. 출력 `r7-3-summary.json`(코드·수치)·
  `r7-3-details.json`(키·줄, 리포 밖)·`r7-3-repo-codes.json`, 표준 출력은 수치·sha256 만.

```bash
O=$HOME/.local/share/hindsight-bench/arch-v2; W=$O/r7-work            # 리포 밖, 0700
PY="uv run --python 3.12 --with-requirements gateway/requirements.txt python -B"
# 1) v2 재키잉(원본 사본 불변, 새 파일). 중첩 claude -p 토큰은 사이드카가 자식 env 에만 넣는다
${=PY} scripts/item_roles_sidecar.py rekey --db $W/ops-store-<KST>.sqlite --out $W/r7-3-store-v2.sqlite --rules v2 \
  --offline-bodies $W/ops-listing.jsonl --offline-meta $W/ops-listing.jsonl --aliases-file $O/r2-aliases.json \
  --max-usd 1 --spend-log $W/r7-3-rekey-spend.json --file-mode 600
# 2) 수집 켬 사본: 복사본에 poll --github(나열은 저장소 기억 id 만 남긴 사본이라 판정 0, --max-usd 0 은 가드)
cp -p $W/r7-3-store-v2.sqlite $W/r7-3-store-v2-gh.sqlite
${=PY} scripts/item_roles_sidecar.py poll --db $W/r7-3-store-v2-gh.sqlite --rules v2 \
  --offline-bodies $W/r7-3-listing-store-only.jsonl --offline-meta $W/r7-3-listing-store-only.jsonl \
  --aliases-file $O/r2-aliases.json --max-usd 0 --github --github-owners $W/r7-3-owners.json --github-default-owner <owner>
# 3) 측정: 켬 / 끔 / 수집 전 / v1(R7-0 재현)
${=PY} bench/r7_eval.py offline --arm on $W/r7-3-store-v2-gh.sqlite use --arm off $W/r7-3-store-v2-gh.sqlite ignore \
  --arm pre $W/r7-3-store-v2.sqlite use --arm v1 $W/ops-store-<KST>.sqlite ignore \
  --ops-copy $W/ops-store-copy.json --ops-listing $W/ops-listing.jsonl --aliases $O/r2-aliases.json \
  --codes $W/r7-repo-codes.json --github-cache $W/r7-0-github.json --baseline $W/r7-0-summary.json --work $W
```

### 원래 증상 새 채점식·저장소 준비·수정 전후 렌더 (R7-3a 단계 0, issue #403)

`bench/r7_endtoend.py`(테스트 `tests/test_r7_endtoend.py`). 지출 0(LLM·d-llm 0), 사본 0, GitHub 는 GET 만. 결과:
`artifacts/arch-v2/r7-stage0.md`. 단계 A(#397)가 같은 `score` 로 새 출력을 채점한다.

- `score`: 원래 증상 열린 목록(status open) 전체가 분모다. 정답표 대응 없음·PR·없는 번호·키 없는 일도 넣고 범주로 판정한다
  (open = 분자, finished, wrong, hold = 판정 보류). 키 있고 GitHub 연결(평가 캐시 리포 200)이면 t 시점 범주(`r7_eval.item_at`),
  GitHub 없는 키는 미확인(P_key 분모에만, 저장소 상태·근거 규칙은 보조 열), 키 없는 일은 근거 규칙(evidence_ids 기억 중 quote 문장의 규칙 역할, 가장 늦은
  열림·끝남 신호, 없으면 보류. LLM 판정은 인터페이스만). P_all, P_key(키 있는 항목), P_key_linked, 키 없는 항목 수·끝남·보류,
  표 밖 끝남 키 유입(키가 t 시점 GitHub 끝남이고 그 팔 응답 표에 `확인된 종결`·`종결(미계획)` 줄이 없음, 반복당 평균),
  항목 분해(`키 유무|표 줄 상태|뱅크 상태|GitHub 범주`), 옛 `symptom_score` 를 나란히 낸다.
- `add-github`: 저장소를 새 경로(`--work` 안)로 복사하고 #395 `collect_github`(GET)로 근거 테이블만 더한다. 복사 전후
  memories·mention_roles·meta 행 수·행 sha256 이 같아야 한다(`r7-s0-add-github.json`).
- `render`: 저장 recall 응답마다 게이트웨이 순서(`wants_table` -> `table_scope` -> `github_read_scope` -> `read_store` ->
  `rescope_for_key_rules` -> `build_table`)로 수정 전(`--before-module`, `git show <sha>:gateway/item_open_table.py` 를 리포 밖에
  뜬 파일)·수정 후(지금 게이트웨이, #402 결과 행 키·읽는 시각을 넘김) 표를 만들고, 결과 행에 보이는 키(#402 `result_keys`
  규칙) 중 t 시점 GitHub 끝난 키인데 반증 줄이 없는 수와 사유(줄 상태, 줄 없음 x 뱅크 상태)를 센다. 손잡이는 기본값 +
  `github_max_age_hours` 0(저장 근거가 오래돼 기본 상한이면 전부 stale 로 덮인다).
- 반복과 응답: 팔당 저장 응답 1개면 모든 반복에, 반복 수와 같으면 반복마다 그 응답 표, 그 밖은 멈춘다.
- 캐시: `--github-cache` 는 읽기만 한다. 넓히면 명령별 파일(`score` = `--work/r7-s0-score-github.json`, `render` =
  `--work/r7-s0-render-github.json`)에 쓰고 `base_sha256` 을 남긴다. 다음 실행은 그 파일이 같은 `--github-cache` 에서 넓힌
  상위 집합(리포·상태·항목 번호)일 때만 읽고, 아니면 멈춘다. 봉인 경로 exit 2. 출력은 리포 밖 0600, 표준 출력은 수치·sha256 만.

```bash
O=$HOME/.local/share/hindsight-bench/arch-v2; W=$O/r7-work; S=$O/r6-2-work            # 리포 밖, 0700
PY="uv run --python 3.12 --with-requirements gateway/requirements.txt python -I"
# 1) 런 6 원래 증상 재채점(저장 출력만)
${=PY} bench/r7_endtoend.py score --gen-dir $S/gen --recalls on=$S/rec-symptom-on --recalls off=$S/rec-symptom-off \
  --answer-key $O/r3-setc-answer-key.json --answer-key $O/r4-1-setd.json --store $W/r7-s0-r5-src.sqlite \
  --aliases $O/r2-aliases.json --github-cache $W/r7-0-github.json --work $W
# 2) 런 5 저장소 사본(원본 복사본)에 근거 테이블만. v2 재키잉 저장소는 보조
${=PY} bench/r7_endtoend.py add-github --pair $W/r7-s0-r5-src.sqlite $W/r7-s0-store-r5-gh.sqlite \
  --aliases $O/r2-aliases.json --owners $W/r7-3-owners.json --default-owner-file $W/r7-3-default-owner.txt --work $W
# 3) 수정 전후 렌더(수정 전 모듈은 리포 밖 임시 파일)
git show <수정 전 sha>:gateway/item_open_table.py > $W/r7-s0-ot-before.py
${=PY} bench/r7_endtoend.py render --store $W/r7-s0-store-r5-gh.sqlite --recalls $S/rec-symptom-on --recalls $S/rec-dev-on \
  --plan $O/r5-a2-plan.json --symptom-file $O/r5-d-work/symptom-q1.txt --aliases $O/r2-aliases.json \
  --github-cache $W/r7-s0-github.json --before-module $W/r7-s0-ot-before.py --work $W --label render --offline
# (r7-s0-github.json = R7-0 캐시를 처음 렌더 때 넓힌 것. score 와 render 는 어느 순서로 돌려도 된다)
```

### 원래 증상 종단 3팔 + 벤치 G3 (R7-3 단계 A, issue #397)

`bench/r7_endtoend.py`(테스트 `tests/test_r7_endtoend.py` 단계 A 절). 설계는 R6-2 와 같다: D10 사본 한 세션에서 팔마다
recall 1회(저장) + d-llm K=7. recall·d-llm 은 `bench/r5_endtoend.py`(`recall`·`dllm`)를 그대로 쓰고 채점은 단계 0 `score`
(P_key 분자 = GitHub 확인 열린 이슈)다. 결과: `artifacts/arch-v2/r7-report.md` "종단 단계 A" 절.

- 팔: 켬(S3 켬, `knobs --github use`) / 켬-ignore(같은 저장소, `knobs --github ignore`, 손잡이 파일만 바꿔 재기동 없음) /
  끔(플래그 없이 `rebuild`). 켬-ignore 는 r5 `dllm --arm on` 으로 따로 `--gen-dir` 에 만들고 `score --gen ignore=<파일>` 로
  넘긴다(생성 머리 `recall_sha256` = 그 팔 recall 파일 sha256 이어야 한다).
- `knobs`: 게이트웨이 기본값(후보형 머리, fmt a) + #402 선택 키 명시(`done_closed_rows` result, `github_max_age_hours` 2) +
  `github_evidence`. 표준 출력에 파일·정규 sha256.
- 저장소: 런 5 역할 저장소 사본(새 파일) -> 사본이 뜬 뒤 `HC_TARGET=next` 나열로 `rekey --rules v2`(`--save-listing`) ->
  복사본에 `poll --github`(나열은 저장소 기억 id 만 남긴 사본이라 판정 0, `--max-usd 0`) -> `<state>/policies/item-roles.sqlite`.
  rekey plan 줄 `source_memories`(런 5 545)와 `unlisted` 0 으로 나열 범위가 런 6 과 같은지 본다. 수집 시각에서 2시간 안에 켬
  팔 recall 을 끝낸다(넘으면 측정 직전 다시 수집).
- `audit`: 구간마다 감사 줄을 세고 켬·켬-ignore 는 `audit_check_v2` 로 본다. `attached` 와 하위 사유(`no-status-term` /
  `no-result-keys` / `no-result-keys-v2`)별 수 = 저장 질의·결과 행으로 다시 한 게이트웨이 판정(`wants_table` -> `table_scope` ->
  저장소 meta key_rules 로 `rescope_for_key_rules`), `attached` = 저장 표 수, `knobs_sha256` 하나 = 손잡이 파일 정규 sha256,
  `github_evidence` = 팔 값, 저장소를 연 줄마다 `key_rules` v2, use 면 attached 줄마다 `counts`·`github_fetched_at` 과
  `unknown_reasons` stale 0, ignore 면 둘 다 없음. 구간에 `store` 를 주면 구간 끝 시점 `store-check` 를 싣는다. 끔 구간은 줄 0.
  down 전에 돌린다(down 이 상태 디렉터리를 지운다).
- `store-check`: 저장소 파일을 직접 읽어 sha256, 행 수·행 sha256, meta key_rules·aliases_sha256, GitHub 테이블 리포 상태별
  수·항목 수, `github_fetched_at`(가장 이른 리포 시각)과 `--at` 시점 나이·stale 리포 수(게이트웨이 `stale_repos`).
- `g3-tables`: 벤치 run 디렉터리의 측정 호출(라운드 중앙값, `arm_compare` 와 같은 셈)과 cand 저장 결과로 부착 질의(결과에
  표 객체가 있는 질의)만 따로 토큰 증감 합·질의당 평균·최대와 표 payload 토큰(o200k)을 낸다.
- G3: hc-next 를 내리고 d-llm 이 끝난 뒤 R6-2 G3 와 같은 인자(세트 s1 = D10 하나에 base / cand, `--split dev --limit 40
  --seed 13 --rounds 3 --clock-from-set s1`, `arm_compare --speed-threshold 0.03 --p95-threshold 0.06`). cand 는
  `--env HC_ITEM_OPEN_TABLE_KNOBS=<켬 손잡이 압축 JSON>`, 저장소는 gw-up 뒤 cand 추출본 `policies/item-roles.sqlite` 로 복사한다.
  run 시작·끝에 `store-check` 로 근거 나이가 2시간 안인지 본다.

```bash
O=$HOME/.local/share/hindsight-bench/arch-v2; W=$O/r7-a-work; D=$O/r5-d-work; R7=$O/r7-work      # 리포 밖, 0700
E7="uv run --python 3.12 --with-requirements gateway/requirements.txt python -I bench/r7_endtoend.py"
E5="uv run --python 3.12 --with-requirements gateway/requirements.txt --with tiktoken python bench/r5_endtoend.py"
PY="uv run --python 3.12 --with-requirements gateway/requirements.txt python -B"
${=E7} knobs --github use --out $W/knobs-on.json; ${=E7} knobs --github ignore --out $W/knobs-ignore.json
cp -p $O/r5-b3-item-roles.sqlite $W/r7a-r5-src.sqlite
# 사본 up(D10) 뒤, 사본 대상
next_env=$(bash scripts/next-stack.sh env) || exit 1; eval "$next_env"
${=PY} scripts/item_roles_sidecar.py rekey --db $W/r7a-r5-src.sqlite --out $W/r7a-store-v2.sqlite --rules v2 --bank "$BANK" \
  --aliases-file $O/r2-aliases.json --max-usd 0.5 --spend-log $W/r7a-rekey-spend.json --file-mode 600 \
  --save-listing $W/r7a-listing-bodies.jsonl $W/r7a-listing-meta.jsonl
# (나열에서 저장소 기억 id 만 남긴 r7a-listing-store-only-{bodies,meta}.jsonl 을 만든다)
cp -p $W/r7a-store-v2.sqlite $W/r7a-store-v2-gh.sqlite
${=PY} scripts/item_roles_sidecar.py poll --db $W/r7a-store-v2-gh.sqlite --rules v2 --bank "$BANK" \
  --offline-bodies $W/r7a-listing-store-only-bodies.jsonl --offline-meta $W/r7a-listing-store-only-meta.jsonl \
  --aliases-file $O/r2-aliases.json --max-usd 0 --file-mode 600 --github --github-owners $R7/r7-3-owners.json \
  --github-default-owner "$(cat $R7/r7-3-default-owner.txt)"
${=E7} store-check --store $W/r7a-store-v2-gh.sqlite --out $W/r7a-store-v2-gh-check.json
# 저장소·켬 손잡이·r2 별칭을 <state>/policies 에 넣고 HC_ITEM_OPEN_TABLE=1 HC_ITEM_OPEN_TABLE_BANK=<bank> rebuild
${=E5} recall --stage symptom --arm on --symptom-file $D/symptom-q1.txt --out-dir $W/rec-symptom-on --work $W
# 손잡이 파일만 knobs-ignore.json 으로
${=E5} recall --stage symptom --arm on --symptom-file $D/symptom-q1.txt --out-dir $W/rec-symptom-ignore --work $W
# 플래그 없이 rebuild
${=E5} recall --stage symptom --arm off --symptom-file $D/symptom-q1.txt --out-dir $W/rec-symptom-off --work $W
${=E7} audit --log <state>/audit/gateway.log --windows $W/audit-windows.json --aliases $O/r2-aliases.json --out $W/audit.json
# down 뒤. d-llm(자식 env 에만 CLAUDE_CODE_OAUTH_TOKEN), 팔마다 gen 디렉터리
${=E5} dllm --stage symptom --arm on --recalls $W/rec-symptom-on --k 7 --gen-dir $W/gen-on --spent-dir $W --max-usd 4 --soft-usd 3.5 --work $W
${=E5} dllm --stage symptom --arm on --recalls $W/rec-symptom-ignore --k 7 --gen-dir $W/gen-ignore --spent-dir $W --max-usd 4 --soft-usd 3.5 --work $W
${=E5} dllm --stage symptom --arm off --recalls $W/rec-symptom-off --k 7 --gen-dir $W/gen-off --spent-dir $W --max-usd 4 --soft-usd 3.5 --work $W
${=E7} score --gen-dir $W/gen-on --gen ignore=$W/gen-ignore/symptom-on-2026-10-28-gen.json --gen off=$W/gen-off/symptom-off-2026-10-28-gen.json \
  --recalls on=$W/rec-symptom-on --recalls ignore=$W/rec-symptom-ignore --recalls off=$W/rec-symptom-off \
  --answer-key $O/r3-setc-answer-key.json --answer-key $O/r4-1-setd.json --store $W/r7a-store-v2-gh.sqlite \
  --aliases $O/r2-aliases.json --github-cache $R7/r7-0-github.json --work $W
# d-llm 이 끝난 뒤 G3(1단계 arm 벤치 절 명령, 위 인자), 끝나고
${=E7} g3-tables --run <운영 체크아웃>/bench/.data/runs/<run id> --out $W/bench/g3-tables-<run id>.json
```

### dev 짝 S3 켬·끔 K=7 (R7-3 단계 B, issue #397)

`bench/r7_endtoend.py dev-score`(테스트 `tests/test_r7_endtoend.py` 단계 B 절). 설계는 R6-2 dev 측정과 같다: D10 사본 한
세션에서 R5-A2 계획 dev t 6개 상태 질의(t 마다 16질의) recall 을 켬(`knobs --github use`) -> 플래그 없이 `rebuild` -> 끔으로
1회씩 저장하고 d-llm K=7. 저장소 구성은 단계 A 와 같다(런 5 저장소 사본 -> 사본 나열 `rekey --rules v2` -> 복사본 `poll --github`,
켬 팔 recall 은 수집 2시간 안). 결과: `artifacts/arch-v2/r7-report.md` "종단 단계 B" 절.

- P(항목 단위): 단계 A `score_arm` 판정을 t 마다(t = 그 recall 의 `query_timestamp`) 돌려 풀링. P_key 분자 = t 시점 GitHub 열린
  이슈(평가 캐시 = R7-0 캐시, 목록 키 리포가 없으면 GET 으로 넓힌 `--work/r7-b-dev-github.json`).
- R(쌍 단위): 분모 = t 시점 실제 열린 이슈(PR 제외, 별칭 표 정규 리포 + 유효 반복 목록 키 리포 중 캐시 200 리포, R7-0 회수율과 같은 정의),
  (키, t) 쌍 상태 = K 반복 과반(런 5·6 `dllm_pair_state`). R 켬 - 끔 = 키 군집 재표집(`r5_metrics.cluster_bootstrap`, 씨앗
  20261101, 2000회), 반폭 = 95% 구간 폭 / 2.
- 하위군: GitHub 연결 키(P = P_key_linked, R = 위 R) / GitHub 없는 키(P 분모와 뱅크·근거 규칙 판정, R 참고 = 런 5 라벨셋
  `--labelset` dev 대조 쌍 중 미연결 키) / 번호 없는 일(P 분모와 근거 규칙 판정, R 분모 없음).
- 소비자가 `미확인·충돌` 키를 열림으로 올린 수(`unknown_raised`) = 그 팔 응답 표에서 그 키 줄이 `미확인·충돌` 인 열린 항목 수.
- 최소 검출 차: 같은 명령을 런 6 R6-2 dev 켬·끔 저장 생성물에 먼저 돌려(`--out-name r7-b-mdd-r6`, 저장소 = 런 5 저장소) 반폭을 적는다.
- 옛 P·P_bank·R·R_bank(S4)는 R6-2 `judge-pair`(같은 `J` 인자, 동결 판정)를 새 생성물에 돌린 참고값이다.

```bash
O=$HOME/.local/share/hindsight-bench/arch-v2; W=$O/r7-b-work; R6=$O/r6-2-work; R7=$O/r7-work     # 리포 밖, 0700
E7="uv run --python 3.12 --with-requirements gateway/requirements.txt python -I bench/r7_endtoend.py"
E5="uv run --python 3.12 --with-requirements gateway/requirements.txt --with tiktoken python bench/r5_endtoend.py"
TS="--t 2026-09-28 --t 2026-10-01 --t 2026-10-04 --t 2026-10-07 --t 2026-10-10 --t 2026-10-13"
# 0) 최소 검출 차(사본 0)
${=E7} dev-score --gen on=$R6/gen --gen off=$R6/gen --recalls on=$R6/rec-dev-on --recalls off=$R6/rec-dev-off \
  --store $O/r5-b3-item-roles.sqlite --aliases $O/r2-aliases.json --github-cache $R7/r7-0-github.json \
  --labelset $O/r5-a1-labelset.jsonl --work $W --out-name r7-b-mdd-r6
# 1) 사본 up(D10) -> 단계 A 절 저장소 구성(rekey v2 -> 저장소 기억 나열 -> poll --github -> store-check) -> 켬 rebuild
${=E5} recall --stage dev --arm on --plan $O/r5-a2-plan.json ${=TS} --out-dir $W/rec-dev-on --work $W
bash scripts/next-stack.sh rebuild                                                     # 플래그 없이 = 끔
${=E5} recall --stage dev --arm off --plan $O/r5-a2-plan.json ${=TS} --out-dir $W/rec-dev-off --work $W
${=E7} audit --log <state>/audit/gateway.log --windows $W/audit-windows.json --aliases $O/r2-aliases.json --out $W/audit.json
# (창의 on 에 plan·recalls_dir·knobs_file·store) down 전에. 2) down 뒤 d-llm(자식 env 에만 CLAUDE_CODE_OAUTH_TOKEN)
${=E5} dllm --stage dev --arm on --recalls $W/rec-dev-on ${=TS} --k 7 --gen-dir $W/gen --spent-dir $W --max-usd 14 --soft-usd 12 --work $W
${=E5} dllm --stage dev --arm off --recalls $W/rec-dev-off ${=TS} --k 7 --gen-dir $W/gen --spent-dir $W --max-usd 14 --soft-usd 12 --work $W
${=E7} dev-score --gen on=$W/gen --gen off=$W/gen --recalls on=$W/rec-dev-on --recalls off=$W/rec-dev-off \
  --store $W/r7b-store-v2-gh.sqlite --aliases $O/r2-aliases.json --github-cache $R7/r7-0-github.json \
  --labelset $O/r5-a1-labelset.jsonl --work $W
```

### 운영 시점 연막 실행기 + 사전 연막 드라이런 (R7-4, issue #408)

`bench/r7_endtoend.py` `smoke-recall`·`smoke-attach`·`smoke-dllm`·`smoke`·`owners-check`(테스트 `tests/test_r7_endtoend.py` #408 절).
원래 증상 경로를 임의 기준 시각 t0 로 돌린다. 결과: `artifacts/arch-v2/r7-preflight.md`.

- t0: recall 요청 시각(저장 recall `query_timestamp`), 소비자 프롬프트 기준일(생성 머리 `t` = t0 의 KST 날짜, 프롬프트를 다시
  조립해 `prompt_text_sha256`·`table_sha256` 대조), 채점 t0(`--t0`, 생성 머리 `t0`)가 같아야 한다(코드 `t0-request`·`t0-prompt`·
  `t0-score`). `score` 도 `--t0`(기본 10-28 `SYMPTOM_TS`)를 받고 생성·recall 파일 이름의 날짜를 t0 에서 정한다. 기본값으로는
  단계 A 저장 생성물의 요약이 바이트 그대로다.
- `smoke-recall`: 게이트웨이 MCP `recall` 읽기만. 질의마다 `--out-dir/<이름>.json`(0600), 모든 질의의 `query_timestamp` = t0
  (요청 시각, 초 단위). 운영은 `--allow-prod --read-only` 둘 다 있어야 한다(`arch_v2_regression.resolve_target`).
- `smoke-attach`: 저장 recall 질의마다 게이트웨이 순서(`render`와 같은 공개 함수)로 역할 저장소 표를 다시 만들어 붙인다.
  손잡이 = 단계 A 켬 손잡이(`knobs --github use` 값), 결과 행 키와 읽는 시각 t0 를 넘긴다(근거 나이 상한 2시간 기본값 그대로).
  응답에 이미 표가 있으면 멈춘다.
- `smoke-dllm`: 동결 기준선 프롬프트로 d-llm K회(기본 3). 출력 경로는 `--gen-out` 그대로이고 머리에 `t0` 를 싣는다. 지출 전에
  t0·표 부착·칸을 검사한다. 지출 가드는 r5 `SpendGuard`(`--max-usd`·`--soft-usd`).
- `smoke`: 평가 캐시는 t0 에 GitHub 에서 새로 받는다(GET, `--work/<label>-github.json`, `--github-cache` 인자 없음). 같은 t0 로
  받은 캐시만 다시 읽고, 리포 `fetched_at` 이 t0 보다 이르면 멈춘다. 소유자 = `--github-owners`, 없으면 `--github-default-owner`.
  - 관문: 거짓 열림 수 = 키 있는 열린 항목 중 t0 시점 GitHub 끝남(종결·미계획·머지·닫힌 PR) / 없는 번호(t0 뒤 생성 포함) /
    그 응답 표의 반증 줄(`확인된 종결`·`종결(미계획)`, GitHub 는 끝나지 않음). 표 안(그 키 줄 있음)·밖 합계와 사유별 수.
    미연결 키(리포 없음·조회 실패)는 세지 않는다. 1건 이상이면 exit 4(rollback).
  - 참고: P_key, P_all(단계 0 판정), 키 없는 항목 수와 끝남·열림·보류, 열린 PR 유래 항목 수, `residual` 로 낸 t0 끝남 키 수
    (키 목록은 리포 밖 `<label>-smoke-details.json`).
  - 표 대조(`--recall` + `--table-recall`): 부착 표마다 모든 줄을 t0 캐시와 대조해 `열림 후보` 정밀도(전체·GitHub 연결 키),
    `확인된 종결` 오류(완료 이슈·머지 PR 아님)·`종결(미계획)` 오류(미계획·닫힌 PR 아님), 미확인·충돌 사유별 수. 미부착 질의는
    부착 사유를 낸다.
  - 실패(exit 1): t0 불일치, 표 미부착 `no-table`, 칸 누락 `field:<자리>.<칸>`(3상태 표 `t fmt keys text counts fetched_at`,
    생성 머리, 생성 `valid`, 항목 `status`), 빈 분모 `empty:open-items|keyed|linked`, 유효 생성 부족 `valid-reps`(기본 K 전부),
    `recall-sha`·`prompt-sha`·`table-sha`. 입력 검사는 GitHub 호출 전이다. 요약 `<label>-smoke.json` 은 실패여도 쓴다.
- `owners-check`: 소유자 표 커버(저장소 리포 대비)와 표에 없는 리포의 근거 상태(저장소 GitHub 테이블), 소유자 표 리포마다
  `repos/<owner>/<repo>` GET 으로 포크 여부, 포크면 `pulls?state=closed` GET 으로 그 소유자의 닫힌 PR 수. 이름은 `--out.names.json`.

```bash
O=$HOME/.local/share/hindsight-bench/arch-v2; W=$O/r7-4-work; R7=$O/r7-work; D=$O/r5-d-work      # 리포 밖, 0700
PROD="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")"
BANK="$(grep -E '^HC_PERSONAL_BANK=' "$PROD/.env.host.local" | cut -d= -f2-)"
E7="uv run --python 3.12 --with-requirements gateway/requirements.txt python -I bench/r7_endtoend.py"
PY="uv run --python 3.12 --with-requirements gateway/requirements.txt python -B"
# 1) 운영 역할 저장소를 리포 밖 새 파일로 복사(sqlite 백업 API, 원본 읽기만) -> 운영 뱅크 나열 GET 으로 v2 재키잉
${=PY} scripts/item_roles_sidecar.py rekey --db $W/ops-store-<KST>.sqlite --out $W/r7-4-store-v2.sqlite --rules v2 \
  --bank "$BANK" --allow-prod --aliases-file $O/r2-aliases.json --max-usd 1 --spend-log $W/r7-4-rekey-spend.json \
  --file-mode 600 --save-listing $W/r7-4-listing-bodies.jsonl $W/r7-4-listing-meta.jsonl
# 2) 복사본에 poll --github(같은 나열, 판정은 남은 상한 안)
cp -p $W/r7-4-store-v2.sqlite $W/r7-4-store-v2-gh.sqlite
${=PY} scripts/item_roles_sidecar.py poll --db $W/r7-4-store-v2-gh.sqlite --rules v2 --bank "$BANK" \
  --offline-bodies $W/r7-4-listing-bodies.jsonl --offline-meta $W/r7-4-listing-meta.jsonl --aliases-file $O/r2-aliases.json \
  --max-usd <남은 상한> --file-mode 600 --github --github-owners $W/r7-4-owners.json --github-default-owner <owner>
${=E7} owners-check --store $W/r7-4-store-v2-gh.sqlite --owners $W/r7-4-owners.json --default-owner <owner> \
  --aliases $O/r2-aliases.json --out $W/r7-4-owners-check.json
# 3) 운영 게이트웨이 recall 읽기(수집 2시간 안) -> 오프라인 표 부착
HC_ENV_FILE="$PROD/.env.local" HC_PROD_ROOT="$PROD" ${=E7} smoke-recall --allow-prod --read-only \
  --query symptom=$D/symptom-q1.txt --query project=$W/q-project.txt --query general1=$W/q-general1.txt --out-dir $W/rec
for n in symptom project general1; do ${=E7} smoke-attach --recall $W/rec/$n.json --store $W/r7-4-store-v2-gh.sqlite \
  --aliases $O/r2-aliases.json --out $W/rec/$n-attached.json; done
# 4) d-llm(자식 env 에만 CLAUDE_CODE_OAUTH_TOKEN) -> 채점
${=E7} smoke-dllm --recall $W/rec/symptom-attached.json --t0 <t0> --k 3 --gen-out $W/gen/smoke-gen.json \
  --spent-dir $W --max-usd 1 --soft-usd 0.8
${=E7} smoke --t0 <t0> --gen $W/gen/smoke-gen.json --recall $W/rec/symptom-attached.json \
  --table-recall $W/rec/project-attached.json --table-recall $W/rec/general1-attached.json \
  --store $W/r7-4-store-v2-gh.sqlite --aliases $O/r2-aliases.json --github-owners $W/r7-4-owners.json \
  --github-default-owner <owner> --work $W
```

### v2.1 드라이런 결정적 검사 + 두 질의 사전 연막 (R7-6, issue #412)

R7-4 실행기를 키 규칙 v2.1 저장소(#410)에 맞춘다. 결과: `artifacts/arch-v2/r7-preflight2.md`.

- 렌더: `render_table`(`render`·`smoke-attach`)과 `r7_eval.table3` 은 3상태 모드에서 게이트웨이처럼 리포 최대 번호를 넘긴다
  (`item_open_table.repo_max_numbers(item_roles.load_github(<저장소>))`, `build_table(..., max_numbers=)`). 손잡이
  `over_max_rows` 기본 drop 이라 over-max 줄은 빠지고, 부착 결과에 `open_table_over_max`(뺀 줄 수)가 붙는다. over-max 가 있는
  고정 입력에서 벤치 표 = 게이트웨이 MCP 응답 표(payload sha256, `tests/test_r7_preflight2.py`). over-max 가 없는 v1·v2 저장소와
  단계 A `score` 요약(10-28)은 바이트 그대로다.
- v2.1 저장소: 단계 A 감사 판정(`audit`)은 저장소 key_rules v2·v2.1 을 받는다(감사 줄 key_rules 는 한 값).
- 관문 재정의(`gate_of`, 요약 `gate.rollback` 범주별 수):
  - 롤백(0 허용): `github_finished`(키 있는 열린 항목 중 t0 GitHub 끝남), `table_refuted`(그 응답 표의 반증 줄 키를 열림으로),
    `over_max_rows`(응답 표 안 `미확인·충돌` no-number 줄 중 게이트웨이 `over_max` 참), `cross_conflict_rows`(아래),
    `runner_failure`(실패 코드 수).
  - 공개만(요약 `disclosure`): `unconfirmed`(미연결 리포 키를 열림으로), `reopened_unknown`, `missing_number_open`
    (없는 번호 키를 열림으로, 저장소 리포 최대 번호 대비 `le_max`·`over_max`·`max_unknown`), `keyless_finished`, `open_pr_items`,
    `residual_finished`.
  - 교차 충돌 종결 줄: 표의 `확인된 종결`·`종결(미계획)` 줄 키마다 그 키를 언급한 저장소 기억(t0 이하 valid)의 본문을 저장
    나열(`--listing`, 사이드카 `--save-listing` 모양)에서 읽어, 저장소 키 규칙의 `key_hits_for` 로 문장마다 다시 푼다. 그 키가
    나온 문장에 명시된(known·owner 접두) 다른 리포의 같은 번호가 t0 GitHub 에서 열림(열린 이슈·열린 PR·재오픈 미상)이면 센다.
    다른 리포 번호도 t0 평가 캐시로 받는다. 본문이 없는 기억 수는 `missing_bodies`. 관문은 좁힌 정의(rework #412-1):
    줄 키의 근거 언급 출처가 전부 명시(known·owner·item 태그)면 빼고 `cross_conflict_rows` 로 센다. 원래 정의 값
    `cross_conflict_rows_all` 과 분해 `cross_detail`(줄 키 출처 explicit-only|mixed, 다른 리포 번호가 문장에 키로 있었는지)은
    표 칸과 요약 `disclosure` 에 낸다.
  - 표 안 over-max 줄은 게이트웨이의 수집 뒤 언급 예외(신선도) 없이 센다(헛롤백 쪽으로 기우는 보수적 선택, rework #412-2).
  - 근거 기억 본문이 나열에 없으면(`missing_bodies` > 0) 실패 코드 `missing-bodies:<n>`(실행기 실패, rework #412-2).
    예상 밖 예외(sqlite·OS·JSON 등)와 봉인 거부도 요약에 `error:<예외 종류>` 와 `gate.verdict` rollback 을 쓰고 0 아닌 코드로
    끝난다(이미 pass 를 쓴 뒤여도 덮어쓴다). `table-gate` 요약에도 `gate`(같은 범주)가 있다.
  - 종료 코드: 0 통과, 4 롤백(롤백 범주 1 이상), 1 실패. 실패면 `runner_failure` 가 1 이상이라 요약 `gate.verdict` 는 rollback
    이고 표준 출력 마지막 줄에도 함께 낸다.
- `table-gate`: 결정적 검사(LLM 0). 저장 recall(표 부착)마다 `table_check` + 표 안 over-max 줄 + 교차 충돌 종결 줄 + 표 정확도.
  평가 캐시는 `smoke` 와 같은 t0 규칙(`--work/<label>-github.json`). exit 0 = 롤백 범주 모두 0, 4 = 하나라도 있음, 1 = 실패.
- 표 정확도(issue #414, `table_accuracy`, `table-gate` 만): 부착 표마다 판정해 표 칸 `accuracy`(verdict pass|rollback|fail,
  정밀도, 오류 수, 사유)를 단다. 문턱은 표마다 적용한다(풀링하지 않음).
  - 롤백 범주(`gate.rollback`): `candidate_precision_low`(`열림 후보` GitHub 연결 키 정밀도 `linked_precision` < 95% 인 표 수,
    95% 는 통과, 상위 #393 checkpoint (3) 정의. 전체 정밀도는 공개 칸 `precision_all`, rework #414-1), `closed_errors`
    (`확인된 종결` 줄 중 t0 범주가 완료 이슈·머지 PR 이 아닌 줄 수 합), `not_planned_errors`(`종결(미계획)` 줄 중 미계획 종결
    이슈·안 머지 닫힌 PR 이 아닌 줄 수 합).
  - 실패(헛통과 방지, `runner_failure`): 값이 없거나 수가 아님 `missing:<칸>:<표>`, 연결 열림 후보 0줄(빈 분모)
    `empty:candidate.linked:<표>`, 표 줄 파싱 수 != `counts` 합 `counts-mismatch:<표>`(rework #414-1).
    종결 오류는 줄 수라 종결 줄 0개는 실패가 아니다.
  - `smoke` 관문 범주와 요약은 그대로다(바이트 불변).
- GitHub 호출 시한(issue #414): `r7_eval.gh_paged` 는 subprocess 시한(기본 60초, `GH_TIMEOUT_S`)을 두고, 넘기면 실행기 실패
  (`gh api timed out ...`)다. `smoke`·`table-gate`·`owners-check` 는 `--gh-timeout <초>`(기본 60)로 바꾼다. `smoke`·`table-gate`
  는 요약 `fail` 에 `error:gh api timed out ...` 과 verdict rollback 을 쓰고 exit 1.

```bash
O=$HOME/.local/share/hindsight-bench/arch-v2; W=$O/r7-6-work; W4=$O/r7-4-work; D=$O/r5-d-work      # 리포 밖, 0700
E7="uv run --python 3.12 --with-requirements gateway/requirements.txt python -I bench/r7_endtoend.py"
# 1) 운영 저장소 복사(sqlite 백업 API) -> rekey --rules v2.1(운영 뱅크 나열 GET, --save-listing) -> 복사본에 poll --rules v2.1 --github
#    (R7-4 절 명령에서 --rules v2 를 v2.1 로, 소유자 표는 $W4/r7-4-owners.json)
# 2) 운영 recall 읽기(R7-4 절 smoke-recall, 원래 증상·이전 사고 프로젝트 상태 질의·일반 질의) -> smoke-attach
# 3) 결정적 검사
${=E7} table-gate --t0 <t0> --recall $W/rec/symptom-attached.json --recall $W/rec/project-attached.json \
  --store $W/r7-6-store-v21-gh.sqlite --aliases $O/r2-aliases.json --listing $W/r7-6-listing-bodies.jsonl $W/r7-6-listing-meta.jsonl \
  --github-owners $W4/r7-4-owners.json --github-default-owner <owner> --work $W --label r7-6-det [--gh-timeout 60]
# 4) 질의마다 smoke-dllm(K=5) -> smoke(--recall 그 질의, --listing 같은 나열, --label 질의별)
# 5) 고정 회귀: R7-4 저장 recall($W4/rec/symptom.json)을 새 저장소로 smoke-attach(--knobs: 단계 A 켬 손잡이 + github_max_age_hours 0,
#    저장소 근거가 그 t0 보다 늦어 기본 2시간이면 미래 시각 stale 로 덮인다) -> smoke-dllm K=3 -> smoke(--t0 = R7-4 t0)
```

### 운영 연막 한 회차 래퍼 (R7-7, issue #414)

`bench/r7_ops_smoke.sh`(테스트 `tests/test_r7_ops_smoke.py`, 가짜 하위 명령으로 순서·종료 코드 합성). 운영에는 읽기만 한다.

- 모드(rework #414-1): 운영 모드(기본)는 운영 게이트웨이 응답의 표(`open_table`)를 그대로 판정한다(다시 그리지 않는다).
  드라이런 모드(`--dry-run-attach`)는 S3 꺼진 운영에서 사전 연막을 다시 돌릴 때만 스냅샷 저장소로 표를 다시 그려 붙인다
  (S3 켜진 응답에는 `smoke-attach` 가 거부해 실패). 두 모드 모두 스냅샷은 `table-gate`·`smoke` 평가 입력이다.
- 순서: 운영 역할 저장소 스냅샷(sqlite 백업 API, 읽기 전용 연결, `<work>/<label>/store.sqlite` + `store-copy.json` 에 원본 전후·사본
  sha256, integrity) -> `smoke-recall`(상태 질의 + 일반 질의, 한 t0 = 지금, `--allow-prod --read-only`) -> (드라이런만) 질의마다
  `smoke-attach` -> `table-presence`(상태 질의 응답에 표가 없으면 실패 `no-open-table:<질의>`, 일반 질의 응답의 표는 롤백 범주
  `general_attached`) -> `table-gate`(상태 질의 표) -> 상태 질의마다 `smoke-dllm`(K) -> `smoke`(일반 질의는 `--table-recall`).
- 실행기 실패(롤백 아닌 0 아닌 코드, 요약 verdict 와 종료 코드 불일치, 요약 줄 없음)가 나면 그 단계에서 멈춘다. 롤백(4)은 끝까지 간다.
- 출력: 단계마다 `STEP <단계> rc=<코드> verdict=<값> secs=<초>`, 실패면 `CAUSE <단계> rc=<코드> fail=<요약 fail>`, 마지막 줄
  `VERDICT pass|rollback|runner_failure mode=ops|dry-run-attach <범주>=<수> ...`(범주 = `table-presence`·`table-gate`·`smoke`
  요약 `gate.rollback` 합 + 멈춘 단계 수).
  종료 코드 0 pass / 4 rollback / 1 runner_failure / 2 인자 오류. 하위 명령 로그는 `<work>/<label>/logs`(0600)에만 쓴다.
- `--work` 는 리포 밖이어야 하고, 같은 회차 디렉터리(`<work>/<label>`)가 이미 있으면 멈춘다.

```bash
PROD=<운영 체크아웃>; O=$HOME/.local/share/hindsight-bench/arch-v2; W=<회차 work, 리포 밖 0700>
HC_ENV_FILE="$PROD/.env.local" HC_PROD_ROOT="$PROD" CLAUDE_CODE_OAUTH_TOKEN="$(<토큰 명령>)" \
  bash bench/r7_ops_smoke.sh --label 10m --store "$PROD/policies/item-roles.sqlite" \
  --listing <나열 본문 JSONL> <나열 메타 JSONL> --owners <소유자 json> --default-owner <owner> --aliases $O/r2-aliases.json \
  --work $W --state-query symptom=<원래 증상 문안> --state-query project=<프로젝트 상태 문안> \
  --general-query general1=<일반 문안 1> --general-query general2=<일반 문안 2> --k 5 --max-usd 2 --soft-usd 1.6 --gh-timeout 60
# S3 꺼진 운영에서 사전 연막을 다시 돌릴 때만: 같은 줄 끝에 --dry-run-attach
```

## 1단계 arm 벤치 (issue #297, 상위 #296)

1단계(#298~#301)는 이 절의 명령을 그대로 쓴다. 판정 해석·문턱 산식·기준선 실측의 정본은 `artifacts/arch-v2/r1-a-baseline.md`다.

- **방식**: 동시 두 세트, 한 run. s1 = D10, s2 = arm 백업. 두 세트는 벤치 사이드카 하나를 공유하고, 세트마다 같은 게이트웨이 ref 의 팔을 하나씩 세운다.
  runner 는 쿼리마다 두 팔을 번갈아 재므로 시간 드리프트가 비교에서 빠진다. 순환 교차(`bench/crossover.py`)는 쓰지 않는다(세트 데이터가 다르다).
- **게이트웨이 ref**: 1단계 내내 #297 머지 커밋으로 고정한다(#302·#303 머지가 비교에 끼어들지 않게). #297 기준선 A/A 는 머지 전이라 그 브랜치 HEAD 로 세웠고,
  머지 때 `git diff <브랜치 HEAD> <머지 커밋> -- gateway/`가 빈 출력인지 코디네이터가 확인한다.
- **축**: 속도·비용만(`bench/arm_compare.py`). judge.py 의 정확도 축(결과 불변)은 관찰을 바꾸는 arm 에서 설계상 늘 changed 라 쓰지 않는다. 결과가 바뀐 쿼리 비율은 보고만 한다.
- **판정하지 않는 run**(다시 잰다): 표본 하한 미달(기본 `--min-rounds 3 --min-queries 40`, 아래 명령의 `--rounds 3 --limit 40`과 같다), 중단(meta 없음·가드 중단·호출 부족), 측정 중 오류 응답이나 두 팔 outcome 차이, 동결 지문 변화, rewrite 실패 증가,
  복원원 sha256 이 D10·LINEAGE 값과 다름, verify 보고서가 없거나 pass 가 아님. `arm_compare.py`가 INVALID(exit 2)로 낸다.

### arm 백업 위치와 LINEAGE

- 뜨는 곳: hc-next 에 arm 설정 적용과 재통합 drain 을 마친 뒤 `next_env=$(bash scripts/next-stack.sh env) || exit 1; eval "$next_env"; bash scripts/backup.sh`.
  `scripts/backup.sh`는 실행한 체크아웃의 `backups/<TS>/`에 쓴다. 뜬 디렉터리는 곧바로 **리포 밖** `~/.local/share/hindsight-bench/arm-backups/<arm>-<YYYYMMDD-HHMMSS>/`(0700)로 옮긴다.
  운영 체크아웃 `backups/`에 두면 "최신 백업"이 돼 기본 복원원이 되고, 워크트리에 두면 워크트리와 함께 사라진다.
- 같은 디렉터리에 `LINEAGE`(JSON, 0600)를 둔다. 런 1 스키마(`schema_version` 1):

```json
{
  "schema_version": 1,
  "arm": "<arm 이름, 예: r1-b-i>",
  "source_backup": {"path": "<운영 체크아웃>/backups/<D10 백업>/pg0.tar.gz",
                    "sha256": "<D10 백업 sha256>"},
  "settings": {"<뱅크 설정 키>": "<비사적 값 그대로>", "<사적 값 키(미션 등)>": {"sha256": "<값 sha256>", "len": 0}},
  "directives": [{"name": "<이름>", "sha256": "<본문 sha256>", "len": 0}],
  "document_patches": 0,
  "reconsolidation": {"started_at": "<UTC ISO>", "drained_at": "<UTC ISO>", "llm_requests": 0},
  "pages": {"delta_suffix": "-pre", "full_suffix": ""},
  "backup": {"pg0_sha256": "<이 디렉터리 pg0.tar.gz 의 sha256>", "created_at": "<UTC ISO>"},
  "expected_bank_overrides": {"<GET .../banks/<bank>/config 의 overrides 전체, 값 그대로>": "..."}
}
```

- 런 2(스펙 #318, #319·#322·#324·#325)는 `schema_version` 2 다. 1의 키 전부에 두 키를 더한다(이 모양 그대로 쓴다):

```json
{
  "schema_version": 2,
  "image": {"tag": "<사본 hindsight 이미지 태그>", "id": "sha256:<64자>"},
  "invalidations": null
}
```

  - `image`: 백업을 뜬 사본 hindsight 컨테이너의 이미지 태그와 ID(`docker image inspect <태그> -f '{{.Id}}'`, 필드 지정으로만 본다).
  - `invalidations`: 무효화 처치가 없으면 `null`. 있으면 아래 객체. 뒤의 두 수는 backfill 대상 목록의 `GET .../memories/{id}`에서 `state`가 `invalidated`인 수를
    backfill 직후와 마지막 drain 뒤에 센 것이다.

```json
{"rule_version": "<RULE_VERSION>", "rule_module_commit": "<40자 sha>", "aliases_sha256": "<64자>",
 "targets_file_sha256": "<64자>", "targets": 0, "applied": 0, "started_at": "<UTC ISO>",
 "invalidated_after_backfill": 0, "invalidated_after_drain": 0}
```

  - 읽는 쪽(`bench/replica.py --expected-overrides`)은 `expected_bank_overrides`만 보므로 런 1 LINEAGE(1)와 런 2 LINEAGE(2)가 모두 읽힌다.
- `expected_bank_overrides`는 백업 직전 사본의 `GET /v1/default/banks/<bank>/config` `overrides` 전체다(값 그대로 - 사적 값이 들 수 있어 LINEAGE 는 리포 밖에만 둔다).
  `bench/replica.py up|verify --expected-overrides <LINEAGE>`가 이 키만 읽는다. 키가 없거나 객체가 아니면 거부한다.
  뜨는 법(사본 env 안에서): `python3 -B -c 'import argparse,json,os; from bench import arch_v2_regression as A; t=A.resolve_target(dict(os.environ),False,False); _,up=A.build_clients(t,argparse.Namespace(owner=None,bank="personal-demo")); print(json.dumps(up.request("GET","/v1/default/banks/personal-demo/config")["overrides"],ensure_ascii=False,sort_keys=True))'`
- verify 의 기대 override: 옵션이 없으면 지금처럼 운영 override 와 같아야 한다. 있으면 세트 override 가 기대와 같아야 하고, 운영 override 와 기대가 다른 키는
  해석된 설정 차이(`bank_config_diff_unexplained`)에서도 설명된 것으로 본다. 그 밖의 설정 차이는 여전히 실패다.
  D10 은 override 가 운영과 같은 `memory_defense` 하나라 기대 없이 통과한다(2026-10-30 실측).
- 복원 sha256: arm 세트는 LINEAGE `backup.pg0_sha256`, D10 세트는 위 D10 값과 `up` 출력 `backup_sha256`을 대조한다(`arm_compare.py --expect-sha`).

### 명령

워크트리(`#297` 머지 뒤 origin/main)에서 실행한다. 운영 체크아웃의 `bench/.data`·`audit`·`secrets`를 심링크로 달고(끝나면 `rm`, 끝 슬래시 없이), hc-next 가 없을 때만 띄운다.

```bash
PROD="$(cd "$(git rev-parse --git-common-dir)/.." && pwd)"
D10="$PROD/backups/<D10 백업>/pg0.tar.gz"
D10SHA=<D10 백업 sha256>
ARM=~/.local/share/hindsight-bench/arm-backups/<arm>-<TS>          # LINEAGE 가 있는 arm 백업
REF=<#297 머지 커밋>
OUT=~/.local/share/hindsight-bench/arch-v2/<arm>-bench; mkdir -p "$OUT"; chmod 700 "$OUT"
bash scripts/next-stack.sh status; [ $? = 3 ] || echo "hc-next 가 떠 있다 - 띄우지 않는다"
docker ps -aq --filter label=hc-bench=1                                # 빈 출력이어야 한다
# 1) 공유 벤치 사이드카 1개(운영 plist 복제, 8787 거부)
python3 -B bench/replica.py sidecar-up r1 --port 18781
# 2) 세트 2개. s2 는 LINEAGE 의 기대 override 를 state 에 남긴다. 출력 backup_sha256 을 D10SHA / LINEAGE backup.pg0_sha256 과 대조
python3 -B bench/replica.py up --set s1 --backup "$D10" --sidecar-port 18781 > "$OUT/up-s1.json"
python3 -B bench/replica.py up --set s2 --backup "$ARM/pg0.tar.gz" --sidecar-port 18781 --expected-overrides "$ARM/LINEAGE" > "$OUT/up-s2.json"
# 3) 팔 2개(같은 ref, 운영 정책 - 읽기만. 두 파일 sha256 은 gw-up 출력 local_policies.files 에 남는다)
python3 -B bench/replica.py gw-up base --set s1 --ref "$REF" --port 18891 --local-policies "$PROD/policies/local" > "$OUT/gw-base.json"
python3 -B bench/replica.py gw-up cand --set s2 --ref "$REF" --port 18892 --local-policies "$PROD/policies/local" > "$OUT/gw-cand.json"
# 4) run 마다: verify 두 세트 -> runner -> arm_compare. 세트 시계가 다르면(재통합이 last_memory_write_at 을 바꾼다) --clock-from-set s1 로 D10 시계를 쓴다
id=<arm>-r1
python3 -B bench/replica.py verify --set s1 > "$OUT/verify-s1-$id.json"
python3 -B bench/replica.py verify --set s2 > "$OUT/verify-s2-$id.json"           # up 때 남긴 기대 override 로 대조
uv run --python 3.12 --with tiktoken python -B -m bench.runner --arm base=18891 --arm cand=18892 \
  --split dev --limit 40 --seed 13 --rounds 3 --run-id "$id" --clock-from-set s1
python3 -B -m bench.arm_compare bench/.data/runs/$id --base base --cand cand \
  --expect-sha base=$D10SHA --expect-sha cand=<LINEAGE backup.pg0_sha256> \
  --verify base="$OUT/verify-s1-$id.json" --verify cand="$OUT/verify-s2-$id.json" \
  --speed-threshold 0.03 --p95-threshold 0.06 > "$OUT/compare-$id.json"   # exit 0 PASS / 1 FAIL / 2 INVALID
# 5) 정리(이 순서). 끝에 아래 확인이 전부 빈 출력이어야 한다
python3 -B bench/replica.py gw-down base; python3 -B bench/replica.py gw-down cand
python3 -B bench/replica.py set-down s1; python3 -B bench/replica.py set-down s2
python3 -B bench/replica.py sidecar-down r1
python3 -B bench/replica.py down                                     # 네트워크·팔 상태·빌드 소스까지
docker ps -aq --filter label=hc-bench=1; docker volume ls -q --filter label=hc-bench=1
docker network ls -q --filter name=hc-bench; lsof -nP -iTCP:18780-18799 -sTCP:LISTEN
```

- 문턱: 속도(기하평균 지연 비 점추정) `--speed-threshold`, 섞은 p95 비 `--p95-threshold`. 값과 산식은 `artifacts/arch-v2/r1-a-baseline.md` "확정 문턱"이 정본이다.
  비용은 cand 토큰 합계 <= base 토큰 합계(허용 0). p99 는 참고로만 본다.
- 공통 시계: `--clock-from-set s1`은 세트 시계가 같아도 meta 에 `bench_clock_common`(세트별 시계, `clocks_differ`)을 남긴다. 옵션이 없으면 시계가 다를 때 지금처럼 거부한다.
- 1 run(40쿼리 x 2팔 x 3라운드)은 약 23~30분이다. 기준선 D10/D10 A/A 는 6 run 을 쟀고 유효 4 run 으로 문턱을 정했다(속도 3.0%, 섞은 p95 6.0%).

## cold 재생 도구 (issue #20, 2026-09-26)

B1 이 바꾸는 사이드카 요청 모양(포화 recall 한 번 = 128x3 순차 vs 300x1)의 **cold 요청** 지연을 분리해 잰다.
`bench/cold_replay.py`: 계획·분석은 순수 함수, 실행만 IO 경계(벤치 사이드카 HTTP). 운영 사이드카(8787)는 state 가 있어도 거부한다.

```bash
# 1) 벤치 사이드카: 300 텍스트 수용 + 데드라인을 크게(잘리지 않은 cold 지연을 잰다 - 7.5s 데드라인·재시도는 분석이 모델로 적용)
.venv/bin/python -B bench/replica.py sidecar-up cold --port 18781 --env RERANK_MAX_TEXTS=600 \
  --env RERANK_TOTAL_DEADLINE_SECONDS=120 --env RERANK_QUEUE_TIMEOUT_SECONDS=120
# 2) 입력: 캠페인 고정 백업으로 세트를 올려 holdout recall 을 trace 로 돌린다(포화 쿼리만, 약 7분). 끝나면 세트는 내린다
.venv/bin/python -B bench/replica.py up --set cx --sidecar-port 18781 --backup backups/<벤치 백업>/pg0.tar.gz
.venv/bin/python -B -m bench.cold_replay extract --set cx      # -> ~/.local/share/hindsight-bench/holdout/cold-inputs.json (0600)
.venv/bin/python -B bench/replica.py set-down cx
# 3) 재생(기본 방식당 40회, idle 300s = 약 6.9시간). 가드 아래서 돈다. 시험은 --n-per-mode 2 --idle-s 30
.venv/bin/python -B -m bench.cold_replay run --port 18781 --run-id <id>
# 4) 분석 -> <run>/report.json
.venv/bin/python -B -m bench.cold_replay analyze ~/.local/share/hindsight-bench/holdout/runs/<id>
.venv/bin/python -B bench/replica.py sidecar-down cold
```

- **계획**: 쌍마다 같은 입력으로 두 방식을 한 번씩, ABBA 순서(시간 드리프트가 한 방식에 몰리지 않게). 도구 시작 시점을
  포함해 매 프로브 전 직전 rerank 활동에서 idle_s 이상 기다린다. 프로브 = cold 요청들 + 같은 요청을 곧바로 한 번 더(warm).
- **표시·제외**: 벤치 사이드카 로그의 `/rerank` 줄 수로 남의 요청을 센다. idle 대기 중에 보이면 감지 시각부터 대기를 다시
  시작한다(`idle_restarts`). 운영 사이드카 로그의 `rerank_processed` 구간(끝 - elapsed)은 같은 MPS 장치의 부하라 프로브마다
  겹친 수를 싣는다. idle 창에 남의 요청·운영 rerank 가 끼면 `idle-broken`, 측정 도중 끼거나 가드 정지 구간과 겹치면
  `interference`(정지가 idle 대기 안에만 있어도 - swapout 이면 cold 가 아니라 스왑 복귀를 재게 된다), 200 이 아닌 응답이면
  `error`, 응답이 없으면(연결 실패) `no-response`, 운영 로그를 못 읽으면 `no-prod-signal`(닫힌 쪽). 제외된 프로브의 쌍은 비
  지표에서 빠진다. 가드 창은 거슬러 기록되고 운영 rerank 줄은 계산이 끝나야 찍히므로, analyze 는 run 끝의 가드 창과 운영
  구간(`meta.prod_intervals`, 마지막 프로브 뒤 30초 기다려 다시 읽음)으로 모든 프로브를 다시 판정한다.
  벤치 사이드카는 override 가 필수 3키뿐이고 프로세스가 살아 있어야 run 이 시작된다(배치·max_length·idle_trim 등 거부).
- **재시도 모델**(rerank-server/server.py, upstream cross_encoder.py): 사이드카는 7.5s 에 503 을 주지만 시작한 계산은 끝까지
  돌린다. upstream 은 동시성 1 로 요청을 이어 보내고 5xx 에 최대 3회 재시도(0.5s 부터 2배, 지터 제외 = 하한). 재시도는 남은
  계산 뒤에 줄 서고 warm 지연으로 계산한다. 줄에서만 7.5s 를 넘기면 worker 가 건너뛴다. 재시도를 다 쓰면 recall 오류.
- **출력**: 방식별 cold 첫 요청·요청 합·warm 합·recall 단위(모델) 분포, 7.5s 초과 요청·프로브·율, 재시도·실패 수.
  지표 `cold_p50`·`cold_p95` = 쌍별 recall 단위 지연의 300x1/128x3 분위수 비 + 부트스트랩 95% CI -
  `judge.noninferiority(ratio, lo, hi, noise)` 가 그대로 받는다. 깨끗한 쌍이 20 미만이면 `hold`(+ 모자란 쌍 수).
  `failures` = 방식별 recall 실패(재시도 소진) + 사이드카 오류 응답 수, 후보가 기준보다 많으면 `regressed` - 분위수 비는
  실패를 악화로 말하지 않는다(넓은 CI -> hold). run 이 예외·Ctrl-C 로 끊겨도 meta.json 은 남는다(`aborted: error:<종류>`).
  idle_s 가 300 미만이면 `cold_definition_met=false`(시험 실행 표시). deep-cold(4시간 이상)는 재현하지 않는다(배포 뒤 감시).
- 5분 idle 은 사이드카 idle_trim(30분, MPS empty_cache)을 부르지 않는다. 30분 이상 cold 는 이 도구로 보지 않는다.

**입력** (캠페인 고정 입력: 백업 <벤치 백업>, holdout sha e28fb71a, 벤치 시계 2026-09-22T20:00Z): holdout 68 중 포화 64(후보 300),
비포화 4 는 뺐다(B1 은 포화 recall 만 바꾼다). 문서 길이 합 중앙값 약 22.9만 자/쿼리.
**재추출 (2026-09-26, final-review 뒤)**: 첫 추출은 REST recall 기본값(budget mid)으로 돌아 캠페인(MCP, budget high) 후보가
아니었다 - 새 입력과 같은 쿼리는 64 중 7, 쿼리당 겹친 후보 중앙값 280/300(최소 263). `recall_args` 로 MCP 기본값과 행의
query_timestamp 를 지켜 다시 뽑았고(포화 64 그대로), 표본 4쿼리(자체 시각 행 포함) 1,200/1,200 이 운영 점수와 bit 일치.
옛 파일은 `cold-inputs.budget-mid.json` 으로 남겼다. 아래 시험 실행은 옛 입력으로 돌았다(도구 동작 확인용이라 결론에 영향 없음).

**시험 실행 `cold-trial-20260926`** (방식당 2회, idle 30s - cold 정의 미충족 표시): 4 프로브 끝까지, 전부 200,
운영 사이드카 503·데드라인 0, 가드 정지 1회(위 가드 절). 프로브 0 은 가드 구간과 겹쳐 제외 -> 남은 쌍 1 -> 지표 hold(19 부족).
이 표시가 시험에서 드러난 결함(쌍 1개에 CI 폭 0 으로 ok)을 고쳤다. 관측값(참고, 판정 아님): 300x1 요청 4.0~4.2s,
128x3 요청 합 4.3s(첫 요청 2.0s), 7.5s 초과 0. 본 측정(방식당 40회)은 #21 에서 두 번 시작해 두 번 다 멈췄다 - 24GB 호스트에서는
벤치 사이드카 계산이 매번 swapout 가드 정지를 불러 다음 프로브를 제외시킨다(위 B1 절 "cold 로컬 측정 불가"). cold 는 #22 카나리 운영 감시로 잰다.

## 정밀도 설계 (왜 이렇게 했나)

- **시계 고정**: upstream recall 점수의 recency 와 "어제·지난주" 해석이 서버 시계(`utcnow`)를 쓴다.
  데이터를 동결해도 날짜가 바뀌면 순위가 바뀐다. runner 가 `query_timestamp` 에 벤치 시계
  (복제본 마지막 쓰기 + 1분)를 넣는다. 이 인자는 필터가 아니라 기준 시각으로만 쓰인다(upstream 코드 확인).
- **동결**: 복제본의 worker·consolidation 재조정·멘탈모델 갱신·llm trace 정리를 끄고, LLM 을 none 으로
  둔다(recall 경로에 LLM 호출 없음, 운영 xai-oauth 자격증명 회전 차단). 동결은 `verify` 의 데이터 지문으로 측정한다.
- **결정성**: graph 동점 순서가 문자열 해시 시드에 의존해 `PYTHONHASHSEED=0` 고정. 실측상 같은 쿼리의
  결과가 팔·라운드 전부에서 동일했다(smoke-aa-1).
- **동일성**: 복제본 설정은 실행 중인 운영 컨테이너 env 를 그대로 복제하고 의도한 override 만 얹는다.
  `verify` 가 이미지 digest·env 차이·뱅크 설정 차이·리랭커 경로(MPS)를 대조한다.
- **측정 간섭**: 직렬 실행(upstream TEI 세마포어 1), 라운드별 셔플 + 팔 순서 교차, 호출마다 리랭크 사이드카
  큐 깊이 기록(운영과 공유) - 겹친 쌍은 속도 판정에서 제외.
- **판정**: 정확도는 결과 불변으로만(필드 단위 무손실 규칙), 비용은 결정적 토큰 합, 속도는 쿼리 단위
  부트스트랩 CI 로 **비열등성**을 증명해야 불악화로 인정한다(차이 없음 ≠ 나쁘지 않음).
- **교차 run 바퀴 집계** (issue #82): 바퀴 = 모든 팔이 모든 세트를 정확히 한 번씩 거치는 연속 단계 n개(n = 팔 수, 단계 k 는
  바퀴 k // n). 세트 오프셋은 바퀴 안에서 합이 0 이 되도록 배정되므로, 쿼리마다 바퀴별로 로그 비를 평균해 오프셋을 상쇄한다.
  완전한 바퀴가 3개 이상이면 바퀴 값들의 중앙값을, 아니면 평균을 쿼리 값으로 쓴다 - 전 단계 평균은 튀는 단계 하나에
  끌려가고(3바퀴 중 한 단계가 3배면 0.9 -> 1.08), 중앙값은 그 바퀴 하나만 버린다. 단계가 빠진 바퀴는 그 쿼리에서만 빼고,
  완전한 바퀴가 하나라도 있는 쿼리는 판정에 남는다(`xho-b1w` 처럼 오염이 흩어진 run 에서 쿼리가 31/68 로 줄던 문제).
  꼬리(warm p95·p99)와 섞은 p95 도 쿼리마다 바퀴별 기하평균 지연 -> 같은 중앙값/평균 규칙으로 모은 뒤 본다.
  `MIN_ROUNDS`(3)는 교차 run 에서 완전한 바퀴(어느 쿼리에서든)에 속한 단계 수로 센다 - 2팔 교차는 2바퀴(4단계)부터 판정된다.
  절사 평균은 쓰지 않는다: 바퀴 안에서 단계를 잘라내면 세트 배정 균형이 깨져 오프셋이 남는다.
  이 집계는 `meta.plan` 이 있는 교차 run(#77 계획 기록 이후 병합)에만 쓴다. 과거 교차 run 은 옛 규칙(전 단계 평균, 단계가 빠진
  쿼리 제외, `meta.rounds` 로 `MIN_ROUNDS`) 그대로 판정한다 - 판정 규칙을 사후에 바꿔 보류를 채택으로 뒤집지 않는다.

## 한계 (알고 있는 것)

- 정확도 "개선"은 자동 증명 대상이 아니다. 결과를 바꾸는 변경(랭킹·리랭커·임베딩)은 자동 탈락이다.
- `prefix-cut`·`extended`·`added`(필드 추가)는 근거가 따로 필요해 INCONCLUSIVE 로 보류한다.
- 리랭크 사이드카는 운영과 공유한다(운영자 결정 2026-09-23 - 전용 인스턴스의 복잡도를 피함). 운영 감사 로그와
  시간 대조로 겹친 쌍을 빼지만, 게이트웨이를 거치지 않는 리랭크 사용(upstream 내부 작업)은 보이지 않는다.
  사이드카 큐 깊이는 호출 직전만 판정에 쓴다(직후 값은 후보 자신의 비동기 작업이 섞일 수 있어 기록만 한다).
- 벤치 run 동안 운영 recall 이 벤치 리랭크 뒤에서 줄을 설 수 있다(운영 지연 영향). 긴 holdout run 은 한가한
  시간에 돌린다.
- 응답 뒤 지연이 큰 후보는 다음 호출에 이월 효과를 준다(휴지로 줄였지만 남음). A/A 초과분이 문턱을 좁혀 흡수한다.
- holdout 은 파일시스템 권한이 아니라 경로 분리·지시로 가린다. 연구 에이전트가 읽으면 막을 수 없다.
- 코퍼스 미수집 출처: YEW·mentor 런타임 transcript, Codex code-mode(`tools.mcp__memory_vault__recall(...)` JS) 호출.
  원장 전용 행은 이 공백을 메우지만 리허설 트래픽과 구분할 수 없다(세션 반복만 1회로 접는다).
- 쿼리 가중치는 1이다(호출 빈도 가중 없음). 자주 쓰는 쿼리의 비중이 실사용보다 작다.
- `query_timestamp` 주입으로 벤치 호출은 게이트웨이 원장의 `reusable` 플래그가 전부 False 다 - recall 응답에는
  영향이 없지만, 이 플래그나 인자 유무로 분기하는 후보는 벤치에서 운영과 다르게 동작한다.
- 토큰은 content 텍스트만 센다. structuredContent 를 모델에 넘기는 클라이언트의 비용은 보지 않는다
  (대신 structuredContent 가 content 와 어긋나면 정확도에서 changed 로 잡는다).
- 동결 지문은 stats·operations·멘탈모델을 본다. 개별 기억의 내용 수정은 간접적으로만 잡힌다.
- 복원 원본인 운영 백업이 물리적으로 비일관할 수 있다(#14).
- 세트(복제본 인스턴스)마다 고정 지연 오프셋이 있어(원인 미확정) 세트 간 비교는 순환 교차로만 한다. 교차는 오프셋이
  가법(로그 척도)이라는 가정에서 상쇄한다. runner 는 직렬이라 동시 부하는 `concurrency_probe.py` 로 따로 잰다.
- 교차 run 의 바퀴 소속은 병합 run 의 라운드(= 병합에 넣은 단계 순서)로 정한다. 병합이 단계마다 세트 배정을 확인하지만 배정은
  k mod n 이라 단계 k 와 k + n 을 가르지 못해, 다른 바퀴의 단계를 바꿔 넣어도 바퀴 집계는 알아채지 못한다.
- 바퀴 값 중앙값은 완전한 바퀴가 3개 이상일 때만 이상값을 누른다. 1~2바퀴 쿼리는 평균이라 튀는 단계 하나가 그대로 끌고 간다.
  run 수준 `MIN_ROUNDS` 는 어느 한 쿼리라도 완전했던 바퀴를 세므로, 대부분의 쿼리가 바퀴를 잃은 run 도 통과할 수 있다
  (그런 run 은 오염 비율 `MAX_CONTAMINATED` 나 쿼리 하한 `MIN_QUERIES` 가 대신 막는다).
- 바퀴 단위 warm 꼬리는 cold 호출(단계마다 팔별 첫 측정 호출)이 낀 바퀴를 그 쿼리에서 통째로 뺀다 - 바퀴 일부만 남기면 오프셋 상쇄가
  깨지기 때문에 의도한 것이다. 단계마다 쿼리 순서가 달라 cold 쿼리가 바뀌면 warm 꼬리 n 이 줄어 tail-hold 가 늘 수 있다.
