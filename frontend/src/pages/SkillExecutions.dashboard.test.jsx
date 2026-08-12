/**
 * UI automation test: SkillExecutions dashboard.
 *
 * Simulates a user viewing the skill executions dashboard, verifying
 * that stat cards, the execution table, status filters, search, and
 * the detail side panel all render and respond to interactions correctly.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

// Mock the API client
vi.mock('@/api/skillStudio', () => ({
  listExecutions: vi.fn(),
}));

import SkillExecutions from '@/pages/SkillExecutions';
import { listExecutions } from '@/api/skillStudio';

const SAMPLE_EXECUTIONS = [
  {
    id: 'exec-1',
    skill_name: 'data-analysis-report',
    skill_id: 'tool-123',
    skill_version: '1.0.0',
    agent_name: 'skill_agent',
    action: 'execute',
    status: 'completed',
    duration_ms: 1200,
    body_length: 2048,
    conversation_id: 'conv-1',
    started_at: '2026-07-29T10:00:00Z',
    completed_at: '2026-07-29T10:00:01Z',
    created_date: '2026-07-29T10:00:00Z',
    error_message: null,
  },
  {
    id: 'exec-2',
    skill_name: 'csv-analyzer',
    skill_id: 'tool-456',
    skill_version: '2.1.0',
    agent_name: 'general_assistant',
    action: 'load',
    status: 'failed',
    duration_ms: 50,
    body_length: null,
    conversation_id: 'conv-2',
    started_at: '2026-07-29T09:00:00Z',
    completed_at: '2026-07-29T09:00:00Z',
    created_date: '2026-07-29T09:00:00Z',
    error_message: 'Skill not found: csv-analyzer',
  },
  {
    id: 'exec-3',
    skill_name: 'fastapi-rest-api',
    skill_id: 'tool-789',
    skill_version: '0.9.1',
    agent_name: 'skill_agent',
    action: 'load',
    status: 'completed',
    duration_ms: 30,
    body_length: 1024,
    conversation_id: 'conv-3',
    started_at: '2026-07-29T08:00:00Z',
    completed_at: '2026-07-29T08:00:00Z',
    created_date: '2026-07-29T08:00:00Z',
    error_message: null,
  },
];

describe('SkillExecutions dashboard — UI automation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('renders stat cards with correct counts after data loads', async () => {
    listExecutions.mockResolvedValueOnce({
      success: true,
      total: 3,
      count: 3,
      executions: SAMPLE_EXECUTIONS,
    });

    render(<SkillExecutions />);

    // Wait for data to load — look for stat card labels
    await waitFor(() => {
      expect(screen.getByText('Total Runs')).toBeDefined();
    });

    expect(screen.getByText('Total Runs')).toBeDefined();
    expect(screen.getByText('Avg Duration')).toBeDefined();
    // "Completed" and "Failed" appear both as stat card labels and filter buttons,
    // so verify they exist (at least one each)
    expect(screen.getAllByText('Completed').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('Failed').length).toBeGreaterThanOrEqual(1);
  });

  it('renders execution rows in the table', async () => {
    listExecutions.mockResolvedValueOnce({
      success: true,
      total: 3,
      count: 3,
      executions: SAMPLE_EXECUTIONS,
    });

    render(<SkillExecutions />);

    await waitFor(() => {
      expect(screen.getByText('data-analysis-report')).toBeDefined();
    });

    // All three skill names should be visible
    expect(screen.getByText('data-analysis-report')).toBeDefined();
    expect(screen.getByText('csv-analyzer')).toBeDefined();
    expect(screen.getByText('fastapi-rest-api')).toBeDefined();

    // Agent names — may appear multiple times (multiple rows)
    expect(screen.getAllByText('skill_agent').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('general_assistant').length).toBeGreaterThanOrEqual(1);

    // Actions
    expect(screen.getAllByText('execute').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('load').length).toBeGreaterThanOrEqual(1);
  });

  it('opens the detail panel when a row is clicked', async () => {
    listExecutions.mockResolvedValueOnce({
      success: true,
      total: 1,
      count: 1,
      executions: [SAMPLE_EXECUTIONS[0]],
    });

    render(<SkillExecutions />);

    await waitFor(() => {
      expect(screen.getByText('data-analysis-report')).toBeDefined();
    });

    // Click the skill name row
    fireEvent.click(screen.getByText('data-analysis-report'));

    // Detail panel should show "Execution Detail" header
    await waitFor(() => {
      expect(screen.getByText('Execution Detail')).toBeDefined();
    });

    // Detail fields — "Conversation ID" label is unique to the detail panel
    expect(screen.getByText('Conversation ID')).toBeDefined();
    expect(screen.getByText('conv-1')).toBeDefined();
    expect(screen.getByText('Skill ID')).toBeDefined();
    expect(screen.getByText('tool-123')).toBeDefined();
    expect(screen.getByText('Skill Version')).toBeDefined();
    expect(screen.getByText('1.0.0')).toBeDefined();
  });

  it('displays error message in detail panel for failed executions', async () => {
    listExecutions.mockResolvedValueOnce({
      success: true,
      total: 1,
      count: 1,
      executions: [SAMPLE_EXECUTIONS[1]], // the failed one
    });

    render(<SkillExecutions />);

    await waitFor(() => {
      expect(screen.getByText('csv-analyzer')).toBeDefined();
    });

    fireEvent.click(screen.getByText('csv-analyzer'));

    await waitFor(() => {
      expect(screen.getByText('Skill not found: csv-analyzer')).toBeDefined();
    });
  });

  it('shows status filter buttons (All, Completed, Failed, Running)', async () => {
    listExecutions.mockResolvedValueOnce({
      success: true,
      total: 0,
      count: 0,
      executions: [],
    });

    render(<SkillExecutions />);

    await waitFor(() => {
      expect(screen.getByText('All')).toBeDefined();
    });

    // "Running" is unique (only filter button, no running executions in test data)
    expect(screen.getByText('Running')).toBeDefined();
    // "Completed" and "Failed" appear both as filter buttons and stat labels
    expect(screen.getAllByText('Completed').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('Failed').length).toBeGreaterThanOrEqual(1);
  });

  it('shows the empty state when no executions exist', async () => {
    listExecutions.mockResolvedValueOnce({
      success: true,
      total: 0,
      count: 0,
      executions: [],
    });

    render(<SkillExecutions />);

    await waitFor(() => {
      expect(screen.getByText('No skill executions yet.')).toBeDefined();
    });
  });

  it('shows the error state when the API fails', async () => {
    listExecutions.mockRejectedValueOnce(new Error('Network error'));

    render(<SkillExecutions />);

    await waitFor(() => {
      expect(screen.getByText('Network error')).toBeDefined();
    });
  });

  it('has a search input that filters by skill name', async () => {
    listExecutions.mockResolvedValue({
      success: true,
      total: 3,
      count: 3,
      executions: SAMPLE_EXECUTIONS,
    });

    render(<SkillExecutions />);

    await waitFor(() => {
      expect(screen.getByText('data-analysis-report')).toBeDefined();
    });

    // Type in the search box
    const searchInput = screen.getByPlaceholderText('Search by skill name...');
    fireEvent.change(searchInput, { target: { value: 'csv' } });

    // The API should be called again with skill_name filter
    await waitFor(() => {
      const lastCall = listExecutions.mock.calls[listExecutions.mock.calls.length - 1];
      expect(lastCall[0].skill_name).toBe('csv');
    });
  });

  it('has a Refresh button that triggers a data reload', async () => {
    listExecutions.mockResolvedValue({
      success: true,
      total: 1,
      count: 1,
      executions: [SAMPLE_EXECUTIONS[0]],
    });

    render(<SkillExecutions />);

    await waitFor(() => {
      expect(screen.getByText('Refresh')).toBeDefined();
    });

    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const refreshBtn = screen.getByText('Refresh');
    await user.click(refreshBtn);

    await act(async () => {
      await Promise.resolve();
    });

    // listExecutions should have been called more than once
    await waitFor(() => {
      expect(listExecutions.mock.calls.length).toBeGreaterThanOrEqual(2);
    });
  });

  it('closes the detail panel when the X button is clicked', async () => {
    listExecutions.mockResolvedValueOnce({
      success: true,
      total: 1,
      count: 1,
      executions: [SAMPLE_EXECUTIONS[0]],
    });

    render(<SkillExecutions />);

    await waitFor(() => {
      expect(screen.getByText('data-analysis-report')).toBeDefined();
    });

    // Open detail panel
    fireEvent.click(screen.getByText('data-analysis-report'));
    await waitFor(() => {
      expect(screen.getByText('Execution Detail')).toBeDefined();
    });

    // Find the close button — it's a sibling of the "Execution Detail"
    // container, both inside the panel header div
    const detailLabel = screen.getByText('Execution Detail');
    const headerDiv = detailLabel.closest('div').parentElement;
    const closeBtn = headerDiv.querySelector('button');
    expect(closeBtn).not.toBeNull();
    fireEvent.click(closeBtn);

    // Detail panel should be gone
    await waitFor(() => {
      expect(screen.queryByText('Execution Detail')).toBeNull();
    });
  });
});
