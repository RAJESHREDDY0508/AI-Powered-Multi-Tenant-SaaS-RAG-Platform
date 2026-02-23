// =============================================================================
// Auth Types
// =============================================================================

export type UserRole = 'owner' | 'admin' | 'member' | 'viewer';

export interface JWTPayload {
  sub:         string;          // user ID
  tenant_id:   string;          // tenant UUID
  tenant_name: string;          // human-readable org name
  email:       string;
  role:        UserRole;
  exp:         number;          // expiry timestamp (seconds)
  iat:         number;          // issued at timestamp
  // Cognito custom claims
  'custom:tenant_id'?:   string;
  'custom:tenant_name'?: string;
  'custom:role'?:        UserRole;
  // Auth0 custom claims
  'https://api.ragplatform.io/tenant_id'?:   string;
  'https://api.ragplatform.io/tenant_name'?: string;
  'https://api.ragplatform.io/role'?:        UserRole;
}

export interface AuthUser {
  id:          string;
  email:       string;
  tenantId:    string;
  tenantName:  string;
  role:        UserRole;
  expiresAt:   number;
}

export interface AuthState {
  user:        AuthUser | null;
  accessToken: string | null;
  isLoading:   boolean;
  isAuthenticated: boolean;
}

export interface LoginCredentials {
  email:    string;
  password: string;
}

export interface RegisterCredentials {
  email:       string;
  password:    string;
  tenantName?: string;  // only for org creation flow
}

export interface AuthTokenResponse {
  access_token:  string;
  refresh_token: string;
  token_type:    'bearer';
  expires_in:    number;   // seconds
}

// Role hierarchy — higher index = more permissions
export const ROLE_ORDER: Record<UserRole, number> = {
  viewer: 0,
  member: 1,
  admin:  2,
  owner:  3,
};

export function hasPermission(userRole: UserRole, required: UserRole): boolean {
  return ROLE_ORDER[userRole] >= ROLE_ORDER[required];
}
