"""원인 추론 Agent.

CrossCheckResult 기반으로 테스트 실패의 근본 원인 후보 Top-N을
신뢰도와 근거와 함께 도출한다.

confidence 2종 분리:
  - RootCauseCandidate.confidence : cause 하나가 얼마나 그럴듯한가
  - ExecuteResult.confidence       : 이번 출력 전체가 얼마나 믿을 만한가 (Judge 기반)

담당: F
Created: 2026-05-07
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from qapilot.agents.base_agent import BaseAgent
from qapilot.shared.errors import AgentExecutionError, ErrorCode
from qapilot.shared.prompt_loader import PromptLoader
from qapilot.shared.schemas import Evidence, ExecuteResult, RootCauseCandidate, RootCauseResult

# tc_id 기준 더미 컨텍스트 파일 위치 (실제 DB 조회 전까지 사용)
_DUMMY_CONTEXT_DIR = Path(__file__).parent.parent.parent / "tests" / "fixtures" / "contexts"

# Judge 프롬프트 파일 경로 (없으면 인라인 상수로 fallback)
_JUDGE_PROMPT_PATH = Path(__file__).parent.parent.parent / "prompts" / "root_cause" / "judge.md"

# 인라인 fallback — judge.md 미존재 환경 대비
_JUDGE_PROMPT_FALLBACK = """\
너는 원인 추론 결과 전체를 평가하는 Judge야.
아래 입력과 추론 결과를 보고 각 항목을 1~5점으로 평가해라.
점수 외 다른 텍스트 없이 JSON만 반환해라.

## 입력
- error_code: {{error_code}}
- situation_summary: {{situation_summary}}

## 추론 결과
{{root_cause_result}}

## 평가 항목
1. relevance: 후보 전체가 입력 문제를 잘 설명하는가
   1 = 전혀 무관 / 5 = 완전히 일치

2. diversity: Top-N 후보 구성이 서로 구분되는가
   1 = 모두 동일한 원인 / 5 = 완전히 독립적인 원인

3. ranking_validity: 후보 간 순위가 타당한가
   1 = 순위 부적절 / 5 = 순위 완전히 타당

4. evidence_quality: evidence 분포가 전체적으로 충분한가
   1 = evidence 거의 없거나 편향 / 5 = 고르고 충분한 근거

## 응답 형식
{{
  "relevance": 점수,
  "diversity": 점수,
  "ranking_validity": 점수,
  "evidence_quality": 점수,
  "reason": "한 줄 평가 요약"
}}"""

_VALID_EVIDENCE_TYPES = frozenset({"code_location", "runtime_data"})


class RootCauseAgent(BaseAgent):
    """원인 추론 Agent.

    역할: CrossCheckResult + 컨텍스트 → Top-N 원인 후보 + 근거
    입력: CrossCheckResult 필드, code_context, runtime_context
    출력: {"root_causes": [RootCauseResult]}
    호출 Tool: 없음

    tc_id:    테스트 케이스 식별자. 코드베이스·로그 조회 키로 사용.
    trace_id: 오케스트레이터가 주입하는 실행 추적 ID (self.trace_id). 컨텍스트 조회에는 사용하지 않음.
    """

    allowed_tools: list[str] = []

    async def _execute(
        self,
        context: dict[str, Any],
        params: dict[str, Any],
        last_error: str | None = None,
    ) -> ExecuteResult:
        """CrossCheckResult 기반으로 원인 후보 Top-N을 추론한다.

        Args:
            context: 파이프라인 컨텍스트. code_context, runtime_context를 읽는다.
            params: 실행 파라미터.
                tc_id, error_code, summary, mismatches, has_mismatch,
                code_context, runtime_context
            last_error: 이전 시도 에러. 자가 수정 힌트에 사용.

        Returns:
            ExecuteResult:
                result["root_causes"] — list[RootCauseResult]
                confidence            — Judge 기반 출력 전체 신뢰도
        """
        tc_id = params.get("tc_id") or context.get("tc_id", "")
        error_code = params.get("error_code") or context.get("error_code", "")
        summary: str = params.get("summary") or context.get("summary") or ""
        mismatches: list = params.get("mismatches") or context.get("mismatches") or []
        has_mismatch: bool = params.get("has_mismatch", context.get("has_mismatch", False))

        code_context_raw = params.get("code_context") or context.get("code_context", "")
        runtime_context_raw = params.get("runtime_context") or context.get("runtime_context", "")

        # params/context에 없으면 tc_id 기준 더미 파일에서 로드
        if not code_context_raw and tc_id:
            code_context_raw = self._load_dummy_context(tc_id, "code_context")
        if not runtime_context_raw and tc_id:
            runtime_context_raw = self._load_dummy_context(tc_id, "runtime_context")

        # 컨텍스트 가용성 확인 및 경고 로그
        code_missing = tc_id and not code_context_raw
        runtime_missing = tc_id and not runtime_context_raw
        if code_missing:
            self.logger.warning(
                "context_not_found", tc_id=tc_id, trace_id=self.trace_id, missing="code_context"
            )
        if runtime_missing:
            self.logger.warning(
                "context_not_found", tc_id=tc_id, trace_id=self.trace_id, missing="runtime_context"
            )

        # summary가 없으면 mismatches에서 fallback 생성
        if not summary and mismatches:
            summary = "; ".join(
                f"{m.get('field', '')}: ui={m.get('ui_value', '')}, api={m.get('api_value', '')}"
                for m in mismatches
            )

        # 관련 코드 컨텍스트만 추려서 프롬프트 크기 최적화
        code_context_filtered = self._select_relevant_code_context(
            self._stringify(code_context_raw), error_code, summary, mismatches,
        )

        context_text = self._format_context(
            code_context_filtered, self._stringify(runtime_context_raw),
        )
        input_data = {
            "tc_id": tc_id,
            "error_code": error_code,
            "summary": summary,
            "mismatches": mismatches,
            "has_mismatch": has_mismatch,
        }

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

        # 파싱 실패 시 fallback candidate 생성
        try:
            candidates = self._parse_response(response.content)
        except AgentExecutionError as e:
            self.logger.warning("parse_failed_using_fallback", tc_id=tc_id, error=str(e))
            candidates = self._build_fallback_candidates(error_code, summary, mismatches)

        candidates = candidates[:3]

        # cause별 confidence 계산 (관련성 페널티 + evidence 보정)
        for candidate in candidates:
            candidate["confidence"] = self._score_candidate_confidence(
                candidate, error_code, summary, mismatches
            )

        # 양쪽 context 모두 없을 때: confidence 0.0 강제 + note evidence 추가
        if code_missing and runtime_missing:
            note: Evidence = {"type": "runtime_data", "content": "error_code와 summary만으로 추론"}
            for candidate in candidates:
                candidate["confidence"] = 0.0
                candidate["evidences"] = candidate.get("evidences", []) + [note]

        # 응답 전체 confidence — LLM-as-Judge
        agent_confidence = await self._run_judge(error_code, summary, candidates)

        self.logger.info("root_cause_analyzed", tc_id=tc_id, candidate_count=len(candidates))

        root_cause_result: RootCauseResult = {"tc_id": tc_id, "candidates": candidates}
        return ExecuteResult(
            result={"root_causes": [root_cause_result]},
            confidence=agent_confidence,
        )

    # ── cause별 confidence ──────────────────────────────────────────────────────

    def _score_candidate_confidence(
        self,
        candidate: dict[str, Any],
        error_code: str,
        summary: str,
        mismatches: list,
    ) -> float:
        """cause별 confidence를 계산한다.

        base 0.5 고정 (LLM confidence 미사용)
          단, _build_fallback_candidates가 설정한 0.1은 그대로 사용.
          + 관련성 페널티  : cause가 입력 키워드와 무관하면 -0.4
          + evidence 보정  : code_location +0.3 / runtime_data +0.3
          → clamp 0.0~1.0

        향후 확장 포인트:
          - Rule 3: evidence가 실제로 이 cause를 지지하는가 (_score_evidence_support)
          - Rule 4: code/runtime이 서로 모순 없이 맞아떨어지는가 (_score_consistency)
        """
        base = float(candidate.get("confidence", 0.5))
        cause = candidate.get("cause", "")
        evidences = candidate.get("evidences", [])

        relevance_delta = self._score_relevance(cause, error_code, summary, mismatches)

        evidence_boost = 0.0
        if any(e.get("type") == "code_location" for e in evidences):
            evidence_boost += 0.2
        if any(e.get("type") == "runtime_data" for e in evidences):
            evidence_boost += 0.2

        return min(1.0, max(0.0, base + relevance_delta + evidence_boost))

    @staticmethod
    def _score_relevance(
        cause: str, error_code: str, summary: str, mismatches: list
    ) -> float:
        """cause가 입력(error_code, summary, mismatches)과 무관하면 -0.4를 반환한다.

        토큰화: 소문자화 후 영문·숫자·한글 이외 문자를 구분자로 분리(길이 2 이상).
        입력 토큰이 없으면(비교 불가) 페널티를 적용하지 않는다.
        """
        _split = lambda s: {
            t for t in re.split(r"[^a-zA-Z0-9가-힣]+", s.lower()) if len(t) >= 2
        }

        input_tokens: set[str] = set()
        for text in [error_code, summary]:
            if text:
                input_tokens |= _split(text)
        for m in mismatches:
            for key in ("field", "ui_value", "api_value", "db_value"):
                val = m.get(key) or ""
                if val:
                    input_tokens |= _split(str(val))

        if not input_tokens:
            return 0.0

        cause_tokens = _split(cause)
        return 0.0 if input_tokens & cause_tokens else -0.4

    # ── 응답 전체 confidence (Judge) ───────────────────────────────────────────

    async def _run_judge(
        self,
        error_code: str,
        summary: str,
        candidates: list[dict[str, Any]],
    ) -> float:
        """Judge LLM으로 추론 결과 전체를 평가하고 0.0~1.0 confidence를 반환한다.

        평가 지표 4개(relevance, diversity, ranking_validity, evidence_quality)의 평균을
        1~5 → 0.0~1.0 구간으로 정규화: (avg - 1) / 4

        실패 시 candidate 최고 confidence로 fallback.
        """
        fallback = max((c["confidence"] for c in candidates), default=0.5)
        if not candidates:
            return fallback

        # judge.md 우선 로드, 없으면 인라인 상수 사용
        template = PromptLoader._read(_JUDGE_PROMPT_PATH) or _JUDGE_PROMPT_FALLBACK
        judge_prompt = (
            template
            .replace("{{error_code}}", error_code or "(없음)")
            .replace("{{situation_summary}}", summary or "(없음)")
            .replace("{{root_cause_result}}", json.dumps(candidates, ensure_ascii=False, indent=2))
        )

        try:
            response = await self.llm.chat(
                system_prompt="JSON만 반환하라.",
                user_prompt=judge_prompt,
            )
            cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", response.content).strip()
            scores = json.loads(cleaned)
            relevance = float(scores["relevance"])
            diversity = float(scores["diversity"])
            ranking_validity = float(scores["ranking_validity"])
            evidence_quality = float(scores["evidence_quality"])
        except Exception:
            return fallback

        avg = (relevance + diversity + ranking_validity + evidence_quality) / 4
        return min(1.0, max(0.0, (avg - 1) / 4))

    # ── 파싱 ───────────────────────────────────────────────────────────────────

    def _parse_response(self, content: str) -> list[RootCauseCandidate]:
        """LLM 응답 JSON을 파싱하고 RootCauseCandidate 목록을 반환한다.

        Raises:
            AgentExecutionError: JSON 파싱 실패 또는 root_causes 키 누락 시.
        """
        try:
            cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", content).strip()
            data = json.loads(cleaned)
        except json.JSONDecodeError as e:
            raise AgentExecutionError(
                ErrorCode.AGENT_004,
                f"LLM 출력 JSON 파싱 실패: {e}\n출력 앞부분: {content[:300]}",
            ) from e

        if "root_causes" not in data:
            raise AgentExecutionError(
                ErrorCode.AGENT_004,
                f"'root_causes' 필드가 없습니다. 출력: {content[:300]}",
            )

        candidates: list[RootCauseCandidate] = []
        for i, raw in enumerate(data["root_causes"], start=1):
            valid_evidences: list[Evidence] = [
                {"type": e["type"], "content": e.get("content", "")}
                for e in raw.get("evidences", [])
                if e.get("type") in _VALID_EVIDENCE_TYPES
            ]
            candidates.append({
                "rank": raw.get("rank", i),
                "cause": raw.get("cause", ""),
                "confidence": 0.5,  # LLM confidence 미사용 — rule-base로 계산
                "evidences": valid_evidences,
            })

        return candidates

    def _build_fallback_candidates(
        self,
        error_code: str,
        summary: str,
        mismatches: list[dict],
    ) -> list[RootCauseCandidate]:
        """LLM 응답 파싱 실패 시 입력 데이터 기반으로 최소 fallback candidate를 생성한다."""
        cause_parts = [p for p in [error_code, summary[:100] if summary else ""] if p]
        cause = (
            f"파싱 실패 - 입력 기반 최소 분석: {', '.join(cause_parts)}"
            if cause_parts
            else "원인 분석 실패 (LLM 응답 파싱 불가)"
        )

        evidences: list[Evidence] = []
        runtime_parts = [p for p in [
            f"error_code={error_code}" if error_code else "",
            summary[:200] if summary else "",
            f"mismatch fields: {[m.get('field', '') for m in mismatches]}" if mismatches else "",
        ] if p]
        if runtime_parts:
            evidences.append({"type": "runtime_data", "content": "; ".join(runtime_parts)})

        return [{"rank": 1, "cause": cause, "confidence": 0.1, "evidences": evidences}]

    # ── 컨텍스트 처리 유틸 ─────────────────────────────────────────────────────

    @staticmethod
    def _select_relevant_code_context(
        code_context_str: str,
        error_code: str,
        summary: str,
        mismatches: list[dict],
    ) -> str:
        """error_code, summary, mismatches 키워드 기준으로 관련 코드 컨텍스트를 추린다.

        JSON 구조(files 배열)이면 파일별 스코어링 후 관련 파일만 반환.
        평문이면 줄 단위 필터링.
        키워드가 없거나 매칭 결과가 없으면 전체를 그대로 반환한다(안전 fallback).
        """
        if not code_context_str:
            return code_context_str

        tokens: set[str] = set()
        for text in [error_code, summary]:
            if text:
                tokens.update(t for t in re.split(r"[\s_/.:,\-]+", text.lower()) if t)
        for m in mismatches:
            field = m.get("field", "")
            if field:
                tokens.update(t for t in re.split(r"[\s_/.:,\-]+", field.lower()) if t)

        if not tokens:
            return code_context_str

        try:
            data = json.loads(code_context_str)
        except (json.JSONDecodeError, TypeError):
            lines = code_context_str.splitlines()
            relevant = [ln for ln in lines if any(t in ln.lower() for t in tokens)]
            return "\n".join(relevant) if relevant else code_context_str

        if isinstance(data, dict) and "files" in data:
            relevant = [
                f for f in data["files"]
                if any(t in json.dumps(f, ensure_ascii=False).lower() for t in tokens)
            ]
            if relevant:
                return json.dumps({**data, "files": relevant}, ensure_ascii=False, indent=2)

        return code_context_str

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
    def _stringify(val: Any) -> str:
        """값을 문자열로 변환한다. dict/list는 JSON으로 직렬화."""
        if isinstance(val, str):
            return val
        if val:
            return json.dumps(val, ensure_ascii=False, indent=2)
        return ""

    @staticmethod
    def _format_context(code_context: str, runtime_context: str) -> str:
        """코드/런타임 컨텍스트를 프롬프트용 문자열로 조합한다."""
        parts = []
        if code_context:
            parts.append(f"## 코드 컨텍스트\n{code_context}")
        if runtime_context:
            parts.append(f"## 런타임 컨텍스트\n{runtime_context}")
        return "\n\n".join(parts) if parts else "(컨텍스트 없음)"
