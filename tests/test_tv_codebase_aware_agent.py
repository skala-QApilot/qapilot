"""TVFromCodebaseAgent 단위 테스트 — mock LLM."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from qapilot.agents.tv_codebase_aware_agent import (
    TVFromCodebaseAgent,
    _build_type_hint_map,
    _collect_unresolved_claims,
    _extract_json,
    _format_unresolved_claims,
    _infer_scenario_intent,
    _pick_schema_name,
    _ui_input_values_only,
)
from qapilot.shared.sensitive_mask import build_sensitive_value_entries


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


@pytest.mark.parametrize("then_text,tags,expected", [
    ("400 Bad Request — 이메일 형식 오류", ["edge_case"], True),
    ("API) 400 오류가 반환된다.", ["boundary"], True),
    ("비밀번호 형식이 올바르지 않으면 오류가 표시된다", ["edge_case"], True),
    ("201 Created — 신규 가입 성공", ["normal"], False),
    ("400 Bad Request", ["normal"], False),  # edge_case/boundary 태그 없음
])
def test_infer_scenario_intent_format_violation(then_text, tags, expected):
    intent = _infer_scenario_intent({"then": then_text, "tags": tags})
    assert intent.get("expects_format_violation", False) is expected


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


def test_collect_unresolved_claims():
    tc = {
        "given": "사용자가 {duplicate email} 을 입력한다.",
        "when": "회원가입을 시도한다.",
        "then": "{중복 이메일 오류 메시지}가 표시된다.",
    }
    claims = _collect_unresolved_claims(tc)
    assert claims == {
        "given": "사용자가 {duplicate email} 을 입력한다.",
        "then": "{중복 이메일 오류 메시지}가 표시된다.",
    }
    assert "given" in _format_unresolved_claims(claims)


def test_parse_claims_and_values():
    agent = TVFromCodebaseAgent(trace_id="tv-parse")
    parsed = agent._parse(
        '{"claims":{"then":"Email already registered가 표시된다."},"values":[{"field":"email","value":"duplicate@example.com","type":"string","purpose":"코드 기반","source":"code"}],"confidence":0.93}'
    )
    assert parsed["claims"]["then"] == "Email already registered가 표시된다."
    assert parsed["values"][0]["source"] == "code"
    assert parsed["confidence"] == 0.93


def test_parse_preserves_channel_prefixed_then_claim():
    agent = TVFromCodebaseAgent(trace_id="tv-parse")
    parsed = agent._parse(
        '{"claims":{"then":"UI) 이메일, 이름, 가입 일자가 표시된다.\\nDB) users에 사용자 정보가 저장된다."},"values":[],"confidence":0.91}'
    )
    assert parsed["claims"]["then"] == (
        "UI) 이메일, 이름, 가입 일자가 표시된다.\n"
        "DB) users에 사용자 정보가 저장된다."
    )
    assert parsed["confidence"] == 0.91


def test_ui_input_values_only_filters_response_and_db_fields_and_keeps_ui_sensitive():
    schemas = {
        "request_schemas": {
            "SignupRequest": {
                "fields": [
                    {"name": "email", "type": "EmailStr", "required": True, "validators": [{"kind": "email_format"}]},
                    {"name": "name", "type": "str", "required": True, "validators": []},
                    {"name": "password", "type": "str", "required": True, "validators": [{"kind": "min_length", "value": 8}], "sensitive": True},
                    {"name": "birth_date", "type": "date", "required": True, "validators": []},
                    {"name": "guardian_consent", "type": "bool", "required": False, "validators": []},
                ]
            }
        },
        "db_models": {
            "Customer": {
                "columns": [
                    {"name": "password_hash", "type": "str", "sensitive": True},
                    {"name": "token", "type": "str", "sensitive": True},
                ]
            }
        },
    }
    selectors = {
        "by_route": {
            "/signup": {
                "inputs": [
                    {"testid": "name", "v_model": "form.name", "html_type": "text"},
                    {"testid": "email", "v_model": "form.email", "html_type": "email"},
                    {"testid": "password", "v_model": "form.password", "html_type": "password"},
                    {"testid": "birth_date", "v_model": "form.birth_date", "html_type": "date"},
                    {"testid": "guardian-consent", "v_model": "form.guardian_consent", "html_type": "checkbox"},
                ]
            }
        }
    }
    llm_values = [
        {"field": "email", "value": "newuser_2026@test.com", "type": "string", "purpose": "이메일", "source": "llm"},
        {"field": "name", "value": "홍길동", "type": "string", "purpose": "이름", "source": "llm"},
        {"field": "password_hash", "value": "process.env.TEST_PASSWORD_HASH", "type": "string", "purpose": "해시", "source": "placeholder"},
        {"field": "token", "value": "process.env.TEST_TOKEN", "type": "string", "purpose": "토큰", "source": "placeholder"},
    ]
    sensitive_entries = build_sensitive_value_entries({"password"})

    values = _ui_input_values_only(
        llm_values,
        existing_values=[],
        schemas=schemas,
        selectors=selectors,
        sensitive_entries=sensitive_entries,
    )

    names = [v["field"] for v in values]
    assert names == ["email", "name", "password", "birth_date", "guardian_consent"]
    assert "password_hash" not in names
    assert "token" not in names
    assert next(v for v in values if v["field"] == "password")["value"] == "process.env.TEST_PASSWORD"
    assert next(v for v in values if v["field"] == "birth_date")["value"] == "2000-01-01"
    assert next(v for v in values if v["field"] == "guardian_consent")["value"] is True
