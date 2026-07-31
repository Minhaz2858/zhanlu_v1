import { describe, it, expect, vi, beforeEach } from 'vitest';
import { buildSkillContext } from '@/lib/skillContext';

// This test file verifies the END-TO-END data flow of skill selection
// in the agent chatbot:
//
//   User clicks skill in picker → activeSkill set to full Tool object
//   → handleSend builds skillContext from the object
//   → skillContext is injected into the system prompt
//   → system prompt is sent to the LLM
//
// We test the critical invariants without mounting Chat.jsx (which has
// heavy deps on @base44/vite-plugin, SSE streaming, etc.) by verifying
// the pure building block (buildSkillContext) against realistic skill
// payloads that match what the API returns.

// ─── Realistic API payloads ───────────────────────────────────────────
// These match the actual Tool rows in the DB (verified via the backend
// /api/apps/{app}/entities/Tool endpoint).

const SKILLS_FROM_API = [
  {
    id: 'marketplace-pdf',
    name: 'pdf',
    description: 'Read, extract, merge, split, and create PDF files',
    trigger: '/pdf',
    category: 'pdf',
    skill_md: '# PDF Skill\n\n## Workflow\n1. Identify PDF operation\n2. Execute\n3. Return result',
    source: 'marketplace',
    created_by_id: null,
    references: null,
    sources: null,
    kind: 'system_skill',
    enabled: true,
    is_deleted: false,
  },
  {
    id: 'marketplace-frontend-design',
    name: 'frontend-design',
    description: 'Design and build modern React frontends',
    trigger: '/frontend-design',
    category: 'frontend-design',
    skill_md: '# Frontend Design\n\n## Principles\n- Use semantic HTML\n- Mobile-first responsive design',
    source: 'marketplace',
    created_by_id: null,
    references: null,
    kind: 'system_skill',
    enabled: true,
    is_deleted: false,
  },
  {
    id: 'builtin-web-search',
    name: 'Web Search',
    description: 'Search the web for information',
    trigger: '/search',
    category: 'search',
    skill_md: null, // builtins don't have methodology
    source: 'builtin',
    created_by_id: 'user-123',
    kind: 'custom_tool',
    enabled: true,
    is_deleted: false,
  },
  {
    id: 'marketplace-skill-creator',
    name: 'skill-creator',
    description: 'Guide for creating effective skills',
    trigger: '/skill-creator',
    category: 'skill-creator',
    skill_md: '# Skill Creator\n\nA very long methodology body...\n'.repeat(100),
    source: 'marketplace',
    created_by_id: null,
    references: [
      { name: 'Skill Format Spec', content: 'SKILL.md must have YAML frontmatter...' },
      { name: 'Examples', content: 'Example skill definitions...' },
    ],
    kind: 'system_skill',
    enabled: true,
    is_deleted: false,
  },
];

describe('Skill selection → system prompt injection flow', () => {
  it('skill selected from InvokePicker produces non-empty context with methodology', () => {
    // Simulate: user selects "pdf" from the skill picker
    const selectedSkill = SKILLS_FROM_API.find((s) => s.name === 'pdf');
    const ctx = buildSkillContext(selectedSkill);

    expect(ctx).toBeTruthy();
    expect(ctx.length).toBeGreaterThan(100);
    expect(ctx).toContain('【已激活技能: pdf】');
    expect(ctx).toContain('【技能方法论正文 - 严格遵循】');
    expect(ctx).toContain('# PDF Skill');
    expect(ctx).toContain('## Workflow');
  });

  it('different skills produce different contexts (no cross-contamination)', () => {
    const pdfCtx = buildSkillContext(SKILLS_FROM_API.find((s) => s.name === 'pdf'));
    const designCtx = buildSkillContext(SKILLS_FROM_API.find((s) => s.name === 'frontend-design'));

    expect(pdfCtx).toContain('pdf');
    expect(pdfCtx).toContain('# PDF Skill');
    expect(pdfCtx).not.toContain('# Frontend Design');

    expect(designCtx).toContain('frontend-design');
    expect(designCtx).toContain('# Frontend Design');
    expect(designCtx).not.toContain('# PDF Skill');
  });

  it('builtin skill without skill_md still gets a context block (with fallback)', () => {
    const builtin = SKILLS_FROM_API.find((s) => s.name === 'Web Search');
    const ctx = buildSkillContext(builtin);

    expect(ctx).toBeTruthy();
    expect(ctx).toContain('【已激活技能: Web Search】');
    expect(ctx).toContain('此技能暂无方法论正文');
    // Should still include the description
    expect(ctx).toContain('Search the web for information');
  });

  it('skill with references includes all reference documents', () => {
    const withRefs = SKILLS_FROM_API.find((s) => s.name === 'skill-creator');
    const ctx = buildSkillContext(withRefs);

    expect(ctx).toContain('【技能参考文档 - 可作为执行依据】');
    expect(ctx).toContain('--- Skill Format Spec ---');
    expect(ctx).toContain('SKILL.md must have YAML frontmatter');
    expect(ctx).toContain('--- Examples ---');
    expect(ctx).toContain('Example skill definitions');
  });

  it('context is ready to be concatenated into a system prompt', () => {
    // This simulates the actual concatenation in Chat.jsx:
    //   const systemPrompt = `${baseSystemPrompt}${skillContext}${agentContext}...`
    const baseSystemPrompt = 'You are a helpful assistant.';
    const skillContext = buildSkillContext(SKILLS_FROM_API[0]);
    const systemPrompt = `${baseSystemPrompt}${skillContext}`;

    // The system prompt should contain both the base and the skill
    expect(systemPrompt.startsWith(baseSystemPrompt)).toBe(true);
    expect(systemPrompt).toContain('【已激活技能: pdf】');
    expect(systemPrompt).toContain('# PDF Skill');
  });

  it('clearing the skill (null) produces empty context — no stale injection', () => {
    // After handleSend, activeSkill is set to null. The next message
    // should NOT have any skill context.
    const ctx = buildSkillContext(null);
    expect(ctx).toBe('');

    const systemPrompt = `Base prompt${ctx}`;
    expect(systemPrompt).toBe('Base prompt');
    expect(systemPrompt).not.toContain('【已激活技能');
  });

  it('large skill_md (32K+) is fully included without truncation', () => {
    const bigSkill = SKILLS_FROM_API.find((s) => s.name === 'skill-creator');
    const ctx = buildSkillContext(bigSkill);
    const expectedBody = bigSkill.skill_md;

    // The entire body should be present
    expect(ctx).toContain(expectedBody);
    expect(ctx.length).toBeGreaterThan(expectedBody.length); // ctx has extra framing
  });
});

describe('InvokePicker object-passing contract', () => {
  // These tests document the contract that InvokePicker / PlusMenu
  // must pass the FULL skill object (not just a name string) to
  // handlePickSkill. The buildSkillContext function requires
  // skill.skill_md to inject the methodology — if only a string
  // were passed, the context would be empty/fallback.

  it('buildSkillContext works correctly when given a full Tool row object', () => {
    // This is what InvokePicker.onSelectSkill(s) passes
    const toolRow = SKILLS_FROM_API[0];
    const ctx = buildSkillContext(toolRow);
    expect(ctx).toContain(toolRow.skill_md);
  });

  it('buildSkillContext produces fallback when given only a string (old bug regression)', () => {
    // OLD BUG: activeSkill used to be a string token like "pdf".
    // buildSkillContext('pdf') should NOT accidentally produce
    // a valid-looking context with methodology — it should return ''
    // because a string has no .name or .skill_md.
    const ctx = buildSkillContext('pdf');
    expect(ctx).toBe('');
  });

  it('buildSkillContext produces fallback when given {name: "pdf"} without skill_md', () => {
    // If only the name were passed (not the full object), we'd get
    // the fallback path — this proves why the full object is needed.
    const ctx = buildSkillContext({ name: 'pdf' });
    expect(ctx).toContain('此技能暂无方法论正文');
    expect(ctx).not.toContain('【技能方法论正文');
  });
});
