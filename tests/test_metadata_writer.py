"""metadata_writer 단위 테스트 — PoC 3 (본인 영역).

실 환경 (PostgreSQL + MinIO) end-to-end 검증은 manual_test_metadata_writer.py 로 분리.
본 단위 테스트는 mock 으로 cache skip / Pydantic kind 추출 / force / 결정성 확인.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from qapilot.db.metadata_writer import (
    _build_s3_key,
    _extract_kind_sub_kind,
    _to_json_bytes,
    upsert_metadata_index,
    upsert_source_file,
)
from qapilot.shared.metadata_schemas import (
    ExtractedFrom,
    FrontendSelectorsIndex,
    InputElement,
    RouteSelectors,
)


SERVICE_ID = "11111111-1111-1111-1111-111111111111"
COMMIT_SHA = "f" * 40


def _make_index() -> FrontendSelectorsIndex:
    return FrontendSelectorsIndex(
        service_id=SERVICE_ID,
        commit_sha=COMMIT_SHA,
        by_route={
            "/signup": RouteSelectors(inputs=[
                InputElement(
                    extracted_from=ExtractedFrom(
                        file="Signup.vue", line_start=1, line_end=1,
                        commit_sha=COMMIT_SHA,
                    ),
                    confidence=1.0,
                    extraction_method="ast",
                    testid="email",
                    html_type="email",
                    required=True,
                    v_model="form.email",
                    validators=["required", "email"],
                ),
            ]),
        },
    )


# ────────────────────────────────────────────────────────────────────────
# helpers
# ────────────────────────────────────────────────────────────────────────

def test_extract_kind_sub_kind_from_pydantic():
    idx = _make_index()
    assert _extract_kind_sub_kind(idx) == ("frontend", "selectors")


def test_extract_kind_sub_kind_from_dict():
    payload = {"kind": "backend", "sub_kind": "schemas"}
    assert _extract_kind_sub_kind(payload) == ("backend", "schemas")


def test_extract_kind_sub_kind_missing():
    assert _extract_kind_sub_kind({}) == ("", "")
    assert _extract_kind_sub_kind(None) == ("", "")  # type: ignore[arg-type]


def test_s3_key_format():
    """spec 정합: services/{sid}/metadata-index/{sha}/{kind}-{sub_kind}.json."""
    key = _build_s3_key(SERVICE_ID, COMMIT_SHA, "frontend", "selectors")
    assert key == (
        f"services/{SERVICE_ID}/metadata-index/{COMMIT_SHA}/frontend-selectors.json"
    )


def test_to_json_bytes_deterministic():
    """sort_keys=True → 같은 model → 같은 bytes → 같은 sha256."""
    idx = _make_index()
    a = _to_json_bytes(idx)
    b = _to_json_bytes(idx)
    assert a == b
    # sort_keys 효과 확인 — dict 순서 다르더라도 같은 결과
    d1 = {"sub_kind": "selectors", "kind": "frontend", "service_id": SERVICE_ID}
    d2 = {"service_id": SERVICE_ID, "kind": "frontend", "sub_kind": "selectors"}
    assert _to_json_bytes(d1) == _to_json_bytes(d2)


# ────────────────────────────────────────────────────────────────────────
# upsert_metadata_index — cache skip / S3 / DB 분기
# ────────────────────────────────────────────────────────────────────────

@patch("qapilot.db.metadata_writer.s3_client")
@patch("qapilot.db.metadata_writer.get_pool")
def test_upsert_cache_skip_when_head_matches(mock_pool, mock_s3):
    """head_object 의 bytes 가 일치하면 PUT skip + DB upsert 만."""
    mock_s3.head_object.return_value = {"bytes": len(_to_json_bytes(_make_index())),
                                         "etag": '"abc"'}
    mock_s3.put_bytes.return_value = None  # skip 확인용
    conn = MagicMock()
    cur = MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value.__enter__.return_value = cur
    mock_pool.return_value.connection.return_value = conn

    ok = upsert_metadata_index(
        _make_index(), service_id=SERVICE_ID, commit_hash=COMMIT_SHA,
        file_count=1, confidence=1.0, extraction_method="ast",
    )

    assert ok is True
    mock_s3.head_object.assert_called_once()
    mock_s3.put_bytes.assert_not_called()  # PUT skip
    cur.execute.assert_called_once()  # DB upsert 1회


@patch("qapilot.db.metadata_writer.s3_client")
@patch("qapilot.db.metadata_writer.get_pool")
def test_upsert_force_ignores_cache(mock_pool, mock_s3):
    """force=True 면 head_object match 여부 무관 — 무조건 PUT."""
    mock_s3.head_object.return_value = {"bytes": 999, "etag": '"x"'}
    mock_s3.put_bytes.return_value = {"bytes": 100, "sha256": "abc"}
    conn = MagicMock()
    cur = MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value.__enter__.return_value = cur
    mock_pool.return_value.connection.return_value = conn

    ok = upsert_metadata_index(
        _make_index(), service_id=SERVICE_ID, commit_hash=COMMIT_SHA,
        force=True,
    )

    assert ok is True
    mock_s3.put_bytes.assert_called_once()  # force 라 head 무시


@patch("qapilot.db.metadata_writer.s3_client")
@patch("qapilot.db.metadata_writer.get_pool")
def test_upsert_s3_failure_short_circuits_db(mock_pool, mock_s3):
    """S3 PUT 실패 → False 즉시 반환 + DB INSERT 호출 안 됨."""
    mock_s3.head_object.return_value = None  # 신규 (PUT 시도)
    mock_s3.put_bytes.return_value = None    # PUT 실패
    conn = MagicMock()
    cur = MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value.__enter__.return_value = cur
    mock_pool.return_value.connection.return_value = conn

    ok = upsert_metadata_index(
        _make_index(), service_id=SERVICE_ID, commit_hash=COMMIT_SHA,
    )

    assert ok is False
    cur.execute.assert_not_called()  # DB 안 씀


@patch("qapilot.db.metadata_writer.get_pool", return_value=None)
def test_upsert_no_pool_returns_false(_mock_pool):
    """DB pool 미설정 → False (graceful)."""
    ok = upsert_metadata_index(
        _make_index(), service_id=SERVICE_ID, commit_hash=COMMIT_SHA,
    )
    assert ok is False


@patch("qapilot.db.metadata_writer.s3_client")
@patch("qapilot.db.metadata_writer.get_pool")
def test_upsert_missing_kind_skips(mock_pool, mock_s3):
    """kind/sub_kind 없는 dict → False (skip + log)."""
    mock_pool.return_value = MagicMock()
    ok = upsert_metadata_index(
        {"foo": "bar"}, service_id=SERVICE_ID, commit_hash=COMMIT_SHA,
    )
    assert ok is False
    mock_s3.put_bytes.assert_not_called()


@patch("qapilot.db.metadata_writer.s3_client")
@patch("qapilot.db.metadata_writer.get_pool")
def test_upsert_passes_confidence_and_method_to_db(mock_pool, mock_s3):
    """ON CONFLICT UPDATE 쿼리 인자에 confidence + extraction_method 가 들어가는지."""
    mock_s3.head_object.return_value = None
    mock_s3.put_bytes.return_value = {"bytes": 100, "sha256": "abc"}
    conn = MagicMock()
    cur = MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value.__enter__.return_value = cur
    mock_pool.return_value.connection.return_value = conn

    upsert_metadata_index(
        _make_index(), service_id=SERVICE_ID, commit_hash=COMMIT_SHA,
        confidence=0.85, extraction_method="hybrid", file_count=42,
    )

    args = cur.execute.call_args[0][1]
    # tuple 안에 우리가 넣은 값들이 들어가는지
    assert 42 in args              # file_count
    assert 0.85 in args            # confidence
    assert "hybrid" in args        # extraction_method


# ────────────────────────────────────────────────────────────────────────
# upsert_source_file
# ────────────────────────────────────────────────────────────────────────

@patch("qapilot.db.metadata_writer.s3_client")
def test_source_file_path_sanitizes_traversal(mock_s3):
    """`../` 같은 path traversal 시도 제거."""
    mock_s3.head_object.return_value = None
    mock_s3.put_bytes.return_value = {"bytes": 5, "sha256": "x"}

    key = upsert_source_file(
        service_id=SERVICE_ID, commit_hash=COMMIT_SHA,
        relative_path="../../etc/passwd",
        content=b"hello",
    )

    # ".." 제거됨 + leading slash 제거됨
    assert key is not None
    assert ".." not in key
    assert key.startswith(f"services/{SERVICE_ID}/source/{COMMIT_SHA}/")


@patch("qapilot.db.metadata_writer.s3_client")
def test_source_file_cache_skip(mock_s3):
    """head_object 의 bytes 일치 시 PUT skip + key 만 반환."""
    content = b"const x = 1;"
    mock_s3.head_object.return_value = {"bytes": len(content), "etag": '"y"'}

    key = upsert_source_file(
        service_id=SERVICE_ID, commit_hash=COMMIT_SHA,
        relative_path="src/foo.ts",
        content=content,
    )

    assert key is not None
    mock_s3.put_bytes.assert_not_called()  # cache skip


@patch("qapilot.db.metadata_writer.s3_client")
def test_source_file_s3_failure_returns_none(mock_s3):
    """S3 PUT 실패 → None."""
    mock_s3.head_object.return_value = None
    mock_s3.put_bytes.return_value = None
    key = upsert_source_file(
        service_id=SERVICE_ID, commit_hash=COMMIT_SHA,
        relative_path="src/foo.ts",
        content=b"x",
    )
    assert key is None
