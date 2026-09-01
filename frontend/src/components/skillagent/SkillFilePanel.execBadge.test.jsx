/**
 * UI automation test: SkillFilePanel execution evidence badge.
 *
 * Verifies that the file panel correctly fetches and displays execution
 * statistics (run count, success rate, failed count, last action) when
 * a skill is selected, and hides the badge when no executions exist.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

// Mock the API client
vi.mock('@/api/skillStudio', () => ({
  listSkillExecutions: vi.fn(),
}));

// Mock react-markdown to avoid rendering complexity
vi.mock('react-markdown', () => ({
  default: ({ children }) => <div data-testid="markdown">{children}</div>,
}));

// Mock the language provider
vi.mock('@/lib/LanguageProvider', () => ({
  useLanguage: () => ({
    t: {
      skillAgent: {
        files: 'Files',
        searchFiles: 'Search files...',
        noFiles: 'No files',
        noFileSelected: 'No file selected',
        download: 'Download',
      },
    },
  }),
}));

import SkillFilePanel from '@/components/skillagent/SkillFilePanel';
import { listSkillExecutions } from '@/api/skillStudio';

const SAMPLE_SKILL = {
  name: 'data-analysis-report',
  description: 'Create comprehensive data analysis reports',
  skill_md: '## Overview\n\nThis is a test skill.\n\n## Steps\n\n1. Do something',
  category: 'custom',
  source: 'user',
  kind: 'system_skill',
  status: 'active',
  version: '1.0.0',
};

const SAMPLE_EXECUTIONS = [
  { id: 'e1', skill_name: 'data-analysis-report', action: 'execute', status: 'completed', duration_ms: 1200 },
  { id: 'e2', skill_name: 'data-analysis-report', action: 'load', status: 'completed', duration_ms: 30, skill_id: 'tool-123', skill_version: '1.0.0', body_length: 2048 },
  { id: 'e3', skill_name: 'data-analysis-report', action: 'load', status: 'failed', duration_ms: 50, error_message: 'timeout' },
];

describe('SkillFilePanel — execution evidence badge', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('displays execution badge with correct stats when executions exist', async () => {
    listSkillExecutions.mockResolvedValueOnce({
      success: true,
      total: 3,
      count: 3,
      executions: SAMPLE_EXECUTIONS,
    });

    render(<SkillFilePanel skill={SAMPLE_SKILL} messages={[]} onClose={vi.fn()} />);

    // Wait for the badge to load
    await waitFor(() => {
      expect(screen.getByText('3 runs')).toBeDefined();
    });

    // Success rate: 2/3 = 67%
    expect(screen.getByText('67% success')).toBeDefined();
    // Failed count
    expect(screen.getByText('1 failed')).toBeDefined();
    expect(screen.getByText('avg load 40ms')).toBeDefined();
    expect(screen.getByText('v1.0.0')).toBeDefined();
    // Last execution action (execs[0] = first in array = most recent = 'execute')
    expect(screen.getByText(/execute/)).toBeDefined();
  });

  it('hides the execution badge when no executions exist', async () => {
    listSkillExecutions.mockResolvedValueOnce({
      success: true,
      total: 0,
      count: 0,
      executions: [],
    });

    render(<SkillFilePanel skill={SAMPLE_SKILL} messages={[]} onClose={vi.fn()} />);

    // Wait a moment for the async fetch to complete
    await waitFor(() => {
      expect(listSkillExecutions).toHaveBeenCalledWith('data-analysis-report', { limit: 100 });
    });

    // The badge should NOT be rendered (no "runs" text)
    expect(screen.queryByText(/runs/)).toBeNull();
  });

  it('shows 100% success rate when all executions completed', async () => {
    listSkillExecutions.mockResolvedValueOnce({
      success: true,
      total: 2,
      count: 2,
      executions: [
        { id: 'e1', skill_name: 'data-analysis-report', action: 'execute', status: 'completed', duration_ms: 100 },
        { id: 'e2', skill_name: 'data-analysis-report', action: 'load', status: 'completed', duration_ms: 50 },
      ],
    });

    render(<SkillFilePanel skill={SAMPLE_SKILL} messages={[]} onClose={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText('2 runs')).toBeDefined();
    });

    expect(screen.getByText('100% success')).toBeDefined();
    // No failed badge when 0 failures
    expect(screen.queryByText(/failed/)).toBeNull();
  });

  it('gracefully handles API failure by hiding the badge', async () => {
    listSkillExecutions.mockRejectedValueOnce(new Error('Network error'));

    render(<SkillFilePanel skill={SAMPLE_SKILL} messages={[]} onClose={vi.fn()} />);

    // Wait for the rejected promise to settle
    await waitFor(() => {
      expect(listSkillExecutions).toHaveBeenCalled();
    });

    // Badge should not appear on error
    expect(screen.queryByText(/runs/)).toBeNull();
  });

  it('does not fetch executions when no skill is provided', async () => {
    render(<SkillFilePanel skill={null} messages={[]} onClose={vi.fn()} />);

    // Give it a moment to ensure no fetch happens
    await new Promise((r) => setTimeout(r, 50));

    expect(listSkillExecutions).not.toHaveBeenCalled();
  });

  it('displays the SKILL.md file content from the skill object', async () => {
    listSkillExecutions.mockResolvedValueOnce({
      success: true,
      total: 0,
      count: 0,
      executions: [],
    });

    render(<SkillFilePanel skill={SAMPLE_SKILL} messages={[]} onClose={vi.fn()} />);

    // The SKILL.md file should be in the tree
    await waitFor(() => {
      expect(screen.getByText('SKILL.md')).toBeDefined();
    });
  });

  it('shows the skill name as the root folder in the file tree', async () => {
    listSkillExecutions.mockResolvedValueOnce({
      success: true,
      total: 0,
      count: 0,
      executions: [],
    });

    render(<SkillFilePanel skill={SAMPLE_SKILL} messages={[]} onClose={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText('data-analysis-report')).toBeDefined();
    });
  });
});
