"""API 鉴权依赖：JWT 签发 + 当前用户解析。

设计要点：
  - JWT（HS256，密钥走环境变量 AGENT_JWT_SECRET）：无状态，服务端不存会话，
    前端/PWA/移动端都能直接带 Authorization: Bearer <token> 调用。
  - get_current_user 是 FastAPI 的 Depends 依赖：所有受保护端点声明
    `user=Depends(get_current_user)` 即可拿到已认证用户，且自动完成越权防护的前置校验。
  - 多租户隔离的落地方式：token 解出 user_id → 后续所有 DB 查询强制带 user_id，
    绝不能用别的用户的 session_id。
"""
import os
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import jwt, JWTError

from storage import user_store

# JWT 密钥绝不提供默认值：可猜测的 fallback 等于允许任何人伪造任意用户 token。
# 缺失时 fail-fast 拒绝启动，而不是静默降级到写死在源码里的值。
SECRET_KEY = os.environ.get("AGENT_JWT_SECRET")
if not SECRET_KEY:
    raise RuntimeError(
        "缺少环境变量 AGENT_JWT_SECRET。请设置一个足够长的随机串，例如：\n"
        '  python -c "import secrets; print(secrets.token_urlsafe(48))"'
    )
ALGORITHM = "HS256"

# HTTPBearer：从 Authorization: Bearer <token> 取凭证；缺头返回 403
_bearer = HTTPBearer(auto_error=True)


def create_access_token(user: dict) -> str:
    """用 user 信息签发 JWT。sub 存内部主键 id（不要用 uuid 当 sub，保持简短）。"""
    payload = {
        "sub": str(user["id"]),
        "username": user["username"],
        "role": user.get("role", "user"),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def get_current_user(
    creds: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
) -> dict:
    """解析 Bearer token → 反查用户。失败一律 401（无效 / 过期 / 用户不存在）。"""
    try:
        payload = jwt.decode(creds.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload["sub"])
    except (JWTError, KeyError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效或过期的 token",
        )
    user = user_store.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户不存在",
        )
    # 双保险：authenticate 已拦截禁用用户登录，但 token 可能是在禁用前签发的，
    # 所以每次请求都再校验一次 status，确保禁用即时生效（无需等 token 过期）
    if user.get("status") != 1:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="账号已被禁用",
        )
    return user
