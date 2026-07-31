# interactive-dashboard-builder
#
# NOTE: The mcpservers.org listing (https://mcpservers.org/agent-skills/anthropic/interactive-dashboard-builder)
# references a path that does not exist in the upstream repo anthropics/knowledge-work-plugins
# (verified 2026-07-22 — path `skills/interactive-dashboard-builder` returns 404; full repo tree
# contains no such skill). The functional gap is already covered by the `build-dashboard` skill
# (also Anthropic, same `data/skills/build-dashboard` path) which provides the same
# self-contained Chart.js HTML dashboards (KPI cards, filters, sortable tables, etc).
#
# This directory is left in place so the planned wiring (DASHBOARD marker, html artifact routing,
# orchestrator fallback) remains intact and the path can be revived if/when upstream publishes
# the skill. See /root/zhanlu/docs/plans/2026-07-22-dashboard-skills-integration.md.
