import { describe, it, expect, vi, beforeEach } from 'vitest';

const { mockFetch } = vi.hoisted(() => ({
  mockFetch: vi.fn(),
}));

vi.mock('@/api/authFetch', () => ({
  authFetch: mockFetch,
}));

// Pin appId so URL assertions are deterministic regardless of env vars.
vi.mock('@/lib/app-params', () => ({
  appParams: { appId: 'app-42' },
}));

import { testNode, rerunWorkflow } from '../workflows';

beforeEach(() => {
  vi.clearAllMocks();
});

const okResponse = () => ({
  ok: true,
  status: 200,
  json: async () => ({}),
});

describe('workflows API wrappers (source contract)', () => {
  it('exports testNode and rerunWorkflow as functions', () => {
    expect(typeof testNode).toBe('function');
    expect(typeof rerunWorkflow).toBe('function');
  });

  it('testNode POSTs trigger_input/upstreams to the node test endpoint', async () => {
    mockFetch.mockResolvedValue(okResponse());

    const payload = { trigger_input: 'hi', upstreams: { 'a1': { output: 1 } } };
    await testNode('wf-1', 'node-9', payload);

    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [url, init] = mockFetch.mock.calls[0];
    expect(url).toBe('/api/apps/app-42/workflows/wf-1/nodes/node-9/test');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body)).toEqual(payload);
  });

  it('rerunWorkflow POSTs run_id/start_node_id/input to the rerun endpoint', async () => {
    mockFetch.mockResolvedValue(okResponse());

    await rerunWorkflow('wf-1', { run_id: 'run-9', start_node_id: 'node-3', input: 'again' });

    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [url, init] = mockFetch.mock.calls[0];
    expect(url).toBe('/api/apps/app-42/workflows/wf-1/rerun');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body)).toEqual({ run_id: 'run-9', start_node_id: 'node-3', input: 'again' });
  });

  it('rerunWorkflow omits input when not provided', async () => {
    mockFetch.mockResolvedValue(okResponse());

    await rerunWorkflow('wf-1', { run_id: 'run-9', start_node_id: 'node-3' });

    const [, init] = mockFetch.mock.calls[0];
    expect(JSON.parse(init.body)).toEqual({ run_id: 'run-9', start_node_id: 'node-3' });
    expect(JSON.parse(init.body).input).toBeUndefined();
  });
});
