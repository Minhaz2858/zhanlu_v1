/**
 * DataSourcesSection — bound-chip crash regression (2026-11-01).
 *
 * The agent detail page (/my-space/agent/:id) crashed with
 * "Cannot read properties of undefined (reading 'db_type')".
 *
 * Root cause: the chip renderer looks each bound KB id up in
 * ``available`` — a FILTERED view of allKbs (drops unknown source_kind
 * rows and project-mismatched KBs). A bound id missing from that view
 * (deleted KB, project mismatch after multi-project sharing stopped
 * rewriting a KB's project on attach) makes ``kb`` undefined, and the
 * unguarded fallback ``|| kb.db_type`` throws, killing the whole page.
 *
 * Contract: a bound id that resolves to no KB must render as a plain
 * chip (label = the id, no type badge) — never throw.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
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

vi.mock('./AgentParts', () => ({
  Section: ({ title, children }) => <div data-testid="section">{title}{children}</div>,
}));

import DataSourcesSection from './DataSourcesSection.jsx';

describe('DataSourcesSection — bound chip that resolves to no KB', () => {
  beforeEach(() => {
    listMock.mockResolvedValue([
      // A visible, bindable KB — but NOT the one bound on the agent.
      { id: 'kb-visible', name: 'Visible KB', source_kind: 'database', db_type: 'mysql', project: 'global' },
    ]);
  });

  it('does not crash when a bound id is missing from the available list', () => {
    // form.project = 'Finance' while the bound KB lives in another project
    // (or was deleted) — the filtered `available` view omits it.
    expect(() =>
      render(
        <DataSourcesSection
          form={{ project: 'Finance', knowledge_bases: ['kb-missing'] }}
          update={() => {}}
          t={{ agentConfig: { dataSources: 'Data sources' } }}
        />
      )
    ).not.toThrow();
    // Fallback label: the raw id is shown instead of a name.
    expect(screen.getByText('kb-missing')).toBeTruthy();
  });

  it('shows db_type badge for a resolved database KB', async () => {
    render(
      <DataSourcesSection
        form={{ project: 'global', knowledge_bases: ['kb-visible'] }}
        update={() => {}}
        t={{ agentConfig: { dataSources: 'Data sources' } }}
      />
    );
    // KB list loads asynchronously — wait for the chip to resolve.
    await waitFor(() => expect(screen.getByText('Visible KB')).toBeTruthy());
    expect(screen.getByText('mysql')).toBeTruthy();
  });
});
