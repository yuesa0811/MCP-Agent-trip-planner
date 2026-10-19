"""密码哈希、用户创建与 JWT 签发集中在此，路由不接触哈希细节。"""

from datetime import datetime, timedelta, timezone

import jwt
from jwt.exceptions import InvalidTokenError
from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..models.entities import User


JWT_ALGORITHM = "HS256"
password_hash = PasswordHash.recommended()


class UsernameConflictError(RuntimeError):
    pass


class AuthConfigurationError(RuntimeError):
    pass


class TokenValidationError(RuntimeError):
    pass


def _jwt_key(settings: Settings) -> str:
    key = settings.jwt_secret_key.strip()
    if not settings.has_valid_jwt_secret():
        raise AuthConfigurationError("JWT_SECRET_KEY 未配置为至少 32 位的随机字符串。")
    return key


def validate_auth_configuration(settings: Settings | None = None) -> None:
    """在写入用户前确认登录所需密钥可用。"""
    _jwt_key(settings if settings is not None else get_settings())


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        return password_hash.verify(password, stored_hash)
    except Exception:
        # 未识别或损坏的哈希不能导致认证接口泄露内部异常。
        return False


def create_user(session: Session, username: str, password: str, *, is_admin: bool = False) -> User:
    """单次短事务创建用户；唯一索引负责处理并发重复注册。"""
    user = User(username=username, password_hash=hash_password(password), is_admin=is_admin)
    session.add(user)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise UsernameConflictError("用户名已存在。") from None
    session.refresh(user)
    return user


def authenticate_user(session: Session, username: str, password: str) -> User | None:
    user = session.scalar(select(User).where(User.username == username))
    if user is None or not verify_password(password, user.password_hash):
        return None
    return user


def create_access_token(
    user_id: int,
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
    expires_delta: timedelta | None = None,
) -> str:
    settings = settings if settings is not None else get_settings()
    issued_at = now or datetime.now(timezone.utc)
    expires = issued_at + (expires_delta or timedelta(minutes=settings.access_token_expire_minutes))
    return jwt.encode(
        {"sub": str(user_id), "iat": issued_at, "exp": expires},
        _jwt_key(settings), algorithm=JWT_ALGORITHM,
    )


def decode_access_token(token: str, settings: Settings | None = None) -> int:
    settings = settings if settings is not None else get_settings()
    try:
        payload = jwt.decode(
            token, _jwt_key(settings), algorithms=[JWT_ALGORITHM],
            options={"require": ["sub", "iat", "exp"]},
        )
        subject = payload["sub"]
        if not isinstance(subject, str) or not subject.isdigit() or int(subject) < 1:
            raise TokenValidationError("令牌中的用户标识无效。")
        return int(subject)
    except AuthConfigurationError:
        raise
    except (InvalidTokenError, KeyError, TokenValidationError):
        raise TokenValidationError("访问令牌无效或已过期。") from None
