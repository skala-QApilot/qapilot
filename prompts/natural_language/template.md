# 이전 대화 이력

{{conversation_history}}

# 코드베이스 컨텍스트

{{scan_result_summary}}

# 도메인 규칙

{{domain_rules}}

# 유사 시나리오 후보 (임베딩 검색 결과)

{{top_candidates}}

# 사용자 입력

{{user_input}}

# 지시사항

위 정보를 참조하여 사용자 입력을 분석하고 아래 중 하나로 응답하라.

1. 시나리오 생성/수정이 가능하면 sufficient + requirements 배열 반환
2. 정보가 부족하면 insufficient + query_feedback(보충 질문) 반환
3. QA와 무관한 질의면 rejected + query_feedback(안내 메시지) 반환

이전 대화 이력이 있으면 현재 입력과 합쳐서 의미를 파악하라.
유사 시나리오 후보가 있으면 수정 요청으로 판단하고, 없으면 신규 생성으로 판단하라.
JSON 외에 아무것도 출력하지 않는다.
