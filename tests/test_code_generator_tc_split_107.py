"""이슈 #107 — CodeGeneratorAgent TC-별 LLM 호출 분할 검증.

spec §4.5.1 의 "C(CodeGenerator) generates leniently" 본격 구현. 한 TC 의 LLM
응답 JSON parse 실패가 다른 TC 의 코드 생성을 차단하지 않음.

배경:
- 이전 단일 LLM 호출 구조: 78 TC 한 응답의 escape 오류 1건 → 전체 실패 (이슈 #105)
- TC-별 분할 후: 그 TC 만 skip, 나머지 정상 생성
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qapilot.agents.code_generator_agent import (
    CodeGeneratorAgent,
    _build_tc_to_scenario_index,
)


def _make_agent(mock_llm_client: MagicMock) -> CodeGeneratorAgent:
    """LLM / prompts / logger 만 mock 한 Agent — _execute 직접 호출."""
    agent = CodeGeneratorAgent.__new__(CodeGeneratorAgent)
    agent.llm = mock_llm_client
    agent.prompts = MagicMock()
    agent.prompts.system = MagicMock(return_value="SYSTEM")
    agent.prompts.render = MagicMock(side_effect=lambda **kw: f"USER<{kw.get('action_mappings','')}|frontend={kw.get('frontend_dom','')[:80]}>")
    agent.logger = MagicMock()
    agent.with_correction_hint = MagicMock(side_effect=lambda p, e: p)

    # 이슈 #140: per-TC LLMClient 분리 후 _create_tc_llm 이 매 호출마다 mock 반환.
    # 반환된 mock 의 chat 은 agent.llm.chat 와 동일 AsyncMock 공유.
    def _mock_create_tc_llm():
        tc_llm = MagicMock()
        tc_llm.chat = agent.llm.chat
        tc_llm.total_input_tokens = 0
        tc_llm.total_output_tokens = 0
        tc_llm.total_cost_usd = 0.0
        return tc_llm
    agent._create_tc_llm = _mock_create_tc_llm

    return agent


def _ok_response(tc_id: str, code: str = "test('ok', async ({ page }) => {});") -> MagicMock:
    return MagicMock(content=json.dumps({
        "generated_codes": [{"tc_id": tc_id, "code": code, "self_fix_count": 0, "syntax_valid": True}],
        "confidence": 0.9,
    }))


# ── _build_tc_to_scenario_index ────────────────────────────────────────────


def test_tc_index_slices_single_tc_per_entry():
    scenarios = [{
        "ts_id": "TS-001", "name": "auth", "depends_on": [],
        "affected_files": ["a.py"],
        "test_cases": [
            {"tc_id": "TS-001-TC-01", "name": "정상 로그인", "given": "g", "when": "w", "then": "t"},
            {"tc_id": "TS-001-TC-02", "name": "실패 로그인", "given": "g", "when": "w", "then": "t"},
        ],
    }]
    idx = _build_tc_to_scenario_index(scenarios)
    assert set(idx.keys()) == {"TS-001-TC-01", "TS-001-TC-02"}
    # 각 entry 는 단일 TC 만 포함
    for tc_id, sliced in idx.items():
        assert sliced["ts_id"] == "TS-001"
        assert len(sliced["test_cases"]) == 1
        assert sliced["test_cases"][0]["tc_id"] == tc_id
        # 메타 보존
        assert sliced["affected_files"] == ["a.py"]


def test_tc_index_empty_when_no_scenarios():
    assert _build_tc_to_scenario_index([]) == {}


def test_normalize_mapping_with_frontend_index_prefers_testid_and_label(mock_llm_client):
    agent = _make_agent(mock_llm_client)
    frontend_dom = [
        {
            "tag": "input",
            "text": "",
            "placeholder": "example@email.com",
            "label": "이메일",
            "testid": "email",
            "id": "email",
            "name": "",
            "file": "Signup.vue",
        },
        {
            "tag": "button",
            "text": "가입하기",
            "placeholder": "",
            "label": "",
            "testid": "signup-submit",
            "id": "",
            "name": "",
            "file": "Signup.vue",
        },
    ]
    mapping = {
        "tc_id": "TS-001-TC-01",
        "steps": [
            {"step_no": 1, "action": "fill", "selector": "이메일", "selector_type": "text", "value": "a", "expected": None},
            {"step_no": 2, "action": "click", "selector": "가입하기", "selector_type": "text", "value": None, "expected": None},
        ],
    }

    normalized = agent._normalize_mapping_with_frontend_index(mapping, frontend_dom)
    assert normalized["steps"][0]["selector_type"] == "testid"
    assert normalized["steps"][0]["selector"] == "email"
    assert normalized["steps"][1]["selector_type"] == "text"
    assert normalized["steps"][1]["selector"] == "가입하기"


def test_render_generated_code_uses_normalized_selectors(mock_llm_client):
    agent = _make_agent(mock_llm_client)
    mapping = {
        "tc_id": "TS-001-TC-01",
        "steps": [
            {"step_no": 1, "action": "navigate", "selector": None, "selector_type": None, "value": "/signup", "expected": None},
            {"step_no": 2, "action": "fill", "selector": "email", "selector_type": "testid", "value": "newuser@example.com", "expected": None},
            {"step_no": 3, "action": "fill", "selector": "password", "selector_type": "testid", "value": "plaintext", "expected": None},
            {"step_no": 4, "action": "fill", "selector": "이름", "selector_type": "label", "value": "John Doe", "expected": None},
            {"step_no": 5, "action": "click", "selector": "signup-submit", "selector_type": "testid", "value": None, "expected": None},
            {"step_no": 6, "action": "assert", "selector": "가입이 완료되었습니다! 로그인 페이지로 이동합니다.", "selector_type": "text", "value": None, "expected": None},
        ],
    }
    scenario = {
        "ts_id": "TS-001",
        "test_cases": [{
            "tc_id": "TS-001-TC-01",
            "name": "정상 회원가입",
            "given": "유효한 이메일, 비밀번호, 이름, 생년월일을 제공한 상태에서",
            "when": "회원가입 요청을 하면",
            "then": "회원가입이 성공적으로 완료된다",
        }],
    }

    code = agent._render_generated_code(mapping, scenario)
    assert code is not None
    assert "page.getByTestId(\"email\").fill(\"newuser@example.com\")" in code
    assert "page.getByTestId(\"password\").fill(process.env.E2E_USER_PASSWORD)" in code
    assert "page.getByLabel(\"이름\").fill(\"John Doe\")" in code
    assert "page.getByTestId(\"signup-submit\").click()" in code
    assert "page.getByPlaceholder(\"이메일을 입력하세요\")" not in code
    assert "page.getByText(\"회원가입\").click()" not in code


def test_normalize_mapping_with_target_hints_without_selector(mock_llm_client):
    agent = _make_agent(mock_llm_client)
    frontend_dom = [
        {
            "tag": "input",
            "text": "",
            "placeholder": "example@email.com",
            "label": "이메일",
            "testid": "email",
            "id": "email",
            "name": "",
            "file": "Signup.vue",
            "page": "Signup",
            "route": "/signup",
            "control_type": "form_input",
        },
        {
            "tag": "button",
            "text": "가입하기",
            "placeholder": "",
            "label": "",
            "testid": "signup-submit",
            "id": "",
            "name": "",
            "file": "Signup.vue",
            "page": "Signup",
            "route": "/signup",
            "control_type": "submit",
        },
    ]
    mapping = {
        "tc_id": "TS-001-TC-01",
        "steps": [
            {
                "step_no": 1,
                "action": "navigate",
                "selector": None,
                "selector_type": None,
                "value": "/signup",
                "expected": None,
            },
            {
                "step_no": 2,
                "action": "fill",
                "selector": None,
                "selector_type": None,
                "target_name": "email",
                "target_kind": "field",
                "value": "newuser@example.com",
                "expected": None,
            },
            {
                "step_no": 3,
                "action": "click",
                "selector": None,
                "selector_type": None,
                "target_kind": "submit",
                "value": None,
                "expected": None,
            },
        ],
    }

    normalized = agent._normalize_mapping_with_frontend_index(mapping, frontend_dom)
    assert normalized["steps"][1]["selector_type"] == "testid"
    assert normalized["steps"][1]["selector"] == "email"
    assert normalized["steps"][2]["selector_type"] == "testid"
    assert normalized["steps"][2]["selector"] == "signup-submit"


def test_normalize_mapping_preserves_existing_valid_selector(mock_llm_client):
    agent = _make_agent(mock_llm_client)
    frontend_dom = [
        {
            "tag": "button",
            "text": "로그인",
            "placeholder": "",
            "label": "",
            "testid": "login-submit",
            "id": "",
            "name": "",
            "file": "Login.vue",
            "page": "Login",
            "route": "/login",
            "control_type": "submit",
        },
        {
            "tag": "button",
            "text": "가입하기",
            "placeholder": "",
            "label": "",
            "testid": "signup-submit",
            "id": "",
            "name": "",
            "file": "Signup.vue",
            "page": "Signup",
            "route": "/signup",
            "control_type": "submit",
        },
    ]
    mapping = {
        "tc_id": "TS-001-TC-01",
        "steps": [
            {
                "step_no": 1,
                "action": "navigate",
                "selector": None,
                "selector_type": None,
                "value": "/signup",
                "expected": None,
            },
            {
                "step_no": 2,
                "action": "click",
                "selector": "signup-submit",
                "selector_type": "testid",
                "target_kind": "submit",
                "value": None,
                "expected": None,
            },
        ],
    }

    normalized = agent._normalize_mapping_with_frontend_index(mapping, frontend_dom)
    assert normalized["steps"][1]["selector_type"] == "testid"
    assert normalized["steps"][1]["selector"] == "signup-submit"


def test_normalize_mapping_with_route_and_button_semantics_prefers_signup_submit(mock_llm_client):
    agent = _make_agent(mock_llm_client)
    frontend_dom = [
        {
            "tag": "input",
            "text": "",
            "placeholder": "example@email.com",
            "label": "이메일",
            "testid": "email",
            "id": "email",
            "name": "",
            "file": "system-under-test/frontend/src/pages/Login.vue",
            "page": "Login",
            "route": "/login",
            "control_type": "form_input",
        },
        {
            "tag": "button",
            "text": "가입하기",
            "placeholder": "",
            "label": "",
            "testid": "signup-submit",
            "id": "",
            "name": "",
            "file": "system-under-test/frontend/src/pages/Signup.vue",
            "page": "Signup",
            "route": "/signup",
            "control_type": "submit",
        },
    ]
    mapping = {
        "tc_id": "TS-001-TC-01",
        "steps": [
            {"step_no": 1, "action": "navigate", "selector": None, "selector_type": None, "value": "/signup", "expected": None},
            {"step_no": 2, "action": "click", "selector": "회원가입 버튼", "selector_type": "text", "value": None, "expected": None},
        ],
    }

    normalized = agent._normalize_mapping_with_frontend_index(mapping, frontend_dom)
    assert normalized["steps"][1]["selector_type"] == "testid"
    assert normalized["steps"][1]["selector"] == "signup-submit"


@pytest.mark.asyncio
async def test_execute_prefers_deterministic_render_for_mapped_steps(mock_llm_client):
    agent = _make_agent(mock_llm_client)
    agent.llm.chat = AsyncMock(side_effect=AssertionError("LLM should not be called for mapped steps"))
    mapping = {
        "tc_id": "TS-001-TC-01",
        "steps": [
            {"step_no": 1, "action": "navigate", "selector": None, "selector_type": None, "value": "/signup", "expected": None},
            {"step_no": 2, "action": "fill", "selector": "email", "selector_type": "testid", "value": "newuser@example.com", "expected": None},
            {"step_no": 3, "action": "click", "selector": "signup-submit", "selector_type": "testid", "value": None, "expected": None},
        ],
    }
    scenarios = [{
        "ts_id": "TS-001",
        "test_cases": [{"tc_id": "TS-001-TC-01", "name": "정상 회원가입"}],
    }]

    result = await agent._execute({"action_mappings": [mapping], "scenarios": scenarios}, {})
    code = result.result["generated_codes"][0]["code"]
    assert "page.getByTestId(\"email\").fill(\"newuser@example.com\")" in code
    assert "page.getByTestId(\"signup-submit\").click()" in code


@pytest.mark.asyncio
async def test_execute_does_not_reload_or_remap_frontend_index(mock_llm_client):
    agent = _make_agent(mock_llm_client)
    agent.llm.chat = AsyncMock(side_effect=AssertionError("LLM should not be called for mapped steps"))
    mapping = {
        "tc_id": "TS-001-TC-01",
        "steps": [
            {"step_no": 1, "action": "navigate", "selector": None, "selector_type": None, "value": "/signup", "expected": None},
            {"step_no": 2, "action": "click", "selector": "signup-submit", "selector_type": "testid", "value": None, "expected": None, "target_kind": "submit"},
        ],
    }
    scenarios = [{
        "ts_id": "TS-001",
        "test_cases": [{"tc_id": "TS-001-TC-01", "name": "정상 회원가입"}],
    }]

    with patch.object(agent, "_load_frontend_dom", side_effect=AssertionError("frontend_dom should not be loaded")), \
         patch.object(agent, "_normalize_mapping_with_frontend_index", side_effect=AssertionError("mapping should not be remapped")):
        result = await agent._execute(
            {"action_mappings": [mapping], "scenarios": scenarios, "frontend_dom": [{"testid": "login-submit"}]},
            {},
        )

    code = result.result["generated_codes"][0]["code"]
    assert "page.goto(\"/signup\")" in code
    assert "page.getByTestId(\"signup-submit\").click()" in code


# ── _execute graceful 동작 ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_all_success(mock_llm_client):
    """3 TC 모두 LLM 응답 정상 → generated_codes 3건 / failed_tcs 0건."""
    agent = _make_agent(mock_llm_client)
    agent.llm.chat = AsyncMock(side_effect=[
        _ok_response("TS-001-TC-01"),
        _ok_response("TS-001-TC-02"),
        _ok_response("TS-001-TC-03"),
    ])

    action_mappings = [
        {"tc_id": "TS-001-TC-01", "actions": []},
        {"tc_id": "TS-001-TC-02", "actions": []},
        {"tc_id": "TS-001-TC-03", "actions": []},
    ]
    scenarios = [{
        "ts_id": "TS-001", "test_cases": [
            {"tc_id": "TS-001-TC-01"}, {"tc_id": "TS-001-TC-02"}, {"tc_id": "TS-001-TC-03"},
        ],
    }]

    result = await agent._execute({"action_mappings": action_mappings, "scenarios": scenarios}, {})
    assert len(result.result["generated_codes"]) == 3
    assert result.result["failed_tcs"] == []
    assert result.confidence == 1.0


@pytest.mark.asyncio
async def test_execute_partial_failure_skips_only_failed_tc(mock_llm_client):
    """3 TC 중 가운데 TC LLM JSON parse 실패 → 나머지 2 TC 정상."""
    agent = _make_agent(mock_llm_client)
    agent.llm.chat = AsyncMock(side_effect=[
        _ok_response("TS-001-TC-01"),
        MagicMock(content='{"generated_codes": [{invalid \\escape'),  # JSON parse 실패
        _ok_response("TS-001-TC-03"),
    ])

    action_mappings = [
        {"tc_id": "TS-001-TC-01"}, {"tc_id": "TS-001-TC-02"}, {"tc_id": "TS-001-TC-03"},
    ]
    result = await agent._execute({"action_mappings": action_mappings, "scenarios": []}, {})

    generated = result.result["generated_codes"]
    failed = result.result["failed_tcs"]
    assert len(generated) == 2
    assert sorted(c["tc_id"] for c in generated) == ["TS-001-TC-01", "TS-001-TC-03"]
    assert len(failed) == 1
    assert failed[0]["tc_id"] == "TS-001-TC-02"
    assert "JSONDecodeError" in failed[0]["error_type"]
    assert abs(result.confidence - 2/3) < 0.01


@pytest.mark.asyncio
async def test_execute_all_failure_returns_empty_with_failed_tcs(mock_llm_client):
    """모든 TC 가 JSON parse 실패 → generated 0건 / failed N건. confidence=0."""
    agent = _make_agent(mock_llm_client)
    agent.llm.chat = AsyncMock(side_effect=[
        MagicMock(content="invalid"),
        MagicMock(content="also invalid"),
    ])
    action_mappings = [{"tc_id": "TC-A"}, {"tc_id": "TC-B"}]
    result = await agent._execute({"action_mappings": action_mappings, "scenarios": []}, {})

    assert result.result["generated_codes"] == []
    assert len(result.result["failed_tcs"]) == 2
    assert result.confidence == 0.0


@pytest.mark.asyncio
async def test_execute_empty_input_returns_clean_result(mock_llm_client):
    """ActionMapping 비어있으면 LLM 호출 없이 정상 종료."""
    agent = _make_agent(mock_llm_client)
    agent.llm.chat = AsyncMock(side_effect=AssertionError("LLM 호출 되면 안 됨"))
    result = await agent._execute({"action_mappings": [], "scenarios": []}, {})
    assert result.result["generated_codes"] == []
    assert result.result["failed_tcs"] == []
    assert result.confidence == 1.0
    agent.llm.chat.assert_not_called()


@pytest.mark.asyncio
async def test_execute_empty_generated_codes_response_creates_stub(mock_llm_client):
    """LLM 이 generated_codes=[] 만 반환해도 stub 결과 (code='') 반환 — pipeline 진행."""
    agent = _make_agent(mock_llm_client)
    agent.llm.chat = AsyncMock(return_value=MagicMock(content='{"generated_codes": [], "confidence": 0}'))
    action_mappings = [{"tc_id": "TC-X"}]
    result = await agent._execute({"action_mappings": action_mappings, "scenarios": []}, {})
    assert len(result.result["generated_codes"]) == 1
    code_obj = result.result["generated_codes"][0]
    assert code_obj["tc_id"] == "TC-X"
    assert code_obj["code"] == ""
    assert code_obj["syntax_valid"] is False


@pytest.mark.asyncio
async def test_execute_concurrency_respects_semaphore(mock_llm_client):
    """동시 LLM 호출이 _MAX_CONCURRENT_LLM_CALLS 초과하지 않음."""
    from qapilot.agents.code_generator_agent import _MAX_CONCURRENT_LLM_CALLS

    agent = _make_agent(mock_llm_client)
    in_flight = 0
    max_in_flight = 0

    async def fake_chat(*args, **kwargs):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        import asyncio
        await asyncio.sleep(0.05)
        in_flight -= 1
        return _ok_response("TC")

    agent.llm.chat = AsyncMock(side_effect=fake_chat)
    action_mappings = [{"tc_id": f"TC-{i}"} for i in range(20)]
    await agent._execute({"action_mappings": action_mappings, "scenarios": []}, {})
    assert max_in_flight <= _MAX_CONCURRENT_LLM_CALLS, f"동시 호출 {max_in_flight} > 한도 {_MAX_CONCURRENT_LLM_CALLS}"


# ── 비즈니스 의도 보존 (scenario slice) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_passes_scenario_slice_to_prompt(mock_llm_client):
    """각 TC LLM 호출에 해당 TC 의 scenario slice 만 전달 (다른 TC 의 비즈니스 의도 누락 X)."""
    agent = _make_agent(mock_llm_client)
    agent.llm.chat = AsyncMock(side_effect=[
        _ok_response("TC-A"), _ok_response("TC-B"),
    ])
    scenarios = [{
        "ts_id": "TS-1",
        "test_cases": [
            {"tc_id": "TC-A", "name": "A 시나리오", "given": "GA", "when": "WA", "then": "TA"},
            {"tc_id": "TC-B", "name": "B 시나리오", "given": "GB", "when": "WB", "then": "TB"},
        ],
    }]
    action_mappings = [{"tc_id": "TC-A"}, {"tc_id": "TC-B"}]

    await agent._execute({"action_mappings": action_mappings, "scenarios": scenarios}, {})

    # prompts.render 가 호출된 두 번의 scenarios 인자 검증
    calls = agent.prompts.render.call_args_list
    assert len(calls) == 2
    rendered_scenarios = [json.loads(c.kwargs["scenarios"]) for c in calls]

    # 각 호출이 단일 TC 만 포함
    for sliced in rendered_scenarios:
        assert len(sliced) == 1
        assert len(sliced[0]["test_cases"]) == 1

    # 호출별 TC id 매칭
    tc_ids_in_calls = [s[0]["test_cases"][0]["tc_id"] for s in rendered_scenarios]
    assert sorted(tc_ids_in_calls) == ["TC-A", "TC-B"]
    assert all("frontend_dom" in c.kwargs for c in calls)


# ── 이슈 #140: per-TC LLMClient 분리 검증 ─────────────────────────────────


@pytest.mark.asyncio
async def test_per_tc_llm_client_created_for_each_tc_codegen(mock_llm_client):
    """이슈 #140: CodeGenerator 도 ActionMapper 와 동일한 per-TC LLMClient 분리."""
    agent = _make_agent(mock_llm_client)
    agent.llm.chat = AsyncMock(side_effect=[
        _ok_response("TC-A"), _ok_response("TC-B"), _ok_response("TC-C"),
    ])
    call_count = {"n": 0}
    original = agent._create_tc_llm
    def _counting_create():
        call_count["n"] += 1
        return original()
    agent._create_tc_llm = _counting_create

    action_mappings = [{"tc_id": "TC-A"}, {"tc_id": "TC-B"}, {"tc_id": "TC-C"}]
    await agent._execute({"action_mappings": action_mappings, "scenarios": []}, {})

    assert call_count["n"] == 3


@pytest.mark.asyncio
async def test_per_tc_llm_accumulates_to_agent_llm_for_reporting_codegen(mock_llm_client):
    """CodeGenerator per-TC LLMClient 의 토큰/비용 누적 합산 검증."""
    agent = _make_agent(mock_llm_client)
    agent.llm.chat = AsyncMock(return_value=_ok_response("TC-A"))
    def _mock_create_tc_llm_with_usage():
        tc_llm = MagicMock()
        tc_llm.chat = agent.llm.chat
        tc_llm.total_input_tokens = 500
        tc_llm.total_output_tokens = 100
        tc_llm.total_cost_usd = 0.0005
        return tc_llm
    agent._create_tc_llm = _mock_create_tc_llm_with_usage

    action_mappings = [{"tc_id": "TC-A"}, {"tc_id": "TC-B"}]
    await agent._execute({"action_mappings": action_mappings, "scenarios": []}, {})

    # 2 TC × (500, 100, 0.0005) = (1000, 200, 0.001)
    assert agent.llm.total_input_tokens == 1000
    assert agent.llm.total_output_tokens == 200
    assert agent.llm.total_cost_usd == 0.001
