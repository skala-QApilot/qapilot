# 컨텍스트

{{context}}

# 입력 데이터

{{input_data}}

# 지시사항

위 원인 후보 목록 전체를 함께 고려하여 해결 가이드를 생성하라.

- 원인 후보(candidate) 하나당 suggestion을 정확히 하나씩 생성하라.
- suggestion 순서는 candidate의 rank 순서와 일치시켜라.
- 각 suggestion의 description은 아래 순서로 2~3문장 이상 작성하라.
  - [위치] 어느 파일/함수/레이어에서 문제가 발생하는가
  - [문제] 무엇이 문제인가 (증상 + 원인)
  - [수정] 구체적으로 어떻게 수정해야 하는가
  - [기대 결과] "수정이 완료되면 ~해야 한다" 형식으로 조건부로 서술한다.
- description은 단정적인 문체로 작성하라. "~가능성이 있다", "~수 있다" 표현은 사용하지 않는다.
- file_path는 evidence의 code_location에서 명확히 추출 가능한 경우에만 채우고, 불확실하면 빈 문자열을 사용하라.
- blame_author, code_snippet, similar_issues는 이번 단계에서 기본값으로 둔다.
- JSON만 반환하라.
