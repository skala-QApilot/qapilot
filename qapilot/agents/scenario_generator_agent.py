"""시나리오 생성 Agent.

코드베이스 분석 결과와 Git diff를 기반으로
테스트 시나리오를 자동 생성한다.

담당: B
Created: 2026-05-07
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from qapilot.agents.base_agent import BaseAgent
from qapilot.shared.errors import AgentExecutionError, ErrorCode
from qapilot.shared.schemas import ExecuteResult, TestCase, TestScenario, TestValue

_SCENARIOS_DIR = Path(".qapilot/scenarios")

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

        # scan_result 없으면 Tool 직접 호출
        if not scan_result:
            scan_result = await self._fetch_scan_result()

        # domain_rules 없으면 Tool 직접 호출
        if not domain_rules:
            query = " ".join(r.get("content", "")[:60] for r in requirements[:3]) or "테스트 시나리오"
            domain_rules = await self._fetch_domain_rules(query)

        # --affected: Git diff 기반 영향 파일 필터링
        affected_files: list[str] = []
        if affected_only:
            git_diff = scan_result.get("git_diff") or {}
            affected_files = git_diff.get("changed_files", [])

        # PRD-코드 불일치 탐지
        mismatches = self._detect_prd_code_mismatch(requirements, scan_result)

        # 프롬프트 렌더링
        from qapilot.tools.domain_knowledge import DomainKnowledgeTool

        user_prompt = self.with_correction_hint(
            self.prompts.render(
                domain_rules=DomainKnowledgeTool.format_rules_for_prompt(domain_rules) or "없음",
                requirements=self._format_requirements(requirements),
                scan_summary=self._format_scan_summary(scan_result, affected_files),
                affected_files=", ".join(affected_files) if affected_files else "전체",
                trigger=trigger,
                mismatch_note=self._format_mismatches(mismatches),
            ),
            last_error,
        )

        response = await self.llm.chat(
            system_prompt=self.prompts.system(),
            user_prompt=user_prompt,
        )

        scenarios, confidence = self._parse_response(
            response.content, trigger, affected_files, domain_rules
        )

        self._save_scenarios(scenarios)

        self.logger.info(
            "scenarios_generated",
            count=len(scenarios),
            tc_count=sum(len(s["test_cases"]) for s in scenarios),
            mismatch_count=len(mismatches),
            confidence=confidence,
        )

        return ExecuteResult(
            result={
                "scenarios": scenarios,
                "prd_code_mismatches": mismatches,
            },
            confidence=confidence,
        )

    # ── Tool 호출 ──────────────────────────────────────────────────────────────

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

    # ── 입력 포매팅 ────────────────────────────────────────────────────────────

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

        framework = scan_result.get("framework", "unknown")
        language = scan_result.get("language", "unknown")
        endpoint_count = scan_result.get("endpoint_count", 0)
        files: list[dict] = scan_result.get("files", [])

        lines = [
            f"프레임워크: {framework} ({language})",
            f"API 엔드포인트 수: {endpoint_count}",
        ]

        target_files = [f for f in files if f["path"] in affected_files] if affected_files else files[:15]

        endpoints: list[str] = []
        for f in target_files:
            for ep in f.get("endpoints", []):
                endpoints.append(f"  {ep.get('method', '?')} {ep.get('path', '?')}")

        if affected_files:
            lines.append(f"변경 파일: {', '.join(affected_files[:10])}")
        if endpoints:
            lines.append("주요 엔드포인트:")
            lines.extend(endpoints[:20])

        return "\n".join(lines)

    # ── PRD-코드 불일치 탐지 ───────────────────────────────────────────────────

    def _detect_prd_code_mismatch(
        self, requirements: list, scan_result: dict
    ) -> list[dict]:
        """high priority 요구사항의 도메인 영역이 코드에 있는지 키워드로 확인한다."""
        if not requirements or not scan_result:
            return []

        files: list[dict] = scan_result.get("files", [])
        all_paths = " ".join(f["path"].lower() for f in files)
        all_endpoints = " ".join(
            ep.get("path", "").lower()
            for f in files
            for ep in f.get("endpoints", [])
        )
        code_text = all_paths + " " + all_endpoints

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

    def _format_mismatches(self, mismatches: list[dict]) -> str:
        if not mismatches:
            return "없음"
        lines = ["⚠️ PRD-코드 불일치 (high priority 요구사항 미구현 의심):"]
        lines += [f"  [{m['req_id']}] {m['note']}" for m in mismatches]
        return "\n".join(lines)

    # ── 파싱 ───────────────────────────────────────────────────────────────────

    def _parse_response(
        self,
        content: str,
        trigger: str,
        affected_files: list[str],
        domain_rules: list,
    ) -> tuple[list[TestScenario], float]:
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

        # TC 중복 제거 (given+when+then 기준)
        seen: set[str] = set()
        scenarios: list[TestScenario] = []

        for i, s in enumerate(data["scenarios"]):
            unique_tcs: list[TestCase] = []
            for j, tc in enumerate(s.get("test_cases", [])):
                key = f"{tc.get('given','')}|{tc.get('when','')}|{tc.get('then','')}"
                if key in seen:
                    continue
                seen.add(key)

                ts_id = f"TS-{i + 1:03d}"
                tc_id = f"{ts_id}-TC-{j + 1:02d}"

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
                        "tc_id": tc_id,
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

            ts_id = f"TS-{i + 1:03d}"
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

    # ── 저장 ───────────────────────────────────────────────────────────────────

    def _save_scenarios(self, scenarios: list[TestScenario]) -> None:
        """각 시나리오를 .qapilot/scenarios/{ts_id}.json에 저장한다."""
        _SCENARIOS_DIR.mkdir(parents=True, exist_ok=True)
        for scenario in scenarios:
            path = _SCENARIOS_DIR / f"{scenario['ts_id']}.json"
            path.write_text(
                json.dumps(scenario, ensure_ascii=False, indent=2), encoding="utf-8"
            )
