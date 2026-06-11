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


def test_parse_preserves_channel_prefixed_then():
    agent = TCFromDocsAgent(trace_id="tc-doc-search-test")
    payload = {
        "analysis": [],
        "confidence": 0.81,
        "test_cases": [
            {
                "name": "회원가입 성공",
                "technique": "동등 분할",
                "given": "사용자가 유효한 가입 정보를 입력한다.",
                "when": "회원가입을 완료한다.",
                "then": "UI) 이메일, 이름, 가입 일자가 표시된다.\nDB) users에 사용자 정보가 저장된다.",
                "values": [],
                "tags": ["normal"],
                "sources": ["PRD_v1.0.md"],
                "depends_on": [],
            }
        ],
    }

    test_cases, _analysis, confidence = agent._parse(json.dumps(payload, ensure_ascii=False))

    assert confidence == 0.81
    assert test_cases[0]["then"] == (
        "UI) 이메일, 이름, 가입 일자가 표시된다.\n"
        "DB) users에 사용자 정보가 저장된다."
    )
    assert test_cases[0]["then_status"] == "grounded_doc"


def test_parse_aligns_api_with_when_inline_endpoint():
    agent = TCFromDocsAgent(trace_id="tc-doc-search-test")
    payload = {
        "analysis": [],
        "confidence": 0.79,
        "test_cases": [
            {
                "name": "요금제 변경",
                "technique": "상태 전이",
                "given": "사용자가 유효한 주문을 가지고 있다.",
                "when": "PATCH /api/orders/{order_id}/change-plan 엔드포인트로 요금제 변경 요청을 하면",
                "then": "API) 변경된 주문 정보가 반환된다.",
                "values": [],
                "tags": ["normal"],
                "api": "PATCH /api/orders/api/orders/{order_id}/cancel",
                "sources": ["PRD_v4.0.md"],
                "depends_on": [],
            }
        ],
    }

    test_cases, _analysis, confidence = agent._parse(json.dumps(payload, ensure_ascii=False))

    assert confidence == 0.79
    assert test_cases[0]["api"] == "PATCH /api/orders/{order_id}/change-plan"


def test_parse_dedupes_duplicated_api_prefix_without_when_inline_endpoint():
    agent = TCFromDocsAgent(trace_id="tc-doc-search-test")
    payload = {
        "analysis": [],
        "confidence": 0.75,
        "test_cases": [
            {
                "name": "주문 생성",
                "technique": "동등 분할",
                "given": "사용자가 유효한 정보를 입력한다.",
                "when": "주문 생성을 요청한다.",
                "then": "API) 생성된 주문 정보가 반환된다.",
                "values": [],
                "tags": ["normal"],
                "api": "POST /api/orders/api/orders",
                "sources": ["PRD_v4.0.md"],
                "depends_on": [],
            }
        ],
    }

    test_cases, _analysis, confidence = agent._parse(json.dumps(payload, ensure_ascii=False))

    assert confidence == 0.75
    assert test_cases[0]["api"] == "POST /api/orders"


def test_parse_consolidates_semantically_duplicated_normal_happy_paths():
    agent = TCFromDocsAgent(trace_id="tc-doc-search-test")
    payload = {
        "analysis": [],
        "confidence": 0.82,
        "test_cases": [
            {
                "name": "유효한 정보로 회원가입이 완료되는지 확인한다",
                "technique": "동등 분할",
                "given": "사용자가 회원가입 화면에 접근해 있고, 유효한 이메일 형식과 비밀번호, 이름을 입력할 수 있는 상태이다.",
                "when": "사용자가 회원가입 화면에서 이메일, 비밀번호, 이름을 입력하고 회원가입을 요청한다.",
                "then": "UI) 회원가입이 완료된다.",
                "values": [{"field": "email", "value": "test@example.com", "type": "string", "purpose": "이메일"}],
                "tags": ["normal"],
                "api": "POST /api/auth/signup",
                "req_id": "FR-AUTH-01",
                "sources": ["PRD_v4.0.md"],
                "depends_on": [],
            },
            {
                "name": "회원가입 성공 후 로그인 화면으로 이동하는지 확인한다",
                "technique": "동등 분할",
                "given": "사용자가 회원가입 화면에 접근해 있고, 유효한 이메일 형식과 비밀번호, 이름을 입력할 수 있는 상태이다.",
                "when": "사용자가 회원가입 화면에서 이메일, 비밀번호, 이름을 입력하고 회원가입을 요청한다.",
                "then": "UI) 로그인 화면으로 이동한다.",
                "values": [{"field": "name", "value": "홍길동", "type": "string", "purpose": "이름"}],
                "tags": ["normal"],
                "api": "POST /api/auth/signup",
                "req_id": "FR-AUTH-01",
                "sources": ["PRD_v4.0.md"],
                "depends_on": [],
            },
        ],
    }

    test_cases, _analysis, confidence = agent._parse(json.dumps(payload, ensure_ascii=False))

    assert confidence == 0.82
    assert len(test_cases) == 1
    assert test_cases[0]["then"] == "UI) 회원가입이 완료된다.\nUI) 로그인 화면으로 이동한다."
    fields = [value["field"] for value in test_cases[0]["values"]]
    assert fields == ["email", "name"]
