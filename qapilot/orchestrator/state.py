"""PipelineState 정의.

LangGraph StateGraph에서 사용하는 공유 상태.
generate(Layer 1)와 test(Layer 2~3)가 하나의 그래프에서
command 값에 따라 진입점이 분기된다.

담당: A
Created: 2026-05-07
"""

from typing import Literal, TypedDict

from qapilot.shared.schemas import (
    ActionMapping,
    AgentMeta,
    CrossCheckResult,
    DefectClassification,
    DomainRule,
    FixResult,
    GeneratedCode,
    HITLRecord,
    RequirementItem,
    RootCauseResult,
    RunOptions,
    ScanResult,
    TestScenario,
    UITestResult,
    APITraceResult,
    DBTestResult,
)


class PipelineState(TypedDict):
    """파이프라인 공유 상태."""

    # 실행 옵션
    run_options: RunOptions

    # 공통
    trace_id: str
    status: str
    current_layer: str
    error: str | None

    # ── Layer 1: 컨텍스트 준비 ──
    scan_result: ScanResult | None
    domain_rules: list[DomainRule]
    requirements: list[RequirementItem]

    # ── Layer 1: 시나리오 생성 ──
    scenarios: list[TestScenario]
    hitl_records: list[HITLRecord]
    hitl_approved: bool
    action_mappings: list[ActionMapping]
    generated_codes: list[GeneratedCode]

    # ── Layer 2: 테스트 실행 ──
    ui_results: list[UITestResult]
    api_results: list[APITraceResult]
    db_results: list[DBTestResult]
    cross_check_results: list[CrossCheckResult]
    has_mismatch: bool

    # ── Layer 3: 장애 분석 ──
    defect_results: list[DefectClassification]
    root_cause_results: list[RootCauseResult]
    fix_results: list[FixResult]

    # ── 리포트 ──
    report_path: str | None

    # ── 메타 ──
    agent_logs: list[AgentMeta]
    total_cost: float
