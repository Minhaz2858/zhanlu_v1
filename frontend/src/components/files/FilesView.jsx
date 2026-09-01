import { useState, useEffect, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { base44 } from '@/api/base44Client';
import { listDashboardApps, markDashboardViewed } from '@/api/dashboards';
import { useLanguage } from '@/lib/LanguageProvider';
import { useTranslate } from '@/lib/useTranslate';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Loader2, FileText, ArrowUp, ArrowDown } from 'lucide-react';
import moment from 'moment';
import 'moment/locale/zh-cn';
import FileCard from './FileCard';
import RenameDialog from './RenameDialog';
import FilePreviewModal from '@/components/chat/FilePreviewModal';

/**
 * FilesView — list of UserFile rows, with optional scope filter.
 *
 * `scope`:
 *   - 'all' (default): no filtering beyond search/sort
 *   - 'personal': files whose project OR agent is personal, or files with
 *                 neither (matches the rule chosen for unassigned files)
 *   - 'company': files whose project OR agent is company
 *
 * Classification is "Project wins → AgentApp fallback → default 'personal'":
 * a file's `project_id` resolves via Project.resource_type; if it has no
 * project, its `agent_name` resolves via AgentApp.resource_type (matched by
 * name — UserFile.agent_name is a String, not a FK); otherwise it defaults
 * to 'personal'.
 *
 * We do the filter client-side by reading `resource_type` (set on the
 * Project/AgentApp entities by the RBAC migration). This avoids a DB
 * migration on UserFile and keeps the rest of the code unchanged.
 */
export default function FilesView({ scope = 'all' }) {
  const navigate = useNavigate();
  const { t, lang } = useLanguage();
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [projectTypes, setProjectTypes] = useState({}); // { project_id: 'personal' | 'company' }
  const [agentTypes, setAgentTypes] = useState({}); // { agent_name: 'personal' | 'company' }
  const [filterProject, setFilterProject] = useState('all');
  const [filterAgent, setFilterAgent] = useState('all');
  const [sortBy, setSortBy] = useState('date');
  const [sortDir, setSortDir] = useState('desc');
  const [keywords, setKeywords] = useState([]);
  const [renameItem, setRenameItem] = useState(null);
  const [previewItem, setPreviewItem] = useState(null);
  // Full Project + AgentApp lists (≤500 rows each). Always loaded so the
  // filter dropdowns can list every project/agent the user has access to,
  // not just the ones that currently have files (was a UX bug: dropdown
  // appeared empty until a file was generated into a new project/agent).
  const [projectEntities, setProjectEntities] = useState([]);
  const [agentEntities, setAgentEntities] = useState([]);

  const KEYWORDS = [
    { key: 'dashboard', match: (i) => i.resource_kind === 'dashboard' },
    { key: 'report', match: (i) => i.resource_kind === 'report' || (i.file_type || '').toLowerCase().includes('report') },
    { key: 'html', match: (i) => i.resource_kind === 'html_file' || (i.file_type || '').toLowerCase() === 'html' },
    { key: 'pdf', match: (i) => (i.file_type || '').toLowerCase() === 'pdf' },
    { key: 'pptx', match: (i) => (i.file_type || '').toLowerCase() === 'pptx' },
    { key: 'md', match: (i) => ['md', 'markdown'].includes((i.file_type || '').toLowerCase()) },
  ];
  function toggleKeyword(key) {
    setKeywords((prev) => (prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key]));
  }

  useEffect(() => { moment.locale(lang === 'zh' ? 'zh-cn' : 'en'); }, [lang]);
  useEffect(() => { load(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [scope]);

  async function load() {
    setLoading(true);
    try {
      const [files, reports, projects, agentApps, automationFiles, automationTasks, dashAppRecords] = await Promise.all([
        base44.entities.UserFile.list('-updated_date', 500),
        base44.entities.Report.list('-updated_date', 500).catch(() => []),
        // Project + AgentApp lists are needed for (a) the filter dropdowns
        // (the union of entity names + item-derived names — see below) and
        // (b) classify() which maps items to personal/company based on
        // resource_type. Always load both regardless of scope so the
        // dropdowns never go silent for projects/agents that have zero
        // files.
        base44.entities.Project.list('-updated_date', 500).catch(() => []),
        base44.entities.AgentApp.list('-updated_date', 500).catch(() => []),
        base44.entities.AutomationFile.list('-updated_date', 500).catch(() => []),
        base44.entities.AutomationTask.list('-updated_date', 500).catch(() => []),
        // Full-stack dashboard apps (Phase 2): separate DashboardApp records
        // (not UserFile rows). Loaded so the My Files list shows live
        // dashboards with "last data change" timestamps + unread badges.
        listDashboardApps().catch(() => []),
      ]);
      // Build agent_id → name lookup so each automation file can resolve
      // its real producer agent (task.agent_id → AgentApp.name) instead
      // of falling back to the hardcoded "automation_agent" label that
      // used to surface in the filter dropdown.
      const agentNameById = {};
      for (const a of (agentApps || [])) agentNameById[a.id] = a.name;
      // Normalize Report rows to UserFile shape so they merge seamlessly
      const _extFromUrl = (url) => {
        if (!url) return 'docx';
        const m = url.match(/\.([a-z0-9]+)(?:\?|$)/i);
        return m ? m[1].toLowerCase() : 'docx';
      };
      const _reportKind = (reportType, ft) => {
        const rt = (reportType || '').toLowerCase();
        if (rt === 'dashboard') return 'dashboard';
        if (ft === 'html' || ft === 'htm') return 'html_file';
        return 'report';
      };
      const reportItems = (reports || []).map((r) => {
        const ft = _extFromUrl(r.file_url);
        const kind = _reportKind(r.type, ft);
        return {
          id: r.id,
          name: r.title,
          file_type: ft,
          file_url: r.file_url,
          source: 'ai_generated',
          resource_kind: kind,
          project: r.project,
          project_id: r.project_id,
          session_id: r.session_id,
          agent_name: r.agent_name,
          read: r.read,
          pinned: r.pinned,
          size: null,
          updated_date: r.updated_date,
          created_date: r.created_date,
          _entityType: 'Report',
        };
      });
      // Automation-agent output lives in the AutomationFile table (not
      // UserFile). Resolve each file's project/session via its parent
      // AutomationTask so it classifies into company/personal correctly and
      // shows the originating project + agent in the card.
      const taskMap = {};
      for (const task of (automationTasks || [])) taskMap[task.id] = task;
      const automationItems = (automationFiles || []).map((f) => {
        const task = taskMap[f.automation_task_id] || {};
        const ft = (f.file_type || _extFromUrl(f.file_url)).toLowerCase().replace(/^\./, '');
        return {
          id: f.id,
          name: f.name,
          file_type: ft,
          file_url: f.file_url || `/api/automations/files/${f.id}/download`,
          source: 'automation_file',
          resource_kind: 'automation_result',
          project: task.project || null,
          project_id: task.project_id || null,
          session_id: task.session_id || null,
          // Resolve to the real producer agent
          // via task.agent_id → AgentApp.name. Falls back to the generic
          // "automation_agent" only when the agent is missing/renamed, so
          // the dropdown can still show *something* rather than "(none)".
          agent_name: agentNameById[task.agent_id] || 'automation_agent',
          read: f.read,
          pinned: f.pinned,
          size: f.size || null,
          updated_date: f.updated_date || f.created_date,
          created_date: f.created_date,
          _entityType: 'AutomationFile',
        };
      });
      // Full-stack dashboard apps → UserFile-shaped items. `updated_date` is
      // set to last_data_change_at (falls back to created_date) so the card
      // shows when the underlying data last refreshed; `read: !unread` powers
      // the unread badge (unread = data changed after the user last opened it).
      const dashAppItems = (dashAppRecords || []).map((r) => ({
        id: r.slug,
        name: r.name,
        file_type: 'html',
        file_url: r.app_url || `/api/dashboards/apps/${r.slug}/`,
        source: 'dashboard_app',
        resource_kind: 'dashboard',
        project: r.project,
        project_id: r.project_id,
        scope: r.scope || 'personal', // T10: personal = creator only; company = whole org
        session_id: r.session_id || null,
        agent_name: r.agent_name || null,
        // T5: the AgentConversation id that built this app — powers the
        // "Open in chat" action (deep-link ``/?conv=<id>``).
        chat_thread_id: r.chat_thread_id || null,
        read: r.unread ? false : true,
        pinned: false,
        size: null,
        updated_date: r.last_data_change_at || r.created_date || r.updated_date,
        created_date: r.created_date,
        _entityType: 'DashboardApp',
      }));
      // Merge Report + automation + dashboard rows after UserFile rows (UserFile first = pinned items surface on top)
      setItems([...files, ...reportItems, ...automationItems, ...dashAppItems]);
      const projectMap = {};
      for (const p of (projects || [])) projectMap[p.id] = p.resource_type || 'personal';
      setProjectTypes(projectMap);
      const agentMap = {};
      for (const a of (agentApps || [])) agentMap[a.name] = a.resource_type || 'personal';
      setAgentTypes(agentMap);
      // Stash the full lists so the filter dropdowns can show every
      // project/agent the user has access to, not just the ones that
      // currently have files. See dropdown union below.
      setProjectEntities(projects || []);
      setAgentEntities(agentApps || []);
    } finally { setLoading(false); }
  }

  const translate = useTranslate(items.map((i) => i.name).filter(Boolean), lang);

  // Filter dropdown options: union of (a) every Project/AgentApp the user
  // can access and (b) project/agent names that appear on existing items.
  // The union makes sure new projects/agents show up in the dropdown
  // immediately, even before they have any files.
  const projects = [...new Set([
    ...projectEntities.map((p) => p.name).filter(Boolean),
    ...items.map((i) => i.project).filter(Boolean),
  ])].sort();
  const agents = [...new Set([
    ...agentEntities.map((a) => a.name).filter(Boolean),
    ...items.map((i) => i.agent_name).filter(Boolean),
  ])].sort();
  // Determine which files match the requested scope using the two-signal
  // rule: Project wins → AgentApp fallback → default 'personal'.
  // Files with no `project_id` fall back to the agent's `resource_type`
  // (matched by `agent_name`); files with neither fall back to "personal".
  // Unknown project ids / agent names also fall back to "personal" so
  // legacy files don't disappear silently.
  const classify = (item) => {
    // T10: full-stack dashboards carry their own scope field (personal =
    // creator only; company = whole org) — it wins over the project fallback.
    if (item._entityType === 'DashboardApp') return item.scope || 'personal';
    if (item.project_id) return projectTypes[item.project_id] || 'personal';
    if (item.agent_name) return agentTypes[item.agent_name] || 'personal';
    return 'personal';
  };
  const scopedItems = useMemo(() => {
    if (scope === 'all') return items;
    return items.filter((item) => classify(item) === scope);
  }, [items, scope, projectTypes, agentTypes]);

  let filtered = scopedItems;
  if (filterProject !== 'all') filtered = filtered.filter((i) => i.project === filterProject);
  if (filterAgent !== 'all') filtered = filtered.filter((i) => i.agent_name === filterAgent);
  if (keywords.length > 0) filtered = filtered.filter((i) => KEYWORDS.some((kw) => keywords.includes(kw.key) && kw.match(i)));

  const dir = sortDir === 'asc' ? 1 : -1;
  filtered = [...filtered].sort((a, b) => {
    const aPin = a.pinned === true, bPin = b.pinned === true;
    if (aPin !== bPin) return aPin ? -1 : 1;
    if (sortBy === 'project') return dir * (a.project || '').localeCompare(b.project || '');
    if (sortBy === 'agent') return dir * (a.agent_name || '').localeCompare(b.agent_name || '');
    if (sortBy === 'dateOnly') return dir * moment(a.updated_date).format('YYYYMMDD').localeCompare(moment(b.updated_date).format('YYYYMMDD'));
    if (sortBy === 'timeOnly') return dir * moment(a.updated_date).format('HHmmss').localeCompare(moment(b.updated_date).format('HHmmss'));
    return dir * (new Date(a.updated_date) - new Date(b.updated_date));
  });

  // Unread count must be derived from the SAME list the header displays
  // (scoped + filtered), otherwise "N files · M unread" can show M > N
  // when the scope/type filter hides unread items.
  const unreadCount = filtered.filter((i) => i.read === false).length;

  const pinned = filtered.filter((i) => i.pinned === true);
  const others = filtered.filter((i) => i.pinned !== true);

  const isReport = (item) => item._entityType === 'Report';
  const isAutomationFile = (item) => item._entityType === 'AutomationFile';
  const isDashboardApp = (item) => item._entityType === 'DashboardApp';
  const entityFor = (item) =>
    isReport(item) ? base44.entities.Report
      : isAutomationFile(item) ? base44.entities.AutomationFile
        : base44.entities.UserFile;

  async function togglePin(item) {
    // Dashboard apps have no `pinned` column — the pin control is hidden for
    // them in FileCard, so this is a no-op guard.
    if (isDashboardApp(item)) return;
    // Optimistic local update + persist for all three entity types. Every
    // type now has a backend `pinned` column (UserFile always has; Report +
    // AutomationFile gained one), so pinning survives page refreshes. The
    // `load()` refresh re-sorts pinned items to the top.
    const next = !item.pinned;
    setItems((prev) => prev.map((i) => (i.id === item.id ? { ...i, pinned: next } : i)));
    try {
      await entityFor(item).update(item.id, { pinned: next });
    } catch (_) {
      // Intentionally swallowed — the optimistic local update above is the
      // visible behaviour; the DB write failing just means the pin won't
      // survive the next load().
    }
    load();
  }
  async function markRead(item) {
    // Dashboard apps: "read" = user opened the app → POST mark-viewed which
    // stamps `viewed_at` on the backend (unread = last_data_change_at newer
    // than viewed_at).
    if (isDashboardApp(item)) {
      if (item.read === true) return;
      setItems((prev) => prev.map((i) => (i.id === item.id ? { ...i, read: true } : i)));
      try {
        await markDashboardViewed(item.id);
      } catch (_) {
        // Intentionally swallowed — optimistic local update stands; the badge
        // will re-appear on next load() if the write was rejected.
      }
      return;
    }
    // Optimistic local update across all three entity types so the orange
    // "Unread" badge / dot clears immediately on click — no manual reload
    // required. Every entity type now persists `read: true` to the DB
    // (UserFile always has; Report + AutomationFile gained a `read` column),
    // so the badge stays cleared across page refreshes. If the DB write
    // fails, the optimistic UI update still stands and the next manual
    // reload will resync.
    if (item.read === true) return;
    setItems((prev) => prev.map((i) => (i.id === item.id ? { ...i, read: true } : i)));
    try {
      await entityFor(item).update(item.id, { read: true });
    } catch (_) {
      // Intentionally swallowed — the optimistic local update above is
      // the visible behaviour; the badge will re-appear on the next
      // load() if the DB write was rejected.
    }
  }
  async function remove(item) {
    // Full-stack dashboard apps are deleted from My Space → Dashboards via
    // DELETE /api/dashboards/app-records/{slug}; FilesView does not manage
    // the dashboard lifecycle.
    if (isDashboardApp(item)) return;
    await entityFor(item).delete(item.id);
    load();
  }
  async function doRename(name) {
    if (!renameItem || !name.trim()) return;
    if (isDashboardApp(renameItem)) return;
    if (isReport(renameItem)) {
      await base44.entities.Report.update(renameItem.id, { title: name.trim() });
    } else if (isAutomationFile(renameItem)) {
      await base44.entities.AutomationFile.update(renameItem.id, { name: name.trim() });
    } else {
      await base44.entities.UserFile.update(renameItem.id, { name: name.trim() });
    }
    setRenameItem(null);
    load();
  }
  function openItem(item) {
    markRead(item);
    setPreviewItem(item);
  }
  // T5: resume the chat conversation that built a dashboard. The artifact
  // carries the AgentConversation id, which the Chat page deep-links via
  // ``/?conv=<id>`` — same contract as Project Detail "Recent Chats".
  function openChat(item) {
    if (!item?.chat_thread_id) return;
    markRead(item);
    navigate(`/?conv=${encodeURIComponent(item.chat_thread_id)}`);
  }

  const isZh = lang === 'zh';
  const fmtDate = (d) => moment(d).format(isZh ? 'YYYY-MM-DD' : 'MMM D, YYYY');
  const fmtTime = (d) => moment(d).format(isZh ? 'HH:mm:ss' : 'h:mm:ss A');

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        {KEYWORDS.map((kw) => {
          const active = keywords.includes(kw.key);
          return (
            <button key={kw.key} onClick={() => toggleKeyword(kw.key)} className={`rounded-full border px-3 py-1 text-xs transition-colors ${active ? 'border-primary bg-primary/5 text-foreground' : 'border-border text-muted-foreground hover:text-foreground'}`}>
              {t.myFiles.keywords[kw.key]}
            </button>
          );
        })}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Select value={filterProject} onValueChange={setFilterProject}>
          <SelectTrigger className={`h-8 w-auto gap-1 rounded-full px-3 text-xs ${filterProject !== 'all' ? 'border-primary text-foreground' : ''}`}><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="all">{t.myFiles.allProjects}</SelectItem>
            {projects.map((p) => <SelectItem key={p} value={p}>{p}</SelectItem>)}
          </SelectContent>
        </Select>
        <Select value={filterAgent} onValueChange={setFilterAgent}>
          <SelectTrigger className={`h-8 w-auto gap-1 rounded-full px-3 text-xs ${filterAgent !== 'all' ? 'border-primary text-foreground' : ''}`}><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="all">{t.myFiles.allAgents}</SelectItem>
            {agents.map((a) => <SelectItem key={a} value={a}>{a}</SelectItem>)}
          </SelectContent>
        </Select>
        <Select value={sortBy} onValueChange={setSortBy}>
          <SelectTrigger className="h-8 w-auto gap-1 rounded-full px-3 text-xs border-primary text-foreground"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="date">{t.myFiles.sortDate}</SelectItem>
            <SelectItem value="dateOnly">{t.myFiles.sortByDate}</SelectItem>
            <SelectItem value="timeOnly">{t.myFiles.sortByTime}</SelectItem>
            <SelectItem value="project">{t.myFiles.sortProject}</SelectItem>
            <SelectItem value="agent">{t.myFiles.sortAgent}</SelectItem>
          </SelectContent>
        </Select>
        <button
          type="button"
          onClick={() => setSortDir((d) => (d === 'desc' ? 'asc' : 'desc'))}
          title={sortDir === 'desc' ? t.myFiles.sortDirDesc : t.myFiles.sortDirAsc}
          className="inline-flex h-8 w-8 items-center justify-center rounded-full border border-border text-muted-foreground transition-colors hover:border-primary/30 hover:text-foreground"
        >
          {sortDir === 'desc' ? <ArrowDown className="h-3.5 w-3.5" /> : <ArrowUp className="h-3.5 w-3.5" />}
        </button>
        <span className="ml-auto text-xs text-muted-foreground">
          {scopedItems.length} {t.myFiles.files}{unreadCount > 0 && ` · ${unreadCount} ${t.myFiles.unread}`}
        </span>
      </div>

      {loading ? (
        <div className="flex justify-center py-20"><Loader2 className="h-6 w-6 animate-spin text-muted-foreground" /></div>
      ) : filtered.length === 0 ? (
        <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-border py-20 text-center">
          <FileText className="mb-3 h-8 w-8 text-muted-foreground/40" />
          <p className="text-sm text-muted-foreground">{scopedItems.length === 0 ? t.myFiles.noFiles : t.myFiles.emptyFiltered}</p>
          <p className="mt-1 text-xs text-muted-foreground">{t.myFiles.noFilesHint}</p>
        </div>
      ) : (
        <div className="space-y-4">
          {pinned.length > 0 && (
            <div>
              <p className="mb-2 text-xs font-medium text-muted-foreground">{t.myFiles.pinned}</p>
              <div className="space-y-2">
                {pinned.map((it) => <FileCard key={it.id} item={it} t={t} translate={translate} dateLabel={fmtDate(it.updated_date)} timeLabel={fmtTime(it.updated_date)} onOpen={openItem} onPin={togglePin} onRename={setRenameItem} onDelete={remove} onOpenChat={openChat} />)}
              </div>
            </div>
          )}
          {others.length > 0 && (
            <div>
              {pinned.length > 0 && <p className="mb-2 text-xs font-medium text-muted-foreground">{t.myFiles.unpinned}</p>}
              <div className="space-y-2">
                {others.map((it) => <FileCard key={it.id} item={it} t={t} translate={translate} dateLabel={fmtDate(it.updated_date)} timeLabel={fmtTime(it.updated_date)} onOpen={openItem} onPin={togglePin} onRename={setRenameItem} onDelete={remove} onOpenChat={openChat} />)}
              </div>
            </div>
          )}
        </div>
      )}

      <RenameDialog open={!!renameItem} item={renameItem} onConfirm={doRename} onCancel={() => setRenameItem(null)} t={t} />
      <FilePreviewModal file={previewItem} open={!!previewItem} onOpenChange={(o) => { if (!o) setPreviewItem(null); }} />
    </div>
  );
}