# Automation Response Quality — Phase 1 (Grounding + Criteria) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ground the FINALIZE response prompt in real observation data + acceptance criteria so the response generator writes specific, data-grounded replies instead of generic "I've processed your request" ones — at zero extra LLM calls.

**Architecture:** A new pure, deterministic `grounding_extractor.py` distills each observation type (nl2sql/synthesize/sandbox/tool) into a compact metadata+data block with a global char cap (metadata preserved over data). `_build_response_prompt` (shared by the blocking and streaming response paths) is rewired to use the grounded block plus a `You must satisfy` block rendered by the existing `quality_eval._format_acceptance_criteria` helper, so the generator and the evaluator share one definition of success.

**Tech Stack:** Python 3.11, SQLAlchemy ORM models, pydantic `Settings` config, pytest (sqlite test DB, explicit file paths, no `-n` workers).

**Spec:** `docs/superpowers/specs/2026-07-29-automation-response-quality-design.md` (Phase 1, §3).

## Scope note

This plan covers **Phase 1 only** — it produces working, independently testable software
(grounding + criteria-aware generation, 0 extra LLM calls). Phases 2 (rich FSM synthesis)
and 3 (plan-args + multi-obs + adaptive re-planning) each get their own follow-up plan,
per the writing-plans scope-check (each phase is an independently deployable subsystem).

## Global Constraints

- **RAM-limited dev box:** run only the specific test files by explicit path; never `python -m pytest tests/` or broad `-k`; no `-n` workers. Check `free -h` first if unsure.
- **Test DB:** `cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)"` then `python3 -m pytest tests/<file> -v`.
- **Config convention:** new flags are pydantic `Settings` fields in `app/config.py` named `SYNEXIA_*`, read at call sites via `getattr(settings, "SYNEXIA_X", default)` (matches existing `SYNEXIA_QUALITY_EVAL_*` pattern).
- **Defensive reads:** all observation field reads use `getattr(..., default)` / `.get()` — observation objects may be partial in tests or detached executions (matches existing style at `fsm.py:853-855`).
- **No new dependencies.** No schema changes. Backend changes require `docker restart zhanlu-backend` after deploy (reload is off to protect in-flight runs).
- **Commit only when the user asks.**

## File Structure

- **Create:** `backend/app/services/synexia/grounding_extractor.py` — pure deterministic per-obs-type grounding extractor with char cap. One responsibility: turn observations into a compact findings string.
- **Modify:** `backend/app/config.py` (near the `SYNEXIA_QUALITY_EVAL_*` block ~line 311) — add `SYNEXIA_GROUNDING_MAX_CHARS: int = 2000`.
- **Modify:** `backend/app/services/synexia/fsm.py:839-904` (`_build_response_prompt`) — replace the thin `obs_summaries` loop with the grounded block + inject the `You must satisfy` criteria block.
- **Test:** `backend/tests/test_grounding_extractor.py` — per-type extraction + char cap + defensive reads.
- **Test:** `backend/tests/test_response_prompt_grounding.py` — `_build_response_prompt` includes grounded values + criteria block; kpis fallback; shared-path.

---

## Task 1: Grounding extractor module

**Files:**
- Create: `backend/app/services/synexia/grounding_extractor.py`
- Test: `backend/tests/test_grounding_extractor.py`

**Interfaces:**
- Produces: `extract_grounding(observations: Iterable[Any], *, max_chars: int | None = None) -> str`. Returns a multi-line findings string, or `"No actions taken."` when the iterable is empty/falsy. `max_chars` defaults to `settings.SYNEXIA_GROUNDING_MAX_CHARS` (2000). Under the cap, **data lines are dropped before metadata lines** (rows truncated first, metadata last).

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_grounding_extractor.py`:

```python
"""Tests for the deterministic grounding extractor (Phase 1, G1).

The extractor distills each observation type into a compact metadata+data
block so the FINALIZE response generator writes grounded, specific replies.
Pure & deterministic — no LLM calls.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.services.synexia.grounding_extractor import extract_grounding


def _obs(**kw):
    """Build a minimal ObservationRecord-like object."""
    base = dict(seq=1, observation_type="tool_call", tool_name=None,
                success=True, result_data=None, result_text=None,
                error_message=None, artifact_ids=None)
    base.update(kw)
    return SimpleNamespace(**base)


class TestNl2sqlExtraction:
    def test_nl2sql_includes_sql_rows_columns_and_aggregates(self):
        obs = _obs(
            seq=2, observation_type="nl2sql", tool_name="nl2sql",
            result_data={
                "sql": "SELECT material, revenue FROM sales WHERE q='Q2'",
                "data": [
                    {"material": "Steel", "revenue": 120},
                    {"material": "Aluminium", "revenue": 60},
                    {"material": "Copper", "revenue": 30},
                    {"material": "Zinc", "revenue": 9},  # beyond top-3
                ],
            },
        )
        out = extract_grounding([obs], max_chars=10000)
        assert "#2" in out and "nl2sql" in out
        assert "SELECT material, revenue" in out           # SQL present
        assert "rows: 4" in out                            # row count
        assert "Steel" in out and "Aluminium" in out       # top-3 rows
        assert "Zinc" not in out                           # 4th row dropped (top-3)
        # aggregate stats on the numeric column
        assert "revenue: sum=219" in out
        assert "min=9" in out and "max=120" in out

    def test_nl2sql_truncates_long_sql(self):
        long_sql = "SELECT " + ", ".join(f"c{i}" for i in range(200))
        obs = _obs(observation_type="nl2sql", result_data={"sql": long_sql, "data": []})
        out = extract_grounding([obs], max_chars=10000)
        # SQL capped at 400 chars (the _SQL_MAX_CHARS constant)
        sql_line = [ln for ln in out.splitlines() if ln.strip().startswith("sql:")][0]
        assert len(sql_line) <= 400 + len("  sql: ")


class TestSynthesizeExtraction:
    def test_synthesize_includes_kpis_insights_chart(self):
        obs = _obs(
            observation_type="synthesize",
            result_data={
                "title": "Q2 Sales Report",
                "kpis": [{"label": "Revenue", "value": "189M CNY"}],
                "chart": {"type": "bar", "title": "Revenue by material"},
                "insights": [{"icon": "trending-up", "text": "Steel leads at 55%"}],
            },
        )
        out = extract_grounding([obs], max_chars=10000)
        assert "Q2 Sales Report" in out
        assert "kpi Revenue: 189M CNY" in out
        assert "chart: Revenue by material" in out
        assert "Steel leads at 55%" in out


class TestSandboxExtraction:
    def test_sandbox_includes_artifact_meta(self):
        obs = _obs(
            observation_type="sandbox", artifact_ids=["abc123"],
            result_data={"artifact_type": "docx", "title": "Q2 Report.docx"},
        )
        out = extract_grounding([obs], max_chars=10000)
        assert "artifact: docx" in out
        assert "Q2 Report.docx" in out
        assert "abc123" in out


class TestToolExtraction:
    def test_tool_truncates_result_text(self):
        obs = _obs(observation_type="tool_call", tool_name="web_search",
                   result_text="x" * 1000)
        out = extract_grounding([obs], max_chars=10000)
        # result capped at 500 chars (_TOOL_RESULT_MAX_CHARS)
        result_line = [ln for ln in out.splitlines() if "result:" in ln][0]
        assert len(result_line) <= 500 + len("  result: ")

    def test_tool_falls_back_to_result_data(self):
        obs = _obs(observation_type="tool_call", result_text=None,
                   result_data={"summary": "found 3 records"})
        out = extract_grounding([obs], max_chars=10000)
        assert "found 3 records" in out


class TestCharCapAndOrdering:
    def test_empty_observations_returns_no_actions(self):
        assert extract_grounding([]) == "No actions taken."
        assert extract_grounding(None) == "No actions taken."

    def test_metadata_preserved_over_data_under_cap(self):
        # Two nl2sql obs; cap tiny so data rows must drop but metadata stays.
        obs = _obs(
            seq=1, observation_type="nl2sql",
            result_data={"sql": "SELECT 1", "data": [{"a": "ROWDATA"}] * 5},
        )
        out = extract_grounding([obs], max_chars=120)
        # metadata survives
        assert "sql: SELECT 1" in out
        assert "rows: 5" in out
        # at least some data dropped to fit the cap
        assert out.count("ROWDATA") < 5

    def test_failed_observation_shows_error(self):
        obs = _obs(success=False, error_message="connection refused")
        out = extract_grounding([obs], max_chars=10000)
        assert "FAILED" in out
        assert "connection refused" in out


class TestDefensiveReads:
    def test_partial_observation_does_not_raise(self):
        # No result_data, no result_text, no tool_name — must not raise.
        obs = SimpleNamespace(seq=1, observation_type="tool_call")
        out = extract_grounding([obs], max_chars=10000)
        assert "#1" in out  # header still rendered
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
cd /root/zhanlu/backend && python3 -m pytest tests/test_grounding_extractor.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.synexia.grounding_extractor'`.

- [ ] **Step 3: Write the implementation**

Create `backend/app/services/synexia/grounding_extractor.py`:

```python
"""Deterministic grounding extractor for the FINALIZE response prompt.

Distills each observation into a compact, token-bounded block so the
response generator writes grounded, specific responses ("Q2 revenue was
189M CNY, top material at 35% share") instead of generic ones ("I've
processed your request").

Pure & deterministic — no LLM calls.  All field reads are defensive
(observation objects may be partial in tests or detached executions).

Char-budget policy (spec §3.1): the assembled block is capped to
``SYNEXIA_GROUNDING_MAX_CHARS`` (default 2000).  Under the cap, **data
lines are dropped before metadata lines** — columns/counts/aggregate
stats are higher-signal-per-char than raw rows, so rows truncate first.
"""
from __future__ import annotations

import json
from typing import Any, Iterable

# Per-contribution caps applied before the global cap.
_SQL_MAX_CHARS = 400
_TOOL_RESULT_MAX_CHARS = 500
_TOP_ROWS = 3
_ROW_LINE_MAX_CHARS = 200


def extract_grounding(
    observations: Iterable[Any],
    *,
    max_chars: int | None = None,
) -> str:
    """Build a compact findings block from observations.

    Args:
        observations: iterable of ObservationRecord-like objects (may be
            partial; all fields read defensively).
        max_chars: overall char cap. Defaults to
            ``settings.SYNEXIA_GROUNDING_MAX_CHARS`` (2000).

    Returns:
        A multi-line string with one ``=== #<seq> <tool> (ok|FAILED) ===``
        block per observation, or ``"No actions taken."`` when empty.
        Under the cap, data lines are dropped from the bottom before any
        metadata line is removed.
    """
    if max_chars is None:
        from app.config import settings
        max_chars = getattr(settings, "SYNEXIA_GROUNDING_MAX_CHARS", 2000)

    if not observations:
        return "No actions taken."

    parts = [_extract_one(obs) for obs in observations]

    # Metadata lines first (one batch across all obs), then data lines —
    # so a cap shortfall drops data (rows) before metadata.
    meta_lines: list[str] = []
    data_lines: list[str] = []
    for p in parts:
        meta_lines.append(p["header"])
        for m in p["meta"]:
            meta_lines.append("  " + m)
    for p in parts:
        for d in p["data"]:
            data_lines.append("  " + d)

    # Drop data lines from the bottom until we fit (metadata preserved).
    while data_lines and len("\n".join(meta_lines + data_lines)) > max_chars:
        data_lines.pop()

    body = "\n".join(meta_lines + data_lines)
    if not body.strip():
        return "No actions taken."
    return body[:max_chars]


# ── Per-obs-type extractors ───────────────────────────────────────────────

def _extract_one(obs: Any) -> dict:
    """Return ``{'header', 'meta', 'data'}`` for one observation."""
    otype = (getattr(obs, "observation_type", "") or "").lower()
    tool = getattr(obs, "tool_name", None) or otype or "action"
    seq = getattr(obs, "seq", "?")
    success = getattr(obs, "success", False)
    header = f"=== #{seq} {tool} ({'ok' if success else 'FAILED'}) ==="

    if not success:
        err = getattr(obs, "error_message", None) or "failed"
        return {"header": header, "meta": [f"error: {err}"], "data": []}

    dispatch = {
        "nl2sql": _extract_nl2sql,
        "synthesize": _extract_synthesize,
        "sandbox": _extract_sandbox,
    }
    fn = dispatch.get(otype, _extract_tool)
    meta, data = fn(obs)
    return {"header": header, "meta": meta, "data": data}


def _result_data_dict(obs: Any) -> dict:
    rd = getattr(obs, "result_data", None)
    return rd if isinstance(rd, dict) else {}


def _extract_nl2sql(obs: Any) -> tuple[list[str], list[str]]:
    rd = _result_data_dict(obs)
    sql = str(rd.get("sql") or "")[:_SQL_MAX_CHARS]
    rows = rd.get("data") or []
    if not isinstance(rows, list):
        rows = []
    cols = rd.get("columns")
    if not cols and rows and isinstance(rows[0], dict):
        cols = list(rows[0].keys())
    cols = cols or []

    meta: list[str] = []
    if sql:
        meta.append(f"sql: {sql}")
    meta.append(f"rows: {len(rows)}")
    if cols:
        meta.append(f"columns: {', '.join(map(str, cols))}")
    for col in cols:
        vals = _numeric_values(rows, col)
        if vals:
            meta.append(
                f"{col}: sum={sum(vals):.4g} min={min(vals):.4g} max={max(vals):.4g}"
            )

    data: list[str] = []
    for row in rows[:_TOP_ROWS]:
        if isinstance(row, dict):
            data.append(json.dumps(row, ensure_ascii=False, default=str)[:_ROW_LINE_MAX_CHARS])
    return meta, data


def _extract_synthesize(obs: Any) -> tuple[list[str], list[str]]:
    rd = _result_data_dict(obs)
    meta: list[str] = []
    if rd.get("title"):
        meta.append(f"title: {rd['title']}")
    for kpi in rd.get("kpis") or []:
        if isinstance(kpi, dict):
            meta.append(f"kpi {kpi.get('label', '?')}: {kpi.get('value', '?')}")
    chart = rd.get("chart")
    if isinstance(chart, dict) and chart.get("title"):
        meta.append(f"chart: {chart['title']}")
    data = [
        f"insight: {i.get('text', '')}"
        for i in (rd.get("insights") or [])
        if isinstance(i, dict)
    ]
    return meta, data


def _extract_sandbox(obs: Any) -> tuple[list[str], list[str]]:
    rd = _result_data_dict(obs)
    meta: list[str] = []
    atype = rd.get("artifact_type") or rd.get("format")
    if atype:
        meta.append(f"artifact: {atype}")
    if rd.get("title"):
        meta.append(f"title: {rd['title']}")
    ids = getattr(obs, "artifact_ids", None) or []
    if ids:
        meta.append(f"artifact_ids: {', '.join(map(str, ids))}")
    return meta, []


def _extract_tool(obs: Any) -> tuple[list[str], list[str]]:
    text = getattr(obs, "result_text", None)
    if not text:
        rd = _result_data_dict(obs)
        text = rd.get("result") or rd.get("summary") or ""
    text = str(text)[:_TOOL_RESULT_MAX_CHARS]
    return [], [f"result: {text}"] if text else []


def _numeric_values(rows: list, col: str) -> list[float]:
    out: list[float] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        v = r.get(col)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            out.append(v)
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
cd /root/zhanlu/backend && python3 -m pytest tests/test_grounding_extractor.py -v
```
Expected: PASS — all tests green.

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu/backend && git add app/services/synexia/grounding_extractor.py tests/test_grounding_extractor.py
git commit -m "feat(synexia): add deterministic grounding extractor for response prompt"
```

---

## Task 2: Config knob `SYNEXIA_GROUNDING_MAX_CHARS`

**Files:**
- Modify: `backend/app/config.py` (near the `SYNEXIA_QUALITY_EVAL_*` block, ~line 315)

**Interfaces:**
- Produces: `settings.SYNEXIA_GROUNDING_MAX_CHARS: int = 2000` (read by `extract_grounding` when `max_chars` is not passed).

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_grounding_extractor.py`:

```python
class TestDefaultMaxCharsFromSettings:
    def test_uses_settings_default_when_max_chars_omitted(self, monkeypatch):
        monkeypatch.setattr(
            "app.config.settings.SYNEXIA_GROUNDING_MAX_CHARS", 80
        )
        obs = _obs(
            observation_type="nl2sql",
            result_data={"sql": "SELECT 1", "data": [{"a": "ROW"}] * 10},
        )
        out = extract_grounding([obs])  # no max_chars → reads settings
        assert len(out) <= 80
        # metadata still preserved under the tiny cap
        assert "sql: SELECT 1" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
cd /root/zhanlu/backend && python3 -m pytest tests/test_grounding_extractor.py::TestDefaultMaxCharsFromSettings -v
```
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'SYNEXIA_GROUNDING_MAX_CHARS'` (monkeypatch cannot set a non-existent attribute on the pydantic Settings instance → the `getattr` fallback returns 2000, so the `len(out) <= 80` assertion fails).

- [ ] **Step 3: Add the config field**

In `backend/app/config.py`, add immediately after the `SYNEXIA_QUALITY_EVAL_MAX_ITERATIONS` line (~line 315):

```python
    # Phase 1 response grounding: char cap for the findings block injected
    # into the FINALIZE response prompt (spec §3.3). Metadata is preserved
    # over data rows under the cap.
    SYNEXIA_GROUNDING_MAX_CHARS: int = 2000
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
cd /root/zhanlu/backend && python3 -m pytest tests/test_grounding_extractor.py -v
```
Expected: PASS — all tests green, including the settings-default test.

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu/backend && git add app/config.py tests/test_grounding_extractor.py
git commit -m "feat(synexia): add SYNEXIA_GROUNDING_MAX_CHARS config knob"
```

---

## Task 3: Wire grounding + criteria into `_build_response_prompt`

**Files:**
- Modify: `backend/app/services/synexia/fsm.py:839-904` (`_build_response_prompt`)
- Test: `backend/tests/test_response_prompt_grounding.py`

**Interfaces:**
- Consumes: `extract_grounding` (Task 1), `quality_eval._format_acceptance_criteria(task_spec: dict) -> str` (existing, `quality_eval.py:190`).
- Produces: `_build_response_prompt` now emits a grounded `=== Findings (from executed actions) ===` block (real data values) and a `=== You must satisfy ===` block (criteria, kpis fallback). Shared by blocking `_generate_response` (`fsm.py:906`) and streaming `stream_final_response` (`fsm.py:922`), so both paths get grounding automatically.

**Context — current code being replaced** (`fsm.py:850-861`, the thin summary loop):
```python
        obs_summaries = []
        if self.execution.observations:
            for obs in self.execution.observations:
                _tool = getattr(obs, "tool_name", None) or getattr(obs, "observation_type", "")
                if getattr(obs, "success", False):
                    obs_summaries.append(f"✓ {_tool}: {getattr(obs, 'result_text', None) or 'completed'}")
                else:
                    obs_summaries.append(f"✗ {_tool}: {getattr(obs, 'error_message', None) or 'failed'}")
        obs_text = "\n".join(obs_summaries) if obs_summaries else "No actions taken."
```
And the f-string at `fsm.py:898-904` uses `{obs_text}` at line 900. `task_spec` is defined at `fsm.py:866`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_response_prompt_grounding.py`:

```python
"""Tests that _build_response_prompt is grounded in real observation data
and acceptance criteria (Phase 1, G1 + G2).

_build_response_prompt is shared by the blocking _generate_response and the
streaming stream_final_response, so grounding both paths via one change.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.synexia.fsm import ExecutionRequest, SynexiaFSM


@pytest.fixture()
def fsm():
    f = SynexiaFSM.__new__(SynexiaFSM)
    f.db = None
    f.execution = SimpleNamespace(
        id="exec-test",
        observations=[],
        task_spec={},
        context_manifest=None,
    )
    return f


@pytest.fixture()
def exec_request():
    return ExecutionRequest(
        conversation_id="conv-test",
        agent_name="general_assistant",
        user_message="summarize Q2 sales",
    )


def _nl2sql_obs():
    return SimpleNamespace(
        seq=1, observation_type="nl2sql", tool_name="nl2sql", success=True,
        result_data={
            "sql": "SELECT material, revenue FROM sales WHERE q='Q2'",
            "data": [{"material": "Steel", "revenue": 120}],
        },
        result_text=None, error_message=None, artifact_ids=None,
    )


class TestGroundedFindings:
    def test_prompt_contains_real_observation_values(self, fsm, exec_request):
        fsm.execution.observations = [_nl2sql_obs()]
        prompt = fsm._build_response_prompt(exec_request)
        assert "Findings (from executed actions)" in prompt
        assert "Steel" in prompt              # real row value, not "completed"
        assert "revenue: sum=120" in prompt   # aggregate stat
        assert "SELECT material, revenue" in prompt  # SQL present

    def test_no_observations_keeps_no_actions_text(self, fsm, exec_request):
        fsm.execution.observations = []
        prompt = fsm._build_response_prompt(exec_request)
        assert "No actions taken" in prompt


class TestCriteriaInjection:
    def test_prompt_includes_acceptance_criteria(self, fsm, exec_request):
        fsm.execution.task_spec = {
            "acceptance_criteria": ["report must include revenue figures",
                                    "must cite the data source"],
        }
        prompt = fsm._build_response_prompt(exec_request)
        assert "You must satisfy" in prompt
        assert "report must include revenue figures" in prompt
        assert "must cite the data source" in prompt

    def test_falls_back_to_kpis_when_criteria_absent(self, fsm, exec_request):
        fsm.execution.task_spec = {"kpis": ["accuracy", "completeness"]}
        prompt = fsm._build_response_prompt(exec_request)
        assert "You must satisfy" in prompt
        assert "accuracy" in prompt
        assert "completeness" in prompt

    def test_no_criteria_no_satisfy_block(self, fsm, exec_request):
        fsm.execution.task_spec = {}
        prompt = fsm._build_response_prompt(exec_request)
        assert "You must satisfy" not in prompt


class TestSharedPath:
    def test_blocking_and_streaming_share_one_prompt_builder(self, fsm, exec_request):
        """_generate_response and stream_final_response both call
        _build_response_prompt, so grounding reaches both paths."""
        fsm.execution.observations = [_nl2sql_obs()]
        prompt = fsm._build_response_prompt(exec_request)
        # The blocking generator builds its system_prompt from this method
        # (fsm.py:910); the streaming path stashes the same prompt during
        # FINALIZE (fsm.py:922+). One assertion: the shared builder is grounded.
        assert "Findings (from executed actions)" in prompt
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
cd /root/zhanlu/backend && python3 -m pytest tests/test_response_prompt_grounding.py -v
```
Expected: FAIL — `test_prompt_contains_real_observation_values` fails (prompt contains `✓ nl2sql: completed`-style text, not `Steel` / `revenue: sum=120`); `test_prompt_includes_acceptance_criteria` fails (no `You must satisfy` block).

- [ ] **Step 3: Implement the wiring**

In `backend/app/services/synexia/fsm.py`, **replace the thin summary loop** (lines 850-861) with an import + grounding computation. Replace:

```python
        obs_summaries = []
        if self.execution.observations:
            for obs in self.execution.observations:
                # getattr defensively — observation objects may be partial
                # (e.g. in tests or detached executions).
                _tool = getattr(obs, "tool_name", None) or getattr(obs, "observation_type", "")
                if getattr(obs, "success", False):
                    obs_summaries.append(f"✓ {_tool}: {getattr(obs, 'result_text', None) or 'completed'}")
                else:
                    obs_summaries.append(f"✗ {_tool}: {getattr(obs, 'error_message', None) or 'failed'}")

        obs_text = "\n".join(obs_summaries) if obs_summaries else "No actions taken."
```

with:

```python
        from app.services.synexia.grounding_extractor import extract_grounding
        from app.services.synexia.quality_eval import _format_acceptance_criteria

        grounding = extract_grounding(self.execution.observations)
```

Then, **after the `followup_note` block** (after line 894, before the `return f"""` at 896), insert the criteria-block computation:

```python
        criteria_block = ""
        _criteria = _format_acceptance_criteria(task_spec) if task_spec else ""
        if _criteria and not _criteria.startswith("(none"):
            criteria_block = f"\n\n=== You must satisfy ===\n{_criteria}"
```

Finally, **update the f-string** (lines 898-904). Replace the `Actions taken:` / `{obs_text}` portion and the guidance sentence. Replace:

```python
User request: {request.user_message}{transcript_block}{followup_note}
Actions taken:
{obs_text}

Provide a concise, natural language response to the user. If actions were successful, summarize what was accomplished. If there were errors, explain what went wrong.
```

with:

```python
User request: {request.user_message}{transcript_block}{followup_note}
=== Findings (from executed actions) ===
{grounding}{criteria_block}

Provide a concise, natural language response to the user. Reference the actual findings above by value (figures, names, counts) — do not narrate tool calls. Your response must satisfy every criterion listed under "You must satisfy" (if any). If actions were successful, summarize what was accomplished. If there were errors, explain what went wrong.
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
cd /root/zhanlu/backend && python3 -m pytest tests/test_response_prompt_grounding.py tests/test_grounding_extractor.py -v
```
Expected: PASS — all grounding tests green.

- [ ] **Step 5: Run the existing FSM suite to confirm no regression**

Run:
```bash
cd /root/zhanlu/backend && python3 -m pytest tests/test_fsm_quality_eval.py tests/test_fsm_followup_context.py tests/test_fsm_reflexion_finalize.py -v
```
Expected: PASS — these existing tests call `_build_response_prompt` (directly or via `run_quality_eval`/`_generate_response`); the prompt shape change must not break them. If any assert on the old `Actions taken:` / `✓` text, update the assertion to the new grounded block (do not weaken the test's intent).

- [ ] **Step 6: Commit**

```bash
cd /root/zhanlu/backend && git add app/services/synexia/fsm.py tests/test_response_prompt_grounding.py
git commit -m "feat(synexia): ground FINALIZE response prompt in observation data + acceptance criteria"
```

---

## Phase 1 done — definition of done

- `extract_grounding` distills nl2sql/synthesize/sandbox/tool observations into a compact, char-capped findings block (metadata preserved over data).
- `_build_response_prompt` emits the grounded findings block + a `You must satisfy` criteria block (kpis fallback), shared by blocking + streaming paths.
- `SYNEXIA_GROUNDING_MAX_CHARS` config knob (default 2000).
- All new tests pass; existing FSM suite unbroken.
- Zero extra LLM calls per run.
- Deploy: `docker restart zhanlu-backend` (reload is off).

## Follow-up plans (separate, per scope-check)

- **Phase 2** — wire `report_synthesis.synthesize_report` into `_execute_synthesize_node` (sync-wrapped via `_run_coro_sync`, simple LLM call as fallback); surface `ReportCardPayload` in FINALIZE for non-file data tasks.
- **Phase 3** — (3a) `generate_plan` emits `inputs` per node; (3b) `_get_all_data_observations` aggregator; (3c) `adaptive_planner.py` (opt-in `SYNEXIA_ADAPTIVE_PLANNING_ENABLED`, default off, `MAX_REVISIONS=2`).
