// =============================================================================
// API Response Types
// =============================================================================

export interface ApiError {
  error_code: string;
  message:    string;
  details?:   { field: string; message: string; code: string }[];
  request_id?: string;
}

export interface PaginatedResponse<T> {
  data:        T[];
  total:       number;
  page:        number;
  page_size:   number;
  total_pages: number;
}

// Admin / metrics types
export interface UsageSummary {
  tenant_id:             string;
  period_from:           string;
  period_to:             string;
  total_queries:         number;
  evaluated_queries:     number;
  avg_faithfulness:      number | null;
  avg_answer_relevance:  number | null;
  avg_context_precision: number | null;
  avg_composite:         number | null;
  avg_latency_ms:        number | null;
  p95_latency_ms:        number | null;
}

export interface MonthlyUsageRow {
  month_year:    string;
  model:         string;
  provider:      string;
  input_tokens:  number;
  output_tokens: number;
  request_count: number;
  cost_usd:      number;
}

export interface CostReport {
  tenant_id:      string;
  rows:           MonthlyUsageRow[];
  total_cost_usd: number;
  total_requests: number;
}

export interface EvalResult {
  id:                string;
  request_id:        string;
  question:          string;
  answer:            string;
  faithfulness:      number | null;
  answer_relevance:  number | null;
  context_precision: number | null;
  composite_score:   number | null;
  model_used:        string;
  latency_ms:        number;
  eval_status:       string;
  created_at:        string;
}

// Feature flags
export interface FeatureFlags {
  hybridSearch:      boolean;
  cohereRerank:      boolean;
  streamingEnabled:  boolean;
  adminDashboard:    boolean;
  darkMode:          boolean;
  i18n:              boolean;
}
