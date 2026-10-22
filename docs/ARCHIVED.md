# ARCHIVED — 已废弃代码归档与优化说明

> 本文件保存**已从生产代码中移除**或**已降级为遗留**的内容，
> 每一条都记录：被什么取代 / 原实现的问题（效率低在哪里）/ 优化在哪里。
> 需要恢复某段代码时，直接从本文件复制回去即可，无需翻 git 历史。
>
> 整理日期：2026-10-21 · 依据：全项目符号引用扫描（`uses=0` 判定为死代码）

---

## 一、已移除的死代码（原 `core/agent.py`）

以下三个方法经全项目扫描**引用数为 0**，已从 `core/agent.py` 删除。

### 1. `Agent.parse_llm_response()` — 解析 LLM 的 JSON 动作协议

**被什么取代**：LangGraph 的 `bind_tools` + `tool_calls` 原生结构化调用。

**原实现的问题（效率低在哪里）**：
- 依赖模型"自觉"输出合法 JSON。模型一旦多说一句寒暄或漏一个括号就解析失败
- 要手工剥 Markdown 代码围栏（` ``` `），属于对模型输出格式的补丁式修补
- 每轮多一次全量 `json.loads`，且错误路径只能返回一句"抱歉，我遇到了问题"，丢失上下文

**优化在哪里**：
- `ChatOpenAI.bind_tools()` 让模型按工具的 JSON Schema 输出 `tool_calls`，由框架保证结构合法
- `ToolNode` 自动执行并回填 `ToolMessage`，不需要应用层解析
- 失败可走 LangGraph 的重试/分支逻辑，而不是返回一句兜底文案

**原代码**：
```python
def parse_llm_response(self, response: str) -> dict:
    """解析 LLM 的响应为结构化数据。"""
    try:
        text = response.strip()
        if text.startswith("```"):
            n = text.find("\n")
            if n != -1:
                text = text[n+1:]
        if text.endswith("```"):
            text = text[:-3].strip()
        return json.loads(text)
    except json.JSONDecodeError:
        return {"action": "answer", "content": "抱歉，我遇到了问题。"}
```

---

### 2. `Agent._history_to_messages()` — 把 dict 历史手工转回 Message 对象

**被什么取代**：LangGraph Checkpointer（`PyMySQLSaver`）自动恢复。

**原实现的问题（效率低在哪里）**：
- 手工遍历 dict 重建 `HumanMessage` / `AIMessage` / `ToolMessage`，**丢失关键字段**：
  `tool_calls`（工具调用请求）、`id`（消息 ID）、`usage_metadata`（token 用量）
- token 记账依赖 `usage_metadata`，手工重建后 `_compress` 与用量统计全部失效
- 无法利用 checkpointer 的增量恢复，每轮都要全量重建

**优化在哪里**：
- `graph.get_state(config)` 直接从 MySQL 取回完整 Message 序列，字段无损
- `GET /api/v1/sessions/{id}/messages` 走的就是这条路（`Agent.load_history`）

**原代码**：
```python
def _history_to_messages(self, history):
    out = []
    for msg in history:
        if msg["role"] == "user":
            out.append(HumanMessage(content=msg["content"]))
        elif msg["role"] == "assistant":
            out.append(AIMessage(content=msg["content"]))
        elif msg["role"] == "tool":
            out.append(ToolMessage(content=msg["content"]))
    return out
```

---

### 3. `Agent.execute_tool()` — 外部逐个调用工具

**被什么取代**：`core/agent_graph.py` 的 `ToolNode(agent.tools_lc)`。

**原实现的问题（效率低在哪里）**：
- 绕过 LangGraph 的图执行，**不受 `max_iterations` 迭代上限保护**，外部循环调用有死循环风险
- 执行结果不会自动回填进 `messages`，调用方要自己拼上下文
- 需要调用方显式传 `user_id` 才能做租户隔离——漏传就静默退化到全局 `workspace/`，
  这是一个"靠人记得"的脆弱接口

**优化在哪里**：
- 工具执行改由图的 `tools` 节点驱动，`route_after_model` 统一控制迭代次数
- 身份注入改走 `core/context.py` 的 contextvars，在 API 层统一设置，
  **不依赖调用方传参，LLM 也无法篡改**（工具参数会进 JSON Schema，可被提示词伪造）

> 注：该方法在 2026-10-21 曾修复过语法错误并补全身份注入，但扫描确认**全项目无调用点**，
> 依"无引用即移除"原则归档。若将来要做 MCP / 管理端点，可从本文件恢复。

**原代码**（修复后的最终版）：
```python
def execute_tool(self, tool_name: str, params: dict = None,
                 user_id: int | None = None, session_id: str | None = None,
                 client_ip: str | None = None) -> str:
    tool = self.tools.get(tool_name)
    if not tool:
        return f"工具 {tool_name} 不存在。"
    token = set_identity(RequestIdentity(user_id=user_id,
                                         session_id=session_id,
                                         client_ip=client_ip))
    try:
        return tool.fn(**(params or {}))
    except Exception as e:
        return f"执行工具 {tool_name} 时出错：{e}"
    finally:
        reset_identity(token)
```

---

## 二、已移入 `_deprecated/` 的入口文件（2026-10-21 整理）

以下文件已从项目根**移动**（非复制）到 `_deprecated/`，原位置不再保留。
详细原因与恢复方法见 `_deprecated/README.md`。

### 4. `webui.py` — Gradio 多用户界面

**被什么取代**：`run.py` → `api/app.py`（FastAPI）+ `static/`（原生 JS PWA）。

**原实现的问题（效率低在哪里）**：
- Gradio 的组件状态存在**服务端**，多用户并发下存在状态串扰风险
- 不暴露 REST API，移动端 / 第三方无法接入
- 流式输出依赖 Gradio 的 generator 封装，细粒度控制弱
- **依赖体积大**：gradio 是重依赖，拖慢镜像构建（已从 `requirements.txt` 移除）

**优化在哪里**：
- FastAPI 无状态 + JWT，天然支持多租户与多端（同一套 API 给 Web/PWA/移动端）
- SSE（`/api/v1/chat/stream`）实现真正的增量流式
- 前端零框架零构建，`COPY` 进镜像即用；去掉 gradio 后镜像显著变小、构建更快

### 5. `main.py` — CLI 交互模式

**状态**：已移入 `_deprecated/`。本地调试现在有 `docker compose`（全链路）与
`/health` 探针覆盖，CLI 路径反而要额外维护 `storage/session.py` 的 `SessionStore`。

### 6. `debug_llm.py` — LLM 连通性调试脚本

**状态**：已移入 `_deprecated/`。原用途是不经过图和数据库直接打 LLM 接口，
判断"是 LLM 配置问题还是 Agent/DB 问题"。该场景现已由
`docker compose logs -f app` + `GET /health` 大部分替代。

---

## 三、数据库层

### 7. `messages` 表 — 设计了但从未接线

**现状**：全项目扫描，`INSERT INTO messages` 与 `FROM messages` **均为 0 处**。
`schema.sql` 中该表的 DDL 已加注释标注"预留未启用"。

**被什么取代**：LangGraph 的 `checkpoints` / `checkpoint_writes` / `checkpoint_blobs` 三表
（由 `PyMySQLSaver.setup()` 自动创建）。

**原设计意图与问题**：
- 意图是"history 拆表而非塞 JSON 列：避免写放大、支持分页、避免并发覆盖丢失"
- 但从未接线，等于**零收益的空表**，还占一份 schema 维护成本
- README 曾把它当核心设计亮点宣传，属于文档与实现不符（已修正）

**优化在哪里**：
- 当前历史实际由 checkpointer 承载，`GET /sessions/{id}/messages` 读 `graph.get_state()`
- 若将来需要独立的分页 / 检索 / 统计，可再把 `messages` 表接上（DDL 已就绪，含 `seq` 序号与唯一键）

---

## 四、配置与默认值

### 8. `MemorySaver` 作为 `checkpointer` 默认值（`core/agent.py`）

**现状**：`Agent()` 不传 checkpointer 时静默使用进程内存版。

**问题（效率低在哪里）**：
- **不安全默认值**：任何新入口忘传 checkpointer，就静默退化成"重启即丢、多 worker 不共享"
- 三个正式入口（`api/app.py`、`webui.py`、`main.py`）都显式传了 MySQL 版，
  只有 `debug_llm.py` 用到默认值，所以功能上没出事——但这是运气不是设计

**优化方向**（未实施，改动会影响 `debug_llm.py`）：
```python
# 方案 A：改为必填
def __init__(self, tools=None, checkpointer):  # 去掉默认值
# 方案 B：缺失即报错
if checkpointer is None:
    raise ValueError("checkpointer 必传：内存版无法用于多租户生产环境")
```

---

## 五、备份文件清单

历史备份（`.bak` / `.orig`）已于 2026-10-21 **移入 `_deprecated/backups/`**，
原位置不再保留。内容在 WorkBuddy 会话目录另有完整备份。
`.gitignore` 已补 `*.bak` / `*.bak2`，`.dockerignore` 已排除整个 `_deprecated/`。

清单：`schema.sql.orig`、`api/schemas.py.bak`、`static/index.html.bak`、
`static/css/style.css.bak`、`static/js/app.js.bak`、`static/js/app.js.bak2`。

---

## 六、连带影响（2026-10-21 整理 `_deprecated/` 时产生）

- **`requirements.txt` 删除 `gradio`**：全项目扫描确认 gradio 只被 `webui.py` 引用。
  收益：镜像更小、`pip install` 阶段更快。代价：恢复 webui 需单独装 gradio。
- **`storage/session.py` 的 `SessionStore` 失去唯一使用者**（原 `main.py` 已移走）。
  同一文件里的 `Session` 数据类仍被 `api/chat.py` 使用，故**文件保留、类待清理**。
- **`.dockerignore` 新增 `_deprecated/`**：归档文件不进镜像。

---

## 七、清理后仍保留但引用很少的东西（有意为之）

| 符号 | 引用数 | 保留理由 |
|---|---|---|
| `Agent.call_llm()` | 1（`_compress` 内部） | 上下文压缩要调一次 LLM 生成摘要，走裸 `requests` 而非 ChatOpenAI——避免与主链路的状态耦合 |
| `Agent.load_history()` | 3 | `GET /sessions/{id}/messages` 与 webui 都在用 |
| `SessionStore` | 0（原唯一使用者 `main.py` 已移入 `_deprecated/`） | 已成死代码，但与其共文件的 `Session` 数据类仍被 `api/chat.py` 使用，故文件保留、类待清理 |
| `core/prompt.py::build_system_prompt` | 1 | 2026-10-21 修复"import 了但从未调用"后接入 `call_model`，现已真正生效 |
