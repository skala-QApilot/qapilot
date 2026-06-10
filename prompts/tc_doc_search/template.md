아래 테스트 시나리오 정보와 검색된 문서를 바탕으로 3단계 분석을 수행한 후 TC를 생성하라.

## 테스트 시나리오

- **이름**: {{ts_name}}
- **설명**: {{ts_description}}
- **관련 요구사항**:
{{requirements}}

## 검색된 문서

{{retrieved_docs}}

## 지시사항

아래 3단계를 순서대로 수행하고 결과를 JSON 하나로 출력하라.

**1단계 — 제약 및 예외 추출**
위 문서에서 입력 제약, 비즈니스 규칙, 인증/권한 조건, 예외 처리 항목을 모두 식별하라.
문서에 명시된 내용만 추출하고 추측하지 마라.
각 항목을 `analysis` 배열에 기록하라 (constraint, source, type, techniques, test_points).

**2단계 — 테스트 기법 결정**
각 제약에 동등 분할 / 경계값 분석 / 예외 검증 / 상태 전이 / 인증 검증 / 결정 테이블 중
적합한 기법을 `techniques`에 명시하라.

**3단계 — TC 작성**
`analysis`의 각 `test_point`를 TC로 변환하라.
각 TC에 `technique` 필드로 어떤 기법 근거인지 명시하라.
TC 유형별 최소 개수를 반드시 충족하라: normal 2+, edge_case 2+, boundary 1+, auth 1+ (총 최소 6개).
 given/when/then은 완전한 한국어 문장으로 작성하되, 문서에 없는 핵심 주장(에러 메시지, 상태 코드, 중복 데이터 값 등)은 지어내지 마라.
 `when` 문장 안에 엔드포인트를 썼다면 `api` 필드는 그 엔드포인트와 정확히 같은 METHOD/path 를 써라.
 `api` 에 router prefix 를 중복해서 붙이지 마라. 예: `PATCH /api/orders/{order_id}/change-plan` 는 가능하지만 `PATCH /api/orders/api/orders/{order_id}/change-plan` 는 금지다.
 `then`은 채널별 결과를 한 문자열 안에 `UI) ...`, `API) ...`, `DB) ...` 형식으로 적어라.
 여러 backend 를 구분해야 하면 `API[service-name]) ...` 형식을 사용해도 된다.
 여러 저장소/스키마를 구분해야 하면 `DB[schema-or-store]) ...` 형식을 사용해도 된다.
 필요한 채널만 포함하고, 불필요한 채널은 아예 쓰지 마라. 여러 채널이 있으면 줄바꿈(`\n`)으로 구분하라.
문서에 없어서 알 수 없는 부분은 전체 문장 속에 확정형 서술을 넣지 말고 `{설명}` 형식으로 남겨라.
예:
- `"then": "UI) {중복 이메일 오류 메시지}가 표시된다."`
- `"then": "API) {HTTP 상태코드} 오류가 반환된다."`
- `"then": "UI) 이메일, 이름, 가입 일자가 표시된다.\nDB) users에 사용자 정보가 저장된다."`
- `"then": "API[auth-service]) 401 오류가 반환된다.\nDB[user-db]) users에 로그인 실패 이력이 저장된다."`
- `"when": "PATCH /api/orders/{order_id}/change-plan 엔드포인트로 요금제 변경 요청을 하면"`
- `"api": "PATCH /api/orders/{order_id}/change-plan"`
- `"value": "{duplicate email}"`
values에는 위 "검색된 문서"에서 추론 가능한 값은 구체적으로 쓰고, 문서에 없어서 알 수 없는 값은 `{설명}` 형식으로 표기하라 (예: `{생년월일 YYYY-MM-DD}`, `{Bearer 토큰}`).
각 TC에 `sources` 필드로 해당 TC 작성에 사용한 문서명을 기재하라. 코드베이스를 스캔했으면 `"codebase"` 포함, 현재처럼 문서만 있으면 문서명만 기재한다.

JSON만 출력하라. 마크다운 코드블록이나 설명 텍스트를 포함하지 마라.
