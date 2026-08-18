import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import {
  getAlertPreferences,
  updateAlertPreferences,
} from '@/api/alertApi';
import AlertPreferencesModal from '@/components/AlertPreferencesModal';

vi.mock('@/api/alertApi', () => ({
  getAlertPreferences: vi.fn(),
  updateAlertPreferences: vi.fn(),
}));

const defaultPrefs = {
  user_id: 'u1',
  min_severity: 'low',
  channels: ['in_app'],
  threshold_multiplier: 1.0,
  quiet_hours: { enabled: false, start: '22:00', end: '07:00' },
  recommendations_enabled: true,
  webhook_url: null,
  webhook_secret: null,
};

function renderModal(props = {}) {
  return render(
    <AlertPreferencesModal
      open
      onClose={vi.fn()}
      onSaved={vi.fn()}
      {...props}
    />,
  );
}

describe('AlertPreferencesModal', () => {
  beforeEach(() => {
    getAlertPreferences.mockReset();
    getAlertPreferences.mockResolvedValue(defaultPrefs);
    updateAlertPreferences.mockReset();
    updateAlertPreferences.mockResolvedValue(defaultPrefs);
  });

  it('sensitivity slider updates the displayed multiplier', async () => {
    renderModal();
    const slider = await screen.findByLabelText(/sensitivity/i);
    expect(screen.getByText('1.0x')).toBeInTheDocument();

    fireEvent.change(slider, { target: { value: '1.5' } });
    expect(screen.getByText('1.5x')).toBeInTheDocument();
  });

  it('save calls updateAlertPreferences with the edited payload', async () => {
    renderModal();
    const slider = await screen.findByLabelText(/sensitivity/i);
    fireEvent.change(slider, { target: { value: '1.5' } });

    fireEvent.click(screen.getByRole('button', { name: /save preferences/i }));

    await waitFor(() =>
      expect(updateAlertPreferences).toHaveBeenCalledWith(
        expect.objectContaining({ threshold_multiplier: 1.5 }),
      ),
    );
  });
});
