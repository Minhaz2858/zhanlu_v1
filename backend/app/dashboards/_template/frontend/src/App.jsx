import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { deriveSlug, useDashboardStream } from './hooks/useDashboardStream.js';
import { applyDesignTokens } from './hooks/useDesignTokens.js';
import { Widget } from './widgets/index.jsx';
import { Panel, panelsForPage } from './panels/index.jsx';

const STATUS_BADGE = {
  connecting: 'Connecting…',
  live: 'Live',
  reconnecting: 'Reconnecting…',
};

export default function App() {
  const slug = useMemo(() => deriveSlug(), []);
  const [config, setConfig] = useState(null);
  const [configError, setConfigError] = useState(null);
  const [data, setData] = useState({}); // metric_id -> {columns, rows, ...}
  const [theme, setTheme] = useState('light');
  const [lastUpdated, setLastUpdated] = useState(null);
  // Multi-page tabs (decision-center info architecture): the active page
  // filters which panels + layout sections render. Defaults to the first tab.
  const [activePage, setActivePage] = useState('overview');

  // Deep-linked filtered views: every query-string key on the dashboard URL
  // (e.g. /api/dashboards/apps/<slug>/?product=%E4%B9%99%E4%BA%8C%E9%86%87) is
  // forwarded to the backend, which only substitutes DECLARED :dim_* tokens.
  // Unlike the old mount-time snapshot, the filter state is LIVE: the filter
  // bar below mutates it and every metric refetches through the new query.
  const [filterState, setFilterState] = useState(() => {
    const url = new URLSearchParams(window.location.search);
    const state = { from: url.get('from') || '', to: url.get('to') || '', dims: {} };
    url.forEach((v, k) => {
      if (k !== 'from' && k !== 'to') state.dims[k] = v;
    });
    return state;
  });
  const filterQuery = useMemo(() => {
    const p = new URLSearchParams();
    if (filterState.from) p.set('from', filterState.from);
    if (filterState.to) p.set('to', filterState.to);
    Object.entries(filterState.dims).forEach(([k, v]) => {
      if (v) p.set(k, v);
    });
    const q = p.toString();
    return q ? `?${q}` : '';
  }, [filterState]);
  const hasFilters = useMemo(() => filterQuery.length > 1, [filterQuery]);
  // Declared dimension filters from config (label + column) for the filter bar.
  const declaredDims = useMemo(() => (config?.filters || []).filter((d) => d.key), [config]);
  // Distinct values per declared dim column, harvested from whatever metric
  // rows are already loaded (client-side, no extra backend call). Gives the
  // dropdowns real options once the table/trend widgets populate.
  const dimOptions = useMemo(() => {
    const map = {};
    declaredDims.forEach((d) => {
      if (!d.column) return;
      const set = new Set();
      Object.values(data).forEach((payload) => {
        (payload?.rows || []).forEach((r) => {
          const v = r[d.column];
          if (v != null && v !== '') set.add(String(v));
        });
      });
      if (set.size) map[d.key] = [...set].sort((a, b) => a.localeCompare(b)).slice(0, 200);
    });
    return map;
  }, [declaredDims, data]);
  function setDim(key, value) {
    setFilterState((s) => ({
      ...s,
      dims: { ...s.dims, [key]: value },
    }));
  }
  function resetFilters() {
    setFilterState({ from: '', to: '', dims: {} });
  }

  useEffect(() => {
    let cancelled = false;
    fetch('./config.json', { headers: { Accept: 'application/json' } })
      .then((r) => {
        if (!r.ok) throw new Error(`config fetch ${r.status}`);
        return r.json();
      })
      .then((cfg) => {
        if (!cancelled) {
          // Apply the persisted design-system tokens (colors/fonts/spacing) as
          // CSS custom properties BEFORE first paint of the dashboard chrome.
          applyDesignTokens(cfg.design);
          setConfig(cfg);
          setTheme(cfg.theme || 'light');
        }
      })
      .catch((e) => {
        if (!cancelled) setConfigError(String(e?.message || e));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const root = document.documentElement;
    if (theme === 'dark') root.classList.add('dark');
    else root.classList.remove('dark');
    // Visual style variants, applied from config.style (independent of the
    // light/dark theme — each style forces its own palette):
    //   chinese_bi  → .chinese-bi  (大屏 DataV, navy glow, red=up/green=down)
    //   ceo         → .ceo-bi      (dark petroleum decision center, gold accent)
    //   editorial   → .editorial-bi (light print report, serif + mono)
    //   standard    → no style class (slate light/dark BI)
    const STYLE_ROOT_CLASS = {
      chinese_bi: 'chinese-bi',
      ceo: 'ceo-bi',
      editorial: 'editorial-bi',
    };
    const styleClass = STYLE_ROOT_CLASS[config?.style];
    for (const cls of Object.values(STYLE_ROOT_CLASS)) {
      if (cls !== styleClass) root.classList.remove(cls);
    }
    if (styleClass) root.classList.add(styleClass);
    // Premium styles (ceo / editorial / chinese_bi) define their own palettes
    // in index.css via .ceo-bi / .editorial-bi / .chinese-bi token overrides.
    // applyDesignTokens() writes the SAME vars as INLINE styles (which beat
    // any stylesheet rule), so the stylesheet palettes would never win. When
    // a premium style is active, drop the inline tokens and let the CSS
    // override take over (the standard style keeps the inline design tokens).
    if (styleClass) {
      const DESIGN_TOKEN_KEYS = [
        'primary', 'primary-rgb', 'on-primary', 'on-primary-rgb',
        'secondary', 'secondary-rgb', 'accent', 'accent-rgb',
        'background', 'background-rgb', 'foreground', 'foreground-rgb',
        'muted', 'muted-rgb', 'border', 'border-rgb',
        'destructive', 'destructive-rgb', 'ring', 'ring-rgb',
        'card-radius', 'font-heading', 'font-body',
      ];
      for (let i = 1; i <= 6; i += 1) {
        DESIGN_TOKEN_KEYS.push(`chart-${i}`, `chart-${i}-rgb`);
      }
      DESIGN_TOKEN_KEYS.forEach((k) => root.style.removeProperty(`--ds-${k}`));
    }
  }, [theme, config]);

  // Initial REST fetch per metric (WS frames keep it fresh afterwards).
  useEffect(() => {
    if (!config) return undefined;
    let cancelled = false;
    config.metrics.forEach((m) => {
      fetch(`./metrics/${encodeURIComponent(m.id)}${filterQuery}`, {
        headers: { Accept: 'application/json' },
      })
        .then((r) => r.json())
        .then((res) => {
          if (!cancelled && res && res.data) {
            setData((prev) => ({ ...prev, [m.id]: res.data }));
            setLastUpdated(new Date());
          }
        })
        .catch(() => {});
    });
    return () => {
      cancelled = true;
    };
  }, [config, filterQuery]);

  // Refetch a metric through the URL filters (used to keep filtered views live).
  const refetchMetric = useCallback(
    (metricId) => {
      fetch(`./metrics/${encodeURIComponent(metricId)}${filterQuery}`, {
        headers: { Accept: 'application/json' },
      })
        .then((r) => r.json())
        .then((res) => {
          if (res && res.data) {
            setData((prev) => ({ ...prev, [metricId]: res.data }));
          }
        })
        .catch(() => {});
    },
    [filterQuery],
  );

  const onFrame = useCallback(
    (frame) => {
      if (frame && frame.metric_id && frame.data) {
        setLastUpdated(new Date());
        if (hasFilters) {
          // Unfiltered WS frames would clobber the deep-linked view — refetch
          // through the active URL filters instead so it stays live + filtered.
          refetchMetric(frame.metric_id);
        } else {
          setData((prev) => ({ ...prev, [frame.metric_id]: frame.data }));
        }
      }
    },
    [hasFilters, refetchMetric],
  );

  // Fresh full snapshot after any WS reconnect — live frames may have been
  // missed while disconnected, so the REST state is the source of truth again.
  const refetchAll = useCallback(() => {
    if (!config) return;
    config.metrics.forEach((m) => refetchMetric(m.id));
  }, [config, refetchMetric]);

  // 2026-08-27: self-healing periodic REST refetch. The WS only pushes on data
  // CHANGE, so a page opened while the app was still building (metrics 404'd)
  // would otherwise sit on "No data" forever if the WS is also down (expired
  // token / reconnect loop). Refetch every refresh interval so the dashboard
  // always converges to real data within one interval — WebSocket or not.
  useEffect(() => {
    if (!config) return undefined;
    const intervalMs = (config.refresh_interval_seconds || 30) * 1000;
    const t = setInterval(refetchAll, intervalMs);
    return () => clearInterval(t);
  }, [config, refetchAll]);

  const streamStatus = useDashboardStream(slug, onFrame, refetchAll);

  // Refresh countdown — ticks every second so the user sees when the next
  // periodic REST refetch fires (the advertised refresh interval is now
  // visible, not just implied).
  const refreshSeconds = config?.refresh_interval_seconds || 30;
  const [countdown, setCountdown] = useState(refreshSeconds);
  useEffect(() => {
    if (!config) return undefined;
    const tick = setInterval(() => {
      setCountdown((c) => (c <= 1 ? refreshSeconds : c - 1));
    }, 1000);
    return () => clearInterval(tick);
  }, [config, refreshSeconds]);
  // Restart the visible countdown whenever a manual/auto refetch completes.
  useEffect(() => {
    if (lastUpdated) setCountdown(refreshSeconds);
  }, [lastUpdated, refreshSeconds]);

  // Fullscreen toggle for the dashboard body (works when the iframe has
  // allowfullscreen, degrades gracefully otherwise).
  const [isFullscreen, setIsFullscreen] = useState(false);
  const rootRef = useRef(null);
  useEffect(() => {
    const onFsChange = () => {
      setIsFullscreen(Boolean(document.fullscreenElement));
    };
    document.addEventListener('fullscreenchange', onFsChange);
    return () => document.removeEventListener('fullscreenchange', onFsChange);
  }, []);
  function toggleFullscreen() {
    try {
      if (document.fullscreenElement) {
        document.exitFullscreen();
      } else if (rootRef.current?.requestFullscreen) {
        rootRef.current.requestFullscreen();
      }
    } catch {
      /* fullscreen not permitted in this frame — no-op */
    }
  }

  // Tell the parent (FullStackDashboardViewer) about our WS state so it can
  // show a reconnect indicator in its toolbar without reaching into the iframe.
  useEffect(() => {
    try {
      window.parent.postMessage(
        { type: 'dashboard-ws-status', status: streamStatus },
        window.location.origin,
      );
    } catch {
      /* cross-origin parent — ignore */
    }
  }, [streamStatus]);

  if (configError) {
    return (
      <div className="p-8 text-sm text-destructive">
        Failed to load dashboard config: {configError}
      </div>
    );
  }
  if (!config) {
    return <div className="p-8 text-sm text-foreground/60">Loading dashboard…</div>;
  }

  // Decision-center info architecture (2026-08-29):
  //  - pages: multi-page tabs; default single "Overview" page.
  //  - panels: typed AI-analysis blocks, filtered per active page.
  //  - layout sections carry "page" (which tab) and "panels" (right rail ids).
  const pages = Array.isArray(config.pages) && config.pages.length
    ? config.pages
    : [{ id: 'overview', label: 'Overview' }];
  const activePageId = pages.some((p) => p.id === activePage) ? activePage : pages[0].id;
  const pagePanels = panelsForPage(config.panels, activePageId);
  const sectionPanelIds = new Set(
    (Array.isArray(config.layout) ? config.layout : [])
      .filter((s) => (s.page || 'overview') === activePageId)
      .flatMap((s) => (Array.isArray(s.panels) ? s.panels : [])),
  );
  // Panels NOT claimed by a section render in the page-level flow.
  const loosePanels = pagePanels.filter((p) => !sectionPanelIds.has(p.id));
  const hdr = config.header || {};

  return (
    <div ref={rootRef} className="min-h-full bg-background p-4 text-foreground sm:p-6">
      <header className="mb-6 flex flex-wrap items-start justify-between gap-3 border-b border-border pb-4">
        <div className="min-w-0">
          <h1 className="font-heading text-xl font-semibold tracking-tight">{config.name}</h1>
          {config.description ? (
            <p className="mt-0.5 max-w-2xl text-sm text-foreground/60">{config.description}</p>
          ) : null}
          {hasFilters ? (
            <p className="mt-1 inline-flex items-center gap-1.5 rounded-full bg-primary/10 px-2.5 py-0.5 text-xs font-medium text-primary">
              Filtered view
            </p>
          ) : null}
        </div>
        <div className="flex shrink-0 items-center gap-3">
          {lastUpdated ? (
            <span className="hidden text-xs tabular-nums text-foreground/45 sm:inline">
              Updated {lastUpdated.toLocaleTimeString()}
            </span>
          ) : null}
          {config ? (
            <span className="hidden text-xs tabular-nums text-foreground/40 lg:inline" title="Next auto-refresh">
              <svg className="mr-1 inline h-3 w-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <circle cx="12" cy="12" r="9" />
                <path strokeLinecap="round" strokeLinejoin="round" d="M12 7v5l3 2" />
              </svg>
              {countdown}s
            </span>
          ) : null}
          <button
            type="button"
            onClick={toggleFullscreen}
            title={isFullscreen ? 'Exit fullscreen' : 'Fullscreen'}
            aria-label={isFullscreen ? 'Exit fullscreen' : 'Fullscreen'}
            className="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-border text-foreground/60 transition-colors hover:bg-muted hover:text-foreground"
          >
            <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              {isFullscreen ? (
                <path strokeLinecap="round" strokeLinejoin="round" d="M9 4v3a2 2 0 0 1-2 2H4m16 0h-3a2 2 0 0 1-2-2V4M9 20v-3a2 2 0 0 0-2-2H4m16 0h-3a2 2 0 0 0-2 2v3" />
              ) : (
                <path strokeLinecap="round" strokeLinejoin="round" d="M4 9V4h5m6 0h5v5M4 15v5h5m6 0h5v-5" />
              )}
            </svg>
          </button>
          <span
            className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium ${
              streamStatus === 'live'
                ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300'
                : 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300'
            }`}
          >
            <span className="h-1.5 w-1.5 rounded-full bg-current" />
            {STATUS_BADGE[streamStatus] || streamStatus}
          </span>
          <button
            type="button"
            onClick={refetchAll}
            title="Refresh now"
            aria-label="Refresh dashboard data"
            className="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-border text-foreground/60 transition-colors hover:bg-muted hover:text-foreground"
          >
            <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path strokeLinecap="round" strokeLinejoin="round" d="M4 4v5h5M20 20v-5h-5" />
              <path strokeLinecap="round" strokeLinejoin="round" d="M4 9a8 8 0 0 1 13.7-3.7L20 8M20 15a8 8 0 0 1-13.7 3.7L4 16" />
            </svg>
          </button>
        </div>
      </header>

      {/* Executive header extras (decision-center): greeting + market
          snapshot chips + period label — e.g. "早上好，刘总 — 今日有 3 项决策
          等待批准 · 布伦特 $79.4 ↓$1.2 · 石脑油 $642 ↓$9 · W-2025-23". */}
      {hdr.greeting || hdr.snapshot?.length || hdr.period ? (
        <div className="dc-exec-header mb-4">
          <div className="min-w-0">
            {hdr.greeting ? (
              <div className="dc-greeting">{hdr.greeting}</div>
            ) : null}
            {hdr.snapshot?.length ? (
              <div className="dc-snapshot">
                {hdr.snapshot.map((s, i) => (
                  <span key={i} className="dc-chip">
                    <span className="dc-chip-label">{s.label}</span>
                    <span className="dc-chip-value">{s.value}</span>
                    {s.delta ? (
                      <span className={`dc-chip-delta ${s.delta_tone === 'down' ? 'tone-down' : s.delta_tone === 'up' ? 'tone-up' : 'tone-neutral'}`}>
                        {s.delta}
                      </span>
                    ) : null}
                  </span>
                ))}
              </div>
            ) : null}
          </div>
          {hdr.period ? <div className="dc-period">{hdr.period}</div> : null}
        </div>
      ) : null}

      {/* Multi-page tabs (CEO / Weekly / Products / Competitive / Financial).
          Renders only when the spec declares pages. */}
      {pages.length > 1 ? (
        <nav className="dc-tabs mb-4" role="tablist" aria-label="Dashboard pages">
          {pages.map((p) => (
            <button
              key={p.id}
              type="button"
              role="tab"
              aria-selected={p.id === activePageId}
              className={`dc-tab${p.id === activePageId ? ' on' : ''}`}
              onClick={() => setActivePage(p.id)}
            >
              {p.label}
            </button>
          ))}
        </nav>
      ) : null}

      {(declaredDims.length > 0 || filterState.from || filterState.to) ? (
        <div className="mb-4 flex flex-wrap items-center gap-2 rounded-lg border border-border bg-muted/20 p-2.5">
          <span className="text-xs font-medium uppercase tracking-wide text-foreground/50">Filters</span>
          <label className="inline-flex items-center gap-1.5 text-xs text-foreground/70">
            <span className="text-foreground/45">From</span>
            <input
              type="date"
              value={filterState.from}
              onChange={(e) => setFilterState((s) => ({ ...s, from: e.target.value }))}
              className="rounded-md border border-border bg-background px-2 py-1 text-xs text-foreground outline-none focus:border-ring"
            />
          </label>
          <label className="inline-flex items-center gap-1.5 text-xs text-foreground/70">
            <span className="text-foreground/45">To</span>
            <input
              type="date"
              value={filterState.to}
              onChange={(e) => setFilterState((s) => ({ ...s, to: e.target.value }))}
              className="rounded-md border border-border bg-background px-2 py-1 text-xs text-foreground outline-none focus:border-ring"
            />
          </label>
          {declaredDims.map((d) => (
            <label key={d.key} className="inline-flex items-center gap-1.5 text-xs text-foreground/70">
              <span className="text-foreground/45">{d.label || d.key}</span>
              <select
                value={filterState.dims[d.key] || ''}
                onChange={(e) => setDim(d.key, e.target.value)}
                className="max-w-[180px] rounded-md border border-border bg-background px-2 py-1 text-xs text-foreground outline-none focus:border-ring"
              >
                <option value="">All</option>
                {(dimOptions[d.key] || []).map((v) => (
                  <option key={v} value={v}>{v}</option>
                ))}
              </select>
            </label>
          ))}
          {hasFilters ? (
            <button
              type="button"
              onClick={resetFilters}
              className="ml-auto rounded-md border border-border px-2 py-1 text-xs text-foreground/70 transition-colors hover:bg-muted hover:text-foreground"
            >
              Reset
            </button>
          ) : null}
        </div>
      ) : null}

      {(config.insights || []).length > 0 ? (
        <div className="mb-4 grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
          {(config.insights || []).map((insight, i) => (
            <div
              key={i}
              className="rounded-card border border-primary/20 bg-primary/5 p-3 text-sm"
            >
              {insight.title ? (
                <div className="mb-1 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-primary">
                  <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M9 12l2 2 4-4m5.6 2A9 9 0 1 1 4.4 12a9 9 0 0 1 16.7-2z" />
                  </svg>
                  {insight.title}
                </div>
              ) : null}
              <p className="leading-relaxed text-foreground/80">{insight.body}</p>
            </div>
          ))}
        </div>
      ) : null}

      {/* Page-level panel flow (decision-center): panels for the active page
          NOT claimed by any section render here first, in a span-aware grid
          (full = full width, half = 2-up, third = 3-up on wide screens). */}
      {loosePanels.length > 0 ? (
        <div className="dc-flow mb-4">
          {loosePanels.map((p) => (
            <Panel key={p.id} panel={p} />
          ))}
        </div>
      ) : null}

      {/* Sectioned layout (BI story): when the spec declares config.layout
          [{title, widgets: [metric_id,...], page?, panels?}], render each
          section for the ACTIVE PAGE with a header + its own responsive grid.
          A section with "panels" renders a 2-column decision-center split:
          left = widget grid, right = panels rail (alerts / decisions / chain /
          customers / inventory …). Metrics not listed in any section fall
          into a trailing "Other" group so nothing is ever dropped. When no
          layout is declared, render one flat grid (legacy behavior). */}
      {(() => {
        const layout = Array.isArray(config.layout) ? config.layout : null;
        if (!layout || layout.length === 0) {
          return (
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
              {config.metrics.map((m) => (
                <Widget
                  key={m.id}
                  metric={m}
                  data={data[m.id]}
                  className={m.options?.span === 'wide' ? 'md:col-span-2' : ''}
                />
              ))}
            </div>
          );
        }
        const listed = new Set();
        // Collect listed ids from ALL sections (any page) BEFORE the page
        // filter — a widget that lives on another page must not fall into the
        // "Other" group here (that leaked cross-page widgets into Other and
        // crashed on the missing `panels` key).
        layout.forEach((sec) => {
          (Array.isArray(sec?.widgets) ? sec.widgets : []).forEach((id) => listed.add(id));
        });
        const sections = layout
          .filter((sec) => (sec?.page || 'overview') === activePageId)
          .map((sec) => {
            const ids = Array.isArray(sec?.widgets) ? sec.widgets : [];
            const secMetrics = config.metrics.filter((m) => ids.includes(m.id));
            const secPanels = (Array.isArray(sec?.panels) ? sec.panels : [])
              .map((pid) => pagePanels.find((p) => p.id === pid))
              .filter(Boolean);
            return { title: sec?.title || 'Section', metrics: secMetrics, panels: secPanels };
          });
        const other = config.metrics.filter((m) => !listed.has(m.id));
        const all = other.length
          ? [...sections, { title: 'Other', metrics: other, panels: [] }]
          : sections;
        return all
          .filter((s) => s.metrics.length > 0 || (s.panels || []).length > 0)
          .map((s, si) => (
            <div key={si} className="mb-6 last:mb-0">
              <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold uppercase tracking-wide text-foreground/70">
                <span className="h-4 w-1 rounded-full" style={{ background: 'var(--ds-primary)' }} />
                {s.title}
                <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-medium text-foreground/45">
                  {s.metrics.length}
                </span>
              </h2>
              {(s.panels || []).length > 0 ? (
                <div className="grid grid-cols-1 gap-4 xl:grid-cols-[1fr_360px]">
                  <div className="grid grid-cols-1 content-start gap-4 md:grid-cols-2">
                    {s.metrics.map((m) => (
                      <Widget
                        key={m.id}
                        metric={m}
                        data={data[m.id]}
                        className={m.options?.span === 'wide' ? 'md:col-span-2' : ''}
                      />
                    ))}
                  </div>
                  <div className="dc-rail space-y-4">
                    {(s.panels || []).map((p) => (
                      <Panel key={p.id} panel={p} />
                    ))}
                  </div>
                </div>
              ) : (
                <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
                  {s.metrics.map((m) => (
                    <Widget
                      key={m.id}
                      metric={m}
                      data={data[m.id]}
                      className={m.options?.span === 'wide' ? 'md:col-span-2' : ''}
                    />
                  ))}
                </div>
              )}
            </div>
          ));
      })()}

      {streamStatus === 'reconnecting' ? (
        <p className="mt-4 text-center text-xs text-foreground/50">Reconnecting to live data…</p>
      ) : null}

      {/* Provenance footer (decision-center): "数字来源：ERP + 市场数据" */}
      {config.footer?.sources ? (
        <footer className="dc-footer">{config.footer.sources}</footer>
      ) : null}
    </div>
  );
}
