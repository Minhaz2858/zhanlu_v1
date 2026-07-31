import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// We control the appId via a module-level variable. The vi.mock factory
// reads it at hoist-time so we have to declare the value with vi.hoisted.
const { mockAppId } = vi.hoisted(() => ({ mockAppId: 'test-app-123' }));

vi.mock('@/lib/app-params', () => ({
  appParams: { appId: mockAppId },
}));

import { createAgentConversation, streamAgentResponse, setPermissionMode } from '@/api/agentEnhanced';

describe('agentEnhanced URL construction', () => {
  let originalFetch;

  beforeEach(() => {
    originalFetch = global.fetch;
  });
  afterEach(() => {
    global.fetch = originalFetch;
  });

  function captureFetch() {
    const calls = [];
    global.fetch = vi.fn(async (url, opts) => {
      calls.push({ url, opts });
      return {
        ok: true,
        status: 200,
        statusText: 'OK',
        json: async () => ({ id: 'conv-1', agent_name: 'X' }),
        body: {
          getReader: () => ({
            read: async () => ({ done: true, value: undefined }),
            releaseLock: () => {},
          }),
        },
      };
    });
    return calls;
  }

  it('createAgentConversation uses /api/apps/{appId}/agents/conversations (not /apps/undefined/...)', async () => {
    const calls = captureFetch();
    await createAgentConversation('Industry Research Analyst');
    expect(calls).toHaveLength(1);
    const url = calls[0].url;
    // The path must start with /api/apps/<our-appId>/agents/conversations
    expect(url.startsWith(`/api/apps/${mockAppId}/agents/conversations`)).toBe(true);
    // The legacy /apps/undefined/ path that 404s in dev must NOT be used.
    expect(url).not.toContain('/apps/undefined/');
    // POST with the right body shape.
    const body = JSON.parse(calls[0].opts.body);
    expect(body.agent_name).toBe('Industry Research Analyst');
  });

  it('createAgentConversation sends top-level ``title`` when provided (and omits it when undefined)', async () => {
    // The backend reads ``title`` via ``body.get("title", ...)`` —
    // it's a top-level field, NOT inside ``metadata``. Passing it
    // inside metadata caused the "general_assistant" Recent Chats
    // regression because the backend also treats
    // ``metadata.name`` as the title. Pin the new contract:
    //   1. title is a top-level field when provided
    //   2. title is absent when undefined (so the backend's
    //      "New Conversation" default kicks in)
    const calls = captureFetch();

    // With a title — it goes to the top level
    await createAgentConversation(
      'general_assistant',
      { description: 'desc' },
      'tell me about my database',
    );
    let body = JSON.parse(calls[0].opts.body);
    expect(body.title).toBe('tell me about my database');
    // ``title`` must NOT be inside metadata (it would re-trigger
    // the bug if the backend ever reads ``metadata.title``).
    expect(body.metadata).not.toHaveProperty('title');
    // ``name`` must NOT be inside metadata either — the old
    // call sites used to send ``name: activeAgent.name`` here,
    // which the backend silently treated as the title.
    expect(body.metadata).not.toHaveProperty('name');

    // Without a title (undefined) — it's absent so the backend
    // default kicks in. Passing ``""`` would store an empty title,
    // which is worse than the default.
    await createAgentConversation('general_assistant', { description: 'desc' });
    body = JSON.parse(calls[1].opts.body);
    expect(body).not.toHaveProperty('title');
  });

  it('streamAgentResponse uses /api/apps/{appId}/agents/conversations/v3/{cid}/messages/stream', async () => {
    const calls = captureFetch();
    // Consume one event from the generator so fetch is invoked.
    const gen = streamAgentResponse('conv-xyz', { role: 'user', content: 'hi' });
    await gen.next();
    expect(calls).toHaveLength(1);
    const url = calls[0].url;
    expect(url).toContain(`/api/apps/${mockAppId}/agents/conversations/v3/conv-xyz/messages/stream`);
    expect(url).not.toContain('/apps/undefined/');
  });

  it('setPermissionMode uses /api/apps/{appId}/agents/conversations/{cid}/permission-mode', async () => {
    const calls = captureFetch();
    await setPermissionMode('conv-xyz', 'plan');
    expect(calls).toHaveLength(1);
    const url = calls[0].url;
    expect(url).toContain(`/api/apps/${mockAppId}/agents/conversations/conv-xyz/permission-mode`);
    expect(url).not.toContain('/apps/undefined/');
  });
});
