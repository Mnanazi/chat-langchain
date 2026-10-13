# 测试修改记录（Testing Notes）

本文件记录针对仓库既有失败测试所做的修复，供后续维护参考。

## 1. `check_links` 异步测试适配 LangChain 1.x

**涉及文件**：`tests/unit/test_check_links_async.py`（并新增 dev 依赖 `pytest-asyncio`）

**根因**：`check_links` 是用 `@tool async def` 定义的工具（见 `src/tools/link_check_tools.py`）。在 LangChain 1.x 下，它成为一个 **async-only 的 `StructuredTool`**，于是出现两类不兼容：

- 旧异步入口 `.acall(...)` 已被移除 → `AttributeError: 'StructuredTool' object has no attribute 'acall'`
- 不支持同步调用 `.invoke(...)` → `NotImplementedError: StructuredTool does not support sync invocation`

**修改内容**：

- 4 处 `await check_links.acall({...})` → `await check_links.ainvoke({...})`
- 4 处同步 `check_links.invoke({...})` 的测试 → 加 `@pytest.mark.asyncio`、改为 `async def`、调用 `await check_links.ainvoke({...})`
- 更新过时注释（原注释把 `.acall` 描述为可用 API），并在段落注释说明「sync 语义现经由 `.ainvoke` 驱动」

**约束**：所有断言逻辑保持不变——`1/2 valid`、`HTTP 404`、`Invalid URL format`、soft-404 的 `result.error == "Soft 404: ..."` 与 `result.valid` / `not result.valid` 判定，以及测试函数名均未改动。

**结果**：该文件 **10/10 全部通过**。

## 2. guardrails 提示词补「数据科学库」限制条款

**涉及文件**：`src/prompts/guardrails_prompts.py`（`guardrails_system_prompt`，`USE_LOCAL_PROMPTS=true` 时即测试断言所用的 `_GUARDRAILS_SYSTEM_PROMPT`）

**背景**：`tests/evals/test_guardrails_scope.py` 中有 4 个故意先红的 TDD 测试，要求 guardrails 系统提示词显式限制「无 LangChain 上下文的纯数据科学库问题」。原提示词只有 `## ALWAYS BLOCK` 段，缺少对应条款与 `## ONLY BLOCK` 段。

**修改内容**：在 `## ALWAYS BLOCK - Zero Tolerance`（NSFW 段）之后、`## Critical Rules` 之前，新增一个宽松（lenient）的 `## ONLY BLOCK` 段，包含 5 条「须同时满足才拦截」的判据，其中第 5 条显式列出数据科学库（pandas、numpy、scipy、matplotlib、sklearn、scikit-learn、pyspark、tensorflow、pytorch）并说明「与 LangChain integration 相关时一律 ALLOW」。

**满足的测试点**：

- 提示词出现 `pandas` / `data science` 提及
- `## ONLY BLOCK` 段内含数据科学库名
- 数据科学库名与 `LangChain integration` 同时出现（澄清「仅在有 LangChain 上下文时放行」）
- NSFW 词出现在 `## ONLY BLOCK` 段之前（保证零容忍优先级）
- 未改动 `default is to allow`、核心话题、NSFW 零容忍段，避免回归

**结果**：`test_guardrails_scope.py` **8/8 通过**；后端全量 **40 passed / 0 failed**。

> 运行方式：`uv run pytest`（离线测试无需真实密钥；建议设 `LANGSMITH_TEST_TRACKING=false` 以免上报噪声）。
