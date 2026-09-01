# Chat Parity Gaps — Implementation Plan (2026-08-31) ✅ COMPLETE

**Goal:** Close 4 verified chat-parity gaps vs Kimi/GPT: global chat-history search, conversation export + share link, completion notifications, parallel session tabs.

**Status: ALL 4 SHIPPED + LIVE-VERIFIED + COMMITTED (3 commits).** Auto-title was already implemented (not a gap).

**Architecture:** One backend endpoint for search (ILIKE across user's chat_messages joined to chat_sessions/agent_conversations), token-based public share (new ChatShare model + public route), frontend-only export (client-side markdown), frontend-only notifications (browser Notification API), frontend-only tabs (state in Chat.jsx over the existing selectSession path).

**Tech Stack:** FastAPI + SQLAlchemy (backend), React + vitest (frontend), alembic for the ChatShare migration.

**Scope note:** Auto-title is ALREADY IMPLEMENTED (Chat.jsx:2074 derives title from first user message — `text.trim().replace(/\s+/g,' ').slice(0,60)`). NOT in this plan.

---

## F1 — Global chat-history search (P2)

### Task 1.1: Backend search endpoint (TDD)

**Objective:** `GET /api/apps/{app_id}/chat/search?q=...&limit=20` returns the caller's sessions whose messages contain q, with snippets.

**Files:**
- Create: `backend/app/routers/chat_search.py`
- Register router in `backend/app/main.py` (include_router pattern)
- Test: `backend/tests/test_chat_search.py`

**Key design:**
- Auth: `Depends(get_current_user_required)` + `Depends(get_db)` (pattern: agents.py:5764).
- Scope: `chat_sessions.created_by_id == user.id`, `is_deleted == False` (TimestampedBase soft-delete), join `chat_messages` on `session_id`, `content ILIKE %q%` with `%`/`_` escaped (`q.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')`), escape clause `'\\'` — Postgres default LIKE escape.
- Group by session; snippet = 120 chars around first match (prepend `…` / append `…`).
- Return: `{query, results: [{session_id, title, agent_name, project_name, last_message_at, matches: [{role, snippet, created_date}]}]}`.
- Empty/whitespace q → 400. `limit` clamped 1..50.
- Tenant filter: follow `entity_service._tenant_filters` convention if org-scoping is active; at minimum user-scoping.

**Verify:** `docker restart zhanlu-backend`; pytest suite; curl smoke with login token.

### Task 1.2: Sidebar search box (TDD)

**Objective:** Search box in SessionList; results replace the session groups while active; click opens the session.

**Files:**
- Modify: `frontend/src/components/chat/SessionList.jsx`
- Test: `frontend/src/components/chat/SessionList.search.test.jsx` (source-text + behavior)

**Key design:**
- Input above session groups: `data-testid="session-search-input"`, search icon, clear button.
- Debounce 250ms; call `base44.entities`-style fetch — add `chatSearch(q)` helper in `frontend/src/api/agentEnhanced.js` hitting the new endpoint.
- While `searchQuery` non-empty: render results list (session title + agent badge + snippet), click → `selectSession(result.session_id)` → clear search.
- Empty result state: "No conversations found".
- i18n keys in `translations.js` (en + zh).

---

## F2 — Conversation export + share link (P2)

### Task 2.1: Client-side markdown export

**Objective:** "Export as Markdown" in the session menu downloads `<title>.md` with full conversation.

**Files:**
- Create: `frontend/src/lib/exportConversation.js` — pure builder `buildConversationMarkdown(session, messages)` (role labels, content, attachment names, agent, date).
- Modify: `frontend/src/components/chat/SessionList.jsx` (menu item) — fetch messages via `base44.entities.ChatMessage.filter({session_id}, 'created_date', 200)` then Blob download.
- Test: `frontend/src/lib/exportConversation.test.js` + contract test on the menu item.

### Task 2.2: ChatShare model + migration

**Objective:** Persist share tokens.

**Files:**
- Create: `backend/app/models/chat_share.py` — `ChatShare(TimestampedBase)`: `session_id` FK chat_sessions (ON DELETE CASCADE), `token` String(64) unique indexed, `expires_at` DateTime nullable.
- Create: `backend/alembic/versions/082_chat_shares.py` (down_revision `081_chat_messages_attachments`) — auto-applies on container restart (prestart.sh runs alembic upgrade head).

### Task 2.3: Share API (TDD)

**Objective:** POST/DELETE share management + public read-only data route.

**Files:**
- Modify: `backend/app/routers/chat_search.py` (rename module scope to chat tools) or new `backend/app/routers/chat_shares.py`
- Test: `backend/tests/test_chat_shares.py`

**Key design:**
- `POST /api/apps/{app_id}/chat/shares` `{session_id}` → creates token (uuid4().hex), returns `{token, share_url: "/share/c/<token>", created_date}`. Reuses existing non-expired token for the same session+owner.
- `DELETE /api/apps/{app_id}/chat/shares/{session_id}` → revoke (owner only).
- `GET /share/c/{token}/data` (public, NO auth) → `{session_title, created_date, messages: [{role, content, created_date}]}` or 404/410.
- `GET /share/c/{token}` (public) → minimal standalone HTML page (dark-friendly, zh/en meta) that fetches the data route and renders read-only; no scripts beyond inline fetch. Pattern: public.py.

**Verify:** restart backend; create share → curl data route without token → messages; revoke → 404.

### Task 2.4: Share UI (TDD)

**Objective:** "Share" + "Copy link" in session menu; toast with link.

**Files:**
- Modify: `frontend/src/components/chat/SessionList.jsx` (menu item), `frontend/src/api/agentEnhanced.js` (chatShare/chatRevokeShare helpers)
- Test: contract test on menu items + helper tests.

---

## F3 — Completion notification (P3)

### Task 3.1: Pure notification helper (TDD)

**Objective:** Testable decision logic.

**Files:**
- Create: `frontend/src/lib/completionNotify.js` — `shouldNotify({hidden, permission, hasFinalMessage})`, `notificationBody(finalMessage, maxLen=200)`.
- Test: `frontend/src/lib/completionNotify.test.js`.

### Task 3.2: Wire into Chat.jsx

**Objective:** Notify once per run when the tab is hidden and the run completes.

**Files:**
- Modify: `frontend/src/pages/Chat.jsx` — central `onRunComplete(aiMsg)` called at the stream-completion sites (setStreamingId(null) at ~1434, 1961, 1970, 1992, 2668); request `Notification.requestPermission()` once on first send (user gesture); `notifiedRunIds` ref dedupes.
- Guard: only when `document.hidden && Notification.permission === 'granted' && final AI message exists`.
- Test: contract test (Chat.jsx imports helper + permission request on send).

---

## F4 — Parallel session tabs (P3) — REMOVED 2026-08-31 (user decision: not needed; all code + tests deleted)

### Task 4.1: SessionTabs component (TDD)

**Objective:** Tab bar above messages: open tabs, active highlight, close, overflow.

**Files:**
- Create: `frontend/src/components/chat/SessionTabs.jsx` — props `{tabs, activeId, onSelect, onClose}`; `data-testid="session-tab-*"`; max 8 tabs, horizontal scroll overflow.
- Test: `frontend/src/components/chat/SessionTabs.test.jsx`.

### Task 4.2: Wire into Chat.jsx

**Objective:** Tabs state + integration with selectSession/new-task.

**Files:**
- Modify: `frontend/src/pages/Chat.jsx`
- Design: `tabs` state `[{sessionId, title}]`; `openTab(sessionId, title)` dedupes; on session select / new task → openTab; close removes tab (active falls back to neighbor or landing); draft per tab preserved in a `draftsRef` map keyed by sessionId (restored on switch). Tab title updates from SessionList data on select.
- Test: `frontend/src/pages/Chat.sessionTabs.test.jsx`.

---

## Execution order + verification

1. F1 (search) — backend endpoint → tests → frontend box → tests → restart + curl + browser smoke.
2. F2 (export/share) — export first (no backend) → share model/migration → API → UI → restart + curl smoke.
3. F3 (notify) — pure helper → wiring → tests.
4. F4 (tabs) — REMOVED by user decision before completion.
5. Full regression: `npx vitest run` chat suite + backend pytest sweep; `docker restart zhanlu-backend`; live browser pass on 8088.

## Pitfalls (from skill references)

- Backend has no --reload: restart the container after backend edits.
- Frontend ships via `npm run build` (dist bind-mounted into nginx) — no compose up needed.
- Generic entity CRUD drops undeclared columns — ChatShare columns MUST be declared in the model (migration + model together).
- Source-text contract tests: generous regex caps, `\}\s*<\/button>` not `}</button>`.
- SessionList fetch cap is 200 rows; search endpoint is DB-side, not client-filtered.
