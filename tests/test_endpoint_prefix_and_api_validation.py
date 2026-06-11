"""격차 4 — endpoint prefix 이중 결합 방지 + TC.api 환각 검증.

배경:
- `_apply_router_prefixes` 가 decorator path 에 이미 prefix 가 포함된 SUT 에서
  "/api/auth" + "/api/auth/signup" = "/api/auth/api/auth/signup" 이중 prefix 생성.
- `_get_full_ep_path` 도 동일 패턴 재결합 가능.
- prd_only_experiment 의 TC 생성 (`_tc_generate_doc_search`) 은 endpoint 카탈로그를
  안 받아 LLM 이 PRD 텍스트에서 SUT 에 없는 api (PUT /profile,
  PATCH /api/contracts/...) 를 창작 — 검증 0.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from qapilot.agents.scenario_generator.agent import ScenarioGeneratorAgent
from qapilot.orchestrator.pipeline import _validate_tc_apis_against_scan
from qapilot.tools.git_codebase_scanner_tool import GitCodebaseScannerTool


class TestApplyRouterPrefixes:
    def test_plain_subpath_gets_prefix(self):
        fi = {"path": "app/routers/auth.py", "endpoints": [{"path": "/signup", "method": "POST"}]}
        GitCodebaseScannerTool._apply_router_prefixes([fi], {"auth": "/api/auth"})
        assert fi["endpoints"][0]["path"] == "/api/auth/signup"

    def test_already_prefixed_subpath_not_doubled(self):
        fi = {"path": "app/routers/auth.py", "endpoints": [{"path": "/api/auth/signup", "method": "POST"}]}
        GitCodebaseScannerTool._apply_router_prefixes([fi], {"auth": "/api/auth"})
        assert fi["endpoints"][0]["path"] == "/api/auth/signup"

    def test_exact_prefix_match_not_doubled(self):
        fi = {"path": "app/routers/orders.py", "endpoints": [{"path": "/api/orders", "method": "POST"}]}
        GitCodebaseScannerTool._apply_router_prefixes([fi], {"orders": "/api/orders"})
        assert fi["endpoints"][0]["path"] == "/api/orders"

    def test_empty_path_becomes_prefix(self):
        fi = {"path": "app/routers/orders.py", "endpoints": [{"path": "", "method": "POST"}]}
        GitCodebaseScannerTool._apply_router_prefixes([fi], {"orders": "/api/orders"})
        assert fi["endpoints"][0]["path"] == "/api/orders"

    def test_similar_but_distinct_path_still_prefixed(self):
        # "/api/authority" 는 "/api/auth" prefix 로 시작하는 path 가 아님 — 결합 유지
        fi = {"path": "app/routers/auth.py", "endpoints": [{"path": "/api/authority", "method": "GET"}]}
        GitCodebaseScannerTool._apply_router_prefixes([fi], {"auth": "/api/auth"})
        assert fi["endpoints"][0]["path"] == "/api/auth/api/authority"


class TestGetFullEpPath:
    def _agent(self) -> ScenarioGeneratorAgent:
        agent = ScenarioGeneratorAgent.__new__(ScenarioGeneratorAgent)
        agent.logger = MagicMock()
        return agent

    def test_relative_path_combined(self):
        ep = {"file": "app/routers/auth.py", "path": "/signup", "method": "POST"}
        assert self._agent()._get_full_ep_path(ep) == "POST /api/auth/signup"

    def test_already_full_path_not_doubled(self):
        ep = {"file": "app/routers/auth.py", "path": "/api/auth/signup", "method": "POST"}
        assert self._agent()._get_full_ep_path(ep) == "POST /api/auth/signup"


class TestValidateTcApisAgainstScan:
    CATALOG = [
        "POST /api/auth/signup",
        "POST /api/auth/login",
        "PATCH /api/orders/{order_id}/cancel",
    ]

    def test_valid_api_kept(self):
        tcs = [{"name": "t", "api": "POST /api/auth/signup"}]
        _validate_tc_apis_against_scan(tcs, self.CATALOG, "trace")
        assert tcs[0]["api"] == "POST /api/auth/signup"

    def test_hallucinated_api_nulled(self):
        tcs = [{"name": "t", "api": "PUT /profile"},
               {"name": "t2", "api": "PATCH /api/contracts/{order_id}/terminate"}]
        _validate_tc_apis_against_scan(tcs, self.CATALOG, "trace")
        assert tcs[0]["api"] is None
        assert tcs[1]["api"] is None

    def test_param_name_mismatch_corrected(self):
        tcs = [{"name": "t", "api": "PATCH /api/orders/{id}/cancel"}]
        _validate_tc_apis_against_scan(tcs, self.CATALOG, "trace")
        assert tcs[0]["api"] == "PATCH /api/orders/{order_id}/cancel"

    def test_null_string_normalized(self):
        tcs = [{"name": "t", "api": "null"}]
        _validate_tc_apis_against_scan(tcs, self.CATALOG, "trace")
        assert tcs[0]["api"] is None

    def test_empty_catalog_no_change(self):
        tcs = [{"name": "t", "api": "PUT /profile"}]
        _validate_tc_apis_against_scan(tcs, [], "trace")
        assert tcs[0]["api"] == "PUT /profile"
