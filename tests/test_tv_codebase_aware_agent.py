"""TVFromCodebaseAgent 단위 테스트 — mock LLM."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from qapilot.agents.tv_codebase_aware_agent import (
    NO_EVIDENCE,
    TVFromCodebaseAgent,
    _build_type_hint_map,
    _extract_json,
    _infer_scenario_intent,
    _pick_schema_name,
)


# ────────────────────────────────────────────────────────────────────────
# _extract_json
# ────────────────────────────────────────────────────────────────────────

def test_extract_json_basic():
    text = '{"values": [{"field": "email"}]}'
    assert _extract_json(text) == text


def test_extract_json_with_markdown():
    text = '```json\n{"values": [{"field": "email"}]}\n```'
    assert _extract_json(text) == '{"values": [{"field": "email"}]}'


def test_extract_json_with_text_before():
    text = 'some explanation\n{"values": []}'
    assert _extract_json(text) == '{"values": []}'


def test_extract_json_handles_string_with_braces():
    """문자열 내부의 { } 가 depth 계산에 영향 안 줘야."""
    text = '{"key": "value with {brace}", "x": 1}'
    assert _extract_json(text) == text


# ────────────────────────────────────────────────────────────────────────
# _infer_scenario_intent
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("then_text,tags,expected_key,expected_value", [
    ("409 Conflict — 이미 존재하는 이메일", ["edge_case"], "expects_existing_in_db", True),
    ("201 Created — 신규 가입 성공", ["normal"], "expects_absent_in_db", True),
    ("400 Bad Request — 이메일 형식 오류", ["edge_case"], None, None),
    ("200 OK", ["normal"], None, None),  # 201/created 키워드 없음
])
def test_infer_scenario_intent(then_text, tags, expected_key, expected_value):
    tc = {"then": then_text, "tags": tags}
    intent = _infer_scenario_intent(tc)
    if expected_key:
        assert intent.get(expected_key) is expected_value
    else:
        assert "expects_existing_in_db" not in intent
        assert "expects_absent_in_db" not in intent


def test_infer_scenario_intent_no_then():
    intent = _infer_scenario_intent({"then": "", "tags": []})
    assert intent == {}


# ────────────────────────────────────────────────────────────────────────
# _pick_schema_name
# ────────────────────────────────────────────────────────────────────────

def test_pick_schema_name_request_priority():
    schemas = {
        "request_schemas": {"SignupRequest": {}, "LoginRequest": {}},
        "db_models": {"Customer": {}},
    }
    name = _pick_schema_name(schemas)
    # request_schemas 의 첫 번째 (dict 순서 보장)
    assert name in {"SignupRequest", "LoginRequest"}


def test_pick_schema_name_no_request():
    schemas = {"request_schemas": {}, "db_models": {"Customer": {}}}
    assert _pick_schema_name(schemas) is None


# ────────────────────────────────────────────────────────────────────────
# _build_type_hint_map
# ────────────────────────────────────────────────────────────────────────

def test_build_type_hint_map_from_request_schemas():
    schemas = {
        "request_schemas": {
            "SignupRequest": {"fields": [
                {"name": "email", "type": "str"},
                {"name": "age", "type": "int"},
            ]},
        },
        "db_models": {},
    }
    assert _build_type_hint_map(schemas) == {"email": "str", "age": "int"}


def test_build_type_hint_map_db_models_fallback():
    """request_schemas 에 없으면 db_models 에서."""
    schemas = {
        "request_schemas": {},
        "db_models": {
            "Customer": {"columns": [{"name": "email", "type": "str"}]},
        },
    }
    assert _build_type_hint_map(schemas) == {"email": "str"}


def test_build_type_hint_map_request_overrides_db():
    """같은 필드면 request 우선."""
    schemas = {
        "request_schemas": {"R": {"fields": [{"name": "email", "type": "EmailStr"}]}},
        "db_models": {"M": {"columns": [{"name": "email", "type": "str"}]}},
    }
    assert _build_type_hint_map(schemas)["email"] == "EmailStr"


# ────────────────────────────────────────────────────────────────────────
# _parse — evidence(근거) 필드
# ────────────────────────────────────────────────────────────────────────

@pytest.fixture
def agent():
    return TVFromCodebaseAgent(trace_id="test-evidence")


def test_parse_evidence_present(agent):
    content = json.dumps({
        "given": "g", "when": "w", "then": "t",
        "evidence": {"given": "근거 없음", "when": "PRD_v4.0.md", "then": "app/routers/auth.py:42-60"},
        "values": [
            {"field": "email", "value": "a@b.com", "type": "string", "purpose": "p", "source": "llm", "evidence": "PRD_v4.0.md"},
        ],
        "confidence": 0.9,
    })
    parsed = agent._parse(content)
    assert parsed["evidence"] == {
        "given": "근거 없음", "when": "PRD_v4.0.md", "then": "app/routers/auth.py:42-60",
    }
    assert parsed["values"][0]["evidence"] == "PRD_v4.0.md"


def test_parse_evidence_missing_defaults_to_no_evidence(agent):
    """evidence 필드가 통째로 없거나 value 별 evidence 가 없으면 '근거 없음'으로 채운다."""
    content = json.dumps({
        "given": "g", "when": "w", "then": "t",
        "values": [
            {"field": "email", "value": "a@b.com", "type": "string", "purpose": "p", "source": "llm"},
        ],
        "confidence": 0.9,
    })
    parsed = agent._parse(content)
    assert parsed["evidence"] == {"given": NO_EVIDENCE, "when": NO_EVIDENCE, "then": NO_EVIDENCE}
    assert parsed["values"][0]["evidence"] == NO_EVIDENCE


def test_parse_error_returns_no_evidence(agent):
    parsed = agent._parse("not json")
    assert parsed["evidence"] == {"given": NO_EVIDENCE, "when": NO_EVIDENCE, "then": NO_EVIDENCE}
    assert parsed["values"] == []
