import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('@/api/marketDashboard', () => ({
  marketDashboardApi: {
    regional: vi.fn().mockResolvedValue({
      product_id: 'naphtha',
      regions: [
        { price_type: '华东', label: '华东', price: 612.5, change_pct: null,
          date: '2026-07-30', days_stale: 0, is_internal: false },
      ],
      note: 'v1 simplified: no spread/cost-floor analysis',
    }),
  },
}));

import RegionalTable from '../RegionalTable';

function renderTable(props) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <RegionalTable {...props} />
    </QueryClientProvider>,
  );
}

describe('RegionalTable', () => {
  it('renders region rows', async () => {
    renderTable({ productId: 'naphtha' });
    expect(await screen.findByText('华东')).toBeInTheDocument();
    expect(await screen.findByText('612.5')).toBeInTheDocument();
  });

  it('renders the v1 note', async () => {
    renderTable({ productId: 'naphtha' });
    expect(await screen.findByText(/v1 simplified/i)).toBeInTheDocument();
  });
});
