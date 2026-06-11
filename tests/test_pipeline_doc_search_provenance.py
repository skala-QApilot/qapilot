"""_tc_generate_doc_search: doc_search_failed 로깅 + provenance.retrieved_docs 검증.

- DomainKnowledgeTool.run()이 예외를 던지면 raw_rules=[] 로 graceful 처리하되,
  logger.warning("doc_search_failed", ...)가 호출되어야 한다 (이전에는 silent swallow).
- 정상 검색 시 provenance_by_index[idx]["retrieved_docs"]에 검색된 chunk가 담겨야 한다.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from qapilot.orchestrator import pipeline as pipeline_module


def _async_return(value):
    async def _coro():
        return value
    return _coro()


def _fake_tc_agent_output(test_cases=None, analysis=None):
    fake_metadata = MagicMock()
    fake_metadata.model = "gpt-test"
    fake_metadata.cost_usd = 0.001
    fake_metadata.model_dump.return_value = {"agent": "tc_doc_search", "cost_usd": 0.001}
    fake_output = MagicMock()
    fake_output.result = {"test_cases": test_cases or [], "analysis": analysis or []}
    fake_output.metadata = fake_metadata
    return fake_output


def _base_state():
    return {
        "trace_id": "trace-x",
        "run_options": {"tc_target_ts_ids": ["TS-001"]},
        "service_id": "svc-1",
        "ts_list": [{"name": "회원가입", "domain_area": "auth", "requirements": ["FR-AUTH-01"]}],
        "requirements": [{"req_id": "FR-AUTH-01", "content": "이메일/비밀번호로 가입한다"}],
        "agent_logs": [],
    }


def test_doc_search_failed_logs_warning_on_exception():
    state = _base_state()

    fake_tool = MagicMock()
    fake_tool.run = MagicMock(side_effect=RuntimeError("qdrant down"))

    fake_agent = MagicMock()
    fake_agent.run = MagicMock(return_value=_async_return(_fake_tc_agent_output()))

    with patch("qapilot.tools.domain_knowledge.DomainKnowledgeTool", return_value=fake_tool), \
         patch("qapilot.agents.tc_doc_search_agent.TCFromDocsAgent", return_value=fake_agent), \
         patch.object(pipeline_module, "logger") as mock_logger:
        result = asyncio.run(pipeline_module._tc_generate_doc_search(state))  # type: ignore[arg-type]

    warning_calls = [c for c in mock_logger.warning.call_args_list if c.args and c.args[0] == "doc_search_failed"]
    assert len(warning_calls) == 1
    assert warning_calls[0].kwargs["error"] == "qdrant down"

    # 예외 발생 시 retrieved_docs는 빈 리스트
    assert result["tc_provenance_by_index"][0]["retrieved_docs"] == []


def test_doc_search_success_records_retrieved_docs_in_provenance():
    state = _base_state()

    fake_search_output = MagicMock()
    fake_search_output.result = {
        "rules": [
            {"content": "POST /api/auth/signup ...", "source": "API명세서.md", "similarity_score": 0.61},
        ]
    }
    fake_tool = MagicMock()
    fake_tool.run = MagicMock(return_value=_async_return(fake_search_output))

    fake_agent = MagicMock()
    fake_agent.run = MagicMock(return_value=_async_return(_fake_tc_agent_output(
        test_cases=[{"tc_id": "TS-001-TC-01", "name": "정상 가입"}],
    )))

    with patch("qapilot.tools.domain_knowledge.DomainKnowledgeTool", return_value=fake_tool), \
         patch("qapilot.agents.tc_doc_search_agent.TCFromDocsAgent", return_value=fake_agent):
        result = asyncio.run(pipeline_module._tc_generate_doc_search(state))  # type: ignore[arg-type]

    provenance = result["tc_provenance_by_index"][0]
    assert provenance["retrieved_docs"] == [
        {"content": "POST /api/auth/signup ...", "source": "API명세서.md", "score": 0.61},
    ]
    assert provenance["metrics"]["doc_search_results_count"] == 1
    assert "회원가입" in provenance["metrics"]["doc_search_query"]
