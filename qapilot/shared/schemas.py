"""공통 스키마 정의.

PipelineState (LangGraph용 TypedDict) + Agent/Tool I/O (Pydantic 검증용)를 정의한다.

Author: 공통
Created: 2026-05-07
"""

from dataclasses import dataclass
from typing import Any, Literal, NotRequired, TypedDict

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

    command: Literal["generate_scenarios", "generate_code", "test", "prd_only_experiment"]
    # generate_scenarios 전용
    trigger: Literal["init", "code_change", "doc_update", "natural_lang"] | None
    user_input: str | None
    # generate_code / test 공통 — 대상 시나리오 필터
    scenario_ids: list[str] | None
    # generate_code 증분 모드 — 삭제된 TC 산출물 정리 및 explicit empty 필터 보존
    deleted_tc_ids: NotRequired[list[str] | None]
    incremental: NotRequired[bool]
    filter: Literal["all", "failed", "affected"] | None
    tags: list[str] | None
    # test 전용 — 이어서 실행: 지정된 이전 trace 의 results 디렉토리에 이미 ui_result.json
    # 이 있는 TC 는 새 실행에서 자동 skip 한다. 끊긴 시점부터 이어가기 위함.
    resume_from_trace: str | None
    # natural_lang 전용 — 챗봇 대화 세션 ID (이슈 #182)
    session_id: str | None
    # git_codebase_scanner 전용 — Git 접속 정보
    repo_url: str
    token: str
    branch: str
    local_path: str
    repos: list[dict]
    # prd_only_experiment 전용 — TC 생성 대상 TS ID 목록 (미설정 시 앞 2개)
    tc_target_ts_ids: NotRequired[list[str] | None]


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
    action_type: NotRequired[Literal["create", "update"]]  # 기본값 "create"
    target_level: NotRequired[Literal["ts", "tc", "tv"]]   # update 시 수정 단위
    target_ts_id: NotRequired[str | None]                  # 대상 TS ID (임베딩 레이어에서 주입)
    target_tc_id: NotRequired[str | None]                  # 대상 TC ID (target_level=tc/tv 시 주입)


# ── TS / TC / TV ──


class TestValue(TypedDict):
    """테스트 밸류."""

    field: str
    value: str
    type: str
    purpose: str
    status: NotRequired[str]
    source: NotRequired[str]
    evidence_refs: NotRequired[list[str]]
    unresolved: NotRequired[bool]


class TCProvenance(TypedDict):
    """TC 버전(vN.json/latest.json)을 생성/수정한 실행에 대한 메타데이터.

    이 버전이 새로 쓰여질 때만 채워지며, 이후 변경되지 않는 TC의
    버전 파일은 기존 provenance를 그대로 유지한다.
    """

    trace_id: str | None
    generated_at: str | None
    source: str | None  # "init" | "code_change" | "doc_update" | "natural_lang" | "prd_only" | "doc_search"
    analysis: NotRequired[list[Any] | None]
    metrics: NotRequired[dict[str, Any] | None]
    retrieved_docs: NotRequired[list[dict[str, Any]]]  # doc_search 단계에서 검색된 문서 chunk (source/section/content/score)
    codebase_refs: NotRequired[list[dict[str, Any]]]  # tv_codebase_aware 단계에서 참고한 코드 스니펫 (file/line_start/line_end/content)


class TCGenerationContext(TypedDict):
    """TS의 TC 생성 단계에서 사용된 검색 쿼리·문서·코드 참조 (TS-{id}/metadata.json에 기록)."""

    query: str
    retrieved_docs: list[dict[str, Any]]
    codebase_refs: list[dict[str, Any]]


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
    api: str | None  # 검증 대상 API (예: "POST /api/orders")
    depends_on: list[str]  # 선행 TC ID 목록 (예: ["TS-034-TC-01"])
    given_status: NotRequired[str]
    when_status: NotRequired[str]
    then_status: NotRequired[str]
    given_source: NotRequired[str]
    when_source: NotRequired[str]
    then_source: NotRequired[str]
    given_evidence_refs: NotRequired[list[str]]
    when_evidence_refs: NotRequired[list[str]]
    then_evidence_refs: NotRequired[list[str]]
    given_unresolved: NotRequired[bool]
    when_unresolved: NotRequired[bool]
    then_unresolved: NotRequired[bool]
    technique: NotRequired[str]
    sources: NotRequired[list[str]]
    doc_verified: NotRequired[bool]
    provenance: NotRequired[TCProvenance]


class TSMetadata(TypedDict):
    """TS-{id}/metadata.json 에 저장되는 TS 구조 정보 (test_cases 제외)."""

    ts_id: str
    name: str
    description: str
    trigger: str
    affected_files: list[str]
    domain_rules_used: list[str]
    requirements: list[str]  # 참조한 PRD 요구사항 번호 (예: ["FR-ORDER-01", "FR-BIL-01"])
    depends_on: list[str]  # 선행 실행이 필요한 TS ID 목록 (예: ["TS-001"])
    last_modified_at: NotRequired[str]
    tc_generation_context: NotRequired[TCGenerationContext]


class TestScenario(TypedDict):
    """테스트 시나리오."""

    ts_id: str
    name: str
    description: str
    trigger: str
    affected_files: list[str]
    domain_rules_used: list[str]
    requirements: list[str]  # 참조한 PRD 요구사항 번호 (예: ["FR-ORDER-01", "FR-BIL-01"])
    depends_on: list[str]  # 선행 실행이 필요한 TS ID 목록 (예: ["TS-001"])
    test_cases: list[TestCase]
    last_modified_at: NotRequired[str]
    tc_generation_context: NotRequired[TCGenerationContext]


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
    target_name: NotRequired[str | None]
    target_kind: NotRequired[str | None]


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
