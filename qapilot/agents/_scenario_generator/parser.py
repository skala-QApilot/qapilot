"""시나리오 생성 파싱·분석 모듈.

LLM 응답 JSON 파싱, TC 중복 제거, PRD-코드 불일치 탐지를 담당한다.
"""

from __future__ import annotations

import json
import re

from qapilot.shared.errors import AgentExecutionError, ErrorCode
from qapilot.shared.schemas import TestCase, TestScenario, TestValue

_DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "결제": ["payment", "pay", "결제", "checkout"],
    "회원": ["user", "member", "account", "profile", "회원"],
    "주문": ["order", "cart", "주문"],
    "배송": ["delivery", "shipping", "ship", "배송"],
    "인증": ["auth", "login", "logout", "token", "session", "인증"],
    "알림": ["notification", "notify", "push", "알림"],
    "취소": ["cancel", "refund", "취소", "환불"],
    "검색": ["search", "query", "검색"],
}


def parse_response(
    content: str,
    trigger: str,
    affected_files: list[str],
    domain_rules: list,
) -> tuple[list[TestScenario], float]:
    """LLM 응답 JSON을 파싱하고 TestScenario 목록을 반환한다.

    given+when+then 조합 기준으로 TC 중복을 제거하고 TS-XXX/TC-XX ID를 부여한다.

    Args:
        content: LLM 응답 문자열.
        trigger: 생성 트리거 (init/code_change 등).
        affected_files: Git diff 기반 변경 파일 목록.
        domain_rules: 도메인 규칙 목록 (rule_id 수집용).

    Returns:
        tuple[list[TestScenario], float]: (시나리오 목록, confidence).

    Raises:
        AgentExecutionError: JSON 파싱 실패 또는 scenarios 필드 누락 시.
    """
    try:
        cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", content).strip()
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise AgentExecutionError(
            ErrorCode.AGENT_004,
            f"LLM 출력 JSON 파싱 실패: {e}\n출력 앞부분: {content[:300]}",
        ) from e

    if "scenarios" not in data:
        raise AgentExecutionError(
            ErrorCode.AGENT_004,
            f"'scenarios' 필드가 없습니다. 출력: {content[:300]}",
        )

    confidence = min(max(float(data.get("confidence", 0.5)), 0.0), 1.0)
    domain_rule_ids = [r.get("rule_id", "")[:8] for r in domain_rules]

    seen: set[str] = set()
    scenarios: list[TestScenario] = []

    for i, s in enumerate(data["scenarios"]):
        ts_id = f"TS-{i + 1:03d}"
        unique_tcs: list[TestCase] = []

        for j, tc in enumerate(s.get("test_cases", [])):
            key = f"{tc.get('given','')}|{tc.get('when','')}|{tc.get('then','')}"
            if key in seen:
                continue
            seen.add(key)

            values: list[TestValue] = [
                {
                    "field": v.get("field", ""),
                    "value": str(v.get("value", "")),
                    "type": v.get("type", "string"),
                    "purpose": v.get("purpose", ""),
                }
                for v in tc.get("values", [])
            ]
            unique_tcs.append(
                {
                    "tc_id": f"{ts_id}-TC-{j + 1:02d}",
                    "name": tc.get("name", ""),
                    "given": tc.get("given", ""),
                    "when": tc.get("when", ""),
                    "then": tc.get("then", ""),
                    "values": values,
                    "tags": tc.get("tags", []),
                    "req_id": tc.get("req_id"),
                }
            )

        if not unique_tcs:
            continue

        scenarios.append(
            {
                "ts_id": ts_id,
                "name": s.get("name", ""),
                "description": s.get("description", ""),
                "trigger": trigger,
                "affected_files": affected_files or s.get("affected_files", []),
                "domain_rules_used": domain_rule_ids,
                "test_cases": unique_tcs,
            }
        )

    return scenarios, confidence


def detect_prd_code_mismatch(requirements: list, scan_result: dict) -> list[dict]:
    """high priority 요구사항의 도메인 영역이 코드에 있는지 키워드로 확인한다."""
    if not requirements or not scan_result:
        return []

    files: list[dict] = scan_result.get("files", [])
    code_text = " ".join(f["path"].lower() for f in files) + " " + " ".join(
        ep.get("path", "").lower()
        for f in files
        for ep in f.get("endpoints", [])
    )

    covered: set[str] = {
        domain
        for domain, keywords in _DOMAIN_KEYWORDS.items()
        if any(kw in code_text for kw in keywords)
    }

    return [
        {
            "req_id": r["req_id"],
            "domain_area": r.get("domain_area", ""),
            "note": f"'{r.get('domain_area','')}' 도메인 관련 엔드포인트를 코드베이스에서 찾지 못했습니다",
        }
        for r in requirements
        if r.get("priority") == "high" and r.get("domain_area") not in covered
    ]


def format_mismatches(mismatches: list[dict]) -> str:
    if not mismatches:
        return "없음"
    lines = ["⚠️ PRD-코드 불일치 (high priority 요구사항 미구현 의심):"]
    lines += [f"  [{m['req_id']}] {m['note']}" for m in mismatches]
    return "\n".join(lines)
