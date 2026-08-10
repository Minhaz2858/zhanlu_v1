/**
 * Regression (2026-08-06): clicking the "Dashboard" link in the
 * Business scope navigated to ``/market-dashboard/overview`` — but
 * the sidebar's auto-switch effect at Sidebar.jsx:70-77 used a
 * broad ``path.startsWith('/market')`` check, which matched
 * ``/market-dashboard/*`` as well. Result: the sidebar silently
 * flipped from the Business tab to the Admin tab, the items list
 * changed (Market / Toolkit / Admin instead of Automation / My Space
 * / My Files), and the user felt the sidebar "moved on its own."
 *
 * The actual code intent (see the comment at Sidebar.jsx:64-69)
 * matches what the user expects: ``/market-dashboard/*`` is part of
 * the BUSINESS scope (it sits under the Dashboard accordion in the
 * Business tab). The bug is that the route predicate is too loose.
 *
 * This suite pins the auto-switch rules:
 *   - /admin/*            → admin
 *   - /toolkit            → admin
 *   - /market             → admin (the platform-config area — distinct
 *                           from /market-dashboard, despite sharing a
 *                           path prefix)
 *   - /market-dashboard/* → business (EDIA mirror, lives under the
 *                           Dashboard accordion in the Business tab)
 *   - everything else     → business
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import React from 'react';

vi.mock('@/api/base44Client', () => ({
  base44: {
    auth: { me: vi.fn().mockResolvedValue(null) },
    get: vi.fn(),
  },
}));

const { mockEnabled } = vi.hoisted(() => ({
  mockEnabled: vi.fn(),
}));

vi.mock('@/api/marketDashboard', () => ({
  marketDashboardApi: {
    enabled: mockEnabled,
    snapshot: vi.fn().mockResolvedValue([]),
    history: vi.fn().mockResolvedValue([]),
  },
}));

vi.mock('@/components/chat/SessionList', () => ({ default: () => null }));
vi.mock('@/components/UserMenu', () => ({ default: () => null }));
vi.mock('@/components/ThemeToggle', () => ({ default: () => null }));
vi.mock('@/lib/ChatSessionContext', () => ({
  useChatSession: () => ({ newChat: vi.fn() }),
}));
vi.mock('@/lib/LanguageProvider', () => ({
  useLanguage: () => ({
    lang: 'en',
    t: {
      sidebar: {
        automation: 'Automation',
        mySpace: 'My Space',
        myFiles: 'My Files',
        market: 'Market',
        toolkit: 'Toolkit',
        brand: 'Zhanlu',
        collapse: 'Collapse',
        expand: 'Expand',
      },
      sessionList: { newTask: 'New Task' },
    },
  }),
}));
vi.mock('@/hooks/useScreenSize', () => ({
  useScreenSize: () => ({ isMobile: false, settings: { sidebarWidth: 240 } }),
}));

import Sidebar from '../Sidebar';

function renderSidebar(initialPath) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initialPath]}>
        <Sidebar />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function readTabs() {
  // English UI: "Business" and "Admin". If the language ever flips to
  // Chinese the labels become "业务" / "配置" — but our mock pins
  // lang='en' so the English labels are stable here.
  const business = screen.getByRole('tab', { name: /business/i });
  const admin = screen.getByRole('tab', { name: /^admin$/i });
  return { business, admin };
}

describe('Sidebar navTab auto-switch', () => {
  beforeEach(() => {
    mockEnabled.mockReset();
    mockEnabled.mockResolvedValue({ enabled: true, reachable: true, reason: 'ok' });
  });

  it('keeps the Business tab on /market-dashboard/overview (was incorrectly switching to Admin)', () => {
    // BUG REGRESSION: the previous ``path.startsWith('/market')``
    // predicate matched ``/market-dashboard/overview`` because the
    // path STARTS WITH the string ``/market``. The sidebar then
    // flipped to the Admin tab, which the user reported as the
    // sidebar "switching to Admin automatically". The fix narrows
    // the predicate so only the bare ``/market`` route (the
    // platform-config area) and ``/market/<...>`` sub-routes
    // switch to Admin.
    renderSidebar('/market-dashboard/overview');
    const { business, admin } = readTabs();
    expect(business.getAttribute('aria-selected'), 'Business tab must be active on /market-dashboard/overview').toBe('true');
    expect(admin.getAttribute('aria-selected'), 'Admin tab must NOT be active on /market-dashboard/overview').toBe('false');
  });

  it('keeps the Business tab on /market-dashboard/upstream', () => {
    renderSidebar('/market-dashboard/upstream');
    const { business, admin } = readTabs();
    expect(business.getAttribute('aria-selected')).toBe('true');
    expect(admin.getAttribute('aria-selected')).toBe('false');
  });

  it('keeps the Business tab on /market-dashboard/midstream', () => {
    renderSidebar('/market-dashboard/midstream');
    const { business, admin } = readTabs();
    expect(business.getAttribute('aria-selected')).toBe('true');
    expect(admin.getAttribute('aria-selected')).toBe('false');
  });

  it('keeps the Business tab on /market-dashboard/downstream', () => {
    renderSidebar('/market-dashboard/downstream');
    const { business, admin } = readTabs();
    expect(business.getAttribute('aria-selected')).toBe('true');
    expect(admin.getAttribute('aria-selected')).toBe('false');
  });

  it('keeps the Business tab on /market-dashboard/report', () => {
    renderSidebar('/market-dashboard/report');
    const { business, admin } = readTabs();
    expect(business.getAttribute('aria-selected')).toBe('true');
    expect(admin.getAttribute('aria-selected')).toBe('false');
  });

  it('still switches to Admin on /market (the platform-config area)', () => {
    // We did not over-narrow the predicate: ``/market`` is the
    // platform-config area (browse/subscribe/deploy) and MUST
    // still auto-switch to Admin. This guard test catches a
    // regression where someone "fixes" the bug by removing the
    // entire /market rule.
    renderSidebar('/market');
    const { business, admin } = readTabs();
    expect(business.getAttribute('aria-selected')).toBe('false');
    expect(admin.getAttribute('aria-selected')).toBe('true');
  });

  it('still switches to Admin on /toolkit', () => {
    renderSidebar('/toolkit');
    const { business, admin } = readTabs();
    expect(business.getAttribute('aria-selected')).toBe('false');
    expect(admin.getAttribute('aria-selected')).toBe('true');
  });

  it('still switches to Admin on /admin/users', () => {
    renderSidebar('/admin/users');
    const { business, admin } = readTabs();
    expect(business.getAttribute('aria-selected')).toBe('false');
    expect(admin.getAttribute('aria-selected')).toBe('true');
  });

  it('keeps the Business tab on /automation', () => {
    renderSidebar('/automation');
    const { business, admin } = readTabs();
    expect(business.getAttribute('aria-selected')).toBe('true');
    expect(admin.getAttribute('aria-selected')).toBe('false');
  });

  it('keeps the Business tab on /', () => {
    renderSidebar('/');
    const { business, admin } = readTabs();
    expect(business.getAttribute('aria-selected')).toBe('true');
    expect(admin.getAttribute('aria-selected')).toBe('false');
  });
});
