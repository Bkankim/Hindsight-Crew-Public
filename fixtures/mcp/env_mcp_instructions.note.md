# HINDSIGHT_API_MCP_INSTRUCTIONS 실측

채집: 2026-10-29T10:13:50Z (upstream 직결, `docker compose exec hindsight printenv`)

상태: **unset - S2 착수 전 실측 필요**

`printenv` 가 rc=1(미설정). compose 에 이 env 가 없어 upstream 은 initialize instructions 를 발신하지 않는다.
S2(description suffix / instructions upstream 위임) 착수 전, env 주입 -> hindsight 재기동 -> upstream 직결 재채집 순으로 실측할 것.
