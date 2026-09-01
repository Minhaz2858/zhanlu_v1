/**
 * PlusMenu.jsx Agent Workflow section contract tests:
 *  - "+" menu gains an "Agent Workflow" entry (separate from Agents)
 *  - the workflow view lists ONLY chat-trigger workflows (listWorkflows +
 *    trigger_chat filter) and calls onSelectWorkflow on pick
 *  - the regular Agent view EXCLUDES flow_mode rows (no double entry)
 *
 * Source-text contract tests — same pattern as PlusMenu.uploadGate.test.jsx.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './PlusMenu.jsx'), 'utf8');

describe('PlusMenu.jsx Agent Workflow section', () => {
  it('declares onSelectWorkflow and fetches workflows via listWorkflows', () => {
    expect(SOURCE).toMatch(/onSelectWorkflow/);
    expect(SOURCE).toMatch(/import \{ listWorkflows \} from '@\/api\/workflows'/);
    expect(SOURCE).toMatch(/listWorkflows\(\)\.then\(\(rows\) => setWorkflows\(rows \|\| \[\]\)\)/);
  });

  it('adds an "Agent Workflow" entry in the main menu (Workflow icon)', () => {
    expect(SOURCE).toMatch(/setView\('workflow'\)/);
    expect(SOURCE).toMatch(/<Workflow className="h-3\.5 w-3\.5 text-muted-foreground" \/> Agent Workflow/);
  });

  it('filters the workflow list to chat-trigger workflows only', () => {
    expect(SOURCE).toMatch(/hasChatTrigger\s*=\s*\(w\)\s*=>\s*Array\.isArray\(w\?\.flow\?\.nodes\)\s*&&\s*w\.flow\.nodes\.some\(\(n\)\s*=>\s*n\?\.type\s*===\s*'trigger_chat'\)/);
    expect(SOURCE).toMatch(/filteredWorkflows\s*=\s*workflows\.filter\(\(w\)\s*=>\s*hasChatTrigger\(w\)/);
  });

  it('calls onSelectWorkflow when a workflow row is picked', () => {
    expect(SOURCE).toMatch(/onSelectWorkflow\?\.\(w\)/);
    // Active workflow is shown with a Check, like agents.
    expect(SOURCE).toMatch(/activeAgent\?\.id === w\.id/);
  });

  it('excludes flow_mode rows from the regular Agent view', () => {
    expect(SOURCE).toMatch(/filteredAgents\s*=\s*agents\.filter\(\(a\)\s*=>\s*!a\.flow_mode/);
  });
});
