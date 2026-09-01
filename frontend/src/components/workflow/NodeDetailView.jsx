import { useEffect, useMemo, useState } from 'react';
import { useLanguage } from '@/lib/LanguageProvider';
import { getNodeTypeRegistry, nodeTypeOf } from '@/lib/nodeTypeRegistry';
import { listWorkflowRuns } from '@/api/workflows';
import NodeParameterForm from '@/components/workflow/NodeParameterForm';
import { X } from 'lucide-react';

/**
 * NDV (Node Detail View) — n8n-style three-tab node inspector:
 *   Parameters | Output | Input
 * Parameters are auto-generated from the shared registry schema; Output/Input
 * show the selected node's data from the latest run (per-node WorkflowRunStep).
 */
export default function NodeDetailView({ workflowId, nodes = [], node, registry, onClose, onConfigChange }) {
  const { lang } = useLanguage();
  const [localRegistry, setLocalRegistry] = useState(registry || null);
  const [tab, setTab] = useState('parameters');
  const [runData, setRunData] = useState(null); // { run, step, inputs }
  const config = (node.data && node.data.config) || {};

  // Expression picker data: upstream nodes (via edges) + their output fields.
  const upstreamFields = useMemo(() => {
    const edges = node._flowEdges || [];
    const sources = [...new Set(edges.filter((e) => e.target === node.id).map((e) => e.source))];
    return sources.map((sid) => {
      const src = (nodes || []).find((n) => n.id === sid);
      const def = (localRegistry && localRegistry.nodeTypes && localRegistry.nodeTypes[src.type]) || null;
      const label = (src && src.data && src.data.label) || (def && def.displayName) || sid;
      return {
        nodeId: sid,
        label,
        fields: [
          { path: 'text', label: 'text' },
          { path: 'items', label: 'items (all)' },
          { path: 'items[0]', label: 'items[0] (first item)' },
        ],
      };
    });
  }, [node, nodes, localRegistry]);
  const [loadingRun, setLoadingRun] = useState(false);
  const type = node?.type;
  const def = localRegistry ? nodeTypeOf(type) : null;

  useEffect(() => {
    if (!localRegistry) {
      getNodeTypeRegistry().then(setLocalRegistry).catch(() => {});
    }
  }, [localRegistry]);

  // Fetch the latest run + this node's step when the Output/Input tab opens.
  useEffect(() => {
    let alive = true;
    if ((tab === 'output' || tab === 'input') && workflowId && node?.id) {
      setLoadingRun(true);
      setRunData(null);
      listWorkflowRuns(workflowId)
        .then((runs) => {
          if (!alive) return;
          const run = Array.isArray(runs) && runs.length > 0 ? runs[0] : null;
          if (!run) { setRunData({ run: null, step: null, inputs: {} }); return; }
          // Steps are ordered ASC (running row first, completed row second
          // per node) — take the LAST step for this node.
          const nodeSteps = (run.steps || []).filter((s) => s.node_id === node.id);
          const step = nodeSteps.length > 0 ? nodeSteps[nodeSteps.length - 1] : null;
          // Inputs = outputs of nodes wired into this node on MAIN edges.
          const inputs = {};
          (node._flowEdges || []).forEach((e) => {
            if (e.target === node.id) {
              const srcSteps = (run.steps || []).filter((s) => s.node_id === e.source);
              const src = srcSteps.length > 0 ? srcSteps[srcSteps.length - 1] : null;
              if (src && src.output_json) inputs[e.source] = src.output_json;
            }
          });
          setRunData({ run, step, inputs });
        })
        .catch(() => alive && setRunData({ run: null, step: null, inputs: {} }))
        .finally(() => alive && setLoadingRun(false));
    }
    return () => { alive = false; };
  }, [tab, workflowId, node?.id, node?._flowEdges]);

  if (!node || !def) {
    return (
      <div className="flex h-full w-80 flex-col border-l border-border bg-card/60">
        <div className="flex items-center justify-between px-4 py-3">
          <span className="text-sm font-semibold text-foreground">{lang === 'zh' ? '节点详情' : 'Node'}</span>
          <button onClick={onClose} className="rounded-md p-1 text-muted-foreground hover:bg-secondary"><X className="h-4 w-4" /></button>
        </div>
        <div className="px-4 py-8 text-center text-xs text-muted-foreground">
          {lang === 'zh' ? '选择一个节点以查看详情' : 'Select a node to inspect'}
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full w-80 flex-col border-l border-border bg-card/60">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <div className="flex min-w-0 items-center gap-2">
          <span className="text-base leading-none" style={{ color: def.color }}>{def.icon}</span>
          <div className="min-w-0">
            <div className="truncate text-xs font-semibold text-foreground">{def.displayName}</div>
            <input
              value={node.data.label || ''}
              onChange={(e) => onConfigChange(node.id, config, e.target.value)}
              className="w-full truncate rounded border border-transparent bg-transparent px-1 py-0.5 text-[11px] text-muted-foreground focus:border-border focus:bg-card focus:outline-none"
              placeholder={def.displayName}
            />
          </div>
        </div>
        <button onClick={onClose} className="rounded-md p-1 text-muted-foreground hover:bg-secondary"><X className="h-4 w-4" /></button>
      </div>

      {/* Tabs */}
      <div className="flex border-b border-border text-xs">
        {['parameters', 'output', 'input'].map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`flex-1 px-3 py-2 font-medium transition-colors ${
              tab === t ? 'border-b-2 border-primary text-foreground' : 'text-muted-foreground hover:text-foreground'
            }`}
          >
            {lang === 'zh' ? { parameters: '参数', output: '输出', input: '输入' }[t] : { parameters: 'Parameters', output: 'Output', input: 'Input' }[t]}
          </button>
        ))}
      </div>

      {/* Tab bodies */}
      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4">
        {tab === 'parameters' && (
          localRegistry ? (
            <NodeParameterForm node={node} registry={localRegistry} upstreamFields={upstreamFields} onChange={(patch) => onConfigChange(node.id, { ...config, ...patch })} />
          ) : (
            <div className="h-24 animate-pulse rounded-lg bg-muted" />
          )
        )}

        {tab === 'output' && (
          loadingRun ? <div className="h-16 animate-pulse rounded-lg bg-muted" />
          : runData?.step ? (
            <div className="space-y-2">
              <div className="flex items-center gap-2 text-[11px]">
                <StatusDot status={runData.step.status} />
                <span className="font-medium text-foreground">{runData.step.status}</span>
                {runData.step.error && <span className="truncate text-red-500">{runData.step.error}</span>}
              </div>
              <pre className="max-h-72 overflow-auto whitespace-pre-wrap rounded-lg border border-border bg-card px-3 py-2 font-mono text-[11px] text-foreground">
                {JSON.stringify(runData.step.output_json ?? runData.step.output ?? null, null, 2)}
              </pre>
            </div>
          ) : (
            <div className="py-6 text-center text-[11px] text-muted-foreground">
              {lang === 'zh' ? '此节点还没有运行数据' : 'No run data for this node yet'}
            </div>
          )
        )}

        {tab === 'input' && (
          loadingRun ? <div className="h-16 animate-pulse rounded-lg bg-muted" />
          : runData && Object.keys(runData.inputs).length > 0 ? (
            <div className="space-y-2">
              {Object.entries(runData.inputs).map(([srcId, out]) => (
                <div key={srcId}>
                  <div className="mb-0.5 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">{srcId}</div>
                  <pre className="max-h-40 overflow-auto whitespace-pre-wrap rounded-lg border border-border bg-card px-3 py-2 font-mono text-[11px] text-foreground">
                    {JSON.stringify(out, null, 2)}
                  </pre>
                </div>
              ))}
            </div>
          ) : (
            <div className="py-6 text-center text-[11px] text-muted-foreground">
              {lang === 'zh' ? '没有上游输入' : 'No upstream inputs'}
            </div>
          )
        )}
      </div>
    </div>
  );
}

function StatusDot({ status }) {
  const color = status === 'completed' ? '#10b981' : status === 'failed' ? '#ef4444' : '#f59e0b';
  return <span className="inline-block h-2 w-2 rounded-full" style={{ backgroundColor: color }} />;
}
