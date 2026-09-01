/**
 * AdminConsole — Governance dashboard for platform administrators.
 *
 * Displays:
 * - Pending approval requests with approve/reject actions
 * - Cost summary and breakdown by type
 * - Recent audit log entries
 * - Policy decision history
 */

import { useState, useEffect, useCallback } from 'react';
import { formatAbsoluteTime } from '@/lib/time';
import {
  Shield, DollarSign, FileText, CheckCircle2, XCircle, Clock,
  Loader2, AlertCircle, ChevronDown, ChevronRight,
} from 'lucide-react';
import { authFetch } from '@/api/authFetch';

const API_BASE = '/api';

async function fetchJSON(url, options) {
  const res = await authFetch(url, options);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export default function AdminConsole() {
  const [tab, setTab] = useState('approvals');
  const [approvals, setApprovals] = useState([]);
  const [costSummary, setCostSummary] = useState(null);
  const [costBreakdown, setCostBreakdown] = useState([]);
  const [auditLogs, setAuditLogs] = useState([]);
  const [policyDecisions, setPolicyDecisions] = useState([]);
  const [loading, setLoading] = useState(true);
  const [actionLoading, setActionLoading] = useState({});

  const loadData = useCallback(async () => {
    try {
      const [apps, cost, breakdown, logs, decisions] = await Promise.all([
        fetchJSON(`${API_BASE}/governance/approvals?status=pending`),
        fetchJSON(`${API_BASE}/governance/cost`),
        fetchJSON(`${API_BASE}/governance/cost/breakdown`),
        fetchJSON(`${API_BASE}/governance/audit?limit=50`),
        fetchJSON(`${API_BASE}/governance/policy-decisions?limit=50`),
      ]);
      setApprovals(apps);
      setCostSummary(cost);
      setCostBreakdown(breakdown);
      setAuditLogs(logs);
      setPolicyDecisions(decisions);
    } catch (e) {
      console.error('Failed to load admin data:', e);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadData(); }, [loadData]);

  async function handleApprove(id) {
    setActionLoading({ ...actionLoading, [id]: 'approving' });
    try {
      await fetchJSON(`${API_BASE}/governance/approvals/${id}/approve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reviewed_by: 'admin' }),
      });
      loadData();
    } catch (e) { console.error(e); }
    setActionLoading({ ...actionLoading, [id]: null });
  }

  async function handleReject(id) {
    setActionLoading({ ...actionLoading, [id]: 'rejecting' });
    try {
      await fetchJSON(`${API_BASE}/governance/approvals/${id}/reject`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reviewed_by: 'admin', reason: 'Rejected by admin' }),
      });
      loadData();
    } catch (e) { console.error(e); }
    setActionLoading({ ...actionLoading, [id]: null });
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  const tabs = [
    { key: 'approvals', label: 'Approvals', icon: Shield, count: approvals.length },
    { key: 'cost', label: 'Cost', icon: DollarSign },
    { key: 'audit', label: 'Audit Log', icon: FileText, count: auditLogs.length },
    { key: 'policy', label: 'Policy Decisions', icon: CheckCircle2, count: policyDecisions.length },
  ];

  return (
    <div className="mx-auto max-w-6xl p-6">
      <h1 className="mb-6 text-2xl font-bold text-foreground">Admin Console</h1>

      {/* Tabs */}
      <div className="mb-6 flex gap-1 border-b border-border">
        {tabs.map(t => {
          const Icon = t.icon;
          return (
            <button
              key={t.key}
              onClick={() => setTab(t.key)}
              className={`flex items-center gap-2 border-b-2 px-4 py-2.5 text-sm font-medium transition-colors ${
                tab === t.key
                  ? 'border-primary text-primary'
                  : 'border-transparent text-muted-foreground hover:text-foreground'
              }`}
            >
              <Icon className="h-4 w-4" />
              {t.label}
              {t.count !== undefined && t.count > 0 && (
                <span className="rounded-full bg-secondary px-1.5 py-0.5 text-[10px] font-bold">
                  {t.count}
                </span>
              )}
            </button>
          );
        })}
      </div>

      {/* Approvals Tab */}
      {tab === 'approvals' && (
        <div className="space-y-3">
          {approvals.length === 0 ? (
            <p className="py-8 text-center text-sm text-muted-foreground">No pending approvals</p>
          ) : (
            approvals.map(req => (
              <div key={req.id} className="rounded-xl border border-border bg-card p-4">
                <div className="flex items-start justify-between gap-4">
                  <div>
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-medium text-foreground">{req.action_type}</span>
                      <span className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${
                        req.risk_tier === 'high' ? 'bg-red-100 text-red-600' :
                        req.risk_tier === 'medium' ? 'bg-amber-100 text-amber-600' :
                        'bg-green-100 text-green-600'
                      }`}>{req.risk_tier}</span>
                    </div>
                    <p className="mt-1 text-sm text-muted-foreground">{req.action_description}</p>
                    {req.execution_id && (
                      <p className="mt-1 text-[11px] text-muted-foreground">Execution: {req.execution_id.slice(0, 8)}</p>
                    )}
                  </div>
                  <div className="flex gap-2">
                    <button
                      onClick={() => handleApprove(req.id)}
                      disabled={actionLoading[req.id]}
                      className="inline-flex items-center gap-1 rounded-md bg-green-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-green-700 disabled:opacity-50"
                    >
                      {actionLoading[req.id] === 'approving' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <CheckCircle2 className="h-3.5 w-3.5" />}
                      Approve
                    </button>
                    <button
                      onClick={() => handleReject(req.id)}
                      disabled={actionLoading[req.id]}
                      className="inline-flex items-center gap-1 rounded-md border border-border px-3 py-1.5 text-xs font-medium text-muted-foreground hover:bg-secondary disabled:opacity-50"
                    >
                      {actionLoading[req.id] === 'rejecting' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <XCircle className="h-3.5 w-3.5" />}
                      Reject
                    </button>
                  </div>
                </div>
              </div>
            ))
          )}
        </div>
      )}

      {/* Cost Tab */}
      {tab === 'cost' && (
        <div className="space-y-6">
          {costSummary && (
            <div className="rounded-xl border border-border bg-card p-6">
              <div className="flex items-center gap-2">
                <DollarSign className="h-5 w-5 text-green-500" />
                <span className="text-sm font-medium text-muted-foreground">Total Cost</span>
              </div>
              <p className="mt-2 text-3xl font-bold text-foreground">
                ${costSummary.total_cost.toFixed(4)}
              </p>
              <p className="mt-1 text-xs text-muted-foreground">{costSummary.entry_count} entries</p>
            </div>
          )}

          <div>
            <h3 className="mb-3 text-sm font-medium text-foreground">Cost Breakdown</h3>
            {costBreakdown.length === 0 ? (
              <p className="text-sm text-muted-foreground">No cost data yet</p>
            ) : (
              <div className="space-y-2">
                {costBreakdown.map(item => (
                  <div key={item.cost_type} className="flex items-center justify-between rounded-lg border border-border bg-card px-4 py-2.5">
                    <div>
                      <span className="text-sm font-medium text-foreground">{item.cost_type}</span>
                      <span className="ml-2 text-xs text-muted-foreground">{item.count} entries</span>
                    </div>
                    <span className="text-sm font-bold text-foreground">${item.total_cost.toFixed(4)}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Audit Tab */}
      {tab === 'audit' && (
        <div className="space-y-1">
          {auditLogs.length === 0 ? (
            <p className="py-8 text-center text-sm text-muted-foreground">No audit logs</p>
          ) : (
            auditLogs.map((log, i) => (
              <div key={i} className="flex items-center gap-3 rounded-lg border border-border bg-card px-4 py-2 text-xs">
                <span className={`h-2 w-2 shrink-0 rounded-full ${
                  log.result === 'success' ? 'bg-green-500' : log.result === 'denied' ? 'bg-red-500' : 'bg-amber-500'
                }`} />
                <span className="shrink-0 font-mono text-muted-foreground">
                  {log.occurred_at ? formatAbsoluteTime(log.occurred_at) : '—'}
                </span>
                <span className="shrink-0 font-medium text-foreground">{log.action}</span>
                <span className="shrink-0 text-muted-foreground">{log.resource_type}</span>
                {log.actor_name && <span className="shrink-0 text-muted-foreground">by {log.actor_name}</span>}
                {log.description && <span className="min-w-0 truncate text-muted-foreground">{log.description}</span>}
              </div>
            ))
          )}
        </div>
      )}

      {/* Policy Decisions Tab */}
      {tab === 'policy' && (
        <div className="space-y-1">
          {policyDecisions.length === 0 ? (
            <p className="py-8 text-center text-sm text-muted-foreground">No policy decisions</p>
          ) : (
            policyDecisions.map((d, i) => (
              <div key={i} className="flex items-center gap-3 rounded-lg border border-border bg-card px-4 py-2 text-xs">
                <span className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${
                  d.decision === 'allow' ? 'bg-green-100 text-green-600' :
                  d.decision === 'deny' ? 'bg-red-100 text-red-600' :
                  'bg-amber-100 text-amber-600'
                }`}>{d.decision}</span>
                <span className={`rounded-full px-2 py-0.5 text-[10px] ${
                  d.risk_tier === 'high' ? 'bg-red-50 text-red-500' :
                  d.risk_tier === 'medium' ? 'bg-amber-50 text-amber-500' :
                  'bg-gray-50 text-gray-500'
                }`}>{d.risk_tier}</span>
                <span className="text-muted-foreground">{d.decision_level}</span>
                {d.reasons && d.reasons[0] && <span className="min-w-0 truncate text-muted-foreground">{d.reasons[0]}</span>}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}
