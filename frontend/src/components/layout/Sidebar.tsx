'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { clsx } from 'clsx';
import {
  LayoutDashboard,
  FileText,
  MessageSquare,
  BarChart3,
  Settings,
  ChevronLeft,
  ChevronRight,
  Cpu,
} from 'lucide-react';
import { useUIStore }   from '@/store/uiStore';
import { useAuthStore } from '@/store/authStore';
import { UserRole, ROUTES } from '@/lib/constants';

interface NavItem {
  label:    string;
  href:     string;
  icon:     React.ElementType;
  roles?:   UserRole[];   // undefined → all roles
}

const NAV_ITEMS: NavItem[] = [
  { label: 'Overview',   href: ROUTES.DASHBOARD,  icon: LayoutDashboard },
  { label: 'Documents',  href: ROUTES.DOCUMENTS,  icon: FileText        },
  { label: 'Chat',       href: ROUTES.CHAT,       icon: MessageSquare   },
  { label: 'Admin',      href: ROUTES.ADMIN,      icon: BarChart3,      roles: ['admin'] },
  { label: 'Settings',   href: '/settings',       icon: Settings        },
];

export function Sidebar() {
  const collapsed    = useUIStore((s) => s.sidebarCollapsed);
  const toggleSidebar = useUIStore((s) => s.toggleSidebar);
  const user         = useAuthStore((s) => s.user);
  const pathname     = usePathname();

  const visibleItems = NAV_ITEMS.filter(
    (item) => !item.roles || (user && item.roles.includes(user.role)),
  );

  return (
    <aside
      className={clsx(
        'relative flex flex-col h-full border-r border-gray-200 dark:border-gray-800',
        'bg-white dark:bg-gray-900 transition-all duration-200 ease-in-out',
        collapsed ? 'w-16' : 'w-60',
      )}
    >
      {/* Logo */}
      <div className="flex items-center gap-2 px-4 h-16 border-b border-gray-200 dark:border-gray-800 shrink-0">
        <Cpu className="h-7 w-7 text-brand-600 shrink-0" />
        {!collapsed && (
          <span className="font-bold text-gray-900 dark:text-gray-100 text-sm truncate">
            RAG Platform
          </span>
        )}
      </div>

      {/* Navigation */}
      <nav className="flex-1 overflow-y-auto py-3 px-2 space-y-0.5">
        {visibleItems.map((item) => {
          const Icon    = item.icon;
          const isActive = pathname === item.href || pathname.startsWith(item.href + '/');
          return (
            <Link
              key={item.href}
              href={item.href}
              title={collapsed ? item.label : undefined}
              className={clsx(
                'flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium',
                'transition-colors duration-100 group',
                isActive
                  ? 'bg-brand-50 text-brand-700 dark:bg-brand-900/30 dark:text-brand-300'
                  : 'text-gray-600 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-800',
              )}
            >
              <Icon
                className={clsx(
                  'h-5 w-5 shrink-0',
                  isActive ? 'text-brand-600 dark:text-brand-400' : 'text-gray-400 group-hover:text-gray-600 dark:group-hover:text-gray-300',
                )}
              />
              {!collapsed && <span className="truncate">{item.label}</span>}
            </Link>
          );
        })}
      </nav>

      {/* Tenant badge */}
      {!collapsed && user && (
        <div className="px-3 py-3 border-t border-gray-200 dark:border-gray-800">
          <p className="text-xs text-gray-400 truncate">{user.tenantId}</p>
          <p className="text-sm font-medium text-gray-700 dark:text-gray-200 truncate">{user.name}</p>
          <p className="text-xs text-gray-400 truncate capitalize">{user.role}</p>
        </div>
      )}

      {/* Collapse toggle */}
      <button
        onClick={toggleSidebar}
        className={clsx(
          'absolute -right-3 top-20 z-10',
          'flex h-6 w-6 items-center justify-center rounded-full',
          'border border-gray-200 bg-white shadow-sm dark:border-gray-700 dark:bg-gray-800',
          'text-gray-500 hover:text-gray-700 dark:hover:text-gray-300',
          'transition-colors duration-100',
        )}
        aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
      >
        {collapsed ? <ChevronRight className="h-3 w-3" /> : <ChevronLeft className="h-3 w-3" />}
      </button>
    </aside>
  );
}
