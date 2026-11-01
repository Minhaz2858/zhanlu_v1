import { useEffect, useState } from 'react';
import { Loader2, X, Database, Boxes, Link2 } from 'lucide-react';
import { authFetch } from '@/api/authFetch';

/**
 * Enterprise drilldown panels (Phase 6C) — the Business Object → Resource
 → Evidence legs of the navigation chain. All data comes from the
 authoritative org-scoped drilldown APIs; missing/cross-org entities show
 an honest unavailable state (never fabricated). Execution trends
 aggregate REAL reference timestamps only — insufficient history stays
 "unavailable", never invented daily counts.
 */

const EVIDENCE_CLS = {
  verified: 'border-emerald-200 bg-emerald-50 text-emerald-700',
  proposed: 'border-stone-200 bg-stone-50 text-stone-500',
  rejected: 'border-red-200 bg-red-50 text-red-700',
  enabled: 'border-emerald-200 bg-emerald-50 text-emerald-700',
};

function StatusChip({ status }) {
  if (!status) return null;
  return (
    <span className={`text-[9px] px-1.5 py-0.5 rounded-full border capitalize ${EVIDENCE_CLS[status] || 'border-stone-200 bg-stone-50 text-stone-500'}`}>
      {status}
    </span>
  );
}

export function Breadcrumbs({ items, onCrumb }) {
  return (
    <div className="flex items-center gap-1 text-[10px] text-stone-400 flex-wrap">
      {items.map((c, i) => (
        <span key={i} className="flex items-center gap-1">
          {i > 0 && <span>/</span>}
          {onCrumb && c.onClick ? (
            <button onClick={c.onClick} className="underline hover:text-stone-600">{c.label}</button>
          ) : (
            <span className={i === items.length - 1 ? 'text-stone-600 font-medium' : ''}>{c.label}</span>
          )}
        </span>
      ))}
    </div>
  );
}

/** Pure daily aggregation of REAL reference timestamps. */
export function dailyTrend(references) {
  const refs = Array.isArray(references) ? references : [];
  const byDay = new Map();
  for (const r of refs) {
    if (!r?.observed_at) continue; // never invent a date
    const day = String(r.observed_at).slice(0, 10);
    byDay.set(day, (byDay.get(day) || 0) + 1);
  }
  const points = [...byDay.entries()]
    .sort(([a], [b]) => (a < b ? -1 : 1))
    .map(([day, count]) => ({ day, count }));
  return { available: points.length >= 2, points };
}

/** Compact execution trend — honest unavailable when history is thin. */
export function ExecutionTrend({ references }) {
  const trend = dailyTrend(references);
  if (!trend.available) {
    return (
      <p className="text-[10px] text-stone-400">
        Execution trend unavailable — not enough dated observations yet.
      </p>
    );
  }
  const max = Math.max(...trend.points.map((p) => p.count));
  return (
    <div className="flex items-end gap-1 h-8" title="Observed executions per day">
      {trend.points.map((p) => (
        <div key={p.day} className="flex flex-col items-center justify-end"
          title={`${p.day}: ${p.count} execution(s)`}>
          <div className="w-3 rounded-t bg-emerald-400"
            style={{ height: `${Math.max((p.count / max) * 24, 4)}px` }} />
          <span className="text-[7px] text-stone-400">{p.day.slice(5)}</span>
        </div>
      ))}
    </div>
  );
}

function PanelShell({ title, icon: Icon, crumbs, onClose, children }) {
  return (
    <div className="fixed inset-0 z-50 bg-black/30 flex justify-end"
      onClick={onClose}>
      <div className="w-full max-w-sm bg-white h-full shadow-2xl overflow-y-auto"
        onClick={(e) => e.stopPropagation()}>
        <div className="px-4 py-3 border-b border-stone-100 flex items-start gap-2">
          <div className="flex-1">
            {crumbs && <Breadcrumbs items={crumbs} />}
            <h3 className="text-[13px] font-semibold text-stone-900 flex items-center gap-1.5">
              {Icon && <Icon size={13} className="text-stone-500" />} {title}
            </h3>
          </div>
          <button onClick={onClose} className="text-stone-400 hover:text-stone-600">
            <X size={14} />
          </button>
        </div>
        <div className="px-4 py-3 space-y-3 text-[11px]">{children}</div>
      </div>
    </div>
  );
}

function Loading() {
  return (
    <div className="flex items-center gap-2 text-stone-400 py-6">
      <Loader2 size={13} className="animate-spin" /> Loading…
    </div>
  );
}

function Unavailable({ what }) {
  return (
    <div className="rounded-lg border border-dashed border-stone-300 bg-stone-50 px-3 py-4 text-stone-400">
      {what} is not available — it may have been removed or belongs to another organization.
    </div>
  );
}

function Row({ k, v }) {
  return (
    <div className="flex gap-2">
      <span className="text-[10px] uppercase tracking-wide text-stone-400 shrink-0 w-24">{k}</span>
      <span className="text-stone-700 break-words">{v}</span>
    </div>
  );
}

/** Business Object drilldown — authoritative OntologyObject summary. */
export function BusinessObjectPanel({ objectId, onClose, onOpenResource,
  onOpenProcess, onOpenObject, crumbs }) {
  const [data, setData] = useState(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    setData(null);
    setFailed(false);
    (async () => {
      try {
        const res = await authFetch(
          `/api/ontology/enterprise/business-objects/${objectId}`);
        if (!res.ok) { if (alive) setFailed(true); return; }
        const body = await res.json();
        if (alive) setData(body);
      } catch {
        if (alive) setFailed(true);
      }
    })();
    return () => { alive = false; };
  }, [objectId]);

  return (
    <PanelShell title={data?.name || 'Business object'} icon={Boxes}
      crumbs={crumbs} onClose={onClose}>
      {failed && <Unavailable what="This business object" />}
      {!failed && !data && <Loading />}
      {data && (
        <>
          <div className="flex items-center gap-2">
            <StatusChip status={data.evidence_status} />
            {data.domain && <span className="text-[10px] text-stone-400">Domain: {data.domain.name}</span>}
          </div>
          {data.description && <p className="text-stone-600">{data.description}</p>}

          <div>
            <h4 className="text-[10px] uppercase tracking-wide text-stone-400 font-semibold">Relationships</h4>
            {data.links.length === 0 ? (
              <p className="text-stone-400 mt-1">No relationships recorded.</p>
            ) : (
              <div className="mt-1 space-y-1">
                {data.links.map((l) => (
                  <div key={l.id} className="flex items-center gap-1.5">
                    <Link2 size={10} className="text-stone-400" />
                    <span className="text-stone-700">{l.link_type}</span>
                    <button className="underline text-stone-700 hover:text-stone-900"
                      onClick={() => onOpenObject?.(l.peer_id)}>
                      {l.peer_name}
                    </button>
                    <StatusChip status={l.evidence_status} />
                  </div>
                ))}
              </div>
            )}
          </div>

          <div>
            <h4 className="text-[10px] uppercase tracking-wide text-stone-400 font-semibold">Used in processes</h4>
            {data.processes.length === 0 ? (
              <p className="text-stone-400 mt-1">No process evidence references this object yet.</p>
            ) : (
              <div className="mt-1 space-y-1">
                {data.processes.map((p) => (
                  <div key={p.id} className="flex items-center gap-1.5">
                    <button className="underline text-stone-700 hover:text-stone-900"
                      onClick={() => onOpenProcess?.(p.id)}>{p.name}</button>
                    <StatusChip status={p.link_status} />
                  </div>
                ))}
              </div>
            )}
          </div>

          <div>
            <h4 className="text-[10px] uppercase tracking-wide text-stone-400 font-semibold">Backing resource</h4>
            {data.backing?.resource ? (
              <button className="mt-1 w-full rounded-lg border border-stone-200 bg-white px-3 py-2 flex items-center gap-2 hover:border-stone-400"
                onClick={() => onOpenResource?.(data.backing.resource.id)}>
                <Database size={12} className="text-sky-600" />
                <span className="font-medium text-stone-800">{data.backing.resource.name}</span>
                <span className="text-[9px] text-stone-400 capitalize">{data.backing.resource.kind}</span>
              </button>
            ) : (
              <p className="text-stone-400 mt-1">Not wired to a live resource yet — manual object.</p>
            )}
          </div>
        </>
      )}
    </PanelShell>
  );
}

/** Resource drilldown — identity + onboarding + related concepts.
    NEVER shows credentials, SQL or execution payloads. */
export function ResourcePanel({ refId, onClose, onOpenObject, crumbs }) {
  const [data, setData] = useState(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    setData(null);
    setFailed(false);
    (async () => {
      try {
        const res = await authFetch(
          `/api/ontology/enterprise/resources/${refId}`);
        if (!res.ok) { if (alive) setFailed(true); return; }
        const body = await res.json();
        if (alive) setData(body);
      } catch {
        if (alive) setFailed(true);
      }
    })();
    return () => { alive = false; };
  }, [refId]);

  return (
    <PanelShell title={data?.identity?.name || 'Resource'} icon={Database}
      crumbs={crumbs} onClose={onClose}>
      {failed && <Unavailable what="This resource" />}
      {!failed && !data && <Loading />}
      {data && (
        <>
          <div className="flex items-center gap-2">
            <StatusChip status={data.identity.kind} />
          </div>
          <div>
            <h4 className="text-[10px] uppercase tracking-wide text-stone-400 font-semibold">Onboarding</h4>
            {data.onboarding.status ? (
              <div className="mt-1 space-y-1">
                <Row k="Status" v={data.onboarding.status} />
                {data.onboarding.attempt != null && <Row k="Attempt" v={String(data.onboarding.attempt)} />}
                {data.onboarding.finished_at && <Row k="When" v={new Date(data.onboarding.finished_at).toLocaleString()} />}
                {data.onboarding.error && <Row k="Last error" v={data.onboarding.error} />}
              </div>
            ) : (
              <p className="text-stone-400 mt-1">Never onboarded — no onboarding runs recorded.</p>
            )}
          </div>
          <div>
            <h4 className="text-[10px] uppercase tracking-wide text-stone-400 font-semibold">Related business concepts</h4>
            {data.related_objects.length === 0 ? (
              <p className="text-stone-400 mt-1">No business objects backed by this resource yet.</p>
            ) : (
              <div className="mt-1 space-y-1">
                {data.related_objects.map((o) => (
                  <div key={o.id} className="flex items-center gap-1.5">
                    <button className="underline text-stone-700 hover:text-stone-900"
                      onClick={() => onOpenObject?.(o.id)}>{o.name}</button>
                    <StatusChip status={o.evidence_status} />
                  </div>
                ))}
              </div>
            )}
          </div>
        </>
      )}
    </PanelShell>
  );
}
