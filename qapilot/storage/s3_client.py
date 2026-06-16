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
from pathlib import Path
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


def list_objects(prefix: str) -> list[str]:
    """주어진 prefix 로 시작하는 객체 키 목록 반환.

    tc_generation/{ts_id}/ 아래 기존 v{N}.json 들을 스캔해 다음 버전 번호를
    계산하는 용도. S3 비활성/실패 시 빈 list (graceful no-op).
    """
    client, bucket = get_client()
    if client is None:
        return []
    keys: list[str] = []
    try:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"])
    except Exception as e:
        _logger.warning("s3_list_failed", prefix=prefix, error=str(e))
    return keys


def head_object(key: str) -> dict | None:
    """S3 HEAD — 객체 존재 + 메타 확인 (cache skip 정책용).

    반환:
      {"bytes": <ContentLength>, "etag": <ETag>}  객체 존재
      None                                          미존재 / S3 비활성

    본인 PoC 3 (metadata_indices upsert) 의 "같은 commit_sha 재스캔 시 PUT skip"
    판단 헬퍼. get_object 대비 본문 다운로드 없음 — 빠름 + 저렴.
    """
    client, bucket = get_client()
    if client is None:
        return None
    try:
        resp = client.head_object(Bucket=bucket, Key=key)
        return {"bytes": int(resp.get("ContentLength", 0)), "etag": resp.get("ETag")}
    except Exception:
        # NoSuchKey / 404 등 — graceful (None 으로 "미존재" 시그널)
        return None


def get_object(key: str) -> bytes | None:
    """S3 GET. 반환: bytes 또는 None (실패/비활성).

    격차 #207 sub-E — agent 측 S3 read 통로. sub-D (RunOptions.domain_files) +
    sub-F Part 2 (pipeline._doc_import) 의 전제.

    graceful no-op 정책 (put_bytes 동형):
    - S3 환경변수 미설정 → None (s3_disabled 이미 log 됨)
    - 객체 부재 (NoSuchKey) → None + warning
    - 기타 boto3 예외 → None + warning
    """
    client, bucket = get_client()
    if client is None:
        return None
    try:
        resp = client.get_object(Bucket=bucket, Key=key)
        return resp["Body"].read()
    except Exception as e:
        _logger.warning("s3_get_failed", key=key, error=str(e))
        return None


def download(key: str, local_path: str) -> bool:
    """S3 GET → local 파일 저장. 반환: 성공 여부.

    격차 #207 sub-E — agent _doc_import (sub-F Part 2) 가 S3 객체를 임시
    디렉토리에 download 한 후 기존 DomainKnowledgeTool / _read_latest_prd_text
    로 처리하기 위한 헬퍼.

    graceful no-op 정책:
    - S3 미설정 또는 객체 부재 → False (get_object None 결과)
    - local_path 의 부모 디렉토리 자동 생성 (mkdir parents=True)
    - 파일 write 실패 → False + warning
    """
    data = get_object(key)
    if data is None:
        return False
    try:
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(data)
        return True
    except OSError as e:
        _logger.warning("s3_download_write_failed", key=key, path=local_path, error=str(e))
        return False
