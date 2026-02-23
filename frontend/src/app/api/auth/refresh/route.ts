import { NextRequest, NextResponse } from 'next/server';

const BACKEND = process.env.BACKEND_URL ?? 'http://localhost:8000';
const SECURE   = process.env.NODE_ENV === 'production';

export async function POST(req: NextRequest) {
  const refreshToken = req.cookies.get('refresh_token')?.value;

  if (!refreshToken) {
    return NextResponse.json({ error: 'No refresh token' }, { status: 401 });
  }

  try {
    const upstream = await fetch(`${BACKEND}/api/v1/auth/refresh`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ refresh_token: refreshToken }),
    });

    const data = await upstream.json();

    if (!upstream.ok) {
      // Clear stale cookies on refresh failure
      const errResp = NextResponse.json(
        { error: data.detail ?? 'Refresh failed' },
        { status: 401 },
      );
      errResp.cookies.delete('refresh_token');
      errResp.cookies.delete('tenant_id');
      return errResp;
    }

    const { access_token, refresh_token: newRefreshToken, tenant_id, expires_in } = data as {
      access_token:  string;
      refresh_token: string;
      tenant_id:     string;
      expires_in:    number;
    };

    const response = NextResponse.json({ access_token, tenant_id, expires_in });

    // Rotate refresh token
    response.cookies.set('refresh_token', newRefreshToken, {
      httpOnly: true,
      secure:   SECURE,
      sameSite: 'strict',
      path:     '/api/auth',
      maxAge:   60 * 60 * 24 * 30,
    });

    return response;
  } catch (err) {
    console.error('[BFF /api/auth/refresh]', err);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
