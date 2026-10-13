"""Shared configuration for all agents."""

import logging
import os
from dataclasses import dataclass

import dotenv
from langchain.agents.middleware import ModelFallbackMiddleware
from langchain_core.runnables import Runnable, RunnableLambda

from src.agent.llm import CHAT_MODEL_ID, DEFAULT_CHAT_MODEL, get_chat_model
from src.middleware.retry_middleware import (
    RETRYABLE_FINISH_REASONS,
    MalformedResponseError,
    ModelRetryMiddleware,
)
from src.middleware.tool_retry_middleware import ToolRetryMiddleware

dotenv.load_dotenv()

logger = logging.getLogger(__name__)

# =============================================================================
# Model Registry
# =============================================================================


@dataclass
class ModelConfig:
    """Configuration for a supported chat model."""

    id: str  # e.g., "openai:qwen3.6-flash"
    name: str  # Display name, e.g., "Qwen3.6 Flash"
    provider: str  # e.g., "google", "openai", "baseten"
    api_key_env: str  # Environment variable for API key
    description: str | None = None


# Backend-supported models.
# 所有对话模型统一走百炼（DashScope OpenAI 兼容端点），模型名由 src/agent/llm.py
# 从环境变量 OPENAI_MODEL 读取（默认 qwen3.6-flash），思考开关由 DISABLE_THINKING 控制。
# 新增/切换模型只需改环境变量，无需在此硬编码多个 provider。
MODELS: dict[str, ModelConfig] = {
    DEFAULT_CHAT_MODEL: ModelConfig(
        id=CHAT_MODEL_ID,
        name=DEFAULT_CHAT_MODEL,
        provider="openai",
        api_key_env="OPENAI_API_KEY",
        description="Bailian (DashScope) chat model, thinking controlled by DISABLE_THINKING",
    ),
}

# Default models for different use cases
DEFAULT_MODEL = MODELS[DEFAULT_CHAT_MODEL]
GUARDRAILS_MODEL = MODELS[DEFAULT_CHAT_MODEL]

# Fallback chain (in order of preference).
# 目前只有百炼一个可用 provider，fallback 即同一模型（保留重试/降级接线结构）。
FALLBACK_MODELS = [
    MODELS[DEFAULT_CHAT_MODEL],
]

# =============================================================================
# API Key Setup
# =============================================================================

API_KEYS = [
    "OPENAI_API_KEY",
]

for key in API_KEYS:
    if value := os.getenv(key):
        os.environ[key] = value.strip()
        logger.info(f"{key} configured")


# =============================================================================
# Model Initialization
# =============================================================================

# Retry configuration
MAX_RETRIES = int(os.getenv("MODEL_MAX_RETRIES", "2"))

# Primary model. Public callers cannot switch this at runtime.
default_model = get_chat_model(DEFAULT_MODEL.id)
logger.info(f"Default model: {DEFAULT_MODEL.name} ({DEFAULT_MODEL.id})")


def _raise_for_retryable_finish_reason(response: object) -> object:
    metadata = getattr(response, "response_metadata", None) or {}
    finish_reason = metadata.get("finish_reason", "")
    if finish_reason in RETRYABLE_FINISH_REASONS:
        raise MalformedResponseError(f"Model returned {finish_reason}")
    return response


def _init_retrying_model(model: str) -> Runnable:
    return (
        get_chat_model(model)
        | RunnableLambda(_raise_for_retryable_finish_reason)
    ).with_retry(stop_after_attempt=MAX_RETRIES + 1)


def init_retry_fallback_model(model: str) -> Runnable:
    """Initialize a model runnable with the shared retry and fallback policy."""
    primary_model = _init_retrying_model(model)
    fallback_models = [
        _init_retrying_model(fallback.id) for fallback in FALLBACK_MODELS
    ]
    return primary_model.with_fallbacks(fallback_models)


summarization_model = init_retry_fallback_model(DEFAULT_MODEL.id)

# =============================================================================
# Middleware
# =============================================================================

model_retry_middleware = ModelRetryMiddleware(max_retries=MAX_RETRIES)
tool_retry_middleware = ToolRetryMiddleware(max_attempts=3)

model_fallback_middleware = ModelFallbackMiddleware(*[m.id for m in FALLBACK_MODELS])
logger.info(f"Fallback chain: {' -> '.join(m.name for m in FALLBACK_MODELS)}")

# =============================================================================
# Exports
# =============================================================================

__all__ = [
    # Models
    "MODELS",
    "DEFAULT_MODEL",
    "GUARDRAILS_MODEL",
    "FALLBACK_MODELS",
    "ModelConfig",
    # Models
    "default_model",
    "init_retry_fallback_model",
    "summarization_model",
    # Middleware
    "model_retry_middleware",
    "tool_retry_middleware",
    "model_fallback_middleware",
    # Config
    "MAX_RETRIES",
    "logger",
]
