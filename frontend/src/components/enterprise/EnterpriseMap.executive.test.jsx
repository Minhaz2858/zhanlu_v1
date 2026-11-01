import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';

class RO { observe() {} unobserve() {} disconnect() {} }
global.ResizeObserver = global.ResizeObserver || RO;

const exec = {
  processes: { total: 4, verified: 2, proposed: 1, rejected: 0, awaiting_review: 1 },
  observed: { total_executions: 12, processes_with_observations: 2,
              confidence_distribution: { high: 7, medium: 3, low: 2 },
              latest_observed_at: '2026-11-01T09:00:00Z', available: true },
  knowledge: { available: true, objects: { total: 9, verified: 5, proposed: 2 },
               links: 6, terminology: 14, metrics: 3,
               rules: { total: 5, enabled: 2, verified: 1, proposed: 2 },
               awaiting_review: 4 },
  resources_onboarded: 7,
  attention: { available: true, processes_awaiting_review: 1,
               knowledge_awaiting_review: 4, partial_onboardings: 1 },
  performance: { total_executions: 12, successful: 9, failed: 3,
                 success_rate: 0.75, last_7d_executions: 5,
                 latest_execution_at: '2026-11-01T09:00:00Z', available: true,
                 definitions: { success_rate: 'test definition' } },
  departments: [
    { id: 'u1', name: 'Operations', unit_type: 'department', processes: 2,
      verified_processes: 1, proposed_processes: 1, observed_executions: 9,
      definitions: {} },
    { id: 'u2', name: 'Finance', unit_type: 'department', processes: 1,
      verified_processes: 0, proposed_processes: 1, observed_executions: 0,
      definitions: {} },
  ],
  risks: { window_days: 30, enabled_prohibitive_rules: 2, blocked_actions: 3,
           failed_onboardings: 1, stale_reference_resources: 1,
           available: true, definitions: {} },
  changes: { window_days: 30, available: true,
             events: [{ event_type: 'resource_onboarded', count: 4,
                        latest_at: '2026-10-31T10:00:00Z' },
                      { event_type: 'process_confirmed', count: 1,
                        latest_at: '2026-10-30T10:00:00Z' }],
             recent: [
               { event_type: 'process_confirmed',
                 occurred_at: '2026-10-30T10:00:00Z',
                 resource_type: 'process', resource_id: 'p1' },
               { event_type: 'resource_onboarded',
                 occurred_at: '2026-10-31T10:00:00Z',
                 resource_type: 'datasource', resource_id: 'kb-1' },
             ],
             definitions: {} },
};

const mapPayload = (overview) => ({
  org_units: [], capabilities: [],
  processes: [{ id: 'p1', name: 'Order to Cash', process_family: 'Sales',
                discovery_status: 'documented', evidence_status: 'verified',
                activities_count: 2, responsible_units: ['Operations'] }],
  families: [], systems: [], process_links: [], overview,
});

let mockFetchGlobal = vi.fn();
vi.mock('@/api/authFetch', () => ({ authFetch: (...args) => mockFetchGlobal(...args) }));
vi.mock('sonner', () => ({ toast: { error: vi.fn() } }));

import EnterpriseMap from './EnterpriseMap';

describe('EnterpriseMap executive intelligence (Phase 6D)', () => {
  beforeEach(() => {
    mockFetchGlobal = vi.fn(async (url) => {
      if (String(url).includes('/enterprise/map')) {
        return { ok: true, json: async () => mapPayload(exec) };
      }
      return { ok: true, json: async () => ({}) };
    });
  });

  it('shows operational performance with traceable definition', async () => {
    render(<EnterpriseMap onOpenProcess={vi.fn()} onNavigate={vi.fn()} />);
    await waitFor(() => expect(screen.getByText('How we operate')).toBeInTheDocument());
    expect(screen.getByText('75%')).toBeInTheDocument();      // success rate
    expect(screen.getByTitle('test definition')).toBeInTheDocument();
    // volume appears in the perf strip (getAllByText — other sections may repeat it)
    expect(screen.getAllByText('12').length).toBeGreaterThan(0);
  });

  it('shows department distribution without scores or ranking', async () => {
    render(<EnterpriseMap onOpenProcess={vi.fn()} onNavigate={vi.fn()} />);
    await waitFor(() => expect(screen.getByText('Operations')).toBeInTheDocument());
    expect(screen.getByText('Finance')).toBeInTheDocument();
    // explicit honesty: execution volume is not performance
    expect(screen.getByText(/volume evidence, not a performance score/i)).toBeInTheDocument();
  });

  it('surfaces risk signals from real records', async () => {
    render(<EnterpriseMap onOpenProcess={vi.fn()} onNavigate={vi.fn()} canReview />);
    const panel = await screen.findByText('Business risks');
    const risksBox = panel.closest('div.rounded-xl');
    expect(within(risksBox).getByText('2')).toBeInTheDocument(); // prohibitive rules
    expect(within(risksBox).getByText('3')).toBeInTheDocument(); // blocked actions
    expect(screen.getByText(/1 system\(s\) with failed or partial onboarding/i)).toBeInTheDocument();
  });

  it('shows recent operational changes with dates', async () => {
    render(<EnterpriseMap onOpenProcess={vi.fn()} onNavigate={vi.fn()} />);
    await waitFor(() => expect(screen.getByText('Recent operational changes')).toBeInTheDocument());
    expect(screen.getAllByText(/process confirmed/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/resource onboarded/).length).toBeGreaterThan(0);
  });

  it('unavailable sections stay honest', async () => {
    const broken = JSON.parse(JSON.stringify(exec));
    broken.performance.available = false;
    broken.changes.available = false;
    mockFetchGlobal = vi.fn(async (url) => {
      if (String(url).includes('/enterprise/map')) {
        return { ok: true, json: async () => mapPayload(broken) };
      }
      return { ok: true, json: async () => ({}) };
    });
    render(<EnterpriseMap onOpenProcess={vi.fn()} onNavigate={vi.fn()} />);
    await waitFor(() => expect(
      screen.getByText('Performance data unavailable.')).toBeInTheDocument());
    expect(screen.getByText('Change data unavailable.')).toBeInTheDocument();
  });

  it('empty enterprise shows honest zero sections without fake trends', async () => {
    const empty = {
      processes: { total: 0, verified: 0, proposed: 0, rejected: 0, awaiting_review: 0 },
      observed: { total_executions: 0, processes_with_observations: 0,
                  confidence_distribution: { high: 0, medium: 0, low: 0 },
                  latest_observed_at: null, available: true },
      knowledge: { available: true, objects: { total: 0, verified: 0, proposed: 0 },
                   links: 0, terminology: 0, metrics: 0,
                   rules: { total: 0, enabled: 0, verified: 0, proposed: 0 },
                   awaiting_review: 0 },
      resources_onboarded: 0,
      attention: { available: true, processes_awaiting_review: 0,
                   knowledge_awaiting_review: 0, partial_onboardings: 0 },
      performance: { total_executions: 0, successful: 0, failed: 0,
                     success_rate: null, last_7d_executions: 0,
                     latest_execution_at: null, available: true,
                     definitions: {} },
      departments: [],
      risks: { window_days: 30, enabled_prohibitive_rules: 0, blocked_actions: 0,
               failed_onboardings: 0, stale_reference_resources: 0,
               available: true, definitions: {} },
      changes: { window_days: 30, events: [], recent: [], available: true,
                 definitions: {} },
    };
    mockFetchGlobal = vi.fn(async () => ({
      ok: true, json: async () => ({
        org_units: [], capabilities: [], processes: [], families: [],
        systems: [], process_links: [], overview: empty }) }));
    render(<EnterpriseMap onOpenProcess={vi.fn()} onNavigate={vi.fn()} />);
    await waitFor(() => expect(screen.getByText('Nothing discovered yet.')).toBeInTheDocument());
    // undefined success rate renders as '—', never a fabricated 0% or 100%
    expect(screen.getByText('—')).toBeInTheDocument();
  });
});
