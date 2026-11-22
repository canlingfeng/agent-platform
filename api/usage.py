"""用量查询路由（只读）。

为什么单独成模块而不是塞进 chat.py：
  配额/用量会持续扩展（按会话、按模型、按月账单），把它和"对话"放在一起会让
  两个关注点纠缠；单独成文件后，app.py 只多一行 include_router。

安全边界：
  - 只读端点，不消耗配额。
  - user_id 一律取自 JWT 解出的当前用户（Depends(get_current_user)），
    绝不接受前端传 id —— 否则就是横向越权，可以查别人的用量。
  - 返回体只有数字，不含密钥、模型配置等敏感信息。
"""
from fastapi import APIRouter, Depends

from api.deps import get_current_user
from api.schemas import UsageInfo
from storage import user_store

router = APIRouter(prefix="/api/v1", tags=["usage"])


@router.get("/usage", response_model=UsageInfo)
def get_usage(user=Depends(get_current_user)):
    """当前用户「今日」用量快照（前端页面加载 / 切会话时刷新顶栏用）。"""
    return UsageInfo(**user_store.get_usage_today(user["id"]))
