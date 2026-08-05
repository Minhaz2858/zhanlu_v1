import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

// Mock the two report components to keep this test focused on tab switching
vi.mock('../CustomerSalesReport', () => ({
  default: ({ org }) => <div data-testid="customer-sales-report">CustomerSales org={org}</div>,
}));
vi.mock('../ContractPerformanceReport', () => ({
  default: ({ org }) => <div data-testid="contract-performance-report">ContractPerf org={org}</div>,
}));

import ReportView from '../ReportView';

function renderReportView() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ReportView />
    </QueryClientProvider>,
  );
}

describe('ReportView', () => {
  it('renders Customer Sales tab by default', () => {
    renderReportView();
    expect(screen.getByTestId('customer-sales-report')).toBeInTheDocument();
    const contractPerf = screen.queryByTestId('contract-performance-report');
    expect(contractPerf).toBeNull();
  });

  it('switches to Contract Performance tab on click', () => {
    renderReportView();
    fireEvent.click(screen.getByText('销售合同执行情况'));
    expect(screen.getByTestId('contract-performance-report')).toBeInTheDocument();
    const cs = screen.queryByTestId('customer-sales-report');
    expect(cs).toBeNull();
  });

  it('switches back to Customer Sales tab', () => {
    renderReportView();
    // Go to Contract Performance first
    fireEvent.click(screen.getByText('销售合同执行情况'));
    expect(screen.getByTestId('contract-performance-report')).toBeInTheDocument();

    // Go back to Customer Sales
    fireEvent.click(screen.getByText('客户销售情况'));
    expect(screen.getByTestId('customer-sales-report')).toBeInTheDocument();
  });

  it('passes org to child components', () => {
    renderReportView();
    const cs = screen.getByTestId('customer-sales-report');
    expect(cs.textContent).toContain('org=huizhou');
  });
});
