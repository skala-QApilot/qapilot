"""_tv_generate_codebase_aware: Option A (TC별 좁은 쿼리 문서 재검색) 검증.

- TC.api/name/req_id 기반으로 DomainKnowledgeTool을 재호출해 tc_doc_refs를 얻고,
  agent context에 전달하며 결과 TC에 _doc_refs로 부착한다.
- _save_experiment_scenarios가 _doc_refs를 pop해 TC별 provenance.retrieved_docs로
  override한다(TS-level retrieved_docs보다 우선).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

from qapilot.orchestrator import pipeline as pipeline_module


def _async_return(value):
    async def _coro():
        return value
    return _coro()


def _base_state():
    return {
        "trace_id": "trace-x",
        "service_id": "svc-1",
        "requirements": [{"req_id": "FR-AUTH-01", "content": "이메일/비밀번호로 가입한다"}],
        "tc_by_ts_index": {
            0: [
                {
                    "name": "정상 가입",
                    "given": "유효한 입력",
                    "when": "회원가입 요청",
                    "then": "201 반환",
                    "values": [],
                    "tags": [],
                    "req_id": "FR-AUTH-01",
                    "api": "POST /api/auth/signup",
                    "depends_on": [],
                },
            ]
        },
        "agent_logs": [],
        "scan_result": {},
    }


def test_tv_generate_codebase_aware_attaches_tc_doc_refs():
    state = _base_state()

    fake_search_output = MagicMock()
    fake_search_output.result = {
        "rules": [
            {"content": "회원가입은 이메일 중복을 허용하지 않는다.", "source": "정책문서.md", "similarity_score": 0.71},
        ]
    }
    fake_doc_tool = MagicMock()
    fake_doc_tool.run = MagicMock(return_value=_async_return(fake_search_output))

    fake_metadata = MagicMock()
    fake_metadata.model_dump.return_value = {"agent": "tv_codebase_aware"}
    fake_agent_output = MagicMock()
    fake_agent_output.result = {
        "values": [{"field": "email", "value": "new@test.com", "type": "string", "purpose": "신규", "source": "llm"}],
        "claims": {},
        "validation_passed": True,
    }
    fake_agent_output.metadata = fake_metadata
    fake_agent = MagicMock()
    fake_agent.run = MagicMock(return_value=_async_return(fake_agent_output))

    with patch.object(pipeline_module, "load_trace", return_value={"service_id": "svc-1"}), \
         patch("qapilot.shared.scan_storage.load_metadata_index", return_value={"some": "index"}), \
         patch("qapilot.shared.metadata_filters.pick_table_for_tc", return_value=None), \
         patch("qapilot.shared.metadata_filters.pick_source_files_for_tc", return_value=[]), \
         patch("qapilot.tools.domain_knowledge.DomainKnowledgeTool", return_value=fake_doc_tool), \
         patch("qapilot.agents.tv_codebase_aware_agent.TVFromCodebaseAgent", return_value=fake_agent):
        result = asyncio.run(pipeline_module._tv_generate_codebase_aware(state))  # type: ignore[arg-type]

    tc = result["tc_by_ts_index"][0][0]
    assert tc["_doc_refs"] == [
        {"content": "회원가입은 이메일 중복을 허용하지 않는다.", "source": "정책문서.md", "score": 0.71},
    ]

    # agent context에 tc_doc_refs가 전달되었는지 확인
    call_args, call_kwargs = fake_agent.run.call_args
    sent_input = call_args[0] if call_args else call_kwargs["agent_input"]
    assert sent_input.context["tc_doc_refs"] == tc["_doc_refs"]


def test_save_experiment_scenarios_overrides_provenance_retrieved_docs_with_doc_refs(tmp_path):
    ts_retrieved_docs = [
        {"content": "TS-level 공통 문서", "source": "API명세서.md", "score": 0.61},
    ]
    tc_doc_refs = [
        {"content": "TC별 재검색 문서", "source": "정책문서.md", "score": 0.71},
    ]

    state = {
        "trace_id": "trace-x",
        "qapilot_dir": str(tmp_path),
        "run_options": {"trigger": "init"},
        "ts_list": [
            {"name": "회원가입", "description": "회원가입 기능", "requirements": ["FR-AUTH-01"]},
        ],
        "tc_by_ts_index": {
            0: [
                {
                    "name": "정상 가입",
                    "given": "유효한 입력",
                    "when": "회원가입 요청",
                    "then": "201 반환",
                    "values": [],
                    "tags": [],
                    "req_id": "FR-AUTH-01",
                    "api": "POST /api/auth/signup",
                    "depends_on": [],
                    "_doc_refs": tc_doc_refs,
                },
            ]
        },
        "tc_provenance_by_index": {
            0: {
                "trace_id": "trace-x",
                "generated_at": "2026-06-10T00:00:00Z",
                "source": "doc_search",
                "analysis": [],
                "metrics": {"doc_search_query": "회원가입 auth FR-AUTH-01"},
                "retrieved_docs": ts_retrieved_docs,
            }
        },
    }

    with patch.object(pipeline_module, "load_trace", return_value={}):
        result = asyncio.run(pipeline_module._save_experiment_scenarios(state))  # type: ignore[arg-type]

    assert result["status"] == "completed"

    tc1 = json.loads((tmp_path / "scenarios" / "TS-001" / "TC-01" / "latest.json").read_text(encoding="utf-8"))
    assert tc1["provenance"]["retrieved_docs"] == tc_doc_refs
    assert "_doc_refs" not in tc1

    metadata = json.loads((tmp_path / "scenarios" / "TS-001" / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["tc_generation_context"]["retrieved_docs"] == ts_retrieved_docs
