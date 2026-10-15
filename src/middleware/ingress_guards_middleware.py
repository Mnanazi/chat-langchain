"""Ingress guards: input caps for Chat LangChain on Managed Deep Agents.

These were previously enforced in ``src/api/auth.py`` (``validate_inputs``).
Under MDA, identity/thread scoping is declared in ``identity.py``; this
middleware only caps oversized user input.

Trace metadata (prompt provenance, ``LANGSMITH_AGENT_VERSION``, ``source_type``)
is applied at agent compile time via ``define_deep_agent(metadata=...)`` in
``agent.py`` — nested ``before_agent`` spans cannot reliably update the
LangSmith root run. Git-linked host fields (``LANGSMITH_LANGGRAPH_GIT_*``) are
not synthesized; archive deploys use ``LANGSMITH_HOST_REVISION_ID`` /
``LANGSMITH_AGENT_VERSION`` instead.
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware, AgentState
from langgraph.runtime import Runtime

#: Upper bound on user-provided text, matching the previous ``MAX_MESSAGE_CHARS``.
MAX_MESSAGE_CHARS = 50_000


class IngressGuardsMiddleware(AgentMiddleware):
    """Cap oversized user input at agent ingress."""

    def before_agent(
        self, state: AgentState, runtime: Runtime
    ) -> dict[str, Any] | None:  # AgentState 字典有三个默认字段：messages、jump_to、structured_response
        """Truncate the latest user message when it exceeds the size cap."""
        messages = state.get("messages", [])  # 从状态里取出对话消息列表
        for message in reversed(messages):  # 从最后一条往前遍历
            if getattr(message, "type", None) == "human":  # 找到最近的一条"用户消息"
                capped = self._truncate_content(message.content)  # 尝试截断它的内容
                if capped is not message.content:  # 如果内容真的被改了
                    # Same id => the messages reducer overwrites in place.  # 因为返回的是同一个 message 对象（同一个 id），LangGraph 的消息合并器 (reducer) 会原地覆盖旧消息，而不是追加一条新的。
                    message.content = capped
                    return {"messages": [message]}  # 把改动交回给 Agent
                break  # 没被改，也不用管，直接结束
        return None  # 没有需要处理的，返回 None

    def _truncate_content(self, content: Any) -> Any:
        """Trim user text to the cap while preserving non-text content blocks."""
        if isinstance(content, str):
            return content[:MAX_MESSAGE_CHARS] if len(content) > MAX_MESSAGE_CHARS else content

        if not isinstance(content, list):
            return content  # 既不是 str 也不是 list，直接放过

        remaining = MAX_MESSAGE_CHARS
        changed = False
        truncated: list[Any] = []
        for block in content:
            if isinstance(block, str):  # 列表项是纯字符串
                text = block[:remaining]  # 用剩余额度截断
                changed = changed or len(text) != len(block)
                truncated.append(text)
                remaining -= len(text)  # 扣掉已用额度
            elif (
                isinstance(block, dict)
                and block.get("type") == "text"  # 列表项是 {type:"text", text:"..."} 这种文字块
                and isinstance(block.get("text"), str)
            ):
                text = block["text"][:remaining]
                changed = changed or len(text) != len(block["text"])
                truncated.append({**block, "text": text})  # 复制整个块，只换掉 text
                remaining -= len(text)
            else:
                truncated.append(block)  # 非文字块（如图片）原样保留，不占额度
        return truncated if changed else content  # 有改动才返回新列表，否则返回原对象


__all__ = ["IngressGuardsMiddleware", "MAX_MESSAGE_CHARS"]
