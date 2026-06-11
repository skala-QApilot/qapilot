"""정합성 병합(given/when/then/value 함께 생성) 단위 테스트.

설계: TCFromDocsAgent 는 TC 골격만 열거하고, TVFromCodebaseAgent 가
given/when/then 과 value 를 한 컨텍스트에서 함께 생성한다.
"""

from __future__ import annotations

import pytest

from qapilot.agents.tc_doc_search_agent import TCFromDocsAgent
from qapilot.agents.tv_codebase_aware_agent import (
    TVFromCodebaseAgent,
    _resolve_scenario_intent,
)


# ────────────────────────────────────────────────────────────────────────
# TCFromDocsAgent — 골격(skeleton)만 열거, gwt/value 는 만들지 않는다
# ────────────────────────────────────────────────────────────────────────

def test_tc_parse_returns_skeleton_only():
    """given/when/then/value 가 응답에 있어도 골격만 남기고 버린다."""
    tc = TCFromDocsAgent(trace_id="t")
    raw = (
        '{"analysis": [], "test_cases": [{'
        '"name": "정상 가입", "technique": "동등분할", "intent": "expects_absent",'
        '"tags": ["normal"], "api": "POST /api/auth/signup", "req_id": "FR-01",'
        '"given": "버려져야 함", "when": "버려져야 함", "then": "버려져야 함",'
        '"values": [{"field": "email"}], "depends_on": []'
        '}], "confidence": 0.8}'
    )
    test_cases, _analysis, conf = tc._parse(raw)
    assert len(test_cases) == 1
    skel = test_cases[0]
    # 골격 필드는 보존
    assert skel["name"] == "정상 가입"
    assert skel["intent"] == "expects_absent"
    assert skel["api"] == "POST /api/auth/signup"
    # gwt/value 는 골격에 없어야 한다 (통합 단계가 생성)
    assert "given" not in skel
    assert "when" not in skel
    assert "then" not in skel
    assert "values" not in skel
    assert conf == 0.8


def test_tc_parse_defaults_intent_to_normal():
    tc = TCFromDocsAgent(trace_id="t")
    raw = '{"test_cases": [{"name": "x"}], "confidence": 0.5}'
    test_cases, _a, _c = tc._parse(raw)
    assert test_cases[0]["intent"] == "normal"


# ────────────────────────────────────────────────────────────────────────
# _resolve_scenario_intent — 골격 intent 우선, 비면 then 키워드 fallback
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("intent,expected", [
    ("expects_existing", {"expects_existing_in_db": True}),
    ("expects_absent", {"expects_absent_in_db": True}),
    ("normal", {}),
    ("boundary", {}),
    ("auth", {}),
    ("expects_validation_error", {}),
])
def test_resolve_scenario_intent_explicit(intent, expected):
    assert _resolve_scenario_intent({"intent": intent}) == expected


def test_resolve_scenario_intent_empty_falls_back_to_then_keywords():
    """intent 가 비어 있으면(레거시 TC) then/tags 키워드 추론으로 fallback."""
    tc = {"intent": "", "tags": ["edge_case"], "then": "409 Conflict — 이미 존재"}
    assert _resolve_scenario_intent(tc) == {"expects_existing_in_db": True}


# ────────────────────────────────────────────────────────────────────────
# TVFromCodebaseAgent._parse — given/when/then + values 를 함께 파싱
# ────────────────────────────────────────────────────────────────────────

def test_tv_parse_returns_gwt_and_values():
    tv = TVFromCodebaseAgent(trace_id="t")
    raw = (
        '{"given": "유효한 신규 가입 요청", "when": "회원가입 API 를 호출하면",'
        '"then": "201 Created 가 반환된다",'
        '"values": [{"field": "email", "value": "a@b.co", "type": "string"}],'
        '"confidence": 0.9}'
    )
    parsed = tv._parse(raw)
    assert parsed["gwt"] == {
        "given": "유효한 신규 가입 요청",
        "when": "회원가입 API 를 호출하면",
        "then": "201 Created 가 반환된다",
    }
    assert parsed["values"][0]["field"] == "email"
    assert parsed["values"][0]["value"] == "a@b.co"
    assert parsed["confidence"] == 0.9


def test_tv_parse_malformed_returns_empty_gwt():
    tv = TVFromCodebaseAgent(trace_id="t")
    parsed = tv._parse("not json at all")
    assert parsed["gwt"] == {"given": "", "when": "", "then": ""}
    assert parsed["values"] == []
    assert parsed["confidence"] == 0.0
