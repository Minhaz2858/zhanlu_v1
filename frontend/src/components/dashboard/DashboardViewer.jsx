import { useEffect, useRef, useState, useCallback, forwardRef, useImperativeHandle } from 'react';
import { RefreshCw, Download, Share2, Pencil, Plus, Trash2, GripVertical } from 'lucide-react';
import { queryDashboard, updateDashboard, createDashboard } from '@/api/dashboards';
import { formatTimeOfDay } from '@/lib/time';
import html2canvas from 'html2canvas';
import WidgetShell from './widgets/WidgetShell';
import KpiWidget from './widgets/KpiWidget';
import TrendChartWidget from './widgets/TrendChartWidget';
import PieWidget from './widgets/PieWidget';
import TableWidget from './widgets/TableWidget';
import AreaChartWidget from './widgets/AreaChartWidget';
import StackedBarWidget from './widgets/StackedBarWidget';
import ScatterChartWidget from './widgets/ScatterChartWidget';
import RadarChartWidget from './widgets/RadarChartWidget';
import GaugeWidget from './widgets/GaugeWidget';
import DateRangeControl, { presetToRange } from './DateRangeControl';
import FilterChips from './FilterChips';
import DrillPanel from './DrillPanel';
import { resolveClick } from './interactions';
import WidgetEditorDrawer from './WidgetEditorDrawer';
import { reorderWidgets } from './reorder';

const DashboardViewer = forwardRef(function DashboardViewer({ dashboard, polling = true, initialDateRange, initialFilters, canEdit = false, onSaved, onCopied }, ref) {
  const [results, setResults] = useState({});
  const [refreshedAt, setRefreshedAt] = useState(null);
  const [loading, setLoading] = useState(false);
  const [dateRange, setDateRange] = useState(() => initialDateRange || presetToRange('30d'));
  const timerRef = useRef(null);
  const visibleRef = useRef(true);
  const mountedRef = useRef(true);
  const initRef = useRef(false);
  const paramsRef = useRef({});

  // Cross-widget filters + drill-down are wired in later tasks; the params
  // object below always carries the current date window so widgets using
  // :from/:to/:date tokens resolve against the selected range.
  const [filters, setFilters] = useState(() => initialFilters || {});
  const [drill, setDrill] = useState(null);

  // --- edit mode ---
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(null);
  const [dirty, setDirty] = useState(false);
  const [editingWidget, setEditingWidget] = useState(null); // widget id or "__new__"
  const [headerName, setHeaderName] = useState('');
  const editingRef = useRef(false);
  const dragId = useRef(null); // native HTML5 DnD: id of the widget being dragged
  useEffect(() => { editingRef.current = editing; }, [editing]);

  // beforeunload guard while there are unsaved edits.
  useEffect(() => {
    if (!editing || !dirty) return;
    const h = (e) => { e.preventDefault(); e.returnValue = ''; };
    window.addEventListener('beforeunload', h);
    return () => window.removeEventListener('beforeunload', h);
  }, [editing, dirty]);

  const params = {
    from: dateRange.from || undefined,
    to: dateRange.to || undefined,
    filters: Object.keys(filters).length ? filters : undefined,
    drill: drill ? { widget_id: drill.widgetId, value: drill.value } : undefined,
  };
  paramsRef.current = params;

  const handleAction = useCallback((action) => {
    if (!action) return;
    if (action.kind === 'drill') {
      setDrill({ widgetId: action.widgetId, value: action.value });
    } else if (action.kind === 'filter') {
      setFilters((f) => {
        if (f[action.token] === action.value) {
          const next = { ...f };
          delete next[action.token];
          return next; // toggle off
        }
        return { ...f, [action.token]: action.value };
      });
    }
  }, []);

  const refresh = useCallback(async (overrideParams, opts) => {
    const force = opts && opts.force;
    if (!dashboard?.id || !visibleRef.current) return;
    if (!force && editingRef.current) return; // pause polling while editing
    setLoading(true);
    try {
      const res = await queryDashboard(dashboard.id, overrideParams || paramsRef.current);
      if (!mountedRef.current) return;
      setResults(res.results || {});
      setRefreshedAt(res.refreshed_at);
    } catch (e) {
      if (mountedRef.current) setResults((r) => ({ ...r, __error: String(e) }));
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }, [dashboard?.id]);

  // Expose an imperative refresh so a parent (docked panel) can trigger an
  // immediate re-query when the agent edits the dashboard, bypassing the poll
  // interval. Does NOT touch the polling/mountedRef/visibilitychange loop.
  useImperativeHandle(ref, () => ({
    refresh: () => refresh(undefined, { force: true }),
  }), [refresh]);

  const closeDrill = useCallback(() => {
    setDrill(null);
    setResults((r) => {
      const next = { ...r };
      delete next.__drill__;
      return next;
    });
    refresh({ ...paramsRef.current, drill: undefined });
  }, [refresh]);

  useEffect(() => {
    mountedRef.current = true;
    visibleRef.current = true;
    refresh();
    if (polling) {
      const ms = Math.max(10, Math.min(300, dashboard?.refresh_interval_seconds || 30)) * 1000;
      timerRef.current = setInterval(refresh, ms);
    }
    const onVis = () => {
      visibleRef.current = !document.hidden;
      if (visibleRef.current && !document.hidden) refresh();
    };
    document.addEventListener('visibilitychange', onVis);
    return () => {
      mountedRef.current = false;
      if (timerRef.current) clearInterval(timerRef.current);
      document.removeEventListener('visibilitychange', onVis);
    };
  }, [refresh, polling, dashboard?.refresh_interval_seconds]);

  // Re-query immediately when an interaction changes params (skip the initial
  // mount — the polling effect above already refreshes on mount).
  useEffect(() => {
    if (!initRef.current) {
      initRef.current = true;
      return;
    }
    refresh();
  }, [dateRange, filters, drill, refresh]);

  const exportRef = useRef(null);

  const handleExportPng = useCallback(async () => {
    if (!exportRef.current) return;
    const bg = getComputedStyle(document.body).backgroundColor || '#ffffff';
    const canvas = await html2canvas(exportRef.current, { backgroundColor: bg, scale: 2 });
    const a = document.createElement('a');
    a.href = canvas.toDataURL('image/png');
    a.download = `${(dashboard?.name || 'dashboard').replace(/[^a-zA-Z0-9_-]/g, '_')}.png`;
    a.click();
  }, [dashboard?.name]);

  const handleShare = useCallback(() => {
    const origin = window.location.origin;
    const sp = new URLSearchParams();
    if (dateRange.from) sp.set('from', dateRange.from);
    if (dateRange.to) sp.set('to', dateRange.to);
    if (Object.keys(filters).length) sp.set('filters', JSON.stringify(filters));
    const link = `${origin}/dashboard/${dashboard?.id}${sp.toString() ? '?' + sp.toString() : ''}`;
    navigator.clipboard?.writeText(link);
  }, [dashboard?.id, dateRange, filters]);

  // --- edit-mode handlers ---
  const enterEdit = useCallback(() => {
    setDraft(JSON.parse(JSON.stringify(dashboard?.definition || { widgets: [] })));
    setHeaderName(dashboard?.name || '');
    setDirty(false);
    setEditing(true);
  }, [dashboard?.definition, dashboard?.name]);

  const exitEdit = useCallback(() => {
    setEditing(false);
    setDraft(null);
    setDirty(false);
    setEditingWidget(null);
    refresh(undefined, { force: true });
  }, [refresh]);

  const handleSave = useCallback(async () => {
    const body = {};
    if (headerName !== dashboard.name) body.name = headerName;
    if (dirty) body.definition = draft;
    if (!body.name && !body.definition) { exitEdit(); return; }
    try {
      const updated = await updateDashboard(dashboard.id, body);
      onSaved?.(updated);
      setEditing(false);
      setDraft(null);
      setDirty(false);
      setEditingWidget(null);
      refresh(undefined, { force: true });
    } catch (e) {
      // leave the user in edit mode so they can retry / copy out
    }
  }, [headerName, dashboard?.name, dashboard?.id, dirty, draft, onSaved, refresh, exitEdit]);

  const handleCancel = useCallback(() => {
    if (dirty && !window.confirm('Discard unsaved changes?')) return;
    exitEdit();
  }, [dirty, exitEdit]);

  const handleSaveAsCopy = useCallback(async () => {
    try {
      const created = await createDashboard({
        name: (headerName || dashboard?.name || 'Dashboard') + ' (copy)',
        description: dashboard?.description,
        datasource_kb_id: dashboard?.datasource_kb_id,
        project_id: dashboard?.project_id,
        project: dashboard?.project,
        definition: draft,
        refresh_interval_seconds: dashboard?.refresh_interval_seconds,
      });
      onCopied?.(created);
    } catch (e) {
      // leave in edit mode
    }
  }, [headerName, dashboard, draft, onCopied]);

  const deleteWidget = useCallback((id) => {
    setDraft((d) => ({ ...d, widgets: d.widgets.filter((w) => w.id !== id) }));
    setDirty(true);
  }, []);

  const applyWidget = useCallback((widget) => {
    setDraft((d) => {
      const exists = d.widgets.some((w) => w.id === widget.id);
      const widgets = exists ? d.widgets.map((w) => w.id === widget.id ? widget : w) : [...d.widgets, widget];
      return { ...d, widgets };
    });
    setDirty(true);
    setEditingWidget(null);
  }, []);

  // Native HTML5 DnD reorder (no dependency — @dnd-kit was unavailable in the
  // offline npm registry; spec §11 fallback). Cards are draggable only in edit
  // mode; drop reorders the draft via the pure reorderWidgets helper.
  const onCardDragStart = (id) => { dragId.current = id; };
  const onCardDragOver = (e) => { e.preventDefault(); }; // allow drop
  const onCardDrop = (id) => {
    const from = dragId.current;
    dragId.current = null;
    if (!from || from === id) return;
    setDraft((d) => ({ ...d, widgets: reorderWidgets(d.widgets, from, id) }));
    setDirty(true);
  };

  const sourceWidgets = editing && draft ? draft.widgets : (dashboard?.definition?.widgets || []);
  const widgets = sourceWidgets;
  const kpis = widgets.filter((w) => w.type === 'kpi');
  const others = widgets.filter((w) => w.type !== 'kpi');

  const editCardProps = (id) => editing ? {
    draggable: true,
    onDragStart: () => onCardDragStart(id),
    onDragOver: onCardDragOver,
    onDrop: () => onCardDrop(id),
  } : {};

  return (
    <div ref={exportRef} className="flex h-full flex-col gap-4 overflow-auto p-4">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border pb-3">
        <div className="flex items-center gap-3">
          <DateRangeControl value={dateRange} onChange={setDateRange} />
          <FilterChips filters={filters} onClear={(t) => setFilters((f) => { const n = { ...f }; delete n[t]; return n; })} />
          <p className="text-xs text-muted-foreground">
            {widgets.length} widget{widgets.length === 1 ? '' : 's'}
            {' · '}
            Updated {refreshedAt ? formatTimeOfDay(refreshedAt) : '—'}
          </p>
        </div>
        <div className="flex items-center gap-1.5">
          {editing ? (
            <>
              <input value={headerName} onChange={(e) => { setHeaderName(e.target.value); setDirty(true); }}
                className="rounded-md border border-border bg-transparent px-2 py-1 text-sm" aria-label="Dashboard name" />
              <button onClick={handleSave} className="rounded-md bg-primary px-3 py-1 text-xs text-primary-foreground">Save</button>
              <button onClick={handleCancel} className="rounded-md border border-border px-3 py-1 text-xs">Cancel</button>
              <button onClick={handleSaveAsCopy} className="rounded-md border border-border px-3 py-1 text-xs">Save as copy</button>
              <button onClick={() => setEditingWidget('__new__')} className="flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs"><Plus className="h-3 w-3" />Add widget</button>
            </>
          ) : (
            <>
              {canEdit && (
                <button onClick={enterEdit} aria-label="Edit dashboard" title="Edit dashboard"
                  className="flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground">
                  <Pencil className="h-3 w-3" />Edit
                </button>
              )}
              <button
                onClick={handleExportPng}
                aria-label="Export PNG"
                title="Export PNG"
                className="flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
              >
                <Download className="h-3 w-3" />
                Export
              </button>
              <button
                onClick={handleShare}
                aria-label="Share"
                title="Copy shareable link"
                className="flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
              >
                <Share2 className="h-3 w-3" />
                Share
              </button>
              <button
                onClick={refresh}
                aria-label="Refresh dashboard"
                className="flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
              >
                <RefreshCw className={`h-3 w-3 ${loading ? 'animate-spin' : ''}`} />
                Refresh
              </button>
            </>
          )}
        </div>
      </div>
      {kpis.length > 0 && (
        <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
          {kpis.map((w) => (
            <div key={w.id} {...editCardProps(w.id)} className={`flex flex-col gap-3 ${editing ? 'opacity-80' : ''}`}>
              {editing && (
                <div className="flex items-center gap-1">
                  <button type="button" aria-label="Drag" tabIndex={-1} className="cursor-grab rounded border border-border p-1 text-muted-foreground"><GripVertical className="h-3 w-3" /></button>
                  <button onClick={() => setEditingWidget(w.id)} aria-label="Edit widget" className="rounded border border-border p-1"><Pencil className="h-3 w-3" /></button>
                  <button onClick={() => deleteWidget(w.id)} aria-label="Delete widget" className="rounded border border-border p-1"><Trash2 className="h-3 w-3" /></button>
                </div>
              )}
              <Widget widget={w} result={results[w.id]} loading={loading} onAction={handleAction} />
            </div>
          ))}
        </div>
      )}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        {others.map((w) => (
          <div key={w.id} {...editCardProps(w.id)} className={`flex flex-col gap-3 ${editing ? 'opacity-80' : ''}`}>
            {editing && (
              <div className="flex items-center gap-1">
                <button type="button" aria-label="Drag" tabIndex={-1} className="cursor-grab rounded border border-border p-1 text-muted-foreground"><GripVertical className="h-3 w-3" /></button>
                <button onClick={() => setEditingWidget(w.id)} aria-label="Edit widget" className="rounded border border-border p-1"><Pencil className="h-3 w-3" /></button>
                <button onClick={() => deleteWidget(w.id)} aria-label="Delete widget" className="rounded border border-border p-1"><Trash2 className="h-3 w-3" /></button>
              </div>
            )}
            <Widget widget={w} result={results[w.id]} loading={loading} onAction={handleAction} />
            {results['__drill__'] && results['__drill__'].source_widget_id === (w.id || w.title) && (
              <DrillPanel result={results['__drill__']} onClose={closeDrill} />
            )}
          </div>
        ))}
      </div>
      {editingWidget && (
        <WidgetEditorDrawer
          widget={editingWidget === '__new__' ? null : (draft.widgets.find((w) => w.id === editingWidget))}
          dashboardId={dashboard.id}
          params={params}
          onApply={applyWidget}
          onCancel={() => setEditingWidget(null)}
        />
      )}
    </div>
  );
});

export default DashboardViewer;

function Widget({ widget, result, loading, onAction }) {
  const err = result?.error;
  const rows = result?.rows || [];
  const cols = result?.columns || [];
  const firstLoad = loading && result === undefined;
  const empty = !err && !firstLoad && rows.length === 0;
  const onCellClick = (column, value) => onAction && onAction(resolveClick(widget, column, value));
  return (
    <WidgetShell title={widget.title} loading={firstLoad} error={err} empty={empty}>
      {widget.type === 'kpi' ? (
        <KpiWidget widget={widget} rows={rows} />
      ) : widget.type === 'line' ? (
        <TrendChartWidget kind="line" widget={widget} rows={rows} onCellClick={onCellClick} />
      ) : widget.type === 'bar' ? (
        <TrendChartWidget kind="bar" widget={widget} rows={rows} onCellClick={onCellClick} />
      ) : widget.type === 'pie' ? (
        <PieWidget widget={widget} rows={rows} onCellClick={onCellClick} />
      ) : widget.type === 'area' ? (
        <AreaChartWidget widget={widget} rows={rows} onCellClick={onCellClick} />
      ) : widget.type === 'stacked-bar' ? (
        <StackedBarWidget widget={widget} rows={rows} onCellClick={onCellClick} />
      ) : widget.type === 'scatter' ? (
        <ScatterChartWidget widget={widget} rows={rows} onCellClick={onCellClick} />
      ) : widget.type === 'radar' ? (
        <RadarChartWidget widget={widget} rows={rows} onCellClick={onCellClick} />
      ) : widget.type === 'gauge' ? (
        <GaugeWidget widget={widget} rows={rows} />
      ) : (
        <TableWidget cols={cols} rows={rows} widget={widget} onCellClick={onCellClick} />
      )}
    </WidgetShell>
  );
}
