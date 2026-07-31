import { describe, it, expect, vi, beforeEach } from 'vitest';

const authFetch = vi.fn();
vi.mock('@/api/authFetch', () => ({ authFetch: (...a) => authFetch(...a) }));

import {
  listDashboards,
  getDashboard,
  createDashboard,
  deleteDashboard,
  queryDashboard,
  updateDashboard,
  previewWidgetSql,
} from './dashboards';

beforeEach(() => { authFetch.mockReset(); });

describe('dashboards api', () => {
  it('listDashboards appends project_id', async () => {
    authFetch.mockResolvedValue({ ok: true, json: async () => [] });
    await listDashboards('p1');
    expect(authFetch).toHaveBeenCalledWith('/api/dashboards?project_id=p1', expect.objectContaining({ method: 'GET' }));
  });

  it('getDashboard', async () => {
    authFetch.mockResolvedValue({ ok: true, json: async () => ({ id: 'd1' }) });
    const d = await getDashboard('d1');
    expect(d.id).toBe('d1');
    expect(authFetch).toHaveBeenCalledWith('/api/dashboards/d1', expect.objectContaining({ method: 'GET' }));
  });

  it('createDashboard posts body', async () => {
    authFetch.mockResolvedValue({ ok: true, json: async () => ({ id: 'd2' }) });
    await createDashboard({ name: 'x' });
    expect(authFetch).toHaveBeenCalledWith('/api/dashboards', expect.objectContaining({ method: 'POST', body: '{"name":"x"}' }));
  });

  it('queryDashboard posts empty', async () => {
    authFetch.mockResolvedValue({ ok: true, json: async () => ({ results: {} }) });
    await queryDashboard('d1');
    expect(authFetch).toHaveBeenCalledWith('/api/dashboards/d1/query', expect.objectContaining({ method: 'POST' }));
  });

  it('queryDashboard posts params as JSON body', async () => {
    authFetch.mockResolvedValue({ ok: true, json: async () => ({ results: {} }) });
    await queryDashboard('d1', { from: '2026-07-01', filters: { region: 'Asia' } });
    const [url, opts] = authFetch.mock.calls[0];
    expect(url).toBe('/api/dashboards/d1/query');
    expect(opts.method).toBe('POST');
    expect(opts.headers['Content-Type']).toBe('application/json');
    expect(JSON.parse(opts.body)).toEqual({ from: '2026-07-01', filters: { region: 'Asia' } });
  });

  it('queryDashboard defaults to empty body when no params', async () => {
    authFetch.mockResolvedValue({ ok: true, json: async () => ({ results: {} }) });
    await queryDashboard('d1');
    expect(JSON.parse(authFetch.mock.calls[0][1].body)).toEqual({});
  });

  it('deleteDashboard', async () => {
    authFetch.mockResolvedValue({ ok: true, status: 204 });
    await deleteDashboard('d1');
    expect(authFetch).toHaveBeenCalledWith('/api/dashboards/d1', expect.objectContaining({ method: 'DELETE' }));
  });

  it('updateDashboard patches body', async () => {
    authFetch.mockResolvedValue({ ok: true, json: async () => ({ id: 'd1', name: 'x' }) });
    await updateDashboard('d1', { name: 'x', definition: { widgets: [] } });
    const [url, opts] = authFetch.mock.calls[0];
    expect(url).toBe('/api/dashboards/d1');
    expect(opts.method).toBe('PATCH');
    expect(opts.headers['Content-Type']).toBe('application/json');
    expect(JSON.parse(opts.body)).toEqual({ name: 'x', definition: { widgets: [] } });
  });

  it('previewWidgetSql posts sql + params', async () => {
    authFetch.mockResolvedValue({ ok: true, json: async () => ({ columns: ['n'], rows: [{ n: 1 }], error: null, truncated: false }) });
    const r = await previewWidgetSql('d1', { sql: 'SELECT 1 AS n', params: { from: '2026-07-01' } });
    const [url, opts] = authFetch.mock.calls[0];
    expect(url).toBe('/api/dashboards/d1/preview-sql');
    expect(opts.method).toBe('POST');
    expect(JSON.parse(opts.body)).toEqual({ sql: 'SELECT 1 AS n', params: { from: '2026-07-01' } });
    expect(r.columns).toEqual(['n']);
  });

  it('previewWidgetSql defaults params to empty', async () => {
    authFetch.mockResolvedValue({ ok: true, json: async () => ({ columns: [], rows: [], error: null, truncated: false }) });
    await previewWidgetSql('d1', { sql: 'SELECT 1' });
    expect(JSON.parse(authFetch.mock.calls[0][1].body)).toEqual({ sql: 'SELECT 1' });
  });
});
