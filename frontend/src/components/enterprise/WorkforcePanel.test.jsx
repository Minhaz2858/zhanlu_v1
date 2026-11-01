import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';

const bindings = [
  { id: 'b1', agent_name: 'general_assistant', domain_name: 'supply_chain',
    permission: 'read' },
  { id: 'b2', agent_name: 'procurement_agent', domain_name: 'procurement',
    permission: 'write' },
];

const context = {
  status: 'partial',
  warnings: ['section_unavailable:metrics'],
  evidence_coverage: 0.4,
  token_budget: { estimated_tokens: 900, budget_tokens: 2000,
                  included_items: 7, omitted_items: 3 },
  project: { authorized: true, name: 'Ops Pilot', resources:
             [{ kind: 'knowledge_base', id: 'kb-1', name: 'Sales DW' }] },
  department: { name: 'Operations', hierarchy: ['Operations'] },
  business_rules: [
    { statement: 'PROHIBIT: selling below cost', status: 'enabled', applies_to: 'action' },
    { statement: 'Review bulk discounts', status: 'verified', applies_to: 'action' },
    { statement: 'Flag churn risk', status: 'proposed', applies_to: 'action' },
  ],
  ontology: { objects: [{ name: 'Order', status: 'verified' }] },
};

let mockFetchGlobal = vi.fn();
vi.mock('@/api/authFetch', () => ({ authFetch: (...args) => mockFetchGlobal(...args) }));
vi.mock('sonner', () => ({ toast: { error: vi.fn() } }));

import WorkforcePanel from './WorkforcePanel';

describe('WorkforcePanel (Phase 6F — AI Workforce explainability)', () => {
  beforeEach(() => {
    mockFetchGlobal = vi.fn(async (url) => {
      const u = String(url);
      if (u.includes('/ontology/bindings')) {
        return { ok: true, json: async () => bindings };
      }
      if (u.includes('/enterprise/assistant-context')) {
        return { ok: true, json: async () => context };
      }
      return { ok: true, json: async () => ({}) };
    });
  });

  it('lists org agent-domain bindings with permission scope', async () => {
    render(<WorkforcePanel />);
    await waitFor(() => expect(screen.getByText('general_assistant')).toBeInTheDocument());
    expect(screen.getByText('procurement_agent')).toBeInTheDocument();
    const row = screen.getByText('general_assistant').closest('div');
    expect(within(row).getByText(/supply_chain/i)).toBeInTheDocument();
    expect(within(row).getByText(/read/i)).toBeInTheDocument();
  });

  it('explains assistant business context: rules by tier, coverage, budget', async () => {
    render(<WorkforcePanel />);
    await waitFor(() => expect(screen.getByText('general_assistant')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /explain context/i }));
    await waitFor(() => expect(screen.getByText(/PROHIBIT: selling below cost/)).toBeInTheDocument());
    // executable vs informational vs hypothesis tiers are separated
    expect(screen.getByText(/Review bulk discounts/)).toBeInTheDocument();
    expect(screen.getByText(/hypothes/i)).toBeInTheDocument();
    // context completeness + coverage are explicit
    expect(screen.getAllByText(/partial/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/40%/)).toBeInTheDocument();
    // warnings surfaced, never hidden
    expect(screen.getByText(/section_unavailable:metrics/)).toBeInTheDocument();
  });

  it('department association is labelled context-only, never an access grant', async () => {
    render(<WorkforcePanel />);
    fireEvent.click(await screen.findByRole('button', { name: /explain context/i }));
    await waitFor(() => expect(screen.getByText(/context only/i)).toBeInTheDocument());
  });

  it('unauthorized project shows the boundary honestly', async () => {
    mockFetchGlobal = vi.fn(async (url) => {
      if (String(url).includes('/ontology/bindings')) {
        return { ok: true, json: async () => [] };
      }
      if (String(url).includes('/enterprise/assistant-context')) {
        return { ok: true, json: async () => ({
          status: 'unavailable', warnings: [], evidence_coverage: 0,
          token_budget: { estimated_tokens: 0, budget_tokens: 2000, included_items: 0, omitted_items: 0 },
          project: { authorized: false, project_id: 'prj-f',
                     unavailable_reason: 'project not authorized (404)' },
          business_rules: [], ontology: { objects: [] } }) };
      }
      return { ok: true, json: async () => ({}) };
    });
    render(<WorkforcePanel />);
    fireEvent.click(await screen.findByRole('button', { name: /explain context/i }));
    await waitFor(() => expect(screen.getByText(/not authorized/i)).toBeInTheDocument());
  });

  it('empty org shows an honest no-assistants state', async () => {
    mockFetchGlobal = vi.fn(async () => ({ ok: true, json: async () => [] }));
    render(<WorkforcePanel />);
    await waitFor(() => expect(screen.getByText(
      /No agent-domain bindings recorded yet/i)).toBeInTheDocument());
  });
});
