import { useEffect, useRef, useState } from 'react';

/** Derive the dashboard slug from the current URL: /api/dashboards/apps/{slug}/ */
export function deriveSlug() {
  const m = window.location.pathname.match(/\/api\/dashboards\/apps\/([^/]+)/);
  return m ? m[1] : null;
}

/**
 * Session access token for dashboard data calls (REST + WS).
 *
 * Same-origin iframe ⇒ localStorage is shared with the main app, whose
 * AuthContext stores the access token under 'base44_access_token' (legacy
 * fallback: 'token'). The backend's dashboard endpoints accept this token as
 * a ?token= query param (Authorization headers cannot be set on subresource
 * fetches from a plain iframe, and WS can't set headers at all).
 *
 * 2026-09-02: extracted to a shared helper so the config.json / metrics REST
 * fetches carry the SAME credential the WS handshake already used — this is
 * what unlocks DASHBOARD_ENDPOINT_AUTH_ENABLED (#7).
 */
export function dashboardAccessToken() {
  try {
    return (
      window.localStorage.getItem('base44_access_token') ||
      window.localStorage.getItem('token') ||
      ''
    );
  } catch {
    return ''; // storage disabled (private mode / sandbox) → anonymous calls
  }
}

/** Query-string suffix ('' or '?token=…') for dashboard data fetches. */
export function tokenQuery() {
  const token = dashboardAccessToken();
  return token ? `?token=${encodeURIComponent(token)}` : '';
}

/**
 * Live-data WebSocket hook.
 * Reconnects with exponential backoff. Each frame is {metric_id, title, data}
 * where data = {columns, rows, error, truncated}.
 *
 * Auth: the backend rejects anonymous WebSockets, so the session access token
 * is appended as a query param (?token=…) — browser WebSockets can't set
 * headers. Same-origin iframe ⇒ localStorage is shared with the main app.
 *
 * onReconnect fires after a successful RE-connect (not the first open) so the
 * caller can refetch a fresh full snapshot before trusting live frames again.
 */
export function useDashboardStream(slug, onFrame, onReconnect) {
  const [status, setStatus] = useState('connecting');
  const cbRef = useRef(onFrame);
  cbRef.current = onFrame;
  const rcRef = useRef(onReconnect);
  rcRef.current = onReconnect;

  useEffect(() => {
    if (!slug) return undefined;
    let ws;
    let retries = 0;
    let closed = false;

    const connect = () => {
      if (closed) return;
      const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
      const token = dashboardAccessToken();
      const q = token ? `?token=${encodeURIComponent(token)}` : '';
      ws = new WebSocket(`${proto}://${window.location.host}/api/dashboards/apps/${slug}/ws${q}`);
      ws.onopen = () => {
        const reconnected = retries > 0;
        retries = 0;
        setStatus('live');
        if (reconnected) rcRef.current?.();
      };
      ws.onmessage = (ev) => {
        try {
          cbRef.current(JSON.parse(ev.data));
        } catch {
          /* ignore malformed frame */
        }
      };
      ws.onclose = () => {
        if (closed) return;
        setStatus('reconnecting');
        const delay = Math.min(2000 * 2 ** retries, 15000);
        retries += 1;
        setTimeout(() => {
          if (!closed) connect();
        }, delay);
      };
      ws.onerror = () => {
        try {
          ws.close();
        } catch {
          /* noop */
        }
      };
    };

    connect();
    return () => {
      closed = true;
      try {
        ws?.close();
      } catch {
        /* noop */
      }
    };
  }, [slug]);

  return status;
}
