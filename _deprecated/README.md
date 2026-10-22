# _deprecated — 已废弃文件归档

> 本目录保存**已从项目主动路径中移除**的文件。它们是 `move`（移动）进来的，
> 原位置不再保留，避免项目里出现两份容易改错的代码。
>
> 每个条目都写明：**为什么不用了 / 被什么取代 / 怎么恢复**。
> 恢复方法：把文件移回项目根目录即可（注意恢复 `webui.py` 需先 `pip install gradio`，
> 且 `requirements.txt` 里的 gradio 依赖已随之删除）。

整理日期：2026-10-21

---

## 一、废弃的入口文件

### `webui.py`

Gradio 多用户界面。主入口已换成 run.py -> api/app.py（FastAPI）+ static/ 原生 JS PWA。换掉原因：① Gradio 组件状态存服务端，多用户并发有状态串扰风险；② 不暴露 REST API，移动端/第三方无法接入；③ 流式输出细粒度控制弱；④ gradio 依赖体积大，拖慢镜像构建。恢复运行需先 pip install gradio。

### `main.py`

CLI 交互模式。主入口为 run.py。保留价值低：本地调试现在有 debug_llm.py（LLM 连通性）和 docker compose（全链路），CLI 路径反而要额外维护 storage/session.py 的 SessionStore。

### `debug_llm.py`

LLM 连通性调试脚本。不经过 LangGraph 和数据库，用于判断'是 LLM 配置问题还是 Agent/DB 问题'。已由 docker compose logs + /health 探针大部分替代；如确需直接打 LLM，可从本目录恢复到项目根运行。

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
