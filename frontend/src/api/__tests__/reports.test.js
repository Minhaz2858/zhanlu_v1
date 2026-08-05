import { describe, it, expect, vi, beforeEach } from 'vitest';

// hoisted mock: factory runs before imports, so variable is safe
const { mockFetch, mockGetToken } = vi.hoisted(() => ({
  mockFetch: vi.fn(),
  mockGetToken: vi.fn().mockReturnValue('test-token'),
}));

vi.mock('@/api/authFetch', () => ({
  authFetch: mockFetch,
  getAuthToken: mockGetToken,
}));

import { reportsApi, streamContractAiAnalysis } from '../reports';

beforeEach(() => {
  vi.clearAllMocks();
});

// ── customerSales ─────────────────────────────────────────────────────

describe('reportsApi.customerSales', () => {
  it('calls the correct URL with default org', async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ month: '2025-06', summary: {} }),
      status: 200,
    });

    await reportsApi.customerSales({ org: 'huizhou' });
    const url = mockFetch.mock.calls[0][0];
    expect(url).toContain('/api/reports/customer-sales');
    expect(url).toContain('org=huizhou');
  });

  it('includes month and material in query string', async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ month: '2025-06', summary: {} }),
      status: 200,
    });

    await reportsApi.customerSales({ org: 'huizhou', month: '2025-06', material: 'C5树脂' });
    const url = mockFetch.mock.calls[0][0];
    expect(url).toContain('month=2025-06');
    expect(url).toContain('material=C5%E6%A0%91%E8%84%82');
  });

  it('does not include empty params', async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ month: '2025-06', summary: {} }),
      status: 200,
    });

    await reportsApi.customerSales({ org: 'huizhou' });
    const url = mockFetch.mock.calls[0][0];
    expect(url.includes('month=')).toBe(false);
    expect(url.includes('material=')).toBe(false);
  });

  it('throws on non-ok response with detail', async () => {
    mockFetch.mockResolvedValue({
      ok: false,
      json: async () => ({ detail: 'Unknown org' }),
      status: 400,
    });

    await expect(
      reportsApi.customerSales({ org: 'bogus' }),
    ).rejects.toThrow('Unknown org');
  });
});

// ── contractPerformance ────────────────────────────────────────────────

describe('reportsApi.contractPerformance', () => {
  it('calls correct URL with date range', async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ summary: {} }),
      status: 200,
    });

    await reportsApi.contractPerformance({
      org: 'huizhou',
      start_date: '2025-07-01',
      end_date: '2025-07-31',
    });

    const url = mockFetch.mock.calls[0][0];
    expect(url).toContain('/api/reports/contract-performance');
    expect(url).toContain('start_date=2025-07-01');
    expect(url).toContain('end_date=2025-07-31');
  });
});

// ── SSE Stream parsing ─────────────────────────────────────────────────

describe('streamContractAiAnalysis', () => {
  function buildReadableStream(frames) {
    const encoder = new TextEncoder();
    return new ReadableStream({
      start(controller) {
        for (const frame of frames) {
          controller.enqueue(encoder.encode(frame));
        }
        controller.close();
      },
    });
  }

  it('parses meta + delta + done events', async () => {
    const frames = [
      'event: meta\ndata: {"status":"started"}\n\n',
      'event: delta\ndata: {"field":"chart_top","text":"分析结果"}\n\n',
      'event: done\ndata: {"chart_top":"分析结果"}\n\n',
    ];

    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      body: buildReadableStream(frames),
    });

    const meta = [];
    const deltas = [];
    let result = null;

    await streamContractAiAnalysis(
      { org: 'huizhou' },
      {
        onMeta: (s) => meta.push(s),
        onDelta: (f, t) => deltas.push({ field: f, text: t }),
        onBullets: () => {},
        onDone: (r) => { result = r; },
        onError: () => {},
      },
    );

    expect(meta).toEqual(['started']);
    expect(deltas.length).toBe(1);
    expect(deltas[0].field).toBe('chart_top');
    expect(deltas[0].text).toBe('分析结果');
    expect(result).toBeDefined();
    expect(result.chart_top).toBe('分析结果');
  });

  it('parses partial event for bullets', async () => {
    const frames = [
      'event: meta\ndata: {"status":"started"}\n\n',
      'event: partial\ndata: {"summary_bullets":["要点1","要点2"]}\n\n',
      'event: done\ndata: {}\n\n',
    ];

    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      body: buildReadableStream(frames),
    });

    let bullets = [];
    await streamContractAiAnalysis(
      { org: 'huizhou' },
      {
        onMeta: () => {},
        onDelta: () => {},
        onBullets: (b) => { bullets = b; },
        onDone: () => {},
        onError: () => {},
      },
    );

    expect(bullets).toEqual(['要点1', '要点2']);
  });

  it('handles error event', async () => {
    const frames = [
      'event: meta\ndata: {"status":"started"}\n\n',
      'event: error\ndata: {"message":"LLM unavailable"}\n\n',
    ];

    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      body: buildReadableStream(frames),
    });

    let errorMsg = '';
    await streamContractAiAnalysis(
      { org: 'huizhou' },
      {
        onMeta: () => {},
        onDelta: () => {},
        onBullets: () => {},
        onDone: () => {},
        onError: (m) => { errorMsg = m; },
      },
    );

    expect(errorMsg).toBe('LLM unavailable');
  });

  it('handles HTTP error', async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: false,
      json: async () => ({ detail: 'edia_unavailable' }),
    });

    let errorMsg = '';
    await streamContractAiAnalysis(
      { org: 'huizhou' },
      {
        onMeta: () => {},
        onDelta: () => {},
        onBullets: () => {},
        onDone: () => {},
        onError: (m) => { errorMsg = m; },
      },
    );

    expect(errorMsg).toBe('edia_unavailable');
  });
});
