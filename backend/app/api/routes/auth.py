"""公开注册、JSON 登录和当前用户接口。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ...config import get_settings
from ...models.entities import User
from ...models.schemas import (
    AuthTokenResponse, UserLoginRequest, UserPublic, UserRegisterRequest,
)
from ...services.auth_service import (
    AuthConfigurationError, UsernameConflictError, authenticate_user,
    create_access_token, create_user, validate_auth_configuration,
)
from ..deps import get_current_user, get_db


router = APIRouter(prefix="/auth", tags=["用户认证"])


@router.post("/register", response_model=UserPublic, status_code=status.HTTP_201_CREATED)
def register(request: UserRegisterRequest, session: Annotated[Session, Depends(get_db)]):
    """同步路由由 FastAPI 在线程池执行 Argon2 哈希，不阻塞事件循环。"""
    try:
        validate_auth_configuration()
        user = create_user(
            session, request.username, request.password.get_secret_value(), is_admin=False,
        )
    except AuthConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except UsernameConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return UserPublic.model_validate(user)


@router.post("/login", response_model=AuthTokenResponse)
def login(request: UserLoginRequest, session: Annotated[Session, Depends(get_db)]):
    user = authenticate_user(session, request.username, request.password.get_secret_value())
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误。", headers={"WWW-Authenticate": "Bearer"},
        )
    settings = get_settings()
    try:
        token = create_access_token(user.id, settings)
    except AuthConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    return AuthTokenResponse(
        access_token=token,
        expires_in=settings.access_token_expire_minutes * 60,
        user=UserPublic.model_validate(user),
    )


@router.get("/me", response_model=UserPublic)
def current_user(user: Annotated[User, Depends(get_current_user)]):
    return UserPublic.model_validate(user)
