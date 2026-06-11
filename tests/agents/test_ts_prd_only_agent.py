from qapilot.agents.ts_prd_only_agent import (
    _fallback_ts_list,
    _functional_requirements,
)


def test_functional_requirements_filters_out_non_functional_items():
    requirements = [
        {
            "req_id": "FR-AUTH-01",
            "req_type": "functional",
            "content": "회원가입할 수 있다.",
            "priority": "high",
            "domain_area": "인증",
        },
        {
            "req_id": "REQ-001",
            "req_type": "non_functional",
            "content": "응답시간은 1초 이내여야 한다.",
            "priority": "medium",
            "domain_area": "성능",
        },
    ]

    filtered = _functional_requirements(requirements)

    assert [item["req_id"] for item in filtered] == ["FR-AUTH-01"]


def test_fallback_ts_list_excludes_non_functional_requirements():
    requirements = [
        {
            "req_id": "FR-AUTH-01",
            "req_type": "functional",
            "content": "회원가입할 수 있다.",
            "priority": "high",
            "domain_area": "인증",
        },
        {
            "req_id": "REQ-001",
            "req_type": "non_functional",
            "content": "응답시간은 1초 이내여야 한다.",
            "priority": "medium",
            "domain_area": "성능",
        },
    ]

    ts_list = _fallback_ts_list(requirements)

    assert ts_list == [
        {
            "name": "인증 시나리오",
            "description": "인증 관련 요구사항 검증",
            "domain_area": "인증",
            "requirements": ["FR-AUTH-01"],
        }
    ]
