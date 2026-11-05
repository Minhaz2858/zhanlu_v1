"""agent_invocations recorder — per-run trace + cost for every agent execution.

The ``agent_invocations`` table has existed with a full schema since the
harness scaffolding, but nothing ever wrote to it (live count was 0 rows).
This module is the single writer: the main chat loop records one row per
conversation turn; the delegate_task tool records one row per sub-agent run
(with parent linkage via ``execution_id``/``span_id``).

Columns mapped (see app/models/agent_invocation.py):
  agent_app_id, conversation_id, execution_id, user_id, invocation_type,
  trigger, input_message, input_json, status, output_json,
  assistant_content, error_message, confidence_score, started_at,
  completed_at, duration_ms, token_usage, cost_amount, trace_id, span_id

Best-effort by design: recording must NEVER crash the chat loop — every
failure is caught and logged (non-fatal).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_DEFAULT_AGENT_NAME = "general_assistant"


def _current_trace_id() -> Optional[str]:
    """Read the active OpenTelemetry span's trace id (ADLC P1-6).

    Best-effort: returns None when the OTel SDK is absent or no span is
    active — the agent_invocations.trace_id column stays NULL instead of
    failing the record.
    """
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx is None or not getattr(ctx, "is_valid", False):
            return None
        tid = format(ctx.trace_id, "032x")
        return tid if tid != "0" * 32 else None
    except Exception:  # noqa: BLE001 — tracing must never break recording
        return None


def _resolve_agent_app_id(db: Session, agent_app_id: Optional[str]) -> Optional[str]:
    """Resolve a missing app id by name lookup (no hardcoded UUIDs)."""
    if agent_app_id:
        return agent_app_id
    try:
        from app.models.agent_app import AgentApp

        app = db.query(AgentApp).filter(AgentApp.name == _DEFAULT_AGENT_NAME).first()
        return app.id if app else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_invocations: app resolution failed: %s", exc)
        return None


def record_invocation(
    db: Session,
    *,
    agent_app_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    execution_id: Optional[str] = None,
    user_id: Optional[str] = None,
    invocation_type: str = "conversation",
    trigger: str = "user",
    input_message: Optional[str] = None,
    input_json: Optional[dict] = None,
    status: str = "completed",
    output_json: Optional[dict] = None,
    assistant_content: Optional[str] = None,
    error_message: Optional[str] = None,
    confidence_score: Optional[float] = None,
    started_at: Optional[datetime] = None,
    completed_at: Optional[datetime] = None,
    duration_ms: Optional[int] = None,
    token_usage: Optional[dict] = None,
    cost_amount: Optional[float] = None,
    trace_id: Optional[str] = None,
    parent_invocation_id: Optional[str] = None,
    model_name: Optional[str] = None,
    tool_call_count: Optional[int] = None,
    route: Optional[str] = None,
    outcome: Optional[str] = None,
    tools_selected: Optional[int] = None,
    delegation_mode: Optional[str] = None,
    open_invocation: bool = False,
) -> Optional[str]:
    """Insert one agent_invocation row. Returns the new row id (or None).

    All fields optional except db. ``invocation_type`` should be one of
    ``conversation`` (main loop) or ``sub_agent`` (delegate_task).
    """
    try:
        from app.models.agent_invocation import AgentInvocation

        resolved_app_id = _resolve_agent_app_id(db, agent_app_id)
        if trace_id is None:
            trace_id = _current_trace_id()
        if not resolved_app_id:
            logger.warning("agent_invocations: no agent app to bind — skipping row")
            return None

        now = datetime.now(timezone.utc)
        # Two-phase lifecycle (mission §23 / Hybrid H1): ``open_invocation=True``
        # records the invocation AT DELEGATION START as ``running`` with no
        # completion time — the child becomes observable in the execution tree
        # while in flight. The SAME row is later finalized by
        # ``update_invocation_terminal`` (no duplicate row, no fabricated
        # completion).
        if open_invocation:
            status = "running"
        row = AgentInvocation(
            agent_app_id=resolved_app_id,
            conversation_id=conversation_id,
            execution_id=execution_id or str(uuid.uuid4()),
            user_id=user_id,
            invocation_type=invocation_type,
            trigger=trigger,
            input_message=(input_message or "")[:2000] or None,
            input_json=input_json,
            status=status,
            output_json=output_json,
            assistant_content=(assistant_content or "")[:10000] or None,
            error_message=(error_message or "")[:2000] or None,
            confidence_score=confidence_score,
            started_at=started_at or now,
            completed_at=None if open_invocation else (completed_at or now),
            duration_ms=None if open_invocation else duration_ms,
            token_usage=token_usage,
            cost_amount=cost_amount,
            trace_id=trace_id,
            span_id=parent_invocation_id,
            model_name=(model_name or "")[:128] or None,
            tool_call_count=tool_call_count,
            route=(route or "")[:32] or None,
            outcome=(outcome or "")[:32] or None,
            tools_selected=tools_selected,
            delegation_mode=(delegation_mode or "")[:16] or None,
        )
        db.add(row)
        db.commit()
        # RL Phase 0: persist the formalized reward for this trajectory
        # (bandit policy updates + SFT/GRPO datasets consume these).
        # Open (running) invocations have no outcome yet — the reward is
        # computed on the terminal update instead.
        if open_invocation:
            return row.id
        try:
            from app.services.rl.rewards import compute_reward
            from app.models.trajectory_reward import TrajectoryReward

            reward, components = compute_reward(
                status=status,
                duration_ms=duration_ms,
                tool_call_count=tool_call_count,
                # Wire the caller-supplied judge confidence (0-1) into the
                # quality-bonus component; previously it was always dropped.
                quality_score=(
                    confidence_score
                    if isinstance(confidence_score, (int, float)) else None
                ),
            )
            db.add(TrajectoryReward(
                invocation_id=row.id,
                agent_name=invocation_type,
                route=route,
                reward=reward,
                components=components,
                source="auto",
            ))
            db.commit()
            # RL Phase 1: feed the policy bandit (route arm).
            from app.services.rl.policy_bandit import record_reward

            record_reward(route or "", reward)
        except Exception as _rw_err:  # noqa: BLE001 — rewards are never fatal
            logger.debug("trajectory reward persist failed (non-fatal): %s", _rw_err)
        return row.id
    except Exception as exc:  # noqa: BLE001 — recording must never break the loop
        logger.warning("agent_invocations: failed to record (%s) — non-fatal", exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


# Terminal statuses — first terminal write wins; never regress (mission §23.6:
# out-of-order events cannot regress terminal status).
_TERMINAL_INVOCATION_STATUSES = {
    "completed", "partial", "failed", "timeout", "cancelled", "blocked",
}


def update_invocation_terminal(
    db: Session,
    invocation_id: str,
    *,
    status: str,
    output_json: Optional[dict] = None,
    error_message: Optional[str] = None,
    completed_at: Optional[datetime] = None,
    duration_ms: Optional[int] = None,
    token_usage: Optional[dict] = None,
    cost_amount: Optional[float] = None,
    confidence_score: Optional[float] = None,
    assistant_content: Optional[str] = None,
    tool_call_count: Optional[int] = None,
    model_name: Optional[str] = None,
) -> bool:
    """Finalize a previously-opened (``running``) invocation row IN PLACE.

    Two-phase lifecycle for delegation (mission §23 / Hybrid H1): the child
    row exists from delegation start; this closes the SAME row with a
    truthful terminal state. Guarantees:
      - terminal status never regresses (late/duplicate update is a no-op);
      - no second row is created;
      - reward is computed once, on the first terminal transition.
    Returns True when the row was transitioned, False when the update was
    refused (unknown id, non-terminal target status, or already-terminal row).
    """
    if status not in _TERMINAL_INVOCATION_STATUSES:
        return False
    try:
        from app.models.agent_invocation import AgentInvocation

        row = db.get(AgentInvocation, invocation_id)
        if row is None:
            return False
        if (row.status or "") in _TERMINAL_INVOCATION_STATUSES:
            return False
        now = datetime.now(timezone.utc)
        row.status = status
        if output_json is not None:
            row.output_json = output_json
        if error_message is not None:
            row.error_message = (error_message or "")[:2000] or None
        if assistant_content:
            row.assistant_content = assistant_content[:10000]
        row.completed_at = completed_at or now
        row.duration_ms = duration_ms
        if token_usage is not None:
            row.token_usage = token_usage
        if cost_amount is not None:
            row.cost_amount = cost_amount
        if confidence_score is not None:
            row.confidence_score = confidence_score
        if tool_call_count is not None:
            row.tool_call_count = tool_call_count
        if model_name:
            row.model_name = (model_name or "")[:128] or None
        db.commit()
        # RL reward on the first terminal transition (same policy as insert).
        try:
            from app.services.rl.rewards import compute_reward
            from app.models.trajectory_reward import TrajectoryReward
            from app.services.rl.policy_bandit import record_reward

            reward, components = compute_reward(
                status=status,
                duration_ms=duration_ms,
                tool_call_count=tool_call_count,
                quality_score=(
                    confidence_score
                    if isinstance(confidence_score, (int, float)) else None
                ),
            )
            db.add(TrajectoryReward(
                invocation_id=row.id,
                agent_name=row.invocation_type,
                route=row.route,
                reward=reward,
                components=components,
                source="auto",
            ))
            db.commit()
            record_reward(row.route or "", reward)
        except Exception as _rw_err:  # noqa: BLE001 — rewards are never fatal
            logger.debug("trajectory reward persist failed (non-fatal): %s", _rw_err)
        return True
    except Exception as exc:  # noqa: BLE001 — must never break the caller
        logger.warning("agent_invocations: terminal update failed (%s) — non-fatal", exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False


def recover_orphaned_invocations(db: Session, max_age_seconds: int = 3600) -> int:
    """H1-B: reconcile `running` invocations whose worker disappeared.

    A process crash / restart between the delegation-start insert and the
    finally-block terminal update would otherwise leave a row RUNNING forever.
    Truthful recovery policy (mission §23):
      - the row is NEVER marked completed (we cannot know the outcome);
      - it becomes ``failed`` with an explicit ``orphaned`` error and
        ``output_json.recovery`` metadata ({reason: worker_lost, recovered_at});
      - rows younger than ``max_age_seconds`` are untouched (legitimately
        in-flight — delegation has wall-clock caps well below this);
      - already-terminal rows are untouched; recovery is idempotent.

    Called periodically from the scheduler (single worker holds the lock) and
    safe to call manually. Returns the number of rows recovered.
    """
    try:
        from datetime import timedelta
        from app.models.agent_invocation import AgentInvocation

        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)
        rows = (
            db.query(AgentInvocation)
            .filter(
                AgentInvocation.status == "running",
                AgentInvocation.started_at.isnot(None),
                AgentInvocation.started_at < cutoff,
            )
            .all()
        )
        recovered = 0
        now = datetime.now(timezone.utc)

        def _aware(dt):
            # SQLite returns naive datetimes even for aware-stored values;
            # normalize before arithmetic (Postgres returns aware).
            if dt is None:
                return None
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

        for row in rows:
            # First-terminal-wins: skip rows that became terminal meanwhile.
            if (row.status or "") != "running":
                continue
            prev = row.output_json if isinstance(row.output_json, dict) else {}
            row.status = "failed"
            row.error_message = (
                "orphaned: worker terminated before completion "
                "(recovered by lifecycle reconciliation)"
            )[:2000]
            row.completed_at = now
            started = _aware(row.started_at)
            if started:
                row.duration_ms = max(
                    0, int((now - started).total_seconds() * 1000)
                )
            row.output_json = {
                **prev,
                "recovery": {
                    "reason": "worker_lost",
                    "recovered_at": now.isoformat(),
                    "max_age_seconds": max_age_seconds,
                },
            }
            recovered += 1
        if recovered:
            db.commit()
            logger.warning(
                "agent_invocations: recovered %d orphaned running invocation(s) "
                "(older than %ss)", recovered, max_age_seconds,
            )
        return recovered
    except Exception as exc:  # noqa: BLE001 — recovery must never break the app
        logger.warning("agent_invocations: orphan recovery failed (%s) — non-fatal", exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 0


def resolve_invocation_for_feedback(
    db: Session, conversation_id: str, assistant_content: str
) -> Optional[str]:
    """Find the invocation a thumbs rating applies to (best-effort).

    Matches the latest invocations of the conversation against the message
    content (loose prefix match, either direction — the stored
    ``assistant_content`` may be truncated or the message edited).
    """
    try:
        from app.models.agent_invocation import AgentInvocation

        rows = (
            db.query(AgentInvocation)
            .filter(AgentInvocation.conversation_id == conversation_id)
            .order_by(AgentInvocation.created_date.desc())
            .limit(25)
            .all()
        )
        if not rows:
            return None
        needle = (assistant_content or "").strip()[:120]
        if needle:
            for inv in rows:
                content = (inv.assistant_content or "").strip()[:120]
                if content and (
                    needle == content
                    or needle.startswith(content)
                    or content.startswith(needle)
                ):
                    return inv.id
        for inv in rows:  # fallback: the latest main-loop turn
            if inv.invocation_type == "conversation":
                return inv.id
        return rows[0].id
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.debug("agent_invocations: feedback resolve failed: %s", exc)
        return None


def apply_human_feedback(
    db: Session,
    *,
    invocation_id: str,
    rating: int,
    comment: str = "",
) -> Optional[str]:
    """Record a human (HITL) reward override for one invocation.

    Upserts the ``source="human"`` TrajectoryReward (auto rows stay
    untouched) and feeds the route bandit, so explicit user feedback
    steers policy — the feedback loop the RL phases consumed but nothing
    wrote to. Best-effort; never raises.
    """
    try:
        from app.models.agent_invocation import AgentInvocation
        from app.models.trajectory_reward import TrajectoryReward
        from app.services.rl.policy_bandit import record_reward as _bandit_record

        inv = (
            db.query(AgentInvocation)
            .filter(AgentInvocation.id == invocation_id)
            .first()
        )
        if inv is None:
            return None

        reward = 1.0 if rating > 0 else -1.0
        components: dict = {"outcome": reward, "total": reward,
                            "origin": "human_thumbs"}
        if comment:
            components["comment"] = comment[:200]

        row = (
            db.query(TrajectoryReward)
            .filter(
                TrajectoryReward.invocation_id == invocation_id,
                TrajectoryReward.source == "human",
            )
            .first()
        )
        if row is None:
            row = TrajectoryReward(
                invocation_id=invocation_id,
                agent_name=inv.invocation_type,
                route=inv.route,
                reward=reward,
                components=components,
                source="human",
            )
            db.add(row)
        else:
            row.reward = reward
            row.components = components
        db.commit()
        # Human signal steers the bandit (same gate as auto rewards).
        _bandit_record(inv.route or "", reward)
        return row.id
    except Exception as exc:  # noqa: BLE001 — feedback must never 500
        logger.warning("agent_invocations: human feedback failed (non-fatal): %s", exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


# ─────────────────────────────────────────────────────────────────────────────
# P2 Phase 3 — Multi-agent execution-tree builder (visualization backend)
#
# Reuses the existing AgentInvocation records (no new event infrastructure, no
# new storage). Parent→child linkage is overloaded onto ``span_id`` (the parent
# invocation id) by record_invocation — there is no dedicated parent_invocation_id
# column. Tenant isolation is enforced by the caller (conversation_id + user_id);
# this builder only assembles the tree for rows the caller is authorized to read.
# ─────────────────────────────────────────────────────────────────────────────

# Visualization states the frontend renders (a superset of AgentInvocation.status).
AGENT_TREE_VIZ_STATES = [
    "planning", "queued", "running", "delegating", "waiting_for_child",
    "reviewing", "waiting_for_approval", "completed", "partial",
    "failed", "blocked", "cancelled", "timeout",
]


def _resolve_agent_names(db: Session, agent_app_ids) -> dict:
    names = {}
    ids = [i for i in (agent_app_ids or []) if i]
    if not ids:
        return names
    try:
        from app.models.agent_app import AgentApp

        rows = (
            db.query(AgentApp.id, AgentApp.name)
            .filter(AgentApp.id.in_(ids))
            .all()
        )
        for _aid, _name in rows:
            names[_aid] = _name
    except Exception as _err:  # noqa: BLE001 — name resolution is best-effort
        logger.debug("agent_invocations: name resolve failed: %s", _err)
    return names


def _node_phase(node: dict) -> str:
    status = node.get("status")
    if status == "blocked":
        return "blocked"
    _review = node.get("review") or {}
    if isinstance(_review, dict) and _review.get("overall") in (
        "needs_revision", "rejected",
    ):
        return "reviewing"
    if node.get("delegation_mode") in ("supervisor", "peer") and status in (
        "running", "pending", "queued",
    ):
        return "delegating"
    if node.get("awaiting_approval"):
        return "waiting_for_approval"
    return {
        "running": "running",
        "completed": "completed",
        "failed": "failed",
        "timeout": "timeout",
        "cancelled": "cancelled",
        "pending": "queued",
        "partial": "partial",
    }.get(status, "queued")


def _scrub(value):
    """Content-invariant for machine-generated strings on the tree API.

    Driver error messages and broker block reasons can embed SQL fragments,
    column names or table identifiers. Reuses the SAME sanitizer that gates
    live events (``sse_builders._sanitize_live_event_params``) — no second
    framework. User/agent-authored prose (``task``) is deliberately NOT
    scrubbed: it is conversation-scoped and tenant-isolated.
    """
    if not isinstance(value, str) or not value:
        return value
    from app.services.agent_loop.sse_builders import _sanitize_live_event_params

    return _sanitize_live_event_params({"v": value})["v"]


def _row_to_node(row, names: dict) -> dict:
    _oj = row.output_json or {}
    _agent_app_id = row.agent_app_id
    _agent_name = (
        names.get(_agent_app_id)
        or _oj.get("agent_name")
        or row.invocation_type
    )
    _is_ga = _agent_name == "general_assistant"
    _review = _oj.get("review")
    _evidence_ids = _oj.get("evidence_ids") or []
    _status = row.status
    _is_partial = bool(_oj.get("truncated")) or (
        isinstance(_review, dict)
        and _review.get("overall") in ("needs_revision", "rejected")
    )
    if _is_partial and _status == "completed":
        _status = "partial"
    _duration = row.duration_ms
    if _duration is None and row.started_at and row.completed_at:
        _duration = max(
            0,
            int((row.completed_at - row.started_at).total_seconds() * 1000),
        )
    _contract = _oj.get("task_contract") or {}
    _node = {
        "id": row.id,
        "agent_app_id": _agent_app_id,
        "agent_name": _agent_name,
        "role": "supervisor" if _is_ga else "specialist",
        "invocation_type": row.invocation_type,
        "delegation_mode": row.delegation_mode
        or (
            "peer"
            if (_oj.get("requesting_agent_id")
                 and _oj.get("requesting_agent_id") != "general_assistant")
            else None
        ),
        "status": _status,
        "task": (row.input_message or "")[:600],
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "duration_ms": _duration,
        "token_usage": row.token_usage,
        "cost_amount": row.cost_amount,
        "confidence_score": row.confidence_score,
        "error_message": _scrub(row.error_message),
        "block_reason": _scrub(
            _oj.get("policy_violation") if _status == "blocked" else None
        ),
        "review": _review,
        "evidence_ids": _evidence_ids,
        "tools": _contract.get("selected_tools"),
        "children": [],
    }
    _node["phase"] = _node_phase(_node)
    return _node


def _rows_to_tree(db: Session, rows, awaiting_approval: bool = False) -> dict:
    names = _resolve_agent_names(db, [r.agent_app_id for r in rows])
    nodes = [_row_to_node(r, names) for r in rows]
    by_id = {n["id"]: n for n in nodes}
    span_map = {r.id: r.span_id for r in rows}
    children_of: dict = {}
    roots = []
    for n in nodes:
        parent = span_map.get(n["id"])
        if parent and parent in by_id:
            children_of.setdefault(parent, []).append(n)
        else:
            roots.append(n)
    for _pid, _kids in children_of.items():
        by_id[_pid]["children"] = _kids
    # Consistency audit D1 (2026-10-31): top-level delegated sub_agent rows
    # carry no span_id (the main loop does not inject parent linkage), which
    # hoisted real specialists to ROOTS — siblings of the supervisor instead
    # of children. Attach each orphan to the supervisor turn whose execution
    # window contains it (latest such supervisor wins). This is the truthful
    # structure: the delegation DID happen inside that supervisor turn.
    def _ts(iso):
        if not iso:
            return None
        try:
            return datetime.fromisoformat(iso)
        except Exception:  # noqa: BLE001
            return None

    supervisors = [
        n for n in roots
        if n.get("role") == "supervisor"
        and n.get("invocation_type") == "conversation"
    ]
    if supervisors:
        _dt_min = datetime.min
        still_roots = []
        for n in roots:
            if n is None or n.get("role") == "supervisor" or n in supervisors:
                still_roots.append(n)
                continue
            n_start = _ts(n.get("started_at"))
            if n_start is None:
                still_roots.append(n)
                continue
            host = None
            for sup in supervisors:
                s_start = _ts(sup.get("started_at"))
                s_end = _ts(sup.get("completed_at"))
                if s_start is None or n_start < s_start:
                    continue
                if s_end is not None and n_start > s_end:
                    continue
                if host is None or (
                    _ts(host.get("started_at")) or _dt_min
                ) < s_start:
                    host = sup
            if host is not None:
                host["children"].append(n)
            else:
                still_roots.append(n)
        roots = still_roots
    if awaiting_approval and roots:
        roots[0]["awaiting_approval"] = True
        if roots[0]["phase"] != "blocked":
            roots[0]["phase"] = "waiting_for_approval"
    return {"roots": roots, "summary": _summarize(roots)}


def build_agent_tree(
    db: Session, *, conversation_id: str, user_id: str,
    awaiting_approval: bool = False,
    started_after: Optional[datetime] = None,
) -> dict:
    """Build a parent→child execution tree from AgentInvocation rows.

    ``span_id`` carries the parent invocation id; ``delegation_mode`` (and the
    persisted requesting_agent_id) distinguishes supervisor vs peer delegation.
    Tenant isolation is enforced by the caller.

    ``started_after`` (consistency audit D1, 2026-10-31) scopes the tree to a
    SINGLE TURN: the frontend anchors each assistant message to its first
    persisted live-event timestamp, so one user turn renders exactly its own
    supervisor row and children instead of every prior turn in the
    conversation.
    """
    from app.models.agent_invocation import AgentInvocation

    rows = (
        db.query(AgentInvocation)
        .filter(
            AgentInvocation.conversation_id == conversation_id,
            AgentInvocation.user_id == user_id,
            AgentInvocation.is_deleted == False,  # noqa: E712
        )
        .order_by(AgentInvocation.started_at)
        .all()
    )
    if started_after is not None:
        _sa = started_after
        if _sa.tzinfo is not None:
            _sa = _sa.astimezone(timezone.utc).replace(tzinfo=None)
        rows = [r for r in rows if r.started_at is None or r.started_at >= _sa]
    return _rows_to_tree(db, rows, awaiting_approval=awaiting_approval)


def _summarize(roots) -> dict:
    _s = {
        "total": 0, "completed": 0, "partial": 0, "failed": 0,
        "peer_collaborations": 0, "reviewer_status": None,
        "total_tokens": 0, "estimated_cost": 0.0, "evidence_count": 0,
        "duration_ms": None,
    }
    _evidence = set()
    _start = None
    _end = None
    from datetime import datetime as _dt

    def _norm(dtstr):
        if not dtstr:
            return None
        try:
            _dtv = _dt.fromisoformat(dtstr)
            return _dtv.replace(tzinfo=None)
        except Exception:  # noqa: BLE001
            return None

    def _walk(nodes):
        nonlocal _start, _end
        for n in nodes:
            _s["total"] += 1
            _st = n["status"]
            if _st == "completed":
                _s["completed"] += 1
            elif _st == "partial":
                _s["partial"] += 1
            elif _st in ("failed", "timeout", "cancelled", "blocked"):
                _s["failed"] += 1
            if n["delegation_mode"] == "peer":
                _s["peer_collaborations"] += 1
            if n["review"]:
                _s["reviewer_status"] = n["review"].get("overall")
            _tu = n["token_usage"] or {}
            if isinstance(_tu, dict):
                _s["total_tokens"] += int(
                    _tu.get("total_tokens") or _tu.get("total") or 0
                )
            if n["cost_amount"] is not None:
                _s["estimated_cost"] += float(n["cost_amount"])
            for _e in (n["evidence_ids"] or []):
                _evidence.add(_e)
            _ts = _norm(n["started_at"])
            if _ts is not None and (_start is None or _ts < _start):
                _start = _ts
            _te = _norm(n["completed_at"])
            if _te is not None and (_end is None or _te > _end):
                _end = _te
            _walk(n["children"])

    _walk(roots)
    if _start and _end:
        _s["duration_ms"] = max(0, int((_end - _start).total_seconds() * 1000))
    _s["evidence_count"] = len(_evidence)
    return _s
