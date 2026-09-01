/**
 * Feedback API client for the BI experience layer (Phase C).
 *
 * POSTs an explicit thumbs up/down rating for an assistant message to
 * `/api/apps/{appId}/agents/conversations/{convId}/messages/{messageId}/feedback`.
 * The backend records an ExperienceEntry and adjusts the matching recipe,
 * semantic cache entry, and user profile accordingly.
 */
import { authFetch } from './authFetch';

/**
 * Submit feedback for an assistant message.
 *
 * @param {string} appId
 * @param {string} conversationId
 * @param {string} messageId
 * @param {1|-1} rating   +1 = thumbs up, -1 = thumbs down
 * @param {string} [comment]
 * @returns {Promise<{ok: boolean, rating: number, intent_class?: string, message_id: string}>}
 */
export async function postMessageFeedback(appId, conversationId, messageId, rating, comment = '') {
  const res = await authFetch(
    `/api/apps/${appId}/agents/conversations/${conversationId}/messages/${messageId}/feedback`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rating, comment }),
    },
  );
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error || `feedback request failed (${res.status})`);
  }
  return res.json();
}

/**
 * Submit a 1-5 "Relevant to your role?" rating for an assistant message.
 *
 * @param {string} appId
 * @param {string} conversationId
 * @param {string} messageId
 * @param {number} rating   1-5 role-relevance rating
 * @returns {Promise<{ok: boolean, rating: number, message_id: string, role_snapshot: string[]}>}
 */
export async function postRoleFeedback(appId, conversationId, messageId, rating) {
  const res = await authFetch(
    `/api/apps/${appId}/agents/conversations/${conversationId}/messages/${messageId}/role-feedback`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rating }),
    },
  );
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error || `role feedback request failed (${res.status})`);
  }
  return res.json();
}

export default postMessageFeedback;
