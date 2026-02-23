import { NextRequest, NextResponse } from 'next/server';

const BACKEND = process.env.BACKEND_URL ?? 'http://localhost:8000';

export async function POST(req: NextRequest) {
  const refreshToken = req.cookies.get('refresh_token')?.value;

  // Best-effort revocation on the backend (fire-and-forget, don't block)
  if (refreshToken) {
    fetch(`${BACKEND}/api/v1/auth/logout`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ refresh_token: refreshToken }),
    }).catch(() => { /* ignore upstream errors on logout */ });
  }

  const response = NextResponse.json({ success: true });

  // Clear all auth cookies
  response.cookies.set('refresh_token', '', {
    httpOnly: true,
    secure:   process.env.NODE_ENV === 'production',
    sameSite: 'strict',
    path:     '/api/auth',
    maxAge:   0,
  });
  response.cookies.set('tenant_id', '', {
    path:   '/',
    maxAge: 0,
  });

  return response;
}
