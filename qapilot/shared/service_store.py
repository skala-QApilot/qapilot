"""파일 기반 서비스 메타데이터 저장소.

MVP에서 service_id는 React UI 계약 호환을 위한 논리 ID이며, 실제 산출물은
대상 테스트 프로젝트의 .qapilot 디렉터리에 저장된다.

Author: C
Created: 2026-05-15
"""

from __future__ import annotations

import json
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path

from qapilot.shared.config import load_config
from qapilot.shared.logger import get_logger

_logger = get_logger("service_store")


def load_services() -> list[dict]:
    """서비스 파일을 읽어 반환한다.

    Returns:
        저장된 서비스 dict 목록. 파일이 없으면 빈 목록.
    """
    path = _services_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        _logger.error("services_load_failed", path=str(path), error=str(e))
        return []
    return data if isinstance(data, list) else []


def save_services(services: list[dict]) -> None:
    """서비스 목록을 파일에 저장한다.

    Args:
        services: 저장할 서비스 dict 목록.
    """
    path = _services_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(services, ensure_ascii=False, indent=2), encoding="utf-8")


def get_service_by_id(service_id: str) -> dict | None:
    """서비스 ID로 서비스를 조회한다."""
    return next((s for s in load_services() if s.get("service_id") == service_id), None)


def get_service_by_token(auth_token: str) -> dict | None:
    """서비스 인증 토큰으로 서비스를 조회한다."""
    return next((s for s in load_services() if s.get("auth_token") == auth_token), None)


def create_service(name: str, description: str, target_root: str) -> dict:
    """서비스 메타데이터를 생성한다.

    Args:
        name: 서비스 이름.
        description: 서비스 설명.
        target_root: 대상 테스트 프로젝트 루트 경로.

    Returns:
        생성된 서비스 dict.
    """
    services = load_services()
    target_path = Path(target_root).expanduser().resolve()
    qapilot_dir = target_path / ".qapilot"
    qapilot_dir.mkdir(parents=True, exist_ok=True)

    now = _utc_now()
    service = {
        "service_id": str(uuid.uuid4()),
        "name": name,
        "description": description,
        "target_root": str(target_path),
        "qapilot_dir": str(qapilot_dir),
        "assigned_url": _assigned_url(),
        "auth_token": secrets.token_urlsafe(32),
        "token_issued_at": now,
        "token_expires_at": None,
        "created_at": now,
    }
    services.append(service)
    save_services(services)
    return service


def update_service(
    service_id: str,
    name: str | None,
    description: str | None,
) -> dict | None:
    """서비스 이름과 설명을 수정한다."""
    services = load_services()
    for service in services:
        if service.get("service_id") != service_id:
            continue
        if name is not None:
            service["name"] = name
        if description is not None:
            service["description"] = description
        save_services(services)
        return service
    return None


def rotate_token(service_id: str) -> dict | None:
    """서비스 인증 토큰을 재발급하고 기존 토큰을 폐기 목록에 기록한다."""
    services = load_services()
    for service in services:
        if service.get("service_id") != service_id:
            continue
        _revoke_service_token(service)
        service["auth_token"] = secrets.token_urlsafe(32)
        service["token_issued_at"] = _utc_now()
        service["token_expires_at"] = None
        save_services(services)
        return service
    return None


def public_service(service: dict) -> dict:
    """응답용 서비스 정보를 반환한다."""
    return dict(service)


def public_credentials(service: dict) -> dict:
    """응답용 서비스 접속 정보를 반환한다."""
    return {
        "assigned_url": service["assigned_url"],
        "auth_token": service["auth_token"],
        "token_issued_at": service["token_issued_at"],
        "token_expires_at": service.get("token_expires_at"),
    }


def _qapilot_dir() -> Path:
    """현재 설정 기준 .qapilot 디렉터리 경로를 반환한다."""
    config = load_config()
    repo_path = config.project.repo_path
    base = Path(repo_path) if repo_path else Path(".")
    return base / ".qapilot"


def _services_path() -> Path:
    return _qapilot_dir() / "services.json"


def _revoked_service_tokens_path() -> Path:
    return _qapilot_dir() / "auth" / "revoked_service_tokens.json"


def _assigned_url() -> str:
    config = load_config()
    return config.server.url or "http://localhost:8001"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _revoke_service_token(service: dict) -> None:
    token = service.get("auth_token")
    if not token:
        return

    revoked = _load_revoked_service_tokens()
    revoked.append(
        {
            "service_id": service.get("service_id"),
            "auth_token": token,
            "revoked_at": _utc_now(),
        }
    )
    _save_revoked_service_tokens(revoked)


def _load_revoked_service_tokens() -> list[dict]:
    path = _revoked_service_tokens_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def _save_revoked_service_tokens(tokens: list[dict]) -> None:
    path = _revoked_service_tokens_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tokens, ensure_ascii=False, indent=2), encoding="utf-8")
