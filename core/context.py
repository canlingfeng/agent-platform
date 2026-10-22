"""请求级用户上下文（request-scoped identity）。

为什么需要它
----------
Agent 是进程级单例（`api/app.py` 里 `app.state.agent`），被所有租户共享。
工具层（core/tools.py）与权限层（core/permission.py）需要知道"当前是哪个用户"，
才能做文件租户隔离与审计归属。

为什么用 contextvars 而不是全局变量
----------------------------------
FastAPI 的同步端点跑在线程池里，多请求并发。全局变量会被后到的请求覆盖，
导致用户 B 读到用户 A 的身份（race condition）。`contextvars.ContextVar`
在每个异步任务 / 每次 `copy_context()` 拷贝中相互独立，天然隔离并发。

为什么不用工具参数传 user_id
----------------------------
工具参数会出现在 LLM 可见的 JSON Schema 里——用户可以用提示词让模型
传 `user_id=1` 来冒充别人。contextvar 由服务端注入，LLM 无法篡改，
这才是正确的信任边界。

无上下文时的行为
----------------
CLI / 调试入口（main.py、webui.py）不设置身份，此时 `user_id` 为 None，
工具退化为全局 ROOT，保持旧行为不破坏本地调试。
"""
import contextvars
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class RequestIdentity:
    """一次请求的用户身份。frozen=True 防止运行中被意外修改。"""
    user_id: Optional[int] = None
    session_id: Optional[str] = None
    client_ip: Optional[str] = None


# 默认 None = 无用户上下文（CLI / 调试）
current_identity: contextvars.ContextVar[Optional[RequestIdentity]] = \
    contextvars.ContextVar("current_identity", default=None)


def get_identity() -> Optional[RequestIdentity]:
    """取当前上下文身份；无上下文返回 None。"""
    return current_identity.get()


def set_identity(identity: RequestIdentity):
    """设置当前上下文身份，返回 token——调用方必须用 reset_identity 还原。"""
    return current_identity.set(identity)


def reset_identity(token) -> None:
    """还原到设置前的身份。必须放在 finally 里，否则异常会污染上下文。"""
    current_identity.reset(token)
