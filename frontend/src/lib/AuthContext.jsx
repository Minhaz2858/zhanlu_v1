import React, { createContext, useState, useContext, useEffect } from 'react';
import { base44 } from '@/api/base44Client';
import { appParams } from '@/lib/app-params';
import { createAxiosClient } from '@base44/sdk/dist/utils/axios-client';

const AuthContext = createContext();

export const AuthProvider = ({ children }) => {
  const [user, setUser] = useState(null);
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [isLoadingAuth, setIsLoadingAuth] = useState(true);
  const [isLoadingPublicSettings, setIsLoadingPublicSettings] = useState(true);
  const [authError, setAuthError] = useState(null);
  const [authChecked, setAuthChecked] = useState(false);
  const [appPublicSettings, setAppPublicSettings] = useState(null); // Contains only { id, public_settings }

  useEffect(() => {
    checkAppState();
  }, []);

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
      }
    } catch (error) {
      console.error('Unexpected error:', error);
      setAuthError({
        type: 'unknown',
        message: error.message || 'An unexpected error occurred'
      });
      setIsLoadingPublicSettings(false);
      setIsLoadingAuth(false);
    }
  };

  const refreshAccessToken = async () => {
    // Try to rotate the refresh token. Returns the new access token on success
    // (also persists both new tokens), or null if there's no refresh token or
    // the rotation failed — caller must then treat the session as expired.
    const refreshToken = localStorage.getItem('refresh_token');
    if (!refreshToken) return null;
    try {
      const resp = await fetch(`/api/apps/${appParams.appId}/auth/refresh`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: refreshToken }),
      });
      if (!resp.ok) return null;
      const data = await resp.json();
      base44.auth.setToken(data.access_token);
      localStorage.setItem('refresh_token', data.refresh_token);
      return data.access_token;
    } catch {
      return null;
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
    } catch (error) {
      // Expired access token? Try a silent refresh once, then retry me().
      if (error.status === 401 || error.status === 403) {
        const newToken = await refreshAccessToken();
        if (newToken) {
          try {
            const currentUser = await base44.auth.me();
            setUser(currentUser);
            setIsAuthenticated(true);
            setIsLoadingAuth(false);
            setAuthChecked(true);
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
