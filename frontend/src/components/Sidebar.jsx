import { useState, useEffect, useRef } from 'react';
import { NavLink, useLocation, useNavigate } from 'react-router-dom';
import { base44 } from '@/api/base44Client';
import { useLanguage } from '@/lib/LanguageProvider';
import { useChatSession } from '@/lib/ChatSessionContext';
import { Plus, Workflow, Store, Wrench, PanelLeftClose, PanelLeftOpen, LayoutDashboard, ChevronDown, ShieldCheck, Folder, LayoutGrid } from 'lucide-react';
import UserMenu from '@/components/UserMenu';
import ThemeToggle from '@/components/ThemeToggle';
import AlertButton from '@/components/AlertButton';
import ProactiveAlerts from '@/components/ProactiveAlerts';
import SessionList from '@/components/chat/SessionList';
import { useScreenSize } from '@/hooks/useScreenSize';
import brandLogo from '../../logo/logo.png';

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
/**
 * Build an alert-type-aware investigation question for the Ecisco BI
 * assistant.  The question is pre-filled into the chat input (not
 * auto-sent) so the user can review before pressing Enter.
 */
export function buildInvestigateQuestion(alert) {
  const product = alert.product_name || alert.product_id || 'this product';
  const change  = Math.abs(alert.change_7d_pct ?? 0).toFixed(1);
  const price   = alert.price
    ? `${Number(alert.price).toLocaleString()} CNY/ton`
    : 'price TBD';
  const sign    = (alert.change_7d_pct ?? 0) >= 0 ? '+' : '-';

  // Specific alert types first (they have dedicated templates)
  // daily_digest
  if (alert.alert_type === 'daily_digest') {
    const date = alert.payload?.date || 'today';
    return `展开 ${date} 晨报详情。分析每个产品的价格变动对利润的影响，并给出具体的采购和库存建议。`;
  }
  // price_spike
  if (alert.alert_type === 'price_spike') {
    return `${product} 7d increase +${change}% (current ${price}). `
      + `Have we entered a price increase window, and how should inventory level `
      + `and shipment pace be coordinated?`;
  }
  // price_drop
  if (alert.alert_type === 'price_drop') {
    return `${product} 7d drop ${change}% (current ${price}). `
      + `Is supplier signaling weakness, and what is the right procurement pivot `
      + `(delay orders / renegotiate / lock contract)?`;
  }
  // regional_arbitrage
  if (alert.alert_type === 'regional_arbitrage') {
    const src = alert.source || 'source A';
    const dst = alert.destination || 'source B';
    return `Regional arbitrage detected — ${product} ${src} vs ${dst} `
      + `(Δ ${sign}${change}%, current ${price}). `
      + `Should we redirect shipment to the higher-margin region?`;
  }
  // capacity_change
  if (alert.alert_type === 'capacity_change') {
    return `Capacity signal — ${product}: ${alert.message || `${sign}${change}% capacity change`}. `
      + `Forecast supply tightness and recommend inventory + contract actions.`;
  }
  // Implicit sign-based fallback (no explicit alert_type)
  if ((alert.change_7d_pct ?? 0) > 0) {
    return `${product} 7d increase +${change}% (current ${price}). `
      + `Have we entered a price increase window, and how should inventory level `
      + `and shipment pace be coordinated?`;
  }
  if ((alert.change_7d_pct ?? 0) < 0) {
    return `${product} 7d drop ${change}% (current ${price}). `
      + `Is supplier signaling weakness, and what is the right procurement pivot `
      + `(delay orders / renegotiate / lock contract)?`;
  }
  // Generic fallback
  return alert.message
    ? `Investigate this alert — ${alert.message}`
    : `Analyze ${product} — ${alert.alert_type || 'market signal'} (${sign}${change}% over 7d). Recommend action.`;
}

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

  // ── Alerts panel state ────────────────────────────────────────────
  const [alertPanelOpen, setAlertPanelOpen] = useState(false);

  const handleOpenAlertPanel = () => setAlertPanelOpen(true);
  const handleCloseAlertPanel = () => setAlertPanelOpen(false);

  const handleInvestigateAlert = (alert) => {
    const question = buildInvestigateQuestion(alert);
    const projectId   = 'e5ac337b-469a-480d-822b-f6a3155e652c';
    const projectName = 'Ecisco BI';

    setAlertPanelOpen(false);

    const onChat = location.pathname === '/' || location.pathname.startsWith('/chat');
    if (onChat) {
      // User is already on the chat page — dispatch an event so the
      // Chat component can create a new session and prefill the input
      // (avoids navigate + event double-fire).
      window.dispatchEvent(new CustomEvent('alerts:investigate', {
        detail: { question, projectId, projectName },
      }));
    } else {
      // Navigate to chat with URL params. Chat.jsx's prefill effect
      // reads ?prefill= (not ?prompt=) and sets the input value.
      navigate(
        `/chat?project=${projectId}` +
        `&projectName=${encodeURIComponent(projectName)}` +
        `&prefill=${encodeURIComponent(question)}`
      );
    }
  };

  useEffect(() => { base44.auth.me().then(setUser).catch(() => setUser(null)); }, []);

  // Auto-switch the sidebar tab based on the current route so deep links
  // land on the correct tab without manual clicks. The admin scope covers
  // /admin/*, /toolkit AND /market (the market is a platform-config area —
  // browse/subscribe/deploy — so it lives under 配置). Everything else
  // (including /, /chat, /automation, /my-space, /my-files,
  // /market-dashboard/*) is the business scope.
  //
  // Note: ``/market`` and ``/market-dashboard/*`` share a path prefix
  // (``/market``) but are completely different features — the former is
  // the platform-config area, the latter is the read-only EDIA mirror
  // that lives under the Dashboard accordion in the Business tab. A
  // naive ``path.startsWith('/market')`` check matches BOTH and would
  // silently flip the sidebar to Admin when the user clicks Dashboard.
  // The narrower predicate below excludes /market-dashboard by checking
  // that the path is either the bare ``/market`` or ``/market/<segment>``
  // (a single ``/`` after ``/market``), not ``/market-dashboard``.
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
  // /market-dashboard/* routes. Always visible so users can discover
  // the feature even on servers where the data source is not yet
  // configured — the page itself shows a friendly "data source
  // offline" banner in that case.
  const isDashboardRoute = location.pathname.startsWith('/market-dashboard');
  const [dashboardOpen, setDashboardOpen] = useState(isDashboardRoute);
  useEffect(() => {
    if (isDashboardRoute) setDashboardOpen(true);
  }, [isDashboardRoute]);

  const dashboardItems = [
    { to: '/market-dashboard/overview', label: t.sidebar.dashboardOverview },
    { to: '/market-dashboard/upstream', label: t.sidebar.dashboardUpstream },
    { to: '/market-dashboard/midstream', label: t.sidebar.dashboardMidstream },
    { to: '/market-dashboard/downstream', label: t.sidebar.dashboardDownstream },
    { to: '/market-dashboard/report', label: t.sidebar.dashboardReport },
  ];
  // enabledQuery is still fetched so the query is warm when the user
  // navigates to the dashboard page, but the accordion is no longer
  // gated on it — see the comment above for rationale.

  return (
    <>
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
              <div className="flex h-9 w-9 shrink-0 items-center justify-center overflow-hidden rounded-lg bg-white">
                <img src={brandLogo} alt="Ecisco AI logo" className="h-full w-full object-contain p-1" />
              </div>
              <div className="min-w-0">
                <div className="font-display text-lg leading-none tracking-tight text-foreground truncate">{t.sidebar.brand}</div>
                <div className="mt-1 text-[10px] uppercase tracking-[0.15em] text-muted-foreground">Ecisco</div>
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
                  <div className="mt-1 border-t border-sidebar-border/60 pt-1">
                    <button
                      type="button"
                      onClick={() => setDashboardOpen((v) => !v)}
                      className={`flex w-full items-center gap-3 rounded-lg px-3 py-2.5 text-sm transition-colors ${
                        dashboardOpen
                          ? 'text-foreground font-medium'
                          : 'text-muted-foreground hover:bg-sidebar-accent/50 hover:text-foreground'
                      }`}
                      aria-expanded={dashboardOpen}
                    >
                      <LayoutDashboard className="h-4 w-4 shrink-0" />
                      <span className="min-w-0 flex-1 truncate text-left">{t.sidebar.dashboard}</span>
                      <ChevronDown
                        className={`h-3.5 w-3.5 shrink-0 transition-transform ${dashboardOpen ? 'rotate-180' : ''}`}
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
                            <span className="min-w-0 truncate text-xs">{item.label}</span>
                          </NavLink>
                        ))}
                      </nav>
                    )}
                  </div>
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
            <AlertButton collapsed={false} onClick={handleOpenAlertPanel} panelOpen={alertPanelOpen} />
          </div>
        )}
        {collapsed && (
          <div className="mb-2 flex flex-col items-center gap-1">
            <ThemeToggle collapsed />
            <AlertButton collapsed onClick={handleOpenAlertPanel} panelOpen={alertPanelOpen} />
          </div>
        )}
        {/* Admin entry now lives in the configuration card above (see
            `adminItems`). The bottom strip is just theme toggle + user
            menu — no role-dependent chrome here. */}
        <UserMenu user={user} collapsed={collapsed} />
      </div>
    </aside>

    {/* Proactive Alerts slide-out panel */}
    <ProactiveAlerts
      open={alertPanelOpen}
      onClose={handleCloseAlertPanel}
      onInvestigate={handleInvestigateAlert}
    />
    </>
  );
}
