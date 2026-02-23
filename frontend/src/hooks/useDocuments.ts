/**
 * useDocuments — document list fetching, upload, polling, delete
 */

'use client';

import { useCallback, useEffect, useRef } from 'react';
import toast from 'react-hot-toast';
import api from '@/lib/api';
import { useDocumentStore } from '@/store/documentStore';
import {
  DOCUMENT_POLL_INTERVAL_MS,
  DOCUMENT_POLL_MAX_RETRIES,
  UPLOAD_MAX_SIZE_BYTES,
  UPLOAD_ALLOWED_TYPES,
} from '@/lib/constants';
import type {
  DocumentListResponse,
  UploadResponse,
  DocumentStatusResponse,
  DocumentFilterParams,
} from '@/types/documents';

export function useDocuments() {
  const store = useDocumentStore();
  // Map of documentId → retryCount for polling
  const pollCounters = useRef<Map<string, number>>(new Map());
  const pollTimers   = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());

  // -------------------------------------------------------------------------
  // Fetch document list
  // -------------------------------------------------------------------------
  const fetchDocuments = useCallback(async (params: DocumentFilterParams = {}) => {
    store.setLoading(true);
    store.setError(null);
    try {
      const data = await api.get<DocumentListResponse>('/api/v1/documents', { params }).then(r => r.data);
      store.setDocuments(data.documents, data.total, data.page);
    } catch (err: unknown) {
      const msg = (err as Error).message || 'Failed to load documents';
      store.setError(msg);
      toast.error(msg);
    } finally {
      store.setLoading(false);
    }
  }, [store]);

  // -------------------------------------------------------------------------
  // Upload a single file with progress tracking
  // -------------------------------------------------------------------------
  const uploadFile = useCallback(async (file: File): Promise<string | null> => {
    // Client-side validation
    if (file.size > UPLOAD_MAX_SIZE_BYTES) {
      toast.error(`File too large: max 500 MB`);
      return null;
    }
    if (!UPLOAD_ALLOWED_TYPES.includes(file.type) && file.type !== '') {
      toast.error(`Unsupported file type: ${file.type}`);
      return null;
    }

    const queueId = store.addToQueue(file);
    store.setFileStatus(queueId, 'uploading');

    const formData = new FormData();
    formData.append('file', file);
    formData.append('document_name', file.name);

    try {
      const response = await api.post<UploadResponse>(
        '/api/v1/documents/upload',
        formData,
        {
          headers: { 'Content-Type': 'multipart/form-data' },
          onUploadProgress: (evt) => {
            if (evt.total) {
              const pct = Math.round((evt.loaded / evt.total) * 100);
              store.updateProgress(queueId, pct);
            }
          },
        }
      );

      const data = response.data;
      store.setFileStatus(queueId, 'processing', { documentId: data.document_id });
      toast.success(`${file.name} uploaded — processing started`);

      // Start polling for completion
      startPolling(queueId, data.document_id);
      return data.document_id;
    } catch (err: unknown) {
      const apiErr = err as { error_code?: string; message?: string };
      if (apiErr.error_code === 'DUPLICATE_FILE') {
        store.setFileStatus(queueId, 'duplicate', { error: 'Already processed' });
        toast('File already processed — skipping', { icon: 'ℹ️' });
        return null;
      }
      const msg = apiErr.message || 'Upload failed';
      store.setFileStatus(queueId, 'error', { error: msg });
      toast.error(`${file.name}: ${msg}`);
      return null;
    }
  }, [store]);

  // -------------------------------------------------------------------------
  // Poll document status until ready or failed
  // -------------------------------------------------------------------------
  const startPolling = useCallback((queueId: string, documentId: string) => {
    pollCounters.current.set(documentId, 0);

    const poll = async () => {
      const retries = (pollCounters.current.get(documentId) ?? 0) + 1;
      pollCounters.current.set(documentId, retries);

      if (retries > DOCUMENT_POLL_MAX_RETRIES) {
        store.setFileStatus(queueId, 'error', { error: 'Processing timeout' });
        toast.error('Document processing timed out');
        return;
      }

      try {
        const status = await api
          .get<DocumentStatusResponse>(`/api/v1/documents/${documentId}/status`)
          .then(r => r.data);

        if (status.status === 'ready') {
          store.setFileStatus(queueId, 'done');
          toast.success('Document processing complete ✓');
          // Refresh the document list
          void fetchDocuments();
        } else if (status.status === 'failed') {
          store.setFileStatus(queueId, 'error', { error: status.error_message ?? 'Processing failed' });
          toast.error(`Processing failed: ${status.error_message ?? 'Unknown error'}`);
        } else {
          // Still processing — schedule next poll
          const timer = setTimeout(poll, DOCUMENT_POLL_INTERVAL_MS);
          pollTimers.current.set(documentId, timer);
        }
      } catch {
        const timer = setTimeout(poll, DOCUMENT_POLL_INTERVAL_MS);
        pollTimers.current.set(documentId, timer);
      }
    };

    const timer = setTimeout(poll, DOCUMENT_POLL_INTERVAL_MS);
    pollTimers.current.set(documentId, timer);
  }, [fetchDocuments, store]);

  // -------------------------------------------------------------------------
  // Delete a document
  // -------------------------------------------------------------------------
  const deleteDocument = useCallback(async (documentId: string): Promise<boolean> => {
    try {
      await api.delete(`/api/v1/documents/${documentId}`);
      store.removeDocument(documentId);
      toast.success('Document deleted');
      return true;
    } catch (err: unknown) {
      toast.error((err as Error).message || 'Delete failed');
      return false;
    }
  }, [store]);

  // Cleanup polling timers on unmount
  useEffect(() => {
    return () => {
      pollTimers.current.forEach((t) => clearTimeout(t));
    };
  }, []);

  return {
    documents:    store.documents,
    uploadQueue:  store.uploadQueue,
    isLoading:    store.isLoading,
    totalCount:   store.totalCount,
    fetchDocuments,
    uploadFile,
    deleteDocument,
    clearCompleted: store.clearCompleted,
  };
}
