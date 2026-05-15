"""멤버 파일 저장소.

Author: C
Created: 2026-05-15
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path


def load_members(service: dict) -> list[dict]:
    """멤버 목록을 반환한다."""
    path = _members_path(service)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def get_member_by_id(service: dict, member_id: str) -> dict | None:
    """member_id로 멤버를 조회한다."""
    return next((m for m in load_members(service) if m.get("member_id") == member_id), None)


def invite_member(
    service: dict,
    email: str,
    name: str,
    role: str = "member",
    team: str | None = None,
) -> dict:
    """멤버를 초대한다. 같은 email이 있으면 기존 멤버를 반환한다."""
    members = load_members(service)
    existing = next((m for m in members if m.get("email", "").lower() == email.lower()), None)
    if existing:
        return existing
    member = {
        "member_id": str(uuid.uuid4()),
        "email": email,
        "name": name,
        "role": role,
        "team": team,
        "joined_at": _utc_now(),
        "status": "active",
    }
    members.append(member)
    _save(service, members)
    return member


def update_member(
    service: dict,
    member_id: str,
    role: str | None,
    team: str | None,
) -> dict | None:
    """멤버 role/team을 수정한다."""
    members = load_members(service)
    for member in members:
        if member.get("member_id") != member_id:
            continue
        if role is not None:
            member["role"] = role
        if team is not None:
            member["team"] = team
        _save(service, members)
        return member
    return None


def remove_member(service: dict, member_id: str) -> bool:
    """멤버를 제거한다."""
    members = load_members(service)
    next_members = [m for m in members if m.get("member_id") != member_id]
    if len(next_members) == len(members):
        return False
    _save(service, next_members)
    return True


def export_members_csv(service: dict) -> str:
    """멤버 목록을 CSV 문자열로 반환한다."""
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=["member_id", "name", "email", "role", "team", "joined_at"],
    )
    writer.writeheader()
    for member in load_members(service):
        writer.writerow({field: member.get(field, "") for field in writer.fieldnames})
    return output.getvalue()


def _members_path(service: dict) -> Path:
    return Path(str(service["qapilot_dir"])) / "members.json"


def _save(service: dict, members: list[dict]) -> None:
    path = _members_path(service)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(members, ensure_ascii=False, indent=2), encoding="utf-8")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
