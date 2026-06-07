"""해결 방안 추천 Agent.

RootCauseResult Top-N 원인 후보 전체를 단일 LLM 호출로 입력받아
통합 해결 가이드를 생성한다.

담당: F
Created: 2026-05-07
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from qapilot.agents.base_agent import BaseAgent
from qapilot.shared.codebase_context_loader import CodebaseContextLoader
from qapilot.shared.errors import AgentExecutionError, ErrorCode
from qapilot.shared.schemas import ExecuteResult, FixResult, FixSuggestion

# tc_id 기준 더미 컨텍스트 파일 위치 (실제 DB 조회 전까지 사용)
_DUMMY_CONTEXT_DIR = Path(__file__).parent.parent.parent / "tests" / "fixtures" / "contexts"


class FixRecommenderAgent(BaseAgent):
    """해결 방안 추천 Agent.

    역할: RootCauseResult Top-N + 코드 인덱스 → 통합 해결 가이드 (FixResult)
    입력: tc_id, candidates (RootCauseCandidate list)
    출력: {"fix_results": [FixResult]}, confidence
    호출 Tool: 없음

    tc_id 기준으로 코드 인덱스(code_context)를 로드해 프롬프트에 주입한다.
    blame_author, file_path 등은 LLM이 코드 인덱스를 참고해 채울 수 있다.
    """

    allowed_tools: list[str] = []

    async def _execute(
        self,
        context: dict[str, Any],
        params: dict[str, Any],
        last_error: str | None = None,
    ) -> ExecuteResult:
        """RootCauseResult Top-N 후보를 한 번에 받아 해결 가이드를 생성한다.

        Args:
            context: 파이프라인 컨텍스트.
            params: tc_id, candidates (list[RootCauseCandidate]).
            last_error: 이전 시도 에러. 자가 수정 힌트에 사용.

        Returns:
            ExecuteResult: fix_results(list[FixResult])와 confidence 포함.
        """
        tc_id = params.get("tc_id") or context.get("tc_id", "")
        candidates: list = params.get("candidates") or context.get("candidates") or []

        # candidates가 없으면 LLM 호출 없이 바로 fallback 반환
        if not candidates:
            self.logger.warning("no_candidates_for_fix", tc_id=tc_id)
            fallback = self._build_fallback_fix_result(tc_id, [])
            return ExecuteResult(result={"fix_results": [fallback]}, confidence=0.3)

        # tc_id 기준 코드 인덱스 로드.
        # 우선순위: 1) params/context 직접 주입 → 2) candidates 의 evidence_code_location
        # (root_cause Agent 가 채움) 에서 추출 → 3) state.qapilot_dir 의 codebase-index
        # → 4) _load_dummy_context (tests fixture).
        # 격차 (e2e trace `c8aadf83`): 기존엔 1 → 4 로 곧장 fallback → SaaS dev 에 fixture
        # 없어 6 TC 모두 `code_context_not_found` warning + confidence=0.0. 본 fix 로
        # candidates 의 code_location + qapilot_dir codebase-index 활용.
        code_context_raw = params.get("code_context") or context.get("code_context", "")
        if not code_context_raw:
            code_context_raw = self._extract_context_from_candidates(candidates)
        if not code_context_raw:
            qapilot_dir = context.get("qapilot_dir") if context else None
            code_context_raw = self._load_from_codebase_index(qapilot_dir)
        if not code_context_raw and tc_id:
            code_context_raw = self._load_dummy_context(tc_id, "code_context")

        if tc_id and not code_context_raw:
            self.logger.warning("code_context_not_found", tc_id=tc_id, trace_id=self.trace_id)

        context_text = (
            self._stringify(code_context_raw)
            if code_context_raw
            else "(코드 인덱스 없음 — 원인 후보 기반 분석)"
        )

        input_data = {"tc_id": tc_id, "candidates": candidates}

        user_prompt = self.with_correction_hint(
            self.prompts.render(
                context=context_text,
                input_data=json.dumps(input_data, ensure_ascii=False, indent=2),
            ),
            last_error,
        )

        response = await self.llm.chat(
            system_prompt=self.prompts.system(),
            user_prompt=user_prompt,
        )

        try:
            fix_results, confidence = self._parse_response(response.content, tc_id)
        except AgentExecutionError as e:
            self.logger.warning("parse_failed_using_fallback", tc_id=tc_id, error=str(e))
            fallback = self._build_fallback_fix_result(tc_id, candidates)
            return ExecuteResult(result={"fix_results": [fallback]}, confidence=0.3)

        total_suggestions = sum(len(r["suggestions"]) for r in fix_results)
        self.logger.info("fix_recommended", tc_id=tc_id, suggestion_count=total_suggestions)

        return ExecuteResult(result={"fix_results": fix_results}, confidence=confidence)

    def _parse_response(
        self, content: str, tc_id: str
    ) -> tuple[list[FixResult], float]:
        """LLM 응답 JSON을 파싱하고 FixResult 목록과 confidence를 반환한다.

        Raises:
            AgentExecutionError: JSON 파싱 실패 또는 fix_results 키 누락 시.
        """
        try:
            cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", content).strip()
            data = json.loads(cleaned)
        except json.JSONDecodeError as e:
            raise AgentExecutionError(
                ErrorCode.AGENT_004,
                f"LLM 출력 JSON 파싱 실패: {e}\n출력 앞부분: {content[:300]}",
            ) from e

        if "fix_results" not in data:
            raise AgentExecutionError(
                ErrorCode.AGENT_004,
                f"'fix_results' 필드가 없습니다. 출력: {content[:300]}",
            )

        confidence = self._compute_agent_confidence(data)

        fix_results: list[FixResult] = []
        for item in data["fix_results"]:
            result_tc_id = item.get("tc_id") or tc_id
            suggestions = [
                self._normalize_suggestion(s) for s in item.get("suggestions", [])
            ][:3]
            fix_results.append({"tc_id": result_tc_id, "suggestions": suggestions})

        return fix_results, confidence

    def _normalize_suggestion(self, raw: dict[str, Any]) -> FixSuggestion:
        """raw dict를 FixSuggestion 스키마에 맞게 정규화한다.

        미확인 필드(file_path, line_number, blame_author, code_snippet, similar_issues)는
        안전한 기본값으로 채운다. 꾸며내지 않는다.
        """
        return {
            "file_path": raw.get("file_path") or "",
            "line_number": int(raw.get("line_number") or 0),
            "blame_author": raw.get("blame_author") or None,
            "code_snippet": raw.get("code_snippet") or "",
            "description": raw.get("description") or "",
            "similar_issues": raw.get("similar_issues") or [],
        }

    def _build_fallback_fix_result(self, tc_id: str, candidates: list) -> FixResult:
        """파싱 실패 또는 candidates 없을 때 최소 fallback FixResult를 생성한다."""
        if candidates:
            top_cause = candidates[0].get("cause", "알 수 없음")
            description = (
                f"rank 1 원인 후보({top_cause})를 우선 검토하고 "
                f"관련 예외 처리 및 데이터 정합성 로직을 점검하세요."
            )
        else:
            description = "원인 분석 결과가 없습니다. 에러 로그와 스택 트레이스를 직접 확인하세요."

        suggestion: FixSuggestion = {
            "file_path": "",
            "line_number": 0,
            "blame_author": None,
            "code_snippet": "",
            "description": description,
            "similar_issues": [],
        }
        return {"tc_id": tc_id, "suggestions": [suggestion]}

    @staticmethod
    def _load_dummy_context(tc_id: str, key: str) -> str:
        """tc_id 기준으로 tests/fixtures/contexts/ 아래 더미 컨텍스트를 로드한다.

        실제 DB/스토리지 조회가 구현되기 전까지 사용하는 임시 로더.
        """
        path = _DUMMY_CONTEXT_DIR / tc_id / f"{key}.json"
        if path.exists():
            return path.read_text(encoding="utf-8")
        return ""

    @staticmethod
    def _extract_context_from_candidates(candidates: list) -> str:
        """RootCauseCandidate 의 evidences (type='code_location') 에서 code 컨텍스트 추출.

        root_cause Agent 가 candidates 의 evidence_code_location 필드에 file/line 정보
        + (선택) snippet 을 채움. fix_recommender 는 이를 활용하여 추가 codebase-index
        조회 없이 즉시 컨텍스트 구성.
        """
        if not candidates:
            return ""
        code_locations: list[str] = []
        for c in candidates:
            for ev in (c.get("evidences") or []):
                if ev.get("type") == "code_location":
                    content = ev.get("content") or ""
                    if content and content not in code_locations:
                        code_locations.append(str(content))
        return "\n".join(code_locations) if code_locations else ""

    @staticmethod
    def _load_from_codebase_index(qapilot_dir: str | None) -> str:
        """SaaS qapilot_dir 의 codebase-index 메타데이터를 요약하여 반환.

        root_cause 의 _load_from_codebase_index 와 동형 — 단 fix_recommender 는 clue
        없이 전체 endpoints 요약만 (root_cause 가 이미 cause 식별 후라 fix 단계는 일반
        컨텍스트 충분). 인덱스 없으면 빈 문자열.
        """
        if not qapilot_dir:
            return ""
        qd = Path(qapilot_dir).resolve()
        base_dir = qd if (qd / "codebase-index").is_dir() else qd.parent
        try:
            index = CodebaseContextLoader.load(base_dir=base_dir)
        except Exception:
            return ""
        if not index.get("_dir_found"):
            return ""
        endpoints = (index.get("endpoints") or [])[:20]
        models = (index.get("models") or [])[:10]
        manifest = index.get("manifest") or {}
        if not endpoints and not models and not manifest:
            return ""
        return json.dumps(
            {"endpoints": endpoints, "models": models, "manifest": manifest},
            ensure_ascii=False,
            indent=2,
        )

    @staticmethod
    def _stringify(val: Any) -> str:
        """값을 문자열로 변환한다. dict/list는 JSON으로 직렬화."""
        if isinstance(val, str):
            return val
        if val:
            return json.dumps(val, ensure_ascii=False, indent=2)
        return ""

    @staticmethod
    def _compute_agent_confidence(data: dict[str, Any]) -> float:
        """LLM 응답에서 confidence를 추출하고 0.0~1.0으로 clamp한다.

        LLM이 confidence를 제공하면 그 값을 사용한다.
        없으면 기본값 0.5를 반환한다.
        """
        if "confidence" in data:
            return min(1.0, max(0.0, float(data["confidence"])))
        return 0.5
