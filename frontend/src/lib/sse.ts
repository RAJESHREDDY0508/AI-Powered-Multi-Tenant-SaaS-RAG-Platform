/**
 * SSE Client — Server-Sent Events wrapper for RAG streaming responses
 *
 * The backend emits:
 *   event: token  data: <raw_text_delta>
 *   event: done   data: {"latency_ms": ..., "chunks_used": ..., "request_id": "..."}
 *   event: error  data: {"message": "..."}
 *
 * Usage:
 *   const controller = streamQuery({
 *     question: "...",
 *     onToken: (t) => append(t),
 *     onDone:  (meta) => finalise(meta),
 *     onError: (err) => showError(err),
 *   });
 *   // later:
 *   controller.abort();
 *
 * Why fetch + ReadableStream instead of EventSource?
 *   - EventSource does not support POST requests or custom headers.
 *   - fetch() supports both POST body (our query payload) and auth headers.
 *   - We parse the SSE format manually using TextDecoder.
 */

import { API_BASE_URL } from './constants';
import type { QueryRequest, SSEDonePayload } from '@/types/chat';

export interface StreamOptions {
  question:              string;
  top_k?:                number;
  privacy?:              string;
  strategy?:             string;
  document_permissions?: string[];
  accessToken:           string;
  onToken:               (token: string) => void;
  onDone:                (payload: SSEDonePayload) => void;
  onError:               (message: string) => void;
}

export function streamQuery(opts: StreamOptions): AbortController {
  const controller = new AbortController();

  const body: QueryRequest = {
    question:              opts.question,
    top_k:                 opts.top_k ?? 5,
    privacy:               (opts.privacy as QueryRequest['privacy']) ?? 'standard',
    strategy:              (opts.strategy as QueryRequest['strategy']) ?? 'highest_quality',
    document_permissions:  opts.document_permissions ?? [],
  };

  // Run async inside the sync factory — lets callers get the controller immediately
  void fetchSSEStream(
    `${API_BASE_URL}/api/v1/query/stream`,
    body,
    opts.accessToken,
    controller.signal,
    opts.onToken,
    opts.onDone,
    opts.onError,
  );

  return controller;
}

async function fetchSSEStream(
  url:         string,
  body:        QueryRequest,
  token:       string,
  signal:      AbortSignal,
  onToken:     (t: string) => void,
  onDone:      (p: SSEDonePayload) => void,
  onError:     (m: string) => void,
): Promise<void> {
  let response: Response;
  try {
    response = await fetch(url, {
      method:      'POST',
      signal,
      headers: {
        'Content-Type':  'application/json',
        'Accept':        'text/event-stream',
        'Authorization': `Bearer ${token}`,
        'Cache-Control': 'no-cache',
      },
      body: JSON.stringify(body),
    });
  } catch (err) {
    if ((err as Error).name === 'AbortError') return;  // cancelled — expected
    onError('Network error — could not reach the server.');
    return;
  }

  if (!response.ok) {
    try {
      const data = await response.json();
      onError(data?.detail?.message || data?.message || `HTTP ${response.status}`);
    } catch {
      onError(`HTTP ${response.status}`);
    }
    return;
  }

  const reader  = response.body?.getReader();
  const decoder = new TextDecoder('utf-8');

  if (!reader) {
    onError('Streaming not supported by this browser.');
    return;
  }

  // SSE buffer — events may span multiple chunks
  let buffer = '';

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      // Split on double newline (SSE event separator)
      const events = buffer.split('\n\n');
      // Last element may be incomplete — keep it in the buffer
      buffer = events.pop() ?? '';

      for (const eventBlock of events) {
        if (!eventBlock.trim()) continue;

        const lines       = eventBlock.split('\n');
        let   eventName   = 'message';
        let   dataPayload = '';

        for (const line of lines) {
          if (line.startsWith('event: ')) {
            eventName = line.slice(7).trim();
          } else if (line.startsWith('data: ')) {
            dataPayload = line.slice(6);
          }
        }

        switch (eventName) {
          case 'token':
            onToken(dataPayload);
            break;
          case 'done':
            try {
              onDone(JSON.parse(dataPayload) as SSEDonePayload);
            } catch {
              onDone({ latency_ms: 0, chunks_used: 0, output_tokens: 0, request_id: '' });
            }
            return;  // stream finished normally
          case 'error':
            try {
              const errPayload = JSON.parse(dataPayload) as { message: string };
              onError(errPayload.message);
            } catch {
              onError(dataPayload);
            }
            return;
          default:
            break;
        }
      }
    }
  } catch (err) {
    if ((err as Error).name !== 'AbortError') {
      onError('Stream interrupted unexpectedly.');
    }
  } finally {
    reader.cancel().catch(() => {});
  }
}
