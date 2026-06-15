"""perf 변경 (5a2f382) 의 verdict-불변 검증 — 노드 레벨.

- Layer3 병렬화: 순서 보존 + 예외 격리 + agent_logs 누적
- cc api-mode skip: CrossCheckAgent 미호출 + verdict 경로 동일
"""
from __future__ import annotations

import asyncio

import pytest

from qapilot.orchestrator import pipeline as pl


class _FakeOutput:
    def __init__(self, result):
        self.result = result

        class _M:
            def model_dump(self_inner):
                return {"agent": "fake"}
        self.metadata = _M()


def _fake_rc_agent_cls(delays: dict, fail_tc: str | None = None):
    class _Fake:
        def __init__(self, trace_id=None):
            pass

        async def run(self, agent_input):
            tc_id = agent_input.params.get("tc_id")
            await asyncio.sleep(delays.get(tc_id, 0))
            if tc_id == fail_tc:
                raise RuntimeError("boom")
            return _FakeOutput({"root_causes": [
                {"tc_id": tc_id, "candidates": [{"cause": f"c-{tc_id}"}]}
            ]})
    return _Fake


class TestRootCauseParallel:
    @pytest.mark.asyncio
    async def test_order_preserved_with_unequal_delays(self, monkeypatch):
        # 늦게 끝나는 TC 가 앞 순서면, 병렬화 후에도 결과 순서는 입력 순서
        import qapilot.agents.root_cause_agent as rca
        monkeypatch.setattr(rca, "RootCauseAgent",
                            _fake_rc_agent_cls({"TS-X-TC-01": 0.05, "TS-X-TC-02": 0.0}))
        state = {
            "trace_id": "t-test",
            "cross_check_results": [
                {"tc_id": "TS-X-TC-01", "has_mismatch": True, "mismatches": [{"x": 1}]},
                {"tc_id": "TS-X-TC-02", "has_mismatch": True, "mismatches": [{"x": 1}]},
                {"tc_id": "TS-X-TC-03", "has_mismatch": False},
            ],
            "ui_results": [],
            "agent_logs": [],
        }
        out = await pl._root_cause(state)
        rcs = out["root_cause_results"]
        assert [r.get("tc_id") for r in rcs] == ["TS-X-TC-01", "TS-X-TC-02"]
        assert all(r.get("candidates") for r in rcs)
        assert len(out["agent_logs"]) == 2

    @pytest.mark.asyncio
    async def test_one_exception_isolated(self, monkeypatch):
        import qapilot.agents.root_cause_agent as rca
        monkeypatch.setattr(rca, "RootCauseAgent",
                            _fake_rc_agent_cls({}, fail_tc="TS-X-TC-01"))
        state = {
            "trace_id": "t-test",
            "cross_check_results": [
                {"tc_id": "TS-X-TC-01", "has_mismatch": True, "mismatches": [{"x": 1}]},
                {"tc_id": "TS-X-TC-02", "has_mismatch": True, "mismatches": [{"x": 1}]},
            ],
            "ui_results": [],
            "agent_logs": [],
        }
        out = await pl._root_cause(state)
        rcs = out["root_cause_results"]
        assert "RootCause skip" in str(rcs[0].get("error"))
        assert rcs[1].get("candidates")  # 옆 TC 는 정상

    @pytest.mark.asyncio
    async def test_deterministic_classification_bypasses_llm(self, monkeypatch):
        import qapilot.agents.root_cause_agent as rca

        class _Boom:
            def __init__(self, trace_id=None):
                raise AssertionError("LLM agent must not be constructed")
        monkeypatch.setattr(rca, "RootCauseAgent", _Boom)
        # text-selector 류 → TEST_DEFECT_UNVERIFIABLE (결정적) — LLM 미호출
        ui = {"tc_id": "TS-X-TC-01", "status": "fail", "steps": [
            {"step_no": 1, "action": "assert", "status": "fail",
             "selector": "인증 실패 처리되고 자동 로그아웃된다.",
             "selector_type": "text", "error": "TOOL_UI_ASSERTION_FAIL"},
        ]}
        state = {
            "trace_id": "t-test",
            "cross_check_results": [
                {"tc_id": "TS-X-TC-01", "has_mismatch": True, "ui_failed": True},
            ],
            "ui_results": [ui],
            "agent_logs": [],
        }
        out = await pl._root_cause(state)
        assert out["root_cause_results"][0]["classified_deterministic"] is True


class TestFixRecommendParallel:
    @pytest.mark.asyncio
    async def test_mixed_deterministic_and_llm_order(self, monkeypatch):
        import qapilot.agents.fix_recommender_agent as fra

        class _Fake:
            def __init__(self, trace_id=None):
                pass

            async def run(self, agent_input):
                tc = agent_input.params.get("tc_id")
                return _FakeOutput({"fix_results": [
                    {"tc_id": tc, "suggestions": [{"description": f"fix-{tc}"}]}
                ]})
        monkeypatch.setattr(fra, "FixRecommenderAgent", _Fake)
        state = {
            "trace_id": "t-test",
            "root_cause_results": [
                {"tc_id": "A", "classified_deterministic": True,
                 "category": "ENV_TIMEOUT", "candidates": []},
                {"tc_id": "B", "candidates": [{"cause": "x"}]},
                {"tc_id": "C", "candidates": [{"cause": "y"}]},
            ],
            "agent_logs": [],
        }
        out = await pl._fix_recommend(state)
        frs = out["fix_results"]
        assert [f.get("tc_id") for f in frs] == ["A", "B", "C"]
        assert "환경" in frs[0]["suggestions"][0]["description"]
        assert frs[1]["suggestions"][0]["description"] == "fix-B"


class TestCcApiModeSkip:
    @pytest.mark.asyncio
    async def test_agent_not_called_for_api_mode(self, monkeypatch):
        import qapilot.agents.cross_check_agent as cca

        class _Boom:
            def __init__(self, trace_id=None):
                raise AssertionError("CrossCheckAgent must not be constructed for api-mode")
        monkeypatch.setattr(cca, "CrossCheckAgent", _Boom)
        state = {
            "trace_id": "t-test",
            "ui_results": [{"tc_id": "T1", "verify_mode": "api", "status": None,
                            "steps": [], "summary": "API-mode 검증: 기대 [201] — 실제 201"}],
            "api_results": [{"tc_id": "T1", "verify_mode": "api", "verdict": "pass",
                             "calls": [{"status_code": 201}], "error_calls": 0}],
            "db_results": [{"tc_id": "T1"}],
            "scenarios": [],
            "agent_logs": [],
        }
        out = await pl._cross_check(state)
        ccs = out["cross_check_results"]
        assert len(ccs) == 1
        assert ccs[0]["api_exec_verdict"] == "pass"
        assert ccs[0]["has_mismatch"] is False
        assert "API-mode" in ccs[0]["summary"]

    @pytest.mark.asyncio
    async def test_api_mode_fail_marks_mismatch(self, monkeypatch):
        import qapilot.agents.cross_check_agent as cca

        class _Boom:
            def __init__(self, trace_id=None):
                raise AssertionError("must not be called")
        monkeypatch.setattr(cca, "CrossCheckAgent", _Boom)
        state = {
            "trace_id": "t-test",
            "ui_results": [{"tc_id": "T1", "verify_mode": "api", "status": None, "steps": []}],
            "api_results": [{"tc_id": "T1", "verify_mode": "api", "verdict": "fail",
                             "calls": [{"status_code": 404}], "error_calls": 1}],
            "db_results": [{"tc_id": "T1"}],
            "scenarios": [],
            "agent_logs": [],
        }
        out = await pl._cross_check(state)
        assert out["cross_check_results"][0]["has_mismatch"] is True
        assert out["has_mismatch"] is True
