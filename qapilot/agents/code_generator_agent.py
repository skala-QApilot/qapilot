"""Playwright 코드 생성 Agent.

ActionMapping 리스트 → Playwright JS 코드. TC 단위 LLM 호출 + 부분 graceful
(spec §4.5.1 "C(CodeGenerator) generates leniently").

담당: D
이슈 #107 (2026-05-19): 단일 LLM 호출 fail-fast → TC-별 분할로 LENIENT 본격 구현.
한 TC LLM 응답 JSON parse 실패 시 그 TC 만 skip, 나머지 TC 는 정상 생성.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import tree_sitter_javascript as tsjs
from tree_sitter import Language, Parser

from qapilot.agents.base_agent import BaseAgent
from qapilot.shared.schemas import ExecuteResult

# OpenAI rate limit 안전 동시 호출 제한. gpt-4o-mini 의 TPM 한도 + 토큰 사용량 고려.
_MAX_CONCURRENT_LLM_CALLS = 5


class CodeGeneratorAgent(BaseAgent):
    """Playwright 코드 생성 Agent.

    역할: 액션 시퀀스 → Playwright JS 코드 (TC 단위 LLM 호출 + 부분 graceful)
    입력: List[ActionMapping]
    출력: List[GeneratedCode] (성공 TC), List[FailedTC] (skip 사유), confidence
    호출 Tool: 없음
    """

    allowed_tools: list[str] = []

    async def _execute(
        self, context: dict[str, Any], params: dict[str, Any], last_error: str | None = None
    ) -> ExecuteResult:
        action_mappings = context.get("action_mappings") or params.get("action_mappings") or []
        scenarios = context.get("scenarios") or params.get("scenarios") or []

        if not action_mappings:
            return ExecuteResult(result={"generated_codes": [], "failed_tcs": []}, confidence=1.0)

        system_prompt = self.prompts.system()
        tc_to_scenario = _build_tc_to_scenario_index(scenarios)

        sem = asyncio.Semaphore(_MAX_CONCURRENT_LLM_CALLS)
        tasks = [
            self._generate_single(sem, system_prompt, am, tc_to_scenario, last_error)
            for am in action_mappings
        ]
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)

        generated_codes: list[dict] = []
        failed_tcs: list[dict] = []

        for am, outcome in zip(action_mappings, outcomes):
            tc_id = am.get("tc_id") or "unknown"
            if isinstance(outcome, BaseException):
                failed_tcs.append({
                    "tc_id": tc_id,
                    "error_type": type(outcome).__name__,
                    "error": str(outcome),
                })
                self.logger.warning(
                    "codegen_tc_skip",
                    tc_id=tc_id,
                    error_type=type(outcome).__name__,
                    error=str(outcome),
                )
                continue
            generated_codes.append(outcome)

        total = len(action_mappings)
        success = len(generated_codes)
        confidence = success / total if total else 0.0
        self.logger.info(
            "codegen_complete",
            total=total,
            success=success,
            failed=len(failed_tcs),
            confidence=round(confidence, 3),
        )

        return ExecuteResult(
            result={
                "generated_codes": generated_codes,
                "failed_tcs": failed_tcs,
            },
            confidence=confidence,
        )

    async def _generate_single(
        self,
        sem: asyncio.Semaphore,
        system_prompt: str,
        action_mapping: dict[str, Any],
        tc_to_scenario: dict[str, dict],
        last_error: str | None,
    ) -> dict[str, Any]:
        """단일 TC 의 ActionMapping → Playwright JS 코드. 부분 응답 graceful 흡수."""
        tc_id = action_mapping.get("tc_id") or "unknown"
        scenario_slice = tc_to_scenario.get(tc_id)
        scenarios_for_prompt: list[dict] = [scenario_slice] if scenario_slice else []

        async with sem:
            user_prompt = self.with_correction_hint(
                self.prompts.render(
                    scenarios=json.dumps(scenarios_for_prompt, ensure_ascii=False),
                    action_mappings=json.dumps([action_mapping], ensure_ascii=False),
                ),
                last_error,
            )
            response = await self.llm.chat(system_prompt, user_prompt)
            parsed = json.loads(response.content)

        codes = parsed.get("generated_codes") or []
        if not codes:
            # 빈 응답 graceful — LLM 이 confidence 만 반환한 경우
            return {
                "tc_id": tc_id,
                "code": "",
                "self_fix_count": 0,
                "syntax_valid": False,
            }

        code_obj = dict(codes[0])
        code_obj.setdefault("tc_id", tc_id)
        code_obj["syntax_valid"] = self._validate_syntax(code_obj.get("code", ""))
        code_obj.setdefault("self_fix_count", 0)
        return code_obj

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


def _build_tc_to_scenario_index(scenarios: list[dict]) -> dict[str, dict]:
    """tc_id → 해당 TC 가 속한 TS 의 slice (단일 TC 만 포함).

    LLM 호출당 토큰 절감 + 해당 TC 의 비즈니스 의도(given/when/then, name) 보존.
    원본 TS 의 메타 (ts_id, name, depends_on, affected_files) 는 유지하고 test_cases 만 1건으로 슬라이스.
    """
    index: dict[str, dict] = {}
    for ts in scenarios or []:
        for tc in ts.get("test_cases") or []:
            tc_id = tc.get("tc_id")
            if not tc_id:
                continue
            sliced = dict(ts)
            sliced["test_cases"] = [tc]
            index[tc_id] = sliced
    return index
