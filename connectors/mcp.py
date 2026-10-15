"""Managed MCP connector declarations for Chat LangChain."""
# 这个文件是 Managed Deep Agents (MDA) 运行时的 MCP 连接器声明文件。
# 它不直接被业务代码 import，而是由编译器自动发现并注入到 MDA runtime，用来声明「应该把哪些 MCP 服务器上的工具加载进 Agent」。
from managed_deepagents.connectors import define_mcp_servers

connector = define_mcp_servers(
    prefix_tool_name_with_server_name=False,  # 表示工具名不会自动加上服务器名前缀
    mcp_servers={
        "langchain-docs": {
            "transport": "http",
            "url": "https://docs.langchain.com/mcp",
        },
    },  # 仅声明一个 langchain-docs 服务器，传输类型为 http，URL 指向 LangChain 官方文档 MCP 端点
)
