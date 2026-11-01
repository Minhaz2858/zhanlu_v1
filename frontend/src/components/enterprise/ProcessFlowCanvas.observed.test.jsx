import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';

// ResizeObserver is required by @xyflow/react in happy-dom
class RO {
  observe() {}
  unobserve() {}
  disconnect() {}
}
global.ResizeObserver = global.ResizeObserver || RO;

const detailWith = (activities, observed) => ({
  id: 'proc-1',
  name: 'Order to Cash',
  discovery_status: 'documented',
  evidence_status: 'verified',
  activities,
  related_processes: [],
  observed_executions: observed,
});

const act = (seq, name, extra = {}) => ({
  seq, name, activity_type: 'human_task',
  performer: 'Ops', evidence_status: 'verified', ...extra,
});

const baseDetail = detailWith(
  [act(1, 'Capture order'), act(2, 'Check stock')],
  { execution_count: 0, activity_counts: {}, confidence_distribution: { high: 0, medium: 0, low: 0 }, time_range: null, note: null },
);

const observedDetail = detailWith(
  [act(1, 'Capture order'), act(2, 'Check stock', { evidence_status: 'proposed' })],
  {
    execution_count: 5,
    activity_counts: { '1': 4, '2': 1 },
    confidence_distribution: { high: 3, medium: 1, low: 1 },
    time_range: { from: '2026-10-30T08:00:00Z', to: '2026-11-01T09:00:00Z', count: 5 },
    note: 'observed ≠ verified ≠ documented',
  },
);

const observationsPayload = {
  process_id: 'proc-1',
  execution_count: 5,
  activity_counts: { '1': 4, '2': 1 },
  confidence_distribution: { high: 3, medium: 1, low: 1 },
  time_range: { from: '2026-10-30T08:00:00Z', to: '2026-11-01T09:00:00Z', count: 5 },
  references: [
    { record_type: 'nl2sql_query', record_id: 'q-1', activity_seq: 1, match_confidence: 0.4, match_method: 'lineage', observed_at: '2026-10-30T08:00:00Z' },
    { record_type: 'agent_run', record_id: 'r-1', activity_seq: 1, match_confidence: 0.8, match_method: 'tool_binding', observed_at: '2026-11-01T09:00:00Z' },
    { record_type: 'agent_run', record_id: 'r-2', activity_seq: 2, match_confidence: null, match_method: 'explicit', observed_at: '2026-10-31T07:00:00Z' },
  ],
};

let fetchUrls = [];
const mockFetch = vi.fn(async (url) => {
  fetchUrls.push(String(url));
  if (String(url).includes('/observations')) {
    return { ok: true, json: async () => observationsPayload };
  }
  return { ok: true, json: async () => baseDetail };
});

vi.mock('@/api/authFetch', () => ({
  authFetch: (...args) => mockFetch(...args),
}));

import ProcessFlowCanvas from './ProcessFlowCanvas';
import { activityObservation } from './observedExecution';

describe('activityObservation (pure aggregation)', () => {
  it('returns zeroed state for activities without observations', () => {
    const obs = activityObservation(observedDetail.observed_executions, observationsPayload.references, 3);
    expect(obs.count).toBe(0);
    expect(obs.observed).toBe(false);
  });

  it('aggregates count, max confidence, band, latest time per activity', () => {
    const obs = activityObservation(observedDetail.observed_executions, observationsPayload.references, 1);
    expect(obs.count).toBe(2);
    expect(obs.max_confidence).toBe(0.8);
    expect(obs.band).toBe('high');
    expect(obs.latest_observed_at).toBe('2026-11-01T09:00:00Z');
    expect(obs.methods).toContain('lineage');
    expect(obs.record_types).toContain('agent_run');
  });

  it('treats missing confidence as unknown band, never fabricated', () => {
    const obs = activityObservation(observedDetail.observed_executions, observationsPayload.references, 2);
    expect(obs.count).toBe(1);
    expect(obs.max_confidence).toBeNull();
    expect(obs.band).toBe('unknown');
  });

  it('handles missing summary and references gracefully', () => {
    const obs = activityObservation(null, undefined, 1);
    expect(obs.observed).toBe(false);
    expect(obs.count).toBe(0);
    expect(obs.band).toBe('unknown');
  });
});

describe('ProcessFlowCanvas observed-execution integration', () => {
  beforeEach(() => {
    fetchUrls = [];
    mockFetch.mockClear();
    mockFetch.mockImplementation(async (url) => {
      fetchUrls.push(String(url));
      if (String(url).includes('/observations')) {
        return { ok: true, json: async () => observationsPayload };
      }
      return { ok: true, json: async () => baseDetail };
    });
  });

  it('renders existing canvas behavior without observations (no observation fetch)', async () => {
    render(<ProcessFlowCanvas processId="proc-1" onClose={() => {}} onOpenDetail={() => {}} />);
    await waitFor(() => expect(screen.getByText('Order to Cash')).toBeInTheDocument());
    expect(screen.getByText('Capture order')).toBeInTheDocument();
    // zero observations → the observation endpoint is NEVER hit
    expect(fetchUrls.filter((u) => u.includes('/observations'))).toHaveLength(0);
    expect(screen.queryByText(/observed executions/i)).not.toBeInTheDocument();
  });

  it('shows observed badge data and fetches references ONCE for the whole canvas', async () => {
    mockFetch.mockImplementation(async (url) => {
      fetchUrls.push(String(url));
      if (String(url).includes('/observations')) {
        return { ok: true, json: async () => observationsPayload };
      }
      return { ok: true, json: async () => observedDetail };
    });
    render(<ProcessFlowCanvas processId="proc-1" onClose={() => {}} onOpenDetail={() => {}} />);
    await waitFor(() => expect(screen.getByText('Order to Cash')).toBeInTheDocument());
    await waitFor(() => expect(
      fetchUrls.filter((u) => u.includes('/observations')),
    ).toHaveLength(1)); // one request per canvas, not per activity
  });

  it('activity detail panel exposes observed summary without payloads', async () => {
    mockFetch.mockImplementation(async (url) => {
      fetchUrls.push(String(url));
      if (String(url).includes('/observations')) {
        return { ok: true, json: async () => observationsPayload };
      }
      return { ok: true, json: async () => observedDetail };
    });
    render(<ProcessFlowCanvas processId="proc-1" onClose={() => {}} onOpenDetail={() => {}} />);
    await waitFor(() => expect(
      fetchUrls.filter((u) => u.includes('/observations')),
    ).toHaveLength(1));
    // click the first activity node
    const node = await waitFor(() => screen.getByText('Capture order'));
    fireEvent.click(node);
    // observed execution count shown (aggregated from references)
    const panelHeading = await screen.findByText('Observed executions');
    const panel = panelHeading.closest('div.space-y-2') || panelHeading.parentElement.parentElement;
    // observed rows present in the panel
    expect(within(panel).getByText('Executions')).toBeInTheDocument();
    expect(within(panel).getByText('Confidence')).toBeInTheDocument();
    // payload-free: no SQL, no raw results anywhere in the panel
    expect(within(panel).queryByText(/SELECT/i)).not.toBeInTheDocument();
    // documented/configured statuses remain separate rows
    expect(within(panel).getByText('Evidence')).toBeInTheDocument();
  });

  it('observation API failure degrades gracefully to the detail summary', async () => {
    mockFetch.mockImplementation(async (url) => {
      fetchUrls.push(String(url));
      if (String(url).includes('/observations')) {
        return { ok: false, status: 500 };
      }
      return { ok: true, json: async () => observedDetail };
    });
    render(<ProcessFlowCanvas processId="proc-1" onClose={() => {}} onOpenDetail={() => {}} />);
    await waitFor(() => expect(screen.getByText('Order to Cash')).toBeInTheDocument());
    // canvas still renders; no crash
    expect(screen.getByText('Capture order')).toBeInTheDocument();
  });

  it('cross-process navigation still works (peer + detail callbacks)', async () => {
    const onOpenDetail = vi.fn();
    const withPeer = {
      ...baseDetail,
      related_processes: [{ peer_id: 'proc-2', peer_name: 'Fulfillment', direction: 'successor' }],
    };
    mockFetch.mockImplementation(async (url) => {
      fetchUrls.push(String(url));
      if (String(url).includes('/observations')) {
        return { ok: true, json: async () => observationsPayload };
      }
      return { ok: true, json: async () => withPeer };
    });
    render(<ProcessFlowCanvas processId="proc-1" onClose={() => {}} onOpenDetail={onOpenDetail} />);
    const peer = await screen.findByText('Fulfillment');
    fireEvent.click(peer);
    expect(onOpenDetail).toHaveBeenCalledWith('proc-2', true);
  });
});
