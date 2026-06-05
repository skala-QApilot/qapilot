"""agent_router / runner / state 의 domain_files 주입 흐름 검증 (격차 #207 sub-D).

격차 12 (test_account) 동형 패턴. Spring sub-C (이슈 #210) 가 body 에
`domain_files=[{file_id, filename, s3_key, version, type, reflected?}, ...]` 채워
보내면 → agent_router → state.domain_files → sub-F Part 2 (pipeline._doc_import) 가
각 entry 의 s3_key 를 s3_client.download (sub-E) 로 받아 처리.

본 PR 은 **통로만** — 실제 활용은 sub-F Part 2 (#213) 가 담당.
본 테스트는 통로 두 지점 단위 검증:
- runner.run_pipeline 시그니처 + initial_state.domain_files 저장
- agent_router._start_pipeline 의 body 추출 + _submit_pipeline 전달 + trace 보존

상세: memory/project_qapilot_prd_docs_saas_wire_gap.md
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from qapilot.api import agent_router as router_module


# ── runner.run_pipeline 시그니처 검증 ──────────────────────────────────────────


def test_run_pipeline_accepts_domain_files_kwarg():
    """run_pipeline 시그니처에 domain_files 인자가 추가되어야 함."""
    import inspect

    from qapilot.orchestrator.runner import run_pipeline
    sig = inspect.signature(run_pipeline)
    assert "domain_files" in sig.parameters
    assert sig.parameters["domain_files"].default is None


def test_run_pipeline_initial_state_stores_domain_files(tmp_path: Path):
    """run_pipeline 진입 시 initial_state.domain_files 에 list 그대로 저장."""
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

    docs = [
        {
            "file_id": "f-1",
            "filename": "PRD.md",
            "s3_key": "services/svc-1/domain/f-1/v1/PRD.md",
            "version": 1,
            "type": "PRD",
        },
        {
            "file_id": "f-2",
            "filename": "정책_v3.md",
            "s3_key": "services/svc-1/domain/f-2/v2/정책_v3.md",
            "version": 2,
            "type": "POLICY",
        },
    ]

    with patch.object(runner_module, "build_pipeline", return_value=_FakeGraph()):
        asyncio.run(
            runner_module.run_pipeline(
                options={"command": "test", "trigger": None, "user_input": None,
                         "scenario_ids": None, "filter": None, "tags": None},
                qapilot_dir=tmp_path,
                trace_id="trace-x",
                domain_files=docs,
            )
        )

    assert captured["state"]["domain_files"] == docs


def test_run_pipeline_domain_files_none_when_missing(tmp_path: Path):
    """domain_files 미전달 시 initial_state.domain_files = None (CLI 호환)."""
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

    assert captured["state"]["domain_files"] is None


def test_run_pipeline_rejects_non_list_domain_files(tmp_path: Path):
    """domain_files 가 list 아니면 None (방어적)."""
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
                domain_files="not-a-list",  # type: ignore[arg-type]
            )
        )

    assert captured["state"]["domain_files"] is None


# ── agent_router._start_pipeline body 추출 + _submit_pipeline 전달 ──────────────


def _fake_request(trace_id: str = "trace-y") -> Any:
    req = MagicMock()
    req.state.trace_id = trace_id
    return req


def test_start_pipeline_extracts_domain_files_from_body(tmp_path: Path):
    """body 의 domain_files list 가 _submit_pipeline 호출 인자로 전달."""
    docs = [
        {"file_id": "f-1", "filename": "PRD.md",
         "s3_key": "services/svc-1/domain/f-1/v1/PRD.md", "version": 1},
    ]
    body = {
        "qapilot_dir": str(tmp_path),
        "service_id": "svc-1",
        "domain_files": docs,
    }
    options = {
        "command": "test", "trigger": None, "user_input": None,
        "scenario_ids": None, "filter": "all", "tags": None,
    }
    captured: dict[str, Any] = {}

    def _fake_submit(qd, tid, opts, su, ta=None, df=None):
        captured["domain_files"] = df

    with patch.object(router_module, "_submit_pipeline", side_effect=_fake_submit), \
         patch.object(router_module, "create_trace"), \
         patch.object(router_module, "annotate_trace"):
        resp = router_module._start_pipeline(_fake_request(), body, options)  # type: ignore[arg-type]

    assert resp.status_code == 202
    assert captured["domain_files"] == docs


def test_start_pipeline_domain_files_none_when_body_missing(tmp_path: Path):
    """body 에 domain_files 없으면 None 전달 (Spring sub-C 미머지 graceful)."""
    body = {
        "qapilot_dir": str(tmp_path),
        "service_id": "svc-1",
    }
    options = {
        "command": "test", "trigger": None, "user_input": None,
        "scenario_ids": None, "filter": "all", "tags": None,
    }
    captured: dict[str, Any] = {}

    def _fake_submit(qd, tid, opts, su, ta=None, df=None):
        captured["domain_files"] = df

    with patch.object(router_module, "_submit_pipeline", side_effect=_fake_submit), \
         patch.object(router_module, "create_trace"), \
         patch.object(router_module, "annotate_trace"):
        router_module._start_pipeline(_fake_request(), body, options)  # type: ignore[arg-type]

    assert captured["domain_files"] is None


def test_start_pipeline_rejects_non_list_domain_files(tmp_path: Path):
    """body.domain_files 가 dict / str 등 비-list 면 None (방어적)."""
    body = {
        "qapilot_dir": str(tmp_path),
        "service_id": "svc-1",
        "domain_files": {"not": "a list"},  # 잘못된 형식
    }
    options = {
        "command": "test", "trigger": None, "user_input": None,
        "scenario_ids": None, "filter": "all", "tags": None,
    }
    captured: dict[str, Any] = {}

    def _fake_submit(qd, tid, opts, su, ta=None, df=None):
        captured["domain_files"] = df

    with patch.object(router_module, "_submit_pipeline", side_effect=_fake_submit), \
         patch.object(router_module, "create_trace"), \
         patch.object(router_module, "annotate_trace"):
        router_module._start_pipeline(_fake_request(), body, options)  # type: ignore[arg-type]

    assert captured["domain_files"] is None


def test_start_pipeline_annotates_trace_with_domain_files(tmp_path: Path):
    """trace.json 에 domain_files 저장 — resume 시 복원 위한 보존."""
    docs = [{"file_id": "f-1", "s3_key": "k", "version": 1}]
    body = {
        "qapilot_dir": str(tmp_path),
        "service_id": "svc-1",
        "domain_files": docs,
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
    assert annotate_calls[0].get("domain_files") == docs
