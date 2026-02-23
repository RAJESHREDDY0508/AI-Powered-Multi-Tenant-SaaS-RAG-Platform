/**
 * Document Store — Zustand
 * Manages document list, upload queue, and polling state.
 */

import { create } from 'zustand';
import { immer } from 'zustand/middleware/immer';
import type { Document, UploadFile } from '@/types/documents';

function makeId(): string {
  return typeof crypto !== 'undefined' && crypto.randomUUID
    ? crypto.randomUUID()
    : Math.random().toString(36).slice(2);
}

interface DocumentStore {
  documents:    Document[];
  uploadQueue:  UploadFile[];
  isLoading:    boolean;
  totalCount:   number;
  currentPage:  number;
  error:        string | null;

  // Actions
  setDocuments:    (docs: Document[], total: number, page: number) => void;
  upsertDocument:  (doc: Document) => void;
  removeDocument:  (id: string) => void;
  setLoading:      (v: boolean) => void;
  setError:        (msg: string | null) => void;

  // Upload queue
  addToQueue:      (file: File) => string;    // returns local file id
  updateProgress:  (id: string, progress: number) => void;
  setFileStatus:   (id: string, status: UploadFile['status'], extra?: Partial<UploadFile>) => void;
  removeFromQueue: (id: string) => void;
  clearCompleted:  () => void;
}

export const useDocumentStore = create<DocumentStore>()(
  immer((set) => ({
    documents:   [],
    uploadQueue: [],
    isLoading:   false,
    totalCount:  0,
    currentPage: 1,
    error:       null,

    setDocuments: (docs, total, page) =>
      set((s) => {
        s.documents   = docs;
        s.totalCount  = total;
        s.currentPage = page;
      }),

    upsertDocument: (doc) =>
      set((s) => {
        const idx = s.documents.findIndex((d) => d.id === doc.id);
        if (idx >= 0) s.documents[idx] = doc;
        else           s.documents.unshift(doc);
      }),

    removeDocument: (id) =>
      set((s) => {
        s.documents = s.documents.filter((d) => d.id !== id);
      }),

    setLoading: (v) => set((s) => { s.isLoading = v; }),
    setError:   (msg) => set((s) => { s.error = msg; }),

    addToQueue: (file: File): string => {
      const id = makeId();
      const uploadFile: UploadFile = {
        id,
        file,
        progress: 0,
        status:   'queued',
      };
      set((s) => { s.uploadQueue.push(uploadFile); });
      return id;
    },

    updateProgress: (id, progress) =>
      set((s) => {
        const f = s.uploadQueue.find((x) => x.id === id);
        if (f) f.progress = progress;
      }),

    setFileStatus: (id, status, extra = {}) =>
      set((s) => {
        const f = s.uploadQueue.find((x) => x.id === id);
        if (f) Object.assign(f, { status, ...extra });
      }),

    removeFromQueue: (id) =>
      set((s) => { s.uploadQueue = s.uploadQueue.filter((x) => x.id !== id); }),

    clearCompleted: () =>
      set((s) => {
        s.uploadQueue = s.uploadQueue.filter(
          (x) => x.status !== 'done' && x.status !== 'duplicate'
        );
      }),
  }))
);
