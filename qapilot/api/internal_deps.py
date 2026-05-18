"""FastAPI 내부 API 인증 의존성.

Author: C
Created: 2026-05-18
"""

import os

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from qapilot.shared.errors import AuthError, ErrorCode

_bearer = HTTPBearer(auto_error=False)


async def verify_internal_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    """Spring Boot -> FastAPI 내부 호출 토큰을 검증한다."""
    expected = os.getenv("QAPILOT_INTERNAL_API_TOKEN")
    if not expected or not credentials or credentials.credentials != expected:
        raise AuthError(ErrorCode.INTERNAL_AUTH_001, "내부 API 토큰이 유효하지 않습니다.")
