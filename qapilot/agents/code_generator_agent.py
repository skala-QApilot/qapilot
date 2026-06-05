"""Playwright 코드 생성 Agent.

ActionMapping 리스트 → Playwright JS 코드. TC 단위 LLM 호출 + 부분 graceful
(spec §4.5.1 "C(CodeGenerator) generates leniently").

담당: D
이슈 #107 (2026-05-19): 단일 LLM 호출 fail-fast → TC-별 분할로 LENIENT 본격 구현.
한 TC LLM 응답 JSON parse 실패 시 그 TC 만 skip, 나머지 TC 는 정상 생성.
"""

from __future__ import annotations

import asyncio
import difflib
import json
from pathlib import Path
from typing import Any

import tree_sitter_javascript as tsjs
from tree_sitter import Language, Parser

from qapilot.agents.base_agent import BaseAgent
from qapilot.shared.llm_client import LLMClient
from qapilot.shared.schemas import ExecuteResult
from qapilot.tools.frontend_dom_scanner import load_frontend_index

# OpenAI rate limit 안전 동시 호출 제한. gpt-4o-mini 의 TPM 한도 + 토큰 사용량 고려.
_MAX_CONCURRENT_LLM_CALLS = 5
_FUZZY_MATCH_THRESHOLD = 0.6
_FUZZY_MATCH_SUBSTRING_BONUS = 0.2


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
        frontend_dom = self._load_frontend_dom(context)
        action_mappings = context.get("action_mappings") or params.get("action_mappings") or []
        scenarios = context.get("scenarios") or params.get("scenarios") or []
        action_mappings = [
            self._normalize_mapping_with_frontend_index(am, frontend_dom)
            for am in action_mappings
        ]

        if not action_mappings:
            return ExecuteResult(result={"generated_codes": [], "failed_tcs": []}, confidence=1.0)

        system_prompt = self.prompts.system()
        tc_to_scenario = _build_tc_to_scenario_index(scenarios)

        sem = asyncio.Semaphore(_MAX_CONCURRENT_LLM_CALLS)
        tasks = [
            self._generate_single(sem, system_prompt, am, tc_to_scenario, frontend_dom, last_error)
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

    def _create_tc_llm(self) -> LLMClient:
        """이슈 #140: per-TC subtask LLM client. 테스트 override point.

        ActionMapper 와 동일 패턴 — `self.llm` 의 누적 토큰이 다른 TC 호출에
        누적되어 SYSTEM_002 임계 도달 시 후반 TC skip 되던 잠재 문제 차단.
        PR #130 / 이슈 #107 의 \"한 TC = 1 task\" 의도 정합.
        """
        return LLMClient(self._config.llm, trace_id=self.trace_id)

    async def _generate_single(
        self,
        sem: asyncio.Semaphore,
        system_prompt: str,
        action_mapping: dict[str, Any],
        tc_to_scenario: dict[str, dict],
        frontend_dom: list[dict],
        last_error: str | None,
    ) -> dict[str, Any]:
        """단일 TC 의 ActionMapping → Playwright JS 코드. 부분 응답 graceful 흡수.

        이슈 #140 (2026-05-21): per-TC LLMClient 인스턴스 분리.
        ActionMapper 와 동일 패턴 — `self.llm.total_tokens` 누적이 다른 TC 호출에
        누적되어 `max_tokens_per_task` (config 200000) 임계 도달 시 SYSTEM_002 로
        후반 TC skip 되던 잠재 문제 차단 (본 e2e 에선 136K 로 미발현, 단 시나리오
        규모 증가 시 ActionMapper 와 동일 사례 발생 잠재). self.llm 은
        agent_complete 보고용 누적 합산만 유지.
        """
        tc_id = action_mapping.get("tc_id") or "unknown"
        scenario_slice = tc_to_scenario.get(tc_id)
        scenarios_for_prompt: list[dict] = [scenario_slice] if scenario_slice else []

        # 이슈 #140: TC 마다 새 LLMClient — 누적 정책 충돌 해결
        tc_llm = self._create_tc_llm()

        async with sem:
            user_prompt = self.with_correction_hint(
                self.prompts.render(
                    scenarios=json.dumps(scenarios_for_prompt, ensure_ascii=False),
                    action_mappings=json.dumps([action_mapping], ensure_ascii=False),
                    frontend_dom=self._format_frontend_dom(frontend_dom),
                ),
                last_error,
            )
            response = await tc_llm.chat(system_prompt, user_prompt)
            parsed = json.loads(response.content)

        # agent_complete 보고용 누적 합산 (이슈 #140)
        self.llm.total_input_tokens += tc_llm.total_input_tokens
        self.llm.total_output_tokens += tc_llm.total_output_tokens
        self.llm.total_cost_usd = round(self.llm.total_cost_usd + tc_llm.total_cost_usd, 6)

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

    def _load_frontend_dom(self, context: dict[str, Any]) -> list[dict]:
        ctx_dom = context.get("frontend_dom")
        if isinstance(ctx_dom, list):
            return ctx_dom
        qapilot_dir = context.get("qapilot_dir")
        if qapilot_dir:
            return load_frontend_index(Path(str(qapilot_dir)) / "codebase-index" / "frontend.json")
        return []

    def _format_frontend_dom(self, elements: list[dict]) -> str:
        if not elements:
            return "인덱스 없음"
        lines: list[str] = []
        for el in elements[:120]:
            parts = [f"<{el.get('tag') or 'element'}>"]
            for key in ("text", "label", "placeholder", "testid", "id", "file"):
                val = (el.get(key) or "").strip()
                if val:
                    parts.append(f'{key}="{val}"')
            lines.append("- " + " ".join(parts))
        return "\n".join(lines)

    def _normalize_mapping_with_frontend_index(
        self, action_mapping: dict[str, Any], frontend_dom: list[dict]
    ) -> dict[str, Any]:
        if not frontend_dom:
            return action_mapping
        mapping = dict(action_mapping)
        steps = []
        for step in mapping.get("steps") or []:
            steps.append(self._normalize_step_with_frontend_index(dict(step), frontend_dom))
        mapping["steps"] = steps
        return mapping

    def _normalize_step_with_frontend_index(self, step: dict[str, Any], frontend_dom: list[dict]) -> dict[str, Any]:
        selector = (step.get("selector") or "").strip()
        selector_type = step.get("selector_type")
        action = step.get("action") or ""
        if not selector:
            return step

        best = self._best_frontend_match(selector, frontend_dom)
        if not best:
            return step
        element, score = best
        if score < _FUZZY_MATCH_THRESHOLD:
            return step

        preferred = self._preferred_selector_for_action(action, element)
        if not preferred:
            return step
        preferred_type, preferred_value = preferred

        # fill 계열은 text selector 금지에 가깝게 보정
        if action in {"fill", "clear", "select", "press", "upload"}:
            step["selector_type"] = preferred_type
            step["selector"] = preferred_value
            return step

        # click/check 류도 더 안정적인 testid/label 이 있으면 교체
        if action in {"click", "dblclick", "hover", "check", "uncheck"}:
            if preferred_type in {"testid", "label", "placeholder"} or selector_type == "text":
                step["selector_type"] = preferred_type
                step["selector"] = preferred_value
            return step

        return step

    def _best_frontend_match(self, selector: str, frontend_dom: list[dict]) -> tuple[dict, float] | None:
        target = selector.lower()
        best_el: dict | None = None
        best_score = 0.0
        for el in frontend_dom:
            for key in ("label", "text", "placeholder", "testid", "id"):
                cand = (el.get(key) or "").strip().lower()
                if not cand:
                    continue
                score = difflib.SequenceMatcher(None, target, cand).ratio()
                if target == cand:
                    score = 1.0
                elif target in cand or cand in target:
                    score += _FUZZY_MATCH_SUBSTRING_BONUS
                if score > best_score:
                    best_score = score
                    best_el = el
        if best_el is None:
            return None
        return best_el, best_score

    def _preferred_selector_for_action(self, action: str, element: dict) -> tuple[str, str] | None:
        if action in {"fill", "clear", "select", "press", "upload"}:
            priority = ("testid", "label", "placeholder", "text")
        elif action in {"click", "dblclick", "hover", "check", "uncheck"}:
            priority = ("testid", "text", "label", "placeholder")
        else:
            priority = ("testid", "text", "label", "placeholder")
        for key in priority:
            val = (element.get(key) or "").strip()
            if val:
                return key, val
        return None

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
