/**
 * Tests for the shared runAutomationTask helper.
 *
 * Contract: ensure-session (non-fatal) -> POST /run -> toast the
 * execution id -> navigate to /chat?session=<sid>. The URL must NEVER
 * carry prefill/autorun (the old LLM-dependent deep-link flow that
 * silently no-op'd for custom agents). Failure path toasts destructive
 * and does not navigate.
 *
 * Run with vitest.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('@/api/authFetch', () => ({
  authFetch: vi.fn(),
}));

import { authFetch } from '@/api/authFetch';
import { runAutomationTask } from '../runAutomationTask';

describe('runAutomationTask', () => {
  beforeEach(() => vi.clearAllMocks());

  it('ensures session, fires /run, toasts execution id, navigates to /chat?session=<sid> with automation context', async () => {
    authFetch.mockImplementation(async (url) => {
      if (url.includes('/ensure-session')) {
        return { ok: true, json: async () => ({ success: true, session_id: 'sess-123' }) };
      }
      if (url.endsWith('/run')) {
        return { ok: true, json: async () => ({ success: true, execution_id: 'exe1234567890abcdef' }) };
      }
      throw new Error(`unexpected fetch ${url}`);
    });
    const navigate = vi.fn();
    const toast = vi.fn();
    await runAutomationTask(
      { id: 'task-1', session_id: null, project: 'C5_C9' },
      { navigate, toast, lang: 'en' },
    );

    // ensure-session called first
    expect(authFetch).toHaveBeenCalledWith(
      '/api/automations/task-1/ensure-session',
      { method: 'POST' },
    );
    // /run called
    expect(authFetch).toHaveBeenCalledWith(
      '/api/automations/task-1/run',
      { method: 'POST' },
    );
    // toast with the execution id
    expect(toast).toHaveBeenCalledTimes(1);
    expect(toast.mock.calls[0][0].title).toBe('Execution started');
    expect(toast.mock.calls[0][0].description).toContain('exe12345');
    // navigate to the dedicated session — NO prefill, NO autorun, but
    // WITH the automation context (automationId + agentName + projectName)
    expect(navigate).toHaveBeenCalledTimes(1);
    const target = navigate.mock.calls[0][0];
    expect(target).toContain('/chat?session=sess-123');
    expect(target).toContain('automationId=task-1');
    expect(target).toContain('agentName=automation_agent');
    expect(target).toContain('projectName=C5_C9');
    expect(target).not.toContain('prefill');
    expect(target).not.toContain('autorun');
  });

  it('failure path: toasts destructive and does not navigate', async () => {
    authFetch.mockImplementation(async (url) => {
      if (url.includes('/ensure-session')) {
        return { ok: true, json: async () => ({ session_id: 'sess-x' }) };
      }
      if (url.endsWith('/run')) {
        return { ok: false, status: 500 };
      }
      throw new Error(`unexpected fetch ${url}`);
    });
    const navigate = vi.fn();
    const toast = vi.fn();
    await runAutomationTask(
      { id: 'task-2' },
      { navigate, toast, lang: 'en' },
    );

    expect(toast).toHaveBeenCalledTimes(1);
    expect(toast.mock.calls[0][0].variant).toBe('destructive');
    expect(navigate).not.toHaveBeenCalled();
  });

  it('ensure-session failure is non-fatal: still fires /run and navigates with the task session_id', async () => {
    authFetch.mockImplementation(async (url) => {
      if (url.includes('/ensure-session')) {
        throw new Error('network down');
      }
      if (url.endsWith('/run')) {
        return { ok: true, json: async () => ({ execution_id: 'exe999' }) };
      }
      throw new Error(`unexpected fetch ${url}`);
    });
    const navigate = vi.fn();
    const toast = vi.fn();
    await runAutomationTask(
      { id: 'task-3', session_id: 'fallback-sid' },
      { navigate, toast, lang: 'en' },
    );

    expect(authFetch).toHaveBeenCalledWith(
      '/api/automations/task-3/run',
      { method: 'POST' },
    );
    expect(toast).toHaveBeenCalledTimes(1);
    expect(navigate).toHaveBeenCalledWith(
      expect.stringContaining('/chat?session=fallback-sid'),
    );
  });
});
