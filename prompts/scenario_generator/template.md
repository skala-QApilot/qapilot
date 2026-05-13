# 도메인 규칙

{{domain_rules}}

# 요구사항 목록

{{requirements}}

# 코드베이스 분석 결과

{{scan_summary}}

## 분석 대상 파일
{{affected_files}}

## 트리거
{{trigger}}

# PRD-코드 불일치 탐지 결과

{{mismatch_note}}

# 지시사항

위 정보를 바탕으로 다음 규칙에 따라 테스트 시나리오를 생성하라.

1. 각 도메인 기능별로 TS를 1개 이상 생성하라.
2. 각 TS에는 정상(normal)과 엣지케이스(edge_case)를 반드시 쌍으로 포함하라.
3. 경계값(boundary) TC는 숫자·문자열 입력이 있는 기능에 추가하라 (최솟값/최댓값/0/null 등).
4. 로그인이 필요한 기능에는 권한(auth) TC를 추가하라.
5. 중복 요청 가능성이 있는 기능에는 동시성(concurrency) TC를 추가하라.
6. 각 TC의 req_id는 요구사항 목록의 REQ-XXX 중 가장 연관성 높은 것을 매핑하라.
7. PRD-코드 불일치 항목의 도메인과 관련된 TS의 description에 "⚠️ 미구현 의심" 메모를 포함하라.
8. JSON 형식으로만 출력하라. 설명이나 마크다운 블록 없이 JSON만 반환하라.
