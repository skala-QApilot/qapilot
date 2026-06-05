"""시나리오-액션 매핑 Agent.

구조화된 시나리오를 UI 액션 리스트, API 매핑,
검증 포인트로 분해하고 실제 셀렉터/엔드포인트와 매칭한다.

담당: C
Created: 2026-05-07
"""

from __future__ import annotations

import asyncio
import difflib
import json
import re
from typing import Any

from qapilot.agents.base_agent import BaseAgent
from qapilot.shared.errors import AgentExecutionError, ErrorCode
from qapilot.shared.llm_client import LLMClient
from qapilot.shared.schemas import ActionMapping, ActionStep, ExecuteResult


MAX_TC_PER_BATCH = 50
MAX_TS_PER_BATCH = 10

# 이슈 #129 Step A — TC-별 LLM 호출 분할. 이슈 #107 (CodeGenerator) 의 동일 패턴.
# OpenAI rate limit + 토큰 사용량 균형. 이전 단일 batch 호출의 token 한계로 인한 부분
# 잘림 (78 TC → 21 TC 누락) 사례 (trace `a86603b9`) 해결.
_MAX_CONCURRENT_LLM_CALLS = 5

# 이슈 #129 Step B — assert step 의 then 절 환각 차단용 fuzzy match.
# UITestTool 옵션 B (이슈 #115) 의 difflib.SequenceMatcher + 임계값 0.6 + 포함관계 +0.2
# 와 동일 알고리즘 — Agent 단 정규화 + Tool 런타임 보정 의 일관성.
_FUZZY_MATCH_THRESHOLD = 0.6
_FUZZY_MATCH_SUBSTRING_BONUS = 0.2
_FUZZY_MATCH_SHARED_SUBSTRING_BONUS = 0.15
_FRONTEND_CANDIDATE_FILE_LIMIT = 4
_FRONTEND_CANDIDATE_ELEMENT_LIMIT = 40
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

# assert 계열 action — Step B 의 frontend.json 매칭 적용 대상.
_ASSERT_ACTIONS = {
    "assert", "assert_visible", "assert_hidden", "assert_text",
    "assert_value", "assert_enabled", "assert_disabled", "assert_count",
}

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

    # 이슈 #129 Step B — _normalize_selector_fields 가 _execute 외 직접 호출되는 경우 (단위
    # 테스트 등) 의 AttributeError 방지. 정상 흐름에서는 _execute 진입 시 갱신.
    _frontend_dom_index: list[dict] = []

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
            return ExecuteResult(
                result={"action_mappings": [], "failed_tcs": []}, confidence=1.0
            )

        # 이슈 #127: frontend DOM 인덱스 로드 — LLM 호출 컨텍스트 주입 + Step B 정규화 활용.
        frontend_dom = self._load_frontend_dom(context)
        # 이슈 #129 Step B: _normalize_selector_fields 가 instance state 로 접근.
        self._frontend_dom_index = frontend_dom

        # 이슈 #129 Step A: TC-별 LLM 호출 분할 — 이전 batch 호출의 token 한계로 인한
        # 부분 잘림 (trace `a86603b9` 의 78→21 누락) 해결. spec §4.5.1 의 ActionMapper
        # "가능한 한 실패하지 않고 표준화" 정신 확장 — 한 TC LLM 실패가 다른 TC 차단 X.
        sem = asyncio.Semaphore(_MAX_CONCURRENT_LLM_CALLS)
        tc_jobs: list[tuple[dict, dict]] = [
            (ts, tc)
            for ts in scenarios
            for tc in (ts.get("test_cases") or [])
        ]
        if not tc_jobs:
            return ExecuteResult(
                result={"action_mappings": [], "failed_tcs": []}, confidence=1.0
            )

        tasks = [
            self._call_single_tc(sem, ts, tc, endpoints, frontend_dom, last_error)
            for ts, tc in tc_jobs
        ]
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)

        all_mappings: list[ActionMapping] = []
        failed_tcs: list[dict] = []
        for (ts, tc), outcome in zip(tc_jobs, outcomes):
            tc_id = tc.get("tc_id") or "unknown"
            if isinstance(outcome, BaseException):
                failed_tcs.append({
                    "tc_id": tc_id,
                    "ts_id": ts.get("ts_id") or "",
                    "error_type": type(outcome).__name__,
                    "error": str(outcome),
                })
                self.logger.warning(
                    "action_mapping_tc_skip",
                    tc_id=tc_id,
                    ts_id=ts.get("ts_id") or "",
                    error_type=type(outcome).__name__,
                    error=str(outcome),
                )
                continue
            if outcome is None:
                # LLM 응답이 비어 ActionMapping 0건 — silent skip 도 fail 카운트.
                failed_tcs.append({
                    "tc_id": tc_id,
                    "ts_id": ts.get("ts_id") or "",
                    "error_type": "EmptyMapping",
                    "error": "LLM 응답에서 단일 ActionMapping 추출 실패",
                })
                continue
            all_mappings.append(outcome)

        confidence = self._calc_confidence(all_mappings, scan_result is not None)
        if failed_tcs:
            confidence = confidence * len(all_mappings) / max(len(tc_jobs), 1)

        self.logger.info(
            "action_mappings_generated",
            mappings=len(all_mappings),
            failed=len(failed_tcs),
            confidence=round(confidence, 3),
        )
        return ExecuteResult(
            result={"action_mappings": all_mappings, "failed_tcs": failed_tcs},
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
        self,
        batch: list[dict],
        endpoints: list[dict],
        frontend_dom: list[dict],
        last_error: str | None,
    ) -> list[ActionMapping]:
        """단일 배치를 LLM으로 ActionMapping 변환한다.

        이슈 #127: frontend_dom 인덱스를 LLM 호출 컨텍스트에 주입.

        주: 이슈 #129 Step A 이후 `_execute` 는 TC-별 분할 (`_call_single_tc`) 을 사용.
        본 메서드는 하위 호환·외부 caller 가능성·테스트 참조용으로 유지.
        """
        frontend_candidates = self._select_frontend_candidates(batch, frontend_dom)
        user_prompt = self.prompts.render(
            scenarios=self._format_scenarios(batch),
            endpoints=self._format_endpoints(endpoints),
            frontend_dom=self._format_frontend_dom(frontend_candidates),
        )
        user_prompt = self.with_correction_hint(user_prompt, last_error)
        response = await self.llm.chat(
            system_prompt=self.prompts.system(),
            user_prompt=user_prompt,
        )
        return self._parse_action_mappings(response.content)

    def _create_tc_llm(self) -> LLMClient:
        """이슈 #140: per-TC subtask LLM client. 테스트 override point.

        `self.llm` 의 누적 토큰이 다른 TC 호출에 누적되어 SYSTEM_002 임계 도달 시
        후반 TC 전체 skip 되던 문제 차단. PR #130 의 \"한 TC = 1 task\" 의도 정합.
        """
        return LLMClient(self._config.llm, trace_id=self.trace_id)

    async def _call_single_tc(
        self,
        sem: asyncio.Semaphore,
        ts: dict,
        tc: dict,
        endpoints: list[dict],
        frontend_dom: list[dict],
        last_error: str | None,
    ) -> ActionMapping | None:
        """단일 TC 를 LLM 으로 ActionMapping 변환 (이슈 #129 Step A).

        한 TC 의 LLM 응답 실패가 다른 TC 의 매핑을 차단하지 않음. spec §4.5.1 의
        \"ActionMapper 가능한 한 실패하지 않고 표준화\" 정신 확장 — batch 차원의
        부분 잘림 (trace `a86603b9` 의 78→21) 을 TC 차원의 부분 graceful 로 해결.

        scenario 는 TS slice (단일 TC 만 포함) 로 전달 — LLM 호출당 토큰 절감 +
        해당 TC 의 비즈니스 의도 (given/when/then, name) 보존.

        이슈 #140 (2026-05-21): per-TC LLMClient 인스턴스 분리.
        `self.llm` 의 `total_tokens` 누적이 다른 TC 호출에 누적되어
        `max_tokens_per_task` (config 200000) 임계 도달 시 후반 TC 전체가
        SYSTEM_002 로 skip 되던 문제 해결 (trace `42919a33` 의 TS-007~010 27 TC).
        PR #130 의 \"한 TC = 1 task\" 의도와 LLMClient 의 \"한 agent = 한 task\"
        누적 정책 충돌 해소. self.llm 은 agent_complete 보고용 누적 합산만 유지.

        반환:
            ActionMapping (성공) / None (LLM 응답 빈 result). raise (LLM/JSON 실패) 시
            `_execute` 의 asyncio.gather(return_exceptions=True) 가 흡수.
        """
        sliced_ts = dict(ts)
        sliced_ts["test_cases"] = [tc]
        batch = [sliced_ts]
        frontend_candidates = self._select_frontend_candidates(batch, frontend_dom)

        # 이슈 #140: TC 마다 새 LLMClient — 누적 정책 충돌 해결
        tc_llm = self._create_tc_llm()

        async with sem:
            user_prompt = self.prompts.render(
                scenarios=self._format_scenarios(batch),
                endpoints=self._format_endpoints(endpoints),
                frontend_dom=self._format_frontend_dom(frontend_candidates),
            )
            user_prompt = self.with_correction_hint(user_prompt, last_error)
            response = await tc_llm.chat(
                system_prompt=self.prompts.system(),
                user_prompt=user_prompt,
            )
            mappings = self._parse_action_mappings(response.content)

        # agent_complete 보고용 누적 합산 (이슈 #140)
        self.llm.total_input_tokens += tc_llm.total_input_tokens
        self.llm.total_output_tokens += tc_llm.total_output_tokens
        self.llm.total_cost_usd = round(self.llm.total_cost_usd + tc_llm.total_cost_usd, 6)

        if not mappings:
            return None
        # 단일 TC 입력 — 응답도 단일 mapping. 다중 시 첫 항목.
        return mappings[0]

    def _load_frontend_dom(self, context: dict[str, Any]) -> list[dict]:
        """frontend DOM 인덱스 로드 — context 우선, 없으면 qapilot_dir/DB/S3 순 fallback.

        우선순위:
        1) context["frontend_dom"] 직접 주입
        2) context["frontend_index_path"]
        3) context["qapilot_dir"]/codebase-index/frontend.json
        4) DB 메타 + S3 mirror (service_id / commit_hash 제공 시)
        5) 레거시 CWD `.qapilot/codebase-index/frontend.json`
        """
        from pathlib import Path

        from qapilot.db.code_reader import load_codebase_index
        from qapilot.tools.frontend_dom_scanner import load_frontend_index

        # 1) context 우선 (테스트/외부 caller 가 직접 전달 가능)
        ctx_dom = context.get("frontend_dom")
        if isinstance(ctx_dom, list):
            return ctx_dom

        # 2) 명시 경로
        index_path = context.get("frontend_index_path")
        if index_path:
            elements = load_frontend_index(Path(str(index_path)))
            if elements:
                return elements

        # 3) qapilot_dir 기준 디스크 fallback
        qapilot_dir = context.get("qapilot_dir")
        if qapilot_dir:
            elements = load_frontend_index(
                Path(str(qapilot_dir)) / "codebase-index" / "frontend.json"
            )
            if elements:
                self.logger.info("frontend_dom_index_loaded", element_count=len(elements))
                return elements

        # 4) DB+S3 mirror fallback
        service_id = context.get("service_id")
        if service_id:
            payload = load_codebase_index(
                str(service_id),
                "frontend",
                commit_hash=context.get("commit_hash"),
            )
            if isinstance(payload, dict):
                elements = list(payload.get("elements") or [])
                if elements:
                    self.logger.info(
                        "frontend_dom_index_loaded_from_mirror",
                        element_count=len(elements),
                        service_id=service_id,
                    )
                    return elements

        # 5) 레거시 CWD fallback
        elements = load_frontend_index(Path(".qapilot") / "codebase-index" / "frontend.json")
        if elements:
            self.logger.info(
                "frontend_dom_index_loaded",
                element_count=len(elements),
            )
        else:
            self.logger.debug(
                "frontend_dom_index_empty",
                hint="qapilot init 또는 rescan 으로 .qapilot/codebase-index/frontend.json 생성 권장",
            )
        return elements

    def _format_frontend_dom(self, elements: list[dict]) -> str:
        """frontend DOM element list 를 프롬프트용 문자열로 변환.

        형식: 각 element 의 의미적 정보 (tag, text, placeholder, label, testid, name, id)
        를 한 줄씩 표기. 최대 200 elements 제한 (LLM 토큰 폭증 방지).
        """
        if not elements:
            return "인덱스 없음 (qapilot init/rescan 으로 .qapilot/codebase-index/frontend.json 생성 시 활용 가능)"

        lines: list[str] = []
        for el in elements[:200]:
            parts: list[str] = []
            tag = el.get("tag") or ""
            if tag:
                parts.append(f"<{tag}>")
            for key in ("text", "placeholder", "label", "testid", "name", "id"):
                val = (el.get(key) or "").strip()
                if val:
                    parts.append(f'{key}="{val}"')
            file_label = el.get("file") or ""
            if file_label:
                parts.append(f"(from {file_label})")
            if parts:
                lines.append("- " + " ".join(parts))
        return "\n".join(lines) if lines else "인덱스 없음"

    def _select_frontend_candidates(self, batch: list[dict], frontend_dom: list[dict]) -> list[dict]:
        """시나리오와 관련된 frontend 후보만 추려 LLM 컨텍스트를 줄인다."""
        if not frontend_dom:
            return []

        scenario_text = self._scenario_text_for_candidates(batch)
        if not scenario_text.strip():
            return frontend_dom[:_FRONTEND_CANDIDATE_ELEMENT_LIMIT]

        scored: list[tuple[float, dict]] = []
        for el in frontend_dom:
            score = self._frontend_candidate_score(scenario_text, el)
            if score > 0:
                scored.append((score, el))

        if not scored:
            return frontend_dom[:_FRONTEND_CANDIDATE_ELEMENT_LIMIT]

        file_counts: dict[str, int] = {}
        file_best: dict[str, float] = {}
        for score, el in scored:
            file_key = str(el.get("file") or "")
            file_counts[file_key] = file_counts.get(file_key, 0) + 1
            file_best[file_key] = max(file_best.get(file_key, 0.0), score)

        max_file_score = max(file_best.values()) if file_best else 0.0
        score_cutoff = max(1.0, max_file_score * 0.7)
        top_files = {
            file_key
            for file_key, _ in sorted(
                (
                    item for item in file_best.items()
                    if item[1] >= score_cutoff
                ),
                key=lambda item: (item[1], file_counts.get(item[0], 0)),
                reverse=True,
            )[:_FRONTEND_CANDIDATE_FILE_LIMIT]
        }

        picked = [
            el
            for _, el in sorted(scored, key=lambda item: item[0], reverse=True)
            if str(el.get("file") or "") in top_files
        ]
        return picked[:_FRONTEND_CANDIDATE_ELEMENT_LIMIT]

    def _scenario_text_for_candidates(self, batch: list[dict]) -> str:
        parts: list[str] = []
        for ts in batch:
            parts.extend([
                str(ts.get("ts_id") or ""),
                str(ts.get("title") or ts.get("name") or ""),
            ])
            for tc in ts.get("test_cases") or []:
                parts.extend([
                    str(tc.get("tc_id") or ""),
                    str(tc.get("name") or ""),
                    str(tc.get("given") or ""),
                    str(tc.get("when") or ""),
                    str(tc.get("then") or ""),
                ])
        return " ".join(part for part in parts if part).lower()

    def _frontend_candidate_score(self, scenario_text: str, el: dict) -> float:
        score = 0.0
        file_path = str(el.get("file") or "").lower()
        page = str(el.get("page") or "").lower()
        route = str(el.get("route") or "").lower()
        control_type = str(el.get("control_type") or "").lower()

        for token, aliases in _SEMANTIC_ALIASES.items():
            if token in scenario_text:
                if any(alias in file_path or alias in page or alias in route for alias in aliases):
                    score += 1.0
                if any(alias in control_type for alias in aliases):
                    score += 0.3

        if any(term in scenario_text for term in ("회원가입", "가입")):
            if "signup" in file_path or route == "/signup" or page == "signup":
                score += 2.0
        if any(term in scenario_text for term in ("로그인",)):
            if "login" in file_path or route == "/login" or page == "login":
                score += 2.0

        for key in ("text", "placeholder", "label", "testid", "id", "name"):
            value = str(el.get(key) or "").lower()
            if not value:
                continue
            for token in self._extract_meaningful_tokens(scenario_text):
                if token and token in value:
                    score += 0.6

        if control_type in {"submit", "button"} and "버튼" in scenario_text:
            score += 0.5
        if control_type == "form_input" and any(tok in scenario_text for tok in ("이메일", "비밀번호", "이름", "생년월일")):
            score += 0.2
        return score

    def _extract_meaningful_tokens(self, text: str) -> list[str]:
        return [tok for tok in re.split(r"[^0-9a-zA-Z가-힣_/-]+", text) if len(tok) >= 2]

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
        """action 성격에 따라 selector와 selector_type을 보정한다.

        이슈 #129 Step B: assert step 의 selector 가 frontend.json 인덱스의 element
        와 매치 안 하면 가장 유사한 element 로 정규화 — then 절 환각 (예:
        `testid="로그인 성공 메시지 노출"`) 의 결정적 차단. LLM 컨텍스트 주입 (#127)
        의 차상위 안전망.
        """
        if action in _SELECTOR_OPTIONAL_ACTIONS:
            return None, None
        if selector and selector_type:
            if self._frontend_dom_index:
                normalized = self._normalize_selector_via_index(
                    action, str(selector), selector_type, tc_id, step_no
                )
                if normalized is not None:
                    return normalized
            return str(selector), selector_type
        fallback = item.get("expected") or item.get("value") or action
        self._log_normalization(tc_id, step_no, "selector", selector, fallback)
        return str(fallback), selector_type or "text"

    def _normalize_selector_via_index(
        self, action: str, selector: str, selector_type: str, tc_id: str, step_no: int
    ) -> tuple[str, str] | None:
        """selector 를 frontend index 의 실제 element 로 정규화한다.

        assert 뿐 아니라 fill/click 류에도 적용해, 시나리오 자연어가 selector 로 새는
        문제를 LLM 이후 단계에서 한 번 더 줄인다.
        """
        target = selector.strip().lower()
        if not target or not self._frontend_dom_index:
            return None

        best_el: dict | None = None
        best_score = 0.0
        exact_match = False
        for el in self._frontend_dom_index:
            candidates = [
                ("text", el.get("text", "")),
                ("placeholder", el.get("placeholder", "")),
                ("label", el.get("label", "")),
                ("testid", el.get("testid", "")),
                ("page", el.get("page", "")),
                ("route", el.get("route", "")),
            ]
            for _, candidate in candidates:
                cand = (candidate or "").strip().lower()
                if not cand:
                    continue
                if cand == target:
                    best_el = el
                    best_score = 1.0
                    exact_match = True
                    break
                score = difflib.SequenceMatcher(
                    None, self._expand_aliases(target), self._expand_aliases(cand)
                ).ratio()
                if target in cand or cand in target:
                    score += _FUZZY_MATCH_SUBSTRING_BONUS
                elif self._has_meaningful_shared_substring(target, cand):
                    score += _FUZZY_MATCH_SHARED_SUBSTRING_BONUS
                score += self._semantic_bonus(target, cand)
                score += self._action_bonus(action, el, target)
                if score > best_score:
                    best_score = score
                    best_el = el
            if exact_match:
                break

        if best_el is None:
            return None
        if not exact_match and best_score < _FUZZY_MATCH_THRESHOLD:
            return None

        new_selector, new_type = self._preferred_selector_for_action(action, best_el)
        if not new_selector or not new_type:
            return None
        if new_selector == selector and new_type == selector_type:
            return None

        if exact_match and action in _ASSERT_ACTIONS:
            # 기존 assert exact-match 동작은 유지한다.
            return None

        self.logger.info(
            "selector_normalized_via_index",
            tc_id=tc_id,
            step_no=step_no,
            action=action,
            original=selector,
            original_type=selector_type,
            normalized=new_selector,
            normalized_type=new_type,
            score=round(best_score, 3),
        )
        return new_selector, new_type

    def _normalize_assert_selector_via_index(
        self, selector: str, selector_type: str, tc_id: str, step_no: int
    ) -> tuple[str, str] | None:
        """assert step 의 selector 를 frontend.json 인덱스로 정규화 (이슈 #129 Step B).

        매칭 단계:
        1. **정확 매치** — 인덱스의 element 의 testid/text/label/placeholder/aria-label
           중 selector 와 정확 일치 → 정규화 불필요 (그대로 반환 None — 호출자가 원본 유지)
        2. **fuzzy match** — UITestTool 옵션 B 와 동일 알고리즘 (difflib.SequenceMatcher
           + 포함관계 가산점 0.2 + 임계값 0.6) 로 가장 유사한 element 발견
        3. **정규화** — 매칭된 element 의 testid > placeholder > label > text 우선순위로
           selector / selector_type 치환 (가장 안정적인 entry point)
        4. **매치 실패** — None 반환 (호출자가 원본 유지). UITestTool 의 옵션 A/B chain
           이 런타임에 추가 보정 시도.

        매칭 알고리즘 일관성:
        - Agent 정적 정규화 (본 메서드) + Tool 런타임 보정 (UITestTool `_fallback_dom_scan`)
        - 동일 difflib.SequenceMatcher + 동일 임계값 — 이중 방어
        """
        return self._normalize_selector_via_index("assert", selector, selector_type, tc_id, step_no)

    def _preferred_selector_for_action(self, action: str, element: dict) -> tuple[str | None, str | None]:
        """action 성격에 맞는 가장 안정적인 selector 필드를 선택한다."""
        if action in {"fill", "clear", "select", "press", "upload"}:
            priority = ("testid", "label", "placeholder", "text")
        else:
            priority = ("testid", "text", "label", "placeholder")

        for key in priority:
            value = (element.get(key) or "").strip()
            if value:
                return value, key
        return None, None

    def _has_meaningful_shared_substring(self, left: str, right: str) -> bool:
        """한국어 UI 문구의 부분 겹침(예: '회원가입' vs '가입하기')을 약하게 보정한다."""
        if not left or not right:
            return False
        match = difflib.SequenceMatcher(None, left, right).find_longest_match(
            0, len(left), 0, len(right)
        )
        min_len = min(len(left), len(right))
        return match.size >= max(2, min_len // 2)

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
