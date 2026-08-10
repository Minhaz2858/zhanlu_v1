"""Unified Agent Harness — P1/P2 execution spine.

Exports:
- AgentRunOrchestrator: DI-based agent loop (messages → RunResult)
- AgentRunService: run lifecycle (inline / queued, collect, list, resume)
- CheckpointSink: persists AgentRunStep rows on loop events (P2)
- TracingSink: bridges orchestrator events → OTEL spans (P2)
"""

from app.services.harness.orchestrator import (
    AgentRunOrchestrator,
    RunResult,
)
from app.services.harness.checkpoint_sink import CheckpointSink
from app.services.harness.tracing_sink import TracingSink

__all__ = [
    "AgentRunOrchestrator",
    "AgentRunService",
    "CheckpointSink",
    "get_run_service",
    "RunResult",
    "TracingSink",
]


def __dir__():
    return list(__all__)


def __getattr__(name: str):
    if name == "AgentRunService":
        from app.services.harness.run_service import AgentRunService as _cls
        return _cls
    if name == "get_run_service":
        from app.services.harness.run_service import get_run_service as _fn
        return _fn
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
