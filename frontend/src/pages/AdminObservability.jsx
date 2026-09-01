import { useState, useEffect, useCallback } from 'react';
import { useAuth } from '@/lib/AuthContext';
import PageHeader from '@/components/PageHeader';
import { Loader2, Shield, Activity, MessageSquare, GitBranch, X } from 'lucide-react';
import { listInvocations, getInvocationSummary, getInvocationDetail } from '@/api/agentInvocations';

function fmtDuration(ms) {
  if (ms === null || ms === undefined) return '—';
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function fmtCost(c) {
  if (c === null || c === undefined) return '—';
  return `$${Number(c).toFixed(4)}`;
}

function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function StatusBadge({ status }) {
  const color = status === 'completed' ? 'bg-emerald-500/15 text-emerald-600'
    : status === 'failed' ? 'bg-red-500/15 text-red-600'
    : 'bg-amber-500/15 text-amber-600';
  return <span className={`inline-flex rounded-full px-2 py-0.5 text-xs font-medium ${color}`}>{status}</span>;
}

function StatCard({ label, value, hint }) {
  return (
    <div className="rounded-xl border border-border bg-card p-4">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="mt-1 text-2xl font-semibold tracking-tight">{value}</p>
      {hint ? <p className="mt-0.5 text-[11px] text-muted-foreground">{hint}</p> : null}
    </div>
  );
}

export default function AdminObservability() {
  const { isAdmin } = useAuth();
  const [rows, setRows] = useState([]);
  const [summary, setSummary] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [filters, setFilters] = useState({ status: '', invocation_type: '', agent_app_id: '' });
  const [selected, setSelected] = useState(null); // detail row
  const [detailLoading, setDetailLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params = {};
      if (filters.status) params.status = filters.status;
      if (filters.invocation_type) params.invocation_type = filters.invocation_type;
      if (filters.agent_app_id) params.agent_app_id = filters.agent_app_id;
      const [listData, summaryData] = await Promise.all([
        listInvocations({ ...params, limit: 100 }),
        getInvocationSummary(params),
      ]);
      setRows(listData.items || []);
      setSummary(summaryData);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [filters]);

  useEffect(() => { load(); }, [load]);

  async function openDetail(row) {
    setDetailLoading(true);
    try {
      setSelected(await getInvocationDetail(row.id));
    } catch (e) {
      setError(e.message);
    } finally {
      setDetailLoading(false);
    }
  }

  if (!isAdmin) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="text-center">
          <Shield className="mx-auto mb-3 h-10 w-10 text-muted-foreground/40" />
          <p className="text-sm text-muted-foreground">Admin access required.</p>
        </div>
      </div>
    );
  }

  const t = summary?.totals || {};
  const agents = Object.keys(summary?.by_agent || {});

  return (
    <div className="h-full overflow-y-auto px-8 py-8">
      <PageHeader title="Agent Observability" subtitle="Per-turn cost, latency, model and tool usage across every agent invocation" />

      <div className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <StatCard label="Total turns" value={summary?.total ?? '—'} />
        <StatCard label="Total cost" value={fmtCost(t.sum_cost)} hint={t.turns_with_cost ? `${t.turns_with_cost} turns tracked` : 'no cost data yet'} />
        <StatCard label="Success rate" value={t.success_rate !== undefined ? `${(t.success_rate * 100).toFixed(1)}%` : '—'} />
        <StatCard label="Avg duration" value={fmtDuration(t.avg_duration_ms)} />
        <StatCard label="Avg confidence" value={t.avg_confidence !== null && t.avg_confidence !== undefined ? Number(t.avg_confidence).toFixed(2) : '—'} />
        <StatCard label="Model coverage" value={t.turns_with_model !== undefined ? `${t.turns_with_model}/${summary?.total ?? 0}` : '—'} hint="turns with model recorded" />
      </div>

      <div className="mb-4 flex flex-wrap items-center gap-3">
        <select
          className="h-9 rounded-md border border-border bg-background px-3 text-sm"
          value={filters.status}
          onChange={(e) => setFilters((f) => ({ ...f, status: e.target.value }))}
        >
          <option value="">All statuses</option>
          <option value="completed">completed</option>
          <option value="failed">failed</option>
          <option value="pending">pending</option>
        </select>
        <select
          className="h-9 rounded-md border border-border bg-background px-3 text-sm"
          value={filters.invocation_type}
          onChange={(e) => setFilters((f) => ({ ...f, invocation_type: e.target.value }))}
        >
          <option value="">All types</option>
          <option value="conversation">conversation</option>
          <option value="sub_agent">sub_agent</option>
        </select>
        <select
          className="h-9 rounded-md border border-border bg-background px-3 text-sm"
          value={filters.agent_app_id}
          onChange={(e) => setFilters((f) => ({ ...f, agent_app_id: e.target.value }))}
        >
          <option value="">All agents</option>
          {agents.map((a) => <option key={a} value={a}>{a}</option>)}
        </select>
        {error ? <span className="text-sm text-red-600">{error}</span> : null}
      </div>

      <div className="overflow-hidden rounded-xl border border-border bg-card">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border bg-muted/30 text-left text-xs text-muted-foreground">
              <th className="px-4 py-2.5 font-medium">Time</th>
              <th className="px-4 py-2.5 font-medium">Type</th>
              <th className="px-4 py-2.5 font-medium">Status</th>
              <th className="px-4 py-2.5 font-medium">Model</th>
              <th className="px-4 py-2.5 font-medium">Tools</th>
              <th className="px-4 py-2.5 font-medium">Duration</th>
              <th className="px-4 py-2.5 font-medium">Cost</th>
              <th className="px-4 py-2.5 font-medium">Confidence</th>
              <th className="px-4 py-2.5 font-medium">Conversation</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={9} className="px-4 py-10 text-center text-muted-foreground"><Loader2 className="mx-auto h-5 w-5 animate-spin" /></td></tr>
            ) : rows.length === 0 ? (
              <tr><td colSpan={9} className="px-4 py-10 text-center text-muted-foreground">No invocations recorded yet — run a conversation turn to populate this view.</td></tr>
            ) : rows.map((r) => (
              <tr
                key={r.id}
                className="cursor-pointer border-b border-border/60 hover:bg-muted/20"
                onClick={() => openDetail(r)}
              >
                <td className="px-4 py-2.5 text-muted-foreground">{fmtDate(r.created_date)}</td>
                <td className="px-4 py-2.5">
                  <span className="inline-flex items-center gap-1.5">
                    {r.invocation_type === 'sub_agent' ? <GitBranch className="h-3.5 w-3.5 text-muted-foreground" /> : <MessageSquare className="h-3.5 w-3.5 text-muted-foreground" />}
                    {r.invocation_type}
                  </span>
                </td>
                <td className="px-4 py-2.5"><StatusBadge status={r.status} /></td>
                <td className="px-4 py-2.5">{r.model_name || '—'}</td>
                <td className="px-4 py-2.5">{r.tool_call_count ?? '—'}</td>
                <td className="px-4 py-2.5">{fmtDuration(r.duration_ms)}</td>
                <td className="px-4 py-2.5">{fmtCost(r.cost_amount)}</td>
                <td className="px-4 py-2.5">{r.confidence_score !== null && r.confidence_score !== undefined ? Number(r.confidence_score).toFixed(2) : '—'}</td>
                <td className="px-4 py-2.5">
                  {r.conversation_id ? (
                    <a
                      href={`/chat?conv=${r.conversation_id}`}
                      className="text-primary hover:underline"
                      onClick={(e) => e.stopPropagation()}
                    >
                      {r.conversation_id.slice(0, 8)}
                    </a>
                  ) : <span className="text-muted-foreground">—</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {selected ? (
        <div className="fixed inset-0 z-50 flex items-start justify-end bg-black/30" onClick={() => setSelected(null)}>
          <div className="h-full w-full max-w-lg overflow-y-auto border-l border-border bg-background p-6" onClick={(e) => e.stopPropagation()}>
            <div className="mb-4 flex items-start justify-between">
              <div>
                <h3 className="text-lg font-semibold">Invocation detail</h3>
                <p className="text-xs text-muted-foreground">{selected.id}</p>
              </div>
              <button className="rounded-md p-1 hover:bg-muted/30" onClick={() => setSelected(null)}><X className="h-5 w-5" /></button>
            </div>

            {detailLoading ? <Loader2 className="mx-auto h-5 w-5 animate-spin" /> : (
              <div className="space-y-4 text-sm">
                <div className="grid grid-cols-2 gap-2">
                  <div className="rounded-lg bg-muted/30 p-3"><p className="text-xs text-muted-foreground">Status</p><p><StatusBadge status={selected.status} /></p></div>
                  <div className="rounded-lg bg-muted/30 p-3"><p className="text-xs text-muted-foreground">Model</p><p>{selected.model_name || '—'}</p></div>
                  <div className="rounded-lg bg-muted/30 p-3"><p className="text-xs text-muted-foreground">Duration</p><p>{fmtDuration(selected.duration_ms)}</p></div>
                  <div className="rounded-lg bg-muted/30 p-3"><p className="text-xs text-muted-foreground">Cost</p><p>{fmtCost(selected.cost_amount)}</p></div>
                  <div className="rounded-lg bg-muted/30 p-3"><p className="text-xs text-muted-foreground">Tool calls</p><p>{selected.tool_call_count ?? '—'}</p></div>
                  <div className="rounded-lg bg-muted/30 p-3"><p className="text-xs text-muted-foreground">Confidence</p><p>{selected.confidence_score ?? '—'}</p></div>
                </div>

                {selected.token_usage ? (
                  <div className="rounded-lg bg-muted/30 p-3">
                    <p className="text-xs text-muted-foreground">Token usage</p>
                    <p className="mt-1 font-mono text-xs">
                      prompt {selected.token_usage.prompt_tokens ?? '—'} · completion {selected.token_usage.completion_tokens ?? '—'} · total {selected.token_usage.total_tokens ?? '—'}
                    </p>
                  </div>
                ) : null}

                <div className="rounded-lg bg-muted/30 p-3">
                  <p className="text-xs text-muted-foreground">Input</p>
                  <p className="mt-1 whitespace-pre-wrap">{selected.input_message || '—'}</p>
                </div>
                <div className="rounded-lg bg-muted/30 p-3">
                  <p className="text-xs text-muted-foreground">Assistant</p>
                  <p className="mt-1 whitespace-pre-wrap">{selected.assistant_content || '—'}</p>
                </div>
                {selected.error_message ? (
                  <div className="rounded-lg bg-red-500/10 p-3">
                    <p className="text-xs text-red-600">Error</p>
                    <p className="mt-1 whitespace-pre-wrap">{selected.error_message}</p>
                  </div>
                ) : null}

                <div className="flex items-center gap-2 text-xs text-muted-foreground">
                  <Activity className="h-3.5 w-3.5" />
                  <span className="font-mono">trace {selected.trace_id || '—'}</span>
                </div>

                {selected.children && selected.children.length > 0 ? (
                  <div className="rounded-lg border border-border p-3">
                    <p className="mb-2 text-xs font-medium text-muted-foreground">Sub-agents spawned ({selected.children.length})</p>
                    {selected.children.map((c) => (
                      <div key={c.id} className="mb-2 rounded-md bg-muted/30 p-2 text-xs">
                        <div className="flex justify-between"><span>{c.status}</span><span>{fmtDuration(c.duration_ms)}</span></div>
                        <p className="mt-1 line-clamp-2 text-muted-foreground">{c.input_excerpt}</p>
                      </div>
                    ))}
                  </div>
                ) : null}
              </div>
            )}
          </div>
        </div>
      ) : null}
    </div>
  );
}
