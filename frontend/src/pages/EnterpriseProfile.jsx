import { useState, useEffect, useCallback, useMemo } from 'react';
import {
  Building2, RefreshCw, Loader2, Download, MapPin, Radar, Plus,
  CircleDot, Layers, Database, ShieldCheck, History, Network, Boxes,
  Workflow, Search, AlertTriangle, CheckCircle2, Activity, Target, Gauge,
  GitBranch, TrendingDown, FileSearch, ShieldAlert,
} from 'lucide-react';
import { toast } from 'sonner';
import { authFetch } from '@/api/authFetch';
import { EVIDENCE_BADGE_STYLE } from '@/lib/evidenceStatuses';
import OntologyGraph from '@/components/ontology/OntologyGraph';
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer,
} from 'recharts';

/**
 * Enterprise Profile — Zhanlu Business Operating Model.
 *
 * Six connected experiences (default landing = A. Executive Overview):
 *   A Executive Business Overview   — governed KPIs + priorities (real compute)
 *   B Business Situation Room        — investigate an entity via graph + metrics
 *   C Business Object Explorer       — Business navigator + Ontology Studio
 *   D Decision & Action Center       — approval-gated recommendations
 *   E Business Outcome Tracking      — decision lifecycle (honest states)
 *   F Operational Health & Governance— technical administration, consolidated
 *
 * Architecture: every number comes from the governed MetricDefinition via the
 * deterministic /api/ontology/metrics/{id}/compute path. No LLM-computed figure,
 * no fabricated value, no hardcoded business schema.
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

function EvidenceBadge({ obj }) {
  const status = EVIDENCE_BADGE_STYLE[obj.evidence_status]
    ? obj.evidence_status : 'proposed';
  const conf = obj.confidence != null ? ` · ${Math.round(obj.confidence * 100)}%` : '';
  return (
    <span
      className={`inline-flex items-center text-[10px] px-1.5 py-0.5 rounded-full border capitalize ${EVIDENCE_BADGE_STYLE[status]}`}
      title={obj.provenance ? JSON.stringify(obj.provenance) : `evidence status: ${status}`}
    >
      {status}{conf}
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
          {value.est_value > 0 && <span className="text-amber-700">≈ ${value.est_value.toLocaleString()} est. value</span>}
          {value.agents?.length > 0 && <span>{value.agents.length} agents</span>}
          {value.acceptance_rate != null && <span>{Math.round(value.acceptance_rate * 100)}% accepted</span>}
          {value.csat != null && <span>CSAT {Math.round(value.csat)}</span>}
        </div>
      )}
    </button>
  );
}

function RevisionsFeed() {
  const [open, setOpen] = useState(false);
  const [revisions, setRevisions] = useState(null);
  const load = async () => {
    try {
      const res = await authFetch('/api/ontology/revisions');
      const data = await res.json();
      setRevisions(data.revisions || []);
    } catch { setRevisions([]); }
  };
  useEffect(() => { if (open && revisions === null) load(); }, [open, revisions]);
  return (
    <div className="bg-white rounded-lg border border-stone-200">
      <button onClick={() => setOpen(!open)} className="w-full flex items-center gap-2 px-4 py-3 text-left">
        <History size={14} className="text-stone-500" />
        <h2 className="text-sm font-semibold text-stone-800">Ontology Revision History</h2>
        <span className="text-[11px] text-stone-400">append-only — every mutation, who made it, when</span>
        <span className="ml-auto text-stone-400 text-xs">{open ? 'hide' : 'show'}</span>
      </button>
      {open && (
        <div className="px-4 pb-3 max-h-72 overflow-y-auto">
          {!revisions && <div className="text-[11px] text-stone-400 py-2">Loading…</div>}
          {revisions && revisions.length === 0 && <div className="text-[11px] text-stone-400 py-2">No revisions yet.</div>}
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

function PoDraftsCard({ onExport, exporting }) {
  const [drafts, setDrafts] = useState(null);
  const load = useCallback(async () => {
    try {
      const res = await authFetch('/api/ontology/po-drafts');
      if (res.ok) setDrafts(await res.json());
    } catch { /* fail-quiet */ }
  }, []);
  useEffect(() => { load(); }, [load]);
  if (!drafts) return null;
  return (
    <div className="mt-6">
      <div className="flex items-center gap-2">
        <ShieldCheck size={15} className="text-emerald-600" />
        <h2 className="text-sm font-semibold text-stone-800">PO Draft Worklist</h2>
        <span className="text-[11px] text-stone-400">approval-gated write-backs — every draft came from an approved action</span>
        <button onClick={onExport} disabled={exporting}
          className="ml-auto flex items-center gap-1.5 text-[11px] px-2.5 py-1.5 rounded-md border border-stone-300 bg-white text-stone-700 hover:bg-stone-50 disabled:opacity-50">
          {exporting ? <Loader2 size={12} className="animate-spin" /> : <Download size={12} />}
          Export xlsx
        </button>
      </div>
      {drafts.length === 0 ? (
        <p className="mt-3 text-[11px] text-stone-400">No PO drafts yet. They are created when an approved ontology write action runs.</p>
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
                  <td className="px-3 py-2 text-right">{d.currency} {(d.est_amount ?? 0).toLocaleString()}</td>
                  <td className="px-3 py-2">
                    <span className={`px-1.5 py-0.5 rounded-full border text-[10px] ${
                      d.status === 'approved' ? 'border-emerald-200 bg-emerald-50 text-emerald-700' : 'border-amber-200 bg-amber-50 text-amber-700'}`}>
                      {d.status}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-stone-400">{d.created_date ? new Date(d.created_date).toLocaleDateString() : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function SensorsCard() {
  const [data, setData] = useState(null);
  const [creating, setCreating] = useState(false);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ name: '', object_name: '', kb_id: '', watch_sql: '', op: 'gt', threshold: 0, schedule_seconds: 3600 });
  const load = useCallback(async () => {
    try { const res = await authFetch('/api/ontology/sensors'); if (res.ok) setData(await res.json()); } catch { /* fail-quiet */ }
  }, []);
  useEffect(() => { load(); }, [load]);
  const create = async () => {
    if (!form.name || !form.object_name || !form.kb_id || !form.watch_sql.trim()) { toast.error('Name, object, data source and watch SQL are required'); return; }
    setCreating(true);
    try {
      const res = await authFetch('/api/ontology/sensors', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(form) });
      if (!res.ok) { const b = await res.json().catch(() => ({})); throw new Error(b.detail || `HTTP ${res.status}`); }
      toast.success('Sensor created'); setShowForm(false); setForm({ ...form, name: '', watch_sql: '' }); await load();
    } catch (e) { toast.error(`Create failed: ${e.message || e}`); }
    finally { setCreating(false); }
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
    } catch (e) { toast.error(`Run failed: ${e.message || e}`); }
  };
  const remove = async (id) => {
    try { const res = await authFetch(`/api/ontology/sensors/${id}`, { method: 'DELETE' }); if (!res.ok) throw new Error(`HTTP ${res.status}`); toast.success('Sensor removed'); await load(); }
    catch (e) { toast.error(`Delete failed: ${e.message || e}`); }
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
    <div className="mt-6">
      <div className="flex items-center gap-2">
        <Radar size={15} className="text-stone-600" />
        <h2 className="text-sm font-semibold text-stone-800">Sensing</h2>
        <span className="text-[11px] text-stone-400">scheduled watches on your real data — on breach, linked-domain agents are notified</span>
        <button onClick={() => setShowForm((v) => !v)} className="ml-auto flex items-center gap-1.5 text-[11px] px-2.5 py-1.5 rounded-md border border-stone-300 bg-white text-stone-700 hover:bg-stone-50">
          <Plus size={12} /> New sensor
        </button>
      </div>
      {showForm && (
        <div className="mt-3 rounded-xl border border-stone-200 bg-white p-4 grid grid-cols-1 sm:grid-cols-2 gap-3 text-[12px]">
          <input className="border border-stone-200 rounded-md px-2.5 py-1.5" placeholder="Sensor name"
            value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <select className="border border-stone-200 rounded-md px-2.5 py-1.5" value={form.object_name} onChange={(e) => setForm({ ...form, object_name: e.target.value })}>
            <option value="">Notify object…</option>
            {data.objects.map((o) => <option key={o.name} value={o.name}>{o.display_name || o.name}</option>)}
          </select>
          <select className="border border-stone-200 rounded-md px-2.5 py-1.5" value={form.kb_id} onChange={(e) => setForm({ ...form, kb_id: e.target.value })}>
            <option value="">Data source…</option>
            {data.data_sources.map((k) => <option key={k.id} value={k.id}>{k.name} ({k.db_type || 'database'})</option>)}
          </select>
          <div className="flex items-center gap-2">
            <select className="border border-stone-200 rounded-md px-2 py-1.5" value={form.op} onChange={(e) => setForm({ ...form, op: e.target.value })}>
              {['gt', 'gte', 'lt', 'lte', 'eq'].map((o) => <option key={o} value={o}>{o}</option>)}
            </select>
            <input type="number" step="any" className="w-24 border border-stone-200 rounded-md px-2.5 py-1.5" value={form.threshold} onChange={(e) => setForm({ ...form, threshold: Number(e.target.value) })} />
            <select className="border border-stone-200 rounded-md px-2 py-1.5" value={form.schedule_seconds} onChange={(e) => setForm({ ...form, schedule_seconds: Number(e.target.value) })}>
              <option value={300}>every 5 min</option><option value={3600}>every hour</option><option value={21600}>every 6 h</option><option value={86400}>daily</option>
            </select>
          </div>
          <textarea className="sm:col-span-2 border border-stone-200 rounded-md px-2.5 py-1.5 font-mono text-[11px]" rows={2}
            placeholder="Watch SQL — must return ONE numeric value" value={form.watch_sql} onChange={(e) => setForm({ ...form, watch_sql: e.target.value })} />
          <div className="sm:col-span-2 flex items-center gap-2">
            <button onClick={create} disabled={creating}
              className="flex items-center gap-1.5 text-[11px] px-3 py-1.5 rounded-md bg-stone-800 text-white hover:bg-stone-700 disabled:opacity-50">
              {creating ? <Loader2 size={12} className="animate-spin" /> : <ShieldCheck size={12} />} Create sensor
            </button>
          </div>
        </div>
      )}
      {data.sensors.length === 0 ? (
        <p className="mt-3 text-[11px] text-stone-400">No sensors yet. A sensor watches a query on a connected data source and notifies bound agents on breach.</p>
      ) : (
        <div className="mt-3 space-y-2">
          {data.sensors.map((s) => {
            const [label, cls] = stateChip(s);
            return (
              <div key={s.id} className="rounded-xl border border-stone-200 bg-white px-4 py-3">
                <div className="flex items-center gap-2">
                  <span className="text-[13px] font-medium text-stone-800">{s.name}</span>
                  <span className={`text-[10px] px-1.5 py-0.5 rounded-full border ${cls}`}>{label}</span>
                  <span className="text-[10px] text-stone-400">watches {s.object_name} · {s.op} {s.threshold} · every {s.schedule_seconds >= 3600 ? `${s.schedule_seconds / 3600}h` : `${s.schedule_seconds / 60}m`}</span>
                  <div className="ml-auto flex items-center gap-1.5">
                    <button onClick={() => runNow(s.id)} className="text-[11px] px-2 py-1 rounded-md border border-stone-300 text-stone-600 hover:bg-stone-50">Run now</button>
                    <button onClick={() => remove(s.id)} className="text-[11px] px-2 py-1 rounded-md border border-stone-200 text-stone-400 hover:bg-red-50 hover:text-red-600">Remove</button>
                  </div>
                </div>
                {s.recent_runs?.length > 0 && (
                  <div className="mt-1.5 flex items-center gap-3 text-[10px] text-stone-400">
                    {s.recent_runs.slice(0, 3).map((r, i) => (
                      <span key={i}>{r.ran_at ? new Date(r.ran_at).toLocaleTimeString() : ''}: {r.error ? `error — ${r.error.slice(0, 40)}` : `${r.transition} (value ${r.value})`}</span>
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

// ---------------------------------------------------------------------------
// A. EXECUTIVE BUSINESS OVERVIEW
// ---------------------------------------------------------------------------
function MetricCard({ m, kpi }) {
  const loading = kpi === undefined;
  const compare = kpi?.compare;
  const trend = kpi?.trend;
  if (loading) {
    return (
      <div className="rounded-xl border border-stone-200 bg-white px-4 py-3.5">
        <div className="text-[11px] text-stone-400">{m.name}</div>
        <Loader2 size={16} className="animate-spin mt-2 text-stone-300" />
      </div>
    );
  }
  const unavail = !compare || compare.unavailable
    || (compare.current && compare.current.unavailable);
  if (unavail) {
    return (
      <div className="rounded-xl border border-amber-200 bg-amber-50/40 px-4 py-3.5">
        <div className="text-[13px] font-medium text-stone-700">{m.name}</div>
        <p className="mt-1 text-[10px] text-amber-700">Unavailable: {compare?.reason || compare?.current?.reason || 'no data for the current period'}</p>
      </div>
    );
  }
  const cur = compare.current || {};
  const prior = (compare.prior && !compare.prior.unavailable) ? compare.prior : null;
  const v = cur.value;
  const prevVal = prior?.value ?? null;
  const delta = (v != null && prevVal != null) ? v - prevVal : null;
  const pct = (delta != null && prevVal) ? (delta / prevVal) * 100 : null;
  const fmt = (x) => `${m.currency || ''} ${Number(x).toLocaleString()}`.trim();
  const points = (trend?.periods || [])
    .filter((p) => p.value != null)
    .map((p) => ({ period: p.period, value: p.value }));

  return (
    <div className="rounded-xl border border-stone-200 bg-white px-4 py-3.5">
      <div className="flex items-center gap-2">
        <span className="text-[13px] font-semibold text-stone-800">{m.name}</span>
        <span className="ml-auto text-[10px] text-stone-400">{compare.current_period || m.unit || ''}</span>
      </div>
      <div className="mt-1 text-2xl font-semibold text-stone-900 tabular-nums">
        {v == null ? '—' : fmt(v)}
      </div>
      {delta != null && (
        <div className={`mt-0.5 text-[11px] ${delta < 0 ? 'text-red-600' : 'text-emerald-600'}`}>
          {delta < 0 ? '▼' : '▲'} {fmt(Math.abs(delta))} ({pct?.toFixed(1)}%) vs prior
        </div>
      )}
      {points.length > 1 && (
        <div className="mt-2 h-12">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={points} margin={{ top: 2, right: 2, bottom: 2, left: 2 }}>
              <Line type="monotone" dataKey="value" stroke={delta != null && delta < 0 ? '#dc2626' : '#059669'} strokeWidth={1.5} dot={false} />
              <XAxis dataKey="period" tick={{ fontSize: 8 }} hide />
              <YAxis hide domain={['dataMin', 'dataMax']} />
              <Tooltip
                contentStyle={{ fontSize: 10, padding: '2px 6px' }}
                formatter={(val) => [fmt(val), m.name]} labelFormatter={(l) => `period ${l}`} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}
      <div className="mt-1 text-[10px] text-stone-400">
        {cur.provenance?.datasource_name || m.datasource_id}
        {cur.provenance?.base_table ? ` · ${cur.provenance.base_table}` : ''}
      </div>
      <details className="mt-1">
        <summary className="cursor-pointer text-[10px] text-stone-500 hover:text-stone-700">definition & provenance</summary>
        <div className="mt-1 text-[10px] text-stone-500 space-y-0.5">
          <div>aggregation: {m.aggregation} · v{m.version} · {m.evidence_status}</div>
          <div>prior period: {compare.prior_period || '—'} = {prevVal != null ? fmt(prevVal) : 'n/a'}</div>
          <div className="font-mono break-all">{cur.execution_trace?.sql}</div>
        </div>
      </details>
    </div>
  );
}

function ExecutiveOverview() {
  const [metrics, setMetrics] = useState(null);
  const [kpis, setKpis] = useState({});
  const [sensors, setSensors] = useState(null);
  const [priorities, setPriorities] = useState(undefined); // undefined=loading, null=empty, {} = data

  useEffect(() => {
    (async () => {
      try {
        const [mRes, sRes] = await Promise.all([
          authFetch('/api/ontology/metrics'),
          authFetch('/api/ontology/sensors'),
        ]);
        const m = (await mRes.json()).metrics || [];
        setMetrics(m);
        if (sRes.ok) setSensors(await sRes.json());

        // Period-aware: each KPI carries current-vs-prior (governed compute) + trend.
        const shown = m.filter((x) => x.lifecycle === 'published' && x.is_enabled);
        const results = await Promise.all(shown.map(async (x) => {
          try {
            const [c, t] = await Promise.all([
              authFetch(`/api/ontology/metrics/${encodeURIComponent(x.id)}/compare`),
              authFetch(`/api/ontology/metrics/${encodeURIComponent(x.id)}/trend`),
            ]);
            return [x.id, { compare: await c.json(), trend: await t.json() }];
          } catch { return [x.id, null]; }
        }));
        setKpis(Object.fromEntries(results));

        // Deterministic declining-profitability detection (no LLM) for the
        // gross-margin metric, by segment — auto-discovers periods + segments.
        const gm = shown.find((x) => /gross\s*margin/i.test(x.name));
        if (gm) {
          try {
            const r = await authFetch(
              `/api/ontology/metrics/${encodeURIComponent(gm.id)}/segments-compare?segment_column=model_id`);
            setPriorities(await r.json());
          } catch { setPriorities(null); }
        } else {
          setPriorities(null);
        }
      } catch { toast.error('Failed to load executive overview'); }
    })();
  }, []);

  const kpiList = (metrics || []).filter((x) => x.lifecycle === 'published' && x.is_enabled);
  const breaches = (sensors?.sensors || []).filter((s) => {
    try { return JSON.parse(s.last_state_json || '{}').breached; } catch { return false; }
  });
  const declining = (priorities && priorities.segments)
    ? priorities.segments.filter((s) => s.declining)
    : [];

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-sm font-semibold text-stone-800 flex items-center gap-2"><Gauge size={15} className="text-stone-600" /> Governed Business KPIs</h2>
        <span className="text-[11px] text-stone-400">every figure is computed by the governed MetricDefinition (current vs prior period, deterministic) — never estimated, never invented</span>
        <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {!metrics && <div className="text-[11px] text-stone-400 py-4"><Loader2 size={14} className="animate-spin inline mr-2" />Loading KPIs…</div>}
          {metrics && kpiList.length === 0 && (
            <div className="col-span-full rounded-xl border border-dashed border-stone-300 bg-white py-8 text-center text-[12px] text-stone-400">
              No governed, published metrics are configured for this organization yet.
            </div>
          )}
          {kpiList.map((m) => (
            <MetricCard key={m.id} m={m} kpi={kpis[m.id]} />
          ))}
        </div>
      </div>

      <div>
        <h2 className="text-sm font-semibold text-stone-800 flex items-center gap-2"><AlertTriangle size={15} className="text-red-600" /> Business Priorities</h2>
        <span className="text-[11px] text-stone-400">deterministically detected situations requiring attention — declining profitability (governed gross-margin by segment) and sensing breaches</span>
        <div className="mt-3 space-y-2">
          {priorities === undefined && <div className="text-[11px] text-stone-400 py-2"><Loader2 size={14} className="animate-spin inline mr-2" />Analyzing…</div>}
          {priorities && declining.length === 0 && breaches.length === 0 && (
            <div className="rounded-xl border border-stone-200 bg-white px-4 py-3 text-[11px] text-stone-400">No declining profitability or active breaches detected.</div>
          )}
          {declining.map((s) => {
            const pct = s.delta_pct != null ? `${s.delta_pct.toFixed(1)}%` : 'n/a';
            return (
              <div key={s.segment} className="rounded-xl border border-red-200 bg-red-50/40 px-4 py-3">
                <div className="flex items-center gap-2">
                  <TrendingDown size={14} className="text-red-600" />
                  <span className="text-[13px] font-medium text-stone-800">Vehicle Model {s.segment} profitability declining</span>
                  <span className="ml-auto text-[10px] text-red-600">{pct} vs prior</span>
                </div>
                <p className="mt-1 text-[11px] text-stone-600">
                  Gross margin fell from <b>{priorities.currency || ''} {Number(s.prior_value).toLocaleString()}</b> to{' '}
                  <b>{priorities.currency || ''} {Number(s.current_value).toLocaleString()}</b> ({priorities.current_period} vs {priorities.prior_period}).
                  Investigate the affected model's sales, component costs, and linked suppliers.
                </p>
              </div>
            );
          })}
          {breaches.map((s) => (
            <div key={s.id} className="rounded-xl border border-red-200 bg-red-50/40 px-4 py-3">
              <div className="flex items-center gap-2">
                <TrendingDown size={14} className="text-red-600" />
                <span className="text-[13px] font-medium text-stone-800">{s.name}</span>
                <span className="text-[10px] text-stone-400">watches {s.object_name}</span>
              </div>
              <p className="mt-1 text-[11px] text-stone-600">A watched signal for <b>{s.object_name}</b> crossed its threshold ({s.op} {s.threshold}). Investigate the affected business entities and governed metrics.</p>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// C. BUSINESS OBJECT EXPLORER (Business navigator + Ontology Studio)
// ---------------------------------------------------------------------------
function BusinessExplorer() {
  const [graph, setGraph] = useState(null);
  const [q, setQ] = useState('');
  const [selected, setSelected] = useState(null);

  useEffect(() => {
    (async () => {
      try { const res = await authFetch('/api/ontology/graph'); if (res.ok) setGraph(await res.json()); }
      catch { toast.error('Failed to load object graph'); }
    })();
  }, []);

  const objects = graph?.objects || [];
  const filtered = q ? objects.filter((o) => (o.name || '').toLowerCase().includes(q.toLowerCase())) : objects;
  const sel = selected ? objects.find((o) => o.id === selected) : null;
  const links = graph?.links || [];
  const related = sel ? links.filter((l) => l.from_object_id === sel.id || l.to_object_id === sel.id) : [];
  const relatedObjs = related.map((l) => {
    const other = l.from_object_id === sel.id ? l.to_object_id : l.from_object_id;
    return objects.find((o) => o.id === other);
  }).filter(Boolean);
  const metrics = (graph?.metrics || []).filter((m) => (m.source_object_refs || []).includes(sel?.id));
  const docEv = sel?.provenance?.document_provenance || [];

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <Search size={14} className="text-stone-500" />
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search business objects…"
          className="flex-1 border border-stone-200 rounded-md px-3 py-1.5 text-[12px]" />
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="rounded-xl border border-stone-200 bg-white max-h-[520px] overflow-y-auto">
          <div className="px-3 py-2 text-[10px] uppercase tracking-wide text-stone-400 border-b border-stone-100">Objects ({filtered.length})</div>
          <ul className="divide-y divide-stone-100">
            {filtered.map((o) => (
              <li key={o.id}>
                <button onClick={() => setSelected(o.id)} className={`w-full text-left px-3 py-2 hover:bg-stone-50 flex items-center gap-2 ${selected === o.id ? 'bg-stone-50' : ''}`}>
                  <Boxes size={12} className="text-stone-400" />
                  <span className="text-[12px] text-stone-700">{o.display_name || o.name}</span>
                  <EvidenceBadge obj={o} />
                </button>
              </li>
            ))}
          </ul>
        </div>
        <div className="lg:col-span-2 space-y-3">
          {!sel && <div className="rounded-xl border border-dashed border-stone-300 bg-white py-12 text-center text-[12px] text-stone-400">Select an object to explore its business relationships, governed metrics, and evidence.</div>}
          {sel && (
            <>
              <div className="rounded-xl border border-stone-200 bg-white px-4 py-3">
                <div className="text-[13px] font-semibold text-stone-800">{sel.display_name || sel.name}</div>
                <div className="mt-1 flex items-center gap-2 text-[10px] text-stone-400"><BackingBadge obj={sel} /><EvidenceBadge obj={sel} /></div>
              </div>
              <div className="rounded-xl border border-stone-200 bg-white px-4 py-3">
                <div className="text-[10px] uppercase tracking-wide text-stone-400 mb-2 flex items-center gap-1"><GitBranch size={11} /> Relationships ({related.length})</div>
                {related.length === 0 && <div className="text-[11px] text-stone-400">No linked objects.</div>}
                <ul className="space-y-1">
                  {related.map((l, i) => (
                    <li key={i} className="text-[11px] text-stone-600 flex items-center gap-2">
                      <span className="capitalize px-1 rounded bg-stone-100">{l.link_type}</span>
                      {relatedObjs[i]?.display_name || relatedObjs[i]?.name || l.to_object_id}
                    </li>
                  ))}
                </ul>
              </div>
              <div className="rounded-xl border border-stone-200 bg-white px-4 py-3">
                <div className="text-[10px] uppercase tracking-wide text-stone-400 mb-2 flex items-center gap-1"><Target size={11} /> Governed metrics</div>
                {metrics.length === 0 && <div className="text-[11px] text-stone-400">No governed metric backs this object.</div>}
                <div className="flex flex-wrap gap-2">
                  {metrics.map((m) => (
                    <span key={m.id} className="text-[10px] px-2 py-1 rounded-lg border border-stone-200 bg-white">{m.name}</span>
                  ))}
                </div>
              </div>
              <div className="rounded-xl border border-stone-200 bg-white px-4 py-3">
                <div className="text-[10px] uppercase tracking-wide text-stone-400 mb-2 flex items-center gap-1"><FileSearch size={11} /> Document evidence ({docEv.length})</div>
                {docEv.length === 0 && <div className="text-[11px] text-stone-400">none visible to you (per-viewer, fail-closed)</div>}
                {docEv.map((d, i) => (
                  <div key={i} className="rounded-lg border border-sky-100 bg-sky-50/50 px-2 py-1.5 mb-1 text-[11px]">
                    <div className="text-stone-700 italic">“{d.quote}”</div>
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function BusinessObjectExplorer() {
  const [mode, setMode] = useState('business');
  return (
    <div>
      <div className="flex items-center gap-2 mb-3">
        <Network size={15} className="text-stone-600" />
        <h2 className="text-sm font-semibold text-stone-800">Business Object Explorer</h2>
        <div className="ml-auto flex items-center gap-1 text-[11px]">
          <button onClick={() => setMode('business')} className={`px-2.5 py-1.5 rounded-md border ${mode === 'business' ? 'border-stone-400 bg-stone-100 text-stone-700' : 'border-stone-200 text-stone-500'}`}>Business Explorer</button>
          <button onClick={() => setMode('studio')} className={`px-2.5 py-1.5 rounded-md border ${mode === 'studio' ? 'border-stone-400 bg-stone-100 text-stone-700' : 'border-stone-200 text-stone-500'}`}>Ontology Studio</button>
        </div>
      </div>
      <span className="text-[11px] text-stone-400">
        {mode === 'business'
          ? 'navigate real business relationships resolved from the ontology — edges exist only where the backend links them'
          : 'administration view: domains, objects, links, bindings, governed metrics, revision history'}
      </span>
      <div className="mt-3">{mode === 'business' ? <BusinessExplorer /> : <OntologyGraph />}</div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// B. BUSINESS SITUATION ROOM
// ---------------------------------------------------------------------------
function SituationRoom() {
  const [graph, setGraph] = useState(null);
  const [selected, setSelected] = useState(null);
  const [question, setQuestion] = useState('');
  const [asking, setAsking] = useState(false);
  const [answer, setAnswer] = useState(null);
  const [answerError, setAnswerError] = useState(null);
  useEffect(() => {
    (async () => { try { const res = await authFetch('/api/ontology/graph'); if (res.ok) setGraph(await res.json()); } catch { toast.error('Failed to load'); } })();
  }, []);

  const askAgent = async () => {
    const q = (question || '').trim();
    if (!q) return;
    setAsking(true); setAnswerError(null); setAnswer(null);
    try {
      const res = await authFetch('/api/ontology/situation/investigate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: q }),
      });
      const data = await res.json();
      if (!res.ok || data.unavailable) {
        setAnswerError(data?.reason || `investigation failed (HTTP ${res.status})`);
      } else {
        setAnswer(data);
      }
    } catch (e) {
      setAnswerError(`agent investigation failed: ${e.message || e}`);
    } finally { setAsking(false); }
  };

  const objects = graph?.objects || [];
  const sel = selected ? objects.find((o) => o.id === selected) : null;
  const links = graph?.links || [];
  const related = sel ? links.filter((l) => l.from_object_id === sel.id || l.to_object_id === sel.id) : [];
  const metrics = (graph?.metrics || []).filter((m) => (m.source_object_refs || []).includes(sel?.id));
  const facts = [
    sel ? `Selected entity: ${sel.display_name || sel.name} (${sel.evidence_status})` : null,
    related.length ? `${related.length} linked business relationships resolved from the ontology` : null,
    metrics.length ? `${metrics.length} governed metric(s) back this entity` : null,
  ].filter(Boolean);

  return (
    <div className="space-y-4">
      <div className="rounded-xl border border-stone-200 bg-white px-4 py-3">
        <div className="flex items-center gap-2">
          <FileSearch size={14} className="text-stone-600" />
          <span className="text-[12px] font-medium text-stone-800">Investigate a business question</span>
        </div>
        <textarea value={question} onChange={(e) => setQuestion(e.target.value)} rows={2}
          placeholder="e.g. Why is Vehicle Model X's profitability declining?"
          className="mt-2 w-full border border-stone-200 rounded-md px-3 py-2 text-[12px]" />
        <div className="mt-2 flex items-center gap-2">
          <button onClick={askAgent} disabled={asking || !question.trim()}
            className="flex items-center gap-1.5 text-[11px] px-3 py-1.5 rounded-md bg-stone-800 text-white hover:bg-stone-700 disabled:opacity-50">
            {asking ? <Loader2 size={12} className="animate-spin" /> : <FileSearch size={12} />}
            Ask the Zhanlu Agent
          </button>
          {sel && (
            <button onClick={() => setQuestion(`Why is ${sel.display_name || sel.name}'s profitability declining?`)}
              className="text-[11px] px-2.5 py-1.5 rounded-md border border-stone-300 text-stone-600 hover:bg-stone-50">
              Investigate selected entity
            </button>
          )}
          <span className="ml-auto text-[10px] text-stone-400">synthesis via the existing Agent Harness</span>
        </div>
        {answerError && <p className="mt-2 text-[11px] text-red-600">{answerError}</p>}
        {answer && (
          <div className="mt-2 rounded-lg border border-stone-200 bg-stone-50 px-3 py-2 text-[11px] space-y-1">
            <div className="text-stone-700">{answer.answer}</div>
            {answer.sql && <div className="font-mono text-[10px] text-stone-500 break-all">SQL: {answer.sql}</div>}
            {Array.isArray(answer.rows) && answer.rows.length > 0 && (
              <div className="text-[10px] text-stone-500">rows: {JSON.stringify(answer.rows)}</div>
            )}
          </div>
        )}
        <p className="mt-1 text-[10px] text-stone-400">
          Structured investigation (below) uses the ontology graph, governed metrics and document evidence.
          The agent synthesis above reuses the existing Agent Harness data-agent (scoped to this org's
          bound sources, write-denied, budget-bounded) — no new orchestration framework.
        </p>
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="rounded-xl border border-stone-200 bg-white max-h-[520px] overflow-y-auto">
          <div className="px-3 py-2 text-[10px] uppercase tracking-wide text-stone-400 border-b">Entities</div>
          <ul className="divide-y divide-stone-100">
            {objects.map((o) => (
              <li key={o.id}><button onClick={() => setSelected(o.id)} className={`w-full text-left px-3 py-2 hover:bg-stone-50 text-[12px] ${selected === o.id ? 'bg-stone-50' : ''}`}>{o.display_name || o.name}</button></li>
            ))}
          </ul>
        </div>
        <div className="lg:col-span-2 space-y-3">
          {!sel && <div className="rounded-xl border border-dashed border-stone-300 bg-white py-12 text-center text-[12px] text-stone-400">Select an entity to investigate.</div>}
          {sel && (
            <>
              <div className="rounded-xl border border-stone-200 bg-white px-4 py-3">
                <div className="text-[10px] uppercase tracking-wide text-stone-400">Verified findings</div>
                <ul className="mt-1 space-y-1">
                  {facts.map((f, i) => <li key={i} className="text-[11px] text-stone-600 flex items-center gap-2"><CheckCircle2 size={12} className="text-emerald-600" />{f}</li>)}
                </ul>
              </div>
              <div className="rounded-xl border border-stone-200 bg-white px-4 py-3">
                <div className="text-[10px] uppercase tracking-wide text-stone-400">Affected business relationships</div>
                <ul className="mt-1 space-y-1">
                  {related.map((l, i) => <li key={i} className="text-[11px] text-stone-600"><span className="capitalize px-1 rounded bg-stone-100">{l.link_type}</span> {l.to_object_id}</li>)}
                </ul>
              </div>
              <div className="rounded-xl border border-amber-200 bg-amber-50/40 px-4 py-3">
                <div className="text-[10px] uppercase tracking-wide text-stone-500">Hypotheses & recommended investigations</div>
                <p className="mt-1 text-[11px] text-stone-600">Correlation is not causation. Inspect governed metrics for the linked objects, confirm with document evidence, and validate against source data before concluding a driver.</p>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// D. DECISION & ACTION CENTER
// ---------------------------------------------------------------------------
function GovernedActionCard({ a, onExecuted }) {
  const [open, setOpen] = useState(false);
  const [approvalId, setApprovalId] = useState('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const execute = async () => {
    const id = (approvalId || '').trim();
    if (!id) return;
    setBusy(true); setResult(null);
    try {
      const res = await authFetch(`/api/ontology/actions/${encodeURIComponent(a.id)}/execute-approved`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ approval_id: id }),
      });
      const data = await res.json();
      setResult(data);
      if (data.success) { toast.success('Governed action executed under approval'); onExecuted?.(); }
    } catch (e) {
      setResult({ success: false, reason: e.message || String(e) });
    } finally { setBusy(false); }
  };
  return (
    <div className="rounded-xl border border-stone-200 bg-white px-4 py-3 mb-2">
      <div className="flex items-center gap-2">
        <ShieldCheck size={14} className={a.risk_level === 'write' ? 'text-amber-600' : 'text-stone-400'} />
        <span className="text-[13px] font-medium text-stone-800">{a.name}</span>
        <span className="text-[10px] px-1.5 py-0.5 rounded-full border border-stone-200 bg-stone-50 text-stone-500">{a.risk_level}</span>
        {a.tool_name && <span className="text-[10px] text-stone-400">tool: {a.tool_name}</span>}
        <button onClick={() => setOpen(!open)}
          className="ml-auto text-[11px] px-2.5 py-1 rounded-md border border-stone-300 text-stone-600 hover:bg-stone-50">
          {open ? 'Close' : 'Execute…'}
        </button>
      </div>
      {open && (
        <div className="mt-2 space-y-2">
          <p className="text-[10px] text-stone-400">
            A write runs ONLY under an org-scoped approval bound to this exact
            action + tool (governance gate: pending, wrong-org, expired or
            param-drifted approvals are refused — fail closed).
          </p>
          <div className="flex items-center gap-2">
            <input value={approvalId} onChange={(e) => setApprovalId(e.target.value)}
              placeholder="approval id (from the governance review)"
              className="flex-1 border border-stone-200 rounded-md px-2.5 py-1.5 text-[11px]" />
            <button onClick={execute} disabled={busy || !approvalId.trim()}
              className="flex items-center gap-1.5 text-[11px] px-3 py-1.5 rounded-md bg-stone-800 text-white hover:bg-stone-700 disabled:opacity-50">
              {busy ? <Loader2 size={12} className="animate-spin" /> : <ShieldCheck size={12} />}
              Execute
            </button>
          </div>
          {result && (
            <div className={`rounded-lg px-3 py-2 text-[11px] ${
              result.success ? 'border border-emerald-200 bg-emerald-50/60 text-emerald-800'
                : result.blocked ? 'border border-amber-200 bg-amber-50/60 text-amber-800'
                : 'border border-red-200 bg-red-50/60 text-red-700'}`}>
              {result.success
                ? <span>Executed under approval {result.approval_id} (approved by {result.approved_by || 'admin'}) — result recorded.</span>
                : result.blocked === 'confirmation_required'
                  ? <span>Blocked: the approval is not granted yet — this decision stays pending until an org admin approves it.</span>
                  : <span>Refused: {result.reason}</span>}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function DecisionCenter() {
  const [escalations, setEscalations] = useState(null);
  const [drafts, setDrafts] = useState(null);
  const [actions, setActions] = useState(null);
  const load = useCallback(async () => {
    try {
      const [e, d, a] = await Promise.all([
        authFetch('/api/ontology/escalations'),
        authFetch('/api/ontology/po-drafts'),
        authFetch('/api/ontology/actions'),
      ]);
      if (e.ok) setEscalations((await e.json()).escalations || []);
      if (d.ok) setDrafts(await d.json());
      if (a.ok) setActions(await a.json());
    } catch { toast.error('Failed to load decision center'); }
  }, []);
  useEffect(() => { load(); }, [load]);
  const resolve = async (id) => {
    try { const res = await authFetch(`/api/ontology/escalations/${id}/resolve`, { method: 'POST' }); if (!res.ok) throw new Error(`HTTP ${res.status}`); toast.success('Escalation resolved'); await load(); }
    catch (e) { toast.error(`Resolve failed: ${e.message || e}`); }
  };
  const writeActions = (actions || []).filter((a) => a.risk_level === 'write' && a.is_active !== false);
  return (
    <div className="space-y-4">
      <h2 className="text-sm font-semibold text-stone-800 flex items-center gap-2"><Workflow size={15} className="text-stone-600" /> Decision & Action Center</h2>
      <span className="text-[11px] text-stone-400">recommendations surface from sensing breaches; every operational write stays approval-gated via governed actions</span>
      <div>
        <div className="text-[10px] uppercase tracking-wide text-stone-400 mb-2">Open escalations</div>
        {!escalations && <div className="text-[11px] text-stone-400"><Loader2 size={14} className="animate-spin inline mr-2" />Loading…</div>}
        {escalations && escalations.length === 0 && <div className="rounded-xl border border-stone-200 bg-white px-4 py-3 text-[11px] text-stone-400">No open escalations.</div>}
        {escalations?.map((e) => (
          <div key={e.id} className="rounded-xl border border-stone-200 bg-white px-4 py-3 mb-2">
            <div className="flex items-center gap-2">
              <ShieldAlert size={14} className="text-amber-600" />
              <span className="text-[13px] font-medium text-stone-800">{e.title || e.entity_name || e.id}</span>
              <span className="text-[10px] text-stone-400 ml-auto">{e.status}</span>
            </div>
            {e.summary && <p className="mt-1 text-[11px] text-stone-600">{e.summary}</p>}
            {e.context_json && <p className="mt-1 text-[10px] text-stone-400 font-mono break-all">{JSON.stringify(e.context_json).slice(0, 200)}</p>}
            <div className="mt-2 flex items-center gap-2">
              <span className="text-[10px] text-stone-400">Approval required · execution via governed action</span>
              <button onClick={() => resolve(e.id)} className="ml-auto text-[11px] px-2.5 py-1 rounded-md border border-stone-300 text-stone-600 hover:bg-stone-50">Resolve</button>
            </div>
          </div>
        ))}
      </div>
      <div>
        <div className="text-[10px] uppercase tracking-wide text-stone-400 mb-2">Approval-gated actions (PO drafts)</div>
        {!drafts && <div className="text-[11px] text-stone-400"><Loader2 size={14} className="animate-spin inline mr-2" />Loading…</div>}
        {drafts && drafts.length === 0 && <div className="rounded-xl border border-stone-200 bg-white px-4 py-3 text-[11px] text-stone-400">No pending actions.</div>}
        {drafts?.map((d) => (
          <div key={d.id} className="rounded-xl border border-stone-200 bg-white px-4 py-3 mb-2 flex items-center gap-2">
            <Activity size={14} className="text-stone-500" />
            <span className="text-[12px] text-stone-700">{d.supplier_name} · {d.product} · {d.quantity} {d.unit}</span>
            <span className={`ml-auto text-[10px] px-1.5 py-0.5 rounded-full border ${d.status === 'approved' ? 'border-emerald-200 bg-emerald-50 text-emerald-700' : 'border-amber-200 bg-amber-50 text-amber-700'}`}>{d.status}</span>
          </div>
        ))}
        {drafts && drafts.length > 0 && (
          <p className="mt-1 text-[10px] text-stone-400">
            drafts are born approved — the governing approval already happened before the
            write was allowed; the buyer edits → exports → enters them in the ERP
            (direct ERP writes stay out of scope until a write connector exists).
          </p>
        )}
      </div>
      <div>
        <div className="text-[10px] uppercase tracking-wide text-stone-400 mb-2">Governed write actions ({writeActions.length})</div>
        {!actions && <div className="text-[11px] text-stone-400"><Loader2 size={14} className="animate-spin inline mr-2" />Loading…</div>}
        {actions && writeActions.length === 0 && (
          <div className="rounded-xl border border-stone-200 bg-white px-4 py-3 text-[11px] text-stone-400">
            No active write-type actions bound in this organization.
          </div>
        )}
        {writeActions.map((a) => (
          <GovernedActionCard key={a.id} a={a} onExecuted={load} />
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// E. BUSINESS OUTCOME TRACKING
// ---------------------------------------------------------------------------
function OutcomeTracking() {
  const [drafts, setDrafts] = useState(null);
  const [escalations, setEscalations] = useState(null);
  const [revisions, setRevisions] = useState(null);
  useEffect(() => {
    (async () => {
      try {
        const [d, e, r] = await Promise.all([
          authFetch('/api/ontology/po-drafts'),
          authFetch('/api/ontology/escalations'),
          authFetch('/api/ontology/revisions'),
        ]);
        if (d.ok) setDrafts(await d.json());
        if (e.ok) setEscalations((await e.json()).escalations || []);
        if (r.ok) setRevisions((await r.json()).revisions || []);
      } catch { toast.error('Failed to load outcome tracking'); }
    })();
  }, []);
  const openEsc = (escalations || []).filter((x) => x.status !== 'resolved');
  const resolvedEsc = (escalations || []).filter((x) => x.status === 'resolved');
  const approvedDrafts = (drafts || []).filter((x) => x.status === 'approved');
  const steps = [
    ['Problem detected', openEsc.length ? `${openEsc.length} open` : escalations ? 'none open' : '…'],
    ['Evidence collected', 'governed metrics + document evidence'],
    ['Recommendation generated', escalations?.length ? `${escalations.length} escalated` : 'none yet'],
    ['Decision reviewed', resolvedEsc.length ? `${resolvedEsc.length} resolved` : 'pending'],
    ['Action approved', approvedDrafts.length ? `${approvedDrafts.length} approval-gated write(s)` : 'none yet'],
    ['Action executed', approvedDrafts.length ? 'recorded — ERP entry by the buyer (no write connector yet)' : '—'],
    ['Outcome measured', 'not yet wired'],
  ];
  return (
    <div className="space-y-4">
      <h2 className="text-sm font-semibold text-stone-800 flex items-center gap-2"><Activity size={15} className="text-stone-600" /> Business Outcome Tracking</h2>
      <span className="text-[11px] text-stone-400">the complete decision lifecycle — only realized value is reported when supporting measurement exists</span>
      <div className="rounded-xl border border-stone-200 bg-white px-4 py-4">
        {steps.map(([label, state], i) => {
          const done = !['…', 'pending', 'none yet', '—', 'not yet wired', 'none open'].includes(state);
          return (
            <div key={i} className="flex items-center gap-3 py-2">
              <div className={`w-6 h-6 rounded-full flex items-center justify-center text-[10px] ${done ? 'bg-emerald-100 text-emerald-700' : 'bg-stone-100 text-stone-400'}`}>{i + 1}</div>
              <span className="text-[12px] text-stone-700">{label}</span>
              <span className="ml-auto text-[10px] text-stone-400">{state}</span>
            </div>
          );
        })}
      </div>
      {approvedDrafts.length > 0 && (
        <div className="rounded-xl border border-stone-200 bg-white px-4 py-3">
          <div className="text-[10px] uppercase tracking-wide text-stone-400 mb-2">Per-decision lifecycle (approval-gated writes)</div>
          {approvedDrafts.map((d) => (
            <div key={d.id} className="py-1.5 border-b border-stone-100 last:border-0 text-[11px] text-stone-600">
              <span className="text-stone-800 font-medium">{d.supplier_name}</span>
              {' · '}{d.product} · {d.currency || ''} {d.est_amount != null ? Number(d.est_amount).toLocaleString() : '—'}
              <span className="ml-2 text-[10px] text-stone-400">
                approved write recorded{d.approval_id ? ` (approval ${String(d.approval_id).slice(0, 8)}…)` : ''} → ERP entry pending buyer → outcome measurement not yet wired
              </span>
            </div>
          ))}
        </div>
      )}
      {revisions && revisions.length > 0 && (
        <div className="rounded-xl border border-stone-200 bg-white px-4 py-3 text-[11px] text-stone-600">
          <span className="text-[10px] uppercase tracking-wide text-stone-400">Governance trail </span>
          {revisions.length} append-only revision(s) recorded — every ontology change is traceable (audit preserved).
        </div>
      )}
      <div className="rounded-xl border border-amber-200 bg-amber-50/40 px-4 py-3 text-[11px] text-stone-600">
        Realized financial/operational outcome measurement is not yet wired to a measurement service.
        When connected, actual-vs-expected deltas will appear here — no ROI is reported until it can be
        supported by actual measurements.
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// F. OPERATIONAL HEALTH & GOVERNANCE
// ---------------------------------------------------------------------------
function OperationalHealth({ overview, value, selectedDomain, setSelectedDomain }) {
  const totals = overview?.totals;
  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-sm font-semibold text-stone-800 flex items-center gap-2"><ShieldCheck size={15} className="text-stone-600" /> Operational Health & Governance</h2>
        <span className="text-[11px] text-stone-400">technical administration, consolidated — business impact is shown in the experiences above</span>
        <div className="mt-3 grid grid-cols-2 sm:grid-cols-5 gap-3">
          {[['Domains', totals?.domains], ['Objects', totals?.objects], ['Objects live', totals?.objects_live], ['Actions', totals?.actions], ['Links', totals?.links]].map(([label, v]) => (
            <div key={label} className="rounded-xl border border-stone-200 bg-white px-4 py-3">
              <div className="text-lg font-semibold text-stone-800">{v}</div>
              <div className="text-[10px] uppercase tracking-wide text-stone-400">{label}</div>
            </div>
          ))}
        </div>
      </div>
      <div>
        <h3 className="text-[13px] font-semibold text-stone-800">Coverage Map</h3>
        <span className="text-[11px] text-stone-400">honest readiness — “live” only counts objects backed by real data + an executable action</span>
        <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {overview?.domains.map((d) => (
            <CoverageCard key={d.id} domain={d} value={value.find((v) => v.domain_id === d.id)} selected={d.id === selectedDomain} onClick={() => setSelectedDomain(d.id)} />
          ))}
        </div>
      </div>
      <SensorsCard />
      <RevisionsFeed />
      <PoDraftsCard onExport={() => {}} exporting={false} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// SHELL
// ---------------------------------------------------------------------------
const EXPERIENCES = [
  { key: 'A', label: 'Executive', icon: Gauge },
  { key: 'B', label: 'Situation Room', icon: FileSearch },
  { key: 'C', label: 'Object Explorer', icon: Network },
  { key: 'D', label: 'Decisions', icon: Workflow },
  { key: 'E', label: 'Outcomes', icon: Activity },
  { key: 'F', label: 'Health', icon: ShieldCheck },
];

export default function EnterpriseProfile() {
  const [exp, setExp] = useState('A');
  const [overview, setOverview] = useState(null);
  const [objects, setObjects] = useState([]);
  const [value, setValue] = useState([]);
  const [selectedDomain, setSelectedDomain] = useState(null);
  const [discovering, setDiscovering] = useState(false);

  const load = useCallback(async () => {
    try {
      const [ovRes, objRes, valRes] = await Promise.all([
        authFetch('/api/ontology/overview'),
        authFetch('/api/ontology/objects'),
        authFetch('/api/ontology/coverage-value'),
      ]);
      if (ovRes.ok) { const ov = await ovRes.json(); setOverview(ov); setSelectedDomain((c) => c || ov.domains?.[0]?.id || null); }
      if (objRes.ok) setObjects(await objRes.json());
      if (valRes.ok) setValue((await valRes.json()).domains || []);
    } catch { toast.error('Failed to load the enterprise profile'); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const runDiscovery = async () => {
    setDiscovering(true);
    try {
      const res = await authFetch('/api/ontology/discover', { method: 'POST' });
      if (!res.ok) { const b = await res.json().catch(() => ({})); throw new Error(b.detail || `HTTP ${res.status}`); }
      const out = await res.json();
      toast.success(out.discovered > 0 ? `Discovery bound ${out.discovered} object(s)` : 'No new bindings');
      await load();
    } catch (e) { toast.error(`Discovery failed: ${e.message || e}`); }
    finally { setDiscovering(false); }
  };

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
          <h1 className="text-lg font-semibold text-stone-900">Enterprise Business Operating Model</h1>
          <span className="text-[11px] text-stone-400">governed intelligence → business decisions → measured outcomes</span>
          <button onClick={runDiscovery} disabled={discovering}
            className="ml-auto flex items-center gap-1.5 text-[12px] px-3 py-1.5 rounded-md border border-stone-300 bg-white text-stone-700 hover:bg-stone-100 disabled:opacity-50">
            {discovering ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />}
            {discovering ? 'Discovering…' : 'Run auto-discovery'}
          </button>
        </div>

        <div className="mt-5 flex items-center gap-1 border-b border-stone-200">
          {EXPERIENCES.map((e) => {
            const Icon = e.icon;
            const active = exp === e.key;
            return (
              <button key={e.key} onClick={() => setExp(e.key)}
                className={`flex items-center gap-1.5 px-3 py-2 text-[12px] border-b-2 -mb-px ${
                  active ? 'border-stone-800 text-stone-900 font-medium' : 'border-transparent text-stone-500 hover:text-stone-700'}`}>
                <Icon size={13} /> {e.label}
              </button>
            );
          })}
        </div>

        <div className="mt-6">
          {exp === 'A' && <ExecutiveOverview />}
          {exp === 'B' && <SituationRoom />}
          {exp === 'C' && <BusinessObjectExplorer />}
          {exp === 'D' && <DecisionCenter />}
          {exp === 'E' && <OutcomeTracking />}
          {exp === 'F' && (
            <OperationalHealth overview={overview} value={value}
              selectedDomain={selectedDomain} setSelectedDomain={setSelectedDomain} />
          )}
        </div>
      </div>
    </div>
  );
}
