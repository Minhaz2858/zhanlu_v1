import { useEffect, useState } from 'react';
import { Loader2, Database, RefreshCw } from 'lucide-react';
import { authFetch } from '@/api/authFetch';
import { toast } from 'sonner';

/**
 * Phase 6F — Enterprise Resources directory (Enterprise Resources area).
 *
 * Read-only inventory of the systems discovered during onboarding, from
 * the SAME authoritative Enterprise Map payload (no second store). Each
 * entry opens the existing Resource drilldown drawer (identity, onboarding
 * status + history, related business concepts). No credentials, no SQL,
 * no execution payloads.
 */

export default function ResourceDirectory({ onOpenResource }) {
  const [systems, setSystems] = useState(null);
  const [failed, setFailed] = useState(false);
  const [loading, setLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const res = await authFetch('/api/ontology/enterprise/map');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const body = await res.json();
      setSystems(body.systems || []);
    } catch (e) {
      toast.error(`Resources load failed: ${e.message || e}`);
      setFailed(true);
    } finally { setLoading(false); }
  };
  useEffect(() => { load(); /* eslint-disable-line react-hooks/exhaustive-deps */ }, []);

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <Database size={15} className="text-stone-600" />
        <h2 className="text-sm font-semibold text-stone-800">Enterprise Resources</h2>
        <span className="text-[11px] text-stone-400">
          the systems that support business decisions — click one for onboarding status and history
        </span>
        <button onClick={load}
          className="ml-auto flex items-center gap-1 text-[11px] px-2 py-1 rounded-md border border-stone-300 bg-white text-stone-600 hover:bg-stone-100">
          <RefreshCw size={11} className={loading ? 'animate-spin' : ''} /> Refresh
        </button>
      </div>

      {failed ? (
        <p className="text-[11px] text-stone-400">Resource inventory is not available right now.</p>
      ) : !systems ? (
        <p className="flex items-center gap-1.5 text-[11px] text-stone-400">
          <Loader2 size={12} className="animate-spin" /> Loading resources…</p>
      ) : systems.length === 0 ? (
        <p className="text-[11px] text-stone-400">
          No resources onboarded yet — connect a database or knowledge base to
          see it here with its onboarding status.
        </p>
      ) : (
        <div className="rounded-xl border border-stone-200 bg-white divide-y divide-stone-100">
          {systems.map((s) => (
            <button key={s.id} onClick={() => onOpenResource?.(s.ref_id || s.id)}
              className="w-full text-left px-4 py-2.5 hover:bg-stone-50 flex items-center gap-2 text-[11px]">
              <Database size={11} className="text-sky-600" />
              <span className="font-medium text-stone-800">{s.name}</span>
              <span className="text-[9px] text-stone-400 capitalize">{s.resource_kind}</span>
              <span className={`ml-auto text-[9px] px-1.5 py-0.5 rounded-full border ${
                s.evidence_status === 'verified'
                  ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
                  : 'border-stone-200 bg-stone-50 text-stone-500'}`}>
                {s.evidence_status || 'unavailable'}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
