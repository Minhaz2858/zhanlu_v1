/**
 * Phase 8 T8-10 — run provenance inspection panel.
 *
 * Answers, for one agent run: which tools executed (and which failed),
 * the run's terminal status (verbatim — failed/partial never look ok),
 * and the parent run linkage for delegated work. Evidence entries ride
 * the same envelope when present. Missing/unavailable envelopes are
 * reported honestly — never fabricated.
 */
import { useState, useEffect } from 'react';
import { Wrench, ShieldCheck, AlertTriangle, Loader2 } from 'lucide-react';
import { authFetch } from '@/api/authFetch';

export default function RunProvenancePanel({ runId }) {
  const [envelope, setEnvelope] = useState(null);
  const [state, setState] = useState('loading'); // loading|ready|unavailable

  useEffect(() => {
    let cancelled = false;
    if (!runId) return undefined;
    (async () => {
      try {
        const res = await authFetch(`/api/agent-runs/${runId}/provenance`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const body = await res.json();
        if (!cancelled) {
          setEnvelope(body);
          setState('ready');
        }
      } catch {
        if (!cancelled) setState('unavailable');
      }
    })();
    return () => { cancelled = true; };
  }, [runId]);

  if (state === 'loading') {
    return (
      <div className="flex items-center gap-1.5 px-3 py-2 text-[11px] text-muted-foreground">
        <Loader2 className="h-3 w-3 animate-spin" /> Loading provenance…
      </div>
    );
  }
  if (state === 'unavailable' || !envelope || !envelope.run) {
    return (
      <div className="px-3 py-2 text-[11px] text-muted-foreground">
        Provenance for this run was not recorded.
      </div>
    );
  }

  const run = envelope.run || {};
  const entries = envelope.entries || [];
  const failed = entries.filter(
    (e) => e.status && e.status !== 'ok' && e.status !== 'completed');
  const runFailed = run.status && run.status !== 'completed';

  return (
    <div data-testid="run-provenance"
      className="rounded-lg border border-border bg-background px-3 py-2 text-[11px]">
      <div className="mb-1 flex items-center gap-1.5 font-medium text-foreground">
        <ShieldCheck className="h-3 w-3 text-primary" />
        <span>
          Run provenance
          {runFailed ? (
            <span className="ml-1.5 rounded bg-red-500/10 px-1 py-0.5 text-[10px] text-red-600">
              {run.status} — incomplete
            </span>
          ) : (
            <span className="ml-1.5 rounded bg-emerald-500/10 px-1 py-0.5 text-[10px] text-emerald-600">
              {run.status}
            </span>
          )}
        </span>
      </div>

      {run.parent_run_id && (
        <p className="mb-1 text-muted-foreground">
          Delegated from parent run {run.parent_run_id}
        </p>
      )}

      {entries.length === 0 ? (
        <p className="text-muted-foreground">No tool executions recorded.</p>
      ) : (
        <ul className="space-y-1">
          {entries.map((e) => (
            <li key={e.source_ref} className="flex items-center gap-1.5">
              <Wrench className="h-3 w-3 text-muted-foreground" />
              <span className="text-foreground">{e.tool_name}</span>
              {e.status && e.status !== 'ok' && e.status !== 'completed' ? (
                <span className="inline-flex items-center gap-0.5 rounded bg-red-500/10 px-1 text-[10px] text-red-600">
                  <AlertTriangle className="h-2.5 w-2.5" /> failed
                </span>
              ) : null}
            </li>
          ))}
        </ul>
      )}

      {failed.length > 0 && (
        <p className="mt-1 text-[10px] text-muted-foreground">
          {failed.length} tool execution(s) failed — parts of this answer may
          be incomplete.
        </p>
      )}
    </div>
  );
}
