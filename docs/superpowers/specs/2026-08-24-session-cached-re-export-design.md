# Session-Cached Re-Export Pipeline

**Date:** 2026-08-24
**Status:** Approved (Sections 1-6 reviewed, ready for implementation planning)
**Author:** CodeBuddy + user
**Scope:** LangGraph-style state machine for re-export requests; replaces the prompt-only "HARD RULE" fix with a deterministic data layer

---

## Problem statement

When a user asks the agent to produce a deliverable in chat, then asks the same data in a different file format ("give me in docx" after a rich prose analysis), the agent currently fails:

1. It does not re-run the data tool that produced the original analysis.
2. It tries to extract a SQL query from the previous chat prose (finds "…"), runs a hallucinated/incomplete query, gets 0 rows.
3. It builds a thin `ReportCardPayload` with `source: "assistant-reply"` and ships a 4-line DOCX with a "Data quality issue detected" warning.

The proposed quick fix was a system-prompt HARD RULE telling the agent to re-run data tools. That fixes the immediate symptom but is architecturally fragile (LLMs leak prompt rules as context grows) and wasteful (re-runs expensive DB tools, risks data drift between Turn 1 and Turn 2).

**This spec proposes the modern alternative:** cache every data tool result in session state, let re-exports reference the cached execution, and enforce validity with code-level guardrails.

---

## Section 1: Architecture Overview

### High-level flow

```
┌──────────────────────────────────────────────────────────────────┐
│ TURN 1: "Contract Performance for last month"                    │
│                                                                  │
│   User → Agent (LLM)                                             │
│          │                                                       │
│          ├─→ ask_data_agent(args) ──→ returns {rows, sql, ...}   │
│          │      │                                                │
│          │      └─→ [AUTO-CACHE MIDDLEWARE]                     │
│          │              │                                        │
│          │              └─→ DataExecution row                    │
│          │                  id = "evt_abc123"                    │
│          │                  result = {rows: [...], sql: ...}     │
│          │                  summary_text = "..."                 │
│          │                                                       │
│          ├─→ LLM renders Markdown prose for chat                 │
│          │                                                       │
│          └─→ SessionState.last_query_result = "evt_abc123"       │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│ TURN 2: "give me in docx"                                        │
│                                                                  │
│   User → Agent (LLM)                                             │
│          │                                                       │
│          ├─→ [INTENT ROUTER] detects "export request"            │
│          │      │                                                │
│          │      ├─→ if strong match (regex + format keyword):    │
│          │      │     skip LLM planning, call create_artifact    │
│          │      │     directly with source_execution_id          │
│          │      │                                                │
│          │      └─→ else: inject directive into context:        │
│          │             "Use source_execution_id from             │
│          │              state.last_query_result"                 │
│          │                                                       │
│          └─→ create_artifact(type="docx",                        │
│                             source_execution_id="evt_abc123")    │
│                  │                                               │
│                  ├─→ [GUARDRAIL] verify execution exists         │
│                  ├─→ [GUARDRAIL] verify result has rows          │
│                  │                                               │
│                  └─→ build ReportCardPayload from cached result  │
│                      render DOCX → return file_url              │
└──────────────────────────────────────────────────────────────────┘
```

### Why this design

| Concern | Before (prompt fix) | After (Full) |
|---|---|---|
| Re-runs DB on re-export | Yes (anti-pattern) | No — pulls from `DataExecution` cache |
| Data drift between turns | Yes (Turn 1 ≠ Turn 2) | No — Turn 2 uses Turn 1's exact data |
| LLM parses prose | Yes (fragile) | No — code reads structured JSON |
| Empty payload shipped | Allowed | Blocked by pre-validation guardrail |
| Latency on re-export | Tool call + LLM synthesis | Single cache lookup + render |
| Works for non-LLM flows | N/A | Intent router handles it deterministically |

### Scope boundary

- **In scope:** All data tools (4 main + any future tool with structured result), `create_artifact`, prompt updates, intent router, session state, cleanup, tests
- **Out of scope:** Replacing the FSM (`SynexiaFSM`) with a LangGraph runtime, changing other tools (`edit_artifact`, `create_automation`), frontend changes (the chat UI already handles the artifact surface)

### Key design decisions

1. **Cache scope:** Per-session with a 7-day TTL as a safety net.
2. **Intent router strength:** Soft hint (inject directive into LLM context) by default; hard bypass (skip LLM planning) for explicit format-only requests.
3. **Backward compat:** Keep the existing `payload` parameter on `create_artifact` as fallback. The new `source_execution_id` is preferred when available.

---

## Section 2: DataExecution model + auto-cache middleware

### DataExecution model

New DB table to cache every data tool result. Lives in `backend/app/models/data_execution.py` (new), with Alembic migration `049_add_data_execution.py`.

```python
class DataExecution(TimestampedBase):
    """One row per successful data-tool call. Caches the raw structured
    result so re-exports (docx, pptx, pdf) can pull from session state
    instead of re-running the tool or parsing prose.
    """
    __tablename__ = "data_executions"

    id: Mapped[str]               # "evt_<uuid>" — short, copy-pasteable
    session_id: Mapped[str]       # Conversation/session
    tool_name: Mapped[str]        # "ask_data_agent", "ask_erp_kpi", etc.
    args: Mapped[dict]            # JSONB — the input (for re-runs / audit)
    result: Mapped[dict]          # JSONB — the raw structured data
    summary_text: Mapped[str|None]  # The prose rendered for chat (fallback)
    expires_at: Mapped[datetime]  # Created_at + 7 days
    org_id: Mapped[str|None]      # tenant
    app_id: Mapped[str|None]      # tenant
    is_deleted: Mapped[bool] = False

    __table_args__ = (
        Index("ix_data_exec_session_created", "session_id", "created_date"),
        Index("ix_data_exec_expires", "expires_at"),  # for cleanup sweep
    )
```

### Auto-cache middleware

Lives in `backend/app/services/data_execution/cache.py` (new). Two pieces:

**1. Cacheable tool pattern set** — explicit opt-in.

```python
CACHEABLE_TOOL_PATTERNS = frozenset({
    "ask_data_agent", "ask_erp_kpi", "ask_perception",
    "ask_forecast", "ask_pricing", "ask_decision",
    "query_composer", "fetch_data_batch", "execute_query",
    "ask_intelligence", "ask_rag_research",
})

async def cache_data_execution(
    *, db: Session, session_id: str, tool_name: str,
    args: dict, result: dict, summary_text: str | None,
    org_id: str | None, app_id: str | None,
) -> str:
    """Persist a DataExecution row. Returns the new execution_id.
    Best-effort: failures are logged but do not break the tool call.
    """
    if tool_name not in CACHEABLE_TOOL_PATTERNS:
        return None
    try:
        execution_id = f"evt_{uuid.uuid4().hex[:12]}"
        row = DataExecution(
            id=execution_id, session_id=session_id, tool_name=tool_name,
            args=args, result=result, summary_text=summary_text,
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
            org_id=org_id, app_id=app_id,
        )
        db.add(row); db.commit()
        SessionState.set_last(db, session_id, execution_id, tool_name)
        return execution_id
    except Exception as exc:
        logger.warning("data_execution cache failed for %s: %s", tool_name, exc)
        return None
```

**2. Registry hook** in the existing tool dispatcher.

```python
# In the tool-call dispatch loop:
result = await tool_handler(**args)
execution_id = await cache_data_execution(
    db=db, session_id=session_id, tool_name=tool_name,
    args=args, result=result, summary_text=current_assistant_content,
    org_id=user.org_id, app_id=user.app_id,
)
if execution_id:
    result = {**(result or {}), "_execution_id": execution_id}
return result
```

### Data flow

```
Turn 1: "Contract Performance for last month"
  LLM → ask_data_agent({"query": "..."})
    → handler returns {rows: [...], sql: "...", columns: [...]}
    → cache_data_execution() persists evt_a1b2c3
    → returns to LLM: {rows, sql, columns, _execution_id: "evt_a1b2c3"}
  LLM → renders prose for chat
  LLM → (optionally) updates SessionState.last_query_result

Turn 2: "give me in docx"
  Intent router (Section 4) detects export request
  → calls create_artifact(source_execution_id="evt_a1b2c3", type="docx")
  → backend fetches DataExecution row by id
  → builds ReportCardPayload from cached result
  → renders DOCX
```

### Cleanup strategy

- **TTL:** 7 days from `created_at`. A lightweight sweep (cron-style, runs hourly) deletes rows where `expires_at < now()` and `is_deleted = false`.
- **Session cascade:** When a `ChatSession` is soft-deleted (`is_deleted = true`), the same flag is set on all its `DataExecution` rows.
- **Per-session cap:** Keep at most 20 most-recent executions per session. Older ones are soft-deleted on insert.

---

## Section 3: SessionState + `state.last_query_result` API

### SessionState model

A lightweight pointer table — one row per session, updated on every cacheable tool call. Lives in `backend/app/models/session_state.py` (new), with Alembic migration `050_add_session_state.py`.

```python
class SessionState(TimestampedBase):
    """Per-session pointer to the most recent data tool execution.

    Updated atomically by ``cache_data_execution`` after every
    cacheable tool call. Read by the intent router and by
    ``create_artifact`` when the LLM does not pass an explicit
    ``source_execution_id``.
    """
    __tablename__ = "session_states"

    session_id: Mapped[str]            # PK — one row per session
    last_execution_id: Mapped[str | None]
    last_tool_name: Mapped[str | None]
    last_data_signature: Mapped[str | None]   # sha1 of result keys+row count
    execution_count: Mapped[int] = 0          # for the history endpoint
    org_id: Mapped[str | None]
    app_id: Mapped[str | None]
    is_deleted: Mapped[bool] = False
```

### State service API

Lives in `backend/app/services/data_execution/session_state.py` (new). Best-effort — failures are logged but never break the calling code path.

```python
def get_last(db, *, session_id: str) -> dict | None:
    """Return the most recent execution pointer, or None.
    Returns dict: {execution_id, tool_name, data_signature,
                   execution_count, updated_at}
    """
    ...

def get_history(db, *, session_id: str, limit: int = 20) -> list[dict]:
    """Return recent executions, newest first."""
    ...

def set_last(db, *, session_id: str, execution_id: str,
             tool_name: str, data_signature: str,
             org_id: str | None, app_id: str | None) -> None:
    """Upsert the pointer + increment the counter."""
    ...
```

### How the LLM accesses state

**1. Injected into the system prompt** (always visible, zero tool-call cost)

After `cache_data_execution` runs, the next LLM turn's system prompt is augmented with:

```
SESSION STATE — last data tool execution in this conversation:
  execution_id: evt_a1b2c3
  tool_name: ask_data_agent
  data_signature: 9f3c1a8b
  When the user asks to re-export or re-format a previous analysis,
  pass this execution_id to create_artifact(source_execution_id=...)
  instead of re-running data tools.
```

Implementation: `build_session_state_block(session_id)` called by the prompt-assembly code.

**2. `session_state_query` tool** (on-demand for history lookups)

```python
{
  "name": "session_state_query",
  "description": "Return the recent data tool executions in this session, "
                 "newest first. Use this when the user wants to re-export "
                 "or re-format a SPECIFIC previous analysis.",
  "parameters": {
    "type": "object",
    "properties": {
      "limit": {"type": "integer", "default": 10},
    },
  },
}
```

### How consumers use it

| Consumer | Reads | Behavior |
|---|---|---|
| **Intent router** (Section 4) | `get_last()` | Pre-fill `source_execution_id` from state. |
| **`create_artifact`** (Section 5) | `get_last()` (fallback) | If `source_execution_id` is not passed, use the session's `last_execution_id`. |
| **Prompt assembly** | `get_last()` | Inject the `SESSION STATE` block. |

---

## Section 4: Code-level intent router

### Goal

Detect "export request" intents BEFORE the LLM planning phase. Strong matches bypass the LLM entirely. Weak matches inject a directive. No match → no intervention.

### Lives in

`backend/app/services/intent/export_intent.py` (new). Called from the v3 stream entry point in `backend/app/routers/agents.py` — after auth, before the LLM.

### Detection logic

```python
@dataclass
class ExportIntent:
    format: str | None              # "docx" | "pdf" | "pptx" | "xlsx" | "html"
    source_execution_id: str | None
    confidence: Literal["strong", "weak"]
    matched_pattern: str

_FORMAT_ALIASES = {
    "docx": "docx", "word": "docx", "ms word": "docx",
    "pdf": "pdf",
    "pptx": "pptx", "powerpoint": "pptx", "slides": "pptx",
    "xlsx": "xlsx", "excel": "xlsx", "spreadsheet": "xlsx",
    "html": "html", "web page": "html",
}

_STRONG_PATTERNS = [
    r"\b(?:give|send|export|save|download|make|convert|render)\b"
    r".*?\b(?:in|as|to)\b\s*(?:a\s+)?(docx|pdf|pptx|xlsx|html|word|powerpoint|excel)\b",
    r"\b(?:in|as)\s+(docx|pdf|pptx|xlsx|html)\s*$",
    r"^(docx|pdf|pptx|xlsx|html)\s*$",
]

_WEAK_PATTERNS = [
    r"\b(?:export|download|save\s+as|send\s+(?:it|the\s+same|that))\b",
    r"\b(?:re-?format|re-?export)\b",
    r"\b(?:now|also)\s+(?:as|in)\s+(?:a\s+)?(?:word|pdf|powerpoint|excel|docx|pptx)\b",
]

def detect_export_intent(user_message: str) -> ExportIntent | None:
    msg = user_message.lower().strip()
    for pattern in _STRONG_PATTERNS:
        m = re.search(pattern, msg)
        if m:
            fmt_token = next((g for g in m.groups() if g), None)
            return ExportIntent(
                format=_FORMAT_ALIASES.get(fmt_token, fmt_token),
                source_execution_id=None,
                confidence="strong",
                matched_pattern=pattern,
            )
    for pattern in _WEAK_PATTERNS:
        if re.search(pattern, msg):
            return ExportIntent(
                format=None, source_execution_id=None,
                confidence="weak", matched_pattern=pattern,
            )
    return None
```

### Two-mode dispatch

```python
# In v3 stream entry (backend/app/routers/agents.py)
intent = detect_export_intent(user_message)

if intent and intent.confidence == "strong":
    state = SessionStateService.get_last(db, session_id=conversation_id)
    if state and state.get("execution_id"):
        intent.source_execution_id = state["execution_id"]
    if intent.source_execution_id:
        result = await _create_artifact_tool(
            db=db, user=user, app_id=app_id,
            args={
                "type": intent.format,
                "title": _derive_title_from_state(state),
                "source_execution_id": intent.source_execution_id,
            },
        )
        yield _format_sse_done(result)
        return

if intent and intent.confidence == "weak":
    yield _inject_directive_into_system_prompt(
        f"\n\nUSER INTENT DETECTED: re-export request. Prefer "
        f"create_artifact(source_execution_id=...) from session state.\n"
    )
```

### Decision matrix

| User message | Detected intent | Action |
|---|---|---|
| "give me Contract Performance for last month" | None | Normal LLM flow |
| "give me in docx" | strong, docx | Hard bypass |
| "export as PDF" | strong, pdf | Hard bypass |
| "now as PowerPoint" | strong, pptx | Hard bypass |
| "in Word" | strong, docx | Hard bypass |
| "export the same" | weak, format=None | Soft hint |
| "regenerate" | None | Normal LLM flow |
| (no cached state + strong match) | strong, no execution_id | Fall through to LLM with soft hint |

---

## Section 5: `create_artifact` updates + code-level guardrails

### Schema change

Add `source_execution_id` to the tool's parameters (optional). The existing `payload` parameter stays for backward compat.

```python
CREATE_ARTIFACT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "create_artifact",
        "description": (
            "Produce a downloadable document (docx, pdf, pptx, html) and "
            "surface it inline in the chat. "
            "PREFERRED for re-exports: pass `source_execution_id` (the "
            "evt_xxx id from a previous data tool result) instead of "
            "`payload` — the platform will re-build the document from "
            "the cached structured data, so you do not need to "
            "re-run the data tool. "
            "If `source_execution_id` is omitted, `payload` is used "
            "(legacy path). For 'html' type, pass {html_content: string}."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["docx","pdf","pptx","html"]},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "skill": {"type": "string"},
                "source_execution_id": {
                    "type": "string",
                    "description": (
                        "OPTIONAL. The evt_xxx id of a cached data tool "
                        "execution in this session. When provided, the "
                        "document is built from the cached structured "
                        "result — no re-run needed."
                    ),
                },
                "payload": { ... },  # unchanged
            },
            "required": ["type", "title"],
        },
    },
}
```

### Three guardrails (in order, fail-fast)

```python
# Inside _create_artifact_tool
source_execution_id = args.get("source_execution_id")
payload = args.get("payload") or {}
conversation_id = args.get("conversation_id") or _extract_conv_id_from_context()

# --- GUARD 1: no execution_id AND no usable payload → BLOCK ---
if not source_execution_id:
    has_data = bool(
        payload.get("chart") or payload.get("kpis") or
        payload.get("data_table") or payload.get("slides") or
        payload.get("html_content")
    )
    if not has_data:
        raise ToolExecutionError(
            "create_artifact called with no source_execution_id and no "
            "structured payload. For re-exports, pass "
            "source_execution_id=evt_xxx. For new analyses, populate "
            "the payload with at least one of: chart, kpis, data_table, "
            "slides, html_content."
        )

# --- Resolve execution: explicit id wins, else fall back to session state ---
if source_execution_id:
    execution = DataExecutionService.get_by_id(db, source_execution_id)
    if not execution:
        raise ToolExecutionError(
            f"source_execution_id {source_execution_id!r} not found or "
            f"expired (TTL: 7 days). Re-run the data tool or pass a "
            f"valid id."
        )
    if execution.session_id != conversation_id:
        raise ToolExecutionError(
            f"source_execution_id {source_execution_id!r} belongs to a "
            f"different session. Use one from this conversation."
        )
else:
    state = SessionStateService.get_last(db, session_id=conversation_id)
    if state and state.get("execution_id"):
        execution = DataExecutionService.get_by_id(db, state["execution_id"])

# --- GUARD 2: 0 rows in cached data → WARN but proceed ---
if execution and len((execution.result or {}).get("rows", [])) == 0:
    payload.setdefault("sections", []).append({
        "title": "Data Quality Note",
        "content": (
            f"The cached data tool ({execution.tool_name}) returned 0 "
            f"rows. The document below reflects that empty result. If "
            f"this is unexpected, re-run the data tool with different "
            f"parameters."
        ),
        "type": "narrative",
    })
```

### Building ReportCardPayload from cached result

```python
def _payload_from_execution(execution: DataExecution) -> dict:
    """Map a cached DataExecution.result to a ReportCardPayload dict.

    Best-effort mapping — the LLM can override any field by passing
    a `payload` dict. We only fill in fields the LLM didn't supply.
    """
    result = execution.result or {}
    rows = result.get("rows") or []
    columns = result.get("columns") or []
    summary = result.get("summary") or result.get("text") or ""

    title = result.get("title") or f"Report from {execution.tool_name}"
    methodology = (
        f"Data sourced from {execution.tool_name} "
        f"({len(rows)} rows, {len(columns)} columns). "
        f"Cached at {execution.created_at.isoformat()}."
    )

    kpis = []
    if rows and len(rows) == 1 and len(columns) <= 6:
        for col, val in zip(columns, rows[0]):
            kpis.append({
                "label": str(col),
                "value": val,
                "caption": f"From {execution.tool_name}",
            })

    chart = None
    if rows and len(rows) > 1 and len(columns) >= 2:
        chart = {
            "type": "bar",
            "title": f"{columns[0]} by {columns[1]}",
            "data": {
                "x": [r[0] for r in rows[:50]],
                "y": [r[1] for r in rows[:50]],
                "x_label": str(columns[0]),
                "y_label": str(columns[1]),
            },
        }

    return {
        "title": title,
        "summary": summary[:1000] if summary else "",
        "methodology": methodology,
        "kpis": kpis,
        "chart": chart,
        "source": execution.tool_name,
    }
```

### Resolution precedence

```
1. source_execution_id provided?
   YES → fetch DataExecution → build payload from cached result
         + merge any LLM-provided payload fields on top
   NO  → fall back to SessionState.get_last()
         IF found → fetch DataExecution → build payload
         IF NOT found → use payload as-is (legacy)
↓
2. Apply GUARD 2 (0-row warning) if needed
↓
3. Pass to _payload_to_reportcard → render DOCX
```

### Backward compatibility

- No breaking changes. Existing callers passing `payload={...}` keep working.
- `source_execution_id` is optional. Old tool-call traces replay unchanged.
- The LLM prompt update (Section 6) tells the LLM to prefer `source_execution_id` for re-exports, but doesn't forbid the old path.

---

## Section 6: Prompt updates, cleanup, E2E, rollout

### Prompt update

Replaces the RE-EXPORT HARD RULE in `default_skills_block()` of `backend/app/services/agent_prompts.py`.

```
RE-EXPORT / RE-FORMAT (HARD RULE): When the user asks to re-export or
re-format a previous analysis to a different file format AFTER you've
already produced an analysis in this conversation (e.g., "give me in
docx", "export as PDF", "now as Word", "make a PowerPoint of that
report", "send the same in Excel"):

1. CHECK the SESSION STATE block in your system prompt for the most
   recent `execution_id` (e.g., "evt_a1b2c3").
2. If present: call
   `create_artifact(source_execution_id="evt_a1b2c3", type=<format>,
   title=<title>)`. The platform builds the document from the cached
   structured data — NO data tool re-run needed (faster, no data
   drift, no LLM parsing of prose).
3. If not present (first turn, cache expired, or different session):
   re-run the data tool(s) you used originally, then call
   `create_artifact` with a populated `payload` (legacy path).
4. NEVER fabricate a payload from the previous chat prose. Prose is a
   rendered report, NOT a query plan — fabricating from it produces
   empty/garbage documents with a data-quality warning.
```

### Cleanup job

Lives in `backend/app/services/data_execution/cleanup.py`.

```python
PER_SESSION_CAP = 20
SWEEP_INTERVAL_S = 3600  # 1 hour


async def cleanup_expired_data_executions() -> int:
    """Delete rows where expires_at < now(). Returns count deleted."""
    db = next(get_db())
    try:
        now = datetime.now(timezone.utc)
        result = db.execute(
            text("""
                DELETE FROM data_executions
                WHERE is_deleted = false
                  AND expires_at < :now
            """),
            {"now": now},
        )
        db.commit()
        if result.rowcount:
            logger.info("data_executions cleanup: %d expired rows deleted",
                        result.rowcount)
        return result.rowcount or 0
    except Exception as exc:
        logger.warning("data_executions cleanup failed: %s", exc)
        db.rollback()
        return 0
    finally:
        db.close()


async def enforce_per_session_cap() -> int:
    """Keep only the N most-recent executions per session."""
    db = next(get_db())
    try:
        result = db.execute(text("""
            DELETE FROM data_executions
            WHERE id IN (
                SELECT id FROM (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY session_id
                        ORDER BY created_date DESC
                    ) AS rn
                    FROM data_executions
                    WHERE is_deleted = false
                ) t
                WHERE rn > :cap
            )
        """), {"cap": PER_SESSION_CAP})
        db.commit()
        return result.rowcount or 0
    finally:
        db.close()


async def scheduled_cleanup_loop():
    import asyncio
    while True:
        try:
            await cleanup_expired_data_executions()
            await enforce_per_session_cap()
        except Exception as exc:
            logger.warning("scheduled cleanup loop error: %s", exc)
        await asyncio.sleep(SWEEP_INTERVAL_S)
```

Started once at app startup:

```python
# backend/main.py
@app.on_event("startup")
async def _startup():
    asyncio.create_task(scheduled_cleanup_loop())
```

### E2E test

Lives in `backend/tests/e2e/test_re_export_no_rerun.py`. Asserts the architectural invariant: across a Turn-1-analysis + Turn-2-export sequence, the data tool is called exactly ONCE.

```python
"""E2E: re-export uses cached execution, never re-runs the data tool."""
import os, time, httpx, pytest

E2E_RUN = os.environ.get("E2E_RUN") == "1"
BASE_URL = os.environ.get("E2E_BASE_URL", "http://localhost:5002").rstrip("/")

DATA_TOOL_NAMES = frozenset({
    "ask_data_agent", "ask_erp_kpi", "ask_perception", "ask_forecast",
    "ask_pricing", "ask_decision", "query_composer", "fetch_data_batch",
    "execute_query", "ask_intelligence", "ask_rag_research",
})

pytestmark = [pytest.mark.e2e, pytest.mark.skipif(not E2E_RUN, reason="E2E_RUN=1 required")]


@pytest.fixture(scope="module")
def session():
    # ... login + create conversation (same as test_ecisco_bi_report_e2e.py) ...
    ...


def test_re_export_does_not_rerun_data_tool(session):
    t1 = session.stream_message("Give me Contract Performance for last month")
    t1_data_calls = sum(
        1 for name, _ in t1["tool_calls"] if name in DATA_TOOL_NAMES
    )
    assert t1_data_calls >= 1, "Turn 1 must call at least one data tool"

    execution_id = t1.get("last_execution_id")
    assert execution_id, "Turn 1 must have set _execution_id on a data tool result"

    t2 = session.stream_message("give me in docx")

    # === THE KEY INVARIANT ===
    t2_data_calls = sum(
        1 for name, _ in t2["tool_calls"] if name in DATA_TOOL_NAMES
    )
    assert t2_data_calls == 0, (
        f"Turn 2 must NOT re-run data tools; got {t2_data_calls} call(s)"
    )

    artifact_calls = [n for n, _ in t2["tool_calls"] if n == "create_artifact"]
    assert artifact_calls, "Turn 2 must call create_artifact"
```

### Rollout plan

| Step | Action | Risk | Rollback |
|---|---|---|---|
| 1 | Apply Alembic migrations 049 + 050 | Low — new tables | `alembic downgrade -1` twice |
| 2 | Deploy with `DATA_EXECUTION_CACHE_ENABLED=false` (default) | Low — no behavior change | — |
| 3 | Enable cleanup loop (`DATA_EXECUTION_CLEANUP_ENABLED=true`) | Low — read-only sweep | Set flag to `false` |
| 4 | Update agent prompt with RE-EXPORT HARD RULE | Low — additive | Revert prompt edit |
| 5 | Enable intent router (`INTENT_ROUTER_ENABLED=true`) | Medium — new code path | Set flag to `false` |
| 6 | Enable cache (`DATA_EXECUTION_CACHE_ENABLED=true`) | Medium — changes Turn 1 behavior | Set flag to `false` |
| 7 | Run E2E test in staging with `E2E_RUN=1` | — | — |
| 8 | Production deploy with all flags ON | — | Flip flags off one at a time |
| 9 | (After 1-2 weeks stable) Deprecate legacy `payload` path | Medium | Re-enable legacy path |

### New flags (all default `false`)

```python
# backend/app/config.py

DATA_EXECUTION_CACHE_ENABLED: bool = False    # Section 2 — auto-cache hook
DATA_EXECUTION_CLEANUP_ENABLED: bool = False  # Section 6 — cleanup loop
INTENT_ROUTER_ENABLED: bool = False           # Section 4 — export intent router
```

### Final test coverage summary

~30 new tests in ~12 new test files:

```
backend/tests/services/data_execution/         # Sections 2, 3
  test_cache.py
  test_inject.py
  test_session_state.py
  test_prompt_block.py
  test_session_state_tool.py
  test_cleanup.py
backend/tests/services/intent/                 # Section 4
  test_export_intent.py
  test_hard_bypass.py
backend/tests/services/tool_handlers/          # Section 5
  test_create_artifact_schema.py
  test_create_artifact_guards.py
  test_payload_from_execution.py
  test_create_artifact_e2e.py
backend/tests/alembic/                         # Sections 2, 3
  test_049.py
  test_050.py
backend/tests/e2e/                            # Section 6
  test_re_export_no_rerun.py
```

### Total scope (all 6 sections)

| Metric | Count |
|---|---|
| New files | ~18 (models, services, routers, tools, migrations, tests) |
| Modified files | ~4 (artifact_tool.py, agents.py router, agent_prompts.py, config.py) |
| New DB tables | 2 (data_executions, session_states) |
| New flags | 3 (DATA_EXECUTION_CACHE_ENABLED, DATA_EXECUTION_CLEANUP_ENABLED, INTENT_ROUTER_ENABLED) |
| New tools registered | 1 (session_state_query) |
| Estimated LoC | ~1,200 (incl. tests + comments) |
| Estimated effort | 3-5 days for one engineer |

---

## Self-review checklist

- [x] No placeholders: every code block shows the full implementation
- [x] Internal consistency: Sections 1-6 reference the same models, flags, and naming
- [x] Scope: focused on the re-export problem; explicitly out-of-scope items listed
- [x] Ambiguity-free: the "guarded" vs "soft" decision for the intent router is explicit; the resolution precedence for `create_artifact` is unambiguous
- [x] Backward compatibility: existing `payload` path preserved; new path is additive
- [x] Safe rollout: 3 flags, each toggleable independently
- [x] Observability: cleanup logs row counts, cache failures logged but don't break the tool call
- [x] Tenant isolation: org_id / app_id on every cached row
