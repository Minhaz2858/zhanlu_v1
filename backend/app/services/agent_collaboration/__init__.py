"""Hybrid multi-agent Collaboration Broker (P1/P2, 2026-10-31).

This package is a LOGICAL service that lives INSIDE the existing
``delegate_task`` runtime — it does NOT create a new orchestrator, a
fourth agent runtime, or a duplicate agent registry. It centralizes the
governance the roadmap demands for controlled specialist-to-specialist
(Pattern C) collaboration:

  - resolve the requesting agent
  - discover/validate the target (published, available)
  - authorize the delegation (target passes its OWN permission checks;
    the broker only enforces collaboration policy)
  - enforce execution budgets: depth, child count, concurrency, timeout,
    token/cost
  - cycle detection (A -> B -> A), duplicate-task prevention
  - record parent-child relationships (via existing invocation linking)
  - return structured results and handle failures/partial completion

The broker is a thin policy layer around the already-existing
``_run_sub_agent`` / DAG agent-node dispatch. Every check is feature-flag
gated through ``settings.COLLAB_POLICY_ENFORCED`` so a single env flip
rolls governance back to pure legacy delegation.
"""
