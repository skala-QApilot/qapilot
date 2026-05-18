"""Layer 2 wire-up 단위 테스트.

mock filesystem + mock Playwright 로 `_load_scenarios_for_test`, `_report`,
헬퍼 (_topo_sort_scenarios) 동작 검증. `_test_execution` / `_cross_check` 는
async_playwright 와 외부 LLM 의존이라 통합 테스트로 분리 (본 PR scope 외).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from qapilot.orchestrator import pipeline as P


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _state(run_options: dict | None = None, trace_id: str = "") -> dict:
    return {
        "run_options": {
            "command": "test",
            "trigger": None,
            "user_input": None,
            "scenario_ids": None,
            "filter": None,
            "tags": None,
            **(run_options or {}),
        },
        "trace_id": trace_id,
    }


# ── _topo_sort_scenarios ──────────────────────────────────────────────────────


def test_topo_sort_orders_by_depends_on():
    scenarios = [
        {"ts_id": "TS-002", "depends_on": ["TS-001"]},
        {"ts_id": "TS-001", "depends_on": []},
        {"ts_id": "TS-003", "depends_on": ["TS-002"]},
    ]
    sorted_list = P._topo_sort_scenarios(scenarios)
    ids = [s["ts_id"] for s in sorted_list]
    assert ids == ["TS-001", "TS-002", "TS-003"]


def test_topo_sort_handles_missing_depends_on():
    scenarios = [
        {"ts_id": "TS-001"},  # depends_on 키 없음
        {"ts_id": "TS-002", "depends_on": ["TS-001"]},
    ]
    sorted_list = P._topo_sort_scenarios(scenarios)
    ids = [s["ts_id"] for s in sorted_list]
    assert ids == ["TS-001", "TS-002"]


def test_topo_sort_handles_unknown_dependency_gracefully():
    scenarios = [
        {"ts_id": "TS-002", "depends_on": ["TS-999"]},  # 존재 안 함
        {"ts_id": "TS-001", "depends_on": []},
    ]
    sorted_list = P._topo_sort_scenarios(scenarios)
    ids = [s["ts_id"] for s in sorted_list]
    assert set(ids) == {"TS-001", "TS-002"}


def test_topo_sort_breaks_cycle_gracefully():
    scenarios = [
        {"ts_id": "TS-A", "depends_on": ["TS-B"]},
        {"ts_id": "TS-B", "depends_on": ["TS-A"]},
    ]
    sorted_list = P._topo_sort_scenarios(scenarios)
    assert {s["ts_id"] for s in sorted_list} == {"TS-A", "TS-B"}


# ── _load_scenarios_for_test ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_load_scenarios_for_test_reads_three_artifact_kinds(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    _write_json(tmp_path / ".qapilot" / "scenarios" / "TS-001.json", {
        "ts_id": "TS-001", "name": "회원가입", "depends_on": [],
        "test_cases": [{"tc_id": "TS-001-TC-01", "tags": ["normal"]}],
    })
    _write_json(tmp_path / ".qapilot" / "action-mappings" / "TS-001-TC-01.json", {
        "tc_id": "TS-001-TC-01",
        "steps": [{"step_no": 1, "action": "navigate", "value": "/login"}],
    })
    code_dir = tmp_path / ".qapilot" / "generated-code"
    code_dir.mkdir(parents=True, exist_ok=True)
    (code_dir / "TS-001-TC-01.js").write_text("// stub", encoding="utf-8")

    result = await P._load_scenarios_for_test(_state())

    assert result["trace_id"]  # uuid4 자동 생성
    assert len(result["scenarios"]) == 1
    assert result["scenarios"][0]["ts_id"] == "TS-001"
    assert len(result["action_mappings"]) == 1
    assert result["action_mappings"][0]["tc_id"] == "TS-001-TC-01"
    assert len(result["generated_codes"]) == 1
    assert result["current_layer"] == "L2"


@pytest.mark.asyncio
async def test_load_scenarios_for_test_preserves_explicit_trace_id(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = await P._load_scenarios_for_test(_state(trace_id="fixed-trace-123"))
    assert result["trace_id"] == "fixed-trace-123"


@pytest.mark.asyncio
async def test_load_scenarios_for_test_scenario_ids_filter(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for ts_id in ("TS-001", "TS-002"):
        _write_json(tmp_path / ".qapilot" / "scenarios" / f"{ts_id}.json", {
            "ts_id": ts_id,
            "test_cases": [{"tc_id": f"{ts_id}-TC-01", "tags": []}],
        })
        _write_json(tmp_path / ".qapilot" / "action-mappings" / f"{ts_id}-TC-01.json", {
            "tc_id": f"{ts_id}-TC-01", "steps": [],
        })

    result = await P._load_scenarios_for_test(_state(run_options={"scenario_ids": ["TS-001"]}))
    assert {s["ts_id"] for s in result["scenarios"]} == {"TS-001"}
    assert {a["tc_id"] for a in result["action_mappings"]} == {"TS-001-TC-01"}


@pytest.mark.asyncio
async def test_load_scenarios_for_test_tags_filter(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_json(tmp_path / ".qapilot" / "scenarios" / "TS-001.json", {
        "ts_id": "TS-001",
        "test_cases": [
            {"tc_id": "TC-A", "tags": ["smoke"]},
            {"tc_id": "TC-B", "tags": ["edge"]},
        ],
    })
    for tc_id in ("TC-A", "TC-B"):
        _write_json(tmp_path / ".qapilot" / "action-mappings" / f"{tc_id}.json", {
            "tc_id": tc_id, "steps": [],
        })

    result = await P._load_scenarios_for_test(_state(run_options={"tags": ["smoke"]}))
    assert {a["tc_id"] for a in result["action_mappings"]} == {"TC-A"}


@pytest.mark.asyncio
async def test_load_scenarios_for_test_empty_dirs_graceful(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # 디렉토리 자체 부재 — graceful 빈 리스트
    result = await P._load_scenarios_for_test(_state())
    assert result["scenarios"] == []
    assert result["action_mappings"] == []
    assert result["generated_codes"] == []


# ── _save_codes 갱신 (action-mappings 디스크 영속화) ─────────────────────────


@pytest.mark.asyncio
async def test_save_codes_persists_action_mappings(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state = {
        "generated_codes": [{"tc_id": "TC-1", "code": "// stub"}],
        "action_mappings": [{"tc_id": "TC-1", "steps": [
            {"step_no": 1, "action": "navigate", "value": "/login"},
        ]}],
    }
    result = await P._save_codes(state)  # type: ignore[arg-type]

    am_path = tmp_path / ".qapilot" / "action-mappings" / "TC-1.json"
    assert am_path.exists()
    data = json.loads(am_path.read_text(encoding="utf-8"))
    assert data["tc_id"] == "TC-1"
    assert data["steps"][0]["action"] == "navigate"

    code_path = tmp_path / ".qapilot" / "generated-code" / "TC-1.js"
    assert code_path.exists()
    assert result["status"] == "completed"


# ── _report ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_report_generates_markdown(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state = {
        "trace_id": "trace-abc",
        "ui_results": [
            {"tc_id": "TC-1", "status": "pass", "steps": []},
            {"tc_id": "TC-2", "status": "fail", "steps": [
                {"step_no": 1, "action": "click", "status": "fail",
                 "error": "TOOL_UI_LOCATOR_NOT_FOUND: ..."},
            ]},
        ],
        "cross_check_results": [
            {"tc_id": "TC-1", "match_score": 1.0, "mismatched_fields": 0, "has_mismatch": False},
            {"tc_id": "TC-2", "match_score": 0.0, "mismatched_fields": 3, "has_mismatch": True},
        ],
    }
    result = await P._report(state)  # type: ignore[arg-type]

    report_path = Path(result["report_path"])
    assert report_path.exists()
    body = report_path.read_text(encoding="utf-8")
    assert "trace-abc" in body
    assert "총 TC: 2" in body
    assert "pass: 1" in body
    assert "fail: 1" in body
    assert "TC-2" in body
    assert "TOOL_UI_LOCATOR_NOT_FOUND" in body
    assert "⚠️" in body and "✅" in body
