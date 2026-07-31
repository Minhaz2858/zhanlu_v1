# Browse Marketplace in-page tab

Date: 2026-07-29
Status: Implemented
Owner: zhanlu frontend

## Goal

Move the skill marketplace from a separate `/marketplace` route into the
existing Skills page (`/toolkit`) as a real in-page tab. The new layout
is a horizontal row of source cards above a 3-column skill grid, matching
the design in the user-supplied screenshot.

A follow-up goal (same day): when a user opens the marketplace tab, the
curated source cards must populate their skill counts automatically —
no "0 skills" flash, no manual Sync click required. Adding a new source
must also auto-sync so its skills show up immediately.

## Changes

### Data model
- `SkillSource.brand_color: String(7)`, nullable hex (`#RRGGBB`)
- `SkillSource.icon_emoji: String(8)`, nullable single emoji
- Auto-migration in `backend/main.py:skill_sources` adds the columns on
  startup. The migration is idempotent and tolerates pre-existing rows.
- `seed_curated_sources` (in `app/services/skill_source_service.py`)
  backfills `brand_color` and `icon_emoji` on existing rows that predate
  the columns, then seeds curated sources with explicit values
  (Anthropic Skills: `#191919` + ✦, Awesome Claude Code: `#7C3AED` + ★).

### Auto-sync (the "prebuild cards" feature)
User feedback: "why this card are prebuild when user add skills source
then it need to show [the skills]". Three triggers populate the cards
without the user clicking Sync manually:

1. **Backend startup** — `main.py` lifespan handler spawns a daemon
   thread that calls `seed_curated_sources` + `get_curated_sources_needing_sync`
   + `sync_source` for every default source in `never` or `failed`
   state. The cards are populated before the user even opens the tab.
2. **Every GET /api/marketplace/sources** — same hook fires inside
   the request handler. Real browser requests run inside a running
   event loop, so the router uses `loop.create_task(sync_source(src.id))`
   for each pending source; tests and sync contexts (no event loop)
   fall back to `threading.Thread(...).start()` so the sync still
   happens.
3. **User-added sources** — `POST /api/marketplace/sources` already
   kicked off an async sync on add. Updated to pass `None` for the
   session (the background task gets its own request-independent
   session via `sync_source(source_id, db=None)`).

`sync_source` itself was refactored to accept an optional `db`. When
the caller passes `None`, it opens its own `SessionLocal()` and closes
it in a `finally` block. The previous version was SQLite-only for the
session lifetime; the FastAPI request-scoped session got closed the
moment the request returned, silently dropping the background write.

`get_curated_sources_needing_sync` now returns sources in `never` OR
`failed` state. A transient error (network blip, GitHub rate limit)
shouldn't permanently disable the card — the next list call retries.

### API
- `GET /api/marketplace/sources` — `brand_color` and `icon_emoji` are
  always present in the response (may be `null`).
- `POST /api/marketplace/sources` — accepts optional `brand_color` and
  `icon_emoji`; the server defaults to `#1f2937` and the first letter
  of the source name when omitted. Kicks off async sync in background.

### CASCADE delete
`ExternalSkill.source_id` has `ondelete="CASCADE"`. When a user
deletes a source, every skill that came from that source is removed in
the same transaction. Verified in live Postgres with a manual
INSERT + DELETE round-trip. Test `TestCascadeDelete` pins the
behavior so a future migration that drops the CASCADE breaks a test
instead of silently leaking rows.

Note: "Hide" (the action on default sources) does NOT trigger CASCADE
— it just sets `is_hidden=True`, leaving the skills in place. Hide is
reversible; Delete is not.

### Hard delete for default sources (user feedback 2026-07-29)
Previously the marketplace tab's button on a default (curated) source
was "Hide" (soft delete). The user reported that they wanted a real
"Delete" option that worked for ALL sources, not just user-added ones.

Added a `force=true` query param to `DELETE /api/marketplace/sources/{id}`:
- `is_default` + no `force` → soft hide (`is_hidden=True`, reversible)
- `is_default` + `force=true` → hard delete (CASCADE removes all skills)
- non-`is_default` → always hard delete (the `force` flag is ignored)

The marketplace tab now shows two buttons for default sources —
"**Hide**" (reversible, eye-off icon, neutral) and "**Delete**" (red,
trash icon, hard delete). User-added sources show just "**Delete**".

Both actions confirm with the user before doing anything destructive.

### Delete UX fix (user feedback 2026-07-29, follow-up)
The user reported "delete button is not working" right after the
delete buttons shipped. Root cause: the handler was using
`window.confirm` for the destructive action confirmation, which can be
silently blocked by some embedded webviews / browser settings, making
the click appear to do nothing. Network errors were also being
swallowed by `console.error` with no UI feedback.

Fixed by:
- New `ConfirmDialog` component (`src/components/ui/ConfirmDialog.jsx`)
  — an in-app modal that always renders in-DOM. The native dialog is
  no longer used for destructive actions.
- `Toolkit.handleMarketDeleteSource` now sets a `pendingConfirm`
  state with the dialog payload (title, message, action, destructive
  flag). The dialog renders, the user clicks "Hide" or "Delete" or
  "Cancel", and the actual API call fires from `confirmMarketDelete`.
- Toasts (`@/components/ui/use-toast` — `<Toaster />` is already
  mounted in `App.jsx`) fire on success / failure with the source
  name + a count of skills removed. No more silent failures.
- `MarketplaceTab` now accepts a `busy` prop. While the delete is in
  flight, the buttons disable to prevent a double-click from firing
  two DELETEs.
- `setActiveSourceId` is now called outside the `setMarketSources`
  updater (was a setState-in-setState anti-pattern).

Tests: `ConfirmDialog.test.jsx` (6 tests) + 3 new tests in
`SourceCard.test.jsx` covering the busy prop + the action passed to
`onDelete`. 22 frontend tests + 33 backend tests, all green.
For default-source hard deletes, the confirmation message includes
"PERMANENTLY DELETE" + the skill count + a note that the source may
reappear on the next backend restart if it's curated.

### Frontend
- New `SourceCard.jsx` — neutral dark card with a small monochrome
  icon on the left and the source name + skill count on the right.
  Active state: 1px primary border. Trailing "+" card opens the
  AddMarketplaceDialog. **No brand colors** — the cards read as
  navigation, not as a marketing surface (user feedback 2026-07-29:
  "make it simple not too much color full"). When `last_sync_status`
  is `"syncing"`, the skill count line shows a spinner + "Syncing…".
- New `MarketplaceTab.jsx` — single source's skill grid + search +
  sync controls. Replaces the old `SourceTab.jsx`.
- `AddMarketplaceDialog.jsx` — URL, name, description, and a single
  icon text input. The original 6-swatch color picker and 7-glyph
  swatch grid were removed for simplicity; brand_color is no longer
  sent from the UI (the field stays in the model for future use).
- `pages/Toolkit.jsx` — adds "Browse Marketplace" as a real tab
  (alongside Skills and MCP Servers). When the tab is active, a
  horizontal row of source cards is always visible above the grid.
  A 2-second poll fires when any source reports `last_sync_status=
  "syncing"` so the user sees the skill count tick up in real time.
- `pages/Marketplace.jsx` — DELETED.
- `App.jsx` — `/marketplace` route REMOVED.

### Removed
- `pages/Marketplace.jsx` (the page)
- `pages/Marketplace.tabs.test.jsx` (tests for the deleted page)
- `components/marketplace/SourceTab.jsx` (replaced by MarketplaceTab)
- `Marketplace` lazy import + `/marketplace` route in `App.jsx`

## Behavior

- The source cards row is always visible when the marketplace tab is
  active. Skills and MCP Servers tabs hide it.
- Active source defaults to the first non-hidden source.
- "My Skills" stays a modal (user choice — no side sheet).
- Clicking the "+" card on the source row opens the same
  AddMarketplaceDialog (with color + emoji inputs).
- Clicking a source card switches the active source; the skill grid
  below reloads.
- The user can still see "My Skills" via the existing "My Skills"
  button at the top of the page.
- On backend startup, all curated sources are auto-synced in
  background. Cards populate their `skill_count` before the user
  ever opens the tab. Verified: Anthropic Skills repo syncs 18
  skills automatically.
- When a user opens the marketplace tab, sources in `never` or
  `failed` state are auto-synced in background. The frontend polls
  every 2 seconds until no source is in `"syncing"` state.
- When a user deletes a non-default source, the CASCADE FK removes
  all `external_skills` rows for that source.

## Tests
- Backend: `test_skill_source_models.py` — brand fields round-trip +
  defaults + backfill.
- Backend: `test_skill_marketplace_api.py` — list endpoint includes
  fields, POST endpoint accepts fields, defaults applied when omitted.
- Backend: `test_skill_source_service.py` — `get_curated_sources_needing_sync`
  returns `never`+`failed` only; hides drop out; success drops out.
- Backend: `test_skill_source_auto_sync.py` — three tests pinning the
  list-hook behavior (unsynced → synced, already-synced → skipped,
  hidden → skipped) + one CASCADE delete test pinning the FK.
- Frontend: `SourceCard.test.jsx` — brand color, icon, active state,
  +add variant, "Syncing…" state, "N skills" state.

## Risks / followups
- No PATCH endpoint for editing a source's brand fields after creation.
  Deferred — user can re-add with new branding.
- The thread fallback in the list hook (no event loop) is the only
  way the sync runs in tests. The real browser path uses
  `loop.create_task`, which is faster but only works inside an async
  request context.
- The Anthropic source syncs 18 skills; the Awesome Claude Code
  source's GitHub repo has no `SKILL.md` files, so its sync
  legitimately fails. The card shows the failure and the user can
  click Sync to retry. The next list call will also auto-retry.
- The startup auto-sync thread is a daemon — it dies with the
  process. On a long-running sync, the thread may be killed mid-
  write. SQLAlchemy's commit() should be atomic per-call, so partial
  writes are unlikely.
- `Market` page (at `/market`) is the AGENT marketplace (different
  concept), NOT the skill marketplace. It is intentionally untouched.

---

## Update 2026-07-29 (same day, follow-up #2)

User reported: "after refresh it showing again it's not working how i want it". Hard-deleted default sources re-appeared on the next list call because the seed only checks row existence.

**Fix: tombstone table**
- New model: `app/models/removed_curated_url.py` — records `(url, removed_at, removed_by)`. The URL has a unique constraint.
- `seed_curated_sources` queries this table and skips URLs in it. Verified live in Postgres — hard-delete + tombstone + restart = source stays gone. Clear tombstone = seed re-creates on next list call.
- `DELETE /sources/{id}?force=true` writes the tombstone before hard-deleting, IF the source is default AND the URL isn't already tombstoned. Non-default sources don't need a tombstone (seed never creates them).
- `GET /sources/removed` — list tombstones. Returns `[{url, removed_at, removed_by}]`.
- `POST /sources/removed/{url}/restore` — clears the tombstone and re-seeds the source. Returns the new source row.

**Frontend**
- `api/marketplace.js`: `listRemovedSources()` + `restoreRemovedSource(url)`.
- `Toolkit.jsx`:
  - `loadMarketplace` now also fetches removed sources in parallel.
  - New "Show removed (N)" toggle below the source row — opens a panel listing each tombstoned URL with a Restore button.
  - `handleRestoreRemoved` — calls the restore endpoint, removes the URL from local `removedSources`, adds the restored source to `marketSources` immediately (no refresh needed).
  - `confirmMarketDelete` now also adds the deleted source's URL to local `removedSources` so the "Show removed" panel stays in sync.
  - Delete toast now mentions the tombstone + how to restore.

**Migration**
- `main.py` startup: `Base.metadata.create_all(bind=engine)` now runs for both SQLite AND Postgres. Postgres path was previously relying on a one-off `create_tables.py` script. `create_all` with `checkfirst=True` is idempotent — only creates tables that don't exist.
- Verified: `removed_curated_urls` table is in the live Postgres with all the right columns (id, url, removed_at, removed_by, plus the standard TimestampedBase fields).

**Tests** — 41 backend + 22 frontend, all green. New `test_skill_source_tombstone.py` has 8 tests:
- Hard delete of default source writes tombstone.
- Hard delete of non-default source does NOT write tombstone.
- Soft hide (default without force) does NOT write tombstone.
- Seed skips tombstoned URLs.
- Seed creates non-tombstoned curated sources (negative control).
- Restore clears tombstone and re-creates source.
- Restore of unknown URL returns 404.
- List-removed returns the tombstones.
