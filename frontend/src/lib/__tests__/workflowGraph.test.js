import { describe, expect, it } from 'vitest';
import { flowToXY, xyToFlow, buildPasteGraph } from '@/lib/workflowGraph';

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
