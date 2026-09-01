"""Pluggable context engine -- abstract base class for compaction strategies.

Provides an extensible abstraction so compaction strategy can be swapped
without touching the turn loop. The default implementation wraps the
existing 4-layer progressive compaction pipeline.

Third-party engines (LCM-based, DAG-based, retrieval-based) can subclass
``ContextEngine`` and be selected via config:

    CONTEXT_ENGINE = "default"  # or "custom_engine_name"

Inspired by Hermes' ``agent/context_engine.py``.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


class ContextEngine(ABC):
    """Abstract base class for context compaction strategies.

    Subclasses implement:
    - ``should_compress()``: check if compaction is needed
    - ``compress()``: run the compaction pipeline
    - ``prune_tool_results_only()``: cheap deterministic pre-pass

    The turn loop calls these methods instead of inline compaction logic.
    """

    # Configurable thresholds (subclasses may override)
    threshold_percent: float = 0.75
    protect_first_n: int = 3
    protect_last_n: int = 6

    @abstractmethod
    def should_compress(
        self,
        messages: list[dict[str, Any]],
        current_tokens: int | None = None,
    ) -> bool:
        """Return True if compaction should run."""
        ...

    @abstractmethod
    async def compress(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str = "",
        force: bool = False,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Run the compaction pipeline.

        Returns ``(messages, was_compacted)``.
        """
        ...

    def prune_tool_results_only(
        self,
        messages: list[dict[str, Any]],
        current_tokens: int | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Deterministic, no-LLM tool-result prune.

        Default: delegate to the existing ``pre_api_prune`` module.
        Returns ``(messages, n_pruned)``.
        """
        from app.services.compaction.pre_api_prune import prune_tool_results_only
        return prune_tool_results_only(messages, current_tokens=current_tokens)

    def should_compress_info(
        self,
        messages: list[dict[str, Any]],
        current_tokens: int | None = None,
    ) -> tuple[bool, str | None]:
        """Return ``(should_compress, reason)`` for logging."""
        should = self.should_compress(messages, current_tokens)
        reason = "threshold_exceeded" if should else None
        return should, reason


class DefaultContextEngine(ContextEngine):
    """Default engine wrapping the existing 4-layer progressive compaction.

    Layers (in order):
    1. Microcompact (clear old tool results, no LLM)
    2. Context collapse (head/tail truncation, no LLM)
    3. Session memory (one-line summaries, no LLM)
    4. Full compact (LLM-generated structured summary)
    """

    def __init__(
        self,
        *,
        model: str = "",
        context_window_tokens: int | None = None,
    ):
        self.model = model
        self.context_window_tokens = context_window_tokens
        self._state: Any = None  # AutoCompactState, lazily initialized

    def _get_state(self):
        if self._state is None:
            from app.services.compaction import AutoCompactState
            self._state = AutoCompactState()
        return self._state

    def should_compress(
        self,
        messages: list[dict[str, Any]],
        current_tokens: int | None = None,
    ) -> bool:
        from app.services.compaction import should_autocompact, estimate_messages_tokens, get_context_window

        if current_tokens is None:
            current_tokens = estimate_messages_tokens(messages)

        ctx_window = self.context_window_tokens or get_context_window(self.model)
        return should_autocompact(
            current_tokens,
            model=self.model,
            context_window_tokens=ctx_window,
        )

    async def compress(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str = "",
        force: bool = False,
    ) -> tuple[list[dict[str, Any]], bool]:
        from app.services.compaction import auto_compact_if_needed

        used_model = model or self.model
        ctx_window = self.context_window_tokens

        result_messages, was_compacted = await auto_compact_if_needed(
            messages,
            model=used_model,
            state=self._get_state(),
            context_window_tokens=ctx_window,
            force=force,
            trigger="engine",
        )
        return result_messages, was_compacted


# -- Engine registry --

_ENGINES: dict[str, type[ContextEngine]] = {"default": DefaultContextEngine}


def register_engine(name: str, engine_class: type[ContextEngine]) -> None:
    """Register a custom context engine."""
    _ENGINES[name] = engine_class
    logger.info("Registered context engine: %s", name)


def get_context_engine(
    name: str = "default",
    *,
    model: str = "",
    context_window_tokens: int | None = None,
) -> ContextEngine:
    """Get a context engine instance by name.

    Args:
        name: Engine name (default = "default").
        model: Model name for context window detection.
        context_window_tokens: Optional override for context window size.

    Returns:
        A ContextEngine instance.
    """
    engine_class = _ENGINES.get(name, DefaultContextEngine)
    return engine_class(model=model, context_window_tokens=context_window_tokens)


__all__ = [
    "ContextEngine",
    "DefaultContextEngine",
    "register_engine",
    "get_context_engine",
]
