// Helpers for working with fields that *should* always be string arrays
// but historically were stored as comma-separated strings or as `null`.
// The AgentApp JSON columns (`capabilities`, `skills`,
// `knowledge_bases`, `sub_agents`) accept any JSON-serializable value on
// the backend, so older rows can come back as a bare string from the
// API. Downstream React components treat these fields strictly as
// arrays of strings and would otherwise crash with
// "form.X.filter is not a function" (or a similar `.map is not a
// function` TypeError), which used to unmount the whole SPA before
// `PageErrorBoundary` was added at `AppLayout`.

function splitString(value) {
  // Split on Chinese / English commas, semicolons, en/em dashes used as
  // separators in zh-CN input ("数据分析, 报表生成"), and slashes —
  // matches what `parseCapabilities` in StepsAgentBuilder does at the
  // write boundary.
  return value
    .split(/[,，;；、/]/g)
    .map((s) => s.trim())
    .filter(Boolean);
}

export function coerceStringArray(value) {
  if (Array.isArray(value)) {
    return value.map((s) => String(s).trim()).filter(Boolean);
  }
  if (typeof value !== 'string') return [];
  return splitString(value);
}

/**
 * Same as `coerceStringArray` but preserves `null` for form state when
 * the field is `null`. Useful for ops that want to distinguish "user
 * cleared it" from "never set".
 */
export function coerceStringArrayOrNull(value) {
  if (value == null || value === '') return null;
  const out = coerceStringArray(value);
  return out.length > 0 ? out : null;
}
