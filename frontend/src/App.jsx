import { lazy, Suspense } from 'react';
import { Toaster } from "@/components/ui/toaster"
import { QueryClientProvider } from '@tanstack/react-query'
import { queryClientInstance } from '@/lib/query-client'
import { BrowserRouter as Router, Route, Routes, useLocation } from 'react-router-dom';
import PageNotFound from './lib/PageNotFound';
import { AuthProvider, useAuth } from '@/lib/AuthContext';
import UserNotRegisteredError from '@/components/UserNotRegisteredError';
import ScrollToTop from './components/ScrollToTop';
import ProtectedRoute from '@/components/ProtectedRoute';
// Layout + primary view stay eager so the first paint is instant and the auth
// shell (login/redirect) never waits on a chunk download.
import AppLayout from '@/components/AppLayout';
import Chat from '@/pages/Chat';
import Login from '@/pages/Login';
import Register from '@/pages/Register';
import ForgotPassword from '@/pages/ForgotPassword';
import ResetPassword from '@/pages/ResetPassword';
import { LanguageProvider } from '@/lib/LanguageProvider';
import useIsMobile from '@/hooks/useIsMobile';
import { MobileModeProvider, useMobileMode } from '@/lib/mobileMode';
import MobileFrame from '@/components/MobileFrame';
import MobileLayout from '@/components/mobile/MobileLayout';
import MobileDetailShell from '@/components/mobile/MobileDetailShell';
import MobileChatPage from '@/pages/mobile/MobileChatPage';
import MobileMySpacePage from '@/pages/mobile/MobileMySpacePage';
import MobileMyFilesPage from '@/pages/mobile/MobileMyFilesPage';

// Secondary routes are code-split so they don't bloat the initial bundle.
// Each becomes its own lazy chunk, downloaded on first navigation. Chat.jsx
// alone is ~124 KB, and the marketplace/toolkit/builder pages pull heavy
// deps (recharts, react-quill, three) that most users never touch on load.
const AutomationTasks = lazy(() => import('@/pages/AutomationTasks'));
const AutomationTaskDetail = lazy(() => import('@/pages/AutomationTaskDetail'));
const MySpace = lazy(() => import('@/pages/MySpace'));
const MyFilesPage = lazy(() => import('@/pages/MyFilesPage'));
const FromPersonalPage = lazy(() => import('@/pages/FromPersonalPage'));
const FromCompanyPage = lazy(() => import('@/pages/FromCompanyPage'));
const ProjectDetail = lazy(() => import('@/pages/ProjectDetail'));
const Market = lazy(() => import('@/pages/Market'));
const MarketAgentDetail = lazy(() => import('@/pages/MarketAgentDetail'));
const Toolkit = lazy(() => import('@/pages/Toolkit'));
const ToolDetail = lazy(() => import('@/pages/ToolDetail'));
const SkillAgent = lazy(() => import('@/pages/SkillAgent'));
const SkillExecutions = lazy(() => import('@/pages/SkillExecutions'));
const AgentBuilder = lazy(() => import('@/pages/AgentBuilder'));
const Settings = lazy(() => import('@/pages/Settings'));
const AdminUsers = lazy(() => import('@/pages/AdminUsers'));
const AdminObservability = lazy(() => import('@/pages/AdminObservability'));
const ResourceDetail = lazy(() => import('@/pages/ResourceDetail'));
const AgentConfig = lazy(() => import('@/pages/AgentConfig'));
const DashboardView = lazy(() => import('@/pages/DashboardView'));
const UITest = lazy(() => import('@/pages/UITest'));

// Shared fallback for any suspended lazy route — matches the app's loading
// spinner so navigation feels seamless instead of flashing blank content.
function RouteFallback() {
  return (
    <div className="fixed inset-0 flex items-center justify-center">
      <div className="w-8 h-8 border-4 border-slate-200 border-t-slate-800 rounded-full animate-spin"></div>
    </div>
  );
}

const AuthenticatedApp = () => {
  const { isLoadingAuth, isLoadingPublicSettings, authError } = useAuth();

  // Show loading spinner while checking app public settings or auth
  if (isLoadingPublicSettings || isLoadingAuth) {
    return (
      <div className="fixed inset-0 flex items-center justify-center">
        <div className="w-8 h-8 border-4 border-slate-200 border-t-slate-800 rounded-full animate-spin"></div>
      </div>
    );
  }

  // Handle authentication errors. "auth_required" is now handled at the route
  // level by <ProtectedRoute> (redirects to /login?next=…), so here we only
  // special-case the "user not registered" full-page error.
  if (authError && authError.type === 'user_not_registered') {
    return <UserNotRegisteredError />;
  }

  // Mobile device detection: a genuine phone (width ≤ 1024 + touch) OR a
  // desktop user who explicitly forced the mobile UI for debugging. On
  // mobile we render a separate, lightweight route tree (MobileLayout +
  // bottom tabs + phone frame) instead of the desktop AppLayout + sidebar.
  const isMobileDevice = useIsMobile();
  const { forceMobile } = useMobileMode();
  const isMobile = isMobileDevice || forceMobile;

  // Auth pages are shared across both route trees.
  const authRoutes = (
    <>
      <Route path="/login" element={<Login />} />
      <Route path="/register" element={<Register />} />
      <Route path="/forgot-password" element={<ForgotPassword />} />
      <Route path="/reset-password" element={<ResetPassword />} />
    </>
  );

  const mobileRoutes = (
    <Route element={<ProtectedRoute />}>
      <Route element={<MobileFrame />}>
        <Route element={<MobileLayout />}>
          <Route path="/" element={<MobileChatPage />} />
          <Route path="/chat" element={<MobileChatPage />} />
          <Route path="/automation/chat" element={<MobileChatPage />} />
          <Route path="/my-space" element={<MobileMySpacePage />} />
          <Route path="/my-files" element={<MobileMyFilesPage />} />
          {/* Deep-linked detail routes — reuse the desktop pages inside a
              mobile shell (back bar + scroll region) so phones don't fall
              through to the 404 page. Kept inside MobileLayout so they
              share the ChatSessionProvider. */}
          <Route
            path="/my-space/project/:id"
            element={
              <MobileDetailShell>
                <ProjectDetail />
              </MobileDetailShell>
            }
          />
          <Route
            path="/my-space/agent/:id"
            element={
              <MobileDetailShell>
                <AgentConfig />
              </MobileDetailShell>
            }
          />
          <Route
            path="/my-space/:type/:id"
            element={
              <MobileDetailShell>
                <ResourceDetail />
              </MobileDetailShell>
            }
          />
        </Route>
      </Route>
    </Route>
  );

  const desktopRoutes = (
    <Route element={<ProtectedRoute />}>
      <Route element={<AppLayout />}>
        <Route path="/" element={<Chat />} />
        <Route path="/chat" element={<Chat />} />
        <Route path="/automation" element={<AutomationTasks />} />
        <Route path="/automation/chat" element={<Chat />} />
        <Route path="/automation/:id" element={<AutomationTaskDetail />} />
        <Route path="/my-space" element={<MySpace />} />
        <Route path="/my-space/project/:id" element={<ProjectDetail />} />
        <Route path="/my-space/agent/:id" element={<AgentConfig />} />
        <Route path="/my-space/:type/:id" element={<ResourceDetail />} />
        <Route path="/market" element={<Market />} />
        <Route path="/market/:id" element={<MarketAgentDetail />} />
        <Route path="/toolkit" element={<Toolkit />} />
        <Route path="/toolkit/:id" element={<ToolDetail />} />
        <Route path="/skill-agent" element={<SkillAgent />} />
        <Route path="/skills/executions" element={<SkillExecutions />} />
        <Route path="/agent-builder" element={<AgentBuilder />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="/admin/users" element={<AdminUsers />} />
        <Route path="/admin/observability" element={<AdminObservability />} />
        <Route path="/dashboard/:id" element={<DashboardView />} />
        <Route path="/my-files" element={<MyFilesPage />} />
        <Route path="/from-personal" element={<FromPersonalPage />} />
        <Route path="/from-company" element={<FromCompanyPage />} />
      </Route>
    </Route>
  );

  return (
    <LanguageProvider>
      <Suspense fallback={<RouteFallback />}>
        <Routes>
          {authRoutes}
          {isMobile ? mobileRoutes : desktopRoutes}
          <Route path="*" element={<PageNotFound />} />
        </Routes>
      </Suspense>
    </LanguageProvider>
  );
};


// /ui-test bypasses auth + layout for component smoke-testing
function AppRouter() {
  const location = useLocation();
  if (location.pathname.startsWith('/ui-test')) {
    return (
      <Suspense fallback={<RouteFallback />}>
        <Routes>
          <Route path="/ui-test" element={<UITest />} />
        </Routes>
      </Suspense>
    );
  }
  return <AuthenticatedApp />;
}

function App() {

  return (
    <AuthProvider>
      <QueryClientProvider client={queryClientInstance}>
        <MobileModeProvider>
          <Router>
            <ScrollToTop />
            <AppRouter />
          </Router>
          <Toaster />
        </MobileModeProvider>
      </QueryClientProvider>
    </AuthProvider>
  )
}

export default App
