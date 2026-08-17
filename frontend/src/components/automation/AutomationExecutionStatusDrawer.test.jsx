import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const navigateMock = vi.fn();
vi.mock('react-router-dom', () => ({
  useNavigate: () => navigateMock,
}));
vi.mock('@/lib/LanguageProvider', () => ({
  useLanguage: () => ({
    lang: 'en',
    t: {
      automationStatus: {
        recentRuns: 'Recent runs',
        openChat: 'Open chat',
        stop: 'Stop',
        close: 'Close',
        queued: 'Queued',
        status: { running: 'Running', completed: 'Completed', failed: 'Failed' },
      },
    },
  }),
}));
vi.mock('@/components/ui/use-toast', () => ({ toast: vi.fn() }));
const authFetchMock = vi.fn();
vi.mock('@/api/authFetch', () => ({ authFetch: (...a) => authFetchMock(...a) }));

const entriesMock = vi.fn();
vi.mock('@/lib/useAutomationExecutionStatus', () => ({
  useAutomationExecutionStatus: () => entriesMock(),
}));

const { default: AutomationExecutionStatusDrawer } = await import(
  './AutomationExecutionStatusDrawer'
);

describe('AutomationExecutionStatusDrawer', () => {
  beforeEach(() => {
    navigateMock.mockClear();
    authFetchMock.mockReset();
    entriesMock.mockReset();
  });

  it('renders nothing when there are no executions', () => {
    entriesMock.mockReturnValue([]);
    const { container } = render(<AutomationExecutionStatusDrawer />);
    expect(container.firstChild).toBeNull();
  });

  it('renders an active execution with steps and skill calls', () => {
    entriesMock.mockReturnValue([
      {
        executionId: 'e1',
        taskName: 'Daily sync',
        sessionId: 's1',
        status: 'running',
        elapsed_sec: 5,
        steps: [{ no: 1, text: 'Loading schema' }, { no: 2, text: 'Running' }],
        skill_calls: [{ skill_name: 'weekly_report', action: 'load', status: 'completed' }],
      },
    ]);
    render(<AutomationExecutionStatusDrawer />);
    expect(screen.getByText('Daily sync')).toBeTruthy();
    expect(screen.getByText('Loading schema')).toBeTruthy();
    expect(screen.getByText('Load weekly_report')).toBeTruthy();
  });

  it('Stop calls the cancel endpoint', async () => {
    entriesMock.mockReturnValue([
      { executionId: 'e1', taskName: 'T', sessionId: 's1', status: 'running', steps: [], skill_calls: [] },
    ]);
    render(<AutomationExecutionStatusDrawer />);
    authFetchMock.mockResolvedValue({ ok: true });
    fireEvent.click(screen.getByTitle('Stop'));
    await waitFor(() =>
      expect(authFetchMock).toHaveBeenCalledWith(
        '/api/automations/executions/e1/cancel',
        { method: 'POST' },
      ),
    );
  });

  it('Open chat navigates to /chat with session + execution', () => {
    entriesMock.mockReturnValue([
      { executionId: 'e1', taskName: 'T', sessionId: 'sess-9', status: 'running', steps: [], skill_calls: [] },
    ]);
    render(<AutomationExecutionStatusDrawer />);
    fireEvent.click(screen.getByTitle('Open chat'));
    expect(navigateMock).toHaveBeenCalledWith('/chat?session=sess-9&execution=e1');
  });

  it('shows a recent-runs pill when only terminal executions exist', () => {
    entriesMock.mockReturnValue([
      { executionId: 'e1', taskName: 'T', sessionId: 's1', status: 'completed', elapsed_sec: 3 },
    ]);
    render(<AutomationExecutionStatusDrawer />);
    expect(screen.getByText('Recent runs')).toBeTruthy();
  });
});
