# Automation Quality Phase 2 — Output Quality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** LLM-reported figures get fact-checked before shipping (one bounded regeneration, then ship-with-banner); deliverables render as professional reports (KPI tiles, numeric cross-run deltas, SVG sparklines); runs persist tool/verification metrics and repeated failures raise one alert.

**Architecture:** A new `output_verifier.py` (modeled on `self_critic.py`'s never-raises LLM-JSON-verdict pattern — **note:** the existing `self_critic` does refusal-detection, a different job, so we reuse its architecture, not the service) checks quantitative claims against the run's captured tool outputs. The executor wires it between run completion and persistence, regenerating once with the critique on mismatch. Renderer v2 extends `_write_html` with a report scaffold driven by a `report_context` (prev-run metrics from `output_data.report_metrics`, history for sparklines, verification banner, zero-change flag). Metrics land on `automation_executions` via the existing explicit `_ensure_schema` additions; the two new `AutomationTask` columns sync via model introspection. `_mark_failed` gains a failure-rate alerter (≥3 of last 5, 24h dedupe via `task.last_alert_at`); a tiny `GET /api/automations/health` feeds the UI badge.

**Tech Stack:** existing `call_llm` (async), `asyncio.run` in the executor's worker thread (proven pattern at `_run_agent_in_conversation:1534`), stdlib SVG generation, React + vitest.

**Spec:** `docs/superpowers/specs/2026-07-29-automation-quality-reliability-design.md` (§5). Phases 0+1 landed.

## Global Constraints

- No new dependencies; verifier never blocks shipping (any error → `skipped`).
- Test DB recipe: `cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)"`, then prefix pytest with `DATABASE_URL="sqlite:///./test_runtime.db"`.
- **RAM limited**: explicit test file paths only; no `-n`.
- Do NOT modify the line `pool.submit(_run_agent_in_conversation, ...)` (gate test) or `_check_bound_source_connectivity` gate region ordering (source-literal tests).
- `settings` is imported function-locally inside `automation_executor.py` helpers (file convention).
- Frontend test cmd: `cd /root/zhanlu/frontend && npx vitest run src/pages/AutomationTasks.test.jsx` (create the file).

## Deferred (with justification)

- `prompt_tokens` / `completion_tokens` / `llm_retries` metrics: the v3 stream path (`agents.py`) carries no usage events, and `_run_agent_in_conversation` returns only text/conv/fsm/tool_outcome. Capturing them requires extending the stream event protocol — a separate follow-up touching the hot path. The three columns are omitted from this phase rather than shipped as permanent zeros.

---

### Task 1: Schema — metrics + verification + alert columns

**Files:**
- Modify: `backend/app/models/automation_execution.py` (3 columns)
- Modify: `backend/app/models/automation_task.py` (2 columns)
- Modify: `backend/app/services/automation_dispatcher.py:200-214` (explicit additions)
- Test: `backend/tests/test_phase2_schema.py`

**Interfaces:**
- Produces: `AutomationExecution.tool_calls/tool_failures/verification_status`; `AutomationTask.verify_outputs` ("true"/"false"), `AutomationTask.last_alert_at`. Consumed by Tasks 3, 4, 6.

- [ ] **Step 1: Write the failing test**

```python
"""Phase 2 schema: runtime ALTER TABLE ensure adds the new columns."""
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from sqlalchemy import inspect as sa_inspect

from app.database import engine
from app.models.automation_execution import AutomationExecution
from app.models.automation_task import AutomationTask
from app.services import automation_dispatcher as disp


def test_model_columns_exist():
    assert hasattr(AutomationExecution, "tool_calls")
    assert hasattr(AutomationExecution, "tool_failures")
    assert hasattr(AutomationExecution, "verification_status")
    assert hasattr(AutomationTask, "verify_outputs")
    assert hasattr(AutomationTask, "last_alert_at")


def test_ensure_schema_adds_columns():
    disp._ensure_schema()
    cols_exec = {c["name"] for c in sa_inspect(engine).get_columns("automation_executions")}
    assert {"tool_calls", "tool_failures", "verification_status"} <= cols_exec
    cols_task = {c["name"] for c in sa_inspect(engine).get_columns("automation_tasks")}
    assert {"verify_outputs", "last_alert_at"} <= cols_task
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)" && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_phase2_schema.py -v`
Expected: FAIL — attributes missing.

- [ ] **Step 3: Implement**

`backend/app/models/automation_execution.py` — after `error`:

```python
    # Phase 2: per-run metrics + verification outcome
    tool_calls = Column(Integer, nullable=True)
    tool_failures = Column(Integer, nullable=True)
    verification_status = Column(String(20), nullable=True)  # verified|failed|skipped|disabled
```

`backend/app/models/automation_task.py` — after `last_run_error`:

```python
    # Phase 2: per-task opt-out for the pre-ship verification pass
    # ("true"/"false" — string to match the runtime column sync's
    # String/Boolean DDL mapping quirks on existing DBs)
    verify_outputs = Column(String(10), nullable=True, default="true")
    # Dedupe for the repeated-failure alert (at most one per 24h)
    last_alert_at = Column(DateTime, nullable=True)
```

`backend/app/services/automation_dispatcher.py` — extend the `automation_executions` additions list:

```python
            ("timeout_at", "TIMESTAMP" if is_pg else "DATETIME"),
            ("tool_calls", "INTEGER"),
            ("tool_failures", "INTEGER"),
            ("verification_status", "VARCHAR(20)"),
```

(The task-table columns need no explicit entry — the `_ensure_schema` model-introspection loop auto-adds them; `_to_ddl` handles String/DateTime.)

- [ ] **Step 4: Run test to verify it passes**

Run: same as Step 2
Expected: 2 passed

---

### Task 2: `output_verifier` service

**Files:**
- Create: `backend/app/services/output_verifier.py`
- Test: `backend/tests/test_output_verifier.py`

**Interfaces:**
- Produces: `async def verify_run_output(*, user_prompt, assistant_text, tool_outputs, llm_call) -> dict` — verdict `{"verified": bool, "mismatches": list, "critique": str, ...}`; never raises; verifier failure → `{"verified": True, "skipped": True}` (shipping is never blocked by the checker itself). Consumed by Task 3 via a sync wrapper.

- [ ] **Step 1: Write the failing test**

```python
"""output_verifier: verdict parsing + never-raises contract."""
import asyncio
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.output_verifier import _parse_verdict, verify_run_output


def test_parse_clean_json():
    v = _parse_verdict('{"verified": false, "mismatches": [{"claim": "rev"}], "critique": "rev wrong"}')
    assert v["verified"] is False
    assert v["mismatches"] == [{"claim": "rev"}]
    assert v["critique"] == "rev wrong"


def test_parse_fenced_and_noisy():
    raw = 'Here is my verdict:\n```json\n{"verified": true}\n```'
    assert _parse_verdict(raw)["verified"] is True


def test_parse_garbage_fails_open():
    v = _parse_verdict("I cannot decide")
    assert v["verified"] is True  # fail-open: don't block shipping on junk


def test_verify_passes_verdict_through():
    async def llm_call(messages, max_tokens=400, temperature=0.0):
        return '{"verified": false, "critique": "revenue figure not in tool data"}'

    v = asyncio.run(verify_run_output(
        user_prompt="p", assistant_text="rev $5M", tool_outputs="rev: $4.2M",
        llm_call=llm_call,
    ))
    assert v["verified"] is False
    assert "revenue" in v["critique"]


def test_verify_never_raises_when_llm_fails():
    async def llm_call(messages, max_tokens=400, temperature=0.0):
        raise RuntimeError("llm down")

    v = asyncio.run(verify_run_output(
        user_prompt="p", assistant_text="x", tool_outputs="y", llm_call=llm_call,
    ))
    assert v["verified"] is True and v["skipped"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && python3 -m pytest tests/test_output_verifier.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement `backend/app/services/output_verifier.py`**

```python
"""Pre-ship verification for automation run outputs (spec §5.1).

Cross-checks the quantitative claims in a run's deliverable text against
the tool outputs captured during the run, using one cheap LLM call in the
same architectural pattern as ``self_critic.py``: a structured JSON
verdict, robust extraction, and a **never-raises** contract — a verifier
failure must never block an unattended run from shipping.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

_VERIFY_SYSTEM = (
    "You are a verification pass for an automated report. You are given:\n"
    "1) the task prompt, 2) the raw data the agent retrieved from its tools "
    "(query results, API responses), and 3) the report the agent wrote.\n"
    "Check every QUANTITATIVE claim in the report (figures, counts, totals, "
    "percentages, dates, names of records) against the raw data. Minor "
    "rounding or formatting differences are acceptable; contradictions and "
    "invented figures are not. Reply with ONLY a JSON object:\n"
    '{"verified": true|false, "mismatches": [{"claim": "...", "reported": '
    '"...", "actual": "..."}], "critique": "one short paragraph for the '
    'agent explaining what to fix (empty when verified)"}'
)

_MAX_TOOL_OUTPUT_CHARS = 4000
_MAX_REPORT_CHARS = 6000


def _parse_verdict(raw: str) -> dict:
    """Extract the verdict JSON from an LLM reply. Fail-open (verified=True)
    when no parseable verdict exists — junk must not block shipping."""
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            verdict = json.loads(m.group(0))
            if isinstance(verdict, dict) and "verified" in verdict:
                verdict["verified"] = bool(verdict.get("verified"))
                verdict.setdefault("mismatches", [])
                verdict.setdefault("critique", "")
                return verdict
        except Exception:
            pass
    return {"verified": True, "mismatches": [], "critique": "", "parse_error": True}


async def verify_run_output(
    *,
    user_prompt: str,
    assistant_text: str,
    tool_outputs: str,
    llm_call: Callable[..., Awaitable[str]],
) -> dict:
    """Verify ``assistant_text``'s quantitative claims against ``tool_outputs``.

    Returns a verdict dict. Never raises: any verifier failure returns
    ``{"verified": True, "skipped": True}``.
    """
    try:
        messages = [
            {"role": "system", "content": _VERIFY_SYSTEM},
            {"role": "user", "content": (
                f"TASK PROMPT:\n{(user_prompt or '')[:2000]}\n\n"
                f"RAW TOOL DATA:\n{(tool_outputs or '')[:_MAX_TOOL_OUTPUT_CHARS]}\n\n"
                f"REPORT TO VERIFY:\n{(assistant_text or '')[:_MAX_REPORT_CHARS]}"
            )},
        ]
        raw = await llm_call(messages, max_tokens=500, temperature=0.0)
        return _parse_verdict(raw)
    except Exception as e:
        logger.warning("verify_run_output: verifier call failed (%s) — skipping", e)
        return {"verified": True, "mismatches": [], "critique": "", "skipped": True}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/backend && python3 -m pytest tests/test_output_verifier.py -v`
Expected: 5 passed

---

### Task 3: Executor verification wiring

**Files:**
- Modify: `backend/app/services/automation_executor.py` (new helpers; wire between line ~888 and ~891; persist status + banner in the completion block)
- Test: `backend/tests/test_run_verification.py`

**Interfaces:**
- Consumes: `verify_run_output` (Task 2), `_run_agent_in_conversation` (existing), `task.verify_outputs` (Task 1).
- Produces: `_collect_tool_outputs(db, conv_id) -> str`; `_verify_sync(prompt, text, outputs) -> dict`; `_verify_and_maybe_regenerate(...) -> tuple[str, str, str|None]` returning `(final_text, verification_status, banner)`; `execution.verification_status`; `output_data["verification_banner"]`.

- [ ] **Step 1: Write the failing test**

```python
"""Pre-ship verification wiring: verified/regenerated/banner/skip/opt-out."""
import os
import sys
from unittest.mock import MagicMock, patch

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services import automation_executor as ax


def _task(verify="true"):
    t = MagicMock()
    t.verify_outputs = verify
    return t


def _call(**overrides):
    """Run _verify_and_maybe_regenerate with mocked collaborators."""
    db = MagicMock()
    kwargs = dict(
        db=db, task=_task(), execution=MagicMock(), agent=MagicMock(),
        agent_prompt="run the sync", user_prompt="sync",
        project=None, data_source_ids=None,
        assistant_text="Revenue was $5M.", conv_id="c1",
        tool_outcome={"calls": 2, "failures": 0},
    )
    kwargs.update(overrides)
    return ax._verify_and_maybe_regenerate(**kwargs)


def test_verified_first_pass():
    with patch.object(ax, "_collect_tool_outputs", return_value="rev: $5M"), \
         patch.object(ax, "_verify_sync", return_value={"verified": True, "mismatches": [], "critique": ""}), \
         patch.object(ax, "_run_agent_in_conversation") as regen:
        text, status, banner = _call()
    assert (text, status, banner) == ("Revenue was $5M.", "verified", None)
    regen.assert_not_called()


def test_mismatch_regenerates_once_then_ships_clean():
    with patch.object(ax, "_collect_tool_outputs", return_value="rev: $4.2M"), \
         patch.object(ax, "_verify_sync", side_effect=[
             {"verified": False, "mismatches": [{"claim": "rev"}], "critique": "rev should be $4.2M"},
             {"verified": True, "mismatches": [], "critique": ""},
         ]), \
         patch.object(ax, "_run_agent_in_conversation",
                      return_value=("Revenue was $4.2M.", "c2", None, {"calls": 1, "failures": 0})):
        text, status, banner = _call()
    assert text == "Revenue was $4.2M."
    assert status == "verified" and banner is None


def test_persistent_mismatch_ships_with_banner():
    bad = {"verified": False, "mismatches": [{"claim": "rev"}], "critique": "wrong"}
    with patch.object(ax, "_collect_tool_outputs", return_value="rev: $4.2M"), \
         patch.object(ax, "_verify_sync", side_effect=[bad, bad]), \
         patch.object(ax, "_run_agent_in_conversation",
                      return_value=("Revenue was $5M.", "c2", None, {})):
        text, status, banner = _call()
    assert text == "Revenue was $5M."
    assert status == "failed"
    assert "Unverified figures" in banner


def test_skips_when_no_tool_outputs():
    with patch.object(ax, "_collect_tool_outputs", return_value=""):
        text, status, banner = _call()
    assert status == "skipped" and banner is None


def test_skips_when_no_tool_calls_happened():
    text, status, banner = _call(tool_outcome={"calls": 0, "failures": 0})
    assert status == "skipped"


def test_task_level_opt_out():
    text, status, banner = _call(task=_task(verify="false"))
    assert status == "disabled"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && python3 -m pytest tests/test_run_verification.py -v`
Expected: FAIL — `AttributeError: _verify_and_maybe_regenerate`.

- [ ] **Step 3: Implement**

New helpers in `automation_executor.py` (place after `_check_bound_source_connectivity`):

```python
_UNVERIFIED_BANNER = (
    "⚠️ Unverified figures: one or more figures in this report could not "
    "be confirmed against the underlying data sources."
)


def _collect_tool_outputs(db: Session, conv_id: str, cap: int = 4000) -> str:
    """Pull the tool-role messages (query results / API responses) out of
    the run conversation for the verification pass. Empty string when the
    conversation or tool outputs can't be read."""
    try:
        conv = db.get(AgentConversation, conv_id) if conv_id else None
        if conv is None:
            return ""
        messages = conv.get_messages() if hasattr(conv, "get_messages") else (conv.messages or [])
        chunks: list[str] = []
        total = 0
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            if msg.get("role") != "tool":
                continue
            content = str(msg.get("content") or "").strip()
            if not content:
                continue
            chunks.append(content)
            total += len(content)
            if total >= cap:
                break
        return "\n---\n".join(chunks)[:cap]
    except Exception as e:
        logger.warning("_collect_tool_outputs: %s", e)
        return ""


def _verify_sync(user_prompt: str, assistant_text: str, tool_outputs: str) -> dict:
    """Sync wrapper around output_verifier.verify_run_output. The executor
    runs in a worker thread with no event loop, so asyncio.run is safe
    (same pattern as _run_agent_in_conversation)."""
    import asyncio
    from app.services.llm_service import call_llm
    from app.services.output_verifier import verify_run_output

    async def _llm_call(messages, max_tokens=400, temperature=0.0):
        result = await call_llm(messages=messages, temperature=temperature)
        return result.get("response", "") if isinstance(result, dict) else str(result or "")

    return asyncio.run(verify_run_output(
        user_prompt=user_prompt,
        assistant_text=assistant_text,
        tool_outputs=tool_outputs,
        llm_call=_llm_call,
    ))


def _verify_and_maybe_regenerate(
    *, db: Session, task, execution, agent, agent_prompt: str,
    user_prompt: str, project, data_source_ids,
    assistant_text: str, conv_id: str, tool_outcome: dict,
) -> tuple[str, str, str | None]:
    """Spec §5.1: verify quantitative claims against captured tool outputs;
    on mismatch, regenerate ONCE with the critique injected, then either
    ship clean ("verified") or ship with an unverified-figures banner
    ("failed"). Never blocks shipping: "skipped" when there is nothing to
    check against, "disabled" via config or per-task opt-out."""
    from app.config import settings

    if not getattr(settings, "AUTOMATION_VERIFY_OUTPUTS", True):
        return assistant_text, "disabled", None
    if str(getattr(task, "verify_outputs", "true") or "true").lower() == "false":
        return assistant_text, "disabled", None
    if not (tool_outcome or {}).get("calls"):
        return assistant_text, "skipped", None
    tool_outputs = _collect_tool_outputs(db, conv_id)
    if not tool_outputs:
        return assistant_text, "skipped", None

    verdict = _verify_sync(user_prompt, assistant_text, tool_outputs)
    if verdict.get("skipped"):
        return assistant_text, "skipped", None
    if verdict.get("verified", True):
        return assistant_text, "verified", None

    critique = verdict.get("critique") or "Some figures contradict the retrieved data."
    logger.info("verification mismatch for execution %s: %s — regenerating once",
                getattr(execution, "id", "?"), critique[:200])
    regen_prompt = (
        f"{agent_prompt}\n\n"
        "VERIFICATION FEEDBACK on your previous draft — fix these issues and "
        "rewrite the full report. Do NOT invent figures; use only values "
        "present in the retrieved data:\n"
        f"{critique}"
    )
    try:
        regen = _run_agent_in_conversation(
            agent, regen_prompt, db,
            task=task, project=project, data_source_ids=data_source_ids,
        )
        regen_text = (regen[0] or "").strip() if regen else ""
    except Exception as e:
        logger.warning("verification regeneration failed: %s", e)
        regen_text = ""
    if not regen_text:
        return assistant_text, "failed", _UNVERIFIED_BANNER

    verdict2 = _verify_sync(user_prompt, regen_text, tool_outputs)
    if verdict2.get("verified", True):
        return regen_text, "verified", None
    return regen_text, "failed", _UNVERIFIED_BANNER
```

Wire it in `execute_automation` immediately after the `if not final_text: return` guard (line ~889), before "Persist the textual output":

```python
        # Phase 2 (spec §5.1): pre-ship verification of quantitative claims.
        # May regenerate once; returns the text to ship + status + banner.
        assistant_text, verification_status, verification_banner = (
            _verify_and_maybe_regenerate(
                db=db, task=task, execution=execution, agent=agent,
                agent_prompt=agent_prompt, user_prompt=user_prompt,
                project=project, data_source_ids=data_source_ids,
                assistant_text=assistant_text, conv_id=conv_id,
                tool_outcome=tool_outcome,
            )
        )
```

In the completion block, after `execution.output_text = ...` (line ~892):

```python
        execution.verification_status = verification_status
```

and extend the first `execution.output_data` dict with:

```python
            "verification_banner": verification_banner,
```

(The completion block at ~989-990 already computes `tool_calls`/`tool_failures` from `tool_outcome`; Task 4 persists them on the new columns.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/backend && python3 -m pytest tests/test_run_verification.py -v`
Expected: 6 passed

---

### Task 4: Run metrics persistence + repeated-failure alerting

**Files:**
- Modify: `backend/app/services/automation_executor.py` (persist tool counts in completion block; `_maybe_alert_repeated_failures` helper + call from `_mark_failed`)
- Modify: `backend/app/routers/automation_api.py` (`GET /health`)
- Test: `backend/tests/test_run_metrics_alerts.py`

**Interfaces:**
- Produces: `execution.tool_calls/tool_failures` persisted; `ax._maybe_alert_repeated_failures(db, execution)`; `GET /api/automations/health` → `{"<task_id>": "failing"|"ok"}`. Task 6 consumes the endpoint.

- [ ] **Step 1: Write the failing test**

```python
"""Run metrics columns + repeated-failure alert (>=3 of last 5, 24h dedupe)."""
import os
import sys
import uuid
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.database import SessionLocal
from app.models.automation_execution import AutomationExecution
from app.models.automation_task import AutomationTask
from app.models.chat_message import ChatMessage
from app.models.chat_session import ChatSession
from app.services import automation_executor as ax


def _seed_task(db, with_session=True):
    session = None
    if with_session:
        session = ChatSession(id=str(uuid.uuid4()), title="t", org_id="o", app_id="a")
        db.add(session); db.flush()
    task = AutomationTask(
        id=str(uuid.uuid4()), name="T", type="data_sync",
        org_id="o", app_id="a", session_id=session.id if session else None,
    )
    db.add(task); db.commit()
    return task


def _add_executions(db, task, statuses):
    rows = []
    for i, st in enumerate(statuses):
        ex = AutomationExecution(
            id=str(uuid.uuid4()), automation_task_id=task.id, status=st,
            created_date=datetime.utcnow() - timedelta(minutes=i),
            org_id="o", app_id="a",
        )
        db.add(ex); rows.append(ex)
    db.commit()
    return rows


def _alert(db, task):
    with patch.object(ax, "_notify_chat_failure"):
        ex = db.query(AutomationExecution).filter(
            AutomationExecution.automation_task_id == task.id,
        ).order_by(AutomationExecution.created_date.asc()).first()
        ax._maybe_alert_repeated_failures(db, ex)
    db.refresh(task)
    return db.query(ChatMessage).filter(ChatMessage.session_id == task.session_id).all()


def test_no_alert_below_threshold():
    db = SessionLocal()
    try:
        task = _seed_task(db)
        _add_executions(db, task, ["failed", "failed", "completed", "failed", "completed"])
        assert _alert(db, task) == []
    finally:
        db.rollback(); db.close()


def test_alert_at_three_of_five_and_dedupes_24h():
    db = SessionLocal()
    try:
        task = _seed_task(db)
        _add_executions(db, task, ["failed", "failed", "completed", "failed", "failed"])
        msgs = _alert(db, task)
        assert len(msgs) == 1
        assert "failing repeatedly" in (msgs[0].content or "").lower()
        db.refresh(task)
        assert task.last_alert_at is not None
        # Second call within 24h → no new message
        msgs2 = _alert(db, task)
        assert len(msgs2) == 1
    finally:
        db.rollback(); db.close()


def test_alert_fires_again_after_24h():
    db = SessionLocal()
    try:
        task = _seed_task(db)
        task.last_alert_at = datetime.utcnow() - timedelta(hours=25)
        db.commit()
        _add_executions(db, task, ["failed", "failed", "failed", "failed", "failed"])
        msgs = _alert(db, task)
        assert len(msgs) == 1
    finally:
        db.rollback(); db.close()


def test_alert_never_breaks_mark_failed():
    db = MagicMock()
    db.query.side_effect = RuntimeError("db down")
    ex = MagicMock()
    # Must not raise even when everything inside fails.
    ax._maybe_alert_repeated_failures(db, ex)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)" && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_run_metrics_alerts.py -v`
Expected: FAIL — `AttributeError: _maybe_alert_repeated_failures`.

- [ ] **Step 3: Implement**

Persist tool counts — in the completion block after `final_status`/`final_error` are computed (~line 991-1000), alongside the CAS update. Find where `execution.status = final_status` (or the CAS dict) is set and add:

```python
        execution.tool_calls = tool_calls
        execution.tool_failures = tool_failures
```

(If the completion uses a CAS `db.query(...).update({...})` dict, add the two keys there instead — read the actual block before editing.)

New helper in `automation_executor.py`:

```python
_ALERT_WINDOW = 5
_ALERT_FAILURE_THRESHOLD = 3  # >50% of the last 5 runs
_ALERT_COOLDOWN = timedelta(hours=24)


def _maybe_alert_repeated_failures(db: Session, execution) -> None:
    """Spec §5.3: when ≥3 of a task's last 5 runs failed, post ONE system
    message to the task's chat session (deduped to 1 per 24h via
    task.last_alert_at). Must never raise — it runs inside _mark_failed."""
    try:
        task = db.get(AutomationTask, execution.automation_task_id)
        if task is None:
            return
        now = datetime.utcnow()
        if task.last_alert_at and now - task.last_alert_at < _ALERT_COOLDOWN:
            return
        recent = db.query(AutomationExecution).filter(
            AutomationExecution.automation_task_id == task.id,
        ).order_by(AutomationExecution.created_date.desc()).limit(_ALERT_WINDOW).all()
        if len(recent) < _ALERT_WINDOW:
            return
        failures = sum(1 for ex in recent if ex.status == "failed")
        if failures < _ALERT_FAILURE_THRESHOLD:
            return
        session_id = task.session_id or _resolve_fallback_session_id(db, task)
        if not session_id:
            return
        last_error = (recent[0].error or task.last_run_error or "unknown")[:300]
        msg = ChatMessage(
            id=str(uuid.uuid4()),
            session_id=session_id,
            role="system",
            content=(
                f"⚠️ This automation is failing repeatedly ({failures} of the "
                f"last {_ALERT_WINDOW} runs). Last error: {last_error}. "
                "Check the task configuration and data sources."
            ),
            org_id=task.org_id, app_id=task.app_id,
        )
        db.add(msg)
        task.last_alert_at = now
        db.commit()
        logger.warning("automation %s repeated-failure alert posted (%d/%d)",
                       task.id, failures, _ALERT_WINDOW)
    except Exception as e:
        logger.warning("_maybe_alert_repeated_failures: %s", e)
        try:
            db.rollback()
        except Exception:
            pass
```

Check `_resolve_fallback_session_id` exists under that name — `_notify_chat` uses a fallback; reuse whatever it uses (read it before writing; if the logic is inline, factor a small `_resolve_fallback_session_id(db, task)` and call it from both places).

Call it from `_mark_failed` after the CAS block (`logger.info("automation ... status transition ...")` line ~97):

```python
    # Phase 2 (spec §5.3): surface repeated failures to the user (deduped).
    _maybe_alert_repeated_failures(db, execution)
```

Health endpoint in `automation_api.py`:

```python
@router.get("/health")
def automation_health(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Per-task health for the /automation UI badge: "failing" when ≥3 of
    the last 5 executions failed."""
    tasks = db.query(AutomationTask).filter(
        AutomationTask.is_deleted == False,  # noqa: E712
    ).all()
    tasks = [t for t in tasks if _is_tenant(t, user)]
    health: dict[str, str] = {}
    for task in tasks:
        recent = db.query(AutomationExecution.status).filter(
            AutomationExecution.automation_task_id == task.id,
        ).order_by(AutomationExecution.created_date.desc()).limit(5).all()
        failures = sum(1 for (st,) in recent if st == "failed")
        health[task.id] = (
            "failing" if len(recent) >= 5 and failures >= 3 else "ok"
        )
    return health
```

(Verify `_is_tenant` is the existing helper name used by other endpoints — reuse the same one; if it's `_assert_tenant`, filter via the same org logic those endpoints use.)

- [ ] **Step 4: Run test to verify it passes**

Run: same as Step 2, plus `tests/test_run_verification.py`
Expected: all pass

---

### Task 5: Renderer v2 — report scaffold

**Files:**
- Modify: `backend/app/services/document_generator.py` (`generate_document` + `_write_html` + new helpers)
- Modify: `backend/app/services/automation_executor.py` (`_render_and_save_files` builds `report_context`; completion block stores `report_metrics`)
- Test: `backend/tests/test_renderer_v2.py`

**Interfaces:**
- Produces: `dg.parse_metrics_table(md) -> list[dict]` (`{name, value_text, value_num, delta_text}`); `dg._sparkline_svg(values, width=96, height=24) -> str`; `generate_document(..., report_context=None)`; `_write_html(..., report_context=None)`. `report_context` keys: `run_at` (iso str), `prev_metrics` (list), `history` (dict name→list[float], oldest→newest), `verification_banner` (str|None), `zero_change` (bool). Non-report payloads and missing sections render exactly as before.

- [ ] **Step 1: Write the failing test**

```python
"""Renderer v2: KPI tiles, numeric deltas, sparklines, zero-change variant."""
import os
import sys
from pathlib import Path

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services import document_generator as dg

REPORT_MD = """## Executive summary
Revenue grew this week.

## Key metrics
| metric | value | delta |
| --- | --- | --- |
| Revenue | $5.2M | +4% |
| Active users | 12,400 | -2% |

## Changes since last run
- Revenue up.

## Anomalies and issues
None detected.

## Recommended actions
None required.
"""


def test_parse_metrics_table():
    rows = dg.parse_metrics_table(REPORT_MD)
    assert rows[0] == {"name": "Revenue", "value_text": "$5.2M",
                       "value_num": 5_200_000.0, "delta_text": "+4%"}
    assert rows[1]["value_num"] == 12_400.0


def test_scaffold_renders_tiles_and_numeric_delta(tmp_path):
    ctx = {
        "run_at": "2026-07-29T10:00:00",
        "prev_metrics": [{"name": "Revenue", "value_text": "$5.0M",
                          "value_num": 5_000_000.0, "delta_text": "+2%"}],
        "history": {"Revenue": [4.6e6, 4.8e6, 5.0e6, 5.2e6]},
        "verification_banner": None,
        "zero_change": False,
    }
    path, _, _ = dg._write_html(tmp_path, "Weekly Sync", REPORT_MD, "e1", report_context=ctx)
    html = path.read_text(encoding="utf-8")
    assert 'class="kpi-tile"' in html
    assert "$5.2M" in html
    assert "+4.0%" in html          # numeric delta vs prev ($5.0M → $5.2M)
    assert "<svg" in html           # sparkline
    assert "Executive summary" in html


def test_verification_banner_rendered(tmp_path):
    ctx = {"run_at": "2026-07-29T10:00:00", "prev_metrics": [], "history": {},
           "verification_banner": "⚠️ Unverified figures", "zero_change": False}
    path, _, _ = dg._write_html(tmp_path, "R", REPORT_MD, "e1", report_context=ctx)
    html = path.read_text(encoding="utf-8")
    assert 'class="verification-banner"' in html
    assert "Unverified figures" in html


def test_zero_change_compact_variant(tmp_path):
    md = REPORT_MD.replace("- Revenue up.", "No changes detected.")
    ctx = {"run_at": "2026-07-29T10:00:00", "prev_metrics": [], "history": {},
           "verification_banner": None, "zero_change": True}
    path, _, _ = dg._write_html(tmp_path, "R", md, "e1", report_context=ctx)
    html = path.read_text(encoding="utf-8")
    assert 'class="zero-change"' in html
    assert "Anomalies and issues" not in html  # compact: summary + note only


def test_non_report_payload_unchanged(tmp_path):
    path, _, _ = dg._write_html(tmp_path, "R", "# Just a heading\n\nsome text", "e1")
    html = path.read_text(encoding="utf-8")
    assert 'class="kpi-tile"' not in html
    assert "<h1>" in html


def test_sparkline_svg_scales():
    svg = dg._sparkline_svg([1.0, 3.0, 2.0, 5.0])
    assert svg.startswith("<svg") and "polyline" in svg
    assert dg._sparkline_svg([1.0]) == ""  # need ≥2 points
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && python3 -m pytest tests/test_renderer_v2.py -v`
Expected: FAIL — `AttributeError: parse_metrics_table` / signature mismatch.

- [ ] **Step 3: Implement**

New helpers in `document_generator.py` (after `_md_to_html`):

```python
_NUMBER_RE = re.compile(r"-?[\d,]+(?:\.\d+)?")


def _parse_number(text: str):
    """Parse "$5.2M" / "12,400" / "-2.5%" → float; None when unparseable."""
    if not text:
        return None
    m = _NUMBER_RE.search(text.replace(",", ""))
    if not m:
        return None
    try:
        value = float(m.group(0).replace(",", ""))
    except ValueError:
        return None
    tail = text[m.end():].strip().lower()
    if tail.startswith("k"):
        value *= 1_000
    elif tail.startswith("m"):
        value *= 1_000_000
    elif tail.startswith("b"):
        value *= 1_000_000_000
    return value


def parse_metrics_table(md: str) -> list[dict]:
    """Parse the '## Key metrics' markdown table from a report body.
    Returns [{name, value_text, value_num, delta_text}]."""
    lines = (md or "").splitlines()
    rows: list[dict] = []
    in_section = False
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith("## "):
            in_section = "key metrics" in stripped[3:].strip().lower()
            i += 1
            continue
        if in_section and "|" in stripped and i + 1 < len(lines) and re.match(
            r"^\s*\|?[\s\-:|]+\|[\s\-:|]*\s*$", lines[i + 1].strip()
        ):
            i += 2  # skip header + separator
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if len(cells) >= 2:
                    rows.append({
                        "name": cells[0],
                        "value_text": cells[1],
                        "value_num": _parse_number(cells[1]),
                        "delta_text": cells[2] if len(cells) > 2 else "",
                    })
                i += 1
            break
        i += 1
    return rows


def _sparkline_svg(values: list, width: int = 96, height: int = 24) -> str:
    """Inline SVG sparkline for a metric's recent history (no CDN/scripts —
    survives the DOMPurify-sanitized preview)."""
    nums = [float(v) for v in values if isinstance(v, (int, float))]
    if len(nums) < 2:
        return ""
    lo, hi = min(nums), max(nums)
    span = (hi - lo) or 1.0
    step = width / (len(nums) - 1)
    points = " ".join(
        f"{i * step:.1f},{height - 2 - ((v - lo) / span) * (height - 4):.1f}"
        for i, v in enumerate(nums)
    )
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg" role="img" aria-label="trend">'
        f'<polyline fill="none" stroke="#3b82f6" stroke-width="1.5" '
        f'points="{points}"/></svg>'
    )


def _split_report_sections(md: str) -> dict:
    """Split a report body into its ## sections. Keys lowercased."""
    sections: dict[str, list[str]] = {}
    current = "_preamble"
    for line in (md or "").splitlines():
        if line.strip().startswith("## "):
            current = line.strip()[3:].strip().lower()
            sections.setdefault(current, [])
        else:
            sections.setdefault(current, []).append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


_REPORT_CSS = """
.report-header { display: flex; justify-content: space-between; align-items: baseline; flex-wrap: wrap; gap: 8px; }
.status-badge { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; background: #dcfce7; color: #166534; }
.kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin: 20px 0; }
.kpi-tile { background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 10px; padding: 14px 16px; }
.kpi-tile .kpi-name { font-size: 12px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.04em; }
.kpi-tile .kpi-value { font-size: 24px; font-weight: 700; color: #111827; margin: 4px 0 2px; }
.kpi-tile .kpi-delta { font-size: 13px; font-weight: 600; }
.kpi-delta.up { color: #059669; }
.kpi-delta.down { color: #dc2626; }
.kpi-delta.flat { color: #6b7280; }
.verification-banner { background: #fffbeb; border: 1px solid #f59e0b; border-radius: 8px; padding: 10px 14px; margin: 16px 0; font-size: 14px; color: #92400e; }
.zero-change { background: #f0f9ff; border: 1px solid #bae6fd; border-radius: 8px; padding: 12px 16px; margin: 16px 0; color: #075985; font-size: 14px; }
.delta-table td.num, .delta-table th.num { text-align: right; }
"""


def _render_kpi_tiles(metrics: list[dict], ctx: dict) -> str:
    prev_by_name = {m.get("name"): m for m in (ctx.get("prev_metrics") or [])}
    history = ctx.get("history") or {}
    tiles: list[str] = []
    for row in metrics[:8]:
        prev = prev_by_name.get(row["name"])
        delta_html = ""
        if prev and row["value_num"] is not None and prev.get("value_num"):
            pct = (row["value_num"] - prev["value_num"]) / abs(prev["value_num"]) * 100
            cls = "up" if pct > 0.05 else ("down" if pct < -0.05 else "flat")
            delta_html = f'<div class="kpi-delta {cls}">{pct:+.1f}% vs last run</div>'
        elif row.get("delta_text"):
            delta_html = f'<div class="kpi-delta flat">{row["delta_text"]} (reported)</div>'
        spark = _sparkline_svg(history.get(row["name"], []))
        tiles.append(
            f'<div class="kpi-tile"><div class="kpi-name">{row["name"]}</div>'
            f'<div class="kpi-value">{row["value_text"]}</div>{delta_html}{spark}</div>'
        )
    return f'<div class="kpi-grid">{"".join(tiles)}</div>' if tiles else ""


def _render_delta_table(metrics: list[dict], ctx: dict) -> str:
    prev_by_name = {m.get("name"): m for m in (ctx.get("prev_metrics") or [])}
    if not prev_by_name:
        return ""
    rows: list[str] = []
    for row in metrics:
        prev = prev_by_name.get(row["name"])
        if not prev:
            continue
        if row["value_num"] is not None and prev.get("value_num"):
            pct = (row["value_num"] - prev["value_num"]) / abs(prev["value_num"]) * 100
            delta = f"{pct:+.1f}%"
        else:
            delta = row.get("delta_text") or "—"
        rows.append(
            f'<tr><td>{row["name"]}</td><td class="num">{prev.get("value_text", "—")}</td>'
            f'<td class="num">{row["value_text"]}</td><td class="num">{delta}</td></tr>'
        )
    if not rows:
        return ""
    return (
        '<h2>Changes vs. previous run</h2><table class="delta-table">'
        '<tr><th>Metric</th><th class="num">Previous</th><th class="num">Current</th>'
        '<th class="num">Δ</th></tr>' + "".join(rows) + "</table>"
    )
```

Rewrite `_write_html`:

```python
def _write_html(directory: Path, title: str, content: Any, exec_id: str,
                report_context: dict | None = None) -> Tuple[Path, str, str]:
    if isinstance(content, dict):
        body_md = content.get("markdown") or content.get("body") or json.dumps(content, indent=2, ensure_ascii=False)
    else:
        body_md = str(content or "")

    meta = content.get("meta", {}) if isinstance(content, dict) else {}
    report_period = meta.get("report_period", datetime.utcnow().strftime("%B %d, %Y"))
    date_of_report = meta.get("date_of_report", datetime.utcnow().strftime("%B %d, %Y"))

    metrics = parse_metrics_table(body_md)
    use_scaffold = report_context is not None and bool(metrics)

    if use_scaffold:
        sections = _split_report_sections(body_md)
        summary_md = sections.get("executive summary", "")
        banner = report_context.get("verification_banner")
        banner_html = (
            f'<div class="verification-banner">{banner}</div>' if banner else ""
        )
        header_html = (
            f'<div class="report-header"><h1>{_safe_filename(title)}</h1>'
            f'<span class="status-badge">completed</span></div>'
            f'<div class="meta">Run: {report_context.get("run_at", date_of_report)}'
            f' · Reporting Period: {report_period}</div>'
        )
        if report_context.get("zero_change"):
            note = (
                '<div class="zero-change">No changes detected since the '
                'previous run. One-page summary — no padded report.</div>'
            )
            body_html = (
                banner_html + note
                + (f"<h2>Executive summary</h2>{_md_to_html(summary_md)}" if summary_md else "")
                + _render_kpi_tiles(metrics, report_context)
            )
        else:
            body_html = (
                banner_html + header_html
                + (f"<h2>Executive summary</h2>{_md_to_html(summary_md)}" if summary_md else "")
                + _render_kpi_tiles(metrics, report_context)
                + _render_delta_table(metrics, report_context)
                + _md_to_html(body_md)
            )
        css = _HTML_CSS + _REPORT_CSS
        header_html_done = True
    else:
        body_html = _md_to_html(body_md)
        css = _HTML_CSS
        header_html_done = False

    header = (
        f"<h1>{_safe_filename(title)}</h1>\n"
        f'<div class="meta">Reporting Period: {report_period} · Date of Report: {date_of_report}</div>'
        if not header_html_done else ""
    )
    html = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{_safe_filename(title)}</title>
<style>{css}</style>
</head>
<body>
<div class="container">
{header}
{body_html}
<div class="footer">Generated by Zhanlu Automation · {datetime.utcnow().isoformat()}Z · run {exec_id}</div>
</div>
</body></html>"""

    path = directory / f"{_safe_filename(title)}.html"
    path.write_text(html, encoding="utf-8")
    return path, _make_url(path), "text/html; charset=utf-8"
```

Thread `report_context` through `generate_document` (add keyword-only param, pass to `_write_html`).

Executor — `_render_and_save_files` builds the context for html output (before calling `generate_document`):

```python
    report_context = None
    if output_format == "html":
        report_context = _build_report_context(db, task, execution, payload)
```

with new helpers:

```python
def _build_report_context(db: Session, task, execution, payload) -> dict:
    """Assemble renderer-v2 context: current metrics, previous run's stored
    metrics, per-metric history (≤8 runs), verification banner, zero-change."""
    from app.services.document_generator import parse_metrics_table

    text = payload.get("markdown") if isinstance(payload, dict) else payload
    text = text or ""
    metrics = parse_metrics_table(text)
    history: dict[str, list[float]] = {}
    prev_metrics: list[dict] = []
    try:
        prev_runs = db.query(AutomationExecution).filter(
            AutomationExecution.automation_task_id == task.id,
            AutomationExecution.status == "completed",
            AutomationExecution.id != execution.id,
        ).order_by(AutomationExecution.completed_at.desc()).limit(8).all()
        if prev_runs:
            prev_metrics = ((prev_runs[0].output_data or {}).get("report_metrics") or [])
            names = {m["name"] for m in metrics}
            for run in reversed(prev_runs):  # oldest → newest
                past = {m.get("name"): m.get("value_num")
                        for m in ((run.output_data or {}).get("report_metrics") or [])}
                for name in names:
                    if past.get(name) is not None:
                        history.setdefault(name, []).append(past[name])
            for name in names:
                current = next((m["value_num"] for m in metrics if m["name"] == name), None)
                if current is not None and name in history:
                    history[name].append(current)
    except Exception as e:
        logger.warning("_build_report_context: history lookup failed: %s", e)
    zero_change = "no changes detected" in text.lower()
    return {
        "run_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "prev_metrics": prev_metrics,
        "history": history,
        "verification_banner": (execution.output_data or {}).get("verification_banner"),
        "zero_change": zero_change,
    }
```

Completion block (next to the `cross_run_delta` update): also store parsed metrics for the next run's deltas:

```python
                "report_metrics": parse_metrics_table(assistant_text or ""),
```

(import `parse_metrics_table` locally there.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/backend && python3 -m pytest tests/test_renderer_v2.py tests/test_document_generator_paths.py tests/test_automation_phase5_quality_and_delta.py -v`
Expected: all pass (6 new + 3 + 7 regressions).

---

### Task 6: Frontend health badge

**Files:**
- Modify: `frontend/src/pages/AutomationTasks.jsx` (fetch `/api/automations/health`, badge on card)
- Test: `frontend/src/pages/AutomationTasks.test.jsx` (new)

**Interfaces:**
- Consumes: `GET /api/automations/health` (Task 4). Badge: red dot + "Failing" text on cards whose `health[task.id] === "failing"`.

- [ ] **Step 1: Write the failing test**

```jsx
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

vi.mock('@/api/authFetch', () => ({ authFetch: vi.fn() }));
vi.mock('@/lib/AuthContext', () => ({ useAuth: () => ({ user: { id: 'u1' } }) }));

const mockList = vi.fn();
vi.mock('@/api/base44Client', () => ({
  base44: { entities: { AutomationTask: { list: (...a) => mockList(...a) } } },
}));

import { authFetch } from '@/api/authFetch';
import AutomationTasks from './AutomationTasks';

const task = {
  id: 't1', name: 'Daily ERP sync', type: 'data_sync', enabled: true,
  schedule: { kind: 'daily', time: '08:00' }, output_format: 'html',
};

describe('AutomationTasks health badge', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockList.mockResolvedValue([task]);
  });

  it('shows the Failing badge when the health endpoint marks the task', async () => {
    authFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve({ t1: 'failing' }) });
    render(<MemoryRouter><AutomationTasks /></MemoryRouter>);
    await waitFor(() => {
      expect(screen.getByText(/failing/i)).toBeTruthy();
    });
    expect(authFetch).toHaveBeenCalledWith('/api/automations/health');
  });

  it('shows no badge when healthy', async () => {
    authFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve({ t1: 'ok' }) });
    render(<MemoryRouter><AutomationTasks /></MemoryRouter>);
    await waitFor(() => {
      expect(screen.getByText('Daily ERP sync')).toBeTruthy();
    });
    expect(screen.queryByText(/failing/i)).toBeNull();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/frontend && npx vitest run src/pages/AutomationTasks.test.jsx`
Expected: FAIL — no health fetch / badge.

- [ ] **Step 3: Implement**

In `AutomationTasks.jsx`: add `const [health, setHealth] = useState({});`; in the existing `load()` (or a parallel effect), `authFetch('/api/automations/health')` → `setHealth(await res.json())` (guarded, non-blocking — page works without it). In the task card header (next to the task name / status chip), render when `health[task.id] === 'failing'`:

```jsx
<span className="inline-flex items-center gap-1 rounded-full bg-red-500/10 px-2 py-0.5 text-xs font-medium text-red-500">
  <span className="h-1.5 w-1.5 rounded-full bg-red-500" />
  Failing
</span>
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/frontend && npx vitest run src/pages/AutomationTasks.test.jsx`
Expected: 2 passed

---

### Task 7: Phase-2 verification sweep

- [ ] **Step 1: Backend — all new + regression files**

```bash
cd /root/zhanlu/backend && rm -f test_runtime.db && \
DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)" && \
DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest \
  tests/test_phase2_schema.py tests/test_output_verifier.py \
  tests/test_run_verification.py tests/test_run_metrics_alerts.py \
  tests/test_renderer_v2.py \
  tests/test_document_generator_paths.py tests/test_automation_phase5_quality_and_delta.py \
  tests/test_automation_truthful_failure.py tests/test_automation_ds_preflight.py \
  tests/test_ds_connectivity_preflight.py tests/test_notify_summary.py \
  tests/test_format_guidance.py -v
```
Expected: all pass.

- [ ] **Step 2: Compile + frontend**

`python3 -m py_compile` on all touched backend files; `npx vitest run` on `AutomationTasks.test.jsx` + `InlineArtifactPreview.test.jsx`; `npx eslint` on `AutomationTasks.jsx`.

## Self-Review

- **Spec coverage:** §5.1 verification (Tasks 2, 3) ✓; §5.2 renderer v2 (Task 5) ✓; §5.3 metrics + alerting (Tasks 1, 4, 6) ✓. Deferred items listed up front with justification ✓.
- **Placeholder scan:** all code complete; two "read the actual block before editing" notes (completion CAS shape; `_notify_chat`'s fallback-session logic) are explicit recon steps, not skipped work.
- **Type consistency:** verifier verdict keys (`verified/mismatches/critique/skipped`) identical across Tasks 2/3; `_verify_and_maybe_regenerate` keyword signature matches its tests; `report_context` keys identical across executor builder / renderer / tests; health payload shape matches frontend test.
- **Known boundaries:** verifier regeneration path unit-tested via mocks (no live LLM); health endpoint tested implicitly through the alerter + Task 6 frontend test (endpoint test with TestClient follows the Phase 0 pattern if time permits).
