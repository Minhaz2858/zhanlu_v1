import { useEffect, useRef, useState } from 'react';

/** Derive the dashboard slug from the current URL: /api/dashboards/apps/{slug}/ */
export function deriveSlug() {
  const m = window.location.pathname.match(/\/api\/dashboards\/apps\/([^/]+)/);
  return m ? m[1] : null;
}

/**
 * Live-data WebSocket hook.
 * Reconnects with exponential backoff. Each frame is {metric_id, title, data}
 * where data = {columns, rows, error, truncated}.
 */
export function useDashboardStream(slug, onFrame) {
  const [status, setStatus] = useState('connecting');
  const cbRef = useRef(onFrame);
  cbRef.current = onFrame;

  useEffect(() => {
    if (!slug) return undefined;
    let ws;
    let retries = 0;
    let closed = false;

    const connect = () => {
      if (closed) return;
      const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
      ws = new WebSocket(`${proto}://${window.location.host}/api/dashboards/apps/${slug}/ws`);
      ws.onopen = () => {
        retries = 0;
        setStatus('live');
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
