import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import React from 'react';
import { ChatSessionProvider, useChatSession } from '@/lib/ChatSessionContext';

const { mockList, mockDelete, mockDeleteMany, mockUpdate } = vi.hoisted(() => ({
  mockList: vi.fn().mockResolvedValue([]),
  mockDelete: vi.fn().mockResolvedValue({}),
  mockDeleteMany: vi.fn().mockResolvedValue({}),
  mockUpdate: vi.fn().mockResolvedValue({}),
}));

vi.mock('@/api/base44Client', () => ({
  base44: {
    entities: {
      ChatSession: {
        list: mockList,
        delete: mockDelete,
        update: mockUpdate,
      },
      ChatMessage: {
        deleteMany: mockDeleteMany,
      },
    },
  },
}));

function wrapWithProvider({ children }) {
  return React.createElement(ChatSessionProvider, null, children);
}

describe('ChatSessionContext', () => {
  beforeEach(() => {
    mockList.mockReset();
    mockDelete.mockReset();
    mockUpdate.mockReset();
    mockDeleteMany.mockReset();
    mockList.mockResolvedValue([]);
    mockDelete.mockResolvedValue({});
    mockUpdate.mockResolvedValue({});
    mockDeleteMany.mockResolvedValue({});
  });

  it('exposes default hooks when no provider is present', () => {
    // The no-provider fallback exists so consumers don't crash if the
    // provider is missing (e.g. in a unit test that mounts a single
    // component in isolation). All actions become no-ops, state is empty.
    const { result } = renderHook(() => useChatSession());
    expect(result.current.sessions).toEqual([]);
    expect(result.current.activeId).toBeNull();
    expect(result.current.pendingProject).toBeNull();
    expect(result.current.loading).toBe(false);
  });

  it('loads sessions on mount', async () => {
    mockList.mockResolvedValueOnce([
      { id: 's1', title: 'Chat 1' },
      { id: 's2', title: 'Chat 2' },
    ]);
    const { result, rerender } = renderHook(() => useChatSession(), { wrapper: wrapWithProvider });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 10));
    });
    rerender();
    expect(mockList).toHaveBeenCalledWith('-updated_date', 100);
    expect(result.current.sessions).toHaveLength(2);
    expect(result.current.loading).toBe(false);
  });

  it('selectSession updates activeId', () => {
    const { result } = renderHook(() => useChatSession(), { wrapper: wrapWithProvider });
    act(() => result.current.selectSession('sess-42'));
    expect(result.current.activeId).toBe('sess-42');
  });

  it('selectSession syncs ?conv= in the URL to the session conversation_id', async () => {
    // The sidebar's "click a row" path calls selectSession from this
    // context. Previously the URL was only written by Chat.jsx's
    // handleAgentSend (first send of a brand-new session) — switching
    // between existing sessions left the URL pointing at the original
    // conv id, so a reload / share resumed the wrong conversation.
    // The fix is to keep the URL in sync here.
    mockList.mockResolvedValueOnce([
      { id: 's1', title: 'Chat 1', conversation_id: 'conv-A' },
      { id: 's2', title: 'Chat 2', conversation_id: 'conv-B' },
    ]);
    const { result } = renderHook(() => useChatSession(), { wrapper: wrapWithProvider });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 10));
    });

    // Start from a clean URL — neither ?conv= nor any other param.
    window.history.replaceState({}, '', '/');

    act(() => result.current.selectSession('s1'));
    expect(window.location.search).toContain('conv=conv-A');

    // Switch to a different session — URL must follow.
    act(() => result.current.selectSession('s2'));
    expect(window.location.search).toContain('conv=conv-B');
  });

  it('selectSession drops ?conv= when the selected session has no conversation_id', async () => {
    // Brand-new sessions have no linked AgentConversation yet. Clicking
    // such a row in the sidebar must clear any stale ?conv= from a
    // previous session, otherwise a reload mid-context would jump back
    // to the old conv.
    mockList.mockResolvedValueOnce([
      { id: 's1', title: 'Old', conversation_id: 'conv-OLD' },
      { id: 's2', title: 'New', conversation_id: null },
    ]);
    const { result } = renderHook(() => useChatSession(), { wrapper: wrapWithProvider });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 10));
    });

    // Pre-seed URL as if we just came from a previous session.
    window.history.replaceState({}, '', '/?conv=conv-OLD');

    act(() => result.current.selectSession('s2'));
    expect(window.location.search).not.toContain('conv=');
  });

  it('selectSession preserves other URL params when syncing ?conv=', async () => {
    // A reload should re-hydrate project context too — don't strip
    // unrelated params just because we're updating ?conv=.
    mockList.mockResolvedValueOnce([
      { id: 's1', title: 'Chat', conversation_id: 'conv-X' },
    ]);
    const { result } = renderHook(() => useChatSession(), { wrapper: wrapWithProvider });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 10));
    });

    window.history.replaceState({}, '', '/?project=42&projectName=ACME');

    act(() => result.current.selectSession('s1'));
    expect(window.location.search).toContain('conv=conv-X');
    expect(window.location.search).toContain('project=42');
    expect(window.location.search).toContain('projectName=ACME');
  });

  it('newChat clears activeId and stages the project', () => {
    const { result } = renderHook(() => useChatSession(), { wrapper: wrapWithProvider });
    act(() => result.current.selectSession('sess-1'));
    act(() => result.current.newChat('My Project'));
    expect(result.current.activeId).toBeNull();
    expect(result.current.pendingProject).toBe('My Project');
  });

  it('setPendingProject sets name + optional id', () => {
    const { result } = renderHook(() => useChatSession(), { wrapper: wrapWithProvider });
    act(() => result.current.setPendingProject('Project A', 'pid-123'));
    expect(result.current.pendingProject).toBe('Project A');
    expect(result.current.pendingProjectId).toBe('pid-123');

    act(() => result.current.setPendingProject('Project B'));
    expect(result.current.pendingProject).toBe('Project B');
    expect(result.current.pendingProjectId).toBeNull();

    act(() => result.current.setPendingProject(null));
    expect(result.current.pendingProject).toBeNull();
    expect(result.current.pendingProjectId).toBeNull();
  });

  it('prependSession adds a session to the top without duplicating', async () => {
    mockList.mockResolvedValueOnce([
      { id: 's1', title: 'Chat 1' },
    ]);
    const { result } = renderHook(() => useChatSession(), { wrapper: wrapWithProvider });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 10));
    });
    expect(result.current.sessions).toHaveLength(1);

    act(() => result.current.prependSession({ id: 's2', title: 'Chat 2' }));
    expect(result.current.sessions[0].id).toBe('s2');
    expect(result.current.sessions).toHaveLength(2);

    // Prepending the same session again should not duplicate.
    act(() => result.current.prependSession({ id: 's2', title: 'Chat 2' }));
    expect(result.current.sessions).toHaveLength(2);
  });

  it('deleteSession removes from the list and calls backend', async () => {
    mockList.mockResolvedValueOnce([
      { id: 's1', title: 'Chat 1' },
      { id: 's2', title: 'Chat 2' },
    ]);
    const { result } = renderHook(() => useChatSession(), { wrapper: wrapWithProvider });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 10));
    });
    expect(result.current.sessions).toHaveLength(2);

    await act(async () => {
      await result.current.deleteSession({ id: 's1' });
    });
    expect(mockDelete).toHaveBeenCalledWith('s1');
    expect(mockDeleteMany).toHaveBeenCalledWith({ session_id: 's1' });
    expect(result.current.sessions).toHaveLength(1);
    expect(result.current.sessions[0].id).toBe('s2');
  });

  it('starSession toggles the starred flag and persists', async () => {
    mockList.mockResolvedValueOnce([
      { id: 's1', title: 'Chat 1', starred: false },
    ]);
    const { result } = renderHook(() => useChatSession(), { wrapper: wrapWithProvider });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 10));
    });

    await act(async () => {
      await result.current.starSession({ id: 's1', starred: false });
    });
    expect(mockUpdate).toHaveBeenCalledWith('s1', { starred: true });
    expect(result.current.sessions[0].starred).toBe(true);

    await act(async () => {
      await result.current.starSession({ id: 's1', starred: true });
    });
    expect(mockUpdate).toHaveBeenLastCalledWith('s1', { starred: false });
    expect(result.current.sessions[0].starred).toBe(false);
  });

  it('renameSession updates title and persists', async () => {
    mockList.mockResolvedValueOnce([
      { id: 's1', title: 'Old' },
    ]);
    const { result } = renderHook(() => useChatSession(), { wrapper: wrapWithProvider });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 10));
    });

    await act(async () => {
      await result.current.renameSession('s1', 'New Title');
    });
    expect(mockUpdate).toHaveBeenCalledWith('s1', { title: 'New Title' });
    expect(result.current.sessions[0].title).toBe('New Title');
  });

  it('adoptSessionProject copies the session project into pending', async () => {
    mockList.mockResolvedValueOnce([
      { id: 's1', title: 'Chat 1', project: 'Proj A', project_id: 'pid-1' },
    ]);
    const { result } = renderHook(() => useChatSession(), { wrapper: wrapWithProvider });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 10));
    });

    act(() => result.current.adoptSessionProject('s1'));
    expect(result.current.pendingProject).toBe('Proj A');
    expect(result.current.pendingProjectId).toBe('pid-1');
  });

  it('touchSession bubbles the session to the top with fresh timestamp', async () => {
    mockList.mockResolvedValueOnce([
      { id: 's1', title: 'Chat 1', last_message_at: '2026-01-01T00:00:00Z' },
      { id: 's2', title: 'Chat 2', last_message_at: '2026-01-02T00:00:00Z' },
    ]);
    const { result } = renderHook(() => useChatSession(), { wrapper: wrapWithProvider });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 10));
    });

    act(() => result.current.touchSession('s1'));
    expect(result.current.sessions[0].id).toBe('s1');
    expect(result.current.sessions[0].last_message_at).not.toBe('2026-01-01T00:00:00Z');
  });

  it('getSession returns the matching session or null', async () => {
    mockList.mockResolvedValueOnce([
      { id: 's1', title: 'Chat 1' },
    ]);
    const { result } = renderHook(() => useChatSession(), { wrapper: wrapWithProvider });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 10));
    });

    expect(result.current.getSession('s1')?.title).toBe('Chat 1');
    expect(result.current.getSession('nope')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// chatGeneration — monotonic counter bumped by newChat() so consumers
// can react even when activeId was already null. Regression: the "+ New
// Task" button silently no-op'd when the user landed on the chat via
// a `?conv=<id>` deep link, because the reset effect depended on
// [activeId] alone and activeId was already null.
// ---------------------------------------------------------------------------

describe('ChatSessionContext -- chatGeneration (newChat counter)', () => {
  beforeEach(() => {
    mockList.mockReset();
    mockList.mockResolvedValue([]);
  });

  it('starts at 0 on a fresh provider', () => {
    const { result } = renderHook(() => useChatSession(), { wrapper: wrapWithProvider });
    expect(result.current.chatGeneration).toBe(0);
  });

  it('newChat(null) bumps chatGeneration by 1', () => {
    const { result } = renderHook(() => useChatSession(), { wrapper: wrapWithProvider });
    const before = result.current.chatGeneration;
    act(() => result.current.newChat(null));
    expect(result.current.chatGeneration).toBe(before + 1);
    expect(result.current.activeId).toBeNull();
  });

  it('newChat(name) also bumps chatGeneration', () => {
    const { result } = renderHook(() => useChatSession(), { wrapper: wrapWithProvider });
    const before = result.current.chatGeneration;
    act(() => result.current.newChat('ACME'));
    expect(result.current.chatGeneration).toBe(before + 1);
  });

  it('chatGeneration bumps even when activeId was already null (the regression)', () => {
    const { result } = renderHook(() => useChatSession(), { wrapper: wrapWithProvider });
    expect(result.current.activeId).toBeNull();
    expect(result.current.chatGeneration).toBe(0);

    act(() => result.current.newChat(null));
    expect(result.current.activeId).toBeNull();
    expect(result.current.chatGeneration).toBe(1);

    act(() => result.current.newChat(null));
    expect(result.current.chatGeneration).toBe(2);
  });

  it('selectSession does NOT bump chatGeneration', () => {
    const { result } = renderHook(() => useChatSession(), { wrapper: wrapWithProvider });
    const before = result.current.chatGeneration;
    act(() => result.current.selectSession('sess-42'));
    expect(result.current.activeId).toBe('sess-42');
    expect(result.current.chatGeneration).toBe(before);
  });
});
