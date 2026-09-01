# Dashboard Interaction State

## Purpose

A live dashboard earns trust through its states: users must always know
whether data is fresh, loading, stale, or broken — without the UI ever
flashing or lying.

## Use when

- Building or reviewing the dashboard viewer, popup, or polling logic.
- Designing KPI delta / unit / subtitle behavior for `create_dashboard`.

## Polling UX

- Poll interval is clamped to 10–300s (default 30s).
- Pause polling while the browser tab is hidden (`visibilitychange`);
  refresh immediately when it becomes visible again.
- Always show `Updated HH:MM:SS` in the header; spin the refresh icon
  only while a fetch is in flight.
- Manual refresh button must always work, even between polls.

## No-flash refresh

- Keep the last good data on screen during refresh — never clear widgets
  to blank/spinner on every poll.
- Skeletons are for FIRST load only; subsequent polls update in place.
- Per-widget min-heights prevent layout shift when data arrives.

## Errors & stale data

- A failing widget shows its own error card (destructive tint + retry
  hint); other widgets keep rendering.
- If a poll fails, keep showing the last good result — the `Updated`
  timestamp communicates staleness honestly.
- Never fake success: an error state beats silently wrong data.

## KPI options (optional, in widget `options`)

- `unit` — suffix appended to the formatted value (`%`, `ms`, `USD`).
- `compare_column` — a second numeric column; renders a delta chip with
  ▲/▼ arrow and % change vs the main `value_column`
  (success tint for up, destructive for down, muted for ~0).
- `subtitle` — one muted context line under the value.

All three are optional; a KPI with only `value_column` must still render
cleanly.

## Popup vs inline

- Chat card → opens the right-side sheet (live, polling).
- My Space → centered modal.
- Show a `Live` badge whenever polling is active.

## Common mistakes

- Clearing all data on every poll → flashing, unreadable dashboard.
- Full-page spinner on refresh → only the icon spins.
- No updated timestamp → users can't tell live from stale.
- One bad widget blanking the dashboard → errors stay local.
