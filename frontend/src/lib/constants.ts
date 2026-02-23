// =============================================================================
// Application Constants
// =============================================================================

export const APP_NAME = 'RAG Platform';
export const APP_VERSION = process.env.NEXT_PUBLIC_APP_VERSION || '1.0.0';

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------
export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

export const API_TIMEOUT_MS = 30_000;

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------
export const AUTH_COOKIE_NAME    = '__rag_rt';        // refresh token (httpOnly)
export const ACCESS_TOKEN_TTL_MS = 15 * 60 * 1000;   // 15 min — match backend JWT exp
export const REFRESH_TOKEN_TTL_MS = 7 * 24 * 60 * 60 * 1000; // 7 days

// ---------------------------------------------------------------------------
// Document upload
// ---------------------------------------------------------------------------
export const UPLOAD_MAX_SIZE_BYTES  = 500 * 1024 * 1024;  // 500 MB
export const UPLOAD_ALLOWED_TYPES   = [
  'application/pdf',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document', // docx
  'text/plain',
  'text/markdown',
];
export const UPLOAD_ALLOWED_EXTENSIONS = ['.pdf', '.docx', '.txt', '.md'];

/** react-dropzone `accept` map — keys are MIME types, values are file extensions */
export const ACCEPTED_MIME_TYPES: Record<string, string[]> = {
  'application/pdf': ['.pdf'],
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document': ['.docx'],
  'text/plain':      ['.txt'],
  'text/markdown':   ['.md'],
};

// ---------------------------------------------------------------------------
// Polling intervals
// ---------------------------------------------------------------------------
export const DOCUMENT_POLL_INTERVAL_MS = 3_000;   // poll status every 3s
export const DOCUMENT_POLL_MAX_RETRIES = 60;      // give up after 3 min

// ---------------------------------------------------------------------------
// Chat
// ---------------------------------------------------------------------------
export const CHAT_MAX_QUESTION_LENGTH = 2_000;
export const CHAT_DEFAULT_TOP_K       = 5;

// ---------------------------------------------------------------------------
// Feature flags (override per-tenant via DB — this is the default)
// ---------------------------------------------------------------------------
export const DEFAULT_FEATURE_FLAGS = {
  hybridSearch:     true,
  cohereRerank:     true,
  streamingEnabled: true,
  adminDashboard:   true,
  darkMode:         true,
  i18n:             false,
} as const;

// ---------------------------------------------------------------------------
// Routes
// ---------------------------------------------------------------------------
// ---------------------------------------------------------------------------
// User roles (re-exported here so Sidebar can import from one place)
// ---------------------------------------------------------------------------
export type UserRole = 'admin' | 'user' | 'viewer';

export const ROUTES = {
  HOME:       '/',
  LOGIN:      '/login',
  REGISTER:   '/register',
  DASHBOARD:  '/dashboard',
  DOCUMENTS:  '/documents',
  CHAT:       '/chat',
  ADMIN:      '/admin',
} as const;
