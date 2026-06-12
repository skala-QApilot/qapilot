"""민감정보 처리 — Option α (sensitive 필드 LLM 컨텍스트 완전 제외 + placeholder 저장).

이전 PR #256 의 본질 재발 방지:
- CodeGenerator 가 password 같은 민감 값을 `process.env.E2E_USER_PASSWORD` 로 마스킹해서
  LLM 에 보내면, LLM 이 그 마스킹 형태를 응답에 그대로 박아넣어서 실행 시 빈 string.
- Fix: ActionMapping 의 원본 value fallback (#256).

본 모듈의 정책:
1. sensitive 필드는 LLM 컨텍스트에서 **완전 제외** — 마스킹된 형태조차 노출 안 함.
2. TC.values 의 sensitive 필드는 LLM 이 생성하지 않고 TV agent 가 placeholder 저장.
3. 액션매핑 시점에 PR #256 의 ActionMapping 원본 value fallback 으로 실제 값이 들어감.

`SchemaField.sensitive=True` 는 `backend_schema_parser` 가 자동으로 password* 컬럼에 표시.

본 모듈은 stateless helper.
"""

from __future__ import annotations

from typing import Any

# sensitive 필드 이름 휴리스틱 (schema 의 sensitive flag 가 없을 때 fallback)
_SENSITIVE_KEYWORDS = (
    "password", "passwd", "pwd", "secret", "token", "api_key", "apikey",
    "private_key", "access_key", "credit_card", "ssn", "card_number",
)

# placeholder 형식 — `_resolve_js_value` 의 `process.env.X` 패턴과 정합.
# generated_code 의 JS 안에 `await page.fill(process.env.TEST_X)` 형태로 박힘 →
# `_resolve_js_value` 가 환경변수 `TEST_X` 으로 자동 치환.
# 환경변수 미설정 시 빈 string → PR #256 의 ActionMapping 원본 value fallback.
_PLACEHOLDER_TEMPLATE = "process.env.TEST_{upper}"


def is_sensitive_field(field_name: str, *, schema_field_spec: dict | None = None) -> bool:
    """필드가 sensitive 한지 판정.

    우선순위:
    1. schema_field_spec.sensitive == True (`backend_schema_parser` 가 자동 마킹)
    2. 필드 이름에 sensitive 키워드 포함 (fallback)
    """
    if schema_field_spec is not None and schema_field_spec.get("sensitive") is True:
        return True
    name_lower = field_name.lower()
    return any(kw in name_lower for kw in _SENSITIVE_KEYWORDS)


def get_sensitive_field_names(schemas: dict[str, Any] | None) -> set[str]:
    """schemas 안의 모든 sensitive 필드 이름 set 반환.

    request_schemas + db_models 의 columns 모두 검사.
    sensitive=True 표시 + 키워드 fallback 둘 다.
    """
    if not schemas:
        return set()
    names: set[str] = set()
    # request schemas
    for spec in (schemas.get("request_schemas") or {}).values():
        for f in spec.get("fields") or []:
            fname = f.get("name", "")
            if fname and is_sensitive_field(fname, schema_field_spec=f):
                names.add(fname)
    # db_models columns
    for model in (schemas.get("db_models") or {}).values():
        for col in model.get("columns") or []:
            cname = col.get("name", "")
            if cname and is_sensitive_field(cname, schema_field_spec=col):
                names.add(cname)
    return names


def get_sensitive_request_field_names(schemas: dict[str, Any] | None) -> set[str]:
    """request_schema 입력 필드 중 sensitive 한 것만 반환 (placeholder 주입용).

    `get_sensitive_field_names` 와 달리 **db_models 컬럼은 보지 않는다.**
    이유: TC.values 는 "사용자가 폼에 입력하는 값" 인데, password_hash / token 같은
    DB 컬럼·응답 필드는 입력값이 아니다. 이를 placeholder 로 주입하면 폼에 없는 필드가
    모든 TC 에 박힌다 (실측: password_hash 96%, token 91% TC 오염).

    DB 스냅샷 마스킹용 sensitive set 은 여전히 `get_sensitive_field_names`(request+db) 를 쓴다.
    """
    if not schemas:
        return set()
    names: set[str] = set()
    for spec in (schemas.get("request_schemas") or {}).values():
        for f in spec.get("fields") or []:
            fname = f.get("name", "")
            if fname and is_sensitive_field(fname, schema_field_spec=f):
                names.add(fname)
    return names


def strip_sensitive_from_schemas(schemas: dict[str, Any] | None) -> dict[str, Any]:
    """schemas 의 sensitive 필드를 dict 에서 제거.

    LLM 호출 직전 컨텍스트에서 sensitive 필드 자체가 안 보이도록.
    원본은 변경 안 함 (deep copy 의 의도, 단 본 PoC 는 shallow + 필드별 새 list).

    Returns:
        sensitive 필드 제거된 schemas dict.
    """
    if not schemas:
        return {"request_schemas": {}, "response_schemas": {}, "db_models": {}}

    out: dict[str, Any] = {
        "request_schemas": {},
        "response_schemas": schemas.get("response_schemas") or {},
        "db_models": {},
    }

    for name, spec in (schemas.get("request_schemas") or {}).items():
        filtered_fields = [
            f for f in (spec.get("fields") or [])
            if not is_sensitive_field(f.get("name", ""), schema_field_spec=f)
        ]
        new_spec = dict(spec)
        new_spec["fields"] = filtered_fields
        out["request_schemas"][name] = new_spec

    for name, model in (schemas.get("db_models") or {}).items():
        filtered_cols = [
            c for c in (model.get("columns") or [])
            if not is_sensitive_field(c.get("name", ""), schema_field_spec=c)
        ]
        new_model = dict(model)
        new_model["columns"] = filtered_cols
        out["db_models"][name] = new_model

    return out


def strip_sensitive_from_db_snapshot(
    rows: list[dict[str, Any]] | None,
    sensitive_field_names: set[str],
) -> list[dict[str, Any]]:
    """DB snapshot row 들의 sensitive 필드 제거.

    LLM 에 DB 실제 값을 노출할 때 password_hash 등이 들어가지 않도록.
    """
    if not rows:
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        filtered_row = {
            k: v for k, v in row.items()
            if k not in sensitive_field_names
        }
        out.append(filtered_row)
    return out


def make_placeholder(field_name: str) -> str:
    """sensitive 필드의 placeholder 생성.

    형식: process.env.TEST_<UPPERCASE_FIELD>
    예: password → process.env.TEST_PASSWORD
        api_key → process.env.TEST_API_KEY

    실행 흐름:
    1. TC.values 의 sensitive 필드 = "process.env.TEST_PASSWORD"
    2. CodeGenerator 가 generated_code 의 JS 안에 그대로 박음
       (예: `await page.fill(process.env.TEST_PASSWORD)`)
    3. 실행 시점에 `_resolve_js_value` 의 정규식 `r"process\\.env\\.([A-Z0-9_]+)"`
       으로 매칭 → `os.getenv("TEST_PASSWORD")` 호출
    4. 환경변수 설정 시 그 값으로 치환, 미설정 시 빈 string —
       PR #256 의 ActionMapping 원본 value fallback 으로 처리.
    """
    upper = field_name.upper().replace("-", "_").replace(" ", "_")
    return _PLACEHOLDER_TEMPLATE.format(upper=upper)


def build_sensitive_value_entries(
    sensitive_field_names: set[str],
    *,
    type_hint_map: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """sensitive 필드를 TC.values 의 entry 형식으로 변환 (LLM 호출 우회).

    각 entry:
        {
            "field": "password",
            "value": "process.env.TEST_PASSWORD",
            "type": "string",
            "purpose": "민감 정보 — 액션매핑 시점에 환경변수에서 가져옴",
            "source": "placeholder",
            "sensitive": True,
            "evidence": "근거 없음",
        }

    Args:
        sensitive_field_names: 처리할 필드 이름 set.
        type_hint_map: 필드 → 타입 매핑 (schemas 에서 추출 가능).

    Returns:
        TC.values 에 그대로 머지할 수 있는 entry list.
    """
    type_hint_map = type_hint_map or {}
    out: list[dict[str, Any]] = []
    for fname in sorted(sensitive_field_names):
        out.append({
            "field": fname,
            "value": make_placeholder(fname),
            "type": type_hint_map.get(fname, "string"),
            "purpose": "민감 정보 — 액션매핑 시점에 환경변수에서 가져옴",
            "source": "placeholder",
            "sensitive": True,
            "evidence": "근거 없음",
        })
    return out


def merge_values_with_sensitive(
    llm_values: list[dict[str, Any]],
    sensitive_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """LLM 이 생성한 values + sensitive placeholder entries 머지.

    LLM 응답에 sensitive 필드가 우연히 들어있어도 placeholder 로 덮어쓴다
    (LLM 이 마스킹 형태나 추측한 값을 채워도 안전).

    중복 필드는 sensitive_entries 가 우선.
    """
    sensitive_names = {e["field"] for e in sensitive_entries}
    # LLM 응답에서 sensitive 필드 제거
    filtered_llm = [v for v in (llm_values or []) if v.get("field") not in sensitive_names]
    return filtered_llm + sensitive_entries
