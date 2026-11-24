"""审查项 #1 回归：/chat/confirm 挂起登记 + 一次性 token（TDD 先行用例）。

背景缺陷：修复前的 /chat/confirm 只重跑 decide() + 检查 decision=="allow"，
不校验 command 是否真的来自 Agent 的 NEED_CONFIRM 挂起——任何登录用户
直接 POST {"command": 任意命令, "decision": "allow"} 即可执行任意不含
元字符的命令，白名单和确认弹框对直接调 API 的人形同虚设（RCE 端点）。

修复后契约（本文件锁死的行为，实现必须满足）：
  1. Agent 产生 NEED_CONFIRM 时，服务端经 storage.confirm_registry.register()
     登记 {session_id, user_id, command} 并返回一次性 token，随 {"ask"} 帧下发；
  2. confirm 端点校验顺序：会话归属 404 → decide() DENY 403 →
     confirm_registry.consume(token, session_id, user_id, command) 403
     → decision=="allow" 才执行 execute_command；
  3. consume 是原子的"校验即删除"：token 匹配四元组才删除并返回 True，
     任何一项不符或已消费过 → False（对应 SQL: DELETE ... WHERE 四元组全匹配，
     DELETE 返回行数 0 即 False——错命令不会误删 token）；
  4. DENY 命令即使持有合法 token 也必须 403（decide() 闸在 consume 之前，
     否则攻击者可用"错命令+错 token"探测登记表状态）。

运行状态：storage.confirm_registry 尚未实现 → 模块顶部 importorskip 让
本文件整体 skip、不拖垮收集；#1 实现落地后自动激活。
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# #1 的存储模块落地前，本文件所有用例 skip（suite 保持全绿）；
# 实现 storage/confirm_registry.py 后此行自动放行
pytest.importorskip("storage.confirm_registry")

from api import chat as chat_mod          # noqa: E402
from api.deps import get_current_user     # noqa: E402

# 非白名单、无元字符 → decide() 判 ASK，不会触发 DENY 闸，专门用于走"合法放行"路径
CMD_ASK = "notepad.exe"
# 含 > 命中元字符闸 → decide() 判 DENY，用于验证闸门优先于 token 校验
CMD_DENY = "echo hi > a.txt"


class FakeRegistry:
    """内存版 confirm_registry 假实现，语义对齐 DELETE...WHERE 契约：

    - items：token -> (session_id, user_id, command) 的登记表；
    - calls：记录每次 consume 的实参，供用例断言"有没有被调、传了什么"；
    - consume：四元组全匹配才删除并返回 True；不符返回 False 且【不删】
      （与 DELETE WHERE 全匹配语义一致，避免错命令探测误删合法 token）。
    """

    def __init__(self):
        self.items = {}
        self.calls = []

    def register(self, sid, uid, cmd):
        token = f"tok-{len(self.items) + 1}"
        self.items[token] = (sid, uid, cmd)
        return token

    def consume(self, token, sid, uid, cmd):
        self.calls.append((token, sid, uid, cmd))
        if self.items.get(token) == (sid, uid, cmd):
            del self.items[token]
            return True
        return False


def make_app(monkeypatch, registry, executed):
    """最小可测 app：mock user_store（会话归属查询）+ execute_command（执行记录器）
    + confirm_registry（换成 FakeRegistry），依赖覆盖注入假登录用户。"""
    monkeypatch.setattr(chat_mod.user_store, "get_session_for_user",
                        lambda sid, uid: {"id": sid, "title": "t", "summary": ""})
    monkeypatch.setattr(chat_mod.confirm_registry, "register",
                        registry.register)
    monkeypatch.setattr(chat_mod.confirm_registry, "consume",
                        registry.consume)
    # 执行记录器：不真跑命令，只记参数（返回 "ok" 模拟命令输出）
    monkeypatch.setattr(chat_mod, "execute_command",
                        lambda cmd, approve=False: (executed.append(cmd) or "ok"))

    app = FastAPI()
    app.include_router(chat_mod.router)
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "tester", "role": "user"}
    return app


def post_confirm(client, token, command, decision="allow", session_id="s1"):
    """统一构造 confirm 请求体，减少用例里的重复字面量。"""
    return client.post("/api/v1/chat/confirm",
                       json={"session_id": session_id, "command": command,
                             "decision": decision, "token": token})


def test_正确token放行(monkeypatch):
    """合法登记 + 四元组全匹配 → 200，命令真的被执行（且只执行这一次）。"""
    registry = FakeRegistry()
    registry.register("s1", 1, CMD_ASK)
    executed = []
    app = make_app(monkeypatch, registry, executed)
    with TestClient(app) as client:
        resp = post_confirm(client, "tok-1", CMD_ASK)
    assert resp.status_code == 200
    assert executed == [CMD_ASK]


def test_错误token拒绝(monkeypatch):
    """token 不在登记表 → 403，命令不执行。"""
    registry = FakeRegistry()
    registry.register("s1", 1, CMD_ASK)
    executed = []
    app = make_app(monkeypatch, registry, executed)
    with TestClient(app) as client:
        resp = post_confirm(client, "tok-wrong", CMD_ASK)
    assert resp.status_code == 403
    assert executed == []


def test_token一次性消费(monkeypatch):
    """同一 token 第二次确认必须拒绝（防重放：一次挂起只允许确认一次）。"""
    registry = FakeRegistry()
    registry.register("s1", 1, CMD_ASK)
    executed = []
    app = make_app(monkeypatch, registry, executed)
    with TestClient(app) as client:
        first = post_confirm(client, "tok-1", CMD_ASK)
        second = post_confirm(client, "tok-1", CMD_ASK)
    assert first.status_code == 200
    assert second.status_code == 403
    assert executed == [CMD_ASK]     # 命令只跑了一次


def test_命令与登记不一致拒绝(monkeypatch):
    """持合法 token 但 command 与登记值不同 → 403（防止偷换命令），
    且 token 不被误删（DELETE WHERE 全匹配语义），仍可被原命令消费。"""
    registry = FakeRegistry()
    registry.register("s1", 1, CMD_ASK)
    executed = []
    app = make_app(monkeypatch, registry, executed)
    with TestClient(app) as client:
        bad = post_confirm(client, "tok-1", "evil.exe")
        # 断言紧跟 bad 请求：证明偷换命令未被执行（good 请求还没发）
        assert bad.status_code == 403
        assert executed == []
        good = post_confirm(client, "tok-1", CMD_ASK)
    assert good.status_code == 200   # 原命令仍能正常消费，token 未被 bad 请求废掉
    assert executed == [CMD_ASK]


def test_DENY命令持合法token仍被挡(monkeypatch):
    """命令命中元字符闸（DENY）→ 即使 token 完全合法也 403。
    顺序契约：decide() 闸在 consume 之前——先 consume 会给攻击者
    提供"用错命令探测登记表"的侧信道。"""
    registry = FakeRegistry()
    registry.register("s1", 1, CMD_DENY)
    executed = []
    app = make_app(monkeypatch, registry, executed)
    with TestClient(app) as client:
        resp = post_confirm(client, "tok-1", CMD_DENY)
    assert resp.status_code == 403
    assert executed == []
    # DENY 挡在 consume 之前：登记未被消费，consume.calls 应为空
    assert registry.calls == []
