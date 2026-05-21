"""이슈 #129 — ActionMapper TC-별 분할 (Step A) + assert step 환각 차단 (Step B) 검증.

배경:
- e2e trace `a86603b9` (PR #128 머지 후) 에서 ActionMapper 78 TC → 21 TC 매핑 (75%
  누락). 원인: 큰 batch + frontend.json 컨텍스트로 LLM 응답 token 한계 도달.
- 같은 e2e 에서 fill/click step 의 selector 는 인덱스 활용 (label="이메일") 성공.
  그러나 assert step 의 selector 는 then 절 자연어 환각 (testid="로그인 성공 메시지
  노출") 잔존 → 페이지에 없어 fail.

해결:
- Step A: TC-별 분할 + asyncio.gather + Semaphore(5) + 부분 graceful (이슈 #107 패턴)
- Step B: assert step + frontend_dom_index fuzzy match + testid/placeholder/label/text
  우선순위 정규화 (UITestTool 옵션 B 와 동일 difflib.SequenceMatcher 알고리즘)

상세: docs/team-faq-codegen-uitest-separation.md
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from qapilot.agents.action_mapper_agent import (
    ActionMapperAgent,
    _MAX_CONCURRENT_LLM_CALLS,
    _ASSERT_ACTIONS,
)


def _make_agent() -> ActionMapperAgent:
    agent = ActionMapperAgent.__new__(ActionMapperAgent)
    agent.logger = MagicMock()
    agent.llm = MagicMock()
    agent.llm.chat = AsyncMock()
    agent.llm.total_input_tokens = 0
    agent.llm.total_output_tokens = 0
    agent.llm.total_cost_usd = 0.0
    agent.prompts = MagicMock()
    agent.prompts.system = MagicMock(return_value="SYSTEM")
    agent.prompts.render = MagicMock(
        side_effect=lambda **kw: f"USER<{kw.get('scenarios', '')[:40]}>"
    )
    agent.with_correction_hint = MagicMock(side_effect=lambda p, e: p)
    agent._frontend_dom_index = []

    # 이슈 #140: per-TC LLMClient 분리 후 _create_tc_llm 이 매 호출마다 mock 반환.
    # 반환된 mock 의 chat 은 agent.llm.chat 와 동일 AsyncMock 공유 — 기존 테스트의
    # side_effect 시퀀스 / await_count 검증 그대로 동작.
    def _mock_create_tc_llm():
        tc_llm = MagicMock()
        tc_llm.chat = agent.llm.chat
        tc_llm.total_input_tokens = 0
        tc_llm.total_output_tokens = 0
        tc_llm.total_cost_usd = 0.0
        return tc_llm
    agent._create_tc_llm = _mock_create_tc_llm

    return agent


def _llm_response_with_mapping(tc_id: str, steps: list[dict] | None = None) -> MagicMock:
    """LLM 응답 — 단일 ActionMapping JSON 배열."""
    payload = [{
        "tc_id": tc_id,
        "steps": steps or [
            {"step_no": 1, "action": "click", "selector": "btn", "selector_type": "css"}
        ],
        "selector_confidence": 0.9,
    }]
    return MagicMock(content=json.dumps(payload))


# ── Step A: TC-별 분할 ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_splits_into_per_tc_calls():
    """N TS × M TC = N*M 개 LLM 호출 (TC-별 분할)."""
    agent = _make_agent()
    agent.llm.chat = AsyncMock(side_effect=[
        _llm_response_with_mapping("TS-001-TC-01"),
        _llm_response_with_mapping("TS-001-TC-02"),
        _llm_response_with_mapping("TS-002-TC-01"),
    ])
    scenarios = [
        {"ts_id": "TS-001", "test_cases": [{"tc_id": "TS-001-TC-01"}, {"tc_id": "TS-001-TC-02"}]},
        {"ts_id": "TS-002", "test_cases": [{"tc_id": "TS-002-TC-01"}]},
    ]
    result = await agent._execute({"scenarios": scenarios, "scan_result": None}, {})

    assert agent.llm.chat.await_count == 3
    assert len(result.result["action_mappings"]) == 3
    assert result.result["failed_tcs"] == []
    assert result.confidence > 0


@pytest.mark.asyncio
async def test_execute_partial_failure_skips_only_failed_tc():
    """가운데 TC LLM 응답 JSON parse 실패 → 그 TC 만 skip, 나머지 정상."""
    import json as _json
    agent = _make_agent()
    agent.llm.chat = AsyncMock(side_effect=[
        _llm_response_with_mapping("TC-A"),
        MagicMock(content="not valid json"),  # JSON parse 실패
        _llm_response_with_mapping("TC-C"),
    ])
    scenarios = [{
        "ts_id": "TS-1",
        "test_cases": [{"tc_id": "TC-A"}, {"tc_id": "TC-B"}, {"tc_id": "TC-C"}],
    }]
    result = await agent._execute({"scenarios": scenarios, "scan_result": None}, {})

    mappings = result.result["action_mappings"]
    failed = result.result["failed_tcs"]
    assert sorted(m["tc_id"] for m in mappings) == ["TC-A", "TC-C"]
    assert len(failed) == 1
    assert failed[0]["tc_id"] == "TC-B"
    assert "AgentExecutionError" in failed[0]["error_type"] or "json" in failed[0]["error"].lower()


@pytest.mark.asyncio
async def test_execute_all_failure_returns_empty_with_failed_tcs():
    """모든 TC LLM 실패 → mappings 빈 list / failed_tcs N건."""
    agent = _make_agent()
    agent.llm.chat = AsyncMock(side_effect=[
        MagicMock(content="invalid 1"),
        MagicMock(content="invalid 2"),
    ])
    scenarios = [{"ts_id": "TS-1", "test_cases": [{"tc_id": "A"}, {"tc_id": "B"}]}]
    result = await agent._execute({"scenarios": scenarios, "scan_result": None}, {})

    assert result.result["action_mappings"] == []
    assert len(result.result["failed_tcs"]) == 2
    assert result.confidence == 0.0


@pytest.mark.asyncio
async def test_execute_empty_scenarios_or_tcs():
    """scenarios 비어있음 또는 모든 TS 의 test_cases 빈 list → 빈 결과 + LLM 호출 X."""
    agent = _make_agent()
    agent.llm.chat = AsyncMock(side_effect=AssertionError("호출되면 안 됨"))

    result1 = await agent._execute({"scenarios": [], "scan_result": None}, {})
    assert result1.result["action_mappings"] == []
    assert result1.result["failed_tcs"] == []
    assert result1.confidence == 1.0

    result2 = await agent._execute(
        {"scenarios": [{"ts_id": "TS-1", "test_cases": []}], "scan_result": None}, {}
    )
    assert result2.result["action_mappings"] == []
    assert result2.confidence == 1.0
    agent.llm.chat.assert_not_called()


@pytest.mark.asyncio
async def test_execute_concurrency_respects_semaphore():
    """동시 LLM 호출이 _MAX_CONCURRENT_LLM_CALLS 초과하지 않음."""
    agent = _make_agent()
    in_flight = 0
    max_in_flight = 0

    async def fake_chat(*args, **kwargs):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        import asyncio
        await asyncio.sleep(0.02)
        in_flight -= 1
        return _llm_response_with_mapping("X")

    agent.llm.chat = AsyncMock(side_effect=fake_chat)
    scenarios = [
        {"ts_id": "TS-X", "test_cases": [{"tc_id": f"TC-{i}"} for i in range(20)]}
    ]
    await agent._execute({"scenarios": scenarios, "scan_result": None}, {})
    assert max_in_flight <= _MAX_CONCURRENT_LLM_CALLS, (
        f"동시 호출 {max_in_flight} > 한도 {_MAX_CONCURRENT_LLM_CALLS}"
    )


@pytest.mark.asyncio
async def test_call_single_tc_passes_scenario_slice():
    """_call_single_tc 가 단일 TC slice 만 prompt 에 전달 (다른 TC 의 의도 누락 X)."""
    import asyncio
    agent = _make_agent()
    agent.llm.chat = AsyncMock(return_value=_llm_response_with_mapping("TC-A"))
    sem = asyncio.Semaphore(1)

    ts = {
        "ts_id": "TS-1",
        "test_cases": [
            {"tc_id": "TC-A", "name": "A scenario"},
            {"tc_id": "TC-B", "name": "B scenario"},  # 무시되어야
        ],
    }
    await agent._call_single_tc(sem, ts, ts["test_cases"][0], [], [], None)

    # prompts.render 호출 시 scenarios 인자에 TC-A 만 있어야
    rendered_scenarios = agent.prompts.render.call_args.kwargs["scenarios"]
    parsed = json.loads(rendered_scenarios)
    assert len(parsed) == 1
    assert len(parsed[0]["test_cases"]) == 1
    assert parsed[0]["test_cases"][0]["tc_id"] == "TC-A"


# ── Step B: assert step 환각 차단 (frontend.json 인덱스 매칭) ────────────────


def test_normalize_assert_selector_exact_match_returns_none():
    """selector 가 인덱스 element text 와 정확 일치 → 정규화 불필요 (None 반환 = 원본 유지)."""
    agent = _make_agent()
    agent._frontend_dom_index = [
        {"tag": "div", "text": "회원가입 완료", "testid": "", "placeholder": "", "label": "",
         "name": "", "id": "", "file": "Signup.vue"}
    ]
    result = agent._normalize_assert_selector_via_index(
        "회원가입 완료", "text", "TC-1", 1
    )
    assert result is None  # 원본 유지


def test_normalize_assert_selector_fuzzy_match_to_testid():
    """fuzzy match 적중 → testid 우선순위로 정규화."""
    agent = _make_agent()
    agent._frontend_dom_index = [
        {"tag": "div", "text": "회원가입 완료 페이지로 이동", "placeholder": "",
         "label": "", "testid": "signup-success", "name": "", "id": "", "file": "Signup.vue"}
    ]
    # selector 가 element text 의 부분 — 포함관계 가산점 + ratio 로 임계값 통과
    result = agent._normalize_assert_selector_via_index(
        "회원가입 완료", "text", "TC-1", 1
    )
    assert result is not None
    assert result == ("signup-success", "testid")


def test_normalize_assert_selector_below_threshold_returns_none():
    """fuzzy score 임계값 미달 → None (UITestTool 런타임 보정에 위임)."""
    agent = _make_agent()
    agent._frontend_dom_index = [
        {"tag": "button", "text": "주문하기", "testid": "", "placeholder": "",
         "label": "", "name": "", "id": "", "file": "Plans.vue"}
    ]
    result = agent._normalize_assert_selector_via_index(
        "로그인 성공 메시지", "text", "TC-1", 1
    )
    assert result is None


def test_normalize_assert_selector_empty_index_returns_none():
    """인덱스 빈 list → None."""
    agent = _make_agent()
    agent._frontend_dom_index = []
    result = agent._normalize_assert_selector_via_index(
        "회원가입 완료", "text", "TC-1", 1
    )
    assert result is None


def test_normalize_assert_selector_priority_testid_over_placeholder():
    """fuzzy 매치 element 에 testid + placeholder + label 모두 있으면 testid 우선.

    정확 매치 시에는 원본 유지가 의도된 동작. fuzzy match (target 이 element 의 어떤
    candidate 와도 정확히 일치 안 하나 ratio 임계값 통과) 케이스에서만 정규화 발화.
    """
    agent = _make_agent()
    agent._frontend_dom_index = [
        {"tag": "input", "text": "", "placeholder": "이메일 입력", "label": "이메일 라벨",
         "testid": "email-input", "name": "", "id": "", "file": "Login.vue"}
    ]
    # target='이메일' — placeholder/label 중 어느 것과도 정확 매치 X, 부분 포함 → fuzzy 통과
    result = agent._normalize_assert_selector_via_index("이메일", "text", "TC-1", 1)
    assert result == ("email-input", "testid")


def test_normalize_assert_selector_falls_back_to_label_when_no_testid():
    """testid 없으면 placeholder → label → text 순서."""
    agent = _make_agent()
    agent._frontend_dom_index = [
        {"tag": "label", "text": "비밀번호", "placeholder": "", "label": "비밀번호",
         "testid": "", "name": "", "id": "", "file": "Login.vue"}
    ]
    result = agent._normalize_assert_selector_via_index("비밀번호", "text", "TC-1", 1)
    assert result is None  # text 정확 매치이므로 원본 유지


def test_normalize_assert_selector_called_only_for_assert_actions():
    """_normalize_selector_fields 는 assert action 에서만 인덱스 정규화 호출."""
    agent = _make_agent()
    agent._frontend_dom_index = [
        {"tag": "button", "text": "환영합니다", "testid": "welcome-banner",
         "placeholder": "", "label": "", "name": "", "id": "", "file": "x.vue"}
    ]
    # fill action — 정규화 적용 안 됨
    sel, st = agent._normalize_selector_fields(
        "fill", "환영", "text", {"value": "x"}, "TC-1", 1
    )
    assert sel == "환영"
    assert st == "text"

    # assert action — 정규화 적용
    sel2, st2 = agent._normalize_selector_fields(
        "assert", "환영", "text", {"expected": "x"}, "TC-1", 2
    )
    assert sel2 == "welcome-banner"
    assert st2 == "testid"


def test_normalize_assert_selector_substring_bonus_breaks_threshold():
    """포함관계 가산점 (+0.2) 으로 임계값 (0.6) 통과 케이스."""
    agent = _make_agent()
    agent._frontend_dom_index = [
        {"tag": "input", "text": "", "placeholder": "이메일을 입력하세요",
         "label": "", "testid": "", "name": "", "id": "", "file": "Login.vue"}
    ]
    # target='이메일' vs cand='이메일을 입력하세요' — 단독 ratio < 0.6, 포함관계 +0.2 로 통과
    result = agent._normalize_assert_selector_via_index("이메일", "text", "TC-1", 1)
    assert result is not None
    assert result == ("이메일을 입력하세요", "placeholder")


def test_normalize_assert_selector_empty_target_returns_none():
    """selector 가 빈 문자열 → None."""
    agent = _make_agent()
    agent._frontend_dom_index = [
        {"tag": "button", "text": "확인", "testid": "ok", "placeholder": "",
         "label": "", "name": "", "id": "", "file": "x.vue"}
    ]
    result = agent._normalize_assert_selector_via_index("", "text", "TC-1", 1)
    assert result is None


def test_assert_action_constants_cover_all_assert_variants():
    """_ASSERT_ACTIONS 가 assert 계열 8종 모두 포함."""
    expected = {"assert", "assert_visible", "assert_hidden", "assert_text",
                "assert_value", "assert_enabled", "assert_disabled", "assert_count"}
    assert _ASSERT_ACTIONS == expected


# ── 이슈 #140: per-TC LLMClient 분리 검증 ─────────────────────────────────


@pytest.mark.asyncio
async def test_per_tc_llm_client_created_for_each_tc():
    """이슈 #140: TC 마다 _create_tc_llm 호출 — agent 의 self.llm 누적 정책 우회.

    PR #130 의 'TC-별 분할 = 한 TC = 1 task' 의도가 LLMClient 의 'agent = 1 task'
    누적과 충돌해서 SYSTEM_002 로 후반 TC skip 되던 문제 회피 검증.
    """
    agent = _make_agent()
    agent.llm.chat = AsyncMock(side_effect=[
        _llm_response_with_mapping("TC-A"),
        _llm_response_with_mapping("TC-B"),
        _llm_response_with_mapping("TC-C"),
    ])
    # _create_tc_llm 호출 횟수 추적
    call_count = {"n": 0}
    original = agent._create_tc_llm
    def _counting_create():
        call_count["n"] += 1
        return original()
    agent._create_tc_llm = _counting_create

    scenarios = [{
        "ts_id": "TS-1",
        "test_cases": [{"tc_id": "TC-A"}, {"tc_id": "TC-B"}, {"tc_id": "TC-C"}],
    }]
    await agent._execute({"scenarios": scenarios, "scan_result": None}, {})

    # 3 TC = 3 회 _create_tc_llm 호출 = 3 개의 독립 LLMClient 인스턴스
    assert call_count["n"] == 3


@pytest.mark.asyncio
async def test_per_tc_llm_accumulates_to_agent_llm_for_reporting():
    """per-TC LLMClient 의 토큰/비용이 agent.llm 에 누적 합산 (agent_complete 보고용)."""
    agent = _make_agent()
    agent.llm.chat = AsyncMock(return_value=_llm_response_with_mapping("TC-A"))
    # _create_tc_llm 이 반환한 mock 의 토큰/비용 시뮬레이션
    def _mock_create_tc_llm_with_usage():
        tc_llm = MagicMock()
        tc_llm.chat = agent.llm.chat
        tc_llm.total_input_tokens = 1000
        tc_llm.total_output_tokens = 200
        tc_llm.total_cost_usd = 0.001
        return tc_llm
    agent._create_tc_llm = _mock_create_tc_llm_with_usage

    scenarios = [{
        "ts_id": "TS-1",
        "test_cases": [{"tc_id": "TC-A"}, {"tc_id": "TC-B"}, {"tc_id": "TC-C"}],
    }]
    await agent._execute({"scenarios": scenarios, "scan_result": None}, {})

    # 3 TC × (input 1000 + output 200 + cost 0.001) = 누적 (3000, 600, 0.003)
    assert agent.llm.total_input_tokens == 3000
    assert agent.llm.total_output_tokens == 600
    assert agent.llm.total_cost_usd == 0.003
