# _deprecated — 已废弃文件归档

> 本目录保存**已从项目主动路径中移除**的文件。它们是 `move`（移动）进来的，
> 原位置不再保留，避免项目里出现两份容易改错的代码。
>
> 每个条目都写明：**为什么不用了 / 被什么取代 / 怎么恢复**。
> 恢复方法：把文件移回项目根目录即可（注意恢复 `webui.py` 需先 `pip install gradio`，
> 且 `requirements.txt` 里的 gradio 依赖已随之删除）。
>
> 本目录内的 Python 脚本已在文件头用 `__file__` 上跳一级把项目根加入
> `sys.path`（模块搜索路径），因此**直接在本目录运行也能解析 `core` /
> `storage` 包**，无需先移回根目录；路径不写死盘符，Windows / Linux 通用。

整理日期：2026-10-21 · 补充整理：2026-11-09

---

## 一、废弃的入口文件

### `webui.py`

Gradio 多用户界面。主入口已换成 run.py -> api/app.py（FastAPI）+ static/ 原生 JS PWA。换掉原因：① Gradio 组件状态存服务端，多用户并发有状态串扰风险；② 不暴露 REST API，移动端/第三方无法接入；③ 流式输出细粒度控制弱；④ gradio 依赖体积大，拖慢镜像构建。恢复运行需先 pip install gradio。

### `main.py`

CLI 交互模式。主入口为 run.py。保留价值低：本地调试现在有 debug_llm.py（LLM 连通性）和 docker compose（全链路），CLI 路径反而要额外维护 storage/session.py 的 SessionStore。

### `debug_llm.py`

LLM 连通性调试脚本。不经过 LangGraph 和数据库，用于判断"是 LLM 配置问题还是 Agent/DB 问题"。已由 docker compose logs + /health 探针大部分替代；如确需直接打 LLM，可从本目录恢复到项目根运行。

### `diag_uvicorn.py`（2026-11-09 由项目根移入）

临时复现启动器，与 `run.py` 等价但用 8002 端口避免冲突。属一次性排障脚本，不纳入版本库（`.gitignore` 已列出）。文件头已改为用 `__file__` 推导项目根并加载根目录 `.env`，原写死的 Windows 绝对路径已移除。

### `test_echo_server.py`（2026-11-09 由 `tests/` 移入）

MCP server 的早期验收桩：文件内直接构造 `MCPServer` 并以 stdio 运行，**没有任何测试函数或断言**，放在 `tests/` 下会被 pytest（测试框架）当作测试模块收集却产出 0 个用例。正式版本为 `mcp_servers/echo_server.py`（含 stdout 保护与平台无关说明）。此桩仅作历史留存。

---

## 二、历史备份文件

这些是开发过程中的 `.bak` / `.orig` 快照。内容在 WorkBuddy 会话目录另有完整备份，
此处仅作留存，**不参与构建**（`.dockerignore` 已排除整个 `_deprecated/`）。

| 文件 | 大小 | 说明 |
|---|---|---|
| `schema.sql.orig` | 3341 B | schema 重构前的旧版 DDL（无 uuid/双 ID 设计） |
| `schemas.py.bak` | 1344 B | 删除 schemas.py 自引用 import 前的备份 |
| `index.html.bak` | 1448 B | UI 改版（对齐 WorkBuddy）前的首页 |
| `style.css.bak` | 2088 B | UI 改版前的样式表 |
| `app.js.bak` | 4648 B | 前端重构（竞态修复前）的 app.js |
| `app.js.bak2` | 4877 B | 前端重构（UI 对齐 WorkBuddy）时的更早备份 |
| `.env.bak.20261021-210051` | 419 B | 环境变量快照，**含密钥**；2026-11-09 由项目根移入（详见下方安全说明） |

### 安全说明：为何把 `.env.bak.*` 移入本目录

`.dockerignore` 原本只排除 `.env` 与 `*.bak`，而 `.env.bak.20261021-210051`
以日期数字结尾，两项规则都不匹配，`Dockerfile` 的 `COPY . .` 会把它打进镜像，
造成密钥随镜像分发。现已移入 `_deprecated/backups/`（整目录被 `.dockerignore`
排除），并在 `.dockerignore` 补 `.env.*` + `!.env.example` 作为纵深防御。

---

## 三、连带影响

- **`requirements.txt` 已删除 `gradio`**：全项目扫描确认 gradio 只被 `webui.py` 引用。
  移走 webui.py 后该依赖成为纯负担（体积大、拖慢镜像构建），故一并移除。
  **效果**：镜像更小、`pip install` 阶段显著更快。
- **`storage/session.py` 的 `SessionStore` 随之失去唯一使用者**（原 `main.py`）。
  同一文件里的 `Session` 数据类仍被 `api/chat.py` 使用，故文件保留、类不删，
  已在 `docs/ARCHIVED.md` 标注为待清理。
- **`.dockerignore` 已加 `_deprecated/`**：这些文件不会进镜像。

## 四、当前真正的入口

| 用途 | 命令 |
|---|---|
| 本地开发 / 运行 | `python run.py` |
| 容器化部署 | `docker compose up -d --build` |
| 健康检查 | `GET /health` |
