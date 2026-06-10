"""sensitive_mask 단위 테스트 — Option α (LLM 제외 + placeholder)."""

from __future__ import annotations

import pytest

from qapilot.shared.sensitive_mask import (
    build_sensitive_value_entries,
    get_sensitive_field_names,
    is_sensitive_field,
    make_placeholder,
    merge_values_with_sensitive,
    strip_sensitive_from_db_snapshot,
    strip_sensitive_from_schemas,
)


# ────────────────────────────────────────────────────────────────────────
# is_sensitive_field
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,expected", [
    ("password", True),
    ("password_hash", True),
    ("api_key", True),
    ("apikey", True),
    ("token", True),
    ("secret", True),
    ("credit_card", True),
    ("email", False),
    ("name", False),
    ("user_id", False),
])
def test_is_sensitive_field_by_name(name, expected):
    assert is_sensitive_field(name) is expected


def test_is_sensitive_field_by_schema_spec_overrides_name():
    """schema 의 sensitive=True 가 키워드 fallback 보다 우선."""
    assert is_sensitive_field("foo", schema_field_spec={"sensitive": True}) is True
    # 키워드는 sensitive 인데 schema 가 명시적 False 인 경우 — 키워드 우선 (안전)
    assert is_sensitive_field("password") is True


# ────────────────────────────────────────────────────────────────────────
# get_sensitive_field_names
# ────────────────────────────────────────────────────────────────────────

def test_get_sensitive_field_names_from_request_schemas():
    schemas = {
        "request_schemas": {
            "SignupRequest": {"fields": [
                {"name": "email", "sensitive": False},
                {"name": "password", "sensitive": True},
                {"name": "name"},
            ]},
        },
        "db_models": {},
    }
    assert get_sensitive_field_names(schemas) == {"password"}


def test_get_sensitive_field_names_from_db_models():
    schemas = {
        "request_schemas": {},
        "db_models": {
            "Customer": {"columns": [
                {"name": "id"},
                {"name": "email"},
                {"name": "password_hash", "sensitive": True},
                {"name": "api_key"},  # 키워드 fallback
            ]},
        },
    }
    assert get_sensitive_field_names(schemas) == {"password_hash", "api_key"}


def test_get_sensitive_field_names_empty_input():
    assert get_sensitive_field_names(None) == set()
    assert get_sensitive_field_names({}) == set()


# ────────────────────────────────────────────────────────────────────────
# strip_sensitive_from_schemas
# ────────────────────────────────────────────────────────────────────────

def test_strip_sensitive_from_schemas_removes_sensitive_fields():
    schemas = {
        "request_schemas": {
            "SignupRequest": {"fields": [
                {"name": "email"},
                {"name": "password", "sensitive": True},
                {"name": "name"},
            ]},
        },
        "db_models": {
            "Customer": {"columns": [
                {"name": "email"},
                {"name": "password_hash"},
            ]},
        },
    }
    result = strip_sensitive_from_schemas(schemas)

    # request_schemas — password 제거
    req_fields = [f["name"] for f in result["request_schemas"]["SignupRequest"]["fields"]]
    assert "password" not in req_fields
    assert "email" in req_fields and "name" in req_fields

    # db_models — password_hash 제거
    db_cols = [c["name"] for c in result["db_models"]["Customer"]["columns"]]
    assert "password_hash" not in db_cols
    assert "email" in db_cols


def test_strip_sensitive_from_schemas_preserves_original():
    """원본은 변경 안 됨."""
    schemas = {
        "request_schemas": {
            "Req": {"fields": [{"name": "password"}, {"name": "email"}]},
        },
        "db_models": {},
    }
    original_fields = list(schemas["request_schemas"]["Req"]["fields"])
    strip_sensitive_from_schemas(schemas)
    assert schemas["request_schemas"]["Req"]["fields"] == original_fields


def test_strip_sensitive_empty_input():
    result = strip_sensitive_from_schemas(None)
    assert result == {"request_schemas": {}, "response_schemas": {}, "db_models": {}}


# ────────────────────────────────────────────────────────────────────────
# strip_sensitive_from_db_snapshot
# ────────────────────────────────────────────────────────────────────────

def test_strip_sensitive_from_db_snapshot():
    rows = [
        {"id": 1, "email": "a@b.com", "password_hash": "hashed1"},
        {"id": 2, "email": "c@d.com", "password_hash": "hashed2"},
    ]
    result = strip_sensitive_from_db_snapshot(rows, {"password_hash"})
    assert all("password_hash" not in r for r in result)
    assert all("email" in r for r in result)
    assert len(result) == 2


def test_strip_sensitive_from_db_snapshot_empty():
    assert strip_sensitive_from_db_snapshot([], {"password"}) == []
    assert strip_sensitive_from_db_snapshot(None, {"password"}) == []


def test_strip_sensitive_from_db_snapshot_skips_non_dict_rows():
    rows = [{"a": 1}, "not a dict", {"b": 2}, None]
    result = strip_sensitive_from_db_snapshot(rows, set())
    assert result == [{"a": 1}, {"b": 2}]


# ────────────────────────────────────────────────────────────────────────
# make_placeholder
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("field,expected", [
    ("password", "${TEST_PASSWORD}"),
    ("api_key", "${TEST_API_KEY}"),
    ("access-key", "${TEST_ACCESS_KEY}"),
    ("credit card", "${TEST_CREDIT_CARD}"),
])
def test_make_placeholder(field, expected):
    assert make_placeholder(field) == expected


# ────────────────────────────────────────────────────────────────────────
# build_sensitive_value_entries
# ────────────────────────────────────────────────────────────────────────

def test_build_sensitive_value_entries_basic():
    entries = build_sensitive_value_entries({"password", "api_key"})
    # sorted 으로 결정성
    assert entries[0]["field"] == "api_key"
    assert entries[1]["field"] == "password"

    for e in entries:
        assert e["sensitive"] is True
        assert e["source"] == "placeholder"
        assert e["value"].startswith("${TEST_")
        assert "환경변수" in e["purpose"]


def test_build_sensitive_value_entries_with_type_hints():
    entries = build_sensitive_value_entries(
        {"password"}, type_hint_map={"password": "str"},
    )
    assert entries[0]["type"] == "str"


def test_build_sensitive_value_entries_empty():
    assert build_sensitive_value_entries(set()) == []


# ────────────────────────────────────────────────────────────────────────
# merge_values_with_sensitive
# ────────────────────────────────────────────────────────────────────────

def test_merge_values_with_sensitive():
    llm_values = [
        {"field": "email", "value": "test@example.com", "type": "string"},
        {"field": "name", "value": "홍길동", "type": "string"},
    ]
    sensitive_entries = build_sensitive_value_entries({"password"})

    merged = merge_values_with_sensitive(llm_values, sensitive_entries)

    fields = [v["field"] for v in merged]
    assert "email" in fields
    assert "name" in fields
    assert "password" in fields
    # password 는 placeholder
    pw = next(v for v in merged if v["field"] == "password")
    assert pw["value"] == "${TEST_PASSWORD}"
    assert pw["sensitive"] is True


def test_merge_overrides_llm_response_for_sensitive():
    """LLM 응답에 sensitive 필드가 우연히 들어있으면 placeholder 가 덮어씀."""
    llm_values = [
        {"field": "email", "value": "x@y.com"},
        {"field": "password", "value": "***hacked***"},  # LLM 이 마스킹 형태로 응답
    ]
    sensitive_entries = build_sensitive_value_entries({"password"})

    merged = merge_values_with_sensitive(llm_values, sensitive_entries)
    pw = next(v for v in merged if v["field"] == "password")
    # LLM 의 "***hacked***" 가 아니라 placeholder
    assert pw["value"] == "${TEST_PASSWORD}"


def test_merge_with_empty_llm_values():
    sensitive_entries = build_sensitive_value_entries({"password"})
    merged = merge_values_with_sensitive([], sensitive_entries)
    assert len(merged) == 1
    assert merged[0]["field"] == "password"


def test_merge_with_no_sensitive_entries():
    llm_values = [{"field": "email", "value": "x"}]
    merged = merge_values_with_sensitive(llm_values, [])
    assert merged == llm_values
