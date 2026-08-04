import { describe, it, expect, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('@/api/marketDashboard', () => ({
  marketDashboardApi: {
    enabled: vi.fn().mockResolvedValue({ enabled: true, reachable: true }),
    snapshot: vi.fn().mockResolvedValue([]),
    regionalBatch: vi.fn().mockResolvedValue({ items: {}, errors: {} }),
    forecastBaseBatch: vi.fn().mockResolvedValue({ day: 7, base_by_product: {} }),
    events: vi.fn().mockResolvedValue({ events: [], count: 0 }),
    fxRate: vi.fn().mockResolvedValue({
      price: 7.2, unit: 'CNY/USD', as_of_date: '2026-07-31',
      data_source: 'static', available: true,
    }),
  },
}));

import OverviewDashboard from '../OverviewDashboard';

function withQueryClient(node) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>{node}</QueryClientProvider>,
  );
}

const SAMPLE_SNAPSHOT = [
  { id: 'crude_oil', name: 'Brent', price: 84.09, date: '2026-07-31',
    change_pct: 0.6, group: 'upstream', unit: 'USD/bbl' },
  { id: 'naphtha', name: 'Japanese Naphtha', price: 825, date: '2026-07-31',
    change_pct: 0.4, group: 'upstream', unit: 'CNY/mt' },
  { id: 'cracked_c5', name: 'Cracked C5', price: 5700, date: '2026-07-31',
    change_pct: -0.3, group: 'upstream', unit: 'CNY/mt' },
  { id: 'isoprene', name: 'Isoprene', price: 8100, date: '2026-07-31',
    change_pct: 0.5, group: 'midstream', unit: 'CNY/mt' },
];

describe('OverviewDashboard', () => {
  it('renders the EDIA overview header', () => {
    withQueryClient(<OverviewDashboard snapshot={SAMPLE_SNAPSHOT} />);
    expect(screen.getByText(/Chemical Product Market Overview/i)).toBeInTheDocument();
  });

  it('renders the upstream/midstream/downstream price card headings', () => {
    withQueryClient(<OverviewDashboard snapshot={SAMPLE_SNAPSHOT} />);
    expect(screen.getAllByText(/Upstream/i).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/Midstream/i).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/Downstream/i).length).toBeGreaterThanOrEqual(1);
  });

  it('renders the product-name labels using PRODUCT_LABELS lookup', () => {
    withQueryClient(<OverviewDashboard snapshot={SAMPLE_SNAPSHOT} />);
    expect(screen.getAllByText(/Brent Crude Oil/i).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/Japanese Naphtha/i).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/Cracked C5/i).length).toBeGreaterThanOrEqual(1);
  });

  it('renders the forecast and intelligence section headings', () => {
    withQueryClient(<OverviewDashboard snapshot={SAMPLE_SNAPSHOT} />);
    expect(screen.getByText(/Prices of Chemical Products/i)).toBeInTheDocument();
    expect(screen.getByText(/7-Day AI Price Outlook/i)).toBeInTheDocument();
  });

  it('gracefully handles an empty snapshot', () => {
    withQueryClient(<OverviewDashboard snapshot={[]} />);
    expect(screen.getByText(/Chemical Product Market Overview/i)).toBeInTheDocument();
  });

  it('renders the sidebar REPORT DATE block in English by default', () => {
    withQueryClient(<OverviewDashboard snapshot={SAMPLE_SNAPSHOT} />);
    expect(screen.getAllByText(/Report Date/i).length).toBeGreaterThanOrEqual(1);
  });

  it('renders sidebar MARKET INDICATORS panel header', () => {
    withQueryClient(<OverviewDashboard snapshot={SAMPLE_SNAPSHOT} />);
    expect(screen.getAllByText(/Market Indicators/i).length).toBeGreaterThanOrEqual(1);
  });

  it('renders footer with sources note', () => {
    withQueryClient(<OverviewDashboard snapshot={SAMPLE_SNAPSHOT} />);
    expect(screen.getByText(/Sources: Longzhong/i)).toBeInTheDocument();
  });

  it('renders industry intelligence section heading', async () => {
    withQueryClient(<OverviewDashboard snapshot={SAMPLE_SNAPSHOT} />);
    expect(screen.getByText(/Industry Intelligence/i)).toBeInTheDocument();
  });

  it('renders a "no events" placeholder when events endpoint returns 0', async () => {
    withQueryClient(<OverviewDashboard snapshot={SAMPLE_SNAPSHOT} />);
    await waitFor(() => {
      expect(screen.queryByText(/No recent industry events/i)).toBeInTheDocument();
    });
  });
});
