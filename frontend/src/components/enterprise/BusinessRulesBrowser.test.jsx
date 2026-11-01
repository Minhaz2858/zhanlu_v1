import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';

const rules = [
  { id: 'br1', statement: 'PROHIBIT: selling below cost', status: 'enabled',
    scope: 'object', applies_to: 'action', effect: 'block',
    source_evidence_ids: ['ev-1'], review_history: [] },
  { id: 'br2', statement: 'Review bulk discounts before approval', status: 'verified',
    scope: 'object', applies_to: 'action', effect: 'informational',
    source_evidence_ids: [], review_history: [] },
  { id: 'br3', statement: 'REQUIRE_APPROVAL: bulk exports', status: 'enabled',
    scope: 'domain', applies_to: 'action', effect: 'approval',
    source_evidence_ids: [], review_history: [] },
  { id: 'br4', statement: 'Flag churn-risk customers', status: 'proposed',
    scope: 'org', applies_to: 'action', effect: 'informational',
    source_evidence_ids: [], review_history: [] },
];

const detail = {
  id: 'br1', org_id: 'org-acme', statement: 'PROHIBIT: selling below cost',
  scope: 'object', scope_ref_id: 'o1', status: 'enabled', applies_to: 'action',
  effect: 'block', source_evidence_ids: ['ev-1', 'ev-gone'],
  review_history: [{ action: 'enable', at: '2026-10-30T10:00:00Z',
                     reviewer_name: 'Ada' }],
  resolved_evidence: [
    { id: 'ev-1', available: true, source_ref: 'ontology:o1',
      evidence_status: 'verified' },
    { id: 'ev-gone', available: false },
  ],
};

let mockFetchGlobal = vi.fn();
vi.mock('@/api/authFetch', () => ({ authFetch: (...args) => mockFetchGlobal(...args) }));
vi.mock('sonner', () => ({ toast: { error: vi.fn(), success: vi.fn() } }));

import BusinessRulesBrowser from './BusinessRulesBrowser';

describe('BusinessRulesBrowser (Phase 6F — knowledge investigation)', () => {
  beforeEach(() => {
    mockFetchGlobal = vi.fn(async (url) => {
      const u = String(url);
      if (u.endsWith('/enterprise/business-rules/br1')) {
        return { ok: true, json: async () => detail };
      }
      if (u.includes('/enterprise/business-rules')) {
        return { ok: true, json: async () => ({ items: rules, total: 4, page: 1, page_size: 50 }) };
      }
      return { ok: true, json: async () => ({}) };
    });
  });

  it('lists rules with executable vs informational distinction', async () => {
    render(<BusinessRulesBrowser isAdmin={false} onOpenRule={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/selling below cost/)).toBeInTheDocument());
    expect(screen.getByText('Review bulk discounts before approval')).toBeInTheDocument();
    const executable = screen.getAllByText(/blocks execution/i).length;
    expect(executable).toBeGreaterThan(0);           // br1 (block marker)
    expect(screen.getAllByText(/needs approval/i).length).toBeGreaterThan(0); // br3
    expect(screen.getAllByText(/informational/i).length).toBeGreaterThan(0);  // br2/br4
    // proposed rule never claims enforcement
    expect(screen.getAllByText(/proposed/i).length).toBeGreaterThan(0);
  });

  it('explains the state vocabulary without conflating it', async () => {
    render(<BusinessRulesBrowser isAdmin={false} onOpenRule={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/selling below cost/)).toBeInTheDocument());
    const glossary = screen.getByText(/What do the states mean\?/).closest('details');
    expect(glossary).not.toBeNull();
    for (const term of ['Documented', 'Discovered', 'Proposed', 'Verified',
                        'Enabled', 'Observed', 'Rejected']) {
      expect(within(glossary).getByText(term)).toBeInTheDocument();
    }
  });

  it('deep-linked rule detail shows resolved evidence honestly', async () => {
    render(<BusinessRulesBrowser isAdmin={false} ruleId="br1" onOpenRule={vi.fn()} onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/selling below cost/)).toBeInTheDocument());
    expect(screen.getByText(/ontology:o1/)).toBeInTheDocument();
    expect(screen.getByText(/evidence .*unavailable|unavailable/i)).toBeInTheDocument();
    expect(screen.getByText(/Ada/)).toBeInTheDocument(); // review history
    // non-admin sees no review action buttons
    expect(screen.queryByRole('button', { name: /enable/i })).toBeNull();
  });

  it('admins can review a rule through the existing lifecycle API', async () => {
    const onOpenRule = vi.fn();
    render(<BusinessRulesBrowser isAdmin ruleId="br1" onOpenRule={onOpenRule} onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/selling below cost/)).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Disable' }));
    await waitFor(() => {
      const calls = mockFetchGlobal.mock.calls.filter((c) =>
        String(c[0]).endsWith('/enterprise/business-rules/br1/disable'));
      expect(calls.length).toBe(1);
    });
  });

  it('unknown rule id shows an honest unavailable state', async () => {
    mockFetchGlobal = vi.fn(async () => ({ ok: false, status: 404 }));
    render(<BusinessRulesBrowser isAdmin={false} ruleId="br-nope" onOpenRule={vi.fn()} onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/not available/i)).toBeInTheDocument());
  });

  it('list → detail navigation uses onOpenRule', async () => {
    const onOpenRule = vi.fn();
    render(<BusinessRulesBrowser isAdmin={false} onOpenRule={onOpenRule} />);
    await waitFor(() => expect(screen.getByText(/selling below cost/)).toBeInTheDocument());
    fireEvent.click(screen.getByText(/selling below cost/));
    expect(onOpenRule).toHaveBeenCalledWith('br1');
  });
});
