import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { applyNodeChanges, applyEdgeChanges } from '@xyflow/react';
import { useLanguage } from '@/lib/LanguageProvider';
import { getWorkflow, saveWorkflow, validateWorkflow, publishWorkflow, deleteWorkflow, listWorkflows } from '@/api/workflows';
import { NodeRegistryProvider, getNodeTypeRegistry } from '@/lib/nodeTypeRegistry';
import { flowToXY, xyToFlow } from '@/lib/workflowGraph';
import WorkflowCanvas from '@/components/workflow/WorkflowCanvas';
import NodePalette from '@/components/workflow/NodePalette';
import NodeDetailView from '@/components/workflow/NodeDetailView';
import RunDrawer from '@/components/workflow/RunDrawer';
import WorkflowBuilderChat from '@/components/workflow/WorkflowBuilderChat';
import { Button } from '@/components/ui/button';
import { toast } from 'sonner';
import { ArrowLeft, Check, Loader2, MessageSquare, Play, Redo2, Rocket, Save, Trash2, Undo2 } from 'lucide-react';

/**
 * Workflow canvas page — CONTROLLED canvas architecture (n8n canvas.store
 * pattern): xyflow nodes/edges live HERE, the canvas renders them and emits
 * change requests upward. Page-initiated ops (load/delete/undo/redo) and
 * canvas interactions (drag/connect/drop) can never diverge.
 */
export default function WorkflowCanvasPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { lang } = useLanguage();

  const [wf, setWf] = useState(null);           // AgentApp row
  const [xyNodes, setXyNodes] = useState(null); // xyflow node state (owned here)
  const [xyEdges, setXyEdges] = useState(null); // xyflow edge state
  const [dirty, setDirty] = useState(false);
  const [selectedNodeId, setSelectedNodeId] = useState(null);
  const [paletteCollapsed, setPaletteCollapsed] = useState(false);
  const [runOpen, setRunOpen] = useState(false);
  const [builderOpen, setBuilderOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [validating, setValidating] = useState(false);
  const [name, setName] = useState('');
  const [settings, setSettings] = useState({});          // flow.settings (error workflow, …)
  const [errorWfOptions, setErrorWfOptions] = useState([]); // candidate error workflows
  const [notFound, setNotFound] = useState(false);
  const [registry, setRegistry] = useState(null);

  // n8n-style undo/redo: history of {nodes, edges} snapshots, committed only
  // on STRUCTURAL changes (add/delete/connect/config) — position drags and
  // selection/dimension noise don't pollute the stack.
  const undoStack = useRef([]);
  const redoStack = useRef([]);
  const lastStructuralKey = useRef(null);

  function structuralKey(nodes, edges) {
    return JSON.stringify({
      nodes: (nodes || []).map((n) => [n.id, n.type, n.data]),
      edges: (edges || []).map((e) => [e.id, e.source, e.target, e.sourceHandle, e.targetHandle]),
    });
  }

  // ---- load ---------------------------------------------------------------

  useEffect(() => {
    getNodeTypeRegistry().then(setRegistry).catch(() => {});
  }, []);

  // Candidate error workflows (n8n Error Workflow picker): every other
  // workflow in the app, excluding this one.
  useEffect(() => {
    listWorkflows()
      .then((rows) => setErrorWfOptions((Array.isArray(rows) ? rows : []).filter((r) => r.id !== id)))
      .catch(() => {});
  }, [id]);

  // reloadWorkflow — fetch + hydrate the canvas (wf row, name, settings,
  // xyflow nodes/edges, reset undo/redo). Runs on mount / id change and is
  // re-invoked after the AI builder persists a flow (onFlowSaved) so the
  // generated graph appears without a manual refresh.
  const reloadWorkflow = useCallback(() => {
    getWorkflow(id)
      .then((row) => {
        setWf(row);
        setName(row.name || 'Untitled Workflow');
        const loaded = row.flow || { version: 1, nodes: [], edges: [] };
        setSettings((loaded.settings) || {});
        const xy = flowToXY(loaded);
        setXyNodes(xy.nodes);
        setXyEdges(xy.edges);
        lastStructuralKey.current = structuralKey(xy.nodes, xy.edges);
        undoStack.current = [];
        redoStack.current = [];
        setDirty(false);
      })
      .catch(() => setNotFound(true));
  }, [id]);

  useEffect(() => {
    reloadWorkflow();
  }, [reloadWorkflow]);

  // ---- change plumbing (canvas -> page) -----------------------------------

  // Commit history + mark dirty when a structural change lands. Returns the
  // next state via the provided setter.
  function commitStructural(prevNodes, prevEdges, nextNodes, nextEdges) {
    const prevKey = structuralKey(prevNodes, prevEdges);
    const nextKey = structuralKey(nextNodes, nextEdges);
    const seen = lastStructuralKey.current;
    lastStructuralKey.current = nextKey;
    if (seen !== null && seen !== nextKey && prevKey !== nextKey) {
      undoStack.current.push({ nodes: prevNodes, edges: prevEdges });
      if (undoStack.current.length > 100) undoStack.current.shift();
      redoStack.current = [];
      setDirty(true);
    }
    return nextKey;
  }

  const onNodesChange = useCallback((changes) => {
    setXyNodes((nds) => {
      const next = applyNodeChanges(changes, nds || []);
      commitStructural(nds || [], xyEdges, next, xyEdges);
      return next;
    });
  }, [xyEdges]);

  const onEdgesChange = useCallback((changes) => {
    setXyEdges((eds) => {
      const next = applyEdgeChanges(changes, eds || []);
      commitStructural(xyNodes, eds || [], xyNodes, next);
      return next;
    });
  }, [xyNodes]);

  const onConnect = useCallback((edge) => {
    setXyEdges((eds) => {
      const next = [...(eds || []), edge];
      commitStructural(xyNodes, eds || [], xyNodes, next);
      return next;
    });
  }, [xyNodes]);

  const onNodeAdd = useCallback((node) => {
    setXyNodes((nds) => {
      const next = [...(nds || []), node];
      commitStructural(nds || [], xyEdges, next, xyEdges);
      return next;
    });
  }, [xyEdges]);

  // ---- page-initiated mutations (delete/undo/redo/config) -----------------

  const onConfigChange = useCallback((nodeId, config, label) => {
    setXyNodes((nds) => {
      const next = (nds || []).map((n) =>
        n.id === nodeId
          ? { ...n, data: { label: label !== undefined ? label : n.data?.label, config } }
          : n
      );
      commitStructural(nds || [], xyEdges, next, xyEdges);
      return next;
    });
  }, [xyEdges]);

  function undo() {
    const entry = undoStack.current.pop();
    if (!entry) return;
    redoStack.current.push({ nodes: xyNodes, edges: xyEdges });
    lastStructuralKey.current = structuralKey(entry.nodes, entry.edges);
    setXyNodes(entry.nodes);
    setXyEdges(entry.edges);
    setDirty(true);
  }

  function redo() {
    const entry = redoStack.current.pop();
    if (!entry) return;
    undoStack.current.push({ nodes: xyNodes, edges: xyEdges });
    lastStructuralKey.current = structuralKey(entry.nodes, entry.edges);
    setXyNodes(entry.nodes);
    setXyEdges(entry.edges);
    setDirty(true);
  }

  function deleteSelectedNode() {
    if (!selectedNodeId) return;
    setXyNodes((nds) => {
      const next = (nds || []).filter((n) => n.id !== selectedNodeId);
      commitStructural(nds || [], xyEdges, next, xyEdges);
      return next;
    });
    setXyEdges((eds) => {
      const next = (eds || []).filter((e) => e.source !== selectedNodeId && e.target !== selectedNodeId);
      commitStructural(xyNodes, eds || [], xyNodes, next);
      return next;
    });
    setSelectedNodeId(null);
    setDirty(true);
  }

  // Keyboard shortcuts (n8n-style): ⌘/Ctrl+Z undo, ⌘/Ctrl+Shift+Z / ⌘+Y redo,
  // Delete/Backspace removes the selected node, Escape deselects.
  useEffect(() => {
    function onKey(e) {
      const target = e.target;
      const typing = target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.tagName === 'SELECT' || target.isContentEditable);
      const mod = e.metaKey || e.ctrlKey;
      if (mod && e.key.toLowerCase() === 'z') {
        if (typing) return;
        e.preventDefault();
        if (e.shiftKey) redo(); else undo();
      } else if (mod && e.key.toLowerCase() === 'y') {
        if (typing) return;
        e.preventDefault();
        redo();
      } else if ((e.key === 'Delete' || e.key === 'Backspace') && selectedNodeId && !typing) {
        e.preventDefault();
        deleteSelectedNode();
      } else if (e.key === 'Escape' && !typing) {
        setSelectedNodeId(null);
      }
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }); // re-bind each render so closures see fresh state

  // ---- actions -------------------------------------------------------------

  async function doSave() {
    if (!xyNodes) return;
    setSaving(true);
    try {
      const flow = xyToFlow(xyNodes, xyEdges, settings);
      const updated = await saveWorkflow(id, { name, description: wf?.description, flow });
      setWf(updated);
      setDirty(false);
      toast.success(lang === 'zh' ? '已保存' : 'Saved');
    } catch (e) {
      toast.error(e.message || String(e));
    } finally {
      setSaving(false);
    }
  }

  async function doValidate() {
    if (!id) return;
    setValidating(true);
    try {
      const res = await validateWorkflow(id);
      if (res.valid) toast.success(lang === 'zh' ? '校验通过' : 'Workflow is valid');
      else {
        const msg = res.errors.join('; ');
        toast.error(msg);
      }
    } catch (e) {
      toast.error(e.message || String(e));
    } finally {
      setValidating(false);
    }
  }

  async function doPublish() {
    try {
      const updated = await publishWorkflow(id);
      setWf(updated);
      toast.success(lang === 'zh' ? '已发布' : 'Published');
    } catch (e) {
      toast.error(e.message || String(e));
    }
  }

  const graphForNdv = {
    nodes: xyNodes || [],
    edges: xyEdges || [],
  };

  if (notFound) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 px-8">
        <p className="text-sm text-muted-foreground">{lang === 'zh' ? '未找到该工作流' : 'Workflow not found'}</p>
        <Button variant="outline" onClick={() => navigate('/my-space?tab=workflow')}>{lang === 'zh' ? '返回' : 'Back'}</Button>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      {/* Top bar */}
      <div className="flex h-14 shrink-0 items-center gap-3 border-b border-border bg-card/60 px-4">
        <button onClick={() => navigate('/my-space?tab=workflow')} className="rounded-md p-1.5 text-muted-foreground hover:bg-secondary" title={lang === 'zh' ? '返回' : 'Back'}>
          <ArrowLeft className="h-4 w-4" />
        </button>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          className="w-56 rounded-lg border border-transparent bg-transparent px-2 py-1 text-sm font-semibold text-foreground focus:border-border focus:bg-card focus:outline-none"
          placeholder="Workflow name"
        />
        <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide ${wf?.status === 'published' ? 'bg-[hsl(var(--chart-2))]/10 text-[hsl(var(--chart-2))]' : 'bg-muted text-muted-foreground'}`}>
          {wf?.status === 'published' ? (lang === 'zh' ? '已发布' : 'Published') : (lang === 'zh' ? '草稿' : 'Draft')}
        </span>
        {dirty && <span className="text-[10px] text-muted-foreground">•</span>}
        <div className="ml-auto flex items-center gap-2">
          <Button size="sm" variant="ghost" onClick={undo} disabled={undoStack.current.length === 0} title="⌘Z">
            <Undo2 className="h-3.5 w-3.5" />
          </Button>
          <Button size="sm" variant="ghost" onClick={redo} disabled={redoStack.current.length === 0} title="⇧⌘Z">
            <Redo2 className="h-3.5 w-3.5" />
          </Button>
          <Button size="sm" variant="outline" onClick={doValidate} disabled={validating}>
            {validating ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : <Check className="mr-1 h-3.5 w-3.5" />}
            {lang === 'zh' ? '校验' : 'Validate'}
          </Button>
          <Button size="sm" variant="outline" onClick={doSave} disabled={saving || !dirty}>
            {saving ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : <Save className="mr-1 h-3.5 w-3.5" />}
            {lang === 'zh' ? '保存' : 'Save'}
          </Button>
          <select
            value={settings.on_error_workflow_id || ''}
            onChange={(e) => {
              setSettings((s) => ({ ...s, on_error_workflow_id: e.target.value || null }));
              setDirty(true);
            }}
            className="h-8 w-48 rounded-lg border border-border bg-card px-2 text-xs text-foreground focus:outline-none focus:ring-2 focus:ring-primary/30"
            title={lang === 'zh' ? '错误工作流：主工作流运行失败时自动执行（n8n Error Workflow）' : 'Error workflow: executed automatically when this workflow fails (n8n Error Workflow)'}
          >
            <option value="">{lang === 'zh' ? '错误工作流：无' : 'Error workflow: none'}</option>
            {errorWfOptions.map((w) => (
              <option key={w.id} value={w.id}>{w.name}</option>
            ))}
          </select>
          <Button size="sm" variant="outline" onClick={() => setBuilderOpen(true)} title={lang === 'zh' ? 'AI 构建' : 'Build with AI'}>
            <MessageSquare className="mr-1 h-3.5 w-3.5" /> {lang === 'zh' ? 'AI 构建' : 'Build with AI'}
          </Button>
          <Button size="sm" variant="outline" onClick={() => setRunOpen(true)}>
            <Play className="mr-1 h-3.5 w-3.5" /> {lang === 'zh' ? '运行' : 'Run'}
          </Button>
          <Button size="sm" onClick={doPublish} disabled={wf?.status === 'published'}>
            <Rocket className="mr-1 h-3.5 w-3.5" /> {lang === 'zh' ? '发布' : 'Publish'}
          </Button>
        </div>
      </div>

      {/* Body: palette | canvas | drawers */}
      <div className="flex min-h-0 flex-1">
        <NodePalette collapsed={paletteCollapsed} onToggle={() => setPaletteCollapsed((c) => !c)} registry={registry} />
        <div className="min-w-0 flex-1">
          {xyNodes ? (
            <NodeRegistryProvider>
              <WorkflowCanvas
                nodes={xyNodes}
                edges={xyEdges || []}
                onNodesChange={onNodesChange}
                onEdgesChange={onEdgesChange}
                onConnect={onConnect}
                onNodeAdd={onNodeAdd}
                onSelectNode={setSelectedNodeId}
              />
            </NodeRegistryProvider>
          ) : (
            <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
              <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading…
            </div>
          )}
        </div>
        {selectedNodeId ? (
          <NodeDetailView
            workflowId={id}
            nodes={xyNodes || []}
            node={{
              ...((xyNodes || []).find((n) => n.id === selectedNodeId) || null),
              _flowEdges: xyEdges || [],
            }}
            registry={registry}
            onClose={() => setSelectedNodeId(null)}
            onConfigChange={onConfigChange}
          />
        ) : null}
        {runOpen && (
          <RunDrawer
            workflowId={id}
            open
            onClose={() => setRunOpen(false)}
          />
        )}
      </div>

      {/* AI builder slide-over — gates its own rendering on `open`, so the
          unconditional mount never covers the canvas while closed. On
          flow_saved it closes itself and reloads the graph from the backend. */}
      <WorkflowBuilderChat
        workflowId={id}
        open={builderOpen}
        onClose={() => setBuilderOpen(false)}
        onFlowSaved={() => { setBuilderOpen(false); reloadWorkflow(); }}
      />
    </div>
  );
}
