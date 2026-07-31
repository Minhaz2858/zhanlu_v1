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
import FilesView from '@/components/files/FilesView';
import ProjectsView from '@/components/project/ProjectsView';
import StepsAgentBuilder from '@/components/agentbuilder/StepsAgentBuilder';
import ProjectCreateDialog from '@/components/project/ProjectCreateDialog';
import { Button } from '@/components/ui/button';
import { Plus, Bot, Database, FileText, Trash2, Play, Loader2, Folder, FileEdit, FolderKanban, Archive, LayoutDashboard } from 'lucide-react';
import { hasDraft as hasDraftData, clearDraft, flushDraft, loadDraft } from '@/lib/draftManager';
import { filterUserAgents } from '@/lib/systemAgents';
import { listDashboards, deleteDashboard } from '@/api/dashboards';
import DashboardPopup from '@/components/dashboard/DashboardPopup';

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
  // Live dashboard popup (same-page overlay) opened from the Dashboards tab.
  const [openDashboardId, setOpenDashboardId] = useState(null);
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
    { key: 'file', label: t.mySpace.tabs.file, icon: FileText, entity: 'UserFile' },
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
        // Dashboards are served by the dedicated /api/dashboards endpoint
        // (not base44.entities), so fetch them directly.
        setItems(await listDashboards());
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
  function openNewKb() { setKbEdit(null); setKbOpen(true); }
  function openEditKb(item) { setKbEdit(item); setKbOpen(true); }
  async function togglePauseKb(item) {
    const updated = await base44.entities.KnowledgeBase.update(item.id, { status: item.status === 'paused' ? 'active' : 'paused' });
    setItems((prev) => prev.map((entry) => entry.id === item.id ? updated : entry));
  }
  async function reindexKb(item) {
    try {
      await authFetch(`/api/apps/${item.app_id || 'default-app'}/knowledge_bases/${item.id}/reindex`, { method: 'POST' });
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
        {tab !== 'file' && tab !== 'project' && tab !== 'dashboard' && (
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

      {tab === 'file' ? (
        <FilesView />
      ) : tab === 'project' ? (
        <ProjectsView />
      ) : tab === 'dashboard' ? (
        loading ? (
          <div className="flex justify-center py-20"><Loader2 className="h-6 w-6 animate-spin text-muted-foreground" /></div>
        ) : items.length === 0 ? (
          <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-border py-20 text-center">
            <LayoutDashboard className="mb-3 h-8 w-8 text-muted-foreground/40" />
            <p className="text-sm text-muted-foreground">{lang === 'en' ? 'No dashboards yet.' : '暂无仪表盘。'}</p>
            <p className="mt-1 text-xs text-muted-foreground">{lang === 'en' ? 'Ask an agent to build a live dashboard.' : '让智能体创建一个实时仪表盘。'}</p>
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
            {items.map((d) => (
              <div key={d.id} onClick={() => setOpenDashboardId(d.id)} className="group flex cursor-pointer flex-col rounded-xl border border-border bg-card p-5 transition-shadow hover:shadow-sm">
                <div className="mb-2 flex items-start gap-2">
                  <LayoutDashboard className="mt-0.5 h-4 w-4 text-primary" />
                  <h3 className="flex-1 font-display text-base text-foreground group-hover:text-primary">{d.name}</h3>
                </div>
                <p className="mb-3 flex-1 text-xs text-muted-foreground">{d.description || (lang === 'en' ? 'Live dashboard' : '实时仪表盘')}</p>
                <div className="mt-3 flex items-center gap-2" onClick={(e) => e.stopPropagation()}>
                  <button onClick={() => setOpenDashboardId(d.id)} className="inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1 text-xs text-foreground hover:bg-secondary"><Play className="h-3 w-3" /> {t.common.run}</button>
                  <button onClick={async () => { await deleteDashboard(d.id); load(); }} className="ml-auto inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1 text-xs text-muted-foreground hover:text-destructive"><Trash2 className="h-3 w-3" /> {t.common.delete}</button>
                </div>
              </div>
            ))}
          </div>
        )
      ) : loading ? (
        <div className="flex justify-center py-20"><Loader2 className="h-6 w-6 animate-spin text-muted-foreground" /></div>
      ) : items.length === 0 ? (
        <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-border py-20 text-center">
          <current.icon className="mb-3 h-8 w-8 text-muted-foreground/40" />
          <p className="text-sm text-muted-foreground">{t.mySpace.empty}{current.label}</p>
          <p className="mt-1 text-xs text-muted-foreground">{t.mySpace.emptyHint}</p>
        </div>
      ) : tab === 'agent' && draftInfo ? (
        // Draft banner shown above the Agent list — tells the user exactly
        // where their unfinished Agent lives.
        <>
          <div className="mb-4 flex items-center justify-between gap-3 rounded-xl border border-amber-500/30 bg-amber-500/5 px-4 py-3">
            <div className="flex min-w-0 items-center gap-3">
              <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-amber-500/15 text-amber-600 dark:text-amber-300">
                <FileEdit className="h-4 w-4" />
              </div>
              <div className="min-w-0">
                <p className="truncate text-sm font-medium text-foreground">
                  {lang === 'en' ? 'Unfinished agent draft' : '有未完成的智能体草稿'}
                </p>
                <p className="truncate text-xs text-muted-foreground">
                  {draftInfo.summary}
                  {draftInfo.hasConfig && (
                    <span className="ml-1 text-amber-600 dark:text-amber-300">
                      · {lang === 'en' ? 'in step 2 (configure)' : '在第 2 步（对话配置）'}
                    </span>
                  )}
                </p>
              </div>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              <Button variant="ghost" size="sm" onClick={discardAgentDrafts} className="text-muted-foreground">
                <Trash2 className="h-3 w-3 mr-1" />
                {lang === 'en' ? 'Discard' : '放弃'}
              </Button>
              <Button size="sm" onClick={continueAgentDraft} className="gap-1">
                {lang === 'en' ? 'Continue' : '继续'}
                <Play className="h-3 w-3" />
              </Button>
            </div>
          </div>
          {/* then the normal card grid */}
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
            {items.map((it) => (
              <div key={it.id} onClick={() => navigate(`/my-space/agent/${it.id}`)} className="group flex cursor-pointer flex-col rounded-xl border border-border bg-card p-5 transition-shadow hover:shadow-sm">
                <div className="mb-2 flex items-start gap-2">
                  <current.icon className="mt-0.5 h-4 w-4 text-primary" />
                  <h3 className="flex-1 font-display text-base text-foreground group-hover:text-primary">{translate(it.name || it.title)}</h3>
                </div>
                <p className="mb-3 flex-1 text-xs text-muted-foreground">{(it.description || it.summary) ? translate(it.description || it.summary) : '—'}</p>
                <div className="mt-3 flex items-center gap-2" onClick={(e) => e.stopPropagation()}>
                  <button onClick={() => navigate(`/?agent=${it.id}`)} className="inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1 text-xs text-foreground hover:bg-secondary"><Play className="h-3 w-3" /> {t.common.run}</button>
                  <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
                    <Folder className="h-3 w-3" />
                    {it.project && it.project !== 'global' ? it.project : t.automation.ungrouped}
                  </span>
                </div>
              </div>
            ))}
          </div>
        </>
      ) : (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
          {items.map((it) =>
            tab === 'kb' ? (
              <KbCard key={it.id} item={it} t={t} translate={translate} onClick={() => navigate(`/my-space/kb/${it.id}`)} onEdit={openEditKb} onTogglePause={togglePauseKb} onDelete={(item) => remove(item.id)} onReindex={reindexKb} />
            ) : (
              <div key={it.id} onClick={() => navigate(tab === 'agent' ? `/my-space/agent/${it.id}` : `/my-space/${tab}/${it.id}`)} className="group flex cursor-pointer flex-col rounded-xl border border-border bg-card p-5 transition-shadow hover:shadow-sm">
                <div className="mb-2 flex items-start gap-2">
                  <current.icon className="mt-0.5 h-4 w-4 text-primary" />
                  <h3 className="flex-1 font-display text-base text-foreground group-hover:text-primary">{translate(it.name || it.title)}</h3>
                </div>
                <p className="mb-3 flex-1 text-xs text-muted-foreground">{(it.description || it.summary || it.file_type) ? translate(it.description || it.summary || it.file_type) : '—'}</p>
                <div className="mt-3 flex items-center gap-2" onClick={(e) => e.stopPropagation()}>
                  <button onClick={() => { if (tab === 'agent') navigate(`/?agent=${it.id}`); }} className="inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1 text-xs text-foreground hover:bg-secondary"><Play className="h-3 w-3" /> {t.common.run}</button>
                  {tab === 'agent' ? (
                    <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
                      <Folder className="h-3 w-3" />
                      {it.project && it.project !== 'global' ? it.project : t.automation.ungrouped}
                    </span>
                  ) : (
                    <button onClick={() => remove(it.id)} className="ml-auto inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1 text-xs text-muted-foreground hover:text-destructive"><Trash2 className="h-3 w-3" /> {t.common.delete}</button>
                  )}
                </div>
              </div>
            )
          )}
        </div>
      )}

      <CreateResourceDialog open={dialogOpen} onOpenChange={setDialogOpen} resourceType={tab} />
      {tab === 'kb' && <KbSetupDialog open={kbOpen} onOpenChange={setKbOpen} editItem={kbEdit} onSaved={handleKbSaved} />}
      {openDashboardId && (
        <DashboardPopup dashboardId={openDashboardId} variant="myspace" onClose={() => setOpenDashboardId(null)} />
      )}
    </div>
  );
}