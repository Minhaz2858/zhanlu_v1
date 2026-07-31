import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('@/api/base44Client', () => ({
  base44: {
    get: vi.fn(),
  },
}));

import { marketDashboardApi } from '../marketDashboard';
import { base44 } from '@/api/base44Client';

describe('marketDashboardApi', () => {
  beforeEach(() => base44.get.mockReset());

  it('enabled() calls /market-dashboard/enabled', async () => {
    base44.get.mockResolvedValueOnce({ enabled: true });
    const r = await marketDashboardApi.enabled();
    expect(base44.get).toHaveBeenCalledWith('/market-dashboard/enabled');
    expect(r.enabled).toBe(true);
  });

  it('snapshot(group) forwards group as params', async () => {
    base44.get.mockResolvedValueOnce([]);
    await marketDashboardApi.snapshot('upstream');
    expect(base44.get).toHaveBeenCalledWith('/market-dashboard/snapshot', {
      params: { group: 'upstream' },
    });
  });

  it('snapshot() without group sends undefined', async () => {
    base44.get.mockResolvedValueOnce([]);
    await marketDashboardApi.snapshot();
    expect(base44.get).toHaveBeenCalledWith('/market-dashboard/snapshot', {
      params: { group: undefined },
    });
  });

  it('history(id, days) builds path + params', async () => {
    base44.get.mockResolvedValueOnce([]);
    await marketDashboardApi.history('naphtha', 30);
    expect(base44.get).toHaveBeenCalledWith('/market-dashboard/history/naphtha', {
      params: { days: 30 },
    });
  });
});
