# Automation Deliverables — Formats, Inline Preview, and Content Quality

**Date**: 2026-07-28
**Status**: approved design (pending implementation plan)
**Scope**: all three workstreams, one spec (user-approved)

## Problem statement

Automations today effectively ship only HTML reports whose content is often
hollow, and one crash path kills scheduled runs outright:

1. **Format choice is invisible at creation time.** The chat "New Automation
   Task" flow never asks for an output format, so every task defaults to
   `html` — even though the backend already supports `html`, `pptx`, `docx`,
   `pdf`, `json`, `csv`, `md` (`document_generator.generate_document` +
   `AutomationTask.output_format`), auto-detects format keywords from natural
   language in `create_automation`, and the task detail page already has a
   format selector.
2. **Content is not useful.** The run prompt never names the target format,
   so `document_generator`'s structure parsers (pptx slides from `##`
   headings + bullets, docx sections, etc.) receive flat prose. Worse, when
   no data source is bound, the LLM fabricates plausible content ("ERP source
   is accessible") over empty tool results instead of saying the source is
   missing — and a `data_sync` task with zero bound sources runs at all.
3. **`'NoneType' object has no attribute 'id'` crash** in scheduled runs when
   the task's `created_by_id` no longer resolves to a user row
   (`_run_agent_in_conversation` passes `user=None` to `add_message_stream`,
   which dereferences `user.id`). Observed live 2026-07-28 15:30 UTC.

## Existing infrastructure (do NOT rebuild)

- `backend/app/services/document_generator.py` — 7 output formats.
- `backend/app/routers/automation_api.py` — `_PREVIEWABLE_FILE_TYPES`
  (csv, docx, gif, htm, html, jpeg, jpg, json, md, pdf, png, pptx, svg, txt,
  webp, xlsx); `/api/automations/files/{id}/preview` converts docx/pptx/xlsx
  via `app.services.artifacts.preview_builder.convert_to_preview`.
- `frontend/src/components/chat/InlineArtifactPreview.jsx` — inline card +
  expandable iframe preview; Word/PDF/Presentation labels already present.
- `backend/app/services/agent_tools.py::_create_automation` — accepts
  `output_format`, with keyword auto-detection as a safety net.
- `frontend/src/pages/AutomationTaskDetail.jsx` — edit-form format selector.
- Truthful run outcomes (2026-07-28 item 5): tool-outcome gate,
  `_tool_warnings_line`, failure notifications.

## Workstream 1 — Format choice at creation + verified inline preview

### 1.1 Chat create dialog: output-format dropdown

The "New Automation Task" dialog (rendered from `/automation` and from the
chat create flow) gains an **Output format** select:

| Label | Value |
|---|---|
| HTML report (default) | `html` |
| Word document | `docx` |
| PowerPoint deck | `pptx` |
| PDF document | `pdf` |
| Markdown | `md` |
| Excel workbook | `xlsx` |
| CSV | `csv` |
| JSON | `json` |

The chosen value is included in the structured prefill as
`- Output format: docx`, so the LLM passes `output_format` to
`create_automation` directly (no reliance on keyword detection).

### 1.2 Draft card: format badge

The chat draft/confirmation card shows the resolved format as a badge
(e.g. `DOCX`) next to the type badge, so the user sees what they will get
before confirming. It reads `output_format` from the draft fields (falling
back to the same keyword detection for display only).

### 1.3 Live preview verification (acceptance)

Browser-verified on the deployed site: create a `docx` automation via chat,
run it, confirm (a) the deliverable is a real `.docx`, (b) the chat card
shows "Word document", and (c) expanding the inline preview renders the
converted content (not a download-only fallback).

## Workstream 2 — Content quality

### 2.1 Format-aware run prompt

`execute_automation`'s `agent_prompt` gains a per-format instruction block
(aligned with what `document_generator` parses):

- `pptx`: structure the answer as `## <slide title>` sections of short
  bullet points (the generator maps h2 → slide title, h3/bullets → body).
- `docx` / `md` / `pdf` / `html`: use `##` sections with concrete tables of
  real figures (the generators map markdown tables into native tables).
- `xlsx` / `csv` / `json`: emit a single machine-readable block (JSON
  object or CSV rows), no prose.

### 2.2 Honesty guardrail

Append to the run prompt (runtime agent): "Report only what tools actually
returned. If a data source is missing, empty, or unreachable, say so
explicitly and stop — never fabricate rows, totals, or 'successful sync'
claims."

### 2.3 Data-source preflight (deterministic gate)

Before invoking the agent for a `data_sync` task, compute the run's
effective data-source bindings using the SAME source of truth as the tool
runtime (`data_source_runtime`):

```
bound = get_bound_data_source_ids(agent)
bound = _maybe_extend_with_workspace_auto_bind(db, agent, bound)
bound = _extend_with_project_kbs(db, agent, bound, task.project_id)
```

If the result is empty, mark the run failed immediately (retryable — a
binding may be added later) with: "no data source bound to project
'<name>' — bind one in My Space → Data Agent, then re-run." No LLM call is
spent producing fiction. Non-`data_sync` types are unaffected.

## Workstream 3 — Creator-user crash fix

In `_run_agent_in_conversation`, when the task's `created_by_id` resolves to
no user row, raise a dedicated error immediately; `execute_automation` marks
the run failed via `_mark_failed_no_retry` with: "task creator account no
longer exists — re-save the automation to reassign ownership." No opaque
`AttributeError`; no retries against a permanently failing condition.

## Testing

Backend (SQLite harness, per-repo convention):
- Preflight: `data_sync` with zero bound sources → failed with the config
  error; with a bound source → proceeds. Non-`data_sync` unaffected.
- Format prompt: `agent_prompt` for a pptx task contains slide-structure
  guidance; docx contains `##`-section guidance.
- Crash fix: creator-missing task → failed with the ownership message,
  no exception propagated.
- Existing suites stay green (`test_automation_truthful_failure`,
  `test_execute_automation_poll`).

Frontend (vitest):
- Dialog emits the selected format into the prefill.
- Draft card renders the format badge.

Live (playwright-cli, deployed site):
- End-to-end docx run + inline preview (Workstream 1.3).

## Out of scope

- Repairing the actual data-source bindings for the user's "test" project
  (their configuration action).
- Document template/styling redesign in `document_generator`.
- Changing `manage_automation` (already accepts `output_format`).
- The 5 pre-existing `test_automation_skip_confirmation.py` failures
  (mock-seam drift, unrelated).
