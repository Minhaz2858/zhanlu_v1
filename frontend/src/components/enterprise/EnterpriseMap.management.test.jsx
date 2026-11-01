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
                 definitions: { success_rate: 'test definition' },
                 agent_runs: { total: 3, completed: 2, failed: 1,
                               success_rate: 0.67, latest_at: null } },
  departments: [
    { id: 'u1', name: 'Operations', unit_type: 'department', processes: 2,
      verified_processes: 1, proposed_processes: 1, observed_executions: 9,
      process_breakdown: [
        { id: 'p1', name: 'Order to Cash', evidence_status: 'verified',
          observed_executions: 9 },
        { id: 'p2', name: 'Procure to Pay', evidence_status: 'proposed',
          observed_executions: 0 },
      ],
      breakdown_truncated: false,
      definitions: {} },
  ],
  risks: { window_days: 30, enabled_prohibitive_rules: 2, blocked_actions: 3,
           failed_onboardings: 1, stale_reference_resources: 1,
           available: true, definitions: {} },
  changes: { window_days: 30, available: true,
             events: [{ event_type: 'resource_onboarded', count: 4,
                        latest_at: '2026-10-31T10:00:00Z' }],
             recent: [], definitions: {} },
};

const mapPayload = (overview) => ({
  org_units: [{ id: 'u1', name: 'Operations', unit_type: 'department' }],
  capabilities: [],
  processes: [{ id: 'p1', name: 'Order to Cash', process_family: 'Sales',
                discovery_status: 'documented', evidence_status: 'verified',
                activities_count: 2, responsible_units: ['Operations'] }],
  families: [], systems: [], process_links: [], overview,
});

const blockedPayload = {
  window_days: 30, total_blocked: 1, available: true, definitions: {},
  items: [{
    occurred_at: '2026-10-30T10:00:00Z', action_id: 'act-1',
    action_name: 'place_order', tool_name: 'execute_action',
    actor_type: 'agent', actor_id: 'proc-agent',
    violated_rules: [{ id: 'br1', statement: 'PROHIBIT: selling below cost',
                       status: 'enabled', unavailable: false }],
    applicable_rule_ids: ['br1'], approval_required: false,
    evaluation_status: 'deterministic',
  }],
};

let mockFetchGlobal = vi.fn();
vi.mock('@/api/authFetch', () => ({ authFetch: (...args) => mockFetchGlobal(...args) }));
vi.mock('sonner', () => ({ toast: { error: vi.fn() } }));

import EnterpriseMap from './EnterpriseMap';

describe('EnterpriseMap management drilldown (Phase 6E)', () => {
  beforeEach(() => {
    mockFetchGlobal = vi.fn(async (url) => {
      const u = String(url);
      if (u.includes('/enterprise/map')) {
        return { ok: true, json: async () => mapPayload(exec) };
      }
      if (u.includes('/enterprise/blocked-actions')) {
        return { ok: true, json: async () => blockedPayload };
      }
      if (u.includes('/enterprise/executive-snapshot')) {
        return { ok: true, blob: async () => new Blob(['<html/>']) };
      }
      return { ok: true, json: async () => ({}) };
    });
  });

  it('renders the reporting-window selector and refetches the map with it', async () => {
    render(<EnterpriseMap onOpenProcess={vi.fn()} onNavigate={vi.fn()} />);
    await waitFor(() => expect(screen.getByText('How we operate')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: '7d' }));
    await waitFor(() => {
      const mapCalls = mockFetchGlobal.mock.calls
        .filter((c) => String(c[0]).includes('/enterprise/map'));
      expect(mapCalls.length).toBeGreaterThanOrEqual(2);
      expect(String(mapCalls[mapCalls.length - 1][0])).toContain('days=7');
    });
  });

  it('shows agent terminal outcomes as a separate segment', async () => {
    render(<EnterpriseMap onOpenProcess={vi.fn()} onNavigate={vi.fn()} />);
    const label = await screen.findByText(/Agent runs \(terminal\):/);
    const strip = label.closest('div');
    expect(within(strip).getByText('3')).toBeInTheDocument(); // terminal total
    expect(label).toHaveTextContent(/rate 67%/);
  });

  it('blocked-action label follows the configured window', async () => {
    render(<EnterpriseMap onOpenProcess={vi.fn()} onNavigate={vi.fn()} />);
    const panel = await screen.findByText('Business risks');
    const risksBox = panel.closest('div.rounded-xl');
    expect(within(risksBox).getByText(/blocked by rules in 30 days/i)).toBeInTheDocument();
  });

  it('opens the blocked-action investigation with resolved rule names', async () => {
    render(<EnterpriseMap onOpenProcess={vi.fn()} onNavigate={vi.fn()} />);
    const panel = await screen.findByText('Business risks');
    fireEvent.click(within(panel.closest('div.rounded-xl')).getByText('Investigate'));
    await waitFor(() => expect(screen.getByText('place_order')).toBeInTheDocument());
    expect(screen.getByText(/PROHIBIT: selling below cost/)).toBeInTheDocument();
    expect(screen.getByText(/enabled/)).toBeInTheDocument(); // current rule status — not a re-enforcement claim
    const calls = mockFetchGlobal.mock.calls.filter((c) =>
      String(c[0]).includes('/enterprise/blocked-actions'));
    expect(String(calls[0][0])).toContain('days=30');
  });

  it('blocked-action panel stays honest when nothing was blocked', async () => {
    mockFetchGlobal = vi.fn(async (url) => {
      const u = String(url);
      if (u.includes('/enterprise/map')) {
        return { ok: true, json: async () => mapPayload(exec) };
      }
      if (u.includes('/enterprise/blocked-actions')) {
        return { ok: true, json: async () => ({
          window_days: 30, total_blocked: 0, items: [], available: true,
          definitions: {} }) };
      }
      return { ok: true, json: async () => ({}) };
    });
    render(<EnterpriseMap onOpenProcess={vi.fn()} onNavigate={vi.fn()} />);
    const panel = await screen.findByText('Business risks');
    fireEvent.click(within(panel.closest('div.rounded-xl')).getByText('Investigate'));
    await waitFor(() => expect(screen.getByText(
      /No actions were blocked by business rules in this window/i)).toBeInTheDocument());
  });

  it('exports the executive snapshot for the current window', async () => {
    const createSpy = vi.fn(() => ({ click: vi.fn(), remove: vi.fn() }));
    URL.createObjectURL = createSpy;
    render(<EnterpriseMap onOpenProcess={vi.fn()} onNavigate={vi.fn()} />);
    await waitFor(() => expect(screen.getByText('How we operate')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /export snapshot/i }));
    await waitFor(() => {
      const calls = mockFetchGlobal.mock.calls.filter((c) =>
        String(c[0]).includes('/enterprise/executive-snapshot'));
      expect(calls.length).toBe(1);
      expect(String(calls[0][0])).toContain('days=30');
      expect(createSpy).toHaveBeenCalled();
    });
  });

  it('department drilldown shows per-process evidence from the breakdown', async () => {
    render(<EnterpriseMap onOpenProcess={vi.fn()} onNavigate={vi.fn()} />);
    // click the map's unit NODE (canvas text), not the summary-table cell
    await waitFor(() => expect(screen.getAllByText('Operations').length).toBeGreaterThan(0));
    const node = screen.getAllByText('Operations')
      .map((el) => el.closest('.react-flow__node'))
      .find(Boolean);
    expect(node).toBeTruthy();
    fireEvent.click(node);
    await waitFor(() => expect(screen.getByText('Order to Cash')).toBeInTheDocument());
    // per-process observed volume + status appear in the drilldown
    expect(screen.getAllByText('9').length).toBeGreaterThan(0);
    expect(screen.getAllByText(/observed/).length).toBeGreaterThan(0);
  });
});
