// =============================================================================
// Document Types
// =============================================================================

export type DocumentStatus = 'pending' | 'processing' | 'ready' | 'failed' | 'deleted';

export interface Document {
  id:             string;
  filename:       string;
  document_name:  string;
  content_type:   string;
  size_bytes:     number;
  status:         DocumentStatus;
  error_message?: string;
  chunk_count:    number;
  vector_count:   number;
  s3_key:         string;
  created_at:     string;
  updated_at:     string;
}

export interface DocumentListResponse {
  documents:   Document[];
  total:       number;
  page:        number;
  page_size:   number;
  total_pages: number;
}

export interface UploadResponse {
  document_id: string;
  filename:    string;
  status:      DocumentStatus;
  s3_key:      string;
  md5_checksum:string;
  message:     string;
}

export interface DocumentStatusResponse {
  document_id:   string;
  status:        DocumentStatus;
  chunk_count:   number;
  vector_count:  number;
  error_message?: string;
  updated_at:    string;
}

// For the upload UI
export interface UploadFile {
  id:       string;       // local UUID for tracking
  file:     File;
  progress: number;       // 0-100
  status:   'queued' | 'uploading' | 'processing' | 'done' | 'error' | 'duplicate';
  documentId?: string;
  error?:   string;
}

export interface DocumentFilterParams {
  page?:     number;
  page_size?: number;
  status?:   DocumentStatus;
  search?:   string;
}
