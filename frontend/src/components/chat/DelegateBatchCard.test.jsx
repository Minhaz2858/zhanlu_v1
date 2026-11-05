/**
 * DelegateBatchCard tests — P1 Phase 4 evidence-inspection surface.
 *
 * The card renders the delegate_task batch contract payload persisted in
 * message.tool_calls: {status, summary, count, results:[...]}. It must:
 *   - Render nothing for null/empty payloads (callers don't gate).
 *   - Show the specialist count + batch summary chip.
 *   - Show per-item status badges (completed/partial/failed), falling
 *     back to success/truncated when a legacy result lacks `status`.
 *   - Surface the partial/failed warning banner ONLY when a child is
 *     not completed (roadmap: partial/failure must be explicit).
 *   - Expand a row on click to reveal the child's full response.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

const useLanguageMock = vi.hoisted(() =>
  vi.fn(() => ({ lang: 'en', setLang: () => {}, t: {} }))
);

vi.mock('@/lib/LanguageProvider', () => ({
  useLanguage: useLanguageMock,
}));

import DelegateBatchCard from './DelegateBatchCard';

const BATCH = {
  success: true,
  status: 'partial',
  summary: '1 completed, 1 partial, 1 failed',
  count: 3,
  results: [
    { status: 'completed', agent_name: 'Inventory Agent', task: 'count stock', response: '142 units' },
    { truncated: true, success: true, agent_name: 'Forecast Agent', task: 'forecast demand', response: 'partial trend...' },
    { success: false, status: 'failed', agent_name: 'QC Agent', task: 'audit defects', error: 'LLM quota exhausted' },
  ],
};

describe('DelegateBatchCard', () => {
  it('renders nothing for null or empty payloads', () => {
    const { container } = render(<DelegateBatchCard batch={null} />);
    expect(container).toBeEmptyDOMElement();
    const { container: c2 } = render(<DelegateBatchCard batch={{ results: [] }} />);
    expect(c2).toBeEmptyDOMElement();
  });

  it('shows specialist count and batch summary', () => {
    render(<DelegateBatchCard batch={BATCH} />);
    expect(screen.getByText('Delegated to 3 specialists')).toBeInTheDocument();
    expect(screen.getByText('1 completed, 1 partial, 1 failed')).toBeInTheDocument();
  });

  it('renders per-item status badges with legacy fallback', () => {
    render(<DelegateBatchCard batch={BATCH} />);
    // The partial item has no `status` key — normalized from truncated.
    expect(screen.getAllByText('completed').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('partial')).toBeInTheDocument();
    expect(screen.getByText('failed')).toBeInTheDocument();
  });

  it('shows the partial/failed warning banner when a child is not completed', () => {
    render(<DelegateBatchCard batch={BATCH} />);
    expect(screen.getByText(/may be incomplete/)).toBeInTheDocument();
  });

  it('hides the warning banner when every child completed', () => {
    const allOk = {
      status: 'completed', summary: '2 completed, 0 partial, 0 failed', count: 2,
      results: [
        { status: 'completed', agent_name: 'A', task: 't1', response: 'ok' },
        { status: 'completed', agent_name: 'B', task: 't2', response: 'ok' },
      ],
    };
    render(<DelegateBatchCard batch={allOk} />);
    expect(screen.queryByText(/may be incomplete/)).not.toBeInTheDocument();
  });

  it('expands a row to reveal the child response on click', () => {
    render(<DelegateBatchCard batch={BATCH} />);
    expect(screen.queryByText('142 units')).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('Inventory Agent'));
    expect(screen.getByText('142 units')).toBeInTheDocument();
    // Collapses again on second click.
    fireEvent.click(screen.getByText('Inventory Agent'));
    expect(screen.queryByText('142 units')).not.toBeInTheDocument();
  });
});
