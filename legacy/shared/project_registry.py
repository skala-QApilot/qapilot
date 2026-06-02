"""File-based project registry and codebase summary helpers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from qapilot.shared.codebase_context_loader import CodebaseContextLoader


class ProjectRecord(BaseModel):
    """Stored project metadata."""

    project_slug: str
    display_name: str
    local_path: str
    config_path: str
    index_path: str
    framework: str
    language: str
    created_at: str
    updated_at: str


class ProjectRegistry:
    """JSON-backed project registry."""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        self.base_dir = Path(base_dir) if base_dir is not None else Path(".")

    @property
    def registry_path(self) -> Path:
        return self.base_dir / ".qapilot" / "projects" / "registry.json"

    def load_all(self) -> dict[str, ProjectRecord]:
        """Load all stored projects."""
        path = self.registry_path
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        records: dict[str, ProjectRecord] = {}
        for slug, payload in raw.items():
            if not isinstance(payload, dict):
                continue
            try:
                records[slug] = ProjectRecord(**payload)
            except Exception:
                continue
        return records

    def get(self, project_slug: str) -> ProjectRecord | None:
        """Return a project by slug."""
        return self.load_all().get(project_slug)

    def upsert(self, project: ProjectRecord | dict[str, Any]) -> tuple[ProjectRecord, bool]:
        """Insert or update a project record.

        Returns:
            (record, created) where created is True for a brand-new slug.
        """
        record = project if isinstance(project, ProjectRecord) else ProjectRecord(**project)
        records = self.load_all()
        existing = records.get(record.project_slug)
        created = existing is None
        created_at = existing.created_at if existing else record.created_at
        updated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        stored = record.model_copy(update={"created_at": created_at, "updated_at": updated_at})
        records[stored.project_slug] = stored
        self._write_all(records)
        return stored, created

    def _write_all(self, records: dict[str, ProjectRecord]) -> None:
        path = self.registry_path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {slug: record.model_dump() for slug, record in sorted(records.items())}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def build_project_summary(project: ProjectRecord) -> dict[str, Any]:
    """Read codebase-index data and build UI-friendly summary information."""
    local_path = Path(project.local_path)
    index_path = Path(project.index_path)
    if index_path.name == "codebase-index":
        base_dir = index_path.parent.parent
    else:
        base_dir = local_path
    index_data = CodebaseContextLoader.load(base_dir)

    manifest = index_data.get("manifest") or {}
    endpoints = index_data.get("endpoints") or []
    models = index_data.get("models") or []

    endpoint_items: list[dict[str, Any]] = []
    for item in endpoints[:10]:
        if isinstance(item, dict):
            endpoint_items.append({
                "path": item.get("path", ""),
                "method": item.get("method", ""),
                "handler": item.get("handler", ""),
                "file": item.get("file", ""),
            })

    return {
        "file_count": int(manifest.get("file_count") or len(manifest.get("scanned_files") or [])),
        "endpoint_count": int(manifest.get("endpoint_count") or len(endpoints)),
        "model_count": len(models),
        "last_scanned_at": manifest.get("scan_timestamp") or project.updated_at,
        "endpoints": endpoint_items,
        "manifest": manifest,
        "has_index": bool(index_data.get("_dir_found")),
    }
