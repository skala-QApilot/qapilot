"""scan_storage 단위 테스트 — PoC 4 (데이터 layer).

mock 으로 DB/S3 격리. cache hit/miss + line range + graceful None + traversal 방어.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from qapilot.shared import scan_storage
from qapilot.shared.scan_storage import (
    _slice_lines,
    cache_info,
    clear_cache,
    load_metadata_index,
    load_source,
)


SERVICE_ID = "11111111-1111-1111-1111-111111111111"
COMMIT = "c" * 40


@pytest.fixture(autouse=True)
def _reset_cache():
    """각 테스트 격리 — LRU cache 비움."""
    clear_cache()
    yield
    clear_cache()


# ────────────────────────────────────────────────────────────────────────
# _slice_lines
# ────────────────────────────────────────────────────────────────────────

def test_slice_lines_inclusive_range():
    text = "a\nb\nc\nd\ne\n"
    # 1-based inclusive
    assert _slice_lines(text, 2, 4) == "b\nc\nd\n"


def test_slice_lines_no_range_returns_all():
    text = "x\ny\nz\n"
    assert _slice_lines(text, None, None) == text


def test_slice_lines_only_start():
    text = "a\nb\nc\nd\n"
    assert _slice_lines(text, 3, None) == "c\nd\n"


def test_slice_lines_only_end():
    text = "a\nb\nc\nd\n"
    assert _slice_lines(text, None, 2) == "a\nb\n"


def test_slice_lines_out_of_range_clamps():
    text = "a\nb\n"
    # start 가 line 수보다 큼 → 빈 문자열
    assert _slice_lines(text, 100, 200) == ""


# ────────────────────────────────────────────────────────────────────────
# load_metadata_index
# ────────────────────────────────────────────────────────────────────────

@patch.object(scan_storage, "load_metadata_index_raw")
def test_metadata_explicit_commit_passes_through(mock_raw):
    mock_raw.return_value = {"kind": "frontend", "sub_kind": "selectors"}
    got = load_metadata_index(SERVICE_ID, "frontend", "selectors", commit_hash=COMMIT)
    assert got == {"kind": "frontend", "sub_kind": "selectors"}
    mock_raw.assert_called_once_with(SERVICE_ID, "frontend", "selectors", COMMIT)


@patch.object(scan_storage, "load_metadata_index_raw")
@patch.object(scan_storage, "get_latest_commit_hash")
def test_metadata_commit_none_resolves_latest(mock_latest, mock_raw):
    mock_latest.return_value = "deadbeef" + "0" * 32
    mock_raw.return_value = {"kind": "frontend", "sub_kind": "selectors"}
    load_metadata_index(SERVICE_ID, "frontend", "selectors")
    mock_latest.assert_called_once_with(SERVICE_ID, "frontend", "selectors")
    mock_raw.assert_called_once()
    assert mock_raw.call_args[0][3] == "deadbeef" + "0" * 32


@patch.object(scan_storage, "load_metadata_index_raw")
@patch.object(scan_storage, "get_latest_commit_hash", return_value=None)
def test_metadata_no_commit_in_db_returns_none(_mock_latest, mock_raw):
    assert load_metadata_index(SERVICE_ID, "frontend", "selectors") is None
    mock_raw.assert_not_called()  # latest 가 None 이면 raw 호출 안 함


@patch.object(scan_storage, "load_metadata_index_raw")
def test_metadata_cache_hits_on_repeat_explicit(mock_raw):
    mock_raw.return_value = {"kind": "frontend", "sub_kind": "selectors"}
    for _ in range(5):
        load_metadata_index(SERVICE_ID, "frontend", "selectors", commit_hash=COMMIT)
    # cache: 첫 호출만 miss → 1회 raw 호출
    assert mock_raw.call_count == 1
    info = cache_info()
    assert info["metadata"]["hits"] == 4
    assert info["metadata"]["misses"] == 1


# ────────────────────────────────────────────────────────────────────────
# load_source
# ────────────────────────────────────────────────────────────────────────

@patch.object(scan_storage.s3_client, "get_object")
def test_source_full_content(mock_get):
    mock_get.return_value = b"line1\nline2\nline3\n"
    out = load_source(SERVICE_ID, COMMIT, "src/foo.py")
    assert out == "line1\nline2\nline3\n"
    mock_get.assert_called_once_with(
        f"services/{SERVICE_ID}/source/{COMMIT}/src/foo.py"
    )


@patch.object(scan_storage.s3_client, "get_object")
def test_source_line_range(mock_get):
    mock_get.return_value = b"a\nb\nc\nd\ne\n"
    out = load_source(SERVICE_ID, COMMIT, "f.py", line_start=2, line_end=4)
    assert out == "b\nc\nd\n"


@patch.object(scan_storage.s3_client, "get_object")
def test_source_cache_hit_across_different_ranges(mock_get):
    """같은 파일 다른 range 호출 → S3 GET 1회만 (cache hit)."""
    mock_get.return_value = b"a\nb\nc\nd\ne\n"
    load_source(SERVICE_ID, COMMIT, "f.py")
    load_source(SERVICE_ID, COMMIT, "f.py", line_start=1, line_end=2)
    load_source(SERVICE_ID, COMMIT, "f.py", line_start=3, line_end=5)
    assert mock_get.call_count == 1
    assert cache_info()["source"]["hits"] == 2


@patch.object(scan_storage.s3_client, "get_object", return_value=None)
def test_source_missing_returns_none(_mock_get):
    assert load_source(SERVICE_ID, COMMIT, "no/such/file.py") is None


@patch.object(scan_storage.s3_client, "get_object")
def test_source_traversal_sanitized(mock_get):
    """`../../etc/passwd` → `..` 제거 + leading `/` 제거."""
    mock_get.return_value = None
    load_source(SERVICE_ID, COMMIT, "../../etc/passwd")
    called_key = mock_get.call_args[0][0]
    assert ".." not in called_key


@patch.object(scan_storage.s3_client, "get_object")
def test_source_invalid_utf8_returns_none(mock_get):
    """binary 파일 등 utf-8 디코딩 실패 시 None (graceful)."""
    mock_get.return_value = b"\xff\xfe\x00\x00invalid"
    assert load_source(SERVICE_ID, COMMIT, "binary.bin") is None


def test_source_empty_args_returns_none():
    assert load_source("", COMMIT, "f.py") is None
    assert load_source(SERVICE_ID, "", "f.py") is None
    assert load_source(SERVICE_ID, COMMIT, "") is None


# ────────────────────────────────────────────────────────────────────────
# cache_info / clear_cache
# ────────────────────────────────────────────────────────────────────────

def test_cache_info_shape():
    info = cache_info()
    assert "metadata" in info and "source" in info
    for stat in info.values():
        assert {"hits", "misses", "maxsize", "currsize"} <= stat.keys()


@patch.object(scan_storage, "load_metadata_index_raw", return_value={"k": "v"})
def test_clear_cache_resets(mock_raw):
    load_metadata_index(SERVICE_ID, "frontend", "selectors", commit_hash=COMMIT)
    assert cache_info()["metadata"]["currsize"] == 1
    clear_cache()
    assert cache_info()["metadata"]["currsize"] == 0
    # 다시 호출하면 miss
    load_metadata_index(SERVICE_ID, "frontend", "selectors", commit_hash=COMMIT)
    assert mock_raw.call_count == 2
