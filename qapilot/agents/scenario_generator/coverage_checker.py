"""요구사항 커버리지 검사 모듈.

생성된 시나리오가 모든 요구사항을 커버하는지 확인하고
미커버 요구사항 목록을 반환한다.
"""

from __future__ import annotations

from qapilot.agents.scenario_generator.repository import load_all_scenarios
from qapilot.shared.schemas import TestScenario


def compute_coverage(
    scenarios: list[TestScenario],
    requirements: list[dict],
) -> dict:
    """TC의 req_id 기준으로 각 requirement의 커버리지를 계산한다.

    Args:
        scenarios: 생성된 TestScenario 목록.
        requirements: RequirementItem 목록 (req_id, content, domain_area 포함).

    Returns:
        covered_ids (set[str]): 커버된 req_id.
        uncovered (list[dict]): 미커버 requirement 목록.
        rate (float): 커버리지 비율 0.0~1.0.
    """
    if not requirements:
        return {"covered_ids": set(), "uncovered": [], "rate": 1.0}

    tc_req_ids: set[str] = set()
    for ts in scenarios:
        for tc in ts.get("test_cases", []):
            if tc.get("req_id"):
                tc_req_ids.add(tc["req_id"])

    covered_ids: set[str] = set()
    uncovered: list[dict] = []

    for req in requirements:
        req_id = req.get("req_id", "")
        if req_id in tc_req_ids:
            covered_ids.add(req_id)
        else:
            uncovered.append(req)

    total = len(requirements)
    rate = len(covered_ids) / total if total > 0 else 1.0

    return {
        "covered_ids": covered_ids,
        "uncovered": uncovered,
        "rate": round(rate, 3),
    }


def check_coverage_from_disk(requirements: list[dict]) -> dict:
    """디스크에 저장된 시나리오를 읽어 커버리지를 계산한다.

    파이프라인 외부(CLI, 단독 검증 등)에서 독립적으로 호출할 수 있다.

    Args:
        requirements: RequirementItem 목록.

    Returns:
        compute_coverage()와 동일한 구조.
    """
    scenarios = load_all_scenarios()
    return compute_coverage(scenarios, requirements)


def format_coverage_report(coverage: dict) -> str:
    """커버리지 결과를 사람이 읽기 쉬운 문자열로 변환한다."""
    covered = coverage["covered_ids"]
    uncovered = coverage["uncovered"]
    rate = coverage["rate"]
    total = len(covered) + len(uncovered)

    lines = [f"요구사항 커버리지: {rate:.0%} ({len(covered)}/{total})"]
    if uncovered:
        lines.append(f"미커버 요구사항 {len(uncovered)}개:")
        for r in uncovered:
            content_preview = r.get("content", "")[:60]
            lines.append(
                f"  [{r['req_id']}] ({r.get('domain_area', '')}) {content_preview}"
            )
    else:
        lines.append("모든 요구사항이 커버되었습니다.")
    return "\n".join(lines)
