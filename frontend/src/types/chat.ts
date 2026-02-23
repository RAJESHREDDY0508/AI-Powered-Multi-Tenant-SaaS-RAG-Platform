// =============================================================================
// Chat & RAG Types
// =============================================================================

export interface Citation {
  source_key:   string;
  page_number:  number | null;
  heading:      string | null;
  relevance:    number;           // 0-1
  excerpt:      string;           // first 200 chars of chunk text
}

export type MessageRole = 'user' | 'assistant' | 'system';

export interface Message {
  id:         string;
  role:       MessageRole;
  content:    string;
  citations?: Citation[];
  isStreaming?: boolean;
  timestamp:  string;
  // Metadata from the backend
  model_used?:    string;
  provider?:      string;
  latency_ms?:    number;
  chunks_used?:   number;
  input_tokens?:  number;
  output_tokens?: number;
  request_id?:    string;
}

export interface ChatSession {
  id:        string;
  title:     string;
  messages:  Message[];
  createdAt: string;
  updatedAt: string;
}

export interface QueryRequest {
  question:              string;
  top_k?:                number;   // default 5
  privacy?:              'standard' | 'sensitive' | 'private';
  strategy?:             'highest_quality' | 'lowest_cost' | 'lowest_latency';
  document_permissions?: string[];
}

export interface QueryResponse {
  answer:        string;
  question:      string;
  model_used:    string;
  provider:      string;
  chunks_used:   number;
  input_tokens:  number;
  output_tokens: number;
  latency_ms:    number;
  request_id:    string;
}

// SSE stream events
export type SSEEventType = 'token' | 'done' | 'error';

export interface SSEDonePayload {
  latency_ms:    number;
  chunks_used:   number;
  output_tokens: number;
  request_id:    string;
}

export interface SSEErrorPayload {
  message: string;
}
