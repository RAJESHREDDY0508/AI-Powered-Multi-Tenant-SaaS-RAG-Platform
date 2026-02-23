/**
 * useAuth — auth state + login/logout actions for UI components
 */

'use client';

import { useCallback } from 'react';
import { useRouter } from 'next/navigation';
import toast from 'react-hot-toast';

import { useAuthStore } from '@/store/authStore';
import { serverLogin } from '@/lib/auth';
import { ROUTES } from '@/lib/constants';
import type { UserRole } from '@/types/auth';
import { hasPermission } from '@/types/auth';

export function useAuth() {
  const router     = useRouter();
  const user       = useAuthStore((s) => s.user);
  const isLoading  = useAuthStore((s) => s.isLoading);
  const setTokens  = useAuthStore((s) => s.setTokens);
  const clearAuth  = useAuthStore((s) => s.clearAuth);

  const isAuthenticated = !!user;

  const login = useCallback(
    async (email: string, password: string): Promise<boolean> => {
      try {
        const result = await serverLogin(email, password);
        if (result?.accessToken) {
          setTokens(result.accessToken);
          return true;
        }
        return false;
      } catch (err: unknown) {
        const msg = (err as Error).message || 'Login failed';
        toast.error(msg);
        return false;
      }
    },
    [setTokens]
  );

  const logout = useCallback(async () => {
    await clearAuth();
    router.push(ROUTES.LOGIN);
    toast.success('Signed out successfully');
  }, [clearAuth, router]);

  const can = useCallback(
    (required: UserRole): boolean => {
      if (!user) return false;
      return hasPermission(user.role, required);
    },
    [user]
  );

  return {
    user,
    isAuthenticated,
    isLoading,
    login,
    logout,
    can,
  };
}
