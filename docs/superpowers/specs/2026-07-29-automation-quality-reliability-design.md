# Automation Quality & Reliability Program — Hotfixes + Reliability + Presentation

**Date:** 2026-07-29
**Status:** Approved (design phase) — user selected "Approach B with A's hotfixes first"
**Scope:** Three phases. Phase 0 = user-visible hotfixes (preview auth, static-mount
exposure, truncation, report structure). Phase 1 = reliability (LLM transient retry,
data-source connectivity preflight). Phase 2 = output quality (pre-ship verification,
deliverable renderer v2, run metrics + failure alerting).

**Source of insights:** Gap analysis of the automation subsystem vs. Kimi/Manus-class
agents, grounded in a production screenshot showing three defects (401 preview box,
log-style output, mid-sentence truncation), plus the prior 5-harness comparative
audit whose top P0 was "no transient retry on any LLM path".

**Explicit non-goals (Approach C, deferred):** mid-run checkpoint/resume, email/webhook
delivery channels, per-task cost budgets, offline eval harness. Related harness-audit
P1s also out of scope here: hooks loader, real MCP client, httpx pooling, upfront
pydantic tool-arg validation — track as follow-ups.

---

## 1. Confirmed defects (evidence)

| # | Defect | Evidence |
|---|--------|----------|
| D1 | Inline deliverable preview renders `{"detail":"Authentication required"}` | `InlineArtifactPreview.jsx:162-168` uses a raw `<iframe src={previewUrl}>`; iframes cannot send `Authorization` headers, and `deps.py:64-73` reads Bearer tokens only. `GET /api/automations/files/{id}/preview` (`automation_api.py:750`) requires `get_current_user_required` → 401 inside the iframe. |
| D2 | Generated reports are world-readable | `document_generator.py:16` writes to `uploads/automation/<task>/<exec>/` and returns `file_url=/api/uploads/automation/...`; `/api/uploads` is an unauthenticated `StaticFiles` mount (`main.py:542`). Anyone with the URL downloads the report — no tenant check. |
| D3 | Output reads like a log, not a report | Run prompt (`automation_executor.py:671-682`) has one line of format guidance; no enforced structure. Raw agent narration ("✅ Connection… 🔄 Incremental changes…") is shipped as the deliverable. |
| D4 | Chat notification truncated mid-sentence | `_notify_chat` hard-truncates at 600 chars (`automation_executor.py:1678-1680`). |
| D5 | One transient LLM error kills an entire run | Streaming path (`agents.py` ~5600) terminates the turn on any error and ignores the classifier's `retryable`/`should_fallback`/`should_compress` hints; non-streaming (~2602) breaks with "temporarily unavailable"; `_is_transient_error` substring-matches (e.g. `"400"` anywhere). |
| D6 | Hollow runs when the data source is down | `data_sync` preflight only checks a binding *exists*; never tests connectivity. Production runs report success-narration against an unreachable MySQL (10.10.10.49). |
| D7 | No post-run fact-check | `self_critic`/`verification_stop` services exist but are never called in the automation path; LLM-reported figures ship unchecked. |
| D8 | Deliverables look like converted notes | Single static HTML from raw markdown (`document_generator._write_html`); no KPI tiles, charts, or cross-run delta table. |

---

## 2. Design decisions (locked from brainstorming)

| Decision | Choice | Rationale |
|---|---|---|
| Preview auth mechanism | **Signed one-time token in query string** (`?token=`), minted per file per user | Iframes/`<img>` cannot send headers; signed-URL pattern is the standard fix. Keeps tenant check server-side. |
| Static mount | **Keep mount, relocate generated files out of it** | Other features may depend on `/api/uploads`; only `uploads/automation/` must leave the public tree. |
| LLM retry scope | **Retry only before the first streamed chunk** | Mid-stream restart would duplicate already-emitted content; post-first-chunk failures emit a structured error event and fall back to dispatcher-level retry. |
| Retry classification | **Status-code-based** (429/500/502/503/529 + transport errors), honor `Retry-After` | Substring matching (`_is_transient_error`) is fragile — matches "400" anywhere. |
| Verification pass | **One regeneration attempt, then ship-with-banner** | Bounding retries prevents runaway cost/latency on unattended tasks. |
| Charts in renderer v2 | **Server-generated inline SVG** (no CDN) | Deliverables must render offline/behind corp networks; DOMPurify already strips external scripts. |
| New dependencies | **None required** (stdlib HMAC for tokens; existing services for verification) | RAM-limited dev box; matches lazy/optional dependency policy. |
| Schema changes | Runtime `ALTER TABLE` ensure on `automation_executions` | Project convention (no Alembic). |

---

## 3. Phase 0 — Hotfix pack (A)

### 3.1 Signed preview tokens (fixes D1)

**New module:** `backend/app/services/preview_tokens.py`

- `mint_preview_token(*, file_id, user_id, ttl_seconds=900) -> str`
  Payload `{fid, uid, exp}` HMAC-SHA256-signed with `settings.SECRET_KEY`
  (stdlib `hmac`+`hashlib`+`base64`; no new dependency). Tokens are stateless;
  one-time semantics via short TTL, not server-side storage.
- `verify_preview_token(token, file_id) -> user_id | None` — constant-time compare,
  expiry check, `fid` binding check.

**Backend changes (`routers/automation_api.py`):**

- New endpoint: `POST /files/{file_id}/preview-token` — `get_current_user_required`
  + `_assert_tenant`; returns `{token, url}` where
  `url = /api/automations/files/{id}/preview?token={token}`.
- Existing `GET /files/{file_id}/preview`: accept **either** the Bearer header
  (current behavior) **or** a valid `?token=`. Token path resolves the user from
  the token payload and still runs `_assert_tenant(file_row, user)`.

**Frontend (`InlineArtifactPreview.jsx`):**

- Replace direct `iframe src={previewUrl}` with: `useEffect` → `authFetch POST
  /files/{id}/preview-token` → set iframe `src` to the returned signed URL.
  Loading spinner + error state (show "Preview unavailable" with a download
  fallback link, not raw JSON). `authFetch` already handles one silent 401
  refresh-retry (H7), so expired sessions self-heal before minting.
- Re-mint on file_id change; do not cache tokens across files.

### 3.2 Authenticated downloads / static-mount lockdown (fixes D2)

- `document_generator.py`: change output root from `settings.upload_path /
  "automation"` to `settings.data_path / "generated" / "automation"` (outside the
  static mount). `_to_public_url` no longer applies to generated files — the
  `AutomationFile.file_url` field is populated with the authenticated download
  route `/api/automations/files/{id}/download` instead of `/api/uploads/...`.
- Frontend download buttons: `authFetch` the download URL → blob →
  `URL.createObjectURL` → anchor click (downloads work with Bearer; preview uses
  the signed-token path from 3.1).
- `main.py:542`: leave the `/api/uploads` mount in place for genuinely public
  assets (avatars etc.); add a comment marking it public-by-design.
- **Migration:** move existing `uploads/automation/**` files to the new root
  (one-off `scripts/relocate_automation_files.py`, idempotent, updates
  `AutomationFile.file_path`). Old `/api/uploads/automation/...` URLs return 404
  after the move — acceptable: those links were an unauthenticated exposure (D2),
  and the in-chat preview for them was already broken (D1).

### 3.3 Notification summary (fixes D4)

- `automation_executor.py:1678`: replace 600-char hard truncation with
  first-N-sentences extraction (N≤3, ≤600 chars, split on `. `/`！`/`。`/`！` /
  newline), falling back to a word-boundary cut. Never cut mid-word or mid-sentence
  unless a single sentence exceeds the cap.

### 3.4 Run-prompt report structure (fixes D3)

- `automation_executor.py:671-682`: replace the free-form guidance with an
  explicit template:
  1. **Executive summary** (≤3 sentences, outcome-first)
  2. **Key metrics** (markdown table: metric | value | delta vs. previous run)
  3. **Changes since last run** (bulleted, concrete)
  4. **Anomalies / issues** (or "None detected")
  5. **Recommended actions** (or "None required")
- Hard rule in prompt: *"Zero-change runs ship a one-page summary, not a padded
  report. Never narrate tool calls or internal steps in the deliverable body."*
- Internal progress narration stays in `activity_steps` (already live-streamed);
  the deliverable body is report-only.

---

## 4. Phase 1 — Reliability

### 4.1 Stream-level LLM transient retry (fixes D5; harness-audit P0)

**Where:** `routers/agents.py` `add_message_stream` (~5600) and the non-streaming
path (~2602); shared logic in a small helper.

**New module:** `backend/app/services/llm_retry.py`

- `classify_llm_error(exc) -> LlmErrorKind` — status-code-based:
  `TRANSIENT` = HTTP {429, 500, 502, 503, 529}, timeouts, connection errors;
  `PERMANENT` = other 4xx (400/401/403/404/422) and validation errors;
  `UNKNOWN` → treated as permanent (fail-safe, no retry storms).
  Replaces `_is_transient_error` substring matching where an HTTP status is
  available; substring fallback kept only for transport-level exceptions.
- `next_backoff(attempt, retry_after=None) -> float` — exponential
  `min(30, 2**attempt)` + full jitter; `Retry-After` header wins when present.
- Policy: max 3 attempts; retry **only if zero content chunks have been emitted**
  for the current stream (see decisions table).

**Streaming integration (`add_message_stream`):**

- Wrap stream establishment in the retry loop. On transient failure pre-first-chunk:
  emit an SSE `retry` event `{attempt, delay_ms, reason}` so the UI can show
  "Reconnecting… (2/3)", sleep, reopen.
- Post-first-chunk failure: emit a structured `error` event (code, transient flag)
  and terminate cleanly — dispatcher-level `schedule_retry` remains the backstop.
- Honor the existing classifier hints instead of dropping them: `retryable` feeds
  the retry loop; `should_fallback` switches model on final attempt;
  `should_compress` triggers one compaction pass before the final attempt.

**Non-streaming parity (~2602):** same policy loop; replace the
"temporarily unavailable" one-shot break.

**`call_llm` failover:** stop failing over on permanent 4xx (fail fast with the
real error); failover only on `TRANSIENT`.

### 4.2 Data-source connectivity preflight (fixes D6)

- Extend the `data_sync` preflight in `automation_executor.py` (currently
  binding-exists-only) with a live check before spending an LLM run:
  - DB bindings: connect + `SELECT 1`, 5s timeout, run in the existing threadpool.
  - API/HTTP bindings: `HEAD` (fallback `GET`), 5s timeout.
- On failure: mark the execution `failed` immediately with
  `error="data source unreachable: <host>"`, send `_notify_chat_failure`, and use
  the dispatcher's existing `schedule_retry` backoff (integrate, don't replace).
- Effect on current production noise: runs against the unreachable MySQL
  (10.10.10.49) become fast, explicit failures instead of hollow LLM narrations.

---

## 5. Phase 2 — Output quality

### 5.1 Pre-ship verification pass (fixes D7)

- Wire the existing `self_critic` service into `automation_executor` between run
  completion and deliverable generation:
  1. Extract quantitative claims (numbers, counts, dates, totals) from
     `assistant_text`.
  2. Cross-check each against the run's tool results in the transcript.
  3. Mismatch → one regeneration attempt with the critique injected into the
     prompt; still mismatching → ship with an "Unverified figures" banner in the
     deliverable and flag `verification_status="failed"` on the execution.
- Flag: `AUTOMATION_VERIFY_OUTPUTS` (default on), per-task opt-out in task config.
- Bounded: at most one extra LLM call per run.

### 5.2 Deliverable renderer v2 (fixes D8)

- Rework `document_generator._write_html` to a structured template:
  - Header: task name, run timestamp, status badge.
  - KPI tile row (top metrics, large numerals).
  - **Delta table**: current vs. previous run, computed numerically from stored
    execution metrics (not LLM-textual comparison) — metric | previous | current |
    Δ | Δ%.
  - Anomaly section; recommended-actions section.
  - Inline SVG sparklines/bar charts generated server-side (no CDN/scripts), so
    output stays compatible with the DOMPurify-sanitized preview pane (C4).
  - Footer: existing attribution + execution id for traceability.
- Zero-change runs render the compact one-page summary variant (from 3.4).

### 5.3 Run metrics + failure alerting

- Persist per-execution metrics on `automation_executions` via runtime
  `ALTER TABLE` ensure (project convention): `duration_ms`, `prompt_tokens`,
  `completion_tokens` (from `token_tracker`), `tool_calls`, `tool_failures`,
  `llm_retries`, `verification_status`.
- Alerting: rolling window of the task's last 5 executions — if failure rate
  > 50%, post one system message to the task's chat session ("This automation is
  failing repeatedly — last error: …") and surface a warning badge in the
  `/automation` UI. De-dupe: at most one alert per task per 24h.
- No email/webhook (non-goal).

---

## 6. Testing strategy

RAM-limited box: explicit test file paths only, sqlite test DB, no `-n` workers
(see backend conventions). New/updated tests, one file per concern:

| Test file | Covers |
|---|---|
| `tests/test_preview_tokens.py` | mint/verify round-trip, expiry, file binding, tamper; preview endpoint accepts token, rejects bad/expired token, still honors Bearer; tenant check on token path |
| `tests/test_document_generator_paths.py` | generated files land outside the static mount; `file_url` is the authenticated download route; relocation script idempotency |
| `tests/test_notify_summary.py` | sentence-boundary truncation, CJK punctuation, >cap single sentence fallback |
| `tests/test_llm_retry.py` | classify by status code (429/503 transient; 400/401 permanent; "400" substring not transient); backoff honors Retry-After; pre-first-chunk retry emits SSE retry event; post-first-chunk failure terminates with structured error; non-streaming parity; `call_llm` no failover on 4xx |
| `tests/test_ds_connectivity_preflight.py` | unreachable DB → execution failed fast, no LLM call, `schedule_retry` engaged; reachable → proceeds. **Keep the existing gate test's literal `pool.submit(_run_agent_in_conversation` single-line** |
| `tests/test_output_verification.py` | matching figures ship clean; mismatch → one regeneration; persistent mismatch → banner + `verification_status=failed`; flag off → pass-through |
| `tests/test_run_metrics.py` | columns ensured; metrics persisted; 5-run window failure-rate alert fires once per 24h |

Frontend: `InlineArtifactPreview` test — mints token, sets signed iframe src,
shows fallback on mint failure.

## 7. Rollout & compatibility

- **Phase 0** ships first as one unit (D1–D4 are user-visible today); requires the
  file-relocation script + `/opt/rebuild-zhanlu.sh --force` for the frontend.
- **Phase 1** independently shippable; retry events are additive to the SSE
  protocol (unknown event types must be ignored by older frontends).
- **Phase 2** last; renderer v2 changes deliverable appearance only — old
  deliverables remain readable as-is.
- Each phase gets its own commit(s); no phase blocks on a later one.

## 8. Follow-ups (tracked, not in this spec)

- Approach C: checkpoint/resume, email/webhook channels, cost budgets, eval harness.
- Harness-audit P1/P2: hooks loader, real MCP client (HTTP transport + reconnect),
  httpx.AsyncClient pooling, upfront pydantic tool-arg validation, tool_search,
  dry-run/preflight tooling.
- Phase 1b/2 RBAC + tracing follow-ups (from the 2026-07-28 audit).
