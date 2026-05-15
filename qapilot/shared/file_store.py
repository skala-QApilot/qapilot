"""도메인 파일 메타데이터와 버전 파일 저장소.

Author: C
Created: 2026-05-15
"""

from __future__ import annotations

import difflib
import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from qapilot.shared.logger import get_logger

_logger = get_logger("file_store")


def load_files_meta(service: dict) -> list[dict]:
    """files.json을 읽어 파일 메타 목록을 반환한다."""
    path = _files_meta_path(service)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        _logger.error("files_meta_load_failed", path=str(path), error=str(e))
        return []
    return data if isinstance(data, list) else []


def save_files_meta(service: dict, files: list[dict]) -> None:
    """파일 메타 목록을 저장한다."""
    path = _files_meta_path(service)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(files, ensure_ascii=False, indent=2), encoding="utf-8")


def get_file_by_id(service: dict, file_id: str) -> dict | None:
    """file_id로 파일 메타를 조회한다."""
    return next((f for f in load_files_meta(service) if f.get("file_id") == file_id), None)


def create_file(service: dict, name: str, content: bytes, mime_type: str) -> dict:
    """새 도메인 파일을 저장하고 메타를 생성한다."""
    files = load_files_meta(service)
    file_id = str(uuid.uuid4())
    meta = _meta(service, file_id, name, 1, content, mime_type)
    _write_file(service, file_id, meta["version_number"], name, content)
    files.append(meta)
    save_files_meta(service, files)
    return meta


def add_file_version(service: dict, file_id: str, content: bytes, mime_type: str) -> dict | None:
    """기존 파일에 새 버전을 추가한다."""
    files = load_files_meta(service)
    for meta in files:
        if meta.get("file_id") != file_id:
            continue
        version_number = int(meta.get("version_number", 1)) + 1
        _write_file(service, file_id, version_number, meta["name"], content)
        meta.update(_meta(service, file_id, meta["name"], version_number, content, mime_type))
        save_files_meta(service, files)
        return meta
    return None


def get_file_versions(service: dict, file_id: str) -> list[dict]:
    """파일의 저장된 버전 목록을 반환한다."""
    file_dir = _domain_dir(service) / file_id
    if not file_dir.exists():
        return []
    versions = [_version_meta(path) for path in file_dir.iterdir() if path.is_file()]
    return sorted(versions, key=lambda item: item["version_number"])


def update_file_meta(
    service: dict,
    file_id: str,
    name: str | None,
    reflected: bool | None,
) -> dict | None:
    """파일 메타의 name/reflected만 수정한다."""
    files = load_files_meta(service)
    for meta in files:
        if meta.get("file_id") != file_id:
            continue
        if name is not None:
            meta["name"] = name
        if reflected is not None:
            meta["reflected"] = reflected
        save_files_meta(service, files)
        return meta
    return None


def delete_file(service: dict, file_id: str) -> bool:
    """파일 메타와 버전 디렉터리를 삭제한다."""
    files = load_files_meta(service)
    next_files = [f for f in files if f.get("file_id") != file_id]
    if len(next_files) == len(files):
        return False
    save_files_meta(service, next_files)
    shutil.rmtree(_domain_dir(service) / file_id, ignore_errors=True)
    return True


def get_file_diff(service: dict, file_id: str) -> dict:
    """최신 버전과 이전 버전의 diff를 반환한다."""
    versions = get_file_versions(service, file_id)
    if len(versions) < 2:
        return {"type": "initial", "message": "최초 버전"}
    prev = _domain_dir(service) / file_id / versions[-2]["filename"]
    latest = _domain_dir(service) / file_id / versions[-1]["filename"]
    try:
        before = prev.read_text(encoding="utf-8").splitlines()
        after = latest.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return {"type": "binary", "message": "바이너리 파일은 diff 미지원"}
    diff = "\n".join(difflib.unified_diff(before, after, fromfile=prev.name, tofile=latest.name))
    return {"type": "text", "diff": diff}


def _domain_dir(service: dict) -> Path:
    return Path(str(service["qapilot_dir"])) / "domain"


def _files_meta_path(service: dict) -> Path:
    return _domain_dir(service) / "files.json"


def _meta(service: dict, file_id: str, name: str, version_number: int, content: bytes, mime: str) -> dict:
    return {
        "file_id": file_id,
        "name": name,
        "version": f"v{version_number}",
        "version_number": version_number,
        "reflected": False,
        "uploaded_at": _utc_now(),
        "size_bytes": len(content),
        "mime_type": mime,
        "storage_path": _storage_path(file_id, version_number, name),
    }


def _write_file(service: dict, file_id: str, version_number: int, name: str, content: bytes) -> None:
    path = _domain_dir(service) / file_id / f"v{version_number}_{name}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _storage_path(file_id: str, version_number: int, name: str) -> str:
    return f".qapilot/domain/{file_id}/v{version_number}_{name}"


def _version_meta(path: Path) -> dict:
    prefix = path.name.split("_", 1)[0]
    version_number = int(prefix.removeprefix("v"))
    return {
        "version": prefix,
        "version_number": version_number,
        "filename": path.name,
        "uploaded_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
