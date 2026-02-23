'use client';

import { usePathname, useRouter } from 'next/navigation';
import { Moon, Sun, Monitor, LogOut, User, ChevronDown } from 'lucide-react';
import { useState, useRef, useEffect } from 'react';
import { clsx } from 'clsx';
import { useAuthStore } from '@/store/authStore';
import { useTheme }     from '@/hooks/useTheme';
import { useAuth }      from '@/hooks/useAuth';
import { ROUTES }       from '@/lib/constants';

/* Map route to human-readable breadcrumb */
const ROUTE_LABELS: Record<string, string> = {
  [ROUTES.DASHBOARD]: 'Overview',
  [ROUTES.DOCUMENTS]: 'Documents',
  [ROUTES.CHAT]:      'Chat',
  [ROUTES.ADMIN]:     'Admin Dashboard',
  '/settings':        'Settings',
};

function ThemeCycler() {
  const { theme, setTheme } = useTheme();

  const nextTheme = () => {
    const cycle: Record<string, string> = { light: 'dark', dark: 'system', system: 'light' };
    setTheme(cycle[theme] as 'light' | 'dark' | 'system');
  };

  const Icon =
    theme === 'dark'   ? Moon :
    theme === 'light'  ? Sun  :
    Monitor;

  return (
    <button
      onClick={nextTheme}
      className="p-2 rounded-lg text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800 dark:text-gray-400 transition-colors"
      aria-label={`Theme: ${theme}`}
      title={`Theme: ${theme}`}
    >
      <Icon className="h-5 w-5" />
    </button>
  );
}

function UserMenu() {
  const user   = useAuthStore((s) => s.user);
  const { logout } = useAuth();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  // Close on outside click
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  if (!user) return null;

  const initials = user.name
    .split(' ')
    .map((n) => n[0])
    .join('')
    .toUpperCase()
    .slice(0, 2);

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-2 rounded-lg px-2 py-1.5 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors"
        aria-expanded={open}
      >
        <span className="h-8 w-8 rounded-full bg-brand-600 text-white text-xs font-bold flex items-center justify-center">
          {initials}
        </span>
        <span className="hidden md:block text-sm font-medium text-gray-700 dark:text-gray-200 max-w-[120px] truncate">
          {user.name}
        </span>
        <ChevronDown className={clsx('h-4 w-4 text-gray-400 transition-transform', open && 'rotate-180')} />
      </button>

      {open && (
        <div className="absolute right-0 top-full mt-1 w-48 rounded-xl border border-gray-200 bg-white shadow-lg dark:border-gray-700 dark:bg-gray-900 z-50 animate-fade-in">
          <div className="px-3 py-2 border-b border-gray-100 dark:border-gray-800">
            <p className="text-xs text-gray-400">Signed in as</p>
            <p className="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">{user.email}</p>
          </div>
          <div className="py-1">
            <button
              onClick={() => { setOpen(false); logout(); }}
              className="flex items-center gap-2 w-full px-3 py-2 text-sm text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20 dark:text-red-400 rounded-lg mx-1 transition-colors"
              style={{ width: 'calc(100% - 8px)' }}
            >
              <LogOut className="h-4 w-4" />
              Sign out
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

export function TopBar() {
  const pathname = usePathname();

  // Get breadcrumb from current path (matches longest prefix)
  const label = Object.entries(ROUTE_LABELS)
    .filter(([route]) => pathname === route || pathname.startsWith(route + '/'))
    .sort(([a], [b]) => b.length - a.length)[0]?.[1] ?? 'Page';

  return (
    <header className="flex items-center justify-between h-16 px-4 md:px-6 border-b border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 shrink-0">
      {/* Page title */}
      <h1 className="text-base font-semibold text-gray-900 dark:text-gray-100">{label}</h1>

      {/* Right controls */}
      <div className="flex items-center gap-1">
        <ThemeCycler />
        <UserMenu />
      </div>
    </header>
  );
}
