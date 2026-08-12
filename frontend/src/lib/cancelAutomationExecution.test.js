import { describe, it, expect, vi } from 'vitest';
import { cancelAutomationExecution } from './cancelAutomationExecution';

describe('cancelAutomationExecution', () => {
  it('rejects missing execution id without calling authFetch', async () => {
    const authFetch = vi.fn();
    const r = await cancelAutomationExecution('', { authFetch });
    expect(r.ok).toBe(false);
    expect(r.error).toMatch(/missing/);
    expect(authFetch).not.toHaveBeenCalled();
  });

  it('rejects missing authFetch without throwing', async () => {
    const r = await cancelAutomationExecution('exe-1', {});
    expect(r.ok).toBe(false);
    expect(r.error).toMatch(/authFetch/);
  });

  it('returns ok=true with status when backend reports cancelled', async () => {
    const authFetch = vi.fn().mockResolvedValue({
      status: 'cancelled',
      already_terminal: false,
      delivered: true,
    });
    const r = await cancelAutomationExecution('exe-1', { authFetch });
    expect(authFetch).toHaveBeenCalledWith(
      '/api/automations/executions/exe-1/cancel',
      { method: 'POST' },
    );
    expect(r).toEqual({
      ok: true,
      status: 'cancelled',
      alreadyTerminal: false,
      delivered: true,
    });
  });

  it('encodes the execution id', async () => {
    const authFetch = vi.fn().mockResolvedValue({ status: 'cancelled' });
    await cancelAutomationExecution('exe with spaces', { authFetch });
    expect(authFetch).toHaveBeenCalledWith(
      '/api/automations/executions/exe%20with%20spaces/cancel',
      { method: 'POST' },
    );
  });

  it('returns ok=true with alreadyTerminal=true on second cancel', async () => {
    const authFetch = vi.fn().mockResolvedValue({
      status: 'cancelled',
      already_terminal: true,
      delivered: false,
    });
    const r = await cancelAutomationExecution('exe-2', { authFetch });
    expect(r.ok).toBe(true);
    expect(r.alreadyTerminal).toBe(true);
  });

  it('catches authFetch errors and returns ok=false', async () => {
    const authFetch = vi.fn().mockRejectedValue(new Error('network down'));
    const r = await cancelAutomationExecution('exe-3', { authFetch });
    expect(r.ok).toBe(false);
    expect(r.error).toMatch(/network down/);
  });

  it('treats null response as a soft success with default status', async () => {
    const authFetch = vi.fn().mockResolvedValue(null);
    const r = await cancelAutomationExecution('exe-4', { authFetch });
    expect(r.ok).toBe(true);
    expect(r.status).toBe('cancelled');
    expect(r.alreadyTerminal).toBe(false);
  });
});
