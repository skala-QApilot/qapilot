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
    """

    agent_name = "cross_check"

    def _extract_error_code(
        self, ui_result: dict, api_trace: dict
    ) -> str | None:
        """UI/API 응답에서 에러 코드 추출.
        
        DB는 모듈 스펙 확정 후 추가 예정.
        """
        # UI 에러 코드 추출 (UI_ 접두사)
        steps = ui_result.get("steps", [])
        for step in steps:
            if step.get("status") == "fail":
                error = step.get("error", "") or ""
                if error.startswith("UI_"):
                    return error

        # API 에러 코드 추출
        calls = api_trace.get("calls", [])
        for call in calls:
            status_code = call.get("status_code", 200)
            if status_code >= 400:
                response_body = call.get("response_body", {}) or {}
                if isinstance(response_body, dict) and "code" in response_body:
                    return response_body["code"]
                return str(status_code)

        return None

    async def _analyze_with_llm(
        self,
        ui_result: dict,
        api_trace: dict,
        db_result: dict,
        last_error: str | None,
    ) -> tuple[list[CrossCheckMismatch], float, str, str]:
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
            error_code = parsed.get("error_code", "none")
            summary = parsed.get("summary", "")
        except Exception:
            mismatches = []
            match_score = 1.0
            error_code = "none"
            summary = ""

        return mismatches, match_score, error_code, summary

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

        # 경로 A: UI 또는 API에서 에러 코드 있는 경우
        error_code = self._extract_error_code(ui_result, api_trace)
        if error_code:
            result = CrossCheckResult(
                tc_id=tc_id,
                match_score=0.0,
                matched_fields=0,
                mismatched_fields=0,
                mismatches=[],
                has_mismatch=True,
            )
            # 경로 A summary는 LLM으로 생성
            _, _, _, summary = await self._analyze_with_llm(
                ui_result, api_trace, db_result, last_error
            )
            return ExecuteResult(
                result={
                    "cross_check": result,
                    "error_code": error_code,
                    "summary": summary,
                    "route": "A",
                },
                confidence=1.0,
            )

        # 경로 B: 에러 코드 없는 경우 → LLM으로 불일치 분석
        mismatches, match_score, mismatch_code, summary = await self._analyze_with_llm(
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
                "error_code": mismatch_code,
                "summary": summary,
                "route": "B",
            },
            confidence=match_score,
        )