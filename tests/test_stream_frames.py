"""SSE 帧协议回归：tool 占位帧 / usage 帧 / 单次 token 上限截断 / 额度耗尽 429。

固化两条此前只有手工探针验证过的路径：
  1. 正常流帧序（方案 B）：{"tool": ...} → {"t": ...} → {"usage": ...} → {"done": true}
     （usage 必须在 done 之前——前端收到 done 直接 return，之后发的帧没人消费）
  2. 单次上限截断（P1-4）：累计 token 触达 min(单请求上限, 剩余额度) 后 producer
     立即 return，后续文本帧不再出现；usage 帧 capped=True、turn_tokens=已消耗值。

实现方式：构造最小 FastAPI app 挂载 api.chat.router，FakeGraph 模拟
graph.stream 的 super-step 增量，monkeypatch 掉 user_store 全部 DB 函数。
全程离线（依赖 storage.db 的惰性建池——import 期不连 MySQL）。
"""
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage

from api import chat as chat_mod
from api.deps import get_current_user


class FakeGraph:
    """按预设步骤产出 chunk，模拟 agent.graph.stream 的 super-step 增量。"""

    def __init__(self, steps):
        self.steps = steps

    def stream(self, inputs, config):
        yield from self.steps


USAGE_OK = {
    "used_tokens": 5, "used_requests": 1,
    "limit_tokens": 100, "limit_requests": 10,
    "remaining_tokens": 95,
}


def make_app(monkeypatch, agent, usage):
    """把 user_store 的 DB 函数全部换成内存假实现，构造最小可测 app。"""
    monkeypatch.setattr(chat_mod.user_store, "get_session_for_user",
                        lambda sid, uid: {"id": sid, "title": "t", "summary": ""})
    monkeypatch.setattr(chat_mod.user_store, "consume_quota",
                        lambda uid, requests=0, tokens=0: True)
    monkeypatch.setattr(chat_mod.user_store, "record_usage",
                        lambda uid, tokens=0, requests=0: None)
    monkeypatch.setattr(chat_mod.user_store, "touch_session",
                        lambda sid, uid: None)
    monkeypatch.setattr(chat_mod.user_store, "get_usage_today", lambda uid: dict(usage))

    app = FastAPI()
    app.include_router(chat_mod.router)
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "tester", "role": "user"}
    app.state.agent = agent
    return app


def parse_frames(resp):
    """把 SSE 响应解析成帧 dict 列表（只认 "data: " 前缀行）。"""
    frames = []
    for raw in resp.iter_lines():
        line = raw if isinstance(raw, str) else raw.decode("utf-8")
        if line.startswith("data: "):
            frames.append(json.loads(line[len("data: "):]))
    return frames


def frame_kind(f):
    for k in ("tool", "t", "usage", "done", "ask"):
        if k in f:
            return k
    return "?"


def test_正常流帧序_tool在前(monkeypatch):
    """方案 B 帧序：先 tool 占位帧，再文本帧，usage 在 done 之前。"""
    steps = [{
        "call_model": {"messages": [
            ToolMessage(content="ok", name="list_dir", tool_call_id="t1"),
            AIMessage(content="done reply"),
        ]},
    }]
    agent = SimpleNamespace(provider="custom", model="fake", graph=FakeGraph(steps))
    app = make_app(monkeypatch, agent, USAGE_OK)
    with TestClient(app) as client:
        with client.stream("POST", "/api/v1/chat/stream",
                           json={"message": "hi", "session_id": "s1"}) as r:
            assert r.status_code == 200
            frames = parse_frames(r)
    kinds = [frame_kind(f) for f in frames]
    assert kinds == ["tool", "t", "usage", "done"]
    assert frames[0] == {"tool": "list_dir"}
    assert frames[1] == {"t": "done reply"}
    assert frames[2]["usage"]["turn_tokens"] == 0
    assert frames[2]["usage"]["capped"] is False
    assert frames[-1] == {"done": True}


def test_限额截断_capped(monkeypatch):
    """cap=30、每 chunk 15 token：第 2 个 chunk 触顶即停 → 文本帧只剩 1 个，
    usage 帧 capped=True、turn_tokens=30（已消耗的照常记账）。"""
    monkeypatch.setenv("AGENT_MAX_TOKENS_PER_REQUEST", "30")

    def mk(i):
        return AIMessage(
            content=f"chunk-{i}",
            usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )

    steps = [
        {"call_model": {"messages": [mk(0)]}},
        {"call_model": {"messages": [mk(1)]}},
        {"call_model": {"messages": [mk(2)]}},   # 触顶后 producer 不该走到这里
    ]
    agent = SimpleNamespace(provider="custom", model="fake", graph=FakeGraph(steps))
    app = make_app(monkeypatch, agent, USAGE_OK)
    with TestClient(app) as client:
        with client.stream("POST", "/api/v1/chat/stream",
                           json={"message": "hi", "session_id": "s1"}) as r:
            frames = parse_frames(r)
    kinds = [frame_kind(f) for f in frames]
    assert kinds == ["t", "usage", "done"]
    assert frames[0] == {"t": "chunk-0"}          # 只剩触顶前的那 1 个文本帧
    usage = frames[1]["usage"]
    assert usage["turn_tokens"] == 30             # 15 + 15 = 触顶那一瞬的累计值
    assert usage["capped"] is True


def test_额度耗尽返回429(monkeypatch):
    """当日剩余额度为 0 → 请求还没进流就被 429 挡下，不消耗请求数。"""
    usage_zero = dict(USAGE_OK, remaining_tokens=0)
    agent = SimpleNamespace(provider="custom", model="fake",
                            graph=FakeGraph(steps=[]))
    app = make_app(monkeypatch, agent, usage_zero)
    with TestClient(app) as client:
        resp = client.post("/api/v1/chat/stream",
                           json={"message": "hi", "session_id": "s1"})
    assert resp.status_code == 429
