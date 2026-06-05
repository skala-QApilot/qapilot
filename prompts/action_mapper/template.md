# 코드베이스 엔드포인트 목록

{{endpoints}}

# 프론트엔드 DOM 인덱스 (실제 존재하는 element)

{{frontend_dom}}

# 테스트 시나리오

{{scenarios}}

# 지시사항

위 엔드포인트 목록·프론트엔드 DOM 인덱스·테스트 시나리오를 기반으로
각 TC를 ActionMapping JSON 배열로 변환하라.

**[CRITICAL: selector 작성 규칙 — 이슈 #127]**
- DOM action (`fill`/`click`/`assert_*` 등) 의 `selector`/`expected` 값은
  **위 "프론트엔드 DOM 인덱스" 에 실제 존재하는 텍스트/속성을 우선 사용**하라.
- 인덱스에 없는 selector 를 새로 만들지 말고, 반드시 인덱스의 실제 값 중 하나를 사용하라.
- API 응답 코드 (`HTTP 401`, `404 Not Found`) 나 DB 내부 상태값
  (`CONFIRMED`, `ACTIVE`) 을 `selector` 또는 `expected` 에 그대로 작성하지 마라.
  화면에 실제 표시될 사용자 친화적 텍스트로 치환하라 (인덱스의 element text 우선).

JSON 배열 외에 아무것도 출력하지 않는다.
