아래 TC 의 `values` 칸을 실제 사용 가능한 값으로 채워라.

## TC 정보

- **name**: {{tc_name}}
- **api**: {{tc_api}}
- **req_id**: {{tc_req_id}}
- **tags**: {{tc_tags}}
- **given**: {{tc_given}}
- **when**: {{tc_when}}
- **then**: {{tc_then}}

## 아직 근거가 부족한 claim ({...} 포함)

{{unresolved_claims}}

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

## 관련 문서 (이 TC 의 api/req_id 로 재검색한 정책/명세 chunk)

코드에 없는 정책적 근거(예: 비즈니스 규칙, 제약 조건의 "왜")를 보강하는 용도다. 코드와 문서가 충돌하면 코드를 우선한다.

{{tc_doc_refs}}

## 이전 검증 실패 피드백 (있는 경우)

{{validation_feedback}}

---

## 지시사항

1. 위 스키마의 `validators` (min_length / max_length / pattern / required) 를 **반드시 만족**시켜라.
2. TC 의 `tags` 와 `then` 을 보고 시나리오 의도를 파악하라 — 성공 케이스 / 실패 케이스 / 경계값.
3. `tags=[edge_case]` + `then` 절이 "409 Conflict" 또는 "이미 존재" 등이면 위 DB 데이터의 값을 그대로 사용.
4. `tags=[edge_case]` + `then` 절이 "400 Bad Request" 등 validation fail 이면 의도적으로 schema 위반.
5. **sensitive 필드 (password / token / secret / api_key 등) 는 values 에 포함하지 마라** — 시스템이 별도 처리.
5-1. values 는 **실제 UI에서 사용자가 입력/체크하는 필드만** 포함하라.
     `password_hash`, `token`, `session_id`, 응답 전용 `id`, DB 내부 컬럼처럼
     UI에서 직접 입력하지 않는 값은 values 에 넣지 마라.
5-2. 단, UI 비밀번호 입력처럼 사용자가 실제로 입력하는 sensitive request 필드는
     시스템 placeholder 로 별도 머지되므로 LLM 응답에는 직접 쓰지 마라.
6. **위 "코드베이스 본문" 의 실 구현 로직을 보고** 검증 규칙 (예: `existing = db.scalar(...)` → 중복 체크 / `raise HTTPException(409, ...)` → 409 에러 메시지) 을 정확히 반영하라. "관련 문서"에 없어도 코드에 있으면 코드 우선.
7. 추론 불가능한 값이면 `purpose` 에 "스키마 정보 부족" 명시.
8. TC 의 `given`/`when`/`then` 에 이미 적힌 **확정형 주장**(에러 메시지, 구체적 동작 등) 중
   "코드베이스 본문" / "관련 문서" / 스키마 / DB 어디에서도 근거를 찾을 수 없는 것이 있으면
   그 문장을 `unverified_claims` 배열에 적어라. 근거가 있거나 확정형 주장이 없으면 `[]`.

근거가 있는 경우에만 `claims.given/when/then` 을 채워라. 근거가 부족하면 해당 key 는 생략하라.
`claims` 에는 `{...}` placeholder 를 제거한 최종 문장을 넣되, **위 코드/DB/스키마 근거를 우선**으로 하고, 코드만으로 부족하면 "관련 문서"의 정책 근거로 보강해 채워라. 둘 다 없으면 생략하라.
`claims.then` 은 기존 형식을 유지해 `UI) ...`, `API) ...`, `DB) ...` 접두어를 사용하라.
여러 backend 를 구분해야 하면 `API[service-name]) ...` 형식을 사용해도 된다.
여러 저장소/스키마를 구분해야 하면 `DB[schema-or-store]) ...` 형식을 사용해도 된다.
필요한 채널만 포함하고, 여러 채널이 필요하면 줄바꿈(`\n`)으로 구분하라.

UI에서 필요한 입력 control(input/checkbox/select)이 있으면 그 값은 빠뜨리지 마라.
예:
- signup 화면이면 `email`, `name`, `password`, `birth_date`, `guardian_consent` 같은 UI 입력 필드를 우선 채운다.
- 반대로 `password_hash`, `token`, `customer_id`, 응답 body 전용 `created_at` 같은 값은 values 에 넣지 않는다.

JSON 만 출력하라. 마크다운 코드블록 X.
