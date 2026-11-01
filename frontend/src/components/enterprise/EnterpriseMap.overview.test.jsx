import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

// ResizeObserver is required by @xyflow/react in happy-dom
class RO {
  observe() {}
  unobserve() {}
  disconnect() {}
}
global.ResizeObserver = global.ResizeObserver || RO;

const overviewFull = {
  processes: { total: 4, verified: 2, proposed: 1, rejected: 0, awaiting_review: 1 },
  observed: {
    total_executions: 12,
    processes_with_observations: 2,
    confidence_distribution: { high: 7, medium: 3, low: 2 },
    latest_observed_at: '2026-11-01T09:00:00Z',
    available: true,
  },
  knowledge: {
    available: true,
    objects: { total: 9, verified: 5, proposed: 2 },
    links: 6,
    terminology: 14,
    metrics: 3,
    rules: { total: 5, enabled: 2, verified: 1, proposed: 2 },
    awaiting_review: 4,
  },
  resources_onboarded: 7,
  attention: {
    available: true,
    processes_awaiting_review: 1,
    knowledge_awaiting_review: 4,
    partial_onboardings: 1,
  },
};

const baseMap = (overview) => ({
  org_units: [{ id: 'u1', name: 'Operations', unit_type: 'department', evidence_status: 'verified' }],
  capabilities: [],
  processes: [
    { id: 'p1', name: 'Order to Cash', process_family: 'Sales', discovery_status: 'documented', evidence_status: 'verified', activities_count: 3, responsible_units: ['Operations'] },
    { id: 'p2', name: 'Procure to Pay', process_family: 'Procurement', discovery_status: 'documented', evidence_status: 'proposed', activities_count: 2, responsible_units: [] },
  ],
  families: [{ family: 'Sales', count: 1 }, { family: 'Procurement', count: 1 }],
  systems: [
    { id: 'kb-1', name: 'Sales DW', resource_kind: 'datasource', ref_id: 'kb-1', evidence_status: 'verified' },
    { id: 'kb-2', name: 'HR Docs', resource_kind: 'document', ref_id: 'kb-2', evidence_status: 'proposed' },
  ],
  process_links: [],
  overview,
});

const mapFetch = (map) => vi.fn(async (url) => {
  if (String(url).includes('/enterprise/map')) {
    return { ok: true, json: async () => map };
  }
  return { ok: true, json: async () => ({}) };
});

vi.mock('@/api/authFetch', () => ({ authFetch: (...args) => mockFetchGlobal(...args) }));
vi.mock('sonner', () => ({ toast: { error: vi.fn() } }));

let mockFetchGlobal = vi.fn();
import EnterpriseMap from './EnterpriseMap';

describe('EnterpriseMap executive overview (Phase 6B)', () => {
  beforeEach(() => {
    mockFetchGlobal = mapFetch(baseMap(overviewFull));
  });

  it('renders the KPI strip from real map data', async () => {
    render(<EnterpriseMap onOpenProcess={vi.fn()} onNavigate={vi.fn()} canReview />);
    await waitFor(() => expect(screen.getByText('Enterprise Map')).toBeInTheDocument());
    expect(screen.getByText('Business processes')).toBeInTheDocument();
    expect(screen.getByText('Verified')).toBeInTheDocument();
    expect(screen.getByText('Awaiting review')).toBeInTheDocument();
    expect(screen.getByText('With execution evidence')).toBeInTheDocument();
    expect(screen.getByText('Systems & resources')).toBeInTheDocument();
  });

  it('shows observed execution overview with confidence bands and latest time', async () => {
    render(<EnterpriseMap onOpenProcess={vi.fn()} onNavigate={vi.fn()} canReview />);
    await waitFor(() => expect(screen.getByText('12')).toBeInTheDocument()); // total observed
    expect(screen.getByText('7')).toBeInTheDocument(); // high band count
    expect(screen.getByText('2 processes without execution evidence — documented only, not (yet) observed.')).toBeInTheDocument();
  });

  it('shows knowledge coverage in business language, no ontology jargon', async () => {
    render(<EnterpriseMap onOpenProcess={vi.fn()} onNavigate={vi.fn()} canReview />);
    await waitFor(() => expect(screen.getByText(/Business objects:/)).toBeInTheDocument());
    expect(screen.getByText(/Connections:/)).toBeInTheDocument();
    expect(screen.getByText(/Business terms:/)).toBeInTheDocument();
    expect(screen.getByText(/Key metrics:/)).toBeInTheDocument();
    expect(screen.getByText(/Business rules:/)).toBeInTheDocument();
    // no technical jargon
    expect(screen.queryByText(/ontology/i)).not.toBeInTheDocument();
    // enabled/verified/proposed distinction present
    expect(screen.getAllByText(/awaiting review/i).length).toBeGreaterThan(0);
  });

  it('surfaces attention signals and navigates to existing views', async () => {
    const onNavigate = vi.fn();
    render(<EnterpriseMap onOpenProcess={vi.fn()} onNavigate={onNavigate} canReview />);
    await waitFor(() => expect(screen.getByText(/Needs attention/i)).toBeInTheDocument());
    const reviewChip = screen.getByText('Awaiting review').closest('button');
    fireEvent.click(reviewChip);
    expect(onNavigate).toHaveBeenCalledWith('R'); // Review Center (admin-gated)
  });

  it('hides review navigation for non-admin users but keeps honest counts', async () => {
    render(<EnterpriseMap onOpenProcess={vi.fn()} onNavigate={vi.fn()} canReview={false} />);
    const label = await screen.findByText('Awaiting review');
    expect(label.closest('button')).toBeNull(); // not clickable for non-admins
  });

  it('unavailable sections display as unavailable, never zero', async () => {
    const unavailable = JSON.parse(JSON.stringify(overviewFull));
    unavailable.knowledge.available = false;
    delete unavailable.knowledge.objects;
    mockFetchGlobal = mapFetch(baseMap(unavailable));
    render(<EnterpriseMap onOpenProcess={vi.fn()} onNavigate={vi.fn()} />);
    await waitFor(() => expect(screen.getByText('Knowledge data unavailable.')).toBeInTheDocument());
    expect(screen.getByText('Execution evidence')).toBeInTheDocument(); // other sections unaffected
  });

  it('empty organization stays honest', async () => {
    mockFetchGlobal = mapFetch({
      org_units: [], capabilities: [], processes: [], families: [],
      systems: [], process_links: [],
      overview: {
        processes: { total: 0, verified: 0, proposed: 0, rejected: 0, awaiting_review: 0 },
        observed: { total_executions: 0, processes_with_observations: 0, confidence_distribution: { high: 0, medium: 0, low: 0 }, latest_observed_at: null, available: true },
        knowledge: { available: true, objects: { total: 0, verified: 0, proposed: 0 }, links: 0, terminology: 0, metrics: 0, rules: { total: 0, enabled: 0, verified: 0, proposed: 0 }, awaiting_review: 0 },
        resources_onboarded: 0,
        attention: { available: true, processes_awaiting_review: 0, knowledge_awaiting_review: 0, partial_onboardings: 0 },
      },
    });
    render(<EnterpriseMap onOpenProcess={vi.fn()} onNavigate={vi.fn()} />);
    await waitFor(() => expect(screen.getByText('Business processes')).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText('Nothing discovered yet.')).toBeInTheDocument());
  });

  it('API failure keeps the map usable', async () => {
    mockFetchGlobal = vi.fn(async () => { throw new Error('network down'); });
    render(<EnterpriseMap onOpenProcess={vi.fn()} onNavigate={vi.fn()} />);
    await waitFor(() => expect(screen.getByText('Nothing discovered yet.')).toBeInTheDocument());
  });

  it('process nodes remain clickable (existing behavior)', async () => {
    const onOpenProcess = vi.fn();
    render(<EnterpriseMap onOpenProcess={onOpenProcess} onNavigate={vi.fn()} />);
    const node = await screen.findByText('Order to Cash');
    fireEvent.click(node);
    expect(onOpenProcess).toHaveBeenCalledWith('p1', true);
  });
});
