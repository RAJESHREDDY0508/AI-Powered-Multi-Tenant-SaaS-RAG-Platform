/**
 * Auth Store — Zustand
 *
 * Access token is stored IN MEMORY ONLY (never localStorage or sessionStorage).
 * This is the recommended pattern for SPAs with httpOnly refresh cookies:
 *   - Access token: short-lived (15 min), in Zustand memory
 *   - Refresh token: long-lived (7 days), in httpOnly cookie (set by /api/auth/login BFF)
 *   - On page reload: AuthProvider calls serverRefresh() to get a new access token
 */

import { create } from 'zustand';
import { immer } from 'zustand/middleware/immer';
import type { AuthUser } from '@/types/auth';
import { extractUser, serverRefresh, serverLogout } from '@/lib/auth';
import { configureApiClient } from '@/lib/api';

// ---------------------------------------------------------------------------
// State shape
// ---------------------------------------------------------------------------
interface AuthStore {
  user:        AuthUser | null;
  accessToken: string | null;
  isLoading:   boolean;
  error:       string | null;

  // Actions
  setTokens:    (accessToken: string) => void;
  clearAuth:    () => Promise<void>;
  refresh:      () => Promise<boolean>;
  initialize:   () => Promise<void>;
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------
export const useAuthStore = create<AuthStore>()(
  immer((set, get) => ({
    user:        null,
    accessToken: null,
    isLoading:   true,
    error:       null,

    setTokens: (accessToken: string) => {
      const user = extractUser(accessToken);
      set((s) => {
        s.accessToken = accessToken;
        s.user        = user;
        s.error       = null;
      });
    },

    clearAuth: async () => {
      set((s) => {
        s.accessToken = null;
        s.user        = null;
      });
      await serverLogout();    // clears httpOnly cookie
    },

    refresh: async (): Promise<boolean> => {
      const newToken = await serverRefresh();
      if (newToken) {
        get().setTokens(newToken);
        return true;
      }
      set((s) => { s.accessToken = null; s.user = null; });
      return false;
    },

    initialize: async () => {
      set((s) => { s.isLoading = true; });
      try {
        const newToken = await serverRefresh();
        if (newToken) {
          get().setTokens(newToken);
        }
      } catch {
        // No valid refresh token — user must log in
      } finally {
        set((s) => { s.isLoading = false; });
      }

      // Wire the Axios client AFTER we have the store reference
      configureApiClient({
        getToken: () => get().accessToken,
        refresh:  async () => {
          const ok = await get().refresh();
          return ok ? get().accessToken : null;
        },
        logout: () => { void get().clearAuth(); },
      });
    },
  }))
);

// Convenience selectors (avoid re-renders from selecting entire store)
export const selectUser        = (s: AuthStore) => s.user;
export const selectAccessToken = (s: AuthStore) => s.accessToken;
export const selectIsLoading   = (s: AuthStore) => s.isLoading;
export const selectIsAuth      = (s: AuthStore) => !!s.accessToken && !!s.user;
