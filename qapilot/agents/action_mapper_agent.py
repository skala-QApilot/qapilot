"""시나리오-액션 매핑 Agent.

구조화된 시나리오를 UI 액션 리스트, API 매핑,
검증 포인트로 분해하고 실제 셀렉터/엔드포인트와 매칭한다.

담당: C
Created: 2026-05-07
"""

from __future__ import annotations

import asyncio
from datetime import date
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
_ACTION_TOKEN_SET = frozenset({
    "click", "fill", "submit", "press", "upload", "check", "uncheck",
    "select", "hover", "dblclick", "assert",
})
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "email": ("email", "이메일", "mail"),
    "password": ("password", "비밀번호", "pwd", "pass"),
    "name": ("name", "이름", "user name", "username", "사용자 이름"),
    "birth_date": ("birth_date", "birth date", "birth", "생년월일"),
    "guardian_consent": ("guardian_consent", "guardian consent", "법정대리인 동의"),
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
    _metadata_bundle: dict[str, Any] = {}

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
        self._metadata_bundle = self._load_metadata_bundle(context)

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
            route_context="",
            selector_catalog="",
            schema_context="",
            source_context="",
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
        tc_context = self._build_tc_metadata_context(tc)

        async with sem:
            tc_context = await self._select_and_load_tc_sources(tc, tc_context, tc_llm)
            user_prompt = self.prompts.render(
                scenarios=self._format_scenarios(batch),
                endpoints=self._format_endpoints(endpoints),
                frontend_dom=self._format_frontend_dom(frontend_candidates),
                route_context=self._format_route_context(tc_context.get("routes")),
                selector_catalog=self._format_selector_catalog(tc_context.get("selectors")),
                schema_context=self._format_schema_context(tc_context.get("schemas")),
                source_context=self._format_source_context(tc_context.get("sources")),
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
        resolved = self._resolve_mapping_with_frontend(
            mappings[0],
            tc,
            frontend_candidates,
            tc_context=tc_context,
        )
        resolved["mapping_context"] = self._build_mapping_context_refs(tc_context)
        return resolved

    def _build_tc_metadata_context(self, tc: dict[str, Any]) -> dict[str, Any]:
        from qapilot.shared.metadata_filters import (
            filter_schemas_by_api,
            filter_selectors_by_route,
        )

        bundle = getattr(self, "_metadata_bundle", {}) or {}
        selectors = filter_selectors_by_route(bundle.get("selectors"), tc.get("api"))
        schemas = filter_schemas_by_api(bundle.get("schemas"), tc.get("api"))
        routes = self._filter_routes_for_tc(bundle.get("routes"), selectors, tc.get("api"))
        return {
            "selectors": selectors,
            "schemas": schemas,
            "routes": routes,
            "service_id": str(bundle.get("service_id") or ""),
            "commit_sha": str(bundle.get("commit_sha") or ""),
        }

    async def _select_and_load_tc_sources(
        self,
        tc: dict[str, Any],
        tc_context: dict[str, Any],
        tc_llm: LLMClient,
    ) -> dict[str, Any]:
        from qapilot.shared.metadata_filters import pick_source_files_for_tc

        service_id = str(tc_context.get("service_id") or "")
        commit_sha = str(tc_context.get("commit_sha") or "")
        selectors = tc_context.get("selectors")
        schemas = tc_context.get("schemas")
        routes = tc_context.get("routes") or []

        sources: list[dict[str, Any]] = []
        source_candidates: list[dict[str, Any]] = []
        source_status = "not_requested"
        source_selection_method = "none"
        source_selection_notes: list[str] = []

        # 디버그: no_candidates 원인 추적 — commit_sha 유무 + tc.api 파싱 결과를
        # 분기 진입 전에 항상 남긴다 (이전엔 deterministic_candidates 가 있을 때만 로그).
        self.logger.info(
            "action_mapping_source_select_entry",
            tc_id=tc.get("tc_id"),
            api=tc.get("api"),
            service_id=service_id,
            commit_sha_present=bool(commit_sha),
            routes_count=len(routes),
            selectors_by_route=list((selectors or {}).get("by_route") or {}),
            schemas_request=list((schemas or {}).get("request_schemas") or {}),
            schemas_db_models=list((schemas or {}).get("db_models") or {}),
        )

        if commit_sha:
            deterministic_candidates = self._pick_source_targets_for_action_mapping(
                tc,
                routes=routes,
                selectors=selectors,
                schemas=schemas,
            )
            self.logger.info(
                "action_mapping_source_candidates_built",
                tc_id=tc.get("tc_id"),
                api=tc.get("api"),
                candidate_count=len(deterministic_candidates),
                candidates=deterministic_candidates,
            )
            source_candidates.extend(deterministic_candidates)
            selected_targets, selection_method, selection_notes = await self._rerank_source_targets_with_llm(
                tc,
                tc_context,
                deterministic_candidates,
                tc_llm,
            )
            source_selection_method = selection_method
            source_selection_notes = selection_notes
            source_status = "missing" if selected_targets else "no_candidates"

            if deterministic_candidates:
                self.logger.info(
                    "action_mapping_source_targets_selected",
                    tc_id=tc.get("tc_id"),
                    api=tc.get("api"),
                    candidates=deterministic_candidates,
                    selection_method=selection_method,
                )

            for target in selected_targets:
                file_path = target.get("file")
                line_start = target.get("line_start")
                line_end = target.get("line_end")
                content = self._load_source_from_mirror(
                    service_id=service_id,
                    commit_sha=commit_sha,
                    file_path=file_path,
                    line_start=line_start,
                    line_end=line_end,
                )
                if not content:
                    self.logger.info(
                        "action_mapping_source_candidate_missing",
                        tc_id=tc.get("tc_id"),
                        api=tc.get("api"),
                        file=file_path,
                        line_start=line_start,
                        line_end=line_end,
                        reason=target.get("reason"),
                        source="s3_mirror",
                    )
                    continue
                sources.append({
                    "file": file_path,
                    "line_start": line_start,
                    "line_end": line_end,
                    "content": content,
                })

            if not any("/routers/" in str(s.get("file") or "") for s in sources):
                fallback_targets = [
                    {
                        "file": file_path,
                        "line_start": line_start,
                        "line_end": line_end,
                        "reason": "backend router/schema fallback from pick_source_files_for_tc",
                    }
                    for file_path, line_start, line_end in pick_source_files_for_tc(tc, schemas=schemas)
                ]
                for target in fallback_targets:
                    if not any(
                        c.get("file") == target.get("file")
                        and c.get("line_start") == target.get("line_start")
                        and c.get("line_end") == target.get("line_end")
                        for c in source_candidates
                    ):
                        source_candidates.append(target)
                if fallback_targets:
                    self.logger.info(
                        "action_mapping_source_targets_fallback_selected",
                        tc_id=tc.get("tc_id"),
                        api=tc.get("api"),
                        candidates=fallback_targets,
                    )
                for target in fallback_targets:
                    file_path = target.get("file")
                    line_start = target.get("line_start")
                    line_end = target.get("line_end")
                    content = self._load_source_from_mirror(
                        service_id=service_id,
                        commit_sha=commit_sha,
                        file_path=file_path,
                        line_start=line_start,
                        line_end=line_end,
                    )
                    if not content:
                        self.logger.info(
                            "action_mapping_source_candidate_missing",
                            tc_id=tc.get("tc_id"),
                            api=tc.get("api"),
                            file=file_path,
                            line_start=line_start,
                            line_end=line_end,
                            reason=target.get("reason"),
                            source="s3_mirror",
                        )
                        continue
                    sources.append({
                        "file": file_path,
                        "line_start": line_start,
                        "line_end": line_end,
                        "content": content,
                    })
            if sources:
                source_status = "loaded"

        self.logger.info(
            "action_mapping_source_select_result",
            tc_id=tc.get("tc_id"),
            api=tc.get("api"),
            source_status=source_status,
            source_selection_method=source_selection_method,
            source_candidates_count=len(source_candidates),
            sources_count=len(sources),
        )

        return {
            **tc_context,
            "sources": self._dedupe_source_snippets(sources),
            "source_candidates": source_candidates[:6],
            "source_status": source_status,
            "source_selection_method": source_selection_method,
            "source_selection_notes": source_selection_notes[:3],
        }

    def _load_metadata_bundle(self, context: dict[str, Any]) -> dict[str, Any]:
        """action mapping 용 메타데이터 번들 로드.

        정책:
        - qapilot_dir / 로컬 디스크는 읽지 않는다.
        - metadata / source 는 service_id 기준 DB/S3 mirror 만 사용한다.
        """
        service_id = str(context.get("service_id") or "").strip()
        commit_sha = str(context.get("commit_sha") or "").strip()

        bundle: dict[str, Any] = {
            "service_id": service_id,
            "commit_sha": commit_sha,
            "selectors": None,
            "routes": None,
            "schemas": None,
        }

        for kind, sub_kind in (
            ("frontend", "selectors"),
            ("frontend", "routes"),
            ("backend", "schemas"),
        ):
            payload = self._load_metadata_index_from_mirror(
                service_id=service_id,
                kind=kind,
                sub_kind=sub_kind,
                commit_sha=commit_sha or None,
            )
            if payload:
                bundle[sub_kind] = payload
                bundle["commit_sha"] = bundle.get("commit_sha") or str(payload.get("commit_sha") or "")

        return bundle

    def _load_metadata_index_from_mirror(
        self,
        *,
        service_id: str,
        kind: str,
        sub_kind: str,
        commit_sha: str | None,
    ) -> dict[str, Any] | None:
        if not service_id:
            return None

        try:
            from qapilot.shared.scan_storage import load_metadata_index

            return load_metadata_index(service_id, kind, sub_kind, commit_hash=commit_sha)
        except Exception:
            return None

    def _load_source_from_mirror(
        self,
        *,
        service_id: str,
        commit_sha: str,
        file_path: str,
        line_start: int | None = None,
        line_end: int | None = None,
    ) -> str | None:
        if not (service_id and commit_sha and file_path):
            return None

        try:
            from qapilot.shared.scan_storage import load_source

            return load_source(
                service_id,
                commit_sha,
                file_path,
                line_start=line_start,
                line_end=line_end,
            )
        except Exception:
            return None

    def _build_tc_code_context(self, tc: dict[str, Any]) -> dict[str, Any]:
        return self._build_tc_metadata_context(tc)

    async def _rerank_source_targets_with_llm(
        self,
        tc: dict[str, Any],
        tc_context: dict[str, Any],
        candidates: list[dict[str, Any]],
        tc_llm: LLMClient,
    ) -> tuple[list[dict[str, Any]], str, list[str]]:
        if not candidates:
            return [], "none", []
        if len(candidates) <= 2:
            return candidates[:2], "deterministic_small_set", [
                "후보 수가 적어 deterministic 후보를 그대로 사용"
            ]

        prompt = self._build_source_candidate_rerank_prompt(tc, tc_context, candidates)
        try:
            response = await tc_llm.chat(
                system_prompt=(
                    "You rank source file candidates for action mapping grounding. "
                    "Choose up to 3 candidates only from the provided list. "
                    "Never invent files. Return JSON object only."
                ),
                user_prompt=prompt,
            )
            selected, notes = self._parse_source_candidate_rerank_response(response.content, candidates)
            if selected:
                self.logger.info(
                    "action_mapping_source_targets_reranked",
                    tc_id=tc.get("tc_id"),
                    api=tc.get("api"),
                    selected=selected,
                    notes=notes,
                )
                return selected, "llm_rerank", notes
        except Exception as exc:
            self.logger.warning(
                "action_mapping_source_rerank_failed",
                tc_id=tc.get("tc_id"),
                api=tc.get("api"),
                error=str(exc),
            )

        return candidates[:3], "deterministic_fallback", [
            "LLM 재랭크 실패 또는 무효 응답으로 deterministic 상위 후보를 사용"
        ]

    def _build_source_candidate_rerank_prompt(
        self,
        tc: dict[str, Any],
        tc_context: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> str:
        route_lines = [
            f"- path={route.get('path')} component={route.get('component_file') or route.get('component_name') or ''}"
            for route in (tc_context.get("routes") or [])[:3]
        ]
        schema_names = list((tc_context.get("schemas") or {}).get("request_schemas", {}).keys())[:3]
        selector_routes = list(((tc_context.get("selectors") or {}).get("by_route") or {}).keys())[:3]
        candidate_lines = []
        for idx, candidate in enumerate(candidates):
            line_range = ""
            if candidate.get("line_start") is not None and candidate.get("line_end") is not None:
                line_range = f":{candidate.get('line_start')}-{candidate.get('line_end')}"
            candidate_lines.append(
                f"{idx}. {candidate.get('file')}{line_range} | reason={candidate.get('reason')}"
            )
        return "\n".join([
            "# TC",
            json.dumps({
                "tc_id": tc.get("tc_id"),
                "name": tc.get("name"),
                "given": tc.get("given"),
                "when": tc.get("when"),
                "then": tc.get("then"),
                "api": tc.get("api"),
            }, ensure_ascii=False),
            "# Related Routes",
            "\n".join(route_lines) or "(none)",
            "# Related Selector Routes",
            "\n".join(f"- {route}" for route in selector_routes) or "(none)",
            "# Related Schemas",
            "\n".join(f"- {name}" for name in schema_names) or "(none)",
            "# Candidate Sources",
            "\n".join(candidate_lines),
            "# Instruction",
            (
                "Select up to 3 candidates that are most directly useful for grounding the TC. "
                "Prefer route component files, selector extracted_from files, and API handler/schema files. "
                "Do not invent indices. Return JSON only: "
                '{"selected_indices":[0,1],"rationale":["...","..."]}'
            ),
        ])

    def _parse_source_candidate_rerank_response(
        self,
        content: str,
        candidates: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        payload = json.loads((content or "").strip())
        indices = payload.get("selected_indices") or []
        rationales = payload.get("rationale") or []
        selected: list[dict[str, Any]] = []
        seen: set[int] = set()
        for raw_idx in indices[:3]:
            try:
                idx = int(raw_idx)
            except (TypeError, ValueError):
                continue
            if idx < 0 or idx >= len(candidates) or idx in seen:
                continue
            seen.add(idx)
            selected.append(candidates[idx])
        return selected, [str(item) for item in rationales[:3]]

    def _filter_routes_for_tc(
        self,
        routes_index: dict[str, Any] | None,
        selectors: dict[str, Any] | None,
        api: str | None,
    ) -> list[dict[str, Any]]:
        if not routes_index:
            return []

        route_records = list(routes_index.get("routes") or [])
        if not route_records:
            return []

        selected_routes = set((selectors or {}).get("by_route", {}).keys())
        if selected_routes:
            matched = [r for r in route_records if str(r.get("path") or "") in selected_routes]
            if matched:
                return matched

        _, path = self._parse_api_for_context(api)
        key = self._last_segment_for_context(path)
        if not key:
            return []

        matched = [
            r for r in route_records
            if self._last_segment_for_context(str(r.get("path") or "")).lower() == key
        ]
        if matched:
            return matched

        if any(term in (path or "").lower() for term in ("signup", "login", "auth")):
            return [
                r for r in route_records
                if any(term in str(r.get("path") or "").lower() for term in ("signup", "login"))
            ]
        return []

    def _pick_source_targets_for_action_mapping(
        self,
        tc: dict[str, Any],
        *,
        routes: list[dict[str, Any]],
        selectors: dict[str, Any] | None,
        schemas: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        from qapilot.shared.metadata_filters import pick_source_files_for_tc

        out: list[dict[str, Any]] = []
        seen: set[tuple[str, int | None, int | None]] = set()

        def _add(
            file_path: str | None,
            line_start: int | None = None,
            line_end: int | None = None,
            *,
            reason: str,
        ) -> None:
            key = (str(file_path or ""), line_start, line_end)
            if not file_path or key in seen:
                return
            seen.add(key)
            out.append({
                "file": str(file_path),
                "line_start": line_start,
                "line_end": line_end,
                "reason": reason,
            })

        for route in routes[:1]:
            _add(
                route.get("component_file"),
                None,
                None,
                reason=f"matched frontend route {route.get('path')}",
            )

        for route_path, route_data in list(((selectors or {}).get("by_route") or {}).items()):
            for group_key in ("inputs", "buttons", "outputs", "dynamic"):
                group = list(route_data.get(group_key) or [])
                for record in group[:1]:
                    extracted = record.get("extracted_from") or {}
                    _add(
                        extracted.get("file"),
                        None,
                        None,
                        reason=f"selector catalog {route_path} {group_key} exemplar",
                    )

        for item in pick_source_files_for_tc(tc, schemas=schemas):
            _add(
                item[0],
                item[1],
                item[2],
                reason="backend router/schema heuristic from TC.api",
            )

        return out[:4]

    def _dedupe_source_snippets(self, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[tuple[str, int | None, int | None]] = set()
        out: list[dict[str, Any]] = []
        for snippet in sources:
            key = (
                str(snippet.get("file") or ""),
                snippet.get("line_start"),
                snippet.get("line_end"),
            )
            if key in seen:
                continue
            seen.add(key)
            out.append(snippet)
        return out[:4]

    def _format_route_context(self, routes: list[dict[str, Any]] | None) -> str:
        if not routes:
            return "관련 route 정보 없음"
        lines: list[str] = []
        for route in routes[:3]:
            guards = [g.get("name") for g in (route.get("guards") or []) if g.get("name")]
            lines.append(
                f"- path={route.get('path')} component={route.get('component_file') or route.get('component_name') or ''} "
                f"guards={guards or []} meta={route.get('meta') or {}}"
            )
        return "\n".join(lines)

    def _format_selector_catalog(self, selectors: dict[str, Any] | None) -> str:
        by_route = (selectors or {}).get("by_route") or {}
        if not by_route:
            return "관련 selector 카탈로그 없음"
        lines: list[str] = []
        for route, route_data in list(by_route.items())[:2]:
            lines.append(f"[route] {route}")
            for item in list(route_data.get("inputs") or [])[:8]:
                lines.append(
                    f"- input testid={item.get('testid')} label={item.get('label')} "
                    f"placeholder={item.get('placeholder')} v_model={item.get('v_model')} "
                    f"validators={item.get('validators') or []}"
                )
            for item in list(route_data.get("buttons") or [])[:4]:
                disabled = ((item.get("disabled_when") or {}).get("expr") if isinstance(item.get("disabled_when"), dict) else None)
                lines.append(
                    f"- button testid={item.get('testid')} label={item.get('label')} "
                    f"role={item.get('form_role')} disabled_when={disabled}"
                )
            for item in list(route_data.get("outputs") or [])[:4]:
                lines.append(
                    f"- output testid={item.get('testid')} kind={item.get('semantic_kind')} "
                    f"purpose={item.get('semantic_purpose')} v_if={item.get('v_if')}"
                )
            for item in list(route_data.get("dynamic") or [])[:4]:
                lines.append(
                    f"- dynamic testid={item.get('testid')} purpose={item.get('semantic_purpose')} v_if={item.get('v_if')}"
                )
        return "\n".join(lines)

    def _format_schema_context(self, schemas: dict[str, Any] | None) -> str:
        if not schemas:
            return "관련 backend schema 없음"
        lines: list[str] = []
        for name, spec in list((schemas.get("request_schemas") or {}).items())[:2]:
            lines.append(f"[request_schema] {name}")
            for field in spec.get("fields") or []:
                validators = [
                    f"{v.get('kind')}({v.get('value')})" if v.get("value") is not None else str(v.get("kind"))
                    for v in (field.get("validators") or [])
                ]
                lines.append(
                    f"- {field.get('name')} type={field.get('type')} required={field.get('required')} "
                    f"sensitive={field.get('sensitive')} validators={validators}"
                )
        for name, spec in list((schemas.get("response_schemas") or {}).items())[:2]:
            lines.append(f"[response_schema] {name}")
            for status in spec.get("status_codes") or []:
                lines.append(
                    f"- status={status.get('code')} desc={status.get('description')} "
                    f"handler={status.get('extracted_from_handler')}"
                )
        return "\n".join(lines) if lines else "관련 backend schema 없음"

    def _format_source_context(self, sources: list[dict[str, Any]] | None) -> str:
        if not sources:
            return "관련 원본 소스 없음"
        lines: list[str] = []
        for snippet in sources[:4]:
            file_path = snippet.get("file") or ""
            line_start = snippet.get("line_start")
            line_end = snippet.get("line_end")
            header = f"[source] {file_path}"
            if line_start and line_end:
                header += f":{line_start}-{line_end}"
            lines.append(header)
            lines.append(str(snippet.get("content") or "").strip())
        return "\n".join(lines)

    def _build_mapping_context_refs(self, tc_context: dict[str, Any]) -> dict[str, Any]:
        routes = tc_context.get("routes") or []
        selectors = (tc_context.get("selectors") or {}).get("by_route") or {}
        schemas = tc_context.get("schemas") or {}
        sources = tc_context.get("sources") or []
        source_candidates = tc_context.get("source_candidates") or []
        source_status = tc_context.get("source_status") or "not_requested"
        source_selection_method = tc_context.get("source_selection_method") or "none"
        source_selection_notes = tc_context.get("source_selection_notes") or []

        return {
            "route_refs": [
                {
                    "path": route.get("path"),
                    "component_file": route.get("component_file"),
                    "component_name": route.get("component_name"),
                }
                for route in routes[:3]
            ],
            "selector_route_refs": [
                {
                    "route": route_path,
                    "input_count": len((route_data or {}).get("inputs") or []),
                    "button_count": len((route_data or {}).get("buttons") or []),
                    "output_count": len((route_data or {}).get("outputs") or []),
                    "dynamic_count": len((route_data or {}).get("dynamic") or []),
                }
                for route_path, route_data in list(selectors.items())[:3]
            ],
            "schema_refs": {
                "request_schemas": list((schemas.get("request_schemas") or {}).keys())[:5],
                "response_schemas": list((schemas.get("response_schemas") or {}).keys())[:5],
                "db_models": list((schemas.get("db_models") or {}).keys())[:5],
            },
            "source_refs": [
                {
                    "file": snippet.get("file"),
                    "line_start": snippet.get("line_start"),
                    "line_end": snippet.get("line_end"),
                }
                for snippet in sources[:5]
            ],
            "source_status": source_status,
            "source_selection_method": source_selection_method,
            "source_selection_notes": [str(item) for item in source_selection_notes[:3]],
            "source_candidates": [
                {
                    "file": candidate.get("file"),
                    "line_start": candidate.get("line_start"),
                    "line_end": candidate.get("line_end"),
                    "reason": candidate.get("reason"),
                }
                for candidate in source_candidates[:6]
            ],
        }

    def _slice_source_lines(
        self,
        text: str,
        line_start: int | None,
        line_end: int | None,
    ) -> str:
        if line_start is None and line_end is None:
            return text
        lines = text.splitlines(keepends=True)
        start_idx = max(0, (line_start or 1) - 1)
        end_idx = len(lines) if line_end is None else min(len(lines), line_end)
        return "".join(lines[start_idx:end_idx])

    def _parse_api_for_context(self, api: str | None) -> tuple[str | None, str | None]:
        m = re.match(r"^\s*(GET|POST|PUT|PATCH|DELETE)\s+(/\S+)\s*$", str(api or ""), re.IGNORECASE)
        if not m:
            return None, None
        return m.group(1).upper(), m.group(2)

    def _last_segment_for_context(self, path: str | None) -> str:
        parts = [p for p in str(path or "").split("/") if p]
        return parts[-1] if parts else ""

    def _load_frontend_dom(self, context: dict[str, Any]) -> list[dict]:
        """frontend DOM 인덱스 로드 — context 우선, 없으면 DB/S3 mirror fallback.

        정책:
        1) context["frontend_dom"] 직접 주입
        2) DB 메타 + S3 mirror (service_id / commit_hash 제공 시)
        3) 그 외 빈 list
        """

        from qapilot.db.code_reader import load_codebase_index

        # 1) context 우선 (테스트/외부 caller 가 직접 전달 가능)
        ctx_dom = context.get("frontend_dom")
        if isinstance(ctx_dom, list):
            return ctx_dom

        # 2) DB+S3 mirror fallback
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

        self.logger.debug(
            "frontend_dom_index_empty",
            hint="service_id mirror 에 frontend index 가 없음",
        )
        return []

    def _format_frontend_dom(self, elements: list[dict]) -> str:
        """frontend DOM element list 를 프롬프트용 문자열로 변환.

        형식: 각 element 의 의미적 정보 (tag, text, placeholder, label, testid, name, id)
        를 한 줄씩 표기. 최대 200 elements 제한 (LLM 토큰 폭증 방지).
        """
        if not elements:
            return "인덱스 없음 (service_id mirror 에 frontend index 없음)"

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
        step: ActionStep = {
            "step_no": step_no,
            "action": action,
            "selector": selector,
            "selector_type": selector_type,
            "value": value,
            "expected": expected,
            "api_endpoint": item.get("api_endpoint"),
        }
        if item.get("target_name") is not None:
            step["target_name"] = str(item.get("target_name") or "").strip() or None
        if item.get("target_kind") is not None:
            step["target_kind"] = str(item.get("target_kind") or "").strip() or None
        return step

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
            return str(selector), selector_type
        return None, None

    def _resolve_mapping_with_frontend(
        self,
        action_mapping: ActionMapping,
        tc: dict[str, Any],
        frontend_dom: list[dict],
        *,
        tc_context: dict[str, Any] | None = None,
    ) -> ActionMapping:
        """LLM 이 만든 step intent 를 frontend index 원소로만 resolve 한다."""
        mapping = dict(action_mapping)
        scenario_text = " ".join(str(tc.get(key) or "") for key in ("name", "given", "when", "then"))
        route_hint = (
            self._route_hint_from_tc_context(tc_context)
            or self._route_hint_from_elements(frontend_dom)
            or self._route_hint_from_tc(tc)
        )
        candidate_dom = self._route_scoped_frontend_dom(frontend_dom, tc_context, route_hint)
        steps: list[ActionStep] = []

        for raw_step in mapping.get("steps") or []:
            step = dict(raw_step)
            action = str(step.get("action") or "")
            if action in _SELECTOR_OPTIONAL_ACTIONS:
                step["selector"] = None
                step["selector_type"] = None
                steps.append(step)
                continue

            intent = self._infer_step_intent(step, tc, scenario_text)
            if self._should_skip_toggle_step(action, intent, tc):
                self.logger.info(
                    "action_mapping_toggle_step_skipped",
                    tc_id=mapping.get("tc_id"),
                    step_no=step.get("step_no"),
                    action=action,
                    target_name=intent.get("target_name"),
                )
                continue
            resolved = self._resolve_selector_from_intent(
                action,
                intent,
                candidate_dom,
                route_hint,
                scenario_text,
            )
            if resolved is None:
                self.logger.warning(
                    "action_mapping_selector_unresolved",
                    tc_id=mapping.get("tc_id"),
                    step_no=step.get("step_no"),
                    action=action,
                    intent=intent,
                )
                step["selector"] = None
                step["selector_type"] = None
            else:
                step["selector"] = resolved["selector"]
                step["selector_type"] = resolved["selector_type"]
            if intent.get("target_name"):
                step["target_name"] = intent["target_name"]
            if intent.get("target_kind"):
                step["target_kind"] = intent["target_kind"]
            step = self._downgrade_self_referential_assert_text(step, mapping.get("tc_id"))
            if action == "assert":
                # 범용 assert(주로 API 응답/비즈니스 로직 검증)는 DOM selector가
                # 없는 경우가 많아 LLM이 만든 expected는 버리고, TC의 then(검증 항목
                # 자체)을 expected로 채워 대시보드에 "뭘 검증하는지"가 보이게 한다.
                step["expected"] = str(tc.get("then") or "").strip() or None
            elif action == "assert_visible":
                step["expected"] = None
            steps.append(step)

        steps = self._repair_assert_steps(
            steps,
            tc,
            candidate_dom,
            route_hint,
            scenario_text,
            mapping.get("tc_id"),
        )
        steps = self._ensure_navigate_step(steps, route_hint)
        mapping["steps"] = steps
        return mapping

    def _downgrade_self_referential_assert_text(
        self,
        step: ActionStep,
        tc_id: str | None,
    ) -> ActionStep:
        action = str(step.get("action") or "").strip().lower()
        selector = str(step.get("selector") or "").strip()
        expected = str(step.get("expected") or "").strip()
        if action != "assert_text" or not selector or not expected:
            return step
        if selector != expected:
            return step

        updated = dict(step)
        updated["action"] = "assert_visible"
        updated["expected"] = None
        self.logger.info(
            "action_mapping_assert_text_downgraded",
            tc_id=tc_id,
            step_no=step.get("step_no"),
            selector=selector,
        )
        return updated

    def _route_hint_from_tc_context(self, tc_context: dict[str, Any] | None) -> str | None:
        selectors = ((tc_context or {}).get("selectors") or {}).get("by_route") or {}
        if len(selectors) == 1:
            return next(iter(selectors.keys()))
        routes = list((tc_context or {}).get("routes") or [])
        if len(routes) == 1:
            route_path = str(routes[0].get("path") or "").strip()
            if route_path:
                return route_path
        return None

    def _route_scoped_frontend_dom(
        self,
        frontend_dom: list[dict],
        tc_context: dict[str, Any] | None,
        route_hint: str | None,
    ) -> list[dict]:
        catalog_elements = self._catalog_elements_from_tc_context(tc_context)
        route_dom = list(frontend_dom or [])
        if route_hint:
            route_dom = [
                el for el in route_dom
                if str(el.get("route") or "").strip() in {"", route_hint}
            ]

        catalog_testids = {
            str(el.get("testid") or "").strip()
            for el in catalog_elements
            if str(el.get("testid") or "").strip()
        }
        if catalog_testids:
            strict_route_dom = [
                el for el in route_dom
                if str(el.get("testid") or "").strip() in catalog_testids
            ]
            if strict_route_dom:
                route_dom = strict_route_dom

        combined: list[dict] = []
        seen: set[tuple[str, str, str, str]] = set()
        for el in catalog_elements + route_dom:
            key = (
                str(el.get("route") or ""),
                str(el.get("testid") or ""),
                str(el.get("label") or ""),
                str(el.get("placeholder") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            combined.append(el)
        return combined or route_dom or catalog_elements or list(frontend_dom or [])

    def _catalog_elements_from_tc_context(self, tc_context: dict[str, Any] | None) -> list[dict]:
        selectors = ((tc_context or {}).get("selectors") or {}).get("by_route") or {}
        out: list[dict] = []

        def _append(route_path: str, record: dict[str, Any], *, group: str) -> None:
            extracted = record.get("extracted_from") or {}
            semantic_purpose = str(record.get("semantic_purpose") or "").strip()
            semantic_kind = str(record.get("semantic_kind") or "").strip()
            text = str(record.get("text") or "").strip()
            label = str(record.get("label") or "").strip()
            placeholder = str(record.get("placeholder") or "").strip()
            testid = str(record.get("testid") or "").strip()
            file_path = extracted.get("file")
            if group == "inputs":
                control_type = "form_input"
                actionable = True
            elif group == "buttons":
                role = str(record.get("form_role") or "").strip().lower()
                control_type = "submit" if role == "submit" else "button"
                actionable = True
            elif group == "dynamic":
                control_type = "feedback_dynamic"
                actionable = False
            else:
                control_type = f"feedback_{semantic_kind}" if semantic_kind else "label"
                actionable = False
            out.append({
                "tag": "div",
                "text": text or semantic_purpose,
                "placeholder": placeholder,
                "label": label or semantic_purpose,
                "testid": testid,
                "name": str(record.get("name") or "").strip(),
                "id": str(record.get("id") or "").strip(),
                "file": file_path,
                "page": "",
                "route": route_path,
                "actionable": actionable,
                "control_type": control_type,
                "dynamic_testid_pattern": str(record.get("dynamic_testid_pattern") or "").strip(),
            })

        for route_path, route_data in selectors.items():
            for group in ("inputs", "buttons", "outputs", "dynamic"):
                for record in list((route_data or {}).get(group) or []):
                    _append(route_path, record, group=group)
        return out

    def _ensure_navigate_step(
        self, steps: list[ActionStep], route_hint: str | None
    ) -> list[ActionStep]:
        """DOM action 시나리오에 route 힌트가 있으면 선행 navigate 를 보장한다."""
        if not route_hint or not steps:
            return steps
        first_action = str(steps[0].get("action") or "")
        if first_action == "navigate":
            return steps
        if first_action not in _SELECTOR_REQUIRED_ACTIONS:
            return steps

        navigate_step: ActionStep = {
            "step_no": 1,
            "action": "navigate",
            "selector": None,
            "selector_type": None,
            "value": route_hint,
            "expected": None,
            "api_endpoint": None,
        }
        renumbered: list[ActionStep] = [navigate_step]
        for index, step in enumerate(steps, start=2):
            updated = dict(step)
            updated["step_no"] = index
            renumbered.append(updated)
        return renumbered

    def _infer_step_intent(
        self, step: dict[str, Any], tc: dict[str, Any], scenario_text: str
    ) -> dict[str, str | None]:
        action = str(step.get("action") or "")
        selector = str(step.get("selector") or "").strip()
        value = step.get("value")
        expected = str(step.get("expected") or "").strip()
        tc_then = str(tc.get("then") or "").strip()
        explicit_target_name = str(step.get("target_name") or "").strip()
        explicit_target_kind = str(step.get("target_kind") or "").strip()

        if explicit_target_name or explicit_target_kind:
            return {
                "target_name": explicit_target_name or None,
                "target_kind": explicit_target_kind or None,
                "target_text": expected or selector or None,
            }

        if action in {"fill", "clear", "select", "press", "upload"}:
            return {
                "target_name": (
                    self._field_from_tc_value(tc, value)
                    or self._field_from_hint(selector)
                    or self._field_from_hint(scenario_text)
                ),
                "target_kind": "field",
                "target_text": selector if self._is_meaningful_selector_hint(selector, value) else None,
            }

        if action in {"click", "dblclick", "hover", "check", "uncheck"}:
            target_text = selector if self._is_meaningful_selector_hint(selector, value) else scenario_text
            target_name = (
                self._boolean_field_from_tc(tc, action, selector, scenario_text)
                or self._field_from_hint(selector)
            )
            target_kind = (
                "checkbox"
                if action in {"check", "uncheck"}
                else ("submit" if any(tok in scenario_text.lower() for tok in ("회원가입", "signup", "가입")) else "actionable")
            )
            return {
                "target_name": target_name,
                "target_kind": target_kind,
                "target_text": target_text or None,
            }

        if action in _ASSERT_ACTIONS:
            return {
                "target_name": None,
                "target_kind": "assertion",
                "target_text": expected or tc_then or selector or None,
            }

        return {"target_name": None, "target_kind": None, "target_text": selector or None}

    def _resolve_selector_from_intent(
        self,
        action: str,
        intent: dict[str, str | None],
        frontend_dom: list[dict],
        route_hint: str | None,
        scenario_text: str,
        min_score: float | None = None,
    ) -> dict[str, str] | None:
        candidates = self._frontend_candidates_for_action(action, frontend_dom, route_hint)
        target_kind = str(intent.get("target_kind") or "").strip().lower()
        if action in {"click", "dblclick", "hover"} and target_kind == "submit":
            submit_candidates = [
                el for el in candidates
                if str(el.get("control_type") or "").lower() in {"submit", "button"}
                or str(el.get("tag") or "").lower() == "button"
            ]
            if submit_candidates:
                candidates = submit_candidates
        if not candidates:
            return None

        if len(candidates) == 1 and action in {"click", "dblclick", "hover"}:
            selector, selector_type = self._preferred_selector_for_action(action, candidates[0])
            if selector and selector_type:
                return {"selector": selector, "selector_type": selector_type}

        best_el: dict[str, Any] | None = None
        best_score = 0.0
        for el in candidates:
            score = self._intent_match_score(action, intent, el, scenario_text, route_hint)
            if score > best_score:
                best_score = score
                best_el = el

        if best_el is None:
            return None

        threshold = min_score if min_score is not None else (0.8 if action in _ASSERT_ACTIONS else 0.7)
        if best_score < threshold:
            return None
        target_text = str(intent.get("target_text") or "").strip().lower()
        if action in _ASSERT_ACTIONS and target_text:
            if not self._assertion_text_matches_element(target_text, best_el):
                return None

        selector, selector_type = self._preferred_selector_for_action(action, best_el)
        if not selector or not selector_type:
            return None
        return {"selector": selector, "selector_type": selector_type}

    def _assertion_text_matches_element(self, target_text: str, element: dict[str, Any]) -> bool:
        expanded_target = self._expand_aliases(target_text)
        for key in ("text", "label", "placeholder", "testid", "id", "name"):
            candidate = str(element.get(key) or "").strip().lower()
            if not candidate:
                continue
            expanded_candidate = self._expand_aliases(candidate)
            score = difflib.SequenceMatcher(None, expanded_target, expanded_candidate).ratio()
            if target_text == candidate:
                return True
            if target_text in candidate or candidate in target_text:
                score += _FUZZY_MATCH_SUBSTRING_BONUS
            score += self._semantic_bonus(target_text, candidate)
            if score >= 0.55:
                return True
        return False

    def _repair_assert_steps(
        self,
        steps: list[ActionStep],
        tc: dict[str, Any],
        frontend_dom: list[dict],
        route_hint: str | None,
        scenario_text: str,
        tc_id: str | None,
    ) -> list[ActionStep]:
        if not frontend_dom:
            return steps

        repaired: list[ActionStep] = []
        for step in steps:
            action = str(step.get("action") or "")
            if action not in _ASSERT_ACTIONS:
                repaired.append(step)
                continue

            selector = str(step.get("selector") or "").strip()
            selector_type = str(step.get("selector_type") or "").strip()
            in_route_catalog = self._selector_exists_in_candidates(selector, selector_type, frontend_dom)
            if in_route_catalog:
                repaired.append(step)
                continue

            repair_text = (
                str(tc.get("then") or "").strip()
                or str(step.get("expected") or "").strip()
            )
            repair_intent = {
                "target_name": str(step.get("target_name") or "").strip() or None,
                "target_kind": "assertion",
                "target_text": repair_text or None,
            }
            resolved = None
            if repair_text:
                resolved = self._resolve_selector_from_intent(
                    action,
                    repair_intent,
                    frontend_dom,
                    route_hint,
                    scenario_text,
                    min_score=0.35,
                )
            updated = dict(step)
            if resolved is not None:
                updated["selector"] = resolved["selector"]
                updated["selector_type"] = resolved["selector_type"]
                self.logger.info(
                    "action_mapping_assert_selector_repaired",
                    tc_id=tc_id,
                    step_no=step.get("step_no"),
                    original_selector=selector or None,
                    repaired_selector=resolved["selector"],
                    route_hint=route_hint,
                )
            else:
                updated["selector"] = None
                updated["selector_type"] = None
                self.logger.warning(
                    "action_mapping_assert_selector_rejected",
                    tc_id=tc_id,
                    step_no=step.get("step_no"),
                    original_selector=selector or None,
                    route_hint=route_hint,
                )
            repaired.append(updated)
        return repaired

    def _selector_exists_in_candidates(
        self,
        selector: str,
        selector_type: str,
        frontend_dom: list[dict],
    ) -> bool:
        if not selector:
            return False
        normalized_type = selector_type.strip().lower()
        for el in frontend_dom:
            for key in ("testid", "text", "label", "placeholder", "id", "name", "dynamic_testid_pattern"):
                value = str(el.get(key) or "").strip()
                if not value:
                    continue
                if normalized_type == key and value == selector:
                    return True
                if value == selector:
                    return True
        return False

    def _frontend_candidates_for_action(
        self, action: str, frontend_dom: list[dict], route_hint: str | None
    ) -> list[dict]:
        result: list[dict] = []
        for el in frontend_dom:
            if route_hint and str(el.get("route") or "").strip() not in {"", route_hint}:
                continue
            actionable = bool(el.get("actionable"))
            control_type = str(el.get("control_type") or "").lower()
            tag = str(el.get("tag") or "").lower()
            if action in {"fill", "clear", "select", "press", "upload"}:
                if actionable and (control_type in {"form_input", "select", "textarea"} or tag in {"input", "textarea", "select"}):
                    result.append(el)
                continue
            if action in {"click", "dblclick", "hover", "check", "uncheck"}:
                if actionable and (control_type in {"button", "submit", "link", "checkbox", "radio"} or tag in {"button", "a", "input"}):
                    result.append(el)
                continue
            if action in _ASSERT_ACTIONS:
                if any(str(el.get(k) or "").strip() for k in ("text", "testid", "label", "placeholder", "dynamic_testid_pattern")):
                    result.append(el)
                continue
            result.append(el)
        return result

    def _intent_match_score(
        self,
        action: str,
        intent: dict[str, str | None],
        element: dict[str, Any],
        scenario_text: str,
        route_hint: str | None,
    ) -> float:
        score = 0.0
        target_name = str(intent.get("target_name") or "").strip().lower()
        target_kind = str(intent.get("target_kind") or "").strip().lower()
        target_text = str(intent.get("target_text") or "").strip().lower()
        control_type = str(element.get("control_type") or "").lower()
        tag = str(element.get("tag") or "").lower()

        if route_hint and str(element.get("route") or "").strip() == route_hint:
            score += 0.4

        if target_name:
            score += self._field_match_score(target_name, element)

        if target_text:
            for key in ("text", "label", "placeholder", "testid", "id", "name"):
                cand = str(element.get(key) or "").strip().lower()
                if not cand:
                    continue
                pair_score = difflib.SequenceMatcher(
                    None, self._expand_aliases(target_text), self._expand_aliases(cand)
                ).ratio()
                if target_text == cand:
                    pair_score = 1.0
                elif target_text in cand or cand in target_text:
                    pair_score += _FUZZY_MATCH_SUBSTRING_BONUS
                pair_score += self._semantic_bonus(target_text, cand)
                score += pair_score

        if action in {"fill", "clear", "select", "press", "upload"} and (
            control_type in {"form_input", "select", "textarea"} or tag in {"input", "textarea", "select"}
        ):
            score += 0.5
        if action in {"click", "dblclick", "hover", "check", "uncheck"} and (
            control_type in {"button", "submit", "link", "checkbox", "radio"} or tag in {"button", "a", "input"}
        ):
            score += 0.5
        if target_kind == "checkbox" and control_type in {"checkbox", "radio"}:
            score += 1.2
        if action in _ASSERT_ACTIONS:
            if not bool(element.get("actionable")):
                score += 0.2
            if control_type.startswith("feedback"):
                score += 1.2
            if target_kind == "assertion" and any(token in str(element.get("testid") or "").lower() for token in ("success", "error", "toast", "message", "status")):
                score += 0.7
            if target_kind == "assertion" and any(token in str(element.get("text") or "").lower() for token in ("완료", "성공", "이동")):
                score += 0.5

        if target_kind == "submit":
            if control_type in {"submit", "button"}:
                score += 1.2
            if control_type in {"form_input", "textarea", "select"}:
                score -= 0.6
            if any(token in str(element.get("text") or "").lower() for token in ("가입", "signup")):
                score += 0.7
            if any(token in str(element.get("testid") or "").lower() for token in ("signup", "submit")):
                score += 0.7

        if any(term in scenario_text.lower() for term in ("회원가입", "가입", "signup")) and "signup" in str(element.get("page") or "").lower():
            score += 0.4
        return score

    def _field_match_score(self, field_name: str, element: dict[str, Any]) -> float:
        aliases = _FIELD_ALIASES.get(field_name, (field_name,))
        score = 0.0
        for key in ("testid", "id", "label", "placeholder", "text", "name"):
            cand = str(element.get(key) or "").strip().lower()
            if not cand:
                continue
            if any(alias == cand for alias in aliases):
                score += 1.2
            elif any(alias in cand for alias in aliases):
                score += 0.9
        return score

    def _field_from_tc_value(self, tc: dict[str, Any], value: Any) -> str | None:
        if value is None:
            return None
        target = str(value).strip()
        if not target:
            return None
        for item in tc.get("values") or []:
            if str(item.get("value") or "").strip() == target:
                return self._canonical_field_name(str(item.get("field") or ""))
        return None

    def _boolean_field_from_tc(
        self,
        tc: dict[str, Any],
        action: str,
        selector_hint: str,
        scenario_text: str,
    ) -> str | None:
        desired = True if action == "check" else False if action == "uncheck" else None
        hinted_field = self._field_from_hint(selector_hint) or self._field_from_hint(scenario_text)
        matching_bool_fields: list[str] = []
        fallback_bool_fields: list[str] = []

        for item in tc.get("values") or []:
            field_name = self._canonical_field_name(str(item.get("field") or ""))
            bool_value = self._coerce_bool(item.get("value"))
            if not field_name or bool_value is None:
                continue
            fallback_bool_fields.append(field_name)
            if desired is None or bool_value == desired:
                matching_bool_fields.append(field_name)

        if hinted_field and hinted_field in matching_bool_fields:
            return hinted_field
        if hinted_field and hinted_field in fallback_bool_fields:
            return hinted_field
        if len(matching_bool_fields) == 1:
            return matching_bool_fields[0]
        if hinted_field:
            return hinted_field
        return None

    def _coerce_bool(self, value: Any) -> bool | None:
        if isinstance(value, bool):
            return value
        lowered = str(value).strip().lower()
        if lowered in {"true", "1", "yes", "y", "checked"}:
            return True
        if lowered in {"false", "0", "no", "n", "unchecked"}:
            return False
        return None

    def _should_skip_toggle_step(
        self,
        action: str,
        intent: dict[str, str | None],
        tc: dict[str, Any],
    ) -> bool:
        if action not in {"check", "uncheck"}:
            return False
        target_name = str(intent.get("target_name") or "").strip().lower()
        if not target_name:
            return False

        desired = True if action == "check" else False
        for item in tc.get("values") or []:
            field_name = self._canonical_field_name(str(item.get("field") or ""))
            if field_name != target_name:
                continue
            actual = self._coerce_bool(item.get("value"))
            if actual is not None and actual != desired:
                return True

        if target_name == "guardian_consent":
            is_minor = self._is_minor_from_tc(tc)
            if is_minor is False:
                return True
        return False

    def _is_minor_from_tc(self, tc: dict[str, Any]) -> bool | None:
        birth_date_value: str | None = None
        for item in tc.get("values") or []:
            if self._canonical_field_name(str(item.get("field") or "")) == "birth_date":
                birth_date_value = str(item.get("value") or "").strip()
                break
        if not birth_date_value or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", birth_date_value):
            return None
        try:
            birth = date.fromisoformat(birth_date_value)
        except ValueError:
            return None
        today = date.today()
        age = today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
        return age < 19

    def _field_from_hint(self, hint: str) -> str | None:
        lowered = hint.strip().lower()
        if not lowered:
            return None
        for canonical, aliases in _FIELD_ALIASES.items():
            if any(alias.lower() in lowered for alias in aliases):
                return canonical
        return None

    def _canonical_field_name(self, field: str) -> str | None:
        lowered = field.strip().lower()
        if lowered in _FIELD_ALIASES:
            return lowered
        for canonical, aliases in _FIELD_ALIASES.items():
            if lowered == canonical or any(lowered == alias.lower() for alias in aliases):
                return canonical
        return None

    def _is_meaningful_selector_hint(self, selector: str, value: Any) -> bool:
        cleaned = selector.strip()
        if not cleaned:
            return False
        lowered = cleaned.lower()
        if lowered in _ACTION_TOKEN_SET:
            return False
        if value is not None and cleaned == str(value):
            return False
        if self._looks_like_literal_value(cleaned):
            return False
        return True

    def _looks_like_literal_value(self, text: str) -> bool:
        lowered = text.strip().lower()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", lowered):
            return True
        if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", lowered):
            return True
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9!@#$%^&*()_+=-]{7,}", text):
            return True
        return False

    def _route_hint_from_elements(self, elements: list[dict]) -> str | None:
        routes = [str(el.get("route") or "").strip() for el in elements if str(el.get("route") or "").strip()]
        if not routes:
            return None
        return max(set(routes), key=routes.count)

    def _route_hint_from_tc(self, tc: dict[str, Any]) -> str | None:
        text = " ".join(str(tc.get(key) or "") for key in ("name", "given", "when", "then")).lower()
        if any(term in text for term in ("회원가입", "가입", "signup")):
            return "/signup"
        if "로그인" in text or "login" in text:
            return "/login"
        return None

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
        """action 성격에 맞는 가장 안정적인 selector 필드를 선택한다.

        우선순위: 고정 testid → 사용자 가시 text → route context → dynamic pattern
        dynamic_testid_pattern 은 런타임 값 미확정이므로 최후 수단으로만 사용.
        """
        if action in {"fill", "clear", "select", "press", "upload"}:
            priority = ("testid", "label", "placeholder", "text")
        else:
            priority = ("testid", "text", "label", "placeholder")

        for key in priority:
            value = (element.get(key) or "").strip()
            if value:
                return value, key

        # fallback: dynamic_testid_pattern (예: plan-select-${plan.id}) — 패턴 힌트용
        dyn = (element.get("dynamic_testid_pattern") or "").strip()
        if dyn:
            return dyn, "testid"

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
