"""시나리오-액션 매핑 Agent.

구조화된 시나리오를 UI 액션 리스트, API 매핑,
검증 포인트로 분해하고 실제 셀렉터/엔드포인트와 매칭한다.

담당: C
Created: 2026-05-07
"""

from __future__ import annotations

import json
import re
from typing import Any

from qapilot.agents.base_agent import BaseAgent
from qapilot.shared.errors import AgentExecutionError, ErrorCode
from qapilot.shared.schemas import ActionMapping, ActionStep, ExecuteResult


MAX_TC_PER_BATCH = 50
MAX_TS_PER_BATCH = 10

_SELECTOR_REQUIRED_ACTIONS = {
    "fill", "clear", "click", "dblclick", "hover", "select", "check", "uncheck",
    "press", "upload", "assert", "assert_visible", "assert_hidden", "assert_text",
    "assert_value", "assert_enabled", "assert_disabled", "assert_count",
}
_SELECTOR_OPTIONAL_ACTIONS = {
    "navigate", "reload", "go_back", "go_forward", "wait", "wait_for_url",
    "wait_for_load_state", "wait_for_response", "assert_url",
}
_ALLOWED_ACTIONS = _SELECTOR_REQUIRED_ACTIONS | _SELECTOR_OPTIONAL_ACTIONS
_ALLOWED_SELECTOR_TYPES = {
    "role", "label", "placeholder", "text", "testid",
    "alttext", "title", "css", "xpath",
}
_ACTION_ALIASES = {
    "input": "fill", "type": "fill", "enter_text": "fill",
    "tap": "click", "press_button": "click", "submit": "click",
    "double_click": "dblclick", "mouseover": "hover",
    "verify": "assert", "expect": "assert", "should_see": "assert",
    "check_text": "assert_text", "check_url": "assert_url",
    "go": "navigate", "open": "navigate", "visit": "navigate",
    "refresh": "reload", "back": "go_back", "forward": "go_forward",
    "choose": "select", "dropdown": "select", "pause": "wait", "sleep": "wait",
    "upload_file": "upload", "set_input_files": "upload",
}
_SELECTOR_TYPE_ALIASES = {
    "aria": "role", "accessible_name": "role", "data-testid": "testid",
    "data_testid": "testid", "data-test-id": "testid", "data_test_id": "testid",
    "query": "css", "locator": "css", "id": "css", "class": "css",
    "text_content": "text", "contains_text": "text", "alt": "alttext",
    "alt_text": "alttext",
}
_VALUE_REQUIRED_ACTIONS = {
    "fill", "select", "press", "upload", "navigate", "wait", "wait_for_url",
    "wait_for_load_state", "wait_for_response",
}
_EXPECTED_REQUIRED_ACTIONS = {
    "assert", "assert_visible", "assert_hidden", "assert_text", "assert_value",
    "assert_url", "assert_enabled", "assert_disabled", "assert_count",
}


class ActionMapperAgent(BaseAgent):
    """시나리오-액션 매핑 Agent.

    역할: 시나리오 → UI액션+API매핑+검증포인트 분해
    입력: TestScenario, CodebaseContext (endpoints.json)
    출력: List[ActionMapping], confidence
    호출 Tool: 코드 인덱스 Tool
    """

    allowed_tools: list[str] = []

    async def _execute(
        self,
        context: dict[str, Any],
        params: dict[str, Any],
        last_error: str | None = None,
    ) -> ExecuteResult:
        """테스트 시나리오를 ActionMapping 목록으로 변환한다.

        Args:
            context: scenarios, scan_result를 포함한 파이프라인 컨텍스트.
            params: 실행 파라미터. 현재 직접 사용하지 않는다.
            last_error: 이전 재시도 오류 메시지.

        Returns:
            ExecuteResult: action_mappings와 confidence.
        """
        scan_result = context.get("scan_result")
        endpoints = self._extract_endpoints(scan_result)
        scenarios = context.get("scenarios") or []
        if not scenarios:
            return ExecuteResult(result={"action_mappings": []}, confidence=1.0)

        all_mappings: list[ActionMapping] = []
        for batch in self._build_batches(scenarios):
            mappings = await self._call_batch(batch, endpoints, last_error)
            all_mappings.extend(mappings)

        confidence = self._calc_confidence(all_mappings, scan_result is not None)
        self.logger.info(
            "action_mappings_generated",
            mappings=len(all_mappings),
            confidence=confidence,
        )
        return ExecuteResult(
            result={"action_mappings": all_mappings},
            confidence=confidence,
        )

    def _extract_endpoints(self, scan_result: dict[str, Any] | None) -> list[dict]:
        """ScanResult에서 프롬프트용 엔드포인트 목록을 추출한다.

        Args:
            scan_result: 코드베이스 스캔 결과.

        Returns:
            list[dict]: method, path, params, response_model 목록.
        """
        if not scan_result:
            self.logger.warning("scan_result_missing")
            return []

        endpoints: list[dict] = []
        for file_info in scan_result.get("files", []):
            for endpoint in file_info.get("endpoints", []):
                endpoints.append({
                    "method": endpoint.get("method", ""),
                    "path": endpoint.get("path", ""),
                    "params": endpoint.get("params", []),
                    "response_model": endpoint.get("response_model", ""),
                })
        return endpoints

    def _build_batches(self, scenarios: list[dict]) -> list[list[dict]]:
        """TS 기준으로 LLM 호출 배치를 구성한다."""
        batches: list[list[dict]] = []
        current_batch: list[dict] = []
        current_tc_count = 0

        for ts in scenarios:
            ts_tc_count = len(ts.get("test_cases", []))
            if ts_tc_count > MAX_TC_PER_BATCH:
                self.logger.warning(
                    "ts_exceeds_batch_limit",
                    ts_id=ts.get("ts_id", ""),
                    tc_count=ts_tc_count,
                )
            exceeds_tc = current_tc_count + ts_tc_count > MAX_TC_PER_BATCH
            exceeds_ts = len(current_batch) >= MAX_TS_PER_BATCH
            if (exceeds_tc or exceeds_ts) and current_batch:
                batches.append(current_batch)
                current_batch = []
                current_tc_count = 0
            current_batch.append(ts)
            current_tc_count += ts_tc_count

        if current_batch:
            batches.append(current_batch)
        return batches

    async def _call_batch(
        self, batch: list[dict], endpoints: list[dict], last_error: str | None
    ) -> list[ActionMapping]:
        """단일 배치를 LLM으로 ActionMapping 변환한다."""
        user_prompt = self.prompts.render(
            scenarios=self._format_scenarios(batch),
            endpoints=self._format_endpoints(endpoints),
        )
        user_prompt = self.with_correction_hint(user_prompt, last_error)
        response = await self.llm.chat(
            system_prompt=self.prompts.system(),
            user_prompt=user_prompt,
        )
        return self._parse_action_mappings(response.content)

    def _format_scenarios(self, batch: list[dict]) -> str:
        """시나리오 배치를 JSON 문자열로 직렬화한다."""
        return json.dumps(batch, ensure_ascii=False, indent=2)

    def _format_endpoints(self, endpoints: list[dict]) -> str:
        """엔드포인트 목록을 프롬프트용 문자열로 변환한다."""
        if not endpoints:
            return "엔드포인트 없음"
        return "\n".join(
            f"- {ep.get('method', '?')} {ep.get('path', '?')}"
            f" params={ep.get('params', [])}"
            f" response_model={ep.get('response_model', '')}"
            for ep in endpoints
        )

    def _parse_action_mappings(self, content: str) -> list[ActionMapping]:
        """LLM 응답에서 ActionMapping JSON 배열을 파싱한다."""
        try:
            cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", content.strip())
            data = json.loads(cleaned)
            if not isinstance(data, list):
                raise ValueError("응답은 JSON 배열이어야 합니다.")
            return [self._validate_mapping(item) for item in data]
        except Exception as e:
            raise AgentExecutionError(
                ErrorCode.AGENT_004,
                f"ActionMapping 파싱 실패: {e}",
            ) from e

    def _validate_mapping(self, item: Any) -> ActionMapping:
        """단일 ActionMapping 구조를 검증한다."""
        required = ("tc_id", "steps", "selector_confidence")
        if not isinstance(item, dict):
            raise ValueError("ActionMapping 항목은 객체여야 합니다.")
        missing = [field for field in required if field not in item]
        if missing:
            raise ValueError(f"필수 필드 누락: {', '.join(missing)}")
        if not isinstance(item["steps"], list):
            raise ValueError("steps는 배열이어야 합니다.")
        tc_id = str(item["tc_id"])
        return {
            "tc_id": tc_id,
            "steps": [self._validate_step(step, tc_id) for step in item["steps"]],
            "selector_confidence": float(item["selector_confidence"]),
        }

    def _validate_step(self, item: Any, tc_id: str) -> ActionStep:
        """단일 ActionStep 구조와 enum 값을 검증한다."""
        required = ("step_no", "action", "selector", "selector_type")
        if not isinstance(item, dict):
            raise ValueError("ActionStep 항목은 객체여야 합니다.")
        missing = [field for field in required if field not in item]
        if missing:
            raise ValueError(f"{tc_id} step 필수 필드 누락: {', '.join(missing)}")
        step_no = int(item["step_no"])
        action = self._normalize_action(item, tc_id, step_no)
        selector = item.get("selector")
        selector_type = self._normalize_selector_type(
            item.get("selector_type"), selector, tc_id, step_no
        )
        selector, selector_type = self._normalize_selector_fields(
            action, selector, selector_type, item, tc_id, step_no
        )
        value = self._normalize_value(action, item, selector, tc_id, step_no)
        expected = self._normalize_expected(action, item, selector, value, tc_id, step_no)
        return {
            "step_no": step_no,
            "action": action,
            "selector": selector,
            "selector_type": selector_type,
            "value": value,
            "expected": expected,
            "api_endpoint": item.get("api_endpoint"),
        }

    def _normalize_action(self, item: dict, tc_id: str, step_no: int) -> str:
        """비표준 action을 표준 action vocabulary로 정규화한다."""
        raw = str(item.get("action") or "").strip().lower().replace("-", "_")
        action = _ACTION_ALIASES.get(raw, raw)
        if action in _ALLOWED_ACTIONS:
            if action != raw:
                self._log_normalization(tc_id, step_no, "action", raw, action)
            return action
        fallback = self._infer_fallback_action(item)
        self._log_normalization(tc_id, step_no, "action", raw, fallback)
        return fallback

    def _infer_fallback_action(self, item: dict) -> str:
        """알 수 없는 action을 필드 단서 기반 fallback action으로 변환한다."""
        if item.get("expected"):
            return "assert"
        if item.get("value") and not item.get("selector"):
            return "navigate"
        if item.get("value"):
            return "fill"
        return "click"

    def _normalize_selector_type(
        self, selector_type: Any, selector: Any, tc_id: str, step_no: int
    ) -> str | None:
        """selector_type을 표준 locator 타입으로 정규화한다."""
        if selector_type is None:
            return None
        raw = str(selector_type).strip().lower().replace(" ", "_")
        normalized = _SELECTOR_TYPE_ALIASES.get(raw, raw)
        if normalized in _ALLOWED_SELECTOR_TYPES:
            if normalized != raw:
                self._log_normalization(tc_id, step_no, "selector_type", raw, normalized)
            return normalized
        fallback = "xpath" if str(selector or "").strip().startswith(("/", "(")) else "css"
        self._log_normalization(tc_id, step_no, "selector_type", raw, fallback)
        return fallback

    def _normalize_selector_fields(
        self, action: str, selector: Any, selector_type: str | None,
        item: dict, tc_id: str, step_no: int
    ) -> tuple[str | None, str | None]:
        """action 성격에 따라 selector와 selector_type을 보정한다."""
        if action in _SELECTOR_OPTIONAL_ACTIONS:
            return None, None
        if selector and selector_type:
            return str(selector), selector_type
        fallback = item.get("expected") or item.get("value") or action
        self._log_normalization(tc_id, step_no, "selector", selector, fallback)
        return str(fallback), selector_type or "text"

    def _normalize_value(
        self, action: str, item: dict, selector: str | None, tc_id: str, step_no: int
    ) -> str | None:
        """value 필수 action에서 실행 가능한 기본값을 보정한다."""
        value = item.get("value")
        if value is not None or action not in _VALUE_REQUIRED_ACTIONS:
            return value
        defaults = {"wait": "1000", "wait_for_load_state": "networkidle"}
        fallback = defaults.get(action, item.get("expected") or selector or "")
        self._log_normalization(tc_id, step_no, "value", value, fallback)
        return str(fallback)

    def _normalize_expected(
        self, action: str, item: dict, selector: str | None,
        value: str | None, tc_id: str, step_no: int
    ) -> str | None:
        """expected 필수 action에서 실행 가능한 기본값을 보정한다."""
        expected = item.get("expected")
        if expected is not None or action not in _EXPECTED_REQUIRED_ACTIONS:
            return expected
        fallback = "1" if action == "assert_count" else value or selector or ""
        self._log_normalization(tc_id, step_no, "expected", expected, fallback)
        return str(fallback)

    def _log_normalization(
        self, tc_id: str, step_no: int, field: str, original: Any, normalized: Any
    ) -> None:
        """정규화/fallback 발생을 로그로 남긴다."""
        self.logger.warning(
            "action_mapping_normalized",
            tc_id=tc_id,
            step_no=step_no,
            field=field,
            original=original,
            normalized=normalized,
        )

    def _calc_confidence(
        self, action_mappings: list[ActionMapping], has_scan_result: bool
    ) -> float:
        """매핑 품질과 API 매핑률 기반 confidence를 계산한다."""
        base = 0.8 if has_scan_result else 0.5
        all_steps = [step for mapping in action_mappings for step in mapping["steps"]]
        api_ratio = (
            sum(1 for step in all_steps if step.get("api_endpoint")) / len(all_steps)
            if all_steps else 0
        )
        selector_steps = [step for step in all_steps if step.get("selector_type")]
        low_quality_types = {"css", "xpath"}
        low_quality = sum(
            1 for step in selector_steps
            if step.get("selector_type") in low_quality_types
        )
        quality_ratio = 1 - (low_quality / len(selector_steps) if selector_steps else 0)
        confidence = base * 0.5 + api_ratio * 0.3 + quality_ratio * 0.2
        return min(max(round(confidence, 2), 0.0), 1.0)
