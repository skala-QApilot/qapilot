"""코드베이스 원본 → S3 source/ dump — PoC 9 (데이터 layer).

회의 결정 3 (2026-06-09): "PoC full S3 dump + TTL 30일".
회의 verbatim: "깃허브 → 스캔 → S3에 전부 저장 (비용 따져)".

본 모듈은 다음 high-level helper 제공:
- `clone_repo_shallow(repo_url)` → `git clone --depth 1` 임시 디렉토리
- `dump_source_to_s3(service_id, repo_root, commit_sha)` → walk + 각 파일 upsert_source_file

caller (PoC 10 orchestrator / service register flow) 가 두 함수 조합 호출.

Author: 주환 (kimjuhwan).
Created: 2026-06-09
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from qapilot.db.metadata_writer import upsert_source_file
from qapilot.shared.logger import get_logger
from qapilot.storage import s3_client

_logger = get_logger("scan.source_dumper")


# ────────────────────────────────────────────────────────────────────────
# Walk 정책 — whitelist + exclude
# ────────────────────────────────────────────────────────────────────────

# 텍스트 확장자 whitelist (s3-path-spec.md §2.2 와 정합)
DEFAULT_WHITELIST: tuple[str, ...] = (
    ".py", ".js", ".ts", ".tsx", ".jsx", ".vue",
    ".html", ".css", ".scss",
    ".json", ".yaml", ".yml", ".toml",
    ".md", ".sql",
)

# 디렉토리 exclude (재귀 스킵)
DEFAULT_EXCLUDE_DIRS: frozenset[str] = frozenset({
    "node_modules", ".git", "__pycache__", ".pytest_cache",
    "dist", "build", "venv", ".venv", ".tox",
    ".qapilot",  # QApilot 산출물
    "coverage", ".coverage",
})

# .lock / lock.json 등 큰 파일 제외 (확장자 외 파일명 패턴)
EXCLUDE_FILENAMES: frozenset[str] = frozenset({
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock",
})

# 단일 파일 크기 상한 (5MB) — minified bundle 등 차단
MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024


# ────────────────────────────────────────────────────────────────────────
# 결과 dataclass
# ────────────────────────────────────────────────────────────────────────

@dataclass
class SourceDumpResult:
    """dump_source_to_s3 의 결과 요약."""

    service_id: str
    commit_sha: str
    files_walked: int = 0
    files_uploaded: int = 0      # 새로 PUT 한 파일
    files_skipped_cache: int = 0  # head_object match → skip
    files_skipped_exclude: int = 0  # whitelist 외 / 디렉토리 exclude / 크기 초과
    files_failed: int = 0
    uploaded_keys: list[str] = field(default_factory=list)
    failed_paths: list[str] = field(default_factory=list)


# ────────────────────────────────────────────────────────────────────────
# git clone --depth 1 helper
# ────────────────────────────────────────────────────────────────────────

def clone_repo_shallow(
    repo_url: str,
    *,
    target_dir: Path | None = None,
    branch: str | None = None,
    timeout: float = 120.0,
) -> tuple[Path, str]:
    """`git clone --depth 1` → (repo_path, commit_sha).

    Args:
        repo_url: HTTPS/SSH URL. PAT 인증은 caller 가 URL 에 동봉 (https://x:TOKEN@github.com/...).
        target_dir: 클론 대상 디렉토리. None 이면 tempfile.mkdtemp() 자동 생성.
        branch: 특정 브랜치 (None 이면 default branch HEAD).
        timeout: subprocess timeout (초).

    Returns:
        (repo_path, commit_sha) — caller 가 사용 후 디렉토리 삭제 책임.

    Raises:
        subprocess.CalledProcessError: git clone 실패.
        subprocess.TimeoutExpired: timeout 초과.
    """
    if target_dir is None:
        target_dir = Path(tempfile.mkdtemp(prefix="qapilot-clone-"))

    cmd = ["git", "clone", "--depth", "1", "--single-branch"]
    if branch:
        cmd.extend(["--branch", branch])
    cmd.extend([repo_url, str(target_dir)])

    _logger.info("git_clone_start", repo_url=_mask_url(repo_url),
                 target_dir=str(target_dir))
    subprocess.run(cmd, check=True, capture_output=True, text=True,
                   timeout=timeout, shell=False)

    # HEAD commit SHA 추출
    rev = subprocess.run(
        ["git", "-C", str(target_dir), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True, timeout=30, shell=False,
    )
    commit_sha = rev.stdout.strip()
    _logger.info("git_clone_done", commit_sha=commit_sha[:12])
    return target_dir, commit_sha


def _mask_url(url: str) -> str:
    """URL 의 token/password 마스킹 (log 안전)."""
    import re
    return re.sub(r"://[^@]+@", "://***@", url)


# ────────────────────────────────────────────────────────────────────────
# Walk
# ────────────────────────────────────────────────────────────────────────

def iter_source_files(
    repo_root: Path,
    *,
    whitelist: tuple[str, ...] = DEFAULT_WHITELIST,
    exclude_dirs: frozenset[str] = DEFAULT_EXCLUDE_DIRS,
    exclude_filenames: frozenset[str] = EXCLUDE_FILENAMES,
    max_size_bytes: int = MAX_FILE_SIZE_BYTES,
) -> Iterator[Path]:
    """repo_root 의 whitelist 확장자 파일 yield (exclude_dirs 재귀 skip)."""
    root = repo_root.resolve()
    if not root.exists():
        return

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        # 디렉토리 exclude — path 의 어떤 부분이라도 매칭되면 skip
        if any(part in exclude_dirs for part in path.relative_to(root).parts):
            continue
        if path.name in exclude_filenames:
            continue
        if path.suffix.lower() not in whitelist:
            continue
        try:
            if path.stat().st_size > max_size_bytes:
                continue
        except OSError:
            continue
        yield path


# ────────────────────────────────────────────────────────────────────────
# Public — dump_source_to_s3
# ────────────────────────────────────────────────────────────────────────

def dump_source_to_s3(
    service_id: str,
    repo_root: Path,
    commit_sha: str,
    *,
    whitelist: tuple[str, ...] = DEFAULT_WHITELIST,
    exclude_dirs: frozenset[str] = DEFAULT_EXCLUDE_DIRS,
    force: bool = False,
) -> SourceDumpResult:
    """repo_root 의 텍스트 파일을 S3 `services/{sid}/source/{sha}/{path}` 로 PUT.

    Args:
        service_id, commit_sha: S3 path 구성 + DB 매칭.
        repo_root: 절대 경로. caller (git clone 결과 또는 local 디렉토리).
        whitelist: 확장자 (DEFAULT_WHITELIST 권장).
        exclude_dirs: 재귀 skip 디렉토리.
        force: True 면 head_object cache skip 무시 + 무조건 PUT.

    Returns:
        SourceDumpResult — 통계 + 업로드 key 리스트 + 실패 paths.

    cache 정책:
        - upsert_source_file 의 head_object 기반 cache skip 그대로 동작
        - 같은 (sid, sha, path) 재호출 = files_skipped_cache 증가
        - force=True 면 무조건 PUT

    graceful:
        - 개별 파일 read/PUT 실패 = result.failed_paths 에 누적, 계속 진행
        - 전체 함수는 raise 없음
    """
    result = SourceDumpResult(service_id=service_id, commit_sha=commit_sha)
    root = repo_root.resolve()

    for path in iter_source_files(root, whitelist=whitelist, exclude_dirs=exclude_dirs):
        result.files_walked += 1
        try:
            rel = str(path.relative_to(root))
            content = path.read_bytes()
        except OSError as e:
            _logger.warning("source_read_failed", path=str(path), error=str(e))
            result.files_failed += 1
            result.failed_paths.append(str(path))
            continue

        # head_object match 면 skip — 미리 확인하면 file 통계 정확
        s3_key_expected = f"services/{service_id}/source/{commit_sha}/{rel}"
        if not force:
            head = s3_client.head_object(s3_key_expected)
            if head is not None and head.get("bytes") == len(content):
                result.files_skipped_cache += 1
                continue

        s3_key = upsert_source_file(
            service_id=service_id,
            commit_hash=commit_sha,
            relative_path=rel,
            content=content,
            force=force,
        )
        if s3_key is None:
            result.files_failed += 1
            result.failed_paths.append(rel)
            _logger.warning("source_upload_failed", path=rel)
            continue
        result.files_uploaded += 1
        result.uploaded_keys.append(s3_key)

    # 통계: walked = uploaded + cache_skipped + failed
    result.files_skipped_exclude = 0  # iter_source_files 가 walked 에 포함 안 시킴 — 통계 의미 별도
    _logger.info(
        "source_dump_done",
        service_id=service_id,
        commit_sha=commit_sha[:12],
        walked=result.files_walked,
        uploaded=result.files_uploaded,
        cache_skipped=result.files_skipped_cache,
        failed=result.files_failed,
    )
    return result
