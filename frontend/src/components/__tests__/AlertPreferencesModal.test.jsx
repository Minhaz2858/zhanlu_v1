import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {
  getAlertPreferences,
  updateAlertPreferences,
} from '@/api/alertApi';
import AlertPreferencesModal from '@/components/AlertPreferencesModal';

vi.mock('@/api/alertApi', () => ({
  getAlertPreferences: vi.fn(),
  updateAlertPreferences: vi.fn(),
  muteProduct: vi.fn(),
}));

const defaultPrefs = {
  user_id: 'u1',
  min_severity: 'low',
  muted_products: ['c5'],
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

  it('mute list supports adding and removing products', async () => {
    renderModal();
    const input = await screen.findByPlaceholderText(/add product to mute/i);
    await userEvent.type(input, 'c9');
    fireEvent.click(screen.getByRole('button', { name: /add mute/i }));

    expect(screen.getByText('c9')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /remove c9/i }));
    expect(screen.queryByText('c9')).not.toBeInTheDocument();
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
