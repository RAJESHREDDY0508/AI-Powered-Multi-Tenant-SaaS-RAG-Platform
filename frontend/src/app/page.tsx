import { redirect } from 'next/navigation';
import { ROUTES } from '@/lib/constants';

/**
 * Root page — immediately redirect to dashboard.
 * The middleware will catch unauthenticated users and send them to login.
 */
export default function RootPage() {
  redirect(ROUTES.DASHBOARD);
}
