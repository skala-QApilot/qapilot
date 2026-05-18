"""service scope 파일 API 라우터.

Author: C
Created: 2026-05-15
"""

from __future__ import annotations

import re
from email.parser import BytesParser
from email.policy import default
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from qapilot.api.deps import get_current_user
from qapilot.api.response import fail, ok
from qapilot.shared.errors import ErrorCode
from qapilot.shared.file_store import (
    add_file_version,
    create_file,
    delete_file,
    get_file_by_id,
    get_file_diff,
    get_file_versions,
    load_files_meta,
    update_file_meta,
)
from qapilot.shared.service_store import get_service_by_id

router = APIRouter(prefix="/api/services/{service_id}", tags=["files"])


@router.get("/files")
async def list_files(service_id: str, user: dict = Depends(get_current_user)) -> Any:
    """파일 목록을 조회한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    files = load_files_meta(service)
    return ok({"files": files, "count": len(files)})


@router.post("/files")
async def upload_file(
    service_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
) -> Any:
    """도메인 파일을 업로드한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    upload = await _read_upload(request)
    if not upload:
        return fail("REQUEST_400", "file 필드가 필요합니다.")
    meta = create_file(service, upload["filename"], upload["content"], upload["mime_type"])
    return JSONResponse(status_code=201, content=ok({"file": meta}))


@router.get("/files/{file_id}/versions")
async def list_file_versions(
    service_id: str,
    file_id: str,
    user: dict = Depends(get_current_user),
) -> Any:
    """파일 버전 목록을 조회한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    if not get_file_by_id(service, file_id):
        return _file_not_found()
    return ok({"versions": get_file_versions(service, file_id)})


@router.post("/files/{file_id}/versions")
async def upload_file_version(
    service_id: str,
    file_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
) -> Any:
    """파일 새 버전을 업로드한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    upload = await _read_upload(request)
    if not upload:
        return fail("REQUEST_400", "file 필드가 필요합니다.")
    meta = add_file_version(service, file_id, upload["content"], upload["mime_type"])
    return JSONResponse(status_code=201, content=ok({"file": meta})) if meta else _file_not_found()


@router.patch("/files/{file_id}")
async def patch_file(
    service_id: str,
    file_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
) -> Any:
    """파일 메타를 수정한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    body = await _json_body(request)
    meta = update_file_meta(service, file_id, body.get("name"), body.get("reflected"))
    return ok({"file": meta}) if meta else _file_not_found()


@router.delete("/files/{file_id}", status_code=204, response_class=Response)
async def remove_file(service_id: str, file_id: str, user: dict = Depends(get_current_user)):
    """파일을 삭제한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    if not delete_file(service, file_id):
        return _file_not_found()
    return Response(status_code=204)


@router.get("/files/{file_id}/diff")
async def file_diff(
    service_id: str,
    file_id: str,
    user: dict = Depends(get_current_user),
) -> Any:
    """파일 최신 버전 diff를 조회한다."""
    service = _service_or_none(service_id)
    if not service:
        return _service_not_found()
    if not get_file_by_id(service, file_id):
        return _file_not_found()
    return ok({"diff": get_file_diff(service, file_id)})


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


async def _read_upload(request: Request) -> dict | None:
    content_type = request.headers.get("content-type", "")
    body = await request.body()
    if not content_type.startswith("multipart/form-data"):
        return None
    message = BytesParser(policy=default).parsebytes(
        f"Content-Type: {content_type}\r\n\r\n".encode() + body
    )
    for part in message.iter_parts():
        disposition = part.get("Content-Disposition", "")
        if 'name="file"' not in disposition:
            continue
        filename = _filename(disposition) or "upload.bin"
        return {
            "filename": filename,
            "content": part.get_payload(decode=True) or b"",
            "mime_type": part.get_content_type(),
        }
    return None


def _filename(disposition: str) -> str | None:
    match = re.search(r'filename="([^"]+)"', disposition)
    return match.group(1) if match else None


def _service_or_none(service_id: str) -> dict | None:
    return get_service_by_id(service_id)


def _service_not_found() -> JSONResponse:
    return fail(ErrorCode.SERVICE_001, "서비스를 찾을 수 없습니다.")


def _file_not_found() -> JSONResponse:
    return fail(ErrorCode.FILE_001, "파일을 찾을 수 없습니다.")
