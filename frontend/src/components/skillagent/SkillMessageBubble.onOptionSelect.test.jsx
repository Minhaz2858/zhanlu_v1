/**
 * Regression: SkillMessageBubble must forward `onOptionSelect` to
 * AgentMarkdown so `:::options` chips are clickable (2026-07-28).
 *
 * Root cause: the previous turn wired the `:::options` directive into
 * the agent_prompts.py bare-request handling, and the AgentMarkdown
 * component renders the chips as clickable buttons — but
 * SkillMessageBubble was NOT passing `onOptionSelect` to AgentMarkdown,
 * so clicks were no-ops (`onSelect?.(opt)` with onSelect undefined).
 *
 * This test pins the wiring: SkillMessageBubble must accept
 * `onOptionSelect` and forward it to AgentMarkdown, so a click on a
 * chip in the Skill Agent chat surfaces the option text to the
 * parent (SkillAgent.jsx wires it to `applySuggestion`).
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(
  resolve(__dirname, './SkillMessageBubble.jsx'),
  'utf8',
);

describe('SkillMessageBubble.onOptionSelect wiring (2026-07-28 regression)', () => {
  it('accepts an onOptionSelect prop in its function signature', () => {
    // The default export must destructure onOptionSelect so the parent
    // can pass a handler down. The destructure may span multiple lines.
    const sig = SOURCE.match(
      /export default function SkillMessageBubble\([\s\S]*?\}\s*\)/,
    );
    expect(sig, 'SkillMessageBubble default export not found').toBeTruthy();
    expect(sig[0]).toMatch(/\bonOptionSelect\b/);
  });

  it('forwards onOptionSelect to AgentMarkdown', () => {
    // The component must pass onOptionSelect to the <AgentMarkdown>
    // element, not just to other children. We grep for the JSX usage
    // and check the prop is present.
    const agentMarkdownCall = SOURCE.match(/<AgentMarkdown[\s\S]*?>/);
    expect(
      agentMarkdownCall,
      '<AgentMarkdown ...> element not found in SkillMessageBubble.jsx',
    ).toBeTruthy();
    expect(agentMarkdownCall[0]).toMatch(/\bonOptionSelect=\{onOptionSelect\}/);
    expect(agentMarkdownCall[0]).toMatch(/\bmultiSelect=\{true\}/);
  });

  it('does NOT call onOptionSelect directly in the bubble (only forwards it)', () => {
    // Sanity: the prop should be FORWARDED, not invoked inside the
    // bubble (which would break the parent contract — the parent
    // expects to receive the chip text, not have the bubble handle
    // it itself).
    const calls = SOURCE.match(/onOptionSelect\??\(/g) || [];
    expect(calls.length).toBe(0);
  });
});

describe('SkillMessageBubble skill result card', () => {
  it('recognizes skill create/update tool names', () => {
    expect(SOURCE).toMatch(/function\s+isSkillMutationTool/);
    expect(SOURCE).toMatch(/create_skill/);
    expect(SOURCE).toMatch(/update_skill/);
    expect(SOURCE).toMatch(/Tool\.create/);
    expect(SOURCE).toMatch(/Tool\.update/);
  });

  it('renders a structured card from parsed skill mutation results', () => {
    expect(SOURCE).toMatch(/function\s+SkillResultCard/);
    expect(SOURCE).toMatch(/result\.id/);
    expect(SOURCE).toMatch(/Skill created/);
    expect(SOURCE).toMatch(/Skill updated/);
    expect(SOURCE).toMatch(/SKILL\.md/);
    expect(SOURCE).toMatch(/<SkillResultCard\s+result=\{parsedResults\}\s+toolName=\{name\}/);
  });
});

describe('SkillAgent.jsx wires onOptionSelect to SkillMessageBubble', () => {
  const SKILL_AGENT_PATH = resolve(
    __dirname,
    '../../pages/SkillAgent.jsx',
  );
  let source;
  try {
    source = readFileSync(SKILL_AGENT_PATH, 'utf8');
  } catch {
    source = null;
  }
  const it_ = source ? it : it.skip;

  it_('passes onOptionSelect when rendering SkillMessageBubble', () => {
    const bubble = source.match(/<SkillMessageBubble[\s\S]*?\/>/);
    expect(
      bubble,
      '<SkillMessageBubble ... /> element not found in SkillAgent.jsx',
    ).toBeTruthy();
    expect(bubble[0]).toMatch(/\bonOptionSelect=\{[^}]+\}/);
  });
});
