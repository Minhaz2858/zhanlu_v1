"""LlmModel — admin-managed LLM provider catalog.

Supports cloud (OpenAI/DeepSeek/...) and private/self-hosted endpoints.
``api_key`` is Fernet-encrypted at rest; decrypted only inside the resolver.
"""

from sqlalchemy import String, Text, Boolean, Index
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

    __table_args__ = (
        Index("ix_llm_models_org_app", "org_id", "app_id"),
        Index("ix_llm_models_enabled_default", "enabled", "is_default"),
    )
