"""Lenient guardrails middleware to filter only egregious misuse."""
# ------------------------- 第 1 段：导入与全局配置（第 1-50 行）
import asyncio
import logging
import os
import random
from typing import Any, Literal

import langsmith as ls
from langchain.agents.middleware import AgentMiddleware, AgentState, hook_config
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.runtime import Runtime
from langsmith import Client
from typing_extensions import NotRequired, TypedDict

from src.agent.llm import get_chat_model
from src.prompts.guardrails_prompts import (
    fallback_rejection_message as _FALLBACK_REJECTION_MESSAGE,
)
from src.prompts.guardrails_prompts import (
    guardrails_system_prompt as _LOCAL_GUARDRAILS_SYSTEM_PROMPT,
)
from src.prompts.guardrails_prompts import (
    rejection_system_prompt as _REJECTION_SYSTEM_PROMPT,
)

logger = logging.getLogger(__name__)

# Dataset configuration for guardrails evaluation
GUARDRAILS_DATASET_NAME = "Chat-LangChain-Guardrails-Samples"  # 一个LangSmith数据库，用于后期离线评估护栏准不准。
ALLOWED_SAMPLE_RATE = 0.01  # 1% 的"放行" query 采样进数据集，用于离线评估
GUARDRAILS_MAX_RETRIES = 2  # 单个模型最多重试 2 次（即尝试 3 次）
GUARDRAILS_TIMEOUT_SECONDS = 10  # 单次 LLM 调用超时 10 秒
_USE_LOCAL_PROMPTS = os.getenv("USE_LOCAL_PROMPTS", "").lower() in {
    "1",
    "true",
    "yes",
}
_USE_STAGING = (
    os.getenv("LANGSMITH_HOST_PROJECT_NAME") == "immanuel-chat-langchain-test"
    or os.getenv("LANGSMITH_ENV") == "dev"
)
_GUARDRAILS_PROMPT_HUB_NAME = (
    "public-chat-langchain-guardrails-test:staging"
    if _USE_STAGING
    else "public-chat-langchain-guardrails-test:production"
)

# Cache for dataset ID to avoid repeated lookups
_dataset_id_cache: str | None = None

# ------------------------- 第 2 段：数据结构与异常定义（第 53-69 行）
class GuardrailsDecision(TypedDict):  # 喂给 LLM 的"输出格式契约"
    """Structured output for guardrails decision."""

    decision: Literal["ALLOWED", "BLOCKED"]
    explanation: str


class GuardrailsClassificationError(Exception):  # 不用普通 Exception 的原因：要区分"这就是分类失败"和"其他意外 bug"。
    """Raised when guardrails classification fails after retries."""

    pass


class GuardrailsState(AgentState):  # 在默认 AgentState（存 messages 的那个共享内存）基础上，加了一个自定义字段 off_topic_query。这样中间件就能把"这个问题被判定为跑题"这个结论写进 state，后续环节（或前端）可以读它。
    """Extended state schema with off-topic flag."""

    off_topic_query: NotRequired[bool]  # NotRequired 表示这字段不是必填。

# ------------------------- 第 3 段：提示词加载（第 72-97 行）
if _USE_LOCAL_PROMPTS:
    _GUARDRAILS_SYSTEM_PROMPT = _LOCAL_GUARDRAILS_SYSTEM_PROMPT
    guardrails_prompt_commit = None
    guardrails_prompt_source = "local:src/prompts/guardrails_prompts.py"
    logger.info("Using local guardrails prompt because USE_LOCAL_PROMPTS is enabled")
else:
    _langsmith_client = Client()
    try:
        _prompt_template = _langsmith_client.pull_prompt(_GUARDRAILS_PROMPT_HUB_NAME)
        _GUARDRAILS_SYSTEM_PROMPT = _prompt_template.invoke({"messages": []}).messages[
            0
        ].content
        guardrails_prompt_commit = (_prompt_template.metadata or {}).get(
            "lc_hub_commit_hash"
        )
        guardrails_prompt_source = f"hub:{_GUARDRAILS_PROMPT_HUB_NAME}"
        logger.info(
            f"Loaded guardrails prompt from hub: {_GUARDRAILS_PROMPT_HUB_NAME} @ {(guardrails_prompt_commit or '')[:8]}"
        )
    except Exception:
        logger.warning(
            f"Failed to pull guardrails prompt from hub ({_GUARDRAILS_PROMPT_HUB_NAME}), falling back to local file"
        )
        _GUARDRAILS_SYSTEM_PROMPT = _LOCAL_GUARDRAILS_SYSTEM_PROMPT
        guardrails_prompt_commit = None
        guardrails_prompt_source = "local:src/prompts/guardrails_prompts.py"


class GuardrailsMiddleware(AgentMiddleware[GuardrailsState]):
    """Lenient guardrails to filter only egregious misuse."""

    state_schema = GuardrailsState
# ------------------------- 第 4 段：__init__（第 105-133 行）—— 构建"主/备用模型链"
    def __init__(
        self,
        model: str | None = None,
        fallback_model: str | None = None,
        block_off_topic: bool = True,
    ):
        """Initialize guardrails with a primary classifier and fallback model."""
        super().__init__()
        if model is None:
            from src.agent.config import DEFAULT_MODEL, GUARDRAILS_MODEL

            model = GUARDRAILS_MODEL.id
            fallback_model = fallback_model or DEFAULT_MODEL.id
        elif fallback_model is None:
            from src.agent.config import DEFAULT_MODEL

            fallback_model = DEFAULT_MODEL.id
       
        self.llm = get_chat_model(model, temperature=0)  # 主裁判模型。  temperature=0：分类要稳定可复现，不要创造性
        self.classifier_llms = [(model, self.llm)]  # 链：先只放主模型
        if fallback_model != model:
            self.classifier_llms.append(
                (fallback_model, get_chat_model(fallback_model, temperature=0))  # 再追加备用模型
            )  # self.classifier_llms 这个列表，它就是一条"降级链"，形如：[("主模型名", 主模型实例), ("备用模型名", 备用模型实例)]
        self.block_off_topic = block_off_topic
        logger.info(
            "GuardrailsMiddleware initialized with model chain: %s",
            " -> ".join(model_name for model_name, _ in self.classifier_llms),
        )

    async def _add_to_dataset(
        self, query: str, result: str, explanation: str, preview: str
    ) -> None:  # 把一条被分类过的用户 query（连同分类结果）写入 LangSmith 的评估数据集，攒起来供以后离线评估护栏准不准。
        """Add query to dataset asynchronously using AsyncClient."""
        global _dataset_id_cache
        try:
            async with ls.AsyncClient() as client:
                # Get or create dataset (cache the ID)
                if _dataset_id_cache is None:  # 1.if _dataset_id_cache is None：只有全局缓存还没 ID 时才去查——避免每次写样本都重复向 LangSmith 查询 dataset。这是一个"每个进程只做一次"的优化。
                    try:
                        dataset = await client.read_dataset(
                            dataset_name=GUARDRAILS_DATASET_NAME
                        )  # 2.先试着读取已存在的数据集。await 是因为这是一次网络请求（慢 IO），必须让车。
                        _dataset_id_cache = str(dataset.id)  # 3.读到就 _dataset_id_cache = str(dataset.id)——缓存住 ID。
                    except Exception:  # 4.except Exception: 里create_dataset(...)：读不到（数据集还不存在，read_dataset抛异常）就创建它，同样await，然后缓存新 ID。
                        dataset = await client.create_dataset(
                            dataset_name=GUARDRAILS_DATASET_NAME,
                            description="Production samples for guardrails evaluation. Contains all blocked queries and 1% of allowed queries.",
                        )
                        _dataset_id_cache = str(dataset.id)

                await client.create_example(
                    dataset_id=_dataset_id_cache,
                    inputs={"query": query},
                    outputs={
                        "expected_result": result,
                        "explanation": explanation,
                    },
                )  # 5.创建样本，写入数据集。
                logger.debug(f"Added {result} query to dataset: {preview}...")  # 写成功打一条 debug 级日志。用 debug 而非 info，是因为这类"旁路样本写入"量大且非核心，不想刷屏污染正式日志。
        except Exception as e:
            logger.warning(f"Failed to add query to dataset: {e}")

    async def _generate_rejection_message(self, content) -> AIMessage:
        """Generate a friendly rejection message for off-topic queries."""
        prompt = [
            SystemMessage(content=_REJECTION_SYSTEM_PROMPT),
            HumanMessage(
                content=self._build_rejection_content(content)
            ),
        ]

        try:
            response = await asyncio.wait_for(
                self.llm.ainvoke(prompt),
                timeout=GUARDRAILS_TIMEOUT_SECONDS,
            )
            return AIMessage(id=response.id, content=response.content)
        except Exception as e:
            logger.error(f"Error generating rejection message: {e}")
            return AIMessage(content=_FALLBACK_REJECTION_MESSAGE)
# ------------------------- 第 5 段：abefore_agent 主流程（第 187-258 行）—— 核心编排
    @hook_config(can_jump_to=["end"])  # 该中间件装饰器声明：我这个钩子有权在 Agent 流程还没走完时，直接跳到 end（结束），把中间所有步骤（调模型、跑工具）全跳过。
    async def abefore_agent(
        self, state: GuardrailsState, runtime: Runtime
    ) -> dict[str, Any] | None:  # GuardrailsState 是字典，有4个 key：messages、jump_to、structured_response、off_topic_query
        """Check if query is LangChain-related before processing."""
        messages = state.get("messages", [])
        if not messages:
            return None  # 没有消息 → 什么都不做，直接放行

        # Extract the current query for all checks below.
        last_message = messages[-1]  # 取最新一条消息
        last_content = (
            last_message.content
            if hasattr(last_message, "content")
            else str(last_message)
        )
        safe_last_content = self._content_to_safe_text(last_content)  # 多模态转纯文本
        query_preview = safe_last_content[:100]

        # One classifier, every turn. Covers topic relevance + zero-tolerance
        # categories (NSFW, fiction, harmful-use-case, prompt-extraction,
        # social-pressure). The prompt's lenient follow-up rules keep legit
        # mid-conversation follow-ups ("show in Python", "3rd one") ALLOWED,
        # while zero-tolerance bullets override the default ALLOW.
        try:
            guardrails_decision = await self._classify_query(messages)  # 调 LLM 分类
        except GuardrailsClassificationError:
            logger.error("Guardrails check failed after retries; allowing query.")
            return {"off_topic_query": False}  # ★ 分类彻底失败 → fail-open 放行

        decision = guardrails_decision["decision"]
        explanation = guardrails_decision["explanation"]

        # Track in LangSmith metadata
        self._track_decision_metadata(guardrails_decision)  # 把结论写进 LangSmith trace，便于排查

        # 采样进评估数据集：BLOCKED 100% 存，ALLOWED 只存 1%
        if decision == "BLOCKED" or random.random() < ALLOWED_SAMPLE_RATE:  # random.random()： 区间 [0.0, 1.0) 上的连续均匀分布。
            asyncio.create_task(
                self._add_to_dataset(
                    safe_last_content,
                    decision,
                    explanation,
                    query_preview,
                )
            )  # 用 asyncio.create_task（不 await）把它丢到事件循环后台，既不阻塞主流程

        # Handle allowed queries
        if decision == "ALLOWED":
            logger.info("Query validated: %s", explanation)
            return None  # 放行：返回 None，主 Agent 正常继续 ← 和入口守卫一样

        # 以下是 BLOCKED 的处理
        logger.warning(
            "Off-topic query detected: %s... Reason: %s",
            query_preview,
            explanation,
        )

        if not self.block_off_topic:
            logger.info(
                "Off-topic query detected but block_off_topic=False, allowing..."
            )
            return None  # 配置成"只检测不拦截"时，也放行

        # Generate rejection and block
        off_topic_message = await self._generate_rejection_message(last_content)  # 再调一次 LLM 生成婉拒语
        return {
            "messages": [off_topic_message],
            "off_topic_query": True,
            "jump_to": "end",
        }
# ------------------------- 第 6 段：多模态内容处理辅助方法（第 260-347 行）
    def _content_to_safe_text(self, content) -> str:
        """Convert multimodal content to text without leaking encoded media."""
        if isinstance(content, str):
            return content

        if not isinstance(content, list):
            return str(content)

        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
                continue

            if not isinstance(block, dict):
                continue

            block_type = block.get("type")
            if block_type == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif block_type in ("image_url", "input_image"):
                parts.append("[image attached]")
            elif block_type:
                parts.append(f"[{block_type} attached]")

        return " ".join(part for part in parts if part).strip()

    def _build_rejection_content(self, content) -> str | list:
        """Build rejection prompt while preserving images for vision-capable models."""
        instruction = (
            "The user asked the following request. Consider both the text and any "
            "attached images, then generate a brief, friendly response explaining "
            "this is outside your scope."
        )  # “用户提出了以下请求。请综合考量文本及任何随附图片，然后给出一个简短、友好的回复，说明此事超出了您的服务范围。”

        if not isinstance(content, list):
            return f"{instruction}\n\nUser request: {content}"

        blocks: list[Any] = [{"type": "text", "text": instruction}]
        for block in content:
            if isinstance(block, str):
                blocks.append({"type": "text", "text": block})
            elif isinstance(block, dict):
                blocks.append(block)

        blocks.append(
            {
                "type": "text",
                "text": (
                    "Generate a brief, friendly response explaining this is outside "
                    "your scope."  # “写一段简短、友好的回复，说明此事超出了你的职责范围。”
                ),
            }
        )
        return blocks

    def _content_has_media(self, content) -> bool:
        """Return whether content contains non-text multimodal blocks."""
        if not isinstance(content, list):
            return False

        return any(
            isinstance(block, dict) and block.get("type") not in (None, "text")
            for block in content
        )

    def _build_guardrails_content(self, content, context_section: str) -> str | list:
        """Build classifier input while preserving image blocks for vision models."""
        instruction = (
            "Classify this user query for the LangChain documentation assistant. "
            "Consider both the text and any attached images. "
            "Return both the decision and one concise sentence explaining why."
        ) # “为 LangChain 文档助手对该用户查询进行分类。请同时参考文本内容及所附图片。请给出分类结果，并附上一句简明扼要的解释说明。”

        if context_section:
            instruction += context_section

        if not isinstance(content, list):
            return f"{instruction}\n\nUser query: {content}"

        blocks: list[Any] = [{"type": "text", "text": instruction}]
        for block in content:
            if isinstance(block, str):
                blocks.append({"type": "text", "text": block})
            elif isinstance(block, dict):
                blocks.append(block)

        return blocks

    def _extract_message_text(self, msg) -> str | None:
        """Extract plain text content from a message."""
        content = getattr(msg, "content", None)
        if not content:
            return None

        if isinstance(content, str):
            return content.strip() or None

        # Handle list content (extract text blocks only)
        if isinstance(content, list):
            parts = [
                b if isinstance(b, str) else b.get("text", "")
                for b in content
                if isinstance(b, str)
                or (isinstance(b, dict) and b.get("type") == "text")
            ]
            return " ".join(parts).strip() or None

        return None
# 第 7 段：_classify_query（第 370-469 行）—— 降级链的执行现场，最重点
    async def _classify_query(self, messages: list) -> GuardrailsDecision:
        """Classify query as ALLOWED or BLOCKED.

        Raises:
            GuardrailsClassificationError: If classification fails after retries.
        """
        # Extract the current query (last human message)
        current_message = None
        current_query = None
        for msg in reversed(messages):  # 从后往前找最新一条 human 消息作为待分类 query
            if isinstance(msg, HumanMessage):
                current_message = msg
                current_query = self._extract_message_text(msg)
                if current_query or self._content_has_media(getattr(msg, "content", None)):
                    break

        if current_message is None or (
            not current_query
            and not self._content_has_media(getattr(current_message, "content", None))
        ):
            return {"decision": "ALLOWED", "explanation": "No human query was available to classify."}  # 如果没有可分类的人类 query（也没图片），直接返回 `{"decision": "ALLOWED", ...}`——没东西可判就放行，不浪费一次 LLM 调用。

        # 收集最近 3 条历史 human 提问，作为"上下文"，帮模型判断这是不是合法追问
        prior_queries = []
        for msg in reversed(messages[:-1]):  # messages[:-1] 排除当前消息
            if isinstance(msg, HumanMessage):
                text = self._extract_message_text(msg)
                if text:
                    prior_queries.append(text[:200])  # Truncate for brevity
                    if len(prior_queries) == 3:
                        break

        # Build the classification prompt
        context_section = ""
        if prior_queries:
            recent = list(reversed(prior_queries))  # Restore chronological order.
            context_section = (
                "\n\nPrevious questions in this conversation:\n"
                + "\n".join(f"- {q}" for q in recent)
            )

        current_content = getattr(current_message, "content", current_query or "")
        prompt = [
            SystemMessage(content=_GUARDRAILS_SYSTEM_PROMPT),
            HumanMessage(
                content=self._build_guardrails_content(current_content, context_section)
            ),
        ]
        # 双层循环执行降级链
        last_exception: Exception | None = None

        for model_index, (model_name, llm) in enumerate(self.classifier_llms):  # 外层：主模型 → 备用模型
            structured_llm = llm.with_structured_output(GuardrailsDecision)     # 强制按结构化格式输出

            for attempt in range(GUARDRAILS_MAX_RETRIES + 1):                   # 内层：每个模型试 3 次(2+1)
                try:
                    result: GuardrailsDecision = await asyncio.wait_for(
                        structured_llm.ainvoke(
                            prompt, config={"callbacks": [], "tags": ["guardrails"]}  # 给这次 LLM 调用打 tag，方便在 LangSmith 里筛出护栏调用的 trace。
                        ),
                        timeout=GUARDRAILS_TIMEOUT_SECONDS,                     # 带 10 秒超时保护
                    )
                    if model_index > 0:                                        # 用的是备用模型才记这条日志
                        logger.info(
                            "Guardrails classification succeeded with fallback model: %s",
                            model_name,
                        )
                    return result                                              # ★ 任一模型成功 → 立即返回
                except Exception as e:
                    last_exception = e  # last_exception 的记忆：循环里反复覆盖它，最后用它构造异常信息，保证最终抛出的异常带着"最后到底为什么失败"的诊断线索。
                    if attempt < GUARDRAILS_MAX_RETRIES:
                        logger.warning(
                            "Guardrails classification failed with %s attempt %s/%s: %s. Retrying...",
                            model_name,
                            attempt + 1,
                            GUARDRAILS_MAX_RETRIES + 1,
                            e,
                        )                                                       # 还没试满 3 次 → 同模型重试
                        continue

                    if model_index < len(self.classifier_llms) - 1:
                        next_model = self.classifier_llms[model_index + 1][0]
                        logger.error(
                            "Guardrails classification failed with %s after %s attempts: %s. Falling back to %s.",
                            model_name,
                            GUARDRAILS_MAX_RETRIES + 1,
                            e,
                            next_model,
                        )                                                       # 这个模型彻底失败 → 换下一个模型
                    else:
                        logger.error(
                            "Guardrails classification failed with %s after %s attempts: %s",
                            model_name,
                            GUARDRAILS_MAX_RETRIES + 1,
                            e,
                        )                                                       # 最后一个模型也失败
        # 两层循环全部走完都没 return，说明主+备所有模型都失败 → 抛自定义异常
        raise GuardrailsClassificationError(
            f"Guardrails classification failed after retries: {last_exception}"
        )
# ------------------------- 第 8 段：_track_decision_metadata（第 471-481 行）—— 统计信息收集
    def _track_decision_metadata(self, decision: GuardrailsDecision) -> None:
        """Add guardrails decision to LangSmith run metadata."""
        try:  # 把"取当前运行树"这一步放进 try，因为在有些环境下它可能报错或返回 None：比如本地跑测试、trace 没启用（LANGCHAIN_TRACING_V2=false）、不在被追踪的执行上下文里。取不到是正常情况，不是 bug。
            run_tree = ls.get_current_run_tree()  # 取回我当前正在执行的这一次请求的运行树对象
            if run_tree:
                run_tree.metadata["guardrails_result"] = decision["decision"]
                run_tree.metadata["guardrails_explanation"] = decision["explanation"]
        except Exception:
            pass  # 兜底：捕获任何异常后 pass（什么都不做，静默吞掉）。
        # 记录 metadata 是纯粹的"锦上添花"观测行为，绝不能因为它失败（取不到 run tree、metadata 结构异常等）而让主对话流程崩掉。

__all__ = ["GuardrailsMiddleware", "GuardrailsClassificationError"]
