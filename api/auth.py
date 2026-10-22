"""鉴权路由：注册 / 登录。

注册与登录都不需要 token（否则没法登录）。登录成功后返回 JWT，
后续请求在 Header 带 `Authorization: Bearer <token>`。
"""
from fastapi import APIRouter, HTTPException, status

from api.deps import create_access_token
from api.schemas import LoginRequest, RegisterRequest, TokenResponse
from storage import user_store

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/register", response_model=TokenResponse)
def register(req: RegisterRequest):
    user = user_store.register(req.username, req.password)
    if user is None:
        # 用户名冲突统一返回 409，不泄露"用户名是否存在"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="用户名已被占用",
        )
    token = create_access_token(user)
    return TokenResponse(access_token=token, username=user["username"])


@router.post("/login", response_model=TokenResponse)
def login(req: LoginRequest):
    user = user_store.authenticate(req.username, req.password)
    if user is None:
        # 用户名错 / 密码错统一返回 401，不向攻击者泄露"用户名是否存在"
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
        )
    token = create_access_token(user)
    return TokenResponse(access_token=token, username=user["username"])
