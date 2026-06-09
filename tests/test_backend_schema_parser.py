"""backend_schema_parser 단위 테스트 — PoC 6.A (본인 영역)."""

from __future__ import annotations

from pathlib import Path

import pytest

from qapilot.scan.extractors.backend_schema_parser import (
    _is_response_class,
    _parse_pydantic_field_args,
    _sqla_type_from_call,
    _strip_string_literal,
    extract_backend_schemas_from_file,
)

FIXTURE = Path(__file__).parent / "fixtures" / "python" / "sample_schemas.py"


@pytest.fixture
def extracted():
    return extract_backend_schemas_from_file(
        FIXTURE, repo_root=FIXTURE.parent, commit_sha="a" * 40,
    )


# ────────────────────────────────────────────────────────────────────────
# helpers
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,expected", [
    ("CustomerOut", True),
    ("LoginResponse", True),
    ("ReplyResult", True),
    ("SignupRequest", False),
    ("OrderItem", False),
])
def test_is_response_class(name, expected):
    assert _is_response_class(name) is expected


def test_strip_string_literal_basic():
    assert _strip_string_literal('"abc"') == "abc"
    assert _strip_string_literal("'abc'") == "abc"


def test_strip_string_literal_raw_string():
    assert _strip_string_literal('r"^.+$"') == "^.+$"
    assert _strip_string_literal("R'^.+$'") == "^.+$"


def test_strip_string_literal_unwrapped():
    assert _strip_string_literal("123") == "123"


def test_parse_pydantic_field_args():
    out = _parse_pydantic_field_args("Field(min_length=3, max_length=255, pattern=r\"^.+$\")")
    assert out == {"min_length": "3", "max_length": "255", "pattern": "^.+$"}


def test_parse_pydantic_field_args_default():
    out = _parse_pydantic_field_args("Field(default=1, ge=1, le=10)")
    assert out["default"] == "1"
    assert out["ge"] == "1"


def test_sqla_type_from_call():
    assert _sqla_type_from_call("mapped_column(Integer, primary_key=True)") == "int"
    assert _sqla_type_from_call("Column(String(255), unique=True)") == "str"
    assert _sqla_type_from_call("mapped_column(Boolean)") == "bool"


# ────────────────────────────────────────────────────────────────────────
# Pydantic 추출
# ────────────────────────────────────────────────────────────────────────

def test_request_schemas_count(extracted):
    req, _, _ = extracted
    assert set(req.keys()) == {"SignupRequest", "LoginRequest"}


def test_response_schemas_for_out_suffix(extracted):
    _, resp, _ = extracted
    assert "CustomerOut" in resp  # 'Out' 접미사 → response


def test_signup_field_with_validators(extracted):
    req, _, _ = extracted
    email = next(f for f in req["SignupRequest"].fields if f.name == "email")
    assert email.required is True
    assert email.type == "str"
    kinds = {v.kind for v in email.validators}
    assert {"min_length", "max_length", "regex"} <= kinds


def test_signup_field_required_without_field_call(extracted):
    """`name: str` (Field 없음, default 없음) → required=True."""
    req, _, _ = extracted
    name = next(f for f in req["SignupRequest"].fields if f.name == "name")
    assert name.required is True


def test_signup_field_optional_with_none_default(extracted):
    """`consent: bool | None = None` → required=False, nullable=True."""
    req, _, _ = extracted
    consent = next(f for f in req["SignupRequest"].fields if f.name == "consent")
    assert consent.required is False
    assert consent.nullable is True


def test_signup_birth_date_typed_only_required(extracted):
    """`birth_date: date` (default value 없음) → required=True."""
    req, _, _ = extracted
    bd = next(f for f in req["SignupRequest"].fields if f.name == "birth_date")
    assert bd.required is True
    assert bd.type == "date"


def test_field_with_field_call_only_kwargs_is_required(extracted):
    """`password: str = Field(min_length=8)` → required=True (default keyword 없으므로)."""
    req, _, _ = extracted
    pw = next(f for f in req["SignupRequest"].fields if f.name == "password")
    assert pw.required is True
    assert pw.sensitive is True  # 휴리스틱


def test_model_config_excluded(extracted):
    """model_config 는 field 가 아님 — 제외."""
    _, resp, _ = extracted
    co = resp["CustomerOut"]
    # body 안에 fields 가 있고 model_config 는 빠져야
    # CustomerOut 의 fields 가 status_codes[0].body['fields'] 안에 있음
    body_fields = co.status_codes[0].body.get("fields", [])
    names = {f["name"] for f in body_fields}
    assert "model_config" not in names
    assert "_" not in names


# ────────────────────────────────────────────────────────────────────────
# SQLAlchemy 추출
# ────────────────────────────────────────────────────────────────────────

def test_db_models_count(extracted):
    _, _, dbm = extracted
    assert set(dbm.keys()) == {"User", "Session"}


def test_user_table_name(extracted):
    _, _, dbm = extracted
    assert dbm["User"].table_name == "users"


def test_user_columns(extracted):
    _, _, dbm = extracted
    cols = {c.name: c for c in dbm["User"].columns}
    assert "id" in cols and "email" in cols and "password_hash" in cols

    assert cols["id"].primary_key is True
    assert cols["id"].type == "int"

    assert cols["email"].unique is True
    assert cols["email"].nullable is False

    assert cols["password_hash"].sensitive is True


def test_session_foreign_key_column(extracted):
    """ForeignKey 컬럼도 type 추출 (인자 0 이 ForeignKey)."""
    _, _, dbm = extracted
    cols = {c.name: c for c in dbm["Session"].columns}
    assert "user_id" in cols
    # type 은 Mapped[int] 에서 추출 — int
    assert cols["user_id"].type == "int"
    # ForeignKey 면 nullable 명시 = False
    assert cols["user_id"].nullable is False


def test_extracted_from_relative_path(extracted):
    req, _, _ = extracted
    f = req["SignupRequest"].fields[0]
    assert f.extracted_from.file == "sample_schemas.py"
    assert f.extracted_from.commit_sha == "a" * 40


def test_confidence_one_for_ast(extracted):
    req, _, dbm = extracted
    for f in req["SignupRequest"].fields:
        assert f.confidence == 1.0
        assert f.extraction_method == "ast"
    for c in dbm["User"].columns:
        assert c.confidence == 1.0


# ────────────────────────────────────────────────────────────────────────
# 에러 핸들링
# ────────────────────────────────────────────────────────────────────────

def test_missing_file_returns_empty_tuple(tmp_path):
    req, resp, dbm = extract_backend_schemas_from_file(
        tmp_path / "no.py", repo_root=tmp_path, commit_sha="x" * 40,
    )
    assert req == {} and resp == {} and dbm == {}


def test_non_class_python_returns_empty(tmp_path):
    f = tmp_path / "main.py"
    f.write_text("print('hi')\nx = 1\n")
    req, resp, dbm = extract_backend_schemas_from_file(
        f, repo_root=tmp_path, commit_sha="x" * 40,
    )
    assert req == {} and resp == {} and dbm == {}
