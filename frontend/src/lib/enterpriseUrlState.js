/**
 * Phase 6F — Enterprise Intelligence URL state.
 *
 * Pure parse/build helpers behind the shareable deep-links of the
 * Enterprise Profile. The six conceptual areas are NAVIGATION GROUPS over
 * the existing experience tabs (same components and routes — no duplicate
 * functionality):
 *
 *   Business Overview     A Executive · G Business Health · B Situation Room · E Outcomes
 *   How We Work           M Enterprise Map · F Processes
 *   Business Knowledge    C Business Objects · K Business Rules
 *   Enterprise Resources  S Resources
 *   AI Workforce          W Assistants & context
 *   Administration        D Decisions · R Review (admin)
 */

export const ENTERPRISE_TABS = {
  A: 'Executive Overview',
  G: 'Business Health',
  B: 'Situation Room',
  E: 'Outcomes',
  M: 'Enterprise Map',
  F: 'Processes',
  C: 'Business Objects',
  K: 'Business Rules',
  S: 'Resources',
  W: 'AI Workforce',
  D: 'Decisions',
  R: 'Review',
};

export const ADMIN_TABS = ['R'];

export const ENTERPRISE_AREAS = [
  { key: 'overview', label: 'Business Overview', tabs: ['A', 'G', 'B', 'E'] },
  { key: 'work', label: 'How We Work', tabs: ['M', 'F'] },
  { key: 'knowledge', label: 'Business Knowledge', tabs: ['C', 'K'] },
  { key: 'resources', label: 'Enterprise Resources', tabs: ['S'] },
  { key: 'workforce', label: 'AI Workforce', tabs: ['W'] },
  { key: 'admin', label: 'Administration', tabs: ['D', 'R'] },
];

const VALID_EXPS = new Set(Object.keys(ENTERPRISE_TABS));

const _ref = (v) => (typeof v === 'string' && v.trim() ? v.trim() : null);

/**
 * Parse URL search params into the enterprise view state. Unknown/blank
 * refs are dropped; an invalid or non-permitted tab falls back to the
 * Enterprise Map ('M') — a deep link NEVER renders a view the user may
 * not access (admin tabs are additionally filtered via `adminKeys`).
 */
export function parseEnterpriseView(params, { adminKeys = [] } = {}) {
  const raw = params.get('exp');
  const allowed = VALID_EXPS.has(raw) &&
    (!ADMIN_TABS.includes(raw) || adminKeys.includes(raw));
  return {
    exp: allowed ? raw : 'M',
    objectId: _ref(params.get('object')),
    resourceId: _ref(params.get('resource')),
    ruleId: _ref(params.get('rule')),
  };
}

/** Inverse of parseEnterpriseView — omits defaults and null refs. */
export function buildEnterpriseSearch(view) {
  const out = new URLSearchParams();
  if (view.exp && view.exp !== 'M') out.set('exp', view.exp);
  if (view.objectId) out.set('object', view.objectId);
  if (view.resourceId) out.set('resource', view.resourceId);
  if (view.ruleId) out.set('rule', view.ruleId);
  return out.toString();
}
