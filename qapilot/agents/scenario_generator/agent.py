"""시나리오 생성 Agent.

코드베이스 분석 결과와 Git diff를 기반으로
테스트 시나리오를 자동 생성한다.

담당: B
Created: 2026-05-07
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from qapilot.agents.scenario_generator.coverage_checker import (
    compute_coverage,
    format_coverage_report,
)
from qapilot.agents.scenario_generator.parser import (
    detect_prd_code_mismatch,
    format_mismatches,
    parse_response,
)
from qapilot.agents.scenario_generator.repository import save_scenarios
from qapilot.agents.base_agent import BaseAgent
from qapilot.shared.schemas import ExecuteResult

# 라우터별 도메인 검색 키워드 — Qdrant 검색 쿼리 및 requirements 필터링에 사용
_ROUTER_KEYWORDS: dict[str, list[str]] = {
    "auth":      ["인증", "로그인", "회원가입", "토큰", "JWT", "세션", "비밀번호"],
    "plans":     ["요금제", "플랜", "구독", "가격", "상품", "서비스"],
    "notices":   ["공지", "알림", "안내", "배너", "이벤트"],
    "orders":    ["주문", "회선", "신청", "개통", "가입", "신규"],
    "billing":   ["결제", "청구", "납부", "카드", "금액", "청구서"],
    "contracts": ["계약", "약정", "해지", "위약금", "만료"],
    "tier":      ["등급", "티어", "혜택", "레벨", "VIP", "포인트"],
    "family":    ["패밀리", "가족", "공유", "구성원", "결합"],
    "usage":     ["사용량", "데이터", "통화", "잔여", "소진"],
}

# 라우터 실행 우선순위 — 인증 → 기본 조회 → 트랜잭션 순
_ROUTER_PRIORITY: list[str] = [
    "auth",
    "plans",
    "notices",
    "orders",
    "billing",
    "contracts",
    "tier",
    "family",
    "usage",
]

# 라우터 basename → API prefix 매핑 (main.py include_router prefix 기준)
# contracts 서비스는 파일명이 routers.py이므로 경로 내 "contracts" 포함 여부로 판단한다.
_ROUTER_PREFIX: dict[str, str] = {
    "auth":      "/api/auth",
    "plans":     "/api/plans",
    "orders":    "/api/orders",
    "billing":   "/api/billing",
    "notices":   "/api/notices",
    "tier":      "/api/tier",
    "usage":     "/api/usage",
    "family":    "/api/family",
    "contracts": "/api/contracts",
}

# 라우터별 선행 의존 관계 (라우터 basename 기준)
_ROUTER_DEPENDENCIES: dict[str, list[str]] = {
    "auth":      [],
    "plans":     [],
    "notices":   [],
    "orders":    ["auth"],
    "billing":   ["auth", "orders"],
    "contracts": ["auth", "orders"],
    "tier":      ["auth"],
    "family":    ["auth"],
    "usage":     ["auth", "orders"],
}

# TC 텍스트 기반 HTTP method 추론 우선순위 테이블.
# 순서대로 평가하며 매칭되면 해당 method 목록을 반환한다.
_ACTION_METHOD_MAP: list[tuple[list[str], list[str]]] = [
    (["해지", "취소", "삭제", "탈퇴", "제거"], ["DELETE", "PATCH"]),
    (["변경", "수정", "업데이트", "갱신"], ["PATCH", "PUT"]),
    (["생성", "등록", "가입", "신청", "추가", "발급", "발행", "로그인", "login", "signup", "toggle"], ["POST"]),
]

# GET이 할당됐을 때 명백히 쓰기 액션임을 나타내는 키워드 — 재추론 대상
# "주문"은 명사로도 쓰여 "주문 상세 조회" 같은 읽기 TC에서 오탐이 발생하므로 제외
_WRITE_ACTION_KEYWORDS: frozenset[str] = frozenset([
    "가입", "등록", "생성", "신청", "로그인", "login", "signup",
    "발급", "발행", "추가", "해지", "취소", "삭제", "탈퇴", "toggle",
])

# 읽기 전용 액션 키워드 — 비GET API가 할당된 경우 재추론 대상
_READ_ACTION_KEYWORDS: frozenset[str] = frozenset(["조회", "열람"])
_NON_INTEGRATION_REQUIREMENT_KEYWORDS: frozenset[str] = frozenset([
    "p95", "p99", "응답 시간", "응답시간", "처리량", "부하", "성능",
    "가용성", "99.5", "모니터링", "CPU", "디스크",
])
_API_TESTABLE_OPERATIONAL_KEYWORDS: frozenset[str] = frozenset([
    "X-Trace-Id", "trace", "추적성", "표준 응답", "모든 응답",
])

# TC 텍스트 키워드 → 핸들러 이름 힌트 매핑.
# 같은 HTTP method를 공유하는 엔드포인트가 여러 개일 때 더 정확한 엔드포인트를 선택하는 데 사용한다.
_HANDLER_HINTS: list[tuple[list[str], list[str]]] = [
    (["로그인", "login", "인증"],        ["login", "signin", "sign_in", "authenticate"]),
    (["가입", "signup", "등록"],         ["signup", "register", "sign_up", "create_user"]),
    (["해지", "terminate", "탈퇴"],      ["terminate", "withdraw", "leave", "deactivate"]),
    (["취소", "cancel"],                 ["cancel", "abort"]),
    (["삭제", "delete"],                 ["delete", "remove", "destroy"]),
    (["초대", "invite", "join"],         ["invite", "join", "add_member"]),
    (["갱신", "refresh", "renew"],       ["refresh", "renew", "reissue"]),
]


class ScenarioGeneratorAgent(BaseAgent):
    """시나리오 생성 Agent.

    역할: 코드 변경 기반 테스트 시나리오 자동 생성
    입력: CodebaseContext, GitDiff, DomainRules, RequirementItems
    출력: List[TestScenario], confidence
    호출 Tool: 코드 인덱스 Tool, 도메인 지식 Tool
    HITL: O (파이프라인 hitl_review 노드 — 미구현, 추후 연동)
    """

    allowed_tools = ["codebase_scanner", "domain_knowledge"]
    use_deep_model = True

    async def _execute(
        self,
        context: dict[str, Any],
        params: dict[str, Any],
        last_error: str | None = None,
    ) -> ExecuteResult:
        """요구사항 기반으로 TS/TC/TV 시나리오를 생성한다.

        흐름: 요구사항 목록 → REQ↔API 매핑(LLM 1회) → REQ별 LLM 호출 → TS 생성
        요구사항이 없으면 라우터 기반 fallback으로 전환한다.

        Args:
            context: scan_result, domain_rules, requirements.
            params: trigger, affected_only.
            last_error: 이전 시도 에러 (자가 수정 힌트).

        Returns:
            ExecuteResult: scenarios(list[TestScenario]), prd_code_mismatches 포함.
        """
        scan_result: dict = context.get("scan_result") or {}
        domain_rules: list = context.get("domain_rules", [])
        requirements: list = context.get("requirements", [])
        trigger: str = params.get("trigger", "code_change")
        affected_only: bool = bool(params.get("affected_only", False))
        # codebase-index read 경로 — pipeline 이 state.qapilot_dir 을 주입함. 없으면 CWD fallback.
        self._qapilot_dir_override: str | None = context.get("qapilot_dir")

        if not scan_result:
            scan_result = await self._fetch_scan_result()

        affected_files: list[str] = []
        if affected_only:
            affected_files = (scan_result.get("git_diff") or {}).get("changed_files", [])

        mismatches = detect_prd_code_mismatch(requirements, scan_result)
        mismatch_text = format_mismatches(mismatches)

        if requirements:
            return await self._run_domain_based(
                scan_result, domain_rules, requirements, trigger,
                affected_files, mismatch_text, mismatches, last_error,
            )

        return await self._run_router_based(
            scan_result, domain_rules, trigger,
            affected_files, mismatch_text, mismatches, last_error,
        )

    async def _run_router_based(
        self,
        scan_result: dict,
        domain_rules: list,
        trigger: str,
        affected_files: list[str],
        mismatch_text: str,
        mismatches: list,
        last_error: str | None,
    ) -> ExecuteResult:
        """요구사항 없을 때의 라우터 기반 fallback."""
        from qapilot.tools.domain_knowledge import DomainKnowledgeTool

        router_map = self._sort_router_map(self._build_router_map(affected_files))
        all_scenarios: list = []
        confidence_sum = 0.0

        for router_file, endpoints in router_map.items():
            basename = router_file.split("/")[-1].replace(".py", "").replace(".ts", "").replace(".js", "")
            keywords = _ROUTER_KEYWORDS.get(basename, [])
            query = " ".join(keywords) if keywords else basename
            router_domain_rules = await self._fetch_domain_rules(query, top_k=5)
            router_domain_rules_text = DomainKnowledgeTool.format_rules_for_prompt(router_domain_rules) or "없음"

            user_prompt = self.with_correction_hint(
                self.prompts.render(
                    domain_rules=router_domain_rules_text,
                    requirements="없음",
                    scan_summary=self._format_scan_summary_for_router(router_file, endpoints),
                    code_index=self._format_code_index_for_router(router_file, scan_result),
                    affected_files=router_file,
                    trigger=trigger,
                    mismatch_note=mismatch_text,
                ),
                last_error,
            )

            response = await self.llm.chat(
                system_prompt=self.prompts.system(),
                user_prompt=user_prompt,
            )
            ts_scenarios, ts_confidence = parse_response(
                response.content, trigger, [router_file], domain_rules
            )
            self._sanitize_api_fields(ts_scenarios)
            self._correct_api_method_mismatches(ts_scenarios)
            all_scenarios.extend(ts_scenarios)
            confidence_sum += ts_confidence

        confidence = round(confidence_sum / len(router_map), 3) if router_map else 0.5
        all_scenarios = self._deduplicate_scenarios(all_scenarios)
        all_scenarios = self._renumber_and_set_depends_on(all_scenarios)
        # 디스크 영속화는 pipeline._save_scenarios 노드에서 수행 (위 메서드와 동일 사유).

        return ExecuteResult(
            result={"scenarios": all_scenarios, "prd_code_mismatches": mismatches},
            confidence=confidence,
        )

    async def _run_domain_based(
        self,
        scan_result: dict,
        domain_rules: list,
        requirements: list,
        trigger: str,
        affected_files: list[str],
        mismatch_text: str,
        mismatches: list,
        last_error: str | None,
    ) -> ExecuteResult:
        """요구사항 기반 도메인별 시나리오 생성 — 1 domain_area = 1 TS.

        action_type == "create" (또는 target_ts_id null 폴백) → 새 TS 생성.
        action_type == "update" + target_ts_id 있음 → target_level에 따라 부분 수정.
        """
        from qapilot.tools.domain_knowledge import DomainKnowledgeTool

        all_scenarios: list = []
        all_updated: list = []   # update 결과 (ts_id 고정, 재번호 부여 없이 바로 저장)
        confidence_sum = 0.0
        all_endpoints: list[dict] = self._read_index_json("endpoints.json")  # type: ignore[assignment]
        target_requirements, skipped_requirements = self._split_scenario_requirements(requirements)
        if skipped_requirements:
            self.logger.info(
                "requirements_skipped_for_scenario_generation",
                count=len(skipped_requirements),
                req_ids=[req.get("req_id") for req in skipped_requirements],
            )

        req_endpoint_map = self._map_requirements_to_endpoints(target_requirements, all_endpoints)

        for req in target_requirements:
            domain_area = req.get("domain_area") or "기타"
            req_id = req.get("req_id", "")
            req_endpoints = req_endpoint_map.get(req_id) or self._select_endpoints_for_requirement(req, all_endpoints)
            req_endpoint_map[req_id] = req_endpoints
            router_files = list(dict.fromkeys(
                ep.get("file", "")
                for ep in req_endpoints
                if ep.get("file")
            ))
            if not router_files:
                router_files = self._find_router_files_for_domain(domain_area, [req])
            prompt_files = router_files or affected_files
            area_domain_rules = domain_rules or await self._fetch_domain_rules(domain_area, top_k=5)
            area_domain_rules_text = DomainKnowledgeTool.format_rules_for_prompt(area_domain_rules) or "없음"

            user_prompt = self.with_correction_hint(
                self.prompts.render(
                    domain_rules=area_domain_rules_text,
                    requirements=self._format_requirements([req]),
                    scan_summary=self._format_scan_summary_for_requirement(req_id, req, req_endpoints),
                    code_index=self._format_code_index_for_requirement(router_files, req_endpoints, scan_result),
                    affected_files=", ".join(prompt_files) if prompt_files else domain_area,
                    trigger=trigger,
                    mismatch_note=mismatch_text,
                ),
                last_error,
            )

            response = await self.llm.chat(
                system_prompt=self.prompts.system(),
                user_prompt=user_prompt,
            )

            ts_scenarios, ts_confidence = parse_response(
                response.content, trigger, prompt_files, domain_rules
            )
            self._sanitize_api_fields(ts_scenarios, self._endpoint_paths(req_endpoints))
            self._correct_api_method_mismatches(ts_scenarios)
            self._pin_req_id(ts_scenarios, req_id)
            self._fill_api_for_domain(ts_scenarios, req, router_files, req_endpoints)
            all_scenarios.extend(ts_scenarios)
            confidence_sum += ts_confidence

        # update 결과는 ts_id 고정 — 재번호 부여 없이 바로 덮어쓴다.
        from qapilot.agents.scenario_generator.repository import save_scenario
        for ts in all_updated:
            save_scenario(ts)

        confidence = round(confidence_sum / max(len(requirements), 1), 3)

        if not all_scenarios:
            # create 대상이 없고 update만 있는 경우
            self.logger.info(
                "requirement_scenario_generated",
                req_id=req_id,
                domain=domain_area,
                mapped_api_count=len(req_endpoints),
                ts_count=len(ts_scenarios),
                tc_count=sum(len(s["test_cases"]) for s in ts_scenarios),
            )

        # ── 커버리지 gap-fill ─────────────────────────────────────────────────
        # 생성 후 미커버 요구사항이 있으면 해당 req만 재생성한다 (최대 1회).
        _MAX_FILL_RETRIES = 2
        call_count = len(target_requirements)

        for _attempt in range(_MAX_FILL_RETRIES):
            coverage = compute_coverage(all_scenarios, target_requirements)
            self.logger.info(
                "coverage_check",
                attempt=_attempt,
                rate=coverage["rate"],
                uncovered_count=len(coverage["uncovered"]),
            )
            if not coverage["uncovered"]:
                break

            self.logger.warning(
                "coverage_gap_detected",
                uncovered_req_ids=[r["req_id"] for r in coverage["uncovered"]],
                attempt=_attempt + 1,
            )

            for req in coverage["uncovered"]:
                domain_area = req.get("domain_area") or "기타"
                req_id = req.get("req_id", "")
                req_endpoints = req_endpoint_map.get(req_id) or self._select_endpoints_for_requirement(req, all_endpoints)
                req_endpoint_map[req_id] = req_endpoints
                router_files = list(dict.fromkeys(
                    ep.get("file", "")
                    for ep in req_endpoints
                    if ep.get("file")
                ))
                if not router_files:
                    router_files = self._find_router_files_for_domain(domain_area, [req])
                prompt_files = router_files or affected_files
                fill_rules = domain_rules or await self._fetch_domain_rules(domain_area, top_k=5)
                fill_rules_text = DomainKnowledgeTool.format_rules_for_prompt(fill_rules) or "없음"

                fill_prompt = self.prompts.render(
                    domain_rules=fill_rules_text,
                    requirements=self._format_requirements([req]),
                    scan_summary=self._format_scan_summary_for_requirement(req_id, req, req_endpoints),
                    code_index=self._format_code_index_for_requirement(router_files, req_endpoints, scan_result),
                    affected_files=", ".join(prompt_files) if prompt_files else domain_area,
                    trigger=trigger,
                    mismatch_note=mismatch_text,
                )
                fill_response = await self.llm.chat(
                    system_prompt=self.prompts.system(),
                    user_prompt=fill_prompt,
                )
                fill_scenarios, fill_confidence = parse_response(
                    fill_response.content, trigger, prompt_files, domain_rules
                )
                self._sanitize_api_fields(fill_scenarios, self._endpoint_paths(req_endpoints))
                self._correct_api_method_mismatches(fill_scenarios)
                self._pin_req_id(fill_scenarios, req_id)
                self._fill_api_for_domain(fill_scenarios, req, router_files, req_endpoints)
                all_scenarios.extend(fill_scenarios)
                confidence_sum += fill_confidence
                call_count += 1
                self.logger.info(
                    "gap_filled",
                    req_id=req["req_id"],
                    ts_added=len(fill_scenarios),
                )

        # 최종 커버리지 로그
        final_coverage = compute_coverage(all_scenarios, target_requirements)
        self.logger.info(
            "final_coverage",
            rate=final_coverage["rate"],
            report=format_coverage_report(final_coverage),
        )

        # 요구사항 기반 모드에서는 전체 API 커버리지를 목표로 추가 TS를 만들지 않는다.
        # 없는 요구사항에 엔드포인트를 억지로 붙이는 문제를 막기 위해,
        # 마지막 api 보정도 각 TS의 요구사항에 매핑된 API 후보 안에서만 수행한다.
        self._fill_api_nulls_global(all_scenarios, req_endpoint_map)
        self._sanitize_scenarios_against_requirements(all_scenarios, target_requirements, req_endpoint_map)

        confidence = round(confidence_sum / call_count, 3) if call_count else 0.5
        all_scenarios = self._deduplicate_scenarios(all_scenarios)
        all_scenarios = self._renumber_and_set_depends_on(all_scenarios)
        save_scenarios(all_scenarios)

        self.logger.info(
            "scenarios_generated",
            count=len(all_scenarios),
            tc_count=sum(len(s["test_cases"]) for s in all_scenarios),
            mismatch_count=len(mismatches),
            confidence=confidence,
            coverage_rate=final_coverage["rate"],
        )

        return ExecuteResult(
            result={
                "scenarios": all_scenarios,
                "prd_code_mismatches": mismatches,
                "coverage": {
                    "rate": final_coverage["rate"],
                    "uncovered": [r["req_id"] for r in final_coverage["uncovered"]],
                    "skipped": [r["req_id"] for r in skipped_requirements],
                },
                "skipped_requirements": skipped_requirements,
            },
            confidence=confidence,
        )

    def _sanitize_api_fields(self, scenarios: list, allowed_apis: set[str] | None = None) -> None:
        """LLM이 생성한 api 필드 중 실제 존재하지 않는 경로를 null로 초기화한다.

        allowed_apis가 주어지면 존재 여부뿐 아니라 요구사항에 매핑된 후보 API인지도
        검증한다. 요구사항 기반 생성에서는 이 후보 밖 API를 연결하지 않는다.

        phantom path(예: DELETE /api/contracts/{order_id})를 방지하며,
        null로 초기화된 TCs는 이후 _fill_api_for_domain 또는 stub 주입이 처리한다.
        """
        valid_apis: set[str] = {
            self._get_full_ep_path(ep)
            for ep in self._read_index_json("endpoints.json")  # type: ignore[arg-type]
        }
        api_scope = allowed_apis if allowed_apis is not None else valid_apis
        for s in scenarios:
            for tc in s.get("test_cases", []):
                api = tc.get("api")
                if api and api not in (None, "null") and (api not in valid_apis or api not in api_scope):
                    tc["api"] = None

    def _fill_api_nulls_global(
        self,
        all_scenarios: list,
        req_endpoint_map: dict[str, list[dict]] | None = None,
    ) -> None:
        """저장 직전 전체 시나리오를 순회해 api=null TC를 TC 단위로 채운다.

        req_endpoint_map이 있으면 TS의 requirements에 연결된 후보 API 안에서만
        채운다. 후보가 없는 요구사항 기반 TS는 api=null을 유지한다.
        """
        all_endpoints: list[dict] = self._read_index_json("endpoints.json")  # type: ignore[assignment]
        valid_apis: set[str] = {self._get_full_ep_path(ep) for ep in all_endpoints}
        _HEALTH_HANDLERS = {"health", "healthcheck", "ping"}

        for s in all_scenarios:
            null_tcs = [
                tc for tc in s.get("test_cases", [])
                if not tc.get("api") or tc.get("api") in (None, "null")
            ]
            if not null_tcs:
                continue

            sibling_apis = [
                tc.get("api") for tc in s.get("test_cases", [])
                if tc.get("api") and tc.get("api") not in (None, "null")
            ]
            if req_endpoint_map is not None:
                req_ids = [
                    r for r in s.get("requirements", [])
                    if r and r in req_endpoint_map
                ]
                domain_eps = [
                    ep for req_id in req_ids
                    for ep in req_endpoint_map.get(req_id, [])
                    if ep.get("handler", "") not in _HEALTH_HANDLERS
                    and not ep.get("path", "").endswith("/health")
                ]
                valid_apis_for_ts = self._endpoint_paths(domain_eps)
                sibling_apis = [api for api in sibling_apis if api in valid_apis_for_ts]
                if not domain_eps:
                    continue
            else:
                router_files: list[str] = s.get("affected_files") or []
                domain_eps = [
                    ep for ep in all_endpoints
                    if ep.get("file", "") in router_files
                    and ep.get("handler", "") not in _HEALTH_HANDLERS
                    and not ep.get("path", "").endswith("/health")
                ]
                if not domain_eps:
                    ts_text = s.get("name", "") + " " + " ".join(tc.get("name", "") for tc in null_tcs)
                    for basename, kws in _ROUTER_KEYWORDS.items():
                        if any(k in ts_text for k in kws):
                            domain_eps = [
                                ep for ep in all_endpoints
                                if ep.get("handler", "") not in _HEALTH_HANDLERS
                                and not ep.get("path", "").endswith("/health")
                                and (
                                    ep.get("file", "").split("/")[-1].replace(".py", "").replace(".ts", "").replace(".js", "") == basename
                                    or ("contracts" in ep.get("file", "") and basename == "contracts")
                                )
                            ]
                            if domain_eps:
                                break

            for tc in null_tcs:
                valid_scope = self._endpoint_paths(domain_eps) if req_endpoint_map is not None else valid_apis
                api = self._infer_api_for_tc(tc, valid_scope, sibling_apis, domain_eps)
                if api:
                    tc["api"] = api

        null_remaining = sum(
            1 for s in all_scenarios
            for tc in s.get("test_cases", [])
            if not tc.get("api") or tc.get("api") in (None, "null")
        )
        self.logger.info("api_null_fill_global_done", null_remaining=null_remaining)

    def _infer_api_for_tc(
        self,
        tc: dict,
        valid_apis: set[str],
        sibling_apis: list[str],
        domain_eps: list[dict],
    ) -> str | None:
        """TC 하나에 대해 api 값을 추론한다.

        우선순위:
        1. when/name 텍스트에서 METHOD + 경로 직접 추출
        2. sibling TC 중 같은 HTTP method에서 최빈 api
        3. domain_eps에서 TC 텍스트 기반 method 추론 후 선택
        """
        # Strategy 1: when → name 순으로 텍스트에서 직접 추출
        for field in ("when", "name"):
            extracted = self._extract_api_from_text(tc.get(field, ""), valid_apis)
            if extracted:
                return extracted

        # Strategy 2: sibling 중 TC method 힌트와 일치하는 최빈 api
        if sibling_apis:
            tc_text = (tc.get("when", "") + " " + tc.get("name", "")).upper()
            for method in ("DELETE", "PATCH", "PUT", "POST", "GET"):
                if method in tc_text:
                    matched = [a for a in sibling_apis if a.startswith(method + " ")]
                    if matched:
                        return Counter(matched).most_common(1)[0][0]
            return Counter(sibling_apis).most_common(1)[0][0]

        # Strategy 3: domain_eps에서 _ACTION_METHOD_MAP 기반 method 추론 후 선택.
        # 같은 method를 공유하는 엔드포인트가 여러 개이면 _HANDLER_HINTS로 핸들러 이름 매칭.
        if domain_eps:
            tc_text = tc.get("when", "") + " " + tc.get("name", "")
            tc_text_lower = tc_text.lower()
            preferred = ["GET"]
            for keywords, methods in _ACTION_METHOD_MAP:
                if any(k in tc_text for k in keywords):
                    preferred = methods
                    break
            candidate_eps = [ep for ep in domain_eps if ep.get("method") in preferred]
            if len(candidate_eps) > 1:
                for hint_kws, handler_kws in _HANDLER_HINTS:
                    if any(k in tc_text_lower for k in hint_kws):
                        matched = [
                            ep for ep in candidate_eps
                            if any(h in ep.get("handler", "").lower() for h in handler_kws)
                        ]
                        if matched:
                            return self._get_full_ep_path(matched[0])
            ep = next(iter(candidate_eps), domain_eps[0])
            return self._get_full_ep_path(ep)

        return None

    def _extract_api_from_text(self, text: str, valid_apis: set[str]) -> str | None:
        """텍스트에서 'METHOD /api/path' 패턴을 추출해 valid_apis와 대조한다.

        경로 파라미터 템플릿({param})도 매칭한다.
        """
        m = re.search(r'\b(GET|POST|PUT|PATCH|DELETE)\s+(/api/[^\s,\)\]]+)', text)
        if not m:
            return None
        candidate = f"{m.group(1)} {m.group(2).rstrip('/.,])')}"
        if candidate in valid_apis:
            return candidate
        # 경로 파라미터 템플릿 매칭: /api/orders/{order_id} 형태
        cand_method, cand_path = candidate.split(" ", 1)
        for valid_api in valid_apis:
            v_method, v_path = valid_api.split(" ", 1)
            if v_method != cand_method:
                continue
            pattern = re.sub(r'\{[^}]+\}', r'[^/]+', v_path)
            if re.fullmatch(pattern, cand_path):
                return valid_api
        return None

    def _pin_req_id(self, scenarios: list, req_id: str) -> None:
        """TS 내 모든 TC의 req_id를 해당 요구사항 ID로 고정한다.

        REQ 기반으로 LLM을 호출하므로 해당 TS의 모든 TC는 그 REQ와 연관된다.
        LLM이 존재하지 않는 req_id를 만들어도 저장되지 않도록 여기서 강제한다.
        """
        for s in scenarios:
            for tc in s.get("test_cases", []):
                tc["req_id"] = req_id or None
            s["requirements"] = [req_id] if req_id else []

    def _fill_api_for_domain(
        self,
        scenarios: list,
        req: dict,
        router_files: list[str],
        allowed_endpoints: list[dict] | None = None,
    ) -> None:
        """api=null TC에 TC 내용 기반 per-TC 추론으로 엔드포인트를 채운다.

        단일 primary API를 일괄 할당하지 않고 _infer_api_for_tc()로 TC별로 적합한
        엔드포인트를 선택한다. 이렇게 하면 로그인 TC → POST /login,
        조회 TC → GET /me처럼 TC 의도에 맞는 method가 선택된다.
        """
        all_endpoints: list[dict] = self._read_index_json("endpoints.json")  # type: ignore[assignment]
        domain_eps = (
            allowed_endpoints
            if allowed_endpoints is not None
            else [ep for ep in all_endpoints if ep.get("file", "") in router_files]
        )
        valid_apis: set[str] = self._endpoint_paths(domain_eps)
        if not domain_eps:
            return

        for s in scenarios:
            sibling_apis: list[str] = [
                tc.get("api") for tc in s.get("test_cases", [])
                if tc.get("api") and tc.get("api") not in (None, "null") and tc.get("api") in valid_apis
            ]
            for tc in s.get("test_cases", []):
                if not tc.get("api") or tc.get("api") in (None, "null"):
                    api = self._infer_api_for_tc(tc, valid_apis, sibling_apis, domain_eps)
                    if api:
                        tc["api"] = api
                        sibling_apis.append(api)

    @staticmethod
    def _kw_in(keyword: str, text_lower: str) -> bool:
        """키워드가 텍스트에서 독립 단어(앞에 공백 또는 시작)로 나타나는지 확인.

        '비로그인'에 '로그인'이 포함되는 오탐을 방지하기 위해 공백 경계를 사용한다.
        """
        return f" {keyword}" in f" {text_lower}"

    def _correct_api_method_mismatches(self, scenarios: list) -> None:
        """TC 내용과 할당된 api의 HTTP method가 불일치하면 api를 null로 초기화한다.

        세 가지 케이스를 처리한다:
        1. 쓰기/삭제 액션 TC에 GET이 할당된 경우
        2. 읽기 전용 액션(조회) TC에 비GET이 할당된 경우
        3. _ACTION_METHOD_MAP 기반으로 추론한 예상 method와 할당 method가 다른 경우

        null로 초기화된 TC는 이후 _fill_api_for_domain 또는 _fill_api_nulls_global에서
        TC 내용 기반으로 재추론한다.
        """
        for s in scenarios:
            for tc in s.get("test_cases", []):
                api = tc.get("api")
                if not api or api in (None, "null"):
                    continue
                method = api.split()[0]
                tc_lower = (tc.get("name", "") + " " + tc.get("when", "")).lower()

                has_write = any(self._kw_in(k, tc_lower) for k in _WRITE_ACTION_KEYWORDS)
                has_read = any(k in tc_lower for k in _READ_ACTION_KEYWORDS)

                # 1. 쓰기/삭제 액션 TC에 GET 할당
                if method == "GET" and has_write:
                    tc["api"] = None
                    continue

                # 2. 읽기 전용 액션(조회) TC에 비GET 할당 — 쓰기 키워드가 없어야 함
                if method != "GET" and has_read and not has_write:
                    tc["api"] = None
                    continue

                # 3. _ACTION_METHOD_MAP 기반 예상 method 불일치
                for keywords, expected_methods in _ACTION_METHOD_MAP:
                    if any(self._kw_in(k, tc_lower) for k in keywords):
                        if method not in expected_methods:
                            tc["api"] = None
                        break

    def _get_full_ep_path(self, ep: dict) -> str:
        """엔드포인트 dict에서 'METHOD /api/prefix/path' 형태의 전체 경로를 반환한다."""
        file_path = ep.get("file", "")
        rel_path = ep.get("path", "")
        method = ep.get("method", "?")

        if "contracts" in file_path:
            prefix = _ROUTER_PREFIX.get("contracts", "")
        else:
            basename = file_path.split("/")[-1].replace(".py", "").replace(".ts", "").replace(".js", "")
            prefix = _ROUTER_PREFIX.get(basename, "")

        full = prefix + rel_path if rel_path else prefix
        return f"{method} {full}"

    def _endpoint_paths(self, endpoints: list[dict]) -> set[str]:
        """엔드포인트 dict 목록을 'METHOD /api/path' 집합으로 변환한다."""
        return {self._get_full_ep_path(ep) for ep in endpoints}

    def _format_ep_for_prompt(self, ep: dict) -> str:
        """프롬프트용 엔드포인트 표기: 'METHOD /path [인증필요|공개]'."""
        auth_label = "인증필요" if ep.get("requires_auth") else "공개"
        return f"{self._get_full_ep_path(ep)} [{auth_label}]"

    def _split_scenario_requirements(
        self, requirements: list[dict]
    ) -> tuple[list[dict], list[dict]]:
        """API 통합 시나리오로 검증 가능한 요구사항과 제외 대상을 분리한다."""
        target: list[dict] = []
        skipped: list[dict] = []
        for req in requirements:
            if self._is_api_scenario_requirement(req):
                target.append(req)
            else:
                skipped.append({
                    **req,
                    "skip_reason": "통합 API 시나리오로 직접 검증하기 어려운 비기능 요구사항",
                })
        return target, skipped

    def _is_api_scenario_requirement(self, req: dict) -> bool:
        """기능 FR 또는 API로 샘플링 가능한 운영성 요구사항만 시나리오화한다."""
        req_id = req.get("req_id", "")
        req_type = req.get("req_type", "functional")
        text = f"{req_id} {req.get('domain_area', '')} {req.get('content', '')}"

        if req_id.startswith("FR-") or req_type == "functional":
            return True

        if any(keyword in text for keyword in _API_TESTABLE_OPERATIONAL_KEYWORDS):
            return True

        if any(keyword in text for keyword in _NON_INTEGRATION_REQUIREMENT_KEYWORDS):
            return False

        return False

    def _select_endpoints_for_requirement(self, req: dict, all_endpoints: list[dict]) -> list[dict]:
        """요구사항 텍스트를 기준으로 실제 존재하는 API 후보를 좁힌다.

        LLM 매핑이 실패했을 때 쓰는 결정적 fallback이다. 요구사항 도메인으로 라우터를
        먼저 좁힌 뒤, 동작 키워드(조회/변경/해지 등)로 HTTP method를 제한한다.
        """
        req_text = f"{req.get('domain_area', '')} {req.get('content', '')}"
        explicit_eps = self._match_explicit_endpoints(req_text, all_endpoints)
        if explicit_eps:
            return explicit_eps

        operational_eps = self._select_operational_endpoints(req_text, all_endpoints)
        if operational_eps:
            return operational_eps

        matched_basenames: set[str] = set()
        for basename, keywords in _ROUTER_KEYWORDS.items():
            if any(keyword in req_text for keyword in keywords):
                matched_basenames.add(basename)

        def endpoint_domain(ep: dict) -> str:
            file_path = ep.get("file", "")
            basename = file_path.split("/")[-1].replace(".py", "").replace(".ts", "").replace(".js", "")
            if "contracts" in file_path:
                return "contracts"
            return basename

        domain_eps = [
            ep for ep in all_endpoints
            if endpoint_domain(ep) in matched_basenames
        ]
        if not domain_eps:
            return []

        preferred_methods: list[str] = []
        text_lower = req_text.lower()
        if any(keyword in req_text for keyword in ("조회", "목록", "상세", "확인", "열람")):
            preferred_methods.append("GET")
        for keywords, methods in _ACTION_METHOD_MAP:
            if any(self._kw_in(keyword, text_lower) for keyword in keywords):
                preferred_methods.extend(methods)

        if preferred_methods:
            method_set = set(preferred_methods)
            filtered = [ep for ep in domain_eps if ep.get("method") in method_set]
            if filtered:
                return filtered

        return domain_eps

    def _match_explicit_endpoints(self, text: str, all_endpoints: list[dict]) -> list[dict]:
        """요구사항 본문에 명시된 METHOD /api/path를 실제 endpoint dict로 매칭한다."""
        wanted = {
            f"{m.group(1)} {m.group(2).rstrip('`.,)')}"
            for m in re.finditer(r'\b(GET|POST|PUT|PATCH|DELETE)\s+`?(/api/[^\s`|,)]+)', text)
        }
        if not wanted:
            return []

        return [
            ep for ep in all_endpoints
            if self._get_full_ep_path(ep) in wanted
        ]

    def _select_operational_endpoints(self, text: str, all_endpoints: list[dict]) -> list[dict]:
        """X-Trace-Id 같은 운영성 요구사항을 대표 API에 매핑한다."""
        if not any(keyword in text for keyword in _API_TESTABLE_OPERATIONAL_KEYWORDS):
            return []

        preferred = [
            "GET /api/plans",
            "GET /api/plans/{plan_id}",
            "POST /api/auth/login",
        ]
        by_path = {self._get_full_ep_path(ep): ep for ep in all_endpoints}
        return [by_path[path] for path in preferred if path in by_path]

    def _sanitize_scenarios_against_requirements(
        self,
        scenarios: list,
        requirements: list[dict],
        req_endpoint_map: dict[str, list[dict]],
    ) -> None:
        """최종 저장 전 존재하는 요구사항과 매핑된 API만 남긴다."""
        valid_req_ids = {req.get("req_id", "") for req in requirements}
        for s in scenarios:
            req_ids = [
                req_id for req_id in s.get("requirements", [])
                if req_id in valid_req_ids
            ]
            s["requirements"] = list(dict.fromkeys(req_ids))
            allowed_apis = self._endpoint_paths([
                ep for req_id in s["requirements"]
                for ep in req_endpoint_map.get(req_id, [])
            ])

            for tc in s.get("test_cases", []):
                if tc.get("req_id") not in valid_req_ids:
                    tc["req_id"] = s["requirements"][0] if s["requirements"] else None
                api = tc.get("api")
                if api and api not in (None, "null") and api not in allowed_apis:
                    tc["api"] = None

    def _find_router_files_for_domain(self, domain_area: str, reqs: list[dict]) -> list[str]:
        """도메인 영역명과 요구사항 내용 키워드로 관련 라우터 파일 목록을 반환한다."""
        all_endpoints: list[dict] = self._read_index_json("endpoints.json")  # type: ignore[assignment]
        search_text = domain_area + " " + " ".join(r.get("content", "") for r in reqs)
        matched_basenames: set[str] = set()
        for basename, keywords in _ROUTER_KEYWORDS.items():
            if any(k in search_text for k in keywords):
                matched_basenames.add(basename)

        def _file_matches(file_path: str) -> bool:
            if not file_path:
                return False
            basename = file_path.split("/")[-1].replace(".py", "").replace(".ts", "").replace(".js", "")
            if basename == "main":  # 진입점 파일(health-only)은 제외
                return False
            if basename in matched_basenames:
                return True
            # 파일명이 도메인과 다른 경우(예: contracts/routers.py) 디렉터리 경로 컴포넌트로 매칭
            return any(part in matched_basenames for part in file_path.split("/")[:-1])

        return list(dict.fromkeys(
            ep.get("file", "")
            for ep in all_endpoints
            if _file_matches(ep.get("file", ""))
        ))

    def _format_scan_summary_for_domain(self, domain_area: str, router_files: list[str]) -> str:
        """도메인 영역 기준 scan summary를 반환한다.

        도메인 라우터 엔드포인트를 전체 경로(/api/prefix/path)로 보여준다.
        매칭된 라우터가 없으면 전체 엔드포인트 목록을 참조용으로 포함한다.
        """
        manifest: dict = self._read_index_json("manifest.json")  # type: ignore[assignment]
        all_endpoints: list[dict] = self._read_index_json("endpoints.json")  # type: ignore[assignment]
        domain_eps = [ep for ep in all_endpoints if ep.get("file", "") in router_files]
        short_files = [
            f.split("/")[-1].replace(".py", "").replace(".ts", "").replace(".js", "")
            for f in router_files
        ]

        if domain_eps:
            ep_strs = [self._format_ep_for_prompt(ep) for ep in domain_eps]
            ep_label = f"엔드포인트 ({len(domain_eps)}개): {', '.join(ep_strs)}"
        else:
            # 라우터 매칭 실패 시 전체 목록을 참조용으로 제공
            all_ep_strs = [self._format_ep_for_prompt(ep) for ep in all_endpoints
                           if ep.get("path") is not None and not ep.get("path", "").endswith("/health")]
            ep_label = f"엔드포인트 (0개 — 전체 목록 참조): {', '.join(all_ep_strs)}"

        lines = [
            f"프레임워크: {manifest.get('framework', 'unknown')} ({manifest.get('language', 'unknown')})",
            f"도메인 영역: {domain_area}",
            f"관련 라우터: {', '.join(short_files) or '없음'}",
            ep_label,
        ]
        return "\n".join(lines)

    def _format_code_index_for_domain(self, router_files: list[str], scan_result: dict) -> str:
        """여러 라우터 파일의 핸들러·헬퍼·모델을 통합해서 반환한다."""
        if not router_files:
            return "코드 인덱스 없음"
        all_functions: list[dict] = self._read_index_json("functions.json")  # type: ignore[assignment]
        all_models: list[dict] = self._read_index_json("models.json")  # type: ignore[assignment]
        all_endpoints: list[dict] = self._read_index_json("endpoints.json")  # type: ignore[assignment]

        domain_eps = [ep for ep in all_endpoints if ep.get("file") in router_files]
        handler_names: set[str] = {ep.get("handler", "") for ep in domain_eps if ep.get("handler")}
        router_fns = [fn for fn in all_functions if fn.get("file") in router_files]
        helper_names: set[str] = set()
        for fn in router_fns:
            if fn.get("name") in handler_names:
                helper_names.update(fn.get("calls", []))

        lines: list[str] = []

        handler_fns = [fn for fn in router_fns if fn.get("name") in handler_names and fn.get("body_excerpt")]
        if handler_fns:
            lines.append("## 핸들러 소스 코드")
            for fn in handler_fns[:20]:
                lines.append(f"\n### {fn.get('file', '').split('/')[-1]} :: {fn['name']}{fn.get('params', '')}")
                lines.append(fn["body_excerpt"])

        helper_fns = [
            fn for fn in all_functions
            if fn.get("name") in helper_names
            and fn.get("name") not in handler_names
            and fn.get("body_excerpt")
        ]
        if helper_fns:
            lines.append("\n## 헬퍼 함수 소스 코드")
            for fn in helper_fns[:8]:
                lines.append(f"\n### {fn.get('file', '').split('/')[-1]} :: {fn['name']}{fn.get('params', '')}")
                lines.append(fn["body_excerpt"])

        router_dirs = {"/".join(f.split("/")[:-1]) for f in router_files}
        related_models = [md for md in all_models if "/".join(md.get("file", "").split("/")[:-1]) in router_dirs]
        if related_models:
            lines.append("\n## 데이터 모델 (스키마 필드)")
            for md in related_models[:15]:
                fields = ", ".join(md.get("fields", [])[:15])
                lines.append(f"  {md['name']}: {fields}")

        git_diff = scan_result.get("git_diff") or {}
        diff_detail = [d for d in git_diff.get("diff_detail", []) if d.get("file") in router_files]
        if diff_detail:
            lines.append("\n## 변경 상세")
            for d in diff_detail:
                lines.append(f"  {d.get('file', '').split('/')[-1]}: +{d.get('added', 0)} -{d.get('deleted', 0)} lines")

        return "\n".join(lines) if lines else "코드 인덱스 없음"

    def _map_requirements_to_endpoints(
        self, requirements: list, all_endpoints: list[dict]
    ) -> dict[str, list[dict]]:
        """규칙 기반으로 REQ↔엔드포인트 매핑 테이블을 생성한다.

        요구사항 본문의 명시적 API 표기 → 운영성 키워드 → 도메인 키워드 → HTTP 메서드 순으로
        결정적으로 매핑한다. LLM을 사용하지 않는다.

        Returns:
            {req_id: [endpoint_dict, ...]} — 매핑 없으면 빈 리스트.
        """
        result: dict[str, list[dict]] = {}
        for req in requirements:
            req_id = req.get("req_id", "")
            result[req_id] = self._select_endpoints_for_requirement(req, all_endpoints)

        mapped = sum(1 for eps in result.values() if eps)
        self.logger.info(
            "req_endpoint_mapping_done",
            total_reqs=len(requirements),
            mapped=mapped,
            unmapped=len(requirements) - mapped,
        )
        return result

    def _deduplicate_scenarios(self, scenarios: list) -> list:
        """동일 API 집합을 커버하는 중복 시나리오를 병합한다.

        두 TS가 완전히 같은 엔드포인트 집합만 다루면 중복으로 판단하고,
        두 번째 TS의 고유 TC를 첫 번째 TS에 병합한 뒤 제거한다.

        단, API 집합이 1개뿐인 경우 이름도 같아야 병합한다.
        LLM이 다른 목적의 TC에 동일한 단일 API를 잘못 태깅했을 때 시나리오들이
        과도하게 합쳐지는 것을 방지하기 위함이다.
        """
        result: list = []
        # key: (api_set, name_if_single_api) → index in result
        seen_api_sets: dict[tuple, int] = {}

        for s in scenarios:
            api_set = frozenset(
                tc.get("api", "")
                for tc in s.get("test_cases", [])
                if tc.get("api") and tc.get("api") not in (None, "null")
            )
            if not api_set:
                result.append(s)
                continue

            # 단일 엔드포인트 시나리오는 이름도 일치해야 중복 판정
            name_key = s.get("name", "") if len(api_set) == 1 else ""
            dedup_key = (api_set, name_key)

            if dedup_key in seen_api_sets:
                existing = result[seen_api_sets[dedup_key]]
                existing_gwt: set[str] = {
                    f"{tc.get('given','')}|{tc.get('when','')}|{tc.get('then','')}"
                    for tc in existing["test_cases"]
                }
                for tc in s["test_cases"]:
                    key = f"{tc.get('given','')}|{tc.get('when','')}|{tc.get('then','')}"
                    if key not in existing_gwt:
                        existing["test_cases"].append(tc)
                        existing_gwt.add(key)
                self.logger.info(
                    "duplicate_scenario_merged",
                    merged_name=s.get("name", ""),
                    into_name=existing.get("name", ""),
                )
            else:
                seen_api_sets[dedup_key] = len(result)
                result.append(s)

        return result

    def _renumber_and_set_depends_on(self, all_scenarios: list) -> list:
        """TS ID를 순서대로 재부여하고 depends_on을 설정한다."""
        for i, s in enumerate(all_scenarios):
            old_ts_id = s["ts_id"]
            new_ts_id = f"TS-{i + 1:03d}"
            s["ts_id"] = new_ts_id

            # 병합으로 중복된 TC ID를 포함해 전체 TC를 순서대로 재번호 부여.
            # old_id → new_id 맵을 먼저 빌드하고 depends_on 참조도 교체한다.
            old_to_new_tc: dict[str, str] = {}
            for j, tc in enumerate(s["test_cases"]):
                old_id = tc["tc_id"]
                new_id = f"{new_ts_id}-TC-{j + 1:02d}"
                if old_id not in old_to_new_tc:
                    old_to_new_tc[old_id] = new_id
                tc["tc_id"] = new_id

            for tc in s["test_cases"]:
                if tc.get("depends_on"):
                    tc["depends_on"] = [old_to_new_tc.get(d, d) for d in tc["depends_on"]]

        basename_to_tsid: dict[str, str] = {}
        for s in all_scenarios:
            for f in s.get("affected_files") or []:
                b = f.split("/")[-1].replace(".py", "").replace(".ts", "").replace(".js", "")
                basename_to_tsid.setdefault(b, s["ts_id"])

        for s in all_scenarios:
            dep_ids: list[str] = []
            seen: set[str] = set()
            for f in s.get("affected_files") or []:
                b = f.split("/")[-1].replace(".py", "").replace(".ts", "").replace(".js", "")
                for dep_b in _ROUTER_DEPENDENCIES.get(b, []):
                    dep_ts = basename_to_tsid.get(dep_b)
                    if dep_ts and dep_ts not in seen and dep_ts != s["ts_id"]:
                        dep_ids.append(dep_ts)
                        seen.add(dep_ts)
            s["depends_on"] = dep_ids

        return all_scenarios

    def _format_single_requirement(self, req: dict) -> str:
        if not req:
            return "없음"
        return (
            f"[{req['req_id']}] ({req.get('req_type', '')}/{req.get('priority', '')}) "
            f"{req['content']} [도메인: {req.get('domain_area', '')}]"
        )

    def _format_scan_summary_for_requirement(
        self, req_id: str, req: dict, endpoints: list[dict]
    ) -> str:
        manifest: dict = self._read_index_json("manifest.json")  # type: ignore[assignment]
        router_names = list(dict.fromkeys(
            ep.get("file", "").split("/")[-1].replace(".py", "").replace(".ts", "").replace(".js", "")
            for ep in endpoints if ep.get("file")
        ))
        ep_strs = [self._format_ep_for_prompt(ep) for ep in endpoints]
        lines = [
            f"프레임워크: {manifest.get('framework', 'unknown')} ({manifest.get('language', 'unknown')})",
            f"검증 요구사항: [{req_id}] {req.get('content', '')}",
            f"구현 라우터: {', '.join(router_names)}",
            f"관련 엔드포인트 ({len(endpoints)}개): {', '.join(ep_strs)}",
        ]
        return "\n".join(lines)

    def _format_code_index_for_requirement(
        self, router_files: list[str], endpoints: list[dict], scan_result: dict
    ) -> str:
        all_functions: list[dict] = self._read_index_json("functions.json")  # type: ignore[assignment]
        all_models: list[dict] = self._read_index_json("models.json")  # type: ignore[assignment]

        handler_names: set[str] = {ep.get("handler", "") for ep in endpoints if ep.get("handler")}
        router_fns = [fn for fn in all_functions if fn.get("file") in router_files]
        helper_names: set[str] = set()
        for fn in router_fns:
            if fn.get("name") in handler_names:
                helper_names.update(fn.get("calls", []))

        lines: list[str] = []

        handler_fns = [fn for fn in router_fns if fn.get("name") in handler_names and fn.get("body_excerpt")]
        if handler_fns:
            lines.append("## 핸들러 소스 코드")
            for fn in handler_fns:
                lines.append(f"\n### {fn.get('file', '').split('/')[-1]} :: {fn['name']}{fn.get('params', '')}")
                lines.append(fn["body_excerpt"])

        helper_fns = [
            fn for fn in all_functions
            if fn.get("name") in helper_names
            and fn.get("name") not in handler_names
            and fn.get("body_excerpt")
        ]
        if helper_fns:
            lines.append("\n## 헬퍼 함수 소스 코드")
            for fn in helper_fns[:8]:
                lines.append(f"\n### {fn.get('file', '').split('/')[-1]} :: {fn['name']}{fn.get('params', '')}")
                lines.append(fn["body_excerpt"])

        router_dirs = {"/".join(f.split("/")[:-1]) for f in router_files}
        related_models = [md for md in all_models if "/".join(md.get("file", "").split("/")[:-1]) in router_dirs]
        if related_models:
            lines.append("\n## 데이터 모델 (스키마 필드)")
            for md in related_models[:15]:
                fields = ", ".join(md.get("fields", [])[:15])
                lines.append(f"  {md['name']}: {fields}")

        git_diff = scan_result.get("git_diff") or {}
        diff_detail = [d for d in git_diff.get("diff_detail", []) if d.get("file") in router_files]
        if diff_detail:
            lines.append("\n## 변경 상세")
            for d in diff_detail:
                lines.append(f"  {d.get('file', '').split('/')[-1]}: +{d.get('added', 0)} -{d.get('deleted', 0)} lines")

        return "\n".join(lines) if lines else "코드 인덱스 없음"

    async def _fetch_scan_result(self) -> dict:
        try:
            return await self.use_tool("codebase_scanner", {"action": "scan"})
        except Exception:
            return {}

    async def _fetch_domain_rules(self, query: str, top_k: int = 5) -> list:
        try:
            result = await self.use_tool(
                "domain_knowledge",
                {"action": "search", "query": query, "top_k": top_k},
            )
            return result.get("rules", [])
        except Exception:
            return []

    def _keyword_match_endpoints(self, text: str, all_endpoints: list[dict]) -> list[dict]:
        """요구사항 텍스트의 도메인 키워드로 관련 엔드포인트를 찾는다."""
        matched_basenames: list[str] = []
        for basename, keywords in _ROUTER_KEYWORDS.items():
            if any(kw in text for kw in keywords):
                matched_basenames.append(basename)
        if not matched_basenames:
            return []
        return [
            ep for ep in all_endpoints
            if ep.get("file", "").split("/")[-1].replace(".py", "").replace(".ts", "").replace(".js", "")
            in matched_basenames
        ]

    def _filter_requirements_for_router(self, requirements: list, basename: str) -> list:
        """라우터 키워드와 매칭되는 요구사항만 반환한다. 매칭 없으면 전체의 앞 5개."""
        keywords = _ROUTER_KEYWORDS.get(basename, [])
        if not keywords:
            return requirements[:5]
        matched = [
            r for r in requirements
            if any(k in r.get("content", "") + r.get("domain_area", "") for k in keywords)
        ]
        return matched[:10] if matched else requirements[:5]

    def _format_requirements(self, requirements: list) -> str:
        if not requirements:
            return "없음"
        lines: list[str] = []
        for r in requirements:
            base = (
                f"[{r['req_id']}] ({r.get('req_type', '')}/{r.get('priority', '')}) "
                f"{r['content']} [도메인: {r.get('domain_area', '')}]"
            )
            action_type = r.get("action_type", "create")
            target_level = r.get("target_level", "ts")
            target_ts_id = r.get("target_ts_id")
            target_tc_id = r.get("target_tc_id")
            if action_type == "update":
                meta = f" [action=update level={target_level}"
                if target_ts_id:
                    meta += f" target_ts={target_ts_id}"
                if target_tc_id:
                    meta += f" target_tc={target_tc_id}"
                meta += "]"
                base += meta
            lines.append(base)
        return "\n".join(lines)

    def _build_router_map(self, affected_files: list[str]) -> dict[str, list[dict]]:
        """endpoints.json에서 라우터 파일 → 엔드포인트 목록 맵을 구성한다.

        health check 전용 파일(핸들러가 health/healthcheck뿐인 경우)은 제외한다.
        affected_files가 있으면 해당 파일만, 없으면 전체 라우터를 반환한다.
        """
        all_endpoints: list[dict] = self._read_index_json("endpoints.json")  # type: ignore[assignment]
        router_map: dict[str, list[dict]] = {}
        for ep in all_endpoints:
            f = ep.get("file", "")
            if f:
                router_map.setdefault(f, []).append(ep)

        _HEALTH_HANDLERS = {"health", "healthcheck", "ping"}

        def is_meaningful(eps: list[dict]) -> bool:
            return any(ep.get("handler", "") not in _HEALTH_HANDLERS for ep in eps)

        filtered = {f: eps for f, eps in router_map.items() if is_meaningful(eps)}

        if affected_files:
            in_scope = {f: eps for f, eps in filtered.items() if f in affected_files}
            return in_scope or filtered

        return filtered

    def _sort_router_map(self, router_map: dict[str, list[dict]]) -> dict[str, list[dict]]:
        """_ROUTER_PRIORITY 순서로 라우터맵을 정렬한다. 목록에 없는 라우터는 뒤에 붙는다."""
        def priority(file_path: str) -> int:
            basename = file_path.split("/")[-1].replace(".py", "").replace(".ts", "").replace(".js", "")
            try:
                return _ROUTER_PRIORITY.index(basename)
            except ValueError:
                return len(_ROUTER_PRIORITY)
        return dict(sorted(router_map.items(), key=lambda kv: priority(kv[0])))

    def _format_scan_summary_for_router(self, router_file: str, endpoints: list[dict]) -> str:
        """단일 라우터 파일 기준의 scan summary를 반환한다."""
        manifest: dict = self._read_index_json("manifest.json")  # type: ignore[assignment]
        short = router_file.split("/")[-1].replace(".py", "").replace(".ts", "").replace(".js", "")
        ep_strs = [self._format_ep_for_prompt(ep) for ep in endpoints]
        lines = [
            f"프레임워크: {manifest.get('framework', 'unknown')} ({manifest.get('language', 'unknown')})",
            f"대상 라우터: [{short}] {router_file}",
            f"엔드포인트 ({len(endpoints)}개): {', '.join(ep_strs)}",
        ]
        return "\n".join(lines)

    def _format_code_index_for_router(self, router_file: str, scan_result: dict) -> str:
        """단일 라우터 파일의 핸들러·헬퍼 소스코드와 관련 모델을 반환한다."""
        all_functions: list[dict] = self._read_index_json("functions.json")  # type: ignore[assignment]
        all_models: list[dict] = self._read_index_json("models.json")  # type: ignore[assignment]
        all_endpoints: list[dict] = self._read_index_json("endpoints.json")  # type: ignore[assignment]

        router_endpoints = [ep for ep in all_endpoints if ep.get("file") == router_file]
        handler_names: set[str] = {ep.get("handler", "") for ep in router_endpoints if ep.get("handler")}

        router_fns = [fn for fn in all_functions if fn.get("file") == router_file]
        helper_names: set[str] = set()
        for fn in router_fns:
            if fn.get("name") in handler_names:
                helper_names.update(fn.get("calls", []))

        lines: list[str] = []

        # 핸들러 소스
        handler_fns = [fn for fn in router_fns if fn.get("name") in handler_names and fn.get("body_excerpt")]
        if handler_fns:
            lines.append("## 핸들러 소스 코드")
            for fn in handler_fns:
                lines.append(f"\n### {fn['name']}{fn.get('params', '')}")
                lines.append(fn["body_excerpt"])

        # 헬퍼 소스 (같은 파일 내 또는 다른 파일)
        helper_fns = [
            fn for fn in all_functions
            if fn.get("name") in helper_names
            and fn.get("name") not in handler_names
            and fn.get("body_excerpt")
        ]
        if helper_fns:
            lines.append("\n## 헬퍼 함수 소스 코드")
            for fn in helper_fns[:8]:
                lines.append(f"\n### {fn.get('file','').split('/')[-1]} :: {fn['name']}{fn.get('params','')}")
                lines.append(fn["body_excerpt"])

        # 관련 모델
        router_dir = "/".join(router_file.split("/")[:-1])
        related_models = [md for md in all_models if router_dir in md.get("file", "")]
        if related_models:
            lines.append("\n## 데이터 모델 (스키마 필드)")
            for md in related_models[:15]:
                fields = ", ".join(md.get("fields", [])[:15])
                lines.append(f"  {md['name']}: {fields}")

        # git diff
        git_diff = scan_result.get("git_diff") or {}
        diff_detail = [d for d in git_diff.get("diff_detail", []) if d.get("file") == router_file]
        if diff_detail:
            d = diff_detail[0]
            lines.append(f"\n## 변경 상세: +{d.get('added',0)} -{d.get('deleted',0)} lines")

        return "\n".join(lines) if lines else "코드 인덱스 없음"

    def _get_index_dir(self) -> Path:
        # pipeline 이 state.qapilot_dir 을 _execute context 로 주입한 경우 그것이 진실원천.
        # 미주입(CLI 단독 호출 등) 시 기존 fallback (config.project.root 또는 CWD).
        override = getattr(self, "_qapilot_dir_override", None)
        if override:
            return Path(override) / "codebase-index"
        proj = self._config.project
        base = Path(proj.root or proj.repo_path or ".")
        return base / ".qapilot" / "codebase-index"

    def _read_index_json(self, filename: str) -> list | dict:
        path = self._get_index_dir() / filename
        if not path.exists():
            return [] if filename != "manifest.json" else {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return [] if filename != "manifest.json" else {}

    def _format_code_index(self, scan_result: dict, affected_files: list[str]) -> str:
        lines: list[str] = []

        all_functions: list[dict] = self._read_index_json("functions.json")  # type: ignore[assignment]
        all_endpoints: list[dict] = self._read_index_json("endpoints.json")  # type: ignore[assignment]

        target_fns = (
            [fn for fn in all_functions if fn.get("file", "") in affected_files]
            if affected_files
            else all_functions
        )

        # 핸들러 이름 집합 — 엔드포인트에 직접 연결된 함수
        handler_names: set[str] = {ep.get("handler", "") for ep in all_endpoints if ep.get("handler")}
        # 핸들러가 호출하는 헬퍼 함수 이름 집합
        helper_names: set[str] = set()
        for fn in target_fns:
            if fn.get("name") in handler_names:
                helper_names.update(fn.get("calls", []))

        # ── 핸들러 소스 코드 (body_excerpt 우선) ──────────────────────────────
        handler_fns = [fn for fn in target_fns if fn.get("name") in handler_names and fn.get("body_excerpt")]
        if handler_fns:
            lines.append("## 핸들러 소스 코드")
            for fn in handler_fns[:20]:
                lines.append(f"\n### {fn['file'].split('/')[-1]} :: {fn['name']}{fn.get('params','')}")
                lines.append(fn["body_excerpt"])

        # ── 헬퍼 함수 소스 코드 ───────────────────────────────────────────────
        helper_fns = [
            fn for fn in target_fns
            if fn.get("name") in helper_names
            and fn.get("name") not in handler_names
            and fn.get("body_excerpt")
        ]
        if helper_fns:
            lines.append("\n## 헬퍼 함수 소스 코드")
            for fn in helper_fns[:10]:
                lines.append(f"\n### {fn['file'].split('/')[-1]} :: {fn['name']}{fn.get('params','')}")
                lines.append(fn["body_excerpt"])

        # ── 데이터 모델 (models.json) ──────────────────────────────────────────
        all_models: list[dict] = self._read_index_json("models.json")  # type: ignore[assignment]
        target_models = (
            [md for md in all_models if md.get("file", "") in affected_files]
            if affected_files
            else all_models
        )
        model_lines: list[str] = []
        for md in target_models[:30]:
            name = md.get("name", "")
            fields = ", ".join(md.get("fields", [])[:15])
            model_lines.append(f"  {name}: {fields}")
        if model_lines:
            lines.append("\n## 데이터 모델 (스키마 필드)")
            lines.extend(model_lines)

        # ── Git diff 상세 (인메모리 — 디스크에 없음) ───────────────────────────
        git_diff = scan_result.get("git_diff") or {}
        diff_detail: list[dict] = git_diff.get("diff_detail", [])
        if diff_detail:
            lines.append("\n## 변경 상세 (파일별 추가/삭제 라인)")
            for d in diff_detail[:15]:
                lines.append(f"  {d.get('file', '')}: +{d.get('added', 0)} -{d.get('deleted', 0)}")
        author = git_diff.get("author", "")
        commit_ts = git_diff.get("commit_timestamp", "")
        if author:
            lines.append(f"## 최근 커밋  작성자: {author}  시각: {commit_ts}")

        return "\n".join(lines) if lines else "코드 인덱스 없음"

    def _format_scan_summary(self, scan_result: dict, affected_files: list[str]) -> str:
        manifest: dict = self._read_index_json("manifest.json")  # type: ignore[assignment]
        lines = [
            f"프레임워크: {manifest.get('framework', 'unknown')} ({manifest.get('language', 'unknown')})",
            f"API 엔드포인트 수: {manifest.get('endpoint_count', 0)}",
        ]

        all_endpoints: list[dict] = self._read_index_json("endpoints.json")  # type: ignore[assignment]
        if affected_files:
            lines.append(f"변경 파일: {', '.join(affected_files[:10])}")
            target_eps = [ep for ep in all_endpoints if ep.get("file", "") in affected_files] or all_endpoints
        else:
            target_eps = all_endpoints

        router_map: dict[str, list[dict]] = {}
        for ep in target_eps:
            router_map.setdefault(ep.get("file", ""), []).append(ep)

        if router_map:
            lines.append(f"API 라우터 ({len(router_map)}개 파일):")
            for file_path, eps in router_map.items():
                short_path = file_path.split("/")[-1].replace(".py", "").replace(".ts", "").replace(".js", "")
                ep_strs = [self._get_full_ep_path(ep) for ep in eps]
                lines.append(f"  [{short_path}] {', '.join(ep_strs)}")

        return "\n".join(lines)
