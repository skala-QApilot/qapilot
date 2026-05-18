"""Project slug utilities."""

from __future__ import annotations

import re
from pathlib import Path


_NON_SLUG_CHARS = re.compile(r"[^a-z0-9-]+")
_MULTI_HYPHENS = re.compile(r"-+")


def slugify_project_name(value: str) -> str:
    """Convert a project name into a URL-safe slug.

    Rules:
    - spaces become hyphens
    - existing hyphens stay hyphens
    - special characters are removed
    - uppercase becomes lowercase
    - repeated separators collapse to a single hyphen
    - leading/trailing hyphens are stripped
    """
    text = value.strip().lower().replace(" ", "-")
    text = _NON_SLUG_CHARS.sub("", text)
    text = _MULTI_HYPHENS.sub("-", text)
    return text.strip("-") or "project"


def project_slug_from_path(path: str | Path) -> str:
    """Generate a slug from the final directory name of a path."""
    return slugify_project_name(Path(path).resolve().name)
