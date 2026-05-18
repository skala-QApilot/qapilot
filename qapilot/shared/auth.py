"""파일 기반 사용자 저장소와 JWT 유틸.

Author: C
Created: 2026-05-15
"""

from __future__ import annotations

import json
import os
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jose import ExpiredSignatureError, JWTError, jwt
from passlib.context import CryptContext

from qapilot.shared.config import load_config
from qapilot.shared.errors import AuthError, ErrorCode
from qapilot.shared.logger import get_logger

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
_logger = get_logger("auth")


def load_users() -> list[dict]:
    """사용자 파일을 읽어 반환한다.

    Returns:
        저장된 사용자 dict 목록. 파일이 없으면 빈 목록.
    """
    path = _users_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        _logger.error("users_load_failed", path=str(path), error=str(e))
        return []
    return data if isinstance(data, list) else []


def save_users(users: list[dict]) -> None:
    """사용자 목록을 파일에 저장한다.

    Args:
        users: 저장할 사용자 dict 목록.
    """
    path = _users_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8")


def get_user_by_email(email: str) -> dict | None:
    """이메일로 사용자를 조회한다."""
    normalized = email.lower()
    return next((u for u in load_users() if u.get("email", "").lower() == normalized), None)


def get_user_by_id(user_id: str) -> dict | None:
    """사용자 ID로 사용자를 조회한다."""
    return next((u for u in load_users() if u.get("user_id") == user_id), None)


def create_user(email: str, password: str, name: str, role: str = "member") -> dict:
    """파일 기반 사용자 저장소에 사용자를 생성한다.

    Args:
        email: 사용자 이메일.
        password: 평문 비밀번호.
        name: 표시 이름.
        role: 사용자 역할.

    Returns:
        생성된 사용자 dict.
    """
    users = load_users()
    user = {
        "user_id": str(uuid.uuid4()),
        "email": email,
        "hashed_password": hash_password(password),
        "name": name,
        "role": role,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    users.append(user)
    save_users(users)
    return user


def verify_password(plain: str, hashed: str) -> bool:
    """평문 비밀번호와 해시를 검증한다."""
    return _pwd_context.verify(plain, hashed)


def hash_password(password: str) -> str:
    """bcrypt 해시를 생성한다."""
    return _pwd_context.hash(password)


def get_jwt_secret() -> str:
    """JWT secret을 반환하고, 파일이 없으면 자동 생성한다."""
    env_secret = os.getenv("QAPILOT_JWT_SECRET")
    if env_secret:
        return env_secret

    config = load_config()
    if config.auth.jwt_secret:
        return config.auth.jwt_secret

    path = _auth_dir() / "secret.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path.read_text(encoding="utf-8").strip()

    secret = secrets.token_hex(32)
    path.write_text(secret, encoding="utf-8")
    return secret


def create_access_token(user_id: str, email: str, role: str) -> str:
    """액세스 토큰을 생성한다."""
    config = load_config()
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=config.auth.access_token_expire_minutes
    )
    payload = {"sub": user_id, "email": email, "role": role, "type": "access", "exp": expire}
    return jwt.encode(payload, get_jwt_secret(), algorithm=config.auth.jwt_algorithm)


def create_refresh_token(user_id: str) -> str:
    """리프레시 토큰을 생성한다."""
    config = load_config()
    expire = datetime.now(timezone.utc) + timedelta(days=config.auth.refresh_token_expire_days)
    payload = {"sub": user_id, "type": "refresh", "exp": expire}
    return jwt.encode(payload, get_jwt_secret(), algorithm=config.auth.jwt_algorithm)


def decode_token(token: str) -> dict:
    """JWT를 검증하고 payload를 반환한다.

    Raises:
        AuthError: 토큰 만료 또는 무효 토큰인 경우.
    """
    try:
        config = load_config()
        return jwt.decode(token, get_jwt_secret(), algorithms=[config.auth.jwt_algorithm])
    except ExpiredSignatureError as e:
        raise AuthError(ErrorCode.AUTH_002, "토큰이 만료되었습니다.") from e
    except JWTError as e:
        raise AuthError(ErrorCode.AUTH_003, "유효하지 않은 토큰입니다.") from e


def verify_access_token(token: str) -> dict:
    """액세스 토큰을 검증하고 payload를 반환한다."""
    payload = decode_token(token)
    if payload.get("type") != "access":
        raise AuthError(ErrorCode.AUTH_003, "액세스 토큰이 아닙니다.")
    if is_token_revoked(token):
        raise AuthError(ErrorCode.AUTH_003, "폐기된 토큰입니다.")
    return payload


def revoke_token(token: str) -> None:
    """액세스 토큰을 폐기 목록에 추가한다."""
    payload = decode_token(token)
    if payload.get("type") != "access":
        raise AuthError(ErrorCode.AUTH_003, "액세스 토큰이 아닙니다.")

    revoked = _load_revoked_tokens()
    exp = _exp_as_timestamp(payload.get("exp"))
    revoked.append({"token": token, "exp": exp})
    _save_revoked_tokens(_prune_revoked_tokens(revoked))


def is_token_revoked(token: str) -> bool:
    """토큰이 폐기 목록에 있는지 반환한다."""
    revoked = _prune_revoked_tokens(_load_revoked_tokens())
    _save_revoked_tokens(revoked)
    return any(item.get("token") == token for item in revoked)


def public_user(user: dict) -> dict:
    """응답용 사용자 정보를 반환한다."""
    return {
        "user_id": user["user_id"],
        "email": user["email"],
        "name": user["name"],
        "role": user["role"],
    }


def _qapilot_dir() -> Path:
    """현재 설정 기준 .qapilot 디렉터리 경로를 반환한다."""
    config = load_config()
    repo_path = config.project.repo_path
    base = Path(repo_path) if repo_path else Path(".")
    return base / ".qapilot"


def _auth_dir() -> Path:
    return _qapilot_dir() / "auth"


def _users_path() -> Path:
    return _qapilot_dir() / "users.json"


def _revoked_tokens_path() -> Path:
    return _auth_dir() / "revoked_tokens.json"


def _load_revoked_tokens() -> list[dict]:
    path = _revoked_tokens_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def _save_revoked_tokens(tokens: list[dict]) -> None:
    path = _revoked_tokens_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tokens, ensure_ascii=False, indent=2), encoding="utf-8")


def _prune_revoked_tokens(tokens: list[dict]) -> list[dict]:
    now = int(time.time())
    return [item for item in tokens if int(item.get("exp", 0)) > now]


def _exp_as_timestamp(exp: Any) -> int:
    if isinstance(exp, datetime):
        return int(exp.timestamp())
    if isinstance(exp, (int, float)):
        return int(exp)
    return int(time.time())
