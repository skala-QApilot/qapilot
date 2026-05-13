"""Cross-check Agent.

UI 실행 결과와 API 응답, DB 상태를 비교하여
데이터 정합성 불일치를 자동 탐지한다.

담당: E
Created: 2026-05-07
"""

import json
from typing import Any

from qapilot.agents.base_agent import BaseAgent
from qapilot.shared.schemas import (
    CrossCheckMismatch,
    CrossCheckResult,
    ExecuteResult,
)


class CrossCheckAgent(BaseAgent):
    """Cross-check Agent.

    역할: UI↔API↔DB 데이터 정합성 검증, 정합성 점수 산출
    입력: UITestResult, APITraceResult, DBTestResult
    출력: CrossCheckResult, confidence
    호출 Tool: 없음
    HITL: X
    """

    agent_name = "cross_check"

    def _extract_error_code(self, api_trace: dict) -> str | None:
        """API 응답에서 에러 코드 추출."""
        calls = api_trace.get("calls", [])
        for call in calls:
            status_code = call.get("status_code", 200)
            if status_code >= 400:
                response_body = call.get("response_body", {}) or {}
                if isinstance(response_body, dict) and "code" in response_body:
                    return response_body["code"]
                return str(status_code)
        return None

    def _summarize_current_state(
        self, ui_result: dict, api_trace: dict, db_result: dict
    ) -> str:
        """현재 상태 요약 생성."""
        ui_status = ui_result.get("status", "unknown")
        total_calls = api_trace.get("total_calls", 0)
        error_calls = api_trace.get("error_calls", 0)
        db_tables = len(db_result.get("snapshots", []))

        return (
            f"UI 테스트: {ui_status}, "
            f"API 호출: 총 {total_calls}건 (에러 {error_calls}건), "
            f"DB 테이블: {db_tables}개 스냅샷"
        )

    async def _analyze_with_llm(
        self,
        ui_result: dict,
        api_trace: dict,
        db_result: dict,
        last_error: str | None,
    ) -> tuple[list[CrossCheckMismatch], float]:
        """LLM으로 UI/API/DB 불일치 분석."""
        system_prompt = self.prompts.system()
        user_prompt = self.prompts.render(
            context=json.dumps(
                {
                    "ui_result": ui_result,
                    "api_trace": api_trace,
                    "db_result": db_result,
                },
                ensure_ascii=False,
                indent=2,
            ),
            input_data=json.dumps(
                {
                    "task": "UI, API, DB 데이터를 비교하여 불일치를 탐지하고 JSON으로 반환하세요.",
                    "output_schema": {
                        "mismatches": [
                            {
                                "field": "필드명",
                                "ui_value": "UI 값",
                                "api_value": "API 값",
                                "db_value": "DB 값 또는 null",
                                "severity": "high/medium/low",
                            }
                        ],
                        "match_score": "0.0~1.0",
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
        )

        user_prompt = self.with_correction_hint(user_prompt, last_error)
        response = await self.llm.chat(system_prompt, user_prompt)

        try:
            parsed = json.loads(response.content)
            mismatches = [CrossCheckMismatch(**m) for m in parsed.get("mismatches", [])]
            match_score = float(parsed.get("match_score", 1.0))
        except Exception:
            mismatches = []
            match_score = 1.0

        return mismatches, match_score

    async def _execute(
        self,
        context: dict[str, Any],
        params: dict[str, Any],
        last_error: str | None = None,
    ) -> ExecuteResult:
        """경로 A/B 분기 후 Cross-check 수행."""
        tc_id = params.get("tc_id", "unknown")
        ui_result = context.get("ui_result", {})
        api_trace = context.get("api_trace", {})
        db_result = context.get("db_result", {})

        current_state_summary = self._summarize_current_state(
            ui_result, api_trace, db_result
        )

        error_code = self._extract_error_code(api_trace)
        if error_code:
            result = CrossCheckResult(
                tc_id=tc_id,
                match_score=0.0,
                matched_fields=0,
                mismatched_fields=0,
                mismatches=[],
                has_mismatch=True,
            )
            return ExecuteResult(
                result={
                    "cross_check": result,
                    "error_code": error_code,
                    "current_state_summary": current_state_summary,
                    "route": "A",
                },
                confidence=1.0,
            )

        mismatches, match_score = await self._analyze_with_llm(
            ui_result, api_trace, db_result, last_error
        )

        result = CrossCheckResult(
            tc_id=tc_id,
            match_score=match_score,
            matched_fields=0,
            mismatched_fields=len(mismatches),
            mismatches=mismatches,
            has_mismatch=len(mismatches) > 0,
        )

        return ExecuteResult(
            result={
                "cross_check": result,
                "current_state_summary": current_state_summary,
                "route": "B",
            },
            confidence=match_score,
        )