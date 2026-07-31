# Agent Tool Wiring Hardening & Builder UX Audit

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Eliminate the real tool-dispatch bugs surfaced by the E2E test, harden the agent-builder UX so users can see and control the tools their created agents actually receive, and re-run the E2E red-green to prove both.

**Architecture:** Two parallel streams.
- **Stream A (backend hardening):** Add reverse-name aliasing to `execute_tool` so dotted-name hallucinations (`skills.hub`, `skill.provenance`, etc.) route back to the registered underscore tool. Audit the runtime tool resolution for user-built agents and document the fallback behavior. Make `fuzzy_match`'s description even more explicit about file editing.
- **Stream B (agent-builder UX):** Add a post-creation "Tools" panel to the agent-builder flow that lists the actual tools the new agent will receive, distinguishing skills-mapped tools from the `DEFAULT_USER_AGENT_TOOLS` fallback. Surface a "Add more tools" CTA that calls the existing `update_agent` flow with a tool list.

**Tech Stack:** Python 3.11 / FastAPI / SQLAlchemy, React 18 / Vite / Tailwind, shadcn/ui Dialog & Card, lucide-react icons.

---

## Stream A — Backend Tool-Dispatch Hardening

### Task A1: Add reverse aliasing for dotted tool names

**Files:**
- Modify: `backend/app/services/agent_tools.py:31-180` (the `execute_tool` function)
- Test: `backend/tests/test_tool_alias_resolution.py` (new)

**Problem:** `TOOL_DISPLAY_NAMES` in `app/routers/agents.py:52-133` maps underscore tool names (`skills_hub`) to dotted display labels (`skills.hub`). The mapping is one-way. When the LLM hallucinates a dotted name (we observed this for `skills.hub` in the E2E), `registry.execute(dotted_name)` returns "tool not found" instead of routing to `skills_hub`.

**Step 1: Write the failing test**

Create `backend/tests/test_tool_alias_resolution.py`:

```python
"""Reverse-aliased tool names must dispatch to the underscore-canonical tool.

The LLM sometimes hallucinates the dotted display label (e.g. ``skills.hub``)
instead of the registered underscore name (``skills_hub``). The dispatcher
should treat the dotted form as an alias and route to the canonical tool.
"""
import asyncio
import pytest
from unittest.mock import MagicMock, patch


@pytest.mark.parametrize("dotted,canonical", [
    ("skills.hub", "skills_hub"),
    ("skills.sync", "skills_sync"),
    ("skills.guard", "skills_guard"),
    ("skill.provenance", "skill_provenance"),
    ("skill.usage", "skill_usage"),
    ("mcp.oauth", "mcp_oauth"),
    ("mcp.oauth_manager", "mcp_oauth_manager"),
    ("process_registry.list", "process_registry_list"),
    ("process_registry.tail", "process_registry_tail"),
    ("process_registry.kill", "process_registry_kill"),
])
def test_dotted_tool_name_routes_to_canonical(dotted, canonical):
    from app.services.tool_registry import registry
    # Pretend both names are asked for; canonical must exist after the fix
    # to allow the alias map to reference it.
    canonical_entry = registry.get_entry(canonical)
    if canonical_entry is None:
        pytest.skip(f"Tool {canonical} not registered in this env")
    # After the fix, the alias map must contain dotted→canonical for each pair
    from app.services.agent_tools import TOOL_NAME_ALIASES
    assert TOOL_NAME_ALIASES.get(dotted) == canonical, (
        f"dotted name {dotted!r} should map to canonical {canonical!r}"
    )
```

**Step 2: Run the test, verify it fails**

```bash
cd /root/zhanlu/backend && python -m pytest tests/test_tool_alias_resolution.py -v
```

Expected: `ImportError` or `AttributeError` for `TOOL_NAME_ALIASES` (or skipped if registry empty — that's the failure mode we want).

**Step 3: Implement the alias map and reverse-lookup**

In `backend/app/services/agent_tools.py`, near the top (after imports), add:

```python
# Reverse alias map: dotted display names → canonical registry names.
# Sourced from TOOL_DISPLAY_NAMES in app/routers/agents.py. When the LLM
# hallucinates the dotted form, dispatch should still find the tool.
TOOL_NAME_ALIASES: dict[str, str] = {
    "skills.hub": "skills_hub",
    "skills.sync": "skills_sync",
    "skills.guard": "skills_guard",
    "skill.provenance": "skill_provenance",
    "skill.usage": "skill_usage",
    "mcp.oauth": "mcp_oauth",
    "mcp.oauth_manager": "mcp_oauth_manager",
    "process_registry.list": "process_registry_list",
    "process_registry.tail": "process_registry_tail",
    "process_registry.kill": "process_registry_kill",
}
```

In the same file, in `execute_tool` (~line 60, where the name is first used), add reverse-resolution:

```python
async def execute_tool(tool_name, arguments, db, user_id=None, context=None):
    # Resolve dotted-name hallucinations to canonical underscore names.
    tool_name = TOOL_NAME_ALIASES.get(tool_name, tool_name)
    # ... rest of the function unchanged
```

**Step 4: Run the test, verify it passes**

```bash
cd /root/zhanlu/backend && python -m pytest tests/test_tool_alias_resolution.py -v
```

Expected: all parametrized cases PASS (or skip if tool not registered).

**Step 5: Run the full backend test suite to confirm no regression**

```bash
cd /root/zhanlu/backend && python -m pytest tests/ -x -q 2>&1 | tail -40
```

Expected: no new failures.

**Step 6: Commit**

```bash
cd /root && git add zhanlu/backend/app/services/agent_tools.py zhanlu/backend/tests/test_tool_alias_resolution.py && git commit -m "fix(tool-dispatch): route dotted-name hallucinations to canonical tools"
```

---

### Task A2: Document and tighten user-built agent tool fallback

**Files:**
- Modify: `backend/app/services/tool_registry.py:435-438` (add comment block)
- Test: `backend/tests/test_user_agent_tool_fallback.py` (new)

**Problem:** `DEFAULT_USER_AGENT_TOOLS = ["web_search", "memory", "todo"]` is silent. User-built agents that don't select any skills get exactly these 3 tools. We want to (a) make this explicit in code, (b) add a test that pins the behavior, and (c) emit a debug log when the fallback fires so the agent-builder can surface it to the user.

**Step 1: Write the failing test**

Create `backend/tests/test_user_agent_tool_fallback.py`:

```python
"""User-built agents with no skill selections must get a stable, well-documented
set of baseline tools. The agent-builder UI surfaces this fallback to the user.
"""
from app.services.tool_registry import (
    DEFAULT_USER_AGENT_TOOLS,
    resolve_tools_from_skills,
)


def test_default_user_agent_tools_contains_expected_three():
    # The baseline is a stable contract: 3 tools, in this order, for any
    # user-built agent that has not selected any skills. Pin the contract.
    assert DEFAULT_USER_AGENT_TOOLS == ["web_search", "memory", "todo"]


def test_resolve_tools_from_skills_with_empty_input_returns_empty():
    # Empty input must return empty list (NOT the defaults). The defaults
    # are applied by the caller, not by this function.
    assert resolve_tools_from_skills([]) == []


def test_resolve_tools_from_skills_skips_unknown_names():
    # Unknown skill names (e.g. marketplace skills without handlers) are
    # silently dropped, not raised.
    assert resolve_tools_from_skills(["Nonexistent Tool", "Web Search"]) == ["web_search"]
```

**Step 2: Run the test, verify it passes already (these pin existing behavior)**

```bash
cd /root/zhanlu/backend && python -m pytest tests/test_user_agent_tool_fallback.py -v
```

Expected: PASS. (If any fail, fix the implementation to match the documented contract.)

**Step 3: Strengthen the docstring on `DEFAULT_USER_AGENT_TOOLS`**

In `backend/app/services/tool_registry.py:435-438`, replace the current comment with:

```python
# Baseline tools every user-built agent gets when its skills list is empty
# or has no mappable entries. This is a stable contract pinned by
# tests/test_user_agent_tool_fallback.py — changing it is a UX-facing
# change and must update the agent-builder "Tools" panel.
#
# Current baseline: web_search (look things up), memory (persist context),
# todo (plan multi-step work). All three are safe-by-default and do not
# require user consent.
#
# If you add a tool here, also update the agent-builder "Tools" panel
# (frontend/src/components/agentbuilder/AgentToolsPanel.jsx) to list it.
DEFAULT_USER_AGENT_TOOLS: list[str] = ["web_search", "memory", "todo"]
```

**Step 4: Add a debug log in `resolve_tools_for_agent` when fallback fires**

In `backend/app/services/tool_registry.py:461-492`, modify `resolve_tools_for_agent` so the fallback path logs once per call. Add a `_log_fallback` helper:

```python
import logging
logger = logging.getLogger(__name__)

# ... at the end of resolve_tools_for_agent, just before `return []`:

    if not (tool_config and isinstance(tool_config, dict)
            and tool_config.get("enabled_tools")):
        if agent_name and agent_name not in DEFAULT_TOOLS_BY_AGENT:
            logger.debug(
                "user-built agent %r has no enabled_tools and no default "
                "set; falling back to DEFAULT_USER_AGENT_TOOLS=%r",
                agent_name, DEFAULT_USER_AGENT_TOOLS,
            )
```

(If a logger is already present, just add the debug call without re-importing.)

**Step 5: Run the test, verify still passes**

```bash
cd /root/zhanlu/backend && python -m pytest tests/test_user_agent_tool_fallback.py -v
```

Expected: PASS.

**Step 6: Commit**

```bash
cd /root && git add zhanlu/backend/app/services/tool_registry.py zhanlu/backend/tests/test_user_agent_tool_fallback.py && git commit -m "docs(tool-registry): pin user-agent fallback contract with tests + log"
```

---

## Stream B — Agent-Builder UX Audit & "Tools" Panel

### Task B1: Add AgentToolsPanel component

**Files:**
- Create: `frontend/src/components/agentbuilder/AgentToolsPanel.jsx` (new)
- Test: `frontend/src/components/agentbuilder/AgentToolsPanel.test.jsx` (new)

**Problem:** When the agent-builder finishes creating an agent, the user has no visibility into which tools that agent will actually receive. The runtime fallback (`DEFAULT_USER_AGENT_TOOLS`) is silent. We need a "Tools" panel that lists resolved tools, distinguishes skill-mapped from fallback, and offers a way to add more.

**Step 1: Write the failing component test**

Create `frontend/src/components/agentbuilder/AgentToolsPanel.test.jsx`:

```jsx
import { render, screen } from '@testing-library/react';
import AgentToolsPanel from './AgentToolsPanel';

const baseAgent = {
  name: 'Test Agent',
  skills: ['Web Search', 'Memory'],
};

describe('AgentToolsPanel', () => {
  test('renders agent name', () => {
    render(<AgentToolsPanel agent={baseAgent} />);
    expect(screen.getByText(/Test Agent/i)).toBeInTheDocument();
  });

  test('lists skill-mapped tools', () => {
    render(<AgentToolsPanel agent={baseAgent} />);
    // Web Search maps to web_search; Memory maps to memory.
    expect(screen.getByText(/web_search/i)).toBeInTheDocument();
    expect(screen.getByText(/memory/i)).toBeInTheDocument();
  });

  test('always lists DEFAULT_USER_AGENT_TOOLS fallback section', () => {
    render(<AgentToolsPanel agent={baseAgent} />);
    expect(screen.getByText(/baseline tools/i)).toBeInTheDocument();
    expect(screen.getByText(/todo/i)).toBeInTheDocument();
  });

  test('shows "Add more tools" CTA', () => {
    render(<AgentToolsPanel agent={baseAgent} />);
    expect(
      screen.getByRole('button', { name: /add more tools/i })
    ).toBeInTheDocument();
  });
});
```

**Step 2: Run the test, verify it fails (component doesn't exist yet)**

```bash
cd /root/zhanlu/frontend && npx vitest run src/components/agentbuilder/AgentToolsPanel.test.jsx
```

Expected: `Failed to resolve import "./AgentToolsPanel"` or similar.

**Step 3: Implement the component**

Create `frontend/src/components/agentbuilder/AgentToolsPanel.jsx`:

```jsx
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Wrench, Plus, ShieldCheck } from 'lucide-react';

// Mirrors backend/app/services/tool_registry.py:DEFAULT_USER_AGENT_TOOLS
const DEFAULT_USER_AGENT_TOOLS = ['web_search', 'memory', 'todo'];

// Mirrors SKILL_DISPLAY_TO_TOOL_NAME in backend/app/services/tool_registry.py
const SKILL_DISPLAY_TO_TOOL = {
  'Web Search': 'web_search',
  'Database Query': 'ask_data_agent',
  'Memory': 'memory',
  'Todo': 'todo',
  'Code Execution': 'execute_code',
  'File Read': 'read_file',
  'File Write': 'write_file',
  'Browser': 'browser',
};

function resolveTools(agent) {
  const skillNames = agent?.skills || [];
  const mapped = skillNames
    .map((s) => SKILL_DISPLAY_TO_TOOL[s])
    .filter(Boolean);
  // De-dupe preserving order, then append baseline fallback.
  const ordered = [...new Set(mapped)];
  const baseline = DEFAULT_USER_AGENT_TOOLS.filter((t) => !ordered.includes(t));
  return { mapped: ordered, baseline };
}

export default function AgentToolsPanel({ agent, onAddTools }) {
  const { mapped, baseline } = resolveTools(agent);

  return (
    <Card data-testid="agent-tools-panel" className="w-full">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Wrench className="h-4 w-4" /> Tools for {agent?.name || 'this agent'}
        </CardTitle>
        <CardDescription>
          These are the tools the agent will be able to call. Add more to expand
          what it can do.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div>
          <h4 className="text-sm font-medium mb-2">From your selections</h4>
          {mapped.length === 0 ? (
            <p className="text-sm text-muted-foreground">No skills selected.</p>
          ) : (
            <ul className="space-y-1">
              {mapped.map((tool) => (
                <li key={tool} className="text-sm font-mono px-2 py-1 bg-muted rounded">
                  {tool}
                </li>
              ))}
            </ul>
          )}
        </div>

        <div>
          <h4 className="text-sm font-medium mb-2 flex items-center gap-2">
            <ShieldCheck className="h-4 w-4" /> Baseline tools (always on)
          </h4>
          <ul className="space-y-1">
            {baseline.map((tool) => (
              <li key={tool} className="text-sm font-mono px-2 py-1 bg-muted/50 rounded">
                {tool}
              </li>
            ))}
          </ul>
          <p className="text-xs text-muted-foreground mt-2">
            web_search, memory, and todo are safe-by-default. They let the
            agent look things up, persist context, and plan multi-step work
            without requiring extra consent.
          </p>
        </div>

        <Button
          onClick={onAddTools}
          variant="outline"
          className="w-full"
        >
          <Plus className="h-4 w-4 mr-2" /> Add more tools
        </Button>
      </CardContent>
    </Card>
  );
}
```

**Step 4: Run the test, verify it passes**

```bash
cd /root/zhanlu/frontend && npx vitest run src/components/agentbuilder/AgentToolsPanel.test.jsx
```

Expected: 4 tests PASS.

**Step 5: Commit**

```bash
cd /root && git add zhanlu/frontend/src/components/agentbuilder/AgentToolsPanel.jsx zhanlu/frontend/src/components/agentbuilder/AgentToolsPanel.test.jsx && git commit -m "feat(agent-builder): add AgentToolsPanel showing resolved tools + baseline"
```

---

### Task B2: Wire AgentToolsPanel into AgentBuilder flow

**Files:**
- Modify: `frontend/src/pages/AgentBuilder.jsx` (insert panel after agent creation)
- Test: manual E2E verification (see Task C1)

**Problem:** The `AgentBuilder` page creates an agent by listening for an `agentapp` tool-call result, but it never shows the user what tools the new agent has. Add the panel.

**Step 1: Read the existing `createdAgent` state insertion point**

In `frontend/src/pages/AgentBuilder.jsx` (~line 49), there's a `const [createdAgent, setCreatedAgent] = useState(null);` state. Find where it is set after a successful creation (search for `setCreatedAgent`).

**Step 2: Import and render the panel**

Add to imports (top of file):

```jsx
import AgentToolsPanel from '@/components/agentbuilder/AgentToolsPanel';
```

After the existing `createdAgent` block, render the panel:

```jsx
{createdAgent && (
  <div className="mt-4">
    <AgentToolsPanel
      agent={createdAgent}
      onAddTools={() => navigate(`/agents/${createdAgent.id}/edit`)}
    />
  </div>
)}
```

**Step 3: Verify it builds**

```bash
cd /root/zhanlu/frontend && npx vite build 2>&1 | tail -20
```

Expected: no errors.

**Step 4: Commit**

```bash
cd /root && git add zhanlu/frontend/src/pages/AgentBuilder.jsx && git commit -m "feat(agent-builder): show AgentToolsPanel after agent creation"
```

---

## Stream C — E2E Red-Green Verification

### Task C1: Re-run E2E and capture pass/fail table

**Files:**
- Read: `/tmp/zhanlu_e2e.py` (existing test)
- Modify: `/tmp/zhanlu_e2e.py` (add a pre-flight step that lists resolved tools per agent)

**Step 1: Add pre-flight introspection to the E2E**

In `/tmp/zhanlu_e2e.py`, add a helper near the top:

```python
def list_resolved_tools(agent_name: str, session) -> list[str]:
    """Call GET /agents/{name} and return the resolved tool list."""
    resp = session.get(f"{BASE}/agents/{agent_name}")
    if resp.status_code != 200:
        return []
    data = resp.json()
    return data.get("resolved_tools", [])
```

Add a pre-flight block right after the agents are seeded, before the per-agent assertions:

```python
# Pre-flight: verify every agent has at least the baseline tools resolved.
expected_baseline = {"web_search", "memory", "todo"}
for agent in AGENTS:
    tools = list_resolved_tools(agent["name"], session)
    missing = expected_baseline - set(tools)
    if missing:
        record_issue(f"{agent['name']} missing baseline tools: {missing}")
    else:
        record_pass(f"{agent['name']} baseline tools present")
```

**Step 2: Run the full E2E**

```bash
cd /root && timeout 900 python3 /tmp/zhanlu_e2e.py 2>&1 | tee /tmp/zhanlu_e2e_post_fix.log | tail -60
```

**Step 3: Compare to the pre-fix baseline**

- The pre-fix baseline (from the prior run summary) was: UI 7/7 PASS, API 27/34 PASS.
- After the fix, we expect: UI 7/7 PASS, API ≥30/34 PASS (with the dotted-name aliasing fix unblocking the `skills_hub` call and the agent-builder UX work closing the user-built-agents gap).

**Step 4: Write a results summary to `docs/plans/2026-07-13-e2e-post-fix.md`**

```bash
cat > /root/zhanlu/docs/plans/2026-07-13-e2e-post-fix.md <<'EOF'
# E2E Post-Fix Results

**Date:** 2026-07-13
**Test:** /tmp/zhanlu_e2e.py
**Fixes applied:** A1 (dotted-name aliasing), A2 (fallback contract), B1+B2 (AgentToolsPanel)

## Pass/fail table
[PASTE TABLE FROM /tmp/zhanlu_e2e_post_fix.log]

## New failures
[LIST ANY]

## Resolved
[LIST ANY THAT PREVIOUSLY FAILED]
EOF
```

**Step 5: Commit the test improvement and results doc**

```bash
cd /root && git add /tmp/zhanlu_e2e.py zhanlu/docs/plans/2026-07-13-e2e-post-fix.md && git commit -m "test(e2e): pre-flight baseline tool check + post-fix results doc"
```

---

## Acceptance Criteria

- [ ] A1: `TOOL_NAME_ALIASES` exists and dotted names route to canonical.
- [ ] A2: `DEFAULT_USER_AGENT_TOOLS` contract is pinned by tests.
- [ ] B1: `AgentToolsPanel` renders and lists resolved + baseline tools.
- [ ] B2: `AgentBuilder` page shows the panel after agent creation.
- [ ] C1: E2E re-run produces a higher pass count than the pre-fix baseline.
- [ ] C1: A results doc is saved to `docs/plans/2026-07-13-e2e-post-fix.md`.
- [ ] All tasks committed.
