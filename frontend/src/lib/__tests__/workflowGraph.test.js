import { describe, expect, it } from 'vitest';
import { flowToXY, xyToFlow, buildPasteGraph, isWorkflowFlow, isAgentArchFlow, editorForRow } from '@/lib/workflowGraph';

const nodes = [
  { id: 'n1', type: 'trigger_chat', position: { x: 0, y: 0 }, data: { label: 'Chat', config: {} } },
  { id: 'n2', type: 'output', position: { x: 100, y: 0 }, data: { label: 'Out', config: { format: 'markdown' } } },
];
const edges = [{ id: 'e1', source: 'n1', target: 'n2' }];

describe('xyToFlow settings round-trip', () => {
  it('preserves flow settings (error workflow) through the save path', () => {
    const settings = { on_error_workflow_id: 'wf-error-1' };
    const flow = xyToFlow(nodes, edges, settings);
    expect(flow.settings).toEqual(settings);
    expect(flow.version).toBe(1);
    expect(flow.nodes).toHaveLength(2);
    expect(flow.edges).toHaveLength(1);
  });

  it('defaults settings to an empty object when omitted', () => {
    const flow = xyToFlow(nodes, edges);
    expect(flow.settings).toEqual({});
  });

  it('round-trips settings through save -> load (xyToFlow -> flowToXY)', () => {
    const saved = xyToFlow(nodes, edges, { on_error_workflow_id: 'wf-error-9' });
    const xy = flowToXY(saved);
    expect(xy.nodes).toHaveLength(2);
    expect(xy.edges).toHaveLength(1);
    // flowToXY deliberately returns only graph state; settings are restored
    // by the page from the raw flow row — assert the raw flow still has them.
    expect(saved.settings.on_error_workflow_id).toBe('wf-error-9');
  });

  it('round-trips a null error-workflow selection', () => {
    const flow = xyToFlow(nodes, edges, { on_error_workflow_id: null });
    expect(flow.settings).toEqual({ on_error_workflow_id: null });
  });
});

describe('edge id normalization', () => {
  it('assigns stable e1..eN ids to edges missing one on load', () => {
    // AI-builder flows can persist edges without ids; xyflow drops null-id
    // edges so nodes would render unconnected. flowToXY must repair them.
    const saved = {
      version: 1,
      nodes,
      edges: [
        { source: 'n1', target: 'n2' },
        { source: 'n2', target: 'n1', id: 'keep-me' },
      ],
    };
    const xy = flowToXY(saved);
    expect(xy.edges.map((e) => e.id)).toEqual(['e1', 'keep-me']);
  });

  it('never persists null edge ids through the save path', () => {
    const flow = xyToFlow(nodes, [{ source: 'n1', target: 'n2', id: null }]);
    expect(flow.edges[0].id).toBe('e1');
  });
});

describe('buildPasteGraph', () => {
  it('remaps node ids and offsets positions by the given delta', () => {
    const clip = {
      nodes: [{ id: 'n_a', type: 'llm', position: { x: 100, y: 100 }, data: { label: 'L', config: {} } }],
      edges: [],
    };
    const { nodes: pasted, edges } = buildPasteGraph(clip, { x: 24, y: 24 });
    expect(pasted).toHaveLength(1);
    expect(pasted[0].id).not.toBe('n_a');
    expect(pasted[0].position).toEqual({ x: 124, y: 124 });
    expect(edges).toEqual([]);
  });

  it('keeps internal edges and rewrites their endpoints to the new ids', () => {
    const clip = {
      nodes: [
        { id: 'n_a', type: 'llm', position: { x: 0, y: 0 }, data: { label: 'A', config: {} } },
        { id: 'n_b', type: 'output', position: { x: 300, y: 0 }, data: { label: 'B', config: {} } },
      ],
      edges: [{ id: 'e1', source: 'n_a', target: 'n_b', sourceHandle: 'source-main', targetHandle: 'target-main' }],
    };
    const { nodes: pasted, edges } = buildPasteGraph(clip);
    const idMap = Object.fromEntries(pasted.map((n, i) => [['n_a', 'n_b'][i], n.id]));
    expect(edges).toHaveLength(1);
    expect(edges[0].source).toBe(idMap.n_a);
    expect(edges[0].target).toBe(idMap.n_b);
    expect(edges[0].id).not.toBe('e1');
  });

  it('rewrites {{inputs.<oldId>.}} references in pasted configs to the new ids', () => {
    const clip = {
      nodes: [{
        id: 'n_a', type: 'llm', position: { x: 0, y: 0 },
        data: { label: 'A', config: { prompt: 'summarize {{inputs.n_a.text}} and {{inputs.n_zz.items[0].x}}' } },
      }],
      edges: [],
    };
    const { nodes: pasted } = buildPasteGraph(clip);
    const newId = pasted[0].id;
    expect(pasted[0].data.config.prompt).toContain(`{{inputs.${newId}.text}}`);
    // references to nodes NOT in the clipboard stay untouched
    expect(pasted[0].data.config.prompt).toContain('{{inputs.n_zz.items[0].x}}');
  });

  it('rewrites expression refs nested inside collection configs', () => {
    const clip = {
      nodes: [{
        id: 'n_a', type: 'set_fields', position: { x: 0, y: 0 },
        data: { label: 'A', config: { fields: [{ name: 'v', value: '{{inputs.n_a.text}}' }] } },
      }],
      edges: [],
    };
    const { nodes: pasted } = buildPasteGraph(clip);
    expect(pasted[0].data.config.fields[0].value).toContain(`{{inputs.${pasted[0].id}.text}}`);
  });
});

// Two shapes share `agent_apps.flow` + the `flow_mode` flag: the n8n canvas
// workflow object and the agent-architecture step array. Routing/rendering
// must branch on the SHAPE, so these helpers are the contract that keeps a
// workflow row out of the agent editor and an agent row out of the canvas.
// Regression: `/my-space/agent/<workflow-id>` crashed with
// "a.flow.forEach is not a function" because AgentConfig rendered FlowTree
// over the canvas object.
describe('flow shape detection', () => {
  it('detects the canvas workflow shape (and its empty form)', () => {
    expect(isWorkflowFlow({ version: 1, nodes: [], edges: [] })).toBe(true);
    expect(isWorkflowFlow({ version: 1, nodes: [{ id: 'n1' }], edges: [{ id: 'e1' }] })).toBe(true);
    expect(isWorkflowFlow({ nodes: [{ id: 'n1' }] })).toBe(true); // nodes alone is enough
  });

  it('rejects the agent-architecture step array', () => {
    expect(isWorkflowFlow([])).toBe(false);
    expect(isWorkflowFlow([{ kind: 'agent', name: 'A' }])).toBe(false);
    expect(isWorkflowFlow([{ kind: 'loop', flow: [] }])).toBe(false);
  });

  it('rejects nullish / malformed values without throwing', () => {
    expect(isWorkflowFlow(null)).toBe(false);
    expect(isWorkflowFlow(undefined)).toBe(false);
    expect(isWorkflowFlow('not a flow')).toBe(false);
    expect(isWorkflowFlow(42)).toBe(false);
    expect(isWorkflowFlow({})).toBe(false);
    expect(isWorkflowFlow({ nodes: 'nope' })).toBe(false);
  });

  it('isAgentArchFlow is the exact mirror used to bounce agent rows off the canvas', () => {
    expect(isAgentArchFlow([])).toBe(true);
    expect(isAgentArchFlow([{ kind: 'agent' }])).toBe(true);
    expect(isAgentArchFlow({ version: 1, nodes: [], edges: [] })).toBe(false);
    expect(isAgentArchFlow(null)).toBe(false);
    expect(isAgentArchFlow(undefined)).toBe(false);
  });

  it('the two predicates are mutually exclusive for every real row shape', () => {
    const rows = [
      { version: 1, nodes: [], edges: [] },
      [{ kind: 'agent' }],
      [],
      null,
      undefined,
    ];
    for (const flow of rows) {
      expect(isWorkflowFlow(flow) && isAgentArchFlow(flow)).toBe(false);
    }
  });
});

// The two pages (AgentConfig, WorkflowCanvas) BOTH route through editorForRow.
// If they ever diverge, a row gets bounced from one to the other forever —
// so the mapping and the termination property are the contract under test.
describe('editorForRow — which editor owns an agent_apps row', () => {
  it('routes canvas-shaped rows to the workflow canvas, whatever flow_mode says', () => {
    expect(editorForRow({ flow_mode: true, flow: { version: 1, nodes: [], edges: [] } })).toBe('workflow');
    expect(editorForRow({ flow_mode: true, flow: { version: 1, nodes: [{ id: 'n1' }], edges: [] } })).toBe('workflow');
    // legacy row: real graph but the flag was never set
    expect(editorForRow({ flow_mode: false, flow: { nodes: [], edges: [] } })).toBe('workflow');
  });

  it('routes composable-flow agents to the agent page even though flow_mode is true', () => {
    expect(editorForRow({ flow_mode: true, flow: [{ kind: 'agent', name: 'A' }] })).toBe('agent');
    expect(editorForRow({ flow_mode: true, flow: [] })).toBe('agent');
  });

  it('routes plain chat agents (no flow) to the agent page', () => {
    expect(editorForRow({ flow_mode: false, flow: null })).toBe('agent');
    expect(editorForRow({ flow_mode: false })).toBe('agent');
    expect(editorForRow({ name: 'Report Writer' })).toBe('agent');
  });

  it('treats a flow_mode row with no graph yet as a workflow (fresh "Untitled Workflow")', () => {
    expect(editorForRow({ flow_mode: true, flow: null })).toBe('workflow');
    expect(editorForRow({ flow_mode: true })).toBe('workflow');
  });

  it('never bounces a row back: an "agent" verdict can never be isWorkflowFlow', () => {
    // This is what guarantees the two redirects terminate instead of looping.
    const rows = [
      { flow_mode: true, flow: { version: 1, nodes: [], edges: [] } },
      { flow_mode: true, flow: [{ kind: 'agent' }] },
      { flow_mode: true, flow: [] },
      { flow_mode: true, flow: null },
      { flow_mode: false, flow: null },
      null,
      undefined,
    ];
    for (const row of rows) {
      if (editorForRow(row) !== 'workflow') {
        expect(isWorkflowFlow(row?.flow)).toBe(false);
      }
    }
  });

  it('handles nullish rows without throwing', () => {
    expect(editorForRow(null)).toBe('agent');
    expect(editorForRow(undefined)).toBe('agent');
  });
});
