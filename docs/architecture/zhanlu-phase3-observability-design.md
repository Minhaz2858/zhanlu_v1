# Phase 3.5 — Enterprise Observability & Cost Management Design (2026-10-31)

## Root cause

Point-in-time cost summaries existed (`/governance/cost`, `/governance/usage-summary`)
but nothing gave administrators an ORG-level operational view: executions by
department/agent, reliability, tool-failure distribution, recovery frequency,
approvals, unauthorized-attempt counts. All needed data was already persisted
and org-scoped.

## Design

`app/services/usage_analytics.py` — read-only aggregation over EXISTING tables:

- `org_overview(db, org_id, days)`: total executions; by_status;
  by_department (AgentApp→Project.department); by_agent (per-agent rollup);
  avg_duration_ms; tool-failure distribution (AgentRunStep failed tool_call
  steps, grouped by tool_name); delegation_frequency (delegation_mode);
  recovery_frequency (output_json.recovery contract); token_usage;
  known_cost_total + unknown_pricing_executions; approvals by status;
  unauthorized_operation_attempts (PolicyDecision decision="deny").
- `agent_stats(db, org_id, days)`: per-agent invocation count, success/
  failure rate, avg latency, tokens, cost honesty.

API (`app/routers/usage_analytics.py`, admin-only via `require_admin`,
org-scoped to the CALLER's org — cross-tenant data is unreachable by
construction): `GET /api/usage/overview`, `GET /api/usage/agents`.

UI: AdminConsole new **Usage** tab — KPI cards (executions, avg duration,
recoveries, unauthorized attempts), token/cost honesty block, by-agent
table, tool-failure list. Loaded independently so an analytics failure never
blanks the other admin tabs.

## Cost honesty rules

- Only entries with a recorded `cost_amount` are summed.
- Executions without known model pricing are counted separately
  (`unknown_pricing_executions`) and NEVER estimated (mission §F).
- No second billing/telemetry system: everything reads existing tables.

## P3.4 relationship

`reliability_metrics.platform_reliability_report` (separate module) computes
the mission's 12-metric table for evaluation; `provider_status` records the
LIVE provider state (2026-10-31: HTTP 401 invalid_api_key — blocked) without
ever exposing secrets.
