import os
from contextlib import contextmanager
import pymysql
from pymysql.cursors import DictCursor
from dbutils.pooled_db import PooledDB

DB_CONFIG = {
    "host": os.environ.get("AGENT_DB_HOST", "127.0.0.1"),
    "port": int(os.environ.get("AGENT_DB_PORT", "3306")),
    "user": os.environ.get("AGENT_DB_USER", "agent_app"),
    "password": os.environ.get("AGENT_DB_PASSWORD", ""),
    "database": os.environ.get("AGENT_DB_NAME", "agent_platform"),
    "charset": "utf8mb4",
    "init_command": "SET time_zone = '+00:00'",
}

# 连接池：复用 MySQL 连接，避免每个请求都新建连接（TCP 握手 + 认证开销）。
# - mincached/maxcached：保活空闲连接数，削峰；
# - maxconnections=10：并发上限；blocking=True 表示超量时排队而非报错（生产更稳）；
# - ping=1：借出前 ping 一下，自动剔除已被 MySQL 空闲超时断开的连接；
# - 借出的连接调 close() 是"归还池"（PooledDB 包装），并非真关闭。
_pool = PooledDB(
    creator=pymysql,
    mincached=2,
    maxcached=5,
    maxconnections=10,
    blocking=True,
    ping=1,
    cursorclass=DictCursor,
    **DB_CONFIG,
)


def get_conn():
    return _pool.connection()

def query(sql:str,params:tuple |list|None=None)->list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql,params or ())
            return cursor.fetchall()

def execute(sql: str, params: tuple | list | None = None) -> int: 
    with get_conn() as conn:
        with conn.cursor() as cur:
            affected = cur.execute(sql, params or ())
            conn.commit()                               
            return affected

@contextmanager
def transaction():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            yield cur    
        conn.commit()    
    except Exception:
        conn.rollback() 
        raise          
    finally:
        conn.close()    

def ping() -> bool:             
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                return cur.fetchone()["1"] == 1 
    except Exception as e:
        print("DB ping failed:", e)
        return False

if __name__ == "__main__":          
    print("DB ok:", ping())
    print("tables:", [r["Tables_in_agent_platform"] for r in query("SHOW TABLES")])