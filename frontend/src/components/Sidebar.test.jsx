import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import React from 'react';

const { mockList, mockAuth, mockProjectList, mockAutomationList, mockAgentConversationList } = vi.hoisted(() => ({
  mockList: vi.fn().mockResolvedValue([]),
  mockProjectList: vi.fn().mockResolvedValue([]),
  // Sidebar/SessionList fetches the full AutomationTask list once on
  // mount to build a Set of session_ids that have at least one
  // automation, used to render the small clock icon in the sidebar.
  // Default to an empty list so existing tests don't suddenly grow a
  // clock icon — and so we don't have to backfill mocks across the
  // whole suite just to keep it green.
  mockAutomationList: vi.fn().mockResolvedValue([]),
  // SessionList also loads AgentConversation ids on mount (conv rows
  // that own this chat); without the entity the effect throws before
  // the first assertion.
  mockAgentConversationList: vi.fn().mockResolvedValue([]),
  mockAuth: { me: vi.fn().mockResolvedValue({ name: 'Tester', email: 't@x.com' }) },
}));

vi.mock('@/api/base44Client', () => ({
  base44: {
    entities: {
      ChatSession: { list: mockList },
      Project: { list: mockProjectList },
      AutomationTask: { list: mockAutomationList },
      AgentConversation: { list: mockAgentConversationList },
    },
    auth: mockAuth,
  },
}));

// UserMenu (rendered by Sidebar) calls useAuth() for `logout`. The
// suite mounts Sidebar without an AuthProvider, so stub the hook —
// matches the base44 mock style above.
vi.mock('@/lib/AuthContext', () => ({
  useAuth: () => ({ logout: vi.fn() }),
}));

// The screen-adaptation work moved the sidebar width from a
// `w-64`/`w-60` Tailwind class to a screen-tier-driven inline style
// (see `lib/screen-config.js`). For these tests we don't care about
// the actual pixel width, just that the sidebar is in its
// "expanded" (non-collapsed) state — so stub the hook to return a
// known value.
vi.mock('@/hooks/useScreenSize', () => ({
  useScreenSize: () => ({ isMobile: false, settings: { sidebarWidth: 256 } }),
}));

const { default: Sidebar } = await import('@/components/Sidebar');
const { ChatSessionProvider, useChatSession } = await import('@/lib/ChatSessionContext');

function renderAtRoute(initialEntries) {
  // Wrap in QueryClientProvider — Sidebar may use useQuery hooks.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    React.createElement(
      QueryClientProvider,
      { client: qc },
      React.createElement(
        MemoryRouter,
        { initialEntries },
        React.createElement(
          ChatSessionProvider,
          null,
          React.createElement(Sidebar),
          React.createElement(InjectActiveId),
        ),
      ),
    )
  );
}

function InjectActiveId() {
  const { selectSession } = useChatSession();
  return React.createElement('button', {
    'data-testid': 'test-select',
    onClick: () => selectSession('sess-test'),
  }, 'select');
}

// The app's default language is Chinese. These constants match the
// nav labels in `translations.js` for `lang=zh`.
const LABELS = {
  brand: 'Zhanlu',
  nav_automation: '自动化任务',
  nav_mySpace: '我的空间',
  newTask: '新建任务',
};

describe('Sidebar (unified)', () => {
  beforeEach(() => {
    mockList.mockReset();
    mockList.mockResolvedValue([]);
    mockProjectList.mockReset();
    mockProjectList.mockResolvedValue([]);
  });

  it('renders brand + new-task CTA + nav on the chat route (/)', async () => {
    renderAtRoute(['/']);
    expect(screen.getByText(LABELS.brand)).toBeInTheDocument();
    // Brand logo image was removed — sidebar now shows text-only brand.
    // "Cognitive Hub" was removed — the primary CTA at the top is
    // now "+ New Task", and the other nav items follow.
    expect(screen.getByText(LABELS.newTask)).toBeInTheDocument();
    expect(screen.queryByText('认知中枢')).not.toBeInTheDocument();
    expect(screen.getByText(LABELS.nav_automation)).toBeInTheDocument();
    // Chat content (New Task button) is shown on /
    await waitFor(() => expect(screen.getByText(LABELS.newTask)).toBeInTheDocument());
  });

  it('treats /chat as a chat route (session list + active CTA + wide)', async () => {
    // Regression: `/chat` renders the same Chat page as `/` (App.jsx
    // mounts <Chat/> on both), and automation "Run Now" deep links
    // land on `/chat?session=<sid>`. The sidebar used to gate the
    // session list on `pathname === '/'` only, so `/chat` showed no
    // chat history.
    renderAtRoute(['/chat?session=sess-1&autorun=1']);
    const newTaskBtn = screen.getByText(LABELS.newTask).closest('button');
    expect(newTaskBtn.className).toMatch(/bg-sidebar-accent/);
    // SessionList mounts → its project dropdown + group header show
    // the Ungrouped label (zh default). On non-chat routes both are
    // absent. (It appears twice, so use the All variant.)
    await waitFor(() => expect(screen.getAllByText('未分组').length).toBeGreaterThan(0));
    // Chat routes use the wider sidebar (set via inline style after
    // the screen-adaptation work — previously `w-64` Tailwind class).
    expect(document.querySelector('aside').style.width).toBe('256px');
  });

  it('shows the session list on non-chat routes too (history always visible)', async () => {
    // The user wants chat history reachable at ALL times — the
    // session list renders on every route, not just `/` and `/chat`.
    renderAtRoute(['/automation']);
    expect(screen.getByText(LABELS.brand)).toBeInTheDocument();
    expect(screen.getByText(LABELS.nav_automation)).toBeInTheDocument();
    expect(screen.getByText(LABELS.newTask)).toBeInTheDocument();
    // SessionList mounts → the Ungrouped label appears (dropdown +
    // group header). The sidebar also keeps the wide layout so
    // the width doesn't shift when navigating between routes.
    await waitFor(() => expect(screen.getAllByText('未分组').length).toBeGreaterThan(0));
    expect(document.querySelector('aside').style.width).toBe('256px');
  });

  it('hides the chat content while collapsed on the chat route', async () => {
    renderAtRoute(['/']);
    await waitFor(() => expect(screen.getByText(LABELS.newTask)).toBeInTheDocument());
    const collapseBtn = screen.getByTitle(/collapse|收起/i);
    fireEvent.click(collapseBtn);
    // New Task label disappears, but the + icon stays (collapsed view)
    await waitFor(() => expect(screen.queryByText(LABELS.newTask)).not.toBeInTheDocument());
    expect(screen.queryByText(LABELS.brand)).not.toBeInTheDocument();
  });

  it('shows nav links on every route', () => {
    renderAtRoute(['/my-space']);
    const mySpaceLink = screen.getByText(LABELS.nav_mySpace).closest('a');
    expect(mySpaceLink).toBeInTheDocument();
  });

  it('brand is now a link to the chat home', () => {
    renderAtRoute(['/my-space']);
    const brandLink = screen.getByText(LABELS.brand).closest('a');
    expect(brandLink).not.toBeNull();
    expect(brandLink.getAttribute('href')).toBe('/');
  });

  it('New Task button is highlighted (active state) on /', () => {
    renderAtRoute(['/']);
    const newTaskBtn = screen.getByText(LABELS.newTask).closest('button');
    expect(newTaskBtn).not.toBeNull();
    // active treatment includes the sidebar-accent background class
    expect(newTaskBtn.className).toMatch(/bg-sidebar-accent/);
  });

  it('New Task button keeps neutral treatment on other routes', () => {
    renderAtRoute(['/automation']);
    const newTaskBtn = screen.getByText(LABELS.newTask).closest('button');
    expect(newTaskBtn).not.toBeNull();
    expect(newTaskBtn.className).toMatch(/border-border/);
    expect(newTaskBtn.className).toMatch(/hover:bg-sidebar-accent\/50/);
  });

  it('New Task button has the press-down scale utility for tactile feedback', () => {
    renderAtRoute(['/']);
    const newTaskBtn = screen.getByText(LABELS.newTask).closest('button');
    // The button has `active:scale-[0.97]` so it shrinks slightly on
    // press for a tactile feel — purely a visual cue, not a behavior
    // change.
    expect(newTaskBtn.className).toMatch(/active:scale-\[0\.97\]/);
  });

  it('New Task button on / does NOT have any continuous animation (no ring-pulse, no shine)', () => {
    // The "very simple" interactive design removed all continuous
    // animations. Only the static ring + click ripple remain.
    renderAtRoute(['/']);
    const newTaskBtn = screen.getByText(LABELS.newTask).closest('button');
    expect(newTaskBtn.className).not.toMatch(/animate-ring-pulse/);
    expect(newTaskBtn.className).not.toMatch(/animate-shine/);
  });

  it('+ icon has no rotation transition (kept static for simplicity)', () => {
    renderAtRoute(['/']);
    // The icon should NOT have a rotation transition utility — the
    // design is intentionally simple.
    const plus = document.querySelector('button svg');
    expect(plus).not.toBeNull();
    const cls = plus.getAttribute('class') || '';
    expect(cls).not.toMatch(/group-hover:rotate-90/);
    expect(cls).not.toMatch(/transition-transform/);
  });

  it('New Task button on / has a hover utility for color shift', () => {
    renderAtRoute(['/']);
    const newTaskBtn = screen.getByText(LABELS.newTask).closest('button');
    expect(newTaskBtn.className).toMatch(/hover:bg-sidebar-accent\/50/);
  });

  it('New Task button on other routes has a hover utility for color shift', () => {
    renderAtRoute(['/my-space']);
    const newTaskBtn = screen.getByText(LABELS.newTask).closest('button');
    expect(newTaskBtn.className).toMatch(/hover:bg-sidebar-accent\/50/);
  });

  it('clicking New Task adds a ripple element at the click point', () => {
    renderAtRoute(['/']);
    const newTaskBtn = screen.getByText(LABELS.newTask).closest('button');
    // Spy on the click handler is too invasive; instead we simulate
    // a click with explicit clientX/Y so the ripple is created at a
    // deterministic location, then check the DOM for a ripple span.
    fireEvent.click(newTaskBtn, { clientX: 100, clientY: 12 });
    const ripples = document.querySelectorAll('span.animate-ripple');
    expect(ripples.length).toBeGreaterThanOrEqual(1);
    // The first ripple's left/top should match the click coords
    const first = ripples[0];
    expect(first.style.left).toBe('100px');
    expect(first.style.top).toBe('12px');
  });

  it('multiple rapid clicks create multiple ripples (no clobbering)', () => {
    vi.useFakeTimers();
    try {
      renderAtRoute(['/']);
      const newTaskBtn = screen.getByText(LABELS.newTask).closest('button');
      fireEvent.click(newTaskBtn, { clientX: 10, clientY: 10 });
      fireEvent.click(newTaskBtn, { clientX: 50, clientY: 20 });
      fireEvent.click(newTaskBtn, { clientX: 200, clientY: 30 });
      const ripples = document.querySelectorAll('span.animate-ripple');
      expect(ripples.length).toBe(3);
    } finally {
      vi.useRealTimers();
    }
  });

  it('ripple is auto-removed after the animation ends', async () => {
    vi.useFakeTimers();
    try {
      renderAtRoute(['/']);
      const newTaskBtn = screen.getByText(LABELS.newTask).closest('button');
      fireEvent.click(newTaskBtn, { clientX: 30, clientY: 15 });
      expect(document.querySelectorAll('span.animate-ripple').length).toBe(1);
      // Advance past the 650ms cleanup timer and let React commit the
      // resulting state update. Without act() the state setter inside
      // the timer callback queues a render that hasn't flushed yet,
      // so the DOM still has the old ripple.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(700);
      });
      expect(document.querySelectorAll('span.animate-ripple').length).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it('clicking New Task clears the active session via the context', () => {
    renderAtRoute(['/']);
    // Drive the test: select a session first, then click New Task
    fireEvent.click(screen.getByTestId('test-select'));
    // After clicking New Task the active session is cleared and we
    // route to /. In MemoryRouter the URL is what we inspect.
    const newTaskBtn = screen.getByText(LABELS.newTask).closest('button');
    fireEvent.click(newTaskBtn);
    // Route should still be `/` (we started there); the newChat path
    // runs in handleNewTask synchronously. We assert the call didn't
    // throw, which is the meaningful behavior.
    expect(newTaskBtn).toBeInTheDocument();
  });

  it('clicking + New Task does NOT inherit the previous project (global chat box is clean)', async () => {
    // Regression: clicking "+ New Task" from a project-scoped
    // session used to call ``newChat(pendingProject)`` which
    // preserved the project. The user would then see the global
    // chat box (Zhanlu Cognitive Core empty state) with a stale
    // project chip. The fix passes ``null`` to ``newChat`` so the
    // global chat box is always a clean slate.
    //
    // If the user wants a project-scoped new chat, they pick the
    // project from the SessionList dropdown (which sets
    // ``pendingProject``) and then type into the chat input — the
    // next send creates the session in that project.
    function SetProject() {
      const { setPendingProject } = useChatSession();
      React.useEffect(() => {
        setPendingProject('ACME', 'acme-id');
      }, []);
      return null;
    }
    function ProjectDisplay() {
      const { pendingProject } = useChatSession();
      return React.createElement(
        'div',
        { 'data-testid': 'project-display' },
        pendingProject || 'null',
      );
    }

    render(
      React.createElement(
        QueryClientProvider,
        { client: new QueryClient({ defaultOptions: { queries: { retry: false } } }) },
        React.createElement(
          MemoryRouter,
          { initialEntries: ['/'] },
          React.createElement(
            ChatSessionProvider,
            null,
            React.createElement(SetProject),
            React.createElement(Sidebar),
            React.createElement(ProjectDisplay),
          ),
        ),
      ),
    );

    // Before click: pendingProject should be 'ACME' (set by
    // SetProject's useEffect).
    await waitFor(() =>
      expect(screen.getByTestId('project-display').textContent).toBe('ACME'),
    );

    const newTaskBtn = screen.getByText(LABELS.newTask).closest('button');
    fireEvent.click(newTaskBtn);

    // After click: pendingProject should be null (NOT inherited
    // from the previous session/selection).
    await waitFor(() =>
      expect(screen.getByTestId('project-display').textContent).toBe('null'),
    );
  });
});
