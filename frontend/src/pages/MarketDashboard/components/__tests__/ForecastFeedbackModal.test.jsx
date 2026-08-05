import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import ForecastFeedbackModal from '../ForecastFeedbackModal';

vi.mock('@/api/marketDashboard', () => ({
  marketDashboardApi: {
    submitForecastFeedback: vi.fn(),
    listForecastFeedback: vi.fn(),
  },
}));

import { marketDashboardApi } from '@/api/marketDashboard';

beforeEach(() => {
  vi.clearAllMocks();
  marketDashboardApi.listForecastFeedback.mockResolvedValue([]);
});

describe('ForecastFeedbackModal', () => {
  it('shows the AI price read-only and submits a positive user price', async () => {
    marketDashboardApi.submitForecastFeedback.mockResolvedValue({ id: '1', status: 'pending' });
    render(<ForecastFeedbackModal productId="异戊二烯" aiPrice={10388} onClose={() => {}} />);
    expect(screen.getByDisplayValue('10388')).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/我认为应该是/i), { target: { value: '11000' } });
    fireEvent.click(screen.getByRole('button', { name: /提交/i }));
    await waitFor(() => expect(marketDashboardApi.submitForecastFeedback).toHaveBeenCalled());
    const args = marketDashboardApi.submitForecastFeedback.mock.calls[0][0];
    expect(args.user_price).toBe(11000);
  });

  it('rejects zero user price', () => {
    render(<ForecastFeedbackModal productId="异戊二烯" aiPrice={10388} onClose={() => {}} />);
    fireEvent.change(screen.getByLabelText(/我认为应该是/i), { target: { value: '0' } });
    fireEvent.click(screen.getByRole('button', { name: /提交/i }));
    expect(marketDashboardApi.submitForecastFeedback).not.toHaveBeenCalled();
    expect(screen.getByText(/必须为正数/i)).toBeInTheDocument();
  });
});
