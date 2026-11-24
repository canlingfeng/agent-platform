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
import logging
import os
import threading
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage

from api.deps import get_current_user
from core.context import RequestIdentity, set_identity, reset_identity
from core.providers import model_supports_images
from pydantic import BaseModel
from core.tools import execute_command
from core.permission import decide, DENY
from api.schemas import (
    ChatRequest,
    ChatResponse,
    MessageResponse,  
    SessionCreateRequest,
    SessionResponse,
    SessionStatusResponse,
    UsageInfo,
)

from storage import user_store
from storage import confirm_registry
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


# 单次请求 token 上限的兜底默认值：环境变量缺省时用它（容器部署必须显式注入覆盖）。
_DEFAULT_MAX_TOKENS_PER_REQUEST = 20000


def _resolve_token_cap(user_id: int) -> tuple[int, dict]:
    """算出本次请求允许消耗的 token 上限，并返回当日用量快照。

    上限 = min(单请求上限, 今日剩余额度)。为什么不是二选一：
      - 只用固定值：用户今日只剩 100 token，单次仍可烧满 20000 → 超额 200 倍；
      - 只用剩余额度：一次超长回答就能把整天额度一口吃光，等于没有单次门槛。
    两者取小：既被单次上限压住，也绝不会突破当日总额度。

    返回值 (cap, usage)：
      cap   >0 时可以开跑；==0 表示今日额度已耗尽，调用方直接 429。
      usage 是 get_usage_today 的原始快照，供响应体 / SSE 帧复用。
    """
    usage = user_store.get_usage_today(user_id)
    try:
        per_request = int(os.environ.get("AGENT_MAX_TOKENS_PER_REQUEST",
                                         _DEFAULT_MAX_TOKENS_PER_REQUEST))
    except (TypeError, ValueError):
        # 环境变量写错（如 "20k"）时不能让整个接口 500，退回默认值
        per_request = _DEFAULT_MAX_TOKENS_PER_REQUEST
    return max(min(per_request, usage["remaining_tokens"]), 0), usage


def _usage_payload(user_id: int, turn_tokens: int, capped: bool) -> dict:
    """组装 SSE / 响应体共用的用量快照：当日用量 + 本次消耗 + 是否被截断。"""
    data = user_store.get_usage_today(user_id)
    data.update({"turn_tokens": int(turn_tokens), "capped": bool(capped)})
    return data


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


@router.delete("/sessions/{session_id}")
def delete_session(session_id: str, request: Request, user=Depends(get_current_user)):
    """删除会话（软删除）：非本人会话一律 404（IDOR 防护）。

    软删 = sessions.deleted_at 置时间戳：列表立刻消失、所有访问路径
    （get_session_for_user 过滤 deleted_at IS NULL）立即 404，数据留库可审计。
    LangGraph 检查点尽力清理：saver 版本支持 delete_thread 才调，失败/不支持
    都不影响删除本身——会话已软删，残留检查点没有访问路径，只是存储占位。
    """
    row = user_store.get_session_for_user(session_id, user["id"])
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="会话不存在或无权访问")
    user_store.delete_session(session_id, user["id"])
    purge = getattr(request.app.state.agent.checkpointer, "delete_thread", None)
    if callable(purge):
        try:
            purge(session_id)
        except Exception:
            pass
    return {"deleted": True}


@router.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, request: Request, user=Depends(get_current_user)):
    agent = request.app.state.agent
    # 越权校验：会话必须属于当前用户
    row = user_store.get_session_for_user(req.session_id, user["id"])
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="会话不存在或无权访问")
    # token 预检：当日 token 已耗尽就直接 429，不消耗请求数、也不白跑 LLM。
    # 注意这与下面的 consume_quota 互补：consume_quota 只管请求数（tokens=0），
    # 未做这道预检时，额度耗尽的老用户仍能无限发请求、把 token 全部透支。
    cap, usage = _resolve_token_cap(user["id"])
    if usage["remaining_tokens"] <= 0:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                            detail="今日 token 配额已用完")
    # 配额闸（原子）：consume_quota 在同一事务里"查+扣"，超支直接 429，不烧 token，
    # 同时把本次请求的额度先行扣掉，避免并发请求双双过闸（TOCTOU）。
    if not user_store.consume_quota(user["id"], requests=1, tokens=0):
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
    # 请求数已在 consume_quota 先行扣减，这里只累计实际 token（best-effort，不重复扣请求）
    user_store.record_usage(user["id"], tokens=sess.token_count, requests=0)
    # 刷新 updated_at：消息进的是 checkpointer，sessions 表不会被碰，
    # 不主动更新则列表顺序停留在创建顺序，"最近活跃"排不上来
    user_store.touch_session(req.session_id, user["id"])

    # NEED_CONFIRM 是"命令需用户确认"的挂起信号，API 层直接透传给调用方处理
    # 同步路径无法在生成中途中止（agent.run 内部一次 invoke 跑完），
    # 所以这里只做"事后稽核"：把本轮实际消耗和当日快照一起返回，前端据此展示；
    # 真正的单次上限拦截在流式路径（可逐 chunk 判停）里生效。
    return ChatResponse(
        reply=reply,
        session_id=req.session_id,
        usage=UsageInfo(**_usage_payload(
            user["id"], sess.token_count,
            bool(cap) and sess.token_count >= cap,
        )),
    )


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
    # token 预检 + 单次上限：额度耗尽直接 429；否则 cap 就是本轮允许烧的 token 上限。
    cap, usage = _resolve_token_cap(user["id"])
    if usage["remaining_tokens"] <= 0:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                            detail="今日 token 配额已用完")
    if not user_store.consume_quota(user["id"], requests=1, tokens=0):
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
    stop_event = threading.Event()  # 客户端断开时通知 producer 尽快退出，避免断连后还白跑 LLM

    async def event_gen():
        nonlocal total_tokens
        queue: asyncio.Queue = asyncio.Queue()

        ask_pending = False     # 标记本轮是否因 ASK 挂起（挂起时不再发 done，交由前端弹框接管）
        capped = False          # 标记本轮是否因触及单次 token 上限被提前中止

        def _compress_after_stream():
            """
            流式收尾压缩（#2）：把窗口外消息摘要进 sess.summary 并落库。
            为什么放 producer 线程：_compress 内部要调 call_llm（同步 HTTP，
            可能几秒），放 async 侧会阻塞事件循环、拖慢同进程其他请求。
            为什么吞异常：摘要是尽力而为的旁路，失败不能影响本轮对话结果。
            为什么 ask 挂起 / capped 早退路径也要压缩：消息已进 checkpoint，
            不压缩这些消息就变成"只丢不摘"，长期记忆照样失效。
            """ 
            try:
                state=agent.graph.get_state(config)
                msg=state.values.get("messages", []) if state and state.values else []
                if len(msg)<=agent.max_history:
                    return
                # 窗口外 = 最旧的 len-max_history 条（切片从头取到分界点）；
                # 写成 msg[len-max_history:] 会取到最新 20 条，把近期上下文摘要掉、旧消息留下。
                evicted=msg[:len(msg)-agent.max_history]
                before= sess.summary
                agent._compress(sess, agent._messages_to_history(evicted))
                if sess.summary and sess.summary != before:
                    user_store.update_session_summary(req.session_id, sess.summary)
            except Exception:
                logging.getLogger(__name__).exception(
                    "流式收尾压缩失败（不影响本轮结果）")

        def producer():
            nonlocal total_tokens, capped
            # 身份必须在这里注入：graph.stream 在本线程内同步执行，
            # 而 run_in_executor 不会把异步侧的 contextvars 带进工作线程。
            # 放在 producer 内部可保证工具执行时身份一定可见。
            token = set_identity(RequestIdentity(
                user_id=user["id"], session_id=req.session_id,
                client_ip=request.client.host if request.client else None))
            try:
                for chunk in agent.graph.stream(graph_input, config):
                    # 断连止血：每个 super-step（模型调用/工具调用）边界读一次取消信号。
                    # stop_event 由外层 event_gen 闭包捕获，这里只调用方法不需 nonlocal。
                    if stop_event.is_set():
                        break
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
                            # 单请求上限止血：累计一旦触达 cap 立即停止本轮生成。
                            # 用 return 而非 break —— producer 的 finally 仍会把 None 推进队列，
                            # 事件循环据此正常收尾；已消耗的 token 照常记账（中止不等于免单）。
                            if cap and total_tokens >= cap:
                                capped = True
                                return
                            content = getattr(m, "content", "")
                            mtype = getattr(m, "type", "")
                            # ASK 三态：工具要执行"非白名单命令"时，execute_command 在
                            # webui 模式下返回 "NEED_CONFIRM:<cmd>"（type=="tool"）。这里
                            # 把它识别成挂起信号，推给前端弹确认框，并 return 暂停 producer，
                            # 等用户确认后再由 /chat/confirm 单独执行。
                            if mtype == "tool":
                                # 方案 B：工具执行轻量占位帧。只推工具名（ToolMessage.name），
                                # 输出全文不进聊天流（保持"聊天为主、轨迹降噪"的定位）。
                                # 放在 NEED_CONFIRM 判断之前：即使紧接着挂起确认，
                                # 前端也先看到"执行了 execute_command"再弹框，时序自然。
                                tool_name = getattr(m, "name", None) or "tool"
                                asyncio.run_coroutine_threadsafe(
                                    queue.put({"tool": tool_name}), loop)
                            if (mtype == "tool" and isinstance(content, str)
                                    and content.startswith("NEED_CONFIRM:")):
                                cmd = content[len("NEED_CONFIRM:"):]
                                # 修复 #1（confirm 绕过）：挂起必须在服务端登记并签发
                                # 一次性 token。凭据从"公开可猜的命令原文"换成
                                # "密码学随机串 + 服务端可验证"，前端确认时必须原样带回。
                                token = confirm_registry.register(
                                    req.session_id, user["id"], cmd)
                                asyncio.run_coroutine_threadsafe(
                                    queue.put({"ask": cmd, "token": token}), loop)
                                return
                            if content and mtype == "ai":
                                asyncio.run_coroutine_threadsafe(queue.put(content), loop)
            except Exception:  # 流式过程出错也要让客户端感知，但不泄露内部异常原文
                logging.getLogger(__name__).exception("chat_stream producer failed")
                asyncio.run_coroutine_threadsafe(
                    queue.put("[错误] 服务内部错误，请稍后重试"), loop
                )
            finally:
                reset_identity(token)
                _compress_after_stream() 
                asyncio.run_coroutine_threadsafe(queue.put(None), loop)

        # 修复假流式：用 run_in_executor 把 producer 调度到线程池，但【不立刻 await 它】。
        # producer 在后台线程边生成边往 queue 塞 token，本协程立刻进入下面的 while 循环
        # 把 queue 里的 token 实时 yield 出去 —— 真正的逐字流式。
        # 旧写法 `await loop.run_in_executor(...)` 会先等 producer 整段跑完才往下走，
        # 导致前端要等模型全部生成完才一次性收到所有帧（假流式/攒批）。
        producer_task = loop.run_in_executor(None, producer)
        aborted = False   # 是否因客户端断开而提前收尾（决定最后要不要发 done 帧）
        try:
            while True:
                # 客户端断开（点了停止 / 关页面）→ 标记 aborted，收尾由 finally 统一 set 信号
                if await request.is_disconnected():
                    aborted = True
                    break
                try:
                    # 给 queue.get() 设 1s 上限：producer 长时间无输出（LLM 首 token 前）
                    # 也不能让断连检测被无限阻塞；超时只是回环再查一次，不丢帧。
                    item = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if item is None:
                    break
                try:
                    # dict 帧 = ASK 挂起信号：把命令透传给前端确认框，然后结束本轮流。
                    # 注意 break 后不再发 done —— 否则前端会先弹框又被 done 收尾逻辑干扰。
                    if isinstance(item, dict) and "tool" in item:
                        # 工具占位帧：透传工具名，继续读下一帧（不打断本轮流）
                        yield "data: " + json.dumps({"tool": item["tool"]}, ensure_ascii=False) + "\n\n"
                        continue
                    if isinstance(item, dict) and "ask" in item:
                        # 先推用量帧再推 ask：前端收到 ask 会立刻 return（弹框接管），
                        # 之后发的帧就没人消费了，挂起前这段消耗必须抢先送出去。
                        yield "data: " + json.dumps(
                            {"usage": _usage_payload(user["id"], total_tokens, False)},
                            ensure_ascii=False) + "\n\n"
                        yield "data: " + json.dumps(
                            {"ask": item["ask"], "token": item["token"]},
                            ensure_ascii=False) + "\n\n"
                        ask_pending = True
                        break
                    # JSON 编码成单行：AI 原文里的 \n\n 会变成转义符 \n，
                    # 绝不会被当成 SSE 帧分隔符（之前的 bug：裸字符串遇空行被切成多段、续行被前端丢弃）。
                    yield "data: " + json.dumps({"t": item}, ensure_ascii=False) + "\n\n"
                except (GeneratorExit, asyncio.CancelledError):
                    aborted = True
                    break   # 连接已断，yield 失败就退出
        finally:
            # 无论走到哪个 break / 异常出口，都在这里置位，保证 producer 一定能看到信号
            stop_event.set()
        # 不无限等：producer 线程无法被强杀，只能在 super-step 边界退出，故给 5s 上限
        try:
            await asyncio.wait_for(producer_task, timeout=5.0)
        except asyncio.TimeoutError:
            logging.getLogger(__name__).warning(
                "chat_stream producer 未在 5s 内退出，已放弃等待")
        # 不论正常结束还是 ASK 挂起，本轮已产生的 token 都记账（避免漏算）
        # 请求数已在 consume_quota 先行扣减，这里只累计实际 token（best-effort，不重复扣请求）
        user_store.record_usage(user["id"], tokens=total_tokens, requests=0)
        # 刷新 updated_at，让最近活跃的会话排到列表前面
        user_store.touch_session(req.session_id, user["id"])
        # 用量帧：必须在 done 之前发 —— 前端 api.js 收到 done 就直接 return 了。
        # 断连（aborted）时不发：连接已死，yield 只会抛异常刷日志。
        if not aborted and not ask_pending:
            yield "data: " + json.dumps(
                {"usage": _usage_payload(user["id"], total_tokens, capped)},
                ensure_ascii=False) + "\n\n"
        # 只有真正跑完才发 done；ASK 挂起时由前端弹框接管，不要发 done
        if not ask_pending and not aborted:
            yield "data: " + json.dumps({"done": True}, ensure_ascii=False) + "\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")


class ConfirmRequest(BaseModel):
    """ASK 三态的确认请求：用户对一条"需确认命令"给出允许 / 拒绝。

    token：NEED_CONFIRM 挂起时服务端签发的一次性凭据（随 {"ask"} 帧下发），
    确认时必须原样带回。没有它 = 请求不是走过挂起流程的，直接 403。
    """
    session_id: str
    command: str
    decision: str   # "allow" 为允许；其它值（含 "deny"）一律视为拒绝（fail-closed）
    token: str


@router.post("/chat/confirm")
def chat_confirm(req: ConfirmRequest, request: Request,
                 user=Depends(get_current_user)):
    """ASK 三态的确认端点：用户在前端弹框里选择允许 / 拒绝后调用。

    安全要点（面试 / 生产都必讲）：
      - 越权校验：会话必须属于当前用户（IDOR 防护），非本人会话直接 404。
      - 安全闸：必须重跑 decide() 挡掉 DENY 命令。否则攻击者只要拿一个
        NEED_CONFIRM 的 command 直接 POST decision=allow，就能借 execute_command
        的 approve 分支绕过白名单 / shell 元字符闸执行任意命令（RCE）。
      - 审计归属：注入请求级身份，让 decide()/execute_command() 内部的审计日志
        能正确记到这个用户 / 会话头上，而不是留 NULL。
    """
    # 越权校验：会话必须属于当前用户
    row = user_store.get_session_for_user(req.session_id, user["id"])
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="会话不存在或无权访问")

    # 注入请求级身份：decide() / execute_command() 内部会记审计日志，
    # 需要 user_id / session_id 才能正确归属到人（否则审计列留 NULL）。
    token = set_identity(RequestIdentity(
        user_id=user["id"], session_id=req.session_id,
        client_ip=request.client.host if request.client else None))
    try:
        # 安全闸：重跑裁决，DENY 一律 403，不允许用 approve=True 强行绕过。
        # 闸门顺序契约（tests/test_confirm_guard.py 锁死）：decide 在 consume 之前，
        # 否则攻击者可用"错命令+错 token"探测登记表状态（侧信道）。
        if decide("execute_command", {"command": req.command}) == DENY:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="命令被拒绝：命中黑名单或包含危险字符")
        # 修复 #1：凭据核销闸。四元组 (token, session, user, command) 全匹配才放行，
        # 校验即删除（原子）——没有登记过 / 已消费过 / 偷换命令 / 换会话换人，全部 403。
        # 放在 decision 判断之前：无论允许还是拒绝，凭据都作废，一次挂起只有一次表态机会。
        if not confirm_registry.consume(req.token, req.session_id,
                                        user["id"], req.command):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="确认凭据无效或已过期")
        # 用户拒绝（含非 "allow" 的任意值）→ 不执行，fail-closed。
        if req.decision != "allow":
            return {"result": "用户拒绝执行命令"}
        # 用户允许 → 真正执行。execute_command(approve=True) 内部仍会再跑一次
        # decide()（DENY 返回拒绝字符串而非执行），与上面的 403 闸构成双重保险。
        output = execute_command(req.command, approve=True)
        return {"result": output}
    finally:
        reset_identity(token)
