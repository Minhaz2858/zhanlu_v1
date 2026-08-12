import { useState, useEffect, useCallback } from 'react';
import {
  Database, FileText, Brain, GitBranch, Search, RefreshCw,
  Pencil, Loader2, Table2, Tag,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Badge } from '@/components/ui/badge';
import {
  Sheet, SheetContent, SheetHeader, SheetTitle, SheetDescription,
} from '@/components/ui/sheet';
import { toast } from '@/components/ui/use-toast';
import {
  listCatalogTables, putCatalogOverlay, listCatalogEntities,
  listRegistryResources, getProjectKnowledgeMap,
} from '@/api/projectCatalog';

const TYPE_ICONS = {
  database: Database, file: FileText, report: Table2,
  memory: Brain, decision: GitBranch, entity: Tag,
};

const VISIBILITY_LABELS = {
  project: 'Project', user_private: 'Private', org: 'Org',
};

const STATUS_COLORS = {
  ready: 'bg-emerald-100 text-emerald-700',
  indexing: 'bg-amber-100 text-amber-700',
  error: 'bg-red-100 text-red-700',
  pending: 'bg-slate-100 text-slate-600',
};

export default function DataMapSection({ projectId, canEdit, lang, T }) {
  const isEn = lang === 'en';
  const [tables, setTables] = useState([]);
  const [kbs, setKbs] = useState([]);
  const [resources, setResources] = useState([]);
  const [entities, setEntities] = useState([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [editTarget, setEditTarget] = useState(null);
  const [editForm, setEditForm] = useState({ alias: '', description: '', metric_definition: '' });

  const load = useCallback(async () => {
    setLoading(true);
    const [kmRes, tRes, rRes, eRes] = await Promise.allSettled([
      getProjectKnowledgeMap(projectId),
      listCatalogTables(projectId),
      listRegistryResources(projectId),
      listCatalogEntities(projectId),
    ]);

    if (kmRes.status === 'fulfilled') {
      const km = kmRes.value || {};

      const resourceRows = Object.values(km.resources_by_type || {}).flat();
      setResources(Array.isArray(resourceRows) ? resourceRows : []);

      const entityRows = Object.values(km.entities_by_type || {}).flat();
      setEntities(Array.isArray(entityRows) ? entityRows : []);

      if (Array.isArray(km.tables)) {
        setTables(km.tables);
      }
    }

    // Keep legacy endpoints as fallback and to preserve KB badges/overlays.
    if (tRes.status === 'fulfilled') {
      setKbs(tRes.value.kbs || []);
      if (kmRes.status !== 'fulfilled') {
        setTables(tRes.value.tables || []);
      }
    }
    if (kmRes.status !== 'fulfilled' && rRes.status === 'fulfilled') {
      setResources(rRes.value.resources || []);
    }
    if (kmRes.status !== 'fulfilled' && eRes.status === 'fulfilled') {
      setEntities(eRes.value.entities || []);
    }

    setLoading(false);
  }, [projectId]);

  useEffect(() => { load(); }, [load]);

  const filtered = tables.filter((t) => {
    if (!search.trim()) return true;
    const q = search.toLowerCase();
    return [t.table_name, t.description_zh, t.description_en,
    t.overlay?.alias, t.overlay?.description]
      .some((v) => (v || '').toLowerCase().includes(q));
  });

  const grouped = resources.reduce((acc, r) => {
    (acc[r.resource_type] = acc[r.resource_type] || []).push(r);
    return acc;
  }, {});

  function startEdit(table) {
    setEditTarget(table);
    setEditForm({
      alias: table.overlay?.alias || '',
      description: table.overlay?.description || '',
      metric_definition: table.overlay?.metric_definition || '',
    });
  }

  async function saveEdit() {
    if (!editTarget) return;
    try {
      await putCatalogOverlay(projectId, {
        kb_id: editTarget.kb_id,
        table_name: editTarget.table_name,
        ...editForm,
      });
      toast({ title: isEn ? 'Overlay saved' : '覆盖已保存' });
      setEditTarget(null);
      load();
    } catch (e) {
      toast({ title: isEn ? 'Save failed' : '保存失败', variant: 'destructive' });
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12 text-muted-foreground">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
        {isEn ? 'Loading data map...' : '加载数据地图...'}
      </div>
    );
  }

  return (
    <div className="space-y-5">
      {/* Search + status */}
      <div className="flex items-center gap-3">
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={isEn ? 'Search tables, columns, descriptions...' : '搜索表名、列名、描述...'}
            className="pl-9"
          />
        </div>
        <Button variant="outline" size="sm" onClick={load}>
          <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
          {isEn ? 'Refresh' : '刷新'}
        </Button>
      </div>

      {/* KB status badges */}
      {kbs.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {kbs.map((kb) => (
            <Badge key={kb.id} variant="outline" className="gap-1.5">
              <Database className="h-3 w-3" />
              {kb.name}
              <span className={`ml-1 rounded px-1.5 py-0.5 text-[10px] font-medium ${STATUS_COLORS[kb.catalog_status] || STATUS_COLORS.pending}`}>
                {kb.catalog_status || 'pending'}
              </span>
            </Badge>
          ))}
        </div>
      )}

      {/* Catalog tables */}
      <div>
        <h3 className="mb-2 text-sm font-medium text-foreground">
          {isEn ? 'Catalog Tables' : '目录表'}
          <span className="ml-2 text-muted-foreground">({filtered.length})</span>
        </h3>
        {filtered.length === 0 ? (
          <p className="py-6 text-center text-sm text-muted-foreground">
            {isEn ? 'No tables found. Connect a data source and trigger indexing.' : '未找到表。请连接数据源并触发索引。'}
          </p>
        ) : (
          <div className="overflow-hidden rounded-lg border border-border">
            <table className="w-full text-sm">
              <thead className="bg-secondary/50">
                <tr className="text-left text-xs text-muted-foreground">
                  <th className="px-3 py-2 font-medium">{isEn ? 'Table' : '表名'}</th>
                  <th className="px-3 py-2 font-medium">{isEn ? 'Description' : '描述'}</th>
                  <th className="px-3 py-2 font-medium">{isEn ? 'Rows' : '行数'}</th>
                  {canEdit && <th className="px-3 py-2 font-medium" />}
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {filtered.slice(0, 100).map((t) => (
                  <tr key={`${t.kb_id}-${t.table_name}`} className="hover:bg-secondary/30">
                    <td className="whitespace-nowrap px-3 py-2 font-mono text-xs text-foreground">
                      {t.table_name}
                    </td>
                    <td className="px-3 py-2 text-xs text-muted-foreground">
                      {t.overlay?.description || t.description_zh || t.description_en || (
                        <span className="italic text-muted-foreground/60">{isEn ? 'No description' : '无描述'}</span>
                      )}
                      {t.overlay && (
                        <Badge variant="outline" className="ml-1.5 py-0 text-[10px]">{isEn ? 'curated' : '已编辑'}</Badge>
                      )}
                    </td>
                    <td className="whitespace-nowrap px-3 py-2 text-xs text-muted-foreground">
                      {t.row_count != null ? t.row_count.toLocaleString() : '—'}
                    </td>
                    {canEdit && (
                      <td className="px-3 py-2">
                        <Button variant="ghost" size="sm" className="h-7 px-2" onClick={() => startEdit(t)}>
                          <Pencil className="h-3 w-3" />
                        </Button>
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
            {filtered.length > 100 && (
              <p className="bg-secondary/30 px-3 py-1.5 text-center text-xs text-muted-foreground">
                {isEn ? `Showing 100 of ${filtered.length}` : `显示 100 / ${filtered.length}`}
              </p>
            )}
          </div>
        )}
      </div>

      {/* Registry resources */}
      {resources.length > 0 && (
        <div>
          <h3 className="mb-2 text-sm font-medium text-foreground">
            {isEn ? 'Project Resources' : '项目资源'}
          </h3>
          <div className="space-y-2">
            {Object.entries(grouped).map(([type, items]) => {
              const Icon = TYPE_ICONS[type] || Tag;
              return (
                <div key={type} className="rounded-lg border border-border p-3">
                  <div className="mb-2 flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
                    <Icon className="h-3.5 w-3.5" />
                    {type}
                    <span className="text-muted-foreground/60">({items.length})</span>
                  </div>
                  <div className="space-y-1">
                    {items.map((r) => (
                      <div key={r.id} className="flex items-center justify-between rounded px-2 py-1 text-xs hover:bg-secondary/30">
                        <span className="text-foreground">{r.name}</span>
                        <div className="flex items-center gap-1.5">
                          {r.summary && (
                            <span className="max-w-xs truncate text-muted-foreground">{r.summary}</span>
                          )}
                          <Badge variant="outline" className="py-0 text-[10px]">
                            {VISIBILITY_LABELS[r.visibility] || r.visibility}
                          </Badge>
                          <span className={`rounded px-1 py-0.5 text-[10px] ${STATUS_COLORS[r.status] || STATUS_COLORS.pending}`}>
                            {r.status}
                          </span>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Entities */}
      {entities.length > 0 && (
        <div>
          <h3 className="mb-2 text-sm font-medium text-foreground">
            {isEn ? 'Project Entities' : '项目实体'}
          </h3>
          <div className="flex flex-wrap gap-1.5">
            {entities.map((e) => (
              <Badge key={e.id} variant="secondary" className="gap-1 py-1">
                <span className="text-[10px] text-muted-foreground">{e.entity_type}</span>
                {e.name}
                {e.links?.length > 0 && (
                  <span className="text-[10px] text-muted-foreground">
                    → {e.links.map((l) => l.target_id).join(', ')}
                  </span>
                )}
              </Badge>
            ))}
          </div>
        </div>
      )}

      {/* Overlay edit sheet */}
      <Sheet open={!!editTarget} onOpenChange={(v) => !v && setEditTarget(null)}>
        <SheetContent side="right" className="w-[400px] space-y-4">
          <SheetHeader>
            <SheetTitle className="font-mono text-sm">{editTarget?.table_name}</SheetTitle>
            <SheetDescription>
              {isEn ? 'Curate the catalog overlay for this table.' : '编辑此表的目录覆盖。'}
            </SheetDescription>
          </SheetHeader>
          <div className="space-y-3">
            <div>
              <label className="mb-1 block text-xs font-medium text-muted-foreground">
                {isEn ? 'Alias' : '别名'}
              </label>
              <Input
                value={editForm.alias}
                onChange={(e) => setEditForm({ ...editForm, alias: e.target.value })}
                placeholder={isEn ? 'Business alias' : '业务别名'}
              />
            </div>
            <div>
              <label className="mb-1 block text-xs font-medium text-muted-foreground">
                {isEn ? 'Description' : '描述'}
              </label>
              <Textarea
                value={editForm.description}
                onChange={(e) => setEditForm({ ...editForm, description: e.target.value })}
                rows={3}
                placeholder={isEn ? 'Business description' : '业务描述'}
              />
            </div>
            <div>
              <label className="mb-1 block text-xs font-medium text-muted-foreground">
                {isEn ? 'Metric Definition' : '指标定义'}
              </label>
              <Textarea
                value={editForm.metric_definition}
                onChange={(e) => setEditForm({ ...editForm, metric_definition: e.target.value })}
                rows={2}
                placeholder={isEn ? 'e.g. GMV = net_amount' : '例如：GMV = 净额'}
              />
            </div>
            <Button onClick={saveEdit} className="w-full">
              {isEn ? 'Save Overlay' : '保存覆盖'}
            </Button>
          </div>
        </SheetContent>
      </Sheet>
    </div>
  );
}
