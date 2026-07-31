"""Tracing provider ABC, Span context manager, and TraceContext (contextvars)."""
from __future__ import annotations

import contextvars
import time
from abc import ABC, abstractmethod
from typing import Any


class Span:
    """A tracing span — a context manager that records duration + attributes.

    The base implementation is a no-op holder. Provider implementations
    (OtelTracer) override start_span to return a span backed by their SDK.
    """

    def __init__(self, name: str, attributes: dict[str, Any] | None = None):
        self.name = name
        self._attributes: dict[str, Any] = dict(attributes or {})
        self._start: float = 0.0

    def __enter__(self) -> "Span":
        self._start = time.monotonic()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self._attributes["duration_ms"] = (time.monotonic() - self._start) * 1000

    def set_attribute(self, key: str, value: Any) -> None:
        self._attributes[key] = value

    @property
    def attributes(self) -> dict[str, Any]:
        return dict(self._attributes)


class TracingProvider(ABC):
    """Abstract base for tracing providers.

    All methods are best-effort — implementations must swallow internal errors
    so a tracing failure never breaks the agent loop.
    """

    @abstractmethod
    def start_span(self, name: str, attributes: dict[str, Any] | None = None) -> Span: ...

    @abstractmethod
    def record_llm_call(
        self,
        *,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: float,
        error: str | None = None,
    ) -> None: ...

    @abstractmethod
    def record_tool_call(
        self,
        *,
        tool_name: str,
        args_hash: str,
        duration_ms: float,
        success: bool,
        error: str | None = None,
    ) -> None: ...
