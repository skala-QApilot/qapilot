"""TCFromDocsAgent placeholder/provenance 단위 테스트."""

from __future__ import annotations

import json

from qapilot.agents.tc_doc_search_agent import TCFromDocsAgent


def test_parse_marks_unresolved_claims_and_values():
    agent = TCFromDocsAgent(trace_id="tc-doc-search-test")
    payload = {
        "analysis": [],
        "confidence": 0.8,
        "test_cases": [
            {
                "name": "중복 이메일 회원가입 시도",
                "technique": "예외 검증",
                "given": "사용자가 {duplicate email} 을 입력한다.",
                "when": "회원가입을 시도한다.",
                "then": "{중복 이메일 오류 메시지}가 표시된다.",
                "values": [
                    {"field": "email", "value": "{duplicate email}", "type": "string", "purpose": "중복 이메일"},
                    {"field": "name", "value": "홍길동", "type": "string", "purpose": "이름"},
                ],
                "tags": ["edge_case"],
                "sources": ["PRD_v1.0.md"],
                "depends_on": [],
            }
        ],
    }

    test_cases, _analysis, confidence = agent._parse(json.dumps(payload, ensure_ascii=False))

    assert confidence == 0.8
    assert len(test_cases) == 1
    tc = test_cases[0]
    assert tc["given_status"] == "unresolved"
    assert tc["when_status"] == "grounded_doc"
    assert tc["then_status"] == "unresolved"
    assert tc["values"][0]["status"] == "unresolved"
    assert tc["values"][0]["source"] == "doc_draft"
    assert tc["values"][1]["status"] == "grounded_doc"
