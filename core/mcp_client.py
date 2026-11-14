from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import os
import json
import logging
import asyncio
import threading
import concurrent.futures
import contextlib
import re
log = logging.getLogger(__name__)

def _connect_timeout() -> float:
    """连接批次硬上限（秒）：配置外置，非法值回退 5.0，下限 0.1 防误配。"""
    try:
        return max(0.1, float(os.environ.get("AGENT_MCP_CONNECT_TIMEOUT", "5.0")))
    except ValueError:
        return 5.0

_runtime = None   
_lock = threading.Lock()  

class McpError(Exception):
    """MCP 客户端统一异常，上层捕获后转成给 LLM 看的文本。"""

def _load_servers():
    """读环境变量 AGENT_MCP_SERVERS（JSON 字符串），解析成 server 配置列表。
    未配置 / 解析失败 → 返回 []（fail-open，绝不抛异常拖垮启动）。"""
    raw = os.environ.get("AGENT_MCP_SERVERS", "")
    if not raw:                      # 变量不存在 或 空串 → 没配 MCP
        return []
    try:
        servers = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning(f"解析环境变量 AGENT_MCP_SERVERS 失败：{e}")
        return []
    if not isinstance(servers, list):
        log.warning(f"环境变量 AGENT_MCP_SERVERS 不是列表：{servers}")
        return []
    return [
        s for s in servers
        if isinstance(s, dict) and s.get("enabled", True) and s.get("command")
    ]

class McpRuntime:
        """常驻事件循环线程 + MCP 会话持有者（进程内单例）。"""
        def __init__(self):
            self._sessions = {} 
            self._stacks = {}
            self._tools = {}
            self._closed = False
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                    target=self._run_loop,   # 不加括号
                    name="mcp-loop",
                    daemon=True,
                )
            self._thread.start()

        def _run_loop(self):
            asyncio.set_event_loop(self._loop)
            self._loop.run_forever()

        def submit(self,coro,timeout:float=30.0):
            """同步侧入口：把协程投到常驻 loop 执行，阻塞等结果。
            - 超时 → cancel 协程 + 抛 McpError
            - 其他异常 → 包成 McpError（保留原始类型名与消息）
            """
            fut=asyncio.run_coroutine_threadsafe(coro, self._loop)
            try: return fut.result(timeout) 
            except concurrent.futures.TimeoutError:
                cancelled = fut.cancel()
                if not cancelled:
                    log.warning("MCP 协程超时后取消失败，仍在后台占用资源")
                raise McpError(f"MCP 调用超时（{timeout}s）")
            except Exception as e:
                raise McpError(f"MCP 调用失败：{type(e).__name__}: {e}") from e


        async def _connect_one(self, spec: dict) -> None:
            """连接单个 stdio MCP server：用 AsyncExitStack 托管上下文使其存活，
            握手后拉取工具清单写入 self._tools / self._sessions / self._stacks。
            连接失败由调用方 connect_all() 捕获并 fail-open。"""
            name=spec.get("name") or spec.get("command")
            name=re.sub(r"\W", "_", name)
            stack=contextlib.AsyncExitStack()
            try:
                params=StdioServerParameters(
                    command=spec["command"],
                    args=spec.get("args", []),
                    env=spec.get("env", {}),
                    cwd=spec.get("cwd", None),
                )
                read,write=await stack.enter_async_context(stdio_client(params))
                session=await stack.enter_async_context(ClientSession(read,write))
                await session.initialize()
                tools = (await session.list_tools()).tools
                self._sessions[name]=session
                self._stacks[name]=stack
                for t in tools:
                    key = f"{name}__{t.name}"
                    schema = t.input_schema or {"type":"object","properties":{}}
                    self._tools[key]=(session,t,schema)
            except (Exception, asyncio.CancelledError):
                await asyncio.shield(stack.aclose())
                raise
        def connect_all(self) -> list[dict]:
            """并行连接所有已配置 server，返回三态报告（ok / error / timeout）。

            一次性投递后统一 wait：总耗时由 _connect_timeout() 决定，
            不随 server 数量累加；单个 server 失败或超时不影响其它 server。
            """
            specs = _load_servers()
            if not specs:
                return []
            timeout = _connect_timeout()
            pending = {}
            for spec in specs:
                fut = asyncio.run_coroutine_threadsafe(
                    self._connect_one(spec), self._loop)
                pending[fut] = spec
            done, not_done = concurrent.futures.wait(
                list(pending), timeout=timeout)
            report = []
            for fut in done:
                spec = pending[fut]
                name = spec.get("name") or spec.get("command")
                try:
                    fut.result()
                    report.append({"name": name, "status": "ok", "detail": ""})
                except (Exception, asyncio.CancelledError) as e:
                    report.append({"name": name, "status": "error",
                                   "detail": f"{type(e).__name__}: {e}"})
            for fut in not_done:
                spec = pending[fut]
                fut.cancel()
                report.append({"name": spec.get("name") or spec.get("command"),
                               "status": "timeout",
                               "detail": f"超过 {timeout}s"})
            return report
       
        def list_tools(self)->list[str]:
            out = []
            for key, (session, tool, schema) in self._tools.items():
                out.append({"name": key, "description": tool.description, "schema": schema})
            return out

        def call(self,key:str,arguments:dict,timeout:float=30.0)->str:
            """同步调用一个已注册 MCP 工具，返回纯文本结果。
            找不到工具 / 工具报错 / 超时 → 抛 McpError（不含内部异常细节）。
            """
            entry = self._tools.get(key)
            if entry is None:
                raise McpError(f"找不到工具 {key}")
            try:
                result=self.submit(entry[0].call_tool(entry[1].name, arguments), timeout=timeout)
            except Exception as e:
                raise McpError(f"调用工具 {key} 失败：{type(e).__name__}: {e}") from e
            if result.is_error: 
                raise McpError(f"工具 {key} 返回错误：{_flatten(result.content)}")
            return _flatten(result.content)

        def shutdown(self, timeout: float = 5.0) -> None:
            """关闭运行时：先释放 stdio 子进程，再停常驻 loop，最后回收线程。
            幂等；任一环节失败只记日志，不抛异常拖垮 lifespan 关闭期。"""
            if self._closed:
                return
            self._closed = True

            async def _close_all():
                for name, stack in list(self._stacks.items()):
                    try:
                        await stack.aclose()
                    except Exception as e:
                        log.warning("关闭 MCP server %s 失败：%s: %s",
                                    name, type(e).__name__, e)
                self._stacks.clear()
                self._sessions.clear()
                self._tools.clear()

            try:
                fut = asyncio.run_coroutine_threadsafe(_close_all(), self._loop)
                fut.result(timeout)
            except Exception as e:
                log.warning("MCP 会话关闭失败：%s: %s", type(e).__name__, e)
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
                self._thread.join(timeout)
            except Exception as e:
                log.warning("MCP 事件循环停止失败：%s: %s", type(e).__name__, e)

def get_runtime():
    global _runtime
    with _lock:
        if _runtime is None:
            _runtime = McpRuntime()
            report = _runtime.connect_all()
            _runtime.connect_report = report
            ok = [r["name"] for r in report if r["status"] == "ok"]
            bad = [f'{r["name"]}({r["status"]})'
                   for r in report if r["status"] != "ok"]
            log.info("MCP 连接汇总：成功 %d 个 [%s]；异常 %d 个 [%s]",
                     len(ok), ", ".join(ok) or "无",
                     len(bad), ", ".join(bad) or "无")  
    return _runtime

def shutdown_runtime(timeout: float = 5.0) -> bool:
    """关闭进程内 MCP 单例；返回是否确实执行了关闭。
    锁内只摘引用，锁外做耗时关闭，避免阻塞并发的 get_runtime()。"""
    global _runtime
    with _lock:
        rt = _runtime
        _runtime = None
    if rt is None:
        return False
    rt.shutdown(timeout)
    return True

def _flatten(content) -> str:
    parts = []
    for block in content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts)

