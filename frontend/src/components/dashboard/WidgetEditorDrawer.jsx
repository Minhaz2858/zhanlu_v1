import { useState } from 'react';
import { previewWidgetSql } from '@/api/dashboards';

const TYPES = ['kpi', 'line', 'bar', 'pie', 'table', 'area', 'stacked-bar', 'scatter', 'gauge', 'radar'];

export default function WidgetEditorDrawer({ widget, dashboardId, params, onApply, onCancel }) {
  const [title, setTitle] = useState(widget?.title || '');
  const [type, setType] = useState(widget?.type || 'table');
  const [sql, setSql] = useState(widget?.sql || '');
  const [optionsText, setOptionsText] = useState(JSON.stringify(widget?.options || {}, null, 2));
  const [preview, setPreview] = useState(null);
  const [previewing, setPreviewing] = useState(false);

  let optionsError = '';
  let parsedOptions = {};
  try { parsedOptions = JSON.parse(optionsText || '{}'); } catch (e) { optionsError = 'Invalid JSON: ' + e.message; }

  const canApply = sql.trim() !== '' && !optionsError;

  const runPreview = async () => {
    setPreviewing(true);
    setPreview(null);
    try {
      setPreview(await previewWidgetSql(dashboardId, { sql, params }));
    } catch (e) {
      setPreview({ columns: [], rows: [], error: String(e), truncated: false });
    } finally {
      setPreviewing(false);
    }
  };

  const apply = () => {
    if (!canApply) return;
    onApply({
      id: widget?.id || ('w_' + Date.now()),
      type,
      title: title.trim() || 'Untitled',
      sql,
      options: parsedOptions,
    });
  };

  return (
    <div className="fixed inset-0 z-50 flex" role="dialog" aria-label="Widget editor">
      <div className="absolute inset-0 bg-black/40" onClick={onCancel} />
      <div className="relative ml-auto flex h-full w-full max-w-md flex-col gap-3 overflow-auto bg-background p-4 shadow-xl">
        <h3 className="text-sm font-semibold">{widget ? 'Edit widget' : 'Add widget'}</h3>
        <label className="text-xs text-muted-foreground">Title
          <input className="mt-1 w-full rounded-md border border-border bg-transparent px-2 py-1 text-sm" placeholder="Title" value={title} onChange={(e) => setTitle(e.target.value)} />
        </label>
        <label className="text-xs text-muted-foreground">Type
          <select className="mt-1 w-full rounded-md border border-border bg-transparent px-2 py-1 text-sm" value={type} onChange={(e) => setType(e.target.value)}>
            {TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        </label>
        <label className="text-xs text-muted-foreground">SQL (read-only SELECT/WITH)
          <textarea className="mt-1 h-40 w-full rounded-md border border-border bg-transparent p-2 font-mono text-xs" placeholder="SQL" spellCheck={false} value={sql} onChange={(e) => setSql(e.target.value)} />
        </label>
        <label className="text-xs text-muted-foreground">Options (JSON)
          <textarea className="mt-1 h-24 w-full rounded-md border border-border bg-transparent p-2 font-mono text-xs" value={optionsText} onChange={(e) => setOptionsText(e.target.value)} />
        </label>
        {optionsError && <p className="text-xs text-destructive">{optionsError}</p>}
        <button onClick={runPreview} disabled={previewing || !sql.trim()} className="rounded-md border border-border px-3 py-1 text-xs hover:bg-secondary disabled:opacity-50">
          {previewing ? 'Running…' : 'Run preview'}
        </button>
        {preview && (
          <div className="rounded-md border border-border p-2 text-xs">
            {preview.error ? (
              <p className="text-destructive">{preview.error}</p>
            ) : (
              <div className="overflow-auto">
                <table className="w-full">
                  <thead><tr>{preview.columns.map((c) => <th key={c} className="text-left font-medium">{c}</th>)}</tr></thead>
                  <tbody>{preview.rows.slice(0, 20).map((r, i) => (
                    <tr key={i}>{preview.columns.map((c) => <td key={c}>{String(r[c])}</td>)}</tr>
                  ))}</tbody>
                </table>
                {preview.truncated && <p className="text-muted-foreground">Truncated…</p>}
              </div>
            )}
          </div>
        )}
        <div className="mt-auto flex gap-2">
          <button onClick={apply} disabled={!canApply} className="rounded-md bg-primary px-3 py-1 text-xs text-primary-foreground disabled:opacity-50">Apply</button>
          <button onClick={onCancel} className="rounded-md border border-border px-3 py-1 text-xs">Cancel</button>
        </div>
      </div>
    </div>
  );
}
