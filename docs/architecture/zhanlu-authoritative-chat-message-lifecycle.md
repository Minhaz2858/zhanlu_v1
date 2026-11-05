# Authoritative Chat Message Lifecycle (D4) — Architecture

**Date:** 2026-11-01
**Branch:** `fix/automation-chat-artifacts-ordering` (uncommitted, no commits per mission constraint)
**Predecessor:** `zhanlu-chat-execution-consistency-root-cause-audit.md` (D1–D3 fixed there; D4 fix is this document)
**Classification:** INTEGRATION-TESTED (deterministic TDD; zero paid LLM credits)

---

## 1. Root cause (confirmed defect)

On the FSM chat route (`POST /apps/{a}/agents/conversations/v3/{c}/messages/stream`, FSM
branch), one user turn produced **two assistant records**:

1. The frontend creates an optimistic assistant `chat_messages` row via the entity API
   *before* streaming (`Chat.jsx` ~1501–1510). That id is the only one backed by a real
   row at stream time, and it is transported to the backend as `assistant_message_id`
   (Stage 2B Commit 4A — transport + validation `^[0-9A-Za-z_-]{8,64}$` was already
   implemented at ~`agents.py:12295` and recorded in `_assistant_message_id`).
2. `_persist_fsm_turn` then **INSERTed a second row with a fresh `uuid4()`**, ignoring the
   transported identity entirely (Commit 3 — consumption — was never finished).

Result per FSM turn: an empty client row + a filled backend row (the "twin bubble" /
duplicate-record symptom), plus a third identity in `agent_conversations.messages`.
Because the FSM `done` payload carried no `answer_persisted` attestation, the client
also unconditionally re-PUT content to its own optimistic row — two writers, one truth.

## 2. Fix: one turn → exactly one authoritative assistant message

**Principle:** the backend reconciles with client identity; it does not blindly insert.
No migration, no second ID system, fully backward compatible.

### Backend — `_persist_fsm_turn` (`backend/app/routers/agents.py:1027`)

Signature: `client_message_id: str | None = None`; return changed
`list | None` → `dict | None` = `{"messages", "assistant_message_id", "answer_persisted"}`.

Finalization rules (single transaction, fresh session bound to `db_bind`):

| Case | Behavior |
|---|---|
| Client row exists (validated: `id == client_message_id` AND `session_id` matches AND `role == 'assistant'` AND not deleted) | **Update in place**: `content`, `trace`, `live_events`, `sources` written onto the client's row; `reasoning_content=None`; `answer_persisted=True` |
| No `client_message_id` (old client), row missing, or row belongs to a foreign session | **Legacy INSERT** (fresh uuid) — a cross-session row is never adopted; `answer_persisted=True` |
| `regenerate=True` | **No chat_messages write** — the client's optimistic row is the writer of record (unchanged contract) |
| Conversation row vanished | Return `None` (Stage-1 contract preserved) |
| Commit fails | Session closed without commit → **both stores roll back atomically** |

`agent_conversations.messages` is merged via `_merge_fsm_conv_messages` carrying the
**same authoritative id** (`_assistant_msg` id = `_assistant_message_id or uuid4()`).

Call site (~13327): `_persist_fsm_turn(..., client_message_id=_assistant_message_id)`.
Done payload (~13404): when `_fsm_persist is not None and not _regenerate`, `done`
carries `answer_persisted` + `assistant_message_id`.

### SSE attestation contract

`done` → `{..., answer_persisted: bool, assistant_message_id: string}`.
The frontend's existing handlers (`answer_persisted` check at `Chat.jsx` ~2634;
`backendOwnsContent()` in `answerLifecycle.js:169` at ~2839) then relinquish the
client's content write — the backend becomes the single writer of record. **No frontend
identity changes were required**; the identity pipeline was ~80% pre-built (4A
transport) and only backend consumption was missing.

## 3. Persistence guarantees

- **Exactly one row per turn** (when the client transports identity): exact-row
  finalization is idempotent — re-persisting the same turn rewrites the same values
  onto the same row.
- **Atomicity:** chat_messages row + conv.messages JSON merge commit or roll back
  together (fresh session, no nested commits).
- **Tenant/session safety:** a `client_message_id` pointing at another session's row is
  never adopted; the backend falls back to legacy INSERT.
- **Multi-turn:** each turn finalizes its own row; history shows no twins.
- **Dual-store note:** the backend never writes USER rows to `chat_messages` (client
  creates them via entity API); the user-message repair covers the
  `agent_conversations.messages` JSON store only — by design.

## 4. D5 — empty `content_replace` / collapse-regenerate

**Root cause (deterministic, reproduced in vitest):** server retries emit
`content_reset` / `content_replace ""`. The frontend correctly rejected the *empty*
replace (invariant I1) but had **no `content_reset` handler**, so subsequent deltas
appended onto stale pre-retry content — transient doubling; the final `done` frame
self-healed via server-wins, which made the symptom flicker then correct itself.

**Fix (minimal, no retry-semantics change):**
- `answerLifecycle.js`: new pure helper
  `applyDelta(previous, delta, pendingReset=false)` — when `pendingReset` is true the
  next delta *replaces* content instead of appending.
- `Chat.jsx`: `let pendingReset = false` near the stream accumulators; new
  `evt.type === 'content_reset'` branch sets `pendingReset = true`; delta branch uses
  `applyDelta`; content_replace branch — rejected empty replace sets
  `pendingReset = evt.content === ''`, accepted replace clears it.

Invariant I1 (empty replace never clobbers content) is preserved; the reset *marker*
now carries state forward to the first real delta instead of being dropped.

## 5. Out of scope / known limitations

- **v2 blocking route** (~`agents.py:7360`) has its own `_assistant_msg` (uuid,
  conv.messages only, no chat_messages twin) — different contract, documented, not
  modified.
- **Legacy identity split:** old clients that never transport
  `assistant_message_id` still get the backend-INSERT uuid in the DB while the client
  holds its optimistic id until `done` reconciliation — unchanged legacy behavior.
- **Pre-existing failure (stash-verified, not caused by this work):**
  `tests/test_v3_fsm_stream.py::test_fsm_in_sse_path_emits_fsm_state_events_unit`
  asserts a stale source string
  (`_sse_with_heartbeat(_disconnect_safe_stream(_fsm_event_stream))`) that the
  stable-stream M1 refactor removed.
- **Pre-existing frontend failures (stash-verified for `test_steer_wiring`; the other
  five files do not touch any file changed here):** `Sidebar.test.jsx` (19),
  `previewToken.test.js` (1), `translations.keyConsistency.test.jsx` (1),
  `ProjectDetail.knowledgeMap.test.jsx` (2), `ProjectDetail.recentChatsFilter.test.jsx`
  (1), `test_steer_wiring.test.js` (4) — 27 total, identical without this mission's
  changes.

## 6. Test coverage (TDD, RED→GREEN)

**Backend — `backend/tests/test_fsm_message_identity.py` (NEW, 9 tests, sqlite
in-memory: ChatSession + ChatMessage + AgentConversation):**
client-row finalization in place; idempotent re-persist; legacy INSERT fallback (old
client); missing-row fallback; foreign-session guard (row never adopted);
regenerate (backend writes nothing); commit-failure rollback
(`monkeypatch.setattr("sqlalchemy.orm.Session.commit", _boom)`); done-payload source
flags; multi-turn history shows no twins.

RED→GREEN evidence: 8/9 failed before the fix, 9/9 passed after.
`test_fsm_persistence_stage1.py` updated for the new dict contract (12 tests green),
including the corrected contract `if _fsm_msgs is None: return None`.

**Frontend — `src/lib/answerLifecycle.test.js`:** 4 new D5 tests (repro: deltas after
rejected empty replace yield `"AB"` not `"ABAB"`; normal accumulation unchanged;
reset cleared by authoritative replace; non-string delta). File: 55/55 green.

**Regression:** focused backend sweep 160 passed (FSM / ReAct / persistence / streaming
/ tree suites, canonical order) + 49 persistence-contract tests = 50/50 except the one
pre-existing stale-assertion failure above. Frontend: 1033 passed / 27 pre-existing
(attributed, §5). Production build `npm run build`: exit 0 (9.59s).

## 7. Acceptance criteria status

| Criterion | Status |
|---|---|
| One user turn → exactly one authoritative assistant message (FSM) | ✅ exact-row finalization + attestation |
| No migration, no second ID system | ✅ reuses transported `assistant_message_id` |
| Backward compatible (old clients) | ✅ legacy INSERT fallback |
| Frontend reconciliation w/o hiding | ✅ none needed (attestation contract already honored) |
| D5 fix only if reproducible root cause | ✅ deterministic vitest repro, minimal fix |
| Tests RED→GREEN | ✅ 8 failed → 9 passed (backend); D5 repro → green (frontend) |
| Build passes | ✅ exit 0 |
| Zero paid LLM credits | ✅ all tests deterministic (sqlite in-memory / vitest) |
| Uncommitted work preserved | ✅ only mission files touched; stash A/B verified |
| No commits | ✅ working tree only |

**Final status: IMPLEMENTED / INTEGRATION-TESTED (deterministic).** Live UX pass not
performed (out of scope; deterministic evidence only).
