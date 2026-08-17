import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('../components/ReportView', () => ({
  default: () => <div data-testid="mock-report-view">Report Section</div>,
}));

vi.mock('@/api/marketDashboard', () => ({
  marketDashboardApi: {
    enabled: vi.fn().mockResolvedValue({ enabled: true, reachable: true, reason: 'ok' }),
    snapshot: vi.fn().mockResolvedValue([]),
    history: vi.fn().mockResolvedValue([]),
    regional: vi.fn().mockResolvedValue({ product_id: '', regions: [] }),
    regionalBatch: vi.fn().mockResolvedValue({ items: {}, errors: {} }),
    supplyChain: vi.fn().mockResolvedValue({ product_id: '', rows: [], summary: null, is_live: false }),
    news: vi.fn().mockResolvedValue({ items: [], note: '' }),
    forecastBaseBatch: vi.fn().mockResolvedValue({ day: 7, base_by_product: {} }),
    decisionBoard: vi.fn().mockResolvedValue({ group: '', day: 7, generated_at: '', briefing_zh: '', products: [] }),
    forecastChart: vi.fn().mockResolvedValue({
      product_id: 'x',
      points: [
        { date: '2026-07-30', zone: 'past', actual: 6100 },
        { date: '2026-07-31', zone: 'past', actual: 6120 },
        { date: '2026-08-01', zone: 'forecast', base: 6130, bull: 6200, bear: 6050 },
      ],
      as_of_date: '2026-07-31',
      previous_ai_date: null,
      executed_in_range_count: 0,
      executed_outside_range_count: 0,
    }),
    priceChain: vi.fn().mockResolvedValue({ tiers: [] }),
    events: vi.fn().mockResolvedValue({ events: [], count: 0 }),
    fxRate: vi.fn().mockResolvedValue({
      price: 7.2, unit: 'CNY/USD', as_of_date: '2026-07-31',
      data_source: 'static_fallback', available: true,
    }),
  },
}));

import MarketDashboardPage from '../MarketDashboardPage';
import { marketDashboardApi } from '@/api/marketDashboard';

const MIDSTREAM_SNAPSHOT = [
  { id: 'isoprene', name: 'Isoprene', price: 8100, date: '2026-07-30',
    change_pct: 0.4, group: 'midstream', unit: 'CNY/mt' },
  { id: 'piperylene', name: 'Piperylene', price: 7900, date: '2026-07-30',
    change_pct: -0.2, group: 'midstream', unit: 'CNY/mt' },
  { id: 'dcpd', name: 'DCPD', price: 6600, date: '2026-07-30',
    change_pct: 1.1, group: 'midstream', unit: 'CNY/mt' },
  { id: 'cracked_c9', name: 'Cracked C9', price: 5800, date: '2026-07-30',
    change_pct: 0.0, group: 'midstream', unit: 'CNY/mt' },
];

function renderAt(path) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/market-dashboard/:section" element={<MarketDashboardPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('MarketDashboardPage — unified section layout', () => {
  it('upstream renders the observed-data strip and the overlay forecast chart', async () => {
    renderAt('/market-dashboard/upstream');
    expect(await screen.findByText('Observed Data')).toBeInTheDocument();
    expect(
      await screen.findByText(/Forecast for next 30 days — Brent Crude Oil/i),
    ).toBeInTheDocument();
  });

  it('product sections own their vertical scroll inside the locked AppLayout main', async () => {
    // AppLayout's <main> has overflow-hidden, so each product page must put
    // `h-full overflow-y-auto` on its root wrapper or the bottom content
    // (price chain tail, regional table) is unreachable.
    const { container } = renderAt('/market-dashboard/upstream');
    await screen.findByText('Observed Data');
    const root = container.querySelector('.h-full.overflow-y-auto');
    expect(root).not.toBeNull();
    // And the scroll container must wrap the observed-data strip.
    expect(root.contains(screen.getByText('Observed Data'))).toBe(true);
  });

  it('midstream renders the same strip + overlay chart with snapshot product names', async () => {
    marketDashboardApi.snapshot.mockResolvedValue(MIDSTREAM_SNAPSHOT);
    renderAt('/market-dashboard/midstream');
    expect(await screen.findByText('Observed Data')).toBeInTheDocument();
    // Strip buttons carry the snapshot names
    expect(await screen.findByText('Piperylene')).toBeInTheDocument();
    // First snapshot product auto-selected → chart titled with its name
    expect(
      await screen.findByText(/Forecast for next 30 days — Isoprene/i),
    ).toBeInTheDocument();
  });

  it('does not render the 7-day price outlook card on product sections', async () => {
    marketDashboardApi.snapshot.mockResolvedValue(MIDSTREAM_SNAPSHOT);
    renderAt('/market-dashboard/midstream');
    await screen.findByText('Observed Data');
    expect(screen.queryByText(/7-day price outlook/i)).not.toBeInTheDocument();
  });

  it('renders a single regional table for the selected product (not one per product)', async () => {
    marketDashboardApi.snapshot.mockResolvedValue(MIDSTREAM_SNAPSHOT);
    renderAt('/market-dashboard/midstream');
    await screen.findByText(/Forecast for next 30 days — Isoprene/i);
    const tables = await screen.findAllByText(/— Regional/i);
    expect(tables).toHaveLength(1);
    expect(tables[0].textContent).toMatch(/isoprene — Regional/i);
  });

  it('clicking a product in the strip switches the chart to that product', async () => {
    marketDashboardApi.snapshot.mockResolvedValue(MIDSTREAM_SNAPSHOT);
    renderAt('/market-dashboard/midstream');
    const btn = await screen.findByText('Piperylene');
    fireEvent.click(btn);
    await waitFor(() => {
      expect(marketDashboardApi.forecastChart).toHaveBeenCalledWith('piperylene', 30);
    });
    expect(
      await screen.findByText(/Forecast for next 30 days — Piperylene/i),
    ).toBeInTheDocument();
  });

  it('renders the price transmission chain only on upstream', async () => {
    marketDashboardApi.snapshot.mockResolvedValue(MIDSTREAM_SNAPSHOT);
    renderAt('/market-dashboard/midstream');
    await screen.findByText('Observed Data');
    await waitFor(() => {
      expect(marketDashboardApi.forecastChart).toHaveBeenCalled();
    });
    expect(marketDashboardApi.priceChain).not.toHaveBeenCalled();
  });
});

describe('MarketDashboardPage — other sections', () => {
  it('renders invalid-section fallback', async () => {
    renderAt('/market-dashboard/bogus');
    expect(await screen.findByText(/unknown section/i)).toBeInTheDocument();
  });

  it('renders Report section via ReportView', async () => {
    renderAt('/market-dashboard/report');
    expect(await screen.findByTestId('mock-report-view')).toBeInTheDocument();
  });

  it('renders the EDIA overview layout header (not the per-section header)', async () => {
    renderAt('/market-dashboard/overview');
    expect(await screen.findByText(/Chemical Product Market Overview/i)).toBeInTheDocument();
  });

  it('does NOT render intelligence events on product sections (overview-only)', async () => {
    marketDashboardApi.events.mockResolvedValue({
      events: [
        {
          event_id: 'evt-1',
          headline: 'Test event',
          event_type: 'price_signal',
          direction: 'up',
          usefulness_score: 5.0,
          affected_commodities: ['naphtha'],
          detected_at: '2026-07-31T10:00:00',
        },
      ],
      count: 1,
    });
    renderAt('/market-dashboard/upstream');
    // Page fully renders (chart title appears); events panel must be absent.
    expect(
      await screen.findByText(/Forecast for next 30 days/i),
    ).toBeInTheDocument();
    expect(screen.queryByText('Test event')).not.toBeInTheDocument();
    expect(screen.queryByText(/Intelligence Events/i)).not.toBeInTheDocument();
  });
});
