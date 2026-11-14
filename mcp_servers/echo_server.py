"""容器内最小 MCP server（stdio 连通性验收桩）。

用途
----
验证 Docker 镜像内这条链路是否完整：
`core.mcp_client` → 拉起 stdio 子进程 → initialize 握手 → list_tools → call_tool。
它只提供一个 echo 工具，不代表业务能力；上线时替换为真实 MCP server 或移除。

硬约束
------
stdio transport（标准输入输出传输）用 **stdout** 承载 JSON-RPC 2.0 帧。
本进程绝不向 stdout 写任何内容（含 print、logging 默认 handler），否则协议帧
被污染，客户端解析失败且报难以定位的错。诊断信息一律写 stderr。

平台无关
--------
不引用任何绝对路径。调用方在 AGENT_MCP_SERVERS 里以【容器内相对路径】指定
本文件，进程的当前工作目录由 Dockerfile 的 WORKDIR /app 决定。
"""
import logging
import sys

from mcp.server.mcpserver import MCPServer

SERVER_NAME = "echo"
log = logging.getLogger(SERVER_NAME)
server = MCPServer(name=SERVER_NAME)


@server.tool()
async def echo(text: str) -> str:
    """原样返回输入文本，用于验证客户端能连上并拿到工具清单。"""
    if not isinstance(text, str):
        raise TypeError("text 必须是字符串")
    return text


def main() -> None:
    # 显式把日志流绑到 stderr：避免 logging 默认 handler 写到 stdout 污染协议帧。
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log.info("MCP stdio server 启动：%s", SERVER_NAME)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
