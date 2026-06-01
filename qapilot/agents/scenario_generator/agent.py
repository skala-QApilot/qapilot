"""시나리오 생성 Agent.

코드베이스 분석 결과와 Git diff를 기반으로
테스트 시나리오를 자동 생성한다.

담당: B
Created: 2026-05-07
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

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


class ScenarioGeneratorAgent(BaseAgent):
    """시나리오 생성 Agent.

    역할: 코드 변경 기반 테스트 시나리오 자동 생성
    입력: CodebaseContext, GitDiff, DomainRules, RequirementItems
    출력: List[TestScenario], confidence
    호출 Tool: 코드 인덱스 Tool, 도메인 지식 Tool
    HITL: O (파이프라인 hitl_review 노드 — 미구현, 추후 연동)
    """

    allowed_tools = ["codebase_scanner", "domain_knowledge"]

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
                mismatch_text, mismatches, last_error,
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
            all_scenarios.extend(ts_scenarios)
            confidence_sum += ts_confidence

        confidence = round(confidence_sum / len(router_map), 3) if router_map else 0.5
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
        mismatch_text: str,
        mismatches: list,
        last_error: str | None,
    ) -> ExecuteResult:
        """요구사항 기반 도메인별 시나리오 생성 — 1 domain_area = 1 TS."""
        from qapilot.tools.domain_knowledge import DomainKnowledgeTool

        all_scenarios: list = []
        confidence_sum = 0.0

        for req in requirements:
            domain_area = req.get("domain_area") or "기타"
            router_files = self._find_router_files_for_domain(domain_area, [req])
            area_domain_rules = await self._fetch_domain_rules(domain_area, top_k=5)
            area_domain_rules_text = DomainKnowledgeTool.format_rules_for_prompt(area_domain_rules) or "없음"

            user_prompt = self.with_correction_hint(
                self.prompts.render(
                    domain_rules=area_domain_rules_text,
                    requirements=self._format_requirements([req]),
                    scan_summary=self._format_scan_summary_for_domain(domain_area, router_files),
                    code_index=self._format_code_index_for_domain(router_files, scan_result),
                    affected_files=", ".join(router_files) if router_files else domain_area,
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
                response.content, trigger, router_files, domain_rules
            )

            # 각 TC 에 req_id 강제 주입 — LLM 이 출력에서 누락해도 RTM 매핑이 유실되지 않도록.
            # _run_domain_based 는 req 단위 1회 호출이라 모든 결과 TC 가 이 req 를 검증.
            for s in ts_scenarios:
                for tc in s.get("test_cases") or []:
                    if not tc.get("req_id"):
                        tc["req_id"] = req.get("req_id")

            for s in ts_scenarios:
                if len(s["test_cases"]) < 6:
                    self.logger.warning(
                        "tc_count_below_minimum",
                        req_id=req.get("req_id"),
                        domain=domain_area,
                        tc_count=len(s["test_cases"]),
                        minimum=6,
                    )

            all_scenarios.extend(ts_scenarios)
            confidence_sum += ts_confidence
            self.logger.info(
                "requirement_scenario_generated",
                req_id=req.get("req_id"),
                domain=domain_area,
                ts_count=len(ts_scenarios),
                tc_count=sum(len(s["test_cases"]) for s in ts_scenarios),
            )

        confidence = round(confidence_sum / len(requirements), 3) if requirements else 0.5
        all_scenarios = self._renumber_and_set_depends_on(all_scenarios)
        save_scenarios(all_scenarios)

        self.logger.info(
            "scenarios_generated",
            count=len(all_scenarios),
            tc_count=sum(len(s["test_cases"]) for s in all_scenarios),
            mismatch_count=len(mismatches),
            confidence=confidence,
        )

        return ExecuteResult(
            result={"scenarios": all_scenarios, "prd_code_mismatches": mismatches},
            confidence=confidence,
        )

    def _find_router_files_for_domain(self, domain_area: str, reqs: list[dict]) -> list[str]:
        """도메인 영역명과 요구사항 내용 키워드로 관련 라우터 파일 목록을 반환한다."""
        all_endpoints: list[dict] = self._read_index_json("endpoints.json")  # type: ignore[assignment]
        search_text = domain_area + " " + " ".join(r.get("content", "") for r in reqs)
        matched_basenames: set[str] = set()
        for basename, keywords in _ROUTER_KEYWORDS.items():
            if any(k in search_text for k in keywords):
                matched_basenames.add(basename)
        return list(dict.fromkeys(
            ep.get("file", "")
            for ep in all_endpoints
            if ep.get("file", "").split("/")[-1]
                .replace(".py", "").replace(".ts", "").replace(".js", "")
            in matched_basenames
            and ep.get("file", "")
        ))

    def _format_scan_summary_for_domain(self, domain_area: str, router_files: list[str]) -> str:
        """도메인 영역 기준 scan summary를 반환한다."""
        manifest: dict = self._read_index_json("manifest.json")  # type: ignore[assignment]
        all_endpoints: list[dict] = self._read_index_json("endpoints.json")  # type: ignore[assignment]
        domain_eps = [ep for ep in all_endpoints if ep.get("file", "") in router_files]
        ep_strs = [f"{ep.get('method', '?')} {ep.get('path', '?')}" for ep in domain_eps]
        short_files = [
            f.split("/")[-1].replace(".py", "").replace(".ts", "").replace(".js", "")
            for f in router_files
        ]
        lines = [
            f"프레임워크: {manifest.get('framework', 'unknown')} ({manifest.get('language', 'unknown')})",
            f"도메인 영역: {domain_area}",
            f"관련 라우터: {', '.join(short_files) or '없음'}",
            f"엔드포인트 ({len(domain_eps)}개): {', '.join(ep_strs) or '없음'}",
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

    async def _map_requirements_to_endpoints(
        self, requirements: list, all_endpoints: list[dict]
    ) -> dict[str, list[dict]]:
        """LLM 1회 호출로 REQ↔엔드포인트 매핑 테이블을 생성한다.

        Returns:
            {req_id: [endpoint_dict, ...]} — 매핑 없으면 빈 리스트.
        """
        req_text = "\n".join(
            f"[{r['req_id']}] ({r.get('req_type', '')}/{r.get('priority', '')}) {r['content']}"
            for r in requirements
        )
        ep_text = "\n".join(
            f"{ep.get('method', '?')} {ep.get('path', '?')} "
            f"[{ep.get('file', '').split('/')[-1]}] handler={ep.get('handler', '')}"
            for ep in all_endpoints
        )

        system = (
            "당신은 소프트웨어 요구사항과 API 엔드포인트를 매핑하는 전문가다. "
            "요구사항 목록과 API 엔드포인트 목록을 분석하여 각 요구사항을 구현하는 엔드포인트를 정확히 찾아라."
        )
        user = f"""# 요구사항 목록
{req_text}

# API 엔드포인트 목록
{ep_text}

# 지시사항
각 요구사항(REQ-XXX)에 대해 그 요구사항을 구현하는 엔드포인트를 매핑하라.
하나의 요구사항이 여러 엔드포인트에 매핑될 수 있다. 구현 엔드포인트가 없으면 빈 배열로 표시하라.
반드시 아래 JSON 형식으로만 출력하라:
{{
  "mappings": [
    {{
      "req_id": "REQ-001",
      "endpoints": [
        {{"method": "POST", "path": "/auth/login"}}
      ]
    }}
  ]
}}"""

        try:
            response = await self.llm.chat(system, user)
            cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", response.content).strip()
            parsed = json.loads(cleaned)

            ep_lookup: dict[tuple[str, str], dict] = {
                (ep.get("method", ""), ep.get("path", "")): ep for ep in all_endpoints
            }

            result: dict[str, list[dict]] = {}
            for mapping in parsed.get("mappings", []):
                req_id = mapping["req_id"]
                matched: list[dict] = []
                for m in mapping.get("endpoints", []):
                    ep = ep_lookup.get((m.get("method", ""), m.get("path", "")))
                    if ep:
                        matched.append(ep)
                result[req_id] = matched

            self.logger.info(
                "req_endpoint_mapping_done",
                total_reqs=len(requirements),
                mapped=sum(1 for eps in result.values() if eps),
                unmapped=sum(1 for eps in result.values() if not eps),
            )
            return result

        except Exception as e:
            self.logger.warning("req_endpoint_mapping_failed", error=str(e))
            return {}

    def _renumber_and_set_depends_on(self, all_scenarios: list) -> list:
        """TS ID를 순서대로 재부여하고 depends_on을 설정한다."""
        for i, s in enumerate(all_scenarios):
            old_ts_id = s["ts_id"]
            new_ts_id = f"TS-{i + 1:03d}"
            s["ts_id"] = new_ts_id
            for tc in s["test_cases"]:
                tc["tc_id"] = tc["tc_id"].replace(old_ts_id, new_ts_id)

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
        ep_strs = [f"{ep.get('method', '?')} {ep.get('path', '?')}" for ep in endpoints]
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
        return "\n".join(
            f"[{r['req_id']}] ({r['req_type']}/{r['priority']}) {r['content']} [도메인: {r['domain_area']}]"
            for r in requirements
        )

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
        ep_strs = [f"{ep.get('method','?')} {ep.get('path','?')}" for ep in endpoints]
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
                ep_strs = [f"{ep.get('method','?')} {ep.get('path','?')}" for ep in eps]
                lines.append(f"  [{short_path}] {', '.join(ep_strs)}")

        return "\n".join(lines)
