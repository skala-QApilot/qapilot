"""공통 스키마 정의.

PipelineState (LangGraph용 TypedDict) + Agent/Tool I/O (Pydantic 검증용)를 정의한다.

Author: 공통
Created: 2026-05-07
"""

from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field


# ═══════════════════════════════════════════════════
# _execute() 반환 타입
# ═══════════════════════════════════════════════════


@dataclass
class ExecuteResult:
    """BaseAgent._execute()의 반환 타입. 팀원은 이것만 반환하면 된다."""

    result: dict[str, Any]
    confidence: float  # 0.0~1.0


# ═══════════════════════════════════════════════════
# Pydantic 스키마 — Agent/Tool I/O 검증용
# ═══════════════════════════════════════════════════


class BaseMetadata(BaseModel):
    """공통 메타데이터 (모든 Agent 필수)."""

    model: str
    tokens_used: int
    cost_usd: float = 0.0
    duration_sec: float
    retry_count: int = 0
    cache_hit: bool = False


class AgentInput(BaseModel):
    """Agent 공통 입력."""

    trace_id: str = Field(..., description="실행 추적 ID")
    context: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)


class AgentOutput(BaseModel):
    """Agent 공통 출력."""

    trace_id: str = Field(..., description="실행 추적 ID")
    result: dict[str, Any] = Field(..., description="실행 결과")
    confidence: float = Field(..., ge=0.0, le=1.0)
    metadata: BaseMetadata


class ToolInput(BaseModel):
    """Tool 공통 입력."""

    trace_id: str = Field(..., description="실행 추적 ID")
    params: dict[str, Any] = Field(default_factory=dict)


class ToolOutput(BaseModel):
    """Tool 공통 출력."""

    trace_id: str = Field(..., description="실행 추적 ID")
    result: dict[str, Any] = Field(..., description="실행 결과")
    metadata: dict[str, Any] = Field(default_factory=dict)


# ═══════════════════════════════════════════════════
# TypedDict — 파이프라인 데이터 타입
# ═══════════════════════════════════════════════════


class RunOptions(TypedDict):
    """실행 옵션.

    파이프라인은 3단계로 분리되어 있다:
    - generate_scenarios: Layer 1A — 시나리오 + 테스트 데이터 생성
    - generate_code: Layer 1B — 액션 매핑 + Playwright 코드 생성
    - test: Layer 2~3 — 테스트 실행 + (필요 시) 장애 분석
    """

    command: Literal["generate_scenarios", "generate_code", "test"]
    # generate_scenarios 전용
    trigger: Literal["init", "code_change", "doc_update", "natural_lang"] | None
    user_input: str | None
    # generate_code / test 공통 — 대상 시나리오 필터
    scenario_ids: list[str] | None
    filter: Literal["all", "failed", "affected"] | None
    tags: list[str] | None
    # test 전용 — 이어서 실행: 지정된 이전 trace 의 results 디렉토리에 이미 ui_result.json
    # 이 있는 TC 는 새 실행에서 자동 skip 한다. 끊긴 시점부터 이어가기 위함.
    resume_from_trace: str | None
    # git_codebase_scanner 전용 — Git 접속 정보
    repo_url: str
    token: str
    branch: str
    local_path: str
    repos: list[dict]


# ── 코드베이스 스캔 ──


class FileInfo(TypedDict):
    """파일 분석 결과."""

    path: str
    language: str
    endpoints: list[dict]
    functions: list[dict]
    dependencies: list[str]
    models: list[dict]


class GitDiff(TypedDict):
    """Git 변경 정보."""

    commit_hash: str
    prev_hash: str
    changed_files: list[str]
    added_lines: int
    deleted_lines: int
    diff_detail: list[dict]
    author: str            # HEAD commit 작성자 이름
    author_email: str      # HEAD commit 작성자 이메일
    commit_timestamp: str  # HEAD commit 시점 (ISO 8601 UTC)
    blame: list[dict]      # 변경 파일별 blame 요약


class ScanResult(TypedDict):
    """코드베이스 스캔 결과."""

    files: list[FileInfo]
    git_diff: GitDiff | None
    framework: str
    language: str
    endpoint_count: int


# ── 도메인 지식 ──


class DomainRule(TypedDict):
    """도메인 규칙."""

    rule_id: str
    source: str
    category: str
    content: str
    similarity_score: float


# ── 요구사항 ──


class RequirementItem(TypedDict):
    """추출된 요구사항 항목."""

    req_id: str  # REQ-XXX
    req_type: Literal["functional", "non_functional"]
    content: str
    priority: Literal["high", "medium", "low"]
    domain_area: str


# ── TS / TC / TV ──


class TestValue(TypedDict):
    """테스트 밸류."""

    field: str
    value: str
    type: str
    purpose: str


class TestCase(TypedDict):
    """테스트 케이스."""

    tc_id: str
    name: str
    given: str
    when: str
    then: str
    values: list[TestValue]
    tags: list[str]
    req_id: str | None


class TestScenario(TypedDict):
    """테스트 시나리오."""

    ts_id: str
    name: str
    description: str
    trigger: str
    affected_files: list[str]
    domain_rules_used: list[str]
    depends_on: list[str]  # 선행 실행이 필요한 TS ID 목록 (예: ["TS-001"])
    test_cases: list[TestCase]


# ── 액션 매핑 ──


class ActionStep(TypedDict):
    """UI 액션 스텝."""

    step_no: int
    action: str
    selector: str | None
    selector_type: str | None
    value: str | None
    expected: str | None
    api_endpoint: str | None


class ActionMapping(TypedDict):
    """시나리오-액션 매핑 결과."""

    tc_id: str
    steps: list[ActionStep]
    selector_confidence: float


# ── 코드 생성 ──


class GeneratedCode(TypedDict):
    """생성된 Playwright 코드."""

    tc_id: str
    code: str
    self_fix_count: int
    syntax_valid: bool


# ── 테스트 실행 결과 ──


class UIStepResult(TypedDict):
    """UI 스텝 실행 결과."""

    step_no: int
    action: str
    status: Literal["pass", "fail", "skip"]
    screenshot_path: str | None
    console_logs: list[str]
    error: str | None
    duration_ms: int


class UITestResult(TypedDict):
    """UI 테스트 결과."""

    tc_id: str
    status: Literal["pass", "fail"]
    steps: list[UIStepResult]
    total_duration_ms: int


class APICall(TypedDict):
    """API 호출 기록."""

    timestamp: str
    method: str
    url: str
    request_headers: dict
    request_body: dict | None
    status_code: int
    response_body: dict | None
    response_size: int
    content_type: str
    latency_ms: int
    matched_step_no: int | None


class APITraceResult(TypedDict):
    """API 추적 결과."""

    tc_id: str
    calls: list[APICall]
    total_calls: int
    error_calls: int


class DBSnapshot(TypedDict):
    """DB 스냅샷."""

    table: str
    row_count_before: int
    row_count_after: int
    added: int
    deleted: int
    modified: int


class DBTestResult(TypedDict):
    """DB 테스트 결과."""

    tc_id: str
    snapshots: list[DBSnapshot]
    summary: str


# ── Cross-check ──


class CrossCheckMismatch(TypedDict):
    """불일치 항목."""

    field: str
    ui_value: str
    api_value: str
    db_value: str | None


class CrossCheckResult(TypedDict):
    """Cross-check 결과."""

    tc_id: str
    match_score: float
    matched_fields: int
    mismatched_fields: int
    mismatches: list[CrossCheckMismatch]
    has_mismatch: bool


# ── 장애 분석 ──


class Evidence(TypedDict):
    """원인 근거."""

    type: Literal["code_location", "runtime_data"]
    content: str


class RootCauseCandidate(TypedDict):
    """원인 후보."""

    rank: int
    cause: str
    confidence: float
    evidences: list[Evidence]



class RootCauseResult(TypedDict):
    """원인 추론 결과."""

    tc_id: str
    candidates: list[RootCauseCandidate]


class FixSuggestion(TypedDict):
    """수정 제안."""

    file_path: str
    line_number: int
    blame_author: str | None
    code_snippet: str
    description: str
    similar_issues: list[str]


class FixResult(TypedDict):
    """해결 방안 결과."""

    tc_id: str
    suggestions: list[FixSuggestion]


# ── 메타 ──


class AgentMeta(TypedDict):
    """Agent 실행 메타."""

    agent_name: str
    model: str
    tokens_used: int
    cost_usd: float
    duration_sec: float
    retry_count: int
    cache_hit: bool
