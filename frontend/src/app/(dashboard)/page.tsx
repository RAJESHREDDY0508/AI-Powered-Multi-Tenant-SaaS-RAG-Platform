import { redirect } from 'next/navigation';
import { ROUTES }   from '@/lib/constants';

/**
 * Route-group root `/` → redirect to `/dashboard` overview.
 * The actual overview page lives at (dashboard)/dashboard/page.tsx
 */
export default function DashboardGroupRoot() {
  redirect(ROUTES.DASHBOARD);
}
