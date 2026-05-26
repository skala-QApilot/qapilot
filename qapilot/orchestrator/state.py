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
    # 산출물 저장 루트. 절대경로 권장. (예: /path/to/system-under-test/.qapilot)
    # 파이프라인 전 노드가 이 값을 기준으로 scenarios / generated-code / results 등을 read/write 한다.
    # CWD 의존을 제거하기 위해 도입됨 — 절대 Path(".qapilot")/... 식으로 직접 쓰지 말 것.
    qapilot_dir: str

    # SUT base URL (예: https://staging.example.com). SaaS 멀티 테넌트 구조에서 service별
    # stagingUrl 을 Layer 2 의 Playwright base URL 로 사용한다. CLI 단독 실행 시엔 빈 문자열
    # → _test_execution 이 cfg.project.target_url 로 fallback 한다.
    staging_url: str

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
    # TC / TS 별 최종 status 요약 ('passed' | 'failed').
    # trace.json 에 보존되어 Spring 이 시나리오별 last_run_status 도출 시 사용한다.
    tc_results: dict[str, str]
    scenario_results: dict[str, str]

    # ── Layer 3: 장애 분석 ──
    root_cause_results: list[RootCauseResult]
    fix_results: list[FixResult]

    # ── 리포트 ──
    report_path: str | None

    # ── 메타 ──
    agent_logs: list[AgentMeta]
    total_cost: float
