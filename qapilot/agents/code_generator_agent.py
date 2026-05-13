"""Playwright 코드 생성 Agent.

UI 액션/API 매핑 리스트를 Playwright 테스트 코드로
변환하고 trace_id를 주입한다.

담당: D
Created: 2026-05-07
"""

import json
from typing import Any

import tree_sitter_javascript as tsjs
from tree_sitter import Language, Parser

from qapilot.agents.base_agent import BaseAgent
from qapilot.shared.schemas import ExecuteResult


class CodeGeneratorAgent(BaseAgent):
    """Playwright 코드 생성 Agent.

    역할: 액션 시퀀스 → Playwright JS 코드
    입력: List[ActionMapping]
    출력: List[GeneratedCode], confidence
    호출 Tool: 없음
    """

    allowed_tools: list[str] = []

    async def _execute(
        self, context: dict[str, Any], params: dict[str, Any], last_error: str | None = None
    ) -> ExecuteResult:
        action_mappings = context.get("action_mappings") or params.get("action_mappings") or []
        scenarios = context.get("scenarios") or params.get("scenarios") or []
        
        if not action_mappings:
            return ExecuteResult(result={"generated_codes": []}, confidence=1.0)

        system_prompt = self.prompts.system()
        user_prompt = self.with_correction_hint(
            self.prompts.render(
                scenarios=json.dumps(scenarios, ensure_ascii=False),
                action_mappings=json.dumps(action_mappings, ensure_ascii=False)
            ),
            last_error,
        )

        response = await self.llm.chat(system_prompt, user_prompt)
        parsed = json.loads(response.content)
        
        generated_codes = parsed.get("generated_codes", [])
        
        # 구문 검증 로직 적용
        for code_obj in generated_codes:
            code_text = code_obj.get("code", "")
            code_obj["syntax_valid"] = self._validate_syntax(code_text)

        return ExecuteResult(
            result={"generated_codes": generated_codes},
            confidence=parsed.get("confidence", 0.0),
        )

    def _validate_syntax(self, code: str) -> bool:
        """tree_sitter를 활용해 자바스크립트 코드 구문을 검증한다."""
        if not code.strip():
            return False
            
        try:
            lang = Language(tsjs.language())
            parser = Parser(lang)
            tree = parser.parse(bytes(code, "utf8"))
            return not tree.root_node.has_error
        except Exception as e:
            self.logger.warning("syntax_validation_failed", error=str(e))
            return False
