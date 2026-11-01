import { useEffect, useMemo, useState, useCallback } from 'react';
import {
  ReactFlow, Background, Controls, MiniMap, Handle, Position,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { Loader2, X, FileSearch, GitBranch, User, Cpu, GitFork, Flag, Zap } from 'lucide-react';
import { authFetch } from '@/api/authFetch';
import { toast } from 'sonner';
import { activityObservation, confidenceLabel } from './observedExecution';

/**
 * ProcessFlowCanvas (EBPM Increment 4) — interactive swimlane visualization
 * of ONE discovered process. Lanes are generated from ACTUAL activity
 * performers (no invented actors); nodes are the persisted activities; the
 * only edges are the persisted activity sequence and persisted cross-process
 * links. Evidence status (verified/proposed/rejected) and discovery status
 * (documented/configured/observed) are rendered AS-IS, never conflated.
 */

const EVIDENCE_RING = {
  verified: '#059669',
  proposed: '#a8a29e',
  rejected: '#dc2626',
  inferred: '#d97706',
};
const DISCOVERY_LABEL = {
  documented: { text: 'documented', cls: 'border-sky-200 bg-sky-50 text-sky-700' },
  configured: { text: 'configured', cls: 'border-violet-200 bg-violet-50 text-violet-700' },
  observed: { text: 'observed', cls: 'border-emerald-200 bg-emerald-50 text-emerald-700' },
};
const TYPE_META = {
  start_event: { label: 'Start', color: '#059669', bg: '#ecfdf5' },
  end_event: { label: 'End', color: '#57534e', bg: '#f5f5f4' },
  human_task: { label: 'Human', color: '#4f46e5', bg: '#eef2ff' },
  system_task: { label: 'System', color: '#0284c7', bg: '#f0f9ff' },
  decision: { label: 'Decision', color: '#d97706', bg: '#fffbeb' },
  approval: { label: 'Approval', color: '#7c3aed', bg: '#f5f3ff' },
  subprocess: { label: 'Subprocess', color: '#0d9488', bg: '#f0fdfa' },
};

function ActivityNode({ data }) {
  const meta = TYPE_META[data.activity_type] || TYPE_META.human_task;
  const ring = EVIDENCE_RING[data.evidence_status] || EVIDENCE_RING.proposed;
  return (
    <div className="rounded-lg border-2 px-3 py-2 shadow-sm text-[11px] max-w-[190px]"
      style={{ borderColor: ring, background: meta.bg }}>
      <Handle type="target" position={Position.Left} style={{ opacity: 0 }} />
      <div className="font-medium text-stone-800">{data.label}</div>
      <div className="mt-0.5 flex items-center gap-1 text-[9px] text-stone-500">
        {data.performer ? (
          <><User size={9} /><span className="truncate max-w-[150px]">{data.performer}</span></>
        ) : (
          <><Cpu size={9} /><span>unassigned</span></>
        )}
      </div>
      {data.type_label && (
        <div className="mt-0.5 text-[9px] font-medium" style={{ color: meta.color }}>
          {data.type_label}
        </div>
      )}
      {/* Phase 6A: OBSERVED dimension — independent of documented/configured.
          Compact indicator only: executions matched this activity. Absent
          badge = no execution evidence (never implied from documentation). */}
      {data.observed?.observed && (
        <div className="mt-0.5 flex items-center gap-1 text-[9px] font-medium text-emerald-700"
          title={`${data.observed.count} observed execution(s), ${confidenceLabel(data.observed.band)}`}>
          <Zap size={9} />
          <span>{data.observed.count}×</span>
        </div>
      )}
      <Handle type="source" position={Position.Right} style={{ opacity: 0 }} />
    </div>
  );
}

function PeerNode({ data }) {
  return (
    <div className="rounded-lg border-2 border-dashed px-3 py-2 text-[11px] max-w-[160px] bg-white"
      style={{ borderColor: '#a8a29e' }}>
      <Handle type="target" position={Position.Left} style={{ opacity: 0 }} />
      <div className="flex items-center gap-1 text-stone-600">
        <GitFork size={9} />
        <span className="font-medium truncate">{data.label}</span>
      </div>
      <div className="text-[9px] text-stone-400">related process — click to open</div>
      <Handle type="source" position={Position.Right} style={{ opacity: 0 }} />
    </div>
  );
}

function LaneLabelNode({ data }) {
  return (
    <div className="flex items-center gap-1.5 rounded-md px-2 py-1 text-[11px] font-medium text-stone-600"
      style={{ background: '#fafaf9', border: '1px solid #e7e5e4' }}>
      <Flag size={10} className="text-stone-400" />
      <span className="truncate max-w-[170px]">{data.label}</span>
    </div>
  );
}

const nodeTypes = { activity: ActivityNode, peer: PeerNode, laneLabel: LaneLabelNode };

const LANE_H = 150;
const NODE_W = 210;
const NODE_H = 76;

function layoutFlow(detail) {
  const activities = detail.activities || [];
  const lanes = [];
  for (const a of activities) {
    const lane = a.performer || 'Unassigned';
    if (!lanes.includes(lane)) lanes.push(lane);
  }
  const laneY = Object.fromEntries(lanes.map((l, i) => [l, i * LANE_H + 40]));
  const seqToNode = {};
  const nodes = [];
  const edges = [];

  lanes.forEach((lane, i) => {
    nodes.push({
      id: `lane:${lane}`, type: 'laneLabel', selectable: false, draggable: false,
      position: { x: -10, y: i * LANE_H + 48 },
      data: { label: lane }, zIndex: 1,
    });
  });

  const sorted = [...activities].sort((a, b) => a.seq - b.seq);
  const summary = detail.observed_executions || null;
  const refs = detail.observation_references || null;
  sorted.forEach((a, i) => {
    const lane = a.performer || 'Unassigned';
    const id = `act:${a.seq}:${a.name}`;
    seqToNode[a.seq] = id;
    nodes.push({
      id, type: 'activity',
      position: { x: 240 + i * NODE_W, y: (laneY[lane] ?? 0) },
      data: {
        label: a.name, performer: a.performer || null,
        activity_type: a.activity_type,
        type_label: (TYPE_META[a.activity_type] || TYPE_META.human_task).label,
        evidence_status: a.evidence_status || 'inferred',
        activity: a,
        observed: activityObservation(summary, refs, a.seq),
      },
    });
  });

  // persisted sequence edges (consecutive by seq)
  for (let i = 0; i < sorted.length - 1; i++) {
    const s = seqToNode[sorted[i].seq];
    const t = seqToNode[sorted[i + 1].seq];
    if (s && t) {
      edges.push({
        id: `seq:${i}`, source: s, target: t,
        style: { stroke: '#d6d3d1', strokeWidth: 1.5 },
      });
    }
  }

  // related processes — ONLY persisted links (dashed, navigable)
  (detail.related_processes || []).forEach((r, i) => {
    if (!r.peer_id) return;
    const pid = `peer:${r.peer_id}`;
    const last = sorted[sorted.length - 1];
    const anchor = last ? seqToNode[last.seq] : null;
    nodes.push({
      id: pid, type: 'peer',
      position: {
        x: 240 + sorted.length * NODE_W,
        y: i * 90 + (r.direction === 'predecessor' ? 0 : 40),
      },
      data: { label: r.peer_name || r.peer_id, peerId: r.peer_id,
              direction: r.direction },
    });
    if (anchor) {
      edges.push({
        id: `peer:${i}`,
        source: r.direction === 'predecessor' ? pid : anchor,
        target: r.direction === 'predecessor' ? anchor : pid,
        style: { stroke: '#a8a29e', strokeDasharray: '5 3' },
        label: r.direction === 'predecessor' ? 'comes before' : 'hands off to',
        labelStyle: { fontSize: 9, fill: '#78716c' },
        labelBgStyle: { fill: '#fafaf9' },
      });
    }
  });

  return { nodes, edges, lanes };
}

export default function ProcessFlowCanvas({ processId, onClose, onOpenDetail }) {
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState(null);
  const [selectedAct, setSelectedAct] = useState(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const res = await authFetch(`/api/ontology/enterprise/processes/${processId}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setDetail(await res.json());
    } catch (e) { setError(e.message || String(e)); }
  }, [processId]);
  useEffect(() => { load(); }, [load]);

  // Phase 6A: ONE observation request per canvas — and ONLY when the
  // detail summary says executions exist (zero-observation processes
  // never pay the cost). Failure degrades to the detail summary (fail-open).
  useEffect(() => {
    if (!detail || !detail.observed_executions?.execution_count) return;
    let alive = true;
    (async () => {
      try {
        const res = await authFetch(
          `/api/ontology/enterprise/processes/${processId}/observations`);
        if (!res.ok) return; // references unavailable — summary still shown
        const data = await res.json();
        if (alive && data?.references) {
          setDetail((d) => (d ? { ...d, observation_references: data.references } : d));
        }
      } catch { /* fail-open: references unavailable */ }
    })();
    return () => { alive = false; };
  }, [detail?.observed_executions?.execution_count, processId]); // eslint-disable-line react-hooks/exhaustive-deps

  const { nodes, edges, lanes } = useMemo(
    () => (detail ? layoutFlow(detail) : { nodes: [], edges: [], lanes: [] }),
    [detail]);

  const ds = detail ? (DISCOVERY_LABEL[detail.discovery_status] || DISCOVERY_LABEL.documented) : null;
  const peers = detail?.related_processes || [];
  const actObs = selectedAct && detail
    ? activityObservation(detail.observed_executions,
        detail.observation_references, selectedAct.seq)
    : null;

  return (
    <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-6"
      onClick={onClose}>
      <div className="w-full h-full max-w-6xl bg-white rounded-xl shadow-2xl flex flex-col overflow-hidden"
        onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-2 px-5 py-3 border-b border-stone-200">
          <GitBranch size={15} className="text-stone-600" />
          <h2 className="text-[14px] font-semibold text-stone-900 truncate">
            {detail ? detail.name : 'Process flow'}
          </h2>
          {ds && (
            <span className={`text-[10px] px-1.5 py-0.5 rounded-full border ${ds.cls}`}>
              {ds.text}
            </span>
          )}
          {detail && (
            <span className={`text-[10px] px-1.5 py-0.5 rounded-full border capitalize ${
              detail.evidence_status === 'verified'
                ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
                : detail.evidence_status === 'rejected'
                  ? 'border-red-200 bg-red-50 text-red-700'
                  : 'border-stone-200 bg-stone-50 text-stone-500'}`}>
              {detail.evidence_status}
            </span>
          )}
          <div className="ml-auto flex items-center gap-2">
            {detail && (
              <button onClick={() => onOpenDetail?.(detail.id)}
                className="flex items-center gap-1 text-[11px] px-2.5 py-1.5 rounded-md border border-stone-300 bg-white text-stone-600 hover:bg-stone-100">
                <FileSearch size={11} /> Process detail
              </button>
            )}
            <button onClick={onClose} className="text-stone-400 hover:text-stone-700">
              <X size={16} />
            </button>
          </div>
        </div>

        {peers.length > 0 && (
          <div className="flex items-center gap-2 px-5 py-2 border-b border-stone-100 bg-stone-50 flex-wrap">
            <span className="text-[10px] uppercase tracking-wide text-stone-400">Related:</span>
            {peers.map((r, i) => (
              <button key={i} onClick={() => onOpenDetail?.(r.peer_id, true)}
                className="text-[10px] px-2 py-0.5 rounded-full border border-stone-200 bg-white text-stone-600 hover:border-stone-400"
                title={`Open ${r.peer_name || r.peer_id} in the flow canvas`}>
                {r.direction === 'predecessor' ? '← ' : '→ '}{r.peer_name || r.peer_id}
              </button>
            ))}
          </div>
        )}

        {error && (
          <div className="m-5 text-[12px] text-red-600 bg-red-50 border border-red-200 rounded-md px-3 py-2">
            Failed to load process flow: {error}
          </div>
        )}
        {!detail && !error && (
          <div className="flex items-center justify-center flex-1 text-stone-400">
            <Loader2 size={16} className="animate-spin mr-2" /> Building flow…
          </div>
        )}
        {detail && (
          <div className="flex-1 relative">
            <ReactFlow
              nodes={nodes}
              edges={edges}
              nodeTypes={nodeTypes}
              nodesConnectable={false}
              fitView
              minZoom={0.2}
              onNodeClick={(_, node) => {
                if (node.id.startsWith('act:')) setSelectedAct(node.data.activity);
                else if (node.id.startsWith('peer:')) onOpenDetail?.(node.data.peerId, true);
                else setSelectedAct(null);
              }}
              onPaneClick={() => setSelectedAct(null)}
              proOptions={{ hideAttribution: true }}
            >
              <Background gap={18} color="#f5f5f4" />
              <Controls showInteractive={false} />
              <MiniMap pannable zoomable style={{ width: 130, height: 90 }} />
            </ReactFlow>

            {/* Activity detail panel */}
            {selectedAct && (
              <div className="absolute right-4 top-4 bottom-4 w-72 bg-white border border-stone-200 rounded-xl shadow-xl overflow-y-auto z-10">
                <div className="flex items-center gap-2 px-3 py-2.5 border-b border-stone-100">
                  <h3 className="text-[12px] font-semibold text-stone-800 truncate">
                    {selectedAct.name}
                  </h3>
                  <button onClick={() => setSelectedAct(null)}
                    className="ml-auto text-stone-400 hover:text-stone-600">
                    <X size={13} />
                  </button>
                </div>
                <div className="px-3 py-2.5 space-y-2 text-[11px]">
                  <Row k="Seq" v={String(selectedAct.seq)} />
                  <Row k="Type" v={(TYPE_META[selectedAct.activity_type] || TYPE_META.human_task).label} />
                  <Row k="Performer" v={selectedAct.performer || 'unassigned'} />
                  <Row k="Evidence" v={selectedAct.evidence_status || 'inferred'} />
                  {selectedAct.business_rules && <Row k="Business rules" v={selectedAct.business_rules} />}
                  {selectedAct.exception_paths && <Row k="Exception paths" v={selectedAct.exception_paths} />}
                  {selectedAct.provenance?.evidence_ids?.length > 0 && (
                    <Row k="Evidence refs" v={`${selectedAct.provenance.evidence_ids.length} extracted item(s)`} />
                  )}
                  {!selectedAct.business_rules && !selectedAct.exception_paths && (
                    <p className="text-stone-400">No rules or exception paths discovered for this activity.</p>
                  )}
                  {/* Phase 6A: OBSERVED dimension — shown SEPARATELY from
                      the documented/configured statuses above. Honest empty
                      state: no observations ≠ never executed, just no
                      matched execution evidence. References are payload-free
                      (no SQL, results, prompts, or credentials). */}
                  <div className="border-t border-stone-100 pt-2 mt-2">
                    <div className="flex items-center gap-1 text-[10px] uppercase tracking-wide text-emerald-700 font-semibold">
                      <Zap size={10} /> Observed executions
                    </div>
                    {actObs.observed ? (
                      <div className="mt-1 space-y-1">
                        <Row k="Executions" v={String(actObs.count)} />
                        <Row k="Confidence" v={`${actObs.max_confidence ?? '—'} (${confidenceLabel(actObs.band)})`} />
                        {actObs.latest_observed_at && (
                          <Row k="Latest" v={new Date(actObs.latest_observed_at).toLocaleString()} />
                        )}
                        {actObs.methods.length > 0 && (
                          <Row k="Linkage" v={actObs.methods.join(', ')} />
                        )}
                        {actObs.record_types.length > 0 && (
                          <Row k="References" v={`${actObs.record_types.join(', ')} — payload-free`} />
                        )}
                      </div>
                    ) : (
                      <p className="mt-1 text-stone-400">
                        No matched executions for this activity yet — documented only, not (yet) observed.
                      </p>
                    )}
                  </div>
                </div>
              </div>
            )}

            {lanes.length === 0 && (
              <div className="absolute inset-0 flex items-center justify-center text-[12px] text-stone-400 pointer-events-none">
                No activities discovered for this process yet.
              </div>
            )}
          </div>
        )}
      </div>
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
