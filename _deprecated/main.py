"""[LEGACY] CLI 交互模式 —— 本地调试入口，非主交付入口。

主入口已换成 `run.py`（FastAPI + static/ 原生 JS 前端），见 api/app.py。
本文件保留的理由：不依赖浏览器，适合在服务器上快速验证 Agent 链路是否通。

注意：这里用 `storage/session.py` 的 SessionStore（进程内 dict），
重启后会话丢失——CLI 调试不需要持久化，属预期行为。
详见 docs/ARCHIVED.md 第二节。
"""
import os
import sys

# 归档脚本位于 _deprecated/ 子目录：直接运行时项目根不在 sys.path（模块搜索路径），
# `from core.xxx` / `from storage.xxx` 会抛 ModuleNotFoundError。
# 用 __file__ 上跳一级定位项目根，不写死绝对路径，保证 Windows / Linux 通用。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

os.environ["LANGGRAPH_STRICT_MSGPACK"] = "true"
from core.agent import Agent
from storage.session import SessionStore
from storage.checkpointer import build_checkpointer

def main():
    checkpointer = build_checkpointer()
    agent = Agent(checkpointer=checkpointer)
    store = SessionStore()
    session = store.create_session()
    while True:
        user_input=input("用户输入: ")
        if user_input.lower() in ["exit", "quit"]:
            print("退出程序。")
            break   
        response=agent.run(user_input,session)
        store.save(session)
        print("回复: ", response)

if __name__ == "__main__":
    main()
    