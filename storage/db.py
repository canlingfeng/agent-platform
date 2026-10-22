import os
from contextlib import contextmanager
import pymysql
from pymysql.cursors import DictCursor

DB_CONFIG={
    "host": os.environ.get("AGENT_DB_HOST", "127.0.0.1"),
    "port": int(os.environ.get("AGENT_DB_PORT", "3306")),
    "user": os.environ.get("AGENT_DB_USER", "agent_app"),
    "password": os.environ.get("AGENT_DB_PASSWORD",""),
    "database": os.environ.get("AGENT_DB_NAME", "agent_platform"),
    "charset":  "utf8mb4",  
    "init_command":"SET time_zone = '+00:00'",
}

def get_conn():
    return pymysql.connect( cursorclass=DictCursor,**DB_CONFIG)

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