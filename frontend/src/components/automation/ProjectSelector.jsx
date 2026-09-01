import { useState, useEffect, useRef } from 'react';
import { base44 } from '@/api/base44Client';
import { useLanguage } from '@/lib/LanguageProvider';
import { Folder, Plus, ChevronDown, Check, Pencil, X } from 'lucide-react';

/**
 * ProjectSelector — dropdown to pick / create / rename a Project.
 *
 * Backward compat: by default the value passed to `onChange` is the project
 * name string (existing callers like AutomationTasks / RoleSection rely on
 * that). When the `useProjectId` prop is true, the value is the project id
 * (the FK column) — used by new code (Project Detail, project-aware agent
 * builder) that filters by project_id.
 *
 * In either mode the dropdown shows the project name; we resolve the label
 * by matching either id OR name so values written by legacy callers still
 * render correctly.
 */
export default function ProjectSelector({
  value,
  onChange,
  allowAll = false,
  size = 'sm',
  useProjectId = false,
}) {
  const { t } = useLanguage();
  const [projects, setProjects] = useState([]);
  const [open, setOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState('');
  const [editingId, setEditingId] = useState(null);
  const [editName, setEditName] = useState('');
  const ref = useRef(null);

  useEffect(() => { load(); }, []);
  useEffect(() => {
    function handleClick(e) { if (ref.current && !ref.current.contains(e.target)) { setOpen(false); setCreating(false); setEditingId(null); } }
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, []);

  async function load() {
    try {
      // Load ALL projects (don't pre-filter by status) because legacy rows
      // created before the `status` column was added carry status=null —
      // they should still be pickable from the dropdown. Filter to
      // "active-or-untagged" on the client side.
      const list = await base44.entities.Project.list('-updated_date', 200);
      setProjects((list || []).filter((p) => p.status !== 'archived'));
    } catch { setProjects([]); }
  }

  // ── Resolve the selected label. Value can be:
  //    • a project id (new code using useProjectId)
  //    • a project name (legacy code)
  //    • 'global' / null / ''   → Ungrouped
  //    • 'all'                  → All Projects (only if allowAll)
  function getValue() {
    if (value === 'all') return 'all';
    if (value === 'global' || !value) return 'global';
    return value;
  }

  async function createProject() {
    if (!newName.trim()) return;
    try {
      const p = await base44.entities.Project.create({ name: newName.trim() });
      setProjects((prev) => [...prev, p]);
      // Always emit the project id going forward; callers using the legacy
      // (name) contract have already been migrated by callers like
      // AutomationTasks that now store id when useProjectId=true, OR keep
      // their name-based logic when useProjectId=false.
      onChange(useProjectId ? p.id : p.name);
      setNewName('');
      setCreating(false);
      setOpen(false);
    } catch { /* noop */ }
  }

  async function renameProject(p) {
    if (!editName.trim()) return;
    try {
      await base44.entities.Project.update(p.id, { name: editName.trim() });
      setProjects((prev) => prev.map((x) => (x.id === p.id ? { ...x, name: editName.trim() } : x)));
      const currentValue = getValue();
      if (currentValue === p.id || currentValue === p.name) onChange(useProjectId ? p.id : editName.trim());
    } catch { /* noop */ }
    setEditingId(null);
    setEditName('');
  }

  function projectMatches(p) {
    const v = getValue();
    return v === p.id || v === p.name;
  }

  const selectedLabel = (() => {
    const v = getValue();
    if (v === 'all') return t.automation.allProjects;
    if (v === 'global') return t.automation.ungrouped;
    const found = projects.find((p) => v === p.id || v === p.name);
    return found ? found.name : (value || t.automation.ungrouped);
  })();

  const sizeCls = size === 'sm' ? 'px-2.5 py-1.5 text-xs' : 'px-3 py-2 text-sm';

  return (
    <div className="relative" ref={ref}>
      <button onClick={() => setOpen(!open)} className={`inline-flex items-center gap-1.5 rounded-lg border border-border bg-card ${sizeCls} text-foreground transition-colors hover:bg-secondary`}>
        <Folder className="h-3.5 w-3.5 text-primary" />
        <span className="max-w-[120px] truncate">{selectedLabel}</span>
        <ChevronDown className="h-3 w-3 text-muted-foreground" />
      </button>
      {open && (
        <div className="absolute left-0 top-full z-50 mt-1 w-56 rounded-lg border border-border bg-popover p-1 shadow-lg">
          {allowAll && (
            <button onClick={() => { onChange('all'); setOpen(false); }} className={`flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs hover:bg-secondary ${value === 'all' ? 'text-primary' : 'text-foreground'}`}>
              <Folder className="h-3.5 w-3.5" /> {t.automation.allProjects}
              {value === 'all' && <Check className="ml-auto h-3 w-3" />}
            </button>
          )}
          <button onClick={() => { onChange('global'); setOpen(false); }} className={`flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs hover:bg-secondary ${value === 'global' ? 'text-primary' : 'text-foreground'}`}>
            <Folder className="h-3.5 w-3.5" /> {t.automation.ungrouped}
            {value === 'global' && <Check className="ml-auto h-3 w-3" />}
          </button>
          {projects.filter((p) => p.name !== 'Ungrouped' && p.name !== '未分组').map((p) => editingId === p.id ? (
            <div key={p.id} className="flex items-center gap-1 px-1 py-1">
              <input value={editName} onChange={(e) => setEditName(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') renameProject(p); if (e.key === 'Escape') { setEditingId(null); setEditName(''); } }} className="flex-1 rounded border border-border bg-background px-2 py-1 text-xs focus:outline-none" autoFocus />
              <button onClick={() => renameProject(p)} className="rounded bg-primary p-1.5 text-primary-foreground"><Check className="h-3 w-3" /></button>
              <button onClick={() => { setEditingId(null); setEditName(''); }} className="rounded border border-border p-1.5 text-muted-foreground hover:text-foreground"><X className="h-3 w-3" /></button>
            </div>
          ) : (
            <div key={p.id} className="group flex w-full items-center gap-1 rounded-md px-2 py-1.5 text-left text-xs hover:bg-secondary">
              <button onClick={() => { onChange(useProjectId ? p.id : p.name); setOpen(false); }} className={`flex flex-1 items-center gap-2 ${projectMatches(p) ? 'text-primary' : 'text-foreground'}`}>
                <span className="h-2 w-2 rounded-full shrink-0" style={{ background: p.color || '#D37435' }} />
                <span className="flex-1 truncate">{p.name}</span>
                {projectMatches(p) && <Check className="ml-auto h-3 w-3" />}
              </button>
              <button onClick={() => { setEditingId(p.id); setEditName(p.name); }} className="text-muted-foreground opacity-0 transition-opacity hover:text-foreground group-hover:opacity-100" title={t.automation.editProject}><Pencil className="h-3 w-3" /></button>
            </div>
          ))}
          <div className="my-1 border-t border-border" />
          {creating ? (
            <div className="flex items-center gap-1 px-1 py-1">
              <input value={newName} onChange={(e) => setNewName(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') createProject(); }} placeholder={t.automation.projectPh} className="flex-1 rounded border border-border bg-background px-2 py-1 text-xs focus:outline-none" autoFocus />
              <button onClick={createProject} className="rounded bg-primary p-1.5 text-primary-foreground"><Check className="h-3 w-3" /></button>
            </div>
          ) : (
            <button onClick={() => setCreating(true)} className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs text-muted-foreground hover:bg-secondary hover:text-foreground">
              <Plus className="h-3.5 w-3.5" /> {t.automation.newProject}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
