import { useEffect, useMemo, useState } from 'react';
import { useLanguage } from '@/lib/LanguageProvider';
import { getNodeTypeRegistry, nodeTypeOf } from '@/lib/nodeTypeRegistry';
import { nodeLabel } from '@/lib/workflowGraph';
import { listWorkflowRuns, testNode } from '@/api/workflows';
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
      const label = (src && src.data && src.data.label) || (def && (nodeLabel(src.type, lang) || def.displayName)) || sid;
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
  // "Test node" drawer: single-node run with an editable payload whose
  // upstreams are prefilled from the last completed run.
  const [testOpen, setTestOpen] = useState(false);
  const [testPayloadText, setTestPayloadText] = useState('');
  const [testPrefillLoading, setTestPrefillLoading] = useState(false);
  const [testRunning, setTestRunning] = useState(false);
  const [testResult, setTestResult] = useState(null); // { status, output, error }
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

  // Fetch the latest run and prefill the drawer payload ({ trigger_input: '',
  // upstreams: { directMainUpstreamId: lastCompletedOutputJson } }).
  const refreshTestPayload = () => {
    if (!workflowId || !node || !node.id) {
      setTestPayloadText(JSON.stringify({ trigger_input: '', upstreams: {} }, null, 2));
      return;
    }
    setTestPrefillLoading(true);
    listWorkflowRuns(workflowId)
      .then((runs) => {
        const run = Array.isArray(runs) && runs.length > 0 ? runs[0] : null;
        setTestPayloadText(JSON.stringify({ trigger_input: '', upstreams: buildUpstreamPrefill(run, node) }, null, 2));
      })
      .catch(() => setTestPayloadText(JSON.stringify({ trigger_input: '', upstreams: {} }, null, 2)))
      .finally(() => setTestPrefillLoading(false));
  };

  const openTestDrawer = () => {
    setTestOpen(true);
    setTestResult(null);
    setTestRunning(false);
    setTestPayloadText('');
    refreshTestPayload();
  };

  // Run the selected node alone. Errors are rendered inline in the drawer —
  // never thrown / never surfaced through the app toaster.
  const runNodeTest = async () => {
    if (testRunning) return;
    let payload;
    try {
      payload = JSON.parse(testPayloadText || '{}');
    } catch {
      setTestResult({ status: 'failed', error: lang === 'zh' ? '负载不是合法的 JSON' : 'Payload is not valid JSON' });
      return;
    }
    setTestRunning(true);
    setTestResult(null);
    try {
      const res = await testNode(workflowId, node.id, payload);
      if (res && (res.status === 'completed' || res.output !== undefined)) setTestResult(res);
      else setTestResult({ status: 'failed', error: lang === 'zh' ? '测试端点未返回结果' : 'Test endpoint returned no result' });
    } catch (err) {
      setTestResult({ status: 'failed', error: (err && err.message) ? err.message : String(err) });
    } finally {
      setTestRunning(false);
    }
  };

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

  const testParsedPayload = (() => {
    try { return JSON.parse(testPayloadText || '{}'); } catch { return null; }
  })();
  const testHasUpstreams = !!(testParsedPayload && testParsedPayload.upstreams && Object.keys(testParsedPayload.upstreams).length > 0);

  return (
    <div className="flex h-full w-80 flex-col border-l border-border bg-card/60">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <div className="flex min-w-0 items-center gap-2">
          <span className="text-base leading-none" style={{ color: def.color }}>{def.icon}</span>
          <div className="min-w-0">
            <div className="truncate text-xs font-semibold text-foreground">{nodeLabel(type, lang) || def.displayName}</div>
            <input
              value={node.data.label || ''}
              onChange={(e) => onConfigChange(node.id, config, e.target.value)}
              className="w-full truncate rounded border border-transparent bg-transparent px-1 py-0.5 text-[11px] text-muted-foreground focus:border-border focus:bg-card focus:outline-none"
              placeholder={nodeLabel(type, lang) || def.displayName}
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

      {/* Test-node action */}
      <div className="flex items-center justify-between border-b border-border px-2 py-1">
        <button
          onClick={() => (testOpen ? setTestOpen(false) : openTestDrawer())}
          className={`inline-flex items-center gap-1 rounded-md px-2 py-1 text-[11px] font-medium transition-colors ${
            testOpen ? 'bg-primary/10 text-primary' : 'text-muted-foreground hover:bg-secondary hover:text-foreground'
          }`}
        >
          <span className="text-[9px] leading-none">▶</span>
          {lang === 'zh' ? '测试节点' : 'Test node'}
        </button>
        {testResult && !testOpen && (
          <span className={`truncate text-[10px] ${testResult.status === 'failed' ? 'text-red-500' : 'text-emerald-500'}`}>
            {testResult.status === 'failed'
              ? (lang === 'zh' ? '上次测试失败' : 'Last test failed')
              : (lang === 'zh' ? '上次测试完成' : 'Last test completed')}
          </span>
        )}
      </div>

      {/* Tab bodies */}
      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4">
        {testOpen ? (
          <div className="space-y-2">
            {/* Drawer header */}
            <div className="flex items-center justify-between">
              <span className="text-[11px] font-semibold text-foreground">
                {lang === 'zh' ? '测试节点' : 'Test node'}
              </span>
              <button
                onClick={() => setTestOpen(false)}
                title={lang === 'zh' ? '关闭' : 'Close'}
                className="rounded-md p-1 text-muted-foreground hover:bg-secondary"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>

            {!testHasUpstreams && !testPrefillLoading && (
              <p className="text-[10px] leading-relaxed text-muted-foreground">
                {lang === 'zh'
                  ? '暂无已完成的运行数据 — upstreams 留空。trigger_input 可用 {"text": "..."} 模拟聊天输入。'
                  : 'No completed run data yet — upstreams left empty. trigger_input accepts {"text": "..."} to simulate chat input.'}
              </p>
            )}

            <textarea
              value={testPayloadText}
              onChange={(e) => setTestPayloadText(e.target.value)}
              rows={10}
              spellCheck={false}
              placeholder='{"text": "..."}'
              className="w-full resize-y rounded-lg border border-border bg-card px-2 py-1.5 font-mono text-[11px] text-foreground outline-none focus:border-primary"
            />
            {testPrefillLoading && <div className="h-5 animate-pulse rounded-md bg-muted" />}

            <div className="flex flex-wrap items-center gap-1.5">
              <button
                onClick={runNodeTest}
                disabled={testRunning || testPrefillLoading}
                className="rounded-md bg-primary px-2.5 py-1 text-[11px] font-medium text-primary-foreground hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {testRunning ? (lang === 'zh' ? '运行中…' : 'Running…') : (lang === 'zh' ? '运行测试' : 'Run test')}
              </button>
              <button
                onClick={refreshTestPayload}
                disabled={testPrefillLoading}
                className="rounded-md border border-border px-2.5 py-1 text-[11px] text-muted-foreground hover:bg-secondary hover:text-foreground disabled:opacity-50"
              >
                {lang === 'zh' ? '从上次运行加载' : 'Reload from last run'}
              </button>
              <button
                onClick={() => setTestOpen(false)}
                className="rounded-md border border-border px-2.5 py-1 text-[11px] text-muted-foreground hover:bg-secondary hover:text-foreground"
              >
                {lang === 'zh' ? '关闭' : 'Close'}
              </button>
            </div>

            {testResult && (
              testResult.status === 'failed' ? (
                <div className="flex items-start gap-1.5 rounded-lg border border-red-500/30 bg-red-500/5 px-2.5 py-2 text-[11px] text-red-500">
                  <StatusDot status="failed" />
                  <span className="min-w-0 whitespace-pre-wrap break-words">{String(testResult.error || (lang === 'zh' ? '测试失败' : 'Test failed'))}</span>
                </div>
              ) : (
                <div className="space-y-1">
                  <div className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
                    <StatusDot status="completed" />
                    <span>{lang === 'zh' ? '输出' : 'Output'}</span>
                  </div>
                  <pre className="max-h-72 overflow-auto whitespace-pre-wrap rounded-lg border border-border bg-card px-3 py-2 font-mono text-[11px] text-foreground">
                    {JSON.stringify(testResult.output ?? null, null, 2)}
                  </pre>
                </div>
              )
            )}

            <p className="pt-1 text-[10px] leading-relaxed text-muted-foreground/80">
              {lang === 'zh'
                ? '测试运行会忽略重试 / 出错设置，且不会保存到运行历史。'
                : 'Test runs ignore retry / on-error settings and are not saved to run history.'}
            </p>
          </div>
        ) : (
          <>
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
          </>
        )}
      </div>
    </div>
  );
}

// Main edges only: this app's handles are `${type}-${connectionType}` with an
// optional numeric suffix for repeated outputs (e.g. Switch's Default/Cases =
// `source-main`, `source-main-1`, ...). Supplemental outputs (error/success…)
// carry their own connection type, so we keep `undefined`/empty + `source-main*`.
function isMainSourceEdge(e) {
  if (!e) return false;
  const h = e.sourceHandle;
  return !h || h === 'source-main' || h.indexOf('source-main-') === 0;
}

// Upstream prefill for the test-node payload: for each DIRECT MAIN upstream
// (edge.target === this node) take the LAST completed step of the latest run
// and use its output_json, keyed by the source node id.
function buildUpstreamPrefill(run, node) {
  const upstreams = {};
  const steps = (run && run.steps) || [];
  (node._flowEdges || []).forEach((e) => {
    if (e.target !== node.id || !isMainSourceEdge(e)) return;
    const done = steps.filter((s) => s.node_id === e.source && s.status === 'completed' && s.output_json != null);
    if (done.length > 0) upstreams[e.source] = done[done.length - 1].output_json;
  });
  return upstreams;
}

function StatusDot({ status }) {
  const color = status === 'completed' ? '#10b981' : status === 'failed' ? '#ef4444' : '#f59e0b';
  return <span className="inline-block h-2 w-2 rounded-full" style={{ backgroundColor: color }} />;
}
