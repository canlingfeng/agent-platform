"""审计日志落库：把权限闸门的每次裁决写入 audit_logs。

设计要点：
  - 依赖 storage.db.execute（参数化 SQL，防注入）
  - user_id / session_id / client_ip 在权限层未必拿得到，故全部可空
  - 审计是旁路：写库失败只能打日志，绝不能让工具调用因审计挂掉而中断
"""
from storage.db import execute

def record_audit_log(tool: str, subject: str | None, decision: str,
                     user_id: int | None = None,
                     session_id: str | None = None,
                     client_ip: str | None = None):
    """记录审计日志"""
    try:
        execute(
            "INSERT INTO audit_logs "
            "(user_id, session_id, tool, subject, decision, client_ip) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (user_id, session_id, tool, subject, decision, client_ip),
        )
    except Exception as e:
        print(f"审计日志写库失败：{type(e).__name__}: {e}")