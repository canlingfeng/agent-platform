"""FastAPI 应用入口。

启动流程（lifespan）：构建 MySQL 检查点 + Agent 单例，挂到 app.state，
供路由层（`request.app.state.agent`）复用——避免每个请求重建 Agent（重建会重连 DB、重编译图）。

艺术化说明：
  - CORS：允许前端/PWA 跨域调用；生产环境应把 allow_origins 限定成前端域名白名单。
  - 与 Gradio webui.py 并存：两者各自 import 一套 agent，互不影响；后续前端可整体切到这套 API。
"""
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api import auth, chat, config, upload, rag
from core.agent import Agent
from storage.checkpointer import build_checkpointer


@asynccontextmanager
async def lifespan(app: FastAPI):
    cp = build_checkpointer()
    app.state.checkpointer = cp
    app.state.agent = Agent(checkpointer=cp)
    yield
    # 关闭期：释放 MySQL 连接
    try:
        cp.conn.close()
    except Exception:
        pass


app = FastAPI(title="AI Agent Platform API", version="0.1.0", lifespan=lifespan)

# 生产加固：CORS 不再用 "*"。allow_credentials=True 配合 "*" 在浏览器端是非法组合
# （浏览器会直接拒绝带凭据的泛域名请求），且等于对任何站点开放 API。
# 改为从环境变量 AGENT_CORS_ORIGINS 读取逗号分隔的前端域名白名单；
# 为空则只允许同源（SPA 由本服务 StaticFiles 托管，不走跨域）。
_cors_origins = [
    o.strip()
    for o in os.environ.get("AGENT_CORS_ORIGINS", "").split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(config.router)
app.include_router(upload.router)
app.include_router(rag.router)  
@app.get("/health")
def health():
    return {"status": "ok"}


# 托管前端 SPA：在所有 /api、/health 接口【之后】挂载，因此这些接口优先匹配；
# 其余路径（含 "/"）由 StaticFiles 提供 static/index.html。
# 目录用 __file__ 上跳一级定位到项目根的 static/，避免依赖"当前工作目录"。
_STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static"
)
app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")
