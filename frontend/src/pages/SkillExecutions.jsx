import { useState, useEffect, useCallback } from 'react';
import { Activity, RefreshCw, Search, X, CheckCircle, AlertCircle, Loader2, ChevronRight, Clock, Cpu } from 'lucide-react';
import { listExecutions } from '@/api/skillStudio';

const STATUS_CONFIG = {
  completed: { icon: CheckCircle, color: 'text-green-400', bg: 'bg-green-500/10', border: 'border-green-500/20', label: 'Completed' },
  failed: { icon: AlertCircle, color: 'text-red-400', bg: 'bg-red-500/10', border: 'border-red-500/20', label: 'Failed' },
  running: { icon: Loader2, color: 'text-blue-400', bg: 'bg-blue-500/10', border: 'border-blue-500/20', label: 'Running' },
  timeout: { icon: Clock, color: 'text-amber-400', bg: 'bg-amber-500/10', border: 'border-amber-500/20', label: 'Timeout' },
};

import { formatShortDateTime } from '@/lib/time';

function formatDuration(ms) {
  if (ms == null) return '—';
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

export default function SkillExecutions() {
  const [executions, setExecutions] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [selected, setSelected] = useState(null);
  const [lastUpdated, setLastUpdated] = useState(null);
  const [total, setTotal] = useState(0);
  const [debouncedSearch, setDebouncedSearch] = useState('');

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search.trim()), 300);
    return () => clearTimeout(timer);
  }, [search]);

  const fetchData = useCallback(async () => {
    try {
      const params = {};
      if (statusFilter) params.status = statusFilter;
      if (debouncedSearch) params.skill_name = debouncedSearch;
      const data = await listExecutions({ ...params, limit: 100 });
      setExecutions(data.executions || []);
      setTotal(data.total || 0);
      setError('');
    } catch (e) {
      setError(e.message || 'Failed to load executions');
    } finally {
      setLoading(false);
      setLastUpdated(new Date());
    }
  }, [debouncedSearch, statusFilter]);

  // Initial load + refresh when the debounced filters change.
  useEffect(() => {
    fetchData();
  }, [fetchData]);

  // Auto-refresh every 30s using the latest debounced filters.
  useEffect(() => {
    const interval = setInterval(fetchData, 30000);
    return () => clearInterval(interval);
  }, [fetchData]);

  const stats = {
    total: executions.length,
    completed: executions.filter((e) => e.status === 'completed').length,
    failed: executions.filter((e) => e.status === 'failed').length,
    avgDuration: executions.length > 0
      ? Math.round(executions.reduce((sum, e) => sum + (e.duration_ms || 0), 0) / executions.length)
      : 0,
  };

  return (
    <div className="flex h-full flex-col bg-slate-950">
      {/* Header */}
      <div className="border-b border-slate-800 bg-slate-900/50 px-6 py-4 backdrop-blur">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-indigo-500/20">
              <Activity className="h-5 w-5 text-indigo-400" />
            </div>
            <div>
              <h1 className="text-lg font-semibold text-slate-100">Skill Executions</h1>
              <p className="text-xs text-slate-500">Live execution evidence for all skill invocations</p>
            </div>
          </div>
          <div className="flex items-center gap-3">
            {lastUpdated && (
              <span className="text-[11px] text-slate-500">
                Last updated {lastUpdated.toLocaleTimeString()}
              </span>
            )}
            <button
              onClick={() => { setLoading(true); fetchData(); }}
              disabled={loading}
              className="inline-flex items-center gap-1.5 rounded-lg border border-slate-700 bg-slate-800 px-3 py-1.5 text-xs font-medium text-slate-300 hover:bg-slate-700 disabled:opacity-50"
            >
              <RefreshCw className={`h-3 w-3 ${loading ? 'animate-spin' : ''}`} />
              Refresh
            </button>
          </div>
        </div>

        {/* Stats bar */}
        <div className="mt-4 flex gap-3">
          <StatCard label="Total Runs" value={stats.total} icon={Activity} color="indigo" />
          <StatCard label="Completed" value={stats.completed} icon={CheckCircle} color="green" />
          <StatCard label="Failed" value={stats.failed} icon={AlertCircle} color="red" />
          <StatCard label="Avg Duration" value={formatDuration(stats.avgDuration)} icon={Clock} color="amber" />
        </div>
      </div>

      {/* Filter bar */}
      <div className="flex items-center gap-3 border-b border-slate-800 bg-slate-900/30 px-6 py-3">
        <div className="flex items-center gap-1.5 rounded-lg bg-slate-800/50 px-3 py-1.5">
          <Search className="h-3.5 w-3.5 text-slate-500" />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search by skill name..."
            className="w-64 bg-transparent text-sm text-slate-200 placeholder:text-slate-500 focus:outline-none"
          />
          {search && (
            <button onClick={() => setSearch('')} className="text-slate-500 hover:text-slate-300">
              <X className="h-3 w-3" />
            </button>
          )}
        </div>
        <div className="flex items-center gap-1">
          {['', 'completed', 'failed', 'running'].map((s) => (
            <button
              key={s || 'all'}
              onClick={() => setStatusFilter(s)}
              className={`rounded-md px-2.5 py-1 text-xs font-medium transition ${statusFilter === s
                  ? 'bg-indigo-500 text-white'
                  : 'bg-slate-800/50 text-slate-400 hover:bg-slate-700'
                }`}
            >
              {s ? s.charAt(0).toUpperCase() + s.slice(1) : 'All'}
            </button>
          ))}
        </div>
        <span className="ml-auto text-xs text-slate-500">{total} total records</span>
      </div>

      {/* Main content: table + detail panel */}
      <div className="flex flex-1 overflow-hidden">
        {/* Table */}
        <div className="flex-1 overflow-y-auto">
          {loading && executions.length === 0 ? (
            <div className="flex h-full items-center justify-center">
              <Loader2 className="h-6 w-6 animate-spin text-slate-600" />
            </div>
          ) : error ? (
            <div className="flex h-full flex-col items-center justify-center text-center">
              <AlertCircle className="mb-2 h-8 w-8 text-red-500/50" />
              <p className="text-sm text-red-400">{error}</p>
            </div>
          ) : executions.length === 0 ? (
            <div className="flex h-full flex-col items-center justify-center text-center">
              <Activity className="mb-3 h-12 w-12 text-slate-700" />
              <p className="text-sm text-slate-500">No skill executions yet.</p>
              <p className="mt-1 text-xs text-slate-600">Create or scrape a skill to see execution evidence here.</p>
            </div>
          ) : (
            <table className="w-full">
              <thead className="sticky top-0 bg-slate-900/80 backdrop-blur">
                <tr className="border-b border-slate-800 text-left">
                  <th className="px-4 py-2 text-[11px] font-medium uppercase tracking-wide text-slate-500">Skill</th>
                  <th className="px-4 py-2 text-[11px] font-medium uppercase tracking-wide text-slate-500">Agent</th>
                  <th className="px-4 py-2 text-[11px] font-medium uppercase tracking-wide text-slate-500">Action</th>
                  <th className="px-4 py-2 text-[11px] font-medium uppercase tracking-wide text-slate-500">Status</th>
                  <th className="px-4 py-2 text-[11px] font-medium uppercase tracking-wide text-slate-500">Duration</th>
                  <th className="px-4 py-2 text-[11px] font-medium uppercase tracking-wide text-slate-500">Time</th>
                  <th className="px-2 py-2"></th>
                </tr>
              </thead>
              <tbody>
                {executions.map((exec) => {
                  const cfg = STATUS_CONFIG[exec.status] || STATUS_CONFIG.completed;
                  const StatusIcon = cfg.icon;
                  const isSelected = selected?.id === exec.id;
                  return (
                    <tr
                      key={exec.id}
                      onClick={() => setSelected(exec)}
                      className={`cursor-pointer border-b border-slate-800/50 transition hover:bg-slate-800/40 ${isSelected ? 'bg-indigo-500/5' : ''
                        }`}
                    >
                      <td className="px-4 py-2.5">
                        <span className="font-mono text-sm text-slate-200">{exec.skill_name}</span>
                      </td>
                      <td className="px-4 py-2.5">
                        <span className="text-xs text-slate-400">{exec.agent_name}</span>
                      </td>
                      <td className="px-4 py-2.5">
                        <span className="inline-flex items-center rounded bg-slate-800 px-1.5 py-0.5 font-mono text-[10px] text-slate-300">
                          {exec.action}
                        </span>
                      </td>
                      <td className="px-4 py-2.5">
                        <span className={`inline-flex items-center gap-1 rounded-md border ${cfg.bg} ${cfg.border} px-2 py-0.5 text-[11px] font-medium ${cfg.color}`}>
                          <StatusIcon className={`h-3 w-3 ${exec.status === 'running' ? 'animate-spin' : ''}`} />
                          {cfg.label}
                        </span>
                      </td>
                      <td className="px-4 py-2.5">
                        <span className="text-xs text-slate-400">{formatDuration(exec.duration_ms)}</span>
                      </td>
                      <td className="px-4 py-2.5">
                        <span className="text-xs text-slate-500">{formatShortDateTime(exec.started_at || exec.created_date)}</span>
                      </td>
                      <td className="px-2 py-2.5">
                        <ChevronRight className={`h-3.5 w-3.5 transition ${isSelected ? 'text-indigo-400' : 'text-slate-600'}`} />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>

        {/* Detail side panel */}
        {selected && (
          <div className="flex w-96 shrink-0 flex-col border-l border-slate-800 bg-slate-900/50">
            <div className="flex items-center justify-between border-b border-slate-800 px-4 py-3">
              <div className="flex items-center gap-2">
                <Cpu className="h-4 w-4 text-indigo-400" />
                <span className="text-sm font-medium text-slate-200">Execution Detail</span>
              </div>
              <button
                onClick={() => setSelected(null)}
                className="inline-flex h-6 w-6 items-center justify-center rounded text-slate-500 hover:bg-slate-800 hover:text-slate-300"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
            <div className="flex-1 overflow-y-auto p-4 space-y-4">
              <DetailField label="Skill Name" value={selected.skill_name} mono />
              <DetailField label="Skill ID" value={selected.skill_id || '—'} mono />
              <DetailField label="Skill Version" value={selected.skill_version || '—'} mono />
              <DetailField label="Agent" value={selected.agent_name} />
              <DetailField label="Action" value={selected.action} mono />
              <DetailField label="Status" value={selected.status} />
              <DetailField label="Duration" value={formatDuration(selected.duration_ms)} />
              <DetailField label="Body Length" value={selected.body_length != null ? `${selected.body_length} chars` : '—'} />
              <DetailField label="Started" value={formatShortDateTime(selected.started_at)} />
              <DetailField label="Completed" value={formatShortDateTime(selected.completed_at)} />
              <DetailField label="Conversation ID" value={selected.conversation_id || '—'} mono />
              {selected.error_message && (
                <div>
                  <div className="mb-1 text-[11px] font-medium uppercase tracking-wide text-slate-500">Error</div>
                  <div className="rounded-lg border border-red-500/20 bg-red-500/5 p-3">
                    <pre className="whitespace-pre-wrap break-words font-mono text-xs text-red-300">{selected.error_message}</pre>
                  </div>
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function StatCard({ label, value, icon: Icon, color }) {
  const colors = {
    indigo: 'text-indigo-400 bg-indigo-500/10',
    green: 'text-green-400 bg-green-500/10',
    red: 'text-red-400 bg-red-500/10',
    amber: 'text-amber-400 bg-amber-500/10',
  };
  return (
    <div className="flex items-center gap-2.5 rounded-lg border border-slate-800 bg-slate-800/30 px-3 py-2">
      <div className={`flex h-7 w-7 items-center justify-center rounded-md ${colors[color]}`}>
        <Icon className="h-3.5 w-3.5" />
      </div>
      <div>
        <div className="text-base font-semibold text-slate-100">{value}</div>
        <div className="text-[10px] text-slate-500">{label}</div>
      </div>
    </div>
  );
}

function DetailField({ label, value, mono }) {
  return (
    <div>
      <div className="mb-0.5 text-[11px] font-medium uppercase tracking-wide text-slate-500">{label}</div>
      <div className={`text-sm text-slate-200 ${mono ? 'font-mono' : ''}`}>{value || '—'}</div>
    </div>
  );
}
