"""시나리오 생성 파싱·분석 모듈.

LLM 응답 JSON 파싱, TC 중복 제거, PRD-코드 불일치 탐지를 담당한다.
"""

from __future__ import annotations

import json
import re

from qapilot.shared.errors import AgentExecutionError, ErrorCode
from qapilot.shared.schemas import TestCase, TestScenario, TestValue


def _resolve_tc_depends_on(raw_depends: list, ts_id: str) -> list[str]:
    """TC depends_on의 "TC-XX" 상대 참조를 완전한 ID로 변환한다.

    LLM이 "TC-01" 형식으로 출력한 참조를 "TS-034-TC-01" 형태로 확장한다.
    이미 완전한 ID(TS-XXX-TC-YY)이면 그대로 유지한다.
    """
    resolved: list[str] = []
    for ref in raw_depends:
        if re.match(r'^TC-\d+$', str(ref)):
            n = int(str(ref).split('-')[1])
            resolved.append(f"{ts_id}-TC-{n:02d}")
        else:
            resolved.append(str(ref))
    return resolved

def _fix_unescaped_newlines(json_str: str) -> str:
    """JSON 문자열 값 내의 이스케이프되지 않은 줄바꿈·탭을 수정한다.

    LLM이 given/when/then 값에 literal newline을 넣을 때 발생하는
    JSONDecodeError를 방지하기 위해 문자 단위로 파싱해 수정한다.
    """
    result: list[str] = []
    in_string = False
    escape_next = False

    for ch in json_str:
        if escape_next:
            result.append(ch)
            escape_next = False
        elif ch == "\\" and in_string:
            result.append(ch)
            escape_next = True
        elif ch == '"':
            result.append(ch)
            in_string = not in_string
        elif in_string and ch == "\n":
            result.append("\\n")
        elif in_string and ch == "\r":
            result.append("\\r")
        elif in_string and ch == "\t":
            result.append("\\t")
        else:
            result.append(ch)

    return "".join(result)


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
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            data = json.loads(_fix_unescaped_newlines(cleaned))
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
                    "api": tc.get("api"),
                    "depends_on": _resolve_tc_depends_on(tc.get("depends_on", []), ts_id),
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
                "affected_files": affected_files if affected_files is not None else s.get("affected_files", []),
                "domain_rules_used": domain_rule_ids,
                "requirements": s.get("requirements", []),
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
