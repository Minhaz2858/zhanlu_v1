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
    expect(authFetch).toHaveBeenCalledWith('/api/market-dashboard/enabled', { method: 'GET' });
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

  it('regionalBatch(productIds) hits /regional-batch with product_ids', async () => {
    authFetch.mockResolvedValueOnce({ ok: true, json: async () => ({ items: {}, errors: {} }) });
    await marketDashboardApi.regionalBatch(['crude_oil', 'naphtha']);
    const [url] = authFetch.mock.calls[0];
    expect(url).toBe('/api/market-dashboard/regional-batch?product_ids=crude_oil%2Cnaphtha');
  });

  it('forecastBaseBatch(productIds, day) hits /forecast-base-batch', async () => {
    authFetch.mockResolvedValueOnce({ ok: true, json: async () => ({ day: 7, base_by_product: {} }) });
    await marketDashboardApi.forecastBaseBatch(['cracked_c5'], 7);
    const [url] = authFetch.mock.calls[0];
    expect(url).toContain('/api/market-dashboard/forecast-base-batch');
    expect(url).toContain('day=7');
    expect(url).toContain('product_ids=');
  });

  it('events(hours, reviewStatus, minScore) hits /events/latest', async () => {
    authFetch.mockResolvedValueOnce({ ok: true, json: async () => ({ events: [], count: 0 }) });
    await marketDashboardApi.events(168, 'approved', 0);
    const [url] = authFetch.mock.calls[0];
    expect(url).toBe('/api/market-dashboard/events/latest?hours=168&review_status=approved&min_usefulness_score=0');
  });

  it('fxRate() hits /fx/usd-cny', async () => {
    authFetch.mockResolvedValueOnce({ ok: true, json: async () => ({ price: 7.2, unit: 'CNY/USD' }) });
    await marketDashboardApi.fxRate();
    const [url] = authFetch.mock.calls[0];
    expect(url).toBe('/api/market-dashboard/fx/usd-cny');
  });
});
