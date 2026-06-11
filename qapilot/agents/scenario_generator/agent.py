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
from qapilot.shared import progress
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
        self._service_id: str | None = context.get("service_id")

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
        """요구사항 없을 때의 라우터 기반 fallback.

        code_change 트리거일 때: 기존 TS의 affected_files와 매칭되는 router_file이 있으면
        새 TS를 생성하지 않고 _run_update_ts 로 기존 TS를 업데이트한다.
        라우터 파일이 삭제된 경우(git diff에 있지만 router_map에 없는) 기존 TS를 orphaned 처리한다.
        """
        from qapilot.tools.domain_knowledge import DomainKnowledgeTool

        router_map = self._sort_router_map(self._build_router_map(affected_files))
        all_scenarios: list = []
        all_updated: list = []
        confidence_sum = 0.0

        # code_change: 기존 TS 로드 (router_file 매칭 + orphan 탐지용)
        existing_scenarios: list[dict] = []
        if trigger == "code_change":
            existing_scenarios = self._load_all_scenarios()

        for _idx, (router_file, endpoints) in enumerate(router_map.items()):
            progress.item(getattr(self, "trace_id", None), "scenario_generate", _idx, len(router_map))
            basename = router_file.split("/")[-1].replace(".py", "").replace(".ts", "").replace(".js", "")
            keywords = _ROUTER_KEYWORDS.get(basename, [])
            query = " ".join(keywords) if keywords else basename
            router_domain_rules = await self._fetch_domain_rules(query, top_k=5)
            router_domain_rules_text = DomainKnowledgeTool.format_rules_for_prompt(router_domain_rules) or "없음"

            # code_change: 기존 TS가 이 router_file을 커버하면 update 경로
            existing_ts = next(
                (ts for ts in existing_scenarios if router_file in ts.get("affected_files", [])),
                None,
            ) if trigger == "code_change" else None

            if existing_ts:
                synthetic_req = {
                    "req_id": "",
                    "domain_area": basename,
                    "content": f"{router_file} 파일 변경",
                    "action_type": "update",
                    "target_ts_id": existing_ts["ts_id"],
                    "target_level": "ts",
                }
                updated_ts, ts_confidence = await self._run_update_ts(
                    synthetic_req, existing_ts, scan_result,
                    router_domain_rules_text, trigger, mismatch_text, endpoints,
                )
                if self._scenario_content_unchanged(updated_ts, existing_ts):
                    updated_ts["_unchanged"] = True
                else:
                    updated_ts["_change_type"] = "ts_updated"
                    updated_ts["_change_target"] = existing_ts["ts_id"]
                    updated_ts["_change_tc_ids"] = self._changed_test_case_ids(updated_ts, existing_ts)
                all_updated.append(updated_ts)
                confidence_sum += ts_confidence
                self.logger.info(
                    "scenario_updated_code_change",
                    ts_id=existing_ts["ts_id"],
                    router_file=router_file,
                )
                continue

            # 기존 TS 없음 → 새 TS 생성
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

        # orphan 탐지: affected_files가 git diff에 있지만 router_map에 없는 TS
        # → 해당 라우터 파일이 삭제된 것으로 판단, 기존 TS를 검토 대상으로 마킹
        orphaned_ts_ids: list[str] = []
        if trigger == "code_change" and existing_scenarios and affected_files:
            router_map_files = set(router_map.keys())
            changed_file_set = set(affected_files)
            for ts in existing_scenarios:
                for af in ts.get("affected_files", []):
                    if af in changed_file_set and af not in router_map_files:
                        ts_id = ts.get("ts_id", "")
                        if ts_id and ts_id not in orphaned_ts_ids:
                            orphaned_ts_ids.append(ts_id)
                            self.logger.info(
                                "scenario_orphaned_code_change",
                                ts_id=ts_id,
                                deleted_file=af,
                            )

        num_processed = len(router_map) or 1
        confidence = round(confidence_sum / num_processed, 3) if confidence_sum else 0.5
        all_scenarios = self._deduplicate_scenarios(all_scenarios)
        all_scenarios = self._renumber_and_set_depends_on(all_scenarios)
        # 기존 TS 번호(DB + all_updated)와의 충돌 방지 — DB 기반 서비스에서도 동작하도록
        existing_nums_in_memory: set[int] = set()
        for ts in existing_scenarios:
            parts = ts.get("ts_id", "").split("-")
            if len(parts) == 2 and parts[1].isdigit():
                existing_nums_in_memory.add(int(parts[1]))
        for ts in all_updated:
            parts = ts.get("ts_id", "").split("-")
            if len(parts) == 2 and parts[1].isdigit():
                existing_nums_in_memory.add(int(parts[1]))
        all_scenarios = self._avoid_ts_id_conflicts(all_scenarios, existing_nums_in_memory)
        # 디스크 영속화는 pipeline._save_scenarios 노드에서 수행 (위 메서드와 동일 사유).

        result_data: dict = {"scenarios": all_scenarios + all_updated, "prd_code_mismatches": mismatches}
        if orphaned_ts_ids:
            result_data["orphaned_ts_ids"] = orphaned_ts_ids

        return ExecuteResult(
            result=result_data,
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

        for _idx, req in enumerate(target_requirements):
            progress.item(getattr(self, "trace_id", None), "scenario_generate", _idx, len(target_requirements))
            domain_area = req.get("domain_area") or "기타"
            req_id = req.get("req_id", "")
            action_type = req.get("action_type", "create")
            target_ts_id = req.get("target_ts_id")
            target_level = req.get("target_level", "ts")

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

            # ── delete 분기: TS/TC 삭제 검토 요청 ──────────────────────────────
            # target_ts_id 유무와 관계없이 반드시 continue — create 브랜치로 낙하 방지
            if action_type == "delete":
                target_tc_id = req.get("target_tc_id")
                if target_level == "tc":
                    # TC 단위 삭제: TS에서 해당 TC를 제거하고 _deleted_tc_ids 마킹
                    if target_ts_id and target_tc_id:
                        existing_ts = self._load_scenario_file(target_ts_id)
                        if existing_ts is None:
                            self.logger.warning(
                                "delete_tc_target_ts_not_found",
                                target_ts_id=target_ts_id,
                                target_tc_id=target_tc_id,
                                req_id=req_id,
                            )
                        else:
                            original_tcs = existing_ts.get("test_cases") or []
                            tc_exists = any(tc.get("tc_id") == target_tc_id for tc in original_tcs)
                            if not tc_exists:
                                self.logger.warning(
                                    "delete_tc_not_found_in_ts",
                                    target_ts_id=target_ts_id,
                                    target_tc_id=target_tc_id,
                                    req_id=req_id,
                                )
                            else:
                                # TC를 제거하지 않고 _pending_delete: True로 마킹해 파일에 보존.
                                # 프론트엔드가 빨간 스타일로 표시 → 사용자 검토 후 승인/거절.
                                # 승인 시 Spring이 _pending_delete TC를 실제 제거, 거절 시 rollback.
                                for tc in original_tcs:
                                    if tc.get("tc_id") == target_tc_id:
                                        tc["_pending_delete"] = True
                                existing_ts["test_cases"] = original_tcs
                                existing_ts["_deleted_tc_ids"] = [target_tc_id]
                                existing_ts["_change_type"] = "tc_deleted"
                                all_updated.append(existing_ts)
                                self.logger.info(
                                    "tc_delete_requested",
                                    ts_id=target_ts_id,
                                    tc_id=target_tc_id,
                                )
                    else:
                        self.logger.warning(
                            "delete_tc_target_not_resolved",
                            domain_area=domain_area,
                            target_ts_id=target_ts_id,
                            target_tc_id=target_tc_id,
                            req_id=req_id,
                        )
                else:
                    # TS 단위 삭제
                    if target_ts_id:
                        existing_ts = self._load_scenario_file(target_ts_id)
                        if existing_ts is None:
                            self.logger.warning(
                                "delete_target_not_found",
                                target_ts_id=target_ts_id,
                                req_id=req_id,
                            )
                        else:
                            existing_ts["_ts_delete_requested"] = True
                            all_updated.append(existing_ts)
                            self.logger.info("scenario_delete_requested", ts_id=target_ts_id)
                    else:
                        self.logger.warning(
                            "delete_target_not_resolved",
                            domain_area=domain_area,
                            req_id=req_id,
                        )
                continue

            # ── update 분기: 기존 TS 로드 후 수정 ──────────────────────────────
            # create + target_ts_id: 기존 TS에 새 TC/TV 추가 (추가해줘 패턴)
            is_add_to_existing = action_type == "create" and target_ts_id and target_level in ("tc", "tv")
            if is_add_to_existing or (action_type == "update" and target_ts_id):
                existing_ts = self._load_scenario_file(target_ts_id)
                if existing_ts is None:
                    self.logger.warning(
                        "update_target_not_found_fallback_create",
                        target_ts_id=target_ts_id,
                        req_id=req_id,
                    )
                    # 대상 TS 없으면 create로 폴백
                else:
                    if target_level == "tc":
                        # create+tc: target_tc_id=None → 새 TC 추가
                        # update+tc: target_tc_id 있음 → 기존 TC 수정
                        updated_ts, ts_confidence = await self._run_update_tc(
                            req, existing_ts, scan_result, area_domain_rules_text,
                            trigger, mismatch_text, req_endpoints,
                        )
                    elif target_level == "tv":
                        updated_ts, ts_confidence = await self._run_add_tv(
                            req, existing_ts, scan_result, area_domain_rules_text,
                            trigger, mismatch_text, req_endpoints,
                        )
                    else:  # "ts" (default) — update only
                        updated_ts, ts_confidence = await self._run_update_ts(
                            req, existing_ts, scan_result, area_domain_rules_text,
                            trigger, mismatch_text, req_endpoints,
                        )
                    if self._scenario_content_unchanged(updated_ts, existing_ts):
                        # LLM이 재생성했지만 TC 구성이 기존과 실질적으로 동일한 경우 —
                        # (요구사항 재추출 결과의 표현 차이로 _diff_status="updated"로
                        # 잘못 분류된 doc_update 등) "AI 생성"(검토 대기) 표시를 띄우지
                        # 않도록 변경 없음으로 마킹한다.
                        updated_ts["_unchanged"] = True
                    else:
                        # 변경 정보 태그 — _save_scenarios에서 change_summary 생성에 사용
                        if target_level == "tc":
                            # target_tc_id가 있으면 기존 TC 수정, 없으면 새 TC 추가
                            updated_ts["_change_type"] = "tc_updated" if req.get("target_tc_id") else "tc_added"
                        elif target_level == "tv":
                            updated_ts["_change_type"] = "tv_updated"
                        else:
                            updated_ts["_change_type"] = "ts_updated"
                        updated_ts["_change_target"] = req.get("target_tc_id") if target_level in ("tc", "tv") else target_ts_id
                        # TC 단위 강조 범위 — 검토 화면에서 TS 전체가 아닌 실제로
                        # 바뀐 TC(및 그 TV)에만 "AI 생성" 표시를 좁히기 위한 정보.
                        updated_ts["_change_tc_ids"] = self._changed_test_case_ids(updated_ts, existing_ts)
                    all_updated.append(updated_ts)
                    confidence_sum += ts_confidence
                    self.logger.info(
                        "scenario_updated",
                        ts_id=target_ts_id,
                        target_level=target_level,
                        req_id=req_id,
                    )
                    continue

            # ── create 분기 (기본) ────────────────────────────────────────────
            user_prompt = self.with_correction_hint(
                self.prompts.render(
                    domain_rules=area_domain_rules_text,
                    requirements=self._format_requirements([req]),
                    scan_summary=self._format_scan_summary_for_requirement(req_id, req, req_endpoints),
                    code_index=self._format_code_index_for_requirement(router_files, req_endpoints, scan_result),
                    affected_files=", ".join(prompt_files) if prompt_files else domain_area,
                    trigger=trigger,
                    mismatch_note=mismatch_text,
                    update_context="",
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
            # create 변경 정보 태그
            target_ts_id_for_create = req.get("target_ts_id")
            for ts in ts_scenarios:
                if target_ts_id_for_create:
                    ts["_change_type"] = "tc_added"
                    ts["_change_target"] = target_ts_id_for_create
                else:
                    ts["_change_type"] = "ts_created"
            all_scenarios.extend(ts_scenarios)
            confidence_sum += ts_confidence

        # update 결과는 ts_id 고정 — 재번호 부여 없이 바로 덮어쓴다.
        # delete 요청(_ts_delete_requested)은 파일 내용 변경 없이 pipeline에서 change_request만 등록.
        from qapilot.agents.scenario_generator.repository import save_scenario
        for ts in all_updated:
            if not ts.get("_ts_delete_requested"):
                save_scenario(ts)

        confidence = round(confidence_sum / max(len(requirements), 1), 3)

        if not all_scenarios and all_updated:
            # create 대상이 없고 update만 있는 경우 — coverage gap-fill 불필요
            self.logger.info(
                "scenarios_updated_only",
                updated_count=len(all_updated),
            )
            confidence = round(confidence_sum / max(len(requirements), 1), 3)
            return ExecuteResult(
                result={
                    "scenarios": all_updated,
                    "prd_code_mismatches": mismatches,
                    "coverage": {"rate": 1.0, "uncovered": [], "skipped": []},
                    "skipped_requirements": skipped_requirements,
                },
                confidence=confidence,
            )
        # ── 커버리지 gap-fill ─────────────────────────────────────────────────
        # 생성 후 미커버 요구사항이 있으면 해당 req만 재생성한다 (최대 1회).
        _MAX_FILL_RETRIES = 2
        call_count = len(target_requirements)

        for _attempt in range(_MAX_FILL_RETRIES):
            # all_updated 도 포함해 커버리지를 계산 — 안 그러면 update 로 처리된
            # 요구사항이 "미커버"로 잘못 판정되어 불필요한 gap-fill 재생성이 발생한다
            # (doc_update 증분 재생성은 create+update 가 한 배치에 섞이는 게 일반적).
            coverage = compute_coverage(all_scenarios + all_updated, target_requirements)
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
                    update_context="",
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
        final_coverage = compute_coverage(all_scenarios + all_updated, target_requirements)
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
        # DB-only 서비스에서도 기존 TS 번호를 감지해 충돌을 방지한다.
        import re as _re
        _existing_for_conflict = self._load_all_scenarios()
        _existing_nums: set[int] = set()
        for _ts in _existing_for_conflict + all_updated:
            _m = _re.match(r"TS-(\d+)$", _ts.get("ts_id", ""))
            if _m:
                _existing_nums.add(int(_m.group(1)))
        all_scenarios = self._avoid_ts_id_conflicts(all_scenarios, _existing_nums or None)
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
                # all_updated 를 함께 반환 — 그렇지 않으면 create+update 가 섞인 배치에서
                # update 결과가 _save_scenarios 로 전달되지 못해 DB upsert/change_request
                # 등록이 누락된다 (이전엔 all_scenarios 가 비어있을 때만 all_updated 를 반환).
                "scenarios": all_scenarios + all_updated,
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

        if rel_path and str(rel_path).startswith("/api/"):
            full = rel_path
        else:
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

    # ── update 액션 헬퍼 (#201) ────────────────────────────────────────────────

    # doc_update 등으로 TS를 재생성할 때 _run_update_ts 가 req_id 를 파이프라인 값으로
    # 덮어쓰기 때문에, 내용(given/when/then/values 등)이 동일해도 dict 전체 비교 시
    # 다른 것으로 판단되는 문제가 있다. 비교에서 제외할 메타데이터 키를 명시적으로 정의해
    # "실질 내용이 같으면 변경 없음"으로 처리한다.
    _TC_METADATA_KEYS: frozenset[str] = frozenset({"req_id", "last_run_status"})

    @classmethod
    def _tc_content(cls, tc: dict) -> dict:
        """TC 딕셔너리에서 비교에 사용할 내용 필드만 추출한다.

        req_id 등 파이프라인이 주입하는 메타데이터, _ 로 시작하는 내부 마커는 제외.
        """
        return {k: v for k, v in tc.items() if k not in cls._TC_METADATA_KEYS and not k.startswith("_")}

    @classmethod
    def _scenario_content_unchanged(cls, updated: dict, existing: dict) -> bool:
        """update 결과가 기존 시나리오와 실질적으로 동일한지 비교한다 (이슈 #261 후속).

        요구사항 재추출 결과의 표현 차이(_diff_status="updated" 오분류 등)로
        실질 변경이 없는데도 LLM이 재생성한 경우, name/test_cases 가 기존과
        동일하면 변경 없음으로 간주해 불필요한 "AI 생성" 표시를 막는다.

        TC 비교 시 req_id 등 메타데이터 필드는 제외한다 — doc_update 재생성 시
        파이프라인이 req_id 를 덮어써 내용이 같아도 다른 것으로 판단되는 문제 방지.
        """
        updated_tcs = [cls._tc_content(tc) for tc in updated.get("test_cases") or []]
        existing_tcs = [cls._tc_content(tc) for tc in existing.get("test_cases") or []]
        return (
            updated.get("name") == existing.get("name")
            and updated_tcs == existing_tcs
        )

    @classmethod
    def _changed_test_case_ids(cls, updated: dict, existing: dict) -> list[str]:
        """update 결과에서 실질적으로 변경/추가된 TC의 tc_id 목록을 반환한다 (이슈 #277 후속).

        TS 전체에 "AI 생성" 표시가 붙으면 어떤 TC가 실제로 바뀌었는지 구분하기
        어려우므로, tc_id 기준으로 기존 TC와 내용을 비교해 변경/추가된 TC만 골라
        검토 화면의 강조 범위를 TC 단위로 좁힌다.

        req_id 등 메타데이터는 비교에서 제외 — _scenario_content_unchanged 와 동일한
        이유로, 내용이 실질적으로 같은 TC 를 잘못 "변경됨"으로 분류하지 않기 위함.
        """
        existing_map = {tc.get("tc_id"): tc for tc in existing.get("test_cases", []) if tc.get("tc_id")}
        changed: list[str] = []
        for tc in updated.get("test_cases", []):
            tc_id = tc.get("tc_id")
            if not tc_id:
                continue
            prior = existing_map.get(tc_id)
            if prior is None or cls._tc_content(tc) != cls._tc_content(prior):
                changed.append(tc_id)
        return changed

    @staticmethod
    def _next_tc_id(ts_id: str, existing_tcs: list[dict]) -> str:
        """기존 TC 목록에서 숫자 suffix의 다음 TC ID를 반환한다."""
        pattern = re.compile(rf"^{re.escape(ts_id)}-TC-(\d+)$", re.IGNORECASE)
        nums: list[int] = []
        for tc in existing_tcs:
            m = pattern.match(str(tc.get("tc_id") or ""))
            if m:
                nums.append(int(m.group(1)))
        return f"{ts_id}-TC-{(max(nums) + 1) if nums else 1:02d}"

    def _avoid_ts_id_conflicts(
        self,
        scenarios: list[dict],
        extra_existing_nums: set[int] | None = None,
    ) -> list[dict]:
        """create 경로 시나리오의 ts_id가 기존 파일 또는 기존 TS(DB)와 충돌하면 다음 번호로 재할당한다.

        natural_lang "추가해줘" 쿼리로 새 TS를 생성할 때 기존 TS를 덮어쓰는 문제를 방지한다.
        extra_existing_nums: DB에서 로드한 기존 TS 번호 집합 — disk check를 보완한다.
        """
        from pathlib import Path

        existing_nums: set[int] = set(extra_existing_nums or set())

        qapilot_dir = getattr(self, "_qapilot_dir_override", None)
        if qapilot_dir:
            scenarios_dir = Path(qapilot_dir) / "scenarios"
            if scenarios_dir.exists():
                for p in scenarios_dir.iterdir():
                    if not p.is_dir():
                        continue
                    parts = p.name.split("-")
                    if len(parts) == 2 and parts[1].isdigit():
                        existing_nums.add(int(parts[1]))

        if not existing_nums:
            return scenarios

        next_num = max(existing_nums) + 1
        result = []
        for ts in scenarios:
            ts_id = ts.get("ts_id", "")
            parts = ts_id.split("-")
            if len(parts) == 2 and parts[1].isdigit() and int(parts[1]) in existing_nums:
                new_ts_id = f"TS-{next_num:03d}"
                next_num += 1
                # TC ID도 새 ts_id 기준으로 갱신 (TS-001-TC-01 → TS-010-TC-01)
                updated_tcs = [
                    {**tc, "tc_id": f"{new_ts_id}-TC-{j:02d}"}
                    for j, tc in enumerate(ts.get("test_cases", []), start=1)
                ]
                ts = {**ts, "ts_id": new_ts_id, "test_cases": updated_tcs}
                self.logger.info("ts_id_conflict_resolved", old=ts_id, new=new_ts_id)
            result.append(ts)
        return result

    def _load_all_scenarios(self) -> list[dict]:
        """서비스의 모든 기존 TS를 로드한다 (DB 우선, qapilot_dir 폴백)."""
        from pathlib import Path
        from qapilot.db.scenario_reader import load_latest_scenarios
        from qapilot.agents.scenario_generator.repository import load_all_scenarios

        service_id = getattr(self, "_service_id", None)
        if service_id:
            rows = load_latest_scenarios(str(service_id))
            if rows:
                return rows

        qapilot_dir = getattr(self, "_qapilot_dir_override", None)
        if not qapilot_dir:
            return []

        scenarios_dir = Path(qapilot_dir) / "scenarios"
        return load_all_scenarios(base_dir=scenarios_dir)

    def _load_scenario_file(self, ts_id: str) -> dict | None:
        """기존 시나리오를 DB 우선, 필요 시 qapilot_dir fallback 으로 로드한다."""
        from pathlib import Path
        from qapilot.db.scenario_reader import load_latest_scenarios
        from qapilot.agents.scenario_generator.repository import load_scenario

        service_id = getattr(self, "_service_id", None)
        if service_id:
            rows = load_latest_scenarios(str(service_id), [ts_id])
            if rows:
                return rows[0]
        qapilot_dir = getattr(self, "_qapilot_dir_override", None)
        if qapilot_dir:
            scenarios_dir = Path(qapilot_dir) / "scenarios"
            ts = load_scenario(ts_id, base_dir=scenarios_dir)
            if ts is not None:
                return ts
            # 디스크 없음 → DB 폴백
            try:
                from qapilot.shared.trace_store import load_trace
                trace = load_trace(self.trace_id) or {}
                service_id = trace.get("service_id")
                if service_id:
                    all_ts = load_latest_scenarios(service_id, scenario_ids=[ts_id])
                    if all_ts:
                        return all_ts[0]
            except Exception:
                pass
            return None
        return load_scenario(ts_id)

    async def _run_update_ts(
        self,
        req: dict,
        existing_ts: dict,
        scan_result: dict,
        area_domain_rules_text: str,
        trigger: str,
        mismatch_text: str,
        req_endpoints: list,
    ) -> tuple[dict, float]:
        """기존 TS에서 변경이 필요한 TC만 delta 출력받아 tc_id 기준으로 병합한다.

        변경 없는 TC는 보존하고, 변경된 TC는 교체하며, 신규 TC는 추가한다.
        """
        ts_id = existing_ts["ts_id"]
        req_id = req.get("req_id", "")
        router_files = list(dict.fromkeys(ep.get("file", "") for ep in req_endpoints if ep.get("file")))

        existing_tc_summary = "\n".join(
            f"  - [{tc.get('tc_id', '')}] {tc.get('name', '')}"
            for tc in existing_ts.get("test_cases", [])
        )
        update_context = (
            f"## 수정 대상 시나리오 (ts_id: {ts_id} 유지)\n"
            f"현재 TS 이름: {existing_ts.get('name', '')}\n"
            f"현재 TC 목록:\n{existing_tc_summary}\n\n"
            f"변경이 필요한 TC만 출력하라. 변경 없는 TC는 출력하지 마라 — 코드에서 기존 TC를 보존한다.\n"
            f"수정된 TC는 기존 tc_id를 그대로 유지하라 (예: {ts_id}-TC-01).\n"
            f"신규 TC는 tc_id를 {ts_id}-TC-NEW-01 형식으로 부여하라.\n"
            f"ts_id는 반드시 {ts_id}로 고정하라."
        )

        user_prompt = self.prompts.render(
            domain_rules=area_domain_rules_text,
            requirements=self._format_requirements([req]),
            scan_summary=self._format_scan_summary_for_requirement(req_id, req, req_endpoints),
            code_index=self._format_code_index_for_requirement(router_files, req_endpoints, scan_result),
            affected_files=", ".join(router_files) if router_files else req.get("domain_area", ""),
            trigger=trigger,
            mismatch_note=mismatch_text,
            update_context=update_context,
        )

        response = await self.llm.chat(system_prompt=self.prompts.system(), user_prompt=user_prompt)
        ts_scenarios, ts_confidence = parse_response(
            response.content, trigger, router_files, [], preserve_tc_ids=True
        )

        if not ts_scenarios:
            return {**existing_ts, "_unchanged": True}, 0.3

        # tc_id 기준 병합: 기존 TC 보존 + 변경 TC 교체 + 신규 TC 추가
        # _to_delete: true TC는 _pending_delete: true 로 마킹해 UI가 빨간 스타일로 표시하도록 한다.
        delta_tcs = ts_scenarios[0].get("test_cases", [])
        delta_map = {tc.get("tc_id", ""): tc for tc in delta_tcs}
        existing_tcs = existing_ts.get("test_cases", [])

        merged_tcs = []
        used_tc_ids = set()
        for tc in existing_tcs:
            tc_id = tc.get("tc_id", "")
            if tc_id in delta_map:
                delta_tc = delta_map[tc_id]
                if delta_tc.get("_to_delete"):
                    # 삭제 대상: 기존 TC를 _pending_delete=True 로 마킹해 보존
                    merged_tcs.append({**tc, "_pending_delete": True})
                else:
                    merged = {**delta_tc}
                    if not merged.get("req_id"):
                        merged["req_id"] = req_id
                    merged_tcs.append(merged)
            else:
                merged_tcs.append(tc)
            used_tc_ids.add(tc_id)

        # 신규 TC (기존 tc_id에 없는 것, _to_delete 전용 항목 제외)
        for tc in delta_tcs:
            tc_id = tc.get("tc_id", "")
            if tc_id not in used_tc_ids and not tc.get("_to_delete"):
                tc["tc_id"] = self._next_tc_id(ts_id, merged_tcs)
                if not tc.get("req_id"):
                    tc["req_id"] = req_id
                merged_tcs.append(tc)

        # TS 메타데이터(name, description 등)는 LLM 출력 우선, test_cases는 병합 결과 사용
        llm_ts = ts_scenarios[0]
        updated = {**existing_ts, **llm_ts, "test_cases": merged_tcs, "ts_id": ts_id}

        # 삭제 대기 TC id 목록 — pipeline._save_scenarios 에서 change_request.content 에 포함
        deleted_tc_ids = [tc["tc_id"] for tc in merged_tcs if tc.get("_pending_delete")]
        if deleted_tc_ids:
            updated["_deleted_tc_ids"] = deleted_tc_ids

        return updated, ts_confidence

    async def _run_update_tc(
        self,
        req: dict,
        existing_ts: dict,
        scan_result: dict,
        area_domain_rules_text: str,
        trigger: str,
        mismatch_text: str,
        req_endpoints: list,
    ) -> tuple[dict, float]:
        """기존 TS에서 target_tc_id TC만 수정한다. 나머지 TC는 보존한다."""
        ts_id = existing_ts["ts_id"]
        req_id = req.get("req_id", "")
        target_tc_id = req.get("target_tc_id")
        router_files = list(dict.fromkeys(ep.get("file", "") for ep in req_endpoints if ep.get("file")))

        target_tc = next(
            (tc for tc in existing_ts.get("test_cases", []) if tc.get("tc_id") == target_tc_id),
            None,
        )

        self.logger.info("run_update_tc_debug", ts_id=ts_id, target_tc_id=target_tc_id, mode="update" if (target_tc_id and target_tc) else "add")

        if target_tc_id and target_tc:
            # update: 기존 TC 전체 재작성 — 사용자가 명시적으로 지칭한 TC를 요구사항에 맞게 수정
            import json as _json
            existing_tc_json = _json.dumps(target_tc, ensure_ascii=False, indent=2)
            update_context = (
                f"## TC 수정 모드 (ts_id: {ts_id}, tc_id: {target_tc_id} 유지)\n"
                f"수정 대상 TC 현재 내용:\n```json\n{existing_tc_json}\n```\n\n"
                f"위 TC를 아래 요구사항에 맞게 수정하라.\n"
                f"- 요구사항이 '더 구체적으로', '상세하게' 등을 요청하면 given/when/then을 실질적으로 구체화하라:\n"
                f"  given: 어떤 데이터/상태가 준비되어야 하는지 (파라미터, 인증 상태 등)\n"
                f"  when: 어떤 API/동작을 수행하는지 (HTTP 메서드, 경로, 입력값 포함)\n"
                f"  then: 어떤 결과가 반환되는지 (응답 코드, 반환 필드 목록, 상태 변화)\n"
                f"- 코드베이스 정보가 없어도 도메인 지식으로 구체화 가능하다. 일반적인 REST API 규칙과 도메인 지식을 활용하라.\n"
                f"- tc_id는 반드시 {target_tc_id}로 고정하고, "
                f"scenarios에 원소 1개, test_cases에 수정된 TC 전체(name/given/when/then/values/tags/depends_on)를 출력하라."
            )
        else:
            # create+tc: 새 TC 추가 — 완전한 새 TC 생성
            update_context = (
                f"## 새 TC 추가 모드 (ts_id: {ts_id} 유지)\n"
                f"기존 TC 목록:\n" +
                "\n".join(
                    f"  [{tc.get('tc_id', '')}] {tc.get('name', '')}"
                    for tc in existing_ts.get("test_cases", [])
                ) +
                f"\n\n위 TS에 아래 요구사항에 맞는 새 TC를 추가하라. "
                f"scenarios에 원소 1개, test_cases에 새 TC만 출력하라. "
                f"기존 TC와 주제가 유사해도 조건(횟수·입력값·결과)이 다르면 별도 TC로 추가한다. "
                f"given/when/then이 완전히 동일한 TC만 중복으로 간주하라."
            )

        user_prompt = self.prompts.render(
            domain_rules=area_domain_rules_text,
            requirements=self._format_requirements([req]),
            scan_summary=self._format_scan_summary_for_requirement(req_id, req, req_endpoints),
            code_index=self._format_code_index_for_requirement(router_files, req_endpoints, scan_result),
            affected_files=", ".join(router_files) if router_files else req.get("domain_area", ""),
            trigger=trigger,
            mismatch_note=mismatch_text,
            update_context=update_context,
        )

        response = await self.llm.chat(system_prompt=self.prompts.system(), user_prompt=user_prompt)
        ts_scenarios, ts_confidence = parse_response(response.content, trigger, router_files, [])

        if not ts_scenarios or not ts_scenarios[0].get("test_cases"):
            return {**existing_ts, "_unchanged": True}, 0.3

        output_tc = ts_scenarios[0]["test_cases"][0]
        if not output_tc.get("req_id"):
            output_tc["req_id"] = req_id

        if target_tc_id and target_tc:
            # update: LLM이 전체 TC를 재작성했으므로 output_tc를 그대로 교체
            # tc_id는 반드시 원본 유지
            output_tc["tc_id"] = target_tc_id
            updated_tcs = [
                output_tc if tc.get("tc_id") == target_tc_id else tc
                for tc in existing_ts.get("test_cases", [])
            ]
        else:
            # create+tc: 새 TC 추가 — LLM 생성 tc_id 대신 TS 기준으로 자동 부여
            existing_tcs = list(existing_ts.get("test_cases", []))
            output_tc["tc_id"] = self._next_tc_id(ts_id, existing_tcs)
            updated_tcs = existing_tcs + [output_tc]

        return {**existing_ts, "test_cases": updated_tcs}, ts_confidence

    async def _run_add_tv(
        self,
        req: dict,
        existing_ts: dict,
        scan_result: dict,
        area_domain_rules_text: str,
        trigger: str,
        mismatch_text: str,
        req_endpoints: list,
    ) -> tuple[dict, float]:
        """기존 TC에 새로운 TV(입력값 변형)를 추가한다."""
        import json as _json

        ts_id = existing_ts["ts_id"]
        req_id = req.get("req_id", "")
        target_tc_id = req.get("target_tc_id")
        router_files = list(dict.fromkeys(ep.get("file", "") for ep in req_endpoints if ep.get("file")))

        target_tcs = [
            tc for tc in existing_ts.get("test_cases", [])
            if (not target_tc_id) or tc.get("tc_id") == target_tc_id
        ]
        tc_summary = "\n".join(
            f"  [{tc.get('tc_id', '')}] {tc.get('name', '')}: "
            f"values={_json.dumps([v.get('field') for v in tc.get('values', [])], ensure_ascii=False)}"
            for tc in target_tcs
        )
        update_context = (
            f"## TV 추가 모드 (ts_id: {ts_id} 유지)\n"
            f"대상 TC: {target_tc_id or '전체'}\n"
            f"현재 TC 및 values:\n{tc_summary}\n\n"
            f"위 TC에 추가할 새로운 입력값 변형(values)을 포함한 TC를 출력하라. "
            f"기존 values를 유지하면서 새 values를 추가하라. "
            f"tc_id는 기존 값을 유지하라. "
            f"scenarios에 원소 1개, test_cases에 대상 TC만 출력하라."
        )

        user_prompt = self.prompts.render(
            domain_rules=area_domain_rules_text,
            requirements=self._format_requirements([req]),
            scan_summary=self._format_scan_summary_for_requirement(req_id, req, req_endpoints),
            code_index=self._format_code_index_for_requirement(router_files, req_endpoints, scan_result),
            affected_files=", ".join(router_files) if router_files else req.get("domain_area", ""),
            trigger=trigger,
            mismatch_note=mismatch_text,
            update_context=update_context,
        )

        response = await self.llm.chat(system_prompt=self.prompts.system(), user_prompt=user_prompt)
        ts_scenarios, ts_confidence = parse_response(
            response.content, trigger, router_files, [], preserve_tc_ids=True
        )

        if not ts_scenarios:
            return {**existing_ts, "_unchanged": True}, 0.3

        new_tc_map = {tc.get("tc_id", ""): tc for tc in ts_scenarios[0].get("test_cases", [])}
        updated_tcs: list = []
        for tc in existing_ts.get("test_cases", []):
            new_tc = new_tc_map.get(tc.get("tc_id", ""))
            if new_tc:
                existing_fields = {v.get("field") for v in tc.get("values", [])}
                extra_values = [
                    v for v in new_tc.get("values", [])
                    if v.get("field") not in existing_fields
                ]
                updated_tcs.append({**tc, "values": tc.get("values", []) + extra_values})
            else:
                updated_tcs.append(tc)

        return {**existing_ts, "test_cases": updated_tcs}, ts_confidence

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
            scoped_files = self._resolve_affected_router_files(affected_files, filtered)
            in_scope = {f: eps for f, eps in filtered.items() if f in scoped_files}
            return in_scope

        return filtered

    def _resolve_affected_router_files(
        self,
        affected_files: list[str],
        router_map: dict[str, list[dict]],
    ) -> set[str]:
        """Git diff 파일에서 실제 영향을 받는 라우터 파일 집합을 계산한다.

        직접 라우터 파일이 바뀐 경우만 포함하면 main.py 의 include_router 변경,
        service/config/helper 변경처럼 라우터가 참조하는 파일 변경을 놓친다. 반대로
        매칭 실패 시 전체 라우터로 확장하면 변경 범위를 벗어난 시나리오까지 수정된다.
        따라서 import/call index 로 연결된 라우터만 보수적으로 포함한다.
        """
        affected_set = set(affected_files)
        router_files = set(router_map.keys())
        scoped = affected_set & router_files

        callgraph: dict[str, list[str]] = self._read_index_json("callgraph.json")  # type: ignore[assignment]
        functions: list[dict] = self._read_index_json("functions.json")  # type: ignore[assignment]

        affected_stems = {Path(f).stem for f in affected_set}
        router_stems = {Path(f).stem: f for f in router_files}

        def imports_module(imports: list[str], stem: str) -> bool:
            pattern = re.compile(rf"(^|\b|\.|/){re.escape(stem)}(\b|\.|/|$)")
            return any(pattern.search(line) for line in imports or [])

        # main/app 등 진입점 변경: 해당 파일이 import/include 하는 라우터만 포함.
        for changed in affected_set:
            imports = callgraph.get(changed, [])
            for stem, router_file in router_stems.items():
                if imports_module(imports, stem):
                    scoped.add(router_file)

        # 라우터가 변경 파일의 모듈을 import 하면 해당 라우터를 포함.
        for router_file in router_files:
            imports = callgraph.get(router_file, [])
            if any(imports_module(imports, stem) for stem in affected_stems):
                scoped.add(router_file)

        # 라우터 핸들러가 변경 파일에 정의된 helper 함수를 호출하면 해당 라우터를 포함.
        changed_function_names = {
            fn.get("name")
            for fn in functions
            if fn.get("file") in affected_set and fn.get("name")
        }
        if changed_function_names:
            for router_file in router_files:
                for fn in functions:
                    if fn.get("file") != router_file:
                        continue
                    calls = set(fn.get("calls") or [])
                    if calls & changed_function_names:
                        scoped.add(router_file)
                        break

        return scoped

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
