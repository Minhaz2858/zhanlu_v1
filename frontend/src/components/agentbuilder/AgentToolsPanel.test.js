/**
 * AgentToolsPanel — test the data-flow contract.
 *
 * The vitest config in this project uses environment: 'node' (no jsdom),
 * so we test the *exported helper* resolveTools rather than rendering the
 * JSX. The helper is the only piece of business logic in the component,
 * and the same helper drives the panel's two lists (skill-mapped and
 * baseline). If the helper is correct, the rendered output is correct.
 *
 * The component itself is smoke-tested in the Vite build (Task B2) and
 * via manual E2E.
 */
import { describe, it, expect } from 'vitest';
import {
  resolveTools,
  DEFAULT_USER_AGENT_TOOLS,
  SKILL_DISPLAY_TO_TOOL,
} from './agentTools';

describe('AgentToolsPanel — resolveTools (exported helper)', () => {
  it('exposes the expected baseline tool list', () => {
    // Must match backend/app/services/tool_registry.py:DEFAULT_USER_AGENT_TOOLS
    expect(DEFAULT_USER_AGENT_TOOLS).toEqual(['web_search', 'web_extract', 'memory', 'todo']);
  });

  it('returns empty mapped list and full baseline when no skills selected', () => {
    const { mapped, baseline } = resolveTools({ name: 'Empty', skills: [] });
    expect(mapped).toEqual([]);
    expect(baseline).toEqual(['web_search', 'web_extract', 'memory', 'todo']);
  });

  it('drives the "no skills" message data', () => {
    // The "No skills selected" message is conditional on
    // mapped.length === 0 (see AgentToolsPanel.jsx).
    const { mapped } = resolveTools({ name: 'Empty', skills: [] });
    expect(mapped.length).toBe(0);
  });

  it('maps "Web Search" -> web_search and excludes it from baseline (dedupe)', () => {
    const { mapped, baseline } = resolveTools({
      name: 'Searcher',
      skills: ['Web Search'],
    });
    expect(mapped).toContain('web_search');
    // web_search is in the baseline, so it should NOT be duplicated.
    expect(baseline).not.toContain('web_search');
    expect(baseline).toEqual(['web_extract', 'memory', 'todo']);
  });

  it('lists every resolved tool the agent will have', () => {
    const { mapped, baseline } = resolveTools({
      name: 'Rich',
      skills: ['Web Search', 'Memory', 'Code Execution'],
    });
    // Mapped: web_search, memory, execute_code (in input order, deduped)
    expect(mapped).toEqual(['web_search', 'memory', 'execute_code']);
    // Baseline: anything from DEFAULT_USER_AGENT_TOOLS not already in mapped
    expect(baseline).toEqual(['web_extract', 'todo']);
  });

  it('silently drops unknown skill display names (e.g. marketplace skills)', () => {
    const { mapped } = resolveTools({
      name: 'Mixed',
      skills: ['Some Unknown Skill', 'Web Search', 'Another Unknown'],
    });
    expect(mapped).toEqual(['web_search']);
  });

  it('preserves the order of user-selected skills', () => {
    const { mapped } = resolveTools({
      name: 'Orderly',
      skills: ['Memory', 'Web Search', 'Todo', 'Read File'],
    });
    // Input order is preserved (insertion order, no sort).
    expect(mapped).toEqual(['memory', 'web_search', 'todo', 'read_file']);
  });

  it('dedupes when a baseline tool is also in the skills', () => {
    // Memory and Todo are in DEFAULT_USER_AGENT_TOOLS, so if the user
    // also selects them via skills, the resolved list should not have
    // duplicates.
    const { mapped, baseline } = resolveTools({
      name: 'Overlap',
      skills: ['Memory', 'Todo'],
    });
    // mapped gets them in input order
    expect(mapped).toEqual(['memory', 'todo']);
    // baseline excludes them because they're already in mapped
    expect(baseline).toEqual(['web_search', 'web_extract']);
  });

  it('handles missing or null agent gracefully', () => {
    // No agent at all -> empty mapped, full baseline
    const a = resolveTools(null);
    expect(a.mapped).toEqual([]);
    expect(a.baseline).toEqual(DEFAULT_USER_AGENT_TOOLS);

    // Agent with no skills field -> empty mapped, full baseline
    const b = resolveTools({ name: 'NoSkills' });
    expect(b.mapped).toEqual([]);
    expect(b.baseline).toEqual(DEFAULT_USER_AGENT_TOOLS);
  });

  it('treats non-array agent.skills as empty (defensive)', () => {
    // Legacy data may have agent.skills as a JSON string or other non-array
    // truthy value. resolveTools is positioned as a stable contract and
    // must not iterate characters or throw.
    const a = resolveTools({ name: 'Legacy', skills: 'Web Search' });
    expect(a.mapped).toEqual([]);  // string was rejected, not iterated
    expect(a.baseline).toEqual(DEFAULT_USER_AGENT_TOOLS);

    const b = resolveTools({ name: 'Nullish', skills: null });
    expect(b.mapped).toEqual([]);

    const c = resolveTools({ name: 'Object', skills: { 'Web Search': true } });
    expect(c.mapped).toEqual([]);
  });

  it('exposes a SKILL_DISPLAY_TO_TOOL map that matches the backend', () => {
    // The frontend's display-name -> tool-name map must be in sync with
    // backend/app/services/tool_registry.py:SKILL_DISPLAY_TO_TOOL_NAME.
    // A loose subset check is NOT enough — the canonical 'Code Executor'
    // key (per backend seed.py) must be present, else the user's
    // selected skill would silently disappear from the panel.
    // 'Code Execution' is kept as a legacy alias for older agents.
    expect(SKILL_DISPLAY_TO_TOOL).toEqual({
      'Web Search': 'web_search',
      'Web Extract': 'web_extract',
      'Memory': 'memory',
      'Todo': 'todo',
      'Read File': 'read_file',
      'Write File': 'write_file',
      'Image Generation': 'image_generation',
      'Code Execution': 'execute_code',  // legacy alias
      'Code Executor': 'execute_code',  // canonical
      'Delegate Task': 'delegate_task',
      'Database Query': 'ask_data_agent',
      'Sandbox Skill': 'run_sandbox_skill',
    });
  });

  it('accepts the canonical "Code Executor" key from backend', () => {
    // Regression for the silent-drop bug: the agent-builder pipeline saves
    // Tool.name = "Code Executor" (per backend/seed.py). The panel must
    // map it to execute_code, not drop it.
    const { mapped } = resolveTools({
      name: 'Coder',
      skills: ['Code Executor'],
    });
    expect(mapped).toContain('execute_code');
  });
});
