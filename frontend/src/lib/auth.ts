/**
 * Auth Utilities — JWT decode, token validation, user extraction
 *
 * Security model:
 *   - Access token: stored in Zustand memory (NOT localStorage)
 *   - Refresh token: stored in httpOnly cookie via Next.js BFF API route
 *   - On page load / refresh: call /api/auth/refresh → get new access token
 *   - On 401: auto-refresh → retry original request → logout on failure
 *
 * This file runs on the CLIENT side only (no Node.js APIs).
 */

import { jwtDecode } from 'jose';
import type { JWTPayload, AuthUser, UserRole } from '@/types/auth';

// ---------------------------------------------------------------------------
// Decode JWT without verification
// Note: verification happens SERVER-SIDE only (FastAPI backend verifies
//       the JWT on every request via the Authorization header).
//       Client-side we only need to READ claims (display role, tenant name).
// ---------------------------------------------------------------------------

export function decodeAccessToken(token: string): JWTPayload | null {
  try {
    // jose's jwtDecode reads the payload without crypto verification
    const payload = JSON.parse(
      Buffer.from(token.split('.')[1], 'base64url').toString('utf-8')
    );
    return payload as JWTPayload;
  } catch {
    return null;
  }
}

export function isTokenExpired(token: string): boolean {
  const payload = decodeAccessToken(token);
  if (!payload) return true;
  // Add 30s buffer so we refresh before it actually expires
  return Date.now() / 1000 > payload.exp - 30;
}

// ---------------------------------------------------------------------------
// Extract normalised AuthUser from JWT payload
// Handles both Cognito and Auth0 custom claim namespaces
// ---------------------------------------------------------------------------

export function extractUser(token: string): AuthUser | null {
  const payload = decodeAccessToken(token);
  if (!payload) return null;

  const tenantId =
    payload.tenant_id ||
    payload['custom:tenant_id'] ||
    payload['https://api.ragplatform.io/tenant_id'] ||
    '';

  const tenantName =
    payload.tenant_name ||
    payload['custom:tenant_name'] ||
    payload['https://api.ragplatform.io/tenant_name'] ||
    'Unknown Tenant';

  const role: UserRole =
    payload.role ||
    payload['custom:role'] ||
    (payload['https://api.ragplatform.io/role'] as UserRole) ||
    'viewer';

  if (!tenantId) return null;

  return {
    id:         payload.sub,
    email:      payload.email || '',
    tenantId,
    tenantName,
    role,
    expiresAt:  payload.exp * 1000,  // convert to ms
  };
}

// ---------------------------------------------------------------------------
// BFF API routes — interact with httpOnly cookie storage
// ---------------------------------------------------------------------------

export async function serverLogin(
  email: string,
  password: string
): Promise<{ accessToken: string } | null> {
  const res = await fetch('/api/auth/login', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ email, password }),
    credentials: 'include',    // receive httpOnly cookie
  });
  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.message || 'Login failed');
  }
  const data = await res.json();
  return { accessToken: data.access_token };
}

export async function serverRefresh(): Promise<string | null> {
  const res = await fetch('/api/auth/refresh', {
    method:      'POST',
    credentials: 'include',   // send httpOnly cookie
  });
  if (!res.ok) return null;
  const data = await res.json();
  return data.access_token || null;
}

export async function serverLogout(): Promise<void> {
  await fetch('/api/auth/logout', {
    method:      'POST',
    credentials: 'include',
  });
}
