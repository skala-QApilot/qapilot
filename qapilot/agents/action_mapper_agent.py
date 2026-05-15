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

_ALLOWED_ACTIONS = {"fill", "click", "assert", "navigate", "select", "wait"}
_ALLOWED_SELECTOR_TYPES = {
    "role", "label", "placeholder", "text", "testid",
    "alttext", "title", "css", "xpath",
}
_SELECTOR_OPTIONAL_ACTIONS = {"navigate", "wait"}


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
        action = item["action"]
        selector_type = item["selector_type"]
        if action not in _ALLOWED_ACTIONS:
            raise ValueError(f"{tc_id} step {step_no}: 허용되지 않는 action: {action}")
        if action not in _SELECTOR_OPTIONAL_ACTIONS and not item["selector"]:
            raise ValueError(f"{tc_id} step {step_no}: selector는 필수입니다.")
        if action not in _SELECTOR_OPTIONAL_ACTIONS and selector_type is None:
            raise ValueError(f"{tc_id} step {step_no}: selector_type은 필수입니다.")
        if selector_type is not None and selector_type not in _ALLOWED_SELECTOR_TYPES:
            raise ValueError(
                f"{tc_id} step {step_no}: 허용되지 않는 selector_type: {selector_type}"
            )
        return {
            "step_no": step_no,
            "action": str(action),
            "selector": item["selector"],
            "selector_type": selector_type,
            "value": item.get("value"),
            "expected": item.get("expected"),
            "api_endpoint": item.get("api_endpoint"),
        }

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
