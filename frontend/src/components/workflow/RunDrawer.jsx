import { useEffect, useRef, useState } from 'react';
import { useLanguage } from '@/lib/LanguageProvider';
import { runWorkflowAsync, rerunWorkflow, streamUrl } from '@/api/workflows';
import { authStreamingFetch } from '@/api/authFetch';
import { Button } from '@/components/ui/button';
import { Loader2, Play, CheckCircle2, XCircle, Clock } from 'lucide-react';

/**
 * Run panel with LIVE per-node execution (n8n push pattern):
 * POST /run-async → run_id → SSE stream of step events → node badges
 * update in real time (running → completed/failed) → run_finished closes.
 */
export default function RunDrawer({ workflowId, open, onClose }) {
  const { lang } = useLanguage();
  const [input, setInput] = useState('');
  const [running, setRunning] = useState(false);
  const [steps, setSteps] = useState({}); // node_id -> {nodeType, status, error, output}
  const [status, setStatus] = useState(null); // 'running' | 'completed' | 'failed'
  const [output, setOutput] = useState(null);
  const [error, setError] = useState(null);
  const [errorWf, setErrorWf] = useState(null); // {run_id, status} — error workflow executed
  const [runId, setRunId] = useState(null); // active run whose SSE stream is connected
  const [rerunMeta, setRerunMeta] = useState(null); // {rerun_of, rerun_from_node} when the active run is a rerun
  const abortRef = useRef(null);

  // Connect (or reconnect) the live SSE stream whenever the active run id
  // changes — the initial Run and the "Rerun from here" switches share this
  // ONE path. Changing runId aborts the previous run's stream (cleanup below),
  // clears no steps by itself: callers reset per-run state explicitly.
  useEffect(() => {
    if (!runId) return undefined;
    let cancelled = false;
    const controller = new AbortController();
    abortRef.current = controller;
    setRunning(true);
    setStatus('running');

    (async () => {
      try {
        const res = await authStreamingFetch(streamUrl(workflowId, runId), {
          signal: controller.signal,
        });
        if (!res.ok || !res.body) throw new Error(`stream ${res.status}`);

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        // eslint-disable-next-line no-constant-condition
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          if (cancelled) return;
          buffer += decoder.decode(value, { stream: true });
          const parts = buffer.split('\n\n');
          buffer = parts.pop() || '';
          for (const chunk of parts) {
            const line = chunk.split('\n').find((l) => l.startsWith('data: '));
            if (!line) continue;
            let ev;
            try { ev = JSON.parse(line.slice(6)); } catch { continue; }
            if (ev.type === 'step') {
              setSteps((prev) => ({ ...prev, [ev.node_id]: { nodeType: ev.node_type, status: ev.status, error: ev.error, output: ev.output } }));
            } else if (ev.type === 'run_finished') {
              setStatus(ev.status);
              setOutput(ev.output ?? null);
              setError(ev.error ?? null);
              if (ev.error_workflow_run_id) {
                setErrorWf({ run_id: ev.error_workflow_run_id, status: ev.error_workflow_status, error: ev.error_workflow_error });
              }
              setRunning(false);
              controller.abort(); // run is done — release the connection
            }
          }
        }
        // Stream closed without run_finished — treat as finished (no more events).
        if (!cancelled) setRunning(false);
      } catch (e) {
        if (e.name === 'AbortError' || cancelled) return; // intentional close / switch
        setError(e.message || String(e));
        setStatus('failed');
        setRunning(false);
      }
    })();

    return () => {
      cancelled = true;
      controller.abort();
      if (abortRef.current === controller) abortRef.current = null;
    };
  }, [workflowId, runId]);

  if (!open) return null;

  function closeStream() {
    if (abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
    }
  }

  async function doRun() {
    setRunning(true);
    setError(null);
    setOutput(null);
    setErrorWf(null);
    setRerunMeta(null);
    setStatus('running');
    setSteps({});
    closeStream();
    setRunId(null); // drop the old active run so the stream effect reconnects below

    try {
      const { run_id } = await runWorkflowAsync(workflowId, input || 'hello');
      setRunId(run_id); // stream effect subscribes to this run's SSE channel
    } catch (e) {
      setError(e.message || String(e));
      setStatus('failed');
      setRunning(false);
    }
  }

  // Point the drawer at a different run (e.g. a rerun spawned from a step):
  // reset the previous run's per-run state, then let the runId stream effect
  // abort the old connection and subscribe to the new run's channel.
  function switchRun(newRunId) {
    setSteps({});
    setStatus('running');
    setOutput(null);
    setError(null);
    setErrorWf(null);
    setRunning(true);
    setRunId(newRunId);
  }

  // "↻ Rerun from here" — re-execute a completed node + its downstream. The
  // backend tags the new run with rerun_of / rerun_from_node; mirror that
  // locally so the header badge can describe the active run (endpoint returns
  // {run_id} only).
  async function rerunFromStep(nodeId) {
    const parentRunId = runId;
    if (!parentRunId || running) return;
    try {
      const res = await rerunWorkflow(workflowId, { run_id: parentRunId, start_node_id: nodeId });
      if (!res || !res.run_id) throw new Error('rerun returned no run_id');
      setRerunMeta({ rerun_of: parentRunId, rerun_from_node: nodeId });
      switchRun(res.run_id);
    } catch (e) {
      // Inline error — surface it in the drawer's error banner, never crash.
      setError(e.message || String(e));
    }
  }

  const statusIcon = {
    completed: <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500" />,
    failed: <XCircle className="h-3.5 w-3.5 text-red-500" />,
    running: <Loader2 className="h-3.5 w-3.5 animate-spin text-amber-500" />,
    retrying: <Loader2 className="h-3.5 w-3.5 animate-spin text-amber-500" />,
    skipped: <Clock className="h-3.5 w-3.5 text-muted-foreground" />,
  };
  const stepList = Object.entries(steps);
  // Friendly name for the node a rerun started from (falls back to its id
  // until the rerun stream reports that step back).
  const rerunLabel = rerunMeta
    ? (steps[rerunMeta.rerun_from_node]?.nodeType || rerunMeta.rerun_from_node)
    : null;

  return (
    <div className="flex h-full w-96 flex-col border-l border-border bg-card/60">
      <div className="flex items-center justify-between gap-2 border-b border-border px-4 py-3">
        <div className="flex min-w-0 items-center gap-2">
          <span className="text-sm font-semibold text-foreground">{lang === 'zh' ? '测试运行' : 'Test Run'}</span>
          {rerunMeta && rerunLabel && (
            <span
              title={`rerun_of: ${rerunMeta.rerun_of}`}
              className="truncate rounded-full border border-primary/20 bg-primary/10 px-2 py-0.5 text-[10px] font-medium text-primary"
            >
              {lang === 'zh'
                ? `从 ${rerunLabel} 重跑 · 原 ${rerunMeta.rerun_of.slice(0, 8)}`
                : `Rerun from ${rerunLabel} · of ${rerunMeta.rerun_of.slice(0, 8)}`}
            </span>
          )}
        </div>
        <button onClick={() => { closeStream(); onClose(); }} className="rounded-md p-1 text-muted-foreground hover:bg-secondary">✕</button>
      </div>

      <div className="space-y-3 px-4 py-4">
        <textarea
          className="w-full resize-y rounded-lg border border-border bg-card px-2.5 py-2 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/30"
          rows={3}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder={lang === 'zh' ? '模拟用户输入…' : 'Simulated user message…'}
        />
        <Button onClick={doRun} disabled={running} className="w-full">
          {running ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <Play className="mr-1 h-4 w-4" />}
          {running ? (lang === 'zh' ? '运行中…' : 'Running…') : (lang === 'zh' ? '运行' : 'Run')}
        </Button>

        {error && <div className="rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-600">{error}</div>}

        {errorWf && (
          <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-600">
            <span className="font-semibold">{lang === 'zh' ? '错误工作流已执行' : 'Error workflow executed'}</span>
            <span className="ml-1">({errorWf.status}{errorWf.error ? ` — ${errorWf.error}` : ''} · {errorWf.run_id.slice(0, 8)})</span>
            <span className="block text-[10px] opacity-80">{lang === 'zh' ? '查看该工作流的运行历史以获取详情' : 'See the error workflow\u2019s run history for details'}</span>
          </div>
        )}

        {(status || stepList.length > 0) && (
          <div className="space-y-3">
            <div className={`flex items-center gap-2 rounded-lg border px-3 py-2 text-xs font-medium ${
              status === 'completed' ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-600'
              : status === 'failed' ? 'border-red-500/30 bg-red-500/10 text-red-600'
              : 'border-amber-500/30 bg-amber-500/10 text-amber-600'
            }`}>
              {statusIcon[status] || <Clock className="h-3.5 w-3.5 text-muted-foreground" />}
              {status === 'completed' ? (lang === 'zh' ? '运行完成' : 'Completed')
                : status === 'failed' ? (lang === 'zh' ? '运行失败' : 'Failed')
                : (lang === 'zh' ? '运行中…' : 'Running…')}
              {status === 'failed' && error && <span className="ml-1 truncate">{error}</span>}
            </div>

            {stepList.length > 0 && (
              <div className="space-y-1.5">
                <div className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
                  {lang === 'zh' ? '节点步骤（实时）' : 'Node steps (live)'}
                </div>
                {stepList.map(([nodeId, s]) => (
                  <div key={nodeId} className="flex items-center gap-2 rounded-lg border border-border bg-card px-2.5 py-1.5 text-xs">
                    {statusIcon[s.status] || <Clock className="h-3.5 w-3.5 text-muted-foreground" />}
                    <span className="min-w-0 flex-1 truncate font-medium text-foreground">{s.nodeType}</span>
                    {s.error ? <span className="truncate text-[10px] text-red-500">{s.error}</span> : null}
                    {s.status === 'completed' && (
                      <button
                        type="button"
                        onClick={() => rerunFromStep(nodeId)}
                        disabled={running}
                        title={lang === 'zh' ? '从此节点重跑' : 'Rerun from here'}
                        className="shrink-0 rounded-md px-1 text-sm leading-none text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground disabled:cursor-not-allowed disabled:opacity-30"
                      >↻</button>
                    )}
                  </div>
                ))}
              </div>
            )}

            {output != null && status === 'completed' && (
              <div>
                <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">{lang === 'zh' ? '输出' : 'Output'}</div>
                <pre className="max-h-48 overflow-auto whitespace-pre-wrap rounded-lg border border-border bg-card px-3 py-2 text-xs text-foreground">{String(output)}</pre>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
