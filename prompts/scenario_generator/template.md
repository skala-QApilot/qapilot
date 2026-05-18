# 도메인 규칙

{{domain_rules}}

# 검증 대상 요구사항

{{requirements}}

# 코드베이스 분석 결과

{{scan_summary}}

## 분석 대상 파일
{{affected_files}}

# 코드 인덱스 (함수·모델·변경 상세)

{{code_index}}

## 트리거
{{trigger}}

# PRD-코드 불일치 탐지 결과

{{mismatch_note}}

# 지시사항

위 정보를 바탕으로 다음 규칙에 따라 테스트 시나리오를 생성하라.

**[필수] 코드 인덱스 분석 절차 (시작 전 반드시 수행):**
"핸들러 소스 코드"와 "헬퍼 함수 소스 코드" 섹션을 읽고 아래 항목을 추출하라:
- 각 if/elif 조건과 그에 따른 HTTPException 상태 코드 및 에러 메시지
- 코드에 나오는 실제 enum/상수 값 (예: status="CONFIRMED", contract_type="NONE" 등)
- 검증 함수 이름과 호출 조건, 외부 서비스 호출 여부
이 분석 결과를 기반으로 TC의 given/when/then과 values를 채워라. 코드에 없는 값은 지어내지 마라.

1. **"검증 대상 요구사항"에 명시된 요구사항을 검증하는 TS를 정확히 1개 생성하라.** scenarios 배열에 원소가 1개여야 한다.
2. "관련 엔드포인트" 목록에 있는 엔드포인트를 모두 커버하는 TC를 작성하라.
3. TC 유형별 최소 개수:
   - normal: 2개 이상 (happy path + 정상 변형)
   - edge_case: 2개 이상 — **코드의 각 에러 분기(HTTPException)마다 별도 TC**
   - boundary: 1개 이상 (null/빈값/최솟값/최댓값 등 각각 별도 TC)
   - auth: 1개 이상 (JWT 없음 / 만료 JWT / 타인 리소스 접근 — 각각 별도 TC)
4. 엣지케이스는 "실패 1개"로 묶지 말고, HTTPException이 발생하는 조건마다 별도 TC를 작성하라.
5. 중복 요청 가능성이 있는 기능에는 동시성(concurrency) TC를 추가하라.
6. 각 TC의 values는 최소 2개 이상이며, 코드에서 확인한 실제 값을 사용하라.
7. 모든 TC의 req_id는 "검증 대상 요구사항"의 REQ-XXX로 설정하라.
8. JSON 형식으로만 출력하라. 설명이나 마크다운 블록 없이 JSON만 반환하라.
