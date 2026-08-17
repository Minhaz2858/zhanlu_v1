import { useState, useEffect, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { base44 } from '@/api/base44Client';
import { useLanguage } from '@/lib/LanguageProvider';
import { useAuth } from '@/lib/AuthContext';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { toast } from '@/components/ui/use-toast';
import {
  Dialog, DialogContent, DialogHeader, DialogTitle,
  DialogDescription, DialogFooter,
} from '@/components/ui/dialog';
import ProjectCreateDialog from '@/components/project/ProjectCreateDialog';
import ResourceAccessPolicyDialog from '@/components/ResourceAccessPolicyDialog';
import {
  Folder, Plus, Loader2, Pencil, FolderKanban, Archive,
  ArchiveRestore, AlertTriangle, Bot, Database, FileText, Search, ArrowUpDown,
  Sparkles, LayoutGrid, Clock, ArrowDownAZ, Building2, ShieldCheck,
} from 'lucide-react';

/**
 * ProjectsView — content for the "Projects" tab in MySpace.
 *
 * Design goals:
 *   - Modern, corporate-grade aesthetic (Linear / Notion / Vercel style)
 *   - Clear visual hierarchy: name → description → metrics
 *   - Subtle hover lift + border color shift on each card
 *   - Fully responsive: 1 column on mobile, 2 on tablet, 3 on desktop
 *   - Searchable + sortable project list
 *   - Empty state with clear value proposition
 *   - Accessible: ARIA labels, focus rings, keyboard nav
 *   - Subtle transitions (200ms) — no jarring movement
 *
 * Behaviour:
 *   - Fetches all Projects (active + archived) in this workspace
 *   - By default, hides archived projects. A "Show archived" toggle
 *     surfaces them at the bottom of the grid (so users can find and
 *     un-archive if they want to). Archived projects render with a
 *     muted "Archived" badge and a single "Unarchive" action — the
 *     trash icon is reserved for active projects only.
 *   - Search filters by name / description
 *   - Sort: "Recent" (default), "Name (A→Z)", "Most agents"
 *   - Card click navigates to /my-space/project/:id
 *   - Per-card actions open rename prompt / archive / unarchive dialogs
 *     and are always visible (not hover-gated) so they are discoverable
 *     on touch devices and via keyboard nav alike.
 */

// Color logic was removed: the user asked for a simple, color-free
// project list. The ``color`` field on the Project model is still
// present in the backend (for backward compatibility with any rows
// that have it set) but the UI no longer reads it, sends it, or
// surfaces a picker. If a row does have a ``color``, it's simply
// ignored here.

import { formatRelativeTime } from '@/lib/time';

export default function ProjectsView({ scope = null }) {
  const { t, lang } = useLanguage();
  const navigate = useNavigate();
  const { user, isAdmin } = useAuth();
  const isEn = lang === 'en';

  const [projects, setProjects] = useState([]);
  const [counts, setCounts] = useState({});
  const [loading, setLoading] = useState(true);
  const [createOpen, setCreateOpen] = useState(false);
  const [archiveTarget, setArchiveTarget] = useState(null);
  const [archiveBusy, setArchiveBusy] = useState(false);
  const [policyTarget, setPolicyTarget] = useState(null);

  // Only owners (or admins) can configure per-user data access.
  const canManageAccess = (p) => !!p && (isAdmin || p.created_by_id === user?.id);
  const openAccessPolicy = (p) => setPolicyTarget(p);

  // Search + sort state. Search is a live filter on name/description;
  // sort is one of "recent" | "name" | "agents".
  const [query, setQuery] = useState('');
  const [sort, setSort] = useState('recent');

  // Archived toggle. Default is to HIDE archived projects from the
  // grid — archived projects are kept in the DB for soft-recovery
  // but should not clutter the user's active workspace view. The
  // toggle is exposed in the toolbar (only when archived rows exist)
  // so users who need to un-archive or inspect them can still do so.
  const [showArchived, setShowArchived] = useState(false);

  useEffect(() => { load(); }, []);

  async function load() {
    setLoading(true);
    try {
      // Load ALL projects (active + archived).
      // resource_type + is_shared_with_me annotations power the COMPANY
      // vs MY split below. Use the base44 SDK so appId + JWT are
      // injected automatically (same path as SessionList.jsx:93).
      let list = await base44.entities.Project.list('-updated_date', 200);
      if (!Array.isArray(list)) list = list.data || list.items || [];
      setProjects(list || []);
      loadCounts(list || []);
    } finally {
      setLoading(false);
    }
  }

  // Per-project counts (agents, KBs, files). Filtered by project_id
  // (the new FK column) with a graceful fallback to the legacy
  // `project` name string for older backends.
  async function loadCounts(projs) {
    const next = {};
    await Promise.all(projs.map(async (p) => {
      try {
        const [agents, kbs, files] = await Promise.all([
          base44.entities.AgentApp.filter({ project_id: p.id }, '-updated_date', 1),
          base44.entities.KnowledgeBase.filter({ project_id: p.id }, '-updated_date', 1),
          base44.entities.UserFile.filter({ project_id: p.id }, '-updated_date', 1),
        ]);
        next[p.id] = { agents: agents.length, kb: kbs.length, files: files.length };
      } catch {
        try {
          const [agents, kbs, files] = await Promise.all([
            base44.entities.AgentApp.filter({ project: p.name }, '-updated_date', 1),
            base44.entities.KnowledgeBase.filter({ project: p.name }, '-updated_date', 1),
            base44.entities.UserFile.filter({ project: p.name }, '-updated_date', 1),
          ]);
          next[p.id] = { agents: agents.length, kb: kbs.length, files: files.length };
        } catch {
          next[p.id] = { agents: 0, kb: 0, files: 0 };
        }
      }
    }));
    setCounts(next);
  }

  function openArchive(p) { setArchiveTarget(p); }
  async function confirmArchive() {
    if (!archiveTarget) return;
    setArchiveBusy(true);
    try {
      await base44.entities.Project.update(archiveTarget.id, { status: 'archived' });
      // Flip the cached status locally so the card immediately switches
      // to its archived presentation (muted + Unarchive action) IF the
      // user has "Show archived" on. If archived rows are hidden, the
      // card just disappears from the grid on the next render.
      setProjects((prev) =>
        prev.map((x) => (x.id === archiveTarget.id ? { ...x, status: 'archived' } : x))
      );
      toast({
        title: isEn ? 'Project archived' : '项目已归档',
        description: archiveTarget.name,
      });
      setArchiveTarget(null);
    } catch (e) {
      console.error('archive failed:', e);
      toast({
        title: isEn ? 'Archive failed' : '归档失败',
        description: e?.message || String(e),
        variant: 'destructive',
      });
    } finally {
      setArchiveBusy(false);
    }
  }

  // Unarchive an already-archived project. No confirmation dialog —
  // this is a low-risk reversible action (sets status back to 'active')
  // and is only reachable from the archived view, so a single click
  // returning the project to the active list matches user intent.
  async function unarchive(p) {
    try {
      const updated = await base44.entities.Project.update(p.id, { status: 'active' });
      setProjects((prev) => prev.map((x) => (x.id === p.id ? (updated || { ...x, status: 'active' }) : x)));
      toast({
        title: isEn ? 'Project restored' : '项目已恢复',
        description: p.name,
      });
    } catch (e) {
      console.error('unarchive failed:', e);
      toast({
        title: isEn ? 'Unarchive failed' : '恢复失败',
        description: e?.message || String(e),
        variant: 'destructive',
      });
    }
  }

  async function rename(p) {
    const next = window.prompt(isEn ? 'Rename project' : '重命名项目', p.name || '');
    if (!next || next === p.name) return;
    try {
      const updated = await base44.entities.Project.update(p.id, { name: next.trim() });
      setProjects((prev) => prev.map((x) => (x.id === p.id ? updated : x)));
      toast({ title: isEn ? 'Project renamed' : '项目已重命名', description: next.trim() });
    } catch (e) {
      console.error('rename failed:', e);
      toast({
        title: isEn ? 'Rename failed' : '重命名失败',
        description: e?.message || String(e),
        variant: 'destructive',
      });
    }
  }

  // Derived list — apply the archived filter, then search, then sort.
// Archived projects are hidden by default; if `showArchived` is on they
// appear at the bottom of the grid regardless of sort so the active
// workspace is the first thing the user sees.
  const visible = useMemo(() => {
    let out = (projects || []).filter((p) => {
      // Always show active; only show archived when the toggle is on.
      if (p.status === 'archived' && !showArchived) return false;
      return true;
    });
    const q = query.trim().toLowerCase();
    if (q) {
      out = out.filter((p) => {
        const hay = `${p.name || ''}\n${p.description || ''}`.toLowerCase();
        return hay.includes(q);
      });
    }
    const rank = (p) => (p.status === 'archived' ? 1 : 0);
    const sorters = {
      recent: (a, b) => new Date(b.updated_date || 0) - new Date(a.updated_date || 0),
      name: (a, b) => (a.name || '').localeCompare(b.name || '', undefined, { sensitivity: 'base' }),
      agents: (a, b) => (counts[b.id]?.agents || 0) - (counts[a.id]?.agents || 0),
    };
    out.sort((a, b) => {
      const r = rank(a) - rank(b);
      if (r !== 0) return r;
      return (sorters[sort] || sorters.recent)(a, b);
    });
    return out;
  }, [projects, counts, query, sort, showArchived]);

  // ── COMPANY vs MY split ──
  const companyProjects = useMemo(
    () => visible.filter((p) => p.resource_type === 'company' || p.is_shared_with_me),
    [visible]
  );
  const myProjects = useMemo(
    () => visible.filter((p) => !(p.resource_type === 'company' || p.is_shared_with_me)),
    [visible]
  );

  // True when there is at least one archived row in the loaded set —
  // the toolbar toggle only renders when it's actionable.
  const hasArchived = (projects || []).some((p) => p.status === 'archived');
  const activeCount = (projects || []).filter((p) => p.status !== 'archived').length;
  const archivedCount = (projects || []).length - activeCount;

  // Aggregate stats for the header strip — used to communicate
  // workspace scale at a glance ("9 projects · 23 agents · 8 sources").
  // We sum counts across active projects only, so the header reflects
  // the workspace the user actually sees by default.
  const totals = useMemo(() => {
    let agents = 0, kb = 0, files = 0;
    const activeIds = new Set(
      (projects || []).filter((p) => p.status !== 'archived').map((p) => p.id)
    );
    for (const [id, c] of Object.entries(counts)) {
      if (!activeIds.has(id)) continue;
      agents += c.agents || 0;
      kb += c.kb || 0;
      files += c.files || 0;
    }
    return { agents, kb, files, projects: activeCount };
  }, [counts, projects, activeCount]);

  return (
    <div className="space-y-6">
      {/* ───── Page header ───── */}
      <header className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div className="min-w-0">
          <div className="flex items-center gap-2.5">
            <h2 className="font-display text-xl font-semibold tracking-tight text-foreground">
              {isEn ? 'Projects' : '项目'}
            </h2>
            {!loading && (
              <span className="inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-secondary px-1.5 text-[11px] font-medium text-muted-foreground">
                {/* Counter shows active projects (not the total). The
                    "Show archived" toggle is the way users see archived
                    rows. Showing the total here would contradict the
                    hide-by-default behaviour and confuse the user. */}
                {activeCount}
              </span>
            )}
          </div>
          <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
            {isEn
              ? 'Group every agent, data source, file and conversation around a single goal. Anything you put in a project is automatically shared with the agents that live there.'
              : '将 Agent、数据源、文件与对话归类到同一目标下。项目内的资源会自动对项目里的所有 Agent 生效。'}
          </p>
        </div>
      </header>

      {/* ───── Toolbar (search + sort + archived toggle) ───── */}
      {!loading && projects.length > 0 && (
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <div className="relative w-full sm:max-w-xs">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={isEn ? 'Search projects…' : '搜索项目…'}
              className="h-9 pl-9 text-sm"
              aria-label={isEn ? 'Search projects' : '搜索项目'}
            />
          </div>
          <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <ArrowUpDown className="h-3.5 w-3.5" />
            <span className="hidden sm:inline">{isEn ? 'Sort:' : '排序：'}</span>
            <SortPill current={sort} onChange={setSort} isEn={isEn} />
          </div>
          {hasArchived && (
            <button
              type="button"
              onClick={() => setShowArchived((v) => !v)}
              aria-pressed={showArchived}
              className={`inline-flex h-9 items-center gap-1.5 rounded-md border px-2.5 text-xs transition-colors duration-150 ${
                showArchived
                  ? 'border-primary/40 bg-primary/10 text-primary'
                  : 'border-border/60 bg-card text-muted-foreground hover:bg-secondary hover:text-foreground'
              }`}
            >
              <Archive className="h-3.5 w-3.5" />
              <span>
                {isEn
                  ? (showArchived ? 'Hide archived' : `Show archived (${archivedCount})`)
                  : (showArchived ? '隐藏已归档' : `显示已归档 (${archivedCount})`)}
              </span>
            </button>
          )}
        </div>
      )}

      {/* ───── Content ───── */}
      {loading ? (
        <div className="flex justify-center py-24">
          <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
        </div>
      ) : projects.length === 0 ? (
        <EmptyState
          isEn={isEn}
          onCreate={() => setCreateOpen(true)}
        />
      ) : visible.length === 0 ? (
        <NoResults query={query} isEn={isEn} onClear={() => setQuery('')} />
      ) : (
        <div className="space-y-8">
          {/* ── COMPANY PROJECTS ── */}
          {scope !== 'personal' && (
            <>
              <SectionHeader
                icon={Building2}
                title={isEn ? 'Company Projects' : '公司项目'}
                badge={isEn ? 'Admin configured' : '管理员配置'}
              />
              {companyProjects.length === 0 ? (
                <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-border py-10 text-center">
                  <FolderKanban className="mb-3 h-8 w-8 text-muted-foreground/40" />
                  <p className="text-sm text-muted-foreground">
                    {isEn ? 'No company projects assigned yet.' : '暂无公司分配的项目。'}
                  </p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {isEn ? 'Your admin can assign shared projects for you to use.' : '管理员可以为您分配共享项目。'}
                  </p>
                </div>
              ) : (
                <ProjectGrid
                  projects={companyProjects}
                  counts={counts}
                  lang={lang}
                  isEn={isEn}
                  formatRelativeTime={formatRelativeTime}
                  navigate={navigate}
                  rename={rename}
                  unarchive={unarchive}
                  openArchive={openArchive}
                  canManageAccess={canManageAccess}
                  onManageAccess={openAccessPolicy}
                />
              )}
            </>
          )}

          {/* ── MY PROJECTS ── */}
          {scope !== 'company' && (
            <>
              <div className="flex items-center justify-between">
                <SectionHeader
                  icon={FolderKanban}
                  title={isEn ? 'My Projects' : '我的项目'}
                  badge={isEn ? 'Created by me' : '个人创建'}
                />
                <Button size="sm" onClick={() => setCreateOpen(true)} className="gap-1.5">
                  <Plus className="h-3.5 w-3.5" />
                  {isEn ? 'New Project' : '新建项目'}
                </Button>
              </div>
              {myProjects.length === 0 ? (
                <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-border py-10 text-center">
                  <FolderKanban className="mb-3 h-8 w-8 text-muted-foreground/40" />
                  <p className="text-sm text-muted-foreground">
                    {isEn ? 'No personal projects yet.' : '暂无个人项目。'}
                  </p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {isEn ? 'Create your first project to get started.' : '创建您的第一个项目开始使用。'}
                  </p>
                </div>
              ) : (
                <ProjectGrid
                  projects={myProjects}
                  counts={counts}
                  lang={lang}
                  isEn={isEn}
                  formatRelativeTime={formatRelativeTime}
                  navigate={navigate}
                  rename={rename}
                  unarchive={unarchive}
                  openArchive={openArchive}
                  canManageAccess={canManageAccess}
                  onManageAccess={openAccessPolicy}
                />
              )}
            </>
          )}
        </div>
      )}

      <ProjectCreateDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        onCreated={(p) => {
          setProjects((prev) => [p, ...prev]);
          setCounts((prev) => ({ ...prev, [p.id]: { agents: 0, kb: 0, files: 0 } }));
        }}
      />

      {/* Archive confirmation */}
      <Dialog
        open={!!archiveTarget}
        onOpenChange={(open) => { if (!open && !archiveBusy) setArchiveTarget(null); }}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2 text-base">
              <span className="flex h-9 w-9 items-center justify-center rounded-full bg-amber-500/10 text-amber-600 dark:text-amber-400">
                <AlertTriangle className="h-4 w-4" />
              </span>
              {isEn ? 'Archive this project?' : '归档此项目？'}
            </DialogTitle>
            <DialogDescription className="pt-1">
              {isEn ? (
                <>
                  "<b>{archiveTarget?.name}</b>" will be hidden from the project list. Its agents, data sources and files are kept — you can un-archive later by setting status back to <code className="rounded bg-secondary px-1 py-0.5 text-[10px]">active</code>.
                </>
              ) : (
                <>
                  "<b>{archiveTarget?.name}</b>" 将从项目列表中隐藏。其下的 Agent、数据源与文件会保留，后续可重新设为 <code className="rounded bg-secondary px-1 py-0.5 text-[10px]">active</code> 以恢复。
                </>
              )}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter className="gap-2">
            <Button
              variant="outline"
              onClick={() => setArchiveTarget(null)}
              disabled={archiveBusy}
            >
              {isEn ? 'Cancel' : '取消'}
            </Button>
            <Button
              variant="destructive"
              onClick={confirmArchive}
              disabled={archiveBusy}
              className="gap-1.5"
            >
              {archiveBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Archive className="h-3.5 w-3.5" />}
              {isEn ? 'Archive' : '归档'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Data access policy dialog (owner/admin only) */}
      {policyTarget && (
        <ResourceAccessPolicyDialog
          open={!!policyTarget}
          resourceType="project"
          resourceId={policyTarget.id}
          resourceName={policyTarget.name}
          onClose={() => setPolicyTarget(null)}
        />
      )}
    </div>
  );
}

// ─────────────────────────── Sub-components ───────────────────────────

/**
 * ProjectCard — the redesigned project card.
 *
 * Layout (top to bottom):
 *   1. Color accent bar (h-1, gradient — gives the card identity)
 *   2. Header row: large icon | name + status | actions
 *   3. Description (2-line clamp)
 *   4. Metrics row: agents | data sources | files (divided, subtle bg)
 *
 * Interactions:
 *   - Whole card is a button (keyboard accessible)
 *   - Hover: -translate-y-0.5, shadow-lg, border color shift
 *   - Focus-visible: ring-2 ring-primary/40
 *   - Per-card actions stop propagation so the card's open handler
 *     doesn't fire when the user clicks rename / archive
 */
function ProjectCard({
  project, counts, isArchived, isEn, relativeTime, onOpen, onRename, onArchive,
  onManageAccess,
}) {
  const total = counts.agents + counts.kb + counts.files;
  return (
    <li>
      <article
        onClick={onOpen}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            onOpen();
          }
        }}
        tabIndex={0}
        role="button"
        aria-label={`${isEn ? 'Open project' : '打开项目'} ${project.name}`}
        className={`group relative flex h-full cursor-pointer flex-col overflow-hidden rounded-xl border bg-card text-left outline-none transition-all duration-200 hover:-translate-y-0.5 hover:border-border hover:shadow-lg focus-visible:-translate-y-0.5 focus-visible:border-border focus-visible:shadow-lg focus-visible:ring-2 focus-visible:ring-primary/40 ${
          isArchived ? 'border-border/40 opacity-80' : 'border-border/60'
        }`}
      >
        <div className="flex flex-1 flex-col p-5">
          {/* ── Header: icon + name + meta + actions ── */}
          <div className="mb-3 flex items-start gap-3">
            <div
              className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-secondary text-muted-foreground transition-transform duration-200 group-hover:scale-105"
              aria-hidden="true"
            >
              <Folder className="h-5 w-5" />
            </div>
            <div className="min-w-0 flex-1">
              <h3
                className="truncate font-display text-base font-semibold tracking-tight text-foreground transition-colors duration-200 group-hover:text-primary"
                title={project.name}
              >
                {project.name}
              </h3>
              <p className="mt-0.5 flex items-center gap-1.5 text-[11px] uppercase tracking-wide text-muted-foreground">
                {isArchived ? (
                  <>
                    <span className="inline-block h-1.5 w-1.5 rounded-full bg-muted-foreground/60" />
                    {isEn ? 'Archived' : '已归档'}
                  </>
                ) : (
                  <>
                    <span className="inline-block h-1.5 w-1.5 rounded-full bg-emerald-500" />
                    {isEn ? 'Active' : '活跃'}
                    {relativeTime && <span className="normal-case tracking-normal">· {relativeTime}</span>}
                  </>
                )}
              </p>
            </div>
            {/* Action buttons. Always visible (not hover-gated) so
                they are discoverable on touch devices and via keyboard
                nav alike. Archived cards expose a single "Unarchive"
                button in place of Rename + Archive, so the trash icon
                is never shown on a row that is already archived (the
                original report). */}
            <div className="flex shrink-0 gap-0.5">
              {isArchived ? (
                <IconAction
                  onClick={(e) => { e.stopPropagation(); onArchive && onArchive(e); }}
                  label={isEn ? 'Unarchive' : '恢复'}
                  icon={<ArchiveRestore className="h-3.5 w-3.5" />}
                />
              ) : (
                <>
                  {onRename && (
                    <IconAction
                      onClick={(e) => { e.stopPropagation(); onRename(e); }}
                      label={isEn ? 'Rename' : '重命名'}
                      icon={<Pencil className="h-3.5 w-3.5" />}
                    />
                  )}
                  {onManageAccess && (
                    <IconAction
                      onClick={onManageAccess}
                      label={isEn ? 'Manage access' : '数据访问'}
                      icon={<ShieldCheck className="h-3.5 w-3.5" />}
                    />
                  )}
                  <IconAction
                    onClick={(e) => { e.stopPropagation(); onArchive && onArchive(e); }}
                    label={isEn ? 'Archive' : '归档'}
                    icon={<Archive className="h-3.5 w-3.5" />}
                    danger
                  />
                </>
              )}
            </div>
          </div>

          {/* ── Description ── */}
          <p className="mb-4 line-clamp-2 min-h-[2.5rem] text-sm leading-relaxed text-muted-foreground">
            {project.description || (
              <span className="italic text-muted-foreground/60">
                {isEn ? 'No description yet — click to add one.' : '暂无描述——点击项目添加。'}
              </span>
            )}
          </p>

          {/* ── Metrics footer ── */}
          <div className="mt-auto flex divide-x divide-border/80 overflow-hidden rounded-lg border border-border/60 bg-secondary/30 text-xs">
            <Metric icon={Bot} value={counts.agents} label={isEn ? 'agents' : '个 Agent'} color="text-violet-600 dark:text-violet-300" />
            <Metric icon={Database} value={counts.kb} label={isEn ? 'sources' : '数据源'} color="text-sky-600 dark:text-sky-300" />
            <Metric icon={FileText} value={counts.files} label={isEn ? 'files' : '文件'} color="text-emerald-600 dark:text-emerald-300" />
          </div>

          {/* ── Footer micro-hint — only when the project is empty,
              gives the user a clear next-action cue ── */}
          {total === 0 && (
            <p className="mt-2 text-center text-[11px] text-muted-foreground/70">
              {isEn ? 'Empty project — open to add resources' : '空白项目 — 进入添加资源'}
            </p>
          )}
        </div>
      </article>
    </li>
  );
}

/**
 * Metric — one cell of the metrics row. Subtle color accent on the
 * icon makes the row scannable at a glance, even when all values
 * are zero.
 */
function Metric({ icon: Icon, value, label, color }) {
  return (
    <div className="flex flex-1 items-center justify-center gap-1.5 px-2 py-2">
      <Icon className={`h-3.5 w-3.5 shrink-0 ${color}`} aria-hidden="true" />
      <span className="font-semibold tabular-nums text-foreground">{value}</span>
      <span className="truncate text-muted-foreground">{label}</span>
    </div>
  );
}

/**
 * IconAction — a small ghost button used for the per-card rename /
 * archive / unarchive actions. Always visible (not hover-gated) so it
 * is discoverable on touch devices. Sets its own focus ring (ring-1)
 * for keyboard nav even when nested inside a focusable card.
 */
function IconAction({ onClick, label, icon, danger }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      title={label}
      className={`flex h-7 w-7 items-center justify-center rounded-md bg-card/60 text-muted-foreground ring-1 ring-border/60 transition-all duration-200 hover:scale-105 hover:bg-secondary hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50 ${
        danger ? 'hover:text-red-500 dark:hover:text-red-400' : ''
      }`}
    >
      {icon}
    </button>
  );
}

/**
 * SortPill — segmented control for the sort dropdown. Three options,
 * no extra dialog. Active option gets a primary background.
 */
function SortPill({ current, onChange, isEn }) {
  const opts = [
    { key: 'recent', label: isEn ? 'Recent' : '最新', icon: Clock },
    { key: 'name', label: isEn ? 'Name' : '名称', icon: ArrowDownAZ },
    { key: 'agents', label: isEn ? 'Most agents' : 'Agent 最多', icon: Bot },
  ];
  return (
    <div className="inline-flex items-center gap-0.5 rounded-md border border-border/60 bg-card p-0.5 text-xs">
      {opts.map((o) => {
        const Active = current === o.key;
        return (
          <button
            key={o.key}
            type="button"
            onClick={() => onChange(o.key)}
            aria-pressed={Active}
            className={`inline-flex items-center gap-1 rounded px-2 py-1 transition-colors duration-150 ${
              Active
                ? 'bg-primary/10 text-primary'
                : 'text-muted-foreground hover:bg-secondary hover:text-foreground'
            }`}
          >
            <o.icon className="h-3 w-3" />
            <span className="hidden sm:inline">{o.label}</span>
          </button>
        );
      })}
    </div>
  );
}

/**
 * EmptyState — used when the user has zero projects. Communicates
 * the value of a project in three short bullets, then a single
 * prominent CTA. Sized to feel inviting, not empty.
 */
function EmptyState({ isEn, onCreate }) {
  return (
    <div className="flex flex-col items-center justify-center rounded-2xl border border-dashed border-border/80 bg-card/50 px-6 py-16 text-center">
      <div className="relative mb-5 flex h-16 w-16 items-center justify-center">
        <div className="absolute inset-0 rounded-2xl bg-gradient-to-br from-primary/20 to-primary/5 blur-sm" />
        <div className="relative flex h-16 w-16 items-center justify-center rounded-2xl bg-primary/10 text-primary">
          <FolderKanban className="h-7 w-7" />
        </div>
      </div>
      <h3 className="font-display text-lg font-semibold text-foreground">
        {isEn ? 'Create your first project' : '创建你的第一个项目'}
      </h3>
      <p className="mt-2 max-w-md text-sm text-muted-foreground">
        {isEn
          ? 'Projects are the workspace for everything around a single goal. Add agents, data sources, files and conversations — once a project exists, every new resource can belong to it.'
          : '项目是为单一目标组织所有资源的容器。可在其中加入 Agent、数据源、文件与对话 — 一旦项目存在，所有新资源都可以归属其中。'}
      </p>
      <ul className="mt-5 grid grid-cols-1 gap-1.5 text-left text-xs text-muted-foreground sm:grid-cols-2">
        <li className="flex items-center gap-2">
          <Sparkles className="h-3.5 w-3.5 text-primary" />
          {isEn ? 'Automatic KB inheritance for agents' : 'Agent 自动继承数据源'}
        </li>
        <li className="flex items-center gap-2">
          <LayoutGrid className="h-3.5 w-3.5 text-primary" />
          {isEn ? 'One inbox for chats, files, memory' : '对话、文件、记忆统一管理'}
        </li>
      </ul>
      <Button onClick={onCreate} size="sm" className="mt-6 gap-1.5 shadow-sm">
        <Plus className="h-4 w-4" />
        {isEn ? 'New Project' : '新建项目'}
      </Button>
    </div>
  );
}

/**
 * NoResults — when search returns 0 hits. Smaller, focused
 * affordance to clear the query.
 */
function NoResults({ query, isEn, onClear }) {
  return (
    <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-border/60 bg-card/40 px-6 py-12 text-center">
      <Search className="mb-3 h-6 w-6 text-muted-foreground" />
      <h3 className="font-display text-sm font-medium text-foreground">
        {isEn ? `No projects match "${query}"` : `未找到与"${query}"匹配的项目`}
      </h3>
      <Button variant="ghost" size="sm" onClick={onClear} className="mt-2 text-primary">
        {isEn ? 'Clear search' : '清除搜索条件'}
      </Button>
    </div>
  );
}


/**
 * Section header for COMPANY and MY project/agent sections.
 */
function SectionHeader({ icon: Icon, title, badge }) {
  return (
    <div className="flex items-center gap-2 border-b border-border pb-2">
      <Icon className="h-4 w-4 text-muted-foreground" />
      <h3 className="text-sm font-medium text-foreground">{title}</h3>
      <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
        {badge}
      </span>
    </div>
  );
}


/**
 * Project card grid shared by COMPANY and MY sections.
 */
function ProjectGrid({
  projects, counts, lang, isEn, formatRelativeTime,
  navigate, rename, unarchive, openArchive,
  canManageAccess, onManageAccess,
}) {
  return (
    <ul
      role="list"
      aria-label={isEn ? 'Project list' : '项目列表'}
      className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3"
    >
      {projects.map((p) => {
        const c = counts[p.id] || { agents: 0, kb: 0, files: 0 };
        const isArchived = p.status === 'archived';
        return (
          <ProjectCard
            key={p.id}
            project={p}
            counts={c}
            isArchived={isArchived}
            isEn={isEn}
            relativeTime={formatRelativeTime(p.updated_date, lang)}
            onOpen={() => navigate(`/my-space/project/${p.id}`)}
            onRename={isArchived ? undefined : (e) => { e.stopPropagation(); rename(p); }}
            onArchive={isArchived ? (e) => { e.stopPropagation(); unarchive(p); } : (e) => { e.stopPropagation(); openArchive(p); }}
            onManageAccess={!isArchived && canManageAccess?.(p) ? (e) => { e.stopPropagation(); onManageAccess?.(p); } : undefined}
          />
        );
      })}
    </ul>
  );
}
