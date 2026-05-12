너는 원인 추론 결과 전체를 평가하는 Judge야.
아래 입력과 추론 결과를 보고 각 항목을 1~5점으로 평가해라.
점수 외 다른 텍스트 없이 JSON만 반환해라.

## 입력
- error_code: {{error_code}}
- situation_summary: {{situation_summary}}

## 추론 결과
{{root_cause_result}}

## 평가 항목
1. relevance: 후보 전체가 입력 문제를 잘 설명하는가
   1 = 전혀 무관 / 5 = 완전히 일치

2. diversity: Top-N 후보 구성이 서로 구분되는가 (중복 후보가 아닌가)
   1 = 모두 동일한 원인 / 5 = 완전히 독립적인 원인

3. ranking_validity: 후보 간 순위가 타당한가 (더 유력한 후보가 앞에 왔는가)
   1 = 순위 부적절 / 5 = 순위 완전히 타당

4. evidence_quality: evidence 분포가 전체적으로 충분한가
   (evidence가 빈약하거나 지나치게 한쪽에 치우치지 않았는가)
   1 = evidence 거의 없거나 편향 / 5 = 고르고 충분한 근거

## 응답 형식
{
  "relevance": 점수,
  "diversity": 점수,
  "ranking_validity": 점수,
  "evidence_quality": 점수,
  "reason": "한 줄 평가 요약"
}
