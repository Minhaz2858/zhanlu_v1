// Pure formatting helpers for rendering automation execution progress in the
// status drawer. Kept dependency-free so they're trivial to unit-test.
//
// `steps` are the executor's activity steps (objects with `no` + `text`
// and/or `title`). `skill_calls` are SkillRun rows surfaced by
// GET /api/automations/executions/{id}/status in the shape
// { skill_name, action, status, duration_ms, at }.

export function stepText(step) {
  if (!step) return '';
  return (
    step.text ||
    step.title ||
    step.label ||
    step.description ||
    step.content ||
    `#${step.no || step.number || ''}`.trim()
  );
}

export function formatSkillCall(sc) {
  if (!sc || !sc.skill_name) return '';
  const name = sc.skill_name;
  const action = sc.action || 'load';
  if (action === 'execute' || action === 'run') return `Run ${name}`;
  return `Load ${name}`;
}

export function skillCallStatus(sc) {
  return (sc && sc.status) || '';
}

export function formatElapsed(seconds) {
  if (seconds == null || Number.isNaN(seconds)) return '';
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  if (m < 60) return rem ? `${m}m ${rem}s` : `${m}m`;
  const h = Math.floor(m / 60);
  const mm = m % 60;
  return `${h}h ${mm}m`;
}

export function formatStepTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleTimeString();
}
