"""数据库 Session、当前用户和管理员权限依赖。"""

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..models.entities import User
from ..services.auth_service import (
    AuthConfigurationError, TokenValidationError, decode_access_token,
)


bearer_scheme = HTTPBearer(auto_error=False)


def get_db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: Annotated[Session, Depends(get_db)],
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="需要有效的 Bearer 访问令牌。",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        user_id = decode_access_token(credentials.credentials)
    except AuthConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except TokenValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc), headers={"WWW-Authenticate": "Bearer"},
        ) from None
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="访问令牌对应的用户不存在。",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def require_admin(current_user: Annotated[User, Depends(get_current_user)]) -> User:
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="需要管理员权限。")
    return current_user
