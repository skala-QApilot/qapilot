"""요구사항 추출 Agent.

PRD 문서에서 개별 요구사항 항목을 구조화된 형식으로 추출하고
REQ-XXX ID를 부여한다. RTM의 행을 구성하는 기준 데이터.

담당: 전아린
Created: 2026-05-07
"""

from __future__ import annotations

import json
import re
from typing import Any

from qapilot.agents.base_agent import BaseAgent
from qapilot.shared.errors import AgentExecutionError, ErrorCode
from qapilot.shared.schemas import ExecuteResult, RequirementItem


class RequirementExtractorAgent(BaseAgent):
    """요구사항 추출 Agent.

    역할: PRD에서 REQ-XXX 구조화 추출, RTM 행 생성
    입력: 도메인 문서 텍스트
    출력: List[RequirementItem], confidence
    호출 Tool: 도메인 지식 Tool
    HITL: X (파이프라인 hitl_review 노드에서 처리)
    """

    allowed_tools = ["domain_knowledge"]

    async def _execute(
        self,
        context: dict[str, Any],
        params: dict[str, Any],
        last_error: str | None = None,
    ) -> ExecuteResult:
        """문서 텍스트에서 요구사항을 추출하고 REQ-XXX ID를 부여한다.

        Args:
            context: 파이프라인 컨텍스트. domain_rules (list[DomainRule]) 를 읽는다.
            params: 실행 파라미터.
                document_text (str): 분석할 문서 텍스트.
                existing_count (int, optional): 기존 REQ 항목 수(ID 오프셋). 기본값 0.
            last_error: 이전 시도 에러. 자가 수정 힌트에 사용.

        Returns:
            ExecuteResult: requirements(list[RequirementItem])와 confidence 포함.

        Raises:
            AgentExecutionError: document_text 누락 또는 LLM 출력 파싱 실패 시.
        """
        document_text = params.get("document_text", "").strip()
        if not document_text:
            raise AgentExecutionError(
                ErrorCode.AGENT_001, "document_text 파라미터가 필요합니다."
            )

        existing_count = int(params.get("existing_count", 0))
        domain_rules: list = context.get("domain_rules", [])

        # 컨텍스트에 도메인 규칙이 없으면 Tool로 직접 검색
        if not domain_rules:
            domain_rules = await self._search_domain_rules(document_text[:200])

        from qapilot.tools.domain_knowledge import DomainKnowledgeTool

        domain_rules_text = DomainKnowledgeTool.format_rules_for_prompt(domain_rules) or "없음"

        user_prompt = self.with_correction_hint(
            self.prompts.render(
                domain_rules=domain_rules_text,
                document_text=document_text,
                req_id_start=f"REQ-{existing_count + 1:03d}",
            ),
            last_error,
        )

        response = await self.llm.chat(
            system_prompt=self.prompts.system(),
            user_prompt=user_prompt,
        )

        requirements, confidence = self._parse_response(response.content, existing_count)

        self.logger.info(
            "requirements_extracted",
            count=len(requirements),
            confidence=confidence,
        )

        return ExecuteResult(
            result={"requirements": requirements},
            confidence=confidence,
        )

    async def _search_domain_rules(self, query: str) -> list:
        """도메인 지식 Tool로 관련 규칙을 검색한다.

        Args:
            query: 검색 쿼리 (문서 앞부분 요약).

        Returns:
            list: 검색된 DomainRule 목록. 실패 시 빈 리스트.
        """
        try:
            result = await self.use_tool(
                "domain_knowledge",
                {"action": "search", "query": query, "top_k": 5},
            )
            return result.get("rules", [])
        except Exception:
            return []

    def _parse_response(
        self, content: str, existing_count: int
    ) -> tuple[list[RequirementItem], float]:
        """LLM 응답 JSON을 파싱하고 RequirementItem 목록을 반환한다.

        중복 항목을 content 기준으로 제거하고, REQ-XXX ID를 순번대로 재부여한다.

        Args:
            content: LLM 응답 문자열.
            existing_count: 기존 REQ 항목 수 (ID 순번 오프셋).

        Returns:
            tuple[list[RequirementItem], float]: (요구사항 목록, confidence).

        Raises:
            AgentExecutionError: JSON 파싱 실패 또는 requirements 필드 누락 시.
        """
        try:
            cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", content).strip()
            data = json.loads(cleaned)
        except json.JSONDecodeError as e:
            raise AgentExecutionError(
                ErrorCode.AGENT_004,
                f"LLM 출력 JSON 파싱 실패: {e}\n출력 앞부분: {content[:300]}",
            ) from e

        if "requirements" not in data:
            raise AgentExecutionError(
                ErrorCode.AGENT_004,
                f"'requirements' 필드가 없습니다. 출력: {content[:300]}",
            )

        raw_items: list[dict] = data["requirements"]
        confidence: float = float(data.get("confidence", 0.5))

        # content 기준 중복 제거
        seen: set[str] = set()
        unique: list[dict] = []
        for item in raw_items:
            key = item.get("content", "").strip()
            if key and key not in seen:
                seen.add(key)
                unique.append(item)

        # ID 순번 재부여 (LLM 생성 ID 무시)
        requirements: list[RequirementItem] = [
            {
                "req_id": f"REQ-{existing_count + i + 1:03d}",
                "req_type": item.get("req_type", "functional"),
                "content": item.get("content", ""),
                "priority": item.get("priority", "medium"),
                "domain_area": item.get("domain_area", ""),
            }
            for i, item in enumerate(unique)
        ]

        return requirements, min(max(confidence, 0.0), 1.0)
