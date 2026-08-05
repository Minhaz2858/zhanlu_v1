import { useState, useEffect, useMemo } from 'react';
import { base44 } from '@/api/base44Client';
import { useLanguage } from '@/lib/LanguageProvider';
import { useTranslate } from '@/lib/useTranslate';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Loader2, FileText } from 'lucide-react';
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
 *   - 'personal': files whose project's `resource_type` is 'personal' OR
 *                 files with no project (matches the rule chosen for unassigned files)
 *   - 'company': files whose project's `resource_type` is 'company'
 *
 * We do the filter client-side by reading the project's `resource_type`
 * (set on the Project entity by the RBAC migration). This avoids a DB
 * migration on UserFile and keeps the rest of the code unchanged.
 */
export default function FilesView({ scope = 'all' }) {
  const { t, lang } = useLanguage();
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [projectTypes, setProjectTypes] = useState({}); // { project_id: 'personal' | 'company' }
  const [filterProject, setFilterProject] = useState('all');
  const [filterAgent, setFilterAgent] = useState('all');
  const [sortBy, setSortBy] = useState('date');
  const [keywords, setKeywords] = useState([]);
  const [renameItem, setRenameItem] = useState(null);
  const [previewItem, setPreviewItem] = useState(null);

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
      const [files, reports, projects] = await Promise.all([
        base44.entities.UserFile.list('-updated_date', 500),
        base44.entities.Report.list('-updated_date', 500).catch(() => []),
        scope === 'all'
          ? Promise.resolve(null)
          : base44.entities.Project.list('-updated_date', 500).catch(() => []),
      ]);
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
          read: false,
          pinned: false,
          size: null,
          updated_date: r.updated_date,
          created_date: r.created_date,
          _entityType: 'Report',
        };
      });
      // Merge Report rows after UserFile rows (UserFile first = pinned items surface on top)
      setItems([...files, ...reportItems]);
      if (projects) {
        const map = {};
        for (const p of projects) map[p.id] = p.resource_type || 'personal';
        setProjectTypes(map);
      } else {
        setProjectTypes({});
      }
    } finally { setLoading(false); }
  }

  const translate = useTranslate(items.map((i) => i.name).filter(Boolean), lang);

  const projects = [...new Set(items.map((i) => i.project).filter(Boolean))];
  const agents = [...new Set(items.map((i) => i.agent_name).filter(Boolean))];
  const unreadCount = items.filter((i) => i.read === false).length;

  // Determine which files match the requested scope. Files with no
  // `project_id` are treated as "personal" — matches the rule chosen
  // for unassigned files during brainstorming. Files whose `project_id`
  // resolves to an unknown project also fall back to "personal" so
  // they don't disappear silently.
  const scopedItems = useMemo(() => {
    if (scope === 'all') return items;
    return items.filter((item) => {
      if (!item.project_id) return scope === 'personal';
      const projType = projectTypes[item.project_id];
      // Unknown / missing project_type → treat as personal (best-effort
      // fallback so legacy files still appear somewhere).
      const normalized = projType || 'personal';
      return normalized === scope;
    });
  }, [items, scope, projectTypes]);

  let filtered = scopedItems;
  if (filterProject !== 'all') filtered = filtered.filter((i) => i.project === filterProject);
  if (filterAgent !== 'all') filtered = filtered.filter((i) => i.agent_name === filterAgent);
  if (keywords.length > 0) filtered = filtered.filter((i) => KEYWORDS.some((kw) => keywords.includes(kw.key) && kw.match(i)));

  filtered = [...filtered].sort((a, b) => {
    const aPin = a.pinned === true, bPin = b.pinned === true;
    if (aPin !== bPin) return aPin ? -1 : 1;
    if (sortBy === 'project') return (a.project || '').localeCompare(b.project || '');
    if (sortBy === 'agent') return (a.agent_name || '').localeCompare(b.agent_name || '');
    return new Date(b.updated_date) - new Date(a.updated_date);
  });

  const pinned = filtered.filter((i) => i.pinned === true);
  const others = filtered.filter((i) => i.pinned !== true);

  const isReport = (item) => item._entityType === 'Report';
  const entityFor = (item) => isReport(item) ? base44.entities.Report : base44.entities.UserFile;

  async function togglePin(item) {
    if (isReport(item)) return; // Report has no pinned field
    await base44.entities.UserFile.update(item.id, { pinned: !item.pinned });
    load();
  }
  async function markRead(item) {
    if (isReport(item)) return; // Report has no read field
    if (item.read === false) await base44.entities.UserFile.update(item.id, { read: true });
  }
  async function remove(item) {
    await entityFor(item).delete(item.id);
    load();
  }
  async function doRename(name) {
    if (!renameItem || !name.trim()) return;
    if (isReport(renameItem)) {
      await base44.entities.Report.update(renameItem.id, { title: name.trim() });
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

  const fmt = (d) => moment(d).fromNow();

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
            <SelectItem value="project">{t.myFiles.sortProject}</SelectItem>
            <SelectItem value="agent">{t.myFiles.sortAgent}</SelectItem>
          </SelectContent>
        </Select>
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
                {pinned.map((it) => <FileCard key={it.id} item={it} t={t} translate={translate} timeLabel={fmt(it.updated_date)} onOpen={openItem} onPin={togglePin} onRename={setRenameItem} onDelete={remove} />)}
              </div>
            </div>
          )}
          {others.length > 0 && (
            <div>
              {pinned.length > 0 && <p className="mb-2 text-xs font-medium text-muted-foreground">{t.myFiles.unpinned}</p>}
              <div className="space-y-2">
                {others.map((it) => <FileCard key={it.id} item={it} t={t} translate={translate} timeLabel={fmt(it.updated_date)} onOpen={openItem} onPin={togglePin} onRename={setRenameItem} onDelete={remove} />)}
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