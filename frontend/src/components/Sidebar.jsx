import { useState, useEffect, useRef } from 'react';
import { NavLink, useLocation, useNavigate } from 'react-router-dom';
import { base44 } from '@/api/base44Client';
import { useLanguage } from '@/lib/LanguageProvider';
import { useChatSession } from '@/lib/ChatSessionContext';
import { Plus, Workflow, Store, Wrench, PanelLeftClose, PanelLeftOpen, ShieldCheck, Folder, LayoutGrid, Activity } from 'lucide-react';
import UserMenu from '@/components/UserMenu';
import ThemeToggle from '@/components/ThemeToggle';
import SessionList from '@/components/chat/SessionList';
import { useScreenSize } from '@/hooks/useScreenSize';

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
  const { t, lang } = useLanguage();
  const [collapsed, setCollapsed] = useState(false);
  // Sidebar top-level tabs (mirrors the two-pill "Apps / Agents" pattern
  // from the reference layout). Two scopes:
  //   - 'biz'    — daily-driver business items (Automation, My Space, My
  //                Files, Market, Dashboard accordion)
  //   - 'admin'  — IT / permission configuration (Toolkit, Admin Users).
  //                Tabs default to 'biz' and persist for the session. The
  //                tab is auto-switched when the user lands on a route
  //                that belongs to the admin scope (see the effect below)
  //                so deep-linking to /toolkit or /admin/users shows the
  //                right tab without a manual click.
  const [navTab, setNavTab] = useState('biz');
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

  useEffect(() => { base44.auth.me().then(setUser).catch(() => setUser(null)); }, []);

  // Auto-switch the sidebar tab based on the current route so deep links
  // land on the correct tab without manual clicks. The admin scope covers
  // /admin/*, /toolkit AND /market (the market is a platform-config area —
  // browse/subscribe/deploy — so it lives under 配置). Everything else
  // (including /, /chat, /automation, /my-space, /my-files) is the
  // business scope.
  //
  // Note: ``/market`` is the platform-config area; the old
  // ``/market-dashboard/*`` mirror was removed with the market
  // dashboard feature. The narrower predicate below matches the bare
  // ``/market`` or ``/market/<segment>`` only.
  useEffect(() => {
    const path = location.pathname;
    const isMarketAdminRoute =
      path === '/market' || path.startsWith('/market/');
    if (
      path.startsWith('/admin') ||
      path.startsWith('/toolkit') ||
      isMarketAdminRoute
    ) {
      setNavTab('admin');
    } else {
      setNavTab('biz');
    }
  }, [location.pathname]);

  // The main nav is intentionally split into two semantic groups, mirroring
  // the dual-card layout shown in the side-by-side reference. The "BIZ"
  // group covers daily-driver business items (the dashboard accordion is
  // rendered separately so it can keep its expand/collapse behavior). The
  // "ADMIN" group covers IT/permission configuration — admins see all of
  // it; non-admins only see the Toolkit entry.
  //
  // Chinese UI labels both blocks with a small uppercase tag (业务 / 配置).
  // English UI uses BIZ / ADMIN. Both render under the section header in
  // muted small caps so the two blocks read as separate cards at a glance.
  const bizItems = [
    { to: '/automation', label: t.sidebar.automation, icon: Workflow },
    { to: '/my-space', label: t.sidebar.mySpace || (lang === 'en' ? 'My Space' : '我的空间'), icon: LayoutGrid },
    { to: '/my-files', label: t.sidebar.myFiles, icon: Folder },
  ];

  const isAdmin = user?.role === 'admin';
  const adminItems = [
    { to: '/market', label: t.sidebar.market, icon: Store },
    { to: '/toolkit', label: t.sidebar.toolkit, icon: Wrench },
    ...(isAdmin ? [{ to: '/admin/users', label: (lang === 'en' ? t.sidebar.admin : t.sidebar.adminZh), icon: ShieldCheck }] : []),
    ...(isAdmin ? [{ to: '/admin/observability', label: (lang === 'en' ? 'Agent Observability' : '智能体可观测性'), icon: Activity }] : []),
  ];

  function linkClass({ isActive }) {
    return `flex min-w-0 items-center gap-3 rounded-lg px-3 py-2.5 text-sm transition-colors ${
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
    //     session (e.g., ACME → chip persisted on `/`).
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

  // ── Dashboard accordion (REMOVED 2026-08-27) ─────────────────────
  // The Market Dashboard section nav (Dashboard/Overview/Upstream/
  // Midstream/Downstream/Report/Weekly Summary) was removed from the
  // sidebar, and the /market-dashboard/* routes were removed from the
  // app with the market dashboard feature.

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
              <div className="min-w-0">
                <div className="font-display text-xl leading-none tracking-tight text-foreground truncate">{t.sidebar.brand}</div>
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
              className={`group relative flex w-full items-center justify-center gap-2 overflow-hidden rounded-lg border border-border px-3 py-2 text-sm font-medium text-foreground transition-colors duration-150 hover:bg-sidebar-accent/50 active:scale-[0.97]`}
            >
              {ripples.map((r) => (
                <span
                  key={r.id}
                  aria-hidden="true"
                  className="pointer-events-none absolute left-0 top-0 h-2 w-2 -translate-x-1/2 -translate-y-1/2 rounded-full animate-ripple bg-primary/30"
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
              className={`group relative flex w-full items-center justify-center overflow-hidden rounded-lg border border-border p-2 text-foreground transition-colors duration-150 hover:bg-sidebar-accent/50 active:scale-[0.97]`}
            >
              {ripples.map((r) => (
                <span
                  key={r.id}
                  aria-hidden="true"
                  className="pointer-events-none absolute left-0 top-0 h-2 w-2 -translate-x-1/2 -translate-y-1/2 rounded-full animate-ripple bg-primary/30"
                  style={{ left: r.x, top: r.y }}
                />
              ))}
              <Plus className="relative z-10 h-4 w-4" />
            </button>
          </div>
        )}
        {/* Sidebar top-level scope tabs (mirrors the two-pill "Apps / Agents"
            pattern from the reference). Two scopes only — business and
            admin — and they share the same card below. The active tab has a
            rounded-pill background + bold text; the inactive one is muted.
            Clicking a tab just swaps the items list; the user stays on
            whichever route they were on. */}
        {!collapsed && (
          <div className="px-3 pb-2">
            <div
              role="tablist"
              aria-label={lang === 'en' ? 'Sidebar scope' : '侧栏分组'}
              className="inline-flex w-full rounded-lg bg-sidebar-accent/60 p-1"
            >
              <button
                type="button"
                role="tab"
                aria-selected={navTab === 'biz'}
                onClick={() => setNavTab('biz')}
                className={`flex-1 inline-flex items-center justify-center gap-1.5 rounded-md px-3 py-1.5 text-sm transition-colors ${
                  navTab === 'biz'
                    ? 'bg-card text-foreground shadow-sm font-medium'
                    : 'text-muted-foreground hover:text-foreground'
                }`}
              >
                <LayoutGrid className="h-3.5 w-3.5 shrink-0" />
                {lang === 'en' ? 'Business' : '业务'}
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={navTab === 'admin'}
                onClick={() => setNavTab('admin')}
                className={`flex-1 inline-flex items-center justify-center gap-1.5 rounded-md px-3 py-1.5 text-sm transition-colors ${
                  navTab === 'admin'
                    ? 'bg-card text-foreground shadow-sm font-medium'
                    : 'text-muted-foreground hover:text-foreground'
                }`}
              >
                <Wrench className="h-3.5 w-3.5 shrink-0" />
                {lang === 'en' ? 'Admin' : '配置'}
              </button>
            </div>
          </div>
        )}
        {!collapsed && (
          <div className="px-3 pb-2">
            <div className="rounded-xl border border-sidebar-border bg-card px-2 py-2 shadow-sm">
              {navTab === 'biz' ? (
                <>
                  <nav className="space-y-0.5">
                    {bizItems.map((item) => (
                      <NavLink key={item.to} to={item.to} end={item.end} className={linkClass}>
                        <item.icon className="h-4 w-4 shrink-0" />
                        <span className="min-w-0 truncate">{item.label}</span>
                      </NavLink>
                    ))}
                  </nav>
                </>
              ) : (
                <nav className="space-y-0.5">
                  {adminItems.map((item) => (
                    <NavLink key={item.to} to={item.to} end={item.end} className={linkClass}>
                      <item.icon className="h-4 w-4 shrink-0" />
                      <span className="min-w-0 truncate">{item.label}</span>
                    </NavLink>
                  ))}
                </nav>
              )}
            </div>
          </div>
        )}
        {collapsed && (
          <nav className="space-y-0.5 px-2 pb-2">
            {bizItems.map((item) => (
              <NavLink key={item.to} to={item.to} end={item.end} className={linkClass} title={item.label}>
                <item.icon className="h-4 w-4 shrink-0" />
              </NavLink>
            ))}
            <div className="my-2 border-t border-sidebar-border" />
            {adminItems.map((item) => (
              <NavLink key={item.to} to={item.to} end={item.end} className={linkClass} title={item.label}>
                <item.icon className="h-4 w-4 shrink-0" />
              </NavLink>
            ))}
          </nav>
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
          <div className="mb-2 flex items-center gap-1">
            <ThemeToggle collapsed={false} />
          </div>
        )}
        {collapsed && (
          <div className="mb-2 flex flex-col items-center gap-1">
            <ThemeToggle collapsed />
          </div>
        )}
        {/* Admin entry now lives in the configuration card above (see
            `adminItems`). The bottom strip is just theme toggle + user
            menu — no role-dependent chrome here. */}
        <UserMenu user={user} collapsed={collapsed} />
      </div>
    </aside>
  );
}
