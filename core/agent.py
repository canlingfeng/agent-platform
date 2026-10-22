import os
os.environ["LANGGRAPH_STRICT_MSGPACK"] = "true"
import json
import requests
import httpx
from core.tools import TOOLS, langchain_tools
from langchain_openai import ChatOpenAI
from core.agent_graph import build_agent_graph
from langgraph.checkpoint.memory import MemorySaver 
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
from core.providers import PRESETS, resolve


CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")

class Agent:
    """智能代理类，能够根据用户输入决定调用工具或直接回答。"""

    def __init__(self,tools=None,checkpointer=None):
        """初始化 Agent，注册可用工具。"""
        # 工具注册表
        self.tools = {t.name: t for t in (tools or TOOLS)}
        self.max_iterations = 5
        # 只把最近 N 条消息送给 LLM（省 token + 防上下文无限增长）；
        # 更早的消息由 _compress 摘要成 session.summary，在下一轮重新进入上下文。
        # 注意：checkpointer 里仍保留全量历史，不影响 GET /sessions/{id}/messages 回看。
        self.max_history = 20
        self.tools_lc = langchain_tools

        # ---- LLM 配置：默认兜底 → config.json 恢复 → _build_llm 构造 ----
        self.provider = "modelscope"
        self.base_url = PRESETS["modelscope"]["base_url"]
        self.model = PRESETS["modelscope"]["model"]
        self.api_url = self.base_url + "/chat/completions"  # call_llm 裸 requests 路径用
        self.api_key = os.environ.get("MODELSCOPE_API_KEY")

        self._load_config()   # 恢复上次保存的 provider / base_url / model / api_key
        self._build_llm()     # 按最终配置构造 ChatOpenAI 并 bind_tools

        self.checkpointer = checkpointer if checkpointer is not None else MemorySaver()
        self.graph = build_agent_graph(self, self.checkpointer)

    def _load_config(self):
        """从 config.json 恢复 LLM 配置。

        优先级：环境变量（生产注入）> config.json（网页保存）——但 **provider="custom"
        例外**：自定义端点没有对应的环境变量，密钥只能存在 config.json，故此时以
        配置文件里的 api_key 为准。缺字段用默认值兜底，旧版只有 api_key 的
        config.json 也能正常加载，不会 KeyError。"""
        try:
            with open(CONFIG_PATH, "r") as f:
                cfg = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return
        saved_provider = cfg.get("provider")
        if saved_provider in PRESETS:      # 白名单校验，脏 provider 名直接忽略
            self.provider = saved_provider
        # provider 先确定，再决定密钥来源（custom 依赖配置文件里的密钥）
        if self.provider == "custom" and cfg.get("api_key"):
            self.api_key = cfg["api_key"]
        elif not self.api_key:
            self.api_key = cfg.get("api_key")
        resolved = resolve(self.provider, cfg.get("base_url", ""), cfg.get("model", ""))
        self.base_url = resolved["base_url"] or self.base_url
        self.model = resolved["model"] or self.model
        self.api_url = self.base_url + "/chat/completions"

    def _build_llm(self):
        """按当前 base_url / model / api_key 构造 ChatOpenAI。
        ⚠️ ChatOpenAI 实例化时就把 base_url、model 焊死在 HTTP 客户端里，
        事后再改属性（agent.llm_plain.base_url = xxx）是假动作，不生效，
        只能整个重建——所以热切换必须走 configure() 重建，不能改属性。"""
        self.llm_plain = ChatOpenAI(
            model=self.model,
            api_key=self.api_key or "dummy",
            base_url=self.base_url,
            temperature=0.2,
            http_client=httpx.Client(),
            http_async_client=httpx.AsyncClient(),
        )
        self.llm = self.llm_plain.bind_tools(self.tools_lc)

    def configure(self, provider, base_url="", model="", api_key=None):
        """热切换供应商 / 模型（webui 保存 API 设置时调用）。
        graph 不用重建：agent_graph.call_model 是运行时才取 agent.llm，
        闭包捕获的是 agent 对象，重新绑定 self.llm 下一轮 invoke 立即生效。"""
        resolved = resolve(provider, base_url, model)
        self.provider = provider
        if resolved["base_url"]:
            self.base_url = resolved["base_url"]
        if resolved["model"]:
            self.model = resolved["model"]
        if api_key:
            self.api_key = api_key
        self.api_url = self.base_url + "/chat/completions"
        self._build_llm()
        
    def call_llm(self, messages: list) -> str:
        """调用大语言模型 API。

        Args:
            messages: run() 拼好的完整消息列表（system + history + 工具结果）

        Returns:    
            模型的回复内容
        """
        if not self.api_key:
            return '{"action": "answer", "content": "还没配置 API key，请在网页下方【API 设置】里填写并保存。"}'
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        data = {
            "model": self.model,
            "messages": messages
        }

        try:
            response = requests.post(
                self.api_url,
                headers=headers,
                json=data,
                timeout=30
            )
        except requests.exceptions.Timeout:
            return '{"action": "answer", "content": "API 响应超时，请稍后重试。"}'
        except requests.exceptions.RequestException as e:
            return f'{{"action": "answer", "content": "网络错误：{type(e).__name__}"}}'
        if response.status_code != 200:
            print(f"API错误码: {response.status_code}")
            print(f"错误详情: {response.text}")
            return '{"action": "answer", "content": "API调用失败"}'
        content = response.json()["choices"][0]["message"]["content"]
        
        print(f"【LLM原始返回】：{content}")
        return content

    def _compress(self,session,evicted:list):
            """压缩消息列表，保留最新的几条消息以节省上下文空间。
    
            Args:
                evicted: 被剔除的消息列表
    
            Returns:
                压缩后的消息列表
            """
            if not evicted:
                return
            lines=[f"{msg['role']}:{msg['content']}"for msg in evicted]
            new_text="\n".join(lines)
            combined=(session.summary+"\n"+new_text).strip()
            summary_message=[
                {"role":"system","content":"你是对话摘要助手，只输出摘要正文（保留人名、偏好、任务、结论等关键事实），不要寒暄、不要解释、不要输出 JSON。"},
                {"role":"user","content":f"请把下面的对话内容总结成一段简洁的摘要：\n\n{combined}"}
            ]
            result=self.call_llm(summary_message)
            if result.startswith('{"action"'):
                session.summary = (session.summary + "\n" + new_text).strip()
            else:
                session.summary = result



    @staticmethod
    def _content_to_text(content):
        """把多模态 content（list of parts）压平成纯文本。

        为什么需要：带图片的消息 content 是 list，而 MessageResponse.content 的类型是
        str；若直接返回 list，前端加载历史会触发 422 校验失败。压平时保留文字、
        图片用 [图片] 占位，历史回看与摘要都能正常工作。
        纯文本消息原样返回，不改变原有行为。
        """
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    texts.append(part.get("text", ""))
                elif isinstance(part, dict) and part.get("type") == "image_url":
                    texts.append("[图片]")
                elif isinstance(part, str):
                    texts.append(part)
            return " ".join(x for x in texts if x).strip()
        return str(content)

    def _messages_to_history(self, messages):
        out = []
        for m in messages:
            t = getattr(m, "type", None)
            if t == "human":
                out.append({"role": "user", "content": self._content_to_text(m.content)})
            elif t == "ai":
                if getattr(m, "tool_calls", None):  
                    continue
                out.append({"role": "assistant", "content": self._content_to_text(m.content)})
            elif t == "tool":
                out.append({"role": "tool", "content": self._content_to_text(m.content)})
        return out

    def run(self,user_input:str,session)->str:  
        config={"configurable":{"thread_id":session.id}}
        result= self.graph.invoke({
            "messages": [HumanMessage(content=user_input)],
            "iteration": 0,
            "summary": session.summary,
            "user_id": None,
        }, config)
        # 上下文压缩：全量历史超过 max_history 时，把窗口外的更早消息交给 _compress 摘要。
        # 摘要经 state["summary"] 在下一轮重新进入 LLM 上下文（见 core/agent_graph.py 的 call_model）。
        # checkpointer 仍保留全量，不影响历史回看。
        all_msgs = result.get("messages", [])
        if len(all_msgs) > self.max_history:
            evicted = all_msgs[:len(all_msgs) - self.max_history]
            self._compress(session, self._messages_to_history(evicted))
        last = result["messages"][-1]
        if isinstance(last, ToolMessage) and str(last.content).startswith("NEED_CONFIRM:"):
            return last.content
        content = last.content if isinstance(last, AIMessage) else "抱歉，我遇到了问题。"
        total_tokens = 0
        for m in result["messages"]:
            usage = getattr(m, "usage_metadata", None)
            if usage:
                total_tokens += int(
                    usage.get("total_tokens", 0)
                    or (usage.get("input_tokens", 0) + usage.get("output_tokens", 0))
                )
        session.token_count = total_tokens
        session.history = self._messages_to_history(result["messages"])
        return content
    def load_history(self, session_id: str) -> list:
        """从 checkpointer 恢复某会话的历史消息（公开接口：多租户按 thread_id 隔离读取）"""
        config = {"configurable": {"thread_id": session_id}}
        state = self.graph.get_state(config)
        msgs = state.values.get("messages", []) if state else []
        return self._messages_to_history(msgs)