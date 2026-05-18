"""시나리오 그래프 생성 유틸.

Author: C
Created: 2026-05-15
"""

from __future__ import annotations

from datetime import datetime, timezone


def build_scenario_graph(scenarios: list[dict]) -> dict:
    """affected_files 기반 시나리오 의존성 그래프를 생성한다."""
    nodes = [_node(scenario) for scenario in scenarios]
    edges = []
    for source in scenarios:
        for target in scenarios:
            if source.get("ts_id") >= target.get("ts_id"):
                continue
            if _shared_files(source, target):
                edges.append(
                    {
                        "source": source.get("ts_id"),
                        "target": target.get("ts_id"),
                        "relation": "affects",
                    }
                )
    return {"nodes": nodes, "edges": edges, "generated_at": _utc_now()}


def build_scenario_flow(scenarios: list[dict]) -> dict:
    """시나리오별 TC 흐름 그래프를 생성한다."""
    flows = []
    for scenario in scenarios:
        flows.append(
            {
                "ts_id": scenario.get("ts_id"),
                "ts_name": scenario.get("name"),
                "steps": [_step(tc) for tc in scenario.get("test_cases", [])],
            }
        )
    return {"flows": flows, "generated_at": _utc_now()}


def _node(scenario: dict) -> dict:
    return {
        "id": scenario.get("ts_id"),
        "name": scenario.get("name"),
        "tc_count": len(scenario.get("test_cases", [])),
    }


def _step(test_case: dict) -> dict:
    return {
        "tc_id": test_case.get("tc_id"),
        "name": test_case.get("name"),
        "given": test_case.get("given"),
        "when": test_case.get("when"),
        "then": test_case.get("then"),
    }


def _shared_files(source: dict, target: dict) -> bool:
    left = set(source.get("affected_files", []))
    right = set(target.get("affected_files", []))
    return bool(left.intersection(right))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
