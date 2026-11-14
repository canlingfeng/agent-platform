"""MCP → LangChain 工具桥接层（L2）。

把 core.mcp_client 运行时拉到的 MCP 工具清单转换成 StructuredTool，
供 Agent 注册进 bind_tools / ToolNode，使 LLM 能看见并调用。

设计约束：
- 依赖延迟到函数内加载：避免模块导入阶段触发 MCP 连接，也避免目标机器
  缺少 mcp 包时拖垮 FastAPI 启动。
- 全程 fail-open（失败开放）：任何异常都返回空列表，原有内置工具照常工作。
"""
import logging
import re

from typing import Any
from core.permission import ALLOW, decide

from langchain_core.tools import StructuredTool

log = logging.getLogger(__name__)
_MCP_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _make_one(rt, item: dict) -> StructuredTool:
    """把单条 MCP 工具描述转成一个 StructuredTool。

    用工厂函数而非在循环内直接定义闭包，是为了规避 Python 的晚绑定
    （late binding）：每次调用创建独立作用域，让每个工具绑定各自的 key。
    """
    key = item["name"]

    def _run(**kwargs: Any) -> str:
        # ToolNode 会把 arguments 拆成关键字参数传入；此处收拢成 dict 交回 L1。
        from core.mcp_client import McpError
        if decide(key,kwargs)!= ALLOW:
            return f"拒绝调用 MCP 工具 {key}：不在允许清单内"
        try:
            return rt.call(key, kwargs)
        except McpError as e:
            return f"MCP 工具调用失败：{e}"
    return StructuredTool.from_function(
        func=_run,
        name=key,
        description=item.get("description") or f"MCP 工具 {key}",
        args_schema=item.get("schema") or {"type": "object", "properties": {}},
        infer_schema=False,
    )


def build_mcp_tools() -> list[StructuredTool]:
    """构造 MCP 工具列表；未配置 / 连接失败 / 依赖缺失时返回 []（fail-open）。"""
    try:
        from core.mcp_client import get_runtime
        rt = get_runtime()
        out = []
        for item in rt.list_tools():
            key = item["name"]
            if not _MCP_NAME.fullmatch(key):
                log.warning("MCP 工具名不合法，已跳过：%r", key)
                continue
            out.append(_make_one(rt, item))
        return out
    except Exception as e:
        log.warning("MCP 工具注册失败，已跳过：%s: %s", type(e).__name__, e)
        return []
