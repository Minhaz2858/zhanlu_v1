/**
 * Enhanced agent API helpers — direct fetch wrappers for endpoints
 * that aren't covered by the base44 SDK (SSE streaming, permission mode).
 *
 * All endpoints use the ``/api/apps/${appId}/...`` prefix so the Vite
 * dev-server proxy forwards them to the local FastAPI backend, exactly
 * like the base44 SDK's own axios client. The previous version of this
 * file hardcoded ``/apps/undefined/...`` which worked only when the
 * request went through the base44 SDK's own baseURL (``/api``) — the
 * ``undefined`` app id and missing ``/api`` prefix caused 404s in
 * development.
 */
import { appParams } from '@/lib/app-params';
// Most call sites use `authFetch` (returns the raw Response). The
// streaming endpoint uses `authFetchOrThrow`, which raises a typed
// `SessionExpiredError` on a 401-with-no-recovery so the chat UI can
// distinguish "session expired" from a generic stream failure.
import { authFetch, authFetchOrThrow } from '@/api/authFetch';

const APP_ID = appParams.appId || 'undefined';
const API_BASE = `/api/apps/${APP_ID}`;

/**
 * Search the caller's chat history by message content.
 * @param {string} q search query
 * @param {number} limit max results (1..50)
 * @returns {Promise<{query: string, results: Array}>}
 */
export async function chatSearch(q, limit = 20) {
  const resp = await authFetch(
    `/api/chat/search?q=${encodeURIComponent(q)}&limit=${limit}`,
    { method: 'GET' }
  );
  if (!resp.ok) {
    throw new Error(`Chat search failed: ${resp.statusText}`);
  }
  return resp.json();
}

/**
 * Create (or reuse) a public share for a chat session.
 * @param {string} sessionId
 * @returns {Promise<{token: string, share_url: string}>}
 */
export async function chatShare(sessionId) {
  const resp = await authFetch(`/api/chat/shares`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId }),
  });
  if (!resp.ok) {
    throw new Error(`Failed to create share: ${resp.statusText}`);
  }
  return resp.json();
}

/**
 * Revoke the public share for a chat session.
 * @param {string} sessionId
 */
export async function chatRevokeShare(sessionId) {
  await authFetch(`/api/chat/shares/${encodeURIComponent(sessionId)}`, {
    method: 'DELETE',
  });
}

export async function setPermissionMode(conversationId, mode) {
  const resp = await authFetch(
    `${API_BASE}/agents/conversations/${conversationId}/permission-mode`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode }),
    }
  );
  if (!resp.ok) {
    throw new Error(`Failed to set permission mode: ${resp.statusText}`);
  }
  return resp.json();
}

/**
 * Get the current permission mode from conversation metadata.
 * @param {object} conversation
 * @returns {"default" | "plan" | "full_auto"}
 */
export function getPermissionMode(conversation) {
  if (!conversation) return 'default';
  const meta = conversation.metadata || {};
  return meta.permission_mode || 'default';
}

/**
 * Send a message and stream the agent's response via SSE.
 *
 * Returns an async generator that yields event objects:
 *   { type: "tool_progress", tool_calls: [...] }
 *   { type: "delta", content: "..." }
 *   { type: "done", content: "...", conversation: {...} }
 *   { type: "paused", conversation: {...} }
 *   { type: "error", message: "..." }
 *
 * @param {string} conversationId
 * @param {{ role: string, content: string, file_urls?: string[] }} message
 * @param {AbortSignal} [signal]
 */
export async function* streamAgentResponse(conversationId, message, signal) {
  // `authFetchOrThrow` raises a typed `SessionExpiredError` when a 401
  // comes back even after the silent refresh-and-retry. The chat UI's
  // catch block inspects this and shows "Session expired, please log in
  // again" instead of the generic "Sorry, the connection was interrupted"
  // — and critically, does NOT persist the error message to ChatMessage.
  const resp = await authFetchOrThrow(
    `${API_BASE}/agents/conversations/v3/${conversationId}/messages/stream`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(message),
      signal,
    }
  );

  if (!resp.ok) {
    throw new Error(`Stream request failed: ${resp.statusText}`);
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      // SSE events are separated by double newlines
      const events = buffer.split('\n\n');
      buffer = events.pop() || '';

      for (const rawEvent of events) {
        const line = rawEvent.trim();
        if (!line.startsWith('data: ')) continue;
        const jsonStr = line.slice(6);
        try {
          yield JSON.parse(jsonStr);
        } catch {
          // Skip malformed JSON
        }
      }
    }

    // Process any remaining buffered data
    if (buffer.trim().startsWith('data: ')) {
      try {
        yield JSON.parse(buffer.trim().slice(6));
      } catch {
        // Ignore
      }
    }
  } finally {
    reader.releaseLock();
  }
}

/**
 * Create a new agent conversation.
 *
 * The backend (`POST /apps/{app_id}/agents/conversations`) accepts
 * `{agent_name, title?, metadata}` and returns the created conversation.
 * The returned id is what you pass to `streamAgentResponse`.
 *
 * IMPORTANT — the ``title`` parameter is a TOP-LEVEL field on the
 * request body (the backend reads it via ``body.get("title", ...)``).
 * The previous version of this helper didn't expose ``title`` at all,
 * so callers that wanted to name a conversation had to smuggle it
 * inside ``metadata`` as ``{ name: "..." }`` — and the backend
 * happened to treat ``metadata.name`` as the title, which silently
 * broke for any caller that also wanted to use ``metadata.name`` for
 * a different purpose (e.g. the chat page stored the agent name
 * there, producing unreadable "general_assistant" entries in the
 * Project Detail "Recent Chats" list). Pass the title explicitly now
 * so the contract is obvious.
 *
 * @param {string} agentName — the AgentApp.name to bind to this conversation
 * @param {{description?: string, [k: string]: any}} [metadata]
 * @param {string} [title] — conversation title; pass an empty string
 *   if you have nothing meaningful yet (the backend defaults to
 *   "New Conversation" only when the field is absent, not when it's
 *   empty, so callers that don't have a title should pass undefined).
 * @returns {Promise<{id: string, agent_name: string, title: string, ...}>}
 */
export async function createAgentConversation(agentName, metadata = {}, title) {
  const resp = await authFetch(
    `${API_BASE}/agents/conversations`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        agent_name: agentName,
        ...(title !== undefined ? { title } : {}),
        metadata: { ...metadata, _source: 'chat_page' },
      }),
    }
  );
  if (!resp.ok) {
    throw new Error(`Failed to create conversation: ${resp.statusText}`);
  }
  return resp.json();
}

/**
 * Mid-turn steer — enqueue a user steer message for an in-flight v3 SSE
 * stream (P2). The backend drains pending steer messages between
 * tool-loop iterations and injects them into the next LLM call as user
 * messages. Returns immediately; does not touch the in-flight stream.
 *
 * Backend endpoint: ``POST /apps/{app_id}/agents/conversations/{id}/steer``
 * Body: ``{ message: str }`` (non-empty, max 8 KB).
 *
 * Throws on 4xx with a user-friendly message derived from the response
 * detail. Returns ``{ok: true, queued: true}`` on success.
 *
 * @param {string} conversationId
 * @param {string} message
 * @returns {Promise<{ok: boolean, queued: boolean}>}
 */
export async function steerAgentConversation(conversationId, message) {
  const resp = await authFetch(
    `${API_BASE}/agents/conversations/${conversationId}/steer`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message }),
    }
  );
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const j = await resp.json();
      if (j && j.detail) detail = j.detail;
    } catch {
      /* ignore */
    }
    if (resp.status === 429) {
      throw new Error('Steer queue is full — wait for the agent to make progress.');
    }
    if (resp.status === 404) {
      throw new Error('Conversation not found.');
    }
    if (resp.status === 400) {
      throw new Error(detail || 'Steer message is invalid.');
    }
    throw new Error(`Steer failed: ${detail}`);
  }
  return resp.json();
}

/**
 * Confirm (or cancel / edit-only) a pending Decision Summary (R4).
 *
 * Backend endpoint: ``POST /apps/{app_id}/agents/conversations/{id}/confirm-decision``
 * Body: ``{ action: 'create' | 'cancel' | 'edit_only', payload?: {...} }``
 *
 * @param {string} conversationId
 * @param {{action: 'create'|'cancel'|'edit_only', payload?: object}} body
 * @returns {Promise<{success: boolean, action: string, agent?: object, conversation: object}>}
 */
export async function confirmDecision(conversationId, body) {
  const resp = await authFetch(
    `${API_BASE}/agents/conversations/${conversationId}/confirm-decision`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || { action: 'cancel' }),
    }
  );
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const j = await resp.json();
      if (j && j.detail) detail = j.detail;
    } catch {
      /* ignore */
    }
    throw new Error(`confirm-decision failed: ${detail}`);
  }
  return resp.json();
}
