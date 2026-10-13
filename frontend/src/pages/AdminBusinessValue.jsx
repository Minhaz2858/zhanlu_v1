import { useState, useEffect, useCallback } from 'react';
import {
  TrendingUp, MessageSquare, FileText, DollarSign, Clock,
  Percent, Layers, RefreshCw, Loader2, ExternalLink, Package, Check,
} from 'lucide-react';
import { toast } from 'sonner';
import { authFetch } from '@/api/authFetch';

/**
 * Domain packs — packaged vertical value (IBM Gap 5). Installing merges
 * the industry's vocabulary into the org glossary + adds starter
 * questions. Idempotent server-side.
 */
function DomainPacksCard() {
  const [packs, setPacks] = useState(null);
  const [installing, setInstalling] = useState(null);

  const load = useCallback(async () => {
    try {
      const res = await authFetch('/api/domain-packs');
      if (res.ok) setPacks((await res.json()).packs);
    } catch {
      /* fail-quiet — the section hides */
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const install = async (key) => {
    setInstalling(key);
    try {
      const res = await authFetch(`/api/domain-packs/${key}/install`, { method: 'POST' });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      const out = await res.json();
      toast.success(`${out.name} installed — ${out.glossary_terms_added} glossary terms added`);
      load();
    } catch (e) {
      toast.error(`Install failed: ${e.message || e}`);
    } finally {
      setInstalling(null);
    }
  };

  if (!packs || packs.length === 0) return null;

  return (
    <div className="mt-8">
      <div className="flex items-center gap-2">
        <Package size={15} className="text-amber-600" />
        <h2 className="text-sm font-semibold text-stone-800">Industry Packs</h2>
        <span className="text-[11px] text-stone-400">
          one-click vertical vocabulary + starter questions for your workspace
        </span>
      </div>
      <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 gap-3">
        {packs.map((p) => (
          <div
            key={p.pack_key}
            className="rounded-xl border border-stone-200 bg-white px-4 py-3.5 flex flex-col gap-2"
          >
            <div className="flex items-center gap-2">
              <span className="text-[13px] font-medium text-stone-800">{p.name}</span>
              <span className="text-[10px] px-1.5 py-0.5 rounded-full border border-stone-200 text-stone-500 capitalize">
                {p.industry}
              </span>
              {p.installed && (
                <span className="ml-auto flex items-center gap-1 text-[10px] text-emerald-600">
                  <Check size={11} /> installed
                </span>
              )}
            </div>
            <p className="text-[11px] text-stone-500 leading-relaxed">{p.description}</p>
            {p.starter_prompts?.[0] && (
              <p className="text-[11px] text-stone-400 truncate">e.g. “{p.starter_prompts[0]}”</p>
            )}
            <div className="flex items-center gap-2 mt-auto pt-1">
              <button
                onClick={() => install(p.pack_key)}
                disabled={p.installed || installing === p.pack_key}
                className={`text-[11px] px-2.5 py-1 rounded-md border transition-colors ${
                  p.installed
                    ? 'border-emerald-200 bg-emerald-50 text-emerald-700 cursor-default'
                    : 'border-amber-300 bg-amber-50 text-amber-700 hover:bg-amber-100 disabled:opacity-50'
                }`}
              >
                {installing === p.pack_key
                  ? 'Installing…'
                  : p.installed
                    ? 'Installed — re-merge terms'
                    : 'Install pack'}
              </button>
              <span className="text-[10px] text-stone-400">{p.terminology_count} glossary terms</span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

/**
 * Business Value — the AI-CoE board (admin-only).
 *
 * One page that makes the value layer visible: ROI summary cards
 * (measured + clearly-labeled estimates), the use-case portfolio
 * (ownership / KPI / lifecycle stage), and per-agent acceptance rates.
 * Stage changes PUT /api/portfolio (Org-Admin only — the backend rejects
 * other roles).
 */

const STAGES = ['idea', 'pilot', 'production', 'retired'];
const STAGE_STYLE = {
  idea: 'bg-stone-100 text-stone-600 border-stone-200',
  pilot: 'bg-sky-50 text-sky-700 border-sky-200',
  production: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  retired: 'bg-stone-50 text-stone-400 border-stone-200',
};

function Card({ icon: Icon, label, value, sub }) {
  return (
    <div className="rounded-xl border border-stone-200 bg-gradient-to-b from-stone-50/80 to-white px-4 py-3">
      <div className="flex items-center gap-1.5 text-[11px] text-stone-500">
        <Icon size={12} className="text-amber-600 shrink-0" />
        <span className="truncate">{label}</span>
      </div>
      <div className="text-2xl font-semibold text-stone-800 tabular-nums leading-tight mt-1">
        {value}
      </div>
      {sub && <div className="text-[10px] text-stone-400 mt-0.5">{sub}</div>}
    </div>
  );
}

export default function AdminBusinessValue() {
  const [days, setDays] = useState(30);
  const [roi, setRoi] = useState(null);
  const [portfolio, setPortfolio] = useState(null);
  const [loading, setLoading] = useState(true);
  const [stageFilter, setStageFilter] = useState('all');
  const [savingAgent, setSavingAgent] = useState(null);

  const load = useCallback(async (w) => {
    setLoading(true);
    try {
      const [roiRes, pfRes] = await Promise.all([
        authFetch(`/api/roi/summary?days=${w}`),
        authFetch(`/api/portfolio?days=${w}`),
      ]);
      if (!roiRes.ok || !pfRes.ok) throw new Error('failed to load value data');
      setRoi(await roiRes.json());
      setPortfolio(await pfRes.json());
    } catch (e) {
      toast.error(`Failed to load: ${e.message || e}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(days); }, [days, load]);

  const changeStage = async (agentId, stage, item) => {
    setSavingAgent(agentId);
    try {
      const res = await authFetch('/api/portfolio', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          agent_id: agentId,
          agent_name: item.agent_name,
          lifecycle_stage: stage,
        }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      setPortfolio((p) => ({
        ...p,
        items: p.items.map((i) => (i.agent_id === agentId ? { ...i, lifecycle_stage: stage } : i)),
        summary: {
          ...p.summary,
          [item.lifecycle_stage]: Math.max(0, (p.summary?.[item.lifecycle_stage] || 0) - 1),
          [stage]: (p.summary?.[stage] || 0) + 1,
        },
      }));
      toast.success(`Stage → ${stage}`);
    } catch (e) {
      toast.error(`Stage change failed: ${e.message || e}`);
    } finally {
      setSavingAgent(null);
    }
  };

  const t = roi?.totals || {};
  const items = (portfolio?.items || [])
    .filter((i) => stageFilter === 'all' || i.lifecycle_stage === stageFilter);

  return (
    <div className="min-h-screen bg-stone-50">
      <div className="mx-auto max-w-6xl px-6 py-8">
        {/* Header */}
        <div className="flex items-center gap-3 flex-wrap">
          <TrendingUp size={20} className="text-amber-600" />
          <h1 className="text-lg font-semibold text-stone-800">Business Value</h1>
          <span className="text-xs text-stone-400">
            the AI-CoE board — measured value, cost, and ownership per use case
          </span>
          <div className="ml-auto flex items-center gap-2">
            {[7, 30, 90].map((w) => (
              <button
                key={w}
                onClick={() => setDays(w)}
                className={`px-2.5 py-1 text-xs rounded-md border transition-colors ${
                  days === w
                    ? 'border-amber-400 bg-amber-50 text-amber-700'
                    : 'border-stone-200 text-stone-500 hover:bg-stone-100'
                }`}
              >
                {w}d
              </button>
            ))}
            <button
              onClick={() => load(days)}
              title="Refresh"
              className="p-1.5 rounded-md border border-stone-200 text-stone-500 hover:bg-stone-100"
            >
              <RefreshCw size={13} />
            </button>
          </div>
        </div>

        {loading ? (
          <div className="mt-10 flex items-center gap-2 text-sm text-stone-400">
            <Loader2 size={15} className="animate-spin" /> Loading value data…
          </div>
        ) : (
          <>
            {/* ROI summary cards */}
            <div className="mt-5 grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-7 gap-2.5">
              <Card icon={MessageSquare} label="Conversations" value={Number(t.conversations || 0).toLocaleString()} />
              <Card icon={MessageSquare} label="User turns" value={Number(t.user_turns || 0).toLocaleString()} />
              <Card icon={FileText} label="Answers" value={Number(t.assistant_answers || 0).toLocaleString()} />
              <Card icon={Layers} label="Artifacts" value={Number(t.artifacts_created || 0).toLocaleString()} />
              <Card
                icon={DollarSign}
                label="Cost"
                value={`$${Number(t.cost_total || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 4 })}`}
                sub={t.cost_per_answered_turn != null ? `$${t.cost_per_answered_turn}/answer` : undefined}
              />
              <Card
                icon={Clock}
                label="Est. hours saved"
                value={Number(t.est_hours_saved || 0).toLocaleString()}
                sub="estimate — see assumptions"
              />
              <Card
                icon={Percent}
                label="Acceptance"
                value={t.acceptance_rate != null ? `${Math.round(t.acceptance_rate * 100)}%` : '—'}
                sub={t.est_value_multiple != null ? `${t.est_value_multiple}× value/cost` : undefined}
              />
            </div>

            {/* Estimate honesty note */}
            <div className="mt-3 rounded-lg border border-amber-200 bg-amber-50/60 px-4 py-2 text-[11px] text-amber-800">
              {t.est_assumptions?.note || 'est_hours_saved is an estimate based on stated assumptions.'}
              {' '}Assumptions: {t.est_assumptions?.minutes_per_turn} min/turn ·{' '}
              {t.est_assumptions?.minutes_per_artifact} min/artifact · ${t.est_assumptions?.hourly_rate}/hr —
              adjust via ROI_* settings.
            </div>

            {/* Stage filter chips */}
            <div className="mt-6 flex items-center gap-2 flex-wrap">
              <button
                onClick={() => setStageFilter('all')}
                className={`px-2.5 py-1 text-xs rounded-full border transition-colors ${
                  stageFilter === 'all' ? 'border-stone-800 bg-stone-800 text-white' : 'border-stone-200 text-stone-500 hover:bg-stone-100'
                }`}
              >
                All ({portfolio?.items?.length || 0})
              </button>
              {STAGES.map((s) => (
                <button
                  key={s}
                  onClick={() => setStageFilter(s)}
                  className={`px-2.5 py-1 text-xs rounded-full border capitalize transition-colors ${
                    stageFilter === s ? 'border-stone-800 bg-stone-800 text-white' : STAGE_STYLE[s]
                  }`}
                >
                  {s} ({portfolio?.summary?.[s] || 0})
                </button>
              ))}
            </div>

            {/* Portfolio board */}
            <div className="mt-3 rounded-xl border border-stone-200 bg-white overflow-x-auto">
              <table className="w-full text-left">
                <thead>
                  <tr className="text-[11px] text-stone-400 border-b border-stone-200">
                    <th className="px-4 py-2.5 font-medium">Use case</th>
                    <th className="px-3 py-2.5 font-medium">Stage</th>
                    <th className="px-3 py-2.5 font-medium">Owner</th>
                    <th className="px-3 py-2.5 font-medium">KPI</th>
                    <th className="px-3 py-2.5 font-medium text-right">Turns</th>
                    <th className="px-3 py-2.5 font-medium text-right">Artifacts</th>
                    <th className="px-3 py-2.5 font-medium text-right">Cost</th>
                    <th className="px-3 py-2.5 font-medium text-right">Est. hrs</th>
                    <th className="px-3 py-2.5 font-medium text-right">Accepted</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((i) => (
                    <tr key={i.agent_id} className="border-b border-stone-100 hover:bg-stone-50/60">
                      <td className="px-4 py-2.5 text-[13px] font-medium text-stone-800">
                        {i.agent_name}
                        {i.catalog_status === 'removed' && (
                          <span className="ml-2 text-[10px] text-stone-400">(removed from catalog)</span>
                        )}
                      </td>
                      <td className="px-3 py-2.5">
                        <select
                          value={i.lifecycle_stage}
                          disabled={savingAgent === i.agent_id}
                          onChange={(e) => changeStage(i.agent_id, e.target.value, i)}
                          className={`text-[11px] px-1.5 py-1 rounded border capitalize ${STAGE_STYLE[i.lifecycle_stage]}`}
                        >
                          {STAGES.map((s) => (
                            <option key={s} value={s}>{s}</option>
                          ))}
                        </select>
                      </td>
                      <td className="px-3 py-2.5 text-[12px] text-stone-600">{i.business_owner || '—'}</td>
                      <td className="px-3 py-2.5 text-[12px] text-stone-600 max-w-[200px] truncate" title={i.business_kpi || ''}>
                        {i.business_kpi || '—'}
                      </td>
                      <td className="px-3 py-2.5 text-[12px] text-stone-700 text-right tabular-nums">
                        {Number(i.roi.user_turns || 0).toLocaleString()}
                      </td>
                      <td className="px-3 py-2.5 text-[12px] text-stone-700 text-right tabular-nums">
                        {Number(i.roi.artifacts_created || 0).toLocaleString()}
                      </td>
                      <td className="px-3 py-2.5 text-[12px] text-stone-700 text-right tabular-nums">
                        ${Number(i.roi.cost_total || 0).toFixed(4)}
                      </td>
                      <td className="px-3 py-2.5 text-[12px] text-stone-700 text-right tabular-nums">
                        {Number(i.roi.est_hours_saved || 0).toFixed(2)}
                      </td>
                      <td className="px-3 py-2.5 text-[12px] text-right tabular-nums">
                        {i.roi.acceptance_rate != null
                          ? `${Math.round(i.roi.acceptance_rate * 100)}%`
                          : <span className="text-stone-300">—</span>}
                      </td>
                    </tr>
                  ))}
                  {items.length === 0 && (
                    <tr>
                      <td colSpan={9} className="px-4 py-8 text-center text-xs text-stone-400">
                        No use cases in this stage.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>

            <p className="mt-3 text-[11px] text-stone-400 flex items-center gap-1">
              <ExternalLink size={10} />
              Data: /api/roi/summary + /api/portfolio · costs from cost_ledger · acceptance from answer thumbs
            </p>

            <DomainPacksCard />
          </>
        )}
      </div>
    </div>
  );
}
