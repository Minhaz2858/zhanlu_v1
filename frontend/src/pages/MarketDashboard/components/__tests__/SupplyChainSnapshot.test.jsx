import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

const { WITH_DATA, EMPTY } = vi.hoisted(() => ({
  WITH_DATA: {
    product_id: 'isoprene',
    is_live: true,
    window_start: '2026-07-16',
    window_end: '2026-08-15',
    summary: {
      sales_30d_tons: 350.0,
      inventory_tons: 60.0,
      pending_tons: 60.0,
      avg_days_pending: 6.0,
    },
    rows: [
      { grade: '45#', sku_code: 'G101000001', sales_30d_tons: 100.0,
        inventory_tons: 50.0, inventory_days: 5.0, pending_tons: 20.0 },
      { grade: '35#', sku_code: 'G101000002', sales_30d_tons: 200.0,
        inventory_tons: null, inventory_days: null, pending_tons: 40.0 },
      { grade: '55#', sku_code: 'G101000003', sales_30d_tons: 50.0,
        inventory_tons: 10.0, inventory_days: 2.0, pending_tons: null },
    ],
  },
  EMPTY: {
    product_id: 'crude_oil',
    is_live: false,
    summary: {
      sales_30d_tons: null, inventory_tons: null,
      pending_tons: null, avg_days_pending: null,
    },
    rows: [],
  },
}));

vi.mock('@/api/marketDashboard', () => ({
  marketDashboardApi: {
    supplyChain: vi.fn().mockResolvedValue(WITH_DATA),
  },
}));

import { marketDashboardApi } from '@/api/marketDashboard';
import SupplyChainSnapshot from '../SupplyChainSnapshot';

function renderPanel(props) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SupplyChainSnapshot {...props} />
    </QueryClientProvider>,
  );
}

describe('SupplyChainSnapshot', () => {
  it('renders 4 KPI cards + detail table rows from mock data', async () => {
    renderPanel({ productId: 'isoprene', language: 'en' });

    expect(await screen.findByText(/Supply Chain Snapshot/)).toBeInTheDocument();
    // KPI card labels (also appear as table headers → use findAllByText)
    expect((await screen.findAllByText('30d Sales')).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('Inventory').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('Pending').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('Avg Days Pending')).toBeInTheDocument();
    // summary values (locale en-US: 350 → "350")
    expect(screen.getByText('350')).toBeInTheDocument();
    expect(screen.getByText('6')).toBeInTheDocument();
    // detail table rows
    expect(screen.getByText('45#')).toBeInTheDocument();
    expect(screen.getByText('G101000001')).toBeInTheDocument();
  });

  it('shows -- for null inventory / pending values', async () => {
    renderPanel({ productId: 'isoprene', language: 'en' });
    // Row 2: inventory_tons null → '--'; Row 3: pending_tons null → '--'
    expect(await screen.findByText('45#')).toBeInTheDocument();
    const dashes = screen.getAllByText('--');
    expect(dashes.length).toBeGreaterThanOrEqual(2);
  });

  it('renders empty-state placeholder when rows is empty', async () => {
    marketDashboardApi.supplyChain.mockResolvedValueOnce(EMPTY);
    renderPanel({ productId: 'crude_oil', language: 'en' });
    expect(await screen.findByText('No ERP mapping for this product')).toBeInTheDocument();
  });

  it('shows the actual 30-day date range when window dates are present', async () => {
    renderPanel({ productId: 'isoprene', language: 'en' });
    const header = await screen.findByText(/live from ERP/);
    expect(header).toBeInTheDocument();
    // both bounds render with their year (Jul 16, 2026 – Aug 15, 2026)
    expect(header.textContent).toContain('2026');
  });

  it('falls back to a plain label when no window dates are returned', async () => {
    marketDashboardApi.supplyChain.mockResolvedValueOnce(EMPTY);
    renderPanel({ productId: 'crude_oil', language: 'en' });
    const label = await screen.findByText('30-day window');
    expect(label).toBeInTheDocument();
    expect(label.textContent).not.toContain('2026');
  });
});
