"""알림 파일 저장소.

Author: C
Created: 2026-05-15
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path


def load_notifications(service: dict) -> list[dict]:
    """알림 목록을 최신순으로 반환한다."""
    path = _notifications_path(service)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    items = data if isinstance(data, list) else []
    return sorted(items, key=lambda item: item.get("created_at", ""), reverse=True)


def get_notification_by_id(service: dict, notification_id: str) -> dict | None:
    """notification_id로 알림을 조회한다."""
    return next(
        (item for item in load_notifications(service) if item.get("notification_id") == notification_id),
        None,
    )


def mark_as_read(service: dict, notification_id: str) -> dict | None:
    """알림을 읽음 처리한다."""
    notifications = load_notifications(service)
    for item in notifications:
        if item.get("notification_id") != notification_id:
            continue
        item["is_read"] = True
        item["read_at"] = _utc_now()
        _save(service, notifications)
        return item
    return None


def create_notification(
    service: dict,
    title: str,
    message: str,
    type: str = "info",
) -> dict:
    """알림을 생성한다."""
    notification = {
        "notification_id": str(uuid.uuid4()),
        "title": title,
        "message": message,
        "type": type,
        "is_read": False,
        "created_at": _utc_now(),
        "read_at": None,
    }
    notifications = load_notifications(service)
    notifications.append(notification)
    _save(service, notifications)
    return notification


def _notifications_path(service: dict) -> Path:
    return Path(str(service["qapilot_dir"])) / "notifications.json"


def _save(service: dict, notifications: list[dict]) -> None:
    path = _notifications_path(service)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(notifications, ensure_ascii=False, indent=2), encoding="utf-8")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
