"""会话与对话路由。

设计要点：
  - 所有端点都 Depends(get_current_user)，拿到的 user 已通过 JWT 校验。
  - 多租户隔离：任何涉及 session 的操作都先 get_session_for_user(session_id, user_id)
    做越权校验（IDOR 防护），非本人会话直接 404。
  - /chat 走同步 agent.run（内部用 LangGraph graph.invoke），定义为普通 def 端点，
    让 FastAPI 丢线程池执行，避免阻塞事件循环。
  - /chat/stream 走 LangGraph graph.stream 做真正的增量流式输出（SSE），
    在后台线程跑同步生成器，通过 asyncio.Queue 把每个 AI 消息增量推给客户端。
  - 请求级身份经 core.context 的 contextvars 注入：文件工具落在
    workspace/{user_id}/ 实现租户隔离，权限裁决的审计日志也由此归属到人。
    （流式端点必须在 producer 线程内注入——run_in_executor 不会把异步侧的
    contextvars 带进工作线程。）
"""
import asyncio
import json  
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage

from api.deps import get_current_user
from core.context import RequestIdentity, set_identity, reset_identity
from core.providers import model_supports_images
from api.schemas import (
    ChatRequest,
    ChatResponse,
    MessageResponse,  
    SessionCreateRequest,
    SessionResponse,
    SessionStatusResponse,
)

from storage import user_store
from storage.session import Session

router = APIRouter(prefix="/api/v1", tags=["chat"])


def _check_and_build_content(req: ChatRequest, agent):
    """能力闸门 + 多模态 content 组装（/chat 与 /chat/stream 共用）。

    - 用户带了图片但当前模型不支持 → 抛 400 model_unsupported，
      明确告知"模型不支持"，而不是让请求打到 LLM 后再失败、被误认成 agent 的 bug。
    - 支持则把"文字 + 图片"拼成 OpenAI 多模态 content 列表；
      没有图片时保持纯字符串，完全不改变原有纯文本行为。
    """
    if req.images and not model_supports_images(agent.provider):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "model_unsupported",
                "message": (
                    f"当前模型 {agent.model} 不支持图片/视频输入，"
                    "请切换到支持多模态的模型（如 Qwen-VL 系列）"
                ),
            },
        )
    if not req.images:
        return req.message
    content = [{"type": "text", "text": req.message}]
    for img in req.images:
        content.append({"type": "image_url", "image_url": {"url": img}})
    return content


@router.get("/sessions", response_model=list[SessionResponse])
def list_sessions(user=Depends(get_current_user)):
    rows = user_store.list_sessions(user["id"])
    return [
        SessionResponse(id=r["id"], title=r["title"], created_at=str(r["created_at"]))
        for r in rows
    ]


@router.post("/sessions", response_model=SessionResponse)
def create_session(req: SessionCreateRequest, user=Depends(get_current_user)):
    sid = user_store.create_session(user["id"], title=req.title or "新会话")
    row = user_store.get_session_for_user(sid, user["id"])
    return SessionResponse(
        id=row["id"], title=row["title"], created_at=str(row["created_at"])
    )
@router.get("/sessions/{session_id}/messages", response_model=list[MessageResponse])
def get_messages(session_id: str, request: Request, user=Depends(get_current_user)):
    """从 LangGraph checkpointer 恢复某会话的消息历史。"""
    row = user_store.get_session_for_user(session_id, user["id"])
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="会话不存在或无权访问")
    agent = request.app.state.agent
    return agent.load_history(session_id)


@router.get("/sessions/{session_id}/status", response_model=SessionStatusResponse)
def get_session_status(session_id: str, request: Request, user=Depends(get_current_user)):
    """会话级状态：消息条数 + 上下文压缩情况。

    compressed=True 表示早期消息已被摘要压缩进 session.summary
    （core/agent.py 的 _compress），上下文不会随对话无限增长。
    """
    row = user_store.get_session_for_user(session_id, user["id"])
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="会话不存在或无权访问")
    agent = request.app.state.agent
    history = agent.load_history(session_id)
    summary = (row or {}).get("summary") or ""
    return SessionStatusResponse(
        id=session_id,
        title=row["title"],
        message_count=len(history),
        summary_chars=len(summary),
        compressed=bool(summary),
    )


@router.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, request: Request, user=Depends(get_current_user)):
    agent = request.app.state.agent
    # 越权校验：会话必须属于当前用户
    row = user_store.get_session_for_user(req.session_id, user["id"])
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="会话不存在或无权访问")
    # 配额闸：超支直接 429，不烧 token
    if not user_store.check_quota(user["id"]):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                            detail="今日配额已用完")

    sess = Session(id=req.session_id)
    old_summary = (row or {}).get("summary") or ""
    if old_summary:
        sess.summary = old_summary

    # 注入请求级身份：让文件工具落在 workspace/{user_id}/，并让审计日志能归属到人。
    # contextvars 在当前线程内可见，agent.run() 在同一线程执行，故能正确传递。
    content = _check_and_build_content(req, agent)
    token = set_identity(RequestIdentity(
        user_id=user["id"], session_id=req.session_id,
        client_ip=request.client.host if request.client else None))
    try:
        reply = agent.run(content, sess)
    finally:
        reset_identity(token)

    # 压缩摘要回写 + 用量记账（token 由 agent.run 从 usage_metadata 提取）
    if sess.summary and sess.summary != old_summary:
        user_store.update_session_summary(req.session_id, sess.summary)
    user_store.record_usage(user["id"], tokens=sess.token_count, requests=1)
    # 刷新 updated_at：消息进的是 checkpointer，sessions 表不会被碰，
    # 不主动更新则列表顺序停留在创建顺序，"最近活跃"排不上来
    user_store.touch_session(req.session_id, user["id"])

    # NEED_CONFIRM 是"命令需用户确认"的挂起信号，API 层直接透传给调用方处理
    return ChatResponse(reply=reply, session_id=req.session_id)


@router.post("/chat/stream")
async def chat_stream(req: ChatRequest, request: Request,
                     user=Depends(get_current_user)):
    """LangChain/LangGraph 架构的增量流式输出（SSE）。

    真正的流式来自 graph.stream：它在每个 super-step（模型调用、工具调用）完成后
    产出状态增量。这里在后台线程跑同步生成器，用 asyncio.Queue 把每个 AI 消息内容
    推给客户端，做到"边生成边返回"。token 记账在流结束后统一落库。
    """
    agent = request.app.state.agent
    row = user_store.get_session_for_user(req.session_id, user["id"])
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="会话不存在或无权访问")
    if not user_store.check_quota(user["id"]):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                            detail="今日配额已用完")

    sess = Session(id=req.session_id)
    old_summary = (row or {}).get("summary") or ""
    if old_summary:
        sess.summary = old_summary

    # 能力闸门必须放在 StreamingResponse 之外：否则 400 会变成流内异常，客户端拿不到明确错误
    content = _check_and_build_content(req, agent)
    config = {"configurable": {"thread_id": req.session_id}}
    graph_input = {
        "messages": [HumanMessage(content=content)],
        "iteration": 0,
        "summary": sess.summary,
        "user_id": None,
    }
    total_tokens = 0
    loop = asyncio.get_event_loop()

    async def event_gen():
        nonlocal total_tokens
        queue: asyncio.Queue = asyncio.Queue()

        def producer():
            nonlocal total_tokens
            # 身份必须在这里注入：graph.stream 在本线程内同步执行，
            # 而 run_in_executor 不会把异步侧的 contextvars 带进工作线程。
            # 放在 producer 内部可保证工具执行时身份一定可见。
            token = set_identity(RequestIdentity(
                user_id=user["id"], session_id=req.session_id,
                client_ip=request.client.host if request.client else None))
            try:
                for chunk in agent.graph.stream(graph_input, config):
                    for node_out in chunk.values():
                        msgs = node_out.get("messages", []) if isinstance(node_out, dict) else []
                        for m in msgs:
                            usage = getattr(m, "usage_metadata", None)
                            if usage:
                                total_tokens += int(
                                    usage.get("total_tokens", 0)
                                    or (usage.get("input_tokens", 0)
                                        + usage.get("output_tokens", 0))
                                )
                            content = getattr(m, "content", "")
                            if content and getattr(m, "type", "") == "ai":
                                asyncio.run_coroutine_threadsafe(queue.put(content), loop)
            except Exception as e:  # 流式过程出错也要让客户端感知
                asyncio.run_coroutine_threadsafe(queue.put(f"[错误] {e}"), loop)
            finally:
                reset_identity(token)
                asyncio.run_coroutine_threadsafe(queue.put(None), loop)

        await loop.run_in_executor(None, producer)
        while True:
            item = await queue.get()
            if item is None:
                break
            # JSON 编码成单行：AI 原文里的 \n\n 会变成转义符 \n，
            # 绝不会被当成 SSE 帧分隔符（之前的 bug：裸字符串遇空行被切成多段、续行被前端丢弃）。
            yield "data: " + json.dumps({"t": item}, ensure_ascii=False) + "\n\n"
        # 结束标记同样 JSON 化；前端按 payload.done 判断，不再依赖裸字符串 "[DONE]"
        yield "data: " + json.dumps({"done": True}, ensure_ascii=False) + "\n\n"
        # 流结束后记账（token 来自本轮所有 AI 消息的 usage_metadata 累加）
        user_store.record_usage(user["id"], tokens=total_tokens, requests=1)
        # 刷新 updated_at，让最近活跃的会话排到列表前面
        user_store.touch_session(req.session_id, user["id"])

    return StreamingResponse(event_gen(), media_type="text/event-stream")
