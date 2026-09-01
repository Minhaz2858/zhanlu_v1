import { useState, useEffect } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { base44 } from '@/api/base44Client';
import { useLanguage } from '@/lib/LanguageProvider';
import { useTranslate } from '@/lib/useTranslate';
import { authFetch } from '@/api/authFetch';
import PageHeader from '@/components/PageHeader';
import CreateResourceDialog from '@/components/CreateResourceDialog';
import KbSetupDialog from '@/components/kb/KbSetupDialog';
import KbCard from '@/components/kb/KbCard';
import ProjectsView from '@/components/project/ProjectsView';
import StepsAgentBuilder from '@/components/agentbuilder/StepsAgentBuilder';
import ProjectCreateDialog from '@/components/project/ProjectCreateDialog';
import { Button } from '@/components/ui/button';
import { Plus, Bot, Database, Trash2, Play, Loader2, Folder, FileEdit, FolderKanban, Archive, LayoutDashboard } from 'lucide-react';
import { hasDraft as hasDraftData, clearDraft, flushDraft, loadDraft } from '@/lib/draftManager';
import { filterUserAgents } from '@/lib/systemAgents';
import AgentsView from '@/components/agent/AgentsView';
import { listDashboards, listDashboardApps, deleteDashboard, deleteDashboardApp } from '@/api/dashboards';
import { toast } from 'sonner';

const AGENT_DRAFT_KEYS = {
  form: 'agent_steps_form',
  config: 'agent_steps_config',
};

export default function MySpace() {
  const { t, lang } = useLanguage();
  // Projects is the landing tab — the user usually opens My Space to
  // jump into a project's chat / data sources / agents. Switching
  // the default from "agent" to "project" matches that mental
  // model: the project is the container, the agents inside it are
  // just one of its resources.
  const [tab, setTab] = useState('project');
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [kbOpen, setKbOpen] = useState(false);
  const [kbEdit, setKbEdit] = useState(null);
  // Embedded Steps Agent Builder state
  const [builderOpen, setBuilderOpen] = useState(false);
  // Draft indicator (so user can see where their unfinished Agent lives)
  const [draftInfo, setDraftInfo] = useState(null);
  // Project context for "New Agent" opened from Project Detail (?initialProjectId=…).
  const [initialProjectId, setInitialProjectId] = useState(null);
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();

  // When arriving from /my-space/project/:id → "New Agent", open the embedded
  // builder pre-selecting the project. We read ?initialProjectId once and
  // strip it from the URL so a refresh doesn't reopen the builder.
  useEffect(() => {
    const pid = searchParams.get('initialProjectId');
    if (pid) {
      setInitialProjectId(pid);
      setBuilderOpen(true);
      const next = new URLSearchParams(searchParams);
      next.delete('initialProjectId');
      setSearchParams(next, { replace: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Tab order: Projects first (the landing page), then the asset
  // tabs grouped by their lifecycle: Agents (created inside a
  // project), Files (uploaded into a project), Connectors (external
  // services wired into a project). This grouping matches how the
  // user usually browses My Space — open a project, then drill into
  // the resources that live inside it.
  const TABS = [
    { key: 'project', label: t.mySpace.tabs.project || 'Projects', icon: FolderKanban, entity: 'Project' },
    { key: 'agent', label: t.mySpace.tabs.agent, icon: Bot, entity: 'AgentApp' },
    { key: 'kb', label: t.mySpace.tabs.kb, icon: Database, entity: 'KnowledgeBase' },
    { key: 'dashboard', label: t.mySpace.tabs.dashboard, icon: LayoutDashboard, entity: 'Dashboard' },
  ];
  const current = TABS.find((x) => x.key === tab);

  useEffect(() => { load(); /* eslint-disable-next-line */ }, [tab]);

  // Detect existing Agent-builder drafts so the user can see the location
  // of unfinished creations on the Agent tab (and from any tab via the
  // small badge on the "+ New Agent" button).
  useEffect(() => {
    function refreshDraft() {
      const formDraft = loadDraft(AGENT_DRAFT_KEYS.form);
      const configDraft = loadDraft(AGENT_DRAFT_KEYS.config);
      if (hasDraftData(AGENT_DRAFT_KEYS.form) || hasDraftData(AGENT_DRAFT_KEYS.config)) {
        const summary = (formDraft && (formDraft.name || formDraft.description))
          || (configDraft && configDraft.name)
          || '';
        setDraftInfo({
          hasForm: hasDraftData(AGENT_DRAFT_KEYS.form),
          hasConfig: hasDraftData(AGENT_DRAFT_KEYS.config),
          summary: summary || (lang === 'en' ? 'An unfinished agent' : '未完成的智能体'),
          updatedAt: Date.now(),
        });
      } else {
        setDraftInfo(null);
      }
    }
    refreshDraft();
    const onVis = () => refreshDraft();
    window.addEventListener('focus', onVis);
    // Listen for explicit draft updates so we can refresh the indicator
    // when the StepsAgentBuilder flushes / clears a draft.
    window.addEventListener('agent_builder_draft_changed', onVis);
    return () => {
      window.removeEventListener('focus', onVis);
      window.removeEventListener('agent_builder_draft_changed', onVis);
    };
  }, [lang, builderOpen]);

  function discardAgentDrafts() {
    flushDraft(AGENT_DRAFT_KEYS.form);
    clearDraft(AGENT_DRAFT_KEYS.form);
    flushDraft(AGENT_DRAFT_KEYS.config);
    clearDraft(AGENT_DRAFT_KEYS.config);
    window.dispatchEvent(new Event('agent_builder_draft_changed'));
  }

  function continueAgentDraft() {
    setBuilderOpen(true);
  }
  async function load() {
    setLoading(true);
    try {
      if (tab === 'dashboard') {
        // Merge BOTH dashboard kinds: legacy SQL-widget dashboards
        // (GET /api/dashboards) + full-stack realtime apps
        // (GET /api/dashboards/app-records — create_fullstack_dashboard).
        // Each item is tagged `kind` so open/delete can route correctly.
        const [legacy, apps] = await Promise.all([
          listDashboards().catch(() => []),
          listDashboardApps().catch(() => []),
        ]);
        setItems([
          ...(Array.isArray(apps) ? apps : []).map((a) => ({ ...a, kind: 'app', id: a.slug })),
          ...(Array.isArray(legacy) ? legacy : []).map((d) => ({ ...d, kind: 'legacy' })),
        ]);
        return;
      }
      const raw = await base44.entities[current.entity].list('-updated_date', 200);
      // My Space → Agents shows the USER's own agents only. Platform
      // system agents (general_assistant, agent_builder, skill_agent,
      // automation_agent, power_user, data_agent) are seeded on
      // startup and managed by the runtime — they must NOT appear
      // here or the user will think they were created by them, and
      // they cannot be deleted from this list. The runtime still
      // uses them — general_assistant is auto-selected silently for
      // any chat with no user-picked agent.
      setItems(current.entity === 'AgentApp' ? filterUserAgents(raw) : raw);
    } finally { setLoading(false); }
  }
  async function remove(id) {
    await base44.entities[current.entity].delete(id);
    load();
  }
  async function removeDashboard(item) {
    // Full-stack apps delete via DELETE /api/dashboards/app-records/{slug};
    // legacy SQL-widget dashboards via DELETE /api/dashboards/{id}.
    const label = item.name || item.title || '';
    const ok = lang === 'en'
      ? window.confirm(`Delete "${label}"? This removes the dashboard and its data.`)
      : window.confirm(`确定删除“${label}”吗？此操作将删除仪表盘及其数据。`);
    if (!ok) return;
    try {
      if (item.kind === 'app') await deleteDashboardApp(item.id);
      else await deleteDashboard(item.id);
    } catch (e) {
      toast.error(lang === 'en' ? 'Failed to delete dashboard' : '删除仪表盘失败');
    } finally {
      load();
    }
  }
  function openNewKb() { setKbEdit(null); setKbOpen(true); }
  function openEditKb(item) { setKbEdit(item); setKbOpen(true); }
  async function togglePauseKb(item) {
    const updated = await base44.entities.KnowledgeBase.update(item.id, { status: item.status === 'paused' ? 'active' : 'paused' });
    setItems((prev) => prev.map((entry) => entry.id === item.id ? updated : entry));
  }
  async function reindexKb(item) {
    try {
      const isDbKb = item.source_kind === 'database' && ['mysql','postgres','postgresql'].includes((item.db_type||'').toLowerCase());
      const endpoint = isDbKb
        ? `/api/apps/${item.app_id || 'default-app'}/knowledge_bases/${item.id}/catalog/reindex`
        : `/api/apps/${item.app_id || 'default-app'}/knowledge_bases/${item.id}/reindex`;
      await authFetch(endpoint, { method: 'POST' });
      const updated = await base44.entities.KnowledgeBase.get(item.id);
      setItems((prev) => prev.map((entry) => entry.id === item.id ? updated : entry));
    } catch { /* non-fatal */ }
  }
  function handleKbSaved(saved) {
    setItems((prev) => prev.some((item) => item.id === saved.id)
      ? prev.map((item) => item.id === saved.id ? saved : item)
      : [saved, ...prev]);
  }

  const translate = useTranslate(
    items.flatMap((it) => [it.name, it.title, it.description, it.summary, it.file_type].filter(Boolean)),
    lang
  );

  // When the embedded Steps Agent Builder is open, render it instead of the list.
  if (builderOpen) {
    return (
      <div className="h-full">
        <StepsAgentBuilder
          initialProjectId={initialProjectId}
          onClose={() => { setBuilderOpen(false); setInitialProjectId(null); load(); }}
        />
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto px-8 py-8">
      <PageHeader title={t.mySpace.title} subtitle={t.mySpace.subtitle} />

      <div className="mb-6 flex items-end justify-between gap-2 border-b border-border">
        <div className="flex flex-wrap gap-1">
          {TABS.map((x) => (
            <button key={x.key} onClick={() => setTab(x.key)} className={`-mb-px inline-flex items-center gap-1.5 border-b-2 px-3 py-2.5 text-sm transition-colors ${tab === x.key ? 'border-primary text-foreground' : 'border-transparent text-muted-foreground hover:text-foreground'}`}>
              <x.icon className="h-4 w-4" /> {x.label}
            </button>
          ))}
        </div>
        {tab !== 'project' && (
          <Button onClick={() => (tab === 'kb' ? openNewKb() : tab === 'agent' ? setBuilderOpen(true) : setDialogOpen(true))} size="sm" variant="outline" className="mb-1 shrink-0 relative">
            <Plus className="h-4 w-4" /> {t.common.new} {t.createDialog.resourceLabels[tab]}
            {tab === 'agent' && draftInfo && (
              <span className="ml-1.5 inline-flex items-center gap-1 rounded-full bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-medium text-amber-600 dark:text-amber-300" title={lang === 'en' ? 'You have an unfinished draft' : '有未完成的草稿'}>
                <FileEdit className="h-2.5 w-2.5" />
                {lang === 'en' ? 'draft' : '草稿'}
              </span>
            )}
          </Button>
        )}
      </div>

      {tab === 'project' ? (
        <ProjectsView />
      ) : tab === 'agent' ? (
        <AgentsView />
      ) : loading ? (
        <div className="flex justify-center py-20"><Loader2 className="h-6 w-6 animate-spin text-muted-foreground" /></div>
      ) : items.length === 0 ? (
        <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-border py-20 text-center">
          <current.icon className="mb-3 h-8 w-8 text-muted-foreground/40" />
          <p className="text-sm text-muted-foreground">{t.mySpace.empty}{current.label}</p>
          <p className="mt-1 text-xs text-muted-foreground">{t.mySpace.emptyHint}</p>
        </div>
      ) : tab === 'kb' ? (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
          {items.map((it) => (
            <KbCard key={it.id} item={it} t={t} translate={translate} onClick={() => navigate(`/my-space/kb/${it.id}`)} onEdit={openEditKb} onTogglePause={togglePauseKb} onDelete={(item) => remove(item.id)} onReindex={reindexKb} />
          ))}
        </div>
      ) : tab === 'dashboard' ? (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
          {items.map((d) => (
            <div
              key={d.id}
              onClick={() => navigate(`/dashboard/${d.id}`)}
              className="group cursor-pointer rounded-xl border border-border bg-card p-4 shadow-sm transition-colors hover:border-primary/40"
            >
              <div className="mb-1.5 flex items-center gap-2">
                <LayoutDashboard className="h-4 w-4 shrink-0 text-primary" />
                <h3 className="min-w-0 flex-1 truncate font-display text-sm text-foreground">{translate(d.name)}</h3>
                {d.kind === 'app' && (
                  <span className="inline-flex shrink-0 items-center gap-1 rounded-full bg-[hsl(var(--chart-2))]/10 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-[hsl(var(--chart-2))]">
                    <span className="relative flex h-1.5 w-1.5">
                      <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-[hsl(var(--chart-2))] opacity-75" />
                      <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-[hsl(var(--chart-2))]" />
                    </span>
                    {lang === 'en' ? 'LIVE' : '实时'}
                  </span>
                )}
              </div>
              <p className="mb-3 line-clamp-2 min-h-[2rem] text-xs text-muted-foreground">
                {translate(d.description) || (lang === 'en' ? 'Live dashboard' : '实时仪表盘')}
              </p>
              <div className="flex items-center gap-2" onClick={(e) => e.stopPropagation()}>
                <button
                  onClick={() => navigate(`/dashboard/${d.id}`)}
                  className="inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1 text-xs transition-colors hover:bg-secondary"
                >
                  <Play className="h-3 w-3" /> {t.common.run}
                </button>
                <button
                  onClick={() => removeDashboard(d)}
                  className="ml-auto inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1 text-xs text-muted-foreground transition-colors hover:text-destructive"
                >
                  <Trash2 className="h-3 w-3" /> {t.common.delete}
                </button>
              </div>
            </div>
          ))}
        </div>
      ) : null}

      <CreateResourceDialog open={dialogOpen} onOpenChange={setDialogOpen} resourceType={tab} />
      {tab === 'kb' && <KbSetupDialog open={kbOpen} onOpenChange={setKbOpen} editItem={kbEdit} onSaved={handleKbSaved} />}
    </div>
  );
}