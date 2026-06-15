아래 TC 의 `values` 칸을 실제 사용 가능한 값으로 채워라.

## TC 정보

- **name**: {{tc_name}}
- **api**: {{tc_api}}
- **req_id**: {{tc_req_id}}
- **tags**: {{tc_tags}}
- **given**: {{tc_given}}
- **when**: {{tc_when}}
- **then**: {{tc_then}}

## 기존 values (placeholder)

{{existing_values}}

## 관련 스키마 (sensitive 필드 제외됨)

{{schemas}}

## 관련 셀렉터 (이 TC 가 사용할 frontend 페이지의 input/button)

{{selectors}}

## 관련 테스트 패턴 (SUT 의 기존 테스트 코드 일부)

{{patterns}}

## DB 실제 데이터 일부 (sensitive 컬럼 제외됨)

{{db_snapshot}}

## 코드베이스 본문 (production 코드 — 실 구현 로직)

이 TC 가 호출할 endpoint 의 실제 구현 코드 일부다. 검증 규칙 / 에러 메시지 / 응답 형식을 정확히 보고 그에 맞는 값을 만들어라.

{{source_snippets}}

## 같은 시나리오(TS)의 이전 TC 들이 이미 사용한 값

{{same_ts_values}}

## 이전 검증 실패 피드백 (있는 경우)

{{validation_feedback}}

---

## 지시사항

1. 위 스키마의 `validators` (min_length / max_length / pattern / required) 를 **반드시 만족**시켜라.
2. TC 의 `tags` 와 `then` 을 보고 시나리오 의도를 파악하라 — 성공 케이스 / 실패 케이스 / 경계값.
3. `tags=[edge_case]` + `then` 절이 "409 Conflict" 또는 "이미 존재" 등이면 위 DB 데이터의 값을 그대로 사용.
4. `tags=[edge_case]` + `then` 절이 "400 Bad Request" 등 validation fail 이면 의도적으로 schema 위반.
5. **sensitive 필드 (password / token / secret / api_key 등) 는 values 에 포함하지 마라** — 시스템이 별도 처리.
6. **위 "코드베이스 본문" 의 실 구현 로직을 보고** 검증 규칙 (예: `existing = db.scalar(...)` → 중복 체크 / `raise HTTPException(409, ...)` → 409 에러 메시지) 을 정확히 반영하라. 문서에 없어도 코드에 있으면 우선.
7. **unique 제약 필드 (email / username / phone 등) 는 위 "같은 시나리오(TS)의 이전 TC 들이 이미 사용한 값" 과 절대 중복되지 않게 하라.** 단, `then` 절이 "이미 존재 / 409 / 중복" 류의 의도적 중복 케이스면 예외 — 그때는 DB 데이터 또는 이전 TC 값을 그대로 재사용해야 한다.
8. 추론 불가능한 값이면 `purpose` 에 "스키마 정보 부족" 명시.

JSON 만 출력하라. 마크다운 코드블록 X.
