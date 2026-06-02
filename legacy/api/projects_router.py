"""Project registration and lookup API."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from qapilot.shared.project_registry import (
    ProjectRecord,
    ProjectRegistry,
    build_project_summary,
)

router = APIRouter(prefix="/api/projects", tags=["projects"])


class ProjectRegisterRequest(BaseModel):
    project_slug: str = Field(..., min_length=1)
    display_name: str = Field(..., min_length=1)
    local_path: str = Field(..., min_length=1)
    config_path: str = Field(..., min_length=1)
    index_path: str = Field(..., min_length=1)
    framework: str = Field(..., min_length=1)
    language: str = Field(..., min_length=1)


@router.post("/register")
async def register_project(http_request: Request, request: ProjectRegisterRequest) -> dict:
    """Register or update a project in the file-backed registry."""
    registry = ProjectRegistry()
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    stored, created = registry.upsert(
        ProjectRecord(
            project_slug=request.project_slug,
            display_name=request.display_name,
            local_path=request.local_path,
            config_path=request.config_path,
            index_path=request.index_path,
            framework=request.framework,
            language=request.language,
            created_at=now,
            updated_at=now,
        )
    )
    dashboard_base = str(http_request.base_url).rstrip("/")
    return {
        "project_slug": stored.project_slug,
        "dashboard_url": f"{dashboard_base}/{stored.project_slug}",
        "status": "created" if created else "updated",
    }


@router.get("/{project_slug}")
async def get_project(project_slug: str) -> dict:
    """Return registered project metadata and codebase summary."""
    registry = ProjectRegistry()
    project = registry.get(project_slug)
    if project is None:
        raise HTTPException(status_code=404, detail="등록되지 않은 프로젝트입니다.")

    summary = build_project_summary(project)
    return {
        "project": project.model_dump(),
        "summary": summary,
    }
