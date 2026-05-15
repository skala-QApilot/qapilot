"""CodebaseContextLoader — .qapilot/codebase-index/ 메타데이터 로더.

endpoints.json, models.json, callgraph.json, manifest.json을 읽어
dict로 반환한다. 파일이 없거나 디렉토리 자체가 없어도 안전하게
빈 구조를 반환한다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_INDEX_SUBDIR = Path(".qapilot") / "codebase-index"


class CodebaseContextLoader:
    """codebase-index 디렉토리의 JSON 메타데이터 파일을 로드한다."""

    _FILES: dict[str, str] = {
        "endpoints": "endpoints.json",
        "models": "models.json",
        "callgraph": "callgraph.json",
        "manifest": "manifest.json",
    }

    @classmethod
    def load(cls, base_dir: Path | None = None) -> dict[str, Any]:
        """codebase-index JSON 파일을 로드하여 반환한다.

        Args:
            base_dir: 인덱스 탐색 기준 디렉토리. None이면 현재 작업 디렉토리.

        Returns:
            dict with keys:
                endpoints  — list, default []
                models     — list, default []
                callgraph  — dict, default {}
                manifest   — dict, default {}
                _dir_found — bool (인덱스 디렉토리 존재 여부)
        """
        base = Path(base_dir) if base_dir else Path(".")
        index_dir = base / _INDEX_SUBDIR

        result: dict[str, Any] = {
            "endpoints": [],
            "models": [],
            "callgraph": {},
            "manifest": {},
            "_dir_found": False,
        }

        if not index_dir.is_dir():
            return result

        result["_dir_found"] = True

        for key, filename in cls._FILES.items():
            path = index_dir / filename
            if not path.is_file():
                continue
            try:
                result[key] = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

        return result
