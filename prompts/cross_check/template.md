# 컨텍스트

{{context}}

# 입력 데이터

{{input_data}}

# 지시사항

위 컨텍스트와 입력 데이터를 기반으로 UI, API, DB 데이터를 비교하여 불일치를 탐지하라.

반드시 아래 JSON 형식으로만 응답하라. 다른 텍스트는 포함하지 않는다.

```json
{
  "error_code": "ui-api-mismatch / ui-db-mismatch / api-db-mismatch / ui-api-db-mismatch / none",
  "summary": "불일치 유형에 따라 작성 (system.md 참조)",
  "mismatches": [
    {
      "field": "필드명",
      "ui_value": "UI 값",
      "api_value": "API 값",
      "db_value": "DB 값 또는 null"
    }
  ],
  "matched_fields": 0,
  "match_score": 0.0
}
```

- matched_fields: 비교한 전체 필드 중 UI/API/DB 값이 일치한 필드 수
- 불일치가 없으면 error_code는 "none", mismatches는 빈 배열, match_score는 1.0으로 반환한다.