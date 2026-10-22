"""[LEGACY] Gradio 多用户界面 —— 已非主交付入口，保留供参考/回退。

主入口：`run.py` → `api/app.py`（FastAPI）+ `static/`（原生 JS PWA）。

为什么换掉 Gradio：
  1. Gradio 组件状态存在服务端，多用户并发下有状态串扰风险；
     FastAPI 无状态 + JWT，天然适配多租户。
  2. Gradio 不暴露 REST API，移动端 / 第三方无法接入；
     现在同一套 /api/v1/* 可给 Web / PWA / 移动端共用。
  3. 流式输出细粒度控制弱；现在用 SSE（/api/v1/chat/stream）做真增量流式。
  4. Gradio 依赖体积大，拖慢镜像构建。

本文件仍可运行（`python webui.py`），但不再随 Docker 镜像交付。
详见 docs/ARCHIVED.md 第二节。
"""
import os
os.environ["LANGGRAPH_STRICT_MSGPACK"] = "true"
import gradio as gr
import core.tools as tools
import time
import json
from core.permission import WHITELIST
from core.agent import Agent, CONFIG_PATH
from storage.checkpointer import build_checkpointer
from storage.session import Session
from storage import user_store
from core.providers import PRESETS

checkpointer = build_checkpointer()
agent = Agent(checkpointer=checkpointer)

tools.UI_MODE = "webui"  # 设置 UI 模式为 webui
TIMEOUT = 60

def on_preset_change(preset_name):
    """下拉切换：只有选 custom 才展开手填的 Base URL / 模型名"""
    return gr.update(visible=(preset_name == "custom"))

def save_provider_config(preset_name, custom_base, custom_model, user_input_key):
    """保存 API 配置：校验 → 热切换 Agent → 落盘 config.json"""
    if not user_input_key:
        return "API Key 不能为空"
    key = user_input_key.strip()
    # 输入卫生：真 key 都是无空格的无意义长串（ms-xxx / sk-xxx，<200 字符）。
    # 含空白/换行/超长 = 大概率粘错了东西（比如整段代码），直接拒，不写盘。
    if len(key) > 200 or any(ch in key for ch in " \t\r\n"):
        return "API Key 格式不对：不应含空格/换行或超过 200 字符——检查是不是粘错了内容"
    # 自定义预设必须手填 base_url + model，否则等于把 Agent 打到空地址上
    if preset_name == "custom" and (not custom_base.strip() or not custom_model.strip()):
        return "自定义配置需要同时填写 Base URL 和模型名"
    agent.configure(preset_name, custom_base, custom_model, api_key=key)
    cfg = {"api_key": key, "provider": preset_name}
    if preset_name == "custom":
        cfg["base_url"] = custom_base.strip()
        cfg["model"] = custom_model.strip()
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f)
    return f"当前状态：已配置（{PRESETS[preset_name]['label']} / {agent.model}）"

def refresh_sessions(user_val):
    """拉取该用户的会话列表，转成 gr.Radio 的 (标签, 值) 选项"""
    if user_val is None:
        return gr.update(choices=[])
    rows = user_store.list_sessions(user_val["id"])
    return gr.update(choices=[(r["title"] or r["id"][:8], r["id"]) for r in rows])

def do_login(username, password):
    """登录：authenticate 成功 → 藏登录卡片、显对话卡片、拉会话列表"""
    user = user_store.authenticate(username, password)
    if user is None:
        # 用户名错/密码错统一返回 None：不向攻击者泄露"用户名是否存在"
        return None, gr.update(visible=True), gr.update(visible=False), "❌ 用户名或密码错误", gr.update(choices=[])
    return (user,
            gr.update(visible=False),   # 登录卡片隐藏
            gr.update(visible=True),    # 对话卡片显示
            f"✅ 欢迎，{user['username']}",
            refresh_sessions(user))

def do_register(username, password):
    """注册：不自动登录，提示去点登录"""
    if not username or not password:
        return "❌ 用户名和密码不能为空"
    user = user_store.register(username, password)
    if user is None:
        return "❌ 用户名已被占用"
    return "✅ 注册成功，请点击【登录】"

def do_logout():
    """退出登录：清空全部 State，回到登录卡片"""
    return (None,
            gr.update(visible=True),
            gr.update(visible=False),
            "",
            gr.update(choices=[]),
            [],      # chatbot 清空
            None,    # session_state 清空
            None)    # pending_state 清空

def do_new_session(user_val):
    """新建会话：落 MySQL + 刷新列表 + 清空聊天区"""
    if user_val is None:
        return None, gr.update(choices=[]), []
    sid = user_store.create_session(user_val["id"], title="新会话")
    return sid, refresh_sessions(user_val), []

def do_switch_session(session_id, user_val):
    """切换会话：越权校验 + 从 checkpointer 恢复历史消息"""
    if not session_id or user_val is None:
        return [], None
    row = user_store.get_session_for_user(session_id, user_val["id"])
    if row is None:
        # 会话不属于该用户（IDOR 拦截）：什么都返回
        return [], None
    return agent.load_history(session_id), session_id

def chat(message, session_id, history, pending_val, user_val):
    """核心对话流程。返回 (回复文本, 新的挂起状态或 None)。
    pending 全程走参数，不再用模块级全局变量——全局 pending 在多用户下会串扰。"""
    # 入口一：挂起区有货 → 用户这条消息是对上一题命令的裁决
    if pending_val is not None:
        if time.time() - pending_val["timestamp"] > TIMEOUT:
            return "Pending request timed out.", None
        cmd = pending_val["command"]
        if message.lower() == "y":
            return tools.execute_command(cmd, approve=True), None
        elif message.lower() == "n":
            return "用户拒绝执行命令", None
        elif message.lower() == "always":
            WHITELIST.append(cmd)
            return tools.execute_command(cmd, approve=True), None
        else:
            return "输入无效", None

    # 入口零：配额闸——放在 LLM 调用之前，超支用户不烧 token
    if not user_store.check_quota(user_val["id"]):
        return "今日配额已用完，请明天再试。", None

    # 入口二：正常聊天
    sess = Session(id=session_id)
    row = user_store.get_session_for_user(session_id, user_val["id"])
    old_summary = (row or {}).get("summary") or ""
    if old_summary:
        sess.summary = old_summary
    reply = agent.run(message, sess)

    # 压缩产生的摘要回写 sessions.summary（不写回则每次刷新都从零压缩）
    if sess.summary and sess.summary != old_summary:
        user_store.update_session_summary(session_id, sess.summary)

    # 用量记账：sess.token_count 由 agent.run 从 usage_metadata 累加得到（真实 token）
    user_store.record_usage(user_val["id"], tokens=sess.token_count, requests=1)

    # 出口：NEED_CONFIRM 上抛为挂起状态，由 UI 弹确认卡片
    if reply.startswith("NEED_CONFIRM:"):
        cmd = reply.split("NEED_CONFIRM:")[1]
        prompt = f"命令 '{cmd}' 不在白名单，是否允许执行？(y/n): or 总是允许请输入always: "
        return prompt, {"command": cmd, "timestamp": time.time()}
    return reply, None

def on_send(message, history, pending_val, session_val, user_val):
    """用户点发送：先「思考中」，再跑对话/工具流程。生成器分两次 yield。"""
    if user_val is None:
        yield history, gr.update(value="等待工具调用…"), gr.update(visible=False), pending_val, session_val, "", gr.update(value="⚠️ 请先登录"), gr.update()
        return
    thinking_history = history + [{"role": "user", "content": message}]
    yield thinking_history, gr.update(value="等待工具调用…"), gr.update(visible=False), pending_val, session_val, "", gr.update(value="🤔 思考中…"), gr.update()
    if session_val is None:
        session_val = user_store.create_session(user_val["id"], title=(message[:20] or "新会话"))
    reply, new_pending = chat(message, session_val, history, pending_val, user_val)
    history = history + [
        {"role": "user", "content": message},
        {"role": "assistant", "content": reply},
    ]
    if new_pending is not None:
        yield (history,
               gr.update(value=f"**命令**：`{new_pending['command']}`"),
               gr.update(visible=True),
               new_pending,
               session_val,
               "",
               gr.update(value=""),
               refresh_sessions(user_val))
    else:
        yield history, gr.update(value="等待工具调用…"), gr.update(visible=False), None, session_val, "", gr.update(value=""), refresh_sessions(user_val)

def on_y(history, pending_val):
    """用户点同意"""
    if pending_val is None:
        return history, gr.update(visible=False), None, gr.update(value="没有挂起的命令")
    result = tools.execute_command(pending_val["command"], approve=True)
    return history + [
        {"role": "user", "content": f"执行命令：{pending_val['command']}"},
        {"role": "assistant", "content": result}
    ], gr.update(visible=False), None, gr.update(value="等待工具调用…")

def on_n(history, pending_val):
    """用户点拒绝"""
    if pending_val is None:
        return history, gr.update(visible=False), None, gr.update(value="没有挂起的命令")
    return history + [
        {"role": "user", "content": f"执行命令：{pending_val['command']}"},
        {"role": "assistant", "content": "用户拒绝执行命令"}
    ], gr.update(visible=False), None, gr.update(value="等待工具调用…")

def on_always(history, pending_val):
    """用户点总是允许：命令进白名单再执行"""
    if pending_val is None:
        return history, gr.update(visible=False), None, gr.update(value="没有挂起的命令")
    WHITELIST.append(pending_val["command"])
    result = tools.execute_command(pending_val["command"], approve=True)
    return history + [
        {"role": "user", "content": f"执行命令：{pending_val['command']}"},
        {"role": "assistant", "content": result}
    ], gr.update(visible=False), None, gr.update(value="等待工具调用…")

with gr.Blocks(title="AI Agent Chat Interface") as demo:
    # ===== 全局 State：每个浏览器标签页独立，天然按连接隔离 =====
    user_state = gr.State(value=None)      # 当前登录用户 dict（None=未登录）
    pending_state = gr.State(value=None)   # 挂起待确认的命令
    session_state = gr.State(value=None)   # 当前会话 UUID

    # ===== 登录 / 注册卡片（未登录时可见）=====
    with gr.Group(visible=True) as login_card:
        gr.Markdown("## AI Agent 平台 · 登录 / 注册")
        with gr.Row():
            reg_user = gr.Textbox(label="用户名", scale=2)
            reg_pwd = gr.Textbox(label="密码", type="password", scale=2)
        with gr.Row():
            login_btn = gr.Button("登录", variant="primary")
            register_btn = gr.Button("注册")
        login_msg = gr.Markdown("")

    # ===== 对话区（登录后可见）=====
    with gr.Group(visible=False) as chat_card:
        with gr.Row():
            # 左栏：会话列表
            with gr.Column(scale=1):
                gr.Markdown("### 我的会话")
                session_list = gr.Radio(label="", choices=[], interactive=True)
                new_session_btn = gr.Button("➕ 新会话")
                logout_btn = gr.Button("🚪 退出登录")
            # 右栏：聊天
            with gr.Column(scale=3):
                chatbot = gr.Chatbot()
                status = gr.Markdown(value="")
                # 确认卡片（夹在聊天记录和输入框之间）
                with gr.Group(visible=False) as confirm_card:
                    tool_info = gr.Markdown(value="等待工具调用…")
                    gr.Markdown("命令不在白名单，是否允许执行？")
                    with gr.Row():
                        yes_btn = gr.Button("同意", scale=1)
                        no_btn = gr.Button("拒绝", scale=1)
                        always_btn = gr.Button("总是允许", scale=1)
                with gr.Row():
                    msg = gr.Textbox(label="输入消息", placeholder="请输入消息...", lines=1, scale=4)
                    submit = gr.Button("发送", scale=1)
                with gr.Accordion("⚙️ API 设置", open=False):
                    preset_dd = gr.Dropdown(
                        label="推荐配置",
                        choices=[(p["label"], name) for name, p in PRESETS.items()],
                        value=agent.provider,
                    )
                    # 手填区：仅"自定义"时可见；初始可见性跟 config.json 里的 provider 走
                    with gr.Group(visible=(agent.provider == "custom")) as custom_box:
                        custom_base = gr.Textbox(
                            label="Base URL（OpenAI 兼容接口）",
                            placeholder="https://api.example.com/v1",
                        )
                        custom_model = gr.Textbox(label="模型名", placeholder="model-name")
                    key_box = gr.Textbox(label="API Key", type="password", placeholder="ms-xxxx... / sk-xxxx...")
                    key_btn = gr.Button("保存")
                    key_status = gr.Markdown(
                        "当前状态：" + (
                            f"已配置（{PRESETS[agent.provider]['label']} / {agent.model}）"
                            if agent.api_key else "未配置"
                        )
                    )

    # ===== 事件绑定：fn 返回值的顺序必须和 outputs 列表一一对应 =====
    login_btn.click(fn=do_login, inputs=[reg_user, reg_pwd],
                    outputs=[user_state, login_card, chat_card, login_msg, session_list])
    register_btn.click(fn=do_register, inputs=[reg_user, reg_pwd], outputs=[login_msg])
    logout_btn.click(fn=do_logout,
                     outputs=[user_state, login_card, chat_card, login_msg, session_list,
                              chatbot, session_state, pending_state])
    new_session_btn.click(fn=do_new_session, inputs=[user_state],
                          outputs=[session_state, session_list, chatbot])
    session_list.change(fn=do_switch_session, inputs=[session_list, user_state],
                        outputs=[chatbot, session_state])
    submit.click(fn=on_send,
                 inputs=[msg, chatbot, pending_state, session_state, user_state],
                 outputs=[chatbot, tool_info, confirm_card, pending_state, session_state, msg, status, session_list])
    msg.submit(fn=on_send,
               inputs=[msg, chatbot, pending_state, session_state, user_state],
               outputs=[chatbot, tool_info, confirm_card, pending_state, session_state, msg, status, session_list])
    # 下拉切换预设：控制手填区显隐
    preset_dd.change(
        fn=on_preset_change,
        inputs=[preset_dd],
        outputs=[custom_box],
    )
    key_btn.click(
        fn=save_provider_config,
        inputs=[preset_dd, custom_base, custom_model, key_box],
        outputs=[key_status],
    )
    yes_btn.click(fn=on_y, inputs=[chatbot, pending_state], outputs=[chatbot, confirm_card, pending_state, tool_info])
    no_btn.click(fn=on_n, inputs=[chatbot, pending_state], outputs=[chatbot, confirm_card, pending_state, tool_info])
    always_btn.click(fn=on_always, inputs=[chatbot, pending_state], outputs=[chatbot, confirm_card, pending_state, tool_info])

if __name__ == "__main__":
    demo.launch()