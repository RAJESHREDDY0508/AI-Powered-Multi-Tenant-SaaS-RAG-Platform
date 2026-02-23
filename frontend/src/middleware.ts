import { NextRequest, NextResponse } from 'next/server';
import { ROUTES } from '@/lib/constants';

/** Paths that never need authentication */
const PUBLIC_PATHS = new Set([
  ROUTES.LOGIN,
  ROUTES.REGISTER,
  '/api/auth/login',
  '/api/auth/logout',
  '/api/auth/refresh',
  '/_next',
  '/favicon.ico',
]);

function isPublic(pathname: string): boolean {
  if (PUBLIC_PATHS.has(pathname)) return true;
  // Static Next.js assets
  if (pathname.startsWith('/_next/')) return true;
  if (pathname.startsWith('/api/auth/')) return true;
  return false;
}

/**
 * Edge-compatible middleware:
 * - Reads the `refresh_token` httpOnly cookie to decide if the user *may* be logged in.
 * - If no cookie and route is protected → redirect to /login with `?next=<path>`.
 * - If cookie present and user hits /login → redirect to dashboard.
 *
 * NOTE: We do NOT verify the JWT here (edge runtime has no Node crypto).
 *       The BFF refresh route and the API response interceptor handle token expiry.
 */
export function middleware(request: NextRequest) {
  const { pathname, search } = request.nextUrl;

  // Allow static/public routes through immediately
  if (isPublic(pathname)) {
    return NextResponse.next();
  }

  const hasRefreshToken = request.cookies.has('refresh_token');

  // Unauthenticated user hitting a protected page → login
  if (!hasRefreshToken) {
    const loginUrl = request.nextUrl.clone();
    loginUrl.pathname = ROUTES.LOGIN;
    loginUrl.search = `?next=${encodeURIComponent(pathname + search)}`;
    return NextResponse.redirect(loginUrl);
  }

  // Authenticated user hitting login/register → dashboard
  if (hasRefreshToken && (pathname === ROUTES.LOGIN || pathname === ROUTES.REGISTER)) {
    const dashUrl = request.nextUrl.clone();
    dashUrl.pathname = ROUTES.DASHBOARD;
    dashUrl.search = '';
    return NextResponse.redirect(dashUrl);
  }

  // Propagate tenant info as a request header (read from cookie set at login)
  const tenantId = request.cookies.get('tenant_id')?.value;
  const response = NextResponse.next();
  if (tenantId) {
    response.headers.set('x-tenant-id', tenantId);
  }

  return response;
}

export const config = {
  /*
   * Match all routes except:
   * - _next/static (static files)
   * - _next/image  (image optimisation)
   * - favicon.ico
   */
  matcher: ['/((?!_next/static|_next/image|favicon.ico).*)'],
};
