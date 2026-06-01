"""EvidenceModule 단위 테스트."""

import pytest
import json
from pathlib import Path
from unittest.mock import patch

from qapilot.modules.evidence_module import EvidenceModule


@pytest.fixture
def module(tmp_path):
    """임시 디렉토리를 EVIDENCE_BASE_DIR로 사용."""
    with patch("qapilot.modules.evidence_module.EVIDENCE_BASE_DIR", tmp_path):
        yield EvidenceModule()


@pytest.mark.asyncio
async def test_save_creates_evidence_file(module, tmp_path):
    """save() 호출 시 evidence.json 파일 생성 확인."""
    with patch("qapilot.modules.evidence_module.EVIDENCE_BASE_DIR", tmp_path):
        await module.save(
            trace_id="trace-001",
            tc_id="TC-001",
            evidence={
                "screenshots": [],
                "api_logs": [],
                "sql_logs": [],
                "console_logs": [],
            }
        )

    evidence_file = tmp_path / "trace-001" / "TC-001" / "evidence.json"
    assert evidence_file.exists()


@pytest.mark.asyncio
async def test_save_stores_correct_data(module, tmp_path):
    """save() 호출 시 올바른 데이터 저장 확인."""
    evidence = {
        "screenshots": ["/tmp/shot1.png"],
        "api_logs": [{"url": "/api/login", "status_code": 200}],
        "sql_logs": [],
        "console_logs": ["console error: timeout"],
    }

    with patch("qapilot.modules.evidence_module.EVIDENCE_BASE_DIR", tmp_path):
        await module.save(trace_id="trace-001", tc_id="TC-001", evidence=evidence)

    evidence_file = tmp_path / "trace-001" / "TC-001" / "evidence.json"
    data = json.loads(evidence_file.read_text())

    assert data["trace_id"] == "trace-001"
    assert data["tc_id"] == "TC-001"
    assert data["screenshots"] == ["/tmp/shot1.png"]
    assert data["api_logs"][0]["url"] == "/api/login"
    assert "saved_at" in data


@pytest.mark.asyncio
async def test_get_returns_saved_evidence(module, tmp_path):
    """get() 호출 시 저장된 증적 반환 확인."""
    evidence = {"screenshots": [], "api_logs": [], "sql_logs": [], "console_logs": []}

    with patch("qapilot.modules.evidence_module.EVIDENCE_BASE_DIR", tmp_path):
        await module.save(trace_id="trace-001", tc_id="TC-001", evidence=evidence)
        result = await module.get(trace_id="trace-001", tc_id="TC-001")

    assert result is not None
    assert result["trace_id"] == "trace-001"
    assert result["tc_id"] == "TC-001"


@pytest.mark.asyncio
async def test_get_returns_none_when_not_found(module, tmp_path):
    """존재하지 않는 증적 조회 시 None 반환 확인."""
    with patch("qapilot.modules.evidence_module.EVIDENCE_BASE_DIR", tmp_path):
        result = await module.get(trace_id="trace-999", tc_id="TC-999")

    assert result is None