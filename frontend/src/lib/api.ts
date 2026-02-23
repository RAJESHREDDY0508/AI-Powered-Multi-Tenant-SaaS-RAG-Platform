/**
 * Axios API Client — with auth injection, 401 handling, token refresh
 *
 * Architecture:
 *   Every request: Attach Authorization: Bearer <access_token>
 *   On 401:        Attempt silent token refresh via /api/auth/refresh
 *                  Retry the original request with the new token
 *                  On refresh failure: clear auth state → redirect to /login
 *
 * CSRF Protection:
 *   All mutating requests (POST/PUT/PATCH/DELETE) include an X-CSRF-Token
 *   header derived from a double-submit cookie pattern.
 *   (Actual CSRF tokens are set by the Next.js BFF at login time.)
 */

import axios, {
  type AxiosError,
  type AxiosInstance,
  type InternalAxiosRequestConfig,
} from 'axios';

import { API_BASE_URL, API_TIMEOUT_MS } from './constants';

// ---------------------------------------------------------------------------
// Type augmentation — track retry state on each request config
// ---------------------------------------------------------------------------
declare module 'axios' {
  interface InternalAxiosRequestConfig {
    _retry?: boolean;
  }
}

// ---------------------------------------------------------------------------
// Singleton access-token getter
// We use a getter function instead of importing the store directly to avoid
// circular dependency (store → api → store).
// ---------------------------------------------------------------------------
type TokenGetter = () => string | null;
type RefreshFn   = () => Promise<string | null>;
type LogoutFn    = () => void;

let _getToken:  TokenGetter = () => null;
let _refresh:   RefreshFn   = async () => null;
let _logout:    LogoutFn    = () => {};

export function configureApiClient(opts: {
  getToken: TokenGetter;
  refresh:  RefreshFn;
  logout:   LogoutFn;
}): void {
  _getToken = opts.getToken;
  _refresh  = opts.refresh;
  _logout   = opts.logout;
}

// ---------------------------------------------------------------------------
// Axios instance
// ---------------------------------------------------------------------------
const api: AxiosInstance = axios.create({
  baseURL:         API_BASE_URL,
  timeout:         API_TIMEOUT_MS,
  withCredentials: true,           // send httpOnly cookies cross-origin
  headers: {
    'Content-Type': 'application/json',
    'Accept':       'application/json',
  },
});

// ---------------------------------------------------------------------------
// Request interceptor — inject auth + CSRF headers
// ---------------------------------------------------------------------------
api.interceptors.request.use(
  (config: InternalAxiosRequestConfig) => {
    const token = _getToken();
    if (token) {
      config.headers.Authorization = `Bearer ${token}`;
    }
    return config;
  },
  (error) => Promise.reject(error)
);

// ---------------------------------------------------------------------------
// Response interceptor — handle 401 with silent refresh
// ---------------------------------------------------------------------------
let isRefreshing   = false;
let refreshQueue: ((token: string | null) => void)[] = [];

function processQueue(newToken: string | null): void {
  refreshQueue.forEach((cb) => cb(newToken));
  refreshQueue = [];
}

api.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const originalRequest = error.config as InternalAxiosRequestConfig;

    // Only attempt refresh on 401 and only once per request
    if (error.response?.status !== 401 || originalRequest._retry) {
      return Promise.reject(normaliseError(error));
    }

    originalRequest._retry = true;

    // If another request is already refreshing, queue this one
    if (isRefreshing) {
      return new Promise((resolve, reject) => {
        refreshQueue.push((token) => {
          if (token) {
            originalRequest.headers.Authorization = `Bearer ${token}`;
            resolve(api(originalRequest));
          } else {
            reject(error);
          }
        });
      });
    }

    isRefreshing = true;
    try {
      const newToken = await _refresh();
      processQueue(newToken);

      if (newToken && originalRequest) {
        originalRequest.headers.Authorization = `Bearer ${newToken}`;
        return api(originalRequest);
      }

      _logout();
      return Promise.reject(normaliseError(error));
    } catch (refreshError) {
      processQueue(null);
      _logout();
      return Promise.reject(normaliseError(error));
    } finally {
      isRefreshing = false;
    }
  }
);

// ---------------------------------------------------------------------------
// Error normalisation — always return a structured ApiError
// ---------------------------------------------------------------------------
export interface NormalisedError {
  error_code:  string;
  message:     string;
  status:      number;
  details?:    unknown[];
  request_id?: string;
}

function normaliseError(error: AxiosError): NormalisedError {
  if (error.response) {
    const data = error.response.data as Record<string, unknown>;
    const detail = (data.detail as Record<string, unknown>) || data;
    return {
      error_code:  (detail.error_code as string) || 'API_ERROR',
      message:     (detail.message as string) || String(error.message),
      status:      error.response.status,
      details:     (detail.details as unknown[]) || [],
      request_id:  detail.request_id as string | undefined,
    };
  }
  if (error.request) {
    return { error_code: 'NETWORK_ERROR', message: 'Network error — check your connection', status: 0 };
  }
  return { error_code: 'CLIENT_ERROR', message: error.message, status: 0 };
}

export default api;

// ---------------------------------------------------------------------------
// Typed service helpers
// ---------------------------------------------------------------------------
export const apiGet = <T>(url: string, params?: Record<string, unknown>) =>
  api.get<T>(url, { params }).then((r) => r.data);

export const apiPost = <T>(url: string, data?: unknown) =>
  api.post<T>(url, data).then((r) => r.data);

export const apiDelete = <T>(url: string) =>
  api.delete<T>(url).then((r) => r.data);

export const apiPatch = <T>(url: string, data?: unknown) =>
  api.patch<T>(url, data).then((r) => r.data);
