import os
import pymysql
from langgraph.checkpoint.mysql.pymysql import PyMySQLSaver

# 凭据单一来源：与 storage/db.py 共用同一组 AGENT_DB_* 环境变量
# （之前 checkpointer 单独读 AGENT_DB_URI、db.py 读 USER/PASSWORD，
#   两套源分裂导致"db.py 能连、checkpointer 1045"的灵异事件）
def _conn_params() -> dict:
    """优先 AGENT_DB_URI（上线注入用），否则按 AGENT_DB_* 组装，默认值与 db.py 对齐"""
    uri = os.environ.get("AGENT_DB_URI")
    if uri:
        return PyMySQLSaver.parse_conn_string(uri)
    return {
        "host": os.environ.get("AGENT_DB_HOST", "127.0.0.1"),
        "port": int(os.environ.get("AGENT_DB_PORT", "3306")),
        "user": os.environ.get("AGENT_DB_USER", "agent_app"),
        "password": os.environ.get("AGENT_DB_PASSWORD", ""),
        "database": os.environ.get("AGENT_DB_NAME", "agent_platform"),
    }


def build_checkpointer() -> PyMySQLSaver:
    # 建【原生 pymysql 连接】
    # autocommit=True —— LangGraph 的非 pipeline 路径靠它实时落盘，不开就写不进库
    # charset="utf8mb4" —— 显式对齐库的字符集，避免中文写入乱码
    params = _conn_params()
    try:
        conn = pymysql.connect(**params, autocommit=True, charset="utf8mb4")
    except pymysql.err.OperationalError as e:
        # 连接失败时抛【带排查指引】的 RuntimeError，替代一大串 traceback。
        # 最常见的两种失败：1045 认证失败（密码不对）、2003 连不上（MySQL 没起/主机端口错）。
        errno = getattr(e, "args", [None])[0]
        host = params.get("host")
        user = params.get("user")
        db = params.get("database")
        if errno == 1045:
            hint = (
                "MySQL 认证失败（1045 Access denied）。\n"
                "  连接参数：host=%s user=%s database=%s\n"
                "  常见原因：本地 MySQL 与 docker compose 起的 MySQL 是【两套实例】，\n"
                "  密码各自独立。docker compose 路径读 .env 的 AGENT_DB_PASSWORD 创建容器用户；\n"
                "  本地 `python run.py` 连的是本机 MySQL，其 agent_app 密码需单独设置。\n"
                "  修复（在本机 MySQL 里执行，密码用 .env 中 AGENT_DB_PASSWORD 的值）：\n"
                "    CREATE USER IF NOT EXISTS 'agent_app'@'localhost' IDENTIFIED BY '<.env 里的 AGENT_DB_PASSWORD>';\n"
                "    ALTER  USER 'agent_app'@'localhost' IDENTIFIED BY '<.env 里的 AGENT_DB_PASSWORD>';\n"
                "    GRANT ALL PRIVILEGES ON agent_platform.* TO 'agent_app'@'localhost';\n"
                "    FLUSH PRIVILEGES;\n"
                "  或者：不改本机 MySQL，直接用 docker compose up -d --build（该路径早已验证通过）。"
            ) % (host, user, db)
        elif errno == 2003:
            hint = (
                "连不上 MySQL（2003 Can't connect）。\n"
                "  连接参数：host=%s port=%s user=%s database=%s\n"
                "  排查：① 本机 MySQL 服务是否已启动（服务名 MySQL84 / MySQL）；\n"
                "        ② docker compose 路径下 app 连的是服务名 db 而非 127.0.0.1；\n"
                "        ③ 端口是否被占用/防火墙拦截。"
            ) % (host, params.get("port"), user, db)
        else:
            hint = "连接参数：host=%s user=%s database=%s\n原始错误：%s" % (host, user, db, e)
        raise RuntimeError("无法连接 MySQL，checkpointer 初始化失败。\n" + hint) from e

    # 把连接交给 PyMySQLSaver，再 setup() 幂等建 3 张 checkpoint 表
    # （checkpoints / checkpoint_writes / checkpoint_blobs）
    saver = PyMySQLSaver(conn)
    saver.setup()
    return saver