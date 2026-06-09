"""TVValidator — TV (test value) 검증 helper, PoC 7 (데이터 layer).

 utility (호출자 = 유빈 agent). TC/TV 생성 후 LLM 출력을 ground truth 와
대조해 valid/invalid 판정. 호출자가 결과 보고 재시도 결정.

검증 종류 (도메인 무관 일반화):
1. schema 일치     — tv_field.name 이 BackendSchemasIndex.fields 에 있나? 타입 match?
2. format 검증     — SchemaValidator (email/regex/min_length/...) 적용
3. DB 존재성       — scenario_intent.expects_existing_in_db 와 실제 snapshot 매칭

ValidationResult 의 reasons 는 사람이 읽기 쉬운 한국어 — agent 가 LLM 에 그대로
피드백 가능 (재시도 시 컨텍스트).

Author: 주환 (kimjuhwan).
Created: 2026-06-09
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

CheckKind = Literal["schema", "type", "format", "db_existence", "db_absence"]


@dataclass
class ValidationCheck:
    """한 검증 단위."""

    kind: CheckKind
    passed: bool
    detail: str
    # 추가 디버깅 정보 (선택)
    expected: Any = None
    actual: Any = None


@dataclass
class ValidationResult:
    """TVValidator.validate 의 반환."""

    valid: bool
    checks: list[ValidationCheck] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)  # valid=False 일 때만 채워짐

    @property
    def failed_checks(self) -> list[ValidationCheck]:
        return [c for c in self.checks if not c.passed]


# ────────────────────────────────────────────────────────────────────────
# format validators (도메인 무관, RFC/표준 기반)
# ────────────────────────────────────────────────────────────────────────

# RFC 5322 의 단순화 (실용적으로 충분)
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# ISO 8601 date (YYYY-MM-DD) — 단순화
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# UUID (RFC 4122)
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _check_format(kind: str, value: Any, *, params: Any = None) -> tuple[bool, str]:
    """format 검증 — (passed, detail)."""
    if value is None:
        return False, f"value 가 None — '{kind}' 검증 불가"
    str_v = str(value)
    if kind == "email_format":
        ok = bool(_EMAIL_RE.match(str_v))
        return ok, f"email 형식 {'OK' if ok else 'FAIL'}: {str_v!r}"
    if kind == "iso_format" or kind == "iso_date":
        ok = bool(_ISO_DATE_RE.match(str_v))
        return ok, f"ISO date 형식 {'OK' if ok else 'FAIL'}: {str_v!r}"
    if kind == "uuid_format":
        ok = bool(_UUID_RE.match(str_v))
        return ok, f"UUID 형식 {'OK' if ok else 'FAIL'}: {str_v!r}"
    if kind == "min_length":
        target = int(params) if params is not None else 0
        ok = len(str_v) >= target
        return ok, f"min_length({target}) {'OK' if ok else 'FAIL'} (actual={len(str_v)})"
    if kind == "max_length":
        target = int(params) if params is not None else 0
        ok = len(str_v) <= target
        return ok, f"max_length({target}) {'OK' if ok else 'FAIL'} (actual={len(str_v)})"
    if kind == "regex":
        pattern = str(params)
        try:
            ok = bool(re.match(pattern, str_v))
        except re.error as e:
            return False, f"regex 패턴 오류 ({pattern}): {e}"
        return ok, f"regex({pattern}) {'OK' if ok else 'FAIL'}"
    if kind == "required":
        ok = bool(str_v.strip())
        return ok, f"required {'OK' if ok else 'FAIL (빈 값)'}"
    # 알 수 없는 validator — pass 처리 (관대) + 로그
    return True, f"unknown validator kind '{kind}' — skipped"


# ────────────────────────────────────────────────────────────────────────
# 타입 매핑 (Pydantic/SQL 타입 → Python 타입 비교용 단순화)
# ────────────────────────────────────────────────────────────────────────

def _type_matches(expected: str, value: Any) -> bool:
    """schema field.type 과 value 의 Python 타입 매칭. 단순화 (PoC)."""
    if value is None:
        return False
    et = expected.lower()
    if et in ("str", "string", "text", "varchar", "emailstr", "email"):
        return isinstance(value, str)
    if et in ("int", "integer", "bigint", "smallint"):
        return isinstance(value, int) and not isinstance(value, bool)
    if et in ("float", "double", "decimal", "numeric"):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if et in ("bool", "boolean"):
        return isinstance(value, bool)
    if et in ("date", "datetime", "timestamp"):
        # date 는 str (ISO format) 으로 표현되는 경우 OK
        return isinstance(value, str)
    if et in ("uuid",):
        return isinstance(value, str) and bool(_UUID_RE.match(value))
    if et in ("dict", "json", "jsonb", "object"):
        return isinstance(value, dict)
    if et in ("list", "array"):
        return isinstance(value, list)
    # 알 수 없는 타입 — 관대하게 pass (PoC)
    return True


# ────────────────────────────────────────────────────────────────────────
# TVValidator — public class
# ────────────────────────────────────────────────────────────────────────

class TVValidator:
    """TV (test value) 검증 helper.

    호출자 (유빈 agent) 가 TC + TV + 메타데이터/DB 를 모아 본 instance 의
    validate() 호출. 결과의 valid=False 시 호출자가 LLM 재시도 결정.

    Stateless — instance 재사용 가능 (per-process singleton OK).
    """

    def validate(
        self,
        tv_field: dict[str, Any],
        scenario_intent: dict[str, Any] | None,
        db_snapshot: list[dict[str, Any]] | None,
        schemas: dict[str, Any] | None,
        schema_name: str | None = None,
    ) -> ValidationResult:
        """tv_field 를 종합 검증.

        Args:
            tv_field: {"name": "email", "value": "x@y.com", "source": "llm"} 같은 dict.
                     name + value 필수. 추가 필드는 무시.
            scenario_intent: TC 의 의도 정보. 다음 key 들 활용:
              - expects_existing_in_db: bool — True 면 db_snapshot 에 같은 value 가 있어야
              - expects_absent_in_db: bool — True 면 db_snapshot 에 같은 value 가 없어야
              둘 다 None/False 면 DB 검증 skip.
            db_snapshot: 해당 table 의 row dump (list of dict). None 이면 DB 검증 skip.
            schemas: BackendSchemasIndex.model_dump() 의 dict (request_schemas / db_models 등).
                    None 이면 schema 검증 skip.
            schema_name: schemas 안의 어떤 schema 를 적용할지 (예: "SignupRequest").
                        None 이면 schema 검증 skip.

        Returns:
            ValidationResult — checks + reasons.
        """
        checks: list[ValidationCheck] = []

        name = tv_field.get("name")
        value = tv_field.get("value")
        if not name:
            return ValidationResult(
                valid=False,
                checks=[ValidationCheck("schema", False, "tv_field.name 누락")],
                reasons=["tv_field.name 이 비어 있음"],
            )

        # ── 1. schema 일치 + 타입 ───────────────────────────────────────
        if schemas and schema_name:
            field_spec = self._find_field(schemas, schema_name, name)
            if field_spec is None:
                checks.append(ValidationCheck(
                    "schema", False,
                    f"schema '{schema_name}' 에 field '{name}' 없음",
                ))
            else:
                checks.append(ValidationCheck(
                    "schema", True,
                    f"schema '{schema_name}' 에 field '{name}' 존재",
                ))
                # 타입 매칭
                expected_type = field_spec.get("type", "")
                if expected_type:
                    type_ok = _type_matches(expected_type, value)
                    checks.append(ValidationCheck(
                        "type", type_ok,
                        f"type '{expected_type}' {'OK' if type_ok else 'MISMATCH'}",
                        expected=expected_type,
                        actual=type(value).__name__,
                    ))
                # format validators
                for v in field_spec.get("validators", []) or []:
                    v_kind = v.get("kind") if isinstance(v, dict) else None
                    v_params = v.get("value") if isinstance(v, dict) else None
                    if not v_kind:
                        continue
                    ok, detail = _check_format(v_kind, value, params=v_params)
                    checks.append(ValidationCheck("format", ok, detail))

        # ── 2. DB 존재성 / 부재성 ─────────────────────────────────────────
        if db_snapshot is not None and scenario_intent:
            expects_existing = bool(scenario_intent.get("expects_existing_in_db"))
            expects_absent = bool(scenario_intent.get("expects_absent_in_db"))
            if expects_existing or expects_absent:
                # snapshot 에서 같은 (name, value) row 찾기
                # snapshot row 가 {col_name: value, ...} 형태라 가정
                matches = [
                    row for row in db_snapshot
                    if row.get(name) == value
                ]
                exists = len(matches) > 0
                if expects_existing:
                    checks.append(ValidationCheck(
                        "db_existence", exists,
                        f"DB 에 {name}={value!r} {'존재' if exists else '부재'} "
                        f"(scenario_intent: 존재 기대)",
                        expected="exists",
                        actual=f"{len(matches)} matches",
                    ))
                if expects_absent:
                    checks.append(ValidationCheck(
                        "db_absence", not exists,
                        f"DB 에 {name}={value!r} {'부재' if not exists else '존재'} "
                        f"(scenario_intent: 부재 기대)",
                        expected="absent",
                        actual=f"{len(matches)} matches",
                    ))

        # ── 결과 종합 ────────────────────────────────────────────────────
        failed = [c for c in checks if not c.passed]
        valid = len(failed) == 0
        reasons = [c.detail for c in failed] if not valid else []
        return ValidationResult(valid=valid, checks=checks, reasons=reasons)

    # ── 내부 헬퍼 ──────────────────────────────────────────────────────────

    @staticmethod
    def _find_field(
        schemas: dict[str, Any], schema_name: str, field_name: str,
    ) -> dict[str, Any] | None:
        """schemas 안의 schema (request_schemas/response_schemas/db_models) 에서
        field_name 의 SchemaField dict 반환. 미존재 시 None.
        """
        # request_schemas 우선
        req = schemas.get("request_schemas") or {}
        if schema_name in req:
            for f in req[schema_name].get("fields", []) or []:
                if f.get("name") == field_name:
                    return f
        # db_models 도 검색 (table_name 으로 매칭)
        dbm = schemas.get("db_models") or {}
        if schema_name in dbm:
            for col in dbm[schema_name].get("columns", []) or []:
                if col.get("name") == field_name:
                    return col
        return None
