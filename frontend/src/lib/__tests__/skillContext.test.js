import { describe, it, expect } from 'vitest';
import { buildSkillContext, hasMethodology } from '@/lib/skillContext';

// ─── Test fixtures ────────────────────────────────────────────────────
// These mirror the shape of a Tool row returned by the backend API
// (``base44.entities.Tool.list``).  The ``skill_md`` field is the parsed
// body of the skill's SKILL.md file — the actual methodology the LLM
// must follow.

const FULL_SKILL = {
  id: 'skill-pdf-001',
  name: 'pdf',
  description: 'Read, extract, merge, split, and create PDF files',
  trigger: '/pdf',
  category: 'pdf',
  skill_md: `# PDF Skill\n\n## Steps\n1. Open the PDF\n2. Extract text\n3. Return result`,
  references: [
    { name: 'PDF Reference', content: 'Detailed PDF API docs...' },
  ],
  source: 'marketplace',
  created_by_id: 'user-abc',
};

const SKILL_NO_MD = {
  id: 'skill-builtin-001',
  name: 'Web Search',
  description: 'Search the web for information',
  trigger: '/search',
  category: 'search',
  skill_md: null,
  references: null,
};

const SKILL_EMPTY_MD = {
  id: 'skill-empty-002',
  name: 'Empty Skill',
  skill_md: '   \n  ',
};

// ─── Tests ────────────────────────────────────────────────────────────

describe('buildSkillContext', () => {
  it('returns empty string when skill is null', () => {
    expect(buildSkillContext(null)).toBe('');
  });

  it('returns empty string when skill has no name and no skill_md', () => {
    expect(buildSkillContext({})).toBe('');
    expect(buildSkillContext({ description: 'just a desc' })).toBe('');
  });

  it('includes the skill name in the activated header', () => {
    const ctx = buildSkillContext(FULL_SKILL);
    expect(ctx).toContain('【已激活技能: pdf】');
  });

  it('includes the mandatory instruction to use the skill methodology', () => {
    const ctx = buildSkillContext(FULL_SKILL);
    expect(ctx).toContain('你必须主动运用此技能的方法论');
    expect(ctx).toContain('而不是给出通用答案');
  });

  it('includes the skill description when present', () => {
    const ctx = buildSkillContext(FULL_SKILL);
    expect(ctx).toContain('技能简介: Read, extract, merge, split, and create PDF files');
  });

  it('includes the trigger when present', () => {
    const ctx = buildSkillContext(FULL_SKILL);
    expect(ctx).toContain('触发词: /pdf');
  });

  it('includes the category when present', () => {
    const ctx = buildSkillContext(FULL_SKILL);
    expect(ctx).toContain('分类: pdf');
  });

  it('injects the full skill_md body into the methodology section', () => {
    const ctx = buildSkillContext(FULL_SKILL);
    expect(ctx).toContain('【技能方法论正文 - 严格遵循】');
    expect(ctx).toContain('# PDF Skill');
    expect(ctx).toContain('1. Open the PDF');
    expect(ctx).toContain('2. Extract text');
    expect(ctx).toContain('3. Return result');
  });

  it('shows a fallback message when skill_md is null', () => {
    const ctx = buildSkillContext(SKILL_NO_MD);
    expect(ctx).toContain('此技能暂无方法论正文');
    // Should NOT contain the methodology section header
    expect(ctx).not.toContain('【技能方法论正文 - 严格遵循】');
  });

  it('shows a fallback message when skill_md is only whitespace', () => {
    const ctx = buildSkillContext(SKILL_EMPTY_MD);
    expect(ctx).toContain('此技能暂无方法论正文');
    expect(ctx).not.toContain('【技能方法论正文 - 严格遵循】');
  });

  it('includes reference documents when present', () => {
    const ctx = buildSkillContext(FULL_SKILL);
    expect(ctx).toContain('【技能参考文档 - 可作为执行依据】');
    expect(ctx).toContain('--- PDF Reference ---');
    expect(ctx).toContain('Detailed PDF API docs...');
  });

  it('does not include reference section when references is null', () => {
    const ctx = buildSkillContext(SKILL_NO_MD);
    expect(ctx).not.toContain('【技能参考文档');
  });

  it('does not include reference section when references is empty array', () => {
    const ctx = buildSkillContext({ ...FULL_SKILL, references: [] });
    expect(ctx).not.toContain('【技能参考文档');
  });

  it('includes the output requirements at the end', () => {
    const ctx = buildSkillContext(FULL_SKILL);
    expect(ctx).toContain('输出要求:');
    expect(ctx).toContain('Markdown 格式化');
  });

  it('starts with a newline (for clean concatenation into system prompt)', () => {
    const ctx = buildSkillContext(FULL_SKILL);
    expect(ctx.startsWith('\n')).toBe(true);
  });

  it('works with a minimal skill (only name)', () => {
    const ctx = buildSkillContext({ name: 'minimal-skill' });
    expect(ctx).toContain('【已激活技能: minimal-skill】');
    expect(ctx).toContain('此技能暂无方法论正文');
  });

  it('works with a skill that only has skill_md (no name)', () => {
    const ctx = buildSkillContext({ skill_md: 'Some methodology content' });
    expect(ctx).toContain('【技能方法论正文 - 严格遵循】');
    expect(ctx).toContain('Some methodology content');
  });

  it('handles special characters in skill_md without breaking', () => {
    const tricky = {
      name: 'tricky',
      skill_md: 'Content with `backticks` and ${template} and "quotes"',
    };
    const ctx = buildSkillContext(tricky);
    expect(ctx).toContain('`backticks`');
    expect(ctx).toContain('${template}');
    expect(ctx).toContain('"quotes"');
  });
});

describe('hasMethodology', () => {
  it('returns true when skill_md has content', () => {
    expect(hasMethodology(FULL_SKILL)).toBe(true);
  });

  it('returns false when skill_md is null', () => {
    expect(hasMethodology(SKILL_NO_MD)).toBe(false);
  });

  it('returns false when skill_md is only whitespace', () => {
    expect(hasMethodology(SKILL_EMPTY_MD)).toBe(false);
  });

  it('returns false when skill is null', () => {
    expect(hasMethodology(null)).toBe(false);
  });
});
