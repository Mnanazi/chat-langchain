"""Pytest 全局配置：在代码层禁止测试期间向 LangSmith 上报追踪。

pytest 会在导入任何测试模块之前先加载本文件，因此这里的 env 变量会在
`src/agent/config.py` 初始化追踪客户端之前就生效；而 `dotenv.load_dotenv()`
默认 override=False，不会用 .env 里的值把追踪重新打开。

目的：避免本地无效/无权限的 LANGSMITH_API_KEY 在测试收尾阶段触发
「Failed to multipart ingest runs ... 403 Forbidden」噪声（该报错与测试断言无关）。
"""

import os

# 同时兼容旧版 LANGCHAIN_* 与新版 LANGSMITH_* 两套变量名，确保追踪与测试上报都关闭。
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TEST_TRACKING"] = "false"
os.environ["LANGSMITH_TEST_TRACKING"] = "false"
