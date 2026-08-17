import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

const { toastMock } = vi.hoisted(() => ({ toastMock: vi.fn() }));

vi.mock('@/components/ui/use-toast', () => ({
  useToast: () => ({ toast: toastMock, dismiss: vi.fn(), toasts: [] }),
}));

vi.mock('@/api/marketDashboard', () => ({
  weeklyDigestApi: {
    get: vi.fn(),
    exportPptx: vi.fn(),
  },
}));

import ReportDetail from '../ReportDetail';
import { weeklyDigestApi } from '@/api/marketDashboard';

const REPORT = {
  id: 'card-1',
  title_zh: 'C5C9 周度总结及预测',
  title_en: 'C5C9 Weekly Summary & Forecast',
  week_start: '2026-08-10',
  week_end: '2026-08-16',
  market_overview: '本周市场整体平稳。',
  executive_summary: '价格窄幅震荡，建议观望。',
  error_msg: null,
  products: [
    {
      product_key: 'naphtha',
      product_name_zh: '石脑油',
      current_price: 6500,
      change_pct_7d: 1.2,
    },
  ],
};

function renderReport() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ReportDetail reportId="card-1" />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  weeklyDigestApi.get.mockResolvedValue(REPORT);
  URL.createObjectURL = vi.fn(() => 'blob:fake');
  URL.revokeObjectURL = vi.fn();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('ReportDetail — Export PPTX', () => {
  it('renders the Export PPTX button in the header strip', async () => {
    renderReport();
    const btn = await screen.findByRole('button', { name: /Export PPTX/i });
    expect(btn).toBeInTheDocument();
  });

  it('calls exportPptx, parses filename, and triggers an anchor download', async () => {
    weeklyDigestApi.exportPptx.mockResolvedValue({
      blob: new Blob(['x'], { type: 'application/zip' }),
      filename: 'weekly_digest_2026-08-10_default-org.pptx',
    });
    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(() => {});

    renderReport();
    const btn = await screen.findByRole('button', { name: /Export PPTX/i });
    fireEvent.click(btn);

    await waitFor(() => {
      expect(weeklyDigestApi.exportPptx).toHaveBeenCalledWith('card-1');
    });
    await waitFor(() => {
      expect(clickSpy).toHaveBeenCalled();
    });
    await waitFor(() => {
      expect(toastMock).toHaveBeenCalledWith(
        expect.objectContaining({ description: expect.stringContaining('downloaded') }),
      );
    });
    clickSpy.mockRestore();
  });

  it('shows an error toast when export fails', async () => {
    weeklyDigestApi.exportPptx.mockRejectedValue(new Error('feature disabled'));

    renderReport();
    const btn = await screen.findByRole('button', { name: /Export PPTX/i });
    fireEvent.click(btn);

    await waitFor(() => {
      expect(toastMock).toHaveBeenCalledWith(
        expect.objectContaining({ variant: 'destructive' }),
      );
    });
  });
});
