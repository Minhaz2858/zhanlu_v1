import { useState, useEffect, useCallback } from 'react';
import {
  Sigma, Loader2, Plus, Pencil, Check, X, Trash2, Power,
} from 'lucide-react';
import { appParams } from '@/lib/app-params';
import authFetch from '@/api/authFetch';

const APP_ID = appParams.appId || 'local-zhanlu-app';

/**
 * Editable METRIC DEFINITIONS for a database KnowledgeBase — the "did =
 * revenue vs margin vs YoY" layer of the OCI semantic store.
 *
 * A metric binds a business word ("net revenue", 净额) to its base table /
 * column / aggregation, so an ambiguous question resolves to ONE definition
 * before SQL is written. MetricDefinition rows are datasource-scoped and
 * consumed by SemanticStore.glossary as VERIFIED-tier vocabulary.
 */
export default function KbMetricsEditor({ kbId }) {
  const [metrics, setMetrics] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [adding, setAdding] = useState(false);
  const [saving, setSaving] = useState(false);
  const [draft, setDraft] = useState({
    name: '', display_name: '', description: '',
    base_table: '', base_column: '', aggregation: 'sum', unit: '', synonyms: '',
  });
  const [editingId, setEditingId] = useState(null);
  const [editDraft, setEditDraft] = useState({});

  const fetchMetrics = useCallback(async () => {
    try {
      const res = await authFetch(
        `/api/apps/${APP_ID}/knowledge_bases/${kbId}/metrics`
      );
      if (!res.ok) {
        setError(`Failed to load (${res.status})`);
        setMetrics([]);
      } else {
        setMetrics(await res.json());
        setError(null);
      }
    } catch {
      setError('Failed to load');
    } finally {
      setLoading(false);
    }
  }, [kbId]);

  useEffect(() => {
    fetchMetrics();
  }, [fetchMetrics]);

  async function addMetric() {
    if (!draft.name.trim()) return;
    setSaving(true);
    try {
      const res = await authFetch(
        `/api/apps/${APP_ID}/knowledge_bases/${kbId}/metrics`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(draft),
        }
      );
      if (res.ok) {
        setAdding(false);
        setDraft({
          name: '', display_name: '', description: '',
          base_table: '', base_column: '', aggregation: 'sum', unit: '', synonyms: '',
        });
        await fetchMetrics();
      } else {
        const detail = await res.json().catch(() => ({}));
        setError(detail.detail || `Save failed (${res.status})`);
      }
    } finally {
      setSaving(false);
    }
  }

  async function saveEdit() {
    setSaving(true);
    try {
      const res = await authFetch(
        `/api/apps/${APP_ID}/knowledge_bases/${kbId}/metrics/${editingId}`,
        {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(editDraft),
        }
      );
      if (res.ok) {
        setEditingId(null);
        await fetchMetrics();
      }
    } finally {
      setSaving(false);
    }
  }

  async function toggleEnabled(m) {
    await authFetch(
      `/api/apps/${APP_ID}/knowledge_bases/${kbId}/metrics/${m.id}`,
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ is_enabled: !m.is_enabled }),
      }
    );
    await fetchMetrics();
  }

  async function removeMetric(m) {
    if (!window.confirm(`Delete metric "${m.name}"?`)) return;
    await authFetch(
      `/api/apps/${APP_ID}/knowledge_bases/${kbId}/metrics/${m.id}`,
      { method: 'DELETE' }
    );
    await fetchMetrics();
  }

  if (loading) {
    return (
      <div className="px-6 pt-5 flex items-center gap-2 text-sm text-stone-400">
        <Loader2 size={14} className="animate-spin" /> Loading metrics…
      </div>
    );
  }

  return (
    <div className="px-6 py-5 border-t border-stone-100">
      <div className="flex items-center gap-2 flex-wrap">
        <Sigma size={16} className="text-amber-600" />
        <span className="text-sm font-semibold text-stone-800">
          Metric Definitions
        </span>
        <span className="text-xs text-stone-400">
          bind business words to table.column + aggregation — “did” resolves to ONE definition
        </span>
        <button
          onClick={() => setAdding((v) => !v)}
          className="ml-auto flex items-center gap-1 text-xs px-2.5 py-1 rounded-md bg-amber-600 text-white hover:bg-amber-700"
        >
          <Plus size={13} /> Add metric
        </button>
      </div>

      {error && <p className="mt-3 text-xs text-red-600">{error}</p>}

      {adding && (
        <div className="mt-3 rounded-lg border border-amber-200 bg-amber-50/40 p-3 space-y-2">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
            <input
              value={draft.name}
              onChange={(e) => setDraft({ ...draft, name: e.target.value })}
              placeholder="Metric name * (e.g. net_revenue / 净收入)"
              className="text-[13px] border border-stone-300 rounded px-2 py-1.5 focus:outline-none focus:ring-1 focus:ring-amber-400"
            />
            <input
              value={draft.display_name}
              onChange={(e) => setDraft({ ...draft, display_name: e.target.value })}
              placeholder="Display name (optional)"
              className="text-[13px] border border-stone-300 rounded px-2 py-1.5 focus:outline-none focus:ring-1 focus:ring-amber-400"
            />
            <input
              value={draft.base_table}
              onChange={(e) => setDraft({ ...draft, base_table: e.target.value })}
              placeholder="Base table (e.g. erp_v_sale_orderentry)"
              className="text-[13px] font-mono border border-stone-300 rounded px-2 py-1.5 focus:outline-none focus:ring-1 focus:ring-amber-400"
            />
            <input
              value={draft.base_column}
              onChange={(e) => setDraft({ ...draft, base_column: e.target.value })}
              placeholder="Base column (e.g. FAMOUNT)"
              className="text-[13px] font-mono border border-stone-300 rounded px-2 py-1.5 focus:outline-none focus:ring-1 focus:ring-amber-400"
            />
            <select
              value={draft.aggregation}
              onChange={(e) => setDraft({ ...draft, aggregation: e.target.value })}
              className="text-[13px] border border-stone-300 rounded px-2 py-1.5 focus:outline-none focus:ring-1 focus:ring-amber-400"
            >
              {['sum', 'avg', 'count', 'min', 'max', 'distinct_count', 'custom'].map((a) => (
                <option key={a} value={a}>{a}</option>
              ))}
            </select>
            <input
              value={draft.unit}
              onChange={(e) => setDraft({ ...draft, unit: e.target.value })}
              placeholder="Unit (e.g. CNY, %, units)"
              className="text-[13px] border border-stone-300 rounded px-2 py-1.5 focus:outline-none focus:ring-1 focus:ring-amber-400"
            />
          </div>
          <textarea
            value={draft.description}
            onChange={(e) => setDraft({ ...draft, description: e.target.value })}
            placeholder="Definition — what this metric means for the business (FAMOUNT = revenue net of tax; FALLAMOUNT = gross)"
            rows={2}
            className="w-full text-[13px] border border-stone-300 rounded px-2 py-1.5 focus:outline-none focus:ring-1 focus:ring-amber-400"
          />
          <input
            value={draft.synonyms}
            onChange={(e) => setDraft({ ...draft, synonyms: e.target.value })}
            placeholder="Synonyms, comma-separated (revenue, sales amount, 销售额)"
            className="w-full text-[13px] border border-stone-300 rounded px-2 py-1.5 focus:outline-none focus:ring-1 focus:ring-amber-400"
          />
          <div className="flex gap-2">
            <button
              onClick={addMetric}
              disabled={saving || !draft.name.trim()}
              className="flex items-center gap-1 text-xs px-3 py-1 rounded bg-emerald-600 text-white hover:bg-emerald-700 disabled:opacity-50"
            >
              <Check size={12} /> {saving ? 'Saving…' : 'Save metric'}
            </button>
            <button
              onClick={() => setAdding(false)}
              className="text-xs px-3 py-1 rounded border border-stone-300 text-stone-600 hover:bg-stone-100"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {metrics.length === 0 && !adding ? (
        <p className="mt-3 text-xs text-stone-400">
          No metric definitions yet. Define the business metrics your analysts
          mean by “revenue”, “growth”, “margin” — ambiguous questions then
          resolve to ONE definition instead of a guess.
        </p>
      ) : (
        <div className="mt-3 rounded-lg border border-stone-200 divide-y divide-stone-100 overflow-hidden">
          {metrics.map((m) => (
            <div key={m.id} className="px-4 py-2.5 flex items-start gap-3">
              {editingId === m.id ? (
                <div className="flex-1 space-y-1.5">
                  <div className="grid grid-cols-2 gap-2">
                    <input
                      value={editDraft.base_table || ''}
                      onChange={(e) => setEditDraft({ ...editDraft, base_table: e.target.value })}
                      placeholder="base table"
                      className="text-[12px] font-mono border border-stone-300 rounded px-2 py-1"
                    />
                    <input
                      value={editDraft.base_column || ''}
                      onChange={(e) => setEditDraft({ ...editDraft, base_column: e.target.value })}
                      placeholder="base column"
                      className="text-[12px] font-mono border border-stone-300 rounded px-2 py-1"
                    />
                    <input
                      value={editDraft.aggregation || ''}
                      onChange={(e) => setEditDraft({ ...editDraft, aggregation: e.target.value })}
                      placeholder="aggregation"
                      className="text-[12px] border border-stone-300 rounded px-2 py-1"
                    />
                    <input
                      value={editDraft.unit || ''}
                      onChange={(e) => setEditDraft({ ...editDraft, unit: e.target.value })}
                      placeholder="unit"
                      className="text-[12px] border border-stone-300 rounded px-2 py-1"
                    />
                  </div>
                  <textarea
                    value={editDraft.description || ''}
                    onChange={(e) => setEditDraft({ ...editDraft, description: e.target.value })}
                    rows={2}
                    className="w-full text-[12px] border border-stone-300 rounded px-2 py-1"
                  />
                  <div className="flex gap-1">
                    <button
                      onClick={saveEdit}
                      disabled={saving}
                      className="flex items-center gap-1 text-[11px] px-2 py-0.5 rounded bg-emerald-600 text-white disabled:opacity-50"
                    >
                      <Check size={11} /> Save
                    </button>
                    <button
                      onClick={() => setEditingId(null)}
                      className="flex items-center gap-1 text-[11px] px-2 py-0.5 rounded border border-stone-300 text-stone-600"
                    >
                      <X size={11} /> Cancel
                    </button>
                  </div>
                </div>
              ) : (
                <>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-[13px] font-medium text-stone-800">
                        {m.display_name || m.name}
                      </span>
                      <span className="text-[11px] font-mono text-stone-500">
                        {m.aggregation || '—'}({m.base_column || '—'}) @ {m.base_table || '—'}
                      </span>
                      {m.unit && (
                        <span className="text-[10px] px-1.5 py-0.5 rounded border border-stone-200 text-stone-500">
                          {m.unit}
                        </span>
                      )}
                      <span
                        className={`text-[10px] px-1.5 py-0.5 rounded border ${
                          m.is_enabled
                            ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
                            : 'border-stone-200 text-stone-400'
                        }`}
                      >
                        {m.is_enabled ? 'VERIFIED · active' : 'disabled'}
                      </span>
                    </div>
                    <p className="text-xs text-stone-500 mt-0.5">{m.description || '—'}</p>
                    {(m.synonyms || []).length > 0 && (
                      <p className="text-[11px] text-stone-400 mt-0.5">
                        aka: {m.synonyms.join(', ')}
                      </p>
                    )}
                  </div>
                  <div className="flex items-center gap-1 shrink-0">
                    <button
                      onClick={() => toggleEnabled(m)}
                      title={m.is_enabled ? 'Disable' : 'Enable'}
                      className={`p-1 rounded ${
                        m.is_enabled
                          ? 'text-emerald-600 hover:bg-emerald-50'
                          : 'text-stone-300 hover:bg-stone-100'
                      }`}
                    >
                      <Power size={13} />
                    </button>
                    <button
                      onClick={() => {
                        setEditingId(m.id);
                        setEditDraft({ ...m });
                      }}
                      className="p-1 rounded text-stone-400 hover:text-amber-600 hover:bg-amber-50"
                    >
                      <Pencil size={13} />
                    </button>
                    <button
                      onClick={() => removeMetric(m)}
                      className="p-1 rounded text-stone-400 hover:text-red-600 hover:bg-red-50"
                    >
                      <Trash2 size={13} />
                    </button>
                  </div>
                </>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
