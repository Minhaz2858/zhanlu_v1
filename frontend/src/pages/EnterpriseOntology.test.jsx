import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom';

class RO { observe() {} unobserve() {} disconnect() {} }
global.ResizeObserver = global.ResizeObserver || RO;

let mockFetchGlobal = vi.fn();
vi.mock('@/api/authFetch', () => ({ authFetch: (...args) => mockFetchGlobal(...args) }));
vi.mock('sonner', () => ({ toast: { error: vi.fn(), success: vi.fn() } }));
vi.mock('reactflow', () => ({
  // not used by this page — guard against accidental ReactFlow imports
}));

import EnterpriseOntology from './EnterpriseOntology';

function LocationProbe() {
  const loc = useLocation();
  return <div data-testid="loc">{loc.search}</div>;
}

function mount(url) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <LocationProbe />
      <Routes>
        <Route path="*" element={<EnterpriseOntology />} />
      </Routes>
    </MemoryRouter>,
  );
}

const overview = {
  org_id: 'org-acme',
  domains: [{ id: 'd1', name: 'Sales', code: 'sales', objects: 3,
              coverage: { objects_live: 2 } },
            { id: 'd2', name: 'Procurement', code: 'procurement', objects: 2,
              coverage: { objects_live: 0 } }],
  totals: { domains: 2, objects: 5, objects_live: 2, actions: 4,
            actions_executable: 2, links: 4 },
  bindings: [
    { id: 'b1', agent_name: 'general_assistant', domain_name: 'Sales',
      permission: 'read' },
  ],
};

const objects = [
  { id: 'o1', name: 'Sales Order', domain_id: 'd1',
    description: 'A confirmed customer order',
    evidence_status: 'verified', backing_kind: 'table',
    backing_ref: 'kb:kb-1:sales_order' },
  { id: 'o2', name: 'Customer', domain_id: 'd1',
    evidence_status: 'proposed', backing_kind: null, backing_ref: null },
  { id: 'o3', name: 'Purchase Order', domain_id: 'd2',
    evidence_status: 'verified', backing_kind: 'table',
    backing_ref: 'kb:kb-1:purchase_order' },
];

const links = [
  { id: 'l1', from_object_id: 'o1', to_object_id: 'o2',
    link_type: 'belongs_to', evidence_status: 'verified',
    cardinality: 'N:1' },
  { id: 'l2', from_object_id: 'o3', to_object_id: 'o1',
    link_type: 'fulfills', evidence_status: 'proposed',
    cardinality: 'N:M' },
];

const mapPayload = {
  org_units: [], capabilities: [], processes: [], families: [],
  systems: [
    { id: 'kb-1', name: 'Sales DW', resource_kind: 'knowledge_base',
      ref_id: 'kb-1', evidence_status: 'verified' },
  ],
  process_links: [],
  overview: { observed: { available: false } },
};

const rulesList = {
  items: [
    { id: 'br1', statement: 'PROHIBIT: selling below cost', status: 'enabled',
      scope: 'object', scope_ref_id: 'o1', applies_to: 'action',
      effect: 'block' },
    { id: 'br2', statement: 'Review bulk discounts', status: 'verified',
      scope: 'org', scope_ref_id: null, applies_to: 'action',
      effect: 'informational' },
  ],
  total: 2, page: 1, page_size: 50,
};

const actions = [
  { id: 'a1', name: 'place_order', description: 'Place a customer order',
    risk_tier: 'high', requires_approval: true, object_id: 'o1' },
  { id: 'a2', name: 'query_sales', description: 'Read sales figures',
    risk_tier: 'low', requires_approval: false, object_id: null },
];

beforeEach(() => {
  mockFetchGlobal = vi.fn(async (url) => {
    const u = String(url);
    if (u.includes('/ontology/overview')) {
      return { ok: true, json: async () => overview };
    }
    if (u.includes('/ontology/objects')) return { ok: true, json: async () => objects };
    if (u.includes('/ontology/links')) return { ok: true, json: async () => links };
    if (u.includes('/enterprise/map')) return { ok: true, json: async () => mapPayload };
    if (u.includes('/enterprise/business-rules/br1')) {
      return { ok: true, json: async () => ({ ...rulesList.items[0],
        resolved_evidence: [], review_history: [] }) };
    }
    if (u.includes('/enterprise/business-rules')) {
      return { ok: true, json: async () => rulesList };
    }
    if (u.includes('/ontology/actions')) return { ok: true, json: async () => actions };
    if (u.includes('/ontology/bindings')) {
      return { ok: true, json: async () => overview.bindings };
    }
    if (u.includes('/enterprise/business-objects/o1')) {
      return { ok: true, json: async () => ({
        id: 'o1', name: 'Sales Order', description: 'A confirmed customer order',
        evidence_status: 'verified', domain: { id: 'd1', name: 'Sales' },
        links: [{ id: 'l1', link_type: 'belongs_to', peer_id: 'o2',
                  peer_name: 'Customer', evidence_status: 'verified' }],
        processes: [], backing: { kind: 'table',
          ref: 'kb:kb-1:sales_order',
          resource: { id: 'kb-1', name: 'Sales DW', kind: 'knowledge_base' } },
        related_rules: [] }) };
    }
    if (u.includes('/enterprise/assistant-context')) {
      return { ok: true, json: async () => ({
        status: 'unavailable', warnings: [], evidence_coverage: 0,
        token_budget: { estimated_tokens: 0, budget_tokens: 2000,
                        included_items: 0, omitted_items: 0 },
        project: {}, business_rules: [], ontology: { objects: [] } }) };
    }
    return { ok: true, json: async () => ({}) };
  });
});

describe('EnterpriseOntology — one page, four sections (redesign)', () => {
  it('renders exactly four sections: Overview, Business Model, Business Logic, AI Intelligence', async () => {
    mount('/');
    await waitFor(() => expect(screen.getByRole('button', { name: 'Overview' })).toBeInTheDocument());
    for (const s of ['Business Model', 'Business Logic', 'AI Intelligence']) {
      expect(screen.getByRole('button', { name: s })).toBeInTheDocument();
    }
    // the old navigation clutter is gone from THIS page
    for (const gone of ['Situation Room', 'Executive Overview', 'Review',
                        'Object Explorer', 'Enterprise Map']) {
      expect(screen.queryByRole('button', { name: gone })).toBeNull();
    }
  });

  it('overview shows the architecture flow and real metrics only', async () => {
    mount('/');
    await waitFor(() => expect(screen.getAllByText('Enterprise Ontology').length).toBeGreaterThan(1));
    for (const layer of ['Business Data', 'Enterprise Ontology',
                         'AI Intelligence', 'Business Decisions']) {
      expect(screen.getAllByText(layer).length).toBeGreaterThan(0);
    }
    // five compact metrics from actual backend data
    expect(screen.getByText('Business objects')).toBeInTheDocument();
    const five = screen.getByText('5');                 // objects total
    expect(five).toBeInTheDocument();
    expect(screen.getByText('4')).toBeInTheDocument();  // relationships (links)
    // value 1 appears for both data sources and connected agents metrics
    expect(screen.getAllByText('1').length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText('Sales DW')).toBeInTheDocument(); // data source name
  });

  it('business model renders objects and relationships; click opens the drawer', async () => {
    mount('/?view=model');
    // 'Sales Order' appears as a graph node AND in the relationship list
    await waitFor(() => expect(screen.getAllByText('Sales Order').length).toBeGreaterThan(1));
    expect(screen.getAllByText('Purchase Order').length).toBeGreaterThan(0);
    // relationship edges are listed with source → target semantics
    expect(screen.getAllByText(/belongs_to/i).length).toBeGreaterThan(0);
    fireEvent.click(screen.getAllByText('Sales Order')[0]);
    // the reused right-side detail drawer loads authoritative detail
    await waitFor(() => expect(screen.getByText('A confirmed customer order')).toBeInTheDocument());
    // related object appears in the drawer (node + edge list already show it)
    expect(screen.getAllByText('Customer').length).toBeGreaterThanOrEqual(3);
  });

  it('business logic presents rules and actions read-only', async () => {
    mount('/?view=logic');
    await waitFor(() => expect(screen.getByText(/selling below cost/)).toBeInTheDocument());
    expect(screen.getByText(/Review bulk discounts/)).toBeInTheDocument();
    // enforcement distinction without badge clutter
    expect(screen.getAllByText(/blocks/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/Place a customer order/)).toBeInTheDocument();
    const actionsCard = screen.getByText('Business actions').closest('div');
    expect(within(actionsCard).getByText(/approval required/i)).toBeInTheDocument();
    // read-only: no lifecycle buttons on this page; the old "administration
    // area" pointer is gone with the enterprise-profile page — the note now
    // points to governed admin operations instead
    expect(screen.queryByRole('button', { name: 'Enable' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Confirm' })).toBeNull();
    expect(screen.queryByText(/administration area/i)).toBeNull();
    expect(screen.getByText(/authorized administrators/i)).toBeInTheDocument();
  });

  it('AI intelligence reuses the workforce context explainer', async () => {
    mount('/?view=ai');
    await waitFor(() => expect(screen.getByText('general_assistant')).toBeInTheDocument());
    expect(screen.getByText(/What does an assistant see\?/)).toBeInTheDocument();
  });

  it('section navigation writes URL state (shareable, refresh-safe)', async () => {
    mount('/');
    await waitFor(() => expect(screen.getByRole('button', { name: 'Overview' })).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Business Logic' }));
    await waitFor(() => expect(screen.getByTestId('loc').textContent).toBe('?view=logic'));
  });

  it('empty ontology shows honest empty states, never fabricated values', async () => {
    mockFetchGlobal = vi.fn(async (url) => {
      const u = String(url);
      if (u.includes('/ontology/overview')) {
        return { ok: true, json: async () => ({
          org_id: 'org-acme', domains: [],
          totals: { domains: 0, objects: 0, objects_live: 0, actions: 0,
                    actions_executable: 0, links: 0 },
          bindings: [] }) };
      }
      if (u.includes('/enterprise/business-rules')) {
        return { ok: true, json: async () => ({ items: [], total: 0, page: 1, page_size: 50 }) };
      }
      return { ok: true, json: async () => [] };
    });
    mount('/');
    await waitFor(() => expect(screen.getByText(/Nothing connected yet/i)).toBeInTheDocument());
  });

  it('failed section loads show unavailable, not zeros', async () => {
    mockFetchGlobal = vi.fn(async (url) => {
      if (String(url).includes('/ontology/overview')) {
        return { ok: false, status: 500 };
      }
      return { ok: true, json: async () => [] };
    });
    mount('/');
    await waitFor(() => expect(screen.getAllByText(/unavailable/i).length).toBeGreaterThan(0));
  });
});
