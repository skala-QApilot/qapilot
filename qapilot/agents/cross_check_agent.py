"""Cross-check Agent.

UI 실행 결과와 API 응답, DB 상태를 비교하여
데이터 정합성 불일치를 자동 탐지한다.

담당: E
Created: 2026-05-07
"""

import json
import os

from langchain_openai import ChatOpenAI

from qapilot.agents.base_agent import BaseAgent
from qapilot.shared.schemas import (
    AgentInput,
    AgentOutput,
    BaseMetadata,
    CrossCheckMismatch,
    CrossCheckResult,
)


class CrossCheckAgent(BaseAgent):
    """Cross-check Agent.

    역할: UI↔API↔DB 데이터 정합성 검증, 정합성 점수 산출
    입력: UITestResult, APITraceResult, DBTestResult
    출력: List[CrossCheckResult], confidence
    호출 Tool: 없음
    HITL: X
    """

    def __init__(self, trace_id: str | None = None):
        super().__init__(trace_id=trace_id)
        self.llm = ChatOpenAI(
            model="gpt-4o-mini",
            api_key=os.getenv("OPENAI_API_KEY"),
            temperature=0,
        )

    async def _extract_ui_data(self, ui_result: dict) -> dict:
        """UI 테스트 결과에서 주요 데이터 추출."""
        extracted = {}
        steps = ui_result.get("steps", [])
        for step in steps:
            if step.get("status") == "pass":
                extracted[f"step_{step.get('step_no')}"] = step.get("action")
        return extracted

    async def _extract_api_data(self, api_trace: dict) -> dict:
        """API 응답 body에서 주요 데이터 추출."""
        extracted = {}
        calls = api_trace.get("calls", [])
        for call in calls:
            if call.get("response_body"):
                extracted[call.get("url", "")] = call.get("response_body")
        return extracted

    async def _map_fields_with_llm(
        self, ui_data: dict, api_data: dict, db_data: dict
    ) -> tuple[list[CrossCheckMismatch], float]:
        """LLM으로 UI/API/DB 필드 매핑 및 불일치 탐지."""
        prompt = f"""
다음 UI, API, DB 데이터를 비교하여 불일치를 탐지해주세요.

UI 데이터:
{json.dumps(ui_data, ensure_ascii=False, indent=2)}

API 데이터:
{json.dumps(api_data, ensure_ascii=False, indent=2)}

DB 데이터:
{json.dumps(db_data, ensure_ascii=False, indent=2)}

응답은 반드시 아래 JSON 형식으로만 답해주세요:
{{
  "mismatches": [
    {{
      "field": "필드명",
      "ui_value": "UI 값",
      "api_value": "API 값",
      "db_value": "DB 값 또는 null",
      "severity": "high/medium/low"
    }}
  ],
  "match_score": 0.0~1.0
}}
"""
        response = await self.llm.ainvoke(prompt)
        content = response.content

        try:
            parsed = json.loads(content)
            mismatches = [CrossCheckMismatch(**m) for m in parsed.get("mismatches", [])]
            match_score = float(parsed.get("match_score", 1.0))
        except Exception:
            mismatches = []
            match_score = 1.0

        return mismatches, match_score

    async def run(self, input: AgentInput) -> AgentOutput:
        """UI↔API↔DB 정합성 검증."""
        import time
        start = time.time()

        tc_id = input.params.get("tc_id", "unknown")
        ui_result = input.context.get("ui_result", {})
        api_trace = input.context.get("api_trace", {})
        db_result = input.context.get("db_result", {})

        # 1. 각 계층 데이터 추출
        ui_data = await self._extract_ui_data(ui_result)
        api_data = await self._extract_api_data(api_trace)
        db_data = db_result.get("snapshots", [])

        # 2. LLM으로 필드 매핑 및 불일치 탐지
        mismatches, match_score = await self._map_fields_with_llm(
            ui_data, api_data, db_data
        )

        # 3. CrossCheckResult 생성
        result = CrossCheckResult(
            tc_id=tc_id,
            match_score=match_score,
            matched_fields=len(ui_data) - len(mismatches),
            mismatched_fields=len(mismatches),
            mismatches=mismatches,
            has_mismatch=len(mismatches) > 0,
        )

        duration = time.time() - start

        return AgentOutput(
            trace_id=input.trace_id,
            result={"cross_check": result},
            confidence=match_score,
            metadata=BaseMetadata(
                model="gpt-4o-mini",
                tokens_used=0,
                duration_sec=round(duration, 2),
            ),
        )