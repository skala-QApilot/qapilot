"""TVValidator 단위 테스트 — PoC 7 (데이터 layer)."""

from __future__ import annotations

import pytest

from qapilot.shared.tv_validator import (
    TVValidator,
    ValidationCheck,
    _check_format,
    _type_matches,
)


# ────────────────────────────────────────────────────────────────────────
# format validators
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    ("a@b.com", True),
    ("user.name+tag@example.co.kr", True),
    ("not-an-email", False),
    ("@nodomain.com", False),
    ("missing-at.com", False),
    ("", False),
    (None, False),
])
def test_email_format(value, expected):
    ok, _ = _check_format("email_format", value)
    assert ok is expected


@pytest.mark.parametrize("value,expected", [
    ("2026-06-09", True),
    ("2026-13-01", True),  # 단순 regex (실제 날짜 유효성은 PoC 범위 외)
    ("2026/06/09", False),
    ("20260609", False),
])
def test_iso_date_format(value, expected):
    ok, _ = _check_format("iso_format", value)
    assert ok is expected


@pytest.mark.parametrize("value,expected", [
    ("550e8400-e29b-41d4-a716-446655440000", True),
    ("not-a-uuid", False),
    ("550e8400-e29b-41d4-a716", False),  # 짧음
])
def test_uuid_format(value, expected):
    ok, _ = _check_format("uuid_format", value)
    assert ok is expected


def test_min_length():
    ok, _ = _check_format("min_length", "abcdefgh", params=8)
    assert ok is True
    ok, _ = _check_format("min_length", "abc", params=8)
    assert ok is False


def test_max_length():
    ok, _ = _check_format("max_length", "abc", params=5)
    assert ok is True
    ok, _ = _check_format("max_length", "abcdef", params=5)
    assert ok is False


def test_regex_pattern():
    ok, _ = _check_format("regex", "ABC123", params=r"^[A-Z]+\d+$")
    assert ok is True
    ok, _ = _check_format("regex", "abc123", params=r"^[A-Z]+\d+$")
    assert ok is False


def test_regex_invalid_pattern_returns_false():
    ok, detail = _check_format("regex", "x", params="[invalid")
    assert ok is False
    assert "regex 패턴 오류" in detail


def test_required():
    ok, _ = _check_format("required", "x")
    assert ok is True
    ok, _ = _check_format("required", "   ")
    assert ok is False


def test_unknown_validator_passes():
    """미정의 validator kind 는 관대하게 pass (PoC)."""
    ok, detail = _check_format("custom_xyz", "value")
    assert ok is True
    assert "unknown validator" in detail


# ────────────────────────────────────────────────────────────────────────
# 타입 매칭
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("expected,value,ok", [
    ("str", "hi", True),
    ("EmailStr", "a@b.com", True),
    ("int", 42, True),
    ("int", True, False),       # bool 은 int 아님 (의도적)
    ("int", "42", False),       # str "42" 는 int 아님
    ("bool", True, True),
    ("float", 3.14, True),
    ("float", 3, True),         # int 도 float 호환
    ("date", "2026-01-01", True),
    ("uuid", "550e8400-e29b-41d4-a716-446655440000", True),
    ("uuid", "not-uuid", False),
    ("dict", {"k": "v"}, True),
    ("list", [1, 2], True),
    ("CustomType", "anything", True),  # 알 수 없는 타입 = 관대 pass
])
def test_type_matches(expected, value, ok):
    assert _type_matches(expected, value) is ok


def test_type_matches_none_value_fails():
    assert _type_matches("str", None) is False


# ────────────────────────────────────────────────────────────────────────
# TVValidator.validate — 통합
# ────────────────────────────────────────────────────────────────────────

@pytest.fixture
def validator():
    return TVValidator()


@pytest.fixture
def signup_schemas():
    """예: BackendSchemasIndex 의 일부 (SignupRequest)."""
    return {
        "request_schemas": {
            "SignupRequest": {
                "fields": [
                    {"name": "email", "type": "EmailStr", "required": True,
                     "validators": [{"kind": "email_format"}]},
                    {"name": "password", "type": "str", "required": True,
                     "validators": [{"kind": "min_length", "value": 8}]},
                    {"name": "age", "type": "int", "required": False, "validators": []},
                ],
            },
        },
        "db_models": {
            "customers": {
                "table_name": "customers",
                "columns": [
                    {"name": "id", "type": "uuid", "primary_key": True},
                    {"name": "email", "type": "EmailStr",
                     "validators": [{"kind": "email_format"}]},
                ],
            },
        },
    }


def test_no_schema_no_db_returns_valid(validator):
    """schemas, db_snapshot 모두 None → valid (검증 건너뜀)."""
    result = validator.validate(
        tv_field={"name": "email", "value": "x@y.com"},
        scenario_intent=None,
        db_snapshot=None,
        schemas=None,
    )
    assert result.valid is True
    assert result.checks == []


def test_missing_name_returns_invalid(validator):
    result = validator.validate(
        tv_field={"value": "x"},
        scenario_intent=None, db_snapshot=None, schemas=None,
    )
    assert result.valid is False
    assert "name" in result.reasons[0]


def test_schema_field_match_passes(validator, signup_schemas):
    result = validator.validate(
        tv_field={"name": "email", "value": "test@example.com"},
        scenario_intent=None, db_snapshot=None,
        schemas=signup_schemas, schema_name="SignupRequest",
    )
    assert result.valid is True
    kinds = [c.kind for c in result.checks]
    assert "schema" in kinds
    assert "type" in kinds
    assert "format" in kinds


def test_schema_field_missing_fails(validator, signup_schemas):
    result = validator.validate(
        tv_field={"name": "phone", "value": "010"},
        scenario_intent=None, db_snapshot=None,
        schemas=signup_schemas, schema_name="SignupRequest",
    )
    assert result.valid is False
    assert any("field 'phone' 없음" in c.detail for c in result.checks)


def test_invalid_email_format_fails(validator, signup_schemas):
    result = validator.validate(
        tv_field={"name": "email", "value": "not-an-email"},
        scenario_intent=None, db_snapshot=None,
        schemas=signup_schemas, schema_name="SignupRequest",
    )
    assert result.valid is False
    assert any("email 형식 FAIL" in c.detail for c in result.failed_checks)


def test_invalid_email_format_passes_when_violation_expected(validator, signup_schemas):
    """edge_case + 400 같은 음성 테스트는 의도적 형식 위반을 실패로 보지 않는다."""
    result = validator.validate(
        tv_field={"name": "email", "value": "not-an-email"},
        scenario_intent={"expects_format_violation": True}, db_snapshot=None,
        schemas=signup_schemas, schema_name="SignupRequest",
    )
    assert result.valid is True
    assert any("의도된 음성 테스트" in c.detail for c in result.checks)


def test_type_mismatch_fails(validator, signup_schemas):
    """age=int 인데 str 주입."""
    result = validator.validate(
        tv_field={"name": "age", "value": "twenty"},
        scenario_intent=None, db_snapshot=None,
        schemas=signup_schemas, schema_name="SignupRequest",
    )
    assert result.valid is False
    assert any("MISMATCH" in c.detail for c in result.failed_checks)


def test_min_length_failure(validator, signup_schemas):
    result = validator.validate(
        tv_field={"name": "password", "value": "short"},
        scenario_intent=None, db_snapshot=None,
        schemas=signup_schemas, schema_name="SignupRequest",
    )
    assert result.valid is False
    assert any("min_length" in c.detail for c in result.failed_checks)


def test_db_existence_passes(validator):
    """scenario_intent.expects_existing_in_db + DB 에 존재 → valid."""
    db = [{"email": "dup@test.com"}, {"email": "other@test.com"}]
    result = validator.validate(
        tv_field={"name": "email", "value": "dup@test.com"},
        scenario_intent={"expects_existing_in_db": True},
        db_snapshot=db,
        schemas=None,
    )
    assert result.valid is True
    assert any(c.kind == "db_existence" and c.passed for c in result.checks)


def test_db_existence_fails_when_absent(validator):
    """expects_existing_in_db=True 인데 DB 에 없음 → invalid."""
    db = [{"email": "other@test.com"}]
    result = validator.validate(
        tv_field={"name": "email", "value": "missing@test.com"},
        scenario_intent={"expects_existing_in_db": True},
        db_snapshot=db, schemas=None,
    )
    assert result.valid is False
    assert any("부재" in c.detail for c in result.failed_checks)


def test_db_absence_passes(validator):
    """expects_absent_in_db=True + DB 에 없음 → valid (신규 가입 시나리오)."""
    db = [{"email": "existing@test.com"}]
    result = validator.validate(
        tv_field={"name": "email", "value": "new@test.com"},
        scenario_intent={"expects_absent_in_db": True},
        db_snapshot=db, schemas=None,
    )
    assert result.valid is True


def test_db_absence_fails_when_existing(validator):
    """expects_absent_in_db=True 인데 DB 에 이미 있음 → invalid."""
    db = [{"email": "exists@test.com"}]
    result = validator.validate(
        tv_field={"name": "email", "value": "exists@test.com"},
        scenario_intent={"expects_absent_in_db": True},
        db_snapshot=db, schemas=None,
    )
    assert result.valid is False
    assert any("존재" in c.detail for c in result.failed_checks)


def test_db_intent_not_specified_skips_db_check(validator):
    """scenario_intent 에 expects_* 가 없으면 DB 검증 skip."""
    db = [{"email": "x"}]
    result = validator.validate(
        tv_field={"name": "email", "value": "x"},
        scenario_intent={"some_other_key": "foo"},
        db_snapshot=db, schemas=None,
    )
    assert result.valid is True
    assert not any(c.kind in ("db_existence", "db_absence") for c in result.checks)


def test_full_pipeline_signup_intent(validator, signup_schemas):
    """통합 — 신규 가입 (schema OK + format OK + DB 부재 기대 OK)."""
    db = [{"email": "existing@test.com"}]
    result = validator.validate(
        tv_field={"name": "email", "value": "newuser@test.com"},
        scenario_intent={"expects_absent_in_db": True},
        db_snapshot=db,
        schemas=signup_schemas, schema_name="SignupRequest",
    )
    assert result.valid is True
    kinds = [c.kind for c in result.checks]
    assert "schema" in kinds and "type" in kinds and "format" in kinds and "db_absence" in kinds


def test_db_model_columns_lookup(validator, signup_schemas):
    """schemas.db_models 의 columns 도 schema field 로 검증."""
    result = validator.validate(
        tv_field={"name": "id", "value": "550e8400-e29b-41d4-a716-446655440000"},
        scenario_intent=None, db_snapshot=None,
        schemas=signup_schemas, schema_name="customers",
    )
    assert result.valid is True
    assert any(c.kind == "schema" and c.passed for c in result.checks)


def test_validation_result_has_human_readable_reasons(validator, signup_schemas):
    """failed reasons 가 한국어/사람이 읽기 쉬운 문자열."""
    result = validator.validate(
        tv_field={"name": "email", "value": "bad"},
        scenario_intent=None, db_snapshot=None,
        schemas=signup_schemas, schema_name="SignupRequest",
    )
    assert result.valid is False
    assert result.reasons  # 비어있지 않음
    assert all(isinstance(r, str) and r for r in result.reasons)
