import secrets


from datetime import datetime, timedelta, timezone
from storage.db import execute

TTL_MINUTES = 10


def register(session_id:str,user_id:int,command: str)->str:
    """登记挂起请求并签发一次性 token。返回 token。"""
    cutoff=datetime.now(timezone.utc) - timedelta(minutes=TTL_MINUTES)
    execute("delete from confirm_pending where created_at < %s", (cutoff,))
    token = secrets.token_urlsafe(32)
    execute("insert into confirm_pending (token, session_id, user_id, command) values (%s, %s, %s, %s)", (token, session_id, user_id, command))
    return token

def consume(token: str, session_id: str, user_id: int, command: str) -> bool:
    """校验并消费：四元组全匹配才删除并返回 True，否则 False 且不删。""" 
    affected = execute(
        "delete from confirm_pending where token=%s and session_id=%s "
        "and user_id=%s and command=%s",
        (token, session_id, user_id, command),
    )
    return affected > 0    