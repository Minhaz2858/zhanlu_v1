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

  it('gates rendering on the open prop so an unconditional mount never covers the canvas', () => {
    expect(SOURCE).toMatch(/if\s*\(!open\)\s*return\s*null/);
  });

  it('evaluates flow_saved for every streamed event (hoisted before the delta/done branching)', () => {
    const flowSavedIdx = SOURCE.indexOf("evt.type === 'flow_saved'");
    const deltaIdx = SOURCE.indexOf("evt.type === 'delta'");
    expect(flowSavedIdx, "evt.type === 'flow_saved' check not found").toBeGreaterThan(-1);
    expect(deltaIdx, "evt.type === 'delta' branch not found").toBeGreaterThan(-1);
    // The flow_saved check must be hoisted above the delta branch — a
    // standalone { type: 'flow_saved' } SSE event must not fall through.
    expect(flowSavedIdx).toBeLessThan(deltaIdx);
  });

  it('closes on Escape via a document-level keydown listener (ReportSidePanel convention)', () => {
    expect(SOURCE).toMatch(/Escape/);
    expect(SOURCE).toMatch(/addEventListener\('keydown'/);
    expect(SOURCE).toMatch(/removeEventListener\('keydown'/);
  });

  it('aborts in-flight streams on unmount/close (cleanup aborts the AbortController)', () => {
    expect(SOURCE).toMatch(/cancelled\s*=\s*true/);
    expect(SOURCE).toMatch(/abortRef\.current\.abort\(\)/);
  });

  it('resumes the latest existing session for the workflow (listAgentConversations by workflow_id, limit 1)', () => {
    expect(SOURCE).toMatch(/listAgentConversations/);
    const resumeBlock = SOURCE.match(/listAgentConversations\('workflow_builder',\s*\{[\s\S]{0,120}?\}\)/);
    expect(resumeBlock, 'listAgentConversations call not found').not.toBeNull();
    expect(resumeBlock[0]).toMatch(/workflowId/);
    expect(resumeBlock[0]).toMatch(/limit:\s*1/);
    // Reuses the found conversation id and hydrates its stored messages.
    expect(SOURCE).toMatch(/existing\[0\]/);
    expect(SOURCE).toMatch(/conv\.messages/);
    expect(SOURCE).toMatch(/setResumed\(true\)/);
  });

  it('falls through to createAgentConversation when no session exists yet', () => {
    // The resume path returns early only when a row exists; otherwise the
    // original create call with workflow_builder + workflow_id must run.
    const createIdx = SOURCE.indexOf("createAgentConversation(\n          'workflow_builder',");
    expect(createIdx, 'create fallback not found').toBeGreaterThan(-1);
    expect(SOURCE.indexOf('existing.length > 0')).toBeLessThan(createIdx);
  });

  it('offers a New session button that starts a fresh conversation (RotateCcw + startNewSession)', () => {
    expect(SOURCE).toMatch(/RotateCcw/);
    expect(SOURCE).toMatch(/startNewSession/);
    expect(SOURCE).toMatch(/setResumed\(false\)/);
    expect(SOURCE).toMatch(/setMessages\(\[\]\)/);
  });
});
