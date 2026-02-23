'use client';

import toast, { type ToastOptions } from 'react-hot-toast';
import { useCallback } from 'react';

export type ToastVariant = 'success' | 'error' | 'loading' | 'info';

const BASE_OPTS: ToastOptions = {
  duration: 4000,
  position: 'top-right',
};

const STYLE_MAP: Record<ToastVariant, ToastOptions['style']> = {
  success: { background: '#10b981', color: '#fff' },
  error:   { background: '#ef4444', color: '#fff' },
  loading: { background: '#6366f1', color: '#fff' },
  info:    { background: '#3b82f6', color: '#fff' },
};

export function useToast() {
  const success = useCallback(
    (message: string, opts?: ToastOptions) =>
      toast.success(message, { ...BASE_OPTS, style: STYLE_MAP.success, ...opts }),
    [],
  );

  const error = useCallback(
    (message: string, opts?: ToastOptions) =>
      toast.error(message, { ...BASE_OPTS, duration: 6000, style: STYLE_MAP.error, ...opts }),
    [],
  );

  const info = useCallback(
    (message: string, opts?: ToastOptions) =>
      toast(message, { ...BASE_OPTS, style: STYLE_MAP.info, ...opts }),
    [],
  );

  const loading = useCallback(
    (message: string, opts?: ToastOptions): string =>
      toast.loading(message, { ...BASE_OPTS, style: STYLE_MAP.loading, ...opts }),
    [],
  );

  /** Dismiss a specific toast by id (returned from `loading()`) */
  const dismiss = useCallback((id?: string) => toast.dismiss(id), []);

  /**
   * Show an async operation: loading → success/error automatically.
   *
   * @example
   * await promise(uploadFile(), 'Uploading…', 'Uploaded!', 'Upload failed');
   */
  const promise = useCallback(
    <T>(
      prom: Promise<T>,
      loadingMsg: string,
      successMsg: string,
      errorMsg: string,
      opts?: ToastOptions,
    ) =>
      toast.promise(
        prom,
        { loading: loadingMsg, success: successMsg, error: errorMsg },
        { ...BASE_OPTS, ...opts },
      ),
    [],
  );

  return { success, error, info, loading, dismiss, promise };
}
