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
  Loader2, AlertCircle, ChevronDown, ChevronRight, UserCheck, GitMerge,
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
  const [escalations, setEscalations] = useState([]);
  const [identityMatches, setIdentityMatches] = useState([]);
  const [loading, setLoading] = useState(true);
  const [actionLoading, setActionLoading] = useState({});

  const loadData = useCallback(async () => {
    try {
      const [apps, cost, breakdown, logs, decisions, escs, matches] = await Promise.all([
        fetchJSON(`${API_BASE}/governance/approvals?status=pending`),
        fetchJSON(`${API_BASE}/governance/cost`),
        fetchJSON(`${API_BASE}/governance/cost/breakdown`),
        fetchJSON(`${API_BASE}/governance/audit?limit=50`),
        fetchJSON(`${API_BASE}/governance/policy-decisions?limit=50`),
        // P3-6: the human handoff queue (escalations with full context)
        fetchJSON(`${API_BASE}/ontology/escalations`),
        // P3: cross-source identity review queue (inferred crosswalks)
        fetchJSON(`${API_BASE}/ontology/identity/matches`),
      ]);
      setApprovals(apps);
      setCostSummary(cost);
      setCostBreakdown(breakdown);
      setAuditLogs(logs);
      setPolicyDecisions(decisions);
      setEscalations(Array.isArray(escs) ? escs : []);
      setIdentityMatches(Array.isArray(matches) ? matches : []);
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

  async function handleResolveEscalation(id) {
    setActionLoading({ ...actionLoading, [`esc-${id}`]: 'resolving' });
    try {
      await fetchJSON(`${API_BASE}/ontology/escalations/${id}/resolve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ note: 'Resolved by admin' }),
      });
      loadData();
    } catch (e) { console.error(e); }
    setActionLoading({ ...actionLoading, [`esc-${id}`]: null });
  }

  // P3: identity crosswalk review — confirm activates, reject remembers.
  async function handleIdentityReview(id, decision) {
    setActionLoading({ ...actionLoading, [`id-${id}`]: decision });
    try {
      await fetchJSON(`${API_BASE}/ontology/identity/matches/${id}/${decision}`, {
        method: 'POST',
      });
      loadData();
    } catch (e) { console.error(e); }
    setActionLoading({ ...actionLoading, [`id-${id}`]: null });
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  const openEscalations = escalations.filter((e) => e.status === 'open');
  const pendingIdentity = identityMatches.filter((m) => m.status === 'inferred');
  const tabs = [
    { key: 'approvals', label: 'Approvals', icon: Shield, count: approvals.length },
    { key: 'escalations', label: 'Escalations', icon: UserCheck, count: openEscalations.length },
    { key: 'identity', label: 'Identity', icon: GitMerge, count: pendingIdentity.length },
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

      {/* Escalations Tab (P3-6: human handoff queue with full context) */}
      {tab === 'escalations' && (
        <div className="space-y-3">
          {escalations.length === 0 ? (
            <p className="py-8 text-center text-sm text-muted-foreground">
              No escalations — agents are handling everything, or nothing needs a human yet
            </p>
          ) : (
            escalations.map((esc) => (
              <div key={esc.id} className="rounded-xl border border-border bg-card p-4">
                <div className="flex items-start justify-between gap-4">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${
                        esc.status === 'open'
                          ? 'bg-amber-100 text-amber-700'
                          : 'bg-green-100 text-green-700'
                      }`}>{esc.status}</span>
                      {esc.sentiment && (
                        <span className="rounded-full bg-red-50 px-2 py-0.5 text-[10px] text-red-600">
                          {esc.sentiment}
                        </span>
                      )}
                      {esc.agent_name && (
                        <span className="text-[11px] text-muted-foreground">from {esc.agent_name}</span>
                      )}
                    </div>
                    <p className="mt-1.5 text-sm font-medium text-foreground">{esc.summary}</p>
                    {esc.context && (
                      <details className="mt-1.5">
                        <summary className="cursor-pointer text-[11px] text-primary">context transferred</summary>
                        <pre className="mt-1 max-h-40 overflow-auto rounded-md bg-secondary p-2 text-[10px] leading-relaxed">
                          {JSON.stringify(esc.context, null, 2)}
                        </pre>
                      </details>
                    )}
                    {esc.resolution_note && (
                      <p className="mt-1 text-[11px] text-green-700">Resolved: {esc.resolution_note}</p>
                    )}
                  </div>
                  {esc.status === 'open' && (
                    <button
                      onClick={() => handleResolveEscalation(esc.id)}
                      disabled={actionLoading[`esc-${esc.id}`]}
                      className="inline-flex shrink-0 items-center gap-1 rounded-md bg-green-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-green-700 disabled:opacity-50"
                    >
                      {actionLoading[`esc-${esc.id}`] === 'resolving' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <CheckCircle2 className="h-3.5 w-3.5" />}
                      Resolve
                    </button>
                  )}
                </div>
              </div>
            ))
          )}
        </div>
      )}

      {/* Identity Tab (P3: cross-source identity review queue) */}
      {tab === 'identity' && (
        <div className="space-y-3">
          {identityMatches.length === 0 ? (
            <p className="py-8 text-center text-sm text-muted-foreground">
              No identity crosswalks yet — run identity resolution on a bound object to populate this queue
            </p>
          ) : (
            identityMatches.map((m) => (
              <div key={m.id} className="rounded-xl border border-border bg-card p-4">
                <div className="flex items-start justify-between gap-4">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${
                        m.status === 'confirmed' ? 'bg-green-100 text-green-700' :
                        m.status === 'rejected' ? 'bg-red-100 text-red-600' :
                        'bg-amber-100 text-amber-700'
                      }`}>{m.status}</span>
                      <span className="rounded-full bg-secondary px-2 py-0.5 text-[10px] text-muted-foreground">
                        {m.match_tier}
                      </span>
                      {m.confidence != null && (
                        <span className="text-[11px] text-muted-foreground">
                          confidence {(m.confidence * 100).toFixed(0)}%
                        </span>
                      )}
                      <span className="text-[11px] text-muted-foreground">
                        object {m.object_id?.slice(0, 8)}
                      </span>
                    </div>
                    <p className="mt-1.5 font-mono text-sm text-foreground">
                      {m.pk_a} <span className="text-muted-foreground">≡</span> {m.pk_b}
                    </p>
                    <p className="mt-0.5 text-[11px] text-muted-foreground">
                      {m.source_a_key} ⇄ {m.source_b_key}
                    </p>
                    {m.evidence_json && (
                      <details className="mt-1.5">
                        <summary className="cursor-pointer text-[11px] text-primary">match evidence</summary>
                        <pre className="mt-1 max-h-40 overflow-auto rounded-md bg-secondary p-2 text-[10px] leading-relaxed">
                          {JSON.stringify(m.evidence_json, null, 2)}
                        </pre>
                      </details>
                    )}
                    {m.reviewed_by && (
                      <p className="mt-1 text-[11px] text-muted-foreground">
                        reviewed by {m.reviewed_by}
                      </p>
                    )}
                  </div>
                  {m.status === 'inferred' && (
                    <div className="flex shrink-0 gap-2">
                      <button
                        onClick={() => handleIdentityReview(m.id, 'confirm')}
                        disabled={!!actionLoading[`id-${m.id}`]}
                        className="inline-flex items-center gap-1 rounded-md bg-green-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-green-700 disabled:opacity-50"
                      >
                        {actionLoading[`id-${m.id}`] === 'confirm' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <CheckCircle2 className="h-3.5 w-3.5" />}
                        Confirm
                      </button>
                      <button
                        onClick={() => handleIdentityReview(m.id, 'reject')}
                        disabled={!!actionLoading[`id-${m.id}`]}
                        className="inline-flex items-center gap-1 rounded-md border border-border px-3 py-1.5 text-xs font-medium text-muted-foreground hover:bg-secondary disabled:opacity-50"
                      >
                        {actionLoading[`id-${m.id}`] === 'reject' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <XCircle className="h-3.5 w-3.5" />}
                        Reject
                      </button>
                    </div>
                  )}
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
