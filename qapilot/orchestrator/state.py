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
    # SaaS 멀티 테넌트 service 식별자. runner.run_pipeline 의 service_id 인자 → initial_state.
    # 노드 간 LangGraph propagation 정합을 위해 schema 명시 (#248). 본 필드 누락 시 TypedDict
    # 가 strict 아니라 dict literal 추가는 통과하나, 일부 노드의 state.get("service_id") 가
    # None 반환 → mirror fallback skip 등 격차 발생 (e2e trace `5ab07f0f`). CLI 단독 실행 시
    # 빈 문자열 "" (default).
    service_id: str
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

    # SUT 코드베이스 root 경로 (격차 SaaS target_root, PR #269/#271 후속). SaaS 호출 경로에서
    # Spring 이 `services.target_root` 컬럼을 body 의 `target_root` 로 채워 보내면 agent_router
    # 가 state 에 주입. `_resolve_project_root` 가 cfg.project.root 다음, qapilot_dir derive 전
    # 우선 사용 → 본인 데이터 layer 의 `scan_all_metadata` / `load_source` 가 SUT 실제 코드
    # path 로 동작. CLI 단독 실행 / Spring 미주입 시 None — cfg.project.root 또는 state.qapilot_dir
    # derive fallback. 둘 다 fail 시 TV codebase-aware 단계가 `tv_metadata_index_empty` graceful skip.
    target_root: str | None

    # SUT 인증 정보 (격차 12 SaaS 후속). SaaS 호출 경로에서 Spring 이 body 에 `test_account`
    # dict ({email, password, login_path?}) 를 채워 보내면 agent_router 가 state 에 주입.
    # _test_execution 이 cfg.project.test_account 보다 우선 사용 (staging_url 동형).
    # CLI 단독 실행 / Spring 미주입 시 None — UITestTool 의 _ensure_authenticated 가 cfg 로
    # fallback 또는 graceful skip.
    test_account: dict | None

    # 도메인 문서 메타 리스트 (격차 #207 sub-D). Spring 이 `DomainDocumentRepository` 에서
    # 읽어 body 에 `domain_files=[{file_id, filename, s3_key, version, type, reflected?}, ...]`
    # 형태로 채워 보내면 agent_router 가 state 에 주입.
    # sub-F Part 2 의 pipeline._doc_import 가 각 entry 의 `s3_key` 를 `s3_client.download()`
    # (sub-E) 로 tmp 디렉토리에 받아 기존 DomainKnowledgeTool / RequirementExtractor 로 처리.
    # CLI 단독 실행 / Spring 미주입 시 None — pipeline 이 cfg.project.root/docs 로 fallback
    # (CLI 호환 한정) 또는 graceful skip (requirements_count=0). 본 PR 은 통로만, 활용은 sub-F Part 2.
    domain_files: list[dict] | None

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
    frontend_dom: list[dict]

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

    # ── prd_only_experiment ──
    ts_list: list[dict]            # TSFromPRDAgent 출력
    tc_by_ts_index: dict           # index → list[dict] (TC 목록)
    tc_provenance_by_index: dict   # index → TCProvenance dict (doc_search 단계에서 채움)

    # ── 메타 ──
    agent_logs: list[AgentMeta]
    total_cost: float

    # ── natural_lang 챗봇 응답 ──
    query_status: str | None
    query_feedback: str | None
    change_summary: list[str] | None
