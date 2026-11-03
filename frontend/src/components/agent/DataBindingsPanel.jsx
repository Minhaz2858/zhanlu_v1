/**
 * Phase 8 T8-4 — binding editor (descriptor mode).
 *
 * Renders the agent's bindings from the structured source-descriptor
 * contract and drives every change through the binding API:
 *   POST   /api/agent-studio/{agentId}/bindings/data        (add)
 *   PATCH  /api/agent-studio/{agentId}/bindings/data/{id}   (enable/disable)
 *   DELETE /api/agent-studio/{agentId}/bindings/data/{id}   (revoke)
 * After every successful mutation the parent's form projection
 * (form.knowledge_bases) is re-synced from the live descriptor set so the
 * legacy JSON path stays consistent. The agent's own binding projection on
 * the backend is maintained by the binding service (same transaction).
 */
import { useState, useEffect, useCallback } from 'react';
import { Database, FileText, Plus, X, Search, Loader2, Power } from 'lucide-react';
import { authFetch } from '@/api/authFetch';
import { base44 } from '@/api/base44Client';

export default function DataBindingsPanel({ agentId, form, update, t }) {
  const [descriptors, setDescriptors] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [busyId, setBusyId] = useState(null);
  const [showPicker, setShowPicker] = useState(false);
  const [pickerKbs, setPickerKbs] = useState([]);
  const [query, setQuery] = useState('');
  const [error, setError] = useState(null);

  const loadDescriptors = useCallback(async () => {
    try {
      const res = await authFetch(
        `/api/agent-studio/${agentId}/source-descriptors`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const body = await res.json();
      setDescriptors(body.descriptors || []);
      // form projection mirrors the LIVE (non-revoked) binding set —
      // disabled bindings stay listed (they are bound, just switched off)
      update({ knowledge_bases: (body.descriptors || []).map((d) => d.id) });
    } catch (err) {
      setLoadError(err?.message || 'Failed to load bindings');
      setDescriptors(null); // parent falls back to the legacy picker
    }
  }, [agentId, update]);

  useEffect(() => { loadDescriptors(); }, [loadDescriptors]);

  async function mutate(id, method, body) {
    setBusyId(id);
    setError(null);
    try {
      const res = await authFetch(
        `/api/agent-studio/${agentId}/bindings/data/${id}`,
        {
          method,
          headers: { 'Content-Type': 'application/json' },
          body: body ? JSON.stringify(body) : undefined,
        });
      if (!res.ok) {
        const b = await res.json().catch(() => ({}));
        throw new Error(b.detail || `HTTP ${res.status}`);
      }
      await loadDescriptors();
    } catch (err) {
      setError(err?.message || 'Binding change failed');
    } finally {
      setBusyId(null);
    }
  }

  async function addBinding(dsId) {
    setBusyId(dsId);
    setError(null);
    try {
      const res = await authFetch(
        `/api/agent-studio/${agentId}/bindings/data`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ datasource_id: dsId }),
        });
      if (!res.ok) {
        const b = await res.json().catch(() => ({}));
        throw new Error(b.detail || `HTTP ${res.status}`);
      }
      setShowPicker(false);
      await loadDescriptors();
    } catch (err) {
      setError(err?.message || 'Binding failed');
    } finally {
      setBusyId(null);
    }
  }

  async function openPicker() {
    setShowPicker(true);
    try {
      const kbs = await base44.entities.KnowledgeBase.list();
      setPickerKbs(kbs || []);
    } catch { setPickerKbs([]); }
  }

  if (!descriptors) return null; // load error → parent renders legacy UI

  const bound = descriptors;
  const pickerList = pickerKbs.filter(
    (kb) => (kb.source_kind === 'database' || kb.source_kind === 'file')
      && !bound.some((d) => d.id === kb.id)
      && (kb.name || '').toLowerCase().includes(query.toLowerCase()));

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-2">
        {bound.map((d) => {
          const isFile = d.resource_type !== 'database';
          const Icon = isFile ? FileText : Database;
          return (
            <span key={d.binding_id || d.id}
              className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-secondary/50 px-2.5 py-1.5 text-xs text-foreground">
              <Icon className="h-3 w-3 text-primary" />
              <span>{d.name || d.id}</span>
              {d.is_enabled
                ? (
                  <button
                    aria-label={`Disable ${d.name || d.id}`}
                    disabled={busyId === d.binding_id}
                    onClick={() => mutate(d.binding_id, 'PATCH',
                      { is_enabled: false })}
                    className="text-muted-foreground hover:text-amber-600">
                    <Power className="h-3 w-3" />
                  </button>
                )
                : (
                  <>
                    <span className="rounded bg-amber-500/10 px-1 py-0.5 text-[10px] text-amber-600">disabled</span>
                    <button
                      aria-label={`Enable ${d.name || d.id}`}
                      disabled={busyId === d.binding_id}
                      onClick={() => mutate(d.binding_id, 'PATCH',
                        { is_enabled: true })}
                      className="text-muted-foreground hover:text-primary">
                      <Power className="h-3 w-3" />
                    </button>
                  </>
                )}
              <button
                aria-label={`Remove ${d.name || d.id}`}
                disabled={busyId === d.binding_id}
                onClick={() => mutate(d.binding_id, 'DELETE')}
                className="text-muted-foreground hover:text-destructive">
                <X className="h-3 w-3" />
              </button>
            </span>
          );
        })}
        {bound.length === 0 && (
          <span className="text-xs text-muted-foreground">
            {t?.agentConfig?.noDataSources || 'No data sources bound'}
          </span>
        )}
      </div>

      {busyId && (
        <div className="flex items-center gap-1.5 text-[10px] text-muted-foreground">
          <Loader2 className="h-3 w-3 animate-spin" /> Saving…
        </div>
      )}
      {error && (
        <div className="text-[10px] text-destructive">{error}</div>
      )}

      {showPicker ? (
        <div className="rounded-lg border border-border bg-background p-2">
          <div className="mb-2 flex items-center gap-2">
            <Search className="h-3.5 w-3.5 text-muted-foreground" />
            <input value={query} onChange={(e) => setQuery(e.target.value)}
              placeholder={t?.agentConfig?.searchDataSources || 'Search…'}
              className="flex-1 bg-transparent text-xs focus:outline-none"
              autoFocus />
            <button onClick={() => setShowPicker(false)}
              className="text-muted-foreground hover:text-foreground">
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
          <div className="max-h-48 space-y-1 overflow-y-auto">
            {pickerList.map((kb) => (
              <button key={kb.id}
                onClick={() => addBinding(kb.id)}
                className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs hover:bg-secondary">
                <Plus className="h-3 w-3 text-muted-foreground" />
                {kb.source_kind === 'file'
                  ? <FileText className="h-3 w-3 text-primary" />
                  : <Database className="h-3 w-3 text-primary" />}
                <span className="flex-1 text-foreground">{kb.name}</span>
                <span className="text-[10px] text-muted-foreground">
                  {kb.db_type || kb.file_type || ''}
                </span>
              </button>
            ))}
          </div>
        </div>
      ) : (
        <button onClick={openPicker}
          className="inline-flex items-center gap-1.5 rounded-lg border border-dashed border-border px-3 py-1.5 text-xs text-muted-foreground hover:border-primary/40 hover:text-foreground">
          <Plus className="h-3.5 w-3.5" />
          {t?.agentConfig?.addDataSource || 'Add Data Source'}
        </button>
      )}
    </div>
  );
}
