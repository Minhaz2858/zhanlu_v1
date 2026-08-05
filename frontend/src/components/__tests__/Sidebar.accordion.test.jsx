import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('@/api/base44Client', () => ({
  base44: {
    auth: { me: vi.fn().mockResolvedValue(null) },
    get: vi.fn(),
  },
}));

const { mockEnabled, mockSnapshot, mockHistory } = vi.hoisted(() => ({
  mockEnabled: vi.fn(),
  mockSnapshot: vi.fn().mockResolvedValue([]),
  mockHistory: vi.fn().mockResolvedValue([]),
}));

vi.mock('@/api/marketDashboard', () => ({
  marketDashboardApi: {
    enabled: mockEnabled,
    snapshot: mockSnapshot,
    history: mockHistory,
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
    t: {
      sidebar: {
        automation: 'Automation',
        mySpace: 'My Space',
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

function renderSidebar(initialPath = '/') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initialPath]}>
        <Sidebar />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('Sidebar DashboardAccordion', () => {
  beforeEach(() => {
    mockEnabled.mockReset();
    // Default: EDIA is enabled — most tests in this file assume the happy path.
    mockEnabled.mockResolvedValue({ enabled: true, reachable: true, reason: 'ok' });
  });

  it('renders the Dashboard group header when EDIA is enabled', async () => {
    renderSidebar();
    expect(await screen.findByText(/dashboard/i)).toBeInTheDocument();
  });

  it('auto-expands and shows sub-items on /market-dashboard/* routes', async () => {
    renderSidebar('/market-dashboard/upstream');
    // Wait for the accordion to render + auto-expand (sub-item 'Overview' appears)
    expect(await screen.findByText('Overview')).toBeInTheDocument();
    expect(screen.getByText('Upstream')).toBeInTheDocument();
    expect(screen.getByText('Midstream')).toBeInTheDocument();
    expect(screen.getByText('Downstream')).toBeInTheDocument();
    expect(screen.getByText('Report')).toBeInTheDocument();
  });

  // Discoverability is a feature: a user who lands on a server where
  // EDIA_MYSQL_URL is not set must still be able to FIND the dashboard
  // from the sidebar. The page itself shows a friendly error when the
  // data source is unavailable, so we don't need to hide the nav.
  it('renders the Dashboard group header even when EDIA is not enabled', async () => {
    mockEnabled.mockResolvedValue({ enabled: false, reachable: false, reason: 'unconfigured' });
    renderSidebar();
    // Header must appear regardless of EDIA state.
    expect(await screen.findByText(/dashboard/i)).toBeInTheDocument();
  });

  it('expands the Dashboard accordion on click even when EDIA is not enabled', async () => {
    mockEnabled.mockResolvedValue({ enabled: false, reachable: false, reason: 'unconfigured' });
    renderSidebar();
    const header = await screen.findByText(/dashboard/i);
    // The header is a button; click to expand
    fireEvent.click(header);
    // All five sub-items become reachable
    expect(screen.getByText('Overview')).toBeInTheDocument();
    expect(screen.getByText('Upstream')).toBeInTheDocument();
    expect(screen.getByText('Midstream')).toBeInTheDocument();
    expect(screen.getByText('Downstream')).toBeInTheDocument();
    expect(screen.getByText('Report')).toBeInTheDocument();
  });
});
