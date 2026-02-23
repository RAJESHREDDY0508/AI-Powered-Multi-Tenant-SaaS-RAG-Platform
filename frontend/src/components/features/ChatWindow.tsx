'use client';

import { useEffect, useRef, useCallback } from 'react';
import { clsx }     from 'clsx';
import {
  Send,
  Square,
  RotateCcw,
  Bot,
  User as UserIcon,
  AlertTriangle,
} from 'lucide-react';
import { useForm }        from 'react-hook-form';
import { Button }         from '@/components/ui/Button';
import { CitationList }   from './CitationCard';
import { useChat }        from '@/hooks/useChat';
import { useChatStore }   from '@/store/chatStore';
import type { Message }   from '@/types/chat';

/* ── Markdown-lite renderer (no external dep) ───────────────── */
function renderMarkdown(text: string) {
  return text
    .replace(/```([\s\S]*?)```/g, '<pre class="bg-gray-900 text-gray-100 rounded p-3 overflow-x-auto text-xs my-2 whitespace-pre-wrap">$1</pre>')
    .replace(/`([^`]+)`/g, '<code class="bg-gray-100 dark:bg-gray-800 rounded px-1 py-0.5 text-xs font-mono">$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*]+)\*/g, '<em>$1</em>')
    .replace(/\n/g, '<br />');
}

/* ── Single message bubble ──────────────────────────────────── */
function MessageBubble({ message }: { message: Message }) {
  const isUser      = message.role === 'user';
  const isStreaming  = message.streaming;

  return (
    <div
      className={clsx(
        'flex gap-3 animate-fade-in',
        isUser && 'flex-row-reverse',
      )}
    >
      {/* Avatar */}
      <div
        className={clsx(
          'shrink-0 flex h-8 w-8 items-center justify-center rounded-full text-white text-sm',
          isUser ? 'bg-gray-600' : 'bg-brand-600',
        )}
      >
        {isUser ? <UserIcon className="h-4 w-4" /> : <Bot className="h-4 w-4" />}
      </div>

      {/* Bubble */}
      <div
        className={clsx(
          'max-w-[80%] rounded-2xl px-4 py-3 text-sm leading-relaxed',
          isUser
            ? 'bg-brand-600 text-white rounded-tr-sm'
            : 'bg-white dark:bg-gray-800 text-gray-800 dark:text-gray-100 border border-gray-200 dark:border-gray-700 rounded-tl-sm',
        )}
      >
        {isUser ? (
          <p className="whitespace-pre-wrap">{message.content}</p>
        ) : (
          <>
            <div
              className={clsx('prose prose-sm max-w-none dark:prose-invert', isStreaming && 'streaming-cursor')}
              dangerouslySetInnerHTML={{ __html: renderMarkdown(message.content) }}
            />
            {message.error && (
              <div className="flex items-center gap-2 mt-2 text-red-500 text-xs">
                <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
                <span>{message.error}</span>
              </div>
            )}
            {!isStreaming && message.citations && message.citations.length > 0 && (
              <CitationList citations={message.citations} />
            )}
          </>
        )}
      </div>
    </div>
  );
}

/* ── Typing indicator ─────────────────────────────────────── */
function TypingIndicator() {
  return (
    <div className="flex gap-3">
      <div className="h-8 w-8 rounded-full bg-brand-600 flex items-center justify-center text-white">
        <Bot className="h-4 w-4" />
      </div>
      <div className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-2xl rounded-tl-sm px-4 py-3">
        <div className="flex gap-1 items-center h-5">
          {[0, 1, 2].map((i) => (
            <span
              key={i}
              className="h-2 w-2 rounded-full bg-gray-400 animate-pulse-dot"
              style={{ animationDelay: `${i * 0.2}s` }}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

/* ── Input form ────────────────────────────────────────────── */
interface ChatFormData { question: string }

function ChatInput({ sessionId }: { sessionId: string }) {
  const { sendMessage, cancelStream } = useChat(sessionId);
  const isStreaming = useChatStore((s) =>
    s.sessions[sessionId]?.messages.some((m) => m.streaming),
  );

  const { register, handleSubmit, reset, watch } = useForm<ChatFormData>();
  const question = watch('question', '');

  const onSubmit = async (data: ChatFormData) => {
    if (!data.question.trim()) return;
    reset();
    await sendMessage(data.question.trim());
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit(onSubmit)();
    }
  };

  return (
    <form
      onSubmit={handleSubmit(onSubmit)}
      className="flex items-end gap-2 border-t border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 p-4"
    >
      <textarea
        {...register('question')}
        onKeyDown={handleKeyDown}
        rows={1}
        placeholder="Ask anything about your documents… (Shift+Enter for new line)"
        disabled={isStreaming}
        className={clsx(
          'flex-1 resize-none rounded-xl border border-gray-300 bg-gray-50 px-4 py-2.5 text-sm',
          'dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100',
          'focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent',
          'placeholder:text-gray-400 disabled:opacity-60',
          'max-h-40 overflow-y-auto',
        )}
        style={{ minHeight: '42px' }}
      />

      {isStreaming ? (
        <Button type="button" variant="danger" size="md" onClick={cancelStream}>
          <Square className="h-4 w-4" />
        </Button>
      ) : (
        <Button
          type="submit"
          size="md"
          disabled={!question.trim()}
          leftIcon={<Send className="h-4 w-4" />}
        >
          Send
        </Button>
      )}
    </form>
  );
}

/* ── Main ChatWindow ─────────────────────────────────────── */
interface ChatWindowProps {
  sessionId: string;
}

export function ChatWindow({ sessionId }: ChatWindowProps) {
  const messages  = useChatStore((s) => s.sessions[sessionId]?.messages ?? []);
  const isLoading = useChatStore((s) =>
    s.sessions[sessionId]?.messages.some((m) => m.streaming),
  );
  const clearSession = useChatStore((s) => s.clearSession);

  const bottomRef = useRef<HTMLDivElement>(null);

  // Auto-scroll on new tokens
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const isEmpty = messages.length === 0;

  return (
    <div className="flex flex-col h-full rounded-xl border border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-950 overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900">
        <div className="flex items-center gap-2">
          <Bot className="h-5 w-5 text-brand-500" />
          <span className="text-sm font-semibold text-gray-800 dark:text-gray-100">AI Assistant</span>
        </div>
        {!isEmpty && (
          <button
            onClick={() => clearSession(sessionId)}
            className="flex items-center gap-1 text-xs text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
          >
            <RotateCcw className="h-3.5 w-3.5" />
            New chat
          </button>
        )}
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {isEmpty ? (
          <div className="flex flex-col items-center justify-center h-full text-center gap-3 text-gray-400">
            <Bot className="h-12 w-12 text-gray-300" />
            <p className="text-sm font-medium">Ask me anything about your documents</p>
            <p className="text-xs">I&apos;ll search across all your uploaded knowledge base</p>
          </div>
        ) : (
          <>
            {messages.map((msg) => (
              <MessageBubble key={msg.id} message={msg} />
            ))}
            {isLoading && messages[messages.length - 1]?.role !== 'assistant' && (
              <TypingIndicator />
            )}
          </>
        )}
        <div ref={bottomRef} />
      </div>

      {/* Input */}
      <ChatInput sessionId={sessionId} />
    </div>
  );
}
