"""s3_client get_object / download 단위 검증 (격차 #207 sub-E).

agent 측 S3 read 통로. sub-D (state.domain_files) + sub-F Part 2
(pipeline._doc_import S3 download) 의 전제. graceful no-op 정책 검증.

본 테스트는 boto3 외부 의존을 unittest.mock 으로 격리 — moto 미사용 (단위
테스트 영역 한정, 통합 테스트는 별도).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from qapilot.storage import s3_client


@pytest.fixture(autouse=True)
def _reset_module_globals():
    """매 테스트마다 _client/_bucket/_failed 초기화 — get_client 의 lazy init 재실행 보장."""
    s3_client._client = None
    s3_client._bucket = None
    s3_client._failed = False
    yield
    s3_client._client = None
    s3_client._bucket = None
    s3_client._failed = False


# ── get_object ──


def test_get_object_returns_none_when_s3_disabled(monkeypatch):
    """S3 환경변수 미설정 → graceful None."""
    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.delenv("S3_ACCESS_KEY", raising=False)
    monkeypatch.delenv("S3_SECRET_KEY", raising=False)

    result = s3_client.get_object("any/key")

    assert result is None


def test_get_object_returns_body_bytes_on_success():
    """정상 — boto3 client.get_object Body.read() 결과 반환."""
    mock_body = MagicMock()
    mock_body.read.return_value = b"hello world"
    mock_client = MagicMock()
    mock_client.get_object.return_value = {"Body": mock_body}

    with patch.object(s3_client, "get_client", return_value=(mock_client, "test-bucket")):
        result = s3_client.get_object("services/svc-1/domain/file-1/v1/PRD.md")

    assert result == b"hello world"
    mock_client.get_object.assert_called_once_with(
        Bucket="test-bucket",
        Key="services/svc-1/domain/file-1/v1/PRD.md",
    )


def test_get_object_returns_none_on_nosuchkey():
    """boto3 NoSuchKey 등 예외 → graceful None + warning."""
    mock_client = MagicMock()
    mock_client.get_object.side_effect = Exception("NoSuchKey: ...")
    mock_logger = MagicMock()

    with patch.object(s3_client, "get_client", return_value=(mock_client, "test-bucket")), \
         patch.object(s3_client, "_logger", mock_logger):
        result = s3_client.get_object("missing/key")

    assert result is None
    # warning 로그 — key + error 포함
    assert mock_logger.warning.called
    call_args = mock_logger.warning.call_args
    assert call_args.args[0] == "s3_get_failed"
    assert call_args.kwargs.get("key") == "missing/key"


def test_get_object_returns_none_on_generic_exception():
    """기타 예외 (네트워크 등) → graceful None."""
    mock_client = MagicMock()
    mock_client.get_object.side_effect = ConnectionError("timeout")

    with patch.object(s3_client, "get_client", return_value=(mock_client, "test-bucket")):
        result = s3_client.get_object("any/key")

    assert result is None


# ── download ──


def test_download_returns_false_when_get_object_none(tmp_path: Path):
    """get_object 가 None 반환 → download 도 False, 파일 생성 안 함."""
    with patch.object(s3_client, "get_object", return_value=None):
        result = s3_client.download("missing/key", str(tmp_path / "out.md"))

    assert result is False
    assert not (tmp_path / "out.md").exists()


def test_download_writes_bytes_to_local_path_on_success(tmp_path: Path):
    """get_object 결과 bytes 를 local_path 에 저장 + True 반환."""
    payload = b"# PRD v4.0\n\n...\n"
    out_path = tmp_path / "downloads" / "PRD.md"

    with patch.object(s3_client, "get_object", return_value=payload):
        result = s3_client.download("services/svc-1/domain/file-1/v1/PRD.md", str(out_path))

    assert result is True
    assert out_path.exists()
    assert out_path.read_bytes() == payload


def test_download_creates_parent_directories(tmp_path: Path):
    """local_path 의 부모 디렉토리가 없으면 자동 생성 (mkdir parents=True)."""
    deep_path = tmp_path / "a" / "b" / "c" / "deep.md"
    assert not deep_path.parent.exists()

    with patch.object(s3_client, "get_object", return_value=b"x"):
        result = s3_client.download("k", str(deep_path))

    assert result is True
    assert deep_path.parent.is_dir()
    assert deep_path.read_bytes() == b"x"


def test_download_returns_false_on_write_oserror(tmp_path: Path):
    """파일 write 실패 (예: permission denied) → graceful False + warning."""
    mock_logger = MagicMock()

    with patch.object(s3_client, "get_object", return_value=b"x"), \
         patch("builtins.open", side_effect=OSError("Permission denied")), \
         patch.object(s3_client, "_logger", mock_logger):
        result = s3_client.download("k", str(tmp_path / "out.md"))

    assert result is False
    # warning 로그 — s3_download_write_failed
    assert mock_logger.warning.called
    call_args = mock_logger.warning.call_args
    assert call_args.args[0] == "s3_download_write_failed"


def test_download_returns_false_when_s3_disabled(monkeypatch, tmp_path: Path):
    """S3 미설정 환경 → get_object None → download False, 파일 생성 안 함."""
    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.delenv("S3_ACCESS_KEY", raising=False)
    monkeypatch.delenv("S3_SECRET_KEY", raising=False)
    out_path = tmp_path / "out.md"

    result = s3_client.download("k", str(out_path))

    assert result is False
    assert not out_path.exists()
