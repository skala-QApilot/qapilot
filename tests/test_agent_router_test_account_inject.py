"""agent_router / runner / state 의 test_account 주입 흐름 검증 (격차 12 SaaS 후속).

PR #176 (이슈 #174) 의 `_ensure_authenticated` 는 `cfg.project.test_account` 의존인데
SaaS 흐름은 cfg 출처가 다름 (agent process 의 CWD). Spring 이 body 로 채워 보낸 test_account
가 state.test_account → pipeline._test_execution → UITestTool 까지 전파되어야 함.

본 테스트는 흐름의 두 지점 단위 검증:
- runner.run_pipeline 시그니처 + initial_state.test_account 저장
- agent_router._start_pipeline 의 body 추출 + _submit_pipeline 전달

상세: memory/project_qapilot_saas_test_account_gap.md
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from qapilot.api import agent_router as router_module


# ── runner.run_pipeline 시그니처 검증 ──────────────────────────────────────────


def test_run_pipeline_accepts_test_account_kwarg():
    """run_pipeline 시그니처에 test_account 인자가 추가되어야 함."""
    import inspect

    from qapilot.orchestrator.runner import run_pipeline
    sig = inspect.signature(run_pipeline)
    assert "test_account" in sig.parameters
    assert sig.parameters["test_account"].default is None


def test_run_pipeline_initial_state_stores_test_account(tmp_path: Path):
    """run_pipeline 진입 시 initial_state.test_account 에 dict 그대로 저장."""
    import asyncio
    from qapilot.orchestrator import runner as runner_module

    captured: dict[str, Any] = {}

    class _FakeGraph:
        def compile(self):
            async def _ainvoke(state):
                captured["state"] = state
                # 최소 필드만 채워서 run_pipeline 정상 완료 흐름 보장
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
                trace_id="trace-x",
                staging_url="http://sut.local",
                test_account={"email": "a@b.com", "password": "secret"},
            )
        )

    assert captured["state"]["test_account"] == {"email": "a@b.com", "password": "secret"}
    assert captured["state"]["staging_url"] == "http://sut.local"


def test_run_pipeline_test_account_none_when_missing(tmp_path: Path):
    """test_account 미전달 시 initial_state.test_account = None (graceful)."""
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
                trace_id="trace-x",
            )
        )

    assert captured["state"]["test_account"] is None


def test_run_pipeline_rejects_non_dict_test_account(tmp_path: Path):
    """test_account 가 dict 아니면 None (방어적)."""
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
                trace_id="trace-x",
                test_account="not-a-dict",  # type: ignore[arg-type]
            )
        )

    assert captured["state"]["test_account"] is None


# ── agent_router._start_pipeline body 추출 + _submit_pipeline 전달 ──────────────


def _fake_request(trace_id: str = "trace-y") -> Any:
    req = MagicMock()
    req.state.trace_id = trace_id
    return req


def test_start_pipeline_extracts_test_account_from_body(tmp_path: Path):
    """body 의 test_account dict 가 _submit_pipeline 호출 인자로 전달."""
    body = {
        "qapilot_dir": str(tmp_path),
        "service_id": "svc-1",
        "staging_url": "http://sut.local",
        "test_account": {"email": "demo@x.com", "password": "Passw0rd!"},
    }
    options = {
        "command": "test", "trigger": None, "user_input": None,
        "scenario_ids": None, "filter": "all", "tags": None,
    }
    captured: dict[str, Any] = {}

    def _fake_submit(qd, tid, opts, su, ta=None, df=None, tr=None):
        captured["test_account"] = ta
        captured["staging_url"] = su

    with patch.object(router_module, "_submit_pipeline", side_effect=_fake_submit), \
         patch.object(router_module, "create_trace"), \
         patch.object(router_module, "annotate_trace"):
        resp = router_module._start_pipeline(_fake_request(), body, options)  # type: ignore[arg-type]

    assert resp.status_code == 202
    assert captured["test_account"] == {"email": "demo@x.com", "password": "Passw0rd!"}
    assert captured["staging_url"] == "http://sut.local"


def test_start_pipeline_test_account_none_when_body_missing(tmp_path: Path):
    """body 에 test_account 없으면 None 전달 (CLI / 미주입 graceful)."""
    body = {
        "qapilot_dir": str(tmp_path),
        "service_id": "svc-1",
        "staging_url": "http://sut.local",
    }
    options = {
        "command": "test", "trigger": None, "user_input": None,
        "scenario_ids": None, "filter": "all", "tags": None,
    }
    captured: dict[str, Any] = {}

    def _fake_submit(qd, tid, opts, su, ta=None, df=None, tr=None):
        captured["test_account"] = ta

    with patch.object(router_module, "_submit_pipeline", side_effect=_fake_submit), \
         patch.object(router_module, "create_trace"), \
         patch.object(router_module, "annotate_trace"):
        router_module._start_pipeline(_fake_request(), body, options)  # type: ignore[arg-type]

    assert captured["test_account"] is None


def test_start_pipeline_rejects_non_dict_test_account(tmp_path: Path):
    """body.test_account 가 str / list 등 비-dict 면 None (방어적)."""
    body = {
        "qapilot_dir": str(tmp_path),
        "service_id": "svc-1",
        "test_account": "bogus-string",
    }
    options = {
        "command": "test", "trigger": None, "user_input": None,
        "scenario_ids": None, "filter": "all", "tags": None,
    }
    captured: dict[str, Any] = {}

    def _fake_submit(qd, tid, opts, su, ta=None, df=None, tr=None):
        captured["test_account"] = ta

    with patch.object(router_module, "_submit_pipeline", side_effect=_fake_submit), \
         patch.object(router_module, "create_trace"), \
         patch.object(router_module, "annotate_trace"):
        router_module._start_pipeline(_fake_request(), body, options)  # type: ignore[arg-type]

    assert captured["test_account"] is None


def test_start_pipeline_annotates_trace_with_test_account(tmp_path: Path):
    """trace.json 에 test_account 저장 — resume 시 복원 위한 보존."""
    body = {
        "qapilot_dir": str(tmp_path),
        "service_id": "svc-1",
        "test_account": {"email": "x@y.com", "password": "p"},
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

    # 첫 annotate_trace 호출 인자에 test_account 포함
    assert annotate_calls, "annotate_trace 가 호출되어야 함"
    assert annotate_calls[0].get("test_account") == {"email": "x@y.com", "password": "p"}
