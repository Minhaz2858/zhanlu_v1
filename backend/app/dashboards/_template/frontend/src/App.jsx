import { useCallback, useEffect, useMemo, useState } from 'react';
import { deriveSlug, useDashboardStream } from './hooks/useDashboardStream.js';
import { Widget } from './widgets/index.jsx';

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

  useEffect(() => {
    let cancelled = false;
    fetch('./config.json', { headers: { Accept: 'application/json' } })
      .then((r) => {
        if (!r.ok) throw new Error(`config fetch ${r.status}`);
        return r.json();
      })
      .then((cfg) => {
        if (!cancelled) {
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
  }, [theme]);

  // Initial REST fetch per metric (WS frames keep it fresh afterwards).
  useEffect(() => {
    if (!config) return undefined;
    let cancelled = false;
    config.metrics.forEach((m) => {
      fetch(`./metrics/${encodeURIComponent(m.id)}`, { headers: { Accept: 'application/json' } })
        .then((r) => r.json())
        .then((res) => {
          if (!cancelled && res && res.data) {
            setData((prev) => ({ ...prev, [m.id]: res.data }));
          }
        })
        .catch(() => {});
    });
    return () => {
      cancelled = true;
    };
  }, [config]);

  const onFrame = useCallback((frame) => {
    if (frame && frame.metric_id && frame.data) {
      setData((prev) => ({ ...prev, [frame.metric_id]: frame.data }));
    }
  }, []);

  const streamStatus = useDashboardStream(slug, onFrame);

  if (configError) {
    return (
      <div className="p-8 text-sm text-red-600 dark:text-red-400">
        Failed to load dashboard config: {configError}
      </div>
    );
  }
  if (!config) {
    return <div className="p-8 text-sm text-slate-500 dark:text-slate-400">Loading dashboard…</div>;
  }

  return (
    <div className="min-h-full bg-slate-50 p-4 dark:bg-slate-950 dark:text-slate-100 sm:p-6">
      <header className="mb-5 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">{config.name}</h1>
          {config.description ? (
            <p className="mt-0.5 text-sm text-slate-500 dark:text-slate-400">{config.description}</p>
          ) : null}
        </div>
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
      </header>

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

      {streamStatus === 'reconnecting' ? (
        <p className="mt-4 text-center text-xs text-slate-400">Reconnecting to live data…</p>
      ) : null}
    </div>
  );
}
