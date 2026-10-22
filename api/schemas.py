"""API 层 Pydantic 模型：请求/响应体校验与序列化。

为什么用 Pydantic：FastAPI 原生基于 Pydantic v2，所有请求体自动校验类型/必填，
错误时返回标准 422；响应模型自动过滤掉不该外泄的字段（如 password_hash）。
"""
from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=1, description="登录名")
    password: str = Field(..., min_length=1, description="密码（bcrypt 哈希后落库，绝不明文）")


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str


class SessionCreateRequest(BaseModel):
    title: str | None = None


class SessionResponse(BaseModel):
    id: str
    title: str
    created_at: str | None = None


class ChatRequest(BaseModel):
    session_id: str = Field(..., description="目标会话 UUID，同时作为 LangGraph thread_id")
    message: str = Field(..., min_length=1, description="用户输入")
    images: list[str] = Field(
        default_factory=list,
        description="多模态图片：base64 data URI 列表，仅当前模型支持图片时才有值",
    )
    images:list[str] =Field(default_factory=list,description="多模态图片（base64 data URI），仅模型支持时传")

class ChatResponse(BaseModel):
    reply: str
    session_id: str

class MessageResponse(BaseModel):
    role: str
    content: str


class ConfigResponse(BaseModel):
    """前端展示用的运行配置。

    安全边界：只暴露 provider/model/base_url 等非敏感信息，
    **绝不包含 api_key**（密钥只存服务端环境变量 / config.json）。
    """
    provider: str
    model: str
    base_url: str
    tools: list[str] = Field(default_factory=list, description="已注册工具名")
    permission_mode: str = Field(..., description="execute_command 遇非白名单命令时的裁决：ask/allow/deny")
    permission_whitelist: list[str] = Field(default_factory=list, description="命令白名单")


class SessionStatusResponse(BaseModel):
    """会话级状态：消息条数 + 上下文压缩情况。

    compressed=True 表示该会话的早期消息已被摘要压缩进 session.summary
    （core/agent.py 的 _compress），上下文不再无限增长。
    """
    id: str
    title: str
    message_count: int
    summary_chars: int
    compressed: bool