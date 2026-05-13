"""자연어 요구사항 해석 Agent.

QA 담당자가 입력한 자연어 시나리오를
구조화된 형식(Given/When/Then)으로 변환한다.

담당: C
Created: 2026-05-07
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from qapilot.agents.base_agent import BaseAgent
from qapilot.shared.errors import AgentExecutionError, ErrorCode
from qapilot.shared.schemas import ExecuteResult, RequirementItem


class NaturalLanguageAgent(BaseAgent):
    """자연어 요구사항 해석 Agent.

    역할: 자연어 → RequirementItem 구조화
    입력: 사용자 자연어 텍스트, 코드베이스 요약, 도메인 규칙
    출력: List[RequirementItem], confidence
    호출 Tool: 없음
    """

    allowed_tools: list[str] = []

    async def _execute(
        self,
        context: dict[str, Any],
        params: dict[str, Any],
        last_error: str | None = None,
    ) -> ExecuteResult:
        """자연어 입력을 RequirementItem 목록으로 변환한다.

        Args:
            context: scan_result, domain_rules 등 파이프라인 컨텍스트.
            params: trigger, user_input 실행 파라미터.
            last_error: 이전 재시도 오류 메시지.

        Returns:
            ExecuteResult: requirements와 confidence.

        Raises:
            AgentExecutionError: 입력이 너무 짧거나 LLM 응답 파싱에 실패한 경우.
        """
        trigger = params.get("trigger")
        if trigger != "natural_lang":
            return ExecuteResult(result={"requirements": []}, confidence=1.0)

        user_input = str(params.get("user_input") or "")
        if len(user_input.strip()) < 10:
            raise AgentExecutionError(
                ErrorCode.AGENT_003,
                "user_input이 너무 짧습니다. 10자 이상 입력해 주세요.",
            )

        scan_summary = self._build_scan_summary(context.get("scan_result"))
        domain_rules_text = self._build_domain_rules_text(context.get("domain_rules") or [])
        user_prompt = self.prompts.render(
            user_input=user_input,
            scan_result_summary=scan_summary,
            domain_rules=domain_rules_text,
        )
        user_prompt = self.with_correction_hint(user_prompt, last_error)

        response = await self.llm.chat(
            system_prompt=self.prompts.system(),
            user_prompt=user_prompt,
        )
        requirements = self._parse_requirements(response.content)
        confidence = self._calc_confidence(requirements)

        self.logger.info(
            "natural_language_interpreted",
            requirements=len(requirements),
            confidence=confidence,
        )
        return ExecuteResult(result={"requirements": requirements}, confidence=confidence)

    def _build_scan_summary(self, scan_result: dict[str, Any] | None) -> str:
        """코드베이스 스캔 결과에서 프롬프트용 요약만 생성한다.

        Args:
            scan_result: CodebaseScannerTool의 ScanResult.

        Returns:
            str: 프레임워크, 언어, 엔드포인트 요약.
        """
        if not scan_result:
            return "코드베이스 정보 없음"

        lines = [
            f"framework: {scan_result.get('framework', 'unknown')}",
            f"language: {scan_result.get('language', 'unknown')}",
            f"endpoint_count: {scan_result.get('endpoint_count', 0)}",
            "endpoints:",
        ]
        endpoints: list[str] = []
        for file_info in scan_result.get("files", []):
            for endpoint in file_info.get("endpoints", []):
                method = endpoint.get("method", "?")
                path = endpoint.get("path", "?")
                endpoints.append(f"  - {method} {path}")
        lines.extend(endpoints[:30] or ["  - 없음"])
        return "\n".join(lines)

    def _build_domain_rules_text(self, domain_rules: list) -> str:
        """도메인 규칙 목록을 프롬프트용 번호 목록으로 변환한다.

        Args:
            domain_rules: DomainRule dict 목록.

        Returns:
            str: 번호 목록 문자열.
        """
        if not domain_rules:
            return "도메인 규칙 없음"

        lines: list[str] = []
        for index, rule in enumerate(domain_rules, start=1):
            category = rule.get("category", "일반")
            content = rule.get("content", "")
            lines.append(f"{index}. [{category}] {content}")
        return "\n".join(lines)

    def _parse_requirements(self, content: str) -> list[RequirementItem]:
        """LLM 응답에서 RequirementItem JSON 배열을 파싱한다.

        Args:
            content: LLM 응답 문자열.

        Returns:
            list[RequirementItem]: 검증된 요구사항 목록.

        Raises:
            AgentExecutionError: JSON 파싱 또는 구조 검증 실패 시.
        """
        try:
            cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", content.strip())
            data = json.loads(cleaned)
            if not isinstance(data, list):
                raise ValueError("응답은 JSON 배열이어야 합니다.")
            return [self._validate_requirement(item) for item in data]
        except Exception as e:
            raise AgentExecutionError(
                ErrorCode.AGENT_004,
                f"응답 파싱 실패: {e}",
            ) from e

    def _validate_requirement(self, item: Any) -> RequirementItem:
        """단일 RequirementItem 구조와 enum 값을 검증한다."""
        required = ("req_id", "req_type", "content", "priority", "domain_area")
        if not isinstance(item, dict):
            raise ValueError("요구사항 항목은 객체여야 합니다.")
        missing = [field for field in required if field not in item]
        if missing:
            raise ValueError(f"필수 필드 누락: {', '.join(missing)}")
        if item["req_type"] not in ("functional", "non_functional"):
            raise ValueError(f"허용되지 않는 req_type: {item['req_type']}")
        if item["priority"] not in ("high", "medium", "low"):
            raise ValueError(f"허용되지 않는 priority: {item['priority']}")
        return {
            "req_id": str(item["req_id"]),
            "req_type": item["req_type"],
            "content": str(item["content"]),
            "priority": item["priority"],
            "domain_area": str(item["domain_area"]),
        }

    def _calc_confidence(self, requirements: Sequence[RequirementItem]) -> float:
        """요구사항 완성도 기반 confidence를 계산한다.

        Args:
            requirements: RequirementItem dict 목록.

        Returns:
            float: 0.0~1.0 범위의 confidence.
        """
        if not requirements:
            return 0.0

        fields = ("req_id", "req_type", "content", "priority", "domain_area")
        complete = sum(1 for item in requirements if all(item.get(field) for field in fields))
        completeness = complete / len(requirements)
        confidence = 0.8 * completeness
        if len(requirements) >= 3:
            confidence += 0.1
        return min(max(confidence, 0.0), 1.0)
