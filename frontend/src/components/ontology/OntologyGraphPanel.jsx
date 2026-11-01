import { useEffect, useMemo, useState } from 'react';
import { Loader2, Link2 } from 'lucide-react';
import { authFetch } from '@/api/authFetch';
import { BusinessObjectPanel } from '@/components/enterprise/Drilldown';

/* Section B — Business Model: an interactive business ontology graph.

   Objects are grouped by domain in deterministic columns; relationships
   are drawn as bezier edges. Clicking an object opens the EXISTING
   BusinessObjectPanel right-side drawer (description, key attributes,
   related objects, data-source mapping, governance status) — no separate
   navigation pages. Layout is computed client-side from authoritative
   data only; nothing is inferred or fabricated. */

const NODE_W = 168;
const NODE_H = 52;
const COL_GAP = 96;
const ROW_GAP = 20;
const PAD = 8;

const EVIDENCE_DOT = {
  verified: 'bg-emerald-500',
  proposed: 'bg-stone-300',
  rejected: 'bg-red-400',
};

export default function OntologyGraphPanel({ overview, objects, links, failed, onOpenObject }) {
  const [selectedId, setSelectedId] = useState(null);
  const domains = overview?.domains || [];

  const byDomain = useMemo(() => {
    const obs = objects || [];
    const groups = domains.map((d) => ({
      domain: d, items: obs.filter((o) => o.domain_id === d.id),
    }));
    const orphans = obs.filter((o) => !domains.some((d) => d.id === o.domain_id));
    if (orphans.length) groups.push({ domain: null, items: orphans });
    return groups.filter((g) => g.items.length > 0);
  }, [domains, objects]);

  // deterministic node positions: one column per domain
  const positions = useMemo(() => {
    const pos = new Map();
    let x = PAD;
    for (const g of byDomain) {
      g.items.forEach((o, i) => {
        pos.set(o.id, { x, y: PAD + i * (NODE_H + ROW_GAP) });
      });
      x += NODE_W + COL_GAP;
    }
    return pos;
  }, [byDomain]);

  const graphW = Math.max(560, PAD * 2 + byDomain.length * NODE_W + (byDomain.length - 1) * COL_GAP);
  const graphH = PAD * 2 + Math.max(1, ...byDomain.map((g) => g.items.length)) * (NODE_H + ROW_GAP);

  const nameOf = (id) => objects.find((o) => o.id === id)?.name || id;

  if (failed) {
    return <p className="text-[12px] text-stone-400">The business model is not available right now.</p>;
  }
  if (!objects) {
    return <p className="flex items-center gap-1.5 text-[12px] text-stone-400">
      <Loader2 size={13} className="animate-spin" /> Loading business model…</p>;
  }
  if (objects.length === 0) {
    return <p className="text-[12px] text-stone-400">
      No business objects discovered yet — run discovery to build the model.</p>;
  }

  return (
    <div className="space-y-4">
      <div className="overflow-x-auto rounded-xl border border-stone-200 bg-white p-3">
        <div className="relative" style={{ width: graphW, height: graphH }}>
          {/* edges */}
          <svg className="absolute inset-0" width={graphW} height={graphH} aria-hidden="true">
            {links.map((l) => {
              const a = positions.get(l.from_object_id);
              const b = positions.get(l.to_object_id);
              if (!a || !b) return null;
              const x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2;
              const x2 = b.x, y2 = b.y + NODE_H / 2;
              const mx = (x1 + x2) / 2;
              const verified = l.evidence_status === 'verified';
              return (
                <path key={l.id} d={`M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`}
                  fill="none" strokeWidth={verified ? 1.6 : 1.1}
                  stroke={verified ? '#10b981' : '#d6d3d1'}
                  strokeDasharray={verified ? undefined : '4 3'}
                  data-testid={`edge-${l.id}`} />
              );
            })}
          </svg>
          {/* nodes + domain captions */}
          {byDomain.map((g) => {
            const first = positions.get(g.items[0].id);
            return (
              <div key={g.domain?.id || 'orphan'}
                className="absolute text-[10px] font-medium uppercase tracking-wide text-stone-400"
                style={{ left: first.x, top: 0 }}>
                {/* captions sit above the column, nodes shift below */}
              </div>
            );
          })}
          <div className="absolute" style={{ left: PAD, top: PAD, right: 0, bottom: 0 }}>
            {byDomain.map((g) => (
              g.items.map((o) => {
                const p = positions.get(o.id);
                const live = o.evidence_status === 'verified';
                return (
                  <button key={o.id}
                    onClick={() => { setSelectedId(o.id); onOpenObject?.(o.id); }}
                    className="absolute rounded-lg border bg-white px-3 py-2 text-left transition-colors hover:border-stone-400"
                    style={{
                      left: p.x - PAD, top: p.y - PAD + 14, width: NODE_W, height: NODE_H,
                      borderColor: live ? '#a7f3d0' : '#e7e5e4',
                    }}>
                    <span className="flex items-center gap-1.5">
                      <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${EVIDENCE_DOT[o.evidence_status] || 'bg-stone-200'}`} />
                      <span className="truncate text-[12px] font-medium text-stone-800">{o.name}</span>
                    </span>
                    <span className="mt-0.5 block truncate text-[10px] text-stone-400">
                      {o.backing_kind || 'concept'}
                    </span>
                  </button>
                );
              })
            ))}
          </div>
        </div>
      </div>

      {/* relationships list — click to inspect source and target */}
      <div className="rounded-xl border border-stone-200 bg-white px-4 py-3">
        <h3 className="flex items-center gap-1.5 text-[12px] font-medium text-stone-800">
          <Link2 size={12} className="text-stone-400" /> Relationships
        </h3>
        {links.length === 0 ? (
          <p className="mt-1 text-[11px] text-stone-400">No relationships discovered yet.</p>
        ) : (
          <div className="mt-2 space-y-1 text-[11px]">
            {links.map((l) => (
              <div key={l.id} className="flex items-center gap-1.5 text-stone-600">
                <button className="underline hover:text-stone-900"
                  onClick={() => { setSelectedId(l.from_object_id); onOpenObject?.(l.from_object_id); }}>
                  {nameOf(l.from_object_id)}
                </button>
                <span className="text-stone-400">{l.link_type || 'related to'}</span>
                <button className="underline hover:text-stone-900"
                  onClick={() => { setSelectedId(l.to_object_id); onOpenObject?.(l.to_object_id); }}>
                  {nameOf(l.to_object_id)}
                </button>
                <span className={`text-[9px] ${l.evidence_status === 'verified' ? 'text-emerald-600' : 'text-stone-400'}`}>
                  {l.evidence_status}
                </span>
                {l.cardinality && <span className="text-stone-300">{l.cardinality}</span>}
              </div>
            ))}
          </div>
        )}
      </div>

      {selectedId && (
        <BusinessObjectPanel objectId={selectedId}
          crumbs={[{ label: 'Enterprise Ontology' }, { label: 'Business object' }]}
          onClose={() => setSelectedId(null)} />
      )}
    </div>
  );
}
