# CAD Agent Tier 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the CAD Agent build parametric, contract-driven, constraint-closed models instead of drawing baked-number shapes.

**Architecture:** Three coordinated changes in the Fusion tool-handler layer: (A) dimensions become named Fusion userParameters (codegen already supports `createByString` — the string branch of `_mm_expr` is live, just unused); (B) a new `cad_build_contracts` table + `fusion360_declare_spec` tool records design intent per conversation, and `verify_build` validates geometry against the STORED contract; (C) a shared sketch-closure snippet adds coincident constraints and reports profile counts, and extrude refuses to run on a sketch with no closed profile.

**Tech Stack:** Python 3.11, FastAPI backend (dockerized `zhanlu-backend`), SQLAlchemy 2.0 (generic JSON type, `Base.metadata.create_all` — NO alembic), Fusion 360 add-in socket bridge (`host.docker.internal:9876`), pytest.

**Decisions (approved):** parameter names prefixed per contract `c<contract_id8>_<name>`; `cad_build_contracts` rows cascade-delete with the conversation (FK `ondelete="CASCADE"`).

---

### Task 1: `cad_build_contracts` model + registration

**Files:**
- Create: `backend/app/models/cad_build_contract.py`
- Modify: `backend/app/models/__init__.py` (add import)
- Test: `backend/tests/test_cad_build_contract.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_cad_build_contract.py
from app.models import CadBuildContract
from app.models.base import TimestampedBase


def test_contract_table_registered():
    # TimestampedBase.metadata knows every imported model — the table must exist.
    assert "cad_build_contracts" in TimestampedBase.metadata.tables


def test_contract_columns():
    t = TimestampedBase.metadata.tables["cad_build_contracts"]
    cols = {c.name: c for c in t.columns}
    for required in ("id", "org_id", "app_id", "conversation_id", "agent_id",
                     "contract_json", "created_date"):
        assert required in cols, f"missing column {required}"
    # conversation cascade: deleting the conversation row removes the contract
    fk = t.columns["conversation_id"].foreign_keys
    assert fk and list(fk)[0].ondelete == "CASCADE"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker exec -i zhanlu-backend bash -lc 'cd /app && python -m pytest tests/test_cad_build_contract.py -v 2>&1 | tail -8'`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.models.cad_build_contract'`

- [ ] **Step 3: Create the model**

```python
# backend/app/models/cad_build_contract.py
"""CadBuildContract — design-intent contract for CAD agent builds.

Stored per conversation; verify_build validates live geometry against the
STORED contract (never the model's hand-picked verify args). Rows cascade
with the conversation (hard-delete rule).
"""
import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class CadBuildContract(TimestampedBase):
    __tablename__ = "cad_build_contracts"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    org_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    app_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    conversation_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("agent_conversations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    contract_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
```

- [ ] **Step 4: Register in models/__init__.py**

Append (alphabetical, after `agent_conversation` import):
```python
from app.models.cad_build_contract import CadBuildContract
```

- [ ] **Step 5: Run test to verify it passes**

Run: `docker exec -i zhanlu-backend bash -lc 'cd /app && python -m pytest tests/test_cad_build_contract.py -v 2>&1 | tail -8'`
Expected: PASS (2 passed)

- [ ] **Step 6: Commit**

```bash
git add backend/app/models/cad_build_contract.py backend/app/models/__init__.py backend/tests/test_cad_build_contract.py
git commit -m "feat(cad): cad_build_contracts model with conversation cascade"
```

---

### Task 2: `fusion360_parametric.py` — `params_list` + `declare_spec` tools

**Files:**
- Create: `backend/app/services/tool_handlers/fusion360_parametric.py`
- Modify: `backend/app/services/tool_handlers/__init__.py` (import module so registration runs)
- Modify: `backend/app/routers/agents.py` (ACTIVITY_STEP_DESCRIPTIONS — Task 7)
- Test: `backend/tests/test_fusion360_parametric.py`

Context pattern (already used by fusion360_io.py:86-90):
```python
from app.services.agent_tools import TOOL_CONTEXT_VAR
ctx = TOOL_CONTEXT_VAR.get() or {}
conversation_id = ctx.get("conversation_id")
app_id = ctx.get("agent_app_id")
```

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_fusion360_parametric.py
from app.services.tool_handlers.fusion360_parametric import (
    _params_list_code, _validate_spec, _contract_prefix,
)


def test_params_list_code_has_name_value_loop():
    code = _params_list_code()
    assert "userParameters" in code
    assert "PARAM_NAME" in code and "PARAM_VALUE" in code


def test_validate_spec_rejects_bad():
    assert _validate_spec({"part": "", "features": []}) is not None          # missing part
    assert _validate_spec({"part": "x", "features": []}) is not None         # empty features
    assert _validate_spec({"part": "x", "features": [{"kind": "hex"}]}) is None  # ok


def test_contract_prefix_stable_and_short():
    p1 = _contract_prefix("12345678-abcd-4ef0-9aab-000000000001")
    assert len(p1) == 9 and p1.startswith("c")  # c<8 hex>
    assert _contract_prefix("12345678-abcd-4ef0-9aab-000000000001") == p1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker exec -i zhanlu-backend bash -lc 'cd /app && python -m pytest tests/test_fusion360_parametric.py -v 2>&1 | tail -8'`
Expected: FAIL — ModuleNotFoundError

- [ ] **Step 3: Write the module**

```python
# backend/app/services/tool_handlers/fusion360_parametric.py
"""Parametric + design-intent tools for the CAD agent (Tier 1).

- fusion360_params_list  — read the document's userParameter table (name=value)
- fusion360_declare_spec — store a design-intent contract for this conversation
"""
from __future__ import annotations

import uuid

from app.services.tool_registry import registry
from app.services.tool_handlers.fusion360_granular import MM, _apply_component, _bad, _run
from app.services.tool_handlers.fusion360_tool import _call


def _params_list_code() -> str:
    """Snippet: print every document parameter as name=value (value in cm).
    Fusion stores values in internal cm; the handler converts to mm for output."""
    return (
        MM
        + "up = design.userParameters\n"
        + "for i in range(up.count):\n"
        + "    p = up.item(i)\n"
        + "    print('PARAM', p.name, p.value)\n"
        + "print('PARAM_COUNT', up.count)\n"
    )


def _fusion360_params_list(args, db, user_id):
    r = _run(_apply_component(_params_list_code(), args.get("component_index")), db)
    if not r["success"]:
        return r
    params = []
    for line in (r.get("stdout") or "").splitlines():
        line = line.strip()
        if line.startswith("PARAM "):
            parts = line.split()
            if len(parts) >= 3:
                try:
                    params.append({"name": parts[1], "value_mm": round(float(parts[2]) * 10.0, 4)})
                except ValueError:
                    continue
    return {"success": True, "params": params,
            "stdout": r.get("stdout", ""), "count": len(params)}


def _validate_spec(args: dict) -> str | None:
    """Return an error string if the spec args are unusable, else None."""
    part = (args.get("part") or "").strip()
    features = args.get("features")
    if not part:
        return "part is required (e.g. 'm6_bolt')"
    if not isinstance(features, list) or not features:
        return "features must be a non-empty list of {kind, ...} objects"
    for i, f in enumerate(features):
        if not isinstance(f, dict) or not (f.get("kind") or "").strip():
            return f"features[{i}] must be an object with a 'kind' (e.g. hex, cylinder, box)"
    return None


def _contract_prefix(contract_id: str) -> str:
    """Short build-id prefix for parameter names: c + first 8 hex chars."""
    return "c" + (contract_id or "").replace("-", "")[:8]


def _fusion360_declare_spec(args, db, user_id):
    from app.models.cad_build_contract import CadBuildContract
    err = _validate_spec(args)
    if err:
        return _bad(err)
    from app.services.agent_tools import TOOL_CONTEXT_VAR
    ctx = TOOL_CONTEXT_VAR.get() or {}
    contract_id = str(uuid.uuid4())
    contract = CadBuildContract(
        id=contract_id,
        org_id=ctx.get("org_id"),
        app_id=ctx.get("agent_app_id") or ctx.get("app_id"),
        conversation_id=ctx.get("conversation_id"),
        agent_id=ctx.get("agent_app_id"),
        contract_json={
            "part": (args.get("part") or "").strip(),
            "features": args.get("features"),
            "notes": args.get("notes") or "",
        },
    )
    db.add(contract)
    db.commit()
    prefix = _contract_prefix(contract_id)
    return {
        "success": True,
        "contract_id": contract_id,
        "param_prefix": prefix,
        "message": (
            f"Contract {contract_id[:8]} stored. Define parameters with the "
            f"'{prefix}_<name>' prefix (e.g. {prefix}_head_width) via "
            "fusion360_user_parameter, then verify with "
            "fusion360_verify_build(contract_id='use_last')."
        ),
    }


def _schema(name, description, properties, required):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


registry.register(
    name="fusion360_params_list",
    schema=_schema(
        "fusion360_params_list",
        "List every user parameter currently defined in the Fusion document "
        "(name + value in mm). Call this BEFORE an update to see the parameter "
        "names to change, and after defining parameters to confirm they exist.",
        {"component_index": {"type": "integer", "description": "Optional component index."}},
        [],
    ),
    handler=_fusion360_params_list,
    category="cad", toolset="cad", enabled_by_default=True,
    description="List document user parameters (name, value_mm).",
    emoji="🔢",
)

registry.register(
    name="fusion360_declare_spec",
    schema=_schema(
        "fusion360_declare_spec",
        "Declare the design-intent contract for the CURRENT build BEFORE any "
        "modeling tool call. features = one object per sub-part/body with "
        "'kind' (hex, box, cylinder, sphere, torus, tube) plus its dimensions "
        "in mm (e.g. {kind:'hex', across_flats:8}, {kind:'cylinder', diameter:5, "
        "height:20}). The backend stores the contract and verify_build validates "
        "the live geometry against it — the model cannot fudge the spec. "
        "Returns contract_id + param_prefix; use param_prefix for parameter names.",
        {
            "part": {"type": "string", "description": "Part name, e.g. 'm6_bolt'."},
            "features": {
                "type": "array",
                "items": {"type": "object"},
                "description": "One object per body: {kind, ...dims in mm}.",
            },
            "notes": {"type": "string", "description": "Optional free-text note."},
        },
        ["part", "features"],
    ),
    handler=_fusion360_declare_spec,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Store the design-intent contract for the current build.",
    emoji="📐",
)
```

- [ ] **Step 4: Register the module in tool_handlers/__init__.py**

Add near the other fusion360 imports:
```python
from app.services.tool_handlers import fusion360_parametric  # noqa: F401
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `docker exec -i zhanlu-backend bash -lc 'cd /app && python -m pytest tests/test_fusion360_parametric.py -v 2>&1 | tail -8'`
Expected: PASS (3 passed)

- [ ] **Step 6: Verify registration + no import-swallow regression**

Run:
```bash
docker exec -i zhanlu-backend bash -lc 'cd /app && python -c "
import app.services.tool_handlers
from app.services.tool_registry import registry
all_t = registry.list_available()
print(\"total:\", len(all_t), \"| params_list:\", \"fusion360_params_list\" in all_t, \"| declare_spec:\", \"fusion360_declare_spec\" in all_t)"'
```
Expected: total 165, both new tools True. (If total dropped below 163, a module failed to import — see skill pitfall.)

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/tool_handlers/fusion360_parametric.py backend/app/services/tool_handlers/__init__.py backend/tests/test_fusion360_parametric.py
git commit -m "feat(cad): params_list + declare_spec parametric tools"
```

---

### Task 3: `verify_build` — parameter-value checks

**Files:**
- Modify: `backend/app/services/tool_handlers/fusion360_granular.py` (verify codegen + parsing, lines ~400-460)
- Test: `backend/tests/test_fusion360_verify_params.py`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_fusion360_verify_params.py
from app.services.tool_handlers.fusion360_granular import _parse_vparam_values, _check_expected_params


def test_parse_vparam_values():
    out = "VPARAMS: a, b\nVPARAM a 0.8\nVPARAM b 1.4\n"
    assert _parse_vparam_values(out) == {"a": 8.0, "b": 14.0}  # cm -> mm


def test_check_expected_params_str_backward_compat():
    # legacy string entries still mean "name must exist"
    assert _check_expected_params(["a"], {"a": 8.0}) == []


def test_check_expected_params_value_match():
    assert _check_expected_params([{"name": "a", "value": 8}], {"a": 8.0}) == []


def test_check_expected_params_value_mismatch():
    issues = _check_expected_params([{"name": "a", "value": 10}], {"a": 8.0})
    assert issues and "a" in issues[0] and "10" in issues[0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker exec -i zhanlu-backend bash -lc 'cd /app && python -m pytest tests/test_fusion360_verify_params.py -v 2>&1 | tail -8'`
Expected: FAIL — ImportError (functions don't exist)

- [ ] **Step 3: Change the verify codegen to print values**

In `_fusion360_verify_build` (fusion360_granular.py ~line 407), replace:
```python
    code = (
        "print('VERIFY_BODIES:', root.bRepBodies.count)\n"
        ...
        "up = design.userParameters\n"
        "names = ','.join(up.item(i).name for i in range(up.count))\n"
        "print('VPARAMS:', names)\n"
    )
```
with (values in cm; parsed to mm in Python):
```python
    code = (
        "print('VERIFY_BODIES:', root.bRepBodies.count)\n"
        ...  # unchanged body loop
        "up = design.userParameters\n"
        "names = ','.join(up.item(i).name for i in range(up.count))\n"
        "print('VPARAMS:', names)\n"
        "for i in range(up.count):\n"
        "    p = up.item(i)\n"
        "    print('VPARAM', p.name, p.value)\n"
    )
```

- [ ] **Step 4: Add the pure parsing/check functions (module level, same file)**

```python
def _parse_vparam_values(stdout: str) -> dict[str, float]:
    """Parse VPARAM name <value_cm> lines into {name: value_mm}."""
    out: dict[str, float] = {}
    for line in (stdout or "").splitlines():
        line = line.strip()
        if line.startswith("VPARAM "):
            parts = line.split()
            if len(parts) >= 3:
                try:
                    out[parts[1]] = round(float(parts[2]) * 10.0, 4)
                except ValueError:
                    continue
    return out


def _check_expected_params(expected_params: list, values: dict[str, float]) -> list[str]:
    """Each entry is a name str (exists check, legacy) or {name, value} dict.
    value is in mm; compared with 5% tolerance."""
    issues: list[str] = []
    for ep in expected_params or []:
        if isinstance(ep, str):
            if ep not in values:
                issues.append(f"missing parameter: {ep}")
        elif isinstance(ep, dict):
            name = ep.get("name")
            if name not in values:
                issues.append(f"missing parameter: {name}")
                continue
            expected = ep.get("value")
            if expected is None:
                continue
            actual = values[name]
            tol = max(abs(float(expected)) * 0.05, 0.01)
            if abs(actual - float(expected)) > tol:
                issues.append(
                    f"parameter {name} = {actual}mm != expected {expected}mm"
                )
    return issues
```

- [ ] **Step 5: Wire into `_fusion360_verify_build`**

In the parsing section (after `params = [...]` at ~line 437) add:
```python
        elif line.startswith("VPARAM "):
            continue  # consumed by _parse_vparam_values below
```
After the params loop, replace the existing `if expected_params:` block (lines ~455-458) with:
```python
    param_values = _parse_vparam_values(stdout)
    issues += _check_expected_params(expected_params, param_values)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `docker exec -i zhanlu-backend bash -lc 'cd /app && python -m pytest tests/test_fusion360_verify_params.py tests/test_fusion360_smoke_regression.py -v 2>&1 | tail -8'`
Note: if `test_fusion360_smoke_regression.py` does not exist yet, run only the new file.
Expected: PASS (4 passed)

- [ ] **Step 7: Run the existing verify tests (non-regression)**

Run: `docker exec -i zhanlu-backend bash -lc 'cd /app && python -m pytest tests/test_answer_verification.py tests/test_data_agent_verification.py -q 2>&1 | tail -3'`
Expected: all pass

- [ ] **Step 8: Commit**

```bash
git add backend/app/services/tool_handlers/fusion360_granular.py backend/tests/test_fusion360_verify_params.py
git commit -m "feat(cad): verify_build checks parameter values (expected_params {name, value})"
```

---

### Task 4: `verify_build` — contract mode (`contract_id="use_last"`)

**Files:**
- Modify: `backend/app/services/tool_handlers/fusion360_granular.py`
- Modify: `backend/app/services/tool_handlers/fusion360_validation.py` (if the hex-kind checker lives there; otherwise keep in granular)
- Test: `backend/tests/test_fusion360_contract_mode.py`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_fusion360_contract_mode.py
from app.services.tool_handlers.fusion360_granular import (
    _load_contract, _contract_issues, _feature_matches_body,
)

CONTRACT = {"part": "hex_bolt", "features": [
    {"kind": "hex", "across_flats": 8, "height": 14},
    {"kind": "cylinder", "diameter": 5, "height": 20},
]}
BODIES = [
    {"index": 0, "min": [-0.462, -0.400, 0.0], "max": [0.462, 0.400, 1.4], "faces": 14},
    {"index": 1, "min": [-0.250, -0.250, 1.4], "max": [0.250, 0.250, 3.4], "faces": 3},
]


def test_feature_matches_hex():
    b = BODIES[0]
    assert _feature_matches_body({"kind": "hex", "across_flats": 8, "height": 14}, b) == []


def test_feature_mismatch_hex_height():
    b = BODIES[0]
    issues = _feature_matches_body({"kind": "hex", "across_flats": 8, "height": 12}, b)
    assert any("height" in i for i in issues)


def test_contract_issues_ok():
    assert _contract_issues(CONTRACT, BODIES) == []


def test_contract_issues_missing_feature():
    # only one body declared kind matches: hex matches body0; cylinder needs a
    # 5mm-dia 20mm-tall body — none, so a per-feature failure must be reported
    issues = _contract_issues(CONTRACT, [BODIES[0]])
    assert issues and any("cylinder" in i for i in issues)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker exec -i zhanlu-backend bash -lc 'cd /app && python -m pytest tests/test_fusion360_contract_mode.py -v 2>&1 | tail -8'`
Expected: FAIL — ImportError

- [ ] **Step 3: Add contract loading + matching (pure functions, module level in fusion360_granular.py)**

```python
def _load_contract(db, contract_id: str, conversation_id: str | None) -> dict | None:
    """Resolve contract_id ('use_last' → newest for this conversation)."""
    from app.models.cad_build_contract import CadBuildContract
    q = db.query(CadBuildContract)
    if contract_id == "use_last":
        if not conversation_id:
            return None
        row = (
            q.filter(CadBuildContract.conversation_id == conversation_id)
            .order_by(CadBuildContract.created_date.desc())
            .first()
        )
    else:
        row = q.filter(CadBuildContract.id == contract_id).first()
    return row.contract_json if row else None


def _feature_matches_body(feature: dict, body: dict, tol_mm: float = 0.5) -> list[str]:
    """Compare ONE declared feature against ONE body's bbox (mm, from cm).
    kind hex: across_flats = min(width, depth); height = z span."""
    issues: list[str] = []
    kind = (feature.get("kind") or "").lower()
    w = (body["max"][0] - body["min"][0]) * 10.0
    d = (body["max"][1] - body["min"][1]) * 10.0
    h = (body["max"][2] - body["min"][2]) * 10.0
    if kind == "hex":
        across = min(w, d)
        if "across_flats" in feature and abs(across - float(feature["across_flats"])) > tol_mm:
            issues.append(f"hex across_flats {across:.1f}mm != declared {feature['across_flats']}mm")
        if "height" in feature and abs(h - float(feature["height"])) > tol_mm:
            issues.append(f"hex height {h:.1f}mm != declared {feature['height']}mm")
    elif kind == "cylinder":
        dia = (w + d) / 2.0
        if "diameter" in feature and abs(dia - float(feature["diameter"])) > tol_mm:
            issues.append(f"cylinder diameter {dia:.1f}mm != declared {feature['diameter']}mm")
        if "height" in feature and abs(h - float(feature["height"])) > tol_mm:
            issues.append(f"cylinder height {h:.1f}mm != declared {feature['height']}mm")
    else:
        issues.append(f"unsupported contract kind: {kind}")
    return issues


def _contract_issues(contract: dict, bodies: list[dict]) -> list[str]:
    """Greedy nearest-match: each feature must match at least one body."""
    issues: list[str] = []
    features = contract.get("features") or []
    used: set[int] = set()
    for i, feat in enumerate(features):
        best = None
        for j, body in enumerate(bodies):
            if j in used:
                continue
            fi = _feature_matches_body(feat, body)
            if not fi:
                best = j
                break
        if best is None:
            issues.append(
                f"contract feature[{i}] kind={feat.get('kind')} "
                f"({feat}) has no matching body"
            )
        else:
            used.add(best)
    return issues
```

- [ ] **Step 4: Wire contract mode into `_fusion360_verify_build`**

In `_fusion360_verify_build` signature add `contract_id` to args (it is a normal tool arg — the LLM passes `contract_id="use_last"` or an id). After `issues = []` is built from existing checks, add:
```python
    contract_arg = args.get("contract_id")
    if contract_arg:
        from app.services.agent_tools import TOOL_CONTEXT_VAR
        _ctx = TOOL_CONTEXT_VAR.get() or {}
        contract = _load_contract(db, contract_arg, _ctx.get("conversation_id"))
        if contract is None:
            issues.append(
                "no stored contract found for contract_id=%r — call "
                "fusion360_declare_spec first" % contract_arg
            )
        else:
            issues += _contract_issues(contract, bodies)
```
Update the tool schema description to document `contract_id` ("use_last" or a contract id; validates against the STORED contract).

- [ ] **Step 5: Run tests to verify they pass**

Run: `docker exec -i zhanlu-backend bash -lc 'cd /app && python -m pytest tests/test_fusion360_contract_mode.py -v 2>&1 | tail -8'`
Expected: PASS (4 passed)

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/tool_handlers/fusion360_granular.py backend/tests/test_fusion360_contract_mode.py
git commit -m "feat(cad): verify_build contract mode validates against stored spec"
```

---

### Task 5: sketch closure pass + profile reporting

**Files:**
- Create: `backend/app/services/tool_handlers/fusion360_sketch_common.py`
- Modify: `backend/app/services/tool_handlers/fusion360_granular.py` (polygon, rectangle, circle append closure)
- Modify: `backend/app/services/tool_handlers/fusion360_sketch2.py` (line, spline, arcs append closure)
- Test: `backend/tests/test_fusion360_sketch_closure.py`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_fusion360_sketch_closure.py
from app.services.tool_handlers.fusion360_sketch_common import (
    _CLOSE_SKETCH, _profile_report,
)


def test_closure_snippet_has_constraints_and_report():
    assert "addCoincident" in _CLOSE_SKETCH
    assert "PROFILES" in _CLOSE_SKETCH
    assert "WARN_NO_PROFILE" in _CLOSE_SKETCH


def test_profile_report_line():
    assert _profile_report(0).startswith("print('WARN_NO_PROFILE'")
    assert _profile_report(1).startswith("print('PROFILES 1'")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker exec -i zhanlu-backend bash -lc 'cd /app && python -m pytest tests/test_fusion360_sketch_closure.py -v 2>&1 | tail -8'`
Expected: FAIL — ModuleNotFoundError

- [ ] **Step 3: Write the closure helper**

```python
# backend/app/services/tool_handlers/fusion360_sketch_common.py
"""Sketch closure pass — makes drawn loops topologically closed.

Every sketch-entity tool appends this so profiles form reliably (the hexagon
drawn as 6 lines must become a CLOSED loop or extrude silently produces no
body — observed live 2026-08-28). Endpoints within EPS cm get an explicit
coincident constraint; the profile count is reported so callers know the
sketch is extrudable.
"""
from __future__ import annotations

_EPS = 1e-6

_CLOSE_SKETCH = """
# ---- closure pass ----
_eps = 1e-6
_pts = []
_segs = []
for _c in sk.sketchCurves:
    if hasattr(_c, 'startSketchPoint') and hasattr(_c, 'endSketchPoint'):
        _segs.append((_c.startSketchPoint, _c.endSketchPoint))
for _s, _e in _segs:
    _pts.append(_s)
    _pts.append(_e)
_cons = sk.geometricConstraints
for i in range(len(_pts)):
    for j in range(i + 1, len(_pts)):
        try:
            _d = _pts[i].geometry.vectorTo(_pts[j].geometry).length
            if _d < _eps:
                _cons.addCoincident(_pts[i], _pts[j])
        except Exception:
            pass
print('PROFILES', sk.profiles.count)
if sk.profiles.count == 0:
    print('WARN_NO_PROFILE')
"""


def _profile_report(count: int) -> str:
    if count == 0:
        return "print('WARN_NO_PROFILE')"
    return f"print('PROFILES {count}')"
```

- [ ] **Step 4: Append closure to sketch tools**

In `fusion360_granular.py`:
- import: `from app.services.tool_handlers.fusion360_sketch_common import _CLOSE_SKETCH`
- `_fusion360_sketch_polygon`: after the `lines.addByTwoPoints` loop and before `print('OK polygon')`, insert `+ _CLOSE_SKETCH + "\n"`.
- `_fusion360_sketch_rectangle` / `_fusion360_sketch_circle`: append `+ _CLOSE_SKETCH + "\n"` before the final `print(...)` line.

In `fusion360_sketch2.py`: same append for `sketch_line`, `sketch_spline`, `sketch_arc`, `sketch_arc_3point`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `docker exec -i zhanlu-backend bash -lc 'cd /app && python -m pytest tests/test_fusion360_sketch_closure.py -v 2>&1 | tail -8'`
Expected: PASS (2 passed)

- [ ] **Step 6: Import check (the __init__ swallow pitfall)**

Run:
```bash
docker exec -i zhanlu-backend bash -lc 'cd /app && python -c "
import app.services.tool_handlers
from app.services.tool_registry import registry
print(\"fusion tools:\", len([t for t in registry.list_available() if t.startswith(\"fusion360\")]))"'
```
Expected: 52 (was 50; +params_list +declare_spec)

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/tool_handlers/fusion360_sketch_common.py backend/app/services/tool_handlers/fusion360_granular.py backend/app/services/tool_handlers/fusion360_sketch2.py backend/tests/test_fusion360_sketch_closure.py
git commit -m "feat(cad): sketch closure pass — coincident constraints + profile reporting"
```

---

### Task 6: extrude guard — no closed profile = loud retryable error

**Files:**
- Modify: `backend/app/services/tool_handlers/fusion360_granular.py` (`_fusion360_extrude`, ~line 248)
- Test: `backend/tests/test_fusion360_extrude_guard.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_fusion360_extrude_guard.py
from app.services.tool_handlers.fusion360_granular import _extrude_guard_code


def test_extrude_guard_code_checks_profiles_before_extrude():
    code = _extrude_guard_code(0)
    assert "profiles.count" in code
    assert "NO_PROFILE_ERROR" in code
    assert code.index("profiles.count") < code.index("createInput") or "createInput" not in code
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker exec -i zhanlu-backend bash -lc 'cd /app && python -m pytest tests/test_fusion360_extrude_guard.py -v 2>&1 | tail -8'`
Expected: FAIL — ImportError

- [ ] **Step 3: Add the guard snippet + wire it**

```python
def _extrude_guard_code(sketch_index: int) -> str:
    """Pre-flight: bail with a clear error if the sketch has no closed profile."""
    return (
        f"sk = root.sketches.item({int(sketch_index)})\n"
        + "if sk.profiles.count == 0:\n"
        + "    print('NO_PROFILE_ERROR')\n"
        + "    raise SystemExit(1)\n"
    )
```

In `_fusion360_extrude`, replace the first code line:
```python
    code = MM + f"sk = root.sketches.item({int(si)})\n"
```
with:
```python
    code = MM + _extrude_guard_code(si)
```

And in `_run`'s error path (`_fusion360_extrude` already checks `r["success"]`), translate the marker:
```python
    if not r["success"]:
        return r
    if "NO_PROFILE_ERROR" in (r.get("stdout") or ""):
        return _bad(
            "the sketch has no closed profile — its curves are not joined into "
            "a loop. Re-run the sketch tool (it auto-closes endpoints), then "
            "retry the extrude. Do NOT draw a new sketch on top."
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker exec -i zhanlu-backend bash -lc 'cd /app && python -m pytest tests/test_fusion360_extrude_guard.py -v 2>&1 | tail -8'`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/tool_handlers/fusion360_granular.py backend/tests/test_fusion360_extrude_guard.py
git commit -m "feat(cad): extrude refuses sketches with no closed profile"
```

---

### Task 7: activity descriptions + CAD agent system prompt

**Files:**
- Modify: `backend/app/routers/agents.py` (ACTIVITY_STEP_DESCRIPTIONS)
- Modify: DB row `agent_apps.prompt_identity` for agent `2c23ded7-addc-417a-981c-e8cc6e6b15ff`

- [ ] **Step 1: Add activity descriptions**

In `ACTIVITY_STEP_DESCRIPTIONS` (agents.py ~line 3836) after the CAD block:
```python
    "fusion360_params_list":     "Reading the model's parameter table",
    "fusion360_declare_spec":    "Declaring the design-intent contract",
```

- [ ] **Step 2: Add the Tier-1 workflow to the system prompt**

Run (exact SQL; appends the parametric workflow to prompt_identity):
```bash
docker exec zhanlu-postgres psql -U zhanlu -d zhanlu -c "
UPDATE agent_apps
SET prompt_identity = prompt_identity || E'\n\nTIER-1 PARAMETRIC WORKFLOW (MANDATORY for BUILD requests):\n1. Before ANY modeling tool, call fusion360_declare_spec with the part name and one feature object per body {kind, ...dims in mm}. It returns contract_id and param_prefix.\n2. Define a user parameter for EVERY spec dimension, named with the contract prefix: <param_prefix>_head_width, <param_prefix>_shank_len, etc. (fusion360_user_parameter creates-or-updates; values in mm).\n3. Pass the PARAMETER NAME (string) instead of a number to dimension args of feature tools (extrude distance_mm, cylinder diameter_mm, etc.) — the model stays parametric and editable.\n4. Verify with fusion360_verify_build(contract_id=\"use_last\", expected_params=[{name:\"...\", value:<mm>}, ...]). The backend checks live geometry against the STORED contract — never invent a verify list that contradicts the declared spec.\n5. UPDATES: read fusion360_params_list, change the parameter (fusion360_user_parameter with the same name), re-verify. NEVER stack a new feature to change a dimension.\n'
WHERE id = '2c23ded7-addc-417a-981c-e8cc6e6b15ff';
"
```

- [ ] **Step 3: Verify the prompt took**

Run: `docker exec zhanlu-postgres psql -U zhanlu -d zhanlu -t -A -c "SELECT prompt_identity LIKE '%TIER-1 PARAMETRIC WORKFLOW%' FROM agent_apps WHERE id='2c23ded7-addc-417a-981c-e8cc6e6b15ff';"`
Expected: `t`

- [ ] **Step 4: Restart backend + confirm boot + registry**

Run:
```bash
docker restart zhanlu-backend && sleep 15
docker logs zhanlu-backend --tail 3
docker exec -i zhanlu-backend bash -lc 'cd /app && python -c "
import app.services.tool_handlers
from app.services.tool_registry import registry
print(\"fusion tools:\", len([t for t in registry.list_available() if t.startswith(\"fusion360\")]))"'
```
Expected: startup complete; fusion tools = 52

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/agents.py
git commit -m "feat(cad): tier-1 activity labels + parametric workflow prompt"
```

---

### Task 8: golden smoke suite (throwaway document, T1-T5)

**Files:**
- Create: `backend/scripts/cad_smoke.py` (mirrors the pattern from the session's throwaway-doc test)

- [ ] **Step 1: Write the suite**

Key requirements (full script in the PR — this is the skeleton):
```python
"""CAD golden smoke suite — runs in a THROWAWAY document, never the user's.
Requires: Fusion 360 add-in running (socket host.docker.internal:9876).
Run: docker exec -i zhanlu-backend bash -lc 'cd /app && python scripts/cad_smoke.py'
"""
# T1 parametric build: declare_spec(hex_bolt, [{hex across_flats 8, height 10}])
#   → user_parameter c<pfx>_head_w=8, c<pfx>_h=10 → sketch_create → sketch_polygon
#   (circumradius = head_w/1.732 via param name string) → extrude(distance_mm="c<pfx>_h")
#   → params_list asserts values → verify_build(contract_id="use_last") PASS
# T2 update-without-stacking: user_parameter same name value=14 → verify PASS →
#   assert body count unchanged (no new body = no feature stacking)
# T3 negative: raw _call draws a 5-line hex (one side missing) → extrude must
#   return success=False with "no closed profile"
# T4 negative contract: declare_spec across_flats=12 (wrong) → verify FAIL with
#   measured-vs-declared issue
# T5 non-regression: plain numeric hex prism (no params) still builds + verifies
# Cleanup: close the throwaway document WITHOUT saving (doc.close(False))
```

- [ ] **Step 2: Run the suite with Fusion live**

Run: `docker exec -i zhanlu-backend bash -lc 'cd /app && python scripts/cad_smoke.py 2>&1 | tail -25'`
Expected: `==== N/5 scenarios PASSED ====` with all PASS. If any FAIL, fix the tool code (not the test) and re-run.

- [ ] **Step 3: Confirm the user's document is untouched**

The suite must assert before/after: `app.documents` count and the original document's body/face counts are identical (the throwaway doc is closed without saving).

- [ ] **Step 4: Commit**

```bash
git add backend/scripts/cad_smoke.py
git commit -m "test(cad): golden smoke suite T1-T5 (throwaway doc)"
```

---

### Task 9: full regression + skill update

- [ ] **Step 1: Full backend test suite**

Run: `docker exec -i zhanlu-backend bash -lc 'cd /app && python -m pytest -q 2>&1 | tail -5'`
Expected: no new failures vs the pre-change baseline (77 relevant tests + new ones pass; known pre-existing failures unchanged — see memory).

- [ ] **Step 2: Update the fusion360-mcp skill**

Add item 32 to `~/.hermes/skills/mcp/fusion360-mcp/SKILL.md`:
- Parametric workflow (declare_spec → prefixed params → name-string dims → verify use_last)
- Closure pass + extrude guard behavior
- The `TOOL_CONTEXT_VAR` pattern for conversation-scoped handlers
- The 52-tool registry expectation after Tier 1

- [ ] **Step 3: Final commit**

```bash
git add -A && git commit -m "chore(cad): tier 1 complete — parametric + contract + closed sketches"
```

---

## Self-Review Notes (run before handing off)

1. **Spec coverage:** Component A (params) → Tasks 2, 3, 7. Component B (contract) → Tasks 1, 2, 4, 7. Component C (closure) → Tasks 5, 6. Testing → Task 8. Error handling table → Tasks 4, 6 + `_bad` paths. All spec sections covered.
2. **Placeholder scan:** Task 8 is intentionally a skeleton (full script in the PR) — flag to the executor that Task 8 requires writing the complete script per the listed scenarios before running it. All other tasks have complete code.
3. **Type consistency:** `_check_expected_params(expected_params, values)` used identically in Task 3; `_load_contract(db, contract_id, conversation_id)` / `_contract_issues(contract, bodies)` / `_feature_matches_body(feature, body)` consistent across Task 4; `_CLOSE_SKETCH` imported in both granular and sketch2 (Task 5). `contract_id` arg name matches schema + prompt in Tasks 4/7.
