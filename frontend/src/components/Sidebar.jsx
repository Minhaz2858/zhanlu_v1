import { useState, useEffect, useRef } from 'react';
import { NavLink, useLocation, useNavigate } from 'react-router-dom';
import { base44 } from '@/api/base44Client';
import { useLanguage } from '@/lib/LanguageProvider';
import { useChatSession } from '@/lib/ChatSessionContext';
import { Plus, Workflow, Boxes, Store, Wrench, PanelLeftClose, PanelLeftOpen, LayoutDashboard, ChevronDown } from 'lucide-react';
import UserMenu from '@/components/UserMenu';
import ThemeToggle from '@/components/ThemeToggle';
import SessionList from '@/components/chat/SessionList';
import { useScreenSize } from '@/hooks/useScreenSize';
import { useQuery } from '@tanstack/react-query';
import { marketDashboardApi } from '@/api/marketDashboard';

/**
 * Sidebar — the app's single unified sidebar.
 *
 * History (Option A: unified sidebar):
 *   Before this refactor, two separate sidebars sat side by side on the
 *   chat page: the main nav sidebar (this component, ~240px) and the
 *   chat session list (~280px). On a 1280px laptop that consumed 520px
 *   before the chat even started. The two have now been merged into one
 *   route-aware component:
 *
 *     - On `/` (chat), the sidebar shows brand + nav + the chat session
 *       list (rendered via <SessionList/>, which reads from
 *       ChatSessionContext). Width is `w-64` (256px).
 *     - On other routes, the sidebar shows only brand + nav + UserMenu.
 *       Width stays `w-60` (240px).
 *
 *   The collapse button at the top collapses the whole sidebar to icon-
 *   only (`w-16`, 64px). When collapsed, the chat content is hidden —
 *   the icon-only strip shows brand + nav + UserMenu.
 */
export default function Sidebar() {
  const [user, setUser] = useState(null);
  const { t } = useLanguage();
  const [collapsed, setCollapsed] = useState(false);
  const location = useLocation();
  const navigate = useNavigate();
  // The "New Task" button uses the chat context to clear the active
  // session, then routes to `/`. Pulling `newChat` from the context
  // keeps a single source of truth (Option A) — the same function the
  // in-page "+ New Task" used to call.
  const { newChat } = useChatSession();
  // Ripples for the "+ New Task" click feedback. Each entry is a
  // {id, x, y} — `x`/`y` are the click position relative to the
  // button. The entry is auto-removed when the CSS animation ends.
  // Using a state list (not a single ripple) means rapid clicks all
  // get their own ripple instead of getting clobbered.
  const [ripples, setRipples] = useState([]);
  const rippleIdRef = useRef(0);

  // The chat routes get the "active" treatment on the "+ New Task"
  // CTA. Both `/` and `/chat` render the same Chat page (see
  // App.jsx) — and `/chat` is what deep links use (e.g. automation
  // "Run Now" navigates to `/chat?session=<sid>&autorun=1`). The
  // session list itself is shown on ALL routes (see the render
  // below) — this flag is only for CTA styling now.
  const isChatRoute = location.pathname === '/' || location.pathname === '/chat';

  useEffect(() => { base44.auth.me().then(setUser).catch(() => setUser(null)); }, []);

  // "Cognitive Hub" was previously the first nav item, but it routed
  // to the same place as "+ New Task" (`/`) and made the sidebar
  // taller for no added value. The other items still navigate to
  // distinct sections.
  const navItems = [
    { to: '/automation', label: t.sidebar.automation, icon: Workflow },
    { to: '/my-space', label: t.sidebar.mySpace, icon: Boxes },
    { to: '/market', label: t.sidebar.market, icon: Store },
    { to: '/toolkit', label: t.sidebar.toolkit, icon: Wrench },
  ];

  function linkClass({ isActive }) {
    return `flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm transition-colors ${
      isActive ? 'bg-sidebar-accent text-accent-foreground font-medium' : 'text-muted-foreground hover:bg-sidebar-accent/50 hover:text-foreground'
    } ${collapsed ? 'justify-center' : ''}`;
  }

  function handleNewTask(event) {
    // Mirror the old in-page handler: clear the active session and
    // route to `/`. The chat page's `useEffect` on `activeId` is
    // responsible for resetting its local UI state.
    //
    // "+ New Task" always goes to the GLOBAL chat box (the empty
    // Zhanlu Cognitive Core state) — it does NOT inherit the
    // previous session's project. Rationale:
    //   - The "+ New Task" button is the primary CTA at the top of
    //     the sidebar; users expect it to give them a clean slate.
    //   - If the user wants a project-scoped new chat, they can
    //     pick the project from the SessionList dropdown (which
    //     sets ``pendingProject``) and then type into the chat
    //     input — the next send will create the session in that
    //     project.
    //   - Preserving the previous session's project caused a bug
    //     where the global chat box showed a stale project chip
    //     after clicking "+ New Task" from a project-scoped
    //     session (e.g., ECISCO → chip persisted on `/`).
    if (event && event.currentTarget) {
      const rect = event.currentTarget.getBoundingClientRect();
      const ripple = {
        id: ++rippleIdRef.current,
        x: event.clientX - rect.left,
        y: event.clientY - rect.top,
      };
      setRipples((prev) => [...prev, ripple]);
      // Auto-remove after the 600ms animation finishes. If the user
      // clicks again before the cleanup runs, multiple ripples can
      // coexist — each has its own id.
      setTimeout(() => {
        setRipples((prev) => prev.filter((r) => r.id !== ripple.id));
      }, 650);
    }
    newChat(null);
    navigate('/');
  }

  // Width: expanded uses the current screen tier's sidebarWidth (see
  // lib/screen-config.js) — narrower on small laptops (compact), wider
  // on 2K/4K (wide/ultra). Collapsed state always uses 64px regardless
  // of route. Inline style (not a Tailwind class) keeps
  // `transition-[width]` animatable for both manual collapse and tier
  // changes, while flex-shrink-0 keeps the rail from being squeezed.
  const { settings } = useScreenSize();
  const sidebarWidth = collapsed ? 64 : settings.sidebarWidth;

  // ── Dashboard accordion ──────────────────────────────────────────
  // Read-only mirror of EDIA's MySQL warehouse. Auto-expands on
  // /market-dashboard/* routes. Hidden entirely when EDIA is not
  // configured (no dead nav items in dev environments).
  const enabledQuery = useQuery({
    queryKey: ['market-dashboard', 'enabled'],
    queryFn: marketDashboardApi.enabled,
    staleTime: 60_000,
  });
  const isDashboardRoute = location.pathname.startsWith('/market-dashboard');
  const [dashboardOpen, setDashboardOpen] = useState(isDashboardRoute);
  useEffect(() => {
    if (isDashboardRoute) setDashboardOpen(true);
  }, [isDashboardRoute]);

  const dashboardItems = [
    { to: '/market-dashboard/overview', label: 'Overview' },
    { to: '/market-dashboard/upstream', label: 'Upstream' },
    { to: '/market-dashboard/midstream', label: 'Midstream' },
    { to: '/market-dashboard/downstream', label: 'Downstream' },
  ];
  const showAccordion = !enabledQuery.isLoading && enabledQuery.data?.enabled === true;

  return (
    <aside
      style={{ width: sidebarWidth }}
      className="relative flex h-screen shrink-0 flex-col border-r border-sidebar-border bg-sidebar transition-[width] duration-200"
    >
      <div className={`flex items-center px-3 py-5 ${collapsed ? 'justify-center' : 'gap-2.5'}`}>
        {collapsed ? (
          <button
            onClick={() => setCollapsed(false)}
            className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary text-primary-foreground transition-opacity hover:opacity-90"
            title={t.sidebar.expand}
          >
            <PanelLeftOpen className="h-4 w-4" />
          </button>
        ) : (
          <>
            {/* Brand is now a link to `/` — gives the user a way to
                return to the chat home from any other section. With
                "Cognitive Hub" removed from the nav, the brand is the
                most visible entry point to the chat page. */}
            <NavLink to="/" end className="flex flex-1 items-center gap-2.5 min-w-0" title={t.sidebar.brand}>
              <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-primary font-display text-base text-primary-foreground">S</div>
              <div className="min-w-0">
                <div className="font-display text-lg leading-none tracking-tight text-foreground truncate">{t.sidebar.brand}</div>
                <div className="mt-1 text-[10px] uppercase tracking-[0.15em] text-muted-foreground">Synexia</div>
              </div>
            </NavLink>
            <button
              onClick={() => setCollapsed(true)}
              className="inline-flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-sidebar-accent hover:text-foreground"
              title={t.sidebar.collapse}
            >
              <PanelLeftClose className="h-4 w-4" />
            </button>
          </>
        )}
      </div>
      {/* Middle: primary CTA + nav + (chat-only) session list. flex-1 +
          min-h-0 so the scrollable SessionList can shrink inside the
          flex column. */}
      <div className="flex min-h-0 flex-1 flex-col">
        {/* Primary CTA: "+ New Task". Sits in the slot where "Cognitive
            Hub" used to be — the previous in-page button was removed
            from SessionList.jsx to avoid a duplicate. Highlighted when
            the user is on `/` so the active state for the chat route
            is still visible (it used to be on Cognitive Hub).

            Kept deliberately simple — no continuous animation, no
            spinning icons, no shine sweeps. Only:
              • hover   — subtle color shift (darken on the primary
                          variant, lighten on the active variant)
              • press   — active:scale-[0.97] for tactile feedback
              • click   — a single ripple from the click point */}
        {!collapsed && (
          <div className="px-3 pb-2">
            <button
              onClick={handleNewTask}
              title={t.sessionList.newTask}
              className={`group relative flex w-full items-center justify-center gap-2 overflow-hidden rounded-lg px-3 py-2 text-sm font-medium transition-colors duration-150 active:scale-[0.97] ${
                isChatRoute
                  ? 'bg-sidebar-accent text-accent-foreground ring-1 ring-sidebar-ring hover:bg-sidebar-accent/80'
                  : 'bg-primary text-primary-foreground hover:bg-primary/90'
              }`}
            >
              {ripples.map((r) => (
                <span
                  key={r.id}
                  aria-hidden="true"
                  className={`pointer-events-none absolute left-0 top-0 h-2 w-2 -translate-x-1/2 -translate-y-1/2 rounded-full animate-ripple ${
                    isChatRoute ? 'bg-sidebar-ring/40' : 'bg-white/40'
                  }`}
                  style={{ left: r.x, top: r.y }}
                />
              ))}
              <Plus className="relative z-10 h-4 w-4" />
              <span className="relative z-10">{t.sessionList.newTask}</span>
            </button>
          </div>
        )}
        {collapsed && (
          <div className="px-2 pb-2">
            <button
              onClick={handleNewTask}
              title={t.sessionList.newTask}
              className={`group relative flex w-full items-center justify-center overflow-hidden rounded-lg p-2 transition-colors duration-150 active:scale-[0.97] ${
                isChatRoute
                  ? 'bg-sidebar-accent text-accent-foreground ring-1 ring-sidebar-ring hover:bg-sidebar-accent/80'
                  : 'bg-primary text-primary-foreground hover:bg-primary/90'
              }`}
            >
              {ripples.map((r) => (
                <span
                  key={r.id}
                  aria-hidden="true"
                  className={`pointer-events-none absolute left-0 top-0 h-2 w-2 -translate-x-1/2 -translate-y-1/2 rounded-full animate-ripple ${
                    isChatRoute ? 'bg-sidebar-ring/40' : 'bg-white/40'
                  }`}
                  style={{ left: r.x, top: r.y }}
                />
              ))}
              <Plus className="relative z-10 h-4 w-4" />
            </button>
          </div>
        )}
        <nav className="space-y-0.5 px-3">
          {navItems.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.end} className={linkClass} title={collapsed ? item.label : undefined}>
              <item.icon className="h-4 w-4 shrink-0" />
              {!collapsed && item.label}
            </NavLink>
          ))}
        </nav>
        {showAccordion && !collapsed && (
          <div className="mt-2 border-t border-sidebar-border pt-2">
            <button
              type="button"
              onClick={() => setDashboardOpen((v) => !v)}
              className="flex w-full items-center gap-2 rounded-md px-3 py-1.5 text-sm font-semibold text-sidebar-foreground hover:bg-sidebar-accent"
              aria-expanded={dashboardOpen}
            >
              <LayoutDashboard className="h-4 w-4 shrink-0" />
              <span className="flex-1 text-left">Dashboard</span>
              <ChevronDown
                className={`h-3.5 w-3.5 transition-transform ${dashboardOpen ? 'rotate-180' : ''}`}
              />
            </button>
            {dashboardOpen && (
              <nav className="mt-1 space-y-0.5">
                {dashboardItems.map((item) => (
                  <NavLink
                    key={item.to}
                    to={item.to}
                    className={linkClass}
                    style={{ paddingLeft: '2rem' }}
                  >
                    <span className="text-xs">{item.label}</span>
                  </NavLink>
                ))}
              </nav>
            )}
          </div>
        )}
        {/* Chat session list: on EVERY route when the sidebar is
            expanded — chat history must be reachable at all times.
            When collapsed, the icon strip is intentionally minimal —
            the user can re-expand to see their sessions. */}
        {!collapsed && (
          <div className="mt-3 flex min-h-0 flex-1 flex-col border-t border-sidebar-border">
            <SessionList />
          </div>
        )}
      </div>
      <div className="border-t border-sidebar-border px-3 py-3">
        {!collapsed && (
          <div className="mb-2">
            <ThemeToggle collapsed={false} />
          </div>
        )}
        {collapsed && (
          <div className="mb-2 flex justify-center">
            <ThemeToggle collapsed />
          </div>
        )}
        <UserMenu user={user} collapsed={collapsed} />
      </div>
    </aside>
  );
}
