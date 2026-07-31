# Dashboard Skills Integration — 2026-07-22

## Context

User asked for 9 resources to be integrated into the Zhanlu Agent so it can build interactive, professional, real-time auto-updating dashboards from databases. Per AskUserQuestion (2026-07-22), the scope was confirmed as:

- **Install all 7 skill-format resources** (skip the Massive MCP server).
- **No external accounts** (Datadog / Grafana / LSEG / Vercel / Massive) — the API-dependent skills are installed as **methodology guidance only**.
- **Skills + end-to-end wiring** — install the skill files AND guarantee the dashboard chain fires end-to-end (query DB → dashboard skill → interactive HTML artifact → inline preview, with a fallback when the LLM produced neither a marker nor a tool call).

This is a parallel of the 2026-07-21 skill integration batch (`docs/plans/2026-07-21-agent-skill-integration-design.md`), but focused on **building dashboards from a user's own database** rather than on enterprise SaaS connectors.

---

## Sources (verified 2026-07-22)

| Resource | Upstream | Source path | Status |
|---|---|---|---|
| UI/UX Pro Max | `nextlevelbuilder/ui-ux-pro-max-skill` (v2.11.0) | `.claude/skills/ui-ux-pro-max/` | **refreshed to v2.11.0** |
| Build Dashboard | `anthropics/knowledge-work-plugins` | `data/skills/build-dashboard/` | **refreshed, addendum appended** (upstream body already matched local; the new addendum is the part that binds the skill to the marker contract) |
| Task Management | `anthropics/knowledge-work-plugins` | `productivity/skills/task-management/` | **new install** |
| Interactive Dashboard Builder | `anthropics/knowledge-work-plugins` | `skills/interactive-dashboard-builder/` | **NOT INSTALLED — URL 404s.** The actual file does not exist in the upstream repo; full tree search confirmed (see "Source notes" below). The same functional gap is fully covered by `build-dashboard`. |
| Datadog API | `anthropics/claude-tag-plugins` | `datadog/skills/datadog-api/` | **new install** (guidance-only, no DD keys) |
| Grafana API | `anthropics/claude-tag-plugins` | `grafana/skills/grafana-api/` | **new install** (guidance-only, no GRAFANA_TOKEN) |
| Macro Rates Monitor | `anthropics/financial-services` | `plugins/partner-built/lseg/skills/macro-rates-monitor/` | **new install** (guidance-only, needs LSEG MCP tools) |
| Vercel Sandbox | `vercel/sandbox` | `skills/sandbox/` | **new install** (renamed to `vercel-sandbox` to avoid collision with Zhanlu's existing native `sandbox` skill; guidance-only) |
| Massive MCP server | `joinmassive/mcp-server` | n/a | **EXCLUDED** (user scope answer) |

All files downloaded via **jsdelivr CDN** (`https://cdn.jsdelivr.net/gh/{owner}/{repo}@{branch}/{path}`) — same pattern that worked reliably on 2026-07-21. `raw.githubusercontent.com` was still flaky from this host.

### Source notes

- The `interactive-dashboard-builder` listing on `mcpservers.org/agent-skills/anthropic/` references a path that does not exist in the upstream repo. GitHub Contents API confirms the path returns 404; a recursive tree search of `anthropics/knowledge-work-plugins` found 14 dashboard-adjacent skills (build-dashboard, create-viz, data-visualization, explore-data, etc.) but no `interactive-dashboard-builder`. The directory `backend/skills/interactive-dashboard-builder/` is left in place as a placeholder README so the planned wiring remains intact if upstream eventually publishes the skill.

- The `vercel/sandbox` skill's SKILL.md has `name: sandbox` in frontmatter, which would have collided with Zhanlu's existing native `sandbox` skill (it would have overwritten it in the in-memory registry). The frontmatter was patched to `name: vercel-sandbox` and the directory renamed to match. `verify_dashboard_skills.py` asserts the frontmatter name matches the directory name to prevent future drift.

---

## Skill directory layout (changes from this batch)

```
backend/skills/
├── task-management/                [NEW]    SKILL.md (2.6 KB) + manifest.yaml
├── datadog-api/                    [NEW]    SKILL.md (13.7 KB) + references/api.md + scripts/dd_logs.sh + manifest.yaml
├── grafana-api/                    [NEW]    SKILL.md (9.5 KB) + references/api.md + manifest.yaml
├── macro-rates-monitor/            [NEW]    SKILL.md (4.5 KB) + manifest.yaml (LSEG dep noted)
├── vercel-sandbox/                 [NEW]    SKILL.md (30.4 KB) + manifest.yaml (frontmatter name overridden to "vercel-sandbox")
├── interactive-dashboard-builder/  [NEW-PLACEHOLDER]  README.md only (URL 404; covered by build-dashboard)
├── build-dashboard/                [MODIFY] SKILL.md (31.7 KB) — upstream content matched + new "Zhanlu runtime integration" addendum
└── ui-ux-pro-max/                  [MODIFY] v2.11.0 refresh:
                                         - SKILL.md  (690 → ~196 lines, slim + references)
                                         - references/quick-reference.md (21.6 KB, new)
                                         - references/pro-rules.md       (9.5 KB, new)
                                         - data/colors.csv   (32 → 38 KB, more palettes)
                                         - data/products.csv (58 → 73 KB, more types)
                                         - scripts/validate_data.py (new)
                                         - scripts/core.py, design_system.py, search.py (updated)
                                         - data/stacks/{avalonia,uno,uwp,winui,wpf}.csv (5 new v2.11.0 stacks)
                                         - manifest.yaml v1.0.0 → v2.11.0 (description updated)
```

---

## End-to-end wiring — the runtime contract

Three stages, no new database tables, no new config keys, no frontend changes.

### 1. Skill addendum (build-dashboard)

Appended a clearly-marked `## Zhanlu runtime integration` section to `backend/skills/build-dashboard/SKILL.md`. The section documents:

- How `ask_data_agent` rows are injected into the assistant's context and must be embedded in the HTML as a `<script id="dashboard-data" type="application/json">` block.
- The exact `◤DASHBOARD◤{...}◤END_DASHBOARD◤` marker shape.
- The optional `setInterval` auto-refresh pattern that activates **only** when a same-origin JSON URL is explicitly provided, and gracefully degrades to a static snapshot for `file://` exports.
- Performance guidance (≤1,000 rows embedded; 1k–10k pre-aggregated; >10k sample down).
- A note that the platform guarantees a dashboard artifact via the orchestrator fallback even when the LLM emits no marker.

On a future upstream refresh of `build-dashboard`, this section should be **re-applied verbatim** (the section is marked as such and lives at the bottom of the file). Do not edit the upstream body to satisfy the marker contract.

### 2. DASHBOARD marker pipeline

`backend/app/services/artifact_markers.py`:
- `SUPPORTED_KINDS` now includes `"DASHBOARD"`.
- Module docstring documents the new marker shape.

`backend/app/services/generation_orchestrator.py`:
- `_MARKER_KIND_TO_TYPE["DASHBOARD"] = "html"`.
- `_FORMAT_TO_ARTIFACT_TYPE["dashboard"] = "html"` (the only "synthetic" format we render).
- `_marker_to_artifact_args("DASHBOARD", ...)` reads the `html_path` from the marker payload into `payload["html_content"]` (bounded 2 MB; out-of-range or missing files fall back to a no-content html artifact so the marker always surfaces *something* rather than silently dropping).
- `_read_dashboard_html()` is a small safe-read helper with the 2 MB cap + path resolution (relative to cwd or backend/) + defensive error handling.

### 3. Server-driven dashboard fallback

`ensure_artifact_for_doc_request()` (`generation_orchestrator.py`) — when `doc_format == "dashboard"`, the function now takes a dedicated branch:

- Mines `ask_data_agent` row dicts from `tool_calls_for_frontend` (capped at 500 rows — the documented platform cap).
- Renders a self-contained interactive HTML dashboard via `_synthesize_dashboard_html()` — KPI cards + 1 Chart.js bar chart + sortable / filterable table, all embedded with the rows as JSON.
- When rows are empty, still renders a valid empty-state shell (title + prose summary + "no data" panel) — never silence, never a dropped request.
- Calls `_create_artifact_tool(..., type="html", ...)` to attach the artifact and surface the inline iframe preview.
- Honors the existing duplicate-safety guards: if `already_created` is non-empty, or any prior `create_artifact` / `run_sandbox_skill` tool call succeeded, the fallback is skipped (no double artifact).
- Handler failures are non-fatal: the function logs and returns `None` (matches module convention).

`_synthesize_dashboard_html()` is a single O(rows) pass using stdlib string templates. Chart.js loads from jsDelivr with a verified SRI hash; styles follow a `ui-reasoning.csv`-inspired dark theme. Generated HTML is fully self-contained — works equally for inline iframe preview and static download.

### What stays unchanged

- **Frontend** — `ArtifactPreviewCard.jsx` (iframe branch), `HtmlArtifactPreview.jsx`, `FilePreviewer.jsx` already render `type="html"` artifacts as interactive inline previews. No rebuild needed.
- **Other formats** — the docx / pptx / pdf / html fallback paths are unchanged; the existing `test_fallback_skipped_for_non_renderable_format` test had its skip list trimmed (`dashboard` was the only previously-non-renderable format that is now renderable).
- **`create_artifact` tool** — already accepted `type="html"` with `payload.html_content` (pre-existing handler behavior, no new code).

