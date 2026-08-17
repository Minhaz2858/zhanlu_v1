import { describe, it, expect, vi, beforeEach } from 'vitest';
import { authFetch } from '@/api/authFetch';
import {
  getAlertPreferences,
  updateAlertPreferences,
  muteProduct,
} from '@/api/alertApi';

vi.mock('@/api/authFetch', () => ({ authFetch: vi.fn() }));

describe('alertApi preferences', () => {
  beforeEach(() => {
    authFetch.mockReset();
  });

  it('getAlertPreferences returns the snapshot from GET', async () => {
    const snapshot = {
      min_severity: 'medium',
      muted_products: ['c5'],
      channels: ['in_app', 'webhook'],
      threshold_multiplier: 1.2,
      quiet_hours: null,
      recommendations_enabled: true,
      webhook_url: null,
      webhook_secret: null,
    };
    authFetch.mockResolvedValue({
      ok: true,
      json: async () => snapshot,
    });

    const result = await getAlertPreferences();
    expect(result).toEqual(snapshot);
    expect(authFetch).toHaveBeenCalledWith('/api/users/me/alert-preferences');
  });

  it('updateAlertPreferences PATCHes the payload', async () => {
    authFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ min_severity: 'high', muted_products: [] }),
    });

    await updateAlertPreferences({ min_severity: 'high' });
    expect(authFetch).toHaveBeenCalledWith(
      '/api/users/me/alert-preferences',
      expect.objectContaining({
        method: 'PATCH',
      }),
    );
    const [, init] = authFetch.mock.calls[0];
    expect(JSON.parse(init.body)).toEqual({ min_severity: 'high' });
  });

  it('muteProduct appends the product to the current muted list', async () => {
    const updated = { muted_products: ['a', 'c5'] };
    authFetch.mockResolvedValue({ ok: true, json: async () => updated });

    const result = await muteProduct('c5', ['a']);
    expect(result).toEqual(updated);
    const [, init] = authFetch.mock.calls[0];
    expect(JSON.parse(init.body)).toEqual({ muted_products: ['a', 'c5'] });
  });
});
