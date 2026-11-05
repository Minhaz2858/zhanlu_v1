import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

vi.mock('@/lib/LanguageProvider', () => ({ useLanguage: () => ({ lang: 'en' }) }));
vi.mock('@/api/authFetch', () => ({
  authFetch: vi.fn(async (url) => {
    if (String(url).includes('/evidence')) {
      return {
        ok: true,
        json: async () => ({
          evidence_id: 'ev-1',
          producer_agent: 'Inventory Management Agent',
          source_id: 'ds-1',
          validation_status: 'validated',
          findings: { on_hand: 42 },
          freshness: { as_of: '2026-10-31', state: 'current' },
          query_provenance: { sql: 'SELECT SECRET_COL FROM t', nl: 'stock', rows: [{ SECRET_COL: 1 }] },
          metric_definition_ids: ['m1'],
          references: ['ev-0'],
          assumptions: 'steady demand',
          limitations: 'sample only',
        }),
      };
    }
    return { ok: true, json: async () => ({ roots: [], summary: {} }) };
  }),
}));

import AgentExecutionTree, { buildTreeFromLiveEvents } from './AgentExecutionTree';

const SUPERVISOR_TREE = {
  roots: [
    {
      id: 'r1', agent_name: 'general_assistant', role: 'supervisor', status: 'completed', phase: 'completed',
      task: 'Why did defect rate spike?', duration_ms: 30000, token_usage: { total_tokens: 500 }, cost_amount: 0.01,
      evidence_ids: [], tools: ['tool_a', 'tool_b'], children: [
        {
          id: 'c1', agent_name: 'Quality Control Agent', role: 'specialist', delegation_mode: 'supervisor',
          status: 'completed', phase: 'completed', task: 'Investigate defects', duration_ms: 18000,
          token_usage: { total_tokens: 220 }, cost_amount: 0.004, evidence_ids: ['ev-1'], children: [],
        },
        {
          id: 'c2', agent_name: 'Supply Chain Agent', role: 'specialist', delegation_mode: 'peer',
          status: 'completed', phase: 'completed', task: 'Check inventory', duration_ms: 9000,
          token_usage: { total_tokens: 90 }, cost_amount: 0.002, evidence_ids: [], children: [],
        },
      ],
    },
  ],
  summary: {
    total: 3, completed: 3, partial: 0, failed: 0, peer_collaborations: 1, reviewer_status: 'approved',
    total_tokens: 810, estimated_cost: 0.016, evidence_count: 1, duration_ms: 30000,
  },
};

describe('AgentExecutionTree', () => {
  beforeEach(() => vi.clearAllMocks());

  it('renders the parent→child tree with supervisor and peer distinction', () => {
    render(<AgentExecutionTree tree={SUPERVISOR_TREE} conversationId="conv-x" />);
    expect(screen.getByText('general_assistant')).toBeTruthy();
    expect(screen.getByText('Quality Control Agent')).toBeTruthy();
    expect(screen.getByText('Supply Chain Agent')).toBeTruthy();
    // Peer collaboration is visually distinguished.
    expect(screen.getByText('Peer')).toBeTruthy();
    // Compact summary numbers.
    expect(screen.getAllByText('3').length).toBeGreaterThan(0);
    expect(screen.getByText('Agents', { exact: false })).toBeTruthy();
    expect(screen.getByText('Peer collab', { exact: false })).toBeTruthy();
  });

  it('does not render when there are no roots', () => {
    const { container } = render(<AgentExecutionTree tree={{ roots: [], summary: {} }} />);
    expect(container.querySelector('[data-testid="agent-execution-tree"]')).toBeNull();
  });

  it('shows reviewer panel when a node carries a review (root expanded by default)', () => {
    const tree = {
      roots: [{
        id: 'rv', agent_name: 'general_assistant', role: 'supervisor', status: 'completed', phase: 'reviewing',
        review: {
          overall: 'needs_revision',
          dimensions: { numerical_accuracy: 'fail', evidence_completeness: 'warn' },
          issues: ['gap'], needs_evidence: ['stock'], follow_up_count: 1,
        },
        children: [],
      }],
      summary: { reviewer_status: 'needs_revision' },
    };
    render(<AgentExecutionTree tree={tree} conversationId="conv-x" />);
    expect(screen.getByText('Critic / Reviewer')).toBeTruthy();
    expect(screen.getByText('Numerical accuracy')).toBeTruthy();
    expect(screen.getAllByText(/Needs more evidence/).length).toBeGreaterThan(0);
  });

  it('resolves an evidence reference through the REST API when clicked', async () => {
    const tree = {
      roots: [{
        id: 'rv', agent_name: 'general_assistant', role: 'supervisor', status: 'completed', phase: 'completed',
        evidence_ids: ['ev-1'], children: [],
      }],
      summary: { evidence_count: 1 },
    };
    render(<AgentExecutionTree tree={tree} conversationId="conv-x" />);
    const chip = screen.getByText('ev-1');
    fireEvent.click(chip);
    await waitFor(() => expect(screen.getByText('Inventory Management Agent')).toBeTruthy());
  });

  it('renders an in-flight running node truthfully from the authoritative tree (H1-D)', () => {
    // Reconnection mid-execution: the backend tree reports a child still
    // running — the UI must show running, never fabricated completion.
    const tree = {
      roots: [
        { id: 'a1', agent_name: 'Inventory Management Agent', role: 'specialist', status: 'completed', phase: 'completed', children: [] },
        { id: 'a2', agent_name: 'Sales Performance Agent', role: 'specialist', status: 'running', phase: 'running', children: [] },
      ],
      summary: { total: 2, completed: 1 },
    };
    render(<AgentExecutionTree tree={tree} conversationId="conv-live" />);
    expect(screen.getByText('Inventory Management Agent')).toBeTruthy();
    expect(screen.getByText('Sales Performance Agent')).toBeTruthy();
    expect(screen.getAllByText(/Running|运行中/i).length).toBeGreaterThan(0);
  });

  it('renders extended evidence metadata and never exposes raw rows', async () => {
    const tree = {
      roots: [{
        id: 'rv', agent_name: 'general_assistant', role: 'supervisor', status: 'completed', phase: 'completed',
        evidence_ids: ['ev-1'], children: [],
      }],
      summary: { evidence_count: 1 },
    };
    render(<AgentExecutionTree tree={tree} conversationId="conv-x" />);
    fireEvent.click(screen.getByText('ev-1'));
    await waitFor(() => expect(screen.getByText(/Query:/)).toBeTruthy());
    expect(screen.getByText('SELECT SECRET_COL FROM t')).toBeTruthy();
    expect(screen.getByText(/Metric definitions:/)).toBeTruthy();
    expect(screen.getByText('m1')).toBeTruthy();
    expect(screen.getByText(/Built on evidence:/)).toBeTruthy();
    expect(screen.getByText('ev-0')).toBeTruthy();
    expect(screen.getByText(/Limitations/)).toBeTruthy();
    // Security: even though the payload carried raw rows, they must not render.
    expect(screen.queryByText('SECRET_COL')).toBeNull();
  });

  it('buildTreeFromLiveEvents synthesizes a tree from the SSE feed (no fabricated facts)', () => {
    const liveEvents = [
      { id: '1', type: 'subagent_invoked', params: { agent_label: 'Quality Control Agent', target: 'query_db', subagent_id: 'qc', task: 'investigate' } },
      { id: '2', type: 'tool_call_started', params: { tool_label: 'query_db', tool_name: 'query_db', subagent_id: 'qc' } },
      { id: '3', type: 'tool_call_finished', params: { tool_label: 'query_db', tool_name: 'query_db', subagent_id: 'qc' } },
      { id: '4', type: 'subagent_returned', params: { agent_label: 'Quality Control Agent', target: 'query_db', subagent_id: 'qc' } },
    ];
    const built = buildTreeFromLiveEvents(liveEvents);
    expect(built).not.toBeNull();
    expect(built.roots).toHaveLength(1);
    expect(built.roots[0].agent_name).toBe('Quality Control Agent');
    expect(built.roots[0].children[0].agent_name).toBe('query_db');
    expect(built.roots[0].phase).toBe('completed');
  });

  // ── Milestone §3.2: supervisor verification stage from EXISTING events ──
  it('surfaces the verification verdict in the live tree (no duplicate event system)', () => {
    const aex = 'aex-v';
    const liveEvents = [
      { id: '1', type: 'agent_started', ts: 't1', params: { agent_execution_id: aex, agent_name: 'sales_analyst', agent_label: 'Sales Analyst', delegation_mode: 'supervisor' } },
      { id: '2', type: 'agent_completed', ts: 't2', params: { agent_execution_id: aex, status: 'completed', duration_ms: 1200, evidence_ids: ['ev-9'] } },
      { id: '3', type: 'verify_passed', ts: 't3', params: { grounded_by_data: true, failed_steps: [] } },
    ];
    const built = buildTreeFromLiveEvents(liveEvents);
    expect(built.verification).toEqual({ status: 'passed', grounded_by_data: true, failed_steps: [] });
  });

  it('verification failed carries the failed steps for honest display', () => {
    const aex = 'aex-w';
    const liveEvents = [
      { id: '1', type: 'agent_started', ts: 't1', params: { agent_execution_id: aex, agent_name: 'supply_chain_agent', delegation_mode: 'supervisor' } },
      { id: '2', type: 'agent_completed', ts: 't2', params: { agent_execution_id: aex, status: 'completed' } },
      { id: '3', type: 'verify_failed', ts: 't3', params: { grounded_by_data: false, failed_steps: ['execute_query', 'run_sandbox_skill'] } },
    ];
    const built = buildTreeFromLiveEvents(liveEvents);
    expect(built.verification.status).toBe('failed');
    expect(built.verification.failed_steps).toContain('execute_query');
  });

  it('renders the verification badge in the panel', () => {
    const tree = {
      roots: [{ id: 'rv', agent_name: 'sales_analyst', role: 'specialist', status: 'completed', phase: 'completed', children: [] }],
      summary: { total: 1, completed: 1 },
      verification: { status: 'failed', failed_steps: ['execute_query'], grounded_by_data: false },
    };
    render(<AgentExecutionTree tree={tree} conversationId="conv-x" />);
    const badge = screen.getByTestId('agent-verification-badge');
    expect(badge).toBeTruthy();
    expect(badge.textContent).toContain('failed');
    expect(badge.textContent).toContain('execute_query');
  });

  // ── Final acceptance: verification event consistency ──────────────────
  it('verify-started without a verdict at stream end becomes NOT-run (never permanent running)', () => {
    const aex = 'aex-fsm';
    const liveEvents = [
      { id: '1', type: 'agent_started', ts: 't1', params: { agent_execution_id: aex, agent_name: 'qc_agent', delegation_mode: 'supervisor' } },
      { id: '2', type: 'agent_completed', ts: 't2', params: { agent_execution_id: aex, status: 'completed' } },
      // FSM route: VERIFY state entered, then the stream died before the verdict.
      { id: '3', type: 'phase_enter', ts: 't3', label_key: 'phase_enter.verify', params: {} },
    ];
    const live = buildTreeFromLiveEvents(liveEvents, { finalize: false });
    expect(live.verification.status).toBe('running');
    const finalized = buildTreeFromLiveEvents(liveEvents, { finalize: true });
    expect(finalized.verification.status).toBe('not_run');
  });

  it('a real verdict always wins over the started state (both routes, same event names)', () => {
    const aex = 'aex-route';
    const base = [
      { id: '1', type: 'agent_started', ts: 't1', params: { agent_execution_id: aex, agent_name: 'sales_analyst', delegation_mode: 'supervisor' } },
      { id: '2', type: 'agent_completed', ts: 't2', params: { agent_execution_id: aex, status: 'completed' } },
      { id: '3', type: 'phase_enter', ts: 't3', label_key: 'phase_enter.verify', params: {} },
    ];
    // FSM route emits verify_passed without params; tool-loop route emits
    // verify_failed with verdict params — both must resolve consistently.
    const fsm = buildTreeFromLiveEvents([...base, { id: '4', type: 'verify_passed', ts: 't4', params: {} }]);
    expect(fsm.verification.status).toBe('passed');
    const loop = buildTreeFromLiveEvents([...base, { id: '4', type: 'verify_failed', ts: 't4', params: { grounded_by_data: false, failed_steps: ['run_sandbox_skill'] } }]);
    expect(loop.verification.status).toBe('failed');
  });
});
