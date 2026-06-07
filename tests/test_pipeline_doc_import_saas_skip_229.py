"""_doc_import SaaS 흐름 시 (2) cfg.root/docs 분기 skip 검증 (#229).

SaaS service_id 있고 (1) DB+S3 import 1건 이상 성공 → (2) cfg.root/docs CLI 호환
분기 발동 안 함. "." fallback + qapilot/docs/ 오염 차단.

mock 대상: 함수 내부 import 경로 직접 patch.
- qapilot.db.domain_reader.list_latest_domain_documents
- qapilot.storage.s3_client.get_object
- qapilot.shared.config.load_config
- qapilot.tools.domain_knowledge.DomainKnowledgeTool
- qapilot.tools.domain_knowledge._store.VectorStore

_import_path 는 _doc_import 내부 nested function 이라 patch 불가 — DomainKnowledgeTool.run
mock 으로 우회 + params["file_path"] 캡쳐.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def fake_state(tmp_path):
    return {
        "trace_id": "trace-x",
        "qapilot_dir": str(tmp_path),
        "run_options": {"trigger": "init"},
    }


def _async_return(value):
    async def _coro():
        return value
    return _coro()


def _capture_tool_run(captured: list):
    """DomainKnowledgeTool.run mock — params['file_path'] 캡쳐."""
    async def _run(tool_input):
        params = tool_input.params if hasattr(tool_input, "params") else tool_input.get("params", {})
        file_path = params.get("file_path", "")
        captured.append(str(file_path))
        return MagicMock()
    return _run


@pytest.mark.asyncio
async def test_doc_import_saas_skips_cfg_root_when_imported(fake_state, tmp_path):
    """service_id + (1) SaaS import 1건 이상 → (2) cfg.root/docs skip."""
    # cfg.root/docs 에 파일 배치 (정상이면 import 안 되어야 함)
    cfg_docs = tmp_path / "cfg_root" / "docs"
    cfg_docs.mkdir(parents=True)
    (cfg_docs / "should-not-import.md").write_text("# leak", encoding="utf-8")

    fake_cfg = MagicMock()
    fake_cfg.project.root = str(tmp_path / "cfg_root")
    fake_cfg.project.repo_path = None

    captured: list[str] = []

    from qapilot.orchestrator import pipeline as pipeline_module

    with patch.object(pipeline_module, "load_trace", return_value={"service_id": "svc-1"}), \
         patch("qapilot.db.domain_reader.list_latest_domain_documents", return_value=[
             {"filename": "PRD.md", "s3_key": "services/svc-1/domain/f-1/v1/PRD.md", "mime_type": "text/markdown"},
         ]), \
         patch("qapilot.storage.s3_client.get_object", return_value=b"# SaaS PRD\n"), \
         patch("qapilot.shared.config.load_config", return_value=fake_cfg), \
         patch("qapilot.tools.domain_knowledge._store.VectorStore.collection_exists", return_value=_async_return(True)), \
         patch("qapilot.tools.domain_knowledge.DomainKnowledgeTool.run", side_effect=_capture_tool_run(captured)):
        result = await pipeline_module._doc_import(fake_state)

    # SaaS PRD.md 1건만 import — should-not-import.md 발동 안 함
    saas_imports = [p for p in captured if "PRD.md" in p]
    cfg_imports = [p for p in captured if "should-not-import.md" in p]
    assert len(saas_imports) == 1, f"SaaS PRD 1건 기대, 실제 {captured}"
    assert len(cfg_imports) == 0, f"(2) cfg.root 발동 — fix 미적용 ({captured})"
    assert result == {}


@pytest.mark.asyncio
async def test_doc_import_cli_falls_through_to_cfg_root(fake_state, tmp_path):
    """service_id 없는 CLI 흐름 → (2) cfg.root/docs 정상 진행 (회귀 0)."""
    cfg_docs = tmp_path / "cfg_root" / "docs"
    cfg_docs.mkdir(parents=True)
    (cfg_docs / "PRD.md").write_text("# CLI PRD\n", encoding="utf-8")

    fake_cfg = MagicMock()
    fake_cfg.project.root = str(tmp_path / "cfg_root")
    fake_cfg.project.repo_path = None

    captured: list[str] = []

    from qapilot.orchestrator import pipeline as pipeline_module

    with patch.object(pipeline_module, "load_trace", return_value={}), \
         patch("qapilot.shared.config.load_config", return_value=fake_cfg), \
         patch("qapilot.tools.domain_knowledge._store.VectorStore.collection_exists", return_value=_async_return(True)), \
         patch("qapilot.tools.domain_knowledge.DomainKnowledgeTool.run", side_effect=_capture_tool_run(captured)):
        await pipeline_module._doc_import(fake_state)

    cli_imports = [p for p in captured if "PRD.md" in p]
    assert len(cli_imports) == 1, f"CLI cfg.root/docs/PRD.md import 기대, 실제 {captured}"


@pytest.mark.asyncio
async def test_doc_import_saas_zero_imports_falls_through(fake_state, tmp_path):
    """service_id 있으나 (1) import 0건 (DB 비었음) → (2) cfg.root/docs fallback."""
    cfg_docs = tmp_path / "cfg_root" / "docs"
    cfg_docs.mkdir(parents=True)
    (cfg_docs / "fallback_PRD.md").write_text("# fallback\n", encoding="utf-8")

    fake_cfg = MagicMock()
    fake_cfg.project.root = str(tmp_path / "cfg_root")
    fake_cfg.project.repo_path = None

    captured: list[str] = []

    from qapilot.orchestrator import pipeline as pipeline_module

    with patch.object(pipeline_module, "load_trace", return_value={"service_id": "svc-1"}), \
         patch("qapilot.db.domain_reader.list_latest_domain_documents", return_value=[]), \
         patch("qapilot.shared.config.load_config", return_value=fake_cfg), \
         patch("qapilot.tools.domain_knowledge._store.VectorStore.collection_exists", return_value=_async_return(True)), \
         patch("qapilot.tools.domain_knowledge.DomainKnowledgeTool.run", side_effect=_capture_tool_run(captured)):
        await pipeline_module._doc_import(fake_state)

    cfg_imports = [p for p in captured if "fallback_PRD.md" in p]
    assert len(cfg_imports) == 1, f"SaaS 0건 → (2) fallback 기대, 실제 {captured}"


@pytest.mark.asyncio
async def test_doc_import_saas_s3_miss_falls_through(fake_state, tmp_path):
    """service_id + DB 결과 있으나 S3 miss → import 0건 → (2) fallback."""
    cfg_docs = tmp_path / "cfg_root" / "docs"
    cfg_docs.mkdir(parents=True)
    (cfg_docs / "fallback_PRD.md").write_text("# fallback\n", encoding="utf-8")

    fake_cfg = MagicMock()
    fake_cfg.project.root = str(tmp_path / "cfg_root")
    fake_cfg.project.repo_path = None

    captured: list[str] = []

    from qapilot.orchestrator import pipeline as pipeline_module

    with patch.object(pipeline_module, "load_trace", return_value={"service_id": "svc-1"}), \
         patch("qapilot.db.domain_reader.list_latest_domain_documents", return_value=[
             {"filename": "PRD.md", "s3_key": "missing/key", "mime_type": "text/markdown"},
         ]), \
         patch("qapilot.storage.s3_client.get_object", return_value=None), \
         patch("qapilot.shared.config.load_config", return_value=fake_cfg), \
         patch("qapilot.tools.domain_knowledge._store.VectorStore.collection_exists", return_value=_async_return(True)), \
         patch("qapilot.tools.domain_knowledge.DomainKnowledgeTool.run", side_effect=_capture_tool_run(captured)):
        await pipeline_module._doc_import(fake_state)

    cfg_imports = [p for p in captured if "fallback_PRD.md" in p]
    assert len(cfg_imports) == 1, f"S3 miss → (2) fallback 기대, 실제 {captured}"
