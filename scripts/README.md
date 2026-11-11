# scripts/

이 디렉터리의 도구를 누가 쓰는지 세 분류로 나눈다.

- **설치자용**: 공개판을 받아 자기 설치를 세우고 유지하는 사람이 쓴다.
- **운영자 전용**: 운영판 운영자가 자기 운영 데이터, 운영 데이터 사본 스택(`hc-next`), upstream 교체에 쓴 도구다.
  실험 보고서(`artifacts/arch-v2/`)와 테스트가 기대므로 남긴다.
  그대로 돌리려면 운영자와 같은 환경(사본 스택, 자기 뱅크, 이전 대상 파일)이 필요하다.
- **공개판 관리용**: 운영판에서 공개판을 내보내는 도구다.

공개판에 실리는 `scripts/` 경로는 `scripts/public-export/manifest.json`의 `include`에 하나씩 적은 것뿐이다.
새 도구를 더하면 `include`와 이 표에 함께 적는다(`tests/test_public_boundary.py`가 둘이 맞는지 본다).

## 설치자용

| 항목 | 하는 일 |
|---|---|
| `install/` | `./install` 구현(`hc_install.py`)과 리랭크 모델 미리 받기(`rerank_preload.py`) |
| `seed-demo.sh` | `.env.local`이 없으면 upstream 키·데모 토큰·데모 ACL을 만든다(무인 데모) |
| `profile-detect.sh` | 임베딩·리랭커 프로필을 첫 기동 전에 고른다 |
| `fetch-claude-cli.sh` | hindsight 이미지에 바인드 마운트할 고정 버전 Claude Code CLI를 받는다 |
| `hc-host-env.sh`, `hc_host_env.py` | 호스트 스크립트가 `.env.host.local`의 `HC_*` 설정을 읽는 로더(셸·파이썬) |
| `hc-python.sh` | launchd의 좁은 PATH에서도 python3를 찾는다 |
| `render-launchd.py` | `*.plist.example` 템플릿을 설치할 plist로 렌더한다 |
| `launchd/` | 예약 작업 LaunchAgent 템플릿과 설치법(`launchd/README.md`). 운영자 전용 작업의 템플릿도 함께 있다 |
| `backup.sh`, `restore-test.sh`, `bank_stats.py` | pg0 백업(0바이트 가드)과 복원 검증, 그 대조용 뱅크 통계 |
| `teardown.sh` | 스택을 내린다(`-v`면 데이터 볼륨까지) |
| `secret-hygiene.sh` | pre-commit 또는 단독으로 시크릿 유출을 막는다 |
| `bank-config.py` | 뱅크 라이브 설정을 추적 템플릿과 비교하거나 차이를 적용한다 |
| `model-refresh.sh` | 멘탈 모델 매일 갱신 드라이버(활동 게이팅, 48시간 전체 갱신) |
| `model-audit.sh`, `model_audit.py` | 멘탈 모델 주간 감사(수요·크기·공급, 알리고 사람이 승인) |
| `model-watch.sh`, `model_watch.py` | 멘탈 모델 갱신 실패 매일 감시(읽기만) |
| `model_cron.py` | 멘탈 모델을 서버 `refresh_cron` 일정에 올리거나 내린다(기본 dry-run) |
| `audit_log_reader.py` | 회전된 감사 로그를 오래된 순으로 잇는 읽기 헬퍼 |
| `trigger_drafts.py` | 게이트웨이가 쌓은 트리거 초안의 승인·기각 기록 CLI |
| `tag_hygiene.py`, `tag-hygiene-export.sh` | 저장된 태그를 지금 규칙으로 다시 평가해 문제 유형을 센다(읽기만) |

## 운영자 전용

| 항목 | 하는 일 |
|---|---|
| `next-stack.sh` | 운영 데이터 사본으로 현재 워크트리 게이트웨이를 옆에 띄우고 지운다(`hc-next`) |
| `llm-migrate-opus.py` | 옛 `.env` 파일의 LLM 블록을 현재 구성으로 옮긴다(`next-stack.sh --llm opus`가 판정에 쓴다) |
| `policy-split.py` | 옛 단일 정책·트리거 파일을 추적분과 로컬 오버레이로 나눈다(일회성 이전) |
| `scope-migrate.py` | 뱅크 문서를 새 관찰 범위로 옮기고 재구성 소진을 지켜본다(실험 런) |
| `item-backfill.py` | 완료 기억이 닫은 열린 기억을 무효화한다(실험 런) |
| `item_roles_sidecar.py`, `item_roles_observe.py`, `item-roles-observe.sh` | 역할 저장소 호스트 사이드카와 그 매시간 관찰(실험 런) |
| `tag_migrate.py`, `tag-migrate.sh`, `tag_retag_plan.py` | manifest 기반 일회성 태그 이전·재태깅. 운영자 재태깅 표는 공개판에 없으니 `--map`으로 자기 표를 준다(형식은 `tests/fixtures/tag_retag/`). `tag-migrate.sh`의 지울 태그 목록은 `policies/local/tag-migrate-cleanup.json`(`{"delete_tags": [...], "remove_tags": [...]}`, 경로는 `HC_TAG_MIGRATE_CLEANUP`로 바꿀 수 있음)에서 읽고, 없으면 빈 목록이다 |
| `duplicate_recall.py` | 감사 로그로 중복 recall 출처를 센다(일회성 분석, 집계만 출력) |
| `scheduled-checks.sh`, `scheduled_checks.py` | 날짜가 정해진 운영 후속 점검(카나리 판정, 태그 재측정) |
| `contract-probe.sh`, `mcp-probe.sh` | upstream 이미지를 바꿀 때 계약 fixture를 upstream 직결로 다시 채집한다 |

## 공개판 관리용

| 항목 | 하는 일 |
|---|---|
| `public-export/` | 경계 매니페스트(`manifest.json`)·검사기(`check_manifest.py`)·내보내기 도구(`export.py`). 사용법은 `docs/public-export.md` |
| `README.md` | 이 분류표 |
