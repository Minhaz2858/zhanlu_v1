import { Outlet } from 'react-router-dom';
import Sidebar from './Sidebar';
import PageErrorBoundary from './PageErrorBoundary';
import AutomationExecutionStatusDrawer from './automation/AutomationExecutionStatusDrawer';
import { ChatSessionProvider } from '@/lib/ChatSessionContext';
import { PersistentStreamProvider } from '@/lib/PersistentStreamContext';
import { useScreenSize } from '@/hooks/useScreenSize';

/**
 * AppLayout — top-level chrome for every route.
 *
 * Owns the ChatSessionProvider (Option A: unified sidebar). The
 * provider owns the chat session list + active selection so the
 * unified sidebar (in `Sidebar.jsx`) and the chat page can both
 * read/write the same state. Mounting the provider at the layout
 * level — instead of inside `Chat.jsx` — keeps the state alive as
 * the user navigates between routes (so the sidebar's session list
 * doesn't re-fetch on every route change).
 *
 * The PageErrorBoundary wraps the route Outlet so that a render
 * error on any single page (e.g. a legacy row with capabilities
 * stored as a string instead of an array, which used to unmount
 * the entire React tree) is caught here and shows a recoverable
 * fallback UI rather than the "blank page" symptom.
 */
export default function AppLayout() {
  const { settings } = useScreenSize();
  // wide/ultra 档给内容区一个最大宽度并水平居中，避免 2K/4K 下页面
  // 过宽、文本行过长；compact/standard 档 contentMaxWidth 为 null，
  // 等价于原有行为（不限宽）。compact 档额外收窄左右内边距，让内容
  // 贴边不空旷。内联 style 承载动态像素值。
  const contentMaxWidth = settings.contentMaxWidth;
  const contentPadding = settings.contentPadding ?? 0;

  return (
    <PersistentStreamProvider>
      <ChatSessionProvider>
        <div className="flex h-screen w-full bg-background">
          <Sidebar />
          <main className="h-full flex-1 overflow-hidden">
            <div
              className={contentMaxWidth ? 'mx-auto h-full' : 'h-full'}
              style={{
                ...(contentMaxWidth ? { maxWidth: contentMaxWidth } : {}),
                ...(contentPadding ? { paddingLeft: contentPadding, paddingRight: contentPadding } : {}),
              }}
            >
              <PageErrorBoundary>
                <Outlet />
              </PageErrorBoundary>
            </div>
          </main>
          <AutomationExecutionStatusDrawer />
        </div>
      </ChatSessionProvider>
    </PersistentStreamProvider>
  );
}