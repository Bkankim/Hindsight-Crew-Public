#!/usr/bin/env bash
# 운영 문서 목록을 JSONL({"id","created_at","tags"})로 stdout에 낸다 (issue #42). 읽기만 한다.
# tag-migrate.sh와 같은 방식: 컨테이너 안에서 GET .../banks/<bank>/documents 를 페이지네이션한다.
# 사용: scripts/tag-hygiene-export.sh [--bank personal-demo] > docs.jsonl
set -euo pipefail

cd "$(dirname "$0")/.."
set -a && . ./.env.local && set +a
. "$PWD/scripts/hc-host-env.sh"; hc_load_host_env "$PWD"

# export 는 컨테이너 안에서 돌아 호스트 설정을 못 읽는다: 호출자가 --bank 를 안 주면 HC_PERSONAL_BANK(.env.host.local, issue #186)를 넘긴다.
bank_args=()
case " $* " in
  *" --bank "*|*" --bank="*) ;;
  *) [ -n "${HC_PERSONAL_BANK:-}" ] && bank_args=(--bank "$HC_PERSONAL_BANK") ;;
esac
docker compose exec -T hindsight python - export ${bank_args[@]+"${bank_args[@]}"} "$@" < scripts/tag_hygiene.py
