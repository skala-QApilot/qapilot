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
from qapilot.shared.codebase_context_loader import CodebaseContextLoader
from qapilot.shared.errors import AgentExecutionError, ErrorCode
from qapilot.shared.prompt_loader import PromptLoader
from qapilot.shared.schemas import Evidence, ExecuteResult, RootCauseCandidate, RootCauseResult

# runtime_context 더미 파일 위치 (tc_id 기반, 테스트/개발용)
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
            context: 파이프라인 컨텍스트.
            params: 실행 파라미터.
                tc_id, error_code, summary, mismatches, has_mismatch, runtime_context
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

        # runtime_context: tc_id 기반 더미 파일 (파이프라인 완성 전까지)
        runtime_context_raw = self._load_dummy_context(tc_id, "runtime_context") if tc_id else ""

        runtime_str = self._stringify(runtime_context_raw)

        # LLM으로 단서 추출 (실패 시 정규식 fallback)
        clues = await self._extract_clues_with_llm(
            error_code, summary, mismatches, runtime_str,
        )

        # codebase-index에서 항상 관련 항목 선별 (코드 컨텍스트의 유일한 소스).
        # SaaS 흐름은 cfg.project.repo_path 가 None → fallback Path(".") = CWD (qapilot 디렉토리)
        # 에서 .qapilot/codebase-index 찾기 시도 → 없음 → `codebase_index_empty` warning.
        # state.qapilot_dir (SaaS temp 또는 CLI 명시) 를 context 로 받아 우선 사용.
        # e2e trace `40fce3fa` 격차.
        qapilot_dir = context.get("qapilot_dir") if context else None
        code_context_raw = self._load_from_codebase_index(clues, error_code, qapilot_dir=qapilot_dir)

        # runtime_context 가용성 경고 (tc_id가 지정됐는데 없을 때)
        runtime_missing = tc_id and not runtime_context_raw
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

        context_text = self._format_context(code_context_raw, runtime_str)
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

        # 코드 컨텍스트(index)도 없고 runtime도 없을 때: confidence 0.0 강제
        if not code_context_raw and runtime_missing:
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

    # ── codebase-index 연동 ────────────────────────────────────────────────────

    def _load_from_codebase_index(
        self,
        clues: dict[str, Any],
        error_code: str = "",
        qapilot_dir: str | None = None,
    ) -> str:
        """codebase-index 메타데이터를 로드하고 관련 항목을 선별하여 반환한다.

        인덱스가 없거나 모든 파일이 비어 있으면 warning을 남기고 빈 문자열을 반환한다.

        SaaS 흐름에서는 qapilot_dir 가 temp 디렉토리 (pipeline state.qapilot_dir).
        CodebaseContextLoader 는 base_dir 가 그 root 이면 그 하위의 `.qapilot/codebase-index`
        를 찾음. SaaS 의 qapilot_dir 는 이미 그 안에 `codebase-index/` 가 직접 들어있어
        한 단계 깊은 구조 — `base_dir = qapilot_dir.parent` 또는 loader 가 직접 디렉토리
        명을 받도록 우회. CodebaseContextLoader 의 _INDEX_SUBDIR = ".qapilot/codebase-index"
        기준이라 qapilot_dir 의 parent 가 root 처럼 동작.
        """
        if qapilot_dir:
            qd = Path(qapilot_dir).resolve()
            # qapilot_dir 명이 ".qapilot" 이면 parent 가 root (CLI 흐름)
            # qapilot_dir 명이 임의 (SaaS temp) 이면 qapilot_dir 자체가 root 처럼 + .qapilot 가상
            # 가장 간단: qapilot_dir 의 parent 를 base_dir 로 — 단 qapilot_dir 명이 .qapilot 이거나
            # 아니거나 모두 동일하게 작동하려면 별도 helper.
            # 본인은 직접 codebase-index/ 경로를 알므로 loader 우회 + 동등 로직 inline.
            base_dir = qd if (qd / "codebase-index").is_dir() else qd.parent
        else:
            repo_path = self._config.project.repo_path
            base_dir = Path(repo_path) if repo_path else Path(".")

        index = CodebaseContextLoader.load(base_dir=base_dir)

        if not index["_dir_found"]:
            self.logger.warning("codebase_index_not_found", base_dir=str(base_dir))
            return ""

        all_empty = (
            not index["endpoints"]
            and not index["models"]
            and not index["callgraph"]
            and not index["manifest"]
        )
        if all_empty:
            self.logger.warning("codebase_index_empty", base_dir=str(base_dir))
            return ""

        ctx = self._select_from_index(index, clues)

        parsed = json.loads(ctx)
        has_specific = parsed.get("endpoints") or parsed.get("models") or parsed.get("callgraph")
        if not has_specific:
            self.logger.warning("codebase_index_no_relevant_match", error_code=error_code)

        return ctx

    def _select_from_index(self, index: dict[str, Any], clues: dict[str, Any]) -> str:
        """codebase-index에서 에러와 관련된 항목만 선별하여 JSON 문자열로 반환한다.

        선별 흐름:
          Phase 1 — 단서(clues) 수신 : LLM 또는 정규식으로 이미 추출된 단서
          Phase 2 — 점수화 선택      : endpoint·model별 점수화 후 Top-N
          Phase 3 — 칼그래프 확장    : 선별 파일 기반 1-hop 의존 관계 포함
          Phase 4 — 컨텍스트 구성
        """
        self.logger.info(
            "codebase_index_selection_start",
            from_llm=clues.get("from_llm", False),
            mismatch_fields=sorted(clues["mismatch_fields"]),
            tokens=sorted(clues["tokens"]),
            url_segments=sorted(clues["url_segments"]),
            file_stems=sorted(clues["file_stems"]),
            llm_endpoints=sorted(clues.get("endpoints", set())),
            llm_functions=sorted(clues.get("functions", set())),
            llm_files=sorted(clues.get("files", set())),
            llm_models=sorted(clues.get("models", set())),
        )

        # ── Phase 2·3: endpoint 점수화 ────────────────────────────────────────
        scored_eps: list[tuple[int, dict]] = []
        for ep in index.get("endpoints", []):
            score = self._score_endpoint(ep, clues)
            if score > 0:
                scored_eps.append((score, ep))
        scored_eps.sort(key=lambda x: -x[0])
        selected_endpoints = [ep for _, ep in scored_eps[:10]]

        selected_files: set[str] = {ep["file"] for ep in selected_endpoints if ep.get("file")}

        for score, ep in scored_eps[:10]:
            self.logger.debug(
                "codebase_index_selection_detail",
                source="endpoints.json",
                item=f"{ep.get('method','')} {ep.get('path','')} ({ep.get('handler','')})".strip(),
                score=score,
            )

        # ── Phase 2·3: model 점수화 ───────────────────────────────────────────
        scored_models: list[tuple[int, dict]] = []
        for model in index.get("models", []):
            score = self._score_model(model, clues)
            if score > 0:
                scored_models.append((score, model))
        scored_models.sort(key=lambda x: -x[0])
        selected_models = [m for _, m in scored_models[:10]]

        selected_files.update(m["file"] for m in selected_models if m.get("file"))

        for score, model in scored_models[:10]:
            self.logger.debug(
                "codebase_index_selection_detail",
                source="models.json",
                item=model.get("name", ""),
                score=score,
            )

        # ── Phase 4: callgraph 1-hop 확장 ─────────────────────────────────────
        # 선별된 파일의 callgraph 항목 포함 → 의존 모듈 이름을 추가 후보 파일로 확장
        callgraph = index.get("callgraph", {})
        selected_callgraph: dict[str, Any] = {}
        expanded_stems: set[str] = set()

        for fp, deps in callgraph.items():
            if any(sf in fp or fp in sf for sf in selected_files):
                selected_callgraph[fp] = deps
                # import 구문에서 모듈명 추출 → 1-hop 확장 후보
                for dep in deps:
                    for mod in re.findall(r"[\w]+", dep.split("import")[-1]):
                        if len(mod) >= 3 and mod.islower():
                            expanded_stems.add(mod)

        # 1-hop: 아직 포함되지 않은 파일 중 확장 후보와 이름이 겹치는 항목 추가
        if expanded_stems:
            for fp, deps in callgraph.items():
                if fp in selected_callgraph:
                    continue
                stem = Path(fp).stem.lower()
                if any(s in stem or stem in s for s in expanded_stems):
                    selected_callgraph[fp] = deps

        # ── Phase 5: manifest 요약 필드만 포함 ───────────────────────────────
        manifest = index.get("manifest", {})
        manifest_summary = {
            k: manifest[k]
            for k in ("framework", "language", "file_count", "endpoint_count")
            if k in manifest
        }

        self.logger.info(
            "codebase_index_selection_result",
            endpoints_selected=len(selected_endpoints),
            models_selected=len(selected_models),
            callgraph_files_selected=len(selected_callgraph),
            manifest_fields=list(manifest_summary.keys()),
        )

        selected: dict[str, Any] = {
            "source": "codebase-index",
            "manifest": manifest_summary,
            "endpoints": selected_endpoints,
            "models": selected_models,
            "callgraph": selected_callgraph,
        }
        total_items = len(selected_endpoints) + len(selected_models) + len(selected_callgraph)
        self.logger.info(
            "codebase_index_context_built",
            total_items=total_items,
            context_bytes=len(json.dumps(selected, ensure_ascii=False)),
        )
        return json.dumps(selected, ensure_ascii=False, indent=2)

    # ── 단서 추출 / 점수화 헬퍼 ────────────────────────────────────────────────

    @staticmethod
    def _extract_clues(
        error_code: str,
        summary: str,
        mismatches: list,
        runtime_context: str,
    ) -> dict[str, Any]:
        """에러 관련 단서를 다각도로 추출한다.

        Returns:
            tokens         — 기본 토큰 (2자 이상 단어)
            url_segments   — request.path에서 추출한 경로 세그먼트
            file_stems     — stack trace 등에서 추출한 파일 줄기 이름
            mismatch_fields — mismatches[].field 값 (정확 매칭용)
        """
        tokens: set[str] = set()
        url_segments: set[str] = set()
        file_stems: set[str] = set()
        mismatch_fields: set[str] = set()

        # error_code + summary + mismatch 값에서 기본 토큰 추출
        all_text = " ".join(filter(None, [error_code, summary]))
        for m in mismatches:
            for k in ("field", "ui_value", "api_value", "db_value"):
                val = m.get(k) or ""
                if val:
                    all_text += " " + str(val)
            if m.get("field"):
                mismatch_fields.add(m["field"].lower())

        for t in re.split(r"[^a-zA-Z0-9가-힣]+", all_text.lower()):
            if len(t) >= 2:
                tokens.add(t)

        # runtime_context에서 추가 단서 추출
        if runtime_context:
            try:
                rt = json.loads(runtime_context)

                # 1) request.path → URL 세그먼트 ("api", "v1" 등 일반 접두사 제외)
                req_path = (rt.get("request") or {}).get("path", "") or \
                           (rt.get("request") or {}).get("url", "")
                if req_path:
                    for seg in req_path.split("/"):
                        seg = re.sub(r"\{.*?\}", "", seg).strip()  # {param} 제거
                        if seg and seg not in ("api", "v1", "v2", "v3"):
                            url_segments.add(seg.lower())
                            for t in re.split(r"[^a-zA-Z0-9]+", seg.lower()):
                                if len(t) >= 2:
                                    tokens.add(t)

                # 2) error.type / error.message → 추가 토큰
                err = rt.get("error") or rt.get("exception") or {}
                if isinstance(err, dict):
                    for v in (err.get("type", ""), err.get("message", ""), err.get("detail", "")):
                        for t in re.split(r"[^a-zA-Z0-9가-힣]+", str(v).lower()):
                            if len(t) >= 2:
                                tokens.add(t)

                # 3) 전체 JSON 텍스트에서 .py 파일 참조 추출 (stack trace 등)
                rt_text = json.dumps(rt, ensure_ascii=False)
                for ref in re.findall(r"[\w/\\]+\.py", rt_text):
                    stem = Path(ref).stem.lower()
                    if len(stem) >= 3:
                        file_stems.add(stem)

            except (json.JSONDecodeError, AttributeError, TypeError):
                # plain text runtime_context — .py 참조만 추출
                for ref in re.findall(r"[\w/\\]+\.py", runtime_context):
                    stem = Path(ref).stem.lower()
                    if len(stem) >= 3:
                        file_stems.add(stem)

        return {
            "tokens": tokens,
            "url_segments": url_segments,
            "file_stems": file_stems,
            "mismatch_fields": mismatch_fields,
            # LLM 추출 필드 — 기본값 빈 set (LLM 성공 시 채워짐)
            "endpoints": set(),
            "functions": set(),
            "files": set(),
            "models": set(),
            "keywords": set(),
            "from_llm": False,
        }

    async def _extract_clues_with_llm(
        self,
        error_code: str,
        summary: str,
        mismatches: list,
        runtime_context: str,
    ) -> dict[str, Any]:
        """LLM으로 에러 관련 코드 단서를 추출한다. 실패 시 정규식 fallback.

        LLM 추출 결과(endpoints, files, functions, models, keywords)를
        정규식 결과와 병합해 반환한다.
        """
        regex_clues = self._extract_clues(error_code, summary, mismatches, runtime_context)

        mismatch_text = (
            ", ".join(f"{m.get('field')}({m.get('ui_value')}→{m.get('api_value')})"
                      for m in mismatches)
            if mismatches else "없음"
        )
        runtime_snippet = runtime_context[:600] if runtime_context else "없음"

        prompt = (
            "에러 정보를 분석해 관련 코드 요소를 추출해라.\n\n"
            f"error_code: {error_code or '없음'}\n"
            f"summary: {summary or '없음'}\n"
            f"mismatches: {mismatch_text}\n"
            f"runtime: {runtime_snippet}\n\n"
            "아래 JSON 형식으로만 반환해라:\n"
            '{\n'
            '  "endpoints": ["관련 API 경로 세그먼트. 예: orders, payment"],\n'
            '  "files": ["관련 파일명(확장자 제외). 예: orders, payment_service"],\n'
            '  "functions": ["관련 함수/핸들러명. 예: create_order"],\n'
            '  "models": ["관련 모델/클래스명. 예: OrderOut"],\n'
            '  "keywords": ["기타 검색 키워드"]\n'
            '}'
        )

        try:
            response = await self.llm.chat(
                system_prompt="코드 단서 추출 전문가야. JSON만 반환해라.",
                user_prompt=prompt,
            )
            cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", response.content).strip()
            data = json.loads(cleaned)

            llm_clues = {
                "endpoints": set(str(e).lower().strip("/") for e in data.get("endpoints", [])),
                "functions": set(str(f).lower() for f in data.get("functions", [])),
                "files":     set(str(f).lower() for f in data.get("files", [])),
                "models":    set(str(m) for m in data.get("models", [])),
                "keywords":  set(str(k).lower() for k in data.get("keywords", [])),
                "from_llm":  True,
            }
            # url_segments, file_stems, tokens에 LLM 결과도 병합
            regex_clues["url_segments"] |= llm_clues["endpoints"]
            regex_clues["file_stems"]   |= llm_clues["files"]
            regex_clues["tokens"]       |= llm_clues["keywords"]

            self.logger.info(
                "codebase_index_llm_clues_extracted",
                endpoints=sorted(llm_clues["endpoints"]),
                files=sorted(llm_clues["files"]),
                functions=sorted(llm_clues["functions"]),
                models=sorted(llm_clues["models"]),
            )
            return {**regex_clues, **llm_clues}

        except Exception as e:
            self.logger.warning(
                "codebase_index_llm_clues_failed",
                error=str(e),
                fallback="regex",
            )
            return regex_clues

    @staticmethod
    def _score_endpoint(ep: dict, clues: dict[str, Any]) -> int:
        """endpoint 하나의 관련성 점수를 계산한다.

        LLM 추출 단서 (높은 신뢰도):
          +8  LLM이 추출한 endpoint 세그먼트가 path에 포함
          +6  LLM이 추출한 함수명이 handler에 포함
          +4  LLM이 추출한 파일명이 file_stem에 포함

        정규식 단서 (fallback):
          +5  URL 세그먼트가 path에 포함
          +4  stack trace 파일명이 file_stem과 일치
          +3  토큰이 path에 포함
          +2  토큰이 handler에 포함
          +1  토큰이 파일명에 포함
        """
        path      = ep.get("path", "").lower()
        handler   = ep.get("handler", "").lower()
        file_stem = Path(ep.get("file") or "x.py").stem.lower()

        score = 0
        for ep_seg in clues.get("endpoints", set()):
            if ep_seg in path:  score += 8
        for fn in clues.get("functions", set()):
            if fn in handler:   score += 6
        for f in clues.get("files", set()):
            if f in file_stem or file_stem in f: score += 4

        for t in clues["tokens"]:
            if t in path:      score += 3
            if t in handler:   score += 2
            if t in file_stem: score += 1
        for seg in clues["url_segments"]:
            if seg in path:    score += 5
        for fs in clues["file_stems"]:
            if fs in file_stem or file_stem in fs: score += 4
        return score

    @staticmethod
    def _score_model(model: dict, clues: dict[str, Any]) -> int:
        """model 하나의 관련성 점수를 계산한다.

        LLM 추출 단서 (높은 신뢰도):
          +8  LLM이 추출한 모델명이 name에 포함
          +4  LLM이 추출한 파일명이 file_stem에 포함

        정규식 단서 (fallback):
          +5  mismatch field명이 fields에 포함
          +4  stack trace 파일명이 file_stem과 일치
          +3  토큰이 model name에 포함
          +2  토큰이 field명에 포함
          +1  토큰이 파일명에 포함
        """
        name       = model.get("name", "").lower()
        fields_str = " ".join(model.get("fields") or []).lower()
        file_stem  = Path(model.get("file") or "x.py").stem.lower()

        score = 0
        for m in clues.get("models", set()):
            if m.lower() in name or name in m.lower(): score += 8
        for f in clues.get("files", set()):
            if f in file_stem or file_stem in f:       score += 4

        for t in clues["tokens"]:
            if t in name:        score += 3
            if t in fields_str:  score += 2
            if t in file_stem:   score += 1
        for mf in clues["mismatch_fields"]:
            if mf in fields_str: score += 5
        for fs in clues["file_stems"]:
            if fs in file_stem or file_stem in fs:     score += 4
        return score

    # ── 컨텍스트 처리 유틸 ─────────────────────────────────────────────────────

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
