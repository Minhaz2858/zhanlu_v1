import { useCallback } from 'react';
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  addEdge,
  useReactFlow,
  ReactFlowProvider,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { workflowNodeTypes } from '@/components/workflow/workflowNodeTypes';
import { connectionMeta, useNodeRegistry } from '@/lib/nodeTypeRegistry';

/**
 * CONTROLLED canvas (n8n canvas.store pattern): xyflow state lives in the
 * page, not here. The page owns nodes/edges and every mutation flows through
 * it, so page-initiated ops (delete/undo/redo/load) and canvas interactions
 * (drag/connect/drop) can never diverge.
 *
 * Props:
 *   nodes, edges        — xyflow state owned by the page
 *   onNodesChange       — (changes) => setNodes(applyNodeChanges(changes, nodes))
 *   onEdgesChange       — same for edges
 *   onConnect           — (connection) => addEdge
 *   onNodeAdd           — (node) => append a dropped node
 *   onSelectNode        — (nodeId|null)
 */
function CanvasInner({
  nodes,
  edges,
  onNodesChange,
  onEdgesChange,
  onConnect,
  onNodeAdd,
  onSelectNode,
  onSelectChange,
  readOnly,
}) {
  const { screenToFlowPosition } = useReactFlow();
  const registry = useNodeRegistry();

  const handleConnect = useCallback((params) => {
    // Color supplemental (non-main) edges by connection type. Multi-output
    // nodes use indexed handles (source-main-1); strip the numeric suffix.
    const rawType = (params.sourceHandle || 'source-main').replace('source-', '');
    const handleType = rawType.replace(/-\d+$/, '');
    const meta = connectionMeta(handleType);
    onConnect(addEdge({
      ...params,
      id: `e_${Date.now().toString(36)}`,
      style: handleType === 'main' ? undefined : { stroke: meta.color },
    }));
  }, [onConnect]);

  const onDrop = useCallback((event) => {
    event.preventDefault();
    const type = event.dataTransfer.getData('application/zhanlu-node');
    if (!type) return;
    const position = screenToFlowPosition({ x: event.clientX, y: event.clientY });
    // Seed config from schema defaults (n8n ParameterDefaults) so a dropped
    // node is immediately runnable — e.g. db_query mode defaults to 'nl'
    // (natural language). An empty config would be interpreted by the
    // runtime as SQL mode and defeat the NL-first default. Empty-string
    // defaults (kb_id, question, sql, prompts) are skipped so the config
    // only carries real values.
    const def = registry?.nodeTypes?.find((t) => t.name === type);
    const config = {};
    if (def) {
      for (const p of def.properties) {
        if (p.default !== undefined && p.default !== null && p.default !== '') {
          config[p.name] = p.default;
        }
      }
    }
    onNodeAdd({
      id: `n_${Date.now().toString(36)}`,
      type,
      position,
      data: { label: type, config },
    });
  }, [screenToFlowPosition, onNodeAdd, registry]);

  const onDragOver = useCallback((event) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
  }, []);

  return (
    <div className="h-full w-full">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={handleConnect}
        nodeTypes={workflowNodeTypes}
        fitView
        nodesDraggable={!readOnly}
        nodesConnectable={!readOnly}
        elementsSelectable
        onSelectionChange={({ nodes: sel }) => onSelectChange?.(sel.map((n) => n.id))}
        onNodeClick={(e, node) => {
          // Plain click opens the NDV; modifier clicks are multi-select
          // gestures (shift/meta toggle membership) and must NOT open it.
          if (!(e.metaKey || e.ctrlKey || e.shiftKey)) onSelectNode?.(node.id);
        }}
        onPaneClick={() => onSelectNode?.(null)}
        onDrop={onDrop}
        onDragOver={onDragOver}
        defaultEdgeOptions={{ type: 'smoothstep', animated: true }}
        proOptions={{ hideAttribution: true }}
      >
        <Background gap={16} size={1} />
        <Controls />
        <MiniMap pannable zoomable nodeColor="#8b5cf6" maskColor="rgba(0,0,0,0.15)" />
      </ReactFlow>
    </div>
  );
}

export default function WorkflowCanvas(props) {
  return (
    <ReactFlowProvider>
      <CanvasInner {...props} onSelectChange={props.onSelectChange || (() => {})} />
    </ReactFlowProvider>
  );
}
