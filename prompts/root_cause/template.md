# 컨텍스트

{{context}}

# 입력 데이터

{{input_data}}

# 지시사항

위 컨텍스트와 입력 데이터를 기반으로 테스트 실패의 근본 원인을 분석하라.

- 원인 후보를 1~3개 도출하고 confidence 내림차순으로 rank를 부여하라.
- 각 후보에 대해 근거(evidence)를 포함하라. 근거 없으면 confidence를 0.3 이하로 설정하라.
- evidences.type은 "code_location" 또는 "runtime_data" 만 사용하라.
- JSON만 반환하라.
