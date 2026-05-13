"""시나리오 생성 Agent.

코드베이스 분석 결과와 Git diff를 기반으로
테스트 시나리오를 자동 생성한다.

담당: B
Created: 2026-05-07
"""

from __future__ import annotations

from typing import Any

from qapilot.agents.scenario_generator.parser import (
    detect_prd_code_mismatch,
    format_mismatches,
    parse_response,
)
from qapilot.agents.scenario_generator.repository import save_scenarios
from qapilot.agents.base_agent import BaseAgent
from qapilot.shared.schemas import ExecuteResult


class ScenarioGeneratorAgent(BaseAgent):
    """시나리오 생성 Agent.

    역할: 코드 변경 기반 테스트 시나리오 자동 생성
    입력: CodebaseContext, GitDiff, DomainRules, RequirementItems
    출력: List[TestScenario], confidence
    호출 Tool: 코드 인덱스 Tool, 도메인 지식 Tool
    HITL: O (파이프라인 hitl_review 노드 — 미구현, 추후 연동)
    """

    allowed_tools = ["codebase_scanner", "domain_knowledge"]

    async def _execute(
        self,
        context: dict[str, Any],
        params: dict[str, Any],
        last_error: str | None = None,
    ) -> ExecuteResult:
        """코드베이스·도메인 규칙·요구사항을 조합하여 TS/TC/TV 시나리오를 생성한다.

        Args:
            context: 파이프라인 컨텍스트.
                scan_result: 코드베이스 스캔 결과 (없으면 Tool 호출).
                domain_rules: 도메인 규칙 목록 (없으면 Tool 호출).
                requirements: 요구사항 목록.
            params:
                trigger: 생성 트리거 (init/code_change/doc_update/natural_lang).
                affected_only: True이면 Git diff 기반 영향 파일만 대상으로 함.
            last_error: 이전 시도 에러 (자가 수정 힌트).

        Returns:
            ExecuteResult: scenarios(list[TestScenario]), prd_code_mismatches 포함.
        """
        scan_result: dict = context.get("scan_result") or {}
        domain_rules: list = context.get("domain_rules", [])
        requirements: list = context.get("requirements", [])
        trigger: str = params.get("trigger", "code_change")
        affected_only: bool = bool(params.get("affected_only", False))

        if not scan_result:
            scan_result = await self._fetch_scan_result()

        if not domain_rules:
            query = " ".join(r.get("content", "")[:60] for r in requirements[:3]) or "테스트 시나리오"
            domain_rules = await self._fetch_domain_rules(query)

        affected_files: list[str] = []
        if affected_only:
            affected_files = (scan_result.get("git_diff") or {}).get("changed_files", [])

        mismatches = detect_prd_code_mismatch(requirements, scan_result)

        from qapilot.tools.domain_knowledge import DomainKnowledgeTool

        user_prompt = self.with_correction_hint(
            self.prompts.render(
                domain_rules=DomainKnowledgeTool.format_rules_for_prompt(domain_rules) or "없음",
                requirements=self._format_requirements(requirements),
                scan_summary=self._format_scan_summary(scan_result, affected_files),
                affected_files=", ".join(affected_files) if affected_files else "전체",
                trigger=trigger,
                mismatch_note=format_mismatches(mismatches),
            ),
            last_error,
        )

        response = await self.llm.chat(
            system_prompt=self.prompts.system(),
            user_prompt=user_prompt,
        )

        scenarios, confidence = parse_response(
            response.content, trigger, affected_files, domain_rules
        )

        save_scenarios(scenarios)

        self.logger.info(
            "scenarios_generated",
            count=len(scenarios),
            tc_count=sum(len(s["test_cases"]) for s in scenarios),
            mismatch_count=len(mismatches),
            confidence=confidence,
        )

        return ExecuteResult(
            result={"scenarios": scenarios, "prd_code_mismatches": mismatches},
            confidence=confidence,
        )

    async def _fetch_scan_result(self) -> dict:
        try:
            return await self.use_tool("codebase_scanner", {"action": "scan"})
        except Exception:
            return {}

    async def _fetch_domain_rules(self, query: str) -> list:
        try:
            result = await self.use_tool(
                "domain_knowledge",
                {"action": "search", "query": query, "top_k": 5},
            )
            return result.get("rules", [])
        except Exception:
            return []

    def _format_requirements(self, requirements: list) -> str:
        if not requirements:
            return "없음"
        return "\n".join(
            f"[{r['req_id']}] ({r['req_type']}/{r['priority']}) {r['content']} [도메인: {r['domain_area']}]"
            for r in requirements
        )

    def _format_scan_summary(self, scan_result: dict, affected_files: list[str]) -> str:
        if not scan_result:
            return "코드베이스 정보 없음"

        files: list[dict] = scan_result.get("files", [])
        lines = [
            f"프레임워크: {scan_result.get('framework', 'unknown')} ({scan_result.get('language', 'unknown')})",
            f"API 엔드포인트 수: {scan_result.get('endpoint_count', 0)}",
        ]

        target_files = [f for f in files if f["path"] in affected_files] if affected_files else files[:15]
        endpoints = [
            f"  {ep.get('method', '?')} {ep.get('path', '?')}"
            for f in target_files
            for ep in f.get("endpoints", [])
        ]

        if affected_files:
            lines.append(f"변경 파일: {', '.join(affected_files[:10])}")
        if endpoints:
            lines.append("주요 엔드포인트:")
            lines.extend(endpoints[:20])

        return "\n".join(lines)
