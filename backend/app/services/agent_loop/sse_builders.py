"""SSE event builders for the agent loop (P2-12 extraction).

Builders extracted verbatim from ``app/routers/agents.py`` so the
frontend ``Chat.jsx`` SSE contract stays byte-for-byte identical. The
agents router re-imports these names into its own module namespace, so
every existing call site and ``from app.routers.agents import ...``
consumer keeps working unchanged.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _emit_activity_step(step_num: int, description: str, status: str,
                        tool_name: str | None = None,
                        detail: str | None = None,
                        command: str | None = None,
                        output_preview: str | None = None,
                        artifact_id: str | None = None,
                        duration_ms: int | None = None) -> str:
    """Return an SSE ``activity_step`` payload as a formatted SSE string.

    The optional ``command`` / ``output_preview`` fields power the
    Claude-style expandable step detail in the frontend: clicking a step
    reveals WHAT the tool was invoked with (command/code/query) and a
    short preview of what came back (output/error).

    ``duration_ms`` is the wall-clock time in milliseconds for this step,
    from the start of the LLM call or tool execution to the point where
    the step result is emitted. It is rendered in the frontend as a
    small "X.Xs" label next to the step status icon.
    """
    step = {
        "number": step_num,
        "description": description,
        "status": status,
    }
    if tool_name:
        step["tool_name"] = tool_name
    if detail:
        step["detail"] = detail
    if command:
        step["command"] = command
    if output_preview:
        step["output_preview"] = output_preview
    if artifact_id:
        step["artifact_id"] = artifact_id
    if duration_ms is not None:
        step["duration_ms"] = duration_ms
    return f'data: {json.dumps({"type": "activity_step", "step": step})}\n\n'


# ── Claude-style phase headlines ──────────────────────────────────────
# Each phase of a turn gets a short verb + descriptive clause, rendered
# as a spinner-with-headline at the top of the activity section (the
# "✳ Fathoming…" / "✳ Fabricating…" pattern). Verb vocabulary is
# deliberately evocative-but-honest: it names the moment, not magic.
PHASE_HEADLINES: dict[str, tuple[str, str]] = {
    "init":     ("Fathoming",     "Reading your request"),
    "goal":     ("Fathoming",     "Understanding what you need"),
    "context":  ("Orienting",     "Gathering the right context"),
    "plan":     ("Orchestrating", "Laying out the plan"),
    "gate":     ("Checking",      "Running policy checks"),
    "act":      ("Fabricating",   "Building your deliverable"),
    "observe":  ("Watching",      "Recording the results"),
    "verify":   ("Validating",    "Checking the outputs"),
    "finalize": ("Crystallizing", "Wrapping everything up"),
    "quality_eval": ("Reviewing", "Checking answer quality"),
    "done":     ("Done",          "All finished"),
    "fail":     ("Stopped",       "Something went wrong"),
}


def _emit_phase(state: str, detail: str | None = None) -> str:
    """Return an SSE ``phase`` payload — the current Claude-style headline.

    Additive SSE event type; existing consumers filter on ``event.type``
    and ignore unknown types, so this is backward-compatible. The
    frontend renders the latest phase as a headline above the activity
    steps (spinner + verb + title).

    Args:
        state: FSM state name or ReAct milestone (``"goal"`` at turn
            start, ``"act"`` on the first tool batch, ``"finalize"``
            when composing the response).
        detail: Optional override for the descriptive clause.
    """
    verb, title = PHASE_HEADLINES.get(state, ("Working", state.replace("_", " ").title()))
    payload: dict = {"type": "phase", "state": state, "verb": verb, "title": detail or title}
    return f'data: {json.dumps(payload)}\n\n'


# ── Live activity stream: typed event taxonomy ───────────────────────
# The live feed carries *actions, counts and post-gate facts* only. Each
# event is a structured container ``{type, label_key, params, ts}``; the
# ``label_key`` is resolved to human text on the frontend (translations.js)
# and ``params`` carries only structured values (labels, counts, durations,
# indices) — never raw model text, SQL, table names or row data. The content
# invariant is enforced at emission time by ``_sanitize_live_event_params``
# so the persisted stream is safe by construction.
_LIVE_EVENT_CAP = 100

# label_key -> {en, zh} human templates. Kept server-side as the source of
# truth for accepted label keys and to power a server-side summary fallback;
# the frontend renders its own localized templates from translations.js.
LIVE_EVENT_TEMPLATES: dict[str, dict[str, str]] = {
    "phase_enter.init":     {"en": "Preparing the run", "zh": "正在准备执行"},
    "phase_enter.goal":     {"en": "Fathoming your request", "zh": "正在理解你的请求"},
    "phase_enter.context":  {"en": "Gathering the right context", "zh": "正在收集上下文"},
    "phase_enter.plan":     {"en": "Laying out the plan", "zh": "正在规划方案"},
    "phase_enter.gate":     {"en": "Running policy checks", "zh": "正在执行策略检查"},
    "phase_enter.act":      {"en": "Building your deliverable", "zh": "正在构建交付物"},
    "phase_enter.observe":  {"en": "Recording the results", "zh": "正在汇总结果"},
    "phase_enter.verify":   {"en": "Checking the outputs", "zh": "正在校验结果"},
    "phase_enter.finalize": {"en": "Wrapping everything up", "zh": "正在生成最终答复"},
    "phase_enter.quality_eval": {"en": "Reviewing the answer", "zh": "正在评估答复质量"},
    "phase_enter.done":     {"en": "All finished", "zh": "全部完成"},
    "phase_enter.fail":     {"en": "Something went wrong", "zh": "出错了"},
    "plan_preview":         {"en": "Plan ready · {n} steps", "zh": "计划已生成 · 共{n}步"},
    "tool_call_started":    {"en": "Running {tool_label}", "zh": "正在执行{tool_label}"},
    "tool_call_finished":   {"en": "{tool_label} completed · {row_count} rows · {duration}s", "zh": "{tool_label}完成 · {row_count}行 · {duration}秒"},
    "tool_call_failed":     {"en": "{tool_label} failed", "zh": "{tool_label}执行失败"},
    "artifact_progress":    {"en": "Building {artifact_type} · {current} of {total}", "zh": "正在构建{artifact_type} · {current}/{total}"},
    "verify_passed":        {"en": "Verification passed", "zh": "校验通过"},
    "verify_failed":        {"en": "Verification failed — some steps did not complete", "zh": "校验未通过 — 部分步骤未完成"},
    "retry":                {"en": "Correcting {target}", "zh": "正在修正{target}"},
    "finalize_started":     {"en": "Finalizing your answer", "zh": "正在生成最终答复"},
    "finalize_done":        {"en": "Final answer ready", "zh": "最终答复已生成"},
    # Sub-agent delegations (e.g. ask_data_agent, ask_perception).
    "subagent_invoked":     {"en": "Delegating to {agent_label}", "zh": "委托给{agent_label}"},
    "subagent_returned":    {"en": "{agent_label} returned · {duration}s", "zh": "{agent_label}已返回 · {duration}秒"},
    # Inline data peek attached to a finished tool that returned rows.
    "data_offer":           {"en": "Sample of {row_count} rows", "zh": "{row_count}行的样本"},
    # Structured plan summary surfaced at PLAN entry.
    "plan_summary":         {"en": "Plan ready · {n} steps", "zh": "计划已就绪 · 共{n}步"},
}

# Content-invariant denylist: SQL keywords, ERP table/column patterns and
# row-data markers must never appear inside the persisted live stream.
# The ERP column-ID branch (\bF[A-Z][A-Z0-9_]{1,15}\b) is deliberately
# CASE-SENSITIVE (the (?i:...) scoping applies ONLY to the SQL/erp_ branches).
# A blanket IGNORECASE made ANY word starting with 'f' — "fusion360_extrude",
# "Fabricating", "Friday" — match the ERP pattern, so every CAD tool label in
# the live feed was replaced with the "[data]" placeholder. Tool labels are
# server-generated metadata (never raw model text) and must survive.
_LIVE_EVENT_FORBIDDEN_RE = re.compile(
    r"(?i:\b(?:select|insert|update|delete|from|where|join|group\s+by|order\s+by|"
    r"limit|inner\s+join|left\s+join|create\s+table|alter\s+table|drop\s+table)\b"
    r"|\berp_[tv]_[a-z0-9_]+\b)"
    r"|\bF[A-Z][A-Z0-9_]{1,15}\b|\bF\d{2,8}\b",
)
_LIVE_EVENT_SAFE_FALLBACK = "[data]"


def _sanitize_live_event_params(params: dict | None) -> dict:
    """Enforce the content invariant on live-event params.

    Any string value that leaks SQL keywords, ``erp_*`` table patterns or
    ERP column IDs is replaced with a safe placeholder so the persisted
    feed is safe by construction. Nested dicts/lists (e.g. ``sample_rows``
    cells inside a ``data_offer`` event) are walked recursively so the
    invariant applies uniformly to the whole payload tree.
    """
    return _sanitize_value(params or {})


def _sanitize_value(val):
    """Recursively sanitize a value, returning a deep-copied safe tree."""
    if isinstance(val, str):
        if _LIVE_EVENT_FORBIDDEN_RE.search(val):
            return _LIVE_EVENT_SAFE_FALLBACK
        return val
    if isinstance(val, (int, float, bool)) or val is None:
        return val
    if isinstance(val, dict):
        return {k: _sanitize_value(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_sanitize_value(v) for v in val]
    return _LIVE_EVENT_SAFE_FALLBACK


# Cap on how much preview data the backend may embed inside a ``data_offer``
# event. Larger payloads would bloat the SSE stream for marginal UX value.
_DATA_OFFER_MAX_ROWS = 3
_DATA_OFFER_MAX_COLS = 5


def _sample_rows_from_payload(payload: dict | list | None, max_rows: int = _DATA_OFFER_MAX_ROWS, max_cols: int = _DATA_OFFER_MAX_COLS) -> dict | None:
    """Extract a tiny preview of a tool's row payload.

    Returns ``{"columns": [...], "sample_rows": [{...}, ...]}`` when the payload
    is a list of records or a dict with a "rows"/"data"/"results" list, capped
    to ``max_rows`` × ``max_cols``. ``None`` if the payload isn't row-shaped.
    Each cell value is run through the same content-invariant sanitizer used
    for live-event params so previews can't leak raw SQL or row data.
    """
    if payload is None:
        return None
    rows = None
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        rows = payload
    elif isinstance(payload, dict):
        for k in ("rows", "data", "results", "sample", "preview"):
            v = payload.get(k)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                rows = v
                break
    if not rows:
        return None

    # Union of keys (in first-row order) so the preview shows every column the
    # sample covers, capped at ``max_cols``. Empty cells stay absent rather
    # than becoming None entries so the table renders cleanly.
    seen: set[str] = set()
    columns: list[str] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        for k in r.keys():
            if k not in seen and len(columns) < max_cols:
                seen.add(k)
                columns.append(k)

    sample: list[dict] = []
    for r in rows[:max_rows]:
        if not isinstance(r, dict):
            continue
        sample.append({col: r.get(col) for col in columns})

    return {"columns": columns, "sample_rows": sample}


def _build_live_event(
    event_type: str,
    label_key: str,
    params: dict | None = None,
    count: list[int] | None = None,
    sanitize: bool = True,
) -> dict | None:
    """Build a typed live-event container, enforcing the per-turn event cap.

    Returns ``None`` once ``_LIVE_EVENT_CAP`` events have been produced so
    callers can skip emission without breaking the stream. Overflow events
    are logged at warning level.

    ``sanitize=False`` bypasses the content-invariant scrubber for
    server-generated metadata (plan step titles — same class as tool labels,
    which must survive verbatim). Raw model text / SQL / table names must
    never be routed through this path.
    """
    if count is not None:
        if count[0] >= _LIVE_EVENT_CAP:
            logger.warning(
                "live_event cap (%d) reached; dropping %s/%s",
                _LIVE_EVENT_CAP, event_type, label_key,
            )
            return None
        count[0] += 1
    return {
        "type": event_type,
        "label_key": label_key,
        "params": _sanitize_live_event_params(params) if sanitize else dict(params or {}),
        "ts": datetime.now(timezone.utc).isoformat(),
    }


def _sse_live_event(event: dict) -> str:
    """Serialize a live-event container as an SSE ``live_event`` frame."""
    return f'data: {json.dumps({"type": "live_event", "event": event})}\n\n'


def _emit_live_event(
    event_type: str,
    label_key: str,
    params: dict | None = None,
    count: list[int] | None = None,
) -> str | None:
    """Convenience: build + serialize a live event (``None`` when capped)."""
    event = _build_live_event(event_type, label_key, params, count=count)
    if event is None:
        return None
    return _sse_live_event(event)
