/**
 * Phase 8 T8-10 — RunProvenancePanel: sources, tools, evidence, status.
 *
 * Reads GET /api/agent-runs/{run_id}/provenance and renders:
 *  - which resources/tools the run used (entries);
 *  - run status verbatim (failed runs show as incomplete/failed);
 *  - honest "not recorded" when the envelope is missing.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import React from 'react';

const fetchMock = vi.fn();
vi.mock('@/api/authFetch', () => ({ authFetch: (...a) => fetchMock(...a) }));

import RunProvenancePanel from './RunProvenancePanel';

const ENVELOPE = {
  version: 'prov-v1',
  run: {
    run_id: 'r1', org_id: 'o1', agent_name: 'analyst',
    status: 'completed', parent_run_id: null,
    conversation_id: 'c1', user_id: 'u1',
  },
  entries: [
    { source_type: 'agent_tool_call', tool_name: 'ask_data_agent',
      source_ref: 'agent_run:r1:step:s1', status: 'ok', retrieved: true,
      org_id: 'o1', execution_ref: 'agent_run:r1:step:s1' },
    { source_type: 'agent_tool_call', tool_name: 'search_documents',
      source_ref: 'agent_run:r1:step:s2', status: 'error', retrieved: true,
      org_id: 'o1', execution_ref: 'agent_run:r1:step:s2' },
  ],
};

describe('RunProvenancePanel', () => {
  beforeEach(() => fetchMock.mockReset());

  it('renders tools, status and failed-tool honesty', async () => {
    fetchMock.mockResolvedValue({ ok: true, json: async () => ENVELOPE });
    render(<RunProvenancePanel runId="r1" />);
    await waitFor(() => expect(screen.getByText('ask_data_agent')).toBeTruthy());
    expect(screen.getByText('search_documents')).toBeTruthy();
    expect(screen.getAllByText(/failed/i).length).toBeGreaterThan(0); // error step shown
    expect(screen.getByText(/completed/i)).toBeTruthy();    // run status
  });

  it('shows parent linkage when present', async () => {
    fetchMock.mockResolvedValue({ ok: true, json: async () => ({
      ...ENVELOPE,
      run: { ...ENVELOPE.run, parent_run_id: 'parent-9' } }) });
    render(<RunProvenancePanel runId="r1" />);
    await waitFor(() => screen.getByText('ask_data_agent'));
    expect(screen.getByText(/parent-9/)).toBeTruthy();
  });

  it('honestly reports a missing envelope', async () => {
    fetchMock.mockResolvedValue({ ok: true, json: async () => ({
      run_id: 'r1', version: 'prov-v1', run: null, entries: [] }) });
    render(<RunProvenancePanel runId="r1" />);
    await waitFor(() =>
      expect(screen.getByText(/not recorded/i)).toBeTruthy());
  });

  it('honestly reports a failed fetch', async () => {
    fetchMock.mockResolvedValue({ ok: false, json: async () => ({}) });
    render(<RunProvenancePanel runId="r1" />);
    await waitFor(() =>
      expect(screen.getByText(/not recorded/i)).toBeTruthy());
  });
});
