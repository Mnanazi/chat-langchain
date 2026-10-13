"""Unified chat-model factory: the single place every LLM call is configured.

本模块集中管理项目内所有对话大模型的构造，避免各调用点各自 `init_chat_model`
导致 provider / base_url / 模型名 / 供应商专属参数（如百炼的思考开关）配置漂移。

统一从环境变量读取：
  - OPENAI_API_KEY / OPENAI_BASE_URL：百炼（DashScope）OpenAI 兼容端点密钥与地址
  - OPENAI_MODEL：对话模型名（默认 qwen3.6-flash）
  - DISABLE_THINKING：是否关闭思考模式（默认 true，减少 token、加快响应）
"""

import os

import dotenv
from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel

dotenv.load_dotenv()

# 百炼对话模型名（不带 provider 前缀），未配置时回退到 qwen3.6-flash。
DEFAULT_CHAT_MODEL = os.getenv("OPENAI_MODEL", "qwen3.6-flash").strip() or "qwen3.6-flash"

# 供注册表使用的完整模型 ID（provider 前缀让 init_chat_model 走 langchain-openai，
# 从而自动读取 OPENAI_BASE_URL 指向的百炼兼容端点）。
CHAT_MODEL_ID = f"openai:{DEFAULT_CHAT_MODEL}"


def _is_truthy(value: str) -> bool:
    """把常见的“真”字符串（1/true/yes/on，忽略大小写）判定为 True。"""
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


# 是否关闭思考模式。默认关闭；若端点不支持该参数并报错，将 DISABLE_THINKING 设为
# false 即可回退为模型默认行为。
DISABLE_THINKING = _is_truthy(os.getenv("DISABLE_THINKING", "true"))


def get_chat_model(model: str | None = None, **kwargs) -> BaseChatModel:
    """构造对话模型，统一注入百炼端点与思考开关。

    Args:
        model: 完整模型 ID（如 ``openai:qwen3.6-flash``）。为空时使用 CHAT_MODEL_ID。
        **kwargs: 透传给 ``init_chat_model`` 的其余参数（如 ``temperature=0``）。

    对走 openai 兼容端点的模型，当 DISABLE_THINKING 为真时下发 ``enable_thinking=False``
    以关闭思考；其它 provider 前缀保持原样不注入该参数。
    """
    model_id = model or CHAT_MODEL_ID

    # 对未带 provider 前缀的裸模型名，默认按 openai（百炼兼容）处理。
    if ":" not in model_id:
        model_id = f"openai:{model_id}"

    if DISABLE_THINKING and model_id.startswith("openai:"):
        model_kwargs = dict(kwargs.pop("model_kwargs", None) or {})
        # 允许调用方显式覆盖，未设置时才注入关思考
        model_kwargs.setdefault("enable_thinking", False)
        kwargs["model_kwargs"] = model_kwargs

    return init_chat_model(model=model_id, **kwargs)


__all__ = [
    "DEFAULT_CHAT_MODEL",
    "CHAT_MODEL_ID",
    "DISABLE_THINKING",
    "get_chat_model",
]
