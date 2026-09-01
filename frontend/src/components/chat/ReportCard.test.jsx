/**
 * ReportCard component tests.
 *
 * Verifies the cleaned-up chart card (Export PDF / More / View buttons
 * removed per the Claude-style redesign):
 *   - The chart body (ReportCardBody) is rendered.
 *   - The header "View" button is NOT rendered.
 *   - The ExportMenu ("Export PDF" / "More") is NOT rendered.
 *   - A Download link appears only for export signals (export/download/save).
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import React from 'react';
import ReportCard from '@/components/chat/ReportCard';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

vi.mock('lucide-react', () => ({
  Sparkles: (props) => React.createElement('span', { 'data-icon': 'sparkles', ...props }),
  Database: (props) => React.createElement('span', { 'data-icon': 'database', ...props }),
  Clock:    (props) => React.createElement('span', { 'data-icon': 'clock', ...props }),
  Download: (props) => React.createElement('span', { 'data-icon': 'download', ...props }),
  Loader2:  (props) => React.createElement('span', { 'data-icon': 'loader2', ...props }),
}));

vi.mock('framer-motion', () => ({
  motion: {
    div: React.forwardRef(({ children, ...props }, ref) =>
      React.createElement('div', { ref, 'data-motion': 'div', ...props }, children)
    ),
  },
}));

vi.mock('@/components/chat/ReportCardBody', () => ({
  default: () => React.createElement('div', { 'data-testid': 'report-card-body' }),
}));

vi.mock('@/lib/utils', () => ({
  cn: (...args) => args.filter(Boolean).join(' '),
}));

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function buildPayload(overrides = {}) {
  return {
    title: 'Test Report',
    source: 'PostgreSQL (analytics)',
    generated_at: '2026-07-15T12:00:00Z',
    kpis: [{ label: 'Count', value: 42 }],
    chart: { type: 'bar', data: [{ name: 'A', value: 10 }] },
    insights: [],
    next_step: null,
    warnings: [],
    actions: [],
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('ReportCard', () => {
  it('renders the chart body', () => {
    render(
      React.createElement(ReportCard, {
        payload: buildPayload(),
        artifactId: null,
        userSignal: 'default',
        isPendingExport: false,
      })
    );

    expect(screen.getByTestId('report-card-body')).toBeInTheDocument();
  });

  it('does NOT render the header "View" button', () => {
    render(
      React.createElement(ReportCard, {
        payload: buildPayload(),
        artifactId: 'art-1',
        userSignal: 'default',
        fileExports: { docx: { artifact_id: 'art-1', preview_url: '/p', download_url: '/d' } },
        isPendingExport: false,
      })
    );

    expect(screen.queryByText('View')).not.toBeInTheDocument();
  });

  it('does NOT render the ExportMenu ("Export PDF" / "More")', () => {
    render(
      React.createElement(ReportCard, {
        payload: buildPayload(),
        artifactId: 'art-1',
        userSignal: 'default',
        isPendingExport: false,
      })
    );

    expect(screen.queryByText('Export PDF')).not.toBeInTheDocument();
    expect(screen.queryByText('More')).not.toBeInTheDocument();
    expect(screen.queryByTestId('export-menu')).not.toBeInTheDocument();
  });

  it('shows a Download link only for export signals', () => {
    const { rerender } = render(
      React.createElement(ReportCard, {
        payload: buildPayload(),
        artifactId: 'art-1',
        userSignal: 'default',
        isPendingExport: false,
      })
    );
    // No export signal → no download link.
    expect(screen.queryByText('Download')).not.toBeInTheDocument();

    rerender(
      React.createElement(ReportCard, {
        payload: buildPayload(),
        artifactId: 'art-1',
        userSignal: 'export',
        isPendingExport: false,
      })
    );
    // Export signal → download link present.
    expect(screen.getByText('Download')).toBeInTheDocument();
    expect(screen.getByText('Download').closest('a')).toHaveAttribute(
      'href',
      '/api/artifacts/art-1/download'
    );
  });
});
