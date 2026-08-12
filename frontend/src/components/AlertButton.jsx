import { Bell } from 'lucide-react';
import { useState, useEffect, useRef } from 'react';
import { getAlerts } from '@/api/alertApi';

/** Alert bell button with unread badge count.
 *
 * Props:
 *  collapsed  - icon-only when sidebar is collapsed
 *  onClick    - opens the ProactiveAlerts panel
 *  panelOpen  - whether the panel is currently visible
 */
export default function AlertButton({ collapsed = false, onClick, panelOpen = false }) {
  const [unreadCount, setUnreadCount] = useState(0);
  const intervalRef = useRef(null);

  // Poll unread count every 2 minutes
  useEffect(() => {
    const fetchCount = async () => {
      try {
        const data = await getAlerts();
        setUnreadCount(data.unread_count || 0);
      } catch {
        // silently fail — badge will just show stale count
      }
    };

    fetchCount();
    intervalRef.current = setInterval(fetchCount, 2 * 60 * 1000);

    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, []);

  // Also refresh count when panel closes (alerts may have been read)
  useEffect(() => {
    if (!panelOpen) {
      (async () => {
        try {
          const data = await getAlerts();
          setUnreadCount(data.unread_count || 0);
        } catch {}
      })();
    }
  }, [panelOpen]);

  const badgeLabel = unreadCount > 9 ? '9+' : String(unreadCount);

  return (
    <button
      onClick={onClick}
      aria-label="Alerts"
      title="Alerts"
      className={`relative flex items-center rounded-md text-muted-foreground transition-colors hover:bg-sidebar-accent hover:text-foreground ${
        collapsed ? 'justify-center p-2' : 'gap-2 px-2 py-1.5 text-xs'
      }`}
    >
      <Bell className="h-4 w-4 shrink-0" />
      {!collapsed && <span>Alerts</span>}

      {/* Badge */}
      {unreadCount > 0 && (
        <span className="absolute -top-0.5 -right-0.5 min-w-[18px] h-[18px] flex items-center justify-center rounded-full bg-red-500 text-white text-[10px] font-bold leading-none px-1 shadow-sm">
          {badgeLabel}
        </span>
      )}
    </button>
  );
}
