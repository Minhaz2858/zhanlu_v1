import React, { createContext, useContext, useState, useEffect, useCallback, useRef } from 'react';
import { base44 } from '@/api/base44Client';
import { isUngroupedProjectName } from '@/lib/projectGrouping';

/**
 * ChatSessionContext — single source of truth for chat session state
 * across the app.
 *
 * Why this exists (Option A: unified sidebar):
 *   Previously, the chat session list lived inside `Chat.jsx` and was
 *   rendered as a separate sidebar next to the main nav sidebar. The
 *   main nav sidebar and the session list each took ~250px of horizontal
 *   space, which left very little room for the actual chat on small
 *   windows. The new design merges them into one route-aware sidebar
 *   (`Sidebar.jsx`) — but that means the session state has to be
 *   accessible to BOTH the sidebar (which renders the list) and the
 *   chat page (which renders the active conversation). Hoisting the
 *   state to a context keeps both consumers in sync without prop
 *   drilling and without making the Sidebar component "smart" about
 *   chat internals.
 *
 *   The provider is mounted in `AppLayout` so it survives route changes
 *   (the user can switch from Chat to My Space and back without losing
 *   their session list / active selection).
 *
 * Public surface (consumed via `useChatSession()`):
 *
 *   State:
 *     - sessions: ChatSession[] (sorted by -updated_date)
 *     - activeId: string | null
 *     - pendingProject: string | null   (the project the next new chat
 *                                        will be tagged with)
 *     - pendingProjectId: string | null (FK form of pendingProject, for
 *                                        the new ChatSession row)
 *     - loading: boolean                (true while the initial list
 *                                        fetch is in flight)
 *
 *   Actions:
 *     - setActiveId(id)
 *     - setPendingProject(name, id?)    (pass id=null to clear the FK)
 *     - selectSession(id)               (sets activeId; also adopts
 *                                        that session's project so the
 *                                        next new chat inherits it)
 *     - newChat(projectName)            (clears activeId + messages —
 *                                        handled by the chat page; this
 *                                        just stages the project. Also
 *                                        bumps `chatGeneration`.)
 *     - chatGeneration: number          (monotonic counter; consumers
 *                                        can include this in their
 *                                        effect deps so they react to
 *                                        "new chat" even when activeId
 *                                        was already null)
 *     - deleteSession(s)                (removes from list, deletes row)
 *     - starSession(s)                  (toggles starred)
 *     - renameSession(id, title)        (updates title)
 *     - refreshSessions()               (re-fetches the list)
 */
const ChatSessionContext = createContext(null);

export function ChatSessionProvider({ children }) {
  const [sessions, setSessions] = useState([]);
  const [activeId, setActiveId] = useState(null);
  const [pendingProject, setPendingProjectState] = useState(null);
  const [pendingProjectId, setPendingProjectId] = useState(null);
  const [loading, setLoading] = useState(true);
  // Monotonic counter bumped every time the user asks for a fresh
  // chat (newChat). Consumers like Chat.jsx that reset their local
  // message/state on `activeId` change ALSO watch this counter so
  // they re-fire when the user clicks "+ New Task" while already
  // in a "messages loaded but activeId is null" state — e.g. when
  // the chat was opened via a `?conv=<id>` deep link, where the URL
  // handler populates messages without ever setting activeId. Without
  // this counter, clicking "+ New Task" in that state would silently
  // no-op (activeId was already null, so the dep-driven effect
  // wouldn't fire and messages would stay on screen).
  const [chatGeneration, setChatGeneration] = useState(0);
  // Generation counter to prevent stale async updates from overwriting
  // newer state. Bumped on every delete/star/rename so an in-flight
  // request that resolves late is ignored if it doesn't match the
  // current generation.
  const genRef = useRef(0);

  // --- Initial load ---
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    base44.entities.ChatSession.list('-updated_date', 100)
      .then((list) => {
        if (cancelled) return;
        setSessions(list || []);
      })
      .catch(() => {
        if (cancelled) return;
        setSessions([]);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, []);

  // --- Refresh helper (used after creating a new session, etc.) ---
  const refreshSessions = useCallback(async () => {
    try {
      const list = await base44.entities.ChatSession.list('-updated_date', 100);
      setSessions(list || []);
    } catch {
      /* best-effort — keep the stale list */
    }
  }, []);

  // --- setPendingProject: always set name + optional id together ---
  const setPendingProject = useCallback((name, id = null) => {
    setPendingProjectState(name || null);
    setPendingProjectId(id || null);
  }, []);

  // --- Select a session (also adopts its project so the project chip
  //     reappears on the chat input and the next send routes through the
  //     project-aware agent runtime) ---
  const selectSession = useCallback((id) => {
    setActiveId(id);
    // Adopt the session's project so the next new chat inherits it.
    // Previously this was only done in Chat.jsx's handleSelectSession,
    // but the SessionList sidebar calls selectSession directly from this
    // context — bypassing Chat.jsx — so the project chip never appeared
    // when reopening a session from a project group. Adopt here so the
    // chip and project-scoped agent routing work regardless of which
    // entry point selected the session.
    const s = sessions.find((x) => x.id === id);
    if (s) {
      // The session's ``project`` string is the source of truth
      // for the project name — the sidebar groups sessions by
      // this same string (SessionList.jsx uses
      // normalizeGroupKey(s.project, t)), and legacy rows may
      // have ``project`` set even when the ``project_id`` FK
      // couldn't be backfilled. Migration 020 only matches by
      // name + created_by_id, so sessions whose project was
      // created by a different user (or after the migration
      // ran) keep ``project = "ACME"`` but have
      // ``project_id = null``. Gating the chip on the FK would
      // hide the project name on reopen even though the sidebar
      // groups the session correctly — so gate on the string
      // and pass the FK through as-is for the next send.
      const projectName = s.project && !isUngroupedProjectName(s.project)
        ? s.project
        : null;
      setPendingProjectState(projectName);
      setPendingProjectId(s.project_id || null);
      // Clear the automation-run unread flag when the user opens the
      // session. Optimistic local update + best-effort persistence —
      // same pattern as starSession / renameSession. Guarded by the
      // current value so we don't write on every click.
      if (s.unread) {
        setSessions((prev) => prev.map((x) => (x.id === id ? { ...x, unread: false } : x)));
        base44.entities.ChatSession.update(id, { unread: false }).catch(() => {});
      }
      // Keep the URL's ?conv= in sync with the active session's
      // AgentConversation. Previously this URL was only written by
      // Chat.jsx's handleAgentSend (on first send of a brand-new
      // session) and by handleNewChat (delete on "+ New Task"). When
      // the user clicked a different row in the sidebar the URL
      // stayed stale — the in-memory chat content updated correctly
      // (because activeId drove the messages) but a reload / share
      // would resume the wrong conversation. Do it here because
      // selectSession is the single entry point both the sidebar and
      // any future session picker use.
      //
      // Also forward the session's project context in the URL so
      // Chat.jsx's handleAgentSend can read it from the live URL
      // and include it in the v3 stream body — the backend then
      // scopes the data-source runtime to the right project.
      // Without this, clicking a conv in a different project (e.g.
      // between a project and Global) leaves the URL as ``?conv=...`` only,
      // the agent has no project_id to extend its bound KBs with,
      // and the user sees the same generic "user-memory" response
      // across every project (because the only thing the runtime
      // can fall back to is the per-user memory store). The
      // ``?project=`` FK lets the backend resolve the project's
      // KBs server-side even when ``s.project_id`` is null (legacy
      // rows backfilled by name only — see migration 020).
      try {
        const url = new URL(window.location.href);
        if (s.conversation_id) {
          url.searchParams.set('conv', s.conversation_id);
        } else {
          // Brand-new session with no first message yet — no conv
          // row exists, so drop the stale ?conv= from any previous
          // session.
          url.searchParams.delete('conv');
        }
        if (s.project_id) {
          url.searchParams.set('project', s.project_id);
        } else if (!s.conversation_id) {
          // (2026-08-31) PRESERVE any pre-existing ?project= for
          // conv-linked sessions — the session row may lack the FK
          // (legacy rows, or a conv whose project context lives in
          // the URL already). The previous delete() here wiped
          // ?project= / ?projectName= on every sidebar click of a
          // session without an FK, so a reload ended up as
          // ``?conv=...&agentName=...`` with no project context —
          // exactly the bug the user reported. Only clear project
          // params for BRAND-NEW sessions (no conversation_id yet)
          // so a fresh global chat never inherits a stale project.
          url.searchParams.delete('project');
        }
        if (projectName) {
          url.searchParams.set('projectName', projectName);
        } else if (!s.conversation_id) {
          url.searchParams.delete('projectName');
        }
        // Same preservation rule for ?projectName= — only overwrite
        // when the session has its own project name, and only clear
        // for brand-new sessions.
        if (s.agent_name) {
          url.searchParams.set('agentName', s.agent_name);
        } else {
          url.searchParams.delete('agentName');
        }
        window.history.replaceState({}, '', url.toString());
      } catch { /* SSR / non-browser env — best-effort */ }
    }
  }, [sessions]);

  // --- getSession: read a session from the current list ---
  const getSession = useCallback((id) => {
    if (!id) return null;
    return sessions.find((s) => s.id === id) || null;
  }, [sessions]);

  // --- Adopt a session's project into pendingProject ---
  const adoptSessionProject = useCallback((id) => {
    const s = sessions.find((x) => x.id === id);
    if (s) {
      // Same normalization as selectSession: use s.project
      // (the string) as the source of truth for the project
      // name. Legacy rows that have ``s.project = "Ungrouped"``
      // (the i18n placeholder) are treated as ungrouped —
      // ``pendingProject`` becomes null and the chat input
      // doesn't render a "Ungrouped" chip. Legacy rows that
      // have a real project name but no project_id FK (missed
      // by migration 020's backfill) still get the chip, so
      // the project context is restored on reopen.
      const projectName = s.project && !isUngroupedProjectName(s.project)
        ? s.project
        : null;
      setPendingProjectState(projectName);
      setPendingProjectId(s.project_id || null);
    }
  }, [sessions]);

  // --- Create a new chat (clears activeId; the consumer wires up
  //     its own message-clearing on top of this). ---
  const newChat = useCallback((projectName) => {
    setActiveId(null);
    // Bump chatGeneration so consumers watching this counter (e.g.
    // Chat.jsx's reset effect) re-fire even when activeId was
    // already null. Without this, clicking "+ New Task" in a
    // "messages loaded but no active session" state — the
    // ?conv=<id> deep-link entry point, for example — silently
    // no-ops because the dep-driven effect doesn't see a change.
    setChatGeneration((g) => g + 1);
    // Normalize the project name: callers (the "+ New Task"
    // button, the project picker) can theoretically pass the
    // "Ungrouped" placeholder (e.g. for an explicit "no
    // project" choice), but the chat input must treat that as
    // null so it doesn't render a "Ungrouped" chip. Use the
    // shared helper to keep this consistent with selectSession
    // and adoptSessionProject.
    const normalizedName = isUngroupedProjectName(projectName) ? null : (projectName || null);
    setPendingProjectState(normalizedName);
    // Sync the URL with the project context the same way
    // selectSession does. Without this, "+ New Chat" in a sidebar
    // group (e.g. Marketing Team) leaves the URL as
    // ``/?`` with no projectName, so the v3 stream request body
    // (read from window.location by Chat.jsx's handleAgentSend)
    // has no project_id to scope the data-source runtime with —
    // the agent then falls back to per-user memory across all
    // projects. Drop ?conv= and ?agentName= so the next send
    // creates a fresh AgentConversation row (instead of reusing
    // the previously-active conv's id) and is not bound to a
    // stale agent.
    //
    // We intentionally do NOT set ``?project=`` here — the newChat
    // caller (sidebar "+ New Chat" button) only has the project
    // NAME, not the FK. Resolving name → FK on the client is racy
    // and the chat's own URL-driven resolution in Chat.jsx's
    // handleAgentSend will look up the FK by name on the server
    // if it sees only projectName. (See backend
    // ``add_message_stream`` v3: validates body project_id, then
    // falls back to name lookup against the live projects table.)
    try {
      const url = new URL(window.location.href);
      url.searchParams.delete('conv');
      url.searchParams.delete('agentName');
      url.searchParams.delete('project');
      if (normalizedName) {
        url.searchParams.set('projectName', normalizedName);
      } else {
        url.searchParams.delete('projectName');
      }
      window.history.replaceState({}, '', url.toString());
    } catch { /* SSR / non-browser env — best-effort */ }
  }, []);

  // --- Delete a session ---
  const deleteSession = useCallback(async (s) => {
    if (!s || !s.id) return;
    const gen = ++genRef.current;
    if (activeId === s.id) setActiveId(null);
    // Optimistic remove from the list so the sidebar updates instantly.
    setSessions((prev) => prev.filter((x) => x.id !== s.id));
    try {
      await base44.entities.ChatMessage.deleteMany({ session_id: s.id });
    } catch { /* messages may already be gone */ }
    try {
      await base44.entities.ChatSession.delete(s.id);
    } catch { /* session may already be deleted */ }
    if (gen !== genRef.current) return;
  }, [activeId]);

  // --- Toggle starred ---
  const starSession = useCallback(async (s) => {
    if (!s || !s.id) return;
    const next = !s.starred;
    // Optimistic update
    setSessions((prev) => prev.map((x) => (x.id === s.id ? { ...x, starred: next } : x)));
    try {
      await base44.entities.ChatSession.update(s.id, { starred: next });
    } catch { /* best-effort */ }
  }, []);

  // --- Rename a session ---
  const renameSession = useCallback(async (id, title) => {
    if (!id || !title) return;
    setSessions((prev) => prev.map((x) => (x.id === id ? { ...x, title } : x)));
    try {
      await base44.entities.ChatSession.update(id, { title });
    } catch { /* best-effort */ }
  }, []);

  // --- Add a freshly-created session to the top of the list (used by
  //     Chat.jsx after creating a session for the first message). ---
  const prependSession = useCallback((session) => {
    if (!session || !session.id) return;
    setSessions((prev) => {
      // Dedupe — never insert twice
      if (prev.some((x) => x.id === session.id)) return prev;
      return [session, ...prev];
    });
  }, []);

  // --- Update the last_message_at on a session and bubble it to top ---
  const touchSession = useCallback((id) => {
    if (!id) return;
    const now = new Date().toISOString();
    setSessions((prev) => {
      const s = prev.find((x) => x.id === id);
      if (!s) return prev;
      const updated = { ...s, last_message_at: now };
      return [updated, ...prev.filter((x) => x.id !== id)];
    });
  }, []);

  const value = {
    sessions,
    activeId,
    pendingProject,
    pendingProjectId,
    loading,
    setActiveId,
    setPendingProject,
    selectSession,
    newChat,
    deleteSession,
    starSession,
    renameSession,
    refreshSessions,
    getSession,
    adoptSessionProject,
    prependSession,
    touchSession,
    chatGeneration,
  };

  return <ChatSessionContext.Provider value={value}>{children}</ChatSessionContext.Provider>;
}

export function useChatSession() {
  const ctx = useContext(ChatSessionContext);
  if (!ctx) {
    // During tests or when the provider is absent, return a no-op default
    // so the consumer doesn't crash. Production code should always be
    // wrapped in ChatSessionProvider (mounted in AppLayout).
    return {
      sessions: [],
      activeId: null,
      pendingProject: null,
      pendingProjectId: null,
      loading: false,
      setActiveId: () => {},
      setPendingProject: () => {},
      selectSession: () => {},
      newChat: () => {},
      deleteSession: () => Promise.resolve(),
      starSession: () => Promise.resolve(),
      renameSession: () => Promise.resolve(),
      refreshSessions: () => Promise.resolve(),
      getSession: () => null,
      adoptSessionProject: () => {},
      prependSession: () => {},
      touchSession: () => {},
      chatGeneration: 0,
    };
  }
  return ctx;
}
