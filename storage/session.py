"""会话与会话仓库：把 Agent 的记忆从 Agent 身上摘下来。"""
import uuid
from dataclasses import dataclass, field


@dataclass
class Session:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    memory: dict = field(default_factory=dict)
    tools: list = field(default_factory=list)
    tool_calls: list = field(default_factory=list)
    history: list = field(default_factory=list)
    summary: str = ""
    token_count: int = 0
    def add_tool(self, tool):
        self.tools.append(tool)

    def add_tool_call(self, tool_call):
        self.tool_calls.append(tool_call)

    def clear_tool_calls(self):
        self.tool_calls.clear()

class SessionStore:
    """会话仓库：管理多个会话。"""
    def __init__(self):
        self.sessions = {}

    def save(self, session):
        """把修改后的会话存回仓库（内存版就是覆盖映射；以后换数据库时这里改成落盘）"""
        self.sessions[session.id] = session
        
    def create_session(self):
        session = Session()
        self.sessions[session.id] = session
        return session

    def get_session(self, session_id):
        return self.sessions.get(session_id)

    def delete_session(self, session_id):
        if session_id in self.sessions:
            del self.sessions[session_id]