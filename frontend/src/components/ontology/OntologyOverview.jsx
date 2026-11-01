import { useEffect, useState } from 'react';
import { authFetch } from '@/api/authFetch';
import ArchitectureFlow from './ArchitectureFlow';

/* Section A — Overview (default): the architecture principle plus five
   compact metrics, all computed from ACTUAL backend payloads. A metric
   that cannot be loaded shows "unavailable" — never a fabricated zero. */

function Metric({ value, label, hint, available = true }) {
  return (
    <div className="rounded-xl border border-stone-200 bg-white px-4 py-3">
      <div className={`text-[22px] font-semibold leading-none ${available ? 'text-stone-900' : 'text-stone-300'}`}>
        {available ? value : '—'}
      </div>
      <div className="mt-1.5 text-[11px] text-stone-500">
        {label}{!available && <span className="text-stone-400"> · unavailable</span>}
      </div>
      {hint && (
        <div className="mt-0.5 truncate text-[10px] text-stone-400" title={hint}>{hint}</div>
      )}
    </div>
  );
}

export default function OntologyOverview({ onLayerClick, overview, systems }) {
  const [enabledRules, setEnabledRules] = useState(null); // null = loading/unavailable

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await authFetch(
          '/api/ontology/enterprise/business-rules?status=enabled&page_size=1');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const body = await res.json();
        if (alive) setEnabledRules(body.total ?? 0);
      } catch {
        if (alive) setEnabledRules(null);
      }
    })();
    return () => { alive = false; };
  }, []);

  const ov = overview; // null while loading; false-ish → parent handles failure
  const totals = ov?.totals;
  const hasAny = totals && (totals.objects > 0 || totals.links > 0 ||
    (ov.bindings || []).length > 0 || (systems || []).length > 0);

  return (
    <div className="space-y-6">
      <ArchitectureFlow onLayerClick={onLayerClick} />

      {!hasAny && ov && (
        <p className="rounded-xl border border-dashed border-stone-300 bg-stone-50 px-4 py-3 text-[12px] text-stone-500">
          Nothing connected yet — onboard a data source and run discovery to
          populate the ontology.
        </p>
      )}

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
        <Metric value={(systems || []).length}
          label="Connected data sources"
          hint={(systems || []).slice(0, 3).map((s) => s.name).join(', ') || undefined}
          available={Array.isArray(systems)} />
        <Metric value={totals?.objects} label="Business objects"
          available={!!totals} />
        <Metric value={totals?.links} label="Object relationships"
          available={!!totals} />
        <Metric value={enabledRules} label="Governed business rules"
          hint="enabled rules that gate actions"
          available={enabledRules != null} />
        <Metric value={ov ? (ov.bindings || []).length : undefined}
          label="Connected AI agents"
          available={!!ov} />
      </div>

      {ov && (ov.domains || []).length > 0 && (
        <div className="rounded-xl border border-stone-200 bg-white px-4 py-3">
          <h3 className="text-[12px] font-medium text-stone-800">Business domains</h3>
          <div className="mt-2 flex flex-wrap gap-x-6 gap-y-1 text-[11px] text-stone-600">
            {ov.domains.map((d) => (
              <span key={d.id}>
                {d.name} <span className="text-stone-400">· {d.objects} object(s)</span>
                {d.coverage && d.coverage.objects_live > 0 && (
                  <span className="text-emerald-600"> · {d.coverage.objects_live} live</span>
                )}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
