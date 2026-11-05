# Dynamic Orchestration Timeline — Implementation Plan (2026-11-02)

Phase 0 findings: dynamic LLM planning already exists (turn_planner.parse_dynamic_plan,
flag TURN_PLAN_DYNAMIC_ENABLED, 2–6 steps, fallback template = the 4 generic steps).
SSE infra: sse_builders._build_live_event + LIVE_EVENT_TEMPLATES (label registry) +
agent-execution allowlist. Persistence: ChatMessage.live_events (auto-replay).
Frontend: timeline already derives from events; gaps are status vocabulary, replanning,
dead events, attribution, counters, collapse trigger.

## Changes (reuse-only, no second event system)

### Backend
B1. turn_planner.py:
  - Steps gain stable `task_id` (uuid4 hex short, minted at plan build), `description`,
    `depends_on` (dynamic plan parse), status `pending`.
  - Dynamic prompt: 2–8 steps, request-shaped titles/descriptions (already LLM-driven).
  - New frames: `plan_step_updated_frame`, `plan_replan_started_frame` (labels added to
    LIVE_EVENT_TEMPLATES, both languages).
B2. agents.py v3 loop:
  - Emit `agent_retrying` inside smart-retry (L~17939-18053) with tool_label + attempt.
  - Emit `verification_started` before verification verdict; keep verify_passed/failed.
  - Evidence-driven replan (hook A): after smart-retry recovery or repeated tool
    failure, recompute the affected step → emit `plan_updated` (supersede/replace step)
    + `replanning_started` once per turn.
  - FSM path: upgrade the `retry` event at L13194 to also emit `replanning_started`.
B3. delegate_tool.py: thread current plan task linkage into agent_started params
    (`plan_task_id`) when TOOL_CONTEXT carries it; emit `peer_collaboration_started`
    when delegation_mode == "peer" (event already whitelisted).

### Frontend
F1. TaskExecutionTimeline.jsx — rewrite derivation (`derivePlanSteps` → `deriveTimeline`):
  - consumes plan_step_added/completed + plan_updated; stable task_id identity;
    statuses pending/running/done/failed/superseded; preserve completed on replan;
    superseded rows stay visible (struck through), new tasks appear in order.
  - agents nested under tasks: use agent_started.plan_task_id when present, else
    existing timestamp attribution (unchanged fallback).
F2. LiveActivityStream.jsx:
  - add agent_progress/agent_result_received/peer_collaboration_started/
    verification_started/verification_completed/agent_retrying to the suppress set
    (they surface in the timeline/tree instead of generic rows);
  - `cancelled` row styling (zinc) distinct from done;
  - business summary instead of raw counters: "Completed · Ns" / "Partial — N of M
    steps · Ns" / "Failed · Ns" (Phase 6: no technical counters).
F3. Chat.jsx: no new handler needed — new events flow through live_event → live_events;
  timeline consumes them. (plan_step_added/completed direct handlers stay for back-compat.)
F4. Collapse (Phase 7): auto-collapse triggers on authoritative terminal
  (finalize_done / phase_enter.done) via live_events — keep 1s-after-stream-end as
  fallback; individual agent completion never collapses.

### Tests (TDD, no paid LLM)
T1. backend tests/test_turn_planner_dynamic.py: task_id stability, description/depends_on
    parse, 2–8 step bounds, frames (updated/replan_started) shape.
T2. backend tests/test_orchestration_events.py: agent_retrying / verification_started /
    plan_updated emission points via pure helpers (no LLM).
T3. frontend TaskExecutionTimeline.test.jsx (vitest): scenarios 1-10,12-15 from the spec
    that are derivable from events (direct execution, delegation, parallel, sequential,
    peer, failure+retry, replan merge, dynamic add/supersede, partial, dedupe, replay,
    turn isolation, collapse-on-terminal).

### Out of scope / documented limitations
- True dependency-graph execution (depends_on recorded, rendered, not scheduling-critical).
- SSE reconnect/replay protocol beyond existing live_events persistence (no new transport).
- plan_task_id threading into delegation requires TOOL_CONTEXT step tracking (best-effort).
