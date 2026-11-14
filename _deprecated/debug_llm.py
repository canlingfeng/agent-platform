"""[LEGACY] LLM 连通性调试脚本 —— 保留。

用途：不经过 LangGraph 和数据库，直接打 LLM 接口，
用于快速判断"是 LLM 配置问题还是 Agent/DB 问题"。
排查对话异常时先跑这个，能省掉一半怀疑对象。

注意：`Agent()` 不传 checkpointer，会用进程内存版 MemorySaver——
本脚本不落库，属预期行为（该默认值的隐患见 docs/ARCHIVED.md 第四节）。
"""
import os
import sys

# 归档脚本位于 _deprecated/ 子目录：直接运行时 sys.path[0]（模块搜索路径首项）
# 是该子目录，项目根不在其中，`from core.xxx import ...` 会抛 ModuleNotFoundError。
# 这里用 __file__ 上跳一级定位项目根并加入 sys.path，不写死绝对路径，
# 保证 Windows / Linux 都能解析（平台无关）。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from core.agent import Agent
from langchain_core.messages import HumanMessage

agent = Agent()

print("=== 测试 1：llm_plain（没 bind_tools）===")
try:
    r1 = agent.llm_plain.invoke([HumanMessage(content="你好")])
    print(r1)
except Exception:
    import traceback
    traceback.print_exc()

print("\n=== 测试 2：llm（bind_tools 后）===")
try:
    r2 = agent.llm.invoke([HumanMessage(content="你好")])
    print(r2)
except Exception:
    import traceback
    traceback.print_exc()