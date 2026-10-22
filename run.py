"""一键启动开发服务器（等价于命令行 uvicorn）。

为什么有这个文件：让"跑前端"变成一条命令、且不用管当前目录（cd）。
直接 `python run.py` 即可——Python 会把本文件所在目录自动加进模块搜索路径，
所以 `api.app` 无论你在哪个目录执行都能被找到。

启动前会自动加载同目录的 `.env`（与 docker compose 的行为对齐）：
  - docker compose 会自动读 .env 做变量替换，但裸跑 Python 不会，
    于是出现"容器里能跑、本地报缺少 AGENT_JWT_SECRET"的割裂。
  - 这里用 python-dotenv 补齐，让两种启动方式行为一致。
  - 关键：`override=True` 让 .env 强制覆盖已存在的环境变量。
    理由：本地开发时用户常在同一 PowerShell 会话里手动 `set` 过 AGENT_* /
    MODELSCOPE_API_KEY / 代理变量（调试残留），这些旧值会盖过 .env 导致
    离奇故障（最典型：发消息 0.1 秒秒挂 500、.env 明明正确却像没生效）。
    强制覆盖后，.env 成为本地唯一真相源，杜绝"环境脏变量"干扰。
  - 对 Docker 无影响：容器内 .env 被 .dockerignore 排除，文件不存在，
    load_dotenv 自动变 no-op，compose 注入的 AGENT_DB_HOST=db 等真实变量照常生效。

- host="0.0.0.0"：监听所有网卡（本机用 localhost，同一局域网手机也能连）。
  若只在本机测，可改成 "127.0.0.1"。
- port=8000：访问地址 http://localhost:8000/
- reload=True：改代码后自动重启，开发期方便。
  （reload 会起子进程重新导入 app，环境变量由父进程继承，故 load_dotenv
   必须在 uvicorn.run() 之前调用。）
"""

import os

from dotenv import load_dotenv

# 显式指向本文件同目录的 .env：load_dotenv() 默认从 cwd 向上找，
# 显式指定后无论用户在哪个目录执行 `python .../run.py` 都能正确加载。
# override=True：.env 强制覆盖已存在的环境变量，避免本地调试残留的脏变量盖过配置。
_ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(_ENV_FILE, override=True)

import uvicorn  # noqa: E402  （放在 load_dotenv 之后：确保导入 app 前环境变量已就绪）

if __name__ == "__main__":
    uvicorn.run(
        "api.app:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        reload_dirs=[os.path.dirname(os.path.abspath(__file__))],
    )
