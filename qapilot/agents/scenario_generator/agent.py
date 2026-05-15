"""시나리오 생성 Agent.

코드베이스 분석 결과와 Git diff를 기반으로
테스트 시나리오를 자동 생성한다.

담당: B
Created: 2026-05-07
"""

from __future__ import annotations

import json
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
        """코드베이스·도메인 규칙·요구사항을 조합하여 TS/TC/TV 시나리오를 생성한다.

        Args:
            context: 파이프라인 컨텍스트.
                scan_result: 코드베이스 스캔 결과 (없으면 Tool 호출).
                domain_rules: 도메인 규칙 목록 (없으면 Tool 호출).
                requirements: 요구사항 목록.
            params:
                trigger: 생성 트리거 (init/code_change/doc_update/natural_lang).
                affected_only: True이면 Git diff 기반 영향 파일만 대상으로 함.
            last_error: 이전 시도 에러 (자가 수정 힌트).

        Returns:
            ExecuteResult: scenarios(list[TestScenario]), prd_code_mismatches 포함.
        """
        scan_result: dict = context.get("scan_result") or {}
        domain_rules: list = context.get("domain_rules", [])
        requirements: list = context.get("requirements", [])
        trigger: str = params.get("trigger", "code_change")
        affected_only: bool = bool(params.get("affected_only", False))

        if not scan_result:
            scan_result = await self._fetch_scan_result()

        affected_files: list[str] = []
        if affected_only:
            affected_files = (scan_result.get("git_diff") or {}).get("changed_files", [])

        mismatches = detect_prd_code_mismatch(requirements, scan_result)
        mismatch_text = format_mismatches(mismatches)

        from qapilot.tools.domain_knowledge import DomainKnowledgeTool

        # 라우터 파일별로 LLM 호출을 분리한다 — 한 번에 전체를 생성하면 LLM이 중간에 중단함
        router_map = self._sort_router_map(self._build_router_map(affected_files))

        all_scenarios: list = []
        confidence_sum = 0.0

        for router_file, endpoints in router_map.items():
            basename = router_file.split("/")[-1].replace(".py", "").replace(".ts", "").replace(".js", "")

            # 라우터별 도메인 규칙: 관련 키워드로 Qdrant 검색 (top_k=5)
            keywords = _ROUTER_KEYWORDS.get(basename, [])
            query = " ".join(keywords) if keywords else basename
            router_domain_rules = await self._fetch_domain_rules(query, top_k=5)
            router_domain_rules_text = DomainKnowledgeTool.format_rules_for_prompt(router_domain_rules) or "없음"

            # 라우터별 요구사항: 파이프라인에서 받은 목록을 키워드로 필터링
            router_requirements = self._filter_requirements_for_router(requirements, basename)
            router_requirements_text = self._format_requirements(router_requirements)

            user_prompt = self.with_correction_hint(
                self.prompts.render(
                    domain_rules=router_domain_rules_text,
                    requirements=router_requirements_text,
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

            for s in ts_scenarios:
                tc_count = len(s["test_cases"])
                if tc_count < 6:
                    self.logger.warning(
                        "tc_count_below_minimum",
                        router=router_file,
                        tc_count=tc_count,
                        minimum=6,
                    )

            all_scenarios.extend(ts_scenarios)
            confidence_sum += ts_confidence
            self.logger.info(
                "router_scenarios_generated",
                router=router_file.split("/")[-1],
                ts_count=len(ts_scenarios),
                tc_count=sum(len(s["test_cases"]) for s in ts_scenarios),
            )

        confidence = round(confidence_sum / len(router_map), 3) if router_map else 0.5

        # TS ID를 전체 순서로 재부여한다
        for i, s in enumerate(all_scenarios):
            old_ts_id = s["ts_id"]
            new_ts_id = f"TS-{i + 1:03d}"
            s["ts_id"] = new_ts_id
            for tc in s["test_cases"]:
                tc["tc_id"] = tc["tc_id"].replace(old_ts_id, new_ts_id)

        # basename → ts_id 맵 구성 후 depends_on 설정
        basename_to_tsid: dict[str, str] = {}
        for s in all_scenarios:
            files = s.get("affected_files") or []
            if files:
                basename = files[0].split("/")[-1].replace(".py", "").replace(".ts", "").replace(".js", "")
                basename_to_tsid[basename] = s["ts_id"]

        for s in all_scenarios:
            files = s.get("affected_files") or []
            basename = files[0].split("/")[-1].replace(".py", "").replace(".ts", "").replace(".js", "") if files else ""
            dep_basenames = _ROUTER_DEPENDENCIES.get(basename, [])
            s["depends_on"] = [basename_to_tsid[dep] for dep in dep_basenames if dep in basename_to_tsid]

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
