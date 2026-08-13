# Plan: Fix Automation Chat Artifacts + File Preview

**Date:** 2026-08-13
**Status:** Ready for execution

## Context

Two related visual bugs in the automation-agent chat UX, both rooted in the
same file (`AutomationFile` rows exist in the DB and render correctly in the
"Scheduled" list and "Files in this task" modal, but never reach the inline
chat bubble or the eye-icon preview):

1. **No inline chat preview card.** The assistant chat message produced by an
   automation run has no `artifacts`, so `MessageBubble.jsx` →
   `partitionArtifacts(message.artifacts)` returns empty and no
   `InlineArtifactPreview` card (Preview/Open/Download) renders. Root cause:
   `_persist_run_to_chat` in `backend/app/services/automation_executor.py`
   writes the assistant `ChatMessage` with `artifacts=None` in both of its
   branches (update-in-place and fresh-insert).

2. **"Files in this task" eye-icon preview fails** with Microsoft Office Online
   "An error occurred". Root cause: `frontend/src/components/chat/FilePreviewer.jsx`
   has no branch for automation-file URLs
   (`/api/automations/files/{id}/download`). Office files (docx etc.) fall
   through to the `view.officeapps.live.com` external viewer, which cannot
   reach the auth-protected backend URL.

The backend already exposes the primitives needed to fix both:
- `_chat_message_artifacts_for_files(files)` serializer (canonical definition in
  `backend/app/routers/automation_api.py:102`, aliased/proxied into
  `automation_executor.py` as a module-level helper).
- `POST /api/automations/files/{file_id}/preview-token` → `{token, url}` where
  `url` is `/api/automations/files/{file_id}/preview?token=...`, used already by
  `InlineArtifactPreview.jsx` (via `authFetch` from `@/api/authFetch`).
- `GET /api/automations/files/{file_id}/preview?token=...` renders docx→HTML etc.

## Global Constraints

- **TDD strictly.** Write the failing test first, run it to confirm it fails
  (RED), implement the minimal fix, run again to confirm it passes (GREEN).
  Report both commands and their output.
- **Minimal change.** Fix only the two named bugs. Do not refactor unrelated
  code, do not rename existing functions, do not add features (YAGNI).
- **Backend venv:** use `backend/venv/bin/python` to run pytest. Run only the
  focused test file (SSH RAM limited — no full-suite backgrounding):
  `backend/venv/bin/python -m pytest backend/tests/test_persist_run_to_chat.py -q`.
- **Frontend tests:** `frontend/node_modules/.bin/vitest run
  src/components/chat/FilePreviewer.test.jsx` (no `--watch`).
- **Files exist / soft-delete:** `AutomationFile` rows have `execution_id` and
  `is_deleted` (via TimestampedBase). Only attach files where
  `is_deleted == False`.
- **Artifact shape:** serialized artifacts must carry
  `source: "automation_file"` so `partitionArtifacts` picks them up. Use the
  existing `_chat_message_artifacts_for_files` serializer — do NOT hand-roll a
  new shape.
- **No new columns / no migration.** This work does not touch any DB schema.
- **Docker:** backend has no `--reload`; service changes require
  `docker restart zhanlu-backend` to take effect. This is out of scope for the
  automated task loop but must be noted in the final handoff.

## Task 1: Attach AutomationFile artifacts to the assistant chat message

**Goal:** After an automation run persists its assistant chat message, that
message's `artifacts` field must be populated from the run's `AutomationFile`
rows (so the inline preview card renders).

**Files:**
- `backend/app/services/automation_executor.py` — the fix.
- `backend/tests/test_persist_run_to_chat.py` — new tests.

**Steps:**
1. Add a helper in `automation_executor.py`:
   `_attach_automation_files_to_assistant(db, execution_id, asst_msg)` that
   queries `AutomationFile` filtered by `execution_id == <id>` and
   `is_deleted == False`, and if any rows exist sets
   `asst_msg.artifacts = _chat_message_artifacts_for_files(files)`. If no rows
   exist, leave `asst_msg.artifacts` unchanged (None).
2. Inside `_persist_run_to_chat`, in the block that writes the assistant
   `ChatMessage`, compute the file list once and apply it in BOTH branches:
   - the "update existing assistant message in place" branch (set
     `.artifacts` on the existing message), and
   - the "create a fresh assistant message" branch (pass `artifacts=...` to the
     constructor).
   Apply the artifacts AFTER the message is flushed/added so the
   `execution_id` foreign key is resolvable; the query itself is by
   `execution_id`, so compute the list before `db.commit()`.

**Tests (add to `test_persist_run_to_chat.py`):**
- **RED/GREEN test 1 — files present:** Given an execution with two
  non-deleted `AutomationFile` rows, after `_persist_run_to_chat` the persisted
  assistant `ChatMessage.artifacts` is a list of length 2, and every item has
  `source == "automation_file"`.
- **RED/GREEN test 2 — no files:** Given an execution with zero `AutomationFile`
  rows, the assistant `ChatMessage.artifacts` remains `None`.

**Acceptance criteria:**
- Both new tests pass; the full `test_persist_run_to_chat.py` file stays green.
- `artifacts` uses the existing serializer shape (verified by asserting
  `source == "automation_file"` on each item).
- No change to `partitionArtifacts` or any frontend code in this task.

## Task 2: Fix FilePreviewer for automation-file URLs

**Goal:** The eye-icon preview in "Files in this task" must render automation
files (docx etc.) via the backend preview-token flow instead of failing through
the MS Office Online viewer.

**Files:**
- `frontend/src/components/chat/FilePreviewer.jsx` — the fix.
- `frontend/src/components/chat/FilePreviewer.test.jsx` — new tests (create if
  absent, following existing frontend test conventions).

**Steps:**
1. Add an automation-file detector in `FilePreviewer.jsx`:
   `getAutomationFileId(url)` returning the `{id}` captured from
   `/api/automations/files/{id}/...` (regex on the URL path segment), or `null`
   when the URL is not an automation-file URL.
2. Extract the office-doc preview into an inner component (e.g.
   `DocxSignedIframe`) that handles BOTH `/api/uploads/` URLs and
   `/api/automations/files/{id}/` URLs: for automation files, `POST
   /api/automations/files/{id}/preview-token` (via `authFetch`, same pattern as
   `InlineArtifactPreview.jsx`), take the returned `url`, and render an
   `<iframe src={url}>`. For `/api/uploads/` URLs, keep the existing
   `/api/files/preview` (mammoth) behavior.
3. Short-circuit docx (and other previewable office types) automation files
   BEFORE the MS Office Online fallback, and guard the MS Live branch with
   `!getAutomationFileId(url)` so automation-file URLs can never reach it.

**Tests (add to `FilePreviewer.test.jsx`):**
- **test 1 — mint token + iframe:** an automation-file docx URL mints a
  preview token (assert the `POST .../preview-token` call) and renders an
  iframe whose `src` is the returned `url`.
- **test 2 — no MS Live:** the MS Office Online viewer is NOT used for an
  automation-file docx URL.
- **test 3 — fallback on 5xx:** if token mint fails (5xx), the component falls
  back gracefully (no crash; assert the fallback state is reached).
- **test 4 — uploads unchanged:** a `/api/uploads/` URL still goes through the
  existing local-preview path and does NOT mint an automation preview token.

**Acceptance criteria:**
- All 4 new tests pass; existing frontend tests that touch `FilePreviewer` stay
  green (run only the focused file).
- Automation-file docx URLs never reach the MS Office Online viewer.
- `/api/uploads/` behavior is unchanged (no token minted).

## Risks

- **`_chat_message_artifacts_for_files` proxy scope:** confirm the serializer is
  importable/aliased in `automation_executor.py` (it is — module-level alias
  exists). If it is not, import it from `automation_api` without introducing a
  circular import; the serializer is a pure function over file rows.
- **`authFetch` import path in FilePreviewer:** verify the exact existing import
  (`@/api/authFetch`) used by `InlineArtifactPreview.jsx` and mirror it.
- **`execution_id` type:** `AutomationFile.execution_id` is the execution's
  primary key; pass the same `execution.id` value `_persist_run_to_chat`
  already holds.

## Success Criteria

1. Backend: assistant chat message for an automation run carries
   `artifacts` sourced from `AutomationFile` rows (non-deleted).
2. Frontend: eye-icon preview renders automation files via preview token;
   MS Live viewer never receives an automation-file URL.
3. Both task test files green; no unrelated files modified.
4. After `docker restart zhanlu-backend`, a real run shows: inline preview
   card in chat + working eye-icon preview.
