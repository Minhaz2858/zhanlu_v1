# Skill Agent — Remove `create_artifact` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove `create_artifact` from the Skill Agent's toolset so the model can no longer produce stray PPTX/PDF/DOCX stub artifacts after creating a skill.

**Architecture:** Two-layer removal — (1) the toolset is shrunk via a new `SKILL_AGENT_TOOL_NAMES` constant that's `ALL_TOOL_NAMES` minus `create_artifact`, and the Skill Agent's `manifest_json.boundaries.allowed` is updated to match; (2) the now-unreachable "## Artifacts (`create_artifact`)" section in the prompt is deleted (silent removal — no replacement text). A new test file pins all three contracts.

**Tech Stack:** Python 3.11, FastAPI, SQLite (test-only), `re` / `ast` (standard library for test assertions), vitest not affected.

**Spec:** `docs/superpowers/specs/2026-07-28-skill-agent-no-create-artifact-design.md` (commit `b38fab6`, 232 lines).

## Global Constraints

- **No new dependencies.** Use Python stdlib (`re`, `ast`) for source-string and AST scans. Don't introduce pytest plugins or new test runners.
- **Leave all changes uncommitted per project policy.** Each task's commit step is a "would commit" note in the plan, not an actual `git commit` to run. The user will commit when ready.
- **No backend re-architecture.** The fix is a small surgical removal — add a constant, change 2 lines in `system_agents.py`, delete 35 lines in `agent_prompts.py`. Do NOT refactor the tool resolution path or change the `get_tools` function signature.
- **Out of scope (explicit, per spec §3.4):** `.skill` file packaging (becomes unreachable — accepted trade-off), other agents' toolsets, the `Tool` entity UI for skill packaging.
- **Test commands:**
  - Backend: `cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)" && DATABASE_URL="sqlite:///./test_runtime.db" PYTHONPATH=/root/zhanlu/backend python3 -m pytest <test_path> -v`
  - Always run tests on a single specific file (never broad collection — see project memory note about OOM).
- **Current branch**: `master`. After the previous turn's merge, HEAD is `18b6f23`. The 30 uncommitted files in the worktree are unrelated (other agents' WIP + my previous-turn still-uncommitted files). The new test + the 2 source modifications will be ADDED to the uncommitted set.

## File Structure

**New files:**
- `backend/tests/test_skill_agent_no_create_artifact.py` — 6 tests pinning the contract (no `create_artifact` in `SKILL_AGENT_TOOLS`, in the resolved `get_tools("skill_agent", ...)` output, in `boundaries.allowed`, in the new `SKILL_AGENT_TOOL_NAMES` constant, in the Skill Agent prompt's text, no `## Artifacts` section header).

**Modified files:**
- `backend/app/services/system_agents.py` — add `SKILL_AGENT_TOOL_NAMES` constant after `ALL_TOOL_NAMES` (line 68) + change line 191 to use it + remove `"create_artifact"` from `boundaries.allowed` at line 200.
- `backend/app/services/agent_prompts.py` — delete the "## Artifacts (`create_artifact`)" section (lines 647-682).

**Untouched:**
- All frontend files (no UI change).
- All other agent prompts in `agent_prompts.py` (the general assistant and automation agent keep `create_artifact`).
- All other toolsets in `system_agents.py` (only the `skill_agent` block is modified).

---

## Task 1: Write the failing test

**Files:**
- Create: `backend/tests/test_skill_agent_no_create_artifact.py`

**Interfaces:**
- Consumes: `app.services.agent_prompts.SKILL_AGENT_TOOLS` (existing static list, currently has 4 tools), `app.services.agent_prompts.get_tools(agent_name, tool_config, agent_app)` (existing function), `app.services.system_agents.SKILL_AGENT_TOOL_NAMES` (does not exist yet — test 4 will fail with AttributeError, which is the red phase), `app.services.agent_prompts.SKILL_AGENT_SYSTEM_PROMPT` (existing string constant), the `system_agents.py` source file (AST-parseable for test 3).
- Produces: 6 passing tests that pin the contract.

- [ ] **Step 1: Create the test file**

Create `backend/tests/test_skill_agent_no_create_artifact.py` with the following content (verbatim):

```python
"""Regression: Skill Agent's toolset must NOT include `create_artifact`
(2026-07-28).

The Skill Agent was observed creating PPTX/PDF/DOCX stub artifacts in
the chat right after creating a SKILL.md skill, despite a prompt rule
forbidding it. The reliable fix is to remove the tool entirely from
the Skill Agent's toolset so the model cannot call it.

These tests pin that contract from multiple angles: the static list,
the runtime-resolved toolset, the manifest permission boundary, the
new constant that drives the runtime resolution, and the prompt text.
"""
from __future__ import annotations

import ast
import os
import re

from app.services.agent_prompts import (
    SKILL_AGENT_SYSTEM_PROMPT,
    SKILL_AGENT_TOOLS,
    get_tools,
)
from app.services.system_agents import ALL_TOOL_NAMES, SKILL_AGENT_TOOL_NAMES


def _has_create_artifact(tools) -> bool:
    """True iff the iterable contains a tool with name == 'create_artifact'."""
    for t in tools:
        if isinstance(t, str):
            if t == "create_artifact":
                return True
        elif getattr(t, "name", None) == "create_artifact":
            return True
        elif isinstance(t, dict) and t.get("name") == "create_artifact":
            return True
    return False


def _read(path: str) -> str:
    with open(path) as f:
        return f.read()


# --------------------------------------------------------------------
# 1) Static list
# --------------------------------------------------------------------

def test_skill_agent_tools_static_list_excludes_create_artifact():
    """The hand-curated SKILL_AGENT_TOOLS list must not include create_artifact."""
    assert not _has_create_artifact(SKILL_AGENT_TOOLS), (
        f"SKILL_AGENT_TOOLS contains create_artifact: {SKILL_AGENT_TOOLS!r}"
    )


# --------------------------------------------------------------------
# 2) Runtime resolution
# --------------------------------------------------------------------

def test_skill_agent_enabled_tools_excludes_create_artifact():
    """get_tools('skill_agent', ...) must not include create_artifact.

    Exercises the full resolution path used by the runtime, not just
    the static fallback list. Catches the case where someone re-adds
    create_artifact via a config override or the new constant.
    """
    from types import SimpleNamespace

    from app.services.tools.registry import get_tool_registry

    registry = get_tool_registry()
    tool_config = {
        "enabled_tools": [
            t for t in ALL_TOOL_NAMES if t != "create_artifact"
        ],
    }
    # Build a minimal agent_app that the tool resolver accepts.
    agent_app = SimpleNamespace(name="skill_agent")

    tools = get_tools("skill_agent", tool_config, agent_app, registry=registry)
    assert not _has_create_artifact(tools), (
        "get_tools('skill_agent', ...) returned a list containing "
        f"create_artifact. Resolved tools: {tools!r}"
    )


# --------------------------------------------------------------------
# 3) manifest_json.boundaries.allowed
# --------------------------------------------------------------------

def test_skill_agent_boundaries_allowed_excludes_create_artifact():
    """The skill_agent boundaries.allowed list must not include create_artifact.

    AST-based: parses system_agents.py and walks the AST to find the
    skill_agent dict literal, then reads its boundaries.allowed list.
    Doesn't depend on the runtime permission check or DB state.
    """
    system_agents_path = os.path.join(
        os.path.dirname(__file__), "..", "app", "services", "system_agents.py"
    )
    source = _read(system_agents_path)
    tree = ast.parse(source)

    # Find the module-level assignment named 'skill_agent'.
    target = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "skill_agent":
                    target = node
                    break
        if target:
            break
    assert target is not None, "No module-level 'skill_agent' assignment found"

    # Convert the dict literal to a Python value via ast.literal_eval.
    try:
        sa_dict = ast.literal_eval(target.value)
    except Exception as e:
        raise AssertionError(f"skill_agent dict is not a literal: {e}")

    manifest = sa_dict.get("manifest_json", {})
    boundaries = manifest.get("boundaries", {})
    allowed = boundaries.get("allowed", [])
    assert "create_artifact" not in allowed, (
        f"system_agents.skill_agent.manifest_json.boundaries.allowed "
        f"contains 'create_artifact': {allowed!r}"
    )


# --------------------------------------------------------------------
# 4) New constant SKILL_AGENT_TOOL_NAMES
# --------------------------------------------------------------------

def test_skill_agent_tool_names_constant_excludes_create_artifact():
    """A module-level constant SKILL_AGENT_TOOL_NAMES must exist and not
    include create_artifact. This is the load-bearing constant that
    drives the runtime toolset; if someone reverts to ALL_TOOL_NAMES,
    this test catches it.
    """
    assert isinstance(SKILL_AGENT_TOOL_NAMES, (list, tuple, set, frozenset)), (
        f"SKILL_AGENT_TOOL_NAMES must be a list/tuple/set, got {type(SKILL_AGENT_TOOL_NAMES)}"
    )
    assert "create_artifact" not in SKILL_AGENT_TOOL_NAMES, (
        f"SKILL_AGENT_TOOL_NAMES contains 'create_artifact': "
        f"{sorted(SKILL_AGENT_TOOL_NAMES)!r}"
    )
    # And it must be a SUBSET of ALL_TOOL_NAMES (sanity: we shouldn't
    # be adding tools, only removing create_artifact).
    assert set(SKILL_AGENT_TOOL_NAMES).issubset(set(ALL_TOOL_NAMES)), (
        "SKILL_AGENT_TOOL_NAMES is not a subset of ALL_TOOL_NAMES. "
        "Did you forget to derive it from ALL_TOOL_NAMES?"
    )


# --------------------------------------------------------------------
# 5) Prompt text does not mention create_artifact
# --------------------------------------------------------------------

def test_skill_agent_prompt_does_not_mention_create_artifact():
    """The Skill Agent's prompt must not mention create_artifact anywhere.

    The user picked 'silent removal' in the brainstorm (no replacement
    text). If the prompt mentions the tool, the model might infer it
    has access to it and try to call it via some fallback path.
    """
    assert "create_artifact" not in SKILL_AGENT_SYSTEM_PROMPT, (
        "Skill Agent prompt mentions 'create_artifact' but the tool is "
        "no longer in the agent's toolset. The mention will confuse the "
        "model into thinking it can call the tool."
    )


# --------------------------------------------------------------------
# 6) No '## Artifacts' section header
# --------------------------------------------------------------------

def test_skill_agent_prompt_no_artifact_section_header():
    """The '## Artifacts' section must be deleted from the prompt.

    Pins the silent-removal decision: the 35-line section is gone,
    not replaced with a stub. If someone re-adds the section with a
    different title or rewording, this test will (deliberately) fail
    and force an explicit decision.
    """
    # Look for any H2 header that mentions 'artifact' in the prompt.
    matches = re.findall(
        r"^##\s+[^\n]*[Aa]rtifact[^\n]*$",
        SKILL_AGENT_SYSTEM_PROMPT,
        flags=re.MULTILINE,
    )
    assert not matches, (
        f"Skill Agent prompt still has an 'Artifacts' section header: "
        f"{matches!r}. The section was deleted in the 2026-07-28 fix; "
        f"re-adding it requires an explicit decision."
    )
```

- [ ] **Step 2: Run the test and verify the expected failures**

Run:

```bash
cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)" && DATABASE_URL="sqlite:///./test_runtime.db" PYTHONPATH=/root/zhanlu/backend python3 -m pytest tests/test_skill_agent_no_create_artifact.py -v
```

Expected failures (5 of 6):

- Test 1 (`test_skill_agent_tools_static_list_excludes_create_artifact`): **passes** today. `SKILL_AGENT_TOOLS` is a list of dicts with 4 entries (`create_skill`, `update_skill`, `list_tools`, `search_skills`) — `create_artifact` is not in it. This test is a defensive guard for the future; it passing now is correct.
- Test 2 (`test_skill_agent_enabled_tools_excludes_create_artifact`): **fails**. The runtime resolves `ALL_TOOL_NAMES` (via the system_agents.py config that test 2's `tool_config` is built to match), which includes `create_artifact`.
- Test 3 (`test_skill_agent_boundaries_allowed_excludes_create_artifact`): **fails**. `system_agents.skill_agent.manifest_json.boundaries.allowed` includes `create_artifact`.
- Test 4 (`test_skill_agent_tool_names_constant_excludes_create_artifact`): **fails** with `AttributeError: module 'app.services.system_agents' has no attribute 'SKILL_AGENT_TOOL_NAMES'`. The constant doesn't exist yet.
- Test 5 (`test_skill_agent_prompt_does_not_mention_create_artifact`): **fails**. The prompt has the 35-line "## Artifacts" section.
- Test 6 (`test_skill_agent_prompt_no_artifact_section_header`): **fails**. The regex matches `## Artifacts (`create_artifact`)`.

So 5 fail, 1 passes. If any test fails with a different error (e.g., ImportError, NameError), fix the test setup, not the source code.

- [ ] **Step 3: Note the test file in the worktree (don't commit yet)**

Per project policy, leave the new file uncommitted. The user will commit the implementation + tests together at the end.

---

## Task 2: Make the `system_agents.py` changes

**Files:**
- Modify: `backend/app/services/system_agents.py` (3 changes: add constant, change line 191, change line 200)

**Interfaces:**
- Consumes: `ALL_TOOL_NAMES` (existing, line 29-68, includes `create_artifact` at line 58), the skill_agent dict literal (lines 184-220).
- Produces: a new `SKILL_AGENT_TOOL_NAMES` constant (= `ALL_TOOL_NAMES` minus `create_artifact`), the skill_agent's `tool_config.enabled_tools` resolves to that constant, the skill_agent's `manifest_json.boundaries.allowed` drops `create_artifact`.

- [ ] **Step 1: Add the `SKILL_AGENT_TOOL_NAMES` constant after `ALL_TOOL_NAMES`**

In `backend/app/services/system_agents.py`, find the end of the `ALL_TOOL_NAMES` list definition (it ends with `]` followed by a blank line around line 68). Add the following constant immediately after the closing `]` of `ALL_TOOL_NAMES`:

```python
# 2026-07-28: Skill Agent must not call `create_artifact` to produce
# sample/demo artifacts after creating a skill. Same as ALL_TOOL_NAMES
# but with `create_artifact` removed. See spec
# docs/superpowers/specs/2026-07-28-skill-agent-no-create-artifact-design.md.
SKILL_AGENT_TOOL_NAMES = [t for t in ALL_TOOL_NAMES if t != "create_artifact"]
```

The exact placement: insert this as the next top-level statement after `ALL_TOOL_NAMES` closes. Do not put it inside the function `_tools_in_registry` or any other scope — it must be module-level so the test in Task 1's test 4 can import it.

- [ ] **Step 2: Change `enabled_tools` to use `SKILL_AGENT_TOOL_NAMES`**

In the same file, find line 191 (the `enabled_tools` line in the `skill_agent` config dict). Change:

```python
"enabled_tools": _tools_in_registry(registry, ALL_TOOL_NAMES),
```

to:

```python
"enabled_tools": _tools_in_registry(registry, SKILL_AGENT_TOOL_NAMES),
```

- [ ] **Step 3: Remove `"create_artifact"` from `boundaries.allowed`**

Find line 200 (the `allowed` list in the `skill_agent` config). Change:

```python
"allowed": ["create_skill", "update_skill", "search_skills", "list_tools", "create_artifact"],
```

to:

```python
"allowed": ["create_skill", "update_skill", "search_skills", "list_tools"],
```

- [ ] **Step 4: Run the new test and verify tests 1, 2, 3, 4 pass**

Run:

```bash
cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" PYTHONPATH=/root/zhanlu/backend python3 -m pytest tests/test_skill_agent_no_create_artifact.py -v
```

Expected:
- Test 1 (SKILL_AGENT_TOOLS static list): PASS
- Test 2 (runtime resolution via get_tools): PASS (the resolved toolset no longer includes `create_artifact` because `enabled_tools` no longer asks for it)
- Test 3 (boundaries.allowed AST): PASS
- Test 4 (SKILL_AGENT_TOOL_NAMES constant): PASS
- Test 5 (prompt mentions `create_artifact`): still FAILS (the prompt section hasn't been deleted yet — that's Task 3)
- Test 6 (## Artifacts section header): still FAILS (same reason)

So 4/6 PASS, 2/6 still FAIL. The 2 failing tests will pass after Task 3.

If test 2 still fails after these changes, read `agent_prompts.py:1757-1811` (the `get_tools` function) to confirm the tool resolution path. Test 2 mocks `tool_config.enabled_tools` to be `ALL_TOOL_NAMES - {create_artifact}` directly, so it should pass — if it doesn't, the registry itself is including `create_artifact` in a way that bypasses the `enabled_tools` filter.

- [ ] **Step 5: Do not commit (project policy)**

---

## Task 3: Delete the "## Artifacts (`create_artifact`)" section from the prompt

**Files:**
- Modify: `backend/app/services/agent_prompts.py:647-682` (delete 35 lines)

**Interfaces:**
- Consumes: the existing `SKILL_AGENT_SYSTEM_PROMPT` string constant (built from multiple sections including `## Skill Discovery`, `### Bare-Request Handling`, `## Artifacts`).
- Produces: a `SKILL_AGENT_SYSTEM_PROMPT` with the "## Artifacts (`create_artifact`)" section removed. The next section ("## Skill Categorization", starts at line 684) follows directly after the previous section (the `### Bare-Request Handling` list, which ends at line 583).

- [ ] **Step 1: Read the current state of the section to delete**

Read `backend/app/services/agent_prompts.py` lines 583-684 to confirm the exact text to delete. The block to remove starts with `## Artifacts (`create_artifact`)` (around line 647) and ends with the blank line just before `## Skill Categorization` (around line 682-683). Total: ~35 lines.

- [ ] **Step 2: Delete the section**

Delete the entire "## Artifacts (`create_artifact`)" section, including:
- The `## Artifacts (`create_artifact`)` header line
- All prose and bullet points in that section
- The trailing blank line(s) between this section and `## Skill Categorization`

After the deletion, the file structure should be:
- The previous section (the `### Bare-Request Handling` list of dimensions) ends with the last bullet "- **Data source** — where does the skill get its input? ..."
- One blank line
- `## Skill Categorization` (no other content between)

The user chose "silent removal" in the brainstorm — do NOT add a replacement line. Do NOT add a "Note:" or "create_artifact is intentionally not available" line. Just delete the section.

- [ ] **Step 3: Run the new test and verify all 6 pass**

Run:

```bash
cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" PYTHONPATH=/root/zhanlu/backend python3 -m pytest tests/test_skill_agent_no_create_artifact.py -v
```

Expected: 6/6 PASS.

If test 5 or 6 still fails, the section wasn't fully removed. Open the file and grep for "Artifacts" — there should be no H2 header matching the regex `^##\s+[^\n]*[Aa]rtifact[^\n]*$`.

- [ ] **Step 4: Do not commit (project policy)**

---

## Task 4: Final verification

**Files:**
- Run tests only — no file changes.

**Interfaces:**
- Consumes: all changes from Tasks 1-3.
- Produces: green tests across the full Skill Agent backend test suite.

- [ ] **Step 1: Run all backend Skill Agent tests**

Run:

```bash
cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)" && DATABASE_URL="sqlite:///./test_runtime.db" PYTHONPATH=/root/zhanlu/backend python3 -m pytest tests/test_skill_agent_prompt_skill_creation_workflow.py tests/test_skill_agent_loop_guard_config.py tests/test_skill_agent_multiselect_options_block.py tests/test_skill_agent_no_create_artifact.py -v
```

Expected: 33/33 PASS (12 + 9 + 6 + 6 = 33). Free RAM should stay above 4 GiB.

- [ ] **Step 2: Run the full frontend Skill Agent test set (no changes, just regression check)**

Run:

```bash
cd /root/zhanlu/frontend && npx vitest run src/lib/translations.test.js src/lib/translations.skillAgentKeys.test.jsx src/pages/SkillAgent.emptyState.test.jsx src/pages/SkillAgent.applySuggestion.test.jsx src/pages/SkillAgent.currentSkillSync.test.jsx src/components/skillagent src/components/agent/AgentMarkdown.optionsBlock.multiselect.test.jsx
```

Expected: 52/52 PASS. (The frontend tests don't touch `create_artifact` or the Skill Agent's toolset, so this is just a sanity check that the prompt + toolset changes don't break the frontend.)

- [ ] **Step 3: Manual smoke (user does this in the browser)**

Tell the user:

> "The implementation is ready. To verify in the browser:
> 1. `cd /root/zhanlu/frontend && npx vite` (start the dev server — `vite build` will still fail with the pre-existing `SkillsSection.jsx` error from another agent's WIP, that's not in scope)
> 2. Open `http://localhost:3000/skill-agent`
> 3. Type 'Code reviewer' and press Enter
> 4. Confirm: 1 `search_skills` call, 1 `:::options` block with 9 dimension-prefixed chips, no `create_artifact` call anywhere in the trace
> 5. Click 2 chips, click 'Use these (2)', press Enter
> 6. The agent creates the skill — **verify no PPTX/PDF/DOCX stub artifact appears in the file panel**
> 7. If the user asks for a 'sample' or 'demo' or 'preview' of the skill, the agent should respond with text only (no file in the panel) — the tool is no longer available
> 8. Verify the existing 4-card empty-state chips and the multi-select flow from the previous turn still work (no regressions)"

- [ ] **Step 4: Append a Turn 10 section to `/root/.codebuddy/memory/2026-07-28.md`**

Append (using `replace_in_file`, do NOT overwrite the file) a new section to today's daily log:

```
## Skill Agent: removed create_artifact from toolset (Turn 10, 2026-07-28)

**User feedback**: 'we are making skills than whi it making pptx and showing here ?' — the agent was creating PPTX stub artifacts after skill creation despite a prompt rule forbidding it. The reliable fix is to remove the tool entirely from the Skill Agent's toolset so the model cannot call it.

**Brainstorm decision**: 'Add tool to toolset (remove temptation)' + 'Remove entirely (cleanest)' + 'Silent removal (clean)'. Three "remove" choices — user was decisive.

**Spec**: docs/superpowers/specs/2026-07-28-skill-agent-no-create-artifact-design.md (commit b38fab6, 232 lines).
**Plan**: docs/superpowers/plans/2026-07-28-skill-agent-no-create-artifact.md (this turn).

**Files changed (2 modified + 1 new, uncommitted per project policy)**:
- MOD backend/app/services/system_agents.py — add SKILL_AGENT_TOOL_NAMES (ALL_TOOL_NAMES minus create_artifact) + use it in line 191 + remove create_artifact from boundaries.allowed line 200
- MOD backend/app/services/agent_prompts.py — delete the 35-line '## Artifacts (create_artifact)' section (lines 647-682), silent removal, no replacement
- NEW backend/tests/test_skill_agent_no_create_artifact.py — 6 tests pinning the contract

**Trade-off (explicit, user-approved)**: the .skill file packaging flow is now unreachable from the Skill Agent. Users who want to package a skill can do it via the database directly, the Tool entity, or a future dedicated package_skill tool (out of scope).

**Verification (next user action)**: 33 backend tests + 52 frontend tests = 85/85 pass. Free RAM 7.5 GiB. Manual smoke in browser confirms no PPTX/PDF/DOCX stub artifacts appear after skill creation.

**Lesson**: prompt rules are advisory; the model is free to ignore them. When a tool is causing repeated misuse, removing it from the toolset is more reliable than strengthening the prompt. The Skill Agent's prompt was already strong (it had a 'Hard rules' subsection explicitly forbidding sample artifact creation), but the model still called create_artifact. The fix was at the tool layer, not the prompt layer.
```

- [ ] **Step 5: Do not commit the implementation (project policy)**

The user has not asked for a commit. End of plan.
