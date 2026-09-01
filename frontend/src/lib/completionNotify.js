/**
 * Completion-notification decision helpers (Kimi/GPT-style browser
 * notifications when a background run finishes).
 *
 * Pure functions — testable without a browser. The Chat.jsx wiring is
 * responsible for requesting Notification permission once (user
 * gesture) and calling the helpers at stream-completion time.
 */

export function shouldNotify({ hidden, permission, hasFinalMessage }) {
  if (!hidden) return false;
  if (permission !== 'granted') return false;
  if (!hasFinalMessage) return false;
  return true;
}

export function notificationBody(finalMessage, maxLen = 200) {
  let text = (finalMessage || '').replace(/```[\s\S]*?```/g, '').trim();
  if (!text) return 'Task finished';
  text = text.replace(/\s+/g, ' ').trim();
  if (text.length > maxLen) {
    return `${text.slice(0, maxLen - 1)}…`;
  }
  return text;
}
