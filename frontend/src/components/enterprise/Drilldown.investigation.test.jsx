import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

class RO { observe() {} unobserve() {} disconnect() {} }
global.ResizeObserver = global.ResizeObserver || RO;

const objectDetail = {
  id: 'o1', name: 'Order', evidence_status: 'verified',
  domain: { id: 'd1', name: 'Sales' },
  links: [], processes: [],
  backing: { kind: 'table', ref: 'kb:kb-1:sales_order',
             resource: { id: 'kb-1', name: 'Sales DW', kind: 'knowledge_base' } },
  related_rules: [
    { id: 'br1', statement: 'PROHIBIT: selling below cost', status: 'enabled',
      scope: 'object', effect: 'block' },
  ],
};

const resourceDetail = {
  identity: { id: 'kb-1', name: 'Sales DW', kind: 'knowledge_base' },
  onboarding: { status: 'partial_failed', attempt: 2, error: 'boom',
                finished_at: '2026-10-30T10:00:00Z' },
  related_objects: [],
};

const history = {
  identity: { id: 'kb-1', name: 'Sales DW', kind: 'knowledge_base' },
  total: 2,
  runs: [
    { status: 'partial_failed', attempt: 2, error: 'boom',
      created_at: '2026-10-30T10:00:00Z',
      steps: [{ step: 'refresh_inventory', status: 'ok' },
              { step: 'extract_evidence', status: 'failed' }] },
    { status: 'completed', attempt: 1, error: null,
      created_at: '2026-10-29T10:00:00Z',
      steps: [{ step: 'refresh_inventory', status: 'ok' }] },
  ],
};

let mockFetchGlobal = vi.fn();
vi.mock('@/api/authFetch', () => ({ authFetch: (...args) => mockFetchGlobal(...args) }));

import { BusinessObjectPanel, ResourcePanel } from './Drilldown';

describe('Drilldown investigation links (Phase 6F)', () => {
  beforeEach(() => {
    mockFetchGlobal = vi.fn(async (url) => {
      const u = String(url);
      if (u.includes('/enterprise/business-objects/o1')) {
        return { ok: true, json: async () => objectDetail };
      }
      if (u.includes('/enterprise/resources/kb-1/onboarding-history')) {
        return { ok: true, json: async () => history };
      }
      if (u.includes('/enterprise/resources/kb-1')) {
        return { ok: true, json: async () => resourceDetail };
      }
      return { ok: true, json: async () => ({}) };
    });
  });

  it('business object shows its scoped business rules; click opens the rule', async () => {
    const onOpenRule = vi.fn();
    render(<BusinessObjectPanel objectId="o1" onClose={vi.fn()} onOpenRule={onOpenRule} />);
    await waitFor(() => expect(screen.getByText('Order')).toBeInTheDocument());
    fireEvent.click(screen.getByText(/selling below cost/));
    expect(onOpenRule).toHaveBeenCalledWith('br1');
    // current status shown as reference, not as an enforcement claim
    expect(screen.getByText(/enabled/)).toBeInTheDocument();
  });

  it('object without related rules shows an honest empty state', async () => {
    mockFetchGlobal = vi.fn(async () => ({
      ok: true, json: async () => ({ ...objectDetail, related_rules: [] }) }));
    render(<BusinessObjectPanel objectId="o1" onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByText('Order')).toBeInTheDocument());
    expect(screen.getByText(/No business rules reference this object/i)).toBeInTheDocument();
  });

  it('resource panel exposes bounded onboarding history on demand', async () => {
    render(<ResourcePanel refId="kb-1" onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByText('Sales DW')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /show history/i }));
    await waitFor(() => expect(screen.getByText('partial_failed')).toBeInTheDocument());
    expect(screen.getByText('completed')).toBeInTheDocument(); // both runs listed
    expect(screen.getByText(/extract_evidence/)).toBeInTheDocument();
  });
});
