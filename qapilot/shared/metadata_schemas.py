"""metadata_indices 4 영역의 Pydantic schema.

2026-06-09 토론 결정 2 — selectors/routes/schemas/test_patterns 4 영역 + 결정성 + confidence
+ AST 기본 + LLM 보강.

상세 명세: docs/scan-enhancement/metadata-schema-spec.md

본 모듈은 schema 정의만 제공 — 추출 (AST/LLM) 은 PoC 2+, S3 writer 는 PoC 3,
조회는 PoC 4 에서 별도 모듈로 구현.

본인 영역 (주환).
Created: 2026-06-09
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ────────────────────────────────────────────────────────────────────────
# 공통 — 모든 영역 record 의 공통 필드
# ────────────────────────────────────────────────────────────────────────

ExtractionMethod = Literal["ast", "llm", "hybrid"]


class ExtractedFrom(BaseModel):
    """원본 추적 정보 — B-4 결정성 + 재검증 가능."""

    model_config = ConfigDict(extra="forbid")

    file: str = Field(..., description="repo root 기준 상대 경로")
    line_start: int = Field(..., ge=1)
    line_end: int = Field(..., ge=1)
    commit_sha: str = Field(..., description="full SHA (40자) 또는 file content sha256")


class BaseRecord(BaseModel):
    """모든 메타데이터 record 의 공통 base."""

    model_config = ConfigDict(extra="forbid")

    extracted_from: ExtractedFrom
    confidence: float = Field(..., ge=0.0, le=1.0,
                              description="AST=1.0 / LLM 의미라벨=0.85 / LLM 추론=0.65")
    extraction_method: ExtractionMethod


# ────────────────────────────────────────────────────────────────────────
# 1. frontend.selectors
# ────────────────────────────────────────────────────────────────────────

class DisabledWhen(BaseModel):
    """button 의 disabled 조건 (예: `loading || (isMinor && !consent)`)."""

    model_config = ConfigDict(extra="forbid")

    expr: str = Field(..., description="raw JS/Vue 표현식 (AST 추출)")
    semantic: str | None = Field(None, description="자연어 해석 (LLM 추출)")
    confidence_for_semantic: float | None = Field(None, ge=0.0, le=1.0)
    extraction_method_for_semantic: ExtractionMethod | None = None


class InputElement(BaseRecord):
    """input / textarea / select 등 입력 element."""

    testid: str
    html_type: str | None = None      # email, password, text, date, ...
    required: bool = False
    v_model: str | None = None        # form.email
    label: str | None = None
    placeholder: str | None = None
    validators: list[str] = Field(default_factory=list)  # ["email", "required", "min_length(8)"]


class ButtonElement(BaseRecord):
    """button element."""

    testid: str
    html_type: str | None = None      # button, submit, reset
    form_role: str | None = None      # submit, cancel
    disabled_when: DisabledWhen | None = None
    label: str | None = None


class OutputElement(BaseRecord):
    """결과 표시 element (toast, alert, message 등). LLM 의미 라벨링 필수."""

    testid: str
    html_tag: str | None = None       # div, span, p
    v_if: str | None = None           # 표시 조건 (AST)
    # LLM 의미 라벨
    semantic_kind: Literal["success_toast", "error_toast", "info_toast",
                            "validation_message", "loading_indicator",
                            "modal", "unknown"] | None = None
    semantic_purpose: str | None = None


class DynamicElement(BaseRecord):
    """동적 표시 element (v-if/v-show 로 조건부)."""

    testid: str
    html_tag: str | None = None
    html_type: str | None = None
    v_if: str | None = None
    semantic_purpose: str | None = None


class RouteSelectors(BaseModel):
    """한 route 의 element 들."""

    model_config = ConfigDict(extra="forbid")

    inputs: list[InputElement] = Field(default_factory=list)
    buttons: list[ButtonElement] = Field(default_factory=list)
    outputs: list[OutputElement] = Field(default_factory=list)
    dynamic: list[DynamicElement] = Field(default_factory=list)


class FrontendSelectorsIndex(BaseModel):
    """frontend.selectors — sub_kind 단위 한 record."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["frontend"] = "frontend"
    sub_kind: Literal["selectors"] = "selectors"
    service_id: str
    commit_sha: str
    by_route: dict[str, RouteSelectors] = Field(
        default_factory=dict,
        description="route path → element 카탈로그",
    )


# ────────────────────────────────────────────────────────────────────────
# 2. frontend.routes
# ────────────────────────────────────────────────────────────────────────

class RouteParam(BaseModel):
    """route 의 path 또는 query param."""

    model_config = ConfigDict(extra="forbid")

    name: str
    type: str | None = None           # string, int 등
    optional: bool = False


class RouteGuard(BaseModel):
    """vue-router beforeEnter / react-router loader 등 guard."""

    model_config = ConfigDict(extra="forbid")

    name: str                          # requireAuth, requireRole 등
    kind: str | None = None            # beforeEnter, loader 등


class RouteRecord(BaseRecord):
    """단일 route."""

    path: str
    component_file: str | None = None
    component_name: str | None = None
    guards: list[RouteGuard] = Field(default_factory=list)
    redirects_when_authed: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    params: list[RouteParam] = Field(default_factory=list)
    query_params: list[RouteParam] = Field(default_factory=list)


class FrontendRoutesIndex(BaseModel):
    """frontend.routes — sub_kind 단위 한 record."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["frontend"] = "frontend"
    sub_kind: Literal["routes"] = "routes"
    service_id: str
    commit_sha: str
    routes: list[RouteRecord] = Field(default_factory=list)
    default_redirects: dict[str, str] = Field(default_factory=dict)


# ────────────────────────────────────────────────────────────────────────
# 3. backend.schemas
# ────────────────────────────────────────────────────────────────────────

class SchemaValidator(BaseModel):
    """field 의 validator (min_length, regex, email_format 등)."""

    model_config = ConfigDict(extra="forbid")

    kind: str                          # min_length, max_length, regex, email_format, iso_format, ...
    value: Any = None                  # 8, "^...$", None (kind 만으로 충분한 경우)


class SchemaField(BaseRecord):
    """request/response schema 또는 db_model 의 field."""

    name: str
    type: str                          # str, int, EmailStr, datetime, ...
    required: bool = True
    validators: list[SchemaValidator] = Field(default_factory=list)
    examples: list[Any] = Field(default_factory=list)
    sensitive: bool = False            # password 등 LLM 출력 시 mask
    nullable: bool = False             # db_model 용
    primary_key: bool = False          # db_model 용
    auto_increment: bool = False       # db_model 용
    unique: bool = False               # db_model 용
    default: Any = None                # db_model 용


class RequestSchema(BaseModel):
    """API request body schema."""

    model_config = ConfigDict(extra="forbid")

    fields: list[SchemaField]


class StatusCode(BaseModel):
    """API response 의 status code + body."""

    model_config = ConfigDict(extra="forbid")

    code: int
    description: str
    body: dict[str, Any] | None = None
    extracted_from_handler: str | None = None  # backend/app/routers/auth.py:42


class ResponseSchema(BaseModel):
    """API response — 가능한 status_codes 별."""

    model_config = ConfigDict(extra="forbid")

    status_codes: list[StatusCode]


class DbModelColumn(SchemaField):
    """db_model 의 column = SchemaField 그대로 (nullable/primary_key/... 포함)."""

    pass


class DbModel(BaseRecord):
    """SQLAlchemy / Prisma / Mongoose model."""

    table_name: str
    columns: list[DbModelColumn] = Field(default_factory=list)


class BackendSchemasIndex(BaseModel):
    """backend.schemas — sub_kind 단위 한 record."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["backend"] = "backend"
    sub_kind: Literal["schemas"] = "schemas"
    service_id: str
    commit_sha: str
    request_schemas: dict[str, RequestSchema] = Field(default_factory=dict)
    response_schemas: dict[str, ResponseSchema] = Field(default_factory=dict)
    db_models: dict[str, DbModel] = Field(default_factory=dict)


# ────────────────────────────────────────────────────────────────────────
# 4. sut_tests.patterns
# ────────────────────────────────────────────────────────────────────────

PatternKind = Literal[
    "auth-setup", "db-seed", "wait-strategy", "page-object",
    "assertion", "cleanup", "fixture", "mock", "unknown",
]

TestFramework = Literal[
    "playwright", "cypress", "pytest", "jest", "vitest",
    "mocha", "junit", "rspec", "unknown",
]


class TestPatternRecord(BaseRecord):
    """SUT 의 기존 테스트에서 추출한 1개 패턴."""

    pattern_kind: PatternKind
    framework: TestFramework
    file: str                          # repo root 기준
    line_start: int                    # extracted_from 과 중복이지만 활용 편의
    line_end: int
    snippet: str                       # 원본 그대로 (truncation 없이)
    purpose: str                       # 자연어 설명 (LLM)
    uses_env_vars: list[str] = Field(default_factory=list)
    uses_data_testid: list[str] = Field(default_factory=list)


class SutTestsPatternsIndex(BaseModel):
    """sut_tests.patterns — sub_kind 단위 한 record."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["sut_tests"] = "sut_tests"
    sub_kind: Literal["patterns"] = "patterns"
    service_id: str
    commit_sha: str
    patterns: list[TestPatternRecord] = Field(default_factory=list)


# ────────────────────────────────────────────────────────────────────────
# Union — writer/reader 가 사용할 type
# ────────────────────────────────────────────────────────────────────────

MetadataIndex = (
    FrontendSelectorsIndex
    | FrontendRoutesIndex
    | BackendSchemasIndex
    | SutTestsPatternsIndex
)


KIND_SUB_KIND_TO_MODEL: dict[tuple[str, str], type[BaseModel]] = {
    ("frontend", "selectors"): FrontendSelectorsIndex,
    ("frontend", "routes"): FrontendRoutesIndex,
    ("backend", "schemas"): BackendSchemasIndex,
    ("sut_tests", "patterns"): SutTestsPatternsIndex,
}


def get_model_for(kind: str, sub_kind: str) -> type[BaseModel]:
    """kind/sub_kind → Pydantic model class.

    PoC 3 의 writer / PoC 4 의 reader 가 활용. 미등록 조합 시 KeyError.
    """
    return KIND_SUB_KIND_TO_MODEL[(kind, sub_kind)]
