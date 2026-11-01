import { useEffect, useMemo, useState, useCallback } from 'react';
import {
  ReactFlow, Background, Controls, MiniMap, Handle, Position,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { Loader2, RefreshCw, Boxes, Building2, Layers, Workflow, Database, Users, Zap } from 'lucide-react';
import { authFetch } from '@/api/authFetch';
import { toast } from 'sonner';

/**
 * Enterprise Map (EBPM Increment 4) — the primary landing experience.
 * One honest picture of the DISCOVERED business structure:
 * Enterprise → Org Units + Capabilities → Process Families → Processes,
 * plus authorized systems and ONLY persisted relationships
 * (process links + performer participation). No hardcoded industries,
 * departments or illustrative edges — every section can be honestly empty.
 */

const EVIDENCE_RING = {
  verified: '#059669',
  proposed: '#a8a29e',
  rejected: '#dc2626',
  inferred: '#d97706',
};
const DISCOVERY_FILL = {
  documented: '#f0f9ff',
  configured: '#f5f3ff',
  observed: '#ecfdf5',
};

function ProcessNode({ data }) {
  const ring = EVIDENCE_RING[data.evidence_status] || EVIDENCE_RING.proposed;
  return (
    <div className="rounded-lg border-2 px-3 py-2 shadow-sm text-[11px] max-w-[200px] cursor-pointer"
      style={{ borderColor: ring, background: DISCOVERY_FILL[data.discovery_status] || '#fafaf9' }}>
      <Handle type="target" position={Position.Left} style={{ opacity: 0 }} />
      <div className="font-medium text-stone-800">{data.label}</div>
      <div className="mt-0.5 text-[9px] text-stone-500 flex items-center gap-1.5 flex-wrap">
        <span>{data.activities_count} activities</span>
        <span className={`px-1 rounded border ${
          data.discovery_status === 'observed' ? 'border-emerald-200 text-emerald-700'
            : data.discovery_status === 'configured' ? 'border-violet-200 text-violet-700'
            : 'border-sky-200 text-sky-700'}`}>
          {data.discovery_status}
        </span>
        <span className="capitalize" style={{ color: ring }}>{data.evidence_status}</span>
      </div>
      <Handle type="source" position={Position.Right} style={{ opacity: 0 }} />
    </div>
  );
}

function GroupNode({ data }) {
  const Icon = data.icon || Layers;
  return (
    <div className="rounded-md px-2.5 py-1.5 text-[11px] font-medium"
      style={{ background: data.color || '#f5f5f4', color: '#44403c', border: '1px solid #e7e5e4' }}>
      <div className="flex items-center gap-1.5">
        <Icon size={10} />
        <span>{data.label}</span>
      </div>
      {data.sub && <div className="text-[9px] text-stone-400 mt-0.5">{data.sub}</div>}
    </div>
  );
}

function UnitNode({ data }) {
  return (
    <div className="rounded-lg border px-2.5 py-1.5 text-[10px] bg-white"
      style={{ borderColor: EVIDENCE_RING[data.evidence_status] || '#e7e5e4' }}>
      <div className="flex items-center gap-1 text-stone-700">
        <Users size={9} className="text-stone-400" />
        <span className="font-medium truncate max-w-[140px]">{data.label}</span>
      </div>
      <div className="text-[9px] text-stone-400 capitalize">{data.unit_type}</div>
    </div>
  );
}

const nodeTypes = { process: ProcessNode, group: GroupNode, unit: UnitNode };

const COL_X = { enterprise: 0, units: 260, capabilities: 260, families: 520, processes: 700, systems: 0 };

function layoutMap(map) {
  const nodes = [];
  const edges = [];
  const processes = map.processes || [];
  const units = map.org_units || [];
  const capabilities = map.capabilities || [];
  const systems = map.systems || [];

  // Enterprise root
  nodes.push({
    id: 'root', type: 'group', selectable: false, draggable: false,
    position: { x: COL_X.enterprise, y: 0 },
    data: { label: `Enterprise (${processes.length} discovered processes)`, icon: Building2, color: '#e7e5e4' },
  });

  // Org units (left column) — real discovered units only
  units.forEach((u, i) => {
    nodes.push({
      id: `unit:${u.id}`, type: 'unit',
      position: { x: COL_X.units - 120, y: 90 + i * 58 },
      data: { label: u.name, unit_type: u.unit_type, evidence_status: u.evidence_status },
    });
  });

  // Capabilities (left column, below units)
  const capTop = 90 + Math.max(units.length, 1) * 58 + 30;
  nodes.push({
    id: 'cap-header', type: 'group', selectable: false, draggable: false,
    position: { x: COL_X.capabilities - 120, y: capTop },
    data: { label: `Capabilities (${capabilities.length})`, icon: Layers },
  });
  capabilities.forEach((c, i) => {
    nodes.push({
      id: `cap:${c.id}`, type: 'group',
      position: { x: COL_X.capabilities - 120, y: capTop + 46 + i * 52 },
      data: { label: c.name, icon: Layers, sub: c.evidence_status },
    });
  });

  // Systems / authorized resources (below enterprise root)
  nodes.push({
    id: 'sys-header', type: 'group', selectable: false, draggable: false,
    position: { x: COL_X.enterprise, y: 90 },
    data: { label: `Authorized systems (${systems.length})`, icon: Database },
  });
  systems.slice(0, 8).forEach((s, i) => {
    nodes.push({
      id: `sys:${s.id}`, type: 'group',
      position: { x: COL_X.enterprise, y: 136 + i * 48 },
      data: { label: s.name, icon: Database, sub: s.resource_kind },
    });
  });

  // Process families → processes
  const fams = map.families || [];
  let y = 0;
  const procNodeIds = new Set();
  for (const fam of fams) {
    const famName = fam.family || 'Unassigned';
    nodes.push({
      id: `fam:${famName}`, type: 'group', selectable: false, draggable: false,
      position: { x: COL_X.families, y: y + 20 },
      data: { label: `${famName} (${fam.count})`, icon: Boxes, color: '#fef3c7' },
    });
    const members = processes.filter((p) => (p.process_family || 'Unassigned') === famName);
    members.forEach((p, i) => {
      const pid = `proc:${p.id}`;
      procNodeIds.add(pid);
      nodes.push({
        id: pid, type: 'process',
        position: { x: COL_X.processes, y: y + i * 84 },
        data: {
          label: p.name, processId: p.id,
          discovery_status: p.discovery_status,
          evidence_status: p.evidence_status,
          activities_count: p.activities_count,
        },
      });
      // performer participation edges — real activity performers only
      for (const ru of p.responsible_units || []) {
        const unit = units.find((u) => u.name === ru);
        if (unit) {
          edges.push({
            id: `perf:${p.id}:${unit.id}`,
            source: `unit:${unit.id}`, target: pid,
            style: { stroke: '#c7d2fe', strokeWidth: 1.2 },
            label: 'performs',
            labelStyle: { fontSize: 8, fill: '#6366f1' },
            labelBgStyle: { fill: '#eef2ff' },
          });
        }
      }
    });
    y += Math.max(members.length, 1) * 84 + 70;
  }

  // persisted process links — the ONLY process→process edges
  for (const l of map.process_links || []) {
    const s = `proc:${l.predecessor_id}`;
    const t = `proc:${l.successor_id}`;
    if (!procNodeIds.has(s) || !procNodeIds.has(t)) continue;
    edges.push({
      id: `link:${s}:${t}`, source: s, target: t,
      style: { stroke: '#a8a29e', strokeWidth: 1.4 },
      label: l.handoff_note || 'handoff',
      labelStyle: { fontSize: 8, fill: '#78716c' },
      labelBgStyle: { fill: '#fafaf9' },
      animated: false,
    });
  }

  return { nodes, edges };
}

const LEGEND = [
  { label: 'verified', ring: '#059669' },
  { label: 'proposed', ring: '#a8a29e' },
  { label: 'rejected', ring: '#dc2626' },
  { label: 'documented', fill: '#f0f9ff' },
  { label: 'configured', fill: '#f5f3ff' },
  { label: 'observed', fill: '#ecfdf5' },
];

export default function EnterpriseMap({ onOpenProcess, onRefreshed, onNavigate, canReview = false }) {
  const [map, setMap] = useState(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await authFetch('/api/ontology/enterprise/map');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setMap(await res.json());
      onRefreshed?.();
    } catch (e) {
      toast.error(`Enterprise map load failed: ${e.message || e}`);
      setMap({ org_units: [], capabilities: [], processes: [], families: [], systems: [], process_links: [] });
    } finally {
      setLoading(false);
    }
  }, [onRefreshed]);
  useEffect(() => { load(); }, [load]);

  const { nodes, edges } = useMemo(() => (map ? layoutMap(map) : { nodes: [], edges: [] }), [map]);
  const [selectedUnit, setSelectedUnit] = useState(null);

  if (loading && !map) {
    return (
      <div className="flex items-center justify-center py-20 text-stone-400 text-[12px]">
        <Loader2 size={16} className="animate-spin mr-2" /> Building the enterprise map…
      </div>
    );
  }

  const empty = !map || !(map.processes || []).length;
  const ov = map?.overview || null;

  return (
    <div>
      <div className="flex items-center gap-2">
        <Boxes size={15} className="text-stone-600" />
        <h2 className="text-sm font-semibold text-stone-800">Enterprise Map</h2>
        <span className="text-[11px] text-stone-400">
          discovered structure only — click a process to open its flow
        </span>
        <button onClick={load}
          className="ml-auto flex items-center gap-1 text-[11px] px-2 py-1 rounded-md border border-stone-300 bg-white text-stone-600 hover:bg-stone-100">
          <RefreshCw size={11} className={loading ? 'animate-spin' : ''} /> Refresh
        </button>
      </div>

      {ov && <OverviewPanels ov={ov} onNavigate={onNavigate} canReview={canReview}
        onOpenProcess={onOpenProcess} />}

      <div className="mt-2 flex items-center gap-3 flex-wrap text-[10px] text-stone-500">
        {LEGEND.map((l) => (
          <span key={l.label} className="inline-flex items-center gap-1">
            <span className="w-2.5 h-2.5 rounded-sm border-2"
              style={{ borderColor: l.ring || '#a8a29e', background: l.fill || 'transparent' }} />
            {l.label}
          </span>
        ))}
        <span className="text-stone-400">· edges = persisted links + performer participation only</span>
      </div>

      {/* Phase 6C: department drilldown — computed ONLY from the map
          payload (persisted performers/capabilities/systems); department
          association is contextual metadata, never an authorization grant. */}
      {selectedUnit && map && (() => {
        const unit = (map.org_units || []).find((u) => u.id === selectedUnit);
        if (!unit) { return null; } // deleted between load and click — honest no-op
        const deptProcesses = (map.processes || [])
          .filter((p) => (p.responsible_units || []).includes(unit.name));
        return (
          <div className="fixed inset-0 z-50 bg-black/30 flex justify-end"
            onClick={() => setSelectedUnit(null)}>
            <div className="w-full max-w-sm bg-white h-full shadow-2xl overflow-y-auto"
              onClick={(e) => e.stopPropagation()}>
              <div className="px-4 py-3 border-b border-stone-100">
                <div className="flex items-center gap-1 text-[10px] text-stone-400">
                  <span>Enterprise</span><span>/</span>
                  <span className="text-stone-600 font-medium">{unit.name}</span>
                  <button onClick={() => setSelectedUnit(null)}
                    className="ml-auto text-stone-400 hover:text-stone-600"><X size={14} /></button>
                </div>
                <h3 className="text-[13px] font-semibold text-stone-900 flex items-center gap-1.5">
                  <Users size={13} className="text-stone-500" /> {unit.name}
                </h3>
                <span className="text-[10px] text-stone-400 capitalize">{unit.unit_type}</span>
              </div>
              <div className="px-4 py-3 space-y-3 text-[11px]">
                <div>
                  <h4 className="text-[10px] uppercase tracking-wide text-stone-400 font-semibold">Responsible for processes</h4>
                  {deptProcesses.length === 0 ? (
                    <p className="text-stone-400 mt-1">No processes with this unit as an activity performer yet.</p>
                  ) : (
                    <div className="mt-1 space-y-1">
                      {deptProcesses.map((p) => (
                        <button key={p.id} className="underline text-stone-700 hover:text-stone-900 block"
                          onClick={() => { setSelectedUnit(null); onOpenProcess?.(p.id, true); }}>
                          {p.name}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
                <div>
                  <h4 className="text-[10px] uppercase tracking-wide text-stone-400 font-semibold">Business capabilities</h4>
                  {(map.capabilities || []).length === 0 ? (
                    <p className="text-stone-400 mt-1">No capabilities discovered yet.</p>
                  ) : (
                    <div className="mt-1 space-y-0.5">
                      {(map.capabilities || []).map((c) => (
                        <div key={c.id} className="text-stone-700">{c.name}</div>
                      ))}
                    </div>
                  )}
                </div>
                <div>
                  <h4 className="text-[10px] uppercase tracking-wide text-stone-400 font-semibold">Connected systems & resources</h4>
                  {(map.systems || []).length === 0 ? (
                    <p className="text-stone-400 mt-1">No resources onboarded yet.</p>
                  ) : (
                    <div className="mt-1 space-y-0.5">
                      {(map.systems || []).slice(0, 8).map((s) => (
                        <div key={s.id} className="text-stone-700 flex items-center gap-1.5">
                          <Database size={10} className="text-stone-400" />{s.name}
                          <span className="text-[9px] text-stone-400 capitalize">{s.resource_kind}</span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
                {map.overview?.observed?.available && (
                  <div>
                    <h4 className="text-[10px] uppercase tracking-wide text-stone-400 font-semibold">Execution evidence</h4>
                    <p className="mt-1 text-stone-600">
                      {map.overview.observed.total_executions} observed execution(s) across{' '}
                      {map.overview.observed.processes_with_observations} process(es) org-wide
                    </p>
                  </div>
                )}
              </div>
            </div>
          </div>
        );
      })()}

      {empty ? (
        <div className="mt-4 rounded-xl border border-dashed border-stone-300 bg-white px-6 py-12 text-center">
          <Workflow size={20} className="mx-auto text-stone-300" />
          <p className="mt-2 text-[13px] text-stone-500">Nothing discovered yet.</p>
          <p className="mt-1 text-[11px] text-stone-400">
            Run AI discovery from the Processes tab — org units, capabilities, processes and
            their evidence-backed structure appear here once proposed.
          </p>
        </div>
      ) : (
        <div className="mt-3 h-[560px] rounded-xl border border-stone-200 bg-white overflow-hidden">
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            nodesConnectable={false}
            fitView
            minZoom={0.15}
            onNodeClick={(_, node) => {
              if (node.id.startsWith('proc:')) onOpenProcess?.(node.data.processId, true);
              else if (node.id.startsWith('unit:')) setSelectedUnit(node.id.slice(5));
              else if (node.id.startsWith('cap:')) setSelectedUnit(null); // capability header — close panels
            }}
            onPaneClick={() => {}}
            proOptions={{ hideAttribution: true }}
          >
            <Background gap={18} color="#f5f5f4" />
            <Controls showInteractive={false} />
            <MiniMap pannable zoomable style={{ width: 130, height: 90 }} />
          </ReactFlow>
        </div>
      )}
    </div>
  );
}

/* ── Phase 6B: executive overview ─────────────────────────────────────
   Business-language, real-data-only KPI strip + execution/knowledge/
   attention panels. Missing sections render "unavailable", never zero.
   documented ≠ configured ≠ observed — never merged. */

function Kpi({ label, value, onClick, tone }) {
  const cls = 'rounded-lg border bg-white px-3 py-2 text-left ' +
    (onClick ? 'cursor-pointer hover:border-stone-400 ' : '') +
    (tone === 'warn' ? 'border-amber-200 ' : tone === 'ok' ? 'border-emerald-200 ' : 'border-stone-200');
  const body = (
    <>
      <div className="text-[16px] font-semibold text-stone-800">{value}</div>
      <div className="text-[10px] text-stone-500">{label}</div>
    </>
  );
  return onClick
    ? <button className={cls} onClick={onClick} title={`Open ${label}`}>{body}</button>
    : <div className={cls}>{body}</div>;
}

function OverviewPanels({ ov, onNavigate, canReview, onOpenProcess }) {
  const nav = (key) => (onNavigate ? () => onNavigate(key) : undefined);
  const p = ov.processes || {};
  const ob = ov.observed || { available: false };
  const k = ov.knowledge || { available: false };
  const at = ov.attention || { available: false };
  const withoutEvidence = ob.available && p.total != null
    ? Math.max(p.total - (ob.processes_with_observations || 0), 0) : null;

  return (
    <div className="mt-3 space-y-3">
      {/* KPI strip */}
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2">
        <Kpi label="Business processes" value={p.total ?? '—'}
          onClick={nav('F')} />
        <Kpi label="Verified" value={p.verified ?? '—'} tone="ok" onClick={nav('F')} />
        {canReview ? (
          <Kpi label="Awaiting review" value={p.awaiting_review ?? '—'}
            tone={(p.awaiting_review || 0) > 0 ? 'warn' : undefined}
            onClick={nav('R')} />
        ) : (
          <Kpi label="Awaiting review" value={p.awaiting_review ?? '—'}
            tone={(p.awaiting_review || 0) > 0 ? 'warn' : undefined} />
        )}
        <Kpi label="With execution evidence"
          value={ob.available ? (ob.processes_with_observations ?? '—') : '—'} />
        <Kpi label="Systems & resources"
          value={ov.resources_onboarded ?? '—'} />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
        {/* Execution overview */}
        <div className="rounded-xl border border-stone-200 bg-white px-4 py-3">
          <h3 className="text-[12px] font-semibold text-stone-800 flex items-center gap-1.5">
            <Zap size={12} className="text-emerald-600" /> Execution evidence
          </h3>
          {!ob.available ? (
            <p className="mt-1.5 text-[11px] text-stone-400">Execution data unavailable.</p>
          ) : (
            <div className="mt-2 space-y-1.5 text-[11px]">
              <div className="flex gap-6">
                <span className="text-stone-500">Observed executions: <b className="text-stone-800">{ob.total_executions}</b></span>
                {ob.latest_observed_at && (
                  <span className="text-stone-500">Latest: <b className="text-stone-800">{new Date(ob.latest_observed_at).toLocaleDateString()}</b></span>
                )}
              </div>
              <div className="flex items-center gap-3">
                {['high', 'medium', 'low'].map((band) => (
                  <span key={band} className="inline-flex items-center gap-1 text-[10px] text-stone-500">
                    <span className={`w-2 h-2 rounded-full ${band === 'high' ? 'bg-emerald-500' : band === 'medium' ? 'bg-amber-400' : 'bg-stone-300'}`} />
                    {band}: {ob.confidence_distribution?.[band] ?? 0}
                  </span>
                ))}
              </div>
              {withoutEvidence != null && (
                <p className="text-[10px] text-stone-400">
                  {withoutEvidence} processes without execution evidence — documented only, not (yet) observed.
                </p>
              )}
            </div>
          )}
        </div>

        {/* Knowledge coverage — business language, no ontology jargon */}
        <div className="rounded-xl border border-stone-200 bg-white px-4 py-3">
          <h3 className="text-[12px] font-semibold text-stone-800 flex items-center gap-1.5">
            <Layers size={12} className="text-sky-600" /> Business knowledge
          </h3>
          {!k.available ? (
            <p className="mt-1.5 text-[11px] text-stone-400">Knowledge data unavailable.</p>
          ) : (
            <div className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-[11px]">
              <KnowledgeRow label="Business objects" data={k.objects}
                render={(o) => `${o.total} · ${o.verified} verified${o.proposed ? ` · ${o.proposed} awaiting review` : ''}`} />
              <KnowledgeRow label="Connections" data={k.links} render={(n) => String(n)} />
              <KnowledgeRow label="Business terms" data={k.terminology} render={(n) => String(n)} />
              <KnowledgeRow label="Key metrics" data={k.metrics} render={(n) => String(n)} />
              <KnowledgeRow label="Business rules" data={k.rules}
                render={(r) => `${r.total} · ${r.enabled} active${r.proposed ? ` · ${r.proposed} awaiting review` : ''}`} />
            </div>
          )}
        </div>
      </div>

      {/* Attention signals — only categories backed by real rows */}
      {at.available && (at.processes_awaiting_review > 0 || at.knowledge_awaiting_review > 0 || at.partial_onboardings > 0) && (
        <div className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-2.5">
          <h3 className="text-[12px] font-semibold text-amber-800">Needs attention</h3>
          <div className="mt-1.5 flex flex-wrap gap-x-5 gap-y-1 text-[11px] text-amber-800">
            {at.processes_awaiting_review > 0 && (
              canReview
                ? <button className="underline hover:text-amber-900" onClick={nav('R')}>
                    {at.processes_awaiting_review} process proposal(s) awaiting review
                  </button>
                : <span>{at.processes_awaiting_review} process proposal(s) awaiting review</span>
            )}
            {at.knowledge_awaiting_review > 0 && (
              <span>{at.knowledge_awaiting_review} knowledge item(s) awaiting review</span>
            )}
            {at.partial_onboardings > 0 && (
              <span>{at.partial_onboardings} system(s) with failed or partial onboarding</span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function KnowledgeRow({ label, data, render }) {
  return (
    <div>
      <span className="text-stone-500">{label}: </span>
      <span className="text-stone-800 font-medium">
        {data == null ? 'unavailable' : render(data)}
      </span>
    </div>
  );
}
