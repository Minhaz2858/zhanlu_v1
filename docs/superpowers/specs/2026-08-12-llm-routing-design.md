# LLM Model Routing — Design Specification

**Date**: 2026-08-12  
**Status**: Implementing  
**Scope**: `backend/app/services/llm_router.py`, `backend/app/routers/llm.py`, `frontend/src/pages/Chat.jsx`, `frontend/src/pages/ProjectDetail.jsx`

---

## 1. Overview

The LLM routing system determines which language model endpoint is used for each chat message. It resolves a model through a precedence hierarchy (project → agent → default → legacy) and provides both a backend resolver and frontend visibility.

This spec covers the complete routing design, including the 5 precedence rules, the system-agent short-circuit, and the UI integration points.

---

## 2. Precedence Rules

The resolver `resolve_effective_llm()` evaluates in strict order. The first match wins.

| Priority | Rule | Condition | Result |
|----------|------|-----------|--------|
| 0 | **System-agent short-circuit** | Agent is a system meta-agent (`agent_builder`, `skill_agent`, `automation_agent`) | Catalog default LLM (source: `system_default`) |
| 1 | **Project binding** | Project has `llm_model_id` set | Project's LLM (source: `project`) |
| 2 | **Agent binding** | Agent has `llm_model_id` set AND no project binding | Agent's LLM (source: `agent`) |
| 3 | **Catalog default** | A `LlmModel` row with `is_default=True` exists | Default LLM (source: `default`) |
| 4 | **Legacy fallback** | None of the above | `OPENAI_BASE_URL` / `OPENAI_API_KEY` from env (source: `legacy`) |

### Key Design Decisions

- **Rule #0 is new** — it short-circuits BEFORE any DB lookups for project/agent bindings. This is intentional: system meta-agents are infrastructure agents that must always use the catalog default, regardless of project/agent-level bindings.
- **Rule #3 (project wins over agent)** — When both a project and an agent have LLM bindings, the project binding takes precedence. This reflects the design principle that project-level settings are the primary configuration surface.
- **Rule #4 (catalog default)** — The `is_default=True` flag on `LlmModel` is the system-wide fallback. Only one default should exist per organization.
- **Rule #5 (legacy)** — The `OPENAI_BASE_URL` / `OPENAI_API_KEY` environment variables are the absolute last resort. This path exists for backward compatibility.

---

## 3. System-Agent Short-Circuit

### 3.1 Definition

```python
_SYSTEM_META_AGENTS = frozenset({
    "agent_builder",    # Agent creation wizard
    "skill_agent",      # Skill execution agent
    "automation_agent", # Automation run agent
})
```

### 3.2 Why These Agents?

- `agent_builder` — Creates and configures other agents. It should not be influenced by the project it's operating in.
- `skill_agent` — Executes skill workflows. It's an infrastructure tool, not a domain-specific agent.
- `automation_agent` — Runs scheduled/manual automations. It must use the stable default LLM to avoid breaking automation pipelines when a project's LLM is changed.

### 3.3 Why NOT `general_assistant` or `power_user`?

- `general_assistant` — The default user-facing agent. It MUST respect project/agent bindings (rules #1-3) because users explicitly choose which project they're in.
- `power_user` — An advanced user agent. Same reasoning as `general_assistant`.

### 3.4 Implementation

The short-circuit is placed at the TOP of `resolve_effective_llm()`, before any DB lookups:

```python
async def resolve_effective_llm(db, project_id, agent_name, org_id=None):
    # Rule 0: System-agent short-circuit
    if agent_name and agent_name in _SYSTEM_META_AGENTS:
        default = await _get_catalog_default(db, org_id)
        if default:
            return EffectiveLLM(
                model_name=default.model_name,
                endpoint=default.endpoint,
                api_key=_decrypt(default.api_key),
                source="system_default",
            )
        return _legacy_fallback()

    # Rule 1: Project binding
    ...
```

---

## 4. API Design

### 4.1 Effective LLM Response

**Endpoint**: `GET /api/llm/effective`

**Query Parameters**:
- `project_id` (optional): UUID of the project
- `agent_name` (optional): Name of the agent

**Response Schema** (`EffectiveLLMOut`):

```python
class EffectiveLLMOut(BaseModel):
    model_name: str
    endpoint: str
    source: str  # "project" | "agent" | "default" | "system_default" | "legacy"
    is_locked: bool = False  # True when admin has locked the model
```

**Source Values**:

| Value | Meaning |
|-------|---------|
| `project` | Resolved from project's `llm_model_id` |
| `agent` | Resolved from agent's `llm_model_id` (no project binding) |
| `default` | Resolved from catalog default `LlmModel` |
| `system_default` | Forced catalog default because agent is a system meta-agent |
| `legacy` | Fallback to `OPENAI_BASE_URL` / `OPENAI_API_KEY` env vars |

---

## 5. Frontend Integration

### 5.1 Chat Composer — EffectiveModelBadge

**Component**: `frontend/src/components/chat/EffectiveModelBadge.jsx`

**Location**: In the chat input area, to the right of the project/agent chips.

**Behavior**:
- Fetches `/api/llm/effective?project_id=X&agent_name=Y` when the project or agent changes.
- Shows a `Cpu` icon + model name (e.g., "deepseek-chat").
- When `source === "system_default"`, shows a subtle "System" label.
- When `is_locked === true`, shows a `Lock` icon with amber styling.

**Wire-up in Chat.jsx**:
```jsx
import EffectiveModelBadge from "../components/chat/EffectiveModelBadge";

// In the chat input area:
<EffectiveModelBadge projectId={currentProjectId} agentName={currentAgentName} />
```

### 5.2 ProjectDetail — System-Agent LLM Chip

**Location**: Per-agent LLM chip section (L1187-1207 in `ProjectDetail.jsx`).

**Behavior**:
- For agents with `is_system === true`, show a "Default (system)" badge with `Lock` icon.
- Hide the LLM selector dropdown for system agents.
- The badge uses muted styling (`text-muted-foreground`) to indicate it's not configurable.

```jsx
{a.is_system ? (
  <span className="inline-flex items-center gap-1 text-[11px] text-muted-foreground border rounded px-1.5 py-0.5">
    <Lock className="h-3 w-3" /> Default (system)
  </span>
) : (
  // existing LLM chip logic
)}
```

---

## 6. Test Coverage

### 6.1 Test File

`backend/tests/test_llm_router.py`

### 6.2 Test Cases

| # | Test Case | Rule | Setup | Expected Source |
|---|-----------|------|-------|-----------------|
| 1 | No project, no agent → default | #3 | Default LlmModel exists | `default` |
| 2 | Project with LLM → project model | #1 | Project with llm_model_id | `project` |
| 3 | Project + agent both bound → project wins | #1 | Both set | `project` |
| 4 | Agent only bound → agent model | #2 | Agent with llm_model_id, no project | `agent` |
| 5 | System agent → default | #0 | agent_builder + project binding | `system_default` |
| 6 | System agent → default even with agent binding | #0 | skill_agent + agent binding | `system_default` |
| 7 | No default, no bindings → legacy | #4 | Nothing configured | `legacy` |
| 8 | Admin-locked model | - | is_locked=True on LlmModel | `is_locked=True` |
| 9 | general_assistant respects project binding | #1 | general_assistant + project binding | `project` |
| 10 | power_user respects agent binding | #2 | power_user + agent binding | `agent` |

---

## 7. Data Model

No new migrations required. The existing schema already supports all features:

- `llm_models` table (migration 045) — `is_default`, `enabled`, `is_private`, `api_key`
- `projects.llm_model_id` FK (migration 045)
- `agent_apps.llm_model_id` FK (migration 045)
- `agent_apps.is_system` column (existing) — used by system_agents.py to mark meta-agents

---

## 8. Error Handling

| Scenario | Behavior |
|----------|----------|
| No default LLM configured | Return legacy fallback (env vars) |
| Project's LlmModel is disabled | Skip to next rule (agent/default) |
| Agent's LlmModel is disabled | Skip to next rule (default) |
| API key decryption fails | Log error, return legacy fallback |
| `/api/llm/effective` returns 500 | Show "Model unavailable" in badge |

---

## 9. Security Considerations

- API keys are encrypted at rest using `Fernet` (existing pattern in `llm_model.py`).
- The `/api/llm/effective` endpoint does NOT return the API key to the frontend — only the model name, endpoint, source, and lock status.
- The `resolve_effective_llm()` function returns the decrypted API key only to the backend LLM call chain, never to the user.
