"""frontend index runtime cache + mirror 회귀 테스트.

- codebase scan 결과 저장 시 frontend.json 도 함께 저장한다.
- generate_code 진입 시 runtime cache 의 frontend.json 을 state.frontend_dom 으로 복원한다.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from qapilot.orchestrator.pipeline import (
    _load_scenarios_for_codegen,
    _save_codebase_index_to_disk,
)


def _make_state(tmp_path: Path) -> dict:
    qapilot_dir = tmp_path / ".qapilot" / "svc-a"
    qapilot_dir.mkdir(parents=True, exist_ok=True)
    return {
        "qapilot_dir": str(qapilot_dir),
        "trace_id": "trace-frontend-208",
        "run_options": {"scenario_ids": []},
    }


def test_save_codebase_index_writes_and_mirrors_frontend_index(tmp_path: Path):
    state = _make_state(tmp_path)
    scan = {
        "files": [],
        "git_diff": {"commit_hash": "abc123"},
        "framework": "vue",
        "language": "typescript",
        "endpoint_count": 0,
    }
    frontend_elements = [
        {"tag": "button", "text": "가입하기", "placeholder": "", "label": "",
         "testid": "signup-submit", "name": "", "id": "", "file": "Signup.vue"}
    ]

    mirror_calls: list[str] = []
    with patch("qapilot.orchestrator.pipeline.load_trace", return_value={"service_id": "svc-1"}), \
         patch("qapilot.orchestrator.pipeline._resolve_project_root", return_value=tmp_path), \
         patch("qapilot.orchestrator.pipeline.scan_frontend_directory", return_value=frontend_elements), \
         patch("qapilot.orchestrator.pipeline.upsert_codebase_index", side_effect=lambda *args, **kwargs: mirror_calls.append(args[2]) or True):
        _save_codebase_index_to_disk(scan, state)  # type: ignore[arg-type]

    frontend_path = Path(state["qapilot_dir"]) / "codebase-index" / "frontend.json"
    payload = json.loads(frontend_path.read_text(encoding="utf-8"))
    assert payload["element_count"] == 1
    assert payload["elements"][0]["testid"] == "signup-submit"
    assert "frontend" in mirror_calls


def test_save_codebase_index_uses_scan_frontend_elements_without_local_rescan(tmp_path: Path):
    state = _make_state(tmp_path)
    scan = {
        "files": [],
        "git_diff": {"commit_hash": "abc123"},
        "framework": "vue",
        "language": "typescript",
        "endpoint_count": 0,
        "frontend_elements": [
            {"tag": "input", "text": "", "placeholder": "example@email.com", "label": "이메일",
             "testid": "email", "name": "", "id": "email", "file": "frontend/src/pages/Signup.vue"}
        ],
    }

    with patch("qapilot.orchestrator.pipeline.load_trace", return_value={"service_id": "svc-1"}), \
         patch("qapilot.orchestrator.pipeline._resolve_project_root", return_value=None), \
         patch("qapilot.orchestrator.pipeline.scan_frontend_directory") as scan_local, \
         patch("qapilot.orchestrator.pipeline.upsert_codebase_index", return_value=True):
        _save_codebase_index_to_disk(scan, state)  # type: ignore[arg-type]

    payload = json.loads((Path(state["qapilot_dir"]) / "codebase-index" / "frontend.json").read_text(encoding="utf-8"))
    assert payload["element_count"] == 1
    assert payload["elements"][0]["testid"] == "email"
    scan_local.assert_not_called()


@pytest.mark.asyncio
async def test_load_scenarios_for_codegen_restores_frontend_dom(tmp_path: Path):
    state = _make_state(tmp_path)
    scenarios_dir = Path(state["qapilot_dir"]) / "scenarios"
    scenarios_dir.mkdir(parents=True, exist_ok=True)
    (scenarios_dir / "TS-001.json").write_text(
        json.dumps({"ts_id": "TS-001", "test_cases": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    index_dir = Path(state["qapilot_dir"]) / "codebase-index"
    index_dir.mkdir(parents=True, exist_ok=True)
    (index_dir / "endpoints.json").write_text("[]", encoding="utf-8")
    (index_dir / "frontend.json").write_text(
        json.dumps(
            {
                "version": 1,
                "element_count": 1,
                "elements": [
                    {"tag": "input", "text": "", "placeholder": "example@email.com",
                     "label": "이메일", "testid": "email", "name": "", "id": "email", "file": "Signup.vue"}
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = await _load_scenarios_for_codegen(state)  # type: ignore[arg-type]

    assert len(result["frontend_dom"]) == 1
    assert result["frontend_dom"][0]["testid"] == "email"


@pytest.mark.asyncio
async def test_load_scenarios_for_codegen_incremental_empty_ids_does_not_load_all(tmp_path: Path):
    state = _make_state(tmp_path)
    state["run_options"] = {"scenario_ids": [], "incremental": True}
    scenarios_dir = Path(state["qapilot_dir"]) / "scenarios"
    scenarios_dir.mkdir(parents=True, exist_ok=True)
    (scenarios_dir / "TS-001.json").write_text(
        json.dumps({"ts_id": "TS-001", "test_cases": []}, ensure_ascii=False),
        encoding="utf-8",
    )

    result = await _load_scenarios_for_codegen(state)  # type: ignore[arg-type]

    assert result["scenarios"] == []
