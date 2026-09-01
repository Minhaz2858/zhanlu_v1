"""LlmModel — admin-managed LLM provider catalog.

Supports cloud (OpenAI/DeepSeek/...) and private/self-hosted endpoints.
``api_key`` is Fernet-encrypted at rest; decrypted only inside the resolver.
"""

from sqlalchemy import String, Text, Boolean, Integer, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class LlmModel(TimestampedBase):
    __tablename__ = "llm_models"

    name: Mapped[str] = mapped_column(
        String(120), nullable=False, doc="Human-readable display label",
    )
    model_id: Mapped[str] = mapped_column(
        String(120), nullable=False,
        doc="The ``model`` field sent in the OpenAI-compatible payload, e.g. ``deepseek-chat``",
    )
    provider: Mapped[str] = mapped_column(
        String(60), nullable=False,
        doc="Vendor tag: deepseek / openai / ollama / vllm / custom",
    )
    base_url: Mapped[str] = mapped_column(
        String(255), nullable=False,
        doc="OpenAI-compatible endpoint, e.g. ``https://api.deepseek.com/v1``",
    )
    api_key: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        doc="Fernet-encrypted API key. NULL = use the global LLM_API_KEY.",
    )
    is_private: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False,
        doc="True = self-hosted / on-prem. Drives the 🔒 badge.",
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False,
        doc="At most one row should be True — the global fallback.",
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False,
        doc="Resolution skips disabled rows.",
    )
    bypass_hallucination_guardrail: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False,
        doc="When True, skip the hallucination guardrail (ask_data_agent force) for this model",
    )
    context_window: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
        doc="Model context window in tokens (e.g. 16384 for small vLLM, 128000 for DeepSeek). "
            "NULL = fall back to compaction default / MODEL_CONTEXT_WINDOWS heuristic.",
    )
    max_output_tokens: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
        doc="Per-model cap on output tokens. NULL = use user setting clamped by LLM_MAX_TOKENS_HARD_CAP.",
    )
    supports_structured_tool_calls: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False,
        doc="False for vLLM without --enable-auto-tool-choice (tool calls arrive as XML in content). "
            "When False, the server proactively omits tool_choice and parses tool calls from content.",
    )

    __table_args__ = (
        Index("ix_llm_models_org_app", "org_id", "app_id"),
        Index("ix_llm_models_enabled_default", "enabled", "is_default"),
    )
