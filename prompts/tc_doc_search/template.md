아래 테스트 시나리오 정보와 검색된 문서를 바탕으로 3단계 분석을 수행한 후 TC 골격을 열거하라.

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

**3단계 — TC 열거**
`analysis`의 각 `test_point`를 TC 1개로 열거하라.
각 TC는 골격만 정한다 — **given/when/then 과 value 는 작성하지 마라** (후속 통합 단계가 문서+코드베이스+DB를 보고 함께 생성한다).
각 TC에 정할 것: `name`, `technique`(기법 근거), `intent`(normal/expects_existing/expects_absent/expects_validation_error/boundary/auth 중 하나), `tags`, `api`, `req_id`, `depends_on`.
**TS 1개당 최대 5개**까지만 열거하라 (반복 실험용 상한). 유형 다양성을 우선하되(normal 1개 이상 + edge_case/boundary/auth 중 중요한 것부터), 5개를 넘기지 마라.
각 TC에 `sources` 필드로 해당 TC를 도출한 문서명을 기재하라. (이 단계는 코드베이스를 보지 않으므로 `"codebase"` 는 포함하지 않는다.)

JSON만 출력하라. 마크다운 코드블록이나 설명 텍스트를 포함하지 마라.
