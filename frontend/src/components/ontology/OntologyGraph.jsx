import { useEffect, useMemo, useState, useCallback } from 'react';
import {
  ReactFlow, Background, Controls, MiniMap, Handle, Position,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { Loader2, RefreshCw, Network, X, ShieldCheck, Database } from 'lucide-react';
import { authFetch } from '@/api/authFetch';
import { toast } from 'sonner';

/**
 * Ontology Graph Explorer (P7) — one honest picture of the org's semantic
 * layer: objects clustered by domain, links as edges, per-viewer document
 * evidence, binding health (P6) and governed metric definitions (P5).
 *
 * Self-contained: no dependency on local style libs; evidence/lifecycle
 * styles are inlined below and rendered AS-IS (no invented states).
 */

const EVIDENCE_STYLE = {
  verified: { border: '#059669', bg: '#ecfdf5' },
  live: { border: '#059669', bg: '#ecfdf5' },
  documented: { border: '#0284c7', bg: '#f0f9ff' },
  inferred: { border: '#d97706', bg: '#fffbeb' },
  proposed: { border: '#a8a29e', bg: '#fafaf9' },
};

const LIFECYCLE_STYLE = {
  published: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  draft: 'bg-amber-50 text-amber-700 border-amber-200',
  retired: 'bg-stone-100 text-stone-500 border-stone-200',
};

const HEALTH_STYLE = {
  broken: 'bg-red-50 text-red-700 border-red-200',
  stale: 'bg-amber-50 text-amber-700 border-amber-200',
  ok: 'bg-emerald-50 text-emerald-700 border-emerald-200',
};

const DOMAIN_COLORS = ['#6366f1', '#0ea5e9', '#10b981', '#f59e0b', '#ec4899', '#8b5cf6', '#14b8a6'];

function ObjectNode({ data }) {
  const st = EVIDENCE_STYLE[data.evidence] || EVIDENCE_STYLE.proposed;
  return (
    <div
      className="rounded-lg border px-3 py-2 shadow-sm text-[11px] max-w-[170px]"
      style={{ borderColor: st.border, background: st.bg }}
    >
      <Handle type="target" position={Position.Left} style={{ opacity: 0 }} />
      <div className="font-semibold text-stone-800 truncate">{data.label}</div>
      <div className="text-[10px] text-stone-500 flex items-center gap-1 mt-0.5">
        <Database size={9} />
        <span className="truncate">{data.backing || 'unwired'}</span>
      </div>
      {data.health && data.health !== 'ok' && (
        <div className={`mt-1 inline-block text-[9px] px-1 rounded border ${HEALTH_STYLE[data.health]}`}>
          {data.health}
        </div>
      )}
      <Handle type="source" position={Position.Right} style={{ opacity: 0 }} />
    </div>
  );
}

const nodeTypes = { object: ObjectNode };

function layoutGraph(payload) {
  const domains = payload.domains || [];
  const objects = payload.objects || [];
  const byDomain = {};
  for (const o of objects) {
    (byDomain[o.domain_id] = byDomain[o.domain_id] || []).push(o);
  }
  const domainById = Object.fromEntries(domains.map((d) => [d.id, d]));
  const nodes = [];
  const edges = [];

  const domainIds = Object.keys(byDomain);
  const cols = Math.ceil(Math.sqrt(Math.max(domainIds.length, 1)));
  const CELL_W = 420;
  const CELL_H = 380;

  domainIds.forEach((domId, di) => {
    const color = DOMAIN_COLORS[di % DOMAIN_COLORS.length];
    const ox = (di % cols) * CELL_W;
    const oy = Math.floor(di / cols) * CELL_H;
    const dom = domainById[domId] || { name: 'unknown', code: '?' };

    nodes.push({
      id: `domain:${domId}`,
      position: { x: ox, y: oy },
      data: { label: `${dom.name} (${byDomain[domId].length})` },
      selectable: false,
      draggable: false,
      connectable: false,
      style: {
        background: color, color: '#fff', border: 'none',
        borderRadius: 8, padding: '4px 10px', fontSize: 11,
        fontWeight: 600, width: 'auto',
      },
    });

    const objs = byDomain[domId];
    objs.forEach((o, oi) => {
      const perRow = 3;
      nodes.push({
        id: `obj:${o.id}`,
        type: 'object',
        position: {
          x: ox + 20 + (oi % perRow) * 130,
          y: oy + 50 + Math.floor(oi / perRow) * 76,
        },
        data: {
          label: o.name,
          evidence: o.evidence_status,
          backing: o.backing_kind === 'table'
            ? (o.backing_ref || '').split(':').slice(2).join(':') || 'table'
            : o.backing_kind,
          health: o.binding_status || null,
        },
      });
    });
  });

  const objNodeIds = new Set(
    (payload.objects || []).map((o) => `obj:${o.id}`));
  for (const l of payload.links || []) {
    const s = `obj:${l.from_object_id}`;
    const t = `obj:${l.to_object_id}`;
    if (!objNodeIds.has(s) || !objNodeIds.has(t)) continue;
    edges.push({
      id: `e:${l.id || `${l.from_object_id}->${l.to_object_id}`}`,
      source: s, target: t,
      label: l.link_type || '',
      style: { stroke: '#d6d3d1' },
      labelStyle: { fontSize: 9, fill: '#78716c' },
      labelBgStyle: { fill: '#fafaf9' },
    });
  }
  return { nodes, edges };
}

export default function OntologyGraph() {
  const [payload, setPayload] = useState(null);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState(null);
  const [showMetrics, setShowMetrics] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await authFetch('/api/ontology/graph');
      if (!res.ok) throw new Error(`graph ${res.status}`);
      setPayload(await res.json());
    } catch (e) {
      toast.error(`Graph load failed: ${e.message || e}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const { nodes, edges } = useMemo(
    () => (payload ? layoutGraph(payload) : { nodes: [], edges: [] }),
    [payload]);

  const selectedObj = useMemo(() => {
    if (!selected || !payload) return null;
    return (payload.objects || []).find((o) => o.id === selected) || null;
  }, [selected, payload]);

  const docEvidence = useMemo(() => {
    const dp = selectedObj?.provenance?.document_provenance;
    return Array.isArray(dp) ? dp : [];
  }, [selectedObj]);

  if (loading && !payload) {
    return (
      <div className="flex items-center justify-center py-16 text-stone-400 text-[12px]">
        <Loader2 size={16} className="animate-spin mr-2" /> Building graph…
      </div>
    );
  }

  const empty = !payload || !(payload.objects || []).length;
  const metrics = payload?.metrics || [];

  return (
    <div>
      <div className="flex items-center gap-2">
        <Network size={15} className="text-stone-600" />
        <h2 className="text-sm font-semibold text-stone-800">Ontology Graph</h2>
        <span className="text-[11px] text-stone-400">
          objects + links + binding health — click a node for detail
        </span>
        <div className="ml-auto flex items-center gap-2">
          {metrics.length > 0 && (
            <button
              onClick={() => setShowMetrics((v) => !v)}
              className="text-[11px] px-2 py-1 rounded-md border border-stone-300 bg-white text-stone-600 hover:bg-stone-100"
            >
              {showMetrics ? 'Hide' : 'Show'} governed metrics ({metrics.length})
            </button>
          )}
          <button
            onClick={load}
            className="flex items-center gap-1 text-[11px] px-2 py-1 rounded-md border border-stone-300 bg-white text-stone-600 hover:bg-stone-100"
          >
            <RefreshCw size={11} className={loading ? 'animate-spin' : ''} /> Refresh
          </button>
        </div>
      </div>

      {showMetrics && metrics.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-2">
          {metrics.map((m) => (
            <div
              key={m.id}
              className="flex items-center gap-1.5 rounded-lg border border-stone-200 bg-white px-2 py-1 text-[10px]"
              title={`${m.aggregation || 'agg'} of ${m.base_table || '?'} · v${m.version} · datasource ${m.datasource_id}`}
            >
              <ShieldCheck size={10} className="text-stone-400" />
              <span className="font-medium text-stone-700">{m.name}</span>
              <span className={`px-1 rounded border ${LIFECYCLE_STYLE[m.lifecycle] || LIFECYCLE_STYLE.retired}`}>
                {m.lifecycle}
              </span>
              <span className="text-stone-400">v{m.version}</span>
              {m.reviewed_by && <span className="text-stone-400">reviewed</span>}
            </div>
          ))}
        </div>
      )}

      {empty ? (
        <div className="mt-3 rounded-xl border border-dashed border-stone-300 bg-white py-14 text-center text-[12px] text-stone-400">
          No ontology objects yet — run auto-discovery above to seed the graph.
        </div>
      ) : (
        <div className="mt-3 h-[480px] rounded-xl border border-stone-200 bg-white overflow-hidden">
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            fitView
            minZoom={0.2}
            onNodeClick={(_, node) => {
              if (node.id.startsWith('obj:')) setSelected(node.id.slice(4));
            }}
            onPaneClick={() => setSelected(null)}
            proOptions={{ hideAttribution: true }}
          >
            <Background gap={18} color="#f5f5f4" />
            <Controls showInteractive={false} />
            <MiniMap pannable zoomable style={{ width: 130, height: 90 }} />
          </ReactFlow>
        </div>
      )}

      {selectedObj && (
        <div className="fixed inset-y-0 right-0 z-50 w-[380px] max-w-full bg-white border-l border-stone-200 shadow-2xl flex flex-col">
          <div className="flex items-center gap-2 px-4 py-3 border-b border-stone-100">
            <h3 className="text-[13px] font-semibold text-stone-800 truncate">
              {selectedObj.name}
            </h3>
            <span className={`text-[10px] px-1.5 rounded border ${HEALTH_STYLE[selectedObj.binding_status] || 'bg-stone-50 text-stone-500 border-stone-200'}`}>
              {selectedObj.binding_status || 'unvalidated'}
            </span>
            <button
              onClick={() => setSelected(null)}
              className="ml-auto text-stone-400 hover:text-stone-600"
            >
              <X size={15} />
            </button>
          </div>
          <div className="flex-1 overflow-y-auto px-4 py-3 space-y-3 text-[11px]">
            <Row k="Evidence" v={selectedObj.evidence_status} />
            {selectedObj.confidence != null && (
              <Row k="Confidence" v={String(selectedObj.confidence)} />
            )}
            <Row k="Backing" v={`${selectedObj.backing_kind} · ${selectedObj.backing_ref || '—'}`} />
            {selectedObj.binding_status && (
              <Row k="Last validated" v={selectedObj.last_validated_at || '—'} />
            )}
            {selectedObj.description && (
              <Row k="Description" v={selectedObj.description} />
            )}
            <div>
              <div className="text-[10px] uppercase tracking-wide text-stone-400 mb-1">
                Properties
              </div>
              {(selectedObj.properties || []).length ? (
                <div className="flex flex-wrap gap-1">
                  {selectedObj.properties.map((p, i) => (
                    <span key={i} className="px-1.5 py-0.5 rounded bg-stone-100 text-stone-600">
                      {p.name}{p.column ? ` → ${p.column}` : ''}
                    </span>
                  ))}
                </div>
              ) : (
                <span className="text-stone-400">none mapped</span>
              )}
            </div>
            <div>
              <div className="text-[10px] uppercase tracking-wide text-stone-400 mb-1">
                Document evidence ({docEvidence.length})
              </div>
              {docEvidence.length ? docEvidence.map((d, i) => (
                <div key={i} className="rounded-lg border border-sky-100 bg-sky-50/50 px-2 py-1.5 mb-1">
                  <div className="text-stone-700 italic">“{d.quote}”</div>
                  <div className="text-[10px] text-stone-400 mt-0.5">
                    {d.location || ''}{d.doc_version_id ? ` · ${d.doc_version_id}` : ''}
                  </div>
                </div>
              )) : (
                <span className="text-stone-400">
                  none visible to you (per-viewer, fail-closed)
                </span>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function Row({ k, v }) {
  return (
    <div className="flex gap-2">
      <span className="text-[10px] uppercase tracking-wide text-stone-400 shrink-0 w-24">{k}</span>
      <span className="text-stone-700 break-all">{v}</span>
    </div>
  );
}
