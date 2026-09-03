/**
 * NodeDetailView.workflow.test.jsx — source-contract tests for the NDV
 * "Test node" drawer (single-node test with an editable payload prefilled
 * from the last completed run's upstream outputs).
 *
 * Why source-text tests?
 * ----------------------
 * Same convention as WorkflowCanvasPage.builder.test.jsx /
 * WorkflowCanvas.test.jsx / PlusMenu.workflow.test.jsx: they pin the
 * *contract* (button present, API call wired, upstreams derived from
 * node._flowEdges) without booting React, the router or API mocks.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, '../NodeDetailView.jsx'), 'utf8');

describe('NodeDetailView.jsx — "Test node" drawer contract', () => {
  it('adds a "Test node" button to the toolbar (localized)', () => {
    expect(SOURCE).toMatch(/Test node/);
    expect(SOURCE).toMatch(/测试节点/);
  });

  it('calls the testNode() API client when running a test', () => {
    expect(SOURCE).toMatch(/testNode\(/);
    expect(SOURCE).toMatch(/from '@\/api\/workflows'/);
  });

  it('builds the upstream payload from node._flowEdges + last completed run', () => {
    expect(SOURCE).toMatch(/trigger_input/);
    expect(SOURCE).toMatch(/upstreams/);
    // helper that derives upstreams must read the node's edges
    expect(SOURCE).toMatch(/buildUpstreamPrefill[\s\S]{0,500}?_flowEdges/);
  });

  it('drawer exposes run / reload-from-last-run / close and the history caption', () => {
    expect(SOURCE).toMatch(/Run test/);
    expect(SOURCE).toMatch(/Reload from last run/);
    expect(SOURCE).toMatch(/Close/);
    // placeholder guidance for an empty trigger_input
    expect(SOURCE).toMatch(/"text": "\.\.\."/);
    // caption: test runs are not persisted
    expect(SOURCE).toMatch(/not saved to run history/);
  });
});
