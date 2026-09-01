"""Token estimation and cost tracking — adapted from OpenHarness.

Tracks token usage per conversation turn and provides cost summaries.
Uses tiktoken when available for precise estimation, falls back to heuristic.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

MODEL_PRICING: dict[str, dict[str, float]] = {
    "deepseek-chat": {"input": 0.0014, "output": 0.0028},
    "deepseek-coder": {"input": 0.0014, "output": 0.0028},
    "deepseek-reasoner": {"input": 0.0055, "output": 0.022},
    "gpt-4o": {"input": 0.0025, "output": 0.01},
    "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
    "gpt-4": {"input": 0.03, "output": 0.06},
    "gpt-3.5-turbo": {"input": 0.0005, "output": 0.0015},
    "claude-3-5-sonnet": {"input": 0.003, "output": 0.015},
    "claude-3-opus": {"input": 0.015, "output": 0.075},
    "default": {"input": 0.002, "output": 0.006},
}


def get_pricing(model: str) -> dict[str, float]:
    model_lower = (model or "").lower()
    for key, pricing in MODEL_PRICING.items():
        if key in model_lower:
            return pricing
    return MODEL_PRICING["default"]


@dataclass
class TokenUsageRecord:
    conversation_id: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    timestamp: str = ""
    agent_name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "timestamp": self.timestamp,
            "agent_name": self.agent_name,
        }


class TokenTracker:
    """Tracks token usage and costs across conversations."""

    def __init__(self):
        self._records: list[TokenUsageRecord] = []

    def record(
        self,
        conversation_id: str,
        model: str,
        usage: dict[str, Any],
        agent_name: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> TokenUsageRecord:
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens)
        pricing = get_pricing(model)
        cost = (prompt_tokens / 1000 * pricing["input"]) + (completion_tokens / 1000 * pricing["output"])
        record = TokenUsageRecord(
            conversation_id=conversation_id,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost_usd=cost,
            timestamp=datetime.now(timezone.utc).isoformat(),
            agent_name=agent_name,
            metadata=metadata or {},
        )
        self._records.append(record)
        return record

    def get_total_usage(self, conversation_id: str | None = None) -> dict[str, Any]:
        records = self._records
        if conversation_id:
            records = [r for r in records if r.conversation_id == conversation_id]
        total_prompt = sum(r.prompt_tokens for r in records)
        total_completion = sum(r.completion_tokens for r in records)
        total_tokens = sum(r.total_tokens for r in records)
        total_cost = sum(r.cost_usd for r in records)
        return {
            "total_calls": len(records),
            "total_prompt_tokens": total_prompt,
            "total_completion_tokens": total_completion,
            "total_tokens": total_tokens,
            "total_cost_usd": round(total_cost, 6),
        }

    def get_records(self, conversation_id: str | None = None, limit: int = 100) -> list[TokenUsageRecord]:
        records = self._records
        if conversation_id:
            records = [r for r in records if r.conversation_id == conversation_id]
        return records[-limit:]

    def clear(self, conversation_id: str | None = None) -> None:
        if conversation_id:
            self._records = [r for r in self._records if r.conversation_id != conversation_id]
        else:
            self._records.clear()


_tracker: TokenTracker | None = None


def get_token_tracker() -> TokenTracker:
    global _tracker
    if _tracker is None:
        _tracker = TokenTracker()
    return _tracker


def record_llm_usage(
    conversation_id: str,
    model: str,
    usage: dict[str, Any],
    agent_name: str = "",
) -> TokenUsageRecord:
    return get_token_tracker().record(conversation_id, model, usage, agent_name)
