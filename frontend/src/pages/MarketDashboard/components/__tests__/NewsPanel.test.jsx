import { describe, it, expect, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('@/api/marketDashboard', () => ({
  marketDashboardApi: {
    events: vi.fn().mockResolvedValue({
      events: [
        {
          event_id: 'evt-1',
          headline: 'Crude prices surge on supply concerns',
          event_type: 'price_signal',
          direction: 'up',
          impact_magnitude: 0.8,
          usefulness_score: 8.5,
          source_url: 'https://example.com/news/1',
          affected_commodities: ['naphtha', 'cracked_c5'],
          detected_at: '2026-07-31T10:00:00',
          summary: 'OPEC+ cuts output, prices rise 4%',
        },
        {
          event_id: 'evt-2',
          headline: 'Isoprene demand weakens in Q3',
          event_type: 'demand_signal',
          direction: 'down',
          impact_magnitude: 0.4,
          usefulness_score: 6.0,
          source_url: null,
          affected_commodities: ['isoprene'],
          detected_at: '2026-07-31T09:30:00',
          summary: 'Tire industry slowdowns cited',
        },
      ],
      count: 2,
    }),
  },
}));

import NewsPanel from '../NewsPanel';

function renderPanel(props) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <NewsPanel {...props} />
    </QueryClientProvider>,
  );
}

describe('NewsPanel (events-driven)', () => {
  it('renders the section header', async () => {
    renderPanel({ group: 'upstream' });
    expect(await screen.findByText(/intelligence events/i)).toBeInTheDocument();
  });

  it('renders one card per event', async () => {
    renderPanel({ group: 'upstream' });
    expect(await screen.findByText(/crude prices surge/i)).toBeInTheDocument();
    expect(await screen.findByText(/isoprene demand weakens/i)).toBeInTheDocument();
  });

  it('shows the event count in the header', async () => {
    renderPanel({ group: 'upstream' });
    expect(await screen.findByText('2 events')).toBeInTheDocument();
  });

  it('renders affected commodities as tags', async () => {
    renderPanel({ group: 'upstream' });
    expect(await screen.findByText('naphtha')).toBeInTheDocument();
    expect(await screen.findByText('isoprene')).toBeInTheDocument();
  });

  it('renders the source link as an external link', async () => {
    renderPanel({ group: 'upstream' });
    const link = await screen.findByRole('link', { name: /crude prices surge/i });
    expect(link).toHaveAttribute('href', 'https://example.com/news/1');
  });

  it('shows empty state when no events returned', async () => {
    // Override mock for this test
    const { marketDashboardApi } = await import('@/api/marketDashboard');
    marketDashboardApi.events.mockResolvedValueOnce({ events: [], count: 0 });
    renderPanel({ group: 'upstream' });
    expect(await screen.findByText(/no intelligence events/i)).toBeInTheDocument();
  });
});
