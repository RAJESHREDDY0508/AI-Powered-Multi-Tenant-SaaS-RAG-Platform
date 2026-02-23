import type { Metadata } from 'next';

export const metadata: Metadata = {
  title: 'Sign In',
};

/**
 * Auth layout — centered card on a gradient background.
 * No sidebar / topbar; designed for login & register pages.
 */
export default function AuthLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen flex items-center justify-center bg-gradient-to-br from-indigo-50 via-white to-purple-50 dark:from-gray-950 dark:via-gray-900 dark:to-indigo-950 p-4">
      {children}
    </div>
  );
}
