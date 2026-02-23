import type { Metadata } from 'next';
import { Sidebar }  from '@/components/layout/Sidebar';
import { TopBar }   from '@/components/layout/TopBar';
import { ErrorBoundary } from '@/components/ui/ErrorBoundary';

export const metadata: Metadata = {
  title: 'Dashboard',
};

/**
 * Dashboard shell — fixed sidebar + topbar with main content area.
 * All authenticated pages live inside this group.
 */
export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-screen overflow-hidden bg-surface-secondary dark:bg-gray-950">
      {/* Fixed left sidebar */}
      <Sidebar />

      {/* Main content column */}
      <div className="flex flex-col flex-1 min-w-0 overflow-hidden">
        <TopBar />
        <main className="flex-1 overflow-y-auto p-4 md:p-6 lg:p-8">
          <ErrorBoundary>
            {children}
          </ErrorBoundary>
        </main>
      </div>
    </div>
  );
}
