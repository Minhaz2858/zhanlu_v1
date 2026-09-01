import { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { base44 } from '@/api/base44Client';
import { appParams } from '@/lib/app-params';
import { authFetch } from '@/api/authFetch';
import { useLanguage } from '@/lib/LanguageProvider';
import { useAuth } from '@/lib/AuthContext';
import { useTranslate } from '@/lib/useTranslate';
import { useChatSession } from '@/lib/ChatSessionContext';
import { formatRelativeTime } from '@/lib/time';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Badge } from '@/components/ui/badge';
import AddAgentToProjectDialog from '@/components/project/AddAgentToProjectDialog';
import AddKbToProjectDialog from '@/components/project/AddKbToProjectDialog';
import LlmModelSelector from '@/components/chat/LlmModelSelector';
import CreateResourceDialog from '@/components/CreateResourceDialog';
import ResourceAccessDialog from '@/components/ResourceAccessDialog';
import ResourceAccessPolicyDialog from '@/components/ResourceAccessPolicyDialog';
import { toast } from '@/components/ui/use-toast';
import {
  ArrowLeft, Bot, Database, MessageSquare, Cog,
  Sparkles, Check, X, Trash2, Plus, Loader2, Folder,
  ArrowUpRight, Zap, Settings2, ListChecks,
  Pencil, Save, MoreVertical, FileStack, Layers,
  Clock, Calendar, ChevronRight, Activity, AlertCircle, Users, Cpu, Lock, ShieldCheck,
  Brain, Pin, Star,
} from 'lucide-react';
import {
  Sheet, SheetContent, SheetHeader, SheetTitle, SheetDescription,
} from '@/components/ui/sheet';

// Color presets removed: the user asked for a simple, color-free
// project detail view. The ``color`` field on the Project model is
// still present in the backend (for backward compatibility) but the
// edit form no longer surfaces a picker or sends ``color`` to the
// API. The project icon now uses neutral colors.

// Sidebar items. `scrollable: true` items are scroll targets in the main
// column (Agents, Recent Chats). `scrollable: false` items are quick
// actions that open a side panel — e.g. Resources is hidden from the
// main flow because everyday users don't need to manage data sources
// in their face; it's one click away via the sidebar.
const SECTIONS = [
  { key: 'agents', icon: Bot, scrollable: true },
  { key: 'resources', icon: Layers, scrollable: false },
  { key: 'chats', icon: MessageSquare, scrollable: true },
  { key: 'memory', icon: Brain, scrollable: true },
  { key: 'access', icon: Users, scrollable: false, action: 'access' },
  { key: 'policy', icon: ShieldCheck, scrollable: false, action: 'policy' },
];

/**
 * ProjectDetail — `/my-space/project/:id`
 *
 * Sections shown:
 *   • Agents        — AgentApp rows that are members of this project
 *                     (via ProjectAgent many-to-many, with legacy
 *                     `project_id` fallback). A "Manage Agents" modal
 *                     lets the user add or remove members.
 *   • Resources     — KnowledgeBase rows bound to this project. Hidden
 *                     from the main flow because everyday users don't
 *                     manage data sources on a per-project basis; the
 *                     count is still surfaced in the sidebar and the
 *                     full list is one click away in a slide-out sheet.
 *   • Recent Chats  — AgentConversation rows where project_id matches.
 *                     Only AgentConversation (not ChatSession) is
 *                     surfaced here, per explicit product decision.
 */
export default function ProjectDetail() {
  const { t, lang } = useLanguage();
  const { isAdmin, user } = useAuth();
  const { id: projectId } = useParams();
  const navigate = useNavigate();

  // `newChat` is the ChatSessionContext's "drop the current active
  // session so the user lands on a fresh empty chat" function. We
  // call it inside `chatWithAgent` so clicking an agent in the
  // project page does NOT resume whatever chat the user was last
  // looking at — it always starts a brand-new conversation with
  // that agent, bound to this project. (Previously the agent click
  // only set URL params; Chat.jsx's URL handler didn't touch
  // activeId, so users were dropped back into the most recent
  // session and saw an unrelated "hi" conversation even though the
  // agent chip was correctly pre-selected.)
  const { newChat } = useChatSession();
  const isEn = lang === 'en';
  const T = (zh, en) => (isEn ? en : zh);

  // ── Project record ──
  const [project, setProject] = useState(null);
  const [loading, setLoading] = useState(true);

  // Whether the current user can manage (edit/delete/add-resources) this
  // project.  Uses the backend-annotated ``can_edit`` field (populated by
  // ``entity_service._annotate_access``) OR falls back to comparing the
  // ``created_by_id`` against the authenticated user.
  //
  // NOTE: This hook MUST sit *after* the `project` useState above.
  // Previously it was declared earlier in the component (where it
  // appeared to read `project` from `useAuth()` context), which put
  // the dependency array `[project, user?.id]` into the temporal dead
  // zone and crashed the page with
  // "Cannot access 'project' before initialization" (renamed to 'o' in
  // minified builds) on every render.
  const canEditProject = useMemo(() => {
    if (project == null) return false;
    if (isAdmin) return true;
    if (project.can_edit !== undefined) return Boolean(project.can_edit);
    return project.created_by_id === user?.id;
  }, [project, user?.id, isAdmin]);

  // ── Edit state ──
  const [editing, setEditing] = useState(false);
  const [editName, setEditName] = useState('');
  const [editDesc, setEditDesc] = useState('');
  const [editStatus, setEditStatus] = useState('active');
  const [editLlmModelId, setEditLlmModelId] = useState(null);

  // ── LLM catalog (for badge + per-agent chips) ──
  const [llmModels, setLlmModels] = useState([]);

  // Helper: resolve model_id → {name, is_private, ...}
  const llmModelById = (id) => (id ? llmModels.find((m) => m.id === id) : null);
  const llmModelName = (id) => llmModelById(id)?.name || null;

  // Fetch the LLM catalog once on mount (best-effort; hidden if feature is off)
  useEffect(() => {
    let cancelled = false;
    authFetch('/api/llm/feature-status')
      .then((r) => (r.ok ? r.json() : null))
      .then((status) => {
        if (cancelled || !status?.enabled) return;
        return authFetch('/api/llm/models');
      })
      .then((res) => {
        if (cancelled || !res || !res.ok) return null;
        return res.json();
      })
      .then((rows) => {
        if (cancelled || !Array.isArray(rows)) return;
        setLlmModels(rows);
      })
      .catch(() => { /* silent — feature off or no models */ });
    return () => { cancelled = true; };
  }, []);

  // ── Lazy-loaded sections ──
  const [agents, setAgents] = useState([]);
  const [kbs, setKbs] = useState([]);
  const [conversations, setConversations] = useState([]);
  const [automations, setAutomations] = useState([]);
  const [memories, setMemories] = useState([]);

  // ── Modals / sheets ──
  const [agentDialogOpen, setAgentDialogOpen] = useState(false);
  const [accessDialogOpen, setAccessDialogOpen] = useState(false);
  const [policyDialogOpen, setPolicyDialogOpen] = useState(false);
  const [policyInitialUserId, setPolicyInitialUserId] = useState(null);
  const [kbDialogOpen, setKbDialogOpen] = useState(false);
  const [resourcesOpen, setResourcesOpen] = useState(false);
  const [createAutoOpen, setCreateAutoOpen] = useState(false);
  const [historyTask, setHistoryTask] = useState(null);  // opened task for history sheet

  // ── Active section (sidebar) ──
  const [activeSection, setActiveSection] = useState('agents');
  const sectionRefs = useRef({});

  useEffect(() => { loadAll(); /* eslint-disable-next-line */ }, [projectId]);

  useEffect(() => {
    function onScroll() {
      const scrollY = window.scrollY + 120;
      let current = 'agents';
      // Only scrollable sections participate in the active-section
      // tracking; sidebar items that open a sheet (e.g. Resources)
      // never become "active" via scroll.
      for (const s of SECTIONS.filter((x) => x.scrollable)) {
        const node = sectionRefs.current[s.key];
        if (node && node.offsetTop <= scrollY) current = s.key;
      }
      setActiveSection(current);
    }
    window.addEventListener('scroll', onScroll, { passive: true });
    onScroll();
    return () => window.removeEventListener('scroll', onScroll);
  }, []);

  async function loadAll() {
    setLoading(true);
    try {
      const p = await base44.entities.Project.get(projectId);
      setProject(p);
      setEditName(p.name || '');
      setEditDesc(p.description || '');
      setEditStatus(p.status || 'active');
      setEditLlmModelId(p.llm_model_id || null);
      const legacyName = (p.name && p.name !== 'global') ? p.name : null;

      // Agents: many-to-many via ProjectAgent membership table. We
      // fetch the memberships first, then resolve agent_ids to full
      // AgentApp records. As a backward-compat safety net, we ALSO
      // keep the legacy `project_id` filter — covers the brief window
      // before the migration runs, and rows whose membership was lost.
      const loadAgents = async () => {
        const memberships = await base44.entities.ProjectAgent
          .filter({ project_id: projectId }, '-updated_date', 500)
          .catch(() => []);
        const memberIds = Array.from(new Set(
          (Array.isArray(memberships) ? memberships : [])
            .map((m) => m.agent_id)
            .filter(Boolean)
        ));
        let memberAgents = [];
        if (memberIds.length > 0) {
          // Fetch all agents once, then filter in-memory (avoids N round trips)
          const all = await base44.entities.AgentApp.list('-updated_date', 500)
            .catch(() => []);
          const byId = new Map((Array.isArray(all) ? all : []).map((a) => [a.id, a]));
          memberAgents = memberIds
            .map((id) => byId.get(id))
            .filter(Boolean);
        }
        // Legacy fallback: agents with project_id (or legacy project name) match
        const legacyAgents = await base44.entities.AgentApp
          .filter({ project_id: projectId }, '-updated_date', 200)
          .then((rows) => legacyName ? mergeFbk(rows,
            base44.entities.AgentApp.filter({ project: legacyName }, '-updated_date', 200)
          ) : rows)
          .catch(() => []);
        // Union: members + legacy (dedup by id)
        const seen = new Set(memberAgents.map((a) => a.id));
        for (const a of (Array.isArray(legacyAgents) ? legacyAgents : [])) {
          if (a && a.id && !seen.has(a.id)) { memberAgents.push(a); seen.add(a.id); }
        }
        return memberAgents;
      };

      const [ag, kb, cs, au, me] = await Promise.allSettled([
        loadAgents(),
        // Project-bundle KB sharing: use the dedicated project-KB
        // endpoint that returns ALL KBs bound to this project
        // (including admin-created ones for share recipients).
        // The backend OR-filter handles both project_id and legacy
        // project name, so no mergeFbk fallback is needed.
        (async () => {
          const res = await authFetch(
            `/api/apps/${appParams.appId}/projects/${projectId}/knowledge-bases?limit=200`
          );
          if (!res.ok) {
            console.warn(`[ProjectDetail] Failed to load project KBs (${res.status}): ${res.statusText}`);
            throw new Error('Failed to load project KBs');
          }
          return res.json();
        })(),
        // AgentConversation has only a ``project_id`` FK — there is no
        // legacy ``project`` string column. The previous code had a
        // ``mergeFbk`` fallback that re-queried with ``{project: legacyName}``,
        // but the backend's parse_query silently drops filters for
        // non-existent model columns, so the fallback returned ALL
        // conversations (unfiltered). mergeFbk then unioned that
        // "fallback" into the result, polluting the project-scoped
        // "Recent Chats" list with every conversation in the DB.
        //
        // The other entities below (KnowledgeBase, AutomationTask)
        // DO have both ``project_id`` and legacy ``project`` columns
        // so the fallback is correct for them and is kept.
        base44.entities.AgentConversation.filter({ project_id: projectId }, '-updated_date', 100),
        // Automations: prefer project_id (FK); fall back to project name
        // for backends that only know the legacy string field.
        base44.entities.AutomationTask.filter({ project_id: projectId }, '-updated_date', 200)
          .then((rows) => legacyName ? mergeFbk(rows,
            base44.entities.AutomationTask.filter({ project: legacyName }, '-updated_date', 200)
          ) : rows)
          .catch(() => []),
        // Project-scoped memory: the agent's persistent memory for this
        // project (Shared Memory panel). Uses the dedicated router
        // (backend/app/routers/project_memories.py) — strict project_id
        // scoping, no legacy name fallback needed.
        (async () => {
          const res = await authFetch(`/api/projects/${projectId}/memories`);
          if (!res.ok) {
            console.warn(`[ProjectDetail] Failed to load project memories (${res.status})`);
            return [];
          }
          const data = await res.json();
          return Array.isArray(data.entries) ? data.entries : [];
        })(),
      ]);
      setAgents(settled(ag, []));
      setKbs(settled(kb, []));
      setConversations(settled(cs, []));
      setAutomations(settled(au, []));
      setMemories(settled(me, []));
    } finally {
      setLoading(false);
    }
  }

  const translate = useTranslate(
    [editName, editDesc, project?.name, project?.description].filter(Boolean),
    lang,
  );

  const setRef = useCallback((key) => (node) => {
    sectionRefs.current[key] = node;
  }, []);

  function scrollToSection(key) {
    const node = sectionRefs.current[key];
    if (node) node.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  async function saveEdit() {
    if (!editName.trim()) return;
    const updated = await base44.entities.Project.update(projectId, {
      name: editName.trim(),
      description: editDesc.trim() || undefined,
      status: editStatus,
      llm_model_id: editLlmModelId || null,
    });
    setProject(updated);
    setEditing(false);
  }

  // Set / clear a single agent's llm_model_id (for per-agent chip action).
  async function handleSetAgentLlm(agentId, modelId) {
    try {
      const res = await authFetch('/api/llm/apply-to-agent', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ agent_id: agentId, model_id: modelId }),
      });
      if (!res.ok) {
        const detail = (await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`;
        toast({ title: isEn ? `Update failed: ${detail}` : `更新失败：${detail}`, variant: 'destructive' });
        return;
      }
      await loadAll();
    } catch (e) {
      toast({ title: isEn ? `Network error: ${e.message}` : `网络错误：${e.message}`, variant: 'destructive' });
    }
  }

  async function archive() {
    if (!window.confirm(
      (isEn
        ? `Archive project "${project?.name}"?`
        : `确定要归档项目「${project?.name}」吗？`)
    )) return;
    try {
      await base44.entities.Project.update(projectId, { status: 'archived' });
      toast({
        title: isEn ? 'Project archived' : '项目已归档',
        description: project?.name,
      });
      navigate('/my-space');
    } catch (e) {
      console.error('archive failed:', e);
      toast({
        title: isEn ? 'Archive failed' : '归档失败',
        description: e?.message || String(e),
        variant: 'destructive',
      });
    }
  }

  // ── Chat with this agent (scoped to the current project) ──
  //
  // Clicking an agent card (or the "open" button) should send the user
  // into the Chat page with this specific agent pre-selected AND the
  // current project pre-selected, so:
  //   - The chat input shows the agent chip immediately.
  //   - Any new chat session is auto-bound to this project (so the
  //     session shows up in the project's "Recent Chats" list later).
  //   - The agent's v3 conversation is bound to this project so the
  //     data-source runtime inherits the project's KBs at runtime.
  //
  // We also call `newChat(pname)` from the ChatSessionContext first,
  // which sets the context's activeId to null. That matters because
  // Chat.jsx (the route we're navigating to) reads `activeId` from
  // the same context: if we don't clear it, the user is dropped back
  // into the *previously* active session — which may be an unrelated
  // conversation from a different agent, project, or even from days
  // ago. The intent of clicking an agent is "start a fresh chat with
  // THIS agent"; clearing the active session is the only way to make
  // that happen reliably regardless of what the user was doing a
  // moment ago.
  //
  // Calling `newChat` is a no-op (state-wise) if the user wasn't in
  // any session, so the cost is one extra context update in the
  // common case.
  function chatWithAgent(a) {
    if (!a) return;
    const pname = project?.name || '';
    // Clear the currently-active session BEFORE we navigate. The
    // context lives on the shared AppLayout, so this update is
    // visible to Chat.jsx the moment it mounts on the next render.
    try { newChat(pname); } catch (_) { /* defensive: never block navigation */ }
    const params = new URLSearchParams();
    params.set('agent', a.id);
    if (projectId) params.set('project', projectId);
    if (pname) params.set('projectName', pname);
    navigate(`/?${params.toString()}`);
  }

  // ── Per-row membership control ──
  async function removeAgent(a) {
    try {
      // Many-to-many: delete the ProjectAgent membership row (NOT the
      // agent itself, and NOT clearing its primary project_id — it
      // might be a member of other projects too). Falls back to the
      // legacy clear-project_id flow if no membership row exists (e.g.
      // for rows that pre-date the migration), AND if the ProjectAgent
      // entity itself is not registered on the backend (older backend
      // deployments don't have it). This makes the X button work on
      // both old and new backends.
      let usedLegacy = false;
      try {
        const memberships = await base44.entities.ProjectAgent.filter({
          project_id: projectId,
          agent_id: a.id,
        });
        if (Array.isArray(memberships) && memberships.length > 0) {
          for (const m of memberships) {
            try { await base44.entities.ProjectAgent.delete(m.id); }
            catch (e) { console.warn('ProjectAgent.delete failed:', e); }
          }
        } else {
          // No membership rows found — fall back to the legacy path.
          usedLegacy = true;
        }
      } catch (e) {
        // ProjectAgent entity is not available (e.g. old backend).
        // Fall back to the legacy clear-project_id flow.
        console.warn('ProjectAgent.filter failed, using legacy fallback:', e);
        usedLegacy = true;
      }
      if (usedLegacy) {
        await base44.entities.AgentApp.update(a.id, {
          project_id: null,
          project: 'global',
        });
      }
      setAgents((prev) => prev.filter((x) => x.id !== a.id));
      toast({
        title: isEn ? 'Removed from project' : '已移出项目',
        description: a.name,
      });
    } catch (e) {
      console.error('removeAgent failed:', e);
      toast({
        title: isEn ? 'Remove failed' : '移除失败',
        description: e?.message || String(e),
        variant: 'destructive',
      });
    }
  }

  async function removeKb(kb) {
    try {
      await base44.entities.KnowledgeBase.update(kb.id, { project_id: null, project: 'global' });
      setKbs((prev) => prev.filter((x) => x.id !== kb.id));
      toast({
        title: isEn ? 'Removed from project' : '已移出项目',
        description: kb.name,
      });
    } catch (e) {
      console.error('removeKb failed:', e);
      toast({
        title: isEn ? 'Remove failed' : '移除失败',
        description: e?.message || String(e),
        variant: 'destructive',
      });
    }
  }

  // ── Shared Memory (project-scoped AgentMemory via project_memories router) ──
  async function addMemory({ content, importance = 0 }) {
    try {
      const res = await authFetch(`/api/projects/${projectId}/memories`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content, importance, target: 'memory', pinned: false }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        toast({
          title: isEn ? `Add failed: ${data.detail || res.status}` : `添加失败：${data.detail || res.status}`,
          variant: 'destructive',
        });
        return false;
      }
      setMemories((prev) => [data, ...prev]);
      toast({ title: isEn ? 'Memory added' : '已添加记忆' });
      return true;
    } catch (e) {
      console.error('addMemory failed:', e);
      toast({
        title: isEn ? 'Network error' : '网络错误',
        description: e?.message || String(e),
        variant: 'destructive',
      });
      return false;
    }
  }

  async function updateMemory(id, patchBody) {
    try {
      const res = await authFetch(`/api/projects/${projectId}/memories/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patchBody),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        toast({
          title: isEn ? `Update failed: ${data.detail || res.status}` : `更新失败：${data.detail || res.status}`,
          variant: 'destructive',
        });
        return false;
      }
      setMemories((prev) => prev.map((m) => (m.id === id ? data : m)));
      return true;
    } catch (e) {
      console.error('updateMemory failed:', e);
      toast({
        title: isEn ? 'Network error' : '网络错误',
        description: e?.message || String(e),
        variant: 'destructive',
      });
      return false;
    }
  }

  async function deleteMemory(id) {
    if (!window.confirm(
      isEn ? 'Delete this memory? This cannot be undone.' : '确定要删除这条记忆吗？此操作不可撤销。'
    )) return;
    try {
      const res = await authFetch(`/api/projects/${projectId}/memories/${id}`, { method: 'DELETE' });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        toast({
          title: isEn ? `Delete failed: ${data.detail || res.status}` : `删除失败：${data.detail || res.status}`,
          variant: 'destructive',
        });
        return;
      }
      setMemories((prev) => prev.filter((m) => m.id !== id));
      toast({ title: isEn ? 'Memory deleted' : '记忆已删除' });
    } catch (e) {
      console.error('deleteMemory failed:', e);
      toast({
        title: isEn ? 'Network error' : '网络错误',
        description: e?.message || String(e),
        variant: 'destructive',
      });
    }
  }

  // ── Render ──
  if (loading) {
    return (
      <div className="h-full overflow-y-auto px-8 py-8">
        <div className="flex justify-center py-20">
          <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
        </div>
      </div>
    );
  }

  if (!project) {
    return (
      <div className="flex h-full items-center justify-center px-8 py-12">
        <div className="text-center">
          <Folder className="mx-auto h-10 w-10 text-muted-foreground" />
          <h3 className="mt-3 font-display text-lg">{t.projectDetail?.notFound || 'Project not found'}</h3>
          <Button variant="outline" size="sm" onClick={() => navigate('/my-space')} className="mt-4">
            <ArrowLeft className="mr-1 h-3.5 w-3.5" /> {t.projectDetail?.backTo || 'Back to Projects'}
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto bg-background">
      {/* ── Header ── */}
      <div className="border-b border-border bg-gradient-to-b from-secondary/40 to-background">
        <div className="mx-auto max-w-7xl px-8 py-6">
          <button
            onClick={() => navigate('/my-space')}
            className="mb-4 inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
          >
            <ArrowLeft className="h-3.5 w-3.5" />
            {t.projectDetail?.backTo || 'Back to Projects'}
          </button>

          <div className="flex items-start gap-4">
            <div className="flex h-14 w-14 shrink-0 items-center justify-center overflow-hidden rounded-2xl bg-white shadow-sm ring-1 ring-border">
              <Folder className="h-7 w-7 text-muted-foreground" />
            </div>
            <div className="min-w-0 flex-1">
              {editing ? (
                <Input
                  value={editName}
                  onChange={(e) => setEditName(e.target.value)}
                  autoFocus
                  className="text-lg font-display"
                />
              ) : (
                <h1
                  onClick={() => canEditProject && setEditing(true)}
                  className={
                    canEditProject
                      ? "cursor-text font-display text-2xl font-semibold text-foreground transition-colors hover:bg-secondary/50 rounded-md px-1"
                      : "font-display text-2xl font-semibold text-foreground px-1"
                  }
                  title={canEditProject ? (isEn ? 'Click to edit' : '点击编辑') : ''}
                >
                  {translate(project.name)}
                </h1>
              )}
              <div className="mt-1.5 flex flex-wrap items-center gap-2">
                <StatusPill status={project.status} />
                {/* LLM model — inline selector (always visible when user can edit).
                    Self-handles empty-catalog state with a link to Settings. */}
                {canEditProject && (
                  <div className="inline-flex items-center">
                    <LlmModelSelector
                      showLabel={false}
                      value={project.llm_model_id || null}
                      onChange={async (id) => {
                        try {
                          const updated = await base44.entities.Project.update(projectId, { llm_model_id: id });
                          setProject(updated);
                          toast({
                            title: isEn
                              ? `Project model ${id ? 'set' : 'cleared'}`
                              : `项目模型已${id ? '设置' : '清除'}`,
                          });
                        } catch (e) {
                          toast({
                            title: isEn ? `Save failed: ${e.message}` : `保存失败：${e.message}`,
                            variant: 'destructive',
                          });
                        }
                      }}
                    />
                  </div>
                )}
                {!canEditProject && project.llm_model_id && llmModelName(project.llm_model_id) && (
                  <span className="inline-flex items-center gap-1 rounded-full border border-border bg-secondary/40 px-2 py-0.5 text-[11px] font-medium text-foreground">
                    <Cpu className="h-3 w-3" />
                    {llmModelName(project.llm_model_id)}
                  </span>
                )}
                <span className="text-xs text-muted-foreground">
                  {agents.length + kbs.length + conversations.length}
                  {' '}{isEn ? 'items' : '项资产'}
                </span>
              </div>
              {editing ? (
                <Textarea
                  value={editDesc}
                  onChange={(e) => setEditDesc(e.target.value)}
                  placeholder={isEn ? 'What is this project about?' : '这个项目主要解决什么？'}
                  rows={2}
                  className="mt-2 resize-none"
                />
              ) : project.description ? (
                <p className="mt-2 max-w-3xl text-sm text-muted-foreground">{translate(project.description)}</p>
              ) : (
                canEditProject ? (
                  <p
                    onClick={() => setEditing(true)}
                    className="mt-2 max-w-3xl cursor-text text-sm italic text-muted-foreground/70"
                  >
                    {isEn ? 'Add a description (click here)' : '添加描述（点击此处）'}
                  </p>
                ) : null
              )}
              {editing && (
                <div className="mt-3 flex flex-wrap items-center gap-2">
                  {/* Color swatches removed: the user asked for a
                      simple, color-free project detail view. The
                      ``color`` field on the Project model is still
                      present in the backend (for backward
                      compatibility) but the edit form no longer
                      surfaces a picker or sends ``color`` to the
                      API. */}
                  <select
                    value={editStatus}
                    onChange={(e) => setEditStatus(e.target.value)}
                    className="ml-2 rounded-md border border-border bg-background px-2 py-1 text-xs"
                  >
                    <option value="active">{isEn ? 'Active' : '活跃'}</option>
                    <option value="archived">{isEn ? 'Archived' : '已归档'}</option>
                  </select>
                  <div className="mt-2">
                    <LlmModelSelector
                      value={project.llm_model_id || null}
                      onChange={(id) => setEditLlmModelId(id)}
                      disabled={!canEditProject || project.resource_type === 'company'}
                    />
                  </div>
                  <div className="ml-auto flex gap-2">
                    <Button size="sm" variant="outline" onClick={() => setEditing(false)}>
                      <X className="mr-1 h-3 w-3" /> {t.projectDetail?.cancel || 'Cancel'}
                    </Button>
                    <Button size="sm" onClick={saveEdit}>
                      <Check className="mr-1 h-3 w-3" /> {t.projectDetail?.save || 'Save'}
                    </Button>
                  </div>
                </div>
              )}
            </div>

            {!editing && canEditProject && (
              <div className="flex shrink-0 gap-2">
                <Button variant="outline" size="sm" className="gap-1.5" onClick={archive}>
                  <Trash2 className="h-4 w-4" />
                </Button>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* ── Body: sticky sidebar + scrollable main ── */}
      <div className="mx-auto flex max-w-7xl gap-6 px-8 py-6">
        {/* Sticky sidebar (no Overview / Files — removed per user) */}
        <aside className="sticky top-4 hidden h-fit w-52 shrink-0 md:block">
          <div className="rounded-xl border border-border bg-card p-2 shadow-sm">
            {SECTIONS.filter((s) => {
              if (s.action === 'access') return isAdmin && canEditProject;
              if (s.action === 'policy') return canEditProject;
              return true;
            }).map((s) => {
              const Icon = s.icon;
              const count = countFor(s.key, { agents, kbs, conversations, memories });
              const active = s.scrollable && activeSection === s.key;
              const onClick = s.scrollable
                ? () => scrollToSection(s.key)
                : s.action === 'access'
                  ? () => setAccessDialogOpen(true)
                  : s.action === 'policy'
                    ? () => { setPolicyInitialUserId(null); setPolicyDialogOpen(true); }
                    : () => setResourcesOpen(true);
              const label = s.scrollable
                ? (t.projectDetail?.sections?.[s.key] || s.key)
                : s.action === 'access'
                  ? (isEn ? 'Access' : '访问权限')
                  : s.action === 'policy'
                    ? (t.accessPolicy?.manageAccess || (isEn ? 'Manage Access' : '管理访问'))
                    : (t.projectDetail?.sections?.[s.key] || 'Resources');
              return (
                <button
                  key={s.key}
                  onClick={onClick}
                  className={`flex w-full items-center justify-between gap-2 rounded-lg px-2.5 py-1.5 text-left text-xs transition-colors ${active ? 'bg-primary/10 text-primary' : 'text-muted-foreground hover:bg-secondary hover:text-foreground'
                    }`}
                  aria-label={label}
                >
                  <span className="inline-flex items-center gap-2">
                    <Icon className="h-3.5 w-3.5" />
                    {label}
                  </span>
                  {count > 0 && (
                    <span className={`rounded-full px-1.5 text-[10px] ${active ? 'bg-primary/20 text-primary' : 'bg-secondary text-muted-foreground'}`}>
                      {count}
                    </span>
                  )}
                </button>
              );
            })}
            {/* Inline edit-shortcut on the sidebar — only for owner */}
            {canEditProject && (
              <>
                <div className="my-1 border-t border-border" />
                <button
                  onClick={() => setEditing(true)}
                  className="flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-xs text-muted-foreground hover:bg-secondary hover:text-foreground"
                >
                  <Pencil className="h-3.5 w-3.5" />
                  {isEn ? 'Edit project' : '编辑项目'}
                </button>
              </>
            )}
          </div>
        </aside>

        {/* Main */}
        <main className="min-w-0 flex-1 space-y-6">
          {/* ── Agents ── */}
          <section
            id="project-section-agents"
            ref={setRef('agents')}
            className="scroll-mt-20 rounded-xl border border-border bg-card p-5 shadow-sm"
          >
            <header className="mb-4 flex items-center justify-between border-b border-border pb-3">
              <div className="flex items-center gap-2">
                <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-primary/10 text-primary">
                  <Bot className="h-4 w-4" />
                </span>
                <h2 className="font-display text-base font-semibold text-foreground">
                  {t.projectDetail?.sections?.agents || 'Agents'}
                </h2>
                <span className="rounded-full bg-secondary px-2 py-0.5 text-[11px] text-muted-foreground">
                  {agents.length}
                </span>
                {/* Subtle inline link to the Resources sheet so users
                    can see at-a-glance that the project has resources
                    without a big Data Sources card competing with
                    the main flow. */}
                {kbs.length > 0 && (
                  <button
                    type="button"
                    onClick={() => setResourcesOpen(true)}
                    className="ml-1 inline-flex items-center gap-1 rounded-full bg-secondary/60 px-2 py-0.5 text-[11px] text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
                    title={T('查看项目资源', 'View project resources')}
                  >
                    <Layers className="h-3 w-3" />
                    {kbs.length} {t.projectDetail?.sections?.resources || 'Resources'}
                  </button>
                )}
              </div>
              <div className="flex gap-2">
                {canEditProject && (
                  <Button
                    size="sm" variant="outline"
                    className="gap-1.5"
                    onClick={() => setAgentDialogOpen(true)}
                  >
                    <ListChecks className="h-3.5 w-3.5" />
                    {isEn ? 'Manage' : '管理'}
                  </Button>
                )}
                <Button
                  size="sm" variant="default"
                  className="gap-1.5"
                  onClick={() => navigate(`/my-space?initialProjectId=${projectId}`)}
                >
                  <Plus className="h-3.5 w-3.5" />
                  {isEn ? 'New Agent' : '新建 Agent'}
                </Button>
              </div>
            </header>
            <AgentsSection agents={agents} onRemove={removeAgent} onChat={chatWithAgent} lang={lang} T={T} canEdit={canEditProject} />
          </section>

          {/* ── Recent Chats (AgentConversation only) ── */}
          <section
            id="project-section-chats"
            ref={setRef('chats')}
            className="scroll-mt-20 rounded-xl border border-border bg-card p-5 shadow-sm"
          >
            <header className="mb-4 flex items-center justify-between border-b border-border pb-3">
              <div className="flex items-center gap-2">
                <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-primary/10 text-primary">
                  <MessageSquare className="h-4 w-4" />
                </span>
                <h2 className="font-display text-base font-semibold text-foreground">
                  {isEn ? 'Recent Chats' : '对话记录'}
                </h2>
                <span className="rounded-full bg-secondary px-2 py-0.5 text-[11px] text-muted-foreground">
                  {conversations.length}
                </span>
              </div>
              <span className="text-xs text-muted-foreground">
                {isEn ? 'Agent conversations bound to this project' : '属于本项目的智能体对话'}
              </span>
            </header>
            <ConversationsSection
              conversations={conversations}
              lang={lang}
              T={T}
              onOpenConv={(conv) => {
                // Open the existing agent conversation in the chat
                // page. We pass the conv id and (when present) the
                // agent_name so the chat page can rehydrate the
                // right agent without a second round-trip.
                //
                // IMPORTANT: also include the current project so the
                // chat page inherits the project's data-source
                // runtime scope. Without `?project=...&projectName=
                // ...`, the v3 stream request body has no project
                // context, the backend's prepare_data_source_runtime
                // only sees `conv.project_id` (which is null for
                // convs created before project scoping landed, and
                // null even for newer convs when the user opened
                // them from somewhere other than a project-scoped
                // entry point), and the agent reports "no bound
                // data sources" — even though the project clearly
                // has a KB attached. Carrying the project params in
                // the deep-link URL makes handleAgentSend forward
                // them in the stream body so the runtime can extend
                // the agent's KB set with the project's KBs.
                const qs = new URLSearchParams();
                qs.set('conv', conv.id);
                if (conv.agent_name) qs.set('agentName', conv.agent_name);
                if (projectId) qs.set('project', projectId);
                if (project?.name) qs.set('projectName', project.name);
                navigate(`/?${qs.toString()}`);
              }}
            />
          </section>

          {/* ── Automation ──
              Project-scoped automation tasks with their latest result,
              and a unified timeline of recent runs from all project
              automations combined. Click a task card to see the full
              execution history in a side sheet (keeps the project
              context). */}
          <section
            id="project-section-automation"
            ref={setRef('automation')}
            className="scroll-mt-20 rounded-xl border border-border bg-card p-5 shadow-sm"
          >
            <header className="mb-4 flex items-center justify-between gap-3 border-b border-border pb-3">
              <div className="flex items-center gap-2">
                <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-primary/10 text-primary">
                  <Zap className="h-4 w-4" />
                </span>
                <h2 className="font-display text-base font-semibold text-foreground">
                  {t.projectDetail?.sections?.automation || (isEn ? 'Automation' : '自动化')}
                </h2>
                <span className="rounded-full bg-secondary px-2 py-0.5 text-[11px] text-muted-foreground">
                  {automations.length}
                </span>
              </div>
              <Button
                size="sm"
                variant="default"
                className="gap-1.5"
                onClick={() => setCreateAutoOpen(true)}
              >
                <Plus className="h-3.5 w-3.5" />
                {t.projectDetail?.newAutomation
                  || (isEn ? 'New Automation' : '新建自动化')}
              </Button>
            </header>
            <AutomationSection
              automations={automations}
              lang={lang}
              isEn={isEn}
              T={T}
              onOpenTask={setHistoryTask}
              onCreateClick={() => setCreateAutoOpen(true)}
            />
          </section>

          {/* ── Shared Memory ──
              Project-scoped agent memory (AgentMemory rows via the
              project_memories router). Users can see what the agent
              remembers for THIS project, add facts/decisions/insights,
              pin important entries, edit, and hard-delete. Strictly
              project-scoped — no cross-project leakage. */}
          <section
            id="project-section-memory"
            ref={setRef('memory')}
            className="scroll-mt-20 rounded-xl border border-border bg-card p-5 shadow-sm"
          >
            <header className="mb-4 flex items-center justify-between border-b border-border pb-3">
              <div className="flex items-center gap-2">
                <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-primary/10 text-primary">
                  <Brain className="h-4 w-4" />
                </span>
                <h2 className="font-display text-base font-semibold text-foreground">
                  {t.projectDetail?.sections?.memory || (isEn ? 'Shared Memory' : '共享记忆')}
                </h2>
                <span className="rounded-full bg-secondary px-2 py-0.5 text-[11px] text-muted-foreground">
                  {memories.length}
                </span>
              </div>
              <span className="text-xs text-muted-foreground">
                {isEn ? 'What this project remembers' : '本项目智能体持续记住的事实、决策与洞察'}
              </span>
            </header>
            <MemorySection
              memories={memories}
              lang={lang}
              T={T}
              canEdit={canEditProject}
              onAdd={addMemory}
              onUpdate={updateMemory}
              onDelete={deleteMemory}
            />
          </section>
        </main>
      </div>

      {/* ── Resources sheet (slide-out from the right) ──
          Hides KnowledgeBase management behind a single click on the
          sidebar so the main flow stays focused on Agents and Chats. */}
      <Sheet open={resourcesOpen} onOpenChange={setResourcesOpen}>
        <SheetContent
          side="right"
          className="flex w-full flex-col gap-0 p-0 sm:max-w-md"
        >
          <SheetHeader className="border-b border-border px-5 py-4">
            <div className="flex items-start justify-between gap-3 pr-6">
              <div className="min-w-0">
                <SheetTitle className="flex items-center gap-2 text-base">
                  <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-primary/10 text-primary">
                    <Layers className="h-4 w-4" />
                  </span>
                  {t.projectDetail?.resourcesTitle || 'Resources'}
                </SheetTitle>
                <SheetDescription className="mt-1">
                  {t.projectDetail?.resourcesDesc
                    || (isEn
                      ? 'Knowledge bases connected to this project. They are available to every agent in this project automatically.'
                      : '连接到本项目的知识库，会对项目内所有 Agent 自动生效。')}
                </SheetDescription>
              </div>
            </div>
            <div className="mt-3 flex items-center justify-between gap-2">
              <span className="text-xs text-muted-foreground">
                {kbs.length} {isEn ? 'connected' : '已连接'}
              </span>
              {canEditProject && (
                <Button
                  size="sm"
                  variant="default"
                  className="gap-1.5"
                  onClick={() => {
                    setResourcesOpen(false);
                    // Open the picker on the next tick so the sheet can
                    // animate out cleanly first.
                    setTimeout(() => setKbDialogOpen(true), 50);
                  }}
                >
                  <Plus className="h-3.5 w-3.5" />
                  {t.projectDetail?.addResource
                    || (isEn ? 'Add Resource' : '添加资源')}
                </Button>
              )}
            </div>
          </SheetHeader>

          {/* Scrollable list */}
          <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
            <KbSection
              kbs={kbs}
              onRemove={(kb) => removeKb(kb).then(() => loadAll())}
              lang={lang}
              navigate={navigate}
              T={T}
              canEdit={canEditProject}
            />
          </div>
        </SheetContent>
      </Sheet>

      {/* ── Modals ── */}
      <AddAgentToProjectDialog
        open={agentDialogOpen}
        onOpenChange={setAgentDialogOpen}
        project={project}
        excludeIds={agents.map((a) => a.id)}
        onAdded={() => loadAll()}
        onRemoved={() => loadAll()}
      />
      <AddKbToProjectDialog
        open={kbDialogOpen}
        onOpenChange={setKbDialogOpen}
        project={project}
        excludeIds={kbs.map((k) => k.id)}
        onAdded={() => loadAll()}
      />

      {/* ── New automation dialog ──
          CreateResourceDialog (resourceType="automation") with the
          current project pre-selected. After submission it routes
          through `/?prefill=...` to the agent builder. The project
          is shown as a read-only badge so the user can see what
          data sources the Agent will inherit. */}
      <CreateResourceDialog
        open={createAutoOpen}
        onOpenChange={setCreateAutoOpen}
        resourceType="automation"
        defaultProjectName={project?.name}
        defaultProjectId={project?.id}
      />

      {/* ── Run history sheet (per-task) ──
          Click a task card or a run row in the timeline to open this.
          Stays on the project page (no navigation) and lists the
          task's full execution_history. */}
      <Sheet open={!!historyTask} onOpenChange={(o) => !o && setHistoryTask(null)}>
        <SheetContent
          side="right"
          className="flex w-full flex-col gap-0 p-0 sm:max-w-md"
        >
          <SheetHeader className="border-b border-border px-5 py-4">
            <SheetTitle className="flex items-center gap-2 text-base">
              <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-primary/10 text-primary">
                <Zap className="h-4 w-4" />
              </span>
              <span className="truncate">
                {historyTask?.name || T('任务执行历史', 'Run history')}
              </span>
            </SheetTitle>
            <SheetDescription>
              {historyTask?.schedule && (
                <span className="inline-flex items-center gap-1">
                  <Clock className="h-3 w-3" />
                  {historyTask.schedule}
                </span>
              )}
              {historyTask?.description && (
                <span className="ml-2">{historyTask.description}</span>
              )}
            </SheetDescription>
            <div className="mt-2 flex items-center gap-2 text-xs text-muted-foreground">
              <StatusPill status={historyTask?.status || 'idle'} small />
              <span>·</span>
              <span>
                {(Array.isArray(historyTask?.execution_history)
                  ? historyTask.execution_history.length
                  : 0)} {T('次运行', 'runs')}
              </span>
            </div>
          </SheetHeader>

          {/* Scrollable list */}
          <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
            {(() => {
              const history = Array.isArray(historyTask?.execution_history)
                ? historyTask.execution_history.slice().sort((a, b) => {
                  const ta = a.timestamp ? new Date(a.timestamp).getTime() : 0;
                  const tb = b.timestamp ? new Date(b.timestamp).getTime() : 0;
                  return tb - ta;
                })
                : [];
              if (history.length === 0) {
                return (
                  <EmptyState
                    icon={Activity}
                    label={T('暂无运行记录。', 'No runs yet.')}
                  />
                );
              }
              return (
                <ol className="space-y-2">
                  {history.map((run, i) => (
                    <li
                      key={`${run.timestamp || i}`}
                      className="rounded-lg border border-border/60 bg-background p-3"
                    >
                      <div className="flex items-center gap-2">
                        <RunStatusIcon status={run.status} />
                        <StatusPill status={run.status || 'unknown'} small />
                        <span className="ml-auto text-[11px] text-muted-foreground">
                          {run.timestamp ? formatRelativeTime(run.timestamp, lang) : ''}
                        </span>
                      </div>
                      {run.result && (
                        <p className="mt-2 whitespace-pre-wrap text-xs text-foreground/80">
                          {run.result}
                        </p>
                      )}
                    </li>
                  ))}
                </ol>
              );
            })()}
          </div>
        </SheetContent>
      </Sheet>

      {/* ── Admin: Manage Access dialog ── */}
      {accessDialogOpen && (
        <ResourceAccessDialog
          resourceType="project"
          resourceId={projectId}
          resourceName={project?.name || `Project ${projectId}`}
          onClose={() => setAccessDialogOpen(false)}
          onConfigureAccess={(userId) => {
            setAccessDialogOpen(false);
            setPolicyInitialUserId(userId);
            setPolicyDialogOpen(true);
          }}
        />
      )}

      {/* ── Data access policy dialog ── */}
      {policyDialogOpen && (
        <ResourceAccessPolicyDialog
          open={policyDialogOpen}
          resourceType="project"
          resourceId={projectId}
          resourceName={project?.name || `Project ${projectId}`}
          initialUserId={policyInitialUserId}
          onClose={() => setPolicyDialogOpen(false)}
        />
      )}

    </div>
  );
}

// ───────────────────────────────────────────────────────────────────
//                          Helpers
// ───────────────────────────────────────────────────────────────────

function StatusPill({ status, small }) {
  // Map every status we care about to a colour + label pair. The keys
  // here cover agent statuses (active/archived/disabled) as well as
  // automation run/task statuses (success/failed/running/skipped/etc).
  const STYLES = {
    active: { dot: 'bg-emerald-500', bg: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300' },
    success: { dot: 'bg-emerald-500', bg: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300' },
    running: { dot: 'bg-blue-500', bg: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300' },
    failed: { dot: 'bg-red-500', bg: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300' },
    skipped: { dot: 'bg-amber-500', bg: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300' },
    paused: { dot: 'bg-amber-500', bg: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300' },
    disabled: { dot: 'bg-muted-foreground', bg: 'bg-secondary text-muted-foreground' },
    archived: { dot: 'bg-muted-foreground', bg: 'bg-secondary text-muted-foreground' },
  };
  const s = (status || 'idle').toLowerCase();
  const style = STYLES[s] || { dot: 'bg-muted-foreground', bg: 'bg-secondary text-muted-foreground' };
  return (
    <span className={`inline-flex items-center gap-1 rounded-full ${small ? 'px-1.5 py-0 text-[10px]' : 'px-2 py-0.5 text-[11px]'} font-medium ${style.bg}`}>
      <span className={`h-1.5 w-1.5 rounded-full ${style.dot}`} />
      {s}
    </span>
  );
}

function EmptyState({ icon: Icon, label, action }) {
  return (
    <div className="flex flex-col items-center justify-center rounded-lg border border-dashed border-border py-10 text-center">
      <div className="mb-2 flex h-10 w-10 items-center justify-center rounded-full bg-secondary text-muted-foreground">
        <Icon className="h-5 w-5" />
      </div>
      <p className="text-xs text-muted-foreground">{label}</p>
      {action}
    </div>
  );
}

function countFor(key, s) {
  switch (key) {
    case 'agents': return s.agents.length;
    case 'resources': return s.kbs.length;
    case 'chats': return s.conversations.length;
    case 'memory': return s.memories.length;
    default: return 0;
  }
}

function settled(p, fallback) {
  return p && p.status === 'fulfilled' ? p.value : fallback;
}

// Merge a primary (project_id-filtered) list with a fallback (legacy
// project-name-filtered) list, deduping by id. Empty/empty fallback
// is a no-op so we don't error-out.
async function mergeFbk(primary, fallbackPromise) {
  let fallback = [];
  try { fallback = await fallbackPromise; } catch { /* noop */ }
  if (!Array.isArray(primary) || primary.length === 0) {
    return Array.isArray(fallback) ? fallback : primary;
  }
  const ids = new Set(primary.map((r) => r.id));
  const extras = (Array.isArray(fallback) ? fallback : []).filter((r) => !ids.has(r.id));
  return [...primary, ...extras];
}

// ───────────────────────────────────────────────────────────────────
//                          Section bodies
// ───────────────────────────────────────────────────────────────────

function AgentsSection({ agents, onRemove, onChat, lang, T, canEdit = true, project, llmModels = [] }) {
  const isEn = lang === 'en';
  if (!agents.length) {
    return (
      <EmptyState
        icon={Bot}
        label={T('该项目暂无 Agent。点击右上角「管理」从现有 Agent 中选择，或「新建 Agent」创建。',
          'No agents in this project yet. Use Manage to add existing ones or New Agent to create.')}
      />
    );
  }
  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
      {agents.map((a) => (
        <div
          key={a.id}
          className="group relative flex items-start gap-3 rounded-lg border border-border bg-background p-3 transition-colors hover:border-primary/40 cursor-pointer"
          onClick={() => onChat?.(a)}
        >
          <div
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary"
            onClick={(e) => { e.stopPropagation(); onChat?.(a); }}
          >
            <Bot className="h-4 w-4" />
          </div>
          <div
            className="min-w-0 flex-1"
            onClick={(e) => { e.stopPropagation(); onChat?.(a); }}
          >
            <p className="truncate font-display text-sm text-foreground group-hover:text-primary">
              {a.name}
            </p>
            <p className="line-clamp-2 mt-0.5 text-xs text-muted-foreground">
              {a.description || (T('暂无描述', 'No description'))}
            </p>
            <div className="mt-1.5 flex items-center gap-1.5">
              <Badge variant="secondary" className="text-[10px]">
                {a.status || 'active'}
              </Badge>
              {/* Per-agent LLM chip — shows current binding (own or
                  inherited from project). Inherited = project has the
                  model and agent doesn't have its own.
                  System agents always use catalog default, so we
                  show a locked badge instead of per-agent bindings. */}
              {(() => {
                if (a.is_system) {
                  return (
                    <Badge variant="outline" className="border-muted-foreground/30 text-[10px] text-muted-foreground" title={isEn ? 'System agent — always uses catalog default' : '系统智能体 — 始终使用目录默认模型'}>
                      <Lock className="mr-1 h-2.5 w-2.5" />
                      {isEn ? 'Default (locked)' : '默认（已锁定）'}
                    </Badge>
                  );
                }
                const ownModel = a.llm_model_id ? llmModels.find((m) => m.id === a.llm_model_id) : null;
                const projModel = project?.llm_model_id ? llmModels.find((m) => m.id === project.llm_model_id) : null;
                if (ownModel) {
                  return (
                    <Badge variant="outline" className="border-primary/40 text-[10px] text-primary">
                      <Cpu className="mr-1 h-2.5 w-2.5" />
                      {ownModel.name}
                    </Badge>
                  );
                }
                if (projModel) {
                  return (
                    <Badge variant="outline" className="border-dashed text-[10px] text-muted-foreground" title={isEn ? `Inherited from project (${projModel.name})` : `继承自项目（${projModel.name}）`}>
                      <Cpu className="mr-1 h-2.5 w-2.5" />
                      {isEn ? 'inherits project model' : '继承项目模型'}
                    </Badge>
                  );
                }
                return null;
              })()}
              <span className="text-[10px] text-muted-foreground">· {formatRelativeTime(a.updated_date, lang)}</span>
            </div>
          </div>
          <div className="absolute right-1.5 top-1.5 z-20 flex gap-1 opacity-0 transition-opacity group-hover:opacity-100">
            {canEdit && (
              <button
                type="button"
                onClick={(e) => { e.preventDefault(); e.stopPropagation(); onRemove?.(a); }}
                onMouseDown={(e) => e.stopPropagation()}
                className="flex h-7 w-7 cursor-pointer items-center justify-center rounded-md bg-background/90 text-muted-foreground shadow-sm transition-colors hover:bg-red-100 hover:text-red-500 dark:hover:bg-red-900/30"
                title={T('从项目移除', 'Remove from project')}
                aria-label={T('从项目移除', 'Remove from project')}
              >
                <X className="h-3.5 w-3.5" />
              </button>
            )}
            <button
              type="button"
              onClick={(e) => { e.preventDefault(); e.stopPropagation(); onChat?.(a); }}
              onMouseDown={(e) => e.stopPropagation()}
              className="flex h-7 w-7 cursor-pointer items-center justify-center rounded-md bg-background/90 text-muted-foreground shadow-sm transition-colors hover:text-foreground"
              title={T('与该 Agent 对话', 'Chat with agent')}
              aria-label={T('与该 Agent 对话', 'Chat with agent')}
            >
              <ArrowUpRight className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}

function KbSection({ kbs, onRemove, lang, navigate, T, canEdit = true }) {
  if (!kbs.length) {
    return (
      <EmptyState
        icon={Layers}
        label={T('该项目暂无资源。点击右上角「添加资源」从已有 Connectors 中选择。',
          'No resources yet. Use Add Resource to pick from your existing Connectors.')}
      />
    );
  }
  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
      {kbs.map((k) => {
        const isDb = k.db_type || k.source_kind === 'database';
        return (
          <div
            key={k.id}
            className="group relative flex items-start gap-3 rounded-lg border border-border bg-background p-3 transition-colors hover:border-primary/40"
          >
            <div
              className={`flex h-8 w-8 shrink-0 cursor-pointer items-center justify-center rounded-lg ${isDb
                  ? 'bg-blue-100 text-blue-600 dark:bg-blue-900/30 dark:text-blue-300'
                  : 'bg-amber-100 text-amber-600 dark:bg-amber-900/30 dark:text-amber-300'
                }`}
              onClick={() => navigate(`/my-space/kb/${k.id}`)}
            >
              <Database className="h-4 w-4" />
            </div>
            <div
              className="min-w-0 flex-1 cursor-pointer"
              onClick={() => navigate(`/my-space/kb/${k.id}`)}
            >
              <p className="truncate font-display text-sm text-foreground group-hover:text-primary">
                {k.name}
              </p>
              <p className="min-w-0 break-all text-[11px] text-muted-foreground">
                {isDb ? `${k.db_type || 'database'} · ${k.host || ''}${k.port ? ':' + k.port : ''}` : (k.file_type || T('文件', 'file'))}
              </p>
              <div className="mt-1.5 flex items-center gap-1.5">
                <Badge variant="secondary" className="text-[10px]">{k.status || 'active'}</Badge>
                <span className="text-[10px] text-muted-foreground">· {formatRelativeTime(k.updated_date, lang)}</span>
              </div>
            </div>
            <div className="absolute right-1.5 top-1.5 z-20 flex gap-1 opacity-0 transition-opacity group-hover:opacity-100">
              {canEdit && (
                <button
                  type="button"
                  onClick={(e) => { e.preventDefault(); e.stopPropagation(); onRemove?.(k); }}
                  onMouseDown={(e) => e.stopPropagation()}
                  className="flex h-7 w-7 cursor-pointer items-center justify-center rounded-md bg-background/90 text-muted-foreground shadow-sm transition-colors hover:bg-red-100 hover:text-red-500 dark:hover:bg-red-900/30"
                  title={T('从项目移除', 'Remove from project')}
                  aria-label={T('从项目移除', 'Remove from project')}
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              )}
              <button
                type="button"
                onClick={(e) => { e.preventDefault(); e.stopPropagation(); navigate(`/my-space/kb/${k.id}`); }}
                onMouseDown={(e) => e.stopPropagation()}
                className="flex h-7 w-7 cursor-pointer items-center justify-center rounded-md bg-background/90 text-muted-foreground shadow-sm transition-colors hover:text-foreground"
                title={T('打开', 'Open')}
                aria-label={T('打开', 'Open')}
              >
                <ArrowUpRight className="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function ConversationsSection({ conversations, lang, T, onOpenConv }) {
  if (!conversations.length) {
    return (
      <EmptyState
        icon={MessageSquare}
        label={T('暂无属于本项目的智能体对话。当 Agent 在此项目内运行时，对话将出现在这里。',
          'No agent conversations bound to this project yet. They will appear here when agents in this project run.')}
      />
    );
  }
  return (
    <div className="space-y-1">
      {conversations.map((c) => {
        const title = c.title || T('未命名对话', 'Untitled');
        const time = formatRelativeTime(c.updated_date || c.created_date, lang);
        return (
          <button
            key={c.id}
            type="button"
            onClick={() => onOpenConv && onOpenConv(c)}
            className="group flex w-full items-center gap-3 rounded-md border border-transparent px-2 py-1.5 text-left transition-colors hover:border-border hover:bg-secondary/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
            title={title}
          >
            <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-primary/10 text-primary group-hover:bg-primary/15">
              <Bot className="h-3.5 w-3.5" />
            </div>
            <span className="min-w-0 flex-1 truncate text-sm font-medium text-foreground">
              {title}
            </span>
            <span className="shrink-0 text-[11px] text-muted-foreground tabular-nums">
              {time}
            </span>
          </button>
        );
      })}
    </div>
  );
}


/* ─────────────────────────────────────────────────────────────────
 * AutomationSection — project-scoped automation tasks (top) plus a
 * unified recent-runs timeline (bottom).
 *
 * Empty state: if there are no automation tasks for the project, we
 * show a single inviting empty state that covers both sub-sections
 * so the section doesn't feel hollow. If there are tasks but no
 * runs yet, the tasks grid renders normally and the timeline shows
 * a smaller "no runs yet" note.
 * ──────────────────────────────────────────────────────────────── */
function AutomationSection({ automations, lang, isEn, T, onOpenTask, onCreateClick }) {
  if (!automations.length) {
    return (
      <EmptyState
        icon={Zap}
        label={T(
          '该项目暂无自动化任务。点击「新建自动化」配置定时报表、数据巡检或流程编排。',
          'No automations yet. Click New Automation to schedule reports, inspections, or workflows for this project.'
        )}
        action={(
          <Button size="sm" variant="default" className="mt-3 gap-1.5" onClick={onCreateClick}>
            <Plus className="h-3.5 w-3.5" />
            {T('新建自动化', 'New Automation')}
          </Button>
        )}
      />
    );
  }

  // Result-first vertical stack: each card is a single report that
  // takes the full content width. This is the user-friendly layout
  // — reports are meant to be read, not crammed into a 2-col grid
  // behind a "Show more" button.
  return (
    <div className="space-y-3">
      {automations.map((task) => (
        <TaskCard
          key={task.id}
          task={task}
          isEn={isEn}
          T={T}
          lang={lang}
          onClick={() => onOpenTask(task)}
        />
      ))}
    </div>
  );
}


/* ─────────────────────────────────────────────────────────────────
 * TaskCard — single automation task summary, **result-first**.
 *
 * The card is laid out so the latest result text is the largest and
 * most prominent element, with metadata (name, type, status,
 * schedule) in a compact header. Long results can be expanded inline
 * without leaving the card.
 *
 * The whole card is clickable to open the full history sheet, except
 * the inline "show more" / "show less" toggle which has its own
 * click handler (with stopPropagation).
 * ──────────────────────────────────────────────────────────────── */
function TaskCard({ task, isEn, T, lang, onClick }) {
  const history = Array.isArray(task.execution_history) ? task.execution_history : [];
  const lastRun = history.length > 0
    ? history.slice().sort((a, b) => {
      const ta = a.timestamp ? new Date(a.timestamp).getTime() : 0;
      const tb = b.timestamp ? new Date(b.timestamp).getTime() : 0;
      return tb - ta;
    })[0]
    : null;

  const status = task.status || 'idle';
  const resultText = (lastRun?.result ?? task.last_result ?? '').toString();

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onClick}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onClick?.();
        }
      }}
      className="group flex cursor-pointer flex-col items-stretch gap-3 rounded-lg border border-border bg-background p-4 text-left transition-all hover:border-primary/40 hover:shadow-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
    >
      {/* Compact metadata header */}
      <div className="flex w-full items-center gap-3 border-b border-border/60 pb-3">
        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
          <Zap className="h-4 w-4" />
        </span>
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium text-foreground">
            {task.name || T('未命名任务', 'Untitled task')}
          </p>
          <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[10px] text-muted-foreground">
            {task.type && (
              <span className="uppercase tracking-wide">
                {task.type.replace(/_/g, ' ')}
              </span>
            )}
            {task.llm_informed_tick && (
              <span className="inline-flex items-center gap-1 rounded-full bg-primary/10 px-1.5 py-0 text-[10px] font-medium text-primary" title={T('每次执行前由智能体生成执行简报', 'Agent receives a briefing before each scheduled run')}>
                <Sparkles className="h-2.5 w-2.5" />
                {T('智能调度', 'LLM-informed')}
              </span>
            )}
            {task.schedule && (
              <span className="inline-flex items-center gap-1">
                <Clock className="h-2.5 w-2.5" />
                {task.schedule}
              </span>
            )}
          </div>
        </div>
        <StatusPill status={status} />
      </div>

      {/* Result block — the focal point. Full content visible by
          default, no "Show more" collapse. The result IS the report
          the user came here to read. Long results get a safety cap
          on height with internal scroll so a single huge result
          doesn't push everything else off-screen. */}
      {resultText ? (
        <div className="max-h-[480px] overflow-y-auto rounded-md bg-secondary/40 px-4 py-3 text-sm leading-relaxed text-foreground/90">
          <p className="whitespace-pre-wrap break-words">{resultText}</p>
        </div>
      ) : (
        <div className="rounded-md border border-dashed border-border bg-secondary/20 px-3 py-4 text-center text-xs text-muted-foreground">
          {T('等待首次运行…', 'Awaiting first run…')}
        </div>
      )}

      {/* Compact footer */}
      <div className="flex w-full items-center justify-between text-[11px] text-muted-foreground">
        <span className="inline-flex items-center gap-1">
          <Calendar className="h-3 w-3" />
          {task.last_run || lastRun?.timestamp
            ? formatRelativeTime(task.last_run || lastRun.timestamp, lang)
            : T('尚未运行', 'Never run')}
        </span>
        {history.length > 1 && (
          <span className="inline-flex items-center gap-1 font-medium text-primary transition-transform group-hover:translate-x-0.5">
            {T('查看历史', 'View history')} ({history.length} {T('次', 'runs')})
            <ChevronRight className="h-3 w-3" />
          </span>
        )}
      </div>
    </div>
  );
}


/* Small icon next to a run status — green check, red alert, yellow
   pause, etc. Used in the history sheet's run items. */
function RunStatusIcon({ status }) {
  if (status === 'success') return <Check className="h-3.5 w-3.5 text-emerald-600" />;
  if (status === 'failed') return <AlertCircle className="h-3.5 w-3.5 text-red-600" />;
  if (status === 'running') return <Loader2 className="h-3.5 w-3.5 animate-spin text-blue-600" />;
  if (status === 'skipped' || status === 'paused') {
    return <Clock className="h-3.5 w-3.5 text-amber-600" />;
  }
  return <Clock className="h-3.5 w-3.5 text-muted-foreground" />;
}


/* ─────────────────────────────────────────────────────────────────
 * MemorySection — project-scoped Shared Memory panel.
 *
 * Renders the AgentMemory rows for this project (fetched via the
 * project_memories router): a usage bar (2200-char budget), an
 * inline add form, and a list of entries. Each entry supports
 * pin/unpin, inline edit (content + importance), and hard delete.
 * All mutations go straight to the backend — no LLM round-trip.
 * ──────────────────────────────────────────────────────────────── */
function MemorySection({ memories, lang, T, canEdit = true, onAdd, onUpdate, onDelete }) {
  const isEn = lang === 'en';
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState('');
  const [draftImportance, setDraftImportance] = useState(0);
  const [saving, setSaving] = useState(false);
  const [editingId, setEditingId] = useState(null);
  const [editDraft, setEditDraft] = useState('');
  const [editImportance, setEditImportance] = useState(0);

  const LIMIT = 2200;
  const totalChars = memories.reduce((s, m) => s + (m.char_count || (m.content || '').length), 0);
  const usagePct = Math.min(100, Math.round((totalChars / LIMIT) * 100));

  async function submitAdd() {
    const content = draft.trim();
    if (!content || saving) return;
    setSaving(true);
    try {
      const ok = await onAdd({ content, importance: draftImportance });
      if (ok) {
        setDraft('');
        setDraftImportance(0);
        setAdding(false);
      }
    } finally {
      setSaving(false);
    }
  }

  async function submitEdit(m) {
    const content = editDraft.trim();
    if (!content) return;
    const ok = await onUpdate(m.id, { content, importance: editImportance });
    if (ok) setEditingId(null);
  }

  function startEdit(m) {
    setEditingId(m.id);
    setEditDraft(m.content || '');
    setEditImportance(m.importance || 0);
  }

  return (
    <div className="space-y-3">
      {/* Usage bar + add button */}
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center justify-between text-[11px] text-muted-foreground">
            <span>
              {totalChars} / {LIMIT} {isEn ? 'chars' : '字符'}
            </span>
            <span className={usagePct >= 90 ? 'font-medium text-red-500' : ''}>{usagePct}%</span>
          </div>
          <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-secondary">
            <div
              className={`h-full rounded-full transition-all ${usagePct >= 90 ? 'bg-red-500' : 'bg-primary/70'}`}
              style={{ width: `${usagePct}%` }}
            />
          </div>
        </div>
        {canEdit && !adding && (
          <Button size="sm" variant="outline" className="gap-1.5 shrink-0" onClick={() => setAdding(true)}>
            <Plus className="h-3.5 w-3.5" />
            {T('添加记忆', 'Add Memory')}
          </Button>
        )}
      </div>

      {/* Inline add form */}
      {adding && (
        <div className="rounded-lg border border-border bg-background p-3">
          <Textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder={T('记录一条项目内的事实、决策或洞察…', 'Record a fact, decision or insight for this project…')}
            rows={3}
            className="mb-2 resize-none text-sm"
            autoFocus
          />
          <div className="flex items-center justify-between gap-2">
            <label className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
              {T('重要性', 'Importance')}
              <select
                value={draftImportance}
                onChange={(e) => setDraftImportance(Number(e.target.value))}
                className="rounded-md border border-border bg-background px-1.5 py-0.5 text-xs"
              >
                {[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map((n) => (
                  <option key={n} value={n}>{n}</option>
                ))}
              </select>
            </label>
            <div className="flex gap-2">
              <Button size="sm" variant="outline" onClick={() => setAdding(false)}>
                {T('取消', 'Cancel')}
              </Button>
              <Button size="sm" onClick={submitAdd} disabled={!draft.trim() || saving}>
                {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
                {T('保存', 'Save')}
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* Empty state */}
      {memories.length === 0 ? (
        <EmptyState
          icon={Brain}
          label={T(
            '该项目暂无共享记忆。点击「添加记忆」记录智能体应持续记住的事实、决策或洞察。',
            'No shared memory yet. Add a fact, decision or insight the agent should remember for this project.'
          )}
          action={canEdit ? (
            <Button size="sm" variant="outline" className="mt-2 gap-1.5" onClick={() => setAdding(true)}>
              <Plus className="h-3.5 w-3.5" />
              {T('添加记忆', 'Add Memory')}
            </Button>
          ) : null}
        />
      ) : (
        <ul className="space-y-2">
          {memories.map((m) => (
            <li key={m.id} className="rounded-lg border border-border bg-background p-3">
              {editingId === m.id ? (
                /* ── Edit mode ── */
                <div>
                  <Textarea
                    value={editDraft}
                    onChange={(e) => setEditDraft(e.target.value)}
                    rows={3}
                    className="mb-2 resize-none text-sm"
                    autoFocus
                  />
                  <div className="flex items-center justify-between gap-2">
                    <label className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
                      {T('重要性', 'Importance')}
                      <select
                        value={editImportance}
                        onChange={(e) => setEditImportance(Number(e.target.value))}
                        className="rounded-md border border-border bg-background px-1.5 py-0.5 text-xs"
                      >
                        {[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map((n) => (
                          <option key={n} value={n}>{n}</option>
                        ))}
                      </select>
                    </label>
                    <div className="flex gap-2">
                      <Button size="sm" variant="outline" onClick={() => setEditingId(null)}>
                        {T('取消', 'Cancel')}
                      </Button>
                      <Button size="sm" onClick={() => submitEdit(m)} disabled={!editDraft.trim()}>
                        <Check className="mr-1 h-3 w-3" />
                        {T('保存', 'Save')}
                      </Button>
                    </div>
                  </div>
                </div>
              ) : (
                /* ── View mode ── */
                <div>
                  <div className="flex items-start justify-between gap-2">
                    <p className="min-w-0 flex-1 whitespace-pre-wrap break-words text-sm leading-relaxed text-foreground">
                      {m.content}
                    </p>
                    {canEdit && (
                      <div className="flex shrink-0 items-center gap-0.5">
                        <button
                          type="button"
                          onClick={() => onUpdate(m.id, { pinned: !m.pinned })}
                          className={`flex h-7 w-7 items-center justify-center rounded-md transition-colors ${
                            m.pinned
                              ? 'bg-primary/10 text-primary'
                              : 'text-muted-foreground hover:bg-secondary hover:text-foreground'
                          }`}
                          title={m.pinned ? T('取消置顶', 'Unpin') : T('置顶', 'Pin')}
                          aria-label={m.pinned ? T('取消置顶', 'Unpin') : T('置顶', 'Pin')}
                        >
                          <Pin className="h-3.5 w-3.5" />
                        </button>
                        <button
                          type="button"
                          onClick={() => startEdit(m)}
                          className="flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
                          title={T('编辑', 'Edit')}
                          aria-label={T('编辑', 'Edit')}
                        >
                          <Pencil className="h-3.5 w-3.5" />
                        </button>
                        <button
                          type="button"
                          onClick={() => onDelete(m.id)}
                          className="flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-red-100 hover:text-red-500 dark:hover:bg-red-900/30"
                          title={T('删除', 'Delete')}
                          aria-label={T('删除', 'Delete')}
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      </div>
                    )}
                  </div>
                  <div className="mt-2 flex flex-wrap items-center gap-2">
                    {m.pinned && (
                      <Badge variant="outline" className="border-primary/40 text-[10px] text-primary">
                        <Pin className="mr-1 h-2.5 w-2.5" />
                        {T('已置顶', 'Pinned')}
                      </Badge>
                    )}
                    {m.importance > 0 && (
                      <Badge variant="secondary" className="text-[10px]">
                        <Star className="mr-1 h-2.5 w-2.5" />
                        {m.importance}
                      </Badge>
                    )}
                    {m.target === 'user' && (
                      <Badge variant="outline" className="text-[10px] text-muted-foreground">
                        {T('用户画像', 'Profile')}
                      </Badge>
                    )}
                    <span className="text-[10px] text-muted-foreground">
                      · {formatRelativeTime(m.updated_at || m.updated_date, lang)}
                    </span>
                  </div>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
