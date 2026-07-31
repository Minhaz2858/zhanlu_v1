import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('@/api/marketDashboard', () => ({
  marketDashboardApi: {
    news: vi.fn().mockResolvedValue({
      items: [],
      note: "Industry news requires EDIA's AI event pipeline (not part of this v1 read-only mirror).",
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

describe('NewsPanel', () => {
  it('renders the v1 info banner when items is empty', async () => {
    renderPanel({ group: 'upstream' });
    expect(await screen.findByText(/AI event pipeline/i)).toBeInTheDocument();
  });

  it('renders the section header', async () => {
    renderPanel({ group: 'midstream' });
    expect(await screen.findByText(/industry news/i)).toBeInTheDocument();
  });
});
