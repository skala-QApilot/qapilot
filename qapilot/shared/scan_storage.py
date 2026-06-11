"""유빈 agent (TC/TV generator) 가 사용할 통합 조회 API — PoC 4 (데이터 layer).

 데이터 layer 의 single entry point:
- `load_metadata_index(service_id, kind, sub_kind)` — 메타데이터 카탈로그 JSON
- `load_source(service_id, sha, path, line_start?, line_end?)` — 코드베이스 본문

내부:
- DB s3_key 조회 → S3 GET → JSON parse (metadata_indices)
- S3 source/ GET + line range 슬라이싱 (source/)
- process-local LRU 캐시 (token 절감 / 같은 trace 중 반복 호출 최적화)

Author: 주환 (kimjuhwan).
Created: 2026-06-09
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from qapilot.db.metadata_reader import get_latest_commit_hash, load_metadata_index_raw
from qapilot.shared.logger import get_logger
from qapilot.storage import s3_client

_logger = get_logger("shared.scan_storage")

# LRU 캐시 크기 — 한 trace 중 반복 호출 패턴 가정.
# 메타데이터 = 영역 4개 × commit 1~2개 × service 수 ≈ 32
# source = 호출 패턴에 따라 변동 (한 trace 50 파일 가정)
_METADATA_CACHE_SIZE = 64
_SOURCE_CACHE_SIZE = 128


# ────────────────────────────────────────────────────────────────────────
# Local mirror helpers
# ────────────────────────────────────────────────────────────────────────

def _repo_root() -> Path:
    """워크스페이스 루트(QApilot)를 반환한다."""
    return Path(__file__).resolve().parents[3]


def _local_services_root() -> Path:
    """qapilot-local/services 루트 경로."""
    return _repo_root() / "qapilot-local" / "services"


def _metadata_local_path(
    service_id: str,
    commit_hash: str,
    kind: str,
    sub_kind: str,
) -> Path:
    return (
        _local_services_root()
        / service_id
        / "metadata-index"
        / commit_hash
        / f"{kind}-{sub_kind}.json"
    )


def _find_latest_local_commit(
    service_id: str,
    kind: str,
    sub_kind: str,
) -> str | None:
    """로컬 미러에 있는 최신 metadata-index commit 디렉토리명을 찾는다."""
    base = _local_services_root() / service_id / "metadata-index"
    if not base.exists():
        return None
    candidates = list(base.glob(f"*/{kind}-{sub_kind}.json"))
    if not candidates:
        return None
    latest = max(candidates, key=lambda path: path.stat().st_mtime)
    return latest.parent.name


def _load_local_metadata_index(
    service_id: str,
    kind: str,
    sub_kind: str,
    commit_hash: str,
) -> dict | None:
    """qapilot-local/services/{sid}/metadata-index/... 로컬 미러에서 직접 로드."""
    path = _metadata_local_path(service_id, commit_hash, kind, sub_kind)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        _logger.warning("local_metadata_index_parse_failed", path=str(path), error=str(e))
        return None


# ────────────────────────────────────────────────────────────────────────
# Metadata index
# ────────────────────────────────────────────────────────────────────────

def load_metadata_index(
    service_id: str,
    kind: str,
    sub_kind: str,
    commit_hash: str | None = None,
) -> dict | None:
    """metadata_indices record 의 JSON 본문 반환.

    Args:
        service_id: service UUID.
        kind: "frontend" | "backend" | "sut_tests".
        sub_kind: "selectors" | "routes" | "schemas" | "patterns".
        commit_hash: 지정 시 해당 commit, 없으면 최신 scanned_at.

    Returns:
        FrontendSelectorsIndex / FrontendRoutesIndex / ... 의 JSON dict.
        DB 미존재 / S3 GET 실패 시 None.

    cache:
        같은 (service, kind, sub_kind, commit) 의 반복 호출 = process-local LRU hit.
        commit_hash=None 인 호출 = 최신 commit_hash 조회 후 그 hash 기준 cache.
    """
    if commit_hash is None:
        commit_hash = _find_latest_local_commit(service_id, kind, sub_kind)
    if commit_hash is None:
        commit_hash = get_latest_commit_hash(service_id, kind, sub_kind)
        if commit_hash is None:
            return None
    return _metadata_cached(service_id, kind, sub_kind, commit_hash)


@lru_cache(maxsize=_METADATA_CACHE_SIZE)
def _metadata_cached(
    service_id: str, kind: str, sub_kind: str, commit_hash: str,
) -> dict | None:
    local = _load_local_metadata_index(service_id, kind, sub_kind, commit_hash)
    if local is not None:
        return local
    return load_metadata_index_raw(service_id, kind, sub_kind, commit_hash)


# ────────────────────────────────────────────────────────────────────────
# Source 본문
# ────────────────────────────────────────────────────────────────────────

def load_source(
    service_id: str,
    commit_sha: str,
    path: str,
    line_start: int | None = None,
    line_end: int | None = None,
) -> str | None:
    """S3 의 source/{commit_sha}/{path} 본문 반환.

    Args:
        service_id: service UUID.
        commit_sha: 풀 SHA (40자) — `metadata_indices.commit_hash` 와 동형.
        path: repo root 기준 상대 경로. `..` traversal 자동 제거.
        line_start, line_end: 1-based inclusive. 지정 시 해당 line range 만 반환
                              (token 절감). 둘 다 None 이면 전체 본문.

    Returns:
        file 본문 (utf-8 str). 미존재 / GET 실패 시 None.

    cache:
        같은 (service, sha, path) 의 반복 호출 = process-local LRU hit.
        line range 슬라이싱은 cache 후 적용 — 같은 파일을 다른 range 로 호출 시
        S3 GET 1회만.
    """
    if not (service_id and commit_sha and path):
        return None
    full = _source_cached(service_id, commit_sha, path)
    if full is None:
        return None
    if line_start is None and line_end is None:
        return full
    return _slice_lines(full, line_start, line_end)


@lru_cache(maxsize=_SOURCE_CACHE_SIZE)
def _source_cached(service_id: str, commit_sha: str, path: str) -> str | None:
    safe_path = path.lstrip("/").replace("..", "")
    key = f"services/{service_id}/source/{commit_sha}/{safe_path}"
    data = s3_client.get_object(key)
    if data is None:
        _logger.info("source_miss", key=key)
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as e:
        _logger.warning("source_decode_failed", key=key, error=str(e))
        return None


def _slice_lines(text: str, line_start: int | None, line_end: int | None) -> str:
    """1-based inclusive line range 슬라이싱. None 은 양 끝 무한."""
    lines = text.splitlines(keepends=True)
    start_idx = max(0, (line_start or 1) - 1)
    end_idx = len(lines) if line_end is None else min(len(lines), line_end)
    return "".join(lines[start_idx:end_idx])


# ────────────────────────────────────────────────────────────────────────
# Cache 관리 (test / 강제 새로고침)
# ────────────────────────────────────────────────────────────────────────

def clear_cache() -> None:
    """process-local LRU 캐시 비움 — 테스트 격리 + 강제 새로고침용."""
    _metadata_cached.cache_clear()
    _source_cached.cache_clear()


def cache_info() -> dict[str, Any]:
    """디버깅용 cache hit/miss 통계."""
    return {
        "metadata": _metadata_cached.cache_info()._asdict(),
        "source": _source_cached.cache_info()._asdict(),
    }
