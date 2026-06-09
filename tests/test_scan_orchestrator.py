"""scan_all_metadata orchestrator 단위 테스트 — PoC 10 (본인 영역).

mock extractor + mock upsert 으로 fan-out 호출 + 결과 집계 검증.
실 환경 통합 검증은 manual integration test 으로 별도.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qapilot.scan.orchestrator import (
    ScanAllResult,
    _infer_route_from_path,
    scan_all_metadata,
)
from qapilot.shared.metadata_schemas import (
    BackendSchemasIndex,
    DbModel,
    ExtractedFrom,
    FrontendRoutesIndex,
    InputElement,
    RequestSchema,
    ResponseSchema,
    RouteRecord,
    SchemaField,
    StatusCode,
    SutTestsPatternsIndex,
    TestPatternRecord,
)

SID = "service-aaaa"
COMMIT = "f" * 40


def _make_extracted_from(file_name: str = "x.py") -> ExtractedFrom:
    return ExtractedFrom(file=file_name, line_start=1, line_end=1, commit_sha=COMMIT)


def _make_route_record() -> RouteRecord:
    return RouteRecord(
        extracted_from=_make_extracted_from("router.js"),
        confidence=1.0, extraction_method="ast",
        path="/x",
    )


def _make_schema_field(name: str = "f") -> SchemaField:
    return SchemaField(
        extracted_from=_make_extracted_from(),
        confidence=1.0, extraction_method="ast",
        name=name, type="str",
    )


def _make_pattern_record(name: str = "test_x") -> TestPatternRecord:
    ef = _make_extracted_from(f"{name}.py")
    return TestPatternRecord(
        extracted_from=ef,
        confidence=1.0, extraction_method="ast",
        pattern_kind="unknown", framework="pytest",
        file="x.py", line_start=1, line_end=1,
        snippet=f"def {name}(): pass", purpose=name,
    )


# ────────────────────────────────────────────────────────────────────────
# _infer_route_from_path
# ────────────────────────────────────────────────────────────────────────

def test_infer_route_from_pages_dir(tmp_path):
    f = tmp_path / "frontend/src/pages/Signup.vue"
    f.parent.mkdir(parents=True)
    f.write_text("x")
    assert _infer_route_from_path(f, tmp_path) == "/signup"


def test_infer_route_from_components_dir(tmp_path):
    f = tmp_path / "frontend/src/components/Sidebar.vue"
    f.parent.mkdir(parents=True)
    f.write_text("x")
    assert _infer_route_from_path(f, tmp_path) == "/_components/sidebar"


# ────────────────────────────────────────────────────────────────────────
# scan_all_metadata — 빈 디렉토리 (모든 영역 빈 결과)
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@patch("qapilot.scan.orchestrator.dump_source_to_s3")
async def test_scan_empty_repo(mock_dump, tmp_path):
    """빈 repo → 모든 영역 0 records, 에러 0, upserted False."""
    mock_dump.return_value = MagicMock(files_walked=0, files_uploaded=0,
                                        files_skipped_cache=0, files_failed=0)
    result = await scan_all_metadata(SID, tmp_path, COMMIT)
    assert result.selectors_count == 0
    assert result.routes_count == 0
    assert result.schemas_request_count == 0
    assert result.patterns_count == 0
    assert result.errors == []


# ────────────────────────────────────────────────────────────────────────
# scan_all_metadata — 각 영역 mock
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@patch("qapilot.scan.orchestrator.upsert_metadata_index", return_value=True)
@patch("qapilot.scan.orchestrator.dump_source_to_s3")
@patch("qapilot.scan.orchestrator.extract_routes_from_router_file")
async def test_scan_routes_upserts(mock_extract, mock_dump, mock_upsert, tmp_path):
    """router/index.js 가 있고 routes 추출되면 upsert + count."""
    router = tmp_path / "frontend/src/router/index.js"
    router.parent.mkdir(parents=True)
    router.write_text("export default {}")
    mock_dump.return_value = MagicMock(files_walked=1, files_uploaded=1,
                                        files_skipped_cache=0, files_failed=0)
    mock_extract.return_value = [_make_route_record(), _make_route_record()]

    result = await scan_all_metadata(SID, tmp_path, COMMIT,
                                      skip_source_dump=False,
                                      skip_llm_classification=True)

    assert result.routes_count == 2
    assert result.routes_upserted is True


@pytest.mark.asyncio
@patch("qapilot.scan.orchestrator.upsert_metadata_index", return_value=True)
@patch("qapilot.scan.orchestrator.dump_source_to_s3")
@patch("qapilot.scan.orchestrator.extract_backend_schemas_from_file")
async def test_scan_schemas_aggregates_files(mock_extract, mock_dump,
                                              mock_upsert, tmp_path):
    """여러 .py 파일의 request/response/db_models 가 합쳐짐."""
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "schemas.py").write_text("# pydantic")
    (tmp_path / "backend" / "models.py").write_text("# sqlalchemy")
    mock_dump.return_value = MagicMock(files_walked=2, files_uploaded=2,
                                        files_skipped_cache=0, files_failed=0)
    req_schema = RequestSchema(fields=[_make_schema_field("name")])
    resp_schema = ResponseSchema(status_codes=[
        StatusCode(code=200, description="OK"),
    ])
    db_model = DbModel(
        extracted_from=_make_extracted_from("models.py"),
        confidence=1.0, extraction_method="ast",
        table_name="t", columns=[],
    )
    mock_extract.side_effect = [
        ({"R1": req_schema}, {"O1": resp_schema}, {}),
        ({}, {}, {"M1": db_model}),
    ]

    result = await scan_all_metadata(SID, tmp_path, COMMIT,
                                      skip_llm_classification=True)

    assert result.schemas_request_count == 1
    assert result.schemas_response_count == 1
    assert result.db_models_count == 1
    assert result.schemas_upserted is True


@pytest.mark.asyncio
@patch("qapilot.scan.orchestrator.upsert_metadata_index", return_value=True)
@patch("qapilot.scan.orchestrator.dump_source_to_s3")
@patch("qapilot.scan.orchestrator.extract_patterns_from_pytest_file")
async def test_scan_patterns_only_collects_test_files(mock_extract, mock_dump,
                                                       mock_upsert, tmp_path):
    """test_*.py / conftest.py 만 pytest_ast_parser 에 전달."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_auth.py").write_text("def test_x(): pass")
    (tmp_path / "tests" / "conftest.py").write_text("import pytest")
    (tmp_path / "tests" / "helper.py").write_text("def helper(): pass")  # 제외 대상
    mock_dump.return_value = MagicMock(files_walked=3, files_uploaded=3,
                                        files_skipped_cache=0, files_failed=0)
    mock_extract.return_value = [_make_pattern_record()]

    result = await scan_all_metadata(SID, tmp_path, COMMIT,
                                      skip_llm_classification=True)

    # test_auth.py + conftest.py 만 = 2 호출
    assert mock_extract.call_count == 2
    # records 2개 (각 file 1 record mock)
    assert result.patterns_count == 2


@pytest.mark.asyncio
@patch("qapilot.scan.orchestrator.upsert_metadata_index", return_value=True)
@patch("qapilot.scan.orchestrator.dump_source_to_s3")
@patch("qapilot.scan.orchestrator.extract_backend_schemas_from_file")
async def test_scan_backend_schemas_excludes_test_files(mock_extract, mock_dump,
                                                         mock_upsert, tmp_path):
    """backend.schemas 추출 시 test_*.py 제외 (pytest_ast_parser 가 처리)."""
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "schemas.py").write_text("# pydantic")
    (tmp_path / "backend" / "test_schemas.py").write_text("# test")
    mock_dump.return_value = MagicMock(files_walked=2, files_uploaded=2,
                                        files_skipped_cache=0, files_failed=0)
    mock_extract.return_value = (
        {"R1": RequestSchema(fields=[_make_schema_field("a")])}, {}, {},
    )

    await scan_all_metadata(SID, tmp_path, COMMIT, skip_llm_classification=True)

    # 호출 시 test_schemas.py 는 제외 — schemas.py 만
    called_files = [c.args[0].name for c in mock_extract.call_args_list]
    assert "schemas.py" in called_files
    assert "test_schemas.py" not in called_files


# ────────────────────────────────────────────────────────────────────────
# 에러 graceful
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@patch("qapilot.scan.orchestrator.upsert_metadata_index", return_value=True)
@patch("qapilot.scan.orchestrator.dump_source_to_s3", side_effect=RuntimeError("S3 down"))
async def test_scan_source_dump_failure_continues(mock_dump, mock_upsert, tmp_path):
    """source dump 실패해도 다른 영역 계속 진행."""
    result = await scan_all_metadata(SID, tmp_path, COMMIT,
                                      skip_llm_classification=True)
    assert any("source_dump" in e for e in result.errors)
    # 다른 영역 결과는 정상 (모두 0 — 빈 디렉토리)
    assert result.selectors_count == 0


@pytest.mark.asyncio
@patch("qapilot.scan.orchestrator.upsert_metadata_index", return_value=True)
async def test_scan_skip_source_dump(mock_upsert, tmp_path):
    """skip_source_dump=True 면 source_dump=None."""
    result = await scan_all_metadata(SID, tmp_path, COMMIT,
                                      skip_source_dump=True,
                                      skip_llm_classification=True)
    assert result.source_dump is None


# ────────────────────────────────────────────────────────────────────────
# LLM 분류 옵션
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@patch("qapilot.scan.orchestrator.upsert_metadata_index", return_value=True)
@patch("qapilot.scan.orchestrator.dump_source_to_s3")
@patch("qapilot.scan.orchestrator.extract_patterns_from_pytest_file")
@patch("qapilot.scan.orchestrator.classify_unknown_patterns")
async def test_scan_llm_classification_called_when_client_provided(
    mock_classify, mock_extract, mock_dump, mock_upsert, tmp_path,
):
    """llm_client 제공 + skip_llm=False 면 classify_unknown_patterns 호출."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_x(): pass")
    mock_dump.return_value = MagicMock(files_walked=1, files_uploaded=1,
                                        files_skipped_cache=0, files_failed=0)
    fake_rec = MagicMock(extraction_method="ast")
    mock_extract.return_value = [fake_rec]
    classified_rec = MagicMock(extraction_method="hybrid")
    mock_classify.return_value = [classified_rec]

    mock_llm = MagicMock()
    await scan_all_metadata(
        SID, tmp_path, COMMIT,
        llm_client=mock_llm,
        skip_llm_classification=False,
    )
    mock_classify.assert_awaited_once()


@pytest.mark.asyncio
@patch("qapilot.scan.orchestrator.upsert_metadata_index", return_value=True)
@patch("qapilot.scan.orchestrator.dump_source_to_s3")
@patch("qapilot.scan.orchestrator.extract_patterns_from_pytest_file")
@patch("qapilot.scan.orchestrator.classify_unknown_patterns")
async def test_scan_skip_llm_classification(mock_classify, mock_extract,
                                              mock_dump, mock_upsert, tmp_path):
    """skip_llm_classification=True 면 LLM 호출 안 됨."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_x(): pass")
    mock_dump.return_value = MagicMock(files_walked=1, files_uploaded=1,
                                        files_skipped_cache=0, files_failed=0)
    mock_extract.return_value = [MagicMock(extraction_method="ast")]

    await scan_all_metadata(
        SID, tmp_path, COMMIT,
        llm_client=MagicMock(),
        skip_llm_classification=True,
    )
    mock_classify.assert_not_awaited()


# ────────────────────────────────────────────────────────────────────────
# ScanAllResult dataclass
# ────────────────────────────────────────────────────────────────────────

def test_scan_all_result_initial_state():
    r = ScanAllResult(service_id="x", commit_sha="y")
    assert r.selectors_count == 0
    assert r.errors == []
    assert r.source_dump is None
    assert r.selectors_upserted is False
