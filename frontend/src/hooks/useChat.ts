/**
 * useChat — manages streaming RAG chat sessions
 */

'use client';

import { useCallback, useRef } from 'react';
import toast from 'react-hot-toast';

import { useChatStore } from '@/store/chatStore';
import { useAuthStore } from '@/store/authStore';
import { streamQuery } from '@/lib/sse';
import type { QueryRequest } from '@/types/chat';

export function useChat() {
  const store       = useChatStore();
  const accessToken = useAuthStore((s) => s.accessToken);
  const abortRef    = useRef<AbortController | null>(null);

  const sessions      = store.sessions;
  const activeSession = store.activeSession();
  const messages      = store.activeMessages();
  const isStreaming   = store.isStreaming;
  const error         = store.error;

  // -------------------------------------------------------------------------
  // Send a question and stream the answer
  // -------------------------------------------------------------------------
  const sendMessage = useCallback(
    async (question: string, options?: Partial<Omit<QueryRequest, 'question'>>) => {
      if (!accessToken) {
        toast.error('Not authenticated');
        return;
      }
      if (!question.trim()) return;
      if (isStreaming) return;

      // Ensure there's an active session
      if (!store.activeSessionId) {
        store.newSession();
      }

      // Add user message
      store.addUserMessage(question);

      // Add an empty streaming placeholder for the assistant
      store.startStreaming();

      // Start SSE stream
      const controller = streamQuery({
        question,
        top_k:                 options?.top_k ?? 5,
        privacy:               options?.privacy ?? 'standard',
        strategy:              options?.strategy ?? 'highest_quality',
        document_permissions:  options?.document_permissions,
        accessToken,
        onToken: (token) => {
          store.appendToken(token);
        },
        onDone: (meta) => {
          store.finaliseStream(meta);
          abortRef.current = null;
        },
        onError: (msg) => {
          store.setStreamError(msg);
          toast.error(msg);
          abortRef.current = null;
        },
      });

      abortRef.current = controller;
    },
    [accessToken, isStreaming, store]
  );

  // -------------------------------------------------------------------------
  // Cancel streaming
  // -------------------------------------------------------------------------
  const cancelStream = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    store.cancelStream();
  }, [store]);

  // -------------------------------------------------------------------------
  // Session management
  // -------------------------------------------------------------------------
  const newSession = useCallback(() => {
    store.newSession();
  }, [store]);

  const switchSession = useCallback((id: string) => {
    store.setActiveSession(id);
  }, [store]);

  const clearCurrentSession = useCallback(() => {
    store.clearSession();
  }, [store]);

  return {
    sessions,
    activeSession,
    messages,
    isStreaming,
    error,
    sendMessage,
    cancelStream,
    newSession,
    switchSession,
    clearCurrentSession,
  };
}
