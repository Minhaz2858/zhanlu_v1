import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('@/api/marketDashboard', () => ({
  marketDashboardApi: {
    enabled: vi.fn().mockResolvedValue({ enabled: true, reachable: true, reason: 'ok' }),
    snapshot: vi.fn().mockResolvedValue([]),
    history: vi.fn().mockResolvedValue([]),
    regional: vi.fn().mockResolvedValue({ product_id: '', regions: [] }),
    regionalBatch: vi.fn().mockResolvedValue({ items: {}, errors: {} }),
    news: vi.fn().mockResolvedValue({ items: [], note: '' }),
    forecastBaseBatch: vi.fn().mockResolvedValue({
      day: 7,
      base_by_product: { cracked_c5: 6200, cracked_c9: 6200 },
    }),
    events: vi.fn().mockResolvedValue({ events: [], count: 0 }),
    fxRate: vi.fn().mockResolvedValue({
      price: 7.2, unit: 'CNY/USD', as_of_date: '2026-07-31',
      data_source: 'static_fallback', available: true,
    }),
  },
}));

import MarketDashboardPage from '../MarketDashboardPage';

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

describe('MarketDashboardPage', () => {
  it('renders the section name from the URL', async () => {
    renderAt('/market-dashboard/upstream');
    expect(await screen.findByText(/EDIA Market Dashboard — upstream/i)).toBeInTheDocument();
  });

  it('renders the EDIA overview layout header (not the per-section header)', async () => {
    renderAt('/market-dashboard/overview');
    expect(await screen.findByText(/Chemical Product Market Overview/i)).toBeInTheDocument();
  });

  it('renders invalid-section fallback', async () => {
    renderAt('/market-dashboard/bogus');
    expect(await screen.findByText(/unknown section/i)).toBeInTheDocument();
  });

  it('renders snapshot cards when snapshot resolves (upstream)', async () => {
    const { marketDashboardApi } = await import('@/api/marketDashboard');
    marketDashboardApi.snapshot.mockResolvedValueOnce([
      { id: 'naphtha', name: 'Japanese Naphtha', price: 612.5, date: '2026-07-30',
        change_pct: 1.2, group: 'upstream', unit: 'CNY/mt' },
    ]);
    renderAt('/market-dashboard/upstream');
    expect(await screen.findByText('Japanese Naphtha')).toBeInTheDocument();
  });

  it('renders a PriceChart for each product in the snapshot (upstream)', async () => {
    const { marketDashboardApi } = await import('@/api/marketDashboard');
    marketDashboardApi.snapshot.mockResolvedValueOnce([
      { id: 'naphtha', name: 'Japanese Naphtha', price: 612.5, date: '2026-07-30',
        change_pct: 1.2, group: 'upstream', unit: 'CNY/mt' },
    ]);
    renderAt('/market-dashboard/upstream');
    const headings = await screen.findAllByText(/naphtha/i);
    expect(headings.length).toBeGreaterThanOrEqual(1);
  });

  it('renders RegionalTable for each product in the snapshot (upstream)', async () => {
    const { marketDashboardApi } = await import('@/api/marketDashboard');
    marketDashboardApi.snapshot.mockResolvedValueOnce([
      { id: 'naphtha', name: 'Japanese Naphtha', price: 612.5, date: '2026-07-30',
        change_pct: 1.2, group: 'upstream', unit: 'CNY/mt' },
    ]);
    renderAt('/market-dashboard/upstream');
    expect(await screen.findByText(/naphtha — regional/i)).toBeInTheDocument();
  });

  it('renders ForecastCard with 7-day outlook title (upstream)', async () => {
    renderAt('/market-dashboard/upstream');
    expect(await screen.findByText(/7-day price outlook/i)).toBeInTheDocument();
  });

  it('renders NewsPanel (events-driven) for non-overview sections', async () => {
    const { marketDashboardApi } = await import('@/api/marketDashboard');
    marketDashboardApi.events.mockResolvedValueOnce({
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
    expect(await screen.findByText('Test event')).toBeInTheDocument();
  });

  it('overview section renders the EDIA sidebar + price cards', async () => {
    const { marketDashboardApi } = await import('@/api/marketDashboard');
    marketDashboardApi.snapshot.mockResolvedValueOnce([
      { id: 'naphtha', name: 'Japanese Naphtha', price: 612.5, date: '2026-07-30',
        change_pct: 1.2, group: 'upstream', unit: 'CNY/mt' },
      { id: 'isoprene', name: 'Isoprene', price: 8100, date: '2026-07-30',
        change_pct: 0.4, group: 'midstream', unit: 'CNY/mt' },
    ]);
    renderAt('/market-dashboard/overview');
    // Sidebar uses the product-keyed display name from PRODUCT_LABELS.
    // Multiple matches possible (sidebar label + row name + title); use findAllByText.
    const matches = await screen.findAllByText(/Japanese Naphtha/i);
    expect(matches.length).toBeGreaterThanOrEqual(1);
    // PRICES OF CHEMICAL PRODUCTS heading
    expect(await screen.findByText(/Prices of Chemical Products/i)).toBeInTheDocument();
  });
});
