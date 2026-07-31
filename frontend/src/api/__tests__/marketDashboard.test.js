import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('@/api/authFetch', () => ({
  authFetch: vi.fn(),
}));

import { marketDashboardApi } from '../marketDashboard';
import { authFetch } from '@/api/authFetch';

describe('marketDashboardApi', () => {
  beforeEach(() => authFetch.mockReset());

  it('enabled() hits /api/market-dashboard/enabled and returns the JSON body', async () => {
    authFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ enabled: true, reachable: true, reason: 'ok' }),
    });
    const r = await marketDashboardApi.enabled();
    expect(authFetch).toHaveBeenCalledWith('/api/market-dashboard/enabled', {
      method: 'GET',
    });
    expect(r).toEqual({ enabled: true, reachable: true, reason: 'ok' });
  });

  it('snapshot(group) appends group as a query string', async () => {
    authFetch.mockResolvedValueOnce({ ok: true, json: async () => [] });
    await marketDashboardApi.snapshot('upstream');
    const [url, opts] = authFetch.mock.calls[0];
    expect(url).toBe('/api/market-dashboard/snapshot?group=upstream');
    expect(opts.method).toBe('GET');
  });

  it('snapshot() without group sends no query string', async () => {
    authFetch.mockResolvedValueOnce({ ok: true, json: async () => [] });
    await marketDashboardApi.snapshot();
    const [url, opts] = authFetch.mock.calls[0];
    expect(url).toBe('/api/market-dashboard/snapshot');
    expect(opts.method).toBe('GET');
  });

  it('history(id, days) builds path + days query', async () => {
    authFetch.mockResolvedValueOnce({ ok: true, json: async () => [] });
    await marketDashboardApi.history('naphtha', 30);
    const [url, opts] = authFetch.mock.calls[0];
    expect(url).toBe('/api/market-dashboard/history/naphtha?days=30');
    expect(opts.method).toBe('GET');
  });

  it('throws when the server returns 503 (fail-closed)', async () => {
    authFetch.mockResolvedValueOnce({
      ok: false,
      status: 503,
      json: async () => ({ detail: 'edia_unavailable' }),
    });
    await expect(marketDashboardApi.enabled()).rejects.toThrow(/edia_unavailable/);
  });
});
