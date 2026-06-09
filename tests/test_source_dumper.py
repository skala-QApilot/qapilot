"""source_dumper 단위 테스트 — PoC 9 (본인 영역).

mock s3_client + 실제 file walk (tmp 디렉토리) 으로 검증.
git clone 자체는 subprocess 의존 — manual integration test 으로 별도.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from qapilot.scan.source_dumper import (
    DEFAULT_EXCLUDE_DIRS,
    DEFAULT_WHITELIST,
    EXCLUDE_FILENAMES,
    MAX_FILE_SIZE_BYTES,
    SourceDumpResult,
    _mask_url,
    dump_source_to_s3,
    iter_source_files,
)


SERVICE_ID = "service-aaaa"
COMMIT = "f" * 40


# ────────────────────────────────────────────────────────────────────────
# helpers
# ────────────────────────────────────────────────────────────────────────

def test_mask_url_basic_pat():
    assert _mask_url("https://x:TOKEN@github.com/foo/bar.git") == \
        "https://***@github.com/foo/bar.git"


def test_mask_url_no_auth_unchanged():
    assert _mask_url("https://github.com/foo/bar.git") == \
        "https://github.com/foo/bar.git"


# ────────────────────────────────────────────────────────────────────────
# iter_source_files
# ────────────────────────────────────────────────────────────────────────

def test_iter_yields_whitelist_extensions(tmp_path):
    (tmp_path / "a.py").write_text("x")
    (tmp_path / "b.vue").write_text("x")
    (tmp_path / "c.png").write_bytes(b"\xff\xd8")  # binary 제외
    (tmp_path / "d.lock").write_text("x")            # 확장자 X
    files = sorted(p.name for p in iter_source_files(tmp_path))
    assert files == ["a.py", "b.vue"]


def test_iter_excludes_dirs(tmp_path):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "foo.js").write_text("x")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("x")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x")
    files = sorted(p.name for p in iter_source_files(tmp_path))
    assert files == ["main.py"]


def test_iter_excludes_known_lock_filenames(tmp_path):
    (tmp_path / "package-lock.json").write_text("x")
    (tmp_path / "yarn.lock").write_text("x")
    (tmp_path / "valid.json").write_text("{}")
    files = sorted(p.name for p in iter_source_files(tmp_path))
    assert files == ["valid.json"]


def test_iter_excludes_oversized_files(tmp_path):
    small = tmp_path / "small.py"
    small.write_text("x")
    big = tmp_path / "big.py"
    big.write_bytes(b"x" * (MAX_FILE_SIZE_BYTES + 1))
    files = sorted(p.name for p in iter_source_files(tmp_path))
    assert files == ["small.py"]


def test_iter_recursive(tmp_path):
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    (nested / "deep.py").write_text("x")
    files = list(iter_source_files(tmp_path))
    assert len(files) == 1
    assert files[0].name == "deep.py"


def test_iter_missing_root_returns_empty(tmp_path):
    assert list(iter_source_files(tmp_path / "no_such")) == []


def test_iter_custom_whitelist(tmp_path):
    (tmp_path / "a.py").write_text("x")
    (tmp_path / "b.vue").write_text("x")
    files = sorted(p.name for p in iter_source_files(
        tmp_path, whitelist=(".vue",),
    ))
    assert files == ["b.vue"]


# ────────────────────────────────────────────────────────────────────────
# dump_source_to_s3
# ────────────────────────────────────────────────────────────────────────

@patch("qapilot.scan.source_dumper.upsert_source_file")
@patch("qapilot.scan.source_dumper.s3_client")
def test_dump_uploads_all_files(mock_s3, mock_upsert, tmp_path):
    (tmp_path / "a.py").write_text("hello")
    (tmp_path / "b.js").write_text("world")
    mock_s3.head_object.return_value = None
    mock_upsert.side_effect = lambda **kw: f"key/{kw['relative_path']}"

    result = dump_source_to_s3(SERVICE_ID, tmp_path, COMMIT)

    assert result.files_walked == 2
    assert result.files_uploaded == 2
    assert result.files_skipped_cache == 0
    assert result.files_failed == 0
    assert len(result.uploaded_keys) == 2


@patch("qapilot.scan.source_dumper.upsert_source_file")
@patch("qapilot.scan.source_dumper.s3_client")
def test_dump_cache_skip_when_head_matches(mock_s3, mock_upsert, tmp_path):
    """head_object 의 bytes 가 일치하면 upsert_source_file 호출 안 함."""
    (tmp_path / "a.py").write_bytes(b"hello")
    # head_object 가 byte size 일치 반환 → cache skip
    mock_s3.head_object.return_value = {"bytes": 5, "etag": '"x"'}

    result = dump_source_to_s3(SERVICE_ID, tmp_path, COMMIT)

    assert result.files_walked == 1
    assert result.files_skipped_cache == 1
    assert result.files_uploaded == 0
    mock_upsert.assert_not_called()


@patch("qapilot.scan.source_dumper.upsert_source_file")
@patch("qapilot.scan.source_dumper.s3_client")
def test_dump_force_bypasses_cache(mock_s3, mock_upsert, tmp_path):
    """force=True 시 head_object 일치해도 upsert 시도."""
    (tmp_path / "a.py").write_bytes(b"hello")
    mock_s3.head_object.return_value = {"bytes": 5, "etag": '"x"'}
    mock_upsert.return_value = "key/a.py"

    result = dump_source_to_s3(SERVICE_ID, tmp_path, COMMIT, force=True)

    assert result.files_uploaded == 1
    mock_upsert.assert_called_once()


@patch("qapilot.scan.source_dumper.upsert_source_file")
@patch("qapilot.scan.source_dumper.s3_client")
def test_dump_failed_files_accumulate(mock_s3, mock_upsert, tmp_path):
    """upsert 실패 → failed_paths 누적 + 다른 파일 계속."""
    (tmp_path / "ok.py").write_text("x")
    (tmp_path / "bad.py").write_text("x")
    mock_s3.head_object.return_value = None
    mock_upsert.side_effect = [None, "key/ok.py"]  # 첫 호출 실패

    result = dump_source_to_s3(SERVICE_ID, tmp_path, COMMIT)

    assert result.files_walked == 2
    # 둘 다 시도 — 1 성공 + 1 실패
    assert result.files_uploaded + result.files_failed == 2
    assert result.files_failed == 1


def test_dump_no_files_in_empty_dir(tmp_path):
    result = dump_source_to_s3(SERVICE_ID, tmp_path, COMMIT)
    assert result.files_walked == 0
    assert result.files_uploaded == 0


def test_source_dump_result_initial_state():
    r = SourceDumpResult(service_id="x", commit_sha="y")
    assert r.files_walked == 0
    assert r.uploaded_keys == []
    assert r.failed_paths == []
