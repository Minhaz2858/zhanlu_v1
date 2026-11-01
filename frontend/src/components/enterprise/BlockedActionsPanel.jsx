import { useEffect, useState } from 'react';
import { Loader2, ShieldAlert, X } from 'lucide-react';
import { authFetch } from '@/api/authFetch';
import { toast } from 'sonner';

/* Phase 6E: blocked-action investigation drawer.

   Source of truth: AuditEvent business_rule_check rows with the recorded
   decision 'block' — exactly the deterministic rule pre-check refusals.
   Rule references are resolved server-side against org BusinessRule rows;
   an unresolvable rule id is shown as "unavailable", never dropped or
   renamed. A rule's current status is displayed as metadata only — the
   panel never implies a disabled/proposed rule was enforcement. Only
   blocks are listed; allow / require_approval events are not blocks. */

export default function BlockedActionsPanel({ days = 30, onClose }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    (async () => {
      setLoading(true);
      try {
        const res = await authFetch(
          `/api/ontology/enterprise/blocked-actions?days=${days}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const body = await res.json();
        if (alive) setData(body);
      } catch (e) {
        toast.error(`Blocked-action load failed: ${e.message || e}`);
        if (alive) setData({ available: false, items: [], total_blocked: 0 });
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => { alive = false; };
  }, [days]);

  const items = data?.items || [];

  return (
    <div className="fixed inset-0 z-50 bg-black/30 flex justify-end"
      onClick={onClose}>
      <div className="w-full max-w-md bg-white h-full shadow-2xl overflow-y-auto"
        onClick={(e) => e.stopPropagation()}>
        <div className="px-4 py-3 border-b border-stone-100">
          <div className="flex items-center gap-1 text-[10px] text-stone-400">
            <span>Enterprise</span><span>/</span>
            <span className="text-stone-600 font-medium">Blocked actions</span>
            <button onClick={onClose}
              className="ml-auto text-stone-400 hover:text-stone-600"><X size={14} /></button>
          </div>
          <h3 className="text-[13px] font-semibold text-stone-900 flex items-center gap-1.5">
            <ShieldAlert size={13} className="text-red-500" /> Actions blocked by business rules
            <span className="text-[10px] text-stone-400 font-normal">last {days} days</span>
          </h3>
        </div>

        <div className="px-4 py-3 space-y-3 text-[11px]">
          {loading ? (
            <p className="flex items-center gap-1.5 text-stone-400">
              <Loader2 size={12} className="animate-spin" /> Loading…
            </p>
          ) : !data?.available ? (
            <p className="text-stone-400">Blocked-action data unavailable.</p>
          ) : items.length === 0 ? (
            <p className="text-stone-400">
              No actions were blocked by business rules in this window.
            </p>
          ) : (
            <>
              <p className="text-stone-500">
                <b className="text-stone-800">{data.total_blocked}</b> block(s) recorded by
                the deterministic rule pre-check. Each entry traces to an audit record.
              </p>
              {items.map((it, i) => (
                <div key={i} className="rounded-lg border border-stone-200 px-3 py-2">
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-medium text-stone-800">
                      {it.action_name || 'unavailable'}
                    </span>
                    <span className="text-[9px] text-stone-400 whitespace-nowrap">
                      {it.occurred_at ? new Date(it.occurred_at).toLocaleString() : '—'}
                    </span>
                  </div>
                  <div className="mt-1 space-y-0.5 text-stone-600">
                    {it.tool_name && <div>Tool: {it.tool_name}</div>}
                    {it.actor_id && <div>By: {it.actor_type || 'agent'} · {it.actor_id}</div>}
                    {it.evaluation_status && <div>Decision basis: {it.evaluation_status}</div>}
                    {it.approval_required && <div className="text-amber-700">approval was required</div>}
                    <div>
                      {(it.violated_rules || []).length === 0 ? (
                        <span className="text-stone-400">violated rules: none recorded</span>
                      ) : (
                        <div className="space-y-0.5">
                          {(it.violated_rules || []).map((r) => (
                            r.unavailable ? (
                              <div key={r.id} className="text-stone-400">
                                rule {r.id} — reference unavailable
                              </div>
                            ) : (
                              <div key={r.id} className="text-stone-700">
                                “{r.statement}”
                                <span className="ml-1 text-[9px] text-stone-400 capitalize">
                                  (rule status: {r.status || '—'} — current status, shown as reference only)
                                </span>
                              </div>
                            )
                          ))}
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              ))}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
