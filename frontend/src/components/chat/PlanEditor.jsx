/**
 * PlanEditor — Editable PlanDAG display for the Synexia FSM.
 *
 * Shows the execution plan as a vertical list of nodes with their
 * dependencies, statuses, and types.  The user can approve or reject
 * the plan at the GATE state.
 *
 * Node types: skill | tool | nl2sql | sandbox | agent
 * Node statuses: pending | approved | running | completed | failed | skipped
 */

import {
  Loader2, CheckCircle2, AlertCircle, Clock, Terminal,
  Database, Container, Wrench, User, ChevronRight,
} from 'lucide-react';

const NODE_TYPE_META = {
  skill: { icon: Wrench, color: 'text-purple-500', bg: 'bg-purple-50', label: 'Skill' },
  tool: { icon: Terminal, color: 'text-blue-500', bg: 'bg-blue-50', label: 'Tool' },
  nl2sql: { icon: Database, color: 'text-cyan-500', bg: 'bg-cyan-50', label: 'Data Query' },
  sandbox: { icon: Container, color: 'text-orange-500', bg: 'bg-orange-50', label: 'Sandbox' },
  agent: { icon: User, color: 'text-green-500', bg: 'bg-green-50', label: 'Agent' },
};

const NODE_STATUS_META = {
  pending: { icon: Clock, color: 'text-gray-500', label: 'Pending' },
  approved: { icon: CheckCircle2, color: 'text-green-500', label: 'Approved' },
  running: { icon: Loader2, color: 'text-blue-500', label: 'Running', spin: true },
  completed: { icon: CheckCircle2, color: 'text-green-600', label: 'Completed' },
  failed: { icon: AlertCircle, color: 'text-red-500', label: 'Failed' },
  skipped: { icon: Clock, color: 'text-gray-400', label: 'Skipped' },
};

export default function PlanEditor({ plan, onApprove, onReject, readOnly = false }) {
  if (!plan) {
    return (
      <div className="flex items-center gap-2 rounded-lg border border-border bg-card p-4 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" /> Generating plan...
      </div>
    );
  }

  const nodes = plan.nodes || [];
  const isDraft = plan.status === 'draft';
  const isApproved = plan.status === 'approved';

  return (
    <div className="rounded-xl border border-border bg-card overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-border px-4 py-2.5 bg-secondary/30">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-foreground">Execution Plan</span>
          <span className="text-[11px] text-muted-foreground">{nodes.length} steps</span>
          {!plan.is_acyclic && (
            <span className="rounded-full bg-red-100 px-2 py-0.5 text-[10px] font-medium text-red-600">
              Cycle detected
            </span>
          )}
        </div>
        <span className={`text-xs font-medium ${
          isDraft ? 'text-amber-500' : isApproved ? 'text-green-500' : 'text-red-500'
        }`}>
          {plan.status}
        </span>
      </div>

      {/* Plan summary */}
      {plan.summary && (
        <div className="border-b border-border px-4 py-2">
          <p className="text-xs text-muted-foreground">{plan.summary}</p>
        </div>
      )}

      {/* Nodes */}
      <div className="p-3 space-y-2">
        {nodes.map((node, i) => {
          const typeMeta = NODE_TYPE_META[node.node_type] || NODE_TYPE_META.tool;
          const statusMeta = NODE_STATUS_META[node.status] || NODE_STATUS_META.pending;
          const TypeIcon = typeMeta.icon;
          const StatusIcon = statusMeta.icon;

          return (
            <div key={i} className="relative">
              {/* Dependency connector */}
              {i > 0 && (
                <div className="absolute -top-2 left-5 h-2 w-px bg-border" />
              )}
              <div className={`flex items-start gap-3 rounded-lg border border-border p-3 ${typeMeta.bg}`}>
                <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-white shadow-sm">
                  <TypeIcon className={`h-3.5 w-3.5 ${typeMeta.color}`} />
                </div>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="text-xs font-medium text-foreground">{node.name}</span>
                    <span className={`text-[10px] ${typeMeta.color}`}>{typeMeta.label}</span>
                  </div>
                  {node.description && (
                    <p className="mt-0.5 text-[11px] text-muted-foreground">{node.description}</p>
                  )}
                  {node.expected_output && (
                    <div className="mt-1 flex items-center gap-1 text-[10px] text-muted-foreground">
                      <ChevronRight className="h-3 w-3" /> {node.expected_output}
                    </div>
                  )}
                  {node.error && (
                    <p className="mt-1 text-[11px] text-red-500">{node.error}</p>
                  )}
                </div>
                <div className="flex shrink-0 items-center gap-1">
                  <StatusIcon className={`h-3.5 w-3.5 ${statusMeta.color} ${statusMeta.spin ? 'animate-spin' : ''}`} />
                  <span className={`text-[10px] ${statusMeta.color}`}>{statusMeta.label}</span>
                </div>
              </div>
            </div>
          );
        })}
      </div>

      {/* Actions */}
      {isDraft && !readOnly && (
        <div className="flex items-center gap-2 border-t border-border px-4 py-3">
          <button
            onClick={onApprove}
            className="inline-flex items-center gap-1.5 rounded-md bg-green-600 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-green-700"
          >
            <CheckCircle2 className="h-3.5 w-3.5" /> Approve Plan
          </button>
          <button
            onClick={onReject}
            className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-secondary"
          >
            <AlertCircle className="h-3.5 w-3.5" /> Reject
          </button>
        </div>
      )}
    </div>
  );
}
