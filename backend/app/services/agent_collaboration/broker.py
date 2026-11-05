"""Collaboration Broker — governance for controlled specialist delegation.

Spec §2D + §4 (P1/P2, 2026-10-31). This is a logical service that wraps
the existing ``_run_sub_agent`` / DAG agent-node execution. It enforces:

  - target availability & publication status (only PUBLISHED agents delegate)
  - cycle detection (A -> B -> A, A -> B -> C -> A)
  - delegation depth limit (caps runaway recursion)
  - max children per requesting agent (no unbounded sub-teams)
  - duplicate-task prevention (no re-delegation of the same objective)
  - requesting-agent delegation allowlist (opt-in; off = all allowed)
  - audit: every policy block is recorded as an observable invocation

The original user identity (user_id / organization_id / project_id) is
carried through the contract unchanged — the requesting AGENT can never
substitute the user's authorization. Actual resource/tool permission
checks for the TARGET agent happen at execution time (AgentApp-aware
delegation + RLS); the broker refuses only collaboration-policy violations.
"""
from __future__ import annotations

import logging
import threading

from sqlalchemy.orm import Session

from app.config import settings
from .contract import build_contract
from .governance_store import get_store

logger = logging.getLogger(__name__)

# NOTE: governance state (global concurrency slots, per-conversation dedup set,
# child counter, cumulative token usage) now lives in `governance_store`, which
# is Redis-backed when reachable and falls back to in-process otherwise. This is
# what makes the guarantees hold across multiple Uvicorn workers / processes /
# backend instances (Part 1.1). The legacy in-process globals were NOT
# multi-worker safe.


def _target_publication_state(db: Session, name: str) -> str:
    """Return 'none' (builtin agent), 'published', or 'unpublished'."""
    if not name:
        return "none"
    try:
        from app.models.agent_app import AgentApp

        row = (
            db.query(AgentApp)
            .filter(AgentApp.name == name, AgentApp.is_deleted == False)  # noqa: E712
            .first()
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("broker: AgentApp lookup failed for %r: %s", name, exc)
        return "none"
    if row is None:
        return "none"
    return "published" if row.status == "published" else "unpublished"


def _requesting_allowed(requesting: str | None) -> bool:
    allow = (getattr(settings, "COLLAB_DELEGATORS_ALLOWLIST", "") or "").strip()
    if not allow:
        return True
    allowed = {a.strip() for a in allow.split(",") if a.strip()}
    return (requesting or "") in allowed


def _blocked(ctx: dict, target: str, requesting: str | None, reason: str, contract, db):
    logger.warning(
        "broker: BLOCKED delegation %s -> %s: %s",
        requesting, target, reason,
    )
    _audit_block(db, requesting, target, reason, contract)
    return False, reason, ctx


def _audit_block(db: Session | None, requesting, target, reason, contract) -> None:
    """Record a policy block as an observable (blocked) invocation row."""
    if db is None:
        return
    try:
        from app.services.agent_invocations import record_invocation
        from datetime import datetime, timezone

        record_invocation(
            db,
            conversation_id=contract.conversation_id,
            agent_app_id=None,
            user_id=contract.user_id,
            invocation_type="sub_agent",
            trigger="agent",
            input_message=f"delegation {requesting} -> {target}",
            status="blocked",
            output_json={"policy_violation": reason, "contract": contract.to_dict()},
            error_message=reason,
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
            duration_ms=0,
            delegation_mode="peer" if requesting and requesting != "general_assistant" else "supervisor",
        )
    except Exception as exc:  # noqa: BLE001 — non-fatal audit
        logger.debug("broker: audit record failed (non-fatal): %s", exc)


def evaluate_delegation(
    *,
    target_agent_id: str,
    objective: str,
    context: dict | None,
    db: Session,
    parent_invocation_id: str | None = None,
) -> tuple[bool, str | None, dict]:
    """Evaluate a single delegation request.

    Returns ``(allowed, reason, extended_context)``. ``extended_context`` is
    a copy of ``context`` with ``delegation_path`` extended, ``requesting_-
    agent_id`` / ``parent_invocation_id`` / ``agent_task_contract`` stamped —
    ready to hand to the child sub-agent loop so NESTED delegations (Pattern
    C) are themselves governed. On block, ``allowed=False`` and ``reason``
    describes the violation.
    """
    ctx = dict(context or {})
    requesting = (ctx.get("requesting_agent_id") or ctx.get("agent_name"))

    contract = build_contract(
        objective=objective,
        target_agent_id=target_agent_id,
        context=ctx,
        parent_invocation_id=parent_invocation_id,
        token_budget=ctx.get("token_budget"),
    )

    # Shared governance store (Redis-backed, in-process fallback). `_conv_id`
    # namespaces dedup / child-count / token usage per conversation; calls with no
    # conversation id are isolated per-call (ephemeral) — matching pre-audit
    # semantics (dedup is intentionally NOT cross-conversation).
    _store = get_store()
    _conv_id = ctx.get("conversation_id") or f"_ephemeral_{id(ctx)}"

    # Master kill-switch: pure legacy delegation, no governance.
    if not getattr(settings, "COLLAB_POLICY_ENFORCED", True):
        path = list(ctx.get("delegation_path") or [])
        path.append(target_agent_id)
        ctx["delegation_path"] = path
        ctx["requesting_agent_id"] = requesting
        ctx["parent_invocation_id"] = parent_invocation_id
        return True, None, ctx

    # 1. Target availability / publication status.
    state = _target_publication_state(db, target_agent_id)
    if state == "unpublished":
        return _blocked(
            ctx, target_agent_id, requesting,
            "target agent is not published / unavailable", contract, db,
        )

    # 2. Duplicate-task prevention (checked BEFORE cycle/depth: re-issuing
    #    the identical objective from the same requester is a duplicate, not a
    #    fresh cycle — surfacing it as "duplicate" is the honest signal).
    #    The dedup set lives in the SHARED per-conversation store (keyed by
    #    conversation_id), so concurrent/sibling delegations collide correctly.
    if getattr(settings, "COLLAB_DEDUP_ENABLED", True):
        # Serialize to a string key: a Python tuple is fine for the in-process
        # set but Redis SET members must be bytes/str/int — storing a tuple
        # raises and the guard would silently fail open.
        key = "|".join([
            str(requesting), str(target_agent_id),
            (objective or "").strip().lower(),
        ])
        if not _store.conv_dedup_seen(_conv_id, key):
            return _blocked(
                ctx, target_agent_id, requesting,
                "duplicate delegation already issued in this context",
                contract, db,
            )

    # 3. Cycle detection (A -> B -> A).
    path = list(ctx.get("delegation_path") or [])
    if (getattr(settings, "COLLAB_CYCLE_DETECTION", True)
            and target_agent_id in path):
        return _blocked(
            ctx, target_agent_id, requesting,
            f"cycle detected in path {' -> '.join(path + [target_agent_id])}",
            contract, db,
        )

    # 4. Depth limit.
    if len(path) >= getattr(settings, "COLLAB_MAX_DEPTH", 4):
        return _blocked(
            ctx, target_agent_id, requesting,
            f"max delegation depth {settings.COLLAB_MAX_DEPTH} exceeded",
            contract, db,
        )

    # 5. Child count per requesting agent. Uses the SAME shared per-conversation
    #    store so a batch fan-out (many sibling children of one parent) is
    #    actually bounded — not a fresh copy per call.
    count = _store.conv_child_count(_conv_id, requesting)
    if count > getattr(settings, "COLLAB_MAX_CHILDREN_PER_PARENT", 8):
        return _blocked(
            ctx, target_agent_id, requesting,
            f"max children per parent {settings.COLLAB_MAX_CHILDREN_PER_PARENT} exceeded",
            contract, db,
        )

    # 5b. Token budget (Part 1.3). Cumulative per-conversation consumption is
    #     tracked in the shared store; once it has ALREADY reached the budget we
    #     stop further delegation. Completed findings are preserved (the parent
    #     receives a partial status) — no fabrication, no silent over-spend.
    _token_budget = ctx.get("token_budget")
    if not _token_budget or int(_token_budget) <= 0:
        _token_budget = int(getattr(settings, "COLLAB_TOKEN_BUDGET", 0) or 0)
    if _token_budget and _token_budget > 0:
        _used = _store.conv_token_total(_conv_id)
        if _used >= _token_budget:
            return _blocked(
                ctx, target_agent_id, requesting,
                f"token budget {_token_budget} already consumed ({_used})",
                contract, db,
            )

    # 6. Requesting-agent delegation allowlist (opt-in).
    if not _requesting_allowed(requesting):
        return _blocked(
            ctx, target_agent_id, requesting,
            "requesting agent not permitted to delegate", contract, db,
        )

    # Allowed: extend path + stamp identity for the child context.
    path.append(target_agent_id)
    ctx["delegation_path"] = path
    ctx["requesting_agent_id"] = requesting
    ctx["parent_invocation_id"] = parent_invocation_id
    ctx["agent_task_contract"] = contract.to_dict()
    ctx["trace_id"] = contract.trace_id
    return True, None, ctx


def acquire_concurrency(context: dict | None) -> tuple[bool, str | None]:
    """Claim a slot in the global top-level delegation concurrency budget.

    Only TOP-LEVEL (supervisor-initiated) delegations consume the budget;
    nested Pattern-C delegations carry a non-empty ``delegation_path`` and are
    intentionally excluded so the gate can never deadlock (an ancestor holding
    a slot would otherwise block its own descendant). Nested calls remain
    bounded by COLLAB_MAX_DEPTH / _CHILDREN / dedup instead.

    Returns ``(allowed, reason)``. The caller MUST call ``release_concurrency``
    exactly once on the success path (typically in a finally block).
    """
    if not getattr(settings, "COLLAB_POLICY_ENFORCED", True):
        return True, None
    if context and context.get("delegation_path"):
        return True, None
    limit = int(getattr(settings, "COLLAB_MAX_CONCURRENT", 4))
    ok = get_store().acquire_global_slot(limit)
    if not ok:
        return False, f"max concurrent tasks {limit} exceeded"
    return True, None


def release_concurrency() -> None:
    """Release a slot claimed by ``acquire_concurrency``."""
    get_store().release_global_slot()


def global_active_count() -> int:
    """Observable current top-level slot usage (tests / introspection)."""
    return get_store().global_active_count()


def reset_concurrency() -> None:
    """Test helper — clear governance state between cases."""
    from .governance_store import reset_store
    reset_store()


def reset_conv_state() -> None:
    """Test helper — clear per-conversation governance state between cases."""
    from .governance_store import reset_store
    reset_store()

