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
import re
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
_ENV_REF_RE = re.compile(r"^process\.env\.[A-Z0-9_]+$")
_SEMANTIC_ALIASES: dict[str, tuple[str, ...]] = {
    "회원가입": ("signup", "가입", "create account"),
    "가입": ("signup", "회원가입"),
    "로그인": ("login", "signin"),
    "이메일": ("email", "mail"),
    "비밀번호": ("password", "pwd", "pass"),
    "이름": ("name", "user name", "username"),
    "생년월일": ("birth", "birth date", "birth_date"),
    "버튼": ("button", "submit"),
}


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
        # source of truth 는 ActionMapping. CodeGenerator 단계에서는 frontend.json 재매핑 금지.
        frontend_dom: list[dict] = []

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

        deterministic_code = self._render_generated_code(action_mapping, scenario_slice)
        if deterministic_code is not None:
            return {
                "tc_id": tc_id,
                "code": deterministic_code,
                "self_fix_count": 0,
                "syntax_valid": self._validate_syntax(deterministic_code),
            }

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

    def _render_generated_code(
        self, action_mapping: dict[str, Any], scenario_slice: dict[str, Any] | None
    ) -> str | None:
        steps = list(action_mapping.get("steps") or [])
        if not steps:
            return None

        tc_id = str(action_mapping.get("tc_id") or "unknown")
        tc = (scenario_slice or {}).get("test_cases", [{}])[0] if scenario_slice else {}
        title = str(tc.get("name") or tc_id)
        comments = [tc.get("given"), tc.get("when"), tc.get("then")]

        lines = ["const { test, expect } = require('@playwright/test');", ""]
        if comments[0]:
            lines.append(f"// Given: {comments[0]}")
        if comments[1]:
            lines.append(f"// When: {comments[1]}")
        if comments[2]:
            lines.append(f"// Then: {comments[2]}")
        if any(comments):
            lines.append("")
        lines.append(f"test({json.dumps(title, ensure_ascii=False)}, async ({{ page }}) => {{")

        _TRIGGER_ACTIONS = {"navigate", "click", "dblclick"}
        i = 0
        while i < len(steps):
            step = steps[i]
            action = str(step.get("action") or "")

            # navigate/click + wait_for_response 쌍 → Promise.all 패턴으로 병합
            if (
                action in _TRIGGER_ACTIONS
                and i + 1 < len(steps)
                and str(steps[i + 1].get("action") or "") == "wait_for_response"
            ):
                next_step = steps[i + 1]
                response_url = self._js_value(next_step.get("value"))
                trigger = self._render_step(step)
                if trigger:
                    # "await X;" → "X," (await·세미콜론 제거)
                    trigger_expr = str(trigger).lstrip().removeprefix("await ").rstrip(";")
                    lines.extend([
                        f"  await Promise.all([",
                        f"    page.waitForResponse({response_url}),",
                        f"    {trigger_expr},",
                        f"  ]);",
                    ])
                i += 2
                continue

            # 트리거 없는 단독 wait_for_response → networkidle fallback
            if action == "wait_for_response":
                lines.append("  await page.waitForLoadState('networkidle');")
                i += 1
                continue

            rendered = self._render_step(step)
            if not rendered:
                i += 1
                continue
            if isinstance(rendered, list):
                lines.extend(f"  {line}" for line in rendered)
            else:
                lines.append(f"  {rendered}")
            i += 1

        lines.append("});")
        return "\n".join(lines)

    def _render_step(self, step: dict[str, Any]) -> str | list[str] | None:
        action = str(step.get("action") or "")
        selector = step.get("selector")
        selector_type = step.get("selector_type")
        value = step.get("value")
        expected = step.get("expected")

        if action == "navigate":
            return f"await page.goto({self._js_value(value)});"
        if action == "reload":
            return "await page.reload();"
        if action == "go_back":
            return "await page.goBack();"
        if action == "go_forward":
            return "await page.goForward();"
        if action == "wait":
            if self._is_number_like(value):
                return f"await page.waitForTimeout({self._js_number(value)});"
            return "await page.waitForLoadState('networkidle');"
        if action == "wait_for_url":
            return f"await page.waitForURL({self._js_value(value)});"
        if action == "wait_for_load_state":
            return f"await page.waitForLoadState({self._js_value(value or 'networkidle')});"
        if action == "wait_for_response":
            return f"await page.waitForResponse({self._js_value(value)});"
        if action == "assert_url":
            return f"await expect(page).toHaveURL({self._js_value(expected)});"

        locator = self._locator_expr(selector_type, selector)
        if not locator:
            return f"test.skip(true, {json.dumps(f'missing selector for action: {action}', ensure_ascii=False)});"

        if action == "fill":
            return f"await {locator}.fill({self._js_value(self._sanitize_fill_value(selector, value))});"
        if action == "clear":
            return f"await {locator}.clear();"
        if action == "click":
            return f"await {locator}.click();"
        if action == "dblclick":
            return f"await {locator}.dblclick();"
        if action == "hover":
            return f"await {locator}.hover();"
        if action == "select":
            return f"await {locator}.selectOption({self._js_value(value)});"
        if action == "check":
            return f"await {locator}.check();"
        if action == "uncheck":
            return f"await {locator}.uncheck();"
        if action == "press":
            return f"await {locator}.press({self._js_value(value)});"
        if action == "upload":
            return f"await {locator}.setInputFiles({self._js_value(value)});"
        if action in {"assert", "assert_visible"}:
            return f"await expect({locator}).toBeVisible();"
        if action == "assert_hidden":
            return f"await expect({locator}).toBeHidden();"
        if action == "assert_text":
            return f"await expect({locator}).toHaveText({self._js_value(expected)});"
        if action == "assert_value":
            return f"await expect({locator}).toHaveValue({self._js_value(expected)});"
        if action == "assert_enabled":
            return f"await expect({locator}).toBeEnabled();"
        if action == "assert_disabled":
            return f"await expect({locator}).toBeDisabled();"
        if action == "assert_count":
            return f"await expect({locator}).toHaveCount({self._js_number(expected)});"

        return [
            f"// TODO: unsupported action {json.dumps(action, ensure_ascii=False)}",
            f"test.skip(true, {json.dumps(f'unsupported action: {action}', ensure_ascii=False)});",
        ]

    def _locator_expr(self, selector_type: Any, selector: Any) -> str | None:
        sel_type = str(selector_type or "").strip().lower()
        sel = (selector or "").strip() if selector is not None else ""
        if not sel:
            return None
        if sel_type == "testid":
            return f"page.getByTestId({self._js_value(sel)})"
        if sel_type == "label":
            return f"page.getByLabel({self._js_value(sel)})"
        if sel_type == "placeholder":
            return f"page.getByPlaceholder({self._js_value(sel)})"
        if sel_type == "text":
            return f"page.getByText({self._js_value(sel)})"
        if sel_type == "alttext":
            return f"page.getByAltText({self._js_value(sel)})"
        if sel_type == "title":
            return f"page.getByTitle({self._js_value(sel)})"
        if sel_type in {"css", "xpath"}:
            return f"page.locator({self._js_value(sel)})"
        return f"page.getByText({self._js_value(sel)})"

    def _sanitize_fill_value(self, selector: Any, value: Any) -> Any:
        selector_text = str(selector or "").lower()
        if isinstance(value, str) and _ENV_REF_RE.match(value.strip()):
            return value.strip()
        if any(token in selector_text for token in ("비밀번호", "password", "pwd", "pass")):
            return "process.env.E2E_USER_PASSWORD"
        # signup 이메일 필드: 정적 주소는 재실행 시 중복 오류 발생 → Date.now() 유니크값 사용
        if any(token in selector_text for token in ("email", "이메일", "mail")):
            val_str = str(value or "")
            if "@" in val_str and not val_str.startswith("process.env"):
                local, domain = val_str.rsplit("@", 1)
                return f"`{local}_${{Date.now()}}@{domain}`"
        return value

    def _js_value(self, value: Any) -> str:
        if isinstance(value, str):
            stripped = value.strip()
            if _ENV_REF_RE.match(stripped):
                return stripped
            # JS template literal (`...`) 은 그대로 반환
            if stripped.startswith("`") and stripped.endswith("`"):
                return stripped
        return json.dumps("" if value is None else value, ensure_ascii=False)

    def _is_number_like(self, value: Any) -> bool:
        try:
            float(str(value))
            return True
        except (TypeError, ValueError):
            return False

    def _js_number(self, value: Any) -> str:
        if self._is_number_like(value):
            num = float(str(value))
            return str(int(num)) if num.is_integer() else str(num)
        return "0"

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
        route_hint = self._extract_route_hint(mapping)
        steps = []
        for step in mapping.get("steps") or []:
            steps.append(self._normalize_step_with_frontend_index(dict(step), frontend_dom, route_hint))
        mapping["steps"] = steps
        return mapping

    def _normalize_step_with_frontend_index(
        self, step: dict[str, Any], frontend_dom: list[dict], route_hint: str | None
    ) -> dict[str, Any]:
        selector = (step.get("selector") or "").strip()
        selector_type = step.get("selector_type")
        action = step.get("action") or ""

        # 이미 frontend index 에 존재하는 유효 selector 는 codegen 에서 재매핑하지 않는다.
        if self._selector_exists_in_frontend(selector_type, selector, frontend_dom, route_hint=route_hint):
            return step

        target_hint = self._step_target_hint(step)
        lookup_target = target_hint or selector
        if not lookup_target:
            return step

        best = self._best_frontend_match(lookup_target, frontend_dom, action=action, route_hint=route_hint)
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

    def _selector_exists_in_frontend(
        self,
        selector_type: Any,
        selector: Any,
        frontend_dom: list[dict],
        *,
        route_hint: str | None = None,
    ) -> bool:
        sel_type = str(selector_type or "").strip().lower()
        sel = str(selector or "").strip()
        if not sel_type or not sel:
            return False

        key_map = {
            "testid": "testid",
            "label": "label",
            "placeholder": "placeholder",
            "text": "text",
            "css": None,
            "xpath": None,
            "alttext": None,
            "title": None,
        }
        lookup_key = key_map.get(sel_type)
        if not lookup_key:
            return False

        for el in frontend_dom:
            value = str(el.get(lookup_key) or "").strip()
            if value != sel:
                continue
            if route_hint:
                el_route = str(el.get("route") or "").strip()
                if el_route and el_route != route_hint:
                    continue
            return True
        return False

    def _step_target_hint(self, step: dict[str, Any]) -> str:
        parts = [
            str(step.get("target_name") or "").strip(),
            str(step.get("target_kind") or "").strip(),
            str(step.get("expected") or "").strip() if step.get("action") in {"assert", "assert_visible", "assert_text"} else "",
        ]
        return " ".join(part for part in parts if part)

    def _best_frontend_match(
        self, selector: str, frontend_dom: list[dict], *, action: str = "", route_hint: str | None = None
    ) -> tuple[dict, float] | None:
        target = selector.lower()
        normalized_target = self._expand_aliases(target)
        best_el: dict | None = None
        best_score = 0.0
        for el in frontend_dom:
            for key in ("label", "text", "placeholder", "testid", "id", "page", "route"):
                cand = (el.get(key) or "").strip().lower()
                if not cand:
                    continue
                score = difflib.SequenceMatcher(None, normalized_target, self._expand_aliases(cand)).ratio()
                if target == cand:
                    score = 1.0
                elif target in cand or cand in target:
                    score += _FUZZY_MATCH_SUBSTRING_BONUS
                score += self._semantic_bonus(target, cand)
                score += self._action_bonus(action, el, target)
                if route_hint and str(el.get("route") or "").strip() == route_hint:
                    score += 0.4
                page = str(el.get("page") or "").strip().lower()
                if route_hint and page and route_hint.strip("/").lower() == page.lower():
                    score += 0.2
                if score > best_score:
                    best_score = score
                    best_el = el
        if best_el is None:
            return None
        return best_el, best_score

    def _extract_route_hint(self, action_mapping: dict[str, Any]) -> str | None:
        for step in action_mapping.get("steps") or []:
            if step.get("action") == "navigate":
                value = str(step.get("value") or "").strip()
                if value.startswith("/"):
                    return value
        return None

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

    def _expand_aliases(self, text: str) -> str:
        expanded = [text]
        for token, aliases in _SEMANTIC_ALIASES.items():
            if token in text:
                expanded.extend(aliases)
        return " ".join(expanded)

    def _semantic_bonus(self, target: str, candidate: str) -> float:
        bonus = 0.0
        for token, aliases in _SEMANTIC_ALIASES.items():
            if token in target and any(alias in candidate for alias in aliases):
                bonus += 0.25
            if any(alias in target for alias in aliases) and token in candidate:
                bonus += 0.25
        return bonus

    def _action_bonus(self, action: str, element: dict, target: str) -> float:
        control_type = str(element.get("control_type") or "").lower()
        bonus = 0.0
        if action in {"fill", "clear", "select", "press", "upload"} and control_type in {"form_input", "select", "textarea"}:
            bonus += 0.35
        if action in {"click", "dblclick", "hover", "check", "uncheck"} and control_type in {"button", "submit", "link", "checkbox", "radio"}:
            bonus += 0.35
        if "버튼" in target and control_type in {"button", "submit"}:
            bonus += 0.2
        return bonus

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
