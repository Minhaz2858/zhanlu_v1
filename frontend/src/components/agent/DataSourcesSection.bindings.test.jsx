/**
 * Phase 8 T8-4 — DataSourcesSection binding editor (descriptor mode).
 *
 * When an agentId is provided and the source-descriptors API responds,
 * the section renders bindings from the structured descriptor contract
 * (name/type/readiness/enabled) and drives changes through the binding
 * API (POST/PATCH/DELETE) — while keeping form.knowledge_bases in sync.
 * When the API is unavailable, the legacy JSON picker behavior is kept
 * (fallback; non-migrated agents keep working unchanged).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import React from 'react';

vi.mock('@/lib/LanguageProvider', () => ({
  useLanguage: () => ({
    lang: 'en',
    t: { kb: { dbTypes: {}, fileTypes: {} }, agentConfig: {} },
  }),
}));

const listMock = vi.fn();
vi.mock('@/api/base44Client', () => ({
  base44: { entities: { KnowledgeBase: { list: (...a) => listMock(...a) } } },
}));

const fetchMock = vi.fn();
vi.mock('@/api/authFetch', () => ({ authFetch: (...a) => fetchMock(...a) }));

vi.mock('./AgentParts', () => ({
  Section: ({ title, children }) => <div data-testid="section">{title}{children}</div>,
}));

import DataSourcesSection from './DataSourcesSection.jsx';

const DESCRIPTORS = [
  { id: 'ds-1', name: 'Sales DW', resource_type: 'database', source_kind: 'database', db_type: 'mysql', binding_id: 'b-1', access_mode: 'read_only', is_enabled: true, readiness: 'ready', queryable: true },
  { id: 'ds-2', name: 'Contracts', resource_type: 'knowledge_base', source_kind: 'file', db_type: null, binding_id: 'b-2', access_mode: 'read_only', is_enabled: false, readiness: 'ready', queryable: false },
];

function setup({ descriptorsFail = false } = {}) {
  // stateful descriptor store — mutations are visible on re-fetch
  const store = DESCRIPTORS.map((d) => ({ ...d }));
  fetchMock.mockImplementation(async (url, opts = {}) => {
    const u = String(url);
    const method = opts.method || 'GET';
    if (u.endsWith('/source-descriptors')) {
      if (descriptorsFail) return { ok: false, json: async () => ({}) };
      return { ok: true, json: async () => ({ descriptors: store }) };
    }
    const m = u.match(/bindings\/data\/(.+)$/);
    if (m && method === 'DELETE') {
      const i = store.findIndex((d) => d.binding_id === m[1]);
      if (i >= 0) store.splice(i, 1);
      return { ok: true, json: async () => ({}) };
    }
    if (m && method === 'PATCH') {
      const d = store.find((x) => x.binding_id === m[1]);
      if (d) d.is_enabled = JSON.parse(opts.body).is_enabled;
      return { ok: true, json: async () => ({}) };
    }
    return { ok: true, json: async () => ({}) };
  });
  listMock.mockResolvedValue([]);
  const update = vi.fn();
  const onChange = vi.fn().mockResolvedValue();
  render(
    <DataSourcesSection
      form={{ project: 'global', knowledge_bases: ['ds-1', 'ds-2'] }}
      update={update}
      onChange={onChange}
      agentId="app-1"
      t={{ agentConfig: { dataSources: 'Data sources',
        addDataSource: 'Add Data Source' } }}
    />,
  );
  return { update, onChange };
}

describe('DataSourcesSection — binding editor (descriptor mode)', () => {
  beforeEach(() => {
    fetchMock.mockReset();
  });

  it('renders bindings from the descriptor contract with type badges', async () => {
    setup();
    await waitFor(() => expect(screen.getByText('Sales DW')).toBeTruthy());
    expect(screen.getByText('Contracts')).toBeTruthy();
    expect(screen.getByText('disabled')).toBeTruthy(); // honest state chip
  });

  it('removes a binding via DELETE and keeps the form projection in sync', async () => {
    const { update } = setup();
    await waitFor(() => expect(screen.getByText('Sales DW')).toBeTruthy());
    const removeButtons = screen.getAllByRole('button', { name: /remove/i });
    fireEvent.click(removeButtons[0]);
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/agent-studio/app-1/bindings/data/b-1',
        expect.objectContaining({ method: 'DELETE' })));
    await waitFor(() =>
      expect(update).toHaveBeenCalledWith({ knowledge_bases: ['ds-2'] }));
  });

  it('toggles a disabled binding back on via PATCH', async () => {
    const { update } = setup();
    await waitFor(() => expect(screen.getByText('Contracts')).toBeTruthy());
    const enableButtons = screen.getAllByRole('button', { name: /enable/i });
    fireEvent.click(enableButtons[0]);
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/agent-studio/app-1/bindings/data/b-2',
        expect.objectContaining({ method: 'PATCH' })));
  });

  it('falls back to the legacy picker when the descriptors API fails', async () => {
    setup({ descriptorsFail: true });
    await waitFor(() =>
      expect(screen.getByText(/Add Data Source/i)).toBeTruthy());
    // legacy chip path still renders from form.knowledge_bases
    expect(screen.getByText('ds-1')).toBeTruthy();
  });
});
