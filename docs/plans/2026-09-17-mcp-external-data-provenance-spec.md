# Spec: provenance + verification boundary for externally-sourced (MCP) data

Status: DRAFT for review. Design only — no code written.
Date: 2026-09-17
Scope: `zhanlu_n8n_main_version` (backend + My Space → MCP)
Trigger: decision request on whether SQLBot can be used as an NL2SQL/ChatBI sidecar.

---

## 1. Why this spec exists

An external MCP server (SQLBot or any other) can be connected in My Space → MCP in
minutes. Its tools are then published to **every** agent, and its results are
returned to the agent loop as if they were ordinary Zhanlu tool results. Nothing
in that path records that the numbers came from outside our governed query
pipeline, and nothing downstream treats them any differently.

The point of this spec is **not** to block MCP or SQLBot. It is to make
externally-sourced data *identifiable* everywhere it is used, and to state exactly
which guarantees stop applying when a number arrives that way.

### Non-goals

- Not a new verification engine. Reuse what exists (§5).
- Not a change to the Synexia verifier's non-fatal semantics (§4.3).
- Not a replacement of the governed NL2SQL pipeline.
- Not a SQLBot fork or source import (license unverified — see §10).

---

## 2. Verified facts (recon, with file:line)

These are measured in this checkout, not assumed.

| # | Fact | Evidence |
|---|------|----------|
| 1 | Every MCP tool result is normalized in exactly one place | `app/services/mcp_client/__init__.py:71` `call_tool()` — "normalize the result for the Zhanlu tool shape" |
| 2 | All three call paths converge there | agent loop `mcp_proxy.py:361`; sync/ephemeral `mcp_proxy.py:388`; manual API `routers/mcp.py:523` |
| 3 | The agent-loop forwarder is a pure passthrough — no checks, no marking | `mcp_proxy.py:139-159` `_forward()` → `call_mcp_tool_for_server()` |
| 4 | MCP tools are also callable from workflows (third surface) | `app/services/workflow_runtime.py:715` |
| 5 | MCP tools are appended to every agent's schemas automatically | `agent_prompts.py:2919-2923` |
| 6 | Default grant = every agent (empty list means all) | `mcp_proxy.py:95-106` |
| 7 | Synexia VERIFY runs in ONE place and is explicitly non-fatal | `synexia/verifier.py:16-20` ("information, not a gate"); called only from `synexia/fsm.py:517` |
| 8 | Grounding extraction runs in the FSM path only | `extract_grounding` called only from `synexia/fsm.py:921` |
| 9 | Grounding dispatches on `observation_type` ∈ {nl2sql, synthesize, sandbox}, else falls back to a generic tool extractor | `synexia/grounding_extractor.py:80-99` |
| 10 | The chat tool loop builds NO observations | `observations` has no producing site in `routers/agents.py` (only an unrelated comment at `:2496`) |
| 11 | There is already a verified/unverified claim mechanism with a structural predicate and a rewrite | `enterprise_orchestrator/claim_tracker.py:85-97` (needs `source_facet` + `source_sql` + `source_row_ids`), `:100-143` (`verify_claims` re-executes `source_sql`), `:146-156` (`rewrite_unverified` → "never carry unverified numbers into the executive document") |
| 12 | The governed query authority exists as a package | `app/services/nl2sql/`: `policy.py`, `row_filter.py`, `schema_validator.py`, `validator.py`, `semantic_resolver.py` |
| 13 | A trust vocabulary already exists in the platform | `app/models/skill_profile.py:65` `trust_level` ∈ untrusted \| community \| verified \| official |
| 14 | `mcp_servers` has no trust/governance column today | `app/models/mcp_server.py` (name, transport, url/command/args, status, granted_agents, is_deleted only) |

Correction to the incoming analysis: the identifiers it cites
(`apply_policy_to_sql`, `CanonicalResultSet`, `EvidenceStore`) have **zero
occurrences** in this checkout. The real equivalents are rows 7-12 above. Any
comparison table built on those names describes a different codebase.

---

## 3. The gap, stated precisely

Three separate sub-gaps, in descending order of user-facing impact:

**G1 — No provenance on the result.** Once `call_tool()` returns, a number from an
external server is indistinguishable from a number produced by governed NL2SQL.
The stamp must be added at the single seam (fact 1-2), so it travels to all three
surfaces at once.

**G2 — No verification path for chat answers.** The observation → verify →
grounding → confidence pipeline exists only in the FSM path (facts 7-10). The
chat loop — which is where MCP tools actually run today — never builds an
observation, so an MCP result is invisible to every deterministic check. This is
not SQLBot-specific: it applies to every MCP tool, today.

**G3 — The existing "unverified" safety net is not reached.** `claim_tracker`
already rewrites unverified claim text before rendering (fact 11), and an
externally-sourced number *cannot* satisfy its predicate (no `source_sql` — the
SQL never left the external server). So the protection exists; what is missing is
that MCP-derived numbers are guaranteed to route through it.

---

## 4. Design

### 4.1 One seam: stamp at the connection

Add a provenance envelope at `mcp_client/__init__.py:71` on every return branch
(success and every error/timeout branch), so no caller can obtain an unstamped
result:

```json
{
  "success": true,
  "result": <unchanged payload>,
  "provenance": {
    "source": "mcp",
    "server": "<server name>",
    "server_id": "<uuid>",
    "tool": "<tool name>",
    "governed": false,
    "externally_executed": true,
    "trust_level": "<from the server row, §4.4>",
    "verified": false,
    "verification_basis": "external — no Zhanlu execution, no source_sql"
  }
}
```

Rationale for placing it here rather than in `_forward`: `_forward` only covers
the agent loop, leaving the manual API and workflow surfaces unstamped. This is
the smallest seam that is also complete.

Additive only: `result`/`success`/`error` keys keep their current shape, so no
existing consumer breaks.

### 4.2 Make it visible where answers are assembled

Two small, symmetric extensions — no new engine:

1. `grounding_extractor.py`: add `mcp` to the dispatch (fact 9) so an MCP
   observation renders as `=== #<seq> mcp:<server>/<tool> (ok, EXTERNAL) ===`
   instead of relying on the generic fallback.
2. The chat loop: when a tool result carries `provenance.source == "mcp"`, record
   it on the message as externally-sourced (a flag, not a rewrite), so downstream
   rendering and synthesizers can see it.

### 4.3 Do NOT make verification fatal

`verifier.py:16-20` deliberately treats a failed check as *information* that
lowers confidence rather than a gate that discards work. Keep that. This spec
adds **identification**, not punishment: an external number should still be
usable, but never silently presented as governed-and-verified.

### 4.4 Enforcement levels (the actual product decision)

| Level | Behaviour | Cost | Recommendation |
|-------|-----------|------|----------------|
| **A — mark only** | Stamp + grounding label. Agent may cite freely, with an external-source note. | Smallest | Minimum viable; do this first |
| **B — unverified-by-construction** | Any claim derived from an external source is treated as unverified and goes through `rewrite_unverified` before it can appear in a rendered deliverable | Medium (route claims through the existing tracker) | **Recommended default** |
| **C — classified at connect time** | Admin sets `trust_level` + `data_access` per server when connecting. `data_access = none` → its results are explanatory only and cannot back a numeric claim; `verified`/`official` → may feed deliverables | Medium + schema change | Do together with B |

Level C is where SQLBot would sit as `community` / `unverified` — usable for
exploration and prose, not authoritative for the numbers in an executive
deliverable. That is the "sidecar, not core" outcome, expressed in code rather
than in a policy document.

---

## 5. Reuse map — what we do NOT build

| Need | Reuse |
|------|-------|
| governed SQL, policy rewriting, row filters | `app/services/nl2sql/` (policy, row_filter, schema_validator, validator, semantic_resolver) |
| verified vs unverified claim lifecycle | `enterprise_orchestrator/claim_tracker.py` (`verify_claims`, `rewrite_unverified`) |
| observation contract for checks | `ObservationRecord` (observation_type, tool_name, success, result_data, error_message, seq, artifact_id) |
| grounding findings block | `synexia/grounding_extractor.py` |
| trust vocabulary | `skill_profile.trust_level` values |
| MCP server registry + grants | `mcp_proxy` (`_MCP_SERVERS`, `grants()`, `refresh_grants`) |

New code is limited to: the provenance stamp (one seam), one dispatch entry, one
schema column pair, and the claim routing.

---

## 6. Failure modes to handle

1. **Stale session results** — a stamped result must not outlive a disconnect;
   `_forward` already handles the missing-server case (`mcp_proxy.py:148-156`).
2. **Stamp loss through summarization/truncation** — the per-result persistence
   path (`_persisted_result_str`) substitutes large results; the stamp must
   survive, or be re-applied at read time.
3. **Second-order laundering** — an agent or synthesizer re-state an external
   number inside its own prose, after which the stamp is gone. Level B is the
   mitigation: the claim, not the tool result, is the unit that carries
   provenance.
4. **Workflow surface** — fact 4 means a workflow step can pull external numbers
   into a deliverable without any chat involvement. The stamp at the seam covers
   detection; enforcement there is a follow-up.
5. **Trust flapping** — raising a server's trust level re-classifies historic
   results. Decide: forward-only (recommended) vs retroactive.

---

## 7. Rollout

- Feature-flagged (`MCP_PROVENANCE_*`), default **on for A**, **off for B/C**
  until reviewed, consistent with `NL2SQL_SHADOW_MODE_ENABLED = False`
  (`config.py:1150`).
- Schema: add `trust_level` and `data_access` to `mcp_servers` (migration), or
  carry them in the existing JSON `metadata` if a migration is undesirable now.
- Existing connected servers: default to the safe class (`community`,
  `data_access=read`), never to `verified`.

## 8. Tests

- Unit: every `call_tool` return branch carries `provenance` (success, error,
  timeout).
- Unit: `_forward` and the manual API path both return stamped results.
- Unit: a claim built from an external result fails the `claim_tracker`
  predicate; `rewrite_unverified` replaces its text.
- Unit: grounding renders the `mcp` dispatch entry with an EXTERNAL marker.
- E2E (extend `scripts/e2e/mcp_agent_tools_e2e.sh`): assert the turn's tool
  result carries `provenance.source == "mcp"`, and that the final answer is
  labelled when the server is not `verified`.
- Regression: existing tool-loop + MCP suites stay green.

## 9. Open decisions (need your call)

- **D1** Default level for a newly connected server: A, **B (recommended)**, or C?
- **D2** Schema: proper columns for `trust_level`/`data_access`, or reuse the
  `metadata` JSON now and migrate later?
- **D3** Stamp the workflow + manual-API surfaces too (recommended, free at the
  seam), or agent loop only for now?
- **D4** SQLBot specifically: a generic MCP server under this rule, or a
  first-class governed adapter that routes through `nl2sql.policy` (much bigger;
  the incoming analysis's "authorized source selection" option)?

## 10. Unverified / not checked

- SQLBot's documented behaviour, MCP interface, permission model, and license
  terms (the license fetch from this machine returned empty — treat the
  modification/branding clause as **unverified** until legal reviews it).
- Whether the chat loop's per-result persistence preserves arbitrary extra keys
  end-to-end (needed for §6.2) — must be measured before implementing B.
- No claim is made here about SQLBot's accuracy versus Zhanlu's NL2SQL; that is
  what the A/B pilot is for, and it should be run against our own questions,
  datasource snapshot, and authorization scope.
