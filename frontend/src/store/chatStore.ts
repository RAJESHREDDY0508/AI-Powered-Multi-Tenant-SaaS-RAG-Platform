/**
 * Chat Store — Zustand
 * Manages chat sessions, streaming state, and message history.
 */

import { create } from 'zustand';
import { immer } from 'zustand/middleware/immer';
import { v4 as uuid } from 'crypto';
import type { Message, ChatSession, SSEDonePayload } from '@/types/chat';

// Polyfill for uuid in browser
function makeId(): string {
  return typeof crypto !== 'undefined' && crypto.randomUUID
    ? crypto.randomUUID()
    : Math.random().toString(36).slice(2);
}

// ---------------------------------------------------------------------------
// State shape
// ---------------------------------------------------------------------------
interface ChatStore {
  sessions:        ChatSession[];
  activeSessionId: string | null;
  isStreaming:     boolean;
  streamingMsgId:  string | null;
  error:           string | null;

  // Computed (not stored, derived)
  activeSession: () => ChatSession | null;
  activeMessages: () => Message[];

  // Actions
  newSession:      () => string;
  setActiveSession:(id: string) => void;
  addUserMessage:  (question: string) => string;
  startStreaming:  () => string;   // returns streaming message id
  appendToken:     (token: string) => void;
  finaliseStream:  (meta: SSEDonePayload) => void;
  setStreamError:  (msg: string) => void;
  cancelStream:    () => void;
  clearSession:    (id?: string) => void;
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------
export const useChatStore = create<ChatStore>()(
  immer((set, get) => ({
    sessions:        [],
    activeSessionId: null,
    isStreaming:     false,
    streamingMsgId:  null,
    error:           null,

    // ---- Derived ----
    activeSession: () => {
      const { sessions, activeSessionId } = get();
      return sessions.find((s) => s.id === activeSessionId) ?? null;
    },
    activeMessages: () => get().activeSession()?.messages ?? [],

    // ---- Actions ----
    newSession: () => {
      const id = makeId();
      const session: ChatSession = {
        id,
        title:     'New Chat',
        messages:  [],
        createdAt: new Date().toISOString(),
        updatedAt: new Date().toISOString(),
      };
      set((s) => {
        s.sessions.unshift(session);
        s.activeSessionId = id;
      });
      return id;
    },

    setActiveSession: (id: string) => {
      set((s) => { s.activeSessionId = id; });
    },

    addUserMessage: (question: string): string => {
      const { activeSessionId, sessions } = get();
      let sessionId = activeSessionId;

      // Auto-create a session if none exists
      if (!sessionId || !sessions.find((s) => s.id === sessionId)) {
        sessionId = get().newSession();
      }

      const msg: Message = {
        id:        makeId(),
        role:      'user',
        content:   question,
        timestamp: new Date().toISOString(),
      };

      set((s) => {
        const session = s.sessions.find((x) => x.id === sessionId);
        if (session) {
          session.messages.push(msg);
          // Auto-title from first message (first 50 chars)
          if (session.messages.length === 1) {
            session.title = question.slice(0, 50) + (question.length > 50 ? '…' : '');
          }
          session.updatedAt = new Date().toISOString();
        }
        s.error = null;
      });

      return msg.id;
    },

    startStreaming: (): string => {
      const msgId = makeId();
      const msg: Message = {
        id:          msgId,
        role:        'assistant',
        content:     '',
        isStreaming: true,
        timestamp:   new Date().toISOString(),
      };
      set((s) => {
        s.isStreaming    = true;
        s.streamingMsgId = msgId;
        s.error          = null;
        const session = s.sessions.find((x) => x.id === s.activeSessionId);
        if (session) session.messages.push(msg);
      });
      return msgId;
    },

    appendToken: (token: string) => {
      const { streamingMsgId, activeSessionId } = get();
      if (!streamingMsgId) return;
      set((s) => {
        const session = s.sessions.find((x) => x.id === activeSessionId);
        const msg     = session?.messages.find((m) => m.id === streamingMsgId);
        if (msg) msg.content += token;
      });
    },

    finaliseStream: (meta: SSEDonePayload) => {
      const { streamingMsgId, activeSessionId } = get();
      set((s) => {
        s.isStreaming    = false;
        s.streamingMsgId = null;
        const session = s.sessions.find((x) => x.id === activeSessionId);
        const msg     = session?.messages.find((m) => m.id === streamingMsgId);
        if (msg) {
          msg.isStreaming  = false;
          msg.latency_ms   = meta.latency_ms;
          msg.chunks_used  = meta.chunks_used;
          msg.output_tokens = meta.output_tokens;
          msg.request_id   = meta.request_id;
        }
      });
    },

    setStreamError: (errMsg: string) => {
      const { streamingMsgId, activeSessionId } = get();
      set((s) => {
        s.isStreaming    = false;
        s.streamingMsgId = null;
        s.error          = errMsg;
        // Remove the empty streaming placeholder
        const session = s.sessions.find((x) => x.id === activeSessionId);
        if (session) {
          session.messages = session.messages.filter((m) => m.id !== streamingMsgId);
        }
      });
    },

    cancelStream: () => {
      const { streamingMsgId, activeSessionId } = get();
      set((s) => {
        s.isStreaming    = false;
        s.streamingMsgId = null;
        const session = s.sessions.find((x) => x.id === activeSessionId);
        const msg     = session?.messages.find((m) => m.id === streamingMsgId);
        if (msg) msg.isStreaming = false;
      });
    },

    clearSession: (id?: string) => {
      set((s) => {
        const targetId = id ?? s.activeSessionId;
        const session  = s.sessions.find((x) => x.id === targetId);
        if (session) session.messages = [];
      });
    },
  }))
);
