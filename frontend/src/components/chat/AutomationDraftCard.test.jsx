// AutomationDraftCard — server-draft mode (lifecycle separation, 2026-11-03).
//
// Verifies the ONLY activation path: an explicit Confirm & Activate that
// calls the authoritative confirm API (If-Match + activate=true), a Discard
// that calls the discard API, server validation rendering, and that the
// legacy (no-draftId) mode renders no action bar.
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent, cleanup } from '@testing-library/react';

vi.mock('@/api/authFetch', () => ({
  authFetch: vi.fn(),
}));

vi.mock('@/components/automation/SchedulePicker', () => ({
  default: () => <div data-testid="schedule-picker" />,
}));

import AutomationDraftCard from './AutomationDraftCard';
import { authFetch } from '@/api/authFetch';

const DRAFT = {
  draft_id: 'draft-1',
  version: 1,
  status: 'draft',
  validation: {
    ok: true,
    errors: [],
    warnings: [{
      field: 'datasource',
      code: 'datasource_not_configured',
      message: 'No datasource is bound to this automation.',
    }],
  },
};

function jsonResponse(body, status = 200) {
  return { ok: status < 400, status, json: () => Promise.resolve(body) };
}

beforeEach(() => {
  authFetch.mockReset();
  authFetch.mockImplementation(async (url) => {
    if (url.includes('/api/automations/drafts/draft-1') && !url.includes('confirm') && !url.includes('discard')) {
      return jsonResponse(DRAFT);
    }
    return jsonResponse({});
  });
});

afterEach(() => cleanup());
import { afterEach } from 'vitest';

describe('AutomationDraftCard — server-draft mode', () => {
  it('fetches the server draft and renders validation warnings', async () => {
    render(<AutomationDraftCard prefill="" lang="en" draftId="draft-1" />);
    await waitFor(() => expect(authFetch).toHaveBeenCalledWith(
      '/api/automations/drafts/draft-1'));
    expect(await screen.findByText(/No datasource is bound/)).toBeTruthy();
  });

  it('Confirm & Activate calls the confirm API with If-Match + activate and fires onConfirmed', async () => {
    const onConfirmed = vi.fn();
    render(<AutomationDraftCard prefill="" lang="en" draftId="draft-1"
      onConfirmed={onConfirmed} />);
    const btn = await screen.findByText('Confirm & Activate');
    authFetch.mockImplementation(async (url, opts = {}) => {
      if (url.includes('/confirm')) {
        expect(url).toContain('If-Match=1');
        expect(JSON.parse(opts.body)).toEqual({
          confirmation_key: expect.stringMatching(/^ck-/),
          activate: true,
        });
        return jsonResponse({ definition_id: 'task-1' }, 201);
      }
      return jsonResponse(DRAFT);
    });
    fireEvent.click(btn);
    await waitFor(() => expect(onConfirmed).toHaveBeenCalledTimes(1));
  });

  it('Discard calls the discard API and dismisses', async () => {
    const onDismiss = vi.fn();
    render(<AutomationDraftCard prefill="" lang="en" draftId="draft-1"
      onDismiss={onDismiss} />);
    const btn = await screen.findByText('Discard');
    authFetch.mockImplementation(async (url) => {
      if (url.includes('/discard')) return jsonResponse({}, 200);
      return jsonResponse(DRAFT);
    });
    fireEvent.click(btn);
    await waitFor(() => expect(onDismiss).toHaveBeenCalledTimes(1));
    expect(authFetch).toHaveBeenCalledWith(
      '/api/automations/drafts/draft-1/discard', { method: 'POST' });
  });

  it('legacy mode (no draftId) renders NO activation bar', () => {
    render(<AutomationDraftCard prefill="" lang="en" />);
    expect(screen.queryByText('Confirm & Activate')).toBeNull();
    expect(screen.queryByText('Discard')).toBeNull();
    expect(authFetch).not.toHaveBeenCalled();
  });

  it('a failed confirm surfaces the error and does NOT fire onConfirmed', async () => {
    const onConfirmed = vi.fn();
    render(<AutomationDraftCard prefill="" lang="en" draftId="draft-1"
      onConfirmed={onConfirmed} />);
    const btn = await screen.findByText('Confirm & Activate');
    authFetch.mockImplementation(async (url) => {
      if (url.includes('/confirm')) {
        return jsonResponse({ detail: { code: 'stale_version' } }, 409);
      }
      return jsonResponse(DRAFT);
    });
    fireEvent.click(btn);
    await waitFor(() => expect(screen.getByText(/stale_version/)).toBeTruthy());
    expect(onConfirmed).not.toHaveBeenCalled();
  });
});
