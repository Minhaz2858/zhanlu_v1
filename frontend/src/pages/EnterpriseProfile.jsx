import { useState, useEffect, useCallback, useMemo } from 'react';
import { useSearchParams } from 'react-router-dom';
import {
  Building2, RefreshCw, Loader2, Download, MapPin, Radar, Plus,
  CircleDot, Layers, Database, ShieldCheck, History, Network, Boxes,
  Workflow, Search, AlertTriangle, CheckCircle2, Activity, Target, Gauge,
  GitBranch, TrendingDown, FileSearch, ShieldAlert, ClipboardCheck,
  ScrollText, Bot,
} from 'lucide-react';
import { toast } from 'sonner';
import { authFetch } from '@/api/authFetch';
import { useAuth } from '@/lib/AuthContext';
import { EVIDENCE_BADGE_STYLE } from '@/lib/evidenceStatuses';
import {
  ENTERPRISE_AREAS, ENTERPRISE_TABS,
  parseEnterpriseView, buildEnterpriseSearch,
} from '@/lib/enterpriseUrlState';
import OntologyGraph from '@/components/ontology/OntologyGraph';
import EnterpriseMap from '@/components/enterprise/EnterpriseMap';
import { BusinessObjectPanel, ResourcePanel } from '@/components/enterprise/Drilldown';
import ProcessFlowCanvas from '@/components/enterprise/ProcessFlowCanvas';
import ReviewCenter from '@/components/enterprise/ReviewCenter';
import BusinessRulesBrowser from '@/components/enterprise/BusinessRulesBrowser';
import WorkforcePanel from '@/components/enterprise/WorkforcePanel';
import ResourceDirectory from '@/components/enterprise/ResourceDirectory';
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
  live: 'bg-emerald-50 dark:bg-emerald-950/30 text-emerald-700 dark:text-emerald-300 border-emerald-200 dark:border-emerald-900/50',
  wired: 'bg-sky-50 text-sky-700 border-sky-200',
  drafted: 'bg-muted/60 text-muted-foreground border-border',
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
        isWired ? 'border-emerald-200 dark:border-emerald-900/50 bg-emerald-50 dark:bg-emerald-950/30 text-emerald-700' : 'border-border bg-muted/40 text-muted-foreground/80'
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
        selected ? 'border-border bg-muted/40' : 'border-border bg-card hover:border-border'
      }`}
    >
      <div className="flex items-center gap-2">
        <span className="text-[13px] font-medium text-foreground truncate">{domain.name}</span>
        <span className={`ml-auto text-[10px] px-1.5 py-0.5 rounded-full border capitalize ${READINESS_STYLE[readiness]}`}>
          {readiness}
        </span>
      </div>
      <p className="mt-1 text-[11px] text-muted-foreground/80 line-clamp-1">{domain.description}</p>
      <div className="mt-2 flex items-center gap-3 text-[10px] text-muted-foreground">
        <span>{domain.objects} objects</span>
        <span className="text-emerald-600">{cov.objects_live ?? 0} live</span>
        <span>{cov.actions_executable ?? 0} actions runnable</span>
      </div>
      {value && (value.est_value > 0 || value.agents?.length > 0) && (
        <div className="mt-1.5 flex items-center gap-3 text-[10px] text-muted-foreground/80">
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
    <div className="bg-card rounded-lg border border-border">
      <button onClick={() => setOpen(!open)} className="w-full flex items-center gap-2 px-4 py-3 text-left">
        <History size={14} className="text-muted-foreground" />
        <h2 className="text-sm font-semibold text-foreground">Ontology Revision History</h2>
        <span className="text-[11px] text-muted-foreground/80">append-only — every mutation, who made it, when</span>
        <span className="ml-auto text-muted-foreground/80 text-xs">{open ? 'hide' : 'show'}</span>
      </button>
      {open && (
        <div className="px-4 pb-3 max-h-72 overflow-y-auto">
          {!revisions && <div className="text-[11px] text-muted-foreground/80 py-2">Loading…</div>}
          {revisions && revisions.length === 0 && <div className="text-[11px] text-muted-foreground/80 py-2">No revisions yet.</div>}
          {revisions && revisions.map((r) => (
            <div key={r.id} className="flex items-center gap-2 py-1.5 border-b border-border/70 last:border-0 text-[11px]">
              <span className="font-mono text-muted-foreground/80">#{r.revision_no}</span>
              <span className="px-1.5 py-0.5 rounded-full border border-border bg-muted/40 text-foreground/80 capitalize">{r.change_kind}</span>
              <span className="text-foreground/80 capitalize">{r.entity_type}</span>
              <span className="text-muted-foreground/80 truncate max-w-[240px]" title={r.entity_id}>{r.entity_id.slice(0, 8)}…</span>
              <span className="text-muted-foreground/80">{r.changed_by || '—'}</span>
              <span className="ml-auto text-muted-foreground/60">{(r.created_date || '').replace('T', ' ').slice(0, 16)}</span>
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
        <h2 className="text-sm font-semibold text-foreground">PO Draft Worklist</h2>
        <span className="text-[11px] text-muted-foreground/80">approval-gated write-backs — every draft came from an approved action</span>
        <button onClick={onExport} disabled={exporting}
          className="ml-auto flex items-center gap-1.5 text-[11px] px-2.5 py-1.5 rounded-md border border-border bg-card text-foreground hover:bg-muted/40 disabled:opacity-50">
          {exporting ? <Loader2 size={12} className="animate-spin" /> : <Download size={12} />}
          Export xlsx
        </button>
      </div>
      {drafts.length === 0 ? (
        <p className="mt-3 text-[11px] text-muted-foreground/80">No PO drafts yet. They are created when an approved ontology write action runs.</p>
      ) : (
        <div className="mt-3 overflow-x-auto rounded-xl border border-border">
          <table className="min-w-full text-[11px]">
            <thead>
              <tr className="bg-muted/40 text-muted-foreground text-left">
                <th className="px-3 py-2 font-medium">Supplier</th>
                <th className="px-3 py-2 font-medium">Product</th>
                <th className="px-3 py-2 font-medium text-right">Qty</th>
                <th className="px-3 py-2 font-medium text-right">Est. Amount</th>
                <th className="px-3 py-2 font-medium">Status</th>
                <th className="px-3 py-2 font-medium">Created</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-stone-100 text-foreground">
              {drafts.map((d) => (
                <tr key={d.id}>
                  <td className="px-3 py-2">{d.supplier_name}</td>
                  <td className="px-3 py-2">{d.product}</td>
                  <td className="px-3 py-2 text-right">{d.quantity} {d.unit}</td>
                  <td className="px-3 py-2 text-right">{d.currency} {(d.est_amount ?? 0).toLocaleString()}</td>
                  <td className="px-3 py-2">
                    <span className={`px-1.5 py-0.5 rounded-full border text-[10px] ${
                      d.status === 'approved' ? 'border-emerald-200 dark:border-emerald-900/50 bg-emerald-50 dark:bg-emerald-950/30 text-emerald-700' : 'border-amber-200 dark:border-amber-900/50 bg-amber-50 dark:bg-amber-950/30 text-amber-700'}`}>
                      {d.status}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-muted-foreground/80">{d.created_date ? new Date(d.created_date).toLocaleDateString() : '—'}</td>
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
    if (!s.enabled) return ['paused', 'border-border bg-muted/40 text-muted-foreground/80'];
    if (st.breached) return ['BREACH', 'border-red-200 dark:border-red-900/50 bg-red-50 dark:bg-red-950/30 text-red-700'];
    if (s.last_run_at) return ['watching', 'border-emerald-200 dark:border-emerald-900/50 bg-emerald-50 dark:bg-emerald-950/30 text-emerald-700'];
    return ['new', 'border-sky-200 bg-sky-50 text-sky-700'];
  };
  return (
    <div className="mt-6">
      <div className="flex items-center gap-2">
        <Radar size={15} className="text-foreground/80" />
        <h2 className="text-sm font-semibold text-foreground">Sensing</h2>
        <span className="text-[11px] text-muted-foreground/80">scheduled watches on your real data — on breach, linked-domain agents are notified</span>
        <button onClick={() => setShowForm((v) => !v)} className="ml-auto flex items-center gap-1.5 text-[11px] px-2.5 py-1.5 rounded-md border border-border bg-card text-foreground hover:bg-muted/40">
          <Plus size={12} /> New sensor
        </button>
      </div>
      {showForm && (
        <div className="mt-3 rounded-xl border border-border bg-card p-4 grid grid-cols-1 sm:grid-cols-2 gap-3 text-[12px]">
          <input className="border border-border rounded-md px-2.5 py-1.5" placeholder="Sensor name"
            value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <select className="border border-border rounded-md px-2.5 py-1.5" value={form.object_name} onChange={(e) => setForm({ ...form, object_name: e.target.value })}>
            <option value="">Notify object…</option>
            {data.objects.map((o) => <option key={o.name} value={o.name}>{o.display_name || o.name}</option>)}
          </select>
          <select className="border border-border rounded-md px-2.5 py-1.5" value={form.kb_id} onChange={(e) => setForm({ ...form, kb_id: e.target.value })}>
            <option value="">Data source…</option>
            {data.data_sources.map((k) => <option key={k.id} value={k.id}>{k.name} ({k.db_type || 'database'})</option>)}
          </select>
          <div className="flex items-center gap-2">
            <select className="border border-border rounded-md px-2 py-1.5" value={form.op} onChange={(e) => setForm({ ...form, op: e.target.value })}>
              {['gt', 'gte', 'lt', 'lte', 'eq'].map((o) => <option key={o} value={o}>{o}</option>)}
            </select>
            <input type="number" step="any" className="w-24 border border-border rounded-md px-2.5 py-1.5" value={form.threshold} onChange={(e) => setForm({ ...form, threshold: Number(e.target.value) })} />
            <select className="border border-border rounded-md px-2 py-1.5" value={form.schedule_seconds} onChange={(e) => setForm({ ...form, schedule_seconds: Number(e.target.value) })}>
              <option value={300}>every 5 min</option><option value={3600}>every hour</option><option value={21600}>every 6 h</option><option value={86400}>daily</option>
            </select>
          </div>
          <textarea className="sm:col-span-2 border border-border rounded-md px-2.5 py-1.5 font-mono text-[11px]" rows={2}
            placeholder="Watch SQL — must return ONE numeric value" value={form.watch_sql} onChange={(e) => setForm({ ...form, watch_sql: e.target.value })} />
          <div className="sm:col-span-2 flex items-center gap-2">
            <button onClick={create} disabled={creating}
              className="flex items-center gap-1.5 text-[11px] px-3 py-1.5 rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50">
              {creating ? <Loader2 size={12} className="animate-spin" /> : <ShieldCheck size={12} />} Create sensor
            </button>
          </div>
        </div>
      )}
      {data.sensors.length === 0 ? (
        <p className="mt-3 text-[11px] text-muted-foreground/80">No sensors yet. A sensor watches a query on a connected data source and notifies bound agents on breach.</p>
      ) : (
        <div className="mt-3 space-y-2">
          {data.sensors.map((s) => {
            const [label, cls] = stateChip(s);
            return (
              <div key={s.id} className="rounded-xl border border-border bg-card px-4 py-3">
                <div className="flex items-center gap-2">
                  <span className="text-[13px] font-medium text-foreground">{s.name}</span>
                  <span className={`text-[10px] px-1.5 py-0.5 rounded-full border ${cls}`}>{label}</span>
                  <span className="text-[10px] text-muted-foreground/80">watches {s.object_name} · {s.op} {s.threshold} · every {s.schedule_seconds >= 3600 ? `${s.schedule_seconds / 3600}h` : `${s.schedule_seconds / 60}m`}</span>
                  <div className="ml-auto flex items-center gap-1.5">
                    <button onClick={() => runNow(s.id)} className="text-[11px] px-2 py-1 rounded-md border border-border text-foreground/80 hover:bg-muted/40">Run now</button>
                    <button onClick={() => remove(s.id)} className="text-[11px] px-2 py-1 rounded-md border border-border text-muted-foreground/80 hover:bg-red-50 dark:hover:bg-red-950/30 hover:text-red-600">Remove</button>
                  </div>
                </div>
                {s.recent_runs?.length > 0 && (
                  <div className="mt-1.5 flex items-center gap-3 text-[10px] text-muted-foreground/80">
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
      <div className="rounded-xl border border-border bg-card px-4 py-3.5">
        <div className="text-[11px] text-muted-foreground/80">{m.name}</div>
        <Loader2 size={16} className="animate-spin mt-2 text-muted-foreground/60" />
      </div>
    );
  }
  const unavail = !compare || compare.unavailable
    || (compare.current && compare.current.unavailable);
  if (unavail) {
    return (
      <div className="rounded-xl border border-amber-200 dark:border-amber-900/50 bg-amber-50/40 dark:bg-amber-950/20 px-4 py-3.5">
        <div className="text-[13px] font-medium text-foreground">{m.name}</div>
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
    <div className="rounded-xl border border-border bg-card px-4 py-3.5">
      <div className="flex items-center gap-2">
        <span className="text-[13px] font-semibold text-foreground">{m.name}</span>
        <span className="ml-auto text-[10px] text-muted-foreground/80">{compare.current_period || m.unit || ''}</span>
      </div>
      <div className="mt-1 text-2xl font-semibold text-foreground tabular-nums">
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
      <div className="mt-1 text-[10px] text-muted-foreground/80">
        {cur.provenance?.datasource_name || m.datasource_id}
        {cur.provenance?.base_table ? ` · ${cur.provenance.base_table}` : ''}
      </div>
      <details className="mt-1">
        <summary className="cursor-pointer text-[10px] text-muted-foreground hover:text-foreground">definition & provenance</summary>
        <div className="mt-1 text-[10px] text-muted-foreground space-y-0.5">
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
        <h2 className="text-sm font-semibold text-foreground flex items-center gap-2"><Gauge size={15} className="text-foreground/80" /> Governed Business KPIs</h2>
        <span className="text-[11px] text-muted-foreground/80">every figure is computed by the governed MetricDefinition (current vs prior period, deterministic) — never estimated, never invented</span>
        <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {!metrics && <div className="text-[11px] text-muted-foreground/80 py-4"><Loader2 size={14} className="animate-spin inline mr-2" />Loading KPIs…</div>}
          {metrics && kpiList.length === 0 && (
            <div className="col-span-full rounded-xl border border-dashed border-border bg-card py-8 text-center text-[12px] text-muted-foreground/80">
              No governed, published metrics are configured for this organization yet.
            </div>
          )}
          {kpiList.map((m) => (
            <MetricCard key={m.id} m={m} kpi={kpis[m.id]} />
          ))}
        </div>
      </div>

      <div>
        <h2 className="text-sm font-semibold text-foreground flex items-center gap-2"><AlertTriangle size={15} className="text-red-600" /> Business Priorities</h2>
        <span className="text-[11px] text-muted-foreground/80">deterministically detected situations requiring attention — declining profitability (governed gross-margin by segment) and sensing breaches</span>
        <div className="mt-3 space-y-2">
          {priorities === undefined && <div className="text-[11px] text-muted-foreground/80 py-2"><Loader2 size={14} className="animate-spin inline mr-2" />Analyzing…</div>}
          {priorities && declining.length === 0 && breaches.length === 0 && (
            <div className="rounded-xl border border-border bg-card px-4 py-3 text-[11px] text-muted-foreground/80">No declining profitability or active breaches detected.</div>
          )}
          {declining.map((s) => {
            const pct = s.delta_pct != null ? `${s.delta_pct.toFixed(1)}%` : 'n/a';
            return (
              <div key={s.segment} className="rounded-xl border border-red-200 dark:border-red-900/50 bg-red-50/40 dark:bg-red-950/20 px-4 py-3">
                <div className="flex items-center gap-2">
                  <TrendingDown size={14} className="text-red-600" />
                  <span className="text-[13px] font-medium text-foreground">Vehicle Model {s.segment} profitability declining</span>
                  <span className="ml-auto text-[10px] text-red-600">{pct} vs prior</span>
                </div>
                <p className="mt-1 text-[11px] text-foreground/80">
                  Gross margin fell from <b>{priorities.currency || ''} {Number(s.prior_value).toLocaleString()}</b> to{' '}
                  <b>{priorities.currency || ''} {Number(s.current_value).toLocaleString()}</b> ({priorities.current_period} vs {priorities.prior_period}).
                  Investigate the affected model's sales, component costs, and linked suppliers.
                </p>
              </div>
            );
          })}
          {breaches.map((s) => (
            <div key={s.id} className="rounded-xl border border-red-200 dark:border-red-900/50 bg-red-50/40 dark:bg-red-950/20 px-4 py-3">
              <div className="flex items-center gap-2">
                <TrendingDown size={14} className="text-red-600" />
                <span className="text-[13px] font-medium text-foreground">{s.name}</span>
                <span className="text-[10px] text-muted-foreground/80">watches {s.object_name}</span>
              </div>
              <p className="mt-1 text-[11px] text-foreground/80">A watched signal for <b>{s.object_name}</b> crossed its threshold ({s.op} {s.threshold}). Investigate the affected business entities and governed metrics.</p>
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
        <Search size={14} className="text-muted-foreground" />
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search business objects…"
          className="flex-1 border border-border rounded-md px-3 py-1.5 text-[12px]" />
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="rounded-xl border border-border bg-card max-h-[520px] overflow-y-auto">
          <div className="px-3 py-2 text-[10px] uppercase tracking-wide text-muted-foreground/80 border-b border-border/70">Objects ({filtered.length})</div>
          <ul className="divide-y divide-stone-100">
            {filtered.map((o) => (
              <li key={o.id}>
                <button onClick={() => setSelected(o.id)} className={`w-full text-left px-3 py-2 hover:bg-muted/40 flex items-center gap-2 ${selected === o.id ? 'bg-muted/40' : ''}`}>
                  <Boxes size={12} className="text-muted-foreground/80" />
                  <span className="text-[12px] text-foreground">{o.display_name || o.name}</span>
                  <EvidenceBadge obj={o} />
                </button>
              </li>
            ))}
          </ul>
        </div>
        <div className="lg:col-span-2 space-y-3">
          {!sel && <div className="rounded-xl border border-dashed border-border bg-card py-12 text-center text-[12px] text-muted-foreground/80">Select an object to explore its business relationships, governed metrics, and evidence.</div>}
          {sel && (
            <>
              <div className="rounded-xl border border-border bg-card px-4 py-3">
                <div className="text-[13px] font-semibold text-foreground">{sel.display_name || sel.name}</div>
                <div className="mt-1 flex items-center gap-2 text-[10px] text-muted-foreground/80"><BackingBadge obj={sel} /><EvidenceBadge obj={sel} /></div>
              </div>
              <div className="rounded-xl border border-border bg-card px-4 py-3">
                <div className="text-[10px] uppercase tracking-wide text-muted-foreground/80 mb-2 flex items-center gap-1"><GitBranch size={11} /> Relationships ({related.length})</div>
                {related.length === 0 && <div className="text-[11px] text-muted-foreground/80">No linked objects.</div>}
                <ul className="space-y-1">
                  {related.map((l, i) => (
                    <li key={i} className="text-[11px] text-foreground/80 flex items-center gap-2">
                      <span className="capitalize px-1 rounded bg-muted/60">{l.link_type}</span>
                      {relatedObjs[i]?.display_name || relatedObjs[i]?.name || l.to_object_id}
                    </li>
                  ))}
                </ul>
              </div>
              <div className="rounded-xl border border-border bg-card px-4 py-3">
                <div className="text-[10px] uppercase tracking-wide text-muted-foreground/80 mb-2 flex items-center gap-1"><Target size={11} /> Governed metrics</div>
                {metrics.length === 0 && <div className="text-[11px] text-muted-foreground/80">No governed metric backs this object.</div>}
                <div className="flex flex-wrap gap-2">
                  {metrics.map((m) => (
                    <span key={m.id} className="text-[10px] px-2 py-1 rounded-lg border border-border bg-card">{m.name}</span>
                  ))}
                </div>
              </div>
              <div className="rounded-xl border border-border bg-card px-4 py-3">
                <div className="text-[10px] uppercase tracking-wide text-muted-foreground/80 mb-2 flex items-center gap-1"><FileSearch size={11} /> Document evidence ({docEv.length})</div>
                {docEv.length === 0 && <div className="text-[11px] text-muted-foreground/80">none visible to you (per-viewer, fail-closed)</div>}
                {docEv.map((d, i) => (
                  <div key={i} className="rounded-lg border border-sky-100 bg-sky-50/50 px-2 py-1.5 mb-1 text-[11px]">
                    <div className="text-foreground italic">“{d.quote}”</div>
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
        <Network size={15} className="text-foreground/80" />
        <h2 className="text-sm font-semibold text-foreground">Business Object Explorer</h2>
        <div className="ml-auto flex items-center gap-1 text-[11px]">
          <button onClick={() => setMode('business')} className={`px-2.5 py-1.5 rounded-md border ${mode === 'business' ? 'border-border bg-muted/60 text-foreground' : 'border-border text-muted-foreground'}`}>Business Explorer</button>
          <button onClick={() => setMode('studio')} className={`px-2.5 py-1.5 rounded-md border ${mode === 'studio' ? 'border-border bg-muted/60 text-foreground' : 'border-border text-muted-foreground'}`}>Ontology Studio</button>
        </div>
      </div>
      <span className="text-[11px] text-muted-foreground/80">
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
      <div className="rounded-xl border border-border bg-card px-4 py-3">
        <div className="flex items-center gap-2">
          <FileSearch size={14} className="text-foreground/80" />
          <span className="text-[12px] font-medium text-foreground">Investigate a business question</span>
        </div>
        <textarea value={question} onChange={(e) => setQuestion(e.target.value)} rows={2}
          placeholder="e.g. Why is Vehicle Model X's profitability declining?"
          className="mt-2 w-full border border-border rounded-md px-3 py-2 text-[12px]" />
        <div className="mt-2 flex items-center gap-2">
          <button onClick={askAgent} disabled={asking || !question.trim()}
            className="flex items-center gap-1.5 text-[11px] px-3 py-1.5 rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50">
            {asking ? <Loader2 size={12} className="animate-spin" /> : <FileSearch size={12} />}
            Ask the Zhanlu Agent
          </button>
          {sel && (
            <button onClick={() => setQuestion(`Why is ${sel.display_name || sel.name}'s profitability declining?`)}
              className="text-[11px] px-2.5 py-1.5 rounded-md border border-border text-foreground/80 hover:bg-muted/40">
              Investigate selected entity
            </button>
          )}
          <span className="ml-auto text-[10px] text-muted-foreground/80">synthesis via the existing Agent Harness</span>
        </div>
        {answerError && <p className="mt-2 text-[11px] text-red-600">{answerError}</p>}
        {answer && (
          <div className="mt-2 rounded-lg border border-border bg-muted/40 px-3 py-2 text-[11px] space-y-1">
            <div className="text-foreground">{answer.answer}</div>
            {answer.sql && <div className="font-mono text-[10px] text-muted-foreground break-all">SQL: {answer.sql}</div>}
            {Array.isArray(answer.rows) && answer.rows.length > 0 && (
              <div className="text-[10px] text-muted-foreground">rows: {JSON.stringify(answer.rows)}</div>
            )}
          </div>
        )}
        <p className="mt-1 text-[10px] text-muted-foreground/80">
          Structured investigation (below) uses the ontology graph, governed metrics and document evidence.
          The agent synthesis above reuses the existing Agent Harness data-agent (scoped to this org's
          bound sources, write-denied, budget-bounded) — no new orchestration framework.
        </p>
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="rounded-xl border border-border bg-card max-h-[520px] overflow-y-auto">
          <div className="px-3 py-2 text-[10px] uppercase tracking-wide text-muted-foreground/80 border-b">Entities</div>
          <ul className="divide-y divide-stone-100">
            {objects.map((o) => (
              <li key={o.id}><button onClick={() => setSelected(o.id)} className={`w-full text-left px-3 py-2 hover:bg-muted/40 text-[12px] ${selected === o.id ? 'bg-muted/40' : ''}`}>{o.display_name || o.name}</button></li>
            ))}
          </ul>
        </div>
        <div className="lg:col-span-2 space-y-3">
          {!sel && <div className="rounded-xl border border-dashed border-border bg-card py-12 text-center text-[12px] text-muted-foreground/80">Select an entity to investigate.</div>}
          {sel && (
            <>
              <div className="rounded-xl border border-border bg-card px-4 py-3">
                <div className="text-[10px] uppercase tracking-wide text-muted-foreground/80">Verified findings</div>
                <ul className="mt-1 space-y-1">
                  {facts.map((f, i) => <li key={i} className="text-[11px] text-foreground/80 flex items-center gap-2"><CheckCircle2 size={12} className="text-emerald-600" />{f}</li>)}
                </ul>
              </div>
              <div className="rounded-xl border border-border bg-card px-4 py-3">
                <div className="text-[10px] uppercase tracking-wide text-muted-foreground/80">Affected business relationships</div>
                <ul className="mt-1 space-y-1">
                  {related.map((l, i) => <li key={i} className="text-[11px] text-foreground/80"><span className="capitalize px-1 rounded bg-muted/60">{l.link_type}</span> {l.to_object_id}</li>)}
                </ul>
              </div>
              <div className="rounded-xl border border-amber-200 dark:border-amber-900/50 bg-amber-50/40 dark:bg-amber-950/20 px-4 py-3">
                <div className="text-[10px] uppercase tracking-wide text-muted-foreground">Hypotheses & recommended investigations</div>
                <p className="mt-1 text-[11px] text-foreground/80">Correlation is not causation. Inspect governed metrics for the linked objects, confirm with document evidence, and validate against source data before concluding a driver.</p>
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
    <div className="rounded-xl border border-border bg-card px-4 py-3 mb-2">
      <div className="flex items-center gap-2">
        <ShieldCheck size={14} className={a.risk_level === 'write' ? 'text-amber-600' : 'text-muted-foreground/80'} />
        <span className="text-[13px] font-medium text-foreground">{a.name}</span>
        <span className="text-[10px] px-1.5 py-0.5 rounded-full border border-border bg-muted/40 text-muted-foreground">{a.risk_level}</span>
        {a.tool_name && <span className="text-[10px] text-muted-foreground/80">tool: {a.tool_name}</span>}
        <button onClick={() => setOpen(!open)}
          className="ml-auto text-[11px] px-2.5 py-1 rounded-md border border-border text-foreground/80 hover:bg-muted/40">
          {open ? 'Close' : 'Execute…'}
        </button>
      </div>
      {open && (
        <div className="mt-2 space-y-2">
          <p className="text-[10px] text-muted-foreground/80">
            A write runs ONLY under an org-scoped approval bound to this exact
            action + tool (governance gate: pending, wrong-org, expired or
            param-drifted approvals are refused — fail closed).
          </p>
          <div className="flex items-center gap-2">
            <input value={approvalId} onChange={(e) => setApprovalId(e.target.value)}
              placeholder="approval id (from the governance review)"
              className="flex-1 border border-border rounded-md px-2.5 py-1.5 text-[11px]" />
            <button onClick={execute} disabled={busy || !approvalId.trim()}
              className="flex items-center gap-1.5 text-[11px] px-3 py-1.5 rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50">
              {busy ? <Loader2 size={12} className="animate-spin" /> : <ShieldCheck size={12} />}
              Execute
            </button>
          </div>
          {result && (
            <div className={`rounded-lg px-3 py-2 text-[11px] ${
              result.success ? 'border border-emerald-200 dark:border-emerald-900/50 bg-emerald-50/60 dark:bg-emerald-950/30 text-emerald-800'
                : result.blocked ? 'border border-amber-200 dark:border-amber-900/50 bg-amber-50/60 dark:bg-amber-950/30 text-amber-800'
                : 'border border-red-200 dark:border-red-900/50 bg-red-50/60 dark:bg-red-950/30 text-red-700'}`}>
              {result.success
                ? <span>Executed under approval {result.approval_id} (approved by {result.approved_by || 'admin'}){result.result?.external_ref ? ` — ERP ref ${result.result.external_ref}` : ''} — result recorded.</span>
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
      // /escalations returns a bare array — normalize both shapes.
      const ej = e.ok ? await e.json() : [];
      setEscalations(Array.isArray(ej) ? ej : ej.escalations || []);
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
      <h2 className="text-sm font-semibold text-foreground flex items-center gap-2"><Workflow size={15} className="text-foreground/80" /> Decision & Action Center</h2>
      <span className="text-[11px] text-muted-foreground/80">recommendations surface from sensing breaches; every operational write stays approval-gated via governed actions</span>
      <div>
        <div className="text-[10px] uppercase tracking-wide text-muted-foreground/80 mb-2">Open escalations</div>
        {!escalations && <div className="text-[11px] text-muted-foreground/80"><Loader2 size={14} className="animate-spin inline mr-2" />Loading…</div>}
        {escalations && escalations.length === 0 && <div className="rounded-xl border border-border bg-card px-4 py-3 text-[11px] text-muted-foreground/80">No open escalations.</div>}
        {escalations?.map((e) => (
          <div key={e.id} className="rounded-xl border border-border bg-card px-4 py-3 mb-2">
            <div className="flex items-center gap-2">
              <ShieldAlert size={14} className="text-amber-600" />
              <span className="text-[13px] font-medium text-foreground">{e.summary?.slice(0, 70) || e.id}</span>
              <span className="text-[10px] text-muted-foreground/80 ml-auto">{e.status}</span>
            </div>
            {e.summary && <p className="mt-1 text-[11px] text-foreground/80">{e.summary}</p>}
            {e.context_json && <p className="mt-1 text-[10px] text-muted-foreground/80 font-mono break-all">{JSON.stringify(e.context_json).slice(0, 200)}</p>}
            <div className="mt-2 flex items-center gap-2">
              <span className="text-[10px] text-muted-foreground/80">Approval required · execution via governed action</span>
              <button onClick={() => resolve(e.id)} className="ml-auto text-[11px] px-2.5 py-1 rounded-md border border-border text-foreground/80 hover:bg-muted/40">Resolve</button>
            </div>
          </div>
        ))}
      </div>
      <div>
        <div className="text-[10px] uppercase tracking-wide text-muted-foreground/80 mb-2">Approval-gated actions (PO drafts)</div>
        {!drafts && <div className="text-[11px] text-muted-foreground/80"><Loader2 size={14} className="animate-spin inline mr-2" />Loading…</div>}
        {drafts && drafts.length === 0 && <div className="rounded-xl border border-border bg-card px-4 py-3 text-[11px] text-muted-foreground/80">No pending actions.</div>}
        {drafts?.map((d) => (
          <div key={d.id} className="rounded-xl border border-border bg-card px-4 py-3 mb-2 flex items-center gap-2">
            <Activity size={14} className="text-muted-foreground" />
            <span className="text-[12px] text-foreground">{d.supplier_name} · {d.product} · {d.quantity} {d.unit}</span>
            <span className={`ml-auto text-[10px] px-1.5 py-0.5 rounded-full border ${d.status === 'approved' ? 'border-emerald-200 dark:border-emerald-900/50 bg-emerald-50 dark:bg-emerald-950/30 text-emerald-700' : 'border-amber-200 dark:border-amber-900/50 bg-amber-50 dark:bg-amber-950/30 text-amber-700'}`}>{d.status}</span>
          </div>
        ))}
        {drafts && drafts.length > 0 && (
          <p className="mt-1 text-[10px] text-muted-foreground/80">
            drafts are born approved — the governing approval already happened before the
            write was allowed; the buyer edits → exports → enters them in the ERP
            (direct ERP writes stay out of scope until a write connector exists).
          </p>
        )}
      </div>
      <div>
        <div className="text-[10px] uppercase tracking-wide text-muted-foreground/80 mb-2">Governed write actions ({writeActions.length})</div>
        {!actions && <div className="text-[11px] text-muted-foreground/80"><Loader2 size={14} className="animate-spin inline mr-2" />Loading…</div>}
        {actions && writeActions.length === 0 && (
          <div className="rounded-xl border border-border bg-card px-4 py-3 text-[11px] text-muted-foreground/80">
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
const IMPACT_FMT = (v, unit) => (v == null ? '—'
  : `${v > 0 ? '+' : ''}${Number(v).toLocaleString()}${unit ? ` ${unit}` : ''}`);

function OutcomeCard({ o, onChanged }) {
  const [busy, setBusy] = useState(false);
  const act = async (verb) => {
    setBusy(true);
    try {
      const res = await authFetch(`/api/ontology/outcomes/${o.id}/${verb}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: verb === 'verify' ? JSON.stringify({ decision: 'verified in Outcome Tracking' }) : undefined,
      });
      const data = await res.json();
      if (!res.ok || data.success === false) {
        toast.error(data.detail || data.reason || `HTTP ${res.status}`);
      } else {
        toast.success(verb === 'measure' ? 'Measured via governed metrics' : 'Outcome verified');
        onChanged?.();
      }
    } catch (e) { toast.error(`${verb} failed: ${e.message || e}`); }
    finally { setBusy(false); }
  };
  const vBadge = { verified: 'border-emerald-200 dark:border-emerald-900/50 bg-emerald-50 dark:bg-emerald-950/30 text-emerald-700', rejected: 'border-red-200 dark:border-red-900/50 bg-red-50 dark:bg-red-950/30 text-red-600' }[o.verification_status]
    || 'border-border bg-muted/40 text-muted-foreground';
  const good = o.realized_impact != null && o.realized_impact > 0;
  return (
    <div className="rounded-xl border border-border bg-card px-4 py-3 mb-2">
      <div className="flex items-center gap-2">
        <Target size={14} className="text-muted-foreground" />
        <span className="text-[13px] font-medium text-foreground">{o.metric_name}</span>
        <span className="text-[10px] px-1.5 py-0.5 rounded-full border border-border bg-muted/40 text-muted-foreground">{o.direction}</span>
        <span className={`text-[10px] px-1.5 py-0.5 rounded-full border capitalize ${vBadge}`}>{o.verification_status}</span>
        <span className="ml-auto text-[10px] text-muted-foreground/80">{o.baseline_period} → {o.observed_period}</span>
      </div>
      <div className="mt-2 grid grid-cols-3 gap-2 text-[11px]">
        <div>
          <div className="text-[10px] uppercase tracking-wide text-muted-foreground/80">Expected</div>
          <div className="text-foreground">{IMPACT_FMT(o.expected_impact, o.unit)}</div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wide text-muted-foreground/80">Measured (realized)</div>
          <div className={o.realized_impact == null ? 'text-muted-foreground/80' : good ? 'text-emerald-700' : 'text-red-600'}>
            {o.measurement_status === 'pending' ? 'not yet measured' : IMPACT_FMT(o.realized_impact, o.unit)}
          </div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wide text-muted-foreground/80">Governed values</div>
          <div className="text-foreground/80">{Number(o.baseline_value).toLocaleString()} → {o.observed_value != null ? Number(o.observed_value).toLocaleString() : '—'}</div>
        </div>
      </div>
      {o.verification_status === 'verified' && (
        <p className="mt-1.5 text-[10px] text-emerald-700">
          Verified by {o.verified_by}{o.verified_at ? ` · ${new Date(o.verified_at).toLocaleString()}` : ''}{o.verification_note ? ` — ${o.verification_note}` : ''}
        </p>
      )}
      <div className="mt-2 flex items-center gap-2">
        <span className="text-[10px] text-muted-foreground/80">baseline & observed computed by the governed MetricDefinition — the LLM never calculates impact</span>
        <div className="ml-auto flex items-center gap-1.5">
          {o.measurement_status !== 'measured' && o.verification_status !== 'verified' && (
            <button onClick={() => act('measure')} disabled={busy}
              className="text-[11px] px-2.5 py-1 rounded-md border border-border text-foreground/80 hover:bg-muted/40 disabled:opacity-50">Measure</button>
          )}
          {o.measurement_status === 'measured' && o.verification_status !== 'verified' && (
            <button onClick={() => act('verify')} disabled={busy}
              className="text-[11px] px-2.5 py-1 rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50">Verify</button>
          )}
        </div>
      </div>
    </div>
  );
}

function LifecycleChain() {
  const [ref, setRef] = useState('');
  const [chain, setChain] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const load = async () => {
    const id = (ref || '').trim();
    if (!id) return;
    setBusy(true); setErr(null); setChain(null);
    try {
      const res = await authFetch(`/api/ontology/decisions/${encodeURIComponent(id)}/lifecycle`);
      const data = await res.json();
      if (res.status === 404) setErr(data.detail || 'No decision record found for this reference in your organization.');
      else if (!res.ok) setErr(data.detail || `HTTP ${res.status}`);
      else setChain(data);
    } catch (e) { setErr(e.message || String(e)); }
    finally { setBusy(false); }
  };
  const STAGE_LABELS = {
    problem: 'Problem detected', approval: 'Decision approved',
    execution: 'Action executed', measurement: 'Impact measured',
    verification: 'Outcome verified',
  };
  return (
    <div className="rounded-xl border border-border bg-card px-4 py-3">
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground/80 mb-2">Decision lifecycle — trace any decision end to end</div>
      <div className="flex items-center gap-2">
        <input value={ref} onChange={(e) => setRef(e.target.value)}
          placeholder="escalation / approval / PO draft / outcome id"
          className="flex-1 border border-border rounded-md px-2.5 py-1.5 text-[11px]" />
        <button onClick={load} disabled={busy || !ref.trim()}
          className="flex items-center gap-1.5 text-[11px] px-3 py-1.5 rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50">
          {busy ? <Loader2 size={12} className="animate-spin" /> : <GitBranch size={12} />} Trace
        </button>
      </div>
      {err && <p className="mt-2 text-[11px] text-red-600">{err}</p>}
      {chain && (
        <div className="mt-2 space-y-1">
          {chain.chain.map(([stage, present]) => (
            <div key={stage} className="flex items-center gap-2 text-[11px] py-1">
              {present
                ? <CheckCircle2 size={12} className="text-emerald-600" />
                : <span className="w-3 h-3 rounded-full border border-border inline-block" />}
              <span className={present ? 'text-foreground' : 'text-muted-foreground/80'}>{STAGE_LABELS[stage] || stage}</span>
              {!present && <span className="text-[10px] text-muted-foreground/80">— no record yet (honest absence)</span>}
            </div>
          ))}
          {chain.stages.execution?.erp_submissions?.map((x) => (
            <p key={x.id} className="text-[10px] text-muted-foreground">
              ERP: {x.connector_id} · {x.status}{x.external_ref ? ` · ref ${x.external_ref}` : ''}
            </p>
          ))}
          {chain.stages.verification?.note && (
            <p className="text-[10px] text-emerald-700">verified: {chain.stages.verification.note} (by {chain.stages.verification.verified_by})</p>
          )}
        </div>
      )}
    </div>
  );
}

function OutcomeTracking() {
  const [drafts, setDrafts] = useState(null);
  const [escalations, setEscalations] = useState(null);
  const [outcomes, setOutcomes] = useState(null);
  const load = useCallback(async () => {
    try {
      const [d, e, o] = await Promise.all([
        authFetch('/api/ontology/po-drafts'),
        authFetch('/api/ontology/escalations'),
        authFetch('/api/ontology/outcomes'),
      ]);
      if (d.ok) setDrafts(await d.json());
      const ej = e.ok ? await e.json() : [];
      setEscalations(Array.isArray(ej) ? ej : ej.escalations || []);
      if (o.ok) setOutcomes((await o.json()).outcomes || []);
    } catch { toast.error('Failed to load outcome tracking'); }
  }, []);
  useEffect(() => { load(); }, [load]);
  const openEsc = (escalations || []).filter((x) => x.status !== 'resolved');
  const resolvedEsc = (escalations || []).filter((x) => x.status === 'resolved');
  const approvedDrafts = (drafts || []).filter((x) => x.status === 'approved');
  const measured = (outcomes || []).filter((x) => x.measurement_status === 'measured');
  const verified = (outcomes || []).filter((x) => x.verification_status === 'verified');
  const steps = [
    ['Problem detected', openEsc.length ? `${openEsc.length} open` : escalations ? 'none open' : '…'],
    ['Evidence collected', 'governed metrics + document evidence'],
    ['Recommendation generated', escalations?.length ? `${escalations.length} escalated` : 'none yet'],
    ['Decision reviewed', resolvedEsc.length ? `${resolvedEsc.length} resolved` : 'pending'],
    ['Action approved', approvedDrafts.length ? `${approvedDrafts.length} approval-gated write(s)` : 'none yet'],
    ['Action executed', approvedDrafts.length ? 'recorded — governed ERP submission available' : '—'],
    ['Impact measured', measured.length ? `${measured.length} governed measurement(s)` : 'pending'],
    ['Outcome verified', verified.length ? `${verified.length} verified` : 'pending'],
  ];
  return (
    <div className="space-y-4">
      <h2 className="text-sm font-semibold text-foreground flex items-center gap-2"><Activity size={15} className="text-foreground/80" /> Business Outcome Tracking</h2>
      <span className="text-[11px] text-muted-foreground/80">expected ≠ measured ≠ verified — realized impact is a governed delta, certified by an authorized reviewer</span>
      <div className="rounded-xl border border-border bg-card px-4 py-4">
        {steps.map(([label, state], i) => {
          const done = !['…', 'pending', 'none yet', '—', 'none open'].includes(state);
          return (
            <div key={i} className="flex items-center gap-3 py-2">
              <div className={`w-6 h-6 rounded-full flex items-center justify-center text-[10px] ${done ? 'bg-emerald-100 text-emerald-700' : 'bg-muted/60 text-muted-foreground/80'}`}>{i + 1}</div>
              <span className="text-[12px] text-foreground">{label}</span>
              <span className="ml-auto text-[10px] text-muted-foreground/80">{state}</span>
            </div>
          );
        })}
      </div>
      <div>
        <div className="text-[10px] uppercase tracking-wide text-muted-foreground/80 mb-2">Outcome measurements ({outcomes ? outcomes.length : '…'})</div>
        {outcomes && outcomes.length === 0 && (
          <div className="rounded-xl border border-border bg-card px-4 py-3 text-[11px] text-muted-foreground/80">
            No outcome measurements recorded yet — record one after a governed action executes.
          </div>
        )}
        {(outcomes || []).map((o) => <OutcomeCard key={o.id} o={o} onChanged={load} />)}
      </div>
      <LifecycleChain />
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
        <h2 className="text-sm font-semibold text-foreground flex items-center gap-2"><ShieldCheck size={15} className="text-foreground/80" /> Operational Health & Governance</h2>
        <span className="text-[11px] text-muted-foreground/80">technical administration, consolidated — business impact is shown in the experiences above</span>
        <div className="mt-3 grid grid-cols-2 sm:grid-cols-5 gap-3">
          {[['Domains', totals?.domains], ['Objects', totals?.objects], ['Objects live', totals?.objects_live], ['Actions', totals?.actions], ['Links', totals?.links]].map(([label, v]) => (
            <div key={label} className="rounded-xl border border-border bg-card px-4 py-3">
              <div className="text-lg font-semibold text-foreground">{v}</div>
              <div className="text-[10px] uppercase tracking-wide text-muted-foreground/80">{label}</div>
            </div>
          ))}
        </div>
      </div>
      <div>
        <h3 className="text-[13px] font-semibold text-foreground">Coverage Map</h3>
        <span className="text-[11px] text-muted-foreground/80">honest readiness — “live” only counts objects backed by real data + an executable action</span>
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
const DISCOVERY_STATUS_STYLE = {
  documented: 'bg-sky-50 text-sky-700 border-sky-200',
  configured: 'bg-violet-50 text-violet-700 border-violet-200',
  observed: 'bg-emerald-50 dark:bg-emerald-950/30 text-emerald-700 dark:text-emerald-300 border-emerald-200 dark:border-emerald-900/50',
};
const ACTIVITY_TYPE_LABEL = {
  start_event: 'Start', end_event: 'End', human_task: 'Human task',
  system_task: 'System task', decision: 'Decision', approval: 'Approval',
  subprocess: 'Subprocess',
};

function ProcessDetailDrawer({ processId, onClose, onChanged, isAdmin, onOpenFlow }) {
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [showReject, setShowReject] = useState(false);
  const [reason, setReason] = useState('');

  const load = useCallback(async () => {
    setError(null);
    try {
      const res = await authFetch(`/api/ontology/enterprise/processes/${processId}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setDetail(await res.json());
    } catch (e) { setError(e.message || String(e)); }
  }, [processId]);
  useEffect(() => { load(); }, [load]);

  const review = async (decision) => {
    setBusy(true);
    try {
      const res = await authFetch(
        `/api/ontology/enterprise/processes/${processId}/${decision}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: decision === 'reject' ? JSON.stringify({ reason: reason || null }) : '{}',
        },
      );
      if (!res.ok) {
        const b = await res.json().catch(() => ({}));
        throw new Error(b.detail || `HTTP ${res.status}`);
      }
      toast.success(decision === 'confirm' ? 'Process confirmed (verified)' : 'Process rejected');
      setShowReject(false);
      setReason('');
      await load();
      onChanged?.();
    } catch (e) { toast.error(`Review failed: ${e.message || e}`); }
    finally { setBusy(false); }
  };

  const d = detail;
  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/30" onClick={onClose}>
      <div className="w-full max-w-2xl h-full overflow-y-auto bg-card shadow-xl"
        onClick={(e) => e.stopPropagation()}>
        <div className="sticky top-0 bg-card/95 backdrop-blur border-b border-border px-6 py-4 flex items-start gap-3">
          <div className="min-w-0">
            <h2 className="text-[15px] font-semibold text-foreground truncate">
              {d ? d.name : 'Process'}
            </h2>
            {d && (
              <div className="mt-1.5 flex items-center gap-2 flex-wrap">
                {d.process_family && (
                  <span className="text-[10px] px-1.5 py-0.5 rounded-full border border-border bg-muted/40 text-muted-foreground">
                    family: {d.process_family}
                  </span>
                )}
                <span className={`text-[10px] px-1.5 py-0.5 rounded-full border capitalize ${DISCOVERY_STATUS_STYLE[d.discovery_status] || DISCOVERY_STATUS_STYLE.documented}`}>
                  {d.discovery_status}
                </span>
                <span className={`text-[10px] px-1.5 py-0.5 rounded-full border capitalize ${EVIDENCE_BADGE_STYLE[d.evidence_status] || EVIDENCE_BADGE_STYLE.proposed}`}>
                  {d.evidence_status}
                </span>
                {onOpenFlow && (
                  <button onClick={() => onOpenFlow(processId)}
                    className="text-[10px] px-1.5 py-0.5 rounded-full border border-border bg-card text-foreground/80 hover:bg-muted/60">
                    flow view
                  </button>
                )}
              </div>
            )}
          </div>
          <button onClick={onClose} className="ml-auto text-muted-foreground/80 hover:text-foreground text-lg leading-none px-1">×</button>
        </div>

        {error && (
          <div className="mx-6 mt-4 text-[12px] text-red-600 bg-red-50 dark:bg-red-950/30 border border-red-200 dark:border-red-900/50 rounded-md px-3 py-2">
            Failed to load process: {error}
          </div>
        )}
        {!d && !error && (
          <div className="flex items-center justify-center py-20 text-muted-foreground/80">
            <Loader2 size={16} className="animate-spin mr-2" /> Loading…
          </div>
        )}
        {d && (
          <div className="px-6 py-5 space-y-6">
            {/* Review actions — org-admin only (server enforces 403) */}
            {isAdmin && (
              <div className="rounded-lg border border-border bg-muted/40 p-4">
                <p className="text-[11px] text-muted-foreground mb-2.5">
                  Human review: confirm only if the evidence genuinely supports this process.
                </p>
                {!showReject ? (
                  <div className="flex gap-2">
                    <button onClick={() => review('confirm')} disabled={busy}
                      className="flex items-center gap-1.5 text-[12px] px-3 py-1.5 rounded-md bg-emerald-600 text-white hover:bg-emerald-700 disabled:opacity-50">
                      {busy ? <Loader2 size={12} className="animate-spin" /> : <CheckCircle2 size={12} />} Confirm
                    </button>
                    <button onClick={() => setShowReject(true)} disabled={busy}
                      className="flex items-center gap-1.5 text-[12px] px-3 py-1.5 rounded-md border border-red-200 text-red-600 bg-card hover:bg-red-50 dark:hover:bg-red-950/30 disabled:opacity-50">
                      <AlertTriangle size={12} /> Reject
                    </button>
                  </div>
                ) : (
                  <div className="space-y-2">
                    <textarea value={reason} onChange={(e) => setReason(e.target.value)}
                      placeholder="Reason for rejection (recorded in the review history)…"
                      rows={2}
                      className="w-full text-[12px] border border-border rounded-md px-2.5 py-2 focus:outline-none focus:ring-1 focus:ring-stone-400" />
                    <div className="flex gap-2">
                      <button onClick={() => review('reject')} disabled={busy}
                        className="text-[12px] px-3 py-1.5 rounded-md bg-red-600 text-white hover:bg-red-700 disabled:opacity-50">
                        {busy ? 'Rejecting…' : 'Reject process'}
                      </button>
                      <button onClick={() => { setShowReject(false); setReason(''); }}
                        className="text-[12px] px-3 py-1.5 rounded-md border border-border text-foreground/80 hover:bg-muted/60">
                        Cancel
                      </button>
                    </div>
                  </div>
                )}
              </div>
            )}

            {/* Overview */}
            <section>
              <h3 className="text-[11px] font-medium text-muted-foreground/80 uppercase tracking-wide mb-2">Overview</h3>
              <dl className="grid grid-cols-1 gap-2 text-[12px]">
                {[['Purpose', d.purpose], ['Trigger event', d.trigger_event],
                  ['Objective', d.objective], ['Start condition', d.start_condition],
                  ['Completion condition', d.completion_condition]].map(([k, v]) => (
                  v ? (
                    <div key={k} className="flex gap-2">
                      <dt className="w-36 shrink-0 text-muted-foreground/80">{k}</dt>
                      <dd className="text-foreground">{v}</dd>
                    </div>
                  ) : null
                ))}
                {!d.purpose && !d.trigger_event && !d.objective && (
                  <p className="text-muted-foreground/80">No overview fields were discovered from evidence.</p>
                )}
              </dl>
            </section>

            {/* Activity sequence */}
            <section>
              <h3 className="text-[11px] font-medium text-muted-foreground/80 uppercase tracking-wide mb-2">
                Activity sequence ({d.activities.length})
              </h3>
              {d.activities.length === 0 ? (
                <p className="text-[12px] text-muted-foreground/80">No activities discovered yet.</p>
              ) : (
                <ol className="space-y-1.5">
                  {d.activities.map((a, i) => (
                    <li key={`${a.seq}-${a.name}`} className="flex items-start gap-3 rounded-lg border border-border px-3 py-2">
                      <span className="mt-0.5 w-6 h-6 shrink-0 rounded-full bg-muted/60 text-foreground/80 text-[11px] flex items-center justify-center">{a.seq}</span>
                      <div className="min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="text-[12px] font-medium text-foreground">{a.name}</span>
                          <span className="text-[10px] px-1.5 py-0.5 rounded-full border border-border bg-muted/40 text-muted-foreground">
                            {ACTIVITY_TYPE_LABEL[a.activity_type] || a.activity_type}
                          </span>
                          {a.performer && (
                            <span className="text-[10px] text-muted-foreground/80">by {a.performer}</span>
                          )}
                        </div>
                        {a.business_rules && <p className="mt-0.5 text-[11px] text-muted-foreground">Rule: {a.business_rules}</p>}
                        {a.exception_paths && <p className="text-[11px] text-amber-700">Exception: {a.exception_paths}</p>}
                      </div>
                    </li>
                  ))}
                </ol>
              )}
            </section>

            {/* Evidence / provenance */}
            <section>
              <h3 className="text-[11px] font-medium text-muted-foreground/80 uppercase tracking-wide mb-2">
                Evidence ({d.evidence.length})
              </h3>
              {d.evidence.length === 0 ? (
                <p className="text-[12px] text-muted-foreground/80">No evidence rows recorded.</p>
              ) : (
                <ul className="space-y-1.5">
                  {d.evidence.map((e) => (
                    <li key={e.id} className="rounded-lg border border-border px-3 py-2">
                      <div className="flex items-center gap-2">
                        <span className="text-[10px] px-1.5 py-0.5 rounded-full border border-border bg-muted/40 text-muted-foreground">{e.evidence_kind}</span>
                        <span className="text-[10px] text-muted-foreground/80 truncate">{e.source_ref}</span>
                        {e.evidence_status && (
                          <span className={`ml-auto text-[10px] px-1.5 py-0.5 rounded-full border capitalize ${
                            e.evidence_status === 'verified'
                              ? 'border-emerald-200 dark:border-emerald-900/50 bg-emerald-50 dark:bg-emerald-950/30 text-emerald-700'
                              : e.evidence_status === 'rejected'
                                ? 'border-red-200 dark:border-red-900/50 bg-red-50 dark:bg-red-950/30 text-red-700'
                                : 'border-border bg-muted/40 text-muted-foreground'}`}>
                            {e.evidence_status}
                          </span>
                        )}
                      </div>
                      {e.quote && <p className="mt-1 text-[11px] text-foreground/80 italic line-clamp-3">“{e.quote}”</p>}
                    </li>
                  ))}
                </ul>
              )}
            </section>

            {/* Related processes + ontology connections */}
            <section className="grid grid-cols-1 gap-4">
              <div>
                <h3 className="text-[11px] font-medium text-muted-foreground/80 uppercase tracking-wide mb-2">Related processes</h3>
                {d.related_processes.length === 0 ? (
                  <p className="text-[12px] text-muted-foreground/80">No discovered handoffs yet.</p>
                ) : d.related_processes.map((r, i) => (
                  <p key={i} className="text-[12px] text-foreground">
                    <span className="text-muted-foreground/80">{r.direction}:</span> {r.peer_name || r.peer_id}
                    {r.handoff_note && <span className="text-muted-foreground/80"> — {r.handoff_note}</span>}
                  </p>
                ))}
              </div>
              <div>
                <h3 className="text-[11px] font-medium text-muted-foreground/80 uppercase tracking-wide mb-2">Ontology connections</h3>
                {d.ontology_connections.length === 0 ? (
                  <p className="text-[12px] text-muted-foreground/80">None referenced by evidence.</p>
                ) : d.ontology_connections.map((c, i) => (
                  <p key={i} className="text-[12px] text-foreground">
                    <span className="text-muted-foreground/80">{c.kind}:</span> {c.name || c.link_type || c.id}
                  </p>
                ))}
              </div>
            </section>

            {/* Review history */}
            <section>
              <h3 className="text-[11px] font-medium text-muted-foreground/80 uppercase tracking-wide mb-2">
                Review history ({d.review_history.length})
              </h3>
              {d.review_history.length === 0 ? (
                <p className="text-[12px] text-muted-foreground/80">Awaiting first human review.</p>
              ) : (
                <ul className="space-y-1.5">
                  {d.review_history.map((h, i) => (
                    <li key={i} className="text-[12px] flex items-start gap-2">
                      <span className={`text-[10px] px-1.5 py-0.5 rounded-full border capitalize ${
                        h.decision === 'confirmed' ? 'border-emerald-200 dark:border-emerald-900/50 bg-emerald-50 dark:bg-emerald-950/30 text-emerald-700'
                          : 'border-red-200 dark:border-red-900/50 bg-red-50 dark:bg-red-950/30 text-red-700'}`}>
                        {h.decision}
                      </span>
                      <span className="text-foreground/80">
                        {h.reviewer_name || h.reviewer_id || 'unknown'}
                        {h.reason && <span className="text-muted-foreground/80"> — {h.reason}</span>}
                        <span className="text-muted-foreground/80 ml-1">{h.at ? new Date(h.at).toLocaleString() : ''}</span>
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </div>
        )}
      </div>
    </div>
  );
}

function BusinessProcesses({ onOpenFlow }) {
  const { isAdmin } = useAuth();
  const [data, setData] = useState(null);
  const [q, setQ] = useState('');
  const [family, setFamily] = useState('');
  const [status, setStatus] = useState('');
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState(null);

  const load = useCallback(async () => {
    try {
      const params = new URLSearchParams({ page: String(page), page_size: '20' });
      if (q) params.set('q', q);
      if (family) params.set('family', family);
      if (status) params.set('status', status);
      const res = await authFetch(`/api/ontology/enterprise/processes?${params}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setData(await res.json());
    } catch {
      setData({ total: 0, page: 1, page_size: 20, families: [], items: [], error: true });
    }
  }, [q, family, status, page]);
  useEffect(() => { load(); }, [load]);

  const grouped = useMemo(() => {
    const map = new Map();
    for (const it of data?.items || []) {
      const key = it.process_family || 'Unassigned';
      (map.get(key) || map.set(key, []).get(key)).push(it);
    }
    return [...map.entries()];
  }, [data]);

  const totalPages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1;

  return (
    <div>
      <div className="flex items-center gap-2 flex-wrap">
        <div className="relative">
          <Search size={13} className="absolute left-2.5 top-2.5 text-muted-foreground/80" />
          <input value={q} onChange={(e) => { setQ(e.target.value); setPage(1); }}
            placeholder="Search processes…"
            className="text-[12px] pl-8 pr-3 py-2 w-56 border border-border rounded-md bg-card focus:outline-none focus:ring-1 focus:ring-stone-400" />
        </div>
        <select value={family} onChange={(e) => { setFamily(e.target.value); setPage(1); }}
          className="text-[12px] px-2.5 py-2 border border-border rounded-md bg-card text-foreground/80">
          <option value="">All families</option>
          {(data?.families || []).filter((f) => f.family).map((f) => (
            <option key={f.family} value={f.family}>{f.family} ({f.count})</option>
          ))}
        </select>
        <select value={status} onChange={(e) => { setStatus(e.target.value); setPage(1); }}
          className="text-[12px] px-2.5 py-2 border border-border rounded-md bg-card text-foreground/80 capitalize">
          <option value="">All statuses</option>
          <option value="proposed">Needs review</option>
          <option value="verified">Verified</option>
          <option value="rejected">Rejected</option>
        </select>
        <span className="ml-auto text-[11px] text-muted-foreground/80">
          {data ? `${data.total} discovered process${data.total === 1 ? '' : 'es'}` : ''}
        </span>
      </div>

      {!data ? (
        <div className="flex items-center justify-center py-20 text-muted-foreground/80">
          <Loader2 size={16} className="animate-spin mr-2" /> Loading processes…
        </div>
      ) : data.error ? (
        <div className="mt-6 flex items-center gap-2 text-[12px] text-amber-700 bg-amber-50 dark:bg-amber-950/30 border border-amber-200 dark:border-amber-900/50 rounded-md px-3 py-2">
          <AlertTriangle size={13} /> Failed to load the process catalogue — run discovery first.
        </div>
      ) : data.total === 0 ? (
        <div className="mt-6 rounded-xl border border-dashed border-border bg-card px-6 py-10 text-center">
          <Workflow size={20} className="mx-auto text-muted-foreground/60" />
          <p className="mt-2 text-[13px] text-muted-foreground">No business processes discovered yet.</p>
          <p className="mt-1 text-[11px] text-muted-foreground/80">
            Run AI discovery from authorized resources — proposed processes appear here for human review.
          </p>
        </div>
      ) : (
        <div className="mt-5 space-y-6">
          {grouped.map(([fam, items]) => (
            <div key={fam}>
              <h3 className="text-[11px] font-medium text-muted-foreground/80 uppercase tracking-wide mb-2">{fam}</h3>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                {items.map((p) => (
                  <button key={p.id} onClick={() => setSelected(p.id)}
                    className="text-left rounded-xl border border-border bg-card px-4 py-3.5 hover:border-border transition-colors">
                    <div className="flex items-center gap-2">
                      <span className="text-[13px] font-medium text-foreground truncate">{p.name}</span>
                      <span className={`ml-auto text-[10px] px-1.5 py-0.5 rounded-full border capitalize ${DISCOVERY_STATUS_STYLE[p.discovery_status] || DISCOVERY_STATUS_STYLE.documented}`}>
                        {p.discovery_status}
                      </span>
                    </div>
                    {p.purpose && <p className="mt-1 text-[11px] text-muted-foreground/80 line-clamp-1">{p.purpose}</p>}
                    <div className="mt-2 flex items-center gap-3 flex-wrap text-[10px] text-muted-foreground">
                      <span className={`px-1.5 py-0.5 rounded-full border capitalize ${EVIDENCE_BADGE_STYLE[p.evidence_status] || EVIDENCE_BADGE_STYLE.proposed}`}>
                        {p.evidence_status}
                      </span>
                      {p.needs_review && (
                        <span className="px-1.5 py-0.5 rounded-full border border-amber-200 dark:border-amber-900/50 bg-amber-50 dark:bg-amber-950/30 text-amber-700">needs review</span>
                      )}
                      <span>{p.evidence_count} evidence</span>
                      <span>{p.activities_count} activities</span>
                      {p.responsible_units.length > 0 && (
                        <span className="truncate max-w-[180px]">{p.responsible_units.join(', ')}</span>
                      )}
                    </div>
                  </button>
                ))}
              </div>
            </div>
          ))}
          {totalPages > 1 && (
            <div className="flex items-center justify-center gap-3 text-[12px] text-muted-foreground">
              <button disabled={page <= 1} onClick={() => setPage((p) => p - 1)}
                className="px-2.5 py-1 rounded-md border border-border bg-card disabled:opacity-40 hover:bg-muted/60">Prev</button>
              <span>page {data.page} / {totalPages}</span>
              <button disabled={page >= totalPages} onClick={() => setPage((p) => p + 1)}
                className="px-2.5 py-1 rounded-md border border-border bg-card disabled:opacity-40 hover:bg-muted/60">Next</button>
            </div>
          )}
        </div>
      )}

      {selected && (
        <ProcessDetailDrawer processId={selected} isAdmin={isAdmin}
          onClose={() => setSelected(null)} onChanged={load}
          onOpenFlow={onOpenFlow} />
      )}
    </div>
  );
}

/* Phase 6F: tab icons — labels/groups come from enterpriseUrlState (the
   six conceptual areas are navigation groups over the SAME components). */
const TAB_ICONS = {
  M: Boxes, A: Gauge, B: FileSearch, C: Network, D: Workflow, E: Activity,
  F: GitBranch, G: ShieldCheck, R: ClipboardCheck, K: ScrollText,
  W: Bot, S: Database,
};
const ADMIN_ONLY_TABS = ['R'];

export default function EnterpriseProfile() {
  const { isAdmin } = useAuth();
  const [overview, setOverview] = useState(null);
  const [objects, setObjects] = useState([]);
  const [value, setValue] = useState([]);
  const [selectedDomain, setSelectedDomain] = useState(null);
  const [discovering, setDiscovering] = useState(false);
  const [flowProcessId, setFlowProcessId] = useState(null);
  const [detailProcessId, setDetailProcessId] = useState(null);

  // Phase 6F: shareable deep-links — the whole view (tab + object/resource/
  // rule refs) lives in the URL; refresh preserves it, invalid or
  // non-permitted refs fall back honestly (parse never renders a view the
  // user may not access).
  const [searchParams, setSearchParams] = useSearchParams();
  const view = useMemo(
    () => parseEnterpriseView(searchParams, {
      adminKeys: isAdmin ? ADMIN_ONLY_TABS : [],
    }),
    [searchParams, isAdmin]);
  const setView = useCallback((patch) => {
    setSearchParams(
      buildEnterpriseSearch({ ...view, ...patch }),
      { replace: false });
  }, [view, setSearchParams]);
  const setExp = useCallback((exp) => setView({ exp }), [setView]);
  const setObjectId = useCallback((objectId) => setView({ objectId }), [setView]);
  const setResourceId = useCallback((resourceId) => setView({ resourceId }), [setView]);
  const openRule = useCallback((ruleId) => setView({ exp: 'K', ruleId }), [setView]);

  const openProcess = useCallback((processId, asFlow) => {
    if (asFlow) setFlowProcessId(processId);
    else setDetailProcessId(processId);
  }, []);

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
      <div className="flex items-center justify-center py-24 text-muted-foreground/80">
        <Loader2 size={18} className="animate-spin mr-2" /> Loading enterprise profile…
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto bg-muted/40">
      <div className="max-w-6xl mx-auto px-6 py-8">
        <div className="flex items-center gap-3">
          <Building2 size={22} className="text-foreground" />
          <h1 className="text-lg font-semibold text-foreground">Enterprise Business Operating Model</h1>
          <span className="text-[11px] text-muted-foreground/80">governed intelligence → business decisions → measured outcomes</span>
          <button onClick={runDiscovery} disabled={discovering}
            className="ml-auto flex items-center gap-1.5 text-[12px] px-3 py-1.5 rounded-md border border-border bg-card text-foreground hover:bg-muted/60 disabled:opacity-50">
            {discovering ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />}
            {discovering ? 'Discovering…' : 'Run auto-discovery'}
          </button>
        </div>

        {/* Phase 6F: six human-facing navigation areas over the SAME tabs —
            no duplicate views; admin-only tabs are filtered per user. */}
        <div className="mt-5 flex items-center gap-1 border-b border-border flex-wrap">
          {ENTERPRISE_AREAS.map((area) => {
            const tabs = area.tabs.filter((k) => !ADMIN_ONLY_TABS.includes(k) || isAdmin);
            if (tabs.length === 0) return null;
            return (
              <div key={area.key} className="flex items-center">
                <span className="text-[9px] uppercase tracking-wide text-muted-foreground/60 px-1.5 whitespace-nowrap"
                  title={`Area: ${area.label}`}>{area.label}</span>
                {tabs.map((key) => {
                  const Icon = TAB_ICONS[key];
                  const active = view.exp === key;
                  return (
                    <button key={key} onClick={() => setExp(key)}
                      title={ENTERPRISE_TABS[key]}
                      className={`flex items-center gap-1.5 px-3 py-2 text-[12px] border-b-2 -mb-px ${
                        active ? 'border-border text-foreground font-medium' : 'border-transparent text-muted-foreground hover:text-foreground'}`}>
                      <Icon size={13} /> {ENTERPRISE_TABS[key]}
                    </button>
                  );
                })}
                <span className="w-2" />
              </div>
            );
          })}
        </div>

        <div className="mt-6">
          {view.exp === 'M' && <EnterpriseMap onOpenProcess={openProcess}
            onNavigate={setExp} canReview={isAdmin} onOpenRule={openRule} />}
          {view.exp === 'A' && <ExecutiveOverview />}
          {view.exp === 'B' && <SituationRoom />}
          {view.exp === 'C' && <BusinessObjectExplorer />}
          {view.exp === 'D' && <DecisionCenter />}
          {view.exp === 'E' && <OutcomeTracking />}
          {view.exp === 'F' && <BusinessProcesses onOpenFlow={setFlowProcessId} />}
          {view.exp === 'G' && (
            <OperationalHealth overview={overview} value={value}
              selectedDomain={selectedDomain} setSelectedDomain={setSelectedDomain} />
          )}
          {view.exp === 'K' && <BusinessRulesBrowser isAdmin={isAdmin}
            ruleId={view.ruleId} onOpenRule={openRule}
            onClose={() => setView({ ruleId: null })} />}
          {view.exp === 'S' && <ResourceDirectory onOpenResource={setResourceId} />}
          {view.exp === 'W' && <WorkforcePanel />}
          {view.exp === 'R' && isAdmin && <ReviewCenter onOpenProcess={openProcess} />}
        </div>
      </div>

      {flowProcessId && (
        <ProcessFlowCanvas processId={flowProcessId}
          onClose={() => setFlowProcessId(null)}
          onOpenObject={setObjectId}
          onOpenDetail={(pid, asFlow) => {
            if (asFlow) setFlowProcessId(pid);
            else setDetailProcessId(pid);
          }} />
      )}
      {detailProcessId && !flowProcessId && (
        <ProcessDetailDrawer processId={detailProcessId} isAdmin={isAdmin}
          onClose={() => setDetailProcessId(null)}
          onOpenFlow={setFlowProcessId} />
      )}
      {view.objectId && (
        <BusinessObjectPanel objectId={view.objectId}
          crumbs={[{ label: 'Enterprise' }, { label: 'Business object' }]}
          onClose={() => setObjectId(null)}
          onOpenResource={setResourceId}
          onOpenProcess={(pid) => { setObjectId(null); openProcess(pid, true); }}
          onOpenObject={setObjectId}
          onOpenRule={openRule} />
      )}
      {view.resourceId && (
        <ResourcePanel refId={view.resourceId}
          crumbs={[{ label: 'Enterprise' }, { label: 'Resource' }]}
          onClose={() => setResourceId(null)}
          onOpenObject={setObjectId} />
      )}
    </div>
  );
}
