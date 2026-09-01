import { Outlet } from 'react-router-dom';
import { ChatSessionProvider } from '@/lib/ChatSessionContext';
import BottomTabBar from './BottomTabBar';

/**
 * MobileLayout — the mobile app shell.
 *
 * A full-height flex column: the routed page renders its own header +
 * content (via Outlet), and the fixed BottomTabBar sits at the bottom.
 * The ChatSessionProvider is mounted here (mirroring AppLayout on
 * desktop) so session state survives route changes between 聊天 and
 * 我的空间.
 */
export default function MobileLayout() {
  return (
    <ChatSessionProvider>
      {/* h-full: the enclosing MobileFrame decides the height (100dvh on a
          real phone, a fixed 430×860 frame in desktop forced-mode). */}
      <div className="flex h-full w-full flex-col bg-background">
        <div className="min-h-0 flex-1 overflow-hidden">
          <Outlet />
        </div>
        <BottomTabBar />
      </div>
    </ChatSessionProvider>
  );
}
