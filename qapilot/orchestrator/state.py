"""PipelineState 정의.

LangGraph StateGraph에서 사용하는 공유 상태.
generate_scenarios / generate_code / test 가 하나의 그래프에서
command 값에 따라 진입점이 분기된다.

HITL은 별도 모듈로 두지 않고, 파이프라인을 3단계로 분리하여
사용자가 generate_scenarios 와 generate_code 사이에서 자유롭게
시나리오를 수정·삭제할 수 있도록 한다.

담당: A
Created: 2026-05-07
"""

from typing import TypedDict

from qapilot.shared.schemas import (
    ActionMapping,
    AgentMeta,
    APITraceResult,
    CrossCheckResult,
    DBTestResult,
    DomainRule,
    FixResult,
    GeneratedCode,
    RequirementItem,
    RootCauseResult,
    RunOptions,
    ScanResult,
    TestScenario,
    UITestResult,
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

    # ── Layer 1A: 컨텍스트 + 시나리오 생성 ──
    scan_result: ScanResult | None
    domain_rules: list[DomainRule]
    requirements: list[RequirementItem]
    scenarios: list[TestScenario]
    saved_scenario_paths: list[str]  # generate_scenarios 결과: 저장된 파일 경로

    # ── Layer 1B: 액션 매핑 + 코드 생성 ──
    action_mappings: list[ActionMapping]
    generated_codes: list[GeneratedCode]
    saved_code_paths: list[str]  # generate_code 결과: 저장된 코드 파일 경로

    # ── Layer 2: 테스트 실행 ──
    ui_results: list[UITestResult]
    api_results: list[APITraceResult]
    db_results: list[DBTestResult]
    cross_check_results: list[CrossCheckResult]
    has_mismatch: bool

    # ── Layer 3: 장애 분석 ──
    root_cause_results: list[RootCauseResult]
    fix_results: list[FixResult]

    # ── 리포트 ──
    report_path: str | None

    # ── 메타 ──
    agent_logs: list[AgentMeta]
    total_cost: float
