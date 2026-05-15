"""service scope run 로그/프리뷰 API 라우터.

Author: C
Created: 2026-05-15
"""

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from qapilot.api.deps import get_current_user
from qapilot.api.response import fail, ok
from qapilot.shared.errors import ErrorCode
from qapilot.shared.service_store import get_service_by_id
from qapilot.shared.trace_store import load_trace

router = APIRouter(prefix="/api/services/{service_id}", tags=["runs"])


@router.get("/runs/{run_id}/logs")
async def get_run_logs(
    service_id: str,
    run_id: str,
    limit: int = 100,
    offset: int = 0,
    user: dict = Depends(get_current_user),
) -> Any:
    """run 로그 파일을 라인 단위로 조회한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    lines = _read_log_lines(service, run_id)
    page = lines[offset : offset + limit]
    return ok({"logs": page, "total_lines": len(lines), "run_id": run_id})


@router.get("/runs/{run_id}/preview")
async def get_run_preview(
    service_id: str,
    run_id: str,
    user: dict = Depends(get_current_user),
) -> Any:
    """MVP 프리뷰 placeholder를 반환한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    trace = load_trace(service, run_id)
    return ok(
        {
            "run_id": run_id,
            "status": trace["status"] if trace else "unknown",
            "preview_available": False,
            "message": "브라우저 프리뷰는 추후 지원 예정입니다.",
            "latest_screenshot": None,
        }
    )


def _read_log_lines(service: dict, run_id: str) -> list[str]:
    path = Path(str(service["qapilot_dir"])) / "logs" / f"{run_id}.log"
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def _service_or_none(service_id: str) -> dict | None:
    return get_service_by_id(service_id)


def _service_not_found() -> JSONResponse:
    return fail(ErrorCode.SERVICE_001, "서비스를 찾을 수 없습니다.")
