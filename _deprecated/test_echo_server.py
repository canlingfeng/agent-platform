from mcp.server.mcpserver import MCPServer
server = MCPServer(name="echo")
@server.tool()
async def echo(text: str) -> str:
    """原样返回输入文本，用于验证客户端能连上并拿到工具清单。"""
    return text

if __name__ == "__main__":
    server.run(transport="stdio")