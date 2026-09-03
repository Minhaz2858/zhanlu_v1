/**
 * RunDrawer.workflow.test.jsx — source-contract tests for the RunDrawer
 * "↻ Rerun from here" affordance (Task 11 of the iterate-loop plan):
 * a completed step row can re-run the workflow from that node, streaming
 * the new run's live step events over the drawer's SSE channel, with a
 * header badge tagging the active run as a rerun of a previous run.
 *
 * Why source-text tests?
 * ----------------------
 * Same convention as NodeDetailView.workflow.test.jsx / PlusMenu.workflow
 * .test.jsx / WorkflowCanvas.test.jsx: they pin the *contract* (affordance
 * present, rerunWorkflow() wired with run_id + start_node_id, stream switch
 * through shared runId state) without booting React, the router or API
 * mocks.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, '../RunDrawer.jsx'), 'utf8');

describe('RunDrawer.jsx — "Rerun from here" contract', () => {
  it('adds a localized "Rerun from here" affordance on step rows', () => {
    expect(SOURCE).toMatch(/Rerun from here/);
    expect(SOURCE).toMatch(/从此节点重跑/);
  });

  it('guards the rerun affordance to completed steps only', () => {
    // the rerun action must only render when the row status is completed
    expect(SOURCE).toMatch(/status === 'completed'/);
    expect(SOURCE).toMatch(/s\.status === 'completed'[\s\S]{0,400}?Rerun from here/);
  });

  it('calls rerunWorkflow() with the active run id and start node, then switchRun', () => {
    expect(SOURCE).toMatch(/rerunWorkflow\(/);
    expect(SOURCE).toMatch(/run_id: /);
    expect(SOURCE).toMatch(/start_node_id: /);
    expect(SOURCE).toMatch(/switchRun\(/);
  });

  it('lifts the active run id into state so the SSE stream follows run switches', () => {
    expect(SOURCE).toMatch(/const \[runId, setRunId\]/);
    expect(SOURCE).toMatch(/switchRun[\s\S]{0,300}?setSteps\(\{\}\)/);
  });

  it('badge shows the rerun ancestry (rerun_of / rerun_from_node) for the active run', () => {
    expect(SOURCE).toMatch(/rerun_of/);
    expect(SOURCE).toMatch(/rerun_from_node/);
    expect(SOURCE).toMatch(/rerun_of\./); // renders the original run id
  });
});
