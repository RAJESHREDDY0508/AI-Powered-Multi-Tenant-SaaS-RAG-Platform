'use client';

import { useCallback, useRef } from 'react';
import { useDropzone }         from 'react-dropzone';
import { clsx }                from 'clsx';
import {
  UploadCloud,
  FileText,
  CheckCircle2,
  AlertCircle,
  Loader2,
  X,
} from 'lucide-react';
import { Button }         from '@/components/ui/Button';
import { Badge }          from '@/components/ui/Badge';
import { useDocuments }   from '@/hooks/useDocuments';
import { useDocumentStore } from '@/store/documentStore';
import { UPLOAD_MAX_SIZE_BYTES, ACCEPTED_MIME_TYPES } from '@/lib/constants';
import type { UploadFile } from '@/types/documents';

function formatBytes(bytes: number): string {
  if (bytes < 1024)          return `${bytes} B`;
  if (bytes < 1024 * 1024)   return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function statusBadge(file: UploadFile) {
  switch (file.status) {
    case 'idle':        return <Badge variant="default">Queued</Badge>;
    case 'uploading':   return <Badge variant="info">Uploading {file.progress}%</Badge>;
    case 'processing':  return <Badge variant="warning">Processing…</Badge>;
    case 'complete':    return <Badge variant="success">Ready</Badge>;
    case 'error':       return <Badge variant="danger">Failed</Badge>;
  }
}

function FileRow({ file }: { file: UploadFile }) {
  const removeFromQueue = useDocumentStore((s) => s.removeFromQueue);

  const canRemove = file.status === 'idle' || file.status === 'error';

  return (
    <div className="flex items-center gap-3 p-3 rounded-lg border border-gray-100 dark:border-gray-800 bg-white dark:bg-gray-900">
      {/* Icon */}
      <div className="shrink-0">
        {file.status === 'complete'   && <CheckCircle2 className="h-5 w-5 text-emerald-500" />}
        {file.status === 'error'      && <AlertCircle  className="h-5 w-5 text-red-500" />}
        {file.status === 'uploading'  && <Loader2      className="h-5 w-5 text-brand-500 animate-spin" />}
        {file.status === 'processing' && <Loader2      className="h-5 w-5 text-amber-500 animate-spin" />}
        {file.status === 'idle'       && <FileText     className="h-5 w-5 text-gray-400" />}
      </div>

      {/* Name + size */}
      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium text-gray-800 dark:text-gray-100 truncate">{file.name}</p>
        <p className="text-xs text-gray-400">{formatBytes(file.size)}</p>
        {file.status === 'uploading' && (
          <div className="mt-1.5 h-1 rounded-full bg-gray-200 dark:bg-gray-700">
            <div
              className="h-1 rounded-full bg-brand-500 transition-all duration-300"
              style={{ width: `${file.progress}%` }}
            />
          </div>
        )}
        {file.error && (
          <p className="text-xs text-red-500 mt-0.5">{file.error}</p>
        )}
      </div>

      {/* Status badge */}
      <div className="shrink-0">{statusBadge(file)}</div>

      {/* Remove */}
      {canRemove && (
        <button
          onClick={() => removeFromQueue(file.id)}
          className="shrink-0 p-1 rounded text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
          aria-label="Remove file"
        >
          <X className="h-4 w-4" />
        </button>
      )}
    </div>
  );
}

export function DocumentUpload() {
  const { uploadFile }   = useDocuments();
  const queue            = useDocumentStore((s) => s.uploadQueue);
  const clearCompleted   = useDocumentStore((s) => s.clearCompleted);

  const onDrop = useCallback(
    async (accepted: File[], rejected: any[]) => {
      for (const file of accepted) {
        await uploadFile(file);
      }
    },
    [uploadFile],
  );

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    maxSize:  UPLOAD_MAX_SIZE_BYTES,
    accept:   ACCEPTED_MIME_TYPES,
    multiple: true,
  });

  const hasCompleted = queue.some((f) => f.status === 'complete');

  return (
    <div className="space-y-4">
      {/* Drop zone */}
      <div
        {...getRootProps()}
        className={clsx(
          'relative flex flex-col items-center justify-center gap-3',
          'rounded-xl border-2 border-dashed p-10 cursor-pointer',
          'transition-colors duration-200',
          isDragActive
            ? 'border-brand-500 bg-brand-50 dark:bg-brand-900/20'
            : 'border-gray-300 bg-gray-50 hover:bg-gray-100 dark:border-gray-700 dark:bg-gray-900/50 dark:hover:bg-gray-900',
        )}
      >
        <input {...getInputProps()} />
        <UploadCloud
          className={clsx(
            'h-12 w-12',
            isDragActive ? 'text-brand-500' : 'text-gray-400',
          )}
        />
        <div className="text-center">
          <p className="text-sm font-medium text-gray-700 dark:text-gray-200">
            {isDragActive ? 'Drop files here…' : 'Drag & drop files, or click to browse'}
          </p>
          <p className="text-xs text-gray-400 mt-1">
            PDF, DOCX, TXT, MD — up to 500 MB each
          </p>
        </div>
        <Button variant="secondary" size="sm" type="button">
          Choose files
        </Button>
      </div>

      {/* Upload queue */}
      {queue.length > 0 && (
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-200">
              Upload queue ({queue.length})
            </h3>
            {hasCompleted && (
              <button
                onClick={clearCompleted}
                className="text-xs text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
              >
                Clear completed
              </button>
            )}
          </div>
          <div className="space-y-2">
            {queue.map((file) => (
              <FileRow key={file.id} file={file} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
