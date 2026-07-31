# Automation Response Quality — Phase 2 (Rich FSM Synthesis) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the task-aware `report_synthesis.synthesize_report` into the FSM synthesize path so FSM-path data tasks get a structured `ReportCardPayload` (KPIs/charts/insights) like the v2 tool loop, and surface that payload in FINALIZE for non-file data tasks — at zero net extra LLM calls.

**Architecture:** `_execute_synthesize_node` (capability_router.py) is refactored to call `synthesize_report` (sync-wrapped via the existing `_run_coro_sync` bridge) instead of its plain `call_llm`. A pure converter maps the returned `FinalizeResult` to the existing `result_data` shape (preserving sandbox backward-compat) plus a new `report_card_payload` key. On exception, a corrected legacy fallback (properly awaited) preserves pre-Phase-2 behavior. FINALIZE surfaces the synthesize `report_card_payload` when no file-export artifact was produced.

**Tech Stack:** Python 3.11, SQLAlchemy ORM, pydantic models (`FinalizeResult`/`ReportCardPayload`), pytest (sqlite test DB, explicit file paths, no `-n` workers).

**Spec:** `docs/superpowers/specs/2026-07-29-automation-response-quality-design.md` (Phase 2, §4).

## Discovered bug (fixed by this phase)

`call_llm` is `async` (`llm_service.py:164`), but the current `_execute_synthesize_node`
calls it **without `await`** (`capability_router.py:443`): `result = call_llm(...)`. `result`
is a coroutine, so `result.get("response", "")` raises `AttributeError`, the `except` branch
catches it, and the node **always returns the stub fallback** (`capability_router.py:457-466`).
The FSM synthesize node has never actually run the LLM. Phase 2 fixes this by running the
async `synthesize_report` path through `_run_coro_sync`, and the legacy fallback now correctly
awaits `call_llm` via `_run_coro_sync` too.

## Scope note

Phase 2 only. Phase 3 (plan-args + multi-obs + adaptive re-planning) gets its own follow-up
plan. This phase produces working, independently testable software.

## Global Constraints

- **RAM-limited dev box:** run only the specific test files by explicit path; never broad collection; no `-n` workers.
- **Test DB:** `cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)"` then `python3 -m pytest tests/<file> -v`.
- **Async bridging:** the FSM runs sync (inside `asyncio.to_thread`); async helpers (`synthesize_report`, `call_llm`) MUST be run via `_run_coro_sync` (`capability_router.py:248`), never bare-called.
- **Backward compat:** the sandbox node reads `result_data["instructions"]` and `result_data["synth_data"]["chart"]["data"]` (`capability_router.py:508-515`); the converted `result_data` MUST preserve both keys.
- **Defensive reads** throughout; no new dependencies; no schema changes.
- **Commit only when the user asks.** Deploy via `docker restart zhanlu-backend`.

## File Structure

- **Modify:** `backend/app/services/synexia/capability_router.py` — refactor `_execute_synthesize_node` (`:389`); add `_synth_call_llm` adapter, `_finalize_result_to_result_data` converter, `_legacy_synthesize_fallback`.
- **Modify:** `backend/app/services/synexia/fsm.py:720-755` (FINALIZE) — surface synthesize `report_card_payload` for non-file data tasks.
- **Test:** `backend/tests/test_fsm_synthesize_rich.py` — converter, wiring (rich path + fallback), FINALIZE surfacing.

---

## Task 1: Refactor `_execute_synthesize_node` to use `synthesize_report`

**Files:**
- Modify: `backend/app/services/synexia/capability_router.py:389-483`
- Test: `backend/tests/test_fsm_synthesize_rich.py`

**Interfaces:**
- Consumes: `report_synthesis.synthesize_report(*, user_message, rows, sql, source_name, source_id, call_llm_fn, user_signal=None) -> FinalizeResult` (async, `report_synthesis.py:598`); `_run_coro_sync(coro)` (`capability_router.py:248`); `call_llm(prompt=, messages=, temperature=) -> {"response":...}` (async, `llm_service.py:164`).
- Produces:
  - `async _synth_call_llm(system_prompt: str, messages: list[dict]) -> {"content": str}` — adapter bridging `synthesize_report`'s `call_llm_fn` contract to the async `call_llm`.
  - `_finalize_result_to_result_data(finalize_result: FinalizeResult) -> dict` — pure converter → `{summary, instructions, synth_data, report_card_payload}`.
  - `_legacy_synthesize_fallback(db, execution, node, user_message, task_spec, rows, sql) -> ObservationRecord` — corrected pre-Phase-2 path (properly awaited).
  - `_execute_synthesize_node` now: rich path (synthesize_report) → convert → record; on exception → legacy fallback.

- [ ] **Step 1: Write the failing tests for the converter**

Create `backend/tests/test_fsm_synthesize_rich.py`:

```python
"""Tests for Phase 2: rich report_synthesis wired into the FSM synthesize path.

Covers (1) the pure FinalizeResult→result_data converter, (2) the synthesize
node wiring (rich path + legacy fallback), (3) FINALIZE surfacing the
ReportCardPayload for non-file data tasks.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.synexia.contracts import FinalizeResult, ReportCardPayload, KPISpec, ChartSpec, InsightSpec
from app.services.synexia import capability_router as cr


def _finalize_result(*, with_payload=True, prose="Q2 revenue was 189M CNY."):
    rcp = None
    if with_payload:
        rcp = ReportCardPayload(
            title="Q2 Sales Report",
            source="sales_db",
            summary="Q2 revenue 189M CNY, Steel leads at 55%.",
            kpis=[KPISpec(label="Revenue", value="189M CNY")],
            chart=ChartSpec(type="bar", title="Revenue by material",
                            x_key="material", y_keys=["revenue"],
                            data=[{"material": "Steel", "revenue": 120}],
                            unit="CNY"),
            insights=[InsightSpec(icon="trending-up", text="Steel leads at 55%")],
        )
    return FinalizeResult(
        task_kind="report",
        assistant_content=prose,
        report_card_payload=rcp,
        user_signal="default",
    )


class TestFinalizeResultToResultData:
    def test_converts_payload_to_result_data_shape(self):
        fr = _finalize_result()
        rd = cr._finalize_result_to_result_data(fr)
        # backward-compat keys the sandbox node reads
        assert "summary" in rd and "instructions" in rd and "synth_data" in rd
        assert rd["report_card_payload"] is not None
        # sandbox reads synth_data["chart"]["data"]
        assert rd["synth_data"]["chart"]["data"] == [{"material": "Steel", "revenue": 120}]
        assert rd["synth_data"]["kpis"][0]["label"] == "Revenue"
        assert rd["synth_data"]["title"] == "Q2 Sales Report"
        # instructions fall back to the prose summary
        assert "189M CNY" in rd["instructions"]
        # report_card_payload is the serialized payload dict
        assert rd["report_card_payload"]["title"] == "Q2 Sales Report"

    def test_no_payload_produces_empty_synth_and_none_rcp(self):
        fr = _finalize_result(with_payload=False, prose="Just prose summary.")
        rd = cr._finalize_result_to_result_data(fr)
        assert rd["report_card_payload"] is None
        assert rd["synth_data"]["kpis"] == []
        assert rd["synth_data"]["chart"] is None
        assert rd["summary"] == "Just prose summary."
        assert rd["instructions"] == "Just prose summary."
```

- [ ] **Step 2: Run converter tests to verify they fail**

Run:
```bash
cd /root/zhanlu/backend && python3 -m pytest tests/test_fsm_synthesize_rich.py::TestFinalizeResultToResultData -v
```
Expected: FAIL — `AttributeError: module ... has no attribute '_finalize_result_to_result_data'`.

- [ ] **Step 3: Implement the adapter + converter**

Add to `backend/app/services/synexia/capability_router.py` (immediately above `_execute_synthesize_node`, before line 389):

```python
async def _synth_call_llm(system_prompt: str, messages: list[dict]) -> dict:
    """Adapter bridging ``synthesize_report``'s ``call_llm_fn`` to async ``call_llm``.

    ``synthesize_report`` awaits ``call_llm_fn(system, messages)`` and expects
    ``{"content": "..."}``; ``call_llm`` returns ``{"response": "..."}``.
    """
    from app.services.llm_service import call_llm
    result = await call_llm(prompt=system_prompt, messages=messages, temperature=0.7)
    return {"content": result.get("response", "")}


def _finalize_result_to_result_data(finalize_result) -> dict:
    """Convert a :class:`FinalizeResult` to the ObservationRecord ``result_data`` shape.

    Preserves backward compat with the sandbox node (which reads
    ``result_data['instructions']`` and ``result_data['synth_data']['chart']['data']``)
    and adds ``report_card_payload`` for FINALIZE to surface (spec §4.2).
    """
    rcp = getattr(finalize_result, "report_card_payload", None)
    rcp_dict = rcp.model_dump() if rcp is not None else {}
    prose = getattr(finalize_result, "assistant_content", "") or ""
    summary = rcp_dict.get("summary") or prose
    synth_data = {
        "title": rcp_dict.get("title") or "Report",
        "summary": summary,
        "instructions": prose or summary,
        "kpis": rcp_dict.get("kpis") or [],
        "chart": rcp_dict.get("chart"),
        "insights": rcp_dict.get("insights") or [],
    }
    return {
        "summary": summary,
        "instructions": synth_data["instructions"],
        "synth_data": synth_data,
        "report_card_payload": rcp_dict or None,
    }
```

- [ ] **Step 4: Run converter tests to verify they pass**

Run:
```bash
cd /root/zhanlu/backend && python3 -m pytest tests/test_fsm_synthesize_rich.py::TestFinalizeResultToResultData -v
```
Expected: PASS.

- [ ] **Step 5: Write the failing wiring tests**

Append to `backend/tests/test_fsm_synthesize_rich.py`:

```python
def _nl2sql_prev_obs():
    return SimpleNamespace(
        success=True, observation_type="nl2sql",
        result_data={
            "sql": "SELECT material, revenue FROM sales WHERE q='Q2'",
            "data": [{"material": "Steel", "revenue": 120}],
        },
    )


def _node():
    return SimpleNamespace(name="synthesize", node_type="synthesize", inputs={})


def _execution():
    return SimpleNamespace(
        id="exec-1", user_message="summarize Q2 sales",
        task_spec={"entities": {"source_name": "sales_db", "source_id": "src-1"}},
    )


class TestSynthesizeNodeRichPath:
    def test_calls_synthesize_report_and_records_converted_result(self, monkeypatch):
        captured = {}

        async def fake_synthesize(*, user_message, rows, sql, source_name,
                                  source_id, call_llm_fn, user_signal=None):
            captured.update(rows=rows, sql=sql, source_name=source_name,
                            source_id=source_id, user_message=user_message)
            return _finalize_result()

        monkeypatch.setattr(cr, "_get_previous_observation", lambda db, ex: _nl2sql_prev_obs())
        monkeypatch.setattr("app.services.synexia.report_synthesis.synthesize_report", fake_synthesize)
        recorded = {}
        def fake_record(db, ex, node, **kw):
            recorded.update(kw)
            return SimpleNamespace(success=True, **kw)
        monkeypatch.setattr(cr, "_record_observation", fake_record)

        out = cr._execute_synthesize_node(MagicMock(), _execution(), _node())

        # synthesize_report received rows/sql sourced from the prior nl2sql obs
        assert captured["rows"] == [{"material": "Steel", "revenue": 120}]
        assert captured["sql"].startswith("SELECT material")
        assert captured["source_name"] == "sales_db"
        # recorded result_data is the converted shape (rich, with report_card_payload)
        assert recorded["result_data"]["report_card_payload"] is not None
        assert recorded["result_data"]["synth_data"]["chart"]["data"] == [{"material": "Steel", "revenue": 120}]
        assert recorded["observation_type"] == "synthesize"
        assert out.success is True

    def test_no_previous_observation_passes_empty_rows(self, monkeypatch):
        captured = {}
        async def fake_synthesize(*, user_message, rows, sql, source_name,
                                  source_id, call_llm_fn, user_signal=None):
            captured.update(rows=rows, sql=sql)
            return _finalize_result()
        monkeypatch.setattr(cr, "_get_previous_observation", lambda db, ex: None)
        monkeypatch.setattr("app.services.synexia.report_synthesis.synthesize_report", fake_synthesize)
        monkeypatch.setattr(cr, "_record_observation", lambda db, ex, node, **kw: SimpleNamespace(success=True, **kw))
        cr._execute_synthesize_node(MagicMock(), _execution(), _node())
        assert captured["rows"] == []
        assert captured["sql"] is None


class TestSynthesizeNodeFallback:
    def test_rich_path_exception_falls_back_to_legacy(self, monkeypatch):
        async def boom(*, **kw):
            raise RuntimeError("synthesis exploded")
        monkeypatch.setattr(cr, "_get_previous_observation", lambda db, ex: _nl2sql_prev_obs())
        monkeypatch.setattr("app.services.synexia.report_synthesis.synthesize_report", boom)
        # legacy fallback calls call_llm (async) — stub it via _run_coro_sync path
        async def fake_call_llm(**kw):
            return {"response": '{"title":"FB","summary":"fb summary","instructions":"do x","kpis":[],"chart":null,"insights":[]}'}
        monkeypatch.setattr("app.services.llm_service.call_llm", fake_call_llm)
        recorded = {}
        def fake_record(db, ex, node, **kw):
            recorded.update(kw)
            return SimpleNamespace(success=True, **kw)
        monkeypatch.setattr(cr, "_record_observation", fake_record)

        cr._execute_synthesize_node(MagicMock(), _execution(), _node())

        # legacy fallback produced a result (no report_card_payload)
        assert recorded["result_data"]["report_card_payload"] is None
        assert recorded["result_data"]["synth_data"]["title"] == "FB"
        assert recorded["result_data"]["summary"] == "fb summary"
```

- [ ] **Step 6: Run wiring tests to verify they fail**

Run:
```bash
cd /root/zhanlu/backend && python3 -m pytest tests/test_fsm_synthesize_rich.py::TestSynthesizeNodeRichPath tests/test_fsm_synthesize_rich.py::TestSynthesizeNodeFallback -v
```
Expected: FAIL — `_execute_synthesize_node` still uses the old `call_llm` path; `synthesize_report` not called; `result_data["report_card_payload"]` absent.

- [ ] **Step 7: Implement the refactor + legacy fallback**

In `backend/app/services/synexia/capability_router.py`, **replace the entire body of `_execute_synthesize_node`** (lines 389-483) with:

```python
def _execute_synthesize_node(db, execution, node) -> ObservationRecord:
    """Execute a synthesize node — produce a rich ReportCardPayload via LLM.

    Phase 2: runs the task-aware ``synthesize_report`` (sync-wrapped via
    ``_run_coro_sync``) so FSM-path data tasks get the structured KPIs /
    chart / insights payload the v2 tool loop already produces. The
    returned :class:`FinalizeResult` is converted to the existing
    ``result_data`` shape (preserving sandbox backward-compat) plus a
    ``report_card_payload`` key for FINALIZE to surface.

    On exception, falls back to the corrected legacy simple-LLM-call path
    (``_legacy_synthesize_fallback``) which properly awaits ``call_llm``.
    """
    from app.services.synexia import report_synthesis

    prev_obs = _get_previous_observation(db, execution)
    rows: list = []
    sql = None
    if prev_obs and prev_obs.success and prev_obs.result_data:
        result = prev_obs.result_data
        if isinstance(result, dict):
            sql = result.get("sql")
            data = result.get("data")
            if isinstance(data, list):
                rows = data

    user_message = execution.user_message or ""
    task_spec = execution.task_spec or {}
    entities = task_spec.get("entities", {}) or {}
    source_name = entities.get("source_name") or entities.get("data_source")
    source_id = entities.get("source_id")

    try:
        finalize_result = _run_coro_sync(report_synthesis.synthesize_report(
            user_message=user_message,
            rows=rows,
            sql=sql,
            source_name=source_name,
            source_id=source_id,
            call_llm_fn=_synth_call_llm,
        ))
        result_data = _finalize_result_to_result_data(finalize_result)
        summary_text = result_data.get("summary", "")
        return _record_observation(
            db, execution, node,
            observation_type="synthesize",
            tool_name="synthesizer",
            request_args={"user_message": user_message},
            result_data=result_data,
            result_text=summary_text,
            success=True,
        )
    except Exception as e:
        logger.warning(
            "Rich synthesize_report failed, falling back to legacy LLM call: %s", e,
        )
        return _legacy_synthesize_fallback(db, execution, node, user_message, task_spec, rows, sql)
```

Then **add the legacy fallback** immediately after `_execute_synthesize_node` (before `_execute_sandbox_node`):

```python
def _legacy_synthesize_fallback(db, execution, node, user_message, task_spec, rows, sql) -> ObservationRecord:
    """Pre-Phase-2 simple synthesize path, used when ``synthesize_report`` raises.

    Mirrors the original ``_execute_synthesize_node`` logic but correctly
    awaits the async ``call_llm`` via ``_run_coro_sync`` (the prior code
    called it without await, silently always falling through to the stub).
    Produces the same ``result_data`` shape, with ``report_card_payload=None``.
    """
    import json
    from app.services.llm_service import call_llm

    entities = task_spec.get("entities", {}) or {}
    nl2sql_context = ""
    if sql:
        nl2sql_context = (
            f"SQL executed:\n{sql}\n\nReturned {len(rows)} rows."
            if rows else f"SQL generated:\n{sql}"
        )

    system_prompt = (
        f"You are the REPORT SYNTHESIZER for a data-driven chat agent.\n\n"
        f"User request: {user_message}\n"
        f"Entities: {json.dumps(entities, ensure_ascii=False)}\n"
        f"{nl2sql_context}\n\n"
        f"Write a 1-2 paragraph executive summary that a Presentation "
        f"Designer will use as **instructions** to build a professional "
        f"report/document. Include:\n"
        f"1. The title and scope of the report\n"
        f"2. Key metrics or KPIs to highlight (with values if known)\n"
        f"3. The narrative flow (executive summary, findings, recommendations)\n"
        f"4. Any specific formatting or style instructions\n\n"
        f"Respond with a JSON object:\n"
        f"{{\n"
        f'  "title": "Report title",\n'
        f'  "summary": "Executive summary narrative (2-3 sentences)",\n'
        f'  "instructions": "Detailed instructions for the Presentation Designer",\n'
        f'  "kpis": [{{"label": "KPI name", "value": "KPI value", "caption": "context"}}],\n'
        f'  "chart": {{"type": "bar", "title": "...", "x_key": "...", "y_keys": ["..."], "data": [...], "unit": "..."}} | null,\n'
        f'  "insights": [{{"icon": "trending-up", "text": "..."}}]\n'
        f"}}\n"
        f"Respond with ONLY the JSON object, no surrounding text."
    )

    try:
        result = _run_coro_sync(call_llm(
            prompt=system_prompt, messages=[], temperature=0.3,
        ))
        response_text = result.get("response", "").strip()
        if "```json" in response_text:
            response_text = response_text.split("```json")[1].split("```")[0]
        elif "```" in response_text:
            response_text = response_text.split("```")[1].split("```")[0]
        synth_data = json.loads(response_text) if response_text else {}
    except Exception as e:
        logger.warning("Legacy synthesize fallback LLM call failed: %s", e)
        synth_data = {
            "title": entities.get("report_title", "Report"),
            "summary": f"Analysis of {entities.get('metric', 'data')}.",
            "instructions": "Generate a professional report from the provided data.",
            "kpis": [], "chart": None, "insights": [],
        }

    summary_text = synth_data.get("summary", "")
    instructions_text = synth_data.get("instructions", synth_data.get("summary", ""))
    return _record_observation(
        db, execution, node,
        observation_type="synthesize",
        tool_name="synthesizer",
        request_args={"user_message": user_message},
        result_data={
            "summary": summary_text,
            "instructions": instructions_text,
            "synth_data": synth_data,
            "report_card_payload": None,
        },
        result_text=summary_text,
        success=True,
    )
```

- [ ] **Step 8: Run all Task 1 tests to verify they pass**

Run:
```bash
cd /root/zhanlu/backend && python3 -m pytest tests/test_fsm_synthesize_rich.py -v
```
Expected: PASS — converter + rich path + fallback all green.

- [ ] **Step 9: Commit**

```bash
cd /root/zhanlu/backend && git add app/services/synexia/capability_router.py tests/test_fsm_synthesize_rich.py
git commit -m "feat(synexia): wire rich report_synthesis into FSM synthesize path (fixes un-awaited call_llm)"
```

---

## Task 2: Surface `ReportCardPayload` in FINALIZE for non-file data tasks

**Files:**
- Modify: `backend/app/services/synexia/fsm.py:720-755` (FINALIZE, after the file-export block)
- Test: `backend/tests/test_fsm_synthesize_rich.py` (append)

**Interfaces:**
- Consumes: the synthesize observation's `result_data["report_card_payload"]` (produced by Task 1).
- Produces: FINALIZE sets `report_card_payload` (on `ExecutionResult`, `fsm.py:832`) from the synthesize observation when no file-export artifact was produced, so the frontend renders structured KPIs/charts for non-file data tasks (`analyze_data` without export).

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_fsm_synthesize_rich.py`:

```python
class TestFinalizeSurfacesSynthesizePayload:
    def test_non_file_data_task_surfaces_synthesize_rcp(self, monkeypatch):
        """When no file-export artifact is produced, FINALIZE surfaces the
        synthesize observation's report_card_payload so the frontend renders
        a structured ReportCard instead of plain text."""
        from app.services.synexia.fsm import ExecutionRequest, SynexiaFSM
        from app.config import settings

        # An execution whose user_signal is NOT export_* (non-file data task)
        # and whose synthesize obs carries a report_card_payload.
        synth_rcp = {"title": "Q2 Sales", "kpis": [{"label": "Revenue", "value": "189M"}]}
        synth_obs = SimpleNamespace(
            observation_type="synthesize", success=True, tool_name="synthesizer",
            request_args={}, result_data={"summary": "s", "instructions": "i",
                                          "synth_data": {}, "report_card_payload": synth_rcp},
            result_text="s", artifact_ids=None,
        )
        tool_obs = SimpleNamespace(
            observation_type="tool_call", success=True, tool_name="ask_data_agent",
            request_args={}, result_data={"rows": []}, result_text="ok", artifact_ids=None,
        )

        f = SynexiaFSM.__new__(SynexiaFSM)
        f.db = MagicMock()
        f.execution = SimpleNamespace(
            id="exec-1",
            observations=[tool_obs, synth_obs],
            task_spec={"user_signal": "default"},  # NOT export_*
            context_manifest=None,
            confidence_factors={},
            confidence_score=0.8,
            assistant_content="done",
            current_state="finalize",
        )
        f.plan = None
        f._last_verify_result = None

        # Disable quality eval / gate side effects for this focused test.
        monkeypatch.setattr(settings, "SYNEXIA_QUALITY_EVAL_ENABLED", False)
        monkeypatch.setattr(settings, "SYNEXIA_QUALITY_GATE_ENABLED", False)

        req = ExecutionRequest(conversation_id="c1", agent_name="a",
                               user_message="analyze Q2 sales")
        result = f._finalize(req)  # _finalize is the FINALIZE entry; see note below

        assert result.report_card_payload == synth_rcp

    def test_export_task_keeps_artifact_rcp_over_synthesize(self, monkeypatch):
        """When a file-export artifact IS produced, its rcp wins (synthesize
        rcp is NOT surfaced on top)."""
        from app.services.synexia.fsm import ExecutionRequest, SynexiaFSM
        from app.config import settings

        artifact_rcp = {"title": "Exported DOCX", "kpis": []}
        synth_rcp = {"title": "Synth (must NOT win)", "kpis": []}
        synth_obs = SimpleNamespace(
            observation_type="synthesize", success=True, tool_name="synthesizer",
            request_args={}, result_data={"summary": "s", "instructions": "i",
                                          "synth_data": {}, "report_card_payload": synth_rcp},
            result_text="s", artifact_ids=None,
        )

        f = SynexiaFSM.__new__(SynexiaFSM)
        f.db = MagicMock()
        f.execution = SimpleNamespace(
            id="exec-2",
            observations=[synth_obs],
            task_spec={"user_signal": "export_docx"},
            context_manifest=None, confidence_factors={}, confidence_score=0.8,
            assistant_content="done", current_state="finalize",
        )
        f.plan = None
        f._last_verify_result = None

        monkeypatch.setattr(settings, "SYNEXIA_QUALITY_EVAL_ENABLED", False)
        monkeypatch.setattr(settings, "SYNEXIA_QUALITY_GATE_ENABLED", False)
        # Stub the file-export finalize to return an artifact + its rcp.
        def fake_finalize_into_artifact(*a, **kw):
            artifact = SimpleNamespace(id="art-1")
            return artifact, {"docx": {"artifact_id": "art-1"}}, _to_payload(artifact_rcp)
        monkeypatch.setattr(
            "app.services.synexia.fsm.fsm_finalize_into_artifact",
            fake_finalize_into_artifact,
        )

        req = ExecutionRequest(conversation_id="c1", agent_name="a",
                               user_message="export Q2 report as docx")
        result = f._finalize(req)
        assert result.report_card_payload["title"] == "Exported DOCX"
```

**Note on `_finalize` / `_to_payload`:** before finalizing this step, grep `fsm.py` for the FINALIZE method name (it is the method containing the `report_card_payload = None` init at `:702` and the `return ExecutionResult(...)` at `:823`). If it is named `_finalize`, use that; otherwise substitute the real name. `_to_payload` must wrap the dict as a `ReportCardPayload` (the file-export path returns a `ReportCardPayload` instance, then `.model_dump()` is called at `:736`); define a tiny helper in the test: `from app.services.synexia.contracts import ReportCardPayload; def _to_payload(d): return ReportCardPayload(**d)`. Adjust fields so the `ReportCardPayload(**d)` construction is valid (the `title` field is the only required one).

- [ ] **Step 2: Run the test to verify it fails**

Run:
```bash
cd /root/zhanlu/backend && python3 -m pytest tests/test_fsm_synthesize_rich.py::TestFinalizeSurfacesSynthesizePayload -v
```
Expected: FAIL — `result.report_card_payload` is `None` for the non-file task (FINALIZE does not yet surface the synthesize rcp).

- [ ] **Step 3: Implement the FINALIZE surfacing**

In `backend/app/services/synexia/fsm.py`, in the FINALIZE method, **immediately after the file-export block** (after the `except Exception as e:` that closes the `if user_signal.startswith("export_"):` block, ~line 754, and before the `# Plan summary for ActivityRail` comment at line 756), insert:

```python
        # ── Non-file data tasks: surface the synthesize ReportCardPayload ──
        # File-export tasks already set report_card_payload from the artifact
        # finalize above. For analyze-data tasks (no export), surface the
        # synthesize node's structured payload so the frontend renders a
        # ReportCard (KPIs/chart/insights) instead of plain text. Spec §4.2.
        if report_card_payload is None:
            for obs in observations:
                if (
                    getattr(obs, "observation_type", "") == "synthesize"
                    and getattr(obs, "success", False)
                ):
                    _rcp = (getattr(obs, "result_data", None) or {}).get("report_card_payload")
                    if isinstance(_rcp, dict) and _rcp:
                        report_card_payload = _rcp
                        break
```

- [ ] **Step 4: Run the Task 2 tests to verify they pass**

Run:
```bash
cd /root/zhanlu/backend && python3 -m pytest tests/test_fsm_synthesize_rich.py -v
```
Expected: PASS — both FINALIZE surfacing tests green; Task 1 tests still green.

- [ ] **Step 5: Run the existing FSM suite to confirm no regression**

Run:
```bash
cd /root/zhanlu/backend && python3 -m pytest tests/test_fsm_synthesize_rich.py tests/test_fsm_quality_eval.py tests/test_fsm_reflexion_finalize.py tests/test_response_prompt_grounding.py tests/test_grounding_extractor.py -v
```
Expected: PASS — no regression across Phase 1 + Phase 2 + existing FSM tests.

- [ ] **Step 6: Commit**

```bash
cd /root/zhanlu/backend && git add app/services/synexia/fsm.py tests/test_fsm_synthesize_rich.py
git commit -m "feat(synexia): surface synthesize ReportCardPayload in FINALIZE for non-file data tasks"
```

---

## Phase 2 done — definition of done

- `_execute_synthesize_node` runs `synthesize_report` (sync-wrapped) and converts `FinalizeResult` → `result_data` (sandbox-backward-compat + `report_card_payload`).
- Latent bug fixed: `call_llm` is now properly awaited (via `_run_coro_sync`) on both the rich path and the legacy fallback.
- FINALIZE surfaces the synthesize `report_card_payload` for non-file data tasks; file-export tasks keep the artifact rcp.
- All Phase 2 tests pass; Phase 1 + existing FSM suite unbroken.
- Zero net extra LLM calls (replaces the existing synthesize call; the legacy fallback only runs on rare exceptions).
- Deploy: `docker restart zhanlu-backend`.

## Follow-up plan (separate)

- **Phase 3** — (3a) `generate_plan` emits `inputs` per node; (3b) `_get_all_data_observations` aggregator; (3c) `adaptive_planner.py` (opt-in `SYNEXIA_ADAPTIVE_PLANNING_ENABLED`, default off, `MAX_REVISIONS=2`).
