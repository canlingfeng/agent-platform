"""运行配置查询 + 模型热切换路由。

用途：给前端展示"当前用什么模型 / 有哪些工具 / 权限怎么裁决"，
并提供切换供应商/模型的能力（对应 WorkBuddy 输入栏里的模型选择器）。

安全边界：只返回 provider / model / base_url / 工具名 / 权限规则，
**绝不返回 api_key**（密钥只存在于服务端环境变量与 config.json，不出内网）。

⚠️ 多租户注意：Agent 是进程级单例，切换模型是**全局生效**（影响所有用户）。
当前按"单实例部署"实现；正式多租户上线前应改为管理员专属权限，
或改成按会话/按请求选择模型（那需要把 llm 从单例里拆出来）。
"""
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from api.deps import get_current_user
from api.schemas import ConfigResponse
from core import permission
from core.agent import CONFIG_PATH
from core.providers import PRESETS

router = APIRouter(prefix="/api/v1", tags=["config"])


class ModelSwitchRequest(BaseModel):
    provider: str
    model: str | None = None
    # 仅 provider="custom" 时使用：手填的 OpenAI 兼容地址与可选密钥
    base_url: str | None = None
    api_key: str | None = None


def _persist(agent, include_key: bool = False) -> None:
    """把当前 provider/base_url/model 写回 config.json，让切换在重启后仍生效。

    默认**不写 api_key**：预设供应商的密钥优先来自环境变量，不该因为一次 UI 操作
    落盘成明文；config.json 里原有的其他字段（含已有 api_key）原样保留。
    例外：用户在"自定义模型"里主动填了密钥（include_key=True）——自定义端点没有
    对应的环境变量，密钥只能存在 config.json 里，否则重启后不可用。
    """
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cfg = {}
    cfg["provider"] = agent.provider
    cfg["base_url"] = agent.base_url
    cfg["model"] = agent.model
    if include_key and agent.api_key:
        cfg["api_key"] = agent.api_key
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _snapshot(agent) -> ConfigResponse:
    """当前 Agent 配置的脱敏快照（GET 与 POST 共用，避免两处字段写岔）。"""
    return ConfigResponse(
        provider=agent.provider,
        model=agent.model,
        base_url=agent.base_url,
        tools=sorted(agent.tools.keys()),
        # execute_command 遇到非白名单命令时的裁决动作（ASK = 交用户确认）
        permission_mode=permission.ASK,
        permission_whitelist=list(permission.WHITELIST),
    )


@router.get("/config", response_model=ConfigResponse)
def get_config(request: Request, user=Depends(get_current_user)):
    """返回当前 Agent 的运行配置（脱敏）。"""
    return _snapshot(request.app.state.agent)


@router.get("/providers")
def list_providers(user=Depends(get_current_user)):
    """可选供应商/模型列表，供前端"切换模型"浮层渲染。

    custom 也会返回，并带 `custom=true` 标记：前端对它显示"手填 Base URL +
    模型名 + 可选密钥"的表单，而不是点击即切换。
    supports_images 供前端标注"支持图片"，配合图片上传功能选模型。
    """
    return [
        {
            "key": key,
            "label": preset["label"],
            "model": preset["model"],
            "supports_images": bool(preset.get("supports_images", False)),
            "custom": key == "custom",
        }
        for key, preset in PRESETS.items()
    ]


@router.post("/config", response_model=ConfigResponse)
def switch_model(req: ModelSwitchRequest, request: Request,
                 user=Depends(get_current_user)):
    """热切换供应商/模型：白名单校验 → configure 重建 LLM → 持久化 → 返回新配置。"""
    if req.provider not in PRESETS:
        raise HTTPException(
            status_code=400,
            detail={"error": "bad_provider",
                    "message": f"未知的 provider：{req.provider}"},
        )
    # 自定义端点：地址和模型名都必须填，否则 configure 会把地址留空、
    # 把请求打到空 base_url 上（表现为莫名其妙的连接错误）。
    if req.provider == "custom" and not (
        (req.base_url or "").strip() and (req.model or "").strip()
    ):
        raise HTTPException(
            status_code=400,
            detail={"error": "custom_incomplete",
                    "message": "自定义模型需要同时填写 Base URL 和模型名"},
        )
    agent = request.app.state.agent
    # configure 会重建 ChatOpenAI（base_url/model/api_key 焊在 HTTP 客户端里，
    # 改属性不生效，必须整体重建）；graph 无需重建，下一轮 invoke 自动取新 llm。
    agent.configure(
        req.provider,
        base_url=req.base_url or "",
        model=req.model or "",
        api_key=req.api_key or None,
    )
    _persist(agent, include_key=bool(req.api_key))
    return _snapshot(agent)
