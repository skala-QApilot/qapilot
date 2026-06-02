"""sync boto3 S3 클라이언트 — MinIO (로컬) / AWS S3 (운영) 둘 다 같은 코드.

환경변수 미설정 시 graceful no-op — 호출자는 None 인지 체크 가능.

환경변수:
  S3_ENDPOINT           e.g. http://localhost:9000 (MinIO) / 미설정 → AWS 기본 endpoint
  S3_REGION             기본 us-east-1
  S3_BUCKET             필수
  S3_ACCESS_KEY         필수
  S3_SECRET_KEY         필수
  S3_PATH_STYLE_ACCESS  true → MinIO path-style 강제

Author: C
Created: 2026-06-01
"""

from __future__ import annotations

import hashlib
import os
import threading
from typing import Any

from qapilot.shared.logger import get_logger

_logger = get_logger("storage.s3")
_client: Any = None  # boto3 S3 client | None
_bucket: str | None = None
_lock = threading.Lock()
_failed = False


def get_client() -> tuple[Any, str | None]:
    """boto3 S3 client 와 bucket 이름 튜플 반환. 미설정 시 (None, None)."""
    global _client, _bucket, _failed
    if _client is not None:
        return _client, _bucket
    if _failed:
        return None, None

    with _lock:
        if _client is not None:
            return _client, _bucket
        if _failed:
            return None, None

        bucket = os.environ.get("S3_BUCKET")
        access_key = os.environ.get("S3_ACCESS_KEY")
        secret_key = os.environ.get("S3_SECRET_KEY")
        if not (bucket and access_key and secret_key):
            _logger.info("s3_disabled", reason="S3_BUCKET/ACCESS_KEY/SECRET_KEY 중 누락")
            _failed = True
            return None, None

        try:
            import boto3
            from botocore.config import Config

            endpoint = os.environ.get("S3_ENDPOINT") or None
            region = os.environ.get("S3_REGION", "us-east-1")
            path_style = os.environ.get("S3_PATH_STYLE_ACCESS", "false").lower() == "true"

            _client = boto3.client(
                "s3",
                endpoint_url=endpoint,
                region_name=region,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                config=Config(s3={"addressing_style": "path" if path_style else "auto"}),
            )
            _bucket = bucket
            _logger.info("s3_client_ready", endpoint=endpoint, bucket=bucket)
            return _client, _bucket
        except Exception as e:
            _logger.warning("s3_client_open_failed", error=str(e))
            _failed = True
            return None, None


def put_bytes(key: str, data: bytes, content_type: str) -> dict | None:
    """S3 PUT. 반환: {bytes, sha256} 또는 None (실패/비활성)."""
    client, bucket = get_client()
    if client is None:
        return None
    try:
        client.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)
        return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    except Exception as e:
        _logger.warning("s3_put_failed", key=key, error=str(e))
        return None


def put_file(key: str, path: str, content_type: str) -> dict | None:
    """파일 경로에서 읽어 PUT."""
    try:
        with open(path, "rb") as f:
            return put_bytes(key, f.read(), content_type)
    except OSError as e:
        _logger.warning("s3_put_file_read_failed", path=path, error=str(e))
        return None
