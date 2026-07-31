import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('@/api/marketDashboard', () => ({
  marketDashboardApi: {
    history: vi.fn().mockResolvedValue([
      { date: '2026-07-29', price: 600.0 },
      { date: '2026-07-30', price: 612.5 },
    ]),
  },
}));

// recharts ResponsiveContainer requires a sized parent; mock to a div.
vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }) => <div data-testid="rc">{children}</div>,
  ComposedChart: () => <div data-testid="composed" />,
  Area: () => null,
  Line: () => null,
  XAxis: () => null,
  YAxis: () => null,
  Tooltip: () => null,
}));

import PriceChart from '../PriceChart';
import { marketDashboardApi } from '@/api/marketDashboard';

function renderChart(props) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <PriceChart {...props} />
    </QueryClientProvider>,
  );
}

describe('PriceChart', () => {
  it('renders the product id as a heading', async () => {
    renderChart({ productId: 'naphtha', days: 30 });
    expect(await screen.findByTestId('composed')).toBeInTheDocument();
    expect(screen.getByText(/naphtha/i)).toBeInTheDocument();
  });

  it('calls history() with productId + days', async () => {
    renderChart({ productId: 'naphtha', days: 30 });
    await screen.findByTestId('composed');
    expect(marketDashboardApi.history).toHaveBeenCalledWith('naphtha', 30);
  });

  it('renders empty-state message when history is empty', async () => {
    marketDashboardApi.history.mockResolvedValueOnce([]);
    renderChart({ productId: 'crude_oil', days: 30 });
    expect(await screen.findByText(/no history/i)).toBeInTheDocument();
  });
});
