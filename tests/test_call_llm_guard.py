"""P2-9 回归：call_llm 对非标准响应的容错 + _compress 的原文回退。

背景 bug：ModelScope 偶发返回空 body / 非 JSON / 缺字段 / content=null。
旧写法链式取值 payload["choices"][0]["message"]["content"] 会让
JSONDecodeError / KeyError / IndexError 裸抛到 run() 收尾段，
把「主回答已正常生成」的请求打成 500。
修复（core/agent.py call_llm()）：结构异常统一折成 {"action": ...} 错误哨兵；
_compress 检测到哨兵前缀后放弃摘要，把被压缩的原文直接拼回 session.summary。

本用例 monkeypatch 掉 requests.post，全程离线。
"""
import pytest

from core.agent import Agent
from storage.session import Session

SENTINEL_PREFIX = '{"action"'


class FakeResponse:
    """最小化 requests.Response：只暴露 call_llm 用到的三个属性。"""

    def __init__(self, status_code=200, text="", json_data=None, json_error=None):
        self.status_code = status_code
        self.text = text
        self._json = json_data
        self._error = json_error

    def json(self):
        if self._error is not None:
            raise self._error
        return self._json


def make_agent(monkeypatch, response):
    """绕过 Agent.__init__，只挂 call_llm / _compress 消费的属性，
    并把 core.agent 命名空间里的 requests.post 换成恒返回 response 的假函数。"""
    agent = Agent.__new__(Agent)
    agent.api_key = "test-key"
    agent.model = "fake-model"
    agent.api_url = "http://fake.local/v1/chat/completions"
    monkeypatch.setattr("core.agent.requests.post", lambda *a, **k: response)
    return agent


def test_空body返回哨兵(monkeypatch):
    agent = make_agent(monkeypatch, FakeResponse(text="", json_error=ValueError("no json")))
    out = agent.call_llm([{"role": "user", "content": "hi"}])
    assert out.startswith(SENTINEL_PREFIX)


def test_缺choices字段返回哨兵(monkeypatch):
    agent = make_agent(monkeypatch, FakeResponse(json_data={"object": "chat.completion"}))
    out = agent.call_llm([{"role": "user", "content": "hi"}])
    assert out.startswith(SENTINEL_PREFIX)


def test_content_null返回哨兵(monkeypatch):
    agent = make_agent(monkeypatch, FakeResponse(
        json_data={"choices": [{"message": {"content": None}}]}))
    out = agent.call_llm([{"role": "user", "content": "hi"}])
    assert out.startswith(SENTINEL_PREFIX)


def test_正常响应透传(monkeypatch):
    agent = make_agent(monkeypatch, FakeResponse(
        json_data={"choices": [{"message": {"content": "hello"}}]}))
    assert agent.call_llm([{"role": "user", "content": "hi"}]) == "hello"


def test_compress摘要失败回退原文(monkeypatch):
    """call_llm 返回哨兵时，_compress 不应把哨兵当摘要写入，
    而是把被压缩的对话原文拼接回 session.summary（保证信息不丢）。"""
    agent = make_agent(monkeypatch, FakeResponse(text="", json_error=ValueError("boom")))
    sess = Session()
    sess.summary = "old-summary"
    agent._compress(sess, [{"role": "user", "content": "turn1"}])
    assert sess.summary == "old-summary\nuser:turn1"
