import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom';

class RO { observe() {} unobserve() {} disconnect() {} }
global.ResizeObserver = global.ResizeObserver || RO;

let mockFetchGlobal = vi.fn();
vi.mock('@/api/authFetch', () => ({ authFetch: (...args) => mockFetchGlobal(...args) }));

const authState = { isAdmin: false };
vi.mock('@/lib/AuthContext', () => ({
  useAuth: () => authState,
}));
vi.mock('sonner', () => ({ toast: { error: vi.fn(), success: vi.fn() } }));

import EnterpriseProfile from './EnterpriseProfile';

function LocationProbe() {
  const loc = useLocation();
  return <div data-testid="loc">{loc.search}</div>;
}

function mount(url) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <LocationProbe />
      <Routes>
        <Route path="*" element={<EnterpriseProfile />} />
      </Routes>
    </MemoryRouter>,
  );
}

const mapPayload = {
  org_units: [], capabilities: [],
  processes: [{ id: 'p1', name: 'Order to Cash', process_family: 'Sales',
                discovery_status: 'documented', evidence_status: 'verified',
                activities_count: 1, responsible_units: ['Operations'] }],
  families: [], systems: [
    { id: 'kb-1', name: 'Sales DW', resource_kind: 'knowledge_base',
      ref_id: 'kb-1', evidence_status: 'verified' },
  ],
  process_links: [],
  overview: { observed: { available: false } },
};

beforeEach(() => {
  mockFetchGlobal = vi.fn(async (url) => {
    const u = String(url);
    if (u.includes('/ontology/overview')) {
      return { ok: true, json: async () => ({ domains: [] }) };
    }
    if (u.includes('/ontology/objects')) return { ok: true, json: async () => [] };
    if (u.includes('/ontology/coverage-value')) {
      return { ok: true, json: async () => ({ domains: [] }) };
    }
    if (u.includes('/enterprise/map')) {
      return { ok: true, json: async () => mapPayload };
    }
    if (u.includes('/enterprise/business-rules/br1')) {
      return { ok: true, json: async () => ({
        id: 'br1', statement: 'PROHIBIT: selling below cost', status: 'enabled',
        scope: 'object', applies_to: 'action', effect: 'block',
        source_evidence_ids: [], review_history: [], resolved_evidence: [] }) };
    }
    if (u.includes('/enterprise/business-rules')) {
      return { ok: true, json: async () => ({ items: [], total: 0, page: 1, page_size: 50 }) };
    }
    if (u.includes('/ontology/bindings')) return { ok: true, json: async () => [] };
    if (u.includes('/enterprise/business-objects/o1')) {
      return { ok: true, json: async () => ({
        id: 'o1', name: 'Order', evidence_status: 'verified', domain: null,
        links: [], processes: [], backing: {}, related_rules: [] }) };
    }
    return { ok: true, json: async () => ({}) };
  });
});

describe('EnterpriseProfile URL state & IA (Phase 6F)', () => {
  it('deep link to AI Workforce renders the workforce panel', async () => {
    mount('/?exp=W');
    await waitFor(() => expect(
      screen.getByText(/No agent-domain bindings recorded yet/)).toBeInTheDocument());
    // both the area label and the active tab are titled "AI Workforce"
    expect(screen.getAllByText('AI Workforce').length).toBeGreaterThanOrEqual(2);
  });

  it('grouped navigation shows the six conceptual areas', async () => {
    mount('/');
    await waitFor(() => expect(screen.getByText('Enterprise Map')).toBeInTheDocument());
    for (const area of ['Business Overview', 'How We Work', 'Business Knowledge',
                        'Enterprise Resources', 'AI Workforce', 'Administration']) {
      expect(screen.getAllByText(area).length).toBeGreaterThan(0);
    }
  });

  it('clicking a tab writes the URL state (shareable deep-link)', async () => {
    mount('/');
    await waitFor(() => expect(screen.getByText('Enterprise Map')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /Business Rules/ }));
    await waitFor(() => expect(screen.getByTestId('loc').textContent).toBe('?exp=K'));
  });

  it('invalid tab falls back to the Enterprise Map', async () => {
    mount('/?exp=ZZZ');
    await waitFor(() => expect(screen.getByText('Enterprise Map')).toBeInTheDocument());
  });

  it('admin-only Review falls back for non-admins, renders for admins', async () => {
    mount('/?exp=R');
    await waitFor(() => expect(screen.getByText('Enterprise Map')).toBeInTheDocument());
    authState.isAdmin = true;
    mount('/?exp=R');
    await waitFor(() => expect(screen.getByText('Review')).toBeInTheDocument());
    authState.isAdmin = false;
  });

  it('refresh preserves the full view: rules deep-link with rule id', async () => {
    mount('/?exp=K&rule=br1');
    await waitFor(() => expect(screen.getByText(/selling below cost/)).toBeInTheDocument());
    // the rule detail fetch used the deep-linked id
    const calls = mockFetchGlobal.mock.calls.filter((c) =>
      String(c[0]).endsWith('/enterprise/business-rules/br1'));
    expect(calls.length).toBe(1);
  });

  it('deep-linked object ref opens the object drilldown', async () => {
    mount('/?object=o1');
    await waitFor(() => expect(screen.getByText('Order')).toBeInTheDocument());
  });

  it('resources tab lists enterprise systems from the map payload', async () => {
    mount('/?exp=S');
    await waitFor(() => expect(screen.getByText('Sales DW')).toBeInTheDocument());
  });
});
