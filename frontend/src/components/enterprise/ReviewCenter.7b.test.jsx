import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const queue = {
  counts: {
    org_units: 0, capabilities: 0, processes: 0, activities: 0, evidence: 0,
    links: 1, document_proposals: 1,
  },
  org_units: [], capabilities: [], processes: [], activities: [], evidence: [],
  links: [{
    kind: 'link', id: 'l1',
    label: 'Order —belongs_to→ Customer',
    confidence: 0.7, discovery_method: 'fk_constraint',
    evidence_ids: ['ev-1'], review_history: [],
  }],
  document_proposals: [{
    kind: 'document_proposal', id: 'dp1', label: 'Penalty Clause',
    proposal_kind: 'object', description: 'Late-payment penalty',
    quote: 'a 2% penalty applies', location_json: { page: 3 },
    source_ref: 'doc:abc12345:c1', discovery_method: 'llm_extraction',
    evidence_status: 'proposed', confidence: null, review_history: [],
  }],
  hidden_document_proposals: 2,
};

let mockFetchGlobal = vi.fn();
vi.mock('@/api/authFetch', () => ({ authFetch: (...args) => mockFetchGlobal(...args) }));
vi.mock('sonner', () => ({ toast: { error: vi.fn(), success: vi.fn() } }));

import ReviewCenter from './ReviewCenter';

describe('ReviewCenter (Phase 7B — links + document proposals)', () => {
  let posts;
  beforeEach(() => {
    posts = [];
    mockFetchGlobal = vi.fn(async (url, opts = {}) => {
      const u = String(url);
      if (u.endsWith('/enterprise/review-queue')) {
        return { ok: true, json: async () => queue };
      }
      if (opts.method === 'POST') {
        posts.push({ url: u, body: opts.body });
        return { ok: true, json: async () => ({}) };
      }
      return { ok: true, json: async () => ({}) };
    });
  });

  it('renders proposed relationships in their own section', async () => {
    render(<ReviewCenter />);
    await waitFor(() =>
      expect(screen.getByText('Order —belongs_to→ Customer')).toBeInTheDocument());
    expect(screen.getByText(/Proposed relationships/i)).toBeInTheDocument();
  });

  it('confirms a link via the generic enterprise endpoint', async () => {
    render(<ReviewCenter />);
    await waitFor(() => screen.getByText('Order —belongs_to→ Customer'));
    const section = screen.getByText(/Proposed relationships/i)
      .closest('section');
    fireEvent.click(withinSection(section).getByText('Confirm'));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0].url)
      .toBe('/api/ontology/enterprise/link/l1/confirm');
  });

  it('renders document-derived proposals with quote and location', async () => {
    render(<ReviewCenter />);
    await waitFor(() => screen.getByText('Penalty Clause'));
    expect(screen.getByText(/a 2% penalty applies/)).toBeInTheDocument();
    expect(screen.getByText(/Document-derived proposals/i)).toBeInTheDocument();
  });

  it('approves a document proposal via the document-evidence endpoint', async () => {
    render(<ReviewCenter />);
    await waitFor(() => screen.getByText('Penalty Clause'));
    const section = screen.getByText(/Document-derived proposals/i)
      .closest('section');
    fireEvent.click(withinSection(section).getByText('Confirm'));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0].url)
      .toBe('/api/ontology/document-evidence/dp1/approve');
  });

  it('rejects a document proposal with reason via document-evidence', async () => {
    render(<ReviewCenter />);
    await waitFor(() => screen.getByText('Penalty Clause'));
    const section = screen.getByText(/Document-derived proposals/i)
      .closest('section');
    fireEvent.click(withinSection(section).getByText('Reject'));
    fireEvent.change(screen.getByPlaceholderText(/Reason for rejection/i),
      { target: { value: 'duplicate of existing object' } });
    fireEvent.click(withinSection(section).getByText('Reject'));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0].url)
      .toBe('/api/ontology/document-evidence/dp1/reject');
    expect(JSON.parse(posts[0].body).reason)
      .toBe('duplicate of existing object');
  });

  it('honestly discloses D6-hidden document proposals', async () => {
    render(<ReviewCenter />);
    await waitFor(() => screen.getByText('Penalty Clause'));
    expect(screen.getByText(/2 document proposal/i)).toBeInTheDocument();
  });

  it('removes an item from its section after a successful decision', async () => {
    render(<ReviewCenter />);
    await waitFor(() => screen.getByText('Order —belongs_to→ Customer'));
    const section = screen.getByText(/Proposed relationships/i)
      .closest('section');
    fireEvent.click(withinSection(section).getByText('Confirm'));
    await waitFor(() =>
      expect(screen.queryByText('Order —belongs_to→ Customer'))
        .not.toBeInTheDocument());
  });
});

function withinSection(section) {
  return { getByText: (t) => getByTextIn(section, t) };
}

function getByTextIn(section, text) {
  const els = Array.from(section.querySelectorAll('button'));
  const hit = els.find((b) => b.textContent.trim() === text);
  if (!hit) throw new Error(`button "${text}" not found in section`);
  return hit;
}
