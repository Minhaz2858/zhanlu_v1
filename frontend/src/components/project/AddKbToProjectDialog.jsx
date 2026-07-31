/**
 * AddKbToProjectDialog — modal for picking existing KnowledgeBases (from
 * the Connectors list) and assigning them to the current Project.
 *
 * UX:
 *  - Loads all active KBs from the backend.
 *  - The KBs already assigned to THIS project are pre-selected & disabled.
 *  - The KBs already assigned to OTHER projects (or no project) can be
 *    selected with checkboxes. Save = bulk-update each by setting
 *    project_id = currentProject.id (and project = currentProject.name
 *    for legacy readers).
 *
 * This makes "Connectors → project membership" a 1-click flow.
 */
import { useState, useEffect } from 'react';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { base44 } from '@/api/base44Client';
import { useLanguage } from '@/lib/LanguageProvider';
import { Database, Check, Loader2, Search, FileText, Plus } from 'lucide-react';

export default function AddKbToProjectDialog({
  open,
  onOpenChange,
  project,
  excludeIds = [],
  onAdded,
}) {
  const { t, lang } = useLanguage();
  const isEn = lang === 'en';

  const [kbs, setKbs] = useState([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [selected, setSelected] = useState(new Set());
  const [search, setSearch] = useState('');

  // Reset when reopened.
  useEffect(() => {
    if (open) {
      setSelected(new Set());
      setSearch('');
      load();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  async function load() {
    setLoading(true);
    try {
      // Pull a sizable window; we filter in-memory.
      const all = await base44.entities.KnowledgeBase.list('-updated_date', 500);
      setKbs(all);
    } catch {
      setKbs([]);
    } finally {
      setLoading(false);
    }
  }

  // KBs that are currently bound to this project are shown already in the
  // main list — we don't allow re-adding them, just leave them visible-but-
  // disabled for clarity.
  const boundHere = new Set(excludeIds);

  const filtered = kbs.filter((k) => {
    const q = search.trim().toLowerCase();
    if (!q) return true;
    return (k.name && k.name.toLowerCase().includes(q))
      || (k.db_type && k.db_type.toLowerCase().includes(q))
      || (k.file_type && k.file_type.toLowerCase().includes(q));
  });

  function toggle(id) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  async function save() {
    if (selected.size === 0) return;
    setSaving(true);
    try {
      const ids = Array.from(selected);
      // Update each kb sequentially: idempotent and small payload.
      for (const id of ids) {
        await base44.entities.KnowledgeBase.update(id, {
          project_id: project.id,
          // Keep legacy `project` field in sync for older code paths.
          project: project.name && project.name !== 'global' ? project.name : 'global',
        });
      }
      onAdded?.(ids);
      onOpenChange(false);
    } catch (e) {
      console.error('Add KB to project failed', e);
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Database className="h-5 w-5 text-primary" />
            {isEn ? 'Add Data Sources' : '添加数据源'}
          </DialogTitle>
          <DialogDescription>
            {isEn
              ? <>Select knowledge bases from your Connectors to associate with project <b>{project.name}</b>.</>
              : <>从 Connectors 已存在的知识库中选择，将其关联到项目 <b>{project.name}</b>。</>}
          </DialogDescription>
        </DialogHeader>

        {/* Search box */}
        <div className="relative">
          <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={isEn ? 'Search by name, db type…' : '搜索名称、数据库类型…'}
            className="w-full rounded-md border border-border bg-background py-1.5 pl-8 pr-3 text-sm focus:outline-none focus:ring-1 focus:ring-primary"
          />
        </div>

        {/* KB list */}
        <div className="max-h-72 overflow-y-auto rounded-lg border border-border">
          {loading ? (
            <div className="flex justify-center py-10"><Loader2 className="h-5 w-5 animate-spin text-muted-foreground" /></div>
          ) : filtered.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-10 text-xs text-muted-foreground">
              <Database className="mb-2 h-6 w-6" />
              {isEn ? 'No knowledge bases available.' : '暂无可用的知识库。'}
            </div>
          ) : (
            <ul className="divide-y divide-border">
              {filtered.map((kb) => {
                const isBound = boundHere.has(kb.id);
                const isSelected = selected.has(kb.id);
                const isFile = !kb.db_type && !kb.source_kind && (kb.file_type || (kb.file_url || kb.source === 'upload'));
                const kindLabel = kb.db_type || (kb.source_kind === 'database' ? 'database' : null) || (kb.file_type || 'file');
                return (
                  <li key={kb.id}>
                    <label
                      className={`flex cursor-pointer items-start gap-3 px-3 py-2.5 transition-colors ${
                        isBound ? 'cursor-default bg-secondary/40 opacity-60' : 'hover:bg-secondary/60 cursor-pointer'
                      } ${isSelected ? 'bg-primary/5' : ''}`}
                    >
                      <span className="mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded border border-border bg-background">
                        {isBound ? (
                          <Check className="h-3 w-3 text-muted-foreground" />
                        ) : isSelected ? (
                          <Check className="h-3 w-3 text-primary" />
                        ) : null}
                      </span>
                      <span className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-lg ${
                        isFile ? 'bg-amber-100 text-amber-600 dark:bg-amber-900/30 dark:text-amber-300' : 'bg-blue-100 text-blue-600 dark:bg-blue-900/30 dark:text-blue-300'
                      }`}>
                        {isFile ? <FileText className="h-3.5 w-3.5" /> : <Database className="h-3.5 w-3.5" />}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-sm font-medium">{kb.name}</span>
                        <span className="block truncate text-[11px] text-muted-foreground">
                          {kindLabel}{kb.host ? ` · ${kb.host}${kb.port ? ':' + kb.port : ''}` : ''}
                          {isBound && (
                            <span className="ml-1.5 rounded bg-secondary px-1 py-0.5 text-[10px]">
                              {isEn ? 'already in this project' : '已在项目中'}
                            </span>
                          )}
                          {!isBound && kb.project_id && kb.project_id !== project.id && (
                            <span className="ml-1.5 rounded bg-amber-100 px-1 py-0.5 text-[10px] text-amber-700 dark:bg-amber-900/30 dark:text-amber-300">
                              {isEn ? 'in another project' : '属于其他项目'}
                            </span>
                          )}
                        </span>
                      </span>
                      {/* Native checkbox for accessibility */}
                      <input
                        type="checkbox"
                        checked={isBound || isSelected}
                        disabled={isBound}
                        onChange={() => !isBound && toggle(kb.id)}
                        className="sr-only"
                      />
                    </label>
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        <DialogFooter>
          <span className="mr-auto text-xs text-muted-foreground">
            {selected.size > 0 ? (
              <>{isEn ? `${selected.size} selected` : `已选择 ${selected.size} 个`}</>
            ) : (isEn ? 'Select one or more' : '选择一个或多个')}
          </span>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={saving}>
            {isEn ? 'Cancel' : '取消'}
          </Button>
          <Button onClick={save} disabled={saving || selected.size === 0} className="gap-1.5">
            {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Plus className="h-3.5 w-3.5" />}
            {isEn ? 'Add to Project' : '添加到项目'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
