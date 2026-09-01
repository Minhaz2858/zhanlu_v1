import { useState, useMemo, useRef, useEffect } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { createPortal } from 'react-dom';
import { Plus, Star, Trash2, Pencil, Check, X, MessageSquare, Folder, ChevronRight, ChevronDown, FolderPlus, MoreHorizontal, Clock, LayoutDashboard, Loader2, Search, Download, Link2 } from 'lucide-react';
import { toast } from 'sonner';
import { useLanguage } from '@/lib/LanguageProvider';
import { base44 } from '@/api/base44Client';
import { chatSearch, chatShare } from '@/api/agentEnhanced';
import { downloadConversationMarkdown } from '@/lib/exportConversation';
import { formatRelativeTime } from '@/lib/time';
import { useChatSession } from '@/lib/ChatSessionContext';
import { usePersistentStream } from '@/lib/PersistentStreamContext';

// Per-group session display caps.  Keeps the sidebar scannable
// when Ungrouped accumulates hundreds of conversations — the user
// can still see and click into every project, with the header
// showing the real total count.
const SESSION_DISPLAY_LIMIT_UNGROUPED = 10;
const SESSION_DISPLAY_LIMIT_PROJECT = 5;

function normalizeGroupKey(raw, t) {
  const v = (raw || '').trim();
  if (!v) return t.sessionList.ungrouped;
  // treat literal "未分组" or "Ungrouped" as the i18n key so duplicates merge
  if (v === '未分组' || v === 'Ungrouped') return t.sessionList.ungrouped;
  return v;
}

/**
 * SessionList — presentational view of the chat session list, backed
 * by `useChatSession()`. Lives inside the unified sidebar.
 *
 * This component is now route-aware only at the navigation level:
 * it never receives session state via props. The unified sidebar in
 * `Sidebar.jsx` renders it on EVERY route (chat history must be
 * reachable at all times), so selecting a session / starting a new
 * chat from a non-chat route also navigates to `/` — the
 * ChatSessionProvider lives at the AppLayout level, so `activeId`
 * survives the route change and Chat.jsx's [activeId] effect loads
 * the messages on mount.
 *
 * The `panelCollapsed` state (the "show / hide" inside the sidebar)
 * used to live here; we still own it locally because it's a visual
 * concern of THIS panel. The top-level sidebar collapse is a separate
 * concern owned by `Sidebar.jsx`.
 */
export default function SessionList() {
  const {
    sessions,
    activeId,
    pendingProject,
    pendingProjectId,
    setPendingProject,
    selectSession,
    newChat,
    deleteSession,
    starSession,
    renameSession,
  } = useChatSession();
  const { t, lang } = useLanguage();
  const location = useLocation();
  const navigate = useNavigate();
  // Per-session run status registry — drives the "running silently" spinner
  // so the user can see which OTHER sessions are busy while focused elsewhere.
  const { runStatuses } = usePersistentStream();
  const [editingId, setEditingId] = useState(null);
  const [editValue, setEditValue] = useState('');
  // The dropdown and "+" button reflect the current project context.
  // pendingProject is owned by ChatSessionContext (the source of truth
  // for where the next new chat will be created). We fall back to
  // "Ungrouped" when there's no pending project yet (fresh page load).
  const currentProject = pendingProject || t.sessionList.ungrouped;
  const [collapsed, setCollapsed] = useState({});
  // Groups the user has manually expanded to show ALL sessions
  // (bypassing the per-group display cap). Keyed by project name.
  const [expandedGroups, setExpandedGroups] = useState({});
  const [extraProjects, setExtraProjects] = useState([]);
  // Look-up table from project name → project id, so the user can
  // switch projects in the dropdown and we hand BOTH the name and the
  // FK id back up to the context.
  const [projectIdsByName, setProjectIdsByName] = useState({});
  const [menuOpen, setMenuOpen] = useState(false);
  const [menuFor, setMenuFor] = useState(null);
  // Fixed-position menu placement (px) for the portaled session menu —
  // computed from the trigger button's rect so the menu escapes the
  // sidebar's overflow clipping and still lands right under the row.
  const [menuPos, setMenuPos] = useState(null);
  const menuRef = useRef(null);
  // Global chat-history search (Kimi/GPT-style): the input lives above
  // the session groups; while a query is active the groups are replaced
  // by results from GET /api/chat/search (user-scoped, message ILIKE).
  const [searchQuery, setSearchQuery] = useState('');
  const [searchResults, setSearchResults] = useState(null);
  const [searchBusy, setSearchBusy] = useState(false);
  // Set of ChatSession.id values that have created at least one
  // AutomationTask. Used to render a small clock icon in place of
  // the default MessageSquare on those sessions so users can
  // easily find the "control room" for a scheduled automation.
  // We pull the full list once on mount (it is bounded — 200 rows
  // is the same cap used everywhere else) and re-fetch whenever
  // the active session changes, because a session is most likely
  // to acquire its first automation while it's the active one.
  const [sessionIdsWithAutomation, setSessionIdsWithAutomation] = useState(() => new Set());
  // Set of AgentConversation ids whose metadata.mode === 'dashboard'
  // (dashboard-dedicated sessions). Rendered with a LayoutDashboard icon
  // instead of the default MessageSquare so users can instantly find every
  // chat that builds/edits a dashboard. Same fetch pattern as automation.
  const [dashboardConversationIds, setDashboardConversationIds] = useState(() => new Set());
  const rootRef = useRef(null);

  // Fetch existing projects from the backend on mount so previously-
  // created projects appear in the dropdown even before any session
  // is assigned to them.  Without this, extraProjects starts empty
  // on every page load and only projects with sessions show up.
  useEffect(() => {
    let cancelled = false;
    base44.entities.Project.list('-updated_date', 200)
      .then((list) => {
        if (cancelled) return;
        const names = (list || [])
          .filter((p) => p.status !== 'archived')
          .map((p) => p.name)
          .filter(Boolean);
        // Also build a name → id map for the project switcher. The
        // new ChatSession/AgentConversation records need the FK id
        // (not just the name) so they show up in the project's
        // "Recent Chats" tab.
        const idMap = {};
        for (const p of (list || [])) {
          if (p && p.id && p.name) idMap[p.name] = p.id;
        }
        setProjectIdsByName(idMap);
        setExtraProjects((prev) => {
          const merged = new Set([...prev, ...names]);
          return Array.from(merged);
        });
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, []);

  // Pull every AutomationTask once on mount and again whenever the
  // active session changes (a new automation is most often created
  // from the session that's currently open). Build a Set of
  // session_ids so the per-row render below can do an O(1) lookup
  // instead of scanning the full automation list for every session.
  // We only need `session_id` from each row, so we ask the backend
  // for the small projection shape (it's still typed as the full
  // entity by the base44 client, but in practice the JSON body
  // returned is bounded by the entity schema).
  useEffect(() => {
    let cancelled = false;
    function refresh() {
      base44.entities.AutomationTask.list('-updated_date', 500)
        .then((list) => {
          if (cancelled) return;
          const ids = new Set();
          for (const a of (list || [])) {
            if (a && a.session_id) ids.add(a.session_id);
          }
          setSessionIdsWithAutomation(ids);
        })
        .catch(() => {
          if (cancelled) return;
          // Best-effort: on failure keep the previous set so the
          // badge doesn't blink off briefly.
        });
    }
    refresh();
    // Also re-refresh when Chat.jsx notifies us it just created a new
    // automation. Without this, the Clock badge only appears after a
    // page navigation — the user has to leave and come back to see it.
    const onAutomationCreated = () => refresh();
    window.addEventListener('zhanlu:automation-created', onAutomationCreated);
    return () => {
      cancelled = true;
      window.removeEventListener('zhanlu:automation-created', onAutomationCreated);
    };
    // Re-fetch when the active session changes — a session is most
    // likely to acquire its first automation while it is open.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId]);

  // Fetch dashboard-mode conversations (metadata.mode === 'dashboard').
  // The sidebar's ChatSession rows carry only ``conversation_id``; the
  // dashboard stamp lives on the linked AgentConversation.metadata, so we
  // build a Set of conv ids here and look up per-row below — same pattern
  // as the automation set above. Refetch when the active session changes
  // (a dashboard is most often created from the session that's open) and
  // when a new dashboard artifact arrives (zhanlu:dashboard-created).
  useEffect(() => {
    let cancelled = false;
    function refresh() {
      base44.entities.AgentConversation.list()
        .then((list) => {
          if (cancelled) return;
          const ids = new Set();
          for (const c of (list || [])) {
            const meta = c.metadata || c.metadata_ || {};
            if (meta.mode === 'dashboard') ids.add(c.id);
          }
          setDashboardConversationIds(ids);
        })
        .catch(() => { /* best-effort — keep previous set */ });
    }
    refresh();
    const onDashboardCreated = () => refresh();
    window.addEventListener('zhanlu:dashboard-created', onDashboardCreated);
    return () => {
      cancelled = true;
      window.removeEventListener('zhanlu:dashboard-created', onDashboardCreated);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId]);

  // All groups default to collapsed (including the current project), so
  // the sidebar shows a clean, scannable header list instead of a wall of
  // sessions. The user can expand any group manually via its header —
  // that manual state is kept until the group set actually changes.
  useEffect(() => {
    if (!currentProject) return;
    // Build a "collapsed" map where every known group is collapsed.
    // Done inside this effect to keep the closure over `groups` accurate.
    setCollapsed((prev) => {
      // groups is defined below via useMemo; recompute the same key set
      // here without depending on the memo (to avoid an init-order
      // double-render). We approximate with the keys we know about.
      // Simpler: build the same map the memo would build.
      const map = {};
      sessions.forEach((s) => {
        const key = normalizeGroupKey(s.project, t);
        (map[key] = map[key] || []).push(s);
      });
      extraProjects.forEach((p) => {
        const key = normalizeGroupKey(p, t);
        if (!map[key]) map[key] = [];
      });
      if (!map[currentProject]) map[currentProject] = [];
      const keys = Object.keys(map);
      const next = {};
      keys.forEach((k) => {
        next[k] = true;
      });
      // Only update state if something actually changed to avoid
      // pointless re-renders.
      const changed = keys.some((k) => {
        const wasCollapsed = !!prev[k];
        const shouldBeCollapsed = true;
        return wasCollapsed !== shouldBeCollapsed;
      });
      return changed ? next : prev;
    });
  }, [currentProject, sessions, extraProjects, t]);

  const groups = useMemo(() => {
    // Build the set of project labels the user OWNS as active projects.
    // Only these labels are promoted to sidebar group headers — a session
    // with any other `project` label falls into UNGROUPED.
    //
    // Why gate on `extraProjects` (user-owned active projects) rather
    // than on every label seen on `sessions`? Two reasons:
    //   1. Consistency with My Space → Projects, which is already
    //      filtered by ownership (the `Project` entity is in
    //      USER_SCOPED_ENTITIES). A user with 0 owned active projects
    //      sees 0 project groups here too — not a confusing "ENTERPRISE"
    //      / "TEST" header that looks like a project record but
    //      actually contains the user's own chats under a text label.
    //   2. The previous behaviour surfaced group headers for projects
    //      owned by OTHER users (when the user's chat happened to be
    //      labelled with someone else's project name). That looked
    //      like access leakage even though the chats themselves were
    //      the user's.
    //
    // Sessions that the user has explicitly "current"-ed (e.g. via the
    // dropdown) still get a header via the `if (!map[currentProject])`
    // fall-through below, so the active project's chats are always
    // reachable.
    const owned = new Set(
      (extraProjects || []).map((p) => normalizeGroupKey(p, t))
    );
    const ungroupedKey = t.sessionList.ungrouped;
    const map = {};
    sessions.forEach((s) => {
      const key = normalizeGroupKey(s.project, t);
      const bucket = owned.has(key) ? key : ungroupedKey;
      (map[bucket] = map[bucket] || []).push(s);
    });
    // Add empty headers for owned active projects with no sessions yet
    // (so the user can still see them in the sidebar to create new
    // sessions in).
    (extraProjects || []).forEach((p) => {
      const key = normalizeGroupKey(p, t);
      if (!map[key]) map[key] = [];
    });
    // The user's currently-selected project should also be reachable
    // even if it's not in the owned set (e.g. a project they used
    // to own and are revisiting, or a soft-deleted one). Header only;
    // no sessions get re-bucketed here — those are already in
    // ungrouped above.
    if (currentProject && !map[currentProject]) map[currentProject] = [];
    const keys = Object.keys(map).sort((a, b) => {
      if (a === t.sessionList.ungrouped) return -1;
      if (b === t.sessionList.ungrouped) return 1;
      return a.localeCompare(b);
    });
    // Cap the visible chats per group so the sidebar stays scannable:
    //   • Ungrouped — 10 most-recent (it can have hundreds of entries)
    //   • Project — 5 most-recent per project
    // The total count is preserved on `g.total` so the group header
    // still shows the real number ("93", "5", etc.).  Groups the user
    // has manually expanded (via "See more") show ALL sessions.
    return keys.map((k) => {
      const all = map[k];
      const cap = k === t.sessionList.ungrouped
        ? SESSION_DISPLAY_LIMIT_UNGROUPED
        : SESSION_DISPLAY_LIMIT_PROJECT;
      const isExpanded = !!expandedGroups[k];
      return {
        project: k,
        items: isExpanded ? all : all.slice(0, cap),
        total: all.length,
        hidden: Math.max(0, all.length - cap),
        expanded: isExpanded,
      };
    });
  }, [sessions, extraProjects, currentProject, expandedGroups, t]);

  // click outside → close dropdown + context menu
  useEffect(() => {
    if (!menuOpen && menuFor === null) return;
    function onDown(e) {
      // The context menu is portaled to <body>, so it is NOT inside
      // rootRef — clicks on it must not count as "outside".
      if (menuRef.current && menuRef.current.contains(e.target)) return;
      if (rootRef.current && !rootRef.current.contains(e.target)) {
        setMenuOpen(false);
        setMenuFor(null);
      }
    }
    function onEsc(e) {
      if (e.key === 'Escape') {
        setMenuOpen(false);
        setMenuFor(null);
      }
    }
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onEsc);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onEsc);
    };
  }, [menuOpen, menuFor]);

  function startEdit(s) { setEditingId(s.id); setEditValue(s.title); }
  function commitEdit() { if (editValue.trim()) renameSession(editingId, editValue.trim()); setEditingId(null); }
  function toggleCollapse(p) { setCollapsed((prev) => ({ ...prev, [p]: !prev[p] })); }
  // The list renders on every route now, but the context actions only
  // update in-memory state — Chat.jsx (which reacts to activeId) is
  // not mounted on e.g. /toolkit. Route to `/` so the selection /
  // new chat becomes visible. Skip the navigation when already on a
  // chat route to avoid pushing a duplicate history entry.
  function ensureChatRoute() {
    if (location.pathname !== '/' && location.pathname !== '/chat') {
      navigate('/');
    }
  }
  function handleSelectSession(id) {
    selectSession(id);
    ensureChatRoute();
  }
  function handleNewChatInGroup(project) {
    newChat(project);
    ensureChatRoute();
  }
  // Debounced chat-history search. Empty/whitespace query clears results;
  // otherwise fetch user-scoped matches and replace the group list.
  useEffect(() => {
    const q = searchQuery.trim();
    if (!q) {
      setSearchResults(null);
      setSearchBusy(false);
      return;
    }
    setSearchBusy(true);
    const timer = setTimeout(async () => {
      try {
        const body = await chatSearch(q, 20);
        setSearchResults(body.results || []);
      } catch {
        setSearchResults([]);
      } finally {
        setSearchBusy(false);
      }
    }, 250);
    return () => clearTimeout(timer);
  }, [searchQuery]);

  function handleSearchSelect(id) {
    setSearchQuery('');
    setSearchResults(null);
    handleSelectSession(id);
  }

  // Kimi/GPT-style conversation export: fetch the session messages and
  // download a client-side .md file. Best-effort — a failure never
  // blocks the menu.
  async function handleExportSession(s) {
    try {
      const msgs = await base44.entities.ChatMessage.filter({ session_id: s.id }, 'created_date', 200);
      downloadConversationMarkdown(s, msgs);
    } catch {
      /* export is best-effort — never block the menu */
    }
    setMenuFor(null);
  }

  // Kimi/GPT-style conversation share: create (or reuse) the public
  // token and copy the /share/c/<token> URL to the clipboard.
  async function handleShareSession(s) {
    try {
      const { share_url } = await chatShare(s.id);
      const url = `${window.location.origin}${share_url}`;
      try {
        await navigator.clipboard.writeText(url);
      } catch {
        /* clipboard can be denied — the toast still shows the URL */
      }
      toast.success(t.sessionList.shareCopied, { description: url });
    } catch {
      toast.error(t.sessionList.shareFailed);
    }
    setMenuFor(null);
  }

  async function handleNewProject() {
    const name = window.prompt(t.sessionList.newProject);
    if (name && name.trim()) {
      const n = name.trim();
      setExtraProjects((prev) => (prev.includes(n) ? prev : [...prev, n]));
      // Newly created project — try to read its id back from the
      // create response so the new ChatSession can FK to it.
      setPendingProject(n, null);
      try {
        const created = await base44.entities.Project.create({ name: n });
        if (created && created.id) {
          setProjectIdsByName((prev) => ({ ...prev, [n]: created.id }));
          setPendingProject(n, created.id);
        }
      } catch { /* noop */ }
    }
    setMenuOpen(false);
  }

  return (
    <div ref={rootRef} className="flex h-full w-full min-w-0 flex-col">
      <div className="space-y-2 border-b border-sidebar-border px-3 py-3">
        {/* The "+ New Task" button was previously rendered here, but
            it duplicated the one at the top of the unified sidebar.
            The top-level button is the primary CTA and is highlighted
            when the user is on `/`, so the chat-section sidebar only
            shows the project dropdown + session list below. */}
        <div className="relative">
          {/* Project dropdown — no folder icon, just the project name. */}
          <button onClick={() => setMenuOpen(!menuOpen)} className="inline-flex w-full items-center gap-1.5 rounded-md border border-border bg-card px-2.5 py-1.5 text-xs text-foreground transition-colors hover:bg-secondary">
            <span className="flex-1 truncate text-left">{currentProject}</span>
            <ChevronDown className="h-3 w-3 text-muted-foreground" />
          </button>
          {menuOpen && (
            <div onMouseDown={(e) => e.stopPropagation()} className="absolute left-0 right-7 top-full z-20 mt-1 rounded-md border border-border bg-popover py-1 shadow-md">
              {groups.map((g) => (
                <button key={g.project} onClick={() => { setPendingProject(g.project, projectIdsByName[g.project] || null); setMenuOpen(false); }} className={`flex w-full min-w-0 items-center gap-1.5 px-3 py-1.5 text-xs transition-colors hover:bg-secondary ${currentProject === g.project ? 'text-primary' : 'text-foreground'}`}>
                  <Folder className="h-3 w-3 shrink-0" />
                  {/* Project name only — the session count was removed
                      here too to match the sidebar group headers. */}
                  <span className="min-w-0 flex-1 truncate text-left">{g.project}</span>
                </button>
              ))}
              <div className="my-1 border-t border-border" />
              <button onClick={handleNewProject} className="flex w-full items-center gap-1.5 px-3 py-1.5 text-xs text-primary hover:bg-secondary">
                <FolderPlus className="h-3 w-3" /> {t.sessionList.newProject}
              </button>
            </div>
          )}
        </div>
        {/* Global chat-history search box (Kimi/GPT-style). Results
            replace the session groups while a query is active. */}
        <div className="relative">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground/60" />
          <input
            data-testid="session-search-input"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder={t.sessionList.searchPlaceholder}
            className="w-full rounded-md border border-border bg-card py-1.5 pl-8 pr-7 text-xs text-foreground placeholder:text-muted-foreground/60 focus:border-primary/40 focus:outline-none"
          />
          {searchQuery && (
            <button
              data-testid="session-search-clear"
              onClick={() => setSearchQuery('')}
              title={t.sessionList.searchClear}
              className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-0.5 text-muted-foreground/60 hover:text-foreground"
            >
              <X className="h-3 w-3" />
            </button>
          )}
        </div>
      </div>
      <div className="flex-1 overflow-y-auto px-2 py-2">
        {searchQuery.trim() ? (
          <div data-testid="session-search-results" className="space-y-0.5">
            {searchBusy && (
              <p className="px-3 py-4 text-center text-xs text-muted-foreground/60">
                <Loader2 className="mx-auto h-3.5 w-3.5 animate-spin" />
              </p>
            )}
            {!searchBusy && searchResults && searchResults.length === 0 && (
              <p className="px-3 py-6 text-center text-xs text-muted-foreground">{t.sessionList.searchEmpty}</p>
            )}
            {!searchBusy &&
              (searchResults || []).map((r) => (
                <div
                  key={r.session_id}
                  data-testid="session-search-result"
                  onClick={() => handleSearchSelect(r.session_id)}
                  className="group cursor-pointer rounded-md border-l-2 border-transparent px-2.5 py-2 transition-colors hover:bg-sidebar-accent/40"
                >
                  <div className="flex items-center gap-1.5">
                    <MessageSquare className="h-3 w-3 shrink-0 text-muted-foreground/50" />
                    <span className="min-w-0 flex-1 truncate text-xs text-foreground">{r.title}</span>
                    {r.agent_name && (
                      <span className="shrink-0 rounded bg-secondary px-1 py-0.5 text-[10px] text-muted-foreground">{r.agent_name}</span>
                    )}
                  </div>
                  <p className="mt-1 truncate pl-[18px] text-[11px] text-muted-foreground/70">{r.matches?.[0]?.snippet || ''}</p>
                </div>
              ))}
          </div>
        ) : (
          <></>
        )}
        {!searchQuery.trim() && sessions.length === 0 && extraProjects.length === 0 && (
          <p className="px-3 py-6 text-center text-xs text-muted-foreground">{t.sessionList.noSessions}</p>
        )}
        {!searchQuery.trim() && groups.map((g) => (
          <div key={g.project} className="mb-1">
            <div className="group/project flex w-full items-center px-2 pt-3 pb-1">
              <button onClick={() => toggleCollapse(g.project)} className="flex flex-1 items-center gap-1 text-[11px] font-semibold uppercase tracking-wider text-foreground/80 transition-colors hover:text-foreground">
                <ChevronRight className={`h-3 w-3 transition-transform ${collapsed[g.project] ? '' : 'rotate-90'}`} />
                {/* Project name. The per-group session count used to be
                    rendered here ("UNGROUPED 62" / "TEST 0") — removed:
                    the at-a-glance signal was adding visual noise without
                    telling the user anything they couldn't see by
                    expanding the group. The "See more (+N)" affordance
                    below still surfaces the hidden count when relevant. */}
                <span className="flex-1 truncate text-left">{g.project}</span>
              </button>
              <button
                onClick={() => handleNewChatInGroup(g.project)}
                title={`${t.sessionList.newTask} — ${g.project}`}
                className="rounded p-0.5 text-muted-foreground/30 opacity-0 transition-opacity group-hover/project:opacity-100 hover:bg-sidebar-accent/50 hover:text-foreground"
              >
                <Plus className="h-3 w-3" />
              </button>
            </div>
            {!collapsed[g.project] && (
              <div className="ml-3 space-y-0.5">
                {g.total === 0 ? (
                  <p className="px-3 py-2 text-[11px] text-muted-foreground/60">{t.sessionList.noProjectTasks}</p>
                ) : (
                  g.items.map((s) => (
                    <div key={s.id} onClick={() => handleSelectSession(s.id)} className={`group relative cursor-pointer rounded-md border-l-2 px-2.5 py-2 transition-colors ${activeId === s.id ? 'border-primary bg-card shadow-sm' : 'border-transparent hover:bg-sidebar-accent/40'}`}>
                      {editingId === s.id ? (
                        <div className="flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
                          <input value={editValue} onChange={(e) => setEditValue(e.target.value)} className="flex-1 rounded border border-border bg-background px-2 py-0.5 text-sm focus:outline-none" autoFocus onKeyDown={(e) => { if (e.key === 'Enter') commitEdit(); if (e.key === 'Escape') setEditingId(null); }} />
                          <button onClick={commitEdit}><Check className="h-3.5 w-3.5" /></button>
                          <button onClick={() => setEditingId(null)}><X className="h-3.5 w-3.5" /></button>
                        </div>
                      ) : (
                        <>
                          <div className="flex items-start gap-1.5">
                            {/*
                              Row icon. For sessions that have created
                              at least one AutomationTask, swap the
                              default MessageSquare for a Clock icon
                              in the primary color so users can
                              visually identify the "control room"
                              for a scheduled automation. A small
                              amber dot is added for extra scannability
                              in the dense sidebar list. Both the
                              icon and the dot share the same
                              `data-has-automation` attribute so
                              tests and future styling can target
                              automation rows precisely.
                            */}
                            {/* Session icon. Dashboard-dedicated sessions
                                (conversation metadata.mode === 'dashboard')
                                show a LayoutDashboard icon so the user can
                                instantly find every chat that builds/edits a
                                dashboard — the same pattern as the Clock for
                                automations below. Sessions with an automation
                                keep the Clock. The `data-has-dashboard` /
                                `data-has-automation` attrs let tests and
                                future styling target the rows precisely. */}
                            {(s.metadata && s.metadata.mode === 'dashboard') || dashboardConversationIds.has(s.conversation_id) ? (
                              <LayoutDashboard
                                className="mt-0.5 h-3 w-3 shrink-0 text-primary"
                                aria-label={t.sessionList.dashboardBadge}
                                title={t.sessionList.dashboardBadge}
                                data-has-dashboard="true"
                              />
                            ) : sessionIdsWithAutomation.has(s.id) ? (
                              <Clock
                                className="mt-0.5 h-3 w-3 shrink-0 text-primary"
                                aria-label={t.sessionList.automationBadge}
                                title={t.sessionList.automationBadge}
                                data-has-automation="true"
                              />
                            ) : (
                              <MessageSquare
                                className="mt-0.5 h-3 w-3 shrink-0 text-muted-foreground"
                                data-has-automation="false"
                              />
                            )}
                            {/* "Running silently" indicator (2026-08-31): a session
                                with a live run (streaming agent reply OR a
                                background automation) shows a small spinner here,
                                so the user can see OTHER sessions are busy while
                                focused elsewhere — without a popup or split pane.
                                `error` leaves a red dot until the session is
                                opened; `done` is a brief flash handled by the
                                registry's 2s timeout. */}
                            {runStatuses[s.id] === 'running' && (
                              <Loader2
                                className="mt-0.5 h-3 w-3 shrink-0 animate-spin text-primary"
                                aria-label={lang === 'en' ? 'Running' : '运行中'}
                                title={lang === 'en' ? 'Running…' : '运行中…'}
                                data-run-status="running"
                              />
                            )}
                            {runStatuses[s.id] === 'queued' && (
                              <Loader2
                                className="mt-0.5 h-3 w-3 shrink-0 animate-spin text-muted-foreground"
                                aria-label={lang === 'en' ? 'Queued' : '排队中'}
                                title={lang === 'en' ? 'Queued (waiting for a free slot)' : '排队中（等待空闲槽位）'}
                                data-run-status="queued"
                              />
                            )}
                            {runStatuses[s.id] === 'error' && (
                              <span
                                className="mt-1.5 h-2 w-2 shrink-0 rounded-full bg-destructive"
                                aria-label={lang === 'en' ? 'Run failed' : '运行失败'}
                                title={lang === 'en' ? 'Run failed' : '运行失败'}
                                data-run-status="error"
                              />
                            )}
                            {/* Unread dot — set by automation runs so the
                                user knows a background run produced new
                                content. Cleared via selectSession (mark-read). */}
                            {s.unread && (
                              <span
                                className="mt-1.5 h-2 w-2 shrink-0 rounded-full bg-blue-500"
                                aria-label={lang === 'en' ? 'Unread' : '未读'}
                                title={lang === 'en' ? 'Unread' : '未读'}
                                data-unread="true"
                              />
                            )}
                            <span className="flex-1 truncate text-sm text-foreground" title={s.title}>{s.title}</span>
                            {/* Right-side metadata — fades out on hover so the
                                action button (rendered absolutely below) can
                                occupy the same visual slot without overlap. */}
                            {(s.updated_date || s.last_message_at) && (
                              <span
                                className="mt-0.5 shrink-0 text-[10px] text-muted-foreground/60 transition-opacity duration-150 group-hover:opacity-0"
                                title={s.updated_date || s.last_message_at}
                              >
                                {formatRelativeTime(s.updated_date || s.last_message_at, lang)}
                              </span>
                            )}
                            {s.starred && <Star className="h-2.5 w-2.5 shrink-0 fill-primary text-primary transition-opacity duration-150 group-hover:opacity-0" />}
                          </div>
                          {/* Action button — positioned in the same top-right
                              slot that the timestamp occupies. On hover the
                              timestamp/star above fade out (group-hover:opacity-0)
                              and this button fades in, so they never visually
                              collide. */}
                          <div className="absolute right-1 top-1 flex items-center opacity-0 transition-opacity duration-150 group-hover:opacity-100" onClick={(e) => e.stopPropagation()}>
                            <button onClick={(e) => {
                              if (menuFor === s.id) { setMenuFor(null); return; }
                              const r = e.currentTarget.getBoundingClientRect();
                              const MENU_H = 160; // ~5 rows of 28px + padding
                              const right = Math.max(8, window.innerWidth - r.right);
                              let top = r.bottom + 4;
                              // Flip upward when the menu would overflow the viewport.
                              if (top + MENU_H > window.innerHeight - 8) top = Math.max(8, r.top - MENU_H - 4);
                              setMenuPos({ top, right });
                              setMenuFor(s.id);
                            }} className="rounded p-1 text-muted-foreground hover:bg-secondary hover:text-foreground">
                              <MoreHorizontal className="h-3.5 w-3.5" />
                            </button>
                            {menuFor === s.id && menuPos && createPortal(
                              <div
                                ref={menuRef}
                                className="fixed z-[60] w-32 rounded-md border border-border bg-popover py-1 shadow-md"
                                style={{ top: menuPos.top, right: menuPos.right }}
                              >
                                <button onClick={() => { starSession(s); setMenuFor(null); }} className="flex w-full items-center gap-2 px-3 py-1.5 text-xs text-foreground hover:bg-secondary">
                                  <Star className="h-3 w-3" /> {s.starred ? t.sessionList.unpin : t.sessionList.pin}
                                </button>
                                <button onClick={() => { startEdit(s); setMenuFor(null); }} className="flex w-full items-center gap-2 px-3 py-1.5 text-xs text-foreground hover:bg-secondary">
                                  <Pencil className="h-3 w-3" /> {t.sessionList.rename}
                                </button>
                                <button onClick={() => { handleExportSession(s); setMenuFor(null); }} className="flex w-full items-center gap-2 px-3 py-1.5 text-xs text-foreground hover:bg-secondary">
                                  <Download className="h-3 w-3" /> {t.sessionList.export}
                                </button>
                                <button onClick={() => { handleShareSession(s); }} className="flex w-full items-center gap-2 px-3 py-1.5 text-xs text-foreground hover:bg-secondary">
                                  <Link2 className="h-3 w-3" /> {t.sessionList.share}
                                </button>
                                <button onClick={() => { deleteSession(s); setMenuFor(null); }} className="flex w-full items-center gap-2 px-3 py-1.5 text-xs text-destructive hover:bg-secondary">
                                  <Trash2 className="h-3 w-3" /> {t.sessionList.delete}
                                </button>
                              </div>,
                              document.body
                            )}
                          </div>
                        </>
                      )}
                    </div>
                  ))
                )}
                {g.hidden > 0 && (
                  <button
                    type="button"
                    onClick={(e) => { e.stopPropagation(); setExpandedGroups((prev) => ({ ...prev, [g.project]: !prev[g.project] })); }}
                    className="mx-2.5 mt-1 mb-2 inline-flex items-center gap-1 rounded-md px-2 py-1 text-left text-[10px] font-medium text-primary transition-colors hover:bg-primary/10"
                    title={g.expanded ? 'Collapse to fewer items' : `Show all ${g.total} sessions in this group`}
                  >
                    {g.expanded ? (
                      <>Show less</>
                    ) : (
                      <>See more (+{g.hidden})</>
                    )}
                  </button>
                )}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
