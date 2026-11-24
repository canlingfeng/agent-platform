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
    """按预设步骤产出 chunk，模拟 agent.graph.stream 的 super-step 增量。

    history：模拟 checkpointer 里已有的全量历史消息（#2 摘要落库用例需要——
    修复后的端点在流收尾会 get_state() 量历史长度，再对窗口外消息做压缩）。
    旧用例不传 history，默认空列表，行为不变。
    """

    def __init__(self, steps, history=None):
        self.steps = steps
        self.history = history or []

    def stream(self, inputs, config):
        yield from self.steps

    def get_state(self, config):
        return SimpleNamespace(values={"messages": list(self.history)})


USAGE_OK = {
    "used_tokens": 5, "used_requests": 1,
    "limit_tokens": 100, "limit_requests": 10,
    "remaining_tokens": 95,
}


def make_app(monkeypatch, agent, usage,summaries=None):
    """把 user_store 的 DB 函数全部换成内存假实现，构造最小可测 app。"""
    if summaries is None:
        summaries = []
    monkeypatch.setattr(chat_mod.user_store, "get_session_for_user",
                        lambda sid, uid: {"id": sid, "title": "t", "summary": ""})
    monkeypatch.setattr(chat_mod.user_store, "consume_quota",
                        lambda uid, requests=0, tokens=0: True)
    monkeypatch.setattr(chat_mod.user_store, "record_usage",
                        lambda uid, tokens=0, requests=0: None)
    monkeypatch.setattr(chat_mod.user_store, "touch_session",
                        lambda sid, uid: None)
    monkeypatch.setattr(chat_mod.user_store, "get_usage_today", lambda uid: dict(usage))
    monkeypatch.setattr(chat_mod.user_store, "update_session_summary",
                        lambda sid, s: summaries.append((sid, s)))

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


def _fake_compress(sess, evicted):
    """替身压缩：把"被压缩的条数"写进 summary，供用例断言切片方向正确。
    注意：切片方向写反（取最新 N 条而不是最旧 N 条）时，条数会变成 max_history
    而不是 len - max_history，这个断言能立刻抓住。"""
    sess.summary = "fake-summary:" + str(len(evicted))


def _fake_messages_to_history(msgs):
    """替身转换：Agent._messages_to_history 的最小实现（本用例不关心具体格式）。"""
    return [{"role": "ai", "content": getattr(m, "content", "")} for m in msgs]


def test_超长流摘要落库(monkeypatch):
    """#2 契约：流式会话消息超过 max_history 时，收尾必须像同步 /chat 一样
    压缩窗口外消息并把摘要回写 sessions.summary（update_session_summary）。

    现状缺陷：/chat/stream 直接 graph.stream，摘要永不更新——纯流式用户
    聊超过 20 条后早期上下文只丢不摘，长期记忆失效。
    本用例预置 25 条历史，断言：
      1. update_session_summary 被调用（summaries 非空）；
      2. 落库的会话 id 正确（"s1"）；
      3. 摘要内容非空字符串；
      4. 被压缩条数 = 25 - 20 = 5（锁死"窗口外=最旧的消息"这一切片方向）。
    帧序不受影响（压缩发生在流收尾、不产生新帧），既有 3 例断言不变。
    """
    history = [AIMessage(content=f"h{i}") for i in range(25)]
    steps = [{"call_model": {"messages": [AIMessage(content="done reply")]}}]
    agent = SimpleNamespace(provider="custom", model="fake",
                            graph=FakeGraph(steps, history=history),
                            max_history=20,
                            _compress=_fake_compress,
                            _messages_to_history=_fake_messages_to_history)
    summaries = []
    app = make_app(monkeypatch, agent, USAGE_OK, summaries)
    with TestClient(app) as client:
        with client.stream("POST", "/api/v1/chat/stream",
                           json={"message": "hi", "session_id": "s1"}) as r:
            frames = parse_frames(r)
    # 流本身照常收尾
    assert frame_kind(frames[-1]) == "done"
    # 摘要落库契约
    assert summaries, "update_session_summary 未被调用：#2 未实现或压缩逻辑缺失"
    sid, summary = summaries[0]
    assert sid == "s1"
    assert isinstance(summary, str) and summary.strip()
    # 25 条历史 - 保留 20 条 = 应压缩 5 条（切片方向写反会变成 20）
    assert summary == "fake-summary:5"
