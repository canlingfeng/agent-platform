from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import ToolNode
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from core.agent_state import AgentState
from core.prompt import build_system_prompt

def build_agent_graph(agent,checkpointer=None):
    tool_node = ToolNode(agent.tools_lc)

    def call_model(state: AgentState) -> dict:
        if not agent.api_key:
            return {"messages": [AIMessage(content="还没配置 API key，请在网页下方【API 设置】里填写并保存。")]}
        # 组装上下文：system prompt + 历史摘要 + 最近的消息。
        # 前缀只用于本次 invoke，不写回 state——否则会被当成对话历史存进 checkpointer，
        # 导致下一轮前缀重复叠加。
        prefix = []
        sys_prompt = build_system_prompt(agent.tools_lc)
        if sys_prompt:
            prefix.append(SystemMessage(content=sys_prompt))
        summary = (state.get("summary") or "").strip()
        if summary:
            prefix.append(SystemMessage(
                content=f"【早期对话摘要】上下文已被压缩，以下是更早内容的摘要：\n{summary}"
            ))
        # 只送最近 max_history 条给 LLM；checkpointer 仍保留全量供历史回看
        recent = list(state["messages"])[-agent.max_history:]
        try:
            ai_msg = agent.llm.invoke(prefix + recent)
        except Exception as e:
            ai_msg = AIMessage(content=f"API 调用失败：{type(e).__name__}: {e}")
        return {"messages": [ai_msg], "iteration": state.get("iteration", 0) + 1}

    def route_after_model(state: AgentState) -> str:
        last = state["messages"][-1]
        if getattr(last, "tool_calls", None) and state.get("iteration", 0) < agent.max_iterations:
            return "tools"
        return END

    def route_after_tools(state: AgentState) -> str:
        last = state["messages"][-1]
        if isinstance(last, ToolMessage) and str(last.content).startswith("NEED_CONFIRM:"):
            return END
        return "call_model"

    builder = StateGraph(AgentState)
    builder.add_node("call_model", call_model)
    builder.add_node("tools", tool_node)
    builder.add_edge(START, "call_model")
    builder.add_conditional_edges("call_model", route_after_model)
    builder.add_conditional_edges("tools", route_after_tools)
    if checkpointer is None:
        checkpointer = MemorySaver()
    return builder.compile(checkpointer=checkpointer)