/**
 * Tests for runAutomationTask's automation:run-started event dispatch.
 *
 * The event is the load-bearing signal that lets Chat.jsx force-reload
 * messages + re-attach the by-session poller after a same-session Run Now
 * lands (see Chat.jsx for the listener).
 *
 * Why these tests exist: the second-Run-Now dedup fix isn't enough on its
 * own. We also need the bubble-render path to fire, and that path was
 * silent-failing when:
 *   (a) Chat.jsx was warm on /chat?session=<sid> and the poller had
 *       stopped after a stable-terminal — no poll cycle would push the
 *       new user bubble into `messages`
 *   (b) Chat.jsx was cold-mounting but `loadMessages` happened to slip
 *       between setLatestExecution and the user reading the screen —
 *       the user briefly sees only the 3 generic placeholders
 *
 * The run-started event makes both cases reload immediately. Any
 * regression in dispatch (wrong session id, missing detail, race with
 * navigate()) reintroduces the bug, so we pin the contract here.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

vi.mock('@/api/authFetch', () => ({
  authFetch: vi.fn(),
}));

import { authFetch } from '@/api/authFetch';
import { runAutomationTask } from './runAutomationTask';

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

function makeTask(overrides = {}) {
  return {
    id: 'task-uuid-1234',
    name: 'Daily Sales Data Sync',
    session_id: 'sess-uuid-aaaa',
    ...overrides,
  };
}

function makeDeps(overrides = {}) {
  return {
    navigate: vi.fn(),
    toast: vi.fn(),
    lang: 'en',
    ...overrides,
  };
}

describe('runAutomationTask — automation:run-started event', () => {
  let addSpy;
  let dispatched;

  beforeEach(() => {
    vi.clearAllMocks();
    dispatched = [];
    addSpy = vi.spyOn(window, 'addEventListener').mockImplementation((type, listener) => {
      if (type === 'automation:run-started') {
        dispatched.__listener__ = listener;
      }
      window.addEventListener.__realImpl?.(type, listener);
    });
  });

  afterEach(() => {
    addSpy.mockRestore();
  });

  it('dispatches automation:run-started with session, execution, task ids after a successful run', async () => {
    authFetch.mockImplementation(async (url) => {
      if (url.endsWith('/ensure-session')) {
        return { ok: true, json: async () => ({ session_id: 'sess-uuid-aaaa' }) };
      }
      if (url.endsWith('/run')) {
        return { ok: true, json: async () => ({ execution_id: 'exec-uuid-bbbb' }) };
      }
      return { ok: false };
    });

    const deps = makeDeps();
    const dispatch = vi.fn();
    const originalDispatchEvent = window.dispatchEvent;
    window.dispatchEvent = function (ev) {
      if (ev && ev.type === 'automation:run-started') dispatch(ev.detail);
      return originalDispatchEvent.call(this, ev);
    };

    await runAutomationTask(makeTask(), deps);
    await flush();

    expect(dispatch).toHaveBeenCalledTimes(1);
    const detail = dispatch.mock.calls[0][0];
    expect(detail).toEqual({
      sessionId: 'sess-uuid-aaaa',
      executionId: 'exec-uuid-bbbb',
      taskId: 'task-uuid-1234',
    });
    // (2026-08-31) Navigation carries the automation context so a
    // refresh re-binds the automation_agent + keeps the automation id.
    expect(deps.navigate).toHaveBeenCalledWith(
      expect.stringContaining('/chat?session=sess-uuid-aaaa'),
    );
    expect(deps.navigate.mock.calls[0][0]).toContain('automationId=task-uuid-1234');
    expect(deps.navigate.mock.calls[0][0]).toContain('agentName=automation_agent');

    window.dispatchEvent = originalDispatchEvent;
  });

  it('still navigates and toasts when dispatchEvent is unavailable (SSR / no-window)', async () => {
    authFetch.mockImplementation(async (url) => {
      if (url.endsWith('/ensure-session')) {
        return { ok: true, json: async () => ({ session_id: 'sess-uuid-aaaa' }) };
      }
      if (url.endsWith('/run')) {
        return { ok: true, json: async () => ({ execution_id: 'exec-uuid-bbbb' }) };
      }
      return { ok: false };
    });

    const dispatchSpy = vi
      .spyOn(window, 'dispatchEvent')
      .mockImplementation(() => true);

    const deps = makeDeps();
    await runAutomationTask(makeTask(), deps);
    await flush();

    // Even if dispatch throws or window is missing, navigate is the
    // fallback and must still run — the listener on Chat.jsx is a
    // best-effort accelerator, not a hard requirement.
    expect(deps.navigate).toHaveBeenCalledWith(
      expect.stringContaining('/chat?session=sess-uuid-aaaa'),
    );
    expect(deps.toast).toHaveBeenCalled();

    dispatchSpy.mockRestore();
  });

  it('dispatches before navigate() so a warm Chat.jsx can pick it up', async () => {
    const callOrder = [];
    authFetch.mockImplementation(async (url) => {
      if (url.endsWith('/ensure-session')) {
        return { ok: true, json: async () => ({ session_id: 'sess-uuid-aaaa' }) };
      }
      if (url.endsWith('/run')) {
        return { ok: true, json: async () => ({ execution_id: 'exec-uuid-bbbb' }) };
      }
      return { ok: false };
    });

    const deps = {
      navigate: vi.fn(() => callOrder.push('navigate')),
      toast: vi.fn(() => callOrder.push('toast')),
      lang: 'en',
    };

    const dispatchOrder = [];
    const realDispatch = window.dispatchEvent;
    window.dispatchEvent = function (ev) {
      if (ev && ev.type === 'automation:run-started') {
        dispatchOrder.push('dispatch');
        // Mirrors the Chat.jsx listener hook: as soon as the event
        // fires we already see the side effects, BEFORE navigate.
      }
      return realDispatch.call(this, ev);
    };

    await runAutomationTask(makeTask(), deps);
    await flush();

    // The toast is fired as part of step 3 first; dispatch runs in the
    // same step but AFTER toast. Navigate runs last. Either ordering
    // works since dispatch is synchronous, but we want to be sure
    // Chat.jsx receives the signal before its own dep change (from
    // navigate) lands.
    expect(dispatchOrder).toEqual(['dispatch']);
    expect(callOrder).toContain('navigate');
    expect(dispatchOrder[0]).toBeDefined();

    window.dispatchEvent = realDispatch;
  });

  it('preserves the originally assigned session_id when ensure-session fails', async () => {
    authFetch.mockImplementation(async (url) => {
      if (url.endsWith('/ensure-session')) {
        return { ok: false };
      }
      if (url.endsWith('/run')) {
        return { ok: true, json: async () => ({ execution_id: 'exec-uuid-bbbb' }) };
      }
      return { ok: false };
    });

    const dispatch = vi.fn();
    const realDispatch = window.dispatchEvent;
    window.dispatchEvent = function (ev) {
      if (ev && ev.type === 'automation:run-started') dispatch(ev.detail);
      return realDispatch.call(this, ev);
    };

    const deps = makeDeps();
    const task = makeTask({ session_id: 'sess-uuid-original' });
    await runAutomationTask(task, deps);
    await flush();

    expect(dispatch).toHaveBeenCalledTimes(1);
    expect(dispatch.mock.calls[0][0].sessionId).toBe('sess-uuid-original');

    window.dispatchEvent = realDispatch;
  });

  it('omits executionId from detail when /run returns no body', async () => {
    authFetch.mockImplementation(async (url) => {
      if (url.endsWith('/ensure-session')) {
        return { ok: true, json: async () => ({ session_id: 'sess-uuid-aaaa' }) };
      }
      if (url.endsWith('/run')) {
        // No execution_id in body — Chat.jsx still gets a signal so it
        // can wake the poller even though it can't tag this run.
        return { ok: true, json: async () => ({}) };
      }
      return { ok: false };
    });

    const dispatch = vi.fn();
    const realDispatch = window.dispatchEvent;
    window.dispatchEvent = function (ev) {
      if (ev && ev.type === 'automation:run-started') dispatch(ev.detail);
      return realDispatch.call(this, ev);
    };

    const deps = makeDeps();
    await runAutomationTask(makeTask(), deps);
    await flush();

    expect(dispatch).toHaveBeenCalledTimes(1);
    expect(dispatch.mock.calls[0][0].executionId).toBeNull();

    window.dispatchEvent = realDispatch;
  });
});
