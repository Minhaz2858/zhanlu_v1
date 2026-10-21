# CAD P0 — Foundation Implementation Plan

> **For Hermes:** execute task-by-task; each task is a full TDD cycle.

**Goal:** unblock the operational roadmap — parametric edit with parsed
read-back + contract awareness, the missing `fusion360-mcp` skill doc, and
PDF drawing export.

**Architecture:** all new tools follow the existing granular-tool pattern
(`registry.register` + handler `(args, db, user_id)` + `_run(code, db)`
snippet execution on the Fusion bridge). Tests unit-test handler logic with
monkeypatched `_run` — no live Fusion bridge needed.

**Tech Stack:** Python 3.11, FastAPI backend, pytest, ezdxf (+matplotlib for
PDF), registry pattern in `app/services/tool_registry.py`.

Source roadmap: `docs/superpowers/specs/2026-10-19-cad-agent-operational-roadmap.md`

---

### Task 1: `fusion360_parameter_set` — failing tests

**Objective:** pin the update-tool contract: update-only (never creates),
exactly one of `value_mm`/`expression`, parsed mm read-back, not-found is a
retryable error.

**Files:**
- Create: `backend/tests/test_fusion360_parameter_set.py`

**Step 1: Write failing tests**

```python
"""fusion360_parameter_set — dedicated parametric UPDATE tool (P0).

user_parameter is create-or-update; parameter_set is UPDATE-ONLY with a
parsed mm read-back and contract-prefix awareness. Pins the roadmap P0
foundation: parameters must be changeable by name and re-verified.
"""
import pytest

from app.services.tool_handlers import fusion360_parametric as fp


@pytest.fixture
def bridge(monkeypatch):
    """Capture the snippet and fake the bridge response per test."""
    calls = {}

    def _fake_run(code, db):
        calls["code"] = code
        return calls.get("response", {"success": True, "stdout": ""})

    monkeypatch.setattr(fp, "_run", _fake_run)
    return calls


def _ok_stdout(name="c1a2b3c4_thk", raw_cm="0.4"):
    return f"PARAM_SET_OK {name}={raw_cm}"


def test_rejects_missing_name(bridge):
    out = fp._fusion360_parameter_set({"value_mm": 4}, None, "u")
    assert not out["success"] and "name" in out["error"]


def test_rejects_value_and_expression_together(bridge):
    out = fp._fusion360_parameter_set(
        {"name": "p", "value_mm": 4, "expression": "4 mm"}, None, "u")
    assert not out["success"] and "exactly one" in out["error"]


def test_rejects_neither_value_nor_expression(bridge):
    out = fp._fusion360_parameter_set({"name": "p"}, None, "u")
    assert not out["success"] and "exactly one" in out["error"]


def test_rejects_non_numeric_value_mm(bridge):
    out = fp._fusion360_parameter_set({"name": "p", "value_mm": "abc"}, None, "u")
    assert not out["success"] and "value_mm" in out["error"]


def test_update_by_value_mm_parses_readback(bridge):
    bridge["response"] = {"success": True, "stdout": _ok_stdout(raw_cm="0.4")}
    out = fp._fusion360_parameter_set({"name": "c1a2b3c4_thk", "value_mm": 4}, None, "u")
    assert out["success"] is True
    assert out["value_mm"] == 4.0
    assert "p.expression = '4 mm'" in bridge["code"]
    assert "itemByName(" in bridge["code"]


def test_update_by_expression_passes_through(bridge):
    bridge["response"] = {"success": True, "stdout": _ok_stdout()}
    out = fp._fusion360_parameter_set(
        {"name": "p", "expression": "40 + stroke_pos * 110"}, None, "u")
    assert out["success"] is True
    assert "'40 + stroke_pos * 110'" in bridge["code"]


def test_not_found_is_error_with_params_list_hint(bridge):
    bridge["response"] = {"success": True, "stdout": "PARAM_SET_ERR not_found"}
    out = fp._fusion360_parameter_set({"name": "nope", "value_mm": 4}, None, "u")
    assert not out["success"] and "fusion360_params_list" in out["error"]


def test_bridge_failure_propagates(bridge):
    bridge["response"] = {"success": False, "error": "bridge offline"}
    out = fp._fusion360_parameter_set({"name": "p", "value_mm": 4}, None, "u")
    assert out["success"] is False and "bridge offline" in out["error"]


def test_name_quote_rejected(bridge):
    out = fp._fusion360_parameter_set({"name": "p'x", "value_mm": 4}, None, "u")
    assert not out["success"]


def test_registered_in_tool_registry():
    from app.services.tool_registry import registry
    assert registry.get("fusion360_parameter_set") is not None
```

**Step 2: Run to verify failure**

Run: `docker exec -e PYTHONPATH=/app zhanlu-backend python -m pytest tests/test_fusion360_parameter_set.py -q`
Expected: FAIL — `fusion360_parameter_set` not defined / not registered.

### Task 2: implement `fusion360_parameter_set` + registration

**Objective:** the handler + schema + registry entry in `fusion360_parametric.py`.

**Files:**
- Modify: `backend/app/services/tool_handlers/fusion360_parametric.py` (append after `_fusion360_declare_spec` block, before "Schemas & registration"; add schema + registry.register at the end)

**Step 1: implement** (key handler shape):

```python
def _param_set_code(name: str, expression: str) -> str:
    """Fusion snippet: UPDATE an existing user parameter's expression."""
    return (
        MM
        + f"p = design.userParameters.itemByName({name!r})\n"
        + "if p is None:\n"
        + "    print('PARAM_SET_ERR not_found')\n"
        + "else:\n"
        + f"    p.expression = {expression!r}\n"
        + "    print('PARAM_SET_OK ' + p.name + '=' + str(p.value))\n"
    )


def _fusion360_parameter_set(args, db, user_id):
    """UPDATE an existing user parameter (never creates). ..."""
    name = str(args.get("name") or "").strip()
    if not name:
        return _bad("name is required")
    if "'" in name:
        return _bad("name must not contain a single quote")
    value_mm = args.get("value_mm")
    expression = args.get("expression")
    if (value_mm is None) == (expression is None):
        return _bad("provide exactly one of value_mm or expression")
    if value_mm is not None:
        try:
            v = float(value_mm)
            if v != v or v in (float("inf"), float("-inf")):
                raise ValueError
        except (TypeError, ValueError):
            return _bad("value_mm must be a finite number")
        expression = f"{v} mm"
    code = _apply_component(_param_set_code(name, str(expression).strip()),
                            args.get("component_index"))
    r = _run(code, db)
    if not r.get("success"):
        return r
    stdout = r.get("stdout", "") or ""
    if "PARAM_SET_ERR not_found" in stdout:
        return _bad(
            f"parameter '{name}' not found — call fusion360_params_list to "
            "see existing parameters (this tool never creates)"
        )
    value = None
    for line in stdout.splitlines():
        if line.startswith("PARAM_SET_OK "):
            _, _, raw = line[len("PARAM_SET_OK "):].partition("=")
            try:
                value = round(float(raw) * 10.0, 4)  # Fusion cm → mm
            except (TypeError, ValueError):
                value = raw
    return {
        "success": True, "name": name, "value_mm": value,
        "message": (f"Parameter '{name}' updated to {expression}. Re-run "
                    "fusion360_verify_build to re-validate against the contract."),
    }
```

Plus `FUSION360_PARAMETER_SET_SCHEMA` and `registry.register(...)` (name
`fusion360_parameter_set`, category/toolset `"cad"`, description
"UPDATE an existing user parameter by name (mm value or expression); never
creates; parsed mm read-back.").

**Step 2: run tests** — same command, expect 10 passed.
**Step 3: contract-mode touch** — `fusion360_declare_spec`'s returned message
should mention `fusion360_parameter_set` for updates (1-line message edit +
update the file docstring).

### Task 3: `SKILL.md` for fusion360-mcp

**Objective:** write the planned skill (Tier-1 spec §7): the
declare→build→verify→probe loop, pitfalls (sketch closure, positional
indices, shared document, serial execution), granular-vs-execute_python
policy, parameter workflow (`params_list` → `parameter_set` → `verify_build`).

**Files:**
- Create: `backend/skills/fusion360-mcp/SKILL.md`

**Verify:** file exists; `grep -c "fusion360" SKILL.md` > 10.

### Task 4: PDF drawing export

**Objective:** `fusion360_make_drawing` gains `format: "pdf"|"dxf"` (default
dxf), rendering via ezdxf's drawing add-on (matplotlib backend).

**Files:**
- Modify: `backend/app/services/tool_handlers/fusion360_io.py`
- Test: `backend/tests/test_fusion360_drawing_pdf.py`

**Step 1:** check matplotlib availability in the container
(`python -c "import matplotlib"`). If absent: add to requirements and rebuild
image — or fall back to SVG via ezdxf's svg backend (no extra dep).
**Step 2:** failing test — call the drawing builder with a tiny spec, assert
a PDF/SVG byte payload is returned with the right magic header
(`%PDF` / `<svg`).
**Step 3:** implement; **Step 4:** tests pass; schema updated with `format`
enum.

### Task 5: restart backend + live smoke

`docker compose restart backend` → run the full new test files → confirm
registry lists the new tools:
`docker exec -e PYTHONPATH=/app zhanlu-backend python -c "from app.services.tool_registry import registry as r; print([t for t in r.list_names() if 'parameter' in t or 'drawing' in t])"`
