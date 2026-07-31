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
    news: vi.fn().mockResolvedValue({ items: [], note: '' }),
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
    expect(await screen.findByText(/upstream/i)).toBeInTheDocument();
  });

  it('renders overview section header', async () => {
    renderAt('/market-dashboard/overview');
    expect(await screen.findByText(/overview/i)).toBeInTheDocument();
  });

  it('renders invalid-section fallback', async () => {
    renderAt('/market-dashboard/bogus');
    expect(await screen.findByText(/unknown section/i)).toBeInTheDocument();
  });
});
