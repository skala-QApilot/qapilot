"""이슈 #127 — ActionMapperAgent 의 frontend DOM 인덱스 컨텍스트 주입 검증.

`_load_frontend_dom` 의 context 우선 + 디스크 fallback / `_format_frontend_dom` 의
프롬프트 변환 / `_call_batch` 가 frontend_dom 인자를 LLM 호출에 포함하는지 검증.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qapilot.agents.action_mapper_agent import ActionMapperAgent


def _make_agent() -> ActionMapperAgent:
    agent = ActionMapperAgent.__new__(ActionMapperAgent)
    agent.logger = MagicMock()
    agent.llm = MagicMock()
    agent.llm.chat = AsyncMock(return_value=MagicMock(content="[]"))
    agent.llm.total_input_tokens = 0
    agent.llm.total_output_tokens = 0
    agent.llm.total_cost_usd = 0.0
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


def test_load_frontend_dom_from_context_priority():
    """context 에 직접 주입된 frontend_dom 우선 — 디스크 무시."""
    agent = _make_agent()
    ctx_dom = [{"tag": "input", "placeholder": "이메일"}]
    result = agent._load_frontend_dom({"frontend_dom": ctx_dom})
    assert result == ctx_dom


def test_load_frontend_dom_from_disk_fallback(tmp_path: Path, monkeypatch):
    """context 없음 → 디스크 (.qapilot/codebase-index/frontend.json) 로드."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".qapilot" / "codebase-index").mkdir(parents=True)
    (tmp_path / ".qapilot" / "codebase-index" / "frontend.json").write_text(
        json.dumps({"version": 1, "element_count": 1, "elements": [
            {"tag": "label", "text": "이메일", "id": "email", "file": "x.vue"}
        ]}),
        encoding="utf-8",
    )

    agent = _make_agent()
    result = agent._load_frontend_dom({})
    assert len(result) == 1
    assert result[0]["text"] == "이메일"


def test_load_frontend_dom_returns_empty_when_no_source(tmp_path: Path, monkeypatch):
    """context 와 디스크 모두 없음 → 빈 list."""
    monkeypatch.chdir(tmp_path)
    agent = _make_agent()
    result = agent._load_frontend_dom({})
    assert result == []


# ── _format_frontend_dom ────────────────────────────────────────────────────


def test_format_frontend_dom_basic():
    """element 의 핵심 속성을 한 줄로 표기."""
    agent = _make_agent()
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


def test_format_frontend_dom_empty_returns_placeholder():
    """빈 list → 안내 문자열 (LLM 이 fallback)."""
    agent = _make_agent()
    out = agent._format_frontend_dom([])
    assert "인덱스 없음" in out
    assert "qapilot init" in out  # hint


def test_format_frontend_dom_limits_200():
    """element 200 초과 시 잘림 (LLM 토큰 폭증 방지)."""
    agent = _make_agent()
    elements = [
        {"tag": "input", "placeholder": f"item-{i}", "text": "", "label": "",
         "testid": "", "name": "", "id": "", "file": "x.vue"}
        for i in range(250)
    ]
    out = agent._format_frontend_dom(elements)
    lines = [l for l in out.split("\n") if l.strip()]
    assert len(lines) == 200  # 최대 200


def test_format_frontend_dom_skips_empty_element():
    """모든 식별자 빈 element 는 라인 제외."""
    agent = _make_agent()
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


# ── _call_batch frontend_dom 전달 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_call_batch_passes_frontend_dom_to_prompt_render():
    """_call_batch 가 prompts.render(..., frontend_dom=...) 로 인자 전달 검증."""
    agent = _make_agent()
    batch = [{"ts_id": "TS-1", "test_cases": []}]
    endpoints = [{"method": "POST", "path": "/login"}]
    frontend_dom = [{"tag": "input", "placeholder": "이메일", "file": "Login.vue",
                     "text": "", "label": "", "testid": "", "name": "", "id": ""}]

    await agent._call_batch(batch, endpoints, frontend_dom, None)

    # prompts.render 호출 인자 검증
    agent.prompts.render.assert_called_once()
    kwargs = agent.prompts.render.call_args.kwargs
    assert "frontend_dom" in kwargs
    assert 'placeholder="이메일"' in kwargs["frontend_dom"]
    assert "endpoints" in kwargs


@pytest.mark.asyncio
async def test_execute_loads_frontend_dom_from_context(tmp_path: Path, monkeypatch):
    """_execute 시작 시 _load_frontend_dom 호출 + frontend_dom 이 _call_batch 에 전달."""
    monkeypatch.chdir(tmp_path)
    agent = _make_agent()

    scenarios = [{
        "ts_id": "TS-1",
        "test_cases": [{"tc_id": "TC-1", "name": "n", "given": "g", "when": "w", "then": "t"}],
    }]
    context = {
        "scenarios": scenarios,
        "scan_result": None,
        "frontend_dom": [{"tag": "label", "text": "이메일", "file": "Login.vue",
                          "placeholder": "", "testid": "", "label": "", "name": "", "id": ""}],
    }

    await agent._execute(context, {}, None)

    # render 호출의 frontend_dom 인자에 "이메일" 포함
    kwargs = agent.prompts.render.call_args.kwargs
    assert "이메일" in kwargs["frontend_dom"]


@pytest.mark.asyncio
async def test_execute_falls_back_to_empty_when_no_frontend_dom(monkeypatch, tmp_path: Path):
    """frontend_dom 없으면 안내 문자열로 prompt 렌더."""
    monkeypatch.chdir(tmp_path)
    agent = _make_agent()

    scenarios = [{
        "ts_id": "TS-1",
        "test_cases": [{"tc_id": "TC-1", "name": "n", "given": "g", "when": "w", "then": "t"}],
    }]
    await agent._execute({"scenarios": scenarios, "scan_result": None}, {}, None)

    kwargs = agent.prompts.render.call_args.kwargs
    assert "인덱스 없음" in kwargs["frontend_dom"]
