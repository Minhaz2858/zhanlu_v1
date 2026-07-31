// Pure function extracted from Chat.jsx's handleSend so the skill-injection
// logic can be unit-tested without mounting the full React component (which
// pulls in base44 SDK, SSE streaming, and a dozen useState hooks).
//
// The function takes a skill OBJECT (a Tool row from the API, as selected
// by InvokePicker / PlusMenu) and returns the string that gets appended to
// the system prompt. The caller is responsible for deciding *whether* to
// call it (i.e. ``activeSkill`` is non-null) — this function only shapes
// the content.

/**
 * Build the system-prompt section that instructs the LLM to follow a
 * selected skill's methodology.
 *
 * @param {object|null} skill - The full Tool row (with ``skill_md``,
 *   ``name``, ``description``, ``trigger``, ``category``, ``references``).
 *   ``null`` or an object without ``name`` / ``skill_md`` produces "".
 * @returns {string} The skill-context block (empty string when no skill).
 */
export function buildSkillContext(skill) {
  if (!skill || (!skill.name && !skill.skill_md)) return '';

  const parts = [
    `【已激活技能: ${skill.name}】`,
    `用户已通过技能选择器装载了此技能。你必须主动运用此技能的方法论、工作流程与输出规范来回应用户的问题，而不是给出通用答案。如果用户的问题与技能领域相关，请严格按照技能正文中的步骤与格式执行；如果问题超出技能范围，先说明超出范围，再尽可能基于技能的相邻能力给出建议。`,
  ];

  if (skill.description) parts.push(`技能简介: ${skill.description}`);
  if (skill.trigger) parts.push(`触发词: ${skill.trigger}`);
  if (skill.category) parts.push(`分类: ${skill.category}`);

  if (skill.skill_md && String(skill.skill_md).trim()) {
    parts.push(`【技能方法论正文 - 严格遵循】\n${skill.skill_md}`);
  } else {
    parts.push('(此技能暂无方法论正文，请基于上述简介与描述自主推断其能力边界并应用。)');
  }

  if (Array.isArray(skill.references) && skill.references.length > 0) {
    parts.push('【技能参考文档 - 可作为执行依据】');
    skill.references.forEach((ref) => {
      if (ref && ref.content) parts.push(`--- ${ref.name} ---\n${ref.content}`);
    });
  }

  parts.push(
    '输出要求: 优先给出可执行的成果（如代码、文档、方案、检查清单），再附简洁依据；严格遵循技能正文中的输出格式与命名约定；使用 Markdown 格式化。'
  );

  return `\n${parts.join('\n')}`;
}

/**
 * Check whether a skill object has usable methodology content.
 * Used by the UI to decide whether to show a "no methodology" hint.
 *
 * @param {object|null} skill
 * @returns {boolean}
 */
export function hasMethodology(skill) {
  return !!(skill && skill.skill_md && String(skill.skill_md).trim());
}

// ── Default Skills Context ──────────────────────────────────────────────
//
// When the user has NOT picked a custom skill chip (activeSkill is null),
// we inject a lightweight default-skills hint into the system prompt.
// This tells the LLM which 6 built-in artifact skills (docx, pptx, pdf,
// html, dashboard, markdown) are always available and how to invoke them.
//
// The full skill bodies are NOT injected — only names and trigger words
// (~200 tokens). The LLM is expected to call the matching skill via
// progressive disclosure (skill_view / skills tool) when needed.
//
// When the user HAS picked a custom skill chip, this function is NOT
// called (override precedence) — only the user-picked skill's context
// is injected via buildSkillContext().

/**
 * Default skills registry — mirrors backend DEFAULT_SKILLS manifest.
 * Each entry: { skill_name, triggers, format }.
 * Kept in sync with backend/app/services/synexia/default_skills.py.
 */
const DEFAULT_SKILLS_DATA = [
  { skill_name: 'docx', triggers: ['report', 'memo', 'word', 'docx', 'document'], format: 'docx' },
  { skill_name: 'pptx', triggers: ['deck', 'slides', 'presentation', 'powerpoint', 'pptx'], format: 'pptx' },
  { skill_name: 'pdf', triggers: ['pdf', 'export pdf'], format: 'pdf' },
  { skill_name: 'web-artifacts-builder', triggers: ['web page', 'html', 'interactive', 'web app', 'webpage'], format: 'html' },
  { skill_name: 'build-dashboard', triggers: ['dashboard', 'kpi', 'metrics', 'chart'], format: 'dashboard' },
  { skill_name: 'documentation', triggers: ['markdown', '.md', 'readme', 'docs', 'documentation'], format: 'md' },
];

/**
 * Build the system-prompt section that announces the built-in default
 * skills to the LLM. Only called when ``activeSkill`` is null (no override).
 *
 * @param {object|null} activeSkill - The currently active custom skill,
 *   or null if none is selected. Returns "" when a custom skill is active.
 * @returns {string} The default-skills context block, or "" when skipped.
 */
export function buildDefaultSkillContext(activeSkill) {
  // Override precedence — if the user picked a custom skill, inject nothing.
  // The custom skill's full context (via buildSkillContext) takes priority.
  if (activeSkill != null) return '';

  const lines = [
    '\n【内置默认技能 - 始终可用】',
    '以下技能始终对你可用。当用户要求产出交付物（报告、幻灯片、PDF、仪表盘、网页、文档）时，请按其名称调用对应技能。不要自行即兴发挥 — 使用技能的方法论。',
  ];

  for (const entry of DEFAULT_SKILLS_DATA) {
    const skillName = entry.skill_name;
    const triggers = entry.triggers.join('、');
    const format = entry.format;
    lines.push(`- **${skillName}** — 适用场景: ${triggers}（输出格式: ${format}）`);
  }

  return `\n${lines.join('\n')}`;
}

/**
 * Get the default skills list for UI display purposes.
 * Returns an array of { skill_name, triggers, format } entries.
 *
 * @returns {Array<{skill_name: string, triggers: string[], format: string}>}
 */
export function getDefaultSkillsList() {
  return DEFAULT_SKILLS_DATA;
}

/**
 * Check whether the active skill is a user-picked custom skill (not a
 * built-in default). Returns true when the user has a custom skill active.
 *
 * UI uses this to decide whether to show default-skills info.
 *
 * @param {object|null} activeSkill - The currently active skill object.
 * @returns {boolean}
 */
export function isOverrideSkill(activeSkill) {
  if (!activeSkill || !activeSkill.name) return false;
  const defaultNames = new Set(DEFAULT_SKILLS_DATA.map((d) => d.skill_name));
  return !defaultNames.has(activeSkill.name);
}
