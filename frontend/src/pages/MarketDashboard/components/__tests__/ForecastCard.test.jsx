import { describe, it, expect, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('@/api/marketDashboard', () => ({
  marketDashboardApi: {
    forecastBaseBatch: vi.fn().mockResolvedValue({
      day: 7,
      base_by_product: {
        cracked_c5: 6200.0,
        cracked_c9: 6200.0,
        naphtha: null,
      },
    }),
    fxRate: vi.fn().mockResolvedValue({
      price: 7.2,
      unit: "CNY/USD",
      as_of_date: "2026-07-31",
      data_source: "static_fallback",
      available: true,
    }),
  },
}));

import ForecastCard from '../ForecastCard';

function renderCard(props) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ForecastCard {...props} />
    </QueryClientProvider>,
  );
}

describe('ForecastCard', () => {
  it('renders a row per product with forecast price in CNY', async () => {
    renderCard();
    await waitFor(() => {
      expect(screen.getByText('Cracked C5')).toBeInTheDocument();
    });
    expect(screen.getByText('Cracked C9')).toBeInTheDocument();
    // Both products share 6,200 — verify the count of CNY price elements.
    expect(screen.getAllByText(/6,200/).length).toBeGreaterThanOrEqual(2);
  });

  it('shows the day count in the header', async () => {
    renderCard({ day: 7 });
    expect(await screen.findByText(/Day 7/)).toBeInTheDocument();
  });

  it('converts to USD when FX rate is available', async () => {
    renderCard();
    // 6200 CNY / 7.2 USD/CNY = 861.11 USD — both products have a USD line.
    await waitFor(() => {
      expect(screen.getAllByText(/861/).length).toBeGreaterThanOrEqual(2);
    });
  });

  it('renders the static-fallback note when FX data_source is static_fallback', async () => {
    renderCard();
    expect(await screen.findByText(/static/)).toBeInTheDocument();
  });

  it('renders a custom title when provided', async () => {
    renderCard({ title: 'AI Outlook' });
    expect(await screen.findByText('AI Outlook')).toBeInTheDocument();
  });
});
