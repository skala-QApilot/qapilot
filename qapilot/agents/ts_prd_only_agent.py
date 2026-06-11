"""PRD 전용 TS 구조 생성 Agent.

요구사항(PRD)만으로 TS 목록(name/description/domain_area/requirements)을 생성한다.
코드베이스 분석 및 TC 생성은 수행하지 않는다.
"""

from __future__ import annotations

import json
import re
from typing import Any

from qapilot.agents.base_agent import BaseAgent
from qapilot.shared.schemas import ExecuteResult, RequirementItem


def _functional_requirements(requirements: list[RequirementItem]) -> list[RequirementItem]:
    """TS 생성 대상은 기능 요구사항만 남긴다."""
    return [r for r in requirements if r.get("req_type", "functional") == "functional"]


def _format_requirements(requirements: list[RequirementItem]) -> str:
    if not requirements:
        return "없음"
    lines: list[str] = []
    for r in requirements:
        req_id = r.get("req_id", "")
        content = r.get("content", "")
        domain = r.get("domain_area", "")
        priority = r.get("priority", "")
        lines.append(f"- [{req_id}] ({domain}, {priority}) {content}")
    return "\n".join(lines)


def _extract_json(text: str) -> str:
    text = text.strip()
    if "```" in text:
        m = re.search(r"```(?:json)?\s*([\s\S]+?)```", text)
        if m:
            text = m.group(1).strip()
    start = text.find("{")
    if start == -1:
        return text
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return text[start:]


def _fallback_ts_list(requirements: list[RequirementItem]) -> list[dict]:
    """파싱 실패 시 domain_area 기준으로 TS 목록을 구성한다."""
    requirements = _functional_requirements(requirements)
    by_domain: dict[str, list[str]] = {}
    for r in requirements:
        domain = r.get("domain_area") or "기타"
        req_id = r.get("req_id", "")
        by_domain.setdefault(domain, []).append(req_id)
    return [
        {
            "name": f"{domain} 시나리오",
            "description": f"{domain} 관련 요구사항 검증",
            "domain_area": domain,
            "requirements": req_ids,
        }
        for domain, req_ids in by_domain.items()
    ]


class TSFromPRDAgent(BaseAgent):
    """PRD 요구사항만으로 TS 구조 목록을 생성한다."""

    agent_name = "ts_prd_only"
    use_deep_model = True

    async def _execute(
        self,
        context: dict[str, Any],
        params: dict[str, Any],
        last_error: str | None = None,
    ) -> ExecuteResult:
        requirements: list[RequirementItem] = _functional_requirements(
            context.get("requirements") or []
        )

        user_prompt = self.prompts.render(
            requirements=_format_requirements(requirements),
        )
        if last_error:
            user_prompt += f"\n\n[이전 시도 오류: {last_error}. JSON 형식을 확인하라.]"

        response = await self.llm.chat(
            system_prompt=self.prompts.system(),
            user_prompt=user_prompt,
        )

        ts_list, confidence = self._parse(response.content, requirements)
        return ExecuteResult(
            result={"ts_list": ts_list},
            confidence=confidence,
        )

    def _parse(
        self,
        content: str,
        requirements: list[RequirementItem],
    ) -> tuple[list[dict], float]:
        try:
            cleaned = _extract_json(content)
            data = json.loads(cleaned)
        except Exception as e:
            self.logger.warning("ts_prd_only_parse_error", error=str(e), raw=content[:300])
            return _fallback_ts_list(requirements), 0.3

        raw_list = data.get("ts_list") or []
        confidence = float(data.get("confidence", 0.7))

        valid: list[dict] = []
        for ts in raw_list:
            if not isinstance(ts, dict) or not ts.get("name"):
                continue
            valid.append({
                "name": str(ts.get("name", "")),
                "description": str(ts.get("description", "")),
                "domain_area": str(ts.get("domain_area", "")),
                "requirements": ts.get("requirements") or [],
            })

        if not valid:
            return _fallback_ts_list(requirements), 0.3

        return valid, confidence
