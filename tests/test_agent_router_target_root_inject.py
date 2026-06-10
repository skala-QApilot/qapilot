"""agent_router / runner 의 target_root 주입 흐름 검증.

SaaS 흐름에서 Spring 이 body 로 보낸 service.target_root 가 다음 경로로 전파:
- runner.run_pipeline 시그니처 → initial_state.target_root 저장
- agent_router._start_pipeline 의 body 추출 → annotate_trace 저장 → _submit_pipeline 전달

격차 12 (test_account inject) 와 동형 패턴.

상세: plan glistening-snuggling-treasure.md (SaaS target_root state 주입).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from qapilot.api import agent_router as router_module


# ── runner.run_pipeline 시그니처 검증 ──────────────────────────────────────────


def test_run_pipeline_accepts_target_root_kwarg():
    """run_pipeline 시그니처에 target_root 인자가 추가되어야 함."""
    import inspect

    from qapilot.orchestrator.runner import run_pipeline
    sig = inspect.signature(run_pipeline)
    assert "target_root" in sig.parameters
    assert sig.parameters["target_root"].default is None


def test_run_pipeline_initial_state_stores_target_root(tmp_path: Path):
    """run_pipeline 진입 시 initial_state.target_root 에 str 그대로 저장."""
    import asyncio
    from qapilot.orchestrator import runner as runner_module

    captured: dict[str, Any] = {}

    class _FakeGraph:
        def compile(self):
            async def _ainvoke(state):
                captured["state"] = state
                state["status"] = "completed"
                state["agent_logs"] = []
                return state

            mock_app = MagicMock()
            mock_app.ainvoke = _ainvoke
            return mock_app

    with patch.object(runner_module, "build_pipeline", return_value=_FakeGraph()):
        asyncio.run(
            runner_module.run_pipeline(
                options={"command": "test", "trigger": None, "user_input": None,
                         "scenario_ids": None, "filter": None, "tags": None},
                qapilot_dir=tmp_path,
                trace_id="trace-tr",
                target_root="/Users/x/skala/mini-bss-lite",
            )
        )

    assert captured["state"]["target_root"] == "/Users/x/skala/mini-bss-lite"


def test_run_pipeline_target_root_none_when_missing(tmp_path: Path):
    """target_root 미전달 시 initial_state.target_root = None (graceful)."""
    import asyncio
    from qapilot.orchestrator import runner as runner_module

    captured: dict[str, Any] = {}

    class _FakeGraph:
        def compile(self):
            async def _ainvoke(state):
                captured["state"] = state
                state["status"] = "completed"
                state["agent_logs"] = []
                return state

            mock_app = MagicMock()
            mock_app.ainvoke = _ainvoke
            return mock_app

    with patch.object(runner_module, "build_pipeline", return_value=_FakeGraph()):
        asyncio.run(
            runner_module.run_pipeline(
                options={"command": "test", "trigger": None, "user_input": None,
                         "scenario_ids": None, "filter": None, "tags": None},
                qapilot_dir=tmp_path,
                trace_id="trace-tr",
            )
        )

    assert captured["state"]["target_root"] is None


def test_run_pipeline_rejects_empty_string_target_root(tmp_path: Path):
    """target_root = '' (빈 문자열) 은 None 으로 변환 (방어적)."""
    import asyncio
    from qapilot.orchestrator import runner as runner_module

    captured: dict[str, Any] = {}

    class _FakeGraph:
        def compile(self):
            async def _ainvoke(state):
                captured["state"] = state
                state["status"] = "completed"
                state["agent_logs"] = []
                return state

            mock_app = MagicMock()
            mock_app.ainvoke = _ainvoke
            return mock_app

    with patch.object(runner_module, "build_pipeline", return_value=_FakeGraph()):
        asyncio.run(
            runner_module.run_pipeline(
                options={"command": "test", "trigger": None, "user_input": None,
                         "scenario_ids": None, "filter": None, "tags": None},
                qapilot_dir=tmp_path,
                trace_id="trace-tr",
                target_root="",
            )
        )

    assert captured["state"]["target_root"] is None


# ── agent_router._start_pipeline body 추출 + _submit_pipeline 전달 ──────────────


def _fake_request(trace_id: str = "trace-tr") -> Any:
    req = MagicMock()
    req.state.trace_id = trace_id
    return req


def test_start_pipeline_extracts_target_root_from_body(tmp_path: Path):
    """body 의 target_root str 이 _submit_pipeline 호출 인자로 전달 (7번째 positional)."""
    body = {
        "qapilot_dir": str(tmp_path),
        "service_id": "svc-1",
        "target_root": "/Users/x/skala/mini-bss-lite",
    }
    options = {
        "command": "test", "trigger": None, "user_input": None,
        "scenario_ids": None, "filter": "all", "tags": None,
    }
    captured: dict[str, Any] = {}

    def _fake_submit(qd, tid, opts, su, ta=None, df=None, tr=None):
        captured["target_root"] = tr

    with patch.object(router_module, "_submit_pipeline", side_effect=_fake_submit), \
         patch.object(router_module, "create_trace"), \
         patch.object(router_module, "annotate_trace"):
        resp = router_module._start_pipeline(_fake_request(), body, options)  # type: ignore[arg-type]

    assert resp.status_code == 202
    assert captured["target_root"] == "/Users/x/skala/mini-bss-lite"


def test_start_pipeline_target_root_none_when_body_missing(tmp_path: Path):
    """body 에 target_root 없으면 None 전달 (CLI / 미주입 graceful)."""
    body = {
        "qapilot_dir": str(tmp_path),
        "service_id": "svc-1",
    }
    options = {
        "command": "test", "trigger": None, "user_input": None,
        "scenario_ids": None, "filter": "all", "tags": None,
    }
    captured: dict[str, Any] = {}

    def _fake_submit(qd, tid, opts, su, ta=None, df=None, tr=None):
        captured["target_root"] = tr

    with patch.object(router_module, "_submit_pipeline", side_effect=_fake_submit), \
         patch.object(router_module, "create_trace"), \
         patch.object(router_module, "annotate_trace"):
        router_module._start_pipeline(_fake_request(), body, options)  # type: ignore[arg-type]

    assert captured["target_root"] is None


def test_start_pipeline_rejects_non_str_target_root(tmp_path: Path):
    """body.target_root 가 dict / list 등 비-str 이면 None (방어적)."""
    body = {
        "qapilot_dir": str(tmp_path),
        "service_id": "svc-1",
        "target_root": {"unexpected": "dict"},
    }
    options = {
        "command": "test", "trigger": None, "user_input": None,
        "scenario_ids": None, "filter": "all", "tags": None,
    }
    captured: dict[str, Any] = {}

    def _fake_submit(qd, tid, opts, su, ta=None, df=None, tr=None):
        captured["target_root"] = tr

    with patch.object(router_module, "_submit_pipeline", side_effect=_fake_submit), \
         patch.object(router_module, "create_trace"), \
         patch.object(router_module, "annotate_trace"):
        router_module._start_pipeline(_fake_request(), body, options)  # type: ignore[arg-type]

    assert captured["target_root"] is None


def test_start_pipeline_annotates_trace_with_target_root(tmp_path: Path):
    """trace.json 에 target_root 저장 — resume 시 복원 위한 보존."""
    body = {
        "qapilot_dir": str(tmp_path),
        "service_id": "svc-1",
        "target_root": "/path/to/sut",
    }
    options = {
        "command": "test", "trigger": None, "user_input": None,
        "scenario_ids": None, "filter": "all", "tags": None,
    }
    annotate_calls: list[dict[str, Any]] = []

    def _fake_annotate(tid, **kwargs):
        annotate_calls.append(kwargs)

    with patch.object(router_module, "_submit_pipeline"), \
         patch.object(router_module, "create_trace"), \
         patch.object(router_module, "annotate_trace", side_effect=_fake_annotate):
        router_module._start_pipeline(_fake_request(), body, options)  # type: ignore[arg-type]

    assert annotate_calls, "annotate_trace 가 호출되어야 함"
    assert annotate_calls[0].get("target_root") == "/path/to/sut"
