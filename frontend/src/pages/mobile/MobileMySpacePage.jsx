import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { base44 } from '@/api/base44Client';
import { useLanguage } from '@/lib/LanguageProvider';
import { useTranslate } from '@/lib/useTranslate';
import { FolderKanban, Bot, FileText, Database, LayoutDashboard, Plus, Loader2, Trash2, Play } from 'lucide-react';
import MobileTopBar from '@/components/mobile/MobileTopBar';
import ProjectsView from '@/components/project/ProjectsView';
import FilesView from '@/components/files/FilesView';
import KbCard from '@/components/kb/KbCard';
import { filterUserAgents } from '@/lib/systemAgents';
import { listDashboards, listDashboardApps, deleteDashboard } from '@/api/dashboards';
import CreateResourceDialog from '@/components/CreateResourceDialog';
import KbSetupDialog from '@/components/kb/KbSetupDialog';
import DashboardPopup from '@/components/dashboard/DashboardPopup';

/**
 * MobileMySpacePage — mobile-friendly "我的空间".
 *
 * Reuses the same top tabs as the desktop MySpace but renders them as a
 * horizontally scrollable strip (per the mobile plan). The heavy content
 * views (ProjectsView, FilesView, KbCard, dashboards) are the SAME
 * components the desktop MySpace uses, so business logic is not duplicated.
 */
export default function MobileMySpacePage() {
  const { t, lang } = useLanguage();
  const navigate = useNavigate();
  const [tab, setTab] = useState('project');
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [kbOpen, setKbOpen] = useState(false);
  const [kbEdit, setKbEdit] = useState(null);
  const [openDashboardId, setOpenDashboardId] = useState(null);

  const TABS = [
    { key: 'project', label: t.mySpace.tabs.project || 'Projects', icon: FolderKanban, entity: 'Project' },
    { key: 'agent', label: t.mySpace.tabs.agent, icon: Bot, entity: 'AgentApp' },
    { key: 'file', label: t.mySpace.tabs.file, icon: FileText, entity: 'UserFile' },
    { key: 'kb', label: t.mySpace.tabs.kb, icon: Database, entity: 'KnowledgeBase' },
    { key: 'dashboard', label: t.mySpace.tabs.dashboard, icon: LayoutDashboard, entity: 'Dashboard' },
  ];

  async function load() {
    setLoading(true);
    try {
      if (tab === 'dashboard') {
        // Merge legacy SQL-widget dashboards + full-stack realtime apps so
        // mobile shows the same surface as desktop. App records navigate to
        // the full DashboardView page (DashboardPopup is legacy-only).
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
      const current = TABS.find((x) => x.key === tab);
      const raw = await base44.entities[current.entity].list('-updated_date', 200);
      setItems(current.entity === 'AgentApp' ? filterUserAgents(raw) : raw);
    } finally { setLoading(false); }
  }

  useEffect(() => { load(); }, [tab]);

  async function remove(id) {
    await base44.entities[TABS.find((x) => x.key === tab).entity].delete(id);
    load();
  }

  async function togglePauseKb(item) {
    const updated = await base44.entities.KnowledgeBase.update(item.id, { status: item.status === 'paused' ? 'active' : 'paused' });
    setItems((prev) => prev.map((entry) => entry.id === item.id ? updated : entry));
  }

  async function reindexKb(item) {
    try {
      await base44.entities.KnowledgeBase.get(item.id);
      const updated = await base44.entities.KnowledgeBase.get(item.id);
      setItems((prev) => prev.map((entry) => entry.id === item.id ? updated : entry));
    } catch { /* non-fatal */ }
  }

  const translate = useTranslate(
    items.flatMap((it) => [it.name, it.title, it.description, it.summary, it.file_type].filter(Boolean)),
    lang
  );

  return (
    <div className="flex h-full flex-col bg-background">
      <MobileTopBar title={t.mySpace.title} showNewChat={false} />

      {/* Horizontally scrollable tab strip */}
      <div className="flex shrink-0 items-center gap-1 overflow-x-auto border-b border-border px-2 py-1.5">
        {TABS.map((x) => (
          <button
            key={x.key}
            onClick={() => setTab(x.key)}
            className={`inline-flex shrink-0 items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm transition-colors ${
              tab === x.key ? 'bg-primary/10 text-primary' : 'text-muted-foreground hover:text-foreground'
            }`}
          >
            <x.icon className="h-4 w-4" /> {x.label}
          </button>
        ))}
        {tab !== 'file' && tab !== 'project' && (
          <button
            onClick={() => (tab === 'kb' ? (setKbEdit(null), setKbOpen(true)) : setDialogOpen(true))}
            className="ml-auto inline-flex shrink-0 items-center gap-1 rounded-lg bg-primary px-2.5 py-1.5 text-xs font-medium text-primary-foreground"
          >
            <Plus className="h-3.5 w-3.5" />
          </button>
        )}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {tab === 'file' ? (
          <FilesView />
        ) : tab === 'project' ? (
          <ProjectsView />
        ) : loading ? (
          <div className="flex justify-center py-16"><Loader2 className="h-6 w-6 animate-spin text-muted-foreground" /></div>
        ) : items.length === 0 ? (
          <div className="flex flex-col items-center justify-center px-6 py-16 text-center">
            <div className="mb-3 h-10 w-10 text-muted-foreground/40">
              {(() => { const C = TABS.find((x) => x.key === tab).icon; return <C className="h-10 w-10" />; })()}
            </div>
            <p className="text-sm text-muted-foreground">{t.mySpace.empty}{TABS.find((x) => x.key === tab).label}</p>
          </div>
        ) : tab === 'dashboard' ? (
          <div className="grid grid-cols-1 gap-3 p-3">
            {items.map((d) => (
              <div key={d.id} onClick={() => (d.kind === 'app' ? navigate(`/dashboard/${d.id}`) : setOpenDashboardId(d.id))} className="cursor-pointer rounded-xl border border-border bg-card p-4">
                <div className="mb-1 flex items-center gap-2">
                  <LayoutDashboard className="h-4 w-4 text-primary" />
                  <h3 className="flex-1 truncate font-display text-base text-foreground">{d.name}</h3>
                  {d.kind === 'app' && (
                    <span className="inline-flex shrink-0 items-center gap-1 rounded-full bg-[hsl(var(--chart-2))]/10 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-[hsl(var(--chart-2))]">
                      {lang === 'en' ? 'LIVE' : '实时'}
                    </span>
                  )}
                </div>
                <p className="mb-2 text-xs text-muted-foreground">{d.description || (lang === 'en' ? 'Live dashboard' : '实时仪表盘')}</p>
                <div className="flex items-center gap-2" onClick={(e) => e.stopPropagation()}>
                  <button onClick={() => (d.kind === 'app' ? navigate(`/dashboard/${d.id}`) : setOpenDashboardId(d.id))} className="inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1 text-xs"><Play className="h-3 w-3" /> {t.common.run}</button>
                  {d.kind !== 'app' && (
                    <button onClick={async () => { await deleteDashboard(d.id); load(); }} className="ml-auto inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1 text-xs text-muted-foreground"><Trash2 className="h-3 w-3" /> {t.common.delete}</button>
                  )}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-3 p-3">
            {items.map((it) =>
              tab === 'kb' ? (
                <KbCard key={it.id} item={it} t={t} translate={translate} onClick={() => navigate(`/my-space/kb/${it.id}`)} onEdit={(item) => { setKbEdit(item); setKbOpen(true); }} onTogglePause={togglePauseKb} onDelete={(item) => remove(item.id)} onReindex={reindexKb} />
              ) : (
                <div key={it.id} onClick={() => navigate(tab === 'agent' ? `/my-space/agent/${it.id}` : `/my-space/${tab}/${it.id}`)} className="cursor-pointer rounded-xl border border-border bg-card p-4">
                  <div className="mb-1 flex items-start gap-2">
                    {(() => { const C = TABS.find((x) => x.key === tab).icon; return <C className="mt-0.5 h-4 w-4 text-primary" />; })()}
                    <h3 className="flex-1 truncate font-display text-base text-foreground">{translate(it.name || it.title)}</h3>
                  </div>
                  <p className="mb-2 line-clamp-2 text-xs text-muted-foreground">{(it.description || it.summary || it.file_type) ? translate(it.description || it.summary || it.file_type) : '—'}</p>
                  <div className="flex items-center gap-2" onClick={(e) => e.stopPropagation()}>
                    <button onClick={() => { if (tab === 'agent') navigate(`/?agent=${it.id}`); }} className="inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1 text-xs"><Play className="h-3 w-3" /> {t.common.run}</button>
                    {tab !== 'agent' && (
                      <button onClick={() => remove(it.id)} className="ml-auto inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1 text-xs text-muted-foreground"><Trash2 className="h-3 w-3" /> {t.common.delete}</button>
                    )}
                  </div>
                </div>
              )
            )}
          </div>
        )}
      </div>

      <CreateResourceDialog open={dialogOpen} onOpenChange={setDialogOpen} resourceType={tab} />
      {tab === 'kb' && <KbSetupDialog open={kbOpen} onOpenChange={setKbOpen} editItem={kbEdit} onSaved={() => load()} />}
      {openDashboardId && (
        <DashboardPopup dashboardId={openDashboardId} variant="myspace" onClose={() => setOpenDashboardId(null)} />
      )}
    </div>
  );
}
