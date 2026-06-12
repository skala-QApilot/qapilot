# 코드베이스 엔드포인트 목록

{{endpoints}}

# 프론트엔드 DOM 인덱스 (실제 존재하는 element)

{{frontend_dom}}

# 관련 route 메타데이터

{{route_context}}

# 관련 selector 카탈로그

{{selector_catalog}}

# 관련 backend schema

{{schema_context}}

# 관련 원본 소스

{{source_context}}

# 테스트 시나리오

{{scenarios}}

# 지시사항

위 엔드포인트 목록·프론트엔드 DOM 인덱스·테스트 시나리오를 기반으로
각 TC를 ActionMapping JSON 배열로 변환하라.

반드시 다음 순서로 사고하라:
1. 관련 route / component / backend handler 를 먼저 식별한다.
2. 원본 소스에서 공통 사용자 흐름을 먼저 복원한다.
3. 그 다음 TC 의 값/분기/성공·실패 조건만 반영해 step 을 만든다.
4. selector 는 실제 selector 카탈로그 / 원본 소스에 있는 것만 사용한다.

**[CRITICAL: intent-first 규칙 — selector 자유 생성 금지]**
- DOM action (`fill`/`click`/`assert_*` 등) 은 먼저 `target_name` / `target_kind` / `target_text`
  로 **의도(step intent)** 를 표현하라.
- `selector` / `selector_type` 은 위 "프론트엔드 DOM 인덱스" 에 실제 존재하는 원소로
  확실히 resolve 할 수 있을 때만 채운다.
- 인덱스에 없는 selector 를 새로 만들지 말고, resolve 불가 시 `selector=null`,
  `selector_type=null` 로 둔다.
- API 응답 코드 (`HTTP 401`, `404 Not Found`) 나 DB 내부 상태값
  (`CONFIRMED`, `ACTIVE`) 을 `selector` 또는 `expected` 에 그대로 작성하지 마라.
  화면에 실제 표시될 사용자 친화적 텍스트로 치환하라 (인덱스의 element text 우선).

JSON 배열 외에 아무것도 출력하지 않는다.
