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

// Secondary routes are code-split so they don't bloat the initial bundle.
// Each becomes its own lazy chunk, downloaded on first navigation. Chat.jsx
// alone is ~124 KB, and the marketplace/toolkit/builder pages pull heavy
// deps (recharts, react-quill, three) that most users never touch on load.
const AutomationTasks = lazy(() => import('@/pages/AutomationTasks'));
const AutomationTaskDetail = lazy(() => import('@/pages/AutomationTaskDetail'));
const MySpace = lazy(() => import('@/pages/MySpace'));
const ProjectDetail = lazy(() => import('@/pages/ProjectDetail'));
const Market = lazy(() => import('@/pages/Market'));
const MarketAgentDetail = lazy(() => import('@/pages/MarketAgentDetail'));
const Toolkit = lazy(() => import('@/pages/Toolkit'));
const ToolDetail = lazy(() => import('@/pages/ToolDetail'));
const SkillAgent = lazy(() => import('@/pages/SkillAgent'));
const SkillExecutions = lazy(() => import('@/pages/SkillExecutions'));
const AgentBuilder = lazy(() => import('@/pages/AgentBuilder'));
const Settings = lazy(() => import('@/pages/Settings'));
const ResourceDetail = lazy(() => import('@/pages/ResourceDetail'));
const AgentConfig = lazy(() => import('@/pages/AgentConfig'));
const UITest = lazy(() => import('@/pages/UITest'));
const DashboardView = lazy(() => import('@/pages/DashboardView'));
const MarketDashboardPage = lazy(() => import('@/pages/MarketDashboard/MarketDashboardPage'));

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

  // Render the main app
  return (
    <LanguageProvider>
    <Suspense fallback={<RouteFallback />}>
    <Routes>
      {/* Auth pages — use their own AuthLayout, not the app shell. These
          must be registered so the SDK's redirectToLogin() (→ /login) and
          the Register/ForgotPassword links work. Previously missing, which
          meant a Guest could never reach the login page. */}
      <Route path="/login" element={<Login />} />
      <Route path="/register" element={<Register />} />
      <Route path="/forgot-password" element={<ForgotPassword />} />
      <Route path="/reset-password" element={<ResetPassword />} />
      <Route element={<ProtectedRoute />}>
        <Route element={<AppLayout />}>
          <Route path="/" element={<Chat />} />
          <Route path="/chat" element={<Chat />} />
          <Route path="/automation" element={<AutomationTasks />} />
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
          <Route path="/dashboard/:id" element={<DashboardView />} />
          <Route path="/market-dashboard/:section" element={<MarketDashboardPage />} />
        </Route>
      </Route>
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
        <Router>
          <ScrollToTop />
          <AppRouter />
        </Router>
        <Toaster />
      </QueryClientProvider>
    </AuthProvider>
  )
}

export default App
