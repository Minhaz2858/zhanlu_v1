import { describe, it, expect, vi, beforeEach } from 'vitest';

const { mockFetch } = vi.hoisted(() => ({
  mockFetch: vi.fn(),
}));

vi.mock('@/api/authFetch', () => ({
  authFetch: mockFetch,
}));

import { getProjectKnowledgeMap } from '../projectCatalog';

beforeEach(() => {
  vi.clearAllMocks();
});

describe('getProjectKnowledgeMap', () => {
  it('calls the knowledge-map endpoint for the project', async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ summary: { resource_count: 3 } }),
    });

    await getProjectKnowledgeMap('p-123');

    expect(mockFetch).toHaveBeenCalledTimes(1);
    expect(mockFetch.mock.calls[0][0]).toContain('/projects/p-123/knowledge-map');
  });

  it('throws when the endpoint returns non-ok', async () => {
    mockFetch.mockResolvedValue({ ok: false, status: 500, json: async () => ({}) });

    await expect(getProjectKnowledgeMap('p-123')).rejects.toThrow('Failed to load project knowledge map');
  });
});
