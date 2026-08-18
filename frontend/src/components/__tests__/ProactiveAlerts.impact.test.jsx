import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { getAlerts, getAlertDiagnostics, getAlertPreferences } from '@/api/alertApi';
import ProactiveAlerts from '@/components/ProactiveAlerts';

vi.mock('@/api/alertApi', () => ({
  getAlerts: vi.fn(),
  markAlertsRead: vi.fn().mockResolvedValue({}),
  dismissAlert: vi.fn(),
  getAlertDiagnostics: vi.fn(),
  scanAlerts: vi.fn(),
  getAlertPreferences: vi.fn(),
  updateAlertPreferences: vi.fn(),
}));

const makeAlert = (overrides = {}) => ({
  id: 'a1',
  severity: 'high',
  alert_type: 'price_spike',
  product_id: 'c5',
  product_name: 'C5',
  price: 5100,
  change_7d_pct: 8.2,
  message: 'C5 price rose sharply.',
  payload: null,
  ...overrides,
});

describe('ProactiveAlerts smart payload', () => {
  beforeEach(() => {
    getAlerts.mockReset();
    getAlertDiagnostics.mockReset().mockResolvedValue({});
    getAlertPreferences.mockReset().mockResolvedValue({
      channels: ['in_app'],
      min_severity: 'low',
      threshold_multiplier: 1.0,
    });
  });

  it('renders the ¥/day impact chip from payload.impact', async () => {
    getAlerts.mockResolvedValue({
      alerts: [
        makeAlert({
          payload: {
            smart: true,
            impact: { impact_yuan_per_day: 1234.5 },
            ctx: null,
          },
        }),
      ],
    });
    render(<ProactiveAlerts open onClose={vi.fn()} onInvestigate={vi.fn()} />);
    expect(await screen.findByText(/¥1,235\/day/)).toBeInTheDocument();
  });

  it('shows "impact TBD" when the impact is missing', async () => {
    getAlerts.mockResolvedValue({
      alerts: [makeAlert({ payload: { smart: true, impact: null, ctx: null } })],
    });
    render(<ProactiveAlerts open onClose={vi.fn()} onInvestigate={vi.fn()} />);
    expect(await screen.findByText(/impact TBD/i)).toBeInTheDocument();
  });

  it('renders recommendation title and step bullets from payload.ctx', async () => {
    getAlerts.mockResolvedValue({
      alerts: [
        makeAlert({
          payload: {
            smart: true,
            impact: null,
            ctx: {
              why: 'C5 is above its typical weekly volatility.',
              recommendation: {
                title: 'Review C5 pricing now',
                steps: ['Check competitor offers', 'Re-quote next order'],
              },
            },
          },
        }),
      ],
    });
    render(<ProactiveAlerts open onClose={vi.fn()} onInvestigate={vi.fn()} />);

    // "Why this matters" is collapsible — expand it first.
    const whyButton = await screen.findByRole('button', {
      name: /why this matters/i,
    });
    fireEvent.click(whyButton);

    expect(
      await screen.findByText('C5 is above its typical weekly volatility.'),
    ).toBeInTheDocument();
    expect(screen.getByText('Review C5 pricing now')).toBeInTheDocument();
    expect(screen.getByText('Check competitor offers')).toBeInTheDocument();
    expect(screen.getByText('Re-quote next order')).toBeInTheDocument();
  });
});
