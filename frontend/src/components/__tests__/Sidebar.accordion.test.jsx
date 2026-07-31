import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('@/api/base44Client', () => ({
  base44: {
    auth: { me: vi.fn().mockResolvedValue(null) },
    get: vi.fn(),
  },
}));

vi.mock('@/api/marketDashboard', () => ({
  marketDashboardApi: {
    enabled: vi.fn().mockResolvedValue({ enabled: true, reachable: true, reason: 'ok' }),
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
  });
});
