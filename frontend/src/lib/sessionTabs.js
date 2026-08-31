/**
 * Parallel-session tab helpers (Kimi/GPT-style). Pure functions so the
 * tab logic is unit-testable without React.
 */

export function openTab(tabs, sessionId, title, max = 8) {
  const rest = (tabs || []).filter((t) => t.sessionId !== sessionId);
  const next = [...rest, { sessionId, title: title || 'Chat' }];
  if (next.length > max) return next.slice(next.length - max);
  return next;
}

export function closeTab(tabs, sessionId) {
  const list = tabs || [];
  const idx = list.findIndex((t) => t.sessionId === sessionId);
  if (idx === -1) return { tabs: list, fallbackId: null };
  const next = list.filter((t) => t.sessionId !== sessionId);
  let fallbackId = null;
  if (next.length > 0) {
    // Prefer the previous neighbor; if the closed tab was first, the
    // next tab becomes active.
    fallbackId = next[Math.min(idx, next.length - 1)].sessionId;
  }
  return { tabs: next, fallbackId };
}
