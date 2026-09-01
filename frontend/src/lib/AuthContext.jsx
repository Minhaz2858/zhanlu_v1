import React, { createContext, useState, useContext, useEffect, useRef, useCallback } from 'react';
import { base44 } from '@/api/base44Client';
import { appParams } from '@/lib/app-params';
import { createAxiosClient } from '@base44/sdk/dist/utils/axios-client';
import { rotateAccessToken } from '@/api/tokenRefresh';

const AuthContext = createContext();

/**
 * How often the access token expires (minutes). MUST match the backend
 * setting `ACCESS_TOKEN_EXPIRE_MINUTES` in `backend/app/config.py`.
 * We refresh proactively at ~80 % of this TTL so the user almost never
 * experiences a request-time 401.
 */
const TOKEN_TTL_MIN = 15;
/** Refresh this many ms before TTL — leaves a comfortable margin for clock skew. */
const REFRESH_LEAD_MS = 60 * 1000; // 1 minute
/** Minimum gap between two proactive refreshes (prevents thrash). */
const REFRESH_MIN_GAP_MS = 30 * 1000; // 30 seconds
/** TTL ceiling assumed for tokens issued before we could capture `iat`. */
const SAFE_TTL_MS = TOKEN_TTL_MIN * 60 * 1000;

export const AuthProvider = ({ children }) => {
  const [user, setUser] = useState(null);
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [isLoadingAuth, setIsLoadingAuth] = useState(true);
  const [isLoadingPublicSettings, setIsLoadingPublicSettings] = useState(true);
  const [authError, setAuthError] = useState(null);
  const [authChecked, setAuthChecked] = useState(false);
  const [appPublicSettings, setAppPublicSettings] = useState(null); // Contains only { id, public_settings }

  // Proactive-refresh plumbing. We hold the last successful refresh
  // timestamp + a timer handle in a ref so re-renders don't reset them.
  const lastRefreshAtRef = useRef(0);
  const proactiveTimerRef = useRef(null);

  const scheduleProactiveRefresh = useCallback(() => {
    if (proactiveTimerRef.current) clearTimeout(proactiveTimerRef.current);
    // Wait 80 % of TTL (or 30 s, whichever is longer) since the last refresh.
    const sinceLast = Date.now() - lastRefreshAtRef.current;
    const ttlBudget = Math.max(REFRESH_MIN_GAP_MS, SAFE_TTL_MS * 0.8 - REFRESH_LEAD_MS);
    const wait = Math.max(0, ttlBudget - sinceLast);
    proactiveTimerRef.current = setTimeout(async () => {
      try {
        const tok = await rotateAccessToken();
        if (tok) {
          lastRefreshAtRef.current = Date.now();
          scheduleProactiveRefresh();
        }
      } catch {
        /* keep silent — refresh failures shouldn't spam the console every 12 min */
      }
    }, wait);
  }, []);

  useEffect(() => {
    checkAppState();
    // Also refresh on tab focus / visibility return so a long-idle tab
    // doesn't get caught by TTL once the user comes back. The proactive
    // timer alone wouldn't fire if the tab was throttled to background.
    const onFocus = () => {
      const sinceLast = Date.now() - lastRefreshAtRef.current;
      // Only refresh if the token is more than half-stale; otherwise the
      // routine timer will handle it.
      if (sinceLast > SAFE_TTL_MS * 0.5) {
        rotateAccessToken()
          .then((tok) => {
            if (tok) {
              lastRefreshAtRef.current = Date.now();
              scheduleProactiveRefresh();
            }
          })
          .catch(() => {});
      }
    };
    window.addEventListener('focus', onFocus);
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') onFocus();
    });
    return () => {
      window.removeEventListener('focus', onFocus);
      if (proactiveTimerRef.current) clearTimeout(proactiveTimerRef.current);
    };
    // scheduleProactiveRefresh is stable (useCallback, no deps).
  }, [scheduleProactiveRefresh]);

  const checkAppState = async () => {
    try {
      setIsLoadingPublicSettings(true);
      setAuthError(null);

      // First, check app public settings (with token if available)
      // This will tell us if auth is required, user not registered, etc.
      const appClient = createAxiosClient({
        baseURL: `/api/apps/public`,
        headers: {
          'X-App-Id': appParams.appId
        },
        token: appParams.token, // Include token if available
        interceptResponses: true
      });

      try {
        const publicSettings = await appClient.get(`/prod/public-settings/by-id/${appParams.appId}`);
        setAppPublicSettings(publicSettings);

        // If we got the app public settings successfully, check if user is authenticated
        if (appParams.token) {
          await checkUserAuth();
        } else {
          setIsLoadingAuth(false);
          setIsAuthenticated(false);
          setAuthChecked(true);
        }
        setIsLoadingPublicSettings(false);
      } catch (appError) {
        console.error('App state check failed:', appError);

        // Handle app-level errors
        if (appError.status === 403 && appError.data?.extra_data?.reason) {
          const reason = appError.data.extra_data.reason;
          if (reason === 'auth_required') {
            setAuthError({
              type: 'auth_required',
              message: 'Authentication required'
            });
          } else if (reason === 'user_not_registered') {
            setAuthError({
              type: 'user_not_registered',
              message: 'User not registered for this app'
            });
          } else {
            setAuthError({
              type: reason,
              message: appError.message
            });
          }
        } else {
          setAuthError({
            type: 'unknown',
            message: appError.message || 'Failed to load app'
          });
        }
        setIsLoadingPublicSettings(false);
        setIsLoadingAuth(false);
        setAuthChecked(true);
      }
    } catch (error) {
      console.error('Unexpected error:', error);
      setAuthError({
        type: 'unknown',
        message: error.message || 'An unexpected error occurred'
      });
      setIsLoadingPublicSettings(false);
      setIsLoadingAuth(false);
      setAuthChecked(true);
    }
  };

  const checkUserAuth = async () => {
    try {
      setIsLoadingAuth(true);
      const currentUser = await base44.auth.me();
      setUser(currentUser);
      setIsAuthenticated(true);
      setIsLoadingAuth(false);
      setAuthChecked(true);
      // We've just proved the access token is fresh — start the proactive
      // refresh scheduler so future sessions don't have to lean on a 401
      // burst for self-healing.
      lastRefreshAtRef.current = Date.now();
      scheduleProactiveRefresh();
    } catch (error) {
      // Expired access token? Try a silent refresh once, then retry me().
      // The Proxy in base44Client.js wraps `auth.me()` too, so this branch
      // only fires for non-401 errors (which we treat as terminal) — the
      // 401 path is already handled by the Proxy.
      if (error.status === 401 || error.status === 403) {
        const newToken = await rotateAccessToken();
        if (newToken) {
          try {
            const currentUser = await base44.auth.me();
            setUser(currentUser);
            setIsAuthenticated(true);
            setIsLoadingAuth(false);
            setAuthChecked(true);
            lastRefreshAtRef.current = Date.now();
            scheduleProactiveRefresh();
            return;
          } catch (e2) {
            console.error('User auth check failed after refresh:', e2);
          }
        }
      } else {
        console.error('User auth check failed:', error);
      }
      setIsLoadingAuth(false);
      setIsAuthenticated(false);
      setAuthChecked(true);
      setAuthError({ type: 'auth_required', message: 'Authentication required' });
    }
  };

  const logout = async (shouldRedirect = true) => {
    setUser(null);
    setIsAuthenticated(false);
    // Stop the proactive refresh loop so we don't keep hitting /auth/refresh
    // after the user explicitly signed out.
    if (proactiveTimerRef.current) {
      clearTimeout(proactiveTimerRef.current);
      proactiveTimerRef.current = null;
    }
    // Best-effort server-side revocation (POST /auth/logout) so the access
    // token's JTI is blacklisted + all refresh tokens invalidated. Uses the
    // current access token from storage; failures are non-fatal (local
    // cleanup still proceeds).
    try {
      const accessToken = localStorage.getItem('base44_access_token') || localStorage.getItem('token');
      if (accessToken) {
        await fetch(`/api/apps/${appParams.appId}/auth/logout`, {
          method: 'POST',
          headers: { Authorization: `Bearer ${accessToken}` },
        });
      }
    } catch (e) {
      console.error('Server-side logout failed:', e);
    }
    // Clear all tokens from localStorage. We do NOT call base44.auth.logout()
    // because the SDK constructs its redirect URL using appBaseUrl (which
    // points at the API, not the frontend), causing the browser to land on
    // http://localhost:5002 instead of the login page.
    localStorage.removeItem('base44_access_token');
    localStorage.removeItem('token');
    localStorage.removeItem('refresh_token');
    if (shouldRedirect) {
      window.location.href = '/login?next=%2F';
    }
  };

  const navigateToLogin = () => {
    // Use the SDK's redirectToLogin method
    base44.auth.redirectToLogin(window.location.href);
  };

  return (
    <AuthContext.Provider value={{
      user,
      isAuthenticated,
      isLoadingAuth,
      isLoadingPublicSettings,
      authError,
      appPublicSettings,
      authChecked,
      isAdmin: user?.role === 'admin',
      // Whether open self-registration is enabled. Defaults to true when the
      // field is absent so existing deployments keep working.
      allowPublicRegistration: appPublicSettings?.public_settings?.allow_public_registration ?? true,
      logout,
      navigateToLogin,
      checkUserAuth,
      checkAppState
    }}>
      {children}
    </AuthContext.Provider>
  );
};

export const useAuth = () => {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
};
