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
import threading
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
# Per-turn live-event cap. P2 Phase 4: raised from 100 → 300 because a single
# multi-agent turn (General Assistant → several parallel specialists → each with
# multiple tool calls + sub-delegations) can legitimately emit >100 events. The
# previous cap silently dropped delegation/tool events, leaving the AgentExecutionTree
# and LiveActivityStream with an incomplete picture. The cap remains a safety valve
# against runaway loops, not a normal-case limit.
_LIVE_EVENT_CAP = 300

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
    # Phase 2.8: recovery observability — only emitted for operations that
    # ACTUALLY ran (classification/decision computed by the backend).
    "failure_classified":   {"en": "{tool_label} failed · {category} — {recovery}", "zh": "{tool_label}执行失败 · {category} — {recovery}"},
    "execution_decision":   {"en": "Next: {decision} — {action}", "zh": "下一步：{decision} — {action}"},
    "finalize_started":     {"en": "Finalizing your answer", "zh": "正在生成最终答复"},
    "finalize_done":        {"en": "Final answer ready", "zh": "最终答复已生成"},
    # Sub-agent delegations (e.g. ask_data_agent, ask_perception).
    "subagent_invoked":     {"en": "Delegating to {agent_label}", "zh": "委托给{agent_label}"},
    "subagent_returned":    {"en": "{agent_label} returned · {duration}s", "zh": "{agent_label}已返回 · {duration}秒"},
    # Inline data peek attached to a finished tool that returned rows.
    "data_offer":           {"en": "Sample of {row_count} rows", "zh": "{row_count}行的样本"},
    # Structured plan summary surfaced at PLAN entry.
    "plan_summary":         {"en": "Plan ready · {n} steps", "zh": "计划已就绪 · 共{n}步"},
    # 2026-11-02 (dynamic orchestration timeline): retries, replanning and
    # verification phase events — previously whitelisted-but-never-emitted.
    "agent_retrying":       {"en": "Retrying {tool_label}", "zh": "正在重试{tool_label}"},
    "replanning_started":   {"en": "Adjusting the plan — {reason}", "zh": "正在调整计划 — {reason}"},
    "plan_updated":         {"en": "Plan updated — {title} ({status})", "zh": "计划已更新 — {title}（{status}）"},
    "verification_started": {"en": "Checking figures and sources", "zh": "正在核对数据与来源"},
}

# Content-invariant denylist: SQL keywords, ERP table/column patterns and
# row-data markers must never appear inside the persisted live stream.
# The ERP column-ID branch (\bF[A-Z][A-Z0-9_]{1,15}\b) is deliberately
# CASE-SENSITIVE (the (?i:...) scoping applies ONLY to the SQL/erp_/view
# branches). A blanket IGNORECASE made ANY word starting with 'f' —
# "fusion360_extrude", "Fabricating", "Friday" — match the ERP pattern, so
# every CAD tool label in the live feed was replaced with the "[data]"
# placeholder. Tool labels are server-generated metadata (never raw model
# text) and must survive.
#
# Security follow-up (P2 Phase 4): the case-insensitive group also matches
# BARE database view names (``v_sale_order``, ``v_inventory``,
# ``V_PURCHASE_ORDER``). Schema-qualified forms (``sales.v_sale_order``) are
# covered automatically — the ``.`` is a non-word char. The view branch uses
# ASCII-only LOOKAROUNDS instead of \b because Chinese characters are Unicode
# word chars: in "查询v_sale_order" a \b before "v" would NOT match (CJK char
# is a word char too), silently letting the identifier through mixed-language
# payloads. The lookarounds only exclude ASCII identifier neighbors, so
# "revenue", "over_value" and "CV_sale" still survive untouched.
_LIVE_EVENT_FORBIDDEN_RE = re.compile(
    r"(?i:\b(?:select|insert|update|delete|from|where|join|group\s+by|order\s+by|"
    r"limit|inner\s+join|left\s+join|create\s+table|alter\s+table|drop\s+table)\b"
    r"|(?<![A-Za-z0-9_])erp_[tv]_[a-z0-9_]+(?![A-Za-z0-9_])"
    r"|(?<![A-Za-z0-9_])v_[a-z0-9][a-z0-9_]*(?![A-Za-z0-9_]))"
    r"|\bF[A-Z][A-Z0-9_]{1,15}\b|\bF\d{2,8}\b",
)
_LIVE_EVENT_SAFE_FALLBACK = "[data]"

# ── Dynamic schema-metadata supplement ───────────────────────────────────────
# The static patterns above can only cover CONVENTIONAL prefixes (erp_t_/erp_v_/
# v_). Real deployments also have tables whose names are indistinguishable from
# prose ("sales_summary", "md_t_lz_price") — hardcoding those would be brittle
# and cross-deployment wrong. Instead, the existing sanitizer accepts runtime
# registration of real schema identifiers (wired from ``db_tools._describe_schema``,
# where the datasource's actual table list is enumerated). This EXTENDS the
# existing sanitizer — it is not a second sanitization framework.
#
# Registration rule: an identifier must contain an underscore (snake_case =
# schema-shaped, not English prose). This is the evidence bar that stops
# "users"/"orders" from becoming scrub targets that corrupt ordinary text.
_DYNAMIC_IDENTIFIERS: set[str] = set()
_DYNAMIC_RE: "re.Pattern | None" = None
_DYNAMIC_MAX_IDENTIFIERS = 2000  # bound the compiled alternation
_DYNAMIC_MIN_LEN = 4


def _normalize_identifier(n: str) -> list[str]:
    """Normalize a schema identifier into scrub-target candidates.

    Handles case, surrounding quoting (`` `name` `` / ``"name"``) and schema
    qualification (``sales.inventory_snapshot`` → the dotted form AND the bare
    last segment — a live event may mention either). The snake_case evidence
    gate is applied to EVERY candidate.
    """
    cleaned = n.strip()
    # Strip quoting PER DOT-SEGMENT: `` `sales`.`inv_snapshot` `` must yield
    # the bare "inv_snapshot", not "`inv_snapshot" (inner quotes survive a
    # whole-string strip and would poison the registered pattern).
    parts = []
    for seg in cleaned.split("."):
        seg = seg.strip()
        while seg[:1] in ("`", '"', "'", "["):
            seg = seg[1:]
        while seg[-1:] in ("`", '"', "'", "]"):
            seg = seg[:-1]
        seg = seg.strip().lower()
        if seg:
            parts.append(seg)
    if not parts:
        return []
    out = []
    if len(parts) > 1:
        # Schema-qualified → also register the bare last segment (events most
        # often mention the bare form).
        out.append(parts[-1])
    out.append(".".join(parts))
    return out


def register_sensitive_identifiers(names) -> int:
    """Register real schema identifiers (table/view names) for scrubbing.

    Called from datasource schema-enumeration paths (e.g. describe_schema).
    Returns the number of identifiers actually added. Thread-safe: the worst
    race is one event being checked against the previous alternation, which
    only ever under-scrubs for a name registered a moment earlier.

    Scope: deliberately PROCESS-GLOBAL. A name registered from datasource A's
    describe also scrubs datasource B's / tenant B's events — over-blocking
    (fail-safe), never under-blocking: there is no per-tenant allowance that
    could open a cross-tenant leak path.

    Cap: at ``_DYNAMIC_MAX_IDENTIFIERS`` further names are skipped AND logged
    at WARNING (never silently dropped) — the static prefix patterns still
    apply to unregistered names.
    """
    global _DYNAMIC_RE
    added = 0
    skipped = 0
    with threading.Lock():
        for n in names or []:
            if not isinstance(n, str):
                continue
            for cand in _normalize_identifier(n):
                if (
                    len(cand) < _DYNAMIC_MIN_LEN
                    or "_" not in cand
                    or cand in _DYNAMIC_IDENTIFIERS
                ):
                    continue
                if len(_DYNAMIC_IDENTIFIERS) >= _DYNAMIC_MAX_IDENTIFIERS:
                    skipped += 1
                    continue
                _DYNAMIC_IDENTIFIERS.add(cand)
                added += 1
        if added:
            alts = sorted(_DYNAMIC_IDENTIFIERS, key=len, reverse=True)
            _DYNAMIC_RE = re.compile(
                "(?<![A-Za-z0-9_])(?:"
                + "|".join(re.escape(a) for a in alts)
                + ")(?![A-Za-z0-9_])",
                re.IGNORECASE,
            )
    if skipped:
        logger.warning(
            "sensitive-identifier cap (%d) reached: %d candidate name(s) NOT "
            "registered — static prefix patterns still apply to them",
            _DYNAMIC_MAX_IDENTIFIERS, skipped,
        )
    return added


def _dynamic_identifier_hit(val: str) -> bool:
    return _DYNAMIC_RE is not None and bool(_DYNAMIC_RE.search(val))


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
        if _LIVE_EVENT_FORBIDDEN_RE.search(val) or _dynamic_identifier_hit(val):
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


# ── Structured agent-execution events (multi-agent orchestration milestone) ─
#
# A tool call is NOT an agent execution. When the orchestrator delegates to a
# real specialist (delegate_task / ask_data_agent / …), the specialist runs
# with its own execution identity and emits a structured event lifecycle
# through the SAME live_event channel — no second streaming system:
#
#   agent_started      — specialist dispatched, executing
#   agent_tool_started — one tool invocation inside the specialist loop
#   agent_tool_completed / (failed variant via status)
#   agent_completed / agent_failed — terminal, with duration + summary
#
# Every event carries the correlation identifiers the frontend tree needs:
# ``agent_execution_id`` (the specialist's AgentInvocation row id — same id
# the persisted tree uses), ``parent_agent_execution_id``, ``agent_id``,
# ``agent_name``, ``task_id``, ``delegation_mode`` and ``status``. Tool rows
# additionally carry ``tool_name`` + ``duration_ms``. Payloads carry only
# metadata (names/counts/durations) — never prompts, credentials or raw
# source content (the shared sanitizer still runs).

_AGENT_EXECUTION_EVENT_TYPES = frozenset({
    "agent_started", "agent_progress", "agent_tool_started",
    "agent_tool_completed", "agent_completed", "agent_failed",
    "agent_result_received", "peer_collaboration_started",
    "verification_started", "verification_completed",
})


def build_agent_execution_event(
    event_type: str,
    params: dict | None = None,
    count: list[int] | None = None,
) -> dict | None:
    """Build a typed agent-execution live event (``None`` when capped)."""
    if event_type not in _AGENT_EXECUTION_EVENT_TYPES:
        return None
    return _build_live_event(event_type, event_type, params, count=count)


def sse_agent_execution_event(
    event_type: str,
    params: dict | None = None,
    count: list[int] | None = None,
) -> str | None:
    """Build + serialize an agent-execution event as an SSE frame."""
    event = build_agent_execution_event(event_type, params, count=count)
    if event is None:
        return None
    return _sse_live_event(event)


def append_agent_execution_event(
    progress: list | None, event_type: str, params: dict | None,
) -> None:
    """Append one structured agent-execution SSE frame to a handler's
    progress channel (the ``context["progress_emitter"]`` list the router
    drains live during long-running batches). Never raises; no-op when
    ``progress`` is None (non-streaming callers)."""
    if progress is None:
        return
    try:
        frame = sse_agent_execution_event(event_type, params)
        if frame:
            progress.append(frame)
    except Exception:  # noqa: BLE001 — observability must never break execution
        pass
