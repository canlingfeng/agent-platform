# ============================================================
# agent_platform · 应用镜像
# 基础镜像 python:3.11-slim：与开发环境 (Anaconda 3.11.5) 同大版本，
# slim = Debian 精简版；Linux 优先部署。
# ============================================================
FROM python:3.11-slim

# PYTHONUNBUFFERED=1：日志实时输出，不被 Python 输出缓冲区吞掉（排障必需）
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 非 root 用户运行：容器逃逸时权限受限，是上线安全基线
RUN useradd --create-home --uid 10001 appuser

# 运行时工作目录：core/permission.py 的 ROOT 指向 <项目根>/workspace，
# 文件工具全以它为根。.dockerignore 排除了 workspace/（不把本地文件带进镜像），
# 所以这里显式建空目录并授权给 appuser，避免 list_dir 报"目录不存在"。
RUN mkdir -p /app/workspace && chown appuser:appuser /app/workspace

# 先只拷依赖清单再安装 —— 利用层缓存，改业务代码不重装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再拷业务代码（--chown 让 appuser 拥有，运行时 workspace/ 才可写）
COPY --chown=appuser:appuser . .

USER appuser

# 仅声明文档端口，真正发布在 compose 的 ports
EXPOSE 8000

# 生产模式：不开 --reload（reload 会起 watchdog、改文件即重启，
# 容器里纯属浪费且会掩盖崩溃）。多 worker 后续再加 --workers。
CMD ["uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8000"]