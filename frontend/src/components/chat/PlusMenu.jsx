import { useState, useEffect, useRef } from 'react';
import { createPortal } from 'react-dom';
import { base44 } from '@/api/base44Client';
import { useLanguage } from '@/lib/LanguageProvider';
import { filterUserAgents } from '@/lib/systemAgents';
import { Plus, Upload, Wrench, Bot, Search, ChevronLeft, Check, Folder } from 'lucide-react';
import { toast } from 'sonner';

export default function PlusMenu({
  onUpload, onSelectSkill, onSelectAgent, onSelectProject,
  activeSkill, activeAgent, activeProject, disabled,
  uploadEnabled = true,
}) {
  const { t } = useLanguage();
  const [view, setView] = useState('main');
  const [open, setOpen] = useState(false);
  const [skills, setSkills] = useState([]);
  const [agents, setAgents] = useState([]);
  const [projects, setProjects] = useState([]);
  const [q, setQ] = useState('');
  const [uploading, setUploading] = useState(false);
  const [pos, setPos] = useState({ top: 0, left: 0 });
  const fileRef = useRef(null);
  const wrapRef = useRef(null);

  // Recompute the dropdown's position whenever it opens or the window
  // scrolls / resizes.  The dropdown is rendered in a React portal at
  // document body level (see below) so it's never clipped by an
  // ancestor with `overflow: hidden` (e.g. the chat Panel from
  // react-resizable-panels).
  useEffect(() => {
    if (!open || !wrapRef.current) return;
    function compute() {
      const r = wrapRef.current.getBoundingClientRect();
      // Position above the button, aligned to its left edge.
      // 8px gap (mb-2 equivalent).
      setPos({
        top: r.top - 8,
        left: r.left,
      });
    }
    compute();
    window.addEventListener('scroll', compute, true);
    window.addEventListener('resize', compute);
    return () => {
      window.removeEventListener('scroll', compute, true);
      window.removeEventListener('resize', compute);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    Promise.all([
      base44.entities.Tool.list('-updated_date', 100),
      base44.auth.me().catch(() => null),
    ]).then(([tools, u]) => {
      setSkills(u ? tools.filter((x) => x.created_by_id === u.id) : tools);
    }).catch(() => {});
    base44.entities.AgentApp.list('-updated_date', 100).then((rows) => setAgents(filterUserAgents(rows))).catch(() => {});
    // Fetch projects (non-archived) so the user can switch project
    // directly from the + menu without going to the sidebar.
    base44.entities.Project.list('-updated_date', 200)
      .then((list) => {
        const filtered = (list || [])
          .filter((p) => p.status !== 'archived')
          .map((p) => ({ id: p.id, name: p.name }))
          .filter((p) => p.name);
        setProjects(filtered);
      })
      .catch(() => {});
  }, [open]);

  useEffect(() => {
    if (!open) return;
    function onDown(e) {
      if (wrapRef.current && !wrapRef.current.contains(e.target)
          && !e.target.closest('[data-plusmenu-portal]')) {
        setOpen(false);
        setView('main');
        setQ('');
      }
    }
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [open]);

  function skillToken(s) {
    if (s.trigger) {
      const parts = s.trigger.split('/').filter(Boolean);
      return parts.length ? parts[parts.length - 1] : s.trigger;
    }
    return s.name;
  }

  async function handleFileChange(e) {
    // Phase 2: support multiple files in one pick (matches ChatGPT/Kimi).
    // The accept attribute on the <input> (see below) restricts the
    // picker to the formats the backend's UploadFile endpoint allows.
    const files = Array.from(e.target.files || []);
    e.target.value = '';
    if (!files.length) return;
    setUploading(true);
    try {
      // Upload sequentially so a failure on file N doesn't lose files 1..N-1.
      // Each call to onUpload uploads + persists + appends to attachments
      // state in the parent (Chat.jsx handleUploadFile).
      for (const file of files) {
        try {
          await onUpload?.(file);
        } catch (err) {
          // Surface the failure but keep going so one bad file doesn't
          // abort the rest of the batch.
          console.error('[PlusMenu] onUpload threw:', err);
          toast.error(err?.message || 'Upload failed');
        }
      }
      setOpen(false);
      setView('main');
    } finally {
      setUploading(false);
    }
  }

  const ql = q.toLowerCase();
  const filteredSkills = skills.filter((s) => (s.name || '').toLowerCase().includes(ql) || (s.trigger || '').toLowerCase().includes(ql));
  const filteredAgents = agents.filter((a) => (a.name || '').toLowerCase().includes(ql));
  const filteredProjects = projects.filter((p) => p.name.toLowerCase().includes(ql));

  // The dropdown is rendered in a portal so it's never clipped by an
  // ancestor with overflow:hidden (the Panel from react-resizable-panels
  // does this, and would otherwise clip the menu when the chat panel is
  // narrow).  Position is computed from the button's bounding rect.
  const dropdown = open ? (
    <div
      data-plusmenu-portal
      style={{ position: 'fixed', top: pos.top, left: pos.left }}
      className="z-[9999] mb-2 w-64 -translate-y-full rounded-lg border border-border bg-popover shadow-lg"
      role="menu"
    >
      {view === 'main' ? (
        <div className="p-1">
          {uploadEnabled && (
            <button
              onClick={() => fileRef.current?.click()}
              disabled={uploading}
              className="flex w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-left text-xs text-foreground transition-colors hover:bg-secondary/70 disabled:opacity-50"
            >
              <Upload className="h-3.5 w-3.5 text-muted-foreground" />
              {uploading ? t.chat.plus.uploading : t.chat.plus.upload}
            </button>
          )}
          <button onClick={() => setView('project')} className="flex w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-left text-xs text-foreground transition-colors hover:bg-secondary/70">
            <Folder className="h-3.5 w-3.5 text-muted-foreground" /> Select project
            {activeProject && <span className="ml-auto max-w-[6rem] truncate text-[10px] text-amber-700 dark:text-amber-300">{activeProject}</span>}
          </button>
          <button onClick={() => setView('skill')} className="flex w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-left text-xs text-foreground transition-colors hover:bg-secondary/70">
            <Wrench className="h-3.5 w-3.5 text-muted-foreground" /> {t.chat.plus.skill}
            {activeSkill && <span className="ml-auto max-w-[6rem] truncate text-[10px] text-primary">/{skillToken(activeSkill)}</span>}
          </button>
          <button onClick={() => setView('agent')} className="flex w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-left text-xs text-foreground transition-colors hover:bg-secondary/70">
            <Bot className="h-3.5 w-3.5 text-muted-foreground" /> {t.chat.plus.agent}
            {activeAgent && <span className="ml-auto max-w-[6rem] truncate text-[10px] text-primary">{activeAgent.name}</span>}
          </button>
        </div>
      ) : (
        <>
          <div className="flex items-center gap-1.5 border-b border-border p-2">
            <button onClick={() => setView('main')} className="text-muted-foreground hover:text-foreground">
              <ChevronLeft className="h-3.5 w-3.5" />
            </button>
            <span className="text-xs font-medium text-foreground">
              {view === 'skill' ? t.chat.plus.skill : view === 'agent' ? t.chat.plus.agent : 'Select project'}
            </span>
          </div>
          <div className="border-b border-border p-2">
            <div className="flex items-center gap-1.5 rounded-md bg-secondary/60 px-2 py-1">
              <Search className="h-3 w-3 text-muted-foreground" />
              <input value={q} onChange={(e) => setQ(e.target.value)} placeholder={t.toolkit.searchPlaceholder} className="w-full bg-transparent text-xs focus:outline-none" autoFocus />
            </div>
          </div>
          <div className="max-h-52 overflow-y-auto p-1">
            {view === 'skill' && (filteredSkills.length === 0 ? (
              <p className="px-2 py-3 text-center text-xs text-muted-foreground">{t.toolkit.empty}</p>
            ) : filteredSkills.map((s) => {
              const tok = skillToken(s);
              return (
                <button key={s.id} onClick={() => { onSelectSkill(s); setOpen(false); setView('main'); setQ(''); }} className="flex w-full items-center justify-between gap-2 rounded-md px-2 py-1.5 text-left text-xs text-foreground transition-colors hover:bg-secondary/70">
                  <span className="truncate">{s.name}</span>
                  {activeSkill?.id === s.id ? <Check className="h-3 w-3 shrink-0 text-primary" /> : <span className="shrink-0 font-mono text-[10px] text-muted-foreground">/{tok}</span>}
                </button>
              );
            }))}
            {view === 'agent' && (filteredAgents.length === 0 ? (
              <p className="px-2 py-3 text-center text-xs text-muted-foreground">{t.mySpace.empty}{t.mySpace.tabs.agent}</p>
            ) : filteredAgents.map((a) => (
              <button key={a.id} onClick={() => { onSelectAgent(a); setOpen(false); setView('main'); setQ(''); }} className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs text-foreground transition-colors hover:bg-secondary/70">
                <Bot className="h-3 w-3 shrink-0 text-muted-foreground" />
                <span className="truncate">{a.name}</span>
                {activeAgent?.id === a.id && <Check className="ml-auto h-3 w-3 shrink-0 text-primary" />}
              </button>
            )))}
            {view === 'project' && (filteredProjects.length === 0 ? (
              <p className="px-2 py-3 text-center text-xs text-muted-foreground">No projects found</p>
            ) : filteredProjects.map((p) => (
              <button key={p.id} onClick={() => { onSelectProject?.(p.name, p.id); setOpen(false); setView('main'); setQ(''); }} className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs text-foreground transition-colors hover:bg-secondary/70">
                <Folder className="h-3 w-3 shrink-0 text-muted-foreground" />
                <span className="truncate">{p.name}</span>
                {activeProject === p.name && <Check className="ml-auto h-3 w-3 shrink-0 text-amber-700 dark:text-amber-300" />}
              </button>
            )))}
          </div>
        </>
      )}
    </div>
  ) : null;

  return (
    <div className="relative" ref={wrapRef}>
      <button
        onClick={() => { setOpen((o) => !o); setView('main'); setQ(''); }}
        disabled={disabled || uploading}
        className="inline-flex h-7 w-7 items-center justify-center rounded-full border border-border text-muted-foreground transition-colors hover:border-primary/40 hover:text-foreground disabled:opacity-40"
        title={t.chat.plus.upload}
        aria-haspopup="menu"
        aria-expanded={open}
      >
        <Plus className="h-4 w-4" />
      </button>
      {typeof document !== 'undefined' && createPortal(dropdown, document.body)}
      {uploadEnabled && (
        <input
          ref={fileRef}
          type="file"
          className="hidden"
          multiple
          data-testid="file-upload-input"
          accept=".pdf,.docx,.pptx,.ppt,.xlsx,.xls,.csv,.txt,.md,.json,.html,.htm,.png,.jpg,.jpeg,.webp,.gif,.bmp,.tiff,.tif,.mp3,.m4a,.wav,.mp4,.mov,.webm"
          onChange={handleFileChange}
        />
      )}
    </div>
  );
}