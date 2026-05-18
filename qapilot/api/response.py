"""FastAPI 공통 응답 헬퍼.

Author: C
Created: 2026-05-15
"""

from fastapi.responses import JSONResponse


def ok(data: dict) -> dict:
    """일반 API 성공 응답을 반환한다.

    Args:
        data: 응답 데이터.

    Returns:
        공통 성공 응답 dict.
    """
    return {"success": True, "data": data}


def agent_ok(data: dict, trace_id: str, confidence: float) -> dict:
    """Agent API 성공 응답을 반환한다.

    Args:
        data: 응답 데이터.
        trace_id: 실행 추적 ID.
        confidence: Agent 결과 신뢰도.

    Returns:
        Agent 공통 성공 응답 dict.
    """
    return {
        "success": True,
        "data": data,
        "trace_id": trace_id,
        "confidence": confidence,
    }


def fail(code: str, message: str) -> JSONResponse:
    """공통 실패 응답을 반환한다.

    Args:
        code: 에러 코드.
        message: 사용자에게 전달할 에러 메시지.

    Returns:
        에러 코드에 맞는 HTTP 상태의 JSONResponse.
    """
    return JSONResponse(
        status_code=_status_from_code(code),
        content={"success": False, "error": {"code": code, "message": message}},
    )


def _status_from_code(code: str) -> int:
    """에러 코드에 대응하는 HTTP 상태 코드를 반환한다."""
    status_map = {
        "REQUEST_400": 400,
        "INTERNAL_AUTH_001": 401,
        "AGENT_API_001": 500,
        "AGENT_API_002": 404,
        "AGENT_API_003": 400,
    }
    return status_map.get(code, 500)
