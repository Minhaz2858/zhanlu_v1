## 0. Executive summary (REV 2 — resolves U1/U2/U3 + full writer audit)

The O3 correction made `answer_persisted` **honest**; it did not make the row **safe**. Rev 2 answers the review
questions and — importantly — **withdraws a rev-1 implication that was wrong**.

### 0.1 What changed in rev 2

| Review question | Answer |
|---|---|
| **1. Can a stale client that omits `If-Match` bypass enforcement?** | **YES today.** An optional header is *not* enforcement. Rev 2 replaces it with a **server-enforced precondition on `ChatMessage.content` writes** that applies to **all four** entity write routes (§2). |
| **2. U2 — version token** | **Recommend B: a dedicated integer `version`.** `updated_date` is *unusable* as a hard token (§3.2 explains why: it is client-echoable via `_filter_data`). |
| **3. U3 — non-empty conflicting row** | Defined precisely: the backend may take the row **only via a validated conditional write** that proves it is *this turn's* successor, never a blind overwrite (§5). |
| **4. Writer audit** | **Four UPDATE-capable writer groups**, not one. Two of them are in `automation_executor.py` and were **missed in rev 1** (§4). |

### 0.2 The three hard findings

| # | Finding | Evidence |
|---|---|---|
| **X1** | **`updated_date` cannot be trusted as the CAS token.** `_filter_data` does **not** strip `updated_date`, so a client can `PUT {"updated_date": "…"}` and forge the precondition. | `entity_service.py:149` immutable set = `{id, created_by_id, org_id, app_id, resource_type}` — no timestamps (F26) |
| **X2** | **There are 4 entity write routes, and 3 of them accept content with no precondition**: `PUT /{id}`, `PUT /bulk`, `PATCH /update-many`. | `entities.py:424, 438, 495` (F23) |
| **X3** | **Automations issue unconditional ORM UPDATEs on existing assistant rows** (`existing_asst.content = cancel_line`; `_existing_asst_msg.content = assistant_text`), bypassing any client-side protocol. | `automation_executor.py:1734, 2888` (F22) |

**Therefore the honest status is: the race is NOT closed, and cannot be closed by an optional header.** Closing it
requires (a) a server-enforced, non-forgeable version token, and (b) coverage of every content-writing route.

---

## 1. Verified facts, assumptions, recommendations

### 1.1 Verified facts (read or executed this session)

| # | Fact | Evidence |
|---|---|---|
| F1 | `chat_messages` PK = `id`; **no other unique constraint** | `pg_constraint`, `pg_indexes` |
| F2 | **No foreign keys at all**; `session_id` is unvalidated `varchar(36)` | `pg_constraint contype='f'` empty |
| F3 | Isolation **`READ COMMITTED`** | `SHOW transaction_isolation` |
| F4 | Backend role **`zhanlu_app`**, `rolbypassrls = false` | live query |
| F5 | RLS enabled + forced on `chat_messages`, `chat_sessions`, `agent_conversations` | `pg_class` |
| F6 | Cross-tenant UPDATE silently affects **0 rows** (no error) | verified earlier session |
| F7 | Client `order` = in-memory array index | `Chat.jsx:1323,1456,1679,1932,1939,2067` |
| F8 | Automation `order` = epoch seconds | `automation_executor.py:3103-3109` |
| F9 | Automations attach to the user's **most recent ChatSession** — same session a chat uses | `automation_executor.py:3076-3086` |
| F10 | **`ORDER BY order DESC` mis-targets** when an automation row is present | executed probe |
| F11 | **Stale client PUT overwrites committed canonical content** | executed probe |
| F12 | `update_record` (`:505-523`) is blind: `SELECT` → mutate → `commit()`. **No precondition support** | read |
| F13 | `ChatMessage` **is** user-scoped on PUT (`created_by_id == user.id`) | `entities.py:198-235` |
| F14 | `updated_date` is microsecond-precision `timestamp`, serialized via `to_dict().isoformat()` (`base.py:67-68`) | `information_schema`, `base.py` |
| F15 | Backend CAS → `rowcount 0`, refuses to clobber | executed probe |
| F16 | **Client CAS** (`WHERE content=''`) refuses the stale write after backend commit; canonical survives | executed probe |
| F17 | `rowcount == 0` for a missing row | executed probe |
| F20 | ReAct persistence block `agents.py:19742-19809`; `answer_persisted` at `:19772`, `:19785`; published `:20228` | read |
| F22 | **Automation UPDATE sites (2)**: `automation_executor.py:1734` (cancel line), `:2888` (final reply) | grep + read |
| F23 | **Entity write routes (4)**: `POST ""` `:366`; `PUT /bulk` `:424`; `PATCH /update-many` `:438`; `PUT /{record_id}` `:495` | read |
| F24 | `PUT /bulk` and `PATCH /update-many` accept `content` with **no precondition**; `update_many` supports `$set` via `_filter_data` (`:672-678`) | read |
| F25 | **No raw SQL writes** to `chat_messages` anywhere in `app/` | grep → empty |
| F26 | **`_filter_data` does NOT strip `updated_date`** (`_IMMUTABLE_FIELDS = {id, created_by_id, org_id, app_id, resource_type}`) | `entity_service.py:149` |
| F27 | `onupdate=` fires on ORM updates (microsecond bump verified) | executed probe |
| F28 | No NULL `updated_date`; no duplicate timestamps across 497 rows | SQL |
| F29 | Client writers of `ChatMessage`: `Chat.jsx:2800` and `mobile/MobileChatPage.jsx:294` | grep |
| F30 | DB-level enforcement without DDL (trigger/constraint) is **not available** in this stage | analysis |

### 1.2 Assumptions

- **A1** The client can send a precondition header (web is a SPA we control; mobile is a separate bundle).
- **A2** Entity PUTs for *other* models must keep working unchanged (blast-radius control).

### 1.3 Recommendations

- **R1** Enforce the precondition **server-side for `ChatMessage.content` only**, on **all four** routes (§2).
- **R2** Use a **dedicated integer `version`** (option B), **not** `updated_date` (§3).
- **R3** Backend takes a conflicting row **only** through a validated conditional write (§5).
- **R4** Keep the flag OFF until every listed writer participates (§8).

---

## 2. Enforceability: making the precondition unbypassable (review Q1)

### 2.1 The rev-1 mistake

Rev 1 proposed an **optional** `If-Match` header. That is **not enforcement**: a stale tab, a cached bundle, the mobile
bundle, or an automation simply omits it and overwrites canonical content. **Rev 2 states plainly: with an optional
header, the race remains open.**

### 2.2 Server-enforced policy (the actual mechanism)

A single guard is applied at the **service layer**, so every route inherits it:

```
On ANY write that would change ChatMessage.content:
    require a valid precondition token, UNLESS the write is a create of a NEW row.
    Missing / malformed / stale token  ->  428 Precondition Required  or  409 Conflict
```

Applied to **all four write routes** (F23/F24):

| Route | Content-writable? | Enforcement |
|---|---|---|
| `PUT /entities/ChatMessage/{id}` | yes | precondition **required** |
| `PUT /entities/ChatMessage/bulk` | yes (per item) | precondition **required per item** |
| `PATCH /entities/ChatMessage/update-many` | yes via `$set` | **rejected outright** when `$set`/`$inc` touches `content` with >1 matched row, and precondition required when exactly 1 |
| `POST /entities/ChatMessage` | create only | no precondition (new row) |

**Scope of enforcement is keyed on `(model == ChatMessage, field == content)`.** Every other entity and every other
`ChatMessage` field (tool_calls, artifacts, sources, phase, order…) keeps today's unconditional behaviour. This is
what keeps the blast radius small while still being *real* enforcement.

**Why a deny-by-default on the `content` field is safe for old clients:** a stale client that omits the token gets a
`409` and **writes nothing** — the canonical row survives. That is the correct failure mode: *refuse*, never *clobber*.
The client already tolerates failed writes (`Chat.jsx` `catch { /* best-effort persist */ }`), so legacy behaviour
degrades to "answer shown, not re-saved", which is exactly today's semantics on error paths.

### 2.3 Rollout / enforcement matrix per writer class

| Writer | Sends token? | Behaviour after enforcement | Acceptable? |
|---|---|---|---|
| **Updated web client** | yes | normal writes; `409` on staleness → re-read | ✅ target state |
| **Old browser tab / cached bundle** | no | content write **rejected 409**, answer still displayed from memory, `conv.messages` still persisted server-side | ✅ **no clobber** (this is the point) |
| **Mobile (current bundle)** | no (until parity) | same as old tab | ⚠️ must ship parity before/with enablement (§8) |
| **Automation executor** | no | its UPDATEs are **server-internal** (ORM, same process) and must be **exempted** by an internal-only marker — see §2.4 | ⚠️ exemption must be explicit, not accidental |
| **Legacy entity-API callers (3rd party)** | no | 409 on content writes | ✅ safe; documented as a breaking change for that field only |
| **FSM route** | n/a (inserts) | unaffected | ✅ |

### 2.4 The automation exemption — and why it is defensible

Automations run **in-process**, own the rows they create, and write distinct fields of *their own* assistant bubble
(`:2888` updates the pre-created placeholder the run itself made). They are not racing a chat turn on the same row.
The design exempts them via an **explicit internal flag** (e.g. a contextvar set by the executor) — **not** by leaving
the route unguarded. If the flag is absent, the guard applies.

> **Honest caveat:** because automations share the *session* (F9) but not the *row*, they cannot clobber a chat answer
> under exact-id targeting (§2 of rev 1, V4). Their exemption is therefore low-risk **provided** identity is
> exact-id and never `order`-based (F10).

---

## 3. U2 — Version token: `updated_date` (A) vs integer `version` (B)

### 3.1 Evaluation

| Criterion | **A. `updated_date`** | **B. integer `version`** |
|---|---|---|
| Non-forgeable | ❌ **NO** — `_filter_data` lets a client PUT `updated_date` (F26), forging the token | ✅ clients cannot set it (`_filter_data` strips non-client-writable cols; version is server-managed) |
| Monotonic guarantee | ⚠️ wall-clock; two writers in one microsecond collide (not observed in 497 rows, but not *guaranteed*) | ✅ strictly monotonic per row |
| Migration | none | **ADD column + backfill** (DDL) |
| Backfill | n/a | `version = 0` or `1` for all existing rows; safe (no reader depends on it today) |
| Client round-trip | ISO string, precision-sensitive across JS `Date` (ms!) | plain integer, lossless in JSON |
| **JS precision issue** | **⚠️ real**: `to_dict` emits µs ISO; JS `Date` truncates to ms, so the echo may not match | none |
| All 4 writer paths | each must bump it; ORM `onupdate` handles ORM writes (F27) but **not** a client-supplied value | each must `version = version + 1`; a single SQL `SET version = version + 1` is atomic regardless |
| Rollback | revert code; column absent | revert code; column inert (nullable/default) — no destructive rollback |

### 3.2 Why A is rejected

Two independent reasons:

1. **Forgeable.** A client can include `updated_date` in the PUT body; `_filter_data` passes it (F26). A malicious or
   merely stale client could therefore *satisfy* a precondition it should fail. A concurrency token that the
   loser can set is not a token.
2. **Precision.** JS `Date` is millisecond-resolution; `updated_date` is µs (F14). A round-tripped token can mismatch
   even when nothing changed → spurious `409`s, which would push clients back toward "retry unconditionally".

### 3.3 Recommendation: **Option B — integer `version`**

- `chat_messages.version INTEGER NOT NULL DEFAULT 0` (or `SMALLINT`)
- Every content mutation: `SET version = version + 1` (atomic, server-side)
- Precondition: `If-Match: <version>`; success iff `WHERE id=:id AND version=:expected`
- Client receives `version` in the entity payload (additive) and echoes it

**This requires a migration.** Per the brief I am **not** performing it — this is a **decision request** (§11 U-M1).

**Fallback if migrations are unavailable this quarter:** use **A** *only* with `updated_date` removed from
client-writable fields (`_IMMUTABLE_FIELDS |= {"updated_date"}` — a code change, no DDL) and accept the ms-precision
risk by normalizing comparisons to millisecond truncation. Documented as inferior: it weakens collision resistance.

---

## 4. Writer audit (review Q4)

| # | Writer group | File(s) | Operation | Touches existing row? | Content-writable | Participates in CAS? |
|---|---|---|---|---|---|---|
| **W1** | Web client | `Chat.jsx:2800` | `PUT /{id}` | ✅ | ✅ | must send token |
| **W2** | Mobile client | `MobileChatPage.jsx:294` | `PUT /{id}` | ✅ | ✅ | must send token (parity) |
| **W3** | ReAct backend | `agents.py:19770` | ORM update | ✅ | ✅ | **server-side CAS** |
| **W4a** | Automation — cancel line | `automation_executor.py:1734` | ORM update | ✅ | ✅ | **exempt (internal)** |
| **W4b** | Automation — final reply | `automation_executor.py:2888` | ORM update | ✅ | ✅ | **exempt (internal)** |
| **W5** | Automation — inserts | `automation_executor.py:1747,2851,2900,2999,3027,3103` | INSERT | ❌ new rows | n/a | n/a |
| **W6** | FSM route | `agents.py:12719` | INSERT | ❌ | n/a | n/a |
| **W7** | Bulk entity routes | `entities.py:424,438` | `PUT /bulk`, `PATCH /update-many` | ✅ | ✅ | **must be guarded (§2.2)** |

**Bypass assessment**
- **No raw SQL** anywhere (F25) → ORM `onupdate` is reliable, but `onupdate` is **not** the mechanism chosen (B uses an
  explicit increment), so this is moot for correctness.
- **W7 was missed in rev 1** and is the most serious gap: without guarding bulk routes, the precondition is
  trivially bypassed.
- **W4a/W4b** are the only writers that legitimately *should* bypass the client protocol; they are exempted explicitly.

### 4.2 Unified writer-by-writer compatibility & rollout matrix

| Writer | Route / mechanism | Sends token after stage 2? | Enforcement result | Rollout action | Residual risk |
|---|---|---|---|---|---|
| **W1 web client** | `PUT /ChatMessage/{id}` | ✅ yes | allowed; `409` only if stale | ship token echo in stage 1 | none after stage 2 |
| **W2 mobile client** | `PUT /ChatMessage/{id}` | ✅ yes (parity release) | allowed; `409` if stale | **must ship before** stage 2 | 409s during stage 2 if parity lags |
| **W3 ReAct backend** | ORM update + CAS | n/a (server) | via `version` CAS | enable with flag | none |
| **W4a/W4b automations** | ORM update (internal) | ❌ exempt | allowed via internal flag | none | exempted hole (K3) — safe only with exact-id identity |
| **W5 automation inserts** | INSERT | n/a | allowed (new row) | none | none |
| **W6 FSM insert** | INSERT | n/a | allowed | none | none |
| **W7 bulk routes** | `PUT /bulk`, `PATCH /update-many` | guard required | **rejected** if `content` touched without token | part of stage 2 | none once guarded |
| **Old tab / cached bundle** | `PUT /ChatMessage/{id}` | ❌ no | **409**, no write | none (acceptable) | answer displayed, not re-saved — matches today's error semantics |
| **3rd-party API callers** | `PUT /ChatMessage/{id}` | ❌ no | **409** on `content` only | document in release notes | breaking for that field only |

**Reading of the table:** after stage 2 the only writers that can change `content` on an existing row are those that
either (a) present a current `version`, or (b) are the explicitly-exempted in-process automation paths. **Every
unconditional-overwrite path is closed.** Before stage 2, nothing changes.

---

## 5. U3 — Conflict resolution (review Q3)

### 5.1 The rule

> The backend **never** overwrites a different non-empty row blindly.
> It may replace it **only** through a conditional write that proves it is this turn's legitimate successor.

### 5.2 The successor proof

The backend holds three facts the client does not: (a) the exact validated id (V1–V6), (b) the `version` it read, and
(c) the turn's own canonical content. It may displace a conflicting row **iff all three hold**:

| Condition | Rationale |
|---|---|
| The row's `content` == the value the backend itself read at persist time (CAS on `content` **and** `version`) | someone else changed it → we lose the race legitimately |
| The turn is the **current** generation (no later turn started for this conversation) | prevents a stale generation winning |
| The row is **not** owned by another turn's canonical answer (not `answer_persisted:true` from a *different* turn_id) | prevents cross-turn displacement |

If any condition fails → **skip**, report `conflict`.

### 5.3 Case analysis

| Case | Who committed first | Backend action | Client action | Final row |
|---|---|---|---|---|
| **B1** Backend first | backend | CAS succeeds (row empty) | token stale → `409`, no write | **canonical** ✅ |
| **B2** Client first | client | row non-empty ≠ canonical → **conflict**, skip | — | client text |
| **B3** Both race | one wins CAS | loser `409`/conflict | loser re-reads | one winner, no clobber ✅ |
| **B4** Terminal lost | backend may have committed | — | client has no `answer_persisted`; writes **with token**; if backend committed, token stale → `409` | **canonical** ✅ |
| **B5** Regenerate | row holds **old** answer | **successor proof** applies (same row, backend read the old value, generation current) → CAS replace allowed | client token stale → `409` | **new canonical** ✅ |
| **B6** Ambiguous commit | unknown | re-read by id; if `content == canonical` → `already_present` | `409` (stale token) | canonical |

**B5 is the case that motivated U3**, and it is exactly where the rev-1 behaviour (skip) left the DB holding the old
answer. With the successor proof + CAS it becomes a *validated* replacement rather than a blind one.

---

## 6. Persistence status contract (unchanged from rev 1, plus token fields)

`answer_persisted` remains **`true` ⇔ this turn's canonical content was verified present in the exact validated row at
commit time**. Additive companion `persist_status`:

`committed` | `already_present` | `no_identity` | `conflict` | `forbidden` | `failed` | `unknown`

`answer_persisted: true` only for `committed` and `already_present`. The terminal frame additionally carries
`answer_version` (the row's version after commit) so the client can write with a **current** token when it still owns
the answer.

---

## 7. Concurrency regression matrix (real PostgreSQL, as `zhanlu_app`)

| # | Test | Expected |
|---|---|---|
| P1 | Exact id targets the correct row with an automation row present | turn row changes; automation row untouched (kills F10) |
| P2 | Foreign tenant/session/role/owner | rejected; rows untouched |
| P3 | No-id legacy request | `skipped_no_id`; no write |
| P4 | Client PUT **without** token after canonical commit | **409**, canonical intact (Q1 core) |
| P5 | Client PUT **with stale** token | 409, canonical intact |
| P6 | Client PUT with current token | write succeeds |
| P7 | `PUT /bulk` containing `content` without token | **rejected** |
| P8 | `PATCH /update-many` `$set:{content}` | **rejected** |
| P9 | Automation internal update | succeeds (exempt) |
| P10 | Concurrent finalization | idempotent; one canonical row |
| P11 | Ambiguous commit | re-read → `already_present` |
| P12 | RLS-invisible / deleted row | `failed(not_found)` |
| P13 | Regenerate successor proof | CAS replace allowed → new canonical |
| P14 | Lost terminal event | client writes with token → 409 if backend committed; canonical preserved |
| P15 | Token forged via body (`updated_date`/`version` in payload) | **ignored** (server-managed field) |
| P16 | Stage 0/1/O3/O3-correction suites | unchanged |
| P17 | NL2SQL/grounding/result_contract | untouched |

**Runner note:** `backend/tests/conftest.py:10` forces SQLite; these tests need a dedicated Postgres-marked suite
that bypasses that override, asserting `current_user='zhanlu_app'` and `rolbypassrls=false` in-test.

---

## 8. Backward compatibility, rollout, rollback

### 8.1 The compatibility truth

Enforcing a precondition on `ChatMessage.content` **is a behaviour change** for any writer that does not send a token.
It is *safe* (failures refuse to write, never clobber) but it *is* a change. Stated plainly rather than called
"backward compatible".

| Stage | Action | Gate |
|---|---|---|
| 0 | Ship version column + `version` in entity payload (additive) | migration approved |
| 1 | Ship client token echo (web **and** mobile) | clients deployed |
| 2 | Enable **enforcement flag** (`CHAT_MESSAGE_CONTENT_PRECONDITION`) | client telemetry shows token present on ≥99% of writes |
| 3 | Remove flag | — |

Until stage 2, behaviour is identical to today.

### 8.2 Rollback

Flip the flag off → enforcement disabled → exactly today's semantics. The `version` column stays (inert, nullable/
defaulted) and requires no destructive rollback.

---

## 9. Migration requirements

**Option B requires one migration:**

```sql
ALTER TABLE chat_messages ADD COLUMN version INTEGER NOT NULL DEFAULT 0;
```

- Additive, nullable-free via DEFAULT, reversible (`DROP COLUMN`).
- Backfill is implicit (`DEFAULT 0` for all 497 existing rows).
- No index required (the PK predicate carries selectivity).
- **Not performed in this stage** — this is decision **U-M1**.

---

## 10. Risks, rollback, unresolved questions

### 10.1 Risks

- **K1** Enforcement on `content` breaks non-participating writers **by design** (409). Must be staged (§8).
- **K2** Bulk routes (W7) are the easiest bypass — must be covered in the same change, not later.
- **K3** The automation exemption (§2.4) is a deliberate hole; it is safe only while identity is exact-id.
- **K4** Mobile parity lag would make mobile writes fail with 409 during stage 2.
- **K5** A `409` on a client write leaves the bubble's metadata (tool_calls/artifacts) unwritten too, unless the guard
  is field-scoped (design: guard **only** `content`, metadata still writes).

### 10.2 Unresolved decisions requiring explicit approval

| ID | Decision | Recommendation |
|---|---|---|
| **U-M1 (blocking)** | Approve the **`version` column migration** (Option B)? If not, accept Option A with `updated_date` made client-immutable + ms-normalized (weaker). | **Approve B** |
| **U-E1 (blocking)** | Approve **server-enforced** precondition on `ChatMessage.content` (deny-by-default, 409), including the bulk-route guards? | **Approve** |
| **U-W1** | Approve the **automation exemption** via internal flag? | Approve |
| **U-R1** | Should the backend **take** a conflicting non-empty row under the successor proof (B5), or keep deferring to the client? | **Take it** (fixes the regenerate gap) |
| **U-C1** | Ship mobile parity **before** stage 2 or accept mobile 409s? | Before |
| **U-X1** | Is a `409` surfaced to the user, or silent? | Silent + log |

---

## 11. Approval boundary

Design only. No code, tests, migrations, commits, deploys, or restarts. Stage 0/1/O3/O3-correction and all
pre-existing dirty files untouched.

**With an optional header the race is NOT closed — I will not claim otherwise.** Implementation requires U-M1 and
U-E1.


---

## Appendix A — Identity contract and interleaving table (from rev 1, still current)

#### A.1 Identity and authorization contract

##### A.1.1 How the id is generated and when it becomes available

Today: `ChatMessage.create({... content: ''})` is an **entity-API create**, so the **server** assigns the UUID
(`base.py:27-29`) and returns it; the client stores it as `aiMsg.id` (`Chat.jsx:1455`). It is therefore available
**immediately after the create returns**, before the stream starts.

##### A.1.2 Transport

Additive, optional field on the existing stream request:

```jsonc
POST /apps/{app_id}/agents/conversations/v3/{conversation_id}/messages/stream
{ "content": "…",
  "assistant_message_id": "<aiMsg.id>",     // NEW, optional
  "turn_id": "<client uuid, optional>" }     // NEW, optional, correlation only
```

Read directly from `body`; never persisted via the entity API (F12/F13 filtering does not apply).

##### A.1.3 Validation — possession is not authorization

`chat_messages` has **no FK** (F2), so an id proves nothing. Six checks, in order:

| # | Check | Rule | On failure |
|---|---|---|---|
| V1 | Syntax | `^[0-9A-Za-z-]{8,64}$` | `skipped_no_id` |
| V2 | Exists **within the caller's tenant** (RLS, F5/F6) | `SELECT … WHERE id=:id` | `failed(not_found)` |
| V3 | Role | `row.role == 'assistant'` | `failed(wrong_role)` |
| V4 | **Conversation binding** | `row.session_id == chat_session_id` **and** `chat_sessions.conversation_id == conversation_id` | `failed(wrong_session)` |
| V5 | Owner | `row.created_by_id in (None, str(user.id))` | `failed(wrong_owner)` |
| V6 | Org | `row.org_id == effective_org` (Python + RLS) | invisible at V2 |

**V4 is the anti-mis-target guarantee.** It replaces `ORDER BY order DESC` (F10) with *"the row the client created for
this conversation"*. Even if an automation row shares the session, it cannot satisfy V4 **unless** it is the row the
client named — and the client names only its own.

##### A.1.4 Anti-forgery

A client cannot name another conversation's row: V4 requires the session→conversation chain to match the conversation
in the URL path, which itself is authorized at `agents.py:11604-11611`. A cross-tenant id is invisible (V2). A
same-tenant, same-user, **different-conversation** id fails V4. **No id ⇒ no write.**

##### A.1.5 Legacy clients, automations, regeneration, stale turns

| Caller | `assistant_message_id` | Behaviour |
|---|---|---|
| Legacy web client | absent | **skip** backend persistence (`skipped_no_id`); client keeps writing (today's behaviour) |
| Automation executor | absent (it writes its own rows) | skip; its rows are untouched |
| FSM route | n/a — writes its own row | unchanged |
| **Regenerate** | **present** — the reused row's id (`Chat.jsx:2833` copies `targetMsg`) | exact id = the regenerated row; V1–V6 apply. The row legitimately holds the **old** answer ⇒ conflict rules (§3) decide, not `order` |
| Stale turn (superseded `gen`) | present | the **client** refuses its own write (`isCurrentGeneration`, existing Stage 1 rule); the backend has no concept of `gen` — see §4.4 |

---

#### A.2 Interleaving table (expected row states)

`canon` = this turn's canonical answer; `row₀` = state before the turn.

| # | Interleaving | Mechanism | Expected final row | Stale wins? |
|---|---|---|---|---|
| C1 | Backend writes first; client writes later | client CAS on stale token | `canon` | No (`409`) |
| C2 | Client writes first (non-empty); backend later | backend CAS requires empty/canonical | client text; backend **skips** | No |
| C3 | Both start simultaneously | row-level serialization + CAS | one winner; loser `409`/skip | No |
| C4 | Backend commits, frontend sends stale PUT | client CAS mismatch | `canon` | No (F16) |
| C5 | Frontend commits, backend finalizes | backend sees non-empty ≠ canon | client text preserved | No |
| C6 | Two finalizations, same turn | 2nd CAS fails / content equal | `canon` once | No |
| C7 | Two overlapping turns, same conversation | **distinct ids** (V4) | two rows, correct content | No |
| C8 | Regenerate while earlier gen still finishing | old row token stale | new canonical on the reused row; old writer `409` | No |
| C9 | Row deleted during finalization | `rowcount 0` (F17) | row gone; `failed(not_found)` | No |
| C10 | Commit ok, ack lost | client CAS fails later | `canon` | No |
| C11 | Timeout / rollback | exception → rollback | unchanged; `failed(timeout)` | No |
| C12 | RLS hides the row | 0 rows (F6) | no write; `failed(not_found)` | No |
| C13 | Automation row shares the session | V4 exact-id | automation row untouched | No |
| C14 | Mis-target by `order` (**today's bug**) | V4 removes the heuristic | correct row | — |

**Note on C1/C4:** they are only safe because the *client* participates (F16). Without the client-side CAS the table's
"no" entries become "yes" (F11). **The race is not closed by backend changes alone.**

---
