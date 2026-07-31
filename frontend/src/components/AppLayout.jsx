import { Outlet } from 'react-router-dom';
import Sidebar from './Sidebar';
import PageErrorBoundary from './PageErrorBoundary';
import { ChatSessionProvider } from '@/lib/ChatSessionContext';

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
  return (
    <ChatSessionProvider>
      <div className="flex h-screen w-full bg-background">
        <Sidebar />
        <main className="h-full flex-1 overflow-hidden">
          <PageErrorBoundary>
            <Outlet />
          </PageErrorBoundary>
        </main>
      </div>
    </ChatSessionProvider>
  );
}