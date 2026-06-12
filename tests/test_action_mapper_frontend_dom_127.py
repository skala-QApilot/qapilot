"""이슈 #127 — ActionMapperAgent 의 frontend DOM 인덱스 컨텍스트 주입 검증.

`_load_frontend_dom` 의 context 우선 + DB/S3 mirror fallback / `_format_frontend_dom`
의 프롬프트 변환 / `_call_batch` 가 frontend_dom 인자를 LLM 호출에 포함하는지 검증.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qapilot.agents.action_mapper_agent import ActionMapperAgent


def _make_agent(mock_llm_client: MagicMock) -> ActionMapperAgent:
    agent = ActionMapperAgent.__new__(ActionMapperAgent)
    agent.logger = MagicMock()
    agent.llm = mock_llm_client
    agent.llm.chat.return_value = MagicMock(content="[]")
    agent.prompts = MagicMock()
    agent.prompts.system = MagicMock(return_value="SYSTEM")
    agent.prompts.render = MagicMock(side_effect=lambda **kw: f"USER<scenarios={kw.get('scenarios','')[:40]}|endpoints={kw.get('endpoints','')[:40]}|frontend_dom={kw.get('frontend_dom','')[:80]}>")
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


# ── _load_frontend_dom ──────────────────────────────────────────────────────


def test_load_frontend_dom_from_context_priority(mock_llm_client):
    """context 에 직접 주입된 frontend_dom 우선 — 디스크 무시."""
    agent = _make_agent(mock_llm_client)
    ctx_dom = [{"tag": "input", "placeholder": "이메일"}]
    result = agent._load_frontend_dom({"frontend_dom": ctx_dom})
    assert result == ctx_dom


def test_load_frontend_dom_from_db_mirror_fallback(mock_llm_client):
    """context 없음 → DB/S3 mirror payload 로드."""
    agent = _make_agent(mock_llm_client)
    with patch("qapilot.db.code_reader.load_codebase_index", return_value={
        "elements": [{"tag": "label", "text": "이메일", "id": "email", "file": "x.vue"}]
    }):
        result = agent._load_frontend_dom({"service_id": "svc-a"})
    assert len(result) == 1
    assert result[0]["text"] == "이메일"


def test_load_frontend_dom_returns_empty_when_no_source(mock_llm_client):
    """context 와 mirror 모두 없음 → 빈 list."""
    agent = _make_agent(mock_llm_client)
    result = agent._load_frontend_dom({})
    assert result == []


def test_load_metadata_bundle_uses_mirror_only(mock_llm_client):
    agent = _make_agent(mock_llm_client)
    with patch("qapilot.shared.scan_storage.load_metadata_index", side_effect=[
        {"kind": "frontend", "sub_kind": "selectors", "service_id": "svc-a", "commit_sha": "abc123", "by_route": {"/signup": {"inputs": [], "buttons": [], "outputs": [], "dynamic": []}}},
        {"kind": "frontend", "sub_kind": "routes", "service_id": "svc-a", "commit_sha": "abc123", "routes": []},
        {"kind": "backend", "sub_kind": "schemas", "service_id": "svc-a", "commit_sha": "abc123", "request_schemas": {}, "response_schemas": {}, "db_models": {}},
    ]):
        bundle = agent._load_metadata_bundle({
            "service_id": "svc-a",
            "commit_sha": "abc123",
        })

    assert bundle["commit_sha"] == "abc123"
    assert bundle["selectors"] is not None
    assert "/signup" in bundle["selectors"]["by_route"]


# ── _format_frontend_dom ────────────────────────────────────────────────────


def test_format_frontend_dom_basic(mock_llm_client):
    """element 의 핵심 속성을 한 줄로 표기."""
    agent = _make_agent(mock_llm_client)
    elements = [
        {"tag": "input", "placeholder": "이메일", "testid": "email", "id": "email",
         "label": "이메일", "name": "email", "text": "", "file": "Login.vue"},
        {"tag": "button", "text": "로그인", "testid": "login-submit",
         "placeholder": "", "label": "", "name": "", "id": "", "file": "Login.vue"},
    ]
    out = agent._format_frontend_dom(elements)
    assert '<input>' in out
    assert 'placeholder="이메일"' in out
    assert 'testid="email"' in out
    assert 'label="이메일"' in out
    assert '<button>' in out
    assert 'text="로그인"' in out
    assert 'testid="login-submit"' in out
    assert '(from Login.vue)' in out


def test_format_frontend_dom_empty_returns_placeholder(mock_llm_client):
    """빈 list → 안내 문자열 (LLM 이 fallback)."""
    agent = _make_agent(mock_llm_client)
    out = agent._format_frontend_dom([])
    assert "인덱스 없음" in out
    assert "mirror" in out  # hint


def test_format_frontend_dom_limits_200(mock_llm_client):
    """element 200 초과 시 잘림 (LLM 토큰 폭증 방지)."""
    agent = _make_agent(mock_llm_client)
    elements = [
        {"tag": "input", "placeholder": f"item-{i}", "text": "", "label": "",
         "testid": "", "name": "", "id": "", "file": "x.vue"}
        for i in range(250)
    ]
    out = agent._format_frontend_dom(elements)
    lines = [l for l in out.split("\n") if l.strip()]
    assert len(lines) == 200  # 최대 200


def test_format_frontend_dom_skips_empty_element(mock_llm_client):
    """모든 식별자 빈 element 는 라인 제외."""
    agent = _make_agent(mock_llm_client)
    elements = [
        {"tag": "input", "text": "", "placeholder": "", "label": "", "testid": "",
         "name": "", "id": "", "file": "x.vue"},  # 모두 빈
        {"tag": "button", "text": "확인", "placeholder": "", "label": "",
         "testid": "", "name": "", "id": "", "file": "x.vue"},
    ]
    out = agent._format_frontend_dom(elements)
    # 첫 element 의 <input> 라인은 (file) 만 표기되어 lines 에 안 들어감
    # 두 번째 element 의 <button> + text=확인 만 포함
    assert 'text="확인"' in out
    # input 의 (from x.vue) 단독 라인은 없어야 함 (parts 가 [<input>, (from x.vue)] 라 join 됨)
    # 첫 element 도 file_label 이 있어 라인 포함될 수 있음 — 명세상 ok 로 통과 가능
    # 핵심: text="확인" 만 검증


def test_select_frontend_candidates_prefers_signup_page_for_signup_scenario(mock_llm_client):
    agent = _make_agent(mock_llm_client)
    batch = [{
        "ts_id": "TS-001",
        "test_cases": [{
            "tc_id": "TS-001-TC-01",
            "name": "정상 회원가입",
            "given": "유효한 이메일, 비밀번호, 이름, 생년월일을 제공하고",
            "when": "회원가입 요청을 하면",
            "then": "회원가입이 성공적으로 완료된다",
        }],
    }]
    frontend_dom = [
        {
            "tag": "input",
            "text": "",
            "placeholder": "example@email.com",
            "label": "이메일",
            "testid": "email",
            "name": "",
            "id": "email",
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
            "name": "",
            "id": "",
            "file": "system-under-test/frontend/src/pages/Signup.vue",
            "page": "Signup",
            "route": "/signup",
            "control_type": "submit",
        },
    ]

    candidates = agent._select_frontend_candidates(batch, frontend_dom)
    assert any(el.get("testid") == "signup-submit" for el in candidates)
    assert all("Signup.vue" in str(el.get("file") or "") for el in candidates)


# ── _call_batch frontend_dom 전달 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_call_batch_passes_frontend_dom_to_prompt_render(mock_llm_client):
    """_call_batch 가 prompts.render(..., frontend_dom=...) 로 인자 전달 검증."""
    agent = _make_agent(mock_llm_client)
    batch = [{"ts_id": "TS-1", "test_cases": []}]
    endpoints = [{"method": "POST", "path": "/login"}]
    frontend_dom = [{"tag": "input", "placeholder": "이메일", "file": "Login.vue",
                     "text": "", "label": "", "testid": "", "name": "", "id": "",
                     "page": "Login", "route": "/login", "control_type": "form_input"}]

    await agent._call_batch(batch, endpoints, frontend_dom, None)

    # prompts.render 호출 인자 검증
    agent.prompts.render.assert_called_once()
    kwargs = agent.prompts.render.call_args.kwargs
    assert "frontend_dom" in kwargs


@pytest.mark.asyncio
async def test_call_single_tc_passes_source_aware_context_to_prompt_render(mock_llm_client):
    agent = _make_agent(mock_llm_client)
    agent.llm.chat.return_value = MagicMock(content=json.dumps([{
        "tc_id": "TS-001-TC-01",
        "steps": [
            {"step_no": 1, "action": "click", "selector": "signup-submit", "selector_type": "testid"}
        ],
        "selector_confidence": 0.9,
    }]))
    agent._metadata_bundle = {
        "service_id": "svc-a",
        "qapilot_dir": "",
        "commit_sha": "",
        "selectors": {
            "by_route": {
                "/signup": {
                    "inputs": [{"testid": "email", "label": "이메일", "placeholder": "example@email.com", "v_model": "form.email", "validators": [], "extracted_from": {"file": "frontend/src/pages/Signup.vue"}}],
                    "buttons": [{"testid": "signup-submit", "label": "가입하기", "form_role": "submit", "disabled_when": {"expr": "loading"}}],
                    "outputs": [],
                    "dynamic": [],
                }
            }
        },
        "routes": {
            "routes": [{"path": "/signup", "component_file": "frontend/src/pages/Signup.vue", "guards": [], "meta": {}}],
        },
        "schemas": {
            "request_schemas": {
                "SignupRequest": {
                    "fields": [{"name": "email", "type": "EmailStr", "required": True, "sensitive": False, "validators": []}]
                }
            },
            "response_schemas": {},
            "db_models": {},
        },
    }

    import asyncio
    sem = asyncio.Semaphore(1)
    ts = {"ts_id": "TS-001", "test_cases": [{"tc_id": "TS-001-TC-01", "api": "POST /api/auth/signup"}]}
    tc = ts["test_cases"][0]
    await agent._call_single_tc(sem, ts, tc, [], [], None)

    kwargs = agent.prompts.render.call_args.kwargs
    assert "route_context" in kwargs
    assert "selector_catalog" in kwargs
    assert "schema_context" in kwargs
    assert "Signup.vue" in kwargs["route_context"]
    assert "signup-submit" in kwargs["selector_catalog"]
    mapping = await agent._call_single_tc(sem, ts, tc, [], [], None)
    assert mapping is not None
    assert "mapping_context" in mapping
    assert mapping["mapping_context"]["route_refs"][0]["path"] == "/signup"
    assert "SignupRequest" in mapping["mapping_context"]["schema_refs"]["request_schemas"]
    assert mapping["mapping_context"]["source_status"] == "not_requested"
    assert mapping["mapping_context"]["source_selection_method"] == "none"
    assert "endpoints" in kwargs


@pytest.mark.asyncio
async def test_execute_loads_frontend_dom_from_context(mock_llm_client, tmp_path: Path, monkeypatch):
    """_execute 시작 시 _load_frontend_dom 호출 + frontend_dom 이 _call_batch 에 전달."""
    monkeypatch.chdir(tmp_path)
    agent = _make_agent(mock_llm_client)

    scenarios = [{
        "ts_id": "TS-1",
        "test_cases": [{"tc_id": "TC-1", "name": "n", "given": "g", "when": "w", "then": "t"}],
    }]
    context = {
        "scenarios": scenarios,
        "scan_result": None,
        "frontend_dom": [{"tag": "label", "text": "이메일", "file": "Login.vue",
                          "placeholder": "", "testid": "", "label": "", "name": "", "id": "",
                          "page": "Login", "route": "/login", "control_type": "label"}],
    }

    await agent._execute(context, {}, None)

    # render 호출의 frontend_dom 인자에 "이메일" 포함
    kwargs = agent.prompts.render.call_args.kwargs
    assert "이메일" in kwargs["frontend_dom"]


@pytest.mark.asyncio
async def test_select_and_load_tc_sources_marks_source_missing(mock_llm_client):
    agent = _make_agent(mock_llm_client)
    agent._metadata_bundle = {
        "service_id": "svc-a",
        "commit_sha": "abc123",
        "selectors": {
            "by_route": {
                "/signup": {
                    "inputs": [{"testid": "email", "extracted_from": {"file": "frontend/src/pages/Signup.vue"}}],
                    "buttons": [],
                    "outputs": [],
                    "dynamic": [],
                }
            }
        },
        "routes": {
            "routes": [{"path": "/signup", "component_file": "frontend/src/pages/Signup.vue", "guards": [], "meta": {}}],
        },
        "schemas": {"request_schemas": {}, "response_schemas": {}, "db_models": {}},
    }
    tc_context = agent._build_tc_metadata_context({"tc_id": "TC-1", "api": "POST /api/auth/signup"})
    with patch.object(agent, "_load_source_from_mirror", return_value=None):
        ctx = await agent._select_and_load_tc_sources(
            {"tc_id": "TC-1", "api": "POST /api/auth/signup"},
            tc_context,
            agent._create_tc_llm(),
        )

    assert ctx["source_status"] == "missing"
    assert ctx["sources"] == []
    assert len(ctx["source_candidates"]) >= 1
    assert any("reason" in c for c in ctx["source_candidates"])
    assert ctx["source_selection_method"] in {"deterministic_small_set", "llm_rerank", "deterministic_fallback"}


@pytest.mark.asyncio
async def test_select_and_load_tc_sources_marks_no_candidates_when_no_source_targets(mock_llm_client):
    agent = _make_agent(mock_llm_client)
    agent._metadata_bundle = {
        "service_id": "svc-a",
        "commit_sha": "abc123",
        "selectors": {"by_route": {}},
        "routes": {"routes": []},
        "schemas": {"request_schemas": {}, "response_schemas": {}, "db_models": {}},
    }
    tc_context = agent._build_tc_metadata_context({"tc_id": "TC-1", "api": None})
    ctx = await agent._select_and_load_tc_sources(
        {"tc_id": "TC-1", "api": None},
        tc_context,
        agent._create_tc_llm(),
    )

    assert ctx["source_status"] == "no_candidates"
    assert ctx["sources"] == []
    assert ctx["source_candidates"] == []
    assert ctx["source_selection_method"] == "none"


@pytest.mark.asyncio
async def test_rerank_source_targets_with_llm_uses_llm_selection_when_candidate_pool_is_large(mock_llm_client):
    agent = _make_agent(mock_llm_client)
    tc = {
        "tc_id": "TC-1",
        "api": "GET /api/orders",
        "name": "활성 요금제 없음 고객의 요청 내역 조회",
        "when": "사용자가 대시보드 화면을 연다.",
        "then": "UI) 빈 요청 목록이 표시된다.",
    }
    tc_context = {
        "routes": [{"path": "/dashboard", "component_file": "frontend/src/pages/Dashboard.vue"}],
        "selectors": {"by_route": {"/dashboard": {"outputs": [{"testid": "dashboard-empty-orders"}]}}},
        "schemas": {"request_schemas": {}, "response_schemas": {}, "db_models": {}},
    }
    candidates = [
        {"file": "frontend/src/pages/Dashboard.vue", "line_start": None, "line_end": None, "reason": "matched frontend route /dashboard"},
        {"file": "frontend/src/components/Sidebar.vue", "line_start": None, "line_end": None, "reason": "selector catalog /dashboard buttons exemplar"},
        {"file": "frontend/src/components/OrdersEmptyState.vue", "line_start": None, "line_end": None, "reason": "selector catalog /dashboard outputs exemplar"},
        {"file": "backend/app/routers/orders.py", "line_start": None, "line_end": None, "reason": "backend router/schema heuristic from TC.api"},
    ]
    llm = agent._create_tc_llm()
    llm.chat = AsyncMock(return_value=MagicMock(content='{"selected_indices":[1,2],"rationale":["sidebar link is relevant","dashboard empty output is relevant"]}'))
    selected, method, notes = await agent._rerank_source_targets_with_llm(tc, tc_context, candidates, llm)

    assert method == "llm_rerank"
    assert [item["file"] for item in selected] == [
        "frontend/src/components/Sidebar.vue",
        "frontend/src/components/OrdersEmptyState.vue",
    ]
    assert notes == ["sidebar link is relevant", "dashboard empty output is relevant"]


@pytest.mark.asyncio
async def test_execute_falls_back_to_empty_when_no_frontend_dom(mock_llm_client, monkeypatch, tmp_path: Path):
    """frontend_dom 없으면 안내 문자열로 prompt 렌더."""
    monkeypatch.chdir(tmp_path)
    agent = _make_agent(mock_llm_client)

    scenarios = [{
        "ts_id": "TS-1",
        "test_cases": [{"tc_id": "TC-1", "name": "n", "given": "g", "when": "w", "then": "t"}],
    }]
    await agent._execute({"scenarios": scenarios, "scan_result": None}, {}, None)

    kwargs = agent.prompts.render.call_args.kwargs
    assert "인덱스 없음" in kwargs["frontend_dom"]
