"""ScenarioGeneratorAgent 단위 테스트.

LLM 응답을 모킹하여 agent 핵심 로직을 검증한다.
- 도메인 규칙·요구사항 → 시나리오 생성
- TC 중복 제거
- PRD-코드 불일치 탐지
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qapilot.agents.scenario_generator import ScenarioGeneratorAgent
from qapilot.shared.llm_client import LLMResponse
from qapilot.shared.schemas import AgentInput

# ── 공통 픽스처 ────────────────────────────────────────────────────────────────

SAMPLE_REQUIREMENTS = [
    {
        "req_id": "REQ-001",
        "req_type": "functional",
        "content": "사용자는 이메일과 비밀번호로 회원 가입할 수 있다",
        "priority": "high",
        "domain_area": "회원",
    },
    {
        "req_id": "REQ-002",
        "req_type": "functional",
        "content": "사용자는 로그인 후 마이페이지에 접근할 수 있다",
        "priority": "medium",
        "domain_area": "인증",
    },
]

SAMPLE_DOMAIN_RULES = [
    {
        "rule_id": "RULE-001",
        "source": "PRD_v3.0.pdf",
        "category": "회원",
        "content": "비밀번호는 8자 이상이어야 한다",
        "similarity_score": 0.95,
    },
]

SAMPLE_SCAN_RESULT = {
    "framework": "FastAPI",
    "language": "Python",
    "endpoint_count": 3,
    "files": [
        {
            "path": "src/auth.py",
            "language": "Python",
            "endpoints": [
                {"method": "POST", "path": "/api/auth/signup"},
                {"method": "POST", "path": "/api/auth/login"},
            ],
        }
    ],
}

VALID_LLM_JSON = {
    "scenarios": [
        {
            "name": "회원 가입 시나리오",
            "description": "회원 가입 정상/예외 흐름 검증",
            "affected_files": ["src/auth.py"],
            "test_cases": [
                {
                    "name": "정상 회원 가입",
                    "given": "이메일과 비밀번호가 유효한 상태에서",
                    "when": "회원 가입을 요청하면",
                    "then": "계정이 생성되고 환영 이메일이 발송된다",
                    "values": [
                        {"field": "email", "value": "test@example.com", "type": "string", "purpose": "유효한 이메일"}
                    ],
                    "tags": ["normal"],
                    "req_id": "REQ-001",
                },
                {
                    "name": "중복 이메일로 가입 시도",
                    "given": "이미 가입된 이메일이 있는 상태에서",
                    "when": "동일한 이메일로 회원 가입을 요청하면",
                    "then": "중복 이메일 오류가 반환된다",
                    "values": [
                        {"field": "email", "value": "dup@example.com", "type": "string", "purpose": "중복 이메일"}
                    ],
                    "tags": ["edge_case"],
                    "req_id": "REQ-001",
                },
            ],
        }
    ],
    "confidence": 0.9,
}


def _make_llm_response(data: dict) -> LLMResponse:
    return LLMResponse(
        content=json.dumps(data, ensure_ascii=False),
        model="gpt-4o-mini",
        input_tokens=50,
        output_tokens=50,
        cost_usd=0.0,
        cached=False,
    )


def _make_input(**context_kwargs) -> AgentInput:
    return AgentInput(
        trace_id="test-trace",
        context={
            "requirements": SAMPLE_REQUIREMENTS,
            "domain_rules": SAMPLE_DOMAIN_RULES,
            "scan_result": SAMPLE_SCAN_RESULT,
            **context_kwargs,
        },
        params={"trigger": "code_change", "affected_only": False},
    )


def _patch_index(agent: ScenarioGeneratorAgent, endpoints: list[dict]) -> None:
    def read_index(filename: str):
        if filename == "endpoints.json":
            return endpoints
        if filename == "manifest.json":
            return {"framework": "FastAPI", "language": "Python"}
        return []

    agent._read_index_json = read_index  # type: ignore[method-assign]


# ── 테스트 ──────────────────────────────────────────────────────────────────────


@patch("qapilot.agents.scenario_generator.agent.save_scenarios")
async def test_시나리오_정상_생성(mock_save):
    """도메인 규칙·요구사항·스캔 결과가 context에 있을 때 시나리오가 생성된다."""
    agent = ScenarioGeneratorAgent(trace_id="test-trace")
    agent.llm.chat = AsyncMock(return_value=_make_llm_response(VALID_LLM_JSON))

    output = await agent.run(_make_input())

    scenarios = output.result["scenarios"]
    assert len(scenarios) == len(SAMPLE_REQUIREMENTS)
    assert scenarios[0]["ts_id"] == "TS-001"
    assert scenarios[0]["name"] == "회원 가입 시나리오"
    assert len(scenarios[0]["test_cases"]) == 2
    assert output.confidence == pytest.approx(0.9)
    mock_save.assert_called_once_with(scenarios)


@patch("qapilot.agents.scenario_generator.agent.save_scenarios")
async def test_TC_중복_제거(mock_save):
    """동일한 given/when/then 조합의 TC는 한 번만 포함된다."""
    tc = {
        "name": "중복 TC",
        "given": "동일한 given",
        "when": "동일한 when",
        "then": "동일한 then",
        "values": [],
        "tags": ["normal"],
        "req_id": None,
    }
    data = {
        "scenarios": [
            {
                "name": "중복 테스트",
                "description": "",
                "affected_files": [],
                "test_cases": [tc, tc],  # 동일 TC 2개
            }
        ],
        "confidence": 0.7,
    }

    agent = ScenarioGeneratorAgent(trace_id="test-trace")
    agent.llm.chat = AsyncMock(return_value=_make_llm_response(data))

    output = await agent.run(_make_input())

    scenarios = output.result["scenarios"]
    assert len(scenarios[0]["test_cases"]) == 1


@patch("qapilot.agents.scenario_generator.agent.save_scenarios")
async def test_PRD_코드_불일치_탐지(mock_save):
    """high priority 요구사항의 도메인이 코드에 없으면 불일치 목록에 포함된다."""
    requirements_with_missing = [
        {
            "req_id": "REQ-099",
            "req_type": "functional",
            "content": "사용자는 배송 현황을 조회할 수 있다",
            "priority": "high",
            "domain_area": "배송",  # scan_result에 배송 관련 코드 없음
        }
    ]
    agent = ScenarioGeneratorAgent(trace_id="test-trace")
    agent.llm.chat = AsyncMock(return_value=_make_llm_response(VALID_LLM_JSON))

    output = await agent.run(_make_input(requirements=requirements_with_missing))

    mismatches = output.result["prd_code_mismatches"]
    assert len(mismatches) == 1
    assert mismatches[0]["req_id"] == "REQ-099"
    assert "배송" in mismatches[0]["note"]


@patch("qapilot.agents.scenario_generator.agent.save_scenarios")
async def test_affected_only_파라미터(mock_save):
    """affected_only=True이면 git_diff 변경 파일만 스캔 대상으로 설정한다."""
    scan_with_diff = {
        **SAMPLE_SCAN_RESULT,
        "git_diff": {"changed_files": ["src/auth.py"]},
    }
    agent = ScenarioGeneratorAgent(trace_id="test-trace")
    agent.llm.chat = AsyncMock(return_value=_make_llm_response(VALID_LLM_JSON))

    agent_input = AgentInput(
        trace_id="test-trace",
        context={
            "requirements": SAMPLE_REQUIREMENTS,
            "domain_rules": SAMPLE_DOMAIN_RULES,
            "scan_result": scan_with_diff,
        },
        params={"trigger": "code_change", "affected_only": True},
    )
    output = await agent.run(agent_input)

    scenarios = output.result["scenarios"]
    # affected_files가 git_diff 기반으로 설정되어야 함
    assert scenarios[0]["affected_files"] == ["src/auth.py"]


def test_affected_router_map_변경파일_미매칭시_전체_fallback_금지():
    """code_change는 diff에 잡힌 파일이 라우터와 매칭되지 않아도 전체 라우터로 확장하지 않는다."""
    agent = ScenarioGeneratorAgent(trace_id="test-trace")
    endpoints = [
        {"file": "src/auth.py", "handler": "signup", "path": "/api/auth/signup"},
        {"file": "src/orders.py", "handler": "create_order", "path": "/api/orders"},
    ]

    def read_index(filename):
        if filename == "endpoints.json":
            return endpoints
        if filename == "callgraph.json":
            return {}
        if filename == "functions.json":
            return []
        return []

    with patch.object(agent, "_read_index_json", side_effect=read_index):
        assert agent._build_router_map(["src/unrelated_config.py"]) == {}


def test_affected_router_map_main_import_라우터만_포함():
    """main.py 변경은 include/import 한 라우터로만 좁혀 시나리오를 생성한다."""
    agent = ScenarioGeneratorAgent(trace_id="test-trace")
    endpoints = [
        {"file": "backend/app/routers/auth.py", "handler": "login", "path": "/api/auth/login"},
        {"file": "backend/app/routers/orders.py", "handler": "create_order", "path": "/api/orders"},
    ]
    callgraph = {
        "backend/app/main.py": ["from app.routers import auth"],
    }

    def read_index(filename):
        if filename == "endpoints.json":
            return endpoints
        if filename == "callgraph.json":
            return callgraph
        if filename == "functions.json":
            return []
        return []

    with patch.object(agent, "_read_index_json", side_effect=read_index):
        router_map = agent._build_router_map(["backend/app/main.py"])

    assert set(router_map) == {"backend/app/routers/auth.py"}


@patch("qapilot.agents.scenario_generator.agent.save_scenarios")
async def test_TS_TC_ID_자동_부여(mock_save):
    """TS-001, TS-001-TC-01 형식의 ID가 순서대로 부여된다."""
    agent = ScenarioGeneratorAgent(trace_id="test-trace")
    agent.llm.chat = AsyncMock(return_value=_make_llm_response(VALID_LLM_JSON))

    output = await agent.run(_make_input())

    scenarios = output.result["scenarios"]
    assert scenarios[0]["ts_id"] == "TS-001"
    tcs = scenarios[0]["test_cases"]
    assert tcs[0]["tc_id"] == "TS-001-TC-01"
    assert tcs[1]["tc_id"] == "TS-001-TC-02"


def test_요구사항_API_후보_밖_api는_null로_정리된다():
    """요구사항에 매핑된 API 후보 밖의 실제 API도 해당 TS에는 연결하지 않는다."""
    agent = ScenarioGeneratorAgent(trace_id="test-trace")
    endpoints = [
        {"method": "POST", "path": "", "file": "src/auth.py", "handler": "signup"},
        {"method": "GET", "path": "/me", "file": "src/auth.py", "handler": "me"},
    ]
    _patch_index(agent, endpoints)

    scenarios = [
        {
            "test_cases": [
                {"api": "POST /api/auth"},
                {"api": "GET /api/auth/me"},
            ]
        }
    ]

    agent._sanitize_api_fields(scenarios, {"POST /api/auth"})

    assert scenarios[0]["test_cases"][0]["api"] == "POST /api/auth"
    assert scenarios[0]["test_cases"][1]["api"] is None


def test_요구사항_ID는_현재_요구사항으로_고정된다():
    """LLM이 없는 요구사항 ID를 만들면 현재 요구사항 ID로 덮어쓴다."""
    agent = ScenarioGeneratorAgent(trace_id="test-trace")
    scenarios = [
        {
            "requirements": ["FR-FAKE-99"],
            "test_cases": [
                {"req_id": "FR-FAKE-99"},
                {"req_id": None},
            ],
        }
    ]

    agent._pin_req_id(scenarios, "FR-ORDER-01")

    assert scenarios[0]["requirements"] == ["FR-ORDER-01"]
    assert [tc["req_id"] for tc in scenarios[0]["test_cases"]] == [
        "FR-ORDER-01",
        "FR-ORDER-01",
    ]


def test_전역_api_null_채움은_요구사항_매핑_api만_사용한다():
    """저장 직전 null api 보정도 요구사항에 매핑된 API 후보 안에서만 수행한다."""
    agent = ScenarioGeneratorAgent(trace_id="test-trace")
    endpoints = [
        {
            "method": "PATCH",
            "path": "/{order_id}/change-plan",
            "file": "backend/app/routers/orders.py",
            "handler": "change_plan",
        },
        {
            "method": "GET",
            "path": "",
            "file": "backend/app/routers/orders.py",
            "handler": "list_orders",
        },
    ]
    _patch_index(agent, endpoints)
    scenarios = [
        {
            "name": "요금제 변경",
            "requirements": ["FR-ORDER-02"],
            "affected_files": ["backend/app/routers/orders.py"],
            "test_cases": [
                {
                    "name": "요금제 즉시 변경",
                    "when": "요금제 변경을 요청하면",
                    "api": None,
                }
            ],
        }
    ]

    agent._fill_api_nulls_global(
        scenarios,
        {"FR-ORDER-02": [endpoints[0]]},
    )

    assert scenarios[0]["test_cases"][0]["api"] == "PATCH /api/orders/{order_id}/change-plan"


def test_get_full_ep_path는_api_절대경로에_prefix를_중복하지_않는다():
    agent = ScenarioGeneratorAgent(trace_id="test-trace")

    ep = {
        "method": "PATCH",
        "path": "/api/orders/{order_id}/change-plan",
        "file": "backend/app/routers/orders.py",
        "handler": "change_order_plan",
    }

    assert agent._get_full_ep_path(ep) == "PATCH /api/orders/{order_id}/change-plan"


def test_요구사항_분리에서_API검증_불가_NFR은_제외된다():
    """성능/가용성 NFR은 제외하고 X-Trace-Id는 API 검증 대상으로 남긴다."""
    agent = ScenarioGeneratorAgent(trace_id="test-trace")
    requirements = [
        {
            "req_id": "FR-TIER-01",
            "req_type": "functional",
            "content": "[FR-TIER-01] 내 등급 조회 — 엔드포인트: GET /api/tier",
            "priority": "high",
            "domain_area": "멤버십 등급",
        },
        {
            "req_id": "REQ-002",
            "req_type": "non_functional",
            "content": "API 응답 시간 p99는 500ms 이하이어야 한다",
            "priority": "medium",
            "domain_area": "성능",
        },
        {
            "req_id": "REQ-005",
            "req_type": "non_functional",
            "content": "모든 응답에 X-Trace-Id 헤더를 부여한다",
            "priority": "medium",
            "domain_area": "추적성",
        },
    ]

    target, skipped = agent._split_scenario_requirements(requirements)

    assert [req["req_id"] for req in target] == ["FR-TIER-01", "REQ-005"]
    assert [req["req_id"] for req in skipped] == ["REQ-002"]


def test_요구사항_본문의_명시적_API를_우선_매핑한다():
    """FR 본문에 적힌 METHOD /api/path가 있으면 그 엔드포인트만 후보로 사용한다."""
    agent = ScenarioGeneratorAgent(trace_id="test-trace")
    endpoints = [
        {
            "method": "GET",
            "path": "",
            "file": "backend/app/routers/tier.py",
            "handler": "get_my_tier",
        },
        {
            "method": "POST",
            "path": "/brands/{brand_code}/issue",
            "file": "backend/app/routers/tier.py",
            "handler": "issue_tier_brand_coupon",
        },
    ]

    selected = agent._select_endpoints_for_requirement(
        {
            "req_id": "FR-TIER-01",
            "content": "[FR-TIER-01] 내 등급 조회 — 엔드포인트: GET /api/tier",
            "domain_area": "멤버십 등급",
        },
        endpoints,
    )

    assert [agent._get_full_ep_path(ep) for ep in selected] == ["GET /api/tier"]


# ── update 액션 테스트 (#201) ──────────────────────────────────────────────────

EXISTING_TS = {
    "ts_id": "TS-001",
    "name": "로그인 시나리오",
    "trigger": "init",
    "test_cases": [
        {
            "tc_id": "TS-001-TC-01",
            "name": "비밀번호 5회 이상 오류",
            "given": "5회 이상 틀린 상태에서",
            "when": "로그인 시도하면",
            "then": "계정이 잠긴다",
            "values": [{"field": "attempt", "value": "6", "type": "int", "purpose": "경계값"}],
            "tags": ["edge_case"],
            "req_id": "REQ-001",
        }
    ],
}

UPDATE_TC_LLM_JSON = {
    "scenarios": [
        {
            "name": "로그인 시나리오",
            "test_cases": [
                {
                    "tc_id": "TS-001-TC-NEW",
                    "name": "비밀번호 4회 이상 오류",
                    "given": "4회 이상 틀린 상태에서",
                    "when": "로그인 시도하면",
                    "then": "경고 메시지가 표시된다",
                    "values": [{"field": "attempt", "value": "4", "type": "int", "purpose": "경계값"}],
                    "tags": ["edge_case"],
                    "req_id": "REQ-001",
                }
            ],
        }
    ],
    "confidence": 0.85,
}

ADD_TV_LLM_JSON = {
    "scenarios": [
        {
            "name": "로그인 시나리오",
            "test_cases": [
                {
                    "tc_id": "TS-001-TC-01",
                    "name": "비밀번호 5회 이상 오류",
                    "given": "5회 이상 틀린 상태에서",
                    "when": "로그인 시도하면",
                    "then": "계정이 잠긴다",
                    "values": [
                        {"field": "attempt", "value": "6", "type": "int", "purpose": "경계값"},
                        {"field": "email", "value": "naver@naver.com", "type": "str", "purpose": "네이버 도메인"},
                    ],
                    "tags": ["edge_case"],
                    "req_id": "REQ-001",
                }
            ],
        }
    ],
    "confidence": 0.85,
}

UPDATE_REQ = {
    "req_id": "REQ-002",
    "req_type": "functional",
    "content": "비밀번호 4회 이상 오류 케이스 추가",
    "priority": "medium",
    "domain_area": "인증",
    "action_type": "update",
    "target_level": "tc",
    "target_ts_id": "TS-001",
    "target_tc_id": None,
}


@patch("qapilot.agents.scenario_generator.agent.save_scenarios")
@patch("qapilot.agents.scenario_generator.repository.save_scenario")
async def test_update_tc_기존_TC_보존하며_새_TC_추가(mock_save_one, mock_save_all):
    """action_type=update + target_level=tc이면 기존 TC를 보존하고 새 TC를 추가한다."""
    agent = ScenarioGeneratorAgent(trace_id="test-update")
    agent.llm.chat = AsyncMock(return_value=_make_llm_response(UPDATE_TC_LLM_JSON))
    agent._load_scenario_file = MagicMock(return_value=EXISTING_TS)  # type: ignore[method-assign]
    _patch_index(agent, [{"method": "POST", "path": "/api/auth/login", "file": "auth.py"}])

    input_data = AgentInput(
        trace_id="test-update",
        context={
            "requirements": [UPDATE_REQ],
            "domain_rules": [],
            "scan_result": SAMPLE_SCAN_RESULT,
            "qapilot_dir": "/tmp/test",
        },
        params={"trigger": "natural_lang", "affected_only": False},
    )

    output = await agent.run(input_data)
    scenarios = output.result["scenarios"]

    assert len(scenarios) == 1
    ts = scenarios[0]
    assert ts["ts_id"] == "TS-001"  # ts_id 고정

    tc_names = [tc["name"] for tc in ts["test_cases"]]
    assert "비밀번호 5회 이상 오류" in tc_names   # 기존 TC 보존
    assert "비밀번호 4회 이상 오류" in tc_names   # 새 TC 추가
    assert len(ts["test_cases"]) == 2
    mock_save_one.assert_called_once()  # update는 save_scenario 사용
    mock_save_all.assert_not_called()   # save_scenarios 미사용


@patch("qapilot.agents.scenario_generator.agent.save_scenarios")
@patch("qapilot.agents.scenario_generator.repository.save_scenario")
async def test_update_ts_기존_ts_id_유지(mock_save_one, mock_save_all):
    """action_type=update + target_level=ts이면 ts_id를 유지하며 전체 재생성한다."""
    regen_json = {
        "scenarios": [{"name": "로그인 시나리오 (재생성)", "test_cases": [
            {"name": "정상 로그인", "given": "g", "when": "w", "then": "t",
             "values": [], "tags": ["normal"], "req_id": "REQ-001"}
        ]}],
        "confidence": 0.9,
    }
    req = {**UPDATE_REQ, "target_level": "ts"}
    agent = ScenarioGeneratorAgent(trace_id="test-update-ts")
    agent.llm.chat = AsyncMock(return_value=_make_llm_response(regen_json))
    agent._load_scenario_file = MagicMock(return_value=EXISTING_TS)  # type: ignore[method-assign]
    _patch_index(agent, [{"method": "POST", "path": "/api/auth/login", "file": "auth.py"}])

    input_data = AgentInput(
        trace_id="test-update-ts",
        context={"requirements": [req], "domain_rules": [], "scan_result": SAMPLE_SCAN_RESULT, "qapilot_dir": "/tmp"},
        params={"trigger": "natural_lang", "affected_only": False},
    )
    output = await agent.run(input_data)
    ts = output.result["scenarios"][0]

    assert ts["ts_id"] == "TS-001"
    assert ts["name"] == "로그인 시나리오 (재생성)"
    mock_save_one.assert_called_once()


@patch("qapilot.agents.scenario_generator.agent.save_scenarios")
@patch("qapilot.agents.scenario_generator.repository.save_scenario")
async def test_update_ts_신규_TC_NEW_id를_다음_번호로_정규화(mock_save_one, mock_save_all):
    """TS delta 모드에서 LLM의 TC-NEW 플레이스홀더는 저장 전 실제 TC 번호로 바뀐다."""
    regen_json = {
        "scenarios": [
            {
                "name": "로그인 시나리오",
                "test_cases": [
                    {
                        "tc_id": "TS-001-TC-NEW-01",
                        "name": "잘못된 이메일 형식으로 회원가입 시도",
                        "given": "잘못된 형식의 이메일이 제공된 상태에서",
                        "when": "회원가입을 요청하면",
                        "then": "이메일 형식 오류가 반환된다",
                        "values": [],
                        "tags": ["edge_case"],
                        "req_id": "REQ-002",
                    }
                ],
            }
        ],
        "confidence": 0.9,
    }
    req = {**UPDATE_REQ, "target_level": "ts"}
    agent = ScenarioGeneratorAgent(trace_id="test-update-ts-new-id")
    agent.llm.chat = AsyncMock(return_value=_make_llm_response(regen_json))
    agent._load_scenario_file = MagicMock(return_value=EXISTING_TS)  # type: ignore[method-assign]
    _patch_index(agent, [{"method": "POST", "path": "/api/auth/login", "file": "auth.py"}])

    input_data = AgentInput(
        trace_id="test-update-ts-new-id",
        context={"requirements": [req], "domain_rules": [], "scan_result": SAMPLE_SCAN_RESULT, "qapilot_dir": "/tmp"},
        params={"trigger": "natural_lang", "affected_only": False},
    )
    output = await agent.run(input_data)
    ts = output.result["scenarios"][0]
    tc_ids = [tc["tc_id"] for tc in ts["test_cases"]]

    assert "TS-001-TC-NEW-01" not in tc_ids
    assert "TS-001-TC-02" in tc_ids
    assert len(ts["test_cases"]) == 2
    mock_save_one.assert_called_once()
    mock_save_all.assert_not_called()


@patch("qapilot.agents.scenario_generator.agent.save_scenarios")
@patch("qapilot.agents.scenario_generator.repository.save_scenario")
async def test_update_target_없으면_create_폴백(mock_save_one, mock_save_all):
    """target_ts_id가 null이면 update 대신 create 경로로 폴백한다."""
    req = {**UPDATE_REQ, "target_ts_id": None}
    agent = ScenarioGeneratorAgent(trace_id="test-fallback")
    agent.llm.chat = AsyncMock(return_value=_make_llm_response(VALID_LLM_JSON))
    agent._load_scenario_file = MagicMock(return_value=None)  # type: ignore[method-assign]
    _patch_index(agent, [{"method": "POST", "path": "/api/auth/login", "file": "auth.py"}])

    input_data = AgentInput(
        trace_id="test-fallback",
        context={"requirements": [req], "domain_rules": [], "scan_result": SAMPLE_SCAN_RESULT, "qapilot_dir": "/tmp"},
        params={"trigger": "natural_lang", "affected_only": False},
    )
    output = await agent.run(input_data)

    assert len(output.result["scenarios"]) >= 1
    mock_save_all.assert_called_once()  # create → save_scenarios 사용


@patch("qapilot.agents.scenario_generator.agent.save_scenarios")
@patch("qapilot.agents.scenario_generator.repository.save_scenario")
async def test_add_tv_기존_values_보존하며_새_value_추가(mock_save_one, mock_save_all):
    """action_type=update + target_level=tv이면 기존 values에 새 value를 병합한다."""
    req = {**UPDATE_REQ, "target_level": "tv", "target_tc_id": "TS-001-TC-01"}
    agent = ScenarioGeneratorAgent(trace_id="test-add-tv")
    agent.llm.chat = AsyncMock(return_value=_make_llm_response(ADD_TV_LLM_JSON))
    agent._load_scenario_file = MagicMock(return_value=EXISTING_TS)  # type: ignore[method-assign]
    _patch_index(agent, [{"method": "POST", "path": "/api/auth/login", "file": "auth.py"}])

    input_data = AgentInput(
        trace_id="test-add-tv",
        context={"requirements": [req], "domain_rules": [], "scan_result": SAMPLE_SCAN_RESULT, "qapilot_dir": "/tmp"},
        params={"trigger": "natural_lang", "affected_only": False},
    )
    output = await agent.run(input_data)
    ts = output.result["scenarios"][0]

    assert ts["ts_id"] == "TS-001"
    tc = ts["test_cases"][0]
    fields = [v["field"] for v in tc["values"]]
    assert "attempt" in fields   # 기존 value 보존
    assert "email" in fields     # 새 value 추가
    mock_save_one.assert_called_once()
