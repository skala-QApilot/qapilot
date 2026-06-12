아래 TC 골격의 given/when/then 과 value 를 **함께, 모순 없이** 작성하라.

## TC 골격 (given/when/then/value 는 아직 비어 있음 — 네가 만든다)

- **name**: {{tc_name}}
- **intent**: {{tc_intent}}
- **technique**: {{tc_technique}}
- **api**: {{tc_api}}
- **req_id**: {{tc_req_id}}
- **tags**: {{tc_tags}}

## 검색된 문서 (given/when/then 의 비즈니스 근거 — 정책서/약관/API)

{{retrieved_docs}}

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

## 이전 검증 실패 피드백 (있는 경우)

{{validation_feedback}}

---

## 지시사항

1. **먼저 `intent` 와 검색 문서, 코드 제약을 종합**해 이 TC 가 무엇을 검증하는지 정하라.
2. 그 의도에 맞는 **given/when/then(완전한 한국어 문장)과 values 를 동시에** 작성하라. when 이 가리키는 입력과 value 가 서로 모순되면 안 된다.
3. 위 스키마의 `validators` (min_length / max_length / pattern / required) 를 정상 케이스면 **만족**, 검증 위반 의도면 **정확히 그 제약을 위반**시키고 when 문장도 같은 위반을 서술하라.
4. `intent=expects_existing` 이면 위 DB 데이터의 값을 그대로 사용(`source: "db"`). `intent=expects_absent` 면 DB 에 없는 신규 값.
5. **sensitive 필드 (password / token / secret / api_key 등) 는 values 에 포함하지 마라** — 시스템이 별도 처리. (단 when/then 문장에서 "비밀번호 7자" 처럼 언급은 가능)
6. **위 "코드베이스 본문" 의 실 구현 로직을 보고** 검증 규칙 (예: `existing = db.scalar(...)` → 중복 체크 / `raise HTTPException(409, ...)` → 409 에러 메시지) 을 then 절과 value 에 정확히 반영하라. 문서에 없어도 코드에 있으면 우선.
7. 추론 불가능한 값이면 `purpose` 에 "스키마 정보 부족" 명시.
8. **`evidence` 필드를 빠짐없이 채워라.** given/when/then 각각, 그리고 values 의 각 항목마다 어떤 검색 문서(파일명) 또는 코드 위치(`file:Lstart-Lend`)를 근거로 작성했는지 적어라. 스키마 제약이 근거면 `"스키마: <필드명>"`. 위 입력 자료 어디에도 근거가 없으면 정확히 `"근거 없음"` 이라고 써라 (다른 표현 금지 — UI 가 이 문자열을 그대로 태그로 사용한다). 근거를 지어내지 마라.

JSON 만 출력하라. 마크다운 코드블록 X.
