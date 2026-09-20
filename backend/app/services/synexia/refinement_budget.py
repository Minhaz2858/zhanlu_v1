"""ONE shared per-turn refinement budget for PLAN-level re-planning.

Why this exists
---------------
A single user turn could create SIX plans. Two independent budgets governed
re-planning and they multiplied:

    FSM VERIFY-loop   SYNEXIA_VERIFY_REPLAN_MAX = 1  -> up to 2 plans
    ACT-depth loop    SYNEXIA_ACT_REPLAN_MAX    = 2  -> up to 3 corrective
                                                       plans per level
    observed live     4 plans from one turn (12+ nodes, 129s wall clock)

Each failure bought another plan, so the budget was spent re-planning. That
is not a cosmetic cost: the planner's own chart step
(``sandbox:render_sales_chart``) was emitted and then left ``pending`` —
planned, never reached — because the turn had run out of room. The same
mechanism silently truncates any long multi-step task, not just charts.

The post-answer regeneration gates already share one budget
(``AGENT_REFINEMENT_MAX``). Plan-level re-planning did not, so a turn could
spend that budget AND a fresh set of plans. This module brings plan-level
refinement under the same idea: one counter for the whole turn.

How it is shared without new plumbing
-------------------------------------
Both re-plan paths funnel through ``plan_dag.generate_plan`` and both persist
a ``Plan`` row against the same ``execution_id``. The persisted row IS the
counter, so the two paths share it by construction — no context var to lose
across threads, no extra column, nothing to thread through signatures.

Exhaustion behaviour: refuse the new plan. Callers keep the content already
produced and the turn still answers; it never ships empty. Refusing a re-plan
is strictly better than spending the remaining wall clock to produce a plan
whose steps will not run.
"""

from __future__ import annotations

import logging
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

# Initial plan + this many refinements. 2 => the turn may re-plan once.
_DEFAULT_MAX = 2


def max_plans_per_turn() -> int:
    """Total plans one turn may create (initial plan + refinements)."""
    try:
        return max(1, int(getattr(settings, "SYNEXIA_TURN_REPLAN_MAX", _DEFAULT_MAX)))
    except Exception:
        return _DEFAULT_MAX


def plan_count(db, execution_id: Optional[str]) -> int:
    """Plans already persisted for this execution. 0 when unknowable."""
    if db is None or not execution_id:
        return 0
    try:
        from app.models.execution import Plan

        return int(
            db.query(Plan).filter(Plan.execution_id == execution_id).count() or 0
        )
    except Exception as exc:
        # Fail OPEN: a budget we cannot read must not silently suppress the
        # self-healing that keeps a turn alive.
        logger.debug("refinement budget: plan count unavailable (%s)", exc)
        return 0


def may_refine(db, execution_id: Optional[str]) -> bool:
    """True when this turn may create ANOTHER plan."""
    used = plan_count(db, execution_id)
    limit = max_plans_per_turn()
    if used >= limit:
        logger.info(
            "turn refinement budget exhausted (%d/%d plans used, exec=%s) — "
            "keeping existing results and not re-planning",
            used, limit, execution_id,
        )
        return False
    return True
