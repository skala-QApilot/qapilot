"""_save_experiment_scenarios: tc_generation_context 구성 + TC provenance 병합 검증.

- TS-level metadata.json에 query/retrieved_docs/codebase_refs(tc_generation_context)가 기록된다.
- 각 TC의 latest.json provenance에 retrieved_docs(공통)와 codebase_refs(TC별 _codebase_refs)가 담긴다.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

from qapilot.orchestrator import pipeline as pipeline_module


def test_save_experiment_scenarios_builds_tc_generation_context(tmp_path):
    retrieved_docs = [
        {"content": "POST /api/auth/signup ...", "source": "API명세서.md", "score": 0.61},
    ]
    codebase_ref = {"file": "app/routers/auth.py", "line_start": 10, "line_end": 25, "content": "def signup(...): ..."}

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
                    "_codebase_refs": [codebase_ref],
                },
                {
                    "name": "중복 이메일",
                    "given": "이미 가입된 이메일",
                    "when": "회원가입 요청",
                    "then": "409 반환",
                    "values": [],
                    "tags": [],
                    "req_id": "FR-AUTH-01",
                    "api": "POST /api/auth/signup",
                    "depends_on": [],
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
                "retrieved_docs": retrieved_docs,
            }
        },
    }

    with patch.object(pipeline_module, "load_trace", return_value={}):
        result = asyncio.run(pipeline_module._save_experiment_scenarios(state))  # type: ignore[arg-type]

    assert result["status"] == "completed"

    metadata = json.loads((tmp_path / "scenarios" / "TS-001" / "metadata.json").read_text(encoding="utf-8"))
    ctx = metadata["tc_generation_context"]
    assert ctx["query"] == "회원가입 auth FR-AUTH-01"
    assert ctx["retrieved_docs"] == retrieved_docs
    assert ctx["codebase_refs"] == [codebase_ref]

    tc1 = json.loads((tmp_path / "scenarios" / "TS-001" / "TC-01" / "latest.json").read_text(encoding="utf-8"))
    assert tc1["provenance"]["retrieved_docs"] == retrieved_docs
    assert tc1["provenance"]["codebase_refs"] == [codebase_ref]
    assert "_codebase_refs" not in tc1

    tc2 = json.loads((tmp_path / "scenarios" / "TS-001" / "TC-02" / "latest.json").read_text(encoding="utf-8"))
    assert tc2["provenance"]["retrieved_docs"] == retrieved_docs
    assert "codebase_refs" not in tc2["provenance"]
