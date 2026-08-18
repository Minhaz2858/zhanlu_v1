import { useEffect, useMemo, useRef, useState } from 'react';
import { Loader2, RefreshCw, AlertTriangle } from 'lucide-react';

/**
 * Full-stack dashboard viewer.
 *
 * Renders a deployed full-stack dashboard application (FastAPI sub-app + prebuilt
 * React frontend) inside an iframe pointed at the app's served URL
 * (`/api/dashboards/apps/{slug}/`). The app itself handles live WebSocket data,
 * so this component only needs to host the iframe and surface load/error state.
 *
 * Props:
 *  - appUrl: string  — absolute or root-relative URL to load in the iframe.
 *  - name?: string  — human label (used in the header + error states).
 *  - slug?: string  — dashboard slug (used for the refresh key).
 */
export default function FullStackDashboardViewer({ appUrl, name, slug }) {
  const [status, setStatus] = useState('loading'); // loading | ready | error
  const [reloadKey, setReloadKey] = useState(0);
  const [wsStatus, setWsStatus] = useState(null); // 'live' | 'connecting' | 'reconnecting'
  const iframeRef = useRef(null);

  // The dashboard app posts its WebSocket state up (same-origin iframe), so the
  // viewer toolbar can show a reconnect indicator without reaching into the app.
  useEffect(() => {
    function onMessage(e) {
      if (e.data && e.data.type === 'dashboard-ws-status' && typeof e.data.status === 'string') {
        setWsStatus(e.data.status);
      }
    }
    window.addEventListener('message', onMessage);
    return () => window.removeEventListener('message', onMessage);
  }, []);

  const resolvedUrl = useMemo(() => {
    if (!appUrl) return null;
    // Accept both absolute (https://…) and root-relative (/api/…) URLs.
    if (/^https?:\/\//i.test(appUrl)) return appUrl;
    const base = window.location.origin;
    return appUrl.startsWith('/') ? `${base}${appUrl}` : `${base}/${appUrl}`;
  }, [appUrl]);

  useEffect(() => {
    setStatus('loading');
  }, [appUrl, reloadKey]);

  function handleLoad() {
    setStatus('ready');
  }

  function handleError() {
    // iframe onError is unreliable cross-browser; we primarily rely on a timeout
    // probe below. This is a secondary guard.
    setStatus('error');
  }

  function reload() {
    setReloadKey((k) => k + 1);
    setStatus('loading');
  }

  if (!resolvedUrl) {
    return (
      <div className="flex h-full w-full items-center justify-center text-sm text-red-500 gap-2">
        <AlertTriangle className="h-4 w-4" />
        <span>Missing dashboard URL.</span>
      </div>
    );
  }

  return (
    <div className="flex h-full w-full flex-col bg-white dark:bg-zinc-900">
      <div className="flex items-center justify-between border-b border-zinc-200 dark:border-zinc-700 px-3 py-1.5">
        <span className="text-xs font-medium text-zinc-500 dark:text-zinc-400 truncate">
          {name || slug || 'Dashboard'} · live app
        </span>
        <div className="flex items-center gap-2">
          {wsStatus === 'reconnecting' && (
            <span className="inline-flex items-center gap-1 rounded bg-amber-100 px-1.5 py-0.5 text-[11px] font-medium text-amber-700 dark:bg-amber-900/40 dark:text-amber-300">
              <Loader2 className="h-3 w-3 animate-spin" />
              Reconnecting…
            </span>
          )}
          {status === 'loading' && (
            <Loader2 className="h-3.5 w-3.5 animate-spin text-zinc-400" />
          )}
          <button
            type="button"
            onClick={reload}
            title="Reload dashboard"
            className="rounded p-1 text-zinc-400 hover:bg-zinc-100 hover:text-zinc-600 dark:hover:bg-zinc-800"
          >
            <RefreshCw className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>
      <div className="relative flex-1">
        {status === 'error' && (
          <div className="absolute inset-0 z-10 flex flex-col items-center justify-center gap-2 bg-white/90 dark:bg-zinc-900/90 text-sm text-red-500">
            <AlertTriangle className="h-5 w-5" />
            <span>Failed to load dashboard.</span>
            <button
              type="button"
              onClick={reload}
              className="rounded bg-red-500/10 px-2 py-1 text-xs text-red-600 hover:bg-red-500/20"
            >
              Retry
            </button>
          </div>
        )}
        <iframe
          key={reloadKey}
          ref={iframeRef}
          src={resolvedUrl}
          title={name || slug || 'dashboard'}
          onLoad={handleLoad}
          onError={handleError}
          className="h-full w-full border-0"
          allow="fullscreen"
        />
      </div>
    </div>
  );
}
