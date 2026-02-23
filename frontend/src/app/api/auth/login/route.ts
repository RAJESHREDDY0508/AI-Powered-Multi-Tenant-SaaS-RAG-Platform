import { NextRequest, NextResponse } from 'next/server';

const BACKEND = process.env.BACKEND_URL ?? 'http://localhost:8000';
const SECURE   = process.env.NODE_ENV === 'production';

export async function POST(req: NextRequest) {
  try {
    const body = await req.json();

    // Forward credentials to FastAPI auth endpoint
    const upstream = await fetch(`${BACKEND}/api/v1/auth/login`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(body),
    });

    const data = await upstream.json();

    if (!upstream.ok) {
      return NextResponse.json(
        { error: data.detail ?? 'Login failed' },
        { status: upstream.status },
      );
    }

    const { access_token, refresh_token, tenant_id, expires_in } = data as {
      access_token:  string;
      refresh_token: string;
      tenant_id:     string;
      expires_in:    number;
    };

    const response = NextResponse.json({ access_token, tenant_id, expires_in });

    // Refresh token → httpOnly, SameSite=Strict cookie (never accessible to JS)
    response.cookies.set('refresh_token', refresh_token, {
      httpOnly: true,
      secure:   SECURE,
      sameSite: 'strict',
      path:     '/api/auth',
      maxAge:   60 * 60 * 24 * 30, // 30 days
    });

    // Non-sensitive tenant identifier (used by middleware for header injection)
    response.cookies.set('tenant_id', tenant_id, {
      httpOnly: false,
      secure:   SECURE,
      sameSite: 'strict',
      path:     '/',
      maxAge:   60 * 60 * 24 * 30,
    });

    return response;
  } catch (err) {
    console.error('[BFF /api/auth/login]', err);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
