import { useState, useEffect } from 'react';
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

export default function FilesView() {
  const { t, lang } = useLanguage();
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
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
  useEffect(() => { load(); }, []);

  async function load() {
    setLoading(true);
    try {
      setItems(await base44.entities.UserFile.list('-updated_date', 500));
    } finally { setLoading(false); }
  }

  const translate = useTranslate(items.map((i) => i.name).filter(Boolean), lang);

  const projects = [...new Set(items.map((i) => i.project).filter(Boolean))];
  const agents = [...new Set(items.map((i) => i.agent_name).filter(Boolean))];
  const unreadCount = items.filter((i) => i.read === false).length;

  let filtered = items;
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

  async function togglePin(item) {
    await base44.entities.UserFile.update(item.id, { pinned: !item.pinned });
    load();
  }
  async function markRead(item) {
    if (item.read === false) await base44.entities.UserFile.update(item.id, { read: true });
  }
  async function remove(item) {
    await base44.entities.UserFile.delete(item.id);
    load();
  }
  async function doRename(name) {
    if (!renameItem || !name.trim()) return;
    await base44.entities.UserFile.update(renameItem.id, { name: name.trim() });
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
          {items.length} {t.myFiles.files}{unreadCount > 0 && ` · ${unreadCount} ${t.myFiles.unread}`}
        </span>
      </div>

      {loading ? (
        <div className="flex justify-center py-20"><Loader2 className="h-6 w-6 animate-spin text-muted-foreground" /></div>
      ) : filtered.length === 0 ? (
        <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-border py-20 text-center">
          <FileText className="mb-3 h-8 w-8 text-muted-foreground/40" />
          <p className="text-sm text-muted-foreground">{items.length === 0 ? t.myFiles.noFiles : t.myFiles.emptyFiltered}</p>
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