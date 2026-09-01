import { useState, useEffect } from 'react';

/**
 * Tracks browser online/offline status using navigator.onLine + events.
 * Returns { isOnline, wasOffline } so the UI can show "Reconnecting..." states.
 */
export default function useOnlineStatus() {
  const [isOnline, setIsOnline] = useState(
    typeof navigator !== 'undefined' ? navigator.onLine : true
  );
  const [wasOffline, setWasOffline] = useState(false);

  useEffect(() => {
    const goOnline = () => {
      setIsOnline(true);
      // Keep wasOffline true for 3s so the UI can show "Back online" briefly
      setTimeout(() => setWasOffline(false), 3000);
    };
    const goOffline = () => {
      setIsOnline(false);
      setWasOffline(true);
    };

    window.addEventListener('online', goOnline);
    window.addEventListener('offline', goOffline);
    return () => {
      window.removeEventListener('online', goOnline);
      window.removeEventListener('offline', goOffline);
    };
  }, []);

  return { isOnline, wasOffline };
}
