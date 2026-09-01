/**
 * WorkflowCanvasPage.builder.test.jsx — source-contract tests for the
 * AI build button + WorkflowBuilderChat panel wiring in the workflow
 * canvas header.
 *
 * Why source-text tests?
 * ----------------------
 * Same convention as PlusMenu.workflow.test.jsx / WorkflowBuilderChat.test.jsx:
 * they pin the *contract* (button present, panel mounted with the right props,
 * flow_saved → reload hookup) without booting React/router/API mocks.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './WorkflowCanvas.jsx'), 'utf8');

describe('WorkflowCanvasPage.jsx — AI build button + builder panel wiring', () => {
  it('has a chat button and builder panel wired', () => {
    expect(SOURCE).toMatch(/WorkflowBuilderChat/);
    expect(SOURCE).toMatch(/MessageSquare/);
    expect(SOURCE).toMatch(/onFlowSaved/);
    expect(SOURCE).toMatch(/builderOpen/);
    expect(SOURCE).toMatch(/reloadWorkflow/);
  });
});
