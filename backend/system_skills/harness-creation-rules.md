# harness-creation-rules

> Hidden system skill for the Agent Builder. Always active — never user-selectable.
> Purpose: the canonical Harness profile the Agent Builder must enforce on every
> agent it creates. Maps the abstract HarnessAgent schema to the concrete
> AgentApp columns.

## MANDATORY AGENT CREATION RULE

When creating any new agent, the Agent Builder MUST produce a HarnessAgent —
never a simple prompt agent, never a standalone chatbot, never a direct LLM
wrapper.

The canonical HarnessAgent shape:

```
HarnessAgent {
    identity,        // who the agent is, its mission, its users
    constitution,    // the 5-layer prompt: identity / boundary / reasoning / tools / output
    model,           // provider, model name, temperature, max_tokens
    skills,          // verified skill names from list_tools / list_market_agents
    tools,           // tool_config.enabled_tools + tier whitelisting
    knowledge,       // knowledge_bases IDs + data_bindings (datasource/table/column)
    memory,          // memory_scope: user_only | app_shared | org_shared
    permissions,     // policy_profile + data_read/data_write/human_fallback
    evaluation,      // evaluation_profile (golden tests, trace replay, grounding)
}
```

## Field → AgentApp column mapping

The AgentApp table IS the persistence layer for the HarnessAgent. When the
Agent Builder calls `create_agent`, it populates these columns:

| HarnessAgent field        | AgentApp column(s)                                                       | Required? |
|---------------------------|--------------------------------------------------------------------------|-----------|
| identity                  | `name`, `description`, `capabilities`, `project`                        | yes       |
| constitution              | `prompt_identity` (L1), `prompt_boundary` (L2), `prompt_reasoning` (L3), `prompt_tools` (L4), `prompt_output` (L5) | yes (all 5) |
| model                     | `model`, `temperature`, `top_p`, `max_tokens`, `agent_type`             | yes       |
| skills                    | `skills` (verified names), `skill_bindings` (allowed/blocked + versions) | optional  |
| tools                     | `tool_config.enabled_tools`, `tool_config.disabled_tools`, `max_call_count`, `max_retries`, `max_iterations` | yes       |
| knowledge                 | `knowledge_bases` (KB IDs), `data_bindings` (datasource/table/column)    | optional  |
| memory                    | `memory_scope` (user_only \| app_shared \| org_shared)                   | yes       |
| permissions               | `policy_profile` (risk_tier, confirmation), `data_read`, `data_write`, `human_fallback` | yes       |
| evaluation                | `evaluation_profile` (golden tests, trace replay, grounding checks)      | optional  |
| manifest                  | `manifest_json` (mission, task_scope, boundaries, output_contract)       | optional  |
| output contract           | `output_contract` (allowed artifact types, source-citation requirements) | optional  |
| observability             | `trace_enabled`, `log_level`                                             | yes       |

## Required vs. optional

- **Required every time**: identity, constitution (all 5 layers), model,
  tool_config (at least `enabled_tools` + `max_call_count`), memory_scope,
  permissions (data_read/data_write/human_fallback), trace_enabled.
- **Required for consequential agents** (touches money, PII, irreversible
  operations): also populate `manifest_json`, `policy_profile` with risk_tier,
  `evaluation_profile` with at least one golden test, `output_contract` with
  citation requirements.
- **Optional**: `skill_bindings` (only when version pinning matters),
  `data_bindings` (only when the agent must read specific tables/columns).

## NEVER create

1. A "simple prompt agent" — only `prompt_identity` filled, the other four
   layers blank. This is a chatbot, not a Harness Agent.
2. A "standalone chatbot" — no `tool_config`, no `memory_scope`, no
   `human_fallback`. This bypasses the runtime's safety.
3. A "direct LLM wrapper" — no skills, no knowledge, no permissions, just a
   model name. This is not a Zhanlu Agent.

## ALWAYS verify before claiming success

Before emitting "Agent Created Successfully":

1. The `create_agent` tool result returned a non-null `agent_id`.
2. Every required field above is populated on the returned record.
3. `skills` (if non-empty) contains only names that `list_tools` or
   `list_market_agents` returned.
4. `prompt_tools` references tools by their **function-calling name**
   (e.g. `ask_data_agent`, `web_search`), never by display name.
5. When `knowledge_bases` is non-empty, `prompt_tools` mentions
   `ask_data_agent` as the mandatory database access tool.

If any check fails, call `update_agent` to fix the field before declaring
completion. Never claim success the tool result did not confirm.

## The Agent is not the LLM

> "A Zhanlu Agent is not only an LLM. It is Model + Harness + Configuration +
> Skills + Memory. The Harness is the foundation of the Zhanlu Agent Ecosystem."
> — Layer 3 Enterprise Harness Architecture

The Agent Builder is itself a Harness Agent running on the same runtime. The
discipline above applies to every agent it creates, including sub-agents of
multi-agent topologies.
