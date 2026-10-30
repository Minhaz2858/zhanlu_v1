import { useState, useEffect, useCallback, useMemo } from 'react';
import {
  Building2, RefreshCw, Loader2, Download, MapPin, Radar, Plus,
  CircleDot, Layers, Database, ShieldCheck, History,
} from 'lucide-react';
import { toast } from 'sonner';
import { authFetch } from '@/api/authFetch';
import { EVIDENCE_BADGE_STYLE } from '@/lib/evidenceStatuses';
import OntologyGraph from '@/components/ontology/OntologyGraph';

/**
 * Enterprise Profile — the org's ontology coverage map (P0-2/P1-1).
 *
 * One admin page that makes the semantic layer visible and honest:
 * - Domain coverage cards (live / wired / drafted — never inflated)
 * - Per-domain object wiring (backing kind + table ref + mapped columns)
 * - Auto-discovery seeding: match unwired objects to real DB schemas
 * - PO draft worklist + xlsx export (the first approval-gated write-back)
 */

const READINESS_STYLE = {
  live: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  wired: 'bg-sky-50 text-sky-700 border-sky-200',
  drafted: 'bg-stone-100 text-stone-500 border-stone-200',
};

const BACKING_ICON = { table: Database, entity: CircleDot, metric: CircleDot, manual: MapPin };

function BackingBadge({ obj }) {
  const kind = obj.backing_kind || 'manual';
  const Icon = BACKING_ICON[kind] || MapPin;
  const isWired = kind !== 'manual' && obj.backing_ref;
  const ref = isWired ? (obj.backing_ref.startsWith('kb:')
    ? obj.backing_ref.split(':').slice(2).join(':')
    : obj.backing_ref) : null;
  return (
    <span
      className={`inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full border ${
        isWired ? 'border-emerald-200 bg-emerald-50 text-emerald-700' : 'border-stone-200 bg-stone-50 text-stone-400'
      }`}
      title={obj.backing_ref || 'manual — not yet wired to live data'}
    >
      <Icon size={10} />
      {isWired ? ref : 'manual'}
    </span>
  );
}

function CoverageCard({ domain, value, onClick, selected }) {
  const cov = domain.coverage || {};
  const readiness = cov.readiness || 'drafted';
  return (
    <button
      onClick={onClick}
      className={`text-left rounded-xl border px-4 py-3.5 transition-colors ${
        selected ? 'border-stone-400 bg-stone-50' : 'border-stone-200 bg-white hover:border-stone-300'
      }`}
    >
      <div className="flex items-center gap-2">
        <span className="text-[13px] font-medium text-stone-800 truncate">{domain.name}</span>
        <span className={`ml-auto text-[10px] px-1.5 py-0.5 rounded-full border capitalize ${READINESS_STYLE[readiness]}`}>
          {readiness}
        </span>
      </div>
      <p className="mt-1 text-[11px] text-stone-400 line-clamp-1">{domain.description}</p>
      <div className="mt-2 flex items-center gap-3 text-[10px] text-stone-500">
        <span>{domain.objects} objects</span>
        <span className="text-emerald-600">{cov.objects_live ?? 0} live</span>
        <span>{cov.actions_executable ?? 0} actions runnable</span>
      </div>
      {value && (value.est_value > 0 || value.agents?.length > 0) && (
        <div className="mt-1.5 flex items-center gap-3 text-[10px] text-stone-400">
          {value.est_value > 0 && (
            <span className="text-amber-700">≈ ${value.est_value.toLocaleString()} est. value</span>
          )}
          {value.agents?.length > 0 && <span>{value.agents.length} agent{value.agents.length === 1 ? '' : 's'}</span>}
          {value.acceptance_rate != null && (
            <span>{Math.round(value.acceptance_rate * 100)}% accepted</span>
          )}
          {value.csat != null && (
            <span>CSAT {Math.round(value.csat)}</span>
          )}
        </div>
      )}
    </button>
  );
}

function RevisionsFeed() {
  // P2: append-only ontology revision history (GET /api/ontology/revisions).
  const [open, setOpen] = useState(false);
  const [revisions, setRevisions] = useState(null);
  const load = async () => {
    try {
      const res = await authFetch('/api/ontology/revisions');
      const data = await res.json();
      setRevisions(data.revisions || []);
    } catch {
      setRevisions([]);
    }
  };
  useEffect(() => { if (open && revisions === null) load(); }, [open, revisions]);
  return (
    <div className="bg-white rounded-lg border border-stone-200">
      <button onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-2 px-4 py-3 text-left">
        <History size={14} className="text-stone-500" />
        <h2 className="text-sm font-semibold text-stone-800">Ontology Revision History</h2>
        <span className="text-[11px] text-stone-400">append-only — every mutation, who made it, when</span>
        <span className="ml-auto text-stone-400 text-xs">{open ? 'hide' : 'show'}</span>
      </button>
      {open && (
        <div className="px-4 pb-3 max-h-72 overflow-y-auto">
          {!revisions && <div className="text-[11px] text-stone-400 py-2">Loading…</div>}
          {revisions && revisions.length === 0 &&
            <div className="text-[11px] text-stone-400 py-2">No revisions yet — seed or bind something.</div>}
          {revisions && revisions.map((r) => (
            <div key={r.id} className="flex items-center gap-2 py-1.5 border-b border-stone-100 last:border-0 text-[11px]">
              <span className="font-mono text-stone-400">#{r.revision_no}</span>
              <span className="px-1.5 py-0.5 rounded-full border border-stone-200 bg-stone-50 text-stone-600 capitalize">{r.change_kind}</span>
              <span className="text-stone-600 capitalize">{r.entity_type}</span>
              <span className="text-stone-400 truncate max-w-[240px]" title={r.entity_id}>{r.entity_id.slice(0, 8)}…</span>
              <span className="text-stone-400">{r.changed_by || '—'}</span>
              <span className="ml-auto text-stone-300">{(r.created_date || '').replace('T', ' ').slice(0, 16)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function EvidenceBadge({ obj }) {
  // Canonical statuses: frontend/src/lib/evidenceStatuses.js mirrors
  // backend services/ontology/evidence.py (single source of truth).
  const status = EVIDENCE_BADGE_STYLE[obj.evidence_status]
    ? obj.evidence_status
    : 'proposed';
  const conf =
    obj.confidence != null ? ` · ${Math.round(obj.confidence * 100)}%` : '';
  return (
    <span
      className={`inline-flex items-center text-[10px] px-1.5 py-0.5 rounded-full border capitalize ${EVIDENCE_BADGE_STYLE[status]}`}
      title={
        obj.provenance
          ? JSON.stringify(obj.provenance)
          : `evidence status: ${status}`
      }
    >
      {status}
      {conf}
    </span>
  );
}

function ObjectsPanel({ domain, objects }) {
  const domObjects = objects.filter((o) => o.domain_id === domain.id);
  return (
    <div className="rounded-xl border border-stone-200 bg-white px-4 py-4">
      <div className="flex items-center gap-2">
        <Layers size={14} className="text-stone-500" />
        <h3 className="text-[13px] font-semibold text-stone-800">
          {domain.name} — objects
        </h3>
      </div>
      {domObjects.length === 0 ? (
        <p className="mt-2 text-[11px] text-stone-400">No objects in this domain.</p>
      ) : (
        <ul className="mt-2 divide-y divide-stone-100">
          {domObjects.map((o) => (
            <li key={o.id} className="py-2">
              <div className="flex items-center gap-2">
                <span className="text-[12px] text-stone-700 font-medium">{o.display_name || o.name}</span>
                <BackingBadge obj={o} />
                <EvidenceBadge obj={o} />
                {o.properties?.length > 0 && (
                  <span className="ml-auto text-[10px] text-stone-400 truncate max-w-[45%]" title={
                    o.properties
                      .map((p) => `${p.name}${p.column ? `→${p.column}` : ''}`)
                      .join(', ')
                  }>
                    {o.properties.map((p) => p.name).join(', ')}
                  </span>
                )}
              </div>
              {/* P4 T9 — document-grounded provenance. The backend already
                  filtered these entries per viewer (D6/D7): what arrives is
                  exactly what this principal may see, so rendering it is
                  safe. Each entry is a navigable citation: source document
                  (kb) + page/sheet/section + the verbatim quote. */}
              {(o.provenance?.document_provenance || []).length > 0 && (
                <details className="mt-1">
                  <summary className="cursor-pointer text-[10px] text-stone-500 hover:text-stone-700">
                    document evidence ({o.provenance.document_provenance.length})
                  </summary>
                  <ul className="mt-1 space-y-1.5 border-l-2 border-stone-200 pl-2.5">
                    {o.provenance.document_provenance.map((e, i) => (
                      <li key={`${e.doc_version_id}-${i}`} className="text-[11px]">
                        <span className="text-stone-600">
                          {e.location?.page != null ? `page ${e.location.page}` : ''}
                          {e.location?.sheet ? ` sheet ${e.location.sheet}` : ''}
                          {e.location?.section ? ` § ${e.location.section}` : ''}
                          {(e.location?.page != null || e.location?.sheet || e.location?.section) ? ' — ' : ''}
                          doc <span className="font-mono">{(e.doc_version_id || '').slice(0, 8)}</span>
                        </span>
                        <p className="italic text-stone-500">“{e.quote}”</p>
                      </li>
                    ))}
                  </ul>
                </details>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function PoDraftsCard({ onExport, exporting }) {
  const [drafts, setDrafts] = useState(null);

  const load = useCallback(async () => {
    try {
      const res = await authFetch('/api/ontology/po-drafts');
      if (res.ok) setDrafts(await res.json());
    } catch {
      /* fail-quiet */
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  if (!drafts) return null;

  return (
    <div className="mt-8">
      <div className="flex items-center gap-2">
        <ShieldCheck size={15} className="text-emerald-600" />
        <h2 className="text-sm font-semibold text-stone-800">PO Draft Worklist</h2>
        <span className="text-[11px] text-stone-400">
          approval-gated write-backs — every draft came from an approved action
        </span>
        <button
          onClick={onExport}
          disabled={exporting}
          className="ml-auto flex items-center gap-1.5 text-[11px] px-2.5 py-1.5 rounded-md border border-stone-300 bg-white text-stone-700 hover:bg-stone-50 disabled:opacity-50"
        >
          {exporting ? <Loader2 size={12} className="animate-spin" /> : <Download size={12} />}
          Export xlsx
        </button>
      </div>
      {drafts.length === 0 ? (
        <p className="mt-3 text-[11px] text-stone-400">
          No PO drafts yet. They are created when an approved ontology write action runs
          (e.g. a procurement agent's "create PO draft" step).
        </p>
      ) : (
        <div className="mt-3 overflow-x-auto rounded-xl border border-stone-200">
          <table className="min-w-full text-[11px]">
            <thead>
              <tr className="bg-stone-50 text-stone-500 text-left">
                <th className="px-3 py-2 font-medium">Supplier</th>
                <th className="px-3 py-2 font-medium">Product</th>
                <th className="px-3 py-2 font-medium text-right">Qty</th>
                <th className="px-3 py-2 font-medium text-right">Est. Amount</th>
                <th className="px-3 py-2 font-medium">Status</th>
                <th className="px-3 py-2 font-medium">Created</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-stone-100 text-stone-700">
              {drafts.map((d) => (
                <tr key={d.id}>
                  <td className="px-3 py-2">{d.supplier_name}</td>
                  <td className="px-3 py-2">{d.product}</td>
                  <td className="px-3 py-2 text-right">{d.quantity} {d.unit}</td>
                  <td className="px-3 py-2 text-right">
                    {d.currency} {(d.est_amount ?? 0).toLocaleString()}
                  </td>
                  <td className="px-3 py-2">
                    <span className={`px-1.5 py-0.5 rounded-full border text-[10px] ${
                      d.status === 'approved'
                        ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
                        : 'border-amber-200 bg-amber-50 text-amber-700'
                    }`}>
                      {d.status}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-stone-400">
                    {d.created_date ? new Date(d.created_date).toLocaleDateString() : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function SensorsCard({ reloadSignal }) {
  // P3-2: scheduled data watches → ontology fan-out on breach.
  const [data, setData] = useState(null);
  const [creating, setCreating] = useState(false);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({
    name: '', object_name: '', kb_id: '', watch_sql: '',
    op: 'gt', threshold: 0, schedule_seconds: 3600,
  });

  const load = useCallback(async () => {
    try {
      const res = await authFetch('/api/ontology/sensors');
      if (res.ok) setData(await res.json());
    } catch { /* fail-quiet */ }
  }, []);

  useEffect(() => { load(); }, [load, reloadSignal]);

  const create = async () => {
    if (!form.name || !form.object_name || !form.kb_id || !form.watch_sql.trim()) {
      toast.error('Name, object, data source and watch SQL are required');
      return;
    }
    setCreating(true);
    try {
      const res = await authFetch('/api/ontology/sensors', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(form),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      toast.success('Sensor created — the loop will watch it every minute');
      setShowForm(false);
      setForm({ ...form, name: '', watch_sql: '' });
      await load();
    } catch (e) {
      toast.error(`Create failed: ${e.message || e}`);
    } finally {
      setCreating(false);
    }
  };

  const runNow = async (id) => {
    try {
      const res = await authFetch(`/api/ontology/sensors/${id}/run`, { method: 'POST' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const out = await res.json();
      if (out.error) toast.error(`Sensor error: ${out.error}`);
      else if (out.transition === 'breach') toast.warning(`BREACH — value ${out.value} · agents notified`);
      else if (out.transition === 'recovery') toast.success(`Recovered — value ${out.value}`);
      else toast.info(`Value ${out.value} — ${out.breached ? 'still in breach' : 'within limits'}`);
      await load();
    } catch (e) {
      toast.error(`Run failed: ${e.message || e}`);
    }
  };

  const remove = async (id) => {
    try {
      const res = await authFetch(`/api/ontology/sensors/${id}`, { method: 'DELETE' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      toast.success('Sensor removed');
      await load();
    } catch (e) {
      toast.error(`Delete failed: ${e.message || e}`);
    }
  };

  if (!data) return null;
  const stateChip = (s) => {
    let st = {};
    try { st = JSON.parse(s.last_state_json || '{}'); } catch { /* empty */ }
    if (!s.enabled) return ['paused', 'border-stone-200 bg-stone-50 text-stone-400'];
    if (st.breached) return ['BREACH', 'border-red-200 bg-red-50 text-red-700'];
    if (s.last_run_at) return ['watching', 'border-emerald-200 bg-emerald-50 text-emerald-700'];
    return ['new', 'border-sky-200 bg-sky-50 text-sky-700'];
  };

  return (
    <div className="mt-8">
      <div className="flex items-center gap-2">
        <Radar size={15} className="text-stone-600" />
        <h2 className="text-sm font-semibold text-stone-800">Sensing</h2>
        <span className="text-[11px] text-stone-400">
          scheduled watches on your real data — on breach, linked-domain agents are notified
          (breach fires once, recovery fires once — never spam)
        </span>
        <button
          onClick={() => setShowForm((v) => !v)}
          className="ml-auto flex items-center gap-1.5 text-[11px] px-2.5 py-1.5 rounded-md border border-stone-300 bg-white text-stone-700 hover:bg-stone-50"
        >
          <Plus size={12} /> New sensor
        </button>
      </div>

      {showForm && (
        <div className="mt-3 rounded-xl border border-stone-200 bg-white p-4 grid grid-cols-1 sm:grid-cols-2 gap-3 text-[12px]">
          <input className="border border-stone-200 rounded-md px-2.5 py-1.5" placeholder="Sensor name (e.g. stalled PO watch)"
            value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <select className="border border-stone-200 rounded-md px-2.5 py-1.5"
            value={form.object_name} onChange={(e) => setForm({ ...form, object_name: e.target.value })}>
            <option value="">Notify object…</option>
            {data.objects.map((o) => (
              <option key={o.name} value={o.name}>{o.display_name || o.name}</option>
            ))}
          </select>
          <select className="border border-stone-200 rounded-md px-2.5 py-1.5"
            value={form.kb_id} onChange={(e) => setForm({ ...form, kb_id: e.target.value })}>
            <option value="">Data source…</option>
            {data.data_sources.map((k) => (
              <option key={k.id} value={k.id}>{k.name} ({k.db_type || 'database'})</option>
            ))}
          </select>
          <div className="flex items-center gap-2">
            <select className="border border-stone-200 rounded-md px-2 py-1.5"
              value={form.op} onChange={(e) => setForm({ ...form, op: e.target.value })}>
              {['gt', 'gte', 'lt', 'lte', 'eq'].map((o) => <option key={o} value={o}>{o}</option>)}
            </select>
            <input type="number" step="any" className="w-24 border border-stone-200 rounded-md px-2.5 py-1.5"
              value={form.threshold} onChange={(e) => setForm({ ...form, threshold: Number(e.target.value) })} />
            <select className="border border-stone-200 rounded-md px-2 py-1.5"
              value={form.schedule_seconds} onChange={(e) => setForm({ ...form, schedule_seconds: Number(e.target.value) })}>
              <option value={300}>every 5 min</option>
              <option value={3600}>every hour</option>
              <option value={21600}>every 6 h</option>
              <option value={86400}>daily</option>
            </select>
          </div>
          <textarea className="sm:col-span-2 border border-stone-200 rounded-md px-2.5 py-1.5 font-mono text-[11px]"
            rows={2} placeholder="Watch SQL — must return ONE numeric value, e.g. SELECT COUNT(*) FROM deals WHERE stage = 'stalled'"
            value={form.watch_sql} onChange={(e) => setForm({ ...form, watch_sql: e.target.value })} />
          <div className="sm:col-span-2 flex items-center gap-2">
            <button onClick={create} disabled={creating}
              className="flex items-center gap-1.5 text-[11px] px-3 py-1.5 rounded-md bg-stone-800 text-white hover:bg-stone-700 disabled:opacity-50">
              {creating ? <Loader2 size={12} className="animate-spin" /> : <ShieldCheck size={12} />}
              Create sensor
            </button>
            <span className="text-[10px] text-stone-400">
              runs through the same read-only gate as chat agents — write SQL is rejected
            </span>
          </div>
        </div>
      )}

      {data.sensors.length === 0 ? (
        <p className="mt-3 text-[11px] text-stone-400">
          No sensors yet. A sensor watches a query on one of your connected data sources and
          notifies the agents bound to the watched object's linked domains when the value crosses
          your threshold.
        </p>
      ) : (
        <div className="mt-3 space-y-2">
          {data.sensors.map((s) => {
            const [label, cls] = stateChip(s);
            return (
              <div key={s.id} className="rounded-xl border border-stone-200 bg-white px-4 py-3">
                <div className="flex items-center gap-2">
                  <span className="text-[13px] font-medium text-stone-800">{s.name}</span>
                  <span className={`text-[10px] px-1.5 py-0.5 rounded-full border ${cls}`}>{label}</span>
                  <span className="text-[10px] text-stone-400">
                    watches {s.object_name} · {s.op} {s.threshold} · every {s.schedule_seconds >= 3600 ? `${s.schedule_seconds / 3600}h` : `${s.schedule_seconds / 60}m`}
                  </span>
                  <div className="ml-auto flex items-center gap-1.5">
                    <button onClick={() => runNow(s.id)}
                      className="text-[11px] px-2 py-1 rounded-md border border-stone-300 text-stone-600 hover:bg-stone-50">
                      Run now
                    </button>
                    <button onClick={() => remove(s.id)}
                      className="text-[11px] px-2 py-1 rounded-md border border-stone-200 text-stone-400 hover:bg-red-50 hover:text-red-600">
                      Remove
                    </button>
                  </div>
                </div>
                {s.recent_runs?.length > 0 && (
                  <div className="mt-1.5 flex items-center gap-3 text-[10px] text-stone-400">
                    {s.recent_runs.slice(0, 3).map((r, i) => (
                      <span key={i}>
                        {r.ran_at ? new Date(r.ran_at).toLocaleTimeString() : ''}: {r.error ? `error — ${r.error.slice(0, 40)}` : `${r.transition} (value ${r.value})`}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default function EnterpriseProfile() {
  const [overview, setOverview] = useState(null);
  const [objects, setObjects] = useState([]);
  const [value, setValue] = useState([]);
  const [selectedDomain, setSelectedDomain] = useState(null);
  const [discovering, setDiscovering] = useState(false);
  const [exporting, setExporting] = useState(false);

  const load = useCallback(async () => {
    try {
      const [ovRes, objRes, valRes] = await Promise.all([
        authFetch('/api/ontology/overview'),
        authFetch('/api/ontology/objects'),
        authFetch('/api/ontology/coverage-value'),
      ]);
      if (ovRes.ok) {
        const ov = await ovRes.json();
        setOverview(ov);
        setSelectedDomain((cur) => cur || ov.domains?.[0]?.id || null);
      }
      if (objRes.ok) setObjects(await objRes.json());
      if (valRes.ok) setValue((await valRes.json()).domains || []);
    } catch {
      toast.error('Failed to load the enterprise profile');
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const runDiscovery = async () => {
    setDiscovering(true);
    try {
      const res = await authFetch('/api/ontology/discover', { method: 'POST' });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      const out = await res.json();
      const bound = (out.report || []).filter((r) => r.status === 'bound');
      if (out.discovered > 0) {
        toast.success(
          `Discovery bound ${out.discovered} object${out.discovered === 1 ? '' : 's'}: ` +
          bound.map((r) => `${r.object}→${r.table}`).join(', ')
        );
      } else {
        toast.info('No new bindings — every matchable object is already wired');
      }
      await load();
    } catch (e) {
      toast.error(`Discovery failed: ${e.message || e}`);
    } finally {
      setDiscovering(false);
    }
  };

  const exportDrafts = async () => {
    setExporting(true);
    try {
      const res = await authFetch('/api/ontology/po-drafts/export');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'po_drafts.xlsx';
      a.click();
      URL.revokeObjectURL(url);
      toast.success('PO drafts exported');
    } catch (e) {
      toast.error(`Export failed: ${e.message || e}`);
    } finally {
      setExporting(false);
    }
  };

  const totals = overview?.totals;
  const domain = useMemo(
    () => overview?.domains?.find((d) => d.id === selectedDomain),
    [overview, selectedDomain]
  );

  if (!overview) {
    return (
      <div className="flex items-center justify-center py-24 text-stone-400">
        <Loader2 size={18} className="animate-spin mr-2" /> Loading enterprise profile…
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto bg-stone-50">
      <div className="max-w-6xl mx-auto px-6 py-8">
        <div className="flex items-center gap-3">
          <Building2 size={22} className="text-stone-700" />
          <h1 className="text-lg font-semibold text-stone-900">Enterprise Profile</h1>
          <span className="text-[11px] text-stone-400">
            the org's semantic layer — what is grounded, what is wired, what is still a draft
          </span>
          <button
            onClick={runDiscovery}
            disabled={discovering}
            className="ml-auto flex items-center gap-1.5 text-[12px] px-3 py-1.5 rounded-md border border-stone-300 bg-white text-stone-700 hover:bg-stone-100 disabled:opacity-50"
          >
            {discovering ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />}
            {discovering ? 'Discovering…' : 'Run auto-discovery'}
          </button>
        </div>

        {/* Totals strip */}
        <div className="mt-5 grid grid-cols-2 sm:grid-cols-5 gap-3">
          {[
            ['Domains', totals.domains],
            ['Objects', totals.objects],
            ['Objects live', totals.objects_live],
            ['Actions', totals.actions],
            ['Links', totals.links],
          ].map(([label, value]) => (
            <div key={label} className="rounded-xl border border-stone-200 bg-white px-4 py-3">
              <div className="text-lg font-semibold text-stone-800">{value}</div>
              <div className="text-[10px] uppercase tracking-wide text-stone-400">{label}</div>
            </div>
          ))}
        </div>

        {/* Coverage map */}
        <div className="mt-8">
          <h2 className="text-sm font-semibold text-stone-800">Coverage Map</h2>
          <span className="text-[11px] text-stone-400">
            honest readiness — “live” only counts objects backed by real data + an executable action
          </span>
          <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {overview.domains.map((d) => (
              <CoverageCard
                key={d.id}
                domain={d}
                value={value.find((v) => v.domain_id === d.id)}
                selected={d.id === selectedDomain}
                onClick={() => setSelectedDomain(d.id)}
              />
            ))}
          </div>
        </div>

        {/* P7 — Ontology Graph Explorer */}
        <div className="mt-8">
          <OntologyGraph />
        </div>

        {domain && <div className="mt-4"><ObjectsPanel domain={domain} objects={objects} /></div>}
        <div className="mt-4"><RevisionsFeed /></div>

        <PoDraftsCard onExport={exportDrafts} exporting={exporting} />

        <SensorsCard />
      </div>
    </div>
  );
}
