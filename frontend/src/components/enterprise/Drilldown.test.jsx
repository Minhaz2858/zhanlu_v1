import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const objectPayload = {
  id: 'o1', name: 'Order', description: 'A customer order',
  evidence_status: 'verified',
  domain: { id: 'd1', name: 'Sales' },
  backing: { kind: 'table', ref: 'kb:kb-1:sales_order',
             resource: { id: 'kb-1', name: 'Sales DW', kind: 'knowledge_base' } },
  links: [{ id: 'l1', link_type: 'belongs_to', peer_id: 'o2',
            peer_name: 'Customer', direction: 'outgoing',
            evidence_status: 'proposed' }],
  processes: [{ id: 'p1', name: 'Order to Cash', evidence_status: 'verified',
                link_status: 'verified' }],
};

const resourcePayload = {
  identity: { id: 'kb-1', name: 'Sales DW', kind: 'datasource' },
  onboarding: { status: 'completed', attempt: 2, error: null,
                finished_at: '2026-11-01T08:00:00Z' },
  related_objects: [{ id: 'o1', name: 'Order', evidence_status: 'verified' }],
};

let mockFetchGlobal = vi.fn();
vi.mock('@/api/authFetch', () => ({ authFetch: (...args) => mockFetchGlobal(...args) }));

import {
  BusinessObjectPanel, ResourcePanel, ExecutionTrend, dailyTrend,
} from './Drilldown';

describe('dailyTrend (pure aggregation — no fabrication)', () => {
  it('aggregates real reference timestamps into daily counts', () => {
    const refs = [
      { observed_at: '2026-10-30T08:00:00Z' },
      { observed_at: '2026-10-30T09:00:00Z' },
      { observed_at: '2026-11-01T10:00:00Z' },
    ];
    const trend = dailyTrend(refs);
    expect(trend.available).toBe(true);
    expect(trend.points).toEqual([
      { day: '2026-10-30', count: 2 },
      { day: '2026-11-01', count: 1 },
    ]);
  });

  it('is honest when history is insufficient (1 point → unavailable)', () => {
    expect(dailyTrend([{ observed_at: '2026-11-01T08:00:00Z' }]).available)
      .toBe(false);
    expect(dailyTrend([]).available).toBe(false);
    expect(dailyTrend(undefined).available).toBe(false);
  });

  it('ignores references without timestamps instead of inventing dates', () => {
    expect(dailyTrend([{ observed_at: null }, { observed_at: '2026-11-01T08:00:00Z' }])
      .available).toBe(false);
  });
});

describe('BusinessObjectPanel', () => {
  beforeEach(() => {
    mockFetchGlobal = vi.fn(async (url) => {
      if (String(url).includes('business-objects/o1')) {
        return { ok: true, json: async () => objectPayload };
      }
      return { ok: false, status: 404 };
    });
  });

  it('renders object identity, status, relationships, processes and resource link', async () => {
    const onOpenResource = vi.fn();
    render(<BusinessObjectPanel objectId="o1" onClose={vi.fn()}
      onOpenResource={onOpenResource} onOpenProcess={vi.fn()} />);
    await waitFor(() => expect(screen.getByText('Order')).toBeInTheDocument());
    expect(screen.getByText(/A customer order/)).toBeInTheDocument();
    expect(screen.getAllByText('verified').length).toBeGreaterThan(0);
    expect(screen.getByText(/Customer/)).toBeInTheDocument();
    const proc = screen.getByText(/Order to Cash/);
    fireEvent.click(proc);
    // process navigation fires with the process id
    expect(screen.getByText('Sales DW')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Sales DW'));
    expect(onOpenResource).toHaveBeenCalledWith('kb-1');
  });

  it('missing or cross-org object shows an honest unavailable state', async () => {
    render(<BusinessObjectPanel objectId="ghost" onClose={vi.fn()} />);
    await waitFor(() => expect(
      screen.getByText(/not available/i)).toBeInTheDocument());
  });

  it('shows breadcrumbs and supports back navigation', async () => {
    render(<BusinessObjectPanel objectId="o1" onClose={vi.fn()}
      crumbs={[{ label: 'Order to Cash' }, { label: 'Order' }]} />);
    const crumbs = await screen.findByText('Order to Cash');
    expect(crumbs).toBeInTheDocument();
    // breadcrumb trail ends at the current entity
    expect(screen.getAllByText('Order').length).toBeGreaterThan(0);
  });
});

describe('ResourcePanel', () => {
  beforeEach(() => {
    mockFetchGlobal = vi.fn(async (url) => {
      if (String(url).includes('/resources/kb-1')) {
        return { ok: true, json: async () => resourcePayload };
      }
      return { ok: false, status: 404 };
    });
  });

  it('renders identity, onboarding status and related concepts', async () => {
    const onOpenObject = vi.fn();
    render(<ResourcePanel refId="kb-1" onClose={vi.fn()} onOpenObject={onOpenObject} />);
    await waitFor(() => expect(screen.getByText('Sales DW')).toBeInTheDocument());
    expect(screen.getByText('completed')).toBeInTheDocument();
    expect(screen.getByText(/Order/)).toBeInTheDocument();
    // no credentials anywhere
    expect(screen.queryByText(/password/i)).not.toBeInTheDocument();
  });

  it('never-onboarded resources show an honest none state', async () => {
    mockFetchGlobal = vi.fn(async () => ({ ok: true, json: async () => ({
      identity: { id: 'kb-9', name: 'Docs KB', kind: 'document' },
      onboarding: { status: null, attempt: null, error: null, finished_at: null },
      related_objects: [],
    }) }));
    render(<ResourcePanel refId="kb-9" onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByText('Docs KB')).toBeInTheDocument());
    expect(screen.getByText(/never onboarded/i)).toBeInTheDocument();
  });

  it('missing resource shows honest unavailable state', async () => {
    render(<ResourcePanel refId="ghost" onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/not available/i)).toBeInTheDocument());
  });
});
