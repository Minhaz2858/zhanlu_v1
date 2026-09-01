/**
 * WorkflowBuilderChat.jsx — source-contract tests.
 *
 * The workflow canvas chat panel streams with the `workflow_builder` agent
 * (bound via metadata.workflow_id so the backend persistence branch knows
 * which workflow to save) and closes itself on `flow_saved` by calling
 * onFlowSaved() so WorkflowCanvasPage can reload the graph.
 *
 * Why source-text tests?
 * ----------------------
 * Same convention as ChatInput.inheritedBadge.test.jsx / PlusMenu.workflow.test.jsx:
 * they pin the *contract* (agent binding, metadata field, streaming helper,
 * flow_saved callback, slide-over shell) without booting React/router/mocks.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './WorkflowBuilderChat.jsx'), 'utf8');

describe('WorkflowBuilderChat.jsx workflow_builder streaming panel', () => {
  it('streams to workflow_builder with workflow_id metadata', () => {
    expect(SOURCE).toMatch(/workflow_builder/);
    expect(SOURCE).toMatch(/workflow_id/);
    expect(SOURCE).toMatch(/streamAgentResponse/);
    expect(SOURCE).toMatch(/onFlowSaved/);
  });

  it('creates the agent conversation via createAgentConversation with workflow_builder + workflow_id', () => {
    // Mirrors the Chat page contract: createAgentConversation(agentName, metadata, title)
    // with agent_name 'workflow_builder' and metadata { workflow_id }.
    expect(SOURCE).toMatch(/createAgentConversation/);
    const createBlock = SOURCE.match(/createAgentConversation\([\s\S]{0,400}?\)/);
    expect(createBlock, 'createAgentConversation call not found').not.toBeNull();
    expect(createBlock[0]).toMatch(/workflow_builder/);
    expect(createBlock[0]).toMatch(/workflow_id/);
  });

  it('accepts the workflowId / open / onClose / onFlowSaved prop contract', () => {
    const props = SOURCE.match(
      /WorkflowBuilderChat\s*\(\s*\{\s*([\s\S]{0,300}?)\}\s*\)/,
    );
    expect(props, 'WorkflowBuilderChat props destructure not found').not.toBeNull();
    expect(props[1]).toMatch(/workflowId/);
    expect(props[1]).toMatch(/open/);
    expect(props[1]).toMatch(/onClose/);
    expect(props[1]).toMatch(/onFlowSaved/);
  });

  it('renders a right slide-over panel w-[380px] with message list, input row and close button', () => {
    expect(SOURCE).toMatch(/w-\[380px\]/);
    expect(SOURCE).toMatch(/fixed/);
    expect(SOURCE).toMatch(/right-0/);
    // X close button + Sparkles header icon + Send button come from lucide-react.
    expect(SOURCE).toMatch(
      /import\s*\{[^}]*\bX\b[^}]*\}[^}]*from\s*['"`]lucide-react['"`]/,
    );
    expect(SOURCE).toMatch(
      /import\s*\{[^}]*\bSparkles\b[^}]*\}[^}]*from\s*['"`]lucide-react['"`]/,
    );
    expect(SOURCE).toMatch(
      /import\s*\{[^}]*\bSend\b[^}]*\}[^}]*from\s*['"`]lucide-react['"`]/,
    );
  });

  it('aborts the in-flight stream on close (AbortController + signal)', () => {
    expect(SOURCE).toMatch(/AbortController/);
    expect(SOURCE).toMatch(/abort/);
    expect(SOURCE).toMatch(/signal/);
  });

  it('localizes the header via useLanguage (en: Build with AI / zh: AI 构建)', () => {
    expect(SOURCE).toMatch(/useLanguage/);
    expect(SOURCE).toMatch(/lang\s*===\s*'en'/);
    expect(SOURCE).toMatch(/Build with AI/);
    expect(SOURCE).toMatch(/AI\s*构建/);
  });

  it('disables send while streaming', () => {
    expect(SOURCE).toMatch(/streaming/);
    expect(SOURCE).toMatch(/disabled/);
  });
});
