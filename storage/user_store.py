"""
user_store.py — 多租户用户体系（注册 / 登录 / 配额 / 会话持久化）
依赖：db.py（query / execute / transaction）、bcrypt、uuid
设计要点：
  - 密码 bcrypt 哈希（绝不明文）
  - 对外 ID 用 uuid4（防枚举 / IDOR）
  - 注册 = users + quotas 同一事务（原子）
  - 每日用量 ON DUPLICATE KEY UPDATE 幂等累加
  - 会话归属 user_id（多租户隔离），读会话必须同时绑 id + user_id（防越权）
"""
import uuid
import bcrypt
from datetime import datetime, timezone
from storage.db import query, execute, transaction

def hash_password(password: str) -> str:
    """哈希密码"""
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password.encode("utf-8"), salt)
    return hashed.decode("utf-8")

def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))

def register(username: str, password: str) -> dict | None:  
    if query("select * from users where username=%s", (username,)):
        return None 
    pw_hash = hash_password(password)
    user_uuid = str(uuid.uuid4())
    with transaction() as cur:
        cur.execute(
            "INSERT INTO users (uuid, username, password_hash, role, status) "
            "VALUES (%s, %s, %s, 'user', 1)",
            (user_uuid, username, pw_hash),
        )
        user_id=cur.lastrowid
        cur.execute(
            "INSERT INTO quotas (user_id) VALUES (%s)", (user_id,)
        )
    return get_user_by_uuid(user_uuid)

def authenticate(username: str, password: str) -> dict | None:
    rows = query("SELECT * FROM users WHERE username=%s", (username,))
    if not rows:
        return None
    user = rows[0]
    if not verify_password(password, user["password_hash"]):
        return None
    # 禁用用户（status=0）一律拒绝登录，密码正确也不放行
    if user.get("status") != 1:
        return None
    execute("UPDATE users SET last_login_at=%s WHERE id=%s",
            (datetime.now(timezone.utc), user["id"]))
    return get_user_by_uuid(user["uuid"])


def get_user_by_uuid(user_uuid: str) -> dict | None:
    rows = query("SELECT id, uuid, username, role, status, created_at "
                 "FROM users WHERE uuid=%s", (user_uuid,))
    return rows[0] if rows else None


def get_user_by_id(user_id: int) -> dict | None:
    """按内部主键 id 取用户（JWT 解析出 sub=user_id 后用）。只返回安全字段。"""
    rows = query("SELECT id, uuid, username, role, status, created_at "
                 "FROM users WHERE id=%s", (user_id,))
    return rows[0] if rows else None


def check_quota(user_id: int) -> bool:
    today = datetime.now(timezone.utc).date()
    q = query("SELECT * FROM quotas WHERE user_id=%s", (user_id,))
    if not q:
        return False
    usage = query("SELECT * FROM usage_daily WHERE user_id=%s AND usage_date=%s",
                  (user_id, today))
    used = usage[0] if usage else {"used_tokens": 0, "used_requests": 0}
    return (used["used_tokens"] < q[0]["daily_token_limit"]
            and used["used_requests"] < q[0]["daily_request_limit"])


def record_usage(user_id: int, tokens: int = 0, requests: int = 1,
                 tool_calls: int = 0, denied_calls: int = 0) -> None:
    today = datetime.now(timezone.utc).date()
    execute(
        "INSERT INTO usage_daily "
        "(user_id, usage_date, used_tokens, used_requests, tool_calls, denied_calls) "
        "VALUES (%s, %s, %s, %s, %s, %s) AS new "
        "ON DUPLICATE KEY UPDATE "
        "used_tokens = usage_daily.used_tokens + new.used_tokens, "
        "used_requests = usage_daily.used_requests + new.used_requests, "
        "tool_calls = usage_daily.tool_calls + new.tool_calls, "
        "denied_calls = usage_daily.denied_calls + new.denied_calls",
        (user_id, today, tokens, requests, tool_calls, denied_calls),
    )


def create_session(user_id: int, title: str = "") -> str:
    session_id = str(uuid.uuid4())
    execute("INSERT INTO sessions (id, user_id, title) VALUES (%s, %s, %s)",
            (session_id, user_id, title))
    return session_id


def list_sessions(user_id: int) -> list[dict]:
    return query(
        "SELECT id, title, created_at, updated_at FROM sessions "
        "WHERE user_id=%s AND deleted_at IS NULL ORDER BY updated_at DESC",
        (user_id,),
    )


def get_session_for_user(session_uuid: str, user_id: int) -> dict | None:
    rows = query(
        "SELECT id, user_id, title, summary, created_at FROM sessions "
        "WHERE id=%s AND user_id=%s AND deleted_at IS NULL",
        (session_uuid, user_id),
    )
    return rows[0] if rows else None

def update_session_summary(session_id: str, summary: str) -> None:
    """回写压缩摘要到 sessions.summary（长期记忆的持久化）"""
    execute("UPDATE sessions SET summary=%s WHERE id=%s", (summary, session_id))


def touch_session(session_id: str, user_id: int) -> None:
    """刷新会话的 updated_at，让"最近活跃"的会话排到列表前面。

    背景：消息实际写进 LangGraph 的 checkpointer，sessions 表不会被碰，
    而 list_sessions 按 updated_at DESC 排序（并建了 idx_sessions_user 索引），
    不主动更新的话列表顺序会停留在创建顺序，索引也形同虚设。

    必须同时带 user_id——只按 session_id 更新等于放弃租户隔离，
    调用方传错 id 就能改到别人的会话。
    """
    execute(
        "UPDATE sessions SET updated_at=CURRENT_TIMESTAMP(3) WHERE id=%s AND user_id=%s",
        (session_id, user_id),
    )


if __name__ == "__  main__":
    uname = f"smoke_{uuid.uuid4().hex[:8]}"
    u = register(uname, "Passw0rd!")
    print("register ->", u)
    a = authenticate(uname, "Passw0rd!")
    print("authenticate ->", a is not None)
    print("check_quota ->", check_quota(a["id"]))
    record_usage(a["id"], tokens=123, requests=1)
    sid = create_session(a["id"], title="测试会话")
    print("list_sessions ->", list_sessions(a["id"]))
    print("get_session_for_user ->", get_session_for_user(sid, a["id"]) is not None)