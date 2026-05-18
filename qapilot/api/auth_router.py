"""인증 API 라우터.

Author: C
Created: 2026-05-15
"""

from typing import Any

from fastapi import APIRouter, Depends, Request

from qapilot.api.deps import get_current_user, oauth2_scheme
from qapilot.api.response import fail, ok
from qapilot.shared.auth import (
    create_access_token,
    create_refresh_token,
    create_user,
    decode_token,
    get_user_by_email,
    get_user_by_id,
    load_users,
    public_user,
    revoke_token,
    verify_password,
)
from qapilot.shared.errors import AuthError, ErrorCode
from qapilot.shared.logger import get_logger

router = APIRouter(prefix="/api/auth", tags=["auth"])
logger = get_logger("api.auth")


@router.post("/login")
async def login(request: Request) -> dict:
    """사용자 로그인 후 access/refresh token을 발급한다."""
    body = await _json_body(request)
    error = _require_fields(body, ["email", "password"])
    if error:
        return error

    email = str(body["email"]).strip()
    password = str(body["password"])
    user = _bootstrap_admin_if_empty(email, password)
    if user is None:
        user = get_user_by_email(email)

    if not user or not verify_password(password, user["hashed_password"]):
        return fail(ErrorCode.AUTH_001, "이메일 또는 비밀번호가 올바르지 않습니다.")

    access_token = create_access_token(user["user_id"], user["email"], user["role"])
    refresh_token = create_refresh_token(user["user_id"])
    logger.info("login_success", user_id=user["user_id"], email=user["email"])
    return ok(
        {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
            "user": public_user(user),
        }
    )


@router.post("/logout")
async def logout(
    token: str = Depends(oauth2_scheme),
    user: dict = Depends(get_current_user),
) -> dict:
    """현재 access token을 폐기한다."""
    revoke_token(token)
    logger.info("logout_success", user_id=user["user_id"])
    return ok({"message": "로그아웃되었습니다."})


@router.post("/refresh")
async def refresh(request: Request) -> dict:
    """refresh token으로 새 access token을 발급한다."""
    body = await _json_body(request)
    error = _require_fields(body, ["refresh_token"])
    if error:
        return error

    payload = decode_token(str(body["refresh_token"]))
    if payload.get("type") != "refresh":
        raise AuthError(ErrorCode.AUTH_003, "리프레시 토큰이 아닙니다.")

    user = get_user_by_id(payload["sub"])
    if not user:
        raise AuthError(ErrorCode.AUTH_005, "사용자를 찾을 수 없습니다.")

    access_token = create_access_token(user["user_id"], user["email"], user["role"])
    return ok({"access_token": access_token})


@router.get("/me")
async def me(user: dict = Depends(get_current_user)) -> dict:
    """현재 로그인 사용자를 조회한다."""
    return ok(public_user(user))


@router.get("/oauth/google")
async def google_oauth() -> dict:
    """Google OAuth 준비 상태를 반환한다."""
    return ok(
        {
            "available": False,
            "message": "Google OAuth는 추후 지원 예정입니다.",
            "redirect_url": None,
        }
    )


async def _json_body(request: Request) -> dict[str, Any]:
    """요청 JSON body를 안전하게 읽는다."""
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _require_fields(body: dict[str, Any], fields: list[str]):
    """필수 필드 존재 여부를 검증한다."""
    missing = [field for field in fields if field not in body or body[field] in (None, "")]
    if missing:
        return fail("REQUEST_400", f"{', '.join(missing)} 필드가 필요합니다.")
    return None


def _bootstrap_admin_if_empty(email: str, password: str) -> dict | None:
    """사용자가 없으면 최초 로그인 정보로 admin 계정을 만든다."""
    if load_users():
        return None

    name = email.split("@", 1)[0] or "admin"
    user = create_user(email=email, password=password, name=name, role="admin")
    logger.info("bootstrap_admin_created", user_id=user["user_id"], email=email)
    return user
