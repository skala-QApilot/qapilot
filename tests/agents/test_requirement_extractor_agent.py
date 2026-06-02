"""RequirementExtractorAgent 단위 테스트."""

import json

from qapilot.agents.requirement_extractor.agent import RequirementExtractorAgent


def test_parse_response_backfills_missing_document_fr_sections():
    """LLM이 누락한 PRD FR heading은 결정적으로 보강한다."""
    agent = RequirementExtractorAgent(trace_id="test-req")
    llm_content = json.dumps(
        {
            "requirements": [
                {
                    "req_id": "FR-AUTH-01",
                    "req_type": "functional",
                    "content": "[FR-AUTH-01] 회원가입",
                    "priority": "high",
                    "domain_area": "인증",
                }
            ],
            "confidence": 0.9,
        },
        ensure_ascii=False,
    )
    document_text = """
#### FR-AUTH-01 회원가입

| 항목 | 내용 |
|------|------|
| 엔드포인트 | `POST /api/auth/signup` |
| 처리 | 이메일과 비밀번호로 회원을 생성 |

#### FR-TIER-01 내 등급 조회

| 항목 | 내용 |
|------|------|
| 엔드포인트 | `GET /api/tier` |
| 처리 | 활성 회선 기반으로 현재 등급과 혜택 목록을 반환 |
"""

    requirements, confidence = agent._parse_response(llm_content, 0, document_text)

    by_id = {req["req_id"]: req for req in requirements}
    assert confidence == 0.9
    assert "FR-AUTH-01" in by_id
    assert by_id["FR-TIER-01"]["domain_area"] == "멤버십 등급"
    assert "GET /api/tier" in by_id["FR-TIER-01"]["content"]
