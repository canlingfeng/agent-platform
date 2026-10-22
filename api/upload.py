"""图片/视频上传端点。

设计要点：
  - 复用 JWT 鉴权（Depends(get_current_user)）。
  - 租户隔离：落到 workspace/{user_id}/uploads/，与文件工具同一套根。
  - 能力闸门：当前模型不支持多模态 → 直接 400 + 结构化错误，明确"模型不支持"。
  - 仅收图片/视频 MIME，限 20MB，防刷爆存储。
  - 路径安全：只取 file.filename 的纯文件名，防止 ../ 路径穿越。

说明：模型调用走的是前端内联的 base64（云端模型访问不到本机路径），
本端点承担"持久化 + 租户隔离 + 能力校验"这一层。
"""
import pathlib

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile

from api.deps import get_current_user
from core.providers import model_supports_images

router = APIRouter(prefix="/api/v1", tags=["upload"])

# 项目根 = api/upload.py 的上一级（parents[0] 是 api/，parents[1] 才是项目根）
_UPLOAD_ROOT = pathlib.Path(__file__).resolve().parents[1] / "workspace"
_ALLOWED_PREFIXES = ("image/", "video/")
_MAX_BYTES = 20 * 1024 * 1024  # 20MB


@router.post("/upload")
async def upload(
    request: Request,
    file: UploadFile = File(...),
    user=Depends(get_current_user),
):
    # 当前模型由全局 Agent 单例持有；user 字典里没有 provider 字段，不能用它判断
    agent = request.app.state.agent
    # ① 能力闸门：模型不支持 → 明确错误，不往下走
    if not model_supports_images(agent.provider):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "model_unsupported",
                "message": (
                    f"当前模型 {agent.model} 不支持图片/视频输入，"
                    "请切换到支持多模态的模型（如 Qwen-VL 系列）"
                ),
            },
        )
    # ② MIME 校验（content_type 可能为 None，必须兜底，否则 startswith 会崩）
    ctype = file.content_type or ""
    if not ctype.startswith(_ALLOWED_PREFIXES):
        raise HTTPException(status_code=400,
                            detail={"error": "bad_type", "message": "仅支持图片/视频"})
    # ③ 读全量 + 大小校验（20MB 内进内存无碍，逻辑最清晰）
    data = await file.read()
    if len(data) > _MAX_BYTES:
        raise HTTPException(status_code=413,
                            detail={"error": "too_large", "message": "超过 20MB"})
    # ④ 路径安全 + 租户隔离写盘
    safe_name = pathlib.Path(file.filename or "upload.bin").name
    dest_dir = _UPLOAD_ROOT / str(user["id"]) / "uploads"
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / safe_name).write_bytes(data)
    return {"path": f"uploads/{safe_name}", "content_type": ctype}
