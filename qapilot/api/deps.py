"""FastAPI 인증 의존성.

Author: C
Created: 2026-05-15
"""

from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer

from qapilot.shared.auth import get_user_by_id, verify_access_token
from qapilot.shared.errors import AuthError, ErrorCode

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


async def get_current_user(token: str = Depends(oauth2_scheme)) -> dict:
    """JWT 검증 후 현재 사용자를 반환한다.

    Args:
        token: Authorization Bearer 토큰.

    Returns:
        현재 사용자 dict.

    Raises:
        AuthError: 토큰 사용자 ID에 해당하는 사용자가 없을 때.
    """
    payload = verify_access_token(token)
    user = get_user_by_id(payload["sub"])
    if not user:
        raise AuthError(ErrorCode.AUTH_005, "사용자를 찾을 수 없습니다.")
    return user


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """관리자 권한을 검증한다.

    Args:
        user: 현재 사용자.

    Returns:
        관리자 사용자 dict.

    Raises:
        AuthError: 관리자 권한이 없을 때.
    """
    if user["role"] != "admin":
        raise AuthError(ErrorCode.AUTH_004, "관리자 권한이 필요합니다.")
    return user
