"""아키텍처 심층 점검 후속 fix 검증 (2026-06-11).

- P0: declarative Base 가 db_model 로 인덱싱되어 pick_table_for_tc fallback 이
  table="base" 선택 → DB 스냅샷 404 ×95 (trace 44a15469)
- P0: 오라클 역전 — doc_verified 의미 정상화 + mismatch_note TC 출력 보존
- P0: TC 생성 화면 grounding (_build_frontend_grounding)
- P1: false-pass 봉인 — ui skip 을 passed 로 세지 않음 (skipped 분리)
- P1: code_generator 의 test.skip → throw (MANUAL_REVIEW)
"""
from __future__ import annotations

from qapilot.agents.code_generator_agent import CodeGeneratorAgent
from qapilot.agents.tc_doc_search_agent import TCFromDocsAgent
from qapilot.orchestrator.pipeline import (
    _aggregate_scenario_results,
    _aggregate_tc_results,
    _build_frontend_grounding,
)
from qapilot.shared.metadata_filters import pick_table_for_tc


class TestPickTableExcludesBase:
    SCHEMAS = {
        "db_models": {
            "Base": {"table_name": "base", "columns": []},
            "Customer": {"table_name": "customers", "columns": [{"name": "id"}]},
        }
    }

    def test_fallback_skips_declarative_base(self):
        # api 매칭 실패 → fallback 이 Base 가 아닌 실제 테이블 선택
        tc = {"api": "POST /api/auth/login"}
        assert pick_table_for_tc(tc, self.SCHEMAS) != "base"

    def test_only_base_means_none(self):
        tc = {"api": "POST /api/auth/login"}
        schemas = {"db_models": {"Base": {"table_name": "base", "columns": []}}}
        assert pick_table_for_tc(tc, schemas) is None


class TestDocVerifiedSemantics:
    def _parse(self, tc: dict) -> dict:
        agent = TCFromDocsAgent.__new__(TCFromDocsAgent)
        import json
        from unittest.mock import MagicMock
        agent.logger = MagicMock()
        valid, _, _ = agent._parse(json.dumps({"test_cases": [tc], "analysis": [], "confidence": 0.8}))
        return valid[0]

    def test_doc_source_sets_verified(self):
        tc = self._parse({"name": "t", "sources": ["PRD_v4.0.md"]})
        assert tc["doc_verified"] is True

    def test_codebase_only_is_provisional(self):
        tc = self._parse({"name": "t", "sources": ["codebase"]})
        assert tc["doc_verified"] is False

    def test_mismatch_note_preserved(self):
        tc = self._parse({"name": "t", "sources": ["PRD_v4.0.md"],
                          "mismatch_note": "문서는 400, 코드는 409"})
        assert tc["mismatch_note"] == "문서는 400, 코드는 409"


class TestFrontendGrounding:
    ROUTES = {"routes": [
        {"path": "/login", "guards": [], "redirects_when_authed": "/dashboard"},
        {"path": "/dashboard", "guards": ["auth"], "redirects_when_authed": None},
    ]}
    SELECTORS = {"by_route": {
        "/login": {"outputs": [
            {"testid": "login-error"}, {"testid": "login-title"},
        ]},
        "/signup": {"outputs": [{"testid": "signup-success-toast"}]},
    }}

    def test_routes_and_feedback_summarized(self):
        text = _build_frontend_grounding(self.ROUTES, self.SELECTORS)
        assert "/login" in text
        assert "로그인 상태면 /dashboard" in text
        assert "인증 필요" in text
        assert "login-error" in text
        assert "signup-success-toast" in text
        # 피드백 토큰 미포함 testid 는 제외
        assert "login-title" not in text

    def test_empty_graceful(self):
        assert _build_frontend_grounding(None, None) == ""


class TestSkipNotCountedAsPass:
    def test_ui_skip_becomes_skipped(self):
        ui = [{"tc_id": "TC-1", "status": "skip"}]
        result = _aggregate_tc_results(ui, [], [])
        assert result["TC-1"] == "skipped"

    def test_pass_still_passed(self):
        ui = [{"tc_id": "TC-1", "status": "pass"}]
        assert _aggregate_tc_results(ui, [], [])["TC-1"] == "passed"

    def test_all_skipped_scenario_unmeasured(self):
        tc_results = {"TS-001-TC-01": "skipped"}
        scenarios = [{"ts_id": "TS-001", "test_cases": [{"tc_id": "TS-001-TC-01"}]}]
        assert _aggregate_scenario_results(tc_results, scenarios) == {}

    def test_mixed_skip_pass_scenario_passed(self):
        tc_results = {"TS-001-TC-01": "skipped", "TS-001-TC-02": "passed"}
        scenarios = [{"ts_id": "TS-001", "test_cases": [
            {"tc_id": "TS-001-TC-01"}, {"tc_id": "TS-001-TC-02"},
        ]}]
        assert _aggregate_scenario_results(tc_results, scenarios) == {"TS-001": "passed"}


class TestNoSilentSkipInGeneratedCode:
    def _agent(self) -> CodeGeneratorAgent:
        from unittest.mock import MagicMock
        agent = CodeGeneratorAgent.__new__(CodeGeneratorAgent)
        agent.logger = MagicMock()
        return agent

    def test_missing_selector_throws(self):
        line = self._agent()._render_step(
            {"action": "fill", "selector": None, "selector_type": None, "value": "x"}
        )
        text = line if isinstance(line, str) else "\n".join(line)
        assert "test.skip" not in text
        assert "QAPILOT_MANUAL_REVIEW" in text

    def test_unsupported_action_throws(self):
        line = self._agent()._render_step({"action": "teleport", "selector": "x", "selector_type": "css"})
        text = line if isinstance(line, str) else "\n".join(line)
        assert "test.skip" not in text
        assert "QAPILOT_MANUAL_REVIEW" in text
