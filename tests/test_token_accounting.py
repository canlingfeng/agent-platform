"""P1-5 回归：token 记账只统计本轮新增消息（防重复累加）。

背景 bug：MySQL checkpointer 反序列化后，历史 AIMessage 的 usage_metadata 仍在。
旧写法对整条 result["messages"] 求和 → 每轮都把历史轮的 token 再算一遍，
记账累计二次方增长（15 → 30 → 45 → ...）。
修复（core/agent.py run()）：invoke 前用 graph.get_state() 记录消息条数 n_before，
只对切片 result["messages"][n_before:] 求和。

本用例用 FakeGraph 模拟「checkpointer 保留全量历史 + 每轮新增一条带 usage 的
AIMessage」，连续跑三轮，断言每轮记账都是 15，不随轮次累加。
全程不连网、不连库（离线回归）。
"""
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage

from core.agent import Agent
from storage.session import Session


class FakeGraph:
    """模拟 LangGraph 编译产物：checkpointer 保留全量历史，每轮追加 2 条消息。

    - get_state：返回当前全量消息（对应 PooledMySQLSaver 读 checkpoint）。
    - invoke：追加 HumanMessage（无 usage）+ AIMessage（usage 总量 15），返回全量。
    """

    def __init__(self):
        self.messages = []

    def get_state(self, config):
        return SimpleNamespace(values={"messages": list(self.messages)})

    def invoke(self, inputs, config):
        self.messages.append(HumanMessage(content=inputs["messages"][0].content))
        self.messages.append(AIMessage(
            content="ok",
            usage_metadata={
                "input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
            },
        ))
        return {"messages": list(self.messages)}


def make_agent():
    """绕过 Agent.__init__（会建 ChatOpenAI / 读模型配置 / 注册 MCP），
    只挂 run() 真正消费的最小属性集：graph、max_history。"""
    agent = Agent.__new__(Agent)
    agent.graph = FakeGraph()
    agent.max_history = 20
    return agent


def test_token_只记本轮():
    agent = make_agent()
    counts = []
    for i in range(3):
        sess = Session()
        agent.run(f"turn-{i}", sess)
        counts.append(sess.token_count)
    # 每轮新增 1 条 usage=15 的 AIMessage → 每轮记账都是 15；
    # 若退回旧写法（对全量求和），这里会得到 [15, 30, 45]
    assert counts == [15, 15, 15]
