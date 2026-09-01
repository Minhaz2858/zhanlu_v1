"""Shared agent-loop core (P2-12 extraction).

Pure refactor of the tool-execution mechanics extracted from
``app/routers/agents.py`` (v2 / resume / v3 ``_stream_llm_with_tools``).
The agents router keeps its per-loop orchestration and delegates the
shared mechanics to this package:

- ``tool_executor`` — parallel tool-batch execution + progress frames
- ``guardrails`` — finish-line forcing, per-call guard partition, tool
  caps, wrap-up nudge, approval pause records
- ``sse_builders`` — SSE event construction aligned with the frontend
  ``Chat.jsx`` consumer contract
- ``fallbacks`` — empty-content / apology / bounce-back fallback text
  and the artifact-aware fallback builders

The agents router re-imports these names into its own module namespace,
so every existing call site and ``from app.routers.agents import ...``
consumer keeps working unchanged (behavior-neutral extraction).
"""

from __future__ import annotations

from .fallbacks import (
    _APOLOGY_PATTERN_RE,
    _BOUNCE_BACK_PATTERN_RE,
    _DASHBOARD_REDIRECT_FALLBACK,
    _EMPTY_CONTENT_FALLBACK,
    _GENERIC_EMPTY_CONTENT_FALLBACK,
    _SYS_COL_PREFIXES,
    _artifact_aware_fallback,
    _collect_artifact_titles,
    _data_summary_fallback,
    _is_degenerate_dataset,
)
from .guardrails import (
    apply_guardrails,
    enforce_tool_caps,
    maybe_force_finish_line,
    maybe_wrap_up_nudge,
    pause_for_approval,
)
from .sse_builders import (
    LIVE_EVENT_TEMPLATES,
    PHASE_HEADLINES,
    _DATA_OFFER_MAX_COLS,
    _DATA_OFFER_MAX_ROWS,
    _LIVE_EVENT_CAP,
    _LIVE_EVENT_FORBIDDEN_RE,
    _LIVE_EVENT_SAFE_FALLBACK,
    _build_live_event,
    _emit_activity_step,
    _emit_live_event,
    _emit_phase,
    _sample_rows_from_payload,
    _sanitize_live_event_params,
    _sanitize_value,
    _sse_live_event,
)
from .tool_executor import (
    emit_tool_progress_while_waiting,
    execute_tool_batch,
    is_long_running_tool,
)

__all__ = [
    # tool_executor
    "execute_tool_batch",
    "emit_tool_progress_while_waiting",
    "is_long_running_tool",
    # guardrails
    "apply_guardrails",
    "enforce_tool_caps",
    "maybe_force_finish_line",
    "maybe_wrap_up_nudge",
    "pause_for_approval",
    # sse_builders
    "PHASE_HEADLINES",
    "LIVE_EVENT_TEMPLATES",
    "_LIVE_EVENT_CAP",
    "_LIVE_EVENT_FORBIDDEN_RE",
    "_LIVE_EVENT_SAFE_FALLBACK",
    "_DATA_OFFER_MAX_ROWS",
    "_DATA_OFFER_MAX_COLS",
    "_emit_activity_step",
    "_emit_phase",
    "_build_live_event",
    "_sse_live_event",
    "_emit_live_event",
    "_sanitize_live_event_params",
    "_sanitize_value",
    "_sample_rows_from_payload",
    # fallbacks
    "_EMPTY_CONTENT_FALLBACK",
    "_GENERIC_EMPTY_CONTENT_FALLBACK",
    "_APOLOGY_PATTERN_RE",
    "_BOUNCE_BACK_PATTERN_RE",
    "_DASHBOARD_REDIRECT_FALLBACK",
    "_collect_artifact_titles",
    "_artifact_aware_fallback",
    "_data_summary_fallback",
    "_SYS_COL_PREFIXES",
    "_is_degenerate_dataset",
]
